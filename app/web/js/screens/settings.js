// Einstellungen (07 Abschnitt 4 und 11.7). Ein allgemeiner Zeichner: Bereiche und Zeilen kommen
// aus Python (settings.categories / settings.page), die Seite kennt keine einzelne Einstellung.
//   "settings"       Wurzel (07 4.1): eine Spalte in der Mitte, die Bereiche mit Symbol
//   "settings-page"  ein Bereich oder eine Unterseite (07 4.2): links die Bereiche zur Orientierung
//                    (nur Anzeige, der Bereich markiert), rechts die Zeilen mit dem Fokus; Titel =
//                    Name der Seite, darüber klein der Weg dorthin. Links oder Kreis: eine Ebene zurück.
// Kreuz oder Rechts auf einem Bereich öffnet ihn als eigenen Bildschirm (so führt Kreis immer
// genau eine Ebene zurück). Zwischen Seite und Unterseite wechseln nur Titel und rechte Spalte
// (css/settings.css).
//
// Das Ergebnis von settings.set / settings.action wertet evaluate() aus (docs/RPC.md):
//   rows (+rows_page)  ersetzt die Zeilen, wenn es die offene Seite ist
//   error + message    Fehlerdialog          message allein   Hinweis oben rechts
//   trial              Rückfrage mit Countdown, Antwort über settings.action {id: trial.row, value}
//   page               diese Seite öffnen    ui (+params)       eigener Bildschirm, wenn angemeldet (E103)
//   confirm            (falls der Kern selbst nachfragt) Rückfrage, dann noch einmal mit confirmed
import { h, icon } from "../core/dom.js";
import { call, on } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { router } from "../core/router.js";
import { sound } from "../core/sound.js";
import { store } from "../core/store.js";
import { S } from "../core/strings.js";
import { createRowList } from "../components/rows.js";
import { confirm, messageDialog, trialDialog } from "../components/dialog.js";
import { toast } from "../components/toast.js";
import { askText } from "../components/osk-input.js";
import displayArea from "./display-area.js";

router.register(displayArea);

const LOAD_HINT_MS = 300;        // erst danach erscheint „Wird geladen …“
const SLOW_HINT_MS = 8000;       // antwortet eine Seite so lange nicht, sagt der Platzhalter das
const TRIAL_KEEP = "trial-keep";
const TRIAL_REVERT = "trial-revert";
function hints(...items) {
  return h("div.hints", items.map(([glyph, text]) => h("div.hint", icon(glyph), h("span", { text }))));
}

// Bildschirmtastatur für eine Text-Zeile (components/osk-input.js). Bei Abbruch bleibt der alte
// Wert stehen; verdeckte Zeilen (secret) beginnen leer, ihr Wert kommt nie zur Seite.
// learn: false (E100): Kennungen wie die itch.io-App-Kennung landen nicht in den gelernten Wörtern
const askRow = (row) => askText({ title: row.label, initial: row.value, secret: row.secret, learn: row.learn !== false });

// Offene Einstellungs-Bildschirme, der unterste zuerst (für Ergebnisse mit "page")
const openScreens = [];

