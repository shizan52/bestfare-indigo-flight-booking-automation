"use strict";
/* Best Fare admin panel. Plain JavaScript, no libraries, no inline scripts. Text from the outside (names, notes, device names,
   event details) is ALWAYS put on the page with textContent - never as HTML - so nothing a user's app sends can run in this page. */

const state = { me: null, csrf: "", page: "dashboard", filter: "all", search: "", evLevel: "", timer: null };

/* ── tiny DOM helper ───────────────────────────────────────────────────────────────────────────────────────────── */
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el[k] = v;
    else if (k === "style") Object.assign(el.style, v);
    else if (k === "value" || k === "checked" || k === "disabled" || k === "selected") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const $ = (sel, root = document) => root.querySelector(sel);
const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };

function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleString(undefined, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}
function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " days ago";
}
function toast(text, kind, undo) {
  // kind: true | "bad" = red; "stop" = dark red (something was stopped); undefined = normal. `undo`: a function that takes the action back (an "Undo" button, 9 s).
  let box = document.getElementById("toasts");
  if (!box) { box = h("div", { id: "toasts", class: "toasts" }); document.body.append(box); }
  const bad = kind === true || kind === "bad";
  const t = h("div", { class: "toast" + (bad ? " bad" : kind === "stop" ? " stop" : "") }, h("span", {}, text));
  if (undo) t.append(h("button", { class: "undo", onclick: async () => { t.remove(); try { await undo(); } catch (e) { toast(e.message || "Could not undo", true); } } }, "Undo"));
  box.append(t);
  setTimeout(() => t.remove(), undo ? 9000 : bad ? 6000 : 3400);
}
async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied"); } catch (e) { toast("Select the text and press Ctrl+C", true); }
}

/* ── server calls ──────────────────────────────────────────────────────────────────────────────────────────────── */
async function api(method, path, body, raw) {
  const opt = { method, credentials: "same-origin", headers: { "X-CSRF": state.csrf } };
  if (raw) { opt.body = raw.body; Object.assign(opt.headers, raw.headers); }
  else if (body !== undefined) { opt.body = JSON.stringify(body); opt.headers["Content-Type"] = "application/json"; }
  let r;
  try { r = await fetch("/admin/api" + path, opt); } catch (e) { throw { code: "network", message: "Cannot reach the server. Check the internet connection." }; }
  let j = {};
  try { j = await r.json(); } catch (e) { /* not JSON */ }
  if (r.status === 401 && path !== "/login") { showLogin(); throw { code: "unauthorized", message: "Please sign in again." }; }
  if (!r.ok || j.ok === false) throw { code: j.code || "error", message: j.message || "Something went wrong." };
  return j;
}
async function act(promise, okText) {
  try { const r = await promise; if (okText) toast(okText); return r; } catch (e) { if (e.code !== "unauthorized") toast(e.message, true); throw e; }
}

/* ── login ─────────────────────────────────────────────────────────────────────────────────────────────────────── */
function showLogin(msg) {
  clearInterval(state.timer);
  const root = clear($("#app"));
  const err = h("div", { class: "err" }, msg || "");
  const user = h("input", { type: "text", autocomplete: "username", placeholder: "Username" });
  const pass = h("input", { type: "password", autocomplete: "current-password", placeholder: "Password" });
  const code = h("input", { type: "text", inputmode: "numeric", autocomplete: "one-time-code", placeholder: "6-digit code", maxlength: "7" });
  const codeRow = h("label", { class: "f hidden" }, h("span", {}, "Authenticator code"), code);
  const go = async () => {
    err.textContent = "";
    try {
      const j = await api("POST", "/login", { username: user.value, password: pass.value, totp: code.value });
      state.me = j; state.csrf = j.csrf; start();
    } catch (e) {
      if (e.code === "totp_needed") { codeRow.classList.remove("hidden"); code.focus(); err.textContent = e.message; }
      else err.textContent = e.message;
    }
  };
  [user, pass, code].forEach((i) => (i.onkeydown = (ev) => { if (ev.key === "Enter") go(); }));
  root.append(h("div", { class: "login" }, h("div", { class: "box" },
    h("div", { class: "brand" }, "✈ Best Fare"), h("p", {}, "Admin panel — sign in to manage users, credits and updates."),
    h("label", { class: "f" }, h("span", {}, "Username"), user), h("label", { class: "f" }, h("span", {}, "Password"), pass), codeRow, err,
    h("button", { class: "btn primary", style: { width: "100%" }, onclick: go }, "Sign in"))));
  user.focus();
}

/* ── shell ─────────────────────────────────────────────────────────────────────────────────────────────────────── */
const PAGES = [["dashboard", "📊", "Dashboard"], ["users", "👥", "Users"], ["activity", "🔔", "Activity"], ["updates", "⬆️", "Updates"], ["settings", "⚙️", "Settings"]];

function start() {
  const root = clear($("#app"));
  const nav = h("div", { class: "side" }, h("div", { class: "brand" }, "✈ Best Fare"), h("div", { class: "tag" }, "Admin panel"));
  const pages = PAGES.concat(state.me.role === "owner" ? [["admins", "🛡️", "Admins"]] : []);
  for (const [id, icon, label] of pages) nav.append(h("a", { "data-page": id, onclick: () => go(id) }, icon + "  " + label));
  nav.append(h("div", { class: "who" }, h("div", {}, "Signed in as ", h("b", {}, state.me.username)), h("div", { class: "muted" }, state.me.role),
    h("button", { class: "btn small", onclick: myAccount }, "My account"), h("button", { class: "btn small", onclick: logout }, "Sign out")));
  root.append(h("div", { class: "shell" }, nav, h("div", { class: "main", id: "view" })));
  go(location.hash.replace("#", "") || "dashboard");
  if (state.me.must_change) { toast("Please set your own password (My account).", true); myAccount(); }
}
async function logout() { try { await api("POST", "/logout"); } catch (e) { /* ignore */ } state.me = null; showLogin(); }
function go(page) {
  clearInterval(state.timer);
  state.page = page; location.hash = page;
  document.querySelectorAll(".side a").forEach((a) => a.classList.toggle("on", a.dataset.page === page));
  const view = clear($("#view"));
  ({ dashboard, users, activity, updates, settings, admins }[page] || dashboard)(view);
}

/* ── dashboard ─────────────────────────────────────────────────────────────────────────────────────────────────── */
async function dashboard(view) {
  view.append(h("div", { class: "head" }, h("div", {}, h("h1", {}, "Dashboard"), h("div", { class: "muted" }, "How Best Fare is doing right now")),
    h("button", { class: "btn primary", onclick: () => newUser() }, "＋ New user")));
  const body = h("div", {}); view.append(body);
  const load = async () => {
    let s, ev, st;
    try { s = (await api("GET", "/stats")).stats; ev = (await api("GET", "/events?limit=8")).events; st = (await api("GET", "/settings")).settings; } catch (e) { return; }
    clear(body);
    if (st.maintenance) body.append(h("div", { class: "banner" }, "⚠ Maintenance mode is ON — nobody can start a booking."));
    const card = (n, l, cls, link) => h("div", { class: "card stat " + (cls || ""), style: link ? { cursor: "pointer" } : {}, onclick: link ? () => { state.filter = link; go("users"); } : null },
      h("div", { class: "n" }, n), h("div", { class: "l" }, l));
    body.append(h("div", { class: "cards" },
      card(s.users, "Users", "", "all"), card(s.online, "Online now", "ok", "online"), card(s.no_credits, "Out of credits", s.no_credits ? "warn" : "", "nocredits"),
      card(s.flagged, "Flagged (check them)", s.flagged ? "bad" : "", "flagged"), card(s.blocked, "Blocked", "", "blocked"),
      card(s.bookings_today, "Bookings, last 24 h", "ok"), card(s.credits_total, "Credits in users' hands"), card(s.unbound, "Keys not yet used", "", "unbound")));
    const max = Math.max(1, ...s.daily.map((d) => d.done));
    body.append(h("div", { class: "grid2" },
      h("div", { class: "card" }, h("h2", {}, "Bookings, last 7 days"), h("div", { class: "bars" }, s.daily.map((d) => h("div", { class: "b" }, h("div", { class: "v" }, d.done),
        h("div", { class: "bar", style: { height: Math.max(3, (d.done / max) * 100) + "%" } }), h("div", { class: "d" }, d.day)))),
        h("div", { class: "small muted", style: { marginTop: "8px" } }, "Failed in the last 24 h: ", s.failed_today, " · in progress: ", s.open, " · credits used this week: ", s.credits_used_week)),
      h("div", { class: "card" }, h("h2", {}, "Latest activity"), h("div", { class: "list" }, ev.length ? ev.map(eventRow) : h("div", { class: "empty" }, "Nothing yet")),
        h("div", { style: { marginTop: "8px" } }, h("a", { class: "btn small", onclick: () => go("activity") }, "See all")))));
  };
  await load(); state.timer = setInterval(load, 20000);
}
/* ── what happened, in plain words: who did what to whom, and when ─────────────────────────────────────────────────────────── */
const ADMIN_KINDS = new Set(["blocked", "unblocked", "credits", "licence_created", "licence_edited", "licence_deleted", "offers_changed", "device_reset", "key_regenerated", "key_viewed",
  "admin_refund", "setting", "release_uploaded", "release_changed", "release_deleted", "installer_uploaded", "installer_changed", "installer_deleted", "admin_created", "admin_changed",
  "admin_login", "totp_on", "totp_off"]);
