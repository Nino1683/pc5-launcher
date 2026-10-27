// Steam-Trophäen (E124): Daten des Kerns (trophies.game, trophies.summary, trophies.icons), kurz gemerkt, die
// Bausteine (Pokal je Stufe, Seltenheit, Fortschritt) und der Abschnitt „Trophäen“ unter dem Hub (hub-sections.js).
// Nur Steam-Spiele (steam:<appid>) haben Trophäen; alles andere fragt gar nicht erst und zeigt nichts.
// Stufen nach Seltenheit (eigene Festlegung des Kerns): unter 10 % Gold, unter 35 % Silber, sonst Bronze; alle eines
// Spiels = Platin. Die Pokale sind eigene Symbole (icons.svg trophy, trophy-<stufe>).
// Bildschirme „trophies“ (Übersicht) und „trophies-game“ (Liste eines Spiels) stehen in screens/trophies.js;
// openTrophies() meldet sie beim ersten Mal selbst am Router an.
import { h, icon } from "../core/dom.js";
import { call } from "../core/bridge.js";
import { router } from "../core/router.js";
import { S, fmt, whenText } from "../core/strings.js";

// Der Kern liest die Dateien bei jeder Frage neu, wenn sie sich geändert haben; hier nur so lange, dass Karte und
// Abschnitt desselben Eintrags (und Home/Spielseite im Wechsel) nicht doppelt fragen
const KEEP_MS = 60 * 1000;
const KEEP_FAIL_MS = 15 * 1000;
const MAX = 30;
const memo = new Map();            // "<methode> <id>" -> {at, keep, promise}
export const TIERS = ["platinum", "gold", "silver", "bronze"];
const SHOWN = 6;                   // so viele Trophäen zeigt der Abschnitt im Hub

export function hasTrophies(entry) {
  return !!(entry && !entry.system && /^steam:\d+$/.test(String(entry.id || "")));
}

function ask(method, id) {
  const key = method + " " + (id || "");
  const now = Date.now();
  const hit = memo.get(key);
  if (hit && now - hit.at < hit.keep) return hit.promise;
  const item = { at: now, keep: KEEP_MS, promise: null };
  item.promise = call(method, id ? { id } : {}).then((r) => {
    if (!r || r.ok === false) item.keep = KEEP_FAIL_MS;
    return r || { ok: false };
  }, (e) => {
    item.keep = 0;
    return { ok: false, reason: "error", message: (e && e.message) || "" };
  });
  memo.set(key, item);
  while (memo.size > MAX) memo.delete(memo.keys().next().value);
  return item.promise;
}

export const trophyGame = (id) => ask("trophies.game", id);
export const trophySummary = () => ask("trophies.summary", "");

// Neue Trophäen gemeldet (Ereignis trophies.changed): beim nächsten Mal frisch fragen
export function forgetTrophies(id) {
  memo.delete("trophies.game " + (id || ""));
  memo.delete("trophies.summary ");
}

// Symbole der genannten Trophäen; der Kern lädt fehlende von Steam (höchstens 24 je Aufruf)
export async function trophyIcons(id, keys) {
  if (!keys.length) return {};
  try {
    const r = await call("trophies.icons", { id, keys: keys.slice(0, 24) });
    return (r && r.icons) || {};
  } catch (e) {
    return {};
  }
}

// ---------------------------------------------------------------- Bausteine
export function ensureTrophyStyle() {
  if (document.querySelector("link[data-part=trophies]")) return;
  document.head.append(h("link", { rel: "stylesheet", href: "css/trophies.css", "data-part": "trophies" }));
}

export function tierIcon(tier) {
  return icon(TIERS.includes(tier) ? "trophy-" + tier : "trophy", "tr-tier" + (tier ? " is-" + tier : " is-none"));
}

