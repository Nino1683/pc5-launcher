// Einstieg der Oberfläche. Eine Seite, zwei Rollen (URL-Parameter role):
//   home     Bildschirm-Stapel: Home, Bibliothek, Einstellungen …
//   overlay  durchsichtige Seite: Control Center, Bildschirmtastatur, Rückfragen, Mitteilungen
import { call, notify, on, role, isMock } from "./core/bridge.js";
import { h, loadIcons, cssMs } from "./core/dom.js";
import { nav } from "./core/nav.js";
import { router } from "./core/router.js";
import { sound } from "./core/sound.js";
import { store } from "./core/store.js";
import { S, fmt } from "./core/strings.js";
import { endingShown } from "./components/ending.js";
import { introActive, introWanted, skipIntro, startIntro } from "./components/intro.js";

const BEAT_MS = 5000;
const errors = [];              // Fehler der Seite; die Prüfung liest sie über window.__couchTest

// ------------------------------------------------------------------ Fehler sammeln und melden
function report(text) {
  errors.push(text);
  if (errors.length > 50) errors.shift();
  notify("sys.log", { level: "error", text: "[" + role + "] " + text });
}
window.addEventListener("error", (e) => report((e.message || "Fehler") + " (" + (e.filename || "").split("/").pop() + ":" + e.lineno + ")"));
window.addEventListener("unhandledrejection", (e) => report("Promise: " + ((e.reason && (e.reason.stack || e.reason.message)) || e.reason)));
const consoleError = console.error.bind(console);
console.error = (...args) => { consoleError(...args); report(args.map(String).join(" ")); };

// ------------------------------------------------------------------ Ruhe: Schimmer und Schreibmarke anhalten
let idleTimer = null;
function touch() {
  document.documentElement.classList.remove("is-idle");
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => document.documentElement.classList.add("is-idle"), cssMs("--t-idle"));
}

// ------------------------------------------------------------------ Eingabe
// Die letzten Tasten mit Bereich und Fokus davor (für Prüfläufe: wohin ging ein Kreuz, das „nichts
// tat“?). Liest window.__couchTest.report() mit.
const padLog = [];
function onPadEvent(ev) {
  if (!ev || !ev.button) return;
  const cur = nav.current;
  padLog.push({ t: Math.round(performance.now()), b: ev.button, r: !!ev.repeat, scope: nav.scope ? nav.scope.name : "",
    focus: cur ? (cur.dataset.id || cur.id || cur.className).slice(0, 40) : "" });
  if (padLog.length > 30) padLog.shift();
  touch();
  // Startanimation (E92): jede Taste überspringt sie und erreicht das Home noch nicht
  if (introActive()) { if (!ev.repeat) skipIntro(); return; }
  nav.handlePad(ev.button, { repeat: !!ev.repeat });
}

// Tastatur zum Ausprobieren im Mock-Betrieb (im echten Betrieb kommen Tasten nur vom Pad)
const KEYS = {
  ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
  Enter: "cross", Escape: "circle", Backspace: "circle", o: "options", x: "square", y: "triangle",
  q: "l1", e: "r1", 1: "l2", 3: "r2", t: "touchpad", c: "create", f: "l3", g: "r3",
};
function bindKeyboard() {
  window.addEventListener("keydown", (e) => {
    if (e.key === "p") { window.__couch.dispatch("ps", { kind: "short" }); return; }
    if (e.key === "h") { window.__couch.dispatch("ps", { kind: "long" }); return; }
    const button = KEYS[e.key];
    if (!button) return;
    e.preventDefault();
    window.__couch.dispatch("pad", { button, repeat: e.repeat });
  });
}

// ------------------------------------------------------------------ erstes Bild und Herzschlag
function lifeSigns() {
  // Doppeltes rAF: der erste Aufruf liegt vor dem Zeichnen, der zweite nach dem ersten Bild
  requestAnimationFrame(() => requestAnimationFrame(() => notify("sys.firstFrame", { role })));
  // Der Herzschlag kommt aus der Seite: zeichnet sie, aus dem Bildtakt; sonst aus dem Zeitgeber
  setInterval(() => {
    let sent = false;
    const send = () => { if (!sent) { sent = true; notify("sys.beat", { role }); } };
    if (store.visible) requestAnimationFrame(send);
    setTimeout(send, 1000);
  }, BEAT_MS);
}

