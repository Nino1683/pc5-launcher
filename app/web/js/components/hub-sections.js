// Abschnitte unter dem Hub (E123): „Über das Spiel“ (Kurzbeschreibung, Fakten, Steam-Bilder) und
// „Neuigkeiten“ (offizielle Meldungen und Patch-Notizen) für Spiele mit Steam-Seite (game.info, game.news).
// Geladen wird erst, wenn der Hub offen ist (open(): Fokus im Hub bzw. Spielseite), nie beim Durchblättern der Reihe.
// Ohne Angaben (kein Steam-Treffer, kein Netz, Prüfmodus) bleibt der Platz leer.
//
// Weitere Abschnitte hängen sich mit add({el, setEntry(entry), load(entry, current), always?}) an: el trägt
// die Klasse hub-sec und ist versteckt, solange es nichts zu zeigen gibt; load liefert ein Promise, current() sagt,
// ob der Eintrag noch derselbe ist. Fokussierbares darin wird wie hier erreicht (Runter vom Hauptknopf), der Hub
// rückt dabei so weit hoch, dass der Abschnitt unter dem Kopf steht (deepTo).
// E124: „Trophäen“ (components/trophies.js) steht als erster Abschnitt. always: true (E126, Widget-Leiste des Home)
// lädt bei jedem Eintrag, auch bei Apps und Systemkacheln, und bleibt beim Wechsel stehen.
// Die Karten unter dem Hub (hub-cards.js, E125) zeigen dieselbe Meldung bzw. Beschreibung: showNews, showAbout.
// Den Knopf „Beschreibung“ trägt der Hub selbst rechts neben „…“ (2026-10-28: unter den Bildern stand er zu dicht
// daran); onAbout(steam oder null) sagt ihm, ob es eine lange Beschreibung gibt.
import { h, cssPx, ensureStyle } from "../core/dom.js";
import { call } from "../core/bridge.js";
import { S, fmt, whenText } from "../core/strings.js";
import { confirm, dialog, imageViewer, messageDialog, scrollTextDialog } from "./dialog.js";
import { gameInfo, gameNews, wantsInfo } from "./gameinfo.js";
import { trophySection } from "./trophies.js";

// Meldung im Browser der Konsole; läuft vorn etwas anderes, fragt die Seite wie bei jedem Start
async function openNews(entry, item) {
  try {
    let r = await call("game.openNews", { id: entry.id, gid: item.gid });
    if (r && r.needsConfirm === "close-other") {
      const other = r.other || {};
      const yes = await confirm({ title: fmt(S.startOtherTitle, { title: S.stBrowserApp }),
        text: fmt(S.startOtherText, { other: other.title || S.unknown }), ok: S.start });
      if (!yes) return;
      r = await call("game.openNews", { id: entry.id, gid: item.gid, closeOther: true });
    }
    if (r && r.ok === false) messageDialog({ title: S.hiNews, text: r.message || S.errorGeneric });
  } catch (e) {
    messageDialog({ title: S.hiNews, text: (e && e.message) || S.errorGeneric });
  }
}

