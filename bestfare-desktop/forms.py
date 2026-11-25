"""What the user types, checked with the same rules the Telegram bot uses (pure Python - no Qt, no browser).

Every check returns a plain-English sentence for the user, shown right under the field that needs fixing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import paths

paths.use_engine()
import stations  # noqa: E402  (IndiGo's own airport list + friendly old names)

MAX_TRAVELLERS = 6                    # adults + children in one booking (the people who take a seat); infants ride on an adult's lap
ADULT, CHILD, INFANT = "adult", "child", "infant"
KINDS = (ADULT, CHILD, INFANT)
MAX_CHILDREN = MAX_INFANTS = 4                # IndiGo's own passenger picker: at most 4 children and 4 infants in one booking
CHILD_MIN_YEARS, ADULT_FROM_YEARS = 2, 12      # IndiGo: a child is 2 to 11 on the day of the flight, from 12 an adult
INFANT_MIN_DAYS = 3                            # ... an infant is from 3 days old up to the day before the 2nd birthday
HORIZON_DAYS = 330                    # IndiGo sells about this far ahead
NAME_TOKEN = re.compile(r"[A-Za-z][A-Za-z.'-]{0,28}")
EMAIL_RE = re.compile(r"[a-z0-9._%+-]{1,64}@[a-z0-9-]{1,63}(\.[a-z0-9-]{1,63}){1,3}")

# The offers' promo codes are NOT in this program: the server hands the code over for one booking at a time, after it has taken the credits.
OFFER_TITLE = {"offer1": "Offer 1", "offer2": "Offer 2"}
# (What an offer really is - who it is for - is known to the admin only: the traveller sees "Offer 1" and "Offer 2", nothing else.)
OFFER_ORDER = ("offer1", "offer2")                       # one offer per booking - never both


def today_ist() -> date:
    """Flights are sold on Indian dates."""
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()


def last_bookable(today: Optional[date] = None) -> date:
    return (today or today_ist()) + timedelta(days=HORIZON_DAYS)


@dataclass
class Traveller:
    first: str = ""
    last: str = ""
    gender: str = ""                  # 'M' | 'F'
    kind: str = ADULT                 # 'adult' | 'child' | 'infant'
    dob: str = ""                     # a child's and an infant's date of birth: as typed, DD-MM-YYYY once the order has been checked

    @property
    def full(self) -> str:
        return f"{self.first} {self.last}".strip()


@dataclass
class Order:
    origin: str = ""
    dest: str = ""
    day: Optional[date] = None
    travellers: list[Traveller] = field(default_factory=lambda: [Traveller()])
    phone: str = ""
    email: str = ""
    offer: str = ""
    eligible: bool = True                      # (not asked any more: the traveller knows who the offer is for; kept so older records and tests still read)

    def counts(self) -> tuple[int, int, int]:
        """(adults, children, infants)"""
        return (sum(t.kind == ADULT for t in self.travellers), sum(t.kind == CHILD for t in self.travellers),
                sum(t.kind == INFANT for t in self.travellers))

    @property
    def seats(self) -> int:
        """The people who take a seat of their own: adults + children."""
        a, c, _i = self.counts()
        return a + c

    def records(self) -> list[dict]:
        """The travellers as the history keeps them (and the engine reads them back)."""
        return [{"first": t.first, "last": t.last, "gender": t.gender, "kind": t.kind, "dob": t.dob if t.kind != ADULT else ""}
                for t in self.travellers]

    @property
    def bookings(self) -> list[str]:
        """One booking, one offer: IndiGo takes a single promo code per booking."""
        return [self.offer] if self.offer in OFFER_ORDER else []


# ── single fields ────────────────────────────────────────────────────────────────────────────────────
def clean_name_part(text: str, what: str) -> tuple[str, Optional[str]]:
    parts = (text or "").replace(",", " ").split()
    if not parts:
        return "", f"Please type the {what}."
    if any(not NAME_TOKEN.fullmatch(p) for p in parts):
        return "", "Please use English letters only (as written on the ID)."
    value = " ".join(parts).upper()
    if len(value) > 32:
        return "", "This name is too long — please shorten it."
    return value, None


def clean_phone(text: str) -> tuple[str, Optional[str]]:
    digits = re.sub(r"\D", "", text or "")
    if len(digits) > 10 and digits.startswith(("91", "0")):
        digits = digits[-10:]
    if not re.fullmatch(r"[6-9]\d{9}", digits):
        return "", "Type a 10-digit Indian mobile number (it starts with 6, 7, 8 or 9)."
    return digits, None


def clean_email(text: str) -> tuple[str, Optional[str]]:
    t = (text or "").strip().lower()
    if not EMAIL_RE.fullmatch(t):
        return "", "That email address does not look right. Example: name@gmail.com"
    return t, None


def parse_dob(text: str) -> Optional[date]:
    """'25/03/2019', '25-03-2019', '25.03.2019' or '25032019' -> the date; None when it is not a real date."""
    t = (text or "").strip()
    m = re.fullmatch(r"(\d{1,2})[/\-. ](\d{1,2})[/\-. ](\d{4})", t) or re.fullmatch(r"(\d{2})(\d{2})(\d{4})", t)
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def years_on(born: date, day: date) -> int:
    """Whole years completed on `day`."""
    return day.year - born.year - ((day.month, day.day) < (born.month, born.day))


def age_words(born: date, day: date) -> str:
    """'5 years', '1 year 3 months', '10 months', '12 days' - how old someone is on the travel date."""
    y = years_on(born, day)
    if y >= 2:
        return f"{y} years"
    months = (day.year - born.year) * 12 + day.month - born.month - (day.day < born.day)
    if months >= 1:
        y, m = divmod(months, 12)
        return (f"{y} year{'s' if y != 1 else ''} " if y else "") + (f"{m} month{'s' if m != 1 else ''}" if m or not y else "").strip()
    d = (day - born).days
    return f"{d} day{'s' if d != 1 else ''}"


def check_dob(kind: str, text: str, travel_day: Optional[date]) -> tuple[str, Optional[str]]:
    """A child's / infant's date of birth, checked against the travel date (the age counts on the day of the flight).
    Returns (DD-MM-YYYY, None) or ("", a sentence for the traveller)."""
    what = "child" if kind == CHILD else "baby"
    if not (text or "").strip():
        return "", f"Please type the {what}'s date of birth — IndiGo asks for it."
    born = parse_dob(text)
    if born is None:
        return "", "Type the date of birth like 25/03/2019."
    if born > today_ist():
        return "", "That date has not come yet."
    if travel_day is None:
        return born.strftime("%d-%m-%Y"), None
    if born > travel_day:
        return "", f"The {what} is not born yet on the travel date."
    y = years_on(born, travel_day)
    if kind == CHILD:
        if y >= ADULT_FROM_YEARS:
            return "", (f"This child will be {y} years old on the travel date. IndiGo books everyone from {ADULT_FROM_YEARS} years as an adult — "
                        "please change this traveller to Adult.")
        if y < CHILD_MIN_YEARS:
            return "", ("This child will be under 2 years old on the travel date. IndiGo takes children under 2 as infants — "
                        "please change this traveller to Infant.")
    else:
        if y >= CHILD_MIN_YEARS:
            return "", ("This baby will be 2 years or older on the travel date. IndiGo takes them as a child (with a seat) — "
                        "please change this traveller to Child.")
        if (travel_day - born).days < INFANT_MIN_DAYS:
            return "", f"IndiGo takes babies from {INFANT_MIN_DAYS} days old — this baby will be too young on the travel date."
    return born.strftime("%d-%m-%Y"), None


def record_mix(travellers: list[dict]) -> tuple[int, int, int]:
    """(adults, children, infants) of a saved booking's travellers (older bookings have no kind: all adults)."""
    kinds = [(t.get("kind") or ADULT) for t in travellers or []]
    return kinds.count(ADULT) + sum(k not in KINDS for k in kinds), kinds.count(CHILD), kinds.count(INFANT)


