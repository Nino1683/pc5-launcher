// Allgemeine Liste aus Zeilen (Row-Format aus docs/RPC.md). Einstellungen, Spieleinstellungen
// und die Tafeln des Control Centers zeichnen damit; die Seite kennt keine einzelne Einstellung.
//
// Row: {id, type, label, value, options:[{value,label,note?}], min, max, step, unit, enabled,
//       hint, note, page, confirm:{title,text,ok,cancel}, trial, secret}
// type: toggle | select | slider | text | action | confirm-action | info | page
//   hint   Erklärung; klappt auf, solange die Zeile den Fokus hat
//   note   kurzer Zustand: rechts, wenn die Zeile dort sonst nichts zeigt (Schalter, Unterseite,
//          verdeckter Text), sonst als Unterzeile
//   enabled false = gesperrt (gedimmt). Fokussierbar bleibt sie nur, wenn sie eine Erklärung
//          trägt (hint): so lässt sich nachlesen, warum; Kreuz bewirkt nichts.
// Erweiterungen (alle wahlfrei, für Tafeln und Spieleinstellungen):
//   section  Abschnittstitel; steht er an einer Zeile, erscheint er einmal über der ersten Zeile
//   indent   eingerückter Unterpunkt
//   icon     Symbolname links;  art: EntryView für ein Vorschaubild links
//   type "progress": Fortschritt 0…100 mit valueText (Downloads)
//
// handlers:
//   onSet(row, value) -> Promise<Ergebnis>     toggle, select, slider, text
//   onAction(row) -> Promise<Ergebnis>         action; confirm-action nach bestätigter Rückfrage
//   onResult(row, ergebnis)                    wertet das Ergebnis aus (rows, message, error, page, trial, ui);
//                                              fehlt er, übernimmt die Liste nur rows und meldet error
//   onPage(row)                                Zeile vom Typ page
//   onText(row) -> Promise<string|null>        Bildschirmtastatur; null = abgebrochen
//   onError(text, row)                         Aufruf gescheitert (Ausnahme)
//   onPress(row, node) -> true                 eigene Behandlung von Kreuz
//   onHeld(node)                               nach dem Neuzeichnen trägt dieses Element den
//                                              gehaltenen Fokus (ein Menü oder Dialog liegt darüber)
//   emptyText                                  Text der einen Zeile einer leeren Liste
//   alwaysFocus                                besteht die Liste nur aus gesperrten Zeilen, trägt die erste den Fokus
import { h, icon, cssPx, settledRect } from "../core/dom.js";
import { S } from "../core/strings.js";
import { sound } from "../core/sound.js";
import { openPicker } from "./picker.js";
import { confirm } from "./dialog.js";
import { artBox } from "./art.js";
import { nav } from "../core/nav.js";

const PAGE_ROWS = 5;             // Zeilen je L2/R2
const LOAD_HINT_MS = 300;        // so lange bleibt der Platzhalter einer ladenden Liste leer
// Regler: gehaltene Richtung beschleunigt (Faktor auf die Schrittweite nach so vielen Wiederholungen)
const HOLD_STEPS = [[14, 5], [6, 2]];

function same(a, b) {
  return a === b || (a !== null && a !== undefined && b !== null && b !== undefined && String(a) === String(b));
}

function str(value) {
  if (value === null || value === undefined || value === false) return "";
  if (value === true) return S.on;
  return String(value);
}

function optionLabel(row) {
  const opt = (row.options || []).find((o) => o && same(o.value, row.value));
  if (opt) return str(opt.label === undefined ? opt.value : opt.label);
  return str(row.value);
}

function sliderParts(row) {
  const min = Number(row.min ?? 0);
  const max = Number(row.max ?? 100);
  const span = max - min || 1;
  const raw = Number(row.value ?? min);
  const value = Math.max(min, Math.min(max, Number.isFinite(raw) ? raw : min));
  const step = Number(row.step) > 0 ? Number(row.step) : 1;
  return { min, max, step, p: (value - min) / span, value };
}

