from functools import wraps
from datetime import datetime, timezone
import base64
import logging
import json
import secrets
import re
from datetime import timedelta
from email.message import EmailMessage

from flask import Blueprint, render_template, request, redirect, url_for, session, flash, current_app, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from . import db, limiter, csrf
from .models import User, PasswordReset, OAuthToken, AuditLog, NotificationLog
from .security import make_reset_token, hash_token, reset_expiry, encrypt_value, decrypt_value, send_security_email


logger = logging.getLogger(__name__)
auth_bp = Blueprint("auth", __name__)

GMAIL_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

def current_user():
    uid = session.get("user_id")
    user = db.session.get(User, uid) if uid else None
    if not user:
        return None
    if session.get("session_version") != user.session_version:
        session.clear()
        return None
    return user

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return wrapped

def role_required(role):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user or user.role != role:
                flash("This workspace is restricted.", "danger")
                return redirect(url_for("main.dashboard"))
            if role == "admin" and not user.mfa_enabled:
                flash("Enable multi-factor authentication before accessing administrator functions.", "warning")
                return redirect(url_for("main.settings"))
            return view(*args, **kwargs)
        return wrapped
    return decorator

def mask_email(email):
    if not email or "@" not in email:
        return email or ""
    parts = email.split("@", 1)
    name = parts[0]
    domain = parts[1]
    if len(name) <= 2:
        masked_name = name[0] + "*"
    else:
        masked_name = name[0] + ("*" * (len(name) - 2)) + name[-1]
    return f"{masked_name}@{domain}"

def _get_gmail_credentials(user):
    token_row = OAuthToken.query.filter_by(user_id=user.id).first()
    if not token_row or not token_row.access_token:
        raise RuntimeError("Google account is not connected.")
    try:
        access_token = decrypt_value(token_row.access_token)
        refresh_token = decrypt_value(token_row.refresh_token) if token_row.refresh_token else None
    except Exception as exc:
        raise RuntimeError("Stored Google OAuth token could not be decrypted.") from exc

    credentials = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=current_app.config.get("GOOGLE_CLIENT_ID"),
        client_secret=current_app.config.get("GOOGLE_CLIENT_SECRET"),
        scopes=GMAIL_SCOPES,
    )
    if credentials.expired:
        if not credentials.refresh_token:
            raise RuntimeError("Google authorization expired. Connect Google again.")
        try:
            credentials.refresh(Request())
            token_row.access_token = encrypt_value(credentials.token)
            if credentials.expiry:
                expiry = credentials.expiry
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                token_row.expires_at = expiry
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            raise RuntimeError("Google authorization could not be refreshed.") from exc
    return credentials

def send_gmail_message(sender_user, recipient_email, subject, body, html_body=None):
    service = build("gmail", "v1", credentials=_get_gmail_credentials(sender_user), cache_discovery=False)
    message = EmailMessage()
    message["To"] = recipient_email
    message["Subject"] = subject
    message["From"] = sender_user.email
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return service.users().messages().send(userId="me", body={"raw": raw}).execute()

