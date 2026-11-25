"""Check IndiGo's own record of a booking against what the traveller typed.

After the PNR exists, IndiGo's booking record (the itinerary answer) says which names, route, date, phone, email and offer it
stored. This compares them with the order, so a wrong date or a mangled name is caught the moment the booking exists - while it
can still be fixed - instead of at the airport. Pure Python; the shapes are those of IndiGo's real answer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import stations


@dataclass
class Check:
    key: str                 # names | route | date | phone | email | offer
    ok: Optional[bool]       # True = matches, False = differs, None = IndiGo's record does not say
    text: str                # plain words (what was found when it differs)


def _letters(s: str) -> str:
    return re.sub(r"[^A-Z]", "", (s or "").upper())


def _iso_day(dmy: str) -> str:
    """'25-03-2019' -> '2019-03-25' (what IndiGo's record shows)."""
    try:
        return datetime.strptime(dmy, "%d-%m-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _nice(d) -> str:
    """'2026-11-28' or a date -> '28 Nov 2026'."""
    try:
        if isinstance(d, str):
            d = datetime.strptime(d[:10], "%Y-%m-%d").date()
        return f"{d.day} {d.strftime('%b %Y')}"
    except (ValueError, TypeError):
        return str(d)


def _g(obj, *path):
    for k in path:
        if isinstance(obj, list):
            obj = obj[k] if isinstance(k, int) and -len(obj) <= k < len(obj) else None
        elif isinstance(obj, dict):
            obj = obj.get(k)
        else:
            return None
    return obj


def _same_masked(shown: str, actual: str, keep_case: bool = True) -> Optional[bool]:
    """Does what IndiGo shows (maybe with the middle hidden by '*') fit the real value? None = nothing useful is shown."""
    if not shown:
        return None
    if not keep_case:
        shown, actual = shown.lower(), actual.lower()
    if "*" not in shown:
        return shown == actual
    head, tail = shown[:shown.index("*")], shown[shown.rindex("*") + 1:]
    if len(head) + len(tail) < 3:                        # "****" or "9****": too little is shown to say anything
        return None
    return len(actual) >= len(head) + len(tail) and actual.startswith(head) and actual.endswith(tail)


def _phone_matches(shown: str, want10: str) -> Optional[bool]:
    """A stored phone ('9876543210', '+91-98765 43210', '98****10') against the 10 digits typed. None = not shown."""
    s = re.sub(r"[^\d*]", "", shown or "")
    if not s.strip("*"):
        return None
    if "*" in s:
        found = [_same_masked(s, w) for w in (want10, "91" + want10)]      # (the country code may or may not be part of it)
        if any(found):
            return True
        return None if all(v is None for v in found) else False
    return s.endswith(want10) or s.lstrip("0").endswith(want10)


def check(req, itinerary: Optional[dict], promo: str = "", flight: Optional[dict] = None) -> list[Check]:
    """`req` is the engine's BookingRequest (origin, destination, date DD-MM-YYYY, passengers, contact_email, contact_phone).
    `flight` = the flight the traveller chose (a flight_options() entry), when the booking came from a choice."""
    d = (itinerary or {}).get("data") or {}
    out: list[Check] = []

    # ── names and gender ─────────────────────────────────────────────────────────────────────────────────
    # (children have a seat and are listed like adults; an infant is listed either as a passenger of its own or inside the adult who carries it -
    #  and some answers do not list infants at all: then the grown-ups and children are compared, and the infants are simply not confirmed)
    kind_of = lambda p: getattr(p, "kind", "adult")                                      # noqa: E731
    want = sorted((_letters(p.first_name + p.last_name), 1 if p.gender == "M" else 2) for p in req.passengers)
    want_seats = sorted((_letters(p.first_name + p.last_name), 1 if p.gender == "M" else 2) for p in req.passengers if kind_of(p) != "infant")
    got_p = d.get("passengers")
    if isinstance(got_p, list) and got_p:
        def one(p):
            return (_letters((_g(p, "name", "first") or "") + (_g(p, "name", "last") or "")), _g(p, "info", "gender"))
        got_seats, got_babies = [], []
        for p in got_p:
            (got_babies if str(p.get("passengerTypeCode") or p.get("type") or "").upper() == "INFT" else got_seats).append(one(p))
            inf = p.get("infant")
            if isinstance(inf, dict) and inf:
                got_babies.append((_letters((_g(inf, "name", "first") or "") + (_g(inf, "name", "last") or "")), _g(inf, "gender") or _g(inf, "info", "gender")))
        got_seats.sort()
        got_babies.sort()
        got = sorted(got_seats + got_babies) if got_babies else got_seats
        if not got_babies:                                    # the record names no infant: only who has a seat can be compared
            want = want_seats
        same_names = [w[0] for w in want] == [g[0] for g in got]
        same_gender = all(g[1] in (None, w[1]) for w, g in zip(want, got)) if same_names else True
        if same_names and same_gender:
            out.append(Check("names", True, ""))
        elif not same_names:
            out.append(Check("names", False, "IndiGo's booking has different traveller names"))
        else:
            out.append(Check("names", False, "IndiGo's booking has a different Male/Female choice"))
    else:
        out.append(Check("names", None, ""))

    # ── dates of birth of the children and the baby (the record keeps them; the baby's NAME it hides) ───────────
    kids = sorted(_iso_day(p.dob) for p in req.passengers if getattr(p, "kind", "adult") != "adult" and p.dob)
    if kids and isinstance(got_p, list):
        seen: list[str] = []
        for p in got_p:
            if isinstance(p, dict):
                if _g(p, "info", "dateOfBirth"):
                    seen.append(str(_g(p, "info", "dateOfBirth"))[:10])
                if isinstance(p.get("infant"), dict) and p["infant"].get("dateOfBirth"):
                    seen.append(str(p["infant"]["dateOfBirth"])[:10])
        if seen:
            ok = sorted(seen) == kids
            out.append(Check("born", ok, "" if ok else "IndiGo's booking has a different date of birth for a child or baby"))
        else:
            out.append(Check("born", None, ""))

    # ── route and date ───────────────────────────────────────────────────────────────────────────────────
    jd = _g(d, "journeysDetail", 0, "journeydetail") or {}
    o, dst = _g(jd, "origin"), _g(jd, "destination")
    if o and dst:
        ok = (o, dst) == (req.origin, req.destination)
        out.append(Check("route", ok, "" if ok else f"IndiGo's booking has a different route ({stations.label(o)} → {stations.label(dst)} "
                                                      f"instead of {stations.label(req.origin)} → {stations.label(req.destination)})"))
    else:
        out.append(Check("route", None, ""))
    dep = _g(jd, "departure")
    if isinstance(dep, str) and len(dep) >= 10:
        want_day = datetime.strptime(req.date, "%d-%m-%Y").date()
        ok = dep[:10] == want_day.isoformat()
        out.append(Check("date", ok, "" if ok else f"IndiGo's booking has a different travel date ({_nice(dep[:10])} instead of {_nice(want_day)})"))
    else:
        out.append(Check("date", None, ""))
    if flight:                                                # the booking must be on the flight that was picked, not just the same day
        want = str(flight.get("dep") or "")
        if isinstance(dep, str) and len(dep) >= 16 and len(want) >= 16:
            ok = dep[:16] == want[:16]
            out.append(Check("flight", ok, "" if ok else
                             f"IndiGo's booking is on a different flight (it leaves at {dep[11:16]} instead of {want[11:16]})"))
        else:
            out.append(Check("flight", None, ""))

    # ── contact ──────────────────────────────────────────────────────────────────────────────────────────
    # IndiGo hides the middle of the contact details in its record ("98****10", "p****t@example.com"): what is shown is compared.
    contact = _g(d, "contacts", 0) or {}
    want_phone = _digits(req.contact_phone)[-10:]
    verdicts = [_phone_matches(str(contact.get(k) or ""), want_phone) for k in ("homePhone", "otherPhone", "mobilePhone", "workPhone")]
    verdicts = [v for v in verdicts if v is not None]
    if verdicts:
        ok = any(verdicts)
        out.append(Check("phone", ok, "" if ok else "IndiGo's booking has a different mobile number"))
    else:
        out.append(Check("phone", None, ""))
    ok = _same_masked(str(contact.get("emailAddress") or "").strip(), req.contact_email.strip(), keep_case=False)
    out.append(Check("email", ok, "" if ok is not False else "IndiGo's booking has a different email address"))

    # ── the offer ────────────────────────────────────────────────────────────────────────────────────────
    # (the record of an on-hold booking usually names no offer; the discount itself is proved by IndiGo's fare-hold answer,
    #  which the engine reads. So the record can only confirm or contradict - never "fail" for saying nothing.)
    code = str(_g(d, "bookingDetails", "promoCode") or "").strip()
    discount = _g(d, "priceBreakdown", "totalDiscount")
    if code:
        ok = code.upper() == promo.upper()
        out.append(Check("offer", ok, "" if ok else "IndiGo's booking carries a different offer"))
    elif isinstance(discount, (int, float)) and discount > 0:
        out.append(Check("offer", True, ""))
    else:
        out.append(Check("offer", None, ""))
    return out


def summarize(checks: list[Check]) -> tuple[str, str]:
    """('ok' | 'mismatch' | 'unknown', one plain sentence). 'ok' needs the names, route and date to be confirmed."""
    bad = [c.text for c in checks if c.ok is False]
    if bad:
        return "mismatch", "Please check this before you pay: " + "; ".join(bad) + "."
    confirmed = {c.key for c in checks if c.ok is True}
    if {"names", "route", "date"} <= confirmed:
        return "ok", "Checked against IndiGo's booking: names, route, date and contact match."
    return "unknown", ""
