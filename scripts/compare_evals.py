"""Compares two saved eval runs: overall and per check, plus which cases got worse or better.

    uv run python scripts/compare_evals.py evals/results/<a>.json evals/results/<b>.json
"""

import sys
from pathlib import Path

from refund_harness.evals.report import compare, load_report

if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    print(compare(load_report(Path(sys.argv[1])), load_report(Path(sys.argv[2]))))
