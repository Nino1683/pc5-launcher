// Was man mit einem Bibliothekseintrag tun kann: starten (mit allen Rückfragen), schließen,
// Optionsmenü, Informationen, ausblenden, Startdatei wählen, Bilder, löschen. Home, Hub und
// Bibliothek benutzen dasselbe. Dazu der Fehlerdialog eines Starts und das Ein/Aus-Menü des Home.
import { call, on, role } from "../core/bridge.js";
import { cssMs, cssPx, settledRect } from "../core/dom.js";
import { S, fmt } from "../core/strings.js";
import { store } from "../core/store.js";
import { router } from "../core/router.js";
import { confirm, dialog, infoDialog, logDialog, CLOSED } from "./dialog.js";
import { openMenu } from "./menu.js";
import { toast } from "./toast.js";
import { askText } from "./osk-input.js";
import { openPowerMenu as powerMenu, switchToDesktopMode, finishInHome } from "../overlay/power-menu.js";

export function isRunning(entry) {
  return !!entry && (entry.running || store.isRunning(entry.id));
}

// Das Spiel wird gerade beendet (höflich gebeten, Schonfrist läuft). Es nach vorn zu holen wäre
// falsch: Es würde Sekunden später hart beendet. Ein Druck startet es stattdessen neu, sobald
// es weg ist (das regelt der Kern in app.launch).
export function isClosing(entry) {
  return !!entry && store.isClosing(entry.id);
}

// Steam-Spiele und der Steam-Client laufen nicht bei uns, sondern per Übergabe (Bauplan E9)
// Nur die Kachel „Steam“ übergibt an Steam Big Picture. Steam-Spiele starten wie jede App: Steam
// läuft dabei unsichtbar, die Konsole bleibt an (E57).
export function isHandover(entry) {
  return !!entry && entry.id === "app:steam";
}

// Archiv-Spiel, bei dem die Startdatei noch gewählt werden muss (der Kern meldet es als nicht verfügbar)
export function needsExe(entry) {
  return !!entry && (entry.tags || []).includes("needs-exe");
}

// E105 (Teil P5): Fehlende App bzw. ROM-Eintrag ohne Emulator mit Katalog-Kennung. Der Kern gibt sie
// als Marke "setup:<id>" in tags an die Seite (library/apps.py, library/roms.py; detail bleibt im Kern),
// setup_id oben am Eintrag nimmt die Seite auch an. Der Hauptknopf heißt dann „Installieren“.
export function setupTarget(entry) {
  if (!entry || entry.installed !== false) return "";
  const tag = (entry.tags || []).find((t) => typeof t === "string" && t.startsWith("setup:"));
  const id = entry.setup_id || (entry.detail && entry.detail.setup_id) || (tag ? tag.slice(6) : "");
  return typeof id === "string" && /^[a-z0-9][a-z0-9-]*$/.test(id) ? id : "";
}

// Schritt „Apps“ (App-Kachel) bzw. „Konsolen“ (ROM-Eintrag: Kennung eines Emulators) mit nur diesem
// Eintrag vorgewählt; aus dem Overlay über ui.open (screens/setup.js)
function openSetup(entry) {
  const screen = entry.kind === "app" ? "setup-apps" : "setup-emus";
  const params = { from: "tile", only: setupTarget(entry) };
  if (role === "home" && router.known(screen)) router.go(screen, params);
  else call("ui.open", { screen, params }).catch((e) => fail(e));
}

export function canStart(entry) {
  if (setupTarget(entry)) return true;          // E105: „Installieren“ ist bedienbar
  return !!entry && entry.installed !== false && (entry.available !== false || needsExe(entry));
}

// Beschriftung des Hauptknopfs
export function mainLabel(entry) {
  if (!entry) return "";
  if (entry.installed === false) return setupTarget(entry) ? S.setupTileInstall : S.notInstalled;
  if (needsExe(entry) && !isRunning(entry)) return S.chooseExe;
  if (entry.available === false) return S.unavailable;
  if (isClosing(entry)) return S.closing;
  if (isRunning(entry)) return S.resume;
  return entry.kind === "app" ? S.open : S.play;
}

const fail = (e, title) => toast({ title: title || S.error, body: (e && e.message) || S.errorGeneric, icon: "warn", sound: "error" });

