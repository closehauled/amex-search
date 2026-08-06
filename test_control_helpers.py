"""Plain-python tests for the control-mode pure/file helpers.
Run: python test_control_helpers.py   (exits non-zero on failure)
The project has no pytest infra; browser paths are covered by the
headless integration test in the plan."""
import json
import os
import tempfile

import amex_scanner as S


def check(name, cond):
    if not cond:
        raise AssertionError(name)
    print("ok", name)


def test_extract_message_id():
    u = ("https://www.americanexpress.com/en-us/credit-cards/apply/business/"
         "business-platinum-charge-card/68443-9-0?messageId=aaaabbbbccccddddee"
         "eeffff00001111&intlink=US-Acq")
    check("mid_present",
          S.extract_message_id(u) == "aaaabbbbccccddddeeeeffff00001111")
    check("mid_none_decision",
          S.extract_message_id(".../68443-9-0/decision") is None)
    check("mid_none_empty", S.extract_message_id(None) is None)


def test_extract_offer_code():
    u = ("https://www.americanexpress.com/en-us/credit-cards/apply/business/"
         "business-platinum-charge-card/68443-9-0?messageId=abc")
    check("code", S.extract_offer_code(u) == "68443")
    check("code_gold",
          S.extract_offer_code(".../businessgold-card/64606-9-0?x=1") == "64606")
    check("code_none", S.extract_offer_code(".../no-code-here") is None)
    check("code_empty", S.extract_offer_code(None) is None)
    # built-in table maps codes to the right offer
    t = S.load_offer_codes()
    check("table_plat_300", t["business_platinum"]["68443"] == 300000)
    check("table_gold_200", t["business_gold"]["64606"] == 200000)


def test_normalize_card():
    check("plat", S.normalize_card("platinum") == "business_platinum")
    check("plat2", S.normalize_card("PLAT") == "business_platinum")
    check("plat_full",
          S.normalize_card("business_platinum") == "business_platinum")
    check("gold", S.normalize_card("gold") == "business_gold")
    check("bad", S.normalize_card("amex blue") is None)
    check("none", S.normalize_card(None) is None)


class FakeSession:
    def __init__(self, idx, pts):
        self.idx = idx
        self._pts = pts

    def public(self):
        return {"idx": self.idx, "points": self._pts}


def test_read_new_commands():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "cmd")
    with open(p, "w") as f:
        f.write(json.dumps({"id": "a", "cmd": "spawn"}) + "\n")
        f.write("\n")                      # blank -> skipped
        f.write("{not json}\n")            # malformed -> skipped
        f.write(json.dumps({"id": "b", "cmd": "status"}) + "\n")
    seen = set()
    first = S.read_new_commands(p, seen)
    check("first_two", [c["id"] for c in first] == ["a", "b"])
    with open(p, "a") as f:
        f.write(json.dumps({"id": "c", "cmd": "release"}) + "\n")
    second = S.read_new_commands(p, seen)
    check("only_new", [c["id"] for c in second] == ["c"])
    check("missing_file",
          S.read_new_commands(os.path.join(d, "nope"), seen) == [])


def test_write_state_roundtrip():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "state.json")
    S.write_state(p, [FakeSession(0, 300000), FakeSession(1, None)], 4242)
    st = json.loads(open(p).read())
    check("pid", st["holder_pid"] == 4242)
    check("count", len(st["sessions"]) == 2)
    check("pts", st["sessions"][0]["points"] == 300000)
    check("updated", isinstance(st["updated"], str) and "T" in st["updated"])


if __name__ == "__main__":
    test_extract_message_id()
    test_extract_offer_code()
    test_normalize_card()
    test_read_new_commands()
    test_write_state_roundtrip()
    print("CONTROL HELPERS PASS")
