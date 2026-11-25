"""What the admin's buttons do to ONE user, as the server sees it: each action must have its real effect (the user's app is stopped, asked
for a new key, ...) and the activity log must say exactly what happened - no more, no less."""
import pytest

from bfcloud.service import CloudError
from conftest import Device  # noqa: F401
from test_service import activate, row  # noqa: F401


def kinds(cloud, lid):
    return [e["kind"] for e in cloud.events(limit=500) if e["licence_id"] == lid]


def test_an_edit_that_changes_nothing_writes_nothing_and_keeps_the_users_session(cloud):
    lic = cloud.create_licence("Rahul", credits=5, note="n", mac_lock="3C:52:82:AA:BB:CC", actor="t")
    before = row(cloud, lic["id"])["epoch"]
    n = len(kinds(cloud, lic["id"]))
    cloud.update_licence(lic["id"], label="Rahul", note="n", mac_lock="3c-52-82-aa-bb-cc", offers=["offer1", "offer2"], actor="boss")
    assert row(cloud, lic["id"])["epoch"] == before                      # the user's app is not thrown out for a save that changed nothing
    assert len(kinds(cloud, lic["id"])) == n                             # and the activity log is not filled with "edited" / "changed coupons"


def test_an_edit_says_exactly_what_changed(cloud):
    lic = cloud.create_licence("Rahul", credits=5, actor="t")
    cloud.update_licence(lic["id"], label="Rahul S.", note="friend", offers=["offer1", "offer2"], actor="boss")
    ev = [e for e in cloud.events(kind="licence_edited") if e["licence_id"] == lic["id"]]
    assert len(ev) == 1 and "name Rahul -> Rahul S." in ev[0]["detail"] and "note" in ev[0]["detail"] and ev[0]["actor"] == "boss"
    assert "offers_changed" not in kinds(cloud, lic["id"])               # the coupons were not touched, so no "changed which coupons"
    cloud.update_licence(lic["id"], offers=["offer2"], actor="boss")
    assert "offers_changed" in kinds(cloud, lic["id"])


def test_only_a_new_mac_lock_ends_the_session(cloud):
    lic = cloud.create_licence("Rahul", credits=5, actor="t")
    e0 = row(cloud, lic["id"])["epoch"]
    cloud.update_licence(lic["id"], note="hello", mac_lock="", actor="boss")
    assert row(cloud, lic["id"])["epoch"] == e0
    cloud.update_licence(lic["id"], mac_lock="3C:52:82:AA:BB:CC", actor="boss")
    assert row(cloud, lic["id"])["epoch"] == e0 + 1
    ev = [e for e in cloud.events(kind="licence_edited") if e["licence_id"] == lic["id"]][0]
    assert "MAC lock set to 3c:52:82:aa:bb:cc" in ev["detail"]


def test_a_bad_value_changes_nothing_at_all(cloud):
    lic = cloud.create_licence("Rahul", credits=5, note="keep", actor="t")
    for bad in ({"mac_lock": "not-a-mac"}, {"expires_days": 0}, {"expires_days": True}, {"offers": ["nope"]}, {"label": "   "}):
        with pytest.raises(CloudError):
            cloud.update_licence(lic["id"], actor="boss", **{"label": "Changed", "note": "changed", **bad})
        r = row(cloud, lic["id"])
        assert r["label"] == "Rahul" and r["note"] == "keep", bad        # the good fields beside a bad one were NOT saved


def test_blocking_stops_the_app_unblocking_lets_it_in_and_both_are_written_with_who_and_why(cloud):
    lic = cloud.create_licence("Rahul", credits=5, actor="t")
    activate(cloud, lic["key"])
    e0 = row(cloud, lic["id"])["epoch"]
    cloud.set_status(lic["id"], "blocked", "shared the key", actor="boss")
    assert row(cloud, lic["id"])["status"] == "blocked" and row(cloud, lic["id"])["epoch"] > e0      # the app's session token no longer fits
    view = cloud.licence_detail(lic["id"])
    assert view["status"] == "blocked" and view["status_by"] == "boss" and view["block_reason"] == "shared the key"
    with pytest.raises(CloudError) as e:                                 # (a unit test on a temporary database: nothing is booked anywhere)
        cloud.begin_booking(row(cloud, lic["id"]), 1, "offer1", "DEL-BOM", "2026-12-01", "x1", "1.1.1.1", 1)
    assert e.value.code == "blocked" and e.value.status == 403 and "shared the key" in e.value.message
    assert row(cloud, lic["id"])["credits"] == 5                         # nothing was charged for the refused booking
    cloud.set_status(lic["id"], "active", actor="boss")
    view = cloud.licence_detail(lic["id"])
    assert view["status"] == "active" and view["status_by"] == ""
    assert [k for k in kinds(cloud, lic["id"]) if k in ("blocked", "unblocked")] == ["unblocked", "blocked"]


def test_a_new_key_kills_the_old_key_and_the_computer_must_ask_again(cloud):
    lic = cloud.create_licence("Rahul", credits=5, actor="t")
    activate(cloud, lic["key"])
    assert row(cloud, lic["id"])["device_pub"] is not None
    new = cloud.regenerate_key(lic["id"], actor="boss")
    assert new != lic["key"] and new.endswith(row(cloud, lic["id"])["key_tail"])
    r = row(cloud, lic["id"])
    assert r["device_pub"] is None and r["bound_at"] is None             # the old computer is no longer trusted
    assert cloud.licence_detail(lic["id"])["credits"] == 5               # credits and history stay
    with pytest.raises(CloudError):
        activate(cloud, lic["key"])                                      # the OLD key is dead
    assert activate(cloud, new)                                          # the new one works (on any computer: it is bound again)


def test_reset_computer_frees_the_key_for_one_new_computer_and_stops_the_old_one(cloud):
    lic = cloud.create_licence("Rahul", credits=5, actor="t")
    activate(cloud, lic["key"])
    e0 = row(cloud, lic["id"])["epoch"]
    cloud.reset_device(lic["id"], actor="boss")
    r = row(cloud, lic["id"])
    assert r["device_pub"] is None and r["epoch"] == e0 + 1
    assert "device_reset" in kinds(cloud, lic["id"])
