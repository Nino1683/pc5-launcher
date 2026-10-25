// Wortvorschläge für die Bildschirmtastatur: die häufigsten deutschen Wörter (data/words-de.txt) und
// Wörter, die im PC5 Launcher selbst getippt wurden. Gelernt wird nie aus Kennwörtern und nie aus
// Eingaben an andere Programme (Launcher, Spiele): Dort könnte ein Kennwort stehen.
// Nach einem „@“ kommen gängige E-Mail-Endungen.
const DOMAINS = ["gmail.com", "web.de", "gmx.de", "gmx.net", "outlook.com", "outlook.de", "hotmail.com",
  "icloud.com", "t-online.de", "yahoo.com", "yahoo.de", "freenet.de", "posteo.de"];
const LEARN_KEY = "couch.osk.words";
const LEARN_MAX = 500;
const WORD = /^[A-Za-zÄÖÜäöüß]+$/;

let words = null;
let loading = null;

export function loadWords() {
  if (!loading) {
    // Englisch (2026-10-25): eigene Wortliste; die Sprache steht in der Adresse (?lang=, wie strings.js)
    const en = typeof location !== "undefined" && new URLSearchParams(location.search).get("lang") === "en";
    loading = fetch(en ? "data/words-en.txt" : "data/words-de.txt")
      .then((r) => r.text())
      .then((t) => { words = t.split("\n").filter((l) => l && !l.startsWith("#")); })
      .catch(() => { words = []; });
  }
  return loading;
}

function learned() {
  try {
    const list = JSON.parse(localStorage.getItem(LEARN_KEY) || "[]");
    return Array.isArray(list) ? list : [];
  } catch {
    return [];
  }
}

// Wörter eines fertigen Texts merken (vorn = zuletzt benutzt)
export function learn(text) {
  const fresh = String(text || "").split(/\s+/).filter((w) => w.length >= 3 && WORD.test(w)).map((w) => w.toLowerCase());
  if (!fresh.length) return;
  const list = [...new Set([...fresh.reverse(), ...learned()])].slice(0, LEARN_MAX);
  try { localStorage.setItem(LEARN_KEY, JSON.stringify(list)); } catch { /* ohne Speicher: nur nicht lernen */ }
}

// Bis zu n Vorschläge für das angefangene Wort; Groß-/Kleinschreibung wie getippt
export function suggest(word, n = 4) {
  if (!word) return [];
  const at = word.lastIndexOf("@");
  if (at >= 0) {
    const head = word.slice(0, at + 1);
    const tail = word.slice(at + 1).toLowerCase();
    if (!/^[a-z0-9.-]*$/.test(tail)) return [];
    return DOMAINS.filter((d) => d.startsWith(tail) && d !== tail).slice(0, n).map((d) => head + d);
  }
  if (!WORD.test(word)) return [];
  const low = word.toLowerCase();
  const out = [];
  const add = (w) => { if (w.length > low.length && w.startsWith(low) && !out.includes(w)) out.push(w); };
  for (const w of learned()) { if (out.length >= n) break; add(w); }
  for (const w of words || []) { if (out.length >= n) break; add(w); }
  const allCaps = word.length > 1 && word === word.toUpperCase();
  const cap = word[0] !== word[0].toLowerCase();
  return out.map((w) => (allCaps ? w.toUpperCase() : cap ? w[0].toUpperCase() + w.slice(1) : w));
}
