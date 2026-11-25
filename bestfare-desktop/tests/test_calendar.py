"""The fare calendar and the date field."""
from datetime import date, timedelta

import pytest

import forms
from widgets import DateField, FareCalendar, add_months, month_start

TODAY = date(2026, 11, 18)
FIRST, LAST = TODAY, TODAY + timedelta(days=330)


def d(n):
    return TODAY + timedelta(days=n)


def make(qapp, selected=None):
    cal = FareCalendar()
    cal.configure(FIRST, LAST, selected)
    asked = []
    picked = []
    cal.need_fares.connect(asked.append)
    cal.picked.connect(picked.append)
    cal.asked, cal.chosen = asked, picked
    return cal


def cell(cal, day):
    for m in range(2):
        for c in cal.cells[m]:
            if c.d == day:
                return c
    raise AssertionError(f"{day} is not on screen")


def fares_for(days, skip=()):
    return {d(i).isoformat(): [6000.0 + 37 * i, "LMH"[i % 3]] for i in range(days) if i not in skip}


def test_the_calendar_shows_two_months_laid_out_like_a_calendar(qapp):
    cal = make(qapp)
    assert cal.title1.text() == "November 2026" and cal.title2.text() == "December 2026"
    assert cell(cal, date(2026, 11, 1)) is cal.cells[0][6]                          # 1 Nov 2026 is a Sunday: 7th column of the first row
    assert cell(cal, date(2026, 12, 1)) is cal.cells[1][1]                          # a Tuesday
    visible = [c for c in cal.cells[0] if c.d is not None]
    assert len(visible) == 30 and len([c for c in cal.cells[1] if c.d is not None]) == 31


def test_days_that_cannot_be_booked_are_greyed_out(qapp):
    cal = make(qapp)
    assert not cell(cal, date(2026, 11, 17)).enabled_day                            # yesterday
    assert cell(cal, TODAY).enabled_day and cell(cal, date(2026, 12, 31)).enabled_day


def test_navigation_stays_inside_what_can_be_booked(qapp):
    cal = make(qapp)
    assert not cal.prev_btn.isEnabled() and cal.next_btn.isEnabled()
    cal.next_btn.click()
    assert cal.title1.text() == "December 2026" and cal.prev_btn.isEnabled()
    for _ in range(20):
        if cal.next_btn.isEnabled():
            cal.next_btn.click()
    assert not cal.next_btn.isEnabled() and add_months(cal.view, 1) <= month_start(LAST) + timedelta(days=31)


def test_loading_shows_dots_but_the_days_can_already_be_picked(qapp):
    cal = make(qapp)
    cal.set_loading()
    c = cell(cal, d(3))
    assert c.enabled_day and c.note == "…" and c.price is None
    assert "Loading fares" in cal.status.text()
    c.click()
    assert cal.chosen == [d(3)]


def test_fares_colour_the_days_and_missing_days_are_marked(qapp):
    cal = make(qapp)
    cal.set_loading()
    cal.set_fares(TODAY, 20, fares_for(20, skip=(5,)))
    c = cell(cal, d(2))
    assert c.price == 6074.0 and c.cat == "H" and c.enabled_day
    assert cell(cal, d(0)).cat == "L" and cell(cal, d(1)).cat == "M"
    no_flight = cell(cal, d(5))
    assert not no_flight.enabled_day and no_flight.note == "—" and no_flight.price is None
    beyond = cell(cal, d(40))
    assert beyond.enabled_day and beyond.price is None and beyond.note == ""         # not answered yet: can still be picked
    assert cal.state == "ok"


def test_the_cheapest_day_is_starred_and_has_a_button(qapp):
    cal = make(qapp)
    fares = {d(2).isoformat(): [7000.0, "H"], d(6).isoformat(): [6101.0, "L"], d(9).isoformat(): [6101.0, "L"], d(11).isoformat(): [8000.0, "H"]}
    cal.set_fares(TODAY, 60, fares)
    assert cal.cheapest_day() == (d(6), 6101.0)                                       # the earlier of two equal fares
    assert cell(cal, d(6)).cheapest and not cell(cal, d(9)).cheapest
    assert cal.cheap_btn.text() == f"Cheapest: {d(6).strftime('%a %d %b')} · ₹6,101"
    cal.cheap_btn.click()
    assert cal.chosen == [d(6)] and cal.selected == d(6)


