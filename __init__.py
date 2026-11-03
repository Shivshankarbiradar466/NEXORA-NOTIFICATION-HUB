
import os

from flask import Flask, request
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from config import Config
from .celery_app import celery


# Extensions
db = SQLAlchemy()
migrate = Migrate()
csrf = CSRFProtect()

# Flask-Limiter configuration
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["200 per hour"],
)


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    secret_key = str(app.config.get("SECRET_KEY") or "")
    if len(secret_key) < 32 or "generate-a-random" in secret_key.lower() or "replace-with-a-random" in secret_key.lower() or secret_key == "dev-only-change-me":
        raise RuntimeError("Set a strong random FLASK_SECRET_KEY of at least 32 characters in .env before starting NEXORA.")
    database_url = str(app.config.get("SQLALCHEMY_DATABASE_URI") or "")
    if not database_url or "CHANGE_ME" in database_url:
        raise RuntimeError("Set DATABASE_URL with real database credentials in .env before starting NEXORA.")
    if os.getenv("APP_ENV", "development").lower() == "production" and not app.config.get("SESSION_COOKIE_SECURE"):
        raise RuntimeError("Production deployments must set SESSION_COOKIE_SECURE=true.")
    if app.config.get("SESSION_COOKIE_SECURE") is False and os.getenv("NEXORA_ALLOW_INSECURE_COOKIES", "true").lower() != "true":
        raise RuntimeError("HTTPS deployments must set SESSION_COOKIE_SECURE=true.")

    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    limiter.init_app(app)

    # Register blueprints
    from .auth import auth_bp
    from .main import main_bp
    from .oauth import oauth_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(oauth_bp)

    # Runtime schema alignment for new columns
    with app.app_context():
        try:
            from sqlalchemy import text
            with db.engine.connect() as conn:
                try:
                    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS auto_reply_enabled BOOLEAN DEFAULT FALSE"))
                    conn.commit()
                except Exception:
                    pass
                try:
                    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS auto_reply_delay_hours INTEGER DEFAULT 6"))
                    conn.commit()
                except Exception:
                    pass
                try:
                    conn.execute(text("ALTER TABLE email_messages ADD COLUMN IF NOT EXISTS auto_reply_sent_at TIMESTAMP WITH TIME ZONE"))
                    conn.commit()
                except Exception:
                    pass
                try:
                    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS login_otp_hash VARCHAR(128)"))
                    conn.commit()
                except Exception:
                    pass
                try:
                    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS login_otp_expires_at TIMESTAMP WITH TIME ZONE"))
                    conn.commit()
                except Exception:
                    pass
                try:
                    conn.execute(text("ALTER TABLE email_messages ALTER COLUMN recipient TYPE TEXT"))
                    conn.commit()
                except Exception:
                    pass
                try:
                    conn.execute(text("ALTER TABLE email_messages ALTER COLUMN sender TYPE TEXT"))
                    conn.commit()
                except Exception:
                    pass
                try:
                    conn.execute(text("ALTER TABLE email_messages ALTER COLUMN subject TYPE TEXT"))
                    conn.commit()
                except Exception:
                    pass
        except Exception:
            pass

    @app.route("/favicon.ico")
    def favicon():
        return app.send_static_file("img/favicon.ico")

    # Configure Celery
    redis_url = app.config["REDIS_URL"]

    celery.conf.update(
        broker_url=redis_url,
        result_backend=redis_url,
        task_ignore_result=False,
        timezone="Asia/Kolkata",
        enable_utc=True,
        beat_schedule={
            "monitor-enabled-users": {
                "task": "app.tasks.monitor_enabled_users",
                "schedule": (
                    app.config["MONITOR_INTERVAL_MINUTES"] * 60
                ),
            },
            "send-due-task-reminders": {
                "task": "app.tasks.send_due_task_reminders",
                "schedule": 1800.0,
            },
            "sweep-auto-ai-replies": {
                "task": "app.tasks.sweep_auto_ai_replies",
                "schedule": 120.0,
            }
        },
    )

    # Security headers
    @app.after_request
    def security_headers(response):
        response.headers.setdefault(
            "X-Content-Type-Options", "nosniff"
        )
        response.headers.setdefault(
            "X-Frame-Options", "SAMEORIGIN"
        )
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=()",
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'self'",
        )
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        if request.is_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    @app.errorhandler(429)
    def rate_limit_exceeded(error):
        app.logger.warning("rate_limit_exceeded ip=%s path=%s", request.remote_addr, request.path)
        return "Too many requests. Please wait before trying again.", 429

    @app.errorhandler(500)
    def internal_server_error(error):
        app.logger.error("Unhandled application error", exc_info=(type(error), error, error.__traceback__))
        from flask import jsonify, request
        if request.path.startswith("/api/"):
            return jsonify({"error": "An unexpected error occurred."}), 500
        return "An unexpected error occurred. Please try again later.", 500

    # Template context
    @app.context_processor
    def inject_globals():
        from .auth import current_user

        return {
            "app_name": "NEXORA",
            "current_user": current_user,
        }

    # Start continuous background auto-monitoring thread for Gmail
    try:
        from .services.continuous_monitor import start_continuous_monitor
        start_continuous_monitor(app)
    except Exception as exc:
        app.logger.warning("Could not launch continuous monitor thread: %s", exc)

    return app


# Import models so Flask-Migrate can discover them
from . import models  # noqa: E402, F401

