"""The window fits every screen: it opens at a size that fits the screen it is on, can be made small, and gives way gracefully - nothing is cut off, however
small or scaled-up the screen is (offscreen, fake engine). Also: the soft shadows cost nothing while the window is used."""
from datetime import timedelta

import pytest
from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtWidgets import QApplication, QWidget

import forms
import shadows
from fakes import FakeEngine
from main_window import DEFAULT_SIZE, MIN_SIZE, MainWindow, fit_window
from widgets import FitButton, FlowLabel, WrapRow, add_months

SIZES = [(1920, 1000), (1320, 860), (1093, 600), (911, 500), (820, 500)]


@pytest.fixture()
def win(qapp, store):
    box = {}

    def factory(emit):
        box["e"] = FakeEngine(store, emit)
        return box["e"]

    w = MainWindow(store, factory)
    w.show()
    w.start()
    w.fake = box["e"]
    yield w
    w.fake.busy = False
    w.close()


def pump(n=12):
    for _ in range(n):
        QApplication.processEvents()


def fill(w):
    b = w.book
    b.origin.set_code("DEL")
    b.dest.set_code("BOM")
    b.date_field.set_date(forms.today_ist() + timedelta(days=5))
    while len(b.rows) < 3:
        b.add_row(kind=forms.CHILD if len(b.rows) == 1 else forms.INFANT)
    b.rows[0].set_value(forms.Traveller(first="RAHUL", last="SHARMA", gender="M", kind=forms.ADULT))
    b.rows[1].set_value(forms.Traveller(first="ANAYA", last="SHARMA", gender="F", kind=forms.CHILD, dob="10/05/2020"))
    b.rows[2].set_value(forms.Traveller(first="VIHAAN", last="SHARMA", gender="M", kind=forms.INFANT, dob="01/09/2025"))
    b.phone.setText("9876543210")
    b.email.setText("rahul.sharma@gmail.com")
    b.tiles["offer1"].setChecked(True)


def cut_off(win):
    """The widgets of the booking form whose right edge lies beyond the form's visible width (what the traveller could not see)."""
    b = win.book
    vp = b.form_scroll.viewport().width()
    body = b.form_scroll.widget()
    bad = []
    for c in body.findChildren(QWidget):
        if c.isVisible() and not isinstance(c, shadows.ShadowLayer) and c.mapTo(body, c.rect().topRight()).x() > vp + 1:
            bad.append(f"{type(c).__name__}#{c.objectName()} ends at {c.mapTo(body, c.rect().topRight()).x()} of {vp}")
    return bad


# ── where and how big the window opens ──────────────────────────────────────────────────────────────────────────────────────
BIG = (0, 0, 1920, 1040)                                  # a 1080p screen without its taskbar


def test_on_a_big_screen_the_window_opens_as_designed_and_centred():
    x, y, w, h, maxed = fit_window(None, [BIG])
    assert (w, h) == DEFAULT_SIZE and not maxed
    assert x == (1920 - w) // 2 and y > 0 and y + h < 1040


@pytest.mark.parametrize("screen", [(0, 0, 1366, 728), (0, 0, 1093, 574), (0, 0, 960, 500), (0, 0, 800, 560)])
def test_on_a_small_screen_the_window_never_opens_bigger_than_the_screen(screen):
    x, y, w, h, _ = fit_window(None, [screen])
    assert x >= screen[0] and y >= screen[1] + 30                                    # (the title bar stays on the screen)
    assert x + w <= screen[0] + screen[2] and y + h <= screen[1] + screen[3]
    assert w >= min(MIN_SIZE[0], screen[2] - 16) and h >= min(MIN_SIZE[1], screen[3] - 40)


def test_the_saved_place_comes_back_when_it_is_still_on_a_screen():
    saved = {"x": 120, "y": 90, "w": 1000, "h": 640, "max": False}
    assert fit_window(saved, [BIG])[:4] == (120, 90, 1000, 640)
    assert fit_window(dict(saved, max=True), [BIG])[4] is True


