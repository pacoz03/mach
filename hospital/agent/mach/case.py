"""Lo stato del caso, ricavato dalla history: funzioni pure, senza rete né stato di modulo.

Fonte unica in ``shared/mach/case.py``; ``shared/sync.sh`` la copia in ``<agente>/agent/mach/``:
non modificare le copie. Solo stdlib e ``city_sdk``. ``chat`` è ``city.history(need_id=...)``,
``agent`` il nostro id. Dove il brain di esempio del kit ha la funzione (``templates/examples/agent.py``:
``_number``, ``_lang``, ``_gender``, ``_offered``, ``_withdrawn``, ``_our_offer``, ``_booking_of``,
``_our_commit``) il nome è lo stesso, senza il trattino basso. Il cittadino del caso e le risposte
del Ledger per atto stanno in ``protocol`` (``citizen``, ``ledger``, ``ledger_answer``).
"""

from __future__ import annotations

import re
from collections.abc import Callable

from city_sdk.city import LEDGER, Message, is_citizen

from . import protocol
from .protocol import fields, fields_of, first_word, words

# A citizen message -> what its free text says ("days": frozenset, "gender": "f"/"m", "lang"),
# read by the caller (the booking's rules and model). The tokens always come first.
Heard = Callable[[Message], dict[str, object]]
_WEEK = frozenset({"mon", "tue", "wed", "thu", "fri"})
_IT_WEEK = {"lun": "mon", "mar": "tue", "mer": "wed", "gio": "thu", "ven": "fri"}
# A number written in prose by the citizen ("la ricetta prescription_number-ab12...").
_RX = re.compile(r"(?<![\w-])prescription_number-([0-9a-f]{24})(?![\w-])", re.IGNORECASE)


def _after(chat: list[Message], row: Message, mark: Message) -> bool:
    """``row`` was sent at or after ``mark``: by sent_at, as protocol.ledger_answer; by place in
    history when either has none (a mark history does not show: True)."""

    at, start = protocol.when(row.sent_at or ""), protocol.when(mark.sent_at or "")
    if at is not None and start is not None:
        return at >= start
    ids = [item.msg_id for item in chat]
    if mark.msg_id not in ids:
        return True
    return row.msg_id in ids and ids.index(row.msg_id) > ids.index(mark.msg_id)


# --- the gp hand-off ---------------------------------------------------------------------------


def _is_handoff(row: Message) -> bool:
    """Our gp's hand-off: ``need.request handoff=gp citizen=... prescription_number=...`` (gp
    ``_handoff``; a gp -> booking row the kit's table R:183-199 does not have)."""

    return row.sender.startswith("agent:") and fields(row).get("handoff") == "gp"


def handoff(chat: list[Message], number: str | None = None, *, gp: str | None = None) -> Message | None:
    """The latest gp hand-off of the case, for prescription ``number`` and from ``gp`` when given."""

    found = None
    for row in chat:
        if _is_handoff(row) and gp in (None, row.sender) and number in (None, fields(row).get("prescription_number")):
            found = row
    return found


def handed(chat: list[Message], number: str, gp: str) -> dict[str, str] | None:
    """Procedure and priority of ``number`` from ``gp``'s hand-off (it wrote the prescription on the
    Ledger just before), as an issued prescription; None without them."""

    row = handoff(chat, number, gp=gp)
    found = fields(row) if row is not None else {}
    if found.get("procedure") and found.get("priority"):
        return {"procedure": found["procedure"], "priority": found["priority"], "state": "issued"}
    return None


# --- the prescription --------------------------------------------------------------------------


def number(chat: list[Message]) -> str | None:
    """The case's prescription number, latest row first: a citizen's ``prescription_number=``
    (never one with spaces), else the one number their prose names (two: None); or the hand-off's.
    A number the Ledger says is another patient's is skipped: an injected one never takes the case."""

    foreign = others(chat)
    for row in reversed(chat):
        if is_citizen(row.sender):
            found = fields(row).get("prescription_number")
            if found and not any(character.isspace() for character in found) and found not in foreign:
                return found
            mentions = {f"prescription_number-{match.upper()}" for match in _RX.findall(row.text)} - foreign
            if len(mentions) == 1:
                return mentions.pop()
            if mentions:
                return None
        elif _is_handoff(row) and fields(row).get("prescription_number"):
            return fields(row)["prescription_number"]
    return None


def others(chat: list[Message]) -> set[str]:
    """Prescription numbers the Ledger answered with another ``patient=`` than the case's citizen."""

    citizen = protocol.citizen(chat)
    return {
        got.get("prescription_number", "")
        for row in chat
        if row.sender == LEDGER and first_word(row.text) == "prescription"
        and (got := fields_of(row.text)).get("patient", citizen) != citizen
    }


