import re

import pytest
from flask.testing import FlaskClient

from rwc import create_app
from rwc.config import TestingConfig
from rwc.extensions import db as _db
from rwc.models import User


class CtxClient(FlaskClient):
    """Run every request in its own app context so `g` (flask-login user, CSRF cache) never leaks between
    requests or between test clients, as in production."""

    def open(self, *a, **kw):
        with self.application.app_context():
            return super().open(*a, **kw)


@pytest.fixture()
def app():
    app = create_app(TestingConfig)
    app.test_client_class = CtxClient
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def csrf_from(html):
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html) or re.search(r'value="([^"]+)"[^>]*name="csrf_token"', html) \
        or re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "no csrf token in page"
    return m.group(1)


def post(client, url, data=None, page=None, **kw):
    """POST with a valid CSRF token fetched from `page` (defaults to the same URL)."""
    html = client.get(page or url).get_data(as_text=True)
    d = dict(data or {})
    d["csrf_token"] = csrf_from(html)
    return client.post(url, data=d, **kw)


def make_user(email, role="customer", password="Sup3rSecret-pass", name="Test User", phone="+7 495 111 2233"):
    u = User(name=name, email=email, role=role, phone=phone)
    u.set_password(password)
    _db.session.add(u)
    _db.session.commit()
    return u


def login(client, email, password="Sup3rSecret-pass"):
    return post(client, "/auth/login", {"email": email, "password": password})


BOOKING = {
    "country": "DE", "service": "standard", "weight_kg": "2.5", "length_cm": "30", "width_cm": "20", "height_cm": "15",
    "declared_value": "100", "insure": "y",
    "sender_name": "Sam Sender", "sender_phone": "+7 495 111 2233", "origin_city": "Moscow",
    "sender_address": "Tverskaya 1", "recipient_name": "Rita Receiver", "recipient_phone": "+49 30 123456",
    "recipient_email": "rita@example.com", "destination_city": "Berlin", "destination_postcode": "10115",
    "destination_address": "Unter den Linden 1", "parcel_description": "Books", "accept_terms": "y",
    "accept_prohibited": "y",
}


def book(client, **over):
    d = dict(BOOKING)
    d.update(over)
    return post(client, "/book", d)
