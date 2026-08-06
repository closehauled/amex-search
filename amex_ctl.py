#!/usr/bin/env python3
"""Thin CLI to drive the amex_scanner.py --control holder via files in a
private per-user runtime dir (must match the holder's AMEX_RUNTIME_DIR).
Stdlib only; starts instantly; does no browser work."""
import argparse
import json
import os
import sys
import uuid
from pathlib import Path

RUNTIME_DIR = Path(os.environ.get("AMEX_RUNTIME_DIR",
                                  str(Path.home() / ".cache" / "amex-ctl")))
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
CMD = RUNTIME_DIR / "amex_ctl_cmd"
STATE = RUNTIME_DIR / "amex_ctl_state.json"
ALIASES = {"platinum": "business_platinum", "plat": "business_platinum",
           "business_platinum": "business_platinum",
           "gold": "business_gold", "business_gold": "business_gold"}


def emit(obj):
    obj["id"] = uuid.uuid4().hex
    with open(CMD, "a") as f:
        f.write(json.dumps(obj) + "\n")


def cmd_spawn(a):
    card = ALIASES.get(a.card.lower())
    if not card:
        print(f"unknown card: {a.card} (use platinum|gold)")
        sys.exit(2)
    emit({"cmd": "spawn", "card": card, "n": a.n})
    print(f"queued: spawn {card} x{a.n}")


def cmd_twotab(a):
    card = ALIASES.get(a.card.lower())
    if not card:
        print(f"unknown card: {a.card} (use platinum|gold)")
        sys.exit(2)
    cards = [card] * a.count
    emit({"cmd": "twotab", "cards": cards})
    print(f"queued: twotab {card} x{a.count} in ONE shared session "
          f"(submit one tab, then the next)")


def cmd_status(a):
    try:
        st = json.loads(STATE.read_text())
    except FileNotFoundError:
        print("holder not running (no state file)")
        return
    sess = st.get("sessions", [])
    print(f"holder pid {st.get('holder_pid')}  updated {st.get('updated')}  "
          f"sessions {len(sess)}  (tabs sharing a 'grp' = one browser session)")
    print(f"{'idx':>3} {'grp':>3} {'code':>6} {'points':>8} {'q':>2} "
          f"{'method':18} title")
    for s in sess:
        pts = s.get("points")
        pts = f"{pts:,}" if isinstance(pts, int) else "-"
        q = "Y" if s.get("qualified") else ("F" if s.get("failed") else ".")
        grp = s.get("group")
        grp = str(grp) if grp is not None else "-"
        code = s.get("offer_code") or "-"
        print(f"{s.get('idx'):>3} {grp:>3} {str(code):>6} {pts:>8} {q:>2} "
              f"{str(s.get('method')):18} {s.get('window_title', '')}")


def _idx_arg(v):
    return v if v in (None, "all") else int(v)


def cmd_release(a):
    emit({"cmd": "release", "idx": _idx_arg(a.idx)})
    print(f"queued: release {a.idx}")


def cmd_expose(a):
    emit({"cmd": "expose", "idx": _idx_arg(a.idx)})
    print(f"queued: expose {a.idx}")


def main():
    p = argparse.ArgumentParser(prog="amex_ctl")
    sub = p.add_subparsers(dest="c", required=True)
    sp = sub.add_parser("spawn")
    sp.add_argument("card")
    sp.add_argument("n", type=int, nargs="?", default=1)
    sp.set_defaults(fn=cmd_spawn)
    tt = sub.add_parser("twotab", help="open N tabs of one card in ONE shared "
                        "session (two-tab method); default 2")
    tt.add_argument("card")
    tt.add_argument("count", type=int, nargs="?", default=2)
    tt.set_defaults(fn=cmd_twotab)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sr = sub.add_parser("release")
    sr.add_argument("idx", nargs="?", default="all")
    sr.set_defaults(fn=cmd_release)
    se = sub.add_parser("expose")
    se.add_argument("idx", nargs="?", default="all")
    se.set_defaults(fn=cmd_expose)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
