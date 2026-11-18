import secrets

from flask import (Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)

from ..extensions import db, limiter
from ..forms import ContactForm, QuoteForm
from ..models import Shipment, Ticket, TicketMessage
from ..services import notify
from ..services.audit import audit
from ..services.tracking import normalize_tracking, stepper_state
from ..tariff import COUNTRIES, SERVICES, ZONES, calculate_quote, quote_to_json

bp = Blueprint("public", __name__)

LEGAL_PAGES = {
    "privacy": "Privacy Policy", "terms": "Terms of Service", "prohibited-items": "Prohibited & Restricted Items",
    "claims": "Claims Policy", "shipping-policy": "Shipping Policy",
}


def _run_quote(data):
    return calculate_quote(
        data.get("country"), data.get("weight_kg"), data.get("length_cm") or None, data.get("width_cm") or None,
        data.get("height_cm") or None, data.get("service", "standard"), bool(data.get("insure")),
        data.get("declared_value") or 0, current_app.config["FUEL_SURCHARGE_PCT"], current_app.config["CURRENCY"])


@bp.get("/")
def home():
    return render_template("index.html", zones=ZONES, services=SERVICES, n_countries=len(COUNTRIES))


@bp.get("/services")
def services():
    return render_template("services.html", services=SERVICES, zones=ZONES)


@bp.get("/about")
def about():
    return render_template("about.html")


@bp.get("/faq")
def faq():
    return render_template("faq.html")


@bp.route("/contact", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def contact():
    form = ContactForm()
    if form.validate_on_submit():
        if form.website.data:  # honeypot filled -> bot. Pretend success, store nothing.
            return redirect(url_for("public.contact_done", ref="T-0000000000"))
        ref = "T-" + secrets.token_hex(4).upper()
        t = Ticket(reference=ref, name=form.name.data.strip(), email=form.email.data.strip().lower(),
                   subject=form.subject.data.strip(), category="general")
        t.messages.append(TicketMessage(body=form.body.data.strip(), is_staff=False))
        db.session.add(t)
        audit("contact_message", "ticket", ref, f"from={t.email}", actor_label="public")
        cfg = current_app.config
        if cfg["COMPANY_EMAIL"]:
            notify.send_email(cfg["COMPANY_EMAIL"], f"[{ref}] {t.subject}",
                              f"New contact message from {t.name} <{t.email}>:\n\n{form.body.data.strip()}", "contact_staff")
        db.session.commit()
        return redirect(url_for("public.contact_done", ref=ref))
    return render_template("contact.html", form=form)


@bp.get("/contact/thanks")
def contact_done():
    return render_template("contact_done.html", ref=request.args.get("ref", "")[:16])


@bp.route("/quote", methods=["GET", "POST"])
@limiter.limit("60 per hour", methods=["POST"])
def quote():
    result = error = None
    if request.method == "POST":
        form = QuoteForm()
        if form.validate_on_submit():
            try:
                result = _run_quote(form.data)
            except ValueError as e:
                error = str(e)
    else:  # GET: shareable links / homepage teaser. Read-only, so no CSRF token needed.
        args = request.args
        form = QuoteForm(formdata=None, country=args.get("country", ""), service=args.get("service", "standard"))
        if args.get("weight_kg"):
            form.weight_kg.process_formdata([args["weight_kg"]])
        if args.get("country") and args.get("weight_kg"):
            try:
                result = _run_quote(args)
            except ValueError as e:
                error = str(e)
    return render_template("quote.html", form=form, result=result, error=error)


@bp.post("/api/quote")
@limiter.limit("120 per hour;20 per minute")
def api_quote():
    data = request.get_json(silent=True) or request.form.to_dict()
    try:
        return jsonify(ok=True, quote=quote_to_json(_run_quote(data)))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 422


@bp.get("/track")
@limiter.limit("30 per minute")
def track_search():
    raw = request.args.get("number", "").strip()
    if not raw:
        return render_template("track.html", shipment=None, searched=False)
    num = normalize_tracking(raw)
    if num:
        return redirect(url_for("public.track", number=num))
    return render_template("track.html", shipment=None, searched=True, query=raw[:40]), 404


def _mask(name):
    parts = (name or "").split()
    return " ".join(p[0] + "*" * (len(p) - 1) if i else p[:1] + "*" * max(len(p) - 1, 1) for i, p in enumerate(parts)) or "-"


@bp.get("/track/<number>")
@limiter.limit("30 per minute")
def track(number):
    num = normalize_tracking(number)
    shipment = Shipment.query.filter_by(tracking_number=num).first() if num else None
    if not shipment:
        return render_template("track.html", shipment=None, searched=True, query=number[:40]), 404
    events = list(reversed(shipment.events))
    return render_template("track.html", shipment=shipment, searched=True, events=events,
                           steps=stepper_state(shipment), masked_recipient=_mask(shipment.recipient_name))


@bp.get("/legal/<slug>")
def legal(slug):
    if slug not in LEGAL_PAGES:
        abort(404)
    return render_template(f"legal/{slug}.html", title=LEGAL_PAGES[slug])


@bp.get("/robots.txt")
def robots():
    body = ("User-agent: *\nAllow: /\nDisallow: /dashboard\nDisallow: /staff\nDisallow: /auth\nDisallow: /pay\n"
            "Disallow: /shipments\nDisallow: /support\nDisallow: /api/\n"
            f"Sitemap: {current_app.config['SITE_URL']}/sitemap.xml\n")
    return Response(body, mimetype="text/plain")


@bp.get("/sitemap.xml")
def sitemap():
    base = current_app.config["SITE_URL"]
    paths = ["/", "/services", "/about", "/quote", "/track", "/faq", "/contact"] + [f"/legal/{s}" for s in LEGAL_PAGES]
    xml = ['<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    xml += [f"<url><loc>{base}{p}</loc></url>" for p in paths]
    xml.append("</urlset>")
    return Response("".join(xml), mimetype="application/xml")


@bp.get("/healthz")
@limiter.exempt
def healthz():
    """Liveness + database check for load balancers / uptime monitors."""
    try:
        db.session.execute(db.text("SELECT 1"))
        return jsonify(status="ok", database="ok")
    except Exception:  # noqa: BLE001
        current_app.logger.exception("health check: database unreachable")
        return jsonify(status="degraded", database="unreachable"), 503


@bp.get("/favicon.ico")
def favicon():
    return send_from_directory(current_app.static_folder + "/img", "favicon.ico", mimetype="image/x-icon")
