import io
import secrets
from datetime import timedelta

import qrcode
import qrcode.image.svg
from flask import (Blueprint, abort, current_app, flash, redirect, render_template, request, url_for)
from flask_login import current_user, login_required

from ..extensions import db, limiter
from ..forms import BookingForm, ChangePasswordForm, ProfileForm, ReplyForm, TicketForm
from ..models import Shipment, Ticket, TicketMessage, TrackingEvent, utcnow
from ..services import notify
from ..services.audit import audit
from ..services.tracking import TERMINAL, generate_tracking_number, normalize_tracking
from ..tariff import ORIGIN_COUNTRY, calculate_quote, country_name, zone_for

bp = Blueprint("customer", __name__)


def customer_only(fn):
    from functools import wraps

    @wraps(fn)
    @login_required
    def wrapper(*a, **kw):
        if current_user.role != "customer":
            return redirect(url_for("staff.dashboard") if current_user.is_ops else url_for("staff.rider_home"))
        return fn(*a, **kw)
    return wrapper


def own_shipment_or_404(shipment_id, allow_ops=False):
    s = db.session.get(Shipment, shipment_id)
    if not s:
        abort(404)
    if s.customer_id == current_user.id or (allow_ops and current_user.is_ops):
        return s
    abort(404)  # 404 not 403: don't reveal that the record exists


def qr_svg(text):
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=1)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    return svg[svg.find("<svg"):]


@bp.get("/dashboard")
@customer_only
def dashboard():
    page = max(request.args.get("page", 1, type=int), 1)
    q = Shipment.query.filter_by(customer_id=current_user.id)
    term = request.args.get("q", "").strip()
    if term:
        like = f"%{term}%"
        q = q.filter(db.or_(Shipment.tracking_number.ilike(like), Shipment.recipient_name.ilike(like),
                            Shipment.destination_city.ilike(like)))
    pagination = q.order_by(Shipment.created_at.desc()).paginate(page=page, per_page=10, error_out=False)
    base = Shipment.query.filter_by(customer_id=current_user.id)
    stats = {
        "total": base.count(),
        "active": base.filter(Shipment.status.notin_(list(TERMINAL))).count(),
        "delivered": base.filter_by(status="Delivered").count(),
        "unpaid": base.filter_by(payment_status="Unpaid").filter(Shipment.status != "Cancelled").count(),
    }
    tickets = Ticket.query.filter_by(user_id=current_user.id, status="open").count()
    return render_template("customer/dashboard.html", pagination=pagination, stats=stats, term=term, open_tickets=tickets)


