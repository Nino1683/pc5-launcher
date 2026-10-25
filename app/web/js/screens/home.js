// Home (07 Abschnitt 2 und 11.2/11.3): Reiter „Spiele“ und „Medien“, Kachelreihe, bewegter
// Hintergrund des fokussierten Eintrags (E33), Hub darunter, Statusleiste oben rechts.
//   Spiele  Steam · die zwölf zuletzt gespielten (liefert der Kern) · Spiel installieren · Spielebibliothek
//   Medien  die Apps des Kerns in seiner Reihenfolge (YouTube, Kodi, Browser, Desktop, EmuDeck)
// Der Fokus hängt am Eintrag: Listen kommen beim Start mehrmals neu (Quellen, Bilder), er bleibt
// auf demselben Spiel und steht anfangs auf dem zuletzt gespielten.
import { h, icon, setIcon, after, cssMs } from "../core/dom.js";
import { call, notify } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { router } from "../core/router.js";
import { sound } from "../core/sound.js";
import { store } from "../core/store.js";
import { S, fmt } from "../core/strings.js";
import { badge, marks } from "../components/art.js";
import { createTileRow } from "../components/tile-row.js";
import { createHub } from "../components/hub.js";
import { createStatusbar } from "../components/statusbar.js";
import { createBackdrop } from "../components/backdrop.js";
import { endingShown } from "../components/ending.js";
import { toast } from "../components/toast.js";
import { activateEntry, openEntryMenu, openPowerMenu, isRunning, closeEntry, desktopMode } from "../components/entry-actions.js";

const JUMP = 5;                   // L2/R2 springen um fünf Einträge
const AREAS = ["games", "media"];
const RETRY_MS = 2500;            // antwortet der Kern nicht, fragt das Home noch ein paarmal nach
const RETRIES = 6;
// Danach seltener, mit wachsendem Abstand (E99): Vorher endete die Suche nach 15 s stumm
const RETRY_SLOW_MS = 30 * 1000;
const RETRY_SLOW_MAX_MS = 5 * 60 * 1000;
const CARDS_AGAIN_MS = 2 * 3600 * 1000;

const SYSTEM = { kind: "app", system: true, badge: "", art: {}, available: true, installed: true };
const LIBRARY_TILE = { ...SYSTEM, id: "sys:library", title: S.library, icon: "library", note: S.libraryHint, mainLabel: S.open };
const INSTALL_TILE = { ...SYSTEM, id: "sys:install", title: S.installTile, icon: "archive", note: S.installTileHint, mainLabel: S.installTileMain };
const FILES_TILE = { ...SYSTEM, id: "sys:files", title: S.exTitle, icon: "folder", note: S.exTileHint, mainLabel: S.open };   // E62
const STORE_TILE = { ...SYSTEM, id: "sys:store", title: S.stTitle, icon: "store", note: S.stTileHint, mainLabel: S.open };   // E68

// Kachel „Spiel installieren“: Die Unterzeile nennt eine laufende Installation
function installTile() {
  const jobs = store.state.jobs || [];
  const choice = jobs.find((j) => j.state === "needs-choice");
  const busy = jobs.find((j) => j.state === "running" || j.state === "finishing" || j.state === "installing" || j.state === "queued");
  // Ein Installationsprogramm meldet keinen Anteil (E114)
  const note = choice ? fmt(S.installTileChoice, { title: choice.title })
    : busy && busy.state === "installing" ? fmt(S.installTileInstalling, { title: busy.title })
    : busy ? fmt(S.installTileBusy, { title: busy.title, n: Math.round(Number(busy.percent) || 0) }) : S.installTileHint;
  return { ...INSTALL_TILE, note };
}

