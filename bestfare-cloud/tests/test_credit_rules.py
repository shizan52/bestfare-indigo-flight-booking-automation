"""The credit rules an honest user can rely on, and the limits that still stop a cheat - in numbers."""
import pytest

from bfcloud.service import CloudError


def start(cloud, lid, key_row, n=1, ref=None):
    return cloud.begin_booking(key_row, n, "offer1", "DEL-BOM", "2026-12-01", ref, "203.0.113.5", 1, 0)


@pytest.fixture()
def user(cloud):
    k = cloud.create_licence("Rahul", credits=60, actor="boss")
    row = cloud.db.one("SELECT * FROM licences WHERE id=?", (k["id"],))
    return k, row


def fail(cloud, row, i):
    b = start(cloud, row["id"], row, ref=f"r{i}")
    return cloud.finish_booking(row, b["booking_id"], "failed", None, "IndiGo was busy", "203.0.113.5")


def test_the_defaults_are_generous_enough_for_a_bad_afternoon(cloud):
    assert int(cloud.setting("refund_daily_limit")) >= 12 and float(cloud.setting("refund_ratio_limit")) >= 0.85 and int(cloud.setting("refund_ratio_window")) >= 20


def test_a_dozen_failures_in_a_day_are_all_refunded_and_the_balance_is_unchanged(cloud, user):
    k, row = user
    for i in range(12):
        assert fail(cloud, row, i)["refunded"] is True
    assert cloud.db.scalar("SELECT credits FROM licences WHERE id=?", (k["id"],)) == 60
    u = cloud.licence_detail(k["id"])["usage"]
    assert u["used"] == 12 and u["returned"] == 12 and u["kept"] == 0


def test_beyond_the_daily_limit_the_credits_stay_taken_the_admin_is_told_and_one_action_returns_them(cloud, user):
    k, row = user
    cloud.set_settings({"refund_daily_limit": 2}, "boss")
    assert fail(cloud, row, 0)["refunded"] and fail(cloud, row, 1)["refunded"]
    third = fail(cloud, row, 2)
    assert third["refunded"] is False and "not returned automatically" in third["note"]
    d = cloud.licence_detail(k["id"])
    assert d["flagged"] and d["credits"] == 59 and d["usage"]["kept"] == 1
    assert any(e["kind"] == "refund_denied" for e in d["events"])
    denied = next(b for b in d["bookings_list"] if not b["refunded"])
    cloud.admin_refund_booking(denied["id"], "boss")
    assert cloud.licence_detail(k["id"])["credits"] == 60


def test_a_client_that_fails_almost_every_time_is_not_refunded_any_more(cloud, user):
    k, row = user
    cloud.set_settings({"refund_daily_limit": 50}, "boss")                                # (so that it is the pattern rule, not the daily limit, that decides)
    for i in range(30):                                                                 # (a long run of 'failures' with no success at all is not a bad afternoon: it is a pattern)
        r = fail(cloud, row, i)
        if not r["refunded"]:
            break
    assert r["refunded"] is False and cloud.db.scalar("SELECT flagged FROM licences WHERE id=?", (k["id"],)) == 1


def test_infants_and_children_are_charged_and_returned_by_the_same_arithmetic(cloud, user):
    k, row = user
    cloud.set_settings({"infant_credits": 0}, "boss")
    b = cloud.begin_booking(row, 3, "offer1", "DEL-BOM", "2026-12-01", "x1", "203.0.113.5", 1, 1)         # 3 seats + 1 infant that costs nothing
    assert b["charged"] == 3 and b["credits"] == 57
    r = cloud.finish_booking(row, b["booking_id"], "failed", None, "x", "203.0.113.5")
    assert r["refunded"] and r["credits"] == 60
    cloud.set_settings({"infant_credits": 1}, "boss")
    b = cloud.begin_booking(row, 2, "offer1", "DEL-BOM", "2026-12-02", "x2", "203.0.113.5", 1, 1)         # 2 seats + 1 infant at a credit each
    assert b["charged"] == 3 and b["credits"] == 57
    assert cloud.finish_booking(row, b["booking_id"], "done", "ABC123", "", "203.0.113.5")["credits"] == 57


def test_the_users_page_adds_up_to_the_ledger(cloud, user):
    k, row = user
    cloud.adjust_credits(k["id"], 10, "bought 10", "boss")
    cloud.adjust_credits(k["id"], -4, "mistake", "boss")
    b = start(cloud, row["id"], row, n=2, ref="a")
    cloud.finish_booking(row, b["booking_id"], "done", "ABC123", "", "203.0.113.5")
    fail(cloud, row, 7)
    d = cloud.licence_detail(k["id"])
    u = d["usage"]
    assert u == {"given": 70, "removed": 4, "used": 3, "returned": 1, "kept": 2}
    assert d["credits"] == u["given"] - u["removed"] - u["kept"] == 64
    assert d["credits"] == d["ledger"][0]["balance"]


def test_a_blocked_user_shows_who_blocked_them_and_when_so_the_panel_can_say_it(cloud):
    k = cloud.create_licence("Rahul", credits=1, actor="boss")
    assert cloud.licence_detail(k["id"])["status_by"] == ""
    cloud.set_status(k["id"], "blocked", "shared the key", "boss")
    d = cloud.licence_detail(k["id"])
    assert d["status_by"] == "boss" and d["status_at"] and d["block_reason"] == "shared the key"
    assert [x for x in cloud.list_licences()["items"] if x["id"] == k["id"]][0]["status_by"] == "boss"
    cloud.set_status(k["id"], "blocked", "again, by someone else", "helper")
    assert cloud.licence_detail(k["id"])["status_by"] == "helper"                       # the LATEST block counts
    cloud.set_status(k["id"], "active", "", "helper")
    assert cloud.licence_detail(k["id"])["status_by"] == "" and cloud.licence_detail(k["id"])["status_at"] is None


def test_a_booking_that_worked_after_indigo_had_to_be_asked_again_keeps_the_apps_short_note_for_the_admin(cloud, user):
    k, row = user
    b = start(cloud, row["id"], row, ref="worked-late")
    out = cloud.finish_booking(row, b["booking_id"], "done", "ABC123", "worked on try 2 (fetch@search)", "203.0.113.5")
    assert out["status"] == "done" and out["refunded"] is False
    mine = [x for x in cloud.licence_detail(k["id"])["bookings_list"] if x["id"] == b["booking_id"]][0]
    assert mine["status"] == "done" and mine["pnr"] == "ABC123" and mine["reason"] == "worked on try 2 (fetch@search)"
    plain = start(cloud, row["id"], row, ref="no-retry")                                     # a booking that needed nothing keeps nothing
    cloud.finish_booking(row, plain["booking_id"], "done", "DEF456", "", "203.0.113.5")
    assert [x for x in cloud.licence_detail(k["id"])["bookings_list"] if x["id"] == plain["booking_id"]][0]["reason"] == ""
