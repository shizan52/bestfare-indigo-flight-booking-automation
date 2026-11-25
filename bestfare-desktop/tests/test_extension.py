"""The coupon extension built into the app: its own core.js decides how the offer code goes into IndiGo's requests."""
import hashlib
from pathlib import Path

import pytest

import coupons
import extension
from nodepage import BrokenPage, NodePage, have_node

pytestmark = pytest.mark.skipif(not have_node(), reason="Node.js is needed to run the extension's JavaScript")

ORIGINALS = [Path(r"F:\card\indigo\core.js"), Path(r"F:\card\indigo-dist\Indigo-1.5.0\core.js")]

SEARCHES = [
    {"codes": {"currency": "INR", "promotionCode": ""}, "criteria": [{"dates": {"beginDate": "2026-12-09"}}], "tripCriteria": "oneWay"},
    {"codes": {"currency": "INR"}, "criteria": []},                                        # no field yet: the extension creates it
    {"codes": {"currency": "INR", "promotionCode": "SITECODE"}},                            # a search keeps being rewritten
    {"criteria": [], "promoCode": ""},                                                      # another spelling of the field
]
SELLS = [
    {"promotionCode": "", "keys": [{"journeyKey": "JK"}]},                                  # IndiGo's empty field is filled
    {"promotionCode": "SITECODE", "keys": []},                                              # a code the site put there is kept
    {"keys": []},                                                                           # no field: never added
    {"promotionCode": "PROMO2"},                                                              # already set
]


def make(page):
    return extension.ExtensionSession(page, None, [])


def test_the_bundled_core_is_the_extensions_own_file():
    mine = __import__("resources").read_bytes(extension.CORE_FILE)
    assert extension.VERSION == "1.5.0" and len(mine) > 10_000
    originals = [p for p in ORIGINALS if p.exists()]
    if not originals:
        pytest.skip("the original extension folders are not on this computer")
    for p in originals:
        assert hashlib.sha1(p.read_bytes()).hexdigest() == hashlib.sha1(mine).hexdigest(), f"ext/core.js differs from {p}"


@pytest.mark.parametrize("promo", ["6ETST001", "PROMO2"])
async def test_search_and_sell_are_rewritten_by_the_extension_itself_and_agree_with_the_python_rules(promo):
    page = NodePage()
    s = make(page)
    for body in SEARCHES:
        assert await s.rewrite_search(body, promo) == coupons.apply_search(body, promo)
    for body in SELLS:
        assert await s.rewrite_sell(body, promo) == coupons.apply_sell(body, promo)
    assert page.calls > 0 and page.core is not None and "__IndigoCore" in page.core          # it really ran the extension's code


async def test_the_offer_code_lands_where_the_extension_puts_it():
    s = make(NodePage())
    assert (await s.rewrite_search(SEARCHES[0], "PROMO2"))["codes"]["promotionCode"] == "PROMO2"
    assert (await s.rewrite_search(SEARCHES[1], "6ETST001"))["codes"]["promotionCode"] == "6ETST001"      # created
    assert (await s.rewrite_sell(SELLS[0], "PROMO2"))["promotionCode"] == "PROMO2"
    assert (await s.rewrite_sell(SELLS[1], "PROMO2"))["promotionCode"] == "SITECODE"                         # kept
    assert "promotionCode" not in await s.rewrite_sell(SELLS[2], "PROMO2")                                    # never added


async def test_reading_the_answer_matches_the_python_rules():
    s = make(NodePage())
    answers = [{"data": {"typeOfSale": {"promotionCode": "PROMO2"}, "breakdown": {"totalAmount": 5000.0, "journeyTotals": {"totalDiscount": 800.0, "totalAmount": 5800.0}}}},
               {"data": {"typeOfSale": {"promotionCode": ""}, "breakdown": {"totalAmount": 5000.0, "journeyTotals": {"totalDiscount": 0.0}}}},
               {"data": {"typeOfSale": {"promotionCode": ""}, "breakdown": {"totalAmount": 5000.0, "journeyTotals": {"totalDiscount": 300.0}}}}]
    for a in answers:
        applied, summ = await s.read_sell(a, "PROMO2")
        assert applied == coupons.coupon_applied(a, "PROMO2")
        assert summ["discount"] == coupons.summarize_sell(a)["discount"]


async def test_a_page_that_cannot_run_the_extension_falls_back_to_the_tested_port():
    s = make(BrokenPage())
    assert (await s.rewrite_search(SEARCHES[0], "PROMO2"))["codes"]["promotionCode"] == "PROMO2"
    assert (await s.rewrite_sell(SELLS[0], "PROMO2"))["promotionCode"] == "PROMO2"
    applied, summ = await s.read_sell(SELLS and {"data": {"typeOfSale": {"promotionCode": "PROMO2"}, "breakdown": {"journeyTotals": {"totalDiscount": 1.0}}}}, "PROMO2")
    assert applied is True


async def test_a_whole_booking_with_the_extension_puts_the_code_into_both_requests(monkeypatch):
    import indigo_client as ic
    monkeypatch.setattr(ic.Pace, "min_gap", 0.0)
    monkeypatch.setattr(ic.Pace, "jitter", 0.0)
    monkeypatch.setattr(ic.random, "uniform", lambda a, b: 0)

    class Human:
        async def wander(self, *_a): ...

    page = NodePage()

    class Tab:
        evaluate = page.evaluate
        evaluate_json = page.evaluate_json
        async def cookies(self, _u): return []
        async def delete_cookie(self, *a, **k): ...

    sess = extension.ExtensionSession(Tab(), Human(), [])
    search = {"data": {"trips": [{"journeysAvailable": [{"journeyKey": "JK", "segKey": "DEL353BOM", "stops": 0, "isSold": False,
              "designator": {"origin": "DEL", "destination": "BOM", "departure": "2026-12-09T06:00:00"},
              "passengerFares": [{"fareAvailabilityKey": "FK", "totalFareAmount": 6558, "isActive": True, "productClass": "R"}]}]}]}}
    sell = {"data": {"passengers": {"PK1": {}}, "typeOfSale": {"promotionCode": "PROMO2"},
                     "breakdown": {"totalAmount": 6000.0, "journeyTotals": {"totalDiscount": 558.0, "totalAmount": 6558.0}}}}
    answers = {ic.U_TOKEN: {"status": 200, "json": {}}, ic.U_SEARCH: {"status": 200, "json": search}, ic.U_SELL: {"status": 200, "json": sell},
               ic.U_ADDPAX: {"status": 200, "json": {"data": {"success": True}}},
               ic.U_ITIN: {"status": 200, "json": {"data": {"bookingDetails": {"recordLocator": "ABC123", "holdDate": "x"}}}}}
    sent = {}

    async def call(name, url, method, key, body):
        sent[name] = body
        return answers[url]

    async def pause(*_a): ...
    monkeypatch.setattr(sess, "call", call)
    monkeypatch.setattr(sess, "human_pause", pause)

    async def psp(): ...
    req = ic.BookingRequest("DEL", "BOM", "09-12-2026", [ic.Passenger("A", "B", "M")], "offer2", "a@b.co", "9000000001")
    res = await ic.run_booking(sess, req, "PROMO2", "payment", psp=psp)
    assert sent["search"]["codes"]["promotionCode"] == "PROMO2" and sent["sell"]["promotionCode"] == "PROMO2"
    assert res.success and res.coupon_applied is True and res.discount == 558.0
