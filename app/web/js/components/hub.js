// Hub eines Eintrags (07 Abschnitt 2.4): Logo oder Titel, Unterzeile, Hinweis, Hauptknopf, „…“,
// Info-Marken rechts, Karten. Im Home ist er Vorschau unter der Reihe und rückt nach oben,
// sobald der Fokus in ihn wandert; die Bibliothek zeigt ihn als eigenen Bildschirm.
import { h, icon, clear, decoded } from "../core/dom.js";
import { nav } from "../core/nav.js";
import { S, fmt, playtimeText, whenText } from "../core/strings.js";
import { store } from "../core/store.js";
import { artBox, updateArt, badge, marks, stateOf } from "./art.js";
import { mainLabel, canStart, isRunning } from "./entry-actions.js";

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

export function createHub(handlers = {}) {
  const logo = h("div.hub-logo");
  const sub = h("div.hub-sub.ell");
  const note = h("div.hub-note.ell", { hidden: true });
  const mainLabelEl = h("span.btn-label");
  const main = h("div.btn.hub-main", { "data-focus": true }, mainLabelEl);
  const secondLabel = h("span.btn-label");
  const second = h("div.btn.is-secondary.hub-second", { "data-focus": true, hidden: true }, secondLabel);
  // Rechts vom „…“ ist Schluss (Randton): Nach Lage läge sonst das Zahnrad oben rechts „rechts“
  const more = h("div.btn.is-secondary.is-round.hub-more", { "data-focus": true, "data-nav-right": "none" }, icon("more"));
  const actions = h("div.hub-actions", main, second, more);
  const chipBox = h("div.hub-chips");
  const cards = h("div.hub-cards");
  // Store-Kachel (Wunsch 2026-10-21: „mehr Infos als nur Öffnen“): anwählbare Karten mit Bild
  // (Gratis bei Epic, Angebote) statt der Infokarten; Kreuz öffnet die Seite des Spiels
  const storeBox = h("div.hub-cards.hub-store", { hidden: true });
  let storeCards = [];
  const el = h("div.hub", { "data-focus-group": true }, logo, sub, note, actions, chipBox, cards, storeBox);
  // Kopf des Eintrags, wenn der Hub offen ist: Mini-Bild, Marke, Titel (07 Zustand B)
  const miniArt = h("div.mini-art");
  const miniTitle = h("div.mini-title");
  const mini = h("div.mini", miniArt, miniTitle);
  let entry = null;
  let detail = null;
  let logoSrc = null;

  main.addEventListener("press", () => { if (entry && handlers.onMain) handlers.onMain(entry); });
  more.addEventListener("press", () => { if (entry && handlers.onMore) handlers.onMore(entry); });
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

  // Logo oder Titel als Schrift. Das Logo ersetzt den Titel erst, wenn es dekodiert ist.
  async function renderLogo() {
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

  function setEntry(next) {
    const changed = !entry || !next || entry.id !== next.id;
    entry = next;
    if (!entry) return;
    if (changed) { detail = null; cards.replaceChildren(); miniArt.replaceChildren(artBox(entry, { withTitle: false })); }
    else updateArt(miniArt.firstChild, entry);
    renderLogo();
    renderState();
    showStore();
  }

  // Aus lib.get: Karten (die ersten drei Angaben), Hinweiszeile, globale Schalter. Marken und Karten
  // kommen so aus derselben Antwort (nach einer Änderung in den Spieleinstellungen stimmten sie nicht überein)
  function setDetail(next) {
    if (!next || !entry || !next.entry || next.entry.id !== entry.id) return;
    detail = next;
    entry = { ...entry, ...next.entry, mainLabel: entry.mainLabel, running: entry.running || next.entry.running };
    cards.replaceChildren(...(next.info || []).slice(0, 3).map((it) =>
      h("div.hub-card", h("div.hub-card-label.ell", { text: it.label }), h("div.hub-card-value.ell", { text: it.value }))));
    renderState();
  }

  return { el, mini, main, second, more, setEntry, setDetail, setStoreCards, refresh: renderState, get entry() { return entry; },
    // erste Karte der Store-Kachel, wenn sie zu sehen ist (runter aus der Kachelreihe landet dort)
    get firstStoreCard() { return storeBox.hidden ? null : storeBox.firstElementChild; } };
}
