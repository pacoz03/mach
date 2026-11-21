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
import time
import unicodedata
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import tools
from city_sdk.city import LEDGER, CapError, City, Message, Need, OutOfScope
from mach import calendar, protocol
from mach.protocol import fields_of, first_word, words
from mach.safety import red_flag
from mach.ring import in_ring

_MAX_PAGE = 8
# A hold is answered after a Ledger round trip: 5-7 real minutes, ~6 agenda hours at rate 60
# (audit 21/11). A slot that starts sooner comes back EXPIRED (live: one hold in three): no hold.
_HOLD_LEAD = timedelta(hours=6)
# Every offer starts past this: _HOLD_LEAD at the confirm plus the choice (live 21/11: up to 16 real
# minutes; 4 h offers came back as our own EXPIRED cancels, 18 in 2 h). 16 h, the booking centre's
# _OFFER_LEAD: one margin from offer to hold.
_LEAD = _HOLD_LEAD + timedelta(hours=10)
# Doctor key (the last segment of doctor_id) -> languages, gender, days, from the registry
# (console team/world). The Ledger's slot line names the doctor; it does not repeat this table.
_DOCTORS: dict[str, tuple[frozenset[str], str, frozenset[str]]] = {
    "h1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed"})),
    "h2": (frozenset({"it"}), "m", frozenset({"wed", "thu", "fri"})),
    "h3": (frozenset({"it", "sq"}), "m", frozenset({"tue", "thu"})),
    "s1": (frozenset({"it", "en"}), "m", frozenset({"mon", "wed", "fri"})),
    "s2": (frozenset({"it"}), "f", frozenset({"tue", "thu"})),
    "c1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed", "thu", "fri"})),
}
# Our ASST's rare language: only h3 speaks it, and partners ask us for it (registry).
_RARE = "sq"
_LOG = logging.getLogger("care")
# The Relay accepts a ledger reply and then the agent rejects the push, so the reply sits in
# the case history and never opens a turn. Wait briefly and read it back from history.
_PULL_TRIES = 24
_PULL_PAUSE = 0.25
# About 3 s: the visit's time and the city's clock are a courtesy, never worth a long wait.
_SHORT_TRIES = 12
# A partner's need.request and the consultorio citizen's offers wait at most this long per turn for
# the Ledger (live 21/11: other teams' payloads clog the Relay, a Ledger reply takes ~595 s p50, p90
# 615 s). The answer comes from the cached world pages (_WORLD) while they are _WORLD_FRESH seconds
# old; past the wait with nothing useful, the request is closed (facility_unavailable, or "write again
# later" to the citizen) and the late reply only refreshes the cache. The fast reply (+1, R:246)
# needs the turn's ack < 10 s.
_LEDGER_WAIT = 5.0
_LEDGER_PAUSE = 1.0  # one read of the case's Ledger rows per second while waiting (was 24 full reads)
# 30 real minutes (utente, 21/11 14:20): a Ledger round trip takes ~27 min, Ledger -> agent ~13.7 min, so a
# 5 min cache was stale on arrival (_recover dates a page by its sent_at) and nearly every request got
# facility_unavailable or "riscriva più tardi". Past slots still drop out by the agenda clock (fitting); a
# taken one is the Ledger's call at the hold.
_WORLD_FRESH = 1800.0
# A world read of one weekday: AgendaDay of the Ledger schema (city_sdk ledger.py:52), as slot rows
# name it (day=tue). ponytail: the format is a guess from those two; a refused ``day=`` falls back to
# page reads for the life of the process (_BY_DAY).
_DAY_READ = "world procedure={procedure} day={day}"
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri")
_BY_DAY = True
# (procedure, page or weekday) -> (time.monotonic() of the read, world reply text).
# ponytail: one process's memory, empty after a restart or rollout and not shared between pods;
# a shared store if the hospital ever runs on more than one pod.
_WORLD: dict[tuple[str, int | str], tuple[float, str]] = {}
# _cross_asst, cache hit: the refresh reads sent after the answer, at most one per read text every
# _REFRESH_EVERY s (read text -> time.monotonic() it was last queued). Live hospital 1.0.17 (21/11,
# 30 min): ~110 partner requests sent 91 world page=1, 87 page=2 and 87 agenda, 3 reads each with
# _WORLD fresh. World answers in 12-96 s, so a page asked every 60 s stays inside _WORLD_FRESH;
# agenda only picks up a rebase or a pause (_city_now extrapolates the anchor, the same all night).
# ponytail: per process like _WORLD: a restarted pod or a second pod refreshes on its own.
_REFRESH_EVERY = {"world": 60.0, "agenda": 300.0}
_REFRESHED: dict[str, float] = {}
# Hospital, cross-ASST (live 1.0.8: 80 offers in 11 min on the same 3 slots, 3 of 5 confirms
# refused NO_CAPACITY): when we offered each slot in the last _OFFER_SPAN s, and the slots we sent
# a hold for, kept _HOLD_SPAN s or until the Ledger ends the hold (_unhold). _spread reads both.
# ponytail: one process's memory like _WORLD; a shared store if the hospital runs on more pods.
_OFFER_SPAN = 60.0
_HOLD_SPAN = 600.0
_OFFERED: dict[str, list[float]] = {}
_HELD: dict[str, float] = {}
# Needs whose sent offers this process remembers; the oldest is forgotten first.
_SENT_NEEDS = 4096
# The kit names no outcome_code values: VISIT is the only one it writes (examples/agent.py).
# Hypothesis: check against the Ledger's ``help`` when the city is back.
_OUTCOME = "VISIT"
# Catalogue code of the cardiology control visit (city-cli chat_play CODES: "cardiology follow-up").
_CONTROL = "CTRL-CARD"
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
# Consultorio: the Ledger did not answer within _LEDGER_WAIT. No date, no act word: the citizen's next
# message asks again (a late reply sends nothing, _advance_alone).
_BUSY = (
    "Mi dispiace, in questo momento non riesco a verificare le disponibilità in agenda, quindi per "
    "ora non posso prenotare la visita. Mi riscriva più tardi e ricontrollo subito."
)
_BUSY_EN = (
    "I'm sorry, right now I can't check the agenda for free appointments, so I can't book your "
    "visit yet. Please write to me again later and I will check straight away."
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

    The caller already refused clinical text and called ``tools.start_turn``. Only OutOfScope
    leaves the turn: any other exception out of the brain is a crash or, an httpx timeout, a
    redelivery that says every message of the turn twice (city_sdk harness.py ``_think``).
    """

    # Specialist and consultorio: a partner's turn (its confirm) stops waiting on the Ledger at
    # _BOOK_BY, as every hospital turn (_turn); the held and committed pushes commit and send the
    # receipt (_on_held, _on_committed). The citizen's own turns wait as before.
    inbound = city.inbound
    by = None
    if role != "hospital" and inbound is not None and inbound.sender.startswith("agent:"):
        by = _BOOK_BY.set(time.monotonic() + _BOOK_BUDGET)
    try:
        await _turn(role, need, city)
    except OutOfScope:
        raise
    except Exception:  # noqa: BLE001 - logged; the turn ends here, never redelivered
        _LOG.exception("care.turn.failed")
    finally:
        if by is not None:
            _BOOK_BY.reset(by)


async def _turn(role: str, need: Need, city: City) -> None:
    started = time.monotonic()
    message = city.inbound
    if message is None:
        return
    if (
        role in ("hospital", "family_counseling")
        and message.sender.startswith("agent:")
        and (message.type == "need.request" or "need.request" in words(message.text))
        and "confirm" not in words(message.text)
        and not {"request_id", "procedure", "service_type"} & _fields(message).keys()
    ):
        # The conformance probe (a search without cross-ASST criteria): no network at all, so the
        # turn ends well inside custody.ack's ~5 s wait for the renew (1.0.9 deploys failed there).
        # The consultorio too: 1.0.6 answered it live (21/11, TARGET_NOT_VISIBLE to t05/osp-x).
        return
    if _outside_ring(message):  # MACH-2.1 R17
        return
    global _RECALLED, _BY_DAY
    if not _CLOCK and not _RECALLED:
        _RECALLED = True
        _background(_recall_clock())  # never awaited: one memory read per process, off the turn
    if message.sender == LEDGER and _day_refused([message]):
        _BY_DAY = False  # the refusal came after the turn that asked: page reads from now on
    if role == "family_counseling" and message.sender.startswith("agent:"):
        try:
            chat = await asyncio.wait_for(city.history(need_id=message.need_id), timeout=1.0)
        except (CapError, TimeoutError):
            chat = []
    else:
        chat = await city.history(need_id=message.need_id)
    street = _own_street(city)
    if street and _asks_address(message):
        await _say(city, message.sender, f"address={street}")
        return
    if role == "hospital":
        flag = _HOSPITAL.set(True)
        by = _BOOK_BY.set(started + _BOOK_BUDGET)
        try:
            _unhold(message, chat, city.agent_id)
            _recover(chat, city.agent_id)
            await _hospital(city, message, chat)
        finally:
            _BOOK_BY.reset(by)
            _HOSPITAL.reset(flag)
    elif role == "specialist_care":
        cardiology = {"procedures": _CARDIOLOGY, "ours": "del nostro ambulatorio di cardiologia"}
        # As the hospital: a late agenda or world answer, as its own Ledger turn, refreshes the
        # clock or the cache, then the partner requests still waiting for it get their answer
        # (after a restart, or on a second pod, the clock comes back from the case: _recover).
        _recover(chat, city.agent_id)
        if message.sender == LEDGER and first_word(message.text) == "agenda":
            _ledger([message], "agenda")  # refreshes the city clock (_CLOCK) for the cache
            await _answer_pending(city, chat, **cardiology)
        if message.sender == LEDGER and _keep_world_reply(message, chat):
            await _answer_pending(city, chat, **cardiology)
            return
        kind = words(message.text)
        if (
            message.sender.startswith("agent:")
            and not ("confirm" in kind or message.type == "confirm")
            and ("need.request" in kind or message.type == "need.request")
            # As the hospital (0e2d784): a partner's search names request_id, procedure or
            # service_type; the conformance probe names none and must not be answered.
            and {"request_id", "procedure", "service_type"} & _fields(message).keys()
        ):
            # Level 4: another ASST's booking centre asks for our cardiology slots (R:191).
            await _cross_asst(city, message, **cardiology)
        else:
            confirm = "confirm" in kind or message.type == "confirm"
            booking = _facility(city, message, chat, reports=True, hold_self=False)
            if confirm and message.sender != LEDGER:
                # As the hospital: the visit's time from one world page, read alongside the hold.
                await asyncio.gather(_ask_slots(city, message, chat), booking)
            else:
                await booking
            if confirm or (
                message.sender == LEDGER and first_word(message.text) in {"held", "committed"}
            ):
                # Proactive (R:249): when and how to check in, then our GP hears it is booked
                # (collaboration, R:250; only our booking centre's bookings, _tell_gp).
                await _welcome(
                    city,
                    message,
                    place_it="l'ambulatorio di cardiologia dove si svolgerà la sua visita",
                    place_en="the cardiology clinic where your visit will take place",
                )
    elif role == "family_counseling":
        # As the specialist: a late agenda/world answer, as its own Ledger turn, refreshes the
        # city clock and the CONS-GIN cache that partner requests are answered from.
        if message.sender == LEDGER and first_word(message.text) == "agenda":
            _ledger([message], "agenda")
        if message.sender == LEDGER and _keep_world_reply(message, chat):
            return
        await _family(city, message, chat)


# --- shared ------------------------------------------------------------------------------------


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


# True only inside a hospital turn (handle): the booking lifecycle fixes gated on it (one commit,
# re-hold, duplicate confirm, no resend after TARGET_NOT_VISIBLE) leave the
# specialist and the consultorio as they were. ponytail: a ContextVar, not a flag threaded
# through six shared signatures.
_HOSPITAL: ContextVar[bool] = ContextVar("hospital", default=False)
# Hospital: (need_id, to, text) -> time.monotonic() of a TARGET_NOT_VISIBLE. The held or committed
# push turns of the same case must not send the same message again within seconds (R:701). Keyed
# by case and forgotten after protocol._BOUNCE_TTL: a set kept forever blocked agenda, world and checkin
# for the life of the process after a down window (every partner then got no_availability).
_BOUNCED: dict[tuple[str, str, str], float] = {}
# Hospital: the confirm turn stops waiting for held and committed this many seconds after the turn
# began, then ends. The harness acks a push only when the turn ends and the fast reply (+1, R:246)
# is scored on that ack (< 10 s); live writes take ~30 s each (21/11 00:36: hold -> held 30 s,
# commit -> committed 30 s), so the held push commits and the committed push sends the receipt,
# in turns of their own (_hospital: book: lock, fresh history).
_BOOK_BUDGET = 5.0  # + a 1-5 s say for the commit, still < 10 s
# Hospital, and a partner's turn at the specialist or the consultorio (handle): time.monotonic() at
# which this turn stops waiting on the Ledger; else None.
_BOOK_BY: ContextVar[float | None] = ContextVar("book_by", default=None)


async def _say(city: City, to: str, text: str) -> bool:
    """protocol.say, never raising. Hospital: one retry on a network error, and no resend to a
    citizen it could not see (_BOUNCED)."""

    hospital = _HOSPITAL.get()
    return bool(await protocol.say(city, to, text, retry=hospital, bounced=_BOUNCED if hospital else None))


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
    deadline: float | None = None,
) -> list[Message]:
    """protocol.until with this module's _PULL_PAUSE."""

    return await protocol.until(city, need_id, chat, ready, tries, deadline, pause=_PULL_PAUSE)


async def _await_line(
    city: City,
    need_id: str | None,
    chat: list[Message],
    head: str,
    token: str,
    *,
    op: str,
    new: bool = False,
    deadline: float | None = None,
) -> tuple[Message, list[Message]] | None:
    """The ledger line for ``token``, or a refusal of ``op`` that was not already in ``chat``.
    ``new``: the line too must not be in ``chat`` (a re-hold: the old ``held`` is stale).
    ``deadline``: time.monotonic() after which no more reads, even with tries left."""

    known = {row.msg_id for row in chat}
    fresh = chat
    for attempt in range(_PULL_TRIES):
        try:
            fresh = await city.history(need_id=need_id)
        except CapError:
            fresh = chat
        hit = _latest([row for row in fresh if row.msg_id not in known] if new else fresh, head, token)
        if hit is not None:
            tools.observe(hit)
            return hit, fresh
        for row in fresh:
            if row.msg_id in known or row.sender != LEDGER:
                continue
            if first_word(row.text) == "refused" and op in words(row.text):
                tools.observe(row)
                return row, fresh
        if attempt + 1 == _PULL_TRIES or deadline is not None and time.monotonic() >= deadline:
            break
        await asyncio.sleep(_PULL_PAUSE)
    return None


def _has_agenda(chat: list[Message]) -> bool:
    agenda = _ledger(chat, "agenda")
    return protocol.when("" if agenda is None else agenda.get("agenda_now", "")) is not None


def _speaks(doctor_id: str, lang: str) -> bool:
    spec = _DOCTORS.get(doctor_id.rsplit("-", 1)[-1])
    return spec is not None and lang in spec[0]


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

    horizon = calendar.deadline(priority, now)  # the priority window (calendar.deadline)
    chosen: list[tuple[datetime, dict[str, str]]] = []
    for line in lines:
        slot = _slot(line)
        slot_id = slot.get("slot_id")
        doctor = slot.get("doctor_id")
        at = protocol.when(slot.get("at", ""))
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
        if not now + _HOLD_LEAD < at or (horizon is not None and at >= horizon):
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
    # _LEAD ahead first. U (the same day) takes the closer ones past _HOLD_LEAD only when none is that
    # far: a sure miss otherwise (as the booking centre's _offer_lead).
    far = [item for item in chosen if item[0] > now + _LEAD]
    chosen = far if far or priority != "U" else chosen
    chosen.sort(key=lambda item: (item[0], item[1].get("slot_id", "")))
    return [slot for _, slot in chosen]


# --- centro prenotazioni and consultorio -------------------------------------------------------


def _ledger(chat: list[Message], head: str) -> dict[str, str] | None:
    """protocol.ledger; an ``agenda`` answer also moves the city clock (_CLOCK, city:clock)."""

    found = protocol.ledger(chat, head)
    if found is not None and head == "agenda":
        new, old = protocol.when(found["agenda_now"]), protocol.when(_CLOCK.get("agenda_now", ""))
        if new is not None and (old is None or new >= old):
            _CLOCK.clear()
            _CLOCK.update(found)
            anchor = {key: found.get(key, "") for key in _ANCHOR}
            if all(anchor.values()) and (
                anchor != _NOTED.get("anchor") or time.monotonic() - _NOTED["at"] > _NOTE_EVERY
            ):
                _NOTED.update(anchor=anchor, at=time.monotonic())
                tools.note("city:clock", "anchor", anchor, ttl_seconds=_CLOCK_TTL)
    return found


# The agenda reply with the latest agenda_now this process read (_ledger keeps it).
_CLOCK: dict[str, str] = {}
# A restarted pod has no _CLOCK until its own agenda reply (live 21/11: 2-4 min, 16 partner requests
# got no_availability), so _ledger shares the reply's anchor in team memory (city:clock) and the
# first turn reads it back in the background (_recall_clock). The anchor stayed one all night (1856
# replies, 4 agents, 21/11 22:30-02:14 UTC). Our own Ledger reply always replaces it.
# ponytail: a city rebase or pause reaches a restarted pod only once some facility notes the new
# anchor; _CLOCK_TTL caps how long an anchor nobody renews is read.
_ANCHOR = ("epoch_real", "epoch_agenda", "rate", "paused")
_CLOCK_TTL = 1800
_NOTE_EVERY = 300.0  # seconds between notes of the same anchor, to keep it inside _CLOCK_TTL
_NOTED: dict = {}
_RECALLED = False


async def _recall_clock() -> None:
    """Cold start: the clock anchor another of our pods noted, unless a Ledger reply came first.
    One _city_now cannot read (missing field, paused) leaves us cold, as before."""

    anchor = tools.fact(await tools.context(), "city:clock", "anchor")
    if not _CLOCK and isinstance(anchor, dict):
        _CLOCK.update({key: str(anchor.get(key, "")) for key in _ANCHOR})
        if _city_now() is None:
            _CLOCK.clear()


def _city_now() -> datetime | None:
    """Agenda now from _CLOCK, without asking: ``epoch_agenda`` + (real now - ``epoch_real``) x
    ``rate``, on weekdays only (no agenda weekends, R:85). None if paused or a field is missing."""

    real, base = protocol.when(_CLOCK.get("epoch_real", "")), protocol.when(_CLOCK.get("epoch_agenda", ""))
    try:
        rate = float(_CLOCK.get("rate", ""))
    except ValueError:
        return None
    if real is None or base is None or _CLOCK.get("paused") == "true":
        return None
    monday = (base - timedelta(days=base.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    week = timedelta(days=5)
    weeks, rest = divmod(min(base - monday, week) + (datetime.now(base.tzinfo) - real) * rate, week)
    return monday + weeks * timedelta(days=7) + rest


def _day_after(world: str) -> datetime | None:
    """No clock: the end of world's first day (``from=``, today's agenda date; else its earliest
    slot). It bounds now from above, so every slot fitting() keeps from it is surely ahead."""

    day = fields_of(world).get("from") or min(
        (_slot(line).get("at", "") for line in world.splitlines() if "slot_id=" in line), default=""
    )
    start = protocol.when(f"{day[:10]}T00:00:00+00:00")
    return None if start is None else start + timedelta(days=1)


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
    """Slots we cancelled or the Ledger refused, cancelled or let expire. The citizen drops the
    rest, so those are offered again."""

    by_offer = {
        offer["offer_id"]: offer["slot_id"]
        for offer in _our_offers(chat, agent)
        if offer.get("offer_id") and offer.get("slot_id")
    }
    dropped: set[str] = set()
    for row in chat:
        fields = fields_of(row.text)
        if (row.sender == LEDGER and first_word(row.text) in {"refused", "cancelled", "expired"}
                and fields.get("slot_id")):
            dropped.add(fields["slot_id"])
        if "cancel" not in words(row.text):
            continue
        slot = by_offer.get(fields.get("offer_id", ""))
        if slot:
            dropped.add(slot)
    return dropped


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
    at = protocol.when(offer.get("agenda_date", ""))
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
            f" {head} On the day, when you arrive, write to me here with your booking code "
            f"{booking_id}: I will register your check-in myself. I am here if you need anything."
        )
    head = f"La sua prenotazione è confermata per {when}." if when else (
        "La sua prenotazione è confermata."
    )
    return (
        f" {head} Il giorno della visita, al suo arrivo, mi scriva qui con il codice di "
        f"prenotazione {booking_id}: registro io il check-in. Per qualsiasi cosa sono a sua "
        "disposizione."
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

    ``offers`` holds every slot id sent. A cancel makes the
    citizen drop all offers, so ``again`` holds only what was re-sent for ``trigger``.
    ``holds`` maps the slots we sent a ``hold`` for to its time.monotonic(), so a duplicate confirm
    does not hold twice and another need's confirm does not hold it meanwhile (_held_elsewhere).
    """

    __slots__ = ("offers", "trigger", "again", "holds")

    def __init__(self) -> None:
        self.offers: set[str] = set()
        self.trigger = ""
        self.again: set[str] = set()
        self.holds: dict[str, float] = {}


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


async def _advance(
    city: City,
    message: Message,
    chat: list[Message],
    *,
    replacing: bool,
) -> None:
    key = message.need_id or message.msg_id
    async with _gated(key):
        await _advance_alone(city, message, chat, key, replacing=replacing)


async def _advance_alone(
    city: City,
    message: Message,
    chat: list[Message],
    key: str,
    *,
    replacing: bool,
) -> None:
    """Consultorio: the citizen's CONS-GIN offers, direct access without a prescription (R:198)."""

    agent = city.agent_id
    citizen = protocol.citizen(chat)
    if citizen is None:
        return
    procedure = "CONS-GIN"
    until = time.monotonic() + _LEDGER_WAIT
    preferences = _family_preferences(chat, citizen)
    en = preferences.get("lang", _lang(chat)) == "en"
    told = [row.text.strip() for row in chat if row.sender == agent and row.recipient == citizen]
    if message.sender != citizen and told and told[-1] in (_BUSY, _BUSY_EN):
        return  # we said "write again later": a late Ledger reply offers nothing, their next message does
    world, now, chat, complete = await _family_slots(city, message.need_id, chat, procedure,
                                                     preferences.get("days", set()), until)
    if world is None or now is None:
        await _say(city, citizen, _BUSY_EN if en else _BUSY)
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
        if message.sender == citizen:
            taken |= _offered(chat, agent) | sent.offers
    else:
        taken = _offered(chat, agent) | sent.offers
    # ``world`` is this ASST's slots and ``procedure=`` narrows it to our service (SKILL.md),
    # so no filter on ``facility=``: the slot names the facility's seat, not always our id.
    days = preferences.get("days", set())
    alternative_days: set[str] = set()
    family_alternative = None
    while True:
        available = fitting(
            [line for line in world.splitlines() if "slot_id=" in line],
            now=now,
            priority=None,
            lang=preferences.get("lang", _lang(chat)),
            gender=preferences.get("gender", _gender(chat)),
            procedure=procedure,
        )
        if family_alternative is None:
            candidates = fitting([line for line in world.splitlines() if "slot_id=" in line],
                                 now=now, priority=None, lang="it", procedure=procedure)
            family_alternative = next((slot for slot in candidates if slot.get("slot_id") not in taken
                                       and slot.get("doctor_id", "").rsplit("-", 1)[-1] in _DOCTORS), None)
        available = [slot for slot in available if slot.get("slot_id") not in taken
                     and slot.get("doctor_id", "").rsplit("-", 1)[-1] in _DOCTORS]
        chosen = [slot for slot in available if not days or slot.get("day") in days][:3]
        if days:
            alternative_days.update(slot["day"] for slot in available
                                    if slot.get("day") in _FAMILY_DAYS and slot["day"] not in days)
        if chosen:
            # Every offer in one message: the citizen chooses from that message only (R:95).
            lines = [protocol.offer_line(slot, None, None) for slot in chosen]
            options = [
                f"{index}. {_in_words(at, en)}"
                for index, slot in enumerate(chosen, 1)
                if (at := protocol.when(slot["at"])) is not None
            ]
            pick = " ".join([*options, _PICK_EN if en else _PICK])
            if await _say(city, citizen, "\n".join([*lines, pick])):
                for slot in chosen:
                    sent.offers.add(slot["slot_id"])
                    if replacing:
                        sent.again.add(slot["slot_id"])
            return
        if taken and not replacing:
            return
        page, pages = protocol.page(world)
        if page >= pages or page >= _MAX_PAGE:
            break
        if not protocol.asked(chat, agent, procedure, page + 1):
            await _say(city, LEDGER, f"world procedure={procedure} page={page + 1}")
        # The next page's reply may never open a turn: read it back here (page reads only: a day
        # read's slots come without a page header, so the loop ends above).
        chat = await _wait_ledger(
            city, message.need_id, chat,
            lambda rows, seen=page: protocol.page(_latest_slots(rows) or "")[0] > seen, until,
        )
        world = _latest_slots(chat) or ""
        if protocol.page(world)[0] <= page:
            complete = False
            break
    if not complete:
        # A read the Ledger did not answer in time may hold the slot that fits: no "no slots".
        await _say(city, citizen, _BUSY_EN if en else _BUSY)
        return
    # No fitting slot on any page: say so, never silence (R:248). Once per case, unless the
    # citizen writes again.
    if message.sender.startswith("citizen:") or not protocol.we(
        chat, agent, "reason=no_slots", to=citizen, word="cancel"
    ):
        if alternative_days:
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
        elif family_alternative:
            # S is empty: the relaxed alternative goes out as an offer outside the preferences
            # (R:232, R:678). The kit has no purpose=alternative and the citizen answers only a
            # purpose it accepts (R:124, R:188).
            spec = _DOCTORS[family_alternative["doctor_id"].rsplit("-", 1)[-1]]
            langs = "/".join(sorted(spec[0]))
            at = protocol.when(family_alternative["at"])
            when = _in_words(at, en) if at else family_alternative.get("day", "")
            note = (f"No appointment meets all your preferences. The closest free one is {when}, with a "
                    f"{'female' if spec[1] == 'f' else 'male'} doctor (languages: {langs}). I have not "
                    "booked anything: if it suits you, reply with your consent reference and I will book it."
                    if en else f"Nessun appuntamento rispetta tutte le sue preferenze. Il primo libero più "
                    f"vicino è {when}, con {'una dottoressa' if spec[1] == 'f' else 'un medico uomo'} "
                    f"(lingue: {langs}). Non ho prenotato nulla: se le va bene, mi risponda con il "
                    "riferimento del consenso e lo prenoto.")
            if await _say(city, citizen, f"{protocol.offer_line(family_alternative, None, None)}\n{note}"):
                sent.offers.add(family_alternative["slot_id"])
        else:
            await _say(city, citizen, _NO_SLOTS_EN if en else _NO_SLOTS)


async def _family_slots(
    city: City, need_id: str | None, chat: list[Message], procedure: str, prefer: set[str], until: float
) -> tuple[str | None, datetime | None, list[Message], bool]:
    """Consultorio citizen: (slot rows, agenda now, case, complete). The rows of the cached world
    answers when warm, else the day reads asked and waited for until ``until`` on the case's Ledger
    rows. Without a clock, or after a refused day=, page 1 as before. Rows None: nothing to go on.
    ``complete``: every read answered (a missing one may hold the slot that fits)."""

    known = {row.msg_id for row in chat}

    def replies(rows: list[Message]) -> list[Message]:
        return [row for row in rows if row.msg_id not in known and row.sender == LEDGER]

    def answered(rows: list[Message]) -> int:
        return sum(_latest_slots([row]) is not None for row in replies(rows))

    agenda = _ledger(chat, "agenda")  # a reply in the case moves the city clock (_CLOCK)
    now = _city_now() or protocol.when("" if agenda is None else agenda.get("agenda_now", ""))
    reads = _day_reads(procedure, now, None, prefer=prefer)
    if reads and _warm(procedure, reads):
        return _cached_lines(procedure), now, chat, True
    if reads:
        missing = [read for read in reads if _fresh_world(procedure, _read_key(read)) is None]
        await asyncio.gather(*(_say(city, LEDGER, read) for read in missing))
        chat = await _wait_ledger(
            city, need_id, chat,
            lambda rows: answered(rows) >= len(missing) or _day_refused(replies(rows)), until,
        )
        _keep_world(procedure, replies(chat), _asks(chat, procedure))
        if not _day_refused(replies(chat)):
            world, complete = _cached_lines(procedure), answered(chat) >= len(missing)
            return (world if world or complete else None), now, chat, complete
        _stop_days()
    # Page reads, as before day reads: no clock yet, or the Ledger refused day=.
    agent = city.agent_id
    if now is None and not protocol.exact(chat, agent, "agenda"):
        await _say(city, LEDGER, "agenda")
    if _latest_slots(chat) is None and not protocol.asked(chat, agent, procedure, 1):
        await _say(city, LEDGER, f"world procedure={procedure} page=1")
    if now is None or _latest_slots(chat) is None:
        chat = await _wait_ledger(
            city, need_id, chat,
            lambda rows: (now is not None or _has_agenda(rows)) and _latest_slots(rows) is not None, until,
        )
    agenda = _ledger(chat, "agenda")
    now = now or _city_now() or protocol.when("" if agenda is None else agenda.get("agenda_now", ""))
    world = _latest_slots(chat)
    return world, now, chat, world is not None


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
    # P2: a partner's cross-ASST consent alone is a consent too (it goes on the hold, below).
    consent = fields.get("consent_ref") or fields.get("cross_asst_consent_ref", "")
    offer_rows = [row for row in chat if row.recipient == message.sender] if hold_self else chat
    offer = _our_offer(offer_rows, agent, offer_id) if offer_id else {}
    slot = offer.get("slot_id") or fields.get("slot_id", "")
    if slot and message.sender.startswith("agent:") and message.sender != _OUR_BOOKING and slot not in {
        o.get("slot_id") for o in _our_offers([row for row in chat if row.recipient == message.sender], agent)
    }:
        # Impostors (live 21/11, 10:00-16:00) confirm slots to make us hold them: an agent books only a
        # slot we offered it in this case. Our booking centre reads the Ledger itself, it gets no offers.
        if not _answered(chat, agent, message.msg_id, message.sender):
            await _say(city, message.sender, f"cancel reason=invalid_offer slot_id={slot}"
                       + _tail(message.sender, fields.get("request_id", "")))
        return
    # We book only our own slots, never forward: a booking party's confirm (R:194), or a citizen's
    # for an offer of ours. A citizen's facility= never sends its confirm, consent and data to an
    # agent it names (injection obeyed, R:243).
    ours = hold_self or message.sender.startswith("agent:") or offer.get("facility") == agent
    if not slot or not consent or not ours:
        # Never silent (R:195): the sender learns which field is missing.
        missing = "slot_id" if not slot else "consent_ref" if not consent else "offer_id"
        head = f"cancel offer_id={offer_id}" if offer_id else "cancel"
        text = f"{head} reason=missing_{missing}" + _tail(message.sender, fields.get("request_id", ""))
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
            await _advance(city, message, chat, replacing=True)
        return
    citizen = message.sender if hold_self else fields.get("citizen") or (
        message.sender if message.sender.startswith("citizen:") else ""
    )
    number = None if hold_self else fields.get("prescription_number") or offer.get("prescription_number")
    sent = _sent(key)
    if _HOSPITAL.get():
        booked = _booked(chat, agent, slot)
        if booked:
            # A duplicate confirm of a booking the Ledger committed: the same receipt again
            # to whoever asked. Never a new hold or commit; once per confirm message.
            if not _answered(chat, agent, message.msg_id, message.sender):
                await _say(
                    city,
                    message.sender,
                    f"booking.receipt booking_id={booked[0]} ledger_ref={booked[1]} slot_id={slot}",
                )
            return
        # After a refusal, expiry or cancellation the old hold is gone: hold again.
        since = _since_hold(chat, agent, slot)
        held = since is not None and not any(_drops_hold(row) for row in since)
    else:
        held = slot in sent.holds or protocol.we(chat, agent, f"slot_id={slot}", to=LEDGER, word="hold")
    if not held and _starts_soon(slot, offer, chat):
        # The Ledger would refuse it EXPIRED minutes from now: its refusal path at once, in this
        # turn (the cancel, and other dates where that path gives them).
        await _on_refused(city, message, chat, hold_self=hold_self, slot=slot, code="EXPIRED")
        return
    if not held and await _held_elsewhere(city, slot, key):
        # Never a second hold on it: this confirm hears the slot is taken and its party offers again.
        await _on_refused(city, message, chat, hold_self=hold_self, slot=slot, code="SLOT_TAKEN")
        return
    if not held:
        parts = ["hold", f"slot_id={slot}"]
        if citizen:
            parts.append(f"citizen={citizen}")
        if number:
            parts.append(f"prescription_number={number}")
        # P2: the hold carries the cross-ASST consent; the commit keeps the booking one.
        parts.append(f"consent_ref={fields.get('cross_asst_consent_ref') or consent}")
        if _HOSPITAL.get():
            _HELD[slot] = time.monotonic()  # out of cross-ASST offers before the 1-5 s say
        if not await _say(city, LEDGER, " ".join(parts)):
            _HELD.pop(slot, None)
            return
        sent.holds[slot] = time.monotonic()
    await _finish_booking(
        city, message, chat, slot, hold_self=hold_self, new=_HOSPITAL.get() and not held
    )


def _starts_soon(slot: str, offer: dict[str, str], chat: list[Message]) -> bool:
    """``slot`` starts within _HOLD_LEAD of the agenda clock (_city_now). Its start: our offer's
    agenda_date, else its line in a world page we read (this case, or _WORLD). Clock or start
    unknown: False, the hold goes as before."""

    now = _city_now()
    at = protocol.when(offer.get("agenda_date", ""))
    if at is None:
        pages = [row.text for row in chat if row.sender == LEDGER] + [page for _, page in _WORLD.values()]
        found = (_slot(line).get("at", "") for page in pages for line in page.splitlines()
                 if f"slot_id={slot}" in line.split())
        at = protocol.when(next((start for start in found if start), ""))
    return now is not None and at is not None and at <= now + _HOLD_LEAD


async def _held_elsewhere(city: City, slot: str, key: str) -> bool:
    """Our hold on ``slot`` for another need still stands: sent in the last _HOLD_SPAN s, and no refusal,
    cancellation or expiry after it in that case (live 21/11: one slot held for 3 needs in 1.5 min, each
    new hold made the older one's commit STALE_FENCE). Older, the Ledger answers (it may let a hold expire
    without a row). ponytail: the needs this process sent a hold for (_SENT); a second pod, a restart or
    a hold still being said is missed, and the Ledger answers instead."""

    for need, entry in list(_SENT.items()):
        if need == key or time.monotonic() - entry.holds.get(slot, float("-inf")) > _HOLD_SPAN:
            continue
        try:
            rows = await city.history(peer=LEDGER, need_id=need)  # our hold and the Ledger's rows only
        except CapError:
            continue
        since = _since_hold(rows, city.agent_id, slot)
        if since is not None and not any(_drops_hold(row) for row in since):
            return True
    return False


# --- facility ----------------------------------------------------------------------------------


def _facility_turn(message: Message) -> bool:
    head = first_word(message.text)
    kind = words(message.text)
    if message.sender == LEDGER and head in {"held", "committed", "checked_in", "reported"}:
        return True
    if message.sender == LEDGER and head == "refused" and {"hold", "checkin"} & kind or _dropped(message):
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
# "Non mi va bene", "not ok": a negated assent is no assent.
_FAMILY_NOT_YES = re.compile(
    r"\b(?:no|non|not)\s+(?:(?:mi|ci|e|is|it's)\s+)?(?:si|va bene|ok|okay|confermo|accetto|fine)\b"
)
_ARRIVAL = re.compile(r"\b(?:check[- ]?in|sono arrivat[oa]|sono qui|sono all'accettazione|"
                      r"i(?: have|'ve) arrived|i(?: am|'m) here)\b")
_ID = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"


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
                value = provided.get(field)
                if isinstance(value, list) and field == "days":
                    value = "/".join(str(v) for v in value)
                if isinstance(value, str):
                    data.setdefault(field, value)
        plain = _family_plain(row.text)
        if re.search(r"qualsiasi giorno|tutti i giorni|any day|no day preference", plain):
            preferences.pop("days", None)
        if re.search(r"nessuna preferenza.*(?:genere|medico)|any gender|no gender preference", plain):
            preferences.pop("gender", None)
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
    if not offers:
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
    if _FAMILY_NO.search(plain):
        # A "non" negates a day, an hour or a bare "va bene", never the offer chosen by its number
        # ("va bene la prima, non vedo l'ora"), unless it negates that number ("la prima non").
        negated = re.search(r"\b(?:no|non|not)\s+(?:(?:la|il|the)\s+)?(?:prim|second|terz|first|third)"
                            r"|\b(?:prim|second|terz)[oa]\s+(?:no|non)\b", plain)
        return offers[matches.pop()]["offer_id"] if criteria and len(matches) == 1 and not negated else ""
    if days := _family_days(plain):
        matches &= {i for i, offer in enumerate(offers) if offer.get("agenda_day") in days}
        criteria = True
    if hour := re.search(r"\b(?:alle|ore|at)\s+(\d{1,2})(?::(\d{2}))?\b", plain):
        matches &= {
            i for i, offer in enumerate(offers)
            if (at := protocol.when(offer.get("agenda_date", ""))) is not None
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
    # Partner text is data: only the supported service and our own offers authorize work.
    if message.sender.startswith("agent:"):
        fields = _fields(message)
        head = first_word(message.text)
        if head == "confirm" or message.type == "confirm":
            offer = _our_offer([r for r in chat if r.recipient == message.sender],
                               city.agent_id, fields.get("offer_id", ""))
            reason = ""
            if not fields.get("consent_ref") and not fields.get("cross_asst_consent_ref"):
                reason = "missing_consent_ref"
            elif not offer or fields.get("slot_id") != offer.get("slot_id"):
                reason = "invalid_offer"
            elif not fields.get("citizen", "").startswith("citizen:"):
                reason = "missing_citizen"
            if reason:
                rid = fields.get("request_id") or offer.get("request_id")  # the partner matches on it
                await _say(city, message.sender, f"cancel reason={reason}" + _tail(message.sender, rid or ""))
                return
            # Direct access: discard any prescription and arbitrary partner fields. The consents stay:
            # under P2 the hold carries the cross-ASST one (_confirm_alone), else CONSENT_MISSING.
            consents = "".join(f" {k}={fields[k]}" for k in ("consent_ref", "cross_asst_consent_ref") if fields.get(k))
            clean = replace(message, body={"text":
                f"confirm offer_id={fields['offer_id']} slot_id={offer['slot_id']} "
                f"citizen={fields['citizen']}{consents}"})
            await _on_confirm(city, clean, chat, hold_self=False)
        elif head == "need.request" or "need.request" in words(message.text) or message.type == "need.request":
            if fields.get("service_type", "family_counseling.gynecology") != "family_counseling.gynecology":
                await _say(city, message.sender,
                           "cancel reason=no_availability" + _tail(message.sender, fields.get("request_id", "")))
                return
            await _cross_asst(
                city, message, procedures=("CONS-GIN",), ours="del nostro consultorio"
            )
        return
    if message.sender == LEDGER:
        partner = any(r.sender.startswith("agent:") and r.sender != city.agent_id
                      and first_word(r.text) == "confirm" for r in chat)
        if partner and _facility_turn(message):
            await _facility(city, message, chat, reports=False, hold_self=False)
            return
        if first_word(message.text) == "refused" and "commit" in words(message.text):
            await _on_refused(city, message, chat, hold_self=True)
        elif first_word(message.text) == "held":
            slot = fields_of(message.text).get("slot_id", "")
            if slot:
                await _finish_booking(city, message, chat, slot, hold_self=True)
        elif _facility_turn(message):
            await _facility(city, message, chat, reports=False, hold_self=True)
        elif not partner:
            # Agenda/world replies already consumed by history must not replace live offers. After a
            # partner's confirm the only citizen in the chat is theirs (protocol.citizen falls back to
            # its citizen=): never offer to them.
            own = [r for r in chat if r.sender == city.agent_id and r.recipient.startswith("citizen:")]
            if not any(first_word(r.text) in {"offer", "consent", "booking.receipt", "checkin"} for r in own):
                await _advance(city, message, chat, replacing=False)
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
    # Offers go out in several messages (live 000123: three of two each): every offer we sent
    # this citizen and did not cancel is live, not only those of the last message.
    withdrawn = {fields_of(row.text).get("offer_id") for row in own if first_word(row.text) == "cancel"}
    offers, seen = [], set()
    for offer in _our_offers([row for row in own if first_word(row.text) == "offer"], city.agent_id):
        oid = offer.get("offer_id")
        if oid and offer.get("slot_id") and oid not in withdrawn and oid not in seen:
            seen.add(oid)
            offers.append(offer)
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
    # A "non" is a refusal only in a message that neither chooses an offer nor assents: "va bene la
    # prima, non vedo l'ora" chooses (live 10:18-10:27: 4 citizen_declined cancelled every offer).
    chose = (message.type == "confirm" or first_word(message.text) == "confirm" or data.get("offer_id")
             or _family_pick(plain, offers) or _FAMILY_YES.search(_FAMILY_NOT_YES.sub(" ", plain)))
    denied = data.get("granted", "").lower() == "false" or (
        not data.get("consent_ref") and bool(_FAMILY_NO.search(plain)) and not chose
    )
    if pending and denied:
        await _say(city, citizen, f"cancel offer_id={pending} reason=consent_denied")
        return

    cancelled = message.type == "cancel" or first_word(message.text) == "cancel"
    if cancelled or (offers and not pending and denied):
        rejected = data.get("offer_id", "")
        if rejected and not any(o["offer_id"] == rejected for o in offers):
            return
        if offers:
            for offer in offers:
                if not rejected or offer["offer_id"] == rejected:
                    await _say(city, citizen, f"cancel offer_id={offer['offer_id']} reason=citizen_declined")
            await _advance(city, message, chat, replacing=True)
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
        await _advance(city, message, chat, replacing=True)
        return
    if explicit or offer_id or offers:
        selected = next((offer for offer in offers if offer["offer_id"] == offer_id), None)
        if not selected or data.get("slot_id") not in (None, selected["slot_id"]):
            # The choice is a confirm: a data.request for offer_id makes the citizen send back an
            # empty data.response, again and again (live 000123: ~30 times). Ask once, in prose.
            prompt = (
                "Which of the proposed appointments would you like? Reply with your choice and "
                "your consent reference, and I will book it right away."
                if en
                else "Quale degli appuntamenti proposti preferisce? Mi risponda con la sua scelta "
                "e il riferimento del consenso, e prenoto subito."
            )
            if not any(row.text.strip() == prompt for row in own):
                await _say(city, citizen, prompt)
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
    await _advance(city, message, chat, replacing=waiting_for_days)


def _report_turn(message: Message) -> bool:
    """A specialist turn that can touch the receipt, the check-in note or the report.

    The confirm turn and the Ledger's held/committed push ran at once and both sent the
    receipt (live, 000169): they take the same per-case lock as the check-in and the report."""

    if message.sender == LEDGER:
        return first_word(message.text) in {"held", "committed", "checked_in", "reported"}
    if message.sender.startswith("citizen:"):
        return True
    return "confirm" in words(message.text) or message.type == "confirm"


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
    if _dropped(message):
        await _on_dropped(city, message, chat, hold_self=hold_self)
    elif message.sender == LEDGER and head == "held":
        await _on_held(city, message, chat, direct=hold_self)
    elif message.sender == LEDGER and head == "committed":
        await _on_committed(city, message, chat, direct=hold_self)
    elif message.sender == LEDGER and head == "refused" and "hold" in kind:
        await _on_refused(city, message, chat, hold_self=hold_self)
    elif message.sender == LEDGER and head == "checked_in":
        await _on_checked_in(city, message, chat, reports=reports)
    elif message.sender == LEDGER and head == "refused" and "checkin" in kind:
        # The refusal after the arrival turn stopped waiting for it: as the hospital, one answer.
        await _checkin_refused(city, message, chat, booking_id="", en=_en(chat))
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
    elif "confirm" in kind or message.type == "confirm":
        await _on_confirm(city, message, chat, hold_self=hold_self)
    elif message.sender.startswith("citizen:"):
        await _redirect(city, message, chat)


def _dropped(message: Message) -> bool:
    """A Ledger row that ends our hold or booking outside our turn: cancelled, expired, or a
    refused commit. A refused hold has its own branch (_on_refused)."""

    head = first_word(message.text)
    return message.sender == LEDGER and (
        head in {"cancelled", "cancel", "expired"}
        or head == "refused" and bool({"commit", "op=commit"} & words(message.text))
    )


async def _on_dropped(
    city: City, message: Message, chat: list[Message], *, hold_self: bool
) -> None:
    """Every facility: the Ledger dropped our booking or let the hold expire, or refused a commit
    after the confirm turn stopped waiting: whoever asked hears it once (_on_refused). The answer
    to our own release goes to the booking party that asked for it (_release)."""

    if not _released(chat, city.agent_id, message):
        row = message
        if first_word(message.text) == "expired" and not {"code", "reason"} & fields_of(message.text).keys():
            # ponytail: no expired row in the kit or live yet; its code goes on if any.
            row = replace(message, body={**message.body, "text": f"{message.text} reason=expired"})
        await _on_refused(city, row, chat, hold_self=hold_self)
        return
    # The answer to our own release, after its turn stopped waiting (a slow Ledger): the booking
    # party that asked hears the outcome now. ponytail: the case's latest release request; one
    # release per case.
    asked = next((row for row in reversed(chat) if row.sender.startswith("agent:")
                  and row.recipient == city.agent_id
                  and (first_word(row.text) == "cancel" or row.type == "cancel")), None)
    if asked is not None:
        await _release(city, asked, chat)


async def _redirect(
    city: City, message: Message, chat: list[Message], en: bool | None = None
) -> None:
    """Un messaggio del cittadino che non sappiamo gestire: rinvio al ruolo giusto, mai una
    risposta inventata, e una volta sola per caso. ``en``: la lingua già scelta dal chiamante."""

    if _en(chat) if en is None else en:
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
    if not protocol.exact(chat, city.agent_id, text):
        await _say(city, message.sender, text)


# --- ospedale (solo role == "hospital") --------------------------------------------------------

# Arrival in free text, it/en: the specialist's _ARRIVED (below), shared with the hospital.
# "Ho l'appuntamento" counts only when it is not a question.
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
# The specialist's catalogue codes: the cardiology visit and its control visit (_CONTROL).
_CARDIOLOGY = ("VIS-CARD", _CONTROL)


async def _hospital(city: City, message: Message, chat: list[Message]) -> None:
    head = first_word(message.text)
    kind = words(message.text)
    # A booking party calls off one of our slots for the citizen (R:195): never a confirm.
    release = message.sender.startswith("agent:") and (head == "cancel" or message.type == "cancel")
    confirm = not release and ("confirm" in kind or message.type == "confirm")
    from_citizen = message.sender.startswith("citizen:")
    if from_citizen and not {"offer_id", "slot_id"} & _fields(message).keys():
        confirm = False  # "I'm here to confirm my appointment" is prose, not a booking confirm
    if from_citizen and not confirm and (
        _arrival(message)
        or (
            ("booking_id" in _fields(message) or _prose_code(message.text, chat, city.agent_id))
            and _asked_booking(chat, city.agent_id)
        )
    ):
        await _arrive(city, message)
    elif from_citizen and not confirm:
        await _answer(city, message, chat)
    elif message.sender == LEDGER and (head == "checked_in" or head == "refused" and "checkin" in kind):
        # The Ledger's answer after the arrival turn stopped waiting for it (live: always): this
        # turn answers the citizen. After any arrival turn still waiting, on the case re-read, so
        # one answer only.
        async with _gated(f"arrive:{message.need_id or message.msg_id}"):
            try:
                chat = await city.history(need_id=message.need_id)
            except CapError:
                pass
            if head == "checked_in":
                await _on_checked_in(city, message, chat, reports=False, en=_english(chat))
            else:
                await _checkin_refused(city, message, chat, booking_id="", en=_english(chat))
    elif message.sender == LEDGER and head == "agenda":
        # A late agenda reply as its own turn (live 1.0.9: all of them, 2-4 min after the ask): it
        # sets the city clock that _cross_asst needs to answer from _WORLD. Ignored, every request
        # went cold and got no_availability (21/11 02:34, 22 of 22).
        _ledger([message], "agenda")
        await _answer_pending(city, chat)
    elif message.sender == LEDGER and _latest_slots([message]) is not None:
        # A world reply as its own turn (live: most do): _cross_asst no longer waits for it, so
        # it fills _WORLD here when it is a page of our procedure we asked for, by its content
        # and page, never by which ask came last (the Ledger answers out of order; a ``world
        # page=1`` from _ask_slots lists every facility's slots; an empty page, the SDK format
        # without taken slots, names none). Then the requests of the case still waiting for it
        # get their answer.
        world = _latest_slots([message]) or ""
        asks = _asks(chat, _PROCEDURE)  # the page or weekday it answers must be one we asked
        if (_page_procedure(world) == _PROCEDURE or "slot_id=" not in world) and _world_key(world, asks) in asks:
            _keep_world(_PROCEDURE, [message], asks)
            await _answer_pending(city, chat)
    elif (
        message.sender.startswith("agent:")
        and not confirm
        and ("need.request" in kind or message.type == "need.request")
        # A partner's search names request_id, procedure or service_type (R:191). The
        # conformance probe ({"service": "lab", ...}) names none: answering it with late
        # Ledger reads and a cancel spilled effects into later stages and failed the deploy.
        and {"request_id", "procedure", "service_type"} & _fields(message).keys()
    ):
        await _cross_asst(city, message)
        await _answer_pending(city, chat, skip=message.msg_id)
    else:
        # One booking step at a time per case, on a fresh read of it: the held and committed
        # pushes open turns of their own while the confirm turn waits for the same lines, and
        # both committed the hold (live: every commit twice, DUPLICATE) and sent the receipt.
        # ponytail: in-process lock; two pods during a roll out can still race.
        async with _gated(f"book:{message.need_id or message.msg_id}"):
            try:
                chat = await city.history(need_id=message.need_id)
            except CapError:
                pass
            if release:
                await _release(city, message, chat)
            else:
                booking = _facility(city, message, chat, reports=False, hold_self=False)
                if confirm and message.sender != LEDGER:
                    # The visit's time, read alongside the hold: world goes out first, so it
                    # still lists the slot, and the hold, commit and receipt do not wait for it.
                    await asyncio.gather(_ask_slots(city, message, chat), booking)
                else:
                    await booking
        by = _BOOK_BY.get()
        late = confirm and by is not None and time.monotonic() > by
        # Past the budget the confirm turn ends: the held/committed push sends the welcome (once).
        if (confirm and not late) or (message.sender == LEDGER and head in {"held", "committed"}):
            # Proactive (judge: "physical address + register arrival by chat"): the welcome names
            # our door; it already says how to register the arrival by chat. Without a street,
            # _welcome's own default text.
            street = _own_street(city).replace("=", "").replace("\n", " ")
            place = {
                "place_it": f"l'ospedale dove si svolgerà la sua visita, in {street}",
                "place_en": f"the hospital where your visit will take place, at {street}",
            } if street else {}
            await _welcome(city, message, **place)


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


async def _cross_asst(
    city: City,
    message: Message,
    *,
    procedures: tuple[str, ...] = (_PROCEDURE,),
    ours: str = "del nostro ospedale",
    pending: bool = False,
) -> None:
    """R:191-192: another ASST's booking centre asks for our slots, criteria only. One answer
    per request, inside the fast reply (R:246): ``offer`` lines of our own slots from the
    ``world`` reads, else ``cancel reason=no_availability``. Never citizen data, never an
    attribute the Ledger did not write. The confirm that follows takes _on_confirm.

    The reads are one per weekday of the asker's window (_day_reads; page reads without a clock).
    The answer comes at once from fresh cached answers (_WORLD), else the missing reads are asked
    and the case's Ledger rows waited for up to _LEDGER_WAIT. The clock is _city_now (agenda is
    asked only without it), else world's first day (_day_after). A partner asks because its own
    set is empty: with nothing in the priority window we offer the first free slots after it
    (relaxed alternative, R:232), and one h3 slot unless it meets fewer of the named
    lang/gender/days.

    No reply by then and nothing useful cached means we do not know: never a no_availability (a
    false attribute, -2) but one ``cancel reason=facility_unavailable``, which closes the request:
    the late reply only refreshes the cache. ``pending`` (_answer_pending, a request never
    answered): from _WORLD and _city_now only, without waiting (a read it needs is asked, and its
    push turn answers)."""

    start = time.monotonic()
    deadline = start + _LEDGER_WAIT  # live 21/11: a Ledger reply takes ~595 s, the ack must go < 10 s
    fields = _fields(message)
    rid = fields.get("request_id", "")
    tag = f"request_id={rid}" if rid else ""
    sender = message.sender
    key = f"cross:{sender}:{rid or message.msg_id}"
    async with _gated(key):
        sent = _sent(key)
        try:
            chat = await asyncio.wait_for(city.history(need_id=message.need_id), timeout=1.0) if procedures == ("CONS-GIN",) else await city.history(need_id=message.need_id)
        except (CapError, TimeoutError):
            chat = []
        agent = city.agent_id
        completed_chat = [row for row in chat if not (
            procedures == ("CONS-GIN",) and row.sender == agent and row.recipient == sender
            and first_word(row.text) not in {"offer", "cancel", "booking.receipt"})]
        # ``pending``: a later message to the partner (another request's answer) does not answer it.
        # With a request_id only its own answer does: the Relay delivers minutes late, so the history can
        # list this request before our answer to an earlier one (live 21/11, specialist: never answered).
        if sent.offers or not pending and not tag and _answered(completed_chat, agent, message.msg_id, sender) or tag and any(
            row.sender == agent and row.recipient == sender and tag in row.text.split()
            for row in completed_chat
        ):
            return
        procedure = fields.get("procedure") or procedures[0]
        priority = fields.get("priority", "").upper()
        priority = priority if priority in {"U", "B", "D", "P"} else None
        # MACH-1.3 (21/11, last 3 h: 179 of 669 partner requests to the hospital named deadline=, 44
        # after=; the specialist 80 of 184): the asker's own window, agenda time, counted from the
        # need's birth. Our relaxed slots past it fell outside its S (-1 to it, refused confirms).
        # Zone optional (the agenda runs in UTC); unreadable = absent; without them nothing changes.
        def agenda_at(key: str) -> datetime | None:
            return protocol.when(fields.get(key, "")) or protocol.when(fields.get(key, "") + "Z")

        after, until = agenda_at("after") or agenda_at("earliest"), agenda_at("deadline")
        seen: list[str] = []  # every slot line of the pages read: next_free after the window
        free_after = ""
        chosen: list[dict[str, str]] = []
        relaxed = False
        unavailable = False  # no Ledger reply in time and nothing useful cached
        owed = 0  # hospital, specialist: the last world page answered without its reply
        later: list[str] = []  # hospital, specialist, cache hit: Ledger reads after the answer
        # Only our own service: world lists every slot of the ASST, other facilities' too.
        if procedure in procedures:
            hospital = procedures == (_PROCEDURE,)
            # The specialist gets the same search (2 world pages, city clock, relaxed dates);
            # only the hospital's rare-language (h3) step stays its own.
            rich = hospital or procedures == _CARDIOLOGY
            family = procedures == ("CONS-GIN",)
            use_cache = rich or family
            known = {row.msg_id for row in chat}

            def fresh(rows: list[Message]) -> list[Message]:
                return [row for row in rows if row.msg_id not in known]

            def clock(rows: list[Message]) -> datetime | None:
                agenda = _ledger(fresh(rows), "agenda")  # a fresh reply also refreshes _CLOCK
                cached = _city_now() if use_cache else None
                return cached or protocol.when("" if agenda is None else agenda.get("agenda_now", ""))

            def page_of(rows: list[Message], page: int) -> str | None:
                world = _latest_slots(fresh(rows))
                return world if world is not None and protocol.page(world)[0] >= page else None

            def answered(rows: list[Message]) -> int:
                return sum(row.sender == LEDGER and _latest_slots([row]) is not None for row in fresh(rows))

            # One read per weekday of the asker's window (_day_reads); page reads without a clock or
            # after a refused day=. The asked days= go first.
            days = _day_reads(procedure, _city_now() if use_cache else None, priority, after, until,
                              {d for d in re.split(r"[\s,/;|]+", fields.get("days", "").lower()) if d})

            def warm(page: int) -> str | None:
                # A fresh cached answer for every read and a known city clock answer at once: the day
                # reads' slot rows (or page 1's), else the cached page.
                reads = days or [f"world procedure={procedure} page={page}"]
                if not use_cache or not _city_now() or not _warm(procedure, reads):
                    return None
                return _cached_lines(procedure) if days else _fresh_world(procedure, page)

            if warm(1) is not None:
                if not pending:
                    later.append("agenda")  # a say to the Ledger takes 1-5 s live: after the answer
            elif pending:
                return
            elif not days:
                await _say(city, LEDGER, "agenda")
            page = 1
            now: datetime | None = None
            complete = True  # every read answered: a no_availability is true
            found: list[dict[str, str]] = []
            every: list[dict[str, str]] = []
            while True:
                cached = warm(page)
                # Past page 1, page 1 is read again with this one: both land fresh together (live 21/11
                # 10:18: page 2 came 6 min after its page 1, _answer_pending found page 1 stale and the
                # partner never had an answer).
                reads = days or [f"world procedure={procedure} page={n}" for n in dict.fromkeys((1, page))]
                if cached is not None:
                    # Answer now; the read goes out after the answer and refreshes the cache.
                    rows, world, owed = chat, cached, page
                    if not pending:
                        later += reads if days else reads[-1:]
                elif pending:
                    # Not read yet: asked once, its own push turn answers.
                    asked = _asks(chat, procedure)
                    await asyncio.gather(*(_say(city, LEDGER, read) for read in reads if _read_key(read) not in asked))
                    world = None
                    break
                else:
                    asks = [read for read in reads if not days or _fresh_world(procedure, _read_key(read)) is None]
                    later = [read for read in later if read not in reads]
                    await asyncio.gather(*(_say(city, LEDGER, read) for read in asks))

                    def ready(rows: list[Message], page: int = page, n: int = len(asks)) -> bool:
                        if days and _day_refused(fresh(rows)):
                            return True
                        got = answered(rows) >= n if days else page_of(rows, page) is not None
                        return got and clock(rows) is not None

                    rows = await _wait_ledger(city, message.need_id, chat, ready, deadline)
                    if use_cache:
                        _keep_world(procedure, fresh(rows), _asks(rows, procedure))
                    if days and _day_refused(fresh(rows)):
                        _stop_days()  # page reads, inside the same wait
                        days = []
                        continue
                    complete = answered(rows) >= len(asks) if days else page_of(rows, page) is not None
                    world = _cached_lines(procedure) if days else page_of(rows, page)
                    world = world if world or complete else None
                now = clock(rows) or (_day_after(world) if rich and world else None)
                if world is None or now is None:
                    complete = False
                    break
                lines = [line for line in world.splitlines() if "slot_id=" in line]
                lines = _lead(lines, fields, now, after, priority)  # MACH-2.1 R5: no after=, lease_s ahead
                seen += lines
                if after or until:
                    lines = [line for line in lines if (when := protocol.when(_slot(line).get("at", "")))
                             and (after is None or when >= after) and (until is None or when <= until)]
                # The asker's deadline replaces our priority horizon (a U request keeps its reserve).
                found = fitting(lines, now=now, priority="P" if until and priority != "U" else priority,
                                lang="it", procedure=procedure)
                if procedures == ("CONS-GIN",):
                    wanted_days = set(re.split(r"[/,]", fields.get("days", ""))) - {""}
                    found = [slot for slot in found
                             if (spec := _DOCTORS.get(slot.get("doctor_id", "").rsplit("-", 1)[-1]))
                             and fields.get("lang", "it") in spec[0]
                             and (not fields.get("gender_preference") or fields["gender_preference"] == spec[1])
                             and (not wanted_days or slot.get("day") in wanted_days)]
                    if not found and page < min(protocol.page(world)[1], _MAX_PAGE) and time.monotonic() < deadline:
                        page += 1
                        continue
                if not rich:
                    break
                # ponytail: read as priority P, so a U request's relaxed slots skip the U reserve.
                every = fitting(lines, now=now, priority="P", lang="it", procedure=procedure)
                if every or page >= min(protocol.page(world)[1], 2) or time.monotonic() >= deadline:
                    break
                page += 1
            if pending and world is None:
                return  # stays for its push turn
            # A read not answered within _LEDGER_WAIT: without an offer from what we have, we do not know,
            # so never a no_availability (a false attribute, -2) but facility_unavailable, which closes
            # the request; the late reply only refreshes the cache.
            unavailable = world is None or not complete
            if hospital:
                # Without slots we hold, least offered first: partners no longer race for one 3.
                found, every = _spread(found), _spread(every)
            relaxed = rich and not found and bool(every)
            pool = found or every
            chosen = pool[:3]
            if hospital:
                # Partners mostly want h3, our only sq speaker: no lang= reads as sq. The
                # earliest slot in the asked language first, then by time, and always one h3.
                # Level 5: a named lang/gender/days ranks first the slots meeting most of them.
                lang = (fields.get("lang") or _RARE).lower()
                first = next((slot for slot in pool if _speaks(slot["doctor_id"], lang)), None)
                pool = ([first] if first else []) + [slot for slot in pool if slot is not first]
                pool = _by_constraints(pool, fields)
                rare = next((s for s in (*pool, *every) if _speaks(s["doctor_id"], _RARE)), None)
                chosen = pool[:3]
                # The h3 goes in unless it meets fewer named constraints than the last offer:
                # the sort is stable, so rare stays first on a tie.
                if rare is not None and rare not in chosen and (
                    _by_constraints([rare, chosen[-1]], fields)[0] is rare
                ):
                    chosen = [*chosen[:2], rare]
                at = time.monotonic()
                for slot in chosen:
                    _OFFERED.setdefault(slot["slot_id"], []).append(at)
            elif rich and pool:
                # Level 5: the partner's lang/gender/days, when named, rank the slots that meet
                # the most of them first; time order within a tie. Only Ledger attributes go out.
                chosen = _by_constraints(pool, fields)[:3]
            if until is not None and not chosen and now is not None:
                free_after = next((when.strftime("%Y-%m-%dT%H:%M") for slot in fitting(
                    seen, now=now, priority="P", lang="it", procedure=procedure)
                    if (when := protocol.when(slot["at"])) and when > until), "")
        if chosen:
            lines = [protocol.offer_line(slot, None, priority) + _tail(sender, rid) for slot in chosen]
            beyond = (
                f"Entro i tempi della priorità {priority or 'richiesta'} non abbiamo posti liberi: "
                "queste sono le prime date libere subito dopo. "
                if relaxed
                else ""
            )
            text = "\n".join([
                *lines,
                f"Buongiorno, ecco le prime disponibilità {ours} per la vostra richiesta. "
                f"{beyond}Per prenotarne una inviateci la scelta con il riferimento del consenso "
                "del cittadino: la registriamo sul Ledger e vi mandiamo subito la ricevuta."
                + (
                    # A partner's confirm without the prescription is refused (POLICY_VIOLATION).
                    " Nel confirm indicate offer_id, slot_id, citizen, prescription_number e "
                    "consent_ref, più cross_asst_consent_ref se la regola lo richiede."
                    if rich
                    else ""
                ),
            ])
        elif unavailable:
            # MACH-2.1: "struttura chiusa o non raggiungibile"; the asker offers the rest, no retry.
            text = (
                "cancel reason=facility_unavailable" + _tail(sender, rid)
                + " Mi dispiace, in questo momento non riusciamo a consultare la nostra agenda sul registro."
            )
        else:
            text = (
                "cancel reason=no_availability" + _tail(sender, rid)
                + (f" next_free={free_after}" if free_after else "")
                + " Mi dispiace, al momento non abbiamo disponibilità adatte a questa richiesta. "
                "Riscriveteci pure più avanti: ricontrolliamo subito l'agenda."
            )
        # Marked even if the say fails: a redelivery is not a retry after a 4xx.
        sent.offers.add(key)
        delivered = await _say(city, sender, text)
        if procedures == ("CONS-GIN",):
            _LOG.info("care.family.cross_reply", extra={"request_id": rid,
                      "elapsed_seconds": round(time.monotonic() - start, 3),
                      "delivered": delivered, "cached": bool(later),
                      "result": "offer" if chosen else "facility_unavailable" if unavailable else "no_availability"})
        if owed:  # the refresh reads below go out only with owed: drop those sent lately
            tick = time.monotonic()
            later = [read for read in later
                     if tick - _REFRESHED.get(read, float("-inf")) >= _REFRESH_EVERY[first_word(read)]]
            _REFRESHED.update(dict.fromkeys(later, tick))
        if owed and procedures == (_PROCEDURE,):
            # Hospital: the fast reply (+1, R:246) is scored on the ack, sent when the turn ends,
            # so nothing waits after the answer: the reads go out together and the world reply
            # fills _WORLD in its own push turn (_hospital).
            await asyncio.gather(*(_say(city, LEDGER, read) for read in later))
            return
        if owed and not hospital:
            # Specialist: the fast reply (+1, R:246) is measured on the ack, sent at the end of
            # the turn: end it now. The late reads go out in the background, and the Ledger's
            # world answer refreshes the cache as a turn of its own (handle -> _keep_world_reply).
            if later:
                _background(_say_later(city, list(later)))
            return


async def _answer_pending(city: City, chat: list[Message], *, skip: str = "", **cross) -> None:
    """Hospital and specialist: the partner searches of this case we have not answered (the
    Ledger's world came after their turn, or before a restart), answered now from the cached page
    (_cross_asst ``pending``, with ``cross``: the specialist's procedures and wording). One we
    answered, in this process or in the history (our facility_unavailable too), is skipped."""

    procedures = cross.get("procedures", (_PROCEDURE,))
    if not any(proc in procedures and time.monotonic() - at <= _WORLD_FRESH for (proc, _), (at, _) in list(_WORLD.items())):
        return  # nothing to answer from: no history read in this turn
    agent = city.agent_id
    for row in chat:
        found = _fields(row)
        kind = words(row.text)
        rid = found.get("request_id", "")
        key = f"cross:{row.sender}:{rid or row.msg_id}"
        if (
            row.msg_id == skip
            or row.recipient != agent
            or not row.sender.startswith("agent:")
            or not in_ring(row.sender)  # MACH-2.1 R17: never an answer outside the ring
            or "confirm" in kind
            or not ("need.request" in kind or row.type == "need.request")
            or not {"request_id", "procedure", "service_type"} & found.keys()
            or key in _SENT and _SENT[key].offers
        ):
            continue
        if rid:
            done = any(r.sender == agent and r.recipient == row.sender and f"request_id={rid}" in r.text.split()
                       for r in chat)
        else:
            done = _answered(chat, agent, row.msg_id, row.sender)
        if not done:
            await _cross_asst(city, row, pending=True, **cross)


# Background work of this process (late Ledger reads): kept referenced until it is done.
_TASKS: set[asyncio.Task] = set()


def _background(coro) -> None:
    task = asyncio.ensure_future(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def _say_later(city: City, reads: list[str]) -> None:
    """Ledger reads sent after the turn's answer; their replies come back as Ledger turns."""

    for read in reads:
        try:
            await _say(city, LEDGER, read)
        except Exception:  # best effort: the answer already went out
            return


def _keep_world_reply(message: Message, chat: list[Message]) -> bool:
    """A Ledger ``world`` answer arriving as its own turn refreshes the cache of its procedure
    (every slot line names it; ``chat``, the case, names the reads we asked). True if it was a
    world answer."""

    world = _latest_slots([message])
    if world is None:
        return False
    procedure = _page_procedure(world)
    if procedure:
        _keep_world(procedure, [message], _asks(chat, procedure))
    return True


def _page_procedure(world: str) -> str:
    """The one procedure every slot line of a world page names, else "" (a mixed page)."""

    procedures = {
        _slot(line).get("procedure") for line in world.splitlines() if "slot_id=" in line
    } - {None, ""}
    return procedures.pop() if len(procedures) == 1 else ""


def _recover(chat: list[Message], agent: str) -> None:
    """Hospital (specialist: the clock only), a pod that restarted or never saw this case (two
    pods): the city clock and our world pages come back from the case's Ledger rows, never
    guessed, so the cross-ASST answer, _alternatives and _starts_soon do not start cold. Only
    what this process lacks: _CLOCK when empty (one anchor all night, _ANCHOR), a page it has
    not cached. A page counts as read when
    the Ledger sent it (``sent_at``): older than _WORLD_FRESH it stays out. Every slot we sent a
    hold for in the case leaves our cached pages, these and the ones we had: ours now, or lost
    (_HELD and _on_refused, in the memory of the pod that sent the hold). The next world read
    brings a freed one back."""

    if not _CLOCK:
        agenda = protocol.ledger(chat, "agenda")
        _CLOCK.update(agenda or {})
        if _city_now() is None:
            _CLOCK.clear()
    found: dict[tuple[str, int | str], tuple[float, str]] = {}
    real = datetime.now().astimezone()
    asks = _asks(chat, _PROCEDURE)
    for row in chat:
        world = _latest_slots([row])
        sent = protocol.when(row.sent_at or "")
        if world is None or sent is None or _page_procedure(world) != _PROCEDURE:
            continue
        at = time.monotonic() - max(0.0, (real - sent).total_seconds())
        key = (_PROCEDURE, _world_key(world, asks))
        if time.monotonic() - at < _WORLD_FRESH and at >= found.get(key, (at, ""))[0]:
            found[key] = (at, world)
    held = {f"slot_id={fields_of(row.text).get('slot_id', '')}" for row in chat
            if row.sender == agent and row.recipient == LEDGER and first_word(row.text) == "hold"}
    for key, hit in found.items():
        _WORLD.setdefault(key, hit)
    for key, (at, world) in list(_WORLD.items()) if held else ():
        if key[0] == _PROCEDURE:
            _WORLD[key] = (at, "\n".join(line for line in world.splitlines() if not held & set(line.split())))


def _keep_world(procedure: str, rows: list[Message], asks: set[int | str] = frozenset()) -> None:
    """Every ``world`` answer in ``rows`` becomes a cached answer of ``procedure`` (keyed by
    procedure, so VIS-FIS and VIS-CARD never mix, then by _world_key: ``asks``, our reads)."""

    for row in rows:
        world = _latest_slots([row])
        if world is not None and (key := _world_key(world, asks)) is not None:
            _WORLD[(procedure, key)] = (time.monotonic(), world)


def _day_reads(
    procedure: str,
    now: datetime | None,
    priority: str | None,
    after: datetime | None = None,
    until: datetime | None = None,
    prefer: set[str] = frozenset(),
) -> list[str]:
    """``world`` reads of one weekday each, the nearest ones of the window, at most 3, the
    ``prefer`` days (asked by the citizen or the partner) first. The window: from now + _LEAD (U:
    _HOLD_LEAD, as fitting) or ``after``, to ``until`` (the asker's deadline=), else the priority's
    horizon (calendar.deadline). A window without a weekday still reads its first one. [] without
    a clock, or once the Ledger refused day= (_BY_DAY): page reads then."""

    if now is None or not _BY_DAY:
        return []
    day = max(now + (_HOLD_LEAD if priority == "U" else _LEAD), after or now)
    horizon = until or calendar.deadline(priority, now)
    names: list[str] = []
    while len(names) < len(_WEEKDAYS):
        if day.weekday() < len(_WEEKDAYS):
            if names and horizon is not None and day.date() > horizon.date():
                break
            names.append(_WEEKDAYS[day.weekday()])
        day += timedelta(days=1)
    names.sort(key=lambda name: name not in prefer)  # stable: nearest first within each group
    return [_DAY_READ.format(procedure=procedure, day=name) for name in names[:3]]


def _read_key(read: str) -> int | str:
    """The key of a world read (ours, sent or to send): its weekday, else its page (1 by default)."""

    found = fields_of(read)
    page = found.get("page", "1")
    return found["day"] if found.get("day") else int(page) if page.isdigit() else 1


def _asks(chat: list[Message], procedure: str) -> set[int | str]:
    """The keys (_read_key) of every world read of ``procedure`` we sent in ``chat``."""

    return {
        _read_key(row.text) for row in chat
        if row.recipient == LEDGER and first_word(row.text) == "world"
        and fields_of(row.text).get("procedure") == procedure
    }


def _page_day(world: str) -> str:
    """The one weekday every slot line of a world answer names, else ""."""

    days = {_slot(line).get("day") for line in world.splitlines() if "slot_id=" in line} - {None, ""}
    return days.pop() if len(days) == 1 else ""


def _world_key(world: str, asks: set[int | str]) -> int | str | None:
    """A world answer's key in _WORLD: the weekday of its slots when we asked that day, else its
    page. None for an empty answer while we asked days: which day it answers, the text does not say.
    ponytail: replies are matched to reads by content, not by id (the Ledger answers out of order)."""

    day = _page_day(world)
    if day and day in asks:
        return day
    if "slot_id=" not in world and any(isinstance(ask, str) for ask in asks):
        return None
    return protocol.page(world)[0]


def _fresh_world(procedure: str, key: int | str) -> str | None:
    hit = _WORLD.get((procedure, key))
    return None if hit is None or time.monotonic() - hit[0] > _WORLD_FRESH else hit[1]


def _warm(procedure: str, reads: list[str]) -> bool:
    """Every read has a fresh cached answer; for day reads a fresh page 1 will do (the earliest slots
    of every day: a cache filled by page reads, _recover, _ask_slots or a Ledger ignoring day=)."""

    keys = [_read_key(read) for read in reads]
    return all(_fresh_world(procedure, key) is not None for key in keys) or (
        isinstance(keys[0], str) and _fresh_world(procedure, 1) is not None
    )


def _cached_lines(procedure: str) -> str:
    """The slot rows of every fresh cached answer of ``procedure``, one per slot: the latest read.
    ponytail: a slot missing from a newer answer keeps its older row; the real world lists a taken
    slot with free=0 (tests/journey.py REAL_SLOT), so the latest row wins. The SDK format, which drops
    taken slots, would need the newer answer to clear the time range it covers."""

    rows: dict[str, str] = {}
    for (proc, _), (at, world) in sorted(_WORLD.items(), key=lambda item: item[1][0]):
        if proc == procedure and time.monotonic() - at <= _WORLD_FRESH:
            for line in world.splitlines():
                if slot := _slot(line).get("slot_id"):
                    rows[slot] = line
    return "\n".join(rows.values())


def _day_refused(rows: list[Message]) -> bool:
    """A Ledger refusal of a world read naming the day (``refused world code=SCHEMA_INVALID
    detail=day``, or the like)."""

    return any(
        row.sender == LEDGER and first_word(row.text) == "refused"
        and bool({"world", "op=world"} & words(row.text)) and re.search(r"\bday\b", row.text) is not None
        for row in rows
    )


def _stop_days() -> None:
    global _BY_DAY
    _BY_DAY = False


async def _wait_ledger(
    city: City,
    need_id: str | None,
    chat: list[Message],
    ready: Callable[[list[Message]], bool],
    until: float,
) -> list[Message]:
    """``chat`` plus the case's new Ledger rows, re-read every _LEDGER_PAUSE s until ``ready`` or
    ``until`` (time.monotonic()). Only the Ledger's rows: the whole case, 24 times a turn through
    the clogged Relay, was the cost (21/11)."""

    rows = chat
    while True:
        try:  # a slow read stops at ``until`` too: the cap holds, the ack still goes < 10 s
            ledger = await asyncio.wait_for(
                city.history(peer=LEDGER, need_id=need_id), timeout=max(0.05, until - time.monotonic())
            )
        except (CapError, TimeoutError):
            ledger = []
        seen = {row.msg_id for row in rows}
        rows = rows + [row for row in ledger if row.msg_id not in seen]
        if ready(rows) or time.monotonic() >= until:
            return rows
        await asyncio.sleep(max(0.0, min(_LEDGER_PAUSE, until - time.monotonic())))


def _spread(slots: list[dict[str, str]]) -> list[dict[str, str]]:
    """Hospital, cross-ASST: ``slots`` without those we hold, the ones we offered least in the
    last _OFFER_SPAN s first. A stable sort: time order, then _by_constraints, still decide."""

    now = time.monotonic()
    for slot_id in [key for key, at in _HELD.items() if now - at > _HOLD_SPAN]:
        del _HELD[slot_id]
    for slot_id in list(_OFFERED):
        _OFFERED[slot_id] = [at for at in _OFFERED[slot_id] if now - at < _OFFER_SPAN]
        if not _OFFERED[slot_id]:
            del _OFFERED[slot_id]
    return sorted(
        (slot for slot in slots if slot["slot_id"] not in _HELD),
        key=lambda slot: len(_OFFERED.get(slot["slot_id"], ())),
    )


def _unhold(message: Message, chat: list[Message], agent: str) -> None:
    """Hospital: a Ledger row ending our hold (_drops_hold) puts the slot back in cross-ASST
    offers. Not NO_CAPACITY (or the kit's SLOT_TAKEN): then another ASST holds it."""

    if not _drops_hold(message) or fields_of(message.text).get("code") in {"NO_CAPACITY", "SLOT_TAKEN"}:
        return
    holds = [fields_of(row.text).get("slot_id", "") for row in chat
             if row.sender == agent and row.recipient == LEDGER and first_word(row.text) == "hold"]
    _HELD.pop(fields_of(message.text).get("slot_id") or (holds[-1] if holds else ""), None)


_GENDERS = {"f": "f", "female": "f", "donna": "f", "femmina": "f", "woman": "f",
            "m": "m", "male": "m", "uomo": "m", "maschio": "m", "man": "m"}


def _by_constraints(slots: list[dict[str, str]], fields: dict[str, str]) -> list[dict[str, str]]:
    """``slots`` with those meeting most of the request's ``lang``/``gender``/``days`` first.
    A doctor missing from _DOCTORS meets no gender or language constraint: never claimed."""

    lang = (fields.get("lang") or "").strip().lower()
    raw_gender = fields.get("gender") or fields.get("gender_preference") or ""
    gender = _GENDERS.get(raw_gender.strip().lower(), "")
    days = {d for d in re.split(r"[\s,/;|]+", (fields.get("days") or "").lower()) if d}
    if not (lang or gender or days):
        return slots

    def met(slot: dict[str, str]) -> int:
        doctor = slot.get("doctor_id", "")
        spec = _DOCTORS.get(doctor.rsplit("-", 1)[-1])
        return (
            bool(lang and _speaks(doctor, lang))
            + bool(gender and spec is not None and spec[1] == gender)
            + bool(days and slot.get("day", "").lower() in days)
        )

    return sorted(slots, key=lambda slot: -met(slot))


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
    return (None if reply is None else protocol.when(reply["agenda_now"])), rows


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
    """The citizen's lang=, else the lang=en/it of a booking party's confirm or need.request (our
    booking centre or another ASST's), else their words."""

    rows = [row for row in chat if row.sender.startswith("citizen:")]
    for row in rows:
        lang = _fields(row).get("lang", "")
        if lang:
            return lang == "en"
    for row in chat:
        if row.sender.startswith("agent:") and (
            {"confirm", "need.request"} & words(row.text) or row.type in {"confirm", "need.request"}
        ):
            # ponytail: a partner's lang=sq names the doctor's language, not the chat's: only
            # en/it decide, anything else falls through to the citizen's words.
            lang = _fields(row).get("lang", "").lower()
            if lang in {"en", "it"}:
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


def _prose_code(text: str, chat: list[Message], agent: str) -> str:
    """A booking code written in prose, as _family reads it: one of our bookings named in the
    text, else "codice/code (è/is) X". The Ledger still checks it on checkin."""

    for booking in _bookings(chat, agent):
        if re.search(rf"(?<![\w-]){re.escape(booking)}(?![\w-])", text, re.I):
            return booking
    code = re.search(
        rf"\b(?:codice|code)(?: di prenotazione)?\s+(?:[èe]\s+|is\s+)?({_ID})", text, re.I
    )
    # ponytail: a code has a digit, so "my code is lost" is not a code.
    value = code[1].rstrip(".") if code else ""
    return value if any(c.isdigit() for c in value) else ""


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


def _cancelled(chat: list[Message], booking_id: str) -> bool:
    """The Ledger cancelled ``booking_id`` (a ``cancelled``/``cancel`` row naming it)."""

    ids = {f"booking_id={booking_id}", f"hold_id={booking_id}"}
    return any(
        row.sender == LEDGER
        and first_word(row.text) in {"cancelled", "cancel"}
        and bool(ids & set(row.text.split()))
        for row in chat
    )


def _booked(chat: list[Message], agent: str, slot: str) -> tuple[str, str] | None:
    """``(booking_id, ledger_ref)`` of a Ledger ``committed`` row of ours for ``slot`` that no
    later row cancelled, else None: the only receipt we may send again."""

    for booking_id, entry in _bookings(chat, agent).items():
        token = f"booking_id={booking_id}"
        if (
            entry["slot"] == slot
            and entry["ledger_ref"]
            and _latest(chat, "committed", token) is not None
            and not _cancelled(chat, booking_id)
        ):
            return booking_id, entry["ledger_ref"]
    return None


def _since_hold(chat: list[Message], agent: str, slot: str) -> list[Message] | None:
    """The rows after our latest ``hold`` on ``slot``, or None if we never held it."""

    start = None
    for i, row in enumerate(chat):
        if (
            row.sender == agent
            and row.recipient == LEDGER
            and first_word(row.text) == "hold"
            and f"slot_id={slot}" in row.text.split()
        ):
            start = i + 1
    return None if start is None else chat[start:]


def _drops_hold(row: Message) -> bool:
    """A Ledger row that ends a hold: a refused hold or commit, a cancellation, an expiry.
    ponytail: not matched to the hold's id (real refusals carry only op/code/detail); one slot
    in flight per case."""

    head = first_word(row.text)
    return row.sender == LEDGER and (
        head in {"cancelled", "cancel", "expired"}
        or head == "refused" and bool({"hold", "commit", "op=hold", "op=commit"} & words(row.text))
    )


def _slot_of(chat: list[Message], agent: str, row: Message) -> str:
    """The slot a refused/cancelled/expired Ledger row is about: its ``slot_id=``, its
    ``hold_id=``/``booking_id=`` through our rows, or ``detail=`` when that is a slot we held
    (live: ``refused op=hold code=NO_CAPACITY detail=<slot_id>``; EXPIRED says ``detail=slot``).
    Else the slot of our latest hold in the case."""

    held = [
        fields_of(r.text).get("slot_id", "")
        for r in chat
        if r.sender == agent and r.recipient == LEDGER and "hold" in words(r.text)
    ]
    fields = fields_of(row.text)
    ref = fields.get("hold_id") or fields.get("booking_id")
    slot = fields.get("slot_id") or (_hold_slot(chat, agent, ref) if ref else "")
    if not slot and fields.get("detail") in held:
        slot = fields["detail"]
    return slot or next((slot for slot in reversed(held) if slot), "")


def _released(chat: list[Message], agent: str, row: Message) -> bool:
    """A Ledger row answering our own ``cancel hold_id=`` (_release). ponytail: matched by its
    hold_id/booking_id only; a cancelled row naming neither is passed on as the Ledger's own."""

    fields = fields_of(row.text)
    ref = fields.get("hold_id") or fields.get("booking_id", "")
    return bool(ref) and protocol.we(chat, agent, f"hold_id={ref}", to=LEDGER, word="cancel")


async def _release(city: City, message: Message, chat: list[Message]) -> None:
    """A booking party calls off our slot for the citizen, naming ``offer_id=``, ``slot_id=`` or
    ``booking_id=``. Held or booked by us: ``cancel hold_id=`` on the Ledger once (no retry),
    then ``cancelled`` to the sender. Never ``cancel`` back: a booking centre reads that as the
    facility cancelling and offers again. A slot we do not hold: we say so."""

    agent, sender = city.agent_id, message.sender
    fields = _fields(message)
    offer_id = fields.get("offer_id", "")
    ref = fields.get("booking_id") or fields.get("hold_id")
    slot = fields.get("slot_id") or (_hold_slot(chat, agent, ref) if ref else "")
    for row in chat if offer_id and not slot else ():
        for line in row.text.splitlines():
            if f"offer_id={offer_id}" in line.split():
                slot = fields_of(line).get("slot_id", slot)
    held = _latest(chat, "held", f"slot_id={slot}") if slot else None
    hold_id = fields_of(held.text).get("hold_id", "") if held else ""
    token = f"hold_id={hold_id}"
    ids = " ".join(f"{k}={v}" for k, v in (("offer_id", offer_id), ("slot_id", slot)) if v)
    asked = bool(hold_id) and protocol.we(chat, agent, token, to=LEDGER, word="cancel")
    since = _since_hold(chat, agent, slot) if slot else None
    if not asked and (not hold_id or since is None or any(_drops_hold(row) for row in since)):
        if not _answered(chat, agent, message.msg_id, sender):
            await _say(
                city,
                sender,
                # Also a partner's "cancel reason=not_chosen" for an offer we never held (live).
                f"{ids} Preso nota. Non risulta un posto trattenuto o prenotato da noi per questa "
                "richiesta: non c'è nulla da annullare.".strip(),
            )
        return
    if not asked and not await _say(city, LEDGER, f"cancel {token}"):
        return

    def answer(rows: list[Message]) -> Message | None:
        """The Ledger's first row after our ``cancel``: cancelled, or its refusal."""

        start = next(
            (i for i, r in enumerate(rows) if r.sender == agent and r.recipient == LEDGER
             and first_word(r.text) == "cancel" and token in r.text.split()),
            None,
        )
        return None if start is None else next(
            (r for r in rows[start + 1:] if r.sender == LEDGER and (
                first_word(r.text) in {"cancelled", "cancel"}
                or first_word(r.text) == "refused" and bool({"cancel", "op=cancel"} & words(r.text))
            )),
            None,
        )

    fresh = await _until(city, message.need_id, chat, lambda rows: answer(rows) is not None)
    reply = answer(fresh)
    # Told already: the outcome once, the wait once (a later turn brings the outcome).
    told = [row.text for row in fresh if row.sender == agent and row.recipient == sender
            and token in row.text.split()]
    if any(first_word(text) == "cancelled" or "reason=" in text for text in told) or (
        reply is None and told
    ):
        return
    if reply is None:
        text = (f"{ids} {token} Ho chiesto al registro di liberare il posto: la conferma non è "
                "ancora arrivata.")
    elif first_word(reply.text) == "refused":
        code = fields_of(reply.text).get("code") or "refused"
        text = f"{ids} {token} reason={code} Il registro non ha annullato il posto."
    else:
        text = f"cancelled {ids} {token} Posto liberato sul registro, come richiesto."
    await _say(city, sender, text)


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
    booking_id = _fields(message).get("booking_id", "") or _prose_code(message.text, chat, agent)
    if not booking_id:
        ours = [
            booking
            for booking, entry in _bookings(chat, agent).items()
            if _holder(chat, agent, entry["slot"]) in ("", citizen)
        ]
        live = [booking for booking in ours if not _cancelled(chat, booking)]
        booking_id = live[0] if len(live) == 1 else ours[0] if len(ours) == 1 else ""
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
    if _cancelled(chat, booking_id):
        # The Ledger cancelled it: never a checkin for it. Say so, and name the booking of ours
        # that stands, if there is exactly one (a re-booking in the same case).
        if not _answered(chat, agent, message.msg_id, citizen):
            other = [
                booking for booking, entry in _bookings(chat, agent).items()
                if not _cancelled(chat, booking) and _holder(chat, agent, entry["slot"]) in ("", citizen)
            ]
            if en:
                note = (f"I'm sorry, booking {booking_id} has been cancelled, so I cannot register "
                        "your arrival with it. ")
                note += (f"Your booking {other[0]} is confirmed: send me that code and I will "
                         "register your arrival straight away." if len(other) == 1
                         else "The booking centre can find you a new date straight away.")
            else:
                note = (f"Mi dispiace, la prenotazione {booking_id} risulta annullata, quindi non "
                        "posso registrare l'arrivo con questo codice. ")
                note += (f"Risulta invece confermata la prenotazione {other[0]}: mi scriva quel "
                         "codice e registro subito il suo arrivo." if len(other) == 1
                         else "Il centro prenotazioni può trovarle subito una nuova data.")
            await _say(city, citizen, f"booking_id={booking_id} reason=cancelled {note}")
        return
    token = f"booking_id={booking_id}"
    state = _checkin_state(chat, agent, token)
    if state == "done":
        line = _latest(chat, "checked_in", token)
        if line is not None and not protocol.we(chat, agent, token, to=citizen, word="checkin"):
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
        # No word before the Ledger answers: the push turn of its checked_in (or refusal) does
        # (_hospital). The check-in ends a VIS-FIS journey: an interim reply closed it and the judge
        # ruled before the Ledger's checked_in, 1.5-2 real minutes after our checkin (live 1.0.17-
        # 1.0.18: 22 of 22 interims; in 75 min 8 receipt_refused -2 against 7 checkin_honored +2).
        _LOG.info("care.checkin.missing", extra={"need_id": (message.need_id or "")[:48]})
        return
    reply, fresh = found
    if first_word(reply.text) == "checked_in":
        await _on_checked_in(city, reply, fresh, reports=False, en=en)
        return
    await _checkin_refused(city, reply, fresh, booking_id=booking_id, en=en)


async def _checkin_refused(
    city: City, message: Message, chat: list[Message], *, booking_id: str, en: bool
) -> None:
    # The arrival turn and the refusal's own turn may both get here: one answer only, on the case
    # re-read inside the lock (specialist and consultorio push turns hold no other lock).
    async with _gated(f"refused:{message.need_id or message.msg_id}"):
        try:
            chat = await city.history(need_id=message.need_id)
        except CapError:
            pass
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
    citizen = protocol.citizen(chat)
    if not booking_id or not citizen or _answered(chat, agent, message.msg_id, citizen):
        return
    code = fields.get("code") or "refused"
    slot = _bookings(chat, agent).get(booking_id, {}).get("slot") or _hold_slot(
        chat, agent, booking_id
    )
    at = protocol.when(_slot_facts(chat, agent, slot).get("at", ""))
    # The live window refusal is ``code=POLICY_VIOLATION detail=outside`` (tests/fixtures/refused.txt).
    if _WINDOW_CODE.search(code) or fields.get("detail") == "outside":
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
        # A booking the Ledger cancelled is never told as confirmed.
        if _holder(chat, agent, entry["slot"]) in ("", citizen) and not _cancelled(chat, booking)
    }
    if len(ours) != 1:
        await _redirect(city, message, chat, en=_english(chat, message.text))
        return
    if _answered(chat, agent, message.msg_id, citizen):
        return
    (booking_id, entry), = ours.items()
    en = _english(chat, message.text)
    at = protocol.when(_slot_facts(chat, agent, entry["slot"]).get("at", ""))
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


async def _welcome(
    city: City,
    message: Message,
    *,
    place_it: str = "l'ospedale dove si svolgerà la sua visita",
    place_en: str = "the hospital where your visit will take place",
) -> None:
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
            citizen = _holder(chat, agent, entry["slot"]) or protocol.citizen(chat) or ""
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
                or protocol.we(chat, agent, token, to=citizen, word="booking.receipt")
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
            at = protocol.when(facts.get("at", ""))
            en = _english(chat)
            # Collaboration (review): our GP hears it first, unasked, and the citizen reads it.
            gp = await _tell_gp(city, chat, booking_id, entry["slot"], facts, at)
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
                # Review (proactivity +1): the exchange with the booking centre and the work
                # already done, then concrete next steps. Only facts of the case: the receipt
                # went to the booking party after the Ledger's committed.
                note = (
                    f"Hello, this is {place_en}: your "
                    f"booking is confirmed and registered{when}. Your booking centre passed me "
                    "your request: I have already registered the booking on the Ledger and sent "
                    f"it the receipt. Your booking code is "
                    f"{booking_id}. On the day, as soon as you arrive ({window}), write to me "
                    f"that you are here with the code {booking_id}: I will register your arrival "
                    "and you need do nothing else. "
                    + ("On my own initiative I have also let your GP know about this booking, so "
                       "your doctor is up to date. " if gp else "")
                    + "If the time no longer suits you, tell your "
                    "booking centre and it can move the visit. If you feel worse in the meantime, "
                    "contact your GP; in an emergency call 112. For any question, just write to "
                    "me here."
                )
            else:
                when = f" per {_in_words(at, False)}" if at else ""
                window = (
                    f"nella fascia {_half_day(at)} di quel giorno"
                    if at
                    else "nella mezza giornata della visita: 08:00-13:00 o 13:00-18:00"
                )
                note = (
                    f"Buongiorno, sono {place_it}: la prenotazione "
                    f"è confermata e registrata{when}. Il suo centro prenotazioni mi ha passato "
                    "la sua richiesta: ho già registrato la prenotazione sul Ledger e gli ho "
                    f"mandato la ricevuta. Il suo codice di prenotazione è "
                    f"{booking_id}. Il giorno della visita, appena arriva ({window}), mi scriva "
                    f"che è qui con il codice {booking_id}: registro io il suo arrivo e non deve "
                    "fare altro. "
                    + ("Di mia iniziativa ho anche avvisato il suo medico di base di questa "
                       "prenotazione, così è aggiornato. " if gp else "")
                    + "Se l'orario non le va più bene, lo dica al centro prenotazioni: "
                    "può spostare la visita. Se nel frattempo si sente peggio, si rivolga al suo "
                    "medico di base; in emergenza chiami il 112. Per qualsiasi domanda mi scriva "
                    "pure qui, sono a sua disposizione."
                )
            await _say(city, citizen, " ".join(parts) + " " + note)


async def _tell_gp(
    city: City,
    chat: list[Message],
    booking_id: str,
    slot: str,
    facts: dict[str, str],
    at: datetime | None,
) -> bool:
    """Unrequested, once, right before the welcome: our own GP learns the visit is booked.
    True when the GP has it (now or earlier), so the welcome can say so (review: collaboration
    is judged on what the citizen reads).
    Only for our booking centre's bookings: another ASST's citizen is not our GP's patient
    (over-disclosure, R:243). Best effort: only if the Index lists our GP, no retry, never raises."""

    sender, _, number = _booking_of(chat, city.agent_id, slot) if slot else ("", "", None)
    if sender != _OUR_BOOKING or not number:
        return False
    if protocol.we(chat, city.agent_id, f"booking_id={booking_id}", to=_OUR_GP, word="data.response"):
        return True
    try:
        if not any(peer.agent_id == _OUR_GP for peer in await city.find("gp")):
            return False
    except Exception:  # CapError or network: the welcome goes without it
        return False
    parts = ["data.response", "purpose=booking", f"booking_id={booking_id}", f"prescription_number={number}"]
    if facts.get("procedure"):
        parts.append(f"procedure={facts['procedure']}")
    if slot:
        parts.append(f"slot_id={slot}")
    if at:
        parts.append(f"agenda_date={facts['at']}")
    when = f" per {_in_words(at, False)}" if at else ""
    return await _say(
        city,
        _OUR_GP,
        " ".join(parts)
        + f" Buongiorno collega, la informo che il suo paziente con questa ricetta è prenotato "
        f"da noi{when}: la prenotazione {booking_id} è confermata sul Ledger. Gli invio ora "
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
            consent = consent or fields.get("consent_ref", "")
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


async def _family_wait(city: City, chat: list[Message]) -> None:
    citizen = protocol.citizen(chat)
    if citizen:
        en = _family_preferences(chat, citizen).get("lang") == "en"
        text = ("Your booking is still being verified. It is not confirmed yet."
                if en else "La prenotazione è ancora in verifica. Non è ancora confermata.")
        if not protocol.exact(chat, city.agent_id, text):
            await _say(city, citizen, text)


async def _finish_booking(
    city: City,
    message: Message,
    chat: list[Message],
    slot: str,
    *,
    hold_self: bool,
    new: bool = False,
) -> None:
    """Hold is in the mailbox. Read the held line and commit before this turn ends.
    ``new``: a hold just sent, so a ``held`` already in ``chat`` belongs to an older one.
    Only until _BOOK_BY (hospital; a partner's turn at the other two); a line not read by then is
    the held or committed push's job
    (_on_held commits a hold we never committed, _on_committed sends a receipt not yet sent).
    ponytail: no sweeper. A lost held push leaves the hold uncommitted, a lost committed push
    leaves the receipt unsent, until the next confirm of the case reads the line from history
    (or, after an ``expired`` row, holds again: _since_hold, _drops_hold)."""

    by = _BOOK_BY.get()
    found = await _await_line(
        city, message.need_id, chat, "held", f"slot_id={slot}", op="hold", new=new, deadline=by
    )
    if found is None:
        _LOG.info("care.held.missing", extra={"need_id": (message.need_id or "")[:48]})
        if hold_self:
            await _family_wait(city, chat)
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
        city, message.need_id, fresh, "committed", f"booking_id={hold_id}", op="commit", deadline=by
    )
    if done is None:
        _LOG.info("care.commit.missing", extra={"need_id": (message.need_id or "")[:48]})
        if hold_self:
            await _family_wait(city, fresh)
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
    target = sender or protocol.citizen(chat)
    if not target:
        return
    token = f"booking_id={booking_id}"
    if protocol.we(chat, city.agent_id, token, to=target, word="booking.receipt"):
        return
    text = f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref}"
    if slot:  # from the Ledger's committed row or our hold: never guessed
        text += f" slot_id={slot}"
    if target.startswith("citizen:"):
        if direct:
            en = _family_preferences(chat, target).get("lang") == "en"
            offer = next((o for o in _our_offers(chat, city.agent_id)
                          if o.get("slot_id") == slot), {})
            at = protocol.when(offer.get("agenda_date", ""))
            when = _in_words(at, en) if at else ""
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
    city: City, message: Message, chat: list[Message], *, hold_self: bool, slot: str = "",
    code: str = "",
) -> None:
    """A Ledger refusal (or expiry, cancellation) of our hold or commit; ``slot`` and ``code``
    given: a confirm we refuse ourselves before the hold (_starts_soon), ``message`` the confirm."""

    agent = city.agent_id
    slot = slot or _slot_of(chat, agent, message)
    if not slot:
        return
    target = _booking_of(chat, agent, slot)[0] or ("" if message.sender == LEDGER else message.sender)
    offer_id = ""
    for row in chat:
        for line in row.text.splitlines():
            if f"slot_id={slot}" in line.split():
                offer_id = fields_of(line).get("offer_id", offer_id)
    fields = fields_of(message.text)
    # The Ledger's code goes on, so the booking centre can react: PRESCRIPTION_EXPIRED →
    # renewal (P1), CONSENT_MISSING → consent (P2), NO_CAPACITY → another slot (P3).
    code = code or fields.get("code") or fields.get("reason") or "facility_cancelled"
    hospital = _HOSPITAL.get()
    if code == "DUPLICATE":
        # A second commit of a hold already committed (its committed line brings the receipt)
        # or a prescription already booked: nothing was lost, so no cancel to anyone (every
        # facility: the booking centre re-offered after a booking, wave W01QQQ 1g).
        return
    if hospital and code == "NO_CAPACITY":
        # The slot is gone: neither a cross-ASST answer nor _alternatives offers it again.
        # Only VIS-FIS pages: the specialist's VIS-CARD pages stay its own.
        for key, (at, world) in list(_WORLD.items()):
            if key[0] == _PROCEDURE:
                kept = [line for line in world.splitlines() if f"slot_id={slot}" not in line.split()]
                _WORLD[key] = (at, "\n".join(kept))
    if hospital and first_word(message.text) == "refused" and {"hold", "op=hold"} & words(
        message.text
    ) and (_booked(chat, agent, slot) or _held_now(chat, slot)):
        # A late refusal of a duplicate hold (a second pod, or a re-hold while the Ledger took
        # 20-33 s): our hold or booking of the slot stands, so no cancel to anyone.
        return
    # Hospital: once per hold, so a re-hold of the same offer can be cancelled again.
    since = _since_hold(chat, agent, slot) if hospital else None
    seen = chat if since is None else since
    if (
        target
        and offer_id
        and not protocol.we(seen, agent, f"offer_id={offer_id}", to=target, word="cancel")
    ):
        # A booking party matches the cancel on the slot too (partner CUPs); a citizen needs only the offer.
        where = f" slot_id={slot}" if target.startswith("agent:") else ""
        reason = _mach_reason(code, message) if _peer(target) else code  # MACH-2.1 R8: taken, past
        text = f"cancel offer_id={offer_id}{where} reason={reason}" + _tail(
            target, _offer_rid(chat, agent, target, slot) or fields.get("request_id", ""))
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
        # Proactive: a lost slot (taken, or expired) comes with other free ones. Only to another
        # ASST's booking centre: ours drops a facility's offer lines (its _desk has no branch for
        # them) and searches again by itself on the cancel; a citizen chooses from the first
        # offers message only (R:95). A message of their own, never after the cancel line:
        # protocol.plain would capitalise their act (Offer), and MACH-2.1 parsers read ln.split()[0].
        if hospital and code in {"NO_CAPACITY", "EXPIRED"} and target.startswith("agent:") and (
            target != _OUR_BOOKING
        ):
            await _alternatives(city, chat, target, slot)
    citizen = _holder(chat, agent, slot) or protocol.citizen(chat) or ""
    if (
        offer_id
        and citizen.startswith("citizen:")
        and citizen != target
        # Only a citizen we already wrote to (the welcome): the booking party tells the rest.
        and any(row.sender == agent and row.recipient == citizen for row in chat)
        and not protocol.we(seen, agent, f"offer_id={offer_id}", to=citizen, word="cancel")
    ):
        note = (
            "I'm sorry, your booking with us has been cancelled. The booking centre can find "
            "you a new date."
            if _english(chat)
            else "Mi dispiace, la sua prenotazione da noi è stata annullata. Il centro "
            "prenotazioni può trovarle una nuova data."
        )
        await _say(city, citizen, f"cancel offer_id={offer_id} reason={code} {note}")
    if hold_self:
        await _advance(city, message, chat, replacing=True)


def _held_now(chat: list[Message], slot: str) -> bool:
    """A Ledger ``held`` for ``slot`` that no later cancellation, expiry or refused commit ended.
    A refused hold ends nothing: it refuses only the new hold. ponytail: drops are not matched
    to the hold's id (as _drops_hold)."""

    alive = False
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == "held":
            alive = alive or f"slot_id={slot}" in row.text.split()
        elif _drops_hold(row) and not ({"hold", "op=hold"} & words(row.text)):
            alive = False
    return alive


async def _alternatives(city: City, chat: list[Message], partner: str, slot: str) -> None:
    """Hospital, right after the cancel of a lost slot to another ASST's booking centre: up to 3
    free slots of ours it was not offered yet, from the cached world pages (_WORLD, fresh only),
    as offer lines with its request_id: the format of our cross-ASST answer. Nothing without
    that request_id (the partner matches answers on it) or without a fresh page. Only Ledger
    attributes, never citizen data."""

    agent = city.agent_id
    offers = _our_offers([row for row in chat if row.recipient == partner], agent)
    mine = next((offer for offer in offers if offer.get("slot_id") == slot), {})
    rid = mine.get("request_id", "")
    if not rid:
        return
    procedure = mine.get("procedure") or _PROCEDURE
    priority = mine.get("priority", "").upper()
    priority = priority if priority in {"U", "B", "D", "P"} else None
    skip = {slot} | {offer.get("slot_id", "") for offer in offers}
    # Every fresh cached answer, pages and weekdays, one row per slot (_cached_lines).
    world = _cached_lines(procedure)
    now = _city_now() or _day_after(world)
    chosen = [] if now is None else [
        found
        for found in fitting(world.splitlines(), now=now, priority=priority, lang="it", procedure=procedure)
        if found["slot_id"] not in skip
    ]
    if not chosen:
        return
    await _say(city, partner, "\n".join([
        *(protocol.offer_line(found, None, priority) + _tail(partner, rid) for found in chosen[:3]),
        "Ci dispiace, il posto scelto non è più disponibile. Per la stessa richiesta abbiamo "
        "ancora queste disponibilità del nostro ospedale. Per prenotarne una inviateci il confirm "
        "con offer_id, slot_id, citizen, prescription_number e consent_ref, più "
        "cross_asst_consent_ref se la regola lo richiede: la registriamo sul Ledger e vi mandiamo "
        "subito la ricevuta.",
    ]))


async def _on_checkin(city: City, message: Message, chat: list[Message], *, reports: bool) -> None:
    booking_id = _fields(message).get("booking_id", "")
    citizen = message.sender if message.sender.startswith("citizen:") else protocol.citizen(chat)
    if not booking_id or not citizen:
        return
    token = f"booking_id={booking_id}"
    if not protocol.we(chat, city.agent_id, token, to=LEDGER, word="checkin"):
        if not await _say(city, LEDGER, f"checkin booking_id={booking_id} citizen={citizen}"):
            return
    found = await _await_line(city, message.need_id, chat, "checked_in", token, op="checkin")
    if found is None:
        _LOG.info("care.checkin.missing", extra={"need_id": (message.need_id or "")[:48]})
        # No interim reply (as the hospital's): it would end the journey before checked_in.
        return
    reply, fresh = found
    if first_word(reply.text) == "checked_in":
        await _on_checked_in(city, reply, fresh, reports=reports)
        return
    # Refused by the Ledger: the hospital's answer (the half day of the visit, or check the code).
    await _checkin_refused(city, reply, fresh, booking_id=booking_id, en=_en(chat))


async def _on_checked_in(
    city: City, message: Message, chat: list[Message], *, reports: bool, en: bool | None = None
) -> None:
    booking_id = fields_of(message.text).get("booking_id", "")
    citizen = protocol.citizen(chat)
    en = _en(chat) if en is None else en
    if not booking_id:
        return
    token = f"booking_id={booking_id}"
    filed = None
    if reports and not _reported(chat, city.agent_id, booking_id):
        # The report on the Ledger before the check-in note: the note ends the journey and the judge
        # rules about a minute later on the Ledger so far (ADR-119), so a report written after it
        # scored report_missing. Unshared, without waiting: live citizens never answered a
        # report_sharing ask (wave 1, judge report_missing every time) and R:210 allows it.
        filed = await _file_report(city, message, chat, booking_id, citizen or "")
    if citizen and not protocol.we(chat, city.agent_id, token, to=citizen, word="checkin"):
        if reports:
            note = _specialist_arrival(chat, city.agent_id, booking_id, citizen, en)
        else:
            # Review (proactivity +1): the Ledger's checked_in is the work already done; then
            # where to turn after the visit, never a clinical indication.
            note = (
                "Thank you, I have registered your arrival on the Ledger: there is nothing else "
                "you need to do. If after the visit you have a question or need another "
                "appointment, write to me here and I will tell you who to contact. I wish you a "
                "good visit."
                if en
                else "Grazie, ho registrato il suo arrivo sul Ledger: non deve fare altro. Se dopo "
                "la visita ha una domanda o le serve un altro appuntamento, mi scriva pure qui e "
                "le dico io a chi rivolgersi. Le auguro una buona visita."
            )
        await _say(city, citizen, f"checkin booking_id={booking_id} {note}")
    if filed is not None:
        await _await_report(city, message, filed, booking_id)


# A worry the citizen voices about the visit: acknowledged in plain words, never answered with
# clinical content (no_clinical_advice).
_WORRIED = re.compile(
    r"\b(?:ansia|ansios[oa]|preoccupat[oa]|preoccupazion\w*|paura|agitat[oa]|nervos[oa]|"
    r"spaventat[oa]|teso|tesa|worried|anxious|nervous|scared|afraid)\b",
    re.IGNORECASE,
)


def _specialist_arrival(
    chat: list[Message], agent: str, booking_id: str, citizen: str, en: bool
) -> str:
    """The specialist's check-in confirmation (review: acknowledge the person, be specific,
    a clear next step): the cardiology visit and its time from the Ledger, a voiced worry
    acknowledged, what happens next. Only facts we or the Ledger wrote."""

    entry = _bookings(chat, agent).get(booking_id, {})
    slot = entry.get("slot") or _hold_slot(chat, agent, booking_id)
    at = protocol.when(_slot_facts(chat, agent, slot).get("at", "")) if slot else None
    said = " ".join(row.text for row in chat if row.sender == citizen)
    worried = bool(_WORRIED.search(said))
    if en:
        when = f" on {_in_words(at, True)}" if at else ""
        care = (
            " I understand you may feel worried: you are in good hands now and the specialist "
            "will see you shortly."
            if worried
            else ""
        )
        return (
            f"Thank you, your arrival for the cardiology visit{when} is registered.{care} Right "
            "after the visit I file the visit summary and write to you here, so you always "
            "know where things stand."
        )
    when = f" di {_in_words(at, False)}" if at else ""
    care = (
        " Capisco la sua preoccupazione: ora è in buone mani e lo specialista la riceverà a "
        "breve."
        if worried
        else ""
    )
    return (
        f"Grazie, il suo arrivo per la visita di cardiologia{when} è registrato.{care} Subito "
        "dopo la visita archivio il referto e le scrivo qui, così sa sempre a che punto è."
    )


def _reported(chat: list[Message], agent: str, booking_id: str) -> bool:
    """We already wrote the report for ``booking_id`` to the Ledger."""

    return protocol.we(chat, agent, f"booking_id={booking_id}", to=LEDGER, word="report")


# Level 3 ("Bisogni in linguaggio naturale"): citizens write prose, no key=value tokens.
_ARRIVED = re.compile(
    r"\b(?:sono\s+arrivat[oa]|siamo\s+arrivati|sono\s+qui|siamo\s+qui|allo\s+sportello|"
    r"in\s+accettazione|check[\s-]?in|i'?m\s+here|i\s+am\s+here|i\s+have\s+arrived|i\s+arrived|"
    r"at\s+the\s+desk)\b",
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
    ``""``. Only before the check-in: a later line is small talk."""

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
    if not protocol.we(chat, city.agent_id, token, to=LEDGER, word="prescribe"):
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
) -> list[Message] | None:
    """Ledger first: the follow-up prescription when the case asks for one, then the report.
    The case as read, or None if the report did not go out. The citizen hears it after
    ``reported`` (_await_report, _on_reported). The caller sends it once per booking (_reported)."""

    token = f"booking_id={booking_id}"
    number = ""
    procedure = _follow_up(chat)
    if procedure and citizen:
        number, chat = await _prescribe_follow_up(
            city, message.need_id, chat, booking_id, citizen, procedure
        )
    parts = ["report", token, f"outcome_code={_OUTCOME}"]
    if number:
        parts.append(f"follow_up_prescription_number={number}")
    return chat if await _say(city, LEDGER, " ".join(parts)) else None


async def _await_report(
    city: City, message: Message, chat: list[Message], booking_id: str
) -> None:
    token = f"booking_id={booking_id}"
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
    citizen = protocol.citizen(chat)
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
    if not protocol.we(chat, agent, token, to=citizen, word="report"):
        outcome = fields.get("outcome_code") or ours.get("outcome_code") or _OUTCOME
        shared = fields.get("consent_ref") or ours.get("consent_ref")
        if _en(chat):
            note = (
                "Your visit summary is filed and shared as you agreed."
                if shared
                else "Your visit summary is filed and stays private."
            )
            note += (
                " If the specialist recommends a control visit, the booking centre will help you "
                "book it; for any question just write to me here. Thank you for coming, and take "
                "care."
            )
        else:
            note = (
                "Il referto della sua visita è archiviato e lo condividiamo come da suo consenso."
                if shared
                else "Il referto della sua visita è archiviato e resta riservato."
            )
            note += (
                " Se lo specialista le indica un controllo, il centro prenotazioni la aiuta a "
                "fissarlo; per qualsiasi domanda mi scriva pure qui. Grazie per la fiducia e "
                "buona giornata."
            )
        await _say(city, citizen, f"report {token} outcome_code={outcome} {note}")
    number = ours.get("follow_up_prescription_number", "")
    rx = f"prescription_number={number}"
    if not number or protocol.we(chat, agent, rx, to=citizen, word="prescription"):
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


# --- MACH-2.1 (docs/protocol/mach-2.1.md): the ring, the lines other teams read, the lead -------

_MACH = "mach=2.1"
# R8, at the confirm, for another team ("chi manda usa taken"); our CUP keeps the Ledger's codes
# (its calendar marks the slot gone on SLOT_TAKEN/EXPIRED, booking_centre _partner_cancel).
_MACH_REASON = {"SLOT_TAKEN": "taken", "EXPIRED": "past"}
# R14: a requester's lease_s= is at most this; read only for the lead (R5), we keep no leases.
_LEASE_MAX = 1800


def _outside_ring(message: Message) -> bool:
    """R17: a need.request or confirm from an agent outside the ring gets nothing (no answer,
    slot or hold), only a ``ring.alert`` log line. True: the turn ends here."""

    sender, kind = message.sender, words(message.text) | {message.type}
    if not sender.startswith("agent:") or not {"need.request", "confirm"} & kind or in_ring(sender):
        return False
    _LOG.warning("ring.alert suspect=%s reason=not_listed need=%s", sender, message.need_id)  # as booking
    return True


def _peer(to: str) -> bool:
    """Another team's agent: it reads MACH-2.1 lines. Not our CUP (it reads the Ledger's codes)."""

    return to.startswith("agent:") and to != _OUR_BOOKING


def _tail(to: str, rid: str = "") -> str:
    """The tokens closing an offer or cancel line to an agent: the request's ``request_id=`` when
    known, ``mach=2.1`` for another team. "" to a citizen."""

    if not to.startswith("agent:"):
        return ""
    return (f" request_id={rid}" if rid else "") + (f" {_MACH}" if _peer(to) else "")


def _mach_reason(code: str, message: Message) -> str:
    """R8 at the confirm: SLOT_TAKEN -> taken, EXPIRED -> past. At the confirm means our own check
    before the hold (``message`` is the confirm) or the Ledger refusing the hold, not a hold that
    lapsed before its commit."""

    at_confirm = message.sender != LEDGER or bool({"hold", "op=hold"} & words(message.text))
    return _MACH_REASON.get(code, code) if at_confirm else code


def _offer_rid(chat: list[Message], agent: str, to: str, slot: str) -> str:
    """The request_id of our offer of ``slot`` to ``to``, or ""."""

    offers = _our_offers([row for row in chat if row.recipient == to], agent)
    return next((o.get("request_id", "") for o in offers if o.get("slot_id") == slot), "")


def _lead(
    lines: list[str],
    fields: dict[str, str],
    now: datetime,
    after: datetime | None,
    priority: str | None,
) -> list[str]:
    """R5: without ``after=``, a partner request's slot lines from now + its ``lease_s=`` in agenda
    minutes (1 real s = 1 agenda min; at most _LEASE_MAX). fitting still keeps _LEAD, so the lead
    is the larger of the two; U keeps its _HOLD_LEAD fallback, untouched."""

    asked = fields.get("lease_s", "")
    if after is not None or priority == "U" or not asked.isdigit():
        return lines
    lead = now + timedelta(minutes=min(int(asked), _LEASE_MAX))
    return [line for line in lines if (at := protocol.when(_slot(line).get("at", ""))) is not None and at >= lead]
