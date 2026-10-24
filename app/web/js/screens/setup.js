// Einrichtungs-Assistent (E105, Teil P5): Bildschirme und Texte nach Bauplan docs/research/e103/SPEC.md
// 8.1, Verträge 4.3 (RPC setup.*, Ereignisse setup.progress/setup.done) und 4.2 (sys.state.setup,
// Aufträge in sys.state.store_jobs mit store "setup").
//   setup-welcome   S1  Willkommen: was erkannt wurde; aus den Einstellungen mit Sprungzeilen
//   setup-place     S2  Spiele-Ort und Ordner für Emulator-Spiele        Schritt 1 von 7
//   setup-shops     S3  Shops (nur Information) und „Epic Games vorbereiten“    2
//   setup-emus      S4  Konsolen: Emulatoren aus dem Katalog                    3
//   setup-apps      S5  Apps für den Fernseher                                  4
//   setup-tools     S6  Windows-Spiele: umu-launcher, Proton (entfällt, wenn nichts fehlt)  5
//   setup-summary   S7  Zusammenfassung mit Platzplan (setup.plan)              6
//   setup-progress  S8  Fortschritt                                             7
//   setup-done      S9  Fertig: Ordner je System, was fehlt, Tipps
// Ein Durchlauf teilt seine Wahl über params.flow; die Schritte ersetzen einander (router.replace).
// Kreis fragt in S2–S7 „Einrichtung verlassen?“, der Knopf „Zurück“ geht einen Schritt zurück, Dreieck
// erklärt den Schritt. Gespeichert wird nichts vor „Installieren“: setup.start bekommt Spiele-Ort,
// Emulator-Ordner und die Kennungen aus dem Katalog; der Kern setzt die Einstellungen selbst.
// Einstiege: sys.state.setup.due (main.js, params.auto), Einstellungen › PC5 Launcher › Einrichtung
// (params.from "settings"), „Installieren“ auf einer fehlenden App-Kachel (params.from "tile",
// params.only = Katalog-Kennung) und ui.open {screen: "setup-…", params} aus dem Overlay.
import { h, sizeText } from "../core/dom.js";
import { call, on } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { router } from "../core/router.js";
import { sound } from "../core/sound.js";
import { store } from "../core/store.js";
import { S, fmt } from "../core/strings.js";
import { confirm, dialog } from "../components/dialog.js";
import { toast } from "../components/toast.js";
import { createListScreen } from "./file-browser.js";

const TOTAL = 7;
const STEP = { "setup-place": 1, "setup-shops": 2, "setup-emus": 3, "setup-apps": 4, "setup-tools": 5, "setup-summary": 6, "setup-progress": 7 };
const ORDER = ["setup-welcome", "setup-place", "setup-shops", "setup-emus", "setup-apps", "setup-tools", "setup-summary"];
const REMAINING_AFTER_S = 10;     // vorher schwankt die Schätzung zu stark (wie screens/installer.js)
const MB = 1024 * 1024;
const HINTS = [["btn-cross", S.select], ["btn-circle", S.hintBack], ["btn-triangle", S.setupMore]];
const RUNNING = new Set(["running", "downloading", "installing", "active"]);

// Zeilen in S4 (Bauplan 8.4, „Titel ← Systeme“): mehrere Zeilen können denselben Emulator meinen
// (RetroArch zählt nur einmal); Titel in strings.js setupEmuRows
const EMU_ROWS = [
  { id: "classic", emu: "retroarch", section: "nintendo" },
  { id: "gc", emu: "dolphin", section: "nintendo" },
  { id: "wiiu", emu: "cemu", section: "nintendo" },
  { id: "nds", emu: "melonds", section: "nintendo" },
  { id: "n3ds", emu: "azahar", section: "nintendo" },
  { id: "switch", emu: "eden", section: "nintendo" },          // nur, wenn der Katalog ihn anbietet (Frage 3)
  { id: "psx", emu: "retroarch", section: "ps", hint: "setupPs1Hint" },
  { id: "ps2", emu: "pcsx2", section: "ps" },
  { id: "ps3", emu: "rpcs3", section: "ps" },
  { id: "psp", emu: "ppsspp", section: "ps" },
  { id: "sega", emu: "retroarch", section: "sega" },
  { id: "dreamcast", emu: "flycast", section: "sega" },
  { id: "xbox", emu: "xemu", section: "sega" },
  { id: "arcade", emu: "mame", section: "sega" },
  { id: "scummvm", emu: "scummvm", section: "sega" },
];
// Endungen je System, falls der Katalog keine nennt (Bauplan 8.4)
const EXTS = {
  nes: [".nes", ".zip", ".7z"], snes: [".sfc", ".smc", ".zip", ".7z"], gb: [".gb", ".zip"], gbc: [".gbc", ".zip"], gba: [".gba", ".zip"],
  n64: [".z64", ".n64", ".v64", ".zip"], psx: [".chd", ".cue", ".pbp", ".m3u"], genesis: [".md", ".gen", ".bin", ".zip"], mastersystem: [".sms", ".zip"],
  gc: [".rvz", ".iso", ".gcz", ".ciso"], wii: [".rvz", ".wbfs", ".iso"], wiiu: [".wua", ".wud", ".wux", ".rpx"], nds: [".nds", ".zip"],
  n3ds: [".3ds", ".cci", ".cxi", ".3dsx"], switch: [".nsp", ".xci"], ps2: [".chd", ".iso", ".cso"], psp: [".iso", ".cso", ".chd", ".pbp"],
  xbox: [".iso", ".xiso"], dreamcast: [".chd", ".gdi", ".cdi"],
};
// Schreibweise der Dateisysteme in der Erklärung einer gesperrten Platte
const FS_NAMES = { exfat: "exFAT", ntfs: "NTFS", ntfs3: "NTFS", fuseblk: "NTFS", vfat: "FAT", fat: "FAT", msdos: "FAT" };

// ---------------------------------------------------------------- kleine Helfer
const ask = (method, p = {}) => call(method, p).then((r) => r || null, () => null);
const num = (v) => { const n = Number(v); return Number.isFinite(n) && n > 0 ? n : 0; };
const mb = (v) => num(v) * MB;
// Größen aus dem Katalog in ganzen MB („55 MB“ wie im Bauplan), ab 1 GB wie sizeText
const mbText = (bytes) => (!num(bytes) ? "" : num(bytes) < 1024 * MB ? Math.max(1, Math.round(num(bytes) / MB)) + " MB" : sizeText(bytes));
// „~/Games“ statt „/var/home/<nutzer>/Games“
const tilde = (path) => String(path || "").replace(/^(?:\/var)?\/home\/[^/]+(?=\/|$)/, "~");
const listOf = (v) => (Array.isArray(v) ? v.filter((x) => x && typeof x === "object" && typeof x.id === "string" && x.id) : []);
const byId = (list, id) => list.find((x) => x.id === id) || null;
function joinAnd(names) {
  return names.length < 2 ? names.join("") : names.slice(0, -1).join(", ") + S.setupAnd + names[names.length - 1];
}
function setupJob() {
  return (store.state.store_jobs || []).find((j) => j && j.store === "setup") || null;
}

// Systeme eines Emulators als [{id, exts}]; der Katalog darf Liste, Objekte oder ein Wörterbuch liefern
function systemsOf(emu) {
  const out = [];
  const add = (id, exts) => {
    if (typeof id !== "string" || !id || out.some((s) => s.id === id)) return;
    out.push({ id, exts: Array.isArray(exts) && exts.length ? exts.map(String) : (EXTS[id] || []) });
  };
  const raw = emu && emu.systems;
  if (Array.isArray(raw)) {
    for (const s of raw) {
      if (typeof s === "string") add(s);
      else if (s && typeof s === "object") add(s.id || s.folder, s.exts || s.extensions);
    }
  } else if (raw && typeof raw === "object") {
    for (const [id, v] of Object.entries(raw)) add(id, Array.isArray(v) ? v : v && (v.exts || v.extensions));
  }
  return out;
}
function needsBios(emu, sys) {
  const b = emu && emu.bios;
  return b === true || (typeof b === "string" && !!b) || (Array.isArray(b) && b.includes(sys.id));
}

