"""Booking centre brain (centro prenotazioni).

Baseline taken from the kit's care-protocol example
(``mach-hackathon-kit/templates/examples/agent.py``), reduced to the booking-centre path:

1. A citizen arrives with ``prescription_number=...``: ask the Ledger ``prescription`` and
   ``agenda``, then ``world procedure=... page=N``.
2. Offer up to three fitting slots in one turn (``offer offer_id=... slot_id=...``), or the
   waiting list for priority P.
3. On the citizen's ``confirm``: forward ``confirm`` to the facility that owns the slot.
   ``hold`` and ``commit`` are the facility's job, not ours (our grant cannot commit).
4. On the facility's ``booking.receipt``: forward it to the citizen. We never close.
5. On the facility's ``cancel``: tell the citizen and offer what is still free.

Every Ledger reply arrives as a message from ``city:ledger``; we also re-read the case
history because a Ledger push may not open a turn of its own.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta

from city_sdk.city import LEDGER, CapError, City, Message, Need, OutOfScope, clinical, understood

_MAX_PAGE = 8
_MAX_OFFERS = 3
# The agenda runs 60x: one agenda hour is a real minute, so a slot is still ahead when the
# citizen decides (the citizen confirms within seconds of the offer).
_LEAD = timedelta(hours=1)
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
# Priority -> agenda days to the deadline. U is the same agenda day, P is the waiting list.
_DEADLINE_DAYS = {"B": 2, "D": 5}
_PULL_TRIES = 24
_PULL_PAUSE = 0.25
_SENT_NEEDS = 4096
_GP_RX = re.compile(r"(?<![\w-])prescription_number-([0-9a-f]{24})(?![\w-])", re.IGNORECASE)
# A facility whose mailbox refused our confirm (TARGET_NOT_VISIBLE, partner_down) is left out
# of new offers for a while; the city does not say when it is back.
# ponytail: fixed pause per process, a probe or a city signal if outages get shorter.
_DOWN_SECONDS = 600.0
_DOWN: dict[str, float] = {}
# Rules in force from the latest ``rules`` answer of any case: (rule ids, effective from).
# None until the Ledger has answered once in this process.
_RULES: list[tuple[frozenset[str], datetime | None]] | None = None

ESCALATION = "escalate.emergency reason=red_flag Contatti subito il 112."
# One short courteous sentence after the act and its tokens, in the case's language.
_PHRASES: dict[str, dict[str, str]] = {
    "it": {
        "number": (
            "Volentieri, la aiuto io a prenotare: mi serve solo il numero della ricetta del medico "
            "di base. Me lo può indicare?"
        ),
        "unknown_rx": (
            "Mi scusi, non trovo una ricetta con questo numero: può ricontrollarlo? Appena ce l'ho, "
            "prenotiamo."
        ),
        "renewal": (
            "Mi dispiace, la ricetta non è più valida: il suo medico di base può rinnovarla con lo "
            "stesso numero. Appena mi manda il numero nuovo, prenotiamo insieme."
        ),
        "offers": "Ecco i posti disponibili che le propongo: mi dica pure quale preferisce e procedo io.",
        "relaxed": (
            "Mi dispiace, entro la scadenza della priorità non ci sono posti liberi: queste sono le "
            "prime alternative disponibili. Mi dica pure quale preferisce."
        ),
        "waiting": "Mi dispiace, al momento non ci sono posti disponibili: le propongo la lista d'attesa.",
        "waiting_p": "Per la priorità P la prenotazione passa dalla lista d'attesa: gliela propongo qui.",
        "which": "Mi scusi, non trovo questa scelta tra le offerte che le ho mandato: quale offerta sceglie?",
        "consent": "Perfetto. Per completare la prenotazione mi serve il suo consenso: può confermarlo?",
        "no_consent": (
            "Capisco. Senza il consenso non posso completare questa prenotazione: se cambia idea, "
            "sono qui."
        ),
        "down": (
            "Mi scusi, la struttura non è raggiungibile in questo momento: annullo questa offerta e "
            "cerco subito un'alternativa."
        ),
        "cancelled": "Mi dispiace, la struttura ha annullato questo posto: cerco subito un'alternativa per lei.",
        "expired": "Mi dispiace, la struttura segnala che la ricetta è scaduta.",
        "receipt": "La prenotazione è confermata: conservi questa ricevuta per il check-in in struttura.",
        "prefs": (
            "Per proporle i posti più comodi: ha giorni preferiti (lunedì-venerdì) o preferisce un "
            "medico donna o uomo? Se non ha preferenze, me lo dica pure e procedo."
        ),
        "hello": (
            "Buongiorno, sono il centro prenotazioni: il suo medico di base mi ha inoltrato la "
            "ricetta, così posso cercarle subito un posto."
        ),
        "handoff": "Grazie, prendo in carico io la prenotazione e contatto subito il paziente.",
        "redirect": (
            "Su questo non posso darle un'informazione sicura. Può rivolgersi a: medico di base "
            "(sintomi, ricette e rinnovi); ospedale (visite di fisiatria e check-in); specialista "
            "(visite di cardiologia e referti); consultorio (ginecologia, accesso diretto senza "
            "ricetta)."
        ),
    },
    "en": {
        "number": (
            "Happy to help you book: I just need the prescription number from your GP. Could you "
            "share it?"
        ),
        "unknown_rx": (
            "Sorry, I cannot find a prescription with this number: could you check it? As soon as "
            "I have it, we book."
        ),
        "renewal": (
            "I am sorry, the prescription is no longer valid: your GP can renew it with the same "
            "number. As soon as you send me the new number, we book together."
        ),
        "offers": "Here are the available slots I can offer you: just tell me which one you prefer and I will take care of it.",
        "relaxed": (
            "I am sorry, there are no slots within the deadline of your priority: these are the "
            "first alternatives available. Just tell me which one you prefer."
        ),
        "waiting": "I am sorry, there are no free slots right now: I can put you on the waiting list.",
        "waiting_p": "For priority P the booking goes through the waiting list: I am offering it to you here.",
        "which": "Sorry, I cannot match this choice with the slots I sent you: which one do you choose?",
        "consent": "Great. To complete the booking I need your agreement: could you give it?",
        "no_consent": "I understand. Without your agreement I cannot complete this booking: if you change your mind, I am here.",
        "down": (
            "Sorry, the facility cannot be reached right now, so this slot is withdrawn: I am "
            "looking for an alternative straight away."
        ),
        "cancelled": "I am sorry, the facility withdrew this slot: I am looking for an alternative for you.",
        "expired": "I am sorry, the facility reports that the prescription has expired.",
        "receipt": "Your booking is confirmed: please keep this receipt for check-in at the facility.",
        "prefs": (
            "To offer you the most convenient slots: do you prefer particular days (Monday-Friday) "
            "or a female or male doctor? If you have no preference, just tell me and I will proceed."
        ),
        "hello": (
            "Good morning, this is the booking centre: your GP forwarded me your prescription, so I "
            "can look for a slot for you right away."
        ),
        "handoff": "Thank you, I am taking over the booking and contacting the patient right away.",
        "redirect": (
            "I cannot give you reliable information on this. You can ask: your GP (symptoms, "
            "prescriptions and renewals); the hospital (physiatry visits and check-in); the "
            "specialist (cardiology visits and reports); the family counselling centre "
            "(gynaecology, direct access without a prescription)."
        ),
    },
}

# Red flags in the citizen's own words (it/en, any tone), on text without accents. The city's
# protocol lists red flags as codes (RF-...), which a citizen does not write: words it is.
# Same table as gp/agent/agent.py: change both.
_PAIN = r"(?:dolor\w*|mal(?:e|issimo)?\b|fitt\w*|strett\w*|oppress\w*|bruci\w*|pain\w*|hurt\w*|ach\w*|tight\w*|pressure|squeez\w*)"
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
            r"(?:can'?t|cannot|hard to|trouble|difficult\w*|struggl\w*)\W+(?:\w+\W+){0,2}?breath\w*",
            r"svenu\w*|svenim\w*|sven(?:go|gono|ire)\b|(?:pers\w*|perdo)\W+(?:i sensi|conoscenza|coscienza)",
            r"faint(?:ed|ing)\b|(?:feel\w*|felt) faint|pass(?:ed|ing)? out|lost consciousness|black(?:ed)? out",
            r"(?:perdita di forza|perdo (?:la )?forza|debolezza|non sento)\W+(?:\w+\W+){0,3}?gamb\w*",
            r"gamb\w*\W+(?:\w+\W+){0,2}?(?:cedono|non (?:mi )?(?:reggono|rispondono)|paralizzat\w*)",
            r"(?:loss of strength|losing strength|weakness|can'?t feel)\W+(?:\w+\W+){0,3}?legs?\b",
            r"legs?\W+(?:\w+\W+){0,2}?(?:giving (?:out|way)|gave (?:out|way)|won'?t hold)",
            r"incontinen\w*|(?:controll\w*|controllo)\W+(?:\w+\W+){0,2}?(?:vescica|intestino|sfinter\w*)",
            r"(?:loss of|lost|can'?t control)\W+(?:\w+\W+){0,2}?(?:bladder|bowel)",
            r"(?:sput\w*|tossisc\w*|tosse con|vomit\w*|perd\w*)\W+(?:\w+\W+){0,2}?sangue",
            r"sangue\W+(?:\w+\W+)?(?:nelle urine|nelle feci|nel vomito|dalla bocca)",
            r"sanguin\w*\W+(?:\w+\W+)?(?:molto|tanto|abbondant\w*|non si ferma)|emorragi\w*",
            r"(?:cough\w*|vomit\w*|spit\w*)\W+(?:\w+\W+)?blood|blood in (?:my |the )?(?:urine|stool|vomit)",
            r"bleed\w*\W+(?:\w+\W+)?(?:heavily|a lot|won'?t stop)|heavy bleeding|h(?:a)?emorrhag\w*",
            r"paralis\w*|paralizz\w*|paraly\w*|(?:faccia|bocca) storta|slurred speech|face droop\w*",
            r"convulsion\w*|seizure\w*|crisi epilettic\w*",
        )
    )
)
# "non ho dolore al petto": a negation right before the words is not a red flag.
_NEGATED = re.compile(
    r"\b(?:non|nessun\w*|senza|niente|mai|no|not|never|without|don'?t|doesn'?t|didn'?t|haven'?t)"
    r"\s+(?:\w+\s+)?$"
)


def red_flag(text: str) -> bool:
    plain = unicodedata.normalize("NFKD", text.replace("’", "'")).encode("ascii", "ignore")
    lowered = plain.decode().lower()
    return any(
        not _NEGATED.search(lowered[max(0, match.start() - 40) : match.start()])
        for match in _RED_FLAG.finditer(lowered)
    )


async def main(need: Need, city: City) -> None:
    message = city.inbound
    if message is None:
        return
    if message.sender == city.agent_id:
        return
    text = message.text or need.summary
    # A red flag first, before anything else and before the clinical refusal.
    if message.sender.startswith("citizen:") and (
        need.is_emergency
        or understood(message).body.get("is_emergency") is True
        or red_flag(text)
    ):
        chat = await city.history(need_id=message.need_id) if message.need_id else []
        if not _we(chat, city.agent_id, "reason=red_flag", to=message.sender, word="escalate.emergency"):
            await _say(city, message.sender, ESCALATION)
        return
    if clinical(text):
        raise OutOfScope()
    street = _own_street(city)
    if street and _asks_address(message):
        await city.say(message.sender, f"address={street}")
        return
    chat = await city.history(need_id=message.need_id)
    # The turn's own message belongs to the case even when history does not show it yet.
    if all(row.msg_id != message.msg_id for row in chat):
        chat = [*chat, message]
    await _desk(city, message, chat)


def _phrase(chat: list[Message], key: str) -> str:
    phrases = _PHRASES.get(_lang(chat), _PHRASES["it"])
    # A case the GP handed over: our first word to the citizen says who we are and why we write.
    if key in {"handoff", "redirect"} or not any(_from_gp(row) for row in chat):
        return phrases[key]
    if any(row.recipient.startswith("citizen:") for row in chat):
        return phrases[key]
    return f"{phrases['hello']} {phrases[key]}"


async def _tell(city: City, chat: list[Message], citizen: str, text: str, *, again: bool) -> None:
    """Say ``text`` once. Asked again by the citizen, point to the right role once; then quiet."""

    agent = city.agent_id
    if not _exact(chat, agent, text):
        await _say(city, citizen, text)
        return
    redirect = _phrase(chat, "redirect")
    if again and not _exact(chat, agent, redirect):
        await _say(city, citizen, redirect)


# --- desk --------------------------------------------------------------------------------------


async def _desk(city: City, message: Message, chat: list[Message]) -> None:
    kind = words(message.text) | {message.type}
    sender = message.sender
    picked = _fields(message)
    # A choice, a consent, or the answer to our consent request (a "no" comes in plain words).
    if sender.startswith("citizen:") and (
        "confirm" in kind
        or "consent" in kind
        or "offer_id" in picked
        or "consent_ref" in picked
        or _consent_offer(chat, city.agent_id, sender)
    ):
        await _on_confirm(city, message, chat)
        return
    if sender.startswith("agent:") and picked.get("handoff") == "gp":
        await _accept_handoff(city, message, chat)
        return
    if "booking.receipt" in kind and sender.startswith("agent:"):
        await _forward_receipt(city, message, chat)
        return
    if "cancel" in kind and sender.startswith("agent:"):
        await _partner_cancel(city, message, chat)
        return
    if sender == LEDGER or sender.startswith("citizen:"):
        await _advance(city, message, chat, replacing=False)


class _Gate:
    """One need's lock and how many turns hold it or wait for it."""

    __slots__ = ("lock", "turns")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.turns = 0


