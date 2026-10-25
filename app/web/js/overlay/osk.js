// Bildschirmtastatur (07 Abschnitt 6.3): deutsche Belegung (QWERTZ mit Umlauten und ß), Ebene
// für Ziffern und Zeichen, Umschalten einmal oder fest.
//   purpose "field": Text entsteht hier (Feld mit Schreibmarke), „Fertig“ liefert osk.done {text}
//   purpose "app":   jedes Zeichen geht sofort über osk.type / osk.key an die App im Vordergrund
//   secret           Kennwort: das Feld zeigt Punkte, „Kennwort anzeigen“ (Auge, R3) deckt auf
//   learn: false     Vorschläge ja, aber nichts merken (Text-Editor, E99: in Dateien stehen auch Kennwörter)
// Pad: Kreuz = Taste, Quadrat = löschen, Dreieck = Leerzeichen, L2 = Umschalten (zweimal = fest),
// L3 = Zeichen-Ebene, L1/R1 = Schreibmarke, R2 oder Optionen = Fertig, Kreis = schließen ohne Übernahme,
// R3 = ersten Wortvorschlag übernehmen (Kennwort: Kennwort anzeigen). Vorschläge: Leiste über den Tasten
// (core/predict.js), nie bei Kennwörtern; gelernt wird nur aus eigenen Feldern des PC5 Launchers.
import { h, icon, setIcon } from "../core/dom.js";
import { notify } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { sound } from "../core/sound.js";
import { LANGUAGE, S } from "../core/strings.js";
import { openMenu } from "../components/menu.js";
import { learn, loadWords, suggest } from "../core/predict.js";

const LETTERS_DE = [
  ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "ß"],
  ["q", "w", "e", "r", "t", "z", "u", "i", "o", "p", "ü"],
  ["a", "s", "d", "f", "g", "h", "j", "k", "l", "ö", "ä"],
  ["y", "x", "c", "v", "b", "n", "m", ",", "."],
];
// Englisch (2026-10-25): QWERTY, statt der Umlaute häufige Zeichen von Adressen und Namen
const LETTERS_EN = [
  ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "-"],
  ["q", "w", "e", "r", "t", "y", "u", "i", "o", "p", "@"],
  ["a", "s", "d", "f", "g", "h", "j", "k", "l", "'", "?"],
  ["z", "x", "c", "v", "b", "n", "m", ",", "."],
];
const LETTERS = LANGUAGE === "en" ? LETTERS_EN : LETTERS_DE;
const SYMBOLS = [
  ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "?"],
  ["!", "\"", "§", "$", "%", "&", "/", "(", ")", "=", "+"],
  ["@", "#", ":", ";", "-", "_", "*", "'", "€", "<", ">"],
  ["[", "]", "{", "}", "\\", "|", "~", ",", "."],
];
const UPPER = { "ß": "ẞ" };
const DOT = "•";

