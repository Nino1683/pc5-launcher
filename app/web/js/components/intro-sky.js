// Polarlicht der Startanimation (E103, Wunsch 2026-10-24: „Mach die Startanimation aber viel schöner und
// modern.“). Drei Seidenbänder in drei Tiefen, in den Farben des Home (E33-Lichtflächen und Fokusring,
// satt und leuchtend; kein Konsolen-Blau):
//   fern   Indigo → Violett, weich (Tiefenunschärfe), im Dunst, mit feinen Strahlen nach oben
//   mitte  Petrol → Türkis
//   nah    Rosé → Pfirsich, scharf, kräftige Lichtkante
// Jedes Band hat Glanzfalten (schmale helle Linien längs, f^8), eine Lichtkante dort, wo es sich zur
// Kante dreht, und einen farbigen Hof. Die Kamera fährt zurück (Zoom) und seitlich; nahe Bänder wandern
// stärker mit (Parallaxe).
// Technik: eine WebGL-2-Fläche (Rückfall WebGL 1), ein Dreieck, ein Fragment-Shader, gerechnet in 50 %
// (ab 1200 px Bildhöhe) bzw. 60 % der Auflösung, ±0,5/255 Rauschen gegen Farbstufen. Ausnahme von
// ARCHITECTURE Abschnitt 5 für genau diese eine Fläche während der Startanimation (E103).
// Rückfall ohne WebGL, bei Kontextverlust oder zu langsamer Bildfolge: dieselbe Komposition aus weichen
// radialen Verläufen, die nur über --fx-x/y/s wandern (wie E33).
// Farben kommen aus tokens.css (Block E103-intro); die Zahlen des Shaders stehen hier, wie in backdrop.js.
import { h } from "../core/dom.js";

const VS = "attribute vec2 aPos; varying vec2 vUv; void main(){ vUv = aPos * 0.5 + 0.5; gl_Position = vec4(aPos, 0.0, 1.0); }";

