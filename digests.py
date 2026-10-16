import logging
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo
from sqlalchemy import select
from models import db, Monitor, Snapshot, DigestDelivery

log = logging.getLogger(__name__)


def _send_digest(recipient, subject, body):
    host = os.getenv("SMTP_HOST", "").strip()
    if not host:
        raise RuntimeError("SMTP_HOST is not configured")
    user = os.getenv("SMTP_USER", "").strip()
    sender = os.getenv("SMTP_FROM", user).strip()
    if not sender:
        raise RuntimeError("SMTP_FROM or SMTP_USER must be configured")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(body)
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=15) as smtp:
        if os.getenv("SMTP_STARTTLS", "true").casefold() == "true":
            smtp.starttls()
        if user:
            smtp.login(user, os.getenv("SMTP_PASSWORD", ""))
        smtp.send_message(msg)


def send_daily_digests(app, now=None):
    timezone_name = app.config.get("TIMEZONE", os.getenv("APP_TIMEZONE", "Asia/Kolkata"))
    local_zone = ZoneInfo(timezone_name)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_utc = now.astimezone(timezone.utc)
    today = now.astimezone(local_zone).date().isoformat()
    since = now_utc - timedelta(hours=24)
    sent = 0
    with app.app_context():
        monitors = db.session.scalars(select(Monitor).where(Monitor.enabled.is_(True), Monitor.daily_digest.is_(True), Monitor.alert_email != "")).all()
        for monitor in monitors:
            recipient = monitor.alert_email.strip()
            if not recipient:
                continue
            already = db.session.scalar(select(DigestDelivery.id).where(DigestDelivery.monitor_id == monitor.id, DigestDelivery.digest_date == today, DigestDelivery.recipient == recipient).limit(1))
            if already:
                continue
            snapshots = db.session.scalars(
                select(Snapshot)
                .where(
                    Snapshot.monitor_id == monitor.id,
                    Snapshot.created_at >= since,
                )
                .order_by(Snapshot.created_at.desc(), Snapshot.id.desc())
                .limit(100)
            ).all()
            if len(snapshots) == 1:
                first_snapshot = db.session.scalar(
                    select(Snapshot.id)
                    .where(Snapshot.monitor_id == monitor.id)
                    .order_by(Snapshot.created_at.asc(), Snapshot.id.asc())
                    .limit(1)
                )
                if snapshots[0].id == first_snapshot:
                    snapshots = []
            if not snapshots:
                continue
            lines = [f"Daily website change digest: {monitor.name}", monitor.url, f"Changes recorded in the last 24 hours: {len(snapshots)}", ""]
            for snapshot in snapshots:
                display_time = snapshot.created_at
                if display_time.tzinfo is None:
                    display_time = display_time.replace(tzinfo=timezone.utc)
                lines.extend([f"{display_time.astimezone(local_zone).strftime('%Y-%m-%d %H:%M:%S %Z')} — {snapshot.title or monitor.name}", snapshot.summary or snapshot.content_text[:500], ""])
            try:
                _send_digest(recipient, f"Daily digest: {monitor.name}", "\n".join(lines)[:50000])
                db.session.add(DigestDelivery(monitor_id=monitor.id, digest_date=today, recipient=recipient))
                db.session.commit()
                sent += 1
            except Exception:
                db.session.rollback()
                log.exception("Daily digest failed for monitor %s", monitor.id)
    return sent
