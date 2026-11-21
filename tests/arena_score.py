"""Cricchetto dell'arena: i punti di v1 per scenario non scendono sotto tests/arena_baseline.txt.

    ~/.local/share/uv/tools/city-cli/bin/python -B tests/arena_score.py [arena.json] [--update-baseline]

Lancia v2/tests/arena.py --set old (gli agenti della radice, ~4 min) o legge un suo --json già fatto. Punti di
uno scenario = modo predefinito dell'arena (risposte del Ledger che aprono un turno) meno clock.fast_reply
(R:246: in città si misura sull'ACK, qui no). Uscita 1 se uno scenario scende o manca, o se l'arena è rotta.
Soglia esatta, senza tolleranza: l'arena è deterministica (2026-11-21, base cd79e87: tre giri, due in parallelo,
stessi punti per ogni scenario). Se un giro dà punti diversi senza cambi di codice, va misurata la varianza.
"""

import json
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests" / "arena_baseline.txt"


def points(arena: dict) -> dict[str, int]:
    return {r["name"]: r["modes"]["turns"]["points"] - sum(x["points"] for x in r["modes"]["turns"]["rows"] if x["row"] == "clock.fast_reply")
            for r in arena["scenarios"]}


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--update-baseline"]
    rc = 0
    if args:
        out = Path(args[0])
    else:
        out = Path(tempfile.mkdtemp(prefix="arena.")) / "arena.json"
        with open(out.with_suffix(".log"), "w") as log:
            rc = subprocess.run([sys.executable, "-B", "v2/tests/arena.py", "--set", "old", "--json", str(out)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT).returncode
        print(f"arena  log {out.with_suffix('.log')}")
    if rc or not out.exists():
        print(f"arena  ROTTA (uscita {rc})")
        return 1
    now = points(json.loads(out.read_text(encoding="utf-8")))
    if "--update-baseline" in sys.argv:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        BASELINE.write_text(f"# Cricchetto dell'arena (tests/arena_score.py): punti di v1 per scenario, modo predefinito (Ledger nei turni),\n"
                            f"# senza clock.fast_reply. {date.today()}  base {head}  ~/.local/share/uv/tools/city-cli/bin/python -B tests/arena_score.py --update-baseline\n"
                            + "".join(f"{k} {v}\n" for k, v in now.items()))
        print(f"scritto {BASELINE.name}: {len(now)} scenari, totale {sum(now.values())}")
    base = {k: int(v) for k, v in (ln.split() for ln in BASELINE.read_text().splitlines() if ln.strip() and not ln.startswith("#"))}
    worse = {k: v for k, v in base.items() if now.get(k, -999) < v}
    better = {k: v for k, v in base.items() if now.get(k, -999) > v}
    print(f"arena  TOTALE {sum(now.values())} (baseline {sum(base.values())})")
    for tag, group in (("PEGGIORATO", worse), ("migliorato", better)):
        for k, v in group.items():
            print(f"  {tag} {k}: {now[k] if k in now else 'assente'} (baseline {v})")
    for k in now.keys() - base.keys():
        print(f"  nuovo {k}: {now[k]} (non in baseline)")
    return 1 if worse else 0


if __name__ == "__main__":
    sys.exit(main())
