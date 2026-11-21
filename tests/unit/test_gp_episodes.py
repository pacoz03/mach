"""Il gp scrive la sua traccia in episodes (step 6 di docs/refactor/piano.md).

uv run --no-project --with city-sdk==0.9.202 --with pytest python -m pytest -q tests/unit
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

from city_sdk.city import City, CapError, Message, Need, Peer

ROOT = Path(__file__).resolve().parents[2]
GP, BOOKING, LEDGER, CITIZEN = "agent:gp", "agent:sbdpbpqz5sxe", "city:ledger", "citizen:t01/anna"


def _load():
    for name in ("tools", "_secrets"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("gp_agent", ROOT / "gp" / "agent" / "agent.py")
    module = sys.modules["gp_agent"] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Fake:
    def __init__(self, fail_ledger: bool = False) -> None:
        self.chat: list[Message] = []
        self.fail_ledger = fail_ledger

    async def search(self, query: str) -> list[Peer]:
        return [Peer(agent_id=BOOKING)]

    async def post(self, *, to, message_type, body, need_id, conversation_id, traceparent) -> str:
        if self.fail_ledger and to == LEDGER:
            raise CapError("TARGET_NOT_VISIBLE", to)
        msg_id = f"out-{len(self.chat)}"
        self.chat.append(Message("chat", GP, msg_id, need_id, body={"text": body["text"]}, recipient=to))
        return msg_id

    async def history(self, *, peer, need_id) -> list[Message]:
        return [row for row in self.chat if row.need_id == need_id]


def _turn(gp, fake: Fake, sender: str, text: str) -> list[tuple[str, dict]]:
    written: list[tuple[str, dict]] = []
    gp.tools._background = lambda fn, args: written.append((fn, args))
    inbound = Message("chat", sender, f"in-{len(fake.chat)}", "N-1", body={"text": text},
                      recipient=GP, sent_at="2026-11-21T04:00:00.000Z")
    fake.chat.append(inbound)
    city = City(index=fake, messages=fake, history=fake, agent_id=GP, inbound=inbound)
    asyncio.run(gp.main(Need(), city))
    return written


def test_citizen_turn_is_traced_without_their_words():
    gp, fake = _load(), Fake()
    rows = [args for fn, args in _turn(gp, fake, CITIZEN, "Mal di schiena da tre giorni") if fn == "observe"]
    inbound, *out = rows
    assert inbound["p_direction"] == "in" and inbound["p_act"] == ""  # "mal" is the citizen's text
    assert inbound["p_fields"] == {"sent_at": "2026-11-21T04:00:00.000Z"}
    assert [(r["p_peer"], r["p_act"]) for r in out] == [
        (LEDGER, "prescribe"), (BOOKING, "need.request"), (CITIZEN, "prescription")]
    assert all(r["p_need_id"] == "N-1" and isinstance(r["p_latency_ms"], int) and r["p_msg_id"] for r in out)


def test_ledger_turn_and_refused_say_are_traced():
    gp = _load()
    rows = [a for _, a in _turn(gp, Fake(), LEDGER, "prescribed prescription_number=X")]
    assert rows[0]["p_peer"] == LEDGER and rows[0]["p_act"] == "prescribed"
    assert rows[0]["p_fields"]["sent_at"] == "2026-11-21T04:00:00.000Z"
    rows = [a for _, a in _turn(gp, Fake(fail_ledger=True), CITIZEN, "Ho mal di schiena")]
    assert (rows[1]["p_peer"], rows[1]["p_error"], rows[1]["p_msg_id"]) == (LEDGER, "TARGET_NOT_VISIBLE", None)
    assert rows[2]["p_peer"] == CITIZEN and rows[2]["p_body"].startswith("Non sono riuscito")
