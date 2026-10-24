// Brücke zwischen Oberfläche und Python-Kern.
//   couch.call(method, params) -> Promise mit dem Ergebnis (abgelehnt mit Error{error, message})
//   couch.on(event, fn)        -> Abmelde-Funktion
// Python ruft window.__couch.dispatch(event, payload) auf.
// Fehlt der Kern (Seite läuft außerhalb des couch-WebView) oder steht ?mock=1 in der URL,
// antwortet stattdessen die Attrappe aus dev/mock-backend.js.

const query = new URLSearchParams(location.search);
export const role = query.get("role") === "overlay" ? "overlay" : "home";
export const params = query;

const native = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.couch;
export const isMock = !native || query.has("mock");

const listeners = new Map();   // Ereignis -> Set von Funktionen
const anyListeners = new Set();

function emit(event, payload) {
  const data = payload === undefined || payload === null ? {} : payload;
  for (const fn of [...(listeners.get(event) || [])]) {
    try { fn(data, event); } catch (e) { console.error("Ereignis " + event + ": " + (e && e.stack || e)); }
  }
  for (const fn of [...anyListeners]) {
    try { fn(event, data); } catch (e) { console.error("Ereignis " + event + ": " + (e && e.stack || e)); }
  }
  return true;
}

export function on(event, fn) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(fn);
  return () => listeners.get(event).delete(fn);
}

// Alle Ereignisse (für den Router, der sie an die Bildschirme weiterreicht)
export function onAny(fn) {
  anyListeners.add(fn);
  return () => anyListeners.delete(fn);
}

export class RpcError extends Error {
  constructor(error, message, method) {
    super(message || error || "Fehler");
    this.name = "RpcError";
    this.error = error || "Error";
    this.method = method;
  }
}

let mockReady = null;
function mock() {
  if (!mockReady) {
    mockReady = import("../../dev/mock-backend.js").then((m) => m.createMock({ role, emit, query }));
  }
  return mockReady;
}

async function callNative(method, callParams) {
  let raw;
  try {
    raw = await native.postMessage(JSON.stringify({ method, params: callParams }));
  } catch (e) {
    throw new RpcError("BridgeError", String(e && e.message || e), method);
  }
  let reply;
  try { reply = typeof raw === "string" ? JSON.parse(raw) : raw; } catch (e) {
    throw new RpcError("BridgeError", "Unlesbare Antwort auf " + method, method);
  }
  if (!reply || reply.ok !== true) {
    throw new RpcError(reply && reply.error, reply && reply.message, method);
  }
  return reply.result === undefined ? null : reply.result;
}

async function callMock(method, callParams) {
  const backend = await mock();
  // Prüfhilfe nur für die Attrappe (tools/web-shot, js:-Schritt): window.__couchFail =
  // {"store.list": "Timeout"} lässt diese Methode mit dem Fehler scheitern, bis der Eintrag fehlt
  const forced = window.__couchFail && window.__couchFail[method];
  if (forced) throw new RpcError(String(forced), "Attrappe: " + method + " antwortet nicht.", method);
  // Über JSON, damit die Attrappe dieselben Grenzen hat wie der echte Weg (keine Objektbezüge)
  let result;
  try {
    result = await backend.call(method, JSON.parse(JSON.stringify(callParams)));
  } catch (e) {
    throw new RpcError(e.error || e.name, e.message, method);
  }
  return result === undefined ? null : JSON.parse(JSON.stringify(result));
}

export function call(method, callParams = {}) {
  return isMock ? callMock(method, callParams) : callNative(method, callParams);
}

// Aufruf, dessen Scheitern die Bedienung nicht aufhalten darf (Herzschlag, Protokoll)
export function notify(method, callParams = {}) {
  return call(method, callParams).catch(() => null);
}

export const couch = { call, on, onAny, notify, role, isMock };
window.couch = couch;
window.__couch = { dispatch: emit };
if (isMock) mock();
