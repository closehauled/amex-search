#!/usr/bin/env python
# coding: utf-8
"""
Amex elevated-offer scanner (v7).

Per attempt: connect a NordVPN city, open a fresh anonymous Playwright session,
reach the official Amex card page the way a person does (Google the card, follow
the first NON-sponsored organic americanexpress.com result), click "Apply" (the
offer only renders after Apply), then expose the real bonus behind the
"as high as" language by reading the Print Terms document directly (no browser
extension):

  1. wait for `.offer-section-content`
  2. if it says "as high as", click the Offer Terms trigger, grab the
     "Print Terms" link, flip isAhaVariant=true->false and
     showExactOffer=false->true on that URL
  3. read the real offer: same-origin print pages via a hidden iframe (#offer-terms),
     cross-origin print pages via Playwright's cookie-sharing request context
  4. parse "Earn N Membership Rewards Points"

This offer-exposing technique (the isAhaVariant/showExactOffer flip on the Print
Terms URL, read via a same-origin iframe or a cross-origin fetch) is a port of
Todd's (toddrob99) "AHA Exposer" userscript. The automation, hunt loop, control
mode, two-tab method, and viewport experiment are built on top of it.

Every attempt is logged to attempts.jsonl to build a dataset of what works.

Critical workflow: the offer is exposed while ON the VPN. On a qualifying hit the
scanner STOPS, disconnects the VPN (`nordvpn d`), re-verifies the offer survived,
and keeps that exact headed browser window open on the VM desktop (DISPLAY=:0) so
the application is SUBMITTED OFF the VPN in the winning session.

Notes for this VM:
- All Playwright evaluate() calls use arrow-function form; Amex disables page eval().
- NordVPN must have `lan-discovery on` or connecting severs SSH.
"""

import argparse
import base64
import html as html_lib
import json
import os
import random
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote_plus

# pandas and requests are imported lazily inside get_server_list()/get_exit_ip()
# so the BBT control mode runs with only Playwright installed (the hunt and
# experiment modes, which fetch the NordVPN server list and the exit IP, pull
# them in on demand). Playwright itself is optional at import time so the pure
# helpers (and test_control_helpers.py) run on machines without it; any mode
# that opens a browser fails at launch instead.
try:
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import TimeoutError as PWTimeout
except ImportError:  # pragma: no cover - exercised only without playwright
    sync_playwright = None

    class PWTimeout(Exception):
        pass

# ==========================
# Config / Tunables
# ==========================

CARD_TARGETS = {"business_platinum": 300000, "business_gold": 200000}

# Primary entry method: Google the term, follow first organic amex US result.
GOOGLE_QUERIES = {
    "business_platinum": "amex business platinum",
    "business_gold": "amex business gold",
}

# Direct product-page fallback (skips Google). Captured from the first organic
# result; fill business_gold once confirmed. None = no direct fallback.
PRODUCT_URLS = {
    "business_platinum": "https://www.americanexpress.com/us/credit-cards/business/business-credit-cards/american-express-business-platinum-credit-card-amex/",
    "business_gold": "https://www.americanexpress.com/us/credit-cards/business/business-credit-cards/american-express-business-gold-card-amex/",
}

CITIES = ["San_Francisco", "Los_Angeles", "Denver"]
NUM_SERVERS_PER_CITY = 10
RANDOMIZE_SERVERS = False
RANDOM_SEED = 11

NORDVPN_CONNECT_RETRIES = 2
NORDVPN_FAILURE_ABORT_THRESHOLD = 8
NORDVPN_RETRY_BACKOFF_SEC = 3

NAV_TIMEOUT_MS = 40000
OFFER_SECTION_TIMEOUT_MS = 30000
# Apply-click attempts per page load. >1 so a click that beat hydration (403 on
# the sessionless URL) is retried after a reload instead of burning the attempt.
APPLY_CLICK_TRIES = 3
PRINT_LINK_TIMEOUT_MS = 10000
IFRAME_TIMEOUT_MS = 20000
SETTLE_MS = 4000

DELAY_MIN_SEC = 8.0
DELAY_MAX_SEC = 22.0
SETTLE_AFTER_CONNECT_SEC = 5

# Amex intermittently serves a CTA-less page to VPN exits (shows up as
# repeated no_apply_cta). After this many consecutive non-ok attempts, pause for
# a cooldown to ride out the "off" window instead of hammering through it.
COOLDOWN_AFTER_FAILS = 6
COOLDOWN_SEC = 240
# Observed off-windows last up to ~an hour: escalate the pause on each
# consecutive cooldown (240 -> 480 -> 960 -> ...) instead of re-probing a
# standing wall every 4 minutes. Resets on any clean page.
COOLDOWN_MAX_SEC = 1800

USER_AGENT = None  # None -> Playwright default Chromium UA

# Per-attempt fingerprint rotation (UA + viewport). Timezone is set to match the
# connected VPN city so the clock is consistent with the exit IP.
FINGERPRINTS = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
     "viewport": {"width": 1920, "height": 1080}},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
     "viewport": {"width": 1440, "height": 900}},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
     "viewport": {"width": 1366, "height": 768}},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
     "viewport": {"width": 1536, "height": 864}},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
     "viewport": {"width": 1280, "height": 800}},
]

CITY_TZ = {
    "San_Francisco": "America/Los_Angeles",
    "Los_Angeles": "America/Los_Angeles",
    "Denver": "America/Denver",
}

# Experiment finding (see docs/experiment-report.md, from attempts.jsonl, 220
# trials): viewport is the ONE factor that moves the exposed offer (p~0.000 on
# both cards, dose-response with screen area). 1920x1080 gave the highest offers
# and by far the best top-tier rate (Platinum 300k 71% vs <=26% smaller). So we
# FIX the viewport at 1920x1080 and keep only UA/timezone rotation for anti-bot.
TARGET_VIEWPORT = {"width": 1920, "height": 1080}

BASE_DIR = Path(__file__).resolve().parent
REFERRALS_FILE = BASE_DIR / "amex-referrals.txt"
LOG_PATH = BASE_DIR / "attempts.jsonl"
AMEX_HOST = "https://www.americanexpress.com"

# The 5-digit code in the apply URL (`<code>-9-0`) deterministically identifies
# the offer tier (confirmed 377/377). This is the fast path: a known code tells
# you the offer instantly, no exposer needed. The exposer (Todd's technique) is
# the fallback for UNKNOWN codes (and auto-learns them) and the confirm step
# before a real submission. Newly-learned codes persist to LEARNED_CODES_FILE.
KNOWN_OFFER_CODES = {
    "business_platinum": {"45094": 150000, "62369": 150000,
                          "64281": 250000, "68443": 300000,
                          # externally sourced (a churning-group member),
                          # not yet self-validated by exposing the terms here.
                          # Includes a Platinum 200k rung (73165) we never drew.
                          "57460": 150000, "65147": 150000,
                          "73113": 150000, "73165": 200000},
    "business_gold": {"45094": 100000, "64573": 150000,
                      "64586": 175000, "64606": 200000},
}
LEARNED_CODES_FILE = BASE_DIR / "offer_codes.json"

# Runtime marker/screenshot/control files live in a private per-user dir
# (world-readable /tmp leaked session tokens and let any local user forge
# control commands). amex_ctl.py, winner_watch.sh, and vm-tools/fullcap agree
# on the same default; override with AMEX_RUNTIME_DIR (both processes must
# see the same value).
RUNTIME_DIR = Path(os.environ.get("AMEX_RUNTIME_DIR",
                                  str(Path.home() / ".cache" / "amex-ctl")))
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
os.chmod(RUNTIME_DIR, 0o700)
WINNER_MARKER = RUNTIME_DIR / "amex_winner.json"
WINNER_FULL_SHOT = RUNTIME_DIR / "amex_winner_full.png"
FATAL_MARKER = RUNTIME_DIR / "amex_fatal.json"
RELEASE_MARKER = RUNTIME_DIR / "amex_release"
HANDOFF_SHOT = RUNTIME_DIR / "amex_handoff.png"
BANNER_SHOT = RUNTIME_DIR / "amex_banner.png"

# --- Interactive control mode (BBT / multi-session) ---
CTRL_CMD_FILE = RUNTIME_DIR / "amex_ctl_cmd"
CTRL_STATE_FILE = RUNTIME_DIR / "amex_ctl_state.json"
CONTROL_POLL_SEC = 3
CONTROL_MAX_WINDOWS = 8
CONTROL_TIMEOUT_SEC = 6 * 3600
DISPLAY_ZOOM = 0.75
CARD_ALIASES = {
    "platinum": "business_platinum", "plat": "business_platinum",
    "business_platinum": "business_platinum",
    "gold": "business_gold", "business_gold": "business_gold",
}


# ==========================
# ANSI styling
# ==========================

ANSI_ENABLED = sys.stdout.isatty()


def blue(t): return f"\033[94m{t}\033[0m" if ANSI_ENABLED else t
def bold(t): return f"\033[1m{t}\033[0m" if ANSI_ENABLED else t
def green(t): return f"\033[92m{t}\033[0m" if ANSI_ENABLED else t
def red(t): return f"\033[91m{t}\033[0m" if ANSI_ENABLED else t


def banner(title):
    bar = "=" * 70
    print(blue("\n" + bar))
    print(blue(bold(title.center(70))))
    print(blue(bar))


# ==========================
# Logging (JSONL)
# ==========================

def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log_line(obj):
    obj.setdefault("timestamp", _now_iso())
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def log_event(kind, **fields):
    rec = {"type": "event", "event": kind}
    rec.update(fields)
    log_line(rec)


# ==========================
# NordVPN helpers
# ==========================

def run_command(cmd, use_shell=False):
    try:
        out = subprocess.check_output(cmd, shell=use_shell, text=True,
                                      stderr=subprocess.STDOUT)
        return True, out
    except subprocess.CalledProcessError as e:
        return False, e.output or str(e)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def nordvpn_disconnect_quiet():
    ok, out = run_command(["nordvpn", "d"])
    if not ok:
        print(f"[WARN] nordvpn disconnect issue: {out.strip()}")
        return False
    print(out.strip())
    return True


