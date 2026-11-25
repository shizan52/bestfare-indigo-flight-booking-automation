"""The flights a traveller may choose from ("Choose your preferred flight"): one entry per bookable flight, each with its LOWEST
fare, for exactly the requested airports - and the booking that goes ahead with the flight that was chosen.

tests/data/search_answer_real.json is IndiGo's real answer of 2026-11-18 for Delhi -> Mumbai on 30 Nov (searched with the Defence
offer), trimmed to ten journeys but with the real field names and values: nearby airports (Hindon, Navi Mumbai, Dixon),
connections, a flight that lands the next day, flights whose lowest fare carries the offer and flights whose does not.
"""
import json
from pathlib import Path

import pytest

import indigo_client as ic

REAL = json.loads((Path(__file__).parent / "data" / "search_answer_real.json").read_text(encoding="utf-8"))


def options(origin="DEL", dest="BOM"):
    return ic.flight_options(REAL, origin, dest)


def by_seg(opts, seg, dep_prefix=""):
    return next(o for o in opts if o["seg"] == seg and o["dep"].startswith(dep_prefix))


def test_only_the_requested_airports_are_offered():
    opts = options()
    assert {(o["origin"], o["dest"]) for o in opts} == {("DEL", "BOM")}
    assert len(opts) == 7                                               # ten journeys minus Hindon, Navi Mumbai and Dixon (3 nearby)
    assert not any(o["seg"] in ("HDO5175BOM", "DEL5289NMI", "DXN5068BOM") for o in opts)


def test_without_airports_everything_is_listed():
    assert len(ic.flight_options(REAL)) == 10


def test_the_list_is_in_departure_order():
    deps = [o["dep"] for o in options()]
    assert deps == sorted(deps) and deps[0].startswith("2026-11-30T02:20")


def test_every_flight_shows_its_own_lowest_fare():
    opts = options()
    # the offer fare (R, 5438, was 7498) is the lowest of that flight; a flight without an offer fare shows its plain lowest (B 7811)
    first = by_seg(opts, "DEL864BOM")
    assert (first["amount"], first["product"], first["offer"], first["original"]) == (5438, "R", True, 7498)
    plain = by_seg(opts, "DEL353BOM")
    assert (plain["amount"], plain["product"], plain["offer"], plain["original"]) == (7811, "B", False, None)
    # business-class fares (BR / BC) on a connection are never "the lowest"
    hyd = by_seg(opts, "DELHYDBOM")
    assert (hyd["amount"], hyd["product"]) == (5962, "R")


def test_flight_numbers_stops_and_the_city_where_a_connection_changes():
    opts = options()
    assert by_seg(opts, "DEL864BOM")["flights"] == ["6E 864"] and by_seg(opts, "DEL864BOM")["stops"] == 0
    assert by_seg(opts, "DEL864BOM")["via"] == []
    nag = by_seg(opts, "DELNAGBOM")
    assert nag["flights"] == ["6E 2402", "6E 6282"] and nag["stops"] == 1 and nag["via"] == ["Nagpur"]


def test_the_flying_time_counts_the_wait_of_a_connection():
    opts = options()
    assert by_seg(opts, "DEL864BOM")["minutes"] == 150                          # 10:00Z -> 12:30Z
    assert by_seg(opts, "DELNAGBOM")["minutes"] > 300                            # a connection: hours, not 2.5
    assert by_seg(opts, "DEL6114BOM")["arr"].startswith("2026-12-01")           # lands the next day


def test_the_cheapest_option_is_what_the_bot_has_always_booked():
    best = ic.cheapest(REAL, "DEL", "BOM")
    low = min(options(), key=lambda o: (o["amount"], o["dep"]))
    assert (best["journeyKey"], best["fareKey"], best["amount"]) == (low["key"], low["fare_key"], low["amount"])
    assert best["seg"] == "DEL864BOM" and best["amount"] == 5438


def test_sold_out_flights_and_disabled_fares_are_not_offered():
    d = json.loads(json.dumps(REAL))
    js = d["data"]["trips"][0]["journeysAvailable"]
    js[0]["isSold"] = True
    for pf in js[1]["passengerFares"]:
        pf["isActive"] = False
    keys = {o["key"] for o in ic.flight_options(d, "DEL", "BOM")}
    assert js[0]["journeyKey"] not in keys and js[1]["journeyKey"] not in keys
    assert len(keys) == 5


def test_an_answer_without_flights_gives_an_empty_list():
    assert ic.flight_options({}) == [] and ic.flight_options({"data": {"trips": []}}) == []
    assert ic.flight_options({"data": {"trips": [{"journeysAvailable": []}]}}, "DEL", "BOM") == []


