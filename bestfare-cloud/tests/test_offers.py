"""Which offers a key may use: the admin chooses when the key is made (and can change it), the server enforces it, and the user only ever
sees "Offer 1" / "Offer 2" - never what an offer is or what its code is."""
import json

import pytest

from bfcloud.service import CloudError
from conftest import Device  # noqa: F401
from test_service import activate, row  # noqa: F401


def begin(cloud, r, offer, ref):
    return cloud.begin_booking(r, 1, offer, "DEL-BOM", "2026-12-01", ref, "1.1.1.1", 1)


def test_a_key_made_without_a_choice_may_use_every_offer(cloud):
    lic = cloud.create_licence("Rahul", credits=3, actor="t")
    assert lic["offers"] == ["offer1", "offer2"] and cloud.licence_detail(lic["id"])["offers"] == ["offer1", "offer2"]


def test_the_admin_picks_the_offers_and_the_server_refuses_the_other(cloud):
    lic = cloud.create_licence("Only Defence", credits=5, actor="t", offers=["offer1"])
    activate(cloud, lic["key"])
    r = row(cloud, lic["id"])
    assert begin(cloud, r, "offer1", "a1")["promo"]
    with pytest.raises(CloudError) as e:
        begin(cloud, r, "offer2", "a2")
    assert e.value.code == "offer_not_allowed" and e.value.status == 403 and e.value.message == "Offer 2 is not available for your key."
    assert row(cloud, lic["id"])["credits"] == 4 and cloud.events(kind="offer_refused")           # nothing was charged, the admin can see the try


def test_the_user_is_told_only_the_offers_they_may_use_and_never_what_they_are(cloud):
    lic = cloud.create_licence("Meera", credits=5, actor="t", offers=["offer2"])
    activate(cloud, lic["key"])
    acc = cloud.account(row(cloud, lic["id"]))
    assert list(acc["offers"]) == ["offer2"] and acc["offers"]["offer2"] == {"enabled": True}
    text = json.dumps(acc)
    for word in ("Defence", "Senior", "NCC", "6ETST001", "PROMO2", "title", "who"):
        assert word not in text, word


def test_a_switched_off_offer_is_named_like_the_user_knows_it(cloud):
    lic = cloud.create_licence("Rahul", credits=5, actor="t")
    activate(cloud, lic["key"])
    offers = cloud.setting("offers")
    offers["offer1"]["enabled"] = False
    cloud.set_setting("offers", offers, "t")
    with pytest.raises(CloudError) as e:
        begin(cloud, row(cloud, lic["id"]), "offer1", "b1")
    assert e.value.code == "offer_off" and e.value.message == "Offer 1 is not available right now." and "Defence" not in e.value.message


def test_the_admin_can_change_a_users_offers_later_and_it_takes_effect_at_once(cloud):
    lic = cloud.create_licence("Rahul", credits=5, actor="t", offers=["offer1"])
    activate(cloud, lic["key"])
    cloud.update_licence(lic["id"], offers=["offer1", "offer2"], actor="boss")
    assert begin(cloud, row(cloud, lic["id"]), "offer2", "c1")["promo"]
    cloud.update_licence(lic["id"], offers=["offer2"], actor="boss")
    with pytest.raises(CloudError):
        begin(cloud, row(cloud, lic["id"]), "offer1", "c2")
    assert cloud.events(kind="offers_changed")


@pytest.mark.parametrize("bad", [[], ["offer3"], ["offer1", "nope"], "offer1", [1], {"offer1": 1}, ["OFFER1"]])
def test_only_real_offers_and_at_least_one_are_accepted(cloud, bad):
    with pytest.raises(CloudError) as e:
        cloud.create_licence("Bad", credits=1, actor="t", offers=bad)
    assert e.value.code == "bad_offers"
    lic = cloud.create_licence("Good", credits=1, actor="t")
    with pytest.raises(CloudError):
        cloud.update_licence(lic["id"], offers=bad, actor="t")
    assert cloud.licence_detail(lic["id"])["offers"] == ["offer1", "offer2"]


def test_the_list_can_be_filtered_by_offer_for_the_admin(cloud):
    a = cloud.create_licence("A", credits=1, actor="t", offers=["offer1"])
    b = cloud.create_licence("B", credits=1, actor="t", offers=["offer2"])
    c = cloud.create_licence("C", credits=1, actor="t")
    ids = lambda flt: sorted(x["id"] for x in cloud.list_licences("", flt)["items"])  # noqa: E731
    assert ids("offer1") == sorted([a["id"], c["id"]]) and ids("offer2") == sorted([b["id"], c["id"]]) and ids("all") == sorted([a["id"], b["id"], c["id"]])
    assert ids("offer3") == sorted([a["id"], b["id"], c["id"]])                              # (an unknown filter is no filter, never SQL)


async def test_through_the_panel_the_admin_chooses_and_changes_the_offers(server, admin, user, cloud):
    st, j = await admin.post("/licences", {"label": "Meera", "credits": 4, "offers": ["offer2"]})
    assert st == 200 and j["offers"] == ["offer2"]
    st, d = await admin.get(f"/licences/{j['id']}")
    assert d["licence"]["offers"] == ["offer2"]
    st, _ = await admin.patch(f"/licences/{j['id']}", {"offers": ["offer1", "offer2"]})
    assert st == 200 and (await admin.get(f"/licences/{j['id']}"))[1]["licence"]["offers"] == ["offer1", "offer2"]
    st, bad = await admin.patch(f"/licences/{j['id']}", {"offers": []})
    assert st == 400 and bad["code"] == "bad_offers"
    st, bad = await admin.post("/licences", {"label": "X", "credits": 1, "offers": ["offer9"]})
    assert st == 400 and bad["code"] == "bad_offers"


async def test_an_app_that_asks_for_a_forbidden_offer_over_the_wire_gets_a_plain_refusal(server, admin, make_device, cloud):
    lic = cloud.create_licence("Only one", credits=3, actor="t", offers=["offer1"])
    dev = make_device(name="PC-OFFER", macs=("3c:52:82:00:00:31",))
    await dev.activate(lic["key"])
    st, j, _ = await dev.begin(1, offer="offer2")
    assert st == 403 and j["d"]["code"] == "offer_not_allowed" and "promo" not in json.dumps(j)
    st, j, _ = await dev.session()
    assert list(j["d"]["account"]["offers"]) == ["offer1"]
