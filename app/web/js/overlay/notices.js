// Kleine Ansichten und Hinweise des Overlays, die der Kern auslöst:
//   confirm        Rückfrage {id, title, text, buttons: [{value, label}], default, timeout}
//                  -> overlay.confirmResult {id, value}; Kreis, PS und Zeitablauf liefern default
//   disconnected   „Controller getrennt“ (letztes Pad weg, Spiel vorn)
//   quit-progress  Fortschrittsring, solange PS + Optionen gehalten werden
//   Hinweis-Karte  Ereignis hint {id: "mouse-mode", seconds}: Belegung des Maus-Modus, ohne Fokus;
//                  {id: "cards-mode", layout, spots?, seconds}: Belegung des Zeiger-Modus;
//                  {id: "opti-mode", seconds}: Belegung im Menü von OptiScaler (E118)
//   saver          Bildschirmschoner nach Leerlauf (idle.py): schwarz, eine leise Uhr, die jede
//                  Minute woanders steht (gegen Einbrennen); jede Taste und PS schließen ihn
import { h, icon, after, enter } from "../core/dom.js";
import { notify } from "../core/bridge.js";
import { store } from "../core/store.js";
import { S } from "../core/strings.js";
import { dialog } from "../components/dialog.js";
import { clockLabel } from "../components/statusbar.js";

// ---------------------------------------------------------------- Bildschirmschoner
export function createSaver(params, api) {
  const clock = h("div.saver-clock");
  const el = h("div.saver", clock);
  const place = () => {
    clock.textContent = clockLabel(new Date(), store.ui.clock24h !== false);
    const w = Math.max(0, innerWidth - clock.offsetWidth);
    const hgt = Math.max(0, innerHeight - clock.offsetHeight);
    clock.style.left = Math.round(Math.random() * w) + "px";
    clock.style.top = Math.round(Math.random() * hgt) + "px";
  };
  let timer = null;
  // Schließt die Ansicht vor dem ersten Bild (Taste im selben Bild, Kern schließt), darf die Uhr
  // nicht an einem gelösten Knoten weiterlaufen und sich jede Minute neu einplanen (E99)
  let closed = false;
  const tick = () => {
    if (closed) return;
    place();
    timer = setTimeout(tick, (60 - new Date().getSeconds()) * 1000 + 50);
  };
  const frame = requestAnimationFrame(tick);
  return {
    el, quiet: true,
    onPad() { api.close(); return true; },
    onPs() { api.close(); },
    destroy() { closed = true; cancelAnimationFrame(frame); clearTimeout(timer); },
  };
}

// ---------------------------------------------------------------- Rückfrage
// Ohne buttons gilt die alte Form {ok?, cancel?} mit true/false.
export function createConfirm(params, api) {
  const el = h("div.ov-confirm");
  const given = Array.isArray(params.buttons) && params.buttons.length ? params.buttons : null;
  const buttons = given
    ? given.map((b, i) => ({ id: "b" + i, label: b.label || String(b.value), value: b.value }))
    : [{ id: "b0", label: params.cancel || S.cancel, value: false }, { id: "b1", label: params.ok || S.ok, value: true }];
  const fallback = given ? (params.default === undefined ? null : params.default) : !!params.default;
  // Der Fokus liegt auf der Vorgabe (der ungefährlichen Wahl), sonst auf dem ersten Knopf
  const initial = (buttons.find((b) => b.value === fallback) || buttons[0]).id;
  let answered = false;
  const answer = (value) => {
    if (answered || !api.isActive()) return;
    answered = true;
    notify("overlay.confirmResult", { id: params.id, value });
    api.close();
  };
  return {
    el, quiet: true,
    // Die PS-Taste beantwortet eine Rückfrage nicht stillschweigend mit Ja: sie gilt als Vorgabe
    onPs(p) { if (p.kind === "short" || p.kind === "long") answer(fallback); },
    // erst öffnen, wenn der Bereich der Ansicht steht: der Dialog legt seinen eigenen darüber
    onOpen() {
      dialog({
        title: params.title || "", text: params.text || "",
        timeout: Number(params.timeout) > 0 ? Number(params.timeout) : 0, timeoutResult: initial, initial,
        buttons: buttons.map((b) => ({ id: b.id, label: b.label })),
      }).then((result) => {
        const hit = buttons.find((b) => b.id === result);
        answer(hit ? hit.value : fallback);
      });
    },
  };
}

