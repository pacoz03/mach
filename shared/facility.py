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
        await _facility(city, message, chat, reports=False, hold_self=False)
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
) -> list[Message]:
    """Re-read the case until ``ready`` or the short wait ends. The push may never arrive."""

    fresh = chat
    for attempt in range(_PULL_TRIES):
        try:
            fresh = await city.history(need_id=need_id)
        except CapError:
            fresh = chat
        if ready(fresh):
            return fresh
        if attempt + 1 < _PULL_TRIES:
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
    if at is None:
        return ""
    if _en(chat):
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
    reply = _ledger(chat, "prescription")
    priority = None if reply is None else reply.get("priority")
    procedure = fixed or (None if reply is None else reply.get("procedure"))
    number = _number(chat)
    agenda = _ledger(chat, "agenda")
    now = _when("" if agenda is None else agenda.get("agenda_now", ""))
    world = _latest_slots(chat)
    # Ask, then read the reply from history: the push of a ledger message does not open a turn.
    for _pass in range(3):
        reply = _ledger(chat, "prescription")
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
        number = _number(chat)
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
    else:
        taken = _offered(chat, agent) | sent.offers
    # ``world`` is this ASST's slots and ``procedure=`` narrows it to our service (SKILL.md),
    # so no filter on ``facility=``: the slot names the facility's seat, not always our id.
    while True:
        chosen = [
            slot
            for slot in fitting(
                [line for line in world.splitlines() if "slot_id=" in line],
                now=now,
                priority=priority,
                lang=_lang(chat),
                gender=_gender(chat),
                procedure=procedure,
            )
            if slot.get("slot_id") not in taken
        ][:3]
        if chosen:
            # Every offer in one message: the citizen chooses from that message only (R:95).
            lines = [_offer_line(slot, number, priority) for slot in chosen]
            pick = _PICK_EN if _en(chat) else _PICK
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
        await _say(city, citizen, _NO_SLOTS_EN if _en(chat) else _NO_SLOTS)


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
    offer = _our_offer(chat, agent, offer_id) if offer_id else {}
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
    citizen = fields.get("citizen") or (
        message.sender if message.sender.startswith("citizen:") else ""
    )
    number = fields.get("prescription_number") or offer.get("prescription_number")
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
    if "checkin" in kind and message.sender.startswith("citizen:"):
        return True
    return ("confirm" in kind or message.type == "confirm") and (
        message.sender.startswith("agent:") or message.sender.startswith("citizen:")
    )


async def _family(city: City, message: Message, chat: list[Message]) -> None:
    if _facility_turn(message):
        await _facility(city, message, chat, reports=False, hold_self=True)
        return
    await _desk(city, message, chat, hold_self=True, fixed="CONS-GIN")


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
        await _on_held(city, message, chat)
    elif message.sender == LEDGER and head == "committed":
        await _on_committed(city, message, chat)
    elif message.sender == LEDGER and head == "refused" and "hold" in kind:
        await _on_refused(city, message, chat, hold_self=hold_self)
    elif message.sender == LEDGER and head == "checked_in":
        await _on_checked_in(city, message, chat, reports=reports)
    elif message.sender == LEDGER and head == "reported":
        await _on_reported(city, message, chat)
    elif "checkin" in kind and message.sender.startswith("citizen:"):
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
    await _on_held(city, reply, fresh)
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
        await _on_committed(city, reply, fresh)
        return
    if first_word(reply.text) == "refused":
        await _on_refused(city, reply, fresh, hold_self=hold_self)


async def _on_held(city: City, message: Message, chat: list[Message]) -> None:
    fields = fields_of(message.text)
    hold_id = fields.get("hold_id", "")
    slot = fields.get("slot_id", "")
    if not hold_id or not slot:
        _LOG.info("care.held.skip", extra={"head": first_word(message.text)[:40]})
        return
    if _our_commit(chat, city.agent_id, hold_id):
        return
    _, consent, number = _booking_of(chat, city.agent_id, slot)
    if not consent:
        consent, held_number = _latest_hold(chat, city.agent_id)
        number = number or held_number
    need = fields.get("need_id", "")
    if not consent and need.startswith("N-"):
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


async def _on_committed(city: City, message: Message, chat: list[Message]) -> None:
    fields = fields_of(message.text)
    booking_id = fields.get("booking_id") or fields.get("hold_id", "")
    ledger_ref = fields.get("ledger_ref", "")
    if not booking_id or not ledger_ref:
        return
    slot = fields.get("slot_id") or _hold_slot(chat, city.agent_id, fields.get("hold_id", ""))
    sender, _, _ = _booking_of(chat, city.agent_id, slot) if slot else ("", "", None)
    target = sender or _citizen(chat)
    if not target:
        return
    token = f"booking_id={booking_id}"
    if _we(chat, city.agent_id, token, to=target, word="booking.receipt"):
        return
    text = f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref}"
    if target.startswith("citizen:"):
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
    booking_id = fields_of(message.text).get("booking_id", "")
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
    city: City, message: Message, chat: list[Message], *, reports: bool
) -> None:
    booking_id = fields_of(message.text).get("booking_id", "")
    citizen = _citizen(chat)
    if not booking_id:
        return
    token = f"booking_id={booking_id}"
    if citizen and not _we(chat, city.agent_id, token, to=citizen, word="checkin"):
        if reports:
            note = (
                "Thank you, your arrival is registered. After the visit the specialist files "
                "the visit summary and I will let you know straight away."
                if _en(chat)
                else "Grazie, il suo arrivo è registrato. Dopo la visita lo specialista archivia "
                "il referto e la avviso subito."
            )
        else:
            note = (
                "Thank you, your arrival is registered: there is nothing else you need to do. "
                "I wish you a good visit."
                if _en(chat)
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
    # R:208: after the check-in the specialist asks consent to share the report.
    if not _we(chat, city.agent_id, token, to=citizen, word="consent"):
        asked = await _say(
            city,
            citizen,
            f"consent purpose=report_sharing {token} "
            + (
                "One more thing: may I share your visit summary? If you agree, "
                "just send me your approval. If you prefer not to, that is perfectly fine: we "
                "file it without sharing it and you need do nothing else."
                if _en(chat)
                else "Un'ultima cosa: posso condividere il suo referto? Se è "
                "d'accordo, mi invii il suo consenso. Se preferisce di no, nessun problema: lo "
                "archiviamo senza condividerlo e non deve fare altro."
            ),
        )
        if not asked:
            # The ask cannot reach the citizen: the report must not wait for an answer.
            await _file_report(city, message, chat, booking_id, citizen, "")
            return
    # The citizen's answer opens the next turn (_on_consent); the report is filed there.


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
