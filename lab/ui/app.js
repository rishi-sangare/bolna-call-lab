"use strict";
/* Call Lab UI - live call tape, pipeline check, diagnosis. No framework, no build step. */

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtClock = (t) => `${Math.floor(t / 60)}:${(t % 60).toFixed(1).padStart(4, "0")}`;
const fmtS = (t) => (t == null ? "–" : `${t.toFixed(1)} s`);

const MILESTONES = [
  ["stream", "Media stream started", "start event, mulaw 8 kHz"],
  ["inbound", "Caller audio flowing to Bolna", "20 ms frames, continuous"],
  ["welcome", "Welcome audio prepared", "text-to-speech"],
  ["heard", "Caller hears the agent", "agent audio played"],
  ["mark", "Playback confirmed", "Twilio mark echoed back"],
  ["stt", "Caller speech transcribed", "speech-to-text transcript"],
  ["reply", "Agent replies to the caller", "LLM → TTS → caller"],
];

const S = {
  state: null,
  scenarios: {},
  selected: null,
  live: null,          // {runId, t0, model, timer}
  shown: null,         // report currently displayed
};

/* ------------------------------------------------------------------ boot */
async function loadState() {
  const r = await fetch("/api/state");
  S.state = await r.json();
  S.scenarios = Object.fromEntries(S.state.scenarios.map((s) => [s.id, s]));
  renderStatus();
  renderScenarios();
  renderHistory();
  if (!S.selected && S.state.scenarios.length) selectScenario(S.state.scenarios[0].id);
  updateCallButton();
}

function renderStatus() {
  const k = S.state.keys, e = S.state.engine;
  const row = (label, on, val = "") =>
    `<div class="row"><span class="dot ${on ? "on" : "off"}"></span>${esc(label)}<span class="val">${esc(val)}</span></div>`;
  const p = S.state.providers || {};
  $("#status").innerHTML =
    row("Bolna engine", e.online, e.online ? "0.10.283" : "offline") +
    row("Speech-to-text", k.elevenlabs || k.deepgram, p.stt || "add key") +
    row("Voice", k.elevenlabs, p.tts || "add key") +
    row("LLM", k.llm, p.llm || "add key");
}

function renderScenarios() {
  $("#scenarios").innerHTML = S.state.scenarios.map((s) => `
    <li class="scn ${S.selected === s.id ? "sel" : ""}" data-id="${s.id}" tabindex="0">
      <div class="t">${esc(s.title)}<span class="pill ${s.kind}">${s.kind === "fault" ? "Fault" : "Fix"}</span></div>
      <div class="k">${esc(s.tags.join(", "))}</div>
    </li>`).join("");
  document.querySelectorAll(".scn").forEach((el) => {
    el.onclick = () => selectScenario(el.dataset.id);
    el.onkeydown = (e) => { if (e.key === "Enter") selectScenario(el.dataset.id); };
  });
}

function renderHistory() {
  const runs = S.state.runs || [];
  if (!runs.length) { $("#history").innerHTML = `<li class="empty">No calls yet</li>`; return; }
  $("#history").innerHTML = runs.slice(0, 12).map((r) => `
    <li data-id="${r.id}"><span>${esc(r.title || r.scenario)}</span><span class="h-d">${r.duration.toFixed(1)} s</span>
    <span class="h-c">${esc(r.diagnosis)}</span></li>`).join("");
  document.querySelectorAll("#history li[data-id]").forEach((el) => (el.onclick = () => openRun(el.dataset.id)));
}

function selectScenario(id) {
  if (S.live) return;
  S.selected = id;
  S.shown = null;
  const s = S.scenarios[id];
  renderScenarios();
  $("#title").textContent = s.title;
  $("#summary").textContent = s.summary;
  $("#clock").textContent = "0:00.0";
  setCallState("Ready", s.expected);
  $("#tapeNote").textContent = "";
  renderConfig(s);
  renderChain({});
  $("#convo").innerHTML = `<p class="muted">Caller script: ${s.script.map((x) => `“${esc(x)}”`).join(" · ")}</p>`;
  $("#player").hidden = true;
  $("#latencyPanel").hidden = true;
  $("#diag").className = "panel diag";
  $("#diag").innerHTML = `<h3>What went wrong</h3><p class="muted">Expected: ${esc(s.expected)}</p>`;
  drawTape(emptyModel(), { live: false });
  updateCallButton();
}

