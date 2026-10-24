// Spielebibliothek (07 Abschnitt 5 und 11.5): Reiter „Favoriten“ (E99), „Installiert“, „Alle“,
// „Ausgeblendet“, Raster aus Covern im Hochformat (art.cover, sechs Spalten), links Rundknöpfe für
// Sortieren/Filtern, Suche und „Spiel installieren“. Gerollt wird per translate3d am Raster.
// Bilder laden träge: dekodiert wird nur, was im Sichtbereich liegt; die Zeilen knapp davor und
// dahinter werden schon geladen (ohne Dekodieren), alles Weitere erst beim Rollen.
import { h, icon, gridTop } from "../core/dom.js";
import { call, notify } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { router } from "../core/router.js";
import { sound } from "../core/sound.js";
import { store } from "../core/store.js";
import { S, fmt } from "../core/strings.js";
import { artBox, loadArt, updateArt, marks, stateOf, titledArt } from "../components/art.js";
import { openEntryMenu, activateEntry, isRunning } from "../components/entry-actions.js";
import { openMenu } from "../components/menu.js";
import { askText } from "../components/osk-input.js";

const COLS = 6;
const ROWS_SHOWN = 3;             // zwei volle Zeilen und der Ansatz der dritten
const ROWS_AHEAD = 2;             // so viele Zeilen dahinter schon laden (nicht dekodieren)
// Favoriten (E99) links von „Installiert“: L1 aus dem Reiter, mit dem die Bibliothek aufgeht
const TABS = [
  { id: "favorites", label: S.libTabFavorites },
  { id: "installed", label: S.libTabInstalled },
  { id: "all", label: S.libTabAll },
  { id: "hidden", label: S.libTabHidden },
];
const SORTS = [
  { id: "recent", label: S.libSortRecent }, { id: "title", label: S.libSortTitle },
  { id: "playtime", label: S.libSortPlaytime }, { id: "added", label: S.libSortAdded },
];
// Filter nach Herkunft (E99, entry.origin des Kerns): „local“ umfasst Store-, Launcher- und eigene
// Spiele, als „Aus Archiv installiert“ standen dort auch Epic, GOG und Battle.net
const SOURCES = [
  { id: "steam", label: S.libSourceSteam }, { id: "proton", label: S.libOriginLutrisWin },
  { id: "native", label: S.libOriginLutrisLinux }, { id: "switch", label: S.libSourceSwitch },
  { id: "wiiu", label: S.libSourceWiiu }, { id: "epic", label: S.libOriginEpic }, { id: "gog", label: S.libOriginGog },
  { id: "battlenet", label: S.libOriginBattlenet }, { id: "ubisoft", label: S.libOriginUbisoft },
  { id: "rockstar", label: S.libOriginRockstar }, { id: "itch", label: S.libOriginItch },   // itch.io (E100)
  { id: "own-windows", label: S.libOriginOwnWin },
  { id: "own-linux", label: S.libOriginOwnLinux },
  { id: "rom", label: S.libOriginRom },                      // ROM-Ordner ohne SRM (E105)
];
const originOf = (e) => e.origin || e.source;
// Die Sortierung bleibt gemerkt, auch über einen Neustart des Home (E99; nur in diesem WebView).
// Der Filter nach Herkunft gilt nur, bis die Bibliothek zugeht: Beim nächsten Öffnen fehlten sonst
// ohne sichtbaren Grund fast alle Spiele.
const SORT_KEY = "couch.library.sort";
function savedSort() {
  try { const v = localStorage.getItem(SORT_KEY); return SORTS.some((s) => s.id === v) ? v : "recent"; } catch { return "recent"; }
}
const prefs = { sort: savedSort(), source: "" };

