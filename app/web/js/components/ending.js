// Vollbild-Hinweis, solange die Konsole ausschaltet, neu startet, zum Desktop wechselt oder an
// Steam übergibt. Vorher blieb das Home normal bedienbar, bis die Apps zu waren und der Rechner
// ausging: ein Druck auf das scheinbar Richtige blieb ohne Antwort. Die Tafel sperrt die Eingabe
// (kein Fokus, jede Taste wird geschluckt) und nennt die Apps, die gerade geschlossen werden.
// Beide Rollen benutzen sie (Ein/Aus-Menü im Home und im Control Center). Schließen tut nur, wer
// sie geöffnet hat: wenn power.do scheitert, abgelehnt wird oder im Prüfmodus zurückkommt.
import { h, icon } from "../core/dom.js";
import { store } from "../core/store.js";
import { S, fmt } from "../core/strings.js";
import { openLayer } from "./layer.js";

const TITLES = { off: S.endOff, reboot: S.endReboot, "restart-session": S.endRestart, desktop: S.endDesktop, steam: S.endSteam };
const ICONS = { off: "power", reboot: "restart", "restart-session": "restart-screen", desktop: "desktop", steam: "steam" };

let shown = 0;

// Steht die Tafel gerade? (Das Home springt dann nicht mehr zur App zurück, die eben vorn war)
export function endingShown() {
  return shown > 0;
}

// Aktionen, für die es die Tafel gibt (der Ruhemodus beendet nichts und kehrt zurück)
export function hasEnding(action) {
  return Object.prototype.hasOwnProperty.call(TITLES, action);
}

// note: zweite Zeile (bei Steam der Rückweg zur Konsole)
export function openEnding(action, { note = "" } = {}) {
  const apps = h("div.ending-apps");
  const panel = h("div.ending", { "data-bounds": true },
    icon(ICONS[action] || "power"),
    h("div.ending-title", { text: TITLES[action] || S.endOff }),
    note ? h("div.ending-note", { text: note }) : null,
    apps);
  const paint = () => {
    const titles = (store.state.running || []).filter((r) => r && r.title).map((r) => "„" + r.title + "“");
    apps.textContent = titles.length ? fmt(S.endClosing, { titles: titles.join(", ") }) : "";
    apps.hidden = !titles.length;
  };
  paint();
  shown += 1;
  const unsub = store.subscribe(["running"], paint);
  const layer = openLayer({
    panel, cls: "is-ending", name: "ending", sticky: true,
    onPad: () => true,
    onBack: () => {},
    onClose: () => { shown -= 1; unsub(); },
  });
  return { close: () => layer.close(), get closed() { return layer.closed; } };
}
