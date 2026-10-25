// Papierkorb des Archiv-Installers (Bauplan E17/E48): gelöschte Archiv-Spiele, wiederherstellen
// oder endgültig löschen (nur nach Rückfrage). install.purge antwortet sofort; Verlauf und Ende
// kommen als install.purgeProgress und install.purgeDone.
import { sizeText } from "../core/dom.js";
import { call, on } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { S, fmt, whenText } from "../core/strings.js";
import { confirm } from "../components/dialog.js";
import { openMenu } from "../components/menu.js";
import { toast } from "../components/toast.js";
import { createListScreen } from "./file-browser.js";

function dateText(stamp) {
  return stamp ? new Date(stamp * 1000).toLocaleDateString(S.locale, { day: "numeric", month: "long", year: "numeric" }) : "";
}

function create() {
  const screen = createListScreen({
    title: S.trTitle,
    hints: [["btn-cross", S.select], ["btn-circle", S.hintBack]],
    handlers: { emptyText: S.trEmpty, onPress: (row) => { press(row); return true; } },
  });
  let items = [];
  const progress = new Map();     // trash_id -> Prozent beim endgültigen Löschen
  // install.purgeDone kann vor der Antwort auf install.purge kommen (E99): Dann setzt die Antwort
  // den Fortschritt nicht mehr, sonst bliebe der Eintrag gesperrt bei „Wird gelöscht 0 %“
  const ended = new Set();
  let serial = 0;

  function rowOf(item) {
    const pct = progress.has(item.trash_id) ? progress.get(item.trash_id) : item.purging ? Number(item.percent) || 0 : null;
    const note = pct !== null ? fmt(S.trPurging, { n: Math.round(pct) })
      : !item.present ? S.trMissing : item.expires ? fmt(S.trExpires, { date: dateText(item.expires) }) : "";
    // Größe je Eintrag (install.trashList.items[].size_bytes): so viel macht „Endgültig löschen“ frei
    const value = [fmt(S.trDeleted, { when: whenText(item.deleted) }), sizeText(item.size_bytes)].filter(Boolean).join(" · ");
    return { id: item.trash_id, type: pct !== null ? "info" : "action", label: item.title || item.name, value,
      note, icon: "trash", enabled: pct === null, item };
  }

  async function load() {
    const mine = ++serial;
    let result = null;
    try { result = await call("install.trashList"); } catch (e) { result = null; }
    if (mine !== serial) return;
    items = (result && result.items) || [];
    // Was der Kern nicht mehr als „wird gelöscht“ meldet, ist auch hier nicht mehr gesperrt
    for (const id of [...progress.keys()]) if (!items.some((i) => i.trash_id === id && i.purging)) progress.delete(id);
    screen.setSub(result ? fmt(S.trSub, { days: result.days || 14 }) : S.errorGeneric);
    screen.list.setRows(items.map(rowOf));
    nav.ensure();
  }

  async function press(row) {
    const item = row && row.item;
    if (!item) return;
    const choice = await openMenu([
      { id: "restore", label: S.trRestore, icon: "restart" },
      { id: "purge", label: S.trPurge, icon: "trash" },
    ], { cls: "menu-center", title: item.title || item.name });
    if (choice === "restore") {
      try {
        const result = await call("install.restore", { trashId: item.trash_id });
        toast({ title: S.trTitle, body: (result && result.message) || S.errorGeneric, icon: result && result.ok ? "restart" : "warn", sound: false });
      } catch (e) { toast({ title: S.trTitle, body: e.message || S.errorGeneric, icon: "warn" }); }
      load();
    } else if (choice === "purge") {
      // Größe nennt die Rückfrage, sobald der Kern sie im Papierkorb mitliefert
      const size = sizeText(item.size_bytes || item.size || item.bytes);
      const text = [S.trPurgeText, size ? fmt(S.trPurgeSize, { size }) : ""].filter(Boolean).join("\n");
      const yes = await confirm({ title: fmt(S.trPurgeTitle, { title: item.title || item.name }), text, ok: S.trPurge });
      if (!yes) return;
      ended.delete(item.trash_id);
      try {
        const result = await call("install.purge", { trashId: item.trash_id });
        if (result && result.ok === false) toast({ title: S.trTitle, body: result.message || S.errorGeneric, icon: "warn" });
        else if (!ended.has(item.trash_id)) { progress.set(item.trash_id, 0); screen.list.setRows(items.map(rowOf)); }
      } catch (e) { toast({ title: S.trTitle, body: e.message || S.errorGeneric, icon: "warn" }); }
    }
  }

  const offs = [
    on("install.purgeProgress", (p) => {
      if (!p || !items.some((i) => i.trash_id === p.trashId)) return;
      progress.set(p.trashId, Number(p.percent) || 0);
      screen.list.setRows(items.map(rowOf));
    }),
    on("install.purgeDone", (p) => {
      if (!p) return;
      ended.add(p.trashId);
      progress.delete(p.trashId);
      toast({ title: S.trTitle, body: p.message || "", icon: p.ok ? "trash" : "warn", sound: false });
      load();
    }),
  ];
  load();
  return { el: screen.el, onShow() { nav.ensure(); }, destroy() { offs.forEach((off) => off()); } };
}

export default { id: "trash", create };