// GLSL ES 1.00: läuft in WebGL 2 und WebGL 1
const FS = `precision highp float;
varying vec2 vUv;
uniform vec2 uRes;
uniform float uT; uniform float uZoom; uniform float uPan; uniform float uFade; uniform float uGlow;
uniform vec3 uTop; uniform vec3 uBot; uniform vec3 uFog;
uniform vec3 uFarA; uniform vec3 uFarB; uniform vec3 uMidA; uniform vec3 uMidB; uniform vec3 uNearA; uniform vec3 uNearB;
uniform vec3 uHi; uniform vec3 uWarm;

float hash(vec2 p){ vec3 p3 = fract(vec3(p.xyx) * 0.1031); p3 += dot(p3, p3.yzx + 33.33); return fract((p3.x + p3.y) * p3.z); }
float noise(vec2 p){ vec2 i = floor(p); vec2 f = fract(p); vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(hash(i), hash(i + vec2(1.0, 0.0)), u.x), mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), u.x), u.y); }
float fbm(vec2 p){ float v = 0.0; float a = 0.5; for (int i = 0; i < 3; i++){ v += a * noise(p); p = mat2(1.6, 1.2, -1.2, 1.6) * p; a *= 0.5; } return v; }

// Ein Seidenband. q: Lage in Bandkoordinaten, soft: Unschärfe (Tiefe), ca/cb: Farbe längs des Bandes.
// Die Fläche dreht sich längs des Bandes (Winkel th): zugewandt ist sie breit und matt, hochkant schmal
// mit heller Kante; ein Glanzstreifen wandert mit der Drehung quer darüber.
// Ergebnis: rgb = Licht des Bandes, a = Deckung (verdeckt, was dahinter liegt)
vec4 silk(vec2 q, float t, float y0, float tilt, float amp, float hw, float soft, float seed, vec3 ca, vec3 cb){
  float n = fbm(vec2(q.x * 0.40 + seed, t * 0.06 + seed * 1.3));
  float cy = y0 + tilt * q.x + amp * (0.75 * sin(q.x * 0.80 + t * 0.30 + seed) + 0.25 * sin(q.x * 1.9 - t * 0.43 + seed * 2.3)) + (n - 0.5) * 0.30;
  float th = q.x * 0.95 + t * 0.33 + seed * 0.7 + n * 1.4;
  float c = cos(th);
  float s = sin(th);
  float face = c * c;                            // glatt (|cos| hätte eine Spitze: Knick in der Kante)
  float w = hw * (0.30 + 0.70 * face);
  float d = (q.y - cy) / w;                      // quer: -1 unten … 1 oben
  float ad = abs(d);
  float body = 1.0 - smoothstep(1.0 - soft, 1.02 + soft * 0.6, ad);
  // Licht von oben vorn: die Seite, die sich zum Licht neigt, ist hell (Wölbung quer zum Band)
  float lit = clamp(0.5 + 0.5 * (s * 0.75 + d * 0.55), 0.0, 1.0);
  float ds = 0.75 * sin(th + 0.6);
  float sw = 0.16 + 0.30 * face + soft * 0.4;
  float spec = exp(-(d - ds) * (d - ds) / (sw * sw));
  // Glanzfalten: schmale Linien fast längs des Bandes (f^16), im Glanzstreifen am hellsten
  float f = 0.5 + 0.5 * sin(d * 9.0 + q.x * 1.6 + n * 5.0 - t * 0.5 + seed);
  float fold = f * f; fold *= fold; fold *= fold; fold *= fold;
  // Lichtkanten: oben kräftig und scharf, unten schwächer; am hellsten, wo das Band hochkant steht
  float kw = 0.035 + soft * 0.5;
  float rt = (d - 0.965) / kw;
  float rb = (d + 0.965) / kw;
  float rim = (exp(-rt * rt) + 0.40 * exp(-rb * rb)) * (0.60 + 0.40 * (1.0 - face));
  float tone = smoothstep(-1.6, 1.6, q.x + (n - 0.5) * 1.4);
  vec3 base = mix(ca, cb, tone);
  vec3 sheen = mix(base, uHi, 0.42);
  float crisp = 1.0 - soft * 0.85;
  // tiefe Schatten in den Falten, leuchtende Lichter: Seide statt Nebel. Der Schatten nimmt den tieferen
  // Ton des Bandes an (Pfirsich wird im Schatten Rosé, nicht Braun)
  vec3 deep = ca * 0.22;
  vec3 col = mix(deep, base * 0.92, lit * lit) * body + base * body * 0.14 * fold;
  col += sheen * body * spec * (0.55 + 0.55 * crisp) + sheen * body * fold * spec * 0.60 * crisp;
  col += mix(base, uHi, 0.62) * rim * crisp * 1.75;
  // farbiger Hof um das Band (Licht, das in den Dunst streut)
  float dist = max(ad - 1.0, 0.0) * w;
  col += base * exp(-dist * dist / (0.006 + soft * 0.04)) * 0.18;
  return vec4(col, body * 0.72);
}

void main(){
  vec2 uv = vUv * 2.0 - 1.0;
  vec2 p = vec2(uv.x * uRes.x / uRes.y, uv.y);
  // Grund: oben tiefes Indigo-Violett, unten fast schwarz
  vec3 col = mix(uBot, uTop, smoothstep(-1.0, 1.1, uv.y));
  // Dunst in der Ferne (langsame Wolken)
  float haze = fbm(p * 0.9 + vec2(uT * 0.02, 0.0));
  col += uFog * smoothstep(0.3, 0.9, haze) * smoothstep(-0.6, 1.0, uv.y) * 0.45;
  // drei Bänder von hinten nach vorn; z = 0 fern … 1 nah (nahe wandern stärker mit: Parallaxe)
  for (int i = 0; i < 3; i++){
    float z = float(i) * 0.5;
    float zoom = 1.0 + (uZoom - 1.0) * (0.30 + 0.70 * z);
    vec2 q = p / zoom * (1.25 - 0.40 * z) + vec2(uPan * (0.25 + 0.75 * z), -uPan * 0.18 * z);
    vec4 b;
    if (i == 0) {
      b = silk(q, uT * 0.70, 0.56, -0.12, 0.20, 0.20, 0.55, 0.0, uFarA, uFarB);
      // feine Strahlen über dem fernen Band (Polarlicht)
      float n = fbm(vec2(q.x * 0.40, uT * 0.042));
      float above = q.y - (0.56 - 0.12 * q.x + 0.15 * sin(q.x * 0.80 + uT * 0.21) + (n - 0.5) * 0.30);
      float ray = noise(vec2(q.x * 9.0 + n * 2.0, uT * 0.12));
      b.rgb += mix(uFarB, uHi, 0.2) * ray * ray * smoothstep(0.0, 0.15, above) * exp(-max(above, 0.0) * 3.0) * 0.30;
      // Tiefe: Dunst liegt über dem fernen Band
      b.rgb = mix(b.rgb, uFog, 0.30) * 0.80;
    } else if (i == 1) {
      b = silk(q, uT * 0.90, 0.06, 0.20, 0.22, 0.17, 0.22, 3.1, uMidA, uMidB);
    } else {
      b = silk(q, uT * 1.10, -0.60, -0.16, 0.24, 0.26, 0.05, 9.3, uNearA, uNearB);
    }
    col = col * (1.0 - b.a * 0.55) + b.rgb;
  }
  // Licht hinter dem Schriftzug: warmer Kern, weiter Hof
  vec2 g = p * vec2(0.55, 1.25);
  float r2 = dot(g, g);
  col += uWarm * uGlow * (0.30 * exp(-r2 * 5.0) + 0.10 * exp(-r2 * 1.4));
  // Lichter weich begrenzen, ohne die Farbe auszuwaschen (über die Helligkeit statt je Kanal)
  float l = dot(col, vec3(0.2126, 0.7152, 0.0722));
  col *= (1.0 - exp(-l * 1.6)) / max(l * 1.6, 1e-4) * 1.2;
  float l2 = dot(col, vec3(0.2126, 0.7152, 0.0722));
  col = max(mix(vec3(l2), col, 1.22), 0.0);
  // Ränder dunkler (Tiefe), kein Grauschleier
  col *= 1.0 - 0.60 * smoothstep(0.55, 1.5, length(uv * vec2(0.95, 1.15)));
  col *= uFade;
  col += (hash(gl_FragCoord.xy + fract(uT * 7.0) * 97.0) - 0.5) / 255.0;
  gl_FragColor = vec4(max(col, vec3(0.0)), 1.0);
}`;

