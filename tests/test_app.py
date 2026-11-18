import hashlib
import hmac
import json
import re
from decimal import Decimal

import pytest

from rwc.extensions import db
from rwc.models import AuditLog, Notification, Payment, Shipment, User
from rwc.services import paystack
from rwc.services.tracking import ALL_STATUSES, normalize_tracking, violates_guard
from rwc.tariff import calculate_quote

from .conftest import BOOKING, book, csrf_from, login, make_user, post


# ---------------------------------------------------------------- public pages
@pytest.mark.parametrize("path", ["/", "/services", "/about", "/faq", "/contact", "/quote", "/track", "/auth/login",
                                  "/auth/register", "/auth/forgot", "/robots.txt", "/sitemap.xml", "/favicon.ico",
                                  "/legal/privacy", "/legal/terms", "/legal/prohibited-items", "/legal/claims",
                                  "/legal/shipping-policy"])
def test_public_pages_ok(client, path):
    assert client.get(path).status_code == 200


def test_seo_meta_and_headers(client):
    r = client.get("/")
    html = r.get_data(as_text=True)
    assert '<meta name="description"' in html and 'rel="canonical"' in html and 'og:title' in html
    assert "application/ld+json" in html
    h = r.headers
    assert h["X-Content-Type-Options"] == "nosniff" and h["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in h["Content-Security-Policy"] and "frame-ancestors 'none'" in h["Content-Security-Policy"]
    assert "Referrer-Policy" in h and "Permissions-Policy" in h


def test_404_and_unknown_legal(client):
    r = client.get("/nope")
    assert r.status_code == 404 and "Page not found" in r.get_data(as_text=True)
    assert client.get("/legal/whatever").status_code == 404


def test_testimonials_marked_as_examples(client):
    html = client.get("/").get_data(as_text=True)
    assert "Example content" in html and html.count("tag-example") >= 3


# ---------------------------------------------------------------- quote engine
def test_quote_volumetric_and_levels(app):
    q = calculate_quote("DE", 1, 50, 40, 30, "standard")  # vol = 12 kg
    assert q["volumetric_weight"] == 12.0 and q["chargeable_weight"] == 12.0
    std = calculate_quote("DE", 2, service="standard")
    exp = calculate_quote("DE", 2, service="express")
    eco = calculate_quote("DE", 2, service="economy")
    assert exp["total"] > std["total"] > eco["total"]
    far = calculate_quote("BR", 2)
    assert far["total"] > std["total"]
    assert calculate_quote("DE", 2.1)["chargeable_weight"] == 2.5


def test_quote_insurance_and_validation(app):
    base = calculate_quote("DE", 2)
    ins = calculate_quote("DE", 2, insure=True, declared_value=1000)
    assert ins["insurance_fee"] == Decimal("15.00") and ins["total"] == base["total"] + Decimal("15.00")
    assert calculate_quote("DE", 2, insure=True, declared_value=10)["insurance_fee"] == Decimal("3.00")
    for bad in (dict(country="KP", weight_kg=1), dict(country="DE", weight_kg=0), dict(country="DE", weight_kg=500),
                dict(country="DE", weight_kg=1, insure=True, declared_value=0),
                dict(country="DE", weight_kg=1, length_cm=10), dict(country="DE", weight_kg="abc")):
        with pytest.raises(ValueError):
            calculate_quote(**bad)


def test_quote_page_and_api(client):
    html = client.get("/quote").get_data(as_text=True)
    token = csrf_from(html)
    r = client.post("/api/quote", json={"country": "NG", "weight_kg": "3", "service": "express"}, headers={"X-CSRFToken": token})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] and float(j["quote"]["total"]) > 0
    assert client.post("/api/quote", json={"country": "NG", "weight_kg": "3"}).status_code == 400  # CSRF enforced
    r = client.post("/api/quote", json={"country": "KP", "weight_kg": "3"}, headers={"X-CSRFToken": token})
    assert r.status_code == 422
    r = post(client, "/quote", {"country": "DE", "weight_kg": "2", "service": "standard"})
    assert r.status_code == 200 and "Estimated shipping fee" in r.get_data(as_text=True) and 'data-q="total">' in r.get_data(as_text=True)
    r = client.get("/quote?country=DE&weight_kg=2")
    assert "data-q-result>" in r.get_data(as_text=True)