// ---------------------------------------------------------------- Controller getrennt
// Nichts zu bedienen, also auch kein Fokus. Jede Taste eines Pads beweist, dass wieder eines da ist.
export function createDisconnected(params, api) {
  // Nach dem Aufwachen (params.resume): Die Verbindung, mit der die PS-Taste geweckt hat, reißt beim
  // Neustart der USB-Geräte ab; der Kern schließt den Hinweis, sobald der Controller wieder da ist
  const title = params.resume ? S.padWakeTitle : params.connected ? S.padBackTitle : S.padLostTitle;
  const text = params.text || (params.resume ? S.padWakeText : params.connected ? S.padBackText : S.padLostText);
  const el = h("div.ov-center",
    h("div.notice", { "data-bounds": true }, icon("gamepad"),
      // Wieder angesteckt: Die Überschrift sagt das auch (vorher stand dort weiter „getrennt“)
      h("div.notice-title", { text: title }),
      h("div.notice-text", { text })));
  return { el, onPad() { api.close(); return true; }, onPs() { api.close(); } };
}

// ---------------------------------------------------------------- Spiel wird beendet
// Fortschritt 0…1 kommt mit dem Ereignis "ps" {kind: "options", progress}
export function createQuitProgress(params) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "ringp");
  svg.setAttribute("viewBox", "0 0 100 100");
  const mk = (cls) => { const c = document.createElementNS(NS, "circle"); c.setAttribute("class", cls); for (const [k, v] of Object.entries({ cx: 50, cy: 50, r: 44, pathLength: 100 })) c.setAttribute(k, v); return c; };
  const arc = mk("ringp-arc");
  svg.append(mk("ringp-rest"), arc);
  const set = (p) => arc.style.setProperty("stroke-dashoffset", String(100 - Math.max(0, Math.min(1, Number(p) || 0)) * 100));
  set(params.progress);
  const el = h("div.ov-center",
    h("div.notice", { "data-bounds": true }, svg,
      h("div.notice-title", { text: S.quitTitle }),
      h("div.notice-text.clamp2", { text: params.title || S.quitText }),
      h("div.notice-hint", { text: S.quitText })));
  return {
    el, set,
    onPs(p) {
      if (p.kind === "options") set(p.cancelled ? 0 : p.progress);
      // Losgelassen, bevor der Ring voll war: Python meldet den Abbruch und schließt die
      // Ansicht selbst (overlay.close); hier nur den Ring zurückstellen
      if (p.kind === "options-cancel") set(0);
    },
    onPad() { return true; },
  };
}

// ---------------------------------------------------------------- Hinweis-Karte
// Belegung des Maus-Modus, wie app/couch/input/padmouse.py sie festlegt (dort die Quelle; Dreieck
// und die PS-Taste behandelt die Shell). Auch die Tafel „Maus-Modus“ im Control Center zeigt sie.
export const MOUSE_KEYS = [
  ["mouseStickL", "mouseStickLDo"],
  ["mouseStickR", "mouseStickRDo"],
  ["mouseClick", "mouseClickDo"],
  ["mouseL2", "mouseL2Do"],
  ["mouseR3", "mouseR3Do"],
  ["mouseL3", "mouseL3Do"],
  ["mouseDpad", "mouseDpadDo"],
  ["mouseTriangle", "mouseTriangleDo"],
  ["mouseCircle", "mouseCircleDo"],
  ["mouseSquare", "mouseSquareDo"],
  ["mouseL1R1", "mouseL1R1Do"],
  ["mousePs", "mousePsDo"],
];

