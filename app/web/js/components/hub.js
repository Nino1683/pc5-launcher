// Hub eines Eintrags (07 Abschnitt 2.4): Logo oder Titel, Unterzeile, Hinweis, Hauptknopf, „…“,
// Info-Marken rechts, Karten. Im Home ist er Vorschau unter der Reihe und rückt nach oben,
// sobald der Fokus in ihn wandert; die Bibliothek zeigt ihn als eigenen Bildschirm.
// E123: darunter Abschnitte („Über das Spiel“, „Neuigkeiten“; hub-sections.js, weitere über
// hub.sections.add) und dahinter der Steam-Trailer samt Spielmusik (trailer.js). Beide kennen nur den Eintrag
// des Hub. Unter Logo bzw. Titel steht die Kurzbeschreibung von Steam in einer Zeile; die Karten
// füllen lib.get und weitere Quellen (addCardSource).
// E125: Die Karten sind nützliche (hub-cards.js: Trophäen, Bildschirmfotos, Neuigkeiten, Über das Spiel), bis zu vier,
// anwählbar, mit Bild bzw. Fortschrittslinie; ganz ohne sie bleiben die technischen aus lib.get („…“ › Informationen
// zeigt sie immer).
// E127: Lage und Größe des Logos je Spiel (Option logo), angepasst über entry-actions.adjustLogo mit Live-Vorschau.
import { h, icon, clear, decoded } from "../core/dom.js";
import { nav } from "../core/nav.js";
import { S, fmt, playtimeText, whenText } from "../core/strings.js";
import { store } from "../core/store.js";
import { artBox, updateArt, badge, marks, stateOf } from "./art.js";
import { mainLabel, canStart, isRunning } from "./entry-actions.js";
import { createHubSections, showAbout } from "./hub-sections.js";
import { gameInfo, wantsInfo } from "./gameinfo.js";
import { createTrailer } from "./trailer.js";
import { CARD_SOURCES } from "./hub-cards.js";

function subLine(entry) {
  if (entry.system || entry.kind === "app") return entry.note || "";
  if (!entry.last_played) return S.neverPlayed;
  const time = playtimeText(entry.playtime);
  return time ? fmt(S.lastPlayedTime, { when: whenText(entry.last_played), time }) : fmt(S.lastPlayed, { when: whenText(entry.last_played) });
}

function chip(symbol, label, value, cls) {
  return h("div.chip" + (cls ? "." + cls : ""), icon(symbol), h("span", { text: label }), value ? h("b", { text: value }) : null);
}

// Info-Marken rechts: VR, HDR, FSR 4, Spielzeit, diese Woche (E99: Sekunden der laufenden Woche
// aus lib.get week). globals: Schalter für alle Spiele (E41)
// Zustände („Läuft“, „Nicht installiert“, „Nicht verfügbar“) stehen schon als Marke am Titel und
// auf dem Hauptknopf; eine dritte Angabe hier sagte nichts Neues (Befund Kodi: viermal dasselbe Wort)
function chips(entry, globals, week) {
  if (entry.system || entry.kind !== "game") return [];
  const tags = entry.tags || [];
  const opts = entry.options || {};
  const out = [];
  if (tags.includes("vr")) out.push(chip("eye", S.markVr));
  if (tags.includes("hdr")) out.push(chip("hdr", S.chipHdr, opts.hdr === "off" || (globals && globals.hdr === false) || !store.state.hdr.enabled ? S.off : S.on));
  if (tags.includes("fsr4") || tags.includes("fsr4-upgrade")) {
    const off = opts.fsr4 === "off" || (globals && globals.fsr4 === false);
    out.push(chip("speed", S.chipFsr, off ? S.off : tags.includes("fsr4") ? S.chipFsrAvailable : S.chipFsrUpgrade));
  }
  const time = playtimeText(entry.playtime);
  if (time) out.push(chip("clock", S.played, time));
  const thisWeek = playtimeText(week);
  if (thisWeek) out.push(chip("play", S.hubWeek, thisWeek));
  return out;
}