function sliderText(row, value) {
  const text = String(Math.round(value * 100) / 100).replace(".", ",");
  const unit = String(row.unit || "").trim();
  return unit ? text + " " + unit : text;
}

// Text, den die Zeile rechts als Wert zeigt (Schalter, Regler und Fortschritt zeichnen selbst)
function valueText(row) {
  switch (row.type) {
    case "toggle": case "slider": case "progress": return "";
    case "select": return optionLabel(row);
    // verdeckte Eingabe: der Kern liefert nie den Wert; steht doch einer da, nur Punkte zeigen
    case "text": return row.secret ? (row.value ? S.secretSet : "") : str(row.value);
    default: return str(row.value);
  }
}

// note steht rechts, wenn dort sonst kein Text stünde
function noteRight(row) {
  return row.note && row.type !== "slider" && row.type !== "progress" && !valueText(row) ? String(row.note) : "";
}
function noteBelow(row) {
  return row.note && !noteRight(row) ? String(row.note) : "";
}

function focusable(row) {
  return row.enabled !== false || !!row.hint;
}

// Was den Aufbau einer Zeile bestimmt; ändert sich nur der Wert, bleibt ihr Element stehen
function signature(row) {
  const art = row.art ? [row.art.id, row.art.art && (row.art.art.tile || row.art.art.cover)] : null;
  return JSON.stringify([row.type || "info", row.enabled !== false, row.label, row.hint, row.icon, art, !!row.indent, !!row.secret]);
}

