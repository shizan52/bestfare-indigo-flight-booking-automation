"""A quick, honest look at this computer: is everything Best Fare needs here? (No IndiGo traffic, no window.)

    python run.py --check            the checklist
    python run.py --check --browser  also start the app's own Edge, load IndiGo's home page and read what IndiGo can see
"""
from __future__ import annotations

import asyncio
import sys
import time

import paths

paths.use_engine()

OK, BAD, INFO = "  ✓ ", "  ✗ ", "  · "


def _line(mark: str, text: str) -> None:
    print(mark + text, flush=True)


def run(browser: bool = False) -> int:
    problems = 0
    print("\nBest Fare — check of this computer\n")

    _line(OK, f"Python {sys.version.split()[0]} ({sys.executable})")
    for mod, why in (("PyQt6.QtWidgets", "the window"), ("aiohttp", "talking to the booking browser")):
        try:
            __import__(mod)
            _line(OK, f"{mod.split('.')[0]} — {why}")
        except ImportError:
            _line(BAD, f"{mod.split('.')[0]} is missing ({why}) — run:  python run.py --setup")
            problems += 1

    for name in ("cdp", "human", "iframe_tools", "coupons", "indigo_client", "psp_flow", "airports"):
        try:
            __import__(name)
        except Exception as e:  # noqa: BLE001
            _line(BAD, f"IndiGo engine module {name} cannot be loaded: {e}")
            problems += 1
    if not problems:
        _line(OK, f"IndiGo engine found ({paths.ENGINE_DIR})")

    import browser as bmod
    edge = bmod.find_edge()
    if edge:
        ver = ""
        try:
            import subprocess
            ver = subprocess.run(["powershell", "-NoProfile", "-Command", f"(Get-Item '{edge}').VersionInfo.ProductVersion"],
                                 capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception:  # noqa: BLE001
            pass
        _line(OK, f"Microsoft Edge {ver} ({edge})")
    else:
        _line(BAD, "Microsoft Edge was not found — install it from microsoft.com/edge")
        problems += 1

    try:
        d = paths.data_dir()
        probe = d / ".write-test"
        probe.write_text("x")
        probe.unlink()
        _line(OK, f"data folder is writable ({d})")
    except OSError as e:
        _line(BAD, f"cannot write to the data folder: {e}")
        problems += 1

    edge_obj = bmod.LocalEdge()
    _line(INFO, f"booking browser profile: {edge_obj.profile}")
    try:
        import store as st
        s = st.Store(paths.db_path())
        saved = s.setting("pace") or {}
        left = float(saved.get("until", 0)) - time.time()
        if left > 0:
            _line(INFO, f"IndiGo asked us to slow down: about {int(left // 60) + 1} minute(s) left")
        else:
            _line(OK, "no IndiGo slow-down pending")
        recent = s.bookings(5)
        _line(INFO, f"{len(s.bookings(1000))} booking(s) in the history" + (f"; last: {recent[0]['origin']}->{recent[0]['dest']} "
              f"{recent[0]['status']} {recent[0].get('pnr') or ''}" if recent else ""))
        s.close()
    except Exception as e:  # noqa: BLE001
        _line(BAD, f"history database problem: {e}")
        problems += 1

    if browser and edge:
        problems += asyncio.run(_browser_check())

    print()
    print("Everything is ready." if not problems else f"{problems} problem(s) found — see the ✗ lines above.")
    return 1 if problems else 0


async def _browser_check() -> int:
    import browser as bmod
    from cdp import CDPBrowser
    from indigo_client import pick_trusted_tab
    print("\nStarting the booking browser…", flush=True)
    e = bmod.LocalEdge()
    try:
        ep = await e.ensure(note=lambda s: _line(INFO, s))
    except bmod.BrowserProblem as ex:
        _line(BAD, str(ex))
        return 1
    async with CDPBrowser(ep) as b:
        tab = await b.open_tab(pick_trusted_tab(await b.pages()))
        try:
            info = await tab.evaluate_json("return {wd: navigator.webdriver, vis: document.visibilityState, title: document.title.slice(0, 60),"
                                           " abck: /_abck=/.test(document.cookie)};")
        finally:
            await tab.close()
    bad = 0
    _line(OK if info["wd"] is False else BAD, f"IndiGo sees navigator.webdriver = {info['wd']} (must be false)")
    bad += 0 if info["wd"] is False else 1
    _line(OK if info["vis"] == "visible" else INFO, f"page visibility: {info['vis']}")
    _line(OK, f"IndiGo home page loaded: “{info['title']}”")
    _line(OK if info["abck"] else INFO, "IndiGo's security cookie present: " + ("yes" if info["abck"] else "not yet"))
    await e.close()
    _line(INFO, "booking browser closed again")
    return bad


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(run("--browser" in sys.argv))
