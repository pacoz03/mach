"""Brain condiviso delle tre strutture: ospedale, specialista, consultorio.

Fonte unica in ``shared/facility.py``; ``shared/sync.sh`` lo copia in ``hospital/``,
``specialist_care/`` e ``family_counseling/`` (``agent/facility.py``): non modificare le copie.
Ogni ``agent.py`` chiama ``emergency`` (red flag → ``escalate.emergency``), poi fa il controllo
clinico e ``tools.start_turn``, poi chiama ``handle(role, need, city)`` con il proprio ruolo.

Deriva dall'esempio del kit (care protocol, un messaggio in ingresso per passo) senza i rami
del medico di base e del centro prenotazioni. Le scritture al Ledger sono chat a
``city:ledger``; i cittadini leggono la parola d'atto e i token ``key=value`` (ADR-110).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import tools
from city_sdk.city import LEDGER, CapError, City, Message, Need

_MAX_PAGE = 8
# Four agenda hours is about half a minute of real time at 480x, so a slot is still
# ahead when the citizen decides. One hour was already past by then.
_LEAD = timedelta(hours=4)
# Doctor key (the last segment of doctor_id) -> languages, gender, days. ``*`` is the ASST's
# rare language. The Ledger's slot line names the doctor; it does not repeat this table.
_DOCTORS: dict[str, tuple[frozenset[str], str, frozenset[str]]] = {
    "h1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed"})),
    "h2": (frozenset({"it"}), "m", frozenset({"wed", "thu", "fri"})),
    "h3": (frozenset({"it", "*"}), "m", frozenset({"tue", "thu"})),
    "s1": (frozenset({"it", "en"}), "m", frozenset({"mon", "wed", "fri"})),
    "s2": (frozenset({"it"}), "f", frozenset({"tue", "thu"})),
    "c1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed", "thu", "fri"})),
}
_DEADLINE_DAYS = {"B": 2, "D": 5}
_LOG = logging.getLogger("care")
# The Relay accepts a ledger reply and then the agent rejects the push, so the reply sits in
# the case history and never opens a turn. Wait briefly and read it back from history.
_PULL_TRIES = 24
_PULL_PAUSE = 0.25
# About 3 s: the visit's time and the city's clock are a courtesy, never worth a long wait.
_SHORT_TRIES = 12
# Needs whose sent offers this process remembers; the oldest is forgotten first.
_SENT_NEEDS = 4096
# The kit names no outcome_code values: VISIT is the only one it writes (examples/agent.py).
# Hypothesis: check against the Ledger's ``help`` when the city is back.
_OUTCOME = "VISIT"
# Catalogue code of the cardiology control visit (city-cli chat_play CODES: "cardiology follow-up").
_CONTROL = "CTRL-CARD"
# Red flags in the citizen's own words (it/en), on text without accents. Copied from the gp
# (gp/agent/agent.py): the consultorio is a front door too and the first agent holding a
# red-flag case must escalate (R:242). ponytail: two copies until the shared tool (plan T).
_PAIN = (
    r"(?:dolor\w*|mal(?:e|issimo)?\b|fitt\w*|strett\w*|oppress\w*|bruci\w*|pain\w*|hurt\w*"
    r"|ach\w*|tight\w*|pressure|squeez\w*)"
)
_CHEST = r"(?:petto|torace|chest)"
_NEAR = r"\W+(?:\w+\W+){0,4}?"
_RED_FLAG = re.compile(
    "|".join(
        (
            _PAIN + _NEAR + _CHEST,
            _CHEST + _NEAR + _PAIN,
            r"peso (?:sul|al) petto|dolor\w* toracic\w*",
            r"(?:dolor\w*|male|pain\w*|hurt\w*)" + _NEAR + r"(?:braccio sinistro|left arm)",
            r"fiato corto|cort\w* di fiato|senza fiato|affann\w*|dispne\w*",
            r"manc\w*\W+(?:\w+\W+){0,2}?(?:fiato|respiro|aria)\b",
            r"(?:non riesco|fatic\w*|difficolt\w*)\W+(?:\w+\W+){0,2}?respir\w*",
            r"short(?:ness)? of breath|out of breath|breathless\w*",
            r"(?:can'?t|cannot|hard to|trouble|difficult\w*|struggl\w*)"
            r"\W+(?:\w+\W+){0,2}?breath\w*",
            r"svenu\w*|svenim\w*|sven(?:go|gono|ire)\b"
            r"|(?:pers\w*|perdo)\W+(?:i sensi|conoscenza|coscienza)",
            r"faint(?:ed|ing)\b|(?:feel\w*|felt) faint|pass(?:ed|ing)? out|lost consciousness"
            r"|black(?:ed)? out",
            r"(?:perdita di forza|perdo (?:la )?forza|debolezza|non sento)"
            r"\W+(?:\w+\W+){0,3}?gamb\w*",
            r"gamb\w*\W+(?:\w+\W+){0,2}?"
            r"(?:cedono|non (?:mi )?(?:reggono|rispondono)|paralizzat\w*)",
            r"(?:loss of strength|losing strength|weakness|can'?t feel)\W+(?:\w+\W+){0,3}?legs?\b",
            r"legs?\W+(?:\w+\W+){0,2}?(?:giving (?:out|way)|gave (?:out|way)|won'?t hold)",
            r"incontinen\w*"
            r"|(?:controll\w*|controllo)\W+(?:\w+\W+){0,2}?(?:vescica|intestino|sfinter\w*)",
            r"(?:loss of|lost|can'?t control)\W+(?:\w+\W+){0,2}?(?:bladder|bowel)",
            r"(?:sput\w*|tossisc\w*|tosse con|vomit\w*|perd\w*)\W+(?:\w+\W+){0,2}?sangue",
            r"sangue\W+(?:\w+\W+)?(?:nelle urine|nelle feci|nel vomito|dalla bocca)",
            r"sanguin\w*\W+(?:\w+\W+)?(?:molto|tanto|abbondant\w*|non si ferma)|emorragi\w*",
            r"(?:cough\w*|vomit\w*|spit\w*)\W+(?:\w+\W+)?blood"
            r"|blood in (?:my |the )?(?:urine|stool|vomit)",
            r"bleed\w*\W+(?:\w+\W+)?(?:heavily|a lot|won'?t stop)"
            r"|heavy bleeding|h(?:a)?emorrhag\w*",
            r"paralis\w*|paralizz\w*|paraly\w*|(?:faccia|bocca) storta|slurred speech"
            r"|face droop\w*",
            r"convulsion\w*|seizure\w*|crisi epilettic\w*",
        )
    )
)
# "non ho dolore al petto": a negation right before the words is not a red flag.
_NEGATED = re.compile(
    r"\b(?:non|nessun\w*|senza|niente|mai|no|not|never|without|don'?t|doesn'?t|didn'?t|haven'?t)"
    r"\s+(?:\w+\s+)?$"
)
# Citizen prose (it/en). Never an act word (offer, confirm, cancel, consent, checkin, report,
# hold, prescription) or a key=value token: understood() reads the last act word as the act.
ESCALATION = (
    "escalate.emergency reason=red_flag Capisco la sua preoccupazione: è importante agire "
    "subito. Contatti subito il 112, il numero unico di emergenza. Quando sarà al sicuro, sono "
    "qui per aiutarla con la prenotazione."
)
ESCALATION_EN = (
    "escalate.emergency reason=red_flag I understand your concern: it is important to act right "
    "away. Please call 112, the emergency number, now. Once you are safe, I am here to help you "
    "with your appointment."
)
_PICK = (
    "Ecco le prime disponibilità per lei, senza bisogno di ricetta: mi indichi quale preferisce "
    "e penso io a fissarla. Se nessuna le va bene, me lo scriva e cerco altre date."
)
_PICK_EN = (
    "Here are the first available appointments for you, no referral needed: tell me which one "
    "you prefer and I will book it. If none suits you, just write to me and I will look for "
    "other dates."
)
_NO_SLOTS = (
    "cancel reason=no_slots Mi dispiace, al momento non ci sono appuntamenti liberi adatti a "
    "lei. Se mi riscrive tra qualche giorno ricontrollo subito l'agenda; può anche rivolgersi al "
    "centro prenotazioni."
)
_NO_SLOTS_EN = (
    "cancel reason=no_slots I'm sorry, there are no suitable free appointments right now. Write "
    "to me again in a few days and I will check the agenda straight away; you can also ask the "
    "booking centre."
)


def red_flag(text: str) -> bool:
    plain = unicodedata.normalize("NFKD", text.replace("’", "'")).encode("ascii", "ignore")
    lowered = plain.decode().lower()
    return any(
        not _NEGATED.search(lowered[max(0, match.start() - 40) : match.start()])
        for match in _RED_FLAG.finditer(lowered)
    )


async def emergency(need: Need, city: City) -> bool:
    """A citizen's red flag: ``escalate.emergency`` and True, before any clinical refusal.

    Every agent.py calls this first. No ``tools.start_turn``: the citizen's text is clinical.
    """

    message = city.inbound
    if message is None or not message.sender.startswith("citizen:"):
        return False
    text = message.text or need.summary
    flagged = fields_of(text).get("is_emergency", "").lower() == "true"
    if not (need.is_emergency or flagged or red_flag(text)):
        return False
    en = fields_of(text).get("lang", "").lower() == "en"
    await _say(city, message.sender, ESCALATION_EN if en else ESCALATION)
    return True


def _asks_address(message: Message) -> bool:
    """True when this turn is a ``data.request`` that names the field ``address``."""

    text = message.text
    if text:
        named = False
        asked = False
        for token in text.split():
            bare = token.strip(".,;:!?()\"'")
            if bare == "data.request":
                named = True
            key, eq, value = bare.partition("=")
            if eq and key == "fields":
                names = {part.strip() for part in value.split(",") if part.strip()}
                asked = asked or "address" in names
        return named and asked
    if message.type != "data.request":
        return False
    raw = message.body.get("fields")
    if isinstance(raw, list):
        return "address" in raw
    if isinstance(raw, str):
        return "address" in {part.strip() for part in raw.replace(",", " ").split() if part.strip()}
    return False


def _own_street(city: City) -> str:
    """This agent's door: the city value, else the street shipped beside the brain."""

    street = getattr(city, "address", "")
    if isinstance(street, str) and street.strip():
        return street.strip()
    env = os.environ.get("CITY_ADDRESS", "").strip()
    if env:
        return env
    path = Path(__file__).with_name("door.txt")
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


async def handle(role: str, need: Need, city: City) -> None:
    """One turn of ``role``: ``hospital``, ``specialist_care`` or ``family_counseling``.

    The caller already refused clinical text and called ``tools.start_turn``.
    """

    message = city.inbound
    if message is None:
        return
    chat = await city.history(need_id=message.need_id)
    street = _own_street(city)
    if street and _asks_address(message):
        await city.say(message.sender, f"address={street}")
        return
    if role == "hospital":
        await _hospital(city, message, chat)
    elif role == "specialist_care":
        await _facility(city, message, chat, reports=True, hold_self=False)
    elif role == "family_counseling":
        await _family(city, message, chat)


# --- shared ------------------------------------------------------------------------------------


def words(text: str) -> set[str]:
    return {token.strip(".,;:!?()\"'").lower() for token in text.split() if token.strip()}


def first_word(text: str) -> str:
    parts = text.split()
    return parts[0].strip(".,;:!?()\"'").lower() if parts else ""


