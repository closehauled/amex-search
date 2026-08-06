#!/bin/bash
# Watcher for a second machine: emails you on any terminal hunt event, then exits.
#   winner found  -> "AMEX WINNER" email (submit window is ~15-30 min)
#   fatal abort   -> "hunt ABORTED" email (amex_fatal.json in the scan host's
#                    runtime dir, ~/.cache/amex-ctl unless AMEX_RUNTIME_DIR
#                    overrides it there)
#   process died  -> "hunt DIED" email (no winner, no fatal marker)
# AMEX_VM_HOST is an ssh destination (user@host or an ssh_config alias) with
# key-based auth already set up. AMEX_NOTIFY_CMD is any script that takes
# (subject, body) and sends the alert your way. Run detached:
#   nohup ./winner_watch.sh >/tmp/winner_watch.log 2>&1 &
set -u
VM="${AMEX_VM_HOST:?set AMEX_VM_HOST to the scan VM ssh destination (user@host)}"
SSH_CMD=(ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 "$VM")
NOTIFY="${AMEX_NOTIFY_CMD:?set AMEX_NOTIFY_CMD to a script taking (subject, body)}"
MISSES=0

subject_from_marker() {
python3 - "$1" <<'PY'
import json, sys
try:
    d = json.loads(sys.argv[1])
    pts = d.get("points") or 0
    print(f"AMEX WINNER: {pts:,} pts {d.get('card', '?')} ({d.get('city', '?')})")
except Exception:
    print("AMEX WINNER found")
PY
}

while true; do
  W=$("${SSH_CMD[@]}" 'cat "${AMEX_RUNTIME_DIR:-$HOME/.cache/amex-ctl}/amex_winner.json" 2>/dev/null' 2>/dev/null)
  if [ -n "$W" ]; then
    SUBJ=$(subject_from_marker "$W")
    "$NOTIFY" "$SUBJ" \
"A qualifying offer is HELD OPEN on the VM. Submit it in the live window (VNC or the VM console) within ~15 min of exposure; the session cannot be reopened.

Marker:
$W"
    exit 0
  fi
  F=$("${SSH_CMD[@]}" 'cat "${AMEX_RUNTIME_DIR:-$HOME/.cache/amex-ctl}/amex_fatal.json" 2>/dev/null' 2>/dev/null)
  if [ -n "$F" ]; then
    "$NOTIFY" "Amex hunt ABORTED" \
"The hunt aborted itself (see amex_fatal.json in the runtime dir and the hunt log on the VM).

$F"
    exit 0
  fi
  if "${SSH_CMD[@]}" 'pgrep -f "[a]mex_scanner.py" >/dev/null' 2>/dev/null; then
    MISSES=0
  else
    # Require 3 consecutive misses: VPN churn on the VM blips SSH, and a
    # single failed probe must not read as "hunt died".
    MISSES=$((MISSES + 1))
    if [ "$MISSES" -ge 3 ]; then
      "$NOTIFY" "Amex hunt DIED" \
"Scanner process not found on $VM (3 consecutive checks), with no winner and no fatal marker. Check hunt_gold.log."
      exit 0
    fi
  fi
  sleep 60
done
