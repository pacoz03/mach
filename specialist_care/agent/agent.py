"""Specialista (specialist_care.cardiology).

Il brain sta in ``facility.py`` (copia di ``shared/facility.py``, ruolo ``specialist_care``)."""

from __future__ import annotations

import sys
from pathlib import Path

from city_sdk.city import City, Need, OutOfScope, clinical

# Tool e brain condivisi (shared/, copiati qui da shared/sync.sh). L'harness carica questo file
# per percorso, non come package: la cartella va sul path per importare i file vicini.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import facility  # noqa: E402
import tools  # noqa: E402


async def main(need: Need, city: City) -> None:
    message = city.inbound
    if message is None:
        return
    if clinical(message.text or need.summary):
        raise OutOfScope()
    tools.start_turn(message)
    await facility.handle("specialist_care", need, city)