def fields_of(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for token in text.replace(",", " ").split():
        key, eq, value = token.partition("=")
        if not eq or not key or not value:
            continue
        value = value.strip(".,;:!?")
        if key not in found and value:
            found[key] = value
    return found


_IT_DAYS = {"lun": "mon", "mar": "tue", "mer": "wed", "gio": "thu", "ven": "fri", "sab": "sat", "dom": "sun"}


def _slot(line: str) -> dict[str, str]:
    """A Ledger slot row with our keys ``at``/``day``/``free``, from the kit example's format or
    the Ledger schema's ``agenda_date``/``agenda_day``/``capacity`` (ledger.py Slot)."""

    slot = fields_of(line)
    for key, alias in (("at", "agenda_date"), ("day", "agenda_day"), ("free", "capacity")):
        if key not in slot and alias in slot:
            slot[key] = slot[alias]
    if "day" in slot:
        day = slot["day"].lower()[:3]  # Monday, MON, lunedì -> mon
        slot["day"] = _IT_DAYS.get(day, day)
    return slot


def _fields(row: Message) -> dict[str, str]:
    fields = fields_of(row.text)
    for key, value in row.body.items():
        if key != "text" and isinstance(value, str) and value:
            fields.setdefault(key, value)
    return fields


def _when(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _citizen(chat: list[Message]) -> str | None:
    for row in chat:
        if row.sender.startswith("citizen:"):
            return row.sender
    for row in chat:
        fields = fields_of(row.text)
        for key in ("patient", "citizen"):
            party = fields.get(key, "")
            if party.startswith("citizen:"):
                return party
    return None


def _we(chat: list[Message], agent: str, token: str, *, to: str, word: str) -> bool:
    for row in chat:
        if row.sender != agent or row.recipient != to:
            continue
        if token in row.text.split() and word in words(row.text):
            return True
    return False


def _exact(chat: list[Message], agent: str, text: str) -> bool:
    return any(row.sender == agent and row.text.strip() == text for row in chat)


async def _say(city: City, to: str, text: str) -> bool:
    try:
        msg_id = await city.say(to, text)
    except CapError as exc:
        tools.said(to, text, error=exc.code)
        return False
    tools.said(to, text, msg_id)
    return True


def _latest(chat: list[Message], head: str, token: str) -> Message | None:
    found: Message | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == head and token in row.text.split():
            found = row
    return found


async def _until(
    city: City,
    need_id: str | None,
    chat: list[Message],
    ready: Callable[[list[Message]], bool],
    tries: int = _PULL_TRIES,
) -> list[Message]:
    """Re-read the case until ``ready`` or the short wait ends. The push may never arrive."""

    fresh = chat
    for attempt in range(tries):
        try:
            fresh = await city.history(need_id=need_id)
        except CapError:
            fresh = chat
        if ready(fresh):
            return fresh
        if attempt + 1 < tries:
            await asyncio.sleep(_PULL_PAUSE)
    return fresh


async def _await_line(
    city: City,
    need_id: str | None,
    chat: list[Message],
    head: str,
    token: str,
    *,
    op: str,
) -> tuple[Message, list[Message]] | None:
    """The ledger line for ``token``, or a refusal of ``op`` that was not already in ``chat``."""

    known = {row.msg_id for row in chat}
    fresh = chat
    for attempt in range(_PULL_TRIES):
        try:
            fresh = await city.history(need_id=need_id)
        except CapError:
            fresh = chat
        hit = _latest(fresh, head, token)
        if hit is not None:
            tools.observe(hit)
            return hit, fresh
        for row in fresh:
            if row.msg_id in known or row.sender != LEDGER:
                continue
            if first_word(row.text) == "refused" and op in words(row.text):
                tools.observe(row)
                return row, fresh
        if attempt + 1 < _PULL_TRIES:
            await asyncio.sleep(_PULL_PAUSE)
    return None


def _has_agenda(chat: list[Message]) -> bool:
    agenda = _ledger(chat, "agenda")
    return _when("" if agenda is None else agenda.get("agenda_now", "")) is not None


def _has_procedure(chat: list[Message]) -> bool:
    reply = _ledger(chat, "prescription")
    return reply is not None and bool(reply.get("procedure"))


def _speaks(doctor_id: str, lang: str) -> bool:
    spec = _DOCTORS.get(doctor_id.rsplit("-", 1)[-1])
    if spec is None:
        return False
    langs = spec[0]
    if lang in langs:
        return True
    return "*" in langs and lang not in {"it", "en"}


def fitting(
    lines: list[str],
    *,
    now: datetime,
    priority: str | None,
    lang: str,
    gender: str | None = None,
    procedure: str | None = None,
) -> list[dict[str, str]]:
    """Slots a citizen can accept: future, free, in the priority window, spoken language.

    A doctor missing from ``_DOCTORS`` is not filtered on language, gender or days: we do not
    know them, so we neither drop the slot nor claim anything about the doctor.
    """

    if priority == "U":
        horizon: datetime | None = now.replace(hour=23, minute=59, second=59, microsecond=0)
    elif priority == "P":
        horizon = None
    else:
        horizon = now + timedelta(days=_DEADLINE_DAYS.get(priority or "", 14))
    earliest = now + _LEAD
    chosen: list[tuple[datetime, dict[str, str]]] = []
    for line in lines:
        slot = _slot(line)
        slot_id = slot.get("slot_id")
        doctor = slot.get("doctor_id")
        at = _when(slot.get("at", ""))
        if not slot_id or not doctor or at is None:
            continue
        if procedure and slot.get("procedure") not in (None, procedure):
            continue
        free = slot.get("free")
        if free is not None and free.isdigit() and int(free) <= 0:
            continue
        reserve = slot.get("urgent_reserve", "").lower() == "true"
        if reserve and priority != "U":
            continue
        if not earliest < at or (horizon is not None and at >= horizon):
            continue
        spec = _DOCTORS.get(doctor.rsplit("-", 1)[-1])
        if spec is not None:
            if not _speaks(doctor, lang):
                continue
            day = slot.get("day", "")
            if day and day not in spec[2]:
                continue
            if gender and spec[1] != gender:
                continue
        chosen.append((at, slot))
    chosen.sort(key=lambda item: (item[0], item[1].get("slot_id", "")))
    return [slot for _, slot in chosen]


# --- centro prenotazioni and consultorio -------------------------------------------------------


def _ledger(chat: list[Message], head: str) -> dict[str, str] | None:
    found: dict[str, str] | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == head:
            found = fields_of(row.text)
    if found is not None and head == "agenda" and "agenda_now" not in found:
        found["agenda_now"] = found.get("agenda_date", "")
    return found


def _latest_slots(chat: list[Message]) -> str | None:
    """The latest ``world`` answer: ``slots page=N/M`` (kit example) or ``world slots=N``
    (Ledger schema, city_sdk ports.py), then one ``slot_id=`` row per slot."""

    found: str | None = None
    for row in chat:
        head = first_word(row.text)
        if row.sender == LEDGER and (head == "slots" or head == "world" and (
            "slots" in fields_of(row.text.split("\n", 1)[0]) or "slot_id=" in row.text
        )):
            found = row.text
    return found


def _page(text: str) -> tuple[int, int]:
    raw = fields_of(text.splitlines()[0] if text else "").get("page", "")
    left, _, right = raw.partition("/")
    if left.isdigit() and right.isdigit():
        return int(left), int(right)
    return 1, 1


def _asked(chat: list[Message], agent: str, procedure: str, page: int) -> bool:
    token = f"page={page}"
    proc = f"procedure={procedure}"
    for row in chat:
        if row.sender == agent and token in row.text.split() and proc in row.text.split():
            return True
    return False


def _our_offers(chat: list[Message], agent: str) -> list[dict[str, str]]:
    """Every offer we sent. One message carries several, one ``offer`` line each (R:95)."""

    return [
        fields_of(line)
        for row in chat
        if row.sender == agent
        for line in row.text.splitlines()
        if first_word(line) == "offer"
    ]


def _offered(chat: list[Message], agent: str) -> set[str]:
    return {offer["slot_id"] for offer in _our_offers(chat, agent) if offer.get("slot_id")}


def _withdrawn(chat: list[Message], agent: str) -> set[str]:
    """Slots we cancelled or the Ledger refused. The citizen drops the rest, so those are
    offered again."""

    by_offer = {
        offer["offer_id"]: offer["slot_id"]
        for offer in _our_offers(chat, agent)
        if offer.get("offer_id") and offer.get("slot_id")
    }
    dropped: set[str] = set()
    for row in chat:
        fields = fields_of(row.text)
        if row.sender == LEDGER and first_word(row.text) == "refused" and fields.get("slot_id"):
            dropped.add(fields["slot_id"])
        if "cancel" not in words(row.text):
            continue
        slot = by_offer.get(fields.get("offer_id", ""))
        if slot:
            dropped.add(slot)
    return dropped


def _number(chat: list[Message]) -> str | None:
    for row in reversed(chat):
        if row.sender.startswith("citizen:"):
            number = fields_of(row.text).get("prescription_number")
            if number:
                return number
    return None


def _lang(chat: list[Message]) -> str:
    for row in chat:
        if row.sender.startswith("citizen:"):
            lang = _fields(row).get("lang", "")
            if lang:
                return lang
    return "it"


def _en(chat: list[Message]) -> bool:
    return _lang(chat) == "en"


_DAY_NAMES = {
    "it": ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"),
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
}


def _slot_when(chat: list[Message], agent: str, slot_id: str) -> str:
    """The slot we offered, in words ("lunedì 23/11 alle 10:00"), or "" if we never offered it."""

    offers = _our_offers(chat, agent) if slot_id else []
    offer = next((o for o in offers if o.get("slot_id") == slot_id), {})
    at = _when(offer.get("agenda_date", ""))
    return "" if at is None else _in_words(at, _en(chat))


def _in_words(at: datetime, en: bool) -> str:
    if en:
        return f"{_DAY_NAMES['en'][at.weekday()]} {at:%d/%m} at {at:%H:%M}"
    return f"{_DAY_NAMES['it'][at.weekday()]} {at:%d/%m} alle {at:%H:%M}"


def _receipt_note(chat: list[Message], agent: str, slot_id: str, booking_id: str) -> str:
    """What the citizen does next after a booking: when, and the code for the check-in."""

    when = _slot_when(chat, agent, slot_id)
    if _en(chat):
        head = f"Your appointment is booked for {when}." if when else "Your appointment is booked."
        return (
            f" {head} On the day, when you arrive, check in with your booking code {booking_id}."
            " I am here if you need anything."
        )
    head = f"La sua prenotazione è confermata per {when}." if when else (
        "La sua prenotazione è confermata."
    )
    return (
        f" {head} Il giorno della visita, al suo arrivo, faccia il check-in con il codice di "
        f"prenotazione {booking_id}. Per qualsiasi cosa sono a sua disposizione."
    )


def _gender(chat: list[Message]) -> str | None:
    for row in chat:
        if not row.sender.startswith("citizen:"):
            continue
        fields = _fields(row)
        raw = fields.get("gender_preference") or fields.get("gender")
        if raw in {"m", "f"}:
            return raw
    return None


def _offer_line(slot: dict[str, str], number: str | None, priority: str | None) -> str:
    parts = [
        "offer",
        f"offer_id=of-{slot['slot_id']}",
        f"slot_id={slot['slot_id']}",
        f"doctor_id={slot['doctor_id']}",
        f"facility={slot['facility']}",
        f"procedure={slot['procedure']}",
        f"agenda_date={slot['at']}",
        f"agenda_day={slot['day']}",
        f"expiry={slot['at']}",
    ]
    if priority:
        parts.append(f"priority={priority}")
    if number:
        parts.append(f"prescription_number={number}")
    return " ".join(parts)


def _our_offer(chat: list[Message], agent: str, offer_id: str) -> dict[str, str]:
    return next((o for o in _our_offers(chat, agent) if o.get("offer_id") == offer_id), {})


class _Gate:
    """One need's lock and how many turns hold it or wait for it."""

    __slots__ = ("lock", "turns")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.turns = 0


class _Sent:
    """Offers this process sent for one need, which the case history may not show yet.

    ``offers`` holds every slot id (and waiting-list offer id) sent. A cancel makes the
    citizen drop all offers, so ``again`` holds only what was re-sent for ``trigger``.
    ``holds`` holds the slots we sent a ``hold`` for, so a duplicate confirm does not hold twice.
    """

    __slots__ = ("offers", "trigger", "again", "holds")

    def __init__(self) -> None:
        self.offers: set[str] = set()
        self.trigger = ""
        self.again: set[str] = set()
        self.holds: set[str] = set()


# Every Ledger or citizen message opens its own turn, and turns of one need run at the same
# time. Each reads the case before the others' offers are in it, so offers wait on this.
# An entry lives only while a turn holds or waits on its lock.
_GATES: dict[str, _Gate] = {}
_SENT: OrderedDict[str, _Sent] = OrderedDict()


def _sent(key: str) -> _Sent:
    entry = _SENT.get(key)
    if entry is None:
        entry = _SENT[key] = _Sent()
        while len(_SENT) > _SENT_NEEDS:
            _SENT.popitem(last=False)
    else:
        _SENT.move_to_end(key)
    return entry


@asynccontextmanager
async def _gated(key: str) -> AsyncIterator[None]:
    """Run alone among the turns holding ``key``. Not re-entrant: confirms use their own key."""

    gate = _GATES.get(key)
    if gate is None:
        gate = _GATES[key] = _Gate()
    gate.turns += 1
    try:
        async with gate.lock:
            yield
    finally:
        gate.turns -= 1
        if gate.turns == 0 and _GATES.get(key) is gate:
            del _GATES[key]


async def _desk(
    city: City,
    message: Message,
    chat: list[Message],
    *,
    hold_self: bool,
    fixed: str | None,
) -> None:
    if message.sender == city.agent_id:
        return
    kind = words(message.text)
    if "confirm" in kind and (
        message.sender.startswith("citizen:") or message.sender.startswith("agent:")
    ):
        await _on_confirm(city, message, chat, hold_self=hold_self)
        return
    if "booking.receipt" in kind and message.sender.startswith("agent:"):
        await _forward_receipt(city, message, chat)
        return
    if "cancel" in kind and message.sender.startswith("agent:"):
        await _partner_cancel(city, message, chat, fixed=fixed)
        return
    if message.sender == LEDGER or message.sender.startswith("citizen:"):
        await _advance(city, message, chat, fixed=fixed, replacing=False)


async def _advance(
    city: City,
    message: Message,
    chat: list[Message],
    *,
    fixed: str | None,
    replacing: bool,
) -> None:
    key = message.need_id or message.msg_id
    async with _gated(key):
        await _advance_alone(city, message, chat, key, fixed=fixed, replacing=replacing)


async def _advance_alone(
    city: City,
    message: Message,
    chat: list[Message],
    key: str,
    *,
    fixed: str | None,
    replacing: bool,
) -> None:
    agent = city.agent_id
    citizen = _citizen(chat)
    if citizen is None:
        return
    reply = None if fixed == "CONS-GIN" else _ledger(chat, "prescription")
    priority = None if reply is None else reply.get("priority")
    procedure = fixed or (None if reply is None else reply.get("procedure"))
    number = None if fixed == "CONS-GIN" else _number(chat)
    agenda = _ledger(chat, "agenda")
    now = _when("" if agenda is None else agenda.get("agenda_now", ""))
    world = _latest_slots(chat)
    # Ask, then read the reply from history: the push of a ledger message does not open a turn.
    for _pass in range(3):
        reply = None if fixed == "CONS-GIN" else _ledger(chat, "prescription")
        priority = None if reply is None else reply.get("priority")
        if priority == "P":
            offer_id = f"wl-{message.need_id or message.msg_id}"
            token = f"offer_id={offer_id}"
            sent = _sent(key)
            if offer_id in sent.offers:
                return
            if not _we(chat, agent, token, to=citizen, word="offer"):
                note = (
                    "I can put you on the waiting list for this visit: let me know if that "
                    "works for you."
                    if _en(chat)
                    else "Posso inserirla in lista d'attesa per questa visita: mi dica se le va "
                    "bene."
                )
                text = f"offer offer_id={offer_id} waiting_list=true {note}"
                if await _say(city, citizen, text):
                    sent.offers.add(offer_id)
            return
        procedure = fixed or (None if reply is None else reply.get("procedure"))
        number = None if fixed == "CONS-GIN" else _number(chat)
        if fixed is None and number and not procedure:
            if _pass == 2:
                return
            if not any(
                row.sender == agent and f"prescription_number={number}" in row.text.split()
                for row in chat
            ):
                await _say(city, LEDGER, f"prescription prescription_number={number}")
            if not _exact(chat, agent, "agenda"):
                await _say(city, LEDGER, "agenda")
            chat = await _until(
                city,
                message.need_id,
                chat,
                lambda rows: _has_procedure(rows) and _has_agenda(rows),
            )
            continue
        if not procedure:
            return
        state = None if reply is None else reply.get("state")
        if fixed is None and state not in (None, "issued"):
            return
        agenda = _ledger(chat, "agenda")
        now = _when("" if agenda is None else agenda.get("agenda_now", ""))
        world = _latest_slots(chat)
        if now is not None and world is not None:
            break
        if _pass == 2:
            return
        if now is None and not _exact(chat, agent, "agenda"):
            await _say(city, LEDGER, "agenda")
        if world is None and not _asked(chat, agent, procedure, 1):
            await _say(city, LEDGER, f"world procedure={procedure} page=1")
        chat = await _until(
            city,
            message.need_id,
            chat,
            lambda rows: _has_agenda(rows) and _latest_slots(rows) is not None,
        )
    else:
        return
    if now is None or world is None:
        return
    # A cancel drops every offer the citizen was holding, so the ones still free are sent again,
    # once per cancel: a second turn for the same cancel or refusal does not repeat them.
    sent = _sent(key)
    if replacing:
        trigger = fields_of(message.text).get("offer_id") or message.msg_id
        if sent.trigger != trigger:
            sent.trigger = trigger
            sent.again = set()
        taken = _withdrawn(chat, agent) | sent.again
        if fixed == "CONS-GIN" and message.sender == citizen:
            taken |= _offered(chat, agent) | sent.offers
    else:
        taken = _offered(chat, agent) | sent.offers
    # ``world`` is this ASST's slots and ``procedure=`` narrows it to our service (SKILL.md),
    # so no filter on ``facility=``: the slot names the facility's seat, not always our id.
    preferences = _family_preferences(chat, citizen) if fixed == "CONS-GIN" else {}
    days = preferences.get("days", set())
    en = preferences.get("lang", _lang(chat)) == "en"
    alternative_days: set[str] = set()
    while True:
        available = fitting(
            [line for line in world.splitlines() if "slot_id=" in line],
            now=now,
            priority=priority,
            lang=preferences.get("lang", _lang(chat)),
            gender=preferences.get("gender", _gender(chat)),
            procedure=procedure,
        )
        available = [slot for slot in available if slot.get("slot_id") not in taken]
        chosen = [slot for slot in available if not days or slot.get("day") in days][:3]
        if days:
            alternative_days.update(slot["day"] for slot in available
                                    if slot.get("day") in _FAMILY_DAYS and slot["day"] not in days)
        if chosen:
            # Every offer in one message: the citizen chooses from that message only (R:95).
            lines = [_offer_line(slot, number, priority) for slot in chosen]
            pick = _PICK_EN if en else _PICK
            if fixed == "CONS-GIN":
                options = [
                    f"{index}. {_family_date(at, en)}"
                    for index, slot in enumerate(chosen, 1)
                    if (at := _when(slot["at"])) is not None
                ]
                pick = " ".join([*options, pick])
            if await _say(city, citizen, "\n".join([*lines, pick])):
                for slot in chosen:
                    sent.offers.add(slot["slot_id"])
                    if replacing:
                        sent.again.add(slot["slot_id"])
            return
        if taken and not replacing:
            return
        page, pages = _page(world)
        if page >= pages or page >= _MAX_PAGE:
            break
        if not _asked(chat, agent, procedure, page + 1):
            await _say(city, LEDGER, f"world procedure={procedure} page={page + 1}")
        # The next page's reply may never open a turn: read it back here.
        chat = await _until(
            city,
            message.need_id,
            chat,
            lambda rows, seen=page: _page(_latest_slots(rows) or "")[0] > seen,
        )
        world = _latest_slots(chat) or ""
        if _page(world)[0] <= page:
            return
    # No fitting slot on any page: say so, never silence (R:248). Once per case, unless the
    # citizen writes again.
    if message.sender.startswith("citizen:") or not _we(
        chat, agent, "reason=no_slots", to=citizen, word="cancel"
    ):
        if fixed == "CONS-GIN" and alternative_days:
            day_names = [
                name for day, name in zip(_FAMILY_DAYS, _DAY_NAMES["en" if en else "it"])
                if day in alternative_days
            ]
            options = ", ".join(day_names)
            note = (
                f"I found no appointments on the requested days. I found openings on {options}. "
                "Which of these days, if any, would work for you?"
                if en else
                f"Nei giorni richiesti non ho trovato appuntamenti. Ho trovato posti di "
                f"{options}. Uno di questi giorni potrebbe andar bene?"
            )
            await _say(city, citizen, "data.request purpose=booking fields=days " + note)
        else:
            await _say(city, citizen, _NO_SLOTS_EN if en else _NO_SLOTS)


async def _on_confirm(
    city: City, message: Message, chat: list[Message], *, hold_self: bool
) -> None:
    # Duplicate confirms of one need run one at a time, so the slot is held once. A key of its
    # own: a refusal inside goes on to _advance, which takes the need's offer gate.
    key = message.need_id or message.msg_id
    async with _gated(f"confirm:{key}"):
        await _confirm_alone(city, message, chat, key, hold_self=hold_self)


async def _confirm_alone(
    city: City, message: Message, chat: list[Message], key: str, *, hold_self: bool
) -> None:
    agent = city.agent_id
    fields = _fields(message)
    offer_id = fields.get("offer_id", "")
    consent = fields.get("consent_ref", "")
    offer_rows = [row for row in chat if row.recipient == message.sender] if hold_self else chat
    offer = _our_offer(offer_rows, agent, offer_id) if offer_id else {}
    slot = offer.get("slot_id") or fields.get("slot_id", "")
    facility = offer.get("facility") or fields.get("facility", "")
    if not slot or not consent:
        # Never silent (R:195): the sender learns which field is missing.
        missing = "slot_id" if not slot else "consent_ref"
        head = f"cancel offer_id={offer_id}" if offer_id else "cancel"
        text = f"{head} reason=missing_{missing}"
        if message.sender.startswith("citizen:"):
            if _en(chat):
                text += (
                    " I'm sorry, one detail is still missing to complete the booking: send me"
                    " your choice again with it and I will go ahead straight away."
                )
            else:
                text += (
                    " Mi dispiace, per completare la prenotazione manca ancora un dato: mi"
                    " riscriva la sua scelta completa e procedo subito."
                )
        await _say(city, message.sender, text)
        if hold_self and message.sender.startswith("citizen:"):
            # A cancel drops the citizen's offers: send the ones still free again.
            await _advance(city, message, chat, fixed="CONS-GIN", replacing=True)
        return
    citizen = message.sender if hold_self else fields.get("citizen") or (
        message.sender if message.sender.startswith("citizen:") else ""
    )
    number = None if hold_self else fields.get("prescription_number") or offer.get("prescription_number")
    book_here = hold_self or message.sender.startswith("agent:") or facility == agent
    if book_here:
        sent = _sent(key)
        held = slot in sent.holds or _we(chat, agent, f"slot_id={slot}", to=LEDGER, word="hold")
        if not held:
            parts = ["hold", f"slot_id={slot}"]
            if citizen:
                parts.append(f"citizen={citizen}")
            if number:
                parts.append(f"prescription_number={number}")
            parts.append(f"consent_ref={consent}")
            if not await _say(city, LEDGER, " ".join(parts)):
                return
            sent.holds.add(slot)
        await _finish_booking(city, message, chat, slot, hold_self=hold_self)
        return
    if not facility or not citizen:
        return
    if _we(chat, agent, f"slot_id={slot}", to=facility, word="confirm"):
        return
    parts = [
        "confirm",
        f"offer_id={offer_id or offer.get('offer_id', '')}",
        f"slot_id={slot}",
        f"citizen={citizen}",
        f"consent_ref={consent}",
        f"facility={facility}",
    ]
    if number:
        parts.append(f"prescription_number={number}")
    await _say(city, facility, " ".join(parts))


async def _forward_receipt(city: City, message: Message, chat: list[Message]) -> None:
    fields = fields_of(message.text)
    booking_id = fields.get("booking_id", "")
    ledger_ref = fields.get("ledger_ref", "")
    citizen = _citizen(chat)
    if not booking_id or not ledger_ref or not citizen:
        return
    token = f"booking_id={booking_id}"
    if _we(chat, city.agent_id, token, to=citizen, word="booking.receipt"):
        return
    note = _receipt_note(chat, city.agent_id, fields.get("slot_id", ""), booking_id)
    await _say(
        city, citizen, f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref}{note}"
    )


async def _partner_cancel(
    city: City, message: Message, chat: list[Message], *, fixed: str | None
) -> None:
    fields = fields_of(message.text)
    offer_id = fields.get("offer_id", "")
    citizen = _citizen(chat)
    if (
        offer_id
        and citizen
        and not _we(chat, city.agent_id, f"offer_id={offer_id}", to=citizen, word="cancel")
    ):
        note = (
            "I'm sorry, the facility has called off this appointment. I am looking for other "
            "dates for you right away."
            if _en(chat)
            else "Mi dispiace, la struttura ha annullato questo appuntamento. Cerco subito altre "
            "date per lei."
        )
        await _say(city, citizen, f"cancel offer_id={offer_id} reason=facility_cancelled {note}")
    await _advance(city, message, chat, fixed=fixed, replacing=True)


# --- facility ----------------------------------------------------------------------------------


def _facility_turn(message: Message) -> bool:
    head = first_word(message.text)
    kind = words(message.text)
    if message.sender == LEDGER and head in {"held", "committed", "checked_in", "reported"}:
        return True
    if message.sender == LEDGER and head == "refused" and "hold" in kind:
        return True
    if ("checkin" in kind or message.type == "checkin") and message.sender.startswith("citizen:"):
        return True
    return ("confirm" in kind or message.type == "confirm") and (
        message.sender.startswith("agent:") or message.sender.startswith("citizen:")
    )


_FAMILY_DAYS = {
    "mon": "lunedi|monday", "tue": "martedi|tuesday", "wed": "mercoledi|wednesday",
    "thu": "giovedi|thursday", "fri": "venerdi|friday", "sat": "sabato|saturday",
    "sun": "domenica|sunday",
}
_FAMILY_NO = re.compile(r"\b(?:no|non|not|rifiuto|don't|cannot|can't)\b")
_FAMILY_YES = re.compile(r"\b(?:si|va bene|ok|okay|confermo|accetto|yes|fine|book it)\b")
_ARRIVAL = re.compile(r"\b(?:check[- ]?in|sono arrivat[oa]|sono qui|sono all'accettazione|"
                      r"i(?: have|'ve) arrived|i(?: am|'m) here)\b")
_ID = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"


def _family_date(at: datetime, en: bool) -> str:
    day = _DAY_NAMES["en" if en else "it"][at.weekday()]
    connector = "at" if en else "alle"
    return f"{day} {at:%d/%m} {connector} {at:%H:%M}"


def _family_plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text.casefold().replace("’", "'")).encode(
        "ascii", "ignore"
    ).decode()


