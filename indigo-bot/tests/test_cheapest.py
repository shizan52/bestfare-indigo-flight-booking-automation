"""The fare the engine picks must be for EXACTLY the airports the customer asked for.

Found live on 2026-11-18: IndiGo's answer for Delhi -> Mumbai also contains journeys from Hindon / Dixon and to Navi Mumbai.
They were cheaper, so an Offer 2 booking came back as DEL -> AMD -> NMI (Navi Mumbai) instead of Mumbai.
"""
import indigo_client as ic


def journey(seg, origin, dest, fare, stops=0, sold=False, active=True):
    return {"segKey": seg, "journeyKey": f"JK-{seg}-{fare}", "stops": stops, "isSold": sold,
            "designator": {"origin": origin, "origination": None, "destination": dest, "departure": "2026-11-28T06:00:00"},
            "passengerFares": [{"fareAvailabilityKey": f"FK-{seg}-{fare}", "totalFareAmount": fare, "productClass": "R",
                                "isActive": active}]}


def answer(*journeys):
    return {"data": {"trips": [{"origin": "DEL", "destination": "BOM", "journeysAvailable": list(journeys)}]}}


# modelled on the real answer of 2026-11-18 (fares are the ones IndiGo returned for the Offer 2 search)
REAL_LIKE = answer(
    journey("DEL353BOM", "DEL", "BOM", 6558),
    journey("DEL6814BOM", "DEL", "BOM", 6747),
    journey("DEL5289NMI", "DEL", "NMI", 6788),
    journey("HDO5175BOM", "HDO", "BOM", 6500 + 549),
    journey("DXN5068BOM", "DXN", "BOM", 8007),
    journey("DELAMDNMI", "DEL", "NMI", 5871, stops=1),
    journey("DELBDQNMI", "DEL", "NMI", 5871, stops=1),
    journey("DELAMDBOM", "DEL", "BOM", 7001, stops=1),
)


def test_without_the_airports_it_is_still_the_plain_cheapest():
    assert ic.cheapest(REAL_LIKE)["seg"] == "DELAMDNMI"                # the old behaviour, which was the bug


def test_only_the_requested_airports_count():
    best = ic.cheapest(REAL_LIKE, "DEL", "BOM")
    assert best["seg"] == "DEL353BOM" and best["amount"] == 6558 and best["stops"] == 0


def test_a_nearby_airport_is_never_booked_even_if_it_is_the_only_flight():
    only_nearby = answer(journey("DELAMDNMI", "DEL", "NMI", 5871, stops=1), journey("HDO5175BOM", "HDO", "BOM", 7049))
    assert ic.cheapest(only_nearby, "DEL", "BOM") is None            # -> "no flights available for this route/date"


def test_cheaper_connection_with_the_right_airports_still_wins():
    best = ic.cheapest(answer(journey("DEL353BOM", "DEL", "BOM", 6558), journey("DELAMDBOM", "DEL", "BOM", 6001, stops=1)),
                       "DEL", "BOM")
    assert best["seg"] == "DELAMDBOM" and best["stops"] == 1


def test_sold_out_and_disabled_fares_are_skipped():
    best = ic.cheapest(answer(journey("DEL1BOM", "DEL", "BOM", 4000, sold=True), journey("DEL2BOM", "DEL", "BOM", 4100, active=False),
                              journey("DEL3BOM", "DEL", "BOM", 5000)), "DEL", "BOM")
    assert best["seg"] == "DEL3BOM"


def test_an_answer_without_airport_fields_is_not_filtered_away():
    j = journey("DEL353BOM", "DEL", "BOM", 6558)
    j["designator"] = {"departure": "2026-11-28T06:00:00"}
    assert ic.cheapest(answer(j), "DEL", "BOM")["seg"] == "DEL353BOM"


def test_the_origination_spelling_is_understood_too():
    j = journey("DEL353BOM", "DEL", "BOM", 6558)
    j["designator"].update({"origin": None, "origination": "DEL"})
    assert ic.cheapest(answer(j), "DEL", "BOM")["seg"] == "DEL353BOM"
    assert ic.cheapest(answer(j), "HDO", "BOM") is None
