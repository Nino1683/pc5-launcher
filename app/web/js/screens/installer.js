// Archiv-Installer als geführter Ablauf (07 Abschnitt 11.6, Bauplan E16/E48):
//   installer          Einstieg: „Archiv auswählen“, laufende und unterbrochene Aufträge, Papierkorb
//   file-browser       Datei wählen (screens/file-browser.js)
//   install-check      Prüfen (install.probe): Name änderbar, Art, Größe, Platz, Kennwort,
//                      Startdatei, Hinweise, Kollision mit Namensvorschlag, „nicht unterstützt“
//   install-progress   Fortschritt (install.progress), läuft im Hintergrund weiter, wenn man geht;
//                      danach „Spielen“ / „Zur Bibliothek“ bzw. der Fehler mit Text
// E114: Auch ein Installationsprogramm (setup.exe, .msi) geht diesen Weg; solange es unsichtbar läuft,
// steht der Auftrag in „installing“ (ohne Prozentzahl, nur die Dauer).
// Alle install.*-Aufrufe antworten später; Fehlschläge stehen im Ergebnis (ok: false, code, message).
import { h, icon, sizeText } from "../core/dom.js";
import { call, on } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { router } from "../core/router.js";
import { store } from "../core/store.js";
import { S, fmt } from "../core/strings.js";
import { confirm, dialog } from "../components/dialog.js";
import { toast } from "../components/toast.js";
import { chooseExe, activateEntry } from "../components/entry-actions.js";
import { askText } from "../components/osk-input.js";
import { createListScreen } from "./file-browser.js";

const ACTIVE = ["queued", "running", "finishing", "installing", "needs-choice"];
const SETUP_FORMATS = ["exe", "msi"];      // Plan eines Installationsprogramms als Datei (E114)
const REMAINING_AFTER_S = 10;     // vorher schwankt die Schätzung der Restzeit zu stark

function durationText(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  return s < 60 ? fmt(S.secondsShort, { n: s }) : fmt(S.minutesSeconds, { m: Math.floor(s / 60), s: s % 60 });
}

function jobState(job) {
  switch (job.state) {
    case "queued": return S.insJobQueued;
    case "running": return Math.round(Number(job.percent) || 0) + " %";
    case "finishing": return S.insJobFinishing;
    case "installing": return S.insJobInstalling;
    case "needs-choice": return S.insJobChoice;
    case "done": return S.insJobDone;
    case "cancelled": return S.insJobCancelled;
    default: return S.insJobFailed;
  }
}

// Lange Pfade: die letzten beiden Ordner zählen (Spiele-Platte/Spielname)
function shortPath(path) {
  const parts = String(path || "").split("/").filter(Boolean);
  return parts.length > 2 ? "…/" + parts.slice(-2).join("/") : String(path || "");
}

const fail = (result) => toast({ title: S.insTitle, body: (result && result.message) || S.errorGeneric, icon: "warn", sound: "error" });

// Kandidaten der Startdatei eines Auftrags im Zustand needs-choice: install.setExe {jobId, exe}
async function chooseJobExe(job) {
  return chooseExe({ id: job.entryId || job.jobId || job.id, title: job.title }, { ref: job.jobId || job.id, candidates: job.candidates || [] });
}

// Datei wählen und prüfen: der Ablauf beginnt in der Dateiauswahl
export function pickArchive() {
  router.go("file-browser", { mode: "archive", onPick: (entry) => router.replace("install-check", { path: entry.path, name: entry.name }) });
}

