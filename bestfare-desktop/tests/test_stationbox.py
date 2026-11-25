"""The From / To boxes: suggestions as you type, picking, and understanding what was typed."""
from widgets import AIRPORT_ROLE, CODE_ROLE, StationBox


def rows(box):
    m = box._model
    return [(m.index(i, 0).data(CODE_ROLE), m.index(i, 0).data(), m.index(i, 0).data(AIRPORT_ROLE)) for i in range(m.rowCount())]


def make(qapp):
    box = StationBox("From")
    log = {"changed": [], "chosen": []}
    box.changed.connect(log["changed"].append)
    box.chosen.connect(log["chosen"].append)
    box.log = log
    return box


def test_typing_fills_the_list_with_city_airport_and_code(qapp):
    box = make(qapp)
    box._edited("bang")
    assert rows(box) == [("BLR", "Bengaluru (BLR)", "Kempegowda International Airport")]
    box._edited("mumb")
    assert [r[0] for r in rows(box)] == ["BOM", "NMI"]
    assert rows(box)[0][2] == "Chhatrapati Shivaji Maharaj International Airport"


def test_old_names_and_airport_names_find_the_airport(qapp):
    box = make(qapp)
    for typed, code in (("bombay", "BOM"), ("vizag", "VTZ"), ("indira", "DEL"), ("calcutta", "CCU"), ("dabolim", "GOI")):
        box._edited(typed)
        assert rows(box)[0][0] == code, typed


def test_an_empty_box_offers_the_popular_airports(qapp):
    box = make(qapp)
    box._edited("")
    assert [r[0] for r in rows(box)][:5] == ["DEL", "BOM", "BLR", "CCU", "MAA"] and len(rows(box)) == 7


def test_nothing_matching_shows_no_list(qapp):
    box = make(qapp)
    box._edited("qzqz")
    assert rows(box) == []


def test_picking_sets_the_airport_and_lets_the_form_move_on(qapp):
    box = make(qapp)
    box._edited("del")
    box._picked(box._model.index(0, 0))
    assert box.code() == "DEL" and box.text() == "Delhi (DEL)"
    assert box.log["changed"] == ["DEL"] and box.log["chosen"] == ["DEL"]


def test_tab_style_acceptance_does_not_move_the_form_on_by_itself(qapp):
    box = make(qapp)
    box._edited("pun")
    box._picked(box._model.index(0, 0), advance=False)
    assert box.code() == "PNQ" and box.log["chosen"] == []


def test_editing_after_a_pick_forgets_the_airport_until_a_new_one_is_chosen(qapp):
    box = make(qapp)
    box._edited("del")
    box._picked(box._model.index(0, 0))
    box.setText("Delhi (DEL")
    box._edited("Delhi (DEL")
    assert box._code is None and box.log["changed"][-1] == ""
    box.setText("Delhi (DEL)")
    assert box.code() == "DEL"                                                      # the text names it exactly: understood again


def test_typed_text_without_a_pick_is_understood_when_it_is_clear(qapp):
    box = make(qapp)
    box.setText("bangalore")
    assert box.code() == "BLR" and box.text() == "Bengaluru (BLR)"
    box.setText("goa")
    box._code = None
    assert box.code() is None                                                       # two airports: the person must choose
    box.setText("")
    box._code = None
    assert box.code() is None


# ── real typing, like a person at a keyboard (these once failed: "m" turned into "Mumbai (BOM)" and the next letter landed
#    behind it, and merely clicking into an empty box filled in Delhi) ─────────────────────────────────────────────────────
def typing_rig(qapp):
    from PyQt6.QtWidgets import QLineEdit, QVBoxLayout, QWidget
    host = QWidget()
    lay = QVBoxLayout(host)
    a, b, other = make(qapp), make(qapp), QLineEdit()
    for w in (a, b, other):
        lay.addWidget(w)
    host.resize(500, 300)
    host.show()
    qapp.processEvents()
    return host, a, b


def type_into(box, text):
    from PyQt6.QtTest import QTest
    box.setFocus()
    QTest.qWait(60)
    box.clear()
    for ch in text:
        QTest.keyClick(box, ch)
        QTest.qWait(40)


def test_clicking_into_an_empty_box_fills_nothing_and_leaving_it_chooses_nothing(qapp):
    from PyQt6.QtTest import QTest
    host, a, _b = typing_rig(qapp)
    a.setFocus()
    QTest.qWait(120)
    assert a.text() == "" and a._code is None                               # the list is open, but nothing is filled in
    a.clearFocus()
    QTest.qWait(60)
    assert a.text() == "" and a.code() is None
    host.close()


def test_typing_keeps_exactly_what_was_typed(qapp):
    host, _a, b = typing_rig(qapp)
    seen = []
    from PyQt6.QtTest import QTest
    b.setFocus()
    QTest.qWait(60)
    for ch in "mum":
        QTest.keyClick(b, ch)
        QTest.qWait(40)
        seen.append(b.text())
    assert seen == ["m", "mu", "mum"] and b._code is None
    assert rows(b)[0][0] == "BOM"                                            # ...while the first suggestion is ready
    host.close()


def test_enter_in_the_list_picks_the_first_suggestion_and_moves_on(qapp):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    host, _a, b = typing_rig(qapp)
    type_into(b, "mum")
    pop = b._completer.popup()
    assert pop.isVisible()
    QTest.keyClick(pop, Qt.Key.Key_Return)                                   # (a real keyboard talks to the open list)
    QTest.qWait(80)
    assert b.text() == "Mumbai (BOM)" and b.code() == "BOM" and b.log["chosen"] == ["BOM"]
    host.close()


def test_clicking_a_suggestion_picks_it(qapp):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    host, a, _b = typing_rig(qapp)
    type_into(a, "kol")
    pop = a._completer.popup()
    QTest.mouseClick(pop.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     pop.visualRect(a._model.index(0, 0)).center())
    QTest.qWait(80)
    assert a.text() == "Kolkata (CCU)" and a.code() == "CCU" and a.log["chosen"] == ["CCU"]
    host.close()


def test_tab_while_suggestions_show_takes_the_first_one(qapp):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    host, _a, b = typing_rig(qapp)
    type_into(b, "pun")
    pop = b._completer.popup()
    assert pop.isVisible()
    QTest.keyClick(pop, Qt.Key.Key_Tab)                                      # (a real keyboard talks to the open list)
    QTest.qWait(80)
    assert b.text() == "Pune (PNQ)" and b.code() == "PNQ"
    assert b.log["chosen"] == []                                             # (Tab moves the focus itself: no extra jump)
    assert not b.hasFocus()
    host.close()


def test_pressing_book_now_with_a_half_typed_airport_takes_the_first_suggestion(qapp):
    host, a, _b = typing_rig(qapp)
    type_into(a, "ahm")
    assert a.code() == "AMD" and a.text() == "Ahmedabad (AMD)"               # what the form asks when Book now is pressed
    host.close()


def test_leaving_the_box_with_text_that_matches_nothing_chooses_nothing(qapp):
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    host, a, _b = typing_rig(qapp)
    type_into(a, "xyzq")
    QTest.keyClick(a, Qt.Key.Key_Tab)
    QTest.qWait(80)
    assert a.text() == "xyzq" and a.code() is None
    host.close()


def test_set_code_and_clear(qapp):
    box = make(qapp)
    box.set_code("HYD")
    assert box.text() == "Hyderabad (HYD)" and box.code() == "HYD"
    box.set_code(None)
    assert box.text() == "" and box._code is None and box.log["changed"] == ["HYD", ""]
    box.set_code("ZZZ")
    assert box.text() == "" and box._code is None
