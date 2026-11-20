"""Medico di base per i percorsi simulati di Milano Agentic City."""

from __future__ import annotations

import asyncio
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
RED_FLAGS = (
    "perdita di forza alle gambe",
    "loss of strength in the legs",
    "dolore al petto",
    "chest pain",
)
ESCALATION = "escalate.emergency reason=red_flag Contatti subito il 112."
LEDGER_REFUSAL = "Non sono riuscito a registrare la ricetta. Può riprovare con una nuova richiesta."
LEDGER_WAIT_ATTEMPTS = 24
LEDGER_WAIT_SECONDS = 0.25
CASE_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


@dataclass(frozen=True)
class Prescription:
    patient: str
    number: str
    procedure: str
    priority: str

    @property
    def reply(self) -> str:
        return (
            f"prescription prescription_number={self.number} procedure={self.procedure} "
            f"priority={self.priority} La ricetta è registrata. "
            "Può usare il numero al centro prenotazioni."
        )


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
    if not message.sender.startswith("citizen:") or message.type != "need.request":
        return

    text = (message.text or need.summary).casefold()
    if need.is_emergency or message.body.get("is_emergency") is True or any(
        flag in text for flag in RED_FLAGS
    ):
        async with _case_lock(message.need_id or message.msg_id):
            history = await city.history(need_id=message.need_id) if message.need_id else []
            if not _sent(history, city.agent_id, message.sender, ESCALATION):
                await city.say(message.sender, ESCALATION)
        return
    if clinical(text):
        raise OutOfScope()
    if message.body.get("prescription_number") and not message.body.get("symptom_code"):
        await city.say(message.sender, "Al momento il rinnovo della ricetta non è disponibile.")
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
    history = await city.history(need_id=need_id)
    digest = sha256(f"{need_id}/{message.sender}".encode()).hexdigest()[:24].upper()
    number = f"prescription_number-{digest}"
    request = _previous_prescription(history, city.agent_id, number=number)
    if request is None:
        procedure, priority = PROTOCOL[code]
        request = Prescription(message.sender, number, procedure, priority)
        try:
            await city.say(
                LEDGER,
                f"prescribe patient={request.patient} procedure={request.procedure} "
                f"priority={request.priority} symptom_code={code} prescription_number={request.number}",
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
            answer = request.reply
            break
        if words[:1] == ["refused"] and "prescribe" in words and "DUPLICATE" not in row.text:
            answer = LEDGER_REFUSAL
    if answer is None:
        return False
    if not _sent(history, city.agent_id, request.patient, answer):
        await city.say(request.patient, answer)
    return True


def _sent(history: list[Message], agent_id: str, recipient: str, text: str) -> bool:
    return any(
        row.sender == agent_id and row.recipient == recipient and row.text == text
        for row in history
    )