function renderConfig(s) {
  const rows = s.diff_vs_fixed.filter((d) => !d.path.endsWith("agent_name"));
  $("#configSub").textContent = s.kind === "fixed"
    ? "Recommended production settings for this agent."
    : "What this scenario changes compared with the fixed agent.";
  if (s.kind === "fixed") {
    const fixedRows = S.state.fixed_rows || [];
    $("#config").innerHTML = `<table class="cfg">${fixedRows.map(([p, v]) => `<tr><td class="p">${esc(p)}</td><td class="a">${esc(v)}</td></tr>`).join("")}</table>`;
    return;
  }
  $("#config").innerHTML = rows.length
    ? `<table class="cfg">${rows.map((d) => `<tr><td class="p">${esc(shortPath(d.path))}</td><td class="b">${esc(fmtVal(d.before))}</td><td class="a">${esc(fmtVal(d.after))}</td></tr>`).join("")}</table>`
    : `<p class="muted">Same configuration as the fixed agent. The fault is on the phone line: ${s.hold_media ? "caller audio is never forwarded to Bolna." : "see the scenario description."}</p>`;
}
const shortPath = (p) => p.replace("agent_config.tasks.0.", "").replace("tools_config.", "");
const fmtVal = (v) => (v == null ? "–" : typeof v === "object" ? JSON.stringify(v) : String(v));

function setCallState(main, sub = "", cls = "") {
  $("#callState").className = "call-state " + cls;
  $("#callState").innerHTML = `<b>${esc(main)}</b>${esc(sub)}`;
}

function updateCallButton() {
  const b = $("#placeCall");
  const k = S.state?.keys || {};
  const ready = S.state?.engine?.online && k.deepgram && k.elevenlabs && k.llm;
  b.disabled = !S.selected || !!S.live || !ready;
  b.title = ready ? "" : "Start the Bolna engine and add your API keys to .env";
}

/* ------------------------------------------------------------------ live call */
$("#placeCall").onclick = async () => {
  if (!S.selected || S.live) return;
  const r = await fetch(`/api/runs/${S.selected}`, { method: "POST" });
  if (!r.ok) { setCallState("Could not start", (await r.json()).detail || ""); return; }
  const { run_id } = await r.json();
  const model = emptyModel();
  S.live = { runId: run_id, t0: null, model, done: false };
  $("#placeCall").classList.add("live");
  $("#placeCall").disabled = true;
  $("#placeCall span").textContent = "Calling…";
  setCallState("Dialling", "preparing the caller", "live");
  $("#convo").innerHTML = "";
  $("#diag").className = "panel diag";
  $("#diag").innerHTML = `<h3>What went wrong</h3><p class="muted">Listening to the call…</p>`;
  $("#latencyPanel").hidden = true;
  $("#player").hidden = true;
  renderChain({});
  const es = new EventSource(`/api/runs/${run_id}/stream`);
  es.onmessage = (m) => onEvent(JSON.parse(m.data), es);
  S.live.timer = setInterval(tick, 50);
};

