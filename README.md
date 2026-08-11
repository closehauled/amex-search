# Amex Elevated-Offer Scanner

Finds American Express Business Platinum (300k) and Business Gold (200k) elevated
sign-up offers by exposing the real bonus hidden behind Amex's "as high as"
language, across rotating NordVPN locations.

Amex now shows "as high as 300,000" on the offer headline instead of a fixed
number; the real bonus is only in the Print Terms document. This tool reaches the
official card page the way a person does (Google the card, follow the first
organic result, click Apply), reveals the actual offer, logs every attempt, and
on a qualifying hit disconnects the VPN and leaves the browser open with an
on-page banner so you can submit on your normal IP.

## What it does

- Rotates NordVPN cities, fresh anonymous session per attempt (several
  attempts per server via `--rotate-every`; exit IP is verified against the
  residential baseline before any Amex page loads)
- Reaches the apply page via the Google search method (referral entry is
  supported but, in testing, gave worse offers, see below)
- Exposes the real offer behind "as high as" by reading the Print Terms
  document with the offer-detail URL parameters flipped (no browser extension)
- Logs every attempt to `attempts.jsonl` and shows a colored on-page banner with
  the real offer; failed or unparsable pages save bounded forensics
  (screenshot + body snippet) to `failures/`
- Rides out anti-bot "off" windows with an escalating cooldown instead of
  hammering a wall; a fatal abort is loud (marker file + desktop notification)
- Stop-and-handoff: on a qualifying offer it disconnects the VPN, zooms the
  page to fit the display, shows a banner with a hold timer, saves a full-page
  proof capture, and keeps the winning browser session open for off-VPN
  submission

## Key finding

Randomized-factor experiments (~950 trials total; the corrected write-up is in
[amex-stats-summary.md](amex-stats-summary.md), the original report in
[docs/experiment-report.md](docs/experiment-report.md) is superseded on this
point) found that **a large desktop viewport is the strongest setting under your
control**: 1920x1080 and 2560x1440 both pulled the Platinum 300k at
roughly 56-60%, vs 23% and below for 1536-wide and smaller, and 0% on mobile
(Fisher p=2.3e-11). The scanner fixes the viewport at 1920x1080. VPN city,
timezone, UA platform, and dwell time all tested flat.

**Exit IP is not flat** (revised 2026-08-08; it had been reported as flat, and
that reading came from runs that logged no exit IP, a /16 grouping in
`analyze.py`, and a 175k-or-better threshold that reverses the effect). At the
/24 grain, NordVPN's Panama-registered 2025-03-13 capacity returned **zero
Business Gold 200k in 339 draws** while every other block returned it at
roughly 20-31%. Those blocks serve only 175k or 100k, so they score *better*
on 175k-or-better and lose absolutely at 200k. Use the block attribute rather
than a list of blocks: a five-block hot list went stale in a day. Separately, the
**5-digit code in the apply URL deterministically identifies the offer tier**
(813/813 draws, zero drift), so a known code is read instantly and the Print
Terms exposer serves as fallback and pre-submit confirmation. Referral-link
entry pulled the bottom offer tier every time and never the top bonus, so it is
off by default.

## Requirements

- Python 3.12+ with `playwright`, `pandas`, `requests` (and `selenium` >= 4.6
  for the two legacy Selenium scripts only, `amex-300k-6.py` and `diagnose.py`,
  which rely on that version fetching its own chromedriver; `pytest` if you
  want to run `test_control_helpers.py`)
- Chromium via Playwright, **plus its system libraries** (see setup, step 3)
- NordVPN CLI, logged in, with `nordvpn set lan-discovery on`
- A Linux host with a display for the headed browser (the handoff needs a
  visible window). The X screen must be at least as large as the browser
  window, or `Page.captureScreenshot` fails outright: with the default
  1920x1080 target viewport, use a 1920x1080-or-larger screen. `Xvfb :1
  -screen 0 1920x1200x24` works for a headless host.

## Setup

Verified on a clean Ubuntu 24.04 LTS server. Steps 1 and 3 are easy to miss and
the tool cannot start without either.

1. System packages (a stock Ubuntu 24.04 image ships no `pip` and no `venv`):

```
sudo apt update && sudo apt install -y python3-pip python3-venv git
```

2. Python dependencies. Ubuntu 24.04 marks its Python as externally managed
   (PEP 668), so a bare `pip install` is refused. Use a virtualenv:

