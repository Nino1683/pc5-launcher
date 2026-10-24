// Ladebild beim Start einer App (07 Abschnitt 6.6): Hintergrund des Spiels, Kachelbild in der
// Mitte, Titel, Phasentext des Kerns, Fortschritt (Prozent bei der Prefix-Kopie, sonst ein
// laufender Balken). Es steht, bis die App vorn ist, ein Fehler kommt oder der Start endet.
// Kreis bricht den Start nach einer Rückfrage ab (app.kill bricht auch Rückfrage und Kopie ab);
// bei der Übergabe an Steam gibt es nichts mehr abzubrechen.
//
// setupLaunchFlow() verbindet die Ereignisse app.launching und app.error mit diesem Bildschirm
// und dem Fehlerdialog (aufgerufen aus main.js).
import { h, icon } from "../core/dom.js";
import { call, on } from "../core/bridge.js";
import { router } from "../core/router.js";
import { sound } from "../core/sound.js";
import { store } from "../core/store.js";
import { S, fmt } from "../core/strings.js";
import { artBox } from "../components/art.js";
import { confirm } from "../components/dialog.js";
import { showAppError, isHandover, sameSentence } from "../components/entry-actions.js";

const END_PHASES = ["done", "visible", "failed", "cancelled"];

function create(params) {
  const entry = params.entry || { id: params.id, title: params.title || "", art: {} };
  const hero = entry.art && entry.art.hero;
  const titleText = fmt(S.launching, { title: entry.title });
  const text = h("div.load-text.ell", { text: sameSentence(params.text, titleText) ? "" : params.text || "" });
  const bar = h("div.load-bar.progress.is-plain.is-busy", h("i"));
  const note = h("div.load-note", { hidden: true });
  const hint = h("div.hints.load-hint", h("div.hint", icon("btn-circle"), h("span", { text: S.launchHintCancel })));
  const el = h("div.loading", { dataset: { id: entry.id } },
    hero ? h("img.load-hero", { src: hero, alt: "", decoding: "async" }) : null,
    h("div.load-cover", artBox(entry, { withTitle: false })),
    h("div.load-title.clamp2", { text: titleText }),
    text, bar, note, hint);
  let phase = "";
  let asking = false;

  function setPhase(p) {
    phase = (p && p.phase) || phase;
    let words = (p && p.text) || (phase === "confirm" ? S.loadConfirm : "");
    // Der Kern meldet in der Phase „starting“ denselben Satz wie die Überschrift: nicht zweimal.
    // Eine zweite Zeile gibt es nur für Phasen mit eigenem Text (prepare, confirm, closing-other …).
    if (sameSentence(words, titleText)) words = "";
    if (words || phase !== "prepare") text.textContent = words;
    const percent = p && typeof p.percent === "number" ? Math.max(0, Math.min(100, p.percent)) : null;
    bar.classList.toggle("is-busy", percent === null);
    bar.style.setProperty("--p", percent === null ? 0 : percent / 100);
    // Übergabe an Steam: Steam übernimmt, die Konsole endet gleich; sagen, wie man zurückkommt.
    // Steam-Spiele: Steam startet unsichtbar, beim ersten Mal dauert das (E57).
    const handover = phase === "handover";
    const steamGame = !handover && entry.source === "steam";
    note.hidden = !(handover || steamGame);
    if (handover) note.textContent = S.loadSteamApp;
    else if (steamGame) note.textContent = S.loadSteamGame;
    hint.hidden = handover;
  }

  async function cancel() {
    if (asking || phase === "handover") return;
    asking = true;
    const yes = await confirm({ title: S.cancelLaunchTitle, text: fmt(S.cancelLaunchText, { title: entry.title }), ok: S.cancelLaunchOk });
    asking = false;
    if (!yes) return;
    try { await call("app.kill", { id: entry.id }); } catch (e) { /* läuft schon nicht mehr: der Kern meldet sich über app.changed */ }
    // Das Ladebild schließt in jedem Fall: niemand soll hier festsitzen
    if (params.onCancel) params.onCancel();
  }

  if (params.phase) setPhase(params.phase);
  else if (isHandover(entry)) setPhase({ phase: "", text: params.text });
  return {
    el,
    id: entry.id,
    setPhase,
    onPad(button, ev) {
      if (button === "circle" && !ev.repeat) cancel();
      return true;
    },
  };
}

// Start einer App: Ladebild, bis sie vorn ist; Fehler und unerwartetes Ende als genau ein Dialog
export function setupLaunchFlow() {
  let launching = null;       // {id, inst, pending, seen}

  async function entryFor(id) {
    try { return (await call("lib.get", { id })).entry; } catch (e) { return { id, title: id, art: {} }; }
  }
  function end() {
    if (!launching) return;
    launching = null;
    router.close("loading");
  }

  on("app.launching", async (p) => {
    if (!p || !p.id) return;
    if (END_PHASES.includes(p.phase)) { if (launching && launching.id === p.id) end(); return; }
    if (launching && launching.id === p.id) { if (launching.inst) launching.inst.setPhase(p); else launching.pending = p; return; }
    if (launching) end();
    const mine = { id: p.id, inst: null, pending: p };
    launching = mine;
    const entry = await entryFor(p.id);
    if (launching !== mine) return;
    sound.play("launch");
    mine.inst = router.go("loading", { entry, phase: mine.pending, onCancel: () => { if (launching === mine) end(); } });
  });
  on("app.error", (p) => {
    if (launching && (!p.id || launching.id === p.id)) end();
    sound.play("error");
    showAppError(p || {});
  });
  // Die App ist vorn (oder schon wieder beendet): das Ladebild hat seinen Dienst getan
  store.subscribe(["foreground", "running"], (state, changed) => {
    if (!launching) return;
    if (state.foreground === launching.id) { launching.seen = true; end(); return; }
    // Eine Instanz, die gerade beendet wird, zählt nicht: Der Eintrag startet danach neu
    const runs = store.isRunning(launching.id) && !store.isClosing(launching.id);
    if (runs) launching.seen = true;
    else if (launching.seen && changed.includes("running")) end();
  });
  // Übergabe an Steam, von der Seite angekündigt (components/entry-actions.js): Das Ladebild steht,
  // bevor der Kern das Home beendet. Kommt der Start nicht zustande, schließt es wieder.
  document.addEventListener("couch-handover", (e) => {
    const entry = e.detail && e.detail.entry;
    if (!entry || !entry.id) return;
    if (launching && launching.id === entry.id && launching.inst) { launching.inst.setPhase({ phase: "handover" }); return; }
    if (launching) end();
    const mine = { id: entry.id, inst: null, pending: null };
    launching = mine;
    sound.play("launch");
    mine.inst = router.go("loading", { entry, phase: { phase: "handover", text: "" }, onCancel: () => { if (launching === mine) end(); } });
  });
  document.addEventListener("couch-launch-end", (e) => {
    if (launching && e.detail && launching.id === e.detail.id) end();
  });
  return { get active() { return !!launching; } };
}

export default { id: "loading", create };
