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
import hashlib
import json
import os
import re
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from city_sdk.city import LEDGER, CapError, City, Message, Need, OutOfScope, clinical, understood

import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import tools  # noqa: E402  (shared/tools.py, copied by shared/sync.sh; best effort, never raises)

_MAX_PAGE = 8
_MAX_OFFERS = 3
_SAY_MAX = 1900
# The agenda runs 60x: one agenda hour is a real minute, so a slot is still ahead when the
# citizen decides (the citizen confirms within seconds of the offer). _lead() adds the Ledger's
# round trip: the facility's hold lands that much later (21/11: ~270 s, 4-5 agenda hours).
_LEAD = timedelta(hours=1)
# The freshest agenda reply this process read, in any case: the city has one agenda clock.
_CLOCK: dict[str, str] = {}
# (answer's sent_at, seconds) of the latest Ledger round trip seen in a case history.
# ponytail: the latest single round trip, capped at _RTT_MAX; a median of the last few if the
# Ledger's answers swing (21/11: median ~270 s, max 648 s).
_RTT: tuple[datetime, float] | None = None
_RTT_MAX = 600.0
# Slots offered to a citizen (slot -> citizen, when): another citizen gets them only after
# every unshared slot (21/11: one slot to five citizens in three minutes, four holds lost).
# ponytail: per process and a fixed window; a second pod does not see them.
_OFFERED: dict[str, tuple[str, float]] = {}
_OFFER_SECONDS = 900.0
# World pages of any case, answers and pushes alike (the slots are the ASST's, not the case's):
# (procedure, page) -> (time.monotonic() when first seen, Ledger sent_at, page text). A new case
# offers from a page younger than _PAGE_FRESH in the turn it becomes offerable, then asks the
# Ledger itself (21/11: world answers in ~270 s, max 648 s; the Relay adds 2-6 minutes).
# ponytail: per process; 4 real minutes = 4 agenda hours at 60x. A slot taken since comes back as
# a refused hold (_GONE, answered in the same turn): shorten it if those refusals climb.
_PAGES: dict[tuple[str, int], tuple[float, datetime | None, str]] = {}
_PAGE_FRESH = 240.0
# Slots a facility lost on the Ledger (taken, expired), or that we asked a facility to book:
# never offered again by this process.
_GONE: set[str] = set()
_GONE_CODES = frozenset({"NO_CAPACITY", "EXPIRED", "SLOT_TAKEN"})
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
            "Volentieri, la aiuto io a prenotare. Ha già la ricetta del medico di base? Se sì, mi "
            "indichi il numero e cerco subito un posto; se non l'ha ancora, il suo medico di base "
            "può prescriverla e poi prenotiamo insieme."
        ),
        "unknown_rx": (
            "Mi scusi, non trovo una ricetta con questo numero: può ricontrollarlo? Appena ce l'ho, "
            "prenotiamo."
        ),
        "renewal": (
            "Mi dispiace, la ricetta non è più valida: il suo medico di base può rinnovarla con lo "
            "stesso numero. Appena mi manda il numero nuovo, prenotiamo insieme."
        ),
        "offers": (
            "Quale preferisce? Mi basta il numero dell'opzione e procedo io. Se nessuno le va bene, "
            "mi dica giorni e preferenze sul medico (donna o uomo, lingua) e cerco altre date."
        ),
        "offers_again": (
            "Capisco, tolgo quell'opzione. Quale di queste preferisce? Se il problema era il "
            "giorno, l'orario o il medico, me lo dica e cerco di conseguenza."
        ),
        "found": "Ho trovato questi posti per lei:",
        "listen": "Capisco la sua preoccupazione, me ne occupo subito.",
        "booked": "Riepilogo: {what}.",
        "more": "Posso aiutarla in altro?",
        "bring": (
            "Per il giorno della visita: porti con sé la ricetta e la tessera sanitaria e arrivi "
            "qualche minuto prima. Se deve spostarla o annullarla, mi scriva qui e la riorganizzo io."
        ),
        "wheelchair": "La struttura è accessibile in sedia a rotelle.",
        "pending": "Sto completando la sua prenotazione con la struttura: le mando la ricevuta tra un istante.",
        "welcome": "Prego, è stato un piacere. La prenotazione resta confermata: per qualsiasi altra cosa sono qui.",
        "dropped": "Va bene, tolgo quell'opzione: le altre restano valide. Quale preferisce?",
        "partners": (
            "Nella nostra ASST al momento non c'è un posto adatto alla sua richiesta: sto chiedendo "
            "alle strutture partner delle altre ASST, senza condividere i suoi dati. Le scrivo appena "
            "ho una risposta."
        ),
        "partner_offers": (
            "Sono posti presso una struttura partner di un'altra ASST. Quale preferisce? Per "
            "prenotare lì le chiederò prima il consenso a condividere i suoi dati con quella struttura."
        ),
        "cross_consent": (
            "Questa visita è presso una struttura di un'altra ASST: per prenotarla devo condividere "
            "con loro i suoi dati. Mi dà il consenso?"
        ),
        "relaxed": (
            "Mi dispiace, entro la scadenza della priorità non ci sono posti liberi: queste sono le "
            "prime alternative disponibili. Quale preferisce? Se nessuna va bene, mi dica giorni o "
            "orari più comodi e cerco ancora."
        ),
        "relaxed_lang": (
            "Mi dispiace, nessuno dei nostri medici parla la sua lingua: queste sono le prime "
            "alternative, con medici che parlano italiano. Quale preferisce? Se nessuna va bene, "
            "posso inserirla in lista d'attesa."
        ),
        "searching": (
            "Capisco, tolgo quelle opzioni. Sto controllando altri posti liberi e le scrivo appena "
            "li ho."
        ),
        "waiting": (
            "Mi dispiace, al momento non ci sono posti disponibili: le propongo la lista d'attesa. "
            "Vuole che la inserisca? Se preferisce, mi dica giorni o orari comodi e ricontrollo."
        ),
        "waiting_p": "Per la priorità P la prenotazione passa dalla lista d'attesa: gliela propongo qui.",
        "which": "Mi scusi, non trovo questa scelta tra le offerte che le ho mandato: quale offerta sceglie?",
        "consent": "Perfetto. Per completare la prenotazione mi serve il suo consenso: può confermarlo?",
        "consent_code": "Per completare la prenotazione mi serve il codice del consenso (consent_ref).",
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
            "Happy to help you book. Do you already have a prescription from your GP? If so, send "
            "me its number and I will look for a slot right away; if not yet, your GP can issue it "
            "and then we book together."
        ),
        "unknown_rx": (
            "Sorry, I cannot find a prescription with this number: could you check it? As soon as "
            "I have it, we book."
        ),
        "renewal": (
            "I am sorry, the prescription is no longer valid: your GP can renew it with the same "
            "number. As soon as you send me the new number, we book together."
        ),
        "offers": (
            "Which one do you prefer? The option number is enough and I will take care of it. If "
            "none suits you, tell me your days and doctor preferences (female or male, language) "
            "and I will look for other dates."
        ),
        "offers_again": (
            "Understood, I am dropping that option. Which of these do you prefer? If the problem "
            "was the day, the time or the doctor, tell me and I will search accordingly."
        ),
        "found": "I found these slots for you:",
        "listen": "I understand your concern, I am taking care of it right away.",
        "booked": "Summary: {what}.",
        "more": "Can I help you with anything else?",
        "bring": (
            "For the day of the visit: bring your prescription and your health card, and arrive a "
            "few minutes early. If you need to move or cancel it, write to me here and I will rearrange it."
        ),
        "wheelchair": "The facility is wheelchair accessible.",
        "pending": "I am completing your booking with the facility: your receipt will follow in a moment.",
        "welcome": "You are welcome. Your booking stays confirmed: I am here if you need anything else.",
        "dropped": "All right, I am dropping that option: the others are still valid. Which one do you prefer?",
        "partners": (
            "Our own facilities have no slot that fits your request right now: I am asking the "
            "partner facilities of the other health districts, without sharing your data. I will "
            "write as soon as I have an answer."
        ),
        "partner_offers": (
            "These slots are at a partner facility of another health district. Which one do you "
            "prefer? To book there I will first ask your consent to share your data with that facility."
        ),
        "cross_consent": (
            "This visit is at a facility of another health district: to book it I need to share "
            "your data with them. Do you agree?"
        ),
        "relaxed": (
            "I am sorry, there are no slots within the deadline of your priority: these are the "
            "first alternatives available. Which one do you prefer? If none works, tell me the "
            "days or times that suit you and I will look again."
        ),
        "relaxed_lang": (
            "I am sorry, none of our doctors speaks your language: these are the first "
            "alternatives, with doctors who speak Italian. Which one do you prefer? If none works, "
            "I can put you on the waiting list."
        ),
        "searching": (
            "Understood, I am dropping those options. I am checking other free slots and will "
            "write to you as soon as I have them."
        ),
        "waiting": (
            "I am sorry, there are no free slots right now: I can put you on the waiting list. "
            "Shall I add you? If you prefer, tell me the days or times that suit you and I check again."
        ),
        "waiting_p": "For priority P the booking goes through the waiting list: I am offering it to you here.",
        "which": "Sorry, I cannot match this choice with the slots I sent you: which one do you choose?",
        "consent": "Great. To complete the booking I need your agreement: could you give it?",
        "consent_code": "To complete the booking I need the code of your consent (consent_ref).",
        "no_consent": "I understand. Without your agreement I cannot complete this booking: if you change your mind, I am here.",
        "down": (
            "Sorry, the facility cannot be reached right now, so this slot is withdrawn: I am "
            "looking for an alternative straight away."
        ),
        "cancelled": "I am sorry, the facility withdrew this slot: I am looking for an alternative for you.",
        "expired": "I am sorry, the facility reports that the prescription has expired.",
        "receipt": "Your booking is confirmed: please keep this receipt for check-in at the facility.",
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
    # The team's trace (episodes): partners' answers included, which no console shows.
    tools.start_turn(message)
    street = _own_street(city)
    if street and _asks_address(message):
        await city.say(message.sender, f"address={street}")
        return
    if message.sender.startswith("agent:"):
        # Only booking messages from peers need the case. An unrelated request can finish
        # without waiting for a remote history read before the harness renews its delivery.
        kind = words(message.text) | {message.type}
        if not (kind & {"offer", "booking.receipt", "cancel", "cancelled"}) and _fields(message).get("handoff") != "gp":
            return
    chat = await city.history(need_id=message.need_id)
    # The turn's own message belongs to the case even when history does not show it yet.
    if all(row.msg_id != message.msg_id for row in chat):
        chat = [*chat, message]
    _keep_pages(chat)
    if message.need_id and any(
        row.sender == city.agent_id and first_word(row.text) == "escalate.emergency" for row in chat
    ):
        return  # An emergency ends the case: nothing else on this need (R:442), whatever arrives.
    await _desk(city, message, chat)