// ---------------------------------------------------------------- Durchlauf (gemeinsame Wahl der Schritte)
function newFlow(params) {
  return {
    from: params.from || (params.auto ? "auto" : ""), auto: !!params.auto,
    state: null, detect: null, catalog: null, failed: false, pending: null, preset: null,
    place: "", emuChoice: "new", emus: new Set(), apps: new Set(),
    tools: { umu: false, legendary: false, proton: "" },
  };
}
function flowOf(params) {
  if (!params.flow || typeof params.flow !== "object") params.flow = newFlow(params);
  return params.flow;
}
// Zustand, Erkennung und Katalog einmal je Durchlauf (setup.detect läuft im Arbeits-Thread des Kerns)
function load(flow) {
  if (!flow.pending) {
    flow.pending = Promise.all([ask("setup.state"), ask("setup.detect"), ask("setup.catalog")]).then(([state, detect, catalog]) => {
      flow.state = state || {};
      flow.detect = detect || {};
      flow.catalog = catalog || {};
      flow.failed = !detect || !catalog;
      defaults(flow);
      return flow;
    });
  }
  return flow.pending;
}

const emulators = (flow) => listOf(flow.catalog && flow.catalog.emulators).filter((e) => e.offer !== false);
const appsOf = (flow) => listOf(flow.catalog && flow.catalog.apps);
const toolsOf = (flow) => listOf(flow.catalog && flow.catalog.tools);
const places = (flow) => listOf(flow.detect && flow.detect.places);
const placeOf = (flow) => places(flow).find((p) => p.path === flow.place) || null;
const flatpakMissing = (flow) => !!(flow.detect && flow.detect.flatpak && flow.detect.flatpak.present === false);

// "system" | "user" | "" – Erkennung (detect.installed je Flatpak-Kennung) vor dem Feld des Katalogs
function installedHow(flow, item) {
  if (!item) return "";
  const map = (flow.detect && flow.detect.installed) || {};
  const how = item.flatpak ? map[item.flatpak] : undefined;
  if (how === "system" || how === "user") return how;
  if (item.installed === "system" || item.installed === "user") return item.installed;
  return item.installed ? "user" : "";
}
function toolPresent(flow, id) {
  const d = flow.detect || {};
  if (id === "umu") return !!(d.umu && d.umu.present);
  if (id === "proton") return !!(d.proton && d.proton.present);
  const tool = byId(toolsOf(flow), id);
  return !!(tool && tool.installed);
}
// Netzstand: setup.detect (Kern: connected | limited | offline | unknown), sonst sys.state.network
function netState(flow) {
  const s = flow.detect && flow.detect.internet ? flow.detect.internet.state : undefined;
  if (s === true) return "connected";
  if (s === false) return "offline";
  if (s === undefined || s === null || s === "") return String((store.state.network && store.state.network.state) || "unknown");
  return String(s);
}
// Nur „offline“ heißt ohne Verbindung. Bei „unknown“ (noch nicht gelesen, kein NetworkManager) und
// „limited“ (Portal) versucht der Assistent es und hört auf den Kern (code "no-network" → D3)
function isOnline(flow) {
  return netState(flow) !== "offline";
}
function netText(flow) {
  const s = netState(flow);
  if (s === "offline") return S.setupOffline;
  if (s === "limited") return S.setupNetLimited;
  return s === "connected" || s === "online" || s === "full" ? S.setupOnline : S.setupNetUnknown;
}
// Proton-Familien aus dem Katalog (tools.proton.families), sonst die beiden aus 8.1
function protonFamilies(flow) {
  const tool = byId(toolsOf(flow), "proton");
  const raw = (tool && Array.isArray(tool.families) ? tool.families : [])
    .map((f) => (typeof f === "string" ? { id: f } : f)).filter((f) => f && typeof f.id === "string" && f.id);
  const list = raw.length ? raw : [{ id: "proton-cachyos", recommended: true }, { id: "ge-proton" }];
  return list.map((f) => {
    const name = f.label || (S.setupProtonNames || {})[f.id] || f.id;
    const recommended = !!(f.recommended || f.default);
    return { id: f.id, name, recommended, label: recommended ? fmt(S.setupProtonRecommended, { label: name }) : name };
  });
}
function needsTools(flow) {
  return !(toolPresent(flow, "umu") && toolPresent(flow, "proton"));
}

function defaults(flow) {
  const list = places(flow);
  const want = (flow.state && flow.state.gamesDisk) || "";
  const pick = list.find((p) => p.path === want && p.ok !== false) || list.find((p) => p.ok !== false) || null;
  flow.place = pick ? pick.path : want;
  const ed = flow.detect && flow.detect.emudeck;
  flow.emuChoice = ed && ed.present && ed.emulationPath ? "emudeck" : "new";
  flow.tools = defaultTools(flow);
  if (flow.preset) preset(flow, flow.preset.kind, flow.preset.only);
}
// Was fehlt, ist vorgewählt: umu, wenn es keins gibt; Proton in der empfohlenen Familie, wenn keins da ist
function defaultTools(flow) {
  const fams = protonFamilies(flow);
  return { umu: !toolPresent(flow, "umu"), legendary: false,
    proton: toolPresent(flow, "proton") ? "" : (fams.find((f) => f.recommended) || fams[0]).id };
}

// Vorauswahl: "recommended" (ganzer Durchlauf), "only" (eine App von ihrer Kachel), "none" (Sprungzeilen)
function recommendedOf(flow, list) {
  const rec = new Set(Array.isArray(flow.catalog && flow.catalog.recommended) ? flow.catalog.recommended : []);
  return list.filter((x) => rec.has(x.id) && !installedHow(flow, x)).map((x) => x.id);
}
function preset(flow, kind, only = "") {
  flow.preset = { kind, only };
  if (!flow.catalog) return;            // kommt mit defaults() nach dem Laden
  // Eine einzelne App oder „hinzufügen“: Proton und umu nur, wenn man sie in Schritt 5 selbst wählt
  flow.tools = kind === "recommended" ? defaultTools(flow) : { umu: false, legendary: false, proton: "" };
  // "only": Kennung einer App (App-Kachel) oder eines angebotenen Emulators (ROM-Eintrag ohne Emulator)
  flow.emus = new Set(kind === "recommended" ? recommendedOf(flow, emulators(flow)) : kind === "only" && byId(emulators(flow), only) ? [only] : []);
  flow.apps = new Set(kind === "recommended" ? recommendedOf(flow, appsOf(flow)) : kind === "only" && byId(appsOf(flow), only) ? [only] : []);
}

