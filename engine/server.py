"""Bolna engine host - runs the real open-source Bolna engine (vendor/bolna) locally.

It does what Bolna's local_setup/quickstart_server.py does, minus Redis and Docker:
  * agents are JSON files in agents/ (exactly Bolna's agent_config + agent_prompts format)
  * the welcome message is pre-synthesised once (as the hosted platform does) and passed to Bolna
  * a Twilio-compatible websocket at /chat/v1/{agent}
  * every Bolna log line is captured with millisecond timestamps to runs/engine.jsonl
    (the Call Doctor reads it, exactly like reading a client's execution logs)

    .venv\\Scripts\\python -m uvicorn engine.server:app --port 5001
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
import traceback
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)            # project keys win over system env
os.chdir(ROOT)                                   # Bolna writes agent_data/ relative to cwd

# Bolna's streaming agent always constructs an OpenAI client for its conversation-completion check, even
# when the conversation LLM is Groq. Without an OpenAI key every call crashes at start. Point that client
# at Groq's OpenAI-compatible endpoint instead.
if not os.getenv("OPENAI_API_KEY") and os.getenv("GROQ_API_KEY"):
    os.environ["OPENAI_API_KEY"] = os.environ["GROQ_API_KEY"]
    os.environ["OPENAI_BASE_URL"] = "https://api.groq.com/openai/v1"
    os.environ.setdefault("CHECK_FOR_COMPLETION_LLM", "openai/gpt-oss-20b")

AGENTS_DIR = ROOT / "agents"
RUNS_DIR = ROOT / "runs"
CACHE_DIR = ROOT / "runs" / "cache"
ENGINE_LOG = RUNS_DIR / "engine.jsonl"
RUNS_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- log capture

class JsonlHandler(logging.Handler):
    """Every log record -> one JSON line {t, level, module, func, msg}."""

    def __init__(self, path: Path):
        super().__init__(logging.DEBUG)          # logging.Handler provides its own re-entrant lock
        self.path = path
        self.fh = open(path, "a", encoding="utf-8", buffering=1)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        if record.exc_info:
            msg += "\n" + "".join(traceback.format_exception(*record.exc_info))[-1500:]
        line = json.dumps({"t": record.created, "level": record.levelname, "logger": record.name,
                           "module": record.module, "func": record.funcName, "msg": msg[:4000]})
        self.fh.write(line + "\n")              # handle() already holds the handler lock


_handler = JsonlHandler(ENGINE_LOG)
logging.getLogger().addHandler(_handler)
logging.getLogger().setLevel(logging.INFO)
for noisy in ("httpx", "httpcore", "websockets", "urllib3", "LiteLLM", "litellm", "openai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("engine")

from bolna.agent_manager.assistant_manager import AssistantManager  # noqa: E402  (after logging setup)
from bolna.llms import litellm as _bolna_litellm  # noqa: E402

_orig_acompletion = _bolna_litellm.acompletion


async def _acompletion(**kw):
    # Bolna has no passthrough for reasoning_effort; gpt-oss defaults to "medium", which adds ~0.3 s per turn
    if "gpt-oss" in str(kw.get("model", "")):
        kw.setdefault("reasoning_effort", "low")
    return await _orig_acompletion(**kw)


_bolna_litellm.acompletion = _acompletion

if os.getenv("LAB_TRACE_LLM") == "1":                        # diagnostic only: log what the LLM stream yields
    _orig_gen = _bolna_litellm.LiteLLM.generate_stream

    async def _traced_generate_stream(self, messages, synthesize=True, meta_info=None, **kw):
        args = {k: v for k, v in self.model_args.items() if k != "messages"}
        logging.getLogger("engine").info(f"LLM_TRACE messages={json.dumps(messages, default=str)[-1500:]}")
        async for ch in _orig_gen(self, messages, synthesize=synthesize, meta_info=meta_info, **kw):
            logging.getLogger("engine").info(f"LLM_TRACE chunk={ch.data!r} eos={ch.end_of_stream} args={args} "
                                             f"finish={(meta_info or {}).get('llm_finish_reason')}")
            yield ch

    _bolna_litellm.LiteLLM.generate_stream = _traced_generate_stream

app = FastAPI(title="Bolna engine host")


def engine_mark(event: str, **data) -> None:
    """Structured marker lines (call start/end) in the same engine log."""
    log.info("ENGINE_EVENT " + json.dumps({"event": event, **data}))


# --------------------------------------------------------------------------- agents

def load_agent(name: str) -> dict:
    path = AGENTS_DIR / f"{name}.json"
    if not path.exists():
        raise HTTPException(404, f"agent '{name}' not found in agents/")
    return json.loads(path.read_text(encoding="utf-8"))


async def welcome_audio(agent_config: dict) -> str | None:
    """Pre-synthesise the welcome message as 8 kHz 16-bit PCM (base64), like the hosted platform.

    Uses the agent's own synthesizer settings, so a broken voice/key breaks the welcome too -
    exactly what happens on the real platform."""
    text = (agent_config.get("agent_welcome_message") or "").strip()
    if not text:
        return None
    synth = agent_config["tasks"][0]["tools_config"].get("synthesizer") or {}
    if synth.get("provider") != "elevenlabs":
        return None
    pc = synth.get("provider_config", {})
    voice_id, model = pc.get("voice_id"), pc.get("model", "eleven_flash_v2_5")
    key = hashlib.sha1(f"{voice_id}|{model}|{text}".encode()).hexdigest()[:16]
    cached = CACHE_DIR / f"welcome_{key}.pcm"
    if cached.exists():
        return base64.b64encode(cached.read_bytes()).decode()
    api_key = os.getenv("ELEVENLABS_API_KEY", "")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=pcm_8000"
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(url, headers={"xi-api-key": api_key},
                             json={"text": text, "model_id": model})
        if r.status_code != 200:
            log.error(f"welcome synthesis failed: HTTP {r.status_code} {r.text[:200]}")
            return None
        cached.write_bytes(r.content)
        return base64.b64encode(r.content).decode()
    except Exception as e:
        log.error(f"welcome synthesis failed: {e}")
        return None


@app.get("/health")
async def health():
    return {"ok": True, "agents": sorted(p.stem for p in AGENTS_DIR.glob("*.json"))}


@app.get("/agents/{name}")
async def get_agent(name: str):
    return load_agent(name)


# --------------------------------------------------------------------------- call websocket

@app.websocket("/chat/v1/{name}")
async def chat(name: str, websocket: WebSocket):
    await websocket.accept()
    t0 = time.time()
    try:
        agent = load_agent(name)
    except HTTPException:
        await websocket.close(code=1008)
        return
    cfg, prompts = agent["agent_config"], agent["agent_prompts"]
    engine_mark("call_accepted", agent=name)
    welcome = await welcome_audio(cfg)
    engine_mark("welcome_ready", agent=name, has_audio=bool(welcome))
    kwargs = {"prompt_responses": prompts}
    if welcome:
        kwargs["welcome_message_audio"] = welcome
    manager = AssistantManager(cfg, websocket, name, **kwargs)
    reason = "completed"
    try:
        async for _task_id, output in manager.run(local=True):
            hang = output.get("hangup_detail") or output.get("hangup_reason") or output.get("ended_by")
            msgs = [{"role": m.get("role"), "content": str(m.get("content", ""))[:600]}
                    for m in (output.get("messages") or []) if m.get("role") in ("assistant", "user")]
            engine_mark("task_output", agent=name, hangup=hang, messages=msgs,
                        summary={k: output.get(k) for k in ("hangup_detail", "ended_by_assistant",
                                                             "conversation_time") if k in output})
    except WebSocketDisconnect:
        reason = "websocket_disconnect"
    except Exception as e:
        reason = f"error: {e}"
        log.error(f"engine error {e}", exc_info=True)
    finally:
        engine_mark("call_finished", agent=name, reason=reason, seconds=round(time.time() - t0, 2))
        try:
            await websocket.close()
        except Exception:
            pass