// ---------------------------------------------------------------- Bildschirm
// source: {id, title, crumb?, fetch(id) -> {title, rows}, set(row, value), action(row, value?),
//          text?(row), watch?(reload), unwatch?(),
//          categories?() -> [{id, label, icon}]   Wurzel: links die Bereiche mit Fokus, rechts Vorschau
//          context?: {categories, selected, scroll} Seite eines Bereichs: links die Bereiche als Anzeige
//          rows?: [Row]                            schon bekannte Zeilen (sofort zeigen, dann neu holen)
//          keepTitle?: bool}                       Titel bleibt, auch wenn die Seite einen anderen nennt
// Ohne categories und context: eine Spalte an der Stelle der rechten (Spieleinstellungen).
export function createPageScreen(source) {
  const mode = typeof source.categories === "function" ? "root" : (source.context ? "page" : "single");
  const context = source.context || null;
  const title = h("h1.set-title.ell", { text: source.title || "" });
  const crumb = h("div.set-crumb.ell", { text: source.crumb || "", hidden: !source.crumb });
  let rightId = mode === "root" ? null : source.id;
  let shownId = null;           // Seite, deren Zeilen gerade rechts stehen
  let pageTitle = source.title || "";
  let serial = 0;
  let waitTimer = null;
  let slowTimer = null;
  let scope = null;            // Fokus-Bereich dieses Bildschirms (vom Router)
  let categories = context ? context.categories || [] : [];
  let closed = false;
  // Rollstand der Bereiche wie auf dem Bildschirm darunter; gilt erst, wenn die Liste im Dokument steht
  let pendingScroll = context && Number.isFinite(context.scroll) ? context.scroll : null;
  const unsubs = [];

  const right = createRowList({
    onSet: (row, value) => source.set(row, value),
    onAction: (row) => source.action(row),
    onResult: (row, result) => { evaluate(row, result); },
    onText: (row) => (source.text ? source.text(row) : askRow(row)),
    onPage: (row) => openSub(row.page || row.id, row.label),
    onError: (text, row) => showError(row, text),
    onHeld: (node) => { if (scope) scope.last = node; },
    emptyText: S.settingsEmpty,
    alwaysFocus: mode !== "root",
  });
  right.el.setAttribute("data-focus-group", "");
  right.scrollbar.classList.add("set-scroll");
  const rightBox = h("div.set-right", right.el);

  // Wurzel (07 4.1): eine Spalte in der Mitte, jeder Bereich mit Symbol; erst die Seite eines
  // Bereichs ist zweispaltig (4.2), links stehen dort die Bereiche als Wegweiser.
  // Leer bleibt die Liste nur, wenn der Kern die Bereiche nicht liefert: dann steht sein Satz darin.
  const left = mode === "single" ? null : createRowList({ onPress: () => true, emptyText: S.unavailable, alwaysFocus: mode === "root" });
  const leftBox = left ? h(mode === "root" ? "div.set-root" : "div.set-left", left.el) : null;
  if (left) {
    left.el.classList.add(mode === "root" ? "is-root" : "is-left");
    left.el.setAttribute("data-focus-group", "");
    if (mode === "page") leftBox.inert = true;
    if (mode === "root") left.scrollbar.classList.add("set-root-scroll");
  }
  const el = h("div.settings" + (mode === "page" ? ".is-two.is-sub" : "") + (mode === "root" ? ".is-root" : ""),
    crumb, title, leftBox,
    mode === "root" ? null : rightBox, mode === "root" ? left.scrollbar : right.scrollbar,
    hints(["btn-cross", mode === "root" ? S.hintOpen : S.select], ["btn-circle", S.hintBack]));

  // ---------------------------------------------------------------- Laden
  function lockedRow(text) {
    return { id: "_unavailable", type: "info", label: text || S.settingsLoadFailed, enabled: false };
  }

  function stopWaiting() {
    clearTimeout(waitTimer);
    clearTimeout(slowTimer);
  }

  async function loadRight(id) {
    if (id === null || id === undefined || closed) return;
    const mine = ++serial;
    const fresh = id !== shownId;
    rightId = id;
    stopWaiting();
    if (fresh) {
      // Noch nichts zu zeigen: Platzhalter, dessen Text erst nach 300 ms erscheint. Stehen Zeilen
      // einer anderen Seite da, bleiben sie so lange stehen (kein Flackern bei schnellen Seiten).
      if (shownId === null) right.setLoading();
      else waitTimer = setTimeout(() => { if (mine === serial) right.setLoading(0); }, LOAD_HINT_MS);
    }
    // Hängt der Kern (alle Arbeits-Threads belegt), steht das da, statt einer leeren Seite
    slowTimer = setTimeout(() => {
      if (mine === serial && (fresh || right.loading)) right.setLoading(0, S.setSlow);
    }, SLOW_HINT_MS);
    let page;
    try {
      page = await source.fetch(id);
    } catch (e) {
      page = { title: "", rows: [lockedRow(e && e.message)] };
    }
    if (mine !== serial || closed) return;
    stopWaiting();
    shownId = id;
    pageTitle = (page && page.title) || pageTitle;
    if (page && page.title && (mode === "single" || (mode === "page" && !source.keepTitle && !source.title))) title.textContent = page.title;
    right.setRows((page && page.rows) || []);
    nav.ensure();
  }

  function categoryRows() {
    // Auf der Wurzel mit Symbol (settings.categories liefert es je Bereich), links auf einer Seite ohne
    return categories.map((c) => ({ id: c.id, type: "page", label: c.label, icon: mode === "root" && c.icon ? c.icon : undefined }));
  }

  async function load() {
    if (mode === "single") { await loadRight(source.id); return; }
    if (mode === "page") {
      left.setRows(categoryRows());
      markSelected();
      paintDots();
      // Die Vorschau der Wurzel kennt die Zeilen schon: sofort zeigen, im Hintergrund frisch holen
      if (Array.isArray(source.rows)) {
        right.setRows(source.rows);
        shownId = source.id;
        nav.ensure();
      }
      await loadRight(source.id);
      return;
    }
    // Wurzel: bis die Bereiche da sind, Platzhalter (sein Text erscheint erst nach 300 ms)
    left.setLoading();
    try {
      categories = (await source.categories()) || [];
    } catch (e) {
      categories = [];
      if (closed) return;
      // Der Satz des Kerns steht als gesperrte Zeile mit Fokus da; Kreis führt zurück
      left.setRows([lockedRow((e && e.message) || S.setUnavailable)]);
      nav.ensure();
      return;
    }
    if (closed) return;
    left.setRows(categoryRows());
    paintDots();
    nav.ensure();
  }

  // ---------------------------------------------------------------- Ergebnis von set / action
  function showError(row, text) {
    sound.play("error");
    return messageDialog({ title: (row && row.label) || S.setErrorTitle, text: text || S.settingsFailed });
  }

  async function evaluate(row, result) {
    if (!result || typeof result !== "object" || closed) return;
    const forPage = result.rows_page === undefined || result.rows_page === null || result.rows_page === rightId;
    if (Array.isArray(result.rows) && forPage) right.setRows(result.rows);
    else if (result.refresh) loadRight(rightId);
    // Der Kern will erst eine Bestätigung (kam ohne confirmed; auch zusammen mit error): mit seinem
    // Satz fragen, dann noch einmal mit confirmed. Zeilen vom Typ confirm-action hat die Seite schon gefragt.
    if (result.confirm && typeof result.confirm === "object" && row && !row.confirmed && row.type !== "confirm-action") {
      const c = result.confirm;
      if (await confirm({ title: c.title || row.label, text: c.text || "", ok: c.ok || row.label, cancel: c.cancel })) {
        const again = { ...row, confirmed: true };
        await answer(again, () => source.action(again));
      }
      return;
    }
    if (result.error) { await showError(row, result.message); return; }
    if (result.message) toast({ title: pageTitle || title.textContent, body: String(result.message), icon: "info" });
    if (result.trial) { await runTrial(row, result.trial); return; }
    // E103: jeder angemeldete Bildschirm (display-area, setup-welcome, backup, cleanup …) mit params;
    // ein unbekannter (Teil fehlt noch) tut nichts
    if (typeof result.ui === "string" && router.known(result.ui)) {
      router.go(result.ui, result.params && typeof result.params === "object" ? result.params : {});
    }
    else if (result.page) gotoPage(String(result.page));
  }

  async function answer(row, send) {
    let result;
    try {
      result = await send();
    } catch (e) {
      await showError(row, e && e.message);
      return;
    }
    await evaluate(row, result);
  }

  // Probezeit: Die Änderung gilt schon. Ohne „Behalten“ nimmt sie der Kern zurück; die Antwort
  // geht trotzdem immer hinaus, damit er nicht bis zum Ende der Frist wartet.
  async function runTrial(row, trial) {
    const keep = await trialDialog({ title: trial.text, text: trial.detail, seconds: Number(trial.seconds) || 15 });
    const target = { id: trial.row || (row && row.id), label: row && row.label };
    await answer(target, () => source.action(target, keep ? TRIAL_KEEP : TRIAL_REVERT));
  }

  // ---------------------------------------------------------------- Unterseiten
  function trail() {
    if (mode === "root") return S.settings;
    return (source.crumb ? source.crumb + S.crumbSep : "") + title.textContent;
  }

  function openSub(pageId, label) {
    if (!pageId || closed) return;
    const ctx = context ? { ...context, scroll: left.scrollTop } : null;
    router.go("settings-page", { id: pageId, title: label || "", crumb: trail(), context: ctx });
  }

  // Bereich der Wurzel öffnen (Kreuz oder Rechts). Die Seite rollt ihre linke Liste selbst so, dass
  // der Bereich zu sehen ist (die Wurzel hat ein anderes Raster).
  function openCategory(row) {
    if (!row || row.id === undefined || row.id === "" || closed) { sound.play("edge"); return; }
    router.go("settings-page", {
      id: row.id, title: row.label, crumb: S.settings, keepTitle: true,
      context: { categories, selected: row.id },
    });
  }

  // Ergebnis {page}: Liegt die Seite schon offen darunter (nach dem Koppeln zurück zur Liste der
  // Geräte), dorthin zurück und neu laden; sonst als Unterseite öffnen.
  function gotoPage(pageId) {
    const alive = openScreens.filter((s) => s.alive());
    const target = [...alive].reverse().find((s) => s.shows(pageId));
    if (!target) { openSub(pageId, ""); return; }
    while (router.top && router.top.inst !== target.api && router.top.id === "settings-page") router.back();
    target.reload();
  }

  // ---------------------------------------------------------------- Bereiche (Wurzel und Wegweiser)
  // Auf einer Seite bleibt links der Bereich markiert, zu dem sie gehört
  function markSelected() {
    if (!left || mode !== "page") return;
    for (const node of left.el.querySelectorAll(".row")) {
      const row = left.rowOf(node);
      node.classList.toggle("is-selected", !!row && row.id === context.selected);
    }
  }
  // Marke an „System“, solange ein System-Update bereitsteht (wie am Zahnrad)
  function paintDots() {
    if (!left) return;
    const node = left.nodeOf("system");
    if (node) node.classList.toggle("has-dot", !!store.state.update_available);
  }

  if (mode === "root") {
    left.el.addEventListener("press", (e) => {
      const row = left.rowOf(e.target.closest(".row"));
      if (!row) { e.detail.sound = null; return; }
      openCategory(row);
    });
    left.el.addEventListener("nav-move", (e) => {
      if (e.detail.dir === "right") {
        e.preventDefault();
        if (e.detail.repeat) return;
        const row = left.rowOf(e.target.closest(".row"));
        if (row) { sound.play("select"); openCategory(row); } else sound.play("edge");
      } else if (e.detail.dir === "left") { e.preventDefault(); sound.play("edge"); }
    });
  }
  right.el.addEventListener("nav-move", (e) => {
    if (e.defaultPrevented) return;        // Regler behandeln Links/Rechts selbst
    if (e.detail.dir === "left") {
      e.preventDefault();
      // Links führt dorthin, wo man herkam (07 4.2); eine Spalte ohne linke Liste stößt an
      if (mode === "page" && !e.detail.repeat) { sound.play("back"); router.back(); } else sound.play("edge");
    } else if (e.detail.dir === "right") { e.preventDefault(); sound.play("edge"); }
  });

  function onPad(button, ev) {
    if (button === "l2" || button === "r2") {
      const inLeft = !!left && !!nav.current && left.el.contains(nav.current);
      return (inLeft ? left : right).page(button === "r2" ? 1 : -1);
    }
    return false;
  }

  // settings.changed: die offene Seite neu holen; der Fokus bleibt an seiner Zeile (rows.js)
  unsubs.push(on("settings.changed", (p) => {
    const page = p && p.page;
    if (rightId !== null && (!page || page === rightId)) loadRight(rightId);
  }));
  if (left) unsubs.push(store.subscribe(["update_available"], paintDots));
  if (source.watch) source.watch(() => loadRight(rightId));

  const api = {
    el, onPad,
    onShow(info) {
      if (!scope && router.top && router.top.inst === api) scope = router.top.scope;
      nav.ensure();
      right.update();
      // Unterseite: links genau wie auf der Seite darunter; Seite eines Bereichs: dieser Bereich im Bild
      if (left && pendingScroll !== null) { left.setScroll(pendingScroll); pendingScroll = null; }
      else if (left && mode === "page" && !(info && info.back)) left.show(context.selected);
      else if (left) left.update();
      // Zurück von einer Unterseite: Werte können sich dort geändert haben
      if (info && info.back && rightId !== null) loadRight(rightId);
    },
    destroy() {
      closed = true;
      stopWaiting();
      for (const u of unsubs) u();
      if (source.unwatch) source.unwatch();
      const at = openScreens.indexOf(entry);
      if (at >= 0) openScreens.splice(at, 1);
    },
  };
  const entry = {
    api,
    alive: () => el.isConnected && !el.classList.contains("is-back-leave"),
    shows: (id) => mode !== "root" && id === rightId,
    reload() { loadRight(rightId); },
  };
  openScreens.push(entry);

  load();
  return api;
}

function settingsSource(extra) {
  return {
    fetch: (id) => call("settings.page", { id }),
    set: (row, value) => call("settings.set", { id: row.id, value }),
    // Zeilen mit Rückfrage hat die Seite schon gefragt: das sagt confirmed (der Kern darf sich darauf verlassen)
    action: (row, value) => {
      const params = { id: row.id };
      if (value !== undefined) params.value = value;
      if (row.type === "confirm-action" || row.confirmed) params.confirmed = true;
      return call("settings.action", params);
    },
    ...extra,
  };
}

function createRoot() {
  return createPageScreen(settingsSource({
    id: null, title: S.settings,
    categories: async () => ((await call("settings.categories")) || {}).categories || [],
  }));
}

function createPage(params) {
  return createPageScreen(settingsSource({
    id: params.id, title: params.title, crumb: params.crumb, context: params.context || null,
    rows: params.rows || null, keepTitle: !!params.keepTitle,
  }));
}

export const settingsRoot = { id: "settings", create: createRoot };
export const settingsPage = { id: "settings-page", create: createPage };
