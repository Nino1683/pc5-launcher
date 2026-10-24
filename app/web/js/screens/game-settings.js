// Spieleinstellungen eines Eintrags (07 Abschnitt 11.2): eine Spalte mit den allgemeinen
// Zeilen-Bausteinen, Titel = Name des Spiels. Werte aus lib.get, Änderungen über lib.setOption
// (die Startdatei eines Archiv-Spiels über install.setExe, das sie gegen den Spielordner prüft).
//
// Welche Zeile erscheint, hängt am Startweg (startWay; E20: jede Zeile hat ein Backend oder entfällt, E99):
//   steam     couch-steam-game (E57): Steam startet das Spiel. FSR 4 und HDR gibt der Helfer dem
//             unsichtbaren Steam mit (launch.steam_game_env); Auflösung, Bildrate, Leistungsanzeige und
//             Anhalten regelt die Konsole wie bei jedem Spiel. Proton-Fassung und Controller (Steam Input)
//             legt Steam fest. Keine Startoptionen: Ob Steam sie über steam://run unsichtbar übernimmt
//             oder erst nachfragt, ist unbestätigt.
//   windows   eigener Proton-Start (launch/proton.py)
//   epic      Epic (E73): legendary startet seine eigene Startdatei, also keine Zeile „Startdatei“
//   launcher  Battle.net, Ubisoft, Rockstar (E94): Der Launcher startet das Spiel, weder Startdatei
//             noch Startargumente wirken
//   linux     native.py (Lutris-Linux, lokale Linux-Programme): ohne Proton, FSR 4, HDR und ohne
//             „Als Xbox-Controller“/„Direkt“
//   other     Emulatoren: nur die allgemeinen Zeilen
//
// Eigene Sprungstellen (E99): Im Zeiger-Modus merkt „Create halten + Steuerkreuz/L1/R1“ im Spiel die
// Zeigerstelle (Option pad_spots); hier lassen sie sich zurücksetzen. lib.get.pointer = das Spiel läuft
// im Zeiger-Modus (auch über „Automatisch“, z. B. Hearthstone).
//
// Globale Schalter (E41, lib.get.globals): Ist FSR 4, HDR oder SDL unter Einstellungen › Spiele
// für alle Spiele aus, kann ein Spiel das nicht übersteuern. Die Zeile steht dann gesperrt auf
// „Aus“ und sagt warum, statt ein wirkungsloses „Ein“ anzubieten.
//
// Tastenbelegung (E109): Bei „Als Tastatur und Maus“ öffnet die Zeile „Tastenbelegung“ den Unterbildschirm
// game-keys (hier angemeldet): je Pad-Taste ein Ziel (Option pad_keys über lib.setOption), Vorlagen,
// Zurücksetzen. Gespeichert wird nur, was von E64 abweicht ({} = Standard), wie library/model.pad_keys_value.
// Wirkt ab dem nächsten Start (launch.spec.apply_pad_keys). Steam-Spiele: nicht hier (Steam Input, E64).
import { call, on } from "../core/bridge.js";
import { router } from "../core/router.js";
import { S, fmt } from "../core/strings.js";
import { createPageScreen } from "./settings.js";

const SPOTS_RESET = "pad_spots_reset";     // Zeile „Sprungstellen zurücksetzen“ (keine Option, eine Aktion)
const KEYS_ROW = "pad_keys_page";          // Zeile „Tastenbelegung“: öffnet game-keys (keine Option)
const KEYS_PRESET = "pad_keys_preset";     // game-keys: Auswahl der Vorlage
const KEYS_RESET = "pad_keys_reset";       // game-keys: Zurücksetzen (Rückfrage)
// Spiegel von library/model.py (PAD_KEY_BUTTONS, PAD_KEY_TARGETS, PAD_KEYS_DEFAULT, PAD_KEY_PRESETS) als JSON
// zwischen den Marken: tests/unit/input/test_padmouse_keys.py vergleicht beide Seiten
const PAD_KEYS = /* E109-pad-keys */ {
  "buttons": ["cross", "circle", "square", "triangle", "l1", "r1", "l2", "r2", "l3", "r3", "options", "create",
    "dpad_up", "dpad_down", "dpad_left", "dpad_right"],
  "targets": ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m", "n", "o", "p", "q", "r", "s", "t", "u", "v",
    "w", "x", "y", "z", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9",
    "f10", "f11", "f12", "space", "enter", "esc", "tab", "backspace", "shift", "ctrl", "alt", "up", "down", "left", "right",
    "mouse_left", "mouse_right", "mouse_middle", "wheel_up", "wheel_down", "none"],
  "default": {"cross": "space", "circle": "c", "square": "r", "triangle": "e", "l1": "q", "r1": "f", "l2": "mouse_right",
    "r2": "mouse_left", "l3": "shift", "r3": "mouse_middle", "options": "esc", "create": "tab", "dpad_up": "up",
    "dpad_down": "down", "dpad_left": "left", "dpad_right": "right"},
  "presets": {
    "standard": {},
    "shooter": {"cross": "space", "circle": "ctrl", "square": "r", "triangle": "e", "l1": "q", "r1": "f", "l3": "shift",
      "r3": "v", "dpad_up": "1", "dpad_right": "2", "dpad_down": "3", "dpad_left": "4", "options": "esc", "create": "tab",
      "r2": "mouse_left", "l2": "mouse_right"},
    "racing": {"r2": "w", "l2": "s", "cross": "space", "circle": "c", "square": "r", "triangle": "enter", "l1": "q", "r1": "e",
      "options": "esc", "create": "tab"},
    "adventure": {"cross": "space", "circle": "esc", "square": "e", "triangle": "i", "l1": "q", "r1": "tab", "dpad_up": "m",
      "dpad_down": "j", "dpad_left": "left", "dpad_right": "right", "options": "esc", "create": "tab", "r2": "mouse_left",
      "l2": "mouse_right"}
  }
} /* Ende E109-pad-keys */;

