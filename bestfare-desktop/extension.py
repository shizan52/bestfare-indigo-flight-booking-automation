"""The coupon extension, built into the app.

The Indigo extension (F:\\card\\indigo, version 1.5.0) puts the promo code into IndiGo's flight-search and fare-hold requests with
the rules in its core.js. Best Fare runs THAT file (ext/core.js, unchanged) inside the booking page and lets it rewrite the two
requests - so the offer is applied by exactly the extension's own code. Nothing is installed in any browser, no toolbar icon, no
panel, nothing for the user to see; the work simply happens.

If the page cannot run it for any reason, the bot's Python port of the same rules (coupons.py, tested against this very file)
takes over, so a booking never fails because of this layer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Optional

import paths
import resources

paths.use_engine()
from indigo_client import U_SEARCH, U_SELL, IndigoBlocked, IndigoSession  # noqa: E402

log = logging.getLogger("extension")

CORE_FILE = "ext/core.js"          # (read through resources.py: from disk in the workspace, from the release in memory otherwise)
VERSION = "1.5.0"

# A refusal in the LATE part of a booking (the flight is chosen, the fare may be held) is never answered by starting over - that would ask the traveller
# to choose again. The one call is simply repeated after a calm moment, like a person pressing the button again. Only where that cannot do harm:
#   * the fare hold ("sell") only when the request provably never reached IndiGo's booking system (an edge deny: 429 / 401 / 403),
#   * adding the travellers and reading the booking back may be repeated whatever the refusal was (both can be done twice without harm).
LATE_RETRIES: dict[str, Optional[tuple[str, ...]]] = {"sell": ("429", "401", "403"), "addpax": None, "itinerary": None}
LATE_TRIES = 2                      # how many more times (after the first), each after a longer calm moment
LATE_CALM_S = (9.0, 16.0)           # seconds of natural activity before the first repeat; the second repeat waits twice as long


def core_source() -> str:
    return resources.read_text(CORE_FILE)


class ExtensionSession(IndigoSession):
    """A booking session whose promo-code rules are the extension's own core.js, run inside the page."""

    _loaded = False

    async def call(self, name: str, url: str, method: str, user_key: str, body: Optional[dict]) -> dict:
        """The shared call, plus a calm repeat of the late calls (see LATE_RETRIES). Everything else - the pacing, the logging, the refusal - is the shared code's."""
        allowed = LATE_RETRIES.get(name, "none")
        repeats = 0
        while True:
            try:
                return await super().call(name, url, method, user_key, body)
            except IndigoBlocked as e:
                ok = allowed is None or (allowed != "none" and e.kind in allowed)
                if not ok or repeats >= LATE_TRIES:
                    raise
                repeats += 1
                calm = random.uniform(*LATE_CALM_S) * repeats
                log.info("%s was refused (%s) - a calm moment of %.0f s, then once more (%d of %d)", name, e.kind or "?", calm, repeats, LATE_TRIES)
                try:
                    await self.human.wander(calm)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - looking alive must never matter: a plain pause does as well
                    await asyncio.sleep(calm)

    async def _core(self) -> None:
        if self._loaded:
            return
        if not await self.tab.evaluate_json("return typeof window.__IndigoCore === 'object';"):
            await self.tab.evaluate(core_source(), await_promise=False)           # defines window.__IndigoCore
        if not await self.tab.evaluate_json("return typeof window.__IndigoCore === 'object' && typeof window.__IndigoCore.rewriteBody === 'function';"):
            raise RuntimeError("the extension rules could not be loaded in the page")
        self._loaded = True

    async def _rewrite(self, url: str, body: dict, promo: str) -> dict:
        await self._core()
        out = await self.tab.evaluate_json(
            "const core = window.__IndigoCore;"
            f"const text = JSON.stringify({json.dumps(body)});"
            f"const rule = core.matchRule({json.dumps(url)}, 'POST', location.href);"
            "if (!rule) return {ok: false, why: 'no-rule'};"
            f"const r = core.rewriteBody(text, rule, {json.dumps(promo)}, {{keepSiteCode: false}});"
            "return {ok: true, changed: !!r.changed, skipped: r.skipped || null, body: JSON.parse(r.text)};")
        if not out or not out.get("ok"):
            raise RuntimeError(f"the extension rules did not match this request ({(out or {}).get('why')})")
        return out["body"]

    async def rewrite_search(self, body: dict, promo: str) -> dict:
        try:
            return await self._rewrite(U_SEARCH, body, promo)
        except Exception as e:  # noqa: BLE001 - never let this layer break a booking
            log.warning("extension rules unavailable for the search (%s) - using the tested port", e)
            return await super().rewrite_search(body, promo)

    async def rewrite_sell(self, body: dict, promo: str) -> dict:
        try:
            return await self._rewrite(U_SELL, body, promo)
        except Exception as e:  # noqa: BLE001
            log.warning("extension rules unavailable for the hold (%s) - using the tested port", e)
            return await super().rewrite_sell(body, promo)

    async def read_sell(self, answer: dict, promo: str) -> tuple[bool, dict]:
        try:
            await self._core()
            s = await self.tab.evaluate_json(f"return window.__IndigoCore.summarizeSell(JSON.stringify({json.dumps(answer)}));")
            if not s:
                return False, {}
            applied = str(s.get("applied") or "")
            return (applied.upper() == promo.upper() or float(s.get("discount") or 0) > 0), s
        except Exception as e:  # noqa: BLE001
            log.warning("extension rules unavailable for reading the answer (%s) - using the tested port", e)
            return await super().read_sell(answer, promo)
