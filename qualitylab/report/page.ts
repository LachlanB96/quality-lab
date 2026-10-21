// page.ts: the report page's script. build.py bundles it with esbuild into the page (index.html), next to the run
// data it embeds as JSON in #ql-data; this draws everything from that. Data goes in as text, never as HTML.
"use strict";
export {};

// ---------- the data build.py embeds ----------
type Cls = "pass" | "fail" | "known" | "skip" | "fixed";
type TargetKey = "local" | "live";
interface Summary {
  green: boolean; total: number; passed: number; failed: number; known: number; skipped: number; fixed: number;
  wall: number; started: string; target?: TargetKey; pass_rate?: number | null;
}
interface Test {
  id: string; file: string; suite: string; name: string; param: string; layer: string; area: string; cls: Cls;
  start: number | null; stop: number | null; dur: number; worker: string; doc: string; msg?: string;
}
interface Slow { id: string; file: string; name: string; param: string; layer: string; duration: number }
interface Protocol { covered: number; total: number; types: { type: string; sent: number; received: number }[] }
interface Target {
  summary: Summary; label: string; tick_ms: number; seeded: boolean; workers: number; commit: string;
  tests: Test[]; layers: Record<string, Partial<Record<Cls, number>>>; slowest: Slow[]; protocol: Protocol;
}
interface Finding { id: string; severity: string; status: string; title: string; impact: string; how: string; repro: string[]; fix: string; tests?: string[] }
interface Analysis {
  model: string; generated: string; runs?: unknown[];
  checks?: { passed?: number; total?: number; failed?: number; struck?: string[] };
  overview?: { headline?: string; paragraphs?: string[]; points?: { kind: string; text: string }[] };
}
interface Suggestion { id: string; status: string; kind?: string; title: string; why: string; effort?: string; proposed?: string; evidence?: string[]; note?: string }
interface Evals {
  title?: string; summary?: string; legend?: string; baseline?: string; candidate?: string;
  variants?: { name: string; scores: Record<string, number | undefined> }[];
  metrics?: { key: string; label: string }[];
  triage?: { kind: string; text: string }[];
}
interface Data {
  generated: string; repo: string; demo: string;
  targets: Partial<Record<TargetKey, Target>>;
  runs: Summary[];
  metrics: Record<string, { target: TargetKey; started: string; value: number }[]>;
  flaky: { target: string; id: string; sequence: string; fails: number; runs: number }[];
  findings: Finding[];
  analysis: Analysis | null;
  suggestions: Suggestion[];
  evals: Evals | null;
  review: { total: number; reviewed: number; rewritten: number; by_test: Record<string, string> };
}

const D: Data = JSON.parse(document.getElementById("ql-data")!.textContent!);
const $ = <T extends Element = HTMLElement>(id: string): T => document.getElementById(id) as unknown as T;
const SVGNS = "http://www.w3.org/2000/svg";

// ---------- small DOM helpers (data goes in as text, never as HTML) ----------
type Kid = Node | string | number | null | undefined | false | Kid[];
function el(tag: string, attrs?: Record<string, unknown> | null, ...kids: Kid[]): HTMLElement {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v as string;
    else if (k === "style") n.style.cssText = v as string;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v as EventListener);
    else n.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat() as Kid[])
    if (kid != null && kid !== false) n.append((kid as Node).nodeType ? kid as Node : document.createTextNode(String(kid)));
  return n;
}
function svg(tag: string, attrs?: Record<string, unknown>): SVGElement {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) n.setAttribute(k, String(v));
  return n;
}
const css = (name: string): string => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// ---------- formatting ----------
const nf = new Intl.NumberFormat("en-AU");
function secs(s: number | null | undefined): string {
  if (s == null) return "";
  if (s < 1) return (s * 1000).toFixed(0) + " ms";
  if (s < 60) return s.toFixed(s < 10 ? 2 : 1) + " s";
  const m = Math.floor(s / 60), r = Math.round(s - m * 60);
  return `${m} min ${r} s`;
}
function when(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString("en-AU", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
}
function ago(iso: string): string {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400 * 2) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " days ago";
}
const pct = (x: number | null | undefined): string => (x == null ? "n/a" : (x * 100).toFixed(x === 1 ? 0 : 1) + "%");

const STATUS: Record<string, { glyph: string; label: string }> = {
  pass:  { glyph: "✓", label: "Passed" },
  fail:  { glyph: "✗", label: "Failed" },
  known: { glyph: "!", label: "Known bug" },
  skip:  { glyph: "–", label: "Skipped" },
  fixed: { glyph: "↑", label: "Fixed? passes but marked xfail" },
};
const STATUS_VAR: Record<string, string> = { pass: "--good", fail: "--bad", known: "--warn", skip: "--skip", fixed: "--bad" };
const statusColor = (cls: string): string => css(STATUS_VAR[cls] || "--skip");
const glyph = (cls: string): HTMLElement => el("span", { class: "st " + cls, "aria-label": STATUS[cls].label, title: STATUS[cls].label }, STATUS[cls].glyph);

