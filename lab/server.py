"""Call Lab server - runs scenarios against real Bolna and streams everything to the UI live.

    .venv\\Scripts\\python -m uvicorn lab.server:app --port 7860
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import httpx
from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from doctor.analyze import analyze
from .audio import write_stereo_wav
from .fake_twilio import CallOptions, FakeTwilioCall, Turn
from .scenarios import SCENARIOS, config_diff, write_agents
from . import voices

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
ENGINE_LOG = RUNS / "engine.jsonl"
ENGINE_URL = os.getenv("ENGINE_URL", "http://127.0.0.1:5001")
UI = Path(__file__).resolve().parent / "ui"

app = FastAPI(title="Bolna Call Lab")
app.mount("/static", StaticFiles(directory=UI), name="static")
app.mount("/plan", StaticFiles(directory=ROOT / "docs" / "plan", html=True), name="plan")


class Run:
    def __init__(self, scenario_id: str):
        self.id = time.strftime("%Y%m%d-%H%M%S") + "-" + scenario_id
        self.scenario = SCENARIOS[scenario_id]
        self.dir = RUNS / self.id
        self.events: list[dict] = []
        self.subscribers: list[asyncio.Queue] = []
        self.done = False
        self.report: dict | None = None

    def push(self, ev: dict) -> None:
        self.events.append(ev)
        for q in list(self.subscribers):
            q.put_nowait(ev)


RUNS_LIVE: dict[str, Run] = {}
_busy = asyncio.Lock()


def keys_state() -> dict:
    v = {**dotenv_values(ROOT / ".env"), **{k: os.getenv(k, "") for k in ()}}
    has = lambda k: bool((v.get(k) or "").strip())  # noqa: E731
    return {"deepgram": has("DEEPGRAM_AUTH_TOKEN"), "elevenlabs": has("ELEVENLABS_API_KEY"),
            "llm": has("OPENAI_API_KEY") or has("GROQ_API_KEY"),
            "llm_provider": "openai" if has("OPENAI_API_KEY") else ("groq" if has("GROQ_API_KEY") else None),
            "twilio": has("TWILIO_ACCOUNT_SID"), "bolna": has("BOLNA_API_KEY")}


@app.on_event("startup")
async def _startup():
    write_agents()


@app.post("/api/agents/rebuild")
async def rebuild_agents():
    write_agents()
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def index():
    return (UI / "index.html").read_text(encoding="utf-8")


@app.get("/api/state")
async def state():
    engine = None
    try:
        async with httpx.AsyncClient(timeout=1.5) as c:
            engine = (await c.get(f"{ENGINE_URL}/health")).json()
    except Exception:
        pass
    fixed = SCENARIOS["fixed"].agent
    scen = []
    for s in SCENARIOS.values():
        m = s.meta()
        m["diff_vs_fixed"] = [{"path": p, "before": b, "after": a}
                              for p, b, a in config_diff(s.agent, fixed) if "system_prompt" not in p]
        scen.append(m)
    fx = fixed["agent_config"]["tasks"][0]["tools_config"]
    providers = {"stt": {"elevenlabs": "ElevenLabs Scribe", "deepgram": "Deepgram nova-3"}.get(fx["transcriber"]["provider"], fx["transcriber"]["provider"]),
                 "tts": "ElevenLabs Flash", "llm": fx["llm_agent"]["llm_config"]["model"].split("/")[-1].replace("qwen3.8-27b", "Qwen3 27B (Groq)").replace("gpt-oss-120b", "gpt-oss-120b (Groq)")}
    return {"engine": {"online": engine is not None, "version": "bolna 0.10.283 (ba03a96)"},
            "providers": providers,
            "fixed_rows": fixed_rows(),
            "keys": keys_state(), "scenarios": scen, "busy": _busy.locked(),
            "runs": list_runs()[:30]}


def fixed_rows() -> list[list[str]]:
    """The fixed agent's key settings, read from its real config (shown in the UI)."""
    task = SCENARIOS["fixed"].agent["agent_config"]["tasks"][0]
    tc, tools = task["task_config"], task["tools_config"]
    tr, sy, llm = tools["transcriber"], tools["synthesizer"], tools["llm_agent"]["llm_config"]
    return [["task_config.hangup_after_silence", f"{tc['hangup_after_silence']} s"],
            ["task_config.trigger_user_online_message_after", f"{tc['trigger_user_online_message_after']} s"],
            ["task_config.call_terminate", f"{tc['call_terminate']} s"],
            ["transcriber", f"{tr['provider']} {tr.get('model', '')}, mulaw, 8000 Hz"],
            ["llm", llm["model"].replace("groq/", "groq · ")],
            ["synthesizer", f"{sy['provider']} {sy['provider_config'].get('model', '')}, ulaw_8000"]]


def list_runs() -> list[dict]:
    out = []
    for d in sorted(RUNS.glob("2*-*"), reverse=True):
        rp = d / "report.json"
        if rp.exists():
            r = json.loads(rp.read_text(encoding="utf-8"))
            out.append({"id": d.name, "scenario": (r.get("scenario") or {}).get("id"),
                        "title": (r.get("scenario") or {}).get("title"),
                        "duration": r["call"]["duration"], "ended_by": r["call"]["ended_by"],
                        "diagnosis": r["diagnosis"]["title"], "cause": r["diagnosis"]["cause_id"]})
    return out


