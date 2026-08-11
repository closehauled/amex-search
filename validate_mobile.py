import json, sys
import amex_scanner as S
from playwright.sync_api import sync_playwright

target = sys.argv[1] if len(sys.argv) > 1 else "iPhone 14 Pro Max"
pw = sync_playwright().start()
browser = S.build_browser(pw, False)
profiles = S.experiment_profiles(pw)
prof = next((p for p in profiles if p["name"] == target), None)
if not prof:
    print("profile not found:", target, "have:", [p["name"] for p in profiles]); sys.exit(1)

ctx, meta = S.new_session_profile(browser, pw, prof, "America/Los_Angeles")
entry = {"card": "business_platinum", "target": 300000, "method": "direct",
         "url": S.PRODUCT_URLS["business_platinum"], "query": None,
         "ref_code": None, "dwell_s": 2}

class _S:
    hostname = "no-vpn"

rec, page = S.run_attempt(ctx, entry, "none", _S())
rec.update(meta)
try:
    page.screenshot(path="/tmp/mobile_test.png")
except Exception:
    pass
print(json.dumps({k: rec.get(k) for k in (
    "device", "is_mobile", "fp_viewport", "page_status", "as_high_as_detected",
    "exposed_offer_text", "exposed_offer_points", "error", "apply_url_final")}, indent=2))
ctx.close(); browser.close(); pw.stop()
