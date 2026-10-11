"""Scenarios: one Bolna agent config + one caller script each.

`python -m lab.scenarios` writes agents/<id>.json (Bolna's own agent_config / agent_prompts format).
The "client-default" agent mirrors a typical Bolna setup with platform defaults; each fault changes one
thing; "fixed" is the production configuration we recommend.
"""
from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

from .fake_twilio import Turn

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

AGENT_VOICE = {"voice": "George", "voice_id": "JBFqnCBsd6RMkjVDRZzb", "model": "eleven_flash_v2_5"}

SYSTEM_PROMPT = """You are Riya, a friendly phone support agent for Northwind Home, an online furniture store.
You are on a live phone call, so:
- answer in one or two short sentences, in plain spoken English, no lists, no markdown;
- ask one question at a time;
- if the caller gives an order number, confirm it back digit by digit.
Order 4 8 2 1 7 shipped yesterday and arrives Thursday between 9am and 1pm. Delivery can be moved to
Friday or Saturday. Returns are free within 30 days.
You have no tools or functions. Never call a function or tool; answer directly from the facts above."""


def llm_config() -> dict:
    env = dotenv_values(ROOT / ".env")                     # the project's .env wins over system variables
    if (env.get("GROQ_API_KEY") or "").strip() and not (env.get("OPENAI_API_KEY") or "").strip():
        # gpt-oss-120b on Groq with low reasoning (injected by engine/server.py): ~0.3 s first token, complete
        # sentences. Qwen3 on Groq emits end-of-turn a word early ("No problem, take your"); gpt-oss-20b invented
        # tool calls, which Bolna correctly treats as llm_error and hangs up.
        return {"provider": "groq", "model": "groq/openai/gpt-oss-120b", "max_tokens": 400, "temperature": 0.3,
                "agent_flow_type": "streaming"}
    return {"provider": "openai", "model": "gpt-4o-mini", "max_tokens": 120, "temperature": 0.3,
            "agent_flow_type": "streaming"}


DEEPGRAM_STT = {"provider": "deepgram", "model": "nova-3", "language": "en", "stream": True,
                "encoding": "mulaw", "sampling_rate": 8000, "endpointing": 300}
ELEVENLABS_STT = {"provider": "elevenlabs", "model": "scribe_v2_realtime", "language": "en", "stream": True,
                  "encoding": "mulaw", "sampling_rate": 8000, "endpointing": 300,
                  "min_silence_duration_ms": 150, "vad_threshold": 0.4}


def stt_config() -> dict:
    """Working speech-to-text for the healthy agents (STT_PROVIDER in .env: deepgram | elevenlabs)."""
    env = dotenv_values(ROOT / ".env")
    return dict(ELEVENLABS_STT if (env.get("STT_PROVIDER") or "deepgram").strip() == "elevenlabs" else DEEPGRAM_STT)


def base_agent() -> dict:
    return {
        "agent_config": {
            "agent_name": "Riya - Northwind support",
            "agent_type": "other",
            "agent_welcome_message": "Hi, this is Riya from Northwind Home. How can I help you today?",
            "tasks": [{
                "task_type": "conversation",
                "toolchain": {"execution": "parallel", "pipelines": [["transcriber", "llm", "synthesizer"]]},
                "tools_config": {
                    "input": {"provider": "twilio", "format": "wav"},
                    "output": {"provider": "twilio", "format": "wav"},
                    "transcriber": stt_config(),
                    "llm_agent": {"agent_type": "simple_llm_agent", "agent_flow_type": "streaming",
                                  "llm_config": llm_config()},
                    "synthesizer": {"provider": "elevenlabs", "provider_config": dict(AGENT_VOICE), "stream": True,
                                    "buffer_size": 100, "audio_format": "wav"},
                    "api_tools": None,
                },
                # Bolna platform defaults (docs): silence hangup 10 s, online check 10 s, cap 90 s
                "task_config": {"hangup_after_silence": 10, "call_terminate": 90, "check_if_user_online": True,
                                "trigger_user_online_message_after": 10,
                                "check_user_online_message": "Are you still there?",
                                "incremental_delay": 100, "number_of_words_for_interruption": 2,
                                "optimize_latency": True, "backchanneling": False, "ambient_noise": False,
                                "hangup_after_LLMCall": False},
            }],
        },
        "agent_prompts": {"task_1": {"system_prompt": SYSTEM_PROMPT}},
    }


def _tc(a: dict) -> dict:
    return a["agent_config"]["tasks"][0]


@dataclass
class Scenario:
    id: str
    title: str
    kind: str                       # "fault" | "fixed"
    summary: str
    expected: str
    agent: dict
    turns: list[Turn]
    hold_media: bool = False
    tags: list[str] = field(default_factory=list)

    def meta(self) -> dict:
        return {"id": self.id, "title": self.title, "kind": self.kind, "summary": self.summary,
                "expected": self.expected, "tags": self.tags, "hold_media": self.hold_media,
                "welcome": self.agent["agent_config"].get("agent_welcome_message", ""),
                "script": [t.say for t in self.turns]}