function newRoot(flow) {
  const p = placeOf(flow);
  const base = p && p.path ? p.path : flow.place;
  if (base) return String(base).replace(/\/+$/, "") + "/Emulation";
  return (flow.state && flow.state.emuRoot) || "";
}
function emuRoot(flow) {
  const ed = flow.detect && flow.detect.emudeck;
  if (flow.emuChoice === "emudeck" && ed && ed.present && ed.emulationPath) return ed.emulationPath;
  return newRoot(flow);
}
// Kennungen für setup.plan/setup.start: Emulatoren und Apps aus dem Katalog, "umu", "legendary",
// "proton:<familie>"; schon Installiertes fehlt
function chosenItems(flow) {
  const out = [];
  for (const id of flow.emus) if (!installedHow(flow, byId(emulators(flow), id))) out.push(id);
  for (const id of flow.apps) { const app = byId(appsOf(flow), id); if (app && !installedHow(flow, app)) out.push(id); }
  if (flow.tools.umu && !toolPresent(flow, "umu")) out.push("umu");
  if (flow.tools.legendary && !toolPresent(flow, "legendary")) out.push("legendary");
  if (flow.tools.proton && !toolPresent(flow, "proton")) out.push("proton:" + flow.tools.proton);
  return out;
}
function request(flow) {
  return { place: flow.place || "", emuRoot: emuRoot(flow), items: chosenItems(flow) };
}
function titleOf(flow, id) {
  const hit = byId(emulators(flow), id) || byId(appsOf(flow), id);
  if (hit) return hit.title || id;
  if (id === "umu") return S.setupUmu;
  if (id === "legendary") return S.setupLegendary;
  if (id.startsWith("proton:")) { const f = protonFamilies(flow).find((x) => x.id === id.slice(7)); return f ? f.name : id.slice(7); }
  return id;
}

// ---------------------------------------------------------------- Wege zwischen den Schritten
function steps(flow) {
  return ORDER.filter((id) => id !== "setup-tools" || needsTools(flow));
}
function goStep(flow, id) { router.replace(id, { flow }); }
function nextStep(flow, from) { const list = steps(flow); goStep(flow, list[Math.min(list.length - 1, list.indexOf(from) + 1)]); }
function prevStep(flow, from) { const list = steps(flow); goStep(flow, list[Math.max(0, list.indexOf(from) - 1)]); }

// Verlassen: Kam der Assistent von selbst (oder war er noch nie fertig), gilt „später“ (kein neuer Anlauf)
function leaveFlow(flow) {
  const st = flow.state && flow.state.state;
  if (flow.auto || st === "new" || st === "shown") call("setup.later").catch(() => {});
  router.back();
}
async function askLeave(flow, alive) {
  const yes = await confirm({ title: S.setupLeaveTitle, text: S.setupLeaveText, ok: S.setupLeaveOk, cancel: S.setupLeaveKeep });
  if (yes && alive()) leaveFlow(flow);
}
// Einstellungen › Netzwerk (WLAN), mit den Bereichen links wie in den Einstellungen
async function openNetwork(id) {
  let categories = [];
  try { categories = ((await call("settings.categories")) || {}).categories || []; } catch (e) { categories = []; }
  router.go("settings-page", {
    id, title: id === "network.wifi" ? S.setupWifiPage : S.setupNetworkPage,
    crumb: id === "network.wifi" ? S.setupCrumbNetwork : S.setupCrumbSettings, context: { categories, selected: "network" },
  });
}
const failDialog = (result) => dialog({ title: S.setupFailedTitle, text: (result && result.message) || S.errorGeneric, buttons: [{ id: "ok", label: S.ok }] });

// ---------------------------------------------------------------- Gerüst eines Schritts
// Kopf (Weg, Schrittanzeige, Titel, Unterzeile, Einleitung), Liste (components/rows.js) und rechte
// Spalte nebeneinander, Knopfzeile unten rechts, Fußzeile links davon. onCircle: Kreis.
function stepScreen({ step, title, sub = "", lead = "", more = "", handlers = {}, side = null, emptyText = "" }) {
  const screen = createListScreen({ title, sub: lead, hints: HINTS, cls: "setup" + (side ? ".setup-with-side" : ""), handlers: { emptyText, ...handlers } });
  const { el, list } = screen;
  const titleEl = el.querySelector(".flow-title");
  const box = el.querySelector(".flow-list");
  const crumb = h("div.setup-crumb", { text: step >= 1 && step <= TOTAL ? fmt(S.setupCrumb, { n: step, total: TOTAL }) : S.setupName });
  const dots = h("div.setup-steps", Array.from({ length: TOTAL }, (_, i) => h("i" + (i + 1 < step ? ".is-done" : i + 1 === step ? ".is-now" : ""))));
  el.prepend(crumb, dots);
  if (sub) titleEl.after(h("div.setup-subline", { text: sub }));
  const body = h("div.setup-body");
  box.before(body);
  body.append(box);
  box.append(list.scrollbar);                  // Rollbalken gehört zur Liste (rechte Kante)
  if (side) body.append(side);
  const foot = h("div.setup-foot", { hidden: true });
  const actions = h("div.setup-actions");
  el.append(h("div.setup-bar", foot, actions));
  // Links/Rechts auf einer Zeile springt nicht in die Knopfzeile (die liegt schräg darunter)
  list.el.addEventListener("nav-move", (e) => {
    if (e.defaultPrevented || e.detail.scroll || (e.detail.dir !== "left" && e.detail.dir !== "right")) return;
    e.preventDefault();
    sound.play("edge", { repeat: !!e.detail.repeat });
  });
  const buttons = new Map();
  const api = {
    el, list, onCircle: null,
    setButtons(defs) {
      buttons.clear();
      actions.replaceChildren(...defs.map((d) => {
        const b = h("div.btn" + (d.main ? "" : ".is-secondary"), { "data-focus": true, "data-focus-default": d.main ? true : null, dataset: { id: d.id } },
          h("span.btn-label", { text: d.label }));
        b.addEventListener("press", () => d.press());
        buttons.set(d.id, b);
        return b;
      }));
    },
    button: (id) => buttons.get(id) || null,
    setDisabled(id, off) {
      const b = buttons.get(id);
      if (!b) return;
      b.toggleAttribute("data-disabled", !!off);
      b.classList.toggle("is-off", !!off);
    },
    setFoot(text) { foot.textContent = text || ""; foot.hidden = !text; },
    setTitle: screen.setTitle,
    setLead: screen.setSub,
    // Dreieck: Erklärung der Zeile im Fokus (more), sonst des Schritts
    more() {
      const node = nav.current && list.el.contains(nav.current) ? nav.current.closest(".row") : null;
      const row = node ? list.rowOf(node) : null;
      sound.play("select");
      dialog({ title: row && row.more ? row.label : titleEl.textContent, text: (row && row.more) || more, buttons: [{ id: "ok", label: S.ok }] });
    },
    onPad(button, ev) {
      if (button === "triangle") { if (!ev.repeat) api.more(); return true; }
      if (button === "circle") {
        if (!ev.repeat && api.onCircle) { sound.play("back"); api.onCircle(); }
        return !!api.onCircle;
      }
      if (button === "l2" || button === "r2") { list.page(button === "r2" ? 1 : -1); return true; }
      return false;
    },
  };
  return api;
}

// Ergebnis eines Bildschirms: Router-Form, mit Abfrage „noch oben?“ für späte Antworten
function screenOf(s, extra = {}) {
  const inst = {
    el: s.el,
    onPad: (button, ev) => s.onPad(button, ev),
    onShow(info) { nav.ensure(); s.list.update(); if (extra.onShow) extra.onShow(info); },
    destroy() { inst.alive = false; if (extra.destroy) extra.destroy(); },
    alive: true,
  };
  return inst;
}
const isTop = (inst) => !!(router.top && router.top.inst === inst && inst.alive);