const TARGETS: Record<TargetKey, { name: string; color: string }> = {
  local: { name: "Local build", color: "--s-local" },
  live: { name: "Live demo", color: "--s-live" },
};
const LAYERS: Record<string, string> = {
  unit: "The lab's own code: the codec, the recorder, the analytics",
  contract: "What the server promises every client: order, shapes, sizes",
  integration: "One feature through a real server: crafting, combat, chat",
  e2e: "A whole player journey, start to finish",
  property: "Generated inputs: Hypothesis looks for a counterexample",
  robustness: "Hostile input: garbage, floods, lies about lengths",
  data: "Invariants over the content catalog every client receives",
  perf: "Timing budgets: tick cadence, round trips, login",
  ui: "The WebGL client in a real browser (Playwright)",
};

// ---------- tooltip ----------
const tip = $("tip");
function showTip(evt: Event, rows: Node[]): void {
  tip.replaceChildren(...rows);
  tip.classList.add("show");
  const target = evt.target as Element;
  const r = target.getBoundingClientRect ? target.getBoundingClientRect() : null;
  const pe = evt as Partial<PointerEvent>;   // a focus event has no pointer position: use the element's
  const x = pe.clientX ?? (r ? r.left + r.width / 2 : 0);
  const y = pe.clientY ?? (r ? r.top : 0);
  const w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = Math.max(8, Math.min(innerWidth - w - 8, x + 14)) + "px";
  tip.style.top = Math.max(8, y - h - 12 < 8 ? y + 18 : y - h - 12) + "px";
}
const hideTip = (): void => { tip.classList.remove("show"); };
function attachTip(node: Element, rowsFn: () => Node[]): void {
  node.addEventListener("pointermove", (e) => showTip(e, rowsFn()));
  node.addEventListener("pointerleave", hideTip);
  node.addEventListener("focus", (e) => showTip(e, rowsFn()));
  node.addEventListener("blur", hideTip);
}
function testTip(t: Test): Node[] {
  return [
    el("div", { class: "tv" }, STATUS[t.cls].glyph + " " + STATUS[t.cls].label + " · " + secs(t.dur)),
    el("div", { class: "tl" }, t.name + (t.param ? ` [${t.param}]` : "")),
    el("div", { class: "tm" }, `${t.suite}/${t.file} · ${t.layer}${t.worker !== "main" ? " · " + t.worker : ""}` + (t.msg ? "\n" + t.msg.split("\n")[0].slice(0, 160) : "")),
  ];
}

// ---------- links ----------
for (const id of ["repo-link", "repo-link-2", "repo-link-3"]) $<HTMLAnchorElement>(id).href = D.repo;
$<HTMLAnchorElement>("demo-link").href = D.demo;
$("generated").textContent = "Data generated " + when(D.generated);

// ---------- theme toggle ----------
(function theme() {
  const root = document.documentElement;
  try { const saved = localStorage.getItem("ql-theme"); if (saved) root.dataset.theme = saved; } catch (e) {}
  $("theme").addEventListener("click", () => {
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("ql-theme", root.dataset.theme); } catch (e) {}
    renderAll();
  });
})();

// ---------- hero: status cards and tiles ----------
function renderStatus(): void {
  const row = $("status-row");
  row.replaceChildren();
  for (const [key, meta] of Object.entries(TARGETS) as [TargetKey, { name: string; color: string }][]) {
    const tg = D.targets[key];
    if (!tg) continue;
    const s = tg.summary;
    const verdict = s.green ? "All green" : `${s.failed + s.fixed} need attention`;
    const ran = s.total - s.skipped;
    const bits = ([
      ["passed", s.passed], ["known bugs", s.known], ["failed", s.failed], ["skipped", s.skipped],
    ] as [string, number][]).filter(([k, v]) => v || k === "passed" || k === "failed");
    const mini = el("div", { class: "bar-mini", "aria-hidden": "true" });
    for (const [cls, n] of [["pass", s.passed], ["known", s.known], ["fail", s.failed + s.fixed], ["skip", s.skipped]] as [string, number][]) {
      if (n) mini.append(el("i", { style: `flex:${n};background:var(${STATUS_VAR[cls]})` }));
    }
    const detail = key === "local"
      ? `${secs(s.wall)} on ${tg.workers || 1} workers · ${tg.tick_ms} ms tick, seed ${7}`
      : `${secs(s.wall)} on ${tg.workers || 1} workers · real ${tg.tick_ms} ms tick · ${s.skipped} local-only tests skipped`;
    row.append(el("div", { class: "card status-card reveal" },
      el("div", { class: "head" },
        el("span", { class: "swatch", style: `background:var(${meta.color})`, "aria-hidden": "true" }),
        el("span", { class: "target-name" }, meta.name),
        el("span", { class: "when", title: when(s.started) }, ago(s.started))),
      el("div", { class: "verdict " + (s.green ? "green" : "red") },
        el("span", { class: "badge", "aria-hidden": "true" }, s.green ? "✓" : "✗"), verdict),
      el("div", { class: "counts" }, bits.map(([k, v]) => el("span", null, el("b", null, nf.format(v)), " " + k))),
      mini,
      el("div", { class: "meta" }, detail + ` · ${nf.format(ran)} tests ran`)));
  }
  const tiles = $("tiles");
  tiles.replaceChildren();
  const open = D.findings.filter((f) => f.status === "open").length;
  const local = D.targets.local, proto = local ? local.protocol : null;
  const rv = D.review;
  const reviewed = rv.reviewed + rv.rewritten;
  const tileData: [string, string, string][] = [
    ["Distinct tests", nf.format(rv.total), "across both targets"],
    ["Bugs found in SpireScape", nf.format(D.findings.length), `${open} still open`],
    ["Protocol messages covered", proto ? `${proto.covered}` : "n/a", proto ? `of ${proto.total} message types` : ""],
    ["Runs recorded", nf.format(D.runs.length), D.runs.length ? "since " + new Date(D.runs[0].started).toLocaleDateString("en-AU", { day: "numeric", month: "short" }) : ""],
  ];
  for (const [label, value, sub] of tileData) {
    tiles.append(el("div", { class: "card tile reveal" }, el("div", { class: "label" }, label), el("div", { class: "value" }, value), el("div", { class: "sub" }, sub)));
  }
  const share = rv.total ? reviewed / rv.total : 0;
  tiles.append(el("div", { class: "card tile reveal" },
    el("div", { class: "label" }, "Reviewed by me"),
    el("div", { class: "value" }, `${nf.format(reviewed)}`, el("small", null, ` / ${nf.format(rv.total)}`)),
    el("div", { class: "sub" }, rv.rewritten ? `${rv.rewritten} rewritten by hand` : "AI-drafted tests I've read line by line"),
    el("div", { class: "meter", role: "meter", "aria-valuemin": 0, "aria-valuemax": rv.total, "aria-valuenow": reviewed, "aria-label": "Tests reviewed" },
      el("i", { style: `width:${(share * 100).toFixed(1)}%` }))));
}

