"""Self-healing for the booking browser (the trusted Edge on the server).

What can break on its own, and what the doctor does about it - without a human:
  * Edge was closed / crashed / the PC rebooted and somebody logged in  -> start Edge again through the Windows task
    BestFare-Edge (it runs in the logged-in desktop, exactly like a person double-clicking the shortcut)
  * Edge runs with our profile but does not answer on the debugging port -> close that instance and start it properly
  * the IndiGo home tab was closed or navigated away                      -> open https://www.goindigo.in/ again
  * payment / bank / itinerary tabs left over from an interrupted booking -> close them
  * the home tab shows Akamai's "Access Denied"                           -> after the cool-down, open a fresh tab
Repairs are throttled (one attempt every 2 minutes unless an admin forces one) and reported as plain sentences for the
admins. The caller must hold the browser lock (automation.heal does), so a running booking is never disturbed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from typing import Optional

import config
from cdp import CDPBrowser
from indigo_client import Pace, pick_trusted_tab

log = logging.getLogger(__name__)

HOME = "https://www.goindigo.in/"
MIN_GAP = 120.0                      # seconds between two automatic repair attempts
# tabs that are never needed between bookings (left by an interrupted booking or opened by accident)
LEFTOVER = ("payment", "itinerary", "order_id=", "juspay", "hdfc", "/book/", "about:blank", "edge://newtab",
            "chrome://newtab", "chrome-error://")
_last_attempt = 0.0


def _run(cmd: list[str], timeout: float = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()[:300]
    except Exception as e:  # noqa: BLE001
        return -1, str(e)[:300]


def can_start_edge() -> bool:
    """Is the opt-in Windows task that starts Edge in the logged-in desktop set up? (deploy_box.py --setup-edge-task)"""
    if os.name != "nt" or not config.EDGE_AUTOSTART:
        return False
    code, _out = _run(["schtasks", "/Query", "/TN", config.EDGE_TASK], timeout=30)
    return code == 0


def start_edge() -> tuple[bool, str]:
    """Ask Windows to start Edge in the logged-in desktop (the opt-in scheduled task)."""
    if os.name != "nt":
        return False, "not running on Windows"
    code, out = _run(["schtasks", "/Run", "/TN", config.EDGE_TASK])
    return code == 0, out


def stop_stuck_edge() -> int:
    """Close the Edge instance that uses OUR profile (it is not answering, so it is useless). Returns processes closed."""
    if os.name != "nt":
        return 0
    profile = config.EDGE_PROFILE.replace("'", "''")
    ps = ("$n = 0; Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
          f"Where-Object {{ $_.CommandLine -like '*{profile}*' }} | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $n++ }; $n")
    _code, out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
    try:
        return int(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 0


async def cdp_up(endpoint: str) -> bool:
    try:
        async with CDPBrowser(endpoint) as b:
            await b.version()
            return True
    except Exception:  # noqa: BLE001
        return False


async def wait_cdp(endpoint: str, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if await cdp_up(endpoint):
            return True
        await asyncio.sleep(2)
    return False


async def tidy(b: CDPBrowser) -> list[str]:
    """Close left-over tabs. The IndiGo home tab is never touched, and NOTHING is closed unless the home tab exists:
    closing the last tab of Edge makes the whole browser quit (found in a live drill), so a window must always stay."""
    actions = []
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
                actions.append("closed a left-over tab")
            except Exception:  # noqa: BLE001
                pass
    if len(actions) > 1:
        actions = [f"closed {len(actions)} left-over tabs"]
    return actions


async def heal(endpoint: str, reason: str = "", force: bool = False) -> dict:
    """Try to bring the booking browser back to a usable state.
    Returns {"ok": True/False/None, "actions": [what was done], "detail": why it is still broken}
    ok=None means 'not attempted' (a repair was tried moments ago)."""
    global _last_attempt
    now = time.monotonic()
    if not force and now - _last_attempt < MIN_GAP:
        return {"ok": None, "actions": [], "detail": "a repair was tried a moment ago"}
    _last_attempt = now
    actions: list[str] = []
    log.info("doctor: checking the booking browser (%s)", reason or "requested")

    if not await wait_cdp(endpoint, 6):
        if not config.EDGE_AUTOSTART:
            return {"ok": False, "actions": actions, "detail": "Edge is not answering and automatic restart is switched off."}
        if not can_start_edge():                             # never close Edge unless we can also start it again
            return {"ok": False, "actions": actions,
                    "detail": "Edge is not answering, and automatic restart is not set up on the server."}
        if not await wait_cdp(endpoint, 10):                 # it may just be starting: look once more before acting
            closed = stop_stuck_edge()
            if closed:
                actions.append("closed an Edge window that had stopped answering")
                await asyncio.sleep(3)
            ok, out = start_edge()
            if not ok:
                log.warning("doctor: could not start Edge: %s", out)
                return {"ok": False, "actions": actions,
                        "detail": "Edge could not be started — is somebody logged in on the server's desktop?"}
            actions.append("started Edge again")
            if not await wait_cdp(endpoint, 60):
                return {"ok": False, "actions": actions, "detail": "Edge was started but is not answering yet."}
            await asyncio.sleep(8)                            # let the IndiGo home page load

    try:
        async with CDPBrowser(endpoint) as b:
            home = pick_trusted_tab(await b.pages())
            if home is None:                                  # FIRST make sure a home tab exists ...
                await b.new_page(HOME)
                actions.append("opened the IndiGo home page again")
                await asyncio.sleep(10)
                home = pick_trusted_tab(await b.pages())
            if home is not None:
                actions += await tidy(b)                      # ... only then close the leftovers (never the last window)
            if home is not None and "Denied" in (home.get("title") or ""):
                if Pace.cooldown_until > time.monotonic():
                    return {"ok": False, "actions": actions,
                            "detail": "IndiGo is refusing this computer for a while; the bot waits for the cool-down."}
                fresh = await b.new_page(HOME)
                await asyncio.sleep(10)
                fresh_now = next((p for p in await b.pages() if p["id"] == fresh.get("id")), None)
                if fresh_now and "Denied" not in (fresh_now.get("title") or ""):
                    await b.close_page(home["id"])
                    actions.append("replaced a blocked IndiGo tab with a fresh one")
                    home = fresh_now
    except Exception as e:  # noqa: BLE001 - Edge may vanish while we work on it: say so plainly, never raise
        log.warning("doctor: Edge stopped answering during the repair: %s", e)
        return {"ok": False, "actions": actions, "detail": "Edge stopped answering while it was being repaired."}
    ok = home is not None and "Denied" not in (home.get("title") or "")
    detail = "" if ok else "The IndiGo page could not be opened in Edge."
    log.info("doctor: ok=%s actions=%s %s", ok, actions, detail)
    return {"ok": ok, "actions": actions, "detail": detail}


def reset_throttle() -> None:
    global _last_attempt
    _last_attempt = 0.0


def describe(result: Optional[dict]) -> str:
    """One readable line for the admins."""
    if not result:
        return ""
    if result.get("actions"):
        done = "; ".join(result["actions"])
        return f"{done[0].upper()}{done[1:]}." + (f" Still a problem: {result['detail']}" if result.get("ok") is False else "")
    return result.get("detail") or ("Everything looks fine." if result.get("ok") else "")
