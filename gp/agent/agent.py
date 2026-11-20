"""Medico di base per i percorsi simulati di Milano Agentic City."""

from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from weakref import WeakValueDictionary

from city_sdk.city import LEDGER, CapError, City, Message, Need, OutOfScope, clinical, understood


PROTOCOL = {
    "SYM-BACK": ("VIS-FIS", "B"),
    "SYM-KNEE": ("VIS-FIS", "D"),
    "SYM-PALPITATIONS": ("VIS-CARD", "B"),
    "SYM-FOLLOWUP": ("VIS-CARD", "P"),
}
SYMPTOM_PHRASES = {
    "SYM-BACK": ("mal di schiena", "back pain"),
    "SYM-KNEE": ("ginocchio", "knee"),
    "SYM-PALPITATIONS": ("palpitazioni", "palpitations", "cuore che batte", "heart racing"),
    "SYM-FOLLOWUP": ("controllo del cuore", "routine heart check"),
}
# Red flags in the citizen's own words (it/en, any tone), on text without accents. The city's
# protocol lists red flags as codes (RF-...), which a citizen does not write: words it is.
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
ESCALATION = (
    "escalate.emergency reason=red_flag Capisco la sua preoccupazione: è importante agire subito. "
    "Contatti subito il 112."
)
LEDGER_REFUSAL = "Non sono riuscito a registrare la ricetta. Può riprovare con una nuova richiesta."
RENEWAL_UNKNOWN = (
    "Non riesco a verificare questa ricetta sul Ledger. Può indicarmi il motivo della richiesta "
    "in poche parole? Così applico il protocollo."
)
LEDGER_WAIT_ATTEMPTS = 24
LEDGER_WAIT_SECONDS = 0.25
CASE_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
# Our ASST's booking centre (booking_centre/card.yaml). The Index Peer carries neither team nor
# district, and find() leaves this agent out: the id is the only way to pick ours. We still write
# to it only when the Index returns it.
BOOKING_CENTRE = "agent:sbdpbpqz5sxe"
HANDOFF = "need.request handoff=gp"
# Priority deadlines (e19-rules.md R:91).
TIMING = {
    "it": {
        "U": "la visita va fatta in giornata",
        "B": "la visita va fatta entro 2 giorni",
        "D": "la visita va fatta entro 5 giorni",
        "P": "è una visita programmabile, per cui il centro prenotazioni può proporle la lista d'attesa",
    },
    "en": {
        "U": "the visit is due today",
        "B": "the visit is due within 2 days",
        "D": "the visit is due within 5 days",
        "P": "it is a scheduled visit, so the booking centre can offer you the waiting list",
    },
}


@dataclass(frozen=True)
class Prescription:
    patient: str
    number: str
    procedure: str
    priority: str

    @property
    def head(self) -> str:
        return (
            f"prescription prescription_number={self.number} procedure={self.procedure} "
            f"priority={self.priority}"
        )

    def reply(self, lang: str, handed: bool) -> str:
        timing = TIMING[lang].get(self.priority)
        if lang == "en":
            text = (
                "I understand your concern and I have taken care of it right away. "
                f"Your prescription is registered with priority {self.priority}"
                + (f": {timing}. " if timing else ". ")
            )
            text += (
                "I have already passed it to our booking centre. If you have preferred days or a "
                "preference for the doctor's gender or language, tell the booking centre: it will "
                "find the slot that suits you best."
                if handed
                else "To book, write to your ASST's booking centre with the prescription number. If "
                "you have preferred days or a preference for the doctor's gender or language, tell "
                "them: they will find the slot that suits you best."
            )
        else:
            text = (
                "Capisco la sua preoccupazione e me ne sono occupato subito. "
                f"La ricetta è registrata con priorità {self.priority}"
                + (f": {timing}. " if timing else ". ")
            )
            text += (
                "Ho già passato la ricetta al nostro centro prenotazioni. Se ha giorni preferiti o "
                "preferenze sul genere o sulla lingua del medico, le indichi al centro prenotazioni: "
                "così troverà lo slot più adatto a lei."
                if handed
                else "Per prenotare scriva al centro prenotazioni della sua ASST con il numero della "
                "ricetta. Se ha giorni preferiti o preferenze sul genere o sulla lingua del medico, "
                "le indichi: così troverà lo slot più adatto a lei."
            )
        return f"{self.head} {text}"


