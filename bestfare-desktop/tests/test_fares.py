from datetime import date

import pytest

import fares

REAL_LIKE = {"data": {"lowFares": [{"date": "2026-11-19T00:00:00", "price": 15473.0, "category": "H"},
                                   {"date": "2026-11-20T00:00:00", "price": 11439.0, "category": "H"},
                                   {"date": "2026-11-23T00:00:00", "price": 7220.0, "category": "L"},
                                   {"date": "2026-11-26T00:00:00", "price": 6331.0, "category": "L"},
                                   {"date": "2026-11-28T00:00:00", "price": 6331.0, "category": "m"}],
                      "mileConversationRate": 0.4088}, "errors": None}
NO_FLIGHTS = {"data": {"CityName": None}, "errors": {"code": "Data is not Available", "message": "Data is not Available"}}


def test_the_request_is_what_indigos_own_date_picker_sends():
    b = fares.request_body("DEL", "BOM", date(2026, 11, 19))
    assert b == {"startDate": "2026-11-19", "endDate": "2027-01-17", "origin": "DEL", "destination": "BOM", "currencyCode": "INR",
                 "promoCode": "", "lowestIn": "M"}
    assert fares.request_body("DEL", "BOM", date(2026, 11, 19), days=10)["endDate"] == "2026-11-28"
    assert fares.request_body("DEL", "BOM", date(2026, 11, 19), days=500)["endDate"] == "2027-01-17"      # at most 60 days a call


def test_the_answer_becomes_a_fare_for_each_day():
    r = fares.parse(REAL_LIKE)
    assert r.error is None and not r.empty
    assert r.fares["2026-11-19"] == (15473.0, "H") and r.fares["2026-11-28"] == (6331.0, "M") and len(r.fares) == 5
    assert "2026-11-21" not in r.fares                                      # no flight that day = no entry


def test_a_route_without_flights_is_told_apart_from_a_failure():
    r = fares.parse(NO_FLIGHTS)
    assert r.empty and not r.fares and r.error is None
    assert fares.parse({}).empty


def test_the_cheapest_day_prefers_the_earlier_date_on_a_tie():
    f = fares.parse(REAL_LIKE).fares
    assert fares.cheapest(f) == ("2026-11-26", 6331.0)
    assert fares.cheapest(f, first=date(2026, 11, 27)) == ("2026-11-28", 6331.0)
    assert fares.cheapest(f, first=date(2027, 1, 1)) is None


class Tab:
    def __init__(self, result=None, boom=False):
        self.result, self.boom, self.js = result, boom, None

    async def evaluate_json(self, js, timeout=60):
        self.js = js
        if self.boom:
            raise RuntimeError("tab gone")
        return self.result


@pytest.mark.parametrize("result,expect_error", [({"status": 200, "json": REAL_LIKE}, None), ({"fetchError": "TypeError: Failed to fetch"}, "busy"),
                                                 ({"status": 403}, "busy"), ({"status": 429}, "busy"), ({"status": 500}, "unavailable")])
async def test_fetch_never_raises_and_says_why(result, expect_error):
    tab = Tab(result)
    r = await fares.fetch(tab, "DEL", "BOM", date(2026, 11, 19))
    assert r.error == expect_error
    assert fares.URL in tab.js and fares.USER_KEY in tab.js and r'\"promoCode\": \"\"' in tab.js     # (the body is a JSON string inside the JS)
    if expect_error is None:
        assert len(r.fares) == 5


async def test_fetch_survives_a_dead_page():
    r = await fares.fetch(Tab(boom=True), "DEL", "BOM", date(2026, 11, 19))
    assert r.error == "unavailable" and not r.fares


def test_a_day_with_a_zero_price_has_no_fare_and_is_never_the_cheapest_day():
    """Found live: late in the evening IndiGo answers 0 for today (no seat left to sell) - that is not a fare, and the calendar's
    'Cheapest' button must never pick it (it booked a day with no seats)."""
    r = fares.parse({"data": {"lowFares": [{"date": "2026-11-18T00:00:00", "price": 0, "category": "L"},
                                          {"date": "2026-11-19T00:00:00", "price": -5, "category": "L"},
                                          {"date": "2026-11-20T00:00:00", "price": 6331, "category": "L"}]}})
    assert r.fares == {"2026-11-20": (6331.0, "L")} and fares.cheapest(r.fares) == ("2026-11-20", 6331.0)
    only_zero = fares.parse({"data": {"lowFares": [{"date": "2026-11-18T00:00:00", "price": 0}]}})
    assert only_zero.fares == {} and only_zero.empty is True
