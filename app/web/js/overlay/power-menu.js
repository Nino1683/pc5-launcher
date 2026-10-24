// Ein/Aus-Menü (07 Abschnitt 3.2 und 11.4), EIN Baustein für beide Stellen: über dem Symbol im
// Control Center (placement "bottom") und unter dem Symbol in der Kopfzeile des Home ("top").
// Unter den Einträgen steht die Erklärung zum Eintrag im Fokus (items[].note des Kerns).
// Die Einträge kommen aus power.menu. Rückfragen nach Bauplan E44, nie doppelt:
//   Eintrag mit confirm    die Seite fragt mit genau diesem Satz (er nennt laufende Spiele,
//                          Installationen …) und ruft power.do {confirmed: true}
//   Eintrag ohne confirm   power.do {confirmed: false}: Der Kern fragt selbst, falls inzwischen
//                          doch etwas dagegen spricht
// Nach der Wahl holt die Seite power.menu frisch: Beginnt zwischen Öffnen des Menüs und Wahl eine
// Installation, nennt die Rückfrage sie (sonst ginge sie mit confirmed: true ungefragt verloren).
// Beendet die Aktion Apps, steht bis zum Ende ein Hinweis über allem (components/ending.js); läuft
// dabei eine App, übernimmt das Home (handToHome, Ereignis ui.open „power-ending“).
// E107: Der letzte Eintrag „Ruhemodus-Timer“ führt nichts aus: Kreuz öffnet eine Auswahl
// (components/picker.js, aktueller Wert markiert) und stellt den Timer über power.timer.
import { h, icon, cssMs } from "../core/dom.js";
import { call, role } from "../core/bridge.js";
import { store } from "../core/store.js";
import { S, fmt } from "../core/strings.js";
import { confirm } from "../components/dialog.js";
import { openLayer } from "../components/layer.js";
import { openEnding, hasEnding } from "../components/ending.js";
import { openPicker } from "../components/picker.js";

// Neustart des Rechners und Neustart nur der Oberfläche tragen verschiedene Symbole
export const POWER_ICONS = { suspend: "moon", off: "power", reboot: "restart", desktop: "desktop", steam: "steam", "restart-session": "restart-screen", "sleep-timer": "clock" };

// E107: Auswahl des Ruhemodus-Timers; Minuten wie power.TIMER_CHOICES (der Kern schickt sie als choices mit)
export const TIMER_ID = "sleep-timer";
const TIMER_CHOICES = [0, 15, 30, 60, 90, 120];

// Optionen der Auswahl: „Aus“, „In 15 Minuten“ …; am laufenden Wert steht die Restzeit (note des Kerns)
export function timerOptions(item) {
  const timer = item && item.timer;
  const current = timer && timer.until ? Number(timer.minutes) || 0 : 0;
  const choices = Array.isArray(item && item.choices) && item.choices.length ? item.choices : TIMER_CHOICES;
  const options = choices.map((m) => ({ value: m, label: m ? fmt(S.timerIn, { n: m }) : S.timerOff,
    note: m && m === current ? String(item.note || "") : "" }));
  return { options, value: current };
}

// Kreuz auf „Ruhemodus-Timer“: true, wenn ein Wert gestellt wurde
async function pickTimer(item, anchor, fail) {
  const { options, value } = timerOptions(item);
  const minutes = await openPicker({ options, value, anchor });
  if (minutes === undefined) return false;
  try {
    await call("power.timer", { minutes });
    return true;
  } catch (e) {
    if (fail) fail(e);
    return false;
  }
}

// Zeigt das Menü; liefert die gewählte id oder null
function choose(menu, anchor, placement, fail) {
  return new Promise((resolve) => {
    let result = null;
    const note = h("div.power-note");
    const list = h("div.power-items");
    const style = { right: Math.max(0, innerWidth - anchor.right) + "px" };
    const panel = h("div.menu.menu-power" + (placement === "top" ? ".is-top" : ""), { "data-bounds": true, style }, list, note);
    const items = (menu.items || []).filter((it) => it && it.id);
    for (const item of items) {
      const off = item.enabled === false;
      const node = h("div.menu-item" + (off ? ".is-off" : ""), { "data-focus": !off, dataset: { id: item.id } },
        icon(POWER_ICONS[item.id] || "power"),
        h("span", { text: item.label || item.id }),
        // gesperrt: der Grund steht gleich daneben (die Zeile bekommt nie den Fokus)
        off && item.note ? h("small", { text: item.note }) : null);
      node.addEventListener("press", () => {
        if (item.id === TIMER_ID) {
          // E107: Auswahl über dem Menü; erst nach dem Stellen schließt es (Kreis: zurück ins Menü)
          pickTimer(item, node, fail).then((done) => { if (done && !layer.closed) { result = item.id; layer.close(); } });
          return;
        }
        result = item.id;
        layer.close();
      });
      list.append(node);
    }
    // Erklärung zum Eintrag im Fokus; sonst, was gerade noch arbeitet (power.menu.busy)
    const paint = (id) => {
      const item = items.find((it) => it.id === id);
      note.textContent = (item && item.enabled !== false && item.note) || (menu.busy && menu.busy[0]) || menu.warning || "";
      note.classList.toggle("is-empty", !note.textContent);
    };
    list.addEventListener("nav-focus", (e) => paint(e.target.closest(".menu-item").dataset.id));
    const layer = openLayer({
      panel, name: "menu", cls: placement === "top" ? "" : "is-clear",
      onPad: (b, ev) => {
        if (b === "options" && !ev.repeat) { layer.close(); return true; }
        return false;
      },
      onClose: () => resolve(result),
    });
    const first = list.querySelector(".menu-item.is-focus");
    paint(first ? first.dataset.id : null);
  });
}

