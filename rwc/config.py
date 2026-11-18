"""Configuration, read from environment variables (see .env.example)."""
import os


def _bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _db_url():
    url = os.getenv("DATABASE_URL", "sqlite:///rusworldcourier.db")
    if url.startswith("postgres://"):  # Heroku-style URL
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):  # SQLAlchemy 2.1 defaults to psycopg3; we ship psycopg2-binary
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


def _is_sqlite(url):
    return url.startswith("sqlite")


class Config:
    APP_ENV = os.getenv("APP_ENV", "development")  # development | production | testing
    SECRET_KEY = os.getenv("SECRET_KEY", "")
    SQLALCHEMY_DATABASE_URI = _db_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = (
        {"pool_pre_ping": True, "pool_size": 1, "max_overflow": 2, "pool_recycle": 240}  # serverless: keep connections few
        if _db_url().startswith("postgresql") else {"pool_pre_ping": True})

    # Public site / company details
    SITE_URL = os.getenv("SITE_URL", "http://127.0.0.1:5000").rstrip("/")
    COMPANY_NAME = os.getenv("COMPANY_NAME", "RusWorld Courier")
    COMPANY_TAGLINE = "Delivering Beyond Borders"
    COMPANY_EMAIL = os.getenv("COMPANY_EMAIL", "")
    COMPANY_HOURS = os.getenv("COMPANY_HOURS", "Mon-Fri 09:00-18:00 (Moscow time)")
    COMPANY_REG_NO = os.getenv("COMPANY_REG_NO", "")  # registration / tax number shown in footer + policies

    # Pricing
    CURRENCY = os.getenv("CURRENCY", "USD").upper()
    FUEL_SURCHARGE_PCT = float(os.getenv("FUEL_SURCHARGE_PCT", "8"))
    REQUIRE_PAYMENT_BEFORE_PICKUP = _bool("REQUIRE_PAYMENT_BEFORE_PICKUP", True)

    # Paystack (sender shipping fee only)
    PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY", "")
    PAYSTACK_PUBLIC_KEY = os.getenv("PAYSTACK_PUBLIC_KEY", "")
    PAYSTACK_BASE_URL = "https://api.paystack.co"

    # Email (SMTP) and SMS (Twilio) -- optional. Nothing is reported "sent" unless the provider accepted it.
    SMTP_HOST = os.getenv("SMTP_HOST", "")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM = os.getenv("SMTP_FROM", "")
    SMTP_SECURITY = os.getenv("SMTP_SECURITY", "starttls").lower()  # starttls | ssl | none
    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
    TWILIO_FROM = os.getenv("TWILIO_FROM", "")

    # Security
    RATELIMIT_ENABLED = True
    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_HEADERS_ENABLED = True
    RATELIMIT_SWALLOW_ERRORS = True              # if the limiter store (Redis) is down, keep serving...
    RATELIMIT_IN_MEMORY_FALLBACK_ENABLED = True  # ...and fall back to per-process counters
    RATELIMIT_DEFAULT = "300 per hour;60 per minute"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", APP_ENV == "production")
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 12
    WTF_CSRF_TIME_LIMIT = 60 * 60 * 8
    MAX_CONTENT_LENGTH = 1 * 1024 * 1024
    TRUSTED_PROXY_HOPS = int(os.getenv("TRUSTED_PROXY_HOPS", "0"))  # set to 1 behind nginx/Render/Heroku etc.
    MAX_FAILED_LOGINS = 5
    LOCKOUT_MINUTES = 15
    PASSWORD_RESET_MAX_AGE = 60 * 60  # 1 hour
    JSON_SORT_KEYS = False


class TestingConfig(Config):
    APP_ENV = "testing"
    TESTING = True
    SECRET_KEY = "test-secret-key-not-for-production"
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    SQLALCHEMY_ENGINE_OPTIONS = {}
    WTF_CSRF_ENABLED = True
    RATELIMIT_ENABLED = False
    PAYSTACK_SECRET_KEY = "sk_test_unit_tests"
    SMTP_HOST = ""
    SESSION_COOKIE_SECURE = False
    REMEMBER_COOKIE_SECURE = False
    SITE_URL = "http://localhost"
