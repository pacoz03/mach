"""Medico di base per i percorsi simulati di Milano Agentic City."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import unicodedata
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from weakref import WeakValueDictionary

from city_sdk.city import LEDGER, CapError, City, Message, Need, OutOfScope, Peer, clinical, understood

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import tools  # noqa: E402  (shared/tools.py, copied by shared/sync.sh)
from mach import protocol, safety  # noqa: E402  (shared/mach/, copied by shared/sync.sh)

# The citizen's language in this turn: our texts exist in Italian and English, any other language
# (named by the level-3 reading) is a translation by the model (tools.localize).
_LANG: ContextVar[str | None] = ContextVar("lang", default=None)


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
ESCALATE = "escalate.emergency reason=red_flag"
# (case, citizen) escalated by this pod: the act once, without reading anything first.
_ESCALATED: set[tuple[str, str]] = set()  # ponytail: per pod; after a restart a new red flag escalates again
# No protocol code in the citizen's words, not even for the model: we ask once more, warmly, and
# say what this desk handles. Naming the protocol's reasons is not clinical advice.
ASK_REASON = {
    "it": (
        "Buongiorno, sono qui per aiutarla. Mi descrive in poche parole il disturbo per cui vuole "
        "farsi visitare? Per esempio mal di schiena, dolore al ginocchio, palpitazioni o il "
        "controllo periodico del cuore: così le preparo subito la ricetta giusta."
    ),
    "en": (
        "Hello, I am here to help. Could you tell me in a few words what you would like to be "
        "seen for? For example back pain, knee pain, palpitations or a routine heart check: I "
        "will prepare the right prescription for you straight away."
    ),
}
LEDGER_REFUSAL = "Non sono riuscito a registrare la ricetta. Può riprovare con una nuova richiesta."
# P1 renewal of a number this case does not show as ours: the Ledger is asked, and answers in its
# own turn (live 21/11: 4-5 minutes). Meanwhile the citizen hears what happens, never silence.
RENEWAL_CHECKING = {
    "it": (
        "Sto verificando sul Ledger la ricetta {number}: appena ho la conferma gliela rinnovo, con "
        "la stessa visita e la stessa priorità, e le scrivo subito il numero nuovo. Non deve fare "
        "altro."
    ),
    "en": (
        "I am checking prescription {number} on the Ledger: as soon as it is confirmed I will renew "
        "it, with the same visit and priority, and write to you with the new number straight away. "
        "You don't need to do anything else."
    ),
}
# Not found, or not the citizen's: nothing is renewed and nothing about it is told (no disclosure).
RENEWAL_NOT_FOUND = "data.response purpose=prescription_renewal outcome=not_found"
RENEWAL_UNKNOWN = {
    "it": (
        "Ho verificato sul Ledger: la ricetta {number} non risulta a suo nome, quindi non posso "
        "rinnovarla. Se mi descrive in poche parole il disturbo per cui vuole farsi visitare, le "
        "preparo una ricetta nuova secondo il protocollo."
    ),
    "en": (
        "I checked the Ledger: prescription {number} is not registered in your name, so I cannot "
        "renew it. If you tell me in a few words what you would like to be seen for, I will "
        "prepare a new prescription according to the protocol."
    ),
}
CASE_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
# Our ASST's booking centre (booking_centre/card.yaml). The Index Peer carries neither team nor
# district, and find() leaves this agent out: the id is the only way to pick ours. We still write
# to it only when the Index returns it.
BOOKING_CENTRE = "agent:sbdpbpqz5sxe"
HANDOFF = "need.request handoff=gp"
# Our facilities: after a booking of our booking centre they tell us, unasked (facility _tell_gp).
FACILITIES = {
    "agent:m6py14a1ej77": ("l'ospedale", "the hospital"),
    "agent:h6krzxx6es83": ("l'ambulatorio di cardiologia", "the cardiology clinic"),
}
_BOOKED = {
    "it": "Buongiorno, sono il suo medico di base. {place} mi ha appena scritto che la sua visita è "
          "prenotata e confermata (codice {booking_id}): bene, è tutto a posto. Con me non deve fare altro; "
          "per l'arrivo segua le indicazioni che le ha dato la struttura. Se prima della visita si sente "
          "peggio o compare un disturbo nuovo mi scriva; in emergenza chiami il 112. Dopo la visita, se le "
          "serve altro, sono qui.",
    "en": "Hello, this is your GP. {place} has just told me that your visit is booked and confirmed "
          "(code {booking_id}): good, everything is in place. There is nothing more to do with me; for your "
          "arrival follow the facility's instructions. If you feel worse or something new comes up before "
          "the visit, write to me; in an emergency call 112. After the visit, if you need anything else, "
          "I am here.",
}
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
    renews: str = ""  # P1: the number this prescription renews

    @property
    def head(self) -> str:
        return (
            f"prescription prescription_number={self.number} procedure={self.procedure} "
            f"priority={self.priority}"
        )

    def reply(self, lang: str, handed: bool, prefs: dict[str, object], *, again: bool = False) -> str:
        """To the person: their reason (their words, no diagnosis), the prescription, one concrete
        next step, their preferences and, unasked, the renewal (P1). ``again``: they wrote again
        after it, so what is ready and what happens next, never the reason asked again."""

        say = _SAY[lang]
        visit = _VISIT[lang].get(self.procedure, self.procedure)
        if self.renews:
            text = say["renewed_again" if again else "renewed"].format(old=self.renews, visit=visit, rx=self)
        else:
            reason = _REASON[lang].get(_code_of(self))
            text = ("" if again else say["thanks"].format(reason) if reason else say["thanks_"]) + say[
                "again" if again else "ready"].format(visit=visit, rx=self)
        timing = TIMING[lang].get(self.priority)
        text += (f": {timing}. " if timing else ". ") + (say["handed" if self.priority != "P" else "handed_p"] if handed else say["write"].format(rx=self))
        if noted := _describe(prefs, lang):
            text += say["passed" if handed and not again else "noted"].format(noted)
        else:
            text += say["ask"]
        return f"{self.head} {text}{say['end']}"


# What we tell the citizen with their prescription (R:248: never only the number).
_SAY = {
    "it": {
        "thanks": "Grazie per avermi descritto {}. ",
        "thanks_": "Grazie per avermi scritto. ",
        "ready": "Le ho preparato la ricetta per una {visit}, numero {rx.number}, priorità {rx.priority}",
        "again": "Le ho già preparato la ricetta per una {visit}, numero {rx.number}, priorità {rx.priority}",
        "renewed": "Le ho rinnovato la ricetta {old}: la nuova ricetta per una {visit} ha numero {rx.number}, "
                   "priorità {rx.priority}",
        "renewed_again": "Le ho già rinnovato la ricetta {old}: la nuova ricetta per una {visit} ha numero "
                         "{rx.number}, priorità {rx.priority}",
        "handed": "Passo successivo: non deve ripetere nulla. Di mia iniziativa ho già scritto al nostro centro "
                  "prenotazioni: gli ho passato la ricetta e gli ho segnalato la scadenza della priorità, così le "
                  "proporrà solo date utili. Le scriverà con gli orari disponibili e le basterà rispondere per "
                  "confermarne uno. ",
        # Priority P has no deadline to flag (_handoff): only the prescription went.
        "handed_p": "Passo successivo: non deve ripetere nulla. Di mia iniziativa ho già passato la ricetta al "
                    "nostro centro prenotazioni: le scriverà e le basterà rispondere. ",
        "write": "Passo successivo: scriva al centro prenotazioni della sua ASST con il numero {rx.number}: le "
                 "proporrà gli orari disponibili. ",
        "passed": "Gli ho indicato anche le sue preferenze ({}). ",
        "noted": "Ho preso nota delle sue preferenze ({}): le confermi al centro prenotazioni. ",
        "ask": "Se ha preferenze sui giorni, sul genere o sulla lingua del medico, le dica al centro prenotazioni. ",
        "end": "Conservi il numero della ricetta: se scade prima che riesca a prenotare, mi scriva e gliela "
               "rinnovo io. Se si sente peggio o compare un disturbo nuovo, mi scriva.",
    },
    "en": {
        "thanks": "Thank you for telling me about {}. ",
        "thanks_": "Thank you for writing. ",
        "ready": "I have prepared your prescription for a {visit}, number {rx.number}, priority {rx.priority}",
        "again": "I have already prepared your prescription for a {visit}, number {rx.number}, priority {rx.priority}",
        "renewed": "I have renewed your prescription {old}: the new prescription for a {visit} is number "
                   "{rx.number}, priority {rx.priority}",
        "renewed_again": "I have already renewed your prescription {old}: the new prescription for a {visit} is "
                         "number {rx.number}, priority {rx.priority}",
        "handed": "Next step: you do not need to repeat anything. On my own initiative I have already written to "
                  "our booking centre: I passed on the prescription and flagged the priority deadline, so it will "
                  "only offer you dates that work. It will write to you with the available slots, and you only "
                  "need to reply to confirm one. ",
        "handed_p": "Next step: you do not need to repeat anything. On my own initiative I have already passed the "
                    "prescription to our booking centre: it will write to you and you only need to reply. ",
        "write": "Next step: write to your ASST's booking centre with the number {rx.number}: it will offer you "
                 "the available slots. ",
        "passed": "I have also passed on your preferences ({}). ",
        "noted": "I have noted your preferences ({}): confirm them to the booking centre. ",
        "ask": "If you have a preference for days, the doctor's gender or language, tell the booking centre. ",
        "end": "Keep the prescription number: if it expires before you manage to book, write to me and I will "
               "renew it. If you feel worse or something new comes up, write to me.",
    },
}


# The reason in the citizen's own words (the protocol's symptom, never a diagnosis) and the visit.
_REASON = {
    "it": {
        "SYM-BACK": "il suo mal di schiena",
        "SYM-KNEE": "il dolore al ginocchio",
        "SYM-PALPITATIONS": "le palpitazioni",
        "SYM-FOLLOWUP": "il controllo periodico del cuore",
    },
    "en": {
        "SYM-BACK": "your back pain",
        "SYM-KNEE": "your knee pain",
        "SYM-PALPITATIONS": "your palpitations",
        "SYM-FOLLOWUP": "your routine heart check",
    },
}
_VISIT = {
    "it": {"VIS-FIS": "visita fisiatrica", "VIS-CARD": "visita cardiologica"},
    "en": {"VIS-FIS": "physiatry visit", "VIS-CARD": "cardiology visit"},
}


def _code_of(rx: Prescription) -> str | None:
    """PROTOCOL is one-to-one: procedure and priority name the reason."""

    return next((code for code, pair in PROTOCOL.items() if pair == (rx.procedure, rx.priority)), None)


async def main(need: Need, city: City) -> None:
    """One turn. Only OutOfScope (the clinical refusal) leaves it: any other exception out of the
    brain is a crash or, an httpx timeout, a redelivery that says every message of the turn twice
    (city_sdk harness.py ``_think``/``_deferred_turn``)."""

    if getattr(need, "summary", None) == "__error__":
        raise RuntimeError("conformance: __error__")  # the conformance probe wants it uncaught
    if city.inbound is not None and tools.too_long(city.inbound):  # an attack message: no work on its text
        return
    msg_id = getattr(city.inbound, "msg_id", "")
    if msg_id and msg_id in _DONE:  # a redelivery of a finished turn: it said everything already
        return
    try:
        await _turn(need, city)
    except OutOfScope:
        raise
    except Exception:  # noqa: BLE001 - logged; the turn ends here, never redelivered
        logging.getLogger("care").exception("care.turn.failed")
    if msg_id:
        _DONE.add(msg_id)


# msg_ids of the turns this pod finished, as the SDK's journal (harness.invoke answers 409 to them).
# Only finished turns: a turn cut by a timeout is retried. Without it a redelivered "Grazie" or
# symptom gets the reminder twice, now that the case's prescription is ours from the prescribe.
_DONE: set[str] = set()  # ponytail: per pod and unbounded, one id per message


async def _turn(need: Need, city: City) -> None:
    inbound = city.inbound
    if inbound is None:
        return
    message = understood(inbound)
    _LANG.set(None)  # never the language of a previous turn
    if message.sender == LEDGER:
        tools.start_turn(inbound)  # the team's trace (episodes): timings per turn
        words = message.text.split()
        # ``prescribed`` needs nothing: the citizen had the prescription when we wrote it.
        if message.need_id and words[:1] in (["refused"], ["prescription"]):
            async with _case_lock(message.need_id):
                history = await city.history(need_id=message.need_id)
                old = understood(message).body.get("prescription_number")
                patient = _renewal_asker(history, city.agent_id, old)
                if patient is not None:  # P1: the Ledger's answer about a number to renew
                    await _renew(city, history, patient, message.need_id, old, None, reply=False)
                elif (words[:1] == ["refused"] and "prescribe" in words and "DUPLICATE" not in message.text
                      and (rx := _issued(history, city.agent_id))
                      and not _sent(history, city.agent_id, rx.patient, LEDGER_REFUSAL)):
                    await _say(city, rx.patient, LEDGER_REFUSAL)
        return
    if message.sender == BOOKING_CENTRE:
        tools.start_turn(inbound)
        return
    if message.sender in FACILITIES:
        tools.start_turn(inbound)
        await _booked(city, message)
        return
    # Every citizen message: a word like "prescription" in the text changes the act understood()
    # reads, and a red flag must never be missed for that.
    if not message.sender.startswith("citizen:"):
        return

    said = message.text or need.summary or ""
    text = said.casefold()
    lang = _guess_lang(text)
    # Before any read (history, model, memory): live 21/11 05:15, 50 citizens wrote in the same
    # second and 10 red flags were escalated only after the wave had closed (-2 each).
    if need.is_emergency or message.body.get("is_emergency") is True or safety.red_flag(said):
        await _escalate(city, message, lang)
        return
    if clinical(text):
        raise OutOfScope()
    tools.start_turn(inbound)  # after the clinical refusal, never before (AGENT.md)
    old = message.body.get("prescription_number")
    if isinstance(old, str) and old and not message.body.get("symptom_code"):
        if not message.need_id:
            await _say(city, message.sender, "Non riesco ad associare la richiesta a un percorso.")
            return
        async with _case_lock(message.need_id):
            history = await city.history(need_id=message.need_id)
            # Often only the number (R:720): the case's language, not a guess on the tokens.
            await _renew(city, history, message.sender, message.need_id, old, None)
        return

    code = _symptom_code(message, text)
    if code is not None and "symptom_code" not in message.body:
        # Free text the rules matched (level 8: a red flag hidden beside the protocol reason, in
        # words the rules do not know). The model reads it for the red flag only; the code stays
        # the rules' one.
        read = await _read_with_model(city, said)
        lang = str(read.get("lang", lang))
        if read.get("red_flag") is True:
            await _escalate(city, message, lang)
            return
    elif code is None:
        # Level 3: the citizen's own words, in any language. The rules found nothing, so the model
        # reads them once; only a protocol code, a yes/no red flag and a language come back.
        read = await _read_with_model(city, said)
        lang = str(read.get("lang", lang))
        if read.get("red_flag") is True:
            await _escalate(city, message, lang)
            return
        code = read.get("code")
    _LANG.set(lang)
    if not message.need_id:
        if code is None:
            await _tell(city, message.sender, ASK_REASON[_reply_lang(lang)])
        else:
            await _say(city, message.sender, "Non riesco ad associare la richiesta a un percorso.")
        return
    async with _case_lock(message.need_id):
        history = await city.history(need_id=message.need_id)
        issued = _issued(history, city.agent_id, message.sender)
        if issued is None:
            if code is None:
                await _tell(city, message.sender, ASK_REASON[_reply_lang(lang)])
            else:
                await _prescribe(city, history, message, code, lang)
            return
        # The case already has its prescription (our prescribe: the Ledger confirms minutes later).
        # A thank-you, a question, the same symptom in other words: what is ready and what happens
        # next, never silence. A renewal asked in words renews that prescription (P1). Only a
        # different protocol reason gets a prescription of its own.
        if _RENEWAL.search(text) and code is None:
            # "Grazie per il rinnovo" after one: the prescription it renewed, so _renew finds the
            # renewal already told and reminds it, never a third one.
            ours = (rx.number for rx in _prescribed(history, city.agent_id))
            old = next((n for n in ours if _renewal_number(message.need_id, message.sender, n) == issued.number),
                       issued.number)
            await _renew(city, history, message.sender, message.need_id, old, lang)
        elif code is None or PROTOCOL[code] == (issued.procedure, issued.priority):
            prefs = {**_case_preferences(history, message.sender), **_preferences(said)}
            handed = _handed(history, city.agent_id, issued.number)
            await _tell(city, message.sender, issued.reply(_reply_lang(lang), handed, prefs, again=True))
        else:
            await _prescribe(city, history, message, code, lang, again=True)


async def _booked(city: City, message: Message) -> None:
    """Our facility told us, unasked, that our patient is booked: a word of ours to the patient,
    once per booking. Only for a prescription we wrote in this case (its patient, from our own
    ``prescribe``); nothing else of the facility's message is passed on."""

    text = message.text or ""
    tokens = dict(re.findall(r"(\w+)=(\S+)", text))
    if text.split()[:1] != ["data.response"] or tokens.get("purpose") != "booking":
        return
    booking_id, number = tokens.get("booking_id", ""), tokens.get("prescription_number", "")
    if not booking_id or not number or not message.need_id:
        return
    async with _case_lock(message.need_id):
        history = await city.history(need_id=message.need_id)
        rx = _issued(history, city.agent_id, number=number)
        if rx is None or any(
            row.sender == city.agent_id and row.recipient == rx.patient and booking_id in row.text
            for row in history
        ):
            return
        lang = _lang(history)
        place = FACILITIES[message.sender][0 if lang == "it" else 1]
        await _say(city, rx.patient, _BOOKED[lang].format(place=place[:1].upper() + place[1:], booking_id=booking_id))


