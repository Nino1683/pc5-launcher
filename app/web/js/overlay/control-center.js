// Control Center (07 Abschnitt 3 und 11.4): Symbolleiste unten, darüber Karten; Kreuz auf einem
// Symbol öffnet dessen Tafel über der Leiste, die Karten weichen dabei aus. Kreis schließt die
// Tafel bzw. das Control Center, PS schließt alles (overlay.js).
//   Karten   laufende Spiele/Apps (fortsetzen, wechseln), Installation, Startdatei wählen,
//            Bilder laden, Home, Zuletzt gespielt, Medien (zuletzt geöffnete App)
//   Leiste   Home · Wechsler · Mitteilungen · Downloads · Ton · Mikrofon · Zubehör · Leistung ·
//            Maus-Modus (nur wenn eine App vorn ist; Kreuz schaltet, Dreieck zeigt die Belegung) ·
//            Einstellungen (öffnet sie im Home) · Ein/Aus
//   oben rechts  Netz (Art und Zustand als Text), Akku des Controllers, Uhr
// params (overlay.open): {entryId, section?: "power", byPowerKey?: true}
// Die Reihenfolge der Symbole lesen die Szenarien in tests/e2e/core (cc_goto): Neue Symbole nur
// nach Absprache einfügen.
import { h, icon, setIcon, cssPx } from "../core/dom.js";
import { call, on } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { sound } from "../core/sound.js";
import { store } from "../core/store.js";
import { S, fmt } from "../core/strings.js";
import { artBox } from "../components/art.js";
import { createRowList } from "../components/rows.js";
import { createStatusbar } from "../components/statusbar.js";
import { toast } from "../components/toast.js";
import { launchEntry } from "../components/entry-actions.js";
import { PANELS, jobActive, jobText, artJobPercent, artJobText, downloadActive, downloadText } from "./panels.js";
import { openPowerMenu } from "./power-menu.js";

const MAX_CARDS = 4;            // mehr passen nicht nebeneinander (erste Karte bei x=84, fokussiert 432 breit)

function iconDefs(withMouse) {
  return [
    { id: "home", icon: "home", label: S.ccHome },
    { id: "switcher", icon: "switcher", label: S.ccSwitcher, panel: true },
    { id: "notifications", icon: "bell", label: S.ccNotifications, panel: true },
    { id: "downloads", icon: "download", label: S.ccDownloads, panel: true },
    { id: "sound", icon: "sound", label: S.ccSound, panel: true },
    // Mikrofon (07 11.4, Symbol 6): Eingabegerät, Pegel, Stumm aus der Einstellungsseite audio.mic
    { id: "mic", icon: "mic", label: S.ccMic, panel: true },
    { id: "accessories", icon: "gamepad", label: S.ccAccessories, panel: true },
    { id: "performance", icon: "speed", label: S.ccPerformance, panel: true },
    withMouse ? { id: "mouse", icon: "mouse", label: S.ccMouse } : null,
    // Einstellungen des Launchers (Wunsch 2026-10-21): wie das Zahnrad im Home, also dort geöffnet
    { id: "settings", icon: "gear", label: S.ccSettings },
    { id: "power", icon: "power", label: S.ccPower },
  ].filter(Boolean);
}

// Tastenhinweis: Symbol des Pads oder Beschriftung als Kappe ("OPTIONS")
export function hintItem([glyph, text]) {
  return h("div.hint", glyph.startsWith("btn-") ? icon(glyph) : h("span.keycap", { text: glyph }), h("span", { text }));
}

