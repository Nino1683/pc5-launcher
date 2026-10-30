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
//
// Eigener Hintergrund (E121, nur mit custom: das Home, nicht die Spielseite): Einstellungen › Home-Bildschirm
// › Hintergrund = „Eigenes Bild“ oder „Diashow“ (sys.state.background {mode, interval, slides, key}). Dann gilt
// dieses Bild immer, auch wenn ein Spiel fokussiert ist; set() merkt sich das Spielbild nur für später. Das
// Bild holt die Seite über gallery.backdrop: eine verkleinerte JPEG-Fassung aus dem Zwischenspeicher des Kerns
// (höchstens 2560x1440, ein 4K-PNG kostete bei jedem Wechsel das Dekodieren), über dieselben Ebenen und
// dieselbe Fahrt. Die Diashow wechselt nach interval Sekunden, gezählt wird nur, solange der Hintergrund zu
// sehen ist; sie blendet langsamer über (--t-bd-slide). Gefragt wird nichts, solange das Home nicht zu sehen
// ist oder ein anderer Bildschirm darüber liegt. Ohne Bild (gelöscht, kaputt, Teil fehlt) gelten die Spielbilder.
//
// Vorrat (E129): Die Kachelreihe nennt die Bilder ihrer Nachbarn (±2, preloadBackdrops); sie werden vorab geladen und
// dekodiert (Image.decode), damit der Wechsel nach der Ruhezeit nicht noch aufs Dekodieren wartet. Fester kleiner
// Vorrat (POOL_MAX Bilder; ein 4K-Bild kostet dekodiert rund 33 MB), eins nach dem anderen und erst nach der Ruhezeit
// (schnelles Blättern fragt nichts), nur solange das Home oben liegt und zu sehen ist; ist es verdeckt, wird der Vorrat
// freigegeben. Nicht bei „Bewegung reduzieren“ und nicht mit eigenem Hintergrund im Home (E121: dort steht kein
// Spielbild). Ein vorab dekodiertes Bild wandert als dasselbe Element in die Ebene, und wenn es die Ebene wieder
// verlässt, als Nachbar zurück in den Vorrat. Prüfhilfe window.__couchBackdrop (Zähler, Wartezeiten je Wechsel).
import { h, cssMs, decoded } from "../core/dom.js";
import { call, params } from "../core/bridge.js";
import { router } from "../core/router.js";
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
const OWN_MODES = ["image", "slideshow"];
const MIN_SLIDE_S = 2;            // kürzester Bildwechsel (der Kern bietet ab 30 s an; die Attrappe prüft mit 2 s)

// ---------------------------------------------------------------- Vorrat der Nachbarbilder (E129)
const POOL_MAX = 4;               // Nachbarn ±1 und ±2
const POOL_AFTER_MS = 150;        // nach der Ruhezeit des Hintergrunds: erst das eigene Bild, dann die Nachbarn
const POOL_OFF = params.get("bdpool") === "0";   // Prüfschalter: ohne Vorrat (Messung vorher/nachher)
const pool = new Map();           // Adresse -> {img, ok}
const onLayer = new Set();        // Adressen, die gerade eine Ebene des Home zeigt (kommen beim Abräumen zurück)
let poolWant = [];                // Nachbarn, näheste zuerst
let poolKeep = "";                // Bild des fokussierten Eintrags: bleibt, bis der Hintergrund es übernimmt
let poolTimer = null;
let poolRunning = false;
const poolStats = { loaded: 0, failed: 0, hits: 0, misses: 0, returned: 0, dropped: 0, freed: 0, decodeMs: [], swaps: [] };
window.__couchBackdrop = { stats: poolStats, get pool() { return [...pool.keys()]; }, get want() { return [...poolWant]; } };

function poolAllowed() {
  const bg = store.state.background;
  const own = bg && typeof bg === "object" && OWN_MODES.includes(bg.mode);
  return !POOL_OFF && store.visible && !store.ui.reduceMotion && !own && !!router.top && router.top.id === "home";
}