def named(chat: list[Message], number: str) -> Message | None:
    """The citizen's latest message with the token ``prescription_number=<number>``."""

    return next(
        (row for row in reversed(chat) if is_citizen(row.sender) and fields(row).get("prescription_number") == number),
        None,
    )


def _asks(chat: list[Message], agent: str, number: str) -> list[Message]:
    return [
        row
        for row in chat
        if row.sender == agent and row.recipient == LEDGER and first_word(row.text) == "prescription"
        and fields_of(row.text).get("prescription_number") == number
    ]


def asked(chat: list[Message], agent: str, number: str, since: Message | None = None) -> bool:
    """We asked the Ledger ``prescription`` for ``number`` (at or after ``since``, when given)."""

    return any(since is None or _after(chat, ask, since) for ask in _asks(chat, agent, number))


def prescription(
    chat: list[Message], agent: str, number: str, since: Message | None = None
) -> dict[str, str] | None:
    """Tokens of the Ledger's latest answer to our ``prescription`` asks for ``number`` (a
    ``refused prescription`` too: an answer with no procedure), paired by sent_at
    (protocol.ledger_answer), never by place in history: the Relay can list it before a citizen
    message sent earlier (booking 1.0.14, N-…0260). With ``since``, only an answer sent at or after
    that message. None: not answered yet."""

    found = None
    for ask in _asks(chat, agent, number):
        answer = protocol.ledger_answer(chat, ask)
        if answer is not None and (since is None or _after(chat, answer, since)):
            found = answer
    return None if found is None else fields_of(found.text)


# --- offers ------------------------------------------------------------------------------------


def offers_in(text: str) -> list[dict[str, str]]:
    """The offers of one message: each ``offer_id=`` starts the next one."""

    found: list[dict[str, str]] = []
    for token in text.replace(",", " ").split():
        key, eq, value = token.partition("=")
        value = value.strip(".,;:!?")
        if not eq or not key or not value:
            continue
        if key == "offer_id":
            found.append({})
        if found and key not in found[-1]:
            found[-1][key] = value
    return found


def _our_offers(chat: list[Message], agent: str) -> list[dict[str, str]]:
    return [offer for row in chat if row.sender == agent and "offer" in words(row.text) for offer in offers_in(row.text)]


def offered(chat: list[Message], agent: str) -> set[str]:
    """Slots we offered in the case."""

    return {offer["slot_id"] for offer in _our_offers(chat, agent) if offer.get("slot_id")}


def withdrawn(chat: list[Message], agent: str) -> set[str]:
    """Slots of our offers that someone cancelled (``cancel offer_id=``). The citizen drops the
    rest, so those are offered again."""

    by_offer = {
        offer["offer_id"]: offer["slot_id"]
        for offer in _our_offers(chat, agent)
        if offer.get("offer_id") and offer.get("slot_id")
    }
    return {
        by_offer[offer_id]
        for row in chat
        if "cancel" in words(row.text) and (offer_id := fields_of(row.text).get("offer_id", "")) in by_offer
    }


def our_offer(chat: list[Message], agent: str, offer_id: str) -> dict[str, str]:
    """Tokens of our first offer ``offer_id``; {} when we made none."""

    return next((offer for offer in _our_offers(chat, agent) if offer.get("offer_id") == offer_id), {})


# --- consents ----------------------------------------------------------------------------------


def consent_offer(chat: list[Message], agent: str, citizen: str) -> str:
    """The offer of our ``consent`` request while it is our latest word to the citizen and no
    ``confirm`` for it left yet; "" otherwise."""

    found = ""
    for row in chat:
        if row.sender == agent and row.recipient == citizen:
            found = fields_of(row.text).get("offer_id", "") if first_word(row.text) == "consent" else ""
    if found and any(
        row.sender == agent and first_word(row.text) == "confirm" and fields_of(row.text).get("offer_id") == found
        for row in chat
    ):
        return ""
    return found


def booking_consent(chat: list[Message], citizen: str, offer_id: str) -> str:
    """The latest ``consent_ref`` the citizen gave with ``offer_id``, other than a cross-ASST one."""

    found = ""
    for row in chat:
        if row.sender == citizen:
            got = fields(row)
            if got.get("offer_id") == offer_id and got.get("consent_ref") and got.get("purpose") != "cross_asst":
                found = got["consent_ref"]
    return found


