// Startanimation „Polarlicht“ (E103, Wunsch 2026-10-24: „Mach die Startanimation aber viel schöner und
// modern.“; ersetzt Bild und Klang von E92, Auslösung und Zeiten bleiben). Läuft nur, wenn der Kern
// intro=1 an die Adresse hängt (einmal je Sitzung, couch/intro.py) und die Einstellung ui.intro an ist.
// Ablauf (Storyboard docs/research/e103/SPEC.md 6.2): schwarz, bis Klang und Schrift bereit sind
// (höchstens --t-intro-sound-wait), dann Bild und Ton zugleich. Aus dem Schwarz zieht eine Lichtnaht,
// öffnet sich als Vorhang, dahinter fließt das Polarlicht (components/intro-sky.js); „P“, „C“, „5“ laufen
// mit kurzer Spur zusammen und werden kräftiger, ein Glanz läuft darüber, „LAUNCHER“ zieht sich aus weiter
// Sperrung zusammen. Ins Home frühestens nach --t-intro-hold und erst, wenn das Home seine Reihen hat
// (Ereignis couch-home-ready), spätestens nach --t-intro-max. Jede Taste und PS überspringen
// (--t-intro-skip), der Ton geht dabei in --t-intro-sound-fade aus.
// Bewegt wird nur über einen JS-Zeitplan, der je Bild die registrierten Werte setzt (--fx-o/x/y/s,
// --in-sweep, --in-ls, --in-w): keine CSS-Übergänge, keine Keyframes, E52 kann hier nicht auftreten.
// Prüfschalter (nur aus der Adresse und nur zusammen mit intro=1): introAt=<ms> (Standbild, kein Lauf),
// introOut=<ms> (Ausgang zu diesem Stand), introMode=css, introScale=<0..1>.
// window.__couchIntro = {mode, scale, at, frames, …} für Prüfungen.
import { params } from "../core/bridge.js";
import { h, cssMs, cssPx } from "../core/dom.js";
import { sound } from "../core/sound.js";
import { store } from "../core/store.js";
import { createGuard, createSky } from "./intro-sky.js";

// Zeitplan in ms ab Bildbeginn (SPEC 6.2). Zeiten, die Kern und Prüfung kennen (Halten, Höchstdauer,
// Ausgang, Überspringen, Ruhefassung), stehen in tokens.css.
const T = {
  seamIn: [0, 650], seamShow: [0, 250], seamOut: [850, 1300], seamGlowIn: [80, 600], seamGlowOut: [850, 1600],
  seamGlowGrow: [0, 1400], curtain: [250, 1550], skyIn: [250, 1300], zoom: [0, 3400], calmFlow: [1800, 3200],
  letters: [620, 1520], letterStep: 70, weight: [620, 1700], sweep: [1450, 2350], sub: [1500, 2400],
  halo: [900, 1700], haloSettle: [1700, 3000], hide: 1550,
  calmSky: [0, 600], calmLetters: [200, 800], calmSub: [400, 1000],
};
const ZOOM = [1.14, 1.0];             // Kamera fährt zurück (Parallaxe der drei Bänder)
const ZOOM_OUT = 0.06;                // Ausgang: die Bänder fahren nach vorn
const PAN = 0.22;                     // seitliche Fahrt, klingt mit dem Zoom aus
const FLOW0 = 11;                     // Fluss-Zeit des Polarlichts beim ersten Bild (s)
const FLOW_CALM = 0.35;               // Tempo des Flusses am Ende (1 = anfangs)
const HALO_SETTLE = 0.65;             // Licht hinter dem Schriftzug: 0 → 1 → 0,65
const HALO_O = 0.85;
const FROM = [[-0.62, 0], [0, 0.16], [0.62, 0]];   // Start der Ziffern in em: P von links, C steigt auf, 5 von rechts
const TRAILS = [0.22, 0.16, 0.11, 0.07, 0.04];       // Deckkraft der Spuren, je 8 ms weiter zurück
const TRAIL_LAG = 8;
const TRAIL_SPEED = 0.06;             // ab dieser Strecke je Spurschritt (in em) volle Spur
const MARK_FADE = 0.7;                // Ausgang: Schriftzug ist bei 70 % des Ausgangs weg
const GUARD = { n: 12, slow: 20, scale: 0.35 };   // Bildzeit-Wache: Median von 12 Abständen über 20 ms

let current = null;

export function introWanted() {
  return params.get("intro") === "1" && (store.state.ui || {}).intro !== false;
}