const prettyOffers = (t) => (t || "").replace(/offer(\d)/g, "Offer $1").replace(/,/g, ", ");
function userLink(e) {
  return e.label ? h("a", { class: "ulink", title: "Open this user", onclick: (ev) => { ev.stopPropagation(); if (e.licence_id) openUser(e.licence_id); } }, e.label) : h("b", {}, e.licence_id ? "a deleted user" : "a user");
}
function describe(e) {
  const me = e.actor && state.me && e.actor === state.me.username;
  const by = e.actor ? (me ? "You" : e.actor) : "";
  const B = (t) => h("b", {}, t);
  const d = e.detail || "";
  const u = userLink(e);
  const mk = (icon, tone, line, note) => ({ icon, tone, line, note });
  switch (e.kind) {
    case "blocked": return mk("🚫", "bad", [by || "Someone", " ", B("BLOCKED"), " ", u], d ? "Reason: " + d : "No reason was given. The user is stopped at their next booking.");
    case "unblocked": return mk("✅", "ok", [by || "Someone", " ", B("unblocked"), " ", u], "They can book again.");
    case "credits": return mk("💳", "gray", [by || "Someone", " changed the credits of ", u], d);
    case "admin_refund": return mk("💸", "ok", [by || "Someone", " gave credits back to ", u], d);
    case "refund_denied": return mk("⚠️", "warn", ["The credits of a failed booking of ", u, " were ", B("not"), " returned automatically"], (d ? d + ". " : "") + "Open the user's Bookings and press “Give credits back” if it is right.");
    case "licence_created": return mk("🆕", "ok", [by || "Someone", " created the user ", u], d);
    case "licence_edited": return mk("✏️", "gray", [by || "Someone", " edited the details of ", u], d);
    case "licence_deleted": return mk("🗑️", "bad", [by || "Someone", " ", B("deleted"), " the user ", B(d.replace(/ \(.*$/, "") || "?")], "The user and their history are gone.");
    case "offers_changed": return mk("🎟️", "gray", [by || "Someone", " changed which coupons ", u, " may use"], prettyOffers(d));
    case "device_reset": return mk("🔄", "warn", [by || "Someone", " moved the key of ", u, " to a new computer"], "The old computer stopped at once. The key works on ONE new computer.");
    case "key_regenerated": return mk("🔑", "warn", [by || "Someone", " made a ", B("new key"), " for ", u], "The old key stopped working.");
    case "key_viewed": return mk("👁️", "gray", [by || "Someone", " looked at the key of ", u], "");
    case "device_bound": return mk("💻", "gray", [u, " used their key on a computer for the first time"], d);
    case "device_refused": return mk("⛔", "bad", ["A second computer tried the key of ", u, " — ", B("refused")], d);
    case "mac_refused": return mk("⛔", "bad", ["The key of ", u, " was refused: wrong MAC address"], d);
    case "offer_refused": return mk("⛔", "warn", [u, " asked for a coupon their key may not use — refused, nothing charged"], prettyOffers(d));
    case "sharing_suspected": return mk("🚨", "bad", ["The key of ", u, " looks shared between computers"], d);
    case "ip_change": return mk("🌐", "gray", [u, " connected from a different internet address"], d);
    case "unconfirmed": return mk("⏳", "warn", ["Bookings of ", u, " were never reported by the app"], d);
    case "bad_key": return mk("❌", "warn", ["Someone typed a ", B("wrong key")], [d, e.ip].filter(Boolean).join(" · "));
    case "release_key": return mk("📲", "gray", [u, " opened Best Fare"], d);
    case "admin_login": return mk("🔓", "gray", [by || "Someone", " signed in to the panel"], e.ip || "");
    case "admin_login_failed": return mk("🔒", "warn", ["A ", B("failed"), " sign-in for ", B(e.actor || "?")], e.ip || "");
    case "admin_created": return mk("🛡️", "ok", [by || "Someone", " added the admin ", B(d || "?")], "");
    case "admin_changed": return mk("🛡️", "gray", [by || "Someone", " changed an admin: ", d], "");
    case "totp_on": return mk("🔐", "ok", [by || "Someone", " turned two-step sign-in ", B("on")], "");
    case "totp_off": return mk("🔓", "warn", [by || "Someone", " turned two-step sign-in ", B("off")], "");
    case "setting": return mk("⚙️", "gray", [by || "Someone", " changed a setting"], d);
    case "release_uploaded": return mk("📦", "gray", [by || "Someone", " uploaded a program release"], d);
    case "release_changed": return mk("📦", "gray", [by || "Someone", " changed a program release"], d);
    case "release_deleted": return mk("📦", "warn", [by || "Someone", " deleted a program release"], d);
    case "installer_uploaded": return mk("💿", "gray", [by || "Someone", " uploaded an installer"], d);
    case "installer_changed": return mk("💿", "gray", [by || "Someone", " changed an installer"], d);
    case "installer_deleted": return mk("💿", "warn", [by || "Someone", " deleted an installer"], d);
    case "rate_limited": return mk("🚦", "warn", ["Too many requests were slowed down"], [d, e.ip].filter(Boolean).join(" · "));
    default: return mk(e.level === "alert" ? "🚨" : e.level === "warn" ? "⚠️" : "•", e.level === "alert" ? "bad" : e.level === "warn" ? "warn" : "gray",
      [by ? by + ": " : "", e.kind.replace(/_/g, " "), e.label ? " — " : "", e.label ? u : null], d);
  }
}
function eventRow(e) {
  const x = describe(e);
  return h("div", { class: "ev tone-" + x.tone },
    h("div", { class: "ico" }, x.icon),
    h("div", { class: "txt" }, h("div", { class: "line" }, x.line), x.note ? h("div", { class: "sub" }, x.note) : null,
      h("div", { class: "meta", title: fmtTime(e.ts) }, ago(e.ts), " · ", fmtTime(e.ts), e.ip && !x.note ? " · " + e.ip : "")));
}
function dayLabel(ts) {
  const d = new Date(ts * 1000), n = new Date();
  const days = Math.round((new Date(n.getFullYear(), n.getMonth(), n.getDate()) - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 86400000);
  return days === 0 ? "Today" : days === 1 ? "Yesterday" : d.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
}
function eventList(events) {
  const out = []; let last = "";
  for (const e of events) {
    const day = dayLabel(e.ts);
    if (day !== last) { out.push(h("div", { class: "dayhead" }, day)); last = day; }
    out.push(eventRow(e));
  }
  return h("div", { class: "evlist" }, out);
}
function liveTag() { return h("span", { class: "live" }, "● Live"); }
function stamp(tag) { tag.textContent = "● Live · updated " + new Date().toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }

/* ── who is stopped, and by whom ──────────────────────────────────────────────────────────────────────────────────────────── */
function statusBadge(u) {
  if (u.status === "blocked") return h("span", { class: "badge bad", title: u.block_reason }, "🚫 BLOCKED");
  if (u.expired) return h("span", { class: "badge warn" }, "Expired");
  if (u.credits === 0) return h("span", { class: "badge warn" }, "No credits");
  return h("span", { class: "badge ok" }, "Active");
}
function statusLine(u) {
  const who = u.status_by ? (state.me && u.status_by === state.me.username ? "you" : u.status_by) : "";
  return ["Blocked", who ? " by " + who : "", u.status_at ? " · " + ago(u.status_at) : "", u.block_reason ? " — “" + u.block_reason + "”" : ""].join("");
}
async function blockUser(u, after) {
  const r = await ask("Block " + u.label + "?", u.label + " (key …" + u.key_tail + ") will be stopped: the app refuses to start any booking and its window locks. You can unblock them at any time.",
    "Yes, block " + u.label, true, "Reason (the user sees this)");
  if (r === null) return;
  try { await api("POST", "/licences/" + u.id + "/block", { reason: r === true ? "" : r }); } catch (e) { if (e.code !== "unauthorized") toast(e.message, true); return; }
  toast("🚫 " + u.label + " is now BLOCKED", "stop", async () => { await api("POST", "/licences/" + u.id + "/unblock"); toast("✅ " + u.label + " is unblocked again"); if (after) after(); });
  if (after) after();
}
async function unblockUser(u, after) {
  const r = await ask("Unblock " + u.label + "?", u.label + " (key …" + u.key_tail + ") will be able to book again right away.", "Yes, unblock " + u.label, false);
  if (r === null) return;
  try { await api("POST", "/licences/" + u.id + "/unblock"); } catch (e) { if (e.code !== "unauthorized") toast(e.message, true); return; }
  toast("✅ " + u.label + " is unblocked", undefined, async () => { await api("POST", "/licences/" + u.id + "/block", { reason: u.block_reason || "" }); toast("🚫 " + u.label + " is blocked again", "stop"); if (after) after(); });
  if (after) after();
}
function statusBanner(d) {
  if (d.status === "blocked") return h("div", { class: "sbanner bad" }, h("div", { class: "big2" }, "🚫 BLOCKED"), h("div", {}, statusLine(d).replace(/^Blocked/, "Stopped")), h("div", { class: "small" }, "They cannot start a booking; the app window is locked."));
  if (d.expired) return h("div", { class: "sbanner warn" }, h("div", { class: "big2" }, "⌛ Licence ended"), h("div", {}, "It ended " + fmtTime(d.expires_at) + ". Edit the user to give a new date."));
  if (d.credits === 0) return h("div", { class: "sbanner warn" }, h("div", { class: "big2" }, "💳 No credits left"), h("div", {}, "They can open the app but cannot book until you add credits."));
  return h("div", { class: "sbanner ok" }, h("div", { class: "big2" }, "✅ Active"), h("div", {}, d.online ? "Online now" : "Last seen " + ago(d.last_seen)));
}

/* ── users ─────────────────────────────────────────────────────────────────────────────────────────────────────── */
// What each offer really is: shown to ADMINS only. The app's users see just "Offer 1" / "Offer 2".
async function loadOffers() {
  try { const j = await api("GET", "/settings"); state.offers = Object.entries(j.settings.offers).sort().map(([k, v]) => ({ key: k, name: "Offer " + k.slice(5), what: v.who, on: v.enabled })); } catch (e) { state.offers = state.offers || []; }
  return state.offers;
}
function offerName(k) { const o = (state.offers || []).find((x) => x.key === k); return o ? "Offer " + k.slice(5) + (o.what ? " — " + o.what : "") : k; }
function offerBadges(keys) { return (keys || []).map((k) => h("span", { class: "badge gray", style: { marginRight: "4px" }, title: offerName(k) }, "O" + k.slice(5))); }
// A set of tick boxes: which offers this user may use (at least one).
function offerPicker(selected) {
  const boxes = (state.offers || []).map((o) => ({ o, box: h("input", { type: "checkbox", checked: selected.includes(o.key) }) }));
  const node = h("div", { class: "f" }, h("span", {}, "Which coupon may this user use?"),
    h("div", { class: "row", style: { flexDirection: "column", alignItems: "flex-start", gap: "6px" } }, boxes.map(({ o, box }) => h("label", { class: "switch" }, box, h("b", {}, o.name), " — " + (o.what || "") + (o.on ? "" : "  (switched off in Settings)")))),
    h("div", { class: "small muted" }, "Tick one coupon, or both. Every booking uses exactly one coupon: a user who has both picks one for each booking. The user sees only “Offer 1” / “Offer 2”, never what they are."));
  return { node, value: () => boxes.filter(({ box }) => box.checked).map(({ o }) => o.key) };
}
const FILTERS = [["all", "All"], ["active", "Active"], ["online", "Online"], ["nocredits", "No credits"], ["blocked", "Blocked"], ["flagged", "Flagged"], ["unbound", "Not used yet"], ["expired", "Expired"]];
function userRow(u, reload) {
  const blocked = u.status === "blocked";
  const lbl = (t) => h("div", { class: "lbl" }, t);
  return h("div", { class: "urow" + (blocked ? " blocked" : ""), onclick: () => openUser(u.id) },
    h("div", { class: "c-name" }, h("div", { class: "nm" }, u.label, u.flagged ? h("span", { class: "badge bad", style: { marginLeft: "6px" }, title: u.flag_reason }, "flagged") : null),
      h("div", {}, statusBadge(u), " ", h("span", { class: "small muted mono" }, "BF-••••-" + u.key_tail)),
      blocked ? h("div", { class: "why" }, statusLine(u)) : null, u.note ? h("div", { class: "small muted" }, u.note.slice(0, 60)) : null),
    h("div", { class: "c-credits" }, lbl("Credits"), h("span", { class: "credits " + (u.credits === 0 ? "zero" : u.credits < 3 ? "low" : "") }, u.credits)),
    h("div", { class: "c-offers" }, lbl("Coupons"), offerBadges(u.offers)),
    h("div", { class: "c-pc" }, lbl("Computer"), u.bound ? [h("div", {}, u.device.name || "—"), h("div", { class: "small muted mono" }, (u.device.macs || []).join(", "))] : h("span", { class: "muted" }, u.mac_lock ? "locked to " + u.mac_lock : "not used yet")),
    h("div", { class: "c-seen" }, lbl("Last seen"), h("span", { class: "dot" + (u.online ? " on" : "") }), ago(u.last_seen), u.last_ip ? h("div", { class: "small muted" }, u.last_ip) : null),
    h("div", { class: "c-act" }, blocked
      ? h("button", { class: "btn small ok", onclick: (ev) => { ev.stopPropagation(); unblockUser(u, reload); } }, "Unblock")
      : h("button", { class: "btn small danger", onclick: (ev) => { ev.stopPropagation(); blockUser(u, reload); } }, "Block")));
}
async function users(view) {
  const table = h("div", {});
  const search = h("input", { type: "search", placeholder: "Search by name, note, key ending, computer, MAC or IP…", value: state.search });
  const chips = h("div", { class: "chips" });
  const tag = liveTag();
  await loadOffers();
  const drawChips = () => { clear(chips); FILTERS.concat((state.offers || []).map((o) => [o.key, o.name])).forEach(([id, l]) => chips.append(h("span", { class: "chip" + (state.filter === id ? " on" : ""), onclick: () => { state.filter = id; drawChips(); load(); } }, l))); };
  let t;
  search.oninput = () => { state.search = search.value; clearTimeout(t); t = setTimeout(load, 250); };
  view.append(h("div", { class: "head" }, h("div", {}, h("h1", {}, "Users"), h("div", { class: "muted" }, "Every key, its credits and its computer · ", tag)),
    h("button", { class: "btn primary", onclick: () => newUser() }, "＋ New user")), h("div", { class: "row", style: { marginBottom: "12px" } }, h("div", { class: "grow" }, search)), chips, h("div", { style: { height: "12px" } }), table);
  const load = async () => {
    let j; try { j = await api("GET", "/licences?limit=200&filter=" + encodeURIComponent(state.filter) + "&search=" + encodeURIComponent(state.search)); } catch (e) { return; }
    clear(table);
    if (!j.items.length) { table.append(h("div", { class: "card empty" }, state.search || state.filter !== "all" ? "No users match." : "No users yet. Press “New user” to create the first key.")); stamp(tag); return; }
    table.append(h("div", { class: "ulist" }, h("div", { class: "urow uhead" }, ["User", "Credits", "Coupons", "Computer", "Last seen", ""].map((c) => h("div", {}, c))), j.items.map((u) => userRow(u, load))),
      h("div", { class: "small muted", style: { margin: "8px 4px" } }, j.total + " user" + (j.total === 1 ? "" : "s") + " · " + j.items.filter((u) => u.status === "blocked").length + " blocked in this list"));
    stamp(tag);
  };
  drawChips(); await load(); state.timer = setInterval(load, 20000);
}

/* ── dialogs ───────────────────────────────────────────────────────────────────────────────────────────────────── */
function modal(content, onClose) {
  const cover = h("div", { class: "cover center", onclick: (ev) => { if (ev.target === cover) close(); } });
  const close = () => { cover.remove(); if (onClose) onClose(); };
  cover.append(h("div", { class: "modal" }, content(close)));
  document.body.append(cover);
  return close;
}
function ask(title, text, okLabel, danger, field) {
  return new Promise((resolve) => {
    modal((close) => {
      const input = field ? h("input", { type: "text", placeholder: field }) : null;
      const done = (v) => { resolve(v); close(); };                     // (the answer FIRST: closing the window reports 'cancelled', and the first answer wins)
      return [h("h2", {}, title), h("p", { class: "muted" }, text), input ? h("label", { class: "f" }, input) : null,
        h("div", { class: "row", style: { justifyContent: "flex-end", marginTop: "14px" } }, h("button", { class: "btn", onclick: () => done(null) }, "Cancel"),
          h("button", { class: "btn " + (danger ? "danger" : "primary"), onclick: () => done(input ? input.value : true) }, okLabel))];
    }, () => resolve(null));
  });
}

async function newUser() {
  await loadOffers();
  modal((close) => {
    const label = h("input", { type: "text", placeholder: "e.g. Rahul Sharma" });
    const credits = h("input", { type: "number", min: "0", value: "10" });
    const note = h("input", { type: "text", placeholder: "Optional (phone number, who sold it…)" });
    const days = h("input", { type: "number", min: "1", placeholder: "No expiry" });
    const mac = h("input", { type: "text", placeholder: "Optional: only this computer, e.g. 3C:52:82:AA:BB:CC" });
    const picker = offerPicker((state.offers || []).map((o) => o.key));
    const err = h("div", { class: "err" });
    const create = async () => {
      err.textContent = "";
      const offers = picker.value();
      if (!offers.length) { err.textContent = "Tick at least one coupon."; return; }
      try {
        const j = await api("POST", "/licences", { label: label.value, credits: parseInt(credits.value || "0", 10), note: note.value, expires_days: days.value ? parseInt(days.value, 10) : null, mac_lock: mac.value, offers });
        close(); showKey(j.key, j.label, j.credits); if (state.page === "users") go("users"); if (state.page === "dashboard") go("dashboard");
      } catch (e) { err.textContent = e.message; }
    };
    return [h("h2", {}, "New user"), h("p", { class: "muted" }, "A key is made for this person. It works on one computer only."),
      h("label", { class: "f" }, h("span", {}, "Name"), label), h("div", { class: "row" }, h("label", { class: "f grow" }, h("span", {}, "Credits"), credits), h("label", { class: "f grow" }, h("span", {}, "Valid for (days)"), days)),
      h("label", { class: "f" }, h("span", {}, "Note"), note), h("label", { class: "f" }, h("span", {}, "Lock to a computer (MAC address)"), mac), picker.node, err,
      h("div", { class: "row", style: { justifyContent: "flex-end" } }, h("button", { class: "btn", onclick: close }, "Cancel"), h("button", { class: "btn primary", onclick: create }, "Create key"))];
  });
}
/* ── the things an admin does to one user: each says what it does, names the user, and tells what happened ─────────────────────── */
async function resetComputer(u, after) {
  const pc = u.bound ? (u.device.name || "their computer") : "";
  const r = await ask("Move " + u.label + "'s key to a new computer?", pc + " stops working at once. The key can then be used on ONE new computer: " + u.label + " types the same key there.", "Yes, reset " + u.label + "'s computer", false);
  if (r === null) return;
  try { await api("POST", "/licences/" + u.id + "/reset-device"); } catch (e) { if (e.code !== "unauthorized") toast(e.message, true); return; }
  toast("💻 " + u.label + "'s key was reset — " + (pc || "the old computer") + " is stopped; the key now works on ONE new computer");
  if (after) after();
}
async function revealKey(u) {
  try { const j = await api("POST", "/licences/" + u.id + "/reveal-key"); showKey(j.key, u.label, u.credits, "shown"); toast("👁️ Showing the key of " + u.label + " (this is written in the activity log)"); } catch (e) { if (e.code !== "unauthorized") toast(e.message, true); }
}
async function newKey(u, after) {
  const r = await ask("Make a new key for " + u.label + "?", "The old key stops working AT ONCE. " + u.label + "'s app locks and asks for the new key. Use this if the key was shared or lost.", "Yes, make a new key for " + u.label, true);
  if (r === null) return;
  let j; try { j = await api("POST", "/licences/" + u.id + "/regenerate-key"); } catch (e) { if (e.code !== "unauthorized") toast(e.message, true); return; }
  toast("🔑 New key made for " + u.label + " — the old key stopped working");
  if (after) after();
  showKey(j.key, u.label, u.credits, "new");
}
async function deleteUser(u, after) {
  const r = await ask("Delete " + u.label + "?", u.label + " (key …" + u.key_tail + ") and their whole history are removed for good, and their app stops. This cannot be undone.", "Yes, delete " + u.label, true);
  if (r === null) return;
  try { await api("DELETE", "/licences/" + u.id); } catch (e) { if (e.code !== "unauthorized") toast(e.message, true); return; }
  toast("🗑️ " + u.label + " was deleted", "stop");
  if (after) after();
}
function showKey(key, label, credits, mode) {
  const B = h("b", {}, label);
  const title = mode === "new" ? "New key for " + label : mode === "shown" ? "The key of " + label : "Key created for " + label;
  const lead = mode === "new" ? ["The old key stopped working. Send this new key to ", B, ": their app asks for it the next time they press Book."]
    : mode === "shown" ? ["This is the full key of ", B, "."] : ["Send this key to ", B, ". It starts working when they type it into Best Fare."];
  modal((close) => [h("h2", {}, title), h("p", { class: "muted" }, lead),
    h("div", { class: "keybox" }, key), h("div", { class: "row", style: { margin: "14px 0" } },
      h("button", { class: "btn primary", onclick: () => copy(key) }, "Copy key"),
      h("button", { class: "btn", onclick: () => copy("Your Best Fare key: " + key + "\nCredits: " + credits + "\nOpen Best Fare and enter this key when it asks. It works on one computer only.") }, "Copy message")),
    h("div", { class: "note" }, mode === "shown" ? "Looking at a key is written in the activity log." : "You can see this key again later from the user's page."),
    h("div", { class: "row", style: { justifyContent: "flex-end", marginTop: "14px" } }, h("button", { class: "btn", onclick: close }, "Done"))]);
}

/* ── one user (drawer) ─────────────────────────────────────────────────────────────────────────────────────────── */
async function openUser(id, tab, keepScroll) {
  if (!state.offers) await loadOffers();
  tab = tab || "overview";
  let d; try { d = (await api("GET", "/licences/" + id)).licence; } catch (e) { return; }
  d.events = d.events.map((e) => ({ ...e, label: d.label, licence_id: d.id }));                      // (this user's own events carry no name: it is filled in, so the sentences say who)
  document.querySelectorAll(".cover.drawer-cover").forEach((c) => c.remove());
  const cover = h("div", { class: "cover drawer-cover", onclick: (ev) => { if (ev.target === cover) cover.remove(); } });
  const reload = () => { const top = (cover.querySelector(".drawer") || {}).scrollTop || 0; cover.remove(); openUser(id, tab, top); if (state.page === "users") go("users"); };
  const amount = h("input", { type: "number", placeholder: "e.g. 10 or -2", style: { maxWidth: "130px" } });
  const note = h("input", { type: "text", placeholder: "Note (optional)" });
  const adjust = async (delta) => { if (!delta) return; await act(api("POST", "/licences/" + id + "/credits", { delta, note: note.value }), (delta > 0 ? "💳 " + d.label + " got +" + delta : "💳 " + d.label + " lost " + Math.abs(delta)) + " credit" + (Math.abs(delta) === 1 ? "" : "s")); reload(); };
  const head = h("div", { class: "head" }, h("div", {}, h("h1", {}, d.label), h("div", {}, statusBadge(d), " ", d.flagged ? h("span", { class: "badge bad" }, "⚠ " + d.flag_reason) : null,
      d.note ? h("div", { class: "muted", style: { marginTop: "4px" } }, d.note) : null)), h("button", { class: "btn small", onclick: () => cover.remove() }, "Close"));
  const creditsCard = h("div", { class: "card" }, h("h3", {}, "Credits"), h("div", { class: "row gap" }, h("div", { class: "big" }, d.credits), h("div", { class: "muted small" }, "1 credit = 1 traveller on 1 booking")),
    d.usage ? h("div", { class: "small muted", style: { margin: "2px 0 10px" } }, "Given " + d.usage.given + " · taken back by admin " + d.usage.removed + " · used by bookings " + d.usage.used + " · returned after failed bookings " + d.usage.returned + " · really spent " + d.usage.kept) : null,
    h("div", { class: "quick" }, [1, 5, 10, 25, 50].map((n) => h("button", { class: "btn small ok", onclick: () => adjust(n) }, "+" + n)), [1, 5].map((n) => h("button", { class: "btn small danger", onclick: () => adjust(-n) }, "−" + n))),
    h("div", { class: "row" }, amount, note, h("button", { class: "btn primary small", onclick: () => adjust(parseInt(amount.value, 10)) }, "Apply")));
  const arow = (icon, title, text, btn) => h("div", { class: "arow" }, h("div", { class: "aico" }, icon),
    h("div", { class: "atxt" }, h("div", { class: "atitle" }, title), h("div", { class: "asub" }, text)), h("div", { class: "abtn" }, btn));
  const pcName = d.bound ? (d.device.name || "their computer") : "";
  const actions = h("div", { class: "card actions" }, h("h3", {}, "What you can do with " + d.label),
    d.status === "blocked"
      ? arow("✅", "Let " + d.label + " back in", statusLine(d) + ". They cannot book until you unblock them.", h("button", { class: "btn ok", onclick: () => unblockUser(d, reload) }, "Unblock " + d.label))
      : arow("🚫", "Block " + d.label, "Stops them at once: every booking is refused and the app window locks. You can unblock them any time.", h("button", { class: "btn danger", onclick: () => blockUser(d, reload) }, "Block " + d.label)),
    arow("💻", "Move the key to a new computer", d.bound ? "The key works on " + pcName + " now. After the reset that computer stops at once and the key works on ONE new computer." : "The key has not been used yet, so there is nothing to reset.",
      h("button", { class: "btn", disabled: !d.bound, onclick: () => resetComputer(d, reload) }, "Reset computer")),
    arow("👁️", "Show the key", "Shows the full key so you can send it again. (Looking at a key is written in the activity log.)", h("button", { class: "btn", onclick: () => revealKey(d) }, "Show key")),
    arow("🔑", "Make a new key", "The old key stops working at once and " + d.label + "'s app asks for the new one. Use it when a key was shared or lost.", h("button", { class: "btn", onclick: () => newKey(d, reload) }, "New key")),
    arow("✏️", "Edit", "Name, note, which coupons they may use, validity, MAC lock.", h("button", { class: "btn", onclick: () => editUser(d, reload) }, "Edit")),
    state.me.role === "owner" ? arow("🗑️", "Delete " + d.label, "Removes the user and their whole history for good; their key stops working at once. This cannot be undone.",
      h("button", { class: "btn danger", onclick: () => deleteUser(d, () => { cover.remove(); if (state.page === "users") go("users"); }) }, "Delete")) : null);
  const tabs = h("div", { class: "tabs" }, [["overview", "Computer"], ["ledger", "Credit history"], ["bookings", "Bookings"], ["events", "Events"]].map(([k, l]) => h("div", { class: "tab" + (tab === k ? " on" : ""), onclick: () => { cover.remove(); openUser(id, k); } }, l)));
  let pane;
  if (tab === "overview") pane = h("div", { class: "card" }, h("div", { class: "kv" },
    h("div", { class: "k" }, "Key"), h("div", { class: "v mono" }, "BF-••••-••••-••••-" + d.key_tail),
    h("div", { class: "k" }, "Computer"), h("div", { class: "v" }, d.bound ? (d.device.name || "—") + " · " + (d.device.os || "") : "Not used yet"),
    h("div", { class: "k" }, "MAC address"), h("div", { class: "v mono" }, d.bound ? (d.device.macs || []).join(", ") || "—" : "—"),
    h("div", { class: "k" }, "Locked to MAC"), h("div", { class: "v mono" }, d.mac_lock || "no (first computer wins)"),
    h("div", { class: "k" }, "May use"), h("div", { class: "v" }, (d.offers || []).map((k) => h("div", {}, offerName(k)))),
    h("div", { class: "k" }, "First used"), h("div", { class: "v" }, fmtTime(d.bound_at)),
    h("div", { class: "k" }, "Last seen"), h("div", { class: "v" }, ago(d.last_seen) + (d.last_ip ? " from " + d.last_ip : "")),
    h("div", { class: "k" }, "App version"), h("div", { class: "v" }, d.client_build ? "build " + d.client_build : "—"),
    h("div", { class: "k" }, "Valid until"), h("div", { class: "v" }, d.expires_at ? fmtTime(d.expires_at) : "no expiry"),
    h("div", { class: "k" }, "Created"), h("div", { class: "v" }, fmtTime(d.created_at) + (d.created_by ? " by " + d.created_by : ""))));
  else if (tab === "ledger") pane = h("div", { class: "tablewrap" }, d.ledger.length ? h("table", {}, h("thead", {}, h("tr", {}, ["When", "Change", "Balance", "What", "Note"].map((c) => h("th", {}, c)))),
    h("tbody", {}, d.ledger.map((l) => h("tr", {}, h("td", { class: "small" }, fmtTime(l.ts)), h("td", { class: "credits" }, (l.delta > 0 ? "+" : "") + l.delta), h("td", {}, l.balance), h("td", {}, h("span", { class: "badge gray" }, l.kind)), h("td", { class: "small muted" }, [l.actor, l.note].filter(Boolean).join(" · ")))))) : h("div", { class: "empty" }, "No credit changes yet"));
  else if (tab === "bookings") pane = h("div", { class: "tablewrap" }, d.bookings_list.length ? h("table", {}, h("thead", {}, h("tr", {}, ["When", "Trip", "People", "Result", ""].map((c) => h("th", {}, c)))),
    h("tbody", {}, d.bookings_list.map((b) => h("tr", {}, h("td", { class: "small" }, fmtTime(b.ts)), h("td", {}, b.route + " · " + b.travel_date, h("div", { class: "small muted" }, b.offer)), h("td", {}, b.travellers),
      h("td", {}, h("span", { class: "badge " + (b.status === "done" ? "ok" : b.status === "failed" ? "gray" : "warn") }, b.status), b.pnr ? h("div", { class: "mono small" }, b.pnr) : null, b.refunded ? h("div", { class: "small muted" }, "credits returned") : (b.reason ? h("div", { class: "small muted" }, b.reason) : null)),
      h("td", {}, b.refunded ? null : h("button", { class: "btn small", onclick: async () => { if (await ask("Return the credits?", b.travellers + " credit(s) go back to " + d.label + ".", "Return")) { await act(api("POST", "/bookings/" + b.id + "/refund"), "Credits returned"); reload(); } } }, "Give credits back")))))) : h("div", { class: "empty" }, "No bookings yet"));
  else pane = h("div", { class: "card" }, d.events.length ? eventList(d.events) : h("div", { class: "empty" }, "No events"));
  const recent = h("div", { class: "card", style: { marginBottom: "12px" } }, h("h3", {}, "Latest changes to " + d.label), d.events.length ? eventList(d.events.slice(0, 5)) : h("div", { class: "muted small" }, "Nothing yet."), d.events.length > 5 ? h("a", { class: "btn small", style: { marginTop: "6px" }, onclick: () => { cover.remove(); openUser(id, "events"); } }, "See everything") : null);
  cover.append(h("div", { class: "drawer" }, head, statusBanner(d), h("div", { style: { height: "12px" } }), recent, creditsCard, h("div", { style: { height: "12px" } }), actions, tabs, pane));
  document.body.append(cover);
  if (keepScroll) cover.querySelector(".drawer").scrollTop = keepScroll;
}
async function editUser(d, done) {
  await loadOffers();
  modal((close) => {
    const label = h("input", { type: "text", value: d.label }); const note = h("input", { type: "text", value: d.note });
    const days = h("input", { type: "number", min: "1", placeholder: d.expires_at ? "Change validity (days from now)" : "Add a validity (days)" });
    const mac = h("input", { type: "text", value: d.mac_lock, placeholder: "Empty = no lock" }); const err = h("div", { class: "err" });
    const noexp = h("input", { type: "checkbox" });
    const picker = offerPicker(d.offers || []);
    const save = async () => {
      const offers = picker.value();
      if (!offers.length) { err.textContent = "Tick at least one coupon."; return; }
      const bits = [];
      if (label.value !== d.label) bits.push("name");
      if (note.value !== d.note) bits.push("note");
      if (mac.value !== d.mac_lock) bits.push("MAC lock");
      if (days.value || noexp.checked) bits.push(noexp.checked ? "no expiry" : "valid " + days.value + " more days");
      if (offers.join() !== (d.offers || []).join()) bits.push("coupons: " + offers.map((k) => "Offer " + k.slice(5)).join(" + "));
      if (!bits.length) { close(); toast("Nothing was changed for " + d.label); return; }
      try { await api("PATCH", "/licences/" + d.id, { label: label.value, note: note.value, mac_lock: mac.value, expires_days: days.value ? parseInt(days.value, 10) : null, clear_expiry: noexp.checked, offers }); close(); toast("✏️ Saved the changes to " + (label.value || d.label) + (bits.length ? " — " + bits.join(", ") : "")); done(); } catch (e) { err.textContent = e.message; }
    };
    return [h("h2", {}, "Edit " + d.label), h("label", { class: "f" }, h("span", {}, "Name"), label), h("label", { class: "f" }, h("span", {}, "Note"), note),
      h("label", { class: "f" }, h("span", {}, "Lock to a computer (MAC address)"), mac), h("label", { class: "f" }, h("span", {}, "Validity"), days),
      d.expires_at ? h("label", { class: "switch" }, noexp, "Remove the expiry date") : null, picker.node, err,
      h("div", { class: "row", style: { justifyContent: "flex-end", marginTop: "12px" } }, h("button", { class: "btn", onclick: close }, "Cancel"), h("button", { class: "btn primary", onclick: save }, "Save"))];
  });
}

/* ── activity ──────────────────────────────────────────────────────────────────────────────────────────────────── */
async function activity(view) {
  const list = h("div", { class: "card" });
  const chips = h("div", { class: "chips" });
  const tag = liveTag();
  const find = h("input", { type: "search", placeholder: "Find a user, an admin or an address…", value: state.evFind || "" });
  const FIL = [["all", "Everything"], ["mine", "What I did"], ["admin", "Admin actions"], ["users", "What users did"], ["warn", "Warnings"], ["alert", "Alerts"]];
  let events = [];
  const show = () => {
    let ev = events; const f = state.evFilter || "all";
    if (f === "mine") ev = ev.filter((e) => e.actor === state.me.username);
    else if (f === "admin") ev = ev.filter((e) => ADMIN_KINDS.has(e.kind));
    else if (f === "users") ev = ev.filter((e) => !ADMIN_KINDS.has(e.kind));
    else if (f === "warn") ev = ev.filter((e) => e.level === "warn" || e.level === "alert");
    else if (f === "alert") ev = ev.filter((e) => e.level === "alert");
    const q = (find.value || "").trim().toLowerCase();
    if (q) ev = ev.filter((e) => [e.label, e.actor, e.detail, e.kind, e.ip].filter(Boolean).join(" ").toLowerCase().includes(q));
    clear(list).append(ev.length ? eventList(ev) : h("div", { class: "empty" }, "Nothing matches."));
  };
  const draw = () => { clear(chips); FIL.forEach(([k, l]) => chips.append(h("span", { class: "chip" + ((state.evFilter || "all") === k ? " on" : ""), onclick: () => { state.evFilter = k; draw(); show(); } }, l))); };
  find.oninput = () => { state.evFind = find.value; show(); };
  view.append(h("div", { class: "head" }, h("div", {}, h("h1", {}, "Activity"), h("div", { class: "muted" }, "Who did what, to whom, and when · ", tag))),
    h("div", { class: "row", style: { marginBottom: "12px" } }, h("div", { class: "grow" }, find)), chips, h("div", { style: { height: "12px" } }), list);
  const load = async () => { try { events = (await api("GET", "/events?limit=300")).events; } catch (e) { return; } show(); stamp(tag); };
  draw(); await load(); state.timer = setInterval(load, 15000);
}

/* ── the installer: the file people download, and the one the installed apps update themselves with ───────────────────────── */
async function installerCard(box, owner, reload) {
  let j; try { j = await api("GET", "/installers"); } catch (e) { return; }
  const mb = (n) => (n / 1048576).toFixed(0) + " MB";
  const card = h("div", { class: "card", style: { marginBottom: "14px" } }, h("h2", {}, "Installer — the link to give people"),
    h("div", { class: "muted", style: { marginBottom: "10px" } }, "People open the link, press Download and run the installer. Installed apps update themselves when you publish a newer installer (the app itself) or a newer release (the program)."));
  if (j.link) {
    card.append(h("div", { class: "row", style: { alignItems: "center", marginBottom: "12px" } }, h("span", { class: "code", style: { flex: "1", overflowWrap: "anywhere" } }, j.link),
      h("button", { class: "btn small primary", onclick: () => copy(j.link) }, "Copy link"), h("a", { class: "btn small", href: j.link, target: "_blank", rel: "noopener" }, "Open")));
  } else {
    card.append(h("div", { class: "note", style: { marginBottom: "12px" } }, "The server does not know its public address yet. Start it with --public-url https://your-address and the link appears here."));
  }
  card.append(h("div", { class: "muted small", style: { marginBottom: "8px" } }, "To publish a new installer, on the developer's computer run: ", h("span", { class: "code" }, "python tools\publish_installer.py"), " (it builds, signs and uploads it here as a draft)."));
  if (!j.installers.length) {
    card.append(h("div", { class: "empty" }, "No installer has been uploaded yet."));
  } else {
    card.append(h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Version", "State", "Required", "Size", "Fingerprint", "Uploaded", "Notes", ""].map((c) => h("th", {}, c)))),
      h("tbody", {}, j.installers.map((r) => h("tr", {}, h("td", {}, h("b", {}, r.version)),
        h("td", {}, h("span", { class: "badge " + (r.state === "live" ? "ok" : r.state === "withdrawn" ? "bad" : "gray") }, r.state)), h("td", {}, r.mandatory ? "Yes" : "—"),
        h("td", {}, mb(r.size)), h("td", { class: "small code" }, r.sha256.slice(0, 12)), h("td", { class: "small" }, fmtTime(r.created_at)), h("td", { class: "small", style: { maxWidth: "220px" } }, r.notes),
        h("td", {}, owner ? h("div", { class: "row" },
          r.state !== "live" ? h("button", { class: "btn small primary", onclick: async () => { if (await ask("Make " + r.version + " the installer?", "The link gives out this one, and every installed app updates itself to it.", "Publish", false)) { await act(api("PATCH", "/installers/" + r.id, { state: "live" }), "Published"); reload(); } } }, "Publish")
            : h("button", { class: "btn small danger", onclick: async () => { if (await ask("Withdraw " + r.version + "?", "The link stops giving it out until you publish one again. Installed apps keep what they have.", "Withdraw", true)) { await act(api("PATCH", "/installers/" + r.id, { state: "withdrawn" }), "Withdrawn"); reload(); } } }, "Withdraw"),
          h("button", { class: "btn small", onclick: async () => { await act(api("PATCH", "/installers/" + r.id, { mandatory: !r.mandatory }), r.mandatory ? "Not required any more" : "Now required"); reload(); } }, r.mandatory ? "Make optional" : "Make required"),
          r.state !== "live" ? h("button", { class: "btn small danger", onclick: async () => { if (await ask("Delete " + r.version + "?", "The file is removed.", "Delete", true)) { await act(api("DELETE", "/installers/" + r.id), "Deleted"); reload(); } } }, "Delete") : null) : null)))))));
  }
  box.append(card);
}

