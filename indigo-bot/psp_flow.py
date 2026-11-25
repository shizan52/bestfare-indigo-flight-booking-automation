"""The user's manual PNR procedure, automated on IndiGo's OWN payment page (live-proven 2026-11-17).

IndiGo's payment.html calls payment/initiate by itself and embeds the gateway (Juspay) as a cross-origin iframe.
Steps (exactly what the user does by hand):
  1. open payment.html in a NEW tab of the trusted profile
  2. in the gateway iframe:  Net banking  ->  HDFC Bank  ->  Pay
  3. the bank opens in a popup window: let it load fully, then CLOSE it
  4. IndiGo shows the "PROCESSING PAYMENT" page (11:45 timer): do NOT wait for the timer; click the
     '<  PROCESSING PAYMENT' back control in the top-left
  5. dialog "Are you sure you want to go back?"  ->  "Yes, exit"
  6. IndiGo lands on itinerary.html ("Your booking is on hold ... PNR: XXXXXX"); the tab is returned so the PNR
     can be read from the Itinerary API.
No credentials, no money: leaving at this point leaves a 30-minute On-Hold booking.

Standalone (needs a cart that already has passengers):   python psp_flow.py [--observe]
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
import time
from typing import Optional

import speed
from cdp import CDPBrowser, CDPTab
from iframe_tools import Frame

log = logging.getLogger(__name__)


async def state(tab: CDPTab) -> dict:
    return await tab.evaluate_json("return {url: location.href.slice(0, 160), title: document.title.slice(0, 80)};")


PAYMENT_URL = ("https://www.goindigo.in/payment.html?isBookingFlow=1&refUrl="
               "https%253A%252F%252Fwww.goindigo.in%252Fcontent%252Fskyplus6e%252Fin%252Fen%252Fbook%252Fitinerary.html")
SHOTDIR = r"D:\vps\apps\indigo-bot\data"


class PspError(Exception):
    pass


async def _wait_frame_text(browser: CDPBrowser, top: CDPTab, pattern: str, timeout: float) -> Frame:
    """(Re)attach to the gateway iframe until an element matching `pattern` is visible in it."""
    end = time.monotonic() + speed.scaled(timeout)
    while time.monotonic() < end:
        fr = await Frame.attach(browser, top)
        if fr:
            try:
                if await fr.find(pattern):
                    return fr
            except Exception:  # noqa: BLE001 - iframe navigating
                pass
            await fr.close()
        await asyncio.sleep(0.5)
    raise PspError(f"gateway never showed: {pattern}")


_run_no = 0


async def _snap(top: CDPTab, label: str, observe: bool, fr: Frame | None = None) -> None:
    if observe:
        try:
            await top.screenshot(f"{SHOTDIR}\\psp_r{_run_no}_{label}.png")
            print(f"  [{label}] {(await state(top))['url'][:100]}")
            if fr:
                print(f"      gateway text: {await fr.text(420)}")
        except Exception as e:  # noqa: BLE001
            print(f"  [{label}] snapshot failed: {e}")


async def _settle(fr: Frame, max_wait: float = 25.0) -> None:
    """A person waits until the gateway has finished loading. It builds the UPI QR code first and REDRAWS the whole
    widget when the code is ready ("View QR" appears) - which silently drops any choice made before that. Live runs
    showed the QR sometimes starts late, so 'no Generating QR yet' is not enough: wait for 'View QR' (or the limit)."""
    end = time.monotonic() + speed.scaled(max_wait)
    while time.monotonic() < end:
        try:
            if "view qr" in (await fr.text(2500)).lower():
                return
        except Exception:  # noqa: BLE001 - iframe navigating
            pass
        await asyncio.sleep(0.5)


async def _choose_hdfc(fr: Frame, tries: int = 3) -> bool:
    """Open Net banking and pick HDFC Bank - then look again a moment later: if the gateway redrew itself (Net banking
    collapsed, choice gone), choose again."""
    for n in range(tries):
        if not await fr.find(r"^HDFC Bank"):
            await fr.click(r"net\s*banking")
            if not await _poll(fr, r"^HDFC Bank", 8.0):
                continue
        await asyncio.sleep(random.uniform(0.2, 0.5))
        await fr.click(r"^HDFC Bank")
        await asyncio.sleep(random.uniform(0.9, 1.3))          # (long enough to see a redraw that undoes the choice)
        if await fr.find(r"^HDFC Bank") and await fr.find(r"popular banks"):
            return True
        log.info("the gateway redrew itself after HDFC Bank was chosen (try %d) - choosing again", n + 1)
    return False


async def _wait_new_page(browser: CDPBrowser, known: set, timeout: float) -> Optional[dict]:
    """The first page/window that was not there before (the bank popup), whatever its address."""
    end = time.monotonic() + speed.scaled(timeout)
    while time.monotonic() < end:
        for p in await browser.pages():
            if p["id"] not in known and "goindigo.in" not in p.get("url", ""):
                return p
        await asyncio.sleep(0.3)
    return None


async def _wait_loaded(browser: CDPBrowser, page_id: str, timeout: float) -> bool:
    """Wait until the popup shows the bank's own page (not about:blank / Juspay's redirect). False on timeout."""
    end = time.monotonic() + speed.scaled(timeout)
    while time.monotonic() < end:
        p = next((x for x in await browser.pages() if x["id"] == page_id), None)
        if p is None:
            return False
        u = p.get("url", "")
        if u.startswith("http") and "juspay" not in u:
            return True
        await asyncio.sleep(0.4)
    return False


async def _poll(fr: Frame, pattern: str, timeout: float) -> bool:
    end = time.monotonic() + speed.scaled(timeout)
    while time.monotonic() < end:
        try:
            if await fr.find(pattern):
                return True
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.4)
    return False


async def run_psp(browser: CDPBrowser, *, observe: bool = False, load_timeout: float = 45.0) -> dict:
    """Run the procedure; returns {'tab_id', 'url'} of the final goindigo.in page (caller reads the PNR).
    PSP_OBSERVE=1 in the environment saves a screenshot of every step (diagnosis only)."""
    global _run_no
    _run_no += 1
    observe = observe or os.getenv("PSP_OBSERVE") == "1"
    info = await browser.new_page(PAYMENT_URL)
    top = await browser.open_tab(info)
    popups_closed = 0
    fr: Frame | None = None
    try:
        await top.send("Page.bringToFront")
        await asyncio.sleep(random.uniform(0.4, 0.9))           # (the gateway frame is waited for below - no need to wait blindly)
        await _snap(top, "1_payment_page", observe)

        # 2) Net banking -> HDFC Bank -> Pay (all inside the gateway iframe)
        fr = await _wait_frame_text(browser, top, r"net\s*banking", load_timeout)
        await _settle(fr)
        await asyncio.sleep(random.uniform(0.3, 0.7))
        await _snap(top, "1b_settled", observe, fr)
        if not await _choose_hdfc(fr):
            log.warning("gateway would not keep HDFC Bank chosen; page text: %s", await fr.text(400))
            raise PspError("HDFC Bank option not shown under Net banking")
        await _snap(top, "2_hdfc_selected", observe, fr)
        known = {p["id"] for p in await browser.pages()} | {info["id"]}

        # 3) Pay -> the bank opens in a popup window. Any NEW window counts (it starts as about:blank while Juspay
        #    prepares it). No window? Either Juspay is waiting behind "Go To Payment" (press it), or the widget redrew
        #    itself and dropped HDFC (choose it again and press Pay again) - exactly what a person would do.
        popup = None
        for press in (1, 2):
            if not await fr.click(r"^Pay$"):
                raise PspError("Pay button not found")
            popup = await _wait_new_page(browser, known, 12)
            if popup:
                break
            log.info("no bank window 12 s after Pay (press %d); the gateway shows: %s", press, await fr.text(300))
            await _snap(top, f"3x_no_popup_{press}", observe)
            if await fr.find(r"go to payment"):
                await fr.click(r"go to payment")
                popup = await _wait_new_page(browser, known, 15)
                if popup:
                    break
            if press == 1 and not await _choose_hdfc(fr):
                break
        if popup is None:
            raise PspError("the bank popup window never opened")
        await _wait_loaded(browser, popup["id"], 35)         # "let it load fully" (about:blank -> juspay -> bank)
        await asyncio.sleep(random.uniform(1.2, 2.0))
        await _snap(top, "3_popup_open", observe)
        await browser.close_page(popup["id"])
        popups_closed += 1
        await fr.close()
        fr = None
        await asyncio.sleep(random.uniform(0.3, 0.6))           # (the next page is waited for below, by what it shows)

        # 4) "PROCESSING PAYMENT" page -> click the back control (do not wait for the timer)
        fr = await _wait_frame_text(browser, top, r"processing payment", 40)
        await _snap(top, "4_processing_page", observe)
        await fr.click(r"processing payment", 0)

        # 5) "Are you sure you want to go back?" -> Yes, exit   (wait for the question to show, then answer it)
        await _poll(fr, r"yes,?\s*exit", 8.0)
        await asyncio.sleep(random.uniform(0.3, 0.6))
        await _snap(top, "5_exit_dialog", observe)
        if not await fr.click(r"yes,?\s*exit"):
            raise PspError("the 'Yes, exit' confirmation did not appear")
        await fr.close()
        fr = None

        # 6) IndiGo returns to itinerary.html
        end = time.monotonic() + speed.scaled(40)
        while time.monotonic() < end:
            cur = (await state(top))["url"]
            if "itinerary" in cur:
                break
            await asyncio.sleep(0.5)
        else:
            raise PspError("did not reach the itinerary page after 'Yes, exit'")
        await asyncio.sleep(0.3)
        await _snap(top, "6_itinerary", observe)
        return {"tab_id": info["id"], "url": (await state(top))["url"], "popups_closed": popups_closed}
    except Exception:
        await _snap(top, "9_failed", observe)
        raise
    finally:
        if fr:
            await fr.close()
        await top.close()
        for p in await browser.pages():                # never leave payment/bank/itinerary tabs behind
            if p["id"] == info["id"] or (p["url"].startswith("http") and "goindigo.in" not in p["url"]):
                try:
                    await browser.close_page(p["id"])
                except Exception:  # noqa: BLE001
                    pass


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    async with CDPBrowser() as b:
        print(await run_psp(b, observe="--observe" in sys.argv))


if __name__ == "__main__":
    asyncio.run(_main())