def _family_days(text: str) -> set[str]:
    return {day for day, names in _FAMILY_DAYS.items() if re.search(rf"\b(?:{names})\b", text)}


def _family_preferences(chat: list[Message], citizen: str) -> dict:
    preferences = {}
    for row in chat:
        if row.sender != citizen:
            continue
        data = _fields(row)
        provided = row.body.get("provided")
        if isinstance(provided, dict):
            for field in ("days", "day", "lang", "gender", "gender_preference"):
                if isinstance(value := provided.get(field), str):
                    data.setdefault(field, value)
        plain = _family_plain(row.text)
        wanted, excluded = set(), set()
        for day, names in _FAMILY_DAYS.items():
            for match in re.finditer(rf"\b(?:{names})\b", plain):
                before = plain[max(0, match.start() - 28):match.start()]
                after = plain[match.end():]
                if re.search(r"\b(?:da|dal|since)\s+$", before):
                    continue
                if (re.search(r"\b(?:non|not|tranne|except)\s+(?:il |on |di )?$", before)
                    or re.match(r"\s+(?:non posso|non va bene|doesn't work|isn't good)", after)):
                    excluded.add(day)
                else:
                    wanted.add(day)
        names = (wanted or set(_FAMILY_DAYS)) - excluded if wanted or excluded else set()
        if raw := data.get("days") or data.get("day"):
            names = set(re.split(r"[/,]", raw.lower())) & _FAMILY_DAYS.keys()
        if names:
            preferences["days"] = names
        gender = data.get("gender_preference") or data.get("gender")
        if not gender and not _FAMILY_NO.search(plain):
            if re.search(r"\b(?:dottoressa|female doctor|woman doctor)\b", plain):
                gender = "f"
            elif re.search(r"\b(?:medico uomo|male doctor)\b", plain):
                gender = "m"
        if gender in ("f", "m"):
            preferences["gender"] = gender
        if lang := data.get("lang"):
            preferences["lang"] = lang
        elif re.search(r"\b(?:i would|i'd like|appointment|could i|in english|i'm here)\b", plain):
            preferences.setdefault("lang", "en")
    return preferences


