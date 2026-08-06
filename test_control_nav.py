"""Headless test of the control-mode navigation detection + re-expose pipeline.

Drives navigations programmatically, including an OUT-OF-BAND CDP navigation
that reproduces the page.url staleness the headed test surfaced (a user-driven
Back/forward that Playwright's page.url did not follow). Verifies that
Session.current_url() (now CDP location.href) tracks the live document, that a
new application session is detected by messageId change, and that
reexpose_session re-reads the offer. Submits NOTHING (only loads apply forms,
same as the hunt)."""
import time

import amex_scanner as S
from playwright.sync_api import sync_playwright


def main():
    pw = sync_playwright().start()
    browser = S.build_browser(pw, headless=True)
    try:
        # 1. Spawn a fresh platinum session (apply page A).
        sess = S.spawn_session(browser, 0, "business_platinum", headless=True)
        assert not sess.failed, f"spawn failed: {sess.error}"
        msgA = sess.last_messageId
        urlA = sess.current_url()
        ptsA = (sess.rec or {}).get("exposed_offer_points")
        print(f"[A] spawned msgId={msgA} pts={ptsA}")
        assert msgA and ptsA, "session A not exposed"
        assert S.extract_message_id(urlA) == msgA

        # 2. Out-of-band navigation via CDP (mimics the user clicking Back outside
        #    Playwright's page API, which is what made page.url go stale).
        prod = S.PRODUCT_URLS["business_platinum"]
        sess.cdp.send("Page.navigate", {"url": prod})
        time.sleep(6)
        live = sess.current_url()       # CDP location.href -> should be fresh
        pw_url = sess.page.url          # Playwright -> may lag
        print(f"[B] out-of-band nav -> current_url(CDP)={live[:62]}")
        print(f"    page.url(Playwright)        ={pw_url[:62]}")
        assert "american-express-business-platinum" in live, \
            f"CDP url did not follow out-of-band nav: {live}"
        assert S.extract_message_id(live) is None, \
            "product page should carry no messageId"
        print(f"    page.url stale vs live CDP url: {pw_url != live}")

        # 3. Re-initiate Apply -> a NEW application session (messageId B).
        rec2 = {}
        S.open_offer_page(sess.page, sess.cdp,
                          S.control_entry("business_platinum"), rec2)
        urlB = sess.current_url()
        msgB = S.extract_message_id(urlB)
        print(f"[C] re-applied msgId={msgB}")
        assert msgB and msgB != msgA, \
            f"expected a NEW messageId, got {msgB} (was {msgA})"

        # 4. The detection condition the holder's poll uses must now fire.
        assert msgB != sess.last_messageId, \
            "poll would not detect the new session (messageId unchanged)"

        # 5. reexpose_session re-reads the live offer and updates state.
        ok = S.reexpose_session(sess)
        assert ok, "reexpose_session failed"
        ptsB = (sess.rec or {}).get("exposed_offer_points")
        print(f"[D] reexposed msgId={sess.last_messageId} pts={ptsB} "
              f"method={sess.rec.get('exposure_method')}")
        assert sess.last_messageId == msgB, \
            "last_messageId not updated to the new session"
        assert ptsB, "no offer re-exposed for session B"
        print("CONTROL NAV TEST PASS")
    finally:
        try:
            browser.close()
        finally:
            pw.stop()


if __name__ == "__main__":
    main()