// Farben aus tokens.css (Block E103-intro) -> Uniforms
const PALETTE = {
  uTop: "--c-intro-sky-top", uBot: "--c-intro-sky-bottom", uFog: "--c-intro-fog",
  uFarA: "--c-intro-far-a", uFarB: "--c-intro-far-b", uMidA: "--c-intro-mid-a", uMidB: "--c-intro-mid-b",
  uNearA: "--c-intro-near-a", uNearB: "--c-intro-near-b", uHi: "--c-intro-hi", uWarm: "--c-intro-warm",
};
const UNIFORMS = ["uRes", "uT", "uZoom", "uPan", "uFade", "uGlow", ...Object.keys(PALETTE)];

// Rückfall: vier Ebenen (fernes Band, mittleres, nahes, Licht in der Mitte; Form und Lage in tokens.css),
// die nur wandern: Ausschlag in vw/vh, Umlaufzeiten in s, Tiefe z (nahe wandern stärker mit)
const BLOBS = [
  { ax: 4, ay: 3, tx: 9.0, ty: 11.0, s: 0.05, z: 0.0 },
  { ax: 6, ay: 4, tx: 12.0, ty: 8.5, s: 0.06, z: 0.5 },
  { ax: 8, ay: 4, tx: 10.5, ty: 13.0, s: 0.06, z: 1.0 },
  { ax: 2, ay: 1, tx: 7.0, ty: 9.0, s: 0.04, z: 0.3 },
];
const TAU = Math.PI * 2;

