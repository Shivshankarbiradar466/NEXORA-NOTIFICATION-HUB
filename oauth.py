import os
import logging
import secrets
from datetime import datetime, timedelta, timezone
from flask import Blueprint, redirect, url_for, session, flash, current_app, request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from . import db
from .models import User, OAuthToken, AuditLog
from .security import encrypt_value
from .auth import _send_login_otp_email, hash_token

logger = logging.getLogger(__name__)
oauth_bp = Blueprint("oauth", __name__, url_prefix="/oauth")

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]
OAUTH_STATE_KEY = "google_oauth_state"
OAUTH_VERIFIER_KEY = "google_oauth_code_verifier"

def _client_config():
    client_id = current_app.config.get("GOOGLE_CLIENT_ID") or os.getenv("GOOGLE_CLIENT_ID")
    client_secret = current_app.config.get("GOOGLE_CLIENT_SECRET") or os.getenv("GOOGLE_CLIENT_SECRET")
    redirect_uri = current_app.config.get("GOOGLE_REDIRECT_URI") or os.getenv("GOOGLE_REDIRECT_URI")
    if not client_id or not client_secret:
        raise RuntimeError("Google OAuth credentials are missing from .env.")
    if not redirect_uri:
        redirect_uri = url_for("oauth.google_callback", _external=True)
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }, redirect_uri

def _create_flow():
    client_config, redirect_uri = _client_config()
    flow = Flow.from_client_config(client_config, scopes=SCOPES)
    flow.redirect_uri = redirect_uri
    return flow

def _clear_oauth_session():
    for key in (OAUTH_STATE_KEY, OAUTH_VERIFIER_KEY):
        session.pop(key, None)

@oauth_bp.get("/google/login")
def google_login():
    try:
        if not session.get("user_id"):
            # Google is the identity provider; local accounts may still be used separately.
            session["google_login_intent"] = True

        flow = _create_flow()
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            # Requests fresh Google authentication on each app login attempt.
            # Google may use password, passkey, 2-step verification, or another method.
            prompt="login select_account consent",
        )
        verifier = getattr(flow, "code_verifier", None)
        if not verifier:
            raise RuntimeError("Google OAuth PKCE verifier was not generated.")
        session[OAUTH_STATE_KEY] = state
        session[OAUTH_VERIFIER_KEY] = verifier
        return redirect(authorization_url)
    except Exception as exc:
        _clear_oauth_session()
        logger.exception("Unable to start Google OAuth")
        flash("Google sign-in could not be started. Check the server logs and try again.", "danger")
        return redirect(url_for("auth.login"))

