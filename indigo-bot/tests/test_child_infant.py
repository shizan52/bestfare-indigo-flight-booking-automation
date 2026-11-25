"""Children and infants: what goes to IndiGo when the booking has more than adults - the passenger kinds in the search and the fare hold,
the child's date of birth, the infant's name and date of birth, who rides on whose lap. IndiGo's answers are faked."""
import pytest

from test_payloads import FakeHuman, ITIN, search_answer, sell_answer  # noqa: F401  (the same stand-ins as the payload tests)

ADULT, CHILD, INFANT = "adult", "child", "infant"


def slots(*codes):
    """The fare hold's answer: one slot per seat, each saying what kind of passenger it is for (like the real one)."""
    return {f"PK{i + 1}": ({"passengerTypeCode": c} if c else {}) for i, c in enumerate(codes)}


def session(monkeypatch, codes):
    import indigo_client as ic
    monkeypatch.setattr(ic.Pace, "min_gap", 0.0)
    monkeypatch.setattr(ic.Pace, "jitter", 0.0)
    monkeypatch.setattr(ic.random, "uniform", lambda a, b: 0)

    class Tab:
        async def cookies(self, _u): return []
        async def delete_cookie(self, *a, **k): ...

    sess = ic.IndigoSession(Tab(), FakeHuman(), [])
    sell = sell_answer(1)
    sell["data"]["passengers"] = slots(*codes)
    answers = {ic.U_TOKEN: {"status": 200, "json": {}}, ic.U_SEARCH: {"status": 200, "json": search_answer()}, ic.U_SELL: {"status": 200, "json": sell},
               ic.U_ADDPAX: {"status": 200, "json": {"data": {"success": True}}}, ic.U_ITIN: {"status": 200, "json": ITIN}}
    sent = {}

    async def call(name, url, method, key, body):
        sent[name] = {"url": url, "method": method, "body": body}
        return answers[url]

    async def pause(*_a): ...
    monkeypatch.setattr(sess, "call", call)
    monkeypatch.setattr(sess, "human_pause", pause)
    return ic, sess, sent


async def book(monkeypatch, people, codes, until="payment"):
    ic, sess, sent = session(monkeypatch, codes)
    req = ic.BookingRequest("DEL", "BOM", "09-12-2026", [ic.Passenger(*p) for p in people], "offer1", "a.b@example.com", "9876543210")

    async def psp():
        return None
    res = await ic.run_booking(sess, req, "6ETST001", until, psp=psp)
    return res, sent


FAMILY = [("RAHUL", "SHARMA", "M", "", ADULT), ("PRIYA", "SHARMA", "F", "", ADULT),
          ("ARYA", "SHARMA", "F", "25-03-2019", CHILD), ("VIHAAN", "SHARMA", "M", "10-06-2026", INFANT)]


async def test_the_search_and_the_hold_name_every_kind_of_passenger(monkeypatch):
    res, sent = await book(monkeypatch, FAMILY, ["ADT", "ADT", "CHD"])
    assert sent["search"]["body"]["passengers"]["types"] == [{"count": 2, "discountCode": "", "type": "ADT"}, {"count": 1, "discountCode": "", "type": "CHD"},
                                                             {"count": 1, "discountCode": "", "type": "INFT"}]
    sell = sent["sell"]["body"]
    assert sell["passengers"]["types"] == [{"count": 2, "discountCode": "", "type": "ADT"}, {"count": 1, "discountCode": "", "type": "CHD"}]
    assert sell["infantCount"] == 1                                                    # the infant is announced to the hold, not given a seat
    assert res.success


async def test_only_adults_is_exactly_what_it_always_was(monkeypatch):
    _res, sent = await book(monkeypatch, [("A", "B", "M"), ("C", "D", "F")], [None, None])
    assert sent["search"]["body"]["passengers"]["types"] == [{"count": 2, "discountCode": "", "type": "ADT"}]
    assert sent["sell"]["body"]["passengers"]["types"] == [{"count": 2, "discountCode": "", "type": "ADT"}] and sent["sell"]["body"]["infantCount"] == 0