```
python3 -m venv .venv && . .venv/bin/activate
pip install playwright pandas requests
```

3. The browser **and its system libraries**. `playwright install chromium`
   alone downloads a browser that cannot launch (it dies on a missing
   `libatk-1.0.so.0`); `install-deps` is what pulls the ~40 shared libraries
   Chromium needs:

```
playwright install chromium
sudo .venv/bin/playwright install-deps chromium
```

4. Create your referral file (optional, referral entry is off by default):
   `cp amex-referrals.txt.example amex-referrals.txt` and add your links.

## Usage

Hunt continuously for a qualifying offer and hold the winner open for
submission (headed, on the machine's display):

```
DISPLAY=:0 python amex_scanner.py --hunt --card business_gold --methods direct --cities Denver Houston Phoenix --servers 1 --rotate-every 10
```

Runtime markers (winner/fatal/release/screenshots and the control channel)
live in a private per-user dir, `~/.cache/amex-ctl` by default, overridable
with `AMEX_RUNTIME_DIR` (all cooperating processes must see the same value).
On a hit the hunt writes `amex_winner.json` there, disconnects the VPN, and
holds the live apply page (release by creating `amex_release` there). A fatal
abort writes `amex_fatal.json`. `winner_watch.sh` (run detached on a second
machine, with `AMEX_VM_HOST` set to the scan host's ssh destination and
`AMEX_NOTIFY_CMD` to any script taking a subject and body) emails on
winner/abort/death.

Other modes:

```
python amex_scanner.py --single-google "amex business platinum"   # one-shot test (VPN must be up)
python amex_scanner.py --dry-run --methods google                 # show the entry matrix
python amex_scanner.py --experiment --trials 150                  # randomized-factor data collection
python amex_scanner.py --control [--headless]                     # BBT/multi-session holder (drive with amex_ctl.py)
python analyze.py                                                 # per-factor top-tier hit-rate analysis
```

## Files

- `amex_scanner.py` - the scanner (search/hunt/experiment/control/handoff)
- `amex_ctl.py` - CLI for the `--control` holder (spawn/status/release/expose)
- `analyze.py` - per-factor top-tier hit-rate analysis of `attempts.jsonl`
- `ab_test.py` - paired Google-vs-referral comparison helper
- `context_ab_test.py` - browser-context A/B diagnostic for `no_apply_cta`
  walls (stealth init script / UA spoof variants over one VPN exit)
- `probe_check.py` - live probe of which `APPLY_SELECTORS` match on the Business
  Gold product page, and what the closest interactive ancestor of "Apply Now"
  is; the first thing to run when `no_apply_cta` returns after a redesign
- `portability_test.py` - tests whether an exposed application session survives
  leaving the VPN (expose on VPN, save `storage_state`, disconnect, reopen the
  apply URL in a fresh off-VPN context)
- `validate_mobile.py` - single attempt under a named device profile
  (default `iPhone 14 Pro Max`); prints the exposure record as JSON
- `winner_watch.sh` - detached watcher that emails on winner/abort/death
- `vm-tools/` - full-page capture helpers for the scan host (`fullcap`,
  `webapp`, `pagecap.py`)
- `amex-300k-6.py` - legacy Selenium version (kept for reference)
- `diagnose.py` - legacy Selenium page-load diagnostic (reads the first link in
  `amex-referrals.txt`; superseded by `probe_check.py`)
- `browser_paths.py` - finds a browser and a driver for the two Selenium
  scripts and `vm-tools/webapp`. It ranks every candidate (env overrides,
  `PATH`, distro and vendor install locations, snap, flatpak, and the Chromium
  Playwright installed for this repo) and then **runs each one before
  returning it**, so a binary that exists but cannot start loses to one that
  works. Recovery is preferred to failure at every step: a driver that cannot
  drive the browser is retried with one downloaded to match it, and a machine
  with no usable browser is repaired with `playwright install chromium`, which
  needs no root and works on any distribution. When nothing can be repaired
  the error names each candidate and why it failed, the missing library if
  that was the cause, and the install command for the distribution actually
  running. Overrides: `AMEX_CHROMIUM` and `AMEX_CHROMEDRIVER` (a full path or
  a bare name), `AMEX_BROWSER_NO_AUTO_INSTALL`, `AMEX_BROWSER_NO_VALIDATE`.
  Run `python browser_paths.py --list` to see the ranking with each
  candidate's status, or `--selftest` to prove the choice by driving it.
  Verified on Ubuntu 24.04, Debian 12, Fedora 41, Arch, Alpine 3.20 and
  openSUSE Leap
- `test_control_helpers.py` - pure-helper unit tests, run with `pytest` (no
  playwright needed)
- `test_control_nav.py` - despite the name, **not a pytest suite**: it is a
  live integration script (`python test_control_nav.py`) that drives real
  Amex apply pages through the control-mode navigation pipeline. It needs
  playwright and a working VPN, and it collects zero tests under `pytest`
- `scripts/check_publish.py` - PII publish gate (heuristics + local denylist
  + canaries); wired into `.githooks/pre-push` via
  `git config core.hooksPath .githooks`
- `amex-stats-summary.md` - corrected findings write-up (viewport + URL code)
- `docs/experiment-report.md` - original experiment write-up (superseded on
  the viewport-range conclusion, see header note)

## Privacy

Your real referral links, run logs, and experiment data are gitignored. Only the
code, templates, and docs are tracked. Review `.gitignore` before pushing
anywhere public.

## Disclaimer

This is a personal research and automation tool. Use it in accordance with
American Express, Google, and NordVPN terms of service.

## Credits

The offer-unmask engine (the `isAhaVariant`/`showExactOffer` flip on the Print Terms URL, read via a same-origin iframe or a cross-origin fetch) is a port of **Todd's (toddrob99) "AHA Exposer"** userscript. The automation, hunt loop, Back Button Trick control mode, two-tab method, and viewport experiment are built on top of it.

## TODO

- Identify the `no_apply_cta` wall trigger from the next wall's `failures/` artifacts (stealth-context and simple-pace hypotheses both disproven; trigger is stateful/temporal on Amex's side) #med
- Validate the four externally-sourced Platinum codes (57460/65147/73113/73165=200k) by actually drawing and exposing them; they are added to the table but unconfirmed in our own data #med
- Re-check `portability_test.py`: it completes and prints a verdict, but both probes returned empty (`offer_section_present: {}`), so the "NOT portable" conclusion rests on falsy-empty rather than a real negative #med
- Exercise `amex-300k-6.py` end to end at least once; only its browser discovery and parsing have been verified #low
- Exercise `winner_watch.sh` beyond config validation (needs a second machine for the poll/notify loop), and `vm-tools/fullcap` on a real desktop #low
- Optional: `amex_ctl decode <url>` helper and an on-demand drift-check mode #low

