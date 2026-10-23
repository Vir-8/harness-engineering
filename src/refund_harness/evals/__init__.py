"""Evals: hand-labelled cases run through the real harness and scored in code."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
EVALS_DIR = REPO_ROOT / "evals"  # the case file, the judge's labelling sheet and the saved results
