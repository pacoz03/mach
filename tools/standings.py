#!/usr/bin/env python3
"""Classifica: storico e confronto con le altre squadre (sola lettura).

    SSL_CERT_FILE=/etc/ssl/cert.pem python3 tools/standings.py              # foto + rapporto
    SSL_CERT_FILE=/etc/ssl/cert.pem python3 tools/standings.py report       # solo rapporto
    SSL_CERT_FILE=/etc/ssl/cert.pem python3 tools/standings.py seed FILE    # aggiunge una foto salvata

Ogni foto va in .standings/history.jsonl (gitignored): ora, livello, moltiplicatore e, per ogni
squadra, il punteggio e reputazione e numero di valutazioni di ogni ruolo. Le righe di rubrica dei
nostri percorsi giudicati (console team/journeys) vanno in .standings/ratings.jsonl, una per
record_id, con l'ora in cui le abbiamo viste. Il rapporto dice dove guadagniamo e dove perdiamo
terreno: per ruolo rispetto alla mediana e ai primi cinque, e per riga di rubrica.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOME = REPO / ".standings"
HISTORY, RATINGS, SEEN = HOME / "history.jsonl", HOME / "ratings.jsonl", HOME / "journeys.json"
US = "the-promptfather-s"
OURS = {"agent:ck9w60ejwnvw": "gp", "agent:sbdpbpqz5sxe": "booking_centre", "agent:m6py14a1ej77": "hospital",
        "agent:h6krzxx6es83": "specialist_care", "agent:yg8debxe3x0p": "family_counseling"}
ROLES = ("gp", "booking_centre", "hospital", "specialist_care", "family_counseling")
SHORT = {"gp": "gp", "booking_centre": "centro", "hospital": "ospedale", "specialist_care": "specialista",
         "family_counseling": "consultorio"}


def console(path: str):
    out = subprocess.run([sys.executable, str(REPO / "tools/nanda_console.py"), path],
                         capture_output=True, text=True, timeout=120).stdout
    start = min((i for i in (out.find("{"), out.find("[")) if i >= 0), default=-1)
    try:
        return json.loads(out[start:]) if start >= 0 else None
    except ValueError:
        return None


def when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def photo(board: dict) -> dict:
    """Una foto della scoreboard: ciò che serve a confrontarci, niente commenti né serie."""
    wave = board.get("wave") or {}
    teams = {}
    for team in board.get("teams") or []:
        roles = {a.get("family", ""): [a.get("reputation") or 0, a.get("rating_count") or 0] for a in team.get("agents") or []}
        teams[team["team"]] = {"score": team.get("score") or 0, "roles": roles, "unserved": team.get("unserved") or 0}
    return {"at": board.get("updated_at") or datetime.now(UTC).isoformat(), "level": wave.get("level"),
            "multiplier": wave.get("multiplier"), "teams": teams}


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def append(path: Path, rows: list[dict]) -> None:
    HOME.mkdir(exist_ok=True)
    with path.open("a") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")


def snapshot() -> None:
    board = console("scoreboard")
    if not isinstance(board, dict) or not board.get("teams"):
        print("scoreboard non raggiungibile: nessuna foto")
    else:
        shot = photo(board)
        if not any(h["at"] == shot["at"] for h in load(HISTORY)[-3:]):
            append(HISTORY, [shot])
    collect_ratings()


def collect_ratings() -> None:
    """Le righe di rubrica dei nostri percorsi giudicati: si rileggono solo i percorsi chiusi il cui
    stato o punteggio è cambiato dall'ultimo giro (.standings/journeys.json). Al primo giro le righe
    sono segnate ``backfill``: erano già lì, non sono "nuove" in nessuna finestra."""
    first = not SEEN.exists()
    index = json.loads(SEEN.read_text()) if SEEN.exists() else {}
    seen = {r["record_id"] for r in load(RATINGS)}
    listing = console("team/journeys")
    rows = listing if isinstance(listing, list) else next((v for v in (listing or {}).values() if isinstance(v, list)), [])
    due = [r for r in rows if r.get("state") != "open" and index.get(r["need_id"]) != [r.get("state"), r.get("score")]]
    with cf.ThreadPoolExecutor(8) as pool:
        details = list(pool.map(lambda r: (r, console(f"team/journeys/{r['need_id']}")), due))
    now, fresh = datetime.now(UTC).isoformat(), []
    for row, d in details:
        if not isinstance(d, dict):
            continue
        index[row["need_id"]] = [row.get("state"), row.get("score")]
        journey = d.get("journey") or {}
        for r in d.get("ratings") or []:
            if r.get("record_id") in seen or r.get("cancelled"):
                continue
            seen.add(r.get("record_id"))
            fresh.append({"record_id": r.get("record_id"), "seen": now, "need_id": journey.get("need_id"),
                          "level": journey.get("level"), "role": OURS.get(r.get("subject"), "altri"),
                          "reason": r.get("reason"), "score": r.get("score") or 0, "backfill": first})
    if fresh:
        append(RATINGS, fresh)
    HOME.mkdir(exist_ok=True)
    SEEN.write_text(json.dumps(index))


def rank(values: dict[str, float], team: str) -> int:
    ordered = sorted(values, key=lambda t: -values[t])
    return ordered.index(team) + 1 if team in ordered else 0


def report(window_min: int) -> None:
    history = load(HISTORY)
    if not history:
        print("storico vuoto: lancia prima una foto")
        return
    last = history[-1]
    teams = last["teams"]
    scores = {t: v["score"] for t, v in teams.items()}
    ordered = sorted(scores, key=lambda t: -scores[t])
    our = scores.get(US, 0)
    pos = ordered.index(US) + 1
    print(f"== {when(last['at']):%H:%M}Z · livello {last.get('level')} · moltiplicatore {last.get('multiplier')} · "
          f"noi {pos}° di {len(ordered)} con {our}")
    for label, place in (("1°", 1), ("5°", 5), ("10°", 10)):
        print(f"   distacco dal {label} ({ordered[place - 1]}): {scores[ordered[place - 1]] - our:+d}")
    if pos > 1:
        print(f"   squadra sopra: {ordered[pos - 2]} a {scores[ordered[pos - 2]] - our:+d} · sotto: "
              f"{ordered[pos] if pos < len(ordered) else '-'} a {our - scores[ordered[pos]] if pos < len(ordered) else 0:+d}")

    print("\nandamento (noi):")
    marks, prev_hour = [], None
    for shot in history:
        hour = when(shot["at"]).strftime("%H")
        if hour != prev_hour or shot is last:
            marks.append(shot)
            prev_hour = hour
    for shot in marks[-12:]:
        sc = {t: v["score"] for t, v in shot["teams"].items()}
        if US in sc:
            top = max(sc.values())
            print(f"   {when(shot['at']):%H:%M}Z  {rank(sc, US):2d}°  {sc[US]:5d}  dal 1° {top - sc[US]:+5d}  livello {shot.get('level')}")

    print("\nper ruolo, ultima foto (reputazione · valutazioni · media per valutazione · posizione nel ruolo):")
    for role in ROLES:
        reps = {t: v["roles"].get(role, [0, 0])[0] for t, v in teams.items()}
        counts = {t: v["roles"].get(role, [0, 0])[1] for t, v in teams.items()}
        mine, n = reps.get(US, 0), counts.get(US, 0)
        best = max(reps, key=reps.get)
        top5 = sorted(reps.values(), reverse=True)[:5]
        print(f"   {SHORT[role]:>11}: {mine:6d} · {n:5d} · {mine / n if n else 0:+.2f} · {rank(reps, US):2d}°   "
              f"mediana {statistics.median(reps.values()):6.0f} · media primi 5 {sum(top5) / 5:6.0f} · "
              f"migliore {best} {reps[best]}")

    start = window_start(history, window_min)
    if start is None:
        print(f"\nnessuna foto di {window_min} minuti fa: il confronto nel tempo partirà dalle prossime foto")
    else:
        compare(start, last)
    rubric(when(start["at"]) if start else when(last["at"]) - timedelta(minutes=window_min))


def window_start(history: list[dict], window_min: int) -> dict | None:
    """La foto più vicina a ``window_min`` minuti prima dell'ultima (almeno metà finestra prima)."""
    last = when(history[-1]["at"])
    target = last - timedelta(minutes=window_min)
    older = [h for h in history[:-1] if last - when(h["at"]) >= timedelta(minutes=window_min / 2)]
    return min(older, key=lambda h: abs(when(h["at"]) - target)) if older else None


