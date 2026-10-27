// Startvideo und Ruhemodus-Video aus steamdeckrepo.com (E130). Der Kern legt das gewählte Video in den Datenordner
// und nennt es in sys.state.videos {style, boot, suspend, suspendOn} (wiring_videos.py); die Seite spielt es von dort.
//   Startvideo     statt der eigenen Startanimation (components/intro.js), wenn ui.intro an und „Video“ gewählt ist:
//                  schwarz, bis das Video spielbereit ist (höchstens WAIT_MS), dann mit Ton in der Lautstärke der
//                  Bedientöne; ins Home nach dem Ende, frühestens wenn das Home seine Reihen hat, spätestens nach
//                  BOOT_MAX_MS. Jede Taste und PS überspringen. Geht etwas schief (Datei fehlt, Fehler, kein Bild
//                  in WAIT_MS, Abspielen verweigert), läuft die eigene Animation. „Bewegung reduzieren“: die eigene,
//                  ruhige Fassung (ein Video ist Bewegung).
//   Ruhemodus      Ereignis video.suspend {url, maxMs} an die Seite, die den Ruhemodus gewählt hat (power.py): das Video
//                  höchstens maxMs, dann video.played; danach bleibt das Bild schwarz, bis der Rechner aufwacht (die
//                  Wanduhr springt gegenüber performance.now), eine Taste kommt oder COVER_MS vergangen sind.
// Ein- und Ausblenden über einen JS-Zeitplan auf --fx-o (keine CSS-Übergänge, wie intro.js, E52). Mit ?mute=1
// (Prüfläufe) ist das Video stumm. Prüfhilfe window.__couchVideo.
// Abgespielt wird die ganze Datei als Blob (fetch über couch://art), nicht die Adresse selbst: So spielte WebKitGTK 2.54
// hier auch die Trailer (E123), und es hängt nicht daran, ob die Quelle Teilabrufe (Range) kann.
import { call, on, params } from "../core/bridge.js";
import { h, cssMs } from "../core/dom.js";
import { sound } from "../core/sound.js";
import { store } from "../core/store.js";

const WAIT_MS = 2500;             // so lange darf das Startvideo bis zum ersten Bild brauchen
const BOOT_MAX_MS = 31000;        // längstes Startvideo (die Auswahl bietet bis 30 s an)
const COVER_MS = 8000;            // Ruhemodus: so lange bleibt es nach dem Video schwarz, wenn nichts geschieht
const WAKE_JUMP_MS = 3000;        // Wanduhr vor performance.now um mehr als das: der Rechner hat geschlafen
const FADE_IN_MS = 250;
const RETRY_MS = 300;             // zweiter Versuch, wenn play() ablehnt

const info = { state: "idle", kind: "", reason: "", played: 0, skipped: false, volume: 0, muted: false, w: 0, h: 0 };
window.__couchVideo = info;

const clamp = (x) => Math.max(0, Math.min(1, x));

function volume() {
  const ui = store.ui || {};
  const raw = Number(ui.soundVolume ?? 60);
  if (ui.sounds === false) return 0;
  return clamp((Number.isFinite(raw) ? raw : 60) / 100);
}

// Gewähltes Video einer Art aus sys.state.videos, sonst null. Das Startvideo nur, wenn es die Startanimation ist.
export function videoFor(kind) {
  const v = store.state.videos;
  if (!v || typeof v !== "object") return null;
  const item = v[kind];
  if (!item || typeof item.url !== "string" || !item.url) return null;
  if (kind === "boot" && v.style !== "video") return null;
  if (kind === "suspend" && v.suspendOn !== true) return null;
  return item;
}

// Die Datei als Blob-Adresse (null, wenn sie fehlt oder nicht lesbar ist)
async function blobOf(url) {
  try {
    const r = await fetch(url);
    if (!r.ok) return null;
    const b = await r.blob();
    return b.size ? URL.createObjectURL(new Blob([b], { type: "video/webm" })) : null;
  } catch (e) {
    return null;
  }
}