// ---------------------------------------------------------------- S1 Willkommen
function createWelcome(params = {}) {
  const flow = flowOf(params);
  const fromSettings = flow.from === "settings";
  const s = stepScreen({
    step: 0, title: S.setupWelcomeTitle, sub: S.setupWelcomeSub, lead: S.setupWelcomeText, more: S.setupMoreText.welcome,
    handlers: { onPage: (row) => jump(row), onAction: (row) => (row.id === "wifi" ? openNetwork("network.wifi") : undefined) },
  });
  let starting = false;
  s.setButtons([
    { id: "later", label: fromSettings ? S.close : S.setupLater, press: () => later() },
    { id: "start", label: S.setupStart, main: true, press: () => start() },
  ]);
  s.onCircle = () => later();
  s.list.setLoading();

  function rows() {
    const d = flow.detect || {};
    const out = [];
    if (flow.failed) out.push({ id: "failed", type: "info", label: S.setupUnavailable, enabled: false });
    if (fromSettings) {
      out.push({ id: "jump-emus", type: "page", label: S.setupJumpEmus, section: S.setupSecAdd });
      out.push({ id: "jump-apps", type: "page", label: S.setupJumpApps, section: S.setupSecAdd });
    }
    const section = fromSettings ? S.setupSecDevice : undefined;
    const sys = d.system || {};
    out.push({ id: "system", type: "info", label: S.setupSystem, value: sys.label || S.unknown, hint: sys.note || "", section });
    // rot und weit oben: ohne Flatpak installiert der Assistent nichts
    if (flatpakMissing(flow)) out.push({ id: "flatpak", type: "info", label: S.setupNoFlatpak, icon: "warn", hint: S.setupNoFlatpakHint, section });
    if (d.user) out.push({ id: "user", type: "info", label: S.setupUser, value: d.user, section });
    const steamMissing = !!(d.steam && d.steam.present === false);
    out.push({ id: "steam", type: "info", label: S.setupSteam, value: steamMissing ? S.setupNotFound : S.setupPresent, hint: steamMissing ? S.setupSteamHint : "", section });
    const net = netState(flow);
    out.push({ id: "internet", type: "info", label: S.setupInternet, value: netText(flow), section,
      hint: net === "limited" ? S.setupNetLimitedHint : net === "unknown" ? S.setupNetUnknownHint : "" });
    if (net === "offline" || net === "limited") out.push({ id: "wifi", type: "action", label: S.setupWifi, hint: S.setupWifiHint, indent: true, section });
    out.push({ id: "language", type: "info", label: S.setupLanguage, value: S.setupGerman, hint: S.setupLanguageHint, section });
    return out;
  }

  function later() {
    if (fromSettings || flow.from === "tile") { router.back(); return; }
    askLater();
  }
  // D1
  async function askLater() {
    const yes = await confirm({ title: S.setupLaterTitle, text: S.setupLaterText, ok: S.setupLater, cancel: S.setupLeaveKeep });
    if (!yes || !isTop(inst)) return;
    call("setup.later").catch(() => {});
    router.back();
  }
  async function start() {
    if (starting) return;
    starting = true;
    await load(flow);
    starting = false;
    if (!isTop(inst)) return;
    preset(flow, "recommended");
    goStep(flow, "setup-place");
  }
  async function jump(row) {
    await load(flow);
    if (!isTop(inst)) return;
    preset(flow, "none");
    goStep(flow, row.id === "jump-emus" ? "setup-emus" : "setup-apps");
  }
  // D4: unterbrochene Einrichtung (nur, wenn jemand den Assistenten öffnet; nie von selbst)
  async function askResume() {
    const choice = await dialog({ title: S.setupResumeTitle, text: S.setupResumeText,
      buttons: [{ id: "later", label: S.setupLater }, { id: "resume", label: S.setupResume }], initial: "resume" });
    if (choice !== "resume" || !isTop(inst)) return;
    const st = flow.state || {};
    const last = st.last && typeof st.last === "object" ? st.last : {};
    // Die Wahl von damals (setup.state.resume {place, emuRoot, items}); "" heißt dort „automatisch“.
    // gamesDisk/emuRoot sind errechnet (etwa aus SRM) und nur der Rückfall ohne resume.
    const res = st.resume && typeof st.resume === "object" ? st.resume : null;
    const raw = res && Array.isArray(res.items) ? res.items : Array.isArray(last.items) ? last.items : [];
    const items = raw.map((i) => (typeof i === "string" ? i : i && i.id)).filter((i) => typeof i === "string" && i);
    const place = res && typeof res.place === "string" ? res.place : st.gamesDisk || flow.place;
    const root = res && typeof res.emuRoot === "string" ? res.emuRoot : st.emuRoot || emuRoot(flow);
    const result = await ask("setup.start", { place, emuRoot: root, items });
    if (!isTop(inst)) return;
    if (result && result.ok) router.replace("setup-progress", { flow, jobId: result.jobId || "" });
    else failDialog(result);
  }

  const inst = screenOf(s, {
    // Zurück aus den WLAN-Einstellungen: Verbindung neu ansehen
    onShow(info) {
      if (!info || !info.back || !flow.detect) return;
      ask("setup.detect").then((d) => { if (d && inst.alive) { flow.detect = d; s.list.setRows(rows()); } });
    },
  });
  load(flow).then(() => {
    if (!inst.alive) return;
    // Läuft schon ein Auftrag, gleich zum Fortschritt
    const job = setupJob();
    const st = flow.state && flow.state.state;
    if ((job || st === "running") && isTop(inst)) { router.replace("setup-progress", { flow, jobId: job ? String(job.id) : "" }); return; }
    s.list.setRows(rows());
    if ((st === "interrupted" || params.resume) && isTop(inst)) askResume();
  });
  return inst;
}

// ---------------------------------------------------------------- S2 Spiele-Ort
function freeText(p) {
  if (num(p.total)) return fmt(S.setupFreeOf, { free: sizeText(p.free) || "0 B", total: sizeText(p.total) });
  return num(p.free) ? fmt(S.setupFree, { free: sizeText(p.free) }) : "";
}
function reasonText(p) {
  const r = String(p.reason || "");
  const fs = String(p.fs || "").toLowerCase();
  if (r === "readonly" || r === "ro") return S.setupReasonReadonly;
  if (r === "space" || r === "small" || r === "no-space") return S.setupReasonSpace;
  if (r === "fs" || r === "filesystem" || (!r && FS_NAMES[fs])) return fmt(S.setupReasonFs, { fs: FS_NAMES[fs] || fs || "?" });
  return r;                                   // fertiger Satz des Kerns
}

function createPlace(params = {}) {
  const flow = flowOf(params);
  const s = stepScreen({ step: STEP["setup-place"], title: S.setupPlaceTitle, lead: S.setupPlaceText, more: S.setupMoreText.place,
    handlers: { onPress: (row) => pick(row) } });
  s.setButtons([
    { id: "back", label: S.setupBack, press: () => prevStep(flow, "setup-place") },
    { id: "next", label: S.setupNext, main: true, press: () => nextStep(flow, "setup-place") },
  ]);
  s.list.setLoading();

  function rows() {
    const out = [];
    for (const p of places(flow)) {
      const ok = p.ok !== false;
      const chosen = ok && p.path === flow.place;
      out.push({ id: "place:" + p.id, type: "action", label: p.label || tilde(p.path), icon: chosen ? "check" : "storage", value: freeText(p),
        note: tilde(p.path), enabled: ok, hint: ok ? "" : reasonText(p), more: p.path, place: p });
    }
    if (!out.length) out.push({ id: "place:none", type: "info", label: flow.failed ? S.setupUnavailable : S.setupGamesFolder, value: tilde(flow.place), enabled: !flow.failed });
    const section = S.setupEmuSection;
    const ed = flow.detect && flow.detect.emudeck;
    if (ed && ed.present && ed.emulationPath) {
      out.push({ id: "emudeck", type: "info", label: S.setupEmudeckFound, value: tilde(ed.emulationPath), hint: S.setupEmudeckHint, more: ed.emulationPath, section });
      out.push({ id: "emu:emudeck", type: "action", label: S.setupEmudeckUse, icon: flow.emuChoice === "emudeck" ? "check" : "folder", section });
      out.push({ id: "emu:new", type: "action", label: S.setupEmudeckNew, icon: flow.emuChoice === "new" ? "check" : "folder", value: tilde(newRoot(flow)), more: newRoot(flow), section });
    } else {
      out.push({ id: "emuroot", type: "info", label: S.setupEmuFolder, value: tilde(emuRoot(flow)), hint: S.setupEmuFolderHint, more: emuRoot(flow), section });
    }
    return out;
  }
  function pick(row) {
    if (row.place) {
      if (row.place.ok === false) return false;
      flow.place = row.place.path;
    } else if (row.id === "emu:emudeck" || row.id === "emu:new") {
      flow.emuChoice = row.id.slice(4);
    } else {
      return false;
    }
    s.list.setRows(rows());
    return true;
  }

  const inst = screenOf(s);
  s.onCircle = () => askLeave(flow, () => isTop(inst));
  load(flow).then(() => { if (inst.alive) s.list.setRows(rows()); });
  return inst;
}

