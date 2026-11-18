"""Payment state machine. 'Paid' is only ever set here, from a verified Paystack result."""
from ..extensions import db
from ..models import Payment, Shipment, utcnow
from . import notify, paystack
from .audit import audit


def start_payment(shipment, callback_url):
    """Create a Payment row, initialise with Paystack, and return the authorization URL."""
    ref = paystack.new_reference()
    minor = paystack.to_minor(shipment.amount)
    data = paystack.initialize(
        email=shipment.customer.email, amount_minor=minor, currency=shipment.currency, reference=ref,
        callback_url=callback_url,
        metadata={"shipment_id": shipment.id, "tracking_number": shipment.tracking_number,
                  "purpose": "shipping_fee_sender"})
    pay = Payment(shipment_id=shipment.id, reference=ref, amount_minor=minor, currency=shipment.currency)
    db.session.add(pay)
    audit("payment_initiated", "shipment", shipment.id, f"reference={ref} amount_minor={minor} {shipment.currency}")
    db.session.commit()
    return data["authorization_url"]


def apply_result(payment, data, via):
    """Apply a Paystack transaction payload (from verify API or signed webhook) to a Payment.
    Returns (ok: bool, message: str). Idempotent."""
    shipment = payment.shipment
    if payment.status == "success":
        return True, "Payment already confirmed."
    gw_status = (data or {}).get("status")
    payment.gateway_response = str((data or {}).get("gateway_response", ""))[:255]
    if gw_status != "success":
        payment.status = "failed" if gw_status in ("failed", "reversed") else "abandoned" if gw_status == "abandoned" else payment.status
        audit("payment_not_successful", "shipment", shipment.id, f"reference={payment.reference} gateway_status={gw_status}",
              actor_label=f"paystack-{via}")
        db.session.commit()
        return False, "Payment was not completed."
    if (data.get("reference") != payment.reference or int(data.get("amount", -1)) != payment.amount_minor
            or str(data.get("currency", "")).upper() != payment.currency):
        audit("payment_mismatch", "shipment", shipment.id,
              f"reference={payment.reference} expected={payment.amount_minor} {payment.currency} "
              f"got={data.get('amount')} {data.get('currency')}", actor_label=f"paystack-{via}")
        db.session.commit()
        return False, "Payment details did not match this shipment; it has not been marked paid."
    payment.status = "success"
    payment.channel = data.get("channel")
    payment.verified_via = via
    payment.paid_at = utcnow()
    if shipment.payment_status == "Unpaid":
        shipment.payment_status = "Paid"
        audit("payment_verified", "shipment", shipment.id, f"reference={payment.reference} via={via}",
              actor_label=f"paystack-{via}")
        db.session.commit()
        notify.notify_payment(shipment)
        db.session.commit()
    else:
        audit("payment_received_unexpected_state", "shipment", shipment.id,
              f"reference={payment.reference} shipment payment_status={shipment.payment_status}; needs staff review",
              actor_label=f"paystack-{via}")
        db.session.commit()
    return True, "Payment confirmed."


def staff_reverify(shipment, reference, actor):
    """Staff re-check of a Paystack reference. Marks Paid only if Paystack confirms it for THIS shipment."""
    data = paystack.verify(reference)
    meta = data.get("metadata") or {}
    if isinstance(meta, dict) and str(meta.get("tracking_number", "")) != shipment.tracking_number:
        audit("payment_reverify_rejected", "shipment", shipment.id, f"reference={reference} metadata mismatch")
        db.session.commit()
        return False, "That reference belongs to a different transaction."
    payment = Payment.query.filter_by(reference=reference).first()
    if payment and payment.shipment_id != shipment.id:
        return False, "That reference belongs to a different shipment."
    if not payment:
        if not isinstance(meta, dict) or meta.get("tracking_number") != shipment.tracking_number:
            return False, "Reference has no matching shipment metadata."
        payment = Payment(shipment_id=shipment.id, reference=reference, amount_minor=paystack.to_minor(shipment.amount),
                          currency=shipment.currency)
        db.session.add(payment)
        db.session.flush()
    ok, msg = apply_result(payment, data, "staff-reverify")
    audit("payment_reverify", "shipment", shipment.id, f"reference={reference} result={'ok' if ok else 'rejected'}")
    db.session.commit()
    return ok, msg