## Changelog highlights

- Apply-click hardening for the 2026-08 product-page redesign: a force-click
  fallback for the continuously-animating button Playwright never sees as
  stable, plus up to three click attempts per page load so a click that beats
  hydration (403 on the sessionless apply URL) is retried after a reload
- Exit-IP guard reads from three providers instead of ipify alone, and rejects
  non-IPv4 answers; DNS filtering of a single provider had been silently
  leaving the VPN-only guard inactive for a whole run
- NordVPN calls use argv lists with server-prefix validation (no `shell=True`); all runtime markers and the control channel moved from `/tmp` to `~/.cache/amex-ctl` (0700, `AMEX_RUNTIME_DIR` override) across the scanner, `amex_ctl.py`, `winner_watch.sh`, and `fullcap`
- Playwright is now optional at import time, so the pure helpers and `test_control_helpers.py` run without it
- Exit-IP guard, loud parse_error status, consecutive-only NordVPN failure counting with dedicated-IP skips excluded, escalating cooldown, `--rotate-every`, failure forensics, handoff auto-zoom/hold-clock/proof-capture, `winner_watch.sh` email alerts
- Overnight 500-trial VPN run validated the code-to-offer mapping at scale (436/436 `code_offer_match`, 813/813 total, zero drift, zero new codes)
- Added four externally-sourced Platinum codes (incl. new 200k rung 73165), flagged as not-yet-self-validated
- Viewport finding refined to "large desktop viewport" (1920x1080 and 2560x1440 both ~60%, p=2e-11); reframed around effective viewport / display scaling
- 5-digit apply-URL code = offer (deterministic in all observed draws); implemented code-first lookup with exposer fallback/confirm
- Viewport fixed at 1920x1080 baseline; referral entry evaluated and abandoned (Google is better)