// Nebenknopf neben dem Hauptknopf (07 2.4, 11.2, 11.3): läuft das Spiel, „Spiel schließen“; bei der
// App „Desktop“ der Wechsel der ganzen Sitzung in den Desktop-Modus. Sonst keiner.
function secondOf(entry) {
  if (!entry || entry.system) return null;
  if (isRunning(entry) && !store.isClosing(entry.id)) return { kind: "close", label: entry.kind === "game" ? S.menuCloseGame : S.menuClose };
  if (entry.id === "app:desktop") return { kind: "desktop-mode", label: S.hubDesktopMode };
  return null;
}

// handlers: onMain(entry), onMore(entry), onSecond(entry, kind) mit kind "close" | "desktop-mode"
const STORE_ID = "sys:store";
// Kurzbeschreibung und weitere Karten erst, wenn die Auswahl so lange ruht (Home: schnelles Blättern fragt nichts)
const REST_MS = 800;
const MAX_CARDS = 3;
const MAX_USEFUL = 4;             // E125: vier nützliche Karten passen schmaler in dieselbe Breite (--card-w-4)
const CARD_WAIT_MS = 3000;        // so lange warten die technischen Karten auf die nützlichen (Netz, Kern)

export function createHub(handlers = {}) {
  const logo = h("div.hub-logo");
  // E123: eine Zeile Kurzbeschreibung (Steam short_description), nur mit Steam-Angaben
  const tagline = h("div.hub-tagline.ell", { hidden: true });
  const sub = h("div.hub-sub.ell");
  const note = h("div.hub-note.ell", { hidden: true });
  const mainLabelEl = h("span.btn-label");
  const main = h("div.btn.hub-main", { "data-focus": true }, mainLabelEl);
  const secondLabel = h("span.btn-label");
  const second = h("div.btn.is-secondary.hub-second", { "data-focus": true, hidden: true }, secondLabel);
  // Rechts vom „…“ ist Schluss (Randton): Nach Lage läge sonst das Zahnrad oben rechts „rechts“. Gibt es eine lange
  // Beschreibung (Steam-Angaben, hub-sections.js), folgt noch „Beschreibung“, dann ist dort Schluss
  const more = h("div.btn.is-secondary.is-round.hub-more", { "data-focus": true, "data-nav-right": "none" }, icon("more"));
  const about = h("div.btn.is-secondary.hub-about-btn", { "data-focus": true, "data-nav-right": "none", hidden: true },
    icon("info"), h("span.btn-label", { text: S.stMore }));
  const actions = h("div.hub-actions", main, second, more, about);
  const chipBox = h("div.hub-chips");
  const cards = h("div.hub-cards");
  // Store-Kachel (Wunsch 2026-10-21: „mehr Infos als nur Öffnen“): anwählbare Karten mit Bild
  // (Gratis bei Epic, Angebote) statt der Infokarten; Kreuz öffnet die Seite des Spiels
  const storeBox = h("div.hub-cards.hub-store", { hidden: true });
  let storeCards = [];
  const el = h("div.hub", { "data-focus-group": true }, logo, tagline, sub, note, actions, chipBox, cards, storeBox);
  // E123: Abschnitte unter den Karten (geladen, sobald der Hub offen ist) und der Trailer dahinter
  let aboutSteam = null;
  const sections = createHubSections(el, { onAbout: (steam) => {
    aboutSteam = steam;
    about.hidden = !steam;
    if (steam) more.removeAttribute("data-nav-right"); else more.setAttribute("data-nav-right", "none");
    requestAnimationFrame(fitChips);  // kommt nach den Marken: vordere treten zurück, statt verdeckt zu werden
  } });
  el.append(sections.el);
  const trailer = createTrailer(el);
  el.addEventListener("nav-focus", () => sections.open());
  // Kopf des Eintrags, wenn der Hub offen ist: Mini-Bild, Marke, Titel (07 Zustand B)
  const miniArt = h("div.mini-art");
  const miniTitle = h("div.mini-title");
  const mini = h("div.mini", miniArt, miniTitle);
  let entry = null;
  let detail = null;
  let logoSrc = null;
  let restTimer = null;
  let rested = "";                // Eintrag, für den Kurzbeschreibung und Karten-Quellen schon gefragt sind
  // Karten aus weiteren Quellen (für den Umbau der Hub-Karten nach E123): fn(entry) -> Promise<[{label, value,
  // order?}]> oder Liste. Sortiert wird nach order (die Karten aus lib.get haben 50, 51, 52; ohne order 100), es
  // bleiben MAX_CARDS. Gefragt wird einmal je Eintrag nach der Ruhezeit (Spielseite: sofort, open()).
  const cardSources = [...CARD_SOURCES];
  let extraCards = [];
  let cardsPending = 0;           // E125: Quellen des Eintrags, die noch nicht geantwortet haben
  let cardsWaited = false;        // CARD_WAIT_MS sind um: dann gilt, was da ist
  let cardsTimer = null;
  let cardSerial = 0;             // je Wechsel des Eintrags neu: späte Antworten eines früheren Besuchs zählen nicht

  main.addEventListener("press", () => { if (entry && handlers.onMain) handlers.onMain(entry); });
  more.addEventListener("press", () => { if (entry && handlers.onMore) handlers.onMore(entry); });
  about.addEventListener("press", () => showAbout(aboutSteam));
  second.addEventListener("press", () => {
    const def = secondOf(entry);
    if (def && handlers.onSecond) handlers.onSecond(entry, def.kind);
  });

  function titleText() {
    const node = h("div.hub-title", { text: entry.title });
    requestAnimationFrame(() => fitTitle(node));
    return node;
  }

  // Größe des Titels nach der gemessenen Breite, nicht nach der Zahl der Zeichen: groß, wenn er in
  // eine Zeile passt, sonst mittel auf zwei Zeilen, sonst klein (vorher war das kürzere „Spiel
  // installieren“ kleiner gesetzt als „Spielebibliothek“)
  function fitTitle(node) {
    if (!node.isConnected || !node.clientWidth) return;
    const fits = () => node.scrollWidth <= node.clientWidth + 1 && node.scrollHeight <= node.clientHeight + 1;
    node.classList.remove("is-m", "is-s");
    if (fits()) return;
    node.classList.add("is-m");
    if (fits()) return;
    node.classList.replace("is-m", "is-s");
  }
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { const t = logo.querySelector(".hub-title"); if (t) fitTitle(t); });

  // Info-Marken und Knöpfe stehen auf derselben Höhe (07 2.4): Reicht der Platz nicht (Nebenknopf,
  // lange Spielzeit), treten die vorderen Marken zurück, statt Knöpfe zu verdecken
  function fitChips() {
    const a = actions.getBoundingClientRect();
    if (!a.width) return;
    const gap = parseFloat(getComputedStyle(chipBox).columnGap) || 0;
    for (const c of chipBox.children) c.hidden = false;
    for (const c of chipBox.children) {
      if (chipBox.getBoundingClientRect().left >= a.right + gap) break;
      c.hidden = true;
    }
  }

  // E127: Lage und Größe des Logos je Spiel (Option logo {x, y, s}, entry-actions.adjustLogo). Der Kasten bleibt,
  // home.css verschiebt und skaliert nur das Bild darin. Beim Anpassen gilt die Vorschau (Ereignis
  // couch-logo-preview), nach dem Speichern der neue Wert, bis der Eintrag ihn selbst mitbringt.
  let logoPreview = null;
  function placeLogo() {
    const saved = entry && entry.options && entry.options.logo;
    const v = logoPreview || (saved && typeof saved === "object" ? saved : {});
    const n = (x, d) => (typeof x === "number" && Number.isFinite(x) ? x : d);
    logo.style.setProperty("--logo-x", String(n(v.x, 0)));
    logo.style.setProperty("--logo-y", String(n(v.y, 0)));
    logo.style.setProperty("--logo-s", String(n(v.s, 1)));
  }
  const onLogoPreview = (e) => {
    const d = (e && e.detail) || {};
    if (!entry || d.id !== entry.id) return;
    if (d.saved) entry = { ...entry, options: { ...(entry.options || {}), logo: d.value || {} } };
    logoPreview = d.saved ? null : d.value || null;
    placeLogo();
  };
  document.addEventListener("couch-logo-preview", onLogoPreview);

  // Logo oder Titel als Schrift. Das Logo ersetzt den Titel erst, wenn es dekodiert ist.
  async function renderLogo() {
    placeLogo();
    const art = entry.art || {};
    const src = art.logo || "";
    // Trägt der Hintergrund den Schriftzug schon im Bild (Feld des Kerns), steht hier nichts doppelt
    el.classList.toggle("is-titled", !!art.hero_titled);
    const sameEntry = logo.dataset.id === entry.id;
    if (src === logoSrc && logo.firstChild && sameEntry) {
      // Umbenannt (E90): den Titel als Schrift neu setzen; ein Logo-Bild bleibt stehen
      if (!src && logo.dataset.title !== entry.title) { clear(logo); logo.append(titleText()); logo.dataset.title = entry.title; }
      return;
    }
    logo.dataset.id = entry.id;
    logo.dataset.title = entry.title;
    logoSrc = src;
    if (!sameEntry || !src) { clear(logo); logo.append(titleText()); }
    if (!src) return;
    const img = h("img", { alt: entry.title, decoding: "async" });
    img.src = src;
    const want = entry.id;
    if (await decoded(img) && entry && entry.id === want && logoSrc === src) { clear(logo); logo.append(img); }
  }

  function renderState() {
    if (!entry) return;
    mainLabelEl.textContent = entry.mainLabel || mainLabel(entry);
    const ok = entry.system ? true : canStart(entry);
    main.classList.toggle("is-off", !ok);
    if (ok) main.removeAttribute("data-disabled"); else main.setAttribute("data-disabled", "");
    main.classList.toggle("is-running", isRunning(entry));
    more.hidden = !!entry.system;
    const def = secondOf(entry);
    // Verschwindet der fokussierte Nebenknopf („Spiel schließen“), bleibt der Fokus im Hub auf dem
    // Hauptknopf; sonst fiel er auf die Kachel zurück und der Hub klappte zu
    const lost = !def && nav.current === second;
    second.hidden = !def;
    secondLabel.textContent = def ? def.label : "";
    if (lost) nav.focus(main);
    // Apps und Systemkacheln: ein Satz Beschreibung. Spiele: zuletzt gespielt, darunter der Hinweis des Kerns.
    // Nicht erreichbar: der Hinweis sagt, was zu tun ist (wie der Startfehler des Kerns)
    const text = (detail && detail.note) || "";
    const plain = entry.system || entry.kind === "app";
    sub.textContent = plain ? (text || subLine(entry)) : subLine(entry);
    note.textContent = plain ? "" : stateOf(entry) === "unavailable" ? S.unavailableNote : text;
    note.hidden = !note.textContent;
    el.classList.toggle("has-note", !note.hidden);
    showTagline();
    chipBox.replaceChildren(...chips(entry, detail && detail.globals, detail && detail.week));
    requestAnimationFrame(fitChips);
    miniTitle.replaceChildren(...[badge(entry.badge), h("span.ell", { text: entry.title }), ...marks(entry)].filter(Boolean));
  }

  function storeCard(c) {
    const img = h("img", { alt: "", decoding: "async" });
    img.src = c.image;
    const node = h("div.hub-sc", { "data-focus": true, "data-nav-up": "#row-current" }, img,
      h("div.hub-sc-text", h("div.hub-sc-label.ell", { text: c.label || "" }), h("div.hub-sc-title.ell", { text: c.title || "" }),
        h("div.hub-sc-value.ell", { text: c.value || "" })));
    node.addEventListener("press", () => { if (handlers.onStoreCard) handlers.onStoreCard(c); });
    return node;
  }

  function setStoreCards(list) {
    storeCards = (list || []).filter((c) => c && c.id && c.image).slice(0, 4);
    storeBox.replaceChildren(...storeCards.map(storeCard));
    showStore();
  }

  // Karten über „Öffnen“ (Wunsch 2026-10-21): Hoch von „Öffnen“ führt auf die Karten, hoch von
  // einer Karte in die Kachelreihe; ohne Karten gilt wieder, was das Home eingestellt hat
  let mainUp = null;
  function showStore() {
    const on = !!(entry && entry.id === STORE_ID && storeCards.length);
    storeBox.hidden = !on;
    el.classList.toggle("is-store", on);
    if (mainUp === null) mainUp = main.getAttribute("data-nav-up") || "";
    if (on) main.setAttribute("data-nav-up", ".hub-store > .hub-sc");
    else if (mainUp) main.setAttribute("data-nav-up", mainUp); else main.removeAttribute("data-nav-up");
  }

  // Kurzbeschreibung: nur ohne Hinweis des Kerns (der ist wichtiger; drei Zeilen passen nicht über die Knöpfe)
  function showTagline() {
    tagline.hidden = !tagline.textContent || !note.hidden;
    el.classList.toggle("has-tagline", !tagline.hidden);
  }

  // E125: Nützliche Karten (order < 50) ersetzen die technischen aus lib.get; fehlen sie ganz, bleiben die technischen.
  // Solange die Quellen eines Spiels noch fragen (höchstens CARD_WAIT_MS nach der Ruhezeit), steht nichts: Sonst
  // stünden erst die technischen da und sprängen dann um.
  function waitingCards() {
    return !!entry && wantsInfo(entry) && !cardsWaited && (rested !== entry.id || cardsPending > 0);
  }

  function cardNode(c) {
    const node = h("div.hub-card" + (c.image ? ".has-img" : ""), { "data-focus": typeof c.press === "function",
      dataset: { kind: c.kind || "" } },
    c.image ? h("img.hub-card-img", { alt: "", decoding: "async", src: c.image }) : null,
    h("div.hub-card-label.ell", { text: c.label }),
    h("div.hub-card-value" + (c.clamp ? ".clamp2" : ".ell"), { text: c.value || "" }),
    c.sub ? h("div.hub-card-sub.ell", { text: c.sub }) : null,
    c.extra || null,
    typeof c.progress === "number" ? h("div.hub-card-line", h("i", { style: { "--p": String(Math.max(0, Math.min(1, c.progress))) } })) : null);
    if (typeof c.press === "function") node.addEventListener("press", () => c.press());
    return node;
  }

  function renderCards() {
    const byOrder = (a, b) => (a.order ?? 100) - (b.order ?? 100);
    let all = [];
    // Auch die nützlichen erst, wenn alle Quellen geantwortet haben: Sonst standen Trophäen und Fotos erst breit da
    // und ordneten sich um, sobald Neuigkeiten und Infos aus dem Netz kamen
    if (waitingCards()) all = [];
    else if (extraCards.some((c) => (c.order ?? 100) < 50)) all = [...extraCards].sort(byOrder).slice(0, MAX_USEFUL);
    else {
      const base = ((detail && detail.info) || []).slice(0, 3).map((it, i) => ({ ...it, order: 50 + i }));
      all = [...base, ...extraCards].sort(byOrder).slice(0, MAX_CARDS);
    }
    // Lag der Fokus auf einer Karte, bleibt er auf derselben Art (sonst auf dem Hauptknopf)
    const held = nav.current && cards.contains(nav.current) ? nav.current.dataset.kind : null;
    cards.classList.toggle("is-four", all.length > MAX_CARDS);
    cards.replaceChildren(...all.map(cardNode));
    if (held !== null) {
      const again = [...cards.children].find((n) => n.dataset.kind === held && n.hasAttribute("data-focus"));
      if (!(again && nav.focus(again))) nav.focus(main);
    }
  }

  // Nach der Ruhezeit: Kurzbeschreibung (game.info, im Kern einen Tag zwischengespeichert) und Karten-Quellen
  function rest() {
    if (!entry || rested === entry.id) return;
    const want = entry;
    rested = want.id;
    if (wantsInfo(want)) {
      gameInfo(want.id).then((r) => {
        if (!entry || entry.id !== want.id) return;
        tagline.textContent = (r && r.ok && r.steam && r.steam.short) || "";
        showTagline();
      });
    }
    cardsPending = cardSources.length;
    clearTimeout(cardsTimer);
    cardsTimer = setTimeout(() => { if (entry && entry.id === want.id) { cardsWaited = true; renderCards(); } }, CARD_WAIT_MS);
    for (const source of cardSources) ask(source, want);
    if (!cardSources.length) renderCards();
  }

  function ask(source, want) {
    const mine = cardSerial;
    const current = () => mine === cardSerial && entry && entry.id === want.id;
    Promise.resolve().then(() => source(want)).then((list) => {
      if (!current() || !Array.isArray(list)) return;
      extraCards = [...extraCards, ...list.filter((c) => c && c.label)];
    }).catch((e) => console.error("Karten-Quelle im Hub: " + (e && e.stack || e))).then(() => {
      if (!current()) return;
      cardsPending = Math.max(0, cardsPending - 1);
      renderCards();
    });
  }

  function setEntry(next) {
    const changed = !entry || !next || entry.id !== next.id;
    entry = next;
    if (!entry) return;
    if (changed) {
      logoPreview = null;           // E127: eine Vorschau gilt nur für ihren Eintrag
      detail = null;
      extraCards = [];
      cardsPending = 0;
      cardsWaited = false;
      cardSerial += 1;
      rested = "";                  // sonst fragte rest() nach A → B → A (B nicht ausgeruht) nie wieder
      clearTimeout(cardsTimer);
      cards.replaceChildren();
      miniArt.replaceChildren(artBox(entry, { withTitle: false }));
      tagline.textContent = "";
      clearTimeout(restTimer);
      restTimer = setTimeout(rest, REST_MS);
    } else updateArt(miniArt.firstChild, entry);
    renderLogo();
    renderState();
    showStore();
    sections.setEntry(entry);
    trailer.setEntry(entry);
  }

  // Aus lib.get: Karten (die ersten drei Angaben), Hinweiszeile, globale Schalter. Marken und Karten
  // kommen so aus derselben Antwort (nach einer Änderung in den Spieleinstellungen stimmten sie nicht überein)
  function setDetail(next) {
    if (!next || !entry || !next.entry || next.entry.id !== entry.id) return;
    detail = next;
    entry = { ...entry, ...next.entry, mainLabel: entry.mainLabel, running: entry.running || next.entry.running };
    placeLogo();                    // E127: frische Option logo aus lib.get
    renderCards();
    renderState();
  }

  return { el, mini, main, second, more, setEntry, setDetail, setStoreCards, refresh: renderState, get entry() { return entry; },
    // erste Karte der Store-Kachel, wenn sie zu sehen ist (runter aus der Kachelreihe landet dort)
    get firstStoreCard() { return storeBox.hidden ? null : storeBox.firstElementChild; },
    // E123: Abschnitte laden (Spielseite gleich, Home sobald der Fokus im Hub liegt), weitere anhängen, Trailer
    open: () => { clearTimeout(restTimer); rest(); sections.open(); }, sections, trailer,
    addCardSource(fn) { cardSources.push(fn); if (entry && rested === entry.id) ask(fn, entry); },
    destroy() { clearTimeout(restTimer); clearTimeout(cardsTimer); trailer.destroy(); document.removeEventListener("couch-logo-preview", onLogoPreview); } };
}
