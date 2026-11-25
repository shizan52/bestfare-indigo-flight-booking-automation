"""My bookings: a PNR lasts 30 minutes, so the page shows exactly how long is left - live - and when it has ended."""
import time
from datetime import datetime, timedelta, timezone

import pytest
from PyQt6.QtGui import QColor

import main_window as mw
from main_window import BookingsPage
from theme import C


def iso(minutes=0.0, seconds=0.0):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes, seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def add(store, status="done", pnr="ABC123", hold=None, kind="record_locator", finished_ago_min=1, error=None, offer="offer1"):
    bid = store.add_booking(grp="G", origin="DEL", dest="BOM", travel_date="2026-12-01", offer=offer,
                            travellers=[{"first": "A", "last": "B", "gender": "M"}], phone="9000000001", email="a@b.co")
    fields = {"status": status, "finished_at": time.time() - finished_ago_min * 60}
    if status == "done":
        fields.update(pnr=pnr, kind=kind, fare=5132.0, hold_until=hold)
    if status == "failed":
        fields["error"] = error or "IndiGo has no seats on this route and date."
    store.update(bid, **fields)
    return bid


def page(qapp, store):
    p = BookingsPage(store, lambda t: None)
    p.refresh()
    return p


def cells(p, row):
    t = p.table
    return {name: t.item(row, i).text() for i, name in enumerate(p.COLS)}


def test_a_held_pnr_shows_when_to_pay_by_and_how_long_is_left(qapp, store):
    d = iso(minutes=24, seconds=30)
    add(store, hold=d)
    p = page(qapp, store)
    c = cells(p, 0)
    when = datetime.fromisoformat(d.replace("Z", "+00:00"))
    assert c["Status"] == "On hold" and c["Pay before"] == mw.clock(when)
    left = c["Time left"]
    assert left.count(":") == 1 and left.startswith(("24:", "23:")) and c["PNR"] == "ABC123"


def test_the_countdown_runs_live_and_the_pnr_expires_by_itself(qapp, store):
    add(store, hold=iso(seconds=2))
    p = page(qapp, store)
    assert cells(p, 0)["Status"] == "On hold" and cells(p, 0)["Time left"] in ("0:02", "0:01", "0:03")
    first = cells(p, 0)["Time left"]
    time.sleep(1.1)
    p.tick()
    assert cells(p, 0)["Time left"] != first or cells(p, 0)["Status"] == "Expired"
    time.sleep(1.6)
    p.tick()
    c = cells(p, 0)
    assert c["Status"] == "Expired" and c["Time left"] == "Expired"
    pnr = p.table.item(0, p.C_PNR)
    assert pnr.font().strikeOut() and pnr.foreground().color() == QColor(C["muted"])


def test_the_last_five_minutes_are_marked_urgent(qapp, store):
    add(store, hold=iso(minutes=3))
    add(store, pnr="LATER1", hold=iso(minutes=20))
    p = page(qapp, store)
    rows = {p.table.item(r, p.C_PNR).text(): r for r in range(2)}
    urgent = p.table.item(rows["ABC123"], p.C_LEFT).foreground().color()
    calm = p.table.item(rows["LATER1"], p.C_LEFT).foreground().color()
    assert urgent == QColor(C["bad"]) and calm == QColor(C["warn"])


def test_an_expired_pnr_is_shown_as_expired_with_the_time_it_ended(qapp, store):
    d = iso(minutes=-12)
    add(store, hold=d)
    p = page(qapp, store)
    c = cells(p, 0)
    assert c["Status"] == "Expired" and c["Time left"] == "Expired"
    assert c["Pay before"] == mw.clock(datetime.fromisoformat(d.replace("Z", "+00:00")))


def test_without_a_hold_time_the_thirty_minutes_count_from_the_booking(qapp, store):
    add(store, hold=None, finished_ago_min=10)
    c = cells(page(qapp, store), 0)
    assert c["Status"] == "On hold" and c["Time left"].startswith(("19:", "20:"))
    store2_rec = store.bookings()[0]
    assert mw.deadline(store2_rec) is not None
    assert abs((mw.deadline(store2_rec) - datetime.fromtimestamp(store2_rec["finished_at"], tz=timezone.utc)).total_seconds() - 1800) < 1


def test_other_states_are_clear(qapp, store):
    add(store, status="failed")
    add(store, status="done", pnr="ORDER1", kind="order_id")
    bid = add(store, status="done", hold=iso(minutes=5))
    store.update(bid, status="running", finished_at=None)
    p = page(qapp, store)
    by_status = {cells(p, r)["Status"]: cells(p, r) for r in range(3)}
    assert set(by_status) == {"Not booked", "Waiting for payment", "Booking now…"}
    assert by_status["Not booked"]["Time left"] == "—" and by_status["Waiting for payment"]["Time left"] == "—"
    assert p.table.item([r for r in range(3) if cells(p, r)["Status"] == "Not booked"][0], p.C_STATUS).toolTip().startswith("IndiGo has no seats")


def test_the_page_explains_the_thirty_minutes(qapp, store):
    p = BookingsPage(store, lambda t: None)
    text = " ".join(lb.text() for lb in p.findChildren(type(p.empty)))
    assert "30 minutes" in text and "cancelled automatically" in text


def test_hold_state_is_a_pure_function_of_the_record():
    rec = {"status": "done", "kind": "record_locator", "hold_until": iso(minutes=10), "finished_at": time.time()}
    assert mw.hold_state(rec)[0] == "held"
    rec["hold_until"] = iso(minutes=-1)
    assert mw.hold_state(rec)[0] == "expired"
    assert mw.hold_state({"status": "waiting"})[0] == "waiting" and mw.hold_state({"status": "failed"})[0] == "failed"
    assert mw.mmss(125) == "2:05" and mw.mmss(59) == "0:59"


@pytest.mark.parametrize("secs,state", [(1790, "held"), (4, "held"), (-1, "expired")])
def test_the_edges_of_the_hold(secs, state):
    rec = {"status": "done", "kind": "record_locator", "hold_until": iso(seconds=secs), "finished_at": time.time()}
    assert mw.hold_state(rec)[0] == state
