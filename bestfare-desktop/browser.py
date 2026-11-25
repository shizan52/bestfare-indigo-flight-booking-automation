"""The booking browser on the user's own computer: a private Microsoft Edge window that Best Fare starts and looks after.

Why Edge, started like this (the same recipe that books live on the server):
  * IndiGo sits behind Akamai Bot Manager. A normal Edge with NO automation flags, driven over a raw DevTools connection
    (cdp.py - never Playwright, never Runtime.enable), is treated as a person's browser. Frameworks that launch their own
    browser are not.
  * It uses its OWN profile folder (%LOCALAPPDATA%\\BestFare\\edge-profile), never the user's everyday Edge: the user's tabs,
    logins and history are never touched, and IndiGo's trust cookies are kept between runs.
  * Debugging listens on 127.0.0.1 only, on a FIXED port the app chooses (first free one from 9333) and remembers in
    edge-port.txt. NEVER --remote-debugging-port=0: measured on Edge 156, port 0 makes navigator.webdriver TRUE (that is
    how automation tools start Edge), while an explicit port keeps it false - exactly like the server's Edge.
  * --disable-backgrounding-occluded-windows: when the Best Fare window covers Edge, the IndiGo page must still count as
    visible (a hidden page that receives mouse movement looks wrong, and its timers would be slowed down).
  * No Microsoft-account sign-in and no sync in this profile (see launch_args).

What it repairs by itself (the desktop twin of the server's doctor.py):
  Edge closed or crashed -> start it again; an Edge of ours that stopped answering -> close it and start it properly;
  the IndiGo tab closed or navigated away -> open the home page again; tabs left over by an interrupted booking -> close
  them (but NEVER the last tab: closing Edge's last tab makes Edge quit).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

import aiohttp

import paths

paths.use_engine()
import speed  # noqa: E402  (shared engine)
from cdp import CDPBrowser  # noqa: E402  (shared engine)
from indigo_client import pick_trusted_tab  # noqa: E402

log = logging.getLogger(__name__)

HOME = "https://www.goindigo.in/"
LEFTOVER = ("payment", "itinerary", "order_id=", "juspay", "hdfc", "/book/", "about:blank", "edge://newtab",
            "chrome://newtab", "chrome-error://")
NO_WINDOW = 0x08000000                       # CREATE_NO_WINDOW: helper commands must not flash a console
DETACHED = 0x00000008 | 0x00000200           # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: Edge outlives a crash of ours


class BrowserProblem(Exception):
    """Something the user can understand and usually fix (the message is shown as it is)."""


class BrowserDenied(BrowserProblem):
    """IndiGo's home page itself answered 'Access Denied' (its guard did not like this start). Nothing is wrong with the computer: the engine rests a
    little, starts a fresh browser and tries again by itself - the traveller is not asked to wait or to do anything."""


Note = Optional[Callable[[str], None]]


def find_edge() -> Optional[str]:
    cands = []
    for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        root = os.environ.get(var)
        if root:
            cands.append(os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"))
    if os.name == "nt":
        try:
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as k:
                        cands.append(winreg.QueryValue(k, None))
                except OSError:
                    pass
        except ImportError:
            pass
    return next((c for c in cands if c and os.path.isfile(c)), None)


FIRST_PORT = 9333
START_WAIT_S = 90.0                          # how long a slow computer may take to start Edge (a fast one needs ~3 s and never waits)
HOME_WAIT_S = 90.0                           # ... and a slow connection to load IndiGo's home page


OFFSCREEN = (-32000, -32000)       # where the quiet window lives: on no monitor at all


def launch_args(edge: str, profile: Path, port: int, quiet: bool = False) -> list[str]:
    """The server's proven recipe (human-launched Edge, remote debugging on 127.0.0.1, own profile) plus only what a
    desktop app needs: no Microsoft-account sign-in/sync in this profile, and the IndiGo page stays 'visible' while
    other windows cover Edge. `quiet` puts the window off-screen (measured on Edge 156: still 'visible' and focused for the
    page, clicks arrive as trusted events, screenshots work) so the customer never sees a browser."""
    if not 1024 <= int(port) <= 65535:              # port 0 would make Edge announce itself as automated
        raise ValueError(f"bad debugging port {port}")
    # --disable-features=msImplicitSignin + --disable-sync: measured on Edge 156, a NEW profile otherwise signs itself
    # into the Windows Microsoft account and turns sync on (the user's history/passwords would flow into it).
    args = [edge, f"--remote-debugging-port={int(port)}", f"--user-data-dir={profile}", "--no-first-run",
            "--no-default-browser-check", "--disable-sync", "--disable-features=msImplicitSignin,CalculateNativeWinOcclusion",
            "--disable-backgrounding-occluded-windows", "--hide-crash-restore-bubble"]
    if quiet:
        args += [f"--window-position={OFFSCREEN[0]},{OFFSCREEN[1]}", "--window-size=1280,860"]
    return args + [HOME]


def port_free(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        if s.connect_ex(("127.0.0.1", port)) == 0:     # something already answers there
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port(preferred: Optional[int] = None, first: int = FIRST_PORT, span: int = 60) -> int:
    for p in ([preferred] if preferred else []) + list(range(first, first + span)):
        if p and port_free(p):
            return p
    raise BrowserProblem("Could not get started. Please restart the computer and try again.")


def _ps(script: str, timeout: float = 30) -> str:
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                           text=True, timeout=timeout, creationflags=NO_WINDOW if os.name == "nt" else 0)
        return (p.stdout or "").strip()
    except Exception as e:  # noqa: BLE001
        log.warning("powershell helper failed: %s", e)
        return ""


HELPER_TYPES = ("background_page", "service_worker")


def is_builtin_helper(target: dict) -> bool:
    """One of Edge's OWN helper pages: the background page (or service worker) of a built-in 'component' extension - the PDF viewer, shopping and collections
    helpers and the like. Never a web page, never something of IndiGo's: only targets of type background_page / service_worker whose address is chrome-extension://."""
    return target.get("type") in HELPER_TYPES and str(target.get("url") or "").startswith("chrome-extension://")