@bp.route("/book", methods=["GET", "POST"])
@customer_only
@limiter.limit("30 per hour", methods=["POST"])
def book():
    form = BookingForm()
    if request.method == "GET":
        form.sender_name.data = current_user.name
        form.sender_phone.data = current_user.phone
        for f in ("country", "weight_kg", "length_cm", "width_cm", "height_cm", "service", "declared_value"):
            if request.args.get(f):
                getattr(form, f).process_formdata([request.args[f]])
        if request.args.get("insure"):
            form.insure.data = True
    if form.validate_on_submit():
        cfg = current_app.config
        if form.pickup_date.data and not (utcnow().date() <= form.pickup_date.data <= utcnow().date() + timedelta(days=30)):
            form.pickup_date.errors.append("Pick a date within the next 30 days.")
        else:
            try:
                q = calculate_quote(form.country.data, form.weight_kg.data, form.length_cm.data, form.width_cm.data,
                                    form.height_cm.data, form.service.data, form.insure.data,
                                    form.declared_value.data or 0, cfg["FUEL_SURCHARGE_PCT"], cfg["CURRENCY"])
            except ValueError as e:
                form.country.errors.append(str(e))
                q = None
            if q:
                s = Shipment(
                    tracking_number=_unique_tracking(), customer_id=current_user.id,
                    sender_name=form.sender_name.data.strip(), sender_phone=form.sender_phone.data.strip(),
                    sender_address=form.sender_address.data.strip(), origin_city=form.origin_city.data.strip(),
                    origin_country=ORIGIN_COUNTRY,
                    recipient_name=form.recipient_name.data.strip(), recipient_phone=form.recipient_phone.data.strip(),
                    recipient_email=(form.recipient_email.data or "").strip().lower() or None,
                    destination_address=form.destination_address.data.strip(),
                    destination_city=form.destination_city.data.strip(),
                    destination_postcode=(form.destination_postcode.data or "").strip() or None,
                    destination_country=form.country.data, zone=q["zone"],
                    parcel_description=form.parcel_description.data.strip(), weight_kg=form.weight_kg.data,
                    length_cm=form.length_cm.data, width_cm=form.width_cm.data, height_cm=form.height_cm.data,
                    chargeable_weight_kg=q["chargeable_weight"], declared_value=q["declared_value"], insured=q["insured"],
                    service_type=q["service"], pickup_date=form.pickup_date.data, notes=(form.notes.data or "").strip() or None,
                    currency=q["currency"], shipping_charge=q["shipping_charge"], fuel_surcharge=q["fuel_surcharge"],
                    insurance_fee=q["insurance_fee"], amount=q["total"], est_days_min=q["days_min"], est_days_max=q["days_max"],
                )
                s.events.append(TrackingEvent(status="Booking received", location=f"{s.origin_city}, {country_name(s.origin_country)}",
                                              details="Shipment booked online.", actor_id=current_user.id))
                db.session.add(s)
                db.session.flush()
                audit("shipment_booked", "shipment", s.id, f"{s.tracking_number} total={s.amount} {s.currency}")
                db.session.commit()
                notify.notify_booking(s)
                db.session.commit()
                flash("Shipment booked. Pay the shipping fee to confirm pickup scheduling.", "success")
                return redirect(url_for("customer.shipment", shipment_id=s.id))
    return render_template("customer/book.html", form=form)


def _unique_tracking():
    for _ in range(10):
        n = generate_tracking_number()
        if not Shipment.query.filter_by(tracking_number=n).first():
            return n
    raise RuntimeError("could not generate tracking number")


@bp.get("/shipments/<int:shipment_id>")
@customer_only
def shipment(shipment_id):
    s = own_shipment_or_404(shipment_id)
    return render_template("customer/shipment.html", s=s, events=list(reversed(s.events)))


@bp.post("/shipments/<int:shipment_id>/cancel")
@customer_only
def cancel(shipment_id):
    s = own_shipment_or_404(shipment_id)
    if s.status in ("Booking received", "Pickup scheduled") and s.payment_status == "Unpaid":
        s.status = "Cancelled"
        s.events.append(TrackingEvent(status="Cancelled", details="Cancelled by sender before payment.", actor_id=current_user.id))
        audit("shipment_cancelled_by_customer", "shipment", s.id)
        db.session.commit()
        flash("Shipment cancelled.", "info")
    else:
        flash("This shipment can no longer be cancelled online. Please open a support ticket.", "error")
    return redirect(url_for("customer.shipment", shipment_id=s.id))


@bp.get("/shipments/<int:shipment_id>/label")
@login_required
def label(shipment_id):
    s = own_shipment_or_404(shipment_id, allow_ops=True)
    if not s.is_paid:
        flash("The shipping label is available once the shipping fee has been paid.", "info")
        return redirect(url_for("customer.shipment", shipment_id=s.id) if current_user.role == "customer" else url_for("staff.shipment", shipment_id=s.id))
    url = f"{current_app.config['SITE_URL']}/track/{s.tracking_number}"
    audit("label_printed", "shipment", s.id)
    db.session.commit()
    return render_template("customer/label.html", s=s, qr=qr_svg(url))