function rgb(value) {
  const m = /^#([0-9a-f]{6})$/i.exec(String(value).trim());
  if (!m) return [0, 0, 0];
  const n = parseInt(m[1], 16);
  return [(n >> 16 & 255) / 255, (n >> 8 & 255) / 255, (n & 255) / 255];
}

// WebGL anlegen; null, wenn es nicht geht (dann CSS)
function makeGL(canvas, info) {
  const t0 = performance.now();
  const opts = { alpha: false, antialias: false, depth: false, stencil: false, premultipliedAlpha: false,
    preserveDrawingBuffer: false, powerPreference: "high-performance" };
  let gl = null;
  let ver = 2;
  try {
    gl = canvas.getContext("webgl2", opts);
    if (!gl) { gl = canvas.getContext("webgl", opts); ver = 1; }
  } catch (e) {
    gl = null;
  }
  info.ctxMs = +(performance.now() - t0).toFixed(1);
  if (!gl) { info.gl = "keins"; return null; }
  info.gl = "webgl" + ver;
  const t1 = performance.now();
  const shader = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; };
  const vs = shader(gl.VERTEX_SHADER, VS);
  const fs = shader(gl.FRAGMENT_SHADER, FS);
  const prog = gl.createProgram();
  gl.attachShader(prog, vs);
  gl.attachShader(prog, fs);
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    info.glError = ((gl.getShaderInfoLog(fs) || "") + " " + (gl.getProgramInfoLog(prog) || "")).trim().slice(0, 300);
    return null;
  }
  info.compileMs = +(performance.now() - t1).toFixed(1);
  gl.useProgram(prog);
  gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);   // ein Dreieck deckt alles
  const loc = gl.getAttribLocation(prog, "aPos");
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  const U = {};
  for (const name of UNIFORMS) U[name] = gl.getUniformLocation(prog, name);
  const root = getComputedStyle(document.documentElement);
  for (const [name, token] of Object.entries(PALETTE)) gl.uniform3fv(U[name], rgb(root.getPropertyValue(token)));
  return { gl, U };
}

