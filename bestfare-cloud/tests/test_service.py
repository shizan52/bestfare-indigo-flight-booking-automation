"""The rules: credits, one device per key, the booking gate, refunds, blocking, releases."""
import json
import threading

import pytest

from bfcloud import bundle, crypto
from bfcloud.service import Cloud, CloudError


def dev_info(name="PC1", mac="3c:52:82:aa:bb:cc"):
    return {"macs": [mac], "name": name, "os": "Windows 11", "guid": "ab" * 16}


def activate(cloud, key, signer=None, **kw):
    s = signer or crypto.Signer.generate()
    out = cloud.activate(key, s.public_b64, kw.get("info", dev_info()), kw.get("ip", "1.2.3.4"), kw.get("build", 1))
    return s, out


def row(cloud, lid):
    return cloud.db.one("SELECT * FROM licences WHERE id=?", (lid,))


# ── credits ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_credits_are_added_and_taken_with_a_ledger_that_always_adds_up(cloud):
    lic = cloud.create_licence("Anita", credits=10, actor="boss")
    assert cloud.adjust_credits(lic["id"], 5, "bonus", "boss") == 15
    assert cloud.adjust_credits(lic["id"], -3, "correction", "boss") == 12
    d = cloud.licence_detail(lic["id"])
    assert d["credits"] == 12 and sum(x["delta"] for x in d["ledger"]) == 12
    assert [x["balance"] for x in reversed(d["ledger"])] == [10, 15, 12]
    assert {x["kind"] for x in d["ledger"]} == {"grant", "remove"}


def test_credits_can_never_go_below_zero_or_be_set_by_nonsense(cloud):
    lic = cloud.create_licence("Anita", credits=2)
    with pytest.raises(CloudError) as e:
        cloud.adjust_credits(lic["id"], -3)
    assert e.value.code == "no_credits"
    assert cloud.licence_detail(lic["id"])["credits"] == 2
    for bad in (0, 1.5, "5", None, True, 10**9, -10**9):
        with pytest.raises(CloudError):
            cloud.adjust_credits(lic["id"], bad)                           # type: ignore[arg-type]
    for bad in (-1, 10**9, "3", 2.5):
        with pytest.raises(CloudError):
            cloud.create_licence("X", credits=bad)                         # type: ignore[arg-type]


# ── one key, one computer ───────────────────────────────────────────────────────────────────────────────────────────
def test_the_first_computer_binds_the_key_and_a_second_one_is_refused_and_flagged(cloud):
    lic = cloud.create_licence("Rahul", credits=3)
    s1, out = activate(cloud, lic["key"])
    assert out["account"]["credits"] == 3 and out["token"]
    r = row(cloud, lic["id"])
    assert r["device_id"] and json.loads(r["device_info"])["macs"] == ["3c:52:82:aa:bb:cc"]
    _s1b, again = activate(cloud, lic["key"], signer=s1)                   # the SAME computer may activate again (after a re-install of the shell)
    assert again["token"]
    with pytest.raises(CloudError) as e:
        activate(cloud, lic["key"], info=dev_info("PC2", "aa:aa:aa:aa:aa:aa"))
    assert e.value.code == "device_mismatch" and "another computer" in e.value.message
    assert row(cloud, lic["id"])["flagged"] == 1
    ev = cloud.events(level="alert")
    assert ev and "another computer tried this key" in ev[0]["detail"] and "aa:aa:aa:aa:aa:aa" in ev[0]["detail"]


def test_an_admin_can_move_the_key_and_the_old_computer_stops_at_once(cloud):
    lic = cloud.create_licence("Rahul", credits=3)
    s1, out = activate(cloud, lic["key"])
    cloud.reset_device(lic["id"], "boss")
    with pytest.raises(CloudError) as e:
        cloud.authorize(out["token"])
    assert e.value.code == "unauthorized"
    s2, out2 = activate(cloud, lic["key"], info=dev_info("NEW-PC", "11:22:33:44:55:66"))
    assert cloud.authorize(out2["token"])[0]["id"] == lic["id"]
    with pytest.raises(CloudError):
        activate(cloud, lic["key"], signer=s1)                             # the old computer's key pair is not the bound one any more


