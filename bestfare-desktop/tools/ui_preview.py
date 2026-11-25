"""Render the main screens to PNG files (offscreen, fake engine - no browser, no IndiGo) to review the design.

    .venv\\Scripts\\python.exe tools\\ui_preview.py OUT_DIR
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
os.environ["BESTFARE_HOME"] = tempfile.mkdtemp(prefix="bf_preview_")

from PyQt6.QtWidgets import QApplication  # noqa: E402

import demo  # noqa: E402
import forms  # noqa: E402
import paths  # noqa: E402
import theme  # noqa: E402
from fakes import FakeEngine  # noqa: E402
from main_window import MainWindow  # noqa: E402
from store import Store  # noqa: E402


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)
    theme.load_fonts()
    app.setFont(theme.base_font())
    app.setStyleSheet(theme.qss())
    store = Store(paths.db_path())
    box = {}

    def factory(emit):
        box["e"] = FakeEngine(store, emit)
        return box["e"]

    w = MainWindow(store, factory)
    w.resize(1280, 860)
    w.show()
    w.start()
    fake: FakeEngine = box["e"]
    t = forms.today_ist()

    def shot(name: str, widget=None) -> None:
        app.processEvents()
        (widget or w).grab().save(str(out / f"{name}.png"))
        print("saved", name)

    shot("1_empty")
    w.book.summary.book.click()
    shot("2_errors")

    b = w.book
    # the From box with its suggestions
    b.origin._edited("d")                                                           # noqa: SLF001
    pop = b.origin._completer.popup()                                               # noqa: SLF001
    b.origin._completer.complete()
    app.processEvents()
    pop.setCurrentIndex(b.origin._model.index(0, 0))                                # noqa: SLF001
    app.processEvents()
    shot("3_from_suggestions", pop)
    pop.hide()
    b.origin.set_code("DEL")
    b.dest._edited("mum")                                                           # noqa: SLF001
    b.dest._completer.complete()                                                    # noqa: SLF001
    app.processEvents()
    shot("3b_to_suggestions", b.dest._completer.popup())                            # noqa: SLF001
    b.dest._completer.popup().hide()                                                # noqa: SLF001
    b.dest.set_code("BOM")

    # the fare calendar
    b.date_field.open_calendar()
    cal = b.date_field.calendar
    shot("4a_calendar_loading", cal)
    start = fake.fare_requests[0][2] if fake.fare_requests else t
    fake.play_fares("DEL", "BOM", start, 60, demo.make_fares("DEL", "BOM", start, 60))
    app.processEvents()
    shot("4b_calendar_fares", cal)
    cal.close()
    b.date_field.set_date(t + timedelta(days=9))

    b.rows[0].first.setText("RAHUL")
    b.rows[0].last.setText("SHARMA")
    b.rows[0].male.setChecked(True)
    b.add_row()
    b.rows[1].first.setText("PRIYA")
    b.rows[1].last.setText("SHARMA")
    b.rows[1].female.setChecked(True)
    b.phone.setText("9876543210")
    b.email.setText("rahul.sharma@gmail.com")
    b.tiles["offer2"].setChecked(True)
    b.show_errors({})
    shot("5_filled")
    sb = b.form_scroll.verticalScrollBar()
    sb.setValue(sb.maximum())
    shot("5b_filled_bottom")
    sb.setValue(0)

    b.summary.book.click()
    grp, ids = fake.submitted[-1]
    fake.emit({"type": "started", "id": ids[0], "grp": grp, "offer": "offer2"})
    # "Choose your flight": IndiGo's real answer (the bot's test fixture), for two travellers
    import json

    import flights
    import indigo_client as ic
    real = json.loads((HERE.parent / "indigo-bot" / "tests" / "data" / "search_answer_real.json").read_text(encoding="utf-8"))
    opts = [flights.public(o) for o in ic.flight_options(real, "DEL", "BOM")]
    for o in opts:
        o["amount"] *= 2
        o["original"] = o["original"] * 2 if o["original"] else None
    fake.emit({"type": "stage", "id": ids[0], "stage": "search"})
    fake.emit({"type": "choose", "id": ids[0], "grp": grp, "flights": opts, "seconds": 300})
    shot("5c_choose_your_flight")
    fake.emit({"type": "chosen", "id": ids[0], "flight": opts[0]})
    fake.emit({"type": "stage", "id": ids[0], "stage": "hold"})
    fake.emit({"type": "note", "id": ids[0], "text": "Your flight: 6E 864 · 15:30 → 18:00 · ₹10,876"})
    shot("6_progress")
    fake.play_success(ids[0], grp, "IYQMKM", fare=6014.0, discount=544.0, verified="ok")
    shot("7_result")

    b.result.back_to_form.emit()
    b.summary.book.click()
    grp, ids = fake.submitted[-1]
    fake.play_success(ids[0], grp, "ABCDEF", verified="mismatch",
                      verify_note="Please check this before you pay: IndiGo's booking has a different travel date (29 Nov 2026 instead of 28 Nov 2026).")
    shot("7b_result_mismatch")
    b.result.back_to_form.emit()
    b.summary.book.click()
    grp, ids = fake.submitted[-1]
    fake.play_failure(ids[0], grp, "IndiGo is not responding well right now, so this booking could not be completed. Nothing was booked or charged — please press Book again.")
    shot("7c_failed")

    # history with every kind of timing
    def add(status, pnr, hold_min=None, kind="record_locator", ago=2):
        bid = store.add_booking(grp="H", origin="DEL", dest="BOM", travel_date=(t + timedelta(days=9)).isoformat(), offer="offer1",
                                travellers=[{"first": "A", "last": "B", "gender": "M"}], phone="9", email="a@b.co")
        f = {"status": status, "finished_at": time.time() - ago * 60}
        if status == "done":
            hold = (datetime.now(timezone.utc) + timedelta(minutes=hold_min)).strftime("%Y-%m-%dT%H:%M:%SZ") if hold_min is not None else None
            f.update(pnr=pnr, kind=kind, fare=5132.0, hold_until=hold)
        if status == "failed":
            f["error"] = "IndiGo has no seats on this route and date."
        store.update(bid, **f)
    add("done", "GCVUQG", hold_min=26)
    add("done", "OIGB3E", hold_min=3.2)
    add("done", "E8319S", hold_min=-9, ago=39)
    add("failed", "")
    w.sidebar.nav[1].click()
    shot("8_history")

    fake.emit({"type": "note", "id": ids[0], "text": "Offer applied — you save ₹544"})
    w.sidebar.nav[2].click()
    shot("9_activity")
    w.sidebar.nav[0].click()
    b.new_booking()
    fake.busy = False
    w.close()


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "ui_preview"))
