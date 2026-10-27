// Trailer und Spielmusik hinter dem Hub (E123, E132). Ruht die Auswahl REST_MS auf einem Spiel (Home-Hub oder
// Spielseite), fragt die Seite game.trailer und spielt die Adresse, die der Kern nennt: einen Datenstrom des
// Trailer-Dienstes auf 127.0.0.1 (trailerfeed.py). Der Kern holt die Stücke selbst und reicht sie als eine Datei weiter;
// die Seite lädt nichts in den Speicher (kein Blob mehr, E132).
//   Bild  (Einstellung „Trailer im Hintergrund“, sys.state.trailers): bis 720p, stumm, in einer eigenen Ebene direkt
//         über dem Hintergrund und unter Reihe und Hub; blendet weich ein. backdrop.js bleibt ohne <video> (E33).
//   Ton   (Einstellung „Spielmusik im Home“, sys.state.game_music, Vorgabe aus): leise (Lautstärke der Bedientöne mal
//         MUSIC_LEVEL), weich ein- und ausgeblendet. Mit Bild spielt die Ebene die Fassung mit Ton (av: Bild und Ton in
//         einer Datei, kein Abgleich nötig), ohne Bild nur den Ton (audio). Nie, solange ein Spiel oder eine App läuft
//         (außer angehaltenen Spielen, die schweigen), also nie über einem Spiel oder einer Medien-App.
// Zurück zum Bild bzw. Stille: Ende, anderer Eintrag, Bildschirm verlassen oder verdeckt (auch der Spielstart: das
// Ladebild liegt darüber), Seite nicht zu sehen (store.visible), Schalter aus. Das Bild zusätzlich bei „Bewegung
// reduzieren“, „Bewegter Hintergrund“ aus und im Home mit eigenem Hintergrund (sys.state.background, E121).
// Verworfen (Standbild, für diesen Eintrag nicht von vorn): kein erstes Bild nach START_MS, Zeit steht beim Spielen
// STALL_MS, Fehler des Elements, Strom länger als seine Länge plus OVER_S. Hängt trotzdem der Web-Prozess, lässt der
// Kern den Trailer dieses Spiels beim nächsten Mal aus (wiring_gameinfo.page_reset).
// Mit ?mute=1 (Prüfläufe) entsteht nie ein hörbares Element: Die Musik zählt dann nur ihren Zustand („silent“).
import { h, cssMs } from "../core/dom.js";
import { router } from "../core/router.js";
import { sound } from "../core/sound.js";
import { store } from "../core/store.js";
import { gameTrailer, wantsInfo } from "./gameinfo.js";

const REST_MS = 3000;             // so lange ruht die Auswahl, bevor geladen wird
const START_MS = 10000;           // so lange darf es bis zum ersten Bild bzw. Ton dauern
const STALL_MS = 8000;            // steht die Zeit beim Spielen so lange, wird der Trailer verworfen
const OVER_S = 20;                // so viel länger als seine Länge darf ein Trailer laufen
const RETRY_MS = 300;             // ein abgelehntes play() gleich nach dem Start des Web-Prozesses (E130): noch einmal
const OWN_MODES = ["image", "slideshow"];
const MUSIC_LEVEL = 0.4;          // „leise“: Anteil der Lautstärke der Bedientöne
const MUSIC_IN_MS = 1500;
const MUSIC_TAIL_S = 1.5;         // so lange vor dem Ende blendet der Ton aus

