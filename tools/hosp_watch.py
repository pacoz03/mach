"""One line every N minutes on the hospital: Supabase episodes + console reputation.

Laptop only (secret key from the repo .env, never printed). Usage:
    python3 -I hosp_watch.py <repo> [minutes]
"""

import collections
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

REPO = sys.argv[1]
EVERY = int(sys.argv[2]) if len(sys.argv) > 2 else 5
env = dict(
    line.split("=", 1) for line in open(f"{REPO}/.env").read().splitlines() if "=" in line and not line.startswith("#")
)
URL, KEY = env["SUPABASE_URL"].strip().strip('"'), env["SUPABASE_SECRET_KEY"].strip().strip('"')
sys.path.insert(0, f"{REPO}/tools")
import nanda_console  # noqa: E402


def episodes(since: datetime) -> list[dict]:
    query = urllib.parse.urlencode(
        {"select": "at,version,direction,peer,act,fields,latency_ms,error", "agent": "eq.hospital",
         "at": f"gte.{since.isoformat()}", "limit": "5000"}
    )
    req = urllib.request.Request(f"{URL}/rest/v1/episodes?{query}", headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def reputation() -> str:
    try:
        _, data = nanda_console.get("team/reputation", nanda_console.session())
        h = next(a for a in data["agents"] if a["family"] == "hospital")
        return f"rep={h['reputation']}/{h['rating_count']} team={data['score']}"
    except Exception as exc:  # console down or cookie expired: say so, keep going
        return f"rep=? ({type(exc).__name__})"


while True:
    since = datetime.now(timezone.utc) - timedelta(minutes=EVERY)
    try:
        rows = episodes(since)
        out = [r for r in rows if r["direction"] == "out"]
        inn = [r for r in rows if r["direction"] == "in"]
        partners = [r for r in inn if r["act"] == "need.request" and str(r["peer"]).startswith("agent:")]
        to_agents = [r for r in out if str(r["peer"]).startswith("agent:")]
        offers = sum(1 for r in to_agents if r["act"] == "offer")
        cancels = collections.Counter((r["fields"] or {}).get("reason", "?") for r in to_agents if r["act"] == "cancel")
        refused = collections.Counter((r["fields"] or {}).get("code", (r["fields"] or {}).get("reason", "?"))
                                      for r in inn if r["act"] == "refused")
        receipts = sum(1 for r in out if r["act"] == "booking.receipt")
        checkins = sum(1 for r in out if r["act"] == "checkin" and r["peer"] == "city:ledger")
        errors = collections.Counter(r["error"] for r in out if r["error"])
        slow = sum(1 for r in to_agents if (r["latency_ms"] or 0) > 8000)
        versions = sorted({r["version"] for r in rows})
        print(
            f"{datetime.now():%H:%M} last{EVERY}m v={','.join(versions) or '-'} in={len(inn)} out={len(out)} "
            f"partner_req={len(partners)} offers={offers} cancels={dict(cancels)} receipts={receipts} "
            f"checkins={checkins} refused={dict(refused)} errors={dict(errors)} slow>8s={slow} {reputation()}",
            flush=True,
        )
    except Exception as exc:
        print(f"{datetime.now():%H:%M} watch error: {type(exc).__name__}: {exc}"[:300], flush=True)
    time.sleep(EVERY * 60)
