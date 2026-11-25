import asyncio, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from aiohttp import web
from bfcloud import crypto
from bfcloud.service import Cloud
from bfcloud.web import App, Config
import tempfile
d = Path(tempfile.mkdtemp(prefix="bfdemo_"))
c = Cloud(d / "data", release_keys=[crypto.Signer.generate().public_b64])
c.create_admin("boss", "Correct-Horse-9-Battery", "owner", actor="demo")
def lic(label, credits, note="", online=False, dev=None, status=None, flagged=False):
    l = c.create_licence(label, credits, note, actor="boss")
    if dev:
        s = crypto.Signer.generate()
        c.activate(l["key"], s.public_b64, {"macs": [dev[1]], "name": dev[0], "os": "Windows 11 Home", "guid": "ab" * 16}, "103.%d.%d.7" % (l["id"], l["id"] * 3), 3)
        if not online:
            c.db.run("UPDATE licences SET last_seen=? WHERE id=?", (time.time() - 7200 * l["id"], l["id"]))
    if status:
        c.set_status(l["id"], "blocked", "shared the key with a friend", "boss")
    if flagged:
        c.db.run("UPDATE licences SET flagged=1, flag_reason='a second computer tried to use this key' WHERE id=?", (l["id"],))
    return l
a = lic("Rahul Sharma", 12, "NCC officer, Delhi", True, ("RAHUL-LAPTOP", "3c:52:82:aa:bb:cc"))
b = lic("Anita Roy", 0, "Senior citizen agent", False, ("ANITA-PC", "a4:5e:60:12:34:56"))
cc = lic("Mohan Das", 3, "", False, ("MOHAN-HP", "00:1a:2b:3c:4d:5e"), flagged=True)
lic("Meera Nair", 25, "Reseller", True, ("MEERA-DELL", "f0:de:f1:99:88:77"))
lic("Imran Khan", 5, "blocked for sharing", False, ("IMRAN-PC", "10:20:30:40:50:60"), status="blocked")
lic("New customer", 10, "key not sent yet")
c.create_licence("Locked to a PC", 4, "MAC locked", mac_lock="aa:bb:cc:00:11:22", actor="boss")
import random
now = time.time()
for lid in (1, 2, 3, 4):
    row = c.db.one("SELECT * FROM licences WHERE id=?", (lid,))
    for i in range(random.randint(3, 9)):
        try:
            c.set_setting("max_open", 50)
            bk = c.begin_booking(row, random.randint(1, 2), random.choice(["offer1", "offer2"]), random.choice(["DEL-BOM", "BLR-DEL", "CCU-DEL"]), "2026-12-%02d" % random.randint(1, 28), "r%d%d" % (lid, i), "103.1.1.1", 3)
            if random.random() < .8:
                c.finish_booking(row, bk["booking_id"], "done", "".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6)), "", "103.1.1.1")
            else:
                c.finish_booking(row, bk["booking_id"], "failed", None, "IndiGo had no seats", "103.1.1.1")
        except Exception as e:
            pass
        c.db.run("UPDATE bookings SET ts=? WHERE licence_id=? AND ts > ?", (now - random.randint(0, 6) * 86400 - random.randint(0, 40000), lid, now - 100))
c.event("device_refused", licence_id=3, ip="49.37.1.2", detail="another computer tried this key: OTHER-PC 11:22:33:44:55:66 (bound: MOHAN-HP 00:1a:2b:3c:4d:5e)", level="alert")
c.event("bad_key", ip="157.4.2.9", detail="unknown key …QX7M", level="warn")
port = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
print("demo data in", d)
web.run_app(App(c, Config(data_dir=c.dir, port=port)).app, host="127.0.0.1", port=port, print=lambda *_: None)
