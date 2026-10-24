// Töne der Oberfläche über Web Audio. Die Dateien liegen in web/sounds/ (rein synthetisch,
// siehe sounds/make-sounds.sh) und als Klangpaket in web/sounds/<paket>/ (E58: "pc5", Kopie aus
// einem Decky-Paket („PC5 Sounds“); Einstellung audio.ui_pack, kommt als state.ui.soundPack). Fehlt dem Paket ein
// Ton, gilt der eigene; fehlt sein Ordner (E104: nie im Paket), gelten alle eigenen. Regeln nach 07 Abschnitt 7:
// - beim Halten einer Richtung höchstens alle 60 ms und um 6 dB leiser,
// - kein Ton, solange die Oberfläche nicht vorn ist; nur "toast" darf immer,
// - Lautstärke und Ein/Aus kommen aus den Einstellungen (state.ui).
// Mit ?mute=1 wird nie ein AudioContext angelegt (Prüfläufe dürfen nichts hörbar machen).
import { params, role } from "./bridge.js";
import { store } from "./store.js";

export const SOUND_NAMES = [
  "focus", "edge", "select", "back", "tab", "toggle-on", "toggle-off",
  "overlay-open", "overlay-close", "toast", "launch", "error",
];
// Lange Töne außerhalb der Liste (die Prüfung verlangt dort kurze): nur die eigenen, nie aus einem
// Paket, und nicht beim Vorladen. play() gibt für sie einen Griff {stop(fadeMs)} zurück (E103: beim
// Überspringen der Startanimation geht der Ton in 250 ms aus). "intro": Startanimation (E103, components/intro.js)
export const LONG_SOUNDS = ["intro"];
// Welche Töne ein Paket mitbringt (so fragt die Seite nie nach einer Datei, die es nicht gibt)
const PACKS = { pc5: SOUND_NAMES.filter((n) => n !== "error") };
// Pakete, deren Dateien hier fehlen (E104). Der Kern meldet dann schon "couch" (settings/audio.ui_pack);
// nennt der Zustand trotzdem ein fehlendes Paket (Attrappe, alter Stand), merkt die Seite es am ersten
// fehlenden Ton und nimmt ab da die eigenen
const missingPacks = new Set();
const HOLD_GAP_MS = 60;
const HOLD_GAIN = 0.5;          // −6 dB
const SLEEP_MS = 20000;         // so lange nach dem letzten Ton bleibt der Ausgang offen
const muted = params.get("mute") === "1";

let ctx = null;
let master = null;
const buffers = new Map();      // Adresse -> AudioBuffer
const loading = new Map();      // Adresse -> Promise
let lastFocusAt = 0;
let edgeHeld = false;           // der Rand wurde in diesem Halten schon angeschlagen
let sleepTimer = null;
const played = [];              // die letzten Abspielwünsche, für Prüfungen (auch wenn stumm)
// Diagnose am Fernseher (2026-10-18: „keine Töne“, der Tontest der Einstellungen klang): je Grund
// höchstens einmal je Minute eine Zeile ins home.log (Konsole der Seite)
const noted = new Map();
function note(key, text) {
  const now = performance.now();
  if (now - (noted.get(key) || -1e9) < 60000) return;
  noted.set(key, now);
  console.warn("Töne: " + text);
}

function context() {
  if (muted) return null;
  if (!ctx) {
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return null;
    ctx = new Ctor({ latencyHint: "interactive" });
    master = ctx.createGain();
    master.connect(ctx.destination);
    console.info("Töne: Ausgabe angelegt (" + ctx.state + ", " + ctx.sampleRate + " Hz, Paket " + (currentPack() || "eigene") + ")");
    ctx.onstatechange = () => { if (ctx.state !== "suspended") console.info("Töne: Ausgabe " + ctx.state); };
  }
  return ctx;
}

// In Ruhe hält die Seite keinen Tonausgang offen: der Kontext schläft und wacht zum nächsten Ton auf
function snooze() {
  clearTimeout(sleepTimer);
  sleepTimer = setTimeout(() => { if (ctx && ctx.state === "running") ctx.suspend().catch(() => {}); }, SLEEP_MS);
}

// "" = die eigenen Töne ("couch", auch ohne oder mit unbekannter Angabe; bis E104 war das "pc5")
function currentPack() {
  const p = (store.state.ui || {}).soundPack;
  return PACKS[p] && !missingPacks.has(p) ? p : "";
}

function urlFor(name, pack = currentPack()) {
  if (LONG_SOUNDS.includes(name)) return "sounds/" + name + ".wav";
  return pack && PACKS[pack].includes(name) ? "sounds/" + pack + "/" + name + ".wav" : "sounds/" + name + ".wav";
}

