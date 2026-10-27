// Steam-Angaben zu einem Eintrag (E123): game.info, game.news, game.trailer des Kerns, je Eintrag kurz gemerkt.
// Der Kern hält die Antworten selbst auf der Platte vor (Angaben 1 Tag, Neuigkeiten 3 Stunden); hier geht es nur
// darum, dass Home und Spielseite beim Hin und Her nicht jedes Mal einen Auftrag in den Arbeits-Thread geben.
// Eine Antwort ohne Angaben ({ok: false, reason}) wird kürzer gemerkt: Kein Netz soll nicht lange gelten.
import { call } from "../core/bridge.js";

const KEEP_MS = 10 * 60 * 1000;
const KEEP_FAIL_MS = 60 * 1000;
const MAX = 40;
const memo = new Map();           // "<methode> <id>" -> {at, keep, promise}

function ask(method, id, extra) {
  const params = extra ? { ...extra } : { id };
  const key = method + " " + (extra ? JSON.stringify(params) : id);
  const now = Date.now();
  const hit = memo.get(key);
  if (hit && now - hit.at < hit.keep) return hit.promise;
  const item = { at: now, keep: KEEP_MS, promise: null };
  item.promise = call(method, params).then((r) => {
    if (!r || r.ok === false) item.keep = KEEP_FAIL_MS;
    return r || { ok: false };
  }, (e) => {
    item.keep = 0;
    return { ok: false, reason: "error", message: (e && e.message) || "" };
  });
  memo.set(key, item);
  while (memo.size > MAX) memo.delete(memo.keys().next().value);
  return item.promise;
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
