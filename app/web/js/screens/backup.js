// Einstellungen sichern und übernehmen (E106, Teil P11). Bildschirm-IDs (Bauplan docs/research/e103/SPEC.md 4.4):
//   backup
// Einstieg: Einstellungen › PC5 Launcher › „Einstellungen sichern und übernehmen“ (settings.action -> {ui: "backup"}).
// Zwei Ansichten im selben Bildschirm (Gerüst der Abläufe, screens/file-browser.js createListScreen):
//   Liste      „Jetzt sichern“ (backup.export), „Sicherung auf ein Laufwerk“ (Datei-Explorer im Wahlmodus,
//              ruft backup.export mit dem Ordner), dann die gefundenen Sicherungen (backup.list: Vorgabeordner
//              und oberste Ebene jedes Laufwerks). Kreuz auf einer Sicherung: backup.inspect -> Vorschau.
//   Vorschau   je Teil ein Schalter (Vorgabe alle), „Übernehmen“ mit Rückfrage (Vorgabe Abbrechen), danach
//              backup.apply und die Frage „PC5 Launcher neu starten?“ (Vorgabe Später; power.do restart-session).
//              Kreis führt zurück zur Liste.
// Geprüft, gesichert und angewendet wird im Kern (backup.py); hier wird nur gezeigt und gefragt.
import { h, sizeText } from "../core/dom.js";
import { call } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { router } from "../core/router.js";
import { sound } from "../core/sound.js";
import { S, fmt } from "../core/strings.js";
import { createListScreen } from "./file-browser.js";
import { confirm, dialog, messageDialog } from "../components/dialog.js";
import { toast } from "../components/toast.js";

const PARTS = [
  { id: "settings", label: S.bkPartSettings, count: (a) => fmt(S.bkCountSettings, { n: a.settings }) },
  { id: "games", label: S.bkPartGames, count: (a) => (a.skipped ? fmt(S.bkCountGamesSkipped, { n: a.games, m: a.skipped }) : fmt(S.bkCountGames, { n: a.games })) },
  { id: "order", label: S.bkPartOrder, count: (a) => fmt(S.bkCountOrder, { n: a.order }) },
  { id: "sessions", label: S.bkPartSessions, count: (a) => fmt(S.bkCountSessions, { n: a.sessions }) },
  { id: "art", label: S.bkPartArt, count: (a) => fmt(S.bkCountArt, { n: a.art }) },
  { id: "prefs", label: S.bkPartPrefs, count: () => S.bkCountPrefs },
];

