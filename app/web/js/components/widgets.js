// Widget-Leiste unten im Home (E126): Karten unter dem Hub, mit dem Controller erreichbar (Runter bis ganz unten).
//   Speicherplatz      frei/gesamt auf der Spiele-Platte (widgets.get)      Kreuz: „Platz schaffen“
//   Trophäen           Summen je Stufe aus allen Steam-Spielen (E124)       Kreuz: Übersicht „Trophäen“
//   Wunschliste        Angebote aus <state>/wishlist-deals.json (E122)       Kreuz: der Store (Steam)
//   Zuletzt gespielt   die letzten Spiele der Reihe (ohne Kern)             Kreuz auf ein Spiel: starten bzw. nach vorn
// Jedes Widget einzeln unter Einstellungen › Home-Bildschirm › Widgets (sys.state.widgets). Ein Abschnitt des Hub
// mit always (hub-sections.js), also nur im Home, für jeden Eintrag. Leistungsregeln des Home: nichts bewegt sich von
// selbst, geladen wird erst, wenn der Hub offen ist, und höchstens alle REFRESH_MS (nie, solange das Home verdeckt ist).
import { h, icon, sizeText } from "../core/dom.js";
import { call } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { router } from "../core/router.js";
import { store } from "../core/store.js";
import { S, fmt, whenText } from "../core/strings.js";
import { artBox } from "./art.js";
import { activateEntry } from "./entry-actions.js";
import { ensureTrophyStyle, openTrophies, tierCounts, trophySummary } from "./trophies.js";

const REFRESH_MS = 5 * 60 * 1000;
const RECENT = 3;
const KINDS = ["storage", "trophies", "deals", "recent"];

// recent(): Einträge der Spiele-Reihe des Home (zuletzt gespielt zuerst)
export function createWidgetBar({ recent = () => [] } = {}) {
  ensureTrophyStyle();
  const cards = {
    storage: h("div.wg.wg-storage", { "data-focus": true, dataset: { kind: "storage" } }),
    trophies: h("div.wg.wg-trophies", { "data-focus": true, dataset: { kind: "trophies" } }),
    deals: h("div.wg.wg-deals", { "data-focus": true, dataset: { kind: "deals" } }),
    recent: h("div.wg.wg-recent", { dataset: { kind: "recent" } }),
  };
  const bar = h("div.wg-bar", KINDS.map((k) => cards[k]));
  const el = h("div.hub-sec.hub-widgets", { hidden: true }, h("div.hub-sec-title", { text: S.wgTitle }), bar);
  let loadedAt = 0;
  let loading = null;
  let data = { storage: null, deals: null, trophies: null };
  let painted = false;

  const on = () => ({ storage: true, trophies: true, deals: true, recent: true, ...(store.state.widgets || {}) });

  function head(sym, label) {
    return h("div.wg-head", icon(sym), h("span.ell", { text: label }));
  }

  function paintStorage() {
    const d = data.storage;
    const used = d && d.total ? 1 - d.free / d.total : 0;
    cards.storage.replaceChildren(head("storage", S.wgStorage),
      h("div.wg-value.ell", { text: d ? fmt(S.wgStorageFree, { free: sizeText(d.free) }) : S.wgStorageNone }),
      h("div.wg-sub.ell", { text: d ? fmt(S.wgStorageOf, { total: sizeText(d.total), name: d.label || "" }) : "" }),
      h("div.wg-line", h("i", { style: { "--p": String(Math.max(0, Math.min(1, used))) } })));
  }

  function paintTrophies() {
    const r = data.trophies;
    const t = r && r.ok && r.totals;
    cards.trophies.replaceChildren(head("trophy", S.wgTrophies),
      h("div.wg-value.ell", { text: t && t.unlocked ? Number(t.unlocked).toLocaleString(S.locale) : S.wgTrophiesNone }),
      t && t.unlocked ? tierCounts(t, { cls: "wg-tiers" }) : h("div.wg-sub.ell", { text: S.wgTrophiesHint }));
  }

  function paintDeals() {
    const d = data.deals;
    let value = S.wgDealsNever;
    let sub = S.wgDealsLogin;
    if (d) {
      value = d.count === 1 ? S.wgDealsOne : d.count ? fmt(S.wgDealsCount, { n: d.count }) : S.wgDealsZero;
      sub = [d.count ? fmt(S.wgDealsBest, { n: d.best }) : "", d.checked ? fmt(S.wgDealsChecked, { when: whenText(d.checked) }) : ""]
        .filter(Boolean).join(" · ");
    }
    cards.deals.replaceChildren(head("store", S.wgDeals), h("div.wg-value.ell", { text: value }), h("div.wg-sub.ell", { text: sub }));
  }

  function paintRecent() {
    // Lag der Fokus auf einem Cover, bleibt er dort (bzw. auf dem ersten): Sonst verschwand er mit dem alten Element,
    // und der Hub blieb ohne nav-blur verschoben stehen
    const held = nav.current && cards.recent.contains(nav.current) ? nav.current.dataset.id || "" : null;
    const list = recent().filter((e) => e && e.kind === "game" && !e.system && e.last_played).slice(0, RECENT);
    const covers = list.map((e) => {
      const node = h("div.wg-cover", { "data-focus": true, dataset: { id: e.id } }, artBox(e, { withTitle: false }));
      node.addEventListener("press", () => activateEntry(e));
      return node;
    });
    cards.recent.replaceChildren(head("clock", S.wgRecent),
      covers.length ? h("div.wg-covers", covers) : h("div.wg-sub.ell", { text: S.wgRecentNone }));
    if (held !== null) {
      const again = covers.find((n) => n.dataset.id === held) || covers[0];
      if (!(again && nav.focus(again))) [cards.deals, cards.trophies, cards.storage].some((c) => !c.hidden && nav.focus(c));
    }
  }

  // Welche Widgets zu sehen sind; ohne eines bleibt der Abschnitt verborgen
  function apply() {
    const want = on();
    for (const k of KINDS) cards[k].hidden = !want[k];
    el.hidden = !painted || !KINDS.some((k) => want[k]);
  }

  function paint() {
    paintStorage();
    paintTrophies();
    paintDeals();
    paintRecent();
    painted = true;
    apply();
  }

  async function fetchAll() {
    const want = on();
    const [w, t] = await Promise.all([
      want.storage || want.deals ? call("widgets.get").catch(() => null) : null,
      want.trophies ? trophySummary() : null,
    ]);
    data = { storage: w ? w.storage : null, deals: w ? w.deals : null, trophies: t };
    loadedAt = Date.now();
  }

  // Hub offen: höchstens alle REFRESH_MS neu fragen; „Zuletzt gespielt“ kommt aus der Reihe und ist immer frisch
  async function load() {
    if (!store.visible) { if (painted) paintRecent(); return; }
    if (Date.now() - loadedAt > REFRESH_MS) {
      if (!loading) loading = fetchAll().finally(() => { loading = null; });
      await loading;
    }
    paint();
  }

  cards.storage.addEventListener("press", () => router.go("cleanup"));
  cards.trophies.addEventListener("press", () => openTrophies());
  cards.deals.addEventListener("press", () => router.go("store", { store: "steam" }));

  return {
    section: { el, always: true, setEntry() {}, load },
    apply,
    // Neue Trophäen oder ein anderes Widget an: beim nächsten Öffnen frisch fragen
    invalidate() { loadedAt = 0; },
    get el() { return el; },
  };
}
