# pyright: reportArgumentType=false
"""Regression for the live failure 'HDFC Bank option not shown under Net banking':
the gateway was still 'Generating QR' (UPI) and re-rendered after our click, undoing it. A person waits until the page
settles, and the bot must do the same - and must poll for the bank list instead of checking once."""
import pytest

import psp_flow

pytestmark = pytest.mark.asyncio


class FakeFrame:
    """Stands in for iframe_tools.Frame: scripted page text / search results, one entry per call."""

    def __init__(self, texts=(), found_after=None):
        self.texts, self.found_after = list(texts), found_after
        self.text_calls = self.find_calls = 0

    async def text(self, limit=900):
        i = min(self.text_calls, len(self.texts) - 1)
        self.text_calls += 1
        return self.texts[i]

    async def find(self, pattern):
        self.find_calls += 1
        return [{"text": "HDFC Bank"}] if self.found_after is not None and self.find_calls > self.found_after else []


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    real = psp_flow.asyncio.sleep

    async def tiny(_s):
        await real(0.001)
    monkeypatch.setattr(psp_flow.asyncio, "sleep", tiny)


async def test_settle_waits_until_the_qr_is_generated():
    fr = FakeFrame(["Recommended | UPI | Generating QR", "UPI | Generating QR", "UPI | View QR | Net banking"])
    await psp_flow._settle(fr, max_wait=5)
    assert fr.text_calls == 3                              # did not return while 'Generating QR' was on screen


async def test_settle_also_waits_when_the_qr_has_not_even_started_yet():
    """Live failure #2: the QR started late - the page showed neither 'Generating QR' nor 'View QR' for a moment."""
    fr = FakeFrame(["UPI | Net banking", "UPI | Net banking", "UPI | Generating QR", "UPI | View QR | Net banking"])
    await psp_flow._settle(fr, max_wait=5)
    assert fr.text_calls == 4


class RedrawingGateway:
    """Net banking collapses (choice lost) the first time HDFC is clicked - like the live QR redraw."""

    def __init__(self, redraws=1):
        self.expanded, self.redraws, self.clicks = False, redraws, []

    async def find(self, pattern):
        if pattern in (r"^HDFC Bank", r"popular banks"):
            return [{"text": pattern}] if self.expanded else []
        return [{"text": pattern}]

    async def click(self, pattern, nth=0):
        self.clicks.append(pattern)
        if pattern == r"net\s*banking":
            self.expanded = True
        elif pattern == r"^HDFC Bank" and self.redraws:
            self.redraws -= 1
            self.expanded = False                           # the widget redrew itself: choice gone
        return {"x": 1, "y": 1}


async def test_hdfc_is_chosen_again_when_the_gateway_redraws_itself():
    fr = RedrawingGateway(redraws=1)
    assert await psp_flow._choose_hdfc(fr) is True
    assert fr.clicks == [r"net\s*banking", r"^HDFC Bank", r"net\s*banking", r"^HDFC Bank"]


async def test_choosing_hdfc_gives_up_after_three_redraws():
    fr = RedrawingGateway(redraws=9)
    assert await psp_flow._choose_hdfc(fr) is False and fr.clicks.count(r"^HDFC Bank") == 3


async def test_settle_gives_up_instead_of_hanging_forever():
    fr = FakeFrame(["UPI | Generating QR"])
    await psp_flow._settle(fr, max_wait=0.05)              # a stuck QR must not freeze the booking
    assert fr.text_calls >= 1


async def test_poll_waits_for_the_bank_list_to_appear():
    fr = FakeFrame(found_after=3)
    assert await psp_flow._poll(fr, r"^HDFC Bank", timeout=5) is True
    assert fr.find_calls == 4


class FakePages:
    """Pages of the browser over time: one list per call to pages()."""

    def __init__(self, timeline):
        self.timeline, self.n = timeline, 0

    async def pages(self):
        snap = self.timeline[min(self.n, len(self.timeline) - 1)]
        self.n += 1
        return snap


async def test_the_bank_popup_is_noticed_even_while_it_is_still_blank():
    pay = {"id": "PAY", "url": "https://www.goindigo.in/payment.html"}
    blank = {"id": "POP", "url": "about:blank"}
    browser = FakePages([[pay], [pay], [pay, blank]])
    found = await psp_flow._wait_new_page(browser, {"PAY"}, timeout=5)
    assert found and found["id"] == "POP"                                  # the old code waited for an http address


async def test_waiting_for_the_bank_page_to_load(tmp_path):
    browser = FakePages([[{"id": "POP", "url": "about:blank"}], [{"id": "POP", "url": "https://api.juspay.in/x"}],
                         [{"id": "POP", "url": "https://netbanking.hdfcbank.com/login"}]])
    assert await psp_flow._wait_loaded(browser, "POP", timeout=5) is True
    gone = FakePages([[]])
    assert await psp_flow._wait_loaded(gone, "POP", timeout=5) is False      # closed by itself
    stuck = FakePages([[{"id": "POP", "url": "about:blank"}]])
    assert await psp_flow._wait_loaded(stuck, "POP", timeout=0.05) is False  # never loads: we go on anyway


async def test_no_popup_at_all_returns_nothing():
    browser = FakePages([[{"id": "PAY", "url": "https://www.goindigo.in/payment.html"}]])
    assert await psp_flow._wait_new_page(browser, {"PAY"}, timeout=0.05) is None


async def test_poll_returns_false_when_the_bank_never_shows():
    fr = FakeFrame(found_after=None)
    assert await psp_flow._poll(fr, r"^HDFC Bank", timeout=0.05) is False
