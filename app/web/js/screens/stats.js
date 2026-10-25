// Spielzeit-Rückblick „Deine Woche“ (E108, Teil P9). Bildschirm-IDs (Bauplan docs/research/e103/SPEC.md 4.4):
//   stats
// Daten von lib.stats {range, offset} (alle Quellen, Sitzungsverlauf E99). Links Gesamtzeit, Vergleich mit
// dem Zeitraum davor, Balken je Tag (Monat: je Tag, Insgesamt: je Monat), meiste Zeit, Sitzungen und die
// längste; rechts die meistgespielten und die neu dazugekommenen Spiele als Zeilen (Kreuz: Spielseite).
//   L1/R1      Woche, Monat, Insgesamt        Links/Rechts   älter/neuer blättern
//   L2/R2      in der Liste seitenweise       Kreis          zurück
// Einstieg: Rundknopf „Statistik“ in der Bibliothek (screens/library.js). Balken stehen über --p, ohne Bewegung.
import { h, icon } from "../core/dom.js";
import { call, notify } from "../core/bridge.js";
import { nav } from "../core/nav.js";
import { router } from "../core/router.js";
import { sound } from "../core/sound.js";
import { S, fmt, playtimeText, whenText } from "../core/strings.js";
import { createRowList } from "../components/rows.js";

const RANGES = [
  { id: "week", tab: S.rbTabWeek, title: S.rbTitleWeek, more: S.rbMoreWeek, less: S.rbLessWeek, same: S.rbSameWeek },
  { id: "month", tab: S.rbTabMonth, title: S.rbTitleMonth, more: S.rbMoreMonth, less: S.rbLessMonth, same: S.rbSameMonth },
  { id: "all", tab: S.rbTabAll, title: S.rbTitleAll },
];
const MONTH_LABEL_DAYS = new Set([1, 5, 10, 15, 20, 25, 30]);     // Monat: nur diese Tage beschriftet

// "2026-10-21" bzw. "2026-10" als Datum um 12 Uhr Ortszeit (die Zeitumstellung verschiebt so keinen Tag)
function dayOf(key) {
  const [y, m, d] = String(key).split("-").map(Number);
  return new Date(y, (m || 1) - 1, d || 1, 12);
}
const fmtDate = (date, opts) => date.toLocaleDateString(S.locale, opts);
function todayKey(range) {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return range === "all" ? d.getFullYear() + "-" + p(d.getMonth() + 1) : d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
}

function dayLabel(range, key) {
  const date = dayOf(key);
  if (range === "week") return fmtDate(date, { weekday: "short" });
  if (range === "all") return fmtDate(date, { month: "short" });
  return MONTH_LABEL_DAYS.has(date.getDate()) ? String(date.getDate()) : "";
}

function peakName(range, key) {
  const date = dayOf(key);
  if (range === "week") return fmtDate(date, { weekday: "long" });
  if (range === "all") return fmtDate(date, { month: "long", year: "numeric" });
  return fmtDate(date, { day: "numeric", month: "long" });
}