def _phrase(chat: list[Message], key: str) -> str:
    phrases = _PHRASES[_talk(chat)]
    # A case the GP handed over: our first word to the citizen says who we are and why we write.
    if key == "handoff" or any(row.recipient.startswith("citizen:") for row in chat):
        return phrases[key]
    # Our first word to the citizen. A case the GP handed over: say who we are and why we write.
    if key != "redirect" and any(_from_gp(row) for row in chat):
        return f"{phrases['hello']} {phrases[key]}"
    # A citizen who wrote to us worried is heard before anything else.
    if any(row.sender.startswith("citizen:") and _WORRIED.search(_plain(row.text or "")) for row in chat):
        return f"{phrases['listen']} {phrases[key]}"
    return phrases[key]


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
    if picked.get("offer_id", "").strip().lower() in _NO_OFFER:
        # "offer_id=none" (a partner's no_availability, journey 0215) names no offer at all.
        picked = {key: value for key, value in picked.items() if key != "offer_id"}
    # Free text (level 3): which of our offers, a yes/no to consent, days, gender, language.
    heard = await _hear(city, message, chat) if sender.startswith("citizen:") else {}
    # "cancel offer_id=... reason=lang": the citizen turns an offer down. Never a choice.
    if sender.startswith("citizen:") and "cancel" in kind and "confirm" not in kind and picked.get("offer_id"):
        await _citizen_cancel(city, message, chat)
        return
    # A choice, a consent, or the answer to our consent request (a "no" comes in plain words).
    if sender.startswith("citizen:") and (
        "confirm" in kind
        or "consent" in kind
        or "offer_id" in picked
        or "consent_ref" in picked
        or heard.get("offer_id")
        or _consent_offer(chat, city.agent_id, sender)
    ):
        await _on_confirm(city, message, chat, heard)
        return
    if (
        sender.startswith("agent:")
        and sender not in _OUR_FACILITIES
        and picked.get("request_id")
        and ("offer" in kind or ("cancel" in kind and "offer_id" not in picked))
    ):
        await _advance(city, message, chat, replacing=False, partner=True)
        return
    if sender.startswith("agent:") and picked.get("handoff") == "gp":
        await _accept_handoff(city, message, chat)
        return
    if "booking.receipt" in kind and sender.startswith("agent:"):
        await _forward_receipt(city, message, chat)
        return
    if (kind & {"cancel", "cancelled"}) and (
        sender.startswith("agent:")
        # From level 6 the city cancels each citizen's first booking: whoever tells us (the
        # facility, or the city itself naming the booking), we rebook at once.
        or (sender.startswith("city:") and _names_ours(chat, city.agent_id, picked))
    ):
        await _partner_cancel(city, message, chat)
        return
    if sender.startswith("citizen:") and not picked.get("prescription_number"):
        # Booking on its way to the facility, or already confirmed: no new offers on a "grazie".
        stage = _booking_stage(chat, city.agent_id, sender)
        if stage:
            text = _phrase(chat, "pending" if stage == "flight" else "welcome")
            if not _exact(chat, city.agent_id, text):
                await _say(city, sender, text)
            return
    if sender == LEDGER or sender.startswith("citizen:"):
        await _advance(city, message, chat, replacing=False)


class _Gate:
    """One need's lock and how many turns hold it or wait for it."""

    __slots__ = ("lock", "turns", "wake")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.turns = 0
        # The latest Ledger answer that arrived while another turn held the lock: the holder
        # runs once more for it, instead of one queued turn per answer.
        self.wake: Message | None = None


class _Sent:
    """Offers this process sent for one need, which the case history may not show yet."""

    __slots__ = ("offers", "trigger", "again", "mark", "before", "cancelled", "said", "pending", "reread")

    def __init__(self) -> None:
        self.offers: set[str] = set()
        # Offers whose cancel we already told the citizen (two facility turns can race).
        self.cancelled: set[str] = set()
        # Sentences and receipts already sent in this process: said once, whatever the race.
        self.said: set[str] = set()
        self.trigger = ""
        self.again: set[str] = set()
        # Our latest ``agenda`` ask: agenda and world answers before it are stale.
        self.mark = ""
        # The rows the case had when we asked: until history shows the mark (or if the city gave
        # the ask no id), only rows that are not among these are answers to it.
        self.before: frozenset[str] = frozenset()
        # A replacement we owe the citizen (we said "cerco un'alternativa") that a slow Ledger
        # left unsent: the next turn of the case resumes it (journeys 0189, 0191 ended in silence).
        self.pending = False
        # Ledger reads to send once the citizen has our answer (a refusal answered from the pages
        # the case already had, a new case from _PAGES): fresh pages for the next round, never
        # waited for.
        self.reread: list[str] = []


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


async def _advance(
    city: City, message: Message, chat: list[Message], *, replacing: bool, partner: bool = False
) -> None:
    key = message.need_id or message.msg_id
    gate = _GATES.get(key)
    if gate is None:
        gate = _GATES[key] = _Gate()
    if message.sender == LEDGER and not partner and not replacing and gate.lock.locked():
        # Local study 21/11 (Ledger at 25 s): each Ledger answer opened a turn that queued on the
        # lock, then polled ~12 s on its own; the citizen's next message waited behind them all
        # (up to 115 s). The turn holding the lock reads the case again and handles it.
        gate.wake = message
        return
    gate.turns += 1
    waited = gate.lock.locked()
    try:
        async with gate.lock:
            if waited:
                # Another turn of this need (the GP's handoff, the citizen) just wrote: read again.
                chat = await _until(city, message.need_id, chat, lambda rows: True)
                if all(row.msg_id != message.msg_id for row in chat):
                    chat = [*chat, message]
            if partner:
                await _partner_answer(city, message, chat, key)
            else:
                await _advance_alone(city, message, chat, key, replacing=replacing)
                sent = _sent(key)
                reads, sent.reread = sent.reread, []
                for read in reads:
                    await _say(city, LEDGER, read)
            while gate.wake is not None:
                wake, gate.wake = gate.wake, None
                chat = await _until(city, wake.need_id, chat, lambda rows: True)
                if all(row.msg_id != wake.msg_id for row in chat):
                    chat = [*chat, wake]
                await _advance_alone(city, wake, chat, key, replacing=False)
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
    resumed = not replacing and sent.pending
    if resumed:
        replacing = True
    if replacing and not resumed:
        trigger = fields_of(message.text).get("offer_id") or message.msg_id
        if message.sender.startswith("citizen:"):
            # A citizen refusing several offers of one message is one refusal: one new search.
            trigger = "citizen:" + (_offer_source(chat, agent, trigger) or trigger)
        if sent.trigger != trigger:
            sent.trigger, sent.again, refresh = trigger, set(), True
        elif message.sender.startswith("citizen:"):
            return
    if replacing:
        sent.pending = True  # cleared by the next message the citizen gets from us
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
    # With no rule that expires prescriptions (P1 off: 21/11 rules count=0), an issued answer from
    # earlier in the case counts too. Live N-2026-11-02-890924000260: the citizen wrote the number
    # again while our re-read was in flight, the Relay sorted it before that read, and the issued
    # answer was ignored for half an hour. Rules unknown or P1 on: only an answer after them.
    now = _now(chat)
    rules = None if now is None else _rules(chat, now)
    known = _prescription(chat, number) if rules is not None and "P1" not in rules else None
    if known is not None and (not known.get("procedure") or known.get("state") not in (None, "issued")):
        known = None
    asked = _we(_after(chat, since), agent, f"prescription_number={number}", to=LEDGER, word="prescription")
    if known is None and _prescription(_after(chat, since), number) is None and not asked:
        if not await _say(city, LEDGER, f"prescription prescription_number={number}"):
            return
        # A number seen for the first time after an earlier phase (a renewal): fresh agenda.
        refresh = refresh or bool(sent.mark) or _ledger(chat, "agenda") is not None
    # Our GP's handoff names procedure and priority of the prescription it just wrote: the search
    # starts at once and the Ledger's check runs beside it (21/11: 5-7 minutes for that answer).
    # The Ledger's answer prevails when it comes (expired, used: the renewal below). P1 in force,
    # or the citizen writing the number themselves (maybe days later): only the Ledger's answer.
    handed = None if since or (rules is not None and "P1" in rules) else _handed(chat, number)
    if known is None and handed is None and _prescription(_after(chat, since), number) is None:
        chat = await _until(
            city, need_id, chat, lambda rows: _prescription(_after(rows, since), number) is not None
        )
    reply = known or _prescription(_after(chat, since), number) or handed
    if reply is None:
        # Not answered yet (21/11: 4-7 minutes): its answer opens a turn. Never "not found" for an
        # answer still on its way; only a real one (no procedure, or refused) says so, below.
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

    # No round of questions before the first offer (21/11: 4-6 minutes per journey): up to three
    # slots as different as can be (_spread: days, then doctors), with the preferences already
    # known; the offer's closing asks for days and doctor if none fits (R:95, R:253).
    days, gender = _days(chat), _gender(chat)

    def page_of(rows: list[Message], page: int) -> str | None:
        """The page from _PAGES while fresh (the latest answer any case saw), else the case's own."""
        return _fresh_page(procedure, page) or _world_page(rows, page)

    # A refusal or a facility's cancel is answered in its own turn, from the pages the case
    # already holds and the clock run forward (21/11: re-reading agenda, rules and world first
    # left the citizen 4-5 minutes in silence). So is a new case, from the pages another case
    # read (_PAGES): every new citizen waited minutes for world. The reads go out after the answer.
    first = not replacing and not sent.offers and not _offered(chat, agent) and _fresh_page(procedure, 1) is not None
    quick = _now(chat) is not None and (first or (replacing and not resumed and page_of(chat, 1) is not None))
    if quick:
        recent = chat
        if replacing:
            sent.reread = [f"world procedure={procedure} page=1"]
        elif not _asked(chat, agent, procedure, 1):
            sent.reread = ["agenda", "rules", f"world procedure={procedure} page=1"]
    else:
        if refresh or (
            not sent.mark and _ledger(chat, "agenda") is None and not _exact(chat, agent, "agenda")
        ):
            mark = await _say(city, LEDGER, "agenda")
            if mark:
                sent.mark, sent.before = mark, frozenset(row.msg_id for row in chat)
            await _say(city, LEDGER, "rules")
        recent = _recent(chat, sent)
        if _world_page(recent, 1) is None and not _asked(recent, agent, procedure, 1):
            await _say(city, LEDGER, f"world procedure={procedure} page=1")

        def fresh(rows: list[Message]) -> bool:
            rows = _recent(rows, sent)
            return _has_agenda(rows) and page_of(rows, 1) is not None

        if not fresh(chat):
            chat = await _until(city, need_id, chat, fresh)
        recent = _recent(chat, sent)
    _round_trip(chat, agent)
    now = _now(chat)
    born = _born(chat)
    world = page_of(recent, 1)
    if now is None or world is None:
        return
    rules = _rules(chat, now)
    if replacing:
        taken = _withdrawn(chat, agent) | sent.again
    else:
        # Offers we cancelled are not held (an expired prescription renewed: the new number books).
        taken = (_offered(chat, agent) | sent.offers) - _withdrawn(chat, agent)
        # A Ledger turn does not add offers to the ones the citizen already holds.
        if taken and not again:
            return
    pick = {
        "now": now,
        "born": born,
        "priority": priority,
        "lang": _lang(chat),
        "gender": gender,
        "days": days,
        "procedure": procedure,
        # The urgent reserve is closed to B, D and P while P3 is in force, or if unknown.
        "reserve": rules is None or "P3" in rules,
    }
    fallback: list[dict[str, str]] = []
    all_lines: dict[str, str] = {}
    # A language one of our doctors speaks is a hard need (the citizen refuses anyone else,
    # journey 0071): only those doctors, on every page, then past the deadline. A language none
    # of ours speaks filters only once the citizen turned a doctor down for it (21/11, journey
    # 238: three offers refused reason=lang): any other doctor of ours would be refused again.
    ours = any(pick["lang"] in spec[0] or (
        "*" in spec[0] and pick["lang"] == _RARE_LANG) for spec in _DOCTORS.values())
    spoken = pick["lang"] != "it" and (ours or _refused_for(chat, {"lang", "language"}))
    refused_slots, refused_doctors = _refused(chat, agent)
    taken = taken | refused_slots | _GONE
    cross = (city, message, chat, citizen, number, priority, procedure, pick, taken, refused_doctors, sent, replacing)
    # A language none of our doctors speaks: only a partner ASST can have that doctor.
    if pick["lang"] != "it" and not ours and await _try_partners(*cross):
        return
    # After the citizen turned an offer down, the new ones come with a word about it.
    opening = "offers_again" if replacing and message.sender.startswith("citizen:") else "offers"
    while True:
        lines = [line for line in world.splitlines() if "slot_id=" in line]
        for line in lines:
            if slot_id := fields_of(line).get("slot_id"):
                all_lines[slot_id] = line
        chosen = [
            slot
            for slot in fitting(lines, **pick)
            if slot.get("slot_id") not in taken
            and slot.get("doctor_id") not in refused_doctors
            and (not spoken or _speaks(slot.get("doctor_id", ""), pick["lang"]))
            and not _down(slot.get("facility", ""))
        ]
        if chosen:
            preferred = [slot for slot in chosen if _preferred(slot, _lang(chat), gender, days)]
            if preferred or not (days or gender):
                picked = _spread(preferred or chosen, _MAX_OFFERS, citizen)
                await _offer(city, citizen, picked, number, priority, sent, replacing, chat, opening)
                return
            fallback.extend(_fresh_first(chosen, citizen)[:_MAX_OFFERS - len(fallback)])
        if taken and not replacing and not fallback:
            # Nothing new to offer. A booked citizen (a "grazie") needs no rinvio.
            if not any(r.sender == agent and first_word(r.text) == "booking.receipt" for r in chat):
                await _tell(city, chat, citizen, _phrase(chat, "redirect"), again=again)
            return
        page, pages = _page(world)
        if page >= pages or page >= _MAX_PAGE:
            break
        next_page = page + 1
        if page_of(recent, next_page) is None:
            # Every missing page in one go, never one after the other (21/11: ~5 minutes a Ledger
            # round, up to _MAX_PAGE rounds in a row). The answers land in history as they come.
            for later in range(next_page, min(pages, _MAX_PAGE) + 1):
                if (
                    page_of(recent, later) is None
                    and not _asked(recent, agent, procedure, later)
                    and not await _say(city, LEDGER, f"world procedure={procedure} page={later}")
                    and later == next_page
                ):
                    return
            if quick and replacing:
                # One look, no wait: offer what fits so far, else say we are on it. The Ledger's
                # answer opens a turn of its own, which resumes the search (pending).
                chat = recent = await _until(
                    city, need_id, chat, lambda rows: page_of(rows, next_page) is not None, tries=1
                )
                if page_of(recent, next_page) is None:
                    if fallback:
                        break
                    if f"searching:{sent.trigger}" not in sent.said:
                        sent.said.add(f"searching:{sent.trigger}")
                        await _say(city, citizen, _phrase(chat, "searching"))
                    sent.pending = True
                    return
            else:
                chat = await _until(
                    city, need_id, chat, lambda rows: page_of(_recent(rows, sent), next_page) is not None
                )
                recent = _recent(chat, sent)
        world = page_of(recent, next_page)
        if world is None:
            return  # On the Ledger's next turn, start with the pages already in history.
    if fallback:
        await _offer(city, citizen, fallback, number, priority, sent, replacing, chat, opening)
        return
    # S is empty on every page: a relaxed alternative (past the priority deadline), else the
    # waiting list. Never silence. The partners are asked only for a language none of our
    # doctors speaks (above): for a full agenda they gave no usable slot (0 of 15 answers,
    # 85 s median wait), so the citizen gets our alternative at once.
    late = _refused_for(chat, {"deadline", "priority", "late", "time"})

    def alternatives(keep_lang: bool) -> list[dict[str, str]]:
        return [
            slot
            for slot in fitting(list(all_lines.values()), **pick, relaxed=not late)
            if slot.get("slot_id") not in taken
            and slot.get("doctor_id") not in refused_doctors
            and (not keep_lang or not spoken or _speaks(slot.get("doctor_id", ""), pick["lang"]))
            and not _down(slot.get("facility", ""))
        ]

    relaxed, phrase = ([] if late else alternatives(True)), "relaxed"
    if not relaxed and spoken and not ours:
        # None of our doctors speaks it, and the citizen said it matters: the honest alternative
        # is a doctor who speaks Italian, said as such.
        relaxed, phrase = alternatives(False), "relaxed_lang"
    if relaxed:
        # The relaxed offer gives the closest real dates, even when they were on earlier pages.
        picked = sorted(
            relaxed, key=lambda slot: (_shared(slot, citizen), datetime.fromisoformat(slot["at"]))
        )[:_MAX_OFFERS]
        await _offer(city, citizen, picked, number, priority, sent, replacing, chat, phrase)
        return
    offer_id = f"wl-{key}"
    if offer_id in sent.offers or _we(chat, agent, f"offer_id={offer_id}", to=citizen, word="offer"):
        return
    if await _say(city, citizen, f"offer offer_id={offer_id} waiting_list=true {_phrase(chat, 'waiting')}"):
        sent.offers.add(offer_id)
        await _notify_gp_no_slots(city, chat, citizen, number)


