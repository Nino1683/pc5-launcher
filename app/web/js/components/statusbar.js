// Statusleiste oben rechts: Zahnrad (nur im Home), Netz, Akku des Controllers, Uhr.
// Die Uhr tickt im Minutentakt; sonst zeichnet hier nichts neu.
//   Uhr           24 Stunden oder 12 Stunden mit AM/PM (state.ui.clock24h, Einstellungen › Home-Bildschirm)
//   Netz          Kabel oder WLAN; „limited“ (verbunden, kein Internet) gedimmt mit Warnfarbe, sonst durchgestrichen
//   Zahnrad       blauer Punkt, sobald ein System-Update bereitsteht (state.update_available)
//   Mond          Ruhemodus-Timer läuft (state.sleep_timer, E107): Mond und Restzeit „42 Min.“, nur Anzeige
import { h, icon, setIcon } from "../core/dom.js";
import { params } from "../core/bridge.js";
import { store } from "../core/store.js";
import { S, fmt } from "../core/strings.js";

const SVG_NS = "http://www.w3.org/2000/svg";

// Akku als eigenes kleines Bild: Rahmen, Pol und eine Füllung nach Stand
export function batteryIcon(level, charging) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "ic battery" + (charging ? " is-charging" : "") + (level !== null && level <= 15 && !charging ? " is-low" : ""));
  svg.setAttribute("viewBox", "0 0 24 24");
  const frame = document.createElementNS(SVG_NS, "rect");
  for (const [k, v] of Object.entries({ x: 2.5, y: 7.5, width: 17, height: 9, rx: 1.8, fill: "none", stroke: "currentColor", "stroke-width": 2 })) frame.setAttribute(k, v);
  const pole = document.createElementNS(SVG_NS, "path");
  pole.setAttribute("d", "M22 10.5v3");
  for (const [k, v] of Object.entries({ fill: "none", stroke: "currentColor", "stroke-width": 2, "stroke-linecap": "round" })) pole.setAttribute(k, v);
  const fill = document.createElementNS(SVG_NS, "rect");
  const pct = level === null || level === undefined ? 0 : Math.max(0, Math.min(100, level));
  for (const [k, v] of Object.entries({ x: 5, y: 10, width: Math.round(12 * pct) / 100, height: 4, rx: 0.6, fill: "currentColor" })) fill.setAttribute(k, v);
  svg.append(frame, pole, fill);
  return svg;
}

// network.state laut docs/RPC.md: connected | limited | offline | unknown
export function networkUp(network) {
  return !!network && (network.state === "connected" || network.state === "limited");
}

export function networkIcon(network) {
  if (!networkUp(network)) return "network-off";
  return network.type === "wifi" ? "network" : "lan";
}

// "20:15" oder "8:15 PM"
export function clockLabel(date, h24) {
  const minutes = String(date.getMinutes()).padStart(2, "0");
  if (h24) return String(date.getHours()).padStart(2, "0") + ":" + minutes;
  const hours = date.getHours();
  return (hours % 12 || 12) + ":" + minutes + " " + (hours < 12 ? S.clockAm : S.clockPm);
}

// Feste Uhrzeit für Prüf-Fotos (?clock=20:15); folgt trotzdem dem Zeitformat
function fixedDate(text) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(text || "");
  if (!m) return null;
  const d = new Date(2026, 0, 1, Number(m[1]), Number(m[2]), 0, 0);
  return Number.isNaN(d.getTime()) ? null : d;
}

// Zustand des Netzes als kurzer Text: Name des WLAN bzw. „Kabel (LAN)“, sonst was fehlt
export function networkLabel(network) {
  const n = network || {};
  if (n.state === "limited") return S.netLimited;
  if (n.state === "offline") return S.netOffline;
  if (n.state !== "connected") return "";
  if (n.type === "wifi") return n.name ? String(n.name) : S.netWifi;
  return n.type === "ethernet" ? S.netEthernet : (n.name ? String(n.name) : S.netConnected);
}

// Was gerade lädt: Installationen, Downloads des Stores und des Browsers. null, wenn nichts läuft,
// sonst {percent} des ersten mit bekanntem Stand (null: Stand noch unbekannt)
const JOB_ACTIVE = new Set(["queued", "running", "finishing", "installing"]);    // installing: ohne Anteil (E114)
export function activeTransfer(state) {
  const items = [];
  for (const j of state.store_jobs || []) items.push(Number(j.percent) || null);
  for (const j of state.jobs || []) if (JOB_ACTIVE.has(j.state)) items.push(j.state === "running" ? Number(j.percent) || 0 : null);
  for (const d of state.downloads || []) {
    if (d.state === "running" || d.state === "queued") items.push(d.state === "running" && d.total > 0 ? Number(d.percent) || 0 : null);
  }
  if (!items.length) return null;
  const known = items.find((p) => p !== null);
  return { percent: known === undefined ? null : Math.round(known) };
}

