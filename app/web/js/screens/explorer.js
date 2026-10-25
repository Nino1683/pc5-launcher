// Datei-Explorer (Bauplan E62): Orte und Ordner, ganz mit dem Pad.
//   Kreuz     öffnen (Ordner betreten; Bild, Text, Archiv, Video/Musik, Startdatei); ein
//             Installationsprogramm (e.installer, E114) und ein Archiv gehen zur Prüfung des Installers; Textdateien
//             (txt, ini, cfg, json …) bis 2 MB im Text-Editor (screens/text-editor.js, E99)
//   Kreis     einen Ordner hoch, an den Orten zurück
//   Quadrat   markieren (mehrere Dateien für Kopieren, Verschieben, Papierkorb)
//   Dreieck   Aktionen: Kopieren, Verschieben, Hier einfügen, Umbenennen, Neuer Ordner,
//             In den Papierkorb, Zur Bibliothek hinzufügen, Mit Kodi abspielen, Bearbeiten, versteckte Dateien
//   L2/R2     seitenweise
// Die Platte fasst der Kern an (files.*); lange Aufträge melden sich mit files.progress/files.done.
// Wahlmodus (params.pick = {label, method, args}): Dreieck bietet zuerst „Diesen Ordner wählen“;
// das ruft method mit {...args, path} und kehrt zurück (Spielstand-Ordner, E63).
// Gelöscht wird hier nur in den Papierkorb; endgültig erst dort (Ort „Papierkorb“, unten), nach Rückfrage.
import { sizeText } from "../core/dom.js";
import { call, on } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { router } from "../core/router.js";
import { sound } from "../core/sound.js";
import { S, fmt, whenText } from "../core/strings.js";
import { createListScreen } from "./file-browser.js";
import { openMenu } from "../components/menu.js";
import { confirm, imageViewer, logDialog, messageDialog } from "../components/dialog.js";
import { askText } from "../components/osk-input.js";
import { toast } from "../components/toast.js";
import { startById } from "../components/entry-actions.js";
import { isEditable, EDIT_MAX_BYTES } from "./text-editor.js";

const ICONS = { dir: "folder", image: "screen", video: "film", audio: "music", archive: "archive", text: "info",
  pdf: "info", windows: "gamepad", linux: "gamepad", rom: "emulator", file: "more" };
const PLACE_ICONS = { home: "home", storage: "storage", folder: "folder" };
const RUNNABLE = new Set(["windows", "linux"]);
// Papierkorb (E99): Was nicht in die Frist passte, misst der Kern im Hintergrund weiter. Solange
// die Seite offen ist, fragt sie ab und zu nach, bis alle Größen da sind (höchstens 2 Minuten).
const SIZE_POLL_MS = 4000;
const SIZE_POLLS = 30;

// Zwischenablage über Bildschirmwechsel hinweg: hier „Kopieren“, in einem anderen Ordner einfügen
let clip = null;          // {op: "copy" | "move", paths: [...]}

