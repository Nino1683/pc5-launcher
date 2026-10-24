// Auswahlliste (07 Abschnitt 4.3): Tafel 504 breit über der Wertespalte, Zeilen im 72er-Raster,
// Haken vor dem aktiven Wert. Lange Listen rollen per translate3d; L2/R2 blättern um eine Seite.
import { h, icon, cssMs, cssPx, settledRect } from "../core/dom.js";
import { nav } from "../core/nav.js";
import { sound } from "../core/sound.js";
import { openLayer } from "./layer.js";

const PAGE = 6;                 // Einträge je L2/R2

function same(a, b) {
  return a === b || (a !== null && a !== undefined && b !== null && b !== undefined && String(a) === String(b));
}

// Laufschrift (07 10.2): Ein zu langer Eintrag läuft im Fokus nach 1,5 s einmal bis zu seinem Ende
// durch, steht dort kurz und springt wieder an den Anfang. Nur einmal: keine Daueranimation.
function marquee(label) {
  const inner = label && label.firstChild;
  if (!inner) return () => {};
  const distance = label.scrollWidth - label.clientWidth;
  if (distance <= 1) return () => {};
  const runMs = Math.round(distance / cssPx("--marquee-step") * 1000);
  const reset = () => {
    label.classList.remove("is-mq");
    inner.style.removeProperty("--mq-x");
  };
  let back = null;
  const timer = setTimeout(() => {
    label.classList.add("is-mq");
    inner.style.setProperty("--mq-x", -distance + "px");
    inner.style.setProperty("--mq-t", runMs + "ms");
    back = setTimeout(reset, runMs + cssMs("--t-marquee-delay"));
  }, cssMs("--t-marquee-delay"));
  return () => {
    clearTimeout(timer);
    clearTimeout(back);
    reset();
  };
}

// options: [{value, label, note?}], value: aktueller Wert, anchor: Zeile, über der die Tafel erscheint,
// within: Fläche, in der die Tafel bleiben soll (z. B. der Inhalt einer Tafel des Control Centers)
// Ergebnis: gewählter Wert oder undefined (abgebrochen)
export function openPicker({ options, value, anchor, within = null }) {
  const list = (options || []).filter((o) => o && typeof o === "object");
  if (!list.length) return Promise.resolve(undefined);
  return new Promise((resolve) => {
    let result;
    const track = h("div.picker-track");
    // data-scroll: Der rechte Stick rollt die Auswahl (core/nav.js, E99)
    const panel = h("div.picker", { "data-bounds": true, "data-scroll": true }, track);
    let initial = null;
    let stopMarquee = () => {};
    const items = [];
    for (const opt of list) {
      const current = same(opt.value, value);
      const el = h("div.picker-item" + (current ? ".is-current" : "") + (opt.note ? ".has-note" : ""), { "data-focus": true },
        icon("check"),
        h("div.picker-text",
          h("span.picker-label", h("span.mq", { text: opt.label === undefined || opt.label === null ? String(opt.value) : String(opt.label) })),
          opt.note ? h("small", { text: opt.note }) : null));
      el.addEventListener("press", () => { result = opt.value; layer.close(); });
      if (current && !initial) initial = el;
      items.push(el);
      track.append(el);
    }
    track.addEventListener("nav-focus", (e) => {
      const item = e.target.closest(".picker-item");
      reveal(item);
      stopMarquee();
      stopMarquee = marquee(item && item.querySelector(".picker-label"));
    });

    function reveal(el) {
      if (!el) return;
      const view = panel.clientHeight;
      const max = Math.max(0, track.offsetHeight - view);
      let scroll = parseFloat(track.style.getPropertyValue("--scroll")) || 0;
      if (el.offsetTop < scroll) scroll = el.offsetTop - cssPx("--menu-inset");
      else if (el.offsetTop + el.offsetHeight > scroll + view) scroll = el.offsetTop + el.offsetHeight - view + cssPx("--menu-inset");
      track.style.setProperty("--scroll", Math.max(0, Math.min(max, scroll)) + "px");
    }

    function page(delta) {
      const at = items.indexOf(nav.current);
      const next = items[Math.max(0, Math.min(items.length - 1, (at < 0 ? 0 : at) + delta))];
      if (!next || next === nav.current) { sound.play("edge"); return; }
      nav.focus(next);
      sound.play("focus");
    }

    const layer = openLayer({
      panel, name: "picker", cls: "is-light", initial,
      onPad: (button) => {
        if (button === "l2") { page(-PAGE); return true; }
        if (button === "r2") { page(PAGE); return true; }
        return false;
      },
      onClose: () => { stopMarquee(); resolve(result); },
    });

    // Lage: neben dem Anfang der Zeile, senkrecht auf die Zeile zentriert, im Bild (bzw. in within) gehalten
    const margin = cssPx("--picker-view-margin");
    const box = within ? settledRect(within) : null;
    const minTop = box ? box.top : margin;
    const maxBottom = box ? box.bottom : innerHeight - margin;
    if (box) panel.style.maxHeight = Math.max(0, maxBottom - minTop) + "px";
    const a = anchor ? settledRect(anchor) : { left: 0, top: innerHeight / 2, height: 0, right: innerWidth };
    const height = panel.offsetHeight;
    const left = Math.min(a.left + cssPx("--picker-offset"), innerWidth - margin - panel.offsetWidth);
    const top = Math.max(minTop, Math.min(maxBottom - height, a.top + a.height / 2 - height / 2));
    panel.style.left = Math.max(margin, left) + "px";
    panel.style.top = top + "px";
    if (initial) reveal(initial);
  });
}
