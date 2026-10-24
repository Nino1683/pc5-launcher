// Texteingabe über die Bildschirmtastatur: ein Weg für alle Felder der Seite (E99). Vorher baute
// jeder Bildschirm osk.open selbst nach, und dem Installer fehlte dabei secret: Das Archiv-Kennwort
// stand im Klartext am Fernseher und landete in den gelernten Wörtern (E95).
//   secret   Kennwort oder Schlüssel: Die Tastatur verdeckt die Eingabe, zeigt keine Vorschläge
//            und lernt nichts. Ein verdecktes Feld beginnt immer leer (der alte Wert kommt nie zur Seite).
//   learn    false: offen, mit Vorschlägen, aber die Tastatur merkt sich nichts (App-Kennung, E100)
// osk.open {purpose: "field"} antwortet erst, wenn die Tastatur schließt: {text, cancelled}.
// Ergebnis: der getippte Text oder null (abgebrochen). Scheitert der Aufruf, wirft askText; wer
// einen Fehler zeigen will, fängt ihn selbst.
import { call } from "../core/bridge.js";

export async function askText({ title = "", initial = "", secret = false, learn = true } = {}) {
  const hidden = !!secret;          // nur ausdrücklich verdeckt; fehlt secret, ist das Feld offen
  const result = await call("osk.open", {
    purpose: "field", secret: hidden, title: String(title || ""), learn: learn !== false,
    initial: hidden || initial === null || initial === undefined ? "" : String(initial),
  });
  return result && !result.cancelled && typeof result.text === "string" ? result.text : null;
}
