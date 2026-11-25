"""The whole chain, end to end, with nothing shortcut: the ADMIN works through the panel's real HTTP API (login, CSRF, cookies - exactly what the
panel's page does), the APP is the real app code (licence, gate, engine) talking to the same real server over real HTTP."""
import json
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

import pytest

import licence as lic
from cloud import CloudClient, CloudError
from device import DeviceKey, Vault
from livecloud import OWNER_PW


class Panel:
    """What the admin panel's page does: sign in, remember the cookie, send the CSRF header with every change."""

    def __init__(self, base: str):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        self.csrf = ""

    def call(self, method: str, path: str, data=None):
        req = urllib.request.Request(self.base + "/admin/api" + path, method=method, data=json.dumps(data if data is not None else {}).encode() if method != "GET" else None,
                                     headers={"Content-Type": "application/json", "X-CSRF": self.csrf})
        try:
            with self.opener.open(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def login(self, user="boss", pw=OWNER_PW):
        st, j = self.call("POST", "/login", {"username": user, "password": pw})
        assert st == 200, j
        self.csrf = j["csrf"]
        return self


def app(live, tmp_path, name="a"):
    vault = Vault(tmp_path / f"v-{name}.bin")
    return lic.Licence(CloudClient(DeviceKey(vault), 1, base_url=live.url, server_public=live.public), vault, "t")


def test_the_admin_gives_a_key_and_credits_the_user_books_runs_out_gets_more_is_blocked_and_moved(live, tmp_path):
    panel = Panel(live.url).login()

    # the admin creates a user in the panel: a key and 2 credits
    st, made = panel.call("POST", "/licences", {"label": "Rahul Sharma", "credits": 2, "note": "NCC officer"})
    assert st == 200 and made["key"].startswith("BF-")
    lid = made["id"]

    # the user types the key into the app's login window
    L = app(live, tmp_path)
    L.activate(made["key"])
    assert L.state == lic.OK and L.account.credits == 2

    # the panel shows the computer that took the key
    st, d = panel.call("GET", f"/licences/{lid}")
    assert d["licence"]["bound"] and d["licence"]["device"]["name"] and d["licence"]["status"] == "active"

    # a booking of 2 travellers takes both credits - and only then is the offer code handed out
    t = L.begin(2, "offer1", "DEL-BOM", "2026-12-01", "chain-0001")
    assert t.promo and t.credits == 0
    st, d = panel.call("GET", f"/licences/{lid}")
    assert d["licence"]["credits"] == 0 and d["licence"]["ledger"][0]["kind"] == "booking" and d["licence"]["bookings_list"][0]["status"] == "reserved"

    # no credits: the user cannot book at all
    with pytest.raises(CloudError) as e:
        L.begin(1, "offer1", "DEL-BOM", "2026-12-01", "chain-0002")
    assert e.value.code == "no_credits"

    # the booking succeeded
    L.finish(t.booking_id, "done", "ABC123", "")
    st, d = panel.call("GET", f"/licences/{lid}")
    assert d["licence"]["bookings_list"][0]["status"] == "done" and d["licence"]["bookings_list"][0]["pnr"] == "ABC123"

    # the admin adds 3 credits in the panel - the user has them at once
    st, j = panel.call("POST", f"/licences/{lid}/credits", {"delta": 3, "note": "bought 3"})
    assert st == 200 and j["credits"] == 3
    L.refresh()
    assert L.account.credits == 3
    t2 = L.begin(1, "offer2", "BLR-DEL", "2026-12-05", "chain-0003")
    assert t2.promo and t2.credits == 2 and t2.promo != t.promo                      # (a different offer, a different code)

    # the admin takes one back
    st, j = panel.call("POST", f"/licences/{lid}/credits", {"delta": -1})
    assert j["credits"] == 1
    st, j = panel.call("POST", f"/licences/{lid}/credits", {"delta": -5})
    assert st == 400 and j["code"] == "no_credits"                                   # cannot take away more than the user has

    # the admin blocks: the app stops at its next check, and booking is refused with plain words
    st, _ = panel.call("POST", f"/licences/{lid}/block", {"reason": "shared the key"})
    assert st == 200
    assert L.refresh() in (lic.BLOCKED, lic.NEW) or not L.usable
    with pytest.raises(CloudError):
        L.begin(1, "offer1", "DEL-BOM", "2026-12-01", "chain-0004")
    assert not L.usable
    L2 = app(live, tmp_path, "restart")                                               # a restart does not help either
    L2.client.device = L.client.device
    assert L2.resume() in (lic.BLOCKED, lic.NEW, lic.OFFLINE) or not L2.usable

    # unblocked: the same computer works again after signing in (the old session ended with the block)
    panel.call("POST", f"/licences/{lid}/unblock")
    assert L.refresh() == lic.OK and L.usable and L.account.credits == 1                       # unblocked: it just works again, nothing to type

    # a second computer with the same key is refused, flagged, and shown in the panel
    other = app(live, tmp_path, "other")
    with pytest.raises(CloudError) as e:
        other.activate(made["key"])
    assert e.value.code == "device_mismatch"
    st, d = panel.call("GET", f"/licences/{lid}")
    assert d["licence"]["flagged"] and "second computer" in d["licence"]["flag_reason"]
    st, ev = panel.call("GET", "/events?level=alert")
    assert any(x["kind"] == "device_refused" for x in ev["events"])

    # the admin moves the key to the new computer: the old one stops at once, the new one works
    panel.call("POST", f"/licences/{lid}/reset-device")
    assert L.refresh() == lic.NEW and not L.usable and "type your key again" in L.message      # the old computer is stopped - and cannot quietly take the key back
    assert L.resume() == lic.NEW and not L.usable                                              # not even after a restart
    other.activate(made["key"])
    assert other.usable and other.account.credits == 1
    with pytest.raises(CloudError) as e:                                                       # and now the OLD computer is the one that is refused
        L.activate(made["key"])
    assert e.value.code == "device_mismatch"

    # a leaked key: regenerate - the old one is dead, the new one works on the same computer after signing in again
    st, j = panel.call("POST", f"/licences/{lid}/regenerate-key")
    new_key = j["key"]
    assert new_key != made["key"]
    third = app(live, tmp_path, "third")
    with pytest.raises(CloudError) as e:
        third.activate(made["key"])
    assert e.value.code == "bad_key"
    third.activate(new_key)
    assert third.usable


def test_the_panels_settings_reach_the_app(live, tmp_path):
    panel = Panel(live.url).login()
    st, made = panel.call("POST", "/licences", {"label": "Anita Roy", "credits": 5})
    L = app(live, tmp_path)
    L.activate(made["key"])
    assert L.account.offers["offer1"]["enabled"] and L.account.broadcast == ""

    st, _ = panel.call("PUT", "/settings", {"broadcast": "IndiGo changed the payment page - an update is coming"})
    assert st == 200
    L.refresh()
    assert "update is coming" in L.account.broadcast                                  # (the message every user sees)

    panel.call("PUT", "/settings", {"offers": {"offer1": {"code": "NEWCODE9", "title": "Promotional fare A", "who": "Defence", "enabled": False},
                                               "offer2": {"code": "PROMO2", "title": "Promotional fare B", "who": "60+", "enabled": True}}})
    L.refresh()
    assert not L.account.offers["offer1"]["enabled"]
    with pytest.raises(CloudError) as e:
        L.begin(1, "offer1", "DEL-BOM", "2026-12-01", "chain-off-1")
    assert e.value.code == "offer_off" and L.account.credits == 5                    # a switched-off offer costs nothing

    panel.call("PUT", "/settings", {"offers": {"offer1": {"code": "NEWCODE9", "title": "Promotional fare A", "who": "Defence", "enabled": True},
                                               "offer2": {"code": "PROMO2", "title": "Promotional fare B", "who": "60+", "enabled": True}}})
    t = L.begin(1, "offer1", "DEL-BOM", "2026-12-01", "chain-new-1")
    assert t.promo == "NEWCODE9"                                                      # a new code from IndiGo: changed in the panel, no app update needed

    panel.call("PUT", "/settings", {"maintenance": True, "maintenance_msg": "Back in 30 minutes"})
    with pytest.raises(CloudError) as e:
        L.begin(1, "offer2", "DEL-BOM", "2026-12-02", "chain-maint-1")
    assert e.value.code == "maintenance" and "30 minutes" in e.value.message and L.account.credits == 4


def test_the_panel_will_not_let_a_stranger_or_a_plain_admin_do_the_owners_things(live):
    stranger = Panel(live.url)
    st, j = stranger.call("GET", "/licences")
    assert st == 401
    st, j = stranger.call("POST", "/login", {"username": "boss", "password": "not-the-password-1A"})
    assert st == 401
    owner = Panel(live.url).login()
    st, j = owner.call("POST", "/admins", {"username": "helper", "role": "admin"})
    helper = Panel(live.url).login("helper", j["password"])
    assert helper.call("GET", "/licences")[0] == 200                                  # an admin manages users ...
    assert helper.call("GET", "/admins")[0] == 403                                    # ... but not other admins
    assert helper.call("POST", "/admins", {"username": "x" * 5, "role": "owner"})[0] == 403


def test_the_admin_chooses_a_users_offers_and_the_app_shows_only_those_as_offer_1_or_2(live, tmp_path, qapp, store):
    from fakes import FakeEngine
    from main_window import MainWindow
    panel = Panel(live.url).login()
    st, made = panel.call("POST", "/licences", {"label": "Meera", "credits": 5, "offers": ["offer2"]})
    assert st == 200 and made["offers"] == ["offer2"]
    L = app(live, tmp_path)
    L.activate(made["key"])
    assert L.account.offer_keys() == ["offer2"]
    text = json.dumps(L.account.offers)
    assert "Senior" not in text and "Defence" not in text and "title" not in text                  # the app is never told what an offer is

    w = MainWindow(store, lambda emit: FakeEngine(store, emit), licence=L)
    assert w.book.tiles["offer1"].isHidden() and w.book.tiles["offer2"].text() == "Offer 2" and w.book.tiles["offer2"].isChecked()

    # the admin adds the other offer: the next check of the key brings it, in every tab
    panel.call("PATCH", f"/licences/{made['id']}", {"offers": ["offer1", "offer2"]})
    L.refresh()
    w._refresh_account()
    n = w.tabs.add()
    assert all(not p.tiles["offer1"].isHidden() and not p.tiles["offer2"].isHidden() for p in w.tabs.pages())
    assert [t.text() for t in n.tiles.values()] == ["Offer 1", "Offer 2"]

    # a modified app that asks for what it may not use gets a plain refusal and loses no credit
    panel.call("PATCH", f"/licences/{made['id']}", {"offers": ["offer1"]})
    with pytest.raises(CloudError) as e:
        L.begin(1, "offer2", "DEL-BOM", "2026-12-01", "offer-chain-1")
    assert e.value.code == "offer_not_allowed" and e.value.message == "Offer 2 is not available for your key."
    st, d = panel.call("GET", f"/licences/{made['id']}")
    assert d["licence"]["credits"] == 5 and d["licence"]["offers"] == ["offer1"]
    w.engine.busy = False
    w.close()
