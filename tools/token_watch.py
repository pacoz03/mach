#!/usr/bin/env python3
"""Watch "Il Token della Sera" exactly as the public scoreboard shows it.

The page (https://projectnandaitaly.ai/) polls ``GET /v1/scoreboard`` and renders:

* the **Token della Sera** column from ``press``: only kinds level, arrivals, rule, fault and
  admin; newest first; identical dispatches (same kind, headline and body) shown once; at
  most 40;
* **banners** from ``wave``: a new level (with its title), a points multiplier change
  (2x/3x/4x), a new care rule (``active_rules``, ``rule_id vN``), the rule notice, and the
  city closing with the next level and its start time.

This script mirrors both. It remembers what it already reported in ``.token-watch/``
(git-ignored) and prints only what is new, as JSON.

Usage:
    python3 tools/token_watch.py           # new items, then mark them reported
    python3 tools/token_watch.py --peek    # new items, state untouched
    python3 tools/token_watch.py --all     # the column and wave as the page shows them now

Output: {"new": [...], "wave_changes": [...], "now": {...}, "column_size": N, "fetched_at": ...}
Exit codes: 0 ok, 2 the scoreboard could not be read. Standard library only; no credentials.
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
from zoneinfo import ZoneInfo

URL = "https://projectnandaitaly.ai/v1/scoreboard"
STATE_DIR = Path(__file__).resolve().parent.parent / ".token-watch"
SEEN = STATE_DIR / "seen.json"
WAVE = STATE_DIR / "wave.json"
ROME = ZoneInfo("Europe/Rome")

# What the page renders (scoreboard bundle: kinds set, dedupe key, cap, labels, level titles).
KINDS = ("level", "arrivals", "rule", "fault", "admin")
LABELS = {"level": "Livello", "arrivals": "Arrivi", "rule": "Regola", "fault": "Guasto", "admin": "Admin"}
CAP = 40
LEVELS = {
    1: "Triage dal medico di base",
    2: "Dalla ricetta alla prenotazione",
    3: "Bisogni in linguaggio naturale",
    4: "Scarsità sul ring",
    5: "Vincoli combinati",
    6: "Pressione temporale e riprenotazioni",
    7: "Guasti e contesa",
    8: "Red flag e cambio regola",
}


def fetch() -> dict:
    # curl uses the system trust store; python.org builds on macOS often ship without one.
    try:
        done = subprocess.run(
            ["curl", "-sfL", "-m", "20", "-A", "token-watch/1.1", URL],
            capture_output=True,
            check=True,
            timeout=30,
        )
        return json.loads(done.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    request = urllib.request.Request(URL, headers={"User-Agent": "token-watch/1.1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def rome(value: str | None) -> str:
    """HH:MM in Europe/Rome, as the page prints times."""

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return parsed.astimezone(ROME).strftime("%H:%M")


def page_key(item: dict) -> str:
    """The page shows one dispatch per kind + headline + body (trimmed)."""

    return f"{item.get('kind')}\n{str(item.get('headline', '')).strip()}\n{str(item.get('body', '')).strip()}"


def column(press: list) -> list[dict]:
    """The Token della Sera column exactly as rendered: newest first, filtered, deduped."""

    seen: set[str] = set()
    shown: list[dict] = []
    for item in reversed(press or []):
        if not isinstance(item, dict) or item.get("kind") not in KINDS:
            continue
        key = page_key(item)
        if key in seen:
            continue
        seen.add(key)
        shown.append(
            {
                "time": rome(item.get("at")),
                "at": item.get("at"),
                "kind": item.get("kind"),
                "label": LABELS[item["kind"]],
                "headline": str(item.get("headline", "")).strip(),
                "body": str(item.get("body", "")).strip(),
                "key": key,
            }
        )
        if len(shown) >= CAP:
            break
    return shown


def multiplier(wave: dict) -> int:
    value = wave.get("multiplier")
    return value if value in (3, 4) else 2


def snapshot(wave: dict) -> dict:
    level = wave.get("level")
    return {
        "wave_id": wave.get("wave_id"),
        "state": wave.get("state"),
        "level": level,
        "level_title": LEVELS.get(level, "") if isinstance(level, int) else "",
        "multiplier": multiplier(wave),
        "active_rules": sorted(
            f"{rule.get('rule_id')} v{rule.get('version')}"
            for rule in wave.get("active_rules") or []
            if isinstance(rule, dict)
        ),
        "rule_notice": wave.get("rule_notice") or "",
        "next_level": wave.get("next_level"),
        "next_at": rome(wave.get("next_at")) if wave.get("next_at") else "",
        "final": bool(wave.get("final")),
    }


def wave_changes(before: dict | None, now: dict) -> list[dict]:
    """The banners the page would raise between two wave snapshots."""

    if not before:
        return []
    changes: list[dict] = []
    if now["level"] != before.get("level") and now["level"] is not None:
        changes.append(
            {"kind": "wave.level", "title": f"Livello {now['level']}", "detail": now["level_title"]}
        )
    if now["multiplier"] != before.get("multiplier"):
        changes.append(
            {
                "kind": "wave.multiplier",
                "title": f"{now['multiplier']}x",
                "detail": f"I punti dei percorsi valgono {now['multiplier']} volte",
            }
        )
    for rule in now["active_rules"]:
        if rule not in before.get("active_rules", []):
            changes.append({"kind": "wave.rule", "title": "Nuova regola di cura", "detail": f"{rule} è ora in vigore"})
    for rule in before.get("active_rules", []):
        if rule not in now["active_rules"]:
            changes.append({"kind": "wave.rule_removed", "title": "Regola ritirata", "detail": rule})
    if now["rule_notice"] and now["rule_notice"] != before.get("rule_notice"):
        changes.append({"kind": "wave.notice", "title": "Avviso regole", "detail": now["rule_notice"]})
    if now["state"] != before.get("state"):
        detail = now["state"] or ""
        if now["state"] == "closed" and now["next_level"] is not None:
            detail = f"Livello {now['next_level']} inizia alle {now['next_at'] or '?'}"
        changes.append({"kind": "wave.state", "title": f"Città: {now['state']}", "detail": detail})
    if now["final"] and not before.get("final"):
        changes.append({"kind": "wave.final", "title": "Classifica finale", "detail": "L'evento è concluso"})
    return changes


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save(path: Path, value) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--peek", action="store_true", help="do not mark new items as reported")
    mode.add_argument("--all", action="store_true", help="print the column and wave as shown now")
    args = parser.parse_args()

    try:
        board = fetch()
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(json.dumps({"error": f"scoreboard unreachable: {exc}"}), file=sys.stderr)
        return 2

    shown = column(board.get("press") or [])
    now = snapshot(board.get("wave") or {})

    if args.all:
        new, changes = shown, []
    else:
        # Keys are the page's dedupe keys, so a re-published identical dispatch is not new.
        # Older state files held press ids; anything not matching is simply treated as unseen once.
        seen = set(load(SEEN, []))
        new = [item for item in shown if item["key"] not in seen]
        before = load(WAVE, None)
        changes = wave_changes(before, now)
        if not args.peek:
            if new:
                save(SEEN, sorted(seen | {item["key"] for item in shown}))
            if before != now:
                save(WAVE, now)

    for item in new:
        item.pop("key", None)
    print(
        json.dumps(
            {
                "new": new,
                "wave_changes": changes,
                "now": now,
                "column_size": len(shown),
                "fetched_at": datetime.now(ROME).isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