# ---------------------------------------------------------------- auth + CSRF
def test_csrf_required_on_forms(client):
    assert client.post("/auth/login", data={"email": "a@b.co", "password": "x"}).status_code == 400
    assert client.post("/auth/register", data={}).status_code == 400
    assert client.post("/contact", data={}).status_code == 400
    assert client.post("/auth/forgot", data={"email": "a@b.co"}).status_code == 400
    assert client.post("/auth/logout").status_code in (400, 401, 302)


def test_register_login_logout(client):
    r = post(client, "/auth/register", {"name": "New User", "email": "New@Example.com", "phone": "+7 495 123 4567",
                                        "password": "Sup3rSecret-pass", "confirm": "Sup3rSecret-pass", "accept": "y"})
    assert r.status_code == 302 and r.location.endswith("/dashboard")
    assert client.get("/dashboard").status_code == 200
    post(client, "/auth/logout", page="/dashboard")
    assert client.get("/dashboard").status_code == 302
    r = login(client, "new@example.com")
    assert r.status_code == 302


def test_register_weak_password_and_duplicate(client):
    r = post(client, "/auth/register", {"name": "A B", "email": "a@b.co", "phone": "+7 495 123 4567", "password": "short",
                                        "confirm": "short", "accept": "y"})
    assert r.status_code == 200 and "at least 10" in r.get_data(as_text=True)
    make_user("dup@example.com")
    r = post(client, "/auth/register", {"name": "A B", "email": "dup@example.com", "phone": "+7 495 123 4567",
                                        "password": "Sup3rSecret-pass", "confirm": "Sup3rSecret-pass", "accept": "y"})
    assert "already exists" in r.get_data(as_text=True)


def test_login_wrong_password_lockout(app, client):
    u = make_user("lock@example.com")
    for _ in range(5):
        r = login(client, "lock@example.com", "wrong-password-1")
        assert r.status_code == 200
    r = login(client, "lock@example.com")  # correct password but locked
    assert r.status_code == 200 and "temporarily locked" in r.get_data(as_text=True)
    assert AuditLog.query.filter_by(action="login_locked_out").count() == 1


def test_open_redirect_blocked(client):
    make_user("redir@example.com")
    r = post(client, "/auth/login?next=https://evil.example/x", {"email": "redir@example.com", "password": "Sup3rSecret-pass"},
             page="/auth/login")
    assert r.status_code == 302 and "evil.example" not in r.location


def test_password_reset_flow(app, client, caplog):
    u = make_user("reset@example.com")
    caplog.set_level("WARNING")
    r = post(client, "/auth/forgot", {"email": "reset@example.com"})
    assert r.status_code == 302
    n = Notification.query.filter_by(kind="password_reset").one()
    assert n.status == "not_sent" and "SMTP not configured" in n.error and "redacted" in n.body
    assert "EMAIL NOT SENT" in caplog.text
    m = re.search(r"http://localhost(/auth/reset/[\w\-\.]+)", caplog.text)
    assert m, "dev-only reset link should be logged when SMTP is not configured"
    path = m.group(1)
    r = post(client, path, {"password": "Brand-New-Pass1", "confirm": "Brand-New-Pass1"})
    assert r.status_code == 302 and r.location.endswith("/auth/login")
    assert login(client, "reset@example.com", "Brand-New-Pass1").status_code == 302
    post(client, "/auth/logout", page="/dashboard")
    # token is single-use
    assert client.get(path).status_code == 302
    # unknown email gives identical response, sends nothing
    before = Notification.query.count()
    r = post(client, "/auth/forgot", {"email": "ghost@example.com"})
    assert r.status_code == 302 and Notification.query.count() == before


def test_reset_token_tampered(client):
    assert client.get("/auth/reset/not-a-token").status_code == 302


def test_no_reset_link_logged_in_production(app, client, caplog):
    app.config["APP_ENV"] = "production"
    make_user("prod@example.com")
    caplog.set_level("WARNING")
    post(client, "/auth/forgot", {"email": "prod@example.com"})
    assert "/auth/reset/" not in caplog.text


# ---------------------------------------------------------------- booking, tracking, dashboard
def _booked(client, email="cust@example.com"):
    make_user(email)
    login(client, email)
    r = book(client)
    assert r.status_code == 302, r.get_data(as_text=True)[:2000]
    return Shipment.query.order_by(Shipment.id.desc()).first()


def test_book_requires_login(client):
    assert client.get("/book").status_code == 302


