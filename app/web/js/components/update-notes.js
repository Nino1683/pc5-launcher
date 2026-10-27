// „Was ist neu“ im PC5 Launcher (E133): Changelog zum Blättern und der einmalige Hinweis nach einem Update.
//   showUpdateNotes(notes)  notes = {title, version, sections: [{label, value, note}], empty} aus settings.action
//                           („Was ist neu“, Ergebnis {notes}) bzw. update.notes (docs/RPC.md). Der Kern wählt die
//                           Sprache; Hoch/Runter und der rechte Stick blättern (scrollTextDialog, dialog.js).
//   watchWhatsNew()         sys.state.whatsnew {version, since}: Liegt das Home oben und ist zu sehen, fragt es einmal
//                           „PC5 Launcher X ist installiert“ (Später | Was ist neu). Jede Antwort schickt update.seen,
//                           ein Wechsel des Bildschirms darunter nicht (dann später noch einmal).
import { h } from "../core/dom.js";
import { call } from "../core/bridge.js";
import { router } from "../core/router.js";
import { store } from "../core/store.js";
import { S, fmt } from "../core/strings.js";
import { CLOSED, dialog, messageDialog, scrollTextDialog } from "./dialog.js";
import { layerHost } from "./layer.js";
import { introActive } from "./intro.js";

const RETRY_MS = 2000;          // so oft wird nachgesehen, ob das Home frei ist (nur solange ein Hinweis aussteht)

export function showUpdateNotes(notes) {
  const n = notes && typeof notes === "object" ? notes : {};
  const sections = (Array.isArray(n.sections) ? n.sections : []).filter((s) => s && typeof s.value === "string" && s.value);
  const body = h("div.un-body", sections.length
    ? sections.map((s) => h("div.un-part",
      h("div.un-head", { text: String(s.label || "") }),
      s.note ? h("div.un-note", { text: String(s.note) }) : null,
      h("div.un-text", { text: s.value })))
    : h("div.un-text", { text: n.empty || S.unEmpty }));
  // data-bounds: eine Tafel, die senkrecht rollt (weggerollte Abschnitte liegen gewollt außerhalb)
  return scrollTextDialog({ title: n.title || S.unTitle, box: h("div.un-box", { "data-bounds": true }, body), body });
}

// Changelog vom Kern holen und zeigen (Hinweis im Home); scheitert es, ein kurzer Satz statt nichts
async function fetchAndShow(params) {
  let notes;
  try {
    notes = await call("update.notes", params);
  } catch (e) {
    await messageDialog({ title: S.unTitle, text: (e && e.message) || S.errorGeneric });
    return;
  }
  await showUpdateNotes(notes);
}

let timer = null;
let asking = false;
const answered = new Set();      // in dieser Seite schon beantwortet (der Kern löscht whatsnew erst danach)

function pending() {
  const w = store.state.whatsnew;
  return w && typeof w === "object" && w.version && !answered.has(String(w.version)) ? w : null;
}

// Frei heißt: Home oben, zu sehen, keine Startanimation, keine Tafel darüber, das Overlay nicht bedienbar
function homeFree() {
  return !!router.top && router.top.id === "home" && store.visible && !introActive()
    && store.state.overlay !== "active" && !layerHost().querySelector(".layer:not(.is-leave)");
}

async function ask() {
  const w = pending();
  if (!w || asking) return;
  if (!homeFree()) { schedule(); return; }
  asking = true;
  const version = String(w.version);
  try {
    const choice = await dialog({
      title: fmt(S.unInstalledTitle, { version }), text: S.unInstalledText,
      buttons: [{ id: "later", label: S.unLater }, { id: "show", label: S.unShow }], initial: "show",
    });
    if (choice === CLOSED) { schedule(); return; }        // Bildschirm darunter gewechselt: später noch einmal
    answered.add(version);
    call("update.seen", { version }).catch(() => {});
    if (choice === "show") await fetchAndShow({ which: "new", since: w.since || "" });
  } finally {
    asking = false;
  }
}

function schedule() {
  clearTimeout(timer);
  if (pending()) timer = setTimeout(ask, RETRY_MS);
}

let watching = false;
export function watchWhatsNew() {
  if (watching) return;
  watching = true;
  store.subscribe(["whatsnew"], schedule);
  schedule();
}
