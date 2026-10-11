"""Rebuild report.json for saved runs with the current Call Doctor (no new calls needed).

    .venv\\Scripts\\python -m lab.reanalyze [run_id ...]
"""
import json
import sys
from pathlib import Path

from doctor.analyze import analyze
from .scenarios import SCENARIOS

RUNS = Path(__file__).resolve().parent.parent / "runs"


def load_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def main() -> int:
    targets = [RUNS / a for a in sys.argv[1:]] or sorted(RUNS.glob("2*-*"))
    for d in targets:
        if not (d / "caller.jsonl").exists():
            continue
        old = json.loads((d / "report.json").read_text(encoding="utf-8")) if (d / "report.json").exists() else {}
        sid = (old.get("scenario") or {}).get("id") or d.name.split("-", 2)[-1]
        meta = SCENARIOS[sid].meta() if sid in SCENARIOS else old.get("scenario")
        rep = analyze(load_jsonl(d / "caller.jsonl"), load_jsonl(d / "engine.jsonl"),
                      json.loads((d / "agent.json").read_text(encoding="utf-8")), meta)
        rep["run_id"] = d.name
        rep["wall_t0"] = old.get("wall_t0")
        (d / "report.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
        print(f"{d.name:40} {rep['diagnosis']['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
