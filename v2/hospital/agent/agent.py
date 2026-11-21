"""Struttura (``hospital``): il lavoro è in ``facility.py``, fonte unica in ``v2/facility.py``."""

from __future__ import annotations

import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import facility  # noqa: E402


async def main(need, city) -> None:
    await facility.handle("hospital", need, city)