// Gespeicherte Form wie model.pad_keys_value: nur bekannte Paare, die von E64 abweichen, in der Reihenfolge der Tasten
function padKeys(value) {
  const src = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const out = {};
  for (const button of PAD_KEYS.buttons) {
    const target = src[button];
    if (typeof target === "string" && PAD_KEYS.targets.includes(target) && target !== PAD_KEYS.default[button]) out[button] = target;
  }
  return out;
}

// Name der Vorlage, der die Belegung genau entspricht, sonst "" (eigene)
function presetOf(keys) {
  const now = JSON.stringify(padKeys(keys));
  return Object.keys(PAD_KEYS.presets).find((id) => JSON.stringify(padKeys(PAD_KEYS.presets[id])) === now) || "";
}

function keysSummary(keys) {
  const preset = presetOf(keys);
  return preset ? S.gkPresets[preset] : fmt(S.gsKeysOwn, { n: Object.keys(padKeys(keys)).length });
}

// Ziele einer Taste für die Auswahl: Buchstaben nach ihrer QWERTZ-Beschriftung (Y vor Z), dann wie die Liste;
// die Vorgabe dieser Taste trägt den Vermerk „Standard“
function targetOptions(button) {
  const letters = PAD_KEYS.targets.slice(0, 26).sort((a, b) => S.gkTargets[a].localeCompare(S.gkTargets[b], "de"));
  return [...letters, ...PAD_KEYS.targets.slice(26)].map((t) => (t === PAD_KEYS.default[button]
    ? { value: t, label: S.gkTargets[t], note: S.gkDefaultNote } : { value: t, label: S.gkTargets[t] }));
}

function keyRows(keys) {
  const own = padKeys(keys);
  const preset = presetOf(own);
  const presets = Object.keys(PAD_KEYS.presets).map((id) => ({ value: id, label: S.gkPresets[id], note: S.gkPresetNotes[id] }));
  if (!preset) presets.push({ value: "", label: S.gkPresetOwn });
  const rows = [
    { id: KEYS_PRESET, type: "select", label: S.gkPreset, value: preset, options: presets, hint: S.gkPresetHint, section: S.gkSectionPreset },
    { id: KEYS_RESET, type: "confirm-action", label: S.gkReset, enabled: Object.keys(own).length > 0, hint: S.gkResetHint,
      section: S.gkSectionPreset, confirm: { title: S.gkResetTitle, text: S.gkResetText, ok: S.gkReset } },
  ];
  for (const button of PAD_KEYS.buttons) {
    rows.push({ id: "pad_key:" + button, button, type: "select", label: S.gkButtons[button],
      value: own[button] || PAD_KEYS.default[button], options: targetOptions(button),
      hint: fmt(S.gkButtonHint, { key: S.gkTargets[PAD_KEYS.default[button]] }), section: S.gkSectionButtons });
  }
  for (const id of ["left", "right", "ps"]) {
    rows.push({ id: "pad_fixed:" + id, type: "info", label: S.gkFixed[id], value: S.gkFixedValues[id], hint: S.gkFixedHint,
      section: S.gkSectionFixed });
  }
  return rows;
}