/* ── updates ───────────────────────────────────────────────────────────────────────────────────────────────────── */
async function updates(view) {
  const box = h("div", {});
  view.append(h("div", { class: "head" }, h("div", {}, h("h1", {}, "Updates"), h("div", { class: "muted" }, "Publish a new version and every app updates itself"))), box);
  const load = async () => {
    let j; try { j = await api("GET", "/releases"); } catch (e) { return; }
    clear(box);
    const owner = state.me.role === "owner";
    box.append(h("div", { class: "card", style: { marginBottom: "14px" } }, h("h2", {}, "How to publish an update"),
      h("ol", { style: { margin: "0 0 0 18px", padding: 0, lineHeight: "1.8" } }, h("li", {}, "On the developer's computer run: ", h("span", { class: "code" }, "python tools\\publish_release.py"), " — it builds, signs and uploads the new version here as a draft."),
        h("li", {}, "Check it below, then press ", h("b", {}, "Publish"), ". Tick ", h("b", {}, "Required"), " if old versions must update before their next booking (use this when IndiGo changes something)."),
        h("li", {}, "Apps pick it up within minutes. To undo: ", h("b", {}, "Withdraw"), " it, and publish the fixed version as a new build.")),
      j.trusted ? null : h("div", { class: "note", style: { marginTop: "10px" } }, "This server does not trust any release key yet - no release can be uploaded. Run: python -m bfcloud trust-key <public key>.")));
    await installerCard(box, owner, load);
    if (!j.releases.length) { box.append(h("div", { class: "card empty" }, "No release has been uploaded yet.")); return; }
    box.append(h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Build", "Version", "State", "Required", "Roll-out", "Installed on", "Uploaded", "Notes", ""].map((c) => h("th", {}, c)))),
      h("tbody", {}, j.releases.map((r) => h("tr", {}, h("td", {}, h("b", {}, r.build)), h("td", {}, r.version),
        h("td", {}, h("span", { class: "badge " + (r.state === "live" ? "ok" : r.state === "withdrawn" ? "bad" : "gray") }, r.state)), h("td", {}, r.mandatory ? "Yes" : "—"), h("td", {}, r.rollout + "%"),
        h("td", {}, r.installed + " user" + (r.installed === 1 ? "" : "s")), h("td", { class: "small" }, fmtTime(r.created_at)), h("td", { class: "small", style: { maxWidth: "240px" } }, r.notes),
        h("td", {}, owner ? h("div", { class: "row" },
          r.state !== "live" ? h("button", { class: "btn small primary", onclick: async () => { const rl = await ask("Publish build " + r.build + "?", "Every app will update to it.", "Publish", false, "Roll-out % (1-100, empty = everyone)"); if (rl !== null) { await act(api("PATCH", "/releases/" + r.build, { state: "live", rollout: rl && rl !== true ? parseInt(rl, 10) : 100 }), "Published"); load(); } } }, "Publish") : h("button", { class: "btn small danger", onclick: async () => { if (await ask("Withdraw build " + r.build + "?", "New installs stop. Apps that already have it keep it until you publish a newer build.", "Withdraw", true)) { await act(api("PATCH", "/releases/" + r.build, { state: "withdrawn" }), "Withdrawn"); load(); } } }, "Withdraw"),
          h("button", { class: "btn small", onclick: async () => { await act(api("PATCH", "/releases/" + r.build, { mandatory: !r.mandatory }), r.mandatory ? "Not required any more" : "Now required"); load(); } }, r.mandatory ? "Make optional" : "Make required"),
          r.state !== "live" ? h("button", { class: "btn small danger", onclick: async () => { if (await ask("Delete build " + r.build + "?", "The file is removed.", "Delete", true)) { await act(api("DELETE", "/releases/" + r.build), "Deleted"); load(); } } }, "Delete") : null) : null)))))));
  };
  await load();
}