@oauth_bp.get("/google/callback")
def google_callback():
    try:
        saved_state = session.get(OAUTH_STATE_KEY)
        returned_state = request.args.get("state")
        if not saved_state or saved_state != returned_state:
            _clear_oauth_session()
            flash("Google OAuth security validation failed. Please try again.", "danger")
            return redirect(url_for("auth.login"))

        if request.args.get("error"):
            err = request.args.get("error_description") or request.args.get("error")
            _clear_oauth_session()
            flash("Google authorization was not completed. Please try again.", "danger")
            return redirect(url_for("auth.login"))

        code = request.args.get("code")
        verifier = session.get(OAUTH_VERIFIER_KEY)
        if not code or not verifier:
            raise RuntimeError("Google authorization response is incomplete.")

        flow = _create_flow()
        flow.code_verifier = verifier
        flow.fetch_token(authorization_response=request.url, code_verifier=verifier)
        credentials = flow.credentials
        if not credentials or not credentials.token:
            raise RuntimeError("Google did not return a valid access token.")

        info = build("oauth2", "v2", credentials=credentials, cache_discovery=False).userinfo().get().execute()
        google_email = (info.get("email") or "").strip().lower()
        google_sub = (info.get("id") or "").strip()
        if not google_email or not google_sub or info.get("verified_email") is not True:
            raise RuntimeError("Google did not return a verified account identity.")

        user = None
        current_id = session.get("user_id")
        if current_id:
            user = db.session.get(User, current_id)
            if not user:
                session.clear()
        if not user:
            user = User.query.filter(
                (User.google_sub == google_sub) | (User.email == google_email)
            ).first()

        if not user:
            username = google_email.split("@")[0][:80]
            base = username or "google-user"
            n = 1
            while User.query.filter_by(username=username).first():
                suffix = str(n)
                username = (base[:80-len(suffix)] + suffix)
                n += 1
            user = User(username=username, email=google_email, role="employee")
            db.session.add(user)
            db.session.flush()

        duplicate = User.query.filter(
            User.google_sub == google_sub, User.id != user.id
        ).first() if user.id else None
        if duplicate:
            raise RuntimeError("This Google account is already linked to another account.")

        user.google_sub = google_sub
        user.email = google_email
        user.is_verified = True

        token_row = OAuthToken.query.filter_by(user_id=user.id).first()
        if not token_row:
            token_row = OAuthToken(user_id=user.id, access_token="")
            db.session.add(token_row)
        if credentials.token:
            token_row.access_token = encrypt_value(credentials.token)
        if credentials.refresh_token:
            token_row.refresh_token = encrypt_value(credentials.refresh_token)
        if credentials.expiry:
            expiry = credentials.expiry
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            token_row.expires_at = expiry
        token_row.scope = " ".join(credentials.scopes or SCOPES)
        token_row.email = google_email

        db.session.commit()

        is_login = not current_id or session.get("google_login_intent")
        if is_login:
            otp = f"{secrets.randbelow(900000) + 100000}"
            expiry_seconds = current_app.config.get("LOGIN_OTP_EXPIRY_SECONDS", 120)
            now_utc = datetime.now(timezone.utc)
            expires_at = now_utc + timedelta(seconds=expiry_seconds)
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
                flash(f"Google account verified! A 2-minute OTP code has been dispatched to your Gmail ({user.email}). [Dev OTP: {otp}]", "info")
            else:
                flash(f"Google account verified! A 6-digit OTP code has been dispatched to your Gmail ({user.email}). Valid for 2 minutes.", "info")
            return redirect(url_for("auth.login_otp_verify"))

        session.clear()
        session.permanent = True
        if user.mfa_enabled:
            session["pending_mfa_user_id"] = user.id
            session.permanent = True
            flash("Google connected. Complete your second-factor check to continue.", "info")
            return redirect(url_for("auth.mfa_challenge"))
        ip = (request.remote_addr or "")[:64]
        agent = (request.user_agent.string or "")[:300]
        if user.last_login_ip and (user.last_login_ip != ip or user.last_login_user_agent != agent):
            db.session.add(AuditLog(user_id=user.id, action="login_new_device_or_location", details=f"ip={ip}; user_agent={agent}"))
            logger.warning("login_new_device_or_location user_id=%s ip=%s user_agent=%s", user.id, ip, agent)
        user.last_login_ip = ip
        user.last_login_user_agent = agent
        db.session.add(AuditLog(user_id=user.id, action="google_login_success", details=f"ip={ip}; user_agent={agent}"))
        db.session.commit()
        session["user_id"] = user.id
        session["session_version"] = user.session_version
        flash("Google sign-in successful. Gmail automation is connected.", "success")
        return redirect(url_for("main.dashboard"))
    except Exception as exc:
        db.session.rollback()
        _clear_oauth_session()
        logger.exception("Google OAuth callback failed")
        flash("Google connection failed. Check the server logs and try again.", "danger")
        return redirect(url_for("auth.login"))

@oauth_bp.post("/google/disconnect")
def google_disconnect():
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("auth.login"))
    user = db.session.get(User, user_id)
    if not user:
        session.clear()
        return redirect(url_for("auth.login"))
    try:
        token = OAuthToken.query.filter_by(user_id=user.id).first()
        if token:
            db.session.delete(token)
        user.google_sub = None
        user.monitoring_enabled = False
        db.session.commit()
        flash("Google and Gmail automation disconnected.", "info")
    except Exception:
        db.session.rollback()
        flash("Unable to disconnect Google. Please try again.", "danger")
    return redirect(url_for("main.dashboard"))
