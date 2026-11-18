import re

from flask_wtf import FlaskForm
from wtforms import (BooleanField, DateField, DecimalField, EmailField, FloatField, HiddenField, PasswordField,
                     SelectField, StringField, SubmitField, TextAreaField)
from wtforms.validators import (DataRequired, Email, EqualTo, InputRequired, Length, NumberRange, Optional,
                                ValidationError)

from .services.tracking import ALL_STATUSES
from .tariff import COUNTRIES, MAX_DECLARED_VALUE, MAX_WEIGHT_KG, SERVICES

COUNTRY_CHOICES = [("", "Select country")] + [(c, n) for c, n in COUNTRIES]
SERVICE_CHOICES = [(k, v["name"]) for k, v in SERVICES.items()]
PHONE_RE = re.compile(r"^\+?[0-9 ()\-]{7,20}$")


def strong_password(form, field):
    p = field.data or ""
    if len(p) < 10:
        raise ValidationError("Use at least 10 characters.")
    if not (re.search(r"[A-Za-z]", p) and re.search(r"[0-9]", p)):
        raise ValidationError("Include both letters and numbers.")
    if p.lower() in {"password123", "1234567890", "qwerty12345", "password1234"}:
        raise ValidationError("That password is too common.")


def phone_ok(form, field):
    if field.data and not PHONE_RE.match(field.data.strip()):
        raise ValidationError("Enter a valid phone number, e.g. +7 495 123 4567.")


class _Strip:
    """Mixin-ish: strip whitespace on string fields."""
    pass


class LoginForm(FlaskForm):
    email = EmailField("Email", validators=[DataRequired(), Email(), Length(max=160)], filters=[lambda x: x.strip().lower() if x else x])
    password = PasswordField("Password", validators=[DataRequired(), Length(max=200)])
    submit = SubmitField("Sign in")


class RegisterForm(FlaskForm):
    name = StringField("Full name", validators=[DataRequired(), Length(min=2, max=120)], filters=[lambda x: x.strip() if x else x])
    email = EmailField("Email", validators=[DataRequired(), Email(), Length(max=160)], filters=[lambda x: x.strip().lower() if x else x])
    phone = StringField("Phone", validators=[DataRequired(), phone_ok, Length(max=40)])
    password = PasswordField("Password", validators=[DataRequired(), strong_password, Length(max=200)])
    confirm = PasswordField("Confirm password", validators=[DataRequired(), EqualTo("password", "Passwords must match.")])
    sms_opt_in = BooleanField("Send me SMS delivery alerts")
    accept = BooleanField("I agree to the Terms of Service and Privacy Policy", validators=[DataRequired(message="You must accept to continue.")])
    submit = SubmitField("Create account")


class ForgotForm(FlaskForm):
    email = EmailField("Email", validators=[DataRequired(), Email(), Length(max=160)], filters=[lambda x: x.strip().lower() if x else x])
    submit = SubmitField("Send reset link")


class ResetForm(FlaskForm):
    password = PasswordField("New password", validators=[DataRequired(), strong_password, Length(max=200)])
    confirm = PasswordField("Confirm new password", validators=[DataRequired(), EqualTo("password", "Passwords must match.")])
    submit = SubmitField("Set new password")


class ChangePasswordForm(FlaskForm):
    current = PasswordField("Current password", validators=[DataRequired()])
    password = PasswordField("New password", validators=[DataRequired(), strong_password, Length(max=200)])
    confirm = PasswordField("Confirm new password", validators=[DataRequired(), EqualTo("password", "Passwords must match.")])
    submit = SubmitField("Change password")


class ProfileForm(FlaskForm):
    name = StringField("Full name", validators=[DataRequired(), Length(min=2, max=120)])
    phone = StringField("Phone", validators=[DataRequired(), phone_ok, Length(max=40)])
    sms_opt_in = BooleanField("Send me SMS delivery alerts")
    submit = SubmitField("Save profile")


class QuoteForm(FlaskForm):
    country = SelectField("Destination country", choices=COUNTRY_CHOICES, validators=[DataRequired(message="Choose a destination.")])
    weight_kg = FloatField("Weight (kg)", validators=[InputRequired(), NumberRange(min=0.01, max=MAX_WEIGHT_KG)])
    length_cm = FloatField("Length (cm)", validators=[Optional(), NumberRange(min=1, max=150)])
    width_cm = FloatField("Width (cm)", validators=[Optional(), NumberRange(min=1, max=150)])
    height_cm = FloatField("Height (cm)", validators=[Optional(), NumberRange(min=1, max=150)])
    service = SelectField("Service level", choices=SERVICE_CHOICES, default="standard")
    insure = BooleanField("Add insurance")
    declared_value = DecimalField("Declared value", validators=[Optional(), NumberRange(min=0, max=MAX_DECLARED_VALUE)], places=2)
    submit = SubmitField("Get quote")


