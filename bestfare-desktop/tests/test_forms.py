from datetime import timedelta

import forms
from forms import Order, Traveller


def good(**kw) -> Order:
    o = Order(origin="DEL", dest="BOM", day=forms.today_ist() + timedelta(days=5),
              travellers=[Traveller("rahul kumar", "sharma", "M")], phone="+91 98765-43210",
              email=" Rahul.Sharma@Gmail.com ", offer="offer1", eligible=True)
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def test_a_good_order_passes_and_is_cleaned():
    o = good()
    assert forms.validate(o) == {}
    assert o.travellers[0].first == "RAHUL KUMAR" and o.travellers[0].last == "SHARMA"
    assert o.phone == "9876543210"
    assert o.email == "rahul.sharma@gmail.com"


def test_every_mistake_is_named_on_its_own_field():
    o = Order(origin="DEL", dest="DEL", day=forms.today_ist() - timedelta(days=1),
              travellers=[Traveller("राहुल", "", "")], phone="5123456789", email="nope", offer="", eligible=False)
    e = forms.validate(o)
    assert set(e) == {"dest", "day", "t0.first", "t0.last", "t0.gender", "phone", "email", "offer"}
    assert "different" in e["dest"]
    assert "English letters" in e["t0.first"]
    assert "surname" in e["t0.last"].lower()


def test_there_is_no_eligibility_question_any_more_only_the_offer_must_be_chosen():
    assert forms.validate(good(eligible=False)) == {}                       # (an old record with the field unset is just as good)
    assert forms.validate(good(offer="")) == {"offer": "Choose the offer."}


def test_dates_outside_what_indigo_sells_are_refused():
    t = forms.today_ist()
    assert forms.check_day(t, t) is None
    assert forms.check_day(t + timedelta(days=forms.HORIZON_DAYS), t) is None
    assert "far ahead" in forms.check_day(t + timedelta(days=forms.HORIZON_DAYS + 1), t)
    assert "passed" in forms.check_day(t - timedelta(days=1), t)


def test_too_many_travellers():
    e = forms.validate(good(travellers=[Traveller("A", "B", "M") for _ in range(forms.MAX_TRAVELLERS + 1)]))
    assert "travellers" in e


def test_airport_text_to_code():
    assert forms.airport_code("Delhi (DEL)") == "DEL"
    assert forms.airport_code("bangalore") == "BLR"
    assert forms.airport_code("Vizag") == "VTZ"
    assert forms.airport_code("bom") == "BOM"
    assert forms.airport_code("goa") is None                                        # two airports: the person has to choose
    assert forms.airport_code("xyzzy") is None
    assert forms.airport_code("") is None


def test_one_offer_is_one_booking_and_both_is_not_an_offer():
    assert good(offer="offer1").bookings == ["offer1"] and good(offer="offer2").bookings == ["offer2"]
    assert good(offer="").bookings == [] and good(offer="both").bookings == []
    assert forms.OFFER_ORDER == ("offer1", "offer2") and "both" not in forms.OFFER_TITLE
    assert forms.validate(good(offer="both")).get("offer")


def test_request_for_the_engine():
    o = good()
    forms.validate(o)
    r = forms.to_request(o, "offer2")
    assert r.date == o.day.strftime("%d-%m-%Y")
    assert r.offer == "offer2" and r.origin == "DEL" and r.destination == "BOM"
    assert r.passengers[0].first_name == "RAHUL KUMAR" and r.passengers[0].gender == "M"


def test_promo_codes_match_the_coupon_extension():
    assert not hasattr(forms, "PROMO")                                  # no offer code lives in this program (the server hands it out)


def test_words():
    assert forms.money(5132.4) == "₹5,132"
    assert forms.money(None) == ""
    assert forms.travellers_word(1) == "1 traveller" and forms.travellers_word(3) == "3 travellers"