def cross_consent(chat: list[Message], citizen: str) -> str:
    """The citizen's latest ``consent_ref`` with ``purpose=cross_asst`` (rule P2)."""

    found = ""
    for row in chat:
        if row.sender == citizen:
            got = fields(row)
            if got.get("purpose") == "cross_asst" and got.get("consent_ref"):
                found = got["consent_ref"]
    return found


# --- hold and commit (the facility's side) -----------------------------------------------------


def booking_of(chat: list[Message], agent: str, slot_id: str) -> tuple[str, str, str | None]:
    """Who asked us to book this slot, the consent, and the prescription, if we have them."""

    token = f"slot_id={slot_id}"
    sender, consent, found = "", "", None
    for row in chat:
        if token not in row.text.split():
            continue
        got = fields_of(row.text)
        if "confirm" in words(row.text) and row.sender not in (LEDGER, agent):
            sender = row.sender
            consent = got.get("consent_ref", consent)
            found = got.get("prescription_number") or found
        if row.sender == agent and "hold" in words(row.text):
            consent = consent or got.get("consent_ref", "")
            found = got.get("prescription_number") or found
            sender = sender or got.get("citizen", "")
    return sender, consent, found


def our_commit(chat: list[Message], agent: str, hold_id: str) -> bool:
    """A commit we sent the Ledger for ``hold_id``. The Ledger's held line also says commit."""

    token = f"hold_id={hold_id}"
    return any(
        row.sender == agent and row.recipient == LEDGER and row.text.strip().startswith("commit ")
        and token in row.text.split()
        for row in chat
    )


# --- the citizen's preferences and language ----------------------------------------------------
# The citizen's own tokens, then their free text (``heard``), then what our gp passed on in the
# hand-off (gp ``_preference_tokens``: ``doctor_lang``, ``gender_preference``, ``days``).


def lang(chat: list[Message], heard: Heard | None = None, *, doctor: bool = False) -> str:
    """The case's language: a citizen's ``lang=``, else their free text, else the hand-off's
    ``lang`` ("it" without any). ``doctor``: the language the doctor should speak, so the
    hand-off's ``doctor_lang`` comes before its ``lang`` (the language the gp spoke with them)."""

    for row in chat:
        if is_citizen(row.sender) and (found := fields(row).get("lang", "")):
            return found
    if heard is not None:
        for row in chat:
            if is_citizen(row.sender):
                found = heard(row).get("lang")
                if isinstance(found, str) and found:
                    return found
    row = handoff(chat)
    got = fields(row) if row is not None else {}
    for key in ("doctor_lang", "lang") if doctor else ("lang",):
        if got.get(key):
            return got[key]
    return "it"


def _gender_token(row: Message) -> str | None:
    got = fields(row)
    raw = got.get("gender_preference") or got.get("gender")
    return raw if raw in {"m", "f"} else None


def gender(chat: list[Message], heard: Heard | None = None) -> str | None:
    """The doctor's gender the citizen asked for: a token (first row first), else their free text
    (latest first), else the hand-off's ``gender_preference``."""

    for row in chat:
        if is_citizen(row.sender) and (found := _gender_token(row)):
            return found
    if heard is not None:
        for row in reversed(chat):
            if is_citizen(row.sender) and (said := heard(row).get("gender")) in {"m", "f"}:
                return str(said)
    row = handoff(chat)
    return _gender_token(row) if row is not None else None


def _days_token(row: Message) -> frozenset[str] | None:
    """``days`` of one row as mon..fri (body, ``provided``, or ``days=mon,wed`` in the text: the
    commas would split it for fields_of); None when the row names none."""

    raw = row.body.get("days")
    provided = row.body.get("provided")
    if isinstance(provided, dict):
        raw = provided.get("days", raw)
    if not isinstance(raw, (str, list)):
        match = re.search(r"(?<!\S)days=([a-z]+(?:\s*[/,|;]\s*[a-z]+)*)", row.text, re.IGNORECASE)
        raw = match.group(1) if match else None
    if not raw:
        return None
    names = raw if isinstance(raw, list) else re.split(r"[/,|;]", raw)
    short = {str(name).strip().lower()[:3] for name in names}
    return frozenset({_IT_WEEK.get(day, day) for day in short} & _WEEK)


def days(chat: list[Message], heard: Heard | None = None) -> frozenset[str]:
    """Agenda days the citizen prefers as mon..fri: their latest message that says (a token, else
    the free text), else the hand-off's ``days``; empty when nobody said."""

    for row in reversed(chat):
        if not is_citizen(row.sender):
            continue
        found = _days_token(row)
        if found is not None:
            return found
        said = heard(row).get("days") if heard is not None else None
        if isinstance(said, frozenset) and said:
            return said
    row = handoff(chat)
    return (_days_token(row) if row is not None else None) or frozenset()
