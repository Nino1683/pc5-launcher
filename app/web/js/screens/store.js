// Store (Bauplan E68): Spiele aus dem Steam-Shop, von GOG und Epic (E73) finden, kaufen und installieren,
// ohne eine Store-App zu sehen. Oben der Shop (Steam | GOG | Epic), darunter seine Reiter (Listen des Shops und
// Suche); Raster aus Kacheln im Querformat mit Preis, Rabatt und Marke (in deiner Bibliothek,
// installiert, wird geladen). L1/R1 Reiter, L2/R2 der Shop (Wunsch 2026-10-21; Quadrat geht weiter
// reihum), Kreuz öffnet die Seite des
// Spiels, Dreieck sucht, Options wählt das Genre (E70). Die Listen kommen seitenweise (store.list)
// und laden beim Rollen nach. Angemeldet wird im Browser des PC5 Launchers (Knopf oben rechts).
// Kacheln bleiben beim Nachladen und bei neuen Marken stehen (nur Neues kommt hinzu), Bilder laden
// träge um den Sichtbereich (E99). Kommt eine Seite nicht, steht am Ende „Erneut versuchen“.
// itch.io (E100) hinten als siebter Shop: Ohne eigene App erklärt „Anmelden“ zuerst die Einrichtung,
// ohne Suche über die Schnittstelle sucht itch.io im Browser, Cover stehen ganz in der Kachel.
import { h, icon, gridTop } from "../core/dom.js";
import { call } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { router } from "../core/router.js";
import { sound } from "../core/sound.js";
import { S, fmt } from "../core/strings.js";
import { toast } from "../components/toast.js";
import { openMenu } from "../components/menu.js";
import { askText } from "../components/osk-input.js";
import { openStoreUrl, storeLogin, STORE_NAMES } from "./store-item.js";

const COLS = 4;
const ROWS_SHOWN = 3;             // zwei volle Reihen und der Ansatz der dritten
const ROWS_AHEAD = 2;             // so viele Reihen dahinter (und eine davor) bekommen ihr Bild schon
// Battle.net, Ubisoft, Rockstar (E94): installiert und gestartet wird über ihren Launcher, verborgen
// Neue Shops kommen hinten dazu: L2/R2 und Quadrat zählen die Stellen (itch.io, E100)
const STORES = ["steam", "gog", "epic", "battlenet", "ubisoft", "rockstar", "itch"];
// „Meine Spiele“ vorn (E69: was dir gehört, zum Installieren); geöffnet wird mit dem zweiten Reiter
const TABS = { steam: ["owned", "specials", "top_sellers", "new_releases", "coming_soon", "search"],
  gog: ["owned", "gog_bestsellers", "gog_specials", "gog_new", "search"],
  epic: ["owned", "epic_top", "epic_free", "epic_specials", "epic_new", "epic_wished", "search"],
  // Vorgaben; die Reiter dieser Shops nennt der Kern (store.front → tabs), sie gelten dann
  battlenet: ["owned", "bnet_games", "search"], ubisoft: ["owned", "ubi_best", "ubi_specials", "ubi_new", "search"],
  rockstar: ["owned", "rs_games", "search"],
  itch: ["owned", "itch_popular", "itch_new", "itch_rated", "itch_free", "itch_sale", "search"] };
const TAB_LABELS = {};               // Beschriftungen, die der Kern mitliefert
const TAB_TEXT = { ...S.stTabs, ...S.stItchTabs };
const LOGIN_HINTS = { steam: "stLoginHint", gog: "stLoginHintGog", epic: "stLoginHintEpic",
  battlenet: "stLoginHintLauncher", ubisoft: "stLoginHintLauncher", rockstar: "stLoginHintLauncher", itch: "stItchLoginHint" };
// Der zuletzt gewählte Shop gilt, bis das Home neu startet
const prefs = { store: "steam" };