def _case_lock(need_id: str) -> asyncio.Lock:
    return CASE_LOCKS.setdefault(need_id, asyncio.Lock())


async def _escalate(city: City, message: Message, lang: str) -> None:
    """``escalate.emergency`` at once, no lock and no read before it; once per case and citizen."""

    key = (message.need_id or message.msg_id, message.sender)
    if key in _ESCALATED:
        return
    _ESCALATED.add(key)
    await _say(city, message.sender, f"{ESCALATE} {safety.escalation_text(_reply_lang(lang))}")


def _reply_lang(lang: str | None) -> str:
    """Our texts exist in Italian and English: Italian for Italian, English for any other language."""

    return "it" if lang in (None, "", "it") else "en"


# --- free text (level 3) -----------------------------------------------------------------------
# Rules first: instant and free. The team model (city.llm, city-default) is asked only when the
# rules find no symptom, under a short timeout. Its JSON is checked against PROTOCOL: it can pick
# a code, never invent a procedure or a priority. CapError, timeout or bad JSON: the rules stand.
# Measured on city-default (2026-11-20, 62 calls): ~0.4 s, or 12-13 s (max 12.97) about one call
# in four, nothing in between. Team budget 2 s in the turn (21/11): a slow call holds the turn,
# so past it the rules stand alone (``{}``); the red flag never waits for the model (safety first).
_LLM_TIMEOUT = 2.0
_LLM_SYSTEM = (
    "You read one message a citizen wrote to their family doctor's desk. The message can be in "
    "any language. Reply with one JSON object and nothing else, with these keys:\n"
    '- "code": the protocol code of the reason for the visit, one of\n'
    "  SYM-BACK = back pain, lower back, lumbar pain\n"
    "  SYM-KNEE = knee pain or a knee problem\n"
    "  SYM-PALPITATIONS = palpitations, racing, pounding or irregular heartbeat\n"
    "  SYM-FOLLOWUP = routine heart check-up or cardiology follow-up, no new symptoms\n"
    "  NONE = anything else (thanks, questions, other complaints) or unclear\n"
    "  If the message names more than one of the above, give the one the citizen asks to be "
    "seen for first; NONE if you cannot tell.\n"
    '- "red_flag": true if the citizen says they have NOW chest pain, shortness of breath, '
    "fainting, bleeding that does not stop, sudden loss of strength in the legs, loss of bladder "
    "or bowel control, stroke signs or seizures; a negated or past symptom is not a red flag; "
    "false otherwise\n"
    '- "lang": the ISO 639-1 code of the language the message is written in'
)
# English without a lang= token: enough plain English words to answer in English.
_ENGLISH = frozenset(
    # No words Italian also uses ("a", "i", "in", "me"): "ciao a tutti" stays Italian.
    "i'm i've my the an and have has been is am was it to for of on at with when "
    "since get feel can you your need would like please hello hi good morning thanks back pain "
    "knee heart check doctor hurts night day days week weeks".split()
)