function create(params = {}) {
  const state = { range: RANGES.some((r) => r.id === params.range) ? params.range : "week", offset: 0, data: null, failed: false };
  const titleEl = h("h1.flow-title.ell");
  const subEl = h("div.flow-sub.ell");
  const tabEls = RANGES.map((r) => h("div.rb-tab", { dataset: { range: r.id }, text: r.tab }));
  const totalEl = h("div.rb-total.ell");
  const compareEl = h("div.rb-compare.ell");
  const chart = h("div.rb-chart");
  const peakEl = h("div.rb-peak.ell");
  const sessionsEl = h("div.rb-sessions.ell");
  const list = createRowList({ alwaysFocus: true, emptyText: S.rbEmpty, onPress: (row) => { open(row); return true; } });
  list.scrollbar.classList.add("flow-scroll", "rb-scroll");
  const hint = (glyph, text) => h("div.hint", glyph.startsWith("btn-") ? icon(glyph) : h("span.keycap", { text: glyph }), h("span", { text }));
  const el = h("div.flow.rb",
    titleEl, subEl, h("div.rb-tabs", tabEls),
    h("div.rb-panel", totalEl, compareEl, chart, peakEl, sessionsEl),
    h("div.rb-list", list.el), list.scrollbar,
    h("div.hints", hint(S.rbKeysRange, S.rbHintRange), hint(S.rbKeysPage, S.rbHintPage),
      hint("btn-cross", S.rbHintOpen), hint("btn-circle", S.hintBack)));
  let serial = 0;

  const range = () => RANGES.find((r) => r.id === state.range);

  function paintHead() {
    const r = range();
    titleEl.textContent = r.title;
    for (const t of tabEls) t.classList.toggle("is-active", t.dataset.range === r.id);
  }

  function compareText(d) {
    const r = range();
    if (r.id === "all" || typeof d.previous_total !== "number") return "";
    if (!d.total && !d.previous_total) return "";
    const diff = (d.total || 0) - d.previous_total;
    if (Math.abs(diff) < 60) return r.same;
    return fmt(diff > 0 ? r.more : r.less, { t: playtimeText(Math.abs(diff)) });
  }

  function paintChart(d) {
    const days = d.days || [];
    const max = Math.max(1, ...days.map((x) => x.seconds || 0));
    const now = d.offset === 0 ? todayKey(d.range) : "";
    chart.classList.toggle("is-many", days.length > 12);
    chart.replaceChildren(...days.map((x) => h("div.rb-bar" + (x.seconds ? "" : ".is-empty") + (x.date === now ? ".is-now" : ""),
      { dataset: { date: x.date, seconds: String(x.seconds || 0) } },
      h("div.rb-bar-box", h("i.rb-bar-fill", { style: { "--p": String((x.seconds || 0) / max) } })),
      h("span.rb-day", { text: dayLabel(d.range, x.date) }))));
    const peak = days.reduce((best, x) => ((x.seconds || 0) > (best ? best.seconds : 0) ? x : best), null);
    peakEl.textContent = peak ? fmt(d.range === "all" ? S.rbPeakMonth : S.rbPeakDay,
      { day: peakName(d.range, peak.date), t: playtimeText(peak.seconds) }) : "";
  }

  function sessionsText(d) {
    if (!d.sessions) return "";
    const parts = [fmt(S.rbSessions, { n: d.sessions })];
    const l = d.longest;
    if (l) {
      const date = new Date(l.start * 1000);
      const when = d.range === "week" ? fmtDate(date, { weekday: "short" }) : fmtDate(date, { day: "numeric", month: "short" });
      parts.push(fmt(S.rbLongest, { t: playtimeText(l.seconds), title: l.title || S.rbGone, when }));
    }
    return parts.join(" · ");
  }

  function rows(d) {
    const out = [];
    const top = d.top || [];
    if (!top.length) out.push({ id: "_none", type: "info", label: S.rbEmpty, enabled: false, section: S.rbTop });
    for (const t of top) {
      const entry = t.entry || null;
      out.push({ id: "top:" + t.id, type: entry ? "action" : "info", section: S.rbTop, label: t.title || S.rbGone,
        value: playtimeText(t.seconds), note: t.sessions === 1 ? S.rbSessionsOne : fmt(S.rbSessionsMany, { n: t.sessions }),
        art: entry || { id: t.id, title: t.title || S.rbGone, kind: "game", art: {} }, entry });
    }
    for (const a of d.added || []) {
      const entry = a.entry || null;
      out.push({ id: "new:" + a.id, type: entry ? "action" : "info", section: S.rbAdded, label: a.title,
        value: whenText(a.added), art: entry || { id: a.id, title: a.title, kind: "game", art: {} }, entry });
    }
    return out;
  }

  function paint() {
    paintHead();
    const d = state.data;
    if (!d) {
      subEl.textContent = state.failed ? S.errorGeneric : "";
      totalEl.textContent = state.failed ? "" : S.setLoading;
      compareEl.textContent = "";
      chart.replaceChildren();
      peakEl.textContent = "";
      sessionsEl.textContent = "";
      list.setRows([]);
      return;
    }
    subEl.textContent = d.range === "all" ? S.rbAllSub : d.label || "";
    totalEl.textContent = d.total ? playtimeText(d.total) : S.rbTotalNone;
    compareEl.textContent = compareText(d);
    paintChart(d);
    sessionsEl.textContent = sessionsText(d);
    list.setRows(rows(d));
    nav.ensure();
  }

  async function load() {
    const mine = ++serial;
    let d = null;
    try {
      d = await call("lib.stats", { range: state.range, offset: state.offset });
    } catch (e) {
      notify("sys.log", { level: "warning", text: "[stats] lib.stats: " + (e && e.message) });
    }
    if (mine !== serial) return;
    state.failed = !d;
    state.data = d;
    paint();
  }

  function setRange(step) {
    const at = RANGES.findIndex((r) => r.id === state.range);
    const next = RANGES[at + step];
    if (!next) { sound.play("edge"); return; }
    sound.play("tab");
    state.range = next.id;
    state.offset = 0;
    state.data = null;
    paint();
    load();
  }

  // Links = älter, Rechts = neuer; weiter als bis zur ersten Sitzung bzw. über heute hinaus geht es nicht
  function page(step) {
    const d = state.data;
    const older = step > 0;
    if (state.range === "all" || !d || (older && !d.older) || (!older && state.offset === 0)) { sound.play("edge"); return; }
    sound.play("focus");
    state.offset += older ? 1 : -1;
    load();
  }

  function open(row) {
    if (row && row.entry) router.go("hub", { entry: row.entry });
  }

  function onPad(button, ev) {
    switch (button) {
      case "l1": if (!ev.repeat) setRange(-1); return true;
      case "r1": if (!ev.repeat) setRange(1); return true;
      case "left": page(1); return true;
      case "right": page(-1); return true;
      case "l2": case "r2": list.page(button === "r2" ? 1 : -1); return true;
      default: return false;
    }
  }

  // Neue Sitzung, neues Spiel: neu laden (nachgetragene Bilder allein ändern hier nichts Wesentliches)
  function onEvent(event, payload) {
    if (event === "lib.changed" && !(payload && payload.reason === "art")) load();
  }

  paint();
  load();
  return {
    el, onPad, onEvent,
    onShow() { nav.ensure(); list.update(); },
    destroy() { serial += 1; },
  };
}

export const screens = [{ id: "stats", create }];