def test_a_key_locked_to_a_mac_address_only_works_on_that_computer(cloud):
    lic = cloud.create_licence("Rahul", credits=1, mac_lock="3C-52-82-AA-BB-CC")
    assert row(cloud, lic["id"])["mac_lock"] == "3c:52:82:aa:bb:cc"
    with pytest.raises(CloudError) as e:
        activate(cloud, lic["key"], info=dev_info("OTHER", "00:11:22:33:44:55"))
    assert e.value.code == "mac_lock"
    assert activate(cloud, lic["key"])[1]["token"]
    with pytest.raises(CloudError):
        cloud.create_licence("Bad", mac_lock="not a mac")


def test_wrong_and_malformed_keys_give_one_vague_answer(cloud):
    for bad in ("", "BF-AAAA-BBBB-CCCC-DDDD", "nonsense", "' OR 1=1 --", "A" * 5000):
        with pytest.raises(CloudError) as e:
            activate(cloud, bad)
        assert e.value.code == "bad_key" and "not valid" in e.value.message
    with pytest.raises(CloudError):
        cloud.activate("BF-AAAA-BBBB-CCCC-DDDD", "not-a-public-key", dev_info(), "1.1.1.1", 1)


def test_blocking_ends_every_session_and_unblocking_needs_a_new_sign_in(cloud):
    lic = cloud.create_licence("Rahul", credits=3)
    _s, out = activate(cloud, lic["key"])
    cloud.set_status(lic["id"], "blocked", "misuse", "boss")
    with pytest.raises(CloudError) as e:
        cloud.authorize(out["token"])
    assert e.value.code == "unauthorized"
    with pytest.raises(CloudError) as e:
        activate(cloud, lic["key"], signer=_s)
    assert e.value.code == "blocked" and e.value.message == "misuse"
    cloud.set_status(lic["id"], "active", "", "boss")
    assert activate(cloud, lic["key"], signer=_s)[1]["token"]


def test_an_expired_licence_does_not_work(cloud):
    lic = cloud.create_licence("Rahul", credits=3, expires_days=1)
    _s, out = activate(cloud, lic["key"])
    cloud.now = lambda: __import__("time").time() + 3 * 86400
    with pytest.raises(CloudError) as e:
        cloud.session(row(cloud, lic["id"]), "1.1.1.1", 1)
    assert e.value.code == "expired"
    with pytest.raises(CloudError):
        cloud.authorize(out["token"]) if False else cloud.begin_booking(row(cloud, lic["id"]), 1, "offer1", "DEL-BOM", "2026-12-01", "r1", "1.1.1.1", 1)


def test_regenerating_a_key_kills_the_old_one(cloud):
    lic = cloud.create_licence("Rahul", credits=3)
    s, out = activate(cloud, lic["key"])
    new = cloud.regenerate_key(lic["id"], "boss")
    assert new != lic["key"] and cloud.reveal_key(lic["id"], "boss") == new
    with pytest.raises(CloudError):
        activate(cloud, lic["key"], signer=s)
    assert activate(cloud, new)[1]["token"]


# ── the booking gate ────────────────────────────────────────────────────────────────────────────────────────────────
def booker(cloud, credits=5):
    lic = cloud.create_licence("Rahul", credits=credits)
    activate(cloud, lic["key"])
    return lic, row(cloud, lic["id"])


def test_a_booking_takes_one_credit_per_traveller_before_anything_is_handed_out(cloud):
    lic, r = booker(cloud, 5)
    out = cloud.begin_booking(r, 2, "offer2", "DEL-BOM", "2026-12-01", "ref1", "1.1.1.1", 1)
    assert out["charged"] == 2 and out["credits"] == 3 and out["promo"] == "PROMO2"
    assert row(cloud, lic["id"])["credits"] == 3
    d = cloud.licence_detail(lic["id"])
    assert d["ledger"][0]["delta"] == -2 and d["ledger"][0]["kind"] == "booking" and d["bookings_list"][0]["status"] == "reserved"