// opts: {wantGL, scale (0…1 oder null = nach Bildhöhe), calm}
export function createSky(opts = {}) {
  const info = {};
  const canvas = h("canvas.intro-gl");
  const blobs = h("div.intro-blobs", BLOBS.map(() => h("i")));
  const el = h("div.intro-sky", canvas, blobs);
  let scale = opts.scale > 0 ? Math.min(1, Math.max(0.1, opts.scale)) : (screen.height > 1200 || innerHeight > 1200 ? 0.5 : 0.6);
  let ctx = opts.wantGL === false ? null : makeGL(canvas, info);
  let mode = ctx ? "webgl" : "css";
  let lostReason = ctx ? "" : (opts.wantGL === false ? "verlangt" : "kein WebGL");
  canvas.hidden = !ctx;
  blobs.hidden = !!ctx;
  canvas.addEventListener("webglcontextlost", (e) => { e.preventDefault(); toCss("Kontextverlust"); });

  function resize() {
    const w = Math.max(1, Math.round(innerWidth * devicePixelRatio * scale));
    const hh = Math.max(1, Math.round(innerHeight * devicePixelRatio * scale));
    if (canvas.width !== w || canvas.height !== hh) {
      canvas.width = w;
      canvas.height = hh;
      ctx.gl.viewport(0, 0, w, hh);
      ctx.gl.uniform2f(ctx.U.uRes, w, hh);
    }
  }

  function toCss(reason) {
    if (mode === "css") return;
    mode = "css";
    lostReason = reason;
    ctx = null;
    canvas.hidden = true;
    blobs.hidden = false;
  }

  // u: {t (Fluss-Zeit in s), zoom, pan, fade, glow}
  let last = null;
  function draw(u) {
    last = u;
    if (ctx) {
      if (ctx.gl.isContextLost()) { toCss("Kontextverlust"); return draw(u); }
      resize();
      const { gl, U } = ctx;
      gl.uniform1f(U.uT, u.t);
      gl.uniform1f(U.uZoom, u.zoom);
      gl.uniform1f(U.uPan, u.pan);
      gl.uniform1f(U.uFade, u.fade);
      gl.uniform1f(U.uGlow, u.glow);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      return;
    }
    // Rückfall: Lichtflächen wandern nur über transform; nahe stärker (Parallaxe)
    blobs.style.setProperty("--fx-o", u.fade.toFixed(4));
    blobs.style.setProperty("--fx-s", u.zoom.toFixed(4));
    BLOBS.forEach((b, i) => {
      const k = blobs.children[i];
      const x = b.ax * Math.sin(TAU * u.t / b.tx + i * 2.1) - u.pan * 30 * (0.25 + 0.75 * b.z);
      const y = b.ay * Math.cos(TAU * u.t / b.ty + i * 1.3);
      k.style.setProperty("--fx-x", x.toFixed(3) + "vw");
      k.style.setProperty("--fx-y", y.toFixed(3) + "vh");
      k.style.setProperty("--fx-s", (1 + b.s * Math.sin(TAU * u.t / (b.tx * 1.3) + i)).toFixed(4));
    });
  }

  // Erstes Bild schon im schwarzen Vorlauf: Treiber übersetzen den Shader oft erst beim ersten Zeichnen
  if (ctx) {
    const t2 = performance.now();
    draw({ t: 0, zoom: 1, pan: 0, fade: 0, glow: 0 });
    info.firstDrawMs = +(performance.now() - t2).toFixed(1);
  }

  return {
    el,
    info,
    draw,
    // dasselbe Bild noch einmal (Standbild: die Fläche behält ihr Bild nicht)
    redraw() { if (last && mode === "webgl") draw(last); },
    toCss,
    get mode() { return mode; },
    get scale() { return mode === "webgl" ? scale : 0; },
    get reason() { return lostReason; },
    // Bildzeit-Wache: kleinere Fläche
    setScale(s) { if (mode === "webgl") scale = s; },
  };
}

// Bildzeit-Wache (E103): Liegt der Median der ersten n Bildabstände über slow ms, rechnet die Fläche in
// kleinerer Auflösung (scale); liegen die nächsten n noch darüber, geht es auf den CSS-Rückfall. WebKit
// verbirgt den Renderer („Apple GPU“), ob etwa llvmpipe rechnet, ist in der Seite nicht ablesbar.
// tick(ts) je Bild mit dem Zeitstempel von requestAnimationFrame; reset(), solange nicht gezeichnet wird.
export function createGuard(sky, { n = 12, slow = 20, scale = 0.35 } = {}) {
  let last = null;
  let gaps = [];
  let stage = 0;             // 0 erste Messung, 1 nach dem Verkleinern, 2 fertig
  const medians = [];
  return {
    medians,
    get done() { return stage > 1; },
    reset() { last = null; },
    tick(ts) {
      if (stage > 1 || sky.mode !== "webgl") return;
      if (last !== null) gaps.push(ts - last);
      last = ts;
      if (gaps.length < n) return;
      const sorted = [...gaps].sort((a, b) => a - b);
      const median = (sorted[Math.floor((n - 1) / 2)] + sorted[Math.floor(n / 2)]) / 2;
      medians.push(+median.toFixed(2));
      gaps = [];
      if (median <= slow) { stage = 2; return; }
      if (stage === 0 && sky.scale > scale) { sky.setScale(scale); stage = 1; return; }
      sky.toCss("zu langsam");
      stage = 2;
    },
  };
}
