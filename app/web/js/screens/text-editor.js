// Text-Editor des Datei-Explorers (Bauplan E99): Textdateien (txt, ini, cfg, json …) zeilenweise mit
// dem Pad bearbeiten. Wunsch 2026-10-23: „Im Datei-Manager möchte ich auch txt- und ini-Dateien
// bearbeiten können.“
//   Steuerkreuz hoch/runter  Zeile wählen (gehalten schneller), rechter Stick ebenso
//   L1/R1                    seitenweise;  L2/R2  an den Anfang / ans Ende
//   Kreuz                    Zeile mit der Bildschirmtastatur bearbeiten (vorbelegt mit der Zeile)
//   Quadrat                  neue Zeile darunter, gleich zum Bearbeiten (Abbrechen nimmt sie wieder weg)
//   Dreieck                  Zeilenmenü: neue Zeile darüber, Zeile verdoppeln, Zeile löschen
//   Optionen                 Speichern, Änderungen verwerfen, neu laden
//   Kreis                    zurück; mit ungespeicherten Änderungen erst fragen
// PS lang (immer zurück ins Home, ohne Rückfrage) und jeder andere Weg, der den Editor ohne diese Rückfrage
// schließt, legt ungespeicherte Änderungen als Entwurf ab (localStorage, nur in diesem WebView); beim
// nächsten Öffnen derselben Datei bietet der Editor sie wieder an (E99).
// Eine angesteckte Tastatur: Pfeile, Bild auf/ab, Strg+Pos1/Ende; Eingabe, F2 oder einfach Tippen
// öffnet ein Feld in der Zeile (Eingabe übernimmt, Escape verwirft), Einfg eine neue Zeile darunter,
// Strg+S speichert. Im Mock-Betrieb gehören die Tasten der Pad-Nachbildung (main.js), dort nur F2 und Strg+S.
// Die Platte fasst der Kern an (couch/textedit.py): files.textOpen {path}, files.textSave {path, text,
// expect}. Kodierung, BOM und Zeilenenden bleiben dort, wie sie waren; die Seite kennt nur Zeilen.
// Nur lesen (readonly mit Grund vom Kern, z. B. Steam- und Emulator-Dateien, mehr als 5000 Zeilen):
// ansehen ja, alle Bearbeiten-Aktionen aus.
// Die Liste zeichnet nur den sichtbaren Ausschnitt: ein fester Satz Zeilen-Elemente, die beim Rollen
// neu beschriftet werden. Der Fokus bleibt dabei auf dem Element der gewählten Zeile.
import { h, icon, cssPx } from "../core/dom.js";
import { call, isMock } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { router } from "../core/router.js";
import { sound } from "../core/sound.js";
import { S, fmt } from "../core/strings.js";
import { openMenu } from "../components/menu.js";
import { dialog, confirm, messageDialog } from "../components/dialog.js";
import { toast } from "../components/toast.js";

// Gleich wie textedit.EXTENSIONS im Kern; der Explorer bietet damit „Bearbeiten“ an
export const EDITABLE = new Set(["txt", "ini", "cfg", "conf", "config", "log", "json", "xml", "yaml", "yml", "toml", "md",
  "csv", "properties", "nfo", "lua", "rc"]);
// Größer öffnet der Kern nicht (textedit.MAX_BYTES); der Explorer zeigt solche Dateien wie bisher als Ausschnitt
export const EDIT_MAX_BYTES = 2 * 1024 * 1024;
const INI_LIKE = new Set(["ini", "cfg", "conf", "config", "properties", "toml", "rc"]);
const YAML_LIKE = new Set(["yaml", "yml"]);
const SHOW_MAX = 400;           // so viele Zeichen einer Zeile kommen in die Liste (mehr passt nie in die Breite)
const FULL_MAX = 2000;          // so viele zeigt die Vorschau einer abgeschnittenen Zeile
const KEEP_ROWS = 2;            // so viele Zeilen bleiben beim Rollen über bzw. unter der gewählten sichtbar
// Entwürfe (E99): je Datei (realpath des Kerns) einer, höchstens DRAFT_MAX Dateien, nach DRAFT_DAYS verfallen.
// Größere Texte passen nicht sicher in den Speicher der Seite (rund 5 MB je Ursprung, dazu die gelernten Wörter).
const DRAFT_KEY = "couch.editor.drafts";
const DRAFT_MAX = 5;
const DRAFT_DAYS = 30;
const DRAFT_CHARS = 1000000;

