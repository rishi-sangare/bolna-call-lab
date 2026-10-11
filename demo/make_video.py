"""Finish a recording: add the live calls' real audio, write the voice-over script and captions.

    .venv\\Scripts\\python -m demo.make_video [recordings\\<stamp>]

Produces in the recording folder:
    demo.mp4        1920x1080 video with the live calls' audio (agent + caller) at the exact moments they happened
    transcript.md   the voice-over script, one block per scene with its start time
    captions.srt    scene captions (optional upload)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg

ROOT = Path(__file__).resolve().parent.parent
FF = imageio_ffmpeg.get_ffmpeg_exe()


def duration(path: Path) -> float:
    r = subprocess.run([FF, "-nostdin", "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


def srt_ts(t: float) -> str:
    h, rem = divmod(max(t, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s % 1) * 1000):03d}"


def main() -> int:
    folder = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else \
        sorted(p.parent for p in (ROOT / "recordings").glob("*/scenes.json"))[-1]
    data = json.loads((folder / "scenes.json").read_text(encoding="utf-8"))
    raw = folder / "raw.mp4"
    total = duration(raw)
    scenes = data["scenes"]
    rec_t0 = data["rec_t0"]

    # ---- call audio, placed at the moment each live call happened on screen
    inputs, chains = ["-i", str(raw)], []
    for i, run_id in enumerate(data.get("live_runs", []), start=1):
        rep = json.loads((ROOT / "runs" / run_id / "report.json").read_text(encoding="utf-8"))
        wav = ROOT / "runs" / run_id / "call.wav"
        if not rep.get("wall_t0") or not wav.exists():
            continue
        offset_ms = max(0, int((rep["wall_t0"] - rec_t0) * 1000))
        inputs += ["-i", str(wav)]
        chains.append(f"[{len(chains) + 1}:a]pan=mono|c0=0.5*c0+0.5*c1,aresample=48000,volume=1.6,"
                      f"adelay={offset_ms}:all=1[a{len(chains)}]")
        print(f"call audio {run_id} at {offset_ms / 1000:.1f} s")
    out = folder / "demo.mp4"
    if chains:
        mix = "".join(f"[a{i}]" for i in range(len(chains)))
        fc = ";".join(chains) + f";{mix}amix=inputs={len(chains)}:normalize=0,apad=whole_dur={total:.3f}[aout]"
        cmd = [FF, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex", fc,
               "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-t", f"{total:.3f}",
               "-movflags", "+faststart", str(out)]
    else:
        cmd = [FF, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw), "-c", "copy",
               "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)

    # ---- transcript + captions
    spans = [(sc["t"], scenes[i + 1]["t"] if i + 1 < len(scenes) else total, sc) for i, sc in enumerate(scenes)]
    words = sum(len(sc["narration"].split()) for sc in scenes)
    md = ["# Voice-over script", "",
          f"Video **{mmss(total)}** · {len(scenes)} scenes · {words} words · read at a calm, conversational pace.", "",
          "Open **Record Voiceover.bat**: it plays the video, highlights each word as you should be saying it, "
          "records your microphone in sync and merges it for you. You'll hear nothing from the video while "
          "recording; the call audio is mixed underneath your voice automatically.", "",
          "Tips: smile while you talk, pause at full stops, and if a line finishes early just wait for the next one.",
          "", "---", ""]
    for i, (a, b, sc) in enumerate(spans, 1):
        md += [f"### {i:02d} · {mmss(a)} – {mmss(b)} · {sc['caption']}", "", sc["narration"], ""]
    (folder / "transcript.md").write_text("\n".join(md), encoding="utf-8")
    srt = []
    for i, (a, b, sc) in enumerate(spans, 1):
        srt += [str(i), f"{srt_ts(a)} --> {srt_ts(b - 0.05)}", sc["caption"], ""]
    (folder / "captions.srt").write_text("\n".join(srt), encoding="utf-8")
    print("video     ", out, f"({mmss(total)})")
    print("transcript", folder / "transcript.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