async def main(need: Need, city: City) -> None:
    inbound = city.inbound
    if inbound is None:
        return
    message = understood(inbound)
    if message.sender == LEDGER:
        if message.need_id and message.text.split()[:1] in (["prescribed"], ["refused"]):
            async with _case_lock(message.need_id):
                history = await city.history(need_id=message.need_id)
                request = _previous_prescription(history, city.agent_id)
                if request is not None:
                    await _notify(city, history, request)
        return
    # Every citizen message: a word like "prescription" in the text changes the act understood()
    # reads, and a red flag must never be missed for that.
    if not message.sender.startswith("citizen:"):
        return

    text = (message.text or need.summary).casefold()
    if need.is_emergency or message.body.get("is_emergency") is True or red_flag(text):
        async with _case_lock(message.need_id or message.msg_id):
            history = await city.history(need_id=message.need_id) if message.need_id else []
            if not _sent(history, city.agent_id, message.sender, ESCALATION):
                await city.say(message.sender, ESCALATION)
        return
    if clinical(text):
        raise OutOfScope()
    old = message.body.get("prescription_number")
    if isinstance(old, str) and old and not message.body.get("symptom_code"):
        if not message.need_id:
            await city.say(message.sender, "Non riesco ad associare la richiesta a un percorso.")
            return
        async with _case_lock(message.need_id):
            await _renew(city, message.sender, message.need_id, old)
        return

    code = _symptom_code(message, text)
    if code is None:
        await city.say(message.sender, "Può indicarmi il motivo della richiesta in poche parole?")
        return
    if not message.need_id:
        await city.say(message.sender, "Non riesco ad associare la richiesta a un percorso.")
        return
    async with _case_lock(message.need_id):
        await _prescribe(city, message, message.need_id, code)


def _case_lock(need_id: str) -> asyncio.Lock:
    return CASE_LOCKS.setdefault(need_id, asyncio.Lock())


def red_flag(text: str) -> bool:
    plain = unicodedata.normalize("NFKD", text.replace("’", "'")).encode("ascii", "ignore")
    lowered = plain.decode().lower()
    return any(
        not _NEGATED.search(lowered[max(0, match.start() - 40) : match.start()])
        for match in _RED_FLAG.finditer(lowered)
    )


def _symptom_code(message: Message, text: str) -> str | None:
    code = message.body.get("symptom_code")
    if code is not None:
        return code if isinstance(code, str) and code in PROTOCOL else None
    matches = [
        code for code, phrases in SYMPTOM_PHRASES.items()
        if any(phrase in text for phrase in phrases)
    ]
    return matches[0] if len(matches) == 1 else None


async def _prescribe(city: City, message: Message, need_id: str, code: str) -> None:
    digest = sha256(f"{need_id}/{message.sender}".encode()).hexdigest()[:24].upper()
    procedure, priority = PROTOCOL[code]
    request = Prescription(message.sender, f"prescription_number-{digest}", procedure, priority)
    await _issue(city, need_id, request, f"symptom_code={code} ")


async def _renew(city: City, patient: str, need_id: str, old: str) -> None:
    """P1: a new prescription with the procedure and priority of ``old``, read from the Ledger."""

    history = await city.history(need_id=need_id)
    ask = f"prescription prescription_number={old}"
    if not _sent(history, city.agent_id, LEDGER, ask):
        try:
            await city.say(LEDGER, ask)
        except CapError:
            await city.say(patient, LEDGER_REFUSAL)
            return
    found = _ledger_rx(history, old)
    for _ in range(LEDGER_WAIT_ATTEMPTS):
        if found is not None:
            break
        await asyncio.sleep(LEDGER_WAIT_SECONDS)
        history = await city.history(need_id=need_id)
        found = _ledger_rx(history, old)
    procedure = None if found is None else found.get("procedure")
    priority = None if found is None else found.get("priority")
    # Someone else's prescription, or one the Ledger does not know: nothing is renewed.
    if (
        not isinstance(procedure, str)
        or not isinstance(priority, str)
        or found.get("patient", patient) != patient
    ):
        if not _sent(history, city.agent_id, patient, RENEWAL_UNKNOWN):
            await city.say(patient, RENEWAL_UNKNOWN)
        return
    digest = sha256(f"{need_id}/{patient}/{old}".encode()).hexdigest()[:24].upper()
    request = Prescription(patient, f"prescription_number-{digest}", procedure, priority)
    code = found.get("symptom_code")
    await _issue(city, need_id, request, f"symptom_code={code} " if isinstance(code, str) else "")


def _ledger_rx(history: list[Message], number: str) -> dict | None:
    """The Ledger's latest answer about prescription ``number``."""

    found = None
    for row in history:
        if row.sender == LEDGER and row.text.split()[:1] == ["prescription"]:
            fields = understood(row).body
            if fields.get("prescription_number") == number:
                found = dict(fields)
    return found


