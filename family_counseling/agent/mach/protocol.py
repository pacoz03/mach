"""Il protocollo comune: leggere i messaggi della città, parlare, rileggere il caso.

Fonte unica in ``shared/mach/protocol.py``; ``shared/sync.sh`` la copia in ``<agente>/agent/mach/``:
non modificare le copie. Solo stdlib e ``city_sdk``. Viene dalla sezione "shared" del brain di
esempio del kit (``templates/examples/agent.py``): stessi nomi, senza il trattino basso.
Lo stato del caso (ricetta, offerte, consensi, preferenze) non sta qui: step 2 del piano.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime

import tools
from city_sdk.city import LEDGER, CapError, City, Message, agenda_time, is_citizen, understood

_LOG = logging.getLogger("care")
# Act words the city reads (city_sdk.city understood()).
_ACTS = frozenset({
    "need.request", "offer", "confirm", "cancel", "prescription", "consent", "data.request",
    "data.response", "checkin", "report", "booking.receipt", "escalate.emergency", "hold",
    "case.closed",
})
# Our request to the Ledger -> the heads of its answer (SKILL.md, tests/fixtures; `world` is the
# head of the SDK format, `world slots=N`). A refusal is `refused <request> code=...`.
_ANSWER = {
    "world": {"slots", "world"}, "agenda": {"agenda"}, "rules": {"rules"},
    "prescribe": {"prescribed"}, "prescription": {"prescription"}, "hold": {"held"},
    "commit": {"committed"}, "cancel": {"cancelled"}, "checkin": {"checked_in"},
    "report": {"reported"},
}
# Words that open a message only to or from the Ledger: its requests and its answers. A citizen
# writing "Help, ..." or "Agenda?" is not naming a Ledger operation.
_LEDGER_WORDS = frozenset(
    {*_ANSWER, *set().union(*_ANSWER.values()), "refused", "expired", "catalogue", "remember",
     "recall", "history", "help"}
)
# Ids that tie a Ledger answer to its request when both name them.
_IDS = ("prescription_number", "hold_id", "booking_id", "slot_id")
_BOUNCE_TTL = 60.0


# --- reading a message -------------------------------------------------------------------------


def words(text: str) -> set[str]:
    return {token.strip(".,;:!?()\"'").lower() for token in text.split() if token.strip()}


def first_word(text: str) -> str:
    parts = text.split()
    return parts[0].strip(".,;:!?()\"'").lower() if parts else ""


def fields_of(text: str) -> dict[str, str]:
    """``key=value`` tokens. A repeated key keeps its FIRST value (one message, several offers or
    slot lines: the first one speaks for the message); trailing ``.,;:!?`` and empty values drop."""

    found: dict[str, str] = {}
    for token in text.replace(",", " ").split():
        key, eq, value = token.partition("=")
        if not eq or not key or not value:
            continue
        value = value.strip(".,;:!?")
        if key not in found and value:
            found[key] = value
    return found


def fields(row: Message) -> dict[str, str]:
    """``key=value`` tokens of the text, plus scalar body fields of a structured message
    (``body["provided"]`` too). The text wins."""

    found = fields_of(row.text)
    provided = row.body.get("provided")
    body = {**row.body, **provided} if isinstance(provided, dict) else row.body
    for key, value in body.items():
        if key != "text" and isinstance(value, str) and value and key not in found:
            found[key] = value
    return found


def act(message: Message) -> str:
    """The act a message opens with: its first word when that is an act word (a Ledger request or
    answer only on a message to or from the Ledger), else ``understood(message).type``.
    ``understood`` alone reads the LAST act word ("data.request ... your prescription" became
    ``prescription``) and knows none of the Ledger's answers (``refused hold`` became ``hold``)."""

    head = first_word(message.text)
    if head in _ACTS or (head in _LEDGER_WORDS and LEDGER in (message.sender, message.recipient)):
        return head
    return understood(message).type


def when(value: str) -> datetime | None:
    """An aware timestamp in UTC (the SDK's ``agenda_time``), or None: empty, not ISO 8601, or
    without a zone."""

    try:
        return agenda_time(value)
    except CapError:
        return None