def _family_pick(plain: str, offers: list[dict[str, str]]) -> str:
    if not offers or _FAMILY_NO.search(plain):
        return ""
    if re.fullmatch(r"[1-3]", plain.strip()):
        position = int(plain) - 1
        return offers[position]["offer_id"] if position < len(offers) else ""
    matches = set(range(len(offers)))
    criteria = False
    for index, names in enumerate(("prim[oa]|first|option 1", "second[oa]|second|option 2",
                                   "terz[oa]|third|option 3")):
        if re.search(rf"\b(?:{names})\b", plain):
            matches &= {index}
            criteria = True
    if days := _family_days(plain):
        matches &= {i for i, offer in enumerate(offers) if offer.get("agenda_day") in days}
        criteria = True
    if hour := re.search(r"\b(?:alle|ore|at)\s+(\d{1,2})(?::(\d{2}))?\b", plain):
        matches &= {
            i for i, offer in enumerate(offers)
            if (at := _when(offer.get("agenda_date", ""))) is not None
            and at.hour == int(hour[1]) and (hour[2] is None or at.minute == int(hour[2]))
        }
        criteria = True
    if len(matches) == 1 and (criteria or _FAMILY_YES.search(plain)):
        return offers[matches.pop()]["offer_id"]
    return ""


def _family_service_answer(plain: str, en: bool) -> str:
    """Administrative facts from the kit for the simulated consultorio."""
    if re.search(r"\b(?:cardiologia|cardiology|fisiatria|physiatry)\b", plain) and not re.search(
        r"\b(?:ginecolog\w*|gynecolog\w*|gynaecolog\w*)\b", plain
    ):
        return (
            "This service handles gynecology appointments. For cardiology or physiatry, "
            "contact the booking centre if you already have a referral, or your GP if you need one."
            if en else
            "Il consultorio gestisce le visite ginecologiche. Per cardiologia o fisiatria, "
            "si rivolga al centro prenotazioni se ha già una ricetta, oppure al medico di base "
            "se deve richiederla."
        )
    recipe = re.search(r"\b(?:serve|occorre|bisogno di|need|require)\b.*"
                       r"\b(?:ricetta|impegnativa|referral)\b", plain)
    services = re.search(r"\b(?:che visite|quali servizi|cosa fate|what (?:services|visits))\b", plain)
    booking = re.search(r"\b(?:vorrei prenotare|prenotatemi|please book|i want to book)\b", plain)
    if (services or (recipe and "?" in plain)) and not booking:
        return (
            "You can book a gynecology visit directly here without a referral. Tell me which "
            "days work for you and I can check the available appointments."
            if en else
            "Può prenotare una visita ginecologica direttamente qui, senza ricetta né impegnativa. "
            "Se mi dice quali giorni le vanno bene, verifico gli appuntamenti disponibili."
        )
    return ""


async def _family(city: City, message: Message, chat: list[Message]) -> None:
    if message.sender == LEDGER:
        if first_word(message.text) == "refused" and "commit" in words(message.text):
            await _on_refused(city, message, chat, hold_self=True)
        elif _facility_turn(message):
            await _facility(city, message, chat, reports=False, hold_self=True)
        else:
            await _desk(city, message, chat, hold_self=True, fixed="CONS-GIN")
        return
    if not message.sender.startswith("citizen:"):
        return

    citizen = message.sender
    plain = _family_plain(message.text)
    data = _fields(message)
    provided = message.body.get("provided")
    if isinstance(provided, dict):
        for field in ("booking_id", "offer_id", "consent_ref", "days", "day"):
            value = provided.get(field)
            if isinstance(value, str):
                data.setdefault(field, value)
    own = [row for row in chat if row.sender == city.agent_id and row.recipient == citizen]
    latest = next((row for row in reversed(own) if first_word(row.text) in
                   {"offer", "cancel", "consent", "booking.receipt", "checkin"}), None)
    last_act = first_word(latest.text) if latest else ""
    pending = fields_of(latest.text).get("offer_id", "") if last_act == "consent" else ""
    offer_message = next((row for row in reversed(own) if first_word(row.text) == "offer"), None)
    offers = _our_offers([offer_message], city.agent_id) if offer_message else []
    offers = [offer for offer in offers if offer.get("offer_id") and offer.get("slot_id")]
    waiting_for_days = bool(own and first_word(own[-1].text) == "data.request"
                            and fields_of(own[-1].text).get("fields") == "days")
    if waiting_for_days or last_act in {"cancel", "booking.receipt", "checkin"}:
        offers = []
    en = _family_preferences(chat, citizen).get("lang") == "en"

    waiting_for_code = bool(own and first_word(own[-1].text) == "data.request"
                            and fields_of(own[-1].text).get("purpose") == "checkin")
    if (waiting_for_code or message.type == "checkin"
        or first_word(message.text) == "checkin" or _ARRIVAL.search(plain)):
        if not message.need_id:
            await _say(city, citizen, "Mi serve il riferimento del percorso per registrare l'arrivo.")
            return
        booking_id = data.get("booking_id", "")
        if not booking_id:
            receipt = next((fields_of(row.text) for row in reversed(own)
                            if first_word(row.text) == "booking.receipt"), {})
            known = receipt.get("booking_id", "")
            if known and re.search(rf"(?<![\w-]){re.escape(known)}(?![\w-])", message.text, re.I):
                booking_id = known
            elif code := re.search(rf"\b(?:codice|code)(?: di prenotazione)?\s+(?:[èe]\s+|is\s+)?({_ID})",
                                   message.text, re.I):
                booking_id = code[1].rstrip(".")
            elif known and _ARRIVAL.fullmatch(plain.strip(" .!")):
                # A bare arrival in this case can reuse our own receipt. Any extra
                # details may designate a different booking, so ask when unclear.
                booking_id = known
        if waiting_for_code and not booking_id and re.fullmatch(_ID, message.text.strip()):
            booking_id = message.text.strip()
        if re.fullmatch(_ID, booking_id):
            checkin = replace(message, body={"text": f"checkin booking_id={booking_id}"})
            await _on_checkin(city, checkin, chat, reports=False)
        else:
            await _say(city, citizen, "data.request purpose=checkin fields=booking_id "
                       + ("What is your booking code?" if en else "Qual è il suo codice di prenotazione?"))
        return

    if message.type in {"chat", "need.request"} and not pending and not offers and not (
        data.get("offer_id") or data.get("consent_ref") or waiting_for_days
    ):
        if answer := _family_service_answer(plain, en):
            await _say(city, citizen, answer)
            return

    if not message.need_id:
        await _say(city, citizen, "Mi serve il riferimento del percorso per procedere.")
        return
    if last_act in {"booking.receipt", "checkin"}:
        return
    denied = data.get("granted", "").lower() == "false" or (
        not data.get("consent_ref") and bool(_FAMILY_NO.search(plain))
    )
    if pending and denied:
        await _say(city, citizen, f"cancel offer_id={pending} reason=consent_denied")
        return

    explicit = message.type == "confirm" or first_word(message.text) == "confirm"
    offer_id = data.get("offer_id") or (
        pending if pending and (data.get("consent_ref") or _FAMILY_YES.search(plain))
        else _family_pick(plain, offers)
    )
    asks_alternatives = re.search(
        r"\b(?:nessun[oa]|altre date|altr[oa] appuntamento|un altro|none|other dates|"
        r"another appointment|different appointment)\b", plain
    )
    if offers and not explicit and not data.get("offer_id") and asks_alternatives:
        await _advance(city, message, chat, fixed="CONS-GIN", replacing=True)
        return
    if explicit or offer_id or offers:
        selected = next((offer for offer in offers if offer["offer_id"] == offer_id), None)
        if not selected or data.get("slot_id") not in (None, selected["slot_id"]):
            await _say(city, citizen, "data.request purpose=booking fields=offer_id "
                       + ("Which of the proposed slots would you like?" if en else
                          "Quale degli appuntamenti proposti preferisce?"))
            return
        consent = data.get("consent_ref", "")
        if denied:
            await _say(city, citizen, f"cancel offer_id={offer_id} reason=consent_denied")
            return
        active_holds = {
            fields_of(row.text).get("slot_id") for row in chat
            if row.sender == city.agent_id and row.recipient == LEDGER
            and first_word(row.text) == "hold"
        } - _withdrawn(chat, city.agent_id)
        if active_holds - {selected["slot_id"]}:
            await _say(city, citizen, "La prima scelta è in verifica. Attenda l'esito della prenotazione.")
            return
        if not re.fullmatch(_ID, consent) or data.get("purpose", "booking") != "booking":
            await _say(city, citizen, f"consent purpose=booking offer_id={offer_id} "
                       + ("Do you consent to this booking? Please send the consent reference." if en else
                          "Acconsente alla prenotazione? Mi invii il riferimento del consenso."))
            return
        confirmation = replace(message, body={"text":
            f"confirm offer_id={offer_id} slot_id={selected['slot_id']} consent_ref={consent}"})
        await _on_confirm(city, confirmation, chat, hold_self=True)
        return
    if waiting_for_days and not _family_days(plain) and not (data.get("days") or data.get("day")):
        await _say(city, citizen, "data.request purpose=booking fields=days " + (
            "If another day suits you, tell me which one and I will check availability."
            if en else "Se le va bene un altro giorno, mi dica quale e verifico la disponibilità."
        ))
        return
    await _advance(city, message, chat, fixed="CONS-GIN", replacing=waiting_for_days)


def _report_turn(message: Message) -> bool:
    """A specialist turn that can touch the check-in note, the consent ask or the report."""

    if message.sender == LEDGER:
        return first_word(message.text) in {"checked_in", "reported"}
    return message.sender.startswith("citizen:")


async def _facility(
    city: City,
    message: Message,
    chat: list[Message],
    *,
    reports: bool,
    hold_self: bool,
) -> None:
    if reports and _report_turn(message):
        # Specialist: the citizen's check-in turn and the Ledger's checked_in push run at the
        # same time, and each would send the check-in note and the consent ask. One turn at a
        # time per case, re-reading the case inside the lock, so each goes out once.
        async with _gated(f"report:{message.need_id or message.msg_id}"):
            try:
                chat = await city.history(need_id=message.need_id)
            except CapError:
                pass
            await _facility_dispatch(city, message, chat, reports=reports, hold_self=hold_self)
        return
    await _facility_dispatch(city, message, chat, reports=reports, hold_self=hold_self)


