// Dialog, Rückfrage, Informationen, Protokoll (07 Abschnitt 4.3 „Rückfrage/Dialog“ und 6.8).
// Der Fokus liegt beim Öffnen auf der ungefährlichen Wahl; Kreis bedeutet „Abbrechen“.
import { h } from "../core/dom.js";
import { sound } from "../core/sound.js";
import { S, fmt } from "../core/strings.js";
import { openLayer } from "./layer.js";

function button(def) {
  return h("div.btn.is-secondary.dialog-btn", { "data-focus": true, dataset: { id: def.id } }, h("span.btn-label", { text: def.label }));
}

// opts: title, text, detail (zweiter, leiserer Absatz), buttons [{id, label}], initial (id),
//       content (Element), wide, icon (Symbolname über dem Titel),
//       timeout (Sekunden) + timeoutResult (id): läuft die Zeit ab, gilt diese Wahl
// Ergebnis: id des Knopfs, null (Kreis) oder CLOSED (der Bildschirm darunter hat gewechselt)
export const CLOSED = "__closed";
export function dialog(opts) {
  return new Promise((resolve) => {
    let result = null;
    let timer = null;
    const buttons = (opts.buttons || [{ id: "ok", label: S.ok }]).map(button);
    const seconds = Math.max(0, Math.ceil(Number(opts.timeout) || 0));
    const countText = h("span.dialog-count-text");
    const countBar = h("div.dialog-timer", h("i"));
    const count = h("div.dialog-count", countText, countBar);
    const panel = h("div.dialog" + (opts.wide ? ".is-wide" : ""), { "data-bounds": true },
      opts.title ? h("div.dialog-title", { text: opts.title }) : null,
      opts.text ? h("div.dialog-text", { text: opts.text }) : null,
      opts.detail ? h("div.dialog-detail", { text: opts.detail }) : null,
      opts.content || null,
      seconds ? count : null,
      // mehr als zwei Knöpfe stehen untereinander (nebeneinander würden die Beschriftungen gekürzt)
      h("div.dialog-buttons" + (buttons.length > 2 ? ".is-stack" : ""), buttons));
    const wrap = h("div.dialog-wrap", panel);
    const initial = buttons.find((b) => b.dataset.id === opts.initial) || buttons[0];
    const layer = openLayer({
      panel: wrap, cls: "is-dialog", name: "dialog", initial,
      onPad: opts.onPad ? (b, ev) => opts.onPad(b, ev, panel) : null,
      onClose: (reason) => { clearInterval(timer); resolve(reason === "external" ? CLOSED : result); },
    });
    for (const b of buttons) {
      b.addEventListener("press", () => { result = b.dataset.id; layer.close(); });
    }
    // Lange Beschriftung (Satz des Kerns, etwa „Steam Big Picture öffnen“): Die Tafel wird breiter,
    // statt die Wahl auf „Zum Desktop wechs…“ zu kürzen; links/rechts bleibt, wie es ist
    if (buttons.length <= 2 && buttons.some((b) => b.firstChild.scrollWidth > b.firstChild.clientWidth + 1)) panel.classList.add("is-roomy");
    if (seconds) {
      // Die Zeit zählt nach der Uhr, nicht nach Takten: Ein Zeitgeber, der sich verspätet (Seite
      // kurz nicht gezeichnet), darf die Frist nicht verlängern
      const end = performance.now() + seconds * 1000;
      const tick = () => {
        const left = Math.max(0, Math.ceil((end - performance.now()) / 1000));
        countText.textContent = fmt(S.confirmCountdown, { n: left });
        if (left <= 0) { result = opts.timeoutResult === undefined ? null : opts.timeoutResult; layer.close(); }
      };
      tick();
      timer = setInterval(tick, 250);
      // Balken läuft in derselben Zeit leer (nur transform)
      countBar.style.setProperty("--d", seconds + "s");
      requestAnimationFrame(() => requestAnimationFrame(() => countBar.classList.add("is-running")));
    }
    if (opts.onOpen) opts.onOpen(layer, panel);
  });
}

// Rückfrage mit zwei Knöpfen. Ergebnis true nur bei ausdrücklicher Bestätigung.
export async function confirm({ title, text, detail, ok, cancel, timeout, defaultOk = false }) {
  const result = await dialog({
    title, text, detail, timeout, timeoutResult: defaultOk ? "ok" : "cancel",
    buttons: [{ id: "cancel", label: cancel || S.cancel }, { id: "ok", label: ok || S.ok }],
    initial: defaultOk ? "ok" : "cancel",
  });
  return result === "ok";
}

