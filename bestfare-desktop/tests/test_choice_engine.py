"""'Choose your flight' in the engine: the booking shows the flights, waits for the traveller, goes ahead with exactly the flight that
was picked, never counts the waiting as work time, and ends politely when nobody chooses or the booking is stopped."""
import asyncio
import time

import pytest

import engine as eng
import flights
from indigo_client import BookingResult
from test_engine import Events, FakeEdge, finished, make, order  # noqa: F401  (the same stand-ins as the engine tests)


def sample_options():
    def o(i, dep, arr, amount, original=None, stops=0, via=()):
        return {"key": f"K{i}", "fare_key": f"F{i}", "amount": float(amount), "original": float(original) if original else None,
                "offer": original is not None, "product": "R", "seg": f"S{i}", "stops": stops, "dep": f"2030-01-05T{dep}:00",
                "arr": f"2030-01-05T{arr}:00", "minutes": 150, "flights": [f"6E {100 + i}"], "via": list(via),
                "origin": "DEL", "dest": "BOM", "origin_city": "Delhi", "dest_city": "Mumbai"}
    return [o(1, "06:00", "08:30", 6500), o(2, "09:15", "11:45", 5438, 7498), o(3, "21:00", "23:30", 7100, None, 1, ("Nagpur",))]


def chooser(holder, picked=None):
    """A runner that asks the traveller, then books the flight it was told to (or fails when nothing was chosen)."""
    async def runner(bid, req, promo):
        opts = sample_options()
        key = await holder["e"].ask_flight(bid, opts)
        if picked is not None:
            picked.append(key)
        if key is None:
            return BookingResult(success=False, error="no flight was chosen")
        flight = next(o for o in opts if o["key"] == key)
        return BookingResult(success=True, pnr="PNRCH", kind="record_locator", fare_amount=flight["amount"], flight=flight)
    return runner


def test_the_list_is_shown_and_the_booking_waits_until_a_flight_is_picked(make, store):
    holder, picked = {}, []
    e, ev = make(chooser(holder, picked))
    holder["e"] = e
    _g, ids = e.submit(order("offer2"))
    ask = ev.wait(lambda x: x["type"] == "choose")[0]
    assert ask["id"] == ids[0] and ask["seconds"] == eng.CHOICE_WAIT_S and [f["key"] for f in ask["flights"]] == ["K1", "K2", "K3"]
    assert all("fare_key" not in f for f in ask["flights"])                       # what the window gets has no booking internals
    time.sleep(0.4)
    assert store.get(ids[0])["status"] == "running" and not any(x["type"] == "finished" for x in ev)     # it really waits
    e.choose_flight("SOMEONE-ELSE", "K1")                                         # a pick for another booking is ignored
    e.choose_flight(ids[0], "NOT-ON-THE-LIST")                                    # and so is a flight that was never offered
    time.sleep(0.2)
    assert not any(x["type"] == "finished" for x in ev)
    e.choose_flight(ids[0], "K3")
    r = finished(ev, ids[0])
    assert picked == ["K3"] and r["status"] == "done" and r["pnr"] == "PNRCH"
    assert r["flight"] == "6E 103 · 21:00 → 23:30" and r["fare"] == 7100.0
    notes = [x["text"] for x in ev if x["type"] == "note" and x["id"] == ids[0]]
    assert "Your flight: 6E 103 · 21:00 → 23:30 · ₹7,100" in notes
    got = [x for x in ev if x["type"] == "chosen"]
    assert len(got) == 1 and got[0]["flight"]["key"] == "K3"
    assert e._choice is None


def test_an_offer_fare_says_how_much_it_saves(make):
    holder = {}
    e, ev = make(chooser(holder))
    holder["e"] = e
    _g, ids = e.submit(order("offer2"))
    ev.wait(lambda x: x["type"] == "choose")
    e.choose_flight(ids[0], "K2")
    finished(ev, ids[0])
    notes = [x["text"] for x in ev if x["type"] == "note"]
    assert any("includes your offer" in n and "you save ₹2,060" in n for n in notes)
    assert not any(c in n for n in notes for c in ("TESTCODE1", "TESTCODE2", "6ETST001", "PROMO2"))


def test_nobody_choosing_in_time_ends_politely_with_nothing_booked(make, store, monkeypatch):
    monkeypatch.setattr(eng, "CHOICE_WAIT_S", 0.4)
    holder, picked = {}, []
    e, ev = make(chooser(holder, picked))
    holder["e"] = e
    _g, ids = e.submit(order())
    r = finished(ev, ids[0])
    assert picked == [None] and r["status"] == "failed" and not r.get("pnr")
    assert r["error"] == "No flight was chosen, so nothing was booked. Please try again and pick a flight."
    assert any(x["type"] == "note" and x["text"] == "No flight was chosen in time." for x in ev)
    assert [x["flight"] for x in ev if x["type"] == "chosen"] == [None]
    assert e._choice is None


def test_stopping_the_booking_while_the_list_is_open_cancels_the_wait(make, store):
    holder = {}
    e, ev = make(chooser(holder))
    holder["e"] = e
    _g, ids = e.submit(order())
    ev.wait(lambda x: x["type"] == "choose")
    e.stop(ids[0])
    r = finished(ev, ids[0])
    assert r["status"] == "failed" and r["error"] == "You stopped this booking." and not r.get("flight")
    assert e._choice is None and not e.busy


