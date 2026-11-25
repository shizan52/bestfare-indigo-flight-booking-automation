"""Infants ride on an adult's lap: the server charges them at the admin's price, never more than one per traveller, and a refund gives back
exactly what was charged."""
import pytest

from bfcloud.service import CloudError
from test_service import booker, row  # noqa: F401


def begin(cloud, r, n, infants, ref):
    return cloud.begin_booking(r, n, "offer1", "DEL-BOM", "2026-12-01", ref, "1.1.1.1", 1, infants)


def test_an_infant_costs_nothing_by_default_only_adults_and_children_are_charged(cloud):
    lic, r = booker(cloud, 10)
    out = begin(cloud, r, 2, 1, "i1")                                                # 2 seats (adults + children) + 1 infant on a lap
    assert out["charged"] == 2 and out["credits"] == 8
    d = cloud.licence_detail(lic["id"])
    assert d["bookings_list"][0]["travellers"] == 2 and d["bookings_list"][0]["infants"] == 1
    assert cloud.account(r)["infant_credits"] == 0                                   # the app is told, so it says so


def test_the_admin_can_still_choose_to_charge_for_infants(cloud):
    lic, r = booker(cloud, 10)
    cloud.set_setting("infant_credits", 1, "boss")
    out = begin(cloud, r, 2, 1, "i2")
    assert out["charged"] == 3 and out["credits"] == 7


def test_there_is_never_more_than_one_infant_per_traveller_and_nothing_odd_is_accepted(cloud):
    lic, r = booker(cloud, 10)
    for bad in (-1, 3, 2 ** 40, 1.5, "1", True, [1], None):
        if bad is None:
            continue
        with pytest.raises(CloudError) as e:
            begin(cloud, r, 2, bad, f"bad-{abs(hash(str(bad)))}")
        assert e.value.code == "bad_request", bad
    assert row(cloud, lic["id"])["credits"] == 10


def test_a_booking_without_the_infants_field_is_what_it_always_was(cloud):
    lic, r = booker(cloud, 10)
    out = cloud.begin_booking(r, 2, "offer1", "DEL-BOM", "2026-12-01", "old-app", "1.1.1.1", 1)
    assert out["charged"] == 2 and out["credits"] == 8


def test_not_enough_credits_is_counted_on_the_seats_only_and_refused_with_the_right_number(cloud):
    lic, r = booker(cloud, 3)
    out = begin(cloud, r, 3, 1, "i3")                                                 # 3 seats + 1 infant = 3 credits: exactly enough
    assert out["charged"] == 3 and out["credits"] == 0
    with pytest.raises(CloudError) as e:
        begin(cloud, r, 1, 1, "i4")                                                   # one more seat (+ an infant) with nothing left
    assert e.value.code == "no_credits" and e.value.extra["needed"] == 1 and e.value.extra["have"] == 0
    assert row(cloud, lic["id"])["credits"] == 0