// Ein Element mit Video über allem; opacity per Zeitplan. ready: Promise, erfüllt mit true, sobald die Quelle gesetzt ist
function stage(url, cls) {
  const video = h("video", { playsinline: true, preload: "auto", disablepictureinpicture: true });
  const muted = sound.muted || params.get("mute") === "1" || volume() <= 0;
  video.muted = muted;
  video.volume = muted ? 0 : volume();
  info.muted = muted;
  info.volume = +video.volume.toFixed(3);
  const el = h("div.intro-video" + (cls ? "." + cls : ""), { "aria-hidden": "true" }, video);
  el.style.setProperty("--fx-o", "1");
  document.body.append(el);
  const ready = blobOf(url).then((src) => {
    if (!src || !el.isConnected) { if (src) URL.revokeObjectURL(src); return false; }
    video.dataset.blob = src;
    video.src = src;
    return true;
  });
  return { el, video, ready };
}

// Deckkraft von from nach to in ms (je Bild gesetzt), danach then()
function fade(el, from, to, ms, then, media) {
  const t0 = performance.now();
  const v0 = media ? media.volume : 0;
  const step = () => {
    const k = clamp((performance.now() - t0) / Math.max(1, ms));
    el.style.setProperty("--fx-o", (from + (to - from) * k).toFixed(4));
    if (media && !media.muted) media.volume = clamp(v0 * (to < from ? 1 - k : 1));
    if (k < 1 && el.isConnected) requestAnimationFrame(step);
    else if (then) then();
  };
  // rAF bleibt aus, wenn die Seite gerade nicht gezeichnet wird: dann gleich ans Ende
  if (store.visible === false) { el.style.setProperty("--fx-o", String(to)); if (then) then(); return; }
  requestAnimationFrame(step);
}

function release(video) {
  try { video.pause(); } catch (e) { /* schon weg */ }
  video.removeAttribute("src");
  video.load();
  if (video.dataset.blob) { URL.revokeObjectURL(video.dataset.blob); delete video.dataset.blob; }
}

// ------------------------------------------------------------------ Startvideo
// homeReady: Promise wie bei der eigenen Animation; fallback(): startet die eigene Animation und liefert deren done.
// Liefert den Zustand {done, leaving, leave(quick)} für intro.js (introActive, skipIntro).
export function startVideoIntro(item, homeReady, fallback) {
  const { el, video, ready: loaded } = stage(item.url, "is-boot");
  el.style.setProperty("--fx-o", "1");
  let finish;
  const done = new Promise((resolve) => { finish = resolve; });
  const state = { leaving: false, done, leave: null, video: true };
  let ready = false;
  let started = false;
  let ended = false;
  let t0 = performance.now();
  let waitTimer = null;
  let maxTimer = null;
  Object.assign(info, { state: "loading", kind: "boot", reason: "", played: 0, skipped: false });
  Promise.resolve(homeReady).then(() => { ready = true; tryLeave(); }, () => { ready = true; tryLeave(); });

  function cleanup() {
    clearTimeout(waitTimer);
    clearTimeout(maxTimer);
    release(video);
    el.remove();
  }

  // Ohne Bild bis jetzt: die eigene Animation (sie beginnt schwarz, der Wechsel ist nicht zu sehen)
  function fail(reason) {
    if (state.leaving || started) return;
    state.leaving = true;
    info.state = "fallback";
    info.reason = reason;
    cleanup();
    Promise.resolve(fallback()).then(finish, finish);
  }

  function tryLeave() {
    if (!state.leaving && ended && ready) state.leave(false);
  }

  state.leave = (quick) => {
    if (state.leaving) return;
    state.leaving = true;
    info.skipped = !!quick;
    info.state = quick ? "skipped" : "out";
    info.played = Math.round(performance.now() - t0);
    clearTimeout(waitTimer);
    clearTimeout(maxTimer);
    fade(el, Number(el.style.getPropertyValue("--fx-o")) || 1, 0, cssMs(quick ? "--t-intro-skip" : "--t-intro-out"), () => {
      cleanup();
      finish();
    }, video);
  };

  // Fehler vor dem ersten Bild: die eigene Animation; mitten im Video: ins Home
  video.addEventListener("error", () => { if (started) state.leave(false); else fail("error"); });
  video.addEventListener("playing", () => {
    if (state.leaving || started) return;
    started = true;
    clearTimeout(waitTimer);
    t0 = performance.now();
    info.state = "playing";
    info.w = video.videoWidth;
    info.h = video.videoHeight;
    maxTimer = setTimeout(() => { ended = true; tryLeave(); if (!ready) maxTimer = setTimeout(() => state.leave(false), cssMs("--t-intro-max")); }, BOOT_MAX_MS);
  });
  // Ende: das letzte Bild bleibt stehen, bis das Home bereit ist (spätestens --t-intro-max später)
  video.addEventListener("ended", () => {
    ended = true;
    info.state = "ended";
    info.played = Math.round(performance.now() - t0);
    if (!ready) { clearTimeout(maxTimer); maxTimer = setTimeout(() => state.leave(false), cssMs("--t-intro-max")); }
    tryLeave();
  });
  waitTimer = setTimeout(() => fail("timeout"), WAIT_MS);
  loaded.then((ok) => {
    if (state.leaving) return;
    if (!ok) { fail("missing"); return; }
    // Einmal nachfassen: Im unsichtbaren Gamescope lehnte das erste play() nach dem Start des Web-Prozesses einmal ab,
    // dieselbe Datei spielte danach (2026-10-27); ein zweites Nein heißt die eigene Animation
    video.play().catch(() => setTimeout(() => {
      if (!state.leaving && !started) video.play().catch((e) => fail("refused:" + ((e && e.name) || "")));
    }, RETRY_MS));
  });
  return state;
}