// ---------------------------------------------------------------- Einstieg
function createRoot() {
  const screen = createListScreen({
    title: S.insTitle, sub: S.insSub,
    hints: [["btn-cross", S.select], ["btn-circle", S.hintBack]],
    handlers: { onPress: (row) => { press(row); return true; } },
  });
  let jobs = [];
  let trash = null;
  let serial = 0;

  function rows() {
    const out = [{ id: "choose", type: "action", label: S.insChoose, hint: S.insChooseHint, icon: "archive" }];
    const shown = jobs.filter((j) => ACTIVE.includes(j.state) || (j.finished && Date.now() / 1000 - j.finished < 3600));
    shown.forEach((job, i) => {
      const id = job.jobId || job.id;
      const row = ACTIVE.includes(job.state) && job.state !== "needs-choice"
        ? { id: "job:" + id, type: "progress", label: job.title, value: Number(job.percent) || 0, valueText: jobState(job), busy: job.state === "installing", job }
        : { id: "job:" + id, type: job.state === "needs-choice" ? "action" : "info", label: job.title, value: jobState(job), note: job.state === "failed" ? job.message : "", job };
      if (i === 0) row.section = S.insJobs;
      out.push(row);
    });
    const n = trash ? (trash.items || []).length : 0;
    out.push({ id: "trash", type: "page", label: S.insTrash, value: trash ? (n === 0 ? S.insTrashEmpty : n === 1 ? S.insTrashOne : fmt(S.insTrashCount, { n })) : "", icon: "trash", section: shown.length ? " " : "" });
    return out;
  }

  async function load() {
    const mine = ++serial;
    try { jobs = ((await call("install.jobs")) || {}).jobs || []; } catch (e) { jobs = []; }
    try { trash = (await call("install.trashList")) || null; } catch (e) { trash = null; }
    if (mine !== serial) return;
    screen.list.setRows(rows());
    nav.ensure();
  }

  function press(row) {
    if (!row) return;
    if (row.id === "choose") pickArchive();
    else if (row.id === "trash") router.go("trash");
    else if (row.job) {
      if (row.job.state === "needs-choice") chooseJobExe(row.job).then(load);
      else if (ACTIVE.includes(row.job.state)) router.go("install-progress", { jobId: row.job.jobId || row.job.id, title: row.job.title });
    }
  }

  const offs = [on("install.progress", (p) => {
    const job = jobs.find((j) => (j.jobId || j.id) === p.jobId);
    if (job && job.state === p.state) { Object.assign(job, p); screen.list.setRows(rows()); } else load();
  }), on("install.done", load), on("install.purgeDone", load)];
  load();
  return { el: screen.el, onShow({ back } = {}) { if (back) load(); nav.ensure(); }, destroy() { offs.forEach((off) => off()); } };
}