async def _issue(city: City, need_id: str, request: Prescription, extra: str) -> None:
    """Ledger first: ``prescribe``, then the prescription to the citizen once it is written."""

    history = await city.history(need_id=need_id)
    if _previous_prescription(history, city.agent_id, number=request.number) is None:
        try:
            await city.say(
                LEDGER,
                f"prescribe patient={request.patient} procedure={request.procedure} "
                f"priority={request.priority} {extra}prescription_number={request.number}",
            )
        except CapError:
            await city.say(request.patient, LEDGER_REFUSAL)
            return

    # In some runs a Ledger reply is visible in history before it opens an agent turn.
    for _ in range(LEDGER_WAIT_ATTEMPTS):
        if await _notify(city, history, request):
            return
        await asyncio.sleep(LEDGER_WAIT_SECONDS)
        history = await city.history(need_id=need_id)
    await _notify(city, history, request)


def _previous_prescription(
    history: list[Message], agent_id: str, *, number: str | None = None
) -> Prescription | None:
    for row in reversed(history):
        if row.sender != agent_id or row.recipient != LEDGER or not row.text.startswith("prescribe "):
            continue
        fields = understood(row).body
        patient, rx, procedure, priority = (
            fields.get("patient"),
            fields.get("prescription_number"),
            fields.get("procedure"),
            fields.get("priority"),
        )
        if not isinstance(patient, str) or not patient.startswith("citizen:"):
            continue
        if isinstance(rx, str) and isinstance(procedure, str) and isinstance(priority, str):
            if number is None or rx == number:
                return Prescription(patient, rx, procedure, priority)
    return None


async def _notify(city: City, history: list[Message], request: Prescription) -> bool:
    answer = None
    matching_request = False
    for row in history:
        if row.sender == city.agent_id and row.recipient == LEDGER and row.text.startswith("prescribe "):
            matching_request = understood(row).body.get("prescription_number") == request.number
        if row.sender != LEDGER:
            continue
        words = row.text.split()
        fields = understood(row).body
        number = fields.get("prescription_number")
        # The Ledger can omit the number, particularly when it refuses a write.
        if number != request.number and (number is not None or not matching_request):
            continue
        if fields.get("patient", request.patient) != request.patient:
            continue
        if words[:1] == ["prescribed"] and (
            fields.get("procedure", request.procedure) == request.procedure
            and fields.get("priority", request.priority) == request.priority
        ):
            answer = request.head
            break
        if words[:1] == ["refused"] and "prescribe" in words and "DUPLICATE" not in row.text:
            answer = LEDGER_REFUSAL
    if answer is None:
        return False
    if answer == LEDGER_REFUSAL:
        if not _sent(history, city.agent_id, request.patient, answer):
            await city.say(request.patient, answer)
        return True
    if not any(
        row.sender == city.agent_id and row.recipient == request.patient and row.text.startswith(answer + " ")
        for row in history
    ):
        lang = _lang(history)
        # Before the citizen's message, so that "already passed on" is true when we say it.
        handed = await _handoff(city, history, request, lang)
        await city.say(request.patient, request.reply(lang, handed))
    return True


def _lang(history: list[Message]) -> str:
    """The citizen's ``lang=``, as the booking centre reads it; Italian otherwise."""

    for row in history:
        if row.sender.startswith("citizen:"):
            lang = understood(row).body.get("lang")
            if lang in TIMING:
                return lang
    return "it"


async def _handoff(city: City, history: list[Message], request: Prescription, lang: str) -> bool:
    """Best effort, once per need: the prescription to our booking centre. Never raises."""

    done = [row.text for row in history if row.sender == city.agent_id and row.text.startswith(HANDOFF + " ")]
    if done:  # once per need; a renewal's new number was not passed on, so we do not claim it
        return any(f" prescription_number={request.number} " in text for text in done)
    try:
        peers = [peer for peer in await city.find("booking_centre") if peer.agent_id == BOOKING_CENTRE]
        if len(peers) != 1:
            return False
        await city.say(
            peers[0],
            f"{HANDOFF} citizen={request.patient} prescription_number={request.number} "
            f"procedure={request.procedure} priority={request.priority} lang={lang} "
            "Buongiorno, vi passo la ricetta appena registrata sul Ledger per la prenotazione. Grazie!",
        )
        return True
    except Exception:  # CapError (TARGET_NOT_VISIBLE...) or network: no retry, the citizen still hears from us
        return False


def _sent(history: list[Message], agent_id: str, recipient: str, text: str) -> bool:
    return any(
        row.sender == agent_id and row.recipient == recipient and row.text == text
        for row in history
    )