function create(params = {}) {
  const screen = createListScreen({
    title: S.exTitle,
    hints: hintsFor(false),
    handlers: { emptyText: S.exEmpty, onPress: (row) => { if (row && row.trash) router.go("explorer-trash"); else open(row && row.entry); return true; } },
  });
  const { list } = screen;
  let current = { path: null, parent: null, name: "" };
  let entries = [];
  const marked = new Set();
  let hidden = false;
  let serial = 0;
  const jobs = new Map();       // jobId -> Satz für die Unterzeile
  // files.done kann vor der Antwort auf files.op kommen (E99: rename und mkdir sind sofort fertig);
  // solange ein files.op unterwegs ist, wartet ein unbekanntes Ende hier auf seine Antwort
  let pending = 0;
  const early = new Map();      // jobId -> files.done
  const offs = [];

  function hintsFor(inFolder) {
    return inFolder
      ? [["btn-cross", S.open], ["btn-square", S.exMark], ["btn-triangle", S.exActions], ["btn-circle", S.hintUp]]
      : [["btn-cross", S.open], ["btn-circle", S.hintBack]];
  }

  function detail(e) {
    return [e.type === "dir" ? "" : sizeText(e.size), whenText(e.mtime)].filter(Boolean).join(" · ");
  }

  function rowOf(e) {
    if (!current.path) return { id: e.path, type: "page", label: e.name, icon: PLACE_ICONS[e.kind] || "storage", entry: e };
    const mark = marked.has(e.path);
    return { id: e.path, type: e.type === "dir" ? "page" : "action", label: e.name, value: detail(e),
      icon: mark ? "check" : (ICONS[e.kind] || "more"), note: mark ? S.exMarked : "", entry: e };
  }

  function subText() {
    const busy = [...jobs.values()];
    if (busy.length) return busy.join(" · ");
    const where = current.path ? current.path : S.exPlaces;
    const extra = [marked.size ? fmt(S.exMarkedN, { n: marked.size }) : "", clip ? fmt(S.exClip, { n: clip.paths.length }) : ""];
    return [where, ...extra].filter(Boolean).join(" · ");
  }

  function paint(result, keepId) {
    entries = (result.entries || []).filter((e) => e && typeof e === "object" && e.path);
    const rows = entries.map(rowOf);
    if (result.ok === false) rows.unshift({ id: "_err", type: "info", label: result.message || S.fbFailed, enabled: false });
    if (result.truncated) rows.push({ id: "_more", type: "info", label: fmt(S.fbTruncated, { n: entries.length, total: result.total }), enabled: false });
    if (!current.path && result.ok !== false) rows.push({ id: "_trash", type: "page", label: S.exTrashPlace, icon: "trash", trash: true });
    list.setRows(rows);
    screen.setSub(subText());
    nav.ensure();
    if (keepId) { const node = list.nodeOf ? list.nodeOf(keepId) : null; if (node) nav.focus(node); }
  }

  async function go(path, keepId) {
    const mine = ++serial;
    if (list.setLoading) list.setLoading();
    let result;
    try {
      result = await call("files.list", path ? { path, hidden } : { hidden });
    } catch (e) {
      result = { ok: false, message: (e && e.message) || S.fbFailed, entries: [] };
    }
    if (mine !== serial) return;
    const moved = (result.path || null) !== current.path;
    current = { path: result.path || null, parent: result.parent || null, name: result.name || "" };
    if (moved) marked.clear();
    screen.setTitle(current.path ? current.name || S.exTitle : S.exTitle);
    screen.setHints(hintsFor(!!current.path));
    paint(result, keepId);
  }

  function refresh() { const f = focused(); go(current.path, f ? f.path : undefined); }

  function focused() {
    const node = nav.current;
    if (!node || !list.el.contains(node)) return null;
    const row = list.rowOf(node.closest(".row"));
    return row && row.entry ? row.entry : null;
  }

  function selection() {
    if (marked.size) return entries.filter((e) => marked.has(e.path));
    const f = focused();
    return f && current.path ? [f] : [];
  }

  // ---------------------------------------------------------------- Öffnen
  async function open(e) {
    if (!e) return;
    if (e.type === "dir") { go(e.path); return; }
    if (e.kind === "image") { viewImage(e); return; }
    if (e.kind === "archive" || e.installer) { router.go("install-check", { path: e.path, name: e.name }); return; }
    // Text-Editor (E99); größere Dateien wie bisher als Ausschnitt (64 KB)
    if (isEditable(e.name) && Number(e.size) <= EDIT_MAX_BYTES) { router.go("text-editor", { path: e.path, name: e.name }); return; }
    if (e.kind === "text") { showText(e); return; }
    if (e.kind === "video" || e.kind === "audio") { play(e); return; }
    if (RUNNABLE.has(e.kind)) { runFile(e); return; }
    messageDialog({ title: e.name, text: S.exNoOpen, detail: detail(e) });
  }

  async function showText(e) {
    try {
      const r = await call("files.text", { path: e.path });
      if (!r || r.ok === false) { messageDialog({ title: e.name, text: (r && r.message) || S.fbFailed }); return; }
      logDialog({ title: e.name, text: r.text + (r.truncated ? "\n…" : "") });
    } catch (err) { messageDialog({ title: e.name, text: (err && err.message) || S.fbFailed }); }
  }

  async function play(e) {
    try {
      const r = await call("files.play", { path: e.path });
      if (r && r.ok === false) messageDialog({ title: e.name, text: r.message || S.fbFailed });
    } catch (err) { messageDialog({ title: e.name, text: (err && err.message) || S.fbFailed }); }
  }

  // Startdatei starten (E93): als Eintrag der Bibliothek (vorhanden oder neu), über den Startweg des Home
  async function runFile(e) {
    try {
      const r = await call("files.run", { path: e.path });
      if (!r || r.ok === false) { messageDialog({ title: e.name, text: (r && r.message) || S.fbFailed }); return; }
      startById(r.id);
    } catch (err) { messageDialog({ title: e.name, text: (err && err.message) || S.fbFailed }); }
  }

  async function addToLibrary(e) {
    const yes = await confirm({ title: S.exAddTitle, text: fmt(S.exAddText, { name: e.name }), ok: S.exAddLib });
    if (!yes) return;
    try {
      const r = await call("files.add", { path: e.path });
      toast({ title: r && r.ok ? S.exAddLib : S.fbFailed, body: (r && r.message) || "" });
    } catch (err) { toast({ title: S.fbFailed, body: (err && err.message) || "" }); }
  }

  // Bilder: großes Bild, links/rechts blättern, Kreis schließt (gemeinsamer Betrachter, E99)
  function viewImage(start) {
    const images = entries.filter((e) => e.kind === "image");
    imageViewer({
      count: images.length,
      start: Math.max(0, images.findIndex((e) => e.path === start.path)),
      src: async (i) => { const r = await call("files.image", { path: images[i].path }); return r && r.url ? r.url : null; },
      caption: (i) => fmt(S.exImageOf, { name: images[i].name, n: i + 1, total: images.length }),
    });
  }

  // ---------------------------------------------------------------- Markieren und Aktionen
  function toggleMark() {
    const e = focused();
    if (!e || !current.path) return;
    if (marked.has(e.path)) marked.delete(e.path); else marked.add(e.path);
    sound.play(marked.has(e.path) ? "toggle-on" : "toggle-off");
    list.setRows(list.rows.map((row) => (row.entry ? rowOf(row.entry) : row)));
    screen.setSub(subText());
  }

  async function actions() {
    const sel = selection();
    const one = sel.length === 1 ? sel[0] : null;
    const items = [];
    if (params.pick && current.path) items.push({ id: "pick", label: params.pick.label || S.exPickHere, icon: "check" }, { sep: true });
    if (clip && current.path) items.push({ id: "paste", label: fmt(clip.op === "move" ? S.exPasteMove : S.exPaste, { n: clip.paths.length }), icon: "download" });
    if (clip) items.push({ id: "unclip", label: S.exUnclip });
    if (sel.length) {
      items.push({ id: "copy", label: sel.length > 1 ? fmt(S.exCopyN, { n: sel.length }) : S.exCopy });
      items.push({ id: "move", label: sel.length > 1 ? fmt(S.exMoveN, { n: sel.length }) : S.exMove });
      if (one) items.push({ id: "rename", label: S.exRename });
      if (one && (one.kind === "archive" || one.installer)) items.push({ id: "install", label: S.exInstall, icon: "archive" });
      if (one && RUNNABLE.has(one.kind)) items.push({ id: "run", label: S.exRun, icon: "play" }, { id: "add", label: S.exAddLib, icon: "library" });
      if (one && (one.kind === "video" || one.kind === "audio")) items.push({ id: "play", label: S.exPlay, icon: "play" });
      // wie Kreuz (E99): größere Textdateien öffnen nur als Ausschnitt, nicht im Editor
      if (one && one.type !== "dir" && isEditable(one.name) && Number(one.size) <= EDIT_MAX_BYTES) items.push({ id: "edit", label: S.teOpenEdit, icon: "keyboard" });
      items.push({ id: "trash", label: sel.length > 1 ? fmt(S.exTrashN, { n: sel.length }) : S.exTrash, icon: "trash" });
    }
    if (current.path) {
      items.push({ sep: true });
      items.push({ id: "mkdir", label: S.exNewFolder, icon: "folder" });
      if (entries.length) items.push({ id: marked.size ? "unmark" : "markall", label: marked.size ? S.exUnmark : S.exMarkAll });
      items.push({ id: "hidden", label: hidden ? S.exHiddenOff : S.exHiddenOn });
      for (const jobId of jobs.keys()) items.push({ id: "cancel:" + jobId, label: S.exCancelJob });
    }
    if (!items.length) return;
    const choice = await openMenu(items, { cls: "menu-center", title: one ? one.name : current.name || S.exTitle });
    if (!choice) return;
    if (choice === "pick") { pick(); return; }
    if (choice === "copy" || choice === "move") {
      clip = { op: choice, paths: sel.map((e) => e.path) };
      marked.clear();
      toast({ title: choice === "copy" ? S.exCopy : S.exMove, body: fmt(S.exClipNote, { n: clip.paths.length }) });
      list.setRows(list.rows.map((row) => (row.entry ? rowOf(row.entry) : row)));
      screen.setSub(subText());
    } else if (choice === "paste") {
      const job = clip;
      if (await run(job.op, job.paths, { dest: current.path }) && job.op === "move") clip = null;
    } else if (choice === "unclip") {
      clip = null;
      screen.setSub(subText());
    } else if (choice === "rename") {
      const name = await askName(S.exRenameTitle, one.name);
      if (name && name !== one.name) run("rename", [one.path], { name });
    } else if (choice === "mkdir") {
      const name = await askName(S.exNewFolderTitle, S.exNewFolderName);
      if (name) run("mkdir", [], { dest: current.path, name });
    } else if (choice === "trash") {
      const yes = await confirm({ title: S.exTrashTitle, text: fmt(S.exTrashText, { what: one ? "„" + one.name + "“" : fmt(S.exItems, { n: sel.length }) }), ok: S.exTrash });
      if (yes) run("trash", sel.map((e) => e.path));
    } else if (choice === "install") router.go("install-check", { path: one.path, name: one.name });
    else if (choice === "run") runFile(one);
    else if (choice === "add") addToLibrary(one);
    else if (choice === "play") play(one);
    else if (choice === "edit") router.go("text-editor", { path: one.path, name: one.name });
    else if (choice === "markall") { entries.forEach((e) => marked.add(e.path)); list.setRows(list.rows.map((row) => (row.entry ? rowOf(row.entry) : row))); screen.setSub(subText()); }
    else if (choice === "unmark") { marked.clear(); list.setRows(list.rows.map((row) => (row.entry ? rowOf(row.entry) : row))); screen.setSub(subText()); }
    else if (choice === "hidden") { hidden = !hidden; refresh(); }
    else if (choice.startsWith("cancel:")) call("files.cancel", { jobId: choice.slice(7) }).catch(() => {});
  }

  async function pick() {
    const here = router.top;
    let r;
    try { r = await call(params.pick.method, { ...(params.pick.args || {}), path: current.path }); } catch (e) { r = { ok: false, message: e && e.message }; }
    if (!r || r.ok === false) { messageDialog({ title: S.exTitle, text: (r && r.message) || S.fbFailed }); return; }
    if (r.message) toast({ title: current.name || S.exTitle, body: r.message, icon: "check" });
    if (router.top === here) router.back();
  }

  // Name über die Bildschirmtastatur (components/osk-input.js, E99); null bei Abbruch oder Fehler
  async function askName(title, initial) {
    try {
      const text = await askText({ title, initial: initial || "" });
      return text === null ? null : text.trim();
    } catch (e) { return null; }
  }

  async function run(op, paths, extra = {}) {
    pending += 1;
    try {
      const r = await call("files.op", { op, paths, ...extra });
      if (!r || r.ok === false) { messageDialog({ title: S.exTitle, text: (r && r.message) || S.fbFailed }); return false; }
      if (early.has(r.jobId)) { const p = early.get(r.jobId); early.delete(r.jobId); finished(p); return true; }
      jobs.set(r.jobId, S["exBusy_" + op] || S.exBusy);
      screen.setSub(subText());
      return true;
    } catch (e) { messageDialog({ title: S.exTitle, text: (e && e.message) || S.fbFailed }); return false; }
    finally {
      pending -= 1;
      if (!pending) early.clear();
    }
  }

  function finished(p) {
    jobs.delete(p.jobId);
    if (p.ok) { marked.clear(); toast({ title: S.exTitle, body: p.message || "" }); }
    else if (!p.cancelled) messageDialog({ title: S.exTitle, text: p.message || S.fbFailed });
    refresh();
  }

  offs.push(on("files.progress", (p) => {
    if (!p || !jobs.has(p.jobId)) return;
    const pct = p.total > 0 ? Math.min(100, Math.round(p.done * 100 / p.total)) : null;
    jobs.set(p.jobId, (S["exBusy_" + p.op] || S.exBusy) + (pct === null ? "" : " " + pct + " %") + (p.current ? " · " + p.current : ""));
    screen.setSub(subText());
  }));
  offs.push(on("files.done", (p) => {
    if (!p) return;
    if (jobs.has(p.jobId)) finished(p);
    else if (pending) early.set(p.jobId, p);
  }));

  function up() {
    if (current.path === null) { router.back(); return; }
    sound.play("back");
    go(current.parent, current.path);
  }

  go(params.start || null);
  return {
    el: screen.el,
    onPad(button, ev) {
      if (button === "circle" && !ev.repeat) { up(); return true; }
      if (button === "square" && !ev.repeat) { toggleMark(); return true; }
      if (button === "triangle" && !ev.repeat) { actions(); return true; }
      if ((button === "l2" || button === "r2") && list.page) { list.page(button === "r2" ? 1 : -1); return true; }
      return false;
    },
    // Zurück aus dem Text-Editor: Größe und Datum der gespeicherten Datei neu lesen
    onShow(info) { nav.ensure(); list.update(); if (info && info.back && info.from === "text-editor" && current.path) refresh(); },
    destroy() { serial += 1; offs.forEach((off) => off()); },
  };
}