// ---------- replay ----------
interface Bar { t: Test; r: SVGElement; x0: number; x1: number }
interface Replay {
  target: TargetKey; speed: null; t: number; playing: boolean; raf: number; last: number; done: Set<string>; started: boolean;
  mult?: number; bars?: Bar[]; wall?: number; total?: number; byId?: Record<string, Test>;
}
const replay: Replay = { target: D.targets.local ? "local" : "live", speed: null, t: 0, playing: false, raf: 0, last: 0, done: new Set(), started: false };
const SPEEDS: [string, number][] = [["Normal", 1], ["Fast", 3], ["Real time", 0]];
function replayTests(): Test[] {
  const tg = D.targets[replay.target];
  return tg ? tg.tests.filter((t) => t.start != null && t.stop != null).sort((a, b) => a.start! - b.start!) : [];
}
function setupReplay(): void {
  const tabs = $("target-tabs");
  tabs.replaceChildren();
  for (const [key, meta] of Object.entries(TARGETS) as [TargetKey, { name: string; color: string }][]) {
    if (!D.targets[key]) continue;
    tabs.append(el("button", { type: "button", "aria-pressed": String(key === replay.target), onclick: () => { replay.target = key; setupReplay(); start(); } },
      el("span", { class: "swatch", style: `background:var(${meta.color})`, "aria-hidden": "true" }), meta.name));
  }
  const sp = $("speeds");
  sp.replaceChildren();
  for (const [label, mult] of SPEEDS) {
    sp.append(el("button", { type: "button", "aria-pressed": String((replay.mult ?? 1) === mult), onclick: () => { replay.mult = mult; setupReplay(); if (!replay.playing) start(); } }, label));
  }
  $("replay-legend").replaceChildren(...["pass", "known", "fail", "skip"].map((c) =>
    el("span", null, el("i", { class: "sw", style: `background:var(${STATUS_VAR[c]})` }), STATUS[c].label)),
    el("span", null, el("i", { class: "sw running", style: "background:var(--accent)" }), "Running"));
  drawLanes();
}
function drawLanes(): void {
  const tests = replayTests();
  const tg = D.targets[replay.target]!;
  const wall = tg.summary.wall || 1;
  const workers = [...new Set(tests.map((t) => t.worker))].sort();
  const s = $<SVGSVGElement>("lanes");
  // drawn at its real pixel width, so 12 px text is 12 px on screen
  const W = Math.max(300, Math.round(s.getBoundingClientRect().width || 640));
  const top = 6, axisH = 26, labelW = 46;
  const laneH = Math.max(30, Math.min(64, Math.floor(270 / Math.max(1, workers.length))));
  const H = top + workers.length * laneH + axisH;
  s.setAttribute("viewBox", `0 0 ${W} ${H}`);
  s.replaceChildren();
  const x = (sec: number): number => labelW + (sec / wall) * (W - labelW - 4);
  // recessive grid and axis
  const step = niceStep(wall, 6);
  for (let v = 0; v <= wall + 1e-9; v += step) {
    s.append(svg("line", { x1: x(v), x2: x(v), y1: top, y2: H - axisH + 4, stroke: css("--grid"), "stroke-width": 1 }));
    const label = svg("text", { x: x(v), y: H - 8, "text-anchor": "middle", fill: css("--muted"), "font-size": 12 });
    label.textContent = v === 0 ? "0" : `${Math.round(v)} s`;
    s.append(label);
  }
  workers.forEach((w, i) => {
    const t = svg("text", { x: 0, y: top + i * laneH + laneH / 2 + 4, fill: css("--muted"), "font-size": 12, "font-family": "JetBrains Mono, monospace" });
    t.textContent = w === "main" ? "main" : w;
    s.append(t);
    s.append(svg("rect", { x: labelW, y: top + i * laneH + 4, width: W - labelW - 4, height: laneH - 8, rx: 5, fill: css("--bg-3"), opacity: 0.55 }));
  });
  replay.bars = tests.map((t) => {
    const lane = workers.indexOf(t.worker);
    const r = svg("rect", { x: x(t.start!), y: top + lane * laneH + 7, width: 0, height: laneH - 14, rx: 3, fill: css("--accent"), tabindex: -1 });
    r.style.cursor = "pointer";
    attachTip(r, () => testTip(t));
    s.append(r);
    return { t, r, x0: x(t.start!), x1: Math.max(x(t.stop!), x(t.start!) + 2) };
  });
  replay.wall = wall;
  replay.total = tests.length;
}
function niceStep(range: number, ticks: number): number {
  const raw = range / ticks, p = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 5, 10]) if (raw <= m * p) return m * p;
  return 10 * p;
}
function logLine(t: Test): void {
  const term = $("terminal");
  const line = el("div", { class: "ln" }, glyph(t.cls), el("span", { class: "f" }, t.file), el("span", { class: "n" }, t.name + (t.param ? ` [${t.param}]` : "")), el("span", { class: "d" }, secs(t.dur)));
  attachTip(line, () => testTip(t));
  const stick = term.scrollHeight - term.scrollTop - term.clientHeight < 40;
  term.append(line);
  while (term.childElementCount > 400) term.firstElementChild!.remove();
  if (stick) term.scrollTop = term.scrollHeight;
}
function footCounts(): void {
  const c: Record<string, number> = { pass: 0, known: 0, fail: 0, skip: 0, fixed: 0 };
  for (const id of replay.done) c[replay.byId![id].cls]++;
  const skipped = 0;
  $("replay-foot").replaceChildren(
    el("span", null, el("b", null, nf.format(replay.done.size)), ` of ${nf.format(replay.total!)} done`),
    el("span", null, glyph("pass"), " ", el("b", null, nf.format(c.pass)), " passed"),
    el("span", null, glyph("known"), " ", el("b", null, nf.format(c.known)), " known bugs"),
    el("span", null, glyph("fail"), " ", el("b", null, nf.format(c.fail + c.fixed)), " failed"),
    // nothing when nothing was skipped (until 2026-10-21 a null here printed "null": replaceChildren turns it to text)
    ...(c.skip || skipped ? [el("span", null, glyph("skip"), " ", el("b", null, nf.format(c.skip + skipped)), " skipped")] : []));
}
function frame(now: number): void {
  const dt = (now - replay.last) / 1000;
  replay.last = now;
  const mult = replay.mult ?? 1;
  const speed = mult === 0 ? 1 : Math.max(1, replay.wall! / 16) * mult;
  replay.t = Math.min(replay.wall!, replay.t + dt * speed);
  paint();
  if (replay.t >= replay.wall!) { replay.playing = false; setPlayLabel(); return; }
  replay.raf = requestAnimationFrame(frame);
}
function paint(): void {
  const t = replay.t;
  for (const b of replay.bars!) {
    const { t: test, r } = b;
    if (test.start! > t) { r.setAttribute("width", "0"); continue; }
    const finished = test.stop! <= t;
    const frac = finished ? 1 : (t - test.start!) / Math.max(1e-9, test.stop! - test.start!);
    r.setAttribute("width", String(Math.max(2, frac * (b.x1 - b.x0))));
    if (finished) {
      r.setAttribute("fill", statusColor(test.cls));
      r.classList.remove("running");
      if (!replay.done.has(test.id)) { replay.done.add(test.id); logLine(test); }
    } else { r.setAttribute("fill", css("--accent")); r.classList.add("running"); }
  }
  $("progress").style.width = (100 * t / replay.wall!).toFixed(2) + "%";
  $("clock").textContent = `${t.toFixed(1)} s / ${replay.wall!.toFixed(1)} s`;
  footCounts();
}
function start(): void {
  cancelAnimationFrame(replay.raf);
  replay.t = 0; replay.done = new Set();
  replay.byId = Object.fromEntries(replayTests().map((t) => [t.id, t]));
  const tg = D.targets[replay.target]!;
  $("terminal").replaceChildren(el("div", { class: "sys" }, `$ pytest --target ${replay.target} -n ${tg.workers || 1}${replay.target === "live" ? " --dist loadgroup" : ""}`),
    el("div", { class: "sys" }, `collected ${tg.summary.total} items · run ${when(tg.summary.started)}`));
  replay.playing = true; replay.last = performance.now();
  setPlayLabel();
  replay.raf = requestAnimationFrame(frame);
}
function setPlayLabel(): void {
  const b = $("play");
  const finished = replay.t >= (replay.wall || 0) && replay.started;
  b.textContent = replay.playing ? "Pause" : finished ? "Replay" : "Play";
  b.setAttribute("aria-label", b.textContent + " the run");
}
$("play").addEventListener("click", () => {
  replay.started = true;
  if (replay.playing) { replay.playing = false; cancelAnimationFrame(replay.raf); setPlayLabel(); return; }
  if (replay.t >= replay.wall!) { start(); return; }
  replay.playing = true; replay.last = performance.now(); setPlayLabel(); replay.raf = requestAnimationFrame(frame);
});

