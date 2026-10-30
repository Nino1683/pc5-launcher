// Mediengalerie (E120) und Bildwahl für den Hintergrund des Home (E121). Bildschirme:
//   gallery        Raster der Bildschirmfotos, neueste zuerst, fünf Spalten; Filter „Alle“ und je Spiel
//                  (Titel aus dem Dateinamen, Kern gallery.parse_name). params.pick = "background": dazu die
//                  Bilder aus „Hintergründe“, Kreuz wählt das Bild (Einstellungen › Home-Bildschirm)
//   gallery-view   ein Bild im Vollbild: L1/R1 und Links/Rechts blättern, Options Optionen, Dreieck blendet
//                  die Angaben aus, Kreis zurück (der Fokus steht dann auf dem zuletzt gezeigten Bild)
// Einstieg: Kachel „Mediengalerie“ unter „Medien“ (screens/home.js) und vor den Spielen der Bibliothek
// (screens/library.js), die Zeile „Bild auswählen“ (pick). Von außen mit Spiel (z. B. aus dem Hub):
//   router.go("gallery", {game: entry.title})            Filter auf die Fotos dieses Spiels (fehlen sie: „Alle“)
//   router.go("gallery", {game: entry.title, open: key})  dazu gleich dieses Bild im Vollbild (key aus gallery.list)
// Die letzten Fotos eines Spiels liefert gallery.list {game: entry.title, limit: n} (docs/RPC.md).
//   Kreuz      Vollbild (pick: auswählen)       Options   Ansehen, Als Hintergrund verwenden, Löschen, Informationen
//   Dreieck    Filter                           L1/R1     voriger/nächster Filter       L2/R2   seitenweise
// Vorschaubilder baut der Kern erst auf Nachfrage (gallery.thumbs, je Reihe), die sichtbaren zuerst; in
// voller Größe zeigt das Vollbild die Datei selbst (couch://files, wie der Datei-Explorer). Gelöscht wird nur
// in den Papierkorb des Systems (gallery.delete), nach einer Rückfrage mit Vorgabe Abbrechen.
// Gerollt wird das Raster in ganzen Reihen über --top (nur transform, wie die Bibliothek).
import { h, hint, icon, sizeText } from "../core/dom.js";
import { call, notify } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { router } from "../core/router.js";
import { sound } from "../core/sound.js";
import { store } from "../core/store.js";
import { S, fmt } from "../core/strings.js";
import { openMenu } from "../components/menu.js";
import { clockLabel } from "../components/statusbar.js";
import { confirm, infoDialog } from "../components/dialog.js";
import { toast } from "../components/toast.js";

const COLS = 5;
const ROWS_SHOWN = 3;             // drei volle Reihen, darunter der Ansatz der vierten
const ROWS_AHEAD = 1;             // so viele Reihen davor und dahinter schon laden
const THUMB_BATCH = COLS;         // Vorschaubilder je Anfrage (eine Reihe)
const PAGE_ROWS = 3;              // L2/R2

const timeOf = (item) => new Date((Number(item.time) || 0) * 1000);
const dateLong = (item) => timeOf(item).toLocaleDateString(S.locale, { day: "numeric", month: "long", year: "numeric" });
const dateShort = (item) => timeOf(item).toLocaleDateString(S.locale, { day: "numeric", month: "short", year: "numeric" });
// Uhrzeit wie in der Statusleiste: nach Einstellungen › System › 24-Stunden-Anzeige, nicht nach der Sprache
const clock = (item) => clockLabel(timeOf(item), store.ui.clock24h !== false);

// Anzeige-Titel: Spiel aus dem Dateinamen, „Home“ als Home-Bildschirm, eigene Hintergründe mit Dateinamen
export function titleOf(item) {
  if (item.source === "wallpapers") return item.title || item.name;
  if (item.title === "Home") return S.galHome;
  return item.title || S.galOther;
}
const groupOf = (item) => (item.source === "wallpapers" ? "wallpapers" : "game:" + (item.title || ""));

