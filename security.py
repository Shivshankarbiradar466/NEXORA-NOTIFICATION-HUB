import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from cryptography.fernet import Fernet
from flask import current_app

def _fernet():
    digest = hashlib.sha256(current_app.config["SECRET_KEY"].encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))

def encrypt_value(value):
    if value is None:
        return ""
    return _fernet().encrypt(str(value).encode()).decode()

def decrypt_value(value):
    if not value:
        return ""
    return _fernet().decrypt(value.encode()).decode()

def make_reset_token():
    return secrets.token_urlsafe(32)

def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()

def reset_expiry():
    return datetime.now(timezone.utc) + timedelta(minutes=30)


def send_security_email(recipient, subject, body, html_body=None):
    """Send transactional verification/recovery email via configured SMTP."""
    import smtplib
    from email.message import EmailMessage as SMTPMessage
    from flask import current_app
    cfg = current_app.config
    if not all((cfg.get("SMTP_HOST"), cfg.get("SMTP_FROM"))):
        raise RuntimeError("Email delivery is not configured. Set SMTP_HOST and SMTP_FROM.")
    message = SMTPMessage()
    message["Subject"] = subject
    message["From"] = cfg["SMTP_FROM"]
    message["To"] = recipient
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")
    with smtplib.SMTP(cfg["SMTP_HOST"], int(cfg.get("SMTP_PORT", 587)), timeout=15) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        if cfg.get("SMTP_USERNAME"):
            server.login(cfg["SMTP_USERNAME"], cfg.get("SMTP_PASSWORD", ""))
        server.send_message(message)
