"""A stand-in for the booking engine: the window can be driven and checked without a browser or IndiGo."""
from __future__ import annotations

import time
from datetime import date

import forms


class FakeEngine:
    def __init__(self, store, emit):
        self.store = store
        self.emit = emit
        self.submitted: list[tuple[str, list[str]]] = []
        self.stopped: list[str] = []
        self.chosen: list[tuple[str, str]] = []
        self.kept_open: list[float] = []
        self.fare_requests: list[tuple] = []
        self.shut = None
        self.busy = False
        self.started = False

    def start(self) -> None:
        self.started = True

    def submit(self, order: forms.Order):
        grp = f"G{len(self.submitted) + 1}"
        travellers = order.records()
        ids = [self.store.add_booking(grp=grp, origin=order.origin, dest=order.dest, travel_date=order.day.isoformat(),
                                      offer=o, travellers=travellers, phone=order.phone, email=order.email)
               for o in order.bookings]
        self.submitted.append((grp, ids))
        self.busy = True
        return grp, ids

    def stop(self, bid: str) -> None:
        self.stopped.append(bid)

    def choose_flight(self, bid: str, key: str) -> None:
        self.chosen.append((bid, key))

    def keep_open(self, seconds: float = 90.0) -> None:
        self.kept_open.append(seconds)

    def request_fares(self, origin: str, dest: str, start: date, days: int = 60) -> None:
        self.fare_requests.append((origin, dest, start, days))

    def shutdown(self, close_browser: bool = True) -> None:
        self.shut = close_browser

    # helpers for tests / previews: play the engine's side ------------------------------------------
    def play_success(self, bid: str, grp: str, pnr: str, fare: float = 5132.0, discount: float = 1902.0,
                     hold_minutes: float = 28, verified: str = "ok", verify_note: str = "") -> None:
        self.emit({"type": "started", "id": bid, "grp": grp, "offer": self.store.get(bid)["offer"]})
        for st in ("browser", "warmup", "search", "hold", "travellers", "pnr"):
            self.emit({"type": "stage", "id": bid, "stage": st})
        from datetime import datetime, timedelta, timezone
        until = (datetime.now(timezone.utc) + timedelta(minutes=hold_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.store.update(bid, status="done", pnr=pnr, kind="record_locator", fare=fare, discount=discount,
                          coupon_applied=True, hold_until=until, finished_at=time.time(), verified=verified, verify_note=verify_note)
        self.emit({"type": "finished", "id": bid, "grp": grp, "record": self.store.get(bid)})

    def play_failure(self, bid: str, grp: str, error: str = "IndiGo has no seats on this route and date. Please try another date.") -> None:
        self.emit({"type": "started", "id": bid, "grp": grp, "offer": self.store.get(bid)["offer"]})
        self.emit({"type": "stage", "id": bid, "stage": "search"})
        self.store.update(bid, status="failed", error=error, finished_at=time.time())
        self.emit({"type": "finished", "id": bid, "grp": grp, "record": self.store.get(bid)})

    def play_fares(self, origin: str, dest: str, start: date, days: int, fares: dict, empty: bool = False, error=None) -> None:
        self.emit({"type": "fares", "origin": origin, "dest": dest, "start": start.isoformat(), "days": days, "fares": fares,
                   "empty": empty, "error": error})
