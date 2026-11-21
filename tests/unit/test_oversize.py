"""tools.too_long: un messaggio lunghissimo chiude il turno di ogni agente senza toccare la città.

uv run --no-project --with city-sdk==0.9.202 --with pytest python -m pytest -q tests/unit/test_oversize.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ("booking_centre", "gp", "hospital", "specialist_care", "family_counseling")
sys.path.insert(0, str(ROOT / "shared"))
import tools  # noqa: E402


def _load(agent: str):
    name = f"oversize_{agent}"
    spec = importlib.util.spec_from_file_location(name, ROOT / agent / "agent" / "agent.py")
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _City:
    """Solo ``inbound`` e ``agent_id``: ogni altro accesso (say, history, llm, find) è registrato."""

    def __init__(self, inbound):
        self.inbound = inbound
        self.agent_id = "agent:us"
        self.touched: list[str] = []

    def __getattr__(self, name):
        self.touched.append(name)
        raise AttributeError(name)


def _msg(sender: str, text: str):
    return SimpleNamespace(sender=sender, text=text, need_id="n-1", msg_id="m-1", type="chat", sent_at="")


def test_limits():
    assert not tools.too_long(_msg("citizen:c", "x" * tools.MAX_CITIZEN_CHARS))
    assert tools.too_long(_msg("citizen:c", "x" * (tools.MAX_CITIZEN_CHARS + 1)))
    assert not tools.too_long(_msg("agent:a", "x" * tools.MAX_AGENT_CHARS))
    assert tools.too_long(_msg("agent:a", "x" * (tools.MAX_AGENT_CHARS + 1)))
    assert not tools.too_long(_msg("city:ledger", "x" * 100_000))  # pagine di world: mai scartate
    assert not tools.too_long(_msg("citizen:c", ""))


@pytest.mark.parametrize("agent", AGENTS)
@pytest.mark.parametrize("sender", ["citizen:W01-1", "agent:zz9other"])
def test_agent_ignores_oversize(agent, sender):
    # Anche con parole d'allarme e cliniche dentro: nessun lavoro sul testo, nessuna risposta.
    text = "prenotazione dolore al petto " * 2000
    need = SimpleNamespace(summary=text, is_emergency=False, need_id="n-1")
    city = _City(_msg(sender, text))
    asyncio.run(_load(agent).main(need, city))
    assert city.touched == []