class _Sent:
    """Offers this process sent for one need, which the case history may not show yet."""

    __slots__ = ("offers", "trigger", "again", "mark")

    def __init__(self) -> None:
        self.offers: set[str] = set()
        self.trigger = ""
        self.again: set[str] = set()
        # Our latest ``agenda`` ask: agenda and world answers before it are stale.
        self.mark = ""


# Turns of one need run at the same time; offers wait on this lock so they are not duplicated.
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


async def _advance(city: City, message: Message, chat: list[Message], *, replacing: bool) -> None:
    key = message.need_id or message.msg_id
    gate = _GATES.get(key)
    if gate is None:
        gate = _GATES[key] = _Gate()
    gate.turns += 1
    waited = gate.lock.locked()
    try:
        async with gate.lock:
            if waited:
                # Another turn of this need (the GP's handoff, the citizen) just wrote: read again.
                chat = await _until(city, message.need_id, chat, lambda rows: True)
                if all(row.msg_id != message.msg_id for row in chat):
                    chat = [*chat, message]
            await _advance_alone(city, message, chat, key, replacing=replacing)
    finally:
        gate.turns -= 1
        if gate.turns == 0 and _GATES.get(key) is gate:
            del _GATES[key]


async def _advance_alone(
    city: City, message: Message, chat: list[Message], key: str, *, replacing: bool
) -> None:
    agent = city.agent_id
    citizen = _citizen(chat)
    if citizen is None:
        return
    need_id = message.need_id
    # A citizen message we already answered (it crossed ours) is not the citizen asking again.
    again = message.sender.startswith("citizen:") and not _answered(chat, agent, message)
    sent = _sent(key)
    number = _number(chat)
    if not number:
        if again:
            ask = f"data.request purpose=booking fields=prescription_number {_phrase(chat, 'number')}"
            await _tell(city, chat, citizen, ask, again=again)
        return
    # A cancel drops every offer the citizen was holding: read agenda and world again, then send
    # what is still free, once per cancel.
    refresh = False
    if replacing:
        trigger = fields_of(message.text).get("offer_id") or message.msg_id
        if sent.trigger != trigger:
            sent.trigger, sent.again, refresh = trigger, set(), True
    # Once the citizen names the number, only a Ledger answer after their message counts. The
    # GP's handoff read it at issue, and a prescription can expire in between (P1).
    since = next(
        (
            row.msg_id
            for row in reversed(chat)
            if row.sender.startswith("citizen:") and _fields(row).get("prescription_number") == number
        ),
        "",
    )
    asked = _we(_after(chat, since), agent, f"prescription_number={number}", to=LEDGER, word="prescription")
    if _prescription(_after(chat, since), number) is None and not asked:
        if not await _say(city, LEDGER, f"prescription prescription_number={number}"):
            return
        # A number seen for the first time after an earlier phase (a renewal): fresh agenda.
        refresh = refresh or bool(sent.mark) or _ledger(chat, "agenda") is not None
    if _prescription(_after(chat, since), number) is None:
        chat = await _until(
            city, need_id, chat, lambda rows: _prescription(_after(rows, since), number) is not None
        )
    reply = _prescription(_after(chat, since), number)
    if reply is None:
        # The Ledger is slow: its answer opens a turn, or the citizen writes again. A citizen
        # writing again about a number the Ledger never confirmed hears that we cannot find it.
        if again and asked:
            ask = f"data.request purpose=booking fields=prescription_number {_phrase(chat, 'unknown_rx')}"
            await _tell(city, chat, citizen, ask, again=again)
        return
    procedure = reply.get("procedure")
    priority = reply.get("priority")
    if not procedure:
        ask = f"data.request purpose=booking fields=prescription_number {_phrase(chat, 'unknown_rx')}"
        await _tell(city, chat, citizen, ask, again=again)
        return
    if reply.get("state") not in (None, "issued"):
        # Expired, used or revoked prescriptions are never booked (P1): the GP renews them.
        await _renewal(city, chat, citizen, again=again)
        return
    if priority == "P":
        offer_id = f"wl-{key}"
        if offer_id in sent.offers or _we(
            chat, agent, f"offer_id={offer_id}", to=citizen, word="offer"
        ):
            return
        text = f"offer offer_id={offer_id} waiting_list=true {_phrase(chat, 'waiting_p')}"
        if await _say(city, citizen, text):
            sent.offers.add(offer_id)
        return

    days, gender = _days(chat), _gender(chat)
    if not replacing and not sent.offers and not _offered(chat, agent):
        missing = [name for name, value in (("days", days), ("gender", gender)) if not value]
        if missing and not await _preferences_ready(city, chat, citizen, missing):
            return
    if refresh or (
        not sent.mark and _ledger(chat, "agenda") is None and not _exact(chat, agent, "agenda")
    ):
        mark = await _say(city, LEDGER, "agenda")
        if mark:
            sent.mark = mark
        await _say(city, LEDGER, "rules")
    recent = _after(chat, sent.mark)
    if _latest_slots(recent) is None and not _asked(recent, agent, procedure, 1):
        await _say(city, LEDGER, f"world procedure={procedure} page=1")

    def fresh(rows: list[Message]) -> bool:
        rows = _after(rows, sent.mark)
        return _has_agenda(rows) and _latest_slots(rows) is not None

    if not fresh(chat):
        chat = await _until(city, need_id, chat, fresh)
    recent = _after(chat, sent.mark)
    now = _when((_ledger(recent, "agenda") or {}).get("agenda_now", ""))
    world = _latest_slots(recent)
    if now is None or world is None:
        return
    rules = _rules(chat, now)
    if replacing:
        taken = _withdrawn(chat, agent) | sent.again
    else:
        taken = _offered(chat, agent) | sent.offers
        # A Ledger turn does not add offers to the ones the citizen already holds.
        if taken and not again:
            return
    pick = {
        "now": now,
        "priority": priority,
        "lang": _lang(chat),
        "gender": gender,
        "days": days,
        "procedure": procedure,
        # The urgent reserve is closed to B, D and P while P3 is in force, or if unknown.
        "reserve": rules is None or "P3" in rules,
    }
    blocked = False
    fallback: list[dict[str, str]] = []
    while True:
        lines = [line for line in world.splitlines() if "slot_id=" in line]
        fit = [slot for slot in fitting(lines, **pick) if slot.get("slot_id") not in taken]
        chosen = [slot for slot in fit if not _down(slot.get("facility", ""))]
        blocked = blocked or len(chosen) < len(fit)
        if chosen:
            preferred = [slot for slot in chosen if _preferred(slot, _lang(chat), gender, days)]
            if preferred or not (days or gender):
                picked = (preferred or chosen)[:_MAX_OFFERS]
                await _offer(city, citizen, picked, number, priority, sent, replacing, chat, "offers")
                return
            fallback.extend(chosen[:_MAX_OFFERS - len(fallback)])
        if taken and not replacing and not fallback:
            # Nothing new to offer. A booked citizen (a "grazie") needs no rinvio.
            if not any(r.sender == agent and first_word(r.text) == "booking.receipt" for r in chat):
                await _tell(city, chat, citizen, _phrase(chat, "redirect"), again=again)
            return
        page, pages = _page(world)
        if page >= pages or page >= _MAX_PAGE:
            break
        if not _asked(recent, agent, procedure, page + 1):
            if not await _say(city, LEDGER, f"world procedure={procedure} page={page + 1}"):
                return

        def turned(rows: list[Message], page: int = page) -> bool:
            return _page(_latest_slots(_after(rows, sent.mark)) or "")[0] > page

        chat = await _until(city, need_id, chat, turned)
        if not turned(chat):
            return  # The Ledger is slow: its answer opens a turn of its own.
        recent = _after(chat, sent.mark)
        world = _latest_slots(recent) or ""
    if fallback:
        await _offer(city, citizen, fallback, number, priority, sent, replacing, chat, "offers")
        return
    if blocked:
        # Only a facility that is down had room: the citizen already has our cancel and reason.
        return
    # S is empty on every page: a relaxed alternative (past the priority deadline), else the
    # waiting list. Never silence.
    relaxed = [
        slot
        for slot in fitting(lines, **pick, relaxed=True)
        if slot.get("slot_id") not in taken and not _down(slot.get("facility", ""))
    ]
    if relaxed:
        picked = relaxed[:_MAX_OFFERS]
        await _offer(city, citizen, picked, number, priority, sent, replacing, chat, "relaxed")
        return
    offer_id = f"wl-{key}"
    if offer_id in sent.offers or _we(chat, agent, f"offer_id={offer_id}", to=citizen, word="offer"):
        return
    if await _say(city, citizen, f"offer offer_id={offer_id} waiting_list=true {_phrase(chat, 'waiting')}"):
        sent.offers.add(offer_id)


