
import os
from dotenv import load_dotenv

# Load environment variables from the project .env file.
load_dotenv()


class Config:
    # --------------------------------------------------
    # Flask security
    # --------------------------------------------------
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "")

    # --------------------------------------------------
    # PostgreSQL database
    # --------------------------------------------------
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "")

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }

    # --------------------------------------------------
    # Redis for Celery background tasks
    # Celery still requires a running Redis server.
    # --------------------------------------------------
    REDIS_URL = os.getenv(
        "REDIS_URL",
        "redis://localhost:6379/0",
    )

    # --------------------------------------------------
    # Flask-Limiter
    # Shared rate-limit counters use Redis so limits work across worker processes.
    # --------------------------------------------------
    RATELIMIT_STORAGE_URI = os.getenv(
        "RATELIMIT_STORAGE_URI",
        os.getenv("REDIS_URL", "redis://localhost:6379/1"),
    )

    # --------------------------------------------------
    # Session and CSRF security
    # --------------------------------------------------
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_NAME = "__Host-nexora_session" if os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true" else "nexora_session"
    SESSION_COOKIE_PATH = "/"
    SESSION_COOKIE_DOMAIN = None
    SESSION_REFRESH_EACH_REQUEST = True
    SESSION_COOKIE_SAMESITE = "Lax"

    SESSION_COOKIE_SECURE = (
        os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
    )

    PERMANENT_SESSION_LIFETIME = 3600
    WTF_CSRF_TIME_LIMIT = 3600
    MAX_CONTENT_LENGTH = 12 * 1024 * 1024
    APP_ENV = os.getenv("APP_ENV", "development")
    SMTP_HOST = os.getenv("SMTP_HOST", "")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM = os.getenv("SMTP_FROM", os.getenv("SMTP_USERNAME", ""))
    APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:5000")
    LOGIN_FAILURE_WINDOW_MINUTES = 1440
    LOGIN_MAX_FAILURES = 5
    LOGIN_LOCK_THRESHOLD = 10
    LOGIN_LOCK_MINUTES = 15
    LOGIN_OTP_EXPIRY_SECONDS = 120  # 2 minutes before expiry

    # --------------------------------------------------
    # Google OAuth
    # --------------------------------------------------
    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")

    GOOGLE_REDIRECT_URI = os.getenv(
        "GOOGLE_REDIRECT_URI",
        "http://127.0.0.1:5000/oauth/google/callback",
    )

    # --------------------------------------------------
    # Twilio WhatsApp
    # --------------------------------------------------
    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")

    TWILIO_WHATSAPP_FROM = os.getenv(
        "TWILIO_WHATSAPP_FROM",
        "whatsapp:+14155238886",
    )

    TWILIO_STATUS_CALLBACK_URL = os.getenv(
        "TWILIO_STATUS_CALLBACK_URL",
        "",
    )

    # --------------------------------------------------
    # Email monitoring schedule
    # --------------------------------------------------
    MONITOR_INTERVAL_MINUTES = max(
        1,
        int(os.getenv("MONITOR_INTERVAL_MINUTES", "5")),
    )

    # --------------------------------------------------
    # Machine-learning model path
    # --------------------------------------------------
    ML_MODEL_PATH = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "app", "ml", "email_classifier.joblib"
    )
    SPAM_MODEL_PATH = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "app", "ml", "spam_classifier.joblib"
    )

