"""How the private Edge is started and looked after (no real Edge needed)."""
import asyncio
import socket

import pytest

import browser


def test_launch_args_are_the_trusted_recipe():
    args = browser.launch_args("msedge.exe", browser.paths.edge_profile(), 9333)
    joined = " ".join(args)
    assert "--remote-debugging-port=9333" in args
    assert "--remote-debugging-port=0" not in joined          # port 0 makes navigator.webdriver true (measured)
    assert "--enable-automation" not in joined and "--headless" not in joined
    feats = next(a for a in args if a.startswith("--disable-features="))
    assert "msImplicitSignin" in feats and "--disable-sync" in args
    assert "--disable-backgrounding-occluded-windows" in args
    assert any(a.startswith("--user-data-dir=") and a.endswith("edge-profile") for a in args)
    assert args[-1] == browser.HOME


@pytest.mark.parametrize("port", [0, 80, 70000])
def test_bad_ports_are_refused(port):
    with pytest.raises(ValueError):
        browser.launch_args("msedge.exe", browser.paths.edge_profile(), port)


def test_pick_port_skips_a_busy_one():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    busy = s.getsockname()[1]
    try:
        assert browser.pick_port(first=busy, span=5) != busy
        assert browser.pick_port(preferred=busy, first=busy, span=5) != busy
    finally:
        s.close()


@pytest.mark.parametrize("title,state", [
    ("Book Domestic and International Flights at Lowest Airfare", "ready"),
    ("goindigo.in", "loading"),
    ("", "loading"),
    ("Loading...", "loading"),
    ("Access Denied", "denied"),
])
def test_page_state(title, state):
    assert browser._page_state({"title": title, "url": "https://www.goindigo.in/"}) == state


def test_no_tab():
    assert browser._page_state(None) == "no-tab"


class FakeB:
    def __init__(self, pages):
        self._pages = pages
        self.closed = []

    async def pages(self):
        return [p for p in self._pages if p["id"] not in self.closed]

    async def close_page(self, pid):
        self.closed.append(pid)


def page(i, url, title="x"):
    return {"id": i, "url": url, "title": title, "type": "page"}


def test_tidy_closes_leftovers_but_never_the_home_tab():
    b = FakeB([page("h", "https://www.goindigo.in/"), page("p", "https://www.goindigo.in/payment.html?x"),
               page("n", "edge://newtab/"), page("o", "https://example.com/"), page("i", "https://www.goindigo.in/itinerary.html")])
    asyncio.run(browser.tidy(b))
    assert sorted(b.closed) == ["i", "n", "o", "p"]


def test_tidy_closes_nothing_without_a_home_tab():
    """Closing Edge's last tab makes Edge quit - so without the home tab nothing is touched."""
    b = FakeB([page("p", "https://www.goindigo.in/payment.html"), page("n", "edge://newtab/")])
    asyncio.run(browser.tidy(b))
    assert b.closed == []


def test_saved_port_file(tmp_path):
    e = browser.LocalEdge(profile=tmp_path / "edge-profile")
    assert e._endpoint_from_file() is None
    e.port_file.write_text("9345")
    assert e._endpoint_from_file() == "http://127.0.0.1:9345"
    e.port_file.write_text("0")
    assert e._endpoint_from_file() is None


def test_find_edge_returns_an_existing_file_or_none():
    p = browser.find_edge()
    assert p is None or p.lower().endswith("msedge.exe")


def test_the_quiet_window_lives_off_screen_and_the_normal_one_does_not():
    prof = browser.paths.edge_profile()
    quiet = browser.launch_args("msedge.exe", prof, 9333, quiet=True)
    assert "--window-position=-32000,-32000" in quiet and "--window-size=1280,860" in quiet and quiet[-1] == browser.HOME
    normal = browser.launch_args("msedge.exe", prof, 9333)
    assert not any(a.startswith("--window-position") for a in normal)
    for args in (quiet, normal):                                                    # quiet or not: still the trusted recipe
        assert "--remote-debugging-port=9333" in args and not any("headless" in a or "enable-automation" in a for a in args)