function onEvent(ev, es) {
  const L = S.live;
  if (!L) return;
  const M = L.model;
  if (ev.kind === "report") { es.close(); finishLive(); return; }
  if (ev.src === "caller" && ev.kind === "ws_open") { L.t0 = performance.now() - ev.rel * 1000; setCallState("Connected", "media stream open", "live"); }
  M.events.push(ev);
  const rel = ev.rel ?? 0;
  switch (ev.kind) {
    case "levels": M.levels.push({ rel, c: ev.c, a: ev.a }); break;
    case "caller_speech_start": M.caller.push({ start: rel, end: null, label: ev.text }); bubble("caller", ev.text); setCallState("In call", "caller speaking", "live"); break;
    case "caller_speech_end": { const s = M.caller.at(-1); if (s) s.end = rel; break; }
    case "agent_speech_start": M.agent.push({ start: rel, end: null }); if (M.agent.length === 1 && !M.caller.length) bubble("agent", S.scenarios[S.selected]?.welcome || "Greeting"); setCallState("In call", "agent speaking", "live"); break;
    case "agent_say": if (ev.text) bubble("agent", ev.text); break;
    case "agent_speech_end": { const s = M.agent.at(-1); if (s) s.end = rel; break; }
    case "stt_final": case "stt_interim": if (ev.text) lastAgentReplace(ev); break;
    case "hangup": M.end = { rel, by: ev.by, reason: ev.reason }; setCallState(ev.by === "server" ? "Ended by Bolna" : "Caller hung up", fmtS(rel), ev.by === "server" ? "cut" : ""); break;
    case "call_end": M.duration = ev.duration; break;
  }
  liveChain(M);
}

function lastAgentReplace() { /* engine transcripts are shown in the report view */ }

function tick() {
  const L = S.live;
  if (!L || L.t0 == null) return;
  const now = L.model.end ? L.model.end.rel : (performance.now() - L.t0) / 1000;
  $("#clock").textContent = fmtClock(now);
  drawTape(L.model, { live: !L.model.end, now });
}

async function finishLive() {
  const L = S.live;
  clearInterval(L.timer);
  const rep = await (await fetch(`/api/runs/${L.runId}/report`)).json();
  S.live = null;
  $("#placeCall").classList.remove("live");
  $("#placeCall span").textContent = "Place call";
  await loadState();
  showReport(rep, true);
}

async function openRun(id) {
  if (S.live) return;
  const rep = await (await fetch(`/api/runs/${id}/report`)).json();
  if (rep.scenario && S.scenarios[rep.scenario.id]) {
    S.selected = rep.scenario.id;
    renderScenarios();
    renderConfig(S.scenarios[rep.scenario.id]);
  }
  showReport(rep, false);
}

/* ------------------------------------------------------------------ report view */
function modelFromReport(rep) {
  const M = emptyModel();
  for (const ev of rep.lanes.events) {
    M.events.push(ev);
    if (ev.kind === "levels") M.levels.push({ rel: ev.rel, c: ev.c, a: ev.a });
  }
  M.caller = rep.lanes.caller;
  M.agent = rep.lanes.agent;
  M.duration = rep.call.duration;
  M.end = { rel: rep.call.hangup_at, by: rep.call.ended_by, reason: rep.call.hangup_reason };
  if (rep.call.ended_by === "server" && rep.call.silence_window > 1)
    M.silence = { from: rep.call.last_activity, to: rep.call.decision_at || rep.call.hangup_at, limit: rep.config?.hangup_after_silence };
  return M;
}

function showReport(rep, animate) {
  S.shown = rep;
  const sc = rep.scenario || {};
  $("#title").textContent = sc.title || rep.run_id;
  $("#summary").textContent = sc.summary || "";
  const M = modelFromReport(rep);
  $("#clock").textContent = fmtClock(rep.call.duration);
  const server = rep.call.ended_by === "server";
  setCallState(server ? "Ended by Bolna" : "Caller hung up",
    server ? `after ${fmtS(rep.call.hangup_at)}${rep.call.hangup_reason ? " · " + rep.call.hangup_reason : ""}` : `after ${fmtS(rep.call.duration)}`,
    server ? "cut" : "");
  $("#tapeNote").textContent = rep.run_id || "";
  drawTape(M, { live: false, now: rep.call.duration, reveal: animate });
  renderChain(Object.fromEntries(rep.milestones.map((m) => [m.key, m])), true);
  renderConvo(rep);
  renderDiagnosis(rep, animate);
  renderLatency(rep);
  const p = $("#player");
  p.src = `/api/runs/${rep.run_id}/audio`;
  p.hidden = false;
}