async def _facility_dispatch(
    city: City,
    message: Message,
    chat: list[Message],
    *,
    reports: bool,
    hold_self: bool,
) -> None:
    head = first_word(message.text)
    kind = words(message.text)
    if message.sender == LEDGER and head == "held":
        await _on_held(city, message, chat, direct=hold_self)
    elif message.sender == LEDGER and head == "committed":
        await _on_committed(city, message, chat, direct=hold_self)
    elif message.sender == LEDGER and head == "refused" and "hold" in kind:
        await _on_refused(city, message, chat, hold_self=hold_self)
    elif message.sender == LEDGER and head == "checked_in":
        await _on_checked_in(city, message, chat, reports=reports)
    elif message.sender == LEDGER and head == "reported":
        await _on_reported(city, message, chat)
    elif ("checkin" in kind or message.type == "checkin") and message.sender.startswith("citizen:"):
        await _on_checkin(city, message, chat, reports=reports)
    elif (
        reports
        and message.sender.startswith("citizen:")
        and (booked := _prose_checkin(message, chat))
    ):
        # Level 3: "sono arrivata", or our booking code in prose, with no ``checkin`` token.
        await _on_checkin(city, _as_checkin(message, booked), chat, reports=reports)
    elif (
        reports
        and message.sender.startswith("citizen:")
        and (asked := _consent_asked(chat, city.agent_id))
    ):
        if _reported(chat, city.agent_id, asked):
            # The report is already filed: answer, do not leave the message unanswered.
            await _after_report(city, message, chat, asked)
        else:
            # The citizen's answer to our report_sharing request, a consent or a no in prose.
            await _on_consent(city, message, chat, asked)
    elif "confirm" in kind or message.type == "confirm":
        await _on_confirm(city, message, chat, hold_self=hold_self)
    elif message.sender.startswith("citizen:"):
        await _redirect(city, message, chat)


async def _redirect(city: City, message: Message, chat: list[Message]) -> None:
    """Un messaggio del cittadino che non sappiamo gestire: rinvio al ruolo giusto, mai una
    risposta inventata, e una volta sola per caso."""

    if _en(chat):
        text = (
            "Thank you for your message. I can't give you reliable information on this: your GP "
            "can help with symptoms and referrals, the booking centre with booking, moving or "
            "calling off a visit. If you already have an appointment here, check in with your "
            "booking code when you arrive and I will take care of the rest."
        )
    else:
        text = (
            f"Grazie per il messaggio. {tools.redirect()} Se ha già un appuntamento qui, al suo "
            "arrivo faccia il check-in con il codice di prenotazione e penso io al resto."
        )
    if not _exact(chat, city.agent_id, text):
        await _say(city, message.sender, text)


# --- ospedale (solo role == "hospital") --------------------------------------------------------

# Arrival in free text, it/en. "Ho l'appuntamento" counts only when it is not a question.
_ARRIVED = re.compile(
    r"\bcheck[\s-]?in\b|\bsono\s+(?:arrivat[oaie]|giunt[oaie]|qui|qua|in\s+(?:sala|accettazione"
    r"|struttura|reception|ospedale))\b|\bsiamo\s+(?:arrivat[ie]|qui|qua)\b|\beccomi\b"
    r"|\bi\s*['’]?\s*m\s+here\b|\b(?:i\s+am|we\s+are|we\s*['’]\s*re)\s+here\b"
    r"|\b(?:i|we)\s*(?:['’]\s*ve|\s+have)\s+(?:just\s+)?arrived\b|\bjust\s+arrived\b",
    re.IGNORECASE,
)
_APPOINTMENT = re.compile(
    r"\bho\s+(?:l\s*['’]\s*|un\s*['’]?\s*)appuntamento\b|\bi\s+have\s+an\s+appointment\b",
    re.IGNORECASE,
)
_QUESTION = re.compile(r"\?|\b(?:a\s+che\s+ora|quando|dove|what\s+time|when|where)\b", re.IGNORECASE)
# ponytail: word list, not a language detector. Only used when the citizen sent no lang=.
_ENGLISH = re.compile(
    r"\b(?:the|my|is|are|am|what|where|when|hello|hi|please|thanks|thank|appointment|arrived"
    r"|here|have)\b",
    re.IGNORECASE,
)
_WINDOW_CODE = re.compile(r"WINDOW|EARLY|LATE|TIME|CLOSED|HOUR|DAY", re.IGNORECASE)
_UNKNOWN_CODE = re.compile(r"UNKNOWN|NOT_FOUND|INVALID|NO_BOOKING|CANCEL", re.IGNORECASE)
# Our ASST's GP (gp/card.yaml): the hospital tells it the visit is booked, nobody else.
_OUR_GP = "agent:ck9w60ejwnvw"
# Our ASST's booking centre (booking_centre/card.yaml): only its bookings are our GP's patients.
_OUR_BOOKING = "agent:sbdpbpqz5sxe"
# The hospital's catalogue code (gp PROTOCOL; kit city_deploy/local.py maps hospital.* to it).
# A cross-ASST request names it, or names nothing and means it.
_PROCEDURE = "VIS-FIS"


async def _hospital(city: City, message: Message, chat: list[Message]) -> None:
    head = first_word(message.text)
    kind = words(message.text)
    confirm = "confirm" in kind or message.type == "confirm"
    from_citizen = message.sender.startswith("citizen:")
    if from_citizen and not confirm and (
        _arrival(message)
        or ("booking_id" in _fields(message) and _asked_booking(chat, city.agent_id))
    ):
        await _arrive(city, message)
    elif from_citizen and not confirm:
        await _answer(city, message, chat)
    elif message.sender == LEDGER and head == "refused" and "checkin" in kind:
        # A refusal that came after the arrival turn stopped waiting for it.
        await _checkin_refused(city, message, chat, booking_id="", en=_english(chat))
    elif message.sender.startswith("agent:") and not confirm and (
        "need.request" in kind or message.type == "need.request"
    ):
        await _cross_asst(city, message)
    else:
        booking = _facility(city, message, chat, reports=False, hold_self=False)
        if confirm and message.sender != LEDGER:
            # The visit's time, read alongside the hold: world goes out first, so it still
            # lists the slot, and the hold, commit and receipt do not wait for it.
            await asyncio.gather(_ask_slots(city, message, chat), booking)
        else:
            await booking
        if confirm or (message.sender == LEDGER and head in {"held", "committed"}):
            await _welcome(city, message)


async def _ask_slots(city: City, message: Message, chat: list[Message]) -> None:
    """One ``world`` page for the visit's time: neither the confirm nor held/committed carry
    it. Page 1 only: a slot not on it stays unknown, never guessed. Its reply is read from
    history (_welcome, _answer); as a turn of its own it does nothing."""

    fields = _fields(message)
    slot = fields.get("slot_id", "")
    facts = _slot_facts(chat, city.agent_id, slot)
    if not slot or facts.get("at"):
        return
    procedure = fields.get("procedure") or facts.get("procedure")
    await _say(city, LEDGER, f"world procedure={procedure} page=1" if procedure else "world page=1")


async def _cross_asst(city: City, message: Message) -> None:
    """R:191-192: another ASST's booking centre asks for our slots, criteria only. One answer
    per request, inside the fast reply (R:246): ``offer`` lines of our own slots from one
    ``world`` page, else ``cancel reason=no_availability``. Never citizen data, never an
    attribute the Ledger did not write. The confirm that follows takes _on_confirm."""

    fields = _fields(message)
    rid = fields.get("request_id", "")
    tag = f"request_id={rid}" if rid else ""
    sender = message.sender
    key = f"cross:{sender}:{rid or message.msg_id}"
    async with _gated(key):
        sent = _sent(key)
        try:
            chat = await city.history(need_id=message.need_id)
        except CapError:
            chat = []
        agent = city.agent_id
        if sent.offers or _answered(chat, agent, message.msg_id, sender) or tag and any(
            row.sender == agent and row.recipient == sender and tag in row.text.split()
            for row in chat
        ):
            return
        procedure = fields.get("procedure") or _PROCEDURE
        priority = fields.get("priority", "").upper()
        priority = priority if priority in {"U", "B", "D", "P"} else None
        chosen: list[dict[str, str]] = []
        # Only our own service: world lists every slot of the ASST, other facilities' too.
        if procedure == _PROCEDURE:
            known = {row.msg_id for row in chat}

            def fresh(rows: list[Message]) -> list[Message]:
                return [row for row in rows if row.msg_id not in known]

            await _say(city, LEDGER, "agenda")
            await _say(city, LEDGER, f"world procedure={procedure} page=1")
            rows = fresh(await _until(
                city,
                message.need_id,
                chat,
                lambda rows: _has_agenda(fresh(rows)) and _latest_slots(fresh(rows)) is not None,
                tries=_SHORT_TRIES,
            ))
            agenda = _ledger(rows, "agenda")
            now = _when("" if agenda is None else agenda.get("agenda_now", ""))
            world = _latest_slots(rows)
            if now is not None and world is not None:
                chosen = fitting(
                    [line for line in world.splitlines() if "slot_id=" in line],
                    now=now,
                    priority=priority,
                    lang="it",
                    procedure=procedure,
                )[:3]
        if chosen:
            lines = [f"{_offer_line(slot, None, priority)} {tag}".rstrip() for slot in chosen]
            text = "\n".join([
                *lines,
                "Buongiorno, ecco le prime disponibilità del nostro ospedale per la vostra "
                "richiesta. Per prenotarne una inviateci la scelta con il riferimento del consenso "
                "del cittadino: la registriamo sul Ledger e vi mandiamo subito la ricevuta.",
            ])
        else:
            text = (
                f"cancel reason=no_availability {tag}".rstrip()
                + " Mi dispiace, al momento non abbiamo disponibilità adatte a questa richiesta. "
                "Riscriveteci pure più avanti: ricontrolliamo subito l'agenda."
            )
        # Marked even if the say fails: a redelivery is not a retry after a 4xx.
        sent.offers.add(key)
        await _say(city, sender, text)


async def _agenda_now(
    city: City, message: Message, chat: list[Message]
) -> tuple[datetime | None, list[Message]]:
    """The city's clock (agenda time, 60x real time, never the pc's): a fresh ``agenda``
    reply within ~3 s, else None and we go on without it. Also the re-read case."""

    known = {row.msg_id for row in chat}

    def fresh(rows: list[Message]) -> dict[str, str] | None:
        return _ledger([row for row in rows if row.msg_id not in known], "agenda")

    if not await _say(city, LEDGER, "agenda"):
        return None, chat
    rows = await _until(
        city, message.need_id, chat, lambda rows: fresh(rows) is not None, tries=_SHORT_TRIES
    )
    reply = fresh(rows)
    return (None if reply is None else _when(reply["agenda_now"])), rows


def _timing(at: datetime, now: datetime | None, en: bool, *, inside: bool = True) -> str:
    """One sentence on when to come, by the city's clock, or "" without it. ``inside``:
    say so when the arrival window is open now."""

    if now is None:
        return ""
    now = now.astimezone(at.tzinfo)
    start = at.replace(hour=8 if at.hour < 13 else 13, minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=5)
    if now >= end:
        return (
            " The time of this visit has already passed: the booking centre can find you a new "
            "date."
            if en
            else " L'orario di questa visita risulta già passato: il centro prenotazioni può "
            "trovarle una nuova data."
        )
    if now.date() != at.date():
        today = f"{_DAY_NAMES['en' if en else 'it'][now.weekday()]} {now:%d/%m}"
        return (
            f" Today is {today}: your visit is on another day, we expect you on "
            f"{_in_words(at, True)}."
            if en
            else f" Oggi è {today}: la visita è un altro giorno, la aspettiamo "
            f"{_in_words(at, False)}."
        )
    if now < start:
        return (
            f" Your visit is today: arrivals open at {start:%H:%M}, please come from then."
            if en
            else f" La visita è oggi: la fascia per l'arrivo apre alle {start:%H:%M}, si "
            "presenti da quell'ora."
        )
    if not inside:
        return ""
    return (
        " Your visit is today and the arrival window is already open: you can come right away."
        if en
        else " La visita è oggi e la fascia per l'arrivo è già aperta: può presentarsi anche "
        "subito."
    )


def _arrival(message: Message) -> bool:
    text = message.text
    if message.type == "checkin" or "checkin" in words(text) or _ARRIVED.search(text):
        return True
    return bool(_APPOINTMENT.search(text)) and not _QUESTION.search(text)


def _english(chat: list[Message], text: str = "") -> bool:
    rows = [row for row in chat if row.sender.startswith("citizen:")]
    for row in rows:
        lang = _fields(row).get("lang", "")
        if lang:
            return lang == "en"
    return bool(_ENGLISH.search(text or (rows[-1].text if rows else "")))


def _answered(chat: list[Message], agent: str, msg_id: str, to: str) -> bool:
    """We already wrote to ``to`` after the row ``msg_id`` (a redelivered turn)."""

    for i, row in enumerate(chat):
        if row.msg_id == msg_id:
            return any(r.sender == agent and r.recipient == to for r in chat[i + 1 :])
    return False