class GuardTab:
    def __init__(self, left, log):
        self.left, self.log = left, log

    async def send(self, method, params=None, timeout=30):
        self.log.append((method, params))
        if method == "Browser.getWindowForTarget":
            return {"windowId": 7, "bounds": {"left": self.left, "top": 0, "width": 800, "height": 600}}
        return {}

    async def close(self):
        pass


class GuardBrowser:
    """A browser that has its home tab, and after a moment a pop-up window (the bank's) appears."""
    sent: list = []
    polls = 0
    popup_left = 0

    def __init__(self, ep):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def pages(self):
        GuardBrowser.polls += 1
        home = {"id": "home", "url": "https://www.goindigo.in/"}
        return [home] if GuardBrowser.polls < 3 else [home, {"id": "popup", "url": "about:blank"}]

    async def open_tab(self, page):
        return GuardTab(GuardBrowser.popup_left, GuardBrowser.sent)


def run_guard(monkeypatch, popup_left):
    GuardBrowser.sent, GuardBrowser.polls, GuardBrowser.popup_left = [], 0, popup_left
    monkeypatch.setattr(browser, "CDPBrowser", GuardBrowser)

    async def go():
        try:
            await asyncio.wait_for(browser.guard_windows("http://x", every=0.01), 0.4)
        except asyncio.TimeoutError:
            pass
    asyncio.run(go())
    return [m for m, _p in GuardBrowser.sent], [p for m, p in GuardBrowser.sent if m == "Browser.setWindowBounds"]


def test_a_new_window_is_moved_out_of_sight_at_once(monkeypatch):
    methods, moves = run_guard(monkeypatch, popup_left=0)
    assert "Browser.getWindowForTarget" in methods and len(moves) == 1
    assert moves[0]["windowId"] == 7 and moves[0]["bounds"]["left"] == browser.OFFSCREEN[0] and moves[0]["bounds"]["top"] == browser.OFFSCREEN[1]


def test_a_window_that_is_already_out_of_sight_is_left_alone(monkeypatch):
    _methods, moves = run_guard(monkeypatch, popup_left=browser.OFFSCREEN[0])
    assert moves == []                                                              # (a new tab in the hidden window needs nothing)


def test_the_guard_never_touches_the_first_window(monkeypatch):
    methods, _moves = run_guard(monkeypatch, popup_left=0)
    assert methods.count("Browser.getWindowForTarget") == 1                         # only the pop-up was looked at, not the home tab


# ── a slow computer or a slow connection is waited for - and a dead Edge is noticed at once ───────────────────────────────────
class FakeProc:
    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


def edge_with(tmp_path):
    e = browser.LocalEdge(profile=tmp_path / "edge-profile")
    e.profile.mkdir(parents=True, exist_ok=True)
    e.port_file.write_text("9333", encoding="utf-8")
    return e


async def test_one_missed_answer_does_not_make_edge_dead(monkeypatch):
    """On a slow computer Edge may miss one answer; 'dead' means its window is closed and restarted - so it is asked twice."""
    answers = iter([False, True])

    class Ctx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def version(self):
            if not next(answers):
                await asyncio.sleep(10)
            return {}

    monkeypatch.setattr(browser, "CDPBrowser", lambda ep: Ctx())
    assert await browser.LocalEdge.alive("http://127.0.0.1:9333", tries=2, timeout=0.05) is True
    answers2 = iter([False, False])
    monkeypatch.setattr(Ctx, "version", lambda self: _slow(answers2))
    assert await browser.LocalEdge.alive("http://127.0.0.1:9333", tries=2, timeout=0.05) is False
    assert await browser.LocalEdge.alive(None) is False


async def _slow(it):
    next(it)
    await asyncio.sleep(10)


