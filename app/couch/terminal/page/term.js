// Terminal der Konsole (E72): xterm.js, Startauswahl, Befehlsmenü, Schreibfeld. Das Fenster
// (window.py) führt das Pseudo-Terminal; Nachrichten gehen über den Meldungskanal "term".
// Pad-Tasten kommen als echte Tasten (padmouse, profile "term"): Pfeile, Enter, Escape, Rücktaste,
// Tab (R1), F10 (L1: Umschalt+Tab), F7/F8 (L2/R2: Blättern), F12 (Options: Befehle), F9 (Dreieck:
// Schreibfeld; die Bildschirmtastatur der Konsole öffnet sich dazu und tippt hinein).
"use strict";

const SUBMIT_DELAY_MS = 150;    // Text und Enter getrennt: kommt beides zusammen, hält Claude Code es für Eingefügtes

// Englisch (2026-10-25): ?lang=en (window.py aus COUCH_LANG); sonst bleibt alles deutsch
const LANG = new URLSearchParams(location.search).get("lang") === "en" ? "en" : "de";
const EN = {
  "Weitermachen": "Continue", "Das letzte Gespräch fortsetzen": "Resume the last conversation",
  "Gespräch auswählen": "Choose conversation", "Aus allen bisherigen Gesprächen wählen": "Pick from all past conversations",
  "Neues Gespräch": "New conversation", "Claude Code frisch starten": "Start Claude Code fresh",
  "Claude Code aktualisieren": "Update Claude Code", "Neueste Fassung holen (claude update)": "Get the latest version (claude update)",
  "Befehlszeile": "Command line", "Die normale Kommandozeile (bash)": "The regular command line (bash)",
  "Schließen": "Close", "Zurück zur Konsole": "Back to the console", "Befehle an Claude": "Commands for Claude",
  "Anderes Gespräch fortsetzen": "Resume another conversation", "Neues Gespräch beginnen": "Start a new conversation",
  "Gespräch verdichten": "Compact the conversation", "Modell wählen": "Choose model", "Wie voll der Kontext ist": "How full the context is",
  "Nutzung und Limits": "Usage and limits", "Version, Konto, Verbindung": "Version, account, connection",
  "Einstellungen von Claude Code": "Claude Code settings", "Alle Befehle": "All commands", "Tasten": "Keys",
  "Strg+C": "Ctrl+C", "Eingabe leeren, zweimal: beenden": "Clear input, twice: quit", "Umschalt+Tab": "Shift+Tab",
  "Modus wechseln (auch L1)": "Switch mode (also L1)", "Zu einer früheren Nachricht zurück": "Go back to an earlier message",
  "Ansicht und Sitzung": "View and session", "Schrift größer": "Larger text", "Schrift kleiner": "Smaller text",
  "Nach unten": "To the bottom", "Zum Ende der Ausgabe": "Jump to the end of the output",
  "Gespräch endet, danach Startauswahl": "Ends the conversation, then back to the start menu",
  "Gespräch beenden": "End conversation", "Zurück zur Startauswahl": "Back to the start menu",
  "Terminal zu, zurück zur Konsole": "Close the terminal, back to the console",
  "Das Programm wurde nicht gefunden.": "The program was not found.", "Das Programm ließ sich nicht starten.": "The program could not be started.",
  "Aktualisierung fertig.": "Update finished.", "Aktualisierung gescheitert (Code ": "Update failed (code ",
  "Befehlszeile beendet.": "Command line closed.",
  "Claude Code wurde beendet. Mit „Weitermachen“ geht es im selben Gespräch weiter.": "Claude Code has quit. “Continue” picks up the same conversation.",
  "Nachricht an Claude": "Message to Claude", "Senden": "Send", "Tastatur": "Keyboard",
  "Im Home-Ordner, wie im Terminal auf dem Desktop.": "In your home folder, like the terminal on the desktop.",
  "Befehle": "Commands", "Löschen": "Delete", "Schreiben": "Write", "Modus": "Mode", "Blättern": "Scroll",
};
const T = (text) => (LANG === "en" && EN[text]) || text;

const START = [
  { id: "continue", label: "Weitermachen", desc: "Das letzte Gespräch fortsetzen", claude: true },
  { id: "resume", label: "Gespräch auswählen", desc: "Aus allen bisherigen Gesprächen wählen", claude: true },
  { id: "new", label: "Neues Gespräch", desc: "Claude Code frisch starten", claude: true },
  { id: "update", label: "Claude Code aktualisieren", desc: "Neueste Fassung holen (claude update)", claude: true },
  { id: "shell", label: "Befehlszeile", desc: "Die normale Kommandozeile (bash)" },
  { id: "quit", label: "Schließen", desc: "Zurück zur Konsole" },
];