// Kachel eines Spiels (auch für andere Bildschirme). lazy: Das Bild kommt erst mit revealCard.
// itch.io (E100): Cover im Format 315x250 statt eines breiten Kopfbilds. Ganz gezeigt (is-fit) auf
// einer unscharfen Fassung desselben Bilds, statt oben und unten stark beschnitten.
export function storeCard(c, { lazy = false } = {}) {
  const node = h("div.st-card" + (c.store === "itch" ? ".is-fit" : ""), { "data-focus": true, dataset: { id: c.id } });
  const img = h("img.st-pic", { alt: "", decoding: "async" });
  img.addEventListener("error", () => { if (img.getAttribute("src")) node.classList.add("is-noimg"); });
  img.addEventListener("load", () => node.classList.remove("is-noimg"));
  const blur = c.store === "itch" ? h("img.st-blur", { alt: "", decoding: "async" }) : null;
  node.append(h("div.st-img", blur, img, h("span.st-ph")), h("span.st-flag", { hidden: true }), h("div.st-name.ell"), h("div.st-price-row"));
  paintCard(node, c, lazy);
  return node;
}

// Neuer Stand derselben Kachel (Marke, Preis): Der Knoten bleibt, das Bild nur bei neuer Adresse
function paintCard(node, c, lazy = false) {
  const old = node.card;
  node.card = c;
  if (!old || old.image !== c.image) {
    const lazyNow = lazy && !node.querySelector(".st-img > img.st-pic").getAttribute("src");
    node.classList.toggle("is-noimg", !c.image);
    for (const img of node.querySelectorAll(".st-img > img")) {
      if (!c.image) { img.removeAttribute("src"); delete img.dataset.src; }
      else if (lazyNow) img.dataset.src = c.image;
      else { delete img.dataset.src; img.src = c.image; }
    }
  }
  if (!old || old.title !== c.title) {
    node.querySelector(".st-ph").textContent = c.title;
    node.querySelector(".st-name").textContent = c.title;
  }
  const kind = c.installed ? "installed" : c.downloading ? "loading" : c.owned ? "owned" : "";
  const flag = node.querySelector(".st-flag");
  flag.hidden = !kind;
  flag.textContent = kind === "installed" ? S.stInstalled : kind === "loading" ? S.stDownloading : kind ? S.stOwned : "";
  if (kind) flag.dataset.kind = kind; else delete flag.dataset.kind;
  node.querySelector(".st-price-row").replaceChildren(...(c.discount
    ? [h("span.st-disc", { text: "−" + c.discount + " %" }), h("span.st-old", { text: c.original }), h("span.st-price", { text: c.price })]
    : [h("span.st-price", { text: c.price })]));
}

function revealCard(node) {
  for (const img of node ? node.querySelectorAll(".st-img > img[data-src]") : []) { img.src = img.dataset.src; delete img.dataset.src; }
}

