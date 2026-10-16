
import difflib
import csv
import io
import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy import inspect, or_, select, text

from models import db, User, Monitor, Snapshot, SnapshotMetric, localtime
from scraper import canonicalize_url
from scheduler import start_scheduler, sync_jobs, _check_url, split_selectors
from image_diff import create_image_diff, create_side_by_side

load_dotenv()


def current_user():
    user_id = session.get("user_id")
    if user_id is not None:
        return db.session.get(User, user_id)
    return db.session.scalar(select(User).where(User.email == "demo@example.com"))


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY", "local-development-key-change-me"),
        SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", "sqlite:///website_changes.db"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        MAX_CONTENT_LENGTH=1_000_000,
        TIMEZONE=os.getenv("APP_TIMEZONE", "Asia/Kolkata"),
    )

    if test_config:
        app.config.update(test_config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    app.config["SCREENSHOT_DIR"] = str(Path(os.getenv("SCREENSHOT_DIR", "data/screenshots")).resolve())
    Path(app.config["SCREENSHOT_DIR"]).mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    csrf = CSRFProtect()
    csrf.init_app(app)

    @app.context_processor
    def inject_current_user():
        return {"current_user": current_user()}

    @app.template_filter("localtime")
    def localtime_filter(value):
        return localtime(value, app.config["TIMEZONE"]).strftime("%Y-%m-%d %H:%M:%S %Z")

    with app.app_context():
        db.create_all()

        monitor_columns = {column["name"] for column in inspect(db.engine).get_columns("monitor")}
        snapshot_columns = {column["name"] for column in inspect(db.engine).get_columns("snapshot")}
        migrations = []
        if "alert_email" not in monitor_columns:
            migrations.append("ALTER TABLE monitor ADD COLUMN alert_email VARCHAR(255) NOT NULL DEFAULT ''")
        if "daily_digest" not in monitor_columns:
            migrations.append("ALTER TABLE monitor ADD COLUMN daily_digest BOOLEAN NOT NULL DEFAULT 0")
        if "summary" not in snapshot_columns:
            migrations.append("ALTER TABLE snapshot ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
        outbox_columns = {column["name"] for column in inspect(db.engine).get_columns("notification_outbox")}
        if "locked_at" not in outbox_columns:
            migrations.append("ALTER TABLE notification_outbox ADD COLUMN locked_at DATETIME")
        if migrations:
            with db.engine.begin() as connection:
                for statement in migrations:
                    connection.execute(text(statement))

        demo = db.session.scalar(select(User).where(User.email == "demo@example.com"))
        if demo is None:
            demo = User(email="demo@example.com", password_hash="!disabled!")
            db.session.add(demo)
            db.session.commit()

    def get_owned_monitor(monitor_id):
        user = current_user()
        monitor = db.session.scalar(
            select(Monitor).where(
                Monitor.id == monitor_id,
                Monitor.user_id == user.id,
            )
        )
        if monitor is None:
            abort(404)
        return monitor

    def get_monitors():
        query = select(Monitor).where(Monitor.user_id == current_user().id)
        search = request.args.get("q", "").strip()
        status = request.args.get("status", "").strip().upper()
        enabled = request.args.get("enabled", "").strip().casefold()
        if search:
            search_term = f"%{search}%"
            query = query.where(
                or_(
                    Monitor.name.ilike(search_term),
                    Monitor.url.ilike(search_term),
                    Monitor.canonical_url.ilike(search_term),
                )
            )
        if status in {"PENDING", "OK", "ERROR", "BLOCKED"}:
            query = query.where(Monitor.status == status)
        if enabled == "active":
            query = query.where(Monitor.enabled.is_(True))
        elif enabled == "paused":
            query = query.where(Monitor.enabled.is_(False))
        return db.session.scalars(query.order_by(Monitor.id.desc())).all()

    def validate_selectors(value):
        from bs4 import BeautifulSoup

        for selector in split_selectors(value):
            BeautifulSoup("<html><body></body></html>", "html.parser").select(selector)

    @app.get("/register")
    def register():
        return render_template("auth.html", mode="register")

    @app.post("/register")
    def register_post():
        email = request.form.get("email", "").strip().casefold()
        password = request.form.get("password", "")
        if len(email) > 255 or "@" not in email or len(password) < 10:
            flash("Use a valid email address and a password of at least 10 characters.", "error")
            return redirect(url_for("register"))
        if db.session.scalar(select(User).where(User.email == email)):
            flash("An account with that email already exists.", "error")
            return redirect(url_for("login"))
        user = User(email=email, password_hash=generate_password_hash(password))
        db.session.add(user)
        db.session.commit()
        session.clear()
        session["user_id"] = user.id
        return redirect(url_for("dashboard"))

    @app.get("/login")
    def login():
        return render_template("auth.html", mode="login")

    @app.post("/login")
    def login_post():
        email = request.form.get("email", "").strip().casefold()
        password = request.form.get("password", "")
        user = db.session.scalar(select(User).where(User.email == email))
        if user is None or not user.password_hash or not check_password_hash(user.password_hash, password):
            flash("Invalid email or password.", "error")
            return redirect(url_for("login"))
        session.clear()
        session["user_id"] = user.id
        return redirect(url_for("dashboard"))

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def dashboard():
        return render_template("dashboard.html", monitors=get_monitors())

    @app.get("/partials/monitors")
    def monitor_rows():
        return render_template("monitor_rows.html", monitors=get_monitors())

    @app.post("/monitors")
    def create_monitor():
        name = request.form.get("name", "").strip()
        raw_url = request.form.get("url", "").strip()
        ignore_selectors = request.form.get("ignore_selectors", "").strip()
        watch_selectors = request.form.get("watch_selectors", "").strip()
        alert_email = request.form.get("alert_email", "").strip()

        try:
            canonical_url = canonicalize_url(raw_url)
            interval = int(request.form.get("interval_minutes", "60"))
            validate_selectors(ignore_selectors)
            validate_selectors(watch_selectors)
        except (ValueError, TypeError) as exc:
            flash(str(exc) or "Invalid URL, interval, or CSS selector.", "error")
            return redirect(url_for("dashboard"))

        if alert_email:
            import re

            if len(alert_email) > 255 or not re.fullmatch(
                r"[^@\s]+@[^@\s]+\.[^@\s]+", alert_email
            ):
                flash("Enter a valid email address for alerts.", "error")
                return redirect(url_for("dashboard"))

        daily_digest = request.form.get("daily_digest") == "on"
        if daily_digest and not alert_email:
            flash("Enter an alert email address to enable daily digests.", "error")
            return redirect(url_for("dashboard"))

        if not name or len(name) > 160:
            flash("Enter a name of at most 160 characters.", "error")
            return redirect(url_for("dashboard"))

        if not 15 <= interval <= 1440:
            flash("Check interval must be between 15 minutes and 24 hours.", "error")
            return redirect(url_for("dashboard"))

        sensitivity = request.form.get("sensitivity", "normal")
        if sensitivity not in {"low", "normal", "high"}:
            flash("Invalid sensitivity.", "error")
            return redirect(url_for("dashboard"))

        monitor = Monitor(
            user_id=current_user().id,
            name=name,
            url=raw_url,
            canonical_url=canonical_url,
            interval_minutes=interval,
            ignore_selectors=ignore_selectors,
            watch_selectors=watch_selectors,
            sensitivity=sensitivity,
            keywords=request.form.get("keywords", "").strip(),
            alert_email=alert_email,
            daily_digest=daily_digest,
        )
        db.session.add(monitor)
        db.session.commit()
        sync_jobs(app)
        flash("Monitor created. Its first successful check establishes the baseline.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/monitors/<int:monitor_id>/check")
    def check_monitor_now(monitor_id):
        monitor = get_owned_monitor(monitor_id)

        if not monitor.enabled:
            flash("This monitor is paused.", "error")
            return redirect(url_for("dashboard"))

        status = _check_url(app, monitor.canonical_url)

        if status == "ALREADY_RUNNING":
            flash("A check for this URL is already running.", "error")
        elif status in {"ERROR", "BLOCKED"}:
            db.session.refresh(monitor)
            flash(f"Check failed: {monitor.status_message or status}", "error")
        elif status == "NO_MONITORS":
            flash("No enabled monitors were found for this URL.", "error")
        else:
            flash("Check finished.", "success")

        return redirect(url_for("dashboard"))

    @app.post("/monitors/<int:monitor_id>/toggle")
    def toggle_monitor(monitor_id):
        monitor = get_owned_monitor(monitor_id)
        monitor.enabled = not monitor.enabled
        db.session.commit()
        sync_jobs(app)
        return redirect(url_for("dashboard"))

    @app.post("/monitors/<int:monitor_id>/delete")
    def delete_monitor(monitor_id):
        monitor = get_owned_monitor(monitor_id)
        db.session.delete(monitor)
        db.session.commit()
        sync_jobs(app)
        return redirect(url_for("dashboard"))

    @app.get("/monitors/<int:monitor_id>")
    def monitor_detail(monitor_id):
        monitor = get_owned_monitor(monitor_id)
        snapshots = db.session.scalars(
            select(Snapshot)
            .where(Snapshot.monitor_id == monitor.id)
            .order_by(Snapshot.created_at.desc(), Snapshot.id.desc())
        ).all()
        return render_template("monitor_detail.html", monitor=monitor, snapshots=snapshots)

    @app.get("/monitors/<int:monitor_id>/export.csv")
    def export_monitor_history(monitor_id):
        monitor = get_owned_monitor(monitor_id)
        snapshots = db.session.scalars(
            select(Snapshot)
            .where(Snapshot.monitor_id == monitor.id)
            .order_by(Snapshot.created_at.asc(), Snapshot.id.asc())
        ).all()
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            [
                "monitor_name",
                "monitor_url",
                "version",
                "snapshot_id",
                "created_at_ist",
                "http_status",
                "content_hash",
                "summary",
                "content_text",
            ]
        )
        for version, snapshot in enumerate(snapshots, start=1):
            writer.writerow(
                [
                    monitor.name,
                    monitor.url,
                    version,
                    snapshot.id,
                    localtime(snapshot.created_at, app.config["TIMEZONE"]).strftime(
                        "%Y-%m-%d %H:%M:%S %Z"
                    ),
                    snapshot.http_status,
                    snapshot.content_hash,
                    snapshot.summary,
                    snapshot.content_text,
                ]
            )
        filename = f"{monitor.name[:80].strip() or 'monitor'}-change-history.csv"
        response = Response(output.getvalue(), mimetype="text/csv")
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    @app.get("/monitors/<int:monitor_id>/diff/<int:old_id>/<int:new_id>")
    def snapshot_diff(monitor_id, old_id, new_id):
        monitor = get_owned_monitor(monitor_id)
        old = db.session.scalar(
            select(Snapshot).where(
                Snapshot.id == old_id,
                Snapshot.monitor_id == monitor.id,
            )
        )
        new = db.session.scalar(
            select(Snapshot).where(
                Snapshot.id == new_id,
                Snapshot.monitor_id == monitor.id,
            )
        )

        if old is None or new is None:
            abort(404)

        diff_html = difflib.HtmlDiff(wrapcolumn=80).make_table(
            old.content_text.splitlines(),
            new.content_text.splitlines(),
            fromdesc="Previous version",
            todesc="Current version",
            context=True,
            numlines=3,
        )
        return render_template("diff.html", monitor=monitor, old=old, new=new, diff_html=diff_html)

    @app.post("/monitors/<int:monitor_id>/compare")
    def compare_snapshots(monitor_id):
        monitor = get_owned_monitor(monitor_id)
        old_id = request.form.get("old_id", type=int)
        new_id = request.form.get("new_id", type=int)
        if old_id is None or new_id is None or old_id == new_id:
            flash("Choose two different snapshots to compare.", "error")
            return redirect(url_for("monitor_detail", monitor_id=monitor.id))
        snapshot_ids = db.session.scalars(
            select(Snapshot.id).where(
                Snapshot.monitor_id == monitor.id,
                Snapshot.id.in_([old_id, new_id]),
            )
        ).all()
        if len(snapshot_ids) != 2:
            abort(404)
        return redirect(url_for("snapshot_diff", monitor_id=monitor.id, old_id=old_id, new_id=new_id))

    def checked_screenshot(snapshot):
        if snapshot is None or snapshot.monitor.user_id != current_user().id:
            abort(404)

        path = Path(snapshot.screenshot_path or "").resolve()
        root = Path(app.config["SCREENSHOT_DIR"]).resolve()

        if not path.is_relative_to(root) or not path.is_file():
            abort(404)

        return path

    @app.get("/snapshots/<int:snapshot_id>/screenshot")
    def snapshot_screenshot(snapshot_id):
        snapshot = db.session.get(Snapshot, snapshot_id)
        path = checked_screenshot(snapshot)
        return send_file(path, mimetype="image/png", conditional=True)

    @app.get("/monitors/<int:monitor_id>/visual-diff/<int:old_id>/<int:new_id>")
    def visual_diff(monitor_id, old_id, new_id):
        monitor = get_owned_monitor(monitor_id)
        old = db.session.scalar(
            select(Snapshot).where(
                Snapshot.id == old_id,
                Snapshot.monitor_id == monitor.id,
            )
        )
        new = db.session.scalar(
            select(Snapshot).where(
                Snapshot.id == new_id,
                Snapshot.monitor_id == monitor.id,
            )
        )

        if old is None or new is None:
            abort(404)

        old_path = checked_screenshot(old)
        new_path = checked_screenshot(new)
        target = Path(app.config["SCREENSHOT_DIR"]) / f"diff-{monitor.id}-{old.id}-{new.id}.png"
        create_image_diff(str(old_path), str(new_path), target)
        return send_file(target, mimetype="image/png", conditional=True)

    @app.get("/monitors/<int:monitor_id>/side-by-side/<int:old_id>/<int:new_id>")
    def side_by_side(monitor_id, old_id, new_id):
        monitor = get_owned_monitor(monitor_id)
        old = db.session.scalar(
            select(Snapshot).where(
                Snapshot.id == old_id,
                Snapshot.monitor_id == monitor.id,
            )
        )
        new = db.session.scalar(
            select(Snapshot).where(
                Snapshot.id == new_id,
                Snapshot.monitor_id == monitor.id,
            )
        )

        if old is None or new is None:
            abort(404)

        old_path = checked_screenshot(old)
        new_path = checked_screenshot(new)
        target = Path(app.config["SCREENSHOT_DIR"]) / f"compare-{monitor.id}-{old.id}-{new.id}.png"
        create_side_by_side(str(old_path), str(new_path), target)
        return send_file(target, mimetype="image/png", conditional=True)

    @app.get("/monitors/<int:monitor_id>/metrics")
    def monitor_metrics(monitor_id):
        monitor = get_owned_monitor(monitor_id)
        metric_name = request.args.get("name", "").strip()
        query = select(SnapshotMetric).join(Snapshot).where(Snapshot.monitor_id == monitor.id)
        if metric_name:
            query = query.where(SnapshotMetric.name == metric_name)
        metrics = db.session.scalars(query.order_by(Snapshot.created_at.asc(), SnapshotMetric.id.asc()).limit(1000)).all()
        return jsonify({"metrics": [{"name": metric.name, "value": metric.value, "unit": metric.unit, "context": metric.context, "snapshot_id": metric.snapshot_id, "created_at": metric.snapshot.created_at.isoformat()} for metric in metrics], "names": sorted({metric.name for metric in db.session.scalars(select(SnapshotMetric).join(Snapshot).where(Snapshot.monitor_id == monitor.id)).all()})})

    @app.get("/monitors/<int:monitor_id>/picker")
    def selector_picker(monitor_id):
        monitor = get_owned_monitor(monitor_id)
        return render_template("picker.html", monitor=monitor)

    @app.post("/monitors/<int:monitor_id>/picker")
    def save_picked_selector(monitor_id):
        monitor = get_owned_monitor(monitor_id)
        selector = request.form.get("selector", "").strip()
        mode = request.form.get("mode", "watch")

        if mode not in {"watch", "ignore"} or not selector or len(selector) > 500:
            abort(400)

        try:
            validate_selectors(selector)
        except Exception:
            flash("Invalid CSS selector.", "error")
            return redirect(url_for("selector_picker", monitor_id=monitor.id))

        field = "watch_selectors" if mode == "watch" else "ignore_selectors"
        existing = split_selectors(getattr(monitor, field))
        if selector not in existing:
            existing.append(selector)
        setattr(monitor, field, ", ".join(existing))
        db.session.commit()
        flash("Selector saved.", "success")
        return redirect(url_for("monitor_detail", monitor_id=monitor.id))


    return app


app = create_app()

if __name__ == "__main__":
    start_scheduler(app)
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5000"))
    print(f"Website Change Detector running at http://{host}:{port}", flush=True)
    app.run(host=host, port=port, debug=False, use_reloader=False)