// Was eine Übergabe an Steam beenden würde (E31): laufende Spiele nennt die Seite, alles Weitere
// (Installation, Papierkorb, Prefix-Kopie, Update) kommt als fertiger Satz vom Kern (power.menu.busy).
function runningText() {
  const running = store.state.running || [];
  return running.length === 1 ? fmt(S.powerConfirmRunning, { title: running[0].title })
    : running.length > 1 ? fmt(S.powerConfirmRunningMany, { n: running.length }) : "";
}
// Was die Übergabe beenden würde: laufende Spiele (Seite) und die Sätze des Kerns (power.menu.busy)
async function handoverBusy() {
  let busy = [];
  try { busy = ((await call("power.menu")) || {}).busy || []; } catch (e) { busy = []; }
  return [runningText(), ...busy].filter(Boolean);
}

// Hauptknopf: starten, nach vorn holen oder zuerst die Startdatei wählen
export function activateEntry(entry) {
  if (needsExe(entry) && !isRunning(entry)) return chooseExe(entry);
  return launchEntry(entry);
}

// Startet einen Eintrag oder holt ihn nach vorn. Liefert true, wenn der Start läuft.
export async function launchEntry(entry) {
  if (setupTarget(entry)) { openSetup(entry); return false; }     // E105: installieren statt starten
  if (!canStart(entry)) return false;
  try {
    if (isRunning(entry) && !isClosing(entry)) {
      await call("app.focus", { id: entry.id });
      return true;
    }
    // Übergabe an Steam: Die Seite fragt selbst (mit den Sätzen des Kerns) und meldet confirmed,
    // damit der Kern nicht ein zweites Mal fragt.
    let confirmed = false;
    let handover = false;
    if (isHandover(entry)) {
      // Vor Steam Big Picture wird immer gefragt (die Konsole endet dafür).
      const busy = await handoverBusy();
      if (entry.kind === "app" || busy.length) {
        const base = entry.kind === "app" ? S.steamTextApp : fmt(S.steamTextGame, { title: entry.title });
        const yes = await confirm({ title: entry.kind === "app" ? S.steamTitle : fmt(S.steamGameTitle, { title: entry.title }),
          text: [base, ...busy].join("\n"), ok: entry.kind === "app" ? S.steamOk : S.start });
        if (!yes) return false;
      }
      confirmed = true;
      handover = true;
      await showHandover(entry);
    }
    try {
      let result = await call("app.launch", { id: entry.id, confirmed });
      if (result && result.needsConfirm === "close-other") {
        const other = result.other || {};
        const yes = await confirm({
          title: fmt(S.startOtherTitle, { title: entry.title }),
          text: fmt(S.startOtherText, { other: other.title || S.unknown }),
          ok: S.start,
        });
        if (!yes) { if (handover) endHandover(entry.id); return false; }
        result = await call("app.launch", { id: entry.id, closeOther: true, confirmed });
      }
      if (handover && !(result && result.ok)) endHandover(entry.id);
      return !!(result && result.ok);
    } catch (e) {
      if (handover) endHandover(entry.id);
      throw e;
    }
  } catch (e) {
    // Startfehler (LaunchError) meldet der Kern immer auch als Ereignis app.error; dazu zeigt
    // showAppError genau einen Dialog. Alles andere (Brücke, Baustein ausgefallen) melden wir hier.
    if (!(e && e.error === "LaunchError")) await showAppError({ id: entry.id, title: entry.title, message: e && e.message, log: "", phase: "start" });
    return false;
  }
}

// Übergabe an Steam: Das Ladebild „Steam übernimmt jetzt …“ steht schon, bevor der Kern das Home
// beendet. Es ist das letzte Bild, bis Steam zeichnet (vorher blieb die Rückfrage eingefroren
// stehen, als wäre der Druck wirkungslos). screens/loading.js zeigt es; hier wird gewartet, bis
// es eingeblendet und gezeichnet ist.
function showHandover(entry) {
  document.dispatchEvent(new CustomEvent("couch-handover", { detail: { entry } }));
  return new Promise((resolve) => {
    let done = false;
    const finish = () => { if (!done) { done = true; resolve(); } };
    setTimeout(() => requestAnimationFrame(() => requestAnimationFrame(finish)), cssMs("--t-screen"));
    setTimeout(finish, cssMs("--t-screen") + 400);     // falls gerade nicht gezeichnet wird
  });
}

function endHandover(id) {
  document.dispatchEvent(new CustomEvent("couch-launch-end", { detail: { id } }));
}

