"""shared/mach/protocol.py on the real Ledger replies of tests/fixtures/*.txt, plus say and until."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import httpx
import pytest
from city_sdk import city as sdk
from city_sdk.city import LEDGER, CapError, City, Message

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared"))
import tools  # noqa: E402
from mach import protocol  # noqa: E402

ME = "agent:me"
CITIZEN = "citizen:W01QQQ-24-900001"
HEADS = ("slots", "agenda", "prescription", "rules", "refused", "held", "committed", "checked_in", "reported")


def blocks(name: str) -> list[str]:
    text = (ROOT / "tests" / "fixtures" / f"{name}.txt").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    return [b.strip() for b in "\n".join(lines).split("\n---\n") if b.strip()]


def msg(sender: str, text: str, *, to: str = ME, at: str = "", mid: str = "", kind: str = "chat", **body) -> Message:
    return Message(type=kind, sender=sender, recipient=to, msg_id=mid or f"m-{id(text)}-{at}",
                   body={"text": text, **body}, sent_at=at)


ALL = [(head, block) for head in HEADS for block in blocks(head)]


def test_fixtures_are_there():
    assert len(ALL) >= 60 and {head for head, _ in ALL} == set(HEADS)


@pytest.mark.parametrize(("head", "block"), ALL)
def test_act_is_the_ledger_head(head, block):
    assert protocol.act(msg(LEDGER, block)) == head == protocol.first_word(block)


@pytest.mark.parametrize(("head", "block"), ALL)
def test_repeated_keys_keep_the_first_value(head, block):
    # tools.parse keeps every value of a repeated key (a list, in text order): fields_of keeps the first.
    _, every = tools.parse(block)
    every.pop("op", None)
    got = protocol.fields_of(block)
    assert got.keys() == every.keys()
    for key, value in every.items():
        assert got[key] == (value[0] if isinstance(value, list) else value), key


KEYS = {
    "slots": ("page", "slot_id", "doctor_id", "facility", "procedure", "at", "day", "free"),
    "agenda": ("agenda_now", "epoch_real", "epoch_agenda", "rate", "paused"),
    "prescription": ("prescription_number", "patient", "procedure", "priority", "state"),
    "rules": ("count",),
    "refused": ("code", "detail"),
    "held": ("hold_id", "slot_id", "need_id", "consent_ref"),
    "committed": ("booking_id", "ledger_ref", "slot_id", "agenda_date", "signature"),
    "checked_in": ("booking_id", "checkin_id", "ledger_ref", "agenda_time"),
    "reported": ("booking_id", "report_id", "ledger_ref"),
}


@pytest.mark.parametrize(("head", "block"), ALL)
def test_key_tokens(head, block):
    got = protocol.fields_of(block)
    assert set(KEYS[head]) <= got.keys()
    assert protocol.ledger([msg(LEDGER, block)], head) == got | (
        {"agenda_now": got["agenda_now"]} if head == "agenda" else {})


def test_slots_pages_and_first_slot():
    for block in blocks("slots"):
        lines = block.splitlines()
        n, m = protocol.page(block)
        assert f"page={n}/{m}" in lines[0].split() and 1 <= n <= m
        assert protocol.fields_of(block)["slot_id"] == protocol.fields_of(lines[1])["slot_id"]
        assert protocol.when(protocol.fields_of(block)["at"]) is not None


def test_held_names_its_hold_twice():
    for block in blocks("held"):
        first, second = (line for line in block.splitlines())
        assert protocol.fields_of(first)["hold_id"] == protocol.fields_of(second)["hold_id"]
        assert protocol.fields_of(block)["hold_id"] == protocol.fields_of(first)["hold_id"]


def test_refused_names_its_operation():
    for block in blocks("refused"):
        op = block.split()[1]
        assert op in {"hold", "commit", "checkin"} and op in protocol.words(block)


def test_prescription_names_the_citizen():
    for block in blocks("prescription"):
        assert protocol.citizen([msg(LEDGER, block)]) == protocol.fields_of(block)["patient"]


def test_agenda_now_and_clock():
    for block in blocks("agenda"):
        reply = protocol.ledger([msg(LEDGER, block)], "agenda")
        assert protocol.when(reply["agenda_now"]).tzinfo is not None
    assert protocol.ledger([msg(LEDGER, "agenda agenda_date=2026-12-01T10:00:00Z")], "agenda")["agenda_now"] == (
        "2026-12-01T10:00:00Z")


def test_act_outside_the_ledger():
    assert protocol._ACTS == sdk._ACTS  # our copy of the SDK's private act words
    assert protocol.act(msg(CITIZEN, "Buongiorno, confirm offer_id=of-S1")) == "confirm"
    assert protocol.act(msg(ME, "data.request purpose=booking ... your prescription", to=CITIZEN)) == "data.request"
    assert protocol.act(msg(CITIZEN, "Help, agenda?")) == "need.request"  # Ledger words only with the Ledger
    assert protocol.act(msg(ME, "agenda", to=LEDGER)) == "agenda"
    assert protocol.act(msg(ME, "world procedure=VIS-FIS page=2", to=LEDGER)) == "world"
    assert protocol.act(msg(CITIZEN, "", kind="confirm")) == "confirm"


def test_when():
    assert protocol.when("2026-12-01T09:00:00.000Z").isoformat() == "2026-12-01T09:00:00+00:00"
    for bad in ("", "2026-12-01T09:00:00", "domani", "2026-12-01"):
        assert protocol.when(bad) is None


def test_fields_reads_the_body_and_the_text_wins():
    row = msg(CITIZEN, "confirm lang=it", provided={"lang": "en", "days": "tue"}, status="wip")
    assert protocol.fields(row) == {"lang": "it", "days": "tue", "status": "wip"}


def test_citizen():
    gp = msg("agent:gp", "need.request handoff=gp prescription_number=X", citizen=CITIZEN)
    assert protocol.citizen([msg(LEDGER, "prescription patient=citizen:other"), gp]) == CITIZEN
    assert protocol.citizen([gp, msg(CITIZEN, "ciao")]) == CITIZEN
    assert protocol.citizen([msg(LEDGER, "rules count=0")]) is None


def test_we_exact_asked():
    chat = [msg(ME, "offer offer_id=of-S1 slot_id=S1", to=CITIZEN), msg(ME, "world procedure=VIS-FIS page=2", to=LEDGER)]
    assert protocol.we(chat, ME, "offer_id=of-S1", to=CITIZEN, word="offer")
    assert not protocol.we(chat, ME, "offer_id=of-S1", to=LEDGER, word="offer")
    assert protocol.exact(chat, ME, "world procedure=VIS-FIS page=2")
    assert protocol.asked(chat, ME, "VIS-FIS", 2) and not protocol.asked(chat, ME, "VIS-FIS", 1)


def test_offer_line():
    slot = {"slot_id": "S1", "doctor_id": "D-h1", "facility": "agent:h", "procedure": "VIS-FIS",
            "at": "2026-12-01T10:30:00.000Z", "day": "tue"}
    assert protocol.offer_line(slot, "N1", "B") == (
        "offer offer_id=of-S1 slot_id=S1 doctor_id=D-h1 facility=agent:h procedure=VIS-FIS "
        "agenda_date=2026-12-01T10:30:00.000Z agenda_day=tue expiry=2026-12-01T10:30:00.000Z "
        "priority=B prescription_number=N1")
    del slot["procedure"], slot["day"]
    assert protocol.offer_line(slot, None, None) == (
        "offer offer_id=of-S1 slot_id=S1 doctor_id=D-h1 facility=agent:h "
        "agenda_date=2026-12-01T10:30:00.000Z expiry=2026-12-01T10:30:00.000Z")


# --- ledger_answer: paired by sent_at, not by place in history ---------------------------------

ASK = msg(ME, "prescription prescription_number=N1", to=LEDGER, at="2026-11-21T02:00:00Z", mid="ask")
ISSUED = msg(LEDGER, "prescription prescription_number=N1 procedure=VIS-FIS priority=B state=issued",
             at="2026-11-21T02:05:00Z", mid="issued")


def test_answer_listed_before_the_citizen_message():
    # Live N-…0260: the citizen wrote the number again while our read was in flight; the Relay
    # listed the answer before that message, and "answers after the citizen" missed it.
    again = msg(CITIZEN, "prescription_number=N1", at="2026-11-21T02:01:00Z", mid="again")
    assert protocol.ledger_answer([ASK, ISSUED, again], ASK) is ISSUED
    assert protocol.ledger_answer([ISSUED, again, ASK], ASK) is ISSUED  # even listed before the ask


def test_answer_older_than_the_ask_or_for_another_number():
    old = msg(LEDGER, ISSUED.text, at="2026-11-21T01:59:00Z", mid="old")
    other = msg(LEDGER, "prescription prescription_number=N2 state=issued", at="2026-11-21T02:06:00Z", mid="n2")
    assert protocol.ledger_answer([old, ASK, other], ASK) is None


def test_answer_refused_latest_and_without_times():
    refused = msg(LEDGER, "refused prescription code=NOT_FOUND", at="2026-11-21T02:07:00Z", mid="ref")
    assert protocol.ledger_answer([ASK, ISSUED, refused], ASK) is refused
    assert protocol.ledger_answer([ASK, msg(LEDGER, "refused hold code=EXPIRED", at="2026-11-21T02:08:00Z")], ASK) is None
    ask, reply = msg(ME, "agenda", to=LEDGER, mid="a"), msg(LEDGER, blocks("agenda")[0], mid="r")
    assert protocol.ledger_answer([reply, ask], ask) is None and protocol.ledger_answer([ask, reply], ask) is reply


def test_answer_to_a_world_page():
    page1, page2 = blocks("slots")[0], blocks("slots")[0].replace("page=1/8", "page=2/8", 1)
    ask = msg(ME, "world procedure=VIS-FIS page=2", to=LEDGER, at="2026-11-21T02:00:00Z", mid="w")
    one = msg(LEDGER, page1, at="2026-11-21T02:04:00Z", mid="p1")
    two = msg(LEDGER, page2, at="2026-11-21T02:03:00Z", mid="p2")
    assert protocol.ledger_answer([ask, two, one], ask) is two


# --- until -------------------------------------------------------------------------------------


class History:
    def __init__(self, ready_at: int, fail: int = 0) -> None:
        self.reads, self.ready_at, self.fail = 0, ready_at, fail

    async def history(self, *, peer, need_id):
        self.reads += 1
        if self.reads <= self.fail:
            raise CapError("POLICY_VIOLATION", "relay history")
        return [msg(LEDGER, "agenda")] if self.reads >= self.ready_at else []


def until(port: History, **kw) -> list[Message]:
    city = City(index=None, messages=None, history=port, agent_id=ME)
    return asyncio.run(protocol.until(city, "N1", ["old"], lambda rows: bool(rows) and rows != ["old"], pause=0, **kw))


def test_until_tries_deadline_and_failed_reads():
    port = History(ready_at=3)
    assert until(port, tries=5)[0].text == "agenda" and port.reads == 3
    port = History(ready_at=9)
    assert until(port, tries=2) == [] and port.reads == 2
    port = History(ready_at=3)  # a deadline ignores tries
    assert until(port, tries=1, deadline=time.monotonic() + 5)[0].text == "agenda" and port.reads == 3
    port, seen = History(ready_at=9, fail=1), []
    assert until(port, tries=1, on_read=seen.append) == ["old"] and seen == [["old"]]  # a refused read is chat


# --- say ---------------------------------------------------------------------------------------


class Relay:
    def __init__(self, *errors: BaseException | None) -> None:
        self.errors, self.posts = list(errors), 0

    async def post(self, *, to, message_type, body, need_id, conversation_id, traceparent) -> str:
        self.posts += 1
        error = self.errors.pop(0) if self.errors else None
        if error is not None:
            raise error
        return f"id-{self.posts}"


@pytest.fixture
def said(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(protocol.tools, "said", lambda to, text, msg_id=None, *, error=None: calls.append(
        (to, text, msg_id, error)))
    return calls


def say(relay: Relay, to: str = LEDGER, **kw) -> str:
    city = City(index=None, messages=relay, agent_id=ME, inbound=msg(CITIZEN, "ciao", mid="in"))
    return asyncio.run(protocol.say(city, to, "agenda", **kw))


def test_say_records_the_msg_id(said):
    assert say(Relay()) == "id-1" and said == [(LEDGER, "agenda", "id-1", None)]


@pytest.mark.parametrize("code", ["TARGET_NOT_VISIBLE", "SCHEMA_INVALID", "POLICY_VIOLATION"])
def test_say_never_retries_a_cap_error(said, code):
    relay = Relay(CapError(code, "no"))
    assert say(relay, retry=True) == "" and relay.posts == 1
    assert said == [(LEDGER, "agenda", None, code)]


@pytest.mark.parametrize("error", [httpx.ReadTimeout("relay send"), TimeoutError("relay"), httpx.ConnectError("x")])
def test_say_timeout_never_escapes(said, error):
    relay = Relay(error)
    assert say(relay) == "" and relay.posts == 1
    assert said == [(LEDGER, "agenda", None, type(error).__name__)]


def test_say_retries_a_timeout_once_when_asked(said):
    relay = Relay(httpx.ReadTimeout("relay send"))
    assert say(relay, retry=True) == "id-2" and relay.posts == 2 and said == [(LEDGER, "agenda", "id-2", None)]
    relay = Relay(httpx.ReadTimeout("a"), httpx.ReadTimeout("b"))
    assert say(relay, retry=True) == "" and relay.posts == 2


def test_say_bounced_citizen_is_not_tried_again(said):
    bounced: dict = {}
    relay = Relay(CapError("TARGET_NOT_VISIBLE", CITIZEN))
    assert say(relay, to=CITIZEN, bounced=bounced) == "" and list(bounced) == [("", CITIZEN, "agenda")]
    assert say(relay, to=CITIZEN, bounced=bounced) == "" and relay.posts == 1  # within _BOUNCE_TTL
    relay = Relay(CapError("TARGET_NOT_VISIBLE", LEDGER))
    assert say(relay, bounced=bounced) == "" and len(bounced) == 1  # only citizens are remembered
