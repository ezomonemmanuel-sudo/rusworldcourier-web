import csv
import io
from datetime import datetime, timedelta
from functools import wraps

from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template, request, url_for)
from flask_login import current_user, login_required
from sqlalchemy import func

from ..extensions import db, limiter
from ..forms import (AssignForm, PaymentAdjustForm, ReplyForm, ReverifyForm, StaffForm, StatusForm)
from ..models import (AuditLog, Notification, Shipment, Ticket, TicketMessage, TrackingEvent, User, utcnow)
from ..services import notify, paystack, payments
from ..services.audit import audit
from ..services.tracking import (ALL_STATUSES, GUARD_MESSAGE, POST_PICKUP, RIDER_STATUSES, TERMINAL, violates_guard)
from ..tariff import COUNTRIES, SERVICES, country_name

bp = Blueprint("staff", __name__, url_prefix="/staff")


def roles_required(*roles):
    def deco(fn):
        @wraps(fn)
        @login_required
        def wrapper(*a, **kw):
            if current_user.role not in roles:
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


ops = roles_required("dispatcher", "admin")
admin_only = roles_required("admin")


# ---------------------------------------------------------------- shared status logic
def apply_status(s, status, location, details, received_by, actor):
    """Validate + record a status change. Returns (ok, message)."""
    cfg = current_app.config
    if status not in ALL_STATUSES:
        return False, "Unknown status."
    if violates_guard(location, details, received_by):
        audit("status_text_blocked", "shipment", s.id, f"status={status}")
        db.session.commit()
        return False, GUARD_MESSAGE
    if s.status in TERMINAL and not actor.is_admin:
        return False, f"This shipment is already '{s.status}'. Only an administrator can change it."
    if status in POST_PICKUP and not s.is_paid and cfg["REQUIRE_PAYMENT_BEFORE_PICKUP"]:
        return False, "The sender's shipping fee has not been paid (verified), so the parcel can't progress past pickup scheduling."
    if status == "Delivered" and not (received_by or "").strip():
        return False, "Enter who received the parcel to mark it Delivered."
    old = s.status
    s.status = status
    if status == "Delivered":
        s.delivered_at = utcnow()
        s.received_by = received_by.strip()
    ev = TrackingEvent(status=status, location=(location or "").strip() or None, details=(details or "").strip() or None,
                       actor_id=actor.id)
    s.events.append(ev)
    audit("status_changed", "shipment", s.id, f"{old} -> {status}", actor=actor)
    db.session.commit()
    notify.notify_status(s, ev)
    db.session.commit()
    msg = "Status updated."
    if status == "Cancelled" and s.is_paid:
        msg += " This shipment was paid: process the refund in Paystack, then record it under Payment."
    return True, msg


# ---------------------------------------------------------------- shipment queries
def filtered_shipments():
    a = request.args
    q = Shipment.query
    term = a.get("q", "").strip()
    if term:
        like = f"%{term}%"
        q = q.filter(db.or_(Shipment.tracking_number.ilike(like), Shipment.sender_name.ilike(like),
                            Shipment.recipient_name.ilike(like), Shipment.destination_city.ilike(like),
                            Shipment.recipient_phone.ilike(like)))
    if a.get("status") in ALL_STATUSES:
        q = q.filter(Shipment.status == a["status"])
    if a.get("payment") in ("Unpaid", "Paid", "Refunded"):
        q = q.filter(Shipment.payment_status == a["payment"])
    if a.get("service") in SERVICES:
        q = q.filter(Shipment.service_type == a["service"])
    if a.get("country") and a["country"] in dict(COUNTRIES):
        q = q.filter(Shipment.destination_country == a["country"])
    rider = a.get("rider", "")
    if rider == "none":
        q = q.filter(Shipment.rider_id.is_(None))
    elif rider.isdigit():
        q = q.filter(Shipment.rider_id == int(rider))
    for key, op in (("date_from", ">="), ("date_to", "<")):
        v = a.get(key)
        if v:
            try:
                d = datetime.strptime(v, "%Y-%m-%d")
            except ValueError:
                continue
            q = q.filter(Shipment.created_at >= d) if op == ">=" else q.filter(Shipment.created_at < d + timedelta(days=1))
    return q


def riders():
    return User.query.filter_by(role="rider", active=True).order_by(User.name).all()