def nordvpn_connect(server, retries=NORDVPN_CONNECT_RETRIES):
    """Returns True on connect, "dedicated" for a dedicated-IP server (a
    known-benign server-list artifact, NOT a VPN failure), False on failure.
    Callers that count failures must treat "dedicated" as a skip."""
    prefix = str(server.hostname.split(".")[0])
    # The prefix comes from the NordVPN API; never hand API data to a shell.
    if not re.fullmatch(r"[A-Za-z0-9-]+", prefix):
        print(f"[ERROR] refusing suspicious server prefix: {prefix!r}")
        return False
    for attempt in range(retries + 1):
        ok, out = run_command(["nordvpn", "c", prefix])
        out_str = (out or "").strip()
        if ok:
            print(out_str)
            return True
        low = out_str.lower()
        if "dedicated ip subscription" in low or "dedicated-ip" in low:
            print(f"[ERROR] {prefix} is a dedicated-IP server; skipping.")
            return "dedicated"
        print(f"[ERROR] connect failed ({attempt + 1}/{retries + 1}) {prefix}: {out_str}")
        if attempt < retries:
            nordvpn_disconnect_quiet()
            time.sleep(NORDVPN_RETRY_BACKOFF_SEC)
    return False


# ==========================
# Nord API
# ==========================

def filter_out_dedicated_ip_servers(df):
    if "categories" not in df.columns:
        return df

    def is_dedicated(cats):
        if not isinstance(cats, list):
            return False
        joined = " ".join(str(c.get("name", "")).lower()
                          for c in cats if isinstance(c, dict))
        return ("dedicated ip" in joined) or ("dedicated_ip" in joined)

    try:
        filtered = df[~df["categories"].apply(is_dedicated)].reset_index(drop=True)
        removed = len(df) - len(filtered)
        if removed:
            print(f"[INFO] Filtered out {removed} dedicated-IP servers.")
        return filtered
    except Exception as e:
        print(f"[WARN] dedicated-IP filter failed: {e}")
        return df


def get_server_list():
    import pandas as pd
    import requests
    try:
        r = requests.get("https://api.nordvpn.com/v1/servers?limit=16384", timeout=10)
        r.raise_for_status()
        return filter_out_dedicated_ip_servers(pd.DataFrame(r.json()))
    except Exception as err:
        print(f"[ERROR] Unable to fetch Nord server list: {err}")
        return None


def find_servers_matching_city(df, city):
    city_str = city.replace("_", " ")
    df = df.copy()
    if "locations" not in df.columns:
        return df.iloc[0:0].reset_index(drop=True)
    try:
        df["locations_str"] = df["locations"].astype("string")
        return df[df["locations_str"].str.contains(city_str, case=False, na=False)] \
            .reset_index(drop=True)
    except Exception as e:
        print(f"[ERROR] city filter {city_str}: {e}")
        return df.iloc[0:0].reset_index(drop=True)


# ==========================
# Entry-point matrix
# ==========================

def card_from_url(url):
    u = url.lower()
    if "business-platinum" in u or "business-gold" in u:
        return "business_platinum" if "platinum" in u else "business_gold"
    return None


def ref_code_from_url(url):
    try:
        return (parse_qs(urlparse(url).query).get("ref") or [None])[0]
    except Exception:
        return None


def load_referral_entries():
    entries = []
    if REFERRALS_FILE.exists():
        for line in REFERRALS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            card = card_from_url(line) or "unknown"
            entries.append({
                "card": card, "target": CARD_TARGETS.get(card, 10 ** 9),
                "method": "referral", "url": line, "query": None,
                "ref_code": ref_code_from_url(line),
            })
        print(f"[INFO] Loaded {len(entries)} referral entr(y/ies).")
    else:
        print(f"[WARN] {REFERRALS_FILE.name} not found; referral method has no "
              f"entries. Copy {REFERRALS_FILE.name}.example and add your links.")
    return entries


def build_entry_matrix(methods, card=None):
    entries = []
    if "google" in methods:
        for c, query in GOOGLE_QUERIES.items():
            entries.append({
                "card": c, "target": CARD_TARGETS[c],
                "method": "google", "url": None, "query": query, "ref_code": None,
            })
    if "referral" in methods:
        entries.extend(load_referral_entries())
    if "direct" in methods:
        for c, url in PRODUCT_URLS.items():
            if url:
                entries.append({
                    "card": c, "target": CARD_TARGETS[c],
                    "method": "direct", "url": url, "query": None, "ref_code": None,
                })
    if card:
        entries = [e for e in entries if e["card"] == card]
    return entries


# ==========================
# Offer-terms exposer
# ==========================

# First non-sponsored americanexpress.com US result from the organic container.
GOOGLE_ORGANIC_JS = r"""
() => {
  const root = document.querySelector('#rso') || document.querySelector('#search') || document.body;
  const inAds = (a) => !!a.closest('#tads,#tadsb,#bottomads,[data-text-ad]');
  for (const h of root.querySelectorAll('a h3')) {
    const a = h.closest('a[href]');
    if (a && !inAds(a) && /americanexpress\.com\/us\//i.test(a.href)) return a.href;
  }
  for (const h of root.querySelectorAll('a h3')) {
    const a = h.closest('a[href]');
    if (a && !inAds(a) && /americanexpress\.com/i.test(a.href)) return a.href;
  }
  return null;
}
"""

# Clicks the Offer Terms trigger, waits for the Print Terms link, returns its
# raw href, then dismisses the modal.
GET_PRINT_HREF_JS = r"""
async (timeoutMs) => {
  function findPrintLink() {
    for (const a of document.querySelectorAll('a[href]')) {
      const href = a.getAttribute('href') || '';
      const text = a.textContent || '';
      if (text.includes('Print Terms') || /\/print\//i.test(href)) return a;
    }
    return null;
  }
  function waitForPrintLink(t) {
    return new Promise((resolve, reject) => {
      const ex = findPrintLink();
      if (ex) return resolve(ex);
      const obs = new MutationObserver(() => {
        const el = findPrintLink();
        if (el) { obs.disconnect(); resolve(el); }
      });
      obs.observe(document.documentElement, { childList: true, subtree: true });
      setTimeout(() => { obs.disconnect(); reject(new Error('Timeout waiting for Print Terms link')); }, t);
    });
  }
  const offerTermsSpan = document.querySelector('span.offer-terms-link');
  const triggerBtn = offerTermsSpan
    ? offerTermsSpan.parentElement
    : document.querySelector('span[title*="Terms"] button');
  if (!triggerBtn) throw new Error('Could not find terms trigger button');
  triggerBtn.click();
  const printLink = await waitForPrintLink(timeoutMs);
  const rawHref = printLink.getAttribute('href');
  if (!rawHref) throw new Error('Print link has no href attribute');
  const modalWrapper = document.querySelector('[data-testid="modal-screen-wrapper"], #modal-screen');
  const closeBtn = modalWrapper
    ? modalWrapper.querySelector('button[aria-label="Close"]')
    : document.querySelector('button[aria-label="Close"]');
  if (closeBtn) closeBtn.click();
  return rawHref;
}
"""

# Loads same-origin print URL in a hidden iframe; returns #offer-terms text.
IFRAME_JS = r"""
async ({ url, timeoutMs }) => {
  return await new Promise((resolve, reject) => {
    const iframe = document.createElement('iframe');
    iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin');
    Object.assign(iframe.style, {
      position: 'fixed', top: '-10000px', left: '-10000px',
      width: '1280px', height: '900px', visibility: 'hidden', border: 'none',
    });
    iframe.src = url;
    document.body.appendChild(iframe);
    let done = false;
    const cleanup = () => { if (iframe.parentNode) iframe.parentNode.removeChild(iframe); };
    const timer = setTimeout(() => {
      if (!done) { done = true; cleanup(); reject(new Error('Timed out waiting for offer content in iframe')); }
    }, timeoutMs);
    function poll() {
      if (done) return;
      try {
        const iDoc = iframe.contentDocument || iframe.contentWindow.document;
        const offerDiv = iDoc.querySelector('#offer-terms');
        if (offerDiv && offerDiv.textContent.trim().length > 100) {
          done = true; clearTimeout(timer);
          const text = offerDiv.textContent; cleanup(); resolve(text); return;
        }
      } catch (_) { /* not accessible yet */ }
      setTimeout(poll, 600);
    }
    iframe.addEventListener('load', () => setTimeout(poll, 2000));
  });
}
"""


def cdp_eval(cdp, expr, await_promise=False):
    """Evaluate JS via CDP Runtime.evaluate. Bypasses Amex's eval monkeypatch
    that breaks Playwright's page.evaluate. `expr` must be an expression
    (wrap arrow functions as IIFEs)."""
    res = cdp.send("Runtime.evaluate", {
        "expression": expr, "returnByValue": True,
        "awaitPromise": await_promise, "userGesture": True,
    })
    if "exceptionDetails" in res:
        ex = res["exceptionDetails"]
        desc = (ex.get("exception", {}) or {}).get("description") or ex.get("text") or "eval error"
        raise RuntimeError(str(desc).splitlines()[0][:160])
    return res["result"].get("value")


def absolutize(href):
    return href if href.startswith("http") else AMEX_HOST + href


def build_print_url(raw_href):
    target = absolutize(raw_href)
    target = re.sub(r"isAhaVariant=true", "isAhaVariant=false", target, flags=re.I)
    target = re.sub(r"showExactOffer=false", "showExactOffer=true", target, flags=re.I)
    return target


def is_same_origin(a, b):
    return urlparse(a).netloc == urlparse(b).netloc


def parse_offer_text(text):
    if not text:
        return None
    norm = re.sub(r"\s+", " ", text)
    m = (re.search(r"Earn\s+[\d,]+\s+Membership\s+Rewards\S*\s*Points", norm, re.I)
         or re.search(r"[\d,]+\s+Membership\s+Rewards\S*\s*Points", norm, re.I))
    return m.group(0).strip() if m else None


def parse_offer_points(text):
    if not text:
        return None
    m = re.search(r"[\d,]+", text)
    return int(m.group(0).replace(",", "")) if m else None


def extract_message_id(url):
    if not url:
        return None
    m = re.search(r"[?&]messageId=([0-9a-fA-F]+)", url)
    return m.group(1) if m else None