// Zeiger-Modus (profile "cards", E97) für Spiele, die man nur mit der Maus spielt; die Sprungstellen
// je Spiel stehen in padmouse.LAYOUTS (Ereignis hint {id: "cards-mode", layout, spots?})
export const CARDS_KEYS = [
  ["mouseStickL", "mouseStickLDo"],
  ["cardsClick", "cardsClickDo"],
  ["cardsCancel", "cardsCancelDo"],
  ["mouseStickR", "mouseStickRDo"],
  ["mouseL3", "mouseL3Do"],
  ["cardsOptions", "cardsOptionsDo"],
  ["mouseTriangle", "mouseTriangleDo"],
  ["mousePs", "mousePsDo"],
];
// OptiScaler-Menü (profile "opti", E118): Kreuz ist dort die Leertaste, Options Einfg (padmouse.KEYS_OPTI)
export const OPTI_KEYS = [
  ["mouseDpad", "optiDpadDo"],
  ["optiCross", "optiCrossDo"],
  ["mouseCircle", "optiCircleDo"],
  ["mouseL1R1", "optiL1R1Do"],
  ["mouseStickL", "mouseStickLDo"],
  ["optiR2", "optiR2Do"],
  ["mouseStickR", "mouseStickRDo"],
  ["cardsOptions", "optiOptionsDo"],
];
export const CARDS_SPOTS = {
  hearthstone: [
    ["cardsR1", "hsEndTurn"], ["cardsL1", "hsBoard"], ["cardsUp", "hsEnemyHero"],
    ["cardsDown", "hsHand"], ["cardsLeft", "hsOwnHero"], ["cardsRight", "hsHeroPower"],
  ],
};
// Eigene Sprungstellen (E99, Ereignis hint … spots: Namen wie in der Option pad_spots): Sie ersetzen die
// eingebaute Stelle derselben Taste. Gemerkt werden sie mit „Create halten + Taste“; das sagt der Text.
const SPOT_KEYS = [["r1", "cardsR1"], ["l1", "cardsL1"], ["up", "cardsUp"], ["down", "cardsDown"],
  ["left", "cardsLeft"], ["right", "cardsRight"]];

function cardsSpots(p) {
  const own = new Set(Array.isArray(p.spots) ? p.spots : []);
  const base = new Map(CARDS_SPOTS[p.layout] || []);
  return SPOT_KEYS.filter(([name, key]) => own.has(name) || base.has(key))
    .map(([name, key]) => [key, own.has(name) ? "cardsSpotOwnDo" : base.get(key)]);
}

const HINTS = {
  "mouse-mode": () => ({
    icon: "mouse", title: S.hintMouseTitle, text: S.hintMouseText,
    items: MOUSE_KEYS.map(([key, does]) => [S[key], S[does]]),
  }),
  "opti-mode": () => ({
    icon: "gamepad", title: S.hintOptiTitle, text: S.hintOptiText,
    items: OPTI_KEYS.map(([key, does]) => [S[key], S[does]]),
  }),
  "cards-mode": (p) => {
    const spots = cardsSpots(p);
    return {
      icon: "gamepad", title: S.hintCardsTitle,
      text: (spots.length ? S.hintCardsTextSpots : S.hintCardsText) + " " + S.hintCardsSpotSet,
      items: [...spots, ...CARDS_KEYS].map(([key, does]) => [S[key], S[does]]),
    };
  },
};

let hintEl = null;
let hintTimer = null;

function dropHint(animate) {
  clearTimeout(hintTimer);
  hintTimer = null;
  const old = hintEl;
  hintEl = null;
  if (!old) return;
  if (!animate) { old.remove(); return; }
  old.classList.add("is-leave");
  after("--t-toast-out", () => old.remove());
}

// p: {id, seconds}; host: Element des Overlays. Unbekannte Hinweise übergeht die Seite.
export function showHint(p, host) {
  const make = p && HINTS[p.id];
  if (!make || !host) return false;
  dropHint(false);
  const def = make(p);
  const half = Math.ceil(def.items.length / 2);
  const column = (items) => h("div.hintcard-col", items.map(([key, does]) => h("div.hintcard-row",
    h("span.hintcard-key", { text: key }), h("span.hintcard-do", { text: does }))));
  hintEl = h("div.hintcard", { "data-bounds": true, dataset: { hint: p.id } },
    h("div.hintcard-head", icon(def.icon),
      h("div.hintcard-titles", h("div.hintcard-title", { text: def.title }), h("div.hintcard-text", { text: def.text }))),
    h("div.hintcard-body", column(def.items.slice(0, half)), column(def.items.slice(half))));
  host.append(hintEl);
  enter(hintEl);
  const seconds = Number(p.seconds) > 0 ? Math.min(60, Number(p.seconds)) : 8;
  hintTimer = setTimeout(() => dropHint(true), seconds * 1000);
  return true;
}

export function hideHint() { dropHint(true); }
