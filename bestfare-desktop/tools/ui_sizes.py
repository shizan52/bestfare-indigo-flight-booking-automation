r"""Pictures of the real window (real fonts and style sheet, practice engine, not shown on the screen) at any logical size and scale - to see that it fits every screen.
    .venv\Scripts\python.exe tools\ui_sizes.py OUT_DIR 1920x1000 1366x700 911x480 820x500 [1093x614@1.25 ...]
Per size: form, filled form (adult + child + infant, contact), calendar, progress, flight choice, result, history, activity, lock screen.
It also prints what is cut off (any widget of the form that ends beyond the form's visible width) and the window's minimum size.
Look at the pictures too: a stuck validation or a hidden panel can make a picture show the wrong thing."""
import os
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

D = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(D)]
os.environ["BESTFARE_HOME"] = tempfile.mkdtemp(prefix="bf-shots-")
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
specs = []
for a in sys.argv[2:]:
    size, _, sc = a.partition("@")
    w, h = (int(x) for x in size.split("x"))
    specs.append((w, h, float(sc or 1)))
os.environ["QT_SCALE_FACTOR"] = str(specs[0][2]) if len({s[2] for s in specs}) == 1 else "1"
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402
QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
app = QApplication([])
import demo  # noqa: E402
import forms  # noqa: E402
import paths  # noqa: E402
import theme  # noqa: E402
from main_window import MainWindow  # noqa: E402
from store import Store  # noqa: E402
theme.load_fonts()
app.setFont(theme.base_font())
app.setStyleSheet(theme.qss())
demo.STEP_SECONDS = {k: 0.02 for k in demo.STEP_SECONDS}
demo.FARE_SECONDS = 0.05
store = Store(paths.db_path(demo=True))
w = MainWindow(store, lambda emit: demo.DemoEngine(store, emit), demo=True)
w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
w.show()
w.start()


def pump(n=15, pause=0.0):
    for _ in range(n):
        app.processEvents()
        if pause:
            time.sleep(pause)


def until(cond, seconds=8):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def overflow(tag):
    """Widgets whose right edge lies beyond their scroll area's / page's visible width (what would be cut off)."""
    bad = []
    b = w.book
    for scroll, name in ((b.form_scroll, "form"),):
        vp = scroll.viewport().width()
        for c in scroll.widget().findChildren(QWidget):
            if c.isVisible() and c.mapTo(scroll.widget(), c.rect().topRight()).x() > vp + 1 and c.width() < 4000:
                bad.append(f"{name}:{type(c).__name__}:{c.objectName()}:{c.mapTo(scroll.widget(), c.rect().topRight()).x()}>{vp}")
                if len(bad) > 6:
                    break
    return bad


def fill():
    b = w.book
    b.origin.set_code("DEL")
    b.dest.set_code("BOM")
    b.date_field.set_date(forms.today_ist() + timedelta(days=5))
    b.rows[0].first.setText("RAHUL")
    b.rows[0].last.setText("SHARMA")
    b.rows[0].male.setChecked(True)
    if len(b.rows) < 2:
        b.add_row(kind=forms.CHILD)
    if len(b.rows) < 3:
        b.add_row(kind=forms.INFANT)
    b.rows[1].set_value(forms.Traveller(first="ANAYA", last="SHARMA", gender="F", kind=forms.CHILD, dob="10/05/2020"))
    b.rows[2].set_value(forms.Traveller(first="VIHAAN", last="SHARMA", gender="M", kind=forms.INFANT, dob="01/09/2025"))
    b.phone.setText("9876543210")
    b.email.setText("rahul.sharma@gmail.com")
    b.tiles["offer1"].setChecked(True)


print("window minimumSizeHint:", w.minimumSizeHint().width(), "x", w.minimumSizeHint().height(), "| minimumSize:", w.minimumSize().width(), "x", w.minimumSize().height())
for (W, H, sc) in specs:
    tag = f"{W}x{H}" + (f"@{sc}" if sc != 1 else "")
    w.resize(W, H)
    pump()
    w.book.form_scroll.verticalScrollBar().setValue(0)
    pump()
    print(f"== {tag}: window {w.width()}x{w.height()}  form column {w.book.form_scroll.viewport().width()} | right column {w.book.stack.width()} | sidebar {w.sidebar.width()}")
    w.grab().save(str(out / f"{tag}_1_form.png"))
    fill()
    pump()
    w.grab().save(str(out / f"{tag}_2_filled.png"))
    print("   overflow:", overflow(tag) or "none")
    cal = w.book.date_field.calendar
    w.book.date_field.open_calendar()
    pump()
    cal.grab().save(str(out / f"{tag}_3_calendar.png"))
    print(f"   calendar popup {cal.width()}x{cal.height()} on a {W}x{H} window")
    cal.close()
    w.engine.auto_choose = False
    w.book.summary.book.click()
    until(lambda: w.book.stack.currentWidget() is w.book.progress, 4)
    pump(5, 0.05)
    w.grab().save(str(out / f"{tag}_4_progress.png"))
    until(lambda: w.book.stack.currentWidget() is w.book.choice, 8)
    pump(10)
    w.grab().save(str(out / f"{tag}_5_choice.png"))
    w.book.choice.go.click()
    until(lambda: w.book.stack.currentWidget() is w.book.result, 10)
    pump(10)
    w.grab().save(str(out / f"{tag}_6_result.png"))
    w._show_page(1)
    pump()
    w.grab().save(str(out / f"{tag}_7_history.png"))
    w._show_page(2)
    pump()
    w.grab().save(str(out / f"{tag}_8_activity.png"))
    w._show_page(0)
    w.book.back_to_form()
    pump()
    w.veil.show_for("stopped", "Your access has been stopped. Please contact your admin.")
    pump()
    w.grab().save(str(out / f"{tag}_9_lock.png"))
    w.veil.hide()
    pump()
sys.stdout.flush()
os._exit(0)