def compare(start: dict, last: dict) -> None:
    """Quanto ha guadagnato ogni nostro ruolo nella finestra, contro la mediana e i primi cinque."""
    minutes = (when(last["at"]) - when(start["at"])).total_seconds() / 60
    both = [t for t in last["teams"] if t in start["teams"]]
    d_score = {t: last["teams"][t]["score"] - start["teams"][t]["score"] for t in both}
    top5 = sorted(both, key=lambda t: -last["teams"][t]["score"])[:5]
    print(f"\nnegli ultimi {minutes:.0f} minuti (dalle {when(start['at']):%H:%M}Z):")
    print(f"   punteggio: noi {d_score.get(US, 0):+d} · mediana {statistics.median(d_score.values()):+.0f} · "
          f"media primi 5 {sum(d_score[t] for t in top5) / len(top5):+.0f} · "
          f"posizione {rank({t: start['teams'][t]['score'] for t in both}, US)}° → {rank({t: last['teams'][t]['score'] for t in both}, US)}°")
    rows = []
    for role in ROLES:
        def gain(team: str) -> tuple[int, int]:
            new, old = last["teams"][team]["roles"].get(role, [0, 0]), start["teams"][team]["roles"].get(role, [0, 0])
            return new[0] - old[0], new[1] - old[1]
        deltas = {t: gain(t) for t in both}
        mine, med = deltas.get(US, (0, 0)), statistics.median(d[0] for d in deltas.values())
        best = sum(deltas[t][0] for t in top5) / len(top5)
        per = (mine[0] / mine[1]) if mine[1] else 0.0
        rows.append((mine[0] - med, role, mine, med, best, per))
    for edge, role, mine, med, best, per in sorted(rows):
        verdict = "guadagniamo" if edge > 0 else "perdiamo" if edge < 0 else "pari"
        print(f"   {SHORT[role]:>11}: noi {mine[0]:+5d} su {mine[1]:4d} valutazioni ({per:+.2f} l'una) · mediana {med:+6.0f} · "
              f"primi 5 {best:+6.0f} → {verdict} {abs(edge):.0f} sulla mediana")