async def test_a_slow_start_is_waited_for_and_the_traveller_is_told(tmp_path, monkeypatch):
    e = edge_with(tmp_path)
    e._proc = FakeProc(None)                                                    # Edge is running, just slow
    seen = {"n": 0}

    async def alive(ep, tries=1, timeout=3.0):
        seen["n"] += 1
        return seen["n"] > 6                                                    # answers at the 7th look
    monkeypatch.setattr(browser.LocalEdge, "alive", staticmethod(alive))
    real = asyncio.sleep
    clock = {"t": 0.0}
    monkeypatch.setattr(browser.time, "monotonic", lambda: clock["t"])

    async def tick(s):
        clock["t"] += 3.0                                                       # every look is 3 s later: it takes ~18 s in all
        await real(0)
    monkeypatch.setattr(browser.asyncio, "sleep", tick)
    notes = []
    ep = await e._wait_up(90, notes.append)
    assert ep == "http://127.0.0.1:9333" and e.endpoint == ep
    assert notes == ["Getting ready — this is taking a little longer on this computer."]      # told once, in plain words


async def test_a_crashed_edge_is_not_waited_for(tmp_path, monkeypatch):
    e = edge_with(tmp_path)
    e._proc = FakeProc(1)                                                       # the process is already gone
    async def never(ep, tries=1, timeout=3.0):
        return False
    monkeypatch.setattr(browser.LocalEdge, "alive", staticmethod(never))
    real = asyncio.sleep
    clock = {"t": 0.0}
    monkeypatch.setattr(browser.time, "monotonic", lambda: clock["t"])

    async def tick(s):
        clock["t"] += 2.0
        await real(0)
    monkeypatch.setattr(browser.asyncio, "sleep", tick)
    assert await e._wait_up(90) is None
    assert clock["t"] < 20                                                      # gave up after a few seconds, not after 90


async def test_the_home_page_of_a_slow_connection_is_waited_for_and_the_traveller_is_told(tmp_path, monkeypatch):
    e = edge_with(tmp_path)
    states = {"n": 0}

    class Ctx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def pages(self):
            states["n"] += 1
            title = "Book Flights" if states["n"] > 30 else "goindigo.in"       # loads after ~30 looks
            return [{"id": "H", "url": "https://www.goindigo.in/", "title": title}]

    monkeypatch.setattr(browser, "CDPBrowser", lambda ep: Ctx())
    real = asyncio.sleep
    clock = {"t": 0.0}
    monkeypatch.setattr(browser.time, "monotonic", lambda: clock["t"])

    async def tick(s):
        clock["t"] += 1.0
        await real(0)
    monkeypatch.setattr(browser.asyncio, "sleep", tick)
    notes = []
    home = await e.wait_home("http://127.0.0.1:9333", note=notes.append)
    assert home["title"] == "Book Flights"                                      # the old 45 s limit would have given up first
    assert notes == ["IndiGo's page is loading slowly — please wait a little."]


# ── a booking always starts in a fresh browser, and none is left behind ───────────────────────────────────────────────
async def test_a_leftover_browser_of_ours_is_closed_not_reused(tmp_path, monkeypatch):
    e = edge_with(tmp_path)
    killed = []
    monkeypatch.setattr(e, "our_edge_pids", lambda: [4242] if not killed else [])
    monkeypatch.setattr(e, "stop_ours", lambda: killed.append(1) or 1)

    async def alive(*a, **k):
        raise AssertionError("a leftover must not even be asked")
    monkeypatch.setattr(browser.LocalEdge, "alive", staticmethod(alive))
    assert await e.connect() is None and killed == [1]


async def test_a_browser_started_by_this_run_is_reused_by_this_run(tmp_path, monkeypatch):
    e = edge_with(tmp_path)
    e._proc = object()                                                           # (this run started it)
    monkeypatch.setattr(e, "our_edge_pids", lambda: pytest.fail("this run's own browser is not a leftover"))

    async def alive(ep, *a, **k):
        return True
    monkeypatch.setattr(browser.LocalEdge, "alive", staticmethod(alive))
    assert await e.connect() == "http://127.0.0.1:9333"


