/* Indigo - core: the pure logic (no browser APIs), unit-tested in Node (indigo/tests/core.test.js).
 *
 * What it does is exactly what video.md shows done by hand in Burp Suite: take IndiGo's flight-search POST request and put a promo
 * code into its JSON body before it is sent - for ANY route (one way, round trip, multi city, any passengers) and ANY code.
 * It also reads IndiGo's answer, so the person can be told what the code did.
 */
(function (root) {
  "use strict";

  /* ------------------------------------------------------------------ which request
   * IndiGo's flight-search API: POST https://api-prod-flight-skyplus6e.goindigo.in/v2/flight/search (seen live, 2026-10-24).
   * The host (any api host with "flight" in its name under goindigo.in) and the version in the path are matched loosely, so a renamed sibling still works.
   * Everything else - other hosts, other paths, GET - is never touched.                                                                              */
  const flightHost = (h) => /^api/.test(h) && h.endsWith(".goindigo.in") && /flight/.test(h.slice(0, -".goindigo.in".length));
  const RULES = [
    // the search: the code is put in (the field is created when the body has none)
    { id: "flight-search", method: "POST", host: flightHost, path: (p) => /^(\/v\d+)?\/flight\/search$/i.test(p), createIfMissing: true },
    // "Continue" on the results page: the chosen flight is sold into the booking. IndiGo's own page sends an EMPTY promotionCode here (it never learnt the code,
    // we only put it into the search), so the price can fall back to the full fare. Fill the field that is already there; never add one, never replace a code the site put there.
    { id: "flight-sell", method: "POST", host: flightHost, path: (p) => /^(\/v\d+)?\/flight\/sell$/i.test(p), createIfMissing: false, onlyIfEmpty: true },
  ];

  function matchRule(url, method, base) {
    let u;
    try { u = new URL(String(url), base || "https://www.goindigo.in/"); } catch (_) { return null; }
    const m = String(method || "GET").toUpperCase(), host = u.hostname.toLowerCase(), path = u.pathname.replace(/\/+$/, "");
    for (const r of RULES) {
      if (m === r.method && r.host(host) && r.path(path)) return Object.assign({}, r, { path });
    }
    return null;
  }

  /* ------------------------------------------------------------------ the promo field */
  function getPath(obj, path) {
    let o = obj;
    for (const k of path) { if (o == null || typeof o !== "object") return undefined; o = o[k]; }
    return o;
  }

  function setPath(obj, path, value) {
    let o = obj;
    for (let i = 0; i < path.length - 1; i++) {
      const k = path[i];
      if (o[k] == null || typeof o[k] !== "object" || Array.isArray(o[k])) o[k] = {};
      o = o[k];
    }
    o[path[path.length - 1]] = value;
  }

  const DEFAULT_FIELD = ["codes", "promotionCode"];                 // what IndiGo's page itself uses
  const PROMO_KEY = /^promo(tion)?(al)?[_-]?(code)?$/i;             // promotionCode, promoCode, promo_code, promo ...

  /* Where is the promo code in THIS body? video.md says "promoCode or a similar field": look for a key like that (breadth first, a few levels deep, objects only -
   * not inside the criteria / passenger lists) and use it; if there is none, create IndiGo's own `codes.promotionCode`. */
  function findPromoPath(obj, strict) {
    if (getPath(obj, DEFAULT_FIELD) !== undefined) return DEFAULT_FIELD.slice();
    const queue = [[obj, []]];
    while (queue.length) {
      const [o, path] = queue.shift();
      if (path.length > 3) continue;
      for (const k of Object.keys(o)) {
        const v = o[k];
        if (PROMO_KEY.test(k) && (typeof v === "string" || v == null)) return path.concat(k);
      }
      for (const k of Object.keys(o)) {
        const v = o[k];
        if (v && typeof v === "object" && !Array.isArray(v)) queue.push([v, path.concat(k)]);
      }
    }
    return strict ? null : DEFAULT_FIELD.slice();
  }

  /* Put `code` into the body. Returns { changed, text, before, after, skipped, field }.
   *   text         the body to send (the original text, byte for byte, when nothing is changed)
   *   skipped      why nothing was changed: "no-code" | "not-json" | "not-object" | "site-code-kept" | "already-set"                       */
  function rewriteBody(text, rule, code, opts) {
    const keepSiteCode = !!(opts && opts.keepSiteCode);
    if (!code) return { changed: false, text, before: undefined, after: undefined, skipped: "no-code" };
    if (typeof text !== "string") return { changed: false, text, skipped: "not-json" };
    let obj;
    try { obj = JSON.parse(text); } catch (_) { return { changed: false, text, skipped: "not-json" }; }
    if (obj == null || typeof obj !== "object" || Array.isArray(obj)) return { changed: false, text, skipped: "not-object" };
    const field = findPromoPath(obj, rule && rule.createIfMissing === false);
    if (!field) return { changed: false, text, skipped: "no-field" };                    // a request that has no promo field of its own is left alone
    const before = getPath(obj, field);
    const empty = before == null || (typeof before === "string" && before.trim() === "");
    if (rule && rule.onlyIfEmpty && !empty && before !== code) return { changed: false, text, before, after: before, skipped: "site-code-kept", field };
    if (before === code) return { changed: false, text, before, after: code, skipped: "already-set", field };
    if (keepSiteCode && typeof before === "string" && before.trim() !== "") return { changed: false, text, before, after: before, skipped: "site-code-kept", field };
    setPath(obj, field, code);
    return { changed: true, text: JSON.stringify(obj), before, after: code, field };
  }

  // Did the server's answer mention the code we sent? (a hint that it was understood; not a guarantee)
  function echoed(responseText, code) {
    return !!code && typeof responseText === "string" && responseText.indexOf(code) !== -1;
  }

  /* ------------------------------------------------------------------ what the search is about (for messages) */
  const PAX = { ADT: "adult", CHD: "child", INFT: "infant", PROMO2: "senior", SNR: "senior" };

  // { route: "BLR-IDR" (first leg), date, legs: [{route, date}], pax: 2, trip: "oneWay" | "roundTrip" | ... } or null when the body is not a search
  function searchInfo(text) {
    try {
      const b = JSON.parse(text), crit = Array.isArray(b.criteria) ? b.criteria : [];
      const legs = crit.map((c) => {
        const st = (c && c.stations) || {};
        const o = (st.originStationCodes || [])[0], d = (st.destinationStationCodes || [])[0];
        return o && d ? { route: o + "-" + d, date: (c.dates && c.dates.beginDate) || "" } : null;
      }).filter(Boolean);
      if (!legs.length) return null;
      const types = (b.passengers && b.passengers.types) || [];
      return { route: legs[0].route, date: legs[0].date, legs, pax: types.reduce((n, p) => n + (p.count || 0), 0), trip: b.tripCriteria || "" };
    } catch (_) { return null; }
  }

  /* ------------------------------------------------------------------ what IndiGo's answer says
   * A fare that got the code shows it in one of two ways (both measured, network/reference):
   *   marked    `promoCode` is set, `originalFareAmount` is the price before, `originalTotalDiscount` the discount      (6ETST001: "Defence Di" PC50D1)
   *   unmarked  `promoCode` is empty and `originalTotalDiscount` is 0 - only the price breakdown tells: a `serviceCharges` entry of type 7 (a discount) whose `code`
   *             is the code we sent, with its amount                                                                       (PROMO2: "Senior Cit", 6E 687 9,237, 815 off)
   * `code` is the code that was sent; without it only marked fares are recognised.
   * One entry per trip of the answer (a round trip has two), plus the single best deal (the cheapest fare that got the code).
   * Returns null when the text is not a search answer.                                                                                                       */
  const DISCOUNT_CHARGE = 7;
  function chargeSaving(f, code) {
    if (!code) return 0;
    const want = String(code).toUpperCase(); let sum = 0;
    for (const px of (f && f.paxFares) || []) {
      const mult = typeof px.multiplier === "number" && px.multiplier > 0 ? px.multiplier : 1;
      for (const sc of (px && px.serviceCharges) || []) {
        if (sc && sc.type === DISCOUNT_CHARGE && typeof sc.amount === "number" && sc.amount > 0 && String(sc.code || "").toUpperCase() === want) sum += sc.amount * mult;
      }
    }
    return sum;
  }

  // A discount line (type 7) on a fare that no code marks and that is NOT called like the code we sent (e.g. we send "STUDENT10", IndiGo labels the line "STU"): only a hint -
  // the name inside IndiGo is not ours to know. Returns { amount, label } or null.
  function otherDiscount(f) {
    let amount = 0, label = "";
    for (const px of (f && f.paxFares) || []) for (const sc of (px && px.serviceCharges) || []) {
      if (sc && sc.type === DISCOUNT_CHARGE && typeof sc.amount === "number" && sc.amount > 0) { amount += sc.amount * (typeof px.multiplier === "number" && px.multiplier > 0 ? px.multiplier : 1); label = label || String(sc.detail || sc.code || "").trim(); }
    }
    return amount > 0 ? { amount, label } : null;
  }

  function summarizeAnswer(text, code) {
    let j; try { j = JSON.parse(text); } catch (_) { return null; }
    const trips = j && j.data && j.data.trips; if (!Array.isArray(trips)) return null;
    const s = { flights: 0, fares: 0, promoFares: 0, unmarkedFares: 0, otherFares: 0, otherLabels: [], otherSaving: 0, maxSaving: 0, cheapestAll: null, cheapestPromo: null, cheapestPromoSaving: 0, codes: [], trips: [], best: null,
                currency: (j.data && j.data.currencyCode) || "INR" };
    trips.forEach((tr, ti) => {
      const t = { origin: tr && tr.origin || "", destination: tr && tr.destination || "", flights: 0, fares: 0, promoFares: 0, cheapestAll: null, cheapestPromo: null, saving: 0 };
      for (const jr of (tr && tr.journeysAvailable) || []) {
        s.flights++; t.flights++;
        for (const f of (jr && jr.passengerFares) || []) {
          s.fares++; t.fares++;
          const total = typeof f.totalFareAmount === "number" ? f.totalFareAmount : null;
          if (total != null && (s.cheapestAll == null || total < s.cheapestAll)) s.cheapestAll = total;
          if (total != null && (t.cheapestAll == null || total < t.cheapestAll)) t.cheapestAll = total;
          const marked = !!f.promoCode, charge = marked ? 0 : chargeSaving(f, code);
          if (!marked && !charge) {
            const o = code ? otherDiscount(f) : null;
            if (o) { s.otherFares++; if (o.label && !s.otherLabels.includes(o.label) && s.otherLabels.length < 3) s.otherLabels.push(o.label); if (o.amount > s.otherSaving) s.otherSaving = o.amount; }
            continue;
          }
          s.promoFares++; t.promoFares++; if (!marked) s.unmarkedFares++;
          const appliedCode = marked ? f.promoCode : String(code);
          if (!s.codes.includes(appliedCode)) s.codes.push(appliedCode);
          const save = marked ? (typeof f.originalTotalDiscount === "number" ? f.originalTotalDiscount : 0) : charge;
          if (save > s.maxSaving) s.maxSaving = save;
          if (save > t.saving) t.saving = save;
          if (total == null) continue;
          if (t.cheapestPromo == null || total < t.cheapestPromo) t.cheapestPromo = total;
          if (s.cheapestPromo == null || total < s.cheapestPromo) {
            s.cheapestPromo = total; s.cheapestPromoSaving = save;
            const seg = (jr.segments && jr.segments[0]) || {}, id = seg.identifier || {}, dep = String((jr.designator && jr.designator.departure) || "");
            // the price before is only known for a marked fare; for an unmarked one the answer holds just the discount, so "was" stays null (never guessed)
            const was = !marked ? null : typeof f.originalFareAmount === "number" && f.originalFareAmount > 0 ? f.originalFareAmount : total + save;
            s.best = { trip: ti, flight: [id.carrierCode, id.identifier].filter(Boolean).join(" "), departs: dep.slice(11, 16), stops: typeof jr.stops === "number" ? jr.stops : null,
                       fareClass: f.FareClass || "", now: total, was, saves: save };
          }
        }
      }
      s.trips.push(t);
    });
    return s;
  }

  /* What the results page showed for each fare: "journeyKey|fareAvailabilityKey" -> totalFareAmount. The sell request names the chosen fare with the same two keys, so the
   * price in the booking can be compared with the price the person clicked on (that catches ANY code - or no code - that is lost on the way). null = not a search answer. */
  const fareKey = (journeyKey, fareAvailabilityKey) => String(journeyKey) + "|" + String(fareAvailabilityKey);
  function indexFares(text) {
    let j; try { j = JSON.parse(text); } catch (_) { return null; }
    const trips = j && j.data && j.data.trips; if (!Array.isArray(trips)) return null;
    const out = {};
    for (const tr of trips) for (const jr of (tr && tr.journeysAvailable) || []) for (const f of (jr && jr.passengerFares) || []) {
      if (jr.journeyKey && f && typeof f.fareAvailabilityKey === "string" && typeof f.totalFareAmount === "number") out[fareKey(jr.journeyKey, f.fareAvailabilityKey)] = f.totalFareAmount;
    }
    return out;
  }

  /* The price the results page showed for the flight a sell request is about - only when that is certain: ONE fare, ONE passenger, no infant (the totals of several
   * passengers / legs were never measured), and the fare was seen in a search answer. Otherwise null. */
  function expectedTotal(sellBodyText, index) {
    let b; try { b = JSON.parse(sellBodyText); } catch (_) { return null; }
    if (!b || !Array.isArray(b.keys) || b.keys.length !== 1 || !index) return null;
    const pax = ((b.passengers && b.passengers.types) || []).reduce((n, p) => n + (Number(p && p.count) || 0), 0);
    if (pax !== 1 || Number(b.infantCount) > 0) return null;
    const k = b.keys[0], v = k && index[fareKey(k.journeyKey, k.fareAvailabilityKey)];
    return typeof v === "number" ? v : null;
  }

  /* The answer to the sell step (the booking IndiGo has just built): `typeOfSale.promotionCode` is the code IndiGo applied, `breakdown.journeyTotals.totalDiscount` the
   * discount, `breakdown.totalAmount` the amount due. Returns null when the text is not a booking answer. */
  function summarizeSell(text) {
    let j; try { j = JSON.parse(text); } catch (_) { return null; }
    const d = j && j.data; if (!d || typeof d !== "object" || !d.breakdown) return null;
    const jt = d.breakdown.journeyTotals || {};
    return { applied: String((d.typeOfSale && d.typeOfSale.promotionCode) || ""), discount: Number(jt.totalDiscount) || 0,
             fare: typeof jt.totalAmount === "number" ? jt.totalAmount : null, total: typeof d.breakdown.totalAmount === "number" ? d.breakdown.totalAmount : null,
             currency: d.currencyCode || "INR" };
  }

  // short fingerprint of a request body: tells "the same search again" (after a refresh) from "a new search"
  function sig(text) {
    let h = 5381; const s = String(text == null ? "" : text);
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
    return (h >>> 0).toString(36);
  }

  const api = { RULES, matchRule, rewriteBody, findPromoPath, echoed, getPath, setPath, sig, searchInfo, summarizeAnswer, summarizeSell, indexFares, expectedTotal, fareKey, PAX };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.__IndigoCore = api;
})(typeof self !== "undefined" ? self : this);
