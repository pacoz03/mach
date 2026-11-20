#!/usr/bin/env python3
"""Read the Milano Agentic City console API from the terminal.

The session cookie (``__Host-city_session``) is read from the macOS Keychain, never from a
file in this repo. Store it once, pasting the value at the prompt (it stays out of shell
history):

    security add-generic-password -a "$USER" -s nanda-console -U -w

Examples:

    python3 tools/nanda_console.py                     # summary of every endpoint
    python3 tools/nanda_console.py team/wave           # one endpoint, full JSON
    python3 tools/nanda_console.py team/journeys --watch 30

The endpoints are the console's own, undocumented: they may change during the race.
Use them only for your own ASST, journeys and agents.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "https://projectnandaitaly.ai/v1/"
COOKIE = "__Host-city_session"
KEYCHAIN_SERVICE = "nanda-console"
PUBLIC = ("scoreboard", "leaderboard")
PRIVATE = (
    "team",
    "team/wave",
    "team/journeys",
    "team/world",
    "team/reputation",
    "team/usage",
    "team/profile",
    "agents",
)


def session() -> str:
    """The cookie value: NANDA_SESSION if set, else the Keychain item."""

    value = os.environ.get("NANDA_SESSION", "").strip()
    if value:
        return value
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-a", os.environ.get("USER", ""), "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    return out.stdout.strip()


def get(path: str, cookie: str) -> tuple[int, object]:
    headers = {"Accept": "application/json", "User-Agent": "nanda-console-cli"}
    if cookie and path not in PUBLIC:
        headers["Cookie"] = f"{COOKIE}={cookie}"
    request = urllib.request.Request(BASE + path.strip("/"), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        return error.code, None
    except urllib.error.URLError as error:
        return 0, str(error.reason)
    except (TimeoutError, OSError, ValueError) as error:  # slow endpoint or a non-JSON body
        return 0, f"{type(error).__name__}: {error}"


def shape(body: object) -> str:
    if isinstance(body, dict):
        return "{" + ", ".join(list(body)[:10]) + ("…" if len(body) > 10 else "") + "}"
    if isinstance(body, list):
        return f"[{len(body)} elementi]"
    return repr(body)[:80]


def summary(cookie: str) -> None:
    for path in PUBLIC + PRIVATE:
        status, body = get(path, cookie)
        note = " (sessione scaduta o assente)" if status == 401 else ""
        print(f"{status:>3}  /v1/{path:<18} {shape(body) if status == 200 else ''}{note}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", help="es. team/wave; senza argomenti stampa un riepilogo")
    parser.add_argument("--watch", type=int, metavar="SECONDI", help="ripete ogni N secondi")
    args = parser.parse_args()

    cookie = session()
    if not cookie:
        print("Nessuna sessione: solo gli endpoint pubblici risponderanno.", file=sys.stderr)
    while True:
        if args.path:
            status, body = get(args.path, cookie)
            if status == 401:
                print("401: cookie scaduto o assente. Ricopialo dalla console nel Portachiavi.", file=sys.stderr)
                return 1
            print(json.dumps(body, ensure_ascii=False, indent=2) if status == 200 else f"HTTP {status} {body or ''}")
        else:
            summary(cookie)
        if not args.watch:
            return 0
        time.sleep(args.watch)
        print(f"\n--- {time.strftime('%H:%M:%S')} ---")


if __name__ == "__main__":
    sys.exit(main())
