"""IndiGo booking flow executed INSIDE the human-launched, Akamai-trusted Edge.

Every API call is an in-page fetch() issued over raw CDP, so IndiGo sees the
residential IP, the real browser TLS/cookies and the same-origin request the site
itself would make. Calls are strictly paced and serialised; a 429 / CORS-less deny
trips a circuit breaker instead of hammering the site.

Flow (mirrors the captured HAR):
  reset session -> search -> choose cheapest fare -> sell(HoldSpace)
  -> addpassenger -> payment/initiate  (returns order_id = pre-payment reference)
Promo injection is identical to the coupon extension: `codes.promotionCode` on
search and `promotionCode` on sell. Both offers use passenger type ADT.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

import coupons
import speed
from cdp import CDPBrowser, CDPEvalError, CDPError, CDPTab
from human import HumanInput

log = logging.getLogger(__name__)

SITE = ["https://www.goindigo.in/"]
UK_FLIGHT = "31e90be8fff2f5e2eea242c225f21b1a"     # search + sell
UK_SKY = "a7696c55cadeb1d26ec1eebfd954ff37"        # addpassenger + payment + login
UK_SESSION = "654a6a3cc4998e498e5c0c8ead072915"    # token refresh

U_SEARCH = "https://api-prod-flight-skyplus6e.goindigo.in/v2/flight/search"
U_SELL = "https://api-prod-flight-skyplus6e.goindigo.in/v1/flight/sell"
U_TOKEN = "https://api-prod-skyplus.goindigo.in/login/v2/token/validate"
U_ADDPAX = "https://api-prod-skyplus.goindigo.in/passengerspost/v2/passenger/addpassenger"
U_PAYINIT = "https://api-prod-skyplus.goindigo.in/prepayment/v1/payment/initiate"
U_CONFIRM = "https://api-prod-skyplus.goindigo.in/postpayment/v1/payment/confirm"
U_ITIN = "https://api-prod-skyplus.goindigo.in/itinerary/v2/Itinerary"


class IndigoBlocked(Exception):
    """Akamai refused us (429 or a CORS-less deny). Do NOT retry immediately.

    `kind` says how: "fetch" (the request never got an answer: an edge deny without CORS headers - or a network failure), "429", "401", "403",
    "5xx" (IndiGo's own server), "cooling" (the circuit breaker is open). `name` is the call (token / search / sell ...). The desktop app uses them
    to decide what a calm, automatic second try looks like; everything else just treats it as 'refused'."""

    def __init__(self, message: str = "", *, kind: str = "", name: str = "", status: Optional[int] = None):
        super().__init__(message)
        self.kind, self.name, self.status = kind, name, status


class IndigoError(Exception):
    """A business/API error with a user-presentable message."""


ADULT, CHILD, INFANT = "adult", "child", "infant"


@dataclass
class Passenger:
    first_name: str
    last_name: str
    gender: str            # 'M' | 'F'
    dob: str = ""          # DD-MM-YYYY. An adult's is not sent to IndiGo (null); a CHILD's and an INFANT's are (IndiGo checks the age)
    kind: str = ADULT      # 'adult' | 'child' (2 to 11 years on the travel date) | 'infant' (under 2 years, on an adult's lap)


@dataclass
class BookingRequest:
    origin: str
    destination: str
    date: str              # DD-MM-YYYY
    passengers: list[Passenger]
    offer: str             # 'offer1' | 'offer2'
    contact_email: str
    contact_phone: str

    @property
    def adults(self) -> list[Passenger]:
        return [p for p in self.passengers if p.kind == ADULT]

    @property
    def children(self) -> list[Passenger]:
        return [p for p in self.passengers if p.kind == CHILD]

    @property
    def infants(self) -> list[Passenger]:
        return [p for p in self.passengers if p.kind == INFANT]

    @property
    def seats(self) -> list[Passenger]:
        """The travellers who take a seat of their own (adults, then children) - what IndiGo calls passengers; infants ride on an adult."""
        return self.adults + self.children


@dataclass
class BookingResult:
    success: bool
    pnr: Optional[str] = None            # order_id / record locator (see `kind`)
    kind: str = ""                       # 'order_id' | 'record_locator'
    fare_amount: Optional[float] = None
    pay_url: Optional[str] = None
    hold_until: Optional[str] = None
    error: Optional[str] = None
    retryable: bool = False              # transient edge block -> worker re-queues after a cool-down
    coupon_applied: Optional[bool] = None   # did IndiGo's booking carry the coupon? (None = not checked)
    discount: float = 0.0
    psp_failed: bool = False             # the PNR step failed and the order-id fallback was used
    itinerary: Optional[dict] = None     # IndiGo's own record of the booking (itinerary answer) - names, route, date, contact
    flight: Optional[dict] = None        # the flight that was booked (a flight_options() entry) - set when it came from a choice
    steps: list = field(default_factory=list)


# ── JS snippets (kept tiny; arguments are JSON-embedded, never string-concatenated) ──
def _fetch_js(url: str, method: str, user_key: str, body: Optional[dict]) -> str:
    return (
        "try {"
        f" const r = await fetch({json.dumps(url)}, {{method: {json.dumps(method)}, credentials: 'include',"
        f" headers: {{'content-type': 'application/json', 'user_key': {json.dumps(user_key)}}},"
        f" body: {json.dumps(json.dumps(body)) if body is not None else 'undefined'}}});"
        " let j = null; let raw = '';"
        " try { raw = await r.text(); j = JSON.parse(raw); } catch (e) {}"
        " return {status: r.status, json: j, raw: j ? '' : raw.slice(0, 200)};"
        "} catch (e) { return {fetchError: String(e).slice(0, 120)}; }"
    )


class Pace:
    """Global pacing + circuit breaker shared by all bookings (one process)."""
    min_gap = 2.5            # seconds between any two IndiGo API calls
    jitter = 2.0
    cooldown_until = 0.0     # monotonic; set by the circuit breaker
    last_call = 0.0
    on_trip: Optional[Callable[[float], object]] = None   # the bot saves the cool-down so a restart cannot forget it
    strikes = 0              # blocks in a row; each one doubles the pause (hammering a blocking site only prolongs the block)
    MAX_COOLDOWN = 2 * 3600.0
    lockout = True           # False = a refusal never closes the door for a while: the caller (the desktop app) retries calmly by itself, see trip()

    @classmethod
    def cooldown_left(cls) -> float:
        return max(0.0, cls.cooldown_until - time.monotonic())

    @classmethod
    def success(cls) -> None:
        """IndiGo answered normally: the next block starts again from the short pause."""
        cls.strikes = 0

    @classmethod
    async def wait(cls) -> None:
        now = time.monotonic()
        if now < cls.cooldown_until:
            raise IndigoBlocked(f"cooling down for {int(cls.cooldown_until - now)}s after a block", kind="cooling")
        gap = cls.min_gap + random.uniform(0, cls.jitter)
        delta = gap - (now - cls.last_call)
        if delta > 0:
            await asyncio.sleep(delta)
        cls.last_call = time.monotonic()

    @classmethod
    def trip(cls, seconds: float = 900.0) -> None:
        if not cls.lockout:
            return                   # (no lock-out: the one who made the call decides what happens next, and how gently)
        cls.strikes += 1
        seconds = min(seconds * 2 ** (cls.strikes - 1), cls.MAX_COOLDOWN)       # 15 min, 30, 60, then 2 h at most
        cls.cooldown_until = time.monotonic() + seconds
        if cls.on_trip:
            try:
                cls.on_trip(seconds)
            except Exception:  # noqa: BLE001 - saving the cool-down must never break a booking
                log.exception("could not save the cool-down")

    @classmethod
    def resume(cls, seconds_left: float) -> None:
        """Re-apply a cool-down that was saved before a restart."""
        if seconds_left > 0:
            cls.cooldown_until = max(cls.cooldown_until, time.monotonic() + seconds_left)


class IndigoSession:
    def __init__(self, tab: CDPTab, human: HumanInput, steplog: list):
        self.tab = tab
        self.human = human
        self.steps = steplog

    async def call(self, name: str, url: str, method: str, user_key: str, body: Optional[dict]) -> dict:
        await Pace.wait()
        t0 = time.monotonic()
        try:                                              # (a slow connection is waited for: the limit grows with the measured speed)
            res = await self.tab.evaluate_json(_fetch_js(url, method, user_key, body), timeout=min(150.0, speed.scaled(60.0)))
        except (CDPEvalError, CDPError) as e:
            self._log(name, "cdp-error", t0, str(e)[:100])
            raise IndigoError(f"browser error during {name}: {e}")
        if res.get("fetchError"):
            Pace.trip()
            self._log(name, "BLOCKED(no-cors)", t0, res["fetchError"])
            raise IndigoBlocked(f"{name}: request blocked at the edge ({res['fetchError']})", kind="fetch", name=name)
        st = res.get("status")
        self._log(name, st, t0, "")
        if st == 429:
            Pace.trip()
            raise IndigoBlocked(f"{name}: HTTP 429 (rate limited)", kind="429", name=name, status=429)
        if st in (401, 403):
            raise IndigoBlocked(f"{name}: HTTP {st} (session not accepted)", kind=str(st), name=name, status=st)
        if isinstance(st, int) and st >= 500:            # IndiGo's own servers hiccuped: try again later, do not refund
            raise IndigoBlocked(f"{name}: HTTP {st} (IndiGo server error)", kind="5xx", name=name, status=st)
        if st == 200:
            speed.Net.record(name, time.monotonic() - t0)   # how fast the connection really is (drives every network wait)
        return res

    # ── the coupon rules. Default: the Python port (coupons.py, parity-tested against the extension's core.js). The desktop app
    #    replaces these three with the extension's OWN core.js run inside the page (bestfare-desktop/extension.py).
    async def rewrite_search(self, body: dict, promo: str) -> dict:
        return coupons.apply_search(body, promo)

    async def rewrite_sell(self, body: dict, promo: str) -> dict:
        return coupons.apply_sell(body, promo)

    async def read_sell(self, answer: dict, promo: str) -> tuple[bool, dict]:
        """(did IndiGo apply the code?, {applied, discount, fare, total})"""
        return coupons.coupon_applied(answer, promo), (coupons.summarize_sell(answer) or {})

    def _log(self, name: str, status, t0: float, note: str) -> None:
        self.steps.append({"step": name, "status": status, "ms": int((time.monotonic() - t0) * 1000), "note": note})
        log.info("indigo %-10s -> %s (%d ms) %s", name, status, int((time.monotonic() - t0) * 1000), note)

    async def human_pause(self, lo: float = 1.0, hi: float = 3.0) -> None:
        """A person does not fire the next request the instant the last answer arrives. But the time that has ALREADY passed since
        the previous call started counts (a slow answer, the traveller choosing a flight): only what is still missing is waited.
        So the spacing between two calls is never shorter than lo..hi, and never longer than it has to be."""
        left = random.uniform(lo, hi) - (time.monotonic() - Pace.last_call)
        if left > 0.05:
            await self.human.wander(left)


def _minutes_between(a: Optional[str], b: Optional[str]) -> Optional[int]:
    """Minutes from one ISO time to another ('2026-11-30T10:00:00Z' style, UTC); None when either is missing."""
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return int((datetime.strptime(b, fmt) - datetime.strptime(a, fmt)).total_seconds() // 60)
    except (TypeError, ValueError):
        return None


def flight_options(search_json: dict, origin: Optional[str] = None, dest: Optional[str] = None) -> list[dict]:
    """Every bookable flight for EXACTLY the requested airports, each with ITS lowest fare - what IndiGo's own page lists under
    "Choose your preferred flight". Sorted by departure time.

    IndiGo's answer for DEL->BOM also lists journeys from / to NEARBY airports (Hindon, Navi Mumbai ...). They are often
    cheaper, but they are a different ticket - found live 2026-11-18: an Offer 2 booking for Delhi->Mumbai came back as
    DEL->AMD->NMI (Navi Mumbai). So when the requested airports are known, only those journeys count.

    Each entry: key (journeyKey), fare_key, amount (total for all travellers), original (the price before the offer, or None),
    offer (this fare carries the offer), product, seg, stops, dep / arr (IndiGo's local ISO times), minutes (flying time incl.
    the wait between legs), flights (["6E 864"] or the legs of a connection), via (cities where a connection changes), airports
    and cities of both ends."""
    out: list[dict] = []
    for trip in (search_json.get("data") or {}).get("trips") or []:
        for jr in trip.get("journeysAvailable") or []:
            if jr.get("isSold") or not jr.get("journeyKey"):
                continue
            des = jr.get("designator") or {}
            jo, jd = des.get("origin") or des.get("origination"), des.get("destination")
            if (origin and jo and jo != origin) or (dest and jd and jd != dest):
                continue
            best_pf = None
            for pf in jr.get("passengerFares") or []:
                if not pf.get("isActive", True) or pf.get("fareDisableReasonCode") or not pf.get("fareAvailabilityKey"):
                    continue
                amt = pf.get("totalFareAmount")
                if not isinstance(amt, (int, float)):
                    continue
                if best_pf is None or amt < best_pf["totalFareAmount"]:
                    best_pf = pf
            if best_pf is None:
                continue
            segs = jr.get("segments") or []
            legs = [f"{(s.get('identifier') or {}).get('carrierCode') or ''} {(s.get('identifier') or {}).get('identifier') or ''}".strip()
                    for s in segs]
            via = [((s.get("designator") or {}).get("destinationCityName") or (s.get("designator") or {}).get("destination") or "")
                   for s in segs[:-1]]
            orig = best_pf.get("originalFareAmount")
            amount = best_pf["totalFareAmount"]
            out.append({
                "key": jr["journeyKey"], "fare_key": best_pf["fareAvailabilityKey"], "amount": amount,
                "original": orig if isinstance(orig, (int, float)) and orig > amount else None,
                "offer": bool(best_pf.get("promoCode")), "product": best_pf.get("productClass"),
                "seg": jr.get("segKey"), "stops": jr.get("stops") if isinstance(jr.get("stops"), int) else max(0, len(segs) - 1),
                "dep": des.get("departure") or "", "arr": des.get("arrival") or "",
                "minutes": _minutes_between(des.get("utcDeparture"), des.get("utcArrival")),
                "flights": [x for x in legs if x], "via": [v for v in via if v],
                "origin": jo, "dest": jd, "origin_city": des.get("originCityName") or "", "dest_city": des.get("destinationCityName") or "",
            })
    out.sort(key=lambda o: (o["dep"], o["amount"]))
    return out


def _as_best(o: dict) -> dict:
    return {"amount": o["amount"], "dep": o["dep"], "journeyKey": o["key"], "fareKey": o["fare_key"], "product": o["product"],
            "seg": o["seg"], "stops": o["stops"]}


def cheapest(search_json: dict, origin: Optional[str] = None, dest: Optional[str] = None) -> Optional[dict]:
    """The cheapest bookable fare for EXACTLY the requested airports (ties: the earlier flight). See flight_options()."""
    opts = flight_options(search_json, origin, dest)
    if not opts:
        return None
    return _as_best(min(opts, key=lambda o: (o["amount"], o["dep"])))


RECORD_TRIES = 3
RECORD_GAP_S = 1.5


async def _read_record(s: "IndigoSession") -> tuple[dict, dict]:
    """IndiGo's record of the booking right after the payment page was left. It can show up a moment after the page does, so a few
    quick reads come first - far cheaper than going through the whole payment page again. Returns (answer, bookingDetails)."""
    ii: dict = {}
    bd: dict = {}
    for i in range(RECORD_TRIES):
        if i:
            await asyncio.sleep(RECORD_GAP_S)
        ii = await s.call("itinerary", U_ITIN, "POST", UK_SKY, {})
        bd = (((ii.get("json") or {}).get("data")) or {}).get("bookingDetails") or {}
        if bd.get("recordLocator"):
            break
    return ii, bd


async def _wait_for_choice(s: "IndigoSession", choose, options: list[dict]) -> Optional[str]:
    """Ask the caller which flight to take - and keep the page alive like a person who is reading the list (a still, silent
    page for minutes is what a script looks like)."""
    async def stay_alive() -> None:
        while True:
            try:
                await s.human.wander(random.uniform(5, 9))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - looking busy must never break the booking
                await asyncio.sleep(3)

    keep = asyncio.ensure_future(stay_alive())
    try:
        return await choose(options)
    finally:
        keep.cancel()
        try:
            await keep
        except (asyncio.CancelledError, Exception):  # (the CancelledError of our own cancel)
            pass


def _iso(dmy: str) -> str:
    return datetime.strptime(dmy, "%d-%m-%Y").strftime("%Y-%m-%d")


# ── passengers of every kind ──────────────────────────────────────────────────────────────────────────────────
# IndiGo's own page: adult = ADT, child = CHD, infant = INFT (a senior citizen is an ADT with the discount code PROMO2 - here that is the offer's
# promo code, so every grown-up stays a plain ADT).
def pax_types(req: BookingRequest, with_infants: bool) -> list[dict]:
    """`passengers.types` of a search / sell body: one entry per kind that is present."""
    types = [{"count": len(req.adults), "discountCode": "", "type": "ADT"}]
    if req.children:
        types.append({"count": len(req.children), "discountCode": "", "type": "CHD"})
    if with_infants and req.infants:
        types.append({"count": len(req.infants), "discountCode": "", "type": "INFT"})
    return types


def dob_iso(p: Passenger) -> Optional[str]:
    """A child's / infant's date of birth the way IndiGo takes it (an adult's is not sent)."""
    if p.kind == ADULT or not p.dob:
        return None
    for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(p.dob, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _name(p: Passenger, young: bool) -> dict:
    title = ("MSTR" if p.gender == "M" else "MISS") if young else ("MR" if p.gender == "M" else "MS")
    return {"first": p.first_name.title(), "middle": "", "last": p.last_name.title(), "title": title, "suffix": "", "action": "Update"}


def _info(p: Passenger, with_dob: bool) -> dict:
    return {"nationality": "IN", "gender": 1 if p.gender == "M" else 2, "dateOfBirth": dob_iso(p) if with_dob else None}


# How an infant is written inside the adult who carries it. IndiGo's own answer (found live, 2026-11-19): `infant.gender` must be TEXT, not the
# number an adult's `info.gender` is. Which text is not documented, so the likely spellings are tried in order, IndiGo's own validation
# message is the judge, and the one it accepts is remembered for every later booking.
INFANT_STYLES: list[dict] = [{"gender": "word"}, {"gender": "letter"}, {"gender": "digit"}, {"gender": "word", "dob": "datetime"}]
LEARNED = {"infant_style": 0}


def _infant_gender(p: Passenger, style: dict) -> object:
    how = style.get("gender", "word")
    male = p.gender == "M"
    if how == "letter":
        return "M" if male else "F"
    if how == "digit":
        return "1" if male else "2"
    return "Male" if male else "Female"


def passenger_objects(slots: dict, req: BookingRequest, style: Optional[dict] = None) -> Optional[list[dict]]:
    """The `passengers` of the add-passenger body: IndiGo's own slot keys (from the fare hold) filled with OUR travellers - adults in
    the adult slots, children in the child slots, and each infant either in its own infant slot or on the adult who carries it.
    None when the slots do not fit the travellers."""
    by_type: dict[str, list[str]] = {"ADT": [], "CHD": [], "INFT": []}
    untyped: list[str] = []
    for key, obj in slots.items():
        code = (obj or {}).get("passengerTypeCode") if isinstance(obj, dict) else None
        if code in by_type:
            by_type[code].append(key)
        else:
            untyped.append(key)
    if untyped and not any(by_type.values()):                     # (an answer that names no kinds: adults first, then children, as sold)
        by_type["ADT"] = untyped[:len(req.adults)]
        by_type["CHD"] = untyped[len(req.adults):len(req.seats)]
    if len(by_type["ADT"]) < len(req.adults) or len(by_type["CHD"]) < len(req.children):
        return None
    objs: list[dict] = []
    infants = list(req.infants)

    def base(key: str, p: Passenger, code: str) -> dict:
        young = p.kind != ADULT
        return {"passengerTypeCode": code, "discountCode": None, "passengerKey": key, "name": _name(p, young),
                "loyaltyInfo": {"selfTravel": False, "isNominee": False, "NomineeId": "", "FFN": ""},
                "extraSeatTag": "", "travelDocumentToTake": None, "passportInfoComment": None,
                "info": _info(p, young), "travelDocuments": []}

    for key, p in zip(by_type["ADT"], req.adults):
        o = base(key, p, "ADT")
        if infants and not by_type["INFT"]:                       # no infant slots: the infant is a part of the adult who carries it
            inf = infants.pop(0)
            born = dob_iso(inf)
            o["infant"] = {"name": _name(inf, True), "gender": _infant_gender(inf, style or {}),
                           "dateOfBirth": (born + "T00:00:00") if born and (style or {}).get("dob") == "datetime" else born,
                           "nationality": "IN", "residentCountry": "IN", "travelDocuments": []}
        objs.append(o)
    for key, p in zip(by_type["CHD"], req.children):
        objs.append(base(key, p, "CHD"))
    if by_type["INFT"]:
        if len(by_type["INFT"]) < len(req.infants):
            return None
        for key, p in zip(by_type["INFT"], req.infants):
            objs.append(base(key, p, "INFT"))
    elif infants:                                                 # more infants than adults to carry them
        return None
    return objs


async def run_booking(s: IndigoSession, req: BookingRequest, promo: str, until: str = "payment",
                      psp=None, choose=None, warmup: Optional[tuple[float, float]] = None) -> BookingResult:
    """Execute the flow. `until` = search | sell | addpax | payment (for staged testing).
    `psp` = async callable() that performs the payment-gateway steps on IndiGo's payment page (psp_flow.py).
    `choose` = optional async callable(options) -> journey key | None: shown the flights of flight_options() (each with its
    lowest fare) once the search is answered, it says which one to book (None = none was chosen: nothing is held).
    Without it the cheapest flight is booked, as the Telegram bot always did.
    `warmup` = (low, high) seconds of natural mouse/scroll activity before the first call; None = the full 22-38 s. A caller that
    has ALREADY kept the page alive that long (the desktop app does, while the traveller fills the form) passes a short one."""
    res = BookingResult(success=False, steps=s.steps)
    n = len(req.seats)                                   # passengers with a seat of their own (infants ride on an adult)
    if not req.adults:
        res.error = "!Children and infants must travel with an adult. Please add an adult."
        return res
    if len(req.infants) > len(req.adults):
        res.error = "!Each infant travels on one adult's lap - please add an adult for every infant."
        return res

    # 0) behave like a person before the first call: Akamai's sensor needs live, organic input, and
    #    live runs showed a search fired seconds after the token call was blocked, whereas the same
    #    flow after ~30 s of natural mouse/scroll activity was accepted.
    lo, hi = warmup or (22.0, 38.0)
    await s.human.wander(random.uniform(lo, hi))

    # 1) fresh anonymous session (drop only api-prod cookies; Akamai trust cookies stay)
    for c in await s.tab.cookies(["https://api-prod-skyplus.goindigo.in/", "https://api-prod-booking-skyplus6e.goindigo.in/",
                                  "https://api-prod-flight-skyplus6e.goindigo.in/", "https://api-prod-session-skyplus6e.goindigo.in/"]):
        d = c.get("domain", "")
        if d.startswith("api-prod-") and "goindigo.in" in d:
            await s.tab.delete_cookie(c["name"], domain=d)
    await s.human_pause(1, 2)
    tok = await s.call("token", U_TOKEN, "POST", UK_SKY,
                       {"ciamToken": None, "policyName": None, "applicationName": "dotREZ"})
    if tok["status"] != 200:
        res.error = f"could not start a session (HTTP {tok['status']})"
        return res

    # 2) search - the request exactly as IndiGo's own page builds it (EMPTY promotionCode), then the coupon is put in
    #    by the same rule the coupon extension uses (coupons.apply_search == core.js rewriteBody, parity-tested)
    await s.human_pause()
    site_body = {"codes": {"currency": "INR", "promotionCode": ""},
                 "criteria": [{"dates": {"beginDate": _iso(req.date)}, "flightFilters": {"type": "All"},
                               "stations": {"originStationCodes": [req.origin], "destinationStationCodes": [req.destination]}}],
                 "passengers": {"residentCountry": "IN", "types": pax_types(req, True)},
                 "taxesAndFees": "TaxesAndFees", "tripCriteria": "oneWay", "isRedeemTransaction": False}
    body = await s.rewrite_search(site_body, promo)
    sr = await s.call("search", U_SEARCH, "POST", UK_FLIGHT, body)
    sj = sr.get("json") or {}
    if sr["status"] != 200 or sj.get("errors"):
        res.error = _api_error(sj) or f"search failed (HTTP {sr['status']})"
        return res
    Pace.success()                                       # the search was answered: we are not blocked
    options = flight_options(sj, req.origin, req.destination)
    if not options:
        res.error = "no flights available for this route/date"
        return res
    best = _as_best(min(options, key=lambda o: (o["amount"], o["dep"])))
    if choose is not None and until != "search":
        key = await _wait_for_choice(s, choose, options)
        picked = next((o for o in options if o["key"] == key), None) if key else None
        if picked is None:
            res.error = "no flight was chosen"
            return res
        res.flight = picked
        best = _as_best(picked)
        log.info("chosen %s stops=%s product=%s Rs%s", best["seg"], best["stops"], best["product"], best["amount"])
    else:
        log.info("cheapest %s stops=%s product=%s Rs%s", best["seg"], best["stops"], best["product"], best["amount"])
    res.fare_amount = best["amount"]
    if until == "search":
        res.success = True
        return res

    # 3) sell (fare hold) - IndiGo's own sell body has an EMPTY promotionCode; the extension fills only that empty field
    await s.human_pause()
    site_sell = {"preventOverlap": True, "suppressPassengerAgeValidation": True, "vaxDoseNo": "", "sourceOrganization": "",
                 "keys": [{"journeyKey": best["journeyKey"], "fareAvailabilityKey": best["fareKey"],
                           "standbyPriorityCode": "", "inventoryControl": "HoldSpace"}],
                 "passengers": {"residentCountry": "", "types": pax_types(req, False)},
                 "infantCount": len(req.infants), "currencyCode": "INR", "promotionCode": "",
                 "loyaltyRequest": {"loyaltyPromoCode": "", "isRedeemTransaction": False, "cashPercentage": 100, "milesPercentage": 0}}
    sell_body = await s.rewrite_sell(site_sell, promo)
    sl = await s.call("sell", U_SELL, "POST", UK_FLIGHT, sell_body)
    slj = sl.get("json") or {}
    if sl["status"] != 200 or slj.get("errors"):
        res.error = _api_error(slj) or f"fare could not be held (HTTP {sl['status']})"
        return res
    # did IndiGo really apply the coupon? (typeOfSale.promotionCode, or a discount > 0 - the extension's own test)
    res.coupon_applied, summ = await s.read_sell(slj, promo)
    res.discount = summ.get("discount") or 0.0
    log.info("coupon %s applied=%s (IndiGo says %r, discount Rs%s)", promo, res.coupon_applied,
             summ.get("applied"), res.discount)
    slots = ((slj.get("data") or {}).get("passengers") or {})
    keys = list(slots.keys())
    if len(keys) < n:
        res.error = f"hold returned {len(keys)} passenger slots for {n} passengers"
        return res
    total = ((slj.get("data") or {}).get("breakdown") or {}).get("totalAmount")
    if isinstance(total, (int, float)):
        res.fare_amount = total
        if req.infants:                  # (found live: the flight list's fares leave the infant fee out - it is added when the fare is held)
            fee = ((slj.get("data") or {}).get("breakdown") or {}).get("infantFee")
            fee = fee if isinstance(fee, (int, float)) and fee > 0 else total - best["amount"]
            if fee > 0:
                log.info("infant fee Rs%s total Rs%s", fee, total)
    if until == "sell":
        res.success = True
        return res

    # 4) passenger details
    await s.human_pause(2, 4)
    lead = req.adults[0]
    if passenger_objects(slots, req) is None:
        res.error = "the fare hold did not give a place for every traveller"
        return res
    cname = {"first": lead.first_name.title(), "middle": "", "last": lead.last_name.title(),
             "title": "MR" if lead.gender == "M" else "MS", "suffix": "", "action": "Update"}
    phone = "".join(ch for ch in req.contact_phone if ch.isdigit())[-10:]
    add_body = {"data": {"passengers": [], "extraSeatKeys": [],
                         "agentAccountInfo": {"mobilePhoneCountryCode": None, "mobilePhone": None, "otherPhoneCountryCode": None,
                                              "otherPhone": None, "emailAddress": None, "name": None, "info": None},
                         "bookingcontacts": [{"MobilePhoneCountryCode": "91", "MobilePhone": phone,
                                              "OtherPhoneCountryCode": "91", "OtherPhone": "",
                                              "EmergencyPhoneCountryCode": None, "EmergencyPhone": None,
                                              "EmergencyContactRelationship": None, "isWhatsAppSubscribed": False,
                                              "emailAddress": req.contact_email, "name": cname,
                                              "fareMaskOptionValue": False}]},
                "errors": None}
    order = [LEARNED["infant_style"]] + [i for i in range(len(INFANT_STYLES)) if i != LEARNED["infant_style"]] if req.infants else [0]
    for attempt, idx in enumerate(order):
        add_body["data"]["passengers"] = passenger_objects(slots, req, INFANT_STYLES[idx])
        if attempt:
            await s.human_pause(2, 3)
        ad = await s.call("addpax", U_ADDPAX, "POST", UK_SKY, add_body)
        adj = ad.get("json") or {}
        if ad["status"] in (200, 201) and ((adj.get("data") or {}).get("success")):
            if req.infants:
                LEARNED["infant_style"] = idx
                log.info("infant written as style %s: %s", idx, INFANT_STYLES[idx])
            break
        res.error = _api_error(adj) or f"passenger details rejected (HTTP {ad['status']})"
        if req.infants:
            log.warning("passenger details refused (infant style %s): %s", idx, res.error)
        if not req.infants or "infant" not in (res.error or "").lower():       # only an infant problem is worth another spelling
            return res
    else:
        return res
    if until == "addpax":
        res.success = True
        return res

    # 5) the real PNR: replay the user's manual procedure on IndiGo's own payment page (Net banking -> HDFC ->
    #    Pay -> close the bank popup -> 'Processing payment' -> 'Yes, exit'). IndiGo creates the order itself
    #    and the 30-minute On-Hold PNR appears on the itinerary page.
    await s.human_pause(2, 4)
    if psp is not None:
        for attempt in (1, 2):                    # the gateway is a third-party page: one calm second try often works
            try:
                await psp()
                await s.human_pause(2, 3)
                ii, bd = await _read_record(s)
                if bd.get("recordLocator"):
                    res.pnr, res.kind = bd["recordLocator"], "record_locator"
                    res.hold_until = bd.get("holdDate")
                    res.itinerary = ii.get("json")
                    res.success = True
                    log.info("PNR %s (%s) hold until %s (attempt %d)", res.pnr, bd.get("bookingStatus"), res.hold_until, attempt)
                    return res
                log.warning("payment-gateway steps finished but the itinerary has no record locator yet (attempt %d)", attempt)
            except IndigoBlocked:
                raise
            except Exception as e:  # noqa: BLE001 - never lose the booking because the PSP step failed
                log.warning("payment-gateway/PNR step failed (attempt %d): %s", attempt, e)
            if attempt == 1:
                await s.human_pause(3, 6)
        log.warning("PNR step failed twice; falling back to order id + payment link")
        res.psp_failed = True

    # 6) fallback (or `psp` not supplied): create the order via the API so the customer at least gets the
    #    booking reference and the gateway payment link (valid ~12 min; the fare hold lasts 30 min).
    pay_body = {"productName": "Flight", "channelType": "Web", "plateform": "Web", "applicationName": "Customer",
                "returnUrl": "https://www.goindigo.in/payment-processing.html?isBookingFlow=1",
                "merchantViewUrl": "https://www.goindigo.in/content/experience-fragments/skyplus6ev2_fragment/in/en/passenger-trip-summary/master.html",
                "comments": [{"type": 0, "text": "Msg"}], "additionalFields": {"additionalProp1": "Msg"}}
    pi = await s.call("pay-init", U_PAYINIT, "POST", UK_SKY, pay_body)
    pj = pi.get("json") or {}
    d = pj.get("data") or {}
    if pi["status"] != 200 or not d.get("order_id"):
        res.error = _api_error(pj) or f"payment could not be initiated (HTTP {pi['status']})"
        return res
    res.pnr, res.kind = d["order_id"], "order_id"
    res.pay_url = ((d.get("payment_links") or {}).get("web"))
    if isinstance(d.get("orderAmount"), (int, float)):
        res.fare_amount = d["orderAmount"]
    res.success = True
    return res


def _api_error(j: dict) -> Optional[str]:
    """IndiGo's own words for what it refused (kept long enough to name the field: 'infant.gender must be text' is what makes a fix possible)."""
    errs = (j or {}).get("errors")
    if not errs:
        return None
    if isinstance(errs, list) and errs:
        e = errs[0]
        if isinstance(e, dict):
            return str(e.get("message") or e.get("code") or e)[:700]
        return str(e)[:700]
    return str(errs)[:700]


def pick_trusted_tab(pages: list[dict]) -> Optional[dict]:
    """The human-launched home tab: on goindigo.in but NOT a booking/payment/itinerary page, which are heavy,
    stateful pages (and our own leftovers) that must never be driven or reused."""
    cands = [t for t in pages if "goindigo.in" in t.get("url", "")]
    clean = [t for t in cands if not any(x in t["url"] for x in ("/book/", "payment", "itinerary", "order_id="))]
    return (clean or [])[0] if clean else None


async def open_session(browser: CDPBrowser, steplog: list, session_cls: type = IndigoSession) -> tuple[CDPTab, IndigoSession]:
    tab_info = pick_trusted_tab(await browser.pages())
    if tab_info is None:
        raise CDPError("trusted browser has no goindigo.in home tab open")   # transient: window was closed
    tab = await browser.open_tab(tab_info)
    w, h = await tab.viewport()
    return tab, session_cls(tab, HumanInput(tab, w, h), steplog)
