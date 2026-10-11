"""Run a scenario through the Call Lab API and print the outcome (for testing).

    .venv\\Scripts\\python -m lab.run_cli fixed [--playback]
"""
import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:7860"


def main() -> int:
    sid = sys.argv[1]
    playback = 1 if "--playback" in sys.argv else 0
    r = httpx.post(f"{BASE}/api/runs/{sid}?playback={playback}", timeout=10)
    r.raise_for_status()
    run_id = r.json()["run_id"]
    print("run", run_id)
    t0 = time.time()
    while time.time() - t0 < 240:
        time.sleep(2)
        rr = httpx.get(f"{BASE}/api/runs/{run_id}/report", timeout=10)
        if rr.status_code == 200:
            rep = rr.json()
            c, d = rep["call"], rep["diagnosis"]
            print(f"duration {c['duration']}s ended_by {c['ended_by']} reason {c['hangup_reason']} "
                  f"silence {c['silence_window']}")
            print("milestones:", " | ".join(f"{m['key']}={'ok@%.1f' % m['t'] if m['ok'] else 'MISSING'}"
                                            for m in rep["milestones"]))
            print("latency:", rep["latency"].get("p50"), [t["gap_ms"] for t in rep["latency"]["turns"]])
            print("diagnosis:", d["cause_id"], "-", d["title"])
            for e in d["evidence"]:
                print("   *", e)
            print("engine warnings/errors:")
            for ln in rep["engine_lines"][-12:]:
                print(f"   {ln['rel']:7.2f} {ln['level']:7} {ln['msg'][:160]}")
            return 0
    print("timed out")
    return 1


if __name__ == "__main__":
    sys.exit(main())
