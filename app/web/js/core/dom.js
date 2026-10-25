// Kleine DOM-Helfer: h() baut Elemente, icon() ein Symbol aus icons.svg.

const SVG_NS = "http://www.w3.org/2000/svg";

// h("div.klasse#id", {attr}, ...kinder)  – Kinder: Knoten, Text, Listen; null/false wird übergangen
export function h(spec, attrs, ...children) {
  const m = /^([a-z0-9]+)?((?:[.#][\w-]+)*)$/i.exec(spec) || [];
  const el = document.createElement(m[1] || "div");
  for (const part of (m[2] || "").match(/[.#][\w-]+/g) || []) {
    if (part[0] === ".") el.classList.add(part.slice(1)); else el.id = part.slice(1);
  }
  if (attrs && (attrs instanceof Node || typeof attrs !== "object" || Array.isArray(attrs))) {
    children.unshift(attrs);
    attrs = null;
  }
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className += (el.className ? " " : "") + value;
    else if (key === "text") el.textContent = value;
    else if (key === "style" && typeof value === "object") for (const [k, v] of Object.entries(value)) el.style.setProperty(k, v);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.firstChild.remove();
  return el;
}

export function icon(name, cls) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "ic" + (cls ? " " + cls : ""));
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", "#" + name);
  svg.append(use);
  return svg;
}

// Symbol nachträglich wechseln (z. B. Ton ein/aus)
export function setIcon(svg, name) {
  const use = svg.querySelector("use");
  if (use) use.setAttribute("href", "#" + name);
}

// Die Symbole stehen als Sprite im Dokument, damit <use href="#name"> ohne weiteren Abruf zeichnet
export async function loadIcons(url = "icons.svg") {
  if (document.getElementById("sprite")) return;
  const text = await (await fetch(url)).text();
  const doc = new DOMParser().parseFromString(text, "image/svg+xml");
  const svg = document.importNode(doc.documentElement, true);
  svg.id = "sprite";
  svg.removeAttribute("style");
  document.body.prepend(svg);
}

// Zeit-Token aus tokens.css in Millisekunden (Zahlenwerte stehen nur dort)
const msCache = new Map();
export function cssMs(name) {
  if (!msCache.has(name)) {
    const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    const value = parseFloat(raw) || 0;
    msCache.set(name, /ms$/.test(raw) ? value : value * 1000);
  }
  return msCache.get(name);
}

// Längen-Token (rem) in echten Pixeln der aktuellen Auflösung
export function cssPx(name) {
  const root = getComputedStyle(document.documentElement);
  const raw = root.getPropertyValue(name).trim();
  const value = parseFloat(raw) || 0;
  return /rem$/.test(raw) ? value * parseFloat(root.fontSize) : value;
}

// Lage eines Elements, als stünde sein Bildschirm schon still: während des Übergangs ist der
// Bildschirm leicht vergrößert, daran ausgerichtete Tafeln säßen sonst knapp daneben
export function settledRect(el) {
  const r = el.getBoundingClientRect();
  const screen = el.closest(".screen");
  if (!screen) return r;
  const m = new DOMMatrixReadOnly(getComputedStyle(screen).transform);
  if (m.a === 1 && m.d === 1) return r;
  const s = screen.getBoundingClientRect();
  const cx = s.left + s.width / 2;
  const cy = s.top + s.height / 2;
  const left = cx + (r.left - cx) / m.a;
  const top = cy + (r.top - cy) / m.d;
  const width = r.width / m.a;
  const height = r.height / m.d;
  return { left, top, width, height, right: left + width, bottom: top + height };
}

export function reducedMotion() {
  return document.documentElement.classList.contains("reduce-motion");
}

// Wartet das Ende eines Übergangs ab (über die Zeit aus den Tokens, nicht über transitionend:
// das Ereignis bleibt aus, wenn die Oberfläche nicht gezeichnet wird)
export function after(tokenOrMs, fn) {
  const ms = typeof tokenOrMs === "number" ? tokenOrMs : (reducedMotion() ? cssMs("--t-reduced") : cssMs(tokenOrMs));
  return setTimeout(fn, ms);
}

// Farbton für Platzhalter aus einem Text (gleichbleibend je Titel)
export function hueOf(text) {
  let hash = 7;
  for (const ch of String(text || "")) hash = (hash * 31 + ch.codePointAt(0)) % 360;
  return hash;
}

// Klasse für den Anfangszustand setzen und im nächsten Bild entfernen: startet den Übergang
export function enter(el, cls = "is-enter") {
  el.classList.add(cls);
  void el.offsetWidth;
  requestAnimationFrame(() => el.classList.remove(cls));
  // Falls gerade nicht gezeichnet wird (Fenster verdeckt), trotzdem fertig werden
  setTimeout(() => el.classList.remove(cls), 50);
}

// Bytes als deutsche Angabe ("12,6 GB"); leer bei 0 oder Unsinn
// Dezimalzeichen nach der Sprache der Seite (?lang=, wie strings.js; 2026-10-25)
const DECIMAL = typeof location !== "undefined" && new URLSearchParams(location.search).get("lang") === "en" ? "." : ",";

export function sizeText(bytes) {
  let value = Number(bytes);
  if (!Number.isFinite(value) || value <= 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
  const text = i < 2 ? String(Math.round(value)) : (value >= 100 ? String(Math.round(value)) : value.toFixed(1).replace(".", DECIMAL));
  return text + " " + units[i];
}

// Wartet, bis ein Bild geladen und dekodiert ist (für Wechsel ohne Flackern). Liefert false,
// wenn es das Bild nicht gibt.
export async function decoded(img) {
  try { await img.decode(); return true; } catch (e) { return img.complete && img.naturalWidth > 0; }
}

// Raster mit fester Spaltenzahl (Store, Bibliothek), gerollt in ganzen Reihen: Die Reihe des
// Elements an Stelle index bleibt in den beiden voll sichtbaren. Liefert die neue oberste Reihe
// (index < 0: ganz oben). Ein Baustein statt je einer Fassung im Bildschirm (E99).
export function gridTop(index, cols, top) {
  if (index < 0) return 0;
  const row = Math.floor(index / cols);
  if (row < top) return row;
  return row > top + 1 ? row - 1 : top;
}