export function extensionOf(name) {
  const base = String(name || "").split("/").pop().replace(/^\.+/, "");
  const at = base.lastIndexOf(".");
  return at > 0 ? base.slice(at + 1).toLowerCase() : "";
}

export function isEditable(name) {
  return EDITABLE.has(extensionOf(name));
}

function modeOf(name) {
  const ext = extensionOf(name);
  return INI_LIKE.has(ext) ? "ini" : YAML_LIKE.has(ext) ? "yaml" : "plain";
}

const INI_PAIR = /^(\s*[^=\s;#[][^=]*?)(\s*=\s*)(.*)$/;
const YAML_PAIR = /^(\s*(?:-\s+)?[^:#\s-][^:#]*?)(\s*:)(\s.*|)$/;

// Eine Zeile mit Hervorhebung: [Abschnitt] betont, bei „schlüssel = wert“ der Schlüssel gedimmt,
// Kommentare (; #) gedimmt
function fill(span, raw, mode) {
  const line = raw.length > SHOW_MAX ? raw.slice(0, SHOW_MAX) : raw;
  span.className = "te-text";
  if (mode !== "plain") {
    const t = line.trimStart();
    if (t.startsWith("#") || (mode === "ini" && t.startsWith(";"))) { span.classList.add("is-comment"); span.textContent = line; return; }
    if (mode === "ini" && /^\[[^\]]*\]\s*$/.test(t)) { span.classList.add("is-section"); span.textContent = line; return; }
    const m = (mode === "ini" ? INI_PAIR : YAML_PAIR).exec(line);
    if (m) { span.replaceChildren(h("span.te-key", { text: m[1] }), h("span.te-eq", { text: m[2] }), m[3]); return; }
  }
  span.textContent = line;
}

// ---------------------------------------------------------------- Entwürfe
// Ohne Speicher (Fehler, voll) gibt es eben keinen Entwurf; der Editor arbeitet trotzdem
function readDrafts() {
  try {
    const list = JSON.parse(localStorage.getItem(DRAFT_KEY) || "[]");
    const since = Date.now() - DRAFT_DAYS * 86400000;
    return Array.isArray(list) ? list.filter((d) => d && typeof d.path === "string" && typeof d.text === "string" && Number(d.at) > since) : [];
  } catch { return []; }
}

function writeDrafts(list) {
  try {
    if (list.length) localStorage.setItem(DRAFT_KEY, JSON.stringify(list)); else localStorage.removeItem(DRAFT_KEY);
    return true;
  } catch { return false; }
}

export function draftOf(key) {
  return readDrafts().find((d) => d.path === key) || null;
}

function dropDraft(key) {
  const list = readDrafts();
  const rest = list.filter((d) => d.path !== key);
  if (rest.length !== list.length) writeDrafts(rest);
}

// Neuester vorn; passt der Speicher nicht, fallen die ältesten anderen weg
function keepDraft(draft) {
  if (draft.text.length > DRAFT_CHARS) return false;
  let list = [draft, ...readDrafts().filter((d) => d.path !== draft.path)].slice(0, DRAFT_MAX);
  while (!writeDrafts(list)) {
    if (list.length <= 1) return false;
    list = list.slice(0, -1);
  }
  return true;
}

function sameStamp(a, b) {
  return !!a && !!b && String(a.mtime_ns) === String(b.mtime_ns) && Number(a.size) === Number(b.size) && (a.sha256 || "") === (b.sha256 || "");
}

function hint(glyph, text) {
  return h("div.hint", glyph.startsWith("btn-") ? icon(glyph) : h("span.keycap", { text: glyph }), h("span", { text }));
}

function create(params = {}) {
  const path = String(params.path || "");
  // Name wie angeklickt (bei einem Verweis nicht der des Ziels)
  const name = String(params.name || path.split("/").pop() || S.teTitle);
  const mode = modeOf(name);

  const nameEl = h("h1.te-name.ell", { text: name });
  const markEl = h("span.te-mark", { text: S.teChanged, hidden: true });
  const metaEl = h("span.te-meta.ell");
  const subEl = h("div.flow-sub.te-sub.ell");
  const noteText = h("span.ell");
  const noteEl = h("div.te-note", { hidden: true }, icon("lock"), noteText);
  const track = h("div.te-track");
  const listEl = h("div.te-list", { "data-scroll": true }, track);
  const scrollbar = h("div.scrollbar.flow-scroll", { hidden: true });
  const fullText = h("div.te-full-text");
  const fullEl = h("div.te-full", { hidden: true }, fullText);
  const hintsEl = h("div.hints");
  const el = h("div.flow.te", h("div.te-head", nameEl, markEl, metaEl), subEl, noteEl, listEl, scrollbar, fullEl, hintsEl);

  let doc = null;               // Antwort von files.textOpen (ohne Text): Format, expect, readonly
  let lines = [];
  let original = "";            // Text beim Öffnen bzw. nach dem letzten Speichern
  let dirty = false;
  let readonly = true;
  let reason = "";
  let sel = 0;                  // gewählte Zeile
  let top = 0;                  // erste sichtbare Zeile
  let pool = [];                // Zeilen-Elemente des Ausschnitts
  let message = S.setLoading;   // statt der Zeilen: Laden, Fehler
  let busy = "";                // Speichern, Bildschirmtastatur, Laden: Text für die Unterzeile
  let editing = null;           // Feld für die echte Tastatur {input, index}
  let closed = false;
  let serial = 0;
  let discarded = false;        // Kreis › „Verwerfen“: beim Schließen kein Entwurf
  let offered = false;          // Entwurf nur beim ersten Laden anbieten
  const msgRow = h("div.te-line.te-msg", { "data-focus": true }, h("span.te-text"));

  const onTop = () => !closed && router.top && router.top.inst === api;
  const scopeIsMine = () => onTop() && nav.scope && nav.scope.el === el;

  // ---------------------------------------------------------------- Zeichnen
  function rowCount() {
    const row = cssPx("--lh-body") || 1;
    return Math.max(1, Math.floor((listEl.clientHeight || row * 12) / row));
  }

  function makeRow() {
    return h("div.te-line", { "data-focus": true }, h("span.te-no"), h("span.te-text"));
  }

  function ensurePool() {
    if (!el.isConnected) return false;
    const want = rowCount();
    if (pool.length !== want) {
      pool = Array.from({ length: want }, makeRow);
      track.replaceChildren(msgRow, ...pool);
    }
    return true;
  }

  function scrollTo() {
    const rows = pool.length || 1;
    const keep = Math.min(KEEP_ROWS, Math.floor((rows - 1) / 2));
    if (sel < top + keep) top = sel - keep;
    if (sel > top + rows - 1 - keep) top = sel - rows + 1 + keep;
    top = Math.max(0, Math.min(top, Math.max(0, lines.length - rows)));
  }

  function paint() {
    if (!ensurePool()) return;
    const showLines = doc && !message;
    msgRow.hidden = !!showLines;
    msgRow.lastChild.textContent = message || "";
    if (!showLines) {
      for (const node of pool) node.hidden = true;
      scrollbar.hidden = true;
      fullEl.hidden = true;
      if (scopeIsMine()) nav.focus(msgRow);
      return;
    }
    scrollTo();
    const width = String(lines.length).length + 1 + "ch";
    pool.forEach((node, k) => {
      const i = top + k;
      if (i >= lines.length) { node.hidden = true; return; }
      node.hidden = false;
      node.dataset.line = String(i);
      node.firstChild.textContent = String(i + 1);
      node.firstChild.style.width = width;
      if (!(editing && editing.index === i)) fill(node.lastChild, lines[i], mode);
      node.classList.toggle("is-editing", !!(editing && editing.index === i));
    });
    // Rollbalken: so hoch wie der sichtbare Anteil (mindestens ein Zehntel), unten bündig am Ende
    const view = pool.length * (cssPx("--lh-body") || 1);
    if (lines.length > pool.length && view > 0) {
      const bar = Math.max(view * 0.1, view * pool.length / lines.length);
      scrollbar.hidden = false;
      scrollbar.style.height = bar + "px";
      scrollbar.style.setProperty("--sb-y", (top / (lines.length - pool.length)) * (view - bar) + "px");
    } else {
      scrollbar.hidden = true;
    }
    const node = pool[sel - top];
    if (node && scopeIsMine() && !editing) nav.focus(node);
    showFull(node);
    head();
  }

  // Abgeschnittene Zeile: unter der Liste ganz (bis FULL_MAX Zeichen)
  function showFull(node) {
    const text = node ? node.lastChild : null;
    const cut = !!text && !editing && (lines[sel].length > SHOW_MAX || text.scrollWidth > text.clientWidth + 1);
    fullEl.hidden = !cut;
    if (cut) fullText.textContent = lines[sel].slice(0, FULL_MAX);
  }

  function head() {
    nameEl.textContent = name;
    markEl.hidden = !dirty;
    const eol = doc ? (doc.eol_mixed ? S.teEolMixed : ({ crlf: S.teEolCrlf, lf: S.teEolLf, cr: S.teEolCr })[doc.eol] || "") : "";
    metaEl.textContent = doc ? [(doc.label || "") + (doc.bom ? " " + S.teBom : ""), eol].filter(Boolean).join(" · ") : "";
    const where = path.slice(0, path.lastIndexOf("/")) || "/";
    const line = doc && !message ? fmt(S.teLineOf, { n: sel + 1, total: lines.length }) : "";
    subEl.textContent = busy || [where, line].filter(Boolean).join(" · ");
    noteEl.hidden = !(doc && readonly && reason);
    noteText.textContent = reason;
    hintsEl.replaceChildren(...(doc && !readonly && !message
      ? [hint("btn-cross", S.teEdit), hint("btn-square", S.teNewLine), hint("btn-triangle", S.teLineMenu),
        hint(S.teKeyOptions, S.teFileMenu), hint("btn-circle", S.hintBack)]
      : [hint("btn-circle", S.hintBack)]));
  }

  function changed() {
    dirty = lines.join("\n") !== original;
    head();
  }

  // ---------------------------------------------------------------- Laden
  async function load() {
    const mine = ++serial;
    busy = S.setLoading;
    head();
    let r;
    try { r = await call("files.textOpen", { path }); } catch (e) { r = { ok: false, message: (e && e.message) || S.teFailed }; }
    if (closed || mine !== serial) return;
    busy = "";
    if (!r || r.ok === false) {
      doc = doc || null;
      message = (r && r.message) || S.teFailed;
      lines = [];
      readonly = true;
      paint();
      head();
      return;
    }
    const { text, ...rest } = r;
    doc = rest;
    lines = String(text || "").split("\n");
    original = lines.join("\n");
    dirty = false;
    readonly = !!r.readonly;
    reason = r.readonly_reason || "";
    message = "";
    sel = Math.min(sel, lines.length - 1);
    paint();
    if (!offered) { offered = true; offerDraft(); }
  }

  // ---------------------------------------------------------------- Entwurf (E99)
  const draftKey = () => (doc && doc.real) || path;

  async function offerDraft() {
    const key = draftKey();
    const draft = draftOf(key);
    if (!draft || readonly) return;
    if (draft.text === original) { dropDraft(key); return; }
    const when = new Date(Number(draft.at)).toLocaleString(S.locale, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
    const choice = await dialog({
      title: S.teDraftTitle, text: fmt(S.teDraftText, { when }), detail: sameStamp(draft.expect, doc.expect) ? "" : S.teDraftChanged,
      initial: "restore", buttons: [{ id: "discard", label: S.teDraftDiscard }, { id: "restore", label: S.teDraftRestore }],
    });
    if (closed) return;
    if (choice === "discard") { dropDraft(key); return; }
    if (choice !== "restore") return;              // Kreis: später noch einmal anbieten
    dropDraft(key);
    lines = draft.text.split("\n");
    // Stand, auf dem der Entwurf beruht: Hat sich die Datei seitdem geändert, meldet Speichern den Konflikt
    if (draft.expect) doc = { ...doc, expect: draft.expect };
    sel = Math.max(0, Math.min(Number(draft.sel) || 0, lines.length - 1));
    changed();
    paint();
  }

  // Beim Schließen ohne die Rückfrage von Kreis (PS lang führt immer ins Home): nichts verlieren
  // Läuft das Speichern noch, bleibt der Entwurf als Rückfall; save() räumt ihn ab, sobald es geklappt hat
  function stashDraft() {
    if (discarded || !dirty || !doc || readonly || message) return;
    const kept = keepDraft({ path: draftKey(), name, text: lines.join("\n"), expect: doc.expect || null, sel, at: Date.now() });
    if (kept && busy === S.teSaving) { toast({ title: name, body: S.teDraftSaving, icon: "info" }); return; }
    toast(kept ? { title: name, body: S.teDraftKept, icon: "info" } : { title: name, body: S.teDraftLost, icon: "warn" });
  }

  // ---------------------------------------------------------------- Bewegen
  function moveTo(i, repeat = false) {
    if (!doc || message) { sound.play("edge", { repeat }); return; }
    const next = Math.max(0, Math.min(lines.length - 1, i));
    if (next === sel) { sound.play("edge", { repeat }); return; }
    sel = next;
    sound.play("focus", { repeat });
    paint();
  }

  // ---------------------------------------------------------------- Bearbeiten
  function refuse() {
    if (!doc || message) { sound.play("edge"); return true; }
    if (readonly) {
      sound.play("edge");
      toast({ title: S.teReadonly, body: reason, icon: "lock" });
      return true;
    }
    if (busy || editing) { sound.play("edge"); return true; }
    return false;
  }

  // Zeile i über die Bildschirmtastatur (Feld-Modus, ohne Lernen: In Dateien stehen auch Kennwörter).
  // back: Die Zeile ist eben eingefügt; Abbrechen nimmt sie wieder weg und wählt diese Zeile.
  async function editLine(i, back = null) {
    if (refuse()) return;
    busy = S.teEditing;
    head();
    let r = null;
    try {
      r = await call("osk.open", { purpose: "field", title: fmt(S.teLineTitle, { n: i + 1, name }), initial: lines[i], secret: false, learn: false });
    } catch (e) {
      toast({ title: S.teFailed, body: (e && e.message) || "" });
    }
    if (closed) return;
    busy = "";
    const text = r && !r.cancelled && typeof r.text === "string" ? r.text : null;
    if (text === null) {
      if (back !== null && lines[i] === "" && lines.length > 1) { lines.splice(i, 1); sel = Math.max(0, Math.min(back, lines.length - 1)); }
    } else {
      // Zeilenumbrüche (eingefügt) werden zu eigenen Zeilen
      lines.splice(i, 1, ...text.split(/\r\n|\r|\n/));
    }
    changed();
    paint();
  }

  function insertBelow() {
    if (refuse()) return;
    lines.splice(sel + 1, 0, "");
    sel += 1;
    sound.play("select");
    changed();
    paint();
    editLine(sel, sel - 1);
  }

  async function lineMenu() {
    if (refuse()) return;
    const at = sel;
    const choice = await openMenu([
      { id: "above", label: S.teInsertAbove, icon: "enter" },
      { id: "dup", label: S.teDuplicate, icon: "more" },
      { id: "delete", label: S.teDelete, icon: "trash" },
    ], { cls: "menu-center", title: fmt(S.teLineTitle, { n: at + 1, name }) });
    if (closed || !choice || at !== sel) return;
    if (choice === "above") {
      lines.splice(at, 0, "");
      changed();
      paint();
      editLine(at, at);
      return;
    }
    if (choice === "dup") { lines.splice(at + 1, 0, lines[at]); sel = at + 1; }
    else if (choice === "delete") {
      if (lines.length > 1) lines.splice(at, 1); else lines[0] = "";
      sel = Math.min(at, lines.length - 1);
    }
    changed();
    paint();
  }

  async function fileMenu() {
    if (!doc || message) { sound.play("edge"); return; }
    const items = readonly ? [] : [
      { id: "save", label: S.teSave, icon: "check", enabled: dirty && !busy, note: dirty ? "" : S.teNoChanges },
      { id: "discard", label: S.teDiscard, icon: "close", enabled: dirty && !busy },
    ];
    items.push({ id: "reload", label: S.teReload, icon: "restart", enabled: !busy });
    const choice = await openMenu(items, { cls: "menu-center", title: name });
    if (closed || !choice) return;
    if (choice === "save") save();
    else if (choice === "discard") {
      if (await confirm({ title: S.teDiscardTitle, text: S.teDiscardText, ok: S.teDiscard })) {
        lines = original.split("\n");
        sel = Math.min(sel, lines.length - 1);
        changed();
        paint();
      }
    } else if (choice === "reload") {
      if (dirty && !(await confirm({ title: S.teReloadTitle, text: S.teDiscardText, ok: S.teReload }))) return;
      load();
    }
  }

  // ---------------------------------------------------------------- Speichern
  async function save() {
    if (readonly || busy || !doc || message) { sound.play("edge"); return false; }
    const text = lines.join("\n");
    busy = S.teSaving;
    head();
    let r;
    try { r = await call("files.textSave", { path, text, expect: doc.expect }); } catch (e) { r = { ok: false, message: (e && e.message) || S.teFailed }; }
    // Inzwischen geschlossen (PS lang): Hat es geklappt, ist der Entwurf von stashDraft überholt (E99)
    if (closed) { if (r && r.ok) dropDraft(draftKey()); return false; }
    busy = "";
    if (r && r.ok) {
      doc = { ...doc, ...r };
      original = text;
      dropDraft(draftKey());
      changed();
      paint();
      toast({ title: name, body: r.unchanged ? S.teNothingToSave : S.teSavedBody, icon: "check" });
      return true;
    }
    head();
    if (r && r.code === "conflict") return conflict();
    if (r && r.code === "readonly") { readonly = true; reason = r.message || ""; paint(); }
    messageDialog({ title: S.teSaveFailed, text: (r && r.message) || S.teFailed });
    return false;
  }

  // Die Datei hat sich auf der Platte geändert (ein Spiel hat sie beim Beenden geschrieben …)
  async function conflict() {
    const choice = await dialog({
      title: S.teConflictTitle, text: S.teConflictText, initial: "keep",
      buttons: [{ id: "keep", label: S.teKeepEditing }, { id: "reload", label: S.teReloadLose }, { id: "overwrite", label: S.teOverwrite }],
    });
    if (closed) return false;
    if (choice === "reload") { load(); return false; }
    if (choice !== "overwrite") return false;
    let fresh;
    try { fresh = await call("files.textOpen", { path }); } catch (e) { fresh = { ok: false, message: (e && e.message) || S.teFailed }; }
    if (closed) return false;
    if (!fresh || !fresh.ok || fresh.readonly) {
      messageDialog({ title: S.teSaveFailed, text: (fresh && (fresh.readonly_reason || fresh.message)) || S.teFailed });
      return false;
    }
    const { text, ...rest } = fresh;
    doc = rest;
    return save();
  }

  // ---------------------------------------------------------------- Zurück
  async function leave() {
    if (editing) { finishInline(false); return; }
    if (busy) { sound.play("edge"); return; }
    if (!dirty) { sound.play("back"); router.back(); return; }
    const choice = await dialog({
      title: S.teUnsavedTitle, text: S.teUnsavedText, initial: "stay",
      buttons: [{ id: "save", label: S.teSave }, { id: "discard", label: S.teDiscardLeave }, { id: "stay", label: S.teStay }],
    });
    if (closed) return;
    if (choice === "save") { if (await save() && onTop()) router.back(); }
    else if (choice === "discard" && onTop()) { discarded = true; router.back(); }
  }

  // ---------------------------------------------------------------- Echte Tastatur
  function startInline(extra) {
    if (refuse()) return;
    const node = pool[sel - top];
    if (!node) return;
    const index = sel;
    const input = h("input.te-input", { type: "text", spellcheck: "false", autocomplete: "off" });
    input.value = lines[index] + (extra || "");
    editing = { input, index };
    node.lastChild.replaceChildren();
    node.lastChild.append(input);
    node.classList.add("is-editing");
    fullEl.hidden = true;
    input.addEventListener("keydown", (ev) => {
      ev.stopPropagation();          // nicht an main.js (Pad-Nachbildung) und nicht an onKey
      if (ev.key === "Enter") { ev.preventDefault(); finishInline(true); }
      else if (ev.ctrlKey && (ev.key === "s" || ev.key === "S")) { ev.preventDefault(); finishInline(true); save(); }
      else if (ev.key === "Escape") { ev.preventDefault(); finishInline(false); }
      else if (ev.key === "ArrowUp" || ev.key === "ArrowDown") {
        ev.preventDefault();
        finishInline(true);
        moveTo(sel + (ev.key === "ArrowDown" ? 1 : -1));
      } else if (ev.key === "Tab") {
        ev.preventDefault();
        input.setRangeText("\t", input.selectionStart, input.selectionEnd, "end");
      }
    });
    input.addEventListener("blur", () => finishInline(true));
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    sound.play("select");
  }

  function finishInline(keep) {
    if (!editing) return;
    const { input, index } = editing;
    editing = null;
    if (keep && input.value !== lines[index]) {
      lines.splice(index, 1, ...input.value.split(/\r\n|\r|\n/));
      changed();
    }
    sound.play(keep ? "select" : "back");
    input.remove();
    paint();
  }

  function onKey(e) {
    if (editing || !scopeIsMine()) return;
    if (e.ctrlKey && (e.key === "s" || e.key === "S")) { e.preventDefault(); save(); return; }
    if (isMock) {
      if (e.key === "F2") { e.preventDefault(); startInline(""); }
      return;
    }
    const page = Math.max(1, pool.length - 1);
    switch (e.key) {
      case "ArrowUp": moveTo(sel - 1, e.repeat); break;
      case "ArrowDown": moveTo(sel + 1, e.repeat); break;
      case "PageUp": moveTo(sel - page, e.repeat); break;
      case "PageDown": moveTo(sel + page, e.repeat); break;
      case "Home": if (!e.ctrlKey) return; moveTo(0); break;
      case "End": if (!e.ctrlKey) return; moveTo(lines.length - 1); break;
      case "Enter": case "F2": startInline(""); break;
      case "Insert":
        if (refuse()) break;
        lines.splice(sel + 1, 0, "");
        sel += 1;
        changed();
        paint();
        startInline("");
        break;
      case "Escape": leave(); break;
      default:
        if (e.key.length !== 1 || e.ctrlKey || e.altKey || e.metaKey) return;
        startInline(e.key);
    }
    e.preventDefault();
  }
  window.addEventListener("keydown", onKey);
  const onResize = () => { pool = []; paint(); };
  window.addEventListener("resize", onResize);

  // ---------------------------------------------------------------- Pad
  function onPad(button, ev) {
    if (editing) {
      if (ev.repeat) return true;
      if (button === "cross" || button === "r2" || button === "options") finishInline(true);
      else if (button === "circle") finishInline(false);
      return true;
    }
    const rows = Math.max(1, pool.length - 1);
    switch (button) {
      case "up": case "rs-up": moveTo(sel - 1, !!ev.repeat); return true;
      case "down": case "rs-down": moveTo(sel + 1, !!ev.repeat); return true;
      case "left": case "right": sound.play("edge", { repeat: !!ev.repeat }); return true;
      case "l1": moveTo(sel - rows, !!ev.repeat); return true;
      case "r1": moveTo(sel + rows, !!ev.repeat); return true;
      case "l2": if (!ev.repeat) moveTo(0); return true;
      case "r2": if (!ev.repeat) moveTo(lines.length - 1); return true;
      case "cross": if (!ev.repeat) { if (!refuse()) { sound.play("select"); editLine(sel); } } return true;
      case "square": if (!ev.repeat) insertBelow(); return true;
      case "triangle": if (!ev.repeat) lineMenu(); return true;
      case "options": if (!ev.repeat) fileMenu(); return true;
      case "circle": if (!ev.repeat) leave(); return true;
      default: return false;
    }
  }

  const api = {
    el,
    onPad,
    onShow() { paint(); },
    onHide() { if (editing) finishInline(true); },
    destroy() {
      stashDraft();
      closed = true;
      serial += 1;
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", onResize);
    },
  };
  head();
  load();
  return api;
}

export default { id: "text-editor", create };
