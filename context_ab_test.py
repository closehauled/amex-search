"""A/B diagnostic: which stealth-context factor triggers Amex's no_apply_cta.

Runs the scanner's REAL open_offer_page path (same selectors, same waits,
same click logic) under 4 context variants, 2 reps each, over one VPN exit:

  A_hunt_exact : init script ON,  UA spoof ON   (reproduces the hunt)
  B_noinit_ua  : init script OFF, UA spoof ON
  C_init_noua  : init script ON,  UA spoof OFF
  D_plain      : init script OFF, UA spoof OFF  (closest to the passing probe)

All variants share the hunt's other config: headed, 1920x1080, en-US locale,
Denver timezone, --disable-blink-features=AutomationControlled launch flag.
PASS = reached the messageId apply page. On FAIL, logs button count /
visibility / final URL / navigator.webdriver value for forensics.
"""
import sys
import time

import os
sys.path.insert(0, os.environ.get("AMEX_SCANNER_DIR",
                                  os.path.dirname(os.path.abspath(__file__))))
import amex_scanner as A
from playwright.sync_api import sync_playwright

ENTRY = {"card": "business_gold", "target": 200000, "method": "direct",
         "url": A.PRODUCT_URLS["business_gold"], "query": None,
         "ref_code": None}
UA = A.FINGERPRINTS[0]["ua"]  # Chrome/148 Windows, the hunt's most common draw

VARIANTS = [
    ("A_hunt_exact", True, True),
    ("B_noinit_ua", False, True),
    ("C_init_noua", True, False),
    ("D_plain", False, False),
]


def make_ctx(browser, init_script, ua_spoof):
    kw = dict(viewport={"width": 1920, "height": 1080}, locale="en-US",
              timezone_id="America/Denver")
    if ua_spoof:
        kw["user_agent"] = UA
    ctx = browser.new_context(**kw)
    if init_script:
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    return ctx


def forensics(page):
    bits = []
    try:
        els = page.query_selector_all("button:has-text('Apply')")
        bits.append(f"applybtns={len(els)}")
        bits.append(f"visible={any(e.is_visible() for e in els)}")
    except Exception as e:
        bits.append(f"btncheck_err={type(e).__name__}")
    try:
        bits.append(f"webdriver={page.evaluate('() => navigator.webdriver')!r}")
    except Exception:
        bits.append("webdriver=?")
    try:
        bits.append(f"url={page.url[:80]}")
    except Exception:
        pass
    return " ".join(bits)


def main():
    with sync_playwright() as pw:
        browser = A.build_browser(pw, headless=False)
        for rep in range(2):
            for name, init_s, ua_s in VARIANTS:
                ctx = make_ctx(browser, init_s, ua_s)
                page = ctx.new_page()
                cdp = ctx.new_cdp_session(page)
                rec = A.new_record(ENTRY, "Denver", None)
                t0 = time.time()
                try:
                    A.open_offer_page(page, cdp, ENTRY, rec)
                    out = f"PASS {rec['apply_url_final'][:90]}"
                except Exception as e:
                    out = (f"FAIL {type(e).__name__}: {e} | "
                           + forensics(page))
                dur = round(time.time() - t0, 1)
                print(f"{name} rep{rep} [{dur}s] {out}", flush=True)
                try:
                    ctx.close()
                except Exception:
                    pass
                time.sleep(25)
        browser.close()
    print("== AB DONE ==", flush=True)


if __name__ == "__main__":
    main()
