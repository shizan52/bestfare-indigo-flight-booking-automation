import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import paths  # noqa: E402

paths.use_engine()


@pytest.fixture(autouse=True)
def dev_offers(monkeypatch):
    """The development gate (no server): made-up codes, so no real offer code appears in any test."""
    monkeypatch.setenv("BESTFARE_DEV_OFFERS", '{"offer1": "TESTCODE1", "offer2": "TESTCODE2"}')


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Every test gets its own data folder (history, settings) - the real one is never touched."""
    monkeypatch.setenv("BESTFARE_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture(autouse=True)
def _pace_isolated(monkeypatch):
    """IndiGo's slow-down state is class-level: each test starts clean and leaves nothing behind."""
    import indigo_client
    monkeypatch.setattr(indigo_client.Pace, "strikes", 0)
    monkeypatch.setattr(indigo_client.Pace, "cooldown_until", 0.0)
    monkeypatch.setattr(indigo_client.Pace, "on_trip", None)
    monkeypatch.setattr(indigo_client.Pace, "lockout", True)             # (the app's engine switches the lock-out off for itself; a test must not leak that)


@pytest.fixture()
def store(home):
    from store import Store
    s = Store(paths.db_path())
    yield s
    s.close()


@pytest.fixture(scope="session")
def qapp():
    from PyQt6.QtWidgets import QApplication
    import theme
    app = QApplication.instance() or QApplication([])
    theme.load_fonts()
    app.setStyleSheet(theme.qss())
    return app


# ── a real cloud server for the licence tests ───────────────────────────────────────────────────────────────────────
@pytest.fixture()
def live(tmp_path):
    from livecloud import LiveServer
    srv = LiveServer(tmp_path / "cloud")
    yield srv
    srv.stop()