// ---------------------------------------------------------------- S3 Shops
function createShops(params = {}) {
  const flow = flowOf(params);
  const s = stepScreen({ step: STEP["setup-shops"], title: S.setupShopsTitle, lead: S.setupShopsText, more: S.setupMoreText.shops,
    handlers: { onSet: (row, value) => { if (row.id === "legendary") flow.tools.legendary = !!value; } } });
  s.setButtons([
    { id: "back", label: S.setupBack, press: () => prevStep(flow, "setup-shops") },
    { id: "next", label: S.setupNext, main: true, press: () => nextStep(flow, "setup-shops") },
  ]);
  s.list.setLoading();

  function rows() {
    const d = flow.detect || {};
    const steamMissing = !!(d.steam && d.steam.present === false);
    const out = [{ id: "steam", type: "info", label: S.setupSteam, value: steamMissing ? S.setupNotFound : S.setupPresent, hint: steamMissing ? S.setupSteamHint : "" }];
    S.setupShopNames.forEach((name, i) => out.push({ id: "shop:" + i, type: "info", label: name, value: S.setupShopInStore }));
    const has = toolPresent(flow, "legendary");
    out.push({ id: "legendary", type: "toggle", label: S.setupEpicPrep, value: has || flow.tools.legendary, enabled: !has,
      note: has ? S.setupInstalled : "", hint: S.setupEpicPrepHint });
    if (d.lutris === true) out.push({ id: "lutris", type: "info", label: S.setupLutris, value: S.setupPresent, note: S.setupLutrisNote });
    return out;
  }
  const inst = screenOf(s);
  s.onCircle = () => askLeave(flow, () => isTop(inst));
  load(flow).then(() => { if (inst.alive) s.list.setRows(rows()); });
  return inst;
}

// ---------------------------------------------------------------- S4 Konsolen
function sectionOf(emu) {
  const sec = String(emu.section || "");
  return /nintendo/i.test(sec) ? "nintendo" : /^ps/i.test(sec) ? "ps" : "sega";
}
function sizeOfIds(flow, ids, list) {
  return [...ids].reduce((sum, id) => { const x = byId(list, id); return sum + (x && !installedHow(flow, x) ? mb(x.size_mb) : 0); }, 0);
}

function createEmus(params = {}) {
  const flow = flowOf(params);
  if (params.only && !(flow.preset && flow.preset.kind === "only" && flow.preset.only === String(params.only))) preset(flow, "only", String(params.only));
  const s = stepScreen({ step: STEP["setup-emus"], title: S.setupEmusTitle, lead: S.setupEmusText, more: S.setupMoreText.emus,
    handlers: { onSet: (row, value) => toggle(row, value), onAction: (row) => bulk(row) } });
  s.setButtons([
    { id: "back", label: S.setupBack, press: () => prevStep(flow, "setup-emus") },
    { id: "next", label: S.setupNext, main: true, press: () => nextStep(flow, "setup-emus") },
  ]);
  s.list.setLoading();

  function emuRow(id, emu, label, section, hint) {
    const how = installedHow(flow, emu);
    const noFp = flatpakMissing(flow);
    const size = how ? S.setupInstalled : mbText(mb(emu.size_mb));
    return { id, type: "toggle", label, section: S.setupEmuSections[section] || section, value: !!how || flow.emus.has(emu.id),
      enabled: !how && !noFp, note: size ? fmt(S.setupProgSize, { prog: emu.title || emu.id, size }) : emu.title || emu.id,
      hint: how === "system" ? S.setupInstalledSystem : noFp ? S.setupNoFlatpakHint : hint || "", emu: emu.id };
  }
  function rows() {
    const list = emulators(flow);
    const noFp = flatpakMissing(flow);
    const rec = recommendedOf(flow, list).map((id) => (byId(list, id) || {}).title || id);
    const out = [];
    if (flow.failed) out.push({ id: "failed", type: "info", label: S.setupUnavailable, enabled: false });
    out.push({ id: "recommended", type: "action", label: S.setupRecommended, hint: rec.length ? fmt(S.setupRecommendedHint, { list: joinAnd(rec) }) : "", enabled: !noFp });
    out.push({ id: "none", type: "action", label: S.setupNone, hint: S.setupNoneHint, enabled: !noFp });
    const used = new Set();
    for (const def of EMU_ROWS) {
      const emu = byId(list, def.emu);
      if (!emu) continue;
      used.add(emu.id);
      out.push(emuRow("emu:" + def.id, emu, S.setupEmuRows[def.id] || emu.title, def.section, def.hint ? S[def.hint] : emu.hint));
    }
    // Was der Katalog darüber hinaus anbietet, steht in seinem Abschnitt
    for (const emu of list) if (!used.has(emu.id)) out.push(emuRow("emu:" + emu.id, emu, emu.title || emu.id, sectionOf(emu), emu.hint));
    return out;
  }
  function foot() {
    const ids = [...flow.emus].filter((id) => !installedHow(flow, byId(emulators(flow), id)));
    const size = sizeText(sizeOfIds(flow, ids, emulators(flow))) || "0 B";
    s.setFoot(!ids.length ? S.setupSelectedNone : ids.length === 1 ? fmt(S.setupSelectedEmu, { size }) : fmt(S.setupSelectedEmus, { n: ids.length, size }));
  }
  function toggle(row, value) {
    if (!row.emu) return undefined;
    if (value) flow.emus.add(row.emu); else flow.emus.delete(row.emu);
    foot();
    return { rows: rows() };                   // RetroArch steht in drei Zeilen: alle gleich zeigen
  }
  function bulk(row) {
    if (row.id === "recommended") flow.emus = new Set(recommendedOf(flow, emulators(flow)));
    else if (row.id === "none") flow.emus = new Set();
    else return undefined;
    foot();
    return { rows: rows() };
  }
  const inst = screenOf(s);
  s.onCircle = () => askLeave(flow, () => isTop(inst));
  load(flow).then(() => { if (!inst.alive) return; s.list.setRows(rows()); foot(); });
  return inst;
}