// Zwei Sätze sagen dasselbe, wenn sie ohne Satzzeichen und Anführungszeichen gleich sind
// (auch für das Ladebild, screens/loading.js: eine Fassung statt zwei, E99)
export function sameSentence(a, b) {
  const norm = (t) => String(t || "").toLowerCase().replace(/[\s.…!?:„“"»«]+/g, " ").trim();
  return !!a && !!b && norm(a) === norm(b);
}

// Fehler eines Starts oder unerwartetes Ende (Ereignis app.error): ein Dialog. Die Überschrift
// unterscheidet „kam nicht zustande“ von „lief schon und ist abgestürzt“; das Protokoll gibt es
// nur, wenn eines da ist; fehlt die Startdatei, führt ein Knopf zur Auswahl.
export async function showAppError(p) {
  const title = p.title || S.unknown;
  const crashed = p.phase === "run" || p.code === "crashed";
  const head = fmt(crashed ? S.crashTitle : S.launchErrorTitle, { title });
  const exe = p.code === "needs-exe" && p.id;
  // E101: Proton beschädigt, ohne heile Ausweich-Version (wird neu geladen oder fehlt ganz):
  // ein Weg zu Einstellungen › System › Updates › Proton-Versionen (Fortschritt, Installieren)
  const proton = p.code === "proton-damaged" && role === "home";
  // Sagt der Satz des Kerns nur, was schon in der Überschrift steht („… wurde unerwartet beendet“),
  // steht er nicht noch einmal darunter; mit Protokoll weist die Zeile darauf hin
  let text = p.message || S.errorGeneric;
  if (sameSentence(text, head)) text = p.log ? S.crashLogHint : "";
  for (;;) {
    const buttons = [{ id: "ok", label: S.ok }];
    if (exe) buttons.push({ id: "exe", label: S.chooseExe });
    if (proton) buttons.push({ id: "proton", label: S.protonOpen });
    if (p.log) buttons.push({ id: "log", label: S.showLog });
    const result = await dialog({ title: head, text, buttons, initial: exe ? "exe" : "ok" });
    if (result === "exe") { await chooseExe({ id: p.id, title }); return; }
    if (result === "proton") { await openSettingsPage("system.proton", S.protonOpen, S.protonCrumb); return; }
    if (result !== "log") return;
    if (await logDialog({ title: fmt(S.logTitle, { title }), text: p.log }) === CLOSED) return;
  }
}

// Eine Einstellungsseite direkt öffnen (E101: „Proton-Versionen“ aus dem Fehlerdialog eines Starts).
// Wie aus den Einstellungen heraus: links die Bereiche als Wegweiser, der Bereich der Seite markiert;
// ohne Bereiche (Kern antwortet nicht) eine Spalte. Kreis führt zurück, wo man herkam.
export async function openSettingsPage(page, title, crumb) {
  let categories = [];
  try { categories = ((await call("settings.categories")) || {}).categories || []; } catch (e) { categories = []; }
  const area = String(page).split(".")[0];
  const context = categories.some((c) => c && c.id === area) ? { categories, selected: area } : null;
  return router.go("settings-page", { id: page, title: title || "", crumb: crumb || S.settings, context });
}

// Auswahl der Startdatei. ref: Kennung des Eintrags ("local:…") oder eines Auftrags ("job-…");
// candidates: schon bekannte Kandidaten (Auftrag), sonst fragt die Seite lib.exeCandidates.
export async function chooseExe(entry, { ref, candidates } = {}) {
  let list = candidates;
  if (!list) {
    try { list = (await call("lib.exeCandidates", { id: entry.id })).candidates || []; } catch (e) { fail(e, entry.title); return false; }
  }
  if (!list.length) { toast({ title: entry.title, body: S.exeNone, icon: "warn" }); return false; }
  const current = list.find((c) => c.current);
  const choice = await openMenu(
    list.map((c) => ({ id: c.path, label: c.path, note: c.current ? S.exeCurrent : (Array.isArray(c.why) ? c.why.join(", ") : "") })),
    { cls: "menu-center", title: fmt(S.exeTitle, { title: entry.title }), initial: current ? current.path : null });
  if (!choice) return false;
  try {
    const result = await call("install.setExe", { jobId: ref || entry.id, exe: choice });
    if (!result || result.ok === false) { fail(result, entry.title); return false; }
    toast({ title: entry.title, body: S.exeSaved, icon: "check", sound: false });
    return true;
  } catch (e) { fail(e, entry.title); return false; }
}

// Rückfrage „… schließen?“ und Schließen
export async function closeEntry(entry) {
  const yes = await confirm({
    title: entry.kind === "app" ? fmt(S.closeAppTitle, { title: entry.title }) : S.closeGameTitle,
    text: fmt(S.closeGameText, { title: entry.title }),
    ok: S.close,
  });
  if (!yes) return false;
  try {
    await call("app.close", { id: entry.id });
    return true;
  } catch (e) { fail(e); return false; }
}

async function setHidden(entry, hidden) {
  try {
    await call("lib.hide", { id: entry.id, hidden });
    toast({ title: S.library, body: fmt(hidden ? S.hiddenToast : S.unhiddenToast, { title: entry.title }), icon: hidden ? "eye-off" : "eye", sound: false });
  } catch (e) { fail(e); }
}

// Favorit (E99, lib.setFavorite): eigener Wert der Konsole, er übersteuert Steams Sammlung „Favoriten“
async function setFavorite(entry, favorite) {
  try {
    await call("lib.setFavorite", { id: entry.id, favorite });
    toast({ title: S.library, body: fmt(favorite ? S.favAddedToast : S.favRemovedToast, { title: entry.title }), icon: "star", sound: false });
  } catch (e) { fail(e, entry.title); }
}

// Umbenennen (E90): Bildschirmtastatur mit dem jetzigen Namen. Ein leerer Name oder „Namen
// zurücksetzen“ (title "") nimmt wieder den Namen der Quelle.
async function renameEntry(entry, title) {
  if (title === undefined) {
    let text = null;
    try {
      text = await askText({ title: fmt(S.renameTitle, { title: entry.title }), initial: entry.title || "" });
    } catch (e) { fail(e); return false; }
    if (text === null) return false;
    title = text.trim();
    if (title === entry.title) return false;
  }
  try {
    const result = await call("lib.rename", { id: entry.id, title });
    const now = (result && result.entry && result.entry.title) || title;
    toast({ title: S.library, body: fmt(title ? S.renamedToast : S.renameResetToast, { title: now }), icon: "check", sound: false });
    return true;
  } catch (e) { fail(e, entry.title); return false; }
}

// Eigenes Archiv-Spiel löschen: erst in den Papierkorb. Ist der Ordner schon weg, bietet der
// Kern an, nur den Eintrag zu entfernen (can_forget): zweite Rückfrage, dann forget.
export async function deleteEntry(entry) {
  const yes = await confirm({ title: fmt(S.delTitle, { title: entry.title }), text: S.delText, ok: S.delOk });
  if (!yes) return false;
  try {
    let result = await call("install.delete", { id: entry.id });
    // Von Hand hinzugefügtes Spiel (E91): Der Kern nennt den Ordner, erst nach dieser Rückfrage wandert er
    if (result && result.ok === false && result.code === "confirm-folder" && result.folder) {
      const sure = await confirm({ title: S.delFolderTitle, text: fmt(S.delFolderText, { title: entry.title }), detail: result.folder, ok: S.delFolderOk });
      if (!sure) return false;
      result = await call("install.delete", { id: entry.id, folder: result.folder });
    }
    if (result && result.ok === false && result.can_forget) {
      const forget = await confirm({ title: S.forgetTitle, text: fmt(S.forgetText, { message: result.message || "" }), ok: S.forgetOk });
      if (!forget) return false;
      result = await call("install.delete", { id: entry.id, forget: true });
    }
    if (!result || result.ok === false) { fail(result, entry.title); return false; }
    toast({ title: entry.title, body: result.message || "", icon: "trash", sound: false });
    return true;
  } catch (e) { fail(e, entry.title); return false; }
}

// Ordner von Hand gelöscht (E91): nur den Eintrag aus der Liste nehmen, auf der Platte geschieht nichts
async function forgetEntry(entry) {
  try {
    const result = await call("lib.forget", { id: entry.id });
    toast({ title: S.library, body: (result && result.message) || S.errorGeneric, icon: result && result.ok ? "trash" : "warn", sound: result && result.ok ? false : "error" });
    return !!(result && result.ok);
  } catch (e) { fail(e, entry.title); return false; }
}

// „Bilder neu laden“ für einen Eintrag (Netz; kann Sekunden dauern)
export async function refetchArt(entry) {
  toast({ title: entry.title, body: S.artFetchBusy, icon: "download", sound: false, stay: 1800 });
  try {
    const result = await call("lib.fetchArt", { id: entry.id });
    toast({ title: entry.title, body: (result && result.message) || S.errorGeneric, icon: result && result.ok ? "check" : "info", sound: false });
    return !!(result && result.ok);
  } catch (e) { fail(e, entry.title); return false; }
}

// Kennungen aus lib.get.actions, die ins Menü gehören (Starten und Fortsetzen macht der Hauptknopf)
const MENU_ACTIONS = new Set(["close", "settings", "exe", "art", "art-fetch", "rename", "rename-reset", "info", "saves", "forget",
  "favorite", "unfavorite", "hide", "unhide", "delete", "uninstall"]);

// Einträge des Optionsmenüs: was lib.get.actions nennt, in dieser Reihenfolge, nichts darüber
// hinaus. Antwortet der Kern nicht, bleibt die kleinste eigene Vorgabe.
function menuItems(entry, detail) {
  const fromCore = detail && Array.isArray(detail.actions) ? detail.actions.filter((a) => MENU_ACTIONS.has(a.id)) : null;
  if (fromCore && fromCore.length) {
    const items = [];
    for (const a of fromCore) {
      if ((a.id === "delete" || a.id === "uninstall") && items.length) items.push({ sep: true });
      items.push({ id: a.id, label: a.label });
    }
    return items;
  }
  const items = [];
  if (isRunning(entry)) items.push({ id: "close", label: entry.kind === "game" ? S.menuCloseGame : S.menuClose });
  items.push({ id: "info", label: S.menuInfo });
  return items;
}

// Lage der Tafel an einem Element (Fokuskachel, Cover, „…“): rechts daneben, am rechten Rand links
// daneben, Oberkante bündig, immer ganz im Bild (07 6.7: Tafel bei x=356, y=126 neben der 168er-Kachel)
function placeAt(panel, anchor) {
  const a = settledRect(anchor);
  const gap = cssPx("--menu-anchor-gap");
  const margin = cssPx("--picker-view-margin");
  let left = a.right + gap;
  if (left + panel.offsetWidth > innerWidth - margin) left = a.left - gap - panel.offsetWidth;
  const top = Math.max(margin, Math.min(innerHeight - margin - panel.offsetHeight, a.top));
  panel.style.left = Math.max(margin, left) + "px";
  panel.style.top = top + "px";
}

// Optionsmenü eines Eintrags (07 Abschnitt 6.7). Die Tafel steht an der gewählten Kachel (anchor)
// und nennt oben, um welches Spiel es geht.
// Steam-Spiel deinstallieren (E69): Steam wird dafür kurz beendet, die Dateien kommen in den Papierkorb.
// Epic-Spiele (E73): legendary löscht den Spielordner, der Prefix mit den Spielständen bleibt
// launcher: Battle.net, Ubisoft, Rockstar (E94) – deinstalliert wird im Launcher, der sich dafür öffnet
async function uninstallStore(entry, launcher = "") {
  const text = launcher ? fmt(S.uninstallTextLauncher, { store: S.stStores[launcher] || launcher })
    : entry.source === "local" ? S.uninstallTextEpic : S.uninstallText;
  const yes = await confirm({ title: fmt(S.uninstallTitle, { title: entry.title }), text, ok: launcher ? S.uninstallOkLauncher : S.uninstallOk });
  if (!yes) return false;
  try {
    // Epic-Spiele (E73) sind eigene Einträge mit legendary-Namen; der Kern sucht ihn über die Eintrags-ID
    const epic = entry.source === "local";
    const r = await call("store.uninstall", launcher ? { store: launcher, entry: entry.id, title: entry.title }
      : epic ? { store: "epic", entry: entry.id, title: entry.title }
        : { store: "steam", id: String(entry.id).split(":")[1] || "", title: entry.title });
    toast({ title: entry.title, body: (r && r.message) || S.errorGeneric, icon: r && r.ok ? "trash" : "warn", sound: r && r.ok ? false : "error" });
    return !!(r && r.ok);
  } catch (e) { fail(e, entry.title); return false; }
}

// extra: zusätzliche Punkte des Aufrufers (Home: „Kachel verschieben“), vor „Ausblenden“; ihre
// Wahl geht an onExtra(id)
export async function openEntryMenu(entry, { anchor = null, extra = [], onExtra = null } = {}) {
  if (!entry || entry.system) return;
  let detail = null;
  try { detail = await call("lib.get", { id: entry.id }); } catch (e) { detail = null; }
  const current = (detail && detail.entry) || entry;
  const items = menuItems(current, detail);
  if (extra.length) {
    const at = items.findIndex((i) => i.id === "hide" || i.id === "unhide");
    items.splice(at < 0 ? items.length : at, 0, ...extra);
  }
  const choice = await openMenu(items, {
    cls: "menu-entry", title: current.title,
    onOpen: (layer) => { if (anchor && anchor.isConnected) placeAt(layer.el.querySelector(".menu"), anchor); },
  });
  switch (choice) {
    case "close": await closeEntry(current); break;
    case "settings": router.go("game-settings", { id: current.id, title: current.title }); break;
    case "exe": await chooseExe(current); break;
    case "art": router.go("art-picker", { entry: current, fetch: (detail.actions || []).some((a) => a.id === "art-fetch") }); break;
    case "art-fetch": await refetchArt(current); break;
    case "rename": await renameEntry(current); break;
    case "rename-reset": await renameEntry(current, ""); break;
    case "info": {
      // Neu holen: Den Ordner der Spielstände sucht der Kern erst nach dem ersten lib.get (E63)
      let fresh = detail;
      try { fresh = (await call("lib.get", { id: current.id })) || detail; } catch (e) { fresh = detail; }
      await infoDialog({ title: current.title, items: (fresh && fresh.info) || [] });
      break;
    }
    case "saves": router.go("saves", { id: current.id, title: current.title }); break;
    case "favorite": await setFavorite(current, true); break;
    case "unfavorite": await setFavorite(current, false); break;
    case "hide": await setHidden(current, true); break;
    case "unhide": await setHidden(current, false); break;
    case "forget": await forgetEntry(current); break;
    case "delete": await deleteEntry(current); break;
    case "uninstall": await uninstallStore(current, ((detail && detail.actions) || []).find((a) => a.id === "uninstall")?.store || ""); break;
    default:
      if (choice && onExtra && extra.some((i) => i.id === choice)) onExtra(choice);
  }
}

// Ein/Aus-Menü des Home (Symbol in der Kopfzeile): derselbe Baustein wie im Control Center
// (overlay/power-menu.js), mit der Erklärung zum Eintrag im Fokus, frischem Satz des Kerns vor der
// Rückfrage und dem Hinweis „Die Konsole wird ausgeschaltet …“ bis zum Ende. „Steam Big Picture
// öffnen“ fragt hier immer kurz nach und nennt den Rückweg.
export function openPowerMenu(anchor) {
  const rect = anchor && anchor.isConnected ? settledRect(anchor) : { right: innerWidth - cssPx("--m-status-right") };
  return powerMenu({ anchor: rect, placement: "top", askSteam: true, fail: (e) => fail(e, S.homePower) });
}

// Nebenknopf „Zum Desktop-Modus wechseln“ (Hub der App „Desktop“)
export function desktopMode() {
  return switchToDesktopMode({ fail: (e) => fail(e, S.homePower) });
}

// Startet einen Eintrag nach seiner Kennung (Startdatei aus den Downloads oder dem Datei-Explorer,
// E93): erst den frischen Stand holen, dann der gewohnte Weg mit allen Rückfragen
export async function startById(id) {
  let detail = null;
  try { detail = await call("lib.get", { id }); } catch (e) { fail(e); return false; }
  if (!detail || !detail.entry) return false;
  return activateEntry(detail.entry);
}

// Bildschirme, die das Overlay im Home öffnen lässt (RPC ui.open, docs/RPC.md): Ausschalten usw.
// aus dem Control Center, während eine App läuft (overlay/power-menu.js handToHome), die Prüfung
// eines Archivs, die Einstellungen. Unbekannte Namen übergeht die Seite.
if (role === "home") {
  on("ui.open", (p) => {
    if (p && p.screen === "power-ending") finishInHome(p.params || {}, { fail: (e) => fail(e, S.homePower) });
    // Archiv aus dem Browser (Control Center › Downloads, E59): gleich zur Prüfung des Installers
    else if (p && p.screen === "install-check" && p.params && p.params.path) {
      router.go("install-check", { path: String(p.params.path), name: String(p.params.name || "") });
    }
    // „Starten“ eines Programms aus Control Center › Downloads (E93)
    else if (p && p.screen === "launch" && p.params && p.params.id) startById(String(p.params.id));
    // Zahnrad im Control Center: die Einstellungen, sofern sie nicht schon oben stehen
    else if (p && p.screen === "settings" && !(router.top && router.top.id === "settings")) router.go("settings");
  });
}
