from datetime import datetime, timedelta, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db, login_manager


def utcnow():
    """Naive UTC timestamp (all datetimes are stored as UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


ROLES = ("customer", "rider", "dispatcher", "admin")
STAFF_ROLES = ("rider", "dispatcher", "admin")


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(160), unique=True, nullable=False, index=True)
    phone = db.Column(db.String(40))
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="customer", index=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    sms_opt_in = db.Column(db.Boolean, nullable=False, default=False)
    failed_logins = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime)
    last_login_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow)

    shipments = db.relationship("Shipment", foreign_keys="Shipment.customer_id", back_populates="customer", lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_active(self):  # Flask-Login
        return bool(self.active)

    @property
    def is_staff(self):
        return self.role in STAFF_ROLES

    @property
    def is_ops(self):
        return self.role in ("dispatcher", "admin")

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def is_locked(self):
        return bool(self.locked_until and self.locked_until > utcnow())

    def register_failed_login(self, max_failures, lockout_minutes):
        self.failed_logins = (self.failed_logins or 0) + 1
        if self.failed_logins >= max_failures:
            self.locked_until = utcnow() + timedelta(minutes=lockout_minutes)
            self.failed_logins = 0
            return True
        return False

    def get_id(self):
        # Session id includes a slice of the password hash so sessions die on password change.
        return f"{self.id}:{self.password_hash[-12:]}"


@login_manager.user_loader
def load_user(session_id):
    try:
        uid, tail = str(session_id).split(":", 1)
        user = db.session.get(User, int(uid))
    except (ValueError, TypeError):
        return None
    if user and user.active and user.password_hash[-12:] == tail:
        return user
    return None


class Shipment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tracking_number = db.Column(db.String(24), unique=True, nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    rider_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)

    sender_name = db.Column(db.String(120), nullable=False)
    sender_phone = db.Column(db.String(40), nullable=False)
    sender_address = db.Column(db.String(255), nullable=False)
    origin_city = db.Column(db.String(100), nullable=False)
    origin_country = db.Column(db.String(2), nullable=False, default="RU")

    recipient_name = db.Column(db.String(120), nullable=False)
    recipient_phone = db.Column(db.String(40), nullable=False)
    recipient_email = db.Column(db.String(160))
    destination_address = db.Column(db.String(255), nullable=False)
    destination_city = db.Column(db.String(100), nullable=False)
    destination_postcode = db.Column(db.String(20))
    destination_country = db.Column(db.String(2), nullable=False, index=True)
    zone = db.Column(db.String(8), nullable=False)

    parcel_description = db.Column(db.String(255), nullable=False)
    weight_kg = db.Column(db.Float, nullable=False)
    length_cm = db.Column(db.Float)
    width_cm = db.Column(db.Float)
    height_cm = db.Column(db.Float)
    chargeable_weight_kg = db.Column(db.Float, nullable=False)
    declared_value = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    insured = db.Column(db.Boolean, nullable=False, default=False)
    service_type = db.Column(db.String(20), nullable=False, default="standard", index=True)
    pickup_date = db.Column(db.Date)
    notes = db.Column(db.Text)

    # Price snapshot (computed server-side at booking)
    currency = db.Column(db.String(3), nullable=False)
    shipping_charge = db.Column(db.Numeric(12, 2), nullable=False)
    fuel_surcharge = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    insurance_fee = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    amount = db.Column(db.Numeric(12, 2), nullable=False)  # total sender pays
    est_days_min = db.Column(db.Integer)
    est_days_max = db.Column(db.Integer)

    status = db.Column(db.String(40), nullable=False, default="Booking received", index=True)
    payment_status = db.Column(db.String(20), nullable=False, default="Unpaid", index=True)  # Unpaid | Paid | Refunded
    delivered_at = db.Column(db.DateTime)
    received_by = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    customer = db.relationship("User", foreign_keys=[customer_id], back_populates="shipments")
    rider = db.relationship("User", foreign_keys=[rider_id])
    events = db.relationship("TrackingEvent", backref="shipment", lazy=True, cascade="all, delete-orphan",
                             order_by="TrackingEvent.created_at, TrackingEvent.id")
    payments = db.relationship("Payment", backref="shipment", lazy=True, cascade="all, delete-orphan",
                               order_by="Payment.created_at")

    @property
    def is_international(self):
        return self.destination_country != self.origin_country

    @property
    def is_paid(self):
        return self.payment_status == "Paid"


class TrackingEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    shipment_id = db.Column(db.Integer, db.ForeignKey("shipment.id"), nullable=False, index=True)
    status = db.Column(db.String(80), nullable=False)
    location = db.Column(db.String(160))
    details = db.Column(db.Text)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    actor = db.relationship("User", foreign_keys=[actor_id])


class Payment(db.Model):
    """A Paystack payment attempt for a shipment's shipping fee (paid by the sender only)."""
    id = db.Column(db.Integer, primary_key=True)
    shipment_id = db.Column(db.Integer, db.ForeignKey("shipment.id"), nullable=False, index=True)
    reference = db.Column(db.String(64), unique=True, nullable=False, index=True)
    amount_minor = db.Column(db.Integer, nullable=False)  # e.g. cents / kobo
    currency = db.Column(db.String(3), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="initiated")  # initiated | success | failed | abandoned
    gateway_response = db.Column(db.String(255))
    channel = db.Column(db.String(40))
    verified_via = db.Column(db.String(20))  # callback | webhook | staff-reverify
    paid_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow)


class Ticket(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(16), unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)  # null for public contact form
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(160), nullable=False)
    subject = db.Column(db.String(160), nullable=False)
    category = db.Column(db.String(30), nullable=False, default="general")
    tracking_number = db.Column(db.String(24))
    status = db.Column(db.String(20), nullable=False, default="open", index=True)  # open | pending | closed
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    user = db.relationship("User", foreign_keys=[user_id])
    messages = db.relationship("TicketMessage", backref="ticket", lazy=True, cascade="all, delete-orphan",
                               order_by="TicketMessage.created_at, TicketMessage.id")


class TicketMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("ticket.id"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    is_staff = db.Column(db.Boolean, nullable=False, default=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    author = db.relationship("User", foreign_keys=[author_id])


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    actor_label = db.Column(db.String(160))  # email at time of action, or "anonymous"/"system"/"paystack-webhook"
    action = db.Column(db.String(60), nullable=False, index=True)
    entity_type = db.Column(db.String(40))
    entity_id = db.Column(db.String(40))
    detail = db.Column(db.Text)
    ip = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=utcnow, index=True)

    actor = db.relationship("User", foreign_keys=[actor_id])


class Notification(db.Model):
    """Outbox: an honest record of every email/SMS the app tried (or could not) to send."""
    id = db.Column(db.Integer, primary_key=True)
    channel = db.Column(db.String(10), nullable=False)  # email | sms
    kind = db.Column(db.String(40), nullable=False)
    recipient = db.Column(db.String(160), nullable=False)
    subject = db.Column(db.String(200))
    body = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False)  # sent | failed | not_sent
    error = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
