#!/bin/sh
# Copia v2/core.py in ogni agente v2, v2/facility.py nelle tre strutture, ../shared/tools.py (la
# traccia per gli eval) in tutti, e genera <agente>/agent/_secrets.py da ../.env.
# city deploy impacchetta solo <agente>/agent/ e salta symlink e .env: servono file veri.
set -eu
cd "$(dirname "$0")"
AGENTS="gp booking_centre hospital specialist_care family_counseling"
FACILITIES="hospital specialist_care family_counseling"
SUPABASE_URL=""; SUPABASE_PUBLISHABLE_KEY=""; EXA_API_KEY=""
if [ -f ../.env ]; then . ../.env; fi
version() { sed -n 's/^version: *//p' "$1" | awk -F. '{ printf "%d%04d%04d", $1, $2, $3 }'; }
for agent in $AGENTS; do
  # La card è una sola per agente (stessa identità in città): vale quella con la versione più alta.
  if [ "$(version "../$agent/card.yaml")" -gt "$(version "$agent/card.yaml")" ]; then
    cp "../$agent/card.yaml" "$agent/card.yaml"
  elif [ "$(version "../$agent/card.yaml")" -lt "$(version "$agent/card.yaml")" ]; then
    echo "nota: v2/$agent/card.yaml è più avanti della radice (deploy fatto da v2): cp v2/$agent/card.yaml $agent/card.yaml" >&2
  fi
  cp core.py "$agent/agent/core.py"
  cp ../shared/tools.py "$agent/agent/tools.py"
  # Solo la publishable key di Supabase: la secret key non lascia mai il laptop.
  printf 'SUPABASE_URL = "%s"\nSUPABASE_KEY = "%s"\nEXA_KEY = "%s"\n' \
    "$SUPABASE_URL" "$SUPABASE_PUBLISHABLE_KEY" "$EXA_API_KEY" > "$agent/agent/_secrets.py"
done
for agent in $FACILITIES; do cp facility.py "$agent/agent/facility.py"; done
for agent in $AGENTS; do cmp core.py "$agent/agent/core.py"; done
for agent in $FACILITIES; do cmp facility.py "$agent/agent/facility.py"; done
echo "v2: core e tools sincronizzati nei 5 agenti, facility nelle 3 strutture"