async def test_the_child_goes_in_a_child_slot_with_the_date_of_birth(monkeypatch):
    _res, sent = await book(monkeypatch, FAMILY, ["ADT", "ADT", "CHD"])
    paxes = sent["addpax"]["body"]["data"]["passengers"]
    kid = [p for p in paxes if p["passengerTypeCode"] == "CHD"]
    assert len(kid) == 1 and kid[0]["passengerKey"] == "PK3"
    assert kid[0]["name"]["first"] == "Arya" and kid[0]["name"]["last"] == "Sharma" and kid[0]["name"]["title"] == "MISS"
    assert kid[0]["info"] == {"nationality": "IN", "gender": 2, "dateOfBirth": "2019-03-25"}
    adults = [p for p in paxes if p["passengerTypeCode"] == "ADT"]
    assert [p["passengerKey"] for p in adults] == ["PK1", "PK2"] and all(p["info"]["dateOfBirth"] is None for p in adults)      # an adult's is not sent


async def test_the_infant_rides_on_the_first_adult_with_a_name_and_a_date_of_birth(monkeypatch):
    _res, sent = await book(monkeypatch, FAMILY, ["ADT", "ADT", "CHD"])
    paxes = sent["addpax"]["body"]["data"]["passengers"]
    carrier = next(p for p in paxes if p["passengerKey"] == "PK1")
    assert carrier["infant"]["name"]["first"] == "Vihaan" and carrier["infant"]["name"]["last"] == "Sharma"
    assert carrier["infant"]["dateOfBirth"] == "2026-06-10" and carrier["infant"]["gender"] == "Male" and carrier["infant"]["nationality"] == "IN"
    assert "infant" not in next(p for p in paxes if p["passengerKey"] == "PK2")
    assert len(paxes) == 3                                                              # (three seats: the baby has none)


async def test_when_indigo_gives_the_infant_its_own_slot_it_is_put_there(monkeypatch):
    _res, sent = await book(monkeypatch, FAMILY, ["ADT", "ADT", "CHD", "INFT"])
    paxes = sent["addpax"]["body"]["data"]["passengers"]
    baby = [p for p in paxes if p["passengerTypeCode"] == "INFT"]
    assert len(baby) == 1 and baby[0]["passengerKey"] == "PK4" and baby[0]["name"]["first"] == "Vihaan" and baby[0]["info"]["dateOfBirth"] == "2026-06-10"
    assert not any("infant" in p for p in paxes)


async def test_a_child_listed_first_does_not_become_the_contact_or_take_an_adults_slot(monkeypatch):
    people = [("ARYA", "SHARMA", "F", "25-03-2019", CHILD), ("RAHUL", "SHARMA", "M", "", ADULT)]
    _res, sent = await book(monkeypatch, people, ["ADT", "CHD"])
    d = sent["addpax"]["body"]["data"]
    by_key = {p["passengerKey"]: p for p in d["passengers"]}
    assert by_key["PK1"]["name"]["first"] == "Rahul" and by_key["PK2"]["name"]["first"] == "Arya"
    assert d["bookingcontacts"][0]["name"]["first"] == "Rahul"                          # the grown-up is the contact


async def test_children_alone_or_more_infants_than_adults_are_refused_before_anything_is_sent(monkeypatch):
    res, sent = await book(monkeypatch, [("ARYA", "S", "F", "25-03-2019", CHILD)], ["CHD"])
    assert not res.success and "adult" in res.error and not sent
    res, sent = await book(monkeypatch, [("RAHUL", "S", "M", "", ADULT), ("B1", "S", "F", "10-06-2026", INFANT), ("B2", "S", "M", "11-06-2026", INFANT)], ["ADT"])
    assert not res.success and "infant" in res.error.lower() and not sent


async def test_a_hold_that_has_no_place_for_the_child_stops_the_booking_cleanly(monkeypatch):
    res, sent = await book(monkeypatch, FAMILY, [None, None, None])                             # 3 untyped slots, 3 seats: fits (adults first, then the child)
    assert res.success
    res, sent = await book(monkeypatch, FAMILY, ["ADT", "ADT", "ADT"])                          # a typed answer with no child slot
    assert not res.success and "place for every traveller" in res.error and "addpax" not in sent


@pytest.mark.parametrize("text,iso", [("25-03-2019", "2019-03-25"), ("2019-03-25", "2019-03-25"), ("25/03/2019", "2019-03-25"), ("", None), ("garbage", None)])
def test_the_date_of_birth_is_sent_the_way_indigo_takes_it(text, iso):
    import indigo_client as ic
    assert ic.dob_iso(ic.Passenger("A", "B", "M", text, CHILD)) == iso
    assert ic.dob_iso(ic.Passenger("A", "B", "M", text, ADULT)) is None                  # an adult's never goes