def extract_offer_code(url):
    """The 5-digit code in the apply URL path (`.../<code>-9-0?...`)."""
    if not url:
        return None
    m = re.search(r"/(\d{4,6})-9-0", url)
    return m.group(1) if m else None


def load_offer_codes():
    """Built-in code->offer table merged with any codes learned on disk."""
    table = {c: dict(v) for c, v in KNOWN_OFFER_CODES.items()}
    try:
        learned = json.loads(LEARNED_CODES_FILE.read_text())
        for card, m in learned.items():
            table.setdefault(card, {}).update(
                {str(k): int(v) for k, v in m.items()})
    except Exception:
        pass
    return table


def learn_offer_code(card, code, points):
    """Persist a newly observed code->offer mapping to disk."""
    if not (card and code and points):
        return
    try:
        learned = json.loads(LEARNED_CODES_FILE.read_text())
    except Exception:
        learned = {}
    learned.setdefault(card, {})[str(code)] = int(points)
    try:
        LEARNED_CODES_FILE.write_text(
            json.dumps(learned, indent=2, sort_keys=True))
    except Exception as e:
        print(f"[WARN] could not persist learned code {code} "
              f"to {LEARNED_CODES_FILE}: {e}")


def normalize_card(s):
    return CARD_ALIASES.get((s or "").strip().lower())


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_new_commands(path, seen_ids):
    try:
        lines = Path(path).read_text().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        cid = obj.get("id")
        if cid is not None and cid in seen_ids:
            continue
        if cid is not None:
            seen_ids.add(cid)
        out.append(obj)
    return out


def write_state(path, sessions, pid):
    data = {"holder_pid": pid, "updated": now_iso(),
            "sessions": [s.public() for s in sessions]}
    tmp = str(path) + ".tmp"
    Path(tmp).write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def fetch_cross_origin_offer(context, url):
    resp = context.request.get(url, timeout=20000)
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status}")
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", resp.text()))
    return re.sub(r"\s+", " ", text)


# JS expressions run via CDP on Amex pages (page.evaluate is blocked there).
OFFER_SECTION_TEXT_EXPR = (
    "(() => { const e = document.querySelector('.offer-section-content'); "
    "return e ? e.textContent : ''; })()")
OFFER_SECTION_LEN_EXPR = (
    "(() => { const e = document.querySelector('.offer-section-content'); "
    "return e ? e.textContent.trim().length : 0; })()")
BODY_HEAD_EXPR = (
    "(() => document.body ? document.body.innerText.slice(0, 2500) : '')()")
SCROLL_EXPR = "(() => { window.scrollTo(0, 0); window.scrollBy(0, 400); })()"


def read_offer_section(cdp):
    return cdp_eval(cdp, OFFER_SECTION_TEXT_EXPR) or ""


def cdp_wait_offer_section(cdp, timeout_ms):
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        try:
            n = cdp_eval(cdp, OFFER_SECTION_LEN_EXPR)
        except Exception:
            n = 0
        if n and n > 30:
            return True
        time.sleep(0.5)
    return False


def expose_actual_offer(page, context, cdp):
    """Return (offer_text, method, print_url). Page must be an apply page with
    an 'as high as' offer section. Runs the exposer JS via CDP.

    Ported from Todd's (toddrob99) "AHA Exposer" userscript: find the Print Terms
    link, flip isAhaVariant/showExactOffer, and read #offer-terms same-origin via
    iframe or cross-origin via fetch."""
    raw_href = cdp_eval(cdp, f"({GET_PRINT_HREF_JS})({PRINT_LINK_TIMEOUT_MS})",
                        await_promise=True)
    target = build_print_url(raw_href)
    if is_same_origin(target, page.url):
        arg = json.dumps({"url": target, "timeoutMs": IFRAME_TIMEOUT_MS})
        text = cdp_eval(cdp, f"({IFRAME_JS})({arg})", await_promise=True)
        return text, "iframe_same_origin", target
    return fetch_cross_origin_offer(context, target), "request_cross_origin", target


# ==========================
# Reaching the offer page
# ==========================

def google_first_organic(page, query):
    # Google does not disable eval, so page.evaluate works here.
    page.goto("https://www.google.com/search?hl=en&gl=us&q=" + quote_plus(query),
              wait_until="commit", timeout=NAV_TIMEOUT_MS)
    # Let the results container settle before evaluating, else a late redirect
    # destroys the execution context mid-evaluate.
    try:
        page.wait_for_selector("#search, #rso", timeout=15000)
    except PWTimeout:
        pass
    page.wait_for_timeout(SETTLE_MS)
    url = None
    for _ in range(3):
        try:
            url = page.evaluate(GOOGLE_ORGANIC_JS)
            break
        except Exception:
            page.wait_for_timeout(1200)
    if not url:
        body = (page.evaluate("() => document.body ? document.body.innerText : ''") or "").lower()
        if any(s in body for s in ("unusual traffic", "i'm not a robot", "recaptcha")):
            raise RuntimeError("google_captcha")
        raise RuntimeError("no_organic_amex_result")
    return url


APPLY_SELECTORS = ("button:has-text('Apply Now')", "a:has-text('Apply Now')",
                   "button:has-text('Apply')", "a:has-text('Apply')")


def wait_any(page, selectors, timeout_ms):
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        for s in selectors:
            try:
                if page.query_selector(s):
                    return s
            except Exception:
                pass
        page.wait_for_timeout(300)
    return None


def _dismiss_referral_intro(page):
    # Referral links open a "You've Been Referred" intro overlay that intercepts
    # clicks; dismiss it via its Continue button before the Apply Now is usable.
    for sel in ("button:has-text('Continue')", "a:has-text('Continue')"):
        try:
            el = page.wait_for_selector(sel, timeout=8000)
        except PWTimeout:
            el = None
        if el:
            try:
                el.click(timeout=5000)
                page.wait_for_timeout(1500)
                return True
            except Exception:
                continue
    return False


def _click_apply(page):
    # Try each matching element; the first match can be an off-screen header
    # button (and Gold's Apply Now carries no href, so it must be clicked).
    # Pick a visible one, scroll it in, and click with a short timeout so a
    # non-actionable button fails fast instead of hanging 30s.
    for sel in APPLY_SELECTORS:
        for el in page.query_selector_all(sel):
            try:
                if not el.is_visible():
                    continue
                el.scroll_into_view_if_needed(timeout=3000)
                el.click(timeout=5000)
                return True
            except Exception:
                # The 2026-08 product-page redesign animates continuously, so
                # Playwright's actionability check never sees the button "stable"
                # and scroll/click time out on a button a human clicks fine.
                # Fall back to a JS scroll and a force-click at its position.
                try:
                    el.evaluate("e => e.scrollIntoView({block: 'center'})")
                    page.wait_for_timeout(300)
                    el.click(timeout=3000, force=True)
                    return True
                except Exception:
                    continue
    return False


def open_offer_page(page, cdp, entry, rec):
    """Reach the apply page (offer-section-content present). The offer only
    renders after clicking Apply. Returns the final apply URL."""
    if entry["method"] == "google":
        landing = google_first_organic(page, entry["query"])
    else:
        landing = entry["url"]
    rec["landing_url"] = landing

    # Product/landing page: eval is allowed here, so Playwright selectors work.
    # The React page renders the Apply button after a moment; wait for it (a
    # fixed delay races slow renders) rather than sleeping a fixed amount.
    page.goto(landing, wait_until="commit", timeout=NAV_TIMEOUT_MS)
    if entry["method"] == "referral":
        _dismiss_referral_intro(page)
    found = wait_any(page, (".offer-section-content",) + APPLY_SELECTORS,
                     OFFER_SECTION_TIMEOUT_MS)
    if found is None:
        raise RuntimeError("no_apply_cta")

    if not page.query_selector(".offer-section-content"):
        # Ensure the page is hydrated before clicking Apply. The valid path runs
        # Amex's JS, which mints a session and lands on
        # /credit-cards/apply/business/...?messageId. Clicking before hydration
        # does a raw navigation to the bare /card-application/apply/ URL, which
        # Amex returns 403 for (no session) and which is unusable for applying.
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except PWTimeout:
            pass
        page.wait_for_timeout(1500)
        dwell = entry.get("dwell_s") or 0
        if dwell:
            page.wait_for_timeout(int(dwell * 1000))
        # Only the business apply URL (with a session/messageId) is valid. A
        # click that beat hydration raw-navigates to /card-application/apply/,
        # which Amex 403s. That is recoverable within the attempt: go back, let
        # the page finish hydrating, and click again (slow VPN exits need it,
        # and it ran ~44% of attempts on a 2026-08-06 sample without this).
        for click_try in range(APPLY_CLICK_TRIES):
            if not _click_apply(page):
                raise RuntimeError("no_apply_cta")
            try:
                page.wait_for_url("**/credit-cards/apply/business/**",
                                  timeout=NAV_TIMEOUT_MS)
                break
            except PWTimeout:
                if "/card-application/apply/" not in page.url:
                    raise RuntimeError("apply_nav_failed")
                if click_try >= APPLY_CLICK_TRIES - 1:
                    raise RuntimeError("apply_403_no_session")
                # Recover to the product page and give hydration more room.
                # Any failure here falls through to the plain 403, so this is
                # never worse than failing fast.
                try:
                    page.goto(landing, wait_until="commit",
                              timeout=NAV_TIMEOUT_MS)
                    if wait_any(page, APPLY_SELECTORS,
                                OFFER_SECTION_TIMEOUT_MS) is None:
                        raise RuntimeError("no_apply_cta")
                    try:
                        page.wait_for_load_state("networkidle", timeout=15000)
                    except PWTimeout:
                        pass
                    page.wait_for_timeout(3000 * (click_try + 1))
                except RuntimeError:
                    raise
                except Exception:
                    raise RuntimeError("apply_403_no_session")

    # Apply page: eval is blocked, poll for the offer section via CDP.
    if not cdp_wait_offer_section(cdp, OFFER_SECTION_TIMEOUT_MS):
        raise RuntimeError("offer_section_not_found")
    rec["apply_url_final"] = page.url
    return page.url


