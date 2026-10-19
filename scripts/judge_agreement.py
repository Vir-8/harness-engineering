"""How often does the LLM judge agree with your labels in evals/judge_labels.yaml?

    uv run python scripts/judge_agreement.py
"""

from refund_harness.evals.judge_check import agreement, load_sheet, render_agreement

if __name__ == "__main__":
    print(render_agreement(agreement(load_sheet())))