# ── IndiGo's own refusal teaches the right spelling of the infant's gender (found live: it must be TEXT) ────────────────
def _session_with_addpax(monkeypatch, codes, replies):
    ic, sess, sent = session(monkeypatch, codes)
    tries = []
    answers = list(replies)

    async def call(name, url, method, key, body):
        sent[name] = {"url": url, "method": method, "body": body}
        if url == ic.U_ADDPAX:
            tries.append(body["data"]["passengers"][0].get("infant", {}).get("gender"))
            return answers.pop(0) if len(answers) > 1 else answers[0]
        return {ic.U_TOKEN: {"status": 200, "json": {}}, ic.U_SEARCH: {"status": 200, "json": search_answer()},
                ic.U_SELL: {"status": 200, "json": sell_answer_with_fee(codes)}, ic.U_ITIN: {"status": 200, "json": ITIN}}[url]

    monkeypatch.setattr(sess, "call", call)
    return ic, sess, tries


def sell_answer_with_fee(codes):
    a = sell_answer(1)
    a["data"]["passengers"] = slots(*codes)
    a["data"]["breakdown"]["totalAmount"] = 8808.0
    return a


REFUSED = {"status": 400, "json": {"errors": [{"message": "{'$.data.passengers[0].infant.gender': ['The JSON value could not be converted to System.String.']}"}]}}
ACCEPTED = {"status": 200, "json": {"data": {"success": True}}}


async def test_a_refused_infant_gender_is_tried_again_in_the_next_spelling_and_the_right_one_is_remembered(monkeypatch):
    import indigo_client as ic0
    monkeypatch.setitem(ic0.LEARNED, "infant_style", 0)
    ic, sess, tries = _session_with_addpax(monkeypatch, ["ADT", "ADT", "CHD"], [REFUSED, ACCEPTED])
    req = ic.BookingRequest("DEL", "BOM", "09-12-2026", [ic.Passenger(*p) for p in FAMILY], "offer1", "a@b.co", "9876543210")
    res = await ic.run_booking(sess, req, "6ETST001", "addpax")
    assert res.success and tries == ["Male", "M"]                                        # the word was refused, the letter was taken
    assert ic.LEARNED["infant_style"] == 1                                              # ... and the next booking starts with the letter
    ic, sess, tries2 = _session_with_addpax(monkeypatch, ["ADT", "ADT", "CHD"], [ACCEPTED])
    await ic.run_booking(sess, req, "6ETST001", "addpax")
    assert tries2 == ["M"]


async def test_a_refusal_that_is_not_about_the_infant_is_not_retried(monkeypatch):
    other = {"status": 400, "json": {"errors": [{"message": "Passenger name is not valid"}]}}
    import indigo_client as ic0
    monkeypatch.setitem(ic0.LEARNED, "infant_style", 0)
    ic, sess, tries = _session_with_addpax(monkeypatch, ["ADT", "ADT", "CHD"], [other])
    req = ic.BookingRequest("DEL", "BOM", "09-12-2026", [ic.Passenger(*p) for p in FAMILY], "offer1", "a@b.co", "9876543210")
    res = await ic.run_booking(sess, req, "6ETST001", "addpax")
    assert not res.success and "name is not valid" in res.error and len(tries) == 1


async def test_every_spelling_refused_ends_with_indigos_own_words(monkeypatch):
    import indigo_client as ic0
    monkeypatch.setitem(ic0.LEARNED, "infant_style", 0)
    ic, sess, tries = _session_with_addpax(monkeypatch, ["ADT", "ADT", "CHD"], [REFUSED])
    req = ic.BookingRequest("DEL", "BOM", "09-12-2026", [ic.Passenger(*p) for p in FAMILY], "offer1", "a@b.co", "9876543210")
    res = await ic.run_booking(sess, req, "6ETST001", "addpax")
    assert not res.success and "infant.gender" in res.error and len(tries) == len(ic.INFANT_STYLES)


async def test_the_infant_fee_that_the_list_leaves_out_is_reported_from_the_hold(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)
    ic, sess, tries = _session_with_addpax(monkeypatch, ["ADT", "ADT", "CHD"], [ACCEPTED])
    req = ic.BookingRequest("DEL", "BOM", "09-12-2026", [ic.Passenger(*p) for p in FAMILY], "offer1", "a@b.co", "9876543210")
    await ic.run_booking(sess, req, "6ETST001", "addpax")
    assert any(m.startswith("infant fee Rs2250.0 total Rs8808.0") for m in caplog.messages)          # 8808 held - 6558 listed
