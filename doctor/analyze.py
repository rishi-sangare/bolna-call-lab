"""Call Doctor: turn a call's evidence into a timeline, a milestone check and a root-cause diagnosis.

Evidence:
  * caller events   - what the phone side saw (stream start, audio sent/heard, marks, who closed the socket)
  * engine log      - Bolna's own log lines for the call window (timestamps to the millisecond)
  * agent config    - the Bolna agent JSON the call ran with

The same function works for a client's call: feed it Bolna's raw execution logs instead of the local engine
log (see doctor/adapters.py).
"""
from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- engine log -> events

PATTERNS: list[tuple[str, str, re.Pattern]] = [
    ("stt", "stt_rejected", re.compile(r"(?:Deepgram|ElevenLabs|transcri).*rejected WebSocket connection: HTTP (\d+)", re.I)),
    # (lane, kind, pattern)
    ("watchdog", "silence_hangup", re.compile(
        r"([\d.]+|inf) seconds since AI last spoke and ([\d.]+|inf) seconds since user last spoke, both exceed (\d+)s")),
    ("watchdog", "max_duration", re.compile(r"call_terminate cap")),
    ("llm", "llm_first", re.compile(r"Got a response from LLM\s+(.*)", re.S)),
    ("tts", "tts_first", re.compile(r"WS recv FIRST")),
    ("tts", "agent_say", re.compile(r"Writing response to log (.*)", re.S)),
    ("stt", "stt_commit", re.compile(r"Committed transcript: (.*)")),
    ("watchdog", "stream_sid_timeout", re.compile(r"Timeout reached while waiting for stream_sid")),
    ("llm", "llm_stuck", re.compile(r"LLM generation stuck")),
    ("stt", "stt_closed", re.compile(r"active transcriber closed, ending call|Transcriber connection has been closed")),
    ("stt", "stt_final", re.compile(r"Received transcript, sending for further processing|is_final set as True - (.*)")),
    ("stt", "stt_interim", re.compile(r"interim (?:result|transcript)s? ?[:\-] ?(.+)", re.I)),
    ("stt", "stt_connected", re.compile(r"(?:Connected to Deepgram|deepgram.*connect(?:ed|ion established))", re.I)),
    ("tts", "welcome_missing", re.compile(r"No welcome message audio to send")),
    ("tts", "welcome_sent", re.compile(r"Got stream sid and hence sending the first message")),
    ("tts", "tts_first_chunk", re.compile(r"Sending first chunk")),
    ("tts", "tts_error", re.compile(r"(?:elevenlabs|synthesi[sz]er).*(?:error|failed|invalid|401|404|quota|voice_id)", re.I)),
    ("llm", "llm_error", re.compile(r"(?:litellm|openai|groq|llm).*(?:error|exception|invalid|rate limit|401|404)", re.I)),
    ("stt", "stt_error", re.compile(r"deepgram.*(?:error|1011|NET-000|closed)", re.I)),
    ("telephony", "interruption", re.compile(r"interrupting because user spoke")),
    ("telephony", "stop_handler", re.compile(r"stopping handler")),
]

HANGUP_REASONS = ["inactivity_timeout", "synthesizer_error", "transcriber_connection_error", "transcriber_error",
                  "llm_error", "max_duration_reached", "llm_prompted_hangup", "end_call_tool", "voicemail_detected",
                  "client_disconnected"]


