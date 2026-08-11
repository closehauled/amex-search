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

## The city is flat, the exit BLOCK is not (revised 2026-08-08)

This section previously read "location does not matter", on the basis that across 12 VPN cities and ~200 distinct exit IPs, city and exit-IP both tested flat (city p ~ 0.5 to 0.9, exit-IP p ~ 0.3 to 0.5). **The city half still holds. The exit-IP half was an artifact of how it was measured**, and the Business Gold 200k turns out to be gated by which address block you come out on.

Three verified reasons the original test could not have seen it:

1. The 1,000-draw viewport study recorded **no exit IP at all** (null on every row), so exit IP was never actually tested on that data. Where server identity was recorded, there were 622 distinct servers at a median of 1 draw each, which has almost no power against an effect that lives at the block level.
2. `analyze.py` groups exits at **/16**. The structure sits at **/24**.
3. The reported metric for Business Gold was **175k-or-better**, not 200k. At 175k-or-better the effect **reverses**, so it was not merely invisible, it pointed the other way.

### The rule

Blocks registered to Panama and allocated 2025-03-13, which is NordVPN's 187.13/187.14 capacity, **have never served a 200k**: 0 in 339 combined draws (348 draws on the primary host, replicated on 111 independent probe draws). Every other block served it at roughly 20-31%.

Tier breakdown on 383 draws:

| Tier | PA/2025 blocks | All other blocks |
|------|----------------|------------------|
| 200,000 | 0 (0.0%) | 18 (29.0%) |
| 175,000 | 293 (91.0%) | 18 (29.0%) |
| 150,000 | 0 (0.0%) | 21 (33.9%) |
| 100,000 | 29 (9.0%) | 5 (8.1%) |
| total | 322 | 62 |

At 175k-or-better the PA blocks **win**, 91.0% against 59.0% (p = 6.3e-09), which is exactly why the original reading came out flat-or-better. At 200k they lose absolutely: 0/322 against 18/61 = 29.5% (p = 4.0e-16), replicated on an independent host at 0/42 against 20.3% (p = 8.7e-04).

The shape carries more than the rates. PA blocks returned only two outcomes in 322 draws, 175k or 100k, with no 150k and no 200k. That reads as a restricted offer set rather than a shifted probability.

**So the advice depends on the target.** For a 200k, use non-PA blocks; they are the only ones that have ever served one. If 175k is acceptable, the original guidance stands and the PA blocks are the better endpoints. Those blocks are about 40% of the US fleet by server count but absorb roughly 85% of draws under normal city selection, so the default path lands in them most of the time.

> [!IMPORTANT]
> Use the block **attribute**, not a list of blocks. A five-block "hot list" assembled on 2026-08-07 broke inside a day: two blocks went hot on 2026-08-08 after twelve cold hours. A lookup table of block identities has a shelf life; the registration/allocation rule survived the same period.

### What else this ruled in and out

- **Not a clock.** Two hosts sampling concurrently from different exits disagreed four to nine seconds apart, one returning 200k while the other returned 175k or 100k. No global time window can produce that. A seven-day time-of-day study was running when this landed and it answered the exit question instead of the clock question.
- **Sticky within an exit.** Consecutive draws inside one exit session agreed 81-82% of the time against ~60% expected from each host's own tier mix, and exits revisited in genuinely separate visits (one pair eight hours apart) held their tier. Caveat: paired draws are only ~26s apart, so "sticky per exit" and "sticky per short interval" are not fully separated by that comparison alone; the cross-host disagreement is what rules out the global version.
- **City matters only as a route to a block.** Phoenix 10.6%, Denver 5.1%, New York 3.2%, Los Angeles 2.9%, Miami 0/71 (it reaches no qualifying block). The city itself is not the lever, its block coverage is.
- **Geolocation is ruled out as the mechanism.** Both classes resolve to correct US cities and all are proxy-flagged. The non-US registration codes are RIR registrations of leased address space, not server locations.
- **The mechanism is unresolved.** Registration country, the 2025-03-13 allocation date, and membership in the 187.x supernet select exactly the same blocks in this data. They are perfectly confounded, so this establishes *that* the split predicts, not *which* attribute does the work. Operationally it makes no difference.
- **Blocks are targetable.** NordVPN's API publishes a station IP per server. It is not the exit IP (zero of 191 observed exits matched one exactly), but the /24 generally does match, so the station IP is a usable proxy for which block you will come out on. Spot-checked at 4 of 5; the miss is why the exit IP should be read and classified on every draw rather than trusting the server you asked for.

### Still open

Whether the same block effect gates the Business Platinum 300k is being tested by drawing both cards from the same exit. No result yet.

## Business Gold

200k is rare at ~4% per attempt (6/135 in the original run), with no viewport effect. **Revised 2026-08-08: it is not purely a numbers game.** That ~4% is a blend of two populations. On PA/2025 exit blocks the rate is 0% and no amount of repetition changes it; on every other block it is roughly 20-31%. Repeated fresh sessions still matter, but only once the exit block can serve the offer at all. See the block section above.

The 200k is also more available than the original 1,000-draw study suggested: once exits were being recorded and classified, 15 of them appeared in about two hours, against 12 in that entire study.

## Caveats

- Automated cold sessions; a warm or residential human browser may behave differently.
- Correlation, not proven causation (most likely Amex bucketing by viewport / device class).
- Offers are a discrete ladder (150k / 200k / 250k / 300k), not continuous.
- Viewport-share percentages are StatCounter's reported-resolution data; the exact 1536x864 share sits behind their interactive chart, so it is cited as a rank, not a precise decimal.
- The block finding was discovered by looking at which blocks had produced a 200k, so the first p-values computed on it are descriptive, not predictive: they measure how cleanly the data splits on a boundary drawn around that same data. The independent replication on a separate host and a separate 111-draw probe is what carries it, not the discovery p-value.
- All of this is NordVPN address space. Nothing here says anything about residential or cellular paths, which were never tested.
- The offer landscape moves. Every number here is a snapshot of the run that produced it, and one block list already went stale inside 24 hours.

## Attribution

The entire offer-unmask engine comes from **Todd's (toddrob99) "AHA Exposer"** userscript (verified against the v2.1 source): detecting "as high as" on the offer section, clicking the Offer Terms trigger, grabbing the Print Terms link, flipping `isAhaVariant=true` to `false` and `showExactOffer=false` to `true`, and reading the real bonus via a hidden same-origin iframe (`#offer-terms`) or a cross-origin fetch fallback. This tool ports that engine, selectors, flip, iframe technique, and regex included, from his in-page userscript to an external automation harness.

The 5-digit apply-URL code is **not** part of Todd's script; it is plainly visible in the apply URL and was noticed anecdotally by a community member.

Original work built on top of the ported engine: the external Playwright harness with a CDP eval-bypass (needed because the tool drives the browser from outside the page, where Amex's disabled `eval` applies; Todd's in-page script is not affected), the fresh-session hunt loop with VPN rotation and JSONL logging, the viewport experiment above, the proven deterministic code-to-offer mapping, and the Back Button Trick / two-tab tooling.
