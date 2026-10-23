"""Writes the numbers the frontend's Quality page shows (frontend/src/data/quality.json), from the saved eval reports.

    uv run python scripts/export_quality.py

Re-run it after a new eval run worth showing. The page reads the file at build time: nothing is fetched at runtime.
"""

import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "evals" / "results"
OUT = ROOT / "frontend" / "src" / "data" / "quality.json"

# The reports to show, newest model first. Claude's per-case costs come from the README: the reports saved before
# agent/pricing.py existed recorded the CLI's model-blind figure, so they are not used here.
MODELS = [
    {"report": "gpt-6-luna-low-x3", "name": "GPT-6 Luna", "detail": "OpenAI", "cost_per_case": None, "current": True},
    {"report": "sonnet-5-5", "name": "Claude Sonnet 5.5", "detail": "Anthropic · CLI", "cost_per_case": 0.03, "current": False},
    {"report": "haiku-5-5", "name": "Claude Haiku 5.5", "detail": "Anthropic · CLI", "cost_per_case": 0.002, "current": False},
]

# The k6 runs (README, "Measured: load on one API process")
LOAD = [
    {"users": 500, "asks_per_s": 82, "p95_s": 2.14, "errors": 0.0, "cpu": "75%"},
    {"users": 1500, "asks_per_s": 168, "p95_s": 5.08, "errors": 0.0, "cpu": "95–99%"},
]


def latest(label: str) -> dict:
    [path, *_] = sorted(RESULTS.glob(f"*-{label}.json"), reverse=True)
    return json.loads(path.read_text())


def summarise(report: dict) -> dict:
    runs = report["runs"]
    passed = [all(v is not False for v in r["scores"].values()) and not r["error"] for r in runs]
    latencies = sorted(r["latency_s"] for r in runs)
    categories: dict[str, list[int]] = {}
    checks: dict[str, list[int]] = {}
    for run, ok in zip(runs, passed):
        tally = categories.setdefault(run["category"], [0, 0])
        tally[0] += ok
        tally[1] += 1
        for name, value in run["scores"].items():
            if value is None:
                continue
            tally = checks.setdefault(name, [0, 0])
            tally[0] += value is True
            tally[1] += 1
    return {
        "cases": len(runs),
        "passed": sum(passed),
        "latency_mean_s": round(statistics.fmean(latencies), 1),
        "latency_p95_s": round(latencies[max(0, round(0.95 * len(latencies)) - 1)], 1),
        "cost_per_case": round(sum(r["cost_usd"] or 0 for r in runs) / len(runs), 6),
        "categories": categories,
        "checks": checks,
        "started_at": report["started_at"],
    }


def main() -> None:
    models = []
    for model in MODELS:
        summary = summarise(latest(model["report"]))
        models.append({
            "name": model["name"], "detail": model["detail"], "current": model["current"],
            "cases": summary["cases"], "passed": summary["passed"], "latency_mean_s": summary["latency_mean_s"],
            "cost_per_case": model["cost_per_case"] if model["cost_per_case"] is not None else summary["cost_per_case"],
        })
    primary = summarise(latest(MODELS[0]["report"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"primary": primary, "models": models, "load": LOAD}, indent=2) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}: {primary['passed']}/{primary['cases']} on {MODELS[0]['name']}")


if __name__ == "__main__":
    main()