export function createControlCenter(params, api) {
  const status = createStatusbar({ netText: true });
  const cards = h("div.cc-cards", { "data-focus-group": true });
  const label = h("div.cc-label", { hidden: true });
  // Zusatz über dem Namen des Maus-Symbols: Dreieck zeigt die Belegung
  const keyHint = h("div.cc-keyhint", { hidden: true }, icon("btn-triangle"), h("span", { text: S.ccMouseKeys }));
  const bar = h("div.cc-bar", { "data-focus-group": true });
  const panelHost = h("div.cc-panel-host");
  const el = h("div.cc", h("div.cc-dim"), status.el, cards, panelHost, keyHint, label, bar);
  const front = params.entryId || store.state.foreground || null;
  const ICONS = iconDefs(!!front);
  const iconEls = new Map();
  const entries = new Map();        // Einträge der laufenden Apps (für Bilder und Titel)
  let panel = null;                 // {id, el, scope, list, unsubs, refresh}
  let closed = false;
  let touched = false;              // hat schon jemand eine Taste gedrückt?
  const unsubs = [];

  const fail = (e) => toast({ title: S.error, body: (e && e.message) || S.errorGeneric, icon: "warn", sound: "error" });
  const note = (text, title) => toast({ title: title || "", body: text, icon: "info" });

  // ---------------------------------------------------------------- Leiste
  for (const def of ICONS) {
    const node = h("div.cc-icon", { "data-focus": true, dataset: { id: def.id } }, icon(def.icon), h("span.cc-dot"));
    if (def.id === "home") node.setAttribute("data-focus-default", "");
    node.addEventListener("press", () => activate(def.id));
    iconEls.set(def.id, node);
    bar.append(node);
  }

  let thumbKey = "";
  function paintBar() {
    const st = store.state;
    iconEls.get("notifications").classList.toggle("has-dot", (st.notifications || 0) > 0);
    iconEls.get("downloads").classList.toggle("has-dot", (st.jobs || []).some((j) => jobActive(j) || j.state === "needs-choice") || !!st.art_job || (st.store_jobs || []).length > 0
      || (st.downloads || []).some(downloadActive));
    if (iconEls.has("mouse")) iconEls.get("mouse").classList.toggle("is-on", !!st.mouseMode);
    setIcon(iconEls.get("sound").querySelector(".ic"), st.volume && st.volume.muted ? "sound-off" : "sound");
    // Wechsler zeigt das Bild der App, die vorn ist bzw. zuletzt lief
    const sw = iconEls.get("switcher");
    const run = (st.running || []).find((r) => r.entryId === st.foreground) || (st.running || [])[0];
    const entry = run && entries.get(run.entryId);
    // Nur neu bauen, wenn sich App oder Bild ändern: Lautstärke oder Download-Stand zeichnen die
    // Leiste oft neu, und bis das neue Bild dekodiert war, blinkte das Vorschaubild
    const key = entry ? entry.id + "|" + JSON.stringify(entry.art || {}) : "";
    if (key === thumbKey) return;
    thumbKey = key;
    const old = sw.querySelector(".cc-thumb");
    if (old) old.remove();
    sw.classList.toggle("has-thumb", !!entry);
    if (entry) sw.prepend(h("div.cc-thumb", artBox(entry, { withTitle: false })));
  }

  function showLabel() {
    const cur = nav.current;
    const def = cur && bar.contains(cur) ? ICONS.find((d) => d.id === cur.dataset.id) : null;
    label.hidden = !def || !!panel;
    keyHint.hidden = label.hidden || def.id !== "mouse";
    if (!def || panel) return;
    label.textContent = def.id === "mouse" ? (store.state.mouseMode ? S.ccMouseOn : S.ccMouseOff) : def.label;
    const r = cur.getBoundingClientRect();
    label.style.left = r.left + r.width / 2 + "px";
    keyHint.style.left = label.style.left;
  }
  el.addEventListener("nav-focus", showLabel);
  // Runter von einer Karte führt immer in die Leiste (auf das zuletzt benutzte Symbol): Die
  // fokussierte Karte ist höher als ihre Nachbarn, nach Lage läge sonst die Nachbarkarte „unten“
  let lastIcon = null;
  bar.addEventListener("nav-focus", (e) => { lastIcon = e.target.closest(".cc-icon"); });
  cards.addEventListener("nav-move", (e) => {
    if (e.detail.dir !== "down") return;
    e.preventDefault();
    const target = lastIcon && lastIcon.isConnected ? lastIcon : iconEls.get("home");
    if (nav.focus(target, { dir: "down" })) sound.play("focus");
  });

  // ---------------------------------------------------------------- Karten
  function card(def) {
    const percent = def.progress !== undefined ? h("span.cc-card-pct", { text: def.progressText || "" }) : null;
    const node = h("div.cc-card", { "data-focus": true, dataset: { id: def.id } },
      h("div.cc-card-art", def.entry ? artBox(def.entry, { withTitle: false }) : h("div.art.is-sys", icon(def.icon || "home"))),
      h("div.cc-card-text",
        h("div.cc-card-sub.ell", { text: def.sub || "" }),
        h("div.cc-card-title.clamp2", { text: def.title }),
        def.progress !== undefined ? h("div.cc-card-progress", h("div.progress" + (def.busy ? ".is-busy" : ""), { style: { "--p": def.progress / 100 } }, h("i")), percent) : null));
    node.addEventListener("press", def.action);
    return node;
  }

  let cardKey = "";
  function paintCards() {
    const st = store.state;
    const running = [];
    for (const run of st.running || []) {
      const entry = entries.get(run.entryId) || { id: run.entryId, title: run.title, kind: run.kind === "game" || run.kind === "emulator" ? "game" : "app", art: {} };
      const isFront = run.entryId === st.foreground;
      running.push({
        id: "run:" + run.entryId, entry, sub: run.title, key: entries.has(run.entryId),
        title: run.closing ? S.closing : isFront ? S.ccCardResume : S.ccCardSwitch,
        // Vorn: nur schließen (zurück ins Spiel). Sonst: nach vorn holen. Wird sie gerade beendet: nichts.
        action: () => {
          if (run.closing) return;
          if (isFront) api.close();
          else call("app.focus", { id: run.entryId }).catch(fail).finally(() => api.close());
        },
      });
    }
    const jobs = (st.jobs || []).filter((j) => jobActive(j) || j.state === "needs-choice");
    const jobCards = jobs.map((job) => (job.state === "needs-choice"
      ? { id: "job:" + job.id, icon: "archive", sub: job.title, title: S.ccCardChoice, action: () => openPanel("downloads") }
      : { id: "job:" + job.id, icon: "archive", sub: S.ccCardInstall, title: job.title, progress: Number(job.percent) || 0, progressText: jobText(job), busy: job.state !== "running", action: () => openPanel("downloads") }));
    const dl = (st.downloads || []).find(downloadActive);
    const dlCard = dl ? { id: "dl", icon: "download", sub: S.ccCardDownload, title: dl.name, progress: dl.percent || 0, progressText: downloadText(dl), busy: !(dl.total > 0), action: () => openPanel("downloads") } : null;
    const art = st.art_job ? { id: "art", icon: "download", sub: S.ccCardArt, title: S.ccArtJob, progress: artJobPercent(st.art_job), progressText: artJobText(st.art_job), action: () => openPanel("downloads") } : null;
    const home = st.foreground ? { id: "home", icon: "home", sub: S.ccCardHomeSub, title: S.ccCardHome, action: () => activate("home") } : null;
    // 07 11.4: „Zuletzt gespielt“ und „Medien“ (zuletzt geöffnete App); was gerade läuft, hat schon seine Karte
    const runningIds = new Set((st.running || []).map((r) => r.entryId));
    const last = extra.recent.find((e) => !runningIds.has(e.id));
    const recent = last ? { id: "recent:" + last.id, entry: last, sub: S.ccCardRecent, title: last.title, action: () => startFromCard(last) } : null;
    const media = extra.media && !runningIds.has(extra.media.id)
      ? { id: "media:" + extra.media.id, entry: extra.media, sub: S.ccCardMedia, title: extra.media.title, action: () => startFromCard(extra.media) } : null;
    const defs = [...running.slice(0, 2), ...jobCards.slice(0, 1), dlCard, home, art, recent, media].filter(Boolean).slice(0, MAX_CARDS);
    // Dieselben Karten: nur den Fortschritt nachführen (sonst flackert der Fokusring bei jedem Prozent)
    const key = JSON.stringify(defs.map((d) => [d.id, d.title, d.sub, d.key, d.busy]));
    if (key === cardKey) {
      for (const d of defs) {
        if (d.progress === undefined) continue;
        const node = [...cards.children].find((n) => n.dataset.id === d.id);
        if (!node) continue;
        node.querySelector(".progress").style.setProperty("--p", d.progress / 100);
        node.querySelector(".cc-card-pct").textContent = d.progressText || "";
      }
      return;
    }
    cardKey = key;
    const focusedId = nav.current && cards.contains(nav.current) ? nav.current.dataset.id : null;
    const nodes = defs.map(card);
    if (nodes[0]) nodes[0].setAttribute("data-focus-default", "");
    cards.replaceChildren(...nodes);
    if (focusedId) nav.focus(nodes.find((n) => n.dataset.id === focusedId) || nodes[0] || iconEls.get("home"));
  }

  // Karten „Zuletzt gespielt“ und „Medien“: einmal beim Öffnen gelesen
  const extra = { recent: [], media: null };
  async function loadExtra() {
    try {
      const games = ((await call("lib.list", { kind: "game", filter: "recent", limit: 4 })) || {}).entries || [];
      // mehrere Kandidaten: Was gerade läuft, hat schon seine eigene Karte
      extra.recent = games.filter((e) => e.last_played && e.available !== false && e.installed !== false);
      const apps = ((await call("lib.list", { kind: "app" })) || {}).entries || [];
      // Steam läuft per Übergabe, nicht als App in der Konsole
      const opened = apps.filter((e) => e.last_played && e.id !== "app:steam" && e.installed !== false);
      opened.sort((a, b) => (b.last_played || 0) - (a.last_played || 0));
      extra.media = opened[0] || null;
    } catch (e) { return; }
    if (closed) return;
    // Beim Öffnen gehört der Fokus auf die erste Karte (07 3.3); kamen die Karten erst jetzt, und
    // hat noch niemand eine Taste gedrückt, wandert er vom Home-Symbol dorthin
    const untouched = !touched && !panel && nav.current === iconEls.get("home");
    paintCards();
    if (untouched && cards.firstElementChild) nav.focus(cards.firstElementChild);
  }
  async function startFromCard(entry) {
    const started = await launchEntry(entry);
    if (started && !closed) api.close();
  }

  async function loadEntries() {
    const ids = (store.state.running || []).map((r) => r.entryId).filter((id) => !entries.has(id));
    await Promise.all(ids.map(async (id) => {
      try { entries.set(id, (await call("lib.get", { id })).entry); } catch (e) { /* ohne Bild */ }
    }));
    if (closed || !ids.length) return;
    paintCards();
    paintBar();
    if (panel && panel.refresh) panel.refresh();
  }

  // ---------------------------------------------------------------- Tafeln
  function closePanel(refocus = true) {
    if (!panel) return;
    const old = panel;
    panel = null;
    nav.popScope(old.scope);
    for (const u of old.unsubs) u();
    old.el.remove();
    el.classList.remove("has-panel");
    for (const n of iconEls.values()) n.classList.remove("is-open");
    cards.inert = false;
    if (refocus) nav.focus(iconEls.get(old.id));
    showLabel();
  }

  async function openPanel(id) {
    // Die Tafel „Maus-Modus“ (Belegung) gehört zum Symbol, öffnet aber nur mit Dreieck
    const def = ICONS.find((d) => d.id === id);
    if (!def || !PANELS[id]) return;
    closePanel(false);
    const ctx = { api, entries, fail, note, refresh: () => refresh() };
    const spec = PANELS[id](ctx);
    const mine = { id, el: null, list: null, scope: null, unsubs: [], refresh: null };
    let loadError = null;          // Satz des Kerns, wenn die Tafel nicht laden kann (z. B. Baustein ausgefallen)
    const list = createRowList({
      emptyText: spec.empty || S.empty,
      alwaysFocus: true,
      onSet: spec.onSet ? async (row, value) => { await spec.onSet(row, value); } : null,
      onAction: spec.onAction ? async (row) => { await spec.onAction(row); refresh(); } : null,
      onPress: spec.onPress ? (row, node) => spec.onPress(row, node) : null,
      onError: (text) => toast({ title: def.label, body: text, icon: "warn", sound: "error" }),
      onHeld: (node) => { if (mine.scope) mine.scope.last = node; },
    });
    // footer: feste Anzeige unter wenigen Zeilen (Belegung des Maus-Modus); die Liste ist dann so hoch wie ihr Inhalt
    const footer = spec.footer ? spec.footer() : null;
    if (footer) list.el.classList.add("is-auto");
    const node = h("div.cc-panel", { "data-bounds": true, dataset: { id } },
      h("div.cc-panel-title.ell", { text: def.label }),
      h("div.cc-panel-body", list.el, footer),
      spec.hints ? h("div.hints.cc-panel-hints", spec.hints.map(hintItem)) : null);
    // Waagerecht auf das Symbol zentriert, am Rand begrenzt
    const anchor = iconEls.get(id).getBoundingClientRect();
    const width = cssPx(id === "downloads" ? "--cc-panel-w-dl" : "--cc-panel-w");     // Downloads breiter (lange Dateinamen)
    const margin = cssPx("--cc-panel-margin");
    node.style.left = Math.max(margin, Math.min(innerWidth - margin - width, anchor.left + anchor.width / 2 - width / 2)) + "px";
    panelHost.append(node);
    el.classList.add("has-panel");
    cards.inert = true;
    iconEls.get(id).classList.add("is-open");
    label.hidden = true;
    mine.el = node;
    mine.list = list;
    const refresh = () => {
      if (panel !== mine || closed) return;
      list.setRows(loadError ? [{ id: "_error", type: "info", label: loadError, enabled: false }] : spec.rows());
    };
    mine.refresh = refresh;
    panel = mine;
    mine.scope = nav.pushScope(node, {
      name: "cc-panel:" + id,
      onBack: () => closePanel(true),
      onPad: (button, ev) => {
        if (ev.repeat) return false;
        if (button === "l1" || button === "r1") { step(button === "r1" ? 1 : -1); return true; }
        if (button === "l2" || button === "r2") return list.page(button === "r2" ? 1 : -1);
        const row = nav.current && node.contains(nav.current) ? list.rowOf(nav.current) : null;
        return spec.onPad ? spec.onPad(button, row) === true : false;
      },
    });
    // Kann die Tafel nicht laden, steht der Satz des Kerns als gesperrte Zeile in ihr (statt einer leeren Liste)
    const reload = async () => {
      if (!spec.load) return;
      try {
        await spec.load();
        loadError = null;
      } catch (e) {
        loadError = (e && e.message) || S.errorGeneric;
        sound.play("error");
      }
      refresh();
    };
    if (spec.load) list.setLoading(); else list.setRows(spec.rows());
    nav.ensure();
    if (spec.keys) {
      mine.unsubs.push(store.subscribe(spec.keys, (state) => {
        if (spec.onState) spec.onState(state);
        if (spec.reloadOn && spec.reloadOn(state)) reload(); else refresh();
      }));
    }
    for (const event of spec.events || []) mine.unsubs.push(on(event, () => { if (panel === mine) reload(); }));
    await reload();
    nav.ensure();
  }

  // ---------------------------------------------------------------- Ein/Aus
  async function openPower() {
    const node = iconEls.get("power");
    closePanel(false);
    nav.focus(node);
    node.classList.add("is-open");
    el.classList.add("has-panel");
    label.hidden = true;
    const done = await openPowerMenu({ anchor: node.getBoundingClientRect(), fail });
    if (closed) return;
    node.classList.remove("is-open");
    el.classList.remove("has-panel");
    showLabel();
    if (done) api.close();
  }

  // ---------------------------------------------------------------- Bedienung
  function activate(id) {
    switch (id) {
      case "home": call("app.home", {}).catch(fail).finally(() => api.close()); break;
      case "power": openPower(); break;
      // Das Home kommt nach vorn (eine App wartet angehalten) und öffnet die Einstellungen
      case "settings": call("ui.open", { screen: "settings" }).catch(fail).finally(() => api.close()); break;
      // Kreuz schaltet sofort um (wie ein Schalter); die Belegung zeigt Dreieck
      case "mouse": call("mouse.set", { on: !store.state.mouseMode }).catch(fail); break;
      default: openPanel(id);
    }
  }

  // L1/R1: Symbol links/rechts, auch aus den Karten und aus einer offenen Tafel heraus
  function step(delta) {
    const cur = nav.current && bar.contains(nav.current) ? nav.current.dataset.id : (panel ? panel.id : null);
    const at = cur ? ICONS.findIndex((d) => d.id === cur) : (delta > 0 ? -1 : ICONS.length);
    const next = ICONS[Math.max(0, Math.min(ICONS.length - 1, at + delta))];
    if (!next || next.id === cur) { sound.play("edge"); return; }
    const hadPanel = !!panel;
    closePanel(false);
    nav.focus(iconEls.get(next.id));
    sound.play("tab");
    if (hadPanel && next.panel) openPanel(next.id);
  }

  function onPad(button, ev) {
    touched = true;
    if (ev.repeat) return false;
    if (button === "l1" || button === "r1") { step(button === "r1" ? 1 : -1); return true; }
    if (button === "triangle" && nav.current && nav.current === iconEls.get("mouse")) { openPanel("mouse"); sound.play("select"); return true; }
    return false;
  }

  unsubs.push(store.subscribe(["running", "foreground", "jobs", "art_job", "store_jobs", "downloads", "notifications", "mouseMode", "volume", "network"], () => {
    paintBar(); paintCards(); loadEntries(); showLabel();
  }));
  unsubs.push(on("app.changed", () => loadEntries()));
  paintBar();
  paintCards();
  loadEntries();
  loadExtra();

  return {
    el, onPad,
    onOpen() {
      // Fokus: erste Karte, sonst die Leiste; mit params.section (vom Kern, z. B. "power" beim
      // Einschaltknopf) bzw. params.panel gleich eine Tafel oder das Ein/Aus-Menü
      const wanted = params.section || params.panel;
      if (wanted && iconEls.has(wanted)) { nav.focus(iconEls.get(wanted)); activate(wanted); }
      // Vom Einschaltknopf geöffnet: Der zweite Druck schaltet aus (auch ohne Pad). Das soll man sehen.
      if (params.byPowerKey) toast({ title: S.powerKeyTitle, body: S.powerKeyHint, icon: "power", sound: false });
      showLabel();
    },
    destroy() {
      closed = true;
      for (const u of unsubs) u();
      if (panel) for (const u of panel.unsubs) u();
      status.destroy();
    },
  };
}
