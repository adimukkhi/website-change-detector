import difflib
import logging
import os
import smtplib
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage

import requests
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from models import db, Monitor, NotificationDelivery, NotificationOutbox

log = logging.getLogger(__name__)


def changed_text(old_text, new_text):
    old = (old_text or "").splitlines()
    new = (new_text or "").splitlines()
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    result = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in {"delete", "replace"}:
            result.extend(old[i1:i2])
        if tag in {"insert", "replace"}:
            result.extend(new[j1:j2])
    return "\n".join(result)


def _channels(monitor, payload):
    channels = []
    discord = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    email_to = (getattr(monitor, "alert_email", "") or "").strip()
    if discord:
        channels.append(("discord", discord, payload))
    if token and chat:
        channels.append(("telegram", f"{token}:{chat}", payload))
    if email_to and os.getenv("SMTP_HOST", "").strip():
        channels.append(("email", email_to, payload))
    return channels


def _send_email(recipient, payload, monitor_name):
    host = os.getenv("SMTP_HOST", "").strip()
    user = os.getenv("SMTP_USER", "").strip()
    sender = os.getenv("SMTP_FROM", user).strip()
    if not host or not sender:
        raise RuntimeError("SMTP_HOST and SMTP_FROM or SMTP_USER must be configured")

    message = EmailMessage()
    message["Subject"] = f"Website change: {monitor_name}"
    message["From"] = sender
    message["To"] = recipient
    message.set_content(payload)

    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=15) as smtp:
        if os.getenv("SMTP_STARTTLS", "true").casefold() == "true":
            smtp.starttls()
        if user:
            smtp.login(user, os.getenv("SMTP_PASSWORD", ""))
        smtp.send_message(message)


def _send(outbox, monitor_name):
    if outbox.channel == "discord":
        response = requests.post(
            outbox.recipient,
            json={"content": outbox.payload[:1900]},
            timeout=10,
        )
        response.raise_for_status()
        return

    if outbox.channel == "telegram":
        token, chat = outbox.recipient.split(":", 1)
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat,
                "text": outbox.payload[:4000],
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        response.raise_for_status()
        if not response.json().get("ok", False):
            raise RuntimeError("Telegram API did not confirm delivery")
        return

    if outbox.channel == "email":
        _send_email(outbox.recipient, outbox.payload, monitor_name)
        return

    raise RuntimeError(f"Unsupported notification channel: {outbox.channel}")


def dispatch_pending_notifications(app, limit=100):
    delivered = 0
    now = datetime.now(timezone.utc)

    with app.app_context():
        stale = now - timedelta(minutes=5)
        db.session.execute(
            update(NotificationOutbox)
            .where(
                NotificationOutbox.status == "SENDING",
                NotificationOutbox.locked_at < stale,
            )
            .values(status="PENDING", locked_at=None)
        )
        db.session.commit()

        candidate_ids = db.session.scalars(
            select(NotificationOutbox.id)
            .where(
                NotificationOutbox.status == "PENDING",
                NotificationOutbox.next_attempt_at <= now,
            )
            .order_by(NotificationOutbox.next_attempt_at, NotificationOutbox.id)
            .limit(limit)
        ).all()
        claimed_ids = []
        for item_id in candidate_ids:
            claimed = db.session.execute(
                update(NotificationOutbox)
                .where(
                    NotificationOutbox.id == item_id,
                    NotificationOutbox.status == "PENDING",
                    NotificationOutbox.next_attempt_at <= now,
                )
                .values(status="SENDING", locked_at=now)
            ).rowcount
            if claimed:
                db.session.commit()
                claimed_ids.append(item_id)

        for item_id in claimed_ids:
            item = db.session.get(NotificationOutbox, item_id)
            if item is None:
                continue
            monitor = db.session.get(Monitor, item.monitor_id)

            if monitor is None:
                item.status = "FAILED"
                item.locked_at = None
                item.last_error = "Monitor no longer exists"
                db.session.commit()
                continue

            existing = db.session.scalar(
                select(NotificationDelivery.id)
                .where(
                    NotificationDelivery.monitor_id == item.monitor_id,
                    NotificationDelivery.snapshot_id == item.snapshot_id,
                    NotificationDelivery.channel == item.channel,
                )
                .limit(1)
            )

            if existing:
                item.status = "SENT"
                item.locked_at = None
                db.session.commit()
                continue

            try:
                _send(item, monitor.name)
                db.session.add(
                    NotificationDelivery(
                        monitor_id=item.monitor_id,
                        snapshot_id=item.snapshot_id,
                        channel=item.channel,
                        delivered_at=datetime.now(timezone.utc),
                    )
                )
                item.status = "SENT"
                item.locked_at = None
                item.last_error = ""
                db.session.commit()
                delivered += 1
            except Exception as exc:
                db.session.rollback()
                item = db.session.get(NotificationOutbox, item_id)
                if item is None:
                    continue

                item.attempts += 1
                item.last_error = str(exc)[:1000]
                item.locked_at = None
                if item.attempts >= 10:
                    item.status = "FAILED"
                else:
                    item.status = "PENDING"
                    item.next_attempt_at = datetime.now(timezone.utc) + timedelta(
                        seconds=min(60 * (2 ** (item.attempts - 1)), 21600)
                    )
                db.session.commit()
                log.exception(
                    "%s notification attempt failed for monitor %s",
                    item.channel,
                    item.monitor_id,
                )

    return delivered


def notify_change(app, monitor, previous, current):
    changed = changed_text(previous.content_text, current.content_text)
    keywords = [
        word.strip().casefold()
        for word in (monitor.keywords or "").split(",")
        if word.strip()
    ]
    if keywords and not any(word in changed.casefold() for word in keywords):
        return 0

    base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    diff_path = f"/monitors/{monitor.id}/diff/{previous.id}/{current.id}"
    diff_url = f"{base_url}{diff_path}" if base_url else diff_path
    summary = (getattr(current, "summary", "") or "").strip()

    message = (
        f"Website change detected: {monitor.name}\n"
        f"{monitor.url}\n"
        f"Version {previous.id} -> {current.id}\n"
        f"Title: {current.title}\n"
    )
    if summary:
        message += f"\nSummary:\n{summary}\n"
    message += f"\nChanged text:\n{changed[:5000]}\n\nDiff: {diff_url}"

    with app.app_context():
        for channel, recipient, payload in _channels(monitor, message):
            exists = db.session.scalar(
                select(NotificationOutbox.id)
                .where(
                    NotificationOutbox.monitor_id == monitor.id,
                    NotificationOutbox.snapshot_id == current.id,
                    NotificationOutbox.channel == channel,
                )
                .limit(1)
            )
            sent = db.session.scalar(
                select(NotificationDelivery.id)
                .where(
                    NotificationDelivery.monitor_id == monitor.id,
                    NotificationDelivery.snapshot_id == current.id,
                    NotificationDelivery.channel == channel,
                )
                .limit(1)
            )
            if exists or sent:
                continue

            db.session.add(
                NotificationOutbox(
                    monitor_id=monitor.id,
                    snapshot_id=current.id,
                    channel=channel,
                    recipient=recipient,
                    payload=payload,
                )
            )
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            log.info(
                "Notification outbox already populated for monitor %s snapshot %s",
                monitor.id,
                current.id,
            )

    return dispatch_pending_notifications(app)