// ------------------------------------------------------------------ Rolle home
async function startHome() {
  const [home, library, hub, loading, gameSettings, settings, dialogs, toasts] = await Promise.all([
    import("./screens/home.js"), import("./screens/library.js"), import("./screens/hub.js"), import("./screens/loading.js"),
    import("./screens/game-settings.js"), import("./screens/settings.js"), import("./components/dialog.js"), import("./components/toast.js"),
  ]);
  const app = h("div#app");
  document.body.append(app, h("div#layers"));
  router.init(app);
  router.register(home.default, library.default, hub.default, loading.default, gameSettings.default, settings.settingsRoot, settings.settingsPage);
  // Archiv-Installer, Dateiauswahl, Papierkorb, Bilder (Block web-library); ein Fehler hier
  // darf das Home nicht am Start hindern
  try {
    const [installer, files, trash, art, explorer, saves, store, storeItem, textEditor] = await Promise.all([
      import("./screens/installer.js"), import("./screens/file-browser.js"), import("./screens/trash.js"), import("./screens/art-picker.js"),
      import("./screens/explorer.js"), import("./screens/saves.js"), import("./screens/store.js"), import("./screens/store-item.js"),
      import("./screens/text-editor.js"),           // Text-Editor des Explorers (E99)
    ]);
    router.register(...installer.screens, files.default, trash.default, ...art.screens, explorer.default, explorer.trashScreen, saves.default,
      store.default, storeItem.default, textEditor.default);
  } catch (e) {
    report("Bildschirme des Installers: " + (e && e.stack || e));
  }
  // E103: neue Bildschirme, je Teil eine Datei (export const screens = [...])
  for (const path of ["./screens/setup.js", "./screens/stats.js", "./screens/backup.js", "./screens/cleanup.js"]) {
    try { const m = await import(path); router.register(...(m.screens || [])); }
    catch (e) { report("Bildschirme " + path + ": " + (e && e.stack || e)); }
  }
  router.go("home");
  // E105: Einrichtungs-Assistent beim ersten Start (sys.state.setup.due); die Startanimation liegt
  // darüber, der Assistent ist zu sehen, sobald sie ausblendet
  if (store.state.setup && store.state.setup.due && router.known("setup-welcome")) router.go("setup-welcome", { auto: true });

  // Start einer App: Ladebild und Fehlerdialog (screens/loading.js)
  const launch = loading.setupLaunchFlow();
  // PS lang führt immer zum Home zurück: alle Bildschirme darüber schließen
  // (nicht, solange die Tafel „wird ausgeschaltet“ steht: sie sperrt die Eingabe)
  on("ps", (p) => { if (p && p.kind === "long") { if (!launch.active && !endingShown()) router.backTo("home"); } });
  window.__toast = toasts.toast;
}

// ------------------------------------------------------------------ Rolle overlay
async function startOverlay() {
  const overlay = await import("./overlay/overlay.js");
  const host = h("div#overlay");
  document.body.append(host, h("div#layers"));
  overlay.init(host);
}