// Unterbildschirm game-keys {id, title}: Titel „Tastenbelegung“, darüber der Name des Spiels
function createKeys(params) {
  const id = params.id;
  let keys = {};
  let told = false;                 // „Gilt ab dem nächsten Start“ einmal je Besuch als Mitteilung
  let unsub = null;
  async function save(next) {
    keys = next;                    // sofort: eine zweite Zeile rechnet schon mit diesem Stand
    await call("lib.setOption", { id, key: "pad_keys", value: next });
    if (told) return { refresh: true };
    told = true;
    return { refresh: true, message: S.gkNextStart };
  }
  return createPageScreen({
    id: "game-keys", title: S.gkTitle, crumb: params.title || "",
    async fetch() {
      const detail = await call("lib.get", { id });
      keys = padKeys(((detail.entry || {}).options || {}).pad_keys);
      return { title: S.gkTitle, rows: keyRows(keys) };
    },
    async set(row, value) {
      if (row.id === KEYS_PRESET) {
        if (!Object.prototype.hasOwnProperty.call(PAD_KEYS.presets, value)) return undefined;   // „Eigene Belegung“
        return save(padKeys(PAD_KEYS.presets[value]));
      }
      if (!row.button) return undefined;
      return save(padKeys({ ...keys, [row.button]: value }));
    },
    async action(row) {
      if (!row || row.id !== KEYS_RESET) return null;
      keys = {};
      await call("lib.setOption", { id, key: "pad_keys", value: {} });
      return { refresh: true, message: S.gkResetDone };
    },
    watch(reload) { unsub = on("lib.changed", (p) => { if (!(p && p.reason === "art")) reload(); }); },
    unwatch() { if (unsub) unsub(); },
  });
}

router.register({ id: "game-keys", create: createKeys });
// Marken der Launcher-Spiele wie in library/local.py (LAUNCHER_BADGES). Ein neuer Launcher ohne
// Eintrag hier zählt als Windows-Spiel: Dann stehen nur zwei wirkungslose Zeilen mehr da.
const LAUNCHER_BADGES = ["BATTLE.NET", "UBISOFT", "ROCKSTAR"];

// Startweg aus Quelle und Kachel-Marke (lib.get liefert detail nicht an die Seite). Lokale Einträge:
// EPIC, Marke des Launchers, PROTON, Linux-Programme ohne Marke (local.py). choices.proton bietet der
// Kern nur Windows-Spielen an; die Marke trägt, wenn der Einstellungs-Teil ausfällt (web-ui-3).
function startWay(entry, choices) {
  const badge = entry.badge || "";
  switch (entry.source) {
    case "steam": return "steam";
    case "proton": return "windows";
    case "native": return "linux";
    case "local":
      if (badge === "EPIC") return "epic";
      if (LAUNCHER_BADGES.includes(badge)) return "launcher";
      return badge === "PROTON" || Array.isArray(choices.proton) ? "windows" : "linux";
    default: return Array.isArray(choices.proton) ? "windows" : "other";
  }
}

// Die Startdatei wählt man nur, wo die Konsole sie selbst startet (launch-proc-7)
function exeWorks(entry, way) {
  return entry.source === "local" && (way === "windows" || way === "linux");
}

function switchRow(id, label, value, globalOn, hint) {
  if (globalOn === false) {
    return { id, type: "select", label, value: "off", options: [{ value: "off", label: S.off }], enabled: false,
      note: S.gsGlobalOff, hint: S.gsGlobalOff, section: S.gsSectionPicture };
  }
  return { id, type: "select", label, value: value || "auto", hint, section: S.gsSectionPicture,
    options: [{ value: "auto", label: S.optAuto }, { value: "off", label: S.off }] };
}

