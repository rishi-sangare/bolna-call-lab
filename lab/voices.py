"""Caller voice: renders the scripted caller lines to 8 kHz mu-law once, then caches them.

ElevenLabs (natural voice) when a key is configured, otherwise the built-in Windows voice (offline).
Rendering happens before a call starts, so it never adds latency to the call itself.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

import httpx

import numpy as np

from .audio import read_wav_as_ulaw, ulaw_to_pcm

CACHE = Path(__file__).resolve().parent.parent / "runs" / "cache"
CALLER_VOICE_ID = os.getenv("CALLER_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")       # "Sarah" - distinct from the agent


def _key(text: str, engine: str) -> Path:
    h = hashlib.sha1(f"{engine}|{CALLER_VOICE_ID}|{text}".encode()).hexdigest()[:16]
    return CACHE / f"caller_{engine}_{h}.ulaw"


def _elevenlabs(text: str) -> bytes | None:
    key = os.getenv("ELEVENLABS_API_KEY", "")
    if not key:
        return None
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{CALLER_VOICE_ID}?output_format=ulaw_8000"
    r = httpx.post(url, headers={"xi-api-key": key}, json={"text": text, "model_id": "eleven_flash_v2_5"},
                   timeout=30)
    return r.content if r.status_code == 200 else None


def _windows_sapi(text: str) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        wav = Path(d) / "line.wav"
        safe = text.replace("'", "''")
        ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(8000, "
              "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono); "
              f"$s.SetOutputToWaveFile('{wav}', $f); $s.Speak('{safe}'); $s.Dispose()")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True)
        return read_wav_as_ulaw(wav)


def trim(ulaw: bytes, pad_ms: int = 40) -> bytes:
    """Cut leading/trailing silence (TTS clips carry ~0.2-0.4 s of it)."""
    pcm = np.abs(ulaw_to_pcm(ulaw).astype(np.int32))
    win = 80                                            # 10 ms windows
    n = len(pcm) // win
    if n == 0:
        return ulaw
    energy = pcm[: n * win].reshape(n, win).mean(axis=1)
    loud = np.where(energy > 300)[0]
    if loud.size == 0:
        return ulaw
    pad = pad_ms // 10
    a, b = max(0, loud[0] - pad) * win, min(n, loud[-1] + 1 + pad) * win
    return ulaw[a:b]


def render(text: str) -> bytes:
    return trim(_render(text))


def _render(text: str) -> bytes:
    CACHE.mkdir(parents=True, exist_ok=True)
    for engine in ("elevenlabs", "sapi"):
        p = _key(text, engine)
        if p.exists():
            return p.read_bytes()
    audio = _elevenlabs(text)
    engine = "elevenlabs"
    if audio is None:
        audio, engine = _windows_sapi(text), "sapi"
    _key(text, engine).write_bytes(audio)
    return audio