// ---------------------------------------------------------------- Über das Spiel
function aboutSection(onAbout) {
  const short = h("div.st-short.hub-about-text");
  const facts = h("div.st-facts.hub-facts");
  const track = h("div.hub-shots-track");
  // Keine eigene Fokus-Gruppe: Der Hub ist eine; eine Gruppe darin schickte Runter aus den Bildern zurück auf
  // den zuletzt fokussierten Knopf des Hub (nav.js: „dorthin, wo man sie zuletzt verlassen hat“)
  const shots = h("div.hub-shots", track);
  const el = h("div.hub-sec.hub-about", { hidden: true }, h("div.hub-sec-title", { text: S.hiAbout }), short, facts, shots);
  let steam = null;
  let first = 0;                  // erstes sichtbares Bild der Reihe

  function factRows(d) {
    const f = S.stFacts;
    return [
      [d.coming_soon ? f.release : S.hiReleased, d.release || ""],
      [f.developer, (d.developers || []).join(", ")],
      [S.hiPublisher, (d.publishers || []).join(", ")],
      [f.genre, (d.genres || []).join(", ")],
      [f.metacritic, d.metacritic ? String(d.metacritic) : ""],
    ].filter(([, v]) => v);
  }

  function render(d) {
    steam = d;
    first = 0;
    track.style.removeProperty("--fx-x");
    short.textContent = d.short || "";
    short.hidden = !d.short;
    facts.replaceChildren(...factRows(d).map(([k, v]) => h("div.st-fact", h("span", { text: k }), h("b.ell", { text: v }))));
    facts.hidden = !facts.childElementCount;
    const list = d.screenshots || [];
    track.replaceChildren(...list.map((s, i) => h("div.st-shot.hub-shot", { "data-focus": true, dataset: { i } },
      h("img", { alt: "", src: s.thumb, decoding: "async", loading: "lazy" }))));
    shots.hidden = !list.length;
    onAbout(d.about && d.about.length > (d.short || "").length + 40 ? d : null);
    el.hidden = short.hidden && facts.hidden && shots.hidden;
  }

  // Mehr als vier Bilder: so weit rücken, dass das fokussierte zu sehen ist
  track.addEventListener("nav-focus", (e) => {
    const t = e.target.closest(".hub-shot");
    if (!t) return;
    const i = Number(t.dataset.i) || 0;
    const visible = 4;
    if (i < first) first = i;
    else if (i > first + visible - 1) first = i - visible + 1;
    const step = cssPx("--st-shot-w") + cssPx("--st-gap-s");
    if (first) track.style.setProperty("--fx-x", -(first * step) + "px"); else track.style.removeProperty("--fx-x");
  });
  track.addEventListener("press", (e) => {
    const t = e.target.closest(".hub-shot");
    const list = (steam && steam.screenshots) || [];
    if (!t || !list.length) return;
    imageViewer({ fill: true, count: list.length, start: Number(t.dataset.i) || 0, src: (i) => list[i].full,
      caption: (i) => fmt(S.stImageOf, { n: i + 1, total: list.length }) });
  });
  return {
    el,
    setEntry() { steam = null; el.hidden = true; track.replaceChildren(); onAbout(null); },
    async load(entry, current) {
      const r = await gameInfo(entry.id);
      if (!current()) return;
      if (r && r.ok && r.steam) render(r.steam); else { el.hidden = true; onAbout(null); }
    },
  };
}

// ---------------------------------------------------------------- Neuigkeiten
function newsSection() {
  const grid = h("div.hub-news");
  const el = h("div.hub-sec.hub-newsbox", { hidden: true }, h("div.hub-sec-title", { text: S.hiNews }), grid);
  let entry = null;
  let items = [];

  function card(n, i) {
    return h("div.hub-card.hub-news-card", { "data-focus": true, dataset: { i } },
      h("div.hub-card-label.ell", { text: line(n) }), h("div.hub-news-title.clamp2", { text: n.title }));
  }

  // Kreuz: der Text der Meldung, daraus „Im Browser öffnen“
  grid.addEventListener("press", (e) => {
    const t = e.target.closest(".hub-news-card");
    const n = t ? items[Number(t.dataset.i) || 0] : null;
    if (n && entry) showNews(entry, n);
  });

  return {
    el,
    setEntry(next) { entry = next; items = []; el.hidden = true; grid.replaceChildren(); },
    async load(target, current) {
      const r = await gameNews(target.id);
      if (!current()) return;
      const news = r && r.ok && r.steam ? r.steam.news : null;
      items = Array.isArray(news) ? news.slice(0, 6) : [];
      grid.replaceChildren(...items.map(card));
      el.hidden = !items.length;
    },
  };
}

// Die lange Beschreibung (Knopf „Beschreibung“, Karte „Über das Spiel“)
export function showAbout(steam) {
  if (!steam || !steam.about) return;
  const text = h("div.st-about", { text: steam.about });
  scrollTextDialog({ title: steam.title || S.hiAbout, box: h("div.st-about-box", text), body: text });
}

const newsSource = (n) => (n.patch ? S.hiNewsPatch : S.hiNewsSteam);
const line = (n) => [whenText(n.date), newsSource(n)].filter(Boolean).join(" · ");

