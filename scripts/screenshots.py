#!/usr/bin/env python
"""Take screenshots of key pages with headless Chrome (needs: pip install playwright, and Chrome/Chromium).
Usage: python scripts/screenshots.py BASE_URL TRACKING_NUMBER [out_dir]"""
import sys
from playwright.sync_api import sync_playwright

base, tn = sys.argv[1].rstrip("/"), sys.argv[2]
out = sys.argv[3] if len(sys.argv) > 3 else "screenshots"
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1366, "height": 900})
    def shot(url, name, full=True, vp=None):
        if vp: pg.set_viewport_size(vp)
        pg.goto(base + url, wait_until="networkidle")
        pg.screenshot(path=f"{out}/{name}.png", full_page=full)
        print("saved", name)
    shot("/", "01-home")
    pg.goto(base + "/", wait_until="networkidle"); pg.screenshot(path=f"{out}/01b-home-hero.png"); print("saved 01b-home-hero")
    shot("/quote?country=DE&weight_kg=2.5&service=express", "02-quote")
    shot(f"/track/{tn}", "03-tracking")
    shot("/services", "04-services")
    shot("/", "05-home-mobile", vp={"width": 390, "height": 844})
    shot(f"/track/{tn}", "06-tracking-mobile")
    pg.set_viewport_size({"width": 1366, "height": 900})
    # live quote interaction
    pg.goto(base + "/quote", wait_until="networkidle")
    pg.select_option("select[name=country]", "NG"); pg.fill("input[name=weight_kg]", "4"); pg.fill("input[name=length_cm]", "50"); pg.fill("input[name=width_cm]", "40"); pg.fill("input[name=height_cm]", "30")
    pg.wait_for_selector("[data-q-result]:not([hidden])"); pg.screenshot(path=f"{out}/07-quote-live-js.png", full_page=True); print("saved 07-quote-live-js")
    # admin dashboard
    pg.goto(base + "/auth/login"); pg.fill("input[name=email]", "admin@example.com"); pg.fill("input[name=password]", "ChangeMe-Admin123"); pg.click("button[type=submit].btn-primary:has-text('Sign in')")
    pg.wait_for_url("**/staff/"); shot("/staff/", "08-admin-dashboard")
    shot("/staff/shipments/1", "09-admin-shipment")
    b.close()
