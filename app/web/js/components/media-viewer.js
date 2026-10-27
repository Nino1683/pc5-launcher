// Trailer und Bilder im Vollbild (Store, E132). Links/Rechts blättert über Trailer und Bilder; Kreuz hält einen Trailer
// an bzw. spielt ihn weiter (auf einem Bild: zu), Kreis schließt, PS lang führt ohnehin ins Home (Schichten schließen
// beim Bildschirmwechsel). Ein Trailer läuft groß mit Ton als Strom des Kerns (game.trailer {appid, movie, full}),
// so laut wie die Bedientöne; mit ?mute=1 (Prüfläufe) stumm. Nichts liegt dabei auf der Platte oder als Blob im
// Speicher. Kommt in START_MS kein Bild oder steht die Zeit STALL_MS, wird er verworfen (Satz statt Bild); am Ende
// schließt die Ansicht. Seite unsichtbar oder eine App vorn: Die Ansicht schließt.
// items: [{kind: "video", name, load: () => Promise<Adresse | null>} | {kind: "image", src}]
import { h, icon } from "../core/dom.js";
import { sound } from "../core/sound.js";
import { store } from "../core/store.js";
import { S, fmt } from "../core/strings.js";
import { openLayer } from "./layer.js";

const START_MS = 10000;
const STALL_MS = 8000;

export function mediaViewer({ items, start = 0 }) {
  const total = Array.isArray(items) ? items.length : 0;
  if (!total) return null;
  let i = Math.max(0, Math.min(total - 1, Number(start) || 0));
  let serial = 0;
  let guard = null;
  const img = h("img.ex-image.is-fill", { alt: "" });
  const video = h("video.ex-image.is-fill.mv-video", { playsinline: true, preload: "auto", disablepictureinpicture: true });
  const state = h("div.mv-state", { hidden: true }, icon("play"));
  const cap = h("div.ex-caption.ell");
  const stage = h("div.mv-stage", img, video, state);
  // Prüfhilfe (window.__gameinfoShot): was die Ansicht gerade tut
  const stats = { i, kind: "", state: "idle", error: "" };

  function setState(s, error = "") {
    stats.state = s;
    stats.error = error;
    stage.dataset.state = s;
  }

  function volume() {
    const raw = Number(store.ui.soundVolume ?? 60);
    return Math.max(0, Math.min(100, Number.isFinite(raw) ? raw : 60)) / 100;
  }

  function releaseVideo() {
    clearInterval(guard);
    guard = null;
    video.pause();
    video.removeAttribute("src");
    video.load();                  // schließt die Verbindung zum Trailer-Dienst
  }

  function failed(why) {
    releaseVideo();
    setState("failed", why);
    video.hidden = true;
    state.hidden = true;
    cap.textContent = S.stTrailerFailed;
  }

  function watch(mine) {
    let last = -1;
    let since = performance.now();
    clearInterval(guard);
    guard = setInterval(() => {
      if (mine !== serial) { clearInterval(guard); return; }
      const now = performance.now();
      if (video.paused && stats.state === "paused") { since = now; return; }
      if (video.currentTime > last + 0.05) { last = video.currentTime; since = now; }
      if (now - since > (stats.state === "playing" ? STALL_MS : START_MS)) failed(stats.state === "playing" ? "stalled" : "start-timeout");
    }, 1000);
  }

  async function show() {
    const mine = ++serial;
    const it = items[i];
    stats.i = i;
    stats.kind = it.kind;
    releaseVideo();
    state.hidden = true;
    if (it.kind === "video") {
      img.hidden = true;
      img.removeAttribute("src");
      video.hidden = false;
      cap.textContent = fmt(S.stTrailerOf, { name: it.name || S.stTrailer, n: i + 1, total });
      setState("loading");
      let url = null;
      try { url = await it.load(); } catch (e) { url = null; }
      if (mine !== serial) return;
      if (!url) { failed("none"); return; }
      video.muted = sound.muted;
      video.volume = volume();
      video.src = url;
      setState("starting");
      watch(mine);
      video.play().catch(() => { if (mine === serial) setTimeout(() => { if (mine === serial) video.play().catch(() => failed("play")); }, 300); });
      return;
    }
    setState("image");
    video.hidden = true;
    img.hidden = false;
    img.src = it.src;
    cap.textContent = fmt(S.stImageOf, { n: i + 1, total });
  }

  video.addEventListener("playing", () => { if (stats.kind === "video") { setState("playing"); state.hidden = true; } });
  video.addEventListener("pause", () => { if (stats.state === "playing") { setState("paused"); state.hidden = false; } });
  video.addEventListener("ended", () => { if (stats.kind === "video" && video.getAttribute("src")) layer.close(); });
  video.addEventListener("error", () => { if (stats.kind === "video" && video.getAttribute("src")) failed("media " + (video.error ? video.error.code : "?")); });

  const layer = openLayer({
    panel: h("div.ex-viewer.mv-viewer", stage, cap), cls: "ex-viewer-layer",
    onPad(button, ev) {
      if ((button === "left" || button === "right") && total > 1) {
        i = (i + (button === "right" ? 1 : total - 1)) % total;
        sound.play("focus", { repeat: ev && ev.repeat });
        show();
        return true;
      }
      if (button === "cross") {
        if (stats.kind !== "video" || stats.state === "failed") { layer.close(); return true; }
        if (video.paused) video.play().catch(() => {}); else video.pause();
        return true;
      }
      return false;
    },
    onClose() { serial += 1; releaseVideo(); unsub(); },
  });
  // Seite nicht mehr zu sehen oder eine App vorn: Ton aus, Ansicht zu
  const unsub = store.subscribe(["visible", "foreground"], () => {
    if (store.visible === false || store.state.foreground) layer.close();
  });
  layer.stats = stats;
  layer.video = video;
  show();
  return layer;
}
