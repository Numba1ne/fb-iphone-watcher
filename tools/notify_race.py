"""Compare saved-search notification arrival against the watcher's poll.

    python tools/notify_race.py

Reads the two records the watcher leaves behind:

  data/notif-log.jsonl   first moment each listing id appeared in a notification
  data/bot.db            first moment the poll loop saw the same id

and reports, per listing, which channel got there first and by how much. Both
timestamps are written by the same process on the same clock, so the delta is
a real measurement rather than an inference from Facebook's relative wording.

The probe only runs when `notifications.probe: true` in config.yaml, and only
every `probe_every` polls, so a listing can be up to that many intervals late
on the notification side. That bias runs *against* the notification, which is
the safe direction for the conclusion.
"""

from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watcher import config as config_loader  # noqa: E402
from watcher.store import Store  # noqa: E402


def _parse(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None


def _load_notif_log(path: Path) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    if not path.exists():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("id"):
            entries[row["id"]] = row
    return entries


def main() -> int:
    cfg = config_loader.load()
    log_path = Path(cfg.db_path).parent / "notif-log.jsonl"

    notif = _load_notif_log(log_path)
    if not notif:
        print(
            f"\n  No notification sightings in {log_path}.\n\n"
            "  Either the probe has not run yet, or no saved-search alert has\n"
            "  fired. Check that notifications.probe is true in config.yaml and\n"
            "  that the watcher has been running for a while.\n"
        )
        return 1

    with Store(cfg.db_path) as store:
        rows, _ = store.search_listings(limit=100000)
    polled = {row["id"]: row for row in rows}

    print(f"\n  {len(notif)} listing(s) seen in notifications\n")
    print(f"  {'listing':<44} {'notif':>8} {'poll':>8} {'delta':>9}  verdict")
    print(f"  {'-' * 44} {'-' * 8} {'-' * 8} {'-' * 9}  -------")

    deltas: list[float] = []
    notif_first = poll_first = only_notif = 0

    for item_id, entry in sorted(notif.items(), key=lambda kv: kv[1].get("first_seen", "")):
        notif_at = _parse(entry.get("first_seen", ""))
        row = polled.get(item_id)
        label = (entry.get("text") or item_id)[:44]

        if row is None:
            only_notif += 1
            print(f"  {label:<44} {'':>8} {'':>8} {'':>9}  not in poll")
            continue

        poll_at = _parse(row.get("first_seen", ""))
        if notif_at is None or poll_at is None:
            continue

        delta = (notif_at - poll_at).total_seconds()
        deltas.append(delta)
        if delta < 0:
            notif_first += 1
            verdict = f"notif {abs(delta):.0f}s earlier"
        elif delta > 0:
            poll_first += 1
            verdict = f"poll {delta:.0f}s earlier"
        else:
            verdict = "same second"
        print(
            f"  {label:<44} {notif_at:%H:%M:%S} {poll_at:%H:%M:%S} "
            f"{delta:>+8.0f}s  {verdict}"
        )

    print()
    if deltas:
        median = statistics.median(deltas)
        print(f"  compared:        {len(deltas)}")
        print(f"  notification first: {notif_first}")
        print(f"  poll first:         {poll_first}")
        print(f"  median delta:    {median:+.0f}s  (negative = notification earlier)")
    if only_notif:
        print(f"  in notifications but never polled: {only_notif}")
        print("    (filtered out, past max_cards, or never indexed)")

    if not deltas:
        print("\n  No listing has been seen by both channels yet — nothing to compare.\n")
        return 1

    print()
    if median < 0:
        print(f"  VERDICT: saved-search notifications arrive first, by ~{abs(median):.0f}s.\n")
    else:
        print(f"  VERDICT: the poll arrives first, by ~{median:.0f}s.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
