#!/usr/bin/env python3
"""Watch "Il Token della Sera" on the public scoreboard and print new dispatches.

The scoreboard page (https://projectnandaitaly.ai/) reads ``GET /v1/scoreboard``; its
``press`` list is the news column. This script keeps the ids it has already seen in
``.token-watch/seen.json`` (git-ignored) and prints only new dispatches, as JSON.

Usage:
    python3 tools/token_watch.py           # new dispatches, then mark them seen
    python3 tools/token_watch.py --peek    # new dispatches, do not mark them
    python3 tools/token_watch.py --all     # every dispatch on the board, state untouched

Output: {"new": [...], "total": N, "current_level": "...", "fetched_at": "..."}
Exit codes: 0 ok (``new`` may be empty), 2 the scoreboard could not be read.
Standard library only; no credentials.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

URL = "https://projectnandaitaly.ai/v1/scoreboard"
STATE_DIR = Path(__file__).resolve().parent.parent / ".token-watch"
SEEN = STATE_DIR / "seen.json"


def fetch() -> dict:
    # curl uses the system trust store; python.org builds on macOS often ship without one.
    try:
        done = subprocess.run(
            ["curl", "-sfL", "-m", "20", "-A", "token-watch/1.0", URL],
            capture_output=True,
            check=True,
            timeout=30,
        )
        return json.loads(done.stdout)
    except (OSError, subprocess.SubprocessError):
        pass
    request = urllib.request.Request(URL, headers={"User-Agent": "token-watch/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def key(item: dict) -> str:
    return str(item.get("id") or f"{item.get('at')}|{item.get('kind')}|{item.get('headline')}")


def load_seen() -> set[str]:
    try:
        return set(json.loads(SEEN.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def save_seen(seen: set[str]) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    tmp = SEEN.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    tmp.replace(SEEN)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--peek", action="store_true", help="do not mark new items as seen")
    mode.add_argument("--all", action="store_true", help="print every dispatch")
    args = parser.parse_args()

    try:
        board = fetch()
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(json.dumps({"error": f"scoreboard unreachable: {exc}"}), file=sys.stderr)
        return 2

    press = [item for item in board.get("press") or [] if isinstance(item, dict)]
    press.sort(key=lambda item: str(item.get("at", "")))
    levels = [item.get("headline", "") for item in press if item.get("kind") == "level"]

    if args.all:
        new = press
    else:
        seen = load_seen()
        new = [item for item in press if key(item) not in seen]
        if new and not args.peek:
            save_seen(seen | {key(item) for item in press})

    print(
        json.dumps(
            {
                "new": new,
                "total": len(press),
                "current_level": levels[-1] if levels else None,
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
