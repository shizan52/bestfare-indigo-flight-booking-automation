"use strict";
/* Runs the REAL admin-panel script (web/app.js) against a tiny fake page and a fake server, and checks that every button that changes a user
   first ASKS, and then really sends what it promised - and says so, with the user's name.
   (This is what was once broken: the confirmation window always answered "no", so Block / Reset computer / New key / Delete did nothing.)
   Usage: node panel_harness.js path/to/app.js */
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

class Node_ {}
class TextNode extends Node_ { constructor(t) { super(); this.data = String(t); this.parent = null; } get textContent() { return this.data; } }
class El extends Node_ {
  constructor(tag) { super(); this.tag = tag; this.children = []; this.parent = null; this.attrs = {}; this.style = {}; this.className = ""; this.value = ""; this.checked = false; this.disabled = false; }
  append(...kids) { for (const k of kids) { if (k.parent) k.parent.removeChild(k); k.parent = this; this.children.push(k); } }
  removeChild(k) { this.children = this.children.filter((c) => c !== k); k.parent = null; return k; }
  get firstChild() { return this.children[0] || null; }
  remove() { if (this.parent) this.parent.removeChild(this); }
  setAttribute(k, v) { this.attrs[k] = v; }
  get textContent() { return this.children.map((c) => c.textContent).join(""); }
  set textContent(t) { this.children = [new TextNode(t)]; }
  all() { return this.children.flatMap((c) => (c instanceof El ? [c, ...c.all()] : [])); }
}

const body = new El("body");
const document = {
  body, createElement: (t) => new El(t), createTextNode: (t) => new TextNode(t),
  getElementById: (id) => body.all().find((e) => e.attrs.id === id) || null, querySelector: () => null, querySelectorAll: () => [],
};
const calls = [];
let failNext = null;
const fakeApi = async (method, path, body_) => {
  calls.push([method, path, body_]);
  if (failNext) { const e = failNext; failNext = null; throw e; }
  if (path === "/settings") return { ok: true, settings: { offers: { offer1: { who: "Defence", enabled: true }, offer2: { who: "Senior", enabled: true } } } };
  if (path.endsWith("/regenerate-key") || path.endsWith("/reveal-key")) return { ok: true, key: "BF-AAAA-BBBB-CCCC-DDDD" };
  return { ok: true };
};