def test_with_too_few_credits_nothing_is_given_and_nothing_is_charged(cloud):
    lic, r = booker(cloud, 1)
    with pytest.raises(CloudError) as e:
        cloud.begin_booking(r, 2, "offer1", "DEL-BOM", "2026-12-01", "r", "1.1.1.1", 1)
    assert e.value.code == "no_credits" and e.value.extra["needed"] == 2 and e.value.extra["have"] == 1
    assert "6ETST001" not in json.dumps([e.value.message, e.value.extra]) and row(cloud, lic["id"])["credits"] == 1
    assert cloud.db.scalar("SELECT COUNT(*) FROM bookings") == 0


def test_when_the_credits_are_used_up_the_user_cannot_book_until_an_admin_adds_more(cloud):
    lic, r = booker(cloud, 1)
    cloud.begin_booking(r, 1, "offer1", "DEL-BOM", "2026-12-01", "a", "1.1.1.1", 1)
    with pytest.raises(CloudError) as e:
        cloud.begin_booking(r, 1, "offer1", "DEL-BOM", "2026-12-02", "b", "1.1.1.1", 1)
    assert e.value.code == "no_credits"
    cloud.adjust_credits(lic["id"], 4, "top up", "boss")
    assert cloud.begin_booking(r, 1, "offer1", "DEL-BOM", "2026-12-02", "c", "1.1.1.1", 1)["credits"] == 3


def test_the_same_request_twice_is_one_charge(cloud):
    lic, r = booker(cloud, 5)
    a = cloud.begin_booking(r, 2, "offer1", "DEL-BOM", "2026-12-01", "same", "1.1.1.1", 1)
    b = cloud.begin_booking(r, 2, "offer1", "DEL-BOM", "2026-12-01", "same", "1.1.1.1", 1)
    assert a["booking_id"] == b["booking_id"] and b["repeat"] and row(cloud, lic["id"])["credits"] == 3


@pytest.mark.parametrize("args", [(0, "offer1", "DEL-BOM", "2026-12-01"), (10, "offer1", "DEL-BOM", "2026-12-01"), (-1, "offer1", "DEL-BOM", "2026-12-01"),
                                  (True, "offer1", "DEL-BOM", "2026-12-01"), (1, "offer9", "DEL-BOM", "2026-12-01"), (1, "' OR 1=1", "DEL-BOM", "2026-12-01"),
                                  (1, "offer1", "DELBOM", "2026-12-01"), (1, "offer1", "DEL-BOM", "tomorrow"), (1.5, "offer1", "DEL-BOM", "2026-12-01"),
                                  ("2", "offer1", "DEL-BOM", "2026-12-01"), (1, None, "DEL-BOM", "2026-12-01")])
def test_nonsense_booking_requests_are_refused_without_charging(cloud, args):
    lic, r = booker(cloud, 5)
    with pytest.raises(CloudError):
        cloud.begin_booking(r, *args, "ref", "1.1.1.1", 1)
    assert row(cloud, lic["id"])["credits"] == 5


