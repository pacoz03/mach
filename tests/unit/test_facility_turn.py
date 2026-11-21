"""shared/facility.py: only OutOfScope leaves a facility turn (an httpx timeout out of the brain
makes the SDK harness redeliver the message, and every say of the turn goes out twice)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from city_sdk.city import City, Message, Need

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared"))
import facility  # noqa: E402

ME, BOOKING = "agent:specialist", "agent:booking"


class Relay:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def history(self, *, peer, need_id) -> list[Message]:
        raise self.error

    async def post(self, **kw) -> str:
        raise self.error


@pytest.mark.parametrize("role", ["hospital", "specialist_care", "family_counseling"])
def test_a_relay_timeout_never_leaves_the_turn(role):
    relay = Relay(httpx.ReadTimeout("relay history"))
    inbound = Message(type="chat", sender=BOOKING, recipient=ME, msg_id="in-1", need_id="N-1",
                      body={"text": "confirm offer_id=of-S-1 slot_id=S-1 citizen=citizen:a consent_ref=c-1"})
    city = City(index=relay, messages=relay, history=relay, agent_id=ME, inbound=inbound)
    asyncio.run(facility.handle(role, Need(), city))  # raised httpx.ReadTimeout before