const MENU = [
  { group: "Befehle an Claude" },
  { id: "/resume", label: "/resume", desc: "Anderes Gespräch fortsetzen", cmd: true },
  { id: "/clear", label: "/clear", desc: "Neues Gespräch beginnen", cmd: true },
  { id: "/compact", label: "/compact", desc: "Gespräch verdichten", cmd: true },
  { id: "/model", label: "/model", desc: "Modell wählen", cmd: true },
  { id: "/context", label: "/context", desc: "Wie voll der Kontext ist", cmd: true },
  { id: "/usage", label: "/usage", desc: "Nutzung und Limits", cmd: true },
  { id: "/status", label: "/status", desc: "Version, Konto, Verbindung", cmd: true },
  { id: "/config", label: "/config", desc: "Einstellungen von Claude Code", cmd: true },
  { id: "/help", label: "/help", desc: "Alle Befehle", cmd: true },
  { group: "Tasten" },
  { id: "ctrl-c", label: "Strg+C", desc: "Eingabe leeren, zweimal: beenden", run: true },
  { id: "shift-tab", label: "Umschalt+Tab", desc: "Modus wechseln (auch L1)", run: true },
  { id: "esc-esc", label: "Esc Esc", desc: "Zu einer früheren Nachricht zurück", run: true },
  { group: "Ansicht und Sitzung" },
  { id: "font+", label: "Schrift größer", desc: "" },
  { id: "font-", label: "Schrift kleiner", desc: "" },
  { id: "bottom", label: "Nach unten", desc: "Zum Ende der Ausgabe" },
  { id: "update", label: "Claude Code aktualisieren", desc: "Gespräch endet, danach Startauswahl" },
  { id: "stop", label: "Gespräch beenden", desc: "Zurück zur Startauswahl", run: true },
  { id: "quit", label: "Schließen", desc: "Terminal zu, zurück zur Konsole" },
];

for (const item of [...START, ...MENU]) {
  if (item.label) item.label = T(item.label);
  if (item.desc) item.desc = T(item.desc);
  if (item.group) item.group = T(item.group);
}
// Feste Texte der Seite (term.html): Textknoten, die ganz im Wörterbuch stehen
if (LANG === "en") {
  document.documentElement.lang = "en";
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const key = node.nodeValue.trim();
    if (key && EN[key]) node.nodeValue = node.nodeValue.replace(key, EN[key]);
  }
}

const state = { running: "", claude: true, font: 22, fontMin: 14, fontMax: 40, pending: "", startFocus: 0, menuFocus: 0 };
const $ = (id) => document.getElementById(id);

function post(msg) {
  try { window.webkit.messageHandlers.term.postMessage(JSON.stringify(msg)); } catch (e) { /* ohne Fenster (Vorschau) */ }
}

// ------------------------------------------------------------------ Terminal
const term = new Terminal({
  fontFamily: "Hack, 'Fira Code', 'Adwaita Mono', 'Liberation Mono', 'Symbols Nerd Font Mono', monospace",
  fontSize: state.font,
  lineHeight: 1.12,
  scrollback: 20000,
  cursorBlink: true,
  allowProposedApi: false,
  theme: {
    background: "#0b0c10", foreground: "#e8e9ec", cursor: "#f2f3f5", cursorAccent: "#0b0c10",
    selectionBackground: "#3d8bff66",
    black: "#1b1e26", red: "#ff6f7d", green: "#5fd38d", yellow: "#f2c66d", blue: "#6aa5ff", magenta: "#d18cff",
    cyan: "#4fd6e0", white: "#d6d8de", brightBlack: "#6b7080", brightRed: "#ff8f9a", brightGreen: "#86e3aa",
    brightYellow: "#ffd98a", brightBlue: "#8fbcff", brightMagenta: "#e0aaff", brightCyan: "#7ee6ee", brightWhite: "#ffffff",
  },
});
const fit = new FitAddon.FitAddon();
term.loadAddon(fit);
term.open($("term"));
term.onData((d) => { if (state.running) post({ t: "in", d }); });

function size() {
  try { fit.fit(); } catch (e) { /* noch nicht sichtbar */ }
  return { cols: term.cols, rows: term.rows };
}
let resizeTimer = 0;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => post({ t: "size", ...size() }), 60);
});

