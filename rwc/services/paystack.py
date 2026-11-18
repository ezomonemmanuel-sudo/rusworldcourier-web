"""Paystack integration -- used ONLY for the shipping fee the sender pays at booking."""
import hashlib
import hmac
import secrets

import requests
from flask import current_app


class PaystackError(Exception):
    pass


def configured():
    return bool(current_app.config["PAYSTACK_SECRET_KEY"])


def to_minor(amount):
    return int((amount * 100).to_integral_value())


def new_reference():
    return "RWCP-" + secrets.token_hex(10).upper()


def _headers():
    return {"Authorization": f"Bearer {current_app.config['PAYSTACK_SECRET_KEY']}", "Content-Type": "application/json"}


def initialize(email, amount_minor, currency, reference, callback_url, metadata):
    if not configured():
        raise PaystackError("Online payment coming soon.")
    try:
        r = requests.post(f"{current_app.config['PAYSTACK_BASE_URL']}/transaction/initialize", headers=_headers(),
                          json={"email": email, "amount": amount_minor, "currency": currency, "reference": reference,
                                "callback_url": callback_url, "metadata": metadata}, timeout=20)
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        raise PaystackError(f"Could not reach Paystack: {exc}")
    if r.status_code != 200 or not data.get("status"):
        raise PaystackError(data.get("message", "Paystack rejected the request."))
    return data["data"]  # authorization_url, access_code, reference


def verify(reference):
    """Server-side verification. Returns Paystack's `data` dict."""
    if not configured():
        raise PaystackError("Online payment coming soon.")
    try:
        r = requests.get(f"{current_app.config['PAYSTACK_BASE_URL']}/transaction/verify/{reference}",
                         headers=_headers(), timeout=20)
        data = r.json()
    except (requests.RequestException, ValueError) as exc:
        raise PaystackError(f"Could not reach Paystack: {exc}")
    if r.status_code != 200 or not data.get("status"):
        raise PaystackError(data.get("message", "Verification failed."))
    return data["data"]


def valid_signature(raw_body: bytes, header_signature: str) -> bool:
    secret = current_app.config["PAYSTACK_SECRET_KEY"]
    if not secret or not header_signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, header_signature.strip().lower())