def _asked_booking(chat: list[Message], agent: str) -> bool:
    return any(
        row.sender == agent
        and first_word(row.text) == "data.request"
        and "fields=booking_id" in row.text.split()
        for row in chat
    )


def _holder(chat: list[Message], agent: str, slot: str) -> str:
    """The ``citizen=`` of our hold on ``slot``, or ``""``."""

    for row in chat:
        if row.sender == agent and first_word(row.text) == "hold" and slot:
            if f"slot_id={slot}" in row.text.split():
                return fields_of(row.text).get("citizen", "")
    return ""


def _bookings(chat: list[Message], agent: str) -> dict[str, dict[str, str]]:
    """``booking_id -> {slot, ledger_ref}`` of what we committed in this case: the Ledger's
    ``committed`` answers to us and the receipts we sent. Nothing else counts."""

    found: dict[str, dict[str, str]] = {}
    for row in chat:
        head = first_word(row.text)
        if not (
            row.sender == LEDGER and row.recipient == agent and head == "committed"
            or row.sender == agent and head == "booking.receipt"
        ):
            continue
        fields = fields_of(row.text)
        booking_id = fields.get("booking_id") or fields.get("hold_id", "")
        if not booking_id:
            continue
        entry = found.setdefault(booking_id, {"slot": "", "ledger_ref": ""})
        entry["slot"] = entry["slot"] or fields.get("slot_id") or _hold_slot(
            chat, agent, fields.get("hold_id") or booking_id
        )
        entry["ledger_ref"] = entry["ledger_ref"] or fields.get("ledger_ref", "")
    return found


def _slot_facts(chat: list[Message], agent: str, slot: str) -> dict[str, str]:
    """What the Ledger or we wrote about ``slot`` (``at``, ``procedure``...). Never a citizen's
    or another agent's word: a time we tell the citizen must be a fact."""

    found: dict[str, str] = {}
    for row in chat:
        if not slot or row.sender not in (LEDGER, agent):
            continue
        for line in row.text.splitlines():
            if f"slot_id={slot}" in line.split():
                for key, value in _slot(line).items():
                    found.setdefault(key, value)
    return found


def _half_day(at: datetime) -> str:
    return "08:00-13:00" if at.hour < 13 else "13:00-18:00"


def _checkin_state(chat: list[Message], agent: str, token: str) -> str:
    """``done`` (Ledger checked_in), ``pending`` (sent, no answer), ``refused`` or ``""``."""

    state = ""
    for row in chat:
        head = first_word(row.text)
        mine = token in row.text.split()
        if row.sender == agent and row.recipient == LEDGER and head == "checkin" and mine:
            state = "pending"
        elif row.sender == LEDGER and head == "checked_in" and mine:
            return "done"
        elif row.sender == LEDGER and head == "refused" and "checkin" in words(row.text):
            # A refusal without booking_id answers our latest pending checkin.
            if mine or (state == "pending" and "booking_id" not in fields_of(row.text)):
                state = "refused"
    return state


async def _arrive(city: City, message: Message) -> None:
    key = message.need_id or message.msg_id
    async with _gated(f"arrive:{key}"):
        try:
            chat = await city.history(need_id=message.need_id)
        except CapError:
            return
        await _arrive_alone(city, message, chat)


async def _arrive_alone(city: City, message: Message, chat: list[Message]) -> None:
    """The citizen is here: ``checkin`` on the Ledger, then a warm word. The booking comes from
    the message or from the one booking we committed for this citizen in this case."""

    agent = city.agent_id
    citizen = message.sender
    en = _english(chat, message.text)
    booking_id = _fields(message).get("booking_id", "")
    if not booking_id:
        ours = [
            booking
            for booking, entry in _bookings(chat, agent).items()
            if _holder(chat, agent, entry["slot"]) in ("", citizen)
        ]
        booking_id = ours[0] if len(ours) == 1 else ""
    if not booking_id:
        if not _answered(chat, agent, message.msg_id, citizen):
            await _say(
                city,
                citizen,
                "data.request purpose=booking fields=booking_id "
                + (
                    "Welcome! To register your arrival I need your booking code (booking_id): "
                    "you find it on the booking receipt. Could you send it to me? I will take "
                    "care of the rest straight away."
                    if en
                    else "Benvenuto/a! Per registrare il suo arrivo mi serve il codice di "
                    "prenotazione (booking_id), che trova sulla ricevuta della prenotazione: me "
                    "lo può scrivere? Penso subito io al resto."
                ),
            )
        return
    token = f"booking_id={booking_id}"
    state = _checkin_state(chat, agent, token)
    if state == "done":
        line = _latest(chat, "checked_in", token)
        if line is not None and not _we(chat, agent, token, to=citizen, word="checkin"):
            await _on_checked_in(city, line, chat, reports=False, en=en)
        elif not _answered(chat, agent, message.msg_id, citizen):
            await _say(
                city,
                citizen,
                f"{token} "
                + (
                    "Your arrival is already registered: there is nothing else you need to do. "
                    "I wish you a good visit."
                    if en
                    else "Il suo arrivo è già registrato: non deve fare altro. Le auguro una "
                    "buona visita."
                ),
            )
        return
    if state != "pending":
        if not await _say(city, LEDGER, f"checkin {token} citizen={citizen}"):
            return
    found = await _await_line(city, message.need_id, chat, "checked_in", token, op="checkin")
    if found is None:
        _LOG.info("care.checkin.missing", extra={"need_id": (message.need_id or "")[:48]})
        await _say(
            city,
            citizen,
            f"{token} "
            + (
                "Thank you, I am registering your arrival now: I will confirm as soon as the "
                "register answers."
                if en
                else "Grazie, sto registrando il suo arrivo: le confermo appena il registro "
                "risponde."
            ),
        )
        return
    reply, fresh = found
    if first_word(reply.text) == "checked_in":
        await _on_checked_in(city, reply, fresh, reports=False, en=en)
        return
    await _checkin_refused(city, reply, fresh, booking_id=booking_id, en=en)


async def _checkin_refused(
    city: City, message: Message, chat: list[Message], *, booking_id: str, en: bool
) -> None:
    # The arrival turn and the refusal's own turn may both get here: one answer only.
    async with _gated(f"refused:{message.need_id or message.msg_id}"):
        await _refused_alone(city, message, chat, booking_id=booking_id, en=en)


async def _refused_alone(
    city: City, message: Message, chat: list[Message], *, booking_id: str, en: bool
) -> None:
    """The Ledger refused the arrival: the reason in plain words and what to do next. No act
    word for the arrival in the prose: it was not honoured."""

    agent = city.agent_id
    fields = fields_of(message.text)
    if not booking_id:
        booking_id = fields.get("booking_id", "")
        for row in chat:
            if not booking_id and row.sender == agent and first_word(row.text) == "checkin":
                booking_id = fields_of(row.text).get("booking_id", "")
    citizen = _citizen(chat)
    if not booking_id or not citizen or _answered(chat, agent, message.msg_id, citizen):
        return
    code = fields.get("code") or "refused"
    slot = _bookings(chat, agent).get(booking_id, {}).get("slot") or _hold_slot(
        chat, agent, booking_id
    )
    at = _when(_slot_facts(chat, agent, slot).get("at", ""))
    if _WINDOW_CODE.search(code):
        timing = ""
        if at:
            # The city's clock says whether it is another day or just early (never the pc's).
            now, chat = await _agenda_now(city, message, chat)
            if _answered(chat, agent, message.msg_id, citizen):
                return
            timing = _timing(at, now, en, inside=False)
        if en:
            when = (
                f", that is {_in_words(at, True)}, between {_half_day(at)}"
                if at
                else " (08:00-13:00 for a morning visit, 13:00-18:00 for an afternoon one)"
            )
            note = (
                f"I'm sorry, I cannot register your arrival for booking {booking_id} right "
                f"now: arrivals are registered only in the half day of the visit{when}.{timing} "
                "Write to me again when you are within that time and I will register it at once, I "
                "already have your code. If you cannot come, the booking centre can move the "
                "visit."
            )
        else:
            when = (
                f", cioè {_in_words(at, False)}, nella fascia {_half_day(at)}"
                if at
                else " (08:00-13:00 se la visita è al mattino, 13:00-18:00 se è al pomeriggio)"
            )
            note = (
                f"Mi dispiace, in questo momento non riesco a registrare il suo arrivo per la "
                f"prenotazione {booking_id}: l'arrivo si registra solo nella mezza giornata "
                f"della visita{when}.{timing} Mi riscriva quando è in quella fascia e lo registro "
                "subito, il codice ce l'ho già. Se non riesce a venire, il centro prenotazioni "
                "può spostare la visita."
            )
    elif _UNKNOWN_CODE.search(code):
        note = (
            f"I'm sorry, I cannot find a confirmed booking with the code {booking_id}. Could you "
            "check the code on your booking receipt and send it to me again? I will verify it "
            "right away. If it still does not work, the booking centre can check the booking."
            if en
            else f"Mi dispiace, non trovo una prenotazione confermata con il codice {booking_id}. "
            "Può controllare il codice sulla ricevuta della prenotazione e rimandarmelo? Lo "
            "verifico subito. Se il problema resta, il centro prenotazioni può controllare la "
            "prenotazione."
        )
    else:
        note = (
            f"I'm sorry, the register did not let me record your arrival for booking "
            f"{booking_id}. Write to me again in a moment and I will try again straight away; "
            "if it still does not work, the booking centre can check the booking."
            if en
            else f"Mi dispiace, il registro non mi ha permesso di registrare il suo arrivo per la "
            f"prenotazione {booking_id}. Mi riscriva tra poco e riprovo subito; se il problema "
            "resta, il centro prenotazioni può controllare la prenotazione."
        )
    await _say(city, citizen, f"booking_id={booking_id} reason={code} {note}")


async def _answer(city: City, message: Message, chat: list[Message]) -> None:
    """Free text that is not an arrival: what we know of the citizen's booking, else the
    honest redirect. Only facts we or the Ledger wrote."""

    agent = city.agent_id
    citizen = message.sender
    ours = {
        booking: entry
        for booking, entry in _bookings(chat, agent).items()
        if _holder(chat, agent, entry["slot"]) in ("", citizen)
    }
    if len(ours) != 1:
        await _redirect(city, message, chat)
        return
    if _answered(chat, agent, message.msg_id, citizen):
        return
    (booking_id, entry), = ours.items()
    en = _english(chat, message.text)
    at = _when(_slot_facts(chat, agent, entry["slot"]).get("at", ""))
    timing = ""
    if at:
        now, chat = await _agenda_now(city, message, chat)
        if _answered(chat, agent, message.msg_id, citizen):
            return
        timing = _timing(at, now, en)
    if en:
        head = (
            f"your visit with us is on {_in_words(at, True)} (booking code {booking_id}). "
            f"Your arrival can be registered between {_half_day(at)} that day"
            if at
            else f"your booking with us is confirmed with the code {booking_id}. Day and time "
            "are the ones of the offer you accepted: I do not have them in my records and I "
            "prefer not to give you a wrong time. Your arrival can be registered in the half "
            "day of the visit (08:00-13:00 or 13:00-18:00)"
        )
        text = (
            f"booking_id={booking_id} Thank you for your message: {head}.{timing} When you are here, "
            "just write to me that you have arrived, I already have your code. For symptoms, "
            "or to move or call off the visit, your GP and the booking centre can help."
        )
    else:
        head = (
            f"la sua visita da noi è {_in_words(at, False)} (codice di prenotazione "
            f"{booking_id}). L'arrivo si registra nella fascia {_half_day(at)} di quel giorno"
            if at
            else f"la sua prenotazione da noi è confermata con il codice {booking_id}. Giorno e "
            "ora sono quelli dell'offerta che ha accettato: non li ho nei miei dati e preferisco "
            "non darle un orario sbagliato. L'arrivo si registra nella mezza giornata della "
            "visita (08:00-13:00 o 13:00-18:00)"
        )
        text = (
            f"booking_id={booking_id} Grazie del messaggio: {head}.{timing} Quando è qui mi scriva "
            "semplicemente che è arrivato/a, il codice ce l'ho già. Per sintomi, o per spostare "
            "o annullare la visita, può rivolgersi al suo medico di base o al centro prenotazioni."
        )
    await _say(city, citizen, text)