def test_booking_creates_shipment_server_side_price(app, client):
    s = _booked(client)
    assert re.match(r"^RWC-[2-9A-Z]{4}-[2-9A-Z]{4}-[2-9A-Z]{4}$", s.tracking_number)
    assert s.status == "Booking received" and s.payment_status == "Unpaid" and len(s.events) == 1
    expected = calculate_quote("DE", 2.5, 30, 20, 15, "standard", True, 100, app.config["FUEL_SURCHARGE_PCT"], app.config["CURRENCY"])
    assert s.amount == expected["total"] and s.insured
    assert Notification.query.filter_by(kind="booking").one().status == "not_sent"
    assert AuditLog.query.filter_by(action="shipment_booked").count() == 1


def test_booking_ignores_client_price_and_validates(client):
    make_user("c2@example.com"); login(client, "c2@example.com")
    r = book(client, amount="0.01", total="0.01")
    s = Shipment.query.one()
    assert float(s.amount) > 10
    r = book(client, country="KP")
    assert r.status_code == 200 and Shipment.query.count() == 1
    r = book(client, accept_prohibited="")
    assert r.status_code == 200 and Shipment.query.count() == 1


def test_tracking_numbers_unique_and_unpredictable(app):
    from rwc.services.tracking import generate_tracking_number
    nums = {generate_tracking_number() for _ in range(2000)}
    assert len(nums) == 2000
    assert normalize_tracking("rwc 7k4m 9qx2 th8d") == "RWC-7K4M-9QX2-TH8D"
    assert normalize_tracking("RWC-0000-0000-0000") is None and normalize_tracking("abc") is None


def test_public_tracking_page(client):
    s = _booked(client)
    client.get("/auth/login")
    anon = client.application.test_client()
    r = anon.get(f"/track/{s.tracking_number}")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and s.tracking_number in html and "Booking received" in html and 'class="timeline"' in html
    assert "Import duties and taxes are assessed and collected only by the destination customs authority" in html
    assert "never ask recipients to pay release fees via this site, bank transfer, crypto or gift cards" in html
    assert "Unter den Linden" not in html and "Tverskaya" not in html and "Rita Receiver" not in html  # private data hidden
    assert anon.get("/track?number=" + s.tracking_number.lower().replace("-", " ")).status_code == 302
    assert anon.get("/track/RWC-AAAA-AAAA-AAAA").status_code == 404
    assert anon.get("/track?number=garbage").status_code == 404


def test_domestic_tracking_has_no_duty_note(app, client):
    s = _booked(client)
    s.destination_country = "RU"; db.session.commit()
    html = client.application.test_client().get(f"/track/{s.tracking_number}").get_data(as_text=True)
    assert "Import duties and taxes are assessed" not in html


def test_customer_dashboard_and_isolation(app, client):
    s = _booked(client, "owner@example.com")
    html = client.get("/dashboard").get_data(as_text=True)
    assert s.tracking_number in html
    assert client.get(f"/shipments/{s.id}").status_code == 200
    other = app.test_client()
    make_user("other@example.com"); login(other, "other@example.com")
    assert other.get(f"/shipments/{s.id}").status_code == 404
    assert other.get(f"/shipments/{s.id}/label").status_code == 404
    assert s.tracking_number not in other.get("/dashboard").get_data(as_text=True)
    assert other.get("/staff/").status_code == 403
    assert other.get("/staff/export.csv").status_code == 403


def test_label_receipt_require_payment(app, client):
    s = _booked(client)
    assert client.get(f"/shipments/{s.id}/label").status_code == 302
    assert client.get(f"/shipments/{s.id}/receipt").status_code == 302


# ---------------------------------------------------------------- Paystack
def _fake_init(monkeypatch):
    calls = {}

    def init(email, amount_minor, currency, reference, callback_url, metadata):
        calls.update(email=email, amount_minor=amount_minor, currency=currency, reference=reference, metadata=metadata)
        return {"authorization_url": "https://checkout.paystack.com/abc", "reference": reference}
    monkeypatch.setattr(paystack, "initialize", init)
    return calls


def _success_payload(pay, **over):
    d = {"status": "success", "reference": pay.reference, "amount": pay.amount_minor, "currency": pay.currency,
         "channel": "card", "gateway_response": "Successful", "metadata": {"tracking_number": pay.shipment.tracking_number}}
    d.update(over)
    return d


