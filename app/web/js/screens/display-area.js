// „Anzeigebereich prüfen“ (Einstellungen › Bildschirm und Video, Ergebnis {ui: "display-area"}):
// vier Marken genau in den Ecken des Bildes und ein Rahmen am Bildrand. Sind sie nicht ganz zu
// sehen, schneidet der Fernseher das Bild ab (Overscan). Einzustellen gibt es hier nichts: Der
// Kern hat dafür keinen Wert, die Abhilfe liegt am Fernseher.
import { h } from "../core/dom.js";
import { nav } from "../core/nav.js";
import { router } from "../core/router.js";
import { S, fmt } from "../core/strings.js";

function create() {
  const done = h("div.btn.area-done", { "data-focus": true, "data-focus-default": true }, h("span.btn-label", { text: S.areaDone }));
  done.addEventListener("press", () => router.back());
  const size = h("div.area-size");
  const el = h("div.area",
    h("div.area-frame"),
    ["tl", "tr", "bl", "br"].map((corner) => h("div.area-mark.is-" + corner)),
    h("div.area-box", { "data-bounds": true },
      h("div.area-title", { text: S.areaTitle }),
      h("div.area-text", { text: S.areaText }),
      h("div.area-text.is-second", { text: S.areaHelp }),
      size,
      done));

  function paintSize() {
    // echte Bildpunkte, wie Gamescope sie an den Fernseher gibt
    const ratio = window.devicePixelRatio || 1;
    size.textContent = fmt(S.areaSize, { w: Math.round(innerWidth * ratio), h: Math.round(innerHeight * ratio) });
  }
  window.addEventListener("resize", paintSize);
  paintSize();

  return {
    el,
    onShow() { nav.ensure(); },
    destroy() { window.removeEventListener("resize", paintSize); },
  };
}

export default { id: "display-area", create };