// Hinweis oder Fehlermeldung mit einem Knopf
export function messageDialog({ title, text, detail, ok }) {
  return dialog({ title, text, detail, buttons: [{ id: "ok", label: ok || S.ok }] });
}

// Probezeit einer Einstellung (HDR, VRR, Bluetooth aus): Frage mit Countdown. Vorgabe ist
// „Zurück“; Kreis, Zeitablauf und ein Wechsel des Bildschirms gelten ebenso als „Zurück“.
// Ergebnis: true = behalten
export async function trialDialog({ title, text, seconds, keep, revert }) {
  const result = await dialog({
    title: title || S.trialTitle, text: text || "", timeout: seconds || 15, timeoutResult: "revert",
    buttons: [{ id: "revert", label: revert || S.trialRevert }, { id: "keep", label: keep || S.trialKeep }],
    initial: "revert",
  });
  return result === "keep";
}

// Liste aus Bezeichnung und Wert (Menüpunkt „Informationen“)
export function infoDialog({ title, items }) {
  const list = h("div.dialog-info", (items || []).map((it) => h("div", h("span", { text: it.label }), h("span", { text: it.value }))));
  return dialog({ title, content: list, buttons: [{ id: "ok", label: S.close }] });
}

// Langer Text zum Blättern in einer breiten Tafel (Hoch/Runter und rechter Stick): box schneidet
// ab, body rollt über --scroll um 40 % der Höhe je Schritt (Polsterung von box zählt nicht mit).
// Gemeinsamer Baustein für Protokoll und Beschreibung eines Spiels im Store (E99)
export function scrollTextDialog({ title, box, body }) {
  let scroll = 0;
  const step = (dir) => {
    const style = getComputedStyle(box);
    const room = box.clientHeight - (parseFloat(style.paddingTop) || 0) - (parseFloat(style.paddingBottom) || 0);
    const max = Math.max(0, body.scrollHeight - room);
    const next = Math.max(0, Math.min(max, scroll + dir * box.clientHeight * 0.4));
    if (next === scroll) return false;
    scroll = next;
    body.style.setProperty("--scroll", scroll + "px");
    return true;
  };
  return dialog({
    title, content: box, wide: true, buttons: [{ id: "ok", label: S.close }],
    onPad: (b) => {
      if (b === "up" || b === "rs-up") { step(-1); return true; }
      if (b === "down" || b === "rs-down") { step(1); return true; }
      return false;
    },
  });
}

// Protokoll zum Blättern
export function logDialog({ title, text }) {
  const pre = h("pre", { text: text || S.logEmpty });
  return scrollTextDialog({ title, box: h("div.dialog-log", pre), body: pre });
}

// Bildbetrachter (Datei-Explorer, Bilder eines Spiels im Store; E99 ein Baustein statt zwei):
// großes Bild, links/rechts blättern, Kreuz und Kreis schließen.
//   count       Zahl der Bilder;  start  das erste (Stelle)
//   src(i)      Adresse des Bildes i oder ein Promise darauf (null/Fehler: kein Bild)
//   caption(i)  Unterschrift zu Bild i
// Bild und Unterschrift wechseln zusammen, erst wenn das neue Bild dekodiert ist: Vorher stand die
// neue Unterschrift schon unter dem alten Bild. Eine laufende Nummer verwirft späte Antworten, wenn
// inzwischen weitergeblättert wurde.
export function imageViewer({ count, start = 0, src, caption }) {
  const total = Math.max(0, Number(count) || 0);
  if (!total) return null;
  let i = Math.max(0, Math.min(total - 1, Number(start) || 0));
  let serial = 0;
  const img = h("img.ex-image", { alt: "" });
  const cap = h("div.ex-caption.ell");
  const show = async () => {
    const mine = ++serial;
    const at = i;
    let url = null;
    try { url = await src(at); } catch (e) { url = null; }
    if (mine !== serial) return;
    if (url) {
      const next = new Image();
      next.src = url;
      try { await next.decode(); } catch (e) { /* kaputtes Bild: trotzdem zeigen, was da ist */ }
      if (mine !== serial) return;
      img.src = url;
    } else {
      img.removeAttribute("src");
    }
    cap.textContent = caption(at);
  };
  const layer = openLayer({
    panel: h("div.ex-viewer", img, cap), cls: "ex-viewer-layer",
    onPad(button, ev) {
      if ((button === "left" || button === "right") && total > 1) {
        i = (i + (button === "right" ? 1 : total - 1)) % total;
        sound.play("focus", { repeat: ev && ev.repeat });
        show();
        return true;
      }
      if (button === "cross") { layer.close(); return true; }
      return false;
    },
  });
  show();
  return layer;
}