async def _notify_gp_no_slots(city: City, chat: list[Message], citizen: str, number: str) -> None:
    for row in reversed(chat):
        if row.recipient != city.agent_id or not _from_gp(row):
            continue
        fields = _fields(row)
        if fields.get("citizen") == citizen and fields.get("prescription_number") == number:
            if fields.get("notify_if") == "no_slots":
                await _say(
                    city, row.sender,
                    f"data.response purpose=booking_follow_up outcome=no_slots prescription_number={number} "
                    "Non ho trovato un posto disponibile e ho proposto la lista d'attesa.",
                )
            return


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
    lang = _talk(chat)
    # Tokens for the citizen's choice, then the same slots in plain words (Ledger dates only)
    # and one question: an answer and a next step in every message.
    readable = [f"{i}) {_when_words(slot.get('at', ''), lang)}{_who(slot, lang)}" for i, slot in enumerate(slots, 1)]
    closing = _phrase(chat, phrase)
    if all(label.split(") ", 1)[1] for label in readable):
        text = "\n".join([*lines, _phrases(chat)["found"], *readable, closing])
    else:
        text = "\n".join([*lines, closing])
    # city.say takes 2000 characters: the tokens matter most, then fewer offers.
    while len(text) > _SAY_MAX and len(lines) > 1:
        lines, slots = lines[:-1], slots[:-1]
        text = "\n".join([*lines, closing])
    if len(text) > _SAY_MAX:
        text = lines[0][:_SAY_MAX]
    if await _say(city, citizen, text):
        clock = time.monotonic()
        for key, (_, at) in list(_OFFERED.items()):
            if clock - at >= _OFFER_SECONDS:
                del _OFFERED[key]
        for slot in slots:
            sent.offers.add(slot["slot_id"])
            _OFFERED[slot["slot_id"]] = (citizen, clock)
            if replacing:
                sent.again.add(slot["slot_id"])


def _shared(slot: dict[str, str], citizen: str) -> bool:
    """Offered to another citizen in the last few minutes: their confirm may take it first."""

    owner = _OFFERED.get(slot.get("slot_id", ""))
    return owner is not None and owner[0] != citizen and time.monotonic() - owner[1] < _OFFER_SECONDS


def _fresh_first(slots: list[dict[str, str]], citizen: str) -> list[dict[str, str]]:
    """The same slots, the ones no other citizen holds first (a refusal beats no offer)."""

    return sorted(slots, key=lambda slot: _shared(slot, citizen))


def _who(slot: dict[str, str], lang: str) -> str:
    """The doctor in a few words, from our registry only (a partner's doctor is not described:
    a false attribute costs -2). Two offers at the same hour then read differently."""

    if slot.get("facility") not in _OUR_FACILITIES:
        return ""
    spec = _DOCTORS.get(slot.get("doctor_id", "").rsplit("-", 1)[-1])
    if spec is None:
        return ""
    if lang == "en":
        return ", female doctor" if spec[1] == "f" else ", male doctor"
    return ", con una dottoressa" if spec[1] == "f" else ", con un dottore"


async def _renewal(city: City, chat: list[Message], citizen: str, *, again: bool) -> None:
    ask = f"data.request purpose=prescription_renewal fields=prescription_number {_phrase(chat, 'renewal')}"
    await _tell(city, chat, citizen, ask, again=again)


async def _on_confirm(
    city: City, message: Message, chat: list[Message], heard: dict[str, object] | None = None
) -> None:
    """The citizen picked an offer: ask the owning facility to hold and commit it.

    Slot, facility and prescription come only from our own offer, found by its ``offer_id``.
    What the citizen writes besides the ``offer_id`` and ``consent_ref`` is never forwarded.
    """

    agent = city.agent_id
    citizen = message.sender
    fields = _fields(message)
    heard = heard or {}
    # The offer named in free text is one of OUR offers (``_hear`` only picks from them).
    offer_id = (
        fields.get("offer_id")
        or _consent_offer(chat, agent, citizen)
        or str(heard.get("offer_id") or "")
    )
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
    number = offer.get("prescription_number") or _number(chat)
    rx = _prescription(chat, number) if number else None
    if rx is not None and rx.get("state") not in (None, "issued"):
        # P1: the Ledger's latest answer says the prescription expired after our offer (the offer
        # now leaves before any question): the offer goes, the GP renews it. Never a doomed commit.
        for live in _live_offers(chat, agent, citizen):
            await _say(city, citizen, f"cancel offer_id={live['offer_id']} reason=prescription_expired")
        await _renewal(city, chat, citizen, again=False)
        return
    consent = fields.get("consent_ref", "")
    if not consent:
        # Level 3: the code written in prose ("il mio consenso è cons-N-...-booking").
        named = {match.rstrip(".,;:!?") for match in re.findall(r"\bcons-[\w-]+", message.text or "")}
        consent = named.pop() if len(named) == 1 else ""
    if not consent and _we(chat, agent, f"slot_id={slot}", to=facility, word="confirm"):
        return  # already confirmed to the facility: nothing to ask again
    if facility not in _OUR_FACILITIES:
        # Rule P2: no hold in another ASST, and none of the citizen's data there, before their
        # consent with purpose cross_asst. Its reference is the one the partner's hold needs.
        ask = f"consent purpose=cross_asst offer_id={offer_id} {_phrase(chat, 'cross_consent')}"
        given = consent if consent and (fields.get("purpose") == "cross_asst" or _exact(chat, agent, ask)) else ""
        cross_consent = given or _cross_ref(chat, citizen)
        consent = cross_consent
        if not consent:
            if not _exact(chat, agent, ask):
                await _say(city, citizen, ask)
            elif heard.get("consent") == "no":
                await _say(
                    city,
                    citizen,
                    f"cancel offer_id={offer_id} reason=rule_p2_no_consent {_phrase(chat, 'no_consent')}",
                )
            else:
                await _tell(city, chat, citizen, _phrase(chat, "redirect"), again=False)
            return
    if not consent:
        ask = f"consent purpose=booking offer_id={offer_id} {_phrase(chat, 'consent')}"
        again = f"consent purpose=booking offer_id={offer_id} {_phrase(chat, 'consent_code')}"
        if not _exact(chat, agent, ask):
            await _say(city, citizen, ask)
        elif heard.get("consent") == "no":
            # An explicit no. Say so and move on.
            await _say(
                city,
                citizen,
                f"cancel offer_id={offer_id} reason=consent_missing {_phrase(chat, 'no_consent')}",
            )
        elif not _exact(chat, agent, again):
            # A yes without the code, or a question: we cannot commit without consent_ref, so
            # we ask for the code once. Never read it as a no.
            await _say(city, citizen, again)
        else:
            await _tell(city, chat, citizen, _phrase(chat, "redirect"), again=False)
        return
    if _we(chat, agent, f"slot_id={slot}", to=facility, word="confirm"):
        return
    theirs, request = _partner_refs(chat, agent, facility, slot)
    booking_consent = consent
    if facility not in _OUR_FACILITIES:
        # As the teams agreed: the booking consent in consent_ref, the cross-ASST one beside it.
        booking_consent = _booking_ref(chat, citizen, offer_id) or consent
    parts = [
        "confirm",
        f"offer_id={theirs or offer_id}",
        f"slot_id={slot}",
        f"citizen={citizen}",
        f"consent_ref={booking_consent}",
        f"facility={facility}",
    ]
    if facility not in _OUR_FACILITIES:
        parts.append(f"cross_asst_consent_ref={consent}")
    if request:
        parts.append(f"request_id={request}")
    if offer.get("prescription_number"):
        parts.append(f"prescription_number={offer['prescription_number']}")
    # The citizen's language, so the facility writes to them in it. It leaves only now, with
    # the citizen's consent in hand (for a partner ASST, the cross-ASST one).
    if re.fullmatch(r"[a-z]{2}", _lang(chat)):
        parts.append(f"lang={_lang(chat)}")
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
        return
    # The slot is being held for this citizen: a cached page (_PAGES) still shows it free.
    _GONE.add(slot)


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
    told = _sent(message.need_id or message.msg_id).said
    if f"receipt:{booking_id}" in told:
        return
    told.add(f"receipt:{booking_id}")
    phrases = _phrases(chat)
    tail = [phrases["receipt"]]
    booked = _booked_offer(chat, city.agent_id, message.sender, citizen)
    when = _when_words(booked.get("agenda_date", ""), _talk(chat))
    if when:
        what = _PROCEDURES.get(booked.get("procedure", ""), {}).get(_talk(chat))
        tail.append(phrases["booked"].format(what=f"{what}, {when}" if what else when))
    tail.append(phrases["bring"])
    if message.sender in _WHEELCHAIR:
        tail.append(phrases["wheelchair"])
    tail.append(phrases["more"])
    await _say(
        city,
        citizen,
        f"booking.receipt booking_id={booking_id} ledger_ref={ledger_ref} {' '.join(tail)}",
    )


