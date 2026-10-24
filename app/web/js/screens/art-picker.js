// Bilder eines Eintrags (Bauplan E34): „Bilder neu laden“ (lib.fetchArt {id}), „Treffer auswählen“
// (lib.artCandidates -> lib.fetchArt {id, candidate}), „Eigenes Bild wählen“ (Dateiauswahl ab dem
// Download-Ordner mit Vorschaubildern -> lib.setArt) und „Automatische Bilder wiederherstellen“
// (lib.resetArt). Rechts stehen Kachel und Hintergrund, wie sie gerade sind; neue Bilder kommen als
// lib.changed {ids, reason: "art"} und erscheinen dort ohne Flackern.
import { h } from "../core/dom.js";
import { call, on } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { router } from "../core/router.js";
import { S, fmt } from "../core/strings.js";
import { artBox, updateArt } from "../components/art.js";
import { confirm } from "../components/dialog.js";
import { openMenu } from "../components/menu.js";
import { toast } from "../components/toast.js";
import { refetchArt } from "../components/entry-actions.js";
import { askText } from "../components/osk-input.js";
import { createListScreen } from "./file-browser.js";

const KINDS = [
  { id: "tile", label: S.artKindTile }, { id: "cover", label: S.artKindCover },
  { id: "hero", label: S.artKindHero }, { id: "logo", label: S.artKindLogo },
];
const KIND_WORDS = { cover: S.candKindCover, hero: S.candKindHero, logo: S.candKindLogo, tile: S.candKindTile, icon: S.candKindIcon };

const report = (title, result, fallback) => toast({ title, body: (result && result.message) || fallback || S.errorGeneric, icon: result && result.ok === false ? "warn" : "check", sound: false });

// ---------------------------------------------------------------- Übersicht
function createPicker(params) {
  let entry = params.entry;
  // Rechts die Bilder, wie sie jetzt sind, beschriftet; ohne Hintergrund steht das da (vorher ein
  // leeres schwarzes Feld ohne Hinweis)
  const tile = h("div.art-now-tile", artBox(entry, { withTitle: false }));
  const heroSrc = (entry.art && entry.art.hero) || "";
  const noHero = h("div.art-now-none", { text: S.artNoHero, hidden: !!heroSrc });
  const hero = h("div.art-now-hero", h("img", { alt: "", hidden: !heroSrc, src: heroSrc }), noHero);
  const side = h("div.flow-side.art-now",
    h("div.art-now-label", { text: S.artNowTile }), tile,
    h("div.art-now-label", { text: S.artNowHero }), hero);
  const screen = createListScreen({
    title: S.artTitle, sub: entry.title, side, cls: "has-side",
    hints: [["btn-cross", S.select], ["btn-circle", S.hintBack]],
    handlers: { onPress: (row) => { press(row); return true; } },
  });
  const rows = [];
  if (params.fetch) {
    rows.push({ id: "fetch", type: "action", label: S.artFetch, hint: S.artFetchHint, icon: "download" });
    rows.push({ id: "pick", type: "page", label: S.artPick, hint: S.artPickHint, icon: "search" });
  }
  rows.push({ id: "own", type: "page", label: S.artOwn, hint: S.artOwnHint, icon: "folder" });
  rows.push({ id: "reset", type: "action", label: S.artReset, hint: S.artResetHint, icon: "restart" });
  screen.list.setRows(rows);

  async function press(row) {
    if (!row) return;
    if (row.id === "fetch") refetchArt(entry);
    else if (row.id === "pick") router.go("art-candidates", { entry });
    else if (row.id === "own") {
      const kind = await openMenu(KINDS, { cls: "menu-center", title: S.artKindTitle });
      if (!kind) return;
      router.go("file-browser", {
        mode: "image",
        onPick: async (file) => {
          // Nur zurück, wenn die Dateiauswahl noch oben liegt (Kreis während des Wartens, zweites Kreuz)
          const here = router.top;
          try {
            const result = await call("lib.setArt", { id: entry.id, kind, path: file.path });
            report(entry.title, result, S.artSetDone);
            if (result && result.ok !== false && router.top === here) router.back();
          } catch (e) { toast({ title: entry.title, body: e.message || S.errorGeneric, icon: "warn", sound: "error" }); }
        },
      });
    } else if (row.id === "reset") {
      const yes = await confirm({ title: S.artResetTitle, text: fmt(S.artResetText, { title: entry.title }), ok: S.artResetOk });
      if (!yes) return;
      try {
        const result = await call("lib.resetArt", { id: entry.id });
        toast({ title: entry.title, body: result && result.fetching ? S.artResetDone + " " + S.artResetFetching : S.artResetDone, icon: "check", sound: false });
      } catch (e) { toast({ title: entry.title, body: e.message || S.errorGeneric, icon: "warn", sound: "error" }); }
    }
  }

  // Neue Bilder dieses Eintrags: Vorschau rechts nachziehen
  async function refresh() {
    try { entry = (await call("lib.get", { id: entry.id })).entry || entry; } catch (e) { return; }
    updateArt(tile.firstChild, entry);
    const img = hero.firstChild;
    const src = (entry.art && entry.art.hero) || "";
    img.hidden = !src;
    noHero.hidden = !!src;
    if (src && img.getAttribute("src") !== src) img.src = src;
  }
  const off = on("lib.changed", (p) => { if (!p || !Array.isArray(p.ids) || p.ids.includes(entry.id)) refresh(); });
  return { el: screen.el, onShow() { nav.ensure(); }, destroy() { off(); } };
}

