"""Booking time: a traveller who books again soon must not pay for a new browser and a full warm-up every time."""
import time

import engine as eng
from indigo_client import BookingResult
from test_engine import FakeEdge, finished, make, order  # noqa: F401


def browser_runner(box):
    """A booking that uses the booking browser the way the real one does (it asks the engine for it)."""
    async def run(bid, req, promo):
        await box["e"]._browser()
        return BookingResult(success=True, pnr="PNRABC", kind="record_locator")
    return run


def waited(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_the_browser_is_closed_at_once_when_a_booking_is_done_no_edge_stays_behind():
    assert eng.AFTER_BOOKING_CLOSE_S == 0.0 and eng.IDLE_POLL_S <= 1.0


def test_it_closes_right_after_the_pnr_and_each_booking_starts_in_a_fresh_browser(make):
    edge, box = FakeEdge(), {}
    e, ev = make(browser_runner(box), edge=edge, idle=60.0, after=0.0)
    box["e"] = e
    _g, first = e.submit(order("offer1"))
    assert finished(ev, first[0])["status"] == "done"
    assert waited(lambda: edge.closed == 1)                                           # gone within a second of the PNR
    _g, second = e.submit(order("offer2"))
    assert finished(ev, second[0])["status"] == "done"
    assert waited(lambda: edge.closed == 2) and edge.launches == 2                    # two bookings, two fresh browsers, none left


def test_a_booking_waiting_in_the_queue_is_not_left_without_its_browser(make):
    edge, box = FakeEdge(), {}
    e, ev = make(browser_runner(box), edge=edge, idle=60.0, after=0.0)
    box["e"] = e
    o1, o2 = order("offer1"), order("offer2")
    _g, a = e.submit(o1)
    _g, b = e.submit(o2)                                                              # (queued behind the first)
    assert finished(ev, a[0])["status"] == "done" and finished(ev, b[0])["status"] == "done"
    assert waited(lambda: edge.closed >= 1)


def test_a_locked_window_closes_the_browser_at_once(make):
    edge, box = FakeEdge(), {}
    e, ev = make(browser_runner(box), edge=edge, idle=60.0, after=300.0)
    box["e"] = e
    _g, ids = e.submit(order("offer1"))
    finished(ev, ids[0])
    time.sleep(0.2)
    assert edge.closed == 0
    e.release_browser()
    assert waited(lambda: edge.closed == 1)


def test_a_page_that_has_had_enough_activity_needs_only_a_moment_more_before_its_first_call(make):
    edge = FakeEdge()
    e, _ev = make(None, edge=edge)
    e._seasoned = True                                                                  # (an installation IndiGo already knows: a new one warms up longer, see test_self_healing)
    e._warm_launch, e._warm_at = getattr(edge, "launches", 0), time.monotonic()
    e._warm_credit = 0.0
    assert e.warmup_for() is None                                                       # a cold page: the full 22-38 s
    e._warm_credit = 12.0
    lo, hi = e.warmup_for()
    assert lo >= 10 and hi >= 20                                                        # partly warm: what is still missing
    e._warm_credit = 36.0
    assert e.warmup_for() == (1.0, 2.5)                                                 # warm: no 2-4 s of wandering for nothing