function renderConvo(rep) {
  const items = [];
  rep.lanes.caller.forEach((s) => items.push({ t: s.start, who: "caller", text: s.label }));
  const firstCaller = rep.lanes.caller[0]?.start ?? 1e9;
  const greet = rep.lanes.agent.find((a) => a.start < firstCaller);
  if (greet) items.push({ t: greet.start, who: "agent", text: (rep.scenario || {}).welcome || "Greeting" });
  const heard = (t) => rep.lanes.agent.some((a) => t >= a.start - 1.5 && t <= (a.end ?? a.start) + 0.2);
  (rep.transcript || []).filter((x) => x.who === "agent_text").forEach((x) => items.push({ t: x.t, who: "agent", text: x.text, unheard: !heard(x.t) }));
  items.sort((a, b) => a.t - b.t);
  let html = items.map((x) => `<div class="bub ${x.who}${x.unheard ? " unheard" : ""}"><span class="w">${x.who === "caller" ? "Caller" : "Agent"} · ${fmtS(x.t)}${x.unheard ? " · generated, never reached the caller" : ""}</span>${esc(x.text)}</div>`).join("");
  if (rep.call.ended_by === "server")
    html += `<div class="bub system">Bolna closed the stream at ${fmtS(rep.call.hangup_at)}. On a real phone line Twilio ends the call here.</div>`;
  $("#convo").innerHTML = html || `<p class="muted">Nobody spoke.</p>`;
  $("#convo").scrollTop = 1e6;
}

function bubble(who, text) {
  const el = document.createElement("div");
  el.className = `bub ${who}`;
  const t = S.live?.t0 ? (performance.now() - S.live.t0) / 1000 : 0;
  el.innerHTML = `<span class="w">${who === "caller" ? "Caller" : "Agent"} · ${fmtS(t)}</span>${esc(text)}`;
  $("#convo").appendChild(el);
  $("#convo").scrollTop = 1e6;
}

function renderChain(ms, final = false) {
  let missed = false;
  $("#chain").innerHTML = MILESTONES.map(([key, label, sub]) => {
    const m = ms[key];
    let cls = "", ic = "", tm = "";
    if (m && m.ok) { cls = "ok"; ic = "✓"; tm = m.t != null ? fmtS(m.t) : ""; }
    else if (final && !missed) { cls = "miss"; ic = "✕"; missed = true; tm = "missing"; }
    else if (final && missed) { cls = "after"; ic = "·"; }
    if (m && m.ok && missed) cls += " ";
    return `<li class="${cls}"><span class="ic">${ic}</span><span class="lbl">${esc(label)}<span class="d">${esc(m?.detail || sub)}</span></span><span class="tm">${tm}</span></li>`;
  }).join("");
}

function liveChain(M) {
  const has = (k) => M.events.find((e) => e.kind === k);
  const firstCallerEnd = M.caller[0]?.end;
  const start = has("start_sent");
  const welcome = M.events.find((e) => e.kind === "welcome_ready" && e.has_audio);
  const ms = {
    stream: start && { ok: true, t: start.rel },
    inbound: start && !start.hold_media && { ok: true, t: start.rel },
    welcome: welcome && { ok: true, t: welcome.rel },
    heard: has("agent_speech_start") && { ok: true, t: has("agent_speech_start").rel },
    mark: has("mark_echoed") && { ok: true, t: has("mark_echoed").rel },
    stt: (has("stt_final") || has("stt_interim")) && { ok: true, t: (has("stt_final") || has("stt_interim")).rel },
    reply: firstCallerEnd != null && M.agent.find((a) => a.start > firstCallerEnd) && { ok: true, t: M.agent.find((a) => a.start > firstCallerEnd).start },
  };
  renderChain(ms, false);
}