def mix_text(adults: int, children: int, infants: int) -> str:
    """For a screen: '2 travellers' when everyone is an adult, otherwise '2 adults, 1 child, 1 infant'."""
    if not children and not infants:
        return travellers_word(adults)
    return group_words(adults, children, infants)


def group_words(adults: int, children: int, infants: int) -> str:
    """'2 adults, 1 child, 1 infant'"""
    parts = []
    if adults:
        parts.append(f"{adults} adult{'s' if adults != 1 else ''}")
    if children:
        parts.append(f"{children} child{'ren' if children != 1 else ''}")
    if infants:
        parts.append(f"{infants} infant{'s' if infants != 1 else ''}")
    return ", ".join(parts)


def check_day(day: Optional[date], today: Optional[date] = None) -> Optional[str]:
    today = today or today_ist()
    if day is None:
        return "Please choose the travel date."
    if day < today:
        return "This date has already passed."
    if day > last_bookable(today):
        return "IndiGo does not sell tickets that far ahead yet."
    return None


def airport_code(text: str) -> Optional[str]:
    """'Delhi (DEL)', 'del', 'bangalore' -> the code; None when it is not one clear airport."""
    return stations.resolve(text or "")


# ── the whole form ───────────────────────────────────────────────────────────────────────────────────
def validate(o: Order, today: Optional[date] = None) -> dict[str, str]:
    """{field key: message}. Keys: origin, dest, day, t{i}.first|last|gender, travellers, phone, email, offer.
    An empty dict means the order can be booked. Cleaned values are written back into the order."""
    errors: dict[str, str] = {}
    if not o.origin or not stations.known(o.origin):
        errors["origin"] = "Choose where you fly from."
    if not o.dest or not stations.known(o.dest):
        errors["dest"] = "Choose where you fly to."
    if "origin" not in errors and "dest" not in errors and o.origin == o.dest:
        errors["dest"] = "From and To must be different airports."
    msg = check_day(o.day, today)
    if msg:
        errors["day"] = msg
    adults, children, infants = o.counts()
    if not o.travellers:
        errors["travellers"] = "Add at least one traveller."
    elif adults + children > MAX_TRAVELLERS:
        errors["travellers"] = f"At most {MAX_TRAVELLERS} adults and children in one booking."
    elif adults == 0:
        errors["travellers"] = "Children and infants must travel with an adult — please add an adult."
    elif children > MAX_CHILDREN:
        errors["travellers"] = f"IndiGo takes at most {MAX_CHILDREN} children in one booking."
    elif infants > MAX_INFANTS:
        errors["travellers"] = f"IndiGo takes at most {MAX_INFANTS} infants in one booking."
    elif infants > adults:
        errors["travellers"] = "Each infant travels on one adult's lap — please add an adult for every infant."
    for i, t in enumerate(o.travellers):
        if t.kind not in KINDS:
            t.kind = ADULT
        if t.kind != ADULT:
            dob, e0 = check_dob(t.kind, t.dob, o.day if not errors.get("day") else None)
            if e0:
                errors[f"t{i}.dob"] = e0
            else:
                t.dob = dob
        first, e1 = clean_name_part(t.first, "first name")
        last, e2 = clean_name_part(t.last, "last name (surname)")
        if e1:
            errors[f"t{i}.first"] = e1
        else:
            t.first = first
        if e2:
            errors[f"t{i}.last"] = e2 if t.last.strip() else "Please type the surname. No surname? Type the first name again."
        else:
            t.last = last
        if t.gender not in ("M", "F"):
            errors[f"t{i}.gender"] = "Choose Male or Female."
    phone, e = clean_phone(o.phone)
    if e:
        errors["phone"] = e
    else:
        o.phone = phone
    email, e = clean_email(o.email)
    if e:
        errors["email"] = e
    else:
        o.email = email
    if o.offer not in OFFER_ORDER:
        errors["offer"] = "Choose the offer."
    return errors


def to_request(o: Order, offer: str):
    """The engine's BookingRequest for ONE booking of this order."""
    from indigo_client import BookingRequest, Passenger
    assert o.day is not None, "validate() the order first"
    return BookingRequest(origin=o.origin, destination=o.dest, date=o.day.strftime("%d-%m-%Y"),
                          passengers=[Passenger(first_name=t.first, last_name=t.last, gender=t.gender, kind=t.kind,
                                                dob=t.dob if t.kind != ADULT else "") for t in o.travellers],
                          offer=offer, contact_email=o.email, contact_phone=o.phone)


# ── words for the screen ─────────────────────────────────────────────────────────────────────────────
def money(amount) -> str:
    try:
        return "₹" + f"{float(amount):,.0f}"
    except (TypeError, ValueError):
        return ""


def travellers_word(n: int, infants: int = 0) -> str:
    return f"{n} traveller" + ("" if n == 1 else "s") + (f" + {infants} infant{'s' if infants != 1 else ''}" if infants else "")


def day_long(d: date) -> str:
    return d.strftime("%a, %d %b %Y").replace(" 0", " ")