function poolDrop(src) {
  if (pool.delete(src)) poolStats.dropped += 1;
}

// Nachbarn der Fokuskachel (Adressen von art.hero, näheste zuerst) und das Bild der Fokuskachel selbst (keep)
export function preloadBackdrops(near, keep = "") {
  poolWant = [...new Set((near || []).filter((s) => typeof s === "string" && s && s !== keep))].slice(0, POOL_MAX);
  poolKeep = typeof keep === "string" ? keep : "";
  for (const src of [...pool.keys()]) if (!poolWant.includes(src) && src !== poolKeep) poolDrop(src);
  clearTimeout(poolTimer);
  poolTimer = null;
  if (poolWant.length && poolAllowed()) poolTimer = setTimeout(pump, cssMs("--t-bd-rest") + POOL_AFTER_MS);
}

// Eins nach dem anderen laden und dekodieren (nie zwei große Bilder zugleich)
async function pump() {
  poolTimer = null;
  if (poolRunning) return;
  poolRunning = true;
  try {
    for (;;) {
      if (!poolAllowed()) break;
      // Was eine Ebene noch zeigt, kommt beim Abräumen zurück (givePooled): nicht ein zweites Mal dekodieren
      const src = poolWant.find((s) => !pool.has(s) && !onLayer.has(s));
      if (!src) break;
      const img = h("img", { alt: "", decoding: "async" });
      img.dataset.key = src;
      img.src = src;
      const item = { img, ok: false };
      pool.set(src, item);
      const t0 = performance.now();
      item.ok = await decoded(img);
      if (item.ok) { poolStats.loaded += 1; poolStats.decodeMs.push(Math.round(performance.now() - t0)); }
      else poolStats.failed += 1;
      if (poolStats.decodeMs.length > 40) poolStats.decodeMs.shift();
      if (pool.get(src) === item && !poolWant.includes(src) && src !== poolKeep) poolDrop(src);
    }
  } finally {
    poolRunning = false;
  }
}

// Fertig dekodiertes Bild für den Hintergrund (es verlässt dabei den Vorrat) oder null
function takePooled(src) {
  const item = pool.get(src);
  if (!item || !item.ok) return null;
  pool.delete(src);
  return item.img;
}

// Ein Bild verlässt seine Ebene: Ist es ein Nachbar, kommt es ohne neues Dekodieren zurück in den Vorrat
function givePooled(img) {
  const src = img && img.dataset ? img.dataset.key : "";
  if (!src || !poolWant.includes(src) || pool.has(src) || pool.size >= POOL_MAX || !poolAllowed()) return;
  img.style.transform = "";
  pool.set(src, { img, ok: true });
  poolStats.returned += 1;
}

// Verdeckt oder unsichtbar: den Vorrat freigeben (mit einem Spiel vorn soll das Home keine Bilder halten). Dieselben
// Schlüssel wie store.visible: Unter dem Bildschirmschoner bleibt visible true, ohne saver blieben die Bilder dort belegt
store.subscribe(["visible", "ui", "background", "foreground", "saver"], () => {
  if (poolAllowed() || !pool.size) return;
  poolStats.freed += pool.size;
  pool.clear();
  clearTimeout(poolTimer);
  poolTimer = null;
});

