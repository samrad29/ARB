"""Summarize polling arb ticks: counts, size, and how long they lasted.

Run:  python -m polling.analysis
      python -m polling.analysis path/to/moneyline.db
"""

from __future__ import annotations

import sys
from pathlib import Path

from polling.analysis.stats import analyze, print_report


def main() -> None:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    print_report(analyze(db_path))


if __name__ == "__main__":
    main()