@bp.get("/")
@ops
def dashboard():
    page = max(request.args.get("page", 1, type=int), 1)
    pagination = filtered_shipments().order_by(Shipment.created_at.desc()).paginate(page=page, per_page=20, error_out=False)
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    S = Shipment
    stats = {
        "total": S.query.count(),
        "today": S.query.filter(S.created_at >= today).count(),
        "active": S.query.filter(S.status.notin_(list(TERMINAL))).count(),
        "delivered": S.query.filter_by(status="Delivered").count(),
        "unassigned": S.query.filter(S.rider_id.is_(None), S.status.in_(["Booking received", "Pickup scheduled"]),
                                     S.payment_status == "Paid").count(),
        "unpaid": S.query.filter_by(payment_status="Unpaid").filter(S.status != "Cancelled").count(),
        "open_tickets": Ticket.query.filter(Ticket.status != "closed").count(),
    }
    revenue = db.session.query(S.currency, func.coalesce(func.sum(S.amount), 0)).filter(S.payment_status == "Paid").group_by(S.currency).all()
    by_status = dict(db.session.query(S.status, func.count(S.id)).group_by(S.status).all())
    days = []
    for i in range(6, -1, -1):
        d0 = today - timedelta(days=i)
        n = S.query.filter(S.created_at >= d0, S.created_at < d0 + timedelta(days=1)).count()
        days.append((d0.strftime("%a"), n))
    return render_template("staff/dashboard.html", pagination=pagination, stats=stats, revenue=revenue, by_status=by_status,
                           days=days, max_day=max([n for _, n in days] + [1]), riders=riders(), countries=COUNTRIES,
                           services=SERVICES, args={k: v for k, v in request.args.items() if k != 'page'})


def _safe_cell(v):
    s = "" if v is None else str(v)
    return "'" + s if s and s[0] in "=+-@\t\r" else s  # neutralise spreadsheet formula injection


@bp.get("/export.csv")
@ops
@limiter.limit("20 per hour")
def export_csv():
    rows = filtered_shipments().order_by(Shipment.created_at.desc()).limit(50000).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["tracking_number", "created_utc", "status", "payment_status", "service", "sender", "sender_phone",
                "pickup_city", "recipient", "recipient_phone", "destination_city", "destination_country", "zone",
                "weight_kg", "chargeable_kg", "declared_value", "insured", "amount", "currency", "rider"])
    for s in rows:
        w.writerow([_safe_cell(x) for x in (
            s.tracking_number, s.created_at.strftime("%Y-%m-%d %H:%M"), s.status, s.payment_status, s.service_type,
            s.sender_name, s.sender_phone, s.origin_city, s.recipient_name, s.recipient_phone, s.destination_city,
            country_name(s.destination_country), s.zone, s.weight_kg, s.chargeable_weight_kg, s.declared_value,
            "yes" if s.insured else "no", s.amount, s.currency, s.rider.name if s.rider else "")])
    audit("csv_export", "shipment", None, f"rows={len(rows)} filters={dict(request.args)}")
    db.session.commit()
    fname = f"rusworldcourier-shipments-{utcnow():%Y%m%d-%H%M}.csv"
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={fname}"})


# ---------------------------------------------------------------- shipment detail (ops)
@bp.get("/shipments/<int:shipment_id>")
@ops
def shipment(shipment_id):
    s = db.session.get(Shipment, shipment_id) or abort(404)
    status_form = StatusForm(status=s.status)
    assign_form = AssignForm(rider_id=s.rider_id or 0)
    assign_form.rider_id.choices = [(0, "Unassigned")] + [(r.id, r.name) for r in riders()]
    return render_template("staff/shipment.html", s=s, events=list(reversed(s.events)), status_form=status_form,
                           assign_form=assign_form, adjust_form=PaymentAdjustForm(), reverify_form=ReverifyForm(),
                           paystack_ready=paystack.configured())


@bp.post("/shipments/<int:shipment_id>/status")
@ops
def update_status(shipment_id):
    s = db.session.get(Shipment, shipment_id) or abort(404)
    f = StatusForm()
    if f.validate_on_submit():
        ok, msg = apply_status(s, f.status.data, f.location.data, f.details.data, f.received_by.data, current_user)
        flash(msg, "success" if ok else "error")
    else:
        flash("Invalid status update: " + "; ".join(e for errs in f.errors.values() for e in errs), "error")
    return redirect(url_for("staff.shipment", shipment_id=s.id))


@bp.post("/shipments/<int:shipment_id>/assign")
@ops
def assign(shipment_id):
    s = db.session.get(Shipment, shipment_id) or abort(404)
    f = AssignForm()
    f.rider_id.choices = [(0, "Unassigned")] + [(r.id, r.name) for r in riders()]
    if f.validate_on_submit():
        old = s.rider.name if s.rider else "none"
        s.rider_id = f.rider_id.data or None
        db.session.flush()
        new = db.session.get(User, s.rider_id).name if s.rider_id else "none"
        audit("rider_assigned", "shipment", s.id, f"{old} -> {new}")
        db.session.commit()
        flash(f"Rider set to {new}.", "success")
    else:
        flash("Choose a valid rider.", "error")
    return redirect(request.referrer if request.referrer and request.referrer.startswith(request.host_url) else url_for("staff.shipment", shipment_id=s.id))