async def trim(b: CDPBrowser) -> int:
    """Close Edge's own helper pages (see is_builtin_helper). Each of them runs in a browser process of its own: on a fresh profile they held about 270 MB of the
    ~670 MB Edge uses - and a booking never touches them. Measured on Edge 156: what a web page can see of the browser (21 properties, incl. the 321 speech
    voices) is identical with and without them. (The launch flag that switches them off changes that - the voices drop to 3 - so the flag is NOT used.)"""
    closed = 0
    try:
        targets = await b.targets()
    except Exception:  # noqa: BLE001 - housekeeping must never get in the way of a booking
        return 0
    for t in targets:
        if is_builtin_helper(t):
            try:
                await b.close_page(t["id"])
                closed += 1
            except Exception:  # noqa: BLE001
                pass
    if closed:
        log.info("closed %d of Edge's own helper page(s) - saves memory, the booking does not use them", closed)
    return closed


async def tidy(b: CDPBrowser) -> list[str]:
    """Close tabs an interrupted booking left behind. Nothing is closed unless the IndiGo home tab exists."""
    actions: list[str] = []
    pages = await b.pages()
    home = pick_trusted_tab(pages)
    if home is None:
        return actions
    for p in pages:
        if p["id"] == home["id"]:
            continue
        url = p.get("url", "")
        if any(x in url for x in LEFTOVER) or (url.startswith("http") and "goindigo.in" not in url):
            try:
                await b.close_page(p["id"])
                actions.append(url[:60])
            except Exception:  # noqa: BLE001
                pass
    if actions:
        log.info("closed %d left-over tab(s)", len(actions))
    return actions


_BANNER_JS = r"""
const vis = el => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
  return r.width >= 20 && r.height >= 10 && st.visibility !== 'hidden' && st.display !== 'none' && +st.opacity !== 0; };
const b = [...document.querySelectorAll('button, a, [role=button]')]
  .find(el => /^\s*accept all\s*$/i.test(el.innerText || '') && vis(el));
if (!b) return null;
const r = b.getBoundingClientRect();
return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)};
"""