def _guess_lang(text: str) -> str:
    words = re.findall(r"[a-z']+", text)
    return "en" if words and sum(w in _ENGLISH for w in words) * 3 >= len(words) else "it"


async def _read_with_model(city: City, text: str) -> dict[str, object]:
    """One city.llm call: ``code`` (a PROTOCOL key), ``red_flag`` (bool), ``lang``. Never raises."""

    try:
        answer = await asyncio.wait_for(
            city.llm([{"role": "system", "content": _LLM_SYSTEM}, {"role": "user", "content": text[:1000]}]),
            _LLM_TIMEOUT,
        )
        content = re.sub(r"<think>.*?</think>", "", str(answer.get("content", "")), flags=re.S)
        match = re.search(r"\{.*\}", content, re.S)
        data = json.loads(match.group(0)) if match else {}
    except Exception:  # noqa: BLE001 - CapError, timeout, bad JSON: the rules stand alone
        return {}
    if not isinstance(data, dict):
        return {}
    read: dict[str, object] = {}
    code = str(data.get("code", "")).strip().upper()
    if code in PROTOCOL:
        read["code"] = code
    flag = data.get("red_flag")
    if flag is True or (isinstance(flag, str) and flag.strip().lower() in ("true", "yes")):
        read["red_flag"] = True
    lang = str(data.get("lang", "")).strip().lower()
    if re.fullmatch(r"[a-z]{2}", lang):
        read["lang"] = lang
    return read


