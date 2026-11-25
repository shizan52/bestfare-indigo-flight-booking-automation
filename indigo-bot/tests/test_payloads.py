"""What really goes to IndiGo: the date, the route, every passenger's details, the contact and the offer code - checked on the
exact request bodies the booking engine builds (search, sell, addpassenger), with IndiGo's answers faked."""
import pytest

PAX_KEYS = {"PK1": {}, "PK2": {}, "PK3": {}}


def search_answer():
    """Modelled on the real answer: the cheapest journeys belong to NEARBY airports - they must never be chosen."""
    def jr(seg, o, d, fare, stops=0):
        return {"segKey": seg, "journeyKey": f"JK-{seg}", "stops": stops, "isSold": False,
                "designator": {"origin": o, "destination": d, "departure": "2026-12-09T06:00:00"},
                "passengerFares": [{"fareAvailabilityKey": f"FK-{seg}", "totalFareAmount": fare, "isActive": True, "productClass": "R"}]}
    return {"data": {"trips": [{"journeysAvailable": [jr("DELAMDNMI", "DEL", "NMI", 5000, 1), jr("HDO5175BOM", "HDO", "BOM", 5100),
                                                     jr("DEL353BOM", "DEL", "BOM", 6558), jr("DEL449BOM", "DEL", "BOM", 6700)]}]}}


def sell_answer(n):
    return {"data": {"passengers": {f"PK{i + 1}": {} for i in range(n)}, "typeOfSale": {"promotionCode": ""},
                     "breakdown": {"totalAmount": 6558.0, "journeyTotals": {"totalDiscount": 0.0, "totalAmount": 6558.0}}}}


ITIN = {"data": {"bookingDetails": {"recordLocator": "ABC123", "bookingStatus": "On-Hold", "holdDate": "2026-12-01T10:00:00Z"}}}


class FakeHuman:
    async def wander(self, *_a): ...


def session(monkeypatch, n=1):
    import indigo_client as ic
    monkeypatch.setattr(ic.Pace, "min_gap", 0.0)
    monkeypatch.setattr(ic.Pace, "jitter", 0.0)
    monkeypatch.setattr(ic.random, "uniform", lambda a, b: 0)

    class Tab:
        async def cookies(self, _u): return []
        async def delete_cookie(self, *a, **k): ...

    sess = ic.IndigoSession(Tab(), FakeHuman(), [])
    answers = {ic.U_TOKEN: {"status": 200, "json": {}}, ic.U_SEARCH: {"status": 200, "json": search_answer()},
               ic.U_SELL: {"status": 200, "json": sell_answer(n)}, ic.U_ADDPAX: {"status": 200, "json": {"data": {"success": True}}},
               ic.U_ITIN: {"status": 200, "json": ITIN}}
    sent = {}

    async def call(name, url, method, key, body):
        sent[name] = {"url": url, "method": method, "body": body}
        return answers[url]

    async def pause(*_a): ...
    monkeypatch.setattr(sess, "call", call)
    monkeypatch.setattr(sess, "human_pause", pause)
    return ic, sess, sent


async def book(monkeypatch, promo, passengers, date="09-12-2026", origin="DEL", dest="BOM", phone="+91 98765-43210", email="a.b@example.com"):
    ic, sess, sent = session(monkeypatch, len(passengers))
    req = ic.BookingRequest(origin, dest, date, [ic.Passenger(*p) for p in passengers], "offer1", email, phone)

    async def psp():
        return None
    res = await ic.run_booking(sess, req, promo, "payment", psp=psp)
    return res, sent


@pytest.mark.parametrize("promo", ["6ETST001", "PROMO2"])
async def test_search_carries_date_route_passengers_and_the_offer_code(monkeypatch, promo):
    res, sent = await book(monkeypatch, promo, [("RAHUL", "SHARMA", "M"), ("PRIYA", "SHARMA", "F")])
    b = sent["search"]["body"]
    assert sent["search"]["url"].endswith("/v2/flight/search") and sent["search"]["method"] == "POST"
    c = b["criteria"][0]
    assert c["dates"]["beginDate"] == "2026-12-09"                                    # DD-MM-YYYY -> IndiGo's ISO date
    assert c["stations"] == {"originStationCodes": ["DEL"], "destinationStationCodes": ["BOM"]}
    assert b["passengers"]["types"] == [{"count": 2, "discountCode": "", "type": "ADT"}]       # both offers book as ADT
    assert b["codes"]["promotionCode"] == promo and b["tripCriteria"] == "oneWay" and b["codes"]["currency"] == "INR"
    assert res.success and res.pnr == "ABC123"