function create() {
  const state = { area: "games", hubOpen: false, loaded: false, shown: null, failed: 0, failCode: "", order: [], moving: null,
    focusPending: false };
  const rows = {};
  let labelTimer = null;
  let detailTimer = null;
  let retryTimer = null;
  let artTimer = null;
  let loadSerial = 0;
  const artIds = new Set();

  // data-label: der Reiter reserviert die Breite seines fetten Schnitts (home.css .tab::before)
  const tabs = {
    games: h("div.tab#tab-games", { "data-focus": true, "data-nav-down": "#row-current", "data-label": S.tabGames, text: S.tabGames }),
    media: h("div.tab#tab-media", { "data-focus": true, "data-nav-down": "#row-current", "data-label": S.tabMedia, text: S.tabMedia }),
  };
  const head = h("div.home-head", { "data-focus-group": true }, tabs.games, tabs.media);
  // Ein/Aus rechts neben dem Zahnrad (am Platz des Profils): in zwei Schritten aus der Kopfzeile erreichbar
  const power = h("div.status-power#status-power", { "data-focus": true, "data-nav-down": "#row-current" }, icon("power"));
  power.addEventListener("press", () => openPowerMenu(power));
  const status = createStatusbar({ withGear: true, onSettings: () => router.go("settings"), extra: power, downloads: true });
  status.gear.setAttribute("data-nav-down", "#row-current");
  const label = h("div.rowlabel");
  const hub = createHub({
    onMain: (entry) => activate(entry),
    onMore: (entry) => openEntryMenu(entry, { anchor: hub.more }),
    // Nebenknopf: laufendes Spiel schließen bzw. Desktop-Modus der ganzen Sitzung
    onSecond: (entry, kind) => { if (kind === "close") closeEntry(entry); else if (kind === "desktop-mode") desktopMode(); },
    // Karte unter der Store-Kachel: gleich die Seite des Spiels im Store
    onStoreCard: (c) => router.go("store-item", { store: c.store, id: c.id, card: c.card || c }),
  });
  hub.main.setAttribute("data-nav-up", "#row-current");
  hub.second.setAttribute("data-nav-up", "#row-current");
  hub.more.setAttribute("data-nav-up", "#row-current");
  const rowsBox = h("div.home-rows");
  // Unaufdringliche Anzeige: unten rechts, solange alle Bilder geladen werden (sys.state.art_job);
  // oben rechts unter der Statusleiste (is-warn), solange die Bibliothek nicht antwortet (E99: die
  // Reihen zeigen dann nur, was zuletzt da war, oder nur die festen Kacheln; vorher sagte das niemand)
  const note = h("div.home-note", { hidden: true }, icon("download"), h("span"));
  const backdrop = createBackdrop();
  const el = h("div.home", backdrop.el, head, status.el, rowsBox, label, hub.mini, hub.el, note);

  for (const area of AREAS) {
    rows[area] = createTileRow({
      onChange: (entry, index, info) => showEntry(entry, info),
      // Kreuz auf der Kachel öffnet den Hub (07 Abschnitt 8); ein laufendes Spiel kommt sofort nach vorn
      onPress: (entry) => {
        if (entry.system || isRunning(entry)) activate(entry);
        else if (!nav.focus(hub.main)) nav.focus(hub.more);
      },
    });
    rows[area].el.dataset.area = area;
    rowsBox.append(rows[area].el);
  }

  const row = () => rows[state.area];

  function activate(entry) {
    if (entry.id === LIBRARY_TILE.id) router.go("library");
    else if (entry.id === INSTALL_TILE.id) router.go("installer");
    else if (entry.id === FILES_TILE.id) router.go("explorer");
    else if (entry.id === STORE_TILE.id) router.go("store");
    else activateEntry(entry);
  }

  // ---------------------------------------------------------------- Daten
  async function fetchLists() {
    const [games, apps, order] = await Promise.all([
      call("lib.list", { kind: "game", filter: "recent" }).then((r) => (r && r.entries) || []),
      call("lib.list", { kind: "app" }).then((r) => (r && r.entries) || []),
      call("home.order").then((r) => (r && Array.isArray(r.games) ? r.games : [])).catch(() => []),
    ]);
    state.order = order;
    return { games, apps };
  }

  // Eigene Reihenfolge (E69, „Kachel verschieben“): Spiele, die nicht in der Liste stehen (neu),
  // vorn nach zuletzt gespielt, dahinter die Liste. Ohne Liste wie bisher nach zuletzt gespielt.
  function ordered(games) {
    if (!state.order.length) return games;
    const pos = new Map(state.order.map((id, i) => [id, i]));
    const fresh = games.filter((g) => !pos.has(g.id));
    const known = games.filter((g) => pos.has(g.id)).sort((a, b) => pos.get(a.id) - pos.get(b.id));
    return [...fresh, ...known];
  }
  // Steam steht am Anfang der Spiele-Reihe (07 11.2), alle anderen Apps unter „Medien“
  const inGames = (a) => a.id === "app:steam" || (a.tags || []).includes("games-row");

  async function load() {
    const serial = ++loadSerial;
    let lists = null;
    let code = "";
    try {
      lists = await fetchLists();
    } catch (e) {
      code = (e && e.error) || "";
      notify("sys.log", { level: "warning", text: "[home] lib.list: " + (e && e.message) });
    }
    if (serial !== loadSerial) return;
    clearTimeout(retryTimer);
    if (!lists) {
      // Der Kern antwortet nicht: stehen lassen, was da ist, es sagen und noch einmal fragen
      state.failed += 1;
      state.failCode = code;
      if (!state.loaded) setRows([], []);
      markCurrent();
      paintNote();
      const wait = retryDelay();
      if (wait) retryTimer = setTimeout(load, wait);
      return;
    }
    state.failed = 0;
    state.failCode = "";
    paintNote();
    setRows(lists.games, lists.apps);
  }

  // Nach den schnellen Versuchen seltener (30 s, 60 s … höchstens 5 min). Ein ausgefallener Baustein
  // (Unavailable, E47) bleibt bis zum Neustart aus: dort hilft kein Neuversuch, nur lib.changed.
  function retryDelay() {
    if (state.failCode === "Unavailable") return 0;
    if (state.failed <= RETRIES) return RETRY_MS;
    return Math.min(RETRY_SLOW_MAX_MS, RETRY_SLOW_MS * 2 ** Math.min(8, state.failed - RETRIES - 1));
  }

  function setRows(games, apps) {
    // Store ganz links (Wunsch 2026-10-19), Installieren und Bibliothek am Ende
    const list = [STORE_TILE, ...apps.filter(inGames), ...ordered(games), installTile(), LIBRARY_TILE];
    const first = list.findIndex((e) => e.kind === "game");
    let last = -1;
    list.forEach((e, i) => { if (e.kind === "game") last = i; });
    rows.games.setEntries(list, { first: Math.max(0, first), last: last < 0 ? undefined : last });
    rows.media.setEntries([...apps.filter((a) => !inGames(a)), FILES_TILE]);
    const initial = !state.loaded;
    state.loaded = true;
    // Die Startanimation (components/intro.js) geht erst ins Home, wenn die Reihen stehen
    if (initial) document.dispatchEvent(new CustomEvent("couch-home-ready"));
    markCurrent();
    const current = row().current;
    if (!current || current.id !== state.shown) showEntry(current, { instant: true });
    else refreshEntry(current);
    // Beim ersten Mal liegt der Fokus auf der Reihe (vorher gab es nur Kopfzeile und Hub). Liegt gerade
    // ein anderer Bildschirm darüber (Einrichtungs-Assistent beim ersten Start, E105), holt onShow das nach
    if (initial && nav.scope && nav.scope.el === el) row().focusCurrent();
    else if (initial) state.focusPending = true;
    nav.ensure();
  }

  // lib.changed mit reason "art": nur die Bilder der genannten Einträge tauschen, keine Reihe neu bauen
  function queueArt(ids) {
    for (const id of ids) artIds.add(id);
    clearTimeout(artTimer);
    artTimer = setTimeout(async () => {
      const ids2 = new Set(artIds);
      artIds.clear();
      let lists;
      try { lists = await fetchLists(); } catch (e) { return; }
      const changed = [...lists.games, ...lists.apps].filter((e) => ids2.has(e.id));
      const hit = [...rows.games.patch(changed), ...rows.media.patch(changed)];
      const current = row().current;
      if (current && hit.some((e) => e.id === current.id)) refreshEntry(row().current);
    }, 120);
  }

  function markCurrent() {
    for (const area of AREAS) {
      const active = area === state.area;
      rows[area].el.classList.toggle("is-off", !active);
      rows[area].el.inert = !active;
      const old = rows[area].el.querySelector("#row-current");
      if (old) old.removeAttribute("id");
      tabs[area].classList.toggle("is-active", active);
    }
    const tile = row().currentTile;
    if (tile) {
      tile.id = "row-current";
      tile.setAttribute("data-nav-up", "#tab-" + state.area);
    }
    // Leer ist eine Reihe nur, bis die Listen das erste Mal da sind: Store, Installieren, Bibliothek
    // und Dateien stehen immer darin (der Hinweis bei einem Fehler steht in note)
    const none = !row().current;
    hub.el.hidden = none;
    label.hidden = none;
  }

  // ---------------------------------------------------------------- fokussierter Eintrag
  function fillLabel(entry) {
    label.replaceChildren(...[badge(entry.badge), h("span.rowlabel-title.ell", { text: entry.title }), ...marks(entry)].filter(Boolean));
  }

  // Derselbe Eintrag mit neuem Stand (Bilder, läuft, Installation, Optionen): ohne Ausblenden
  // nachziehen, die Karten des Hub aus einem frischen lib.get (sonst stand dort nach einer Änderung
  // in den Spieleinstellungen noch der alte Wert)
  // Hintergrund der Store-Kachel (Wunsch 2026-10-21): das breite Bild eines aktuellen Topsellers,
  // jeden Tag ein anderes (store.homeArt). Ohne Netz bleiben die Lichtflächen. Eigenes art-Objekt:
  // SYSTEM.art teilen sich alle Systemkacheln
  async function storeArt() {
    let r = null;
    try { r = await call("store.homeArt"); } catch (e) { return; }
    if (!r || !r.ok || !r.hero) return;
    STORE_TILE.art = { hero: String(r.hero) };
    const hit = rows.games.patch([STORE_TILE]);
    if (hit.length && row().current && row().current.id === STORE_TILE.id) refreshEntry(row().current);
  }

  // Karten unter der Store-Kachel (store.homeCards): Gratis bei Epic, Angebote bei Steam und GOG.
  // Alle zwei Stunden neu (die Gratis-Spiele wechseln donnerstags, Angebote öfter)
  let cardsTimer = null;
  async function storeCards() {
    clearTimeout(cardsTimer);
    cardsTimer = setTimeout(storeCards, CARDS_AGAIN_MS);
    let r = null;
    try { r = await call("store.homeCards"); } catch (e) { return; }
    if (r && Array.isArray(r.cards)) hub.setStoreCards(r.cards);
  }

  function refreshEntry(entry) {
    if (!entry) return;
    hub.setEntry(entry);
    fillLabel(entry);
    backdrop.set(entry);
    loadDetail(entry, 120);
  }

  function loadDetail(entry, delay) {
    clearTimeout(detailTimer);
    if (!entry || entry.system) return;
    detailTimer = setTimeout(async () => {
      try { hub.setDetail(await call("lib.get", { id: entry.id })); } catch (e) { /* Karten bleiben, wie sie sind */ }
    }, delay);
  }

  function showEntry(entry, info = {}) {
    markCurrent();
    state.shown = entry ? entry.id : null;
    if (!entry) { backdrop.set(null, { instant: true }); return; }
    hub.setEntry(entry);
    // Titel: sofort aus, nach dem Stillstand der Reihe wieder ein (07 Abschnitt 7)
    clearTimeout(labelTimer);
    const fill = () => { fillLabel(entry); label.classList.remove("is-out"); };
    if (info.instant) fill();
    else {
      label.classList.add("is-out");
      labelTimer = after(info.repeat ? "--t-row-fast" : "--t-row", fill);
    }
    backdrop.set(entry, { instant: !!info.instant });
    loadDetail(entry, info.instant ? 0 : cssMs("--t-bg-delay"));
  }

  // ---------------------------------------------------------------- Reiter
  function switchArea(area) {
    if (area === state.area || !AREAS.includes(area)) { sound.play("edge"); return; }
    const inHead = nav.current && head.contains(nav.current);
    state.area = area;
    markCurrent();
    sound.play("tab");
    showEntry(row().current, {});
    if (inHead) nav.focus(tabs[area]);
    else if (!row().focusCurrent()) nav.focus(tabs[area]);
  }
  for (const area of AREAS) tabs[area].addEventListener("press", () => switchArea(area));

  // Der Hub rückt nach oben, solange der Fokus in ihm liegt (Zustand B)
  el.addEventListener("nav-focus", (e) => {
    const open = hub.el.contains(e.target);
    if (open === state.hubOpen) return;
    state.hubOpen = open;
    el.classList.toggle("is-hub", open);
    head.inert = open;
  });
  // Links/Rechts auf der Reihe verschiebt die Reihe, nicht den Fokus
  rowsBox.addEventListener("nav-move", (e) => {
    const { dir, repeat } = e.detail;
    if (state.moving) { e.preventDefault(); return; }
    if (dir === "left" || dir === "right") { e.preventDefault(); row().move(dir === "right" ? 1 : -1, repeat); }
  });

  // ---------------------------------------------------------------- Kachel verschieben (E69)
  function gameRange() {
    const list = rows.games.entries;
    let lo = -1;
    let hi = -1;
    list.forEach((e, i) => { if (e.kind === "game" && !e.system) { if (lo < 0) lo = i; hi = i; } });
    return [lo, hi];
  }

  function startMove() {
    const entry = row().current;
    if (!entry || state.area !== "games" || entry.kind !== "game") return;
    state.moving = { id: entry.id, before: rows.games.entries.map((e) => e.id) };
    rows.games.setMoving(true);
    rows.games.focusCurrent();
    toast({ title: S.tileMove, body: S.tileMoveHint, icon: "info", sound: false });
  }

  function shiftTile(delta, repeat) {
    const [lo, hi] = gameRange();
    rows.games.shift(delta, lo, hi, repeat);
  }

  async function endMove(save) {
    const moving = state.moving;
    state.moving = null;
    rows.games.setMoving(false);
    if (!moving) return;
    if (!save) { load(); return; }
    const games = rows.games.entries.filter((e) => e.kind === "game" && !e.system).map((e) => e.id);
    state.order = games;
    try {
      const r = await call("home.setOrder", { games });
      if (r && r.ok === false) toast({ title: S.tileMove, body: r.message || S.errorGeneric, icon: "warn" });
      else sound.play("select");
    } catch (e) { toast({ title: S.tileMove, body: (e && e.message) || S.errorGeneric, icon: "warn" }); }
  }

  function tileExtras(entry) {
    if (state.area !== "games" || !entry || entry.kind !== "game") return [];
    const extra = [{ id: "move", label: S.tileMove }];
    if (state.order.length) extra.push({ id: "order-reset", label: S.tileOrderReset });
    return extra;
  }

  async function onTileExtra(id) {
    if (id === "move") startMove();
    else if (id === "order-reset") {
      state.order = [];
      try { await call("home.setOrder", { games: [] }); } catch (e) { /* bleibt bis zum Neustart */ }
      toast({ title: S.tileOrderReset, body: S.tileOrderResetDone, icon: "check", sound: false });
      load();
    }
  }

  function onPad(button, ev) {
    if (state.moving) {
      // Beim Verschieben zählen nur Links/Rechts (auch gehalten), Kreuz und Kreis
      if (button === "left" || button === "right") { shiftTile(button === "right" ? 1 : -1, !!ev.repeat); return true; }
      if (ev.repeat) return true;
      if (button === "cross" || button === "options") endMove(true);
      else if (button === "circle") endMove(false);
      return true;
    }
    if (ev.repeat && button !== "l2" && button !== "r2") return false;
    const inRow = nav.current && rowsBox.contains(nav.current);
    switch (button) {
      // Nach unten aus der Reihe immer auf den Hauptknopf (07 Zustand B). Nach Lage landete der
      // Fokus unter einer Kachel weiter rechts auf „…“ oder dem Nebenknopf (Befund Desktop-Kachel)
      case "down":
        // Store-Kachel mit Karten: zuerst die Karten (sie stehen über „Öffnen“)
        if (inRow && ((hub.firstStoreCard && nav.focus(hub.firstStoreCard)) || nav.focus(hub.main) || nav.focus(hub.more))) {
          sound.play("focus");
          return true;
        }
        return false;
      case "l1": switchArea("games"); return true;
      case "r1": switchArea("media"); return true;
      case "l2": if (inRow) row().move(-JUMP, ev.repeat); return true;
      case "r2": if (inRow) row().move(JUMP, ev.repeat); return true;
      // Optionsmenü an der Fokuskachel bzw. am „…“ des offenen Hub (07 6.7)
      case "options":
        if (row().current && (inRow || state.hubOpen)) {
          openEntryMenu(row().current, { anchor: state.hubOpen ? hub.more : row().currentTile,
            extra: inRow ? tileExtras(row().current) : [], onExtra: onTileExtra });
        }
        return true;
      case "touchpad": router.go("library"); return true;
      // Dreieck springt in die Kopfzeile (im Vorbild auf die Suche, hier auf die Einstellungen)
      case "triangle": if (nav.focus(status.gear)) sound.play("focus"); return true;
      case "circle":
        // Hub oder Kopfzeile: zurück in die Reihe; in der Reihe: zum ersten Eintrag
        if (!inRow) { if (row().focusCurrent()) sound.play("back"); return true; }
        if (row().index > 0) { row().move(-row().index); }
        return true;
      default: return false;
    }
  }

  function refreshRunning() {
    const ids = new Set((store.state.running || []).map((r) => r.entryId));
    for (const area of AREAS) rows[area].setRunning(ids);
    hub.refresh();
    if (row().current) fillLabel(row().current);
  }

  // Zurück aus einer App, die weiterläuft (PS lang, Karte „Zum Home-Bildschirm“, Symbol Home im
  // Control Center): Das Home zeigt genau diese App, im Hub mit „Fortsetzen“ im Fokus (07 2.4
  // Zustand B). Vorher blieb der Fokus, wo er beim Start war, oft bei einem anderen Spiel: Ein Kreuz
  // startete dann jenes und beendete das laufende erst nach einer Rückfrage.
  let lastFront = store.state.foreground || null;
  let returnTimer = null;
  function onForeground(front) {
    const was = lastFront;
    lastFront = front || null;
    if (front || !was) return;
    clearTimeout(returnTimer);
    // Ein Tick Geduld: Ist die App gerade abgestürzt oder beendet, steht das gleich in running
    returnTimer = setTimeout(() => {
      // Ausschalten usw. läuft (ui.open aus dem Control Center): die Tafel bleibt, kein Rücksprung
      if (endingShown() || store.state.foreground || !store.isRunning(was) || store.isClosing(was)) return;
      returnTo(was);
    }, 150);
  }
  function returnTo(id) {
    const area = AREAS.find((a) => rows[a].entries.some((e) => e.id === id));
    if (!area) return;
    router.backTo("home");
    if (area !== state.area) state.area = area;
    rows[area].select(id);
    markCurrent();
    showEntry(row().current, { instant: true });
    if (!nav.focus(hub.main)) row().focusCurrent();
  }

  function onEvent(event, payload) {
    if (event === "lib.changed") {
      if (payload && payload.reason === "art" && Array.isArray(payload.ids)) queueArt(payload.ids);
      else load();
    } else if (event === "app.changed") refreshRunning();
    else if (event === "state" && payload && ("running" in payload || "foreground" in payload)) {
      refreshRunning();
      if ("foreground" in payload) onForeground(payload.foreground);
    }
    // Das Control Center öffnet einen Bildschirm im Home (ui.open, z. B. „Installieren“ eines
    // Downloads): Der Kern holt das Home zuerst nach vorn, der Rücksprung zur App liefe danach und
    // nähme den Bildschirm wieder weg (Wunsch 2026-10-21: statt der Prüfung stand YouTube da)
    else if (event === "ui.open") clearTimeout(returnTimer);
  }

  const unsubJobs = store.subscribe(["jobs"], () => {
    const hit = rows.games.patch([installTile()]);
    if (hit.length && row().current && row().current.id === INSTALL_TILE.id) refreshEntry(row().current);
  });
  function paintNote() {
    const job = store.state.art_job;
    const failed = state.failed > 0;
    note.hidden = !job && !failed;
    note.classList.toggle("is-warn", failed);
    setIcon(note.firstChild, failed ? "warn" : "download");
    if (failed) note.lastChild.textContent = S.homeFailed;
    else if (job) note.lastChild.textContent = fmt(S.homeArtJob, { done: job.done || 0, total: job.total || 0 });
  }
  const unsubArt = store.subscribe(["art_job"], paintNote);
  paintNote();

  load();
  storeArt();
  storeCards();
  return {
    el, onPad, onEvent,
    // Zurück (z. B. aus den Spieleinstellungen): Karten und Marken des Hub frisch holen
    // Antwortete die Bibliothek zuletzt nicht, fragt die Rückkehr gleich wieder (E99)
    onShow(info) {
      backdrop.setActive(true);
      // Die Reihen kamen, während ein anderer Bildschirm oben lag: Fokus auf die Kachel statt auf den Reiter
      if (state.focusPending && state.loaded && row().current) { state.focusPending = false; row().focusCurrent(); }
      nav.ensure();
      if (info && info.back) loadDetail(row().current, 0);
      if (info && info.back && state.failed && state.failCode !== "Unavailable") load();
    },
    onHide() { backdrop.setActive(false); },
    destroy() { status.destroy(); backdrop.destroy(); unsubJobs(); unsubArt(); clearTimeout(retryTimer); clearTimeout(artTimer); clearTimeout(returnTimer); clearTimeout(cardsTimer); },
  };
}

export default { id: "home", create };