function number(value, digits) {
  return Number(value).toLocaleString(S.locale, { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

export function rarityText(p) {
  return typeof p === "number" ? fmt(S.trRarity, { p: number(p, p < 1 ? 2 : 1) }) : "";
}

export function percentText(p) {
  return fmt(S.trPercent, { p: number(p || 0, 0) });
}

// Zähler je Stufe („Pokal 2 · Pokal 23“); all: auch Stufen ohne Trophäe (Übersicht)
export function tierCounts(tiers, { all = false, cls = "" } = {}) {
  const t = tiers || {};
  const parts = TIERS.filter((k) => all || t[k]).map((k) =>
    h("span.tr-count", { dataset: { tier: k } }, tierIcon(k), h("b", { text: number(t[k] || 0, 0) })));
  return h("div.tr-counts" + (cls ? "." + cls : ""), parts);
}

export function progressBar(percent, cls = "") {
  const p = Math.max(0, Math.min(100, Number(percent) || 0));
  return h("div.tr-bar" + (cls ? "." + cls : ""), h("i", { style: { "--p": String(p / 100) } }));
}

// Name einer Trophäe; verborgene, gesperrte nennen ihn nicht (der Kern schickt ihn dann auch nicht)
export function trophyName(item) {
  return item.hidden ? S.trHidden : item.name || S.trHidden;
}
export function trophyDesc(item) {
  return item.hidden ? S.trHiddenDesc : item.desc || "";
}

// Fläche für das Symbol: Bild von Steam, sonst ein Platzhalter (Pokal bzw. Schloss bei verborgenen)
export function trophyArt(item) {
  const box = h("div.tr-art" + (item.unlocked ? "" : ".is-locked"));
  setTrophyArt(box, item);
  return box;
}
export function setTrophyArt(box, item) {
  if (item.icon) {
    const img = h("img", { alt: "", decoding: "async", src: item.icon });
    img.addEventListener("error", () => img.replaceWith(icon(item.hidden ? "lock" : "trophy")), { once: true });
    box.replaceChildren(img);
  } else {
    box.replaceChildren(icon(item.hidden ? "lock" : "trophy"));
  }
}

// Zeile unter Name und Beschreibung: Stufe, Seltenheit, Datum
export function trophyFoot(item) {
  const when = item.unlocked ? (item.time ? whenText(item.time) : S.trUnlockedNoDate) : "";
  return h("div.tr-foot", tierIcon(item.tier), h("span.ell", { text: [rarityText(item.rarity), when].filter(Boolean).join(" · ") }));
}

export async function ensureTrophyScreens() {
  if (router.known("trophies")) return;
  const m = await import("../screens/trophies.js");
  if (!router.known("trophies")) router.register(...m.screens);
}

// Übersicht ohne params; mit {id, title?, entry?, focus?} gleich die Liste dieses Spiels
export async function openTrophies(params = {}) {
  await ensureTrophyScreens();
  router.go(params.id ? "trophies-game" : "trophies", params);
}

// ---------------------------------------------------------------- Abschnitt „Trophäen“ unter dem Hub
// Kopf mit Fortschritt und Stufen, die ersten sechs (zuletzt freigeschaltet zuerst, dann gesperrte), „Alle Trophäen“.
// Kreuz auf einer Trophäe öffnet die Liste des Spiels an dieser Stelle.
export function trophySection() {
  ensureTrophyStyle();
  const pct = h("div.tr-pct");
  const bar = progressBar(0, "tr-head-bar");
  const count = h("div.tr-head-count.ell");
  const counts = h("div.tr-head-tiers");
  const head = h("div.tr-head", pct, h("div.tr-head-main", bar, count), counts);
  const grid = h("div.tr-grid");
  const more = h("div.btn.is-secondary.tr-more", { "data-focus": true }, tierIcon(null), h("span.btn-label", { text: S.trAll }));
  const el = h("div.hub-sec.hub-trophies", { hidden: true }, h("div.hub-sec-title", { text: S.trTitle }), head, grid, more);
  let entry = null;
  let data = null;

  function card(item) {
    return h("div.tr-card" + (item.unlocked ? "" : ".is-locked"), { "data-focus": true, dataset: { key: item.key } },
      trophyArt(item),
      h("div.tr-card-text", h("div.tr-card-name.ell", { text: trophyName(item) }),
        h("div.tr-card-desc.clamp2", { text: trophyDesc(item) }), trophyFoot(item)));
  }

  function render(r) {
    data = r;
    const p = r.progress || {};
    pct.textContent = percentText(p.percent);
    bar.firstChild.style.setProperty("--p", String((p.percent || 0) / 100));
    count.textContent = fmt(S.trOfLong, { n: p.unlocked || 0, total: p.total || 0 });
    counts.replaceChildren(tierCounts(p.tiers, { all: true }));
    const items = ((r.steam && r.steam.items) || []).slice(0, SHOWN);
    grid.replaceChildren(...items.map(card));
    el.hidden = false;
    const missing = items.filter((i) => !i.icon && !i.hidden).map((i) => i.key);
    const want = entry && entry.id;
    if (missing.length) trophyIcons(want, missing).then((icons) => {
      if (!entry || entry.id !== want) return;
      for (const node of grid.children) {
        const url = icons[node.dataset.key];
        const item = items.find((i) => i.key === node.dataset.key);
        if (url && item) { item.icon = url; setTrophyArt(node.querySelector(".tr-art"), item); }
      }
    });
  }

  grid.addEventListener("press", (e) => {
    const t = e.target.closest(".tr-card");
    if (t && entry) openTrophies({ id: entry.id, title: entry.title, entry, focus: t.dataset.key });
  });
  more.addEventListener("press", () => { if (entry) openTrophies({ id: entry.id, title: entry.title, entry }); });

  return {
    el,
    setEntry(next) { entry = next; data = null; el.hidden = true; grid.replaceChildren(); },
    async load(target, current) {
      if (!hasTrophies(target)) { el.hidden = true; return; }
      const r = await trophyGame(target.id);
      if (!current()) return;
      if (r && r.ok && r.progress && r.progress.total) render(r); else el.hidden = true;
    },
    get data() { return data; },
  };
}