def test_the_time_spent_choosing_does_not_count_as_work_time(make, store, monkeypatch):
    monkeypatch.setattr(eng, "BOOKING_TIMEOUT_S", 1.0)
    holder = {}

    async def runner(bid, req, promo):
        await asyncio.sleep(0.3)                                                  # 0.3 s of work ...
        key = await holder["e"].ask_flight(bid, sample_options())                # ... the traveller reads for ~1.6 s ...
        await asyncio.sleep(0.3)                                                  # ... 0.3 s of work: 0.6 s < the 1.0 s allowed
        return BookingResult(success=True, pnr="PNRSLOW", kind="record_locator", flight=next(o for o in sample_options() if o["key"] == key))

    e, ev = make(runner)
    holder["e"] = e
    t0 = time.monotonic()
    _g, ids = e.submit(order())
    ev.wait(lambda x: x["type"] == "choose")
    time.sleep(1.6)
    e.choose_flight(ids[0], "K1")
    r = finished(ev, ids[0])
    assert time.monotonic() - t0 > 1.6 and r["status"] == "done" and r["pnr"] == "PNRSLOW"


def test_work_that_really_hangs_is_still_stopped(make, monkeypatch):
    monkeypatch.setattr(eng, "BOOKING_TIMEOUT_S", 0.6)
    holder = {}

    async def runner(bid, req, promo):
        key = await holder["e"].ask_flight(bid, sample_options())
        assert key == "K1"
        await asyncio.sleep(5)                                                    # hangs AFTER the choice
        return BookingResult(success=True, pnr="NEVER")

    e, ev = make(runner)
    holder["e"] = e
    _g, ids = e.submit(order())
    ev.wait(lambda x: x["type"] == "choose")
    e.choose_flight(ids[0], "K1")
    r = finished(ev, ids[0])
    assert r["status"] == "failed" and "longer than usual" in r["error"]


def test_the_record_must_show_the_flight_that_was_picked(make):
    def record(req, departs):
        iso = f"{req.date[6:]}-{req.date[3:5]}-{req.date[:2]}"
        return {"data": {"passengers": [{"name": {"first": "RAHUL", "last": "SHARMA"}, "info": {"gender": 1}}],
                         "journeysDetail": [{"journeydetail": {"origin": "DEL", "destination": "BOM", "departure": f"{iso}T{departs}:00"}}],
                         "contacts": [{"homePhone": "98****10", "emailAddress": "a@b.co"}]}}, iso

    n = {"i": 0}

    async def runner(bid, req, promo):
        n["i"] += 1
        departs = "09:15" if n["i"] == 1 else "11:00"                              # the second record shows another flight
        itin, iso = record(req, departs)
        flight = {**sample_options()[1], "dep": f"{iso}T09:15:00", "arr": f"{iso}T11:45:00"}
        return BookingResult(success=True, pnr=f"PNR{n['i']}", kind="record_locator", coupon_applied=True, itinerary=itin, flight=flight)

    e, ev = make(runner)
    _g, a = e.submit(order("offer2"))
    ra = finished(ev, a[0])
    assert ra["verified"] == "ok" and ra["flight"].startswith("6E 102 · 09:15")
    _g, b = e.submit(order("offer2"))
    rb = finished(ev, b[0])
    assert rb["verified"] == "mismatch" and "different flight" in rb["verify_note"] and "11:00 instead of 09:15" in rb["verify_note"]


def test_a_failed_booking_keeps_no_flight_line(make, store):
    async def runner(bid, req, promo):
        return BookingResult(success=False, error="fare could not be held (HTTP 400)")

    e, ev = make(runner)
    _g, ids = e.submit(order())
    r = finished(ev, ids[0])
    assert r["status"] == "failed" and r["flight"] is None


@pytest.mark.parametrize("err,words", [("no flight was chosen", "No flight was chosen"), ("stopped by you", "You stopped")])
def test_friendly_words_for_the_choice_outcomes(err, words):
    assert eng.friendly(err).startswith(words)


def test_applying_the_offer_is_announced_only_after_the_traveller_has_chosen(make):
    """Found in the live Activity page: 'Applying your offer' used to be written BEFORE 'Choose your flight'."""
    import logging
    log = logging.getLogger("indigo_client")
    holder = {}

    async def runner(bid, req, promo):
        log.info("indigo token      -> 200 (300 ms) ")
        log.info("indigo search     -> 200 (900 ms) ")
        key = await holder["e"].ask_flight(bid, sample_options())
        log.info("chosen S2 stops=0 product=R Rs5438")
        log.info("indigo sell       -> 200 (800 ms) ")
        log.info("coupon %s applied=True (IndiGo says '%s', discount Rs2060.0)", promo, promo)
        log.info("indigo addpax     -> 200 (700 ms) ")
        return BookingResult(success=True, pnr="PNRORD", kind="record_locator", flight=next(o for o in sample_options() if o["key"] == key))

    e, ev = make(runner)
    holder["e"] = e
    e.expect_choice = True                                    # (what a real booking sets)
    _g, ids = e.submit(order("offer1"))
    ev.wait(lambda x: x["type"] == "choose")
    time.sleep(0.3)
    stages_while_choosing = [x["stage"] for x in ev if x["type"] == "stage"]
    assert stages_while_choosing == ["search"]                # still "Finding your flights": the offer step has not started
    e.choose_flight(ids[0], "K2")
    finished(ev, ids[0])
    kinds = [(x["type"], x.get("stage")) for x in ev if x["type"] in ("stage", "choose", "chosen")]
    assert kinds == [("stage", "search"), ("choose", None), ("chosen", None), ("stage", "hold"), ("stage", "travellers"), ("stage", "pnr")]