@bp.post("/shipments/<int:shipment_id>/payment/adjust")
@ops
def adjust_payment(shipment_id):
    """Manual adjustment. 'Paid' is NOT selectable and is rejected here even for forged requests."""
    s = db.session.get(Shipment, shipment_id) or abort(404)
    f = PaymentAdjustForm()
    if request.form.get("new_status") == "Paid":
        audit("payment_manual_paid_attempt_blocked", "shipment", s.id, f"reason={request.form.get('reason', '')[:200]}")
        db.session.commit()
        flash("Payment can't be set to Paid manually. Use 'Verify with Paystack reference' instead.", "error")
    elif f.validate_on_submit():
        new = f.new_status.data
        if new == "Refunded" and not s.is_paid:
            flash("Only a Paid shipment can be recorded as Refunded.", "error")
        elif new == s.payment_status:
            flash("Payment status is already " + new + ".", "info")
        else:
            old = s.payment_status
            s.payment_status = new
            audit("payment_manual_adjustment", "shipment", s.id, f"{old} -> {new}; reason: {f.reason.data.strip()}")
            db.session.commit()
            flash("Adjustment recorded in the audit log.", "success")
    else:
        flash("A reason of at least 10 characters is required.", "error")
    return redirect(url_for("staff.shipment", shipment_id=s.id))


@bp.post("/shipments/<int:shipment_id>/payment/reverify")
@ops
@limiter.limit("30 per hour")
def reverify_payment(shipment_id):
    s = db.session.get(Shipment, shipment_id) or abort(404)
    f = ReverifyForm()
    if f.validate_on_submit():
        try:
            ok, msg = payments.staff_reverify(s, f.reference.data.strip(), current_user)
        except paystack.PaystackError as e:
            ok, msg = False, str(e)
        flash(msg, "success" if ok else "error")
    else:
        flash("Enter the Paystack reference.", "error")
    return redirect(url_for("staff.shipment", shipment_id=s.id))


# ---------------------------------------------------------------- rider
@bp.get("/rider")
@roles_required("rider", "dispatcher", "admin")
def rider_home():
    q = Shipment.query.filter_by(rider_id=current_user.id)
    active = q.filter(Shipment.status.notin_(list(TERMINAL))).order_by(Shipment.created_at).all()
    done = q.filter(Shipment.status.in_(list(TERMINAL))).order_by(Shipment.updated_at.desc()).limit(10).all()
    return render_template("staff/rider.html", active=active, done=done, rider_statuses=RIDER_STATUSES)


@bp.route("/rider/<int:shipment_id>", methods=["GET", "POST"])
@roles_required("rider", "dispatcher", "admin")
def rider_shipment(shipment_id):
    s = db.session.get(Shipment, shipment_id) or abort(404)
    if s.rider_id != current_user.id:
        abort(404)
    f = StatusForm(status=s.status)
    f.status.choices = [(x, x) for x in RIDER_STATUSES]
    if f.validate_on_submit():
        if f.status.data not in RIDER_STATUSES:
            abort(400)
        ok, msg = apply_status(s, f.status.data, f.location.data, f.details.data, f.received_by.data, current_user)
        flash(msg, "success" if ok else "error")
        return redirect(url_for("staff.rider_shipment", shipment_id=s.id))
    return render_template("staff/rider_shipment.html", s=s, form=f, events=list(reversed(s.events)))


# ---------------------------------------------------------------- tickets
@bp.get("/tickets")
@ops
def tickets():
    st = request.args.get("status", "")
    q = Ticket.query
    if st in ("open", "pending", "closed"):
        q = q.filter_by(status=st)
    page = max(request.args.get("page", 1, type=int), 1)
    pag = q.order_by(Ticket.updated_at.desc()).paginate(page=page, per_page=25, error_out=False)
    return render_template("staff/tickets.html", pagination=pag, st=st)