_NO_OFFER = frozenset({"", "none", "null", "nil", "-", "n/a", "na", "0"})


async def _partner_cancel(city: City, message: Message, chat: list[Message]) -> None:
    offer_id = fields_of(message.text).get("offer_id", "")
    if offer_id.strip().lower() in _NO_OFFER:
        offer_id = ""
    citizen = _citizen(chat)
    if not offer_id and citizen:
        # A cancel naming the booking only: our offer behind it, from the receipt we forwarded.
        offer_id = _offer_of_booking(chat, city.agent_id, citizen, fields_of(message.text).get("booking_id", ""))
    # P1: the Ledger refused the commit because the prescription expired. The GP renews it.
    expired = "PRESCRIPTION_EXPIRED" in message.text.upper()
    told = _sent(message.need_id or message.msg_id).cancelled
    if fields_of(message.text).get("reason", "").upper() == "DUPLICATE":
        # The Ledger refused a second commit on a prescription it has already booked (journey
        # 0112: the facility sent this with its receipt, and we offered six more slots to a
        # booked citizen). The visit stands: nothing to tell, nothing to offer. A cancel with
        # any other reason is a real one (from level 6 the city cancels the first booking).
        # With no receipt in the case after a short wait, the booking is not ours to trust:
        # then this is handled as any other cancel.
        def booked(rows: list[Message]) -> bool:
            return any(
                row.sender.startswith("agent:") and first_word(row.text) == "booking.receipt"
                for row in rows
            )

        if booked(chat) or booked(await _until(city, message.need_id, chat, booked, tries=8)):
            return
    if offer_id and not _our_offer(chat, city.agent_id, offer_id):
        offer_id = ""  # not one of our offers: nothing the citizen holds was cancelled
    if offer_id and fields_of(message.text).get("reason", "").upper() in _GONE_CODES:
        # Taken by someone else, or its hold expired: no citizen of ours gets it again.
        _GONE.add(_our_offer(chat, city.agent_id, offer_id).get("slot_id", ""))
    if not offer_id and not fields_of(message.text).get("booking_id"):
        # Names neither an offer of ours nor a booking: a partner's "no availability" goes to
        # the ring, anything else is not ours to tell the citizen about.
        if message.sender not in _OUR_FACILITIES and fields_of(message.text).get("request_id"):
            await _advance(city, message, chat, replacing=False, partner=True)
        return
    if (
        offer_id
        and citizen
        and offer_id not in told
        and not _we(chat, city.agent_id, f"offer_id={offer_id}", to=citizen, word="cancel")
    ):
        told.add(offer_id)
        reason, key = ("prescription_expired", "expired") if expired else ("facility_cancelled", "cancelled")
        await _say(city, citizen, f"cancel offer_id={offer_id} reason={reason} {_phrase(chat, key)}")
    if expired and citizen:
        await _renewal(city, chat, citizen, again=False)
        return
    await _advance(city, message, chat, replacing=True)


# --- other ASSTs (the ring) --------------------------------------------------------------------
# Our own facilities, from the registry (team/world). Anyone else that answers a search is a
# partner ASST's facility.
_OUR_FACILITIES = frozenset({"agent:m6py14a1ej77", "agent:h6krzxx6es83", "agent:yg8debxe3x0p"})
# From the registry (team/world): our hospital is wheelchair accessible. Said only for it.
_WHEELCHAIR = frozenset({"agent:m6py14a1ej77"})
_SERVICES = {
    "VIS-FIS": "hospital.physiatry",
    "VIS-CARD": "specialist_care.cardiology",
    "CTRL-CARD": "specialist_care.cardiology",
    "CONS-GIN": "family_counseling.gynecology",
}
# The rare language of each ASST's third hospital doctor, as the teams shared it on Discord
# (asst name in the doctor id -> language). Unknown ASSTs: only the offer's own doctor_lang.
_RARE_BY_ASST = {
    "gorla": "sq", "giambellino": "ka", "nord": "ur", "lambrate": "ja", "greco": "si",
    "bicocca": "ti", "corvetto": "fr", "brera": "fa", "isola": "tl", "ortica": "wo",
    "forlanini": "de",
}
# Facilities the other teams published on Discord (table of 21/11, against impostor agents):
# facility -> its ASST's rare language ("" = not published, or not a physiatry doctor). Only
# these are asked: an agent the teams did not publish may be an impostor, and a false
# attribute in an offer we pass on costs -2.
_KNOWN_PARTNERS = {
    "hospital.physiatry": {
        "agent:h6r1v45esk77": "ka",  # Giambellino, RED-AGENT
        "agent:nanpr5mgm2py": "ja",  # Lambrate, LTF-JECATT
        "agent:gpr1zqe343gb": "ur",  # Nord, agentic-paguri
        "agent:xtj1dk9x3qej": "si",  # Greco, tartufi-pazzi
        "agent:bb01ggxymm05": "ti",  # Bicocca, p4radigm
        "agent:tmt0bcv0st1f": "",    # Lorenteggio, RASPBERRY
        "agent:gxq3vzgt3h5t": "",    # Bovisa, prompt-soccorso
    },
    "specialist_care.cardiology": {
        "agent:hnpe58epeyh1": "",  # Giambellino
        "agent:pky3vqyqbepa": "",  # Lambrate
        "agent:zand0f57ec1j": "",  # Nord
        "agent:q18es2d82f8r": "",  # Greco
        "agent:sh35cbsy287d": "",  # Bicocca
        "agent:n9bf9y6670hp": "",  # Lorenteggio
        "agent:tty8erdj6t41": "",  # Bovisa
        "agent:etpwrfmfa8hm": "",  # Isola, Diuo
    },
    "family_counseling.gynecology": {
        "agent:bzs050xacs1m": "",  # Giambellino
        "agent:atp772whjkgd": "",  # Lambrate
        "agent:ccjzqm7yekwx": "",  # Nord
        "agent:pxvd2kk2yx5j": "",  # Greco
        "agent:nkh372rd16yq": "",  # Bicocca
        "agent:mcppmrbjvxag": "",  # Lorenteggio
        "agent:txar5994gbkj": "",  # Bovisa
        "agent:mkfzagfe8jtt": "",  # Isola
    },
}
_MAX_PARTNERS = 3
# x _PULL_PAUSE: one minute. The partners promise 10 seconds, but their own Ledger read takes
# about 12 and none answered within our old 12 (39 rings, one offer). The citizen waits 16 minutes.
_PARTNER_TRIES = 240
# Each try is also a history read (~0.22 s live): 240 tries took ~113 s (live: "asking the
# partners" -> offer, median 98 s) for one partner offer in 39 rings. A partner answering later
# still reaches the citizen through _partner_answer.
_PARTNER_WAIT = 30.0
_PARTNER_ROUNDS = 3


def _asked_partners(chat: list[Message], agent: str) -> dict[str, str]:
    """Partner facility -> the request_id of our ``need.request`` to it."""

    asked: dict[str, str] = {}
    for row in chat:
        if row.sender == agent and row.recipient.startswith("agent:") and first_word(row.text) == "need.request":
            request = fields_of(row.text).get("request_id", "")
            if request:
                asked[row.recipient] = request
    return asked


def _partner_rounds(chat: list[Message], agent: str) -> list[str]:
    """Our need.request rounds to the partners, oldest first: the msg_id of each round's first
    request (one round = one request_id)."""

    seen: dict[str, str] = {}
    for row in chat:
        if row.sender == agent and row.recipient.startswith("agent:") and first_word(row.text) == "need.request":
            request = fields_of(row.text).get("request_id", "")
            if request and request not in seen:
                seen[request] = row.msg_id
    return list(seen.values())


def _refused_since(chat: list[Message], citizen: str, mark: str) -> bool:
    """Whether the citizen turned an offer down (language, deadline, time) after our message ``mark``."""

    after = False
    for row in chat:
        if row.msg_id == mark:
            after = True
        elif after and row.sender == citizen and "cancel" in words(row.text):
            if fields_of(row.text).get("reason", "").lower() in {"lang", "language", "deadline", "priority", "late", "time"}:
                return True
    return False


def _partner_rows(chat: list[Message], agent: str) -> list[Message]:
    """The partners' answers to our requests (matched on sender and request_id)."""

    asked = _asked_partners(chat, agent)
    return [
        row
        for row in chat
        if row.sender in asked and f"request_id={asked[row.sender]}" in row.text.replace(",", " ").split()
    ]


def _partner_slots(chat: list[Message], agent: str) -> list[dict[str, str]]:
    """Slots the partners offered. ``facility`` is always the agent that answered: an offer
    cannot point our confirm at someone else."""

    found: list[dict[str, str]] = []
    for row in _partner_rows(chat, agent):
        if "offer" not in words(row.text):
            continue
        for offer in _offers_in(row.text):
            slot = dict(offer)
            for key, alias in (("at", "agenda_date"), ("day", "agenda_day")):
                if key not in slot and alias in slot:
                    slot[key] = slot[alias]
            if not slot.get("slot_id") or not slot.get("doctor_id") or _when(slot.get("at", "")) is None:
                continue
            slot["day"] = _IT_DAYS.get(slot.get("day", "").lower()[:3], slot.get("day", "").lower()[:3])
            slot["facility"] = row.sender
            found.append(slot)
    return found