def test_the_last_credit_can_only_be_spent_once_even_when_requests_race(cloud):
    lic, r = booker(cloud, 3)
    results = []

    def go(i):
        try:
            cloud.begin_booking(r, 1, "offer1", "DEL-BOM", "2026-12-01", f"race{i}", "1.1.1.1", 1)
            results.append("ok")
        except CloudError as e:
            results.append(e.code)
    cloud.set_setting("max_open", 50, "boss")
    ts = [threading.Thread(target=go, args=(i,)) for i in range(25)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count("ok") == 3 and results.count("no_credits") == 22
    assert row(cloud, lic["id"])["credits"] == 0 and cloud.db.scalar("SELECT COUNT(*) FROM bookings") == 3


def test_too_many_open_bookings_at_once_are_held_back(cloud):
    lic, r = booker(cloud, 10)
    for i in range(3):
        cloud.begin_booking(r, 1, "offer1", "DEL-BOM", "2026-12-01", f"o{i}", "1.1.1.1", 1)
    with pytest.raises(CloudError) as e:
        cloud.begin_booking(r, 1, "offer1", "DEL-BOM", "2026-12-01", "o4", "1.1.1.1", 1)
    assert e.value.code == "busy"


def test_maintenance_and_old_versions_stop_new_bookings(cloud):
    lic, r = booker(cloud, 5)
    cloud.set_setting("maintenance", True, "boss")
    with pytest.raises(CloudError) as e:
        cloud.begin_booking(r, 1, "offer1", "DEL-BOM", "2026-12-01", "m", "1.1.1.1", 1)
    assert e.value.code == "maintenance" and e.value.status == 503
    cloud.set_setting("maintenance", False, "boss")
    cloud.set_setting("min_build", 5, "boss")
    with pytest.raises(CloudError) as e:
        cloud.begin_booking(r, 1, "offer1", "DEL-BOM", "2026-12-01", "m", "1.1.1.1", 4)
    assert e.value.code == "update_required" and e.value.status == 426
    assert cloud.begin_booking(r, 1, "offer1", "DEL-BOM", "2026-12-01", "m2", "1.1.1.1", 5)["charged"] == 1
    assert row(cloud, lic["id"])["credits"] == 4


def test_an_offer_an_admin_switched_off_cannot_be_booked_and_the_code_can_change(cloud):
    lic, r = booker(cloud, 5)
    offers = cloud.setting("offers")
    offers["offer1"]["enabled"] = False
    offers["offer2"]["code"] = "NEWCODE9"
    cloud.set_setting("offers", offers, "boss")
    with pytest.raises(CloudError) as e:
        cloud.begin_booking(r, 1, "offer1", "DEL-BOM", "2026-12-01", "x", "1.1.1.1", 1)
    assert e.value.code == "offer_off"
    assert cloud.begin_booking(r, 1, "offer2", "DEL-BOM", "2026-12-01", "y", "1.1.1.1", 1)["promo"] == "NEWCODE9"


# ── finishing: success keeps the charge, failure gives it back (within limits) ──────────────────────────────────────
def test_a_failed_booking_gives_the_credits_back_once(cloud):
    lic, r = booker(cloud, 5)
    b = cloud.begin_booking(r, 2, "offer1", "DEL-BOM", "2026-12-01", "f", "1.1.1.1", 1)
    out = cloud.finish_booking(r, b["booking_id"], "failed", None, "IndiGo had no seats", "1.1.1.1")
    assert out["refunded"] and out["credits"] == 5
    again = cloud.finish_booking(r, b["booking_id"], "failed", None, "", "1.1.1.1")
    assert again["repeat"] and row(cloud, lic["id"])["credits"] == 5          # reporting it again never refunds twice


def test_a_successful_booking_keeps_its_charge_and_cannot_be_turned_into_a_refund(cloud):
    lic, r = booker(cloud, 5)
    b = cloud.begin_booking(r, 1, "offer1", "DEL-BOM", "2026-12-01", "s", "1.1.1.1", 1)
    assert cloud.finish_booking(r, b["booking_id"], "done", "abc123", "", "1.1.1.1")["status"] == "done"
    out = cloud.finish_booking(r, b["booking_id"], "failed", None, "oops, it failed after all", "1.1.1.1")
    assert out["status"] == "done" and row(cloud, lic["id"])["credits"] == 4
    assert cloud.licence_detail(lic["id"])["bookings_list"][0]["pnr"] == "ABC123"


def test_one_user_cannot_report_on_or_refund_another_users_booking(cloud):
    _l1, r1 = booker(cloud, 5)
    lic2 = cloud.create_licence("Other", credits=5)
    activate(cloud, lic2["key"])
    r2 = row(cloud, lic2["id"])
    b = cloud.begin_booking(r1, 1, "offer1", "DEL-BOM", "2026-12-01", "x", "1.1.1.1", 1)
    with pytest.raises(CloudError) as e:
        cloud.finish_booking(r2, b["booking_id"], "failed", None, "", "1.1.1.1")
    assert e.value.code == "not_found"


def test_a_client_that_reports_failure_over_and_over_loses_the_automatic_refund(cloud):
    lic, r = booker(cloud, 100)
    cloud.set_setting("max_open", 50, "boss")
    denied = 0
    for i in range(20):
        b = cloud.begin_booking(r, 1, "offer1", "DEL-BOM", "2026-12-01", f"w{i}", "1.1.1.1", 1)
        out = cloud.finish_booking(r, b["booking_id"], "failed", None, "failed", "1.1.1.1")
        denied += 0 if out["refunded"] else 1
    assert denied >= 5 and row(cloud, lic["id"])["flagged"] == 1
    assert any(e["kind"] == "refund_denied" for e in cloud.events(level="alert"))
    unpaid = cloud.db.one("SELECT id FROM bookings WHERE refunded=0 AND status='failed' LIMIT 1")["id"]
    before = row(cloud, lic["id"])["credits"]
    cloud.admin_refund_booking(unpaid, "boss")                              # an admin can still decide to give it back
    assert row(cloud, lic["id"])["credits"] == before + 1
    with pytest.raises(CloudError):
        cloud.admin_refund_booking(unpaid, "boss")


def test_an_honest_user_with_a_mix_of_results_keeps_getting_refunds(cloud):
    lic, r = booker(cloud, 100)
    cloud.set_setting("max_open", 50, "boss")
    for i in range(15):                                   # (a third fail: 5 refunds in a day, inside the daily limit of 6)
        b = cloud.begin_booking(r, 1, "offer1", "DEL-BOM", "2026-12-01", f"h{i}", "1.1.1.1", 1)
        out = cloud.finish_booking(r, b["booking_id"], "failed" if i % 3 == 0 else "done", "ABC123" if i % 3 else None, "no seats", "1.1.1.1")
        if i % 3 == 0:
            assert out["refunded"], i
    assert row(cloud, lic["id"])["flagged"] == 0


def test_a_booking_the_app_never_reported_is_marked_unconfirmed_and_can_still_be_reported_later(cloud):
    lic, r = booker(cloud, 5)
    b = cloud.begin_booking(r, 1, "offer1", "DEL-BOM", "2026-12-01", "c", "1.1.1.1", 1)
    cloud.now = lambda: __import__("time").time() + 2 * 3600
    assert cloud.expire_reservations() == 1
    assert cloud.open_bookings(r)[0]["status"] == "unconfirmed"
    out = cloud.finish_booking(r, b["booking_id"], "failed", None, "the app was closed", "1.1.1.1")
    assert out["refunded"] and row(cloud, lic["id"])["credits"] == 5


# ── releases ────────────────────────────────────────────────────────────────────────────────────────────────────────
def make_release(signer, build_no=1, files=None, **kw):
    files = files or {"app.py": b"print('hi')\n", "ext/core.js": b"// js\n"}
    data, key, manifest = bundle.build(files, build_no=build_no, version=f"1.0.{build_no}", channel="stable", notes="n", signing_key=signer._k, **kw)
    return data, crypto.b64e(key), manifest


def test_only_a_release_signed_by_the_developer_is_accepted(cloud, release_signer):
    data, key, _ = make_release(release_signer, 1)
    assert cloud.add_release(data, key, actor="boss")["build"] == 1
    fake, fkey, _ = make_release(crypto.Signer.generate(), 2)
    with pytest.raises(CloudError) as e:
        cloud.add_release(fake, fkey, actor="boss")
    assert e.value.code == "bad_bundle" and "signature" in e.value.message
    assert any(ev["kind"] == "release_refused" for ev in cloud.events(level="alert"))


def test_releases_cannot_be_replaced_or_go_backwards_and_the_key_must_open_them(cloud, release_signer):
    d1, k1, _ = make_release(release_signer, 5)
    cloud.add_release(d1, k1)
    with pytest.raises(CloudError) as e:
        cloud.add_release(d1, k1)
    assert e.value.code == "exists"
    d0, k0, _ = make_release(release_signer, 3)
    with pytest.raises(CloudError) as e:
        cloud.add_release(d0, k0)
    assert e.value.code == "old_build"
    d9, _k9, _ = make_release(release_signer, 9)
    with pytest.raises(CloudError) as e:
        cloud.add_release(d9, crypto.b64e(b"x" * 32))
    assert e.value.code == "bad_bundle"
    with pytest.raises(CloudError):
        cloud.add_release(b"not a zip", k1)


def test_a_device_is_offered_the_newest_live_release_in_its_rollout_group_only(cloud, release_signer):
    lic = cloud.create_licence("Rahul", credits=1)
    activate(cloud, lic["key"])
    r = row(cloud, lic["id"])
    for b in (1, 2, 3):
        d, k, _ = make_release(release_signer, b)
        cloud.add_release(d, k)
    assert cloud.update_for(r, 0)["available"] is False                    # nothing is live yet: drafts are never served
    cloud.edit_release(1, state="live")
    cloud.edit_release(2, state="live", mandatory=True)
    u = cloud.update_for(r, 1)
    assert u["available"] and u["build"] == 2 and u["mandatory"] is True
    assert cloud.update_for(r, 2)["available"] is False
    cloud.edit_release(3, state="live", rollout=1)                         # 1 % of devices
    inside = [cloud._bucket(f"dev{i}", 3) < 1 for i in range(2000)]
    assert 0 < sum(inside) < 60                                            # roughly one percent
    cloud.edit_release(3, state="withdrawn")
    assert cloud.update_for(r, 1)["build"] == 2
    with pytest.raises(CloudError):
        cloud.release_file(r, 3)                                           # a withdrawn release can no longer be downloaded


def test_a_release_key_is_delivered_to_the_asking_device_only(cloud, release_signer):
    lic = cloud.create_licence("Rahul", credits=1)
    activate(cloud, lic["key"])
    r = row(cloud, lic["id"])
    d, k, _ = make_release(release_signer, 1)
    cloud.add_release(d, k)
    with pytest.raises(CloudError):
        cloud.release_key(r, 1, "x" * 43)                                  # a draft has no key to give
    cloud.edit_release(1, state="live")
    priv, pub = crypto.new_ephemeral()
    wrapped = cloud.release_key(r, 1, pub)
    assert crypto.unwrap_with(priv, wrapped, f"release/1/{r['device_id']}".encode()) == crypto.b64d(k)
    lic2 = cloud.create_licence("Other", credits=1)
    activate(cloud, lic2["key"])
    other = row(cloud, lic2["id"])
    w2 = cloud.release_key(other, 1, pub)
    with pytest.raises(ValueError):
        crypto.unwrap_with(priv, w2, f"release/1/{r['device_id']}".encode())      # a key meant for another device is useless here
    with pytest.raises(CloudError):
        cloud.release_key(r, 1, "not a key")


def test_stats_and_search_find_what_an_admin_looks_for(cloud):
    a = cloud.create_licence("Anita Roy", credits=0, note="from Delhi")
    b = cloud.create_licence("Rahul", credits=7)
    activate(cloud, b["key"], info=dev_info("RAHUL-LAPTOP", "aa:bb:cc:dd:ee:ff"))
    cloud.set_status(a["id"], "blocked", "test", "boss")
    s = cloud.stats()
    assert (s["users"], s["active"], s["blocked"], s["no_credits"], s["credits_total"], s["unbound"]) == (2, 1, 1, 0, 7, 1)
    assert [x["label"] for x in cloud.list_licences("anita")["items"]] == ["Anita Roy"]
    assert [x["label"] for x in cloud.list_licences("RAHUL-LAP")["items"]] == ["Rahul"]               # even by the computer's name
    assert [x["label"] for x in cloud.list_licences(b["key"][-4:])["items"]] == ["Rahul"]
    assert cloud.list_licences("%")["total"] == 0 and cloud.list_licences("_")["total"] == 0           # wildcards are not wildcards
    assert [x["label"] for x in cloud.list_licences("", "blocked")["items"]] == ["Anita Roy"]
    assert cloud.list_licences("", "online")["total"] == 1