def page(text: str) -> tuple[int, int]:
    """``page=N/M`` of a world answer's first line; (1, 1) without one."""

    raw = fields_of(text.splitlines()[0] if text else "").get("page", "")
    left, _, right = raw.partition("/")
    if left.isdigit() and right.isdigit():
        return int(left), int(right)
    return 1, 1


def offer_line(slot: dict[str, str], number: str | None, priority: str | None) -> str:
    """One ``offer`` line for a slot (R:95). ``procedure``, ``at`` and ``day`` are written only when
    the slot has them; ``at`` is also the offer's ``expiry``, so it must be there."""

    parts = [
        "offer",
        f"offer_id=of-{slot['slot_id']}",
        f"slot_id={slot['slot_id']}",
        f"doctor_id={slot['doctor_id']}",
        f"facility={slot['facility']}",
    ]
    for key, name in (("procedure", "procedure"), ("at", "agenda_date"), ("day", "agenda_day")):
        if slot.get(key):
            parts.append(f"{name}={slot[key]}")
    parts.append(f"expiry={slot['at']}")
    if priority:
        parts.append(f"priority={priority}")
    if number:
        parts.append(f"prescription_number={number}")
    return " ".join(parts)


# --- reading the case history ------------------------------------------------------------------


def citizen(chat: list[Message]) -> str | None:
    """The case's citizen: the first who wrote; else the one our GP's hand-off names
    (``need.request handoff=gp citizen=...``); else a ``patient=``/``citizen=`` token."""

    for row in chat:
        if is_citizen(row.sender):
            return row.sender
    for row in chat:
        found = fields(row)
        handoff = row.sender.startswith("agent:") and found.get("handoff") == "gp"
        if handoff and is_citizen(found.get("citizen", "")):
            return found["citizen"]
    for row in chat:
        found = fields_of(row.text)
        for key in ("patient", "citizen"):
            if is_citizen(found.get(key, "")):
                return found[key]
    return None


def we(chat: list[Message], agent: str, token: str, *, to: str, word: str) -> bool:
    """True when we already sent ``to`` a message with this exact token and act word."""

    for row in chat:
        if row.sender != agent or row.recipient != to:
            continue
        if token in row.text.split() and word in words(row.text):
            return True
    return False


def exact(chat: list[Message], agent: str, text: str) -> bool:
    """True when we already said exactly ``text``, to anyone."""

    return any(row.sender == agent and row.text.strip() == text for row in chat)


def asked(chat: list[Message], agent: str, procedure: str, page: int) -> bool:
    """True when we already asked for this page of this procedure (any of our messages)."""

    tokens = (f"page={page}", f"procedure={procedure}")
    return any(row.sender == agent and all(t in row.text.split() for t in tokens) for row in chat)


def ledger(chat: list[Message], head: str) -> dict[str, str] | None:
    """Tokens of the last Ledger row with this head, in history order; an ``agenda`` answer
    without ``agenda_now`` gets its ``agenda_date``. Not paired to a request: see ledger_answer."""

    found: dict[str, str] | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == head:
            found = fields_of(row.text)
    if found is not None and head == "agenda":
        found.setdefault("agenda_now", found.get("agenda_date", ""))
    return found


def ledger_answer(chat: list[Message], ask: Message) -> Message | None:
    """The Ledger's latest answer to our request ``ask`` (its head, or ``refused <request>``) sent
    after it, by ``sent_at``. Never by place in history: the Relay can list an answer before a
    message sent earlier (booking 1.0.14, N-…0260: an issued prescription read as "not found").
    The ids the ask names (``_IDS``, and the world ``page``) must match where the answer names
    them. Without a ``sent_at`` on either side, the rows after the ask in history count."""

    op = first_word(ask.text)
    heads = _ANSWER.get(op, set())
    wanted = fields_of(ask.text)
    start = when(ask.sent_at or "")
    after, found = False, None
    for row in chat:
        if row.msg_id == ask.msg_id:
            after = True
            continue
        head = first_word(row.text)
        if row.sender != LEDGER or not (head in heads or head == "refused" and op in words(row.text)):
            continue
        at = when(row.sent_at or "")
        if not (at >= start if at is not None and start is not None else after):
            continue
        got = fields_of(row.text)
        if any(got.get(key, wanted[key]) != wanted[key] for key in _IDS if key in wanted):
            continue
        if op == "world" and wanted.get("page", "").isdigit() and page(row.text)[0] != int(wanted["page"]):
            continue
        found = row
    return found


