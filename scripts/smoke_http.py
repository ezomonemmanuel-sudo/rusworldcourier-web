#!/usr/bin/env python
"""End-to-end HTTP smoke test against a RUNNING server (default http://127.0.0.1:5000).

Usage: python scripts/smoke_http.py [base_url] [admin_email] [admin_password]
Creates a throw-away customer account, books a shipment and checks public/customer/admin routes.
"""
import re
import sys
import uuid

import requests

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5000").rstrip("/")
ADMIN = (sys.argv[2] if len(sys.argv) > 2 else "admin@example.com", sys.argv[3] if len(sys.argv) > 3 else "ChangeMe-Admin123")
results = []


def check(name, cond, extra=""):
    results.append(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")


def token(html):
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def form_post(s, url, data, page=None):
    t = token(s.get(BASE + (page or url)).text)
    return s.post(BASE + url, data={**data, "csrf_token": t}, allow_redirects=False)


anon = requests.Session()
for path in ["/", "/services", "/about", "/faq", "/contact", "/quote", "/track", "/auth/login", "/auth/register",
             "/legal/privacy", "/legal/terms", "/legal/prohibited-items", "/legal/claims", "/legal/shipping-policy",
             "/robots.txt", "/sitemap.xml", "/favicon.ico"]:
    check(f"GET {path}", anon.get(BASE + path).status_code == 200)
check("GET /nope -> 404 page", anon.get(BASE + "/nope").status_code == 404)
check("POST without CSRF rejected", anon.post(BASE + "/auth/login", data={"email": "a@b.co", "password": "x"}).status_code == 400)
check("/dashboard requires login", anon.get(BASE + "/dashboard", allow_redirects=False).status_code == 302)
check("/staff requires login", anon.get(BASE + "/staff/", allow_redirects=False).status_code == 302)

# quote API
t = token(anon.get(BASE + "/quote").text)
r = anon.post(BASE + "/api/quote", json={"country": "DE", "weight_kg": "2.5", "length_cm": "40", "width_cm": "30", "height_cm": "20", "service": "express", "insure": True, "declared_value": "200"}, headers={"X-CSRFToken": t})
check("quote API", r.status_code == 200 and r.json()["ok"], r.text[:140])

# register + login + book
s = requests.Session()
email = f"smoke-{uuid.uuid4().hex[:8]}@example.com"
r = form_post(s, "/auth/register", {"name": "Smoke Tester", "email": email, "phone": "+7 495 111 2233", "password": "Smoke-Test-12345", "confirm": "Smoke-Test-12345", "accept": "y"})
check("register -> dashboard", r.status_code == 302 and r.headers["Location"].endswith("/dashboard"))
check("dashboard", s.get(BASE + "/dashboard").status_code == 200)
r = form_post(s, "/book", {"country": "NG", "service": "standard", "weight_kg": "3", "length_cm": "30", "width_cm": "20", "height_cm": "20", "declared_value": "0",
                           "sender_name": "Smoke Tester", "sender_phone": "+7 495 111 2233", "origin_city": "Moscow", "sender_address": "Tverskaya 1",
                           "recipient_name": "Ada Receiver", "recipient_phone": "+234 800 000 0000", "destination_city": "Lagos", "destination_address": "1 Marina",
                           "parcel_description": "Books", "accept_terms": "y", "accept_prohibited": "y"})
check("book -> shipment page", r.status_code == 302 and "/shipments/" in r.headers["Location"], r.headers.get("Location", ""))
page = s.get(BASE + r.headers["Location"]).text
tn = re.search(r"RWC-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}", page).group(0)
check("tracking number issued", bool(tn), tn)
tp = anon.get(f"{BASE}/track/{tn}")
check("public tracking page + timeline", tp.status_code == 200 and "timeline" in tp.text and "Import duties and taxes are assessed and collected only by the destination customs authority" in tp.text)
check("unknown tracking -> 404", anon.get(BASE + "/track/RWC-ZZZZ-ZZZZ-ZZZZ").status_code == 404)
check("label locked until paid", s.get(BASE + re.search(r"/shipments/\d+", page).group(0) + "/label", allow_redirects=False).status_code == 302)
check("customer blocked from admin", s.get(BASE + "/staff/").status_code == 403)

# admin
a = requests.Session()
r = form_post(a, "/auth/login", {"email": ADMIN[0], "password": ADMIN[1]})
check("admin login", r.status_code == 302, r.headers.get("Location", ""))
check("admin dashboard", a.get(BASE + "/staff/").status_code == 200)
check("admin search", tn in a.get(BASE + "/staff/?q=" + tn[:8]).text)
c = a.get(BASE + "/staff/export.csv")
check("CSV export", c.status_code == 200 and "tracking_number" in c.text and tn in c.text)
for p in ("/staff/users", "/staff/audit", "/staff/outbox", "/staff/tickets"):
    check(f"admin {p}", a.get(BASE + p).status_code == 200)

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
