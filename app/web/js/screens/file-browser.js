// Dateiauswahl (07 Abschnitt 11.6, Schritt 1) und das gemeinsame Gerüst der geführten Abläufe
// (Installer, Papierkorb, Bilder): Überschrift oben links, Unterzeile, Liste ab x=172,
// Tastenhinweise unten rechts.
//
// Dateiauswahl: mode "archive" (install.browse: Ordner und Archive mit Größe, mehrteilige als ein
// Eintrag, dazu Installationsprogramme setup.exe/.msi, E114) oder "image" (lib.artBrowse: Ordner und Bilder, je Bild ein Vorschaubild über
// lib.artPreview, beginnt im Download-Ordner). Neueste Dateien zuerst, Ordner davor. Die Unterzeile
// nennt den Weg ab dem Ort („Downloads › Archive“), die Zeilen Größe und Datum.
// Kreis geht einen Ordner hoch, an den Orten zurück. params.onPick(eintrag) bekommt die Wahl.
import { h, icon, sizeText } from "../core/dom.js";
import { call } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { router } from "../core/router.js";
import { sound } from "../core/sound.js";
import { S, fmt, whenText } from "../core/strings.js";
import { createRowList } from "../components/rows.js";

// Gerüst eines Ablauf-Bildschirms. handlers gehen an die Zeilenliste (components/rows.js).
export function createListScreen({ title = "", sub = "", hints = [], handlers = {}, cls = "", side = null } = {}) {
  // Auch eine Liste aus lauter gesperrten Zeilen (Fehlermeldung) trägt einen Fokus
  const list = createRowList({ alwaysFocus: true, ...handlers });
  const titleEl = h("h1.flow-title.ell", { text: title });
  const subEl = h("div.flow-sub.ell", { text: sub, hidden: !sub });
  const hintItems = (items) => items.map(([glyph, text]) => h("div.hint", icon(glyph), h("span", { text })));
  const hintsEl = h("div.hints", hintItems(hints));
  list.scrollbar.classList.add("flow-scroll");
  const el = h("div.flow" + (cls ? "." + cls : ""), titleEl, subEl, h("div.flow-list", list.el), list.scrollbar, side, hintsEl);
  return {
    el, list,
    setTitle(text) { titleEl.textContent = text || ""; },
    setSub(text) { subEl.textContent = text || ""; subEl.hidden = !text; },
    setHints(items) { hintsEl.replaceChildren(...hintItems(items)); },
  };
}

// Pfad als Weg ab seinem Ort („Downloads › Archive“) statt als absoluter Pfad. roots: die Orte der
// Dateiauswahl ({name, path}, aus der Liste ohne path). Liegt er unter keinem, bleibt der Pfad.
function placeText(path, roots) {
  if (!path) return S.fbRoots;
  const hit = (roots || []).filter((r) => r.path && (path === r.path || path.startsWith(r.path.replace(/\/+$/, "") + "/")))
    .sort((a, b) => b.path.length - a.path.length)[0];
  if (!hit) return path;
  const rest = path.slice(hit.path.replace(/\/+$/, "").length).split("/").filter(Boolean);
  return [hit.name, ...rest].join(S.fbRootsSep);
}

function byNewest(a, b) {
  if ((a.type === "dir") !== (b.type === "dir")) return a.type === "dir" ? -1 : 1;
  if (a.type === "dir") return a.name.localeCompare(b.name, "de");
  return (b.mtime || 0) - (a.mtime || 0) || a.name.localeCompare(b.name, "de");
}