async def _accept_handoff(city: City, message: Message, chat: list[Message]) -> None:
    """The GP forwarded the prescription: answer the GP at once (before any Ledger wait), then
    reach the citizen named in ``citizen=`` with the same flow as when they write to us."""

    fields = _fields(message)
    number = fields.get("prescription_number", "")
    if not number or not fields.get("citizen", "").startswith("citizen:"):
        return
    if not _we(chat, city.agent_id, f"prescription_number={number}", to=message.sender, word="data.response"):
        reply = f"data.response handoff=accepted prescription_number={number} {_phrase(chat, 'handoff')}"
        await _say(city, message.sender, reply)
    await _advance(city, message, chat, replacing=False)


async def _offer(
    city: City,
    citizen: str,
    slots: list[dict[str, str]],
    number: str,
    priority: str | None,
    sent: _Sent,
    replacing: bool,
    chat: list[Message],
    phrase: str,
) -> None:
    """Every offer in ONE message: the citizen chooses among what that message holds."""

    lines = [_offer_line(slot, number, priority) for slot in slots]
    text = "\n".join([*lines, _phrase(chat, phrase)])
    if await _say(city, citizen, text):
        for slot in slots:
            sent.offers.add(slot["slot_id"])
            if replacing:
                sent.again.add(slot["slot_id"])


