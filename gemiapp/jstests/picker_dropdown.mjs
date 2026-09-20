/**
 * Behaviour test for the Organization Radar criteria dropdown in static/js/app.js.
 *
 * Why this exists
 * ---------------
 * The bugs this file guards against are bugs of *event propagation*, and a Django test cannot see them:
 *   - choosing an option detaches the clicked node, so a bubble-phase "click outside" listener closed the
 *     dropdown on every selection;
 *   - the chevron closed the dropdown and the same click then reached the field's opener, which reopened it,
 *     so clicking the chevron of an open dropdown appeared to do nothing.
 * Both are invisible to a test that only reads the source, and both are produced by the real listeners talking
 * to each other. So this runs the real script against a DOM small enough to live in one file: no browser, no
 * jsdom, no new npm dependency. It implements only what the picker touches -- attribute/class selectors,
 * capture-then-bubble dispatch with stopPropagation, dataset, classList, focus -- and nothing else.
 *
 * Run it directly (`node gemiapp/jstests/picker_dropdown.mjs`) or through
 * gemiapp/test_organization_radar_ui.PickerDropdownTests, which is what CI does. It prints one "ok <name>"
 * line per check and exits non-zero on the first failure.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createContext, runInContext } from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const SCRIPT = join(here, "..", "..", "static", "js", "app.js");

/* ------------------------------------------------------------------ a DOM, only as much as the picker uses */