export function createOsk(params, api) {
  const purpose = params.purpose === "field" ? "field" : "app";
  const titleText = String(params.title || "");
  // Verdeckt nur über params.secret (der Kern reicht es durch, shell.osk_open); das unsichtbare
  // Zeichen vor dem Titel gibt es seit E99 nicht mehr
  const secret = purpose === "field" && params.secret === true;
  const learnWords = !secret && params.learn !== false;
  const state = { shift: 0, symbols: false, text: String(params.initial || ""), caret: 0, reveal: false };
  state.caret = state.text.length;
  const before = h("span.osk-before");
  const afterCaret = h("span.osk-after");
  const eye = secret ? h("div.osk-eye", { "data-focus": true, dataset: { key: "reveal" } }, icon("eye"), h("span.key-hint", { text: "R3" })) : null;
  const field = h("div.osk-field" + (secret ? ".is-secret" : ""), h("div.osk-text", before, h("span.caret"), afterCaret), eye);
  const keysBox = h("div.osk-keys");
  const suggBox = h("div.osk-sugg");
  const panel = h("div.osk", { "data-bounds": true },
    purpose === "field" ? h("div.osk-title.ell", { text: titleText || S.oskTitle }) : null,
    purpose === "field" ? field : null,
    secret ? null : suggBox,
    keysBox);
  // Bei Eingaben an eine App kennt die Tastatur nur, was sie selbst getippt hat: das angefangene Wort
  let appWord = "";
  let keyRows = [];
  let suggKeys = [];
  let suggWords = [];
  // Eigenes Feld: die Seite dahinter wird abgedunkelt (overlay.css .osk-view.is-field)
  const el = h("div.osk-view" + (purpose === "field" ? ".is-field" : ""), panel);
  let grid = [];                 // Zeilen aus Tasten-Elementen, für die Bewegung mit Umbruch

  if (eye) eye.addEventListener("press", (e) => { e.detail.sound = null; toggleReveal(); });

  function charOf(base) {
    if (state.symbols || !state.shift) return base;
    return UPPER[base] || base.toUpperCase();
  }

  function key(def) {
    const node = h("div.key" + (def.cls ? "." + def.cls.split(" ").join(".") : ""), { "data-focus": true, dataset: { key: def.id || def.char } },
      def.icon ? icon(def.icon) : h("span.key-label", { text: def.label !== undefined ? def.label : charOf(def.char) }),
      def.hint ? h("span.key-hint", { text: def.hint }) : null);
    node.addEventListener("press", (e) => { e.detail.sound = null; def.run ? def.run() : type(charOf(def.char)); });
    return node;
  }

  function build() {
    // Der Fokus bleibt an derselben Stelle des Rasters, auch wenn die Ebene wechselt
    let pos = null;
    if (nav.current && keysBox.contains(nav.current)) {
      const r = grid.findIndex((row) => row.includes(nav.current));
      if (r >= 0) pos = [r, grid[r].indexOf(nav.current)];
    }
    const set = state.symbols ? SYMBOLS : LETTERS;
    const rows = set.slice(0, 3).map((row) => row.map((char) => key({ char })));
    rows.push([
      key({ id: "shift", icon: "shift", hint: "L2", cls: "is-fn" + (state.shift === 1 ? " is-on" : state.shift === 2 ? " is-on is-lock" : ""), run: toggleShift }),
      ...set[3].map((char) => key({ char })),
      key({ id: "backspace", icon: "backspace", hint: "□", cls: "is-fn", run: backspace }),
    ]);
    rows.push([
      key({ id: "symbols", label: state.symbols ? "abc" : "@#:", hint: "L3", cls: "is-fn is-w2" + (state.symbols ? " is-on" : ""), run: toggleSymbols }),
      key({ id: "left", label: "◀", hint: "L1", cls: "is-fn", run: () => moveCaret(-1) }),
      key({ id: "space", label: S.oskSpace, hint: "△", cls: "is-fn is-w4", run: () => type(" ") }),
      key({ id: "right", label: "▶", hint: "R1", cls: "is-fn", run: () => moveCaret(1) }),
      key({ id: "more", label: "…", cls: "is-fn", run: more }),
      key({ id: "done", label: purpose === "field" ? S.oskDone : S.oskEnter, hint: "R2", cls: "is-done is-w2", run: done }),
    ]);
    keyRows = rows;
    keysBox.replaceChildren(...rows.map((row) => h("div.key-row", row)));
    setGrid();
    if (pos) nav.focus(grid[pos[0]][Math.min(pos[1], grid[pos[0]].length - 1)]);
  }

  // Das Auge im Feld und die Vorschläge sind die obersten „Zeilen“: Hoch aus den Ziffern führt dorthin
  function setGrid() {
    grid = [...(eye ? [[eye]] : []), ...(suggKeys.length ? [suggKeys] : []), ...keyRows];
  }

  // ---------------------------------------------------------------- Vorschläge
  function currentWord() {
    if (purpose === "app") return appWord;
    const m = state.text.slice(0, state.caret).match(/\S+$/);
    return m ? m[0] : "";
  }
  function refreshSuggestions() {
    if (secret) return;
    const wasHere = nav.current && suggBox.contains(nav.current);
    suggWords = suggest(currentWord());
    suggKeys = suggWords.map((w, i) => key({ id: "sugg" + i, label: w, hint: i === 0 ? "R3" : null, cls: "is-sugg", run: () => pick(w) }));
    suggBox.replaceChildren(...suggKeys);
    setGrid();
    if (wasHere) nav.focus(suggKeys[0] || keyRows[0][0]);
  }
  function pick(word) {
    const cur = currentWord();
    if (!cur) { sound.play("edge"); return; }
    sound.play("select");
    if (purpose === "app") {
      for (let i = 0; i < [...cur].length; i++) notify("osk.key", { name: "BackSpace" });
      notify("osk.type", { text: word + " " });
      appWord = "";
    } else {
      const head = state.text.slice(0, state.caret - cur.length);
      state.text = head + word + " " + state.text.slice(state.caret);
      state.caret = head.length + word.length + 1;
      paintField();
    }
    if (state.shift === 1) { state.shift = 0; build(); }
    refreshSuggestions();
  }

  function paintField() {
    const head = state.text.slice(0, state.caret);
    const tail = state.text.slice(state.caret);
    const hide = secret && !state.reveal;
    // Punkte je Zeichen (nicht je Code-Einheit)
    before.textContent = hide ? DOT.repeat([...head].length) : head;
    afterCaret.textContent = hide ? DOT.repeat([...tail].length) : tail;
  }

  // ---------------------------------------------------------------- Eingabe
  function type(text) {
    sound.play("focus");
    if (purpose === "app") {
      notify("osk.type", { text });
      appWord = /\s/.test(text) ? "" : appWord + text;
    } else {
      state.text = state.text.slice(0, state.caret) + text + state.text.slice(state.caret);
      state.caret += text.length;
      paintField();
    }
    // Einmaliges Umschalten gilt für genau ein Zeichen
    if (state.shift === 1) { state.shift = 0; build(); }
    refreshSuggestions();
  }
  function backspace() {
    sound.play("focus");
    if (purpose === "app") {
      notify("osk.key", { name: "BackSpace" });
      appWord = [...appWord].slice(0, -1).join("");
      refreshSuggestions();
      return;
    }
    if (state.caret === 0) { sound.play("edge"); return; }
    // ganze Zeichen löschen, auch wenn sie aus zwei Code-Einheiten bestehen
    const head = [...state.text.slice(0, state.caret)];
    const removed = head.pop();
    state.text = head.join("") + state.text.slice(state.caret);
    state.caret -= removed.length;
    paintField();
    refreshSuggestions();
  }
  function moveCaret(delta) {
    if (purpose === "app") {
      notify("osk.key", { name: delta < 0 ? "Left" : "Right" });
      appWord = "";                       // die Schreibmarke steht jetzt woanders: kein Wort mehr bekannt
      refreshSuggestions();
      return;
    }
    const chars = [...state.text];
    // Schreibmarke in ganzen Zeichen bewegen
    let index = [...state.text.slice(0, state.caret)].length + delta;
    if (index < 0 || index > chars.length) { sound.play("edge"); return; }
    index = Math.max(0, Math.min(chars.length, index));
    state.caret = chars.slice(0, index).join("").length;
    sound.play("focus");
    paintField();
    refreshSuggestions();
  }
  function toggleShift() { state.shift = (state.shift + 1) % 3; sound.play(state.shift ? "toggle-on" : "toggle-off"); build(); }
  function toggleSymbols() { state.symbols = !state.symbols; sound.play("tab"); build(); }
  function toggleReveal() {
    if (!secret) return;
    state.reveal = !state.reveal;
    sound.play(state.reveal ? "toggle-on" : "toggle-off");
    setIcon(eye.querySelector(".ic"), state.reveal ? "eye-off" : "eye");
    eye.classList.toggle("is-on", state.reveal);
    paintField();
  }
  function done() {
    sound.play("select");
    if (purpose === "app") notify("osk.key", { name: "Return" });
    else {
      if (learnWords) learn(state.text);
      notify("osk.done", { text: state.text });
    }
    api.close();
  }
  async function more() {
    const items = purpose === "field"
      ? [{ id: "clear", label: S.oskClear, icon: "trash" }]
      : [{ id: "Tab", label: S.oskTab }, { id: "Escape", label: S.oskEscape }];
    const choice = await openMenu(items, { cls: "menu-osk", layerCls: "is-clear" });
    if (choice === "clear") { state.text = ""; state.caret = 0; paintField(); refreshSuggestions(); }
    else if (choice) { notify("osk.key", { name: choice }); appWord = ""; refreshSuggestions(); }
  }

  // ---------------------------------------------------------------- Bewegung: in der Zeile mit Umbruch
  panel.addEventListener("nav-move", (e) => {
    const cur = e.target.closest(".key, .osk-eye");
    const r = grid.findIndex((row) => row.includes(cur));
    if (r < 0) return;
    e.preventDefault();
    const c = grid[r].indexOf(cur);
    let target = null;
    const { dir, repeat } = e.detail;
    if (dir === "left") target = grid[r][(c - 1 + grid[r].length) % grid[r].length];
    else if (dir === "right") target = grid[r][(c + 1) % grid[r].length];
    else {
      const nr = r + (dir === "down" ? 1 : -1);
      if (nr < 0 || nr >= grid.length) { sound.play("edge"); return; }
      // die Taste, deren Mitte der eigenen am nächsten liegt
      const box = cur.getBoundingClientRect();
      const x = box.left + box.width / 2;
      let best = Infinity;
      for (const k of grid[nr]) {
        const b = k.getBoundingClientRect();
        const dist = x >= b.left && x <= b.right ? 0 : Math.min(Math.abs(x - b.left), Math.abs(x - b.right));
        if (dist < best) { best = dist; target = k; }
      }
    }
    if (target === cur) { sound.play("edge"); return; }
    if (target && nav.focus(target, { dir, repeat })) sound.play("focus", { repeat });
  });

  function onPad(button, ev) {
    if (ev.repeat && button !== "square" && button !== "l1" && button !== "r1") return false;
    switch (button) {
      case "square": backspace(); return true;
      case "triangle": type(" "); return true;
      case "l2": toggleShift(); return true;
      case "l3": toggleSymbols(); return true;
      case "r3":
        if (secret) toggleReveal();
        else if (suggWords.length) pick(suggWords[0]);
        else sound.play("edge");
        return true;
      case "l1": moveCaret(-1); return true;
      case "r1": moveCaret(1); return true;
      case "r2": case "options": done(); return true;
      default: return false;
    }
  }

  build();
  paintField();
  // Start auf „g“: Mitte der Tastatur, kurze Wege in alle Richtungen
  keyRows[2][4].setAttribute("data-focus-default", "");
  if (!secret) loadWords().then(refreshSuggestions);
  return { el, onPad, get text() { return state.text; } };
}