function decode(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function sendKeys(seq) { if (state.running) post({ t: "in", d: seq }); }

// Text eintippen und mit eigenem Enter abschicken (Befehle, Schreibfeld)
function submit(text) {
  if (!state.running) return;
  post({ t: "in", d: text });
  setTimeout(() => post({ t: "in", d: "\r" }), SUBMIT_DELAY_MS);
}

function setFont(n) {
  state.font = Math.max(state.fontMin, Math.min(state.fontMax, n));
  term.options.fontSize = state.font;
  post({ t: "size", ...size() });
  post({ t: "font", size: state.font });
}

function paintState() {
  const names = { continue: "Claude Code", resume: "Claude Code", new: "Claude Code", update: "Aktualisierung", shell: "Befehlszeile" };
  $("state").textContent = state.running ? names[state.running] || "" : "";
}

// ------------------------------------------------------------------ Listen mit Fokus
function buildList(box, items, cls) {
  box.replaceChildren();
  const nodes = [];
  for (const it of items) {
    if (it.group) {
      const g = document.createElement("div");
      g.className = "group";
      g.textContent = it.group;
      box.append(g);
      continue;
    }
    const el = document.createElement("div");
    el.className = "item" + (cls ? " " + cls : "");
    el.dataset.id = it.id;
    const label = document.createElement("div");
    label.className = "label";
    label.textContent = it.label;
    el.append(label);
    if (it.desc) {
      const desc = document.createElement("div");
      desc.className = "desc";
      desc.textContent = it.desc;
      el.append(desc);
    }
    box.append(el);
    nodes.push(el);
  }
  return nodes;
}

function focusIn(nodes, i) {
  nodes.forEach((n, k) => n.classList.toggle("is-focus", k === i));
  if (nodes[i]) nodes[i].scrollIntoView({ block: "nearest" });
}

// ------------------------------------------------------------------ Startauswahl
let startNodes = [];
function showStart(note, warn) {
  const items = START.map((it) => ({ ...it }));
  startNodes = buildList($("start-list"), items);
  startNodes.forEach((n, i) => n.classList.toggle("is-off", !!(items[i].claude && !state.claude)));
  if (!state.claude) { note = "Claude Code ist nicht installiert (~/.local/bin/claude fehlt)."; warn = true; }
  $("start-note").textContent = note || "";
  $("start-note").classList.toggle("is-warn", !!warn);
  state.startFocus = state.claude ? Math.min(state.startFocus, startNodes.length - 1) : START.findIndex((s) => s.id === "shell");
  focusIn(startNodes, state.startFocus);
  $("menu").hidden = true;
  closeCompose(false);
  $("start").hidden = false;
}

function startKey(key) {
  const n = startNodes.length;
  if (key === "ArrowUp") state.startFocus = (state.startFocus - 1 + n) % n;
  else if (key === "ArrowDown") state.startFocus = (state.startFocus + 1) % n;
  else if (key === "Enter") {
    const it = START[state.startFocus];
    if (it.claude && !state.claude) return;
    if (it.id === "quit") { post({ t: "quit" }); return; }
    $("start-note").textContent = "";
    post({ t: "run", what: it.id });
    return;
  } else return;
  focusIn(startNodes, state.startFocus);
}

// ------------------------------------------------------------------ Befehlsmenü
let menuNodes = [];
const menuItems = MENU.filter((m) => !m.group);
function openMenu() {
  menuNodes = buildList($("menu-grid"), MENU);
  const claude = ["continue", "resume", "new"].includes(state.running);
  menuNodes.forEach((n, i) => n.classList.toggle("is-off", !!(menuItems[i].cmd && !claude)));
  focusIn(menuNodes, state.menuFocus);
  $("menu").hidden = false;
}
function closeMenu() { $("menu").hidden = true; term.focus(); }

function menuKey(key) {
  const n = menuNodes.length;
  let i = state.menuFocus;
  if (key === "Escape" || key === "F12") { closeMenu(); return; }
  if (key === "ArrowRight") i = Math.min(n - 1, i + 1);
  else if (key === "ArrowLeft") i = Math.max(0, i - 1);
  else if (key === "ArrowDown") i = Math.min(n - 1, nearestBelow(i, 1));
  else if (key === "ArrowUp") i = Math.max(0, nearestBelow(i, -1));
  else if (key === "Enter") { runMenu(menuItems[i]); return; }
  else return;
  state.menuFocus = i;
  focusIn(menuNodes, i);
}

// Nächstes Feld darüber/darunter, das ungefähr in derselben Spalte steht (Gruppen brechen das Raster)
function nearestBelow(i, dir) {
  const cur = menuNodes[i].getBoundingClientRect();
  let best = i;
  let bestScore = Infinity;
  menuNodes.forEach((n, k) => {
    const r = n.getBoundingClientRect();
    const dy = dir > 0 ? r.top - cur.top : cur.top - r.top;
    if (dy <= 4) return;
    const score = dy * 4 + Math.abs(r.left - cur.left);
    if (score < bestScore) { bestScore = score; best = k; }
  });
  return best;
}

function runMenu(it) {
  if (!it) return;
  const claude = ["continue", "resume", "new"].includes(state.running);
  if (it.cmd && !claude) return;
  closeMenu();
  if (it.cmd) { submit(it.id); return; }
  switch (it.id) {
    case "ctrl-c": sendKeys("\x03"); break;
    case "shift-tab": sendKeys("\x1b[Z"); break;
    case "esc-esc": sendKeys("\x1b"); setTimeout(() => sendKeys("\x1b"), 120); break;
    case "font+": setFont(state.font + 2); break;
    case "font-": setFont(state.font - 2); break;
    case "bottom": term.scrollToBottom(); break;
    case "update":
      if (state.running) { state.pending = "update"; post({ t: "stop" }); } else post({ t: "run", what: "update" });
      break;
    case "stop": post({ t: "stop" }); break;
    case "quit": post({ t: "quit" }); break;
    default: break;
  }
}

// ------------------------------------------------------------------ Schreibfeld
const compose = $("compose");
const composeText = $("compose-text");
function openCompose() {
  if (!state.running) return;
  $("compose-title").textContent = state.running === "shell" || state.running === "update" ? "Eingabe" : "Nachricht an Claude";
  compose.hidden = false;
  composeText.focus();
  composeText.setSelectionRange(composeText.value.length, composeText.value.length);
}
function closeCompose(refocus = true) {
  if (compose.hidden) return;
  compose.hidden = true;
  if (refocus) term.focus();
}
function sendCompose() {
  const text = composeText.value.replace(/\s*\n\s*/g, " ").trim();
  composeText.value = "";
  closeCompose();
  if (text) submit(text);
  else sendKeys("\r");
}

// ------------------------------------------------------------------ Tasten
// Eigene Tasten fängt das Fenster ab, bevor xterm.js sie sieht
term.attachCustomKeyEventHandler((ev) => !["F7", "F8", "F9", "F10", "F12"].includes(ev.key) && !overlayOpen());

function overlayOpen() { return !$("start").hidden || !$("menu").hidden || !compose.hidden; }

window.addEventListener("keydown", (ev) => {
  const key = ev.key;
  if (!$("start").hidden) { ev.preventDefault(); ev.stopPropagation(); startKey(key); return; }
  if (!$("menu").hidden) { ev.preventDefault(); ev.stopPropagation(); menuKey(key); return; }
  if (!compose.hidden) {
    if (key === "Enter" && !ev.shiftKey) { ev.preventDefault(); sendCompose(); }
    else if (key === "Escape") { ev.preventDefault(); closeCompose(); }
    else if (key === "F9") ev.preventDefault();
    else if (key === "F12") { ev.preventDefault(); closeCompose(); openMenu(); }
    return;
  }
  const own = { F7: () => term.scrollPages(-1), F8: () => term.scrollPages(1), F9: openCompose,
                F10: () => sendKeys("\x1b[Z"), F12: openMenu };
  if (own[key]) { ev.preventDefault(); ev.stopPropagation(); own[key](); }
}, true);

// ------------------------------------------------------------------ Nachrichten vom Fenster
window.pc5 = {
  recv(msg) {
    if (msg.t === "out") term.write(decode(msg.b));
    else if (msg.t === "init") {
      state.claude = !!msg.claude;
      state.fontMin = msg.fontMin || state.fontMin;
      state.fontMax = msg.fontMax || state.fontMax;
      if (msg.font && msg.font !== state.font) {
        state.font = msg.font;
        term.options.fontSize = state.font;
        post({ t: "size", ...size() });
      }
      state.running = msg.running || "";
      paintState();
      if (!state.running) showStart();
    } else if (msg.t === "started") {
      state.running = msg.what;
      $("start").hidden = true;
      paintState();
      term.focus();
    } else if (msg.t === "exited") {
      const was = msg.what;
      state.running = "";
      paintState();
      if (state.pending) {
        const next = state.pending;
        state.pending = "";
        term.write("\r\n");
        post({ t: "run", what: next });
        return;
      }
      let note = "";
      let warn = false;
      if (msg.missing) { note = T("Das Programm wurde nicht gefunden."); warn = true; }
      else if (msg.failed) { note = T("Das Programm ließ sich nicht starten."); warn = true; }
      else if (was === "update") note = msg.code === 0 ? T("Aktualisierung fertig.") : T("Aktualisierung gescheitert (Code ") + msg.code + ").";
      else if (was === "shell") note = T("Befehlszeile beendet.");
      else note = T("Claude Code wurde beendet. Mit „Weitermachen“ geht es im selben Gespräch weiter.");
      if (was === "update" && msg.code !== 0) warn = true;
      term.write("\r\n");
      showStart(note, warn);
    }
  },
};

post({ t: "ready", ...size() });
