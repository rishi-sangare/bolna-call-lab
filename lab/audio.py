"""G.711 mu-law helpers and small audio utilities (numpy only, no audioop dependency)."""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

RATE = 8000
FRAME_MS = 20
FRAME_BYTES = RATE * FRAME_MS // 1000          # 160 bytes of mu-law per 20 ms frame
SILENCE_BYTE = 0xFF                            # mu-law digital silence

_BIAS, _CLIP = 0x84, 32635


def _build_decode_table() -> np.ndarray:
    table = np.zeros(256, dtype=np.int16)
    for i in range(256):
        u = ~i & 0xFF
        sign = u & 0x80
        exponent = (u >> 4) & 0x07
        mantissa = u & 0x0F
        sample = ((mantissa << 3) + _BIAS) << exponent
        sample -= _BIAS
        table[i] = -sample if sign else sample
    return table


_DECODE = _build_decode_table()


def ulaw_to_pcm(data: bytes) -> np.ndarray:
    """mu-law bytes -> int16 samples."""
    return _DECODE[np.frombuffer(data, dtype=np.uint8)]


def pcm_to_ulaw(samples: np.ndarray) -> bytes:
    """int16 samples -> mu-law bytes (ITU G.711)."""
    s = samples.astype(np.int32)
    sign = (s < 0).astype(np.int32) << 7
    s = np.minimum(np.abs(s), _CLIP) + _BIAS
    exponent = np.floor(np.log2(np.maximum(s, 1))).astype(np.int32) - 7
    exponent = np.clip(exponent, 0, 7)
    mantissa = (s >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8).tobytes()


def resample_linear(samples: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or samples.size == 0:
        return samples
    n = int(round(samples.size * dst / src))
    x = np.linspace(0, samples.size - 1, n)
    return np.interp(x, np.arange(samples.size), samples.astype(np.float32)).astype(np.int16)


def read_wav_as_ulaw(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        ch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError("expected 16-bit WAV")
    s = np.frombuffer(raw, dtype=np.int16)
    if ch > 1:
        s = s.reshape(-1, ch).mean(axis=1).astype(np.int16)
    return pcm_to_ulaw(resample_linear(s, rate, RATE))


def write_stereo_wav(path: Path, left_ulaw: bytearray, right_ulaw: bytearray) -> None:
    """Caller on the left, agent on the right - the way call recordings are usually split."""
    n = max(len(left_ulaw), len(right_ulaw))
    l = ulaw_to_pcm(bytes(left_ulaw).ljust(n, b"\xff"))
    r = ulaw_to_pcm(bytes(right_ulaw).ljust(n, b"\xff"))
    st = np.empty(n * 2, dtype=np.int16)
    st[0::2], st[1::2] = l, r
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(st.tobytes())


def rms_db(ulaw: bytes) -> float:
    if not ulaw:
        return -90.0
    s = ulaw_to_pcm(ulaw).astype(np.float32) / 32768
    return float(20 * np.log10(np.sqrt(np.mean(s * s)) + 1e-9))