# ── the booking goes ahead with the flight that was chosen ──────────────────────────────────────────────────────────
from test_payloads import ITIN, FakeHuman, sell_answer  # noqa: E402  (the same fakes as the payload tests)


def booking_session(monkeypatch, answer):
    monkeypatch.setattr(ic.Pace, "min_gap", 0.0)
    monkeypatch.setattr(ic.Pace, "jitter", 0.0)
    monkeypatch.setattr(ic.random, "uniform", lambda a, b: 0)

    class Tab:
        async def cookies(self, _u): return []
        async def delete_cookie(self, *a, **k): ...

    waits = []

    class Human(FakeHuman):
        async def wander(self, secs=0):
            waits.append(secs)
            import asyncio
            await asyncio.sleep(0.01)

    sess = ic.IndigoSession(Tab(), Human(), [])
    answers = {ic.U_TOKEN: {"status": 200, "json": {}}, ic.U_SEARCH: {"status": 200, "json": answer},
               ic.U_SELL: {"status": 200, "json": sell_answer(1)}, ic.U_ADDPAX: {"status": 200, "json": {"data": {"success": True}}},
               ic.U_ITIN: {"status": 200, "json": ITIN}}
    sent = {}

    async def call(name, url, method, key, body):
        sent[name] = body
        return answers[url]

    async def pause(*_a): ...
    monkeypatch.setattr(sess, "call", call)
    monkeypatch.setattr(sess, "human_pause", pause)
    return sess, sent, waits


REQ = ic.BookingRequest("DEL", "BOM", "30-11-2026", [ic.Passenger("RAHUL", "SHARMA", "M")], "offer1", "a@b.co", "9876543210")


async def psp():
    return None


async def test_the_flight_the_traveller_chose_is_the_one_that_is_held(monkeypatch):
    sess, sent, _w = booking_session(monkeypatch, REAL)
    seen = []

    async def choose(opts):
        seen.append(opts)
        return by_seg(opts, "DEL449BOM")["key"]                 # NOT the cheapest

    res = await ic.run_booking(sess, REQ, "6ETST001", "payment", psp=psp, choose=choose)
    key = sent["sell"]["keys"][0]
    want = by_seg(seen[0], "DEL449BOM")
    assert (key["journeyKey"], key["fareAvailabilityKey"]) == (want["key"], want["fare_key"])
    assert res.flight["seg"] == "DEL449BOM" and res.flight["key"] == want["key"]
    assert res.success and res.pnr == "ABC123"
    assert len(seen) == 1 and len(seen[0]) == 7                  # the chooser saw exactly the list of the requested airports


async def test_nothing_chosen_means_nothing_is_held(monkeypatch):
    sess, sent, _w = booking_session(monkeypatch, REAL)

    async def choose(_opts):
        return None

    res = await ic.run_booking(sess, REQ, "6ETST001", "payment", psp=psp, choose=choose)
    assert not res.success and res.error == "no flight was chosen" and "sell" not in sent and res.flight is None


async def test_a_choice_that_is_not_on_the_list_is_refused(monkeypatch):
    sess, sent, _w = booking_session(monkeypatch, REAL)

    async def choose(_opts):
        return "JK-of-a-nearby-airport"

    res = await ic.run_booking(sess, REQ, "6ETST001", "payment", psp=psp, choose=choose)
    assert not res.success and "sell" not in sent


async def test_without_a_chooser_the_cheapest_flight_is_booked_as_always(monkeypatch):
    sess, sent, _w = booking_session(monkeypatch, REAL)
    res = await ic.run_booking(sess, REQ, "6ETST001", "payment", psp=psp)
    assert sent["sell"]["keys"][0]["journeyKey"] == by_seg(options(), "DEL864BOM")["key"] and res.success and res.flight is None


async def test_the_page_keeps_moving_while_the_traveller_decides(monkeypatch):
    import asyncio
    sess, _sent, waits = booking_session(monkeypatch, REAL)

    async def choose(opts):
        await asyncio.sleep(0.2)                                  # the traveller reads the list
        return opts[0]["key"]

    res = await ic.run_booking(sess, REQ, "6ETST001", "payment", psp=psp, choose=choose)
    assert res.success and len(waits) >= 3                        # (the first one is the warm-up before the first call)


async def test_a_search_only_run_does_not_ask_anyone(monkeypatch):
    sess, _sent, _w = booking_session(monkeypatch, REAL)
    asked = []

    async def choose(opts):
        asked.append(opts)
        return opts[0]["key"]

    res = await ic.run_booking(sess, REQ, "6ETST001", "search", choose=choose)
    assert res.success and asked == []