def _symptom_code(message: Message, text: str) -> str | None:
    code = message.body.get("symptom_code")
    if code is not None:
        return code if isinstance(code, str) and code in PROTOCOL else None
    matches = [
        code for code, phrases in SYMPTOM_PHRASES.items()
        if any(phrase in text for phrase in phrases)
    ]
    return matches[0] if len(matches) == 1 else None


# --- the citizen's preferences (level 3) -------------------------------------------------------
# What the citizen tells the gp about the visit they want: the doctor's language and gender, the
# days they can come. We confirm them and pass them to the booking centre with the prescription,
# with the tokens it reads from a citizen (lang/doctor_lang, gender_preference, days). Rules only,
# never guesses: a preference we are not sure of is left to the booking centre to ask.
_LANG_NAMES = {
    "en": r"ingles\w*|english|anglofon\w*",
    "sq": r"albanes\w*|albanian",
    "es": r"spagnol\w*|spanish|espa[nñ]ol",
    "fr": r"frances\w*|french|fran[cç]ais",
    "de": r"tedesc\w*|german|deutsch",
    "ar": r"arab\w*",
    "ro": r"romen\w*|rumen\w*|romanian",
    "zh": r"cines\w*|chinese",
    "ru": r"russ[oia]\b|russian",
    "uk": r"ucrain\w*|ukrainian",
    "pt": r"portoghes\w*|portuguese",
}
# A language is a preference for the doctor only next to a word about speaking it.
_SPEAK = r"(?:speak\w*|speaker|parl\w*|lingua|language|madrelingua|-speaking)"
_DOCTOR_LANG = {
    code: re.compile(rf"{_SPEAK}\W+(?:\w+\W+){{0,3}}?(?:{names})|(?:{names})(?:\W+\w+){{0,2}}?\W*{_SPEAK}")
    for code, names in _LANG_NAMES.items()
}
_FEMALE = re.compile(
    r"\b(?:una|la|da una|con una) dottoressa\b|\bmedic[oa] donna\b|\bdottor\w* donna\b|\buna donna\b"
    r"|\b(?:female|woman|lady) (?:doctor|physician|physio\w*)\b|\ba woman\b"
)
_MALE = re.compile(
    r"\bmedico uomo\b|\bdottor\w* uomo\b|\bun uomo\b|\b(?:male|man) (?:doctor|physician|physio\w*)\b"
    r"|\ba man\b"
)
_DAY_NAMES = {
    "mon": r"luned[iì]|monday", "tue": r"marted[iì]|tuesday", "wed": r"mercoled[iì]|wednesday",
    "thu": r"gioved[iì]|thursday", "fri": r"venerd[iì]|friday",
}
_DAY = re.compile("|".join(rf"(?P<{day}>\b(?:{names})s?\b)" for day, names in _DAY_NAMES.items()))
# A weekday is a wish only in a sentence that says so ("preferisco il lunedì", "I can only do
# Fridays"), never "da lunedì ho mal di schiena" / "since Monday".
_WISH = re.compile(
    r"prefer\w*|meglio|possibilmente|disponibil\w*|liber[oa]\b|posso|potrei|riesco|va bene|vanno bene"
    r"|ideally|available|free\b|\bcan\b|could|works?\b|suits?\b|better|only\b|solo\b|soltanto"
)
_PAST = re.compile(r"\b(?:da|dal|dalla|dallo|scorso|scorsa|since|last|ago|fa)\s+(?:\w+\s+)?$")
_NOT_DAY = re.compile(
    r"\b(?:non|no|not|never|tranne|eccetto|except|but not|n't)\b\W+(?:\w+\W+){0,3}?$|n't\s+(?:\w+\s+){0,3}$"
)
_WEEK = ("mon", "tue", "wed", "thu", "fri")


