"""Regression: with a leftover booking/itinerary tab open, the engine must still drive only the clean home tab."""


def T(url, i="X"):
    return {"id": i, "url": url, "title": ""}


def test_prefers_clean_home_tab_over_booking_pages():
    from indigo_client import pick_trusted_tab
    pages = [T("https://www.goindigo.in/book/itinerary.html?order_id=JFTWE1&payment_status=INPROCESS", "I"),
             T("https://www.goindigo.in/payment.html?isBookingFlow=1", "P"),
             T("https://www.goindigo.in/?linkNav=Logo%7C%7CHeader", "H")]
    assert pick_trusted_tab(pages)["id"] == "H"


def test_never_falls_back_to_a_booking_page():
    from indigo_client import pick_trusted_tab
    pages = [T("https://www.goindigo.in/book/flight-select.html", "F"), T("https://payments.juspay.in/x", "J")]
    assert pick_trusted_tab(pages) is None            # -> CDPError -> retryable "browser unavailable"


def test_ignores_other_sites():
    from indigo_client import pick_trusted_tab
    assert pick_trusted_tab([T("https://example.com/", "E"), T("edge://newtab/", "N")]) is None