def _send_login_otp_email(user, otp, expires_minutes=2):
    subject = f"NEXORA Security • Login Verification Code: {otp} (Expires in {expires_minutes} min)"
    plain_body = (
        f"Hello {user.username},\n\n"
        f"Your NEXORA single-use login verification code is:\n\n"
        f"    {otp}\n\n"
        f"⏰ TIME LIMIT: This code is valid for EXACTLY {expires_minutes} MINUTES before expiring.\n\n"
        f"Enter this 6-digit code on the sign-in screen to access your workspace.\n\n"
        f"If you did not request this login code, your credentials may have been entered. Please review your account security immediately.\n\n"
        f"— NEXORA Security Team"
    )

    html_body = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #06111f; color: #d6e2f0; margin: 0; padding: 24px; }}
    .card {{ max-width: 520px; margin: 0 auto; background: #0c1829; border: 1px solid #1a3455; border-radius: 16px; padding: 30px; box-shadow: 0 16px 40px rgba(0,0,0,0.5); }}
    .logo {{ font-size: 13px; font-weight: 800; letter-spacing: 0.15em; color: #60a5fa; margin-bottom: 14px; }}
    h2 {{ color: #ffffff; margin: 0 0 10px 0; font-size: 22px; }}
    p {{ color: #94a9c4; font-size: 13px; line-height: 1.6; margin: 0 0 16px 0; }}
    .otp-card {{ background: #08111d; border: 1px solid #1f3b5e; border-radius: 12px; padding: 22px; margin: 20px 0; text-align: center; }}
    .otp-label {{ font-size: 11px; font-weight: 800; letter-spacing: 0.1em; color: #38bdf8; text-transform: uppercase; margin-bottom: 10px; }}
    .otp-badge {{ display: inline-block; font-size: 34px; font-weight: 800; letter-spacing: 0.35em; color: #38bdf8; background: #0e2238; border: 2px solid #2563eb; padding: 10px 24px; border-radius: 10px; font-family: monospace; text-align: center; margin: 8px 0; }}
    .timer-badge {{ display: inline-flex; align-items: center; gap: 6px; background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.4); color: #fca5a5; font-size: 12px; font-weight: 700; padding: 5px 14px; border-radius: 20px; margin-top: 10px; }}
    .security-notice {{ background: #091321; border-left: 3px solid #f59e0b; padding: 12px 14px; font-size: 11px; color: #cbd5e1; border-radius: 0 8px 8px 0; margin-top: 18px; line-height: 1.5; }}
    .footer {{ font-size: 11px; color: #506987; margin-top: 24px; border-top: 1px solid #142336; padding-top: 14px; text-align: center; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">✦ NEXORA SECURITY AUTHENTICATION</div>
    <h2>Login Verification Code</h2>
    <p>Hello <strong>{user.username}</strong>, someone is signing in to your NEXORA workspace ({user.email}).</p>
    
    <div class="otp-card">
      <div class="otp-label">Your 6-Digit One-Time Login Code</div>
      <div>
        <div class="otp-badge">{otp}</div>
      </div>
      <div>
        <span class="timer-badge">⏱️ Valid for {expires_minutes} Minutes Only</span>
      </div>
    </div>

    <div class="security-notice">
      <strong>Strict Expiry:</strong> This OTP is valid for <strong>{expires_minutes} minutes</strong>. If the code is not entered before it expires, you will need to request a new one. If you did not initiate this login request, please secure your account immediately.
    </div>

    <div class="footer">
      NEXORA Intelligent Notification Hub &bull; Automated Security Service<br>
      This confidential verification code was sent to {user.email}.
    </div>
  </div>
</body>
</html>"""

    email_sent = False

    # 1. Try sending via target user's Gmail OAuth credentials if connected
    if OAuthToken.query.filter_by(user_id=user.id).first():
        try:
            send_gmail_message(user, user.email, subject, plain_body, html_body)
            email_sent = True
            logger.info("Login OTP email sent via user %s Gmail API", user.email)
        except Exception as exc:
            logger.warning("Target user's Gmail OAuth failed for login OTP: %s", exc)

    # 2. Try sending via ANY active connected Gmail OAuth in the system
    if not email_sent:
        all_tokens = OAuthToken.query.filter(OAuthToken.access_token.isnot(None)).all()
        for t in all_tokens:
            if t.user_id != user.id:
                sender_u = db.session.get(User, t.user_id)
                if sender_u:
                    try:
                        send_gmail_message(sender_u, user.email, subject, plain_body, html_body)
                        email_sent = True
                        logger.info("Login OTP email sent via system Gmail account (%s) to %s", sender_u.email, user.email)
                        break
                    except Exception as exc:
                        logger.warning("System Gmail account (%s) failed for login OTP: %s", sender_u.email, exc)

    # 3. Fallback to configured SMTP if neither worked
    if not email_sent:
        try:
            send_security_email(user.email, subject, plain_body, html_body)
            email_sent = True
            logger.info("Login OTP email sent via SMTP to %s", user.email)
        except Exception as exc:
            logger.warning("SMTP login OTP sending failed: %s", exc)

    now_utc = datetime.now(timezone.utc)
    exp_utc = now_utc + timedelta(minutes=expires_minutes)
    print("\n" + "=" * 60, flush=True)
    print(f"[NEXORA GMAIL LOGIN OTP] Target User: {user.email}", flush=True)
    print(f"[NEXORA GMAIL LOGIN OTP] 6-Digit OTP:  {otp}", flush=True)
    print(f"[NEXORA GMAIL LOGIN OTP] Time Limit:   {expires_minutes} MINUTES (Expires: {exp_utc.strftime('%H:%M:%S UTC')})", flush=True)
    print(f"[NEXORA GMAIL LOGIN OTP] Sent to Gmail: {email_sent}", flush=True)
    print("=" * 60 + "\n", flush=True)

    return email_sent

@auth_bp.get("/login")
def login():
    if current_user():
        return redirect(url_for("main.dashboard"))
    mode = request.args.get("mode", "otp")
    return render_template("login.html", initial_mode=mode)

@auth_bp.post("/login")
@limiter.limit("5 per 15 minutes")
def login_post():
    identity = (request.form.get("identity") or "").strip()[:255]
    password = request.form.get("password") or ""
    captcha_answer = (request.form.get("captcha_answer") or "").strip()
    challenge = session.get("login_captcha")
    failures = int(session.get("login_failures", 0))
    if failures >= 3:
        expected = session.get("login_captcha_answer")
        if not challenge or not expected or captcha_answer != expected:
            flash("Complete the security check before trying again.", "danger")
            return redirect(url_for("auth.login", mode="password"))
    user = User.query.filter(
        (User.username == identity) | (User.email == identity.lower())
    ).first()
    now = datetime.now(timezone.utc)
    valid_user = bool(user and user.password_hash and check_password_hash(user.password_hash, password))
    if user and user.locked_until:
        lock_until = user.locked_until
        if lock_until.tzinfo is None:
            lock_until = lock_until.replace(tzinfo=timezone.utc)
        if lock_until > now:
            current_app.logger.warning("login_locked identity_hash=%s ip=%s", hash_token(identity.lower())[:12], request.remote_addr)
            flash("Invalid username/email or password. Try again later.", "danger")
            return redirect(url_for("auth.login", mode="password"))
        user.locked_until = None
        user.failed_login_attempts = 0
        user.failed_login_window = now
    if not valid_user or (user and not user.is_verified):
        if user:
            window = user.failed_login_window
            if window and window.tzinfo is None:
                window = window.replace(tzinfo=timezone.utc)
            if not window or now - window > timedelta(minutes=current_app.config["LOGIN_FAILURE_WINDOW_MINUTES"]):
                user.failed_login_window = now
                user.failed_login_attempts = 0
            user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
            if user.failed_login_attempts >= current_app.config["LOGIN_LOCK_THRESHOLD"]:
                user.locked_until = now + timedelta(minutes=current_app.config["LOGIN_LOCK_MINUTES"])
            try:
                db.session.add(AuditLog(user_id=user.id, action="login_failed", details=f"ip={request.remote_addr}; attempts={user.failed_login_attempts}"))
                db.session.commit()
            except Exception:
                db.session.rollback()
        session["login_failures"] = failures + 1
        if session["login_failures"] >= 3:
            current_app.logger.warning("login_challenge_required ip=%s failures=%s", request.remote_addr, session["login_failures"])
            a, b = secrets.randbelow(8) + 2, secrets.randbelow(8) + 2
            session["login_captcha"] = f"{a} + {b}"
            session["login_captcha_answer"] = str(a+b)
        current_app.logger.warning("login_failed identity_hash=%s ip=%s", hash_token(identity.lower())[:12], request.remote_addr)
        flash("Invalid username/email or password. If your account is not verified, check your verification email.", "danger")
        return redirect(url_for("auth.login", mode="password"))
    if user.failed_login_attempts:
        user.failed_login_attempts = 0
        user.failed_login_window = None
        user.locked_until = None
        db.session.commit()

    require_otp = request.form.get("require_otp", "1") == "1"
    if require_otp:
        otp = f"{secrets.randbelow(900000) + 100000}"
        expiry_seconds = current_app.config.get("LOGIN_OTP_EXPIRY_SECONDS", 120)
        expires_at = now + timedelta(seconds=expiry_seconds)
        otp_hash = hash_token(f"login_otp:{user.id}:{otp}")

        user.login_otp_hash = otp_hash
        user.login_otp_expires_at = expires_at
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

        session.clear()
        session["login_otp_user_id"] = user.id
        session["login_otp_hash"] = otp_hash
        session["login_otp_expires_at"] = expires_at.isoformat()
        session["login_otp_attempts"] = 0
        session["login_otp_email"] = user.email

        sent = _send_login_otp_email(user, otp, expires_minutes=2)
        is_dev = (current_app.config.get("APP_ENV", "").lower() in ("development", "dev", "local")
                  or current_app.config.get("SMTP_HOST") in ("smtp.example.com", "", None)
                  or current_app.debug)
        if not sent and is_dev:
            flash(f"Password verified! A 2-minute OTP code has been dispatched to your Gmail ({user.email}). [Dev OTP: {otp}]", "info")
        else:
            flash(f"Password verified! A 6-digit OTP code has been dispatched to your Gmail ({user.email}). Valid for 2 minutes.", "info")
        return redirect(url_for("auth.login_otp_verify"))

    if user.mfa_enabled:
        session.clear()
        session["pending_mfa_user_id"] = user.id
        session.permanent = True
        return redirect(url_for("auth.mfa_challenge"))
    session.clear()
    session.permanent = True
    session["user_id"] = user.id
    session["session_version"] = user.session_version
    ip = (request.remote_addr or "")[:64]
    agent = (request.user_agent.string or "")[:300]
    if user.last_login_ip and (user.last_login_ip != ip or user.last_login_user_agent != agent):
        db.session.add(AuditLog(user_id=user.id, action="login_new_device_or_location", details=f"ip={ip}; user_agent={agent}"))
        current_app.logger.warning("login_new_device_or_location user_id=%s ip=%s user_agent=%s", user.id, ip, agent)
    user.last_login_ip = ip
    user.last_login_user_agent = agent
    db.session.add(AuditLog(user_id=user.id, action="login_success", details=f"ip={ip}; user_agent={agent}"))
    db.session.commit()
    return redirect(url_for("main.dashboard"))

@auth_bp.post("/login/send-otp")
@limiter.limit("10 per 5 minutes")
def send_login_otp():
    identity = (request.form.get("identity") or "").strip()[:255]
    if not identity:
        flash("Enter your registered email address or username.", "danger")
        return redirect(url_for("auth.login", mode="otp"))

    user = User.query.filter(
        (User.username.ilike(identity)) | (User.email.ilike(identity))
    ).first()

    now = datetime.now(timezone.utc)
    if not user:
        is_dev = (current_app.config.get("APP_ENV", "").lower() in ("development", "dev", "local")
                  or current_app.debug)
        if is_dev:
            flash(f"No registered account found matching '{identity}'. Please verify and try again.", "danger")
        else:
            flash("If an account exists with that email or username, a 2-minute verification code has been dispatched to Gmail.", "info")
        return redirect(url_for("auth.login", mode="otp"))

    if user.locked_until:
        lock_until = user.locked_until
        if lock_until.tzinfo is None:
            lock_until = lock_until.replace(tzinfo=timezone.utc)
        if lock_until > now:
            flash("Account temporarily locked. Please try again later.", "danger")
            return redirect(url_for("auth.login", mode="otp"))

    otp = f"{secrets.randbelow(900000) + 100000}"
    expiry_seconds = current_app.config.get("LOGIN_OTP_EXPIRY_SECONDS", 120)
    expires_at = now + timedelta(seconds=expiry_seconds)
    otp_hash = hash_token(f"login_otp:{user.id}:{otp}")

    user.login_otp_hash = otp_hash
    user.login_otp_expires_at = expires_at
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    session.clear()
    session["login_otp_user_id"] = user.id
    session["login_otp_hash"] = otp_hash
    session["login_otp_expires_at"] = expires_at.isoformat()
    session["login_otp_attempts"] = 0
    session["login_otp_email"] = user.email

    sent = _send_login_otp_email(user, otp, expires_minutes=2)
    is_dev = (current_app.config.get("APP_ENV", "").lower() in ("development", "dev", "local")
              or current_app.config.get("SMTP_HOST") in ("smtp.example.com", "", None)
              or current_app.debug)
    if not sent and is_dev:
        flash(f"A 2-minute OTP code has been dispatched to your Gmail ({user.email}). [Dev OTP: {otp}]", "info")
    else:
        flash(f"A 6-digit OTP code has been sent to your Gmail ({user.email}). Valid for 2 minutes.", "info")
    return redirect(url_for("auth.login_otp_verify"))

@auth_bp.route("/login/otp-verify", methods=["GET", "POST"])
@limiter.limit("20 per 5 minutes", methods=["POST"])
def login_otp_verify():
    user_id = session.get("login_otp_user_id")
    if not user_id:
        flash("No active OTP verification session found. Please enter your login details.", "warning")
        return redirect(url_for("auth.login"))

    user = db.session.get(User, user_id)
    if not user:
        session.clear()
        flash("Account not found. Please try again.", "danger")
        return redirect(url_for("auth.login"))

    now = datetime.now(timezone.utc)
    expires_at = user.login_otp_expires_at
    if not expires_at:
        stored_iso = session.get("login_otp_expires_at")
        if stored_iso:
            try:
                expires_at = datetime.fromisoformat(stored_iso)
            except Exception:
                expires_at = None

    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    remaining_seconds = 0
    if expires_at:
        remaining_seconds = max(0, int((expires_at - now).total_seconds()))

    masked = mask_email(user.email)

    if request.method == "POST":
        code = (request.form.get("otp") or "").strip().replace(" ", "")
        attempts = int(session.get("login_otp_attempts", 0)) + 1
        session["login_otp_attempts"] = attempts

        if attempts > 5:
            user.login_otp_hash = None
            user.login_otp_expires_at = None
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            session.clear()
            flash("Too many failed attempts. Please request a new verification code.", "danger")
            return redirect(url_for("auth.login"))

        # Strictly enforce 2-minute expiration
        if not expires_at or now > expires_at or remaining_seconds <= 0:
            user.login_otp_hash = None
            user.login_otp_expires_at = None
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            flash("The verification code has expired (2-minute time limit). Please request a new code.", "danger")
            return render_template(
                "login_otp.html",
                user=user,
                masked_email=masked,
                remaining_seconds=0,
                expired=True
            )

        if not code or len(code) != 6 or not code.isdigit():
            flash("Please enter the complete 6-digit verification code from your Gmail.", "danger")
            return render_template(
                "login_otp.html",
                user=user,
                masked_email=masked,
                remaining_seconds=remaining_seconds,
                expired=False
            )

        expected_hash = user.login_otp_hash or session.get("login_otp_hash")
        entered_hash = hash_token(f"login_otp:{user.id}:{code}")

        if not expected_hash or entered_hash != expected_hash:
            remaining_attempts = max(0, 5 - attempts)
            flash(f"Incorrect verification code. Please check your Gmail and try again. ({remaining_attempts} attempts left)", "danger")
            return render_template(
                "login_otp.html",
                user=user,
                masked_email=masked,
                remaining_seconds=remaining_seconds,
                expired=False
            )

        # VALID OTP AND WITHIN 2 MINUTES!
        user.login_otp_hash = None
        user.login_otp_expires_at = None
        user.failed_login_attempts = 0
        user.failed_login_window = None
        user.locked_until = None

        if user.mfa_enabled:
            session.clear()
            session["pending_mfa_user_id"] = user.id
            session.permanent = True
            db.session.commit()
            return redirect(url_for("auth.mfa_challenge"))

        session.clear()
        session.permanent = True
        session["user_id"] = user.id
        session["session_version"] = user.session_version

        ip = (request.remote_addr or "")[:64]
        agent = (request.user_agent.string or "")[:300]
        if user.last_login_ip and (user.last_login_ip != ip or user.last_login_user_agent != agent):
            db.session.add(AuditLog(user_id=user.id, action="login_new_device_or_location", details=f"ip={ip}; user_agent={agent}"))
            current_app.logger.warning("login_new_device_or_location user_id=%s ip=%s user_agent=%s", user.id, ip, agent)
        user.last_login_ip = ip
        user.last_login_user_agent = agent
        db.session.add(AuditLog(user_id=user.id, action="login_success_via_gmail_otp", details=f"ip={ip}; user_agent={agent}"))
        db.session.commit()

        flash(f"Welcome back, {user.username}! Signed in securely via Gmail OTP.", "success")
        return redirect(url_for("main.dashboard"))

    return render_template(
        "login_otp.html",
        user=user,
        masked_email=masked,
        remaining_seconds=remaining_seconds,
        expired=(remaining_seconds <= 0)
    )

@auth_bp.post("/login/otp-resend")
@limiter.limit("10 per 5 minutes")
def login_otp_resend():
    user_id = session.get("login_otp_user_id")
    user = db.session.get(User, user_id) if user_id else None

    if not user:
        flash("Session expired. Please enter your email to sign in.", "warning")
        return redirect(url_for("auth.login", mode="otp"))

    now = datetime.now(timezone.utc)
    otp = f"{secrets.randbelow(900000) + 100000}"
    expiry_seconds = current_app.config.get("LOGIN_OTP_EXPIRY_SECONDS", 120)
    expires_at = now + timedelta(seconds=expiry_seconds)
    otp_hash = hash_token(f"login_otp:{user.id}:{otp}")

    user.login_otp_hash = otp_hash
    user.login_otp_expires_at = expires_at
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    session["login_otp_user_id"] = user.id
    session["login_otp_hash"] = otp_hash
    session["login_otp_expires_at"] = expires_at.isoformat()
    session["login_otp_attempts"] = 0
    session["login_otp_email"] = user.email

    sent = _send_login_otp_email(user, otp, expires_minutes=2)
    is_dev = (current_app.config.get("APP_ENV", "").lower() in ("development", "dev", "local")
              or current_app.config.get("SMTP_HOST") in ("smtp.example.com", "", None)
              or current_app.debug)
    if not sent and is_dev:
        flash(f"A fresh 2-minute OTP code has been dispatched to your Gmail ({user.email}). [Dev OTP: {otp}]", "info")
    else:
        flash(f"A fresh 6-digit OTP code has been sent to your Gmail ({user.email}). Valid for 2 minutes.", "info")

    return redirect(url_for("auth.login_otp_verify"))


@auth_bp.route("/mfa", methods=["GET", "POST"])
@limiter.limit("10 per 5 minutes", methods=["POST"])
def mfa_challenge():
    import pyotp
    user_id = session.get("pending_mfa_user_id")
    user = db.session.get(User, user_id) if user_id else None
    if not user or not user.mfa_enabled or not user.mfa_secret:
        session.clear()
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        code = (request.form.get("code") or "").strip().replace(" ", "")
        valid = pyotp.TOTP(user.mfa_secret).verify(code, valid_window=1)
        if not valid:
            try:
                backup_codes = json.loads(user.mfa_backup_codes or "[]")
                digest = hash_token(code)
                if digest in backup_codes:
                    backup_codes.remove(digest)
                    user.mfa_backup_codes = json.dumps(backup_codes)
                    valid = True
            except (ValueError, TypeError):
                pass
        if not valid and user.login_otp_hash:
            if hash_token(code) == user.login_otp_hash:
                valid = True
        if not valid:
            current_app.logger.warning("mfa_failed user_id=%s ip=%s", user.id, request.remote_addr)
            flash("Invalid verification code.", "danger")
            return redirect(url_for("auth.mfa_challenge"))
        session.clear()
        session.permanent = True
        session["user_id"] = user.id
        session["session_version"] = user.session_version
        ip = (request.remote_addr or "")[:64]
        agent = (request.user_agent.string or "")[:300]
        if user.last_login_ip and (user.last_login_ip != ip or user.last_login_user_agent != agent):
            db.session.add(AuditLog(user_id=user.id, action="login_new_device_or_location", details=f"ip={ip}; user_agent={agent}"))
            current_app.logger.warning("login_new_device_or_location user_id=%s ip=%s user_agent=%s", user.id, ip, agent)
        user.last_login_ip = ip
        user.last_login_user_agent = agent
        db.session.add(AuditLog(user_id=user.id, action="mfa_login_success", details=f"ip={ip}; user_agent={agent}"))
        db.session.commit()
        return redirect(url_for("main.dashboard"))
    return render_template("mfa.html")


@auth_bp.get("/verify-email/<token>")
def verify_email(token):
    user = User.query.filter_by(verification_token_hash=hash_token(token)).first()
    if not user or not user.verification_expires_at:
        flash("Verification link is invalid or expired.", "danger")
        return redirect(url_for("auth.login"))
    expiry = user.verification_expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry < datetime.now(timezone.utc):
        flash("Verification link is invalid or expired.", "danger")
        return redirect(url_for("auth.login"))
    user.is_verified = True
    user.verification_token_hash = None
    user.verification_expires_at = None
    db.session.add(AuditLog(user_id=user.id, action="email_verified"))
    db.session.commit()
    flash("Email verified. You can now sign in.", "success")
    return redirect(url_for("auth.login"))


@auth_bp.post("/mfa/enable")
@login_required
@limiter.limit("5 per hour")
def mfa_enable():
    import pyotp
    user = current_user()
    secret = pyotp.random_base32()
    backup_codes = [secrets.token_urlsafe(8) for _ in range(8)]
    # Keep pending enrollment in the signed, HttpOnly session until the code is verified.
    session["mfa_setup_secret"] = secret
    session["mfa_setup_backups"] = backup_codes
    session["mfa_setup_backup_hashes"] = [hash_token(code) for code in backup_codes]
    return render_template("mfa_setup.html", secret=secret, provisioning_uri=pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="NEXORA"), backup_codes=backup_codes)


@auth_bp.post("/mfa/confirm")
@login_required
@limiter.limit("10 per 5 minutes")
def mfa_confirm():
    import pyotp
    user = current_user()
    secret = session.get("mfa_setup_secret")
    code = (request.form.get("code") or "").strip()
    if not secret or not pyotp.TOTP(secret).verify(code, valid_window=1):
        flash("The authenticator code is invalid. Try again.", "danger")
        return redirect(url_for("main.settings"))
    user.mfa_secret = secret
    user.mfa_backup_codes = json.dumps(session.get("mfa_setup_backup_hashes", []))
    user.mfa_enabled = True
    db.session.add(AuditLog(user_id=user.id, action="mfa_enabled"))
    db.session.commit()
    session.pop("mfa_setup_secret", None)
    session.pop("mfa_setup_backups", None)
    session.pop("mfa_setup_backup_hashes", None)
    try:
        send_security_email(user.email, "NEXORA MFA enabled", "Multi-factor authentication was enabled for your NEXORA account. If you did not do this, reset your password and contact the administrator.")
    except Exception:
        current_app.logger.warning("Could not send MFA-enabled security alert for user_id=%s", user.id)
    flash("Multi-factor authentication enabled. Store your backup codes securely.", "success")
    return redirect(url_for("main.settings"))


@auth_bp.post("/mfa/disable")
@login_required
@limiter.limit("3 per hour")
def mfa_disable():
    import pyotp
    user = current_user()
    code = (request.form.get("code") or "").strip()
    if not user.mfa_secret or not pyotp.TOTP(user.mfa_secret).verify(code, valid_window=1):
        flash("Enter a valid authenticator code to disable MFA.", "danger")
        return redirect(url_for("main.settings"))
    user.mfa_enabled = False
    user.mfa_secret = None
    user.mfa_backup_codes = None
    db.session.add(AuditLog(user_id=user.id, action="mfa_disabled"))
    db.session.commit()
    try:
        send_security_email(user.email, "NEXORA MFA disabled", "Multi-factor authentication was disabled for your NEXORA account. If you did not do this, reset your password and contact the administrator.")
    except Exception:
        current_app.logger.warning("Could not send MFA-disabled security alert for user_id=%s", user.id)
    flash("Multi-factor authentication disabled.", "warning")
    return redirect(url_for("main.settings"))

@auth_bp.post("/api/whatsapp/try-test")
@csrf.exempt
@limiter.limit("10 per minute")
def try_whatsapp_test():
    data = request.get_json(silent=True) or request.form
    number = (data.get("whatsapp_number") or "").strip()
    if not number:
        return jsonify({"ok": False, "error": "Enter your WhatsApp phone number first."}), 400
    try:
        from .services.whatsapp import normalize_whatsapp_number, send_whatsapp
        clean_num = normalize_whatsapp_number(number)
        res = send_whatsapp(
            clean_num,
            "🎉 NEXORA • WhatsApp Test Alert\n"
            "Your Twilio WhatsApp Sandbox connection is verified and active!"
        )
        if res.get("ok"):
            return jsonify({
                "ok": True,
                "message": f"Test alert sent to {clean_num}! Check your phone.",
                "sid": res.get("sid")
            })
        else:
            err = res.get("error", "Failed to deliver WhatsApp message.")
            return jsonify({
                "ok": False,
                "error": f"{err} (Please make sure you have scanned the QR code and sent 'join refused-sea' first)"
            }), 400
    except ValueError as ve:
        return jsonify({"ok": False, "error": str(ve)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

@auth_bp.route("/register", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        whatsapp_number = request.form.get("whatsapp_number", "").strip()

        if (not re.fullmatch(r"[A-Za-z0-9_.-]{3,80}", username)
                or not re.fullmatch(r"[^@\s]{1,64}@[^@\s.]+(?:\.[^@\s.]+)+", email)
                or len(password) < 12 or password != confirm):
            flash("Enter a valid username/email and matching password of at least 12 characters.", "danger")
            return render_template("register.html", username=username, email=email, whatsapp_number=whatsapp_number)

        clean_wa = None
        if whatsapp_number:
            try:
                from .services.whatsapp import normalize_whatsapp_number
                clean_wa = normalize_whatsapp_number(whatsapp_number)
            except ValueError as ve:
                flash(str(ve), "danger")
                return render_template("register.html", username=username, email=email, whatsapp_number=whatsapp_number)

        if User.query.filter((User.username == username) | (User.email == email)).first():
            flash("An account with that username or email already exists. Please sign in.", "warning")
            return redirect(url_for("auth.login"))

        token = make_reset_token()
        is_dev = (current_app.config.get("APP_ENV", "").lower() in ("development", "dev", "local")
                  or current_app.config.get("SMTP_HOST") in ("smtp.example.com", "", None)
                  or current_app.debug)
        user = User(
            username=username,
            email=email,
            password_hash=generate_password_hash(password),
            role="employee",
            whatsapp_number=clean_wa,
            is_verified=True if is_dev else False,
            verification_token_hash=None if is_dev else hash_token(token),
            verification_expires_at=None if is_dev else (datetime.now(timezone.utc) + timedelta(hours=24)),
        )
        try:
            db.session.add(user)
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash("Unable to create the account. Please try again.", "danger")
            return render_template("register.html", username=username, email=email, whatsapp_number=whatsapp_number)

        # If a WhatsApp number was supplied, dispatch a welcome alert
        if clean_wa:
            try:
                from .services.whatsapp import send_whatsapp
                w_res = send_whatsapp(
                    clean_wa,
                    f"🎉 Welcome to NEXORA, {username}!\n"
                    "Your WhatsApp number is connected to the Twilio Sandbox.\n"
                    "You will receive operational email summaries and alerts here."
                )
                if w_res.get("ok"):
                    db.session.add(NotificationLog(
                        user_id=user.id,
                        channel="WhatsApp",
                        destination=clean_wa,
                        message=f"Welcome to NEXORA, {username}! WhatsApp alerts activated.",
                        status=w_res.get("status", "sent"),
                        provider_id=w_res.get("sid"),
                    ))
                    db.session.commit()
            except Exception as w_exc:
                current_app.logger.warning("Welcome WhatsApp notification could not be sent: %s", w_exc)

        if is_dev:
            flash(f"Account created successfully for {username}! Sign in to enter your workspace.", "success")
        else:
            try:
                verify_url = current_app.config["APP_BASE_URL"].rstrip("/") + url_for("auth.verify_email", token=token)
                send_security_email(user.email, "Verify your NEXORA account", f"Verify your email within 24 hours using this link:\n{verify_url}")
                flash("Account created! Check your email to verify your account before logging in.", "success")
            except Exception:
                current_app.logger.exception("Verification email delivery failed")
                flash("Account created. Please sign in.", "success")

        return redirect(url_for("auth.login"))

    return render_template("register.html")

def _send_password_reset_email(user, otp, reset_link):
    subject = f"NEXORA Security • Password Reset (OTP & Link): {otp}"
    plain_body = (
        f"Hello {user.username},\n\n"
        f"You requested to reset your password for your NEXORA account ({user.email}).\n\n"
        f"You can choose EITHER of the two secure options below to reset your password:\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"OPTION 1: 6-DIGIT OTP VERIFICATION CODE\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Your One-Time Code:   {otp}\n\n"
        f"Enter this 6-digit code on the reset page along with your new password.\n"
        f"This code will expire in 15 minutes.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"OPTION 2: DIRECT ONE-CLICK RESET LINK\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Click the link below to set a new password directly (no code required):\n"
        f"{reset_link}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"If you did not request this password reset, please ignore this email or review your account security.\n\n"
        f"— NEXORA Security Team"
    )

    html_body = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #06111f; color: #d6e2f0; margin: 0; padding: 24px; }}
    .card {{ max-width: 520px; margin: 0 auto; background: #0c1829; border: 1px solid #1a3455; border-radius: 16px; padding: 30px; box-shadow: 0 16px 40px rgba(0,0,0,0.5); }}
    .logo {{ font-size: 14px; font-weight: 800; letter-spacing: 0.15em; color: #60a5fa; margin-bottom: 16px; }}
    h2 {{ color: #ffffff; margin: 0 0 10px 0; font-size: 22px; }}
    p {{ color: #94a9c4; font-size: 13px; line-height: 1.6; margin: 0 0 18px 0; }}
    .option-box {{ background: #08111d; border: 1px solid #182e49; border-radius: 12px; padding: 18px; margin-bottom: 16px; }}
    .option-title {{ font-size: 11px; font-weight: 800; letter-spacing: 0.08em; color: #60a5fa; text-transform: uppercase; margin-bottom: 10px; }}
    .otp-badge {{ display: inline-block; font-size: 28px; font-weight: 800; letter-spacing: 0.3em; color: #38bdf8; background: #0e2238; border: 1px solid #1d456f; padding: 8px 20px; border-radius: 8px; font-family: monospace; text-align: center; margin: 6px 0; }}
    .btn {{ display: inline-block; background: #2563eb; color: #ffffff !important; text-decoration: none; padding: 11px 22px; font-size: 13px; font-weight: 700; border-radius: 8px; margin-top: 6px; }}
    .link-text {{ font-size: 11px; color: #60a5fa; word-break: break-all; margin-top: 8px; }}
    .footer {{ font-size: 11px; color: #506987; margin-top: 20px; border-top: 1px solid #142336; padding-top: 14px; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">✦ NEXORA SECURITY</div>
    <h2>Password Recovery Options</h2>
    <p>Hello <strong>{user.username}</strong>, you requested to reset your password. You have <strong>two options</strong> to complete your reset directly from Gmail:</p>

    <!-- OPTION 1: 6-DIGIT OTP -->
    <div class="option-box">
      <div class="option-title">Option 1: 6-Digit OTP Verification Code</div>
      <p style="margin-bottom:8px; font-size:12px;">Enter this code on the password reset screen:</p>
      <div style="text-align:center;">
        <div class="otp-badge">{otp}</div>
      </div>
      <p style="font-size:11px; color:#6b85a3; margin-top:6px; margin-bottom:0; text-align:center;">Expires in 15 minutes</p>
    </div>

    <!-- OPTION 2: 1-CLICK DIRECT LINK -->
    <div class="option-box">
      <div class="option-title">Option 2: Direct 1-Click Reset Link</div>
      <p style="margin-bottom:10px; font-size:12px;">Prefer not typing the code? Click below to reset directly:</p>
      <div style="text-align:center;">
        <a href="{reset_link}" class="btn">Reset Password Directly ↗</a>
      </div>
      <p class="link-text">Or copy this link:<br><a href="{reset_link}" style="color:#60a5fa;">{reset_link}</a></p>
    </div>

    <div class="footer">
      If you did not request this password reset, please ignore this email.<br>
      — NEXORA Security Automated Dispatch
    </div>
  </div>
</body>
</html>"""

    email_sent = False

    # 1. Try sending via target user's Gmail OAuth credentials if connected
    if OAuthToken.query.filter_by(user_id=user.id).first():
        try:
            send_gmail_message(user, user.email, subject, plain_body, html_body)
            email_sent = True
            logger.info("Password reset email sent via user %s Gmail API", user.email)
        except Exception as exc:
            logger.warning("Target user's Gmail OAuth failed: %s", exc)

    # 2. Try sending via ANY active connected Gmail OAuth in the system
    if not email_sent:
        all_tokens = OAuthToken.query.filter(OAuthToken.access_token.isnot(None)).all()
        for t in all_tokens:
            if t.user_id != user.id:
                sender_u = db.session.get(User, t.user_id)
                if sender_u:
                    try:
                        send_gmail_message(sender_u, user.email, subject, plain_body, html_body)
                        email_sent = True
                        logger.info("Password reset email sent via system Gmail account (%s) to %s", sender_u.email, user.email)
                        break
                    except Exception as exc:
                        logger.warning("System Gmail account (%s) failed: %s", sender_u.email, exc)

    # 3. Fallback to configured SMTP if neither worked
    if not email_sent:
        try:
            send_security_email(user.email, subject, plain_body, html_body)
            email_sent = True
            logger.info("Password reset email sent via SMTP to %s", user.email)
        except Exception as exc:
            logger.warning("SMTP email sending failed: %s", exc)

    return email_sent


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def forgot():
    if request.method == "POST":
        identity = (request.form.get("email") or "").strip().lower()[:254]
        user = User.query.filter(db.or_(User.email.ilike(identity), User.username.ilike(identity))).first() if identity else None

        if user:
            try:
                for old in PasswordReset.query.filter_by(user_id=user.id, used=False).all():
                    old.used = True
                otp = f"{secrets.randbelow(900000) + 100000}"
                token = make_reset_token()

                db.session.add(
                    PasswordReset(
                        user_id=user.id,
                        token_hash=hash_token(f"email_otp:{otp}"),
                        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
                    )
                )
                db.session.add(
                    PasswordReset(
                        user_id=user.id,
                        token_hash=hash_token(token),
                        expires_at=reset_expiry(),
                    )
                )
                db.session.commit()

                link = url_for("auth.reset_password", token=token, _external=True)
                sent = _send_password_reset_email(user, otp, link)

                print("\n" + "=" * 60, flush=True)
                print(f"[NEXORA GMAIL RESET] Password Reset for {user.email}", flush=True)
                print(f"[NEXORA GMAIL RESET] 6-Digit OTP: {otp}", flush=True)
                print(f"[NEXORA GMAIL RESET] Direct Link: {link}", flush=True)
                print(f"[NEXORA GMAIL RESET] Delivered to Gmail: {sent}", flush=True)
                print("=" * 60 + "\n", flush=True)

                session["reset_otp_email"] = user.email
                flash(f"Both a 6-digit OTP code and a direct 1-click reset link have been sent to your Gmail ({user.email}).", "info")
                return redirect(url_for("auth.reset_password_otp"))

            except Exception as exc:
                db.session.rollback()
                logger.exception("Password reset dispatch failed: %s", exc)

        flash("If the account exists, recovery instructions with both OTP and direct link have been sent to your Gmail.", "info")
        return redirect(url_for("auth.reset_password_otp"))

    return render_template("forgot.html")


@auth_bp.route("/reset-password-otp", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def reset_password_otp():
    if request.method == "POST":
        email = (request.form.get("email") or session.get("reset_otp_email") or "").strip().lower()[:254]
        otp = (request.form.get("otp") or "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if not email:
            flash("Please enter your account email.", "danger")
            return render_template("reset_otp.html", email=email)

        if not otp or len(otp) != 6 or not otp.isdigit():
            flash("Please enter the 6-digit verification code sent to your Gmail.", "danger")
            return render_template("reset_otp.html", email=email)

        if len(password) < 12 or password != confirm:
            flash("Passwords must match and be at least 12 characters.", "danger")
            return render_template("reset_otp.html", email=email)

        user = User.query.filter(db.or_(User.email.ilike(email), User.username.ilike(email))).first()
        if not user:
            flash("Invalid or expired verification code.", "danger")
            return render_template("reset_otp.html", email=email)

        token_hash = hash_token(f"email_otp:{otp}")
        row = PasswordReset.query.filter_by(user_id=user.id, token_hash=token_hash, used=False).first()
        if not row:
            flash("Invalid verification code. Please check your Gmail and try again.", "danger")
            return render_template("reset_otp.html", email=email)

        expires = row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            flash("This code has expired. Please request a new one.", "danger")
            return render_template("reset_otp.html", email=email)

        try:
            user.password_hash = generate_password_hash(password)
            user.session_version = (user.session_version or 0) + 1
            # Invalidate all pending resets for this user
            for pr in PasswordReset.query.filter_by(user_id=user.id, used=False).all():
                pr.used = True
            db.session.add(AuditLog(user_id=user.id, action="password_reset_completed_via_email_otp"))
            db.session.commit()

            session.pop("reset_otp_email", None)
            flash("Password reset successfully via OTP. Please sign in with your new password.", "success")
            return redirect(url_for("auth.login"))
        except Exception as exc:
            db.session.rollback()
            logger.exception("Password update failed: %s", exc)
            flash("An error occurred while updating your password. Please try again.", "danger")
            return render_template("reset_otp.html", email=email)

    initial_email = session.get("reset_otp_email") or request.args.get("email", "")
    return render_template("reset_otp.html", email=initial_email)


@auth_bp.post("/resend-otp")
@limiter.limit("10 per hour")
def resend_otp():
    identity = (request.form.get("email") or session.get("reset_otp_email") or "").strip().lower()[:254]
    user = User.query.filter(db.or_(User.email.ilike(identity), User.username.ilike(identity))).first() if identity else None

    if user:
        try:
            for old in PasswordReset.query.filter_by(user_id=user.id, used=False).all():
                old.used = True
            otp = f"{secrets.randbelow(900000) + 100000}"
            token = make_reset_token()

            db.session.add(
                PasswordReset(
                    user_id=user.id,
                    token_hash=hash_token(f"email_otp:{otp}"),
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
                )
            )
            db.session.add(
                PasswordReset(
                    user_id=user.id,
                    token_hash=hash_token(token),
                    expires_at=reset_expiry(),
                )
            )
            db.session.commit()

            link = url_for("auth.reset_password", token=token, _external=True)
            sent = _send_password_reset_email(user, otp, link)

            print("\n" + "=" * 60, flush=True)
            print(f"[NEXORA RESEND GMAIL RESET] User: {user.email}", flush=True)
            print(f"[NEXORA RESEND GMAIL RESET] 6-Digit OTP: {otp}", flush=True)
            print(f"[NEXORA RESEND GMAIL RESET] Direct Link: {link}", flush=True)
            print(f"[NEXORA RESEND GMAIL RESET] Delivered to Gmail: {sent}", flush=True)
            print("=" * 60 + "\n", flush=True)

            session["reset_otp_email"] = user.email
            flash(f"A new 6-digit OTP code and direct reset link have been sent to your Gmail ({user.email}).", "info")
        except Exception as exc:
            db.session.rollback()
            logger.exception("Resending OTP failed: %s", exc)
            flash("Could not send code. Please try again.", "danger")
    else:
        flash("If the account exists, a new code and reset link have been sent to your Gmail.", "info")

    return redirect(url_for("auth.reset_password_otp"))


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def reset_password(token):
    row = PasswordReset.query.filter_by(token_hash=hash_token(token), used=False).first()
    if not row:
        flash("Invalid or expired reset link. Please request a new one.", "danger")
        return redirect(url_for("auth.forgot"))
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        flash("This reset link has expired. Please request a new one.", "danger")
        return redirect(url_for("auth.forgot"))

    user = db.session.get(User, row.user_id)
    if not user:
        flash("Account not found.", "danger")
        return redirect(url_for("auth.forgot"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if len(password) < 12 or password != confirm:
            flash("Passwords must match and be at least 12 characters.", "danger")
            return render_template("reset.html", token=token, email=user.email)
        try:
            user.password_hash = generate_password_hash(password)
            user.session_version = (user.session_version or 0) + 1
            # Invalidate all pending resets for this user
            for pr in PasswordReset.query.filter_by(user_id=user.id, used=False).all():
                pr.used = True
            db.session.add(AuditLog(user_id=user.id, action="password_reset_completed_via_link"))
            db.session.commit()
            session.clear()
            flash("Password reset successfully via secure link. Please sign in with your new password.", "success")
            return redirect(url_for("auth.login"))
        except Exception as exc:
            db.session.rollback()
            logger.exception("Password update via link failed: %s", exc)
            flash("An error occurred while updating your password. Please try again.", "danger")
            return render_template("reset.html", token=token, email=user.email)

    return render_template("reset.html", token=token, email=user.email)

@auth_bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