class BookingForm(QuoteForm):
    sender_name = StringField("Sender name", validators=[DataRequired(), Length(max=120)])
    sender_phone = StringField("Sender phone", validators=[DataRequired(), phone_ok, Length(max=40)])
    origin_city = StringField("Pickup city", validators=[DataRequired(), Length(max=100)], default="Moscow")
    sender_address = StringField("Pickup address", validators=[DataRequired(), Length(max=255)])
    pickup_date = DateField("Preferred pickup date", validators=[Optional()])
    recipient_name = StringField("Recipient name", validators=[DataRequired(), Length(max=120)])
    recipient_phone = StringField("Recipient phone", validators=[DataRequired(), phone_ok, Length(max=40)])
    recipient_email = EmailField("Recipient email (optional)", validators=[Optional(), Email(), Length(max=160)])
    destination_city = StringField("Destination city", validators=[DataRequired(), Length(max=100)])
    destination_postcode = StringField("Postcode / ZIP", validators=[Optional(), Length(max=20)])
    destination_address = StringField("Delivery address", validators=[DataRequired(), Length(max=255)])
    parcel_description = StringField("Contents description", validators=[DataRequired(), Length(min=3, max=255)])
    notes = TextAreaField("Delivery notes (optional)", validators=[Optional(), Length(max=1000)])
    accept_terms = BooleanField("I agree to the Terms of Service and Shipping Policy", validators=[DataRequired(message="Required.")])
    accept_prohibited = BooleanField("I confirm the parcel contains no prohibited or restricted items", validators=[DataRequired(message="Required.")])
    submit = SubmitField("Book shipment")


class TicketForm(FlaskForm):
    subject = StringField("Subject", validators=[DataRequired(), Length(min=4, max=160)])
    category = SelectField("Topic", choices=[("general", "General question"), ("shipment", "A shipment"),
                                             ("payment", "Payment / billing"), ("claim", "Damage / loss claim"),
                                             ("account", "My account")])
    tracking_number = StringField("Tracking number (optional)", validators=[Optional(), Length(max=24)])
    body = TextAreaField("Message", validators=[DataRequired(), Length(min=10, max=4000)])
    submit = SubmitField("Open ticket")


class ContactForm(FlaskForm):
    name = StringField("Your name", validators=[DataRequired(), Length(min=2, max=120)])
    email = EmailField("Email", validators=[DataRequired(), Email(), Length(max=160)])
    subject = StringField("Subject", validators=[DataRequired(), Length(min=4, max=160)])
    body = TextAreaField("Message", validators=[DataRequired(), Length(min=10, max=4000)])
    website = StringField("Website")  # honeypot -- must stay empty
    submit = SubmitField("Send message")


class ReplyForm(FlaskForm):
    body = TextAreaField("Reply", validators=[DataRequired(), Length(min=2, max=4000)])
    close = BooleanField("Mark as closed")
    submit = SubmitField("Send reply")


class StatusForm(FlaskForm):
    status = SelectField("New status", choices=[(s, s) for s in ALL_STATUSES])
    location = StringField("Location", validators=[Optional(), Length(max=160)])
    details = TextAreaField("Details (visible to customer)", validators=[Optional(), Length(max=500)])
    received_by = StringField("Received by (for delivery)", validators=[Optional(), Length(max=120)])
    submit = SubmitField("Update status")


class AssignForm(FlaskForm):
    rider_id = SelectField("Rider", coerce=int)
    submit = SubmitField("Assign")


class PaymentAdjustForm(FlaskForm):
    new_status = SelectField("Adjust payment status to", choices=[("Unpaid", "Unpaid"), ("Refunded", "Refunded (recorded after refund was issued in Paystack)")])
    reason = TextAreaField("Reason (required, stored in audit log)", validators=[DataRequired(), Length(min=10, max=500)])
    submit = SubmitField("Record adjustment")


class ReverifyForm(FlaskForm):
    reference = StringField("Paystack reference", validators=[DataRequired(), Length(min=6, max=64)])
    submit = SubmitField("Verify with Paystack")


class StaffForm(FlaskForm):
    name = StringField("Full name", validators=[DataRequired(), Length(min=2, max=120)])
    email = EmailField("Email", validators=[DataRequired(), Email(), Length(max=160)], filters=[lambda x: x.strip().lower() if x else x])
    phone = StringField("Phone", validators=[Optional(), phone_ok, Length(max=40)])
    role = SelectField("Role", choices=[("rider", "Rider"), ("dispatcher", "Dispatcher"), ("admin", "Administrator")])
    password = PasswordField("Temporary password", validators=[DataRequired(), strong_password, Length(max=200)])
    submit = SubmitField("Create staff account")