@app.post("/api/runs/{scenario_id}")
async def start_run(scenario_id: str, playback: int = 1):
    if scenario_id not in SCENARIOS:
        raise HTTPException(404, "unknown scenario")
    if _busy.locked():
        raise HTTPException(409, "a call is already running")
    run = Run(scenario_id)
    run.playback = bool(playback)
    RUNS_LIVE[run.id] = run
    asyncio.create_task(execute(run))
    return {"run_id": run.id}


async def tail_engine(run: Run, offset: int, t0_holder: dict, stop: asyncio.Event, sink: list) -> None:
    """Follow runs/engine.jsonl from `offset`, push lines (with call-relative time) to the UI."""
    from doctor.analyze import parse_engine
    pos = offset
    while not stop.is_set():
        await asyncio.sleep(0.12)
        try:
            with open(ENGINE_LOG, "r", encoding="utf-8") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
        except FileNotFoundError:
            continue
        for raw in chunk.splitlines():
            try:
                ln = json.loads(raw)
            except json.JSONDecodeError:
                continue
            sink.append(ln)
            t0 = t0_holder.get("t0")
            if t0:
                try:
                    for ev in parse_engine([ln], t0):
                        run.push(ev)
                except Exception as e:                       # never let one odd log line stop a run
                    run.push({"src": "lab", "kind": "parse_warning", "rel": 0, "msg": str(e)})


async def execute(run: Run) -> None:
    async with _busy:
        sc = run.scenario
        run.dir.mkdir(parents=True, exist_ok=True)
        run.push({"src": "lab", "kind": "preparing", "rel": 0, "title": sc.title})
        # render the caller's lines (cached after the first run)
        turns = []
        for t in sc.turns:
            audio = await asyncio.to_thread(voices.render, t.say)
            turns.append(Turn(**{**t.__dict__, "audio": audio}))
        offset = ENGINE_LOG.stat().st_size if ENGINE_LOG.exists() else 0
        t0_holder: dict = {}
        stop = asyncio.Event()
        engine_lines: list[dict] = []
        caller_events: list[dict] = []

        def on_caller(ev: dict) -> None:
            if ev["kind"] == "ws_open":
                t0_holder["t0"] = ev["t"]
            caller_events.append(ev)
            run.push(ev)

        tail = asyncio.create_task(tail_engine(run, offset, t0_holder, stop, engine_lines))
        url = ENGINE_URL.replace("http", "ws") + f"/chat/v1/{sc.id}"
        call = FakeTwilioCall(CallOptions(url=url, turns=turns, hold_media=sc.hold_media,
                                          playback=getattr(run, "playback", True)), on_caller)
        result = await call.run()
        await asyncio.sleep(2.5)                         # let Bolna flush its teardown logs
        stop.set()
        try:
            await tail
        except Exception:
            pass
        # ---- persist
        run.wall_t0 = t0_holder.get("t0")                # wall-clock of the call's first frame (video sync)
        write_stereo_wav(run.dir / "call.wav", result.caller_track, result.agent_track)
        (run.dir / "caller.jsonl").write_text("\n".join(json.dumps(e) for e in caller_events), encoding="utf-8")
        (run.dir / "engine.jsonl").write_text("\n".join(json.dumps(e) for e in engine_lines), encoding="utf-8")
        (run.dir / "agent.json").write_text(json.dumps(sc.agent, indent=2), encoding="utf-8")
        try:
            report = analyze(caller_events, engine_lines, sc.agent, sc.meta())
        except Exception as e:
            import traceback
            traceback.print_exc()
            report = {"scenario": sc.meta(), "error": str(e),
                      "call": {"duration": result.duration, "ended_by": result.ended_by, "hangup_at": result.duration,
                               "hangup_reason": None, "silence_window": None, "last_activity": 0},
                      "lanes": {"caller": [], "agent": [], "events": caller_events}, "milestones": [],
                      "latency": {"turns": []}, "transcript": [], "engine_lines": [],
                      "diagnosis": {"cause_id": "unknown", "title": "Analysis failed", "confidence": "low",
                                    "summary": str(e), "evidence": [], "fix": [], "fix_text": ""}}
        report["run_id"] = run.id
        report["wall_t0"] = run.wall_t0
        (run.dir / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        run.report = report
        run.done = True
        run.push({"src": "lab", "kind": "report", "rel": report["call"]["duration"], "report_ready": True})


@app.get("/api/runs/{run_id}/stream")
async def stream(run_id: str):
    run = RUNS_LIVE.get(run_id)
    if not run:
        raise HTTPException(404)
    q: asyncio.Queue = asyncio.Queue()
    backlog = list(run.events)
    run.subscribers.append(q)

    async def gen():
        try:
            for ev in backlog:
                yield f"data: {json.dumps(ev, default=str)}\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(ev, default=str)}\n\n"
                if ev.get("kind") == "report":
                    break
        finally:
            if q in run.subscribers:
                run.subscribers.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/runs/{run_id}/report")
async def report(run_id: str):
    p = RUNS / run_id / "report.json"
    if not p.exists():
        raise HTTPException(404)
    return json.loads(p.read_text(encoding="utf-8"))


@app.get("/api/runs/{run_id}/audio")
async def audio(run_id: str):
    p = RUNS / run_id / "call.wav"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="audio/wav")