// Rückfrage, dann in den Papierkorb des Systems. Liefert true, wenn das Bild weg ist.
async function trashItem(item) {
  const yes = await confirm({ title: S.galDeleteTitle, text: fmt(S.galDeleteText, { name: item.name }), ok: S.galDeleteOk });
  if (!yes) return false;
  let r = null;
  try { r = await call("gallery.delete", { path: item.path }); } catch (e) { r = { ok: false, message: e && e.message }; }
  const ok = !!(r && r.ok);
  toast({ title: ok ? S.galTitle : S.errorGeneric, body: (r && r.message) || "", icon: ok ? "trash" : "warn" });
  if (ok) sound.play("select");
  return ok;
}

async function useItem(item) {
  let r = null;
  try { r = await call("gallery.setBackground", { path: item.path }); } catch (e) { r = { ok: false, message: e && e.message }; }
  const ok = !!(r && r.ok);
  toast({ title: S.galUse, body: (r && r.message) || (ok ? "" : S.errorGeneric), icon: ok ? "check" : "warn" });
  return ok;
}

function showInfo(item, img) {
  const px = img && img.naturalWidth ? img.naturalWidth + " × " + img.naturalHeight : "";
  const items = [{ label: S.galInfoFile, value: item.name }];
  if (item.source !== "wallpapers") items.push({ label: S.galInfoGame, value: titleOf(item) });
  items.push({ label: S.galInfoDate, value: dateLong(item) + ", " + clock(item) },
    { label: S.galInfoSize, value: [sizeText(item.size), px].filter(Boolean).join(" · ") },
    { label: S.galInfoKind, value: item.source === "wallpapers" ? S.galSourceWallpapers : S.galSourceScreenshots });
  return infoDialog({ title: titleOf(item), items });
}

// Aus der Bildwahl (pick) zurück dorthin, wo sie begann (Einstellungen)
function leavePick() {
  while (router.top && (router.top.id === "gallery-view" || router.top.id === "gallery")) {
    if (!router.back()) break;
  }
}

