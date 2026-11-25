"""Proof that the bot applies coupons EXACTLY like the coupon extension: the extension's real core.js is run through
Node on the same bodies and codes, and the Python port (coupons.py) must give identical bodies and decisions.
Skipped (not failed) only when Node or the extension folder is not present on the machine."""
import copy
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

CORE = Path(os.getenv("INDIGO_CORE", r"F:\card\indigo\core.js"))
FIX = CORE.parent / "tests" / "fixtures"
RUNNER = Path(__file__).with_name("core_runner.js")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(not (NODE and CORE.exists()), reason="node or the extension's core.js is not available")


def run_core(cases):
    p = subprocess.run([NODE, str(RUNNER)], input=json.dumps(cases), capture_output=True, text=True, timeout=60,
                       env={**os.environ, "INDIGO_CORE": str(CORE)})
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def fixture(name):
    d = json.loads((FIX / name).read_text(encoding="utf-8"))
    return d["body"] if "body" in d else {k: v for k, v in d.items() if not k.startswith("_")}


SEARCH = lambda: fixture("flight_search_request.json")          # noqa: E731 - the real captured search body
SELL = lambda: fixture("flight_sell_request.json")              # noqa: E731 - the real captured sell body

CASES = []
for code in ("6ETST001", "PROMO2", "", "STUDENT10"):
    CASES.append(("flight-search", SEARCH(), code))
    CASES.append(("flight-sell", SELL(), code))
# edge cases the extension's own tests cover
CASES += [
    ("flight-search", {"codes": {"currency": "INR", "promotionCode": "OLD"}, "x": 1}, "PROMO2"),        # site code replaced on search
    ("flight-search", {"x": 1}, "PROMO2"),                                                               # missing codes -> created
    ("flight-search", {"codes": "weird", "x": 1}, "PROMO2"),                                            # wrong-typed codes
    ("flight-search", {"codes": {"promotionCode": "PROMO2"}}, "PROMO2"),                                  # already set
    ("flight-sell", {"promotionCode": "SITECODE", "a": 1}, "PROMO2"),                                  # sell: site's own code is kept
    ("flight-sell", {"promotionCode": None, "a": 1}, "PROMO2"),                                        # sell: null counts as empty
    ("flight-sell", {"promotionCode": "   ", "a": 1}, "PROMO2"),                                       # sell: blank counts as empty
    ("flight-sell", {"a": 1}, "PROMO2"),                                                                # sell: never invents a field
    ("flight-sell", {"promotionCode": "PROMO2"}, "PROMO2"),                                              # sell: already set
    ("flight-search", {"bookingCodes": {"promoCode": ""}}, "PROMO2"),                                   # similarly named field is found
]


@pytest.mark.parametrize("idx", range(len(CASES)))
def test_python_port_matches_the_real_extension(idx):
    import coupons
    rule, body, code = CASES[idx]
    js = run_core([{"rule": rule, "body": body, "code": code}])[0]
    py_body, info = coupons.rewrite_body(copy.deepcopy(body), rule, code)
    assert py_body == js["body"], f"body differs for {rule}/{code!r}"
    assert info["changed"] == js["changed"]
    assert info.get("skipped") == js["skipped"]
    assert info.get("field") == js["field"]


def test_offer_codes_are_the_extension_defaults():
    """The two coupons of tasks.md are the codes the extension's popup offers."""
    import config
    popup = (CORE.parent / "popup.js").read_text(encoding="utf-8") + (CORE.parent / "settings.js").read_text(encoding="utf-8")
    for code in config.COUPONS.values():
        assert code in popup, f"{code} is not one of the extension's built-in codes"


def test_search_sets_codes_promotion_code_and_nothing_else():
    import coupons
    body = SEARCH()
    out = coupons.apply_search(copy.deepcopy(body), "PROMO2")
    assert out["codes"]["promotionCode"] == "PROMO2"
    out["codes"]["promotionCode"] = body["codes"]["promotionCode"]
    assert out == body                                   # every other byte is untouched (incl. passenger type ADT)


def test_sell_fills_only_the_empty_promotion_code():
    import coupons
    body = SELL()
    out = coupons.apply_sell(copy.deepcopy(body), "6ETST001")
    assert out["promotionCode"] == "6ETST001"
    out["promotionCode"] = ""
    assert out == body
    kept = coupons.apply_sell({**body, "promotionCode": "SITE"}, "6ETST001")
    assert kept["promotionCode"] == "SITE"               # a code the site itself put there is never overridden


def test_summarize_sell_matches_the_real_extension():
    import coupons
    answer = json.loads((FIX / "flight_sell_answer.json").read_text(encoding="utf-8"))
    ours = coupons.summarize_sell(answer)
    js = subprocess.run([NODE, "-e",
                         "const c=require(process.env.INDIGO_CORE);const fs=require('fs');"
                         "process.stdout.write(JSON.stringify(c.summarizeSell(fs.readFileSync(process.argv[1],'utf8'))))",
                         str(FIX / "flight_sell_answer.json")],
                        capture_output=True, text=True, env={**os.environ, "INDIGO_CORE": str(CORE)}, timeout=60)
    ref = json.loads(js.stdout)
    assert ours["applied"] == ref["applied"] and ours["total"] == ref["total"] and ours["fare"] == ref["fare"]
    assert ours["discount"] == ref["discount"]
