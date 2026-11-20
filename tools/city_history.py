"""Messaggi reali dei nostri agenti, letti dalla console di squadra. Solo lettura.

La console (https://projectnandaitaly.ai/team, tab Journeys) espone le journey della squadra:

    GET /v1/team/journeys            -> {"journeys": [{need_id, state, level, updated_at, ...}]}
    GET /v1/team/journeys/{need_id}  -> {"messages": [{msg_id, type, from, to, sent_at, body}], ...}

Sono GET su documenti della console: non toccano la inbox del Relay, quindi nessun ack e nessun
messaggio tolto agli agenti in produzione. Non serve (e non si usa) la chiave dell'agente.

Auth: il cookie di sessione della console (login via email), copiato dal browser loggato:
DevTools -> Network -> una richiesta /v1/team/... -> Request Headers -> `cookie`. Passalo così,
senza salvarlo nel repo (lo script non lo stampa e non scrive file):

    export CITY_CONSOLE_COOKIE='...'      # o: CITY_CONSOLE_COOKIE="$(cat ~/.config/city/console.cookie)"
    ~/.local/share/uv/tools/city-cli/bin/python tools/city_history.py --peer city:ledger
    ... --agent booking_centre -n 40 --journeys 60     # più storia, un solo agente
    ... --json                                         # una riga JSON per messaggio (body intero)

Output su stdout: per agente le ultime N righe (sender, recipient, type, need_id, testo troncato).
Contiene testo dei cittadini: non redirigerlo in file dentro il repo.
Exit 2 = console non raggiungibile o cookie mancante/scaduto.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://projectnandaitaly.ai"
FOLDERS = ("gp", "booking_centre", "hospital", "specialist_care", "family_counseling")
_CTX = ssl.create_default_context()
if not _CTX.get_ca_certs() and Path("/etc/ssl/cert.pem").is_file():
    _CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem")


def agents() -> dict[str, str]:
    """agent_id -> cartella, dalle card del repo."""
    out = {}
    for folder in FOLDERS:
        card = ROOT / folder / "card.yaml"
        found = re.search(r"^agent_id:\s*(\S+)", card.read_text(encoding="utf-8"), re.M) if card.is_file() else None
        if found:
            out[found.group(1)] = folder
    return out


def get(path: str, cookie: str):
    request = urllib.request.Request(BASE + path, headers={"Cookie": cookie, "User-Agent": "city-history/1.0"})
    with urllib.request.urlopen(request, timeout=20, context=_CTX) as response:
        return json.load(response)


def rows(journeys: list[dict], details: list[dict], ours: dict[str, str]) -> list[dict]:
    """Un dict per messaggio che tocca un nostro agente, con l'agente e l'interlocutore."""
    out = []
    for journey, detail in zip(journeys, details):
        for m in detail.get("messages") or []:
            sender, to = m.get("from", ""), m.get("to", "")
            body = m.get("body") or {}
            for me, peer in ((sender, to), (to, sender)):
                if me in ours:
                    out.append({"agent": ours[me], "peer": peer, "sender": sender, "recipient": to,
                                "type": m.get("type", ""), "need_id": journey.get("need_id", ""),
                                "sent_at": m.get("sent_at", ""), "msg_id": m.get("msg_id", ""),
                                "text": body.get("text", "") if isinstance(body, dict) else "", "body": body})
                    break
    return out


def pick(found: list[dict], agent: str | None, peer: str | None, n: int) -> dict[str, list[dict]]:
    """Le ultime n righe per agente, in ordine di invio, filtrate per agente e interlocutore."""
    by: dict[str, list[dict]] = {}
    for r in sorted(found, key=lambda r: (r["sent_at"], r["msg_id"])):
        if (agent is None or r["agent"] == agent) and (peer is None or r["peer"] == peer):
            by.setdefault(r["agent"], []).append(r)
    return {a: rs[-n:] for a, rs in by.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--agent", choices=FOLDERS, help="solo questo agente (cartella)")
    ap.add_argument("--peer", help="solo messaggi con questo interlocutore, es. city:ledger")
    ap.add_argument("-n", type=int, default=20, help="righe per agente (default 20)")
    ap.add_argument("--journeys", type=int, default=30, help="journey più recenti da leggere (default 30)")
    ap.add_argument("--width", type=int, default=300, help="caratteri di testo per riga (default 300)")
    ap.add_argument("--json", action="store_true", help="una riga JSON per messaggio, con il body intero")
    args = ap.parse_args()
    cookie = os.environ.get("CITY_CONSOLE_COOKIE", "").strip()
    if not cookie:
        print("CITY_CONSOLE_COOKIE mancante: vedi il docstring (cookie della console dal browser).", file=sys.stderr)
        return 2
    try:
        listed = sorted(get("/v1/team/journeys", cookie).get("journeys") or [],
                        key=lambda j: j.get("updated_at") or "", reverse=True)[: args.journeys]
        details = [get("/v1/team/journeys/" + urllib.parse.quote(j["need_id"], safe=""), cookie) for j in listed]
    except urllib.error.HTTPError as exc:
        hint = " (cookie mancante o scaduto: rifai il login sulla console)" if exc.code in (401, 403) else ""
        print(f"console: HTTP {exc.code}{hint}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"console non raggiungibile: {type(exc).__name__}", file=sys.stderr)
        return 2
    chosen = pick(rows(listed, details, agents()), args.agent, args.peer, args.n)
    for folder, found in chosen.items():
        if not args.json:
            print(f"== {folder} ({len(found)})")
        for r in found:
            if args.json:
                print(json.dumps({k: v for k, v in r.items() if k != "text"}, ensure_ascii=False))
                continue
            text = " ".join(r["text"].split())
            text = text if len(text) <= args.width else text[: args.width - 1] + "…"
            print(f"{r['sent_at'][11:19]} {r['sender']} -> {r['recipient']} [{r['type']}] {r['need_id']}\n    {text}")
    if not chosen:
        print(f"nessun messaggio nelle ultime {len(listed)} journey con questi filtri", file=sys.stderr)
    return 0


def _check() -> None:
    ours = {"agent:a": "gp", "agent:b": "booking_centre"}
    journeys = [{"need_id": "N-1"}]
    details = [{"messages": [
        {"msg_id": "1", "type": "chat", "from": "agent:a", "to": "city:ledger", "sent_at": "t1", "body": {"text": "prescribe x=1"}},
        {"msg_id": "2", "type": "chat", "from": "city:ledger", "to": "agent:a", "sent_at": "t2", "body": {"text": "prescribed"}},
        {"msg_id": "3", "type": "chat", "from": "citizen:X", "to": "agent:b", "sent_at": "t3", "body": {"text": "ciao"}},
        {"msg_id": "4", "type": "chat", "from": "citizen:X", "to": "agent:zzz", "sent_at": "t4", "body": {"text": "altri"}},
    ]}]
    got = pick(rows(journeys, details, ours), None, "city:ledger", 1)
    assert list(got) == ["gp"] and got["gp"][0]["text"] == "prescribed", got
    assert len(pick(rows(journeys, details, ours), None, None, 9)["gp"]) == 2
    assert pick(rows(journeys, details, ours), "booking_centre", None, 9)["booking_centre"][0]["peer"] == "citizen:X"


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-check"]:
        _check()
        print("ok")
        sys.exit(0)
    sys.exit(main())