def page_blocked(cdp):
    try:
        body = cdp_eval(cdp, BODY_HEAD_EXPR) or ""
    except Exception:
        body = ""
    low = body.lower()
    if "something went wrong" in low:
        return "amex_error_page"
    if any(s in low for s in ("access denied", "reference #", "unusual activity",
                              "are you a robot", "error 403 forbidden",
                              "http error 403")):
        return "blocked"
    return None


# ==========================
# Attempt runner
# ==========================

def new_record(entry, city, server):
    return {
        "type": "attempt", "attempt_id": uuid.uuid4().hex,
        "card": entry["card"], "target": entry["target"],
        "city": city, "server_hostname": getattr(server, "hostname", None),
        "method": entry["method"], "query": entry["query"],
        "entry_url": entry["url"], "ref_code": entry["ref_code"],
        "landing_url": None, "apply_url_final": None, "offer_code": None,
        "page_status": None, "as_high_as_detected": None,
        "exposure_method": None, "print_url": None,
        "exposed_offer_text": None, "exposed_offer_points": None,
        "qualified": False, "error": None, "duration_sec": None, "try": 0,
    }


# Bounded per-run failure forensics: on a failed/unparsable attempt, save a
# screenshot + body head + URL so "what did Amex actually serve" is answerable
# from artifacts instead of a live debugging session.
FAILURE_DIR = BASE_DIR / "failures"
FAILURE_SAVE_MAX = 5
_failure_saves = 0


def save_failure_artifacts(page, cdp, rec, tag):
    global _failure_saves
    if _failure_saves >= FAILURE_SAVE_MAX:
        return
    _failure_saves += 1
    try:
        FAILURE_DIR.mkdir(exist_ok=True)
        base = FAILURE_DIR / (time.strftime("%Y%m%d-%H%M%S")
                              + f"-{tag}-{rec['attempt_id'][:8]}")
        try:
            page.screenshot(path=f"{base}.png")
        except Exception:
            pass
        try:
            body = cdp_eval(cdp, BODY_HEAD_EXPR) or ""
        except Exception:
            body = ""
        Path(f"{base}.txt").write_text(
            f"url: {getattr(page, 'url', '?')}\n"
            f"page_status: {rec.get('page_status')}\n"
            f"error: {rec.get('error')}\n"
            f"exposed_offer_text: {rec.get('exposed_offer_text')}\n"
            f"--- body head ---\n{body[:2000]}\n")
        rec["failure_artifact"] = str(base)
        print(f"[FORENSICS] saved {base}.png/.txt")
    except Exception:
        pass


def run_attempt(context, entry, city, server, force_expose=False):
    rec = new_record(entry, city, server)
    page = context.new_page()
    cdp = context.new_cdp_session(page)
    t0 = time.time()
    try:
        open_offer_page(page, cdp, entry, rec)
        blk = page_blocked(cdp)
        rec["page_status"] = blk or "ok"
        if rec["page_status"] == "ok":
            # The apply-URL code deterministically = the offer. Known code is the
            # fast path (skip the exposer); unknown code or force_expose falls
            # back to the exposer (Todd's technique), which also learns new codes.
            code = extract_offer_code(rec.get("apply_url_final"))
            rec["offer_code"] = code
            known = load_offer_codes().get(entry["card"], {}).get(code) if code else None

            if known is not None and not force_expose:
                rec["exposed_offer_points"] = known
                rec["exposed_offer_text"] = f"Earn {known:,} Membership Rewards Points"
                rec["exposure_method"] = "url_code"
            else:
                sec = read_offer_section(cdp)
                aha = "as high as" in sec.lower()
                rec["as_high_as_detected"] = aha
                if aha:
                    text, method, target = expose_actual_offer(page, context, cdp)
                    rec["exposure_method"] = method
                    rec["print_url"] = target
                    rec["exposed_offer_text"] = parse_offer_text(text)
                else:
                    rec["exposure_method"] = "direct"
                    rec["exposed_offer_text"] = parse_offer_text(sec)
                rec["exposed_offer_points"] = parse_offer_points(
                    rec["exposed_offer_text"])
                # Learn an unknown code; flag drift if a known code disagrees.
                if code and rec["exposed_offer_points"]:
                    if known is None:
                        learn_offer_code(entry["card"], code,
                                         rec["exposed_offer_points"])
                        rec["code_learned"] = True
                    else:
                        rec["code_offer_match"] = (
                            known == rec["exposed_offer_points"])
            # M5: an exposed-but-unparsable offer must be LOUD, not a silent
            # zero. A parse regression on a real winner page would otherwise
            # log clean and keep hunting past the winner.
            if (rec["exposed_offer_text"]
                    and rec["exposed_offer_points"] is None):
                rec["page_status"] = "parse_error"
                save_failure_artifacts(page, cdp, rec, "parse")
            pts = rec["exposed_offer_points"]
            rec["qualified"] = bool(pts is not None and pts >= entry["target"])
    except PWTimeout as e:
        rec["error"] = f"PWTimeout: {str(e).splitlines()[0]}"
        rec["page_status"] = rec["page_status"] or "timeout"
        save_failure_artifacts(page, cdp, rec, "timeout")
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["page_status"] = rec["page_status"] or "error"
        save_failure_artifacts(page, cdp, rec, "error")
    finally:
        rec["duration_sec"] = round(time.time() - t0, 1)
    return rec, page


RETRYABLE = ("no_organic_amex_result", "Timeout", "no_apply_cta",
             "offer_section_not_found", "google_captcha",
             "Execution context was destroyed", "navigation",
             "apply_403_no_session", "apply_nav_failed", "ERR_CONNECTION_RESET")


def is_retryable(rec):
    err = rec.get("error") or ""
    return any(k in err for k in RETRYABLE)


def summarize(rec):
    pts = rec["exposed_offer_points"]
    pts_s = f"{pts:,}" if isinstance(pts, int) else "-"
    if rec["qualified"]:
        flag = green("QUALIFIES " + pts_s)
    elif rec["page_status"] != "ok":
        flag = red(rec["page_status"])
    else:
        flag = pts_s
    tail = f"  ({rec['error']})" if rec["error"] else ""
    return (f"[{rec['card']}] {rec['city']} {rec.get('server_hostname','')} "
            f"{rec['method']} aha={rec['as_high_as_detected']} -> {flag}{tail}")


# ==========================
# Handoff
# ==========================

def show_offer_banner(cdp, rec):
    """Inject a prominent fixed banner into the page showing the REAL exposed
    offer (Amex's own page only shows the 'as high as' headline). Built via DOM
    so it works through CDP despite the page's eval block."""
    pts = rec.get("exposed_offer_points")
    payload = {
        "card": rec.get("card", ""),
        "offer": rec.get("exposed_offer_text") or "unknown",
        "target": f"{rec.get('target'):,}" if isinstance(rec.get("target"), int) else "?",
        "color": "#1a7f37" if rec.get("qualified") else "#9a6700",
        "label": "QUALIFYING OFFER" if rec.get("qualified") else "EXPOSED OFFER",
    }
    expr = ("(() => { const P=" + json.dumps(payload) + ";"
            "const id='amex-real-offer-banner';"
            "const old=document.getElementById(id); if(old) old.remove();"
            "const d=document.createElement('div'); d.id=id;"
            "Object.assign(d.style,{position:'fixed',top:'10px',left:'50%',"
            "transform:'translateX(-50%)',zIndex:'2147483647',background:P.color,"
            "color:'#fff',padding:'10px 16px',borderRadius:'10px',textAlign:'center',"
            "font:'700 15px Helvetica,Arial,sans-serif',boxShadow:'0 6px 24px rgba(0,0,0,.35)',"
            "maxWidth:'92vw'});"
            "const l1=document.createElement('div'); l1.textContent=P.label+'  ('+P.card+', headline as-high-as '+P.target+')';"
            "l1.style.cssText='font-size:12px;font-weight:600;opacity:.92;margin-bottom:3px';"
            "const l2=document.createElement('div'); l2.textContent=P.offer; l2.style.cssText='font-size:20px;font-weight:800';"
            "const l3=document.createElement('div'); l3.id='amex-banner-clock';"
            "l3.textContent='exposed just now; sessions expire ~15-30 min after exposure';"
            "l3.style.cssText='font-size:11px;font-weight:600;opacity:.85;margin-top:3px';"
            "const x=document.createElement('span'); x.textContent='✕'; x.title='dismiss';"
            "x.style.cssText='position:absolute;top:6px;right:10px;cursor:pointer;font-size:13px';"
            "x.onclick=()=>d.remove();"
            "d.appendChild(x); d.appendChild(l1); d.appendChild(l2); d.appendChild(l3);"
            "document.body.appendChild(d); return true; })()")
    try:
        cdp_eval(cdp, expr)
    except Exception as e:
        print(f"[WARN] banner inject failed: {e}")


