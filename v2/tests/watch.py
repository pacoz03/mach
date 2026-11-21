"""Sorveglianza dei cinque agenti v2 in città, dal laptop (sola lettura).

    SSL_CERT_FILE=/etc/ssl/cert.pem python3 v2/tests/watch.py [ISO di partenza, es. 2026-11-21T04:16:00Z]

Legge la console (tools/nanda_console.py, cookie nel Portachiavi) e, se c'è la secret key in .env,
gli errori nella memoria condivisa. Per ogni agente: messaggi dei cittadini, risposte, tempi,
messaggi rimasti senza risposta, atti inviati, rifiuti dei cittadini e righe del giudice.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import subprocess
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NAMES = {"agent:ck9w60ejwnvw": "gp", "agent:sbdpbpqz5sxe": "booking_centre", "agent:m6py14a1ej77": "hospital",
         "agent:h6krzxx6es83": "specialist_care", "agent:yg8debxe3x0p": "family_counseling"}
ACTS = ("prescription", "offer", "booking.receipt", "checkin", "report", "cancel", "escalate.emergency", "data.request", "consent", "confirm")


def console(path: str):
    out = subprocess.run([sys.executable, str(REPO / "tools/nanda_console.py"), path], capture_output=True, text=True, timeout=90).stdout
    start = min((i for i in (out.find("{"), out.find("[")) if i >= 0), default=-1)
    return json.loads(out[start:]) if start >= 0 else None


def when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def text_of(message: dict) -> str:
    body = message.get("body")
    return (body.get("text") if isinstance(body, dict) else "") or ""


def act_of(text: str) -> str:
    return next((w for w in text.split() if w in ACTS), "prosa")


def reason_of(text: str) -> str:
    return next((w.split("=", 1)[1].strip(".,;") for w in text.split() if w.startswith("reason=")), "")


def main() -> None:
    since = when(sys.argv[1]) if len(sys.argv) > 1 else when("2026-11-21T04:16:00Z")
    now = datetime.now(UTC)
    listing = console("team/journeys") or []
    rows = listing if isinstance(listing, list) else next((v for v in listing.values() if isinstance(v, list)), [])
    rows = sorted(rows, key=lambda r: str(r.get("updated_at", "")), reverse=True)[:60]
    with cf.ThreadPoolExecutor(8) as pool:
        details = [d for d in pool.map(lambda r: console(f"team/journeys/{r['need_id']}"), rows) if isinstance(d, dict)]
    stats = {name: Counter() for name in NAMES.values()}
    delays, silent, refused, sample = defaultdict(list), [], Counter(), {}
    ratings, states, judged = Counter(), Counter(), 0
    for d in details:
        messages = [m for m in d.get("messages", []) if m.get("sent_at") and when(m["sent_at"]) >= since]
        if not messages:
            continue
        journey = d.get("journey") or {}
        fresh = all(when(m["sent_at"]) >= since for m in d.get("messages", []) if m.get("sent_at"))
        states[journey.get("state", "?")] += 1
        if fresh and d.get("ratings"):
            judged += 1
            for r in d["ratings"]:
                ratings[(NAMES.get(r.get("subject"), "?"), r.get("reason"), r.get("score"))] += 1
        for index, m in enumerate(messages):
            sender, to, text = m.get("from", ""), m.get("to", ""), text_of(m)
            if sender in NAMES and to.startswith("citizen:"):
                name = NAMES[sender]
                stats[name][act_of(text)] += 1
                sample.setdefault((name, act_of(text)), text)
            elif sender.startswith("citizen:") and to in NAMES:
                name = NAMES[to]
                stats[name]["<cittadino"] += 1
                if act_of(text) == "cancel":
                    refused[(name, reason_of(text))] += 1
                reply = next((r for r in messages[index + 1:] if r.get("from") == to and r.get("to") == sender), None)
                if reply:
                    delays[name].append((when(reply["sent_at"]) - when(m["sent_at"])).total_seconds())
                else:
                    age = (now - when(m["sent_at"])).total_seconds()
                    stats[name]["senza risposta"] += 1
                    if age > 300:
                        silent.append((name, int(age), journey.get("need_id", "")[-12:], text[:90]))
    print(f"== v2 in città dal {since:%H:%M:%S}Z (ora {now:%H:%M:%S}Z) · percorsi toccati {sum(states.values())} {dict(states)} · giudicati (tutti v2) {judged}")
    for name, counter in stats.items():
        waits = sorted(delays[name])
        mid = f"mediana {waits[len(waits) // 2]:.0f}s max {waits[-1]:.0f}s" if waits else "nessuna risposta misurata"
        print(f"{name:>17}: {dict(counter)} · {mid}")
    if refused:
        print("rifiuti dei cittadini:", {f"{n}:{r or '-'}": c for (n, r), c in refused.most_common(12)})
    if ratings:
        print("giudice (percorsi interamente v2):", {f"{n}:{r}:{s:+d}": c for (n, r, s), c in sorted(ratings.items(), key=lambda kv: -kv[1])[:16]})
    for name, age, need, text in sorted(silent, key=lambda s: -s[1])[:10]:
        print(f"  SENZA RISPOSTA da {age}s · {name} · …{need} · «{text}»")
    for (name, act), text in sorted(sample.items()):
        print(f"  es. {name}/{act}: {text[:230]!r}")
    errors(since)


def errors(since: datetime) -> None:
    """Gli errori che i brain hanno tracciato (4xx del Relay, timeout), per agente e versione."""
    env = dict(line.strip().split("=", 1) for line in (REPO / ".env").read_text().splitlines() if "=" in line and not line.startswith("#"))
    url, key = env.get("SUPABASE_URL", ""), env.get("SUPABASE_SECRET_KEY", "")
    if not url or not key:
        return
    query = f"{url}/rest/v1/episodes?select=agent,version,act,error,peer_kind&at=gte.{since:%Y-%m-%dT%H:%M:%SZ}&direction=eq.out&limit=4000"
    try:
        request = urllib.request.Request(query, headers={"apikey": key, "Authorization": f"Bearer {key}"})
        rows = json.loads(urllib.request.urlopen(request, timeout=20).read())
    except Exception as exc:  # noqa: BLE001
        print("memoria non raggiungibile:", type(exc).__name__)
        return
    sent, failed = Counter(), Counter()
    for row in rows:
        sent[(row["agent"], row.get("version"))] += 1
        if row.get("error"):
            failed[(row["agent"], row.get("version"), row["error"], row.get("peer_kind"))] += 1
    print("traccia (messaggi inviati per agente e versione):", {f"{a}@{v}": c for (a, v), c in sorted(sent.items())})
    if failed:
        print("ERRORI di invio:", {f"{a}@{v} {e}→{k}": c for (a, v, e, k), c in failed.most_common(12)})


def ledger(since: datetime) -> None:
    """Quante richieste mandiamo al Ledger e dopo quanto risponde, per fasce di 10 minuti: il
    Ledger serve la squadra da una coda sola, e ogni lettura in più ritarda hold e commit."""
    env = dict(line.strip().split("=", 1) for line in (REPO / ".env").read_text().splitlines() if "=" in line and not line.startswith("#"))
    url, key = env.get("SUPABASE_URL", ""), env.get("SUPABASE_SECRET_KEY", "")
    rows: list[dict] = []
    for offset in (0, 1000, 2000, 3000):
        query = (f"{url}/rest/v1/episodes?select=at,agent,direction,act,need_id&at=gte.{since:%Y-%m-%dT%H:%M:%SZ}"
                 f"&peer_kind=eq.ledger&order=at.asc&limit=1000&offset={offset}")
        try:
            request = urllib.request.Request(query, headers={"apikey": key, "Authorization": f"Bearer {key}"})
            page = json.loads(urllib.request.urlopen(request, timeout=25).read())
        except Exception:  # noqa: BLE001
            break
        rows += page
        if len(page) < 1000:
            break
    answers = {"prescription": ("prescription",), "hold": ("held", "refused"), "prescribe": ("prescribed",), "agenda": ("agenda",),
               "checkin": ("checked_in", "refused"), "commit": ("committed", "refused"), "world": ("slots",), "rules": ("rules",)}
    waiting, delays, sent, results = defaultdict(list), defaultdict(list), Counter(), Counter()
    for row in rows:
        slot, case = row["at"][11:15] + "0", (row["agent"], row["need_id"])
        if row["direction"] == "out":
            sent[(slot, "letture" if row["act"] in ("world", "agenda", "rules", "prescription") else "scritture")] += 1
            if row["act"] in answers:
                waiting[(case, row["act"])].append(when(row["at"]))
            continue
        if row["act"] in ("held", "committed", "checked_in", "reported", "refused"):
            results[(slot, row["act"])] += 1
        for op, words in answers.items():
            if row["act"] in words and waiting[(case, op)]:
                delays[slot].append((when(row["at"]) - waiting[(case, op)].pop(0)).total_seconds())
                break
    print("Ledger, per fasce di 10 minuti (UTC):")
    for slot in sorted({s for s, _ in sent} | set(delays)):
        waits = sorted(delays.get(slot, []))
        lag = f"risposte {len(waits)}, ritardo mediano {waits[len(waits) // 2]:.0f}s" if waits else "nessuna risposta abbinata"
        done = {act: n for (s, act), n in sorted(results.items()) if s == slot}
        print(f"  {slot}  letture {sent[(slot, 'letture')]:3}  scritture {sent[(slot, 'scritture')]:3}  · {lag} · esiti {done}")


if __name__ == "__main__":
    main()
    ledger(when(sys.argv[1]) if len(sys.argv) > 1 else when("2026-11-21T04:16:00Z"))
