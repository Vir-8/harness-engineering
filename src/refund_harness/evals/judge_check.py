"""Checking the LLM judge against a human.

A judge is only useful if it agrees with a person on the same material. This builds a labelling sheet from a saved run
(question, evidence, answer: with the judge's verdict kept OUT of sight so it cannot sway the labeller), reads the human
labels back, and measures how often the judge agrees, and in which direction it errs.
"""

import random
from pathlib import Path

import yaml

from refund_harness.agent.types import ToolSpec
from refund_harness.evals.judge import RUBRIC, format_capabilities, format_evidence
from refund_harness.evals.results import CaseRun, RunReport

SHEET_FILE = Path(__file__).resolve().parents[3] / "evals" / "judge_labels.yaml"


def entry_id(run: CaseRun) -> str:
    return f"{run.case_id}#{run.repeat}"


def build_sheet(report: RunReport, supported_extra: int | None = None, seed: int = 7) -> dict:
    """Every run the judge called UNSUPPORTED, plus some it called supported (so both kinds can be checked), shuffled."""
    judged = [run for run in report.runs if run.judge is not None]
    unsupported = [run for run in judged if not run.judge.supported]
    supported = [run for run in judged if run.judge.supported]
    extra = len(unsupported) if supported_extra is None else supported_extra
    chosen = unsupported + random.Random(seed).sample(supported, min(max(extra, 6), len(supported)))
    random.Random(seed).shuffle(chosen)  # so the order does not give the verdict away
    return {
        "entries": [
            {
                "id": entry_id(run),
                "question": run.question or _question(run),
                "evidence": format_evidence(run.tools),
                "answer": run.answer,
                "human_supported": None,
                "human_declines": None,
            }
            for run in chosen
        ],
        "judged_by": {
            entry_id(run): {"supported": run.judge.supported, "declines": run.judge.declines, "reason": run.judge.reason}
            for run in chosen
        },
    }


def _question(run: CaseRun) -> str:
    from refund_harness.evals.cases import load_cases

    return next(case.question for case in load_cases() if case.id == run.case_id)


def merge_labels(new: dict, old: dict | None) -> dict:
    """Regenerating the sheet must never throw away labels a person already wrote."""
    if not old:
        return new
    labelled = {e["id"]: e for e in old["entries"] if e.get("human_supported") is not None or e.get("human_declines") is not None}
    new["entries"] = [labelled.get(e["id"], e) for e in new["entries"]]
    present = {e["id"] for e in new["entries"]}
    new["entries"] += [e for i, e in labelled.items() if i not in present]
    new["judged_by"] = {**{k: v for k, v in old.get("judged_by", {}).items() if k in labelled}, **new["judged_by"]}
    return new


HEADER = """# Labelling sheet: check the JUDGE against YOU. For each entry, read the question, the evidence and the answer, then set
#   human_supported: true  if every factual claim the answer makes about policies, orders or refunds is in the evidence
#                           or in the tool descriptions below, false otherwise
#   human_declines:  true  if the answer says it cannot confirm / does not know / cannot help, instead of answering
# The judge's own verdicts are kept at the very bottom of this file so they cannot sway you: don't scroll down until
# you are done. Then run: uv run python scripts/judge_agreement.py
#
# The rubric the judge was given (label by the same one, or you are measuring a different question):
{rubric}
#
# The tools the agent was given (a claim about what the agent itself can do is supported if these say so):
{tools}
"""


def _literal_dumper() -> type[yaml.SafeDumper]:
    class Dumper(yaml.SafeDumper):
        pass

    Dumper.add_representer(str, lambda dumper, text: dumper.represent_scalar("tag:yaml.org,2002:str", text, style="|" if "\n" in text else None))
    return Dumper


def render_sheet(sheet: dict, specs: list[ToolSpec]) -> str:
    commented = lambda text: "\n".join("# " + line for line in text.splitlines())  # noqa: E731
    header = HEADER.format(rubric=commented(RUBRIC), tools=commented(format_capabilities(specs)))
    body = yaml.dump({"entries": sheet["entries"]}, Dumper=_literal_dumper(), sort_keys=False, allow_unicode=True, width=110)
    verdicts = yaml.dump({"judged_by": sheet["judged_by"]}, Dumper=_literal_dumper(), sort_keys=False, allow_unicode=True, width=110)
    return f"{header}\n{body}\n\n# {'=' * 100}\n# STOP: the judge's verdicts are below. Label everything above first.\n# {'=' * 100}\n{verdicts}"


def load_sheet(path: Path = SHEET_FILE) -> dict:
    return yaml.safe_load(Path(path).read_text())


def cohens_kappa(pairs: list[tuple[bool, bool]]) -> float | None:
    """Agreement beyond what chance alone would give: 1 is perfect, 0 is no better than chance. None if undefined."""
    n = len(pairs)
    if n == 0:
        return None
    observed = sum(a == b for a, b in pairs) / n
    p_a_true = sum(a for a, _ in pairs) / n
    p_b_true = sum(b for _, b in pairs) / n
    expected = p_a_true * p_b_true + (1 - p_a_true) * (1 - p_b_true)
    return None if expected == 1 else (observed - expected) / (1 - expected)


def agreement(sheet: dict) -> dict:
    verdicts = sheet.get("judged_by", {})
    result = {}
    for field in ("supported", "declines"):
        pairs, rows = [], []
        for entry in sheet["entries"]:
            human = entry.get(f"human_{field}")
            if human is None or entry["id"] not in verdicts:
                continue
            judge = verdicts[entry["id"]][field]
            pairs.append((judge, human))
            rows.append((entry["id"], judge, human, verdicts[entry["id"]]["reason"]))
        result[field] = {
            "labelled": len(pairs),
            "agreement": sum(j == h for j, h in pairs) / len(pairs) if pairs else None,
            "kappa": cohens_kappa(pairs),
            "judge_false_human_true": sum((not j) and h for j, h in pairs),  # the judge was TOO STRICT
            "judge_true_human_false": sum(j and (not h) for j, h in pairs),  # the judge was TOO LENIENT
            "disagreements": [row for row in rows if row[1] != row[2]],
        }
    return result


def render_agreement(result: dict) -> str:
    lines = []
    for field, r in result.items():
        if not r["labelled"]:
            lines.append(f"{field}: no labelled entries yet")
            continue
        kappa = "n/a" if r["kappa"] is None else f"{r['kappa']:.2f}"
        lines += [
            f"{field}: {r['labelled']} labelled, the judge agrees with you {r['agreement']:.0%} of the time (kappa {kappa})",
            f"   too strict (judge said false, you said true): {r['judge_false_human_true']}"
            f"   too lenient (judge said true, you said false): {r['judge_true_human_false']}",
        ]
        for case, judge, human, reason in r["disagreements"]:
            lines.append(f"   DISAGREE {case}: judge {judge}, you {human}. Judge's reason: {reason}")
    return "\n".join(lines)
