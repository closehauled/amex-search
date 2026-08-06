# Amex Business Card "As High As" Offer: Findings

Method: an automated tool unmasks the real bonus behind Amex's "as high as 300,000" headline (it reads the exact offer out of the Print Terms document), then runs many fresh cold sessions to measure what drives the offer. Numbers below are from this session's runs (NordVPN cities + residential IP, headless), reported as top-tier hit-rates.

## Headline: a large browser VIEWPORT drives the top offer (not screen size, and scaling matters)

The lever is the browser's reported **viewport** (the page's render area in CSS pixels, `window.innerWidth/Height`), not the physical screen resolution. Those two diverge a lot, and the gap is where most people lose the offer:

- **Windows default display scaling is 125%.** On a real 1920x1080 screen that makes the browser report **1536x864** (1920 / 1.25). In our data 1536x864 pulled the 300k only 23%, vs 53-61% at a true 1920x1080. So a Windows user on a 1080p panel, at the default scaling, is unknowingly in the weak band.
- **Mac Retina** reports a scaled "looks like" resolution (often ~1440 to 1680 CSS px wide) despite a 2560+ physical panel, also weak-to-mid.
- **Browser chrome and a non-maximized window** shrink the reported viewport further.

Actionable version: get the browser's reported viewport to a true ~1920 wide. That means a **1080p-or-larger screen, display scaling set to 100%, and the browser maximized.** Turning off the default scaling is the step most people miss.

Business Platinum 300k hit-rate by viewport (n=133 exposures):

| reported viewport | 300k hits | rate |
|-------------------|-----------|------|
| 2560x1440 | 8/13 | 61% |
| 1920x1080 | 7/13 | 53% |
| 1536x864 (= 1080p at 125% scaling) | 3/13 | 23% |
| 1440x900 | 3/17 | 17% |
| 1366x768 | 0/14 | 0% |
| 1280x800 | 0/12 | 0% |
| mobile/tablet (iPhone SE, iPhone 14 Pro Max, Pixel 7, iPad) | 0/51 | 0% |

Large desktop viewport (1920x1080 + 2560x1440): **15/26 (57%)** vs everything else **6/107 (5%)**. Fisher exact one-tailed p = 9.9e-09. 1920 and 2560 perform the same, so it is "large viewport," not a single magic number.

## How common is a sub-1920 viewport? Common enough to matter.

From StatCounter desktop screen-resolution data (recent): 1920x1080 is the most common reported resolution at roughly 55%, 1366x768 is about 15%, and 1536x864 (the 1080p-at-125%-scaling bucket) is the next largest. So roughly 45% of desktop users already report sub-1920. On top of that, mobile is about half of all web traffic, and every mobile viewport pulled 0% here. The takeaway is not "buy a 4K monitor", it is "stop your OS from scaling your 1080p+ screen down below a 1920 viewport."

## Entry method: search beats referral

From an A/B with viewport fixed at 1920x1080:

| entry | 300k rate | offers seen |
|-------|-----------|-------------|
| Google search to apply page | 40% (2/5) | only 250k or 300k |
| referral link | 0% (0/10) | 150k every time |

Use Google search to reach the apply page, not a referral link.

## Location does not matter

Across 12 VPN cities and ~200 distinct exit IPs (plus the residential IP), city and exit-IP tested flat (city p ~ 0.5 to 0.9, exit-IP p ~ 0.3 to 0.5). VPN location does not steer the offer. Rotate IPs only for anti-bot hygiene, not for a better offer.

## Business Gold

200k is just rare: ~4% per attempt (6/135 this run), with no viewport effect. It is a numbers game of repeated fresh sessions.

## Caveats

- Automated cold sessions; a warm or residential human browser may behave differently.
- Correlation, not proven causation (most likely Amex bucketing by viewport / device class).
- Offers are a discrete ladder (150k / 200k / 250k / 300k), not continuous.
- Viewport-share percentages are StatCounter's reported-resolution data; the exact 1536x864 share sits behind their interactive chart, so it is cited as a rank, not a precise decimal.

## Attribution

The entire offer-unmask engine comes from **Todd's (toddrob99) "AHA Exposer"** userscript (verified against the v2.1 source): detecting "as high as" on the offer section, clicking the Offer Terms trigger, grabbing the Print Terms link, flipping `isAhaVariant=true` to `false` and `showExactOffer=false` to `true`, and reading the real bonus via a hidden same-origin iframe (`#offer-terms`) or a cross-origin fetch fallback. This tool ports that engine, selectors, flip, iframe technique, and regex included, from his in-page userscript to an external automation harness.

The 5-digit apply-URL code is **not** part of Todd's script; it is plainly visible in the apply URL and was noticed anecdotally by a community member.

Original work built on top of the ported engine: the external Playwright harness with a CDP eval-bypass (needed because the tool drives the browser from outside the page, where Amex's disabled `eval` applies; Todd's in-page script is not affected), the fresh-session hunt loop with VPN rotation and JSONL logging, the viewport experiment above, the proven deterministic code-to-offer mapping, and the Back Button Trick / two-tab tooling.
