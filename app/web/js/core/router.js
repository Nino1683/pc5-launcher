// Bildschirm-Stapel mit Übergängen.
// Ein Bildschirm-Modul: export default { id, create(params) -> { el, onShow?(info), onHide?(),
// onPad?(button, ev) -> bool, onEvent?(event, payload), destroy?() } }.
// Der oberste Bildschirm ist sichtbar und bedienbar; die darunter bleiben erhalten (mit ihrem
// Fokus) und bekommen weiterhin Ereignisse aus Python, damit sie beim Zurückkehren stimmen.
import { after, enter } from "./dom.js";
import { nav } from "./nav.js";
import { onAny } from "./bridge.js";

const modules = new Map();
const stack = [];               // {id, inst, scope, params}
let host = null;

function init(el) {
  host = el;
  onAny((event, payload) => {
    for (const item of [...stack]) {
      if (item.inst.onEvent) {
        try { item.inst.onEvent(event, payload); } catch (e) { console.error(item.id + ".onEvent(" + event + "): " + (e && e.stack || e)); }
      }
    }
  });
}

function register(...mods) {
  for (const mod of mods) modules.set(mod.id, mod);
}

function top() { return stack[stack.length - 1] || null; }

function mount(item, cls) {
  host.append(item.inst.el);
  item.inst.el.classList.add("screen");
  item.inst.el.dataset.screen = item.id;
  enter(item.inst.el, cls);
  item.scope = nav.pushScope(item.inst.el, {
    name: item.id,
    onPad: (button, ev) => (item.inst.onPad ? item.inst.onPad(button, ev) === true : false),
    onBack: () => back(),
  });
}

function leave(item, cls, remove) {
  const el = item.inst.el;
  el.classList.add(cls);
  el.inert = true;
  after("--t-screen", () => {
    el.classList.remove(cls);
    if (remove) { el.remove(); if (item.inst.destroy) item.inst.destroy(); }
    else if (top() !== item) el.hidden = true;
  });
}

function go(id, params = {}) {
  const mod = modules.get(id);
  if (!mod) throw new Error("Unbekannter Bildschirm: " + id);
  document.dispatchEvent(new Event("couch-screen-change"));
  const prev = top();
  const item = { id, params, inst: mod.create(params), scope: null };
  if (prev) {
    if (prev.inst.onHide) prev.inst.onHide();
    leave(prev, "is-leave", false);
  }
  stack.push(item);
  mount(item, prev ? "is-enter" : "is-back-enter");
  if (item.inst.onShow) item.inst.onShow({ back: false });
  nav.ensure();
  return item.inst;
}

// Ersetzt den obersten Bildschirm (Schritte eines Ablaufs: Kreis führt dann nicht zurück in einen
// erledigten Schritt, sondern dorthin, wo der Ablauf begann)
function replace(id, params = {}) {
  const old = top();
  const inst = go(id, params);
  if (old) {
    const index = stack.indexOf(old);
    if (index >= 0) stack.splice(index, 1);
    nav.dropScope(old.scope);
    old.inst.el.remove();
    if (old.inst.destroy) old.inst.destroy();
  }
  return inst;
}

// Geht einen Bildschirm zurück. Liefert false, wenn es keinen darunter gibt.
function back() {
  if (stack.length < 2) return false;
  document.dispatchEvent(new Event("couch-screen-change"));
  const item = stack.pop();
  if (item.inst.onHide) item.inst.onHide();
  const prev = top();
  prev.inst.el.hidden = false;
  prev.inst.el.inert = false;
  enter(prev.inst.el, "is-back-enter");
  // schließt auch alles, was über dem Bildschirm noch offen war (Menüs, Dialoge)
  nav.popScope(item.scope);
  leave(item, "is-back-leave", true);
  if (prev.inst.onShow) prev.inst.onShow({ back: true, from: item.id });
  nav.ensure();
  return true;
}

// Zurück bis zu einem bestimmten Bildschirm (z. B. nach dem Start eines Spiels)
function backTo(id) {
  let moved = false;
  while (stack.length > 1 && top().id !== id) { back(); moved = true; }
  return moved;
}

// Entfernt einen Bildschirm, egal wo er im Stapel liegt (Ladebild nach dem Start)
function close(id) {
  if (top() && top().id === id) return back();
  const index = stack.findIndex((s) => s.id === id);
  if (index < 0) return false;
  const [item] = stack.splice(index, 1);
  nav.dropScope(item.scope);
  item.inst.el.remove();
  if (item.inst.destroy) item.inst.destroy();
  return true;
}

export const router = {
  init, register, go, replace, back, backTo, close,
  get top() { return top(); },
  get ids() { return stack.map((s) => s.id); },
  has(id) { return stack.some((s) => s.id === id); },
  // E103: ist ein Bildschirm angemeldet? (Teile laden ihre Bildschirme selbst; fehlt einer, passiert nichts)
  known(id) { return modules.has(id); },
};
window.__router = router;
