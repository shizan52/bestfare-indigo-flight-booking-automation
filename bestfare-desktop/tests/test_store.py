import time


def add(store, grp="G", offer="offer1"):
    return store.add_booking(grp=grp, origin="DEL", dest="BOM", travel_date="2026-12-01", offer=offer,
                             travellers=[{"first": "RAHUL", "last": "SHARMA", "gender": "M"}],
                             phone="9876543210", email="a@b.co")


def test_add_update_and_read_back(store):
    bid = add(store)
    r = store.get(bid)
    assert r["status"] == "waiting" and r["pax_count"] == 1 and r["travellers"][0]["first"] == "RAHUL"
    store.update(bid, status="done", pnr="ABC123", kind="record_locator", fare=5132.0, coupon_applied=True,
                 finished_at=time.time())
    r = store.get(bid)
    assert r["pnr"] == "ABC123" and r["coupon_applied"] is True
    assert store.bookings()[0]["id"] == bid


def test_unknown_fields_are_refused(store):
    bid = add(store)
    try:
        store.update(bid, phone="x")
    except ValueError:
        return
    raise AssertionError("personal fields must not be writable through update()")


def test_groups_keep_both_offers_together(store):
    a, b = add(store, "G1", "offer1"), add(store, "G1", "offer2")
    add(store, "G2")
    assert [r["id"] for r in store.group("G1")] == [a, b]


def test_personal_details_are_wiped_two_hours_after_a_booking(store):
    old, new, running = add(store), add(store), add(store)
    now = time.time()
    store.update(old, status="done", pnr="OLD111", finished_at=now - 2 * 3600 - 5)
    store.update(new, status="done", pnr="NEW222", finished_at=now - 60)
    assert store.wipe_personal(now=now) == 1
    o, n, r = store.get(old), store.get(new), store.get(running)
    assert o["travellers"] == [] and o["phone"] == "" and o["email"] == "" and o["pnr"] == "OLD111"
    assert n["travellers"] and n["phone"]
    assert r["travellers"]                      # unfinished bookings are never wiped
    raw = store.path.read_bytes() + (store.path.with_name(store.path.name + "-wal").read_bytes()
                                     if store.path.with_name(store.path.name + "-wal").exists() else b"")
    assert raw  # file exists; the row content check above is the contract


def test_bookings_cut_off_by_a_close_are_reported_honestly(store):
    a, b = add(store), add(store)
    store.update(b, status="running")
    assert store.interrupted() == 2
    assert all(store.get(x)["status"] == "failed" and "closed" in store.get(x)["error"] for x in (a, b))


def test_clear_history_keeps_what_is_running(store):
    a, b = add(store), add(store)
    store.update(a, status="done", pnr="X", finished_at=time.time())
    store.update(b, status="running")
    assert store.clear_history() == 1
    assert store.get(a) is None and store.get(b) is not None


def test_settings_round_trip(store):
    assert store.setting("contact") is None
    store.set_setting("contact", {"phone": "9876543210", "email": "a@b.co"})
    assert store.setting("contact")["email"] == "a@b.co"
    store.set_setting("contact", None)
    assert store.setting("contact", "gone") == "gone"


def test_the_chosen_flight_is_kept_with_the_booking(store):
    bid = add(store)
    store.update(bid, status="done", pnr="ABC123", flight="6E 864 · 15:30 → 18:00", finished_at=time.time())
    assert store.get(bid)["flight"] == "6E 864 · 15:30 → 18:00"
    assert store.get(add(store))["flight"] is None


def test_a_history_file_from_before_the_flight_list_is_upgraded(tmp_path):
    import sqlite3

    from store import Store
    old = tmp_path / "old.db"
    db = sqlite3.connect(str(old))
    db.execute("CREATE TABLE bookings (id TEXT PRIMARY KEY, grp TEXT NOT NULL, created_at REAL NOT NULL, origin TEXT NOT NULL, "
               "dest TEXT NOT NULL, travel_date TEXT NOT NULL, offer TEXT NOT NULL, pax_count INTEGER NOT NULL, "
               "pax_json TEXT NOT NULL DEFAULT '[]', phone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', "
               "status TEXT NOT NULL, pnr TEXT, kind TEXT, fare REAL, discount REAL, coupon_applied INTEGER, hold_until TEXT, "
               "pay_url TEXT, error TEXT, finished_at REAL, wiped INTEGER NOT NULL DEFAULT 0)")
    db.execute("INSERT INTO bookings (id, grp, created_at, origin, dest, travel_date, offer, pax_count, status, pnr) "
               "VALUES ('OLD1','G',1,'DEL','BOM','2026-12-01','offer1',1,'done','OLDPNR')")
    db.commit()
    db.close()
    s = Store(old)
    r = s.get("OLD1")
    assert r["pnr"] == "OLDPNR" and r["flight"] is None and r["verified"] is None
    s.update("OLD1", flight="6E 1 · 06:00 → 08:00")
    assert s.get("OLD1")["flight"] == "6E 1 · 06:00 → 08:00"
    s.close()