def test_payment_flow_callback_verification(app, client, monkeypatch):
    s = _booked(client)
    calls = _fake_init(monkeypatch)
    r = post(client, f"/pay/{s.id}", page=f"/shipments/{s.id}")
    assert r.status_code == 302 and r.location == "https://checkout.paystack.com/abc"
    assert calls["amount_minor"] == int(s.amount * 100) and calls["metadata"]["purpose"] == "shipping_fee_sender"
    pay = Payment.query.one()
    assert s.payment_status == "Unpaid"
    # visiting callback with wrong amount -> NOT paid
    monkeypatch.setattr(paystack, "verify", lambda ref: _success_payload(pay, amount=100))
    client.get(f"/pay/callback?reference={pay.reference}")
    db.session.refresh(s)
    assert s.payment_status == "Unpaid" and AuditLog.query.filter_by(action="payment_mismatch").count() == 1
    # failed
    monkeypatch.setattr(paystack, "verify", lambda ref: _success_payload(pay, status="failed"))
    client.get(f"/pay/callback?reference={pay.reference}"); db.session.refresh(s)
    assert s.payment_status == "Unpaid"
    # verified success
    monkeypatch.setattr(paystack, "verify", lambda ref: _success_payload(pay))
    client.get(f"/pay/callback?reference={pay.reference}"); db.session.refresh(s); db.session.refresh(pay)
    assert s.payment_status == "Paid" and pay.status == "success" and pay.verified_via == "callback"
    assert Notification.query.filter_by(kind="payment").count() == 1
    # idempotent
    client.get(f"/pay/callback?reference={pay.reference}")
    assert Notification.query.filter_by(kind="payment").count() == 1
    # label + receipt now available
    assert client.get(f"/shipments/{s.id}/label").status_code == 200
    html = client.get(f"/shipments/{s.id}/receipt").get_data(as_text=True)
    assert pay.reference in html and "Total shipping fee" in html
    # can't pay twice
    r = post(client, f"/pay/{s.id}", page=f"/shipments/{s.id}")
    assert r.location.endswith(f"/shipments/{s.id}")


def test_callback_unknown_reference(client):
    assert client.get("/pay/callback?reference=nope").status_code == 302


def test_payment_not_configured(app, client):
    app.config["PAYSTACK_SECRET_KEY"] = ""
    s = _booked(client)
    r = post(client, f"/pay/{s.id}", page=f"/shipments/{s.id}")
    assert r.status_code == 302 and Payment.query.count() == 0
    html = client.get(f"/shipments/{s.id}").get_data(as_text=True)
    assert "Online payment coming soon" in html and "disabled" in html
    # booking itself still works and nothing crashed
    assert client.get("/dashboard").status_code == 200


def test_only_owner_can_pay(app, client, monkeypatch):
    s = _booked(client)
    _fake_init(monkeypatch)
    other = app.test_client(); make_user("thief@example.com"); login(other, "thief@example.com")
    assert post(other, f"/pay/{s.id}", page="/dashboard").status_code == 404


def _sign(body: bytes, secret="sk_test_unit_tests"):
    return hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()


def test_webhook_signature_and_processing(app, client, monkeypatch):
    s = _booked(client)
    _fake_init(monkeypatch)
    post(client, f"/pay/{s.id}", page=f"/shipments/{s.id}")
    pay = Payment.query.one()
    body = json.dumps({"event": "charge.success", "data": _success_payload(pay)}).encode()
    anon = app.test_client()  # webhook is CSRF-exempt but signature protected
    assert anon.post("/webhooks/paystack", data=body).status_code == 401
    assert anon.post("/webhooks/paystack", data=body, headers={"x-paystack-signature": "deadbeef"}).status_code == 401
    assert anon.post("/webhooks/paystack", data=body, headers={"x-paystack-signature": _sign(body, "wrong")}).status_code == 401
    db.session.refresh(s); assert s.payment_status == "Unpaid"
    assert AuditLog.query.filter_by(action="webhook_bad_signature").count() == 3
    r = anon.post("/webhooks/paystack", data=body, headers={"x-paystack-signature": _sign(body), "Content-Type": "application/json"})
    assert r.status_code == 200
    db.session.refresh(s); db.session.refresh(pay)
    assert s.payment_status == "Paid" and pay.verified_via == "webhook"
    # validly-signed but wrong amount is rejected
    s2 = Shipment.query.first()
    bad = json.dumps({"event": "charge.success", "data": _success_payload(pay, amount=1)}).encode()
    anon.post("/webhooks/paystack", data=bad, headers={"x-paystack-signature": _sign(bad)})