async def _renewal(city: City, chat: list[Message], citizen: str, *, again: bool) -> None:
    ask = f"data.request purpose=prescription_renewal fields=prescription_number {_phrase(chat, 'renewal')}"
    await _tell(city, chat, citizen, ask, again=again)


async def _on_confirm(city: City, message: Message, chat: list[Message]) -> None:
    """The citizen picked an offer: ask the owning facility to hold and commit it.

    Slot, facility and prescription come only from our own offer, found by its ``offer_id``.
    What the citizen writes besides the ``offer_id`` and ``consent_ref`` is never forwarded.
    """

    agent = city.agent_id
    citizen = message.sender
    fields = _fields(message)
    offer_id = fields.get("offer_id") or _consent_offer(chat, agent, citizen)
    offer = _our_offer(chat, agent, offer_id) if offer_id else {}
    if offer.get("waiting_list") == "true":
        return  # The waiting list ends the case here: nothing to book.
    slot = offer.get("slot_id", "")
    facility = offer.get("facility", "")
    if not slot or not facility or _we(chat, agent, f"offer_id={offer_id}", to=citizen, word="cancel"):
        # Not one of our live offers: nothing goes to a facility on the citizen's word.
        ask = f"data.request purpose=booking fields=offer_id {_phrase(chat, 'which')}"
        await _tell(city, chat, citizen, ask, again=True)
        return
    consent = fields.get("consent_ref", "")
    if not consent:
        ask = f"consent purpose=booking offer_id={offer_id} {_phrase(chat, 'consent')}"
        if not _exact(chat, agent, ask):
            await _say(city, citizen, ask)
        else:
            # Asked once and the answer has no consent_ref: that is a no. Say so and move on.
            await _say(
                city,
                citizen,
                f"cancel offer_id={offer_id} reason=consent_missing {_phrase(chat, 'no_consent')}",
            )
        return
    if _we(chat, agent, f"slot_id={slot}", to=facility, word="confirm"):
        return
    parts = [
        "confirm",
        f"offer_id={offer_id}",
        f"slot_id={slot}",
        f"citizen={citizen}",
        f"consent_ref={consent}",
        f"facility={facility}",
    ]
    if offer.get("prescription_number"):
        parts.append(f"prescription_number={offer['prescription_number']}")
    try:
        await city.say(facility, " ".join(parts))
    except CapError:
        # TARGET_NOT_VISIBLE (partner_down) or another refusal: never send it again. Tell the
        # citizen with a reason, then offer what is still free elsewhere.
        _DOWN[facility] = time.monotonic()
        await _say(
            city,
            citizen,
            f"cancel offer_id={offer_id} reason=facility_unavailable {_phrase(chat, 'down')}",
        )
        await _advance(city, message, chat, replacing=True)


