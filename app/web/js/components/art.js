// Bild eines Bibliothekseintrags: Kachel (art.tile, quadratisch) oder Cover (art.cover, Hochformat),
// sonst Systemkachel mit Symbol (Apps) oder ein Platzhalter mit Farbverlauf aus dem Titel.
// Bilder wechseln ohne Flackern: Das neue wird erst eingesetzt, wenn es dekodiert ist; bis dahin
// bleibt stehen, was da war (Platzhalter oder altes Bild).
import { h, icon, hueOf, decoded } from "../core/dom.js";
import { S } from "../core/strings.js";
import { store } from "../core/store.js";

const APP_ICONS = {
  "app:steam": "steam", "app:youtube": "tv", "app:kodi": "film", "app:browser": "globe", "app:desktop": "desktop",
  "app:emudeck": "emulator", "app:claude": "terminal",
};
const SOURCE_ICONS = { switch: "emulator", wiiu: "emulator", steam: "steam" };

export function entryIcon(entry) {
  if (!entry) return "gamepad";
  if (entry.icon) return entry.icon;
  return APP_ICONS[entry.id] || (entry.kind === "app" ? "apps" : SOURCE_ICONS[entry.source] || "gamepad");
}

// Adresse für eine Bildart. kind "tile": Kachelreihe, Mini-Bilder; "cover": Raster der Bibliothek.
// Fehlt die passende Fassung, hilft die andere aus (shape sagt, welche es wurde).
function pick(entry, kind) {
  const art = (entry && entry.art) || {};
  if (kind === "cover") return art.cover ? { src: art.cover, shape: "portrait" } : art.tile ? { src: art.tile, shape: "square" } : null;
  return art.tile ? { src: art.tile, shape: "square" } : art.cover ? { src: art.cover, shape: "portrait" } : null;
}

// Trägt das Bild, das für diese Fläche gezeigt wird, Titel und Marke schon selbst? Das gilt für
// die Platzhalter, die der Kern erzeugt (art.generated, docs/RPC.md), und für unseren eigenen
// Platzhalter ohne Bild (Titel als Schrift). Dann legt die Seite weder Namen noch Marke darüber.
export function titledArt(entry, kind = "tile") {
  if (!entry || entry.system) return false;
  const found = pick(entry, kind);
  if (!found) return true;
  const generated = Array.isArray(entry.art && entry.art.generated) ? entry.art.generated : [];
  return generated.includes(found.shape === "portrait" ? "cover" : "tile");
}

function fallback(box, entry, withTitle) {
  box.replaceChildren();
  box.classList.remove("is-ph", "is-sys", "has-img", "is-square", "is-wait");
  const art = (entry && entry.art) || {};
  if (entry && (entry.kind === "app" || entry.system)) {
    box.classList.add("is-sys");
    if (art.icon) {
      const img = h("img.art-appicon", { alt: "", decoding: "async" });
      img.addEventListener("error", () => { img.replaceWith(icon(entryIcon(entry))); }, { once: true });
      img.src = art.icon;
      box.append(img);
    } else {
      box.append(icon(entryIcon(entry)));
    }
    return;
  }
  box.classList.add("is-ph");
  box.style.setProperty("--hue", hueOf(entry ? entry.title : ""));
  if (withTitle && entry) box.append(h("div.art-title", { text: entry.title }));
}

// Setzt das Bild ein, sobald es dekodiert ist. Ein späterer Aufruf für dieselbe Fläche gewinnt.
async function show(box, found) {
  const img = h("img", { alt: "", decoding: "async", draggable: "false" });
  img.src = found.src;
  box.dataset.src = found.src;
  box.dataset.pending = found.src;
  const ok = await decoded(img);
  if (box.dataset.pending !== found.src) return;       // inzwischen ist ein anderes Bild verlangt
  delete box.dataset.pending;
  if (!ok) {
    // Bild fehlt oder ist kaputt: Platzhalter, sofern nicht schon ein älteres Bild steht
    if (!box.classList.contains("has-img")) { delete box.dataset.src; fallback(box, box.artEntry, box.artOpts.withTitle); }
    return;
  }
  place(box, img, found);
}

function place(box, img, found) {
  box.classList.remove("is-ph", "is-sys", "is-wait");
  box.classList.add("has-img");
  // Quadratische Kachel in einer Fläche fürs Hochformat: Bild oben, der Titel füllt den Rest
  const square = box.artOpts.kind === "cover" && found.shape === "square" && box.artEntry && !box.artEntry.system;
  box.classList.toggle("is-square", !!square);
  // Trägt die Kachel ihren Titel schon im Bild (Platzhalter des Kerns), steht er nicht noch einmal darunter
  const cap = square && !titledArt(box.artEntry, "cover");
  box.replaceChildren(...[img, cap ? h("div.art-cap", { text: box.artEntry.title }) : null].filter(Boolean));
}