/* ── settings ──────────────────────────────────────────────────────────────────────────────────────────────────── */
async function settings(view) {
  let s; try { s = (await api("GET", "/settings")).settings; } catch (e) { return; }
  const save = async (obj, text) => { await act(api("PUT", "/settings", obj), text || "Saved"); go("settings"); };
  const maint = h("input", { type: "checkbox", checked: s.maintenance }); const mmsg = h("input", { type: "text", value: s.maintenance_msg });
  const bc = h("input", { type: "text", value: s.broadcast, placeholder: "Shown to every user in the app (empty = nothing)" });
  const minb = h("input", { type: "number", min: "0", value: s.min_build });
  const num = (k, v) => h("input", { type: "number", min: "0", value: v, "data-k": k });
  const lim = num("refund_daily_limit", s.refund_daily_limit), open = num("max_open", s.max_open), trav = num("max_travellers", s.max_travellers), sess = num("session_days", s.session_days), hourly = num("begin_per_hour", s.begin_per_hour), infantc = num("infant_credits", s.infant_credits);
  const offerBox = h("div", {});
  const offers = JSON.parse(JSON.stringify(s.offers));
  Object.entries(offers).forEach(([k, o]) => {
    const code = h("input", { type: "text", value: o.code }); const title = h("input", { type: "text", value: o.title }); const who = h("input", { type: "text", value: o.who }); const on = h("input", { type: "checkbox", checked: o.enabled });
    code.oninput = () => { o.code = code.value; }; title.oninput = () => { o.title = title.value; }; who.oninput = () => { o.who = who.value; }; on.onchange = () => { o.enabled = on.checked; };
    offerBox.append(h("div", { class: "card", style: { marginBottom: "10px" } }, h("div", { class: "row" }, h("b", {}, k), h("label", { class: "switch" }, on, "Available")),
      h("div", { class: "row" }, h("label", { class: "f grow" }, h("span", {}, "Promo code (given to the app only when a booking starts)"), code), h("label", { class: "f grow" }, h("span", {}, "Name"), title), h("label", { class: "f grow" }, h("span", {}, "For"), who))));
  });
  view.append(h("div", { class: "head" }, h("div", {}, h("h1", {}, "Settings"), h("div", { class: "muted" }, "Changes reach every app within minutes"))),
    h("div", { class: "card", style: { marginBottom: "14px" } }, h("h2", {}, "Pause everything"), h("p", { class: "muted" }, "Switch this on when IndiGo changes something and you need time to fix the app. Nobody can start a booking; credits are not touched."),
      h("label", { class: "switch" }, maint, "Maintenance mode"), h("label", { class: "f", style: { marginTop: "10px" } }, h("span", {}, "Message users see"), mmsg),
      h("button", { class: "btn primary", onclick: () => save({ maintenance: maint.checked, maintenance_msg: mmsg.value }) }, "Save")),
    h("div", { class: "card", style: { marginBottom: "14px" } }, h("h2", {}, "Message to all users"), h("label", { class: "f" }, bc), h("button", { class: "btn primary", onclick: () => save({ broadcast: bc.value }) }, "Send")),
    h("h2", {}, "Offers"), offerBox, h("button", { class: "btn primary", style: { marginBottom: "18px" }, onclick: () => save({ offers }) }, "Save offers"),
    h("div", { class: "card" }, h("h2", {}, "Rules"), h("div", { class: "row" }, h("label", { class: "f grow" }, h("span", {}, "Automatic refunds per user per day"), lim), h("label", { class: "f grow" }, h("span", {}, "Bookings in progress at once"), open),
      h("label", { class: "f grow" }, h("span", {}, "Most travellers in one booking"), trav)), h("div", { class: "row" }, h("label", { class: "f grow" }, h("span", {}, "Stay signed in (days)"), sess),
      h("label", { class: "f grow" }, h("span", {}, "Oldest app build allowed to book (0 = any)"), minb), h("label", { class: "f grow" }, h("span", {}, "Most bookings one user can start per hour"), hourly), h("label", { class: "f grow" }, h("span", {}, "Credits for an infant (1 = like everyone, 0 = free)"), infantc)),
      h("button", { class: "btn primary", onclick: () => save({ refund_daily_limit: +lim.value, max_open: +open.value, max_travellers: +trav.value, session_days: +sess.value, min_build: +minb.value, begin_per_hour: +hourly.value, infant_credits: +infantc.value }) }, "Save rules")));
}

