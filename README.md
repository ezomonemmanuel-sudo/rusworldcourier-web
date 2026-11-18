# RusWorld Courier - international courier platform (Flask)

*Delivering Beyond Borders.* A production-oriented courier website + operations system for shipping from Russia worldwide:
marketing site, instant quote calculator, customer booking, Paystack payment of the **sender's shipping fee**, public tracking,
customer / dispatcher / rider / admin dashboards, support tickets, audit log and honest email/SMS hooks.

Brand: navy `#102b52`, orange `#f07835`, slate `#58677d`; Manrope (headings) + DM Sans (body), self-hosted in `rwc/static/fonts`.

## What's included
| Area | Details |
|---|---|
| Marketing | Home (hero + tracking box, services, how it works, coverage, **clearly-marked example** testimonials, FAQ, CTA), Services, About, FAQ, Contact, SEO meta/OpenGraph/JSON-LD, `sitemap.xml`, `robots.txt`, favicon (SVG/ICO/Apple), 404/403/429/500 pages |
| Quote | Weight, dimensions → volumetric weight (L×W×H ÷ 5000), 8 zones / ~110 destination countries, Express/Standard/Economy multipliers, fuel surcharge, optional insurance, ETA. Live JSON API (`/api/quote`) + works without JS |
| Booking | Logged-in customers, server-side price (client price is ignored), unpredictable `RWC-XXXX-XXXX-XXXX` numbers (60 bits, `secrets`), terms + prohibited-items confirmation |
| Tracking | Public page with progress stepper + event timeline; private data (addresses, full recipient name) hidden; international note about duties/taxes |
| Payments | Paystack only, for the sender's shipping fee: server-side initialize → callback **verify** → HMAC-SHA512 **webhook signature check**; amount/currency/reference must match; idempotent. Keys from env |
| Documents | Printable shipping label (QR → tracking page) and payment receipt (only after verified payment) |
| Customer | Dashboard (stats, search, pagination), shipment detail, cancel-before-pay, support tickets with thread, profile/password |
| Staff | Dispatcher/admin dashboard (stats, 7-day chart, revenue of *verified* payments, filters + search + pagination, **CSV export** that neutralises spreadsheet formula injection), rider assignment, status updates, payment re-verify, audited manual adjustments, support inbox, staff management (create/role/deactivate), audit log viewer, notification outbox |
| Rider | Own assigned deliveries only; limited status set; "Delivered" requires receiver name |
| Security | CSRF on every form (Flask-WTF) and on the JSON quote API, Flask-Limiter on login/register/forgot/track/contact/quote/pay/webhook, account lockout, single-use expiring reset tokens, session invalidation on password change, secure cookies, CSP + HSTS + X-Frame-Options + nosniff + Referrer/Permissions-Policy, honeypot on contact form, audit log, open-redirect protection, IDOR-safe 404s |
| Notifications | SMTP email and (optional) Twilio SMS hooks. **Nothing is reported "sent" unless the provider accepted it**; otherwise recorded as `not_sent` / `failed` in *Staff → Outbox* and logged |
| Ops | Alembic migrations (Flask-Migrate), Dockerfile, docker-compose (Postgres + Redis), Procfile, gunicorn config, pytest suite (73 tests), HTTP smoke script, screenshot script |

## Deliberately NOT built (anti-scam guardrails)
This platform has **no** customs duty / clearance / release fee for recipients, **no** "held - pay to release" status, banner, invoice or payment flow,
**no** invoice generator for customs charges and **no** crypto / gift-card payments. Specifically:
* The status list (`rwc/services/tracking.py`) contains no hold/payment-gated states, and a test asserts that.
* All staff/rider free text (status details, locations, ticket replies) is checked by a server-side guard that rejects wording such as "release fee", "held at customs",
  "customs duty/fee", "clearance fee", crypto, gift cards, bank/wire transfer. Blocked attempts are written to the audit log.
* International tracking pages carry: *"Import duties and taxes are assessed and collected only by the destination customs authority or its official delivery carrier.
  RusWorld Courier will never ask recipients to pay release fees via this site, bank transfer, crypto or gift cards."*
* **Payment can never be set to "Paid" manually.** `Paid` is only set by `rwc/services/payments.py` from a verified Paystack result (callback verify, signed webhook, or staff
  "verify reference" which re-checks with Paystack for that exact shipment/amount). Staff can only record `Unpaid` or `Refunded` with a written reason; every adjustment and every
  blocked attempt is in the audit log.

## Run locally
Requires Python 3.10+ (tested on 3.13).
```bash
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                      # edit: SECRET_KEY, company details, Paystack test key...
flask --app app init-db                                   # applies migrations
ADMIN_EMAIL=you@yourdomain.com ADMIN_PASSWORD='a-long-passphrase-1' flask --app app create-admin   # non-interactive
flask --app app run                                       # http://127.0.0.1:5000
```
Optional demo data (never in production): `flask --app app seed-demo` (demo customer/rider/dispatcher, password `DemoPass12345`, and 4 sample shipments - the "Paid" demo
shipments carry clearly labelled `DEMO-` payment records).