export function createBackdrop({ custom = false } = {}) {
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
  let entrySrc = "";              // Hintergrund des fokussierten Eintrags (gilt ohne eigenen Hintergrund)
  // Eigener Hintergrund (E121): gezeigtes Bild (url, key), Anfrage unterwegs, noch zu fragen (dirty),
  // Diashow: fällig um due (performance.now), beim Verdecken übrige Zeit in left
  const own = { url: "", key: "", pending: false, dirty: true, serial: 0, applied: "", interval: 60,
    timer: null, due: 0, left: -1 };

  function live() {
    const ui = store.state.ui || {};
    return seen() && !ui.reduceMotion && ui.liveBackground !== false;
  }

  // Zu sehen (ohne die Einstellungen der Bewegung): Davon hängen Anfragen und die Diashow ab
  function seen() {
    return active && store.visible && el.isConnected && !el.closest("[hidden]");
  }

  function background() {
    const b = custom ? store.state.background : null;
    return b && typeof b === "object" ? b : {};
  }
  const ownMode = () => (OWN_MODES.includes(background().mode) ? background().mode : "");

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
    if (custom) syncOwn();
  }

  // Dauer der Überblendung einer Ebene (Diashow langsamer, E121)
  const fadeOf = (layer) => cssMs(layer.el.classList.contains("is-slow") ? "--t-bd-slide" : "--t-bd-fade");

  // Ebene sofort ausblenden, ohne Übergang (nur wenn sie verdeckt ist oder ohnehin ganz durchsichtig)
  function cut(layer) {
    clearTimeout(layer.done);
    layer.el.classList.add("is-cut");
    layer.el.classList.remove("is-on", "is-slow");
    void layer.el.offsetWidth;
    const old = layer.el.firstChild;
    layer.el.replaceChildren();
    if (old && custom) {                           // E129: Nachbar zurück in den Vorrat
      onLayer.delete(old.dataset.key);
      givePooled(old);
    }
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
      layer.done = setTimeout(() => cut(layer), fadeOf(layer) + 50);
    }
  }

  async function swap(src, slow = false) {
    // Eine freie Ebene; sind alle drei zu sehen (mehrere Wechsel kurz hintereinander), die unterste
    const free = layers.find((l) => !l.vis && l !== top)
      || layers.find((l) => l.el === el.querySelector(":scope > .bd-layer"));
    // E129: vorab dekodiert aus dem Vorrat (nur im Home), sonst jetzt laden
    const t0 = performance.now();
    const pooled = custom ? takePooled(src) : null;
    const img = pooled || h("img", { alt: "", decoding: "async" });
    if (!pooled) { img.dataset.key = src; img.src = src; }
    const ok = await decoded(img);
    if (custom) {
      poolStats[pooled ? "hits" : "misses"] += 1;
      poolStats.swaps.push({ pooled: !!pooled, waitMs: Math.round(performance.now() - t0) });
      if (poolStats.swaps.length > 40) poolStats.swaps.shift();
    }
    if (key !== src) { if (pooled) givePooled(img); return; }    // inzwischen ist ein anderer Eintrag gewählt
    if (!ok) { missing(src); return; }             // Bild fehlt: ruhiger Standardhintergrund
    cut(free);
    if (custom) onLayer.add(src);
    el.insertBefore(free.el, dim);
    free.el.classList.toggle("is-slow", !!slow);
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
    }, fadeOf(free) + 50);
  }

  // Ein Bild ließ sich nicht laden: beim eigenen Hintergrund gelten die Spielbilder, sonst die Lichtflächen
  function missing(src) {
    if (custom && src === own.url) {
      own.url = "";
      own.key = "";
      key = "";
      want(entrySrc, true);
      return;
    }
    clear();
  }

  // Bild verlangen (leer = Lichtflächen). instant: ohne die Ruhezeit
  function want(src, instant, slow = false) {
    if (src === key) return;
    key = src;
    clearTimeout(restTimer);
    restTimer = setTimeout(() => {
      if (key !== src || src === shown) return;
      if (src) swap(src, slow); else clear();
    }, instant ? 0 : cssMs("--t-bd-rest"));
  }

  // Zeigt den Hintergrund eines Eintrags (art.hero) oder, ohne Bild, die Lichtflächen.
  // instant: ohne die Ruhezeit (erster Aufbau, eigener Bildschirm eines Eintrags)
  // Mit eigenem Hintergrund (E121) bleibt das eigene Bild stehen; das Spielbild gilt, sobald es wegfällt.
  function set(entry, { instant = false } = {}) {
    entrySrc = (entry && entry.art && entry.art.hero) || "";
    if (custom && ownMode() && (own.url || own.pending || own.dirty)) return;
    want(entrySrc, instant);
  }

  // ---------------------------------------------------------------- eigener Hintergrund (E121)
  // Einstellungen, die ein neues Bild verlangen; das Intervall allein plant nur neu
  const signature = (b) => [b.mode || "", b.key || "", b.slides || ""].join("|");

  function onBackground() {
    const b = background();
    own.interval = Math.max(MIN_SLIDE_S, Number(b.interval) || 60);
    const sig = signature(b);
    if (sig !== own.applied) {
      own.applied = sig;
      own.dirty = true;
      own.left = -1;
    }
    syncOwn();
  }

  // Bei jeder Änderung der Sichtbarkeit: fällige Anfrage stellen, Diashow anhalten bzw. weiterlaufen lassen
  function syncOwn() {
    const mode = ownMode();
    if (!mode) {
      clearTimeout(own.timer);
      own.timer = null;
      own.serial += 1;                             // eine Antwort unterwegs gilt nicht mehr
      const had = own.url || own.pending;
      own.url = "";
      own.key = "";
      own.pending = false;
      own.dirty = false;
      if (had || key !== entrySrc) want(entrySrc, true);
      return;
    }
    if (!seen()) { pause(); return; }
    if (own.dirty) { fetchOwn(false); return; }
    plan();
  }

  async function fetchOwn(next) {
    clearTimeout(own.timer);
    own.timer = null;
    own.dirty = false;
    own.pending = true;
    const mine = ++own.serial;
    const mode = ownMode();
    let r = null;
    try { r = await call("gallery.backdrop", { after: next ? own.key : "" }); } catch (e) { r = null; }
    if (mine !== own.serial) return;
    own.pending = false;
    if (!ownMode()) return;
    own.url = (r && typeof r.url === "string" && r.url) || "";
    own.key = (r && r.key) || "";
    if (r && Number(r.interval) > 0) own.interval = Math.max(MIN_SLIDE_S, Number(r.interval));
    own.due = performance.now() + own.interval * 1000;
    own.left = -1;
    if (own.url) want(own.url, true, mode === "slideshow" && next);
    else want(entrySrc, true);
    plan();
  }

  // Nächstes Bild der Diashow einplanen (nur sichtbar; die verdeckte Zeit zählt nicht)
  function plan() {
    clearTimeout(own.timer);
    own.timer = null;
    if (ownMode() !== "slideshow" || !own.url || own.pending || !seen()) return;
    if (own.left >= 0) { own.due = performance.now() + own.left; own.left = -1; }
    own.timer = setTimeout(() => { own.timer = null; if (seen()) fetchOwn(true); }, Math.max(0, own.due - performance.now()));
  }

  function pause() {
    if (own.timer) {
      clearTimeout(own.timer);
      own.timer = null;
      own.left = Math.max(0, own.due - performance.now());
    }
  }

  // foreground: Mit einer App vorn steht die Bewegung, auch wenn das Control Center darüber offen ist;
  // saver: unter dem Bildschirmschoner ebenso (store.visible, E99)
  const unsub = store.subscribe(["visible", "ui", "foreground", "saver"], sync);
  const unsubOwn = custom ? store.subscribe(["background"], onBackground) : () => {};
  if (custom) { own.applied = signature(background()); own.interval = Math.max(MIN_SLIDE_S, Number(background().interval) || 60); }
  own.dirty = custom && !!ownMode();
  paint();
  // Der Bildschirm hängt erst nach create() im Dokument
  queueMicrotask(sync);
  setTimeout(sync, 0);

  return {
    el, set, sync,
    // Der Bildschirm sagt, ob er oben liegt (onShow/onHide): darunter bewegt sich nichts
    setActive(on) { active = !!on; sync(); },
    get moving() { return !!ticker; },
    // Prüfhilfe (E121): was gerade gilt
    get own() { return { mode: ownMode(), url: own.url, key: own.key, pending: own.pending, timer: !!own.timer }; },
    destroy() {
      unsub(); unsubOwn(); clearInterval(ticker); clearTimeout(restTimer); clearTimeout(own.timer);
      ticker = null; own.serial += 1;
    },
  };
}