/* ── admins and my account ─────────────────────────────────────────────────────────────────────────────────────── */
async function admins(view) {
  const box = h("div", {});
  view.append(h("div", { class: "head" }, h("div", {}, h("h1", {}, "Admins"), h("div", { class: "muted" }, "Owners manage everything. Admins do the daily work: users, credits, blocking.")),
    h("button", { class: "btn primary", onclick: newAdmin }, "＋ New admin")), box);
  const load = async () => {
    let j; try { j = await api("GET", "/admins"); } catch (e) { return; }
    clear(box).append(h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Username", "Role", "2-step", "Last sign-in", "Status", ""].map((c) => h("th", {}, c)))),
      h("tbody", {}, j.admins.map((a) => h("tr", {}, h("td", {}, h("b", {}, a.username)), h("td", {}, a.role), h("td", {}, a.totp_on ? "On" : "—"), h("td", { class: "small" }, ago(a.last_login)), h("td", {}, a.disabled ? h("span", { class: "badge bad" }, "Disabled") : h("span", { class: "badge ok" }, "Active")),
        h("td", {}, h("div", { class: "row" }, h("button", { class: "btn small", onclick: async () => { if (await ask("Reset password?", "A new password is made for " + a.username + " and shown once.", "Reset")) { const r = await act(api("PATCH", "/admins/" + a.id, { reset_password: true })); showSecret("New password for " + a.username, r.password); } } }, "Reset password"),
          h("button", { class: "btn small " + (a.disabled ? "ok" : "danger"), onclick: async () => { await act(api("PATCH", "/admins/" + a.id, { disabled: !a.disabled }), "Saved"); load(); } }, a.disabled ? "Enable" : "Disable"),
          h("button", { class: "btn small", onclick: async () => { await act(api("PATCH", "/admins/" + a.id, { role: a.role === "owner" ? "admin" : "owner" }), "Saved"); load(); } }, a.role === "owner" ? "Make admin" : "Make owner")))))))));
  };
  await load();
}
function newAdmin() {
  modal((close) => {
    const user = h("input", { type: "text", placeholder: "username" }); const role = h("select", {}, h("option", { value: "admin" }, "Admin (daily work)"), h("option", { value: "owner" }, "Owner (everything)")); const err = h("div", { class: "err" });
    const make = async () => { try { const j = await api("POST", "/admins", { username: user.value, role: role.value }); close(); showSecret("Password for " + user.value, j.password); if (state.page === "admins") go("admins"); } catch (e) { err.textContent = e.message; } };
    return [h("h2", {}, "New admin"), h("label", { class: "f" }, h("span", {}, "Username"), user), h("label", { class: "f" }, h("span", {}, "Role"), role), err,
      h("div", { class: "row", style: { justifyContent: "flex-end" } }, h("button", { class: "btn", onclick: close }, "Cancel"), h("button", { class: "btn primary", onclick: make }, "Create"))];
  });
}
function showSecret(title, secret) {
  modal((close) => [h("h2", {}, title), h("p", { class: "muted" }, "Shown once. Give it to them safely - they must change it at their first sign-in."), h("div", { class: "keybox" }, secret),
    h("div", { class: "row", style: { margin: "14px 0", justifyContent: "flex-end" } }, h("button", { class: "btn primary", onclick: () => copy(secret) }, "Copy"), h("button", { class: "btn", onclick: close }, "Done"))]);
}
async function myAccount() {
  modal((close) => {
    const old = h("input", { type: "password", autocomplete: "current-password" }); const nw = h("input", { type: "password", autocomplete: "new-password" }); const err = h("div", { class: "err" });
    const change = async () => { try { await api("POST", "/me/password", { old: old.value, new: nw.value }); close(); toast("Password changed - sign in again"); state.me = null; showLogin(); } catch (e) { err.textContent = e.message; } };
    const tf = h("div", {});
    const drawTf = async () => {
      let me; try { me = await api("GET", "/me"); } catch (e) { return; }
      clear(tf);
      if (me.totp_on) { const pw = h("input", { type: "password", placeholder: "Your password" }); tf.append(h("div", { class: "note" }, "Two-step sign-in is ON."), h("div", { class: "row", style: { marginTop: "8px" } }, pw, h("button", { class: "btn danger small", onclick: async () => { try { await api("POST", "/me/totp/disable", { password: pw.value }); toast("Turned off"); drawTf(); } catch (e) { err.textContent = e.message; } } }, "Turn off"))); }
      else {
        const pw = h("input", { type: "password", placeholder: "Your password", autocomplete: "current-password" });
        tf.append(h("div", { class: "note" }, "Two-step sign-in is off. Type your password to set it up."), h("div", { class: "row", style: { marginTop: "8px" } }, pw, h("button", { class: "btn", onclick: async () => {
          let j; try { j = await api("POST", "/me/totp/setup", { password: pw.value }); } catch (e) { err.textContent = e.message; return; }
          const code = h("input", { type: "text", inputmode: "numeric", placeholder: "6-digit code" });
          clear(tf).append(h("p", { class: "muted small" }, "In an authenticator app (Google Authenticator, Microsoft Authenticator…) add a key by hand with this secret, then type the code it shows:"), h("div", { class: "keybox", style: { fontSize: "16px" } }, j.secret),
            h("div", { class: "row", style: { marginTop: "8px" } }, code, h("button", { class: "btn primary small", onclick: async () => { try { await api("POST", "/me/totp/enable", { code: code.value }); toast("Two-step sign-in is on"); drawTf(); } catch (e) { err.textContent = e.message; } } }, "Turn on")));
        } }, "Set up two-step sign-in")));
      }
    };
    drawTf();
    return [h("h2", {}, "My account"), h("h3", {}, "Change password"), h("label", { class: "f" }, h("span", {}, "Current password"), old), h("label", { class: "f" }, h("span", {}, "New password (10+ characters, mixed)"), nw),
      h("button", { class: "btn primary", onclick: change }, "Change password"), err, h("h3", { style: { marginTop: "18px" } }, "Two-step sign-in"), tf,
      h("div", { class: "row", style: { justifyContent: "flex-end", marginTop: "14px" } }, h("button", { class: "btn", onclick: close }, "Close"))];
  });
}

/* ── start ─────────────────────────────────────────────────────────────────────────────────────────────────────── */
(async function init() {
  try { const me = await api("GET", "/me"); state.me = me; state.csrf = me.csrf; start(); } catch (e) { if (e.code !== "unauthorized") showLogin(); }
})();
