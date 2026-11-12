"""Run every test suite. Exits non-zero if any fails.

    python -m tests.run_all
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUITES = [
    "tests/test_logic.py",
    "tests/test_parsing.py",
    "tests/test_notify.py",
    "tests/test_loop.py",
    "tests/test_probe.py",
    "tests/test_web.py",
]


def main() -> int:
    failed = []
    for suite in SUITES:
        print(f"\n=== {suite} ===")
        result = subprocess.run([sys.executable, str(ROOT / suite)], cwd=ROOT)
        if result.returncode != 0:
            failed.append(suite)

    print("\n" + "=" * 50)
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print(f"All {len(SUITES)} suites passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
