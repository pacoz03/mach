#!/usr/bin/env bash
# Rete di sicurezza del refactoring (step 0 di docs/refactor/piano.md): tutti i controlli in parallelo,
# una riga per controllo, uscita 0 solo se è tutto verde. Non lancia shared/sync.sh (riscrive i file).
# Uso: bash tests/check.sh [-k <testo>] [--chaos] [--arena]    (-k passa a tests/journey.py)
set -u
cd "$(dirname "$0")/.."
K=() CHAOS=0 ARENA=0
while [ $# -gt 0 ]; do
  case "$1" in
    -k) K=(-k "$2"); shift 2 ;;
    --chaos) CHAOS=1; shift ;;   # anche tests/chaos.py (~4 min, ~6 GB RAM): all'integrazione di codice degli agenti
    --arena) ARENA=1; shift ;;   # anche il cricchetto dell'arena, tests/arena_score.py (~4 min)
    *) shift ;;
  esac
done
LOG=$(mktemp -d "${TMPDIR:-/tmp}/check.XXXXXX")
PY=~/.local/share/uv/tools/city-cli/bin/python
AGENTS="booking_centre gp hospital specialist_care family_counseling"   # come shared/sync.sh
FACILITIES="hospital specialist_care family_counseling"
MACH="booking_centre gp hospital specialist_care family_counseling"   # come shared/sync.sh

# run <nome> <comando...>: in background, output in $LOG/<nome>.log, exit code in $LOG/<nome>.rc
run() { local name=$1; shift; ( "$@" >"$LOG/$name.log" 2>&1; echo $? >"$LOG/$name.rc" ) & }

run journey-plain     "$PY" -B tests/journey.py ${K[@]+"${K[@]}"}
run journey-turns     "$PY" -B tests/journey.py --ledger-turns ${K[@]+"${K[@]}"}
run journey-sdk       "$PY" -B tests/journey.py --ledger-format sdk ${K[@]+"${K[@]}"}
run journey-turns-sdk "$PY" -B tests/journey.py --ledger-turns --ledger-format sdk ${K[@]+"${K[@]}"}
# Invocazioni separate: conftest.py separati e moduli omonimi (test_level5.py).
PYTEST=(uv run --no-project --with city-sdk==0.9.202 --with pytest python -m pytest -q -p no:cacheprovider)
run pytest-specialist        "${PYTEST[@]}" tests/specialist
run pytest-consultorio       "${PYTEST[@]}" tests/consultorio
run pytest-family_counseling "${PYTEST[@]}" family_counseling/tests
run pytest-unit              "${PYTEST[@]}" tests/unit
for a in $AGENTS; do run "card-$a" city card validate "$a"; done
[ "$CHAOS" = 1 ] && run chaos "$PY" -B tests/chaos.py
[ "$ARENA" = 1 ] && run arena "$PY" -B tests/arena_score.py
wait

fails=()
ok() { [ "$(cat "$LOG/$1.rc" 2>/dev/null)" = 0 ]; }
for m in plain turns sdk turns-sdk; do
  f=$LOG/journey-$m.log
  n() { grep -c "^$1 " "$f"; }
  printf 'journey %-10s PASS %3s  FAIL %s  XFAIL %s  XPASS %s  SKIP %s\n' \
    "$m" "$(n PASS)" "$(n FAIL)" "$(n XFAIL)" "$(n XPASS)" "$(n SKIP)"
  ok "journey-$m" || fails+=("journey-$m")
done
for p in "$LOG"/pytest-*.log; do
  name=$(basename "$p" .log)
  n() { local x; x=$(grep -Eo "[0-9]+ $1" "$p" | tail -1 | cut -d' ' -f1); echo "${x:-0}"; }
  printf '%-25s %3s passed  %s failed  %s errors\n' "$name" "$(n passed)" "$(n failed)" "$(n 'errors?')"
  ok "$name" || fails+=("$name")
done
for a in $AGENTS; do
  if ok "card-$a"; then echo "card $a OK"; else echo "card $a FAIL"; fails+=("card-$a"); fi
done
if [ "$CHAOS" = 1 ]; then
  tail -3 "$LOG/chaos.log" | sed 's/^/chaos: /'
  ok chaos || fails+=(chaos)
fi
if [ "$ARENA" = 1 ]; then
  cat "$LOG/arena.log"
  ok arena || fails+=(arena)
fi
files=0 diff=()
for a in $AGENTS; do files=$((files+1)); cmp -s shared/tools.py "$a/agent/tools.py" || diff+=("$a/agent/tools.py"); done
for a in $FACILITIES; do files=$((files+1)); cmp -s shared/facility.py "$a/agent/facility.py" || diff+=("$a/agent/facility.py"); done
for a in $MACH; do
  for f in shared/mach/*.py; do files=$((files+1)); cmp -s "$f" "$a/agent/mach/${f##*/}" || diff+=("$a/agent/mach/${f##*/}"); done
  diff -rq -x __pycache__ shared/mach "$a/agent/mach" >/dev/null 2>&1 || diff+=("$a/agent/mach/")  # un modulo in più
done
if [ ${#diff[@]} = 0 ]; then echo "cmp OK ($files files)"; else echo "cmp DIFF: ${diff[*]}"; fails+=(cmp); fi

echo "time ${SECONDS}s  logs $LOG"
if [ ${#fails[@]} = 0 ]; then echo "ALL GREEN"; exit 0; fi
echo "FAILED: ${fails[*]}"
exit 1
