import json, subprocess, time
import amex_scanner as S
from playwright.sync_api import sync_playwright

# Assumes VPN already connected. Exposes a Platinum offer headless, saves the
# session, disconnects VPN, then reopens the apply URL in a FRESH context off-VPN
# to test whether the application session transfers.
pw = sync_playwright().start()
browser = S.build_browser(pw, headless=True)
ctx, meta = S.new_session(browser)
entry = {"card": "business_platinum", "target": 300000, "method": "direct",
         "url": S.PRODUCT_URLS["business_platinum"], "query": None,
         "ref_code": None, "dwell_s": 2}

class _S:
    hostname = "porttest"

rec, page = S.run_attempt(ctx, entry, "test", _S())
print("EXPOSE headless ->", rec.get("page_status"), "|", rec.get("exposed_offer_text"),
      "| err", rec.get("error"))
apply_url = rec.get("apply_url_final")
if not apply_url:
    print("RESULT: exposure failed headless; cannot test portability")
    raise SystemExit(1)

state = ctx.storage_state()
json.dump(state, open("/tmp/amex_session.json", "w"))
print("saved storage_state (cookies:", len(state.get("cookies", [])), ")")

# Disconnect VPN to simulate submitting off-VPN from a different machine/IP.
subprocess.run("nordvpn d", shell=True, capture_output=True)
time.sleep(4)

ctx2 = browser.new_context(storage_state="/tmp/amex_session.json",
                           viewport={"width": 1920, "height": 1080}, locale="en-US")
pg2 = ctx2.new_page()
cdp2 = ctx2.new_cdp_session(pg2)
pg2.goto(apply_url, wait_until="commit", timeout=40000)
pg2.wait_for_timeout(9000)
title = pg2.title()
has_offer = S.cdp_eval(cdp2, "(() => !!document.querySelector('.offer-section-content'))")
body = S.cdp_eval(cdp2, "(() => document.body ? document.body.innerText.slice(0,200) : '')") or ""
expired = "expired" in body.lower() or "expired" in title.lower()
print("RESUMED (fresh ctx, off-VPN) -> title:", title)
print("   offer_section_present:", has_offer, "| looks_expired:", expired)
print("   body head:", body.replace("\n", " ")[:160])
print("RESULT:", "PORTABLE (session transfers)" if (has_offer and not expired)
      else "NOT portable (session did not transfer)")
ctx.close(); ctx2.close(); browser.close(); pw.stop()
