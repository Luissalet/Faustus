"""Capture the Studio shell at the three supported viewports.

The baseline in docs/ui/baseline/ photographed the OLD interface. This is its
counterpart: the same journey, the same sizes, so "the new one is better" is
something you can look at instead of something someone claims.

    ODYSSEUS_STUDIO_URL=http://127.0.0.1:7001 venv\\Scripts\\python.exe scripts/shot_studio.py

Point it at a running instance; it does not start one.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("ODYSSEUS_STUDIO_URL", "http://127.0.0.1:7001")
OUT = Path(
    os.environ.get("ODYSSEUS_STUDIO_OUT")
    or Path(__file__).resolve().parents[1] / "docs" / "ui" / "after"
)

VIEWPORTS = {
    "desktop": (1400, 900),
    "tablet": (1024, 768),
    "mobile": (390, 844),
}

SCREENS = {
    "01_inicio": "/?shell=studio",
    "02_activity_deep_link": "/activity",
    "03_gallery": "/?gallery=1",
    "04_projects": "/projects",
    "05_project_context": "/projects/100d012d1ff3?tab=contexto",
    "06_automations": "/automations",
    "07_library": "/library",
    "08_studio_empty": "/studio",
    # A real session: ODYSSEUS_STUDIO_SESSION picks it, else the newest.
    "09_studio_session": "/studio?s=__SESSION__",
}


def newest_session(page) -> str:
    wanted = os.environ.get("ODYSSEUS_STUDIO_SESSION")
    if wanted:
        return wanted
    sessions = page.evaluate("fetch('/api/sessions').then(r => r.json())")
    ranked = sorted(sessions, key=lambda s: s.get("last_message_at") or "", reverse=True)
    return ranked[0]["id"] if ranked else ""


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, (width, height) in VIEWPORTS.items():
            # Headless Chromium reports light; the product's default and the
            # theme every manual pass saw is dark, so the evidence matches it.
            context = browser.new_context(
                viewport={"width": width, "height": height},
                color_scheme=os.environ.get("ODYSSEUS_STUDIO_SCHEME", "dark"),
            )
            page = context.new_page()
            # The flag lives in localStorage, so the first URL must set it.
            # Not networkidle: Faustus holds long-lived connections open, so
            # the page is never "idle" and every goto times out at 30s.
            page.goto(f"{BASE}/?shell=studio", wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            session = newest_session(page)
            for screen, path in SCREENS.items():
                path = path.replace("__SESSION__", session)
                page.goto(f"{BASE}{path}", wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
                target = OUT / f"{screen}_{name}.png"
                page.screenshot(path=str(target), full_page=True)
                print(f"wrote {target.relative_to(OUT.parents[2])}")
            context.close()
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
