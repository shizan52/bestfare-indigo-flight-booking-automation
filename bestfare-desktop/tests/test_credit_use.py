"""Credit use, in numbers, from the admin's side and the traveller's side at once: what is taken, what comes back, and what each of them SEES."""
import pytest

import gate as gatemod
import main_window as mw
from indigo_client import BookingResult
from test_gate_engine import credits, finished, make, order, signed_in, wait_for  # noqa: F401


def book(e, ev, runner_result, offer="offer1", extra=0):
    o = order(offer)
    for i in range(extra):
        o.travellers.append(type(o.travellers[0])(f"PAX{i}", "SHARMA", "F"))
    _g, ids = e.submit(o)
    return ids[0], finished(ev, ids[0])


def settled(store, bid):
    return wait_for(lambda: store.get(bid)["cloud_state"] == "reported")


def test_a_successful_booking_costs_exactly_its_travellers_and_the_record_says_so(live, signed_in, make, store):
    async def runner(bid, req, promo):
        return BookingResult(success=True, pnr="ABC123", kind="record_locator")
    e, ev = make(runner, gate=gatemod.LicenceGate(signed_in))
    bid, r = book(e, ev, None, extra=1)                                         # two travellers
    assert r["status"] == "done" and settled(store, bid)
    rec = store.get(bid)
    assert rec["credits_used"] == 2 and rec["credits_back"] == 0 and mw.credit_cell(rec) == ("2 used", "2 credits used for this booking.")
    assert credits(live, signed_in.k) == 3
    u = live.cloud.licence_detail(signed_in.k["id"])["usage"]
    assert u == {"given": 5, "removed": 0, "used": 2, "returned": 0, "kept": 2}


def test_a_failed_booking_gives_everything_back_and_the_record_and_the_admin_agree(live, signed_in, make, store):
    async def runner(bid, req, promo):
        return BookingResult(success=False, error="no flights available for this route/date")
    e, ev = make(runner, gate=gatemod.LicenceGate(signed_in))
    bid, r = book(e, ev, None, extra=2)                                         # three travellers
    assert r["status"] == "failed" and settled(store, bid)
    rec = store.get(bid)
    assert rec["credits_used"] == 3 and rec["credits_back"] == 3 and mw.credit_cell(rec)[0] == "returned ✓"
    assert credits(live, signed_in.k) == 5 and signed_in.account.credits == 5
    u = live.cloud.licence_detail(signed_in.k["id"])["usage"]
    assert u["used"] == 3 and u["returned"] == 3 and u["kept"] == 0


def test_a_booking_that_is_stopped_by_the_traveller_is_given_back_too(live, signed_in, make, store):
    async def runner(bid, req, promo):
        return BookingResult(success=False, error="stopped by you")
    e, ev = make(runner, gate=gatemod.LicenceGate(signed_in))
    bid, r = book(e, ev, None)
    assert settled(store, bid) and store.get(bid)["credits_back"] == 1 and credits(live, signed_in.k) == 5


def test_when_the_server_will_not_return_it_the_record_says_so_in_plain_words(live, signed_in, make, store):
    live.cloud.set_settings({"refund_daily_limit": 0}, "boss")
    async def runner(bid, req, promo):
        return BookingResult(success=False, error="no flights available for this route/date")
    e, ev = make(runner, gate=gatemod.LicenceGate(signed_in))
    bid, r = book(e, ev, None)
    assert settled(store, bid)
    rec = store.get(bid)
    assert rec["credits_used"] == 1 and rec["credits_back"] == 0
    text, tip = mw.credit_cell(rec)
    assert text == "1 used" and "not returned automatically" in tip and "admin has been told" in tip
    assert credits(live, signed_in.k) == 4
    d = live.cloud.licence_detail(signed_in.k["id"])
    assert d["flagged"] and d["usage"]["kept"] == 1
    live.cloud.admin_refund_booking(d["bookings_list"][0]["id"], "boss")                 # the admin gives it back with one action
    assert credits(live, signed_in.k) == 5


def test_a_run_of_honest_failures_on_a_bad_day_is_still_refunded(live, signed_in, make, store):
    """IndiGo being slow for an afternoon must not eat anybody's credits: the old limits (6 a day, 7 failures in 10) stopped refunds too early."""
    live.cloud.adjust_credits(signed_in.k["id"], 40, "stock up", "boss")
    async def runner(bid, req, promo):
        return BookingResult(success=False, error="IndiGo refused the request")
    e, ev = make(runner, gate=gatemod.LicenceGate(signed_in))
    before = credits(live, signed_in.k)
    for _ in range(10):
        bid, r = book(e, ev, None)
        assert settled(store, bid)
        assert store.get(bid)["credits_back"] == 1, "refund stopped early"
    assert credits(live, signed_in.k) == before


def test_the_cell_for_every_state_of_a_booking():
    cell = mw.credit_cell
    assert cell({"credits_used": None, "status": "done"})[0] == "—"                       # an older record
    assert cell({"credits_used": 1, "status": "running"})[0] == "1 used"
    assert cell({"credits_used": 2, "status": "failed", "credits_back": None})[0] == "2 used…"
    assert cell({"credits_used": 2, "status": "failed", "credits_back": 2})[0] == "returned ✓"
    assert cell({"credits_used": 2, "status": "failed", "credits_back": 0})[0] == "2 used"


def test_a_history_file_from_the_version_before_is_upgraded_without_losing_anything(tmp_path):
    import sqlite3
    from store import Store
    path = tmp_path / "old.db"
    db = sqlite3.connect(str(path))
    db.executescript("""CREATE TABLE bookings (id TEXT PRIMARY KEY, grp TEXT NOT NULL, created_at REAL NOT NULL, origin TEXT NOT NULL, dest TEXT NOT NULL,
        travel_date TEXT NOT NULL, offer TEXT NOT NULL, pax_count INTEGER NOT NULL, pax_json TEXT NOT NULL DEFAULT '[]', phone TEXT NOT NULL DEFAULT '',
        email TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, pnr TEXT, kind TEXT, fare REAL, discount REAL, coupon_applied INTEGER, hold_until TEXT, pay_url TEXT,
        error TEXT, finished_at REAL, wiped INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO bookings (id, grp, created_at, origin, dest, travel_date, offer, pax_count, status, pnr) VALUES ('OLD1','g',1.0,'DEL','BOM','2026-01-01','offer1',1,'done','ABC123');""")
    db.commit()
    db.close()
    s = Store(path)
    old = s.get("OLD1")
    assert old["pnr"] == "ABC123" and old["credits_used"] is None and mw.credit_cell(old)[0] == "—"           # an old record says nothing it does not know
    s.update("OLD1", credits_used=2, credits_back=0)
    assert s.get("OLD1")["credits_used"] == 2
    s.close()
