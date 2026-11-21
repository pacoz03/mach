"""shared/facility.py, hospital cross-ASST with a late or out-of-order Ledger and a restarted pod
(tests/chaos.py: ledger_late, ledger_reorder, restart): no page means no answer, never a
``cancel reason=no_availability``; the page answers the pending request when it comes; the case
history gives a restarted pod its clock and pages back."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from city_sdk.city import LEDGER, City, Message, Need

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared"))
import facility  # noqa: E402

ME, PARTNER, NEED = "agent:hospital", "agent:yzcs4p44ep39", "N-1"  # PARTNER nell'anello (MACH-2.1 R17)
REQUEST = "need.request service_type=hospital.physiatry procedure=VIS-FIS priority=B request_id=R-1"
MONDAY = "2026-11-23T08:00:00+00:00"
PAGE = "\n".join([
    "slots page=1/1 count=2",
    "slot_id=S-h1-01-10 doctor_id=D-h1 facility=agent:hospital procedure=VIS-FIS at=2026-11-24T10:00:00+00:00 day=tue free=1",
    "slot_id=S-h1-01-15 doctor_id=D-h1 facility=agent:hospital procedure=VIS-FIS at=2026-11-24T15:00:00+00:00 day=tue free=1",
])


def row(sender: str, to: str, text: str) -> Message:
    return Message(type="chat", sender=sender, recipient=to, msg_id=f"m-{abs(hash((sender, to, text)))}",
                   need_id=NEED, body={"text": text}, sent_at=datetime.now(UTC).isoformat())


def agenda() -> Message:
    now = datetime.now(UTC).isoformat()
    return row(LEDGER, ME, f"agenda rate=60.0 paused=false agenda_day=mon agenda_now={MONDAY} "
                           f"epoch_real={now} epoch_agenda={MONDAY}")


class Case:
    """The case history as the Relay keeps it; what the hospital says is appended (no reply)."""

    def __init__(self, rows: list[Message]) -> None:
        self.rows, self.said = rows, []

    async def history(self, *, peer, need_id) -> list[Message]:
        return list(self.rows)

    async def post(self, *, to, message_type, body, need_id, conversation_id, traceparent) -> str:
        self.said.append((to, body["text"]))
        self.rows.append(row(ME, to, body["text"]))
        return self.rows[-1].msg_id


def turn(case: Case, inbound: Message) -> None:
    case.rows.append(inbound)
    city = City(index=case, messages=case, history=case, agent_id=ME, inbound=inbound)
    asyncio.run(facility.handle("hospital", Need(), city))


@pytest.fixture(autouse=True)
def fresh_pod(monkeypatch):
    for state in (facility._WORLD, facility._CLOCK, facility._SENT, facility._OFFERED, facility._HELD):
        state.clear()
    monkeypatch.setattr(facility, "_PULL_PAUSE", 0)
    monkeypatch.setattr(facility, "_LEDGER_WAIT", 0.05)
    monkeypatch.setattr(facility, "_RECALLED", True)  # no team-memory read in the background


def to_partner(case: Case) -> list[str]:
    return [text for to, text in case.said if to == PARTNER]


def test_no_page_in_time_facility_unavailable_never_no_availability():
    case = Case([])
    turn(case, row(PARTNER, ME, REQUEST))
    assert [text.split()[0] for to, text in case.said if to == LEDGER] == ["agenda", "world"]
    # W3 (21/11, Ledger replies in ~595 s): past _LEDGER_WAIT the request is closed with one
    # facility_unavailable (the old code said "cancel reason=no_availability request_id=R-1", then nothing).
    [answer] = to_partner(case)
    assert answer.split()[:3] == ["cancel", "reason=facility_unavailable", "request_id=R-1"]
    assert "offer_id=" not in answer and "no_availability" not in answer


def test_the_late_page_answers_the_pending_request_whatever_we_asked_last():
    # Our last world ask is the visit's time (`world page=1`, all facilities); the VIS-FIS page
    # comes after it, out of order. It is still the answer to the VIS-FIS ask (content and page).
    case = Case([row(PARTNER, ME, REQUEST), row(ME, LEDGER, "agenda"), agenda(),
                 row(ME, LEDGER, "world procedure=VIS-FIS page=1"), row(ME, LEDGER, "world page=1")])
    turn(case, row(LEDGER, ME, PAGE))
    [answer] = to_partner(case)
    assert answer.startswith("offer ") and "request_id=R-1" in answer.split()
    assert "no_availability" not in answer
    turn(case, row(LEDGER, ME, PAGE))  # the same page again: answered once
    assert len(to_partner(case)) == 1


def test_a_restarted_pod_gets_clock_and_page_back_from_the_case_without_our_held_slot():
    chat = [row(ME, LEDGER, "agenda"), agenda(), row(ME, LEDGER, "world procedure=VIS-FIS page=1"),
            row(LEDGER, ME, PAGE), row(ME, LEDGER, "hold slot_id=S-h1-01-10 citizen=citizen:a consent_ref=c-1")]
    facility._recover(chat, ME)
    assert facility._city_now() is not None
    _, page = facility._WORLD[("VIS-FIS", 1)]
    assert "slot_id=S-h1-01-15" in page and "slot_id=S-h1-01-10" not in page