def rubric(since: datetime) -> None:
    """Le nostre righe di rubrica viste dopo ``since``, per ruolo e motivo, dalla peggiore."""
    rows = [r for r in load(RATINGS) if when(r["seen"]) >= since and r["role"] != "altri" and not r.get("backfill")]
    if not rows:
        print("\nrighe di rubrica nella finestra: nessuna nuova")
        return
    total, count = Counter(), Counter()
    for r in rows:
        key = (r["role"], r["reason"])
        total[key] += r["score"]
        count[key] += 1
    print(f"\nrighe di rubrica dei nostri percorsi giudicati (viste dalle {since:%H:%M}Z, {len(rows)} righe):")
    for (role, reason), points in sorted(total.items(), key=lambda kv: kv[1]):
        print(f"   {points:+5d}  {SHORT.get(role, role):>11}  {reason:<28} ×{count[(role, reason)]}")
    by_role = defaultdict(int)
    for (role, _), points in total.items():
        by_role[role] += points
    print("   per ruolo:", {SHORT[r]: by_role[r] for r in ROLES if r in by_role})


def seed(path: str) -> None:
    raw = Path(path).read_text()
    board = json.loads(raw[raw.index("{"):])
    shot = photo(board)
    history = load(HISTORY)
    if any(h["at"] == shot["at"] for h in history):
        print("foto già nello storico")
        return
    rows = sorted(history + [shot], key=lambda h: h["at"])
    HOME.mkdir(exist_ok=True)
    HISTORY.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    print(f"aggiunta la foto delle {when(shot['at']):%H:%M}Z")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", nargs="?", default="all", choices=("all", "report", "snapshot", "seed"))
    parser.add_argument("file", nargs="?")
    parser.add_argument("--since", type=int, default=60, help="finestra del confronto, in minuti (default 60)")
    args = parser.parse_args()
    if args.command == "seed":
        seed(args.file)
        return
    if args.command in ("all", "snapshot"):
        snapshot()
    if args.command in ("all", "report"):
        report(args.since)


if __name__ == "__main__":
    main()