@bp.route("/tickets/<ref>", methods=["GET", "POST"])
@ops
def ticket(ref):
    t = Ticket.query.filter_by(reference=ref).first_or_404()
    form = ReplyForm()
    if form.validate_on_submit():
        body = form.body.data.strip()
        if violates_guard(body):
            flash(GUARD_MESSAGE, "error")
        else:
            t.messages.append(TicketMessage(author_id=current_user.id, is_staff=True, body=body))
            t.status = "closed" if form.close.data else "pending"
            t.updated_at = utcnow()
            audit("ticket_reply_staff", "ticket", t.reference)
            n = notify.send_email(t.email, f"Re: [{t.reference}] {t.subject}",
                                  f"{body}\n\n-- {current_app.config['COMPANY_NAME']} support\nTicket {t.reference}", "ticket_reply")
            db.session.commit()
            flash("Reply saved. " + {"sent": "Email notification sent.", "failed": "Email notification FAILED - see Outbox.",
                                     "not_sent": "Email notification NOT sent (SMTP not configured); the customer will see it in their dashboard."}[n.status],
                  "success" if n.status == "sent" else "info")
            return redirect(url_for("staff.ticket", ref=t.reference))
    return render_template("staff/ticket.html", t=t, form=form)


@bp.post("/tickets/<ref>/close")
@ops
def close_ticket(ref):
    t = Ticket.query.filter_by(reference=ref).first_or_404()
    t.status = "closed"
    audit("ticket_closed", "ticket", ref)
    db.session.commit()
    return redirect(url_for("staff.tickets"))


# ---------------------------------------------------------------- admin: staff, audit, outbox
@bp.route("/users", methods=["GET", "POST"])
@admin_only
def users():
    form = StaffForm()
    if form.validate_on_submit():
        if User.query.filter_by(email=form.email.data).first():
            form.email.errors.append("Email already in use.")
        else:
            u = User(name=form.name.data.strip(), email=form.email.data, phone=(form.phone.data or "").strip() or None,
                     role=form.role.data)
            u.set_password(form.password.data)
            db.session.add(u)
            db.session.flush()
            audit("staff_created", "user", u.id, f"role={u.role} email={u.email}")
            db.session.commit()
            flash(f"{u.role.title()} account created. Share the temporary password securely and ask them to change it.", "success")
            return redirect(url_for("staff.users"))
    role = request.args.get("role", "")
    q = User.query
    if role in ("customer", "rider", "dispatcher", "admin"):
        q = q.filter_by(role=role)
    else:
        q = q.filter(User.role != "customer")
    page = max(request.args.get("page", 1, type=int), 1)
    pag = q.order_by(User.created_at.desc()).paginate(page=page, per_page=30, error_out=False)
    return render_template("staff/users.html", form=form, pagination=pag, role=role)


@bp.post("/users/<int:user_id>/toggle")
@admin_only
def toggle_user(user_id):
    u = db.session.get(User, user_id) or abort(404)
    if u.id == current_user.id:
        flash("You can't deactivate your own account.", "error")
    else:
        u.active = not u.active
        audit("user_activated" if u.active else "user_deactivated", "user", u.id, u.email)
        db.session.commit()
        flash(f"{u.email} is now {'active' if u.active else 'deactivated'}.", "success")
    return redirect(request.referrer if request.referrer and request.referrer.startswith(request.host_url) else url_for("staff.users"))


@bp.post("/users/<int:user_id>/role")
@admin_only
def change_role(user_id):
    u = db.session.get(User, user_id) or abort(404)
    new = request.form.get("role")
    if u.id == current_user.id:
        flash("You can't change your own role.", "error")
    elif new not in ("rider", "dispatcher", "admin") or u.role == "customer":
        flash("Invalid role change.", "error")
    else:
        audit("role_changed", "user", u.id, f"{u.role} -> {new}")
        u.role = new
        db.session.commit()
        flash("Role updated.", "success")
    return redirect(url_for("staff.users"))


@bp.get("/audit")
@admin_only
def audit_log():
    page = max(request.args.get("page", 1, type=int), 1)
    q = AuditLog.query
    action = request.args.get("action", "").strip()
    if action:
        q = q.filter(AuditLog.action.ilike(f"%{action}%"))
    term = request.args.get("q", "").strip()
    if term:
        q = q.filter(db.or_(AuditLog.actor_label.ilike(f"%{term}%"), AuditLog.detail.ilike(f"%{term}%"),
                            AuditLog.entity_id.ilike(f"%{term}%")))
    pag = q.order_by(AuditLog.id.desc()).paginate(page=page, per_page=50, error_out=False)
    return render_template("staff/audit.html", pagination=pag, action=action, term=term)


@bp.get("/outbox")
@admin_only
def outbox():
    page = max(request.args.get("page", 1, type=int), 1)
    pag = Notification.query.order_by(Notification.id.desc()).paginate(page=page, per_page=50, error_out=False)
    return render_template("staff/outbox.html", pagination=pag, email_ready=notify.email_configured(),
                           sms_ready=notify.sms_configured())
