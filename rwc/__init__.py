import logging
import os
import secrets
from datetime import datetime
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, g, jsonify, render_template, request  # noqa: E402
from flask_limiter.errors import RateLimitExceeded  # noqa: E402
from flask_login import current_user  # noqa: E402
from flask_wtf.csrf import CSRFError  # noqa: E402
from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402

from .config import Config, TestingConfig  # noqa: E402
from .extensions import csrf, db, limiter, login_manager, migrate  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def create_app(config_object=None):
    app = Flask(__name__, instance_relative_config=False)
    cfg = config_object or (TestingConfig if os.getenv("APP_ENV") == "testing" else Config)
    app.config.from_object(cfg)

    if not app.config["SECRET_KEY"]:
        if app.config["APP_ENV"] == "production":
            raise RuntimeError("SECRET_KEY must be set in production (python -c 'import secrets;print(secrets.token_hex(32))').")
        app.config["SECRET_KEY"] = secrets.token_hex(32)  # ephemeral dev key: sessions reset on restart
        logging.getLogger("rwc").warning("SECRET_KEY not set - using an ephemeral development key.")
    if (app.config["APP_ENV"] == "production" and app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite")
            and os.getenv("ALLOW_SQLITE_IN_PRODUCTION") != "1"):
        raise RuntimeError("Production needs a persistent database: set DATABASE_URL to PostgreSQL "
                           "(SQLite is not safe on serverless/ephemeral disks).")
    if app.config["APP_ENV"] == "production" and not app.config["SITE_URL"].startswith("https://"):
        logging.getLogger("rwc").warning("SITE_URL is not https:// - set it to your public https URL in production.")

    if app.config["TRUSTED_PROXY_HOPS"]:
        h = app.config["TRUSTED_PROXY_HOPS"]
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=h, x_proto=h, x_host=h)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    db.init_app(app)
    migrate.init_app(app, db, directory=os.path.join(BASE_DIR, "migrations"))
    csrf.init_app(app)
    login_manager.init_app(app)
    limiter.init_app(app)
    limiter.request_filter(lambda: request.endpoint == "static")  # don't rate-limit CSS/JS/fonts

    from . import models  # noqa: F401
    from .blueprints import auth, customer, payments, public, staff
    for bp in (public.bp, auth.bp, customer.bp, payments.bp, staff.bp):
        app.register_blueprint(bp)
    csrf.exempt(payments.webhook)  # authenticated by Paystack HMAC signature instead of a CSRF token

    register_filters(app)
    register_hooks(app)
    register_errors(app)

    from .cli import register_cli
    register_cli(app)
    return app


def register_filters(app):
    @app.template_filter("dt")
    def fmt_dt(v, fmt="%d %b %Y, %H:%M UTC"):
        return v.strftime(fmt) if isinstance(v, datetime) else (v.strftime("%d %b %Y") if v else "-")

    @app.template_filter("money")
    def fmt_money(v, currency=None):
        currency = currency or app.config["CURRENCY"]
        try:
            return f"{currency} {Decimal(v):,.2f}"
        except Exception:
            return "-"

    @app.context_processor
    def inject():
        from .content import FAQ
        from .services import paystack
        from .services.tracking import ALL_STATUSES
        from .tariff import country_name
        c = app.config
        return {
            "company": {k.replace("COMPANY_", "").lower(): c[k] for k in
                        ("COMPANY_NAME", "COMPANY_TAGLINE", "COMPANY_EMAIL",
                         "COMPANY_HOURS", "COMPANY_REG_NO")},
            "site_url": c["SITE_URL"], "currency": c["CURRENCY"], "year": datetime.utcnow().year,
            "paystack_ready": paystack.configured(), "all_statuses": ALL_STATUSES, "country_name": country_name,
            "csp_nonce": getattr(g, "csp_nonce", ""), "faq_items": FAQ,
            "faq_jsonld": {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
                {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in FAQ]},
        }


def register_hooks(app):
    @app.before_request
    def _nonce():
        g.csp_nonce = secrets.token_urlsafe(16)

    @app.after_request
    def security_headers(resp):
        h = resp.headers
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "strict-origin-when-cross-origin"
        h["Permissions-Policy"] = "geolocation=(), microphone=(), camera=(), payment=()"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; font-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; form-action 'self' https://checkout.paystack.com; "
            "frame-ancestors 'none'; base-uri 'self'; object-src 'none'")
        if app.config["APP_ENV"] == "production":
            h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.endpoint and not request.endpoint.startswith("static") and (
                current_user.is_authenticated or request.path.startswith(("/track", "/pay", "/auth"))):
            h["Cache-Control"] = "no-store"
            h["Pragma"] = "no-cache"
        return resp


def register_errors(app):
    def wants_json():
        return request.path.startswith("/api/") or request.path.startswith("/webhooks/")

    def handle(code, title, message):
        def _h(e):
            if wants_json():
                return jsonify(error=title), code
            return render_template("error.html", code=code, title=title, message=message), code
        return _h

    app.register_error_handler(404, handle(404, "Page not found", "We couldn't find the page you were looking for. It may have moved, or the link may be mistyped."))
    app.register_error_handler(403, handle(403, "Access denied", "You don't have permission to view this page."))
    app.register_error_handler(413, handle(413, "Request too large", "The submitted data was too large."))

    @app.errorhandler(429)
    @app.errorhandler(RateLimitExceeded)
    def too_many(e):
        if wants_json():
            return jsonify(error="Too many requests. Please slow down."), 429
        return render_template("error.html", code=429, title="Too many requests",
                               message="You've made too many requests in a short time. Please wait a minute and try again."), 429

    @app.errorhandler(CSRFError)
    def csrf_error(e):
        if wants_json():
            return jsonify(error="Security token missing or expired. Reload the page and try again."), 400
        return render_template("error.html", code=400, title="Session expired",
                               message="Your form's security token was missing or has expired. Go back, refresh the page and try again."), 400

    @app.errorhandler(500)
    def server_error(e):
        db.session.rollback()
        if wants_json():
            return jsonify(error="Server error"), 500
        return render_template("error.html", code=500, title="Something went wrong",
                               message="An unexpected error occurred on our side. It has been logged. Please try again shortly."), 500
