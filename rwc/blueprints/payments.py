import json

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, request, url_for
from flask_login import current_user, login_required

from ..extensions import db, limiter
from ..models import Payment, Shipment
from ..services import paystack, payments
from ..services.audit import audit

bp = Blueprint("payments", __name__)


@bp.post("/pay/<int:shipment_id>")
@login_required
@limiter.limit("15 per hour")
def start(shipment_id):
    s = db.session.get(Shipment, shipment_id)
    if not s or s.customer_id != current_user.id:  # only the sender (booking account) can pay
        abort(404)
    back = redirect(url_for("customer.shipment", shipment_id=s.id))
    if s.payment_status != "Unpaid" or s.status == "Cancelled":
        flash("This shipment is not awaiting payment.", "info")
        return back
    if not paystack.configured():
        flash("Online payment coming soon. Your booking is saved; we will confirm the payment step with you.", "info")
        return back
    try:
        url = payments.start_payment(s, callback_url=f"{current_app.config['SITE_URL']}{url_for('payments.callback')}")
    except paystack.PaystackError as e:
        current_app.logger.error("paystack initialize failed: %s", e)
        flash(f"Could not start payment: {e}", "error")
        return back
    return redirect(url)


@bp.get("/pay/callback")
@limiter.limit("30 per hour")
def callback():
    """Customer returns from Paystack. Never trust the redirect: verify server-side."""
    ref = (request.args.get("reference") or request.args.get("trxref") or "").strip()
    pay = Payment.query.filter_by(reference=ref).first() if ref else None
    if not pay:
        flash("We couldn't find that payment.", "error")
        return redirect(url_for("customer.dashboard") if current_user.is_authenticated else url_for("public.home"))
    try:
        data = paystack.verify(ref)
    except paystack.PaystackError as e:
        flash("We couldn't confirm your payment with Paystack yet. If you were charged, it will be confirmed automatically "
              "shortly; otherwise contact support with your tracking number.", "error")
        current_app.logger.error("paystack verify failed for %s: %s", ref, e)
        return redirect(url_for("customer.dashboard") if current_user.is_authenticated else url_for("auth.login"))
    ok, msg = payments.apply_result(pay, data, "callback")
    flash(msg, "success" if ok else "error")
    if current_user.is_authenticated and current_user.id == pay.shipment.customer_id:
        return redirect(url_for("customer.shipment", shipment_id=pay.shipment_id))
    return redirect(url_for("auth.login"))


@bp.post("/webhooks/paystack")
@limiter.limit("600 per minute")
def webhook():
    raw = request.get_data()
    if not paystack.valid_signature(raw, request.headers.get("x-paystack-signature", "")):
        audit("webhook_bad_signature", detail=f"len={len(raw)}", actor_label="paystack-webhook")
        db.session.commit()
        return jsonify(error="invalid signature"), 401
    try:
        event = json.loads(raw)
    except ValueError:
        return jsonify(error="bad json"), 400
    if event.get("event") == "charge.success":
        data = event.get("data") or {}
        pay = Payment.query.filter_by(reference=data.get("reference")).first()
        if pay:
            ok, msg = payments.apply_result(pay, data, "webhook")
            audit("webhook_charge_success", "shipment", pay.shipment_id, f"ok={ok} {msg}", actor_label="paystack-webhook")
        else:
            audit("webhook_unknown_reference", detail=str(data.get("reference"))[:64], actor_label="paystack-webhook")
        db.session.commit()
    return jsonify(status="ok"), 200