def _notify(title, msg):
    """Best-effort GNOME desktop notification from the detached hunt process."""
    try:
        env = dict(os.environ)
        env.setdefault("DISPLAY", ":0")
        env.setdefault("DBUS_SESSION_BUS_ADDRESS",
                       f"unix:path=/run/user/{os.getuid()}/bus")
        subprocess.Popen(["notify-send", title, msg], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _fatal_abort(reason, failures, attempts):
    """Loud, machine-readable death: a detached hunt must never just vanish.
    Writes a marker for watchers, logs the event, and fires a desktop notify."""
    print(red(bold(f"[FATAL] {reason}: {failures} consecutive failures; "
                   f"aborting after {attempts} attempts.")))
    log_event("fatal_abort", reason=reason, failures=failures,
              attempts=attempts)
    try:
        FATAL_MARKER.write_text(json.dumps(
            {"reason": reason, "failures": failures, "attempts": attempts,
             "ts": now_iso()}))
    except Exception:
        pass
    _notify("Amex hunt ABORTED",
            f"{reason} x{failures}; stopped after {attempts} attempts.")


def capture_full_page(page, path):
    """Full-page PNG of the live document via CDP captureBeyondViewport: renders
    the whole page (full width + full scroll height) off-surface, so it does NOT
    scroll or disturb a filled-in form the way page.screenshot(full_page=True)
    would. Preserves formatting exactly (it is a real raster render, unlike
    print-to-PDF or save-as-HTML)."""
    cdp = page.context.new_cdp_session(page)
    res = cdp.send("Page.captureScreenshot",
                   {"format": "png", "captureBeyondViewport": True,
                    "fromSurface": True})
    Path(path).write_bytes(base64.b64decode(res["data"]))
    return path


# Touch this file (e.g. from the `fullcap` hotkey) to make the held apply page
# save a full-page PNG to ~/Pictures without disturbing the form.
SHOT_TRIGGER = RUNTIME_DIR / "amex_shot"
SHOT_DONE = RUNTIME_DIR / "amex_shot_done"
FULL_SHOT = RUNTIME_DIR / "amex_full.png"


def maybe_capture(page):
    """If the shot trigger file is present, save a full-page capture of the held
    page to ~/Pictures (native) and the runtime dir (for off-VM pull).
    Non-disruptive."""
    if not SHOT_TRIGGER.exists():
        return
    try:
        pics = Path.home() / "Pictures"
        pics.mkdir(exist_ok=True)
        n = 1
        while (pics / f"amex-app-capture-{n}.png").exists():
            n += 1
        out = pics / f"amex-app-capture-{n}.png"
        capture_full_page(page, str(out))
        FULL_SHOT.write_bytes(out.read_bytes())
        SHOT_DONE.write_text(str(out))
        print(green(bold(f"[CAPTURE] full page -> {out}")))
        _notify("Amex capture", f"Full page saved: {out.name}")
    except Exception as e:
        SHOT_DONE.write_text(f"FAILED: {e}")
        print(red(f"[CAPTURE] failed: {e}"))
    finally:
        try:
            SHOT_TRIGGER.unlink()
        except Exception:
            pass


def handoff(page, rec, hunt=False, attempts=None, elapsed_sec=None):
    banner("ELEVATED OFFER FOUND")
    print(blue(f"Card:   {rec['card']}"))
    print(blue("Offer:  ") + green(bold(rec["exposed_offer_text"] or "?")))
    print(blue(f"City:   {rec['city']}"))
    print(blue(f"Server: {rec.get('server_hostname','')}"))
    print(blue(f"Apply:  {rec['apply_url_final']}"))
    if attempts is not None:
        took = f" in {elapsed_sec / 60.0:.1f} min" if elapsed_sec is not None else ""
        print(blue("Found:  ") + green(bold(f"attempt #{attempts}{took}")))

    try:
        cdp = page.context.new_cdp_session(page)
    except Exception:
        cdp = None
    try:
        page.bring_to_front()
        if cdp:
            cdp_eval(cdp, SCROLL_EXPR)
    except Exception as e:
        print(f"[WARN] focus/scroll: {e}")

    # If this winner was qualified from the URL code alone (fast path), confirm
    # with the exposer (Todd's technique) before a real submission. Catches a
    # stale code table; the live terms are the ground truth.
    if cdp and rec.get("exposure_method") == "url_code":
        try:
            text, method, target = expose_actual_offer(page, page.context, cdp)
            confirmed = parse_offer_points(parse_offer_text(text))
            print(blue(f"[CONFIRM] code {rec.get('offer_code')} table-says "
                       f"{rec['exposed_offer_points']:,}; live-exposed {confirmed}"))
            if confirmed and confirmed != rec["exposed_offer_points"]:
                print(red(bold("[WARN] code/offer MISMATCH, trusting the live "
                               "offer. Verify before submitting.")))
                rec["exposed_offer_points"] = confirmed
                rec["exposed_offer_text"] = parse_offer_text(text)
            rec["exposure_method"] = method
            rec["print_url"] = target
        except Exception as e:
            print(f"[WARN] confirm-expose failed: {e}")

    print("\n[INFO] Disconnecting VPN before submission...")
    nordvpn_disconnect_quiet()
    log_event("vpn_disconnect_for_handoff", attempt_id=rec["attempt_id"])

    try:
        sec = read_offer_section(cdp) if cdp else ""
        still = "as high as" in sec.lower()
        print(blue(f"[CHECK] offer section still present after disconnect: {still}"))
        log_event("post_disconnect_check", attempt_id=rec["attempt_id"], still=still)
        if not still:
            print(red(bold("[WARN] Offer section changed after disconnect. "
                           "Verify the elevated offer before applying.")))
    except Exception as e:
        print(f"[WARN] post-disconnect check failed: {e}")

    if cdp:
        show_offer_banner(cdp, rec)
        # The 1920 viewport is wider than the VM's 1408 display: zoom the
        # content so the whole form is on-screen (cosmetic; window.innerWidth
        # stays 1920 and the exposed offer is already locked).
        apply_display_zoom(cdp, DISPLAY_ZOOM)
        try:
            page.screenshot(path=str(HANDOFF_SHOT))
        except Exception:
            pass

    print("=" * 70)
    print(bold("ACTION: complete and submit the application in the open window."))
    print(red(bold("VPN is now DISCONNECTED. Submit on your normal IP.")))
    print("=" * 70)
    log_event("handoff", attempt_id=rec["attempt_id"], card=rec["card"],
              offer=rec["exposed_offer_text"])
    if hunt:
        # Detached hunt: write a winner marker and hold the browser open by
        # waiting for a release file (or a long timeout) instead of stdin.
        marker = {"card": rec["card"], "offer": rec.get("exposed_offer_text"),
                  "points": rec.get("exposed_offer_points"),
                  "apply_url": rec.get("apply_url_final"),
                  "city": rec.get("city"), "attempt_id": rec["attempt_id"],
                  "attempts": attempts, "elapsed_sec": elapsed_sec}
        try:
            WINNER_MARKER.write_text(json.dumps(marker))
        except Exception as e:
            print(f"[WARN] winner marker: {e}")
        print(green(bold("[HUNT] Winner held open on the VM desktop. Apply now. "
                         f"Create {RELEASE_MARKER} to close.")))
        _notify("AMEX WINNER FOUND",
                f"{rec['card']}: {rec.get('exposed_offer_text')} "
                f"({rec.get('city')}). Submit within ~15 min.")
        # Immediate full-page proof capture, so the exposed offer is on disk
        # even if the session expires before submission.
        try:
            pics = Path.home() / "Pictures"
            pics.mkdir(exist_ok=True)
            n = 1
            while (pics / f"amex-winner-{n}.png").exists():
                n += 1
            wout = pics / f"amex-winner-{n}.png"
            capture_full_page(page, str(wout))
            WINNER_FULL_SHOT.write_bytes(wout.read_bytes())
            print(green(f"[CAPTURE] winner proof -> {wout}"))
        except Exception as e:
            print(f"[WARN] winner capture: {e}")
        deadline = time.time() + 4 * 3600
        t_hold = time.time()
        last_kick = 0.0
        try:
            while time.time() < deadline and not RELEASE_MARKER.exists():
                # Keep the window in front and the renderer alive so an idle
                # window manager / blanker cannot drop it off the desktop.
                if time.time() - last_kick > 25:
                    try:
                        page.bring_to_front()
                        page.screenshot(path=str(HANDOFF_SHOT))
                    except Exception:
                        pass
                    if cdp:
                        mins = int((time.time() - t_hold) / 60)
                        txt = (f"held {mins} min; sessions expire ~15-30 min "
                               "after exposure")
                        try:
                            cdp_eval(cdp, "(() => { const e=document."
                                     "getElementById('amex-banner-clock');"
                                     " if(e) e.textContent="
                                     + json.dumps(txt) + "; return true; })()")
                        except Exception:
                            pass
                    last_kick = time.time()
                maybe_capture(page)
                time.sleep(3)
        except KeyboardInterrupt:
            pass
        try:
            RELEASE_MARKER.unlink()
        except Exception:
            pass
        return
    try:
        confirm = ""
        while confirm.upper() != "COMPLETE":
            confirm = input("Type COMPLETE once submitted to close the browser: ")
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted while waiting for completion.")


# ==========================
# Browser
# ==========================

def build_browser(pw, headless):
    return pw.chromium.launch(
        headless=headless,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled"])


def new_session(browser, city=None, timezone=None):
    fp = random.choice(FINGERPRINTS)
    tz = timezone or CITY_TZ.get(city, "America/New_York")
    # Viewport is fixed at 1920x1080 (best-offer level from the experiment);
    # only the UA varies for anti-bot. Pass viewport=None for the old
    # randomized-viewport behavior (e.g. to re-run the experiment).
    viewport = TARGET_VIEWPORT
    ctx = browser.new_context(
        viewport=viewport, locale="en-US",
        user_agent=USER_AGENT or fp["ua"], timezone_id=tz)
    ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    m = re.search(r"Chrome/(\d+)", fp["ua"])
    meta = {
        "fp_platform": "windows" if "Windows" in fp["ua"] else "mac",
        "fp_chrome": m.group(1) if m else "?",
        "fp_viewport": f'{viewport["width"]}x{viewport["height"]}',
        "fp_timezone": tz,
    }
    return ctx, meta


# Several providers, because one is a single point of failure for the VPN-only
# guard. Home-network DNS filtering blackholes api.ipify.org to 0.0.0.0 (it sits
# on common blocklists), which silently killed the residential baseline read and
# left the guard inactive for a whole run. A VPN connect masks it, since the
# tunnel's resolvers answer normally, so it only shows up on the direct path.
EXIT_IP_URLS = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
)


def _looks_like_ipv4(s):
    """Reject block pages, empty bodies, and IPv6. Comparing a v6 answer against
    a v4 baseline would trip the guard on a perfectly good exit."""
    parts = s.split(".")
    if len(parts) != 4 or s == "0.0.0.0":
        return False
    try:
        return all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def get_exit_ip():
    """First plausible answer wins. None means every provider failed, which the
    exit-IP guard must treat as inconclusive, never as a pass."""
    import requests
    for url in EXIT_IP_URLS:
        try:
            txt = requests.get(url, timeout=8).text.strip()
        except Exception:
            continue
        if _looks_like_ipv4(txt):
            return txt
    return None


def experiment_profiles(pw):
    """Viewport/device profiles for the experiment: desktop sizes (varied UA)
    plus full mobile/tablet emulation via Playwright device descriptors
    (viewport + mobile UA + device-scale-factor + isMobile + hasTouch)."""
    profs = []
    for w, h in [(1280, 800), (1366, 768), (1440, 900), (1536, 864),
                 (1920, 1080), (2560, 1440)]:
        profs.append({"kind": "desktop", "name": f"desktop-{w}x{h}",
                      "viewport": {"width": w, "height": h}, "is_mobile": False})
    for name in ["iPhone SE", "iPhone 14 Pro Max", "Pixel 7", "iPad (gen 7)"]:
        d = pw.devices.get(name)
        if d:
            profs.append({"kind": "device", "name": name, "device": dict(d),
                          "viewport": d.get("viewport"),
                          "is_mobile": d.get("is_mobile", True)})
    return profs


def new_session_profile(browser, pw, profile, timezone):
    """Build a context for a given viewport/device profile. Production uses the
    fixed-viewport new_session(); this is for the experiment only."""
    fp = random.choice(FINGERPRINTS)
    if profile["kind"] == "device":
        kwargs = dict(profile["device"])
        kwargs.pop("default_browser_type", None)  # not a new_context arg
    else:
        kwargs = {"viewport": profile["viewport"], "user_agent": fp["ua"]}
    kwargs["locale"] = "en-US"
    kwargs["timezone_id"] = timezone
    ctx = browser.new_context(**kwargs)
    ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    vp = profile.get("viewport") or {}
    meta = {
        "fp_platform": "mobile" if profile["is_mobile"]
                       else ("windows" if "Windows" in fp["ua"] else "mac"),
        "fp_viewport": f"{vp.get('width')}x{vp.get('height')}",
        "fp_timezone": timezone,
        "device": profile["name"],
        "is_mobile": profile["is_mobile"],
    }
    return ctx, meta


def human_delay():
    time.sleep(random.uniform(DELAY_MIN_SEC, DELAY_MAX_SEC))


# ==========================
# Interactive control mode (BBT / multi-session)
# ==========================

class Session:
    def __init__(self, idx, context, page, cdp, card, target):
        self.idx = idx
        self.context = context
        self.page = page
        self.cdp = cdp
        self.card = card
        self.target = target
        self.last_messageId = None
        self.rec = None
        self.exposed_ts = None
        self.window_title = ""
        self.failed = False
        self.error = None
        self.group = None  # tabs sharing one context (two-tab method) share a group

    def current_url(self):
        # Read the LIVE document location via CDP. Playwright's page.url lags on
        # user-initiated Back/forward navigations (it stayed on the old apply URL
        # while the window had already gone to the product page), which made the
        # navigation poll miss real BBT navigations. location.href is always
        # current. Fall back to page.url only if the CDP eval fails.
        try:
            u = cdp_eval(self.cdp, "(() => location.href)()")
            if u:
                return u
        except Exception:
            pass
        try:
            return self.page.url
        except Exception:
            return None

    def public(self):
        r = self.rec or {}
        return {
            "idx": self.idx,
            "window_title": self.window_title,
            "url": self.current_url(),
            "messageId": self.last_messageId,
            "offer_text": r.get("exposed_offer_text"),
            "points": r.get("exposed_offer_points"),
            "offer_code": r.get("offer_code"),
            "qualified": r.get("qualified"),
            "method": r.get("exposure_method"),
            "failed": self.failed,
            "error": self.error or r.get("error"),
            "exposed_ts": self.exposed_ts,
            "group": self.group,
        }


def control_entry(card):
    return {"card": card, "target": CARD_TARGETS[card], "method": "direct",
            "url": PRODUCT_URLS[card], "query": None, "ref_code": None}


def set_window_title(cdp, text):
    try:
        cdp_eval(cdp, "(() => { document.title = " + json.dumps(text)
                 + "; return true; })()")
    except Exception:
        pass


def apply_display_zoom(cdp, factor):
    # Cosmetic page-content zoom so the 1920 viewport fits the 1408 display.
    # window.innerWidth stays 1920, so the exposed offer is unaffected.
    try:
        cdp_eval(cdp, "(() => { document.documentElement.style.zoom = "
                 + json.dumps(str(factor)) + "; return true; })()")
    except Exception:
        pass


def decorate_session(sess):
    try:
        show_offer_banner(sess.cdp, sess.rec)
    except Exception:
        pass
    pts = (sess.rec or {}).get("exposed_offer_points")
    q = (sess.rec or {}).get("qualified")
    code = (sess.rec or {}).get("offer_code") or "?"
    short = (sess.last_messageId or "")[:8]
    k = f"{pts // 1000}k" if isinstance(pts, int) else "?"
    sess.window_title = (f"{k} {'OK' if q else ''} [{code}] | AmEx "
                         f"{sess.card.split('_')[-1]} {short}")
    set_window_title(sess.cdp, sess.window_title)
    apply_display_zoom(sess.cdp, DISPLAY_ZOOM)
    sess.exposed_ts = now_iso()


def spawn_session(browser, idx, card, headless):
    entry = control_entry(card)

    class _S:
        hostname = "control"

    ctx = page = rec = None
    for attempt in range(2):
        ctx, meta = new_session(browser)
        rec, page = run_attempt(ctx, entry, "control", _S())
        rec.update(meta)
        rec["try"] = attempt
        log_line(rec)
        if not rec.get("error") or not is_retryable(rec):
            break
        try:
            ctx.close()
        except Exception:
            pass
        time.sleep(2)
    cdp = ctx.new_cdp_session(page)
    sess = Session(idx, ctx, page, cdp, card, CARD_TARGETS[card])
    sess.rec = rec
    sess.last_messageId = extract_message_id(rec.get("apply_url_final"))
    if rec.get("error"):
        sess.failed = True
        sess.error = rec.get("error")
    else:
        decorate_session(sess)
    return sess


def spawn_tabgroup(browser, start_idx, cards):
    """Two-tab method: open one tab per card in ONE SHARED context (shared
    cookies), instead of separate contexts. Each tab is its own application
    (its own messageId/offer), but they share the browser session, which is the
    thing that can let Amex reuse a single credit pull across the two
    submissions. Returns a list of Sessions, all sharing one context.

    Typical use is one charge card (e.g. business_platinum/business_gold) plus
    one revolving card; you submit the first, then immediately the second. The
    tool only opens and verifies the tabs, you fill and submit both yourself."""
    ctx, meta = new_session(browser)

    class _S:
        hostname = "control"

    out = []
    for i, card in enumerate(cards):
        entry = control_entry(card)
        rec = page = None
        for attempt in range(2):
            rec, page = run_attempt(ctx, entry, "control", _S())
            rec.update(meta)
            rec["try"] = attempt
            log_line(rec)
            if not rec.get("error") or not is_retryable(rec):
                break
            try:
                page.close()  # close the failed tab, retry in the SAME context
            except Exception:
                pass
            time.sleep(2)
        cdp = ctx.new_cdp_session(page)
        sess = Session(start_idx + i, ctx, page, cdp, card, CARD_TARGETS[card])
        sess.rec = rec
        sess.last_messageId = extract_message_id(rec.get("apply_url_final"))
        if rec.get("error"):
            sess.failed = True
            sess.error = rec.get("error")
        else:
            decorate_session(sess)
        out.append(sess)
    return out


def reexpose_session(sess):
    """Re-run exposure on the session's CURRENT page (after a navigation to a
    new application). Returns True if a fresh offer was read."""
    page, ctx, cdp = sess.page, sess.context, sess.cdp
    if not cdp_wait_offer_section(cdp, 8000):
        return False
    rec = dict(sess.rec or {})
    rec["error"] = None
    rec["apply_url_final"] = sess.current_url()
    rec["card"] = sess.card
    rec["target"] = sess.target
    code = extract_offer_code(rec["apply_url_final"])
    rec["offer_code"] = code
    known = load_offer_codes().get(sess.card, {}).get(code) if code else None
    try:
        if known is not None:
            # Fast path: known code = known offer, no exposer needed.
            rec["exposed_offer_points"] = known
            rec["exposed_offer_text"] = f"Earn {known:,} Membership Rewards Points"
            rec["exposure_method"] = "url_code"
        else:
            sec = read_offer_section(cdp)
            aha = "as high as" in sec.lower()
            rec["as_high_as_detected"] = aha
            if aha:
                text, method, target = expose_actual_offer(page, ctx, cdp)
                rec["exposure_method"] = method
                rec["print_url"] = target
                rec["exposed_offer_text"] = parse_offer_text(text)
            else:
                rec["exposure_method"] = "direct"
                rec["exposed_offer_text"] = parse_offer_text(sec)
            rec["exposed_offer_points"] = parse_offer_points(
                rec["exposed_offer_text"])
            if code and rec["exposed_offer_points"]:
                learn_offer_code(sess.card, code, rec["exposed_offer_points"])
        pts = rec["exposed_offer_points"]
        rec["qualified"] = bool(pts is not None and pts >= sess.target)
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
        sess.rec = rec
        return False
    sess.rec = rec
    sess.last_messageId = extract_message_id(rec["apply_url_final"])
    decorate_session(sess)
    log_line({"type": "control_reexpose", "card": sess.card,
              "messageId": sess.last_messageId,
              "exposed_offer_points": rec.get("exposed_offer_points"),
              "exposure_method": rec.get("exposure_method"),
              "qualified": rec.get("qualified"), "timestamp": now_iso()})
    return True


def mode_control(args):
    pw = sync_playwright().start()
    browser = build_browser(pw, args.headless)
    max_windows = getattr(args, "max_windows", CONTROL_MAX_WINDOWS)
    sessions = []
    seen_ids = set()
    next_idx = 0
    group_counter = 0
    pid = os.getpid()
    for f in (CTRL_CMD_FILE, CTRL_STATE_FILE):
        try:
            Path(f).unlink()
        except FileNotFoundError:
            pass
    print(f"[control] holder pid {pid}; cmd={CTRL_CMD_FILE} "
          f"state={CTRL_STATE_FILE} headless={args.headless} "
          f"max_windows={max_windows}")

    def do_spawn(card, n):
        nonlocal next_idx, group_counter
        room = max_windows - len(sessions)
        if n > room:
            print(f"[control] clamping spawn {n} -> {room} (max {max_windows})")
            n = room
        for _ in range(max(0, n)):
            s = spawn_session(browser, next_idx, card, args.headless)
            s.group = group_counter        # independent context = its own group
            group_counter += 1
            next_idx += 1
            sessions.append(s)
            pts = (s.rec or {}).get("exposed_offer_points")
            print(f"[control] spawned idx={s.idx} pts={pts} failed={s.failed} "
                  f"msgId={(s.last_messageId or '')[:8]}")
            write_state(CTRL_STATE_FILE, sessions, pid)

    def do_tabgroup(cards):
        # Two-tab method: open all cards as tabs in ONE shared context.
        nonlocal next_idx, group_counter
        room = max_windows - len(sessions)
        if len(cards) > room:
            print(f"[control] clamping tabgroup {len(cards)} -> {room} "
                  f"(max {max_windows})")
            cards = cards[:room]
        if not cards:
            return
        g = group_counter
        group_counter += 1
        grp = spawn_tabgroup(browser, next_idx, cards)
        next_idx += len(grp)
        for s in grp:
            s.group = g
            sessions.append(s)
            pts = (s.rec or {}).get("exposed_offer_points")
            print(f"[control] tabgroup g{g} idx={s.idx} card={s.card} pts={pts} "
                  f"failed={s.failed} msgId={(s.last_messageId or '')[:8]}")
        write_state(CTRL_STATE_FILE, sessions, pid)

    if getattr(args, "card", None):
        c = normalize_card(args.card)
        if c:
            do_spawn(c, 1)

    deadline = time.time() + CONTROL_TIMEOUT_SEC
    stop = False
    try:
        while not stop and time.time() < deadline:
            for cmd in read_new_commands(CTRL_CMD_FILE, seen_ids):
                c = cmd.get("cmd")
                if c == "spawn":
                    card = normalize_card(cmd.get("card"))
                    if not card:
                        print(f"[control] bad card: {cmd.get('card')}")
                        continue
                    do_spawn(card, max(1, int(cmd.get("n", 1))))
                elif c in ("twotab", "tabgroup"):
                    cards = [x for x in (normalize_card(v)
                                         for v in cmd.get("cards", [])) if x]
                    if not cards:
                        print(f"[control] tabgroup: no valid cards in "
                              f"{cmd.get('cards')}")
                        continue
                    do_tabgroup(cards)
                elif c == "expose":
                    tgt = cmd.get("idx")
                    for s in sessions:
                        if tgt in (None, "all") or s.idx == tgt:
                            reexpose_session(s)
                elif c == "release":
                    tgt = cmd.get("idx")
                    if tgt in (None, "all"):
                        stop = True
                    else:
                        for s in list(sessions):
                            if s.idx == tgt:
                                # If other tabs share this context (two-tab
                                # group), close only this tab; close the context
                                # only when this is its last session.
                                siblings = [o for o in sessions if o is not s
                                            and o.context is s.context]
                                try:
                                    if siblings:
                                        s.page.close()
                                    else:
                                        s.context.close()
                                except Exception:
                                    pass
                                sessions.remove(s)
                                print(f"[control] released idx={tgt}")
            for s in list(sessions):
                if s.failed:
                    continue
                mid = extract_message_id(s.current_url())
                if mid and mid != s.last_messageId:
                    print(f"[control] idx={s.idx} navigated to {mid[:8]}; "
                          f"re-exposing")
                    reexpose_session(s)
            write_state(CTRL_STATE_FILE, sessions, pid)
            time.sleep(CONTROL_POLL_SEC)
    except KeyboardInterrupt:
        pass
    finally:
        seen_ctx = set()
        for s in sessions:
            if id(s.context) in seen_ctx:  # close each shared context only once
                continue
            seen_ctx.add(id(s.context))
            try:
                s.context.close()
            except Exception:
                pass
        try:
            browser.close()
        finally:
            pw.stop()
        try:
            Path(CTRL_STATE_FILE).unlink()
        except FileNotFoundError:
            pass
    print("[control] holder stopped.")


# ==========================
# Modes
# ==========================

def mode_single(args):
    """Test one entry with no VPN. --single-google "term" or --single-url <url>."""
    if args.single_google:
        entry = {"card": "business_platinum", "target": 0, "method": "google",
                 "url": None, "query": args.single_google, "ref_code": None}
    else:
        card = card_from_url(args.single_url) or "unknown"
        method = "referral" if "/referral/" in args.single_url else "direct"
        entry = {"card": card, "target": 0, "method": method,
                 "url": args.single_url, "query": None,
                 "ref_code": ref_code_from_url(args.single_url)}

    class _S:
        hostname = "no-vpn"

    with sync_playwright() as pw:
        browser = build_browser(pw, args.headless)
        ctx, fp_meta = new_session(browser)
        rec, page = run_attempt(ctx, entry, "none", _S())
        rec.update(fp_meta)
        log_line(rec)
        print(json.dumps({k: rec[k] for k in (
            "method", "landing_url", "apply_url_final", "page_status",
            "as_high_as_detected", "exposure_method", "exposed_offer_text",
            "exposed_offer_points", "print_url", "error")}, indent=2))
        if rec.get("exposed_offer_text"):
            try:
                show_offer_banner(ctx.new_cdp_session(page), rec)
                page.screenshot(path=str(BANNER_SHOT))
                print(f"[INFO] banner screenshot -> {BANNER_SHOT}")
            except Exception as e:
                print(f"[WARN] banner: {e}")
        if args.keep_open:
            input("ENTER to close...")
        ctx.close()
        browser.close()


def mode_scan(args):
    random.seed(RANDOM_SEED)
    matrix = build_entry_matrix(args.methods, args.card)
    if not matrix:
        print("[FATAL] No entry points.")
        return
    print(f"[INFO] {len(matrix)} entry point(s); methods={args.methods}; "
          f"cards={sorted(set(e['card'] for e in matrix))}")

    df = get_server_list()
    if df is None or df.empty:
        print("[FATAL] No NordVPN server list.")
        return

    log_event("run_start", cities=args.cities, servers_per_city=args.servers,
              entry_count=len(matrix), methods=args.methods,
              simulate=args.simulate_qualify)

    pw = sync_playwright().start()
    browser = build_browser(pw, args.headless)
    # Residential baseline for the exit-IP guard. Disconnect any stale VPN
    # first so the baseline is genuinely the home IP, not a leftover exit.
    nordvpn_disconnect_quiet()
    time.sleep(2)
    home_ip = get_exit_ip()
    log_event("baseline_ip", ip=home_ip)
    if not home_ip:
        print("[WARN] could not learn the residential baseline IP; "
              "exit-IP guard will be inactive this run.")
    vpn_connected = False
    nordvpn_failures = 0
    attempts = 0
    consec_fail = 0
    cooldown_streak = 0
    t_start = time.time()
    winner = None

    try:
      while not winner:
        cities = list(args.cities)
        if args.hunt:
            random.shuffle(cities)
        for city in cities:
            print(f"\n=== City: {city} ===")
            df_city = find_servers_matching_city(df, city)
            if df_city.empty:
                print(f"[WARN] No servers for {city}")
                continue
            n_sv = min(args.servers, len(df_city))
            indices = (random.sample(range(len(df_city)), n_sv)
                       if args.hunt else range(n_sv))
            for idx in indices:
                server = df_city.iloc[idx]
                print(f"[VPN] connect {server.hostname}")
                res = nordvpn_connect(server)
                if res == "dedicated":
                    continue  # benign server-list artifact, not a VPN failure
                if res is not True:
                    nordvpn_failures += 1
                    if nordvpn_failures >= NORDVPN_FAILURE_ABORT_THRESHOLD:
                        _fatal_abort("nordvpn_failures", nordvpn_failures,
                                     attempts)
                        return
                    continue
                nordvpn_failures = 0  # threshold counts CONSECUTIVE failures
                vpn_connected = True
                time.sleep(SETTLE_AFTER_CONNECT_SEC)
                # Exit-IP guard: confirm the exit actually left the residential
                # baseline before loading Amex (a connect that reported success
                # but left routing on the home IP breaches the VPN-only rule).
                # One call: get_exit_ip already walks several providers, and a
                # second full walk would cost another 24s in the hot loop.
                cur_ip = get_exit_ip()
                if home_ip and cur_ip == home_ip:
                    print(red(f"[GUARD] exit IP {cur_ip} is the residential "
                              "baseline; treating as a VPN failure."))
                    log_event("exit_ip_guard_trip", ip=cur_ip)
                    nordvpn_disconnect_quiet()
                    vpn_connected = False
                    nordvpn_failures += 1
                    if nordvpn_failures >= NORDVPN_FAILURE_ABORT_THRESHOLD:
                        _fatal_abort("exit_ip_guard", nordvpn_failures,
                                     attempts)
                        return
                    continue
                if home_ip and cur_ip is None:
                    print("[GUARD] exit-IP check inconclusive (no provider "
                          "answered); nordvpn reports connected, proceeding.")
                log_event("vpn_connect", city=city, server=server.hostname,
                          exit_ip=cur_ip)

                # Stay on this exit for several attempts before rotating, to
                # avoid the NordVPN failure surface and connect/settle overhead
                # of rotating every attempt.
                #
                # The original rationale was that exit IP is flat for the draw.
                # That was wrong (2026-08-08): the offer tracks the exit /24,
                # and consecutive draws inside one exit session agree 81-82% of
                # the time against ~60% expected by chance. So repeat attempts
                # on one exit mostly re-draw the same tier, and a hunt for the
                # Gold 200k is better served by reaching a block that can serve
                # it at all than by more attempts on one that cannot. Left at
                # the reliability-driven default; lower --rotate-every to trade
                # connect overhead for block coverage.
                passes = max(1, args.rotate_every) if args.hunt else 1
                stop_server = False
                for _pass in range(passes):
                    for entry in matrix:
                        ctx = rec = page = None
                        # One retry on transient failures (Google miss, slow load).
                        for tryi in range(2):
                            ctx, fp_meta = new_session(browser, city)
                            rec, page = run_attempt(ctx, entry, city, server)
                            rec.update(fp_meta)
                            rec["try"] = tryi
                            if args.simulate_qualify and attempts == 0:
                                rec["qualified"] = True
                                rec["exposed_offer_text"] = "(simulated) Earn 300,000 Points"
                            attempts += 1
                            elapsed = time.time() - t_start
                            rec["hunt_attempt"] = attempts
                            rec["hunt_elapsed_sec"] = round(elapsed, 1)
                            will_retry = (not rec["qualified"] and is_retryable(rec)
                                          and tryi == 0)
                            log_line(rec)
                            print(f"  #{attempts} ({elapsed / 60.0:.1f}m) "
                                  + summarize(rec) + (" [retrying]" if will_retry else ""))
                            if not will_retry:
                                break
                            ctx.close()
                            ctx = None
                            time.sleep(3)
                        if rec["qualified"]:
                            winner = (ctx, page, rec)
                            stop_server = True
                            break
                        if ctx:
                            ctx.close()
                        # Track the CTA-less "off" window: reset on a clean page,
                        # count non-ok pages, and cool down when Amex is blocking.
                        if rec.get("page_status") == "ok":
                            consec_fail = 0
                            cooldown_streak = 0
                        else:
                            consec_fail += 1
                        if args.max_attempts and attempts >= args.max_attempts:
                            stop_server = True
                            break
                        if args.hunt and consec_fail >= COOLDOWN_AFTER_FAILS:
                            cd = min(COOLDOWN_SEC * (2 ** cooldown_streak),
                                     COOLDOWN_MAX_SEC)
                            cooldown_streak += 1
                            print(f"[COOLDOWN] {consec_fail} straight blocked pages; "
                                  f"pausing {cd}s to let Amex recover "
                                  f"(streak {cooldown_streak}).")
                            log_event("cooldown", consec_fail=consec_fail,
                                      seconds=cd, attempt=attempts,
                                      streak=cooldown_streak)
                            if vpn_connected:
                                nordvpn_disconnect_quiet()
                                vpn_connected = False
                            time.sleep(cd)
                            consec_fail = 0
                            stop_server = True
                            break
                        human_delay()
                    if stop_server:
                        break

                if winner or (args.max_attempts and attempts >= args.max_attempts):
                    break
                if vpn_connected:
                    nordvpn_disconnect_quiet()
                    vpn_connected = False

            if winner or (args.max_attempts and attempts >= args.max_attempts):
                break
        # Non-hunt: one sweep then stop. Hunt: keep cycling until a winner.
        if not args.hunt or (args.max_attempts and attempts >= args.max_attempts):
            break
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted.")
    finally:
        if winner:
            _, page, rec = winner
            handoff(page, rec, hunt=args.hunt, attempts=attempts,
                    elapsed_sec=round(time.time() - t_start, 1))
            try:
                winner[0].close()
            except Exception:
                pass
        elif vpn_connected:
            nordvpn_disconnect_quiet()
        log_event("run_end", attempts=attempts,
                  elapsed_sec=round(time.time() - t_start, 1), found=bool(winner))
        try:
            browser.close()
        finally:
            pw.stop()

    if not winner:
        print("[INFO] No qualifying offer found. See attempts.jsonl.")


# ==========================
# Self-learning experiment
# ==========================

EXPERIMENT_TIMEZONES = ["America/New_York", "America/Chicago",
                        "America/Denver", "America/Los_Angeles"]

# Curated, geographically spread pool (intersected with what Nord actually
# offers at runtime). Fewer levels than "all US cities" so each gets enough
# samples for the per-city test to have power.
CURATED_CITIES = ["New York", "Atlanta", "Miami", "Chicago", "Dallas",
                  "Houston", "Denver", "Phoenix", "Los Angeles", "Seattle",
                  "San Francisco", "Salt Lake City"]

EXPERIMENT_DWELLS = [0, 4, 10]


def us_cities_from_df(df, min_servers=3):
    from collections import Counter
    cnt = Counter()
    for locs in df.get("locations", []):
        if not isinstance(locs, list):
            continue
        for loc in locs:
            try:
                co = loc.get("country", {})
                if co.get("name") != "United States":
                    continue
                name = (co.get("city") or {}).get("name")
                if name:
                    cnt[name] += 1
            except Exception:
                pass
    return {c for c, n in cnt.items() if n >= min_servers}


def mode_experiment(args):
    random.seed()  # nondeterministic factor sampling
    no_vpn = getattr(args, "no_vpn", False)
    if no_vpn:
        df = None
        cities = ["residential"]
        print("[INFO] experiment WITHOUT VPN: running on the current "
              "(residential) IP, no city rotation")
    else:
        df = get_server_list()
        if df is None or df.empty:
            print("[FATAL] No NordVPN server list.")
            return
        available = us_cities_from_df(df, min_servers=3)
        cities = [c for c in CURATED_CITIES if c in available] or sorted(available)
        print(f"[INFO] experiment city pool ({len(cities)}): {cities}")

    pw = sync_playwright().start()
    browser = build_browser(pw, args.headless)
    profiles = experiment_profiles(pw)
    print(f"[INFO] {len(profiles)} viewport/device profiles: "
          f"{[p['name'] for p in profiles]}")
    cards = ["business_platinum", "business_gold"]
    log_event("experiment_start", cities=cities, trials_target=args.trials)
    trial = 0
    try:
        while not (args.trials and trial >= args.trials):
            card = random.choice(cards)
            tz = random.choice(EXPERIMENT_TIMEZONES)
            dwell = random.choice(EXPERIMENT_DWELLS)

            if no_vpn:
                city = "residential"
                server = None
                exit_ip = get_exit_ip()
            else:
                city = random.choice(cities)
                servers = find_servers_matching_city(df, city)
                if servers.empty:
                    continue
                server = servers.iloc[random.randrange(len(servers))]
                if nordvpn_connect(server) is not True:
                    continue  # failure or dedicated-IP skip: try another draw
                time.sleep(SETTLE_AFTER_CONNECT_SEC)
                exit_ip = get_exit_ip()
            # Direct product-URL entry (no Google) so mobile UAs don't trip
            # Google's CAPTCHA and entry method is held constant across all
            # viewport/device profiles. The experiment varies viewport/device.
            entry = {"card": card, "target": CARD_TARGETS[card],
                     "method": "direct", "url": PRODUCT_URLS[card],
                     "query": None, "ref_code": None, "dwell_s": dwell}
            profile = random.choice(profiles)

            ctx = rec = None
            for tryi in range(2):
                ctx, fp_meta = new_session_profile(browser, pw, profile, tz)
                # Experiment always exposes (force_expose) so each trial is an
                # independent confirmation of the code->offer mapping, not a
                # table lookup. This is the drift/validation check at scale.
                rec, page = run_attempt(ctx, entry, city, server, force_expose=True)
                rec.update(fp_meta)
                rec.update({"trial": trial, "exit_ip": exit_ip,
                            "dwell_s": dwell, "try": tryi})
                if rec["qualified"] or not is_retryable(rec):
                    break
                ctx.close()
                ctx = None
                time.sleep(3)
            log_line(rec)
            pts = rec.get("exposed_offer_points")
            print(f"  #{trial} {city[:10]:10} {profile['name'][:16]:16} "
                  f"{card.split('_')[1]:8} "
                  f"ip={exit_ip} -> {pts}"
                  + ("  !!QUALIFIES" if rec['qualified'] else "")
                  + (f"  ({rec['error']})" if rec.get('error') else ""))
            if rec["qualified"]:
                log_event("experiment_qualifier", trial=trial, card=card,
                          city=city, exit_ip=exit_ip,
                          offer=rec.get("exposed_offer_text"))
                print(red(bold(f"  *** QUALIFIER: {card} {rec.get('exposed_offer_text')} "
                               f"city={city} ip={exit_ip} ***")))
            if ctx:
                ctx.close()
            if not no_vpn:
                nordvpn_disconnect_quiet()
            trial += 1
            time.sleep(random.uniform(DELAY_MIN_SEC, DELAY_MAX_SEC))
    except KeyboardInterrupt:
        print("\n[INFO] Experiment interrupted.")
    finally:
        nordvpn_disconnect_quiet()
        log_event("experiment_end", trials=trial)
        try:
            browser.close()
        finally:
            pw.stop()
    print(f"[INFO] Experiment done after {trial} trials. See {LOG_PATH.name}.")


# ==========================
# CLI
# ==========================

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Amex elevated-offer scanner")
    p.add_argument("--single-url", help="Test one product/apply URL (no VPN)")
    p.add_argument("--single-google", help="Test the Google->organic->apply flow (no VPN)")
    p.add_argument("--keep-open", action="store_true")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--simulate-qualify", action="store_true")
    p.add_argument("--methods", nargs="+", default=["google"],
                   choices=["google", "referral", "direct"])
    p.add_argument("--cities", nargs="+", default=CITIES)
    p.add_argument("--servers", type=int, default=NUM_SERVERS_PER_CITY)
    p.add_argument("--max-attempts", type=int, default=0)
    p.add_argument("--rotate-every", type=int, default=10,
                   help="hunt: attempts per VPN server before rotating "
                        "(fewer rotations = fewer NordVPN failure modes, but "
                        "draws are sticky within an exit, so a lower value "
                        "buys more exit-block coverage)")
    p.add_argument("--card", choices=["business_platinum", "business_gold"],
                   help="Restrict the hunt to one card")
    p.add_argument("--hunt", action="store_true",
                   help="Detached handoff: on a hit, hold the browser open via a "
                        f"marker file ({WINNER_MARKER}) instead of a prompt")
    p.add_argument("--experiment", action="store_true",
                   help="Self-learning loop: randomize all factors, collect data")
    p.add_argument("--trials", type=int, default=0,
                   help="Experiment: stop after N trials (0 = until killed)")
    p.add_argument("--control", action="store_true",
                   help="Interactive BBT/multi-session control mode (no VPN)")
    p.add_argument("--max-windows", type=int, default=CONTROL_MAX_WINDOWS,
                   help="Cap on concurrent control sessions/windows")
    p.add_argument("--no-vpn", action="store_true",
                   help="Experiment: skip NordVPN, run on the current "
                        "(residential) IP, no city rotation")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.dry_run:
        for e in build_entry_matrix(args.methods, args.card):
            print(f"  {e['card']:18} {e['method']:9} target={e['target']:>7} "
                  f"{e['query'] or e['url']}")
        return
    if args.single_url or args.single_google:
        mode_single(args)
        return
    if args.experiment:
        mode_experiment(args)
        return
    if args.control:
        mode_control(args)
        return
    mode_scan(args)


if __name__ == "__main__":
    main()
