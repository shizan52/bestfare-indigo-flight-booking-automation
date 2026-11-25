"""Booking engine facade used by the worker.

* ONE booking at a time per trusted browser (an IndiGo session lives in that browser's cookies).
* A shared circuit breaker (indigo_client.Pace) pauses every booking after a 429/deny.
* Never reloads the trusted tab; never opens extra windows.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

import config
import doctor
from cdp import CDPBrowser, CDPError
from indigo_client import (BookingRequest, BookingResult, IndigoBlocked, IndigoError, Pace,
                           open_session, pick_trusted_tab, run_booking)
from psp_flow import run_psp

log = logging.getLogger(__name__)

# one lock per CDP endpoint -> serial per browser, parallel across browsers
_locks: dict[str, asyncio.Lock] = {}
_rr = 0


def _lock(ep: str) -> asyncio.Lock:
    return _locks.setdefault(ep, asyncio.Lock())


async def health(endpoint: Optional[str] = None) -> dict:
    """Is the trusted browser reachable and showing a real IndiGo page? (local only, no IndiGo traffic)"""
    ep = endpoint or config.CDP_ENDPOINTS[0]
    try:
        async with CDPBrowser(ep) as b:
            ver = (await b.version()).get("Browser", "?")
            pages = await b.pages()
            home = pick_trusted_tab(pages)
            title = home.get("title", "") if home else ""
            ok = home is not None and "Denied" not in title and not title.startswith("Loading")
            return {"ok": ok, "browser": ver, "tabs": len(pages), "title": title[:60], "endpoint": ep,
                    "cooldown_s": max(0, int(Pace.cooldown_until - __import__("time").monotonic()))}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:120], "endpoint": ep, "cooldown_s": 0}


async def heal(reason: str = "", force: bool = False) -> dict:
    """Repair the booking browser (doctor.py) - but never while a booking is using it."""
    ep = config.CDP_ENDPOINTS[0]
    lock = _lock(ep)
    if lock.locked():
        return {"ok": True, "actions": [], "detail": "A booking is running right now, so the browser is in use."}
    async with lock:
        try:
            return await doctor.heal(ep, reason, force)
        except Exception as e:  # noqa: BLE001 - the doctor must never crash its caller
            log.exception("doctor failed")
            return {"ok": False, "actions": [], "detail": f"The automatic repair failed: {str(e)[:80]}"}


async def book_flight(req: BookingRequest, promo: str, until: str = "payment") -> BookingResult:
    global _rr
    eps = config.CDP_ENDPOINTS
    ep = eps[_rr % len(eps)]
    _rr += 1
    async with _lock(ep):
        steps: list = []
        try:
            async with CDPBrowser(ep) as b:
                try:
                    await doctor.tidy(b)                 # tabs left over by an interrupted booking would get in the way
                except Exception:  # noqa: BLE001
                    pass
                tab, sess = await open_session(b, steps)

                async def psp() -> None:                   # the user's manual payment-gateway procedure
                    await run_psp(b)
                try:
                    res = await run_booking(sess, req, promo, until, psp=psp)
                finally:
                    await tab.close()
                res.steps = steps
                return res
        except IndigoBlocked as e:
            log.warning("blocked: %s", e)
            return BookingResult(success=False, retryable=True, steps=steps,
                                 error=f"IndiGo temporarily refused the request ({e}). Please retry later.")
        except IndigoError as e:
            return BookingResult(success=False, error=str(e), steps=steps)
        except (CDPError, OSError, asyncio.TimeoutError, aiohttp.ClientError) as e:
            # browser closed / tab gone / box rebooting: repair it now (we already hold the lock), and let the worker
            # retry after a pause instead of refunding at once
            try:
                fix = await doctor.heal(ep, "a booking could not reach the browser", force=True)
            except Exception:  # noqa: BLE001
                fix = {}
            note = f" (auto-repair: {doctor.describe(fix)})" if fix.get("actions") else ""
            return BookingResult(success=False, retryable=True, steps=steps,
                                 error=f"booking browser unavailable: {str(e)[:120]}{note}")
