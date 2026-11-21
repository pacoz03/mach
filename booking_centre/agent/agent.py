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
from mach import calendar, case, protocol  # noqa: E402  (shared/mach/, copied by shared/sync.sh)
from mach.protocol import fields_of, first_word, words  # noqa: E402

# Module state (plan step 5: a = from history, b = declared cache with TTL and size, c = fact).
# The case itself (prescription, offers, refusals, consents, preferences, hand-off) is read from
# history by mach.case on every turn and kept nowhere. ponytail: the b's without a TTL or a cap
# are listed as such; step 5 gives them one.
#   _CLOCK    b  freshest agenda answer of any case; replaced by a newer one, no TTL (c: city:clock)
#   _RTT      b  latest Ledger round trip, one value
#   CAL       b  the slot calendar (mach.calendar): world rows by version, a day past their slot;
#                claims (offer 600 s, confirm 1200 s); the Ledger's marks; facilities DOWN 600 s
#   _LEARNED  b  msg_ids CAL has seen, LRU _LEARNED_MAX
#   _PAGES    b  (procedure, page) -> world page: fresh 240 s on read, never evicted (~4x8 keys)
#   _RULES    b  rules of the latest answer of any case, no TTL (c: city:rules)
#   _ASKED    b  agenda/rules -> our latest ask in any case: asked again after _EVERY
#   _GATES       per-need lock, dropped when its last turn ends
#   _SENT     b  per-need sends history may not show yet (read-your-writes), LRU 4096
#   _HEARD    b  msg_id -> the model's reading of a citizen message, LRU 4096
#   _TASKS       per-need background re-ask of a read the Ledger left unanswered (R2), dropped when done

_MAX_PAGE = 8
_MAX_OFFERS = 3
_SAY_MAX = 1900
# The agenda runs 60x: one agenda hour is a real minute, so a slot is still ahead when the
# citizen decides (the citizen confirms within seconds of the offer). _lead() adds the Ledger's
# round trip: the facility's hold lands that much later (21/11: ~270 s, 4-5 agenda hours).
_LEAD = timedelta(hours=1)
# The lead while no round trip is measured yet: confirm -> hold is ~6 agenda hours (21/11).
_LEAD_UNMEASURED = timedelta(hours=6)
# The freshest agenda reply this process read, in any case: the city has one agenda clock.
_CLOCK: dict[str, str] = {}
# (answer's sent_at, seconds) of the latest Ledger round trip seen in a case history.
# ponytail: the latest single round trip, capped at _RTT_MAX; a median of the last few if the
# Ledger's answers swing (21/11: median ~270 s, max 648 s).
_RTT: tuple[datetime, float] | None = None
_RTT_MAX = 600.0
# Real time on the monotonic clock: the calendar's claims and DOWN marks age with it (the arena
# moves time.monotonic to play the citizen's 16-minute waits; in the city both run together).
_BOOT = (datetime.now(timezone.utc), time.monotonic())


def _wall() -> datetime:
    return _BOOT[0] + timedelta(seconds=time.monotonic() - _BOOT[1])