def test_webhook_amount_mismatch_not_paid(app, client, monkeypatch):
    s = _booked(client); _fake_init(monkeypatch)
    post(client, f"/pay/{s.id}", page=f"/shipments/{s.id}")
    pay = Payment.query.one()
    body = json.dumps({"event": "charge.success", "data": _success_payload(pay, amount=5)}).encode()
    app.test_client().post("/webhooks/paystack", data=body, headers={"x-paystack-signature": _sign(body)})
    db.session.refresh(s)
    assert s.payment_status == "Unpaid"


# ---------------------------------------------------------------- staff / admin
def _paid_shipment(app, client, monkeypatch, email="payer@example.com"):
    s = _booked(client, email)
    _fake_init(monkeypatch)
    post(client, f"/pay/{s.id}", page=f"/shipments/{s.id}")
    pay = Payment.query.order_by(Payment.id.desc()).first()
    monkeypatch.setattr(paystack, "verify", lambda ref: _success_payload(pay))
    client.get(f"/pay/callback?reference={pay.reference}")
    db.session.refresh(s)
    assert s.is_paid
    return s


def _staff_client(app, role="dispatcher", email=None):
    c = app.test_client()
    make_user(email or f"{role}@example.com", role=role)
    login(c, email or f"{role}@example.com")
    return c


def test_status_flow_and_payment_gate(app, client):
    s = _booked(client)
    d = _staff_client(app)
    url = f"/staff/shipments/{s.id}/status"
    page = f"/staff/shipments/{s.id}"
    post(d, url, {"status": "Picked up", "location": "Moscow"}, page=page)
    db.session.refresh(s)
    assert s.status == "Booking received"  # blocked: unpaid
    post(d, url, {"status": "Pickup scheduled", "location": "Moscow"}, page=page)
    db.session.refresh(s); assert s.status == "Pickup scheduled"


def test_status_updates_paid(app, client, monkeypatch):
    s = _paid_shipment(app, client, monkeypatch)
    d = _staff_client(app)
    url, page = f"/staff/shipments/{s.id}/status", f"/staff/shipments/{s.id}"
    post(d, url, {"status": "Picked up", "location": "Moscow", "details": "Collected"}, page=page)
    post(d, url, {"status": "Delivered"}, page=page)  # needs received_by
    db.session.refresh(s); assert s.status == "Picked up"
    post(d, url, {"status": "Delivered", "received_by": "R. Receiver"}, page=page)
    db.session.refresh(s); assert s.status == "Delivered" and s.delivered_at and len(s.events) == 3
    post(d, url, {"status": "In transit"}, page=page)  # terminal: dispatcher can't reopen
    db.session.refresh(s); assert s.status == "Delivered"
    assert Notification.query.filter_by(kind="status").count() >= 2


def test_status_list_has_no_customs_hold_states():
    bad = re.compile(r"customs|held|hold|release|clearance|duty|payment|pay", re.I)
    assert not [s for s in ALL_STATUSES if bad.search(s)], ALL_STATUSES


@pytest.mark.parametrize("text", ["Parcel held at customs, pay release fee", "Customs duty of 380 EUR is due",
                                  "Pay clearance fee by bitcoin", "send gift card codes", "bank transfer required",
                                  "Pending payment to release", "customs fee payable"])
def test_guard_blocks_scam_wording(text):
    assert violates_guard(text)


def test_guard_allows_normal_text():
    assert not violates_guard("Departed Moscow hub, on flight to Frankfurt")


def test_staff_cannot_post_scam_details(app, client, monkeypatch):
    s = _paid_shipment(app, client, monkeypatch)
    d = _staff_client(app)
    post(d, f"/staff/shipments/{s.id}/status", {"status": "In transit", "details": "Held at customs: pay release fee to continue"},
         page=f"/staff/shipments/{s.id}")
    db.session.refresh(s)
    assert s.status == "Booking received" and AuditLog.query.filter_by(action="status_text_blocked").count() == 1
    # ticket replies are guarded too
    post(client, "/support", {"subject": "Where is it", "category": "shipment", "body": "Where is my parcel please?"})
    from rwc.models import Ticket
    t = Ticket.query.one()
    post(d, f"/staff/tickets/{t.reference}", {"body": "Please pay the customs fee by gift card"})
    assert len(t.messages) == 1


