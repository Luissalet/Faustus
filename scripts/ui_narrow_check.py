#!/usr/bin/env python3
"""ui_narrow_check.py — find what runs off the right edge of Studio at a
narrow width (a phone, a side-by-side window).

Opens every main screen, and every Settings section, in a headless browser
at the given width and lists the elements that end past the viewport's right
edge. Elements inside something that scrolls sideways on purpose (a tab bar,
a table, a code block) do not count. Every request that is not a GET is
answered locally with `{}`, so the run cannot change settings or data.

    python scripts/ui_narrow_check.py                       # 420 px, all screens
    python scripts/ui_narrow_check.py --width 360 --routes /studio,/settings
    python scripts/ui_narrow_check.py --css ".fs-x{min-inline-size:0}"   # try a fix first

Credentials come from FAUSTUS_USER / FAUSTUS_PASS. Exit code 1 when anything
overflows. Needs Playwright with its Chromium.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List

DEFAULT_ROUTES = (
    "/,/studio,/projects,/library,/automations,/activity,/calendar,/notes,/connectors,"
    "/whatsapp,/processes,/creator,/brain,/source-control,/email,/memory,/agents,/skills,"
    "/research,/compare,/group,/council,/state,/deltas,/alternatives,/completion,/cookbook,"
    "/context,/workflows,/settings"
)

# Elements past the right edge, minus the ones inside a sideways scroller
# or the decorative background layer. One line per element: "class<parent@right".
OVERFLOW_JS = """() => {
  const W = innerWidth, out = [];
  const root = document.querySelector('.fs-main') || document.body;
  const name = (x) => ((typeof x.className === 'string' && x.className) ? x.className.split(' ')[0] : x.tagName).slice(0, 32);
  for (const e of root.querySelectorAll('*')) {
    const r = e.getBoundingClientRect();
    if (!r.width || r.right <= W + 1 || r.left >= W) continue;
    if (e.closest('.fs-aurora')) continue;
    // Decoration placed past the edge on purpose (a watermark that bleeds out
    // of its header): absolutely placed and not clickable.
    const cs = getComputedStyle(e);
    if (cs.pointerEvents === 'none' && /(absolute|fixed)/.test(cs.position)) continue;
    // Inside a sideways scroller, or clipped on purpose by a box that itself
    // fits (a header cropping its watermark): not a layout bug.
    let a = e.parentElement, contained = false;
    while (a && a !== root) {
      const ox = getComputedStyle(a).overflowX;
      if (/(auto|scroll)/.test(ox) || (/(hidden|clip)/.test(ox) && a.getBoundingClientRect().right <= W + 1
          && !a.classList.contains('fs-main__inner') && !a.classList.contains('fs-route'))) { contained = true; break; }
      a = a.parentElement;
    }
    if (contained) continue;
    out.push(name(e) + '<' + (e.parentElement ? name(e.parentElement) : '') + '@' + Math.round(r.right));
  }
  return [...new Set(out)];
}"""


def parse_routes(text: str) -> List[str]:
    """Comma-separated routes, each starting with "/", duplicates dropped."""
    out: List[str] = []
    for part in (text or "").split(","):
        route = part.strip()
        if not route:
            continue
        if not route.startswith("/"):
            route = "/" + route
        if route not in out:
            out.append(route)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", default="http://127.0.0.1:" + os.environ.get("FAUSTUS_PORT", "7000"))
    ap.add_argument("--width", type=int, default=420)
    ap.add_argument("--routes", default=DEFAULT_ROUTES)
    ap.add_argument("--css", default="", help="extra CSS injected before measuring (to try a fix)")
    ap.add_argument("--wait-ms", type=int, default=3500)
    args = ap.parse_args(argv)

    from playwright.sync_api import sync_playwright

    base = args.base.rstrip("/")
    found: Dict[str, List[str]] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": args.width, "height": 900})
        # Read-only run: anything but a GET (and the login) is answered here.
        ctx.route("**/api/**", lambda r: r.fulfill(status=200, body="{}", content_type="application/json")
                  if r.request.method != "GET" and "/api/auth/login" not in r.request.url else r.continue_())
        page = ctx.new_page()
        page.goto(base + "/", wait_until="domcontentloaded")
        user = os.environ.get("FAUSTUS_USER", "")
        if user:
            page.request.post(base + "/api/auth/login",
                              data={"username": user, "password": os.environ.get("FAUSTUS_PASS", "")})

        def measure(label: str) -> None:
            if args.css:
                page.add_style_tag(content=args.css)
                page.wait_for_timeout(300)
            over = page.evaluate(OVERFLOW_JS)
            found[label] = over
            print(f"{label:<34} {len(over):>4}  {', '.join(over[:4])}", flush=True)

        for route in parse_routes(args.routes):
            try:
                page.goto(base + route, wait_until="domcontentloaded")
                page.wait_for_timeout(args.wait_ms)
                measure(route)
                if route == "/settings":
                    tabs = page.locator(".fs-set__nav-item")
                    for i in range(tabs.count()):
                        tab = tabs.nth(i)
                        label = tab.inner_text().strip().replace("\n", " ")[:24]
                        tab.click()
                        page.wait_for_timeout(1500)
                        measure(f"/settings › {label}")
            except Exception as exc:  # noqa: BLE001 - one screen's failure is a result
                found[route] = [f"error: {str(exc)[:120]}"]
                print(f"{route:<34}  ERR {str(exc)[:100]}", flush=True)
        browser.close()

    bad = {k: v for k, v in found.items() if v}
    print(f"\n{len(found) - len(bad)}/{len(found)} clean at {args.width} px")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