// Kreuz auf einer Meldung (Abschnitt „Neuigkeiten“, Karte „Neuigkeiten“): ihr Text, daraus „Im Browser öffnen“
export async function showNews(entry, n) {
  if (!entry || !n) return;
  const choice = await dialog({ title: n.title, text: n.text || S.hiNewsNoText, detail: line(n), wide: true,
    buttons: [{ id: "close", label: S.close }, { id: "open", label: S.hiNewsOpen }], initial: "open" });
  if (choice === "open") openNews(entry, n);
}

// ---------------------------------------------------------------- Gerüst
// hubEl: der Hub (.hub); ihm setzt deepTo die Verschiebung (--fx-y), solange der Fokus in einem Abschnitt liegt.
// onAbout(steam oder null): Gibt es zum Eintrag eine lange Beschreibung (Knopf „Beschreibung“ im Hub)?
export function createHubSections(hubEl, { onAbout = () => {} } = {}) {
  ensureStyle("hub-info");             // eigene Gestaltung (css/hub-info.css)
  const el = h("div.hub-extra", { hidden: true });
  const sections = [];
  let entry = null;
  let serial = 0;
  let opened = false;

  function refresh() { el.hidden = !sections.some((s) => !s.el.hidden); }

  function add(section) {
    sections.push(section);
    el.append(section.el);
    if (entry) section.setEntry(entry);
    if (opened && (section.always || wantsInfo(entry))) load(section);
    refresh();
  }

  function load(section) {
    const mine = serial;
    const current = () => mine === serial;
    Promise.resolve().then(() => section.load(entry, current)).catch((e) => {
      console.error("Abschnitt im Hub: " + (e && e.stack || e));
    }).then(() => { if (current()) refresh(); });
  }

  function setEntry(next) {
    if (entry && next && entry.id === next.id) { entry = next; return; }
    entry = next || null;
    serial += 1;
    opened = false;
    undeep();
    for (const s of sections) s.setEntry(entry);
    refresh();
  }

  // Hub offen (Fokus darin, Spielseite): jetzt laden, einmal je Eintrag. Ohne Steam-Angaben nur die Abschnitte für
  // jeden Eintrag (always)
  function open() {
    if (opened || !entry) return;
    opened = true;
    const game = wantsInfo(entry);
    for (const s of sections) if (game || s.always) load(s);
  }

  function undeep() {
    hubEl.classList.remove("is-deep");
    hubEl.style.removeProperty("--fx-y");
    for (const s of sections) s.el.classList.remove("is-past");
  }

  // Abschnitt unter den Kopf (Bild und Titel oben links) rücken; ragt das fokussierte Element dann unten aus dem
  // Bild, so weit weiter, dass es ganz zu sehen ist. offsetTop zählt ohne die laufende Verschiebung.
  function deepTo(target) {
    const sec = target.closest(".hub-sec");
    if (!sec) return;
    const head = cssPx("--mini-pos") + cssPx("--mini-size") + cssPx("--section-mt");
    const top = hubEl.offsetTop + el.offsetTop + sec.offsetTop;
    let shift = head - top;
    const inner = target.getBoundingClientRect().bottom - sec.getBoundingClientRect().top;
    const room = innerHeight - cssPx("--section-mt");
    if (head + inner > room) shift -= head + inner - room;
    hubEl.classList.add("is-deep");
    hubEl.style.setProperty("--fx-y", Math.round(shift) + "px");
    // Abschnitte darüber treten zurück: Sie lägen sonst über dem Kopf (Bild und Titel oben links)
    let past = true;
    for (const s of sections) {
      if (s.el === sec) past = false;
      s.el.classList.toggle("is-past", past);
    }
  }

  el.addEventListener("nav-focus", (e) => deepTo(e.target));
  el.addEventListener("nav-blur", (e) => {
    const to = e.detail && e.detail.to;
    if (!to || !el.contains(to)) undeep();
  });

  add(trophySection());           // E124
  add(aboutSection(onAbout));
  add(newsSection());
  return { el, add, setEntry, open, undeep, get sections() { return sections; } };
}
