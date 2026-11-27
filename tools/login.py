"""Sign in to Facebook in the watcher's own browser profile, and verify it.

    python tools/login.py

Equivalent to `python main.py --login`; both run watcher.login.sign_in(). The
verification matters: a signed-out profile still serves Marketplace search
results, anonymously and from a more heavily cached edge, so "the page loaded"
proves nothing. This waits for a c_user/xs pair and then confirms the
notifications page does not bounce to a login form.

The profile is single-use while Chromium holds it, so stop the watcher first
(the dashboard's Stop button, or POST /api/stop to the dashboard port).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Facebook's pages carry characters a cp1252 console cannot encode.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - a redirected stream may not support it
        pass

from watcher import config as config_loader  # noqa: E402
from watcher.login import DEFAULT_TIMEOUT_S, sign_in  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"seconds to wait for a sign-in (default {int(DEFAULT_TIMEOUT_S)})",
    )
    args = parser.parse_args()
    cfg = config_loader.load()
    return asyncio.run(sign_in(cfg, args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
