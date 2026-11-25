// Runs the coupon extension's REAL core.js (F:\card\indigo\core.js) on a list of cases read from stdin and prints
// the results as JSON, so the Python port in coupons.py can be checked against it byte for byte.
//   stdin : [{ "rule": "flight-search"|"flight-sell", "body": {...}, "code": "PROMO2", "keepSiteCode": false }, ...]
//   stdout: [{ "changed": bool, "body": {...}, "before": ..., "after": ..., "skipped": ..., "field": [...] }, ...]
const path = require("path");
const core = require(process.env.INDIGO_CORE || path.join("F:", "card", "indigo", "core.js"));

let input = "";
process.stdin.on("data", (d) => (input += d));
process.stdin.on("end", () => {
  const cases = JSON.parse(input);
  const out = cases.map((c) => {
    const rule = core.RULES.find((r) => r.id === c.rule);
    const text = JSON.stringify(c.body);
    const r = core.rewriteBody(text, rule, c.code, { keepSiteCode: !!c.keepSiteCode });
    return { changed: r.changed, body: JSON.parse(r.text), before: r.before === undefined ? null : r.before,
             after: r.after === undefined ? null : r.after, skipped: r.skipped || null, field: r.field || null };
  });
  process.stdout.write(JSON.stringify(out));
});