// ---------- coverage ----------
function renderCoverage(): void {
  const tg = (D.targets.local || D.targets.live)!;
  const layers = Object.keys(LAYERS).filter((k) => tg.layers[k]);
  const max = Math.max(...layers.map((k) => sumRow(tg.layers[k])));
  $("layers-sub").textContent = `Latest ${D.targets.local ? "local" : "live"} run. Each bar is split by outcome.`;
  const rows = $("layer-rows");
  rows.replaceChildren();
  for (const k of layers) {
    const c = tg.layers[k];
    const n = sumRow(c);
    const track = el("div", { class: "ltrack", style: `width:${(100 * n / max).toFixed(1)}%` });
    for (const cls of ["pass", "known", "fail", "fixed", "skip"] as Cls[]) {
      if (!c[cls]) continue;
      const seg = el("i", { style: `flex:${c[cls]};background:var(${STATUS_VAR[cls]})`, tabindex: 0, "aria-label": `${k}: ${c[cls]} ${STATUS[cls].label}` });
      attachTip(seg, () => [el("div", { class: "tv" }, `${c[cls]} ${STATUS[cls].label.toLowerCase()}`), el("div", { class: "tl" }, `${k} layer`)]);
      track.append(seg);
    }
    rows.append(el("div", { class: "layer-row" }, el("span", { class: "lname" }, k), track, el("span", { class: "lnum" }, nf.format(n)), el("span", { class: "ldesc" }, LAYERS[k])));
  }
  $("layer-legend").replaceChildren(...["pass", "known", "skip"].map((cls) => el("span", null, el("i", { class: "sw", style: `background:var(${STATUS_VAR[cls]})` }), STATUS[cls].label)));

  // area x layer, merged over both targets (a test counted once, in the target where it ran)
  const counts: Record<string, Record<string, number>> = {}, seen = new Set<string>();
  for (const key of ["local", "live"] as TargetKey[]) {
    const t2 = D.targets[key];
    if (!t2) continue;
    for (const t of t2.tests) {
      if (t.cls === "skip" || seen.has(t.id)) continue;
      seen.add(t.id);
      const a = t.area || "other";
      (counts[a] ??= {})[t.layer] = ((counts[a] ?? {})[t.layer] || 0) + 1;
    }
  }
  const areas = Object.keys(counts).sort((a, b) => sumRow(counts[b]) - sumRow(counts[a]));
  const cols = Object.keys(LAYERS).filter((l) => areas.some((a) => counts[a][l]));
  const cmax = Math.max(...areas.flatMap((a) => cols.map((l) => counts[a][l] || 0)));
  const table = $("matrix");
  table.replaceChildren(el("thead", null, el("tr", null, el("th", { scope: "col" }, ""), cols.map((c) => el("th", { scope: "col" }, c)))));
  const body = el("tbody");
  for (const a of areas) {
    body.append(el("tr", null, el("th", { scope: "row" }, a), cols.map((l) => {
      const v = counts[a][l] || 0;
      if (!v) return el("td", { class: "z" }, "0");
      const strength = 0.16 + 0.78 * Math.sqrt(v / cmax);
      const td = el("td", { tabindex: 0, style: `background:rgba(${css("--mag")},${strength.toFixed(2)});color:${strength > 0.55 ? "#fff" : "var(--ink)"}`, "aria-label": `${a}, ${l}: ${v} tests` }, String(v));
      attachTip(td, () => [el("div", { class: "tv" }, `${v} test${v > 1 ? "s" : ""}`), el("div", { class: "tl" }, `${a} · ${l}`)]);
      return td;
    })));
  }
  table.append(body);

  // protocol chips from the local run, which drives every message type
  const proto = (D.targets.local || D.targets.live)!.protocol;
  $("proto-sub").textContent = `SpireScape's client and server talk in ${proto.total} message types. Lit chips are the ones the latest ${D.targets.local ? "local" : "live"} run sent or received, with counts: ${proto.covered} of ${proto.total}.`;
  const groups = [["Client to server", proto.types.slice(0, 9), "sent"], ["Server to client", proto.types.slice(9), "received"]] as [string, Protocol["types"], "sent" | "received"][];
  $("proto").replaceChildren(...groups.map(([title, rows, key]) => el("div", { class: "proto-group" },
    el("h3", null, title),
    el("div", { class: "chips" }, rows.map((r) => {
      const n = r[key];
      return el("span", { class: "chip " + (n ? "on" : "off"), title: n ? `${n} ${key}` : "not exercised" }, r.type, el("span", { class: "cnt" }, n ? (key === "sent" ? "↑" : "↓") + compact(n) : "none"));
    })))));
}
const sumRow = (o: Record<string, number | undefined>): number => Object.values(o).reduce<number>((a, b) => a + (b as number), 0);
const compact = (n: number): string => (n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k" : String(n));

// ---------- bugs ----------
function renderBugs(): void {
  const wrap = $("bug-cards");
  wrap.replaceChildren();
  for (const f of D.findings) {
    const state = f.status === "fixed" ? ["fixed", "Fixed, test now passes"] : f.status === "open" ? ["known", "Open, pinned by xfail"] : ["skip", "Not run in the latest runs"];
    wrap.append(el("article", { class: "card bug reveal" },
      el("div", { class: "top" }, f.id, el("span", { class: "sev " + f.severity }, f.severity), el("span", { class: "state" }, glyph(state[0]), state[1])),
      el("h3", null, f.title),
      el("p", null, f.impact),
      el("p", { class: "how" }, "How it was found: " + f.how),
      el("details", { class: "more" },
        el("summary", null, "Repro and fix"),
        el("ol", null, f.repro.map((s) => el("li", null, s))),
        el("p", { class: "fix" }, el("b", null, "Suggested fix: "), f.fix),
        f.tests && f.tests.length ? el("div", { class: "pins" }, f.tests.map((tid) => el("span", { class: "pin" }, tid.split("::").pop()))) : null)));
  }
}

// ---------- trends ----------
interface Point { x: Date; y: number }
interface Series { tgt: TargetKey; pts: Point[] }
interface Chart { title: string; sub: string; fmt: (v: number) => string; min?: number; max?: number; target?: number; s: Series[] }
function renderTrends(): void {
  const wrap = $("trend-charts");
  wrap.replaceChildren();
  const runs = D.runs;
  const n = { local: runs.filter((r) => r.target === "local").length, live: runs.filter((r) => r.target === "live").length };
  $("trends-sub").textContent = `One point per recorded run: ${n.local} local and ${n.live} live so far.` + (runs.length < 6 ? " The lines fill in as the nightly runs add up." : "");
  const series = (_key: string, fn: (r: Summary) => number | null | undefined): Series[] => (Object.keys(TARGETS) as TargetKey[]).map((tgt) => ({
    tgt, pts: runs.filter((r) => r.target === tgt).map((r) => ({ x: new Date(r.started), y: fn(r) })).filter((p): p is Point => p.y != null),
  }));
  const metric = (name: string, tgt: TargetKey): Point[] => (D.metrics[name] || []).filter((p) => p.target === tgt).map((p) => ({ x: new Date(p.started), y: p.value }));
  const charts: Chart[] = [
    { title: "Pass rate", sub: "Known bugs and skips don't count either way.", fmt: (v) => (v * 100).toFixed(v === 1 ? 0 : 1) + "%", min: 0.9, max: 1, s: series("rate", (r) => r.pass_rate) },
    { title: "Suite time", sub: "Wall-clock time for the whole run.", fmt: (v) => Math.round(v) + " s", min: 0, s: series("wall", (r) => r.wall) },
    { title: "Server tick on the live demo", sub: "Median gap between movement updates. The game's design says 600 ms.", fmt: (v) => Math.round(v) + " ms", min: 500, max: 700, target: 600, s: [{ tgt: "live", pts: metric("tick_median_ms", "live") }] },
    { title: "Round trip on the live demo", sub: "Examine an NPC and wait for the reply: the 95th percentile of 20 tries.", fmt: (v) => Math.round(v) + " ms", min: 0, s: [{ tgt: "live", pts: metric("examine_rtt_p95_ms", "live") }] },
  ];
  for (const c of charts) {
    const card = el("div", { class: "card pad reveal" });
    wrap.append(card);
    lineChart(card, c, Math.max(260, card.clientWidth - 42));
  }
}
function lineChart(card: HTMLElement, c: Chart, W: number): HTMLElement {
  card.append(el("h3", null, c.title), el("p", { class: "card-sub" }, c.sub));
  const all = c.s.flatMap((s) => s.pts);
  if (!all.length) { card.append(el("p", { class: "empty-note" }, "No data yet.")); return card; }
  // the latest value per series, as the headline number
  card.append(el("div", { class: "latest" }, c.s.filter((x) => x.pts.length).map((x) => {
    const last = x.pts.slice().sort((a, b) => +a.x - +b.x).at(-1)!;
    return el("span", null, el("i", { class: "sw", style: `background:var(${TARGETS[x.tgt].color})` }), el("b", null, c.fmt(last.y)), " " + TARGETS[x.tgt].name.toLowerCase());
  })));
  const H = 170, L = 52, R = 14, T = 12, B = 24;
  let ymin = c.min ?? Math.min(...all.map((p) => p.y)), ymax = c.max ?? Math.max(...all.map((p) => p.y));
  ymin = Math.min(ymin, ...all.map((p) => p.y)); ymax = Math.max(ymax, ...all.map((p) => p.y));
  if (ymax - ymin < 1e-9) { ymax = ymin + 1; }
  if (c.max == null) ymax = ymax + (ymax - ymin) * 0.08;
  let xmin = Math.min(...all.map((p) => +p.x)), xmax = Math.max(...all.map((p) => +p.x));
  if (xmax - xmin < 3600e3) { xmin -= 12 * 3600e3; xmax += 12 * 3600e3; }
  const X = (d: Date | number): number => L + ((+d - xmin) / (xmax - xmin)) * (W - L - R);
  const Y = (v: number): number => T + (1 - (v - ymin) / (ymax - ymin)) * (H - T - B);
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": c.title });
  for (let i = 0; i <= 3; i++) {
    const v = ymin + ((ymax - ymin) * i) / 3;
    s.append(svg("line", { x1: L, x2: W - R, y1: Y(v), y2: Y(v), stroke: css(i ? "--grid" : "--axis"), "stroke-width": 1 }));
    const tx = svg("text", { x: L - 8, y: Y(v) + 4, "text-anchor": "end" }); tx.textContent = c.fmt(v); s.append(tx);
  }
  if (c.target != null) {
    const ty = svg("text", { x: W - R, y: Y(c.target) - 5, "text-anchor": "end" }); ty.textContent = "design: " + c.fmt(c.target);
    s.append(svg("line", { x1: L, x2: W - R, y1: Y(c.target), y2: Y(c.target), stroke: css("--axis"), "stroke-width": 1 })); s.append(ty);
  }
  const days = [new Date(xmin), new Date(xmax)];
  days.forEach((d, i) => { const tx = svg("text", { x: X(d), y: H - 6, "text-anchor": i ? "end" : "start" }); tx.textContent = d.toLocaleDateString("en-AU", { day: "numeric", month: "short" }); s.append(tx); });
  for (const ser of c.s) {
    if (!ser.pts.length) continue;
    const col = css(TARGETS[ser.tgt].color);
    const pts = ser.pts.slice().sort((a, b) => +a.x - +b.x);
    if (pts.length > 1) s.append(svg("polyline", { points: pts.map((p) => `${X(p.x)},${Y(p.y)}`).join(" "), fill: "none", stroke: col, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    for (const p of pts) {
      const g = svg("g", { tabindex: 0 });
      g.append(svg("circle", { cx: X(p.x), cy: Y(p.y), r: 12, fill: "transparent" }));
      g.append(svg("circle", { cx: X(p.x), cy: Y(p.y), r: 4.5, fill: col, stroke: css("--bg-2"), "stroke-width": 2 }));
      attachTip(g, () => [el("div", { class: "tv" }, c.fmt(p.y)), el("div", { class: "tl" }, `${TARGETS[ser.tgt].name} · ${when(p.x.toISOString())}`)]);
      s.append(g);
    }
  }
  card.append(el("div", { class: "chart" }, s));
  const used = c.s.filter((x) => x.pts.length);
  if (used.length > 1) card.append(el("div", { class: "legend" }, used.map((x) => el("span", null, el("i", { class: "ln-key", style: `background:var(${TARGETS[x.tgt].color})` }), TARGETS[x.tgt].name))));
  const rows = used.flatMap((x) => x.pts.map((p) => [TARGETS[x.tgt].name, when(p.x.toISOString()), c.fmt(p.y)]));
  card.append(el("details", { class: "table-view" }, el("summary", null, "Show as a table"),
    el("table", null, el("thead", null, el("tr", null, el("th", null, "Run"), el("th", null, "When"), el("th", null, "Value"))),
      el("tbody", null, rows.map((r) => el("tr", null, r.map((v) => el("td", null, v))))))));
  return card;
}
let resizeTimer = 0;
addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { renderTrends(); drawLanes(); paint(); reveal(); }, 150);
});
function renderSlowFlaky(): void {
  const tg = (D.targets.live || D.targets.local)!;
  const max = Math.max(...tg.slowest.map((s) => s.duration));
  $("slowest").replaceChildren(...tg.slowest.map((s) => {
    const row = el("div", { class: "hbar", tabindex: 0 }, el("span", { class: "hn" }, el("small", null, s.file), s.name), el("span", { class: "ht" }, el("i", { style: `width:${(100 * s.duration / max).toFixed(1)}%` })), el("span", { class: "hv" }, secs(s.duration)));
    attachTip(row, () => [el("div", { class: "tv" }, secs(s.duration)), el("div", { class: "tl" }, s.name + (s.param ? ` [${s.param}]` : "")), el("div", { class: "tm" }, s.layer)]);
    return row;
  }));
  const fl = $("flaky");
  if (!D.flaky.length) {
    fl.replaceChildren(el("p", { class: "empty-note" }, `None in ${D.runs.length} recorded run${D.runs.length === 1 ? "" : "s"}. The live target shares its world with real players, so its helpers retry when someone else gets to a rock or a rat first, and the fights share one worker. That's where flakiness would come from, and it'll show up here if it does.`));
  } else {
    fl.replaceChildren(el("div", { class: "hbar-rows" }, D.flaky.slice(0, 8).map((f) => el("div", { class: "hbar" }, el("span", { class: "hn" }, el("small", null, f.target), f.id.split("::").pop()), el("span", { class: "mono", style: "letter-spacing:2px" }, f.sequence), el("span", { class: "hv" }, `${f.fails}/${f.runs}`)))));
  }
}

// ---------- AI ----------
function modelName(id: string | undefined): string {
  // "claude-sonnet-5-5" -> "Claude Sonnet 5.5"; anything unexpected is shown as given
  const m = /^claude-([a-z]+)-(\d+)(?:-(\d+))?/.exec(id || "");
  return m ? `Claude ${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}${m[3] && m[3].length < 3 ? "." + m[3] : ""}` : (id || "Claude");
}
function renderAI(): void {
  const box = $("ai-overview");
  box.replaceChildren();
  const a = D.analysis;
  if (!a) { box.append(el("p", { class: "empty-note" }, "The first AI review runs after the next nightly run.")); return; }
  const ch = a.checks || {};
  box.append(el("div", { class: "ai-meta" },
    el("span", null, `Written by ${modelName(a.model)} · ${when(a.generated)}`),
    el("span", { class: "ok" }, glyph(ch.failed ? "known" : "pass"), `${ch.passed ?? 0} of ${ch.total ?? 0} claims checked against the records`),
    a.runs ? el("span", null, `from the latest ${a.runs.length > 1 ? "local and live runs" : "run"}`) : null));
  const o = a.overview || {};
  if (o.headline) box.append(el("div", { class: "ai-headline" }, o.headline));
  box.append(el("div", { class: "ai-body" }, (o.paragraphs || []).map((p) => el("p", null, p))));
  if (o.points && o.points.length) {
    box.append(el("ul", { class: "ai-points" }, o.points.map((p) => el("li", null, glyph(p.kind === "risk" ? "known" : p.kind === "bad" ? "fail" : "pass"), p.text))));
  }
  if (ch.struck && ch.struck.length) {
    box.append(el("details", { class: "table-view" }, el("summary", null, `${ch.struck.length} claim${ch.struck.length > 1 ? "s" : ""} didn't match the records and ${ch.struck.length > 1 ? "were" : "was"} struck`),
      el("ul", null, ch.struck.map((x) => el("li", null, x)))));
  }
  const sw = $("suggestions");
  sw.replaceChildren();
  const sugs = D.suggestions || [];
  if (!sugs.length) { sw.append(el("p", { class: "empty-note" }, "No suggestions yet.")); return; }
  const order: Record<string, number> = { open: 0, accepted: 1, done: 2, rejected: 3 };
  for (const s of sugs.slice().sort((x, y) => (order[x.status] ?? 0) - (order[y.status] ?? 0))) {
    const label = ({ open: "Waiting on me", accepted: "Accepted", done: "Done", rejected: "Rejected" } as Record<string, string>)[s.status] || s.status;
    sw.append(el("article", { class: "card sug reveal" },
      el("div", { class: "tags" }, el("span", { class: "tag" }, s.kind || "idea"), el("span", { class: "decision " + s.status }, label)),
      el("h3", null, s.title),
      el("p", null, s.why),
      s.effort ? el("span", { class: "effort" }, `${s.id} · ${s.effort} effort · proposed ${s.proposed || ""}`) : null,
      s.evidence && s.evidence.length ? el("div", { class: "pins" }, s.evidence.map((e) => el("span", { class: "pin" }, e.split("::").pop()))) : null,
      s.note ? el("p", { class: "note" }, el("b", null, "My call: "), s.note) : null));
  }
}

// ---------- evals ----------
function renderEvals(): void {
  const body = $("eval-body");
  body.replaceChildren();
  const e = D.evals;
  if (!e) { body.append(el("div", { class: "card pad" }, el("p", { class: "empty-note" }, "The first eval run is pending."))); return; }
  const variants = e.variants || [];
  const base = variants.find((v) => v.name === e.baseline) || variants[0];
  const best = variants.find((v) => v.name === e.candidate) || variants[variants.length - 1];
  const card = el("div", { class: "card pad reveal" });
  card.append(el("h3", null, e.title || "Email triage"), el("p", { class: "card-sub" }, e.summary || ""));
  const scores = el("div", { class: "scores" });
  for (const m of e.metrics || []) {
    const now = best.scores[m.key], was = base && base !== best ? base.scores[m.key] : null;
    const d = was == null || now == null ? null : now - was;
    scores.append(el("div", { class: "card score" }, el("div", { class: "label" }, m.label),
      el("div", { class: "value" }, pct(now)),
      was != null ? el("div", { class: "was" },
        d ? el("span", { class: d > 0 ? "delta-up" : "delta-down" }, (d > 0 ? "▲ " : "▼ ") + (Math.abs(d) * 100).toFixed(1) + " pts") : el("span", null, "no change"),
        el("span", null, "from " + pct(was))) : null));
  }
  card.append(scores);
  if (e.legend) card.append(el("p", { class: "meta", style: "margin-top:0.8rem" }, e.legend));
  body.append(card);
  if (e.triage && e.triage.length) {
    const t = el("div", { class: "card pad reveal", style: "margin-top:1rem" }, el("h3", null, "Why the misses missed"),
      el("p", { class: "card-sub" }, "Each miss sorted into a prompt gap (the model wasn't told), a rubric gap (my labels were wrong or unclear) or a product issue (the code around the model)."));
    t.append(el("div", { class: "triage" }, e.triage.map((x) => el("div", { class: "triage-item" }, el("span", { class: "k" }, x.kind), el("span", { class: "v" }, x.text)))));
    body.append(t);
  }
}

// ---------- stack ----------
$("stack").replaceChildren(...["Python 3.13", "pytest", "pytest-xdist", "Hypothesis", "Playwright", "websockets", "GitHub Actions", "Claude (via Claude Code)", "C# .NET 10 server", "Unity WebGL client"].map((s) => el("span", { class: "chip" }, s)));

// ---------- reveal on scroll ----------
function reveal(): void {
  const io: IntersectionObserver | null = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) { e.target.classList.add("in"); io!.unobserve(e.target); }
  }, { rootMargin: "0px 0px -8% 0px" }) : null;
  document.querySelectorAll(".reveal:not(.in)").forEach((n) => (io ? io.observe(n) : n.classList.add("in")));
}

function renderAll(): void {
  renderStatus(); renderCoverage(); renderBugs(); renderTrends(); renderSlowFlaky(); renderAI(); renderEvals();
  replay.byId = Object.fromEntries(replayTests().map((t) => [t.id, t]));
  setupReplay(); paint();
  reveal();
}
renderAll();

// start the replay when it scrolls into view, once
const rp = $("replay");
if ("IntersectionObserver" in window) {
  const io = new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting) && !replay.started) { replay.started = true; start(); io.disconnect(); }
  }, { threshold: 0.35 });
  io.observe(rp);
} else { replay.started = true; start(); }