// Nur laden, nicht dekodieren: für Flächen knapp außerhalb des Sichtbereichs. Dekodiert wird
// ein so eingesetztes Bild erst, wenn es gezeichnet wird.
function attach(box, found) {
  const img = h("img", { alt: "", decoding: "async", draggable: "false" });
  box.dataset.src = found.src;
  box.dataset.pending = found.src;
  img.addEventListener("load", () => {
    if (box.dataset.pending !== found.src) return;
    delete box.dataset.pending;
    place(box, img, found);
  }, { once: true });
  img.addEventListener("error", () => {
    if (box.dataset.pending !== found.src) return;
    delete box.dataset.pending;
    if (!box.classList.contains("has-img")) { delete box.dataset.src; fallback(box, box.artEntry, box.artOpts.withTitle); }
  }, { once: true });
  img.src = found.src;
}

function setDim(box, entry) {
  box.classList.toggle("is-dim", !!entry && (entry.available === false || entry.installed === false));
}

// opts: withTitle (Titel auf dem Platzhalter), kind ("tile" | "cover"), lazy (Bild erst mit loadArt laden)
export function artBox(entry, opts = {}) {
  const box = h("div.art");
  box.artOpts = { withTitle: opts.withTitle !== false, kind: opts.kind || "tile" };
  box.artEntry = entry;
  setDim(box, entry);
  const found = pick(entry, box.artOpts.kind);
  if (found) {
    // Solange das Bild lädt, bleibt die Fläche ruhig dunkel (kein Platzhalter, der gleich wieder weicht)
    box.artShape = found.shape;
    box.classList.add("is-wait");
    if (opts.lazy) box.dataset.lazy = found.src; else show(box, found);
  } else {
    fallback(box, entry, box.artOpts.withTitle);
  }
  return box;
}

// Träge geladene Fläche: jetzt laden (sie ist in die Nähe des Sichtbereichs gerückt).
// decode false: nur laden (knapp außerhalb des Sichtbereichs), true: laden und dekodieren.
export function loadArt(box, { decode = true } = {}) {
  const src = box && box.dataset.lazy;
  if (!src) return;
  delete box.dataset.lazy;
  if (decode) show(box, { src, shape: box.artShape }); else attach(box, { src, shape: box.artShape });
}

// Gleicht eine vorhandene Fläche mit dem neuen Stand des Eintrags ab (lib.changed, reason "art"):
// nur wenn sich die Adresse geändert hat, wird getauscht.
export function updateArt(box, entry) {
  if (!box || !box.artOpts) return;
  box.artEntry = entry;
  setDim(box, entry);
  const found = pick(entry, box.artOpts.kind);
  const now = box.dataset.lazy || box.dataset.src || "";
  if ((found ? found.src : "") === now) return;
  if (!found) {
    delete box.dataset.src; delete box.dataset.lazy; delete box.dataset.pending;
    fallback(box, entry, box.artOpts.withTitle);
    return;
  }
  box.artShape = found.shape;
  if (box.dataset.lazy) box.dataset.lazy = found.src; else show(box, found);
}

export function badge(text, cls) {
  return text ? h("span.badge" + (cls ? "." + cls : ""), { text }) : null;
}

// Zustand eines Eintrags für Marken: läuft, nicht installiert, nicht verfügbar
export function stateOf(entry) {
  if (!entry || entry.system) return "";
  if (entry.installed === false) return "not-installed";
  if (entry.available === false && !(entry.tags || []).includes("needs-exe")) return "unavailable";
  if (entry.running || store.isRunning(entry.id)) return "running";
  return "";
}

const STATE_TEXT = { "not-installed": S.notInstalled, unavailable: S.unavailable, running: S.markRunning };

// Kleine Marken neben dem Titel: FSR 4, VR, Zustand. Die Marke der Quelle baut badge().
export function marks(entry, { withState = true } = {}) {
  if (!entry || entry.system) return [];
  const tags = entry.tags || [];
  const out = [];
  if (tags.includes("fsr4") || tags.includes("fsr4-upgrade")) out.push(h("span.mark", { text: S.markFsr }));
  if (tags.includes("vr")) out.push(h("span.mark", { text: S.markVr }));
  const state = withState ? stateOf(entry) : "";
  if (state) out.push(h("span.mark.is-" + state, { text: STATE_TEXT[state] }));
  return out;
}
