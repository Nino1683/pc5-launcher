// Zustand aus Python: einmal über sys.state geholt, danach über das Ereignis "state" (Teilmengen)
// aktuell gehalten. Bausteine abonnieren einzelne Schlüssel.
// Einstellungen der Oberfläche (state.ui, docs/RPC.md „State“) wirken hier sofort auf das Dokument:
//   reduceMotion  -> Klasse reduce-motion am <html>
//   scale         -> data-ui-scale="1.1" | "1.2" am <html> (tokens.css vergrößert dann nur die Schrift)
//   themeCss      -> <style id="theme"> am Ende von <head>: überschreibt Farb-Tokens aus tokens.css (Design, E113;
//                    der Kern liefert nur geprüfte :root-Deklarationen, couch/themes.py)
//   clock24h, sounds, soundVolume, liveBackground lesen die Bausteine selbst (statusbar.js, sound.js, home.js)
import { call, on, role } from "./bridge.js";

const UI_DEFAULTS = { sounds: true, soundVolume: 60, reduceMotion: false, scale: 1, clock24h: true, liveBackground: true,
  theme: "", themeCss: "" };
const SCALES = [1, 1.1, 1.2];

const DEFAULTS = {
  role, version: "", time: 0, foreground: null, overlay: "hidden",
  running: [], pads: [], volume: { level: 0, muted: false, sink: "" },
  network: { state: "unknown", type: "", name: "" }, hdr: { supported: false, enabled: false, active: false },
  notifications: 0, jobs: [], art_job: null, update_available: false, mouseMode: false, visible: true, saver: false,
  ui: { ...UI_DEFAULTS },
};

const state = structuredClone(DEFAULTS);
const subs = new Set();          // {keys: Set|null, fn}
let ready = false;

// Nur die drei Stufen der Einstellung; alles andere (Unsinn in settings.json) gilt als 1
function scaleOf(ui) {
  const raw = Number(ui && ui.scale);
  return SCALES.find((s) => Math.abs(s - raw) < 0.01) || 1;
}

// Zu sehen ist die Seite nur, wenn Gamescope eine unserer Seiten zeigt UND (im Home) keine App
// vorn ist: sys.state.visible bleibt auch dann true, wenn nur das Overlay über einer App steht.
// Hinter dem Spiel soll das Home nichts zeichnen (E33). Ebenso wenig unter dem Bildschirmschoner
// (state.saver, E99): Er ist eine passive Ansicht wie eine Mitteilung, deckt das Home aber ganz zu.
function seen() {
  if (state.visible === false) return false;
  if (role === "home" && state.saver === true) return false;
  return !(role === "home" && state.foreground);
}

// Design (E113): ein eigenes <style> nach allen Stylesheets, gleiche Spezifität wie tokens.css (:root) gewinnt so
function applyTheme(css) {
  const text = typeof css === "string" ? css : "";
  let el = document.getElementById("theme");
  if (!text) { if (el) el.remove(); return; }
  if (!el) {
    el = document.createElement("style");
    el.id = "theme";
    document.head.appendChild(el);
  }
  if (el.textContent !== text) el.textContent = text;
}

function applyClasses() {
  const el = document.documentElement;
  // Home: nicht sichtbar heißt, eine App liegt davor. Overlay: versteckt.
  el.classList.toggle("is-hidden", !seen());
  // Home unter einer bedienbaren Ansicht des Overlays (Control Center, Tastatur, Rückfrage):
  // Seine Kopfzeile tritt zurück, sonst scheint sie verschwommen hinter der des Overlays durch
  el.classList.toggle("is-overlaid", role === "home" && state.overlay === "active");
  el.classList.toggle("reduce-motion", !!(state.ui && state.ui.reduceMotion));
  const scale = scaleOf(state.ui);
  if (scale === 1) el.removeAttribute("data-ui-scale"); else el.setAttribute("data-ui-scale", String(scale));
  applyTheme(state.ui && state.ui.themeCss);
}

function merge(partial) {
  const changed = [];
  for (const [key, value] of Object.entries(partial || {})) {
    if (key === "ui" && value && typeof value === "object") state.ui = { ...UI_DEFAULTS, ...state.ui, ...value };
    else state[key] = value;
    changed.push(key);
  }
  applyClasses();
  for (const sub of [...subs]) {
    if (!sub.keys || changed.some((k) => sub.keys.has(k))) {
      try { sub.fn(state, changed); } catch (e) { console.error("store: " + (e && e.stack || e)); }
    }
  }
}

async function init() {
  on("state", merge);
  // app.changed trägt die Liste der laufenden Apps; so bleibt der Zustand auch ohne eigenes
  // state-Ereignis stimmig
  on("app.changed", (p) => { if (p && Array.isArray(p.running)) merge({ running: p.running }); });
  try {
    merge(await call("sys.state"));
  } catch (e) {
    console.error("sys.state: " + e.message);
    applyClasses();
  }
  ready = true;
}

// subscribe(fn) für alles oder subscribe(["running", "pads"], fn) für einzelne Schlüssel
function subscribe(keys, fn) {
  if (typeof keys === "function") { fn = keys; keys = null; }
  const sub = { keys: keys ? new Set([].concat(keys)) : null, fn };
  subs.add(sub);
  return () => subs.delete(sub);
}

function isRunning(entryId) {
  return (state.running || []).some((r) => r.entryId === entryId);
}

// Läuft noch, wird aber gerade beendet (Schonfrist): nicht mehr „Fortsetzen“ anbieten
function isClosing(entryId) {
  return (state.running || []).some((r) => r.entryId === entryId && r.closing);
}

export const store = {
  state, init, subscribe, merge, isRunning, isClosing,
  get ready() { return ready; },
  get visible() { return seen(); },
  get ui() { return state.ui || UI_DEFAULTS; },
  get scale() { return scaleOf(state.ui); },
};
window.__store = store;
