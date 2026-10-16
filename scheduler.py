import hashlib
import logging
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler
from bs4 import BeautifulSoup
from sqlalchemy import select, update
from models import db, Monitor, Snapshot, CheckQueue
from scraper import scrape_url, hash_content
from metrics import save_snapshot_metrics
from summaries import summarize_change

log = logging.getLogger(__name__)
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Kolkata")
scheduler = BackgroundScheduler(timezone=ZoneInfo(APP_TIMEZONE))
MIN_INTERVAL, MAX_INTERVAL = 15, 1440
_url_locks = {}
_url_locks_guard = threading.Lock()
_domain_locks = {}
_domain_locks_guard = threading.Lock()
_domain_last_check = {}


def get_url_lock(url):
    with _url_locks_guard:
        return _url_locks.setdefault(url, threading.Lock())


def split_selectors(value):
    parts, current, quote = [], [], None
    depth_paren = depth_bracket = 0
    escaped = False
    for char in value or "":
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == chr(92):
            current.append(char)
            escaped = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            current.append(char)
        elif char == "(":
            depth_paren += 1
            current.append(char)
        elif char == ")":
            if depth_paren == 0:
                raise ValueError("Unmatched closing parenthesis in CSS selector")
            depth_paren -= 1
            current.append(char)
        elif char == "[":
            depth_bracket += 1
            current.append(char)
        elif char == "]":
            if depth_bracket == 0:
                raise ValueError("Unmatched closing bracket in CSS selector")
            depth_bracket -= 1
            current.append(char)
        elif char == "," and depth_paren == 0 and depth_bracket == 0:
            selector = "".join(current).strip()
            if not selector:
                raise ValueError("Empty CSS selector")
            parts.append(selector)
            current = []
        else:
            current.append(char)
    selector = "".join(current).strip()
    if quote or depth_paren or depth_bracket or escaped:
        raise ValueError("Incomplete CSS selector")
    if selector:
        parts.append(selector)
    elif parts and (value or "").rstrip().endswith(","):
        raise ValueError("Empty CSS selector")
    return parts
def delete_unused_screenshot(path, app):
    if not path:
        return
    try:
        with app.app_context():
            used = db.session.scalar(select(Snapshot.id).where(Snapshot.screenshot_path == path).limit(1))
            db.session.rollback()
        if used is None:
            Path(path).unlink(missing_ok=True)
    except Exception:
        log.exception("Screenshot cleanup failed")


DEFAULT_NOISE_SELECTORS = (
    '[aria-live="polite"]',
    '[aria-live="assertive"]',
    '[id^="ad-"]',
    '[id^="ad_"]',
    '[id*="-ad-"]',
    '[class~="ad"]',
    '[class*="ad-"]',
    '[class*="-ad-"]',
    '[class*="advert"]',
    '[class*="sponsor"]',
    '[class*="cookie"]',
    '[class*="consent"]',
    '[class*="banner"]',
    '[class*="carousel"]',
    '[class*="slider"]',
)


