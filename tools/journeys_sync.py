#!/usr/bin/env python3
"""Copy the console's journeys (verdict, rubric rows, review) into Supabase for the evals.

    python3 tools/journeys_sync.py            # new or changed journeys, and judged ones still awaiting a review
    python3 tools/journeys_sync.py --all      # every journey again
    python3 tools/journeys_sync.py --schema   # also apply shared/evals.sql (Management API)

Writes ``journeys`` (one row per need, transcript cleaned: text only of our own agents, never
the citizen's text or personal fields) and ``outcomes`` (one row per rubric row and per review
vote: ``review.collaboration``, ``review.proactivity``, ``review.domain``). Idempotent: re-run
it whenever you like. Laptop only: it needs the console cookie and the Supabase secret key.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import nanda_console  # noqa: E402
import sentiment  # noqa: E402  - .env, CA bundle and the Supabase REST helper

# Personal fields the citizen may reveal: never stored, not even as a token.
PERSONAL = {
    "address", "street", "age", "tax_code", "disability", "occupation", "car", "car_euro_class",
    "name", "surname", "phone", "email", "birth_date", "dob",
}
REVIEW = ("collaboration", "proactivity", "domain")


def console(path: str, cookie: str):
    request = urllib.request.Request(
        nanda_console.BASE + path,
        headers={
            "Accept": "application/json",
            "User-Agent": "nanda-console-cli",
            "Cookie": f"{nanda_console.COOKIE}={cookie}",
        },
    )
    with urllib.request.urlopen(request, timeout=120, context=sentiment._CTX) as response:
        return json.load(response)


def sql(query: str):
    """Run SQL through the Supabase Management API (DDL: the REST API cannot)."""

    ref = sentiment.URL.split("//", 1)[1].split(".", 1)[0]
    token = sentiment.ENV.get("SUPABASE_ACCESS_TOKEN", "")
    request = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{ref}/database/query",
        data=json.dumps({"query": query}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "mach-cli"},
        method="POST",
    )
    return sentiment._open(request)


def tokens(text: str) -> dict[str, str]:
    out = {}
    for word in text.split():
        if "=" in word and not word.startswith("="):
            key, value = word.split("=", 1)
            key = key.strip(".,;:()").lower()
            if key and key.replace("_", "").replace(".", "").isalnum():
                out[key] = "·" if key in PERSONAL else value.strip(".,;:()")
    return out


def role_of(agent_id: str, ours: dict[str, str]) -> str:
    if agent_id in ours:
        return ours[agent_id]
    if agent_id.startswith("citizen:"):
        return "citizen"
    if agent_id.startswith("city:"):
        return agent_id
    return "partner"


def transcript(messages: list[dict], ours: dict[str, str]) -> list[dict]:
    rows = []
    for m in sorted(messages, key=lambda m: m.get("sent_at", "")):
        body = m.get("body") or {}
        text = body.get("text", "") if isinstance(body, dict) else ""
        sender = m.get("from", "")
        row = {
            "msg_id": m.get("msg_id"),
            "at": m.get("sent_at"),
            "from": role_of(sender, ours),
            "to": role_of(m.get("to", ""), ours),
            "type": m.get("type"),
            "act": text.split(None, 1)[0] if text else "",
            "tokens": tokens(text),
        }
        if role_of(sender, ours) == "partner":
            row["agent_id"] = sender
        if role_of(m.get("to", ""), ours) == "partner":
            row["to_agent_id"] = m.get("to")
        if sender in ours:
            row["text"] = text  # only our own words
        if isinstance(body, dict) and body.get("status"):
            row["status"] = body["status"]
        rows.append(row)
    return rows


def journey_row(detail: dict, ours: dict[str, str], updated_at: str) -> dict:
    j = detail.get("journey") or {}
    verdict = detail.get("verdict") or {}
    msgs = transcript(detail.get("messages") or [], ours)
    facts = []
    for fact in verdict.get("facts") or []:
        facts.append({**fact, "role": role_of(fact.get("subject", ""), ours)})
    return {
        "need_id": j["need_id"],
        "wave_id": j.get("wave_id"),
        "level": j.get("level"),
        "citizen": j.get("citizen"),
        "state": j.get("state"),
        "score": j.get("score"),
        "verdict_at": verdict.get("time"),
        "answer_set": verdict.get("answer_set"),
        "facts": facts,
        "rule_versions": verdict.get("rule_versions") or [],
        "transcript": msgs,
        "first_at": msgs[0]["at"] if msgs else None,
        "last_at": msgs[-1]["at"] if msgs else None,
        "n_messages": len(msgs),
        "updated_at": updated_at,
    }


def outcome_rows(detail: dict, ours: dict[str, str]) -> list[dict]:
    j = detail.get("journey") or {}
    need = j.get("need_id")
    verdict = detail.get("verdict") or {}
    at = verdict.get("time")
    ratings = list(detail.get("ratings") or [])
    out = []
    for row in verdict.get("rows") or []:
        match = next(
            (
                r for r in ratings
                if r.get("subject") == row.get("subject") and r.get("rater") == row.get("rater")
                and r.get("score") == row.get("score") and r.get("reason") == row.get("reason")
            ),
            None,
        )
        if match:
            ratings.remove(match)
        rid = match["record_id"] if match else f"{verdict.get('record_id')}:{row.get('row')}:{row.get('subject')}"
        out.append({
            "record_id": rid,
            "need_id": need,
            "citizen": j.get("citizen"),
            "agent": role_of(row.get("subject", ""), ours),
            "rater": row.get("rater"),
            "fact": row.get("row") or row.get("reason") or "?",
            "points": row.get("score", 0),
            "reason": row.get("reason"),
            "at": at,
            "raw": {**row, "cancelled": bool(match and match.get("cancelled"))},
        })
    for r in ratings:  # a rating the verdict does not list (fast reply, audit, ...)
        out.append({
            "record_id": r["record_id"],
            "need_id": need,
            "citizen": j.get("citizen"),
            "agent": role_of(r.get("subject", ""), ours),
            "rater": r.get("rater"),
            "fact": r.get("reason") or "?",
            "points": r.get("score", 0),
            "reason": r.get("reason"),
            "at": at,
            "raw": r,
        })
    review = detail.get("review") or {}
    for agent in review.get("agents") or []:
        for dim in REVIEW:
            if agent.get(dim) is None:
                continue
            out.append({
                "record_id": f"{review.get('record_id')}:{agent.get('agent_id')}:{dim}",
                "need_id": need,
                "citizen": j.get("citizen"),
                "agent": role_of(agent.get("agent_id", ""), ours),
                "rater": "city:review",
                "fact": f"review.{dim}",
                "points": agent.get(dim),
                "reason": (agent.get("notes") or {}).get(dim),
                "at": at,
                "raw": {"agent_id": agent.get("agent_id"), "evidence": (agent.get("evidence") or {}).get(dim)},
            })
    return out


def upsert(table: str, rows: list[dict], key: str) -> None:
    for i in range(0, len(rows), 200):
        sentiment._call(
            "POST",
            f"{table}?on_conflict={key}",
            rows[i:i + 200],
            prefer="resolution=merge-duplicates,return=minimal",
        )


# The review (city:review) lands 5-6 hours after the verdict (21/11): a judged journey without
# one is read again while it is this recent, so its votes reach ``outcomes``.
REVIEW_WAIT = timedelta(hours=12)


def _rows(query: str) -> list[dict]:
    """Every row of a PostgREST query (it returns at most 1000 per call)."""

    rows, offset = [], 0
    while True:
        page = sentiment._call("GET", f"{query}&limit=1000&offset={offset}") or []
        rows += page
        offset += 1000
        if len(page) < 1000:
            return rows


def _awaiting_review(known: dict[str, dict]) -> list[dict]:
    """Journeys judged in the last REVIEW_WAIT that have no review vote yet."""

    since = (datetime.now(timezone.utc) - REVIEW_WAIT).strftime("%Y-%m-%dT%H:%M:%S")
    reviewed = {row["need_id"] for row in _rows(
        f"outcomes?select=need_id&fact=like.review.*&at=gte.{since}")}
    return [
        {"need_id": need, "updated_at": row["updated_at"]}
        for need, row in known.items()
        if row["verdict_at"] and row["verdict_at"] >= since and need not in reviewed
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="re-read every journey")
    parser.add_argument("--schema", action="store_true", help="apply shared/evals.sql first")
    args = parser.parse_args()

    if args.schema:
        sql((HERE.parent / "shared" / "evals.sql").read_text())
    cookie = nanda_console.session()
    if not cookie:
        print(json.dumps({"error": "cookie della console mancante nel Keychain (nanda-console)"}))
        return 2
    ours = {a["agent_id"]: a["name"] for a in console("agents", cookie)["agents"]}
    listed = console("team/journeys", cookie)["journeys"]
    known = {row["need_id"]: row for row in _rows("journeys?select=need_id,updated_at,verdict_at")}
    todo = [
        j for j in listed
        if args.all or j["need_id"] not in known
        or known[j["need_id"]]["updated_at"] != j.get("updated_at")
        or known[j["need_id"]]["verdict_at"] is None
    ]
    queued = {j["need_id"] for j in todo}
    late = [j for j in _awaiting_review(known) if j["need_id"] not in queued]
    todo += late

    def fetch(j):
        try:
            return j, console("team/journeys/" + urllib.parse.quote(j["need_id"]), cookie)
        except Exception as exc:  # noqa: BLE001 - one slow journey does not stop the rest
            return j, exc

    journeys, outcomes, errors = [], [], []
    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        for j, detail in pool.map(fetch, todo):
            if isinstance(detail, Exception):
                errors.append(f"{j['need_id']}: {detail}")
                continue
            journeys.append(journey_row(detail, ours, j.get("updated_at", "")))
            outcomes.extend(outcome_rows(detail, ours))
    upsert("journeys", journeys, "need_id")
    upsert("outcomes", outcomes, "record_id")
    print(json.dumps({
        "listed": len(listed), "awaiting_review": len(late), "fetched": len(journeys),
        "outcomes": len(outcomes), "reviews": sum(o["fact"].startswith("review.") for o in outcomes),
        "errors": errors[:5],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
