"""The PNR step: IndiGo's own payment page + gateway yields the real record locator; if that fails the customer must
never lose the booking (they still get the order id + payment link via the API fallback)."""
import pytest

SEARCH = {"data": {"trips": [{"journeysAvailable": [{"journeyKey": "JK", "segKey": "DELBOM", "stops": 0, "isSold": False,
          "designator": {"departure": "2026-12-09T03:30:00"},
          "passengerFares": [{"fareAvailabilityKey": "FK", "totalFareAmount": 5132, "isActive": True, "productClass": "R"}]}]}]}}
SELL = {"data": {"passengers": {"PK1": {}}, "breakdown": {"totalAmount": 5132}}}
INIT = {"data": {"order_id": "JFTWEORDER", "orderAmount": 5132,
                 "payment_links": {"web": "https://payments.juspay.in/payment-page/order/ordeh_x"}}}
ITIN_OK = {"data": {"bookingDetails": {"recordLocator": "MYBBQP", "bookingStatus": "On-Hold", "holdDate": "2026-11-17T10:21:55Z"}}}
ITIN_EMPTY = {"data": {"bookingDetails": {"recordLocator": "", "bookingStatus": None}}}


@pytest.fixture(autouse=True)
def no_waiting_between_record_reads(monkeypatch):
    import indigo_client as ic
    monkeypatch.setattr(ic, "RECORD_GAP_S", 0.0)


class FakeHuman:
    async def wander(self, *_a): ...


def make_session(monkeypatch, itinerary, calls):
    import indigo_client as ic
    monkeypatch.setattr(ic.Pace, "min_gap", 0.0)
    monkeypatch.setattr(ic.Pace, "jitter", 0.0)

    class Tab:
        async def cookies(self, _urls): return []
        async def delete_cookie(self, *a, **k): ...

    sess = ic.IndigoSession(Tab(), FakeHuman(), [])
    responses = {ic.U_TOKEN: {"status": 200, "json": {}}, ic.U_SEARCH: {"status": 200, "json": SEARCH},
                 ic.U_SELL: {"status": 200, "json": SELL},
                 ic.U_ADDPAX: {"status": 200, "json": {"data": {"success": True}}},
                 ic.U_PAYINIT: {"status": 200, "json": INIT}, ic.U_ITIN: {"status": 200, "json": itinerary}}

    async def call(name, url, method, key, body):
        calls.append(name)
        return responses[url]
    monkeypatch.setattr(sess, "call", call)

    async def pause(*_a): ...
    monkeypatch.setattr(sess, "human_pause", pause)
    return ic, sess


def req(ic):
    return ic.BookingRequest("DEL", "BOM", "09-12-2026", [ic.Passenger("A", "B", "M", "01-01-1950")], "offer1", "a@b.co", "6000000000")


async def test_gateway_success_returns_real_pnr_without_calling_initiate(monkeypatch):
    calls = []
    ic, sess = make_session(monkeypatch, ITIN_OK, calls)
    ran = {"psp": 0}

    async def psp():
        ran["psp"] += 1
    monkeypatch.setattr(ic.random, "uniform", lambda a, b: 0)
    res = await ic.run_booking(sess, req(ic), "6ETST001", "payment", psp=psp)
    assert res.success and res.pnr == "MYBBQP" and res.kind == "record_locator"
    assert res.hold_until == "2026-11-17T10:21:55Z" and ran["psp"] == 1
    assert "pay-init" not in calls          # IndiGo's own payment page creates the order; we must not


async def test_gateway_failure_falls_back_to_order_and_link(monkeypatch):
    calls = []
    ic, sess = make_session(monkeypatch, ITIN_OK, calls)

    async def psp():
        raise RuntimeError("Net banking not shown")
    res = await ic.run_booking(sess, req(ic), "6ETST001", "payment", psp=psp)
    assert res.success and res.pnr == "JFTWEORDER" and res.kind == "order_id" and res.pay_url
    assert calls[-1] == "pay-init"