// ---------------------------------------------------------------- S5 Apps
function createApps(params = {}) {
  const flow = flowOf(params);
  if (params.only && !(flow.preset && flow.preset.kind === "only" && flow.preset.only === String(params.only))) preset(flow, "only", String(params.only));
  const s = stepScreen({ step: STEP["setup-apps"], title: S.setupAppsTitle, lead: S.setupAppsText, more: S.setupMoreText.apps,
    handlers: { onSet: (row, value) => { if (row.app) { if (value) flow.apps.add(row.app); else flow.apps.delete(row.app); } } } });
  s.setButtons([
    { id: "back", label: S.setupBack, press: () => prevStep(flow, "setup-apps") },
    { id: "next", label: S.setupNext, main: true, press: () => nextStep(flow, "setup-apps") },
  ]);
  s.list.setLoading();

  function appRow(a) {
    const how = installedHow(flow, a);
    const noFp = flatpakMissing(flow);
    return { id: "app:" + a.id, type: "toggle", label: a.title || a.id, value: !!how || flow.apps.has(a.id), enabled: !how && !noFp,
      note: how ? S.setupInstalled : mbText(mb(a.size_mb)),
      hint: how === "system" ? S.setupInstalledSystem : noFp ? S.setupNoFlatpakHint : a.hint || S.setupAppHints[a.id] || "", app: a.id };
  }
  function rows() {
    const out = [];
    if (flow.failed) out.push({ id: "failed", type: "info", label: S.setupUnavailable, enabled: false });
    const browser = { id: "browser", type: "info", label: S.setupBrowser, value: S.setupBuiltIn };
    let placed = false;
    for (const a of appsOf(flow)) {
      // Chrome ist nur der Ersatz für den eingebauten Browser: darüber steht, dass es ihn gibt
      if (a.id === "chrome" && !placed) { out.push(browser); placed = true; }
      out.push(appRow(a));
    }
    if (!placed) out.push(browser);
    return out;
  }
  const inst = screenOf(s);
  s.onCircle = () => askLeave(flow, () => isTop(inst));
  load(flow).then(() => { if (inst.alive) s.list.setRows(rows()); });
  return inst;
}

// ---------------------------------------------------------------- S6 Windows-Spiele
function createTools(params = {}) {
  const flow = flowOf(params);
  const s = stepScreen({ step: STEP["setup-tools"], title: S.setupToolsTitle, lead: S.setupToolsText, more: S.setupMoreText.tools,
    handlers: { onSet: (row, value) => { if (row.id === "umu") flow.tools.umu = !!value; }, onPress: (row) => pick(row) } });
  s.setButtons([
    { id: "back", label: S.setupBack, press: () => prevStep(flow, "setup-tools") },
    { id: "next", label: S.setupNext, main: true, press: () => nextStep(flow, "setup-tools") },
  ]);
  s.list.setLoading();

  // Proton-Fassung als Auswahl in der Liste (eine Zeile je Fassung, Haken an der gewählten): ganz
  // lesbar, ohne Auswahlfenster
  function rows() {
    const out = [];
    if (toolPresent(flow, "umu")) out.push({ id: "umu", type: "info", label: S.setupUmu, value: S.setupPresent });
    else out.push({ id: "umu", type: "toggle", label: S.setupUmu, value: !!flow.tools.umu, note: S.setupUmuLoad, hint: S.setupUmuHint });
    if (toolPresent(flow, "proton")) {
      out.push({ id: "proton", type: "info", label: S.setupProton, value: S.setupPresent });
      return out;
    }
    const section = S.setupProton;
    for (const f of protonFamilies(flow)) {
      out.push({ id: "proton:" + f.id, type: "action", label: f.label, icon: flow.tools.proton === f.id ? "check" : "update", hint: S.setupProtonHint, proton: f.id, section });
    }
    out.push({ id: "proton:", type: "action", label: S.setupProtonNone, icon: flow.tools.proton ? "close" : "check", proton: "", section });
    return out;
  }
  function pick(row) {
    if (row.proton === undefined) return false;
    flow.tools.proton = row.proton;
    s.list.setRows(rows());
    return true;
  }
  const inst = screenOf(s);
  s.onCircle = () => askLeave(flow, () => isTop(inst));
  load(flow).then(() => { if (inst.alive) s.list.setRows(rows()); });
  return inst;
}

// ---------------------------------------------------------------- S7 Zusammenfassung
// Ordner relativ zum Emulator-Ordner; lange Listen gekürzt wie im Bauplan („roms/gc, roms/wii, …, bios“)
function folderNames(folders, root) {
  const base = String(root || "").replace(/\/+$/, "");
  return (Array.isArray(folders) ? folders : []).map((f) => String(f || "")).filter(Boolean)
    .map((f) => (base && f.startsWith(base + "/") ? f.slice(base.length + 1) : f));
}
function shortList(names) {
  return names.length <= 4 ? names.join(", ") : [...names.slice(0, 3), "…", names[names.length - 1]].join(", ");
}
function planItems(plan, flow) {
  const raw = Array.isArray(plan.items) ? plan.items : chosenItems(flow);
  return raw.map((it) => {
    const o = typeof it === "string" ? { id: it } : it && typeof it === "object" ? it : {};
    const id = String(o.id || o.ref || "");
    const cat = byId(emulators(flow), id) || byId(appsOf(flow), id);
    const github = o.source ? o.source === "github" : /^(umu|legendary|proton)(:|$)/.test(id);
    return {
      id, title: o.title || titleOf(flow, id), source: github ? "github" : "flathub",
      size: num(o.installed) || num(o.download) || mb(o.size_mb) || mb(cat && cat.size_mb),
      skip: !!(o.skip || o.skipped || o.state === "skip" || o.state === "skipped" || o.state === "installed"),
    };
  }).filter((i) => i.id);
}
function runtimesOf(plan) {
  return (Array.isArray(plan.runtimes) ? plan.runtimes : []).map((r) => (typeof r === "string" ? { title: r } : r && typeof r === "object" ? r : null))
    .filter(Boolean).map((r) => ({ title: String(r.title || r.ref || r.id || ""), size: num(r.installed) || num(r.download) || mb(r.size_mb) }));
}