def _partner_speaks(slot: dict[str, str], lang: str) -> bool:
    """Whether the partner's doctor speaks ``lang``: the offer's own ``doctor_lang`` first, then
    the shared table of rare languages; the common ones (it, en) follow the roster every ASST has."""

    if lang == "it":
        return True
    stated = {part.strip().lower() for part in re.split(r"[/,|;]", slot.get("doctor_lang", "")) if part.strip()}
    if stated:
        return lang in stated
    doctor = slot.get("doctor_id", "")
    spec = _DOCTORS.get(doctor.rsplit("-", 1)[-1])
    if spec is None:
        return False
    if lang in spec[0]:
        return True
    if "*" not in spec[0]:
        return False
    known = next((rare for name, rare in _RARE_BY_ASST.items() if f"-{name}-" in f"-{doctor}-"), None)
    if known is not None:
        return known == lang
    # An ASST that did not publish its rare language, and a language no published ASST has
    # (journey 0141, Korean: S had five slots and we offered none): by elimination its rare
    # doctor may be the one. Our offer states no language, so a miss is a refusal, not a claim.
    return lang not in _RARE_BY_ASST.values()


def _partner_fit(
    chat: list[Message],
    agent: str,
    pick: dict[str, object],
    taken: set[str],
    refused_doctors: set[str],
) -> list[dict[str, str]]:
    """Partner slots the citizen can accept, best first (same rules as our own)."""

    slots = [
        slot
        for slot in _partner_slots(chat, agent)
        if slot["slot_id"] not in taken
        and slot["doctor_id"] not in refused_doctors
        and _partner_speaks(slot, str(pick["lang"]))
        and not _down(slot["facility"])
    ]
    by_id = {slot["slot_id"]: slot for slot in slots}
    lines = [
        " ".join(f"{key}={value}" for key, value in slot.items() if key in _SLOT_KEYS and value)
        for slot in slots
    ]
    return [by_id[slot["slot_id"]] | slot for slot in fitting(lines, **pick) if slot.get("slot_id") in by_id]


_SLOT_KEYS = ("slot_id", "doctor_id", "facility", "procedure", "at", "day", "free", "urgent_reserve")


def _partner_refs(chat: list[Message], agent: str, facility: str, slot_id: str) -> tuple[str, str]:
    """The partner's own offer_id for ``slot_id`` and our request_id to it ("" for our own)."""

    if facility in _OUR_FACILITIES:
        return "", ""
    request = _asked_partners(chat, agent).get(facility, "")
    for slot in _partner_slots(chat, agent):
        if slot["facility"] == facility and slot["slot_id"] == slot_id:
            return slot.get("offer_id", ""), request
    return "", request


def _booking_ref(chat: list[Message], citizen: str, offer_id: str) -> str:
    """The consent reference the citizen gave when choosing ``offer_id`` (not the cross-ASST one)."""

    found = ""
    for row in chat:
        if row.sender == citizen:
            fields = _fields(row)
            if fields.get("offer_id") == offer_id and fields.get("consent_ref") and fields.get("purpose") != "cross_asst":
                found = fields["consent_ref"]
    return found


def _cross_ref(chat: list[Message], citizen: str) -> str:
    """The citizen's latest consent reference with purpose cross_asst."""

    found = ""
    for row in chat:
        if row.sender == citizen:
            fields = _fields(row)
            if fields.get("purpose") == "cross_asst" and fields.get("consent_ref"):
                found = fields["consent_ref"]
    return found


async def _ask_partners(
    city: City, chat: list[Message], citizen: str, procedure: str, priority: str | None, key: str,
    lang: str = "it",
) -> tuple[list[Message], bool]:
    """Once per need: the search criteria, and nothing about the citizen, to the facilities of
    the other ASSTs that offer the service. Then a short wait for their answers."""

    agent = city.agent_id
    service = _SERVICES.get(procedure)
    if not service:
        return chat, False
    rounds = _partner_rounds(chat, agent)
    if not rounds or (len(rounds) < _PARTNER_ROUNDS and _refused_since(chat, citizen, rounds[-1])):
        try:
            peers = await city.find(service)
        except Exception:  # noqa: BLE001 - the Index is down: the published partners remain
            peers = []
        ranked = sorted(
            (
                peer
                for peer in peers
                if peer.agent_id not in _OUR_FACILITIES
                and peer.agent_id.startswith("agent:")
                and peer.role in ("", "facility")
                and (not peer.capabilities or service in peer.capabilities)
            ),
            key=lambda peer: -float(peer.reputation or 0),
        )
        known = _KNOWN_PARTNERS.get(service, {})
        # Only the published partners: an agent outside the teams' list may be an impostor.
        found = [peer.agent_id for peer in ranked if peer.agent_id in known]
        # The ASST whose doctor speaks the citizen's rare language first, then the Index's best,
        # then the published ones the Index did not return. The language itself is never sent.
        first = [agent_id for agent_id, rare in known.items() if rare and rare == lang]
        # Every published partner is asked: at level 4 the valid slots are in one or few ASSTs
        # (journeys 0134, 0164: English speakers, three partners asked, none had room).
        limit = max(_MAX_PARTNERS, len(known))
        if not first and lang not in ("it", "en"):
            # Nobody published this language: the ASSTs that published none come first.
            first = [agent_id for agent_id, rare in known.items() if not rare]
        partners = list(dict.fromkeys([*first, *found, *known]))
        # A new round after the citizen turned our offers down: the partners' agendas move.
        seed = f"{agent}/{key}" + (f"/{len(rounds)}" if rounds else "")
        request = "x-" + hashlib.sha256(seed.encode()).hexdigest()[:12]
        text = f"need.request service_type={service} procedure={procedure}"
        if priority:
            text += f" priority={priority}"
        text += (
            f" request_id={request} Buongiorno, siamo il centro prenotazioni dell'ASST Gorla: "
            "cerchiamo disponibilità per questa prestazione. Nessun dato del cittadino in questa "
            "richiesta: ve li mandiamo solo dopo il suo consenso. Grazie!"
        )
        reached = 0
        for partner in partners[:limit]:
            if _down(partner):
                continue
            try:
                await city.say(partner, text)
                reached += 1
            except Exception:  # noqa: BLE001 - CapError or an httpx timeout: the next partner still gets it
                _DOWN[partner] = time.monotonic()  # never the same message again
        if not reached:
            return chat, False
        notice = _phrase(chat, "partners")
        if not _exact(chat, agent, notice):
            await _say(city, citizen, notice)

    def answered(rows: list[Message]) -> bool:
        asked = _asked_partners(rows, agent)
        heard = {row.sender for row in _partner_rows(rows, agent)}
        # One partner's slots are enough, unless the citizen needs a language: then a slot whose
        # doctor speaks it, or every partner's answer.
        slots = [slot for slot in _partner_slots(rows, agent) if _partner_speaks(slot, lang)]
        return bool(slots) or (bool(asked) and set(asked) <= heard)

    if not answered(chat):
        # The citizen chooses from our first offers message (R:95) and waits minutes for it:
        # a partner's answer is worth some seconds more than a Ledger read.
        chat = await _until(city, chat[-1].need_id if chat else None, chat, answered, _PARTNER_TRIES,
                            deadline=time.monotonic() + _PARTNER_WAIT)
    return chat, True


async def _try_partners(
    city: City,
    message: Message,
    chat: list[Message],
    citizen: str,
    number: str,
    priority: str | None,
    procedure: str,
    pick: dict[str, object],
    taken: set[str],
    refused_doctors: set[str],
    sent: _Sent,
    replacing: bool,
) -> bool:
    """Ask the ring and offer what fits. False: nothing from the partners (yet), go on alone."""

    key = message.need_id or message.msg_id
    chat, asked = await _ask_partners(city, chat, citizen, procedure, priority, key, str(pick["lang"]))
    if not asked:
        return False
    slots = _partner_fit(chat, city.agent_id, pick, taken, refused_doctors)
    if not slots:
        return False
    await _offer(
        city, citizen, _spread(slots, _MAX_OFFERS, citizen), number, priority, sent, replacing, chat, "partner_offers"
    )
    return True


async def _partner_answer(city: City, message: Message, chat: list[Message], key: str) -> None:
    """A partner's answer that arrived after our turn stopped waiting: offer its slots once,
    unless the citizen is already being booked or already holds partner offers."""

    agent = city.agent_id
    citizen = _citizen(chat)
    if citizen is None or _booking_stage(chat, agent, citizen):
        return
    sent = _sent(key)
    offered = _offered(chat, agent) | sent.offers
    if any(slot["slot_id"] in offered for slot in _partner_slots(chat, agent)):
        return
    number = _number(chat)
    reply = _prescription(chat, number) if number else None
    _round_trip(chat, agent)
    now = _now(chat)
    if not number or reply is None or not reply.get("procedure") or now is None:
        return
    rules = _rules(chat, now)
    pick = {
        "now": now,
        "born": _born(chat),
        "priority": reply.get("priority"),
        "lang": _lang(chat),
        "gender": _gender(chat),
        "days": _days(chat),
        "procedure": reply["procedure"],
        "reserve": rules is None or "P3" in rules,
    }
    refused_slots, refused_doctors = _refused(chat, agent)
    slots = _partner_fit(chat, agent, pick, offered | refused_slots | _GONE, refused_doctors)
    if slots:
        await _offer(
            city, citizen, _fresh_first(slots, citizen)[:_MAX_OFFERS], number, reply.get("priority"), sent,
            False, chat, "partner_offers",
        )


# --- conversation ------------------------------------------------------------------------------

_WORRIED = re.compile(
    r"preoccup|ansia|ansios|apprension|paura|spaventat|agitat|angosci|"
    r"worried|anxious|scared|afraid|nervous|concerned"
)
_PROCEDURES = {
    "VIS-FIS": {"it": "visita fisiatrica", "en": "physiatry visit"},
    "VIS-CARD": {"it": "visita cardiologica", "en": "cardiology visit"},
    "CTRL-CARD": {"it": "controllo cardiologico", "en": "cardiology follow-up"},
    "CONS-GIN": {"it": "consulenza ginecologica", "en": "gynaecology consultation"},
}
_DAY_WORDS = {
    "it": ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"),
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
}
_MONTH_WORDS = {
    "it": ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
           "settembre", "ottobre", "novembre", "dicembre"),
    "en": ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"),
}


def _phrases(chat: list[Message]) -> dict[str, str]:
    return _PHRASES[_talk(chat)]


# A few function words of Italian prose: a citizen who writes them reads Italian.
_ITALIAN_WORDS = re.compile(
    r"\b(?:buongiorno|buonasera|salve|grazie|vorrei|sono|ho|mi|della|per favore|medico|"
    r"visita|prenotare|dolore|schiena|ginocchio|che|non|una?)\b",
    re.IGNORECASE,
)


def _talk(chat: list[Message]) -> str:
    """The language we write in: "it" or "en", the two we have phrases for. ``lang=`` is the
    language the citizen needs the doctor to speak; a citizen asking for Korean or Spanish
    writes to us in English (journeys 0141, 0172), so anything but Italian is answered in
    English, unless their own words are Italian."""

    lang = _lang(chat)
    if lang in ("it", "en"):
        return lang
    text = " ".join(row.text or "" for row in chat if row.sender.startswith("citizen:"))
    return "it" if len(_ITALIAN_WORDS.findall(text)) >= 3 else "en"


def _when_words(at: str, lang: str) -> str:
    """A Ledger timestamp in plain words, in the agenda's own clock: "martedì 10 novembre alle
    10:30". Empty when the timestamp cannot be read: we never guess a date."""

    when = _when(at)
    if when is None:
        return ""
    lang = "en" if lang == "en" else "it"
    day, month = _DAY_WORDS[lang][when.weekday()], _MONTH_WORDS[lang][when.month - 1]
    joint = "at" if lang == "en" else "alle"
    return f"{day} {when.day} {month} {joint} {when:%H:%M}"


