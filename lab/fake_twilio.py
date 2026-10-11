"""A Twilio Media Streams client that behaves like a real phone call.

What Twilio does on a <Connect><Stream> call, this does too:
  * sends `connected`, then `start` (streamSid, callSid, mediaFormat audio/x-mulaw 8000 mono)
  * streams the caller's audio as 20 ms / 160-byte base64 `media` frames, continuously (silence when
    the caller isn't talking), with track="inbound", chunk counter and timestamp
  * plays the agent's `media` in real time, and echoes each `mark` back only once the audio queued
    before it has finished playing (that echo is what tells Bolna the agent "spoke")
  * on `clear` drops queued agent audio and returns pending marks immediately (barge-in)
  * when the server closes the websocket the call is over (Twilio would end <Connect> the same way)

The caller follows a script (what to say, and when) so every take is reproducible.
"""
from __future__ import annotations

import asyncio
import base64
import json
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import websockets

from .audio import FRAME_BYTES, SILENCE_BYTE, resample_linear, rms_db, ulaw_to_pcm

SILENCE = bytes([SILENCE_BYTE]) * FRAME_BYTES


@dataclass
class Turn:
    say: str                               # the caller's line
    wait: str = "agent_done"               # "agent_done" | "now" | "seconds"
    pause: float = 0.6                     # thinking pause before speaking (after the wait condition)
    seconds: float = 0.0                   # for wait="seconds": seconds after the previous turn ended
    timeout: float = 6.0                   # speak anyway if the agent hasn't started talking within this many seconds
    audio: bytes = b""                     # rendered mu-law (filled by the runner)


@dataclass
class CallOptions:
    url: str
    turns: list[Turn]
    hold_media: bool = False               # fault: never send caller audio (dead inbound track)
    playback: bool = True                  # play the agent through the speakers
    max_seconds: float = 150.0
    hangup_after_script: float = 4.0       # caller hangs up this long after its last line + agent reply
    agent_idle_ms: int = 1300              # agent counts as "done talking" after this much silence


@dataclass
class CallResult:
    ended_by: str = ""                     # "server" | "caller" | "timeout" | "error"
    close_code: int | None = None
    close_reason: str = ""
    duration: float = 0.0
    caller_track: bytearray = field(default_factory=bytearray)
    agent_track: bytearray = field(default_factory=bytearray)