async def test_a_gateway_hiccup_is_tried_once_more_before_falling_back(monkeypatch):
    """The live failure 'the bank popup window never opened' was intermittent: a calm second try got the PNR."""
    calls = []
    ic, sess = make_session(monkeypatch, ITIN_OK, calls)
    tries = {"n": 0}

    async def psp():
        tries["n"] += 1
        if tries["n"] == 1:
            raise RuntimeError("the bank popup window never opened")
    res = await ic.run_booking(sess, req(ic), "6ETST001", "payment", psp=psp)
    assert tries["n"] == 2 and res.kind == "record_locator" and res.pnr == "MYBBQP" and not res.psp_failed
    assert "pay-init" not in calls


async def test_two_gateway_failures_fall_back_and_are_flagged(monkeypatch):
    calls = []
    ic, sess = make_session(monkeypatch, ITIN_OK, calls)

    async def psp():
        raise RuntimeError("the bank popup window never opened")
    res = await ic.run_booking(sess, req(ic), "6ETST001", "payment", psp=psp)
    assert res.kind == "order_id" and res.psp_failed is True and calls[-1] == "pay-init"


async def test_no_record_locator_yet_falls_back_to_order(monkeypatch):
    calls = []
    ic, sess = make_session(monkeypatch, ITIN_EMPTY, calls)

    async def psp(): ...
    res = await ic.run_booking(sess, req(ic), "PROMO2", "payment", psp=psp)
    assert res.success and res.kind == "order_id" and res.pnr == "JFTWEORDER"


async def test_without_psp_the_flow_ends_with_order_and_link(monkeypatch):
    calls = []
    ic, sess = make_session(monkeypatch, ITIN_OK, calls)
    res = await ic.run_booking(sess, req(ic), "PROMO2", "payment", psp=None)
    assert res.kind == "order_id" and res.pay_url


async def test_edge_block_during_gateway_is_not_swallowed(monkeypatch):
    """A 429/deny must reach the worker (retry later), not be hidden by the soft fallback."""
    calls = []
    ic, sess = make_session(monkeypatch, ITIN_OK, calls)

    async def psp():
        raise ic.IndigoBlocked("itinerary: HTTP 429")
    with pytest.raises(ic.IndigoBlocked):
        await ic.run_booking(sess, req(ic), "PROMO2", "payment", psp=psp)


def test_message_labels_pnr_vs_order_id():
    import config, importlib
    importlib.reload(config)
    import worker
    from indigo_client import BookingResult
    b = {"origin": "DEL", "destination": "BOM", "travel_date": "09-12-2026", "offer": "offer1", "pax_count": 1}
    pnr = worker._fmt_pnr_message(b, BookingResult(True, pnr="MYBBQP", kind="record_locator", fare_amount=5132,
                                                   hold_until="2026-11-17T10:21:55Z"))
    assert "MYBBQP" in pnr and "PNR" in pnr
    assert "16:21 BDT / 15:51 IST" in pnr and "Pay Now" in pnr      # hold deadline in both local times + how to pay
    order = worker._fmt_pnr_message(b, BookingResult(True, pnr="JFTWEORDER", kind="order_id", pay_url="https://x/y"))
    assert "order id" in order and "JFTWEORDER" in order and "https://x/y" in order


def test_message_tells_the_truth_about_the_coupon():
    import config, importlib
    importlib.reload(config)
    import worker
    from indigo_client import BookingResult
    b = {"origin": "DEL", "destination": "BOM", "travel_date": "09-12-2026", "offer": "offer2", "pax_count": 1}
    ok = worker._fmt_pnr_message(b, BookingResult(True, pnr="X1", kind="record_locator", coupon_applied=True, discount=815))
    assert "Offer 2 applied" in ok and "₹815" in ok and "PROMO2" not in ok
    no = worker._fmt_pnr_message(b, BookingResult(True, pnr="X1", kind="record_locator", coupon_applied=False))
    assert "could not be applied" in no and "regular fare" in no        # never a silent offer label on a full fare