def test_a_saved_place_that_no_longer_fits_is_brought_back_onto_the_screen():
    assert fit_window({"x": -700, "y": -400, "w": 3000, "h": 2000}, [(0, 0, 1366, 728)])[2:4] == (1350, 688)      # (centred on the screen) but bigger than it: made to fit
    x, y, w, h, _ = fit_window({"x": 1300, "y": 700, "w": 900, "h": 600}, [(0, 0, 1366, 728)])               # hanging over the corner
    assert x + w <= 1366 and y + h <= 728


def test_a_saved_place_on_an_unplugged_monitor_is_forgotten():
    on_the_second = {"x": 2500, "y": 100, "w": 900, "h": 600}
    x, y, w, h, _ = fit_window(on_the_second, [BIG])                                  # only the first monitor is left
    assert (w, h) == DEFAULT_SIZE and 0 <= x < 1920
    both = [BIG, (1920, 0, 1920, 1040)]
    assert fit_window(on_the_second, both)[:4] == (2500, 100, 900, 600)               # ... and respected while it is there


@pytest.mark.parametrize("bad", [{}, {"x": "a"}, {"x": 1, "y": 2, "w": 0, "h": 0}, "junk", None, {"x": 1, "y": 2, "w": -5, "h": 9}])
def test_a_damaged_saved_place_is_ignored(bad):
    x, y, w, h, maxed = fit_window(bad, [BIG])
    assert (w, h) == DEFAULT_SIZE and not maxed


def test_no_screen_known_gives_the_default():
    assert fit_window(None, [])[2:4] == DEFAULT_SIZE


def test_the_window_remembers_its_place_and_size(qapp, store):
    w = MainWindow(store, lambda emit: FakeEngine(store, emit))
    w.setGeometry(60, 70, 900, 600)
    w.show()
    pump()
    w.close()
    saved = store.setting("window")
    assert (saved["w"], saved["h"]) == (900, 600) and saved["max"] is False
    again = MainWindow(store, lambda emit: FakeEngine(store, emit))
    assert (again.width(), again.height()) == (min(900, again.width()), min(600, again.height()))
    assert again.width() >= MIN_SIZE[0] - 120 and again.height() >= 400
    again.close()


def test_the_window_can_be_made_small_but_not_too_small(win):
    assert win.minimumWidth() <= MIN_SIZE[0] and win.minimumHeight() <= MIN_SIZE[1]
    assert win.minimumWidth() >= 600 and win.minimumHeight() >= 400


# ── nothing is cut off, at any size ─────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("w,h", SIZES)
def test_the_booking_form_has_nothing_cut_off_at_any_size(win, w, h):
    win.resize(w, h)
    fill(win)
    pump(25)
    assert cut_off(win) == []


@pytest.mark.parametrize("w,h", SIZES)
def test_every_right_hand_panel_fits_its_column_at_any_size(win, w, h):
    win.resize(w, h)
    pump(20)
    b = win.book
    for panel in (b.summary, b.progress, b.choice, b.result):
        room = b.stack.width() - b.stack.contentsMargins().left() - b.stack.contentsMargins().right()
        assert panel.minimumSizeHint().width() <= room, (type(panel).__name__, panel.minimumSizeHint().width(), room)


def test_the_page_gives_way_in_three_steps(win):
    b = win.book
    seen = {}
    for w, h in ((1500, 900), (1100, 700), (830, 600)):                                # (the page is the window less the menu: 1268, 868 and 762 pixels)
        win.resize(w, h)
        pump()
        seen[w] = (b.tier, b.right.width())
    assert seen[1500][0] == 0 and seen[1500][1] == b.RIGHT[0]
    assert seen[1100][0] == 1 and seen[1100][1] == b.RIGHT[1]
    assert seen[830][0] == 2 and seen[830][1] == b.RIGHT[2]
    win.resize(1500, 900)
    pump()
    assert b.tier == 0 and b.right.width() == b.RIGHT[0]                                # ... and back again


def test_the_menu_becomes_a_slim_rail_on_a_narrow_window_and_back(win):
    win.resize(1320, 860)
    pump()
    assert win.sidebar.width() == win.sidebar.FULL and not win.sidebar.compact
    win.resize(900, 600)
    pump()
    assert win.sidebar.width() == win.sidebar.SLIM and win.sidebar.compact
    assert all(not b.text().strip().split()[-1].isalpha() for b in win.sidebar.nav)      # (icons only; the words are in the tooltips)
    assert all(b.toolTip() for b in win.sidebar.nav)
    win.resize(1320, 860)
    pump()
    assert win.sidebar.width() == win.sidebar.FULL and "booking" in win.sidebar.nav[0].text()