def test_manual_paid_is_impossible(app, client):
    s = _booked(client)
    d = _staff_client(app, "admin")
    page = f"/staff/shipments/{s.id}"
    adj = f"/staff/shipments/{s.id}/payment/adjust"
    post(d, adj, {"new_status": "Paid", "reason": "customer sent cash, trust me"}, page=page)
    db.session.refresh(s)
    assert s.payment_status == "Unpaid"
    assert AuditLog.query.filter_by(action="payment_manual_paid_attempt_blocked").count() == 1
    # no "Paid" option offered in the UI
    html = d.get(page).get_data(as_text=True)
    assert '<option value="Paid"' not in html.split("Manual adjustment")[1]
    # refunded only from paid
    post(d, adj, {"new_status": "Refunded", "reason": "attempt without payment"}, page=page)
    db.session.refresh(s); assert s.payment_status == "Unpaid"
    # reason required
    post(d, adj, {"new_status": "Unpaid", "reason": "x"}, page=page)


def test_manual_adjustment_audited(app, client, monkeypatch):
    s = _paid_shipment(app, client, monkeypatch)
    d = _staff_client(app, "admin")
    post(d, f"/staff/shipments/{s.id}/payment/adjust", {"new_status": "Refunded", "reason": "Refunded in Paystack dashboard ref 123"},
         page=f"/staff/shipments/{s.id}")
    db.session.refresh(s)
    assert s.payment_status == "Refunded"
    a = AuditLog.query.filter_by(action="payment_manual_adjustment").one()
    assert "Paid -> Refunded" in a.detail and "Refunded in Paystack dashboard" in a.detail


def test_staff_reverify_requires_paystack_confirmation(app, client, monkeypatch):
    s = _booked(client)
    d = _staff_client(app)
    ref = "RWCP-MANUAL123"
    page = f"/staff/shipments/{s.id}"
    url = f"/staff/shipments/{s.id}/payment/reverify"
    # paystack says abandoned
    monkeypatch.setattr(paystack, "verify", lambda r: {"status": "abandoned", "reference": r, "amount": 0, "currency": "USD",
                                                       "metadata": {"tracking_number": s.tracking_number}})
    post(d, url, {"reference": ref}, page=page); db.session.refresh(s)
    assert s.payment_status == "Unpaid"
    # success but for another shipment's metadata
    monkeypatch.setattr(paystack, "verify", lambda r: {"status": "success", "reference": r, "amount": int(s.amount * 100), "currency": s.currency,
                                                       "metadata": {"tracking_number": "RWC-OTHER"}})
    post(d, url, {"reference": ref}, page=page); db.session.refresh(s)
    assert s.payment_status == "Unpaid"
    # success, right shipment, wrong amount
    monkeypatch.setattr(paystack, "verify", lambda r: {"status": "success", "reference": r, "amount": 100, "currency": s.currency,
                                                       "metadata": {"tracking_number": s.tracking_number}})
    post(d, url, {"reference": ref}, page=page); db.session.refresh(s)
    assert s.payment_status == "Unpaid"
    # fully verified
    monkeypatch.setattr(paystack, "verify", lambda r: {"status": "success", "reference": r, "amount": int(s.amount * 100), "currency": s.currency,
                                                       "metadata": {"tracking_number": s.tracking_number}, "channel": "card"})
    post(d, url, {"reference": ref}, page=page); db.session.refresh(s)
    assert s.payment_status == "Paid"
    assert Payment.query.filter_by(reference=ref).one().verified_via == "staff-reverify"


def test_admin_dashboard_filters_search_csv(app, client, monkeypatch):
    s = _paid_shipment(app, client, monkeypatch)
    s.recipient_name = "=HYPERLINK(\"http://evil\")"; db.session.commit()
    d = _staff_client(app, "admin")
    html = d.get("/staff/").get_data(as_text=True)
    assert s.tracking_number in html and "Verified revenue" in html and "Export CSV" in html
    assert s.tracking_number in d.get("/staff/?q=" + s.tracking_number[:9]).get_data(as_text=True)
    assert s.tracking_number not in d.get("/staff/?q=zzzzznomatch").get_data(as_text=True)
    assert s.tracking_number in d.get("/staff/?payment=Paid&service=standard&country=DE").get_data(as_text=True)
    assert s.tracking_number not in d.get("/staff/?payment=Unpaid").get_data(as_text=True)
    r = d.get("/staff/export.csv?payment=Paid")
    assert r.status_code == 200 and r.mimetype == "text/csv" and "attachment" in r.headers["Content-Disposition"]
    text = r.get_data(as_text=True)
    assert s.tracking_number in text and "'=HYPERLINK" in text  # formula injection neutralised
    assert AuditLog.query.filter_by(action="csv_export").count() == 1
    assert d.get("/staff/export.csv?payment=Unpaid").get_data(as_text=True).count("\n") == 1  # header only


