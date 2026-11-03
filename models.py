
from datetime import datetime, timezone
from . import db


def utc_now():
    return datetime.now(timezone.utc)


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=True)
    role = db.Column(db.String(20), nullable=False, default="employee", index=True)
    google_sub = db.Column(db.String(255), unique=True, nullable=True)
    whatsapp_number = db.Column(db.String(30), nullable=True)
    monitoring_enabled = db.Column(db.Boolean, nullable=False, default=False, index=True)
    auto_reply_enabled = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false(), index=True)
    auto_reply_delay_hours = db.Column(db.Integer, nullable=False, default=6, server_default="6")
    gmail_initial_sync_complete = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    created_at = db.Column(db.DateTime(timezone=True), default=utc_now)

    # New: stores the generated filename of the user's profile image.
    profile_image = db.Column(db.String(255), nullable=True)
    session_version = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    last_login_ip = db.Column(db.String(64), nullable=True)
    last_login_user_agent = db.Column(db.String(300), nullable=True)
    is_verified = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    failed_login_attempts = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    failed_login_window = db.Column(db.DateTime(timezone=True), nullable=True)
    locked_until = db.Column(db.DateTime(timezone=True), nullable=True)
    mfa_secret = db.Column(db.String(64), nullable=True)
    mfa_enabled = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    mfa_backup_codes = db.Column(db.Text, nullable=True)
    verification_token_hash = db.Column(db.String(64), nullable=True, index=True)
    verification_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    login_otp_hash = db.Column(db.String(128), nullable=True)
    login_otp_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)

    emails = db.relationship(
        "EmailMessage",
        backref="user",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )
    notifications = db.relationship(
        "NotificationLog",
        backref="user",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )


class OAuthToken(db.Model):
    __tablename__ = "oauth_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), unique=True,
        nullable=False, index=True
    )
    email = db.Column(db.String(255), nullable=True, index=True)
    access_token = db.Column(db.Text, nullable=False)
    refresh_token = db.Column(db.Text, nullable=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    scope = db.Column(db.Text, nullable=True)
    updated_at = db.Column(
        db.DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class PasswordReset(db.Model):
    __tablename__ = "password_resets"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    token_hash = db.Column(db.String(128), nullable=False, index=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used = db.Column(db.Boolean, default=False, nullable=False)


class EmailMessage(db.Model):
    __tablename__ = "email_messages"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    message_id = db.Column(db.String(500), nullable=False)
    sender = db.Column(db.Text)
    recipient = db.Column(db.Text)
    subject = db.Column(db.Text)
    body = db.Column(db.Text)
    category = db.Column(db.String(80), index=True)
    priority = db.Column(db.String(30), index=True)
    confidence = db.Column(db.Float)
    confidentiality = db.Column(
        db.String(40), index=True, default="Internal"
    )
    summary = db.Column(db.Text)
    ai_reply = db.Column(db.Text)
    auto_reply_sent_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # Email intelligence fields; defaults keep existing rows compatible.
    is_spam = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false(), index=True)
    spam_score = db.Column(db.Float, nullable=False, default=0.0, server_default="0")
    spam_reason = db.Column(db.Text)
    phishing_score = db.Column(db.Float, nullable=False, default=0.0, server_default="0")
    phishing_reason = db.Column(db.Text)
    user_override = db.Column(db.String(20), nullable=True)
    is_read = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false(), index=True)
    has_attachments = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    gmail_labels = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(30), default="received", index=True)
    received_at = db.Column(
        db.DateTime(timezone=True), default=utc_now, index=True
    )

    __table_args__ = (
        db.UniqueConstraint(
            "user_id", "message_id", name="uq_email_user_message"
        ),
        db.Index("ix_email_user_received", "user_id", "received_at"),
    )
    is_trashed = db.Column(
    db.Boolean,
    nullable=False,
    default=False,
    server_default=db.false(),
    index=True,
)
    attachments = db.relationship("EmailAttachment", backref="email", lazy="select", cascade="all, delete-orphan")

class NotificationLog(db.Model):
    __tablename__ = "notification_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    channel = db.Column(db.String(30), nullable=False)
    destination = db.Column(db.String(255))
    message = db.Column(db.Text)
    status = db.Column(db.String(50))
    provider_id = db.Column(db.String(255))
    created_at = db.Column(
        db.DateTime(timezone=True), default=utc_now, index=True
    )


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True, index=True
    )
    action = db.Column(db.String(255), nullable=False)
    details = db.Column(db.Text)
    created_at = db.Column(
        db.DateTime(timezone=True), default=utc_now, index=True
    )

class BlockedSender(db.Model):
    __tablename__ = "blocked_senders"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    sender_key = db.Column(db.String(320), nullable=False)
    match_type = db.Column(db.String(20), nullable=False, default="email")  # email or domain
    created_at = db.Column(db.DateTime(timezone=True), default=utc_now, nullable=False)
    reason = db.Column(db.String(255), nullable=True)
    __table_args__ = (db.UniqueConstraint("user_id", "sender_key", "match_type", name="uq_blocked_sender_user_key_type"),)


class TrustedSender(db.Model):
    __tablename__ = "trusted_senders"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    sender_key = db.Column(db.String(320), nullable=False)
    match_type = db.Column(db.String(20), nullable=False, default="email")
    created_at = db.Column(db.DateTime(timezone=True), default=utc_now, nullable=False)
    __table_args__ = (db.UniqueConstraint("user_id", "sender_key", "match_type", name="uq_trusted_sender_user_key_type"),)


class EmailAttachment(db.Model):
    __tablename__ = "email_attachments"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    email_id = db.Column(db.Integer, db.ForeignKey("email_messages.id", ondelete="CASCADE"), nullable=False, index=True)
    gmail_message_id = db.Column(db.String(500), nullable=False)
    gmail_attachment_id = db.Column(db.String(500), nullable=True)
    filename = db.Column(db.String(500), nullable=False)
    mime_type = db.Column(db.String(255), nullable=True)
    size_bytes = db.Column(db.BigInteger, nullable=True)
    extracted_text = db.Column(db.Text, nullable=True)
    extraction_status = db.Column(db.String(60), nullable=False, default="pending")
    created_at = db.Column(db.DateTime(timezone=True), default=utc_now, nullable=False)
    __table_args__ = (db.UniqueConstraint("user_id", "gmail_message_id", "filename", name="uq_attachment_user_message_filename"),)


class EmailTask(db.Model):
    __tablename__ = "email_tasks"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    email_id = db.Column(db.Integer, db.ForeignKey("email_messages.id", ondelete="SET NULL"), nullable=True, index=True)
    title = db.Column(db.String(500), nullable=False)
    due_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    status = db.Column(db.String(20), nullable=False, default="open", index=True)
    source_text = db.Column(db.Text)
    reminder_sent_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utc_now, nullable=False)

    email = db.relationship(
        "EmailMessage",
        foreign_keys=[email_id],
        backref=db.backref("tasks", lazy="dynamic"),
    )