class Speaker:
    """Real-time playback on the default output device (48 kHz), fed from a thread."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.ok = False
        try:
            import sounddevice as sd
            self.stream = sd.OutputStream(samplerate=48000, channels=1, dtype="int16", blocksize=960, latency="low")
            self.stream.start()
            self.ok = True
            threading.Thread(target=self._run, daemon=True).start()
        except Exception:
            self.stream = None

    def _run(self):
        while True:
            pcm = self.q.get()
            if pcm is None:
                break
            try:
                self.stream.write(pcm)
            except Exception:
                pass

    def play(self, ulaw: bytes) -> None:
        if self.ok:
            self.q.put(resample_linear(ulaw_to_pcm(ulaw), 8000, 48000).reshape(-1, 1))

    def close(self):
        if self.ok:
            self.q.put(None)
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass


class FakeTwilioCall:
    def __init__(self, opts: CallOptions, emit: Callable[[dict], None]):
        self.o = opts
        self.emit_cb = emit
        self.stream_sid = "MZ" + uuid.uuid4().hex
        self.call_sid = "CA" + uuid.uuid4().hex
        self.t0 = 0.0
        self.ws = None
        self.res = CallResult()
        self.agent_q: deque = deque()          # ("audio", bytes) | ("mark", name)
        self.agent_partial = b""
        self.agent_speaking = False
        self.agent_last_audio = 0.0
        self.agent_ever_spoke = False
        self.caller_audio = b""                 # remaining bytes of the line being spoken
        self.caller_speaking = False
        self.closed = asyncio.Event()
        self.chunk = 0
        self.speaker = Speaker() if opts.playback else None

    # ------------------------------------------------------------------ events
    def now(self) -> float:
        return time.time() - self.t0 if self.t0 else 0.0

    def emit(self, kind: str, **data) -> None:
        self.emit_cb({"t": time.time(), "rel": round(self.now(), 3), "src": "caller", "kind": kind, **data})

    async def send(self, msg: dict) -> None:
        if self.ws is not None and not self.closed.is_set():
            await self.ws.send(json.dumps(msg))

    # ------------------------------------------------------------------ main
    async def run(self) -> CallResult:
        try:
            async with websockets.connect(self.o.url, max_size=None, ping_interval=None) as ws:
                self.ws = ws
                self.t0 = time.time()
                self.emit("ws_open", url=self.o.url)
                await self.send({"event": "connected", "protocol": "Call", "version": "1.0.0"})
                await self.send({"event": "start", "sequenceNumber": "1", "streamSid": self.stream_sid,
                                 "start": {"accountSid": "AC" + "0" * 32, "streamSid": self.stream_sid,
                                           "callSid": self.call_sid, "tracks": ["inbound"],
                                           "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000,
                                                           "channels": 1},
                                           "customParameters": {}}})
                self.emit("start_sent", stream_sid=self.stream_sid, call_sid=self.call_sid,
                          media_format="audio/x-mulaw 8000 Hz mono", hold_media=self.o.hold_media)
                tasks = [asyncio.create_task(c) for c in (self._receiver(), self._clock(), self._script())]
                try:
                    await asyncio.wait_for(self.closed.wait(), timeout=self.o.max_seconds)
                except asyncio.TimeoutError:
                    self.res.ended_by = "timeout"
                    self.emit("hangup", by="caller", reason="max duration reached")
                for t in tasks:
                    t.cancel()
                if self.res.ended_by in ("caller", "timeout"):
                    await self.send({"event": "stop", "streamSid": self.stream_sid,
                                     "stop": {"accountSid": "AC" + "0" * 32, "callSid": self.call_sid}})
                    await asyncio.sleep(0.3)
                    await ws.close()
        except Exception as e:
            if not self.res.ended_by:
                self.res.ended_by = "error"
                self.res.close_reason = str(e)
                self.emit("error", message=str(e))
        finally:
            self.res.duration = round(self.now(), 3)
            self.emit("call_end", duration=self.res.duration, ended_by=self.res.ended_by,
                      close_code=self.res.close_code)
            if self.speaker:
                self.speaker.close()
        return self.res

    # ------------------------------------------------------------------ websocket in
    async def _receiver(self) -> None:
        first_media = True
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                ev = msg.get("event")
                if ev == "media":
                    audio = base64.b64decode(msg["media"]["payload"])
                    if first_media:
                        first_media = False
                        self.emit("first_agent_media", bytes=len(audio))
                    self.agent_q.append(("audio", audio))
                elif ev == "mark":
                    self.agent_q.append(("mark", msg["mark"]["name"]))
                    self.emit("mark_received", name=msg["mark"]["name"][:12])
                elif ev == "clear":
                    dropped = sum(len(x[1]) for x in self.agent_q if x[0] == "audio") + len(self.agent_partial)
                    marks = [x[1] for x in self.agent_q if x[0] == "mark"]
                    self.agent_q.clear()
                    self.agent_partial = b""
                    self.emit("clear_received", dropped_ms=dropped // 8, marks_returned=len(marks))
                    for m in marks:
                        await self.send({"event": "mark", "streamSid": self.stream_sid, "mark": {"name": m}})
                    if self.agent_speaking:
                        self.agent_speaking = False
                        self.emit("agent_speech_end", interrupted=True)
        except websockets.ConnectionClosed as e:
            self._closed_by_server(e.code, e.reason)
            return
        self._closed_by_server(getattr(self.ws, "close_code", None), getattr(self.ws, "close_reason", ""))

    def _closed_by_server(self, code, reason) -> None:
        if self.closed.is_set():
            return
        if not self.res.ended_by:
            self.res.ended_by = "server"
            self.res.close_code, self.res.close_reason = code, reason or ""
            self.emit("hangup", by="server", code=code, reason=reason or "",
                      note="server closed the media stream -> Twilio ends <Connect> and the call")
        self.closed.set()

    # ------------------------------------------------------------------ 20 ms clock
    async def _clock(self) -> None:
        """Every 20 ms: send one caller frame, play one agent frame, release due marks."""
        next_t = time.perf_counter()
        lvl_c, lvl_a, ticks = [], [], 0
        while not self.closed.is_set():
            # ---- caller -> server
            if self.caller_audio:
                frame, self.caller_audio = self.caller_audio[:FRAME_BYTES], self.caller_audio[FRAME_BYTES:]
                frame = frame.ljust(FRAME_BYTES, bytes([SILENCE_BYTE]))
                if not self.caller_audio and self.caller_speaking:
                    self.caller_speaking = False
                    self.emit("caller_speech_end")
            else:
                frame = SILENCE
            self.res.caller_track += frame
            if not self.o.hold_media:
                self.chunk += 1
                await self.send({"event": "media", "sequenceNumber": str(self.chunk + 1),
                                 "streamSid": self.stream_sid,
                                 "media": {"track": "inbound", "chunk": str(self.chunk),
                                           "timestamp": str(self.chunk * 20),
                                           "payload": base64.b64encode(frame).decode()}})
            # ---- server -> caller (playback)
            out = self._next_agent_frame()
            self.res.agent_track += out
            if out != SILENCE:
                if self.speaker:
                    self.speaker.play(out)
                self.agent_last_audio = time.time()
                if not self.agent_speaking:
                    self.agent_speaking = True
                    self.agent_ever_spoke = True
                    self.emit("agent_speech_start")
            elif self.agent_speaking and time.time() - self.agent_last_audio > self.o.agent_idle_ms / 1000:
                self.agent_speaking = False
                self.emit("agent_speech_end", interrupted=False)
            await self._release_marks()
            # audio levels for the UI tape: one sample per 100 ms per channel
            lvl_c.append(frame)
            lvl_a.append(out)
            ticks += 1
            if ticks % 5 == 0:
                self.emit("levels", c=round(rms_db(b"".join(lvl_c)), 1), a=round(rms_db(b"".join(lvl_a)), 1))
                lvl_c, lvl_a = [], []
            next_t += 0.020
            await asyncio.sleep(max(0.0, next_t - time.perf_counter()))

    def _next_agent_frame(self) -> bytes:
        buf = self.agent_partial
        while len(buf) < FRAME_BYTES and self.agent_q and self.agent_q[0][0] == "audio":
            buf += self.agent_q.popleft()[1]
        frame, self.agent_partial = buf[:FRAME_BYTES], buf[FRAME_BYTES:]
        if not frame:
            return SILENCE
        return frame.ljust(FRAME_BYTES, bytes([SILENCE_BYTE]))

    async def _release_marks(self) -> None:
        # a mark is "played" once everything queued before it has been played
        while not self.agent_partial and self.agent_q and self.agent_q[0][0] == "mark":
            name = self.agent_q.popleft()[1]
            await self.send({"event": "mark", "streamSid": self.stream_sid, "mark": {"name": name}})
            self.emit("mark_echoed", name=name[:12])

    # ------------------------------------------------------------------ caller script
    async def _script(self) -> None:
        last_end = time.time()
        for i, turn in enumerate(self.o.turns):
            if turn.wait == "agent_done":
                # wait for the agent to speak and finish; give up if it hasn't started within `timeout`
                start = time.time()
                spoke = False
                while not self.closed.is_set() and time.time() - start < (30.0 if spoke else turn.timeout):
                    if self.agent_speaking:
                        spoke = True
                    if spoke and not self.agent_speaking and time.time() - self.agent_last_audio > self.o.agent_idle_ms / 1000:
                        break
                    await asyncio.sleep(0.05)
                if not spoke:
                    self.emit("caller_waited", seconds=round(time.time() - start, 1), note="agent never spoke")
            elif turn.wait == "seconds":
                while time.time() - last_end < turn.seconds and not self.closed.is_set():
                    await asyncio.sleep(0.05)
            if self.closed.is_set():
                return
            await asyncio.sleep(turn.pause)
            if self.closed.is_set():
                return
            self.caller_audio = turn.audio
            self.caller_speaking = True
            self.emit("caller_speech_start", text=turn.say, turn=i + 1,
                      seconds=round(len(turn.audio) / 8000, 2), level_db=round(rms_db(turn.audio), 1))
            while self.caller_speaking and not self.closed.is_set():
                await asyncio.sleep(0.02)
            last_end = time.time()
        # after the last line: wait for the agent's reply, then the caller hangs up
        start = time.time()
        while time.time() - start < 12 and not self.closed.is_set():
            if self.agent_ever_spoke and not self.agent_speaking and time.time() - self.agent_last_audio > 1.0 \
                    and time.time() - last_end > 2.0:
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(self.o.hangup_after_script)
        if not self.closed.is_set():
            self.res.ended_by = "caller"
            self.emit("hangup", by="caller", reason="script finished")
            self.closed.set()


def silence_ms(ms: int) -> bytes:
    return bytes([SILENCE_BYTE]) * (8 * ms)


__all__ = ["Turn", "CallOptions", "CallResult", "FakeTwilioCall", "np"]