def test_rider_assignment_and_rider_view(app, client, monkeypatch):
    s = _paid_shipment(app, client, monkeypatch)
    rider = make_user("rider@example.com", role="rider", name="Rod Rider")
    d = _staff_client(app)
    post(d, f"/staff/shipments/{s.id}/assign", {"rider_id": rider.id}, page=f"/staff/shipments/{s.id}")
    db.session.refresh(s); assert s.rider_id == rider.id
    assert AuditLog.query.filter_by(action="rider_assigned").count() == 1
    r = app.test_client(); login(r, "rider@example.com")
    assert s.tracking_number in r.get("/staff/rider").get_data(as_text=True)
    assert r.get("/staff/").status_code == 403 and r.get("/staff/users").status_code == 403
    page = f"/staff/rider/{s.id}"
    post(r, page, {"status": "Picked up", "location": "Tverskaya"}, page=page)
    db.session.refresh(s); assert s.status == "Picked up"
    post(r, page, {"status": "In transit"}, page=page)  # not a rider status
    db.session.refresh(s); assert s.status == "Picked up"
    post(r, page, {"status": "Delivered", "received_by": "Rita"}, page=page)
    db.session.refresh(s); assert s.status == "Delivered"
    # another rider can't open it
    make_user("rider2@example.com", role="rider")
    r2 = app.test_client(); login(r2, "rider2@example.com")
    assert r2.get(page).status_code == 404


def test_admin_staff_management(app):
    a = app.test_client(); admin = make_user("boss@example.com", role="admin"); login(a, "boss@example.com")
    r = post(a, "/staff/users", {"name": "New Rider", "email": "newrider@example.com", "role": "rider", "password": "Sup3rSecret-pass"})
    assert r.status_code == 302
    u = User.query.filter_by(email="newrider@example.com").one(); assert u.role == "rider"
    post(a, f"/staff/users/{u.id}/toggle", page="/staff/users")
    db.session.refresh(u); assert not u.active
    # deactivated user can't log in
    c = app.test_client(); r = login(c, "newrider@example.com"); assert r.status_code == 200
    post(a, f"/staff/users/{admin.id}/toggle", page="/staff/users")  # can't deactivate self
    db.session.refresh(admin); assert admin.active
    assert "Audit log" in a.get("/staff/audit").get_data(as_text=True)
    assert a.get("/staff/outbox").status_code == 200
    d = _staff_client(app, "dispatcher")
    assert d.get("/staff/audit").status_code == 403 and d.get("/staff/users").status_code == 403


def test_customer_cannot_use_staff_endpoints(app, client):
    s = _booked(client)
    for path in ("/staff/", "/staff/tickets", "/staff/users", "/staff/audit", "/staff/outbox", "/staff/rider"):
        assert client.get(path).status_code == 403
    assert post(client, f"/staff/shipments/{s.id}/status", {"status": "Delivered", "received_by": "x"}, page="/dashboard").status_code == 403


# ---------------------------------------------------------------- support
def test_support_tickets_roundtrip(app, client):
    make_user("tk@example.com"); login(client, "tk@example.com")
    r = post(client, "/support", {"subject": "Parcel question", "category": "shipment", "body": "Where is my parcel? It's late."})
    assert r.status_code == 302
    from rwc.models import Ticket
    t = Ticket.query.one()
    assert client.get(f"/support/{t.reference}").status_code == 200
    d = _staff_client(app)
    post(d, f"/staff/tickets/{t.reference}", {"body": "We are checking this for you."})
    db.session.refresh(t); assert t.status == "pending" and len(t.messages) == 2
    assert "We are checking" in client.get(f"/support/{t.reference}").get_data(as_text=True)
    other = app.test_client(); make_user("o2@example.com"); login(other, "o2@example.com")
    assert other.get(f"/support/{t.reference}").status_code == 404


