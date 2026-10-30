// Steam-Angaben zu einem Eintrag (E123): game.info, game.news, game.trailer des Kerns, je Eintrag kurz gemerkt.
// Der Kern hält die Antworten selbst auf der Platte vor (Angaben 1 Tag, Neuigkeiten 3 Stunden); hier geht es nur
// darum, dass Home und Spielseite beim Hin und Her nicht jedes Mal einen Auftrag in den Arbeits-Thread geben.
// Eine Antwort ohne Angaben ({ok: false, reason}) wird kürzer gemerkt: Kein Netz soll nicht lange gelten.
import { call } from "../core/bridge.js";

const KEEP_MS = 10 * 60 * 1000;
const KEEP_FAIL_MS = 60 * 1000;
const MAX = 40;
const memo = new Map();           // "<methode> <id>" -> {at, keep, promise}

// Antwort des Kerns unter key merken (auch für die Trophäen, components/trophies.js). Das Promise scheitert nie: Ein Fehler
// wird zu {ok: false, reason: "error"} und nicht gemerkt, eine Antwort ohne Angaben nur keepFail Millisekunden.
export function memoCall(cache, key, method, params, { keep, keepFail, max }) {
  const now = Date.now();
  const hit = cache.get(key);
  if (hit && now - hit.at < hit.keep) return hit.promise;
  const item = { at: now, keep, promise: null };
  item.promise = call(method, params).then((r) => {
    if (!r || r.ok === false) item.keep = keepFail;
    return r || { ok: false };
  }, (e) => {
    item.keep = 0;
    return { ok: false, reason: "error", message: (e && e.message) || "" };
  });
  cache.set(key, item);
  while (cache.size > max) cache.delete(cache.keys().next().value);
  return item.promise;
}

function ask(method, id, extra) {
  const params = extra ? { ...extra } : { id };
  return memoCall(memo, method + " " + (extra ? JSON.stringify(params) : id), method, params,
    { keep: KEEP_MS, keepFail: KEEP_FAIL_MS, max: MAX });
}

// Steam-Angaben gibt es nur für Spiele (Systemkacheln und Apps fragen gar nicht erst)
export function wantsInfo(entry) {
  return !!(entry && entry.id && !entry.system && entry.kind === "game");
}

export const gameInfo = (id) => ask("game.info", id);
export const gameNews = (id) => ask("game.news", id);
export const gameTrailer = (id) => ask("game.trailer", id);
// Store (E132): Trailer zu einer Steam-App-ID ohne Eintrag; movie: einer aus game.trailers, full: groß bis 1080p
export const storeTrailers = (appid) => ask("game.trailers", "", { appid });
export const storeTrailer = (appid, movie, full = false) => ask("game.trailer", "", { appid, movie, full });
