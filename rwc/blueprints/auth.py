from urllib.parse import urljoin, urlparse

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from ..extensions import db, limiter
from ..forms import ForgotForm, LoginForm, RegisterForm, ResetForm
from ..models import User, utcnow
from ..services import notify
from ..services.audit import audit

bp = Blueprint("auth", __name__, url_prefix="/auth")
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def home_for(user):
    if user.role == "rider":
        return url_for("staff.rider_home")
    if user.is_ops:
        return url_for("staff.dashboard")
    return url_for("customer.dashboard")


def safe_next(target):
    if not target:
        return None
    host = urlparse(request.host_url)
    test = urlparse(urljoin(request.host_url, target))
    if test.scheme in ("http", "https") and test.netloc == host.netloc and target.startswith("/") and not target.startswith("//"):
        return target
    return None


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="rwc-password-reset")


def make_reset_token(user):
    return _serializer().dumps({"uid": user.id, "h": user.password_hash[-12:]})


def read_reset_token(token):
    try:
        data = _serializer().loads(token, max_age=current_app.config["PASSWORD_RESET_MAX_AGE"])
    except (BadSignature, SignatureExpired):
        return None
    user = db.session.get(User, data.get("uid"))
    if user and user.active and user.password_hash[-12:] == data.get("h"):  # single use: hash changes on reset
        return user
    return None


@bp.route("/register", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(home_for(current_user))
    form = RegisterForm()
    if form.validate_on_submit():
        if User.query.filter_by(email=form.email.data).first():
            form.email.errors.append("An account with this email already exists. Try signing in or resetting your password.")
        else:
            u = User(name=form.name.data.strip(), email=form.email.data, phone=form.phone.data.strip(),
                     role="customer", sms_opt_in=form.sms_opt_in.data)
            u.set_password(form.password.data)
            db.session.add(u)
            db.session.flush()
            audit("register", "user", u.id, actor=u)
            db.session.commit()
            login_user(u)
            flash("Welcome to RusWorld Courier! Your account is ready.", "success")
            return redirect(url_for("customer.dashboard"))
    return render_template("auth/register.html", form=form)


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute;60 per hour", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(home_for(current_user))
    form = LoginForm()
    if form.validate_on_submit():
        cfg = current_app.config
        user = User.query.filter_by(email=form.email.data).first()
        generic = "Incorrect email or password."
        if user and user.is_locked:
            audit("login_blocked_locked", "user", user.id, actor_label=user.email)
            db.session.commit()
            flash("Too many failed attempts. This account is temporarily locked - try again in a few minutes or reset your password.", "error")
        elif user and user.active and user.check_password(form.password.data):
            user.failed_logins = 0
            user.locked_until = None
            user.last_login_at = utcnow()
            session.clear()
            login_user(user)
            audit("login", "user", user.id, actor=user)
            db.session.commit()
            return redirect(safe_next(request.args.get("next")) or home_for(user))
        else:
            if user is None:
                check_password_hash(_DUMMY_HASH, form.password.data)  # equalise timing
            else:
                locked = user.register_failed_login(cfg["MAX_FAILED_LOGINS"], cfg["LOCKOUT_MINUTES"])
                audit("login_locked_out" if locked else "login_failed", "user", user.id, actor_label=user.email)
            if user is None:
                audit("login_failed_unknown", detail=f"email={form.email.data[:80]}", actor_label="anonymous")
            db.session.commit()
            flash(generic, "error")
    return render_template("auth/login.html", form=form)


@bp.post("/logout")
@login_required
def logout():
    audit("logout", "user", current_user.id)
    db.session.commit()
    logout_user()
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("public.home"))


@bp.route("/forgot", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def forgot():
    form = ForgotForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user and user.active:
            link = f"{current_app.config['SITE_URL']}{url_for('auth.reset', token=make_reset_token(user))}"
            n = notify.send_password_reset(user, link)
            audit("password_reset_requested", "user", user.id, f"email_status={n.status}", actor_label=user.email)
            db.session.commit()
        else:
            audit("password_reset_unknown_email", detail=form.email.data[:80], actor_label="anonymous")
            db.session.commit()
        # Same response either way (no account enumeration).
        flash("If an account exists for that email, a reset link has been sent. The link is valid for 1 hour.", "info")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot.html", form=form, email_ready=notify.email_configured())


@bp.route("/reset/<token>", methods=["GET", "POST"])
@limiter.limit("20 per hour", methods=["POST"])
def reset(token):
    user = read_reset_token(token)
    if not user:
        flash("That reset link is invalid, expired or has already been used. Request a new one.", "error")
        return redirect(url_for("auth.forgot"))
    form = ResetForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        user.failed_logins = 0
        user.locked_until = None
        audit("password_reset_completed", "user", user.id, actor=user)
        db.session.commit()
        flash("Password updated. Please sign in.", "success")
        return redirect(url_for("auth.login"))
    return render_template("auth/reset.html", form=form, token=token)
