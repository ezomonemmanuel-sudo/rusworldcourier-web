#!/usr/bin/env python
"""Restore binary assets (fonts, favicons, OG image) that are stored base64-encoded in scripts/binary_assets.json.
Used by the Render build step so the git repo contains text only. Safe to run repeatedly."""
import base64
import json
import os

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(root, "scripts", "binary_assets.json")) as f:
    assets = json.load(f)
for rel, b64 in assets.items():
    dest = os.path.join(root, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as out:
        out.write(base64.b64decode(b64))
    print("restored", rel)