def clean_monitor_content(html, monitor, preserve_numbers=False):
    soup = BeautifulSoup(html, "html.parser")
    selectors = list(DEFAULT_NOISE_SELECTORS)
    selectors.extend(split_selectors(getattr(monitor, "ignore_selectors", "")))
    for selector in selectors:
        try:
            for node in soup.select(selector):
                node.decompose()
        except Exception as exc:
            raise ValueError(f"Invalid ignore selector {selector!r}") from exc
    watched = split_selectors(getattr(monitor, "watch_selectors", ""))
    if watched:
        pieces = []
        for selector in watched:
            try:
                pieces.extend(node.get_text(" ", strip=True) for node in soup.select(selector))
            except Exception as exc:
                raise ValueError(f"Invalid watch selector {selector!r}") from exc
        text = "\n".join(pieces)
    else:
        for node in soup.select("script, style, noscript, template, svg, iframe, canvas"):
            node.decompose()
        text = soup.get_text("\n", strip=True)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for line in text.splitlines():
        line = " ".join(line.split())
        line = re.sub(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?\b", "<TIMESTAMP>", line)
        line = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", "<TIME>", line)
        line = re.sub(r"\b[0-9a-f]{16,}\b", "<TOKEN>", line, flags=re.I)
        line = re.sub(r"\b(?:session|nonce|token|request)[_-]?(?:id)?[=: ]+[A-Za-z0-9_-]{8,}\b", "<TOKEN>", line, flags=re.I)
        line = re.sub(
            r"\b[A-Za-z0-9_-]{24,}\b",
            lambda match: "<TOKEN>"
            if (
                any(char.isalpha() for char in match.group())
                and any(char.isdigit() for char in match.group())
            )
            else match.group(),
            line,
        )
        line = re.sub(r"\b(?:updated|refreshed|last checked)\s*:?\s*(?:just now|\d+\s*(?:seconds?|minutes?)\s*ago)\b", "<DYNAMIC_TIME>", line, flags=re.I)
        line = re.sub(
            r"\b(?:ad|ads|advertisement|banner|cookie|consent|"
            r"impression|view(?:s)?|likes?|comments?|share(?:s)?|"
            r"online|active)\s*(?:count|counter)?\s*[:=]?\s*\d+\b",
            "<DYNAMIC_COUNTER>",
            line,
            flags=re.I,
        )
        if line:
            lines.append(line)
    text = "\n".join(lines)
    sensitivity = getattr(monitor, "sensitivity", None) or "normal"
    if preserve_numbers:
        return text.casefold() if sensitivity == "low" else text
    if sensitivity == "low":
        text = re.sub(r"\b\d+\b", "<NUMBER>", text).casefold()
    elif sensitivity == "normal":
        text = re.sub(r"\b\d{5,}\b", "<NUMBER>", text)
    elif sensitivity != "high":
        raise ValueError("Unknown sensitivity setting.")
    return text


def _domain_guard(url):
    host = (urlsplit(url).hostname or "").lower()
    with _domain_locks_guard:
        lock = _domain_locks.setdefault(host, threading.Lock())
    return host, lock


def _check_url_locked(app, url):
    host, domain_lock = _domain_guard(url)
    with domain_lock:
        wait = float(os.getenv("DOMAIN_MIN_INTERVAL_SECONDS", "2"))
        remaining = wait - (time.monotonic() - _domain_last_check.get(host, 0))
        if remaining > 0:
            time.sleep(remaining)
        _domain_last_check[host] = time.monotonic()
        return _check_url_for_monitors(app, url)


def _check_url_for_monitors(app, url):
    with app.app_context():
        monitors = db.session.scalars(select(Monitor).where(Monitor.canonical_url == url, Monitor.enabled.is_(True))).all()
        if not monitors:
            return "NO_MONITORS"
        result = scrape_url(url, app.config["SCREENSHOT_DIR"], timeout_ms=int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", "25000")), retries=int(os.getenv("SCRAPE_RETRIES", "2")))
        now = datetime.now(timezone.utc)
        if not result.ok:
            for monitor in monitors:
                monitor.status = result.status
                monitor.status_message = result.message[:500]
                monitor.last_checked_at = now
            db.session.commit()
            return result.status
        pending = []
        try:
            for monitor in monitors:
                monitor.last_checked_at = now
                monitor.last_success_at = now
                monitor.status, monitor.status_message = "OK", ""
                try:
                    content = clean_monitor_content(result.html, monitor)
                    metrics_content = clean_monitor_content(
                        result.html, monitor, preserve_numbers=True
                    )
                except ValueError as exc:
                    monitor.status, monitor.status_message = "ERROR", str(exc)[:500]
                    continue
                digest = hash_content(content)
                previous = db.session.scalar(select(Snapshot).where(Snapshot.monitor_id == monitor.id).order_by(Snapshot.created_at.desc(), Snapshot.id.desc()).limit(1))
                if previous and previous.content_hash == digest:
                    continue
                summary = ""
                if previous:
                    try:
                        summary = summarize_change(previous.content_text, content, result.title)
                    except Exception:
                        log.exception("AI summary generation failed for monitor %s", monitor.id)
                snapshot = Snapshot(monitor_id=monitor.id, content_hash=digest, content_text=content, title=result.title, screenshot_path=result.screenshot_path, http_status=result.http_status or 200, summary=summary)
                save_snapshot_metrics(snapshot, metrics_content)
                db.session.add(snapshot)
                db.session.flush()
                if previous:
                    monitor.last_changed_at = now
                    pending.append((monitor.id, previous.id, snapshot.id))
            db.session.commit()
        except Exception:
            db.session.rollback()
            delete_unused_screenshot(result.screenshot_path, app)
            log.exception("Check processing failed for %s", url)
            return "ERROR"
        used = bool(result.screenshot_path and db.session.scalar(select(Snapshot.id).where(Snapshot.screenshot_path == result.screenshot_path).limit(1)))
        if not used:
            delete_unused_screenshot(result.screenshot_path, app)
        if pending:
            from alerts import notify_change
            for monitor_id, old_id, new_id in pending:
                try:
                    monitor = db.session.get(Monitor, monitor_id)
                    previous = db.session.get(Snapshot, old_id)
                    current = db.session.get(Snapshot, new_id)
                    if monitor and previous and current:
                        notify_change(app, monitor, previous, current)
                except Exception:
                    log.exception("Notification processing failed for monitor %s", monitor_id)
        return "OK"


def _check_url(app, url):
    lock = get_url_lock(url)
    if not lock.acquire(blocking=False):
        return "ALREADY_RUNNING"
    try:
        return _check_url_locked(app, url)
    finally:
        lock.release()


def enqueue_check(app, url):
    with app.app_context():
        row = db.session.scalar(select(CheckQueue).where(CheckQueue.canonical_url == url))
        now = datetime.now(timezone.utc)
        if row is None:
            db.session.add(CheckQueue(canonical_url=url, status="PENDING", due_at=now, attempts=0))
        elif row.status != "RUNNING":
            row.status, row.due_at, row.attempts, row.last_error = "PENDING", now, 0, ""
        db.session.commit()


def process_check_queue(app, limit=5):
    processed = 0
    with app.app_context():
        stale = datetime.now(timezone.utc) - timedelta(minutes=5)
        stale_rows = db.session.scalars(select(CheckQueue).where(CheckQueue.status == "RUNNING", CheckQueue.locked_at < stale)).all()
        for row in stale_rows:
            row.status, row.locked_at, row.due_at = "PENDING", None, datetime.now(timezone.utc)
        db.session.commit()
        now = datetime.now(timezone.utc)
        candidate_ids = db.session.scalars(
            select(CheckQueue.id)
            .where(CheckQueue.status == "PENDING", CheckQueue.due_at <= now)
            .order_by(CheckQueue.due_at, CheckQueue.id)
            .limit(limit)
        ).all()
        claimed_ids = []
        for row_id in candidate_ids:
            claimed = db.session.execute(
                update(CheckQueue)
                .where(
                    CheckQueue.id == row_id,
                    CheckQueue.status == "PENDING",
                    CheckQueue.due_at <= now,
                )
                .values(status="RUNNING", locked_at=datetime.now(timezone.utc))
            ).rowcount
            if not claimed:
                continue
            db.session.commit()
            claimed_ids.append(row_id)

        for row_id in claimed_ids:
            row = db.session.get(CheckQueue, row_id)
            if row is None:
                continue
            status = _check_url(app, row.canonical_url)
            row = db.session.get(CheckQueue, row_id)
            if row is None:
                continue
            row.locked_at = None
            if status in {"OK", "NO_MONITORS", "BLOCKED"}:
                row.status = "DONE"
                row.last_error = "" if status == "OK" else status
            else:
                row.attempts += 1
                row.last_error = status
                if row.attempts >= 5:
                    row.status = "FAILED"
                else:
                    row.status = "PENDING"
                    row.due_at = datetime.now(timezone.utc) + timedelta(seconds=min(30 * (2 ** (row.attempts - 1)), 1800))
            db.session.commit()
            processed += 1
    return processed


def _enqueue_job(app, url):
    enqueue_check(app, url)


def sync_jobs(app):
    with app.app_context():
        monitors = db.session.scalars(select(Monitor).where(Monitor.enabled.is_(True))).all()
        grouped = defaultdict(list)
        for monitor in monitors:
            grouped[monitor.canonical_url].append(monitor.interval_minutes)
        wanted = {"url:" + hashlib.sha256(url.encode()).hexdigest(): (url, intervals) for url, intervals in grouped.items()}
        for job in scheduler.get_jobs():
            if job.id.startswith("url:") and job.id not in wanted:
                scheduler.remove_job(job.id)
        for job_id, (url, intervals) in wanted.items():
            minutes = max(MIN_INTERVAL, min(min(intervals), MAX_INTERVAL))
            scheduler.add_job(_enqueue_job, "interval", minutes=minutes, args=[app, url], id=job_id, replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=60)
            if any(not monitor.snapshots for monitor in monitors if monitor.canonical_url == url):
                enqueue_check(app, url)
        if scheduler.running:
            scheduler.add_job(process_check_queue, "interval", seconds=10, args=[app], id="check-queue-worker", replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=30)
            scheduler.add_job(_retry_notifications, "interval", minutes=1, args=[app], id="notification-retry-worker", replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=60)
            scheduler.add_job(_run_daily_digest, "cron", hour=int(os.getenv("DAILY_DIGEST_HOUR", os.getenv("DAILY_DIGEST_HOUR_UTC", "8"))), minute=0, timezone=APP_TIMEZONE, args=[app], id="daily-digest-worker", replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=3600)


def _retry_notifications(app):
    try:
        from alerts import dispatch_pending_notifications
        dispatch_pending_notifications(app)
    except Exception:
        log.exception("Notification retry worker failed")


def _run_daily_digest(app):
    try:
        from digests import send_daily_digests
        send_daily_digests(app)
    except Exception:
        log.exception("Daily digest worker failed")


def start_scheduler(app):
    if not scheduler.running:
        scheduler.start()
    with app.app_context():
        stale = datetime.now(timezone.utc) - timedelta(minutes=5)
        rows = db.session.scalars(select(CheckQueue).where(CheckQueue.status == "RUNNING", CheckQueue.locked_at < stale)).all()
        for row in rows:
            row.status, row.locked_at, row.due_at = "PENDING", None, datetime.now(timezone.utc)
        db.session.commit()
    scheduler.add_job(process_check_queue, "interval", seconds=10, args=[app], id="check-queue-worker", replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=30)
    scheduler.add_job(_retry_notifications, "interval", minutes=1, args=[app], id="notification-retry-worker", replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=60)
    scheduler.add_job(_run_daily_digest, "cron", hour=int(os.getenv("DAILY_DIGEST_HOUR", os.getenv("DAILY_DIGEST_HOUR_UTC", "8"))), minute=0, timezone=APP_TIMEZONE, args=[app], id="daily-digest-worker", replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=3600)
    sync_jobs(app)