def test_a_wide_window_is_laid_out_exactly_as_it_always_was(win):
    win.resize(1320, 860)
    fill(win)
    pump(20)
    b = win.book
    assert b.tier == 0 and win.sidebar.width() == 232 and b.right.width() == 370
    assert all(not r.compact for r in b.rows)                                          # every traveller in ONE line
    assert not b.form_scroll.widget().findChild(WrapRow).stacked                       # the date and its quick buttons side by side
    assert win.book.date_field.calendar.months_shown in (1, 2)


# ── the rows that fit themselves ────────────────────────────────────────────────────────────────────────────────────────────
def test_the_traveller_rows_stand_in_one_line_or_two_by_the_room_they_have_and_all_alike(win):
    fill(win)
    b = win.book
    win.resize(1320, 860)
    pump(20)
    assert [r.compact for r in b.rows] == [False, False, False]
    win.resize(820, 600)
    pump(20)
    assert [r.compact for r in b.rows] == [True, True, True]                           # one decision for all, so the rows look alike
    win.resize(1320, 860)
    pump(20)
    assert [r.compact for r in b.rows] == [False, False, False]


def test_a_row_does_not_flip_back_and_forth_at_the_edge(win):
    """A scrollbar appearing takes a few pixels: that must not undo the decision (hysteresis)."""
    fill(win)
    b = win.book
    need = max(r.wide_need for r in b.rows)
    win.resize(820, 600)
    pump(20)
    assert b.rows[0].compact
    # grow the window until the row has just a little more than it needs: it stays in two lines until it has clearly more
    for extra in range(0, 60, 6):
        win.resize(820 + extra, 600)
        pump(8)
        if b.rows[0].width() >= need and b.rows[0].width() < need + 24:
            assert b.rows[0].compact
    win.resize(1320, 860)
    pump(20)
    assert not b.rows[0].compact


def test_a_traveller_added_later_stands_like_the_others(win):
    win.resize(840, 600)
    pump(20)
    assert win.book.rows[0].compact
    win.book.add_row(kind=forms.CHILD)
    pump(20)
    assert all(r.compact for r in win.book.rows)


def test_the_two_line_traveller_keeps_every_control_and_works(win):
    win.resize(820, 600)
    fill(win)
    pump(20)
    r = win.book.rows[1]
    assert r.compact and r.value().first == "ANAYA" and r.value().kind == forms.CHILD and r.value().gender == "F"
    for w in (r.num, r.kind_btns[forms.CHILD], r.male, r.female, r.first, r.last, r.rm, r.dob):
        assert w.isVisible() and w.width() > 0 and w.height() > 0
    r.female.setChecked(True)
    assert r.value().gender == "F"
    r.last.setText("KUMAR")
    assert r.value().last == "KUMAR"


def test_the_wrap_row_stands_side_by_side_when_there_is_room_and_stacked_when_there_is_not(qapp):
    from PyQt6.QtWidgets import QLabel
    from PyQt6.QtWidgets import QVBoxLayout
    a, c = QLabel("A" * 20), QLabel("B" * 20)
    row = WrapRow(a, c, gap=8)
    host = QWidget()                                                                   # (inside a layout, as in the window)
    QVBoxLayout(host).addWidget(row)
    host.resize(600, 60)
    host.show()
    pump()
    assert not row.stacked and a.geometry().right() < c.geometry().left()
    host.resize(row.minimumSizeHint().width() + 10, 90)
    pump()
    assert row.stacked and a.geometry().bottom() < c.geometry().top()
    host.resize(600, 60)
    pump()
    assert not row.stacked
    host.close()


def test_the_wrap_row_asks_for_the_smaller_room(qapp):
    from PyQt6.QtWidgets import QLabel
    a, c = QLabel("A" * 30), QLabel("B" * 30)
    row = WrapRow(a, c, gap=8)
    row.resize(900, 40)
    row.show()
    pump()
    assert row.minimumSizeHint().width() < a.sizeHint().width() + c.sizeHint().width()      # (so the window around it can get narrow enough for it to give way)
    row.close()


