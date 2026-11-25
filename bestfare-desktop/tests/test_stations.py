import json

import stations


def codes(text, n=8):
    return [s.code for s in stations.search(text, n)]


def test_the_list_is_indigos_own_domestic_airports():
    st = stations.all_stations()
    assert len(st) >= 90 and all(s.airport for s in st)
    assert {"DEL", "BOM", "BLR", "CCU", "MAA", "HYD", "GOI", "GOX", "IXZ"} <= {s.code for s in st}
    data = json.loads(__import__("resources").read_text(stations.FILE))
    assert data["source"] == "IndiGo station search" and len(data["stations"]) == len(st)


def test_typing_a_few_letters_finds_the_airport():
    assert codes("del")[0] == "DEL" and codes("bang") == ["BLR"] and codes("pun") == ["PNQ"]
    assert codes("kol")[:2] == ["CCU", "KLH"]
    assert codes("goa") == ["GOI", "GOX"]
    assert codes("mum") == ["BOM", "NMI"]


def test_old_and_other_names_work():
    assert codes("bombay") == ["BOM"] and codes("calcutta") == ["CCU"] and codes("cochin") == ["COK"]
    assert codes("vizag") == ["VTZ"] and codes("new delhi") == ["DEL"] and codes("port blair") == ["IXZ"]


def test_the_airport_name_and_the_code_work_too():
    assert codes("indira") == ["DEL"] and codes("shivaji") == ["BOM"] and codes("hyd")[0] == "HYD"


def test_empty_box_shows_the_popular_airports_first():
    assert codes("", 4) == ["DEL", "BOM", "BLR", "CCU"]
    assert codes("zzzz") == []


def test_best_match_first_and_exact_beats_partial():
    assert codes("d")[0] == "DEL"                                  # popular first among many
    assert codes("rajkot")[:2] == ["HSR", "RAJ"] or set(codes("rajkot")) == {"HSR", "RAJ"}


def test_resolve_needs_exactly_one_airport():
    assert stations.resolve("Delhi (DEL)") == "DEL" and stations.resolve("bangalore") == "BLR" and stations.resolve("del") == "DEL"
    assert stations.resolve("goa") is None and stations.resolve("rajkot") is None and stations.resolve("nowhere") is None
    assert stations.resolve("") is None


def test_labels():
    assert stations.label("BOM") == "Mumbai (BOM)" and stations.city("BLR") == "Bengaluru" and stations.label("ZZZ") == "ZZZ"
    assert stations.known("DEL") and not stations.known("ZZZ") and not stations.known(None)
