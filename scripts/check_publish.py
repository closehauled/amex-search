#!/usr/bin/env python3
"""Publish gate for this public mirror.

This is the automated half of the gate; the other half is an independent
human/agent read of the full diff. A clean run here is necessary, never
sufficient, for a push.

What it does:
  1. Inventories the files that would actually ship (git ls-files, tracked
     plus untracked-unignored), so gitignored local data is out of scope but
     anything that would ride along in a commit is in scope.
  2. Loads an exact-string denylist from scripts/pii-denylist.txt (gitignored;
     holds the real private strings) and matches every value case-insensitively
     in several encodings: raw, base64, percent-encoded, JSON \\u escapes, and
     hyphen/space-split digit runs.
  3. Runs shape heuristics that need no denylist: email addresses, home
     directory paths, private IP addresses, sshpass invocations, secret-shaped
     assignments, Amex referral ref= tokens, and messageId session tokens.
  4. Verifies every matcher against synthetic canaries BEFORE scanning. A
     scanner that finds nothing and a scanner that is broken produce identical
     output; the canaries tell them apart. Canary failure exits 2, and findings
     from a run that exited 2 mean nothing.
  5. Verifies the git publishing identity: repo-local user.name/user.email and
     every author/committer in history must be a declared publishing identity
     in scripts/publish-allowlist.json. Fail-closed.
  6. Refuses to bless an empty tree (a mistyped --root looks exactly like a
     clean run).

Exit codes: 0 clean, 1 findings, 2 the gate itself is broken or misused.

If the denylist is missing or empty the run degrades to heuristics-only and
says so loudly. Heuristics match shapes; a name, a handle, or a token inside
base64 has no distinctive shape and is findable only by the denylist. Do not
read a heuristics-only clean as equivalent to a full run.
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.parse

SELF_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.dirname(SELF_DIR)

# Scanned-content exemptions, printed at run time. This file holds the canary
# strings and matcher patterns, so it would flag itself; nothing else is exempt.
SCAN_EXEMPT = {"scripts/check_publish.py"}
# The real denylist must never be scanned, printed, or shipped.
DENYLIST_PATH = "scripts/pii-denylist.txt"
ALLOWLIST_PATH = "scripts/publish-allowlist.json"

NOREPLY = re.compile(r"@users\.noreply\.github\.com$|^noreply@|@noreply\.", re.I)


def sh(args, cwd):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    return r.returncode, r.stdout


def inventory(root):
    """Files that would ship: tracked + untracked-unignored per git; falls
    back to a raw walk (minus .git) when the tree is not a repo yet, which
    over-scans rather than under-scans."""
    files = []
    if os.path.isdir(os.path.join(root, ".git")):
        code, out = sh(["git", "ls-files", "-co", "--exclude-standard", "-z"], root)
        if code != 0:
            print("GATE BROKEN: git ls-files failed in", root)
            sys.exit(2)
        files = [f for f in out.split("\0") if f]
    else:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__", ".claude"}]
            for fn in filenames:
                rel = os.path.relpath(os.path.join(dirpath, fn), root)
                files.append(rel)
    return sorted(f for f in files if f != DENYLIST_PATH)


def load_allowlist(root):
    p = os.path.join(root, ALLOWLIST_PATH)
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:
        print(f"GATE BROKEN: cannot load {ALLOWLIST_PATH}: {e}")
        sys.exit(2)


def load_denylist(root):
    p = os.path.join(root, DENYLIST_PATH)
    vals = []
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                vals.append(line)
    return vals


def encodings(value):
    """Every representation of a denylist value we search for."""
    v = value.lower()
    out = {v}
    out.add(base64.b64encode(value.encode()).decode().lower())
    out.add(base64.b64encode(value.lower().encode()).decode().lower())
    q = urllib.parse.quote(value).lower()
    out.add(q)
    out.add("".join("\\u%04x" % ord(c) for c in value.lower()))
    if value.isdigit():
        out.add("-".join(value))
        out.add(" ".join(value))
    return out


def is_binary(path):
    with open(path, "rb") as fh:
        return b"\0" in fh.read(8192)


class Rule:
    def __init__(self, name, rx, allow=None):
        self.name = name
        self.rx = re.compile(rx)
        self.allow = allow or (lambda m: False)


def build_rules(allowlist):
    email_ok = {d.lower() for d in
                allowlist.get("allowedEmailDomains", {}).get("values", [])}
    synth = {s.lower() for s in allowlist.get("syntheticTokens", {}).get("values", [])}

    def email_allowed(m):
        s = m.group(0).lower()
        # git@host is an SSH remote user, not a mailbox
        return s.startswith("git@") or s.rsplit("@", 1)[1] in email_ok

    def token_allowed(m):
        # A prefix only counts when it is long enough to be unambiguous
        # (tokens split across source lines); ref=Y must never pass.
        v = m.group(1).lower()
        return any(v == s or (len(v) >= 8 and s.startswith(v)) for s in synth)

    return [
        Rule("email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", email_allowed),
        Rule("home-path", r"(?:/Users/|/home/)[A-Za-z0-9._-]+"),
        Rule("private-ip",
             r"\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
             r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"),
        Rule("sshpass", r"sshpass\s+-p\s*\S+"),
        Rule("secret",
             r"(?i)\b(?:password|passwd|api[_-]?key|apikey|client[_-]?secret"
             r"|authorization)\b\s*[:=]\s*\S{6,}"),
        Rule("referral-token", r"[?&]ref=([A-Za-z0-9_]{4,})", token_allowed),
        Rule("message-id", r"(?i)messageid=([0-9a-f]{16,})", token_allowed),
    ]


CANARIES = [
    ("email", "contact leak.canary@canary-example.zz today"),
    ("home-path", "log at /Users/canaryuser/x and /home/canaryuser/y"),
    ("private-ip", "host 192.168.77.66 and 10.7.6.5 and 172.31.2.1"),
    ("sshpass", "sshpass -p canaryhunter99 ssh box"),
    ("secret", "password=canaryhunter99secret"),
    ("referral-token", "https://x.example/referral/card?ref=CANARYTOK9x"),
    ("message-id", "apply/x?messageId=0123456789abcdefcafe"),
]


def run_canaries(rules):
    broken = []
    by_name = {r.name: r for r in rules}
    for name, hay in CANARIES:
        r = by_name[name]
        m = r.rx.search(hay)
        if not m or r.allow(m):
            broken.append(name)
    # exercise the denylist machinery itself, including an encoded form
    dval = "canary-denylist-value-9z"
    hay = ("prefix " + dval + " suffix "
           + base64.b64encode(dval.encode()).decode()).lower()
    if not any(v in hay for v in encodings(dval)):
        broken.append("denylist-raw")
    if broken:
        print("GATE BROKEN: matchers failed their own canaries:", ", ".join(broken))
        print("Findings from this run would mean nothing. Fix the gate first.")
        sys.exit(2)


def git_identity_findings(root, allowlist):
    findings = []
    ids = allowlist.get("publishingIdentities", {})
    ok_names = set(ids.get("names", []))
    ok_emails = {e.lower() for e in ids.get("emails", [])}
    if not os.path.isdir(os.path.join(root, ".git")):
        findings.append(("block", "git-identity", "(repo)", 0,
                         "not a git repository; identity cannot be verified"))
        return findings
    for key in ("user.name", "user.email"):
        code, out = sh(["git", "config", key], root)
        val = out.strip()
        if code != 0 or not val:
            findings.append(("block", "git-identity", "(repo)", 0,
                             f"repo-local {key} is not set; a commit would use "
                             f"the global identity"))
        elif key == "user.name" and val not in ok_names:
            findings.append(("block", "git-identity", "(repo)", 0,
                             f"user.name is not a declared publishing identity"))
        elif key == "user.email" and val.lower() not in ok_emails and not NOREPLY.search(val):
            findings.append(("block", "git-identity", "(repo)", 0,
                             f"user.email is not a declared publishing identity"))
    code, out = sh(["git", "log", "--all", "--format=%an%x00%ae%x00%cn%x00%ce"], root)
    if code == 0:
        for line in out.splitlines():
            parts = line.split("\0")
            if len(parts) != 4:
                continue
            for name in (parts[0], parts[2]):
                if name and name not in ok_names:
                    findings.append(("block", "git-identity", "(history)", 0,
                                     "a commit carries an undeclared author/committer name"))
            for email in (parts[1], parts[3]):
                if email and email.lower() not in ok_emails and not NOREPLY.search(email):
                    findings.append(("block", "git-identity", "(history)", 0,
                                     "a commit carries an undeclared author/committer email"))
    # dedupe
    return sorted(set(findings))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    root = os.path.abspath(args.root)

    allowlist = load_allowlist(root)
    rules = build_rules(allowlist)
    run_canaries(rules)

    deny = load_denylist(root)
    deny_variants = {v: encodings(v) for v in deny}
    mode = "denylist + heuristics" if deny else "HEURISTICS ONLY"

    files = inventory(root)
    findings = []
    scanned = 0
    for rel in files:
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            continue
        if rel in SCAN_EXEMPT:
            print(f"  note: {rel} is exempt from content scan (holds the matchers)")
            continue
        if is_binary(path):
            findings.append(("review", "binary", rel, 0,
                             "binary file; verify by hand before shipping"))
            continue
        scanned += 1
        with open(path, encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                low = line.lower()
                for value, variants in deny_variants.items():
                    if any(v in low for v in variants):
                        findings.append(("block", "denylist", rel, lineno,
                                         f"denylisted value present (len {len(value)}, "
                                         f"starts {value[:2]!r})"))
                for r in rules:
                    for m in r.rx.finditer(line):
                        if not r.allow(m):
                            snippet = m.group(0)[:60]
                            findings.append(("block", r.name, rel, lineno, snippet))

    if scanned == 0:
        print("GATE BROKEN: zero files scanned. An empty tree passes every "
              "check, which is exactly what a mistyped --root looks like.")
        sys.exit(2)

    findings.extend(git_identity_findings(root, allowlist))

    print(f"mode: {mode}; files scanned: {scanned}")
    if not deny:
        print("  denylist: UNAVAILABLE (scripts/pii-denylist.txt missing or empty).")
        print("  Known-value matching did not run; heuristics cannot see a value")
        print("  hidden inside base64 or an unusual format. Weaker than a full run.")
    if findings:
        print(f"\nFAIL: {len(findings)} finding(s):")
        for sev, rule, rel, lineno, msg in findings:
            loc = f"{rel}:{lineno}" if lineno else rel
            print(f"  [{sev}] {rule:14s} {loc}  {msg}")
        sys.exit(1)
    print("OK: no findings." + ("" if deny else "  (heuristics-only run; see note above)"))
    sys.exit(0)


if __name__ == "__main__":
    main()