async def _forward_receipt(city: City, message: Message, chat: list[Message]) -> None:
    fields = _fields(message)
    booking_id = fields.get("booking_id", "")
    ledger_ref = fields.get("ledger_ref", "")
    citizen = _citizen(chat)
    if not booking_id or not ledger_ref or not citizen:
        return
    if not _we(chat, city.agent_id, f"citizen={citizen}", to=message.sender, word="confirm"):
        return
    if _we(chat, city.agent_id, f"booking_id={booking_id}", to=citizen, word="booking.receipt"):
        return
    await _say(
        city,
        citizen,
        f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref} {_phrase(chat, 'receipt')}",
    )


async def _partner_cancel(city: City, message: Message, chat: list[Message]) -> None:
    offer_id = fields_of(message.text).get("offer_id", "")
    citizen = _citizen(chat)
    # P1: the Ledger refused the commit because the prescription expired. The GP renews it.
    expired = "PRESCRIPTION_EXPIRED" in message.text.upper()
    if (
        offer_id
        and citizen
        and not _we(chat, city.agent_id, f"offer_id={offer_id}", to=citizen, word="cancel")
    ):
        reason, key = ("prescription_expired", "expired") if expired else ("facility_cancelled", "cancelled")
        await _say(city, citizen, f"cancel offer_id={offer_id} reason={reason} {_phrase(chat, key)}")
    if expired and citizen:
        await _renewal(city, chat, citizen, again=False)
        return
    await _advance(city, message, chat, replacing=True)


