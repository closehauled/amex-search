#!/usr/bin/env python
# coding: utf-8
"""
Controlled A/B: Google entry vs referral-link entry for Business Platinum,
PAIRED on the same VPN server (same exit IP), to isolate the effect of entry
method on the exposed offer. Platinum only (no Gold referral links exist).

Usage: ab_test.py [n_servers]   (default 8). Writes to ab_test.jsonl.
"""
import random
import statistics as st
import sys
import time
from pathlib import Path

import amex_scanner as S
from playwright.sync_api import sync_playwright

N = int(sys.argv[1]) if len(sys.argv) > 1 else 8
S.LOG_PATH = Path(__file__).resolve().parent / "ab_test.jsonl"

CITIES = ["San_Francisco", "Los_Angeles", "Chicago", "Denver", "Dallas", "Atlanta"]

refs = [e for e in S.load_referral_entries() if e["card"] == "business_platinum"]
if not refs:
    print("[FATAL] no Platinum referral links in amex-referrals.txt")
    sys.exit(1)
print(f"[INFO] {len(refs)} platinum referral link(s); {N} server pairs")

df = S.get_server_list()
random.seed()
pairs = []
for i in range(N):
    city = CITIES[i % len(CITIES)]
    servers = S.find_servers_matching_city(df, city)
    if servers.empty:
        continue
    pairs.append((city, servers.iloc[random.randrange(len(servers))]))

pw = sync_playwright().start()
browser = S.build_browser(pw, False)
results = []
try:
    for i, (city, server) in enumerate(pairs):
        if not S.nordvpn_connect(server):
            continue
        time.sleep(S.SETTLE_AFTER_CONNECT_SEC)
        exit_ip = S.get_exit_ip()
        ref = refs[i % len(refs)]
        entries = [
            ("google", {"card": "business_platinum", "target": 300000,
                        "method": "google", "url": None,
                        "query": S.GOOGLE_QUERIES["business_platinum"],
                        "ref_code": None, "dwell_s": 4}),
            ("referral", {"card": "business_platinum", "target": 300000,
                          "method": "referral", "url": ref["url"], "query": None,
                          "ref_code": ref["ref_code"], "dwell_s": 4}),
        ]
        for label, entry in entries:
            ctx, fp = S.new_session(browser, city)
            rec, page = S.run_attempt(ctx, entry, city, server)
            rec.update(fp)
            rec.update({"ab": label, "exit_ip": exit_ip, "server_pair": i})
            S.log_line(rec)
            pts = rec.get("exposed_offer_points")
            print(f"  pair{i} {city[:11]:11} ip={exit_ip} {label:9} -> {pts}"
                  f"  {rec.get('error') or ''}")
            results.append((i, label, pts))
            ctx.close()
            time.sleep(2)
        S.nordvpn_disconnect_quiet()
        time.sleep(random.uniform(5, 12))
except KeyboardInterrupt:
    print("[INFO] interrupted")
finally:
    S.nordvpn_disconnect_quiet()
    try:
        browser.close()
    finally:
        pw.stop()

print("\n=== A/B SUMMARY (paired, same exit IP) ===")
byp = {}
for i, label, pts in results:
    byp.setdefault(i, {})[label] = pts
gvals, rvals = [], []
gw = rw = tie = 0
for i, d in sorted(byp.items()):
    g, r = d.get("google"), d.get("referral")
    if g is not None:
        gvals.append(g)
    if r is not None:
        rvals.append(r)
    if g is not None and r is not None:
        tag = "referral higher" if r > g else "google higher" if g > r else "tie"
        gw += g > r
        rw += r > g
        tie += g == r
        print(f"  pair{i}: google={g} referral={r} -> {tag}")
if gvals:
    print(f"google   mean={st.mean(gvals):,.0f} max={max(gvals):,} n={len(gvals)}")
if rvals:
    print(f"referral mean={st.mean(rvals):,.0f} max={max(rvals):,} n={len(rvals)}")
print(f"paired wins: referral_higher={rw} google_higher={gw} tie={tie}")