// ================================================================ Raster
function create(params = {}) {
  const pick = params.pick === "background";
  const state = { items: [], shown: [], filter: "all", top: 0, failed: false, loaded: false, current: "", folder: "" };
  const titleEl = h("h1.flow-title.ell", { text: pick ? S.galPickTitle : S.galTitle });
  const subEl = h("div.flow-sub.ell");
  const filterLabel = h("span.btn-label");
  const filterBtn = h("div.btn.is-secondary.gal-filter", { "data-focus": true, "data-nav-down": ".gal-shot[data-focus-default]" },
    icon("sort"), filterLabel);
  // data-scroll: Der rechte Stick rollt das Raster Reihe für Reihe (core/nav.js)
  const grid = h("div.gal-grid", { "data-scroll": true });
  const view = h("div.gal-view", { "data-focus-group": true }, grid);
  const empty = h("div.gal-empty", { hidden: true });
  const hints = h("div.hints",
    hint("btn-cross", pick ? S.galHintChoose : S.galHintView), hint("btn-triangle", S.galHintFilter),
    hint(S.galKeysOptions, S.galHintOptions), hint("btn-circle", S.hintBack));
  const el = h("div.flow.gal", titleEl, subEl, filterBtn, view, empty, hints);
  const nodes = new Map();        // Kennung -> Zelle
  const thumbs = new Map();       // Kennung -> Adresse des Vorschaubilds | null (keins möglich)
  const asked = new Set();
  let serial = 0;
  let pumping = false;
  let closed = false;
  let wantKey = params.open || params.focus || null;
  // Mit Spiel geöffnet: Filter darauf und ggf. gleich das Bild (nur beim ersten Laden)
  let wantGame = typeof params.game === "string" ? params.game.trim() : "";
  let wantOpen = typeof params.open === "string" ? params.open : "";

  function filters() {
    const groups = new Map();
    for (const item of state.items) {
      const id = groupOf(item);
      const g = groups.get(id) || { id, label: id === "wallpapers" ? S.galFilterWallpapers : titleOf(item), n: 0, latest: 0 };
      g.n += 1;
      g.latest = Math.max(g.latest, Number(item.time) || 0);
      groups.set(id, g);
    }
    // Hintergründe vorn, dann die Spiele nach dem jüngsten Bild
    const list = [...groups.values()].sort((a, b) => (a.id === "wallpapers" ? -1 : b.id === "wallpapers" ? 1 : b.latest - a.latest));
    return [{ id: "all", label: S.galFilterAll, n: state.items.length }, ...list];
  }

  function cell(item) {
    const img = h("img", { alt: "", decoding: "async" });
    const shot = h("div.gal-shot", { "data-focus": true, dataset: { key: item.key } },
      h("div.gal-pic", img, icon("image", "gal-ph")), h("span.gal-mark", { hidden: true, text: S.galCurrent }));
    const node = h("div.gal-cell", shot,
      h("div.gal-cap", h("div.gal-cap-title.ell"), h("div.gal-cap-date.ell")));
    return node;
  }

  function fill(node, item) {
    node.item = item;
    const shot = node.firstChild;
    shot.item = item;
    shot.dataset.key = item.key;
    node.querySelector(".gal-cap-title").textContent = titleOf(item);
    node.querySelector(".gal-cap-date").textContent = dateShort(item);
    shot.querySelector(".gal-mark").hidden = !(pick && state.current && item.key === state.current);
  }

  function countText(n) {
    if (pick) return n === 1 ? S.galCountPickOne : fmt(S.galCountPick, { n });
    return n === 1 ? S.galCountOne : fmt(S.galCount, { n });
  }

  // keepKey: Fokus auf dieses Bild; fehlt es (gelöscht), auf den Nachbarn an derselben Stelle
  function render(keepKey, at = -1) {
    const all = filters();
    if (!all.some((f) => f.id === state.filter)) state.filter = "all";
    const f = all.find((x) => x.id === state.filter);
    state.shown = state.filter === "all" ? state.items : state.items.filter((i) => groupOf(i) === state.filter);
    const cells = state.shown.map((item) => {
      let node = nodes.get(item.key);
      if (!node) { node = cell(item); nodes.set(item.key, node); }
      fill(node, item);
      return node;
    });
    for (const [key, node] of [...nodes]) if (!cells.includes(node)) nodes.delete(key);
    const same = cells.length === grid.children.length && cells.every((c, i) => grid.children[i] === c);
    if (!same) grid.replaceChildren(...cells);
    cells.forEach((c, i) => {
      const shot = c.firstChild;
      if (i < COLS) shot.setAttribute("data-nav-up", ".gal-filter"); else shot.removeAttribute("data-nav-up");
    });
    filterLabel.textContent = f.id === "all" ? S.galFilter + ": " + f.label : f.label;
    subEl.textContent = state.failed ? S.errorGeneric : !state.loaded ? S.setLoading : f.label + " · " + countText(state.shown.length);
    empty.hidden = !state.loaded || cells.length > 0 || state.failed;
    empty.textContent = pick ? fmt(S.galEmptyPick, { folder: state.folder || S.galSourceWallpapers }) : S.galEmpty;
    for (const s of grid.querySelectorAll("[data-focus-default]")) s.removeAttribute("data-focus-default");
    let keep = cells.find((c) => c.item.key === keepKey) || null;
    if (!keep && at >= 0 && cells.length) keep = cells[Math.min(at, cells.length - 1)];
    keep = keep || cells[0] || null;
    if (keep) keep.firstChild.setAttribute("data-focus-default", "");
    return keep ? keep.firstChild : null;
  }

  async function load({ focusGrid = false } = {}) {
    const mine = ++serial;
    let r = null;
    try {
      const ask = pick ? { sources: ["wallpapers", "screenshots"] } : { sources: ["screenshots"] };
      // Titel wie im Dateinamen bereinigt der Kern (capture.clean_title); all: das Raster behält alle Bilder
      if (wantGame) Object.assign(ask, { game: wantGame, all: true });
      r = await call("gallery.list", ask);
    } catch (e) {
      notify("sys.log", { level: "warning", text: "[gallery] gallery.list: " + (e && e.message) });
    }
    if (mine !== serial || closed) return;
    const inGrid = nav.current && grid.contains(nav.current);
    const was = inGrid ? nav.current.item : null;
    const at = was ? state.shown.indexOf(was) : -1;
    state.failed = !r || r.ok === false;
    state.loaded = true;
    state.items = (r && Array.isArray(r.items)) ? r.items : [];
    state.current = (r && r.current) || "";
    // Name des Ordners „Hintergründe“ wie auf der Platte (englisch „Wallpapers“, gallery.wallpaper_folder)
    const wall = r && r.folders && typeof r.folders.wallpapers === "string" ? r.folders.wallpapers : "";
    state.folder = wall.split("/").filter(Boolean).pop() || "";
    for (const item of state.items) if (item.thumb && !thumbs.has(item.key)) thumbs.set(item.key, item.thumb);
    if (wantGame) {
      const name = String((r && r.game) || wantGame).toLowerCase();
      const hit = state.items.find((i) => i.source === "screenshots" && String(i.title || "").toLowerCase() === name);
      if (hit) state.filter = groupOf(hit);
      wantGame = "";
    }
    const keep = render(wantKey || (was && was.key), at);
    if (wantKey && keep && keep.item.key === wantKey) wantKey = null;
    if ((focusGrid || inGrid) && keep) nav.focus(keep);
    else if (!keep && inGrid) nav.focus(filterBtn);
    scrollTo(nav.current && grid.contains(nav.current) ? nav.current : keep);
    nav.ensure();
    if (wantOpen) {
      const item = state.shown.find((i) => i.key === wantOpen);
      wantOpen = "";
      if (item) openView(item);
    }
  }

  // Die Reihe des Fokus bleibt in den drei voll sichtbaren
  function scrollTo(shot) {
    const index = shot ? state.shown.indexOf(shot.item) : -1;
    if (index < 0) state.top = 0;
    else {
      const row = Math.floor(index / COLS);
      if (row < state.top) state.top = row;
      else if (row > state.top + ROWS_SHOWN - 1) state.top = row - ROWS_SHOWN + 1;
    }
    grid.style.setProperty("--top", state.top);
    reveal();
  }

  function windowItems() {
    const first = Math.max(0, state.top - ROWS_AHEAD) * COLS;
    const last = Math.min(state.shown.length, (state.top + ROWS_SHOWN + ROWS_AHEAD) * COLS);
    return state.shown.slice(first, last);
  }

  // Bilder nur um den Sichtbereich: die übrigen Zellen bleiben ohne Adresse (nichts zu dekodieren)
  function reveal() {
    for (const item of windowItems()) {
      const node = nodes.get(item.key);
      const url = thumbs.get(item.key);
      if (!node || url === undefined) continue;
      const pic = node.querySelector(".gal-pic");
      const img = pic.firstChild;
      if (url && img.getAttribute("src") !== url) img.src = url;
      pic.classList.toggle("has-img", !!url);
    }
    pump();
  }

  // Fehlende Vorschaubilder der Reihe nach holen, die sichtbaren zuerst
  async function pump() {
    if (pumping || closed) return;
    const want = windowItems().filter((i) => !thumbs.has(i.key) && !asked.has(i.key)).slice(0, THUMB_BATCH);
    if (!want.length) return;
    pumping = true;
    for (const i of want) asked.add(i.key);
    let r = null;
    try { r = await call("gallery.thumbs", { paths: want.map((i) => i.path) }); } catch (e) { r = null; }
    const got = (r && r.thumbs) || {};
    for (const i of want) thumbs.set(i.key, got[i.path] || null);
    pumping = false;
    if (!closed) reveal();
  }

  function setFilter(id, focusGrid) {
    if (id === state.filter) return;
    state.filter = id;
    state.top = 0;
    sound.play("tab");
    const keep = render(null);
    if (focusGrid && keep) nav.focus(keep);
    else if (!keep && nav.current && grid.contains(nav.current)) nav.focus(filterBtn);
    scrollTo(keep);
    nav.ensure();
  }

  function stepFilter(step) {
    const all = filters();
    const at = all.findIndex((f) => f.id === state.filter);
    const next = all[at + step];
    if (!next) { sound.play("edge"); return; }
    setFilter(next.id, nav.current && grid.contains(nav.current));
  }

  async function filterMenu() {
    const tick = (on) => (on ? "check" : "none");
    const items = filters().map((f) => ({ id: f.id, label: f.label, icon: tick(f.id === state.filter), note: String(f.n) }));
    const choice = await openMenu(items, { cls: "menu-center", title: S.galFilter, initial: state.filter });
    if (choice) setFilter(choice, nav.current && grid.contains(nav.current));
  }

  function openView(item) {
    const at = state.shown.indexOf(item);
    if (at < 0) return;
    router.go("gallery-view", {
      items: state.shown, index: at, pick,
      onLeave: (last) => { if (last) wantKey = last.key; },
      onRemoved: (gone) => drop(gone),
      onUsed: (used) => { state.current = used.key; render(used.key); },
    });
  }

  // Ein Bild ist weg (hier oder im Vollbild gelöscht): Zelle raus, Fokus auf den Nachbarn
  function drop(item) {
    const at = state.shown.indexOf(item);
    state.items = state.items.filter((i) => i.key !== item.key);
    const inGrid = nav.current && grid.contains(nav.current);
    const keep = render(null, at);
    if (inGrid && keep) nav.focus(keep);
    else if (inGrid) nav.focus(filterBtn);
    scrollTo(nav.current && grid.contains(nav.current) ? nav.current : keep);
    nav.ensure();
  }

  async function choose(item) {
    if (await useItem(item)) {
      state.current = item.key;
      render(item.key);
      if (pick) leavePick();
    }
  }

  async function options(item) {
    const items = pick
      ? [{ id: "use", label: S.galUse, icon: "check" }, { id: "view", label: S.galView, icon: "eye" },
        { id: "info", label: S.galInfo, icon: "info" }, { id: "delete", label: S.galDelete, icon: "trash" }]
      : [{ id: "view", label: S.galView, icon: "eye" }, { id: "use", label: S.galUse, icon: "home" },
        { id: "info", label: S.galInfo, icon: "info" }, { id: "delete", label: S.galDelete, icon: "trash" }];
    const choice = await openMenu(items, { cls: "menu-center", title: titleOf(item) });
    if (choice === "view") openView(item);
    else if (choice === "use") choose(item);
    else if (choice === "info") showInfo(item, null);
    else if (choice === "delete" && await trashItem(item)) drop(item);
  }

  grid.addEventListener("nav-focus", (e) => {
    for (const s of grid.querySelectorAll("[data-focus-default]")) s.removeAttribute("data-focus-default");
    e.target.setAttribute("data-focus-default", "");
    scrollTo(e.target);
  });
  grid.addEventListener("nav-move", () => { wantKey = null; });
  grid.addEventListener("press", (e) => {
    const shot = e.target.closest(".gal-shot");
    if (!shot || !shot.item) return;
    if (pick) choose(shot.item); else openView(shot.item);
  });
  filterBtn.addEventListener("press", filterMenu);

  function onPad(button, ev) {
    if (ev.repeat && button !== "l2" && button !== "r2") return false;
    const inGrid = nav.current && grid.contains(nav.current);
    switch (button) {
      case "l1": stepFilter(-1); return true;
      case "r1": stepFilter(1); return true;
      case "triangle": filterMenu(); return true;
      case "options": if (inGrid && nav.current.item) options(nav.current.item); return true;
      case "l2": case "r2": {
        if (!inGrid) return true;
        const shots = [...grid.querySelectorAll(".gal-shot")];
        const at = shots.indexOf(nav.current);
        const target = shots[Math.max(0, Math.min(shots.length - 1, at + (button === "r2" ? 1 : -1) * PAGE_ROWS * COLS))];
        if (target && target !== nav.current) { nav.focus(target); sound.play("focus"); } else sound.play("edge");
        return true;
      }
      default: return false;
    }
  }

  // Neues Bildschirmfoto oder anderswo gelöscht (Kern: gallery.changed)
  function onEvent(event) {
    if (event === "gallery.changed") load();
  }

  render(null);
  load({ focusGrid: true });
  return {
    el, onPad, onEvent,
    onShow(info) {
      // Zurück aus dem Vollbild: der Fokus auf das zuletzt gezeigte Bild
      if (info && info.back && wantKey) {
        const node = nodes.get(wantKey);
        if (node && grid.contains(node)) { nav.focus(node.firstChild); scrollTo(node.firstChild); wantKey = null; }
      }
      nav.ensure();
    },
    destroy() { closed = true; serial += 1; },
  };
}