function renderDiagnosis(rep, animate) {
  const d = rep.diagnosis;
  const good = d.cause_id === "healthy";
  const fix = (d.fix || []).length ? `<h4>Fix</h4><table class="fix">${d.fix.map((f) =>
    `<tr><td class="p">${esc(f.path)}</td><td class="b">${esc(fmtVal(f.before))}</td><td class="a">${esc(fmtVal(f.after))}</td><td class="n">${esc(f.note || "")}</td></tr>`).join("")}</table>` : "";
  $("#diag").className = `panel diag ${good ? "good" : "bad"} ${animate ? "reveal" : ""}`;
  $("#diag").innerHTML = `
    <h3>${good ? "Result" : "What went wrong"}</h3>
    <p class="diag-title">${esc(d.title)}<span class="conf">${esc(d.confidence)} confidence</span></p>
    <p class="diag-sum">${esc(d.summary)}</p>
    <h4>Evidence</h4><ul class="ev-list">${d.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>
    ${fix}
    ${d.fix_text ? `<p class="fix-text">${esc(d.fix_text)}</p>` : ""}`;
}

function renderLatency(rep) {
  const L = rep.latency;
  if (!L || !L.turns.length) { $("#latencyPanel").hidden = true; return; }
  $("#latencyPanel").hidden = false;
  const max = Math.max(1800, ...L.turns.map((t) => t.gap_ms));
  const seg = (ms, cls, label) => ms ? `<span class="${cls}" style="width:${(ms / max) * 100}%" title="${label} ${ms} ms"></span>` : "";
  const rows = L.turns.map((t) => {
    const p = t.parts;
    const bar = p ? seg(p.stt_ms, "p-stt", "Speech-to-text") + seg(p.llm_ms, "p-llm", "LLM") + seg(p.tts_ms, "p-tts", "Voice") + seg(p.play_ms, "p-net", "Playback")
                  : `<span class="p-tts" style="width:${(t.gap_ms / max) * 100}%"></span>`;
    return `<div class="lat-row"><div><div class="lat-cap">“${esc((t.text || "").slice(0, 64))}”</div><div class="lat-stack">${bar}</div></div><div class="v">${t.gap_ms} ms</div></div>`;
  }).join("");
  const pm = L.parts_median;
  const legend = `<div class="lat-legend"><span class="k p-stt"></span>Speech-to-text${pm ? ` ${pm.stt_ms} ms` : ""}<span class="k p-llm"></span>LLM${pm ? ` ${pm.llm_ms} ms` : ""}<span class="k p-tts"></span>Voice${pm ? ` ${pm.tts_ms} ms` : ""}</div>`;
  let note = "";
  if (pm && pm.stt_ms > 0.5 * L.p50)
    note = `<p class="lat-note">${Math.round((pm.stt_ms / L.p50) * 100)}% of each reply is the speech-to-text deciding the caller has finished. The LLM and voice together take ${pm.llm_ms + pm.tts_ms} ms. Faster end-of-speech detection is the lever that brings replies under a second.</p>`;
  $("#latency").innerHTML = legend + rows +
    `<div class="lat-sum"><div>Median reply<br><b>${L.p50} ms</b></div><div>LLM + voice<br><b>${pm ? pm.llm_ms + pm.tts_ms : "–"} ms</b></div><div>Turns<br><b>${L.turns.length}</b></div></div>` + note;
}

/* ------------------------------------------------------------------ the tape */
function emptyModel() { return { levels: [], caller: [], agent: [], events: [], end: null, duration: 0, silence: null }; }

const NS = "http://www.w3.org/2000/svg";
function el(tag, attrs, text) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (text != null) e.textContent = text;
  return e;
}

