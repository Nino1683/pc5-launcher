// Bewegter Hintergrund für Home und Spielseite (Bauplan E33).
//   ohne Spielbild  drei große, weiche Lichtflächen, die langsam umeinander fließen
//   mit art.hero    das Bild in langsamer Fahrt (leichter Zoom und Verschiebung, 45 s hin, 45 s
//                   zurück); beim Wechsel blenden zwei Ebenen weich über, aber erst, wenn der
//                   Fokus rund 300 ms ruht (schnelles Durchblättern lädt und flackert nichts)
// Bewegt wird ausschließlich transform, überblendet mit opacity, auf fünf großen Ebenen; kein
// filter, kein <video>. Die Bewegung treibt ein Zeitgeber mit fester, niedriger Bildrate statt
// einer CSS-Animation: So zeichnet der WebView nur so oft, wie es für diese langsame Fahrt nötig
// ist, auch in 3840x2160, und er steht ganz, wenn das Home nicht zu sehen ist
// (store.visible, auch unter dem Bildschirmschoner), bei „Bewegung reduzieren“, bei
// ui.liveBackground = false und solange der Bildschirm verdeckt ist.
import { h, cssMs, decoded } from "../core/dom.js";
import { store } from "../core/store.js";

const FPS = 20;                   // reicht: die Fahrt bewegt sich um Bruchteile eines Pixels je Bild
const TRIP = 90;                  // Sekunden für hin und zurück
// Bahnen der drei Lichtflächen: Mitte (in % der Bildbreite/-höhe), Ausschlag, Umlaufzeiten in s
const BLOBS = [
  { cls: "is-a", x: 70, y: 34, ax: 16, ay: 12, tx: 67, ty: 83, s: 0.14, ts: 59 },
  { cls: "is-b", x: 24, y: 12, ax: 14, ay: 10, tx: 79, ty: 61, s: 0.12, ts: 71 },
  { cls: "is-c", x: 46, y: 88, ax: 20, ay: 9, tx: 97, ty: 73, s: 0.16, ts: 53 },
];
const TAU = Math.PI * 2;

