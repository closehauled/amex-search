# amex-search

Public mirror of a private working tree, published with fresh git history. The
upstream repo, its history, its logs, and its process docs never ship; changes
are ported here as code, not as wholesale file copies.

## Publish gate (binding)

- Run `python3 scripts/check_publish.py` before every commit and push. Exit 0
  is required but not sufficient: the diff also gets an independent read.
- The gate layers: shape heuristics, an exact-string denylist
  (`scripts/pii-denylist.txt`, gitignored, real strings live only there),
  matcher canaries (a broken scanner exits 2 instead of reporting clean), a
  git publishing-identity check, and an empty-tree guard.
- Without the denylist file the run degrades to heuristics-only and says so.
  Do not read a heuristics-only clean as equivalent to a full run: a name,
  handle, or token inside base64 has no distinctive shape.
- The pre-push hook (`.githooks/pre-push`, installed via
  `git config core.hooksPath .githooks`) refuses any push that fails the gate,
  and refuses even a clean push unless `PUBLISH_APPROVED=1` is set. Pushes
  happen only with the owner's explicit per-push approval.
- Never add a value to `scripts/publish-allowlist.json` to silence a finding
  you have not proven synthetic.

## Identity

All git activity in this repo uses the repo-local identity `closehauled
<closehauled@users.noreply.github.com>` and the SSH alias
`github.com-closehauled`. The gate fails closed if config or history carries
anything else. Remote: `git@github.com-closehauled:closehauled/amex-search.git`.

## Conventions

- Referral machinery stays, but the only `ref=` value ever committed is the
  `YOUR_REF_CODE` placeholder; real links live in the gitignored
  `amex-referrals.txt`.
- Session tokens in tests are fabricated (see `syntheticTokens` in the
  allowlist); never paste a value from a real browser session.
- WORKLOG.md and docs/plans/ are local-only (gitignored).