function buildRows(entry, detail, extra) {
  const o = entry.options || {};
  const g = detail.globals || {};
  const choices = detail.choices || {};
  const rows = [];
  const way = startWay(entry, choices);
  const steam = way === "steam";
  const win = way === "windows" || way === "epic" || way === "launcher";     // Proton-Start der Konsole
  if (win) {
    const builds = [{ value: "", label: S.gsProtonDefault }, ...(choices.proton || []).map((b) => ({ value: b.value, label: b.label }))];
    if (o.proton && !builds.some((b) => b.value === o.proton)) builds.push({ value: o.proton, label: o.proton });
    rows.push({ id: "proton", type: "select", label: S.gsProton, value: o.proton || "", options: builds, section: S.gsSectionStart });
  }
  if (exeWorks(entry, way) && extra.exe.length) {
    const exes = extra.exe.map((c) => ({ value: c.path, label: c.path }));
    const current = (extra.exe.find((c) => c.current) || {}).path || o.exe || exes[0].value;
    if (!exes.some((x) => x.value === current)) exes.push({ value: current, label: current });
    rows.push({ id: "exe", type: "select", label: S.gsExe, value: current, options: exes, hint: S.gsExeHint, section: S.gsSectionStart });
  }
  rows.push({ id: "freeze", type: "toggle", label: S.gsFreeze, value: o.freeze !== false,
    hint: steam ? S.gsSteamFreezeHint : S.gsFreezeHint, section: S.gsSectionStart });
  if (win || steam) {
    rows.push(switchRow("fsr4", S.gsFsr, o.fsr4, g.fsr4, steam ? S.gsSteamFsrHint : S.gsFsrHint));
    rows.push(switchRow("hdr", S.gsHdr, o.hdr, g.hdr, steam ? S.gsSteamHdrHint : S.gsHdrHint));
    // OptiScaler je Spiel (2026-10-24): Proton lädt und schleust ihn selbst ein, Vorgabe aus
    rows.push({ id: "optiscaler", type: "select", label: S.gsOpti, value: o.optiscaler === "on" ? "on" : "off",
      hint: steam ? S.gsSteamOptiHint : S.gsOptiHint, section: S.gsSectionPicture,
      options: [{ value: "off", label: S.off }, { value: "on", label: S.on }] });
  }
  rows.push({ id: "resolution", type: "select", label: S.gsResolution, value: o.resolution || "native", hint: S.gsResolutionHint, section: S.gsSectionPicture,
    options: [{ value: "native", label: S.optNative }, { value: "1440p", label: "1440p" }, { value: "1080p", label: "1080p" }] });
  // Nur Teiler der Bildwiederholrate wirken (E23): die Liste kommt aus perf.get (fpsLimits).
  // Ein früher gespeicherter anderer Wert bleibt sichtbar und abwählbar.
  const limits = (extra.fpsLimits.length ? extra.fpsLimits : [0, 30, 60]).map(Number);
  const current = Number(o.fps_limit) || 0;
  if (!limits.includes(current)) limits.push(current);
  rows.push({ id: "fps_limit", type: "select", label: S.gsFpsLimit, value: current, hint: S.gsFpsHint, section: S.gsSectionPicture,
    options: limits.sort((a, b) => a - b).map((n) => ({ value: n, label: n ? fmt(S.optFps, { n }) : S.optNoLimit })) });
  rows.push({ id: "perf_overlay", type: "select", label: S.gsPerf, value: o.perf_overlay || "", hint: S.gsPerfHint, section: S.gsSectionPicture,
    options: [{ value: "", label: S.optSystem }, { value: "off", label: S.perfOff }, { value: "fps", label: S.perfFps }, { value: "bar", label: S.perfCompact }, { value: "detailed", label: S.perfFull }] });
  // Controller (E74): automatisch, als Xbox-Controller, direkt; ohne Controller-Unterstützung (E64) als
  // Tastatur und Maus, für reine Maus-Spiele als Zeiger (E97). Xbox und direkt nur für Windows-Spiele (Proton)
  if (win || way === "linux") {
    const padOptions = [{ value: "auto", label: S.gsPadAuto },
      ...(win ? [{ value: "xbox", label: S.gsPadXbox }, { value: "direct", label: S.gsPadDirect }] : []),
      { value: "kbm", label: S.gsPadKbm }, { value: "mouse", label: S.gsPadMouse }];
    // Ein Wert, den dieser Startweg nicht kennt (früher bot die Seite „Xbox“/„Direkt“ auch Linux-
    // Einträgen an), wirkt wie „Automatisch“ (pad_mode) und steht deshalb auch so da (E99)
    const pad = padOptions.some((x) => x.value === o.pad) ? o.pad : "auto";
    // Mit eigener Tastenbelegung (E109) stimmt die Liste der Tasten in gsPadKbmHint nicht mehr
    const ownKeys = Object.keys(padKeys(o.pad_keys)).length > 0;
    const padHint = pad === "kbm" ? (ownKeys ? S.gsPadKbmHintOwn : S.gsPadKbmHint) : pad === "mouse" ? S.gsPadMouseHint : win ? S.gsPadHint : S.gsPadHintNative;
    rows.push({ id: "pad", type: "select", label: S.gsPad, value: pad, hint: padHint,
      section: S.gsSectionPad, options: padOptions });
    if (o.pad === "kbm") {
      rows.push({ id: "pad_look", type: "select", label: S.gsPadLook, value: Number(o.pad_look) || 1, hint: S.gsPadLookHint, section: S.gsSectionPad,
        options: [[0.5, S.gsPadLookSlow], [0.75, S.gsPadLookCalm], [1, S.gsPadLookNormal], [1.5, S.gsPadLookQuick], [2, S.gsPadLookFast]]
          .map(([value, label]) => ({ value, label })) });
      // E109: Tastenbelegung je Spiel, Unterbildschirm game-keys
      rows.push({ id: KEYS_ROW, type: "action", label: S.gsKeys, value: keysSummary(o.pad_keys), hint: S.gsKeysHint, section: S.gsSectionPad });
    }
    // Nur ein echtes Objekt zählt (eine von Hand verdorbene library.json behandelt der Kern als leer)
    const own = o.pad_spots && typeof o.pad_spots === "object" && !Array.isArray(o.pad_spots) ? o.pad_spots : {};
    const spots = Object.keys(own).length;
    // keepSpots: Die Zeile stand schon da und bleibt bis zum Verlassen der Seite (E99): Nach dem
    // Zurücksetzen verschwand sie sonst bei Spielen ohne Zeiger-Modus, und der Fokus sprang weg
    if (pad === "mouse" || detail.pointer || spots || extra.keepSpots) {
      rows.push({ id: SPOTS_RESET, type: "confirm-action", label: S.gsPadSpotsReset, section: S.gsSectionPad,
        enabled: spots > 0, note: spots ? fmt(S.gsPadSpotsCount, { n: spots }) : S.gsPadSpotsNone, hint: S.gsPadSpotsHint,
        confirm: { title: S.gsPadSpotsReset, text: S.gsPadSpotsResetText, ok: S.gsPadSpotsResetOk } });
    }
  }
  // proton.py und native.py hängen option args an; bei Epic gehen sie über couch-epic-game an
  // „legendary launch“ (E99, launch-proc-7; ob legendary sie ans Spiel weiterreicht, ist unbestätigt)
  if (way === "windows" || way === "epic" || way === "linux") {
    rows.push({ id: "args", type: "text", label: S.gsArgs, value: o.args || "", hint: S.gsArgsHint, section: S.gsSectionAdvanced });
  }
  // Was Steam selbst festlegt, als Angabe mit Erklärung (fokussierbar, damit man sie auch erreicht)
  if (steam) {
    rows.push({ id: "_steam", type: "info", label: S.gsSteamManaged, value: S.gsSteamManagedValue,
      hint: S.gsSteamManagedHint, section: S.gsSectionAdvanced });
  }
  return rows;
}

