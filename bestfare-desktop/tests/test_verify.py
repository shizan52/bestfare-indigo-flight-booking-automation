"""IndiGo's own record of the booking is checked against what the traveller typed.

The record below has the shape IndiGo's real answer had in the live test of 2026-11-18: the phone and the e-mail are MASKED
("98****10", "p****t@example.com" - first and last characters only, four stars in between), the booking carries no promo code,
and the passenger names / gender / route / date are in the fields used here.
"""
import copy

import verify
from indigo_client import BookingRequest, Passenger

REQ = BookingRequest("DEL", "BOM", "28-11-2026", [Passenger("RAHUL KUMAR", "SHARMA", "M"), Passenger("PRIYA", "SHARMA", "F")],
                     "offer2", "Rahul.Sharma@Gmail.com", "+91 98765-43210")

RECORD = {"data": {
    "bookingDetails": {"recordLocator": "ABC123", "promoCode": None, "holdDate": "2026-11-18T05:30:00Z", "bookingStatus": "On-Hold"},
    "journeysDetail": [{"journeydetail": {"origin": "DEL", "destination": "BOM", "departure": "2026-11-28T06:15:00"}}],
    "passengers": [{"name": {"first": "PRIYA", "last": "SHARMA", "title": "MS"}, "info": {"gender": 2}},
                   {"name": {"first": "RAHUL KUMAR", "last": "SHARMA", "title": "MR"}, "info": {"gender": 1}}],
    "contacts": [{"homePhoneCountryCode": "91", "homePhone": "98****10", "otherPhoneCountryCode": "91", "otherPhone": "",
                  "emailAddress": "r****a@gmail.com", "name": {"first": "RAHUL", "last": "SHARMA"}}]}}


def by_key(checks):
    return {c.key: c for c in checks}


def test_a_correct_booking_is_confirmed_even_though_indigo_masks_the_contact():
    checks = by_key(verify.check(REQ, RECORD, "PROMO2"))
    assert checks["names"].ok is True and checks["route"].ok is True and checks["date"].ok is True
    assert checks["phone"].ok is True and checks["email"].ok is True, [(c.key, c.ok, c.text) for c in checks.values()]
    assert checks["offer"].ok is None                                  # the record names no offer: that is not a failure
    status, note = verify.summarize(list(checks.values()))
    assert status == "ok" and "match" in note


def test_names_are_compared_without_case_spaces_or_punctuation():
    rec = copy.deepcopy(RECORD)
    rec["data"]["passengers"][1]["name"] = {"first": "Rahul  Kumar", "last": "Sharma"}
    req = BookingRequest("DEL", "BOM", "28-11-2026", [Passenger("D'SOUZA", "MARY-ANN", "F")], "offer1", "a@b.co", "9000000001")
    rec2 = {"data": {"passengers": [{"name": {"first": "DSOUZA", "last": "MARYANN"}, "info": {"gender": 2}}]}}
    assert by_key(verify.check(REQ, rec, "PROMO2"))["names"].ok is True
    assert by_key(verify.check(req, rec2, "PROMO2"))["names"].ok is True


def test_every_kind_of_difference_is_caught_and_explained():
    cases = {
        "names": lambda r: r["data"]["passengers"][0]["name"].update(first="PRIYAA"),
        "date": lambda r: r["data"]["journeysDetail"][0]["journeydetail"].update(departure="2026-11-29T06:15:00"),
        "route": lambda r: r["data"]["journeysDetail"][0]["journeydetail"].update(destination="NMI"),
        "phone": lambda r: r["data"]["contacts"][0].update(homePhone="90****00"),
        "email": lambda r: r["data"]["contacts"][0].update(emailAddress="s****e@else.com"),
        "offer": lambda r: r["data"]["bookingDetails"].update(promoCode="OTHER"),
    }
    for key, mutate in cases.items():
        rec = copy.deepcopy(RECORD)
        mutate(rec)
        c = by_key(verify.check(REQ, rec, "PROMO2"))
        assert c[key].ok is False and c[key].text, key
        status, note = verify.summarize(list(c.values()))
        assert status == "mismatch" and note.startswith("Please check this before you pay")
    rec = copy.deepcopy(RECORD)
    rec["data"]["passengers"][0]["info"]["gender"] = 1                    # PRIYA stored as Male
    c = by_key(verify.check(REQ, rec, "PROMO2"))
    assert c["names"].ok is False and "Male/Female" in c["names"].text


