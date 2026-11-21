#!/bin/sh
# Copia shared/tools.py in ogni agente, shared/facility.py nelle tre strutture, il pacchetto
# shared/mach/ in <agente>/agent/mach/ (in tutti e 5) e genera
# <agente>/agent/_secrets.py da .env.
# city deploy impacchetta solo <agente>/agent/ e salta symlink e .env: servono file veri.
set -eu
cd "$(dirname "$0")/.."
AGENTS="booking_centre gp hospital specialist_care family_counseling"
FACILITIES="hospital specialist_care family_counseling"
MACH="booking_centre gp hospital specialist_care family_counseling"
SUPABASE_URL=""
SUPABASE_PUBLISHABLE_KEY=""
EXA_API_KEY=""
if [ -f .env ]; then . ./.env; fi
for agent in $AGENTS; do
  cp shared/tools.py "$agent/agent/tools.py"
  # Solo la publishable key di Supabase: la secret key non lascia mai il laptop.
  printf 'SUPABASE_URL = "%s"\nSUPABASE_KEY = "%s"\nEXA_KEY = "%s"\n' \
    "$SUPABASE_URL" "$SUPABASE_PUBLISHABLE_KEY" "$EXA_API_KEY" > "$agent/agent/_secrets.py"
done
for agent in $FACILITIES; do
  cp shared/facility.py "$agent/agent/facility.py"
done
for agent in $MACH; do
  mkdir -p "$agent/agent/mach"
  cp shared/mach/*.py "$agent/agent/mach/"
done
# Ogni copia deve coincidere con la fonte: cmp esce con errore e set -e ferma lo script.
for agent in $AGENTS; do cmp shared/tools.py "$agent/agent/tools.py"; done
for agent in $FACILITIES; do cmp shared/facility.py "$agent/agent/facility.py"; done
# Anche un modulo tolto da shared/mach/ e rimasto nella copia ferma lo script.
for agent in $MACH; do diff -r -x __pycache__ shared/mach "$agent/agent/mach"; done
if [ -z "$SUPABASE_URL" ] || [ -z "$SUPABASE_PUBLISHABLE_KEY" ]; then
  echo "attenzione: .env senza SUPABASE_URL/SUPABASE_PUBLISHABLE_KEY, la memoria resta spenta" >&2
fi
if [ -z "$EXA_API_KEY" ]; then
  echo "attenzione: .env senza EXA_API_KEY, web_search restituisce []" >&2
fi
echo "tools sincronizzati nei 5 agenti, facility nelle 3 strutture, mach nei 5 agenti"