function createSummary(params = {}) {
  const flow = flowOf(params);
  let plan = null;
  let busy = false;
  let first = true;
  // Rechts das Ergebnis (Gesamt, frei, Warnung): sichtbar, ohne durch die Liste zu rollen
  const side = h("div.setup-side");
  const s = stepScreen({ step: STEP["setup-summary"], title: S.setupSummaryTitle, more: S.setupMoreText.summary, side });
  s.setButtons([
    { id: "back", label: S.setupBack, press: () => prevStep(flow, "setup-summary") },
    { id: "install", label: S.setupInstall, main: true, press: () => install() },
  ]);
  s.setDisabled("install", true);
  s.setFoot(S.setupSummaryHint);
  s.list.setLoading(0, S.setupComputing);

  function rows() {
    const p = plan || {};
    const out = [];
    const root = emuRoot(flow);
    if (p.ok === false) out.push({ id: "planfail", type: "info", label: p.message || S.setupUnavailable, enabled: false });
    const folders = folderNames(p.folders, root);
    out.push({ id: "f-games", type: "info", label: S.setupGamesFolder, value: tilde(flow.place), more: flow.place, section: S.setupFolders });
    out.push({ id: "f-emu", type: "info", label: S.setupEmuGamesFolder, value: tilde(root), section: S.setupFolders,
      note: folders.length ? fmt(S.setupNewFolders, { list: shortList(folders) }) : "",
      more: folders.length ? root + "\n" + fmt(S.setupNewFolders, { list: folders.join(", ") }) : root });
    const items = planItems(p, flow);
    const rts = runtimesOf(p);
    for (const it of items.filter((i) => i.source === "flathub")) {
      out.push({ id: "it:" + it.id, type: "info", label: it.title, value: it.skip ? S.setupSkipped : mbText(it.size), section: S.setupFromFlathub });
    }
    if (rts.length) {
      out.push({ id: "runtimes", type: "info", label: S.setupRuntimes, value: fmt(S.setupRuntimesValue, { n: rts.length, size: sizeText(rts.reduce((a, r) => a + r.size, 0)) || "?" }),
        hint: S.setupRuntimesHint, more: rts.map((r) => r.title).filter(Boolean).join(", "), section: S.setupFromFlathub });
    }
    for (const it of items.filter((i) => i.source === "github")) {
      out.push({ id: "it:" + it.id, type: "info", label: it.title, value: it.skip ? S.setupSkipped : mbText(it.size), section: S.setupFromGithub });
    }
    return out;
  }
  const fact = (label, value) => h("div.setup-fact", h("div.setup-fact-label", { text: label }), h("div.setup-fact-value", { text: value }));
  function paintSide() {
    const p = plan || {};
    const parts = [];
    // Die Warnung sperrt „Installieren“ und steht deshalb ganz oben
    if (p.enough === false) parts.push(h("div.setup-missing", { text: fmt(S.setupNoSpace, { size: sizeText(p.missing) || "?" }) }));
    parts.push(h("div.setup-side-title", { text: S.setupTotal }));
    if (!planItems(p, flow).some((i) => !i.skip)) {
      parts.push(h("div.setup-tip", { text: S.setupNothing }));
    } else {
      const t = p.totals || {};
      parts.push(fact(S.setupDownload, fmt(S.setupApprox, { size: sizeText(t.download) || "?" })),
        fact(S.setupAfter, fmt(S.setupApprox, { size: sizeText(t.installed) || "?" })));
    }
    // Flatpaks liegen immer im persönlichen Ordner; ein anderer Spiele-Ort steht extra da
    const free = p.free || {};
    if (free.home !== undefined && free.home !== null) parts.push(fact(S.setupFreeHome, sizeText(free.home) || "0 B"));
    if (free.games !== undefined && free.games !== null && free.games !== free.home) parts.push(fact(S.setupFreeGames, sizeText(free.games) || "0 B"));
    side.replaceChildren(...parts);
  }

  async function compute() {
    await load(flow);
    let result;
    try { result = await call("setup.plan", request(flow)); } catch (e) { result = { ok: false, message: (e && e.message) || S.setupUnavailable }; }
    if (!inst.alive) return;
    plan = result && typeof result === "object" ? result : { ok: false, message: S.setupUnavailable };
    s.list.setRows(rows());
    paintSide();
    const blocked = plan.enough === false;
    s.setDisabled("install", blocked);
    // Der schnelle Weg: Kreuz auf „Installieren“; ist es gesperrt, steht der Fokus auf „Zurück“
    if (first && isTop(inst)) { first = false; nav.focus(s.button(blocked ? "back" : "install")); }
  }

  // D3: keine Verbindung (vorher erkannt oder vom Kern gemeldet); „Erneut versuchen“ fragt neu nach
  async function install() {
    if (busy || !plan || plan.enough === false) return;
    busy = true;
    try {
      for (;;) {
        const req = request(flow);
        if (!req.items.length || isOnline(flow)) {
          let r;
          try { r = await call("setup.start", req); } catch (e) { r = { ok: false, message: e && e.message }; }
          if (!inst.alive) return;
          if (r && r.ok) {
            if (isTop(inst)) router.replace("setup-progress", { flow, jobId: String(r.jobId || ""), items: planItems(plan, flow).map((i) => ({ id: i.id, title: i.title, state: i.skip ? "skipped" : "waiting" })) });
            return;
          }
          if (!(r && (r.code === "no-network" || r.code === "offline"))) { await failDialog(r); return; }
        }
        const choice = await dialog({ title: S.setupOfflineTitle, text: S.setupOfflineText, initial: "retry",
          buttons: [{ id: "retry", label: S.setupRetry }, { id: "network", label: S.setupNetwork }, { id: "later", label: S.setupLater }] });
        if (!isTop(inst)) return;
        if (choice === "network") { openNetwork("network"); return; }
        if (choice === "later") { leaveFlow(flow); return; }
        if (choice !== "retry") return;
        const d = await ask("setup.detect");
        if (d) flow.detect = d;
        if (!isTop(inst)) return;
      }
    } finally {
      busy = false;
    }
  }

  const inst = screenOf(s, {
    // Zurück aus den Netzwerk-Einstellungen: neu rechnen (Verbindung kann jetzt da sein)
    onShow(info) { if (info && info.back) ask("setup.detect").then((d) => { if (d && inst.alive) flow.detect = d; }); },
  });
  s.onCircle = () => askLeave(flow, () => isTop(inst));
  compute();
  return inst;
}

// ---------------------------------------------------------------- S8 Installieren
function etaText(seconds) {
  const m = Math.max(1, Math.ceil(num(seconds) / 60));
  if (m === 1) return S.setupEtaMinute;
  return m < 60 ? fmt(S.setupEtaMinutes, { m }) : fmt(S.setupEtaHours, { h: Math.floor(m / 60), m: m % 60 });
}
function itemRow(it) {
  const id = "it:" + it.id;
  const label = it.title || it.id;
  const st = String(it.state || "waiting");
  if (RUNNING.has(st)) {
    const n = Math.max(0, Math.min(100, Math.round(Number(it.percent) || 0)));
    const speed = num(it.speed) ? sizeText(it.speed) : "";
    return { id, type: "progress", label, value: n, valueText: speed ? fmt(S.setupStSpeed, { n, speed }) : fmt(S.setupStPercent, { n }), busy: !n };
  }
  if (st === "done" || st === "ok") return { id, type: "info", label, value: S.setupStDone };
  // message des Kerns steht darunter, wenn sie mehr sagt als der Wert („Schon auf diesem Gerät (systemweit).“)
  const note = (value) => { const m = String(it.message || "").replace(/\.$/, ""); return m && m !== value ? it.message : ""; };
  if (st === "skipped" || st === "already" || st === "installed") return { id, type: "info", label, value: S.setupSkipped, note: note(S.setupSkipped) };
  if (st === "failed" || st === "error") return { id, type: "info", label, value: S.setupStFailed, note: it.message || "" };
  if (st === "cancelled") return { id, type: "info", label, value: S.setupStCancelled, note: note(S.setupStCancelled) };
  return { id, type: "info", label, value: S.setupStWaiting };
}