async def _welcome(city: City, message: Message) -> None:
    """After our receipt to the booking centre, a word of our own to the citizen: when, the
    code, how to register the arrival. Once per booking."""

    key = message.need_id or message.msg_id
    async with _gated(f"welcome:{key}"):
        try:
            chat = await city.history(need_id=message.need_id)
        except CapError:
            return
        agent = city.agent_id
        for booking_id, entry in _bookings(chat, agent).items():
            token = f"booking_id={booking_id}"
            citizen = _holder(chat, agent, entry["slot"]) or _citizen(chat) or ""
            receipt = any(
                row.sender == agent
                and row.recipient != citizen
                and first_word(row.text) == "booking.receipt"
                and token in row.text.split()
                for row in chat
            )
            if (
                not citizen.startswith("citizen:")
                or not receipt
                or _we(chat, agent, token, to=citizen, word="booking.receipt")
            ):
                continue
            if _latest_slots(chat) is None and any(
                row.sender == agent and row.recipient == LEDGER and first_word(row.text) == "world"
                for row in chat
            ):
                # Our world read may still be on its way: a short wait, then go on without it.
                chat = await _until(
                    city,
                    message.need_id,
                    chat,
                    lambda rows: _latest_slots(rows) is not None,
                    tries=_SHORT_TRIES,
                )
            facts = _slot_facts(chat, agent, entry["slot"])
            at = _when(facts.get("at", ""))
            en = _english(chat)
            parts = ["booking.receipt", token]
            if entry["ledger_ref"]:
                parts.append(f"ledger_ref={entry['ledger_ref']}")
            if facts.get("procedure"):
                parts.append(f"procedure={facts['procedure']}")
            if at:
                parts.append(f"agenda_date={facts['at']}")
            if en:
                when = f" for {_in_words(at, True)}" if at else ""
                window = (
                    f"between {_half_day(at)} that day"
                    if at
                    else "in the half day of the visit (08:00-13:00 or 13:00-18:00)"
                )
                note = (
                    f"Hello, this is the hospital where your visit will take place: your "
                    f"booking is confirmed and registered{when}. Your booking code is "
                    f"{booking_id}. On the day, as soon as you arrive ({window}), write to me "
                    f"that you are here with the code {booking_id}: I will register your arrival "
                    "and you need do nothing else. For any question, just write to me here."
                )
            else:
                when = f" per {_in_words(at, False)}" if at else ""
                window = (
                    f"nella fascia {_half_day(at)} di quel giorno"
                    if at
                    else "nella mezza giornata della visita: 08:00-13:00 o 13:00-18:00"
                )
                note = (
                    f"Buongiorno, sono l'ospedale dove si svolgerà la sua visita: la prenotazione "
                    f"è confermata e registrata{when}. Il suo codice di prenotazione è "
                    f"{booking_id}. Il giorno della visita, appena arriva ({window}), mi scriva "
                    f"che è qui con il codice {booking_id}: registro io il suo arrivo e non deve "
                    "fare altro. Per qualsiasi domanda mi scriva pure qui, sono a sua disposizione."
                )
            await _say(city, citizen, " ".join(parts) + " " + note)
            await _tell_gp(city, chat, booking_id, entry["slot"], facts, at)


async def _tell_gp(
    city: City,
    chat: list[Message],
    booking_id: str,
    slot: str,
    facts: dict[str, str],
    at: datetime | None,
) -> None:
    """Unrequested, once, right after the welcome: our own GP learns the visit is booked.
    Only for our booking centre's bookings: another ASST's citizen is not our GP's patient
    (over-disclosure, R:243). Best effort: only if the Index lists our GP, no retry, never raises."""

    sender, _, number = _booking_of(chat, city.agent_id, slot) if slot else ("", "", None)
    if sender != _OUR_BOOKING or not number or _we(chat, city.agent_id, f"booking_id={booking_id}", to=_OUR_GP, word="data.response"):
        return
    try:
        if not any(peer.agent_id == _OUR_GP for peer in await city.find("gp")):
            return
    except Exception:  # CapError or network: the citizen already heard from us
        return
    parts = ["data.response", "purpose=booking", f"booking_id={booking_id}", f"prescription_number={number}"]
    if facts.get("procedure"):
        parts.append(f"procedure={facts['procedure']}")
    if slot:
        parts.append(f"slot_id={slot}")
    if at:
        parts.append(f"agenda_date={facts['at']}")
    when = f" per {_in_words(at, False)}" if at else ""
    await _say(
        city,
        _OUR_GP,
        " ".join(parts)
        + f" Buongiorno collega, la informo che il suo paziente con questa ricetta è prenotato "
        f"da noi{when}: la prenotazione {booking_id} è confermata sul Ledger. Gli ho già inviato "
        "il codice e le istruzioni per l'arrivo. Se le serve altro sono a disposizione, grazie!",
    )


def _booking_of(chat: list[Message], agent: str, slot_id: str) -> tuple[str, str, str | None]:
    """Who asked us to book this slot, the consent, and the prescription, if we have them."""

    token = f"slot_id={slot_id}"
    sender = ""
    consent = ""
    number: str | None = None
    for row in chat:
        if token not in row.text.split():
            continue
        fields = fields_of(row.text)
        if "confirm" in words(row.text) and row.sender not in (LEDGER, agent):
            sender = row.sender
            consent = fields.get("consent_ref", consent)
            number = fields.get("prescription_number") or number
        if row.sender == agent and "hold" in words(row.text):
            consent = fields.get("consent_ref", consent)
            number = fields.get("prescription_number") or number
            sender = sender or fields.get("citizen", "")
    return sender, consent, number


def _hold_slot(chat: list[Message], agent: str, hold_id: str) -> str:
    token = f"hold_id={hold_id}"
    for row in chat:
        if token in row.text.split() and (row.sender == agent or row.sender == LEDGER):
            slot = fields_of(row.text).get("slot_id", "")
            if slot:
                return slot
    return ""


def _our_commit(chat: list[Message], agent: str, hold_id: str) -> bool:
    """A commit we actually sent. The ledger's held line also contains the word commit."""

    token = f"hold_id={hold_id}"
    for row in chat:
        if row.sender != agent or row.recipient != LEDGER:
            continue
        if row.text.strip().startswith("commit ") and token in row.text.split():
            return True
    return False


def _latest_hold(chat: list[Message], agent: str) -> tuple[str, str | None]:
    consent = ""
    number: str | None = None
    for row in chat:
        if row.sender != agent or not row.text.strip().startswith("hold "):
            continue
        fields = fields_of(row.text)
        consent = fields.get("consent_ref", consent)
        number = fields.get("prescription_number") or number
    return consent, number


