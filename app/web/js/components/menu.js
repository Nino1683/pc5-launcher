// Menü (07 Abschnitt 6.7): Tafel mit Einträgen im 72er-Raster. Für die Optionen eines Eintrags
// und das Ein/Aus-Menü.
import { h, icon } from "../core/dom.js";
import { openLayer } from "./layer.js";

// items: [{id, label, icon?, enabled?, note?}] oder {sep: true}
// opts: cls (Lage der Tafel über CSS), style (Lage in Pixeln), layerCls, initial (id), title (Kopfzeile)
// Ergebnis: id des gewählten Eintrags oder null
export function openMenu(items, opts = {}) {
  return new Promise((resolve) => {
    let result = null;
    // data-scroll: Der rechte Stick geht die Einträge durch wie das Steuerkreuz (core/nav.js, E99)
    const panel = h("div.menu" + (opts.cls ? "." + opts.cls : ""), { "data-bounds": true, "data-scroll": true, style: opts.style || null });
    if (opts.title) panel.append(h("div.menu-title.ell", { text: opts.title }));
    for (const item of items) {
      if (!item) continue;
      if (item.sep) { panel.append(h("div.menu-sep")); continue; }
      const off = item.enabled === false;
      const el = h("div.menu-item" + (off ? ".is-off" : ""), { "data-focus": !off, dataset: { id: item.id } },
        item.icon ? icon(item.icon) : null,
        h("span", { text: item.label }),
        item.note ? h("small", { text: item.note }) : null);
      el.addEventListener("press", () => { result = item.id; layer.close(); });
      panel.append(el);
    }
    const layer = openLayer({
      panel, name: "menu", cls: opts.layerCls || "",
      initial: opts.initial ? panel.querySelector('[data-id="' + opts.initial + '"]') : null,
      onPad: (b, ev) => {
        // Optionen schließt das Menü wieder (07 Abschnitt 8)
        if (b === "options" && !ev.repeat) { layer.close(); return true; }
        return opts.onPad ? opts.onPad(b, ev) === true : false;
      },
      onClose: () => resolve(result),
    });
    // Lange Listen (etwa die Genres im Store) sind höher als die Tafel: der gewählte Eintrag bleibt im Bild
    // (Wunsch 2026-10-19: „Bei der Dropdown-Liste scrollen die Einträge nicht mit nach unten“)
    const follow = (el, smooth) => {
      if (!el || panel.scrollHeight <= panel.clientHeight) return;
      const pad = el.offsetHeight;
      let top = panel.scrollTop;
      if (el.offsetTop - pad < top) top = el.offsetTop - pad;
      else if (el.offsetTop + el.offsetHeight + pad > top + panel.clientHeight) top = el.offsetTop + el.offsetHeight + pad - panel.clientHeight;
      top = Math.max(0, Math.min(top, panel.scrollHeight - panel.clientHeight));
      if (top !== panel.scrollTop) panel.scrollTo({ top, behavior: smooth ? "smooth" : "auto" });
    };
    panel.addEventListener("nav-focus", (e) => follow(e.target.closest(".menu-item"), true));
    requestAnimationFrame(() => follow(panel.querySelector(".menu-item.is-focus"), false));
    if (opts.onOpen) opts.onOpen(layer);
  });
}