function create(params) {
  const id = params.id;
  let unsub = null;
  let spotsShown = false;           // Zeile „Sprungstellen zurücksetzen“ war schon zu sehen
  let entryTitle = params.title || "";
  return createPageScreen({
    id, title: params.title || S.gsTitle,
    async fetch() {
      const detail = await call("lib.get", { id });
      const entry = detail.entry || {};
      entryTitle = entry.title || entryTitle;
      const extra = { exe: [], fpsLimits: [] };
      if (exeWorks(entry, startWay(entry, detail.choices || {}))) {
        try { extra.exe = (await call("lib.exeCandidates", { id })).candidates || []; } catch (e) { /* ohne Auswahl */ }
      }
      try { extra.fpsLimits = (await call("perf.get")).fpsLimits || []; } catch (e) { /* Vorgabe */ }
      const rows = buildRows(entry, detail, { ...extra, keepSpots: spotsShown });
      spotsShown = rows.some((r) => r.id === SPOTS_RESET);
      return { title: entry.title || params.title, rows };
    },
    async set(row, value) {
      if (row.id === "exe") {
        const result = await call("install.setExe", { jobId: id, exe: value });
        if (!result || result.ok === false) throw new Error((result && result.message) || S.settingsFailed);
        return undefined;
      }
      await call("lib.setOption", { id, key: row.id, value });
      return undefined;
    },
    async action(row) {
      // E109: settings.js öffnet angemeldete Bildschirme aus {ui, params}
      if (row && row.id === KEYS_ROW) return { ui: "game-keys", params: { id, title: entryTitle } };
      if (!row || row.id !== SPOTS_RESET) return null;
      await call("lib.setOption", { id, key: "pad_spots", value: {} });
      return { refresh: true, message: S.gsPadSpotsResetDone };
    },
    // Bilder anderer Einträge ändern an dieser Seite nichts
    watch(reload) { unsub = on("lib.changed", (p) => { if (!(p && p.reason === "art")) reload(); }); },
    unwatch() { if (unsub) unsub(); },
  });
}

export default { id: "game-settings", create };
