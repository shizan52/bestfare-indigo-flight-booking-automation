"""What lands in the log file a curious person could open: only the customer-facing story - nothing about how the work is done."""
import logging

import pytest

import app


@pytest.fixture()
def clean_root():
    root = logging.getLogger()
    before = list(root.handlers)
    yield root
    for h in list(root.handlers):
        if h not in before:
            h.close()
            root.removeHandler(h)


def written(home):
    p = home / "logs" / "bestfare.log"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def flush(root):
    for h in root.handlers:
        h.flush()


TECHNICAL = [("indigo_client", "indigo token -> 200 (300 ms)"), ("indigo_client", "indigo search -> 200 (900 ms)"),
             ("psp_flow", "the gateway redrew itself after HDFC Bank was chosen"), ("engine", "booking X failed: IndiGo refused"),
             ("browser", "started Edge on 127.0.0.1:9333 (profile C:/x)"), ("extension", "extension rules unavailable"),
             ("cdp", "Runtime.evaluate"), ("asyncio", "something")]


def test_normal_mode_keeps_only_the_customer_story_in_the_log_file(home, clean_root):
    app.setup_logging(debug=False)
    for name, msg in TECHNICAL:
        logging.getLogger(name).warning(msg)
    logging.getLogger("bestfare.activity").info("Finding the best fare")
    logging.getLogger("app").info("Best Fare started")
    flush(clean_root)
    text = written(home)
    assert "Finding the best fare" in text and "Best Fare started" in text
    for _name, msg in TECHNICAL:
        assert msg not in text
    low = text.lower()
    for leak in ("token", "edge", "gateway", "hdfc", "127.0.0.1", "cdp", "extension", "indigo_client", "psp_flow"):
        assert leak not in low


def test_debug_mode_keeps_everything(home, clean_root):
    app.setup_logging(debug=True)
    for name, msg in TECHNICAL[:3]:
        logging.getLogger(name).warning(msg)
    flush(clean_root)
    text = written(home)
    assert "indigo token -> 200" in text and "HDFC Bank" in text


def test_debug_is_switched_on_by_a_flag_an_environment_variable_or_a_file(home, monkeypatch):
    monkeypatch.delenv("BESTFARE_DEBUG", raising=False)
    assert app.debug_wanted(False) is False
    assert app.debug_wanted(True) is True
    monkeypatch.setenv("BESTFARE_DEBUG", "1")
    assert app.debug_wanted(False) is True
    monkeypatch.delenv("BESTFARE_DEBUG")
    (home / "debug.flag").write_text("on")
    assert app.debug_wanted(False) is True
    (home / "debug.flag").unlink()
    assert app.debug_wanted(False) is False
