"""Indian domestic airports (the IndiGo network): friendly input -> IATA code.

A passenger can type "bangalore", "Vizag" or "BLR" and get the same airport. Names and aliases live in one table, so
adding an airport is one line.   CODE | display name | aliases (comma separated) | short name for buttons (optional)
"""
from __future__ import annotations

import re

_RAW = """
DEL|Delhi|new delhi,ndls,indira gandhi
BOM|Mumbai|bombay,chhatrapati shivaji
NMI|Navi Mumbai|
BLR|Bengaluru|bangalore,bengalooru
MAA|Chennai|madras
CCU|Kolkata|calcutta
HYD|Hyderabad|shamshabad,rajiv gandhi
AMD|Ahmedabad|
PNQ|Pune|poona
GOI|Goa Dabolim|dabolim,panaji|Goa
GOX|Goa Mopa|goa,mopa,north goa,manohar|Goa Mopa
JAI|Jaipur|
LKO|Lucknow|
COK|Kochi|cochin,ernakulam
TRV|Thiruvananthapuram|trivandrum
CCJ|Kozhikode|calicut
CNN|Kannur|cannanore
IXE|Mangaluru|mangalore
IXM|Madurai|
TRZ|Tiruchirappalli|trichy,tiruchirapalli
CJB|Coimbatore|kovai
TCR|Thoothukudi|tuticorin
SXV|Salem|
PNY|Puducherry|pondicherry
VGA|Vijayawada|
VTZ|Visakhapatnam|vizag,vishakhapatnam
TIR|Tirupati|
RJA|Rajahmundry|
CDP|Kadapa|cuddapah
KJB|Kurnool|
IXG|Belagavi|belgaum
HBX|Hubballi|hubli
MYQ|Mysuru|mysore
IXR|Ranchi|
PAT|Patna|
GAY|Gaya|
DBR|Darbhanga|
BBI|Bhubaneswar|
JRG|Jharsuguda|
JGB|Jagdalpur|
RPR|Raipur|
GAU|Guwahati|gauhati
IMF|Imphal|
DIB|Dibrugarh|
JRH|Jorhat|
IXI|Lilabari|north lakhimpur
TEZ|Tezpur|
IXA|Agartala|
DMU|Dimapur|
AJL|Aizawl|
SHL|Shillong|
IXS|Silchar|
IXB|Bagdogra|siliguri
IXZ|Port Blair|andaman
RDP|Durgapur|
NAG|Nagpur|
IDR|Indore|
BHO|Bhopal|
JLR|Jabalpur|
GWL|Gwalior|
HJR|Khajuraho|
STV|Surat|
BDQ|Vadodara|baroda
RAJ|Rajkot|
HSR|Rajkot Hirasar|hirasar|Rajkot Hirasar
BHJ|Bhuj|
JGA|Jamnagar|
PBD|Porbandar|
BHU|Bhavnagar|
DIU|Diu|
IXU|Aurangabad|chhatrapati sambhajinagar,sambhajinagar
ISK|Nashik|nasik
KLH|Kolhapur|
SSE|Solapur|
SAG|Shirdi|
NDC|Nanded|
JLG|Jalgaon|
UDR|Udaipur|
JDH|Jodhpur|
JSA|Jaisalmer|
BKB|Bikaner|
KQH|Kishangarh|ajmer
IXJ|Jammu|
SXR|Srinagar|
IXL|Leh|ladakh
ATQ|Amritsar|
IXC|Chandigarh|mohali
LUH|Ludhiana|
AIP|Adampur|jalandhar
DED|Dehradun|
PGH|Pantnagar|
KUU|Kullu|manali,bhuntar
SLV|Shimla|
DHM|Dharamshala|gaggal,kangra
VNS|Varanasi|banaras,benares,kashi
IXD|Prayagraj|allahabad
KNU|Kanpur|
AYJ|Ayodhya|
GOP|Gorakhpur|
HDO|Hindon|ghaziabad
AGR|Agra|
BEK|Bareilly|
DGH|Deoghar|
"""

# the buttons shown first (3 x 4 grid)
POPULAR = ["DEL", "BOM", "BLR", "CCU", "MAA", "HYD", "AMD", "PNQ", "GOI", "JAI", "LKO", "COK"]
# shown instead of an airport that is already chosen as the other end of the trip
SPARE = ["PAT", "GAU", "SXR", "IXC"]

CITY: dict[str, str] = {}
SHORT: dict[str, str] = {}
_NAMES: dict[str, set[str]] = {}


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split())


for _line in _RAW.strip().splitlines():
    _p = (_line.split("|") + ["", "", ""])[:4]
    _code, _city, _aliases, _short = _p[0].strip(), _p[1].strip(), _p[2], _p[3].strip() or _p[1].strip()
    CITY[_code], SHORT[_code] = _city, _short
    _NAMES[_code] = {_norm(_city), _norm(_short)} | {_norm(a) for a in _aliases.split(",") if a.strip()}


def known(code: str) -> bool:
    return code in CITY


def label(code: str) -> str:
    """'DEL' -> 'Delhi (DEL)'; unknown codes are shown as they are."""
    return f"{CITY[code]} ({code})" if code in CITY else code


def short(code: str) -> str:
    return SHORT.get(code, code)


def resolve(text: str) -> list[str]:
    """Airport codes matching what the person typed (code, city or alias); [] when nothing matches."""
    t = _norm(text)
    if not t:
        return []
    if re.fullmatch(r"[a-z]{3}", t) and t.upper() in CITY:
        return [t.upper()]
    exact = [c for c, names in _NAMES.items() if t in names]
    if exact:
        return exact
    starts = [c for c, names in _NAMES.items() if any(n.startswith(t) for n in names)]
    if starts:
        return starts[:6]
    if len(t) >= 3:
        return [c for c, names in _NAMES.items() if any(t in n for n in names)][:6]
    return []
