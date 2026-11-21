"""The gp's review bonuses (21/11): unasked work the citizen can read, and red flags hidden in
free text beside a protocol reason (level 8).

uv run --no-project --with city-sdk==0.9.202 --with pytest python -m pytest -q tests/unit
"""

from __future__ import annotations

import asyncio
import importlib.util
import itertools
import sys
from pathlib import Path

import pytest
from city_sdk.city import City, Message, Need, Peer

ROOT = Path(__file__).resolve().parents[2]
GP, BOOKING, LEDGER, CITIZEN = "agent:ck9w60ejwnvw", "agent:sbdpbpqz5sxe", "city:ledger", "citizen:t01/anna"
SPECIALIST, STRANGER = "agent:h6krzxx6es83", "agent:zz-impostor"


def _load():
    for name in ("tools", "_secrets"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("gp_bonus_agent", ROOT / "gp" / "agent" / "agent.py")
    module = sys.modules["gp_bonus_agent"] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.tools._background = lambda fn, args: None
    return module


class Fake:
    def __init__(self) -> None:
        self.chat: list[Message] = []

    async def search(self, query: str) -> list[Peer]:
        return [Peer(agent_id=BOOKING)]

    async def post(self, *, to, message_type, body, need_id, conversation_id, traceparent) -> str:
        msg_id = f"out-{len(self.chat)}"
        self.chat.append(Message("chat", GP, msg_id, need_id, body={"text": body["text"]}, recipient=to))
        return msg_id

    async def history(self, *, peer, need_id) -> list[Message]:
        return [row for row in self.chat if row.need_id == need_id]


class Model:
    def __init__(self, content: str) -> None:
        self.content, self.calls = content, 0

    async def complete(self, messages, *, need_id=None):
        self.calls += 1
        return {"model": "city-default", "content": self.content}


_IDS = itertools.count()


def _turn(gp, fake: Fake, sender: str, text: str, llm=None) -> list[Message]:
    before = len(fake.chat)
    inbound = Message("chat", sender, f"in-{next(_IDS)}", "N-1", body={"text": text},
                      recipient=GP, sent_at="2026-11-21T10:00:00.000Z")
    fake.chat.append(inbound)
    city = City(index=fake, messages=fake, history=fake, agent_id=GP, inbound=inbound, llm=llm)
    asyncio.run(gp.main(Need(), city))
    return [row for row in fake.chat[before + 1:] if row.sender == GP]


def _to(rows: list[Message], who: str) -> list[str]:
    return [row.text for row in rows if row.recipient == who]


def test_prescription_says_the_unasked_handoff_and_the_handoff_names_the_deadline():
    gp, fake = _load(), Fake()
    out = _turn(gp, fake, CITIZEN, "Ho mal di schiena da tre giorni")
    [handoff] = _to(out, BOOKING)
    assert "priorità B: la visita va fatta entro 2 giorni" in handoff
    assert "solo date entro la scadenza" in handoff
    [told] = _to(out, CITIZEN)
    assert told.startswith("prescription ")
    assert "Di mia iniziativa" in told and "scadenza della priorità" in told


def test_priority_p_claims_no_deadline():
    gp, fake = _load(), Fake()
    out = _turn(gp, fake, CITIZEN, "Vorrei il controllo del cuore periodico")
    [handoff] = _to(out, BOOKING)
    assert "scadenza" not in handoff
    [told] = _to(out, CITIZEN)
    assert "Di mia iniziativa" in told and "scadenza" not in told


def _facility_notice(number: str, booking: str = "bk-1") -> str:
    return (f"data.response purpose=booking booking_id={booking} prescription_number={number} "
            "procedure=VIS-FIS slot_id=S-1 Buongiorno collega, la informo che il suo paziente è prenotato.")


def test_facility_notice_reaches_our_patient_once():
    gp, fake = _load(), Fake()
    first = _turn(gp, fake, CITIZEN, "Ho mal di schiena")
    number = next(t for t in _to(first, LEDGER)).split("prescription_number=")[1].split()[0]
    out = _turn(gp, fake, SPECIALIST, _facility_notice(number))
    [told] = _to(out, CITIZEN)
    assert "bk-1" in told and "medico di base" in told
    assert not any(word in told.split() for word in ("prescription", "confirm", "offer", "cancel", "report"))
    assert _turn(gp, fake, SPECIALIST, _facility_notice(number)) == []  # once per booking


def test_notice_about_a_prescription_not_ours_or_from_a_stranger_is_ignored():
    gp, fake = _load(), Fake()
    first = _turn(gp, fake, CITIZEN, "Ho mal di schiena")
    number = next(t for t in _to(first, LEDGER)).split("prescription_number=")[1].split()[0]
    assert _turn(gp, fake, SPECIALIST, _facility_notice("prescription_number-NOTOURS")) == []
    assert _turn(gp, fake, STRANGER, _facility_notice(number)) == []


def test_hidden_red_flag_beside_a_protocol_reason_escalates():
    gp, fake = _load(), Fake()
    model = Model('{"code": "SYM-BACK", "red_flag": true, "lang": "it"}')
    out = _turn(gp, fake, CITIZEN, "Ho mal di schiena e da stamattina la gamba destra non la governo", llm=model)
    assert model.calls == 1
    assert _to(out, LEDGER) == [] and _to(out, BOOKING) == []
    [told] = _to(out, CITIZEN)
    assert told.startswith("escalate.emergency reason=red_flag")


def test_model_down_still_prescribes():
    gp, fake = _load(), Fake()
    out = _turn(gp, fake, CITIZEN, "Ho mal di schiena da tre giorni", llm=None)
    assert any(t.startswith("prescribe ") for t in _to(out, LEDGER))


def test_structured_symptom_code_skips_the_model():
    gp, fake = _load(), Fake()
    model = Model('{"red_flag": true}')
    out = _turn(gp, fake, CITIZEN, "need.request symptom_code=SYM-BACK Buongiorno", llm=model)
    assert model.calls == 0
    assert any(t.startswith("prescribe ") for t in _to(out, LEDGER))


def test_error_summary_raises_for_conformance():
    gp, fake = _load(), Fake()
    inbound = Message("chat", CITIZEN, "in-err", "N-1", body={"text": "x"}, recipient=GP)
    city = City(index=fake, messages=fake, history=fake, agent_id=GP, inbound=inbound)
    with pytest.raises(RuntimeError):
        asyncio.run(gp.main(Need(summary="__error__"), city))
