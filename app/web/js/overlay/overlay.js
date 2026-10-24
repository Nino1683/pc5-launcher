// Rolle overlay: eine durchsichtige Seite über Spiel oder Home (Bauplan E6). Sie zeigt immer
// höchstens eine Ansicht (Control Center, Bildschirmtastatur, Rückfrage, Hinweis) und dazu
// Mitteilungen oben rechts und Hinweis-Karten. Python öffnet mit dem Ereignis
// overlay.open {view, params, serial}; die Seite meldet mit overlay.close {view, serial}, wenn
// sie von sich aus fertig ist (Kreis, PS, Wahl getroffen).
import { after, enter } from "../core/dom.js";
import { on, notify } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { sound } from "../core/sound.js";
import { endingShown } from "../components/ending.js";
import { toast } from "../components/toast.js";
import { createControlCenter } from "./control-center.js";
import { createOsk } from "./osk.js";
import { createConfirm, createDisconnected, createQuitProgress, createSaver, showHint, hideHint } from "./notices.js";

let host = null;
let active = null;           // {name, serial, view, scope, el}

const VIEWS = { cc: createControlCenter, osk: createOsk, confirm: createConfirm, disconnected: createDisconnected, "quit-progress": createQuitProgress, saver: createSaver };
// Ansichten ohne Ton beim Öffnen: Hinweise, die von selbst kommen
const SILENT = new Set(["disconnected", "quit-progress", "saver"]);

// ---------------------------------------------------------------- Verwaltung
function remove(item, animate) {
  if (!item) return;
  nav.popScope(item.scope);
  document.dispatchEvent(new Event("couch-screen-change"));   // offene Menüs und Dialoge schließen
  if (item.view.destroy) item.view.destroy();
  item.el.classList.add("is-leave");
  item.el.inert = true;
  if (animate) after("--t-overlay-out", () => item.el.remove()); else item.el.remove();
}

// Die Seite hat eine Ansicht von sich aus geschlossen: Python Bescheid geben, sobald sie
// ausgeblendet ist. Mit Namen und der laufenden Nummer aus overlay.open: Ist die Meldung schon
// unterwegs, während Python eine gleichnamige Ansicht neu öffnet (PS zweimal kurz
// hintereinander), schließt sie nur die alte. Und gar nicht, wenn die Seite inzwischen schon
// die nächste Ansicht zeigt.
function closedByPage(name, serial) {
  after("--t-overlay-out", () => { if (!active) notify("overlay.close", { view: name, serial }); });
}

function open(name, params = {}, serial = null) {
  const make = VIEWS[name];
  if (!make) { console.error("overlay.open: unbekannte Ansicht " + name); return; }
  // Dieselbe Ansicht noch einmal: nur neue Angaben übernehmen (Fortschrittsring)
  if (active && active.name === name && active.view.set && name === "quit-progress") { active.view.set(params.progress); active.serial = serial; return; }
  if (active) remove(active, false);
  // Eine Ansicht zum Bedienen braucht den Platz: die Hinweis-Karte weicht
  if (!SILENT.has(name)) hideHint();
  const item = { name, serial, view: null, scope: null, el: null };
  const api = {
    isActive: () => active === item,
    // Die Seite schließt von sich aus: erst ausblenden, dann Python Bescheid geben
    close() {
      if (active !== item) return;
      active = null;
      if (!item.view.quiet) sound.play("overlay-close");
      remove(item, true);
      closedByPage(name, item.serial);
    },
  };
  active = item;
  try {
    item.view = make(params || {}, api);
  } catch (e) {
    // Eine kaputte Ansicht darf das Overlay nicht offen und unbedienbar stehen lassen
    active = null;
    console.error("overlay.open " + name + ": " + (e && e.stack || e));
    notify("overlay.close", { view: name, serial });
    return;
  }
  item.el = item.view.el;
  item.el.classList.add("ov-view");
  item.el.dataset.view = name;
  host.append(item.el);
  enter(item.el);
  item.scope = nav.pushScope(item.el, {
    name: "overlay:" + name,
    onPad: (button, ev) => (item.view.onPad ? item.view.onPad(button, ev) === true : false),
    onBack: () => (item.view.onBack ? item.view.onBack() : api.close()),
  });
  // Rückfragen hängen als Schicht über der Ansicht; der Bereich der Ansicht liegt darunter
  if (item.view.onOpen) item.view.onOpen();
  if (!item.view.quiet && !SILENT.has(name)) sound.play("overlay-open");
}

function closeFromCore() {
  if (!active) return;
  const item = active;
  active = null;
  remove(item, true);
}

export function init(el) {
  host = el;
  on("overlay.open", (p) => open(p.view, p.params || {}, p.serial === undefined ? null : p.serial));
  on("overlay.close", closeFromCore);
  // Mitteilung: Standzeit aus der Einstellung „Anzeigedauer“ (toast.seconds)
  on("toast", (p) => toast({ id: p.id, title: p.title, body: p.body, icon: p.icon, sound: p.sound, seconds: p.seconds }));
  // Einmaliger Bedienhinweis (z. B. Belegung des Maus-Modus); das Overlay ist dafür nur sichtbar, nicht bedienbar
  on("hint", (p) => { showHint(p, host); });
  on("ps", (p) => {
    if (!active || endingShown()) return;           // die Tafel „wird ausgeschaltet“ sperrt die Eingabe
    if (active.view.onPs) active.view.onPs(p);
    else if (p.kind === "short" || p.kind === "long") {
      // PS schließt, was offen ist (PS lang führt Python danach zum Home)
      const item = active;
      active = null;
      sound.play("overlay-close");
      remove(item, true);
      closedByPage(item.name, item.serial);
    }
  });
  window.__overlay = { open, get active() { return active ? active.name : null; } };
}