@bp.get("/shipments/<int:shipment_id>/receipt")
@login_required
def receipt(shipment_id):
    s = own_shipment_or_404(shipment_id, allow_ops=True)
    paid = next((p for p in reversed(s.payments) if p.status == "success"), None)
    if not s.is_paid and s.payment_status != "Refunded":
        flash("A receipt is available once the shipping fee has been paid.", "info")
        return redirect(url_for("customer.shipment", shipment_id=s.id) if current_user.role == "customer" else url_for("staff.shipment", shipment_id=s.id))
    return render_template("customer/receipt.html", s=s, payment=paid)


# ---- Support tickets ------------------------------------------------------------------
@bp.route("/support", methods=["GET", "POST"])
@customer_only
@limiter.limit("10 per hour", methods=["POST"])
def support():
    form = TicketForm()
    if form.validate_on_submit():
        tn = normalize_tracking(form.tracking_number.data) if form.tracking_number.data else None
        if form.tracking_number.data and not tn:
            form.tracking_number.errors.append("That doesn't look like a valid RWC tracking number.")
        else:
            t = Ticket(reference="T-" + secrets.token_hex(4).upper(), user_id=current_user.id, name=current_user.name,
                       email=current_user.email, subject=form.subject.data.strip(), category=form.category.data,
                       tracking_number=tn)
            t.messages.append(TicketMessage(author_id=current_user.id, body=form.body.data.strip()))
            db.session.add(t)
            audit("ticket_opened", "ticket", t.reference)
            if current_app.config["COMPANY_EMAIL"]:
                notify.send_email(current_app.config["COMPANY_EMAIL"], f"[{t.reference}] {t.subject}",
                                  f"Ticket from {t.name} <{t.email}>:\n\n{form.body.data.strip()}", "ticket_staff")
            db.session.commit()
            flash(f"Ticket {t.reference} opened. We'll reply in this thread.", "success")
            return redirect(url_for("customer.ticket", ref=t.reference))
    tickets = Ticket.query.filter_by(user_id=current_user.id).order_by(Ticket.updated_at.desc()).all()
    return render_template("customer/support.html", form=form, tickets=tickets)


@bp.route("/support/<ref>", methods=["GET", "POST"])
@customer_only
def ticket(ref):
    t = Ticket.query.filter_by(reference=ref, user_id=current_user.id).first_or_404()
    form = ReplyForm()
    if form.validate_on_submit() and t.status != "closed":
        t.messages.append(TicketMessage(author_id=current_user.id, body=form.body.data.strip()))
        t.status = "open"
        t.updated_at = utcnow()
        audit("ticket_reply_customer", "ticket", t.reference)
        db.session.commit()
        return redirect(url_for("customer.ticket", ref=t.reference))
    return render_template("customer/ticket.html", t=t, form=form)


# ---- Account ------------------------------------------------------------------------------
@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    pform = ProfileForm(obj=current_user) if request.method == "GET" or "p-submit" in request.form else ProfileForm(formdata=None, obj=current_user)
    cform = ChangePasswordForm() if "c-submit" in request.form else ChangePasswordForm(formdata=None)
    if "p-submit" in request.form and pform.validate_on_submit():
        current_user.name = pform.name.data.strip()
        current_user.phone = pform.phone.data.strip()
        current_user.sms_opt_in = pform.sms_opt_in.data
        audit("profile_updated", "user", current_user.id)
        db.session.commit()
        flash("Profile saved.", "success")
        return redirect(url_for("customer.account"))
    if "c-submit" in request.form and cform.validate_on_submit():
        if not current_user.check_password(cform.current.data):
            cform.current.errors.append("Current password is incorrect.")
        else:
            current_user.set_password(cform.password.data)
            audit("password_changed", "user", current_user.id)
            db.session.commit()
            from flask_login import login_user
            login_user(current_user)  # refresh session id (it embeds a password-hash slice)
            flash("Password changed.", "success")
            return redirect(url_for("customer.account"))
    return render_template("customer/account.html", pform=pform, cform=cform)
