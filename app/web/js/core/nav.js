// Fokus-Steuerung (Bauplan Abschnitt 5).
// - Fokussierbar ist, was data-focus trägt. Das fokussierte Element hat die Klasse is-focus;
//   solange der aktive Bereich etwas Fokussierbares enthält, gibt es genau eines.
// - Bereiche stapeln sich (pushScope/popScope): Dialog über Menü über Bildschirm. Jeder Bereich
//   merkt sich seinen Fokus und bekommt ihn zurück, wenn der Bereich darüber schließt.
// - Richtungen suchen das nächste Element nach Lage (getBoundingClientRect). Vorgaben:
//   data-nav-up|down|left|right="<Selektor>" (fester Nachbar) oder "none" (hier ist Schluss).
// - Gruppen (data-focus-group an einem Vorfahren) merken sich ihr zuletzt fokussiertes Element:
//   Wer von außen hineinwandert, landet wieder dort (sonst auf data-focus-default).
// - Rechter Stick hoch/runter (rs-up/rs-down) rollt die Liste oder das Raster, in dem der Fokus
//   steht (Vorfahr mit data-scroll): Zeile für Zeile, gehalten schneller (padlogic), und nie aus
//   der Liste hinaus zu Reitern oder Knöpfen; am Ende schlägt er an. Außerhalb einer solchen
//   Liste tut er nichts. (E99)
// - Ereignisse am fokussierten Element (steigen auf):
//   "nav-move" {dir, repeat, scroll?}  vor der Suche, abbrechbar (preventDefault = selbst behandelt);
//                             scroll: true, wenn der rechte Stick rollt
//   "nav-focus" / "nav-blur"  bei jedem Wechsel
//   "press"                   Kreuz-Taste
import { sound } from "./sound.js";

const DIRS = { up: [0, -1], down: [0, 1], left: [-1, 0], right: [1, 0] };
const scopes = [];              // {el, onPad, onBack, last, name}
const groupMemory = new WeakMap();
let current = null;
let observer = null;
let ensureQueued = false;

function top() { return scopes[scopes.length - 1] || null; }

export function isFocusable(el) {
  if (!el || !el.isConnected || !el.hasAttribute("data-focus")) return false;
  if (el.hasAttribute("data-disabled") || el.getAttribute("aria-disabled") === "true") return false;
  if (el.closest("[inert], [hidden]")) return false;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
}

export function focusables(root) {
  const base = root || (top() && top().el);
  if (!base) return [];
  return [...base.querySelectorAll("[data-focus]")].filter(isFocusable);
}

function groupOf(el) { return el ? el.closest("[data-focus-group]") : null; }

function defaultIn(root) {
  if (!root) return null;
  const marked = [...root.querySelectorAll("[data-focus-default]")].find(isFocusable);
  return marked || focusables(root)[0] || null;
}

function fire(el, type, detail, cancelable = false) {
  const ev = new CustomEvent(type, { bubbles: true, cancelable, detail: detail || {} });
  el.dispatchEvent(ev);
  return ev;
}

// Setzt den Fokus. Liefert true, wenn el jetzt den Fokus hat.
export function focus(el, info = {}) {
  if (typeof el === "string") el = (top() ? top().el.querySelector(el) : null) || document.querySelector(el);
  if (!isFocusable(el)) return false;
  const s = top();
  if (s && !s.el.contains(el)) return false;
  if (el === current) {
    if (!el.classList.contains("is-focus")) el.classList.add("is-focus");
    return true;
  }
  const old = current;
  // Aufräumen auch dort, wo ein Baustein die Klasse von Hand stehen ließ: es gibt nur einen Fokus
  for (const stale of document.querySelectorAll(".is-focus")) stale.classList.remove("is-focus");
  current = el;
  el.classList.add("is-focus");
  if (s) s.last = el;
  const group = groupOf(el);
  if (group) groupMemory.set(group, el);
  if (old && old.isConnected) fire(old, "nav-blur", { to: el, ...info });
  fire(el, "nav-focus", { from: old, ...info });
  return true;
}

