from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Index, UniqueConstraint

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc)


def localtime(value, timezone_name="Asia/Kolkata"):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo(timezone_name))


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255))
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    monitors = db.relationship("Monitor", back_populates="user", cascade="all, delete-orphan")


class Monitor(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False)
    url = db.Column(db.Text, nullable=False)
    canonical_url = db.Column(db.Text, nullable=False, index=True)
    interval_minutes = db.Column(db.Integer, nullable=False, default=60)
    ignore_selectors = db.Column(db.Text, nullable=False, default="")
    watch_selectors = db.Column(db.Text, nullable=False, default="")
    sensitivity = db.Column(db.String(16), nullable=False, default="normal")
    keywords = db.Column(db.Text, nullable=False, default="")
    alert_email = db.Column(db.String(255), nullable=False, default="")
    daily_digest = db.Column(db.Boolean, nullable=False, default=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    status = db.Column(db.String(16), nullable=False, default="PENDING")
    status_message = db.Column(db.String(500), nullable=False, default="")
    last_checked_at = db.Column(db.DateTime(timezone=True))
    last_success_at = db.Column(db.DateTime(timezone=True))
    last_changed_at = db.Column(db.DateTime(timezone=True))
    user = db.relationship("User", back_populates="monitors")
    snapshots = db.relationship("Snapshot", back_populates="monitor", cascade="all, delete-orphan", order_by="Snapshot.created_at.desc()")
    __table_args__ = (Index("ix_monitor_url_enabled", "canonical_url", "enabled"),)


class Snapshot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    monitor_id = db.Column(db.Integer, db.ForeignKey("monitor.id", ondelete="CASCADE"), nullable=False)
    content_hash = db.Column(db.String(64), nullable=False)
    content_text = db.Column(db.Text, nullable=False)
    title = db.Column(db.String(500), nullable=False, default="")
    screenshot_path = db.Column(db.Text)
    http_status = db.Column(db.Integer, nullable=False, default=200)
    summary = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    monitor = db.relationship("Monitor", back_populates="snapshots")
    metrics = db.relationship("SnapshotMetric", back_populates="snapshot", cascade="all, delete-orphan")
    __table_args__ = (Index("ix_snapshot_history", "monitor_id", "created_at", "id"),)


class SnapshotMetric(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    snapshot_id = db.Column(db.Integer, db.ForeignKey("snapshot.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False)
    value = db.Column(db.Float, nullable=False)
    unit = db.Column(db.String(24), nullable=False, default="")
    context = db.Column(db.String(500), nullable=False, default="")
    snapshot = db.relationship("Snapshot", back_populates="metrics")
    __table_args__ = (Index("ix_snapshot_metric_name", "name", "snapshot_id"),)


class NotificationDelivery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    monitor_id = db.Column(db.Integer, db.ForeignKey("monitor.id", ondelete="CASCADE"), nullable=False)
    snapshot_id = db.Column(db.Integer, db.ForeignKey("snapshot.id", ondelete="CASCADE"), nullable=False)
    channel = db.Column(db.String(32), nullable=False)
    delivered_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("monitor_id", "snapshot_id", "channel", name="uq_notification_delivery"),)


class NotificationOutbox(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    monitor_id = db.Column(db.Integer, db.ForeignKey("monitor.id", ondelete="CASCADE"), nullable=False, index=True)
    snapshot_id = db.Column(db.Integer, db.ForeignKey("snapshot.id", ondelete="CASCADE"), nullable=False, index=True)
    channel = db.Column(db.String(32), nullable=False)
    recipient = db.Column(db.String(500), nullable=False, default="")
    payload = db.Column(db.Text, nullable=False, default="")
    status = db.Column(db.String(16), nullable=False, default="PENDING", index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    next_attempt_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    locked_at = db.Column(db.DateTime(timezone=True))
    last_error = db.Column(db.String(1000), nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("monitor_id", "snapshot_id", "channel", name="uq_notification_outbox"),)


class DigestDelivery(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    monitor_id = db.Column(db.Integer, db.ForeignKey("monitor.id", ondelete="CASCADE"), nullable=False)
    digest_date = db.Column(db.String(10), nullable=False)
    recipient = db.Column(db.String(255), nullable=False)
    delivered_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("monitor_id", "digest_date", "recipient", name="uq_digest_delivery"),)


class CheckQueue(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    canonical_url = db.Column(db.Text, nullable=False, unique=True)
    status = db.Column(db.String(16), nullable=False, default="PENDING", index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    due_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    locked_at = db.Column(db.DateTime(timezone=True))
    last_error = db.Column(db.String(1000), nullable=False, default="")
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