async def test_after_closing_no_process_of_ours_may_remain(tmp_path, monkeypatch):
    e = edge_with(tmp_path)
    running = {"left": [11, 12]}
    killed = []
    monkeypatch.setattr(e, "our_edge_pids", lambda: list(running["left"]))
    monkeypatch.setattr(e, "stop_ours", lambda: killed.append(list(running["left"])) or running.update(left=[]) or 2)
    await e._gone(seconds=0.6)                                                   # they never leave by themselves: the rest is ended
    assert killed == [[11, 12]] and running["left"] == []
    killed.clear()
    await e._gone(seconds=0.6)                                                   # nothing left: nothing is done
    assert killed == []


# ── Edge's own helper pages are closed (they hold a big part of its memory; a booking never touches them) ──────────────
def target(i, typ, url):
    return {"id": i, "type": typ, "url": url, "title": ""}


class FakeTargets(FakeB):
    def __init__(self, targets):
        super().__init__([])
        self._targets = targets

    async def targets(self):
        return [t for t in self._targets if t["id"] not in self.closed]


def test_only_edges_own_helper_pages_count_as_helpers():
    assert browser.is_builtin_helper(target("a", "background_page", "chrome-extension://abc/_generated_background_page.html"))
    assert browser.is_builtin_helper(target("b", "service_worker", "chrome-extension://abc/sw.js"))
    assert not browser.is_builtin_helper(target("c", "page", "https://www.goindigo.in/"))
    assert not browser.is_builtin_helper(target("d", "page", "chrome-extension://abc/options.html"))        # an extension's page that somebody looks at
    assert not browser.is_builtin_helper(target("e", "service_worker", "https://www.goindigo.in/sw.js"))    # IndiGo's own worker: never
    assert not browser.is_builtin_helper(target("f", "iframe", "https://pay.example/"))
    assert not browser.is_builtin_helper(target("g", "background_page", "https://example.com/"))
    assert not browser.is_builtin_helper({"id": "h"})


def test_trim_closes_the_helpers_and_nothing_else():
    b = FakeTargets([target("bg", "background_page", "chrome-extension://abc/_generated_background_page.html"),
                     target("sw", "service_worker", "chrome-extension://def/sw.js"), target("home", "page", "https://www.goindigo.in/"),
                     target("opt", "page", "chrome-extension://abc/options.html"), target("iw", "service_worker", "https://www.goindigo.in/sw.js"),
                     target("pay", "iframe", "https://pay.example/")])
    assert asyncio.run(browser.trim(b)) == 2
    assert sorted(b.closed) == ["bg", "sw"]
    assert asyncio.run(browser.trim(b)) == 0                                     # (nothing left to close)


def test_trim_never_gets_in_the_way_of_a_booking():
    class Broken(FakeTargets):
        async def targets(self):
            raise OSError("Edge went away")
    assert asyncio.run(browser.trim(Broken([]))) == 0

    class OneRefuses(FakeTargets):
        async def close_page(self, pid):
            if pid == "bg1":
                raise OSError("refused")
            await super().close_page(pid)
    b = OneRefuses([target("bg1", "background_page", "chrome-extension://a/b.html"), target("bg2", "background_page", "chrome-extension://c/d.html")])
    assert asyncio.run(browser.trim(b)) == 1 and b.closed == ["bg2"]


async def test_the_first_seconds_after_a_start_are_watched_for_late_helpers(tmp_path, monkeypatch):
    """Edge's helper pages come up one by one over several seconds: the browser is looked at again and again for a while, then left alone."""
    b = FakeTargets([target("bg1", "background_page", "chrome-extension://a/b.html")])
    looked = []

    class Session:
        def __init__(self, ep):
            looked.append(ep)

        async def __aenter__(self):
            if len(looked) == 2:
                b._targets.append(target("bg2", "background_page", "chrome-extension://c/d.html"))      # one that started late
            return b

        async def __aexit__(self, *exc):
            return False
    monkeypatch.setattr(browser, "CDPBrowser", Session)
    e = edge_with(tmp_path)
    await e._trim_a_while("http://127.0.0.1:9333", seconds=0.5, every=0.1)
    assert sorted(b.closed) == ["bg1", "bg2"] and 3 <= len(looked) <= 6          # looked several times, then stopped by itself