export function createRowList(handlers = {}) {
  const track = h("div.rows-track");
  // data-scroll: Der rechte Stick rollt die Liste Zeile für Zeile (core/nav.js, E99)
  const el = h("div.rows", { "data-scroll": true }, track);
  const scrollbar = h("div.scrollbar", { hidden: true });
  let rows = [];
  let scroll = 0;
  let loading = false;
  const byEl = new WeakMap();
  const sending = new Map();      // Zeile -> {busy, next, has, confirmed}: Änderungen der Reihe nach, die jüngste gewinnt
  const acting = new Set();       // Aktionen, deren Antwort noch aussteht
  let hold = { id: null, count: 0 };

  const rowById = (id) => rows.find((r) => r.id === id) || null;
  const nodeById = (id) => [...track.querySelectorAll(".row")].find((n) => n.dataset.rowId === String(id)) || null;
  const focusNodes = () => [...track.querySelectorAll(".row[data-focus]")];

  // ---------------------------------------------------------------- Zeichnen
  function side(row) {
    const text = valueText(row);
    const note = noteRight(row);
    const shown = text || note;
    const value = (cls) => (shown ? h("span.row-value" + (cls || ""), { text: shown }) : null);
    switch (row.type) {
      case "toggle":
        return h("div.row-side", value(), h("span.toggle" + (row.value ? ".is-on" : ""), h("span.toggle-knob")));
      case "slider": {
        const s = sliderParts(row);
        return h("div.row-side", h("div.slider", { style: { "--p": s.p } },
          h("span.slider-val", { text: sliderText(row, s.value) }),
          h("div.slider-line", h("div.slider-fill"), h("div.slider-knob"))));
      }
      case "progress": {
        const p = Math.max(0, Math.min(100, Number(row.value) || 0));
        return h("div.row-side", h("span.row-value", { text: row.valueText || "" }),
          h("div.progress" + (row.busy ? ".is-busy" : ""), { style: { "--p": p / 100 } }, h("i")));
      }
      case "page":
        return h("div.row-side", value(), icon("chevron", "row-chevron"));
      case "text":
        return h("div.row-side", h("span.row-value" + (shown ? "" : ".is-none"), { text: shown || S.textEmpty }));
      default:
        return shown ? h("div.row-side", value()) : null;
    }
  }

  function build(row) {
    const off = row.enabled === false;
    const node = h("div.row.is-" + (row.type || "info") + (off ? ".is-off" : "") + (row.indent ? ".is-sub" : ""),
      { "data-focus": focusable(row), dataset: { rowId: row.id === undefined || row.id === null ? "" : String(row.id), sig: signature(row) } },
      row.art ? h("div.row-art", artBox(row.art, { withTitle: false })) : null,
      row.icon ? icon(row.icon, "row-icon") : null,
      h("div.row-main",
        h("div.row-label", { text: row.label || "" }),
        row.hint ? h("div.row-hint", { text: row.hint }) : null));
    byEl.set(node, row);
    refresh(node, row);
    return node;
  }

  // Zeichnet Wert und Zustand einer Zeile neu (das Element und sein Fokus bleiben)
  function refresh(node, row) {
    const main = node.querySelector(".row-main");
    const below = noteBelow(row);
    let noteEl = main.querySelector(".row-note");
    if (below) {
      if (!noteEl) { noteEl = h("div.row-note"); main.querySelector(".row-label").after(noteEl); }
      if (noteEl.textContent !== below) noteEl.textContent = below;
    } else if (noteEl) {
      noteEl.remove();
    }
    const old = node.querySelector(".row-side");
    const next = side(row);
    if (old && next) old.replaceWith(next); else if (old) old.remove(); else if (next) node.append(next);
    node.classList.toggle("is-busy", acting.has(row.id));
    if (node.classList.contains("is-focus")) showFull(node);
  }

  // Ein rechts abgeschnittener Wert (Pfad, langer Satz) steht im Fokus vollständig unter der Zeile
  function showFull(node) {
    const main = node.querySelector(".row-main");
    const old = main.querySelector(".row-full");
    const value = node.querySelector(".row-side > .row-value");
    const clipped = value && value.scrollWidth > value.clientWidth + 1;
    // Steht der Wert ganz darunter, tritt der gekürzte rechts zurück (sonst stand er doppelt da)
    node.classList.toggle("has-full", !!clipped);
    if (!clipped) { if (old) old.remove(); return; }
    if (old) { old.textContent = value.textContent; return; }
    const full = h("div.row-full", { text: value.textContent });
    const hint = main.querySelector(".row-hint");
    if (hint) hint.before(full); else main.append(full);
  }

  function emptyRow() {
    const text = loading ? (loadingText || S.setLoading) : (handlers.emptyText || S.empty);
    // Auch eine leere Liste hat eine Zeile: so bleibt die Regel "genau ein Fokus" erhalten
    return h("div.row.is-info.is-empty" + (loading ? ".is-loading" : ""), { "data-focus": true },
      h("div.row-main", h("div.row-label", { text })));
  }

  function render() {
    // Derselbe Eintrag (sonst dieselbe Stelle) behält den Fokus, wenn die Liste neu kommt. Liegt
    // ein Menü oder Dialog darüber, gilt das für das Element, das den Fokus hält.
    const focused = nav.current && track.contains(nav.current) ? nav.current : null;
    const held = focused ? null : track.querySelector(".row.is-held");
    const keep = focused || held;
    const keepId = keep ? keep.dataset.rowId : null;
    const keepIndex = keep ? focusNodes().indexOf(keep) : -1;

    const old = new Map();
    for (const node of track.querySelectorAll(".row")) {
      const id = node.dataset.rowId;
      if (id && !old.has(id) && !node.classList.contains("is-empty")) old.set(id, node);
    }
    const wanted = [];
    let section = null;
    for (const row of rows) {
      if (row.type === "section") { wanted.push(h("div.row-section", { text: row.label || "" })); section = row.label; continue; }
      if (row.section && row.section !== section) { section = row.section; wanted.push(h("div.row-section", { text: section })); }
      const id = row.id === undefined || row.id === null ? "" : String(row.id);
      let node = id ? old.get(id) : null;
      if (node && node.dataset.sig === signature(row)) {
        old.delete(id);
        byEl.set(node, row);
        node.toggleAttribute("data-focus", focusable(row));
        refresh(node, row);
      } else {
        node = build(row);
      }
      wanted.push(node);
    }
    if (!rows.length) wanted.push(emptyRow());
    // Eine Seite nur aus gesperrten Zeilen: die erste trägt trotzdem den Fokus (genau ein Fokus,
    // Kreis führt zurück)
    if (handlers.alwaysFocus && rows.length && !wanted.some((n) => n.hasAttribute("data-focus"))) {
      const first = wanted.find((n) => n.classList.contains("row"));
      if (first) first.setAttribute("data-focus", "");
    }
    // Nur anfassen, was sich ändert: ein Element, das stehen bleibt, behält Ring und Übergänge
    const keepSet = new Set(wanted);
    for (const child of [...track.children]) if (!keepSet.has(child)) child.remove();
    let at = track.firstChild;
    for (const node of wanted) {
      if (node === at) { at = at.nextSibling; continue; }
      track.insertBefore(node, at);
    }
    if (keep && !keep.isConnected) {
      const nodes = focusNodes();
      const again = nodes.find((n) => n.dataset.rowId === keepId) || nodes[Math.max(0, Math.min(keepIndex, nodes.length - 1))];
      if (again && focused) nav.focus(again);
      else if (again && held) { again.classList.add("is-held"); if (handlers.onHeld) handlers.onHeld(again); }
    }
    updateScroll();
  }

  function updateScroll(target) {
    const tr = settledRect(track);
    const view = settledRect(el).height;
    const content = tr.height;
    const max = Math.max(0, content - view);
    const node = target || track.querySelector(".is-focus") || track.querySelector(".is-held");
    // Gerollte Ränder blenden über --rows-fade aus (CSS is-cut-top/-bottom); die fokussierte Zeile
    // hält so viel Abstand, dass sie selbst nie in diesem Verlauf steht
    const pad = cssPx("--rows-fade");
    if (node && view > 0) {
      const all = track.querySelectorAll("[data-focus]");
      const nr = settledRect(node);
      const top = nr.top - tr.top;
      const bottom = top + nr.height;
      if (node === all[0]) scroll = 0;                               // Abschnittstitel darüber mitzeigen
      else if (node === all[all.length - 1]) scroll = max;            // gesperrte Zeilen darunter mitzeigen
      else if (top - pad < scroll) scroll = top - pad;
      else if (bottom + pad > scroll + view) scroll = bottom + pad - view;
    }
    scroll = Math.max(0, Math.min(max, scroll));
    track.style.setProperty("--scroll", scroll + "px");
    // Eine halb gerollte Zeile lugte sonst oben unter der Überschrift hervor
    el.classList.toggle("is-cut-top", scroll > 0.5);
    el.classList.toggle("is-cut-bottom", scroll < max - 0.5);
    if (max > 0 && view > 0) {
      scrollbar.hidden = false;
      scrollbar.style.height = Math.max(view * 0.1, view * view / content) + "px";
      scrollbar.style.setProperty("--sb-y", (scroll / content) * view + "px");
    } else {
      scrollbar.hidden = true;
    }
  }

  // ---------------------------------------------------------------- Ändern
  function fail(e, row) {
    const text = (e && e.message) || S.settingsFailed;
    if (handlers.onError) handlers.onError(text, row);
    else console.error("rows: " + text);
  }

  function deliver(row, result) {
    if (handlers.onResult) { handlers.onResult(row, result); return; }
    if (!result || typeof result !== "object") return;
    if (Array.isArray(result.rows)) setRows(result.rows);
    if (result.error) fail({ message: result.message }, row);
  }

  function show(id, value) {
    const row = rowById(id);
    const node = nodeById(id);
    if (!row) return;
    row.value = value;
    if (node) refresh(node, row);
  }

  // Wert, den die Zeile gerade von hier aus zeigt, solange ihre Änderung unterwegs ist
  function localValue(id, st) {
    return st.has ? st.next : st.shown;
  }

  // Wert sofort zeigen, dann an den Kern. Je Zeile läuft immer nur ein Aufruf; kommt währenddessen
  // ein neuer Wert (gehaltener Regler, Schalter zweimal), geht danach nur der jüngste hinaus.
  function commit(row, value) {
    const id = row.id;
    let st = sending.get(id);
    if (!st) { st = { busy: false, has: false, next: undefined, shown: value, confirmed: row.value }; sending.set(id, st); }
    st.next = value;
    st.shown = value;
    st.has = true;
    show(id, value);
    if (!st.busy) pump(id, st);
  }

  async function pump(id, st) {
    st.busy = true;
    while (st.has) {
      const value = st.next;
      st.has = false;
      const row = rowById(id) || { id };
      let result;
      try {
        result = handlers.onSet ? await handlers.onSet(row, value) : undefined;
      } catch (e) {
        if (!st.has) { sending.delete(id); show(id, st.confirmed); }
        fail(e, row);
        continue;
      }
      const failed = !!(result && typeof result === "object" && result.error);
      if (!failed) st.confirmed = value;
      if (st.has) continue;                 // ein neuerer Wert wartet: dieses Zwischenergebnis überholen
      sending.delete(id);
      if (failed) show(id, st.confirmed);
      deliver(rowById(id) || row, result);
    }
    st.busy = false;
    if (sending.get(id) === st) sending.delete(id);
  }

  async function runAction(row) {
    if (!handlers.onAction || acting.has(row.id)) return;
    acting.add(row.id);
    const mark = () => { const n = nodeById(row.id); if (n) n.classList.toggle("is-busy", acting.has(row.id)); };
    mark();
    try {
      const result = await handlers.onAction(row);
      acting.delete(row.id);
      mark();
      deliver(rowById(row.id) || row, result);
    } catch (e) {
      acting.delete(row.id);
      mark();
      fail(e, row);
    }
  }

  async function activate(node, ev) {
    const row = byEl.get(node);
    if (!row) { ev.detail.sound = null; return; }
    if (handlers.onPress && handlers.onPress(row, node) === true) return;
    if (row.enabled === false) { ev.detail.sound = "edge"; return; }
    try {
      switch (row.type) {
        case "toggle":
          ev.detail.sound = row.value ? "toggle-off" : "toggle-on";
          commit(row, !row.value);
          break;
        case "select": {
          const value = await openPicker({ options: row.options || [], value: row.value, anchor: node });
          const now = rowById(row.id) || row;
          if (value !== undefined && !same(value, now.value)) commit(now, value);
          break;
        }
        case "text": {
          if (!handlers.onText) { ev.detail.sound = null; break; }
          const text = await handlers.onText(row);
          const now = rowById(row.id) || row;
          // verdeckte Eingabe: der alte Wert ist unbekannt, auch eine leere Eingabe zählt (= entfernen)
          if (typeof text === "string" && (now.secret || text !== str(now.value))) commit(now, text);
          break;
        }
        case "page":
          if (handlers.onPage) handlers.onPage(row);
          break;
        case "confirm-action": {
          const c = row.confirm || {};
          const yes = await confirm({ title: c.title || row.label, text: c.text || row.hint || "", ok: c.ok || row.label, cancel: c.cancel });
          if (yes) await runAction(rowById(row.id) || row);
          break;
        }
        case "action":
          await runAction(row);
          break;
        default:
          ev.detail.sound = null;      // Info-Zeilen und Regler tun bei Kreuz nichts
      }
    } catch (e) {
      fail(e, row);
    }
  }

  track.addEventListener("press", (e) => {
    const node = e.target.closest(".row");
    if (node) activate(node, e);
  });
  track.addEventListener("nav-focus", (e) => {
    const node = e.target.closest(".row");
    if (!node) return;
    showFull(node);
    updateScroll(node);
  });
  track.addEventListener("nav-blur", (e) => {
    const node = e.target.closest(".row");
    const full = node && node.querySelector(".row-full");
    if (full) full.remove();
    if (node) node.classList.remove("has-full");
  });
  // Regler: Links/Rechts ändern den Wert in Schritten, er wirkt sofort. Gehalten wird es schneller.
  track.addEventListener("nav-move", (e) => {
    const node = e.target.closest(".row");
    const row = node && byEl.get(node);
    const dir = e.detail.dir;
    if (!row || row.type !== "slider" || (dir !== "left" && dir !== "right")) return;
    e.preventDefault();
    if (row.enabled === false) { sound.play("edge"); return; }
    const key = row.id + dir;
    hold = e.detail.repeat && hold.id === key ? { id: key, count: hold.count + 1 } : { id: key, count: 0 };
    const factor = (HOLD_STEPS.find(([after]) => hold.count >= after) || [0, 1])[1];
    const s = sliderParts(row);
    const moved = s.value + (dir === "right" ? 1 : -1) * s.step * factor;
    // auf dem Raster der Schrittweite bleiben, an den Enden anschlagen
    const snapped = s.min + Math.round((moved - s.min) / s.step) * s.step;
    const next = Math.round(Math.max(s.min, Math.min(s.max, snapped)) * 1e6) / 1e6;
    if (next === s.value) { sound.play("edge"); return; }
    sound.play("focus", { repeat: !!e.detail.repeat });
    commit(row, next);
  });

  function setRows(next) {
    loading = false;
    clearTimeout(waitTimer);
    el.classList.remove("is-waiting");
    rows = (next || []).filter((r) => r && typeof r === "object").map((r) => ({ ...r }));
    // Eine Zeile, deren Änderung noch unterwegs ist, zeigt weiter den Wert von hier: Sonst springt
    // der Regler zurück, wenn zwischendurch ein älterer Stand der Seite eintrifft
    for (const [id, st] of sending) {
      const row = rowById(id);
      if (row) row.value = localValue(id, st);
    }
    render();
  }

  // Platzhalter, solange die Seite geladen wird. Sein Text („Wird geladen …“ oder text) erscheint
  // erst nach delayMs: Schnelle Seiten (fast alle) blitzen so nicht auf.
  let waitTimer = null;
  let loadingText = null;
  function setLoading(delayMs = LOAD_HINT_MS, text = null) {
    loading = true;
    loadingText = text;
    rows = [];
    render();
    clearTimeout(waitTimer);
    el.classList.toggle("is-waiting", delayMs > 0);
    if (delayMs > 0) waitTimer = setTimeout(() => el.classList.remove("is-waiting"), delayMs);
  }

  // L2/R2: um eine Seite blättern (07 Abschnitt 8)
  function page(delta) {
    const nodes = focusNodes();
    const at = nodes.indexOf(nav.current);
    if (at < 0) return false;
    const next = nodes[Math.max(0, Math.min(nodes.length - 1, at + delta * PAGE_ROWS))];
    if (!next || next === nav.current) { sound.play("edge"); return true; }
    nav.focus(next);
    sound.play("focus");
    return true;
  }

  return {
    el, scrollbar, setRows, setLoading, page,
    get rows() { return rows; },
    get loading() { return loading; },
    rowOf: (node) => byEl.get(node),
    nodeOf: nodeById,
    first: () => focusNodes()[0] || null,
    update: () => updateScroll(),
    // Zeile mit dieser Kennung in den Sichtbereich rollen (ohne Fokus; z. B. der markierte Bereich)
    show: (id) => { const node = nodeById(id); if (node) updateScroll(node); },
    // Rollstand in Pixeln (eine zweite Liste soll genauso stehen, z. B. die Bereiche einer Unterseite)
    get scrollTop() { return scroll; },
    setScroll(px) { scroll = Math.max(0, Number(px) || 0); updateScroll(); },
  };
}
