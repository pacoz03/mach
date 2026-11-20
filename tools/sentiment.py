"""Sentiment dei messaggi dei nostri agenti: legge gli episodi non analizzati e salva i punteggi.

Usato dal job pianificato (skill `sentiment-agenti`). La classificazione la fa Claude; questo
script fa solo I/O verso Supabase con la secret key di .env (mai nei pod). Solo stdlib.

    python3 tools/sentiment.py pending [--limit 60]   # JSON: {agent: [episodi]}
    python3 tools/sentiment.py save < scores.json     # JSON: [{episode_id, score, label, ...}]
    python3 tools/sentiment.py summary [--minutes 60] # media per agente e direzione

Exit 2 = Supabase non raggiungibile o credenziali mancanti.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABELS = {"positive", "neutral", "negative"}
FIELDS = ("episode_id", "msg_id", "agent", "direction", "peer", "need_id", "at")


def _env() -> dict[str, str]:
    env = {}
    path = ROOT / ".env"
    if path.is_file():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    return env


ENV = _env()
URL = ENV.get("SUPABASE_URL", "").rstrip("/")
KEY = ENV.get("SUPABASE_SECRET_KEY", "")
# Python di python.org su macOS spesso non ha CA: in quel caso usa quelle di sistema.
_CTX = ssl.create_default_context()
if not _CTX.get_ca_certs() and Path("/etc/ssl/cert.pem").is_file():
    _CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem")


def _call(method: str, path: str, body=None, prefer: str | None = None):
    headers = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"{URL}/rest/v1/{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=20, context=_CTX) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def pending(limit: int) -> dict:
    query = urllib.parse.urlencode({
        "select": "episode_id,msg_id,at,agent,direction,peer,peer_kind,need_id,act,fields,body,error",
        "order": "at.asc",
        "limit": str(limit),
    })
    rows = _call("GET", f"sentiment_pending?{query}") or []
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row["agent"], []).append(row)
    return {"total": len(rows), "agents": grouped}


def _clean(score: dict, episode: dict) -> dict:
    value = max(-1.0, min(1.0, float(score["score"])))
    label = score.get("label") or ("positive" if value > 0.2 else "negative" if value < -0.2 else "neutral")
    if label not in LABELS:
        raise ValueError(f"label {label!r}")
    row = {k: episode.get(k) for k in FIELDS}
    row.update({
        "score": round(value, 2),
        "label": label,
        "emotion": (score.get("emotion") or None),
        "confidence": None if score.get("confidence") is None else max(0.0, min(1.0, float(score["confidence"]))),
        "basis": "text" if episode.get("body") else "act",
        "reason": (score.get("reason") or "")[:200] or None,
        "model": score.get("model") or "claude",
    })
    return row


def save(scores: list[dict]) -> dict:
    ids = [s["episode_id"] for s in scores]
    if not ids:
        return {"saved": 0}
    query = urllib.parse.urlencode({
        "select": "episode_id,msg_id,at,agent,direction,peer,need_id,body",
        "episode_id": f"in.({','.join(ids)})",
    })
    episodes = {e["episode_id"]: e for e in _call("GET", f"sentiment_pending?{query}") or []}
    rows, skipped = [], []
    for score in scores:
        episode = episodes.get(score["episode_id"])
        if episode is None:  # già analizzato o inesistente
            skipped.append(score["episode_id"])
            continue
        rows.append(_clean(score, episode))
    if rows:
        _call("POST", "sentiments?on_conflict=episode_id", rows,
              prefer="resolution=ignore-duplicates,return=minimal")
    return {"saved": len(rows), "skipped": skipped}


def summary(minutes: int) -> list:
    query = urllib.parse.urlencode({
        "select": "agent,direction,bucket,messages,avg_score,negatives,positives",
        "bucket": f"gte.{_since(minutes)}",
        "order": "agent.asc,bucket.desc",
    })
    return _call("GET", f"sentiment_by_agent?{query}") or []


def _since(minutes: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pending")
    p.add_argument("--limit", type=int, default=60)
    sub.add_parser("save")
    s = sub.add_parser("summary")
    s.add_argument("--minutes", type=int, default=60)
    args = parser.parse_args()

    if not URL or not KEY:
        print(json.dumps({"error": "SUPABASE_URL o SUPABASE_SECRET_KEY mancanti in .env"}))
        return 2
    try:
        if args.cmd == "pending":
            out = pending(args.limit)
        elif args.cmd == "save":
            out = save(json.load(sys.stdin))
        else:
            out = summary(args.minutes)
    except (urllib.error.URLError, OSError) as exc:
        detail = exc.read().decode()[:300] if isinstance(exc, urllib.error.HTTPError) else str(exc)
        print(json.dumps({"error": detail}))
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