def test_the_gender_must_not_be_swapped():
    rec = copy.deepcopy(RECORD)
    rec["data"]["passengers"][1]["info"]["gender"] = 2
    assert by_key(verify.check(REQ, rec, "PROMO2"))["names"].ok is False


def test_a_record_that_says_little_is_not_called_ok():
    assert verify.summarize(verify.check(REQ, {"data": {"bookingDetails": {"recordLocator": "X"}}}))[0] == "unknown"
    assert verify.summarize(verify.check(REQ, None))[0] == "unknown"
    checks = verify.check(REQ, {"data": {"bookingDetails": {"recordLocator": "X"}}})
    assert all(c.ok is None for c in checks)


def test_the_offer_is_never_failed_by_a_record_that_does_not_mention_it():
    """(IndiGo's fare-hold answer proves the discount; the record of an on-hold booking carries neither a code nor a discount.)"""
    rec = copy.deepcopy(RECORD)
    assert by_key(verify.check(REQ, rec, "PROMO2"))["offer"].ok is None
    rec["data"]["bookingDetails"]["promoCode"] = "promo2"
    assert by_key(verify.check(REQ, rec, "PROMO2"))["offer"].ok is True
    rec["data"]["bookingDetails"]["promoCode"] = ""
    rec["data"]["priceBreakdown"] = {"totalDiscount": 544.0}
    assert by_key(verify.check(REQ, rec, "PROMO2"))["offer"].ok is True


def test_the_phone_matches_whichever_way_it_is_shown():
    for stored in ("9876543210", "919876543210", "+91-9876543210", "09876543210", "98****10", "91****10", "9876****10"):
        rec = copy.deepcopy(RECORD)
        rec["data"]["contacts"][0]["homePhone"] = stored
        assert by_key(verify.check(REQ, rec, "PROMO2"))["phone"].ok is True, stored
    for stored in ("9876543211", "99****10", "98****11", "919000000000"):
        rec = copy.deepcopy(RECORD)
        rec["data"]["contacts"][0]["homePhone"] = stored
        assert by_key(verify.check(REQ, rec, "PROMO2"))["phone"].ok is False, stored
    for stored in ("", "******", "9*****"):                               # too little shown to say anything
        rec = copy.deepcopy(RECORD)
        rec["data"]["contacts"][0]["homePhone"] = stored
        assert by_key(verify.check(REQ, rec, "PROMO2"))["phone"].ok is None, stored


def test_the_email_matches_masked_or_plain_and_ignores_case():
    for stored in ("r****a@gmail.com", "R****A@GMAIL.COM", "rahul.sharma@gmail.com", "Rahul.Sharma@Gmail.com", "ra****ma@gmail.com"):
        rec = copy.deepcopy(RECORD)
        rec["data"]["contacts"][0]["emailAddress"] = stored
        assert by_key(verify.check(REQ, rec, "PROMO2"))["email"].ok is True, stored
    for stored in ("r****a@yahoo.com", "x****a@gmail.com", "r****b@gmail.com", "rahul.sharma@gmail.co"):
        rec = copy.deepcopy(RECORD)
        rec["data"]["contacts"][0]["emailAddress"] = stored
        assert by_key(verify.check(REQ, rec, "PROMO2"))["email"].ok is False, stored
    rec = copy.deepcopy(RECORD)
    rec["data"]["contacts"][0]["emailAddress"] = None
    assert by_key(verify.check(REQ, rec, "PROMO2"))["email"].ok is None


def test_the_booking_must_be_on_the_flight_that_was_picked():
    picked = {"dep": "2026-11-28T06:15:00", "arr": "2026-11-28T08:45:00"}
    c = by_key(verify.check(REQ, RECORD, "PROMO2", picked))
    assert c["flight"].ok is True
    other = {"dep": "2026-11-28T09:40:00"}
    c = by_key(verify.check(REQ, RECORD, "PROMO2", other))
    assert c["flight"].ok is False and "different flight" in c["flight"].text and "06:15 instead of 09:40" in c["flight"].text
    status, note = verify.summarize(list(c.values()))
    assert status == "mismatch" and "different flight" in note


