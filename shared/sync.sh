#!/bin/sh
# Copia shared/tools.py in ogni agente e genera <agente>/agent/_secrets.py da .env.
# city deploy impacchetta solo <agente>/agent/ e salta symlink e .env: servono file veri.
set -eu
cd "$(dirname "$0")/.."
SUPABASE_URL=""
SUPABASE_PUBLISHABLE_KEY=""
EXA_API_KEY=""
if [ -f .env ]; then . ./.env; fi
for agent in booking_centre gp hospital specialist_care family_counseling; do
  cp shared/tools.py "$agent/agent/tools.py"
  # Solo la publishable key di Supabase: la secret key non lascia mai il laptop.
  printf 'SUPABASE_URL = "%s"\nSUPABASE_KEY = "%s"\nEXA_KEY = "%s"\n' \
    "$SUPABASE_URL" "$SUPABASE_PUBLISHABLE_KEY" "$EXA_API_KEY" > "$agent/agent/_secrets.py"
done
if [ -z "$SUPABASE_URL" ] || [ -z "$SUPABASE_PUBLISHABLE_KEY" ]; then
  echo "attenzione: .env senza SUPABASE_URL/SUPABASE_PUBLISHABLE_KEY, la memoria resta spenta" >&2
fi
if [ -z "$EXA_API_KEY" ]; then
  echo "attenzione: .env senza EXA_API_KEY, web_search restituisce []" >&2
fi
echo "tools sincronizzati nei 5 agenti"
