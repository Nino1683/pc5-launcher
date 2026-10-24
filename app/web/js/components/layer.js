// Schicht über dem Bildschirm: Abdunklung + Tafel + eigener Fokus-Bereich.
// Grundlage für Menü, Auswahlliste und Dialog (Übergänge nach 07 Abschnitt 7).
import { h, after, enter } from "../core/dom.js";
import { nav } from "../core/nav.js";

const open = new Set();

// Wechselt der Bildschirm darunter (z. B. durch ein Ereignis aus Python), schließen alle Schichten;
// außer einer festen (sticky), die nur ihr Besitzer schließt (Hinweis „wird ausgeschaltet“)
document.addEventListener("couch-screen-change", () => { for (const api of [...open].reverse()) if (!api.sticky) api.close("external"); });

export function layerHost() {
  return document.getElementById("layers") || document.body;
}

// opts: panel (Element), cls, initial, onPad(button, ev) -> bool, onBack(), onClose(reason), sticky
// reason ist "external", wenn der Bildschirm darunter gewechselt hat
export function openLayer(opts) {
  const dim = h("div.layer-dim");
  const el = h("div.layer", { class: opts.cls || "" }, dim, opts.panel);
  opts.panel.classList.add("layer-panel");
  layerHost().append(el);
  enter(el);
  let closed = false;
  const api = {
    el,
    scope: null,
    sticky: !!opts.sticky,
    close(reason) {
      if (closed) return;
      closed = true;
      open.delete(api);
      nav.popScope(api.scope);
      if (opts.onClose) opts.onClose(reason);
      el.classList.add("is-leave");
      el.inert = true;
      after("--t-menu-out", () => el.remove());
    },
    get closed() { return closed; },
  };
  open.add(api);
  api.scope = nav.pushScope(el, {
    name: opts.name || "layer",
    initial: opts.initial,
    onPad: opts.onPad,
    onBack: () => (opts.onBack ? opts.onBack() : api.close()),
  });
  return api;
}
