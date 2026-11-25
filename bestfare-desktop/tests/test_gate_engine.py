"""The booking gate inside the engine, with a REAL server: the credits are taken before anything starts, the promo code comes from the
server, a failure gives the credits back, a stop is reported, and nothing is lost when the report cannot be made at once."""
import asyncio
import time

import pytest

import engine as eng
import gate as gatemod
import licence as lic
from cloud import CloudClient
from device import DeviceKey, Vault
from indigo_client import BookingResult
from test_engine import Events, FakeEdge, finished, make, order  # noqa: F401


@pytest.fixture()
def signed_in(live, tmp_path):
    """An activated licence with 5 credits on a live server."""
    k = live.cloud.create_licence("Rahul Sharma", credits=5, actor="boss")
    vault = Vault(tmp_path / "v.bin")
    L = lic.Licence(CloudClient(DeviceKey(vault), 1, base_url=live.url, server_public=live.public), vault, "test")
    L.activate(k["key"])
    L.k = k
    return L


def credits(live, k):
    return live.cloud.db.scalar("SELECT credits FROM licences WHERE id=?", (k["id"],))


def wait_for(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_the_credits_are_taken_before_the_booking_starts_and_the_code_comes_from_the_server(live, signed_in, make, store):
    seen = {}

    async def runner(bid, req, promo):
        seen["promo"] = promo
        seen["credits_while_running"] = credits(live, signed_in.k)                # already paid: 2 travellers = 2 credits
        return BookingResult(success=True, pnr="ABC123", kind="record_locator")

    e, ev = make(runner, gate=gatemod.LicenceGate(signed_in))
    o = order("offer1")
    o.travellers.append(type(o.travellers[0])("PRIYA", "SHARMA", "F"))
    _g, ids = e.submit(o)
    r = finished(ev, ids[0])
    assert r["status"] == "done" and seen["promo"] == "6ETST001" and seen["credits_while_running"] == 3
    assert wait_for(lambda: store.get(ids[0])["cloud_state"] == "reported")
    assert credits(live, signed_in.k) == 3                                        # a successful booking keeps its charge
    b = live.cloud.licence_detail(signed_in.k["id"])["bookings_list"][0]
    assert b["status"] == "done" and b["pnr"] == "ABC123" and b["travellers"] == 2


def test_a_failed_booking_gets_its_credits_back(live, signed_in, make, store):
    async def runner(bid, req, promo):
        return BookingResult(success=False, error="no flights available for this route/date")

    e, ev = make(runner, gate=gatemod.LicenceGate(signed_in))
    _g, ids = e.submit(order("offer2"))
    r = finished(ev, ids[0])
    assert r["status"] == "failed"
    assert wait_for(lambda: credits(live, signed_in.k) == 5)                      # refunded
    assert wait_for(lambda: signed_in.account.credits == 5)


def test_without_enough_credits_the_booking_never_starts_and_the_words_are_plain(live, signed_in, make):
    live.cloud.adjust_credits(signed_in.k["id"], -4, "test", "boss")             # 1 credit left
    ran = []

    async def runner(bid, req, promo):
        ran.append(1)
        return BookingResult(success=True, pnr="X")

    e, ev = make(runner, gate=gatemod.LicenceGate(signed_in))
    o = order("offer1")
    o.travellers.append(type(o.travellers[0])("PRIYA", "SHARMA", "F"))
    _g, ids = e.submit(o)
    r = finished(ev, ids[0])
    assert r["status"] == "failed" and ran == []
    assert r["error"] == "You need 2 credits for this booking but have 1. Please contact your admin to get more credits."
    assert credits(live, signed_in.k) == 1


def test_a_blocked_user_cannot_start_a_booking(live, signed_in, make):
    live.cloud.set_status(signed_in.k["id"], "blocked", "shared the key", "boss")
    ran = []

    async def runner(bid, req, promo):
        ran.append(1)
        return BookingResult(success=True, pnr="X")

    e, ev = make(runner, gate=gatemod.LicenceGate(signed_in))
    _g, ids = e.submit(order("offer1"))
    r = finished(ev, ids[0])
    assert r["status"] == "failed" and ran == []
    assert "stopped" in r["error"].lower() or "sign in" in r["error"].lower()


def test_stopping_a_booking_is_reported_and_refunded(live, signed_in, make, store):
    started = []

    async def runner(bid, req, promo):
        started.append(1)
        await asyncio.sleep(30)
        return BookingResult(success=True, pnr="NEVER")

    e, ev = make(runner, gate=gatemod.LicenceGate(signed_in))
    _g, ids = e.submit(order("offer1"))
    assert wait_for(lambda: started)
    assert credits(live, signed_in.k) == 4
    e.stop(ids[0])
    finished(ev, ids[0])
    assert wait_for(lambda: credits(live, signed_in.k) == 5)


def test_a_booking_indigo_never_takes_gives_the_credit_back_and_nothing_is_locked(live, signed_in, make, monkeypatch):
    """IndiGo refuses every try: the engine tries again by itself, gives up politely, the credit comes back - and the next booking is not turned away."""
    from indigo_client import IndigoBlocked
    monkeypatch.setattr(eng, "RECOVERY", ((False, (0.01, 0.02), (1.0, 2.0)),))
    calls = []

    async def runner(bid, req, promo):
        calls.append(bid)
        if len(calls) <= 2:
            raise IndigoBlocked("search: request blocked at the edge (TypeError: Failed to fetch)", kind="fetch", name="search")
        return BookingResult(success=True, pnr="LATER1", kind="record_locator")

    async def online(self):
        return True
    monkeypatch.setattr(eng.Engine, "_online", online)
    e, ev = make(runner, gate=gatemod.LicenceGate(signed_in))
    _g, a = e.submit(order("offer1"))
    ra = finished(ev, a[0])
    assert ra["status"] == "failed" and "press Book again" in ra["error"] and len(calls) == 2          # one try + one calm recovery, then it says so
    assert wait_for(lambda: credits(live, signed_in.k) == 5)                                          # the credit is back
    _g, b = e.submit(order("offer1"))                                                                  # nothing is locked: the very next booking goes
    assert finished(ev, b[0])["status"] == "done" and len(calls) == 3


def test_a_result_that_could_not_be_reported_is_reported_at_the_next_start(live, signed_in, make, store, monkeypatch):
    gate = gatemod.LicenceGate(signed_in)
    real = gate.finish
    monkeypatch.setattr(gate, "finish", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no connection")))
    monkeypatch.setattr(time, "sleep", lambda s: None)                             # (do not really wait between the tries)

    async def runner(bid, req, promo):
        return BookingResult(success=False, error="booking timed out")

    e, ev = make(runner, gate=gate)
    _g, ids = e.submit(order("offer1"))
    finished(ev, ids[0])
    assert wait_for(lambda: len(store.unreported()) == 1) and credits(live, signed_in.k) == 4      # the server has not been told
    monkeypatch.setattr(gate, "finish", real)                                      # the connection is back; the app is started again
    e2, ev2 = make(None, gate=gate)
    assert wait_for(lambda: credits(live, signed_in.k) == 5)
    assert wait_for(lambda: store.unreported() == [])


def test_no_offer_code_is_written_to_disk_or_the_log(live, signed_in, make, store, caplog, tmp_path):
    import logging

    async def runner(bid, req, promo):
        logging.getLogger("engine").info("working with %s", promo)
        return BookingResult(success=True, pnr="ABC123", kind="record_locator")

    e, ev = make(runner, gate=gatemod.LicenceGate(signed_in), debug=True)
    _g, ids = e.submit(order("offer1"))
    finished(ev, ids[0])
    lines = [x["text"] for x in ev if x["type"] == "log"]
    assert not any("6ETST001" in t for t in lines)
    assert b"6ETST001" not in store.path.read_bytes() and b"6ETST001" not in (tmp_path / "v.bin").read_bytes()
    assert "6ETST001" not in eng.SECRET_CODES and not eng.SECRET_CODES                # forgotten when the booking ended