# --- slot selection ----------------------------------------------------------------------------


def _speaks(doctor_id: str, lang: str) -> bool:
    spec = _DOCTORS.get(doctor_id.rsplit("-", 1)[-1])
    if spec is None:
        return False
    if lang in spec[0]:
        return True
    return "*" in spec[0] and lang not in {"it", "en"}


def _preferred(slot: dict[str, str], lang: str, gender: str | None, days: frozenset[str]) -> bool:
    doctor = slot.get("doctor_id", "")
    spec = _DOCTORS.get(doctor.rsplit("-", 1)[-1])
    return (
        spec is not None and _speaks(doctor, lang)
        and (not gender or spec[1] == gender) and (not days or slot.get("day") in days)
    )


def fitting(
    lines: list[str],
    *,
    now: datetime,
    priority: str | None,
    lang: str,
    gender: str | None = None,
    procedure: str | None = None,
    days: frozenset[str] = frozenset(),
    reserve: bool = True,
    relaxed: bool = False,
) -> list[dict[str, str]]:
    """The answer set S: future, free, in the priority window, outside the urgent reserve when
    ``reserve``. S_pref first (doctor speaks ``lang``, is ``gender``, on one of ``days``), then
    the rest of S, earliest first. ``relaxed`` drops the priority window (S is empty)."""

    if relaxed or priority == "P":
        horizon: datetime | None = None
    elif priority == "U":
        horizon = now.replace(hour=23, minute=59, second=59, microsecond=0)
    else:
        horizon = now + timedelta(days=_DEADLINE_DAYS.get(priority or "", 14))
    earliest = now + _LEAD
    chosen: list[tuple[bool, datetime, str, dict[str, str]]] = []
    for line in lines:
        slot = fields_of(line)
        slot_id = slot.get("slot_id")
        doctor = slot.get("doctor_id")
        at = _when(slot.get("at", ""))
        if not slot_id or not doctor or not slot.get("facility") or at is None:
            continue
        if procedure and slot.get("procedure") not in (None, procedure):
            continue
        free = slot.get("free")
        if free is not None and free.isdigit() and int(free) <= 0:
            continue
        # The urgent reserve is only for priority U (rule P3).
        if reserve and slot.get("urgent_reserve", "").lower() == "true" and priority != "U":
            continue
        if not earliest < at or (horizon is not None and at >= horizon):
            continue
        spec = _DOCTORS.get(doctor.rsplit("-", 1)[-1])
        day = slot.get("day", "")
        if spec is not None and day and day not in spec[2]:
            continue
        # A doctor missing from our table is still in S; we just cannot call it preferred.
        preferred = _preferred(slot, lang, gender, days)
        chosen.append((not preferred, at, slot_id, slot))
    chosen.sort(key=lambda item: item[:3])
    return [item[3] for item in chosen]


