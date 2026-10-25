// Seite eines Spiels im Store (Bauplan E68): großes Bild, Preis, Hauptknopf (Installieren, Spielen,
// Kaufen, Anmelden), Fakten (Erscheinung, Entwickler, Steam Deck, ProtonDB, Metacritic …), Bilder
// und Beschreibung. Kaufen öffnet die Shop-Seite im Browser des PC5 Launchers (nie den Steam-Client);
// Installieren lädt über Steam unsichtbar im Hintergrund, der Fortschritt steht auf dem Knopf.
// itch.io (E100): Browserspiele spielt man auf ihrer Seite, das kleine Cover steht scharf rechts oben
// vor einer unscharfen Fassung von sich selbst.
import { h, icon, cssPx } from "../core/dom.js";
import { call } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { router } from "../core/router.js";
import { sound } from "../core/sound.js";
import { store } from "../core/store.js";
import { S, fmt } from "../core/strings.js";
import { confirm, dialog, imageViewer, messageDialog, scrollTextDialog } from "../components/dialog.js";
import { toast } from "../components/toast.js";
import { activateEntry } from "../components/entry-actions.js";

// Browser des PC5 Launchers mit einer Adresse (Kaufen, Anmelden). Läuft vorn etwas anderes, fragt
// die Seite wie bei jedem Start, ob es geschlossen werden darf.
export async function openStoreUrl(method, params) {
  try {
    let r = await call(method, params);
    if (r && r.needsConfirm === "close-other") {
      const other = r.other || {};
      const yes = await confirm({ title: fmt(S.startOtherTitle, { title: S.stBrowserApp }),
        text: fmt(S.startOtherText, { other: other.title || S.unknown }), ok: S.start });
      if (!yes) return null;
      r = await call(method, { ...params, closeOther: true });
    }
    if (r && r.ok === false) messageDialog({ title: S.stTitle, text: r.message || S.errorGeneric });
    return r;
  } catch (e) {
    messageDialog({ title: S.stTitle, text: (e && e.message) || S.errorGeneric });
    return null;
  }
}

// Namen der Shops; itch.io kommt aus dem eigenen Block der Texte (E100). Auch für store.js.
export const STORE_NAMES = { ...S.stStores, itch: S.stItchStore };

// itch.io ohne eigene App (E100): erst erklären, was man im itch.io-Konto anlegt; „Weiter“ öffnet
// die Seite dazu (store.login). Die Redirect-URL ist die aus store/itch_account.py (REDIRECT).
const ITCH_REDIRECT = "http://127.0.0.1:34711/itch-callback";
export async function itchSetupDialog() {
  const list = h("div.dialog-info",
    h("div", h("span", { text: S.stItchSetupName }), h("span", { text: S.stItchSetupAppName })),
    h("div", h("span", { text: S.stItchSetupRedirect }), h("span", { text: ITCH_REDIRECT })));
  const choice = await dialog({ title: S.stItchSetupTitle, text: S.stItchSetupText, content: list, detail: S.stItchSetupDetail,
    buttons: [{ id: "cancel", label: S.cancel }, { id: "ok", label: S.stItchSetupOk }], initial: "ok" });
  return choice === "ok";
}

// Anmelden im Browser. setup: itch.io ist noch nicht eingerichtet, die Tafel oben kommt zuerst.
// Ergebnis wie openStoreUrl (null: abgebrochen)
export async function storeLogin(storeId, { setup = false } = {}) {
  if (setup && !(await itchSetupDialog())) return null;
  const r = await openStoreUrl("store.login", { store: storeId });
  if (r && r.ok) {
    toast(setup ? { title: S.stItchSetupTitle, body: S.stItchSetupToast, icon: "info" }
      : { title: fmt(S.stLoginStore, { store: STORE_NAMES[storeId] }), body: loginHint(storeId), icon: "info" });
  }
  return r;
}