async def until(
    city: City,
    need_id: str | None,
    chat: list[Message],
    ready: Callable[[list[Message]], bool],
    tries: int = 24,
    deadline: float | None = None,
    *,
    pause: float = 0.25,
    on_read: Callable[[list[Message]], None] | None = None,
) -> list[Message]:
    """Re-read the case until ``ready``: ``tries`` reads, or with ``deadline`` (time.monotonic())
    every read until then, ``tries`` ignored. The push may never arrive. A CapError read counts
    as ``chat``; ``on_read`` sees every read."""

    fresh = chat
    attempt = 0
    while True:
        try:
            fresh = await city.history(need_id=need_id)
        except CapError:
            fresh = chat
        if on_read is not None:
            on_read(fresh)
        attempt += 1
        if ready(fresh) or (attempt >= tries if deadline is None else time.monotonic() >= deadline):
            return fresh
        await asyncio.sleep(pause)


# --- speaking ----------------------------------------------------------------------------------


async def say(
    city: City,
    to: str,
    text: str,
    *,
    retry: bool = False,
    bounced: dict[tuple[str, str, str], float] | None = None,
) -> str:
    """Say ``text`` to ``to``: the msg_id, or "" when it did not go. Never raises: an exception
    out of the brain makes the harness redeliver the inbound message, and every say of the turn
    goes out twice (harness.py:455-480). Each try is recorded with ``tools.said``, with ``error=``
    (the CapError code or the exception's name) when it fails.

    - A CapError is final, never sent again: the SDK turns every HTTP error into one (http.py
      ``_cap_http``), TARGET_NOT_VISIBLE and the 4xx included.
    - A network error (the Relay's httpx timeouts) is sent once more only with ``retry``. city.say
      is not idempotent: each call signs a new envelope with a fresh msg_id (http.py
      ``HttpRelay.post``, uuid4) and the SDK re-sends only history reads, which "write nothing"
      (http.py:221). A ReadTimeout can come after the Relay took the message, so a retry can say
      it twice. Only the hospital asks for it (1.0.9; tests/journey.py ``ospedale_relay_timeout``).
    - ``bounced``: (need_id, to, text) -> time.monotonic() of a TARGET_NOT_VISIBLE to a citizen.
      The same text to the same citizen in the same case is not tried again for _BOUNCE_TTL s
      (the hospital's push turns, R:701). Only a citizen can stay invisible (another ASST's): the
      Ledger and agents bounce only while we are down, so a later turn means they are back.
    """

    key = (getattr(city.inbound, "need_id", None) or "", to, text)
    watch = bounced is not None and is_citizen(to)
    if watch and time.monotonic() - bounced.get(key, float("-inf")) < _BOUNCE_TTL:
        return ""
    for attempt in range(2 if retry else 1):
        try:
            msg_id = await city.say(to, text) or "sent"
        except CapError as exc:
            if watch and exc.code == "TARGET_NOT_VISIBLE":
                now = time.monotonic()
                for old in [k for k, at in bounced.items() if now - at >= _BOUNCE_TTL]:
                    del bounced[old]
                bounced[key] = now
            tools.said(to, text, error=exc.code)
            return ""
        except Exception as exc:  # noqa: BLE001 - the Relay's httpx timeouts must not end the turn
            if attempt == 0 and retry:
                continue
            _LOG.info("care.say.failed", extra={"error": type(exc).__name__})
            tools.said(to, text, error=type(exc).__name__)
            return ""
        tools.said(to, text, msg_id)
        return msg_id
    return ""
