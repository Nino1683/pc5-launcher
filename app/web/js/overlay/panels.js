// Tafeln des Control Centers (07 Abschnitt 11.4). Jede Tafel ist eine Beschreibung für die
// allgemeine Zeilenliste (components/rows.js):
//   {empty, hints, keys (Schlüssel aus sys.state, bei deren Änderung neu gezeichnet wird),
//    events (Ereignisse, nach denen load neu läuft), load(), rows(),
//    onSet(row, value), onAction(row), onPress(row, node) -> true, onPad(button, row) -> true}
// ctx: {api (schließt das Control Center), entries (Map entryId -> EntryView), fail(e), note(text, title)}
import { h, sizeText } from "../core/dom.js";
import { call, on } from "../core/bridge.js";
import { store } from "../core/store.js";
import { S, fmt } from "../core/strings.js";
import { confirm } from "../components/dialog.js";
import { openPicker } from "../components/picker.js";
import { MOUSE_KEYS } from "./notices.js";

const ACTIVE_JOBS = new Set(["queued", "running", "finishing", "installing"]);   // installing: Installationsprogramm (E114)

// Kandidaten für die Startdatei kommen mit install.progress {state: "needs-choice", candidates}
// und mit install.jobs; gemerkt wird das Ereignis, falls die Tafel erst später aufgeht
const exeChoices = new Map();
on("install.progress", (p) => {
  if (p && p.jobId && p.state === "needs-choice" && Array.isArray(p.candidates)) exeChoices.set(p.jobId, p.candidates);
});

export function jobActive(job) { return !!job && ACTIVE_JOBS.has(job.state); }

export function jobText(job) {
  switch (job.state) {
    case "running": return fmt(S.jobRunning, { n: Math.round(Number(job.percent) || 0) });
    case "finishing": return S.jobFinishing;
    case "installing": return S.jobInstalling;
    case "needs-choice": return S.jobNeedsChoice;
    case "done": return S.jobDone;
    case "failed": return S.jobFailed;
    case "cancelled": return S.jobCancelled;
    default: return S.jobQueued;
  }
}

// Downloads der Konsole (E59, E60, sys.state.downloads): Das Home lädt, was der Browser übergibt
export function downloadActive(d) { return !!d && (d.state === "running" || d.state === "queued"); }
export function downloadText(d) {
  switch (d.state) {
    case "running": {
      const parts = [d.total > 0 ? fmt(S.dlRunning, { n: d.percent, got: sizeText(d.received), total: sizeText(d.total) })
        : sizeText(d.received || 0) || S.dlStarting];
      if (d.speed > 0) parts.push(fmt(S.dlSpeed, { v: sizeText(d.speed) }));
      // Restzeit wie bei den Store-Downloads (Wunsch 2026-10-24)
      const left = Number(d.total) - Number(d.received);
      if (d.speed > 0 && d.total > 0 && left > 0) parts.push(fmt(S.stDlLeft, { t: etaText(left / d.speed) }));
      return parts.join(" · ");
    }
    case "queued": return S.dlQueued;
    case "paused": return d.total > 0 ? fmt(S.dlPaused, { n: d.percent }) : S.dlPausedPlain;
    case "done": return S.jobDone;
    case "failed": return S.jobFailed;
    default: return S.jobCancelled;
  }
}
// Zeile unter einem Download in der Tafel: Der Anteil steht schon am Balken (2026-10-24)
function downloadDetail(d) {
  if (d.state === "paused") return S.dlPausedPlain;
  if (d.state !== "running" || !(d.total > 0)) return downloadText(d);
  const parts = [fmt(S.stDlAmount, { got: sizeText(d.received) || "0 B", total: sizeText(d.total) })];
  if (d.speed > 0) parts.push(fmt(S.dlSpeed, { v: sizeText(d.speed) }));
  const left = Number(d.total) - Number(d.received);
  if (d.speed > 0 && left > 0) parts.push(fmt(S.stDlLeft, { t: etaText(left / d.speed) }));
  return parts.join(" · ");
}
// Unter einem Store-Download (E99): woher, wie viel, wie schnell, wie lange noch
// (itch.io steht im eigenen Block der Texte, E100)
const DL_NOTES = { ...S.stDlNotes, itch: S.stItchDlNote, setup: S.setupDlNote };   // setup: Einrichtungs-Assistent (E105)
function etaText(seconds) {
  const m = Math.max(1, Math.ceil((Number(seconds) || 0) / 60));
  return m < 60 ? fmt(S.stEtaMin, { m }) : fmt(S.stEtaHours, { h: Math.floor(m / 60), m: m % 60 });
}
// Proton-Aufträge (E101/E102) laufen über dieselbe Liste (store "proton", kind install | repair)
const isProton = (j) => !!j && j.store === "proton";
function storeJobNote(j) {
  const parts = [isProton(j) ? (j.kind === "repair" ? S.protonDlRepair : S.protonDlInstall) : DL_NOTES[j.store] || S.stDlNote];
  if (j.total > 0) parts.push(fmt(S.stDlAmount, { got: sizeText(j.done) || "0 B", total: sizeText(j.total) }));
  if (j.speed > 0) parts.push(fmt(S.dlSpeed, { v: sizeText(j.speed) }));
  if (j.eta > 0) parts.push(fmt(S.stDlLeft, { t: etaText(j.eta) }));
  return parts.join(" · ");
}

