"""Console + rotating-file logging."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path


def configure(log_path: str, *, verbose: bool = False) -> None:
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S"))
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-7s %(name)s: %(message)s")
    )
    root.addHandler(file_handler)

    # Playwright and asyncio chatter drowns out our own poll summary at DEBUG.
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