function stamp(seconds) {
  if (!seconds) return "";
  return new Date(seconds * 1000).toLocaleString(S.locale, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

function noteOf(item) {
  if (!item.ok) return S.bkBroken;
  return [item.safety ? S.bkSafety : "", item.drive ? fmt(S.bkOnDrive, { drive: item.drive }) : "", item.system,
    item.version ? fmt(S.bkVersion, { version: item.version }) : ""].filter(Boolean).join(" · ");
}

function create() {
  const label = (text) => h("div.saves-label", { text });
  const block = (title, text) => h("div.bk-block", label(title), h("div.bk-text", { text }));
  const whereEl = h("div.saves-path.is-small.bk-path");
  const listSide = h("div.bk-side", block(S.bkWhat, S.bkWhatText), block(S.bkNot, S.bkNotText),
    h("div.bk-block", label(S.bkWhere), whereEl, h("div.bk-text", { text: S.bkWhereText })));
  const factsEl = h("div.bk-facts");
  const previewSide = h("div.bk-side", { hidden: true }, factsEl, h("div.bk-text", { text: S.bkPreviewSide }));
  const side = h("div.flow-side.bk", listSide, previewSide);
  const screen = createListScreen({
    title: S.bkTitle, sub: S.bkSub, side, cls: "has-side",
    hints: [["btn-cross", S.select], ["btn-circle", S.hintBack]],
    handlers: {
      // Schalter der Vorschau schaltet die Liste selbst (onSet); alles andere hier
      onPress: (row) => (row && row.type === "toggle" ? false : (press(row), true)),
      onSet: (row, value) => Promise.resolve(toggle(row, value)),
    },
  });
  const state = { mode: "list", items: null, dir: "", busy: false, applying: false, preview: null, parts: new Set() };
  let serial = 0;

  // ---------------------------------------------------------------- Liste
  function listRows() {
    const rows = [
      { id: "now", type: "action", label: state.busy ? S.bkBusy : S.bkNow, hint: S.bkNowHint, icon: "archive", enabled: !state.busy },
      { id: "drive", type: "page", label: S.bkDrive, hint: S.bkDriveHint, icon: "storage" },
      { id: "_s", type: "section", label: S.bkFound },
    ];
    if (state.items === null) rows.push({ id: "_l", type: "info", label: S.bkLoading, enabled: false });
    else if (!state.items.length) rows.push({ id: "_none", type: "info", label: S.bkNone, enabled: false });
    for (const item of state.items || []) {
      rows.push({ id: "b:" + item.path, type: "action", label: stamp(item.created) || item.name, value: sizeText(item.size),
        note: noteOf(item), icon: !item.ok ? "warn" : item.safety ? "restart" : "archive", item });
    }
    return rows;
  }

  function paint() {
    const list = state.mode === "list";
    listSide.hidden = !list;
    previewSide.hidden = list;
    if (list) {
      screen.setTitle(S.bkTitle);
      screen.setSub(S.bkSub);
      whereEl.textContent = state.dir || "";
      screen.list.setRows(listRows());
    } else {
      const p = state.preview;
      screen.setTitle(S.bkPreview);
      screen.setSub([stamp(p.item.created), p.item.system].filter(Boolean).join(" · "));
      screen.list.setRows(previewRows());
    }
    nav.ensure();
  }

  async function load() {
    const mine = ++serial;
    let r = null;
    try { r = await call("backup.list", {}); } catch (e) { r = { items: [], message: (e && e.message) || S.errorGeneric }; }
    if (mine !== serial) return;
    state.items = (r && r.items) || [];
    state.dir = (r && r.dir) || "";
    if (state.mode === "list") paint();
  }

  async function exportNow() {
    if (state.busy) return;
    state.busy = true;
    paint();
    let r;
    try { r = await call("backup.export", {}); } catch (e) { r = { ok: false, message: (e && e.message) || S.errorGeneric }; }
    state.busy = false;
    if (r && r.ok) toast({ title: S.bkDone, body: r.message || "", icon: "check" });
    else messageDialog({ title: S.bkTitle, text: (r && r.message) || S.errorGeneric });
    paint();
    load();
  }

  function toDrive() {
    toast({ title: S.bkDrive, body: S.bkPickToast });
    router.go("explorer", { start: null, pick: { label: S.bkPickLabel, method: "backup.export", args: {} } });
  }

  // ---------------------------------------------------------------- Vorschau
  function previewRows() {
    const p = state.preview;
    const rows = [];
    for (const part of PARTS) {
      if (!p.available.has(part.id)) continue;
      rows.push({ id: "part:" + part.id, type: "toggle", label: part.label, value: state.parts.has(part.id),
        note: part.count(p.apply || {}), enabled: !state.applying,
        hint: part.id === "games" && (p.apply || {}).skipped ? S.bkGamesHint : "" });
    }
    rows.push({ id: "apply", type: "action", label: state.applying ? S.bkApplying : S.bkApply, hint: S.bkApplyHint,
      icon: "check", enabled: !state.applying && state.parts.size > 0 });
    return rows;
  }

  function toggle(row, value) {
    const id = String(row.id).slice(5);
    if (value) state.parts.add(id); else state.parts.delete(id);
    return { rows: previewRows() };
  }

  async function openPreview(item) {
    const mine = ++serial;
    let r;
    try { r = await call("backup.inspect", { path: item.path }); } catch (e) { r = { ok: false, message: (e && e.message) || S.errorGeneric }; }
    if (mine !== serial) return;
    if (!r || !r.ok) { messageDialog({ title: S.bkPreview, text: (r && r.message) || S.errorGeneric }); load(); return; }
    const available = new Set((r.parts || []).map((x) => x.id));
    state.preview = { item, apply: r.apply || {}, available, manifest: r.manifest || {} };
    state.parts = new Set(available);
    state.mode = "preview";
    const m = r.manifest || {};
    const fact = (title, text) => (text ? h("div.bk-block", h("div.saves-label", { text: title }), h("div.bk-text", { text })) : null);
    factsEl.replaceChildren(...[fact(S.bkCreated, stamp(m.created || item.created)), fact(S.bkSystem, m.system || item.system),
      fact(S.bkFile, item.path)].filter(Boolean));
    paint();
    const first = screen.list.first();
    if (first) nav.focus(first);
  }

  function closePreview() {
    state.mode = "list";
    state.preview = null;
    paint();
    const node = screen.list.first();
    if (node) nav.focus(node);
    load();
  }

  async function apply() {
    const p = state.preview;
    if (!p || state.applying) return;
    if (!state.parts.size) { messageDialog({ title: S.bkPreview, text: S.bkNothing }); return; }
    if (!await confirm({ title: S.bkConfirmTitle, text: S.bkConfirmText, ok: S.bkApply })) return;
    state.applying = true;
    paint();
    let r;
    try {
      r = await call("backup.apply", { path: p.item.path, parts: PARTS.map((x) => x.id).filter((id) => state.parts.has(id)) });
    } catch (e) { r = { ok: false, message: (e && e.message) || S.errorGeneric }; }
    state.applying = false;
    if (!r || !r.ok) {
      paint();
      messageDialog({ title: S.bkPreview, text: (r && r.message) || S.errorGeneric });
      return;
    }
    closePreview();
    const file = String(r.safety || "").split("/").pop();
    const choice = await dialog({
      title: S.bkRestartTitle, text: r.message || "", detail: file ? fmt(S.bkRestartDetail, { file }) : "",
      buttons: [{ id: "later", label: S.bkRestartLater }, { id: "restart", label: S.bkRestartOk }], initial: "later",
    });
    if (choice === "restart") call("power.do", { id: "restart-session", confirmed: true }).catch(() => {});
  }

  function press(row) {
    if (!row) return;
    if (row.id === "now") exportNow();
    else if (row.id === "drive") toDrive();
    else if (row.id === "apply") apply();
    else if (row.item) openPreview(row.item);
  }

  function onPad(button, ev) {
    if (button === "circle" && state.mode === "preview" && !ev.repeat) {
      if (state.applying) { sound.play("edge"); return true; }
      sound.play("back");
      closePreview();
      return true;
    }
    if (button === "l2" || button === "r2") { screen.list.page(button === "r2" ? 1 : -1); return true; }
    return false;
  }

  paint();
  load();
  return {
    el: screen.el, onPad,
    onShow(info) { nav.ensure(); screen.list.update(); if (info && info.back && state.mode === "list") load(); },
    destroy() { serial += 1; },
  };
}

export const screens = [{ id: "backup", create }];
