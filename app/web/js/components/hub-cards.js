// Nützliche Karten unter dem Hub (E125), für Home und Spielseite: statt „Quelle“, „Proton-Fassung“ … aus lib.get
//   Trophäen          Fortschritt, Stufen, Linie am unteren Rand (E124; nur Steam-Spiele) – Kreuz: Liste des Spiels
//   Deine Bildschirmfotos  das letzte Foto des Spiels als Bild (gallery.list) – Kreuz: Galerie mit Filter auf das Spiel
//   Neuigkeiten       die neueste Meldung mit Datum (game.news) – Kreuz: ihr Text, daraus „Im Browser öffnen“
//   Über das Spiel    Genre, Erscheinungsjahr, Metacritic (game.info) – Kreuz: die lange Beschreibung
// Fehlt etwas, rücken die anderen nach; ganz ohne (Nicht-Steam-Spiel ohne Fotos, Apps, Systemkacheln) bleiben die
// technischen Karten aus lib.get (hub.js). Die technischen Angaben stehen immer unter „…“ › Informationen.
// Das Foto kommt als Vorschaubild (gallery.thumbs, 640x360), nie als 4K-Datei.
// Jede Quelle liefert [] oder eine Karte {order (< 50 = nützlich), kind, label, value, sub?, image?, progress?, extra?,
// clamp?, press?} für hub.addCardSource (hub.js zeichnet sie; mit press ist die Karte anwählbar).
import { call } from "../core/bridge.js";
import { router } from "../core/router.js";
import { S, fmt, whenText } from "../core/strings.js";
import { gameInfo, gameNews, wantsInfo } from "./gameinfo.js";
import { hasTrophies, trophyGame, openTrophies, percentText, tierCounts } from "./trophies.js";
import { showNews, showAbout } from "./hub-sections.js";

const SHOTS = 6;                  // so viele Fotos fragt die Karte (nur das neueste wird gezeigt, die Zahl ab 6 „mehr als“)

async function trophyCard(entry) {
  if (!hasTrophies(entry)) return [];
  const r = await trophyGame(entry.id);
  const p = r && r.ok && r.progress;
  if (!p || !p.total) return [];
  return [{ order: 10, kind: "trophies", label: S.trTitle, value: fmt(S.trOf, { n: p.unlocked, total: p.total }),
    sub: percentText(p.percent), progress: (p.percent || 0) / 100, extra: tierCounts(p.tiers, { cls: "hub-card-tiers" }),
    press: () => openTrophies({ id: entry.id, title: entry.title, entry }) }];
}

async function shotsCard(entry) {
  if (!entry || entry.system || entry.kind !== "game") return [];
  let r = null;
  try { r = await call("gallery.list", { game: entry.title, limit: SHOTS }); } catch (e) { return []; }
  const items = (r && r.items) || [];
  const last = items[0];
  if (!last) return [];
  let thumb = last.thumb;
  if (!thumb) {
    try {
      const t = await call("gallery.thumbs", { paths: [last.path] });
      thumb = t && t.thumbs ? t.thumbs[last.path] : null;
    } catch (e) { thumb = null; }
  }
  const n = items.length;
  const count = r.truncated ? fmt(S.hcShotsMany, { n }) : n === 1 ? S.hcShotsOne : fmt(S.hcShotsCount, { n });
  return [{ order: 20, kind: "shots", label: S.hcShots, value: count, sub: whenText(last.time), image: thumb || "",
    press: () => router.go("gallery", { game: entry.title }) }];
}

async function newsCard(entry) {
  if (!wantsInfo(entry)) return [];
  const r = await gameNews(entry.id);
  const news = r && r.ok && r.steam && Array.isArray(r.steam.news) ? r.steam.news : [];
  const n = news[0];
  if (!n || !n.title) return [];
  return [{ order: 30, kind: "news", label: [S.hiNews, whenText(n.date)].filter(Boolean).join(" · "), value: n.title, clamp: true,
    press: () => showNews(entry, n) }];
}

function yearOf(release) {
  const m = /\b(19|20)\d{2}\b/.exec(String(release || ""));
  return m ? m[0] : "";
}

async function aboutCard(entry) {
  if (!wantsInfo(entry)) return [];
  const r = await gameInfo(entry.id);
  const d = r && r.ok && r.steam;
  if (!d) return [];
  const genres = (d.genres || []).slice(0, 2).join(", ");
  const year = d.coming_soon ? "" : yearOf(d.release);
  const meta = d.metacritic ? fmt(S.hcMeta, { n: d.metacritic }) : "";
  const value = genres || year || meta;
  if (!value) return [];
  const sub = [genres ? year : "", meta && value !== meta ? meta : ""].filter(Boolean).join(" · ");
  const long = d.about && d.about.length > (d.short || "").length + 40;
  return [{ order: 40, kind: "about", label: S.hiAbout, value, sub, press: long ? () => showAbout(d) : null }];
}

// hub.js fragt sie für jeden Eintrag nach der Ruhezeit; die Gestaltung (css/trophies.css) lädt der Abschnitt „Trophäen“,
// den jeder Hub hat
export const CARD_SOURCES = [trophyCard, shotsCard, newsCard, aboutCard];
