"""shared/mach/ring.py: l'anello MACH-2.1 (130 agenti) e la lingua rara degli ospedali.

uv run --no-project --with city-sdk==0.9.202 --with pytest python -m pytest -q tests/unit/test_ring.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared"))
from mach import ring  # noqa: E402

OURS = ["agent:ck9w60ejwnvw", "agent:sbdpbpqz5sxe", "agent:m6py14a1ej77", "agent:h6krzxx6es83", "agent:yg8debxe3x0p"]


def test_ring_is_the_whole_map():
    assert len(ring.RING) == 130
    assert len(set(ring.ASST.values())) == 26
    assert all(ring.ASST[a] == "asst-gorla" for a in OURS)


def test_in_ring():
    assert all(ring.in_ring(a) for a in OURS)
    assert ring.in_ring("agent:gezf38x5940y")  # EPICA, ospedale
    assert ring.in_ring("agent:d6cxxmh767k3")  # Isola, ospedale
    assert not ring.in_ring("agent:zzzzzzzzzzzz")
    assert not ring.in_ring(None)
    assert not ring.in_ring("")
    assert not ring.in_ring("citizen:ck9w60ejwnvw")


def test_rare():
    assert ring.RARE["agent:d6cxxmh767k3"] == "tl"  # asst-isola
    assert ring.RARE["agent:gezf38x5940y"] == "pt"  # EPICA, detto da loro
    assert ring.RARE["agent:m6py14a1ej77"] == "sq"  # noi
    assert set(ring.RARE) <= ring.RING