// ---------------------------------------------------------------- Treffer auswählen
function createCandidates(params) {
  const entry = params.entry;
  const spinner = h("div.spinner", { hidden: true });
  const screen = createListScreen({
    title: S.artPick, sub: entry.title, side: spinner,
    hints: [["btn-cross", S.select], ["btn-circle", S.hintBack]],
    handlers: { onPress: (row) => { press(row); return true; }, emptyText: S.candEmpty },
  });
  let query = "";
  let serial = 0;
  let taking = false;

  function rowOf(c, i) {
    const kinds = (c.kinds || []).map((k) => KIND_WORDS[k] || k).join(", ");
    return {
      id: "cand:" + i, type: "action", label: c.name || c.ref, value: c.label || c.source,
      note: [c.safe ? S.candAuto : "", kinds].filter(Boolean).join(" · "),
      art: c.thumb ? { id: "cand:" + c.source + ":" + c.ref, kind: "game", title: c.name || "", art: { tile: c.thumb } } : null,
      icon: c.thumb ? null : "search", cand: c,
    };
  }

  async function search() {
    const mine = ++serial;
    spinner.hidden = false;
    screen.setSub(S.candLoading);
    if (screen.list.setLoading) screen.list.setLoading(0);
    let rows;
    try {
      const p = { id: entry.id };
      if (query) p.query = query;
      const result = await call("lib.artCandidates", p);
      const list = (result && result.candidates) || [];
      // Der Treffer der Automatik zuerst
      list.sort((a, b) => (b.safe ? 1 : 0) - (a.safe ? 1 : 0) || (b.score || 0) - (a.score || 0));
      rows = list.map(rowOf);
    } catch (e) {
      rows = [{ id: "_err", type: "info", label: e.message || S.errorGeneric, enabled: false }];
    }
    if (mine !== serial) return;
    spinner.hidden = true;
    screen.setSub(query ? fmt(S.candQuery, { q: query }) : entry.title);
    screen.list.setRows([{ id: "query", type: "action", label: S.candSearch, value: query, icon: "keyboard" }, ...rows]);
    const first = rows.find((r) => r.cand);
    if (first && screen.list.nodeOf) { const node = screen.list.nodeOf(first.id); if (node) nav.focus(node); }
    nav.ensure();
  }

  async function press(row) {
    if (!row) return;
    if (row.id === "query") {
      let text;
      try { text = await askText({ title: S.candSearch, initial: query || entry.title }); } catch (e) { return; }
      if (text === null) return;
      query = text.trim();
      search();
      return;
    }
    if (!row.cand || taking) return;
    taking = true;
    spinner.hidden = false;
    screen.setSub(S.candTaking);
    const here = router.top;          // lib.fetchArt darf bis 90 s dauern: Kreis führte inzwischen schon zurück
    try {
      const result = await call("lib.fetchArt", { id: entry.id, candidate: { source: row.cand.source, ref: row.cand.ref } });
      report(entry.title, result);
      if (result && result.ok !== false) { if (router.top === here) router.back(); return; }
    } catch (e) {
      toast({ title: entry.title, body: e.message || S.errorGeneric, icon: "warn", sound: "error" });
    } finally {
      taking = false;
      spinner.hidden = true;
      screen.setSub(query ? fmt(S.candQuery, { q: query }) : entry.title);
    }
  }

  search();
  return { el: screen.el, onShow() { nav.ensure(); }, destroy() { serial += 1; } };
}

export const screens = [
  { id: "art-picker", create: createPicker },
  { id: "art-candidates", create: createCandidates },
];
export default screens[0];
