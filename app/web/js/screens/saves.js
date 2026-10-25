// Spielstände eines Eintrags (Bauplan E63): wo sie liegen (voller Pfad), sichern, zurückholen.
//   links    Jetzt sichern, Im Datei-Explorer öffnen, Anderen Ordner wählen, die Sicherungen
//   rechts   der Ordner als ganzer Pfad (umbrochen), Dateien/Größe/Änderung, der Sicherungsordner
// Suchen und Packen macht der Kern (saves.*); Zurückholen sichert vorher den jetzigen Stand und
// legt ihn in den Papierkorb, nichts wird endgültig gelöscht. E99: automatische Sicherungen nach
// dem Spielen stehen mit „automatisch“ in der Liste; ob die Automatik an ist, sagt die Seitenleiste.
import { h, sizeText } from "../core/dom.js";
import { call } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { router } from "../core/router.js";
import { S, fmt, whenText } from "../core/strings.js";
import { createListScreen } from "./file-browser.js";
import { openMenu } from "../components/menu.js";
import { confirm, messageDialog } from "../components/dialog.js";
import { toast } from "../components/toast.js";

function stamp(seconds) {
  if (!seconds) return "";
  return new Date(seconds * 1000).toLocaleString(S.locale, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

// Kurzform eines Kandidaten für das Menü: ab dem Windows-Benutzer bzw. ab dem Home-Ordner
function shortPath(path) {
  const m = /\/drive_c\/users\/[^/]+\/(.+)$/.exec(path);
  if (m) return m[1];
  return path.replace(/^\/(var\/)?home\/[^/]+\//, "~/");
}

// Art einer Sicherung (Kern: kind); ältere Antworten nur mit dem Namen
function backupNote(b) {
  const kind = b.kind || (/^auto-/.test(b.name) ? "auto" : /_vor-wiederherstellung/.test(b.name) ? "safety" : "manual");
  return kind === "auto" ? S.svAuto : kind === "safety" ? S.svSafety : "";
}

function create(params = {}) {
  const id = params.id;
  const title = params.title || "";
  const folderEl = h("div.saves-path");
  const statsEl = h("div.saves-stats");
  const backupEl = h("div.saves-path.is-small");
  const autoEl = h("div.saves-stats");
  const side = h("div.flow-side.saves-side",
    h("div.saves-label", { text: S.svFolder }), folderEl, statsEl,
    h("div.saves-label", { text: S.svBackupDir }), backupEl, autoEl);
  const screen = createListScreen({
    title: S.svTitle, sub: title, side, cls: "has-side",
    hints: [["btn-cross", S.select], ["btn-circle", S.hintBack]],
    handlers: { onPress: (row) => { press(row); return true; } },
  });
  let data = null;
  let busy = false;
  let serial = 0;

  function paint() {
    const d = data || {};
    const cur = d.current || null;
    // E99: Switch-Spiel ohne TitleID im Namen: Der Kern sagt, dass der Ordner nach dem Spielen kommt
    const none = d.note ? S.svNone + "\n\n" + d.note : S.svNone;
    folderEl.textContent = d.folder || (d.prefix ? none + "\n\n" + S.svPrefix + ":\n" + d.prefix : none);
    folderEl.classList.toggle("is-none", !d.folder);
    statsEl.textContent = cur ? [fmt(S.svStats, { files: cur.files, size: sizeText(cur.size) || "0 B", when: whenText(cur.mtime) }),
      d.chosen ? S.svChosen : S.svFound].join(" · ") : "";
    statsEl.hidden = !cur;
    backupEl.textContent = d.backupDir || "";
    autoEl.textContent = d.auto === undefined ? "" : (d.auto ? fmt(S.svAutoOn, { keep: d.keep || 5 }) : S.svAutoOff);
    autoEl.hidden = d.auto === undefined;
    // Ohne Ordner gibt es nichts zu sichern: dann steht „Anderen Ordner wählen“ vorn
    const rows = d.folder ? [{ id: "backup", type: "action", label: busy ? S.svBusy : S.svBackupNow, hint: S.svBackupNowHint, icon: "archive", enabled: !busy }] : [];
    if (d.folder || d.prefix) rows.push({ id: "open", type: "page", label: S.svOpen, icon: "folder" });
    rows.push({ id: "choose", type: "page", label: S.svChoose, icon: "search" }, { id: "_s", type: "section", label: S.svBackups });
    const backups = Array.isArray(d.backups) ? d.backups : [];
    if (!backups.length) rows.push({ id: "_none", type: "info", label: S.svNoBackups, enabled: false });
    for (const b of backups) {
      rows.push({ id: "b:" + b.path, type: "action", label: stamp(b.created), value: sizeText(b.size),
        note: backupNote(b), icon: "restart", backup: b });
    }
    const keep = nav.current && screen.list.el.contains(nav.current) ? screen.list.rowOf(nav.current.closest(".row")) : null;
    screen.list.setRows(rows);
    nav.ensure();
    if (keep) { const node = screen.list.nodeOf ? screen.list.nodeOf(keep.id) : null; if (node) nav.focus(node); }
  }

  async function load() {
    const mine = ++serial;
    if (!data) { folderEl.textContent = S.svLoading; screen.list.setRows([{ id: "_l", type: "info", label: S.svLoading, enabled: false }]); }
    let result;
    try { result = await call("saves.info", { id }); } catch (e) { result = { ok: false, message: (e && e.message) || S.errorGeneric }; }
    if (mine !== serial) return;
    if (!result || result.ok === false) {
      messageDialog({ title: S.svTitle, text: (result && result.message) || S.errorGeneric });
      data = data || {};
    } else data = result;
    paint();
  }

  async function backup() {
    if (busy) return;
    busy = true;
    paint();
    let r;
    try { r = await call("saves.backup", { id }); } catch (e) { r = { ok: false, message: (e && e.message) || S.errorGeneric }; }
    busy = false;
    if (r && r.ok) toast({ title: title || S.svTitle, body: r.message || "", icon: "check" });
    else messageDialog({ title: S.svTitle, text: (r && r.message) || S.errorGeneric });
    load();
  }

  async function choose() {
    const items = [];
    for (const c of (data && data.candidates) || []) {
      if (c.path === (data && data.folder)) continue;
      items.push({ id: "c:" + c.path, label: shortPath(c.path), note: fmt(S.svStatsShort, { files: c.files, size: sizeText(c.size) || "0 B" }) });
    }
    items.push({ id: "explorer", label: S.svChooseExplorer, icon: "folder" });
    if (data && data.chosen) items.push({ id: "auto", label: S.svChooseAuto, icon: "restart" });
    const choice = await openMenu(items, { cls: "menu-center", title: S.svChooseTitle });
    if (!choice) return;
    if (choice === "explorer") {
      toast({ title: S.svChooseTitle, body: S.svPickHint });
      router.go("explorer", { start: (data && (data.folder || data.prefix)) || null,
        pick: { label: S.exPickHere, method: "saves.choose", args: { id } } });
      return;
    }
    const path = choice === "auto" ? "" : choice.slice(2);
    let r;
    try { r = await call("saves.choose", { id, path }); } catch (e) { r = { ok: false, message: (e && e.message) || S.errorGeneric }; }
    if (r && r.ok) toast({ title: S.svTitle, body: r.message || "" });
    else messageDialog({ title: S.svTitle, text: (r && r.message) || S.errorGeneric });
    load();
  }

  async function backupMenu(b) {
    const when = stamp(b.created);
    const choice = await openMenu([{ id: "restore", label: S.svRestore, icon: "restart" }, { id: "remove", label: S.svRemove, icon: "trash" }],
      { cls: "menu-center", title: when });
    if (choice === "restore") {
      if (data && data.running) { messageDialog({ title: S.svTitle, text: S.svRunning }); return; }
      // E99: Zurückholen in Edens/Cemus Ordner ist noch nicht freigegeben; der Kern nennt den Grund
      if (data && data.restoreBlocked) { messageDialog({ title: S.svTitle, text: data.restoreBlocked }); return; }
      if (!await confirm({ title: S.svRestoreTitle, text: fmt(S.svRestoreText, { title, when }), ok: S.svRestore })) return;
      const r = await call("saves.restore", { id, path: b.path }).catch((e) => ({ ok: false, message: e && e.message }));
      if (r && r.ok) toast({ title: title || S.svTitle, body: r.message || "", icon: "check" });
      else messageDialog({ title: S.svTitle, text: (r && r.message) || S.errorGeneric });
      load();
    } else if (choice === "remove") {
      if (!await confirm({ title: S.svRemoveTitle, text: fmt(S.svRemoveText, { when }), ok: S.svRemove })) return;
      const r = await call("saves.remove", { id, path: b.path }).catch((e) => ({ ok: false, message: e && e.message }));
      if (!(r && r.ok)) messageDialog({ title: S.svTitle, text: (r && r.message) || S.errorGeneric });
      load();
    }
  }

  function press(row) {
    if (!row) return;
    if (row.id === "backup") backup();
    else if (row.id === "open") router.go("explorer", { start: (data && (data.folder || data.prefix)) || null });
    else if (row.id === "choose") choose();
    else if (row.backup) backupMenu(row.backup);
  }

  load();
  return {
    el: screen.el,
    onShow() { nav.ensure(); screen.list.update(); if (data) load(); },
    destroy() { serial += 1; },
  };
}

export default { id: "saves", create };