def parse_engine(lines: list[dict], t0: float) -> list[dict]:
    out: list[dict] = []
    for ln in lines:
        msg = ln.get("msg", "")
        rel = round(ln["t"] - t0, 3)
        if msg.startswith("ENGINE_EVENT "):
            try:
                ev = json.loads(msg[len("ENGINE_EVENT "):])
            except json.JSONDecodeError:
                continue
            out.append({"src": "engine", "lane": "engine", "kind": ev.pop("event"), "rel": rel, **ev})
            continue
        matched = False
        for lane, kind, rx in PATTERNS:
            m = rx.search(msg)
            if m:
                ev = {"src": "engine", "lane": lane, "kind": kind, "rel": rel, "msg": msg[:300]}
                if kind == "silence_hangup":
                    ai, user, thr = m.group(1), m.group(2), int(m.group(3))
                    ev["ai_silent"] = None if ai == "inf" else round(float(ai), 2)
                    ev["user_silent"] = None if user == "inf" else round(float(user), 2)
                    ev["threshold"] = thr
                    quiet = min(x for x in (ev["ai_silent"], ev["user_silent"], 9e9) if x is not None)
                    ev["silent_since"] = round(rel - quiet, 3) if quiet < 9e9 else 0.0
                if kind == "stt_rejected":
                    ev["http"] = int(m.group(1))
                if kind in ("llm_first", "stt_commit", "agent_say") and m.lastindex:
                    ev["text"] = m.group(1).strip()[:300]
                if kind in ("stt_final", "stt_interim") and m.lastindex:
                    ev["text"] = (m.group(m.lastindex) or "")[:200]
                out.append(ev)
                matched = True
                break
        if not matched and ln.get("level") in ("ERROR", "CRITICAL"):
            out.append({"src": "engine", "lane": "errors", "kind": "error", "rel": rel, "msg": msg[:400]})
        for reason in HANGUP_REASONS:
            if reason in msg and ("hangup" in msg.lower() or "HangupReason" in msg or "hangup_detail" in msg):
                out.append({"src": "engine", "lane": "watchdog", "kind": "hangup_reason", "reason": reason,
                            "rel": rel, "msg": msg[:300]})
                break
    return out


# --------------------------------------------------------------------------- report


@dataclass
class Diagnosis:
    cause_id: str
    title: str
    confidence: str
    summary: str
    evidence: list[str] = field(default_factory=list)
    fix: list[dict] = field(default_factory=list)       # {path, before, after, note}
    fix_text: str = ""


def _first(events, kind, src=None):
    for e in events:
        if e["kind"] == kind and (src is None or e["src"] == src):
            return e
    return None


def _all(events, kind, src=None):
    return [e for e in events if e["kind"] == kind and (src is None or e["src"] == src)]


def _segments(events, start_kind, end_kind, label_key=None):
    segs, cur = [], None
    for e in events:
        if e["kind"] == start_kind:
            cur = {"start": e["rel"], "label": e.get(label_key, "") if label_key else ""}
        elif e["kind"] == end_kind and cur is not None:
            cur["end"] = e["rel"]
            cur["interrupted"] = e.get("interrupted", False)
            segs.append(cur)
            cur = None
    if cur is not None:
        cur["end"] = None
        segs.append(cur)
    return segs