def _refused(chat: list[Message], agent: str) -> tuple[set[str], set[str]]:
    """Slots the citizen turned down, and doctors turned down for who they are (language or
    gender): another slot of the same doctor would be refused again."""

    slots: set[str] = set()
    doctors: set[str] = set()
    for row in chat:
        if not row.sender.startswith("citizen:") or "cancel" not in words(row.text):
            continue
        fields = fields_of(row.text)
        offer = _our_offer(chat, agent, fields.get("offer_id", ""))
        if not offer.get("slot_id"):
            continue
        slots.add(offer["slot_id"])
        if fields.get("reason", "").lower() in {"lang", "language", "gender", "doctor"} and offer.get("doctor_id"):
            doctors.add(offer["doctor_id"])
    return slots, doctors


def _refused_for(chat: list[Message], reasons: set[str]) -> bool:
    """Whether the citizen turned one of our offers down for one of these reasons."""

    return any(
        row.sender.startswith("citizen:") and "cancel" in words(row.text)
        and fields_of(row.text).get("reason", "").lower() in reasons
        for row in chat
    )


def _offer_source(chat: list[Message], agent: str, offer_id: str) -> str:
    """The msg_id of our message that carried ``offer_id``."""

    for row in chat:
        if row.sender == agent and "offer" in words(row.text):
            if any(offer.get("offer_id") == offer_id for offer in _offers_in(row.text)):
                return row.msg_id
    return ""


def _names_ours(chat: list[Message], agent: str, fields: dict[str, str]) -> bool:
    """Whether a cancel names one of OUR offers or a booking made from one: anything else from
    the city (a conformance probe, another case) is not ours to act on."""

    citizen = _citizen(chat)
    offer_id = fields.get("offer_id", "")
    if offer_id and _our_offer(chat, agent, offer_id):
        return True
    return bool(citizen and _offer_of_booking(chat, agent, citizen, fields.get("booking_id", "")))


def _offer_of_booking(chat: list[Message], agent: str, citizen: str, booking_id: str) -> str:
    """The offer_id of ours that became ``booking_id`` ("" if unknown)."""

    if not booking_id:
        return ""
    for row in chat:
        if row.sender.startswith("agent:") and first_word(row.text) == "booking.receipt" and (
            fields_of(row.text).get("booking_id") == booking_id
        ):
            return _booked_offer(chat, agent, row.sender, citizen).get("offer_id", "")
    return ""


def _booked_offer(chat: list[Message], agent: str, facility: str, citizen: str) -> dict[str, str]:
    """Our offer behind the latest ``confirm`` we sent this facility for the citizen."""

    offer_id = ""
    for row in chat:
        if row.sender == agent and row.recipient == facility and first_word(row.text) == "confirm":
            fields = fields_of(row.text)
            if fields.get("citizen") == citizen:
                # A partner's confirm carries the partner's offer_id: ours is by slot.
                offer_id = f"of-{fields['slot_id']}" if fields.get("slot_id") else fields.get("offer_id", "")
    return _our_offer(chat, agent, offer_id) if offer_id else {}


async def _citizen_cancel(city: City, message: Message, chat: list[Message]) -> None:
    """The citizen turned one of our offers down. While another offer of ours is still open the
    choice is theirs (a ``confirm`` usually follows); with none left we look again, leaving out
    what they refused."""

    agent, citizen = city.agent_id, message.sender
    if _booking_stage(chat, agent, citizen):
        return  # already booking another offer for them
    refused, doctors = _refused(chat, agent)
    for slot in refused:  # free again for the other citizens
        if _OFFERED.get(slot, ("",))[0] == citizen:
            del _OFFERED[slot]
    reason = fields_of(message.text).get("reason", "").lower()
    live = _live_offers(chat, agent, citizen)
    if live and all(offer.get("offer_id") != _fields(message).get("offer_id") for offer in live):
        # One cancel per offer, all at once (21/11): the first turn that read them all already
        # answered with new offers. This one refuses an offer they replaced.
        return
    open_offers = [
        offer
        for offer in live
        if offer.get("slot_id") not in refused and offer.get("doctor_id") not in doctors
    ]
    # Refused for who the doctor is: the other offers may fail the same way, so we look again.
    if open_offers and reason not in {"lang", "language", "gender", "doctor"}:
        text = _phrase(chat, "dropped")
        told = _sent(message.need_id or message.msg_id).said
        if text not in told and not _exact(chat, agent, text):
            told.add(text)
            await _say(city, citizen, text)
        return
    await _advance(city, message, chat, replacing=True)


def _booking_stage(chat: list[Message], agent: str, citizen: str) -> str:
    """"flight" while our confirm is with the facility, "done" once the receipt reached the
    citizen, "" otherwise (also after a cancel: then we are offering again)."""

    stage = ""
    for row in chat:
        if row.sender != agent:
            continue
        head = first_word(row.text)
        if head == "confirm" and f"citizen={citizen}" in row.text.split():
            stage = "flight"
        elif row.recipient == citizen and head == "booking.receipt":
            stage = "done"
        elif row.recipient == citizen and head in ("cancel", "offer"):
            stage = ""
    return stage


# --- slot selection ----------------------------------------------------------------------------


# The ASST's rare language, from the registry (team/world): asst-gorla-h3 speaks it and sq.
_RARE_LANG = "sq"


def _speaks(doctor_id: str, lang: str) -> bool:
    spec = _DOCTORS.get(doctor_id.rsplit("-", 1)[-1])
    if spec is None:
        return False
    if lang in spec[0]:
        return True
    return "*" in spec[0] and lang == _RARE_LANG


def _spread(slots: list[dict[str, str]], limit: int, citizen: str = "") -> list[dict[str, str]]:
    """Up to ``limit`` slots, one per day first and then one per doctor, in the order given.
    The citizen picks, among our offers, the one that meets what they did not tell us: the 15
    bookings in S outside S_pref were all Monday to Wednesday, their hidden days Thursday and
    Friday, and we never offered one of those. Slots another citizen holds come only after
    every other one (_shared)."""

    picked: list[dict[str, str]] = []
    for pool in ([slot for slot in slots if not _shared(slot, citizen)], slots):
        for key in ("day", "doctor_id", ""):
            seen = {slot.get(key) for slot in picked} if key else set()
            for slot in pool:
                if len(picked) >= limit:
                    return picked
                if slot in picked or (key and slot.get(key) in seen):
                    continue
                picked.append(slot)
                seen.add(slot.get(key))
    return picked


_BORN_MARGIN = timedelta(hours=1)  # agenda time: one real minute before our first sight of the case


def _born(chat: list[Message]) -> datetime | None:
    """The agenda time at which the case started (its first row), a little earlier to be safe.
    Same clock as _now: a regenerated case (same need, half an hour later) keeps its birth."""

    _read_clock(chat)
    stamps = [at for at in (_when(row.sent_at or "") for row in chat) if at is not None]
    born = _agenda_at(min(stamps)) if stamps else None
    return None if born is None else born - _BORN_MARGIN


def _past_window(lines: list[str], now: datetime, priority: str | None) -> bool:
    """Whether this agenda page reaches past the priority deadline (B and D only)."""

    days = _DEADLINE_DAYS.get(priority or "")
    if not days:
        return False
    horizon = now + timedelta(days=days)
    return any((at := _when(_slot(line).get("at", ""))) is not None and at >= horizon for line in lines)


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
    born: datetime | None = None,
) -> list[dict[str, str]]:
    """The answer set S: future, free, in the priority window, outside the urgent reserve when
    ``reserve``. S_pref first (doctor speaks ``lang``, is ``gender``, on one of ``days``), then
    the rest of S, earliest first. ``relaxed`` drops the priority window (S is empty)."""

    if relaxed or priority == "P":
        horizon: datetime | None = None
    elif priority == "U":
        horizon = (born or now).replace(hour=23, minute=59, second=59, microsecond=0)
    else:
        # The deadline runs from the need's birth, not from now: the judge's S is computed when
        # the need is born, and a slow chain eats the window (journey 0001340002: refused for
        # the deadline a slot that was inside ours).
        horizon = (born or now) + timedelta(days=_DEADLINE_DAYS.get(priority or "", 14))
    earliest = now + _lead()
    chosen: list[tuple[bool, datetime, str, dict[str, str]]] = []
    for line in lines:
        slot = _slot(line)
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


_GP = "agent:ck9w60ejwnvw"


def _handed(chat: list[Message], number: str) -> dict[str, str] | None:
    """Procedure and priority from our GP's handoff of ``number`` (it wrote it on the Ledger just
    before), as an issued prescription; None without one."""

    for row in reversed(chat):
        if row.sender == _GP and _from_gp(row):
            fields = _fields(row)
            if fields.get("prescription_number") == number and fields.get("procedure") and fields.get("priority"):
                return {"procedure": fields["procedure"], "priority": fields["priority"], "state": "issued"}
    return None


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
        msg_id = await city.say(to, text) or "sent"
    except Exception as exc:  # noqa: BLE001 - CapError, and the Relay's httpx timeouts (ReadTimeout):
        # out of the brain the turn goes back retryable, the Relay delivers it again and we send twice.
        tools.said(to, text, "", error=getattr(exc, "code", None) or type(exc).__name__)
        return ""
    tools.said(to, text, msg_id)
    if to.startswith("citizen:") and city.inbound is not None:
        # The citizen heard from us: no replacement is owed any more.
        owed = _SENT.get(city.inbound.need_id or city.inbound.msg_id)
        if owed is not None:
            owed.pending = False
    return msg_id


def _ledger(chat: list[Message], head: str) -> dict[str, str] | None:
    found: dict[str, str] | None = None
    for row in chat:
        if row.sender == LEDGER and first_word(row.text) == head:
            found = fields_of(row.text)
    if found is not None and head == "agenda" and "agenda_now" not in found:
        found["agenda_now"] = found.get("agenda_date", "")
    return found


def _world_page(chat: list[Message], page: int) -> str | None:
    """The latest answer for a page, in either Ledger world format."""
    for row in reversed(chat):
        head = first_word(row.text)
        if (
            row.sender == LEDGER and _page(row.text)[0] == page
            and (head == "slots" or head == "world" and (
                "slots" in fields_of(row.text.split("\n", 1)[0]) or "slot_id=" in row.text
            ))
        ):
            return row.text
    return None


def _keep_pages(chat: list[Message]) -> None:
    """Every world page in ``chat`` goes to _PAGES under the procedure its slot lines name (an
    empty page: the one our latest world ask named), unless _PAGES holds a later Ledger answer."""

    asked = ""
    for row in chat:
        if row.recipient == LEDGER and first_word(row.text) == "world":
            asked = fields_of(row.text).get("procedure", "")
            continue
        page = _page(row.text)[0]
        if _world_page([row], page) is None:
            continue
        named = {_slot(line).get("procedure") for line in row.text.splitlines() if "slot_id=" in line} - {None, ""}
        procedure = named.pop() if len(named) == 1 else "" if named else asked
        if not procedure:
            continue
        at, old = _when(row.sent_at or ""), _PAGES.get((procedure, page))
        if old is None or (at is not None and (old[1] is None or at > old[1])):
            _PAGES[(procedure, page)] = (time.monotonic(), at, row.text)


def _fresh_page(procedure: str, page: int) -> str | None:
    hit = _PAGES.get((procedure, page))
    return hit[2] if hit is not None and time.monotonic() - hit[0] < _PAGE_FRESH else None


def _now(chat: list[Message]) -> datetime | None:
    """Agenda time now, never the bare reading: the Ledger answers minutes late and a reading
    from an earlier turn ages too (21/11: a reading 26 minutes old, slots offered 13 agenda
    hours in the past). The freshest reading, this case's or any other's, plus the real time
    since it was taken."""

    _read_clock(chat)
    return _agenda_at(datetime.now(timezone.utc))