def _preferences(text: str) -> dict[str, object]:
    """``doctor_lang`` (ISO code), ``gender`` (f/m), ``days`` (sorted tuple of mon..fri)."""

    plain = unicodedata.normalize("NFKC", text.replace("’", "'")).casefold()
    prefs: dict[str, object] = {}
    langs = [code for code, pattern in _DOCTOR_LANG.items() if pattern.search(plain)]
    if len(langs) == 1:
        prefs["doctor_lang"] = langs[0]
    female, male = bool(_FEMALE.search(plain)), bool(_MALE.search(plain))
    if female != male:
        prefs["gender"] = "f" if female else "m"
    wanted: set[str] = set()
    excluded: set[str] = set()
    for sentence in re.split(r"[.!?;\n]+", plain):
        if not _WISH.search(sentence):
            continue
        for match in _DAY.finditer(sentence):
            before = sentence[max(0, match.start() - 40) : match.start()]
            if _PAST.search(before):
                continue
            (excluded if _NOT_DAY.search(before) else wanted).add(match.lastgroup)
    days = (wanted or (set(_WEEK) if excluded else set())) - excluded
    if days and len(days) < len(_WEEK):
        prefs["days"] = tuple(day for day in _WEEK if day in days)
    return prefs


def _case_preferences(history: list[Message], patient: str) -> dict[str, object]:
    """The preferences the citizen gave the gp in this case; a later message wins per field."""

    prefs: dict[str, object] = {}
    for row in history:
        if row.sender == patient and row.recipient != LEDGER:
            prefs.update(_preferences(row.text or ""))
    return prefs