def test_a_short_caption_stays_in_one_line_and_a_long_one_wraps(qapp):
    cap = FlowLabel("TRAVEL DATE — tap to see the fare of every day", "label")
    cap.show()
    pump()
    one = cap.sizeHint()
    assert cap.heightForWidth(one.width()) == cap.heightForWidth(10000)                  # at its own natural width it is a single line
    assert cap.heightForWidth(80) > cap.heightForWidth(one.width())                      # squeezed, it wraps
    assert cap.minimumSizeHint().width() < one.width()
    cap.close()


def test_a_button_shows_the_wording_that_fits_and_never_asks_for_more_room(qapp):
    b = FitButton("x", "primary")
    b.set_wordings("Continue with 6E 5917 + 6E 5855 · ₹9,440", "Continue · ₹9,440", "Continue")
    host = QWidget()
    host.resize(400, 60)
    b.setParent(host)
    host.show()
    b.setGeometry(0, 0, 400, 46)
    pump()
    assert b.text().startswith("Continue with 6E 5917")
    b.setGeometry(0, 0, 230, 46)
    pump()
    assert b.text() == "Continue · ₹9,440" and "6E 5917" in b.toolTip()                    # shorter wording, the full one in the tooltip
    b.setGeometry(0, 0, 120, 46)
    pump()
    assert b.text() == "Continue"
    b.setGeometry(0, 0, 50, 46)
    pump()
    assert b.text().endswith("…") or b.text() == "Continue"
    assert b.minimumSizeHint().width() <= 60
    host.close()


def test_a_button_that_was_laid_out_while_hidden_still_gets_the_wording_that_fits(qapp):
    """Found in a screenshot: a flight list that arrived while the panel was hidden left the long wording on the button (cut off on both sides)."""
    host = QWidget()
    host.resize(230, 60)
    b = FitButton("x", "primary")
    b.setParent(host)
    b.setGeometry(0, 0, 230, 46)                              # (sized while the host is still hidden)
    b.set_wordings("Continue with 6E 5917 + 6E 5855 · ₹9,440", "Continue · ₹9,440", "Continue")
    host.show()
    pump()
    assert b.text() == "Continue · ₹9,440" and "6E 5917" in b.toolTip()
    host.close()


# ── the fare calendar on a narrow screen ────────────────────────────────────────────────────────────────────────────────────
def test_the_calendar_shows_one_month_on_a_narrow_screen_and_two_on_a_wide_one(win):
    cal = win.book.date_field.calendar
    win.book.date_field.set_date(forms.today_ist() + timedelta(days=3))
    cal.configure(forms.today_ist(), forms.today_ist() + timedelta(days=330), forms.today_ist() + timedelta(days=3))
    cal.show_months(2)
    cal.adjustSize()
    two = cal.width()
    cal.show_months(1)
    cal.adjustSize()
    one = cal.width()
    assert one < 520 and two > 800 and one < two
    first = cal.view
    cal.next_btn.click()                                                               # one month at a time, and the last bookable month can still be reached
    assert cal.view == add_months(first, 1)
    cal.close()


# ── the soft shadows cost nothing while the window is used ──────────────────────────────────────────────────────────────────
def test_no_card_or_panel_carries_a_graphics_effect(win):
    b = win.book
    for w in list(b.cards) + [b.summary, b.progress, b.choice, b.result]:
        assert w.graphicsEffect() is None, type(w).__name__
    assert win.history.table.graphicsEffect() is None


@pytest.fixture()
def layer_updates(monkeypatch):
    """Counts how often a shadow layer asks to be re-drawn (update()). (When a child repaints - a blinking cursor - Qt also draws the small dirty area of the
    layer under it: that is a few clipped picture-copies, nothing like re-drawing and re-blurring a whole card. What must not happen is the layer re-drawing ITSELF
    because something inside a card changed.)"""
    calls = []
    original = shadows.ShadowLayer.update
    monkeypatch.setattr(shadows.ShadowLayer, "update", lambda self, *a: (calls.append(1), original(self, *a))[1])
    return calls