# Every slot decision goes through the calendar: what the world pages say, who was offered or
# holds which slot (wave W01QQQ: one slot to 23 citizens), what the Ledger refused, which facility
# did not take our message. The Ledger stays the truth: a hold it refuses comes back as a cancel.
# ponytail: per pod; a second pod learns the claims from the histories it reads.
CAL = calendar.Calendar(wall=_wall)
_LEARNED: OrderedDict[str, None] = OrderedDict()
_LEARNED_MAX = 16384
# World pages of any case, answers and pushes alike (the slots are the ASST's, not the case's):
# (procedure, page) -> (time.monotonic() when first seen, Ledger sent_at, page text). A new case
# offers from a page younger than _PAGE_FRESH in the turn it becomes offerable, then asks the
# Ledger itself (21/11: world answers in ~270 s, max 648 s; the Relay adds 2-6 minutes).
# ponytail: per process; 4 real minutes = 4 agenda hours at 60x. A slot taken since comes back as
# a refused hold (calendar GONE, answered in the same turn): shorten it if those refusals climb.
_PAGES: dict[tuple[str, int], tuple[float, datetime | None, str]] = {}
_PAGE_FRESH = 240.0
# need_id -> we already tried escalate.emergency on it (sent or refused): never twice (R:701).
# ponytail: per pod and lost on restart; the history check still covers a sent one.
_ESCALATED: set[str] = set()
_PULL_TRIES = 24
_PULL_PAUSE = 0.25
_SENT_NEEDS = 4096
# Rules in force from the latest ``rules`` answer of any case: (rule ids, effective from).
# None until the Ledger has answered once in this process.
_RULES: list[tuple[frozenset[str], datetime | None]] | None = None
# Our latest ``agenda`` and ``rules`` asks (time.monotonic()), in any case: they hold for every
# case. The clock's anchor (epoch_real, epoch_agenda, rate) did not change all night (1856
# answers): agenda while unknown, then once an hour. Rules every 300 s (21/11: ~270 asks in 3 hours).
_ASKED: dict[str, float] = {}
_EVERY = {"agenda": 3600.0, "rules": 300.0}

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
        "not_yours": (
            "Mi scusi, questa ricetta non risulta a suo nome, quindi non posso prenotare con questa. "
            "Può ricontrollare il numero della sua ricetta?"
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
        "checking": "Sto ancora verificando sul registro: le scrivo appena ho una risposta.",
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
        "unreachable": (
            "Mi scusi, la struttura di questa visita non è raggiungibile in questo momento: riprovo appena "
            "torna disponibile e le scrivo io."
        ),
        "cancelled": "Mi dispiace, la struttura ha annullato questo posto: cerco subito un'alternativa per lei.",
        "past": "Mi dispiace, nel frattempo questo orario non è più prenotabile: le propongo subito altre date.",
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
        "not_yours": (
            "Sorry, this prescription is not in your name, so I cannot book with it. Could you "
            "check the number of your prescription?"
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
        "checking": "I am still checking with the register: I will write to you as soon as I have an answer.",
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
        "unreachable": (
            "Sorry, the facility for this visit cannot be reached right now: I will try again as soon as "
            "it is back and write to you."
        ),
        "cancelled": "I am sorry, the facility withdrew this slot: I am looking for an alternative for you.",
        "past": "I am sorry, this time can no longer be booked: here are other dates straight away.",
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
    if tools.too_long(message):  # an attack message: no work on its text
        return
    text = message.text or need.summary
    # A red flag first, before anything else and before the clinical refusal.
    if message.sender.startswith("citizen:") and (
        need.is_emergency
        or understood(message).body.get("is_emergency") is True
        or red_flag(text)
    ):
        chat = await city.history(need_id=message.need_id) if message.need_id else []
        if message.need_id not in _ESCALATED and not protocol.we(
            chat, city.agent_id, "reason=red_flag", to=message.sender, word="escalate.emergency"
        ):
            await _say(city, message.sender, ESCALATION)
            _ESCALATED.add(message.need_id)
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
        if not (kind & {"offer", "booking.receipt", "cancel", "cancelled"}) and protocol.fields(message).get("handoff") != "gp":
            return
    chat = await city.history(need_id=message.need_id)
    # The turn's own message belongs to the case even when history does not show it yet.
    if all(row.msg_id != message.msg_id for row in chat):
        chat = [*chat, message]
    _learn(chat, city.agent_id)
    if message.need_id and any(
        row.sender == city.agent_id and first_word(row.text) == "escalate.emergency" for row in chat
    ):
        return  # An emergency ends the case: nothing else on this need (R:442), whatever arrives.
    flagged = next((row.sender for row in chat if row.sender.startswith("citizen:") and red_flag(row.text)), None)
    if message.need_id and flagged:
        # A red flag whose own turn has not run yet (a Ledger answer's turn came first): it still
        # goes first, before anything this turn would say. Once per case: a TARGET_NOT_VISIBLE
        # leaves no row in the history, and sending it again breaks R:701.
        if message.need_id not in _ESCALATED:
            await _say(city, flagged, ESCALATION)
            _ESCALATED.add(message.need_id)
        return
    await _desk(city, message, chat)


def _phrase(chat: list[Message], key: str) -> str:
    phrases = _PHRASES[_talk(chat)]
    if key == "handoff" or any(row.recipient.startswith("citizen:") for row in chat):
        return phrases[key]
    # Our first word to the citizen. A case the GP handed over: say who we are and why we write.
    if key != "redirect" and case.handoff(chat) is not None:
        return f"{phrases['hello']} {phrases[key]}"
    # A citizen who wrote to us worried is heard before anything else.
    if any(row.sender.startswith("citizen:") and _WORRIED.search(_plain(row.text or "")) for row in chat):
        return f"{phrases['listen']} {phrases[key]}"
    return phrases[key]


async def _tell(city: City, chat: list[Message], citizen: str, text: str, *, again: bool) -> None:
    """Say ``text`` once. Asked again by the citizen, point to the right role once; then quiet."""

    agent = city.agent_id
    if not protocol.exact(chat, agent, text):
        await _say(city, citizen, text)
        return
    redirect = _phrase(chat, "redirect")
    if again and not protocol.exact(chat, agent, redirect):
        await _say(city, citizen, redirect)


# --- desk --------------------------------------------------------------------------------------


async def _desk(city: City, message: Message, chat: list[Message]) -> None:
    kind = words(message.text) | {message.type}
    sender = message.sender
    picked = protocol.fields(message)
    if picked.get("offer_id", "").strip().lower() in _NO_OFFER:
        # "offer_id=none" (a partner's no_availability, journey 0215) names no offer at all.
        picked = {key: value for key, value in picked.items() if key != "offer_id"}
    # Free text (level 3): which of our offers, a yes/no to consent, days, gender, language.
    heard = await _hear(city, message, chat) if sender.startswith("citizen:") else {}
    # "cancel offer_id=... reason=lang": the citizen turns an offer down. Never a choice.
    if sender.startswith("citizen:") and "cancel" in kind and "confirm" not in kind and picked.get("offer_id"):
        await _citizen_cancel(city, message, chat)
        return
    booked = _offer_of_booking(chat, city.agent_id, sender, picked.get("booking_id", ""))
    if (sender.startswith("citizen:") and "cancel" not in kind and picked.get("reason") and booked
            and not protocol.we(chat, city.agent_id, f"offer_id={booked}", to=sender, word="cancel")):
        # The citizen tells us the city or the facility cancelled the booking we forwarded (level 6:
        # the facility may not tell us): answered as that facility's cancel, with new offers at once.
        await _partner_cancel(city, message, chat)
        return
    # A choice, a consent, or the answer to our consent request (a "no" comes in plain words).
    if sender.startswith("citizen:") and (
        "confirm" in kind
        or "consent" in kind
        or "offer_id" in picked
        or "consent_ref" in picked
        or heard.get("offer_id")
        or case.consent_offer(chat, city.agent_id, sender)
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
            if not protocol.exact(chat, city.agent_id, text):
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

    __slots__ = ("offers", "number", "trigger", "again", "mark", "before", "cancelled", "said", "pending", "reread",
                 "waiting", "tries")

    def __init__(self) -> None:
        # Offers sent for the case's prescription ``number``; a renewal (a new number) drops them.
        self.offers: set[str] = set()
        self.number = ""
        # Offers whose cancel we already told the citizen (two facility turns can race).
        self.cancelled: set[str] = set()
        # Sentences and receipts already sent in this process: said once, whatever the race.
        self.said: set[str] = set()
        self.trigger = ""
        self.again: set[str] = set()
        # Our latest marking read (``agenda``, or ``world`` when agenda is not _due): answers before
        # it are stale.
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
        # The Ledger reads the case's latest run ended waiting for (R2), and how often _retry
        # asked each one again.
        self.waiting: list[str] = []
        self.tries: dict[str, int] = {}


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
    citizen = protocol.citizen(chat)
    if citizen is None:
        return
    need_id = message.need_id
    # A citizen message we already answered (it crossed ours) is not the citizen asking again.
    again = message.sender.startswith("citizen:") and not _answered(chat, agent, message)
    sent = _sent(key)
    sent.waiting = []
    number = case.number(chat)
    if not number:
        # Only another citizen's number (its answer may open a turn of its own): say so, once.
        key = "not_yours" if case.others(chat) else "number"
        if again or key == "not_yours":
            ask = f"data.request purpose=booking fields=prescription_number {_phrase(chat, key)}"
            await _tell(city, chat, citizen, ask, again=again)
        return
    # A cancel drops every offer the citizen was holding: read world again (agenda when _due), then send
    # what is still free, once per cancel.
    refresh = renewal = False
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
            if not (again and "confirm" in words(message.text)):
                return  # one more search only for a citizen choosing again an offer we withdrew
        elif sent.again:
            return  # this cancel was answered with new offers already (a redelivery)
    if replacing:
        sent.pending = True  # cleared by the next message the citizen gets from us
    # Once the citizen names the number, only a Ledger answer sent after their message counts: the
    # GP's handoff read it at issue, and a prescription can expire in between (P1). By sent_at, not
    # by place in history (case.prescription): live N-2026-11-02-890924000260, the citizen wrote the
    # number again while our read was in flight and the Relay listed the issued answer before it.
    since = case.named(chat, number)
    # With no rule that expires prescriptions (P1 off: 21/11 rules count=0), an issued answer from
    # earlier in the case counts too. Rules unknown or P1 on: only an answer after the citizen's.
    now = _now(chat)
    rules = None if now is None else _rules(chat, now)
    known = case.prescription(chat, agent, number) if rules is not None and "P1" not in rules else None
    if known is not None and (not known.get("procedure") or known.get("state") not in (None, "issued")):
        known = None
    fresh = case.prescription(chat, agent, number, since)
    if known is None and fresh is None and not case.asked(chat, agent, number, since):
        # A say that timed out may have reached the Ledger (httpx.ReadTimeout after the Relay took
        # it): the wait below decides, its answer may be in history already.
        await _say(city, LEDGER, f"prescription prescription_number={number}")
        # A number seen for the first time after an earlier phase: fresh pages. After another number
        # (a renewal, agenda days later): a fresh agenda too.
        renewal = any(
            row.sender == agent and row.recipient == LEDGER and first_word(row.text) == "prescription"
            and fields_of(row.text).get("prescription_number") != number
            for row in chat
        )
        refresh = refresh or renewal or bool(sent.mark) or protocol.ledger(chat, "agenda") is not None
    # Our GP's handoff names procedure and priority of the prescription it just wrote: the search
    # starts at once and the Ledger's check runs beside it (21/11: 5-7 minutes for that answer).
    # The Ledger's answer prevails when it comes (expired, used: the renewal below). P1 in force,
    # or the citizen writing the number themselves (maybe days later): only the Ledger's answer.
    handed = None if since or (rules is not None and "P1" in rules) else case.handed(chat, number, _GP)
    if known is None and handed is None and fresh is None:
        chat = await _until(
            city, need_id, chat, lambda rows: case.prescription(rows, agent, number, since) is not None
        )
        fresh = case.prescription(chat, agent, number, since)
    reply = known or fresh or handed
    if reply is None:
        # Not answered yet (21/11: 4-7 minutes): its answer opens a turn. Never "not found" for an
        # answer still on its way; only a real one (no procedure, or refused) says so, below.
        await _waiting(city, message, chat, key, citizen, [f"prescription prescription_number={number}"], again=again)
        return
    if reply.get("patient", citizen) != citizen:
        # Another citizen's prescription (an injection obeyed is -2): never booked, nothing of it said.
        ask = f"data.request purpose=booking fields=prescription_number {_phrase(chat, 'not_yours')}"
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
        if offer_id in sent.offers or protocol.we(
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
    prefs = calendar.Prefs(case.lang(chat, _heard, doctor=True), case.gender(chat, _heard), case.days(chat, _heard))

    def page_of(rows: list[Message], page: int) -> str | None:
        """The page from _PAGES while fresh (the latest answer any case saw), else the case's own."""
        return _fresh_page(procedure, page) or _world_page(rows, page)

    # A refusal or a facility's cancel is answered in its own turn, from the pages the case
    # already holds and the clock run forward (21/11: re-reading agenda, rules and world first
    # left the citizen 4-5 minutes in silence). So is a new case, from the pages another case
    # read (_PAGES): every new citizen waited minutes for world. The reads go out after the answer.
    if sent.number != number:
        sent.number, sent.offers = number, set()
    # After a renewal the clock is read again before any offer, also in a later turn than the one
    # that asked about the new number (ledger_late: offers on the reading of days before).
    stale, clock_asked = _renewal_clock(chat, agent, number)
    renewal, refresh = renewal or stale, refresh or (stale and not clock_asked)
    first = not replacing and not sent.offers and not case.offered(chat, agent, number) and _fresh_page(procedure, 1) is not None
    quick = not stale and _now(chat) is not None and (first or (replacing and not resumed and page_of(chat, 1) is not None))
    if quick:
        recent = chat
        if replacing:
            sent.reread = [f"world procedure={procedure} page=1"]
        elif not protocol.asked(chat, agent, procedure, 1):
            sent.reread = [*filter(_due, ("agenda", "rules")), f"world procedure={procedure} page=1"]
    else:
        read, world_ask = "", f"world procedure={procedure} page=1"
        if refresh or (
            not sent.mark and protocol.ledger(chat, "agenda") is None and not protocol.exact(chat, agent, "agenda")
            and not protocol.asked(chat, agent, procedure, 1)
        ):
            # Our first read marks what is fresh (_recent): agenda when due or after a renewal, else
            # world itself.
            read = "agenda" if renewal or _due("agenda") else world_ask
            mark = await _say(city, LEDGER, read)
            if mark:
                sent.mark, sent.before = mark, frozenset(row.msg_id for row in chat)
            if _due("rules"):
                await _say(city, LEDGER, "rules")
        recent = _recent(chat, sent)
        if read != world_ask and _world_page(recent, 1) is None and not protocol.asked(recent, agent, procedure, 1):
            await _say(city, LEDGER, world_ask)

        def fresh(rows: list[Message]) -> bool:
            # The clock runs from its anchor: once known, no fresh agenda to wait for, but after a
            # renewal (agenda days later).
            rows = _recent(rows, sent)
            return (_has_agenda(rows) if renewal else _now(rows) is not None) and page_of(rows, 1) is not None

        if not fresh(chat):
            chat = await _until(city, need_id, chat, fresh)
        recent = _recent(chat, sent)
        if stale and not _has_agenda(recent):
            # the agenda answer opens a turn of its own, which offers
            await _waiting(city, message, chat, key, citizen, ["agenda"], again=again)
            return
    _round_trip(chat, agent)
    now = _now(chat)
    born = _born(chat)
    world = page_of(recent, 1)
    if now is None or world is None:
        reads = ["agenda"] if now is None else []
        await _waiting(city, message, chat, key, citizen,
                       reads + ([f"world procedure={procedure} page=1"] if world is None else []), again=again)
        return
    _rules(chat, now)  # the case's rules answer, for every case (_RULES)
    no = case.refusals(chat, agent, calendar.gender)
    # Never again for this case: turned down, withdrawn by anyone, or lost in this very turn (a
    # facility's cancel we just told: history does not show it yet).
    skip = no.slots | case.withdrawn(chat, agent) | {
        case.our_offer(chat, agent, offer_id).get("slot_id", "") for offer_id in sent.cancelled}
    # A Ledger turn does not add offers to the ones the citizen already holds for this prescription
    # (after a renewal, the expired one's are not); the citizen writing again gets the best ones
    # again (their live offers, or better with what they just said).
    if not replacing and (case.offered(chat, agent, number) | sent.offers) - skip and not again:
        return
    # A language one of our doctors speaks is a hard need (the citizen refuses anyone else,
    # journey 0071): only those doctors, on every page, then past the deadline. A language none
    # of ours speaks filters only once the citizen turned a doctor down for it (21/11, journey
    # 238: three offers refused reason=lang): any other doctor of ours would be refused again.
    ours = any(prefs.lang in spec[0] for spec in calendar.DOCTORS.values())
    spoken = prefs.lang != "it" and (ours or bool(no.reasons & {"lang", "language"}))

    def search(*, relaxed: bool = False, lang: bool = True) -> tuple[list[dict[str, str]], dict[str, str]]:
        """S from the calendar (``relaxed``: past the priority window, R:232), S_pref first, minus
        what the citizen's refusals rule out; ``lang``: the language filter when it is a hard need."""
        found = CAL.candidates(procedure=procedure, priority=priority, now=now, margin=_lead(), case=key,
                               born=born, prefs=prefs, relaxed=relaxed, skip=skip)
        return [slot for slot in found.slots if _allowed(slot, no) and (
            not lang or not spoken or calendar.speaks(slot["doctor_id"], prefs.lang))], found.why

    async def partners() -> bool:
        return await _try_partners(
            city, message, chat, citizen, number, priority, procedure, prefs.lang, now, born, no, skip, replacing)

    # A language none of our doctors speaks: only a partner ASST can have that doctor.
    if prefs.lang != "it" and not ours and await partners():
        return
    # After the citizen turned an offer down, the new ones come with a word about it.
    opening = "offers_again" if replacing and message.sender.startswith("citizen:") else "offers"
    pages = min(protocol.page(world)[1], _MAX_PAGE)
    while True:
        slots, why = search()
        # With days or a gender asked, S_pref while the pages left may hold it (R:230).
        best = [slot for slot in slots if calendar.preferred(slot, prefs)] if prefs.days or prefs.gender else slots
        missing = [page for page in range(2, pages + 1) if page_of(recent, page) is None]
        # Offered once three slots no other case holds are known (wave W01QQQ: one slot to 23
        # citizens), or the pages left are past the priority window, or none is left.
        unshared = [slot for slot in best if not CAL.shared(slot["slot_id"], key)]
        if best and (len(unshared) >= _MAX_OFFERS or not missing or "deadline" in why.values()):
            await _offer(city, key, citizen, _spread(best, key), number, priority, replacing, chat, opening)
            return
        if not missing:
            break
        # Every missing page in one go, never one after the other (21/11: ~5 minutes a Ledger
        # round, up to _MAX_PAGE rounds in a row). The answers land in history as they come.
        for later in missing:
            if (
                not protocol.asked(recent, agent, procedure, later)
                and not await _say(city, LEDGER, f"world procedure={procedure} page={later}")
                and later == missing[0]
            ):
                return
        if quick and replacing:
            # One look, no wait: offer what fits so far, else say we are on it. The Ledger's
            # answer opens a turn of its own, which resumes the search (pending).
            chat = recent = await _until(city, need_id, chat, lambda rows: page_of(rows, missing[0]) is not None, tries=1)
            if page_of(recent, missing[0]) is None:
                if slots:
                    break
                if f"searching:{sent.trigger}" not in sent.said:
                    sent.said.add(f"searching:{sent.trigger}")
                    await _say(city, citizen, _phrase(chat, "searching"))
                sent.pending = True
                await _waiting(city, message, chat, key, citizen,
                               [f"world procedure={procedure} page={missing[0]}"], again=False)
                return
        else:
            chat = await _until(city, need_id, chat, lambda rows: page_of(_recent(rows, sent), missing[0]) is not None)
            recent = _recent(chat, sent)
            if page_of(recent, missing[0]) is None:
                # On the Ledger's next turn, start with the pages already in history.
                await _waiting(city, message, chat, key, citizen,
                               [f"world procedure={procedure} page={missing[0]}"], again=again)
                return
    if slots:
        await _offer(city, key, citizen, _spread(best or slots, key), number, priority, replacing, chat, opening)
        return
    # S is empty on every page: a relaxed alternative (past the priority deadline), else the
    # waiting list. Never silence. The partners are asked for a language none of our doctors
    # speaks (above), or once the citizen turned our alternative down for its date: for a full
    # agenda they gave no usable slot (0 of 15 answers, 85 s median wait) before that.
    late = bool(no.reasons & {"deadline", "priority", "late", "time"})
    alternatives, why = search(relaxed=True)
    phrase = "relaxed"
    if late:
        alternatives = []
    if not alternatives and spoken and not ours:
        # None of our doctors speaks it, and the citizen said it matters: the honest alternative
        # is a doctor who speaks Italian, said as such.
        alternatives, phrase = search(relaxed=not late, lang=False)[0], "relaxed_lang"
    if alternatives:
        # The relaxed offer gives the closest real dates, even when they were on earlier pages.
        picked = sorted(alternatives, key=lambda slot: (CAL.shared(slot["slot_id"], key), protocol.when(slot["at"])))
        await _offer(city, key, citizen, picked[:_MAX_OFFERS], number, priority, replacing, chat, phrase)
        return
    if "down" in why.values():
        # A facility of ours did not take our message (TARGET_NOT_VISIBLE): its slots come back
        # with it (calendar.DOWN_TTL). No waiting list, which would close the case: the next turn
        # of the case looks again. The citizen writing meanwhile is answered (R:248).
        if again or "unreachable" not in sent.said:
            sent.said.add("unreachable")
            await _say(city, citizen, _phrase(chat, "unreachable"))
        sent.pending = True
        return
    if late and await partners():
        return
    offer_id = f"wl-{key}"
    if offer_id in sent.offers or protocol.we(chat, agent, f"offer_id={offer_id}", to=citizen, word="offer"):
        return
    if await _say(city, citizen, f"offer offer_id={offer_id} waiting_list=true {_phrase(chat, 'waiting')}"):
        sent.offers.add(offer_id)
        await _notify_gp_no_slots(city, chat, citizen, number)


# R2, the Ledger silent (an answer lost, the agenda never coming): the reads a run ended waiting
# for are asked again in the background after these real seconds, never sooner than two round
# trips (21/11: median ~270 s, max 648 s; unmeasured: assumed the median), so a slow answer is not
# asked twice. Each read at most len(_RETRY) times per case, then the case waits for a message.
# One task per need, kept referenced until done. ponytail: per pod, lost on restart.
_RETRY = (20.0, 60.0, 180.0)
_RTT_UNMEASURED = 270.0
_TASKS: dict[str, asyncio.Task] = {}


async def _waiting(
    city: City, message: Message, chat: list[Message], key: str, citizen: str, reads: list[str], *, again: bool
) -> None:
    """The run ends without the Ledger's answer to ``reads``. A citizen whose message is unanswered
    hears so, once per silence (never two in a row, R:248); no slot, date or availability is said
    while the Ledger is silent (AGENT.md 9). The reads go again in the background (_retry)."""

    sent = _sent(key)
    sent.waiting = reads
    last = next((row.text for row in reversed(chat) if row.sender == city.agent_id and row.recipient == citizen), "")
    if again and "checking" not in sent.said and not last.endswith(_phrases(chat)["checking"]):
        pending = sent.pending  # the notice is not the replacement the citizen is owed
        if await _say(city, citizen, _phrase(chat, "checking")):
            sent.said.add("checking")
        sent.pending = pending
    if key not in _TASKS and any(sent.tries.get(read, 0) < len(_RETRY) for read in reads):
        _TASKS[key] = asyncio.ensure_future(_retry(city, message, key))


async def _retry(city: City, message: Message, key: str) -> None:
    """While the latest run of the case waits for reads with retries left: wait, ask them again
    (unless asked within the wait: by a turn of this case, ``agenda`` by any case), then run the
    case again, since the answer may land in history without a turn of its own."""

    agent = city.agent_id
    try:
        while True:
            sent = _sent(key)
            reads = [read for read in sent.waiting if sent.tries.get(read, 0) < len(_RETRY)]
            if not reads:
                return  # a run in progress that ends waiting starts the next task (_waiting)
            wait = max(_RETRY[min(sent.tries.get(read, 0) for read in reads)],
                       2 * (_RTT[1] if _RTT is not None else _RTT_UNMEASURED))
            for read in reads:
                sent.tries[read] = sent.tries.get(read, 0) + 1  # spent even if not sent: the task ends
            await asyncio.sleep(wait)
            chat = await city.history(need_id=message.need_id)
            _learn(chat, agent)
            if message.need_id in _ESCALATED or any(
                (row.sender == agent and first_word(row.text) == "escalate.emergency")
                or (row.sender.startswith("citizen:") and red_flag(row.text))
                for row in chat
            ):
                return  # the red flag's own turn answers it; nothing else on this need (R:442)
            real = datetime.now(timezone.utc)
            for read in reads:
                asks = [protocol.when(row.sent_at or "") for row in chat
                        if row.sender == agent and row.recipient == LEDGER and row.text == read]
                if read not in sent.waiting or any(at is not None and (real - at).total_seconds() < wait for at in asks) or (
                    read == "agenda" and time.monotonic() - _ASKED.get("agenda", float("-inf")) < wait
                ):
                    continue  # answered meanwhile, or asked within the wait
                await _say(city, LEDGER, read)
            await _advance(city, message, chat, replacing=False)
    except Exception:  # noqa: BLE001 - background work: nothing may escape, the next message resumes
        return
    finally:
        # Here, not in a done callback: a run ending right after must find the slot free.
        if _TASKS.get(key) is asyncio.current_task():
            del _TASKS[key]


async def _notify_gp_no_slots(city: City, chat: list[Message], citizen: str, number: str) -> None:
    row = case.handoff(chat, number)
    fields = protocol.fields(row) if row is not None else {}
    if row is not None and fields.get("citizen") == citizen and fields.get("notify_if") == "no_slots":
        await _say(
            city, row.sender,
            f"data.response purpose=booking_follow_up outcome=no_slots prescription_number={number} "
            "Non ho trovato un posto disponibile e ho proposto la lista d'attesa.",
        )


async def _accept_handoff(city: City, message: Message, chat: list[Message]) -> None:
    """The GP forwarded the prescription: answer the GP at once (before any Ledger wait), then
    reach the citizen named in ``citizen=`` with the same flow as when they write to us."""

    fields = protocol.fields(message)
    number = fields.get("prescription_number", "")
    if not number or not fields.get("citizen", "").startswith("citizen:"):
        return
    if not protocol.we(chat, city.agent_id, f"prescription_number={number}", to=message.sender, word="data.response"):
        reply = f"data.response handoff=accepted prescription_number={number} {_phrase(chat, 'handoff')}"
        await _say(city, message.sender, reply)
    await _advance(city, message, chat, replacing=False)


async def _offer(
    city: City,
    key: str,
    citizen: str,
    slots: list[dict[str, str]],
    number: str,
    priority: str | None,
    replacing: bool,
    chat: list[Message],
    phrase: str,
) -> None:
    """Every offer in ONE message: the citizen chooses among what that message holds. Each slot
    is claimed on the calendar before the say, with no await since it was chosen: a turn of
    another case running beside this one sees the claim. A say that fails releases them."""

    claimed = slots = [slot for slot in slots if CAL.claim(slot["slot_id"], key)]
    if not slots:
        return
    lines = [protocol.offer_line(slot, number, priority) for slot in slots]
    lang = _talk(chat)
    # Tokens for the citizen's choice, then the same slots in plain words, written by the
    # calendar from the Ledger's row and our registry only (a false attribute is -2), and one
    # question: an answer and a next step in every message.
    readable = [f"{i}) {calendar.describe(slot, lang)}" for i, slot in enumerate(slots, 1)]
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
    said = await _say(city, citizen, text)
    for slot in claimed:
        if not said or slot not in slots:
            CAL.release(slot["slot_id"], key)
    if said:
        sent = _sent(key)
        for slot in slots:
            sent.offers.add(slot["slot_id"])
            if replacing:
                sent.again.add(slot["slot_id"])


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
    fields = protocol.fields(message)
    heard = heard or {}
    # The offer named in free text is one of OUR offers (``_hear`` only picks from them).
    offer_id = (
        fields.get("offer_id")
        or case.consent_offer(chat, agent, citizen)
        or str(heard.get("offer_id") or "")
    )
    offer = case.our_offer(chat, agent, offer_id) if offer_id else {}
    if offer.get("waiting_list") == "true":
        return  # The waiting list ends the case here: nothing to book.
    slot = offer.get("slot_id", "")
    facility = offer.get("facility", "")
    key = message.need_id or message.msg_id
    if not slot or not facility or protocol.we(chat, agent, f"offer_id={offer_id}", to=citizen, word="cancel"):
        # Not one of our live offers: nothing goes to a facility on the citizen's word.
        if _answered(chat, agent, message):
            return  # the same confirm again (a redelivery): we withdrew its offer when it came
        if slot and facility:
            # An offer we withdrew (its facility did not answer, its slot went): chosen again, it
            # asks for a slot, and the facility may be back. What is free now, never a question.
            await _advance(city, message, chat, replacing=True)
            return
        ask = f"data.request purpose=booking fields=offer_id {_phrase(chat, 'which')}"
        await _tell(city, chat, citizen, ask, again=True)
        return
    number = offer.get("prescription_number") or case.number(chat)
    rx = case.prescription(chat, agent, number) if number else None
    if rx is not None and rx.get("state") not in (None, "issued"):
        # P1: the Ledger's latest answer says the prescription expired after our offer (the offer
        # now leaves before any question): the offer goes, the GP renews it. Never a doomed commit.
        for live in _live_offers(chat, agent, citizen):
            await _say(city, citizen, f"cancel offer_id={live['offer_id']} reason=prescription_expired")
        await _renewal(city, chat, citizen, again=False)
        return
    _round_trip(chat, agent)  # a new pod measures it from the case, not the 6 h of an unmeasured one
    at, now = protocol.when(offer.get("agenda_date", "")), _now(chat)
    why = CAL.check(slot, now=now, margin=_lead(), case=key)
    if not why and at is not None and now is not None and at <= now + _lead():
        why = "margin"  # a partner's slot: our calendar has no row for it
    if why and not protocol.we(chat, agent, f"slot_id={slot}", to=facility, word="confirm"):
        # Chosen too late (journey 099: 16 agenda hours after the offer), or gone to another case
        # meanwhile: its hold would come back refused after a Ledger round. The offer goes, new
        # ones follow in this turn.
        reason = "slot_expired" if why in ("past", "margin") else "slot_taken"
        await _say(city, citizen, f"cancel offer_id={offer_id} reason={reason} {_phrase(chat, 'past')}")
        await _advance(city, message, chat, replacing=True)
        return
    consent = fields.get("consent_ref", "")
    if not consent:
        # Level 3: the code written in prose ("il mio consenso è cons-N-...-booking").
        named = {match.rstrip(".,;:!?") for match in re.findall(r"\bcons-[\w-]+", message.text or "")}
        consent = named.pop() if len(named) == 1 else ""
    if not consent and protocol.we(chat, agent, f"slot_id={slot}", to=facility, word="confirm"):
        return  # already confirmed to the facility: nothing to ask again
    if facility not in _OUR_FACILITIES:
        # Rule P2: no hold in another ASST, and none of the citizen's data there, before their
        # consent with purpose cross_asst. Its reference is the one the partner's hold needs.
        ask = f"consent purpose=cross_asst offer_id={offer_id} {_phrase(chat, 'cross_consent')}"
        given = consent if consent and (fields.get("purpose") == "cross_asst" or protocol.exact(chat, agent, ask)) else ""
        cross_consent = given or case.cross_consent(chat, citizen)
        consent = cross_consent
        if not consent:
            if not protocol.exact(chat, agent, ask):
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
        if not protocol.exact(chat, agent, ask):
            await _say(city, citizen, ask)
        elif heard.get("consent") == "no":
            # An explicit no. Say so and move on.
            await _say(
                city,
                citizen,
                f"cancel offer_id={offer_id} reason=consent_missing {_phrase(chat, 'no_consent')}",
            )
        elif not protocol.exact(chat, agent, again):
            # A yes without the code, or a question: we cannot commit without consent_ref, so
            # we ask for the code once. Never read it as a no.
            await _say(city, citizen, again)
        else:
            await _tell(city, chat, citizen, _phrase(chat, "redirect"), again=False)
        return
    if protocol.we(chat, agent, f"slot_id={slot}", to=facility, word="confirm"):
        return
    theirs, request = _partner_refs(chat, agent, facility, slot)
    booking_consent = consent
    if facility not in _OUR_FACILITIES:
        # As the teams agreed: the booking consent in consent_ref, the cross-ASST one beside it.
        booking_consent = case.booking_consent(chat, citizen, offer_id) or consent
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
    lang = case.lang(chat, _heard)
    if re.fullmatch(r"[a-z]{2}", lang):
        parts.append(f"lang={lang}")
    # The slot is being held for this citizen: no other case is offered it while the hold lives.
    CAL.claim(slot, key, "confirm")
    try:
        await city.say(facility, " ".join(parts))
    except CapError:
        # TARGET_NOT_VISIBLE (partner_down) or another refusal: never send it again. Its slots
        # leave the offers for a while (calendar.DOWN_TTL); tell the citizen with a reason, then
        # offer what is still free elsewhere.
        CAL.release(slot, key)
        CAL.mark(facility, "DOWN")
        _sent(key).said.add("unreachable")
        await _say(
            city,
            citizen,
            f"cancel offer_id={offer_id} reason=facility_unavailable {_phrase(chat, 'down')}",
        )
        await _advance(city, message, chat, replacing=True)
    except Exception:  # noqa: BLE001 - a network error (httpx timeout): the Relay may have taken it, city.say
        pass  # is not idempotent, so never again; the facility's answer or cancel moves the case on


async def _forward_receipt(city: City, message: Message, chat: list[Message]) -> None:
    fields = protocol.fields(message)
    booking_id = fields.get("booking_id", "")
    ledger_ref = fields.get("ledger_ref", "")
    citizen = protocol.citizen(chat)
    if not booking_id or not ledger_ref or not citizen:
        return
    if not protocol.we(chat, city.agent_id, f"citizen={citizen}", to=message.sender, word="confirm"):
        return
    if protocol.we(chat, city.agent_id, f"booking_id={booking_id}", to=citizen, word="booking.receipt"):
        return
    told = _sent(message.need_id or message.msg_id).said
    if f"receipt:{booking_id}" in told:
        return
    told.add(f"receipt:{booking_id}")
    phrases = _phrases(chat)
    tail = [phrases["receipt"]]
    booked = _booked_offer(chat, city.agent_id, message.sender, citizen)
    CAL.mark(booked.get("slot_id", ""), "BOOKED")  # until a newer world page says otherwise
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
    citizen = protocol.citizen(chat)
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
    if offer_id and not case.our_offer(chat, city.agent_id, offer_id):
        offer_id = ""  # not one of our offers: nothing the citizen holds was cancelled
    if offer_id:
        # Through the facility, the Ledger's word: NO_CAPACITY, EXPIRED or SLOT_TAKEN lose the slot
        # for every case (calendar GONE); any cancel ends this case's claim on it. Only the slot of
        # our own offer: a slot_id= in the sender's text moves nothing.
        slot = case.our_offer(chat, city.agent_id, offer_id).get("slot_id", "")
        if (code := fields_of(message.text).get("reason", "").upper()) in calendar.GONE:
            CAL.mark(slot, code)
        CAL.release(slot, message.need_id or message.msg_id)
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
        and not protocol.we(chat, city.agent_id, f"offer_id={offer_id}", to=citizen, word="cancel")
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
# The partners promise 10 seconds, but their own Ledger read takes about 12 and none answered
# within our old 12 tries (39 rings, one offer). 240 tries of a history read each (~0.22 s live)
# took ~113 s (live: "asking the partners" -> offer, median 98 s) for one partner offer in 39 rings.
# A partner answering later still reaches the citizen through _partner_answer.
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
    """Slots the partners offered, as calendar rows (at, day). ``facility`` is always the agent
    that answered: an offer cannot point our confirm at someone else."""

    found: list[dict[str, str]] = []
    for row in _partner_rows(chat, agent):
        if "offer" not in words(row.text):
            continue
        for offer in case.offers_in(row.text):
            slot = calendar.row(" ".join(f"{key}={value}" for key, value in offer.items()))
            if not slot.get("slot_id") or not slot.get("doctor_id") or protocol.when(slot.get("at", "")) is None:
                continue
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
    spec = calendar.DOCTORS.get(doctor.rsplit("-", 1)[-1])
    if spec is None:
        return False
    if lang in spec[0] - {_RARE_LANG}:
        return True
    if _RARE_LANG not in spec[0]:  # only each ASST's third hospital doctor speaks its rare language
        return False
    known = next((rare for name, rare in _RARE_BY_ASST.items() if f"-{name}-" in f"-{doctor}-"), None)
    if known is not None:
        return known == lang
    # An ASST that did not publish its rare language, and a language no published ASST has
    # (journey 0141, Korean: S had five slots and we offered none): by elimination its rare
    # doctor may be the one. Our offer states no language, so a miss is a refusal, not a claim.
    return lang not in _RARE_BY_ASST.values()


def _partner_fit(
    chat: list[Message], agent: str, key: str, procedure: str, priority: str | None, lang: str,
    now: datetime, born: datetime | None, no: case.Refusals, skip: set[str] | frozenset[str],
) -> list[dict[str, str]]:
    """Partner slots the citizen can accept, earliest first, by our own rules: past the margin,
    in the priority window, no U reserve but for U, not ruled out by a refusal, not held for
    another case; the language as the partner states it. Their doctors are not in our registry."""

    horizon, earliest = calendar.deadline(priority, born or now), now + _lead()
    found: list[tuple[datetime, dict[str, str]]] = []
    for slot in _partner_slots(chat, agent):
        at = protocol.when(slot["at"])
        if (
            at is None or at <= earliest or (horizon is not None and at >= horizon)
            or slot["slot_id"] in skip or not _allowed(slot, no) or slot.get("free") == "0"
            or slot.get("procedure", procedure) != procedure or not _partner_speaks(slot, lang)
            or (slot.get("urgent_reserve", "").lower() == "true" and priority != "U")
            or CAL.down(slot["facility"]) or CAL.check(slot["slot_id"], now=None, margin=_lead(), case=key)
        ):
            continue
        found.append((at, slot))
    return [slot for _, slot in sorted(found, key=lambda item: item[0])]


def _partner_refs(chat: list[Message], agent: str, facility: str, slot_id: str) -> tuple[str, str]:
    """The partner's own offer_id for ``slot_id`` and our request_id to it ("" for our own)."""

    if facility in _OUR_FACILITIES:
        return "", ""
    request = _asked_partners(chat, agent).get(facility, "")
    for slot in _partner_slots(chat, agent):
        if slot["facility"] == facility and slot["slot_id"] == slot_id:
            return slot.get("offer_id", ""), request
    return "", request


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
            if CAL.down(partner):
                continue
            try:
                await city.say(partner, text)
                reached += 1
            except Exception:  # noqa: BLE001 - CapError or an httpx timeout: the next partner still gets it
                CAL.mark(partner, "DOWN")  # never the same message again
        if not reached:
            return chat, False
        notice = _phrase(chat, "partners")
        if not protocol.exact(chat, agent, notice):
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
        chat = await _until(city, chat[-1].need_id if chat else None, chat, answered,
                            deadline=time.monotonic() + _PARTNER_WAIT)
    return chat, True


async def _try_partners(
    city: City, message: Message, chat: list[Message], citizen: str, number: str, priority: str | None,
    procedure: str, lang: str, now: datetime, born: datetime | None, no: case.Refusals,
    skip: set[str] | frozenset[str], replacing: bool,
) -> bool:
    """Ask the ring and offer what fits. False: nothing from the partners (yet), go on alone."""

    key = message.need_id or message.msg_id
    chat, asked = await _ask_partners(city, chat, citizen, procedure, priority, key, lang)
    if not asked:
        return False
    slots = _partner_fit(chat, city.agent_id, key, procedure, priority, lang, now, born, no, skip)
    if not slots:
        return False
    await _offer(city, key, citizen, _spread(slots, key), number, priority, replacing, chat, "partner_offers")
    return True


async def _partner_answer(city: City, message: Message, chat: list[Message], key: str) -> None:
    """A partner's answer that arrived after our turn stopped waiting: offer its slots once,
    unless the citizen is already being booked or already holds partner offers."""

    agent = city.agent_id
    citizen = protocol.citizen(chat)
    if citizen is None or _booking_stage(chat, agent, citizen):
        return
    offered = case.offered(chat, agent) | _sent(key).offers
    if any(slot["slot_id"] in offered for slot in _partner_slots(chat, agent)):
        return
    number = case.number(chat)
    reply = case.prescription(chat, agent, number) if number else None
    _round_trip(chat, agent)
    now = _now(chat)
    if not number or reply is None or not reply.get("procedure") or reply.get("patient", citizen) != citizen or now is None:
        return
    no = case.refusals(chat, agent, calendar.gender)
    lang = case.lang(chat, _heard, doctor=True)
    slots = _partner_fit(chat, agent, key, reply["procedure"], reply.get("priority"), lang, now, _born(chat), no, offered)
    if slots:
        await _offer(city, key, citizen, _spread(slots, key), number, reply.get("priority"), False, chat, "partner_offers")


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

    lang = case.lang(chat, _heard)
    if lang in ("it", "en"):
        return lang
    text = " ".join(row.text or "" for row in chat if row.sender.startswith("citizen:"))
    return "it" if len(_ITALIAN_WORDS.findall(text)) >= 3 else "en"


def _when_words(at: str, lang: str) -> str:
    """A Ledger timestamp in plain words, in the agenda's own clock: "martedì 10 novembre alle
    10:30". Empty when the timestamp cannot be read: we never guess a date."""

    when = protocol.when(at)
    if when is None:
        return ""
    lang = "en" if lang == "en" else "it"
    day, month = _DAY_WORDS[lang][when.weekday()], _MONTH_WORDS[lang][when.month - 1]
    joint = "at" if lang == "en" else "alle"
    return f"{day} {when.day} {month} {joint} {when:%H:%M}"


def _allowed(slot: dict[str, str], no: case.Refusals) -> bool:
    """Not ruled out by the citizen's refusals (case.refusals): the doctor, the weekday, the gender."""

    return (slot.get("doctor_id") not in no.doctors and slot.get("day") not in no.days
            and calendar.gender(slot.get("doctor_id", "")) not in no.genders)


def _offer_source(chat: list[Message], agent: str, offer_id: str) -> str:
    """The msg_id of our message that carried ``offer_id``."""

    for row in chat:
        if row.sender == agent and "offer" in words(row.text):
            if any(offer.get("offer_id") == offer_id for offer in case.offers_in(row.text)):
                return row.msg_id
    return ""


def _names_ours(chat: list[Message], agent: str, fields: dict[str, str]) -> bool:
    """Whether a cancel names one of OUR offers or a booking made from one: anything else from
    the city (a conformance probe, another case) is not ours to act on."""

    citizen = protocol.citizen(chat)
    offer_id = fields.get("offer_id", "")
    if offer_id and case.our_offer(chat, agent, offer_id):
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
    return case.our_offer(chat, agent, offer_id) if offer_id else {}


async def _citizen_cancel(city: City, message: Message, chat: list[Message]) -> None:
    """The citizen turned one of our offers down. While another offer of ours is still open the
    choice is theirs (a ``confirm`` usually follows); with none left we look again, leaving out
    what they refused."""

    agent, citizen = city.agent_id, message.sender
    if _booking_stage(chat, agent, citizen):
        return  # already booking another offer for them
    no = case.refusals(chat, agent, calendar.gender)
    for slot in no.slots:  # free again for the other citizens
        CAL.release(slot, message.need_id or message.msg_id)
    reason = fields_of(message.text).get("reason", "").lower()
    live = _live_offers(chat, agent, citizen)
    if live and all(offer.get("offer_id") != protocol.fields(message).get("offer_id") for offer in live):
        # One cancel per offer, all at once (21/11): the first turn that read them all already
        # answered with new offers. This one refuses an offer they replaced.
        return
    # The offers left that the refusals do not rule out (a day or a gender turned down rules out
    # the other offers on that day or with that gender too).
    open_offers = [
        offer for offer in live if offer.get("slot_id") not in no.slots
        and _allowed({"doctor_id": offer.get("doctor_id", ""), "day": offer.get("agenda_day", "")}, no)
    ]
    # Refused for who the doctor is: the other offers may fail the same way, so we look again.
    if open_offers and reason not in {"lang", "language", "gender", "doctor"}:
        text = _phrase(chat, "dropped")
        told = _sent(message.need_id or message.msg_id).said
        if text not in told and not protocol.exact(chat, agent, text):
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


def _spread(slots: list[dict[str, str]], key: str, limit: int = _MAX_OFFERS) -> list[dict[str, str]]:
    """Up to ``limit`` slots, one per day first and then one per doctor, in the order given.
    The citizen picks, among our offers, the one that meets what they did not tell us: the 15
    bookings in S outside S_pref were all Monday to Wednesday, their hidden days Thursday and
    Friday, and we never offered one of those. Slots another case was offered come only after
    every other one (CAL.shared)."""

    picked: list[dict[str, str]] = []
    for pool in ([slot for slot in slots if not CAL.shared(slot["slot_id"], key)], slots):
        for field in ("day", "doctor_id", ""):
            seen = {slot.get(field) for slot in picked} if field else set()
            for slot in pool:
                if len(picked) >= limit:
                    return picked
                if slot in picked or (field and slot.get(field) in seen):
                    continue
                picked.append(slot)
                seen.add(slot.get(field))
    return picked


_BORN_MARGIN = timedelta(hours=1)  # agenda time: one real minute before our first sight of the case


def _born(chat: list[Message]) -> datetime | None:
    """The agenda time at which the case started (its first row), a little earlier to be safe.
    Same clock as _now: a regenerated case (same need, half an hour later) keeps its birth."""

    _read_clock(chat)
    stamps = [at for at in (protocol.when(row.sent_at or "") for row in chat) if at is not None]
    born = _agenda_at(min(stamps)) if stamps else None
    return None if born is None else born - _BORN_MARGIN


# --- reading the case --------------------------------------------------------------------------


_GP = "agent:ck9w60ejwnvw"


async def _say(city: City, to: str, text: str) -> str:
    """protocol.say: the msg_id, or "". A citizen who heard from us is owed no replacement."""

    msg_id = await protocol.say(city, to, text)
    if msg_id and to == LEDGER and text in _EVERY:
        _ASKED[text] = time.monotonic()
    if msg_id and to.startswith("citizen:") and city.inbound is not None:
        owed = _SENT.get(city.inbound.need_id or city.inbound.msg_id)
        if owed is not None:
            owed.pending = False
            owed.said.discard("checking")  # a new silence may be told again (_waiting)
    return msg_id


def _world_page(chat: list[Message], page: int) -> str | None:
    """The latest answer for a page, in either Ledger world format."""
    for row in reversed(chat):
        head = first_word(row.text)
        if (
            row.sender == LEDGER and protocol.page(row.text)[0] == page
            and (head == "slots" or head == "world" and (
                "slots" in fields_of(row.text.split("\n", 1)[0]) or "slot_id=" in row.text
            ))
        ):
            return row.text
    return None


def _learn(chat: list[Message], agent: str) -> None:
    """Every row of a case history, once, to the calendar: world pages and the Ledger's answers,
    our offers, confirms and cancels (the claims of the pod before a restart, or of another pod).
    Every world page also to _PAGES under the procedure its slot lines name (an empty page: the
    one our latest world ask named), unless _PAGES holds a later Ledger answer."""

    asked = ""
    for row in chat:
        if row.recipient == LEDGER and first_word(row.text) == "world":
            asked = fields_of(row.text).get("procedure", "")
        if row.msg_id in _LEARNED:
            continue
        if row.msg_id:
            _LEARNED[row.msg_id] = None
            while len(_LEARNED) > _LEARNED_MAX:
                _LEARNED.popitem(last=False)
        CAL.learn(row, me=agent)
        page = protocol.page(row.text)[0]
        if _world_page([row], page) is None:
            continue
        named = {calendar.row(line).get("procedure") for line in row.text.splitlines() if "slot_id=" in line} - {None, ""}
        procedure = named.pop() if len(named) == 1 else "" if named else asked
        if not procedure:
            continue
        at, old = protocol.when(row.sent_at or ""), _PAGES.get((procedure, page))
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
        new, old = protocol.when(found.get("agenda_now", "")), protocol.when(_CLOCK.get("agenda_now", ""))
        if new is not None and (old is None or new >= old):
            _CLOCK.clear()
            _CLOCK.update(found, sent_at=row.sent_at or "")


def _agenda_at(real: datetime) -> datetime | None:
    """The agenda time at the real instant ``real``: ``epoch_agenda`` + (``real`` - ``epoch_real``)
    x ``rate``, weekdays only (no agenda weekends, R:80). Without the epoch fields, the reading's
    own agenda_now and sent_at; paused, or with no time to start from, the reading itself."""

    if _CLOCK.get("epoch_real") and _CLOCK.get("epoch_agenda"):
        base, start = protocol.when(_CLOCK["epoch_agenda"]), protocol.when(_CLOCK["epoch_real"])
    else:
        base, start = protocol.when(_CLOCK.get("agenda_now", "")), protocol.when(_CLOCK.get("sent_at", ""))
    if base is None or start is None or _CLOCK.get("paused") == "true":
        return protocol.when(_CLOCK.get("agenda_now", ""))
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
        at = protocol.when(row.sent_at or "")
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

    if _RTT is None:
        return _LEAD_UNMEASURED
    return _LEAD + timedelta(seconds=_RTT[1] * _rate())


def _renewal_clock(chat: list[Message], agent: str, number: str) -> tuple[bool, bool]:
    """After a renewal (we asked the Ledger about another number first: agenda days later, P1):
    (no agenda answer since our first ask for ``number``, an agenda ask of ours since then)."""

    asks = [i for i, row in enumerate(chat) if row.sender == agent and row.recipient == LEDGER
            and first_word(row.text) == "prescription"]
    first = next((i for i in asks if fields_of(chat[i].text).get("prescription_number") == number), None)
    if first is None or all(fields_of(chat[i].text).get("prescription_number") == number for i in asks if i < first):
        return False, False
    later = chat[first + 1:]
    return (not any(row.sender == LEDGER and first_word(row.text) == "agenda" for row in later),
            any(row.sender == agent and row.recipient == LEDGER and row.text.strip() == "agenda" for row in later))


def _has_agenda(chat: list[Message]) -> bool:
    agenda = protocol.ledger(chat, "agenda")
    return protocol.when("" if agenda is None else agenda.get("agenda_now", "")) is not None


def _due(read: str) -> bool:
    """Whether to ask the Ledger ``read`` ("agenda" or "rules") now; agenda always while the clock
    is unknown."""

    if read == "agenda" and not _CLOCK:
        return True
    return time.monotonic() - _ASKED.get(read, float("-inf")) >= _EVERY[read]


def _recent(chat: list[Message], sent: "_Sent") -> list[Message]:
    """Rows that answer our latest marking read (sent.mark). A history read before the ask shows neither
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
                protocol.when(fields_of(line).get("effective_agenda", "")),
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
    """protocol.until, giving every history it reads to the calendar and _PAGES (_learn)."""

    return await protocol.until(
        city, need_id, chat, ready, tries, deadline, pause=_PULL_PAUSE,
        on_read=lambda rows: _learn(rows, city.agent_id),
    )


def _answered(chat: list[Message], agent: str, message: Message) -> bool:
    """True when we already wrote to the sender of ``message`` after it in the case."""

    ids = [row.msg_id for row in chat]
    if message.msg_id not in ids:
        return False
    later = chat[ids.index(message.msg_id) + 1 :]
    return any(row.sender == agent and row.recipient == message.sender for row in later)


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
            latest = [offer for offer in case.offers_in(row.text) if offer.get("slot_id")]
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
        pending = case.consent_offer(chat, agent, citizen)
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
            row.sender.startswith("citizen:") and (protocol.fields(row).get("lang") or _heard(row).get("lang"))
            for row in chat
        )
        needs = (
            (offers and not pending and "offer_id" not in heard)
            or (pending and "consent" not in heard)
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
