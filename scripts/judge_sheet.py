"""Builds (or refreshes) the sheet you label to check the LLM judge against a human. Existing labels are kept.

    uv run python scripts/judge_sheet.py evals/results/<run>.json
Then fill in evals/judge_labels.yaml and run scripts/judge_agreement.py.
"""

import sys
from pathlib import Path

from refund_harness.evals.judge_check import (
    SHEET_FILE,
    build_sheet,
    load_sheet,
    merge_labels,
    render_sheet,
)
from refund_harness.evals.report import load_report
from refund_harness.refund_tools import build_registry
from refund_harness.retrieval.embedder import FakeEmbedder

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    old = load_sheet() if SHEET_FILE.exists() else None
    sheet = merge_labels(build_sheet(load_report(Path(sys.argv[1]))), old)
    specs = build_registry(None, "acme", FakeEmbedder()).specs()
    SHEET_FILE.write_text(render_sheet(sheet, specs))
    labelled = sum(e["human_supported"] is not None for e in sheet["entries"])
    print(f"wrote {SHEET_FILE}: {len(sheet['entries'])} entries to label ({labelled} already labelled)")
