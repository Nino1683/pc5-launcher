// Mitteilung oben rechts (07 Abschnitt 6.4): gleitet von rechts herein, steht ihre Zeit, gleitet
// hinaus. Die Standzeit kommt mit der Mitteilung (toast.seconds = Einstellung „Anzeigedauer“).
//
// Kommen mehrere kurz hintereinander, stehen sie untereinander (höchstens drei), jede für ihre
// eigene Zeit ab dem Eintreffen. Eine Warteschlange wäre falsch: Der Kern hält das Overlay nur so
// lange sichtbar, wie die jüngste Mitteilung steht (shell.on_toast); eine wartende würde nie gesehen.
import { h, icon, after, enter, cssMs } from "../core/dom.js";
import { sound } from "../core/sound.js";
import { artBox } from "./art.js";

const MAX_VISIBLE = 3;
let host = null;
const showing = [];              // {el, timer, id}

function hostEl() {
  if (!host || !host.isConnected) {
    host = h("div.toasts");
    document.body.append(host);
  }
  return host;
}

// Unbekannte Symbolnamen (der Kern darf neue erfinden) fallen auf die Glocke zurück
function iconName(name) {
  return name && document.getElementById(name) ? name : "bell";
}

function leave(item) {
  const at = showing.indexOf(item);
  if (at < 0) return;
  showing.splice(at, 1);
  clearTimeout(item.timer);
  item.el.classList.add("is-leave");
  after("--t-toast-out", () => item.el.remove());
}

function stayMs(item) {
  const seconds = Number(item.seconds);
  if (Number.isFinite(seconds) && seconds > 0) return Math.min(60, seconds) * 1000;
  return item.stay || cssMs("--t-toast-stay");
}

// item: {id?, title?, body, icon?, image?, entry?, sound?, seconds? (Sekunden), stay? (ms)}
export function toast(item) {
  if (typeof item === "string") item = { body: item };
  if (!item || (!item.body && !item.title)) return;
  // Dieselbe Mitteilung noch einmal (das Ereignis geht an beide Seiten, ein Baustein darf es
  // weiterreichen): nicht doppelt zeigen
  if (item.id !== undefined && item.id !== null && item.id !== "" && showing.some((s) => s.id === String(item.id))) return;
  while (showing.length >= MAX_VISIBLE) leave(showing[0]);
  const iconBox = h("div.toast-icon");
  if (item.entry) iconBox.append(artBox(item.entry, { withTitle: false }));
  else if (item.image) iconBox.append(h("img", { src: item.image, alt: "" }));
  else iconBox.append(icon(iconName(item.icon)));
  const el = h("div.toast", { "data-bounds": true, dataset: { toastId: item.id === undefined || item.id === null ? "" : String(item.id) } },
    iconBox,
    h("div.toast-body",
      item.title ? h("div.toast-title", { text: item.title }) : null,
      h("div.toast-text", { text: item.body || "" })));
  hostEl().append(el);
  enter(el);
  const mine = { el, timer: null, id: el.dataset.toastId || null };
  showing.push(mine);
  if (item.sound !== false) sound.play(typeof item.sound === "string" && item.sound ? item.sound : "toast");
  mine.timer = setTimeout(() => leave(mine), stayMs(item));
}