def _read_clock(chat: list[Message]) -> None:
    """Keep in _CLOCK the agenda answer with the latest agenda_now, with its row's sent_at."""

    for row in chat:
        if row.sender != LEDGER or first_word(row.text) != "agenda":
            continue
        found = fields_of(row.text)
        found.setdefault("agenda_now", found.get("agenda_date", ""))
        new, old = _when(found["agenda_now"]), _when(_CLOCK.get("agenda_now", ""))
        if new is not None and (old is None or new >= old):
            _CLOCK.clear()
            _CLOCK.update(found, sent_at=row.sent_at or "")


def _agenda_at(real: datetime) -> datetime | None:
    """The agenda time at the real instant ``real``: ``epoch_agenda`` + (``real`` - ``epoch_real``)
    x ``rate``, weekdays only (no agenda weekends, R:80). Without the epoch fields, the reading's
    own agenda_now and sent_at; paused, or with no time to start from, the reading itself."""

    if _CLOCK.get("epoch_real") and _CLOCK.get("epoch_agenda"):
        base, start = _when(_CLOCK["epoch_agenda"]), _when(_CLOCK["epoch_real"])
    else:
        base, start = _when(_CLOCK.get("agenda_now", "")), _when(_CLOCK.get("sent_at", ""))
    if base is None or start is None or _CLOCK.get("paused") == "true":
        return _when(_CLOCK.get("agenda_now", ""))
    monday = (base - timedelta(days=base.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    week = timedelta(days=5)
    weeks, rest = divmod(min(base - monday, week) + (real - start) * _rate(), week)
    return monday + weeks * timedelta(days=7) + rest


def _rate() -> float:
    try:
        return float(_CLOCK.get("rate") or 60.0)
    except ValueError:
        return 60.0


def _round_trip(chat: list[Message], agent: str) -> None:
    """_RTT from the case history: each of our asks to the Ledger and the first answer with its
    act after it, by sent_at. The answer seen last in any case wins."""

    global _RTT
    asked: dict[str, datetime] = {}
    for row in chat:
        at = _when(row.sent_at or "")
        if at is None:
            continue
        head = first_word(row.text)
        if row.sender == agent and row.recipient == LEDGER:
            asked[head] = at
        elif row.sender == LEDGER and (ask := asked.pop("world" if head == "slots" else head, None)):
            if _RTT is None or at >= _RTT[0]:
                _RTT = (at, min(max((at - ask).total_seconds(), 0.0), _RTT_MAX))


def _lead() -> timedelta:
    """How far ahead an offered slot must be: the citizen's choice, then the hold's trip to the Ledger."""

    return _LEAD + timedelta(seconds=(_RTT[1] if _RTT else 0.0) * _rate())


def _has_agenda(chat: list[Message]) -> bool:
    agenda = _ledger(chat, "agenda")
    return _when("" if agenda is None else agenda.get("agenda_now", "")) is not None


def _prescription(chat: list[Message], number: str) -> dict[str, str] | None:
    """The Ledger's latest answer about prescription ``number``; None before it answers."""

    found: dict[str, str] | None = None
    for row in chat:
        head = first_word(row.text)
        # "refused prescription code=NOT_FOUND ...": an answer too, with no procedure.
        if row.sender == LEDGER and (head == "prescription" or head == "refused" and "prescription" in words(row.text)):
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


def _recent(chat: list[Message], sent: "_Sent") -> list[Message]:
    """Rows that answer our latest ``agenda`` ask. A history read before the ask shows neither
    the mark nor its answers: then nothing is recent, never the old agenda (journey 0099: slots
    already past were offered from a clock sixteen agenda hours old)."""

    if not sent.mark:
        return chat
    for index, row in enumerate(chat):
        if row.msg_id == sent.mark:
            return chat[index + 1 :]
    return [row for row in chat if row.msg_id not in sent.before]


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
    tries: int = _PULL_TRIES,
    deadline: float | None = None,
) -> list[Message]:
    """Re-read the case until ``ready`` or the short wait ends. The push may never arrive.
    ``deadline``: time.monotonic() after which no more reads, even with tries left."""

    fresh = chat
    for attempt in range(tries):
        if attempt and deadline is not None and time.monotonic() >= deadline:
            break
        try:
            fresh = await city.history(need_id=need_id)
        except CapError:
            fresh = chat
        _keep_pages(fresh)
        if ready(fresh):
            return fresh
        if attempt + 1 < tries:
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
    if found and any(
        row.sender == agent and first_word(row.text) == "confirm"
        and fields_of(row.text).get("offer_id") == found
        for row in chat
    ):
        return ""  # the consent arrived: the booking is on its way to the facility
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


_WEEKDAYS = {"lun": "mon", "mar": "tue", "mer": "wed", "gio": "thu", "ven": "fri"}


def _lang(chat: list[Message]) -> str:
    """The case's language: a ``lang=`` token, else what the citizen's own words show."""

    for row in chat:
        if row.sender.startswith("citizen:"):
            lang = _fields(row).get("lang", "")
            if lang:
                return lang
    for row in chat:
        if row.sender.startswith("citizen:"):
            lang = _heard(row).get("lang")
            if isinstance(lang, str) and lang:
                return lang
    # The GP's handoff carries a default when the citizen gave it none: the last resort.
    for row in chat:
        if _from_gp(row) and _fields(row).get("lang"):
            return _fields(row)["lang"]
    return "it"


def _days(chat: list[Message]) -> frozenset[str]:
    """Agenda days the citizen prefers as mon..fri (token ``days=`` or free text, latest
    message first); empty when not said."""

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
        said = _heard(row).get("days")
        if isinstance(said, frozenset) and said:
            return said
    return frozenset()


def _gender(chat: list[Message]) -> str | None:
    """Doctor gender the citizen asked for: a token first, else free text (latest first)."""

    for row in chat:
        if not row.sender.startswith("citizen:"):
            continue
        fields = _fields(row)
        raw = fields.get("gender_preference") or fields.get("gender")
        if raw in {"m", "f"}:
            return raw
    for row in reversed(chat):
        if row.sender.startswith("citizen:"):
            said = _heard(row).get("gender")
            if said in {"m", "f"}:
                return str(said)
    return None


# --- free text (level 3) -----------------------------------------------------------------------
# Rules first: they are instant and free. The team model (city.llm, city-default) only fills
# what the rules leave open, under a short timeout, and its answer is checked against closed
# lists and our own offers. It reads the citizen; it never writes a token or a fact.

_LLM_TIMEOUT = 3.0
_LANGS = frozenset("it en sq es fr de pt ro pl uk ru ar zh hi bn ur tr tl".split())
_HEARD_MAX = 4096
# msg_id -> what the model read in that citizen message (this process only; rules need no cache).
_HEARD: OrderedDict[str, dict[str, object]] = OrderedDict()
_WEEK = ("mon", "tue", "wed", "thu", "fri")
_DAY_RX = {
    "mon": r"lunedi|mondays?", "tue": r"martedi|tuesdays?", "wed": r"mercoledi|wednesdays?",
    "thu": r"giovedi|thursdays?", "fri": r"venerdi|fridays?",
}
# Right before a day: "non il venerdì", "tranne giovedì", "not on Friday" (excluded) ...
# A clause that rules its days out: "lunedì non posso", "Mondays I work", "except Friday".
_CLAUSE_NOT = re.compile(
    r"\b(?:non|no|not|never|mai|can'?t|cannot|couldn'?t|won'?t|don'?t|tranne|eccetto|escluso|esclus[oi]|"
    r"except|apart from|lavoro|lavorando|work|working|impegnat\w*|occupat\w*|busy|impossibile|unavailable)\b"
)
_CLAUSE_SPLIT = re.compile(r"[,.;:!?]+|\s+(?:ma|pero|invece|mentre|but|however|while|so)\s+")
_IT_WORDS = frozenset(
    "vorrei buongiorno buonasera grazie prenotare prenotazione ricetta visita sono che non mi ho "
    "bene preferisco giorno dottoressa dottore medico posso ciao salve avete potete numero della "
    "del gli perche anche sempre lavoro mattina pomeriggio settimana".split()
)
_EN_WORDS = frozenset(
    "i i'd i'm my please would like book booking appointment thanks thank hello hi the is to "
    "for only prefer doctor need can yes with on any morning afternoon week work".split()
)
_SQ_WORDS = frozenset(
    "pershendetje miredita faleminderit dua kam nje takim mjek mjeku vizite ju lutem flas shqip".split()
)
# A yes opens the message ("Sì, va bene"); "si" inside a sentence is the Italian reflexive.
_YES = re.compile(
    r"^\W*(?:si|ok|okay|va bene|perfetto|benissimo|confermo|accetto|prenoto|d'accordo|certo|"
    r"acconsento|yes|yeah|yep|sure|fine|great|perfect|agreed|i agree|go ahead|deal)\b"
    r"|\b(?:acconsento|do il (?:mio )?consenso|autorizzo|i agree|i consent|book it|lo prendo|la prendo)\b"
)
_NO = re.compile(
    r"\b(?:no|non|nessun\w*|rifiuto|preferisco di no|not|don'?t|do not|none|neither|refuse|"
    r"altr[oaie]|other|another|different|divers[oaie])\b"
)
# "ci penso", "let you know": not a choice yet.
_LATER = re.compile(r"ci penso|faccio sapere|ci devo pensare|non so|forse|think about|let you know|not sure|maybe")
_NAMES = r"(?:la|il|lo|l'|quell[ao]|opzione|option|numero|number|the|n\.?|#|prendo|scelgo|choose|take)"
_ORDINALS = (
    (1, r"prim[oa](?!\s+(?:possibile|pomeriggio|mattin\w*|di tutto|cosa))|first(?!\s+of all)|1st|1"),
    (2, r"second[oa](?!\s+me\b)|second|2nd|2"),
    (3, r"terz[oa]|third|3rd|3"),
)
_HOUR = re.compile(
    r"\b(?:alle|delle|dalle|ore|le|at|h)\s*(\d{1,2})(?:[:.](\d{2}))?\b|\b(\d{1,2})[:.](\d{2})\b"
)
_ANY_DAY = "|".join(_DAY_RX.values())
# "venerdì non posso", "non il venerdì", "not Friday": a day ruled out, cut before picking.
_DAY_OUT = re.compile(
    rf"(?:\b(?:non|not|no|tranne|eccetto|except)\W+(?:(?:il|la|di|on|the)\W+)?(?:{_ANY_DAY})\b)"
    rf"|(?:\b(?:quell[oa]\W+)?(?:di\W+|del\W+|on\W+)?(?:{_ANY_DAY})\W+(?:\w+\W+)?"
    r"(?:non (?:posso|riesco|va bene|mi va)|no\b|not possible|doesn'?t work|can'?t|won'?t work))"
)
# Scripts that say the language on their own when the model is slow.
_SCRIPTS = (
    ("ar", "\u0600-\u06ff"), ("ru", "\u0400-\u04ff"), ("zh", "\u4e00-\u9fff"),
    ("hi", "\u0900-\u097f"), ("bn", "\u0980-\u09ff"), ("ur", "\u0750-\u077f"),
)


def _plain(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text.replace("’", "'")).encode("ascii", "ignore")
    return " ".join(plain.decode().lower().split())


def _said_days(plain: str) -> frozenset[str] | None:
    """Weekdays wanted in free text, clause by clause: named days, minus the ones a clause rules
    out ("lunedì non posso", "except Friday"). "Da lunedì" alone is when it started."""

    wanted: set[str] = set()
    excluded: set[str] = set()
    span = re.search(
        rf"\b(?:da|dal|from)\s+({_ANY_DAY})\s+(?:a|al|to|until|fino a)\s+({_ANY_DAY})\b", plain
    )
    if span:
        first, last = (_day_of(span.group(1)), _day_of(span.group(2)))
        if first in _WEEK and last in _WEEK and _WEEK.index(first) <= _WEEK.index(last):
            wanted |= set(_WEEK[_WEEK.index(first) : _WEEK.index(last) + 1])
        plain = plain[: span.start()] + " " + plain[span.end() :]
    for clause in _CLAUSE_SPLIT.split(plain):
        clause = re.sub(rf"\b(?:da|dal|since)\s+(?:(?:il|lo|scorso|last)\s+)?(?:{_ANY_DAY})\b", " ", clause)
        named = {day for day, pattern in _DAY_RX.items() if re.search(rf"\b(?:{pattern})\b", clause)}
        if not named:
            continue
        if _CLAUSE_NOT.search(clause):
            excluded |= named
        else:
            wanted |= named
    if not wanted and not excluded:
        return None
    days = (wanted or set(_WEEK)) - excluded
    return frozenset(days) if days and len(days) < len(_WEEK) else None


def _day_of(word: str) -> str:
    return next((day for day, pattern in _DAY_RX.items() if re.fullmatch(pattern, word)), "")


# A doctor's gender asked for, not a word about the citizen ("sono una donna") or a greeting
# ("buongiorno dottoressa").
_FEMALE = re.compile(
    r"\b(?:una dottoressa|medico donna|dottore donna|donna medico|specialista donna|"
    r"(?:da|con) una donna|female (?:doctor|physician|gp|specialist)|(?:woman|lady) doctor|"
    r"a female|(?:by|with) a woman)\b"
)
_MALE = re.compile(
    r"\b(?:medico uomo|dottore uomo|uomo medico|specialista uomo|medico maschio|"
    r"(?:da|con) un uomo|male (?:doctor|physician|gp|specialist)|man doctor|a male|"
    r"(?:by|with) a man)\b"
)
_GENDER_NOT = re.compile(r"\b(?:non|no|not|don'?t|do not|never|mai|rather not|niente)\b")


def _said_gender(plain: str) -> str | None:
    found: set[str] = set()
    for clause in _CLAUSE_SPLIT.split(plain):
        female, male = bool(_FEMALE.search(clause)), bool(_MALE.search(clause))
        if female == male:
            continue
        gender = "f" if female else "m"
        if _GENDER_NOT.search(clause):  # "non voglio una dottoressa": the other one
            gender = "m" if gender == "f" else "f"
        found.add(gender)
    return found.pop() if len(found) == 1 else None


def _said_lang(text: str, plain: str) -> str | None:
    """it or en from distinctive words; None (ask the model) for anything else or a tie."""

    if any(ch.isalpha() and ord(ch) > 0x24F for ch in text):
        # Another script: the model names it; a well-known script is our answer meanwhile.
        for code, letters in _SCRIPTS:
            if re.search(f"[{letters}]", text):
                return code
        return None
    tokens = re.findall(r"[a-z']+", plain)
    it = sum(token in _IT_WORDS for token in tokens)
    en = sum(token in _EN_WORDS for token in tokens)
    if it >= 1 and en == 0 and it >= min(2, len(tokens)):
        return "it"
    if en >= 1 and it == 0 and en >= min(2, len(tokens)):
        return "en"
    if abs(it - en) >= 2:
        return "it" if it > en else "en"
    if it == 0 and en == 0 and sum(token in _SQ_WORDS for token in tokens) >= 2:
        return _RARE_LANG
    return None


def _heard(row: Message) -> dict[str, object]:
    """Days, gender and language in one citizen message: the rules, then the model's reading."""

    plain = _plain(row.text or "")
    found: dict[str, object] = {}
    days = _said_days(plain)
    if days:
        found["days"] = days
    gender = _said_gender(plain)
    if gender:
        found["gender"] = gender
    lang = _said_lang(row.text or "", plain)
    if lang:
        found["lang"] = lang
    for key, value in _HEARD.get(row.msg_id, {}).items():
        found.setdefault(key, value)
    return found


def _live_offers(chat: list[Message], agent: str, citizen: str) -> list[dict[str, str]]:
    """Our latest offers to the citizen, in the order sent, while none is confirmed yet."""

    latest: list[dict[str, str]] = []
    cancelled: set[str] = set()
    for row in chat:
        if row.sender != agent:
            continue
        head = first_word(row.text)
        if row.recipient == citizen and head == "offer":
            latest = [offer for offer in _offers_in(row.text) if offer.get("slot_id")]
        elif row.recipient == citizen and head == "cancel":
            cancelled.add(fields_of(row.text).get("offer_id", ""))
        elif head in ("confirm", "booking.receipt") and f"citizen={citizen}" in row.text.split():
            return []  # already on its way to the facility
        elif row.recipient == citizen and head == "booking.receipt":
            return []
    return [offer for offer in latest if offer.get("offer_id") not in cancelled]


def _offer_hour(offer: dict[str, str]) -> tuple[int, int] | None:
    match = re.search(r"T(\d{2}):(\d{2})", offer.get("agenda_date", ""))
    return (int(match.group(1)), int(match.group(2))) if match else None


def _pick_by_rules(plain: str, offers: list[dict[str, str]]) -> str | None:
    """The offer the citizen names by position, day or hour; None when unsure."""

    plain = _DAY_OUT.sub(" ", plain)
    if not offers or "?" in plain or _NO.search(plain) or _LATER.search(plain):
        return None  # a question, "no", "un altro", "ci penso": not a choice by rule
    sets: list[set[int]] = []
    bare = re.fullmatch(r"\W*(?:opzione|option|numero|number|n\.?|#)?\s*([123])\W*", plain)
    for number, pattern in _ORDINALS:
        named = re.search(rf"\b{_NAMES}\s*(?:{pattern})\b", plain)
        if named or (bare and int(bare.group(1)) == number):
            sets.append({number - 1} if number <= len(offers) else set())
    if re.search(r"\b(?:ultim[oa]|last)\b", plain):
        sets.append({len(offers) - 1})
    days = {day for day, pattern in _DAY_RX.items() if re.search(rf"\b(?:{pattern})\b", plain)}
    if days:
        sets.append({i for i, offer in enumerate(offers) if offer.get("agenda_day") in days})
    hours = [
        (int(m.group(1) or m.group(3)), int(m.group(2) or m.group(4) or 0))
        for m in _HOUR.finditer(plain)
    ]
    if hours:
        sets.append(
            {
                i
                for i, offer in enumerate(offers)
                if (at := _offer_hour(offer)) is not None
                and any(at[0] == h and (m == 0 or at[1] == m) for h, m in hours)
            }
        )
    if not sets:
        return offers[0]["offer_id"] if len(offers) == 1 and _YES.search(plain) else None
    chosen = set(range(len(offers)))
    for found in sets:
        chosen &= found
    return offers[chosen.pop()]["offer_id"] if len(chosen) == 1 else None


def _asked_preferences(chat: list[Message], agent: str, citizen: str) -> bool:
    """Our latest word to the citizen asks for days or gender."""

    latest = ""
    for row in chat:
        if row.sender == agent and row.recipient == citizen:
            latest = row.text
    fields = fields_of(latest)
    return first_word(latest) == "data.request" and fields.get("fields") in {"days", "gender"}


async def _hear(city: City, message: Message, chat: list[Message]) -> dict[str, object]:
    """What a citizen's free-text message means for the desk.

    ``offer_id`` (one of OUR live offers), ``consent`` (yes/no while our consent request is
    open), plus days, gender and language. Tokens win: a message with ``key=value`` tokens is
    read as before and never goes to the model.
    """

    try:
        agent, citizen = city.agent_id, message.sender
        text = message.text or ""
        plain = _plain(text)
        heard: dict[str, object] = dict(_heard(message))
        offers = _live_offers(chat, agent, citizen)
        pending = _consent_offer(chat, agent, citizen)
        if offers and not pending:
            chosen = _pick_by_rules(plain, offers)
            if chosen:
                heard["offer_id"] = chosen
        if pending:
            yes, no = bool(_YES.search(plain)), bool(_NO.search(plain))
            if yes != no:
                heard["consent"] = "yes" if yes else "no"
        if fields_of(text) or not plain:
            return heard
        lang_known = any(
            row.sender.startswith("citizen:") and (_fields(row).get("lang") or _heard(row).get("lang"))
            for row in chat
        )
        needs = (
            (offers and not pending and "offer_id" not in heard)
            or (pending and "consent" not in heard)
            or (
                _asked_preferences(chat, agent, citizen)
                and "days" not in heard
                and "gender" not in heard
            )
            or not lang_known
        )
        if not needs:
            return heard
        read = await _read_with_model(city, text, [] if pending else offers, bool(pending))
        if read:
            _HEARD[message.msg_id] = {k: v for k, v in read.items() if k in ("days", "gender", "lang")}
            while len(_HEARD) > _HEARD_MAX:
                _HEARD.popitem(last=False)
            for key, value in read.items():
                heard.setdefault(key, value)
        return heard
    except Exception:  # noqa: BLE001 - reading free text must never stop the desk
        return {}


async def _read_with_model(
    city: City, text: str, offers: list[dict[str, str]], consent: bool
) -> dict[str, object]:
    """One city.llm call; only values from closed lists (and our own offer ids) come back."""

    keys = []
    if offers:
        listed = "\n".join(
            f"{i}. {offer.get('agenda_day', '?')} {offer.get('agenda_date', '?')}"
            for i, offer in enumerate(offers, 1)
        )
        keys.append(
            f'"choice": the number (1-{len(offers)}) of the offer the citizen accepts; 0 if they '
            "accept none, ask for something else, or it is unclear. Offers sent, in order:\n" + listed
        )
    if consent:
        keys.append('"consent": "yes" if the citizen gives the requested consent, "no" if they refuse, "unclear" otherwise')
    keys += [
        '"days": weekdays the citizen wants, from [mon, tue, wed, thu, fri]; [] if not said',
        '"not_days": weekdays the citizen cannot do, from the same list; [] if not said',
        '"gender": "f" if they want a female doctor, "m" a male doctor, "none" otherwise',
        '"lang": ISO 639-1 code of the language the message is written in',
    ]
    system = (
        "You read one message a citizen wrote to a healthcare booking desk. Reply with one JSON "
        "object and nothing else, with these keys:\n- " + "\n- ".join(keys)
    )
    try:
        answer = await asyncio.wait_for(
            city.llm([{"role": "system", "content": system}, {"role": "user", "content": text[:1000]}]),
            _LLM_TIMEOUT,
        )
        match = re.search(r"\{.*\}", str(answer.get("content", "")), re.S)
        data = json.loads(match.group(0)) if match else {}
    except Exception:  # noqa: BLE001 - CapError, timeout, bad JSON: the rules stand alone
        return {}
    if not isinstance(data, dict):
        return {}
    read: dict[str, object] = {}
    choice = data.get("choice")
    if offers and type(choice) is int and 1 <= choice <= len(offers):
        read["offer_id"] = offers[choice - 1]["offer_id"]
    if consent and data.get("consent") in ("yes", "no"):
        read["consent"] = data["consent"]

    def week(value: object) -> set[str]:
        return {str(d).lower()[:3] for d in value} & set(_WEEK) if isinstance(value, list) else set()

    wanted, excluded = week(data.get("days")), week(data.get("not_days"))
    if wanted or excluded:
        days = (wanted or set(_WEEK)) - excluded
        if days and len(days) < len(_WEEK):
            read["days"] = frozenset(days)
    if data.get("gender") in ("f", "m"):
        read["gender"] = data["gender"]
    lang = str(data.get("lang", "")).lower()
    if lang in _LANGS:
        read["lang"] = lang
    return read


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