def test_contact_form_and_honeypot(client):
    from rwc.models import Ticket
    r = post(client, "/contact", {"name": "Visitor", "email": "v@example.com", "subject": "Hello there", "body": "I have a question about shipping."})
    assert r.status_code == 302 and Ticket.query.count() == 1
    post(client, "/contact", {"name": "Bot", "email": "b@example.com", "subject": "Buy now", "body": "spam spam spam spam", "website": "http://spam"})
    assert Ticket.query.count() == 1


# ---------------------------------------------------------------- misc security
def test_rate_limiting_enabled():
    from rwc import create_app
    from rwc.config import TestingConfig
    from rwc.extensions import limiter

    class RL(TestingConfig):
        RATELIMIT_ENABLED = True
    app = create_app(RL)
    with app.app_context():
        db.create_all()
    c = app.test_client()
    codes = [c.get("/track/RWC-AAAA-AAAA-AAAA").status_code for _ in range(35)]
    assert 429 in codes and codes[0] == 404
    login_codes = []
    for _ in range(14):
        html = c.get("/auth/login").get_data(as_text=True)
        login_codes.append(c.post("/auth/login", data={"csrf_token": csrf_from(html), "email": "x@y.co", "password": "nope"}).status_code)
    assert 429 in login_codes
    limiter.enabled = False


def test_production_requires_secret_key(monkeypatch):
    from rwc.config import Config
    from rwc import create_app

    class Prod(Config):
        APP_ENV = "production"; SECRET_KEY = ""; SQLALCHEMY_DATABASE_URI = "sqlite://"
    with pytest.raises(RuntimeError):
        create_app(Prod)


def test_no_customs_payment_routes_exist(app):
    rules = [r.rule for r in app.url_map.iter_rules()]
    assert not [r for r in rules if re.search(r"customs|clearance|release|invoice|crypto|giftcard", r, re.I)]
    from rwc.services import paystack as p
    assert p.to_minor(Decimal("12.34")) == 1234


# ---------------------------------------------------------------- notifications honesty
def test_smtp_success_marks_sent_and_failure_marks_failed(app, monkeypatch):
    import smtplib
    from rwc.services import notify
    app.config.update(SMTP_HOST="smtp.test", SMTP_FROM="no-reply@test.example", SMTP_USER="u", SMTP_PASSWORD="p")
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, **k): pass
        def login(self, u, p): pass
        def send_message(self, m): sent.append(m)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    n = notify.send_email("x@example.com", "Hello", "Body", "test")
    assert n.status == "sent" and sent and sent[0]["To"] == "x@example.com"

    class BoomSMTP(FakeSMTP):
        def send_message(self, m): raise smtplib.SMTPException("relay denied")
    monkeypatch.setattr(smtplib, "SMTP", BoomSMTP)
    n = notify.send_email("x@example.com", "Hello", "Body", "test")
    assert n.status == "failed" and "relay denied" in n.error


def test_sms_not_configured_is_not_faked(app):
    from rwc.services import notify
    n = notify.send_sms("+79990000000", "hi", "test")
    assert n.status == "not_sent"


def test_book_prefill_from_quote_link(app, client):
    make_user("pf@example.com"); login(client, "pf@example.com")
    html = client.get("/book?country=NG&weight_kg=3&service=express&insure=true&declared_value=50").get_data(as_text=True)
    assert 'value="3.0"' in html or 'value="3"' in html
    assert '<option selected value="NG">' in html and '<option selected value="express">' in html


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.get_json() == {"status": "ok", "database": "ok"}


def test_create_admin_if_missing(app):
    from click.testing import CliRunner
    from rwc.models import User
    runner = app.test_cli_runner()
    r = runner.invoke(args=["create-admin", "--if-missing"])
    assert "skipping" in r.output
    env = {"ADMIN_EMAIL": "Boss@Example.com", "ADMIN_PASSWORD": "Str0ng-Passphrase-77"}
    r = runner.invoke(args=["create-admin", "--if-missing"], env=env)
    assert "created" in r.output
    r = runner.invoke(args=["create-admin", "--if-missing"], env={**env, "ADMIN_PASSWORD": "Another-Passphrase-88"})
    assert "already exists" in r.output
    with app.app_context():
        u = User.query.filter_by(email="boss@example.com").first()
        assert u.role == "admin" and u.check_password("Str0ng-Passphrase-77")