function create(params = {}) {
  const state = { store: params.store || prefs.store, tab: "", genre: "", genres: [], sections: {}, lists: new Map(),
    query: "", top: 0, loggedIn: false, failed: "", wantGrid: false,
    // itch.io (E100): eigene App angelegt? Sucht itch.io nur im Browser („browser“ statt „api“)?
    configured: true, searchWeb: false };
  state.tab = params.tab || TABS[state.store][1];
  const tabs = h("div.lib-tabs", { "data-focus-group": true });
  let tabEls = [];
  // Beschriftung in einem eigenen Kasten: Der Fokusring ragt über den Reiter hinaus
  function buildTabs() {
    tabEls = TABS[state.store].map((id) => h("div.lib-tab", { "data-focus": true, dataset: { tab: id } }, h("span", { text: TAB_TEXT[id] || TAB_LABELS[id] || id })));
    tabs.replaceChildren(...tabEls);
  }
  buildTabs();
  const storeEls = STORES.map((id) => h("div.st-store", { "data-focus": true, dataset: { store: id }, text: STORE_NAMES[id] }));
  const storeBox = h("div.st-stores", { "data-focus-group": true }, storeEls);
  const genreBtn = h("div.btn.is-secondary.st-genre", { "data-focus": true, "data-nav-down": ".st-card[data-focus-default]" },
    icon("sort"), h("span.btn-label", { text: S.stGenreShort }));
  const account = h("div.btn.is-secondary.st-account", { "data-focus": true, "data-nav-down": ".st-card[data-focus-default]" },
    icon("user"), h("span.btn-label", { text: S.stLogin }));
  const headBtns = h("div.st-head-btns", { "data-focus-group": true }, genreBtn, account);
  // Shops und Knöpfe in einer Zeile: Bei sieben Shops (E100) wird das Genre gekürzt, statt dass sich
  // Knöpfe und Shops überdecken
  const headRow = h("div.st-top", storeBox, headBtns);
  const count = h("span.lib-count.ell");
  // data-scroll: Der rechte Stick rollt das Raster Reihe für Reihe (core/nav.js, E99)
  const grid = h("div.st-grid", { "data-scroll": true });
  let nodes = new Map();          // Shop:Kennung -> Kachel der gezeigten Liste
  // Am Ende einer Liste, deren nächste Seite nicht kam: Kreuz fragt noch einmal (E99)
  const retryWhy = h("div.st-retry-why.ell");
  const retryCard = h("div.st-card.st-retry", { "data-focus": true },
    h("div.st-img", icon("restart")), h("div.st-name.ell", { text: S.retry }), retryWhy);
  // Die Karte hat keine Kennung; steht der Fokus auf ihr, merken sich render und fetchPage das so
  const RETRY = Symbol("retry");
  const view = h("div.st-view", { "data-focus-group": true }, grid);
  const empty = h("div.lib-empty", { hidden: true });
  const el = h("div.library.store",
    h("div.lib-icon", icon("store")),
    h("div.lib-title", { text: S.stTitle }), headRow,
    tabs,
    h("div.lib-head", count),
    view, empty);
  let serial = 0;
  let jobIds = "";                // welche Downloads laufen (nur ein Wechsel lädt die Marken neu)

  // Listen (E70): je Reiter, Genre und Suchbegriff eigene Seiten, nachgeladen beim Rollen
  function listKey(tab = state.tab) { return [state.store, tab, state.genre, tab === "search" ? state.query : ""].join("|"); }
  function list(tab = state.tab) {
    const key = listKey(tab);
    if (!state.lists.has(key)) state.lists.set(key, { items: [], page: -1, more: true, total: 0, loading: false, failed: "" });
    return state.lists.get(key);
  }

  function cards() {
    if (state.tab === "owned") return (state.sections.owned || {}).items || [];
    return list().items;
  }

  function genreLabel() {
    const g = state.genres.find((x) => x.id === state.genre);
    return g ? g.label : "";
  }

  // Nur ändern, was anders ist: Bleiben die ersten Kacheln dieselben (Nachladen, neue Marken), bleiben
  // sie im Dokument stehen, und der Fokusring blitzt nicht neu auf (E99)
  function syncGrid(all) {
    const now = grid.children;
    let i = 0;
    while (i < now.length && i < all.length && now[i] === all[i]) i += 1;
    while (now.length > i) now[now.length - 1].remove();
    grid.append(...all.slice(i));
  }

  function render(keepId) {
    const items = cards();
    const fresh = new Map();
    const cardNodes = items.map((c) => {
      const id = state.store + ":" + c.id;
      let node = nodes.get(id);
      if (node) paintCard(node, c, true); else node = storeCard(c, { lazy: true });
      fresh.set(id, node);
      return node;
    });
    nodes = fresh;
    const l = state.tab === "owned" ? null : list();
    // Auch während des Neuversuchs bleibt die Karte stehen („Lädt …“): Fiele sie bei einem neuen
    // Stand dazwischen weg, spränge der Fokus an den Anfang der Liste (E99)
    const failed = l ? l.failed : "";
    const busy = !!(failed && l.loading);
    retryCard.classList.toggle("is-busy", busy);
    retryWhy.textContent = busy ? S.stLoading : failed;
    const all = failed ? [...cardNodes, retryCard] : cardNodes;
    const retryAt = [...grid.children].indexOf(retryCard);
    syncGrid(all);
    for (const c of grid.querySelectorAll("[data-focus-default]")) c.removeAttribute("data-focus-default");
    all.forEach((n, i) => { if (i < COLS) n.setAttribute("data-nav-up", ".lib-tab.is-active"); else n.removeAttribute("data-nav-up"); });
    // Fokus auf der Karte: Sie selbst, solange die Seite fehlt; kam sie, die Stelle der Karte (die erste neue Kachel)
    const keep = (keepId === RETRY ? (failed ? retryCard : cardNodes[retryAt] || cardNodes[cardNodes.length - 1])
      : all.find((n) => n.dataset.id === keepId)) || all[0];
    if (keep) keep.setAttribute("data-focus-default", "");
    for (const t of tabEls) {
      t.classList.toggle("is-active", t.dataset.tab === state.tab);
      if (all.length) t.setAttribute("data-nav-down", ".st-card[data-focus-default]"); else t.removeAttribute("data-nav-down");
      t.setAttribute("data-nav-up", ".st-store.is-active");
    }
    const name = STORE_NAMES[state.store];
    account.querySelector(".btn-label").textContent = state.loggedIn ? S.stLoggedInShort : S.stLoginShort;
    account.dataset.store = name;
    account.classList.toggle("is-in", state.loggedIn);
    genreBtn.querySelector(".btn-label").textContent = state.genre ? genreLabel() : S.stGenreShort;
    genreBtn.classList.toggle("is-in", !state.genre);
    genreBtn.hidden = state.tab === "owned" || !state.genres.length;
    for (const b of storeEls) b.classList.toggle("is-active", b.dataset.store === state.store);
    const parts = [];
    if (state.tab === "search" && state.query) parts.push(fmt(S.libQuery, { q: state.query }));
    if (state.genre && state.tab !== "owned") parts.push(genreLabel());
    const total = l ? l.total : items.length;
    if (total) parts.push(fmt(S.stCount, { n: total.toLocaleString(S.locale) }));
    count.textContent = parts.join(" · ");
    empty.hidden = all.length > 0;
    // Sucht itch.io im Browser (E100), sagt die leere Liste das statt „Nichts gefunden“
    const web = state.tab === "search" && (state.searchWeb || (l && l.web));
    empty.textContent = (state.failed && state.tab !== "owned" ? state.failed : "")
      || (web ? (state.query ? S.stItchSearchWeb : S.stItchSearchEmpty) : "")
      || (state.tab === "search" ? (state.query ? fmt(S.stSearchNone, { q: state.query }) : S.stSearchEmpty)
        : state.tab === "owned" ? (state.loggedIn ? S.stOwnedNone : S.stOwnedEmpty)
          : l && !l.loading && l.page >= 0 ? S.stListEmpty : S.stLoading);
    scrollTo(keep);
    return keep;
  }

  function scrollTo(node) {
    state.top = gridTop(node && grid.contains(node) ? [...grid.children].indexOf(node) : -1, COLS, state.top);
    grid.style.setProperty("--top", state.top);
    reveal();
  }

  // Bilder nur um den Sichtbereich: eine Reihe davor, die sichtbaren und zwei dahinter
  function reveal() {
    const kids = grid.children;
    const last = Math.min(kids.length, (state.top + ROWS_SHOWN + ROWS_AHEAD) * COLS);
    for (let i = Math.max(0, state.top - 1) * COLS; i < last; i += 1) revealCard(kids[i]);
  }

  // Liegt eine andere Seite darüber (die Seite eines Spiels), holt onShow({back}) die Marken nach
  function isTop() { return !!(router.top && router.top.inst && router.top.inst.el === el); }

  function focusedId() {
    if (!nav.current || !grid.contains(nav.current)) return null;
    return nav.current === retryCard ? RETRY : nav.current.dataset.id;
  }

  // Nächste Seite des aktuellen Reiters; der Fokus bleibt auf seiner Kachel.
  // Nach einem Fehler fragt erst ein ausdrücklicher Neuversuch (retry: Karte „Erneut versuchen“,
  // Kreuz auf dem aktiven Reiter) oder ein neuer Stand (load) wieder. Sonst löste jede Bewegung in
  // den letzten Reihen erneut einen Aufruf mit Zeitablauf aus (E99); vorher ging gar nichts mehr.
  async function fetchPage(tab = state.tab, { retry = false } = {}) {
    if (tab === "owned" || (tab === "search" && (!state.query || state.searchWeb))) return;
    const l = list(tab);
    if (l.loading || !l.more || (l.failed && !retry)) return;
    l.loading = true;
    const key = listKey(tab);
    if (retry) { retryCard.classList.add("is-busy"); retryWhy.textContent = S.stLoading; }
    let r;
    try {
      r = await call("store.list", { store: state.store, tab, page: l.page + 1, genre: state.genre, query: tab === "search" ? state.query : "" });
    } catch (e) { r = { ok: false, message: (e && e.message) || S.stFailed }; }
    l.loading = false;
    // Keine Suche über die Schnittstelle (itch.io, E100): kein Fehler, die Suche geht in den Browser
    // (einmal je Begriff; steht der Store noch auf dieser Suche, öffnet sich der Browser gleich)
    if (r && r.ok === false && r.web === "search") {
      Object.assign(l, { failed: "", more: false, page: 0, web: true });
      state.searchWeb = true;
      if (listKey() === key) webSearch();
    } else if (!r || r.ok === false) l.failed = (r && r.message) || S.stFailed;
    else {
      l.failed = "";
      const seen = new Set(l.items.map((c) => c.id));
      l.items.push(...(r.items || []).filter((c) => !seen.has(c.id)));
      l.page += 1;
      l.more = !!r.more;
      l.total = Number(r.total) || l.items.length;
    }
    if (listKey() !== key) return;            // inzwischen anderer Reiter, Genre oder Shop
    const keep = focusedId();
    const node = render(keep);
    // In die Liste, wenn sie gewollt war (Öffnen, L1/R1) und niemand den Fokus inzwischen bewegt hat.
    // Vom Neuversuch aus gibt render die erste neue Kachel (oder wieder „Erneut versuchen“); kam eine leere Liste, der Reiter
    if ((keep || state.wantGrid) && node) nav.focus(node);
    else if (keep === RETRY) nav.focus(tabEls.find((t) => t.dataset.tab === state.tab));
    if (node) state.wantGrid = false;
    nav.ensure();
  }

  // Marken (gehört dir, installiert, wird geladen) neu: geladene Seiten noch einmal holen (der Kern
  // hat sie im Zwischenspeicher)
  async function refreshMarks() {
    const l = state.tab === "owned" ? null : list();
    if (!l || l.page < 0 || l.web) return;
    const pages = l.page + 1;
    const fresh = [];
    for (let p = 0; p < pages; p += 1) {
      try {
        const r = await call("store.list", { store: state.store, tab: state.tab, page: p, genre: state.genre, query: state.tab === "search" ? state.query : "" });
        fresh.push(...((r && r.items) || []));
      } catch (e) { return; }
    }
    const byId = new Map(fresh.map((c) => [c.id, c]));
    l.items = l.items.map((c) => byId.get(c.id) || c);
    const keep = focusedId();
    const node = render(keep);
    if (keep && node) nav.focus(node);
  }

  async function load({ focusGrid = false, keepId = null, marks = false } = {}) {
    const mine = ++serial;
    state.failed = "";
    try {
      const r = await call("store.front", { store: state.store });
      if (mine !== serial) return;
      if (!r || r.ok === false) state.failed = (r && r.message) || S.stFailed;
      state.sections = {};
      for (const sec of (r && r.sections) || []) state.sections[sec.id] = sec;
      state.loggedIn = !!(r && r.loggedIn);
      state.genres = (r && r.genres) || [];
      state.configured = !(r && r.configured === false);
      state.searchWeb = !!(r && r.search === "browser");
      // Reiter vom Kern (E94): „Meine Spiele“ vorn, Suche hinten, dazwischen die Listen des Shops
      if (r && Array.isArray(r.tabs) && r.tabs.length) {
        for (const [id, label] of r.tabs) TAB_LABELS[id] = label;
        const want = ["owned", ...r.tabs.map((t) => t[0]).filter((id) => id !== "owned" && id !== "search"), "search"];
        if (want.join() !== TABS[state.store].join()) {
          TABS[state.store] = want;
          if (!want.includes(state.tab)) state.tab = want[1];
          buildTabs();
        }
      }
    } catch (e) {
      if (mine !== serial) return;
      state.failed = (e && e.message) || S.stFailed;
    }
    const l = state.tab === "owned" ? null : list();
    const inGrid = nav.current && grid.contains(nav.current);
    const keep = render(keepId || focusedId());
    if ((focusGrid || inGrid) && keep) nav.focus(keep);
    else if (focusGrid) state.wantGrid = true;
    nav.ensure();
    // Ein neuer Stand ist auch ein Anlass, eine gescheiterte Seite noch einmal zu holen (E99). Die
    // Karte „Erneut versuchen“ bleibt dabei stehen und mit ihr der Fokus; vorher fiel sie weg, der
    // Fokus sprang an den Anfang, und die fehlende Seite holte erst das nächste Rollen ans Ende.
    if (l && l.failed) fetchPage(state.tab, { retry: true });
    // Ohne geladene Seite die erste holen; Marken gibt es nur auf geladenen Seiten nachzuziehen
    else if (l && l.page < 0) fetchPage();
    if (marks && l && l.page >= 0) refreshMarks();
  }

  async function search() {
    let text;
    try { text = await askText({ title: S.stSearchTitle, initial: state.query }); } catch (e) { return; }
    if (text === null || !text.trim()) return;
    state.query = text.trim();
    if (state.tab !== "search") setTab("search", false, false);
    render(null);
    // itch.io ohne Suche über die Schnittstelle (E100): Gesucht wird auf itch.io im Browser. Weiß der
    // Store das noch nicht, sagt es die erste Seite (fetchPage öffnet dann den Browser).
    if (state.searchWeb) { nav.focus(tabEls.find((t) => t.dataset.tab === "search")); await webSearch(); return; }
    // Das Ergebnis bekommt den Fokus, sobald es da ist: Hat setTab die erste Seite schon angefordert,
    // kehrt der Aufruf unten sofort zurück (Seite lädt noch), und erst dieser erste Abruf fokussiert
    // (vorher blieb der Fokus dann auf dem Reiter „Suche“)
    state.wantGrid = true;
    // retry: Derselbe Begriff nach einem Fehler fragt neu (ohne Fehler wirkt es nicht anders)
    await fetchPage("search", { retry: true });
    const first = grid.firstElementChild;
    if (first) nav.focus(first); else nav.focus(tabEls.find((t) => t.dataset.tab === "search"));
  }

  function webSearch() {
    return openStoreUrl("store.web", { store: state.store, what: "search", query: state.query });
  }

  function setTab(id, focusGrid, ask = true) {
    if (id === state.tab) {
      // Kreuz auf dem aktiven Reiter einer Liste, deren Seite nicht kam: noch einmal fragen (E99)
      const l = id === "owned" ? null : list();
      if (l && l.failed) fetchPage(id, { retry: true });
      return;
    }
    state.tab = id;
    sound.play("tab");
    const active = tabEls.find((t) => t.dataset.tab === id);
    if (nav.current && tabs.contains(nav.current) && active) nav.focus(active);
    const keep = render(null);
    if (focusGrid && keep) nav.focus(keep);
    else if (focusGrid) { nav.focus(active); state.wantGrid = true; }
    if (id === "search" && !state.query && ask) { search(); return; }
    if (list().page < 0) fetchPage();
  }

  async function chooseGenre() {
    if (state.tab === "owned") return;
    const items = [{ id: "all", label: S.stGenreAll, icon: state.genre ? "none" : "check" },
      ...state.genres.map((g) => ({ id: g.id, label: g.label, icon: g.id === state.genre ? "check" : "none" }))];
    const choice = await openMenu(items, { cls: "menu-center", title: S.stGenre, initial: state.genre || "all" });
    if (!choice) return;
    const genre = choice === "all" ? "" : choice;
    if (genre === state.genre) return;
    state.genre = genre;
    state.wantGrid = true;
    const keep = render(null);
    nav.focus(keep || genreBtn);
    if (list().page < 0) await fetchPage();
  }

  async function login() {
    // itch.io ohne eigene App (E100): erst die Erklärung, dann die Seite zum Anlegen (store-item.js)
    if (state.store === "itch" && !state.configured) { await storeLogin(state.store, { setup: true }); return; }
    const r = await openStoreUrl("store.login", { store: state.store });
    if (r && r.ok) toast({ title: fmt(S.stLoginStore, { store: STORE_NAMES[state.store] }),
      body: fmt(S[LOGIN_HINTS[state.store]], { store: STORE_NAMES[state.store] }), icon: "info" });
  }

  function setStore(id) {
    if (id === state.store) return;
    state.store = prefs.store = id;
    state.tab = TABS[id][1];
    state.sections = {};
    state.query = "";
    state.genre = "";
    state.loggedIn = false;
    state.configured = true;
    state.searchWeb = false;
    sound.play("tab");
    buildTabs();
    render(null);
    load({ focusGrid: !(nav.current && storeBox.contains(nav.current)) });
  }

  account.addEventListener("press", () => { if (!state.loggedIn) login(); else load({ marks: true }); });
  genreBtn.addEventListener("press", chooseGenre);
  tabs.addEventListener("press", (e) => {
    const t = e.target.closest(".lib-tab");
    if (!t) return;
    if (t.dataset.tab === "search" && state.tab === "search") search(); else setTab(t.dataset.tab, false);
  });
  grid.addEventListener("nav-focus", (e) => {
    for (const c of grid.querySelectorAll("[data-focus-default]")) c.removeAttribute("data-focus-default");
    e.target.setAttribute("data-focus-default", "");
    scrollTo(e.target);
    // Kurz vor dem Ende der geladenen Spiele die nächste Seite holen
    const at = [...grid.children].indexOf(e.target);
    if (at >= grid.children.length - 3 * COLS) fetchPage();
  });
  // Bewegt jemand den Fokus selbst, springt er nicht mehr in die nachgeladene Liste
  el.addEventListener("nav-move", () => { state.wantGrid = false; });
  grid.addEventListener("press", (e) => {
    const node = e.target.closest(".st-card");
    // configured: Die Seite eines itch.io-Spiels weiß sonst nicht, ob „Anmelden“ erst einrichtet (E100)
    if (node && node.card) router.go("store-item", { store: state.store, id: node.card.id, card: node.card, configured: state.configured });
  });
  retryCard.addEventListener("press", () => fetchPage(state.tab, { retry: true }));

  storeBox.addEventListener("press", (e) => { const b = e.target.closest(".st-store"); if (b) setStore(b.dataset.store); });

  function onPad(button, ev) {
    if (ev.repeat) return false;
    const tabsList = TABS[state.store];
    const index = tabsList.indexOf(state.tab);
    switch (button) {
      case "l1": if (index > 0) setTab(tabsList[index - 1], true); else sound.play("edge"); return true;
      case "r1": if (index < tabsList.length - 1) setTab(tabsList[index + 1], true); else sound.play("edge"); return true;
      case "l2": case "r2": {
        const at = STORES.indexOf(state.store) + (button === "r2" ? 1 : -1);
        if (at >= 0 && at < STORES.length) setStore(STORES[at]); else sound.play("edge");
        return true;
      }
      case "triangle": search(); return true;
      case "square": setStore(STORES[(STORES.indexOf(state.store) + 1) % STORES.length]); return true;
      case "options": chooseGenre(); return true;
      default: return false;
    }
  }

  function onEvent(event, payload) {
    // Neuer Stand (Download fertig, Kauf, Anmeldung): Marken auf den Kacheln nachziehen. Zurück
    // aus dem Browser (Anmeldung, Kauf: nichts mehr vorn) heißt ebenfalls neu fragen.
    // Neue Bilder der Bibliothek (reason "art", nach einer Installation alle 1–2 s) ändern keine Marke.
    // Nur, solange der Store oben liegt: Darunter (Seite eines Spiels) holt onShow({back}) das nach,
    // statt bei jeder Rückkehr ins Home alle geladenen Seiten neu zu fragen (E99)
    let fresh = (event === "lib.changed" && !(payload && payload.reason === "art")) || event === "store.owned"
      || (event === "state" && payload && "foreground" in payload && !payload.foreground);
    if (event === "state" && payload && Array.isArray(payload.store_jobs)) {
      // Proton-Aufträge (E101/E102) stehen in derselben Liste, ändern aber keine Marke im Store
      const ids = payload.store_jobs.filter((j) => j.store !== "proton").map((j) => j.id).join(",");
      if (ids !== jobIds) { jobIds = ids; fresh = true; }
    }
    if (fresh && isTop()) load({ marks: true });
  }

  load({ focusGrid: true });
  return {
    el, onPad, onEvent,
    // Zurück aus dem Browser (Anmeldung, Kauf): neu fragen, was dir gehört
    onShow(info) { nav.ensure(); if (info && info.back) load({ marks: true }); },
  };
}

export default { id: "store", create };
