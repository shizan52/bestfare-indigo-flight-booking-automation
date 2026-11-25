"""Queue + worker pool.

The bot only enqueues. N workers (N = number of trusted browsers) pull jobs one at a
time, so the machine is never overloaded however many users book at once. A job can
never hang the pool (hard timeout), a failure refunds its credits exactly once, a
restart re-queues interrupted jobs, and the 2-hour PII cleanup runs on a timer.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

import airports
import automation
import config
import db
import ui
from indigo_client import BookingRequest, BookingResult, Passenger

log = logging.getLogger(__name__)

JOB_TIMEOUT_S = 240

Notifier = Callable[..., Awaitable[None]]            # notify(chat_id, text, kind="info"|"success"|"failure")
Runner = Callable[[BookingRequest, str], Awaitable[BookingResult]]


@dataclass
class Job:
    job_id: str


_queue: "asyncio.Queue[Job]" = asyncio.Queue(maxsize=config.MAX_QUEUE)
_notify: Optional[Notifier] = None
_alert_admins: Optional[Callable[[str], Awaitable[None]]] = None
_runner: Runner = automation.book_flight         # replaceable in tests
_tasks: list[asyncio.Task] = []
_active = 0                                       # bookings being worked on right now


def queue_size() -> int:
    return _queue.qsize()


def active_count() -> int:
    return _active


def jobs_ahead() -> int:
    """How many bookings a new request has to wait for (waiting + the one in progress)."""
    return _queue.qsize() + _active


async def enqueue(job_id: str) -> int:
    _queue.put_nowait(Job(job_id))
    return _queue.qsize()


def _when(b: dict) -> str:
    try:
        return ui.d_long(ui.from_store(b["travel_date"]))
    except ValueError:
        return b["travel_date"]


def _fmt_pnr_message(b: dict, res: BookingResult) -> str:
    """The customer's result card: the code first, then the trip, then exactly what to do next. No coupon codes."""
    esc = html.escape
    kind = res.kind or "order_id"
    offer = ui.offer_tag(b["offer"])
    head = "🔖 <b>PNR</b> (your booking code)" if kind == "record_locator" else "🔖 <b>Booking reference</b> (order id)"
    lines = ["🎉 <b>Your booking is ready!</b>", "", head, f"<code>{esc(res.pnr or '')}</code>   <i>← tap to copy</i>", "",
             f"✈️ <b>{esc(airports.label(b['origin']))} → {esc(airports.label(b['destination']))}</b>",
             f"📅 {_when(b)}  ·  👥 {ui.pax_word(b['pax_count'])}"]
    if res.coupon_applied is True:
        lines.append(f"🎁 {offer} applied" + (f" — you save <b>{ui.money(res.discount)}</b>" if res.discount else ""))
    elif res.coupon_applied is False:
        lines.append(f"⚠️ {offer} could not be applied to this flight — this is the regular fare. Please check before paying.")
    else:
        lines.append(f"🎁 {offer}")
    if res.fare_amount:
        lines.append(f"💰 Fare: <b>{ui.money(res.fare_amount)}</b>")
    if kind == "record_locator":
        until = ui.hold_until_text(res.hold_until)
        lines += ["", f"⏳ <b>Pay before {until}</b>" if until else f"⏳ <b>Pay within {config.PNR_HOLD_MINUTES} minutes</b>",
                  "How: goindigo.in → Manage Booking → enter this PNR and your email or last name → <b>Pay Now</b>."]
    else:
        if res.pay_url:
            lines += ["", f'💳 <a href="{esc(res.pay_url, quote=True)}">Tap here to pay</a>']
        lines += ["", f"⏳ Please pay within <b>{config.PNR_HOLD_MINUTES} minutes</b>."]
    return "\n".join(lines)


def _friendly_reason(err: str) -> str:
    e = (err or "").lower()
    if "no flights" in e:
        return "No flights are available for this route and date."
    if "timed out" in e:
        return "IndiGo took too long to answer."
    if "refused" in e or "unavailable" in e or "could not start a session" in e or "cooling down" in e:
        return "IndiGo's website could not be reached just now."
    if "fare could not be held" in e:
        return "The fare could not be held — it may have just sold out."
    if "passenger details rejected" in e:
        return "IndiGo did not accept the passenger details."
    return "The booking could not be completed."


def _fmt_failure_message(b: dict, err: str, refunded: bool) -> str:
    """Plain words for the customer; the technical reason stays in the admin's Bookings view."""
    esc = html.escape
    lines = ["😔 <b>We could not book this one</b>", "",
             f"✈️ {esc(airports.short(b['origin']))} → {esc(airports.short(b['destination']))}  ·  📅 {_when(b)}",
             f"Reason: {esc(_friendly_reason(err))}"]
    if refunded:
        lines += ["", f"💳 Your <b>{ui.credit_word(b['credit_cost'])}</b> {'has' if b['credit_cost'] == 1 else 'have'} been refunded automatically."]
    lines += ["", "You can try again, or pick a different day."]
    return "\n".join(lines)


