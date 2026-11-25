"""The bot token must never appear in logs (httpx logs full request URLs by default)."""
import logging
import re

FAKE_TOKEN = "1234567890:" + "AbCdEf0123456789" * 2 + "xyz"      # same shape as a real token, but synthetic


def test_token_never_logged(cfg):
    import importlib
    import bot
    importlib.reload(bot)
    assert logging.getLogger("httpx").level >= logging.WARNING

    records = []

    class Cap(logging.Handler):
        def emit(self, record):
            records.append(record)
    h = Cap()
    h.addFilter(bot._RedactTokens())
    lg = logging.getLogger("leak-test")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    lg.info("HTTP Request: POST https://api.telegram.org/bot%s/getMe", FAKE_TOKEN)
    out = records[0].getMessage()
    assert FAKE_TOKEN not in out and "<BOT_TOKEN>" in out
    assert not re.search(r"\d{8,10}:[A-Za-z0-9_-]{30,}", out)