// hubEl: der Hub; die Ebene hängt sich in denselben Bildschirm, direkt hinter dessen .backdrop.
// opts (Store, E132): source(entry) statt game.trailer des Eintrags, attach(el) statt hinter .backdrop, music: false
export function createTrailer(hubEl, opts = {}) {
  const video = h("video", { muted: true, playsinline: true, preload: "auto", disablepictureinpicture: true });
  video.muted = true;
  const audio = h("audio", { preload: "auto" });
  const el = h("div.trailer", { "aria-hidden": "true" }, video, h("div.trailer-dim"));
  const withMusic = opts.music !== false;
  let entry = null;
  let restTimer = null;
  let fadeTimer = null;
  let rampTimer = null;
  let guard = null;
  let serial = 0;
  let media = null;               // das Element, das gerade spielt (video oder audio)
  let mode = "";                  // "video" (stumm), "av" (Bild mit Ton), "audio" (nur Ton), "silent" (Prüflauf)
  let info = null;                // Antwort von game.trailer zum laufenden Trailer
  let playing = false;            // Bild läuft (eingeblendet)
  let music = false;              // Ton läuft (auch „silent“ im Prüflauf)
  let tail = false;               // der Ton blendet zum Ende aus
  let began = 0;                  // Start des Ladens bzw. letzter Fortschritt (Wächter)
  let lastTime = -1;
  let done = "";                  // für diesen Eintrag ist Schluss (Ende, kein Trailer, Fehler): nicht von vorn
  // Prüfhilfe (window.__couchTest): was die Ebene gerade tut
  const stats = { state: "idle", music: "idle", id: "", mode: "", error: "" };
  function setState(state, error) {
    stats.state = state;
    el.dataset.state = state;
    if (error !== undefined) { stats.error = error; el.dataset.error = error; }
  }
  function setMusic(state) {
    stats.music = state;
    el.dataset.music = state;
  }

  function screenEl() { return hubEl.closest(".screen"); }

  function attach() {
    if (el.isConnected) return true;
    if (opts.attach) return opts.attach(el) !== false && el.isConnected;
    const root = hubEl.parentElement;
    if (!root) return false;
    const bd = root.querySelector(":scope > .backdrop");
    if (bd) bd.after(el); else root.prepend(el);
    return true;
  }

  // Liegt der eigene Bildschirm oben (kein anderer darüber, nicht verdeckt) und ist die Seite zu sehen?
  function common() {
    const scr = screenEl();
    const top = router.top;
    return !!(store.visible && scr && top && top.inst.el === scr && !scr.inert && !scr.hidden && wantsInfo(entry));
  }

  function videoAllowed() {
    const ui = store.ui;
    if (store.state.trailers === false || ui.reduceMotion || ui.liveBackground === false) return false;
    const scr = screenEl();
    const bg = store.state.background;
    // Eigener Hintergrund im Home (E121): Dort steht dann gar kein Spielbild, also auch kein Trailer
    if (scr && scr.dataset.screen === "home" && bg && OWN_MODES.includes(bg.mode)) return false;
    return common();
  }

  // Lautstärke der Bedientöne (audio.ui_volume als ui.soundVolume 0…100), leiser
  function level() {
    const raw = Number(store.ui.soundVolume ?? 60);
    return Math.max(0, Math.min(100, Number.isFinite(raw) ? raw : 60)) / 100 * MUSIC_LEVEL;
  }

  function musicAllowed() {
    if (!withMusic || store.state.game_music !== true || level() <= 0) return false;
    // Läuft ein Spiel oder eine App (auch im Hintergrund), schweigt die Musik; angehaltene Spiele sind still
    // (Arten wie launch/spec.APP_KINDS: game und emulator sind Spiele, media, browser, desktop, tool sind Apps)
    const busy = (store.state.running || []).some((r) => !["game", "emulator"].includes(r.kind) || !r.frozen || r.closing);
    return !busy && !store.state.foreground && common();
  }

  // Was jetzt laufen soll: "", "video", "av", "audio" oder "silent" (Ton gewünscht, aber Prüflauf bzw. ohne Tonspur)
  function wanted(t) {
    const v = videoAllowed();
    const m = musicAllowed();
    if (v && m && t && t.av && !sound.muted) return "av";
    if (v) return "video";
    if (m && t && t.audio && !sound.muted) return "audio";
    if (m && (sound.muted || !t || !t.audio)) return "silent";
    return "";
  }

  // ---------------------------------------------------------------- Abbauen
  function release(elm) {
    elm.pause();
    elm.removeAttribute("src");
    elm.load();                    // schließt die Verbindung zum Trailer-Dienst
  }

  function releaseVideo() {
    clearTimeout(fadeTimer);
    fadeTimer = null;
    release(video);
    el.hidden = true;
  }

  function releaseAudio() {
    clearInterval(rampTimer);
    rampTimer = null;
    release(audio);
  }

  // Lautstärke weich auf target, danach then(); setInterval statt Animation: Ton braucht kein Bild
  function ramp(elm, target, ms, then) {
    clearInterval(rampTimer);
    const from = elm.volume;
    const t0 = performance.now();
    rampTimer = setInterval(() => {
      const p = Math.min(1, (performance.now() - t0) / Math.max(1, ms));
      elm.volume = Math.max(0, Math.min(1, from + (target - from) * p));
      if (p >= 1) { clearInterval(rampTimer); rampTimer = null; if (then) then(); }
    }, 50);
  }

  // Zurück zum Bild bzw. Stille: Bild ausblenden, Ton ausblenden, danach freigeben (der Decoder hält sonst Speicher)
  function stop(state = "stopped") {
    serial += 1;
    clearTimeout(restTimer);
    restTimer = null;
    clearInterval(guard);
    guard = null;
    if (stats.state !== "idle") setState(state);
    if (stats.music !== "idle") setMusic(state);
    const was = mode;
    const elm = media;
    mode = "";
    media = null;
    info = null;
    music = false;
    tail = false;
    if (playing || el.classList.contains("is-on")) {
      playing = false;
      el.classList.remove("is-on");
      if (was === "av") ramp(video, 0, cssMs("--t-bd-fade"));
      clearTimeout(fadeTimer);
      fadeTimer = setTimeout(releaseVideo, cssMs("--t-bd-fade") + 50);
    } else if (video.getAttribute("src")) {
      releaseVideo();
    } else {
      el.hidden = true;
    }
    if (elm === audio && audio.getAttribute("src")) ramp(audio, 0, cssMs("--t-bd-fade"), releaseAudio);
    else if (audio.getAttribute("src")) releaseAudio();
  }

  // Verwerfen: für diesen Eintrag Schluss, Standbild
  function fail(why) {
    done = stats.id;
    stop("failed");
    stats.error = el.dataset.error = why;
  }

  // ---------------------------------------------------------------- Laden und Abspielen
  function play(elm, mine, again = true) {
    elm.play().catch((e) => {
      if (mine !== serial) return;
      if (again) { setTimeout(() => { if (mine === serial) play(elm, mine, false); }, RETRY_MS); return; }
      fail(String((e && e.message) || e));
    });
  }

  // Wächter: kein erstes Bild, stehende Zeit, zu lang
  function watch(mine) {
    clearInterval(guard);
    began = performance.now();
    lastTime = -1;
    guard = setInterval(() => {
      if (mine !== serial || !media) { clearInterval(guard); guard = null; return; }
      const now = performance.now();
      const t = media.currentTime || 0;
      if (t > lastTime + 0.05) { lastTime = t; began = now; }
      const started = stats.state === "playing" || stats.music === "playing";
      if (!started && now - began > START_MS) { fail("start-timeout"); return; }
      if (started && now - began > STALL_MS) { fail("stalled"); return; }
      const seconds = Number(info && info.seconds) || 0;
      if (seconds > 0 && t > seconds + OVER_S) fail("too-long");
    }, 1000);
  }

  async function start(target) {
    const mine = ++serial;
    const same = () => mine === serial && entry && entry.id === target.id;
    if (!videoAllowed() && !musicAllowed()) return;
    stats.id = target.id;
    const askVideo = videoAllowed();
    const askMusic = musicAllowed();
    if (askVideo) setState("loading", "");
    if (askMusic) setMusic("loading");
    const r = await (opts.source ? opts.source(target) : gameTrailer(target.id));
    if (!same()) return;
    const t = r && r.ok && r.trailer ? r.trailer : null;
    if (!t || !t.video) {
      if (askVideo) setState("none");
      if (askMusic) setMusic("none");
      done = target.id;
      return;
    }
    const m = wanted(t);
    if (!m) { setState("idle"); setMusic("idle"); return; }
    if (!askVideo && stats.state !== "idle") setState("idle");
    info = t;
    mode = m;
    tail = false;
    // Ton gewünscht, aber Prüflauf (?mute=1) bzw. ohne Tonspur: Die Musik zählt nur ihren Zustand
    if (m === "silent" || (m === "video" && musicAllowed())) {
      music = true;
      setMusic(t.audio ? "silent" : "none");
      if (m === "silent") return;
    }
    if (m === "video" || m === "av") {
      if (!attach()) { setState("failed", "attach"); done = target.id; return; }
      clearTimeout(fadeTimer);
      el.hidden = false;
      media = video;
      video.muted = m !== "av";
      video.volume = m === "av" ? 0 : 1;
      if (m === "av") setMusic("starting");
      video.src = m === "av" ? t.av : t.video;
      setState("starting");
    } else if (m === "audio") {
      media = audio;
      audio.volume = 0;
      audio.src = t.audio;
      setMusic("starting");
    }
    if (media) { watch(mine); play(media, mine); }
  }

  // Ein Bild ist da: einblenden (vorher stünde ein schwarzer Kasten über dem Hintergrund); der Ton blendet ein
  video.addEventListener("playing", () => {
    if (media !== video || stats.state !== "starting") return;
    playing = true;
    setState("playing");
    el.classList.add("is-on");
    if (mode === "av") { music = true; setMusic("playing"); ramp(video, level(), MUSIC_IN_MS); }
  });
  audio.addEventListener("playing", () => {
    if (media !== audio || stats.music !== "starting") return;
    music = true;
    setMusic("playing");
    ramp(audio, level(), MUSIC_IN_MS);
  });
  const onTime = (elm) => () => {
    if (media !== elm || !music || tail || (mode !== "av" && mode !== "audio")) return;
    const seconds = Number(info && info.seconds) || elm.duration || 0;
    if (seconds && seconds - elm.currentTime < MUSIC_TAIL_S) { tail = true; ramp(elm, 0, MUSIC_TAIL_S * 1000); }
  };
  video.addEventListener("timeupdate", onTime(video));
  audio.addEventListener("timeupdate", onTime(audio));
  // Ende: zurück zum Bild bzw. Stille, für diesen Eintrag nicht von vorn (erst wieder nach einem Wechsel)
  for (const elm of [video, audio]) {
    elm.addEventListener("ended", () => { if (media === elm) { done = stats.id; stop("ended"); } });
    elm.addEventListener("error", () => {
      if (media !== elm || !elm.getAttribute("src")) return;
      fail("media " + (elm.error ? elm.error.code : "?"));
    });
  }

  const busy = () => !!(media || restTimer || mode || stats.state === "loading" || stats.music === "loading");

  function arm() {
    clearTimeout(restTimer);
    restTimer = null;
    if (!wantsInfo(entry) || done === entry.id || (!videoAllowed() && !musicAllowed())) return;
    const target = entry;
    restTimer = setTimeout(() => { restTimer = null; if (entry === target) start(target); }, REST_MS);
  }

  // Zustand geändert (sichtbar, Einstellungen, Bildschirm, laufende Apps): stoppen, umschalten oder warten
  function sync() {
    if (mode) {
      const m = wanted(info);
      if (m !== mode) { stop(); if (m) arm(); return; }
      if (music && (mode === "av" || mode === "audio") && !rampTimer && !tail) media.volume = level();
      return;
    }
    if (!videoAllowed() && !musicAllowed()) { if (busy()) stop(); return; }
    if (!busy()) arm();
  }

  const unsub = store.subscribe(["visible", "ui", "trailers", "game_music", "background", "foreground", "saver", "running"], sync);
  // Bildschirmwechsel: Der Router setzt inert und hidden erst nach dem Ereignis
  const onScreen = () => setTimeout(sync, 0);
  document.addEventListener("couch-screen-change", onScreen);
  el.hidden = true;

  return {
    el,
    // Neuer Eintrag im Hub: das alte Video und der alte Ton weg, für den neuen die Ruhezeit abwarten
    setEntry(next) {
      if (entry && next && entry.id === next.id) { entry = next; return; }
      entry = next || null;
      done = "";
      stop();
      setState("idle");
      setMusic("idle");
      attach();
      arm();
      // Neuer Bildschirm (Spielseite): Er hängt erst nach create() im Stapel, vorher ist er nicht „oben“
      setTimeout(sync, 0);
    },
    sync,
    stop,
    get stats() {
      return { ...stats, mode, playing, music, time: (media && media.currentTime) || 0, width: video.videoWidth, height: video.videoHeight,
        seconds: (info && info.seconds) || 0 };
    },
    destroy() { unsub(); document.removeEventListener("couch-screen-change", onScreen); stop(); releaseVideo(); releaseAudio(); },
  };
}