SEND_DELAYS = (2.0, 5.0, 15.0)      # retries for delivering a message (Telegram or the network may hiccup)


async def _send(chat_id: int, text: str, kind: str = "info") -> bool:
    """Deliver a message to a customer; a few retries, and True only when it really arrived."""
    if _notify is None:
        log.info("notify(%s): %s", chat_id, text[:80])
        return True
    for i, delay in enumerate((0.0, *SEND_DELAYS)):
        if delay:
            await asyncio.sleep(delay)
        try:
            await _notify(chat_id, text, kind)
            return True
        except Exception:  # noqa: BLE001
            log.warning("could not notify %s (attempt %d)", chat_id, i + 1, exc_info=True)
    return False


async def _alert(text: str) -> None:
    if _alert_admins:
        try:
            await _alert_admins(text)
        except Exception:  # noqa: BLE001
            log.exception("could not alert the admins")


# ── watching trends: tell the admins once when something keeps going wrong ───────────────────────────────
STREAK_ALERT = 3
_streak = {"psp": 0, "fail": 0, "offer1": 0, "offer2": 0}


async def _watch_success(b: dict, res: BookingResult) -> None:
    _streak["fail"] = 0
    if res.psp_failed:
        _streak["psp"] += 1
        if _streak["psp"] == STREAK_ALERT:
            await _alert(f"The PNR step failed {STREAK_ALERT} times in a row - customers are getting an order id and a "
                         "payment link instead of a PNR. IndiGo's payment page may have changed.")
    else:
        _streak["psp"] = 0
    offer = b["offer"]
    if res.coupon_applied is False:
        _streak[offer] = _streak.get(offer, 0) + 1
        if _streak[offer] == STREAK_ALERT:
            await _alert(f"{ui.offer_tag(offer)} ({config.COUPONS.get(offer, '')}) was not applied by IndiGo "
                         f"{STREAK_ALERT} times in a row - the coupon may have expired or changed.")
    elif res.coupon_applied is True:
        _streak[offer] = 0


async def _watch_failure(err: str) -> None:
    if "no flights" in err.lower() or "cancelled by the customer" in err:
        return                                                    # not a fault of the system
    _streak["fail"] += 1
    if _streak["fail"] == STREAK_ALERT:
        await _alert(f"{STREAK_ALERT} bookings in a row have failed. Last reason: {err[:160]}. Open the Admin panel -> "
                     "Health, or pause bookings until it is fixed.")


MAX_BLOCK_RETRIES = 3
RETRY_DELAY_S = 180.0
_attempts: dict[str, int] = {}


async def _requeue(job_id: str, attempt: int) -> None:
    _attempts[job_id] = attempt
    try:
        await enqueue(job_id)
    except asyncio.QueueFull:
        await db.mark_failed_and_refund(job_id, "queue full while retrying")


async def process(job_id: str, attempt: Optional[int] = None) -> None:
    attempt = _attempts.pop(job_id, 0) if attempt is None else attempt
    if not await db.claim_job(job_id):          # finished, failed, or already being booked by someone else
        log.info("job %s not claimable (already running/finished) - skipping", job_id)
        return
    b = await db.get_booking(job_id)
    if not b:
        return
    req = BookingRequest(
        origin=b["origin"], destination=b["destination"], date=b["travel_date"],
        passengers=[Passenger(**p) for p in json.loads(b["passengers_json"])],
        offer=b["offer"], contact_email=b["contact_email"] or "", contact_phone=b["contact_phone"] or "")
    promo = config.COUPONS[b["offer"]]
    try:
        res = await asyncio.wait_for(_runner(req, promo), timeout=JOB_TIMEOUT_S)
    except asyncio.TimeoutError:                 # a hung browser / slow site: worth another try later
        res = BookingResult(success=False, retryable=True, error="booking timed out")
    except Exception as e:  # noqa: BLE001
        log.exception("job %s crashed", job_id)
        res = BookingResult(success=False, error=f"internal error: {type(e).__name__}")

    if res.success and res.pnr:
        await db.mark_done(job_id, res.pnr, res.kind or "order_id", res.pay_url, res.fare_amount, res.coupon_applied,
                           res.hold_until)
        delivered = await _send(b["user_tg_id"], _fmt_pnr_message(b, res), "success")
        log.info("job %s done ref=%s delivered=%s", job_id, res.pnr, delivered)
        if not delivered:                        # the booking exists - make sure a human can pass the PNR on
            await _alert(f"Booking {res.pnr} ({b['origin']}->{b['destination']} {b['travel_date']}) is done, but the "
                         f"message to the customer (Telegram id {b['user_tg_id']}) could not be delivered. "
                         "It is also shown in their My bookings.")
        await _watch_success(b, res)
        return

    err = res.error or "unknown error"
    # An edge block (429 / CORS-less deny) is transient: put the job back at the END of the queue and
    # try again after a cool-down instead of failing the customer. Credits stay charged meanwhile.
    if getattr(res, "retryable", False) and attempt < MAX_BLOCK_RETRIES:
        await db.requeue(job_id)
        log.warning("job %s blocked (attempt %d/%d) - retrying in %ss", job_id, attempt + 1, MAX_BLOCK_RETRIES, RETRY_DELAY_S)
        if attempt == 0:
            await _send(b["user_tg_id"], "⏳ <b>This is taking a little longer than usual.</b>\nYour booking will be retried "
                                         "automatically — you do not need to do anything.")
        asyncio.get_running_loop().call_later(RETRY_DELAY_S * (attempt + 1),
                                              lambda: asyncio.ensure_future(_requeue(job_id, attempt + 1)))
        return
    bal = await db.mark_failed_and_refund(job_id, err)
    await _send(b["user_tg_id"], _fmt_failure_message(b, err, bal is not None), "failure")
    log.warning("job %s failed: %s", job_id, err)
    if "refused" in err or "unavailable" in err or "timed out" in err:
        await _alert(f"Booking engine problem (after {attempt + 1} attempts): {err[:200]}")
    await _watch_failure(err)