// ---------------------------------------------------------------- Prüfen
function createCheck(params) {
  const button = h("div.btn.flow-main", { "data-focus": true, "data-focus-default": true }, h("span.btn-label", { text: S.insStart }));
  const screen = createListScreen({
    title: S.insCheckTitle, sub: params.name || "",
    hints: [["btn-cross", S.select], ["btn-circle", S.hintBack]],
    side: button,
    handlers: {
      onSet: (row, value) => { set(row, value); return Promise.resolve(); },
      onText: (row) => askRow(row),
      emptyText: S.insProbing,
    },
  });
  let plan = null;
  const choices = {};
  const first = { done: false };
  let busy = false;

  // Das Archiv-Kennwort geht verdeckt durch die Tastatur: keine Vorschläge, nichts gelernt (E95, E99)
  async function askRow(row) {
    try { return await askText({ title: row.label, initial: row.value || "", secret: row.secret }); } catch (e) { return null; }
  }

  function set(row, value) {
    if (row.id === "title") choices.title = String(value).trim() || plan.title;
    else if (row.id === "exe") choices.exe = value;
    else if (row.id === "password") { choices.password = value; probe(); return; }
    paint();
  }

  function rows() {
    const out = [];
    if (!plan) return out;
    const unsupported = plan.supported === false || plan.status === "unsupported" || plan.status === "outside-roots";
    const target = plan.target || {};
    const space = plan.space || {};
    if (!unsupported) out.push({ id: "title", type: "text", label: S.insName, value: choices.title || plan.title || "", hint: S.insNameHint });
    if (plan.kind_label) out.push({ id: "kind", type: "info", label: S.insKind, value: plan.kind_label });
    if (plan.unpacked_bytes) out.push({ id: "size", type: "info", label: S.insSize, value: sizeText(plan.unpacked_bytes) });
    if (plan.installed_bytes) out.push({ id: "installed", type: "info", label: S.insSizeInstalled, value: sizeText(plan.installed_bytes) });
    if (target.dir) {
      const folder = choices.folder || target.name;
      const dir = choices.folder && target.parent ? target.parent + "/" + choices.folder : target.dir;
      out.push({ id: "target", type: "info", label: S.insTarget, value: shortPath(dir), note: target.exists && target.suggestion ? fmt(S.insFolderTaken, { name: target.name, suggestion: folder }) : "" });
    }
    if (space.free_bytes !== undefined && space.free_bytes !== null) {
      out.push({ id: "space", type: "info", label: S.insFree, value: sizeText(space.free_bytes), enabled: space.ok !== false,
        note: space.ok === false ? fmt(S.insSpaceMissing, { n: sizeText(space.missing_bytes) }) : "" });
    }
    if (plan.needs_password || plan.status === "password-needed" || plan.status === "password-wrong") {
      out.push({ id: "password", type: "text", secret: true, label: S.insPassword, value: choices.password ? "•" : "",
        note: plan.status === "password-wrong" ? S.insPasswordWrong : choices.password ? S.insPasswordSet : S.insPasswordEmpty, hint: S.insPasswordHint });
    }
    const candidates = plan.candidates || [];
    if (candidates.length > 1) {
      out.push({ id: "exe", type: "select", label: S.insExe, value: choices.exe || plan.exe || candidates[0].path, hint: S.insExeHint,
        options: candidates.map((c) => ({ value: c.path, label: c.path, note: (c.why || []).join(", ") })) });
    }
    (plan.problems || []).forEach((p, i) => out.push({ id: "problem:" + i, type: "info", label: p.message || plan.message, enabled: false, section: i === 0 ? S.insWarning : undefined }));
    if (!(plan.problems || []).length && plan.message) out.push({ id: "problem:msg", type: "info", label: plan.message, enabled: false, section: S.insWarning });
    // Installationsprogramm als Datei (E114): samt der .bin-Dateien daneben
    const setup = SETUP_FORMATS.includes(plan.format);
    const extra = Math.max(0, (plan.setup_files || []).length - 1);
    const name = plan.name || params.name || "";
    const fileText = !setup || !extra ? name : fmt(extra === 1 ? S.insSetupWithOne : S.insSetupWith, { name, n: extra });
    out.push({ id: "archive", type: "info", label: setup ? S.insSetupFile : S.insArchive, value: fileText, section: (plan.problems || []).length || plan.message ? " " : undefined });
    return out;
  }

  function paint() {
    const unsupported = plan && (plan.supported === false || plan.ready === false && plan.status !== "password-needed" && plan.status !== "password-wrong" && plan.status !== "target-exists");
    const file = plan && SETUP_FORMATS.includes(plan.format);
    screen.setTitle(plan && plan.supported === false ? (file ? S.insUnsupportedFile : S.insUnsupported) : S.insCheckTitle);
    button.querySelector(".btn-label").textContent = unsupported ? (file ? S.insOtherFile : S.insOther) : S.insStart;
    button.dataset.mode = unsupported ? "other" : "start";
    const blocked = !unsupported && plan && plan.space && plan.space.ok === false;
    button.toggleAttribute("data-disabled", !!blocked);
    button.classList.toggle("is-off", !!blocked);
    screen.list.setRows(rows());
    nav.ensure();
  }

  async function probe() {
    if (screen.list.setLoading) screen.list.setLoading();
    button.hidden = true;
    let result;
    try {
      const p = { path: params.path };
      if (choices.password) p.password = choices.password;
      result = await call("install.probe", p);
    } catch (e) {
      result = { supported: false, ready: false, status: "error", message: (e && e.message) || S.insProbeFailed, problems: [] };
    }
    plan = result || { supported: false, message: S.insProbeFailed };
    if (plan.target && plan.target.exists && plan.target.suggestion) choices.folder = plan.target.suggestion;
    button.hidden = false;
    paint();
    // Der schnelle Weg: Kreuz, Kreuz (prüfen, installieren); alles andere liegt darüber
    if (!first.done) { first.done = true; nav.focus(button); } else if (!nav.current || !screen.el.contains(nav.current)) nav.focus(button);
  }

  async function start() {
    if (busy || !plan) return;
    if (button.dataset.mode === "other") { router.replace("file-browser", { mode: "archive", onPick: (entry) => router.replace("install-check", { path: entry.path, name: entry.name }) }); return; }
    if (button.hasAttribute("data-disabled")) return;
    busy = true;
    const here = router.top;
    try {
      let result = await call("install.start", { plan, choices });
      // Den Ordner gibt es inzwischen: den Vorschlag des Kerns nehmen und es noch einmal versuchen
      if (result && result.ok === false && result.code === "target-exists" && result.suggestion && choices.folder !== result.suggestion) {
        choices.folder = result.suggestion;
        paint();
        toast({ title: S.insTitle, body: fmt(S.insFolderTaken, { name: (plan.target || {}).name || plan.title, suggestion: result.suggestion }), icon: "info", sound: false });
        result = await call("install.start", { plan, choices });
      }
      if (!result || result.ok === false) { await dialog({ title: S.insUnsupported, text: (result && result.message) || S.errorGeneric, buttons: [{ id: "ok", label: S.ok }] }); return; }
      // Kreis während des Wartens: nicht den Bildschirm darunter (im schlimmsten Fall das Home) ersetzen
      if (router.top === here) router.replace("install-progress", { jobId: result.jobId, title: choices.title || plan.title });
      else toast({ title: choices.title || plan.title, body: S.insBackgroundToast, icon: "info", sound: false });
    } catch (e) {
      fail(e);
    } finally {
      busy = false;
    }
  }
  button.addEventListener("press", start);

  probe();
  return { el: screen.el, onShow() { nav.ensure(); } };
}