def analyze(caller: list[dict], engine_lines: list[dict], agent: dict, scenario: dict | None = None) -> dict:
    t0 = caller[0]["t"] - caller[0]["rel"] if caller else (engine_lines[0]["t"] if engine_lines else 0)
    eng = parse_engine(engine_lines, t0)
    events = sorted(caller + eng, key=lambda e: e["rel"])
    task = agent["agent_config"]["tasks"][0]
    tc = task.get("task_config", {})
    synth = task["tools_config"].get("synthesizer", {})
    silence_cfg = tc.get("hangup_after_silence", 10)
    online_cfg = tc.get("trigger_user_online_message_after", 10)

    end = _first(caller, "call_end")
    hang = _first(caller, "hangup")
    duration = end["duration"] if end else (events[-1]["rel"] if events else 0)
    ended_by = (end or {}).get("ended_by", "")
    start = _first(caller, "start_sent")
    hold_media = bool(start and start.get("hold_media"))

    agent_segs = _segments(caller, "agent_speech_start", "agent_speech_end")
    caller_segs = _segments(caller, "caller_speech_start", "caller_speech_end", "text")
    for s in agent_segs:
        if s["end"] is None:
            s["end"] = duration
    for s in caller_segs:
        if s["end"] is None:
            s["end"] = duration
    marks = _all(caller, "mark_echoed")
    stt_finals = _all(eng, "stt_final")
    stt_any = stt_finals + _all(eng, "stt_interim")
    welcome = _first(eng, "welcome_ready")
    silence_hang = _first(eng, "silence_hangup")
    reason_ev = next((e for e in eng if e["kind"] == "hangup_reason"), None)
    task_out = _first(eng, "task_output")
    hangup_reason = (reason_ev or {}).get("reason") or (task_out or {}).get("hangup")
    if not hangup_reason and silence_hang:
        hangup_reason = "inactivity_timeout"
    stt_rej = _first(eng, "stt_rejected")
    tts_err = _first(eng, "tts_error")
    stt_err = _first(eng, "stt_error") or _first(eng, "stt_closed")
    llm_err = _first(eng, "llm_error") or _first(eng, "llm_stuck")

    # ---- milestones (the chain that must happen for a call to work)
    def ms(key, label, ev, detail=""):
        return {"key": key, "label": label, "t": ev["rel"] if ev else None, "ok": ev is not None, "detail": detail}

    first_agent_audio = _first(caller, "agent_speech_start")
    milestones = [
        ms("stream", "Media stream started", start, "start event · mulaw 8 kHz"),
        ms("inbound", "Caller audio flowing to Bolna", None if hold_media else start,
           "0 media frames sent" if hold_media else "20 ms frames, continuous"),
        ms("welcome", "Welcome audio prepared", welcome if welcome and welcome.get("has_audio") else None,
           "TTS pre-synthesis"),
        ms("heard", "Caller hears the agent", first_agent_audio, "agent media played"),
        ms("mark", "Playback confirmed (mark)", marks[0] if marks else None, "Twilio mark echo"),
        ms("stt", "Caller speech transcribed", (stt_any or [None])[0] if stt_any else None, "speech-to-text transcript"),
        ms("reply", "Agent replies to the caller",
           next((a_ev for a_ev in _all(caller, "agent_speech_start") if caller_segs and a_ev["rel"] > caller_segs[0]["end"]), None),
           "LLM → TTS → caller"),
    ]
    first_missing = next((m for m in milestones if not m["ok"]), None)

    # ---- latency per turn (caller stops talking -> agent audio starts), measured on the phone side
    turns = []
    for cs in caller_segs:
        nxt = next((a for a in agent_segs if a["start"] > cs["end"] - 0.05), None)
        if nxt and nxt["start"] - cs["end"] < 8:
            turns.append({"caller_end": cs["end"], "agent_start": nxt["start"], "gap_ms": round((nxt["start"] - cs["end"]) * 1000),
                          "text": cs["label"]})
    # break each turn down with Bolna's own trace timestamps
    commits = [e for e in eng if e["kind"] in ("stt_commit", "stt_final")]
    llm_firsts = [e for e in eng if e["kind"] == "llm_first"]
    tts_firsts = [e for e in eng if e["kind"] == "tts_first"]
    for t in turns:
        ce, ast = t["caller_end"], t["agent_start"]
        stt = next((e["rel"] for e in commits if ce - 0.3 <= e["rel"] <= ast), None)
        llm = next((e["rel"] for e in llm_firsts if stt is not None and stt <= e["rel"] <= ast), None)
        tts = next((e["rel"] for e in tts_firsts if llm is not None and llm <= e["rel"] <= ast + 0.05), None)
        if stt is not None and llm is not None and tts is not None:
            t["parts"] = {"stt_ms": round(max(0, stt - ce) * 1000), "llm_ms": round((llm - stt) * 1000),
                          "tts_ms": round((tts - llm) * 1000), "play_ms": round(max(0, ast - tts) * 1000)}
    gaps = [t["gap_ms"] for t in turns]
    parts = [t["parts"] for t in turns if "parts" in t]
    med = lambda k: round(statistics.median([p[k] for p in parts])) if parts else None  # noqa: E731
    latency = {"turns": turns, "parts_median": {k: med(k) for k in ("stt_ms", "llm_ms", "tts_ms", "play_ms")} if parts else None,
               "p50": round(statistics.median(gaps)) if gaps else None,
               "p95": round(sorted(gaps)[max(0, int(len(gaps) * 0.95) - 1)]) if gaps else None}

    # ---- silence window: straight from Bolna's watchdog line when present
    last_activity = 0.0
    decision_at = None
    if silence_hang:
        last_activity = silence_hang.get("silent_since", 0.0)
        decision_at = silence_hang["rel"]
    else:
        for s in agent_segs + caller_segs:
            if s["end"] and s["end"] <= duration:
                last_activity = max(last_activity, s["end"])
    silence_window = round((decision_at or duration) - last_activity, 2) if ended_by == "server" else None

    diag = diagnose(ended_by=ended_by, duration=duration, hangup_reason=hangup_reason, hold_media=hold_media,
                    agent_segs=agent_segs, caller_segs=caller_segs, marks=marks, stt_any=stt_any,
                    welcome=welcome, tts_err=tts_err, stt_err=stt_err, llm_err=llm_err, silence_cfg=silence_cfg,
                    online_cfg=online_cfg, synth=synth, silence_window=silence_window, last_activity=last_activity,
                    first_missing=first_missing, silence_hang=silence_hang, stt_rej=stt_rej,
                    stt_provider=task["tools_config"].get("transcriber", {}).get("provider", "deepgram"))

    transcript = []
    for s in caller_segs:
        transcript.append({"who": "caller", "t": s["start"], "text": s["label"]})
    # agent lines: Bolna logs the first chunk of each reply; the full text comes from the final history
    final_msgs = [m["content"] for m in ((task_out or {}).get("messages") or []) if m.get("role") == "assistant"]
    used: set[int] = set()
    last_full = ""
    for e in _all(eng, "agent_say"):
        first = (e.get("text") or "").strip()
        if not first:
            continue
        full = None
        for i, m in enumerate(final_msgs):
            if i not in used and m.strip().startswith(first[:20]):
                full, _ = m.strip(), used.add(i)
                break
        if full is None and first in last_full:
            continue                      # a later TTS chunk of the reply already shown in full
        full = re.sub(r"([.!?])(?=[A-Z])", r"\1 ", full or first)   # Bolna joins back-to-back replies without a space
        last_full = full
        transcript.append({"who": "agent_text", "t": e["rel"], "text": full})
    transcript.sort(key=lambda x: x["t"])

    return {
        "scenario": scenario,
        "call": {"duration": duration, "ended_by": ended_by, "hangup_at": hang["rel"] if hang else duration,
                 "decision_at": decision_at,
                 "watchdog": {k: silence_hang.get(k) for k in ("ai_silent", "user_silent", "threshold")} if silence_hang else None,
                 "close_code": (end or {}).get("close_code"), "hangup_reason": hangup_reason,
                 "silence_window": silence_window, "last_activity": round(last_activity, 2)},
        "config": {"hangup_after_silence": silence_cfg, "trigger_user_online_message_after": online_cfg,
                   "call_terminate": tc.get("call_terminate"), "voice_id": synth.get("provider_config", {}).get("voice_id"),
                   "transcriber": task["tools_config"].get("transcriber", {}),
                   "llm": task["tools_config"]["llm_agent"]["llm_config"].get("model")},
        "lanes": {"caller": caller_segs, "agent": agent_segs,
                  "events": [e for e in events if e["kind"] not in ("mark_received",)]},
        "milestones": milestones,
        "latency": latency,
        "transcript": transcript,
        "diagnosis": diag.__dict__,
        "engine_lines": [{"rel": round(l["t"] - t0, 3), "level": l["level"], "msg": l["msg"][:300]}
                         for l in engine_lines if l["level"] in ("WARNING", "ERROR") or "hang" in l["msg"].lower()][-40:],
    }