// ---------------------------------------------------------------- Papierkorb (2026-10-21)
// Was der Explorer (oder der Desktop) in den Papierkorb gelegt hat, von allen Laufwerken (Kern:
// xdgtrash.py). Kreuz: zurücklegen oder endgültig löschen; Dreieck: Papierkorb leeren. Endgültig
// nur nach Rückfrage; die Aufträge melden sich wie im Explorer mit files.progress/files.done.
function createTrash() {
  const screen = createListScreen({
    title: S.exTrashPlace,
    hints: [["btn-cross", S.select], ["btn-triangle", S.exTrashEmptyAll], ["btn-circle", S.hintBack]],
    handlers: { emptyText: S.exTrashEmpty, onPress: (row) => { press(row); return true; } },
  });
  const { list } = screen;
  let items = [];
  let size = 0;
  let sized = false;              // Größe in der Unterzeile nur, wenn alle Einträge gemessen sind
  let serial = 0;
  let poll = 0;                   // Zeitgeber: Größen nachfragen
  let jobId = null;               // laufender Auftrag (zurücklegen, löschen, leeren)
  let starting = false;           // files.op unterwegs: files.done kann vor der Antwort kommen
  const early = new Map();
  let busy = "";
  const offs = [];

  function sub() {
    if (busy) return busy;
    if (!items.length) return "";
    if (!sized) return fmt(items.length === 1 ? S.exTrashSubOneCount : S.exTrashSubCount, { n: items.length });
    return fmt(items.length === 1 ? S.exTrashSubOne : S.exTrashSub, { n: items.length, size: sizeText(size) || "0 B" });
  }

  function rowOf(it) {
    const cut = it.orig ? it.orig.lastIndexOf("/") : -1;
    const folder = cut > 0 ? it.orig.slice(0, cut) : cut === 0 ? "/" : "";
    const value = [typeof it.size === "number" ? sizeText(it.size) : "", whenText(it.deleted)].filter(Boolean).join(" · ");
    return { id: it.id, type: "action", label: it.name, value, note: folder ? fmt(S.exTrashFrom, { folder }) : "",
      icon: it.dir ? "folder" : "more", item: it };
  }

  // Erst die Einträge ohne Größen (sofort), dann mit (E99): Große Ordner zu messen dauert, und
  // so lange blieb die Seite leer. Der Kern merkt sich gemessene Größen. again: Nachfrage, bis
  // auch die im Hintergrund gemessenen da sind.
  async function load(again = 0) {
    const mine = ++serial;
    clearTimeout(poll);
    if (!again && list.setLoading) list.setLoading();
    for (const quick of again ? [false] : [true, false]) {
      let r;
      try { r = await call("files.trashList", quick ? { sizes: false } : {}); } catch (e) { r = { ok: false, message: (e && e.message) || S.fbFailed, items: [] }; }
      if (mine !== serial) return;
      items = (r && Array.isArray(r.items) ? r.items : []).filter((it) => it && it.id);
      size = Number(r && r.size) || 0;
      sized = !quick && !!r && r.ok !== false && r.complete !== false;
      const rows = items.map(rowOf);
      if (r && r.ok === false) rows.unshift({ id: "_err", type: "info", label: r.message || S.fbFailed, enabled: false });
      list.setRows(rows);
      screen.setSub(sub());
      nav.ensure();
      if (!items.length || (r && r.ok === false)) return;
      if (!quick && !sized && again < SIZE_POLLS) poll = setTimeout(() => { if (mine === serial) load(again + 1); }, SIZE_POLL_MS);
    }
  }

  function finished(p) {
    jobId = null;
    busy = "";
    if (p.ok) toast({ title: S.exTrashPlace, body: p.message || "" });
    else if (!p.cancelled) messageDialog({ title: S.exTrashPlace, text: p.message || S.fbFailed });
    load();
  }

  async function start(op, ids) {
    starting = true;
    early.clear();
    try {
      const r = await call("files.op", { op, paths: ids });
      if (!r || r.ok === false) { messageDialog({ title: S.exTrashPlace, text: (r && r.message) || S.fbFailed }); return; }
      jobId = r.jobId;
      busy = S["exBusy_" + op] || S.exBusy;
      screen.setSub(sub());
      if (early.has(jobId)) finished(early.get(jobId));
    } catch (e) {
      messageDialog({ title: S.exTrashPlace, text: (e && e.message) || S.fbFailed });
    } finally {
      starting = false;
      early.clear();
    }
  }

  async function press(row) {
    const it = row && row.item;
    if (!it || jobId || starting) return;
    const choice = await openMenu([{ id: "restore", label: S.exTrashRestore, icon: "restart" }, { id: "purge", label: S.exTrashPurge, icon: "trash" }],
      { cls: "menu-center", title: it.name });
    if (choice === "restore") start("restore", [it.id]);
    else if (choice === "purge") {
      const yes = await confirm({ title: S.exTrashPurgeTitle, text: fmt(S.exTrashPurgeText, { name: it.name }), ok: S.exTrashPurge });
      if (yes) start("purge", [it.id]);
    }
  }

  async function emptyAll() {
    if (!items.length || jobId || starting) return;
    const text = sized ? fmt(S.exTrashEmptyText, { n: items.length, size: sizeText(size) || "0 B" }) : fmt(S.exTrashEmptyTextCount, { n: items.length });
    const yes = await confirm({ title: S.exTrashEmptyTitle, text, ok: S.exTrashEmptyAll });
    if (yes) start("empty", []);
  }

  offs.push(on("files.progress", (p) => {
    if (!p || p.jobId !== jobId) return;
    const pct = p.total > 0 ? Math.min(100, Math.round(p.done * 100 / p.total)) : null;
    busy = (S["exBusy_" + p.op] || S.exBusy) + (pct === null ? "" : " " + pct + " %");
    screen.setSub(sub());
  }));
  offs.push(on("files.done", (p) => {
    if (!p) return;
    if (p.jobId === jobId) finished(p);
    else if (starting) early.set(p.jobId, p);
  }));

  load();
  return {
    el: screen.el,
    onPad(button, ev) {
      if (button === "triangle" && !ev.repeat) { emptyAll(); return true; }
      if ((button === "l2" || button === "r2") && list.page) { list.page(button === "r2" ? 1 : -1); return true; }
      return false;
    },
    onShow() { nav.ensure(); list.update(); },
    destroy() { serial += 1; clearTimeout(poll); offs.forEach((off) => off()); },
  };
}

export const trashScreen = { id: "explorer-trash", create: createTrash };
export default { id: "explorer", create };