// E107: Restzeit des Ruhemodus-Timers in ganzen Minuten, aufgerundet und mindestens 1 (wie die Notiz
// „Noch 42 Min.“ im Ein/Aus-Menü, power.timer_note); until in Unix-Sekunden. null: kein Timer
export function timerMinutesLeft(timer, now = Date.now()) {
  const until = timer && Number(timer.until);
  if (!until) return null;
  return Math.max(1, Math.ceil((until * 1000 - now) / 60000));
}

// withGear: Zahnrad zeigen (Home); onSettings: wird bei Kreuz auf dem Zahnrad gerufen;
// netText: neben dem Netz-Symbol steht sein Zustand als Text (Control Center)
// Eine Anordnung für alle Bildschirme (07 11.4): rechtsbündig bis x=1832 stehen von rechts Uhr,
// Netz, Akku des Controllers, dann (nur im Home) Download, Ein/Aus und Zahnrad. Das Netz-Symbol
// steht so überall an derselben Stelle; ein Text zum Netz (Control Center) steht links von den Symbolen.
// extra: Elemente zwischen Zahnrad und Netz (Ein/Aus im Home)
// downloads: Symbol mit Stand, solange etwas lädt (Wunsch 2026-10-21; nur Anzeige, die Liste steht
// im Control Center unter „Downloads“)
export function createStatusbar({ withGear = false, onSettings, netText = false, extra = null, downloads = false } = {}) {
  const clock = h("span.status-clock");
  const net = icon("lan");
  const netLabel = netText ? h("span.status-net.ell") : null;
  const mini = h("div.status-mini", netLabel, net);
  let pad = null;
  const gear = withGear ? h("div.status-gear#status-gear", { "data-focus": true }, icon("gear"), h("span.status-dot")) : null;
  if (gear && onSettings) gear.addEventListener("press", onSettings);
  const dlPct = h("span.status-dl-pct");
  const dl = downloads ? h("div.status-dl", { hidden: true }, icon("download"), dlPct) : null;
  // E107: Ruhemodus-Timer links vom Download-Symbol (bzw. vom Netz, wo es keines gibt); Aussehen wie das
  // Download-Symbol (home.css .status-timer, Werte aus tokens.css Block E107-timer)
  const moonLeft = h("span.status-timer-left");
  const moon = h("div.status-timer", { hidden: true }, icon("moon"), moonLeft);
  const el = h("div.status" + (netText ? ".has-net-text" : ""), gear, extra, moon, dl, mini, clock);
  const fixed = fixedDate(params.get("clock"));
  let moonTimer = null;

  // Neu zeichnen, sobald die Restzeit um eine Minute fällt (eigener Wecker, die Uhr tickt zur vollen Minute)
  function paintTimer(state) {
    clearTimeout(moonTimer);
    moonTimer = null;
    const left = timerMinutesLeft(state.sleep_timer);
    moon.hidden = left === null;
    if (left === null) return;
    moonLeft.textContent = fmt(S.timerLeft, { n: left });
    const rest = Number(state.sleep_timer.until) * 1000 - Date.now();
    if (rest > 60000) moonTimer = setTimeout(() => paintTimer(store.state), (rest % 60000 || 60000) + 20);
  }

  function tick() {
    clock.textContent = clockLabel(fixed || new Date(), store.ui.clock24h !== false);
  }
  function schedule() {
    const now = new Date();
    timer = setTimeout(() => { tick(); schedule(); }, (60 - now.getSeconds()) * 1000 - now.getMilliseconds() + 20);
  }
  function update(state) {
    setIcon(net, networkIcon(state.network));
    net.classList.toggle("is-off", !networkUp(state.network));
    net.classList.toggle("is-warn", !!state.network && state.network.state === "limited");
    if (netLabel) {
      netLabel.textContent = networkLabel(state.network);
      netLabel.classList.toggle("is-warn", !!state.network && state.network.state === "limited");
    }
    const first = (state.pads || [])[0];
    const next = first && first.battery !== null && first.battery !== undefined ? batteryIcon(first.battery, first.charging) : null;
    if (pad) pad.remove();
    pad = next;
    // Akku links vom Netz: das Netz-Symbol bleibt neben der Uhr stehen, ob ein Controller da ist oder nicht
    if (pad) net.before(pad);
    if (gear) gear.classList.toggle("has-dot", !!state.update_available);
    if (dl) {
      const t = activeTransfer(state);
      dl.hidden = !t;
      dlPct.hidden = !t || t.percent === null;
      dlPct.textContent = t && t.percent !== null ? fmt(S.jobRunning, { n: t.percent }) : "";
    }
    paintTimer(state);
    tick();
  }

  let timer = null;
  schedule();
  update(store.state);
  const unsub = store.subscribe(["network", "pads", "ui", "update_available", "sleep_timer"].concat(downloads ? ["jobs", "store_jobs", "downloads"] : []), update);
  return { el, gear, destroy() { clearTimeout(timer); clearTimeout(moonTimer); unsub(); } };
}