// Erscheinungsdatum: GOG und Epic liefern „2020-10-23“, Steam schon „9. Dez. 2020“ (so zeigen es alle)
function dateText(value) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ""));
  if (!m) return value;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])).toLocaleDateString(S.locale, { day: "numeric", month: "short", year: "numeric" });
}

// Shops, deren Spiele der Launcher des Herstellers installiert (E94)
const LAUNCHER_STORES = ["battlenet", "ubisoft", "rockstar"];

export function loginHint(storeId) {
  if (LAUNCHER_STORES.includes(storeId)) return fmt(S.stLoginHintLauncher, { store: STORE_NAMES[storeId] });
  return { gog: S.stLoginHintGog, epic: S.stLoginHintEpic, itch: S.stItchLoginHint }[storeId] || S.stLoginHint;
}

function factRows(d, storeId) {
  const f = S.stFacts;
  const shop = d.store || storeId;
  const systems = [d.windows || shop === "steam" ? "Windows" : "", d.linux ? "Linux" : ""].filter(Boolean).join(", ");
  return [
    ...(shop === "gog" ? [[STORE_NAMES.gog, S.stDrmFree], [S.stFactSystem, systems]] : []),
    // itch.io (E100): ohne Kopierschutz wie GOG; Browserspiele haben kein System, „Zahl, was du willst“ steht dabei
    ...(shop === "itch" ? [[STORE_NAMES.itch, S.stDrmFree], [S.stFactSystem, d.browserOnly ? S.stItchBrowser : systems],
      [S.stItchPriceFact, d.pwyw ? S.stItchPwyw : ""]] : []),
    [f.release, dateText(d.release)],
    [f.developer, (d.developers || []).join(", ")],
    [f.genre, (d.genres || []).join(", ")],
    [f.deck, S.stDeck[d.deck] || ""],
    [f.protondb, S.stProton[d.protondb] || ""],
    [f.metacritic, d.metacritic ? String(d.metacritic) : ""],
    [f.controller, S.stController[d.controller] || ""],
    [f.size, d.size_gb ? fmt(S.stSizeGb, { n: Math.round(d.size_gb) }) : ""],
  ].filter(([, v]) => v);
}