def test_without_a_picked_flight_there_is_no_flight_check_and_a_record_without_a_time_says_nothing():
    assert "flight" not in by_key(verify.check(REQ, RECORD, "PROMO2"))
    rec = copy.deepcopy(RECORD)
    rec["data"]["journeysDetail"][0]["journeydetail"].pop("departure")
    assert by_key(verify.check(REQ, rec, "PROMO2", {"dep": "2026-11-28T06:15:00"}))["flight"].ok is None


def test_a_correct_booking_on_the_picked_flight_is_confirmed():
    status, note = verify.summarize(verify.check(REQ, RECORD, "PROMO2", {"dep": "2026-11-28T06:15:00"}))
    assert status == "ok"


# ── children and infants ─────────────────────────────────────────────────────────────────────────────────────────
def _family_request():
    from indigo_client import BookingRequest, Passenger
    return BookingRequest("DEL", "BOM", "09-12-2026", [Passenger("RAHUL", "SHARMA", "M"), Passenger("ARYA", "SHARMA", "F", "25-03-2019", "child"),
                                                       Passenger("VIHAAN", "SHARMA", "M", "10-06-2026", "infant")], "offer1", "a@b.co", "9876543210")


def _record(passengers):
    return {"data": {"passengers": passengers, "journeysDetail": [{"journeydetail": {"origin": "DEL", "destination": "BOM", "departure": "2026-12-09T06:00:00"}}],
                     "contacts": [{"homePhone": "98****10", "emailAddress": "a@b.co"}]}}


def _p(first, last, gender, **more):
    return {"name": {"first": first, "last": last}, "info": {"gender": gender}, **more}


def _names(checks):
    return next(c for c in checks if c.key == "names")


def test_a_family_whose_infant_is_a_passenger_of_its_own_is_confirmed():
    rec = _record([_p("Rahul", "Sharma", 1), _p("Arya", "Sharma", 2, passengerTypeCode="CHD"), _p("Vihaan", "Sharma", 1, passengerTypeCode="INFT")])
    assert _names(verify.check(_family_request(), rec)).ok is True


def test_a_family_whose_infant_sits_inside_the_adult_is_confirmed():
    rec = _record([_p("Rahul", "Sharma", 1, infant={"name": {"first": "Vihaan", "last": "Sharma"}, "gender": 1}), _p("Arya", "Sharma", 2)])
    assert _names(verify.check(_family_request(), rec)).ok is True


def test_an_answer_that_does_not_list_the_infant_still_confirms_the_grown_ups_and_children():
    rec = _record([_p("Rahul", "Sharma", 1), _p("Arya", "Sharma", 2)])
    assert _names(verify.check(_family_request(), rec)).ok is True


def test_a_wrong_child_or_a_wrong_baby_name_is_caught():
    bad_child = _record([_p("Rahul", "Sharma", 1), _p("Anya", "Sharma", 2)])
    assert _names(verify.check(_family_request(), bad_child)).ok is False
    bad_baby = _record([_p("Rahul", "Sharma", 1, infant={"name": {"first": "Vivaan", "last": "Sharma"}, "gender": 1}), _p("Arya", "Sharma", 2)])
    assert _names(verify.check(_family_request(), bad_baby)).ok is False


def test_the_dates_of_birth_of_the_children_and_the_baby_are_compared_with_indigos_record():
    req = _family_request()
    good = _record([_p("Rahul", "Sharma", 1, infant={"dateOfBirth": "2026-06-10T00:00:00", "gender": 1, "name": {}}), _p("Arya", "Sharma", 2, info={"gender": 2, "dateOfBirth": "2019-03-25T00:00:00"})])
    assert next(c for c in verify.check(req, good) if c.key == "born").ok is True
    wrong_child = _record([_p("Rahul", "Sharma", 1, infant={"dateOfBirth": "2026-06-10T00:00:00", "name": {}}), _p("Arya", "Sharma", 2, info={"gender": 2, "dateOfBirth": "2018-03-25T00:00:00"})])
    c = next(c for c in verify.check(req, wrong_child) if c.key == "born")
    assert c.ok is False and "date of birth" in c.text
    assert verify.summarize(verify.check(req, wrong_child))[0] == "mismatch"
    silent = _record([_p("Rahul", "Sharma", 1), _p("Arya", "Sharma", 2)])
    assert next(c for c in verify.check(req, silent) if c.key == "born").ok is None           # nothing shown: nothing to say