### Tests
```bash
pip install -r requirements-dev.txt
pytest -q                                   # 73 tests
python scripts/smoke_http.py http://127.0.0.1:5000 admin@example.com 'your-admin-password'   # end-to-end against a running server
python scripts/screenshots.py http://127.0.0.1:5000 RWC-XXXX-XXXX-XXXX screenshots           # needs playwright + Chrome
```

## Configure before launch (checklist)
1. **Tariff:** `rwc/tariff.py` ships an *example* tariff (zones, per-kg rates, multipliers, insurance rate, limits). Replace with your approved rates. Destination list excludes sanction-restricted countries; review it with your compliance advisers.
2. **Company details:** `COMPANY_*` env vars (email, phone, address, registration no.) - until set, pages say "not configured" rather than inventing details.
3. **Currency & Paystack:** `CURRENCY` must be enabled on your Paystack account (earlier prototype used NGN; default here is USD for a Russia-origin business). Put `PAYSTACK_SECRET_KEY` in env (use `sk_test_...` first). In the Paystack dashboard set the webhook URL to `https://YOUR-DOMAIN/webhooks/paystack` and test a payment end-to-end with a test card before going live. *(This build has been verified against mocked Paystack responses and signature maths only - no live Paystack account was available.)*
4. **Email/SMS:** set `SMTP_*` (and optionally `TWILIO_*`), then check *Staff → Outbox* shows `sent`. Password-reset emails require SMTP; without it the link is only written to the server log in non-production (never in production).
5. **Policies & testimonials:** legal pages are templates - have a lawyer review them. Replace the example testimonials (marked "Example") with real ones or delete the section.
6. **Staff accounts:** create dispatchers/riders under *Staff & users*. Consider adding MFA/IP allow-listing for admin (not included).
7. **Backups & monitoring:** use managed PostgreSQL with backups; ship logs; monitor `/` and the Paystack webhook.

## Deploy
* **Docker:** `docker compose up --build` (see `docker-compose.yml`; create `.env` first). Put it behind HTTPS (Caddy / nginx / your platform) and set `TRUSTED_PROXY_HOPS=1`, `SITE_URL=https://...`, `APP_ENV=production`.
* **Render / Railway / Fly / Heroku-style:** build `pip install -r requirements.txt`, release `flask --app app init-db`, start `gunicorn app:app --config gunicorn.conf.py` (see `Procfile`). Provision Postgres (`DATABASE_URL`) and Redis (`RATELIMIT_STORAGE_URI`) - in-memory rate limiting is per-process.
* **VPS:** `gunicorn` under systemd + nginx reverse proxy + certbot.
* In `APP_ENV=production` the app refuses to start without `SECRET_KEY`, enables `Secure` cookies and HSTS.

## Project layout
```
app.py, wsgi.py             entry points
rwc/__init__.py             app factory, security headers, error pages
rwc/config.py               env-driven configuration
rwc/models.py               User, Shipment, TrackingEvent, Payment, Ticket(+Message), AuditLog, Notification
rwc/tariff.py               zones, countries, pricing engine (Decimal)
rwc/services/               tracking (status list, numbers, content guard), payments, paystack, notify, audit
rwc/blueprints/             public, auth, customer, payments, staff
rwc/templates, rwc/static   Jinja templates, CSS/JS (no inline scripts - strict CSP), fonts, logo, favicons
migrations/                 Alembic migrations     tests/  pytest suite     scripts/  smoke + screenshots
```

## Known limits / next steps
* Single currency per deployment; example tariff; no MFA; no proof-of-delivery photo/signature upload; no live GPS/map; SMS path (Twilio) is implemented but untested against Twilio.
* Staff-entered text is guard-checked by keyword patterns - a determined insider can still phrase things differently, so keep staff access tight and review the audit log.

## Vercel notes
`vercel.json` + `api/index.py` run the app on Vercel's Python runtime; `public/static` is a copy of `rwc/static` so assets are served by the CDN (re-copy after changing static files). Needs a hosted Postgres `DATABASE_URL` (Neon free plan via the Vercel Marketplace works) - in production the app refuses to start with SQLite unless `ALLOW_SQLITE_IN_PRODUCTION=1` (testing only). Run migrations from any machine: `DATABASE_URL=... flask --app app init-db`.


## Render (free plan) deployment notes

* `render.yaml` describes the free web service + free Postgres + free Key Value (Redis) used for the live site. Secrets (`SECRET_KEY`, admin password) are **not** in the repo - set them in the Render dashboard.
* Binary assets (fonts, favicons, OG image) are stored base64-encoded in `scripts/binary_assets.json` so the git repo is text-only; the build command runs `python scripts/restore_assets.py` to restore them.
* Free plan has no pre-deploy hook, so the start command runs `flask --app app init-db` (idempotent migrations) before gunicorn.
* Create the first admin with `flask --app app create-admin` (env `ADMIN_EMAIL`, `ADMIN_PASSWORD`) from any shell that has `DATABASE_URL` set (e.g. your laptop using the database's external URL).
* Free-tier caveats: the web service sleeps after ~15 min idle (cold start ~30-60 s); free Postgres expires 30 days after creation unless upgraded; free Key Value is not persistent. Rate limiting falls back to per-process counters if Redis is unreachable.
* `/healthz` returns `{"status":"ok","database":"ok"}` when the DB is reachable.
