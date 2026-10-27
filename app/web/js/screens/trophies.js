// Trophäen (E124). Bildschirme:
//   trophies        Übersicht aller Steam-Spiele: links die Summen je Stufe, rechts die Spiele mit Fortschritt
//                   (zuletzt freigeschaltet zuerst; trophies.summary). Kreuz: die Liste des Spiels
//   trophies-game   ein Spiel ({id, title?, entry?, focus?}): links Fortschritt und Stufen, rechts alle Trophäen,
//                   freigeschaltete zuerst (neueste zuerst), dann gesperrte; verborgene zeigen „Verborgene Trophäe“,
//                   bis sie frei sind (trophies.game). Kreuz: Angaben der Trophäe
//   L2/R2  seitenweise      Kreis  zurück
// Einstieg: Widget „Trophäen“ im Home, Karte und Abschnitt „Trophäen“ im Hub (components/trophies.js meldet die
// Bildschirme selbst an). Symbole lädt der Kern erst für die Zeilen um den Fokus (trophies.icons).
import { h, icon } from "../core/dom.js";
import { notify } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { router } from "../core/router.js";
import { S, fmt, whenText } from "../core/strings.js";
import { createRowList } from "../components/rows.js";
import { messageDialog } from "../components/dialog.js";
import { TIERS, ensureTrophyStyle, forgetTrophies, percentText, progressBar, rarityText, tierIcon, trophyDesc, trophyGame,
  trophyIcons, trophyName, trophySummary } from "../components/trophies.js";

const AROUND = [6, 14];           // Symbole für so viele Zeilen vor und nach dem Fokus

const hint = (glyph, text) => h("div.hint", glyph.startsWith("btn-") ? icon(glyph) : h("span.keycap", { text: glyph }), h("span", { text }));

function tierName(tier) {
  return (S.trTiers && S.trTiers[tier]) || "";
}

function countsText(p) {
  const t = (p && p.tiers) || {};
  return TIERS.filter((k) => t[k]).map((k) => tierName(k) + " " + t[k]).join(" · ");
}

// Links: Stufen untereinander (Pokal, Name, Zahl)
function tierRows(tiers, unknown) {
  const t = tiers || {};
  const rows = TIERS.map((k) => h("div.tr-tier-row", { dataset: { tier: k } }, tierIcon(k), h("span.ell", { text: tierName(k) }),
    h("b", { text: String(t[k] || 0) })));
  if (unknown) rows.push(h("div.tr-tier-row.is-unknown", tierIcon(null), h("span.ell", { text: S.trTiers.unknown }), h("b", { text: String(unknown) })));
  return h("div.tr-tier-rows", rows);
}

function frame(titleEl, subEl, panel, list, hints) {
  list.scrollbar.classList.add("flow-scroll", "tr-scroll");
  return h("div.flow.tr-screen", titleEl, subEl, panel, h("div.tr-list", list.el), list.scrollbar, h("div.hints", hints));
}

// ---------------------------------------------------------------- Übersicht
function createOverview() {
  ensureTrophyStyle();
  const titleEl = h("h1.flow-title.ell", { text: S.trTitle });
  const subEl = h("div.flow-sub.ell", { text: S.trSub });
  const panel = h("div.tr-panel");
  const list = createRowList({ alwaysFocus: true, emptyText: S.trEmpty, onPress: (row) => { open(row); return true; } });
  const el = frame(titleEl, subEl, panel, list, [hint(S.trKeysPage, S.trHintPage), hint("btn-cross", S.trHintOpen), hint("btn-circle", S.hintBack)]);
  let serial = 0;

  function paint(r) {
    if (!r || !r.ok) {
      panel.replaceChildren(h("div.tr-total", { text: "0" }), h("div.tr-total-label", { text: S.trTotal }));
      list.setRows([]);
      return;
    }
    const t = r.totals || {};
    panel.replaceChildren(h("div.tr-total", { text: String(t.unlocked || 0) }), h("div.tr-total-label", { text: S.trTotal }),
      tierRows(t, t.unknown), h("div.tr-games", { text: fmt(S.trGames, { n: (r.games || []).length, all: t.games || 0, total: t.total || 0 }) }));
    list.setRows((r.games || []).map((g) => ({
      id: g.id, type: "progress", label: g.title, value: g.percent, valueText: percentText(g.percent),
      note: [fmt(S.trOf, { n: g.unlocked, total: g.total }), g.platinum ? tierName("platinum") : countsText(g)].filter(Boolean).join(" · "),
      art: g.entry || { id: g.id, title: g.title, kind: "game", art: {} }, game: g })));
    nav.ensure();
  }

  async function load() {
    const mine = ++serial;
    list.setLoading();
    const r = await trophySummary();
    if (mine !== serial) return;
    if (!r || !r.ok) notify("sys.log", { level: "info", text: "[trophies] summary: " + ((r && (r.reason || r.message)) || "?") });
    paint(r);
  }

  function open(row) {
    if (row && row.game) router.go("trophies-game", { id: row.game.id, title: row.game.title, entry: row.game.entry || null });
  }

  load();
  return {
    el,
    onPad(button) {
      if (button === "l2" || button === "r2") { list.page(button === "r2" ? 1 : -1); return true; }
      return false;
    },
    onEvent(event) { if (event === "trophies.changed") { forgetTrophies(""); load(); } },
    onShow() { nav.ensure(); list.update(); },
    destroy() { serial += 1; },
  };
}

