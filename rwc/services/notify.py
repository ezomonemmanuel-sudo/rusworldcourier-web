"""Email (SMTP) and SMS (Twilio) hooks.

Honesty rule: a Notification row is only marked "sent" when the provider accepted the message.
Without configuration it is recorded as "not_sent" and logged at WARNING -- never faked.
"""
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

import requests
from flask import current_app

from ..extensions import db
from ..models import Notification

log = logging.getLogger("rwc.notify")


def email_configured(cfg=None):
    cfg = cfg or current_app.config
    return bool(cfg["SMTP_HOST"] and cfg["SMTP_FROM"])


def sms_configured(cfg=None):
    cfg = cfg or current_app.config
    return bool(cfg["TWILIO_ACCOUNT_SID"] and cfg["TWILIO_AUTH_TOKEN"] and cfg["TWILIO_FROM"])


def _record(channel, kind, recipient, subject, body, status, error=None):
    n = Notification(channel=channel, kind=kind, recipient=recipient, subject=subject, body=body,
                     status=status, error=(error or "")[:255] or None)
    db.session.add(n)
    return n


def send_email(to, subject, body, kind="generic", stored_body=None, dev_log_body=None):
    """Send an email. `stored_body` is what is kept in the outbox (use it to redact secrets such as reset links).
    `dev_log_body` is logged in full ONLY when SMTP is not configured and APP_ENV != production."""
    cfg = current_app.config
    stored = stored_body if stored_body is not None else body
    if not email_configured():
        log.warning("EMAIL NOT SENT (SMTP not configured): kind=%s to=%s subject=%r", kind, to, subject)
        if dev_log_body and cfg["APP_ENV"] != "production":
            log.warning("DEV-ONLY email content for %s:\n%s", to, dev_log_body)
        return _record("email", kind, to, subject, stored, "not_sent", "SMTP not configured")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["SMTP_FROM"] if "<" in cfg["SMTP_FROM"] else formataddr((cfg["COMPANY_NAME"], cfg["SMTP_FROM"]))
    msg["To"] = to
    msg.set_content(body)
    try:
        if cfg["SMTP_SECURITY"] == "ssl":
            server = smtplib.SMTP_SSL(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=15, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=15)
            if cfg["SMTP_SECURITY"] == "starttls":
                server.starttls(context=ssl.create_default_context())
        with server:
            if cfg["SMTP_USER"]:
                server.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            server.send_message(msg)
        log.info("email sent kind=%s to=%s", kind, to)
        return _record("email", kind, to, subject, stored, "sent")
    except Exception as exc:  # noqa: BLE001 - we must record any failure honestly
        log.error("EMAIL FAILED kind=%s to=%s error=%s", kind, to, exc)
        return _record("email", kind, to, subject, stored, "failed", str(exc))


def send_sms(to, text, kind="generic"):
    cfg = current_app.config
    if not to:
        return None
    if not sms_configured():
        log.warning("SMS NOT SENT (Twilio not configured): kind=%s to=%s", kind, to)
        return _record("sms", kind, to, None, text, "not_sent", "SMS provider not configured")
    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{cfg['TWILIO_ACCOUNT_SID']}/Messages.json",
            data={"To": to, "From": cfg["TWILIO_FROM"], "Body": text},
            auth=(cfg["TWILIO_ACCOUNT_SID"], cfg["TWILIO_AUTH_TOKEN"]), timeout=10)
        if r.status_code in (200, 201):
            return _record("sms", kind, to, None, text, "sent")
        return _record("sms", kind, to, None, text, "failed", f"HTTP {r.status_code}: {r.text[:150]}")
    except requests.RequestException as exc:
        return _record("sms", kind, to, None, text, "failed", str(exc))


# ---- Domain helpers ---------------------------------------------------------------------
def _track_url(shipment):
    return f"{current_app.config['SITE_URL']}/track/{shipment.tracking_number}"


def notify_booking(shipment):
    c = shipment.customer
    body = (f"Hello {c.name},\n\nYour shipment to {shipment.destination_city} has been booked.\n"
            f"Tracking number: {shipment.tracking_number}\nShipping fee: {shipment.currency} {shipment.amount:.2f} "
            f"(status: {shipment.payment_status})\n\nTrack it any time: {_track_url(shipment)}\n\n"
            f"{current_app.config['COMPANY_NAME']} will never ask you to pay a release fee by bank transfer, crypto or gift card.\n")
    send_email(c.email, f"Booking received - {shipment.tracking_number}", body, "booking")


def notify_payment(shipment):
    c = shipment.customer
    body = (f"Hello {c.name},\n\nWe received your payment of {shipment.currency} {shipment.amount:.2f} for shipment "
            f"{shipment.tracking_number}. Your receipt and label are available in your dashboard.\n")
    send_email(c.email, f"Payment received - {shipment.tracking_number}", body, "payment")


SMS_STATUSES = {"Picked up", "Out for delivery", "Delivered", "Delivery attempted"}
EMAIL_STATUSES = {"Picked up", "Departed origin country", "Arrived in destination country", "Out for delivery",
                  "Delivered", "Delivery attempted", "Delivery exception - contact support", "Returned to sender",
                  "Cancelled"}


def notify_status(shipment, event):
    if event.status not in EMAIL_STATUSES:
        return
    where = f" ({event.location})" if event.location else ""
    body = (f"Shipment {shipment.tracking_number}: {event.status}{where}.\n"
            f"{event.details or ''}\n\nTrack: {_track_url(shipment)}\n")
    recipients = {shipment.customer.email}
    if shipment.recipient_email:
        recipients.add(shipment.recipient_email)
    for r in recipients:
        send_email(r, f"{event.status} - {shipment.tracking_number}", body, "status")
    c = shipment.customer
    if c.sms_opt_in and c.phone and event.status in SMS_STATUSES:
        send_sms(c.phone, f"RusWorld Courier {shipment.tracking_number}: {event.status}. {_track_url(shipment)}", "status")


def send_password_reset(user, link):
    body = (f"Hello {user.name},\n\nUse the link below to set a new password. It is valid for 1 hour and can be used once.\n\n"
            f"{link}\n\nIf you did not request this, ignore this email - your password will not change.\n")
    return send_email(user.email, "Reset your RusWorld Courier password", body, "password_reset",
                      stored_body="[password reset email - link redacted from outbox]", dev_log_body=body)