function drawTape(M, opt) {
  const svg = $("#tape");
  const W = svg.clientWidth || 1200, H = 330;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.replaceChildren();
  const G = 132, R = 18;                               // gutter for lane labels, right pad
  const now = opt.now ?? 0;
  const span = Math.max(14, (M.end ? M.end.rel : now) + (opt.live ? 3 : 1.2));
  const X = (t) => G + (t / span) * (W - G - R);
  const lanes = {
    caller: { y: 34, h: 74, label: "Caller" },
    agent: { y: 116, h: 74, label: "Agent" },
    stt: { y: 202, h: 30, label: "Speech to text" },
    engine: { y: 240, h: 30, label: "Bolna engine" },
    watch: { y: 278, h: 30, label: "Watchdog" },
  };
  const defs = el("defs", {});
  const pat = el("pattern", { id: "hatch", width: 8, height: 8, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" });
  pat.append(el("rect", { width: 8, height: 8, fill: "#FFF3C7" }), el("rect", { width: 3, height: 8, fill: "#F2B705", opacity: .55 }));
  defs.append(pat);
  svg.append(defs);

  // graticule + time ruler
  const step = span > 60 ? 10 : span > 30 ? 5 : span > 16 ? 2 : 1;
  for (let t = 0; t <= span; t += step) {
    svg.append(el("line", { x1: X(t), x2: X(t), y1: 24, y2: H - 8, stroke: "#E4E9EF", "stroke-width": 1 }));
    svg.append(el("text", { x: X(t), y: 15, class: "tick", "text-anchor": "middle" }, `${t}s`));
  }
  for (const [k, ln] of Object.entries(lanes)) {
    svg.append(el("rect", { x: G - 6, y: ln.y, width: W - G - R + 6, height: ln.h, fill: k === "caller" || k === "agent" ? "#F6F8FB" : "#FAFBFC", rx: 6 }));
    svg.append(el("text", { x: 10, y: ln.y + ln.h / 2 + 4, class: "lane-label" }, ln.label));
  }

  // silence window (amber hatch) - known at the end of a call that Bolna ended
  if (M.silence && M.end) {
    const x1 = X(M.silence.from), x2 = X(M.silence.to);
    svg.append(el("rect", { x: x1, y: 26, width: Math.max(2, x2 - x1), height: H - 34, fill: "url(#hatch)", opacity: .75, rx: 3 }));
    const secs = (M.silence.to - M.silence.from).toFixed(1);
    const lim = M.silence.limit ? ` (limit ${M.silence.limit} s)` : "";
    svg.append(el("text", { x: (x1 + x2) / 2, y: lanes.watch.y + 20, class: "sil-label", "text-anchor": "middle" }, `${secs} s of silence${lim} → watchdog hangs up`));
    if (M.silence.to < M.end.rel - 0.2) {
      svg.append(el("rect", { x: x2, y: 26, width: X(M.end.rel) - x2, height: H - 34, fill: "#FCE3E4", opacity: .6 }));
    }
  }

  // waveforms
  const amp = (db) => Math.max(0, Math.min(1, (db + 58) / 46));
  const barW = Math.max(1.2, (X(0.1) - X(0)) - 0.8);
  for (const l of M.levels) {
    if (opt.live && l.rel > now + 0.05) continue;
    const x = X(l.rel - 0.1);
    const ac = amp(l.c), aa = amp(l.a);
    if (ac > 0.02) svg.append(el("rect", { x, width: barW, y: lanes.caller.y + lanes.caller.h / 2 - ac * 33, height: Math.max(1.5, ac * 66), fill: "#2F6BFF", rx: 1 }));
    else svg.append(el("rect", { x, width: barW, y: lanes.caller.y + lanes.caller.h / 2 - .6, height: 1.2, fill: "#BCCBEF" }));
    if (aa > 0.02) svg.append(el("rect", { x, width: barW, y: lanes.agent.y + lanes.agent.h / 2 - aa * 33, height: Math.max(1.5, aa * 66), fill: "#0E9F8E", rx: 1 }));
    else svg.append(el("rect", { x, width: barW, y: lanes.agent.y + lanes.agent.h / 2 - .6, height: 1.2, fill: "#B9E3DD" }));
  }

  // engine + stt + watchdog markers
  const marker = (lane, t, color, label, shape = "dot") => {
    const y = lanes[lane].y + lanes[lane].h / 2;
    if (shape === "diamond") svg.append(el("path", { d: `M${X(t)} ${y - 6} L${X(t) + 6} ${y} L${X(t)} ${y + 6} L${X(t) - 6} ${y}Z`, fill: color }));
    else svg.append(el("circle", { cx: X(t), cy: y, r: 4.5, fill: color }));
    if (label) svg.append(el("text", { x: X(t) + 9, y: y + 4, class: "ev-label" }, label));
  };
  let lastSttLabel = -99, lastEngLabel = -99;
  for (const e of M.events) {
    if (opt.live && e.rel > now + 0.05) continue;
    if (M.end && e.rel > M.end.rel + 0.05) continue;
    const t = e.rel;
    if (e.kind === "stt_commit") {
      marker("stt", t, "#2F6BFF", X(t) - lastSttLabel > 160 ? clip("“" + (e.text || "") + "”", 170) : null);
      if (X(t) - lastSttLabel > 160) lastSttLabel = X(t);
    } else if (e.kind === "llm_first") {
      marker("engine", t, "#14213D", X(t) - lastEngLabel > 90 ? "LLM" : null);
      if (X(t) - lastEngLabel > 90) lastEngLabel = X(t);
    } else if (e.kind === "tts_first" || e.kind === "welcome_sent") {
      marker("engine", t, "#0E9F8E", null);
    } else if (e.kind === "welcome_ready" && !e.has_audio) {
      marker("engine", t, "#E0343A", "no welcome audio", "diamond");
      lastEngLabel = X(t) + 70;
    } else if (e.kind === "welcome_missing") {
      /* same fact as welcome_ready(has_audio=false) - drawn once */
    } else if (["tts_error", "stt_error", "llm_error", "error", "llm_stuck"].includes(e.kind)) {
      marker("engine", t, "#E0343A", X(t) - lastEngLabel > 110 ? shortErr(e) : null, "diamond");
      if (X(t) - lastEngLabel > 110) lastEngLabel = X(t);
    } else if (e.kind === "interruption") {
      marker("engine", t, "#F2B705", "barge-in");
    } else if (e.kind === "mark_echoed") {
      const y = lanes.agent.y + lanes.agent.h - 6;
      svg.append(el("rect", { x: X(t) - 1, y, width: 2, height: 6, fill: "#0B7A6E" }));
    } else if (e.kind === "silence_hangup") {
      marker("watch", t, "#E0343A", null, "diamond");
    } else if (e.kind === "stt_rejected") {
      marker("stt", t, "#E0343A", `rejected: HTTP ${e.http}`, "diamond");
    }
  }

  // playhead / cut
  if (M.end) {
    const x = X(M.end.rel);
    const g = el("g", { class: opt.reveal ? "cut-line" : "" });
    const server = M.end.by === "server";
    g.append(el("line", { x1: x, x2: x, y1: 22, y2: H - 6, stroke: server ? "#E0343A" : "#14213D", "stroke-width": 3 }));
    const label = server ? `Ended by Bolna · ${M.end.rel.toFixed(1)} s` : `Caller hung up · ${M.end.rel.toFixed(1)} s`;
    const right = x > W - 220;
    const tx = right ? x - 10 : x + 10;
    const pw = label.length * 7.4 + 14;
    g.append(el("rect", { x: right ? tx - pw + 7 : tx - 7, y: 30, width: pw, height: 22, rx: 11, fill: "#FFFFFF", stroke: server ? "#F3B4B7" : "#C9D2E0" }));
    g.append(el("text", { x: tx, y: 45, class: "cut-label", "text-anchor": right ? "end" : "start", fill: server ? "#E0343A" : "#14213D" }, label));
    svg.append(g);
  } else if (opt.live) {
    const x = X(now);
    svg.append(el("line", { x1: x, x2: x, y1: 22, y2: H - 6, stroke: "#14213D", "stroke-width": 1.5, "stroke-dasharray": "4 4" }));
  }
}

const clip = (s, w) => { s = s || ""; const n = Math.max(0, Math.floor(w / 6.6)); return s.length > n ? s.slice(0, Math.max(0, n - 1)) + "…" : s; };
const shortErr = (e) => ({ tts_error: "voice error", stt_error: "STT error", llm_error: "LLM error", stt_closed: "STT closed", llm_stuck: "LLM stuck" }[e.kind] || "error");

window.addEventListener("resize", () => {
  if (S.shown) drawTape(modelFromReport(S.shown), { live: false, now: S.shown.call.duration });
  else if (S.live) tick();
  else drawTape(emptyModel(), { live: false });
});
loadState();
setInterval(() => { if (!S.live) loadState(); }, 6000);