function createProgress(params = {}) {
  const flow = flowOf(params);
  let jobId = String(params.jobId || "");
  let items = (Array.isArray(params.items) ? params.items : []).filter((i) => i && i.id);
  let done = 0;
  let total = items.length;
  let eta = 0;
  let started = 0;
  const seen = Date.now() / 1000;
  let finished = false;
  let asking = false;
  let pending = null;
  const s = stepScreen({ step: STEP["setup-progress"], title: S.setupProgressTitle, lead: fmt(S.setupProgressSub, { done: 0, total }), more: S.setupMoreText.progress });
  s.setButtons([
    { id: "cancel", label: S.setupCancel, press: () => cancel() },
    { id: "bg", label: S.setupBackground, main: true, press: () => background() },
  ]);
  // Gesamtfortschritt unter der Unterzeile (Fertiges zählt ganz, Laufendes mit seinem Stand)
  const bar = h("div.progress.setup-overall", h("i"));
  s.el.querySelector(".flow-sub").after(bar);

  function subText() {
    const n = total || items.length;
    const spent = Date.now() / 1000 - (started || seen);
    if (eta > 0 && spent >= REMAINING_AFTER_S && done < n) return fmt(S.setupProgressEta, { done, total: n, t: etaText(eta) });
    return fmt(S.setupProgressSub, { done, total: n });
  }
  function overall() {
    if (!items.length) return 0;
    const sum = items.reduce((a, i) => {
      const st = String(i.state || "");
      return a + (st === "done" || st === "ok" || st === "skipped" ? 100 : RUNNING.has(st) ? Math.max(0, Math.min(100, Number(i.percent) || 0)) : 0);
    }, 0);
    return sum / items.length / 100;
  }
  function paint() {
    s.setLead(subText());
    bar.style.setProperty("--p", overall());
    if (!items.length) { if (!s.list.loading) s.list.setLoading(); return; }
    s.list.setRows(items.map(itemRow));
    // Ohne Fokus in der Liste steht der laufende Eintrag im Bild
    const run = items.find((i) => RUNNING.has(String(i.state)));
    if (run && !(nav.current && s.list.el.contains(nav.current))) s.list.show("it:" + run.id);
  }
  function apply(p) {
    if (Array.isArray(p.items)) items = p.items.filter((i) => i && i.id).map((i) => ({ ...i, id: String(i.id) }));
    if (Number.isFinite(Number(p.done))) done = Number(p.done);
    if (Number.isFinite(Number(p.total))) total = Number(p.total);
    eta = num(p.eta);
    if (num(p.started)) started = num(p.started);
    paint();
  }
  // „Im Hintergrund weiter“: zurück ins Home (Bauplan 8.1), der Auftrag läuft weiter
  function background() {
    toast({ title: S.setupName, body: S.setupBackgroundToast, icon: "download", sound: false });
    if (!router.backTo("home")) router.back();
  }
  // D2; kommt das Ende, während die Rückfrage offen ist, geht es danach weiter
  async function cancel() {
    if (asking || finished) return;
    asking = true;
    const yes = await confirm({ title: S.setupCancelTitle, text: S.setupCancelText, ok: S.setupCancelOk, cancel: S.setupCancelKeep });
    asking = false;
    if (pending) { const p = pending; pending = null; finish(p); return; }
    if (!yes || finished || !inst.alive) return;
    try {
      const r = await call("setup.cancel", { jobId });
      if (r && r.ok === false) toast({ title: S.setupName, body: r.message || S.errorGeneric, icon: "warn", sound: "error" });
    } catch (e) {
      toast({ title: S.setupName, body: (e && e.message) || S.errorGeneric, icon: "warn", sound: "error" });
    }
  }
  function finish(p) {
    if (asking) { pending = p; return; }
    finished = true;
    if (isTop(inst)) router.replace("setup-done", { flow, result: p });
  }
  const mine = (p) => p && typeof p === "object" && (!jobId || !p.jobId || String(p.jobId) === jobId);
  const offs = [
    on("setup.progress", (p) => {
      if (finished || !mine(p)) return;
      if (!jobId && p.jobId) jobId = String(p.jobId);
      apply(p);
    }),
    on("setup.done", (p) => { if (!finished && mine(p)) finish(p); }),
  ];
  const inst = screenOf(s, { destroy() { offs.forEach((off) => off()); } });
  s.onCircle = () => background();
  if (!jobId) { const job = setupJob(); if (job) jobId = String(job.id); }
  paint();
  return inst;
}

// ---------------------------------------------------------------- S9 Fertig
// „RetroArch und PCSX2 (keine Verbindung)“: je Grund eine Gruppe; Laufzeiten und Ordner sind nur Mittel
// zum Zweck und stehen nur da, wenn sonst nichts fehlt
function missingText(failed) {
  const groups = new Map();
  const add = (f) => {
    const why = (S.setupWhy || {})[f.code] || f.message || "";
    if (!groups.has(why)) groups.set(why, []);
    groups.get(why).push(f.title || String(f.id || ""));
  };
  const main = failed.filter((f) => !/^(runtime:|folders$)/.test(String(f.id || "")));
  (main.length ? main : failed).forEach(add);
  return [...groups].map(([why, titles]) => joinAnd(titles) + (why ? " (" + why + ")" : "")).join("; ");
}

function createDone(params = {}) {
  const flow = flowOf(params);
  const result = params.result && typeof params.result === "object" ? params.result : {};
  const failed = (Array.isArray(result.failed) ? result.failed : []).filter((f) => f && typeof f === "object");
  const complete = result.ok !== false && !result.cancelled && !failed.length;
  const side = h("div.setup-side");
  const s = stepScreen({ step: TOTAL + 1, title: complete ? S.setupDoneTitle : S.setupDonePartial, lead: S.setupDoneText,
    more: S.setupMoreText.done, side, emptyText: S.setupDoneNoSystems });
  s.setButtons([
    { id: "files", label: S.setupOpenFiles, press: () => { call("setup.finish").catch(() => {}); router.go("explorer"); } },
    { id: "home", label: S.setupToHome, main: true, press: () => home() },
  ]);
  s.list.setLoading();

  function home() {
    call("setup.finish").catch(() => {});
    router.backTo("home");
  }
  function rows(list) {
    const out = [];
    const root = emuRoot(flow);
    const seenSys = new Set();
    for (const emu of list) {
      for (const sys of systemsOf(emu)) {
        if (seenSys.has(sys.id)) continue;
        seenSys.add(sys.id);
        out.push({ id: "sys:" + sys.id, type: "info", label: (S.setupSystems || {})[sys.id] || sys.id, value: "…/roms/" + sys.id,
          note: sys.exts.length ? fmt(S.setupDoneExts, { exts: sys.exts.join(", ") }) : "",
          hint: needsBios(emu, sys) ? fmt(S.setupDoneBios, { emu: emu.title || emu.id }) : emu.library === false ? fmt(S.setupDoneOwn, { emu: emu.title || emu.id }) : "",
          more: root ? root.replace(/\/+$/, "") + "/roms/" + sys.id : "" });
      }
    }
    if (out.length && root) out.unshift({ id: "emuroot", type: "info", label: S.setupEmuFolder, value: tilde(root), more: root });
    return out;
  }
  function paintSide(list) {
    const parts = [];
    if (failed.length) parts.push(h("div.setup-missing", { text: fmt(S.setupDoneMissing, { list: missingText(failed) }) }));
    const tips = [S.setupTipUsb, S.setupTipUmu];
    if (list.some((e) => e.id === "retroarch")) tips.push(S.setupTipCores);
    parts.push(h("div.setup-side-title", { text: S.setupTips }), ...tips.map((t) => h("div.setup-tip", { text: t })));
    side.replaceChildren(...parts);
  }

  const inst = screenOf(s);
  s.onCircle = () => home();
  (async () => {
    await load(flow);
    const bad = new Set(failed.map((f) => String(f.id || "")));
    let list = [...flow.emus].map((id) => byId(emulators(flow), id)).filter((e) => e && !bad.has(e.id));
    // Ohne Wahl dieses Durchlaufs (geöffnet über ui.open): was jetzt installiert ist
    if (!flow.emus.size) {
      const fresh = await ask("setup.catalog");
      list = listOf(fresh && fresh.emulators).filter((e) => installedHow({ detect: {} }, e) && e.library !== false);
    }
    if (!inst.alive) return;
    const out = rows(list);
    // Emulatoren gewählt, aber keiner kam dazu (abgebrochen, gescheitert)
    if (!out.length && flow.emus.size) out.push({ id: "noemus", type: "info", label: S.setupDoneNoEmus });
    s.list.setRows(out);
    paintSide(list);
  })();
  return inst;
}

// ---------------------------------------------------------------- Einstieg über das Overlay
// ui.open {screen: "setup-…", params} (Kern ui_open, docs/RPC.md): z. B. aus einer Mitteilung
// „Einrichtung unterbrochen“ mit params.resume. Steht schon ein Schritt oben, ersetzt der neue ihn.
on("ui.open", (p) => {
  if (!p || typeof p.screen !== "string" || !p.screen.startsWith("setup-") || !router.known(p.screen)) return;
  const params = p.params && typeof p.params === "object" ? { ...p.params } : {};
  delete params.flow;
  if (router.top && String(router.top.id).startsWith("setup-")) router.replace(p.screen, params);
  else router.go(p.screen, params);
});

export const screens = [
  { id: "setup-welcome", create: createWelcome },
  { id: "setup-place", create: createPlace },
  { id: "setup-shops", create: createShops },
  { id: "setup-emus", create: createEmus },
  { id: "setup-apps", create: createApps },
  { id: "setup-tools", create: createTools },
  { id: "setup-summary", create: createSummary },
  { id: "setup-progress", create: createProgress },
  { id: "setup-done", create: createDone },
];