async def accept_cookie_banner(b: CDPBrowser) -> bool:
    """A new profile sees IndiGo's 'We respect your privacy' banner over the lower part of every page. A person clicks
    'Accept All' once; we do the same, with a real mouse movement. Afterwards the choice is remembered in the profile."""
    from human import HumanInput
    home = pick_trusted_tab(await b.pages())
    if home is None:
        return False
    tab = await b.open_tab(home)
    try:
        spot = await tab.evaluate_json(_BANNER_JS)
        if not spot:
            return False
        w, h = await tab.viewport()
        human = HumanInput(tab, w, h)
        await human.wander(2.5)
        await human.click(spot["x"], spot["y"])
        await asyncio.sleep(2.0)
        gone = not await tab.evaluate_json(_BANNER_JS)
        log.info("clicked 'Accept All' on IndiGo's cookie banner - %s", "closed" if gone else "still shown")
        return gone
    finally:
        await tab.close()


def _page_state(home: Optional[dict]) -> str:
    if home is None:
        return "no-tab"
    title = home.get("title") or ""
    if "Denied" in title:
        return "denied"
    # while the page loads its title is empty, "Loading…", or just the host name ("goindigo.in") - seen live
    if not title or title.startswith("Loading") or (" " not in title and "." in title) or title == home.get("url"):
        return "loading"
    return "ready"