// ================================================================ Vollbild
function createView(params = {}) {
  const items = Array.isArray(params.items) ? params.items : [];
  const pick = !!params.pick;
  let i = Math.max(0, Math.min(items.length - 1, Number(params.index) || 0));
  let serial = 0;
  const img = h("img.gal-full", { alt: "" });
  // Das Bild selbst trägt den Fokus (genau einer je Bildschirm); einen Ring zeichnet es nicht
  const stage = h("div.gal-stage", { "data-focus": true, "data-nav-up": "none", "data-nav-down": "none" });
  const titleEl = h("div.gal-bar-title.ell");
  const metaEl = h("div.gal-bar-meta.ell");
  const hintItems = [hint(S.galKeysPage, S.galHintPage), hint(S.galKeysOptions, S.galHintOptions),
    hint("btn-triangle", S.galHintInfo), hint("btn-circle", S.hintBack)];
  if (pick) hintItems.unshift(hint("btn-cross", S.galHintChoose));
  const hints = h("div.hints", hintItems);
  const bar = h("div.gal-bar", titleEl, metaEl);
  const el = h("div.gal-viewer", img, stage, bar, hints);

  const item = () => items[i] || null;

  function paintBar() {
    const it = item();
    if (!it) return;
    titleEl.textContent = titleOf(it);
    const where = it.source === "wallpapers" ? S.galSourceWallpapers : dateLong(it) + ", " + clock(it);
    metaEl.textContent = where + " · " + fmt(S.galOf, { n: i + 1, total: items.length });
  }

  // Bild und Angaben wechseln zusammen, erst wenn das neue Bild dekodiert ist (wie components/dialog.js
  // imageViewer); späte Antworten nach weiterem Blättern verfallen
  async function show() {
    const mine = ++serial;
    const it = item();
    if (!it) return;
    const next = new Image();
    next.decoding = "async";
    next.src = it.url;
    try { await next.decode(); } catch (e) { /* kaputtes Bild: trotzdem zeigen, was da ist */ }
    if (mine !== serial) return;
    img.src = it.url;
    paintBar();
  }

  function step(dir, repeat) {
    if (items.length < 2) { sound.play("edge"); return; }
    i = (i + dir + items.length) % items.length;
    sound.play("focus", { repeat });
    show();
  }

  async function removeCurrent() {
    const it = item();
    if (!it || !(await trashItem(it))) return;
    if (params.onRemoved) params.onRemoved(it);     // das Raster nimmt es aus derselben Liste
    const at = items.indexOf(it);
    if (at >= 0) items.splice(at, 1);
    if (!items.length) { router.back(); return; }
    i = Math.min(i, items.length - 1);
    show();
  }

  async function use() {
    const it = item();
    if (!it || !(await useItem(it))) return;
    if (params.onUsed) params.onUsed(it);
    if (pick) leavePick();
  }

  async function options() {
    const it = item();
    if (!it) return;
    const choice = await openMenu([
      { id: "use", label: S.galUse, icon: pick ? "check" : "home" },
      { id: "info", label: S.galInfo, icon: "info" },
      { id: "delete", label: S.galDelete, icon: "trash" },
    ], { cls: "menu-center", title: titleOf(it) });
    if (choice === "use") use();
    else if (choice === "info") showInfo(it, img);
    else if (choice === "delete") removeCurrent();
  }

  stage.addEventListener("press", () => { if (pick) use(); });

  function onPad(button, ev) {
    switch (button) {
      case "left": case "l1": step(-1, !!ev.repeat); return true;
      case "right": case "r1": step(1, !!ev.repeat); return true;
      case "up": case "down": return true;
      case "options": if (!ev.repeat) options(); return true;
      // Angaben und Tastenhinweise aus- und wieder einblenden (nur das Bild sehen)
      case "triangle": if (!ev.repeat) { el.classList.toggle("is-bare"); sound.play("toggle-" + (el.classList.contains("is-bare") ? "off" : "on")); } return true;
      default: return false;
    }
  }

  paintBar();
  show();
  return {
    el, onPad,
    onShow() { nav.ensure(); },
    onHide() { if (params.onLeave) params.onLeave(item()); },
    destroy() { serial += 1; },
  };
}

export const screens = [{ id: "gallery", create }, { id: "gallery-view", create: createView }];