// ------------------------------------------------------------------ Ruhemodus-Video
let sleeping = null;              // {el, video, leave(quick)}

export function suspendActive() {
  return !!sleeping && !sleeping.gone;
}

// Eine Taste: Video bzw. schwarzes Bild weg (nach dem Aufwachen ist das der erste Druck)
export function skipSuspend() {
  if (sleeping) sleeping.leave(true);
}

function playSuspend(p) {
  const url = p && typeof p.url === "string" ? p.url : "";
  const maxMs = Math.max(500, Math.min(10000, Number(p && p.maxMs) || 5000));
  if (sleeping) sleeping.leave(true);
  if (!url) { call("video.played", {}).catch(() => {}); return; }
  const { el, video, ready: loaded } = stage(url, "is-suspend");
  el.style.setProperty("--fx-o", "0");
  Object.assign(info, { state: "loading", kind: "suspend", reason: "", played: 0, skipped: false });
  const me = { el, video, gone: false, leave: null };
  sleeping = me;
  let reported = false;
  let t0 = performance.now();
  let wall = Date.now();
  let mono = performance.now();
  let stopTimer = null;
  let coverTimer = null;
  let wakeTimer = null;

  // Video zu Ende (oder ging nicht): dem Kern melden, er schläft dann; das Bild bleibt schwarz
  function report(reason) {
    if (reported) return;
    reported = true;
    clearTimeout(stopTimer);
    info.reason = reason || info.reason;
    info.played = Math.round(performance.now() - t0);
    try { video.pause(); } catch (e) { /* schon aus */ }
    video.style.visibility = "hidden";
    call("video.played", {}).catch(() => {});
    wall = Date.now();
    mono = performance.now();
    coverTimer = setTimeout(() => me.leave(false), COVER_MS);
    wakeTimer = setInterval(() => {
      const awake = (Date.now() - wall) - (performance.now() - mono) > WAKE_JUMP_MS;
      if (awake) me.leave(false);
    }, 250);
  }

  me.leave = (quick) => {
    if (me.gone) return;
    me.gone = true;
    if (!reported) { info.skipped = !!quick; report(quick ? "skipped" : "left"); }
    clearTimeout(coverTimer);
    clearInterval(wakeTimer);
    info.state = "idle";
    fade(el, Number(el.style.getPropertyValue("--fx-o")) || 1, 0, cssMs("--t-intro-skip"), () => {
      release(video);
      el.remove();
      if (sleeping === me) sleeping = null;
    });
  };

  video.addEventListener("error", () => { info.state = "failed"; report("error"); });
  video.addEventListener("ended", () => { info.state = "ended"; report("ended"); });
  video.addEventListener("playing", () => {
    if (reported) return;
    t0 = performance.now();
    info.state = "playing";
    info.w = video.videoWidth;
    info.h = video.videoHeight;
    fade(el, 0, 1, FADE_IN_MS);
    clearTimeout(stopTimer);
    stopTimer = setTimeout(() => report("max"), maxMs);
  });
  // Kommt gar kein Bild, schläft der Rechner trotzdem (der Kern wartet ohnehin nicht länger)
  stopTimer = setTimeout(() => report("timeout"), maxMs);
  loaded.then((ok) => {
    if (reported) return;
    if (!ok) { info.state = "failed"; report("missing"); return; }
    video.play().catch((e) => report("refused:" + ((e && e.name) || "")));
  });
}

on("video.suspend", playSuspend);