function clearFocus() {
  for (const stale of document.querySelectorAll(".is-focus")) stale.classList.remove("is-focus");
  current = null;
}

function rectOf(el) {
  const r = el.getBoundingClientRect();
  return { l: r.left, t: r.top, r: r.right, b: r.bottom, cx: r.left + r.width / 2, cy: r.top + r.height / 2, w: r.width, h: r.height };
}

// Nächstes Element in einer Richtung nach Lage. Elemente, die sich in der Querrichtung mit dem
// Ausgangselement überdecken, gewinnen gegen schräg liegende.
function spatial(from, dir, candidates) {
  const [dx, dy] = DIRS[dir];
  const a = rectOf(from);
  let best = null;
  let bestScore = Infinity;
  for (const el of candidates) {
    if (el === from) continue;
    const b = rectOf(el);
    let primary, cross, overlap;
    if (dx) {
      if (dx > 0 ? b.cx <= a.cx + 1 || b.l < a.l + 1 : b.cx >= a.cx - 1 || b.r > a.r - 1) continue;
      primary = dx > 0 ? Math.max(0, b.l - a.r) : Math.max(0, a.l - b.r);
      overlap = Math.min(a.b, b.b) - Math.max(a.t, b.t);
      cross = overlap > 0 ? 0 : -overlap;
      overlap = overlap > 0 ? overlap / Math.min(a.h, b.h) : 0;
    } else {
      if (dy > 0 ? b.cy <= a.cy + 1 || b.t < a.t + 1 : b.cy >= a.cy - 1 || b.b > a.b - 1) continue;
      primary = dy > 0 ? Math.max(0, b.t - a.b) : Math.max(0, a.t - b.b);
      overlap = Math.min(a.r, b.r) - Math.max(a.l, b.l);
      cross = overlap > 0 ? 0 : -overlap;
      overlap = overlap > 0 ? overlap / Math.min(a.w, b.w) : 0;
    }
    const centers = dx ? Math.abs(b.cy - a.cy) : Math.abs(b.cx - a.cx);
    // Querabstand zählt dreifach; ohne Überdeckung zusätzlich ein Aufschlag in Größe des Elements
    const score = primary + cross * 3 + (overlap > 0 ? 0 : (dx ? a.h : a.w)) + centers * 0.05 - overlap;
    if (score < bestScore) { bestScore = score; best = el; }
  }
  return best;
}

function resolveFixed(from, dir) {
  const spec = from.getAttribute("data-nav-" + dir);
  if (spec === null) return undefined;       // keine Vorgabe
  if (spec === "none" || spec === "") return null;
  const s = top();
  const el = (s && s.el.querySelector(spec)) || document.querySelector(spec);
  return isFocusable(el) ? el : undefined;   // Ziel gerade nicht da: normale Suche
}

// Bewegt den Fokus. Liefert true, wenn sich etwas getan hat (oder ein Baustein selbst gehandelt hat).
export function move(dir, repeat = false) {
  ensure();
  if (!current) return false;
  const ev = fire(current, "nav-move", { dir, repeat }, true);
  if (ev.defaultPrevented) return true;
  let target = resolveFixed(current, dir);
  if (target === undefined) {
    target = spatial(current, dir, focusables());
    // In eine andere Gruppe hinein: dorthin, wo man sie zuletzt verlassen hat
    const group = groupOf(target);
    if (target && group && group !== groupOf(current)) {
      const remembered = groupMemory.get(group);
      if (isFocusable(remembered) && group.contains(remembered)) target = remembered;
      else {
        const marked = [...group.querySelectorAll("[data-focus-default]")].find(isFocusable);
        if (marked) target = marked;
      }
    }
  }
  if (!target) { sound.play("edge", { repeat }); return false; }
  focus(target, { dir, repeat });
  sound.play("focus", { repeat });
  return true;
}