// Was Kreuz auf einem Download anbietet (Auswahlliste), je nach Zustand
// Windows-Programm (E93): „Starten“ läuft über den Startweg des Home (ui.open launch)
// Installationsprogramm (E114, d.installer: der Name sagt es): „Installieren“ zuerst, „Starten“ bleibt dahinter
const isExe = (d) => /\.exe$/i.test(String(d.name || ""));

// Downloads, die WebKit selbst lädt (local, E99): Anhalten, Fortsetzen, Abbrechen und Neu laden kann
// nur die Konsole bei ihren eigenen. Läuft er noch, gibt es nichts zu wählen (Hinweis statt Liste);
// gescheitert oder abgebrochen bleibt nur „Entfernen“, fertig wie jeder andere
function downloadChoices(d) {
  if (d.local === true) {
    if (d.state === "failed" || d.state === "cancelled") return [{ value: "remove", label: S.dlRemove }];
    if (d.state !== "done") return [];
  }
  switch (d.state) {
    case "running": case "queued": return [{ value: "pause", label: S.dlPause }, { value: "cancel", label: S.dlCancel }];
    case "paused": return [{ value: "resume", label: S.dlResume }, { value: "cancel", label: S.dlCancel }];
    case "failed": return [{ value: "resume", label: S.dlRetry }, { value: "remove", label: S.dlRemove }];
    case "cancelled": return [{ value: "resume", label: S.dlAgain }, { value: "remove", label: S.dlRemove }];
    default: return (d.archive || d.installer ? [{ value: "install", label: S.dlInstall }] : [])
      .concat(isExe(d) ? [{ value: "run", label: S.dlRun }] : [], [{ value: "remove", label: S.dlRemove }]);
  }
}

export function artJobText(job) {
  return fmt(S.ccArtJobValue, { done: Number(job.done) || 0, total: Number(job.total) || 0 });
}
export function artJobPercent(job) {
  const total = Number(job.total) || 0;
  return total > 0 ? Math.max(0, Math.min(100, (Number(job.done) || 0) * 100 / total)) : 0;
}

// "gerade eben", "vor 5 Min.", "vor 3 Std." oder das Datum
function agoText(time) {
  const t = Number(time);
  if (!Number.isFinite(t) || t <= 0) return "";
  const seconds = Date.now() / 1000 - t;
  if (seconds < 90) return S.agoNow;
  if (seconds < 3600) return fmt(S.agoMinutes, { n: Math.round(seconds / 60) });
  if (seconds < 86400) return fmt(S.agoHours, { n: Math.round(seconds / 3600) });
  return new Date(t * 1000).toLocaleDateString(S.locale, { day: "numeric", month: "long" });
}