def build() -> list[Scenario]:
    S: list[Scenario] = []

    # ---- 1. TTS broken: invalid / foreign voice id (also what an out-of-credit TTS account looks like)
    a = base_agent()
    _tc(a)["tools_config"]["synthesizer"]["provider_config"]["voice_id"] = "VoiceIdFromAnotherAccount00"
    S.append(Scenario(
        "tts-broken", "Agent voice never plays", "fault",
        "The ElevenLabs voice can't be used (wrong voice ID, deleted voice, or no credits). "
        "Nothing reaches the caller, Bolna never gets a playback mark, and the silence watchdog ends the call.",
        "Dead air, then hangup ~10–12 s · synthesizer_error / inactivity_timeout", a,
        [Turn("Hello?", wait="agent_done", timeout=1.5, pause=0.2)],
        tags=["TTS", "ElevenLabs"]))

    # ---- 2. STT provider rejects the stream (recorded for real: Deepgram 402, project out of credits)
    a = base_agent()
    _tc(a)["tools_config"]["transcriber"] = dict(DEEPGRAM_STT)
    S.append(Scenario(
        "stt-rejected", "Speech-to-text account rejected", "fault",
        "Deepgram refuses the live transcription socket (HTTP 402: the project has no credits; 401 for a bad key). "
        "The greeting still plays, so it sounds fine at first, then Bolna ends the call as a transcriber error.",
        "Greeting plays, caller speaks, call ends within seconds · transcriber_connection_error", a,
        [Turn("Hi, yes, I'm calling about my order.", wait="agent_done", pause=0.6),
         Turn("Hello? Can you hear me?", wait="seconds", seconds=3.5, pause=0.2)],
        tags=["STT", "Deepgram", "Billing"]))

    # ---- 3. Dead inbound audio: the stream starts but caller audio never reaches Bolna
    a = base_agent()
    S.append(Scenario(
        "inbound-dead", "Caller audio never arrives", "fault",
        "The media stream starts but no inbound audio is forwarded (wrong <Stream track>, a proxy "
        "dropping frames, or a muted leg). Deepgram gets nothing and the agent can't hear the caller.",
        "Greeting plays, agent never answers, hangup ~10–12 s · inactivity_timeout / transcriber_connection_error",
        a,
        [Turn("Hi, yes, I'm calling about my order.", wait="agent_done", pause=0.6),
         Turn("Hello? Can you hear me?", wait="seconds", seconds=3.5, pause=0.2)],
        hold_media=True, tags=["Telephony", "STT", "Deepgram"]))

    # ---- 4. Silence watchdog too tight: a healthy call dies while the caller looks something up
    a = base_agent()
    S.append(Scenario(
        "silence-tight", "Healthy call, caller pauses", "fault",
        "Everything works, but hangup_after_silence is 10 s and the 'are you still there' check is also "
        "10 s, so the hangup check wins and the reminder never plays. A caller who pauses to look for an "
        "order number is cut off.",
        "Normal conversation, caller goes quiet, hangup ~10–12 s later · inactivity_timeout", a,
        [Turn("Hi, I'm calling about a delivery.", wait="agent_done", pause=0.6),
         Turn("Sure, give me a second, let me find the order number.", wait="agent_done", pause=0.5),
         Turn("Okay, found it. It's four eight two one seven.", wait="agent_done", pause=14.0, timeout=8.0)],
        tags=["Config", "Silence"]))

    # ---- 5. Fixed: production configuration
    a = base_agent()
    tc = _tc(a)["task_config"]
    tc.update({"hangup_after_silence": 25, "trigger_user_online_message_after": 8, "call_terminate": 600,
               "check_user_online_message": "Are you still there? Take your time."})
    S.append(Scenario(
        "fixed", "Fixed configuration", "fixed",
        "Valid voice and keys, inbound audio flowing, silence hangup 25 s with a gentle check-in at 8 s, "
        "10-minute cap. The same caller now pauses, gets a check-in, and finishes the call.",
        "Full conversation, check-in during the pause, caller hangs up · no early disconnect", a,
        [Turn("Hi, I'm calling about a delivery.", wait="agent_done", pause=0.6),
         Turn("Sure, give me a second, let me find the order number.", wait="agent_done", pause=0.5),
         Turn("Okay, found it. It's four eight two one seven.", wait="agent_done", pause=14.0, timeout=8.0),
         Turn("Can I move the delivery to Saturday instead?", wait="agent_done", pause=0.6),
         Turn("Perfect, thank you so much. Bye!", wait="agent_done", pause=0.6)],
        tags=["Fix", "Latency"]))
    return S


SCENARIOS = {s.id: s for s in build()}


def write_agents() -> None:
    out = ROOT / "agents"
    out.mkdir(exist_ok=True)
    for s in SCENARIOS.values():
        (out / f"{s.id}.json").write_text(json.dumps(s.agent, indent=2), encoding="utf-8")
        print("agents/" + s.id + ".json")


def config_diff(a: dict, b: dict, path: str = "") -> list[tuple[str, object, object]]:
    """Flat list of (path, before, after) where two agent configs differ."""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            out += config_diff(a.get(k), b.get(k), f"{path}.{k}" if path else k)
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            out += config_diff(x, y, f"{path}.{i}")
    elif a != b:
        out.append((path, copy.deepcopy(a), copy.deepcopy(b)))
    return out


if __name__ == "__main__":
    write_agents()
