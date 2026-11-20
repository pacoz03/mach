"""Sentiment dei messaggi dei nostri agenti e dei commenti dei nostri cittadini.

Tre fonti, una tabella (`sentiments`, schema in shared/sentiment.sql):
- `episode`: righe di `episodes` scritte dagli agenti con shared/tools.py;
- `comment`: commenti pubblici della scoreboard (`/v1/scoreboard` → `comments`) dei nostri
  cittadini, salvati in `citizen_comments` dal collector;
- `journey`: messaggi delle journey della console (backfill, letti dal browser loggato).

La classificazione la fa Claude (skill `sentiment-agenti`); questo script fa solo I/O verso
Supabase con la secret key di .env (mai nei pod). Solo stdlib.

    python3 tools/sentiment.py collect                # ogni 30 s (launchd): salva i commenti nuovi
    python3 tools/sentiment.py pending [--limit 60]   # JSON: episodi e commenti da analizzare
    python3 tools/sentiment.py save < scores.json     # JSON: [{source, ref, score, label, ...}]
    python3 tools/sentiment.py summary [--minutes 60] # media per agente e direzione

Exit 2 = Supabase o scoreboard non raggiungibili, o credenziali mancanti.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCOREBOARD = "https://projectnandaitaly.ai/v1/scoreboard"
ASST_CODE = "-890924"  # ASST Gorla: il codice nei nostri need_id
LABELS = {"positive", "neutral", "negative"}
SOURCES = {"episode", "comment", "journey"}
JOURNEY_FIELDS = ("msg_id", "agent", "direction", "peer", "need_id", "at")


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


def _open(request: urllib.request.Request):
    with urllib.request.urlopen(request, timeout=20, context=_CTX) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def _call(method: str, path: str, body=None, prefer: str | None = None):
    headers = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    data = None if body is None else json.dumps(body).encode()
    return _open(urllib.request.Request(f"{URL}/rest/v1/{path}", data=data, headers=headers, method=method))


def _get(view: str, **params: str) -> list:
    return _call("GET", f"{view}?{urllib.parse.urlencode(params)}") or []


# --- collector ---------------------------------------------------------------------------------


def collect() -> dict:
    board = _open(urllib.request.Request(SCOREBOARD, headers={"User-Agent": "mach-sentiment/1.0"}))
    comments = (board or {}).get("comments") or []
    ours = [c for c in comments if ASST_CODE in (c.get("need_id") or "") and c.get("text")]
    rows = [{"need_id": c["need_id"], "at": c["time"], "citizen_score": c.get("score"), "text": c["text"]}
            for c in ours]
    inserted = []
    if rows:
        inserted = _call("POST", "citizen_comments?on_conflict=need_id,at", rows,
                         prefer="resolution=ignore-duplicates,return=representation") or []
    window = [c["time"] for c in comments]
    return {"seen": len(comments), "ours": len(ours), "new": len(inserted),
            "window": [min(window), max(window)] if window else None}


# --- analisi -----------------------------------------------------------------------------------


def pending(limit: int) -> dict:
    episodes = _get("sentiment_pending", order="at.asc", limit=str(limit),
                    select="episode_id,msg_id,at,agent,direction,peer,peer_kind,need_id,act,fields,body,error")
    comments = _get("comment_pending", order="at.asc", limit=str(limit),
                    select="comment_id,need_id,at,citizen_score,text")
    grouped: dict[str, list] = {}
    for row in episodes:
        grouped.setdefault(row["agent"], []).append(row)
    return {"total": len(episodes) + len(comments), "episodes": grouped, "comments": comments}


def _scored(score: dict) -> dict:
    value = max(-1.0, min(1.0, float(score["score"])))
    label = score.get("label") or ("positive" if value > 0.2 else "negative" if value < -0.2 else "neutral")
    if label not in LABELS:
        raise ValueError(f"label {label!r}")
    confidence = score.get("confidence")
    return {
        "score": round(value, 2),
        "label": label,
        "emotion": score.get("emotion") or None,
        "confidence": None if confidence is None else max(0.0, min(1.0, float(confidence))),
        "reason": (score.get("reason") or "")[:200] or None,
        "model": score.get("model") or "claude",
    }


def _in(view: str, column: str, ids: list[str], select: str) -> dict:
    if not ids:
        return {}
    rows = _get(view, select=select, **{column: f"in.({','.join(ids)})"})
    return {str(r[column]): r for r in rows}


def save(scores: list[dict]) -> dict:
    by_source: dict[str, list[dict]] = {s: [] for s in SOURCES}
    for score in scores:
        source = score.get("source", "episode")
        if source not in SOURCES:
            raise ValueError(f"source {source!r}")
        score["ref"] = str(score.get("ref") or score.get("episode_id") or score.get("comment_id") or score.get("msg_id"))
        by_source[source].append(score)

    episodes = _in("sentiment_pending", "episode_id", [s["ref"] for s in by_source["episode"]],
                   "episode_id,msg_id,at,agent,direction,peer,need_id,body")
    comments = _in("comment_pending", "comment_id", [s["ref"] for s in by_source["comment"]],
                   "comment_id,need_id,at,citizen_score")

    rows, skipped = [], []
    for score in by_source["episode"]:
        e = episodes.get(score["ref"])
        if e is None:  # già analizzato o inesistente
            skipped.append(score["ref"])
            continue
        rows.append({"source": "episode", "source_ref": score["ref"], "episode_id": score["ref"],
                     "msg_id": e["msg_id"], "agent": e["agent"], "direction": e["direction"],
                     "peer": e["peer"], "need_id": e["need_id"], "at": e["at"],
                     "basis": "text" if e.get("body") else "act", **_scored(score)})
    for score in by_source["comment"]:
        c = comments.get(score["ref"])
        if c is None:
            skipped.append(score["ref"])
            continue
        rows.append({"source": "comment", "source_ref": score["ref"], "agent": "team",
                     "direction": "in", "peer": "citizen", "need_id": c["need_id"], "at": c["at"],
                     "citizen_score": c["citizen_score"], "basis": "text", **_scored(score)})
    for score in by_source["journey"]:
        missing = [k for k in JOURNEY_FIELDS if not score.get(k)]
        if missing:
            raise ValueError(f"journey {score['ref']}: mancano {missing}")
        rows.append({"source": "journey", "source_ref": score["ref"],
                     **{k: score[k] for k in JOURNEY_FIELDS},
                     "citizen_score": score.get("citizen_score"), "basis": "text", **_scored(score)})

    saved = []
    if rows:
        saved = _call("POST", "sentiments?on_conflict=source,source_ref", rows,
                      prefer="resolution=ignore-duplicates,return=representation") or []
    done = {r["source_ref"] for r in saved}
    skipped += [r["source_ref"] for r in rows if r["source_ref"] not in done]
    return {"saved": len(saved), "skipped": skipped}


def summary(minutes: int) -> list:
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    return _get("sentiment_by_agent", bucket=f"gte.{since}", order="agent.asc,bucket.desc",
                select="agent,direction,bucket,messages,avg_score,negatives,positives")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("collect")
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
        if args.cmd == "collect":
            out = collect()
        elif args.cmd == "pending":
            out = pending(args.limit)
        elif args.cmd == "save":
            out = save(json.load(sys.stdin))
        else:
            out = summary(args.minutes)
    except (urllib.error.URLError, OSError) as exc:
        detail = exc.read().decode()[:300] if isinstance(exc, urllib.error.HTTPError) else str(exc)
        print(json.dumps({"error": detail, "at": datetime.now(timezone.utc).isoformat()}))
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=None if args.cmd == "collect" else 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