export function introActive() {
  return !!current && !current.leaving;
}

// Ein Tastendruck: schnell ins Home
export function skipIntro() {
  if (current) current.leave(true);
}

// ------------------------------------------------------------------ Hilfen
const clamp = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
const seg = (t, r) => clamp((t - r[0]) / (r[1] - r[0]));
const lerp = (a, b, k) => a + (b - a) * k;
// Kurven wie in tokens.css (--ease-out, --ease-in, --ease-std)
function bezier(x1, y1, x2, y2) {
  const cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx;
  const cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by;
  const X = (u) => ((ax * u + bx) * u + cx) * u;
  const Y = (u) => ((ay * u + by) * u + cy) * u;
  const dX = (u) => (3 * ax * u + 2 * bx) * u + cx;
  return (x) => {
    if (x <= 0) return 0;
    if (x >= 1) return 1;
    let u = x;
    for (let i = 0; i < 6; i++) { const d = dX(u); if (Math.abs(d) < 1e-6) break; u -= (X(u) - x) / d; }
    return Y(clamp(u));
  };
}
const easeOut = bezier(0.2, 0.8, 0.2, 1);
const easeIn = bezier(0.4, 0, 1, 1);
const easeStd = bezier(0.4, 0, 0.2, 1);
const expoOut = (x) => (x >= 1 ? 1 : 1 - Math.pow(2, -10 * x));
const num = (name) => parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name)) || 0;
const set = (el, name, value) => { if (el) el.style.setProperty(name, value); };

// Tempo des Flusses: anfangs lebhaft, dann ruhig (das Bild „setzt sich“); im Ausgang schneller
const flowSpeed = (t, o) => lerp(1, FLOW_CALM, easeStd(seg(t, T.calmFlow))) * (1 + o * 1.5);

// Prüfschalter aus der Adresse (introWanted() verlangt schon intro=1)
function testSwitches() {
  const number = (key) => (params.has(key) && params.get(key) !== "" && Number.isFinite(Number(params.get(key))) ? Number(params.get(key)) : null);
  return { at: number("introAt"), out: number("introOut"), css: params.get("introMode") === "css", scale: number("introScale") };
}