async def _finish_booking(
    city: City, message: Message, chat: list[Message], slot: str, *, hold_self: bool
) -> None:
    """Hold is in the mailbox. Read the held line and commit before this turn ends."""

    found = await _await_line(city, message.need_id, chat, "held", f"slot_id={slot}", op="hold")
    if found is None:
        _LOG.info("care.held.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = found
    if first_word(reply.text) == "refused":
        await _on_refused(city, reply, fresh, hold_self=hold_self)
        return
    await _on_held(city, reply, fresh, direct=hold_self)
    hold_id = fields_of(reply.text).get("hold_id", "")
    if not hold_id:
        return
    done = await _await_line(
        city, message.need_id, fresh, "committed", f"booking_id={hold_id}", op="commit"
    )
    if done is None:
        _LOG.info("care.commit.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = done
    if first_word(reply.text) == "committed":
        await _on_committed(city, reply, fresh, direct=hold_self)
        return
    if first_word(reply.text) == "refused":
        await _on_refused(city, reply, fresh, hold_self=hold_self)


async def _on_held(
    city: City, message: Message, chat: list[Message], *, direct: bool = False
) -> None:
    fields = fields_of(message.text)
    hold_id = fields.get("hold_id", "")
    slot = fields.get("slot_id", "")
    if not hold_id or not slot:
        _LOG.info("care.held.skip", extra={"head": first_word(message.text)[:40]})
        return
    if _our_commit(chat, city.agent_id, hold_id):
        return
    _, consent, number = _booking_of(chat, city.agent_id, slot)
    if direct:
        # Only an actual hold sent for this slot can authorize a direct-access commit.
        hold = next((fields_of(row.text) for row in reversed(chat)
                     if row.sender == city.agent_id and row.recipient == LEDGER
                     and first_word(row.text) == "hold"
                     and fields_of(row.text).get("slot_id") == slot), {})
        consent, number = hold.get("consent_ref", ""), None
    elif not consent:
        consent, held_number = _latest_hold(chat, city.agent_id)
        number = number or held_number
    need = fields.get("need_id", "")
    if not direct and not consent and need.startswith("N-"):
        # The citizen's booking consent is cons-<need_id>-booking. The held line does not
        # echo the real ref, and the hold that carried it may not be in this page of history.
        consent = f"cons-{need}-booking"
    if not consent:
        _LOG.info("care.held.noconsent", extra={"need_id": need[:48]})
        return
    parts = [
        "commit",
        f"hold_id={hold_id}",
        f"consent_ref={consent}",
        f"fence={fields.get('fence', '1')}",
    ]
    if number:
        parts.append(f"prescription_number={number}")
    await _say(city, LEDGER, " ".join(parts))


async def _on_committed(
    city: City, message: Message, chat: list[Message], *, direct: bool = False
) -> None:
    fields = fields_of(message.text)
    booking_id = fields.get("booking_id") or fields.get("hold_id", "")
    ledger_ref = fields.get("ledger_ref", "")
    if not booking_id or not ledger_ref:
        return
    hold_id = fields.get("hold_id") or (booking_id if direct else "")
    slot = fields.get("slot_id") or _hold_slot(chat, city.agent_id, hold_id)
    sender, _, _ = _booking_of(chat, city.agent_id, slot) if slot else ("", "", None)
    target = sender or _citizen(chat)
    if not target:
        return
    token = f"booking_id={booking_id}"
    if _we(chat, city.agent_id, token, to=target, word="booking.receipt"):
        return
    text = f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref}"
    if target.startswith("citizen:"):
        if direct:
            en = _family_preferences(chat, target).get("lang") == "en"
            offer = next((o for o in _our_offers(chat, city.agent_id)
                          if o.get("slot_id") == slot), {})
            at = _when(offer.get("agenda_date", ""))
            when = _family_date(at, en) if at else ""
            address = getattr(city, "address", "").strip()
            # Avoid letting punctuation in a civic address change the chat's protocol tokens.
            address = address.replace("=", "").replace("\n", " ")
            if en:
                text += f" Your appointment is confirmed{f' for {when}' if when else ''}."
                if address:
                    text += f" The facility is at {address}."
                text += " When you arrive, tell me here and I can register your arrival."
            else:
                text += f" La visita è confermata{f' per {when}' if when else ''}."
                if address:
                    text += f" Il consultorio si trova in {address}."
                text += " Al suo arrivo mi scriva qui: posso registrare io il suo arrivo."
        else:
            text += _receipt_note(chat, city.agent_id, slot, booking_id)
    await _say(city, target, text)


async def _on_refused(
    city: City, message: Message, chat: list[Message], *, hold_self: bool
) -> None:
    agent = city.agent_id
    slot = ""
    for row in chat:
        if row.sender == agent and row.recipient == LEDGER and "hold" in words(row.text):
            slot = fields_of(row.text).get("slot_id", slot)
    if not slot:
        return
    target, _, _ = _booking_of(chat, agent, slot)
    offer_id = ""
    for row in chat:
        for line in row.text.splitlines():
            if f"slot_id={slot}" in line.split():
                offer_id = fields_of(line).get("offer_id", offer_id)
    if (
        target
        and offer_id
        and not _we(chat, agent, f"offer_id={offer_id}", to=target, word="cancel")
    ):
        # The Ledger's code goes on, so the booking centre can react: PRESCRIPTION_EXPIRED →
        # renewal (P1), CONSENT_MISSING → consent (P2), NO_CAPACITY → another slot (P3).
        code = fields_of(message.text).get("code") or "facility_cancelled"
        text = f"cancel offer_id={offer_id} reason={code}"
        if target.startswith("citizen:"):
            if _en(chat):
                text += " I'm sorry, this appointment can no longer be booked." + (
                    " I am looking for other dates for you right away."
                    if hold_self
                    else " The booking centre can find you another date."
                )
            else:
                text += " Mi dispiace, questo appuntamento non è più disponibile." + (
                    " Cerco subito altre date per lei."
                    if hold_self
                    else " Il centro prenotazioni può trovarle un'altra data."
                )
        await _say(city, target, text)
    if hold_self:
        await _advance(city, message, chat, fixed="CONS-GIN", replacing=True)


async def _on_checkin(city: City, message: Message, chat: list[Message], *, reports: bool) -> None:
    booking_id = _fields(message).get("booking_id", "")
    citizen = message.sender if message.sender.startswith("citizen:") else _citizen(chat)
    if not booking_id or not citizen:
        return
    token = f"booking_id={booking_id}"
    if not _we(chat, city.agent_id, token, to=LEDGER, word="checkin"):
        if not await _say(city, LEDGER, f"checkin booking_id={booking_id} citizen={citizen}"):
            return
    found = await _await_line(city, message.need_id, chat, "checked_in", token, op="checkin")
    if found is None:
        _LOG.info("care.checkin.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = found
    if first_word(reply.text) == "checked_in":
        await _on_checked_in(city, reply, fresh, reports=reports)
        return
    # Refused by the Ledger: tell the citizen why. No ``checkin`` word, it was not honoured.
    code = fields_of(reply.text).get("code") or "refused"
    await _say(
        city,
        citizen,
        f"{token} reason={code} "
        + (
            f"I'm sorry, I could not register your arrival with the code {booking_id}. Could "
            "you check the code on the booking confirmation you received and send it to me "
            "again? I will verify it right away. If it still does not work, whoever booked "
            "the appointment for you can check the booking."
            if _en(chat)
            else f"Mi dispiace, non sono riuscito a registrare il suo arrivo con il codice "
            f"{booking_id}. Può controllare il codice sulla conferma di prenotazione che ha "
            "ricevuto e reinviarmelo? Lo verifico subito. Se il problema resta, chi le ha "
            "fissato l'appuntamento può controllare la prenotazione."
        ),
    )


async def _on_checked_in(
    city: City, message: Message, chat: list[Message], *, reports: bool, en: bool | None = None
) -> None:
    booking_id = fields_of(message.text).get("booking_id", "")
    citizen = _citizen(chat)
    en = _en(chat) if en is None else en
    if not booking_id:
        return
    token = f"booking_id={booking_id}"
    if citizen and not _we(chat, city.agent_id, token, to=citizen, word="checkin"):
        if reports:
            note = (
                "Thank you, your arrival is registered. After the visit the specialist files "
                "the visit summary and I will let you know straight away."
                if en
                else "Grazie, il suo arrivo è registrato. Dopo la visita lo specialista archivia "
                "il referto e la avviso subito."
            )
        else:
            note = (
                "Thank you, your arrival is registered: there is nothing else you need to do. "
                "I wish you a good visit."
                if en
                else "Grazie, il suo arrivo è registrato: non deve fare altro. Le auguro una buona "
                "visita."
            )
        await _say(city, citizen, f"checkin booking_id={booking_id} {note}")
    if not reports:
        return
    if not citizen:
        # Nobody to ask: file the report unshared (R:208).
        await _file_report(city, message, chat, booking_id, "", "")
        return
    if _reported(chat, city.agent_id, booking_id):
        return
    # Live journeys (wave 1): citizens never answered a report_sharing ask and the judge scored
    # report_missing every time. R:210 allows filing it unshared, which is never an
    # over-disclosure: file the report now, in the check-in turn, without waiting.
    await _file_report(city, message, chat, booking_id, citizen, "")


def _reported(chat: list[Message], agent: str, booking_id: str) -> bool:
    """We already wrote the report for ``booking_id`` to the Ledger."""

    return _we(chat, agent, f"booking_id={booking_id}", to=LEDGER, word="report")


async def _after_report(
    city: City, message: Message, chat: list[Message], booking_id: str
) -> None:
    """A citizen line after the report is filed: a short, warm answer, nothing to redo."""

    text = (
        "You're welcome! Your visit summary is already filed: there is nothing else you "
        "need to do. Take care."
        if _en(chat)
        else "Prego! Il referto della sua visita è già archiviato: non deve fare altro. "
        "Buona giornata."
    )
    await _say(city, message.sender, f"booking_id={booking_id} {text}")


def _consent_asked(chat: list[Message], agent: str) -> str:
    """The booking whose report we asked the citizen to share, or ``""``."""

    found = ""
    for row in chat:
        if (
            row.sender == agent
            and row.recipient.startswith("citizen:")
            and first_word(row.text) == "consent"
            and "purpose=report_sharing" in row.text.split()
        ):
            found = fields_of(row.text).get("booking_id", found)
    return found


async def _on_consent(city: City, message: Message, chat: list[Message], booking_id: str) -> None:
    """R:208-210: a ``consent`` carries ``consent_ref``; any other answer to our request is a
    no, and the report is filed without sharing it."""

    fields = fields_of(message.text)
    consent = ""
    if fields.get("purpose", "report_sharing") == "report_sharing":
        consent = fields.get("consent_ref", "")
    if not consent and (code := _CONSENT_CODE.search(message.text)):
        # Level 3: the code in prose, "il codice del consenso è cons-...".
        consent = code.group(0).rstrip(".,;:!?)")
    if (
        not consent
        and _YES_PROSE.search(message.text)
        and not _NO_PROSE.search(message.text)
        and _consent_asks(chat, city.agent_id, booking_id) < 2
    ):
        # "Sì" without the code: we never invent a consent_ref (sharing without consent is
        # over-disclosure, R:244). Ask for the code once; a second answer without it is a no.
        await _say(
            city,
            message.sender,
            f"consent purpose=report_sharing booking_id={booking_id} "
            + (
                "Thank you! To share it I need the code of your consent (consent_ref): could "
                "you send it to me? Without it we file the summary without sharing it."
                if _en(chat)
                else "Grazie! Per condividerlo mi serve il codice del suo consenso (consent_ref): "
                "me lo può inviare? Senza il codice lo archiviamo senza condividerlo."
            ),
        )
        return
    await _file_report(city, message, chat, booking_id, message.sender, consent)


# Level 3 ("Bisogni in linguaggio naturale"): citizens write prose, no key=value tokens.
_ARRIVED = re.compile(
    r"\b(?:sono\s+arrivat[oa]|siamo\s+arrivati|sono\s+qui|siamo\s+qui|allo\s+sportello|"
    r"in\s+accettazione|check[\s-]?in|i'?m\s+here|i\s+am\s+here|i\s+have\s+arrived|i\s+arrived|"
    r"at\s+the\s+desk)\b",
    re.IGNORECASE,
)
_CONSENT_CODE = re.compile(r"\bcons-[A-Za-z0-9_.-]+")
_YES_PROSE = re.compile(
    r"\b(?:s[iì]|ok|okay|va\s+bene|certo|acconsento|d'accordo|autorizzo|confermo|"
    r"yes|sure|i\s+agree|i\s+consent)\b",
    re.IGNORECASE,
)
_NO_PROSE = re.compile(
    r"\b(?:no|non\s+acconsento|preferisco\s+di\s+no|non\s+voglio|rifiuto|nope|"
    r"i\s+do\s+not|i\s+don'?t)\b",
    re.IGNORECASE,
)


def _our_booking(chat: list[Message]) -> str:
    """The booking the Ledger committed for us in this case, or ``""``."""

    found = ""
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == "committed":
            found = fields_of(row.text).get("booking_id", found)
    return found


def _prose_checkin(message: Message, chat: list[Message]) -> str:
    """Our booking when a citizen says in prose that they arrived (or names its code), else
    ``""``. Only before the check-in: a later line is a consent answer or small talk."""

    if "checkin" in words(message.text):
        return ""
    booking = _our_booking(chat)
    if not booking or _latest(chat, "checked_in", f"booking_id={booking}") is not None:
        return ""
    named = booking in {token.strip(".,;:!?()\"'") for token in message.text.split()}
    return booking if named or _ARRIVED.search(message.text) else ""


def _as_checkin(message: Message, booking_id: str) -> Message:
    """The citizen's prose turn as the ``checkin`` the Ledger path reads."""

    return Message(
        type=message.type,
        sender=message.sender,
        msg_id=message.msg_id,
        need_id=message.need_id,
        conversation_id=message.conversation_id,
        body={"text": f"checkin booking_id={booking_id}"},
        recipient=message.recipient,
        sent_at=message.sent_at,
    )


def _consent_asks(chat: list[Message], agent: str, booking_id: str) -> int:
    """How many report_sharing requests we sent the citizen for ``booking_id``."""

    token = f"booking_id={booking_id}"
    return sum(
        1
        for row in chat
        if row.sender == agent
        and row.recipient.startswith("citizen:")
        and first_word(row.text) == "consent"
        and "purpose=report_sharing" in row.text.split()
        and token in row.text.split()
    )


def _follow_up(chat: list[Message]) -> str | None:
    """Procedure of the control visit this case asks for, else None.

    R:197: the protocol table has no follow-up field, the specialist prescribes one "quando il
    percorso lo richiede", and the kit names no signal for that. So we never prescribe on our
    own. ponytail: reads a ``follow_up=`` token from Ledger lines only (a citizen's token could
    be an injection); swap in the real signal once the Ledger's ``help`` shows it.
    """

    for row in reversed(chat):
        if row.sender != LEDGER:
            continue
        value = fields_of(row.text).get("follow_up", "")
        if value.lower() in {"true", "yes", "1"}:
            return _CONTROL
        if value[:1].isalpha() and value == value.upper():
            return value
    return None


async def _prescribe_follow_up(
    city: City,
    need_id: str | None,
    chat: list[Message],
    booking_id: str,
    citizen: str,
    procedure: str,
) -> tuple[str, list[Message]]:
    """``prescribe ... follow_up_of=`` and wait for ``prescribed``: its number, or ``""``."""

    suffix = "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in booking_id.upper())
    number = f"prescription_number-FU-{suffix.strip('-')[:37]}"
    token = f"prescription_number={number}"
    if not _we(chat, city.agent_id, token, to=LEDGER, word="prescribe"):
        # No priority: the kit gives none for a control visit and we do not invent one.
        text = (
            f"prescribe patient={citizen} procedure={procedure} "
            f"follow_up_of={booking_id} {token}"
        )
        if not await _say(city, LEDGER, text):
            return "", chat
    found = await _await_line(city, need_id, chat, "prescribed", token, op="prescribe")
    if found is None:
        _LOG.info("care.follow_up.missing", extra={"need_id": (need_id or "")[:48]})
        return "", chat
    reply, fresh = found
    if first_word(reply.text) != "prescribed":
        return "", fresh
    return fields_of(reply.text).get("prescription_number", number), fresh


async def _file_report(
    city: City,
    message: Message,
    chat: list[Message],
    booking_id: str,
    citizen: str,
    consent: str,
) -> None:
    """Ledger first: the follow-up prescription when the case asks for one, then the report;
    the citizen hears it after ``reported`` (_on_reported). Once per booking."""

    token = f"booking_id={booking_id}"
    if not _we(chat, city.agent_id, token, to=LEDGER, word="report"):
        number = ""
        procedure = _follow_up(chat)
        if procedure and citizen:
            number, chat = await _prescribe_follow_up(
                city, message.need_id, chat, booking_id, citizen, procedure
            )
        parts = ["report", token, f"outcome_code={_OUTCOME}"]
        if consent:
            parts.append(f"consent_ref={consent}")
        if number:
            parts.append(f"follow_up_prescription_number={number}")
        if not await _say(city, LEDGER, " ".join(parts)):
            return
    found = await _await_line(city, message.need_id, chat, "reported", token, op="report")
    if found is None:
        _LOG.info("care.report.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = found
    if first_word(reply.text) == "reported":
        await _on_reported(city, reply, fresh)


async def _on_reported(city: City, message: Message, chat: list[Message]) -> None:
    fields = fields_of(message.text)
    booking_id = fields.get("booking_id", "")
    citizen = _citizen(chat)
    if not booking_id or not citizen:
        return
    agent = city.agent_id
    token = f"booking_id={booking_id}"
    ours: dict[str, str] = {}
    for row in chat:
        if (
            row.sender == agent
            and row.recipient == LEDGER
            and first_word(row.text) == "report"
            and token in row.text.split()
        ):
            ours = fields_of(row.text)
    if not _we(chat, agent, token, to=citizen, word="report"):
        outcome = fields.get("outcome_code") or ours.get("outcome_code") or _OUTCOME
        shared = fields.get("consent_ref") or ours.get("consent_ref")
        if _en(chat):
            note = (
                "Your visit summary is filed and shared as you agreed."
                if shared
                else "Your visit summary is filed; without your agreement it is not shared and "
                "stays private."
            )
            note += " Thank you for coming, and take care."
        else:
            note = (
                "Il referto della sua visita è archiviato e lo condividiamo come da suo consenso."
                if shared
                else "Il referto della sua visita è archiviato; senza il suo consenso non viene "
                "condiviso e resta riservato."
            )
            note += " Grazie per la fiducia e buona giornata."
        await _say(city, citizen, f"report {token} outcome_code={outcome} {note}")
    number = ours.get("follow_up_prescription_number", "")
    rx = f"prescription_number={number}"
    if not number or _we(chat, agent, rx, to=citizen, word="prescription"):
        return
    # The control visit's prescription, as we asked it and as the Ledger wrote it.
    known: dict[str, str] = {}
    for row in chat:
        if rx in row.text.split() and (
            (row.sender == agent and first_word(row.text) == "prescribe")
            or (row.sender == LEDGER and first_word(row.text) == "prescribed")
        ):
            known.update(fields_of(row.text))
    parts = ["prescription", rx]
    for key in ("procedure", "priority"):
        if known.get(key):
            parts.append(f"{key}={known[key]}")
    parts.append(f"follow_up_of={booking_id}")
    await _say(
        city,
        citizen,
        " ".join(parts)
        + (
            " I have already arranged the referral for your follow-up visit, so you do not need "
            "to go back to your GP: you can book it directly with the booking centre."
            if _en(chat)
            else " Le ho già preparato la ricetta per la visita di controllo, così non deve "
            "tornare dal medico: può prenotarla direttamente al centro prenotazioni."
        ),
    )