export const PANELS = {
  // ---------------------------------------------------------------- laufende Spiele und Apps
  switcher: (ctx) => ({
    empty: S.ccSwitcherEmpty,
    hints: [["btn-cross", S.ccSwitchTo], ["btn-square", S.ccCloseApp]],
    keys: ["running", "foreground"],
    rows: () => (store.state.running || []).map((run) => {
      const front = run.entryId === store.state.foreground;
      return {
        id: run.entryId, type: "action", label: run.title, closing: !!run.closing, front,
        note: run.closing ? S.closing : run.launching ? S.ccStarting : front ? S.ccInFront : (run.frozen ? S.ccPaused : S.ccRunning),
        art: ctx.entries.get(run.entryId) || { id: run.entryId, title: run.title, kind: run.kind === "game" || run.kind === "emulator" ? "game" : "app", art: {} },
      };
    }),
    onAction: async (row) => {
      if (row.closing) return;
      // Vorn: nur das Control Center schließen (zurück ins Spiel). Sonst: nach vorn holen.
      if (!row.front) { try { await call("app.focus", { id: row.id }); } catch (e) { ctx.fail(e); return; } }
      ctx.api.close();
    },
    onPad: (button, row) => {
      if ((button !== "square" && button !== "options") || !row) return false;
      if (row.closing) return true;
      confirm({ title: fmt(S.closeAppTitle, { title: row.label }), text: fmt(S.closeGameText, { title: row.label }), ok: S.close })
        .then((yes) => { if (yes) call("app.close", { id: row.id }).catch(ctx.fail); });
      return true;
    },
  }),

  // ---------------------------------------------------------------- Mitteilungen
  notifications: (ctx) => {
    let items = [];
    const clear = async () => {
      try { await call("notify.clear"); items = []; } catch (e) { ctx.fail(e); }
    };
    return {
      empty: S.ccNotifyEmpty,
      hints: [["OPTIONS", S.ccNotifyClear]],
      keys: ["notifications"],
      // Kommt eine neue dazu, während die Liste offen ist: neu holen (das Holen zählt als gelesen)
      reloadOn: (state) => (state.notifications || 0) > 0,
      load: async () => { items = ((await call("notify.list")) || {}).items || []; },
      rows: () => [
        ...items.map((n) => ({
          id: "n:" + n.id, type: "info", label: n.title || n.body || "", note: n.title ? (n.body || "") : "",
          value: agoText(n.time), icon: n.icon && document.getElementById(n.icon) ? n.icon : "bell",
        })),
        ...(items.length ? [{ id: "clear", type: "action", label: S.ccNotifyClear, icon: "trash" }] : []),
      ],
      onAction: clear,
      onPad: (button) => {
        if (button !== "options" || !items.length) return false;
        clear().then(ctx.refresh);
        return true;
      },
    };
  },

  // ---------------------------------------------------------------- Installationen, Bilder laden
  downloads: (ctx) => {
    let known = [];               // letzte Antwort von install.jobs (auch erledigte der letzten Zeit)
    // Nur was läuft oder eine Wahl braucht. Erledigte Installationen stehen nicht hier (Wunsch
    // 2026-10-18: ließen sich nicht entfernen); ihr Ergebnis kommt als Mitteilung (wiring_install._done)
    const merged = () => {
      const live = store.state.jobs || [];
      const out = known.map((job) => ({ ...job, ...(live.find((j) => j.id === job.id) || {}) }));
      for (const job of live) if (!out.some((j) => j.id === job.id)) out.unshift(job);
      for (const job of out) if (!(job.candidates && job.candidates.length) && exeChoices.has(job.id)) job.candidates = exeChoices.get(job.id);
      // Was noch läuft oder eine Wahl braucht, steht oben
      const rank = (j) => (j.state === "needs-choice" ? 0 : jobActive(j) ? 1 : 2);
      return out.filter((j) => rank(j) < 2).sort((a, b) => rank(a) - rank(b) || (Number(b.created) || 0) - (Number(a.created) || 0));
    };
    const cancel = (row) => {
      confirm({ title: S.ccJobCancelTitle, text: fmt(S.ccJobCancelText, { title: row.label }), ok: S.ccJobCancelOk, cancel: S.ccJobKeep })
        .then(async (yes) => {
          if (!yes) return;
          try {
            const result = await call("install.cancel", { jobId: row.id });
            if (result && result.ok === false) ctx.fail({ message: result.message });
          } catch (e) { ctx.fail(e); }
        });
    };
    // Kreuz auf einem Download (E60): anhalten, fortsetzen, abbrechen, entfernen, installieren
    const downloadMenu = async (row, node) => {
      const d = row.download;
      const options = downloadChoices(d);
      if (!options.length) { if (d.local === true) ctx.note(S.dlLocalBusyNote, d.name || S.unknown); return; }
      const what = await openPicker({ options, value: undefined, anchor: node, within: node.closest(".cc-panel-body") });
      if (what === undefined) return;
      if (what === "run") {
        // Eintrag in der Bibliothek (vorhanden oder neu, eigener Prefix), dann startet ihn das Home
        try {
          const r = await call("files.run", { path: d.path });
          if (!r || r.ok === false) { ctx.note((r && r.message) || S.dlFailed, d.name); return; }
          await call("ui.open", { screen: "launch", params: { id: r.id } });
        } catch (e) { ctx.fail(e); }
        return;
      }
      if (what === "install") {
        // Archiv oder Installationsprogramm: die Prüfung des Installers im Home (ui.open, wie „Spiel installieren“)
        call("ui.open", { screen: "install-check", params: { path: d.path, name: d.name } }).catch(ctx.fail);
        return;
      }
      try {
        const result = await call("downloads." + what, { id: d.id });
        if (result && result.ok === false) ctx.note(result.message || S.dlFailed, d.name);
      } catch (e) { ctx.fail(e); }
    };
    // Startdatei wählen (Auftrag im Zustand needs-choice): Kandidaten aus install.jobs
    const choose = async (row, node) => {
      const options = (row.job.candidates || []).map((c) => ({ value: c.path, label: c.path, note: (c.why || []).join(" · ") }));
      if (!options.length) { ctx.note(row.job.message || S.ccJobChoiceNone, row.label); return; }
      // In der Tafel unter ihrem Titel halten (mittig auf der Zeile verdeckte die Liste den Titel)
      const exe = await openPicker({ options, value: undefined, anchor: node, within: node.closest(".cc-panel-body") });
      if (exe === undefined) return;
      try {
        const result = await call("install.setExe", { jobId: row.id, exe });
        if (result && result.ok === false) ctx.fail({ message: result.message });
        else ctx.note(fmt(S.ccJobChoiceDone, { title: row.label }), S.ccDownloads);
      } catch (e) { ctx.fail(e); }
    };
    return {
      empty: S.ccDownloadsEmpty,
      keys: ["jobs", "art_job", "downloads", "store_jobs"],
      events: ["install.done", "install.purgeDone"],
      // install.jobs nennt auch, was die Zustandsliste nicht kennt (Kandidaten, Erledigtes)
      reloadOn: (state) => (state.jobs || []).some((j) => !known.some((k) => k.id === j.id) || j.state === "needs-choice"),
      load: async () => {
        try { known = ((await call("install.jobs")) || {}).jobs || []; } catch (e) { known = []; }
      },
      rows: () => {
        const rows = merged().map((job) => {
          const base = { id: job.id, label: job.title || S.unknown, job, icon: "archive" };
          if (job.state === "needs-choice") return { ...base, type: "action", value: jobText(job), hint: job.message || "" };
          return { ...base, type: "progress", value: Number(job.percent) || 0, valueText: jobText(job), busy: job.state !== "running", note: job.file || "" };
        });
        // Downloads des Browsers: laufende oben, fertige Archive lassen sich gleich installieren
        const dls = [...(store.state.downloads || [])].sort((a, b) => Number(downloadActive(b)) - Number(downloadActive(a)));
        for (const d of dls) {
          const base = { id: "dl:" + d.id, label: d.name || S.unknown, icon: "download", download: d };
          // Lädt WebKit selbst (local), steht „Lädt im Browser“ statt „Mit Kreuz fortsetzen“ (E99).
          // Rechts nur der Anteil: Der Dateiname braucht den Platz, alles Übrige steht darunter (2026-10-24)
          const pct = d.total > 0 ? fmt(S.dlPercent, { n: d.percent || 0 }) : "";
          const below = (origin) => [downloadDetail(d), origin].join(" · ");
          if (downloadActive(d)) rows.push({ ...base, type: "progress", value: d.percent || 0, valueText: pct, busy: !(d.total > 0) || d.state === "queued", note: below(d.local === true ? S.dlLocalNote : S.dlFromBrowser) });
          else if (d.state === "paused") rows.push({ ...base, type: "progress", value: d.percent || 0, valueText: pct, note: below(d.local === true ? S.dlLocalNote : S.dlPausedNote) });
          else if (d.state === "done" && (d.archive || d.installer)) rows.push({ ...base, type: "action", value: S.dlInstall, hint: d.path || "" });
          else if (d.state === "done" && isExe(d)) rows.push({ ...base, type: "action", value: S.dlRun, hint: d.path || "" });
          else rows.push({ ...base, type: "action", value: downloadText(d), hint: d.state === "done" ? d.path || "" : d.error || "" });
        }
        // Store (E68): Steam lädt unsichtbar, die Launcher (E94) nach Bestätigung; Kreuz bricht ab (mit Rückfrage)
        for (const j of [...(store.state.store_jobs || [])].reverse()) {
          rows.unshift({ id: "st:" + j.id, type: "progress", label: j.title || S.unknown, value: Number(j.percent) || 0,
            valueText: j.percent ? fmt(S.stJobPercent, { text: j.text, n: j.percent }) : j.text, busy: !j.percent, note: storeJobNote(j),
            icon: isProton(j) ? "update" : "store", storeJob: j,
            hint: isProton(j) ? fmt(j.kind === "repair" ? S.protonDlRepairHint : S.protonDlHint, { title: j.title || S.unknown }) : undefined });
        }
        const art = store.state.art_job;
        if (art) rows.unshift({ id: "_art", type: "progress", label: S.ccArtJob, value: artJobPercent(art), valueText: artJobText(art), icon: "download" });
        return rows;
      },
      // Kreuz: Startdatei wählen bzw. die laufende Installation abbrechen (mit Rückfrage);
      // das Laden der Bilder ist nur Anzeige
      onPress: (row, node) => {
        if (row.job && row.job.state === "needs-choice") choose(row, node);
        else if (row.job && jobActive(row.job)) cancel(row);
        else if (row.download) downloadMenu(row, node);
        else if (row.storeJob) {
          const j = row.storeJob;
          // E99: Eine Deinstallation läuft zu Ende; ein Update bricht anders ab als ein Download.
          // Eine Absage des Kerns (ok: false) blieb vorher stumm
          if (j.state === "removing") { ctx.note(S.stUninstallNoCancel, row.label); return true; }
          const update = j.kind === "update";
          // Proton: Geladenes bleibt für den nächsten Versuch liegen, nichts kommt in den Papierkorb
          // Einrichtung (E105, Rückfrage D2): Fertiges bleibt installiert, Vorgabe „Weiter installieren“
          const setup = j.store === "setup";
          const ask = isProton(j)
            ? (j.kind === "repair" ? [S.protonCancelRepairTitle, S.protonCancelRepairText] : [S.protonCancelTitle, S.protonCancelText])
            : setup ? [S.setupCancelTitle, S.setupCancelText]
            : (update ? [S.stCancelUpdateTitle, S.stCancelUpdateText] : [S.stCancelTitle, S.stCancelText]);
          confirm({ title: ask[0], text: fmt(ask[1], { title: row.label }), ok: setup ? S.setupCancelOk : S.stCancelOk, cancel: setup ? S.setupCancelKeep : S.stKeep })
            .then((yes) => (yes ? call("store.cancel", { store: j.store, id: j.id }) : null))
            .then((r) => { if (r && r.ok === false) ctx.note(r.message || S.errorGeneric, row.label); })
            .catch(ctx.fail);
        }
        return true;
      },
    };
  },

  // ---------------------------------------------------------------- Ton
  sound: (ctx) => {
    let audio = { level: 0, muted: false, sink: "", sinks: [], available: true };
    let loaded = false;
    return {
      keys: ["volume"],
      load: async () => { audio = { ...audio, ...((await call("audio.get")) || {}) }; loaded = true; },
      // Lautstärke oder Stumm von anderswo geändert (Fernbedienung, Einstellungen): mitziehen
      onState: (state) => {
        const v = state.volume || {};
        if (typeof v.level === "number") audio.level = v.level;
        if (typeof v.muted === "boolean") audio.muted = v.muted;
        if (v.sink && v.sink !== audio.sink) {
          audio.sink = v.sink;
          audio.sinks = (audio.sinks || []).map((s) => ({ ...s, active: s.id === v.sink }));
        }
      },
      rows: () => {
        if (!loaded) return [];
        if (audio.available === false) return [{ id: "none", type: "info", label: S.ccSoundNone, enabled: false }];
        const sinks = audio.sinks || [];
        const active = sinks.find((s) => s.active) || sinks.find((s) => s.id === audio.sink);
        return [
          { id: "level", type: "slider", label: S.ccVolume, value: audio.level, min: 0, max: 100, step: 5, unit: "%", icon: audio.muted ? "sound-off" : "sound" },
          { id: "muted", type: "toggle", label: S.ccMute, value: !!audio.muted },
          { id: "sink", type: "select", label: S.ccSink, value: active ? active.id : audio.sink, options: sinks.map((s) => ({ value: s.id, label: s.label || s.id })), enabled: sinks.length > 0, note: sinks.length ? "" : S.ccSinkNone },
        ];
      },
      onSet: async (row, value) => {
        if (row.id === "level") { audio.level = value; await call("audio.setVolume", { level: value }); }
        else if (row.id === "muted") { audio.muted = value; await call("audio.setMuted", { muted: value }); }
        else if (row.id === "sink") {
          await call("audio.setSink", { id: value });
          audio.sink = value;
          audio.sinks = (audio.sinks || []).map((s) => ({ ...s, active: s.id === value }));
        }
        ctx.refresh();
      },
    };
  },

  // ---------------------------------------------------------------- Mikrofon (07 11.4: Eingabegerät, Stumm, Pegel)
  // Die Zeilen sind die der Einstellungsseite audio.mic (Kern: settings/audio.py); geändert wird
  // über settings.set wie dort. So gibt es eine Stelle, die das Mikrofon kennt.
  mic: (ctx) => {
    let rows = null;
    return {
      events: ["settings.changed"],
      load: async () => {
        const page = (await call("settings.page", { id: "audio.mic" })) || {};
        rows = Array.isArray(page.rows) ? page.rows : [];
      },
      rows: () => (rows === null ? [] : rows.length ? rows : [{ id: "none", type: "info", label: S.ccMicNone, enabled: false }]),
      onSet: async (row, value) => {
        const result = (await call("settings.set", { id: row.id, value })) || {};
        // Fehler als Ausnahme: Die Zeile springt auf den alten Wert zurück, die Tafel meldet den Satz des Kerns
        if (result.error) throw new Error(result.message || S.settingsFailed);
        if (Array.isArray(result.rows) && (!result.rows_page || result.rows_page === "audio.mic")) { rows = result.rows; ctx.refresh(); }
      },
    };
  },

  // ---------------------------------------------------------------- Controller
  accessories: (ctx) => {
    let offs = [];                // Bluetooth-Geräte, die sich ausschalten lassen: {id, name, hint}
    return {
      empty: S.ccPadsEmpty,
      keys: ["pads"],
      events: ["settings.changed"],
      // „Controller ausschalten“ gibt es nur, wenn die Einstellungen es für ein verbundenes Gerät
      // anbieten (Zeile accessories.bt.disconnect:<MAC> auf der Seite des Geräts, docs/RPC.md),
      // und nur für Geräte, die gerade als Controller angemeldet sind (gleicher Name): Ein
      // Bluetooth-Kopfhörer gehört nicht hierher
      load: async () => {
        const found = [];
        const padNames = (store.state.pads || []).map((p) => String(p.name || "").toLowerCase()).filter(Boolean);
        const isPad = (name) => { const n = String(name || "").toLowerCase(); return !!n && padNames.some((p) => p.includes(n) || n.includes(p)); };
        try {
          const page = padNames.length ? await call("settings.page", { id: "accessories.bluetooth" }) : null;
          const devices = ((page && page.rows) || []).filter((r) => r.type === "page" && String(r.page || "").startsWith("accessories.bt.device:") && isPad(r.label)).slice(0, 6);
          for (const dev of devices) {
            const sub = await call("settings.page", { id: dev.page });
            const off = ((sub && sub.rows) || []).find((r) => String(r.id).startsWith("accessories.bt.disconnect:") && r.enabled !== false);
            if (off) found.push({ id: off.id, name: dev.label, hint: off.hint || "" });
          }
        } catch (e) { /* ohne die Zeilen zum Ausschalten */ }
        offs = found;
      },
      rows: () => [
        ...(store.state.pads || []).map((pad) => ({
          id: "pad:" + pad.id, type: "info", label: pad.name || S.unknown, icon: "gamepad",
          value: pad.battery === null || pad.battery === undefined ? S.ccPadWired : fmt(pad.charging ? S.ccPadCharging : S.ccPadBattery, { n: Math.round(pad.battery) }),
        })),
        ...offs.map((off) => ({ id: off.id, type: "action", label: fmt(S.ccPadOff, { name: off.name }), icon: "power", hint: off.hint })),
      ],
      onAction: async (row) => {
        const result = await call("settings.action", { id: row.id });
        // Der Kern fragt bei confirm-action selbst (docs/RPC.md); nach „Abbrechen“ bleibt die Zeile stehen
        if (result && result.cancelled) return;
        if (result && result.error) { ctx.fail({ message: result.message }); return; }
        if (result && result.message) ctx.note(result.message, S.ccAccessories);
        offs = offs.filter((o) => o.id !== row.id);
      },
    };
  },

  // ---------------------------------------------------------------- Leistung
  performance: (ctx) => {
    let perf = null;
    const overlayLabel = { off: S.perfOff, fps: S.perfFps, bar: S.perfCompact, detailed: S.perfFull };
    const resLabel = { native: S.optNative };
    return {
      load: async () => { perf = await call("perf.get"); },
      rows: () => {
        if (!perf) return [];
        const rows = [];
        // nur was hier wirkt (perf.get: overlayAvailable, limitAvailable)
        if (perf.overlayAvailable && Array.isArray(perf.overlays) && perf.overlays.length) {
          // perf.set overlay ist die globale Einstellung (anders als die Bildrate darunter): das sagen
          rows.push({ id: "overlay", type: "select", label: S.ccPerfOverlay, value: perf.overlay, hint: S.ccPerfOverlayHint,
            options: perf.overlays.map((v) => ({ value: v, label: overlayLabel[v] || String(v) })) });
        }
        if (perf.limitAvailable && Array.isArray(perf.fpsLimits) && perf.fpsLimits.length) {
          rows.push({ id: "fpsLimit", type: "select", label: S.ccPerfFps, value: Number(perf.fpsLimit) || 0, hint: perf.appActive ? S.ccPerfFpsHint : S.ccPerfFpsIdle,
            options: perf.fpsLimits.map((n) => ({ value: n, label: n ? fmt(S.optFps, { n }) : S.optNoLimit })) });
        }
        if (Array.isArray(perf.resolutions) && perf.resolutions.length) {
          rows.push({ id: "resolution", type: "select", label: S.ccPerfRes, value: perf.resolution, hint: S.ccPerfResHint,
            options: perf.resolutions.map((v) => ({ value: v, label: resLabel[v] || String(v) })) });
        }
        if (perf.refresh) rows.push({ id: "refresh", type: "info", label: S.ccPerfRefresh, value: fmt(S.ccPerfHz, { n: perf.refresh }) });
        // OptiScaler (E118): nur, wenn das Spiel vorn mit ihm gestartet wurde (running[].optiscaler)
        const front = (store.state.running || []).find((r) => r.entryId === store.state.foreground);
        if (front && front.optiscaler) rows.push({ id: "optiscaler", type: "action", label: S.ccOpti, hint: S.ccOptiHint, icon: "gamepad" });
        return rows;
      },
      onSet: async (row, value) => {
        perf = { ...perf, [row.id]: value, ...((await call("perf.set", { [row.id]: value })) || {}) };
        ctx.refresh();
      },
      // Die Taste kommt erst, wenn das Control Center zu ist und das Spiel weiterläuft (shell.optiscaler_menu)
      onAction: async () => { await call("app.optiscaler", {}); ctx.api.close(); },
    };
  },

  // ---------------------------------------------------------------- Maus-Modus (Dreieck am Symbol)
  // Schalter und darunter die Belegung als feste Tabelle (nicht fokussierbar: nur zum Lesen)
  mouse: () => ({
    keys: ["mouseMode"],
    rows: () => [{ id: "on", type: "toggle", label: S.ccMouse, value: !!store.state.mouseMode, hint: S.ccMouseHint }],
    onSet: async (row, value) => { await call("mouse.set", { on: !!value }); },
    footer: () => h("div.cc-legend",
      h("div.row-section", { text: S.ccMouseKeys }),
      MOUSE_KEYS.map(([key, does]) => h("div.cc-legend-row", h("span.cc-legend-key", { text: S[key] }), h("span.cc-legend-do", { text: S[does] })))),
  }),
};