async function fetchBytes(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error("Ton fehlt: " + url);
  return res.arrayBuffer();
}

// Bytes eines Tons {bytes, own}: aus dem Paket; fehlt die Datei dort, der eigene Ton (und das Paket
// gilt ab da als nicht vorhanden)
async function soundBytes(name, pack) {
  const own = urlFor(name, "");
  const url = urlFor(name, pack);
  if (url === own) return { bytes: await fetchBytes(own), own: true };
  try {
    return { bytes: await fetchBytes(url), own: false };
  } catch (e) {
    if (!missingPacks.has(pack)) {
      missingPacks.add(pack);
      console.warn("Töne: Paket " + pack + " fehlt (" + (e && e.message || e) + "), es gelten die eigenen");
    }
    return { bytes: await fetchBytes(own), own: true };
  }
}

function load(name) {
  const pack = currentPack();
  const url = urlFor(name, pack);
  if (buffers.has(url)) return Promise.resolve(buffers.get(url));
  if (!loading.has(url)) {
    const c = context();
    if (!c) return Promise.resolve(null);
    loading.set(url, soundBytes(name, pack)
      .then((got) => c.decodeAudioData(got.bytes))
      .then((buf) => { buffers.set(url, buf); return buf; })
      .catch((e) => { console.warn("Ton " + name + ": " + e.message); return null; }));
  }
  return loading.get(url);
}

// Einstellungen „Töne der Oberfläche“ und „Lautstärke der Töne“ (audio.ui_sounds, audio.ui_volume):
// der Kern liefert sie als state.ui.sounds und state.ui.soundVolume (0…100)
function settings() {
  const ui = store.state.ui || {};
  const raw = Number(ui.soundVolume ?? 60);
  return { enabled: ui.sounds !== false, volume: Math.max(0, Math.min(100, Number.isFinite(raw) ? raw : 60)) / 100 };
}

// Hörprobe: Ändert sich die Lautstärke der Töne (Regler in den Einstellungen), klingt kurz ein Ton
// in der neuen Lautstärke. Höchstens alle 150 ms, sonst rattert es beim Halten.
let lastVolume = null;
let lastPreviewAt = 0;
let lastPack = null;
function watchSettings() {
  store.subscribe(["ui"], () => {
    const cfg = settings();
    const key = cfg.enabled ? cfg.volume : -1;
    if (lastVolume !== null && key !== lastVolume && key > 0 && performance.now() - lastPreviewAt > 150) {
      lastPreviewAt = performance.now();
      play("focus");
    }
    lastVolume = key;
    // Anderes Klangpaket: seine Töne laden und einmal hören lassen
    const pack = currentPack();
    if (lastPack !== null && pack !== lastPack) {
      if (!muted) for (const name of SOUND_NAMES) load(name);
      if (cfg.enabled && cfg.volume > 0) play("select");
    }
    lastPack = pack;
  });
  const cfg = settings();
  lastVolume = cfg.enabled ? cfg.volume : -1;
  lastPack = currentPack();
}

// Darf gerade ein Ton kommen? Das Home schweigt, solange eine App vorn ist; das Overlay
// spielt nur, wenn es bedient wird.
function allowed(name) {
  if (name === "toast") return true;
  // Öffnet der Kern eine Ansicht im Overlay, folgt sein Zustand (overlay "active", visible) erst
  // danach (shell.open_view): Am alten Stand scheiterte dieser Ton bisher jedes Mal (home.log)
  if (role === "overlay" && name === "overlay-open") return true;
  if (!store.visible) return false;
  if (role === "home") return !store.state.foreground;
  return store.state.overlay === "active" || store.state.overlay === undefined;
}

// Griff für einen langen Ton (E103): stop(fadeMs) blendet ihn über einen eigenen GainNode aus und hält
// ihn an. Kommt stop() an, bevor der Ton dekodiert ist, beginnt er gar nicht erst.
function longHandle() {
  let src = null;
  let gain = null;
  const handle = {
    stopped: false,
    attach(s, g) { src = s; gain = g; },
    stop(fadeMs = 0) {
      if (handle.stopped) return;
      handle.stopped = true;
      if (!src || !ctx) return;
      const now = ctx.currentTime;
      const end = now + Math.max(0, fadeMs) / 1000;
      try {
        gain.gain.cancelScheduledValues(now);
        gain.gain.setValueAtTime(gain.gain.value, now);
        gain.gain.linearRampToValueAtTime(0, end);
        src.stop(end + 0.02);
      } catch (e) {
        note("stop", "Ton lässt sich nicht anhalten: " + (e && e.message || e));
      }
    },
  };
  return handle;
}

