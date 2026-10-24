// Kachelreihe des Home (07 Abschnitt 2.2). Die Fokuskachel steht immer bei x=172; die Reihe
// gleitet darunter. Jede Kachel ist ein 168er-Kasten, die kleinen sind per transform verkleinert:
// bewegt wird ausschließlich transform (Lage aus --d = Abstand zur Fokuskachel, Rechnung im CSS).
//
// Der Fokus hängt am Eintrag (seiner Kennung), nicht an der Stelle: Kommt die Liste neu, bleibt
// er auf demselben Eintrag. Solange niemand die Reihe bewegt hat, steht er auf dem Eintrag, den
// der Aufrufer als ersten nennt (das zuletzt gespielte Spiel), auch wenn die Liste vorher leer war.
// Kacheln bleiben über Neuaufbauten hinweg dieselben Elemente: Ein geladenes Bild flackert nicht.
import { h } from "../core/dom.js";
import { nav } from "../core/nav.js";
import { sound } from "../core/sound.js";
import { artBox, updateArt } from "./art.js";

// handlers: onChange(entry, index, {repeat}), onPress(entry)
export function createTileRow(handlers = {}) {
  const el = h("div.tile-row", { "data-focus-group": true });
  let entries = [];
  let tiles = [];
  let index = 0;
  let touched = false;            // hat der Nutzer die Reihe schon bewegt?
  const nodes = new Map();        // Kennung -> Kachel

  // Spielkacheln ohne Text und ohne Zeichen darüber (07 2.2): Die Marke der Quelle steht neben dem
  // Titel der Fokuskachel, „nicht verfügbar“ zeigt die gedimmte Kachel (art.js setzt is-dim)
  function build(entry) {
    const node = h("div.tile", { dataset: { id: entry.id } }, artBox(entry), h("span.tile-run"));
    node.addEventListener("press", (e) => { if (handlers.onPress) handlers.onPress(node.entry, e); });
    return node;
  }

  // Gleicht eine Kachel mit dem Stand ihres Eintrags ab (Bild nur, wenn sich die Adresse geändert hat)
  function refresh(node, entry) {
    node.entry = entry;
    updateArt(node.querySelector(".art"), entry);
    node.classList.toggle("is-running", !!entry.running);
  }

  function layout() {
    tiles.forEach((node, i) => {
      const d = i - index;
      node.style.setProperty("--d", d);
      node.classList.toggle("is-before", d < 0);
      node.classList.toggle("is-current", d === 0);
      // Nur die Fokuskachel ist ein Ziel der Fokus-Steuerung; Links/Rechts verschiebt die Reihe
      if (d === 0) { node.setAttribute("data-focus", ""); node.setAttribute("data-focus-default", ""); }
      else { node.removeAttribute("data-focus"); node.removeAttribute("data-focus-default"); }
      node.classList.toggle("is-running", !!entries[i].running);
    });
  }

  // Setzt die Einträge. opts.first: Stelle des Eintrags, auf dem der Fokus steht, solange der
  // Nutzer die Reihe nicht bewegt hat. opts.last: letzte Stelle, auf die der Fokus zurückfällt,
  // wenn sein Eintrag verschwunden ist (damit er nicht auf einer Systemkachel am Ende landet).
  function setEntries(next, opts = {}) {
    const hadFocus = nav.current && el.contains(nav.current);
    const wanted = entries[index] && entries[index].id;
    const before = index;
    entries = next || [];
    const max = Math.max(0, entries.length - 1);
    const found = entries.findIndex((e) => e.id === wanted);
    if (!touched) index = Math.min(opts.first || 0, max);
    else if (found >= 0) index = found;
    else index = Math.min(before, opts.last === undefined ? max : Math.max(0, Math.min(opts.last, max)));
    const same = entries.length === tiles.length && entries.every((e, i) => tiles[i].dataset.id === e.id);
    tiles = entries.map((entry) => {
      let node = nodes.get(entry.id);
      if (!node) { node = build(entry); nodes.set(entry.id, node); }
      refresh(node, entry);
      return node;
    });
    for (const [id, node] of [...nodes]) if (!tiles.includes(node)) nodes.delete(id);
    if (!same) {
      el.classList.add("is-still");            // neue Kacheln sollen nicht an ihren Platz gleiten
      el.replaceChildren(...tiles);
      layout();
      void el.offsetWidth;
      el.classList.remove("is-still");
    } else {
      layout();
    }
    if (hadFocus) focusCurrent();
    return same;
  }

  // Tauscht nur Bilder und Marken der genannten Einträge (lib.changed mit reason "art").
  // Liefert die Einträge, die in dieser Reihe stehen.
  function patch(list) {
    const hit = [];
    for (const entry of list || []) {
      const i = entries.findIndex((e) => e.id === entry.id);
      if (i < 0) continue;
      entries[i] = { ...entry, running: entries[i].running };
      refresh(tiles[i], entries[i]);
      hit.push(entries[i]);
    }
    return hit;
  }

  function focusCurrent() {
    return tiles[index] ? nav.focus(tiles[index]) : false;
  }

  // Verschiebt den Fokus um delta Einträge. Liefert false am Rand.
  function move(delta, repeat = false) {
    const next = Math.max(0, Math.min(entries.length - 1, index + delta));
    // Am Rand: kurzes Anstoßen, beim Halten nur einmal (sound.js)
    if (next === index) { sound.play("edge", { repeat }); return false; }
    const hadFocus = nav.current && el.contains(nav.current);
    index = next;
    touched = true;
    el.classList.toggle("is-fast", !!repeat);
    layout();
    if (hadFocus) nav.focus(tiles[index], { repeat });
    sound.play("focus", { repeat });
    if (handlers.onChange) handlers.onChange(entries[index], index, { repeat });
    return true;
  }

  // Kachel verschieben (E69): tauscht die Fokuskachel mit ihrem Nachbarn; der Fokus wandert mit.
  // lo/hi: Grenzen (Stellen), über die sie nicht hinaus darf (Systemkacheln am Rand)
  function shift(delta, lo = 0, hi = entries.length - 1, repeat = false) {
    const next = index + delta;
    if (next < lo || next > hi || next < 0 || next >= entries.length) { sound.play("edge", { repeat }); return false; }
    [entries[index], entries[next]] = [entries[next], entries[index]];
    [tiles[index], tiles[next]] = [tiles[next], tiles[index]];
    index = next;
    touched = true;
    layout();
    nav.focus(tiles[index], { repeat });
    sound.play("focus", { repeat });
    return true;
  }

  function setMoving(on) {
    for (const node of tiles) node.classList.remove("is-moving");
    if (on && tiles[index]) tiles[index].classList.add("is-moving");
    el.classList.toggle("is-moving", !!on);
  }

  function select(id) {
    const found = entries.findIndex((e) => e.id === id);
    if (found < 0 || found === index) return found >= 0;
    index = found;
    touched = true;
    layout();
    return true;
  }

  function setRunning(ids) {
    for (const e of entries) e.running = ids.has(e.id);
    tiles.forEach((node, i) => refresh(node, entries[i]));
    layout();
  }

  return {
    el, setEntries, patch, move, shift, setMoving, select, focusCurrent, setRunning,
    get entries() { return entries; },
    get index() { return index; },
    get current() { return entries[index] || null; },
    get currentTile() { return tiles[index] || null; },
    get touched() { return touched; },
  };
}
