import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    """Fresh config + empty database in a temp dir for every test."""
    monkeypatch.setenv("INDIGO_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST")
    monkeypatch.setenv("ADMIN_IDS", "1001")
    monkeypatch.setenv("BOOKING_CLEANUP_HOURS", "2")
    monkeypatch.setenv("EDGE_AUTOSTART", "false")              # tests never start or stop a real Edge
    import config
    importlib.reload(config)
    return config


@pytest.fixture()
async def db(cfg):
    import db as dbmod
    importlib.reload(dbmod)
    await dbmod.init_db()
    return dbmod


@pytest.fixture(autouse=True)
def _pace_isolated(monkeypatch):
    """IndiGo's block counter / cool-down is class-level state: every test starts clean and leaves nothing behind."""
    import indigo_client
    monkeypatch.setattr(indigo_client.Pace, "strikes", 0)
    monkeypatch.setattr(indigo_client.Pace, "cooldown_until", 0.0)
    monkeypatch.setattr(indigo_client.Pace, "on_trip", None)


@pytest.fixture()
async def tgenv(db, monkeypatch):
    """The real bot (all handlers) wired to a fake Telegram. env.person(uid) types and taps like a user."""
    from types import SimpleNamespace

    import tg
    wire = tg.install(monkeypatch)
    tg.reload_app_modules()
    import bot
    import worker
    bot.RATE_MAX = 10 ** 9                                     # the flood limiter has its own tests
    app = bot.build_app()
    await app.initialize()
    env = SimpleNamespace(app=app, wire=wire, bot=bot, worker=worker, db=db,
                          person=lambda uid, name="Test", username=None: tg.Person(app, wire, uid, name, username))
    yield env
    await worker.stop()
    await app.shutdown()


def booking(job_id="J1", offer="offer1", pax=1, **kw):
    return {"job_id": job_id, "offer": offer, "origin": "DEL", "destination": "BOM", "travel_date": "02-12-2026",
            "passengers": [{"first_name": f"P{i}", "last_name": "TEST", "gender": "M", "dob": "01-01-1950"} for i in range(pax)],
            "contact_email": "a@b.co", "contact_phone": "9000000001", "credit_cost": pax, "declared_eligible": True, **kw}