function create(params = {}) {
  const id = String(params.id || "");
  const storeId = params.store || "steam";
  const card = params.card || {};
  const hero = h("img.st-hero", { alt: "", decoding: "async" });
  const shade = h("div.st-shade");
  const title = h("h1.st-title.clamp2", { text: card.title || "" });
  // itch.io (E100): Das Cover (315x250) wäre als Kopfbild grob vergrößert; es steht scharf daneben
  const itch = storeId === "itch";
  const cover = itch ? h("img.st-cover", { alt: "", decoding: "async", hidden: true }) : null;
  const short = h("div.st-short.clamp3");
  const priceBox = h("div.st-price-big");
  const mainLabel = h("span.btn-label", { text: S.stLoading });
  const main = h("div.btn.st-main", { "data-focus": true, "data-focus-default": true }, mainLabel);
  const second = h("div.btn.is-secondary.st-second", { "data-focus": true, hidden: true }, h("span.btn-label"));
  const web = h("div.btn.is-secondary.is-round.st-web", { "data-focus": true }, icon("globe"));
  const buttons = h("div.st-buttons", { "data-focus-group": true }, main, second, web);
  const note = h("div.st-note");
  const facts = h("div.st-facts");
  const shots = h("div.st-shots", { "data-focus-group": true });
  const more = h("div.btn.is-secondary.st-more", { "data-focus": true, hidden: true }, icon("info"), h("span.btn-label", { text: S.stMore }));
  const shotsSub = h("div.st-sub", { text: S.stShots });
  const lower = h("div.st-lower", shotsSub, shots, more);
  const page = h("div.st-page", cover, title, short, priceBox, buttons, note, facts, lower);
  const el = h("div.store-item" + (itch ? ".is-fit" : ""), hero, shade, page);
  let d = null;
  let serial = 0;
  let loading = false;

  function price() {
    priceBox.replaceChildren();
    if (!d || d.owned) return;
    if (d.discount) priceBox.append(h("span.st-disc", { text: "−" + d.discount + " %" }), h("span.st-old", { text: d.original }));
    if (d.price) priceBox.append(h("span.st-price", { text: d.price }));
    if (d.pwyw) priceBox.append(h("span.st-pwyw", { text: S.stItchPwyw }));
  }

  // Noch nicht eingerichtet (nur itch.io, E100): Das sagt die Seite selbst oder der Store, der sie öffnete
  const unset = () => itch && (d && "configured" in d ? d.configured === false : params.configured === false);
  // Ohne Kosten zu haben: kostenlos oder „Zahl, was du willst“ ohne Mindestpreis
  const freeToGet = () => !!(d.free || (d.pwyw && !d.buyable));

  // Epic (E73): Downloads laufen je Namensraum („ns:“), die Seite kann ein Angebot sein („ns:angebot“)
  const mine = (j) => (j.store || "steam") === storeId
    && (storeId === "epic" ? String(j.id).split(":")[0] === id.split(":")[0] : j.id === id);

  function job() {
    const jobs = store.state.store_jobs || [];
    return jobs.find(mine) || (d && d.job ? { ...d.job, id } : null);
  }

  // Was der Hauptknopf gerade tut
  function action() {
    if (!d) return null;
    const j = job();
    const jobLabel = j && (j.percent ? fmt(S.stJobPercent, { text: j.text, n: j.percent }) : j.text);
    // Über einen Launcher (E94): Der Knopf holt ihn nach vorn (bestätigen, Fortschritt ansehen)
    if (j && LAUNCHER_STORES.includes(storeId) && !j.hidden && (j.state === "confirm" || j.state === "installing")) return { id: "show", label: jobLabel };
    if (j) return { id: "loading", label: jobLabel, off: true };
    if (d.installed) return { id: "play", label: S.stPlay };
    // itch.io (E100): Ein Browserspiel lässt sich nicht installieren, gespielt wird auf seiner Seite
    if (d.browserOnly) return { id: "web", label: S.stItchPlayWeb };
    if (d.owned) return { id: "install", label: S.stInstall, off: d.installable === false };
    if (d.coming_soon && !d.buyable) return { id: "soon", label: S.stSoon, off: true };
    if (!d.loggedIn) return { id: "login", label: fmt(S.stLoginStore, { store: STORE_NAMES[storeId] }) };
    return { id: "buy", label: freeToGet() ? S.stGetFree : S.stBuy };
  }

  function paintButtons() {
    const a = action();
    mainLabel.textContent = a ? a.label : S.stLoading;
    main.dataset.action = a ? a.id : "";
    main.classList.toggle("is-off", !!(a && a.off));
    const j = job();
    // E99: Eine Deinstallation lässt sich nicht abbrechen; ein installiertes Steam-Spiel mit offenem
    // Update heißt „Spielen“ (nicht „Installieren“) und bekommt nur den Hinweis
    second.hidden = !j || j.state === "removing";
    second.querySelector(".btn-label").textContent = S.stCancelDl;
    // itch.io (E100): neuere Datei als Hinweis (updateHint), Browserspiel, Einrichtung, kostenlos holen
    note.textContent = !d ? "" : j ? "" : d.installed ? (d.update_pending ? S.stUpdatePending : d.updateHint || "") : d.hint ? d.hint
      : d.browserOnly ? S.stItchBrowserHint
        : d.owned && d.library ? fmt(S.stFacts.target + ": " + S.stTargetFree, { path: d.library.path, n: d.library.free_gb })
          : !d.owned && !d.loggedIn ? (unset() ? S.stItchSetupHint : loginHint(storeId))
            : !d.owned ? (itch && freeToGet() ? S.stItchFreeHint : S.stBuyHint) : "";
    nav.ensure();
  }

  function paint() {
    title.textContent = d.title;
    short.textContent = d.short || "";
    hero.src = d.hero;
    hero.onerror = () => { hero.onerror = null; hero.src = (d.screenshots || [])[0] || d.image; };
    if (cover) {
      const src = d.image || d.hero || "";
      cover.onerror = () => { cover.hidden = true; };
      cover.onload = () => { cover.hidden = false; };
      if (cover.getAttribute("src") !== src) { cover.hidden = true; if (src) cover.src = src; else cover.removeAttribute("src"); }
    }
    price();
    facts.replaceChildren(...factRows(d, storeId).map(([k, v]) => h("div.st-fact", h("span", { text: k }), h("b.ell", { text: v }))));
    shots.replaceChildren(...(d.screenshots || []).map((src, i) => {
      const t = h("div.st-shot", { "data-focus": true, dataset: { i } }, h("img", { alt: "", src, decoding: "async", loading: "lazy" }));
      return t;
    }));
    more.hidden = !d.about;
    // itch.io (E100) liefert keine Bilder: dann auch keine Überschrift „Bilder“ über der Beschreibung
    shotsSub.hidden = !(d.screenshots || []).length;
    lower.hidden = !(d.screenshots || []).length && !d.about;
    paintButtons();
  }

  // Ohne Seite (erstes Laden gescheitert, web-ui-4) heißt der Hauptknopf „Erneut versuchen“ und
  // lädt neu; vorher stand dort nur „Der Store ist gerade nicht erreichbar.“, gesperrt, ohne Ausweg
  // außer zurück. Scheitert ein späteres Neuladen, bleibt die Seite mit dem alten Stand bedienbar.
  async function load() {
    const mine = ++serial;
    loading = true;
    if (!d) {
      mainLabel.textContent = S.stLoading;
      main.dataset.action = "";
      main.classList.add("is-off");
    }
    let r;
    try { r = await call("store.item", { store: storeId, id, card: params.card || null }); } catch (e) { r = { ok: false, message: e && e.message }; }
    if (mine !== serial) return;
    loading = false;
    if (!r || r.ok === false || !r.item) {
      const why = (r && r.message) || S.stFailed;
      if (d) { paintButtons(); note.textContent = why; return; }
      mainLabel.textContent = S.retry;
      main.dataset.action = "retry";
      main.classList.remove("is-off");
      note.textContent = why;
      nav.ensure();
      return;
    }
    d = r.item;
    paint();
  }

  async function press() {
    if (!d) {
      if (loading) { sound.play("edge"); return; }
      load();
      return;
    }
    const a = action();
    if (!a || a.off) { sound.play("edge"); return; }
    if (a.id === "play") { if (d.entryId) activateEntry({ id: d.entryId, title: d.title, kind: "game", installed: true, available: true }); return; }
    if (a.id === "login") { await storeLogin(storeId, { setup: unset() }); return; }
    if (a.id === "web") { await openStoreUrl("store.web", { store: storeId, what: "page", id }); return; }
    if (a.id === "buy") { await openStoreUrl("store.buy", { store: storeId, id, slug: d.slug || "", title: d.title }); return; }
    if (a.id === "show") {
      const r = await call("store.install", { store: storeId, id }).catch((e) => ({ ok: false, message: e && e.message }));
      if (!r || !r.ok) messageDialog({ title: d.title, text: (r && r.message) || S.errorGeneric });
      return;
    }
    if (a.id === "install") {
      const r = await call("store.install", { store: storeId, id }).catch((e) => ({ ok: false, message: e && e.message }));
      if (r && r.ok) toast({ title: d.title, body: r.message || "", icon: "download" });
      else messageDialog({ title: d.title, text: (r && r.message) || S.errorGeneric });
      load();
    }
  }

  async function cancel() {
    const j = job();
    const update = !!(j && j.kind === "update");          // Epic-Update (E99): das Spiel bleibt
    const yes = await confirm({ title: update ? S.stCancelUpdateTitle : S.stCancelTitle,
      text: fmt(update ? S.stCancelUpdateText : S.stCancelText, { title: d.title }), ok: S.stCancelOk, cancel: S.stKeep });
    if (!yes) return;
    const r = await call("store.cancel", { store: storeId, id }).catch((e) => ({ ok: false, message: e && e.message }));
    if (!(r && r.ok)) messageDialog({ title: d.title, text: (r && r.message) || S.errorGeneric });
  }

  // Beschreibung: Tafel zum Blättern (Hoch/Runter, rechter Stick; gemeinsamer Baustein, E99)
  function about() {
    const text = h("div.st-about", { text: d.about });
    scrollTextDialog({ title: d.title, box: h("div.st-about-box", text), body: text });
  }

  function viewShot(start) {
    const list = d.screenshots || [];
    imageViewer({ count: list.length, start, src: (i) => list[i], caption: (i) => fmt(S.stImageOf, { n: i + 1, total: list.length }) });
  }

  // Unten (Bilder, Beschreibung) rückt die Seite nach oben, mindestens um --st-lower-shift und so
  // weit, dass das gewählte Element über den Tastenhinweisen ganz zu sehen ist: Bei mehr als einer
  // Reihe Screenshots reichte der feste Versatz nicht bis „Beschreibung“ (Wunsch 2026-10-21).
  // Gemessen wird ohne Verschiebung (offsetTop in der Seite), nicht mitten in der Bewegung.
  el.addEventListener("nav-focus", (e) => {
    const inLower = lower.contains(e.target);
    page.classList.toggle("is-lower", inLower);
    if (!inLower) { page.style.removeProperty("--fx-y"); return; }
    const t = e.target.closest("[data-focus]") || e.target;
    let top = 0;
    for (let n = t; n && n !== page; n = n.offsetParent) top += n.offsetTop;
    const room = innerHeight - cssPx("--st-bottom-room") - cssPx("--st-page-top");
    const shift = Math.max(cssPx("--st-lower-shift"), top + t.offsetHeight - room);
    page.style.setProperty("--fx-y", -Math.round(shift) + "px");
  });
  main.addEventListener("press", press);
  second.addEventListener("press", cancel);
  // Globus: die Seite des Spiels im Browser (itch.io über store.web, E100; sonst wie Kaufen)
  web.addEventListener("press", () => (itch ? openStoreUrl("store.web", { store: storeId, what: "page", id })
    : openStoreUrl("store.buy", { store: storeId, id, slug: (d && d.slug) || "" })));
  more.addEventListener("press", about);
  shots.addEventListener("press", (e) => { const t = e.target.closest(".st-shot"); if (t) viewShot(Number(t.dataset.i) || 0); });

  load();
  return {
    el,
    onEvent(event, payload) {
      if (event === "state" && payload && "store_jobs" in payload && d) {
        const had = !!(d.job);
        const now = (payload.store_jobs || []).some(mine);
        if (had && !now) load(); else paintButtons();
      // Bilder der Bibliothek (reason "art") ändern hier nichts; der Neubau nahm sonst den Fokus aus den Screenshots
      } else if (event === "lib.changed" && d && !(payload && payload.reason === "art")) load();
      // Zurück aus dem Browser (Kaufen, Anmelden) und gekauft (E71): auch ohne Seite neu laden (web-ui-4)
      else if (event === "state" && payload && "foreground" in payload && !payload.foreground) load();
      // id "" heißt: alles dieses Shops neu (Anmeldung, Launcher, itch.io E100)
      else if (event === "store.owned" && payload && (payload.store || storeId) === storeId && (payload.id === id || !payload.id)) load();
    },
    onShow(info) { nav.ensure(); if (info && info.back) load(); },
  };
}

export default { id: "store-item", create };