const sandbox = { document, Node: Node_, setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {}, console, navigator: {}, fetch: async () => { throw new Error("no network in this test"); } };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8").split("/* ── start")[0], sandbox, { filename: "app.js" });
sandbox.api = fakeApi;
vm.runInContext('state.me = { username: "boss", role: "owner" }; state.csrf = "x";', sandbox);
const { ask, blockUser, unblockUser, resetComputer, newKey, deleteUser, revealKey, editUser } = sandbox;
for (const [n, f] of Object.entries({ ask, blockUser, unblockUser, resetComputer, newKey, deleteUser, revealKey, editUser })) assert.strictEqual(typeof f, "function", "the panel has no " + n + "()");

const tick = () => new Promise((r) => setImmediate(r));
const same = (a, b, msg) => assert.strictEqual(JSON.stringify(a), JSON.stringify(b), msg);          // (objects made inside the page have their own prototypes: compare by content)
const find = (pred) => body.all().find(pred);
const button = (label) => { const b = find((e) => e.tag === "button" && e.textContent === label); assert(b, "no button called '" + label + "'; the window says: " + body.textContent.slice(0, 240)); return b; };
const click = (label) => button(label).onclick({ target: null });
const toasts = () => { const b = document.getElementById("toasts"); return b ? b.children.map((t) => t.textContent) : []; };
const clearToasts = () => { const b = document.getElementById("toasts"); if (b) b.children.slice().forEach((c) => c.remove()); };
const reset = () => { calls.length = 0; clearToasts(); };
const U = { id: 7, label: "Anita Roy", key_tail: "4XDN", bound: true, device: { name: "ANITA-PC" }, credits: 5, status: "active", block_reason: "" };

async function main() {
  // ── the confirmation window: it must hand back the answer ───────────────────────────────────────────────────────────────────────────
  let p = ask("Title", "text", "Do it", false); click("Do it"); assert.strictEqual(await p, true, "OK must answer yes");
  p = ask("Title", "text", "Do it", false); click("Cancel"); assert.strictEqual(await p, null, "Cancel must answer no");
  p = ask("Title", "text", "Go", true, "Reason"); find((e) => e.tag === "input").value = "because"; click("Go"); assert.strictEqual(await p, "because", "the typed reason must come back");
  p = ask("Title", "text", "Go", true); const cover = body.children[body.children.length - 1]; cover.onclick({ target: cover }); assert.strictEqual(await p, null, "clicking outside must answer no");
  assert.strictEqual(body.children.filter((c) => c.className.includes("cover")).length, 0, "every window closed");

  // ── Block ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  reset(); let after = 0;
  p = blockUser(U, () => after++);
  assert(body.textContent.includes("Block Anita Roy?"), "the window names the user");
  find((e) => e.tag === "input").value = "shared the key"; click("Yes, block Anita Roy"); await p;
  same(calls, [["POST", "/licences/7/block", { reason: "shared the key" }]]); assert.strictEqual(after, 1, "the page refreshes after a block");
  assert(toasts().some((t) => t.includes("Anita Roy is now BLOCKED")), "the result is announced with the name: " + toasts());
  await find((e) => e.className === "undo").onclick(); same(calls[calls.length - 1], ["POST", "/licences/7/unblock", undefined], "Undo unblocks");
  const refreshed = after;                                                                  // (the Undo above refreshed the page once more)
  reset(); p = blockUser(U, () => after++); click("Cancel"); await p; assert.strictEqual(calls.length, 0, "Cancel sends nothing"); assert.strictEqual(after, refreshed, "Cancel does not refresh");
  reset(); failNext = { code: "error", message: "The server said no" }; after = 0;
  p = blockUser(U, () => after++); click("Yes, block Anita Roy"); await p; assert.strictEqual(after, 0, "a failed block must not pretend it worked"); assert(toasts().includes("The server said no"));

  // ── Unblock ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  reset(); after = 0; const B = { ...U, status: "blocked", block_reason: "shared the key" };
  p = unblockUser(B, () => after++); assert(body.textContent.includes("Unblock Anita Roy?")); click("Yes, unblock Anita Roy"); await p;
  same(calls, [["POST", "/licences/7/unblock", undefined]]); assert.strictEqual(after, 1);
  assert(toasts().some((t) => t.includes("Anita Roy is unblocked")), String(toasts()));
  await find((e) => e.className === "undo").onclick(); same(calls[calls.length - 1], ["POST", "/licences/7/block", { reason: "shared the key" }], "Undo blocks again with the same reason");

  // ── Reset computer ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  reset(); after = 0;
  p = resetComputer(U, () => after++); assert(body.textContent.includes("ANITA-PC stops working at once"), "the window names the computer"); click("Yes, reset Anita Roy's computer"); await p;
  same(calls, [["POST", "/licences/7/reset-device", undefined]]); assert.strictEqual(after, 1);
  assert(toasts().some((t) => t.includes("Anita Roy") && t.includes("ANITA-PC")), String(toasts()));
  reset(); p = resetComputer(U, () => after++); click("Cancel"); await p; assert.strictEqual(calls.length, 0);

  // ── Show key (no question: looking is harmless, and it is written in the log) ────────────────────────────────────────────────────────
  reset(); await revealKey(U);
  same(calls, [["POST", "/licences/7/reveal-key", undefined]]);
  assert(body.textContent.includes("The key of Anita Roy") && find((e) => e.className === "keybox").textContent === "BF-AAAA-BBBB-CCCC-DDDD"); click("Done");

  // ── New key ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  reset(); after = 0;
  p = newKey(U, () => after++); assert(body.textContent.includes("The old key stops working AT ONCE")); click("Yes, make a new key for Anita Roy"); await p;
  same(calls, [["POST", "/licences/7/regenerate-key", undefined]]); assert.strictEqual(after, 1);
  assert(body.textContent.includes("New key for Anita Roy") && find((e) => e.className === "keybox").textContent === "BF-AAAA-BBBB-CCCC-DDDD", "the new key is shown"); click("Done");
  reset(); p = newKey(U, () => after++); click("Cancel"); await p; assert.strictEqual(calls.length, 0, "Cancel makes no new key");

  // ── Delete ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  reset(); after = 0;
  p = deleteUser(U, () => after++); assert(body.textContent.includes("Delete Anita Roy?") && body.textContent.includes("cannot be undone")); click("Yes, delete Anita Roy"); await p;
  same(calls, [["DELETE", "/licences/7", undefined]]); assert.strictEqual(after, 1); assert(toasts().some((t) => t.includes("Anita Roy was deleted")));
  reset(); p = deleteUser(U, () => after++); click("Cancel"); await p; assert.strictEqual(calls.length, 0, "Cancel deletes nothing");

  // ── Edit: sends what changed, says what changed, and says so when nothing did ─────────────────────────────────────────────────────────
  reset(); after = 0; const D = { ...U, note: "", mac_lock: "", expires_at: null, offers: ["offer1", "offer2"] };
  p = editUser(D, () => after++); await tick();
  const texts = body.all().filter((e) => e.tag === "input" && e.attrs.type === "text"); texts[1].value = "friend"; click("Save"); await tick(); await p;
  const patch = calls.find((c) => c[0] === "PATCH"); assert(patch, "Save sends the change"); assert.strictEqual(patch[1], "/licences/7");
  same(patch[2], { label: "Anita Roy", note: "friend", mac_lock: "", expires_days: null, clear_expiry: false, offers: ["offer1", "offer2"] });
  assert(toasts().some((t) => t.includes("Saved the changes to Anita Roy") && t.includes("note")), String(toasts())); assert.strictEqual(after, 1);
  reset(); p = editUser(D, () => after++); await tick(); click("Save"); await tick(); await p;
  assert(!calls.some((c) => c[0] === "PATCH"), "nothing changed, nothing sent"); assert(toasts().some((t) => t.includes("Nothing was changed for Anita Roy")), String(toasts()));
  console.log("OK");
}
main().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
