// Auswahl eines Start- bzw. Ruhemodus-Videos aus der Community-Sammlung steamdeckrepo.com (E130). Bildschirm
// boot-videos {kind: "boot"|"suspend"}, geöffnet aus Einstellungen › Home-Bildschirm (settings/home.py). Angemeldet von
// components/intro.js (main.js lädt es immer). Raster aus fünf Spalten 16:10 (die Videos sind fürs Steam Deck gemacht)
// mit Vorschaubild, Titel, Länge, Downloads und Ersteller; Sortierung Beliebt (meiste Downloads) oder Neu. Ruht der
// Fokus PREVIEW_MS auf einem Video, spielt seine Vorschau einmal stumm in der Kachel (nicht bei „Bewegung reduzieren“).
//   Kreuz      dieses Video laden und verwenden (video.choose; Fortschritt über video.progress)
//   Dreieck    Sortierung wechseln (auch L1/R1)      Kreis   zurück
// Die Liste kommt seitenweise vom Kern (video.list, zwölf je Seite); die nächste Seite, sobald der Fokus in der
// letzten Reihe steht. Vorschaubilder und Vorschauvideos lädt die Seite direkt von cdn.steamdeckrepo.com (der Kern hat
// die Adressen geprüft); das Video selbst lädt nur der Kern.
import { h, icon } from "../core/dom.js";
import { call } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { router } from "../core/router.js";
import { sound } from "../core/sound.js";
import { store } from "../core/store.js";
import { S, fmt } from "../core/strings.js";
import { toast } from "../components/toast.js";

const COLS = 5;
const ROWS_SHOWN = 3;
const PREVIEW_MS = 900;
const SORTS = ["popular", "new"];