// ---------------------------------------------------------------- ein Spiel
function createGame(params = {}) {
  ensureTrophyStyle();
  const id = String(params.id || "");
  const titleEl = h("h1.flow-title.ell", { text: params.title || (params.entry && params.entry.title) || S.trTitle });
  const subEl = h("div.flow-sub.ell");
  const panel = h("div.tr-panel");
  const list = createRowList({ alwaysFocus: true, emptyText: S.trNone, onPress: (row) => { details(row); return true; } });
  const el = frame(titleEl, subEl, panel, list, [hint(S.trKeysPage, S.trHintPage), hint("btn-cross", S.trHintShow), hint("btn-circle", S.hintBack)]);
  let serial = 0;
  let items = [];
  let focusWanted = params.focus || null;
  const asked = new Set();

  function rowOf(item) {
    const value = [item.tier ? tierName(item.tier) : "", rarityText(item.rarity),
      item.unlocked ? (item.time ? whenText(item.time) : S.trUnlockedNoDate) : ""].filter(Boolean).join(" · ");
    return { id: item.key, type: "info", label: trophyName(item), note: trophyDesc(item), value,
      icon: item.unlocked ? (item.tier ? "trophy-" + item.tier : "trophy") : "trophy",
      art: { id: "tr:" + item.key, title: trophyName(item), kind: "app", system: true, icon: item.hidden ? "lock" : "trophy",
        art: item.icon ? { tile: item.icon } : {} }, item };
  }

  function paint(r) {
    if (!r || !r.ok) {
      subEl.textContent = (r && r.message) || S.trNone;
      panel.replaceChildren();
      list.setRows([]);
      return;
    }
    const p = r.progress || {};
    if (r.steam && r.steam.title) titleEl.textContent = r.steam.title;
    subEl.textContent = fmt(S.trOfLong, { n: p.unlocked || 0, total: p.total || 0 }) + " · " + percentText(p.percent);
    const left = (p.total || 0) - (p.unlocked || 0);
    panel.replaceChildren(h("div.tr-total", { text: percentText(p.percent) }), progressBar(p.percent, "tr-panel-bar"),
      tierRows(p.tiers, p.tiers && p.tiers.unknown),
      h("div.tr-games", { text: p.platinum ? S.trPlatinumDone : left === 1 ? S.trPlatinumLeftOne : fmt(S.trPlatinumLeft, { n: left }) }),
      p.last ? h("div.tr-games", { text: fmt(S.trLast, { when: whenText(p.last) }) }) : null);
    items = (r.steam && r.steam.items) || [];
    list.setRows(items.map(rowOf));
    if (focusWanted) {
      const node = list.nodeOf(focusWanted);
      focusWanted = null;
      if (node) nav.focus(node);
    }
    nav.ensure();
    wantIcons();
  }

  // Symbole für die Zeilen um den Fokus; der Kern lädt fehlende von Steam
  async function wantIcons() {
    const nodes = [...list.el.querySelectorAll(".row[data-row-id]")];
    const at = Math.max(0, nodes.indexOf(nav.current));
    const keys = items.slice(Math.max(0, at - AROUND[0]), at + AROUND[1]).filter((i) => !i.icon && !i.hidden && !asked.has(i.key)).map((i) => i.key);
    if (!keys.length) return;
    for (const k of keys) asked.add(k);
    const mine = serial;
    const icons = await trophyIcons(id, keys);
    if (mine !== serial) return;
    let changed = false;
    for (const item of items) if (icons[item.key] && !item.icon) { item.icon = icons[item.key]; changed = true; }
    if (changed) list.setRows(items.map(rowOf));
  }
  list.el.addEventListener("nav-focus", () => wantIcons());

  function details(row) {
    const item = row && row.item;
    if (!item) return;
    const when = item.unlocked ? fmt(S.trUnlocked, { when: item.time ? whenText(item.time) : S.trUnlockedNoDate }) : S.trLocked;
    const detail = [item.tier ? tierName(item.tier) : "", typeof item.rarity === "number" ? fmt(S.trRarityLong, { p: rarityText(item.rarity) }) : S.trRarityNone, when]
      .filter(Boolean).join(" · ");
    messageDialog({ title: trophyName(item), text: trophyDesc(item) || " ", detail });
  }

  async function load() {
    const mine = ++serial;
    list.setLoading();
    const r = await trophyGame(id);
    if (mine !== serial) return;
    paint(r);
  }

  load();
  return {
    el,
    onPad(button) {
      if (button === "l2" || button === "r2") { list.page(button === "r2" ? 1 : -1); return true; }
      return false;
    },
    onEvent(event, payload) {
      if (event === "trophies.changed" && payload && payload.id === id) { forgetTrophies(id); asked.clear(); load(); }
    },
    onShow() { nav.ensure(); list.update(); },
    destroy() { serial += 1; },
  };
}

export const screens = [{ id: "trophies", create: createOverview }, { id: "trophies-game", create: createGame }];