// ---------------------------------------------------------------- Fortschritt, Fertig, Fehler
function createProgress(params) {
  const jobId = params.jobId;
  const title = h("h1.flow-title.ell", { text: fmt(S.insProgTitle, { title: params.title || "" }) });
  const percent = h("div.prog-percent", { text: "0 %" });
  const bar = h("div.progress.prog-bar", h("i"));
  const file = h("div.prog-file.ell");
  const elapsed = h("div.prog-time");
  const message = h("div.prog-message", { hidden: true });
  const actions = h("div.prog-actions");
  const box = h("div.prog", percent, bar, file, elapsed, message, actions);
  const el = h("div.flow.is-progress", title, box);
  let job = { jobId, title: params.title || "", percent: 0, state: "queued", elapsed: 0 };
  let timer = null;
  let asked = false;
  let finished = false;

  function btn(label, fn, secondary) {
    const b = h("div.btn" + (secondary ? ".is-secondary" : ""), { "data-focus": true }, h("span.btn-label", { text: label }));
    b.addEventListener("press", fn);
    return b;
  }

  function running() {
    actions.replaceChildren(
      btn(S.insBackground, () => { toast({ title: job.title, body: S.insBackgroundToast, icon: "download", sound: false }); router.back(); }),
      btn(S.cancel, cancel, true));
    nav.focus(actions.firstChild);
  }

  function paint() {
    const p = Math.max(0, Math.min(100, Math.round(Number(job.percent) || 0)));
    // Ein Installationsprogramm meldet keinen Anteil (E114): nur „Wird installiert …“ und die Dauer
    const installing = job.state === "installing";
    percent.textContent = job.state === "queued" ? S.insJobQueued : installing ? S.insJobInstalling : p + " %";
    bar.style.setProperty("--p", p / 100);
    bar.classList.toggle("is-busy", job.state === "queued" || installing);
    file.textContent = job.state === "queued" ? S.insProgQueued : job.state === "finishing" ? S.insJobFinishing
      : installing ? S.insProgInstalling : job.file || "";
    elapsed.textContent = timeText();
    if (job.state === "needs-choice" && !asked) {
      asked = true;
      chooseJobExe(job).then((ok) => { if (!ok) asked = false; });
    }
  }

  // Laufzeit und, sobald die Schätzung trägt (nach 10 s und ab 2 %), die Restzeit (07 11.6)
  function timeText() {
    const spent = Number(job.elapsed) || 0;
    const p = Math.max(0, Math.min(100, Number(job.percent) || 0));
    const parts = [fmt(S.insElapsed, { t: durationText(spent) })];
    if (job.state === "running" && spent >= REMAINING_AFTER_S && p >= 2 && p < 100) {
      parts.push(fmt(S.insRemaining, { t: durationText(spent * (100 - p) / p) }));
    }
    return parts.join(" · ");
  }

  // Die Dauer zählt die Seite zwischen zwei Ereignissen selbst weiter (einmal je Sekunde, nur hier)
  function tickTime() {
    clearInterval(timer);
    timer = setInterval(() => {
      if (finished || (job.state !== "running" && job.state !== "installing")) return;
      job.elapsed = (job.elapsed || 0) + 1;
      elapsed.textContent = timeText();
    }, 1000);
  }

  function done(p) {
    finished = true;
    clearInterval(timer);
    const ok = p.ok && p.state !== "cancelled";
    title.textContent = fmt(ok ? S.insDoneTitle : S.insFailedTitle, { title: p.title || job.title });
    percent.hidden = true;
    bar.hidden = true;
    file.hidden = true;
    elapsed.hidden = true;
    message.hidden = false;
    message.textContent = p.message || "";
    const list = [];
    if (ok && p.entryId) {
      let playing = false;
      list.push(btn(S.insPlay, async () => {
        if (playing) return;                     // zweites Kreuz: nicht zweimal zurück und starten
        playing = true;
        const here = router.top;
        let entry = null;
        try { entry = (await call("lib.get", { id: p.entryId })).entry; } catch (e) { entry = null; }
        if (router.top === here) router.back();
        if (entry) activateEntry(entry);
        playing = false;
      }));
    }
    // Zur Bibliothek: der Fokus steht dort auf dem neuen Spiel
    if (ok) list.push(btn(S.insToLibrary, () => { router.backTo("home"); router.go("library", { tab: "installed", focus: p.entryId || null }); }, !!p.entryId));
    else list.push(btn(S.ok, () => router.back()), btn(S.insOther, () => router.replace("file-browser", { mode: "archive", onPick: (entry) => router.replace("install-check", { path: entry.path, name: entry.name }) }), true));
    actions.replaceChildren(...list);
    nav.focus(actions.firstChild);
  }

  async function cancel() {
    const yes = await confirm({ title: S.ccJobCancelTitle, text: fmt(S.ccJobCancelText, { title: job.title }), ok: S.ccJobCancelOk, cancel: S.ccJobKeep });
    if (!yes) return;
    try { const result = await call("install.cancel", { jobId }); if (result && result.ok === false) fail(result); } catch (e) { fail(e); }
  }

  const offs = [
    on("install.progress", (p) => {
      if (!p || p.jobId !== jobId || finished) return;
      Object.assign(job, p);
      paint();
    }),
    on("install.done", (p) => { if (p && p.jobId === jobId) done(p); }),
  ];
  // Ereignisse können vor der Antwort auf install.start gekommen sein: Stand nachfragen
  (async () => {
    try {
      const jobs = ((await call("install.jobs")) || {}).jobs || [];
      const known = jobs.find((j) => (j.jobId || j.id) === jobId);
      if (!known || finished) return;
      Object.assign(job, known);
      if (ACTIVE.includes(known.state)) paint();
      else done({ ...known, ok: known.state === "done" });
    } catch (e) { /* die Ereignisse genügen */ }
  })();
  running();
  paint();
  tickTime();
  return {
    el,
    onShow() { nav.ensure(); },
    destroy() { clearInterval(timer); offs.forEach((off) => off()); },
  };
}

export const screens = [
  { id: "installer", create: createRoot },
  { id: "install-check", create: createCheck },
  { id: "install-progress", create: createProgress },
];
export default screens[0];