_LANG_LABEL = {
    "it": {"en": "inglese", "sq": "albanese", "es": "spagnolo", "fr": "francese", "de": "tedesco",
           "ar": "arabo", "ro": "rumeno", "zh": "cinese", "ru": "russo", "uk": "ucraino",
           "pt": "portoghese"},
    "en": {"en": "English", "sq": "Albanian", "es": "Spanish", "fr": "French", "de": "German",
           "ar": "Arabic", "ro": "Romanian", "zh": "Chinese", "ru": "Russian", "uk": "Ukrainian",
           "pt": "Portuguese"},
}
_DAY_LABEL = {
    "it": {"mon": "lunedì", "tue": "martedì", "wed": "mercoledì", "thu": "giovedì", "fri": "venerdì"},
    "en": {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday"},
}


def _describe(prefs: dict[str, object], lang: str) -> str:
    """The preferences in the citizen's words, for our reply; '' when there are none."""

    parts = []
    doctor_lang = prefs.get("doctor_lang")
    speaks = _LANG_LABEL[lang].get(doctor_lang) if isinstance(doctor_lang, str) else None
    gender = prefs.get("gender")
    if speaks or gender in ("f", "m"):
        if lang == "en":
            who = {"f": "a female doctor", "m": "a male doctor"}.get(str(gender), "a doctor")
            parts.append(who + (f" who speaks {speaks}" if speaks else ""))
        else:
            who = {"f": "una dottoressa", "m": "un medico uomo"}.get(str(gender), "un medico")
            parts.append(who + (f" che parli {speaks}" if speaks else ""))
    days = prefs.get("days")
    if isinstance(days, tuple) and days:
        names = [_DAY_LABEL[lang][day] for day in days]
        joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + (" or " if lang == "en" else " o ") + names[-1]
        parts.append(joined)
    return "; ".join(parts)


def _preference_tokens(prefs: dict[str, object]) -> str:
    """The tokens the booking centre reads from a citizen, for our handoff."""

    tokens = []
    if isinstance(prefs.get("doctor_lang"), str):
        tokens.append(f"doctor_lang={prefs['doctor_lang']}")
    if prefs.get("gender") in ("f", "m"):
        tokens.append(f"gender_preference={prefs['gender']}")
    if isinstance(prefs.get("days"), tuple) and prefs["days"]:
        tokens.append("days=" + ",".join(prefs["days"]))  # type: ignore[arg-type]
    return " ".join(tokens)


async def _prescribe(
    city: City, history: list[Message], message: Message, code: str, lang: str, *, again: bool = False
) -> None:
    # The number is derived from the case, so a retry never issues twice. A second protocol reason
    # in the same case (``again``) gets its own number: same number, other priority = no answer.
    key = f"{message.need_id}/{message.sender}" + (f"/{code}" if again else "")
    digest = sha256(key.encode()).hexdigest()[:24].upper()
    procedure, priority = PROTOCOL[code]
    request = Prescription(message.sender, f"prescription_number-{digest}", procedure, priority)
    await _issue(city, history, request, f"symptom_code={code} ", lang, _preferences(message.text or ""))


# "the prescription expired, I need a new one" in words, without the prescription_number token.
_RENEWAL = re.compile(
    r"rinnov\w*|scadut\w*|nuova ricetta|ricetta nuova|renew\w*|expired|new prescription"
)


def _prescribed(
    history: list[Message], agent_id: str, patient: str | None = None, number: str | None = None
) -> list[Prescription]:
    """Our ``prescribe`` writes in this case (for ``patient``, of ``number``), the latest first."""

    found = []
    for row in reversed(history):
        if row.sender != agent_id or row.recipient != LEDGER or not row.text.startswith("prescribe "):
            continue
        fields = understood(row).body
        rx = Prescription(*(fields.get(k) for k in ("patient", "prescription_number", "procedure", "priority")))
        if all(isinstance(v, str) for v in (rx.patient, rx.number, rx.procedure, rx.priority)) and (
            patient in (None, rx.patient) and number in (None, rx.number)
        ):
            found.append(rx)
    return found


def _issued(
    history: list[Message], agent_id: str, patient: str | None = None, number: str | None = None
) -> Prescription | None:
    """The case's prescription: our latest ``prescribe``, not the Ledger's ``prescribed``. Live
    21/11 that came 5-10 minutes later, and a citizen who wrote in between was asked the reason
    again, or got no answer. None = no prescription yet."""

    return next(iter(_prescribed(history, agent_id, patient, number)), None)


def _handed(history: list[Message], agent_id: str, number: str) -> bool:
    return any(row.sender == agent_id and row.text.startswith(HANDOFF + " ")
               and f"prescription_number={number}" in row.text.split() for row in history)


async def _renew(
    city: City, history: list[Message], patient: str, need_id: str, old: str, lang: str | None,
    *, reply: bool = True,
) -> None:
    """P1: a new prescription with the procedure and priority of ``old``, as the Ledger has it.

    Ours in this case: renewed in this turn, no Ledger read. Else the Ledger is asked once; live
    21/11 it answers in 4-5 minutes, in a turn of its own (main), or a later message of the citizen
    finds the answer here. ``reply``: a citizen's turn, which always gets an answer. ``lang`` None:
    the case's language."""

    tongue = _lang(history) if lang is None else _reply_lang(lang)
    mine = _issued(history, city.agent_id, patient, number=old)
    found = _ledger_rx(history, old) if mine is None else {
        "procedure": mine.procedure, "priority": mine.priority, "symptom_code": _code_of(mine)
    }
    if found is None:
        ask = f"prescription prescription_number={old}"
        if not _sent(history, city.agent_id, LEDGER, ask):
            try:
                await _say(city, LEDGER, ask)
            except CapError:
                await _say(city, patient, LEDGER_REFUSAL)
                return
            history = await city.history(need_id=need_id)  # a quick Ledger has already answered
            found = _ledger_rx(history, old)
        if found is None:
            if reply:
                await _tell(city, patient, RENEWAL_CHECKING[tongue].format(number=old))
            return
    procedure, priority = found.get("procedure"), found.get("priority")
    # Someone else's prescription, or one the Ledger does not know: nothing is renewed.
    if (
        not isinstance(procedure, str)
        or not isinstance(priority, str)
        or found.get("patient", patient) != patient
    ):
        head = f"{RENEWAL_NOT_FOUND} prescription_number={old}"
        # The Ledger's own turn says it once; a citizen's turn always gets its answer.
        if reply or not any(
            row.sender == city.agent_id and row.recipient == patient and row.text.startswith(head + " ")
            for row in history
        ):
            await _tell(city, patient, f"{head} {RENEWAL_UNKNOWN[tongue].format(number=old)}")
        return
    request = Prescription(patient, _renewal_number(need_id, patient, old), procedure, priority, renews=old)
    if reply and any(
        row.sender == city.agent_id and row.recipient == patient and row.text.startswith(request.head + " ")
        for row in history
    ):  # asked again once renewed: what is ready, never silence
        handed = _handed(history, city.agent_id, request.number)
        await _tell(city, patient, request.reply(tongue, handed, {}, again=True))
        return
    code = found.get("symptom_code")
    await _issue(city, history, request, f"symptom_code={code} " if isinstance(code, str) else "", tongue)


def _renewal_number(need_id: str, patient: str, old: str) -> str:
    """Derived from the case and ``old``: a retry never renews twice."""

    return "prescription_number-" + sha256(f"{need_id}/{patient}/{old}".encode()).hexdigest()[:24].upper()


def _ledger_rx(history: list[Message], number: str) -> dict | None:
    """The Ledger's latest answer about prescription ``number``: its fields, {} for a refusal;
    None = no answer yet."""

    found = None
    for row in history:
        if row.sender != LEDGER or understood(row).body.get("prescription_number") != number:
            continue
        words = row.text.split()
        if words[:1] == ["prescription"]:
            found = dict(understood(row).body)
        elif words[:1] == ["refused"] and "prescription" in words:
            found = {}
    return found


def _renewal_asker(history: list[Message], agent_id: str, number: object) -> str | None:
    """The citizen whose renewal of ``number`` we asked the Ledger about in this case."""

    if not isinstance(number, str) or not _sent(history, agent_id, LEDGER, f"prescription prescription_number={number}"):
        return None
    return next((
        row.sender for row in history
        if row.sender.startswith("citizen:") and understood(row).body.get("prescription_number") == number
    ), None)


async def _issue(
    city: City, history: list[Message], request: Prescription, extra: str, lang: str,
    prefs: dict[str, object] | None = None,
) -> None:
    """Ledger first: ``prescribe`` is written, then in the same turn the handoff and the citizen.

    Live 21/11 the Ledger answered ``prescribed`` after 6-10 minutes. Waiting for it kept the
    booking centre idle for that long in every journey, and a failed journey restarts only ~14
    minutes after its verdict. The number is ours and the write is already sent (R:219); a refusal
    is told in the Ledger's own turn (main)."""

    if not _issued(history, city.agent_id, number=request.number):
        try:
            await _say(
                city,
                LEDGER,
                f"prescribe patient={request.patient} procedure={request.procedure} "
                f"priority={request.priority} {extra}prescription_number={request.number}",
            )
        except CapError:
            await _say(city, request.patient, LEDGER_REFUSAL)
            return
    elif any(row.sender == city.agent_id and row.recipient == request.patient
             and row.text.startswith(request.head + " ") for row in history):
        return  # written and told: a second Ledger answer
    lang = _reply_lang(lang)
    # This turn's words may not be in history yet: they win over the case's earlier ones.
    prefs = {**_case_preferences(history, request.patient), **(prefs or {})}
    # Before the citizen's message, so that "already passed on" is true when we say it.
    handed = await _handoff(city, history, request, lang, prefs)
    await _tell(city, request.patient, request.reply(lang, handed, prefs))


async def _tell(city: City, patient: str, text: str) -> None:
    """A message to the citizen. Our texts are Italian or English; the model translates them only
    for another language the level-3 reading named in this turn."""

    lang = _LANG.get()
    if lang not in (None, "it", "en"):
        text = await tools.localize(city, text, None, lang)
    await _say(city, patient, text)


def _lang(history: list[Message]) -> str:
    """The citizen's ``lang=``, as the booking centre reads it; else plain English; Italian otherwise."""

    said = [row for row in history if row.sender.startswith("citizen:")]
    for row in said:
        lang = understood(row).body.get("lang")
        if lang in TIMING:
            return lang
    return "en" if said and _guess_lang(said[0].text.casefold()) == "en" else "it"


async def _handoff(
    city: City, history: list[Message], request: Prescription, lang: str, prefs: dict[str, object]
) -> bool:
    """Best effort, once per need: the prescription to our booking centre. Never raises."""

    done = [row.text for row in history if row.sender == city.agent_id and row.text.startswith(HANDOFF + " ")]
    if any(f" prescription_number={request.number} " in text for text in done):
        return True
    if done and not request.renews:  # once per need; only a renewal's new number (P1) goes too
        return False
    try:
        peers = [peer for peer in await city.find("booking_centre") if peer.agent_id == BOOKING_CENTRE]
        if len(peers) != 1:
            return False
        return bool(await _say(  # "": a network error, as when it raised
            city,
            peers[0],
            f"{HANDOFF} citizen={request.patient} prescription_number={request.number} "
            f"procedure={request.procedure} priority={request.priority} lang={lang} "
            + (f"renewal_of={request.renews} " if request.renews else "")
            + (f"{tokens} " if (tokens := _preference_tokens(prefs)) else "")
            + (
                f"Buongiorno, vi passo il rinnovo della ricetta {request.renews}, con la stessa visita e "
                "la stessa priorità, che ho appena scritto sul Ledger per la prenotazione"
                if request.renews
                else "Buongiorno, vi passo la ricetta che ho appena scritto sul Ledger per la prenotazione"
            )
            + (f" (priorità {request.priority}: {due}; vi chiedo di proporre solo date entro la scadenza)"
               if (due := TIMING["it"].get(request.priority)) and request.priority != "P" else "")
            + (f", con le preferenze che il paziente mi ha indicato ({_describe(prefs, 'it')}). " if tokens else ". ")
            + "Grazie!",
        ))
    except Exception:  # CapError (TARGET_NOT_VISIBLE...) or network: no retry, the citizen still hears from us
        return False


async def _say(city: City, to: Peer | str, text: str) -> str:
    """city.say of ``protocol.plain(text)`` (one act for the judge), recorded in the team's trace
    (tools.said, never blocks). A CapError is raised (the Ledger's refusal has its own answer); a
    network error is recorded and gives "": no retry (city.say is not idempotent, a ReadTimeout can
    come after the Relay took the message) and the turn goes on."""

    recipient = to.agent_id if isinstance(to, Peer) else to
    text = protocol.plain(text)
    try:
        msg_id = await city.say(to, text)
    except CapError as exc:
        tools.said(recipient, text, error=exc.code)
        raise
    except Exception as exc:  # noqa: BLE001 - the Relay's httpx timeouts must not end the turn
        tools.said(recipient, text, error=type(exc).__name__)
        return ""
    tools.said(recipient, text, msg_id)
    return msg_id


def _sent(history: list[Message], agent_id: str, recipient: str, text: str) -> bool:
    text = protocol.plain(text)
    return any(
        row.sender == agent_id and row.recipient == recipient and row.text == text
        for row in history
    )