// Rechter Stick: in der Liste (data-scroll) eine Zeile weiter. Liefert true, wenn er dort etwas tat.
export function scroll(dir, repeat = false) {
  ensure();
  const list = current && current.closest("[data-scroll]");
  if (!list) return false;
  const ev = fire(current, "nav-move", { dir, repeat, scroll: true }, true);
  if (ev.defaultPrevented) return true;
  const target = spatial(current, dir, focusables(list));
  if (!target) { sound.play("edge", { repeat }); return true; }
  focus(target, { dir, repeat });
  sound.play("focus", { repeat });
  return true;
}

export function press() {
  ensure();
  if (!current) return false;
  const ev = fire(current, "press", { sound: "select" }, true);
  if (ev.detail.sound) sound.play(ev.detail.sound);
  return true;
}

export function back() {
  const s = top();
  if (s && s.onBack) { sound.play("back"); s.onBack(); return true; }
  return false;
}

// Eine Taste des Pads. Der oberste Bereich darf sie zuerst behandeln.
export function handlePad(button, ev = {}) {
  ensure();
  const s = top();
  if (s && s.onPad && s.onPad(button, ev) === true) return true;
  if (DIRS[button]) return move(button, !!ev.repeat);
  if (button === "rs-up" || button === "rs-down") return scroll(button === "rs-up" ? "up" : "down", !!ev.repeat);
  if (button === "cross") return ev.repeat ? false : press();
  if (button === "circle") return ev.repeat ? false : back();
  return false;
}

export function pushScope(el, opts = {}) {
  const prev = top();
  if (prev) {
    prev.last = current;
    if (current) current.classList.add("is-held");
  }
  const scope = { el, onPad: opts.onPad || null, onBack: opts.onBack || null, last: null, name: opts.name || "" };
  scopes.push(scope);
  clearFocus();
  const initial = typeof opts.initial === "string" ? el.querySelector(opts.initial) : opts.initial;
  if (!focus(initial)) focus(defaultIn(el));
  return scope;
}

// Schließt den obersten Bereich oder, mit Angabe, diesen samt allen darüber.
export function popScope(scope) {
  let index = scope ? scopes.indexOf(scope) : scopes.length - 1;
  if (index < 0) return;
  scopes.length = index;
  clearFocus();
  for (const held of document.querySelectorAll(".is-held")) held.classList.remove("is-held");
  for (const below of scopes.slice(0, -1)) if (below.last && below.last.isConnected) below.last.classList.add("is-held");
  const s = top();
  if (s && !focus(s.last)) focus(defaultIn(s.el));
}

// Nimmt einen Bereich aus dem Stapel, der nicht oben liegt (ein verdeckter Bildschirm wird
// entfernt); der Fokus bleibt, wo er ist.
export function dropScope(scope) {
  const index = scopes.indexOf(scope);
  if (index < 0) return;
  if (index === scopes.length - 1) popScope(scope);
  else scopes.splice(index, 1);
}

// Stellt die Regel "genau ein Fokus" wieder her, nachdem sich das Dokument geändert hat.
export function ensure() {
  const s = top();
  if (!s) { if (current) clearFocus(); return; }
  if (current && isFocusable(current) && s.el.contains(current)) {
    if (!current.classList.contains("is-focus")) current.classList.add("is-focus");
    return;
  }
  clearFocus();
  if (!focus(s.last)) focus(defaultIn(s.el));
}

function queueEnsure() {
  if (ensureQueued) return;
  ensureQueued = true;
  queueMicrotask(() => { ensureQueued = false; ensure(); });
}

export function init() {
  if (observer) return;
  // Bildschirme bauen ihren Inhalt nach, sobald Daten da sind; der Fokus folgt von selbst
  observer = new MutationObserver(queueEnsure);
  observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["hidden", "inert", "data-disabled", "data-focus"] });
}

export const nav = {
  init, focus, move, scroll, press, back, handlePad, pushScope, popScope, dropScope, ensure, focusables, isFocusable,
  get current() { return current; },
  get scope() { return top(); },
  get depth() { return scopes.length; },
};
window.__nav = nav;
