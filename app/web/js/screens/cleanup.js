// Platz schaffen (E110, Teil P13). Bildschirm-IDs (Bauplan docs/research/e103/SPEC.md 4.4):
//   cleanup
// Einstieg: Einstellungen › Speicher › „Platz schaffen“ (settings.action -> {ui: "cleanup"}).
// Daten von storage.cleanupScan (Arbeits-Thread, liest nur, höchstens rund 20 s): Gruppen mit Größe, je Eintrag
// eine Zeile. Gelöscht wird nur über die vorhandenen Wege, jeweils nach einer Rückfrage mit Vorgabe Abbrechen
// (Größe und voller Pfad in der Rückfrage):
//   purge   install.purge {trashId}              Papierkorb der Konsole; Kopf „Jetzt leeren“ = alle Einträge
//   empty   files.op {op: "empty"}               Papierkorb des Systems (Kopf „Papierkorb leeren“)
//   trash   files.op {op: "trash", paths}        Archive schon installierter Spiele (wiederherstellbar)
//   open    Datei-Explorer an dieser Stelle      große Downloads
//   show    nur anzeigen                         Prefixe ohne Spiel, Steams Shader-Zwischenspeicher
// Danach storage.cleanupScan neu (Antwort bzw. install.purgeDone, files.done).
import { h, sizeText } from "../core/dom.js";
import { call, on } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { router } from "../core/router.js";
import { S, fmt } from "../core/strings.js";
import { dialog } from "../components/dialog.js";
import { toast } from "../components/toast.js";
import { createListScreen } from "./file-browser.js";

const ICONS = { "console-trash": "trash", "system-trash": "trash", archives: "archive", downloads: "download",
  prefixes: "folder", shadercache: "storage" };

function ageText(days) {
  if (days === null || days === undefined || !Number.isFinite(Number(days))) return "";
  const n = Math.max(0, Math.floor(Number(days)));
  return n === 0 ? S.clAgeToday : n === 1 ? S.clAgeYesterday : fmt(S.clAgeDays, { n });
}

function freeText(free) {
  const parts = (free || []).filter((v) => v && Number(v.free) >= 0).map((v) => (v.id === "home"
    ? fmt(S.clFreeHome, { size: sizeText(v.free) || "0 B" }) : fmt(S.clFreeOn, { size: sizeText(v.free) || "0 B", label: v.label || "" })));
  return parts.length ? fmt(S.clFree, { list: parts.join(S.clFreeSep) }) : "";
}

// Rückfrage mit Vorgabe Abbrechen; der Pfad steht umbrochen darunter (cleanup.css)
async function ask({ title, text, size, path, ok }) {
  const detail = h("div.cl-confirm", size ? h("div.cl-confirm-size", { text: size }) : null,
    path ? h("div.cl-confirm-path", { text: path }) : null);
  const result = await dialog({ title, text, content: detail, wide: true,
    buttons: [{ id: "cancel", label: S.cancel }, { id: "ok", label: ok }], initial: "cancel" });
  return result === "ok";
}