// homeReady: Promise, das erfüllt ist, sobald das Home seine Reihen hat
export function startIntro(homeReady) {
  if (current) return current.done;
  const [first, ...rest] = String(document.title || "").split(" ");
  const calm = !!(store.state.ui || {}).reduceMotion;
  const test = testSwitches();
  const glyphs = [...(first || "")];

  // ---------------------------------------------------------------- Aufbau
  const sky = createSky({ wantGL: !test.css, scale: test.scale });
  const halo = h("div.intro-halo");
  const curtainTop = calm ? null : h("div.intro-curtain.is-top");
  const curtainBottom = calm ? null : h("div.intro-curtain.is-bottom");
  const seamGlow = calm ? null : h("div.intro-seam-glow");
  const seam = calm ? null : h("div.intro-seam");
  const chars = glyphs.map((c) => h("span.intro-ch", { text: c }));
  // Spuren: dieselben Ziffern ein paar Millisekunden zurück, nur solange sie sich schnell bewegen
  const trails = calm ? [] : TRAILS.map(() => glyphs.map((c) => h("span", { text: c })));
  const shine = calm ? null : h("div.intro-shine", { text: first || "" });
  const word = h("div.intro-word",
    trails.map((row) => h("div.intro-trail", row)),
    h("div.intro-glyphs", chars),
    shine);
  const sub = h("div.intro-sub", { text: rest.join(" ").toUpperCase() });
  const mark = h("div.intro-mark", word, sub);
  const el = h("div.intro", { "aria-hidden": "true", class: calm ? "is-calm" : "" },
    sky.el, halo, curtainTop, curtainBottom, seamGlow, seam, mark);
  document.body.append(el);

  const info = {
    mode: sky.mode, scale: sky.scale, at: 0, frames: 0, calm, still: test.at !== null || test.out !== null,
    gl: sky.info, guard: [], reason: sky.reason, js: { avg: 0, max: 0 }, leaving: "", soundFadeMs: 0, markO: 1, markS: 1,
    digitsO: glyphs.map(() => 0), subO: 0, shineO: 0, shineX: 0, speed: 1,
  };
  window.__couchIntro = info;

  // Maße einmal lesen (je Bild getComputedStyle zu fragen, erzwänge jedes Mal eine Stilberechnung)
  const m = {};
  function measure() {
    m.em = cssPx("--fs-intro");
    m.feather = cssPx("--intro-feather");
    m.lsFrom = cssPx("--ls-intro-sub-from");
    m.ls = cssPx("--ls-intro-sub");
    m.subRise = cssPx("--intro-sub-rise");
    m.lift = cssPx("--intro-out-lift");
    m.outScale = num("--intro-out-scale") || 1;
    m.wFrom = num("--intro-wght-from");
    m.w = num("--intro-wght");
  }
  measure();

  // ---------------------------------------------------------------- ein Bild aus der Zeit t (ms) und dem Ausgang o (0…1)
  function letter(i, t) {
    const k = expoOut(seg(t, [T.letters[0] + i * T.letterStep, T.letters[1] + i * T.letterStep]));
    const from = FROM[i] || [0, 0];
    return { x: from[0] * m.em * (1 - k), y: from[1] * m.em * (1 - k), k };
  }

  let flow = FLOW0;
  let curtainsGone = false;
  function frame(t, o) {
    info.at = Math.round(t);
    if (calm) return frameCalm(t, o);
    const zoom = lerp(ZOOM[0], ZOOM[1], easeOut(seg(t, T.zoom))) - ZOOM_OUT * easeIn(o);
    const pan = PAN * (1 - easeOut(seg(t, T.zoom)));
    const glow = easeOut(seg(t, T.halo)) * lerp(1, HALO_SETTLE, easeStd(seg(t, T.haloSettle)));
    if (store.visible !== false) sky.draw({ t: flow, zoom, pan, fade: easeOut(seg(t, T.skyIn)), glow });
    set(halo, "--fx-o", (HALO_O * glow * (1 - o)).toFixed(4));
    set(halo, "--fx-s", (0.8 + 0.2 * glow).toFixed(4));
    // Lichtnaht und Vorhang; danach ganz weg (nichts mehr zu zeichnen)
    if (t < T.hide) {
      if (curtainsGone) { for (const e of [curtainTop, curtainBottom, seam, seamGlow]) e.hidden = false; curtainsGone = false; }
      set(seam, "--fx-s", easeOut(seg(t, T.seamIn)).toFixed(4));
      set(seam, "--fx-o", (seg(t, T.seamShow) * (1 - easeIn(seg(t, T.seamOut)))).toFixed(4));
      set(seamGlow, "--fx-o", (easeOut(seg(t, T.seamGlowIn)) * (1 - easeStd(seg(t, T.seamGlowOut)))).toFixed(4));
      set(seamGlow, "--fx-s", lerp(0.3, 1.1, easeOut(seg(t, T.seamGlowGrow))).toFixed(4));
      const open = easeStd(seg(t, T.curtain)) * (innerHeight / 2 + m.feather);
      set(curtainTop, "--fx-y", (-open).toFixed(2) + "px");
      set(curtainBottom, "--fx-y", open.toFixed(2) + "px");
    } else if (!curtainsGone) {
      for (const e of [curtainTop, curtainBottom, seam, seamGlow]) e.hidden = true;
      curtainsGone = true;
    }
    // Schriftzug: Ziffern laufen zusammen, Spuren, Strichstärke
    set(word, "--in-w", lerp(m.wFrom, m.w, easeOut(seg(t, T.weight))).toFixed(1));
    chars.forEach((c, i) => {
      const now = letter(i, t);
      info.digitsO[i] = +clamp(now.k * 2.2).toFixed(4);
      set(c, "--fx-x", now.x.toFixed(2) + "px");
      set(c, "--fx-y", now.y.toFixed(2) + "px");
      set(c, "--fx-o", String(info.digitsO[i]));
      trails.forEach((row, j) => {
        const then = letter(i, t - TRAIL_LAG * (j + 1));
        const moved = Math.hypot(now.x - then.x, now.y - then.y);
        set(row[i], "--fx-x", then.x.toFixed(2) + "px");
        set(row[i], "--fx-y", then.y.toFixed(2) + "px");
        // die Ziffer selbst blendet schon mit: die Spur nie heller als sie
        set(row[i], "--fx-o", (clamp(moved / (TRAIL_SPEED * m.em)) * TRAILS[j] * clamp(now.k * 2.2)).toFixed(4));
      });
    });
    // Glanz: Lage des hellen Streifens (Mitte des Verlaufs, 300 % breit) über dem Schriftzug, 0…1 = darauf
    const sweep = lerp(115, -15, easeStd(seg(t, T.sweep)));
    info.shineO = t > T.sweep[0] && t < T.sweep[1] ? 1 : 0;
    info.shineX = +(1.5 - 2 * sweep / 100).toFixed(3);
    set(shine, "--in-sweep", sweep.toFixed(2) + "%");
    set(shine, "--fx-o", String(info.shineO));
    const sb = easeOut(seg(t, T.sub));
    info.subO = +sb.toFixed(4);
    info.speed = +flowSpeed(t, o).toFixed(3);
    set(sub, "--fx-o", sb.toFixed(4));
    set(sub, "--in-ls", lerp(m.lsFrom, m.ls, expoOut(seg(t, T.sub))).toFixed(2) + "px");
    set(sub, "--fx-y", (lerp(m.subRise, 0, sb)).toFixed(2) + "px");
    exit(o);
  }

  // Ausgang: Schriftzug kommt näher, hebt sich und löst sich auf, das Ganze blendet ins Home
  function exit(o) {
    info.markS = 1 + (m.outScale - 1) * easeIn(o);
    info.markO = 1 - easeIn(clamp(o / MARK_FADE));
    set(mark, "--fx-s", info.markS.toFixed(4));
    set(mark, "--fx-o", info.markO.toFixed(4));
    set(mark, "--fx-y", (-m.lift * easeIn(o)).toFixed(2) + "px");
    set(el, "--fx-o", (1 - easeIn(o)).toFixed(4));
  }

  // „Bewegung reduzieren“: keine Naht, kein Vorhang, kein Zoom, keine Spuren, kein Glanz; das
  // Polarlicht ist ein festes Bild (immer dieselben Werte, nichts bewegt sich) und blendet nur ein
  function frameCalm(t, o) {
    if (store.visible !== false) sky.draw({ t: FLOW0, zoom: 1, pan: 0, fade: 1, glow: HALO_SETTLE });
    const k = easeOut(seg(t, T.calmSky));
    set(sky.el, "--fx-o", (k * (1 - o)).toFixed(4));
    set(halo, "--fx-o", (HALO_O * HALO_SETTLE * k * (1 - o)).toFixed(4));
    set(word, "--in-w", String(m.w));
    const kc = easeOut(seg(t, T.calmLetters));
    chars.forEach((c, i) => { info.digitsO[i] = +kc.toFixed(4); set(c, "--fx-o", kc.toFixed(4)); set(c, "--fx-x", "0px"); set(c, "--fx-y", "0px"); });
    info.subO = +easeOut(seg(t, T.calmSub)).toFixed(4);
    info.speed = 0;
    set(sub, "--fx-o", String(info.subO));
    set(sub, "--in-ls", m.ls.toFixed(2) + "px");
    set(sub, "--fx-y", "0px");
    info.markO = 1;
    set(el, "--fx-o", (1 - o).toFixed(4));
  }

  // Fluss-Zeit bis t (Standbild: in kleinen Schritten aufsummiert, wie sie im Lauf entstünde)
  function flowUntil(t, outMs, outDur) {
    let f = FLOW0;
    for (let x = 0; x < t; x += 4) f += flowSpeed(x, 0) * Math.min(4, t - x) / 1000;
    for (let x = 0; x < outMs; x += 4) f += flowSpeed(t, clamp(x / outDur)) * Math.min(4, outMs - x) / 1000;
    return f;
  }

  // ---------------------------------------------------------------- Ablauf
  let finish;
  const done = new Promise((resolve) => { finish = resolve; });
  const state = { leaving: false, done, leave: null };
  current = state;
  let audio = null;          // Griff des Klangs {stop(ms)}; stumm oder ohne Ausgabe: null
  let t0 = 0;
  let running = false;
  let leaving = null;        // {start, dur}
  let ready = false;
  let timer = null;

  // erstes Bild: alles schwarz (Vorhang zu, Polarlicht dunkel, Schriftzug unsichtbar)
  frame(0, 0);

  state.leave = (quick) => {
    if (state.leaving) return;
    state.leaving = true;
    clearTimeout(timer);
    const dur = cssMs(quick ? "--t-intro-skip" : (calm ? "--t-intro-calm-out" : "--t-intro-out"));
    leaving = { start: performance.now(), dur };
    info.leaving = quick ? "skip" : "out";
    // Überspringen und Ruhefassung: Ton in 250 ms aus; sonst klingt der Rest im Home aus
    if (quick || calm) {
      info.soundFadeMs = cssMs("--t-intro-sound-fade");
      if (audio) audio.stop(info.soundFadeMs);
    }
    // Taste schon im schwarzen Vorlauf: auch dann weich ausblenden
    if (!running && !info.still) {
      t0 = performance.now();
      running = true;
      requestAnimationFrame(loop);
    }
    setTimeout(() => {
      running = false;
      removeEventListener("resize", onResize);
      el.remove();
      if (current === state) current = null;
      finish();
    }, dur + 80);
  };
  let onResize = measure;

  // Standbild für Prüffotos: Zeit fest, kein Lauf, kein Ton
  if (info.still) {
    const at = Math.max(0, test.at === null ? cssMs("--t-intro-hold") : test.at);
    const outDur = cssMs("--t-intro-out");
    const outMs = test.out === null ? 0 : Math.max(0, test.out);
    const still = () => {
      measure();
      flow = flowUntil(at, outMs, outDur);
      frame(at, clamp(outMs / outDur));
      info.mode = sky.mode;
      info.scale = sky.scale;
    };
    still();
    onResize = () => { if (!state.leaving) still(); };
    addEventListener("resize", onResize);
    // Die WebGL-Fläche behält ihr Bild nicht (preserveDrawingBuffer: false): je Bild neu zeichnen
    const redraw = () => {
      if (state.leaving || !el.isConnected) return;
      if (store.visible !== false) sky.redraw();
      requestAnimationFrame(redraw);
    };
    requestAnimationFrame(redraw);
    return done;
  }

  addEventListener("resize", onResize);

  // Frühestens nach der Haltezeit, sobald das Home bereit ist; spätestens nach --t-intro-max
  // (über einen Zeitgeber: rAF bleibt aus, wenn die Seite gerade nicht gezeichnet wird)
  const check = () => {
    if (state.leaving) return;
    const age = performance.now() - t0;
    if ((ready && age >= cssMs(calm ? "--t-intro-calm" : "--t-intro-hold")) || age >= cssMs("--t-intro-max")) state.leave(false);
    else timer = setTimeout(check, 100);
  };
  Promise.resolve(homeReady).then(() => { ready = true; }, () => { ready = true; });

  // Bildzeit-Wache (components/intro-sky.js): zu langsam → kleinere Fläche, dann CSS
  const guard = createGuard(sky, GUARD);
  info.guard = guard.medians;

  let jsSum = 0;
  let lastJs = null;
  let lastTick = null;
  info.gaps = { max: 0, over25: 0 };     // Abstände der Bilder (rAF): größter, Zahl über 25 ms (Prüfung, Bericht)
  function loop(ts) {
    if (!running) return;
    const js0 = performance.now();
    if (store.visible === false) guard.reset(); else guard.tick(ts);
    if (lastTick !== null) {
      const gap = ts - lastTick;
      info.gaps.max = Math.max(info.gaps.max, +gap.toFixed(2));
      if (gap > 25) info.gaps.over25 += 1;
    }
    lastTick = ts;
    const t = js0 - t0;
    const o = leaving ? clamp((js0 - leaving.start) / leaving.dur) : 0;
    // Fluss-Zeit aus der gelaufenen Zeit (ein verspätetes Bild ändert das Tempo nicht; höchstens 100 ms je Schritt)
    flow += Math.min(100, Math.max(0, js0 - (lastJs === null ? js0 : lastJs))) / 1000 * flowSpeed(t, o);
    lastJs = js0;
    frame(t, o);
    info.frames += 1;
    info.mode = sky.mode;
    info.scale = sky.scale;
    info.reason = sky.reason;
    const js = performance.now() - js0;
    jsSum += js;
    info.js = { avg: +(jsSum / info.frames).toFixed(3), max: +Math.max(info.js.max, js).toFixed(2) };
    requestAnimationFrame(loop);
  }

  // Bild und Ton zusammen beginnen lassen: auf den dekodierten Klang und die Schrift warten, aber nicht lange
  const wait = new Promise((resolve) => setTimeout(resolve, cssMs("--t-intro-sound-wait")));
  const fonts = document.fonts && document.fonts.load ? document.fonts.load(m.w + " " + Math.round(m.em) + "px 'PC5 Intro'").catch(() => null) : null;
  Promise.race([Promise.all([sound.prepare("intro").catch(() => null), fonts]), wait]).then(() => {
    if (state.leaving) return;
    // zwei Bilder Abstand: Das schwarze Bild steht, bevor die Animation beginnt
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (state.leaving) return;
      measure();
      t0 = performance.now();
      running = true;
      const got = sound.play("intro");
      audio = got && typeof got === "object" ? got : null;
      check();
      requestAnimationFrame(loop);
    }));
  });
  return done;
}
