// Spielseite: Hub eines einzelnen Eintrags als eigener Bildschirm (aus der Spielebibliothek heraus).
// Sieht aus wie das Home mit offenem Hub (07 Abschnitt 2.4, Zustand B), mit bewegtem Hintergrund.
import { h } from "../core/dom.js";
import { call } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { router } from "../core/router.js";
import { createHub } from "../components/hub.js";
import { createStatusbar } from "../components/statusbar.js";
import { createBackdrop } from "../components/backdrop.js";
import { activateEntry, openEntryMenu, closeEntry, desktopMode } from "../components/entry-actions.js";

function create(params) {
  let entry = params.entry;
  const backdrop = createBackdrop();
  const hub = createHub({
    onMain: (e) => activateEntry(e),
    onMore: (e) => openEntryMenu(e, { anchor: hub.more }),
    onSecond: (e, kind) => { if (kind === "close") closeEntry(e); else if (kind === "desktop-mode") desktopMode(); },
  });
  const status = createStatusbar({});
  const el = h("div.hub-screen", backdrop.el, status.el, hub.mini, hub.el);
  let serial = 0;

  function paint() {
    hub.setEntry(entry);
    backdrop.set(entry, { instant: true });
  }

  async function load() {
    const mine = ++serial;
    try {
      const detail = await call("lib.get", { id: entry.id });
      if (mine !== serial) return;
      if (detail && detail.entry) {
        entry = detail.entry;
        paint();
        hub.setDetail(detail);
        // Wurde der Eintrag ausgeblendet, gibt es hier nichts mehr zu tun
        if (entry.hidden && !params.entry.hidden && router.top && router.top.inst === api) router.back();
      }
    } catch (e) {
      // Den Eintrag gibt es nicht mehr (gelöscht) oder der Kern antwortet nicht: zurück
      if (mine === serial && router.top && router.top.inst === api) router.back();
    }
    nav.ensure();
  }

  function onPad(button, ev) {
    if (button === "options" && !ev.repeat) { openEntryMenu(entry, { anchor: hub.more }); return true; }
    return false;
  }
  function onEvent(event, payload) {
    if (event === "lib.changed") {
      // Bilder anderer Einträge gehen diese Seite nichts an
      if (payload && payload.reason === "art" && Array.isArray(payload.ids) && !payload.ids.includes(entry.id)) return;
      load();
    } else if (event === "app.changed" || (event === "state" && payload && "running" in payload)) hub.refresh();
  }

  const api = {
    el, onPad, onEvent,
    // Zurück (z. B. aus den Spieleinstellungen): Marken und Karten frisch aus lib.get
    onShow(info) { backdrop.setActive(true); nav.ensure(); if (info && info.back) load(); },
    onHide() { backdrop.setActive(false); },
    destroy() { status.destroy(); backdrop.destroy(); },
  };
  paint();
  load();
  return api;
}

export default { id: "hub", create };