class LocalEdge:
    def __init__(self, profile: Optional[Path] = None, edge_path: Optional[str] = None):
        self.profile = Path(profile or paths.edge_profile())
        self.edge_path = edge_path
        self.endpoint: Optional[str] = None
        self.launches = 0                      # how many times Edge was started (a fresh page needs a fresh warm-up)
        self._proc: Optional[subprocess.Popen] = None
        self._trim_task: Optional[asyncio.Future] = None
        self._trimmed_launch = 0               # the launch whose helper pages are being (or have been) trimmed

    # ── finding a running Edge of ours ─────────────────────────────────────────────────────────────────────
    @property
    def port_file(self) -> Path:
        """The port our Edge was started with (Edge itself writes no port file when the port is given explicitly)."""
        return self.profile.parent / "edge-port.txt"

    def _saved_port(self) -> Optional[int]:
        try:
            port = int(self.port_file.read_text(encoding="utf-8").split()[0])
            return port if 1024 <= port <= 65535 else None
        except (OSError, ValueError, IndexError):
            return None

    def _endpoint_from_file(self) -> Optional[str]:
        port = self._saved_port()
        return f"http://127.0.0.1:{port}" if port else None

    @staticmethod
    async def alive(endpoint: Optional[str], tries: int = 2, timeout: float = 6.0) -> bool:
        """Does Edge answer? One missed answer proves nothing on a slow computer (a busy disk, an antivirus scan): it is asked twice,
        with a patient limit, before anyone is called dead - because 'dead' means the window and its page get closed and restarted."""
        if not endpoint:
            return False
        for n in range(max(1, tries)):
            try:
                async with CDPBrowser(endpoint) as b:
                    await asyncio.wait_for(b.version(), timeout)
                return True
            except Exception:  # noqa: BLE001
                if n + 1 < tries:
                    await asyncio.sleep(0.5)
        return False

    async def connect(self) -> Optional[str]:
        """The endpoint of OUR running Edge, or None."""
        if self._proc is None and self.endpoint is None and await self._leftover():     # (an Edge of ours from an earlier run or a crash: never reused)
            return None
        for ep in (self.endpoint, self._endpoint_from_file()):
            if ep and await self.alive(ep):
                self.endpoint = ep
                return ep
        self.endpoint = None
        return None

    async def _leftover(self) -> bool:
        """The first time this run looks for its browser: if one of OURS is already open (the app was closed in a hurry, it crashed, an old version left it), it is closed -
        a booking must start in a browser that was opened for it, fresh, never in somebody's leftover. The user's own Edge is never touched."""
        loop = asyncio.get_running_loop()
        pids = await loop.run_in_executor(None, self.our_edge_pids)
        if not pids:
            return False
        await loop.run_in_executor(None, self.stop_ours)
        self.endpoint = None
        await asyncio.sleep(1.0)
        log.info("closed a leftover browser of ours so that this one starts fresh")
        return True

    def our_edge_pids(self) -> list[int]:
        if os.name != "nt":
            return []
        prof = str(self.profile).replace("'", "''")
        out = _ps("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | Where-Object { $_.CommandLine -and "
                  f"$_.CommandLine.Contains('{prof}') }} | ForEach-Object {{ $_.ProcessId }}")
        return [int(x) for x in out.split() if x.isdigit()]

    def stop_ours(self) -> int:
        """Force-close OUR Edge (only ever used when it no longer answers). The user's own Edge is never touched."""
        pids = self.our_edge_pids()
        for pid in pids:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                           creationflags=NO_WINDOW if os.name == "nt" else 0)
        if pids:
            log.warning("closed %d Edge process(es) of ours that had stopped answering", len(pids))
        return len(pids)

    # ── starting ───────────────────────────────────────────────────────────────────────────────────────────
    def _launch(self, quiet: bool = False) -> None:
        edge = self.edge_path or find_edge()
        if not edge:
            raise BrowserProblem("Microsoft Edge is needed on this computer. Please install it from microsoft.com/edge "
                                 "and try again.")
        port = pick_port(self._saved_port())
        self.port_file.write_text(str(port), encoding="utf-8")
        self.launches += 1
        self._proc = subprocess.Popen(launch_args(edge, self.profile, port, quiet), close_fds=True, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      creationflags=DETACHED if os.name == "nt" else 0)
        log.info("started Edge on 127.0.0.1:%d (profile %s)", port, self.profile)

    def _exited_early(self) -> bool:
        """Edge's own process ended already (a crash at start): waiting longer is pointless."""
        return self._proc is not None and self._proc.poll() is not None

    async def _wait_up(self, seconds: float, note: Note = None) -> Optional[str]:
        """Wait for Edge's debugging port. A slow computer (cold disk, antivirus) can need a minute: it is waited for, politely told
        so after a few seconds - and given up on only when Edge's process is gone or the generous limit is over."""
        start = time.monotonic()
        end = start + seconds
        told = False
        while time.monotonic() < end:
            ep = self._endpoint_from_file()
            if ep and await self.alive(ep, tries=1, timeout=3.0):
                self.endpoint = ep
                return ep
            if self._exited_early() and time.monotonic() - start > 6:
                return None
            if note and not told and time.monotonic() - start > 12:
                told = True
                note("Getting ready — this is taking a little longer on this computer.")
            await asyncio.sleep(0.5)
        return None

    async def start(self, note: Note = None, quiet: bool = False) -> str:
        if self.our_edge_pids():                         # ours is running but did not answer (twice, patiently): it is stuck
            self.stop_ours()
            await asyncio.sleep(2)
        self._launch(quiet)
        ep = await self._wait_up(START_WAIT_S, note)
        if ep is None:                                   # one clean second try
            self.stop_ours()
            await asyncio.sleep(2)
            self._launch(quiet)
            ep = await self._wait_up(START_WAIT_S, note)
        if ep is None:
            raise BrowserProblem("Could not get started. Please try again — if it keeps happening, restart the computer.")
        return ep

    async def wait_home(self, ep: str, seconds: float = HOME_WAIT_S, note: Note = None) -> dict:
        """Wait until the IndiGo home page has really loaded (its title is shown). On a slow connection that takes a while: the wait is
        generous (and longer still when the connection is known to be slow) and the traveller is told after a few seconds."""
        started = time.monotonic()
        end = started + speed.scaled(seconds)
        home: Optional[dict] = None
        told = False
        while time.monotonic() < end:
            async with CDPBrowser(ep) as b:
                home = pick_trusted_tab(await b.pages())
            st = _page_state(home)
            if st in ("ready", "denied"):
                return home or {}
            if note and not told and time.monotonic() - started > 15:
                told = True
                note("IndiGo's page is loading slowly — please wait a little.")
            await asyncio.sleep(0.8)
        return home or {}

    async def ensure(self, note: Note = None, quiet: bool = False) -> str:
        """A running Edge of ours with a loaded IndiGo home tab and no leftovers. Returns the DevTools endpoint.
        Started only when something needs it (a fare calendar, a booking) - never when the app opens."""
        ep = await self.connect()
        if ep is None:
            ep = await self.start(note, quiet)
        async with CDPBrowser(ep) as b:
            home = pick_trusted_tab(await b.pages())
            if home is None:
                await b.new_page(HOME)
        home = await self.wait_home(ep, note=note)
        st = _page_state(home)
        if st == "denied":
            raise BrowserDenied("IndiGo is not responding well right now. Nothing was booked or charged — please press Book again.")
        if st != "ready":
            raise BrowserProblem("IndiGo did not respond. Please check the internet connection and try again.")
        async with CDPBrowser(ep) as b:
            await tidy(b)
            await trim(b)
            try:
                await accept_cookie_banner(b)
            except Exception as e:  # noqa: BLE001 - a banner must never stop a booking
                log.info("cookie banner not handled: %s", e)
        if self.launches != self._trimmed_launch:                       # a new Edge: its helpers start one by one in the first seconds - look again for a while
            self._trimmed_launch = self.launches
            self._trim_task = asyncio.ensure_future(self._trim_a_while(ep))
        return ep

    async def _trim_a_while(self, ep: str, seconds: float = 45.0, every: float = 5.0) -> None:
        """Right after Edge starts its helper pages come up one by one over several seconds: close them as they appear, for the first minute only."""
        end = time.monotonic() + seconds
        try:
            while time.monotonic() < end:
                await asyncio.sleep(every)
                async with CDPBrowser(ep) as b:
                    await trim(b)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.debug("helper trimming ended", exc_info=True)

    async def status(self) -> dict:
        """Cheap, local-only look (no IndiGo traffic): closed | no-tab | loading | denied | ready."""
        ep = await self.connect()
        if ep is None:
            return {"state": "closed"}
        try:
            async with CDPBrowser(ep) as b:
                home = pick_trusted_tab(await b.pages())
        except Exception:  # noqa: BLE001
            return {"state": "closed"}
        return {"state": _page_state(home), "title": (home or {}).get("title", "")[:60], "endpoint": ep}

    async def show(self) -> bool:
        ep = await self.connect()
        if ep is None:
            return False
        async with CDPBrowser(ep) as b:
            home = pick_trusted_tab(await b.pages())
            if home:
                await b.activate_page(home["id"])
                return True
        return False

    async def close(self) -> bool:
        """Close our Edge politely (Browser.close). Falls back to ending its processes."""
        if self._trim_task is not None and not self._trim_task.done():
            self._trim_task.cancel()
        ep = await self.connect()
        if ep is None:
            return False
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as http:
                async with http.get(ep + "/json/version") as r:
                    ws_url = (await r.json()).get("webSocketDebuggerUrl")
                async with http.ws_connect(ws_url) as ws:
                    await ws.send_str(json.dumps({"id": 1, "method": "Browser.close"}))
                    try:
                        await asyncio.wait_for(ws.receive(), 3)
                    except asyncio.TimeoutError:
                        pass
        except Exception:  # noqa: BLE001
            self.stop_ours()
        self.endpoint = None
        await self._gone()
        return True

    async def _gone(self, seconds: float = 4.0) -> None:
        """After the polite close: wait for every process of ours to be gone, and end what is left - no Edge may stay behind in the taskbar after a booking."""
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        while loop.time() < end:
            if not await loop.run_in_executor(None, self.our_edge_pids):
                return
            await asyncio.sleep(0.5)
        left = await loop.run_in_executor(None, self.stop_ours)
        if left:
            log.info("ended %d browser process(es) that did not close by themselves", left)