// ------------------------------------------------------------------ Prüfhilfen (tools/web-shot)
function round(n) { return Math.round(n * 100) / 100; }
window.__couchTest = {
  errors,
  extra: null,                 // freies Feld: Prüfschritte legen hier Ergebnisse ab (js:-Schritt in tools/web-shot)
  focusCount: () => document.querySelectorAll(".is-focus").length,
  // Winkel des Ring-Schimmers: ändert er sich zwischen zwei Abfragen, läuft die Animation
  ringAngle() {
    const el = document.querySelector(".is-focus");
    return el ? getComputedStyle(el, "::after").getPropertyValue("--ring-angle") : null;
  },
  // Zustand in einem Stück, als JSON-Text (evaluate_javascript liefert keine Objekte mit Promises)
  report(selectors = []) {
    const focus = [...document.querySelectorAll(".is-focus")];
    const rect = (el) => { const r = el.getBoundingClientRect(); return [round(r.left), round(r.top), round(r.width), round(r.height)]; };
    const rects = {};
    for (const sel of selectors) rects[sel] = [...document.querySelectorAll(sel)].slice(0, 60).map(rect);
    return JSON.stringify({
      role, mock: isMock, w: innerWidth, h: innerHeight, rem: parseFloat(getComputedStyle(document.documentElement).fontSize),
      screens: router.ids, scopes: nav.depth, focusCount: focus.length,
      focus: focus[0] ? { cls: focus[0].className, id: focus[0].id || focus[0].dataset.id || focus[0].dataset.rowId || "", text: (focus[0].textContent || "").trim().slice(0, 60), rect: rect(focus[0]) } : null,
      errors: [...errors], overflow: this.overflow(), rects,
      calls: window.__mock ? window.__mock.calls.map((c) => c.method) : [],
      sounds: [...sound.played],
      pads: padLog.slice(-12),
      classes: document.documentElement.className,
      extra: this.extra,
    });
  },
  // Texte, die über ihren Kasten hinauslaufen, ohne gekürzt zu sein (sollte leer sein)
  overflow() {
    const out = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
    for (let el = walker.nextNode(); el; el = walker.nextNode()) {
      if (el.closest("[hidden], [inert], #sprite, #dev-backdrop") || !el.childNodes.length) continue;
      if (![...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())) continue;
      const style = getComputedStyle(el);
      if (style.visibility === "hidden" || Number(style.opacity) === 0) continue;
      // Text in einem kürzenden Eltern-Kasten (Laufschrift der Auswahlliste, auch während sie läuft):
      // sichtbar ist der Kasten
      const host = el.parentElement && (getComputedStyle(el.parentElement).textOverflow === "ellipsis" || el.classList.contains("mq")) ? el.parentElement : null;
      const r = (host || el).getBoundingClientRect();
      if (!r.width || !r.height) continue;
      const clipped = !!host || style.textOverflow === "ellipsis" || style.webkitLineClamp !== "none" || style.overflow === "hidden";
      // Der Fokusring (::after mit negativem inset, base.css) zählt in scrollWidth mit: Sein Überstand rechts
      // ist kein Überlauf (sonst Fehlalarm beim fokussierten Reiter .tab, der eigenen Text trägt)
      let ring = 0;
      if (el.matches(".is-focus, .is-held")) ring = Math.max(0, -parseFloat(getComputedStyle(el, "::after").right) || 0);
      const tooWide = el.scrollWidth > el.clientWidth + 1 + ring && !clipped;
      const bounds = el.closest("[data-bounds]");
      let outside = false;
      if (bounds) {
        const b = bounds.getBoundingClientRect();
        // Eine Tafel, die senkrecht rollt (lange Menüs, E74): weggerollte Einträge liegen gewollt außerhalb
        const rolls = bounds.scrollHeight > bounds.clientHeight + 1 && getComputedStyle(bounds).overflowY !== "visible";
        outside = r.left < b.left - 1 || r.right > b.right + 1 || (!rolls && (r.top < b.top - 1 || r.bottom > b.bottom + 1));
      }
      const offscreen = r.right > innerWidth + 1 && r.left < innerWidth && !el.closest(".tile-row, .lib-view, .cc-cards");
      if (tooWide || outside || offscreen) out.push({ text: el.textContent.trim().slice(0, 40), cls: el.className, why: tooWide ? "breiter als Kasten" : outside ? "ragt aus der Tafel" : "ragt aus dem Bild" });
    }
    return out;
  },
  soundTest: (pack = "") => sound.selfTest(pack).then((r) => JSON.stringify(r)),
};

// ------------------------------------------------------------------ Start
async function boot() {
  document.body.dataset.role = role;
  await loadIcons();
  nav.init();
  await store.init();
  on("pad", onPadEvent);
  on("ps", () => { touch(); if (introActive()) skipIntro(); });
  if (isMock) bindKeyboard();
  if (role === "home" && introWanted()) {
    // vor dem Aufbau des Home: Das erste Bild ist schon die Startanimation
    startIntro(new Promise((resolve) => document.addEventListener("couch-home-ready", resolve, { once: true })));
  }
  if (role === "overlay") await startOverlay(); else await startHome();
  touch();
  sound.preload();
  lifeSigns();
  window.__couchReady = true;
}

boot().catch((e) => report("Start: " + (e && e.stack || e)));