function create(params = {}) {
  const kind = params.kind === "suspend" ? "suspend" : "boot";
  const state = { posts: [], sort: SORTS.includes(params.sort) ? params.sort : "popular", page: 0, more: true, loading: false,
    loaded: false, failed: "", chosen: "", top: 0, busy: "", stale: false };
  const titleEl = h("h1.flow-title.ell", { text: kind === "boot" ? S.bvTitleBoot : S.bvTitleSuspend });
  const subEl = h("div.flow-sub.ell");
  const sortLabel = h("span.btn-label");
  const sortBtn = h("div.btn.is-secondary.gal-filter", { "data-focus": true, "data-nav-down": ".bv-shot[data-focus-default]" },
    icon("sort"), sortLabel);
  const grid = h("div.gal-grid.bv-grid", { "data-scroll": true });
  const view = h("div.gal-view.bv-view", { "data-focus-group": true }, grid);
  const empty = h("div.gal-empty", { hidden: true });
  const hint = (glyph, text) => h("div.hint", glyph.startsWith("btn-") ? icon(glyph) : h("span.keycap", { text: glyph }), h("span", { text }));
  const hints = h("div.hints", hint("btn-cross", S.bvHintChoose), hint("btn-triangle", S.bvHintSort), hint("btn-circle", S.hintBack));
  const credit = h("div.bv-credit.ell", { text: S.bvCredit });
  const el = h("div.flow.gal.bv", titleEl, subEl, sortBtn, view, empty, credit, hints);
  let serial = 0;
  let closed = false;
  let previewTimer = null;
  let preview = null;             // {shot, video}

  const sortName = (id) => (id === "new" ? S.bvSortNew : S.bvSortPopular);

  function meta(post) {
    const parts = [fmt(S.bvSeconds, { n: Math.max(1, Math.round(Number(post.duration) || 0)) })];
    parts.push(fmt(S.bvDownloads, { n: (Number(post.downloads) || 0).toLocaleString(S.locale) }));
    if (post.author) parts.push(post.author);
    return parts.join(" · ");
  }

  function cell(post) {
    const img = h("img", { alt: "", decoding: "async" });
    img.addEventListener("load", () => img.parentElement.classList.add("has-img"));
    if (post.thumb) img.src = post.thumb;
    const bar = h("div.bv-bar", { hidden: true }, h("div.progress.is-plain", h("i")));
    const shot = h("div.gal-shot.bv-shot", { "data-focus": true },
      h("div.gal-pic", img, icon("film", "gal-ph")), h("span.gal-mark", { hidden: post.id !== state.chosen, text: S.bvChosen }), bar);
    shot.post = post;
    return h("div.gal-cell.bv-cell", shot,
      h("div.gal-cap", h("div.gal-cap-title.ell", { text: post.title }), h("div.gal-cap-date.ell", { text: meta(post) })));
  }

  function paintSub() {
    if (state.failed) subEl.textContent = state.failed;
    else if (!state.loaded) subEl.textContent = S.setLoading;
    else subEl.textContent = fmt(S.bvSub, { sort: sortName(state.sort), n: state.posts.length }) + (state.stale ? " · " + S.bvStale : "");
    sortLabel.textContent = S.bvSort + ": " + sortName(state.sort);
  }

  // Die Reihe des Fokus bleibt in den drei voll sichtbaren; in der letzten geladenen Reihe kommt die nächste Seite
  function scrollTo(shot) {
    const shots = [...grid.querySelectorAll(".bv-shot")];
    const index = shot ? shots.indexOf(shot) : -1;
    if (index < 0) state.top = 0;
    else {
      const row = Math.floor(index / COLS);
      if (row < state.top) state.top = row;
      else if (row > state.top + ROWS_SHOWN - 1) state.top = row - ROWS_SHOWN + 1;
      if (row >= Math.floor((shots.length - 1) / COLS) && state.more && !state.loading) load(false);
    }
    grid.style.setProperty("--top", state.top);
  }

  async function load(fresh) {
    const mine = fresh ? ++serial : serial;
    if (fresh) { state.posts = []; state.page = 0; state.more = true; state.failed = ""; state.loaded = false; grid.replaceChildren(); paintSub(); }
    if (!state.more) return;
    state.loading = true;
    let r = null;
    try { r = await call("video.list", { kind, sort: state.sort, page: state.page + 1 }); } catch (e) { r = { ok: false, message: e && e.message }; }
    if (mine !== serial || closed) return;
    state.loading = false;
    state.loaded = true;
    if (!r || r.ok === false) {
      if (!state.posts.length) state.failed = (r && r.message) || S.errorGeneric;
      state.more = false;
    } else {
      state.page = r.page || state.page + 1;
      state.more = !!r.more;
      state.chosen = r.chosen || state.chosen;
      state.stale = !!r.stale;
      const known = new Set(state.posts.map((p) => p.id));
      const fresh2 = (r.posts || []).filter((p) => p && p.id && !known.has(p.id));
      state.posts.push(...fresh2);
      grid.append(...fresh2.map(cell));
      if (!grid.querySelector("[data-focus-default]") && grid.firstChild) grid.firstChild.firstChild.setAttribute("data-focus-default", "");
      [...grid.querySelectorAll(".bv-shot")].forEach((s, i) => { if (i < COLS) s.setAttribute("data-nav-up", ".gal-filter"); });
    }
    paintSub();
    empty.hidden = state.posts.length > 0;
    empty.textContent = state.failed || S.bvEmpty;
    const inside = nav.current && (grid.contains(nav.current) || nav.current === sortBtn);
    if (!inside || (nav.current === sortBtn && fresh && grid.firstChild)) {
      if (grid.firstChild) nav.focus(grid.firstChild.firstChild); else nav.focus(sortBtn);
    }
    nav.ensure();
  }

  // ---------------------------------------------------------------- Vorschau
  function stopPreview() {
    clearTimeout(previewTimer);
    previewTimer = null;
    if (!preview) return;
    const { video } = preview;
    try { video.pause(); } catch (e) { /* schon aus */ }
    video.removeAttribute("src");
    video.load();
    video.remove();
    preview = null;
  }

  function planPreview(shot) {
    stopPreview();
    const post = shot && shot.post;
    if (!post || !post.preview || store.ui.reduceMotion || !store.visible) return;
    previewTimer = setTimeout(() => {
      if (nav.current !== shot || closed || !store.visible || router.top.inst.el !== el) return;
      // Einmal abspielen, danach wieder das Vorschaubild: loop (Zurückspulen) hängte im unsichtbaren Gamescope den
      // Web-Prozess von WebKitGTK 2.54 (Blob mit WebM, gesehen 2026-10-27)
      const video = h("video.bv-preview", { muted: true, playsinline: true, preload: "auto", disablepictureinpicture: true });
      video.muted = true;
      video.addEventListener("playing", () => video.classList.add("is-on"));
      video.addEventListener("ended", () => { if (preview && preview.video === video) stopPreview(); });
      // Direkt von cdn.steamdeckrepo.com (kein fetch: die Seite dort erlaubt keine fremden Leser); geht es nicht, bleibt
      // das Vorschaubild
      video.addEventListener("error", () => { if (preview && preview.video === video) stopPreview(); });
      video.src = post.preview;
      shot.querySelector(".gal-pic").append(video);
      preview = { shot, video };
      video.play().catch(() => { if (preview && preview.video === video) stopPreview(); });
    }, PREVIEW_MS);
  }

  // ---------------------------------------------------------------- Wahl
  async function choose(shot) {
    const post = shot && shot.post;
    if (!post || state.busy) { if (state.busy) toast({ title: titleEl.textContent, body: S.bvBusy, icon: "info", sound: false }); return; }
    state.busy = post.id;
    const bar = shot.querySelector(".bv-bar");
    bar.hidden = false;
    bar.querySelector(".progress").classList.add("is-busy");
    let r = null;
    try { r = await call("video.choose", { kind, id: post.id }); } catch (e) { r = { ok: false, message: e && e.message }; }
    state.busy = "";
    bar.hidden = true;
    if (closed) return;
    if (!r || r.ok === false) {
      sound.play("error");
      toast({ title: post.title, body: (r && r.message) || S.errorGeneric, icon: "warn", sound: false });
      return;
    }
    state.chosen = post.id;
    for (const s of grid.querySelectorAll(".bv-shot")) s.querySelector(".gal-mark").hidden = s.post.id !== post.id;
    sound.play("select");
    toast({ title: kind === "boot" ? S.bvTitleBoot : S.bvTitleSuspend, body: r.message || "", icon: "check", sound: false });
    if (router.top && router.top.inst === api) router.back();
  }

  function setSort(id) {
    if (!SORTS.includes(id) || id === state.sort) { sound.play("edge"); return; }
    state.sort = id;
    sound.play("tab");
    stopPreview();
    load(true);
  }

  grid.addEventListener("nav-focus", (e) => {
    for (const s of grid.querySelectorAll("[data-focus-default]")) s.removeAttribute("data-focus-default");
    e.target.setAttribute("data-focus-default", "");
    scrollTo(e.target);
    planPreview(e.target);
  });
  grid.addEventListener("nav-blur", () => stopPreview());
  grid.addEventListener("press", (e) => choose(e.target.closest(".bv-shot")));
  sortBtn.addEventListener("press", () => setSort(state.sort === "popular" ? "new" : "popular"));

  function onPad(button, ev) {
    if (ev.repeat) return false;
    switch (button) {
      case "l1": setSort("popular"); return true;
      case "r1": setSort("new"); return true;
      case "triangle": setSort(state.sort === "popular" ? "new" : "popular"); return true;
      default: return false;
    }
  }

  // Fortschritt des Ladens (Kern: video.progress)
  function onEvent(event, payload) {
    if (event !== "video.progress" || !payload || payload.kind !== kind || payload.id !== state.busy) return;
    const shot = [...grid.querySelectorAll(".bv-shot")].find((s) => s.post.id === payload.id);
    if (!shot) return;
    const total = Number(payload.total) || 0;
    const bar = shot.querySelector(".progress");
    bar.classList.toggle("is-busy", !total);
    bar.style.setProperty("--p", total ? Math.min(1, (Number(payload.done) || 0) / total) : 0);
  }

  const api = {
    el, onPad, onEvent,
    onShow() { nav.ensure(); },
    onHide() { stopPreview(); },
    destroy() { closed = true; serial += 1; stopPreview(); },
  };
  paintSub();
  load(true);
  // Prüfhilfe: was die Auswahl gerade zeigt
  window.__bootVideos = { get state() { return { ...state, posts: state.posts.length }; }, get preview() { return preview ? preview.shot.post.id : ""; } };
  return api;
}

export const screens = [{ id: "boot-videos", create }];