function play(name, opts = {}) {
  if (!name) return false;
  // Rand: beim Halten einer Richtung nur einmal anstoßen, nicht im Takt der Wiederholung brummen
  // (07 2.2). Jeder andere Ton (der Fokus ist weitergewandert) gibt den Rand wieder frei.
  if (name === "edge") {
    if (opts.repeat && edgeHeld) return false;
    edgeHeld = true;
  } else {
    edgeHeld = false;
  }
  const now = performance.now();
  let gain = 1;
  if (opts.repeat) {
    if (now - lastFocusAt < HOLD_GAP_MS) return false;
    gain = HOLD_GAIN;
  }
  if (name === "focus") lastFocusAt = now;
  const cfg = settings();
  if (!cfg.enabled || cfg.volume <= 0) { note("off", "aus (Einstellung " + cfg.enabled + ", Lautstärke " + cfg.volume + ")"); return false; }
  if (!allowed(name)) {
    note("hidden", "nicht erlaubt (" + name + ", sichtbar " + store.visible + ", vorn " + store.state.foreground + ", Overlay " + store.state.overlay + ")");
    return false;
  }
  played.push(name);
  if (played.length > 50) played.shift();
  const c = context();
  if (!c) return false;
  const handle = LONG_SOUNDS.includes(name) ? longHandle() : null;
  load(name).then(async (buf) => {
    if (!buf || !ctx) { note("nobuf", name + " nicht geladen"); return; }
    if (handle && handle.stopped) return;
    if (ctx.state !== "running") {
      // resume() kann ohne Antwort bleiben (Web Audio hängt): nach 1,5 s trotzdem weiter
      const ok = await Promise.race([
        ctx.resume().then(() => true, (e) => { note("resume", "Ausgabe lässt sich nicht starten: " + (e && e.message || e)); return false; }),
        new Promise((done) => setTimeout(() => done(null), 1500)),
      ]);
      if (ok === false) return;
      if (ok === null) note("hang", "resume() antwortet nicht (" + ctx.state + ")");
      else if (ctx.state !== "running") note("state", "Ausgabe bleibt " + ctx.state + " nach resume()");
    }
    if (handle && handle.stopped) return;      // während resume() übersprungen
    snooze();
    const src = ctx.createBufferSource();
    const g = ctx.createGain();
    g.gain.value = cfg.volume * gain;
    src.buffer = buf;
    src.connect(g).connect(master);
    if (handle) handle.attach(src, g);
    src.start();
  });
  return handle || true;
}

// Alle Töne einmal dekodieren, damit der erste Tastendruck nicht verzögert klingt
// (main.js ruft das einmal nach store.init: ab hier gelten auch die Einstellungen aus state.ui)
let watching = false;
function preload() {
  if (!watching) { watching = true; watchSettings(); }
  if (muted) return;
  for (const name of SOUND_NAMES) load(name);
  snooze();
}

// Prüfung ohne Ausgabegerät: dekodiert jede Datei in einem OfflineAudioContext. pack = "" prüft
// die eigenen Töne, sonst die des Pakets (dort fehlende zählen nicht mit) über denselben Weg wie
// das Abspielen: fehlt die Datei im Paket, steht own: true am eigenen Ton (E104)
async function selfTest(pack = "") {
  const Ctor = window.OfflineAudioContext || window.webkitOfflineAudioContext;
  const off = new Ctor(1, 4410, 44100);
  const out = {};
  for (const name of pack ? PACKS[pack] || [] : SOUND_NAMES) {
    try {
      const got = await soundBytes(name, pack);
      const buf = await off.decodeAudioData(got.bytes);
      let peak = 0;
      const data = buf.getChannelData(0);
      for (let i = 0; i < data.length; i++) peak = Math.max(peak, Math.abs(data[i]));
      out[name] = { ms: Math.round(buf.duration * 1000), peak: Math.round(peak * 1000) / 1000, ...(pack && got.own ? { own: true } : {}) };
    } catch (e) {
      out[name] = { error: String(e && e.message || e) };
    }
  }
  return out;
}

// Einen Ton schon laden und dekodieren (die Startanimation wartet darauf, damit Bild und Ton
// zusammen beginnen). Stumm oder ohne Ausgabe: null
function prepare(name) {
  return muted ? Promise.resolve(null) : load(name);
}

export const sound = { play, prepare, preload, selfTest, played, get muted() { return muted; }, get pack() { return currentPack(); } };
