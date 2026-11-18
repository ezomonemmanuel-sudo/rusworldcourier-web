import os
import random
from datetime import timedelta

import click
from flask import current_app
from flask.cli import with_appcontext

from .extensions import db
from .models import Payment, Shipment, TrackingEvent, User, utcnow


def register_cli(app):
    @app.cli.command("init-db")
    @with_appcontext
    def init_db():
        """Create / upgrade the database schema (uses Alembic migrations when present)."""
        from flask_migrate import upgrade
        mig = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")
        if os.path.isdir(mig):
            upgrade(directory=mig)
            click.echo("Database upgraded via migrations.")
        else:
            db.create_all()
            click.echo("Database tables created.")

    @app.cli.command("create-admin")
    @click.option("--email", envvar="ADMIN_EMAIL", prompt=False, help="Admin email (or env ADMIN_EMAIL).")
    @click.option("--name", envvar="ADMIN_NAME", default="Administrator", show_default=True)
    @click.option("--password", envvar="ADMIN_PASSWORD", help="Password (or env ADMIN_PASSWORD).")
    @click.option("--if-missing", is_flag=True,
                  help="Bootstrap mode for start-up hooks: do nothing if email/password are unset or the user already "
                       "exists (never resets an existing password).")
    @with_appcontext
    def create_admin(email, name, password, if_missing):
        """Create (or promote) an administrator, non-interactively when options/env are given."""
        if if_missing:
            if not email or not password:
                click.echo("create-admin --if-missing: ADMIN_EMAIL/ADMIN_PASSWORD not set; skipping.")
                return
            if User.query.filter_by(email=email.strip().lower()).first():
                click.echo("create-admin --if-missing: user already exists; skipping.")
                return
        from .forms import strong_password
        from wtforms.validators import ValidationError
        if not email:
            email = click.prompt("Admin email")
        if not password:
            password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
        email = email.strip().lower()

        class _F:  # minimal field shim for the shared password validator
            data = password
        try:
            strong_password(None, _F)
        except ValidationError as e:
            raise click.ClickException(f"Weak password: {e}")
        u = User.query.filter_by(email=email).first()
        if u:
            u.role, u.active = "admin", True
            u.set_password(password)
            click.echo(f"Existing user {email} promoted to admin and password reset.")
        else:
            u = User(name=name, email=email, role="admin", phone=None)
            u.set_password(password)
            db.session.add(u)
            click.echo(f"Admin {email} created.")
        from .services.audit import audit
        audit("admin_created_via_cli", "user", email, actor_label="cli")
        db.session.commit()

    @app.cli.command("seed-demo")
    @click.option("--force", is_flag=True, help="Allow in production (NOT recommended).")
    @with_appcontext
    def seed_demo(force):
        """Create DEMO data (customer, rider, dispatcher, sample shipments) for screenshots/testing only."""
        if current_app.config["APP_ENV"] == "production" and not force:
            raise click.ClickException("Refusing to seed demo data in production.")
        from .services.tracking import generate_tracking_number
        from .tariff import calculate_quote
        pw = "DemoPass12345"

        def mk(email, name, role):
            u = User.query.filter_by(email=email).first()
            if not u:
                u = User(name=name, email=email, role=role, phone="+7 495 000 0000")
                u.set_password(pw)
                db.session.add(u)
            return u
        cust = mk("demo.customer@example.com", "Demo Customer", "customer")
        rider = mk("demo.rider@example.com", "Demo Rider", "rider")
        mk("demo.dispatcher@example.com", "Demo Dispatcher", "dispatcher")
        db.session.flush()
        flows = [
            ("DE", "Berlin", "Express", "express", ["Booking received", "Pickup scheduled", "Picked up", "Processing at origin hub",
                                                    "Export processing", "Departed origin country", "In transit"], True),
            ("NG", "Lagos", "Standard", "standard", ["Booking received", "Pickup scheduled", "Picked up"], True),
            ("US", "New York", "Economy", "economy", ["Booking received"], False),
            ("KZ", "Almaty", "Standard", "standard", ["Booking received", "Pickup scheduled", "Picked up", "Processing at origin hub",
                                                      "Export processing", "Departed origin country", "Arrived in destination country",
                                                      "With local delivery partner", "Out for delivery", "Delivered"], True),
        ]
        locs = {"Booking received": "Moscow, Russia", "Pickup scheduled": "Moscow, Russia", "Picked up": "Moscow, Russia",
                "Processing at origin hub": "Moscow hub, Russia", "Export processing": "Moscow hub, Russia",
                "Departed origin country": "Sheremetyevo Airport (SVO), Russia", "In transit": "In transit",
                "Arrived in destination country": "Destination country", "With local delivery partner": "Destination city",
                "Out for delivery": "Destination city", "Delivered": "Destination city"}
        for cc, city, _, svc, statuses, paid in flows:
            q = calculate_quote(cc, 2.5, 30, 20, 15, svc, False, 0, current_app.config["FUEL_SURCHARGE_PCT"], current_app.config["CURRENCY"])
            t0 = utcnow() - timedelta(days=len(statuses))
            s = Shipment(
                tracking_number=generate_tracking_number(), customer_id=cust.id, rider_id=rider.id if len(statuses) <= 3 else None,
                sender_name="Demo Customer", sender_phone="+7 495 000 0000", sender_address="Tverskaya St 1, Moscow",
                origin_city="Moscow", recipient_name="Alex Example", recipient_phone="+49 30 000000",
                destination_address="Example Str. 10", destination_city=city, destination_country=cc, zone=q["zone"],
                parcel_description="Documents and gifts (demo)", weight_kg=2.5, length_cm=30, width_cm=20, height_cm=15,
                chargeable_weight_kg=q["chargeable_weight"], declared_value=0, service_type=svc, currency=q["currency"],
                shipping_charge=q["shipping_charge"], fuel_surcharge=q["fuel_surcharge"], insurance_fee=0, amount=q["total"],
                est_days_min=q["days_min"], est_days_max=q["days_max"], status=statuses[-1],
                payment_status="Paid" if paid else "Unpaid", created_at=t0)
            for i, st in enumerate(statuses):
                loc = locs[st]
                if st in ("Arrived in destination country", "With local delivery partner", "Out for delivery", "Delivered"):
                    loc = f"{city}"
                s.events.append(TrackingEvent(status=st, location=loc, details="Demo data.", created_at=t0 + timedelta(days=i, hours=random.randint(0, 5))))
            if statuses[-1] == "Delivered":
                s.delivered_at = utcnow()
                s.received_by = "Alex Example"
            db.session.add(s)
            if paid:  # clearly-labelled DEMO payment record (never produced by real code paths)
                db.session.flush()
                db.session.add(Payment(shipment_id=s.id, reference="DEMO-" + s.tracking_number[-9:], amount_minor=int(s.amount * 100),
                                       currency=s.currency, status="success", verified_via="demo-seed", paid_at=t0, channel="demo"))
            click.echo(f"{s.tracking_number}  {city:10} {s.status}")
        db.session.commit()
        click.echo(f"Demo users (password {pw}): demo.customer@/demo.rider@/demo.dispatcher@example.com")