function create(params = {}) {
  const image = params.mode === "image";
  const preview = image ? h("div.flow-preview", { hidden: true }, h("img", { alt: "" })) : null;
  const screen = createListScreen({
    title: params.title || (image ? S.fbTitleImage : S.fbTitleArchive),
    hints: [["btn-cross", S.select], ["btn-circle", S.hintUp]],
    cls: image ? "has-preview" : "",
    side: preview,
    handlers: {
      emptyText: image ? S.fbEmptyImage : S.fbEmptyArchive,
      onPress: (row) => { open(row); return true; },
    },
  });
  const { list } = screen;
  let current = { path: null, parent: null };
  let serial = 0;
  const thumbs = new Map();       // Pfad -> Adresse des Vorschaubilds
  let previewTimer = null;
  let roots = null;               // Orte der Auswahl [{name, path}] für die Unterzeile

  function rowOf(entry) {
    if (entry.type === "dir") {
      return { id: entry.path, type: "page", label: entry.name, icon: entry.kind === "folder" || !current.path ? "storage" : "folder", entry };
    }
    if (image) {
      const url = thumbs.get(entry.path);
      return { id: entry.path, type: "action", label: entry.name, value: whenText(entry.mtime) || "", art: url ? { id: entry.path, kind: "game", title: entry.name, art: { tile: url } } : null, icon: url ? null : "screen", entry };
    }
    // Größe und Datum (07 11.6)
    const parts = entry.parts && entry.parts > 1 ? fmt(S.fbParts, { n: entry.parts }) : "";
    return { id: entry.path, type: "action", label: entry.name, value: [sizeText(entry.size), parts, whenText(entry.mtime)].filter(Boolean).join(" · "), icon: "archive", entry };
  }

  // Die Orte kennt die Liste ohne path; ist die Auswahl woanders eingestiegen, einmal nachfragen
  async function learnRoots() {
    if (roots) return;
    try {
      const result = image ? await call("lib.artBrowse", { roots: true }) : await call("install.browse", {});
      roots = ((result && result.entries) || []).filter((e) => e && e.type === "dir" && e.path).map((e) => ({ name: e.name, path: e.path }));
    } catch (e) { roots = []; }
    screen.setSub(placeText(current.path, roots));
  }

  function paint(result) {
    const entries = (result.entries || []).filter((e) => e && typeof e === "object" && (e.type === "dir" || (image ? e.type === "image" : e.type === "archive" || e.type === "installer")));
    entries.sort(byNewest);
    const rows = entries.map(rowOf);
    if (result.ok === false) rows.unshift({ id: "_err", type: "info", label: result.message || S.fbFailed, enabled: false });
    if (result.truncated) rows.push({ id: "_more", type: "info", label: fmt(S.fbTruncated, { n: entries.length, total: result.total }), enabled: false });
    list.setRows(rows);
    return entries;
  }

  async function go(path, keepId) {
    const mine = ++serial;
    if (list.setLoading) list.setLoading();
    let result;
    try {
      result = image
        ? await call("lib.artBrowse", path ? { path } : (params.roots || current.path !== null ? { roots: true } : {}))
        : await call("install.browse", path ? { path } : {});
    } catch (e) {
      result = { ok: false, message: (e && e.message) || S.fbFailed, entries: [] };
    }
    if (mine !== serial) return;
    current = { path: result.path || null, parent: result.parent || null, name: result.name || "" };
    if (!current.path && result.ok !== false) roots = (result.entries || []).filter((e) => e && e.type === "dir" && e.path).map((e) => ({ name: e.name, path: e.path }));
    screen.setSub(placeText(current.path, roots));
    if (current.path && !roots) learnRoots();
    // An den Orten führt Kreis zurück, darunter einen Ordner hoch
    screen.setHints([["btn-cross", S.select], ["btn-circle", current.path ? S.hintUp : S.hintBack]]);
    const entries = paint(result);
    nav.ensure();
    if (keepId) { const node = list.nodeOf ? list.nodeOf(keepId) : null; if (node) nav.focus(node); }
    if (image) loadThumbs(entries.filter((e) => e.type === "image"), mine);
  }

  // Vorschaubilder nacheinander (jedes ist ein Auftrag im Arbeits-Thread); die Liste wird in
  // kleinen Schüben neu gezeichnet, der Fokus bleibt auf seiner Zeile
  async function loadThumbs(images, mine) {
    let fresh = 0;
    for (const entry of images) {
      if (mine !== serial) return;
      if (thumbs.has(entry.path)) continue;
      try {
        const result = await call("lib.artPreview", { path: entry.path });
        if (result && result.url) { thumbs.set(entry.path, result.url); fresh += 1; }
      } catch (e) { thumbs.set(entry.path, ""); }
      if (fresh >= 4) { fresh = 0; repaint(mine); }
    }
    if (fresh) repaint(mine);
  }
  function repaint(mine) {
    if (mine !== serial) return;
    list.setRows(list.rows.map((row) => (row.entry && row.entry.type === "image" ? rowOf(row.entry) : row)));
    showPreview();
  }

  // Großes Vorschaubild der fokussierten Bilddatei
  function showPreview() {
    if (!preview) return;
    const row = nav.current && list.el.contains(nav.current) ? list.rowOf(nav.current.closest(".row")) : null;
    const url = row && row.entry && row.entry.type === "image" ? thumbs.get(row.entry.path) : "";
    preview.hidden = !url;
    if (url && preview.firstChild.getAttribute("src") !== url) preview.firstChild.src = url;
  }
  if (preview) list.el.addEventListener("nav-focus", () => { clearTimeout(previewTimer); previewTimer = setTimeout(showPreview, 120); });

  function open(row) {
    const entry = row && row.entry;
    if (!entry) return;
    if (entry.type === "dir") { go(entry.path); return; }
    if (params.onPick) params.onPick(entry);
  }

  function up() {
    if (current.path === null) { router.back(); return; }
    sound.play("back");
    go(current.parent, current.path);
  }

  go(params.start || null);
  return {
    el: screen.el,
    onPad(button, ev) {
      if (button === "circle" && !ev.repeat) { up(); return true; }
      if ((button === "l2" || button === "r2") && list.page) { list.page(button === "r2" ? 1 : -1); return true; }
      return false;
    },
    onShow() { nav.ensure(); list.update(); },
    destroy() { clearTimeout(previewTimer); serial += 1; },
  };
}

export default { id: "file-browser", create };