async def guard_windows(ep: str, every: float = 0.12) -> None:
    """Run as a task while a booking works in a quiet browser: any NEW browser window (a pop-up opened by a payment page) is
    moved off-screen within a fraction of a second, so nothing ever flashes on the customer's screen. Cancel it to stop."""
    seen: set[str] = set()
    try:
        async with CDPBrowser(ep) as b:
            for p in await b.pages():
                seen.add(p["id"])
            while True:
                await asyncio.sleep(every)
                try:
                    pages = await b.pages()
                except Exception:  # noqa: BLE001
                    continue
                for p in pages:
                    if p["id"] in seen:
                        continue
                    seen.add(p["id"])
                    try:
                        tab = await b.open_tab(p)
                        try:
                            w = (await tab.send("Browser.getWindowForTarget", timeout=5)) or {}
                            left = (w.get("bounds") or {}).get("left", 0)
                            if left is not None and left > OFFSCREEN[0] + 1000:
                                await tab.send("Browser.setWindowBounds", {"windowId": w["windowId"], "bounds": {
                                    "left": OFFSCREEN[0], "top": OFFSCREEN[1], "windowState": "normal"}}, timeout=5)
                                log.info("moved a new window out of sight")
                        finally:
                            await tab.close()
                    except Exception:  # noqa: BLE001 - a window that is already gone needs nothing
                        pass
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.debug("window guard ended", exc_info=True)