// Anfangsbuchstabe fürs Springen mit L2/R2 bei „Name (A–Z)“ (E99): Umlaute zählen zu ihrem
// Grundbuchstaben (die Sortierung des Kerns ordnet „Ö“ bei „O“), Ziffern und Zeichen als „#“
function letterOf(entry) {
  const first = String((entry && (entry.sort_title || entry.title)) || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").trim().charAt(0).toUpperCase();
  return first >= "A" && first <= "Z" ? first : "#";
}

function create(params = {}) {
  const state = { tab: params.tab || "installed", all: [], entries: [], top: 0, query: "", failed: false };
  const tabEls = TABS.map((t) => h("div.lib-tab", { "data-focus": true, dataset: { tab: t.id }, text: t.label }));
  const tabs = h("div.lib-tabs", { "data-focus-group": true }, tabEls);
  const count = h("span.lib-count.ell");
  const sort = h("span.lib-sort");
  // data-scroll: Der rechte Stick rollt das Raster Reihe für Reihe (core/nav.js, E99)
  const grid = h("div.lib-grid", { "data-scroll": true });
  const view = h("div.lib-view", { "data-focus-group": true }, grid);
  const empty = h("div.lib-empty", { hidden: true });
  const side = {
    sort: h("div.btn.is-secondary.is-round.lib-side-btn", { "data-focus": true, dataset: { id: "sort" } }, icon("sort")),
    search: h("div.btn.is-secondary.is-round.lib-side-btn", { "data-focus": true, dataset: { id: "search" } }, icon("search")),
    install: h("div.btn.is-secondary.is-round.lib-side-btn", { "data-focus": true, dataset: { id: "install" } }, icon("archive")),
    // E108: Spielzeit-Rückblick, als letzter (bestehende Wege über Links und Runter bleiben gleich)
    stats: h("div.btn.is-secondary.is-round.lib-side-btn", { "data-focus": true, dataset: { id: "stats" } }, icon("clock")),
  };
  const sideLabel = h("div.lib-side-label", { hidden: true });
  const sideBox = h("div.lib-side", { "data-focus-group": true }, side.sort, side.search, side.install, side.stats, sideLabel);
  const el = h("div.library",
    h("div.lib-icon", icon("library")),
    h("div.lib-title", { text: S.library }),
    tabs,
    h("div.lib-head", count, sort),
    sideBox, view, empty);
  const nodes = new Map();        // Kennung -> Kachel
  let loadSerial = 0;
  let pendingFocus = null;        // Kennung, auf die der Fokus soll, sobald sie in der Liste steht

  function cover(entry) {
    const node = h("div.cover", { "data-focus": true, dataset: { id: entry.id } },
      artBox(entry, { kind: "cover", lazy: true }),
      h("div.cover-marks"),
      h("span.cover-state", { hidden: true }),
      h("span.tile-run"),
      h("div.cover-name", h("span.clamp2", { text: entry.title })));
    return node;
  }

  function refresh(node, entry) {
    node.entry = entry;
    updateArt(node.querySelector(".art"), entry);
    // Platzhalter tragen Titel und Marke schon im Bild: dann weder Name noch Marken noch Zustand
    // darüber (library.css .cover.is-titled); FSR 4 und VR stehen im Hub
    node.classList.toggle("is-titled", titledArt(entry, "cover"));
    node.querySelector(".cover-marks").replaceChildren(...[entry.badge ? h("span.cover-badge", { text: entry.badge }) : null, ...marks(entry, { withState: false })].filter(Boolean));
    const st = stateOf(entry);
    const flag = node.querySelector(".cover-state");
    flag.hidden = !(st === "unavailable" || st === "not-installed");
    flag.textContent = flag.hidden ? "" : st === "not-installed" ? S.notInstalled : S.unavailable;
    node.classList.toggle("is-running", isRunning(entry));
    node.classList.toggle("is-off", entry.available === false || entry.installed === false);
    node.querySelector(".cover-name > span").textContent = entry.title;
  }

  function entryOf(node) {
    return node && node.entry ? node.entry : null;
  }

  function sortLabel() {
    const label = SORTS.find((s) => s.id === prefs.sort).label;
    const at = nav.current && grid.contains(nav.current) ? entryOf(nav.current) : null;
    return prefs.sort === "title" && at ? fmt(S.libSortLetter, { value: label, letter: letterOf(at) }) : fmt(S.libSort, { value: label });
  }

  function sourceLabel() {
    const hit = SOURCES.find((s) => s.id === prefs.source);
    return hit ? hit.label : "";
  }

  function render(keepId) {
    state.entries = prefs.source ? state.all.filter((e) => originOf(e) === prefs.source) : state.all;
    const covers = state.entries.map((entry) => {
      let node = nodes.get(entry.id);
      if (!node) { node = cover(entry); nodes.set(entry.id, node); }
      refresh(node, entry);
      return node;
    });
    for (const [id, node] of [...nodes]) if (!covers.includes(node)) nodes.delete(id);
    const same = covers.length === grid.children.length && covers.every((c, i) => grid.children[i] === c);
    if (!same) grid.replaceChildren(...covers);
    covers.forEach((c, i) => {
      // Aus der obersten Zeile führt Hoch immer zum aktiven Reiter, aus der linken Spalte Links zu den Rundknöpfen
      if (i < COLS) c.setAttribute("data-nav-up", ".lib-tab.is-active"); else c.removeAttribute("data-nav-up");
      if (i % COLS === 0) c.setAttribute("data-nav-left", ".lib-side-btn[data-focus-default]"); else c.removeAttribute("data-nav-left");
    });
    const tab = TABS.find((t) => t.id === state.tab);
    const parts = [];
    if (state.query) parts.push(fmt(S.libQuery, { q: state.query }));
    if (prefs.source) parts.push(sourceLabel());
    parts.push(state.tab === "all" ? fmt(S.libCount, { n: state.entries.length }) : fmt(S.libCountOf, { label: tab.label, n: state.entries.length }));
    count.textContent = parts.join(" · ");
    sort.textContent = sortLabel();
    empty.hidden = covers.length > 0;
    empty.textContent = state.failed ? S.homeFailed : state.query ? S.libEmptyQuery : state.tab === "hidden" ? S.libEmptyHidden
      : state.tab === "favorites" ? S.libEmptyFavorites : S.libEmpty;
    for (const t of tabEls) {
      t.classList.toggle("is-active", t.dataset.tab === state.tab);
      if (covers.length) t.setAttribute("data-nav-down", ".cover[data-focus-default]"); else t.removeAttribute("data-nav-down");
    }
    for (const c of grid.querySelectorAll("[data-focus-default]")) c.removeAttribute("data-focus-default");
    const keep = covers.find((c) => c.dataset.id === keepId) || covers[0];
    if (keep) keep.setAttribute("data-focus-default", "");
    for (const b of Object.values(side)) { if (keep) b.setAttribute("data-nav-right", ".cover[data-focus-default]"); else b.removeAttribute("data-nav-right"); }
    return keep;
  }

  async function fetchList() {
    const p = { kind: "game", filter: state.tab, sort: prefs.sort };
    if (state.query) p.query = state.query;
    return (await call("lib.list", p)).entries || [];
  }

  // fresh: anderer Reiter, das Raster beginnt beim ersten Cover (nichts vom alten Reiter festhalten)
  async function load({ focusGrid = false, keepId = null, fresh = false } = {}) {
    const serial = ++loadSerial;
    let entries = [];
    state.failed = false;
    try {
      entries = await fetchList();
    } catch (e) {
      state.failed = true;
      notify("sys.log", { level: "warning", text: "[library] lib.list: " + (e && e.message) });
    }
    if (serial !== loadSerial) return;
    const wasInGrid = nav.current && grid.contains(nav.current);
    const marked = fresh ? null : wasInGrid ? nav.current : grid.querySelector("[data-focus-default]");
    const wanted = keepId || pendingFocus || (marked ? marked.dataset.id : null);
    const at = marked ? [...grid.children].indexOf(marked) : -1;
    state.all = entries;
    let keep = render(wanted);
    if (keep && pendingFocus && keep.dataset.id === pendingFocus) pendingFocus = null;
    // Der Eintrag ist weg (gelöscht, ausgeblendet): der Nachbar an derselben Stelle bekommt den Fokus
    if (keep && wanted && keep.dataset.id !== wanted && at > 0) {
      keep = grid.children[Math.min(at, grid.children.length - 1)] || keep;
      for (const c of grid.querySelectorAll("[data-focus-default]")) c.removeAttribute("data-focus-default");
      keep.setAttribute("data-focus-default", "");
    }
    if ((focusGrid || wasInGrid) && keep) nav.focus(keep);
    else if (!keep && (focusGrid || wasInGrid)) nav.focus(tabEls.find((t) => t.dataset.tab === state.tab));
    scrollTo(nav.current && grid.contains(nav.current) ? nav.current : keep);
    nav.ensure();
  }

  // Nur die Bilder der genannten Einträge tauschen (lib.changed mit reason "art")
  async function patchArt(ids) {
    let entries;
    try { entries = await fetchList(); } catch (e) { return; }
    for (const entry of entries) {
      const node = ids.includes(entry.id) ? nodes.get(entry.id) : null;
      if (!node) continue;
      const i = state.all.findIndex((e) => e.id === entry.id);
      if (i >= 0) state.all[i] = entry;
      refresh(node, entry);
    }
    reveal();
  }

  // Lädt die Bilder um den Sichtbereich: sichtbare Zeilen mit Dekodieren, die davor und dahinter ohne
  function reveal() {
    const covers = grid.children;
    const first = Math.max(0, state.top - 1) * COLS;
    const last = Math.min(covers.length, (state.top + ROWS_SHOWN + ROWS_AHEAD) * COLS);
    for (let i = first; i < last; i += 1) {
      const row = Math.floor(i / COLS);
      loadArt(covers[i].querySelector(".art"), { decode: row >= state.top && row < state.top + ROWS_SHOWN });
    }
  }

  // Die fokussierte Zeile bleibt unter den beiden voll sichtbaren (core/dom.js gridTop, wie im Store)
  function scrollTo(node) {
    state.top = gridTop(node && grid.contains(node) ? [...grid.children].indexOf(node) : -1, COLS, state.top);
    grid.style.setProperty("--top", state.top);
    reveal();
  }

  // focusGrid: Der Fokus geht mit (L1/R1): aufs erste Cover des neuen Reiters, bei leerer Liste auf
  // den aktiven Reiter. Nie bleibt der Ring auf einem Reiter stehen, der nicht mehr aktiv ist.
  function setTab(id, focusGrid) {
    if (id === state.tab) return;
    state.tab = id;
    state.top = 0;
    sound.play("tab");
    const active = tabEls.find((t) => t.dataset.tab === id);
    for (const t of tabEls) t.classList.toggle("is-active", t === active);
    if (nav.current && tabs.contains(nav.current)) nav.focus(active);
    load({ focusGrid, fresh: true });
  }

  // ---------------------------------------------------------------- Sortieren, Filtern, Suchen
  async function openSortMenu() {
    const present = new Set(state.all.map(originOf));
    const tick = (on) => (on ? "check" : "none");
    const items = [
      ...SORTS.map((s) => ({ id: "sort:" + s.id, label: s.label, icon: tick(prefs.sort === s.id) })),
      { sep: true },
      { id: "source:", label: S.libSourceAll, icon: tick(!prefs.source) },
      ...SOURCES.filter((s) => present.has(s.id) || prefs.source === s.id).map((s) => ({ id: "source:" + s.id, label: s.label, icon: tick(prefs.source === s.id) })),
    ];
    if (state.query) items.push({ sep: true }, { id: "clear", label: S.libQueryClear, icon: "close" });
    const choice = await openMenu(items, { cls: "menu-lib", title: S.libBtnSort, initial: "sort:" + prefs.sort });
    if (!choice) return;
    const inGrid = nav.current && grid.contains(nav.current);
    if (choice === "clear") { state.query = ""; load({ focusGrid: inGrid }); return; }
    const [kind, value] = choice.split(":");
    if (kind === "sort") {
      if (prefs.sort === value) return;
      prefs.sort = value;
      try { localStorage.setItem(SORT_KEY, value); } catch { /* ohne Speicher gilt die Wahl bis zum Neustart */ }
      state.top = 0;
      load({ focusGrid: inGrid });
    } else {
      if (prefs.source === value) return;
      prefs.source = value;
      state.top = 0;
      const keep = render(null);
      if (inGrid) { if (keep) nav.focus(keep); else nav.focus(side.sort); }
      scrollTo(keep);
      nav.ensure();
    }
  }

  // Suchbegriff über die Bildschirmtastatur (components/osk-input.js, E99); null = abgebrochen
  async function search() {
    let typed;
    try { typed = await askText({ title: S.libSearchTitle, initial: state.query }); } catch (e) { return; }
    if (typed === null) return;
    const text = typed.trim();
    if (text === state.query) return;
    state.query = text;
    state.top = 0;
    await load({});
    const first = grid.firstElementChild;
    if (first) nav.focus(first);
  }

  const SIDE_ACTIONS = { sort: openSortMenu, search, install: () => router.go("installer"),
    stats: () => { if (router.known("stats")) router.go("stats"); else sound.play("edge"); } };
  const SIDE_LABELS = { sort: S.libBtnSort, search: S.libBtnSearch, install: S.libBtnInstall, stats: S.libBtnStats };
  side.sort.setAttribute("data-focus-default", "");
  sideBox.addEventListener("press", (e) => { const b = e.target.closest(".lib-side-btn"); if (b) SIDE_ACTIONS[b.dataset.id](); });
  // Was ein Rundknopf tut, steht daneben, solange er den Fokus hat
  sideBox.addEventListener("nav-focus", (e) => {
    const b = e.target.closest(".lib-side-btn");
    for (const other of Object.values(side)) other.toggleAttribute("data-focus-default", other === b);
    sideLabel.textContent = SIDE_LABELS[b.dataset.id];
    sideLabel.dataset.at = b.dataset.id;
    sideLabel.hidden = false;
  });
  sideBox.addEventListener("nav-blur", (e) => { if (!e.detail.to || !sideBox.contains(e.detail.to)) sideLabel.hidden = true; });

  grid.addEventListener("nav-focus", (e) => {
    for (const c of grid.querySelectorAll("[data-focus-default]")) c.removeAttribute("data-focus-default");
    e.target.setAttribute("data-focus-default", "");
    for (const b of Object.values(side)) b.setAttribute("data-nav-right", ".cover[data-focus-default]");
    scrollTo(e.target);
    if (prefs.sort === "title") sort.textContent = sortLabel();
  });
  grid.addEventListener("press", (e) => {
    const entry = entryOf(e.target.closest(".cover"));
    if (!entry) return;
    // Ein laufendes Spiel kommt sofort nach vorn; sonst öffnet sich seine Seite
    if (isRunning(entry)) activateEntry(entry); else router.go("hub", { entry });
  });
  tabs.addEventListener("press", (e) => { const t = e.target.closest(".lib-tab"); if (t) setTab(t.dataset.tab, false); });

  // Nach Namen sortiert springen L2/R2 zum vorigen/nächsten Anfangsbuchstaben (E99), sonst zwei Zeilen.
  // L2 mitten in einer Gruppe führt erst an ihren Anfang.
  function letterJump(covers, at, forward) {
    const letters = covers.map((c) => letterOf(entryOf(c)));
    const here = letters[at];
    if (forward) {
      for (let i = at + 1; i < covers.length; i += 1) if (letters[i] !== here) return covers[i];
      return null;
    }
    let start = at;
    while (start > 0 && letters[start - 1] === here) start -= 1;
    if (start < at) return covers[start];
    if (start === 0) return null;
    const prev = letters[start - 1];
    let i = start - 1;
    while (i > 0 && letters[i - 1] === prev) i -= 1;
    return covers[i];
  }

  function onPad(button, ev) {
    if (ev.repeat) return false;
    const index = TABS.findIndex((t) => t.id === state.tab);
    const inGrid = nav.current && grid.contains(nav.current);
    switch (button) {
      case "l1": if (index > 0) setTab(TABS[index - 1].id, true); else sound.play("edge"); return true;
      case "r1": if (index < TABS.length - 1) setTab(TABS[index + 1].id, true); else sound.play("edge"); return true;
      case "l2": case "r2": {
        if (!inGrid) return true;
        const covers = [...grid.children];
        const at = covers.indexOf(nav.current);
        const target = prefs.sort === "title" ? letterJump(covers, at, button === "r2")
          : covers[Math.max(0, Math.min(covers.length - 1, at + (button === "r2" ? 2 : -2) * COLS))];
        if (target && target !== nav.current) { nav.focus(target); sound.play("focus"); } else sound.play("edge");
        return true;
      }
      case "triangle": openSortMenu(); return true;
      // Das Optionsmenü steht an der gewählten Kachel (07 6.7: rechts daneben, Oberkante bündig)
      case "options": { const entry = inGrid ? entryOf(nav.current) : null; if (entry) openEntryMenu(entry, { anchor: nav.current }); return true; }
      default: return false;
    }
  }

  function onEvent(event, payload) {
    if (event === "lib.changed") {
      if (payload && payload.reason === "art" && Array.isArray(payload.ids)) patchArt(payload.ids);
      else load();
    } else if (event === "app.changed") {
      for (const c of grid.children) c.classList.toggle("is-running", store.isRunning(c.dataset.id));
    }
  }

  // params.focus: nach einer Installation steht der Fokus auf dem neuen Spiel (auch wenn es erst mit
  // dem nächsten lib.changed in der Liste steht; bewegt der Nutzer den Fokus vorher, gilt das nicht mehr)
  pendingFocus = params.focus || null;
  grid.addEventListener("nav-move", () => { pendingFocus = null; });
  load({ focusGrid: true });
  return {
    el, onPad, onEvent,
    onShow() { nav.ensure(); },
    // Zu: der Filter nach Herkunft gilt nicht mehr (die Sortierung bleibt)
    destroy() { prefs.source = ""; },
  };
}

export default { id: "library", create };