function create() {
  const screen = createListScreen({
    title: S.clTitle,
    hints: [["btn-cross", S.select], ["btn-circle", S.hintBack]],
    cls: "is-cleanup",
    handlers: { emptyText: S.clNothing, onPress: (row) => { press(row); return true; } },
  });
  const { list } = screen;
  let data = null;
  let serial = 0;
  let closed = false;
  const jobs = new Set();         // eigene files.op-Aufträge: bei files.done neu suchen

  function rowsOf(groups) {
    const rows = [];
    for (const g of groups || []) {
      const section = g.size > 0 ? fmt(S.clSection, { title: g.title, size: sizeText(g.size) }) : g.title;
      const items = g.items || [];
      if (items.length && (g.action === "purge" || g.action === "empty")) {
        rows.push({ id: "group:" + g.id, type: "action", label: g.action === "purge" ? S.clPurgeAll : S.clEmpty, value: sizeText(g.size),
          icon: "trash", hint: g.note || "", section, group: g });
      }
      for (const it of items) {
        const age = ageText(it.age_days);
        const when = !age ? "" : g.id === "console-trash" || g.id === "system-trash" ? fmt(S.clDeletedAge, { age })
          : g.id === "downloads" || g.id === "archives" ? age : fmt(S.clChangedAge, { age });
        const note = [it.note, when].filter(Boolean).join(" · ");
        const acts = it.action === "purge" || it.action === "trash" || it.action === "open";
        // Im Fokus der volle Pfad und was Kreuz hier tut; Nur-Anzeige-Zeilen werden so auch fokussierbar
        const what = it.action === "open" ? S.clHintOpen : acts ? "" : g.note || S.clHintShow;
        rows.push({ id: it.id, type: acts ? "action" : "info", label: it.title, value: sizeText(it.size), note,
          icon: ICONS[g.id] || "storage", section, hint: [it.path, what].filter(Boolean).join(" · "), item: it, group: g });
      }
      if (g.more > 0) rows.push({ id: "more:" + g.id, type: "info", label: fmt(S.clMore, { n: g.more }), enabled: false, section });
      if (!items.length) rows.push({ id: "none:" + g.id, type: "info", label: g.note || S.clNothing, enabled: false, section });
    }
    return rows;
  }

  function sub() {
    if (!data) return S.clScanning;
    return [freeText(data.free), data.complete === false ? S.clIncomplete : ""].filter(Boolean).join(S.clFreeSep);
  }

  async function load() {
    const mine = ++serial;
    screen.setSub(S.clScanning);
    if (!data) list.setLoading();
    let result = null;
    try { result = await call("storage.cleanupScan"); } catch (e) { result = null; }
    if (mine !== serial || closed) return;
    if (!result || !Array.isArray(result.groups)) {
      screen.setSub(S.clFailed);
      list.setRows([{ id: "failed", type: "info", label: S.clFailed, enabled: false }]);
      nav.ensure();
      return;
    }
    data = result;
    screen.setSub(sub());
    list.setRows(rowsOf(result.groups));
    nav.ensure();
  }

  function report(result, fallback) {
    if (result && result.ok === false) toast({ title: S.clTitle, body: result.message || S.errorGeneric, icon: "warn" });
    else if (fallback) toast({ title: S.clTitle, body: fallback, icon: "trash", sound: false });
  }

  async function filesOp(params) {
    let result;
    try { result = await call("files.op", params); } catch (e) { result = { ok: false, message: e && e.message }; }
    if (result && result.ok !== false && result.jobId) jobs.add(result.jobId);
    report(result, "");
    load();
  }

  async function purge(ids) {
    let failed = false;
    for (const trashId of ids) {
      let result;
      try { result = await call("install.purge", { trashId }); } catch (e) { result = { ok: false, message: e && e.message }; }
      if (result && result.ok === false) { report(result); failed = true; break; }
    }
    if (!failed) toast({ title: S.clTitle, body: S.clPurgeStarted, icon: "trash", sound: false });
    load();
  }

  async function press(row) {
    if (!row) return;
    const it = row.item;
    const g = row.group;
    if (row.id === "group:console-trash" && g) {
      // alle Einträge, auch die, die über die Liste hinausgehen (install.trashList wie der Papierkorb)
      let all = [];
      try { all = ((await call("install.trashList")) || {}).items || []; } catch (e) { all = []; }
      const ids = all.filter((i) => i && i.present && !i.purging).map((i) => i.trash_id);
      if (!ids.length) { load(); return; }
      // Größen noch nicht gemessen (Kern: „Größen werden noch gemessen.“): dann nur die Zahl nennen
      const size = (g.items || []).every((i) => Number(i.size) > 0) ? sizeText(g.size) : "";
      const text = !size ? fmt(S.clPurgeAllTextCount, { n: ids.length })
        : ids.length === 1 ? fmt(S.clPurgeAllTextOne, { size }) : fmt(S.clPurgeAllText, { n: ids.length, size });
      const yes = await ask({ title: S.clPurgeAllTitle, text, ok: S.clPurgeAllOk });
      if (yes) purge(ids);
      return;
    }
    if (row.id === "group:system-trash" && g) {
      const yes = await ask({ title: S.clEmptyTitle, text: fmt(S.clEmptyText, { n: g.count || (g.items || []).length, size: sizeText(g.size) || "0 B" }),
        ok: S.clEmptyOk });
      if (yes) filesOp({ op: "empty" });
      return;
    }
    if (!it) return;
    if (it.action === "open") {
      router.go("explorer", { start: it.ref });
      return;
    }
    if (it.action === "purge") {
      const yes = await ask({ title: fmt(S.clPurgeTitle, { title: it.title }), text: S.clPurgeText, size: sizeText(it.size), path: it.path,
        ok: S.clPurgeOk });
      if (yes) purge([it.ref]);
      return;
    }
    if (it.action === "trash" && Array.isArray(it.ref) && it.ref.length) {
      const text = it.ref.length > 1 ? fmt(S.clTrashTextParts, { n: it.ref.length }) : S.clTrashText;
      // jeder Teil mit vollem Pfad; sehr viele Teile: die ersten vier und die Zahl der übrigen
      const shown = it.ref.slice(0, 4).concat(it.ref.length > 4 ? [fmt(S.clMore, { n: it.ref.length - 4 })] : []);
      const yes = await ask({ title: fmt(S.clTrashTitle, { title: it.title }), text, size: sizeText(it.size), path: shown.join("\n"), ok: S.clTrashOk });
      if (yes) filesOp({ op: "trash", paths: it.ref });
    }
  }

  const offs = [
    on("install.purgeDone", () => load()),
    on("files.done", (p) => { if (p && jobs.has(p.jobId)) { jobs.delete(p.jobId); report(p, p.ok ? S.clDone : ""); load(); } }),
  ];
  load();
  return {
    el: screen.el,
    onPad(button) {
      if ((button === "l2" || button === "r2") && list.page) { list.page(button === "r2" ? 1 : -1); return true; }
      return false;
    },
    onShow(info) { nav.ensure(); list.update(); if (info && info.back) load(); },
    destroy() { closed = true; serial += 1; offs.forEach((off) => off()); },
  };
}

export const screens = [{ id: "cleanup", create }];