const dash = (key) => key.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`);

const compile = (selector) => {
  const tag = (selector.match(/^[a-zA-Z]+/) || [null])[0];
  const attrs = [...selector.matchAll(/\[([\w-]+)(?:=(?:"([^"]*)"|([^\]]*)))?\]/g)]
    .map((m) => [m[1], m[2] ?? m[3] ?? null]);
  const classes = [...selector.matchAll(/\.([\w-]+)/g)].map((m) => m[1]);
  return (el) =>
    (!tag || el.tag === tag.toUpperCase()) &&
    attrs.every(([name, value]) => el.hasAttribute(name) && (value === null || el.getAttribute(name) === value)) &&
    classes.every((name) => el.classList.contains(name));
};

class DomEvent {
  constructor(type, init = {}) {
    this.type = type;
    this.bubbles = Boolean(init.bubbles);
    this.key = init.key;
    this.target = null;
    this.defaultPrevented = false;
    this.stopped = false;
  }
  preventDefault() { this.defaultPrevented = true; }
  stopPropagation() { this.stopped = true; }
}

class El {
  constructor(tag) {
    this.tag = String(tag).toUpperCase();
    this.attrs = new Map();
    this.children = [];
    this.parentNode = null;
    this.listeners = [];
    this.style = {};
    this.text = "";
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.dataset = new Proxy({}, {
      get: (_, key) => this.attrs.get(`data-${dash(String(key))}`),
      set: (_, key, value) => { this.attrs.set(`data-${dash(String(key))}`, String(value)); return true; },
      has: (_, key) => this.attrs.has(`data-${dash(String(key))}`),
    });
    this.classList = {
      contains: (name) => this.classes().includes(name),
      add: (...names) => this.setClasses([...new Set([...this.classes(), ...names])]),
      remove: (...names) => this.setClasses(this.classes().filter((n) => !names.includes(n))),
      toggle: (name, state) => {
        const on = state === undefined ? !this.classes().includes(name) : Boolean(state);
        this.setClasses(on ? [...new Set([...this.classes(), name])] : this.classes().filter((n) => n !== name));
        return on;
      },
    };
  }
  classes() { return (this.attrs.get("class") || "").split(/\s+/).filter(Boolean); }
  setClasses(names) { this.attrs.set("class", names.join(" ")); }
  get className() { return this.attrs.get("class") || ""; }
  set className(value) { this.attrs.set("class", String(value)); }
  get textContent() { return this.text + this.children.map((c) => c.textContent).join(""); }
  set textContent(value) { this.children = []; this.text = String(value); }
  get type() { return this.attrs.get("type"); }
  set type(value) { this.attrs.set("type", String(value)); }
  get name() { return this.attrs.get("name"); }
  set name(value) { this.attrs.set("name", String(value)); }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  hasAttribute(name) { return this.attrs.has(name); }
  append(...nodes) { for (const node of nodes) { node.parentNode = this; this.children.push(node); } }
  replaceChildren(...nodes) { for (const child of this.children) child.parentNode = null; this.children = []; this.append(...nodes); }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter((c) => c !== this);
    this.parentNode = null;
  }
  descendants() { return this.children.flatMap((child) => [child, ...child.descendants()]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) { const match = compile(selector); return this.descendants().filter(match); }
  contains(node) { for (let n = node; n; n = n.parentNode) if (n === this) return true; return false; }
  closest(selector) { const match = compile(selector); for (let n = this; n && n.tag; n = n.parentNode) if (match(n)) return n; return null; }
  addEventListener(type, handler, options) {
    this.listeners.push({ type, handler, capture: options === true || Boolean(options && options.capture) });
  }
  scrollIntoView() {}
  focus() { if (doc.activeElement !== this) { doc.activeElement = this; dispatch(this, new DomEvent("focus")); } }
  blur() { if (doc.activeElement === this) doc.activeElement = null; }
  click() { dispatch(this, new DomEvent("click", { bubbles: true })); }
  dispatchEvent(event) { return dispatch(this, event); }
}

function fire(node, event, capture) {
  for (const entry of [...node.listeners]) {
    if (entry.type !== event.type || entry.capture !== capture) continue;
    entry.handler.call(node, event);
  }
}

function dispatch(target, event) {
  event.target = target;
  const path = [];
  for (let node = target; node; node = node.parentNode) path.push(node);
  for (let i = path.length - 1; i >= 0 && !event.stopped; i--) fire(path[i], event, true);
  if (event.bubbles) { for (const node of path) { if (event.stopped) break; fire(node, event, false); } }
  else if (!event.stopped) fire(target, event, false);
  return !event.defaultPrevented;
}

const doc = new El("#document");
doc.tag = "";                                  // so closest() stops here rather than matching the document
doc.activeElement = null;
doc.createElement = (tag) => new El(tag);
doc.getElementById = (id) => doc.querySelector(`[id="${id}"]`);
doc.createTextNode = (text) => { const node = new El("#text"); node.text = String(text); return node; };

const win = {
  location: { origin: "http://localhost" },
  matchMedia: () => ({ matches: false }),
  addEventListener: () => {},
  requestAnimationFrame: () => {},
};

/* ------------------------------------------------------------------ the page: one picker, as rendered */

const el = (tag, attrs = {}, children = []) => {
  const node = new El(tag);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
  node.append(...children);
  return node;
};

const input = el("input", { "data-picker-search": "", "aria-expanded": "false" });
const spinner = el("span", { "data-picker-spinner": "" });
spinner.hidden = true;
const toggleIcon = el("svg", {});
const toggle = el("button", { type: "button", "data-picker-toggle": "" }, [toggleIcon]);
const field = el("div", { "data-picker-field": "" }, [input, spinner, toggle]);
const results = el("div", { "data-picker-results": "", role: "listbox" });
results.hidden = true;
const chips = el("div", { "data-picker-selected": "" });
const status = el("p", { "data-picker-status": "" });
const select = el("select", { name: "prefectures", "data-picker-select": "", multiple: "" });
const fallback = el("div", { "data-picker-fallback": "" }, [select]);
const picker = el("div", {
  "data-reference-picker": "",
  "data-kind": "prefecture",
  "data-input-name": "prefectures",
  "data-emit": "values",
  "data-max-items": "50",
  "data-search-url": "/api/reference/",
}, [field, results, chips, status, fallback]);
const outside = el("main", {}, [picker]);
doc.append(outside);

/* ------------------------------------------------------------------ run the real script */

let pending = [];                              // resolvers for the in-flight search responses
const ROWS = [
  { value: "1", label: "CHIOU", detail: "" },
  { value: "2", label: "ATTIKIS", detail: "" },
];

const sandbox = {
  window: win,
  document: doc,
  console,
  setTimeout,
  clearTimeout,
  performance,
  URL,
  AbortController,
  IntersectionObserver: class { observe() {} disconnect() {} },
  requestAnimationFrame: () => {},
  fetch: (url, options) =>
    new Promise((resolve, reject) => {
      const signal = options && options.signal;
      const settle = () => resolve({ ok: true, json: () => Promise.resolve({ results: ROWS }) });
      if (signal) signal.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
      pending.push(settle);
    }),
};
sandbox.globalThis = sandbox;
createContext(sandbox);
runInContext(readFileSync(SCRIPT, "utf8"), sandbox, { filename: SCRIPT });
doc.dispatchEvent(new DomEvent("DOMContentLoaded", { bubbles: true }));

/* ------------------------------------------------------------------ the checks */

const settle = async () => {                   // let every queued response and promise callback run
  const queued = pending;
  pending = [];
  queued.forEach((resolve) => resolve());
  for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, 0));
};

let failures = 0;
const check = (name, condition) => {
  if (condition) { console.log(`ok ${name}`); return; }
  console.log(`FAIL ${name}`);
  failures += 1;
};
const isOpen = () => results.hidden === false;
const options = () => results.querySelectorAll('[role="option"]');

const run = async () => {
  check("the script takes over from the no-JavaScript control", fallback.hidden === true && select.disabled === true);
  check("the dropdown starts closed", !isOpen());

  // The production bug, both ways round.
  toggle.click();
  await settle();
  check("closed -> click chevron -> open", isOpen() && options().length === 2);

  toggle.click();
  await settle();
  check("open -> click chevron -> closed", !isOpen());

  toggle.click();
  await settle();
  check("closed -> click chevron -> open again", isOpen());

  // The rest of the field still only opens, and clicking it while open leaves it open.
  toggle.click();
  await settle();
  input.click();
  await settle();
  check("clicking the text input opens it", isOpen());
  field.click();
  await settle();
  check("clicking elsewhere in the field leaves it open", isOpen());

  // Focus opens it too.
  input.blur();
  doc.activeElement = null;
  toggle.click();                               // close, so focus has something to do
  await settle();
  input.focus();
  await settle();
  check("focusing the input opens it", isOpen());

  // Escape and a click outside still close it.
  input.dispatchEvent(new DomEvent("keydown", { bubbles: true, key: "Escape" }));
  check("Escape closes it", !isOpen());
  input.click();
  await settle();
  outside.click();
  check("a click outside closes it", !isOpen());

  // Choosing an option keeps the dropdown open and does not offer that row again.
  input.click();
  await settle();
  const chosen = options()[0];
  chosen.click();
  await settle();
  check("choosing an option keeps the dropdown open", isOpen());
  check("the chosen row is held as one chip", chips.querySelectorAll("[data-picker-chip]").length === 1);
  check("the chip posts the reference key", chips.querySelectorAll("[data-picker-value]").map((h) => `${h.name}=${h.value}`).join() === "prefectures=1");
  check("the chosen row is not offered again", options().length === 1);
  chips.querySelector("[data-picker-remove]").click();
  await settle();
  check("removing the chip offers the row again", options().length === 2 && !chips.querySelector("[data-picker-chip]"));

  // A response that lands after the customer closed the list must not reopen it. This needs a typed query:
  // the opening list is answered from the cache after the first time, so nothing would be in flight.
  input.click();
  await settle();
  input.value = "CHI";
  input.dispatchEvent(new DomEvent("input", { bubbles: true }));
  await new Promise((r) => setTimeout(r, 250));  // past the debounce
  const late = pending;
  pending = [];
  check("a typed query is really in flight", late.length === 1);
  toggle.click();                               // closed before the answer arrives
  late.forEach((resolve) => resolve());
  for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, 0));
  check("a late response does not reopen a closed dropdown", !isOpen());

  if (failures) { console.log(`${failures} check(s) failed`); process.exit(1); }
  console.log("all checks passed");
};

run().catch((error) => { console.error(error); process.exit(1); });