JOB_HARD_TIMEOUT_S = JOB_TIMEOUT_S + 180      # last line of defence: nothing may keep the queue waiting longer than this


async def _gave_up(job_id: str) -> None:
    """One booking attempt hung even beyond its own timeouts: stop it, refund the customer once, carry on."""
    log.error("job %s hung for more than %ss - giving up on it", job_id, JOB_HARD_TIMEOUT_S)
    b = await db.get_booking(job_id)
    if not b or b["status"] in ("done", "failed"):
        return
    bal = await db.mark_failed_and_refund(job_id, "booking timed out")
    await _send(b["user_tg_id"], _fmt_failure_message(b, "booking timed out", bal is not None), "failure")
    await _alert(f"A booking attempt hung for more than {JOB_HARD_TIMEOUT_S // 60} minutes and was stopped (job {job_id}); "
                 "the customer was refunded. If this repeats, open Admin panel -> Health.")


async def _worker(n: int) -> None:
    global _active
    log.info("worker #%d started", n)
    while True:
        job = await _queue.get()
        _active += 1
        try:
            await asyncio.wait_for(process(job.job_id), timeout=JOB_HARD_TIMEOUT_S)
        except asyncio.TimeoutError:
            try:
                await _gave_up(job.job_id)
            except Exception:  # noqa: BLE001
                log.exception("worker #%d: could not clean up the hung job %s", n, job.job_id)
        except Exception:  # noqa: BLE001
            log.exception("worker #%d: unhandled error for %s", n, job.job_id)
        finally:
            _active -= 1
            _queue.task_done()


async def _cleanup_loop(interval: int = 600) -> None:
    while True:
        try:
            n = await db.cleanup_old_bookings()
            if n:
                log.info("cleanup: archived+deleted %d booking record(s)", n)
        except Exception:  # noqa: BLE001
            log.exception("cleanup loop error")
        await asyncio.sleep(interval)


async def backup_now() -> dict:
    """Write a backup of the money records (no personal booking data) and prune old ones. Returns its details."""
    info = await db.backup_to(config.BACKUP_DIR / db.backup_name())
    info["removed"] = db.prune_backups(config.BACKUP_DIR, config.BACKUP_KEEP)
    log.info("backup written: %s (%d users, %d ledger rows, %d KB, %d old removed)", Path(info["path"]).name, info["users"],
             info["credit_log"], info["bytes"] // 1024, info["removed"])
    return info


async def _backup_loop(interval: int = 3600, first_delay: float = 60.0) -> None:
    """Once a day (checked hourly): a fresh backup. Three failures in a row are reported to the admins."""
    await asyncio.sleep(first_delay)
    fails = 0
    while True:
        try:
            last = db.latest_backup(config.BACKUP_DIR)
            if last is None or time.time() - last.stat().st_mtime > 23 * 3600:
                await backup_now()
            fails = 0
        except Exception:  # noqa: BLE001
            fails += 1
            log.exception("backup failed (%d in a row)", fails)
            if fails == 3:
                await _alert("The daily backup has failed 3 times in a row. Check the disk space on the server "
                             "(D:) - the credit records are not being copied.")
        await asyncio.sleep(interval)


async def start(notify: Notifier, alert_admins: Optional[Callable[[str], Awaitable[None]]] = None,
                workers: Optional[int] = None) -> None:
    """Start workers + cleanup + backups, and re-queue anything a crash/restart interrupted."""
    global _notify, _alert_admins
    _notify, _alert_admins = notify, alert_admins
    n = workers or len(config.CDP_ENDPOINTS)
    _tasks.extend(asyncio.create_task(_worker(i + 1)) for i in range(n))
    _tasks.append(asyncio.create_task(_cleanup_loop()))
    _tasks.append(asyncio.create_task(_backup_loop()))
    for j in await db.recoverable_jobs():
        await enqueue(j["job_id"])
        log.info("recovered job %s", j["job_id"])
    log.info("started %d worker(s) + cleanup loop", n)


async def stop() -> None:
    for t in _tasks:
        t.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