def test_a_blinking_cursor_or_a_typed_letter_does_not_make_the_shadows_redraw(win, layer_updates):
    b = win.book
    pump(25)
    layer_updates.clear()
    b.rows[0].first.setFocus()
    for ch in "RAHUL":
        b.rows[0].first.setText(b.rows[0].first.text() + ch)
        pump(3)
    b.phone.setFocus()
    b.phone.setText("98765")
    pump(10)
    assert layer_updates == []


def test_the_shadow_layer_follows_a_card_that_moves_or_changes_size(win, layer_updates):
    b = win.book
    pump(25)
    layer_updates.clear()
    b.add_row(kind=forms.CHILD)                                                          # the travellers card grows, the cards below it move down
    pump(15)
    assert len(layer_updates) >= 1


def _alpha_around(blur=22, alpha=16, dy=4, radius=16):
    img = QImage(300, 220, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    p = QPainter(img)
    shadows.paint_shadow(p, QRect(70, 50, 160, 100), blur, alpha, dy, radius)
    p.end()
    return img


def test_the_shadow_is_soft_symmetrical_and_reaches_only_so_far():
    img = _alpha_around()
    a = lambda x, y: QColor(img.pixelColor(x, y)).alpha()                                # noqa: E731
    assert a(150, 100) == 0                                                              # inside the card nothing is painted... (the card covers it anyway)
    assert a(150, 154) > a(150, 160) > a(150, 170) >= 0                                  # fading away below the card
    assert a(5, 5) == 0 and a(295, 215) == 0                                             # far away: nothing
    assert abs(a(60, 100) - a(240, 100)) <= 1                                            # symmetrical left / right
    assert a(150, 40) < a(150, 160)                                                      # the shadow sits a little LOWER than the card (dy)
    assert max(QColor(img.pixelColor(x, y)).alpha() for x in range(300) for y in range(220)) <= 16 + 1   # never darker than asked for


def test_the_shadow_pictures_are_made_once_and_reused():
    shadows._cache.clear()
    shadows.shadow_slices(22, 16, 16)
    first = shadows._cache[(22, 16, 16)]
    shadows.shadow_slices(22, 16, 16)
    assert shadows._cache[(22, 16, 16)] is first and len(shadows._cache) == 1


def test_the_shadow_looks_like_the_effect_it_replaced(qapp):
    """The look is the same soft shadow as the old QGraphicsDropShadowEffect: compared pixel by pixel (mean difference well below 1 of 255)."""
    from PyQt6.QtWidgets import QGraphicsDropShadowEffect, QGraphicsPixmapItem, QGraphicsScene
    blur, alpha, dy, radius = 28, 28, 6, 16
    size = (320, 240)
    ours = QImage(*size, QImage.Format.Format_ARGB32_Premultiplied)
    ours.fill(0)
    p = QPainter(ours)
    shadows.paint_shadow(p, QRect(80, 60, 160, 100), blur, alpha, dy, radius)
    p.end()
    card = QImage(160, 100, QImage.Format.Format_ARGB32_Premultiplied)
    card.fill(0)
    cp = QPainter(card)
    cp.setBrush(QColor("white"))
    cp.setPen(Qt.PenStyle.NoPen)
    cp.setRenderHint(QPainter.RenderHint.Antialiasing)
    cp.drawRoundedRect(QRect(0, 0, 160, 100), radius, radius)
    cp.end()
    from PyQt6.QtGui import QPixmap
    scene = QGraphicsScene(0, 0, *size)
    item = QGraphicsPixmapItem(QPixmap.fromImage(card))
    item.setPos(80, 60)
    eff = QGraphicsDropShadowEffect()
    eff.setBlurRadius(blur)
    eff.setOffset(0, dy)
    eff.setColor(QColor(30, 40, 90, alpha))
    item.setGraphicsEffect(eff)
    scene.addItem(item)
    theirs = QImage(*size, QImage.Format.Format_ARGB32_Premultiplied)
    theirs.fill(0)
    sp = QPainter(theirs)
    scene.render(sp)
    sp.end()
    total = n = 0
    for y in range(0, size[1], 2):
        for x in range(0, size[0], 2):
            inside_card = 80 <= x < 240 and 60 <= y < 160
            if inside_card:
                continue
            total += abs(QColor(ours.pixelColor(x, y)).alpha() - QColor(theirs.pixelColor(x, y)).alpha())
            n += 1
    assert total / n < 1.0, total / n