def test_the_selected_day_is_marked_and_clicking_picks_it(qapp):
    cal = make(qapp, selected=d(4))
    assert cell(cal, d(4)).selected and not cell(cal, d(5)).selected
    cell(cal, d(8)).click()
    assert cal.chosen == [d(8)] and cell(cal, d(8)).selected
    disabled = cell(cal, date(2026, 11, 10))
    disabled.click()
    assert cal.chosen == [d(8)]                                                         # a day in the past cannot be picked


def test_a_route_the_calendar_has_no_fares_for_still_lets_every_date_be_picked(qapp):
    """Found live: IndiGo's calendar answers 'Data is not Available' for Bengaluru -> Mumbai although 64 flights fly it. It must
    never grey out the dates or claim that there are no flights - the flights are listed when the traveller books."""
    cal = make(qapp)
    cal.set_fares(TODAY, 60, {}, empty=True)
    assert cal.state == "empty" and "pick any date" in cal.status.text() and "no flights" not in cal.status.text()
    c = cell(cal, d(6))
    assert c.enabled_day and c.price is None and c.note == ""
    c.click()
    assert cal.chosen == [d(6)]
    assert cal.cheapest_day() is None and not cal.cheap_btn.isVisible()


@pytest.mark.parametrize("error,words", [("busy", "a little later"), ("unavailable", "not available")])
def test_when_fares_cannot_be_had_the_calendar_still_works(qapp, error, words):
    cal = make(qapp)
    cal.set_fares(TODAY, 60, {}, error=error)
    assert words in cal.status.text()
    c = cell(cal, d(3))
    assert c.enabled_day and c.price is None
    c.click()
    assert cal.chosen == [d(3)]


def test_it_asks_for_the_windows_it_has_not_got_and_stops_asking_once_it_has_them(qapp):
    cal = make(qapp)
    cal.ask_for_view()
    assert cal.asked == [TODAY]                                                        # the first day of the first month on screen
    cal.set_fares(TODAY, 60, fares_for(60))                                           # 18 Nov .. 16 Jan covers November and December
    cal.asked.clear()
    cal.ask_for_view()
    assert cal.asked == []
    cal.next_btn.click()                                                               # December + January: the end of January is new
    assert cal.asked == [d(60)]
    cal.set_fares(d(60), 60, {d(60 + i).isoformat(): [6000.0, "L"] for i in range(60)})
    cal.asked.clear()
    cal.ask_for_view()
    assert cal.asked == []


def test_a_new_route_forgets_the_old_fares(qapp):
    cal = make(qapp)
    cal.set_fares(TODAY, 60, fares_for(60))
    cal.reset_fares("DEL-GOI")
    assert cal.fares == {} and cal.loaded == [] and cal.state == "idle" and cell(cal, d(2)).price is None


def test_the_popup_lands_on_the_screen(qapp):
    from PyQt6.QtWidgets import QPushButton
    anchor = QPushButton("x")
    anchor.resize(200, 30)
    anchor.show()
    cal = make(qapp)
    cal.popup_at(anchor)
    assert cal.isVisible()
    screen = qapp.primaryScreen().availableGeometry()
    assert screen.contains(cal.geometry().topLeft())
    cal.close()
    anchor.close()


def test_the_date_field_shows_the_day_and_tells_when_it_changes(qapp):
    f = DateField(FIRST, LAST, d(1))
    seen = []
    f.changed.connect(seen.append)
    assert f.text().endswith(forms.day_long(d(1)))
    f.set_date(d(5))
    f.set_date(d(5))                                                                  # the same day again: no new event
    assert seen == [d(5)] and f.date() == d(5) and f.text().endswith(forms.day_long(d(5)))
    f.set_date(d(1000))
    assert f.date() == LAST                                                           # never beyond what IndiGo sells
    f.set_date(date(2020, 1, 1))
    assert f.date() == FIRST


def test_picking_in_the_calendar_updates_the_field(qapp):
    f = DateField(FIRST, LAST, d(1))
    seen = []
    f.changed.connect(seen.append)
    f.open_calendar()
    cell(f.calendar, d(12)).click()
    assert f.date() == d(12) and seen == [d(12)] and not f.calendar.isVisible()
