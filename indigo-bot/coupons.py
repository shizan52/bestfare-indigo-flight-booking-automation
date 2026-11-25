"""Coupon application - a faithful Python port of the IndiGo coupon extension's rules (F:\\card\\indigo\\core.js).

The extension (and therefore this bot) touches exactly two requests:

  POST .../flight/search   the code is put into the body; the field is CREATED if the body has none
                           (createIfMissing).  Default field: codes.promotionCode.
  POST .../flight/sell     only IndiGo's own EMPTY promotionCode is filled (onlyIfEmpty): never a field
                           the body does not have, never a code the site itself put there.

Passenger types are never changed (both offers book as ADT; only the promo code differs).

`rewrite_body` mirrors core.js `rewriteBody` + `findPromoPath` line by line and is cross-checked against the
real core.js by tests/test_coupon_parity.py (run through Node), so the two cannot drift apart silently.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

DEFAULT_FIELD = ["codes", "promotionCode"]
_PROMO_KEY = re.compile(r"^promo(tion)?(al)?[_-]?(code)?$", re.I)

RULES = {
    "flight-search": {"createIfMissing": True, "onlyIfEmpty": False},
    "flight-sell": {"createIfMissing": False, "onlyIfEmpty": True},
}


def _get(obj: Any, path: list[str]) -> Any:
    o = obj
    for k in path:
        if not isinstance(o, dict):
            return None
        o = o.get(k)
    return o


def _has(obj: Any, path: list[str]) -> bool:
    o = obj
    for k in path:
        if not isinstance(o, dict) or k not in o:
            return False
        o = o[k]
    return True


def _set(obj: dict, path: list[str], value: Any) -> None:
    o = obj
    for k in path[:-1]:
        if not isinstance(o.get(k), dict):
            o[k] = {}
        o = o[k]
    o[path[-1]] = value


def find_promo_path(obj: dict, strict: bool) -> Optional[list[str]]:
    """Where is the promo code in THIS body? (core.js findPromoPath)"""
    if _has(obj, DEFAULT_FIELD) and _get(obj, DEFAULT_FIELD) is not None:
        return DEFAULT_FIELD[:]
    queue: list[tuple[dict, list[str]]] = [(obj, [])]
    while queue:
        o, path = queue.pop(0)
        if len(path) > 3:
            continue
        for k, v in o.items():
            if _PROMO_KEY.match(k) and (isinstance(v, str) or v is None):
                return path + [k]
        for k, v in o.items():
            if isinstance(v, dict):
                queue.append((v, path + [k]))
    return None if strict else DEFAULT_FIELD[:]


def rewrite_body(body: dict, rule: str, code: str, keep_site_code: bool = False) -> tuple[dict, dict]:
    """Return (new_body, info). `info` mirrors core.js: {changed, before, after, skipped?, field?}."""
    r = RULES[rule]
    if not code:
        return body, {"changed": False, "skipped": "no-code"}
    field = find_promo_path(body, strict=(r["createIfMissing"] is False))
    if not field:
        return body, {"changed": False, "skipped": "no-field"}
    before = _get(body, field)
    empty = before is None or (isinstance(before, str) and before.strip() == "")
    if r["onlyIfEmpty"] and not empty and before != code:
        return body, {"changed": False, "before": before, "after": before, "skipped": "site-code-kept", "field": field}
    if before == code:
        return body, {"changed": False, "before": before, "after": code, "skipped": "already-set", "field": field}
    if keep_site_code and isinstance(before, str) and before.strip() != "":
        return body, {"changed": False, "before": before, "after": before, "skipped": "site-code-kept", "field": field}
    out = json.loads(json.dumps(body))
    _set(out, field, code)
    return out, {"changed": True, "before": before, "after": code, "field": field}


def apply_search(body: dict, code: str) -> dict:
    return rewrite_body(body, "flight-search", code)[0]


def apply_sell(body: dict, code: str) -> dict:
    return rewrite_body(body, "flight-sell", code)[0]


# ── reading IndiGo's answer: was the code really applied?  (core.js summarizeSell / summarizeAnswer) ──────
def summarize_sell(answer: dict) -> Optional[dict]:
    d = (answer or {}).get("data")
    if not isinstance(d, dict) or not d.get("breakdown"):
        return None
    jt = (d["breakdown"].get("journeyTotals")) or {}
    tos = d.get("typeOfSale") or {}
    return {"applied": str(tos.get("promotionCode") or ""),
            "discount": float(jt.get("totalDiscount") or 0),
            "fare": jt.get("totalAmount") if isinstance(jt.get("totalAmount"), (int, float)) else None,
            "total": d["breakdown"].get("totalAmount") if isinstance(d["breakdown"].get("totalAmount"), (int, float)) else None}


def coupon_applied(sell_answer: dict, code: str) -> bool:
    """True when IndiGo's booking carries the code we sent (typeOfSale.promotionCode), or - when it names none -
    a discount > 0 (core.js treats both as 'applied')."""
    s = summarize_sell(sell_answer)
    if not s:
        return False
    return s["applied"].upper() == code.upper() or s["discount"] > 0