@pytest.mark.parametrize("promo", ["6ETST001", "PROMO2"])
async def test_sell_holds_the_chosen_fare_for_everyone_with_the_offer_code(monkeypatch, promo):
    _res, sent = await book(monkeypatch, promo, [("A", "B", "M"), ("C", "D", "F"), ("E", "F", "M")])
    b = sent["sell"]["body"]
    key = b["keys"][0]
    assert (key["journeyKey"], key["fareAvailabilityKey"], key["inventoryControl"]) == ("JK-DEL353BOM", "FK-DEL353BOM", "HoldSpace")
    assert b["passengers"]["types"] == [{"count": 3, "discountCode": "", "type": "ADT"}]
    assert b["promotionCode"] == promo and b["currencyCode"] == "INR"


async def test_nearby_airports_are_never_the_flight_that_gets_booked(monkeypatch):
    res, sent = await book(monkeypatch, "PROMO2", [("A", "B", "M")])
    assert sent["sell"]["body"]["keys"][0]["journeyKey"] == "JK-DEL353BOM"         # not DEL->AMD->NMI (cheaper), not HDO->BOM
    assert res.fare_amount == 6558.0


async def test_every_passengers_details_reach_indigo_exactly(monkeypatch):
    people = [("RAHUL KUMAR", "SHARMA", "M"), ("PRIYA", "D'SOUZA", "F"), ("ANN-MARIE", "O'NEIL", "F")]
    _res, sent = await book(monkeypatch, "6ETST001", people, phone="+91 98765-43210", email="rahul.sharma@gmail.com")
    d = sent["addpax"]["body"]["data"]
    paxes = d["passengers"]
    assert [p["passengerKey"] for p in paxes] == ["PK1", "PK2", "PK3"]               # one per held seat, in the order entered
    got = [(p["name"]["first"], p["name"]["last"], p["name"]["title"], p["info"]["gender"]) for p in paxes]
    assert got == [("Rahul Kumar", "Sharma", "MR", 1), ("Priya", "D'Souza", "MS", 2), ("Ann-Marie", "O'Neil", "MS", 2)]
    assert all(p["passengerTypeCode"] == "ADT" and p["info"]["nationality"] == "IN" and p["info"]["dateOfBirth"] is None for p in paxes)
    contact = d["bookingcontacts"][0]
    assert contact["MobilePhone"] == "9876543210" and contact["MobilePhoneCountryCode"] == "91"     # +91 and punctuation removed
    assert contact["emailAddress"] == "rahul.sharma@gmail.com"
    assert (contact["name"]["first"], contact["name"]["last"], contact["name"]["title"]) == ("Rahul Kumar", "Sharma", "MR")


async def test_the_date_and_route_are_whatever_the_customer_chose(monkeypatch):
    for date, iso in (("01-01-2027", "2027-01-01"), ("28-02-2027", "2027-02-28"), ("31-12-2026", "2026-12-31")):
        _res, sent = await book(monkeypatch, "PROMO2", [("A", "B", "M")], date=date, origin="BLR", dest="DEL")
        c = sent["search"]["body"]["criteria"][0]
        assert c["dates"]["beginDate"] == iso and c["stations"]["originStationCodes"] == ["BLR"] and c["stations"]["destinationStationCodes"] == ["DEL"]


async def test_the_bookings_own_record_comes_back_for_checking(monkeypatch):
    res, _sent = await book(monkeypatch, "PROMO2", [("A", "B", "M")])
    assert res.itinerary == ITIN and res.kind == "record_locator" and res.hold_until == "2026-12-01T10:00:00Z"


async def test_the_session_hooks_default_to_the_python_port(monkeypatch):
    import coupons
    ic, sess, _sent = session(monkeypatch)
    body = {"codes": {"promotionCode": ""}, "x": 1}
    assert await sess.rewrite_search(body, "PROMO2") == coupons.apply_search(body, "PROMO2") == {"codes": {"promotionCode": "PROMO2"}, "x": 1}
    sell = {"promotionCode": "", "keys": []}
    assert await sess.rewrite_sell(sell, "PROMO2") == coupons.apply_sell(sell, "PROMO2")
    ans = {"data": {"typeOfSale": {"promotionCode": "PROMO2"}, "breakdown": {"totalAmount": 5.0, "journeyTotals": {"totalDiscount": 2.0}}}}
    applied, summ = await sess.read_sell(ans, "PROMO2")
    assert applied is True and summ["discount"] == 2.0 and summ["applied"] == "PROMO2"
