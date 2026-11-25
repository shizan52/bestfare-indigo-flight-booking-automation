"""LIVE test of the real app on THIS computer: opens the Best Fare window with the real engine, fills the form like a person
(types the cities and picks them from the suggestions, opens the fare calendar and takes the cheapest day), presses Book now and
waits for IndiGo's real PNR. Creates a real 30-minute On-Hold booking (nothing is paid). Also proves, while it runs:
  * the booking browser is NOT started when the app opens, only when the calendar / the booking needs it;
  * its window sits off-screen (Browser.getWindowForTarget is sampled every 2 s) and any pop-up is moved away at once;
  * IndiGo's own record of the booking matches what was typed (the 'Checked with IndiGo' line);
  * the browser closes again after the booking.
Screenshots of the window go to OUT_DIR.

    .venv\\Scripts\\python.exe tools\\live_app_test.py OUT_DIR [--offer offer1|offer2] [--origin delhi --dest mumbai] [--debug]
          [--name "RAHUL TESTER" --gender M --phone 9000000001 --email test.booking@example.com] [--fixed-days 10]

Do not run it in bursts: IndiGo slows down a computer that makes many holds in a short time.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import paths  # noqa: E402

sys.path.insert(0, str(HERE / "tools"))
import devcodes  # noqa: E402

devcodes.load()                                   # (the development gate: the offer codes come from the bot's config)
paths.use_engine()
from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import app as appmod  # noqa: E402
import flights  # noqa: E402
import forms  # noqa: E402
import theme  # noqa: E402
from cdp import CDPBrowser  # noqa: E402
from main_window import MainWindow  # noqa: E402
from store import Store  # noqa: E402

LOCK = threading.Lock()


def edge_count() -> int:
    import subprocess
    ps = ("(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | Where-Object { $_.CommandLine -and "
          "$_.CommandLine.Contains('BestFare\\edge-profile') } | Measure-Object).Count")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else -1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--offer", default="offer2", choices=("offer1", "offer2"))
    ap.add_argument("--origin", default="delhi")
    ap.add_argument("--dest", default="mumbai")
    ap.add_argument("--fixed-days", type=int, default=0, help="use this many days ahead instead of the calendar's cheapest day")
    ap.add_argument("--name", default="RAHUL TESTER")
    ap.add_argument("--gender", default="M", choices=("M", "F"))
    ap.add_argument("--phone", default="9000000001")
    ap.add_argument("--email", default="test.booking@example.com")
    ap.add_argument("--adults", type=int, default=1, help="how many adults (the first is --name)")
    ap.add_argument("--children", type=int, default=0, help="add this many children (2 to 11 years, with a date of birth)")
    ap.add_argument("--infants", type=int, default=0, help="add this many infants (under 2 years, with a name and a date of birth)")
    ap.add_argument("--second-tab", action="store_true", help="while the first booking runs, fill a SECOND tab (other route and offer) and book it too: it must wait its turn and then be booked")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--limit", type=int, default=12 * 60)
    ap.add_argument("--pick", default="other",
                    help="which flight to take from 'Choose your flight': lowest | other (the cheapest one that is NOT the lowest, "
                         "with the offer when possible) | a flight number like 864")
    ap.add_argument("--fill-delay", type=float, default=0.0,
                    help="seconds a real traveller takes to fill the form before pressing Book now (key presses are sent meanwhile): "
                         "the browser is kept warm during this time, which is what makes Book now fast")
    ap.add_argument("--stop-after", type=float, default=0.0,
                    help="press Stop this booking this many seconds after Book now (a robustness check: nothing may hang or be left behind)")
    ap.add_argument("--stop-at-choice", action="store_true",
                    help="open the flight list, take the screenshot and cancel: only a search is made, nothing is held or booked")
    ap.add_argument("--dump-record", action="store_true",
                    help="print the SHAPE of IndiGo's record of the booking (letters -> a, digits -> 9) to see how it stores contact details")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    debug = appmod.debug_wanted(a.debug)

    appmod.setup_logging(debug)
    if a.dump_record:
        import json as _json
        import indigo_client as _ic
        _real_options = _ic.flight_options

        def _spy_options(search_json, *args, **kw):                                  # the fare breakdown IndiGo gave for ONE journey (prices only)
            try:
                jr = next(j for t in (search_json.get("data") or {}).get("trips") or [] for j in t.get("journeysAvailable") or [] if not j.get("isSold"))
                pf = next(p for p in jr.get("passengerFares") or [] if p.get("fareAvailabilityKey"))
                keep = {k: v for k, v in pf.items() if k not in ("fareAvailabilityKey", "journeyKey")}
                pax = [{k: v for k, v in px.items() if not isinstance(v, (list, dict))} | {"charges": [(c.get("type"), c.get("code"), c.get("amount")) for c in px.get("serviceCharges") or []]}
                       for px in (pf.get("paxFares") or [])]
                keep["paxFares"] = pax
                print("SEARCH FARE SHAPE:", _json.dumps({k: v for k, v in keep.items() if not isinstance(v, (list, dict)) or k == "paxFares"}, ensure_ascii=False)[:2400], flush=True)
            except Exception as e:  # noqa: BLE001
                print("SEARCH FARE SHAPE: could not read", e, flush=True)
            return _real_options(search_json, *args, **kw)

        _ic.flight_options = _spy_options
        import re

        import verify as verify_mod

        def masked(o, depth=0):
            if isinstance(o, dict):
                return {k: masked(v, depth + 1) for k, v in o.items() if depth < 4}
            if isinstance(o, list):
                return [masked(x, depth + 1) for x in o[:3]]
            if isinstance(o, str):
                return re.sub(r"[A-Za-z]", "a", re.sub(r"\d", "9", o))[:48]
            return o

        real_check = verify_mod.check

        def spying_check(req, itinerary, promo="", flight=None):
            import json
            d = (itinerary or {}).get("data") or {}
            keep = {k: d.get(k) for k in ("passengers", "contacts", "bookingDetails", "priceBreakdown")}
            print("RECORD SHAPE:", json.dumps(masked(keep), ensure_ascii=False)[:3500], flush=True)
            checks = real_check(req, itinerary, promo, flight)
            print("RECORD CHECKS:", [(c.key, c.ok, c.text) for c in checks], flush=True)
            return checks

        verify_mod.check = spying_check
    if not debug:                                                  # (the test itself may print what the user cannot see)
        import logging
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("   [engine log] %(name)s: %(message)s"))
        sh.addFilter(lambda r: r.name in ("engine", "extension", "browser") or (r.name == "indigo_client" and ("infant" in r.getMessage() or "addpax" in r.getMessage())))
        logging.getLogger().addHandler(sh)
    app = QApplication(sys.argv)
    theme.load_fonts()
    app.setFont(theme.base_font())
    app.setWindowIcon(theme.app_icon())
    app.setStyleSheet(theme.qss())
    store = Store(paths.db_path())
    w = MainWindow(store, debug=debug)
    w.show()
    t0 = time.monotonic()
    st = {"phase": "start", "shots": 0, "last_shot": 0.0, "result": 1, "windows_seen": [], "calendar_waited": 0.0}

    def say(text: str) -> None:
        with LOCK:
            print(f"[{time.monotonic() - t0:6.1f}s] {text}", flush=True)

    def on_event(ev: dict) -> None:
        t = ev.get("type")
        if t == "started":
            st["started"] = True
        if t in ("stage", "note", "started"):
            say(f"{t}: {ev.get('stage') or ev.get('text') or ev.get('offer')}")
        elif t == "fares":
            say(f"FARES: {len(ev.get('fares') or {})} days, empty={ev.get('empty')}, error={ev.get('error')}")
        elif t == "finished":
            r = ev.get("record") or {}
            say(f"FINISHED: status={r.get('status')} pnr={r.get('pnr')} flight={r.get('flight')!r} kind={r.get('kind')} fare={r.get('fare')} "
                f"discount={r.get('discount')} "
                f"coupon_applied={r.get('coupon_applied')} verified={r.get('verified')} hold_until={r.get('hold_until')} error={r.get('error')}")
            if r.get("verify_note"):
                say(f"  verification: {r['verify_note']}")
        elif t == "note" and "longer than usual" in ev.get("text", ""):
            say(f"NOTE (the app is trying again by itself): {ev['text']}")

    w.bridge.fired.connect(on_event)
    w.start()
    say(f"the app is open; booking browser started so far: {w.engine.edge.launches} time(s); Edge processes: {edge_count()}")

    # ── where does the browser window sit? sampled in a thread of its own ───────────────────────────────
    stop_watch = threading.Event()

    def watcher() -> None:
        async def run() -> None:
            while not stop_watch.is_set():
                ep = w.engine.edge.endpoint
                if ep:
                    try:
                        async with CDPBrowser(ep) as b:
                            seen = []
                            for p in await b.pages():
                                tab = await b.open_tab(p)
                                try:
                                    bounds = (await tab.send("Browser.getWindowForTarget", timeout=5)).get("bounds", {})
                                    seen.append((p.get("url", "")[:38], bounds.get("left"), bounds.get("top")))
                                finally:
                                    await tab.close()
                            st["windows_seen"].append(seen)
                            say("windows: " + "; ".join(f"{u} @({l},{t})" for u, l, t in seen))
                    except Exception:  # noqa: BLE001
                        pass
                await asyncio.sleep(2.0)
        asyncio.run(run())

    threading.Thread(target=watcher, daemon=True).start()

    def shot(name: str, widget=None) -> None:
        st["shots"] += 1
        path = out / f"{st['shots']:02d}_{name}.png"
        (widget or w).grab().save(str(path))
        say(f"screenshot {path.name}")

    b = w.book

    def type_airport(box, text: str) -> None:
        box._edited(text)                                                             # noqa: SLF001 - exactly what typing does
        box._picked(box._model.index(0, 0), advance=False)                            # noqa: SLF001 - picking the first suggestion
        say(f"typed {text!r} -> {box.text()}")

    def step() -> None:
        el = time.monotonic() - t0
        ph = st["phase"]
        if ph == "start" and el > 4:
            say(f"4 s after opening: booking browser started {w.engine.edge.launches} time(s), Edge processes {edge_count()}  <- must be 0 and 0")
            type_airport(b.origin, a.origin)
            type_airport(b.dest, a.dest)
            if a.fixed_days:
                b.date_field.set_date(forms.today_ist() + timedelta(days=a.fixed_days))
                st["phase"] = "fill"
            else:
                b.date_field.open_calendar()
                say("calendar opened - waiting for IndiGo's fares")
                st["phase"] = "calendar"
        elif ph == "calendar":
            cal = b.date_field.calendar
            if cal.state in ("ok", "empty", "error", "busy") or el > 180:
                shot("calendar", cal)
                say(f"calendar state={cal.state}: {len(cal.fares)} days with fares; cheapest={cal.cheapest_day()}; "
                    f"browser launched {w.engine.edge.launches} time(s); Edge processes {edge_count()}")
                if cal.cheapest_day():
                    cal._pick_cheapest()                                              # noqa: SLF001 - what a click on the button does
                    say(f"picked the cheapest day: {b.date_field.date()}")
                else:
                    cal.close()
                    b.date_field.set_date(forms.today_ist() + timedelta(days=10))
                st["phase"] = "fill"
        elif ph == "fill":
            first, last = a.name.rsplit(" ", 1)
            b.rows[0].first.setText(first.upper())
            b.rows[0].last.setText(last.upper())
            (b.rows[0].male if a.gender == "M" else b.rows[0].female).setChecked(True)
            day = b.date_field.date()
            for i in range(1, a.adults):
                b.add_btn.click()
                r = b.rows[-1]
                r.first.setText(f"ADULT{'ABCDEF'[i]}")
                r.last.setText(last.upper())
                r.female.setChecked(True)
            for i in range(a.children):                                               # the form's own buttons, like a person
                b.add_child.click()
                r = b.rows[-1]
                r.first.setText(f"CHILD{'ABCD'[i]}")
                r.last.setText(last.upper())
                r.female.setChecked(True)
                r.dob.textEdited.emit(day.replace(year=day.year - 6, day=min(day.day, 28)).strftime("%d%m%Y"))
            for i in range(a.infants):
                b.add_infant.click()
                r = b.rows[-1]
                r.first.setText(f"BABY{'ABCD'[i]}")
                r.last.setText(last.upper())
                r.male.setChecked(True)
                mo = day.month - 8
                r.dob.textEdited.emit(day.replace(year=day.year - (1 if mo < 1 else 0), month=mo + (12 if mo < 1 else 0), day=min(day.day, 28)).strftime("%d%m%Y"))
            say(f"travellers on the form: {[(r.kind, r.first.text(), r.dob.text()) for r in b.rows]}")
            b.phone.setText(a.phone)
            b.email.setText(a.email)
            b.tiles[a.offer].setChecked(True)
            shot("form_filled")
            st["typing_until"] = el + a.fill_delay
            st["next_key"] = el
            st["phase"] = "typing"
        elif ph == "typing":
            if el >= st["next_key"] and el < st["typing_until"]:                      # somebody is typing: the window tells the engine
                from PyQt6.QtCore import QEvent, Qt
                from PyQt6.QtGui import QKeyEvent
                # (sent to a panel that ignores keys: the window only needs to SEE that somebody is typing - it must not change the form)
                QApplication.sendEvent(b.summary, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Shift, Qt.KeyboardModifier.NoModifier, ""))
                st["next_key"] = el + 8
            if el >= st["typing_until"]:
                say(f"the form has been filled for {a.fill_delay:.0f} s; page warm-up credit: {w.engine.warm_credit():.0f} s")
                b.summary.book.click()
                say(f"pressed Book now ({a.offer}, {b.route()[0]}->{b.route()[1]}, {b.date_field.date()})")
                st["phase"] = "booking"
                st["booked_at"] = el
        elif ph == "booking":
            if a.stop_after and not st.get("stopped") and el - st["booked_at"] > a.stop_after:
                st["stopped"] = True
                say(f"STOP pressed {a.stop_after:.0f} s after Book now (stage so far: {w.book.progress.steps.active if hasattr(w.book.progress.steps, 'active') else '?'})")
                for i in w.group_ids:
                    w.engine.stop(i)
            if a.second_tab and st.get("started") and not st.get("tab2"):
                st["tab2"] = True
                p2 = w.tabs.add()
                st["p2"] = p2
                type_airport(p2.origin, "bengaluru")
                type_airport(p2.dest, "delhi")
                p2.date_field.set_date(b.date_field.date())
                p2.rows[0].first.setText("PRIYA")
                p2.rows[0].last.setText("TESTER")
                p2.rows[0].female.setChecked(True)
                p2.phone.setText(a.phone)
                p2.email.setText(a.email)
                p2.tiles["offer2" if a.offer == "offer1" else "offer1"].setChecked(True)
                p2.summary.book.click()
                say(f"SECOND TAB booked while the first runs: tab states now {[(p.tab_title()[0], p.state) for p in w.tabs.pages()]}")
            if not st.get("started") and el - st["booked_at"] > 15 and b.stack.currentWidget() is b.summary:
                say("THE BOOKING NEVER STARTED - the form refused it:")
                for k, lab in b.errors.items() if hasattr(b, "errors") else []:
                    say(f"   {k}: {lab.text()}")
                finish(5)
                return
            if b.stack.currentWidget() is b.choice:
                ch = b.choice
                opts = flights.arranged(ch.options, "price")
                say(f"CHOOSE YOUR FLIGHT: {len(opts)} flights; lowest = {flights.line(opts[0])} · {flights.money(opts[0])}")
                for o in opts:
                    say(f"   {flights.timeline(o):17s} {flights.duration(o.get('minutes')):7s} {flights.stops_text(o):22s} "
                        f"{flights.flight_number(o):18s} {flights.money(o):>9s}  "
                        f"{'offer: save ' + forms.money(flights.saving(o)) if flights.saving(o) else 'regular fare'}")
                QTimer.singleShot(400, lambda: shot("choose_your_flight"))
                if a.stop_at_choice:
                    st["phase"] = "cancelling"
                    QTimer.singleShot(1500, lambda: (say("cancelling at the flight list (nothing was held)"), ch.cancel.click()))
                    return
                low = opts[0]
                if a.pick == "lowest":
                    want = low
                elif a.pick == "other":
                    others = [o for o in opts if o["key"] != low["key"]]
                    want = next((o for o in others if flights.saving(o) > 0), others[0] if others else low)
                else:
                    want = next((o for o in opts if a.pick in flights.flight_number(o)), low)
                st["want"] = want
                card = ch.cards[want["key"]]
                card.clicked.emit(card.key)
                say(f"the traveller taps: {flights.line(want)} · {flights.money(want)}"
                    + ("  (NOT the lowest fare)" if want["key"] != low["key"] else "  (the lowest fare)"))
                QTimer.singleShot(1800, lambda: (shot("flight_selected"), ch.go.click(), say("pressed Continue")))
                st["phase"] = "waiting_for_progress"
            elif b.stack.currentWidget() is b.result:
                shot("result")
                rec = store.get(w.group_ids[0])
                st["result"] = 0 if rec["status"] == "done" else 1
                want = st.get("want")
                if want is not None:
                    same = rec.get("flight") == flights.line(want) and (rec.get("fare") == want["amount"] or (a.infants and (rec.get("fare") or 0) > want["amount"]))
                    say(f"THE BOOKED FLIGHT IS THE CHOSEN ONE: {same}  (chosen {flights.line(want)} · {flights.money(want)}; "
                        f"booked {rec.get('flight')} · {forms.money(rec.get('fare') or 0)})")
                    if not same:
                        st["result"] = 4
                elif a.stop_after:
                    say(f"stopped booking ended as intended: status={rec['status']} error={rec.get('error')!r} pnr={rec.get('pnr')}")
                    st["result"] = 0 if rec["status"] == "failed" and not rec.get("pnr") else 1
                elif a.stop_at_choice:
                    say(f"booking ended without a PNR, as intended: status={rec['status']} error={rec.get('error')!r}")
                    st["result"] = 0 if rec["status"] == "failed" and not rec.get("pnr") else 1
                if st.get("p2") is not None and st["p2"].state not in ("done", "failed"):
                    say(f"first tab finished; the second tab is {st['p2'].state!r} - waiting for it")
                    st["phase"] = "second"
                    st["first_result"] = st["result"]
                    return
                w.sidebar.nav[1].click()
                st["phase"] = "after"
                st["done_at"] = el
                QTimer.singleShot(1200, lambda: (shot("my_bookings"), w.sidebar.nav[2].click(),
                                                 QTimer.singleShot(800, lambda: shot("activity"))))
            elif el - st["last_shot"] > 45:
                st["last_shot"] = el
                shot("progress")
        elif ph == "second":
            p2 = st["p2"]
            if p2.stack.currentWidget() is p2.choice and not st.get("p2_picked"):
                st["p2_picked"] = True
                opts = flights.arranged(p2.choice.options, "price")
                say(f"SECOND TAB: choose your flight - {len(opts)} flights; taking {flights.line(opts[0])} · {flights.money(opts[0])}")
                card = p2.choice.cards[opts[0]["key"]]
                card.clicked.emit(card.key)
                QTimer.singleShot(1200, lambda: p2.choice.go.click())
            if el - st.get("last_p2", 0) > 20:
                st["last_p2"] = el
                say(f"second tab state: {p2.state}")
            if p2.state in ("done", "failed"):
                rec2 = store.get(p2.group_ids[0])
                say(f"SECOND TAB FINISHED: status={rec2['status']} pnr={rec2.get('pnr')} flight={rec2.get('flight')!r} verified={rec2.get('verified')} error={rec2.get('error')}")
                st["result"] = 0 if st.get("first_result", 1) == 0 and rec2["status"] == "done" else 1
                say(f"tabs at the end: {[(p.tab_title()[0], p.state) for p in w.tabs.pages()]}")
                shot("tabs_end")
                st["phase"] = "after"
                st["done_at"] = el
        elif ph in ("waiting_for_progress", "cancelling"):
            if b.stack.currentWidget() is not b.choice:                               # the pick went ahead (or the booking was stopped)
                st["phase"] = "booking"
        elif ph == "after":
            if el - st["done_at"] > 14:                                               # the browser should have closed itself by now
                say(f"14 s after the booking: Edge processes {edge_count()}  <- must be 0")
                finish(st["result"])
        if el > a.limit:
            shot("gave_up")
            finish(3)

    def finish(code: int) -> None:
        say(f"closing the app (exit {code})")
        timer.stop()
        stop_watch.set()
        w.close()
        app.exit(code)

    timer = QTimer()
    timer.timeout.connect(step)
    timer.start(500)
    code = app.exec()
    store.close()
    off = [x for seen in st["windows_seen"] for x in seen if x[1] is not None]
    on_screen = [x for x in off if x[1] > -20000]
    say(f"window samples: {len(off)}; any window on a monitor: {on_screen if on_screen else 'none - it stayed out of sight'}")
    say(f"Edge processes after closing the app: {edge_count()}")
    return code


if __name__ == "__main__":
    sys.exit(main())
