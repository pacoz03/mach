"""Harness per lo specialista: carica specialist_care/agent/agent.py (con facility e tools
vicini) contro un City finto con history condivisa. Memoria ed Exa spente."""

from __future__ import annotations

import asyncio
import importlib.util
import itertools
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from city_sdk.city import City, Message, Need, Peer

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "specialist_care" / "agent"

CITIZEN = "citizen:t01/anna"
LEDGER = "city:ledger"
BOOKING = "bk-1"
AGENT = "agent:spec"

_ids = itertools.count(1)


def msg(sender: str, text: str, *, recipient: str = "", need_id: str = "N-1") -> Message:
    return Message(
        type="chat",
        sender=sender,
        msg_id=f"m-{next(_ids)}",
        need_id=need_id,
        body={"text": text},
        recipient=recipient,
    )


class FakeCity:
    """Index, mailbox e history in memoria. Ogni post finisce in ``chat`` e in ``sent``.

    ``fail(to, text)`` restituisce un'eccezione da sollevare (o None); ``yields`` cede il loop
    prima del post come una chiamata di rete; ``on_post`` simula risposte che arrivano intanto.
    """

    def __init__(
        self,
        chat: list[Message],
        *,
        agent_id: str = AGENT,
        on_post: Callable[[str, str, FakeCity], None] | None = None,
        fail: Callable[[str, str], BaseException | None] | None = None,
        yields: bool = False,
        auto: tuple[str, ...] = ("report", "checkin"),
    ) -> None:
        self.auto = auto  # Ledger ops answered at once (a slow live Ledger: leave one out)
        self.chat = chat
        self.agent_id = agent_id
        self.on_post = on_post
        self.fail = fail
        self.yields = yields
        self.sent: list[tuple[str, str]] = []
        self.peers: list[Peer] = []  # what the Index returns to city.find

    async def search(self, query: str) -> list[Peer]:
        return list(self.peers)

    async def post(self, *, to, message_type, body, need_id, conversation_id, traceparent) -> str:
        text = str(body["text"])
        if self.yields:
            await asyncio.sleep(0)
        if self.fail is not None:
            error = self.fail(to, text)
            if error is not None:
                raise error
        self.sent.append((to, text))
        row = msg(self.agent_id, text, recipient=to, need_id=need_id or "N-1")
        self.chat.append(row)
        if to == LEDGER:
            for head, reply in (("report", "reported"), ("checkin", "checked_in")):
                if head in self.auto and text.startswith(f"{head} booking_id="):
                    booking = text.split()[1]
                    self.chat.append(msg(LEDGER, f"{reply} {booking}", recipient=self.agent_id))
        if self.on_post is not None:
            self.on_post(to, text, self)
        return row.msg_id

    async def history(self, *, peer: str | None, need_id: str | None) -> list[Message]:
        return [row for row in self.chat if need_id is None or row.need_id == need_id]

    def city(self, inbound: Message) -> City:
        return City(index=self, messages=self, history=self, agent_id=self.agent_id, inbound=inbound)

    def reports(self) -> list[str]:
        return [t for to, t in self.sent if to == LEDGER and t.startswith("report")]

    def to_citizen(self, head: str) -> list[str]:
        return [t for to, t in self.sent if to == CITIZEN and t.split()[0] == head]


async def turn(brain, fake: FakeCity, inbound: Message) -> None:
    if inbound not in fake.chat:
        fake.chat.append(inbound)
    await brain.main(Need(), fake.city(inbound))


def run(brain, fake: FakeCity, inbound: Message) -> None:
    asyncio.run(turn(brain, fake, inbound))


@pytest.fixture
def brain(monkeypatch):
    """Il modulo agent.py dello specialista, con facility e tools ricaricati da zero."""

    for key in ("CITY_ENV", "TEAM_MEMORY"):
        monkeypatch.delenv(key, raising=False)
    for name in [n for n in sys.modules if n in ("facility", "tools", "_secrets", "mach") or n.startswith("mach.")]:
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("specialist_agent", AGENT_DIR / "agent.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.facility._PULL_PAUSE = 0
    return module


@pytest.fixture
def fac(brain):
    return brain.facility
