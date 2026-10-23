"""Summarising a run, saving it, and comparing two runs."""

from collections import defaultdict
from pathlib import Path

from refund_harness.evals import EVALS_DIR
from refund_harness.evals.results import CaseRun, RunReport

RESULTS_DIR = EVALS_DIR / "results"


def pass_rate(runs: list[CaseRun]) -> float:
    return sum(run.passed for run in runs) / len(runs) if runs else 0.0


def case_pass_rates(report: RunReport) -> dict[str, float]:
    """Each case's pass rate across its repeats. One run per case is 0.0 or 1.0; with repeats it shows flakiness."""
    by_case: dict[str, list[CaseRun]] = defaultdict(list)
    for run in report.runs:
        by_case[run.case_id].append(run)
    return {case_id: pass_rate(runs) for case_id, runs in by_case.items()}


def scorer_rates(report: RunReport) -> dict[str, tuple[int, int]]:
    """For each scorer: (passed, how many runs it applied to). Not-applicable runs are left out."""
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for run in report.runs:
        for name, value in run.scores.items():
            if value is not None:
                counts[name][1] += 1
                counts[name][0] += value
    return {name: (passed, total) for name, (passed, total) in counts.items()}


def category_rates(report: RunReport) -> dict[str, float]:
    by_category: dict[str, list[CaseRun]] = defaultdict(list)
    for run in report.runs:
        by_category[run.category].append(run)
    return {category: pass_rate(runs) for category, runs in sorted(by_category.items())}


def p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, round(0.95 * len(ordered)) - 1)] if ordered else 0.0


def summarize(report: RunReport) -> dict:
    runs = report.runs
    latencies = [run.latency_s for run in runs]
    return {
        "runs": len(runs),
        "cases": len({run.case_id for run in runs}),
        "pass_rate": pass_rate(runs),
        "by_category": category_rates(report),
        "by_scorer": scorer_rates(report),
        "latency_mean_s": sum(latencies) / len(latencies) if latencies else 0.0,
        "latency_p95_s": p95(latencies),
        "agent_cost_usd": round(sum(run.cost_usd for run in runs), 4),
        "judge_cost_usd": round(sum(run.judge_cost_usd for run in runs), 4),
        "cost_per_run_usd": round(sum(run.cost_usd for run in runs) / len(runs), 4) if runs else 0.0,
        "input_tokens": sum(run.input_tokens for run in runs),
        "output_tokens": sum(run.output_tokens for run in runs),
        "errors": sum(run.error is not None for run in runs),
        "retried": sum(run.attempts > 1 for run in runs),
        "judge_errors": sum(run.judge_error is not None for run in runs),
        "repeated_calls": sum(run.repeated_calls for run in runs),
        "failing_cases": sorted(case for case, rate in case_pass_rates(report).items() if rate < 1),
    }


def render_summary(report: RunReport) -> str:
    s = summarize(report)
    dirty = " (uncommitted changes)" if report.git_dirty else ""
    lines = [
        f"run: {report.label}   agent: {report.agent_model}   judge: {report.judge_model or 'none'}   code: {report.git_sha}{dirty}",
        f"{s['runs']} runs over {s['cases']} cases   pass rate: {s['pass_rate']:.0%}",
        "",
        "by category:  " + "   ".join(f"{name} {rate:.0%}" for name, rate in s["by_category"].items()),
        "",
        "by check (passed / applicable):",
        *(f"  {name:20} {passed:>3} / {total:<3} {passed / total:>5.0%}" for name, (passed, total) in sorted(s["by_scorer"].items())),
        "",
        f"latency: mean {s['latency_mean_s']:.1f}s, p95 {s['latency_p95_s']:.1f}s",
        f"tokens: {s['input_tokens']:,} in, {s['output_tokens']:,} out   agent cost ${s['agent_cost_usd']:.2f} (${s['cost_per_run_usd']:.3f} per run)   judge cost ${s['judge_cost_usd']:.2f}",
        f"runs that errored: {s['errors']}   needed a retry: {s['retried']}   judge errors: {s['judge_errors']}   identical tool calls repeated: {s['repeated_calls']}",
    ]
    if s["failing_cases"]:
        lines += ["", "cases that did not always pass:", *(f"  {case}" for case in s["failing_cases"])]
    return "\n".join(lines)


def compare(a: RunReport, b: RunReport) -> str:
    sa, sb = summarize(a), summarize(b)
    rates_a, rates_b = case_pass_rates(a), case_pass_rates(b)
    shared = sorted(set(rates_a) & set(rates_b))
    regressions = [c for c in shared if rates_b[c] < rates_a[c]]
    improvements = [c for c in shared if rates_b[c] > rates_a[c]]

    def row(name: str, left: str, right: str, delta: str = "") -> str:
        return f"  {name:24} {left:>12} {right:>12}  {delta}"

    scorers = sorted(set(sa["by_scorer"]) | set(sb["by_scorer"]))

    def scorer_rate(summary: dict, name: str) -> float | None:
        passed_total = summary["by_scorer"].get(name)
        return passed_total[0] / passed_total[1] if passed_total else None

    lines = [
        f"A: {a.label} ({a.agent_model}, code {a.git_sha})",
        f"B: {b.label} ({b.agent_model}, code {b.git_sha})",
        "",
        row("", "A", "B", "change"),
        row("pass rate", f"{sa['pass_rate']:.0%}", f"{sb['pass_rate']:.0%}", f"{sb['pass_rate'] - sa['pass_rate']:+.0%}"),
    ]
    for name in scorers:
        ra, rb = scorer_rate(sa, name), scorer_rate(sb, name)
        lines.append(row(name, "-" if ra is None else f"{ra:.0%}", "-" if rb is None else f"{rb:.0%}",
                         "" if ra is None or rb is None else f"{rb - ra:+.0%}"))
    lines += [
        row("latency mean (s)", f"{sa['latency_mean_s']:.1f}", f"{sb['latency_mean_s']:.1f}", f"{sb['latency_mean_s'] - sa['latency_mean_s']:+.1f}"),
        row("cost per run ($)", f"{sa['cost_per_run_usd']:.3f}", f"{sb['cost_per_run_usd']:.3f}", f"{sb['cost_per_run_usd'] - sa['cost_per_run_usd']:+.3f}"),
        row("repeated tool calls", str(sa["repeated_calls"]), str(sb["repeated_calls"])),
        "",
        "got WORSE:  " + (", ".join(f"{c} ({rates_a[c]:.0%} -> {rates_b[c]:.0%})" for c in regressions) or "none"),
        "got BETTER: " + (", ".join(f"{c} ({rates_a[c]:.0%} -> {rates_b[c]:.0%})" for c in improvements) or "none"),
    ]
    only = sorted(set(rates_a) ^ set(rates_b))
    if only:
        lines.append(f"(not in both runs, so not compared: {', '.join(only)})")
    return "\n".join(lines)


def save_report(report: RunReport, directory: Path = RESULTS_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = report.started_at.replace(":", "").replace("-", "")[:15]
    path = directory / f"{stamp}-{report.label}.json"
    path.write_text(report.model_dump_json(indent=2))
    return path


def load_report(path: Path) -> RunReport:
    return RunReport.model_validate_json(Path(path).read_text())
