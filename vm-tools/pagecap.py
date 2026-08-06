#!/usr/bin/env python3
"""Full-page screenshot of the current tab in a Chromium started with
`--remote-debugging-port` (see the `webapp` launcher). Saves a single raster PNG
to ~/Pictures covering the whole page beyond the viewport, so the formatting is
preserved exactly (unlike print-to-PDF or save-as-HTML). Non-disruptive: it does
not scroll the live page, so a filled-in form is untouched.
"""
import base64
import os
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PORT = os.environ.get("PAGECAP_PORT", "9222")
PICS = Path.home() / "Pictures"


def notify(title, msg):
    try:
        env = dict(os.environ)
        env.setdefault("DISPLAY", ":0")
        env.setdefault("DBUS_SESSION_BUS_ADDRESS",
                       f"unix:path=/run/user/{os.getuid()}/bus")
        subprocess.Popen(["notify-send", title, msg], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def pick_page(browser):
    """Prefer the focused tab; fall back to the most recently opened page."""
    pages = [p for c in browser.contexts for p in c.pages
             if not p.url.startswith(("devtools://", "chrome://"))]
    if not pages:
        return None
    for p in pages:
        try:
            if p.evaluate("document.hasFocus()"):
                return p
        except Exception:
            pass
    return pages[-1]


def main():
    PICS.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(f"http://localhost:{PORT}")
        except Exception as e:
            notify("pagecap", f"No debug browser on :{PORT}. Start it with `webapp`.")
            print(f"[ERROR] connect localhost:{PORT} failed: {e}", file=sys.stderr)
            sys.exit(1)
        page = pick_page(browser)
        if page is None:
            notify("pagecap", "No page to capture.")
            print("[ERROR] no capturable page", file=sys.stderr)
            sys.exit(1)
        cdp = page.context.new_cdp_session(page)
        res = cdp.send("Page.captureScreenshot",
                       {"format": "png", "captureBeyondViewport": True,
                        "fromSurface": True})
        n = 1
        while (PICS / f"pagecap-{n}.png").exists():
            n += 1
        out = PICS / f"pagecap-{n}.png"
        out.write_bytes(base64.b64decode(res["data"]))
        print(out)
        notify("pagecap", f"Full page saved: {out.name}")


if __name__ == "__main__":
    main()