export function createBackdrop() {
  const blobs = BLOBS.map((b) => h("div.bd-blob." + b.cls));
  const flow = h("div.bd-flow", blobs);
  // Drei Bild-Ebenen: Das neue Bild blendet immer oben über dem stehenden alten ein, nie beide
  // gegeneinander (sonst scheint in der Mitte der dunkle Grund durch). Die dritte Ebene fängt
  // einen Wechsel auf, während die vorige Überblendung noch läuft.
  // Reihenfolge im Dokument = Stapel: Die neue Ebene wird vor die Abdunklung gehängt und liegt damit oben.
  const layers = [0, 1, 2].map(() => ({ el: h("div.bd-layer"), vis: false, born: 0, done: null }));
  const dim = h("div.bd-dim");
  const el = h("div.backdrop", flow, layers.map((l) => l.el), dim);
  let top = null;                 // oberste Ebene mit Bild, null = keins (Lichtflächen)
  let covered = false;            // die oberste Ebene ist ganz eingeblendet: Lichtflächen verdeckt
  let key = "";                   // Adresse des verlangten Bildes
  let shown = "";                 // Adresse des gezeigten Bildes
  let restTimer = null;
  let ticker = null;
  let clock = 0;                  // Sekunden Bewegung (steht, solange nichts gezeichnet wird)
  let last = 0;
  let active = true;              // false, solange ein anderer Bildschirm darüber liegt

  function live() {
    const ui = store.state.ui || {};
    return active && store.visible && !ui.reduceMotion && ui.liveBackground !== false
      && el.isConnected && !el.closest("[hidden]");
  }

  // Ein Schritt der Bewegung. Rechnet aus der gelaufenen Zeit, nicht aus der Zahl der Schritte:
  // Ein verspäteter Zeitgeber ändert das Tempo nicht.
  function tick() {
    const now = performance.now();
    clock += Math.min(0.25, (now - last) / 1000);
    last = now;
    paint();
  }

  function paint() {
    if (!covered) {
      // Lichtflächen sind zu sehen (allein oder während eines Übergangs)
      BLOBS.forEach((b, i) => {
        const x = b.x + b.ax * Math.sin(TAU * clock / b.tx + i * 2.1);
        const y = b.y + b.ay * Math.cos(TAU * clock / b.ty + i * 1.3);
        const s = 1 + b.s * Math.sin(TAU * clock / b.ts + i);
        blobs[i].style.transform = "translate3d(" + x.toFixed(3) + "vw," + y.toFixed(3) + "vh,0) scale(" + s.toFixed(4) + ")";
      });
    }
    layers.forEach((layer, i) => {
      const img = layer.el.firstChild;
      if (!img || !layer.vis) return;
      // 0 → 1 → 0 über TRIP Sekunden, weich an den Wendepunkten
      const p = (1 - Math.cos(TAU * (clock - layer.born) / TRIP)) / 2;
      const dir = i % 2 ? -1 : 1; // benachbarte Ebenen fahren gegenläufig: Der Wechsel wirkt nicht wie ein Sprung
      img.style.transform = "scale(" + (1.03 + 0.05 * p).toFixed(5) + ") translate3d(" + (dir * -1.2 * p).toFixed(4) + "%," + (-0.8 * p).toFixed(4) + "%,0)";
    });
  }

  function sync() {
    const on = live();
    el.classList.toggle("is-still", !on);
    if (on && !ticker) { last = performance.now(); ticker = setInterval(tick, 1000 / FPS); }
    else if (!on && ticker) { clearInterval(ticker); ticker = null; }
  }

  // Ebene sofort ausblenden, ohne Übergang (nur wenn sie verdeckt ist oder ohnehin ganz durchsichtig)
  function cut(layer) {
    clearTimeout(layer.done);
    layer.el.classList.add("is-cut");
    layer.el.classList.remove("is-on");
    void layer.el.offsetWidth;
    layer.el.replaceChildren();
    layer.el.classList.remove("is-cut");
    layer.vis = false;
  }

  function setCovered(on) {
    covered = on;
    flow.classList.toggle("is-off", on);
    if (!on) paint();
  }

  function clear() {
    top = null;
    shown = "";
    el.classList.remove("has-art");
    setCovered(false);                             // Lichtflächen sofort darunter, die Bilder blenden über ihnen aus
    for (const layer of layers) {
      if (!layer.vis) continue;
      clearTimeout(layer.done);
      layer.el.classList.remove("is-on");
      layer.done = setTimeout(() => cut(layer), cssMs("--t-bd-fade") + 50);
    }
  }

  async function swap(src) {
    // Eine freie Ebene; sind alle drei zu sehen (mehrere Wechsel kurz hintereinander), die unterste
    const free = layers.find((l) => !l.vis && l !== top)
      || layers.find((l) => l.el === el.querySelector(":scope > .bd-layer"));
    const img = h("img", { alt: "", decoding: "async" });
    img.src = src;
    const ok = await decoded(img);
    if (key !== src) return;                       // inzwischen ist ein anderer Eintrag gewählt
    if (!ok) { clear(); return; }                  // Bild fehlt: ruhiger Standardhintergrund
    cut(free);
    el.insertBefore(free.el, dim);
    free.el.replaceChildren(img);
    free.born = clock;
    free.vis = true;
    top = free;
    shown = src;
    covered = false;                               // bis die neue Ebene ganz da ist, nichts darunter wegnehmen
    paint();
    void free.el.offsetWidth;
    free.el.classList.add("is-on");
    el.classList.add("has-art");
    free.done = setTimeout(() => {
      if (top !== free) return;
      // Ganz eingeblendet: Darunter liegt nichts mehr Sichtbares; ältere Ebenen und Lichtflächen weg
      for (const layer of layers) if (layer !== free && layer.vis) cut(layer);
      setCovered(true);
    }, cssMs("--t-bd-fade") + 50);
  }

  // Zeigt den Hintergrund eines Eintrags (art.hero) oder, ohne Bild, die Lichtflächen.
  // instant: ohne die Ruhezeit (erster Aufbau, eigener Bildschirm eines Eintrags)
  function set(entry, { instant = false } = {}) {
    const src = (entry && entry.art && entry.art.hero) || "";
    if (src === key) return;
    key = src;
    clearTimeout(restTimer);
    restTimer = setTimeout(() => {
      if (key !== src || src === shown) return;
      if (src) swap(src); else clear();
    }, instant ? 0 : cssMs("--t-bd-rest"));
  }

  // foreground: Mit einer App vorn steht die Bewegung, auch wenn das Control Center darüber offen ist;
  // saver: unter dem Bildschirmschoner ebenso (store.visible, E99)
  const unsub = store.subscribe(["visible", "ui", "foreground", "saver"], sync);
  paint();
  // Der Bildschirm hängt erst nach create() im Dokument
  queueMicrotask(sync);
  setTimeout(sync, 0);

  return {
    el, set, sync,
    // Der Bildschirm sagt, ob er oben liegt (onShow/onHide): darunter bewegt sich nichts
    setActive(on) { active = !!on; sync(); },
    get moving() { return !!ticker; },
    destroy() { unsub(); clearInterval(ticker); clearTimeout(restTimer); ticker = null; },
  };
}