async function freshItem(id, old) {
  try {
    const menu = (await call("power.menu")) || {};
    return (menu.items || []).find((it) => it.id === id) || old;
  } catch (e) {
    return old;
  }
}

// Führt eine Aktion aus: Rückfrage (Satz des Kerns), Hinweis bis zum Ende, power.do.
// opts: fail(e); ask(item) -> {title, text, ok} | null: eigene Rückfrage, wenn der Kern keinen
// Satz hat (z. B. Steam-Übergabe aus dem Home, Desktop-Modus aus dem Hub). Liefert true, wenn die
// Aktion ausgeführt wurde.
export async function runPowerAction(id, item, { fail, ask } = {}) {
  item = await freshItem(id, item || { id });
  let confirmed = false;
  const question = item.confirm || (ask ? ask(item) : null);
  if (question) {
    const yes = await confirm({ title: question.title || item.label, text: question.text || "", ok: question.ok || item.label, cancel: question.cancel || S.cancel });
    if (!yes) return false;
    confirmed = true;
  }
  // Aktionen, die Apps beenden (power.menu endsApps) bzw. die Konsole verlassen: Hinweis über allem
  const ends = hasEnding(id) && item.endsApps !== false;
  if (ends && await handToHome(id, confirmed)) return true;
  const note = id === "steam" ? (item.note || S.steamBack) : "";
  return execute(id, confirmed, ends ? openEnding(id, { note }) : null, fail);
}

async function execute(id, confirmed, ending, fail) {
  try {
    const result = await call("power.do", { id, confirmed });
    // {ok: false, cancelled: true}: Die Rückfrage des Kerns wurde abgelehnt
    if (result && result.ok === false) { if (ending) ending.close(); return false; }
    // Ausgeführt. Im echten Betrieb geht der Rechner jetzt aus bzw. das Home endet; bleibt alles
    // stehen (Prüfmodus: nur writes.log), gibt der Hinweis nach kurzer Zeit wieder frei.
    if (ending) await new Promise((r) => setTimeout(r, cssMs("--t-ending-hold")));
    if (ending) ending.close();
    return true;
  } catch (e) {
    if (ending) ending.close();
    if (fail) fail(e);
    return false;
  }
}

// Control Center über einer App: Schließt der Kern die App, nimmt er die Ansicht mit (sie gehört
// zur App); der Hinweis verschwand, und das Home war bis zum Ende wieder bedienbar (Rundgang,
// Foto 61). Darum übernimmt das Home: ui.open holt es nach vorn, das Overlay schließt, dort steht
// der Hinweis bis zum Ende und das Home ruft power.do (finishInHome). Kennt der Kern ui.open nicht
// (BadRequest, unbekannte Methode), bleibt alles im Overlay wie bisher.
async function handToHome(id, confirmed) {
  if (role !== "overlay" || !(store.state.running || []).length) return false;
  try {
    await call("ui.open", { screen: "power-ending", params: { id, confirmed } });
    return true;
  } catch (e) {
    return false;
  }
}

// Home-Seite, Ereignis ui.open {screen: "power-ending", params: {id, confirmed}} (siehe handToHome)
export function finishInHome(params, { fail } = {}) {
  const id = params && params.id;
  if (!hasEnding(id)) return Promise.resolve(false);
  return execute(id, params.confirmed === true, openEnding(id, { note: id === "steam" ? S.steamBack : "" }), fail);
}

// anchor: Lage des Ein/Aus-Symbols; placement "bottom" (Control Center) oder "top" (Home).
// askSteam: „Steam Big Picture öffnen“ immer kurz bestätigen lassen (mit dem Rückweg im Text).
// Liefert true, wenn eine Aktion ausgelöst wurde (das Control Center schließt dann); Fehler meldet fail(e).
export async function openPowerMenu({ anchor, fail, placement = "bottom", askSteam = false }) {
  let menu;
  try {
    menu = (await call("power.menu")) || {};
  } catch (e) {
    fail(e);
    return false;
  }
  if (!(menu.items || []).length) { fail({ message: S.powerMenuFailed }); return false; }
  const id = await choose(menu, anchor, placement, fail);
  if (!id) return false;
  if (id === TIMER_ID) return true;           // E107: schon gestellt (power.timer), nichts auszuführen
  const item = (menu.items || []).find((it) => it.id === id) || { id };
  const ask = askSteam ? (it) => (it.id === "steam" ? { title: S.steamTitle, text: S.steamTextApp, ok: S.steamOk } : null) : null;
  return runPowerAction(id, item, { fail, ask });
}

// Wechsel in den Desktop-Modus der ganzen Sitzung (Nebenknopf im Hub der App „Desktop“): immer mit
// Rückfrage (07 11.7), mit dem Satz des Kerns, wenn er einen hat
export function switchToDesktopMode({ fail } = {}) {
  return runPowerAction("desktop", null, {
    fail,
    ask: (it) => ({ title: fmt(S.powerConfirmTitle, { label: it.label || S.desktopModeLabel }), text: S.desktopModeText, ok: it.label || S.desktopModeLabel }),
  });
}