def _offer_line(slot: dict[str, str], number: str | None, priority: str | None) -> str:
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


# --- reading the case --------------------------------------------------------------------------


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


def _fields(row: Message) -> dict[str, str]:
    """``key=value`` tokens of the text, plus scalar body fields of a structured message."""

    found = fields_of(row.text)
    provided = row.body.get("provided")
    body = {**row.body, **provided} if isinstance(provided, dict) else row.body
    for key, value in body.items():
        if key != "text" and isinstance(value, str) and value and key not in found:
            found[key] = value
    return found


def _when(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _from_gp(row: Message) -> bool:
    """The GP's handoff: ``need.request handoff=gp citizen=... prescription_number=...``."""

    return row.sender.startswith("agent:") and _fields(row).get("handoff") == "gp"


def _citizen(chat: list[Message]) -> str | None:
    for row in chat:
        if row.sender.startswith("citizen:"):
            return row.sender
    for row in chat:
        party = _fields(row).get("citizen", "") if _from_gp(row) else ""
        if party.startswith("citizen:"):
            return party
    for row in chat:
        fields = fields_of(row.text)
        for key in ("patient", "citizen"):
            party = fields.get(key, "")
            if party.startswith("citizen:"):
                return party
    return None


def _we(chat: list[Message], agent: str, token: str, *, to: str, word: str) -> bool:
    """True when we already sent ``to`` a message with this token and act word."""

    for row in chat:
        if row.sender != agent or row.recipient != to:
            continue
        if token in row.text.split() and word in words(row.text):
            return True
    return False


def _exact(chat: list[Message], agent: str, text: str) -> bool:
    return any(row.sender == agent and row.text.strip() == text for row in chat)


async def _say(city: City, to: str, text: str) -> str:
    """The msg_id of what we said, or "" when the city refused it (never sent again)."""

    try:
        return await city.say(to, text) or "sent"
    except CapError:
        return ""


def _ledger(chat: list[Message], head: str) -> dict[str, str] | None:
    found: dict[str, str] | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == head:
            found = fields_of(row.text)
    return found


def _latest_slots(chat: list[Message]) -> str | None:
    found: str | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == "slots":
            found = row.text
    return found


def _has_agenda(chat: list[Message]) -> bool:
    agenda = _ledger(chat, "agenda")
    return _when("" if agenda is None else agenda.get("agenda_now", "")) is not None


def _prescription(chat: list[Message], number: str) -> dict[str, str] | None:
    """The Ledger's latest answer about prescription ``number``; None before it answers."""

    found: dict[str, str] | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == "prescription":
            fields = fields_of(row.text)
            if fields.get("prescription_number", number) == number:
                found = fields
    return found


def _after(chat: list[Message], mark: str) -> list[Message]:
    """Rows after our message ``mark``, the answers to a fresh ask. No mark, or a mark history
    does not show: the whole case, as before marks existed."""

    for index, row in enumerate(chat):
        if mark and row.msg_id == mark:
            return chat[index + 1 :]
    return chat


def _rules(chat: list[Message], now: datetime) -> set[str] | None:
    """Rule ids (P1, P2, P3) in force at ``now``, from the latest ``rules`` answer of this case
    or, before it arrives, of any case this process saw. None: never read."""

    global _RULES
    text = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == "rules":
            text = row.text
    if text is not None:
        # "P3", "rule_id=P3", "P3=on" are in force; "P3=off" is not. Unsure means in force.
        _RULES = [
            (
                frozenset(
                    rule
                    for rule, value in re.findall(r"\b(P[123])\b(?:=(\S+))?", line)
                    if value.strip(".,;").lower() not in {"off", "false", "0", "no", "inactive"}
                ),
                _when(fields_of(line).get("effective_agenda", "")),
            )
            for line in text.splitlines()
        ]
    if _RULES is None:
        return None
    return {rule for ids, start in _RULES for rule in ids if start is None or start <= now}


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


def _page(text: str) -> tuple[int, int]:
    raw = fields_of(text.splitlines()[0] if text else "").get("page", "")
    left, _, right = raw.partition("/")
    if left.isdigit() and right.isdigit():
        return int(left), int(right)
    return 1, 1


def _asked(chat: list[Message], agent: str, procedure: str, page: int) -> bool:
    page_token = f"page={page}"
    proc_token = f"procedure={procedure}"
    for row in chat:
        tokens = row.text.split()
        if row.sender == agent and page_token in tokens and proc_token in tokens:
            return True
    return False


def _offered(chat: list[Message], agent: str) -> set[str]:
    found: set[str] = set()
    for row in chat:
        if row.sender != agent or "offer" not in words(row.text):
            continue
        found |= {offer["slot_id"] for offer in _offers_in(row.text) if offer.get("slot_id")}
    return found


def _answered(chat: list[Message], agent: str, message: Message) -> bool:
    """True when we already wrote to the sender of ``message`` after it in the case."""

    ids = [row.msg_id for row in chat]
    if message.msg_id not in ids:
        return False
    later = chat[ids.index(message.msg_id) + 1 :]
    return any(row.sender == agent and row.recipient == message.sender for row in later)


def _withdrawn(chat: list[Message], agent: str) -> set[str]:
    """Slots we cancelled. The citizen drops the rest, so those are offered again."""

    by_offer: dict[str, str] = {}
    for row in chat:
        if row.sender != agent or "offer" not in words(row.text):
            continue
        for offer in _offers_in(row.text):
            if offer.get("offer_id") and offer.get("slot_id"):
                by_offer[offer["offer_id"]] = offer["slot_id"]
    dropped: set[str] = set()
    for row in chat:
        if "cancel" not in words(row.text):
            continue
        slot = by_offer.get(fields_of(row.text).get("offer_id", ""))
        if slot:
            dropped.add(slot)
    return dropped


def _our_offer(chat: list[Message], agent: str, offer_id: str) -> dict[str, str]:
    for row in chat:
        if row.sender == agent and "offer" in words(row.text):
            for offer in _offers_in(row.text):
                if offer.get("offer_id") == offer_id:
                    return offer
    return {}


def _offers_in(text: str) -> list[dict[str, str]]:
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


def _consent_offer(chat: list[Message], agent: str, citizen: str) -> str:
    """The offer of our booking consent request, while it is our latest word to the citizen."""

    found = ""
    for row in chat:
        if row.sender == agent and row.recipient == citizen:
            found = fields_of(row.text).get("offer_id", "") if first_word(row.text) == "consent" else ""
    return found


def _down(facility: str) -> bool:
    since = _DOWN.get(facility)
    return since is not None and time.monotonic() - since < _DOWN_SECONDS


def _number(chat: list[Message]) -> str | None:
    for row in reversed(chat):
        if row.sender.startswith("citizen:"):
            number = _fields(row).get("prescription_number")
            if number and not any(character.isspace() for character in number):
                return number
            mentions = {match.upper() for match in _GP_RX.findall(row.text)}
            if len(mentions) == 1:
                return f"prescription_number-{next(iter(mentions))}"
            if mentions:
                return None
        elif _from_gp(row) and _fields(row).get("prescription_number"):
            return _fields(row)["prescription_number"]
    return None


def _lang(chat: list[Message]) -> str:
    for row in chat:
        if row.sender.startswith("citizen:") or _from_gp(row):
            lang = _fields(row).get("lang", "")
            if lang:
                return lang
    return "it"


_WEEKDAYS = {"lun": "mon", "mar": "tue", "mer": "wed", "gio": "thu", "ven": "fri"}


async def _preferences_ready(city: City, chat: list[Message], citizen: str, missing: list[str]) -> bool:
    for index, row in enumerate(chat):
        if row.sender != city.agent_id or row.recipient != citizen or first_word(row.text) != "data.request":
            continue
        fields = fields_of(row.text)
        if fields.get("purpose") == "booking" and fields.get("fields") in {"days", "gender"}:
            # A plain-language refusal also ends the preference request.
            return any(reply.sender == citizen for reply in chat[index + 1:])
    text = f"data.request purpose=booking fields={','.join(missing)} {_phrase(chat, 'prefs')}"
    await _say(city, citizen, text)
    return False


def _days(chat: list[Message]) -> frozenset[str]:
    """Agenda days the citizen prefers (``days=mon/wed``) as mon..fri; empty when not said."""

    for row in reversed(chat):
        if not row.sender.startswith("citizen:"):
            continue
        raw = row.body.get("days")
        provided = row.body.get("provided")
        if isinstance(provided, dict):
            raw = provided.get("days", raw)
        if not isinstance(raw, (str, list)):
            match = re.search(r"(?<!\S)days=([a-z]+(?:\s*[/,|;]\s*[a-z]+)*)", row.text, re.IGNORECASE)
            raw = match.group(1) if match else None
        if raw:
            names = raw if isinstance(raw, list) else re.split(r"[/,|;]", raw)
            short = {str(name).strip().lower()[:3] for name in names}
            return frozenset({_WEEKDAYS.get(day, day) for day in short} & set(_WEEKDAYS.values()))
    return frozenset()


def _gender(chat: list[Message]) -> str | None:
    for row in chat:
        if not row.sender.startswith("citizen:"):
            continue
        fields = _fields(row)
        raw = fields.get("gender_preference") or fields.get("gender")
        if raw in {"m", "f"}:
            return raw
    return None


# --- address -----------------------------------------------------------------------------------


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
        return "address" in {part.strip() for part in re.split(r"[,\s]+", raw) if part.strip()}
    return False


def _own_street(city: City) -> str:
    street = getattr(city, "address", "")
    if isinstance(street, str) and street.strip():
        return street.strip()
    return os.environ.get("CITY_ADDRESS", "").strip()