def diagnose(*, ended_by, duration, hangup_reason, hold_media, agent_segs, caller_segs, marks, stt_any, welcome,
             tts_err, stt_err, llm_err, silence_cfg, online_cfg, synth, silence_window, last_activity, first_missing,
             silence_hang, stt_rej=None, stt_provider="deepgram") -> Diagnosis:
    at = f"{duration:.1f} s"
    if ended_by in ("caller", "timeout") and duration > 20:
        return Diagnosis("healthy", "No early disconnect", "high",
                         f"The call ran {at} and was ended by the caller after the conversation finished. "
                         "Every stage of the pipeline produced its events.",
                         [f"Agent spoke {len(agent_segs)} times, caller {len(caller_segs)} times",
                          f"{len(marks)} playback marks confirmed", f"{len(stt_any)} transcript events"])
    fix_silence = [{"path": "task_config.hangup_after_silence", "before": silence_cfg, "after": 25,
                    "note": "allow natural pauses"},
                   {"path": "task_config.trigger_user_online_message_after", "before": online_cfg, "after": 8,
                    "note": "must be lower than the hangup, or it never plays"}]

    # 0. speech-to-text provider refused the connection
    if stt_rej:
        code = stt_rej.get("http")
        meaning = {402: "the account has no credits left (Payment Required)", 401: "the API key is invalid",
                   403: "the key lacks permission for live transcription", 429: "the account hit its rate limit"}.get(
            code, f"it returned HTTP {code}")
        name = stt_provider.capitalize()
        return Diagnosis(
            "stt_rejected", f"{name} refused the speech-to-text stream", "high",
            f"{name} rejected the live transcription socket at {stt_rej['rel']:.1f} s because {meaning}. The greeting "
            f"still played, so the call sounded normal, then Bolna ended it at {at} as {hangup_reason or 'a transcriber error'}.",
            [f"{stt_rej['rel']:.1f} s: {stt_rej['msg'][:150]}",
             f"Bolna hang-up reason: {hangup_reason or 'transcriber_connection_error'}",
             "Agent audio played and playback was confirmed, so telephony and TTS are fine",
             f"The caller spoke {len(caller_segs)} time(s); nothing could be transcribed"],
            [{"path": f"{name} account", "before": f"HTTP {code}", "after": "credits / valid key",
              "note": "top up or enable overage; check the key's project"},
             {"path": "monitoring", "before": "none", "after": "alert on transcriber_connection_error",
              "note": "catch this before customers do"}],
            f"Restore the {name} account (credits or a valid key), then alert on transcriber_connection_error in "
            "Bolna's execution data so a billing lapse never silently drops calls again.")

    # 1. agent audio never reached the caller (Bolna may still send marks for turns that produced no audio)
    if not agent_segs:
        ev = [f"Server closed the stream at {at} (hangup_reason: {hangup_reason or 'n/a'})",
              "The caller never heard a single frame of agent audio",
              f"Nothing was said for {silence_cfg}+ s, so Bolna's 2-second watchdog hung up"]
        if silence_hang:
            us = silence_hang.get('user_silent')
            ev.append(f"Bolna log: AI silent for {silence_hang.get('ai_silent')} s, caller "
                      + (f"silent for {us} s" if us is not None else "never registered as speaking")
                      + f" (limit {silence_cfg} s)")
        if welcome and not welcome.get("has_audio"):
            ev.insert(1, "Welcome message could not be synthesised")
        if tts_err:
            ev.insert(1, f"TTS error at {tts_err['rel']:.1f} s: {tts_err['msg'][:140]}")
        return Diagnosis(
            "tts", "Agent voice never reached the caller", "high",
            "The text-to-speech stage failed, so the caller heard dead air. The call looked silent to Bolna and "
            f"its inactivity watchdog ended it at {at}.",
            ev,
            [{"path": "synthesizer.provider_config.voice_id", "before": synth.get("provider_config", {}).get("voice_id"),
              "after": "JBFqnCBsd6RMkjVDRZzb", "note": "a voice that exists in this ElevenLabs account"},
             {"path": "ElevenLabs account", "before": "?", "after": "credits + TTS permission on the key",
              "note": "quota/permission errors look identical"}],
            "Use a voice ID from the same ElevenLabs account as the API key, confirm credits and the key's Text "
            "to Speech permission, and keep output at ulaw_8000 for Twilio.")

    # 2. caller audio never reached STT
    if hold_media or (caller_segs and not stt_any):
        ev = [f"Server closed the stream at {at} (hangup_reason: {hangup_reason or 'n/a'})",
              f"The caller spoke {len(caller_segs)} time(s) but no transcript was produced",
              "Agent audio played and marks were confirmed - the outbound path is healthy"]
        if hold_media:
            ev.insert(1, "0 inbound media frames reached Bolna after the start event")
        if stt_err:
            ev.append(f"STT at {stt_err['rel']:.1f} s: {stt_err['msg'][:140]}")
        return Diagnosis(
            "inbound", "The agent can't hear the caller", "high",
            "Caller audio is not reaching speech-to-text. Bolna never sees the user speak, so after the greeting "
            f"both silence clocks run out and the call ends at {at}.",
            ev,
            [{"path": "Twilio <Stream>", "before": "track / forwarding broken", "after": "<Connect><Stream url=wss://…> (inbound track)",
              "note": "no query string in the URL; use <Parameter>"},
             {"path": "transcriber", "before": "?", "after": "deepgram · mulaw · 8000 Hz", "note": "match the phone audio"}],
            "Check that the media stream carries the caller's inbound track to Bolna (Twilio Stream track and any "
            "proxy in between), and that the transcriber decodes mulaw 8 kHz.")

    # 3. healthy pipeline, silence watchdog too tight
    if ended_by == "server" and (silence_hang or hangup_reason == "inactivity_timeout"):
        return Diagnosis(
            "silence", "Silence watchdog fired on a healthy call", "high",
            f"The pipeline worked, then the conversation paused. hangup_after_silence = {silence_cfg} s and the "
            f"watchdog polls every 2 s, so it decided to hang up {silence_window or 0:.1f} s after the last speech "
            f"and the line dropped at {at}. "
            f"The 'are you still there' check is set to {online_cfg} s, not lower than the hangup, so it never played.",
            [f"Last speech at {last_activity:.1f} s, watchdog decision at {(silence_hang or {}).get('rel', duration):.1f} s, "
             f"line dropped at {at}",
             f"Bolna log: AI silent for {(silence_hang or {}).get('ai_silent')} s, caller silent for "
             f"{(silence_hang or {}).get('user_silent')} s (limit {silence_cfg} s)",
             f"Agent spoke {len(agent_segs)}x, caller {len(caller_segs)}x, transcripts and marks present",
             f"Configured: hangup_after_silence {silence_cfg} s, online check {online_cfg} s"],
            fix_silence,
            "Raise hangup_after_silence to 20–30 s and set the online check below it (about 8 s), so callers "
            "who pause get a gentle prompt instead of a hang-up.")

    if llm_err:
        return Diagnosis("llm", "LLM error ended the call", "medium",
                         f"The language model call failed and Bolna ended the call at {at}.",
                         [f"{llm_err['rel']:.1f} s: {llm_err['msg'][:160]}"],
                         [{"path": "llm_config.model", "before": "?", "after": "a model your key can access", "note": ""}],
                         "Check the LLM key, model name and rate limits.")
    return Diagnosis("unknown", "Disconnect without a clear signature", "low",
                     f"The call ended at {at}. The first missing stage is "
                     f"'{first_missing['label'] if first_missing else 'none'}'.",
                     [f"Ended by: {ended_by}", f"Hangup reason: {hangup_reason}"], [], "")
