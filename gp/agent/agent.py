"""Medico di base per i percorsi simulati di Milano Agentic City."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import unicodedata
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from weakref import WeakValueDictionary

from city_sdk.city import LEDGER, CapError, City, Message, Need, OutOfScope, clinical, understood

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import tools  # noqa: E402

# What the citizen wrote in this turn: our reply goes out in that language (tools.localize).
_HEARD: ContextVar[str] = ContextVar("heard", default="")
# The language the model named for those words in this turn, so localize does not ask again.
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
ESCALATION = {
    "it": (
        "escalate.emergency reason=red_flag Mi dispiace, dev'essere un momento difficile. "
        "Chiami subito il 112 e spieghi all'operatore che cosa le sta succedendo. "
        "Se c'è qualcuno vicino, può chiedergli di chiamare per lei o di restarle accanto. "
        "Passata l'urgenza, se ha bisogno di aiuto per i prossimi passi, mi può riscrivere."
    ),
    "en": (
        "escalate.emergency reason=red_flag I'm sorry you're going through this. "
        "Call 112 now and tell the operator what's happening. If someone is nearby, you can ask "
        "them to make the call for you or stay with you. Once the emergency is over, you can write "
        "to me if you need help with the next steps."
    ),
}
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
BOOKING_FOLLOW_UP = "data.response purpose=booking_follow_up outcome=no_slots"
BOOKING_WITHOUT_SLOTS = {
    "it": (
        "Il centro prenotazioni mi ha avvisato che non ha trovato un posto per lei. Capisco che "
        "dover aspettare sia frustrante. Se nel frattempo la ricetta scade, mi scriva con il numero "
        "{number} e verificherò se è da rinnovare."
    ),
    "en": (
        "The booking centre told me it couldn't find an appointment for you. I know waiting can be "
        "frustrating. If your referral expires while you wait, send me the number {number} so I can "
        "check and renew it if needed."
    ),
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

    def opening(self, lang: str) -> str:
        """To the person: the reason they gave (their words, no diagnosis) and what is ready."""

        reason = _REASON[lang].get(_code_of(self))
        visit = _VISIT[lang].get(self.procedure, self.procedure)
        number = self.number
        if self.renews:
            if lang == "en":
                return (
                    f"I have renewed your prescription {self.renews}: the new prescription for a "
                    f"{visit} is number {number}, priority {self.priority}"
                )
            return (
                f"Le ho rinnovato la ricetta {self.renews}: la nuova ricetta per una {visit} ha "
                f"numero {number}, priorità {self.priority}"
            )
        if lang == "en":
            return (
                (f"Thank you for telling me about {reason}. " if reason else "Thank you for writing. ")
                + f"I have prepared your prescription for a {visit}, number {number}, "
                f"priority {self.priority}"
            )
        return (
            (f"Grazie per avermi descritto {reason}. " if reason else "Grazie per avermi scritto. ")
            + f"Le ho preparato la ricetta per una {visit}, numero {number}, priorità {self.priority}"
        )

    def reply(self, lang: str, handed: bool, prefs: dict[str, object] | None = None) -> str:
        """To the person: their reason, the prescription, one concrete next step, and unasked the
        handoff with their preferences, the preferences still missing and the renewal (P1)."""

        timing = TIMING[lang].get(self.priority)
        noted = _describe(prefs or {}, lang)
        missing = _missing(prefs or {}, lang)
        text = self.opening(lang) + (f": {timing}. " if timing else ". ")
        if lang == "en":
            if handed:
                text += (
                    "Next step: you do not need to repeat anything. I have already sent the "
                    "prescription to our booking centre"
                    + (f", together with your preferences ({noted})" if noted else "")
                    + ": it will write to you with the available slots, and you only need to "
                    "reply to confirm the one that suits you. "
                )
                if missing:
                    text += f"If you have a preference for {missing}, just tell the booking centre. "
            else:
                text += (
                    f"Next step: write to your ASST's booking centre with the number {self.number}"
                    + (f" and your preferences ({noted})" if noted else "")
                    + ": it will offer you the available slots. "
                )
                if missing:
                    text += f"If you have a preference for {missing}, tell them too. "
            text += (
                "Keep the prescription number: if it expires before you manage to book, write to "
                "me and I will renew it. If you feel worse or something new comes up, write to me."
            )
        else:
            if handed:
                text += (
                    "Passo successivo: non deve ripetere nulla. Ho già inviato la ricetta al nostro "
                    "centro prenotazioni"
                    + (f", insieme alle sue preferenze ({noted})" if noted else "")
                    + ": le scriverà con gli orari disponibili e a lei basterà rispondere per "
                    "confermare quello che le va meglio. "
                )
                if missing:
                    text += f"Se ha preferenze {missing}, le dica pure al centro prenotazioni. "
            else:
                text += (
                    "Passo successivo: scriva al centro prenotazioni della sua ASST indicando il "
                    f"numero {self.number}"
                    + (f" e le sue preferenze ({noted})" if noted else "")
                    + ": le proporrà gli orari disponibili. "
                )
                if missing:
                    text += f"Se ha preferenze {missing}, le indichi anche quelle. "
            text += (
                "Conservi il numero della ricetta: se scade prima che riesca a prenotare, mi "
                "scriva e gliela rinnovo io. Se si sente peggio o compare un disturbo nuovo, mi "
                "scriva."
            )
        return f"{self.head} {text}"


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
    inbound = city.inbound
    if inbound is None:
        return
    message = understood(inbound)
    _HEARD.set("")  # never the words or language of a previous turn
    _LANG.set(None)
    if message.sender == LEDGER:
        first = message.text.split()[:1]
        if message.need_id and first in (["prescribed"], ["refused"], ["prescription"]):
            async with _case_lock(message.need_id):
                history = await city.history(need_id=message.need_id)
                old = understood(message).body.get("prescription_number")
                patient = _renewal_asker(history, city.agent_id, old) if first != ["prescribed"] else None
                if patient is not None:  # P1: the Ledger's answer about a number to renew
                    await _renew(city, history, patient, message.need_id, old, None, reply=False)
                elif first != ["prescription"] and (request := _previous_prescription(history, city.agent_id)):
                    await _notify(city, history, request)
        return
    if message.sender == BOOKING_CENTRE:
        if message.need_id and (number := _no_slots_number(message)):
            async with _case_lock(message.need_id):
                await _follow_up_booking(city, message.need_id, number)
        return
    # Every citizen message: a word like "prescription" in the text changes the act understood()
    # reads, and a red flag must never be missed for that.
    if not message.sender.startswith("citizen:"):
        return

    said = message.text or need.summary
    _HEARD.set(said or "")
    text = said.casefold()
    if need.is_emergency or message.body.get("is_emergency") is True or red_flag(text):
        await _escalate(city, message, _guess_lang(text))
        return
    if clinical(text):
        raise OutOfScope()
    old = message.body.get("prescription_number")
    if isinstance(old, str) and old and not message.body.get("symptom_code"):
        if not message.need_id:
            await city.say(message.sender, "Non riesco ad associare la richiesta a un percorso.")
            return
        async with _case_lock(message.need_id):
            history = await city.history(need_id=message.need_id)
            # Often only the number (R:720): the case's language, not a guess on the tokens.
            await _renew(city, history, message.sender, message.need_id, old, None)
        return

    code = _symptom_code(message, text)
    # The model names the citizen's language (any language), once per turn: the level-3 reading
    # brings it, otherwise one short call. Our word list only if the model cannot.
    # The handoff's lang= stays it/en (_reply_lang): the booking centre filters doctors on it.
    if code is None:
        # Level 3: the citizen's own words, in any language. The rules found nothing, so the model
        # reads them once; only a protocol code, a yes/no red flag and a language come back.
        read = await _read_with_model(city, said)
        heard = read.get("lang")
        lang = heard if isinstance(heard, str) else _guess_lang(text)
        if read.get("red_flag") is True:
            await _escalate(city, message, lang)
            return
        code = read.get("code")
    else:
        heard = await tools.language(city, said)
        lang = heard or _guess_lang(text)
    _LANG.set(heard if isinstance(heard, str) else "")  # "": asked, no answer, not again
    if message.need_id:
        async with _case_lock(message.need_id):
            history = await city.history(need_id=message.need_id)
            issued = _issued(history, city.agent_id, message.sender)
            if issued is not None:
                # The case already has its prescription. A thank-you, a question, the same symptom
                # in other words: we remind what is ready and what happens next, never silence.
                # A renewal asked in words renews that prescription (P1). Only a different protocol
                # reason gets a prescription of its own.
                if _RENEWAL.search(text) and code is None:
                    # "Grazie per il rinnovo" after one: the prescription it renewed, so _renew
                    # finds the renewal already told and reminds it, never a third one.
                    ours = (understood(row).body.get("prescription_number") for row in history
                            if row.sender == city.agent_id and row.recipient == LEDGER
                            and row.text.startswith("prescribe "))
                    old = next((n for n in ours if isinstance(n, str)
                                and _renewal_number(message.need_id, message.sender, n) == issued.number), issued.number)
                    await _renew(city, history, message.sender, message.need_id, old, lang)
                    return
                if code is None or PROTOCOL[code] == (issued.procedure, issued.priority):
                    prefs = {**_case_preferences(history, message.sender), **_preferences(said)}
                    await _tell(city, message.sender, _reminder(history, city.agent_id, issued, lang, prefs))
                    return
                await _prescribe(city, message, message.need_id, code, lang, again=True)
                return
    if code is None:
        await _tell(city, message.sender, ASK_REASON[_reply_lang(lang)])
        return
    if not message.need_id:
        await city.say(message.sender, "Non riesco ad associare la richiesta a un percorso.")
        return
    async with _case_lock(message.need_id):
        await _prescribe(city, message, message.need_id, code, lang)


def _case_lock(need_id: str) -> asyncio.Lock:
    return CASE_LOCKS.setdefault(need_id, asyncio.Lock())


async def _escalate(city: City, message: Message, lang: str) -> None:
    async with _case_lock(message.need_id or message.msg_id):
        history = await city.history(need_id=message.need_id) if message.need_id else []
        if not any(_sent(history, city.agent_id, message.sender, text) for text in ESCALATION.values()):
            await city.say(message.sender, ESCALATION[_reply_lang(lang)])


def _reply_lang(lang: str | None) -> str:
    """Our texts exist in Italian and English: Italian for Italian, English for any other language."""

    return "it" if lang in (None, "", "it") else "en"


# --- free text (level 3) -----------------------------------------------------------------------
# Rules first: instant and free. The team model (city.llm, city-default) is asked only when the
# rules find no symptom, under a short timeout. Its JSON is checked against PROTOCOL: it can pick
# a code, never invent a procedure or a priority. CapError, timeout or bad JSON: the rules stand.
# Measured on city-default (2026-11-20, 62 calls): ~0.4 s, or 12-13 s (max 12.97) about one call
# in four, nothing in between. A short timeout would lose those; the gp has no tight deadline.
_LLM_TIMEOUT = 15.0
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


def _missing(prefs: dict[str, object], lang: str) -> str:
    """The preferences the citizen has not given yet, to suggest them; '' when all are there."""

    names = {
        "it": {"days": "sui giorni", "gender": "sul genere", "doctor_lang": "sulla lingua del medico"},
        "en": {"days": "days", "gender": "the doctor's gender", "doctor_lang": "the doctor's language"},
    }[lang]
    left = [names[key] for key in ("days", "gender", "doctor_lang") if not prefs.get(key)]
    if not left:
        return ""
    if lang == "en":
        return left[0] if len(left) == 1 else ", ".join(left[:-1]) + " or " + left[-1]
    return left[0] if len(left) == 1 else ", ".join(left[:-1]) + " o " + left[-1]


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
    city: City, message: Message, need_id: str, code: str, lang: str, *, again: bool = False
) -> None:
    # The number is derived from the case, so a retry never issues twice. A second protocol reason
    # in the same case (``again``) gets its own number: same number, other priority = no answer.
    key = f"{need_id}/{message.sender}" + (f"/{code}" if again else "")
    digest = sha256(key.encode()).hexdigest()[:24].upper()
    procedure, priority = PROTOCOL[code]
    request = Prescription(message.sender, f"prescription_number-{digest}", procedure, priority)
    await _issue(city, need_id, request, f"symptom_code={code} ", lang, _preferences(message.text or ""))


# "the prescription expired, I need a new one" in words, without the prescription_number token.
_RENEWAL = re.compile(
    r"rinnov\w*|scadut\w*|nuova ricetta|ricetta nuova|renew\w*|expired|new prescription"
)


def _issued(
    history: list[Message], agent_id: str, patient: str, number: str | None = None
) -> Prescription | None:
    """The latest prescription of this case the Ledger confirmed for ``patient`` (``number``: that
    one only); None = none yet."""

    wanted = number
    confirmed = {
        understood(row).body.get("prescription_number")
        for row in history
        if row.sender == LEDGER and row.text.split()[:1] == ["prescribed"]
    }
    for row in reversed(history):
        if row.sender != agent_id or row.recipient != LEDGER or not row.text.startswith("prescribe "):
            continue
        fields = understood(row).body
        number, procedure, priority = (
            fields.get("prescription_number"), fields.get("procedure"), fields.get("priority")
        )
        if (
            fields.get("patient") == patient
            and number in confirmed
            and isinstance(number, str)
            and wanted in (None, number)
            and isinstance(procedure, str)
            and isinstance(priority, str)
        ):
            return Prescription(patient, number, procedure, priority)
    return None


def _reminder(
    history: list[Message], agent_id: str, issued: Prescription, lang: str, prefs: dict[str, object]
) -> str:
    """What is ready and what happens next, for a citizen who writes again after the prescription."""

    lang = _reply_lang(lang)
    noted = _describe(prefs, lang)
    handed = any(
        row.sender == agent_id
        and row.text.startswith(HANDOFF + " ")
        and f" prescription_number={issued.number} " in row.text
        for row in history
    )
    timing = TIMING[lang].get(issued.priority)
    text = issued.opening(lang).replace("I have prepared", "I have already prepared").replace(
        "Le ho preparato", "Le ho già preparato"
    ) + (f": {timing}. " if timing else ". ")
    if handed and any(
        row.recipient == agent_id and _no_slots_number(row) == issued.number for row in history
    ):
        return f"{issued.head} booking_status=no_slots {text}{BOOKING_WITHOUT_SLOTS[lang].format(number=issued.number)}"
    if lang == "en":
        text += (
            "Next step: our booking centre already has it and will write to you with the "
            "available slots; you only need to reply to confirm one. "
            if handed
            else f"Next step: write to your ASST's booking centre with the number {issued.number}. "
        )
        text += (
            f"I have noted your preferences ({noted}): confirm them to the booking centre when it "
            "contacts you, so it can look for a slot that matches them. "
            if noted
            else "If you have preferred days or a preference for the doctor's gender or language, "
            "tell the booking centre. "
        )
        text += "If you feel worse or something new comes up, write to me."
    else:
        text += (
            "Passo successivo: il nostro centro prenotazioni l'ha già ricevuta e le scriverà con "
            "gli orari disponibili; a lei basterà rispondere per confermarne uno. "
            if handed
            else "Passo successivo: scriva al centro prenotazioni della sua ASST indicando il "
            f"numero {issued.number}. "
        )
        text += (
            f"Ho preso nota delle sue preferenze ({noted}): le confermi al centro prenotazioni "
            "quando la contatta, così cercherà uno slot che le rispetti. "
            if noted
            else "Se ha giorni preferiti o preferenze sul genere o sulla lingua del medico, le "
            "indichi al centro prenotazioni. "
        )
        text += "Se si sente peggio o compare un disturbo nuovo, mi scriva."
    return f"{issued.head} {text}"


async def _renew(
    city: City, history: list[Message], patient: str, need_id: str, old: str, lang: str | None,
    *, reply: bool = True,
) -> None:
    """P1: a new prescription with the procedure and priority of ``old``, as the Ledger has it.

    Ours in this case and confirmed (``prescribed``): renewed in this turn, no Ledger read. Else
    the Ledger is asked once; live 21/11 it answers in 4-5 minutes, in a turn of its own (main),
    or a later message of the citizen finds the answer here. ``reply``: a citizen's turn, which
    always gets an answer. ``lang`` None: the case's language."""

    tongue = _lang(history) if lang is None else _reply_lang(lang)
    mine = _issued(history, city.agent_id, patient, number=old)
    found = _ledger_rx(history, old) if mine is None else {
        "procedure": mine.procedure, "priority": mine.priority, "symptom_code": _code_of(mine)
    }
    if found is None:
        ask = f"prescription prescription_number={old}"
        if not _sent(history, city.agent_id, LEDGER, ask):
            try:
                await city.say(LEDGER, ask)
            except CapError:
                await city.say(patient, LEDGER_REFUSAL)
                return
            history = await city.history(need_id=need_id)  # a quick Ledger has already answered
            found = _ledger_rx(history, old)
        if found is None:
            if reply:
                await _tell(city, patient, RENEWAL_CHECKING[tongue].format(number=old), history)
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
            await _tell(city, patient, f"{head} {RENEWAL_UNKNOWN[tongue].format(number=old)}", history)
        return
    request = Prescription(patient, _renewal_number(need_id, patient, old), procedure, priority, renews=old)
    if reply and any(
        row.sender == city.agent_id and row.recipient == patient and row.text.startswith(request.head + " ")
        for row in history
    ):  # asked again once renewed: what is ready, never silence
        await _tell(city, patient, _reminder(history, city.agent_id, request, tongue, {}), history)
        return
    code = found.get("symptom_code")
    await _issue(city, need_id, request, f"symptom_code={code} " if isinstance(code, str) else "", tongue)


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
    city: City, need_id: str, request: Prescription, extra: str, lang: str,
    prefs: dict[str, object] | None = None,
) -> None:
    """Ledger first: ``prescribe`` is written, then the citizen and the booking centre hear at once."""

    history = await city.history(need_id=need_id)
    asked = _previous_prescription(history, city.agent_id, number=request.number) is None
    if asked:
        try:
            await city.say(
                LEDGER,
                f"prescribe patient={request.patient} procedure={request.procedure} "
                f"priority={request.priority} {extra}prescription_number={request.number}",
            )
        except CapError:
            await city.say(request.patient, LEDGER_REFUSAL)
            return
        history = await city.history(need_id=need_id)  # a quick Ledger has already answered

    # Live 21/11 the Ledger answered ``prescribed`` after 6-10 minutes. Waiting for it kept the
    # booking centre idle for that long in every journey, and a failed journey restarts only ~14
    # minutes after its verdict. The number is ours and the write is already sent (R:219), so the
    # turn that wrote it goes on at once: the prescription to the citizen and the handoff. The
    # Ledger's own turn (main) then finds the message sent, or corrects it on a ``refused``.
    await _notify(city, history, request, lang, prefs, written=asked)


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


async def _notify(
    city: City,
    history: list[Message],
    request: Prescription,
    lang: str | None = None,
    prefs: dict[str, object] | None = None,
    *,
    written: bool = False,
) -> bool:
    """``lang``/``prefs``: what this turn read; a Ledger turn has none and reads the case's.
    ``written``: this turn just sent ``prescribe``, so it goes ahead without the Ledger's answer."""

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
        if not written:
            return False
        answer = request.head
    if answer == LEDGER_REFUSAL:
        if not _sent(history, city.agent_id, request.patient, answer):
            await city.say(request.patient, answer)
        return True
    if not any(
        row.sender == city.agent_id and row.recipient == request.patient and row.text.startswith(answer + " ")
        for row in history
    ):
        lang = _lang(history) if lang is None else _reply_lang(lang)
        # Before the citizen's message, so that "already passed on" is true when we say it.
        # This turn's words may not be in history yet: they win over the case's earlier ones.
        prefs = {**_case_preferences(history, request.patient), **(prefs or {})}
        handed = await _handoff(city, history, request, lang, prefs)
        await _tell(city, request.patient, request.reply(lang, handed, prefs), history)
    return True


async def _tell(city: City, patient: str, text: str, history: list[Message] | None = None) -> None:
    """A message to the citizen, in the language they write in. A Ledger turn has no words of
    the citizen: the last thing they wrote in the case stands in."""

    heard = _HEARD.get()
    if not heard and history:
        # Their own words, not a bare "data.response purpose=booking": tokens say no language.
        words = (re.sub(r"\S+=\S+", "", row.text or "") for row in reversed(history) if row.sender == patient)
        heard = next((said for said in words if len(re.findall(r"[^\W\d_]{2,}", said)) >= 3), "")
    await city.say(patient, await tools.localize(city, text, heard, _LANG.get()))


def _no_slots_number(message: Message) -> str | None:
    if message.sender != BOOKING_CENTRE or message.text.split()[:1] != ["data.response"]:
        return None
    fields = understood(message).body
    number = fields.get("prescription_number")
    if fields.get("purpose") == "booking_follow_up" and fields.get("outcome") == "no_slots" and isinstance(number, str):
        return number
    return None


async def _follow_up_booking(city: City, need_id: str, number: str) -> None:
    history = await city.history(need_id=need_id)
    request = _previous_prescription(history, city.agent_id, number=number)
    if request is None or _issued(history, city.agent_id, request.patient) != request:
        return
    head = f"{BOOKING_FOLLOW_UP} prescription_number={number}"
    handoff = next((
        row for row in history
        if row.sender == city.agent_id and row.recipient == BOOKING_CENTRE
        and row.text.startswith(HANDOFF + " ") and f"prescription_number={number}" in row.text.split()
    ), None)
    if handoff is None or any(
        row.sender == city.agent_id and row.recipient == request.patient and row.text.startswith(head + " ")
        for row in history
    ):
        return
    lang = "en" if understood(handoff).body.get("lang") == "en" else "it"
    text = BOOKING_WITHOUT_SLOTS[lang].format(number=number)
    try:
        await _tell(city, request.patient, f"{head} {text}", history)
    except CapError:
        return


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
        await city.say(
            peers[0],
            f"{HANDOFF} citizen={request.patient} prescription_number={request.number} "
            f"procedure={request.procedure} priority={request.priority} lang={lang} notify_if=no_slots "
            + (f"renewal_of={request.renews} " if request.renews else "")
            + (f"{tokens} " if (tokens := _preference_tokens(prefs)) else "")
            + (
                f"Buongiorno, vi passo il rinnovo della ricetta {request.renews}, con la stessa visita e "
                "la stessa priorità, che ho appena scritto sul Ledger per la prenotazione"
                if request.renews
                else "Buongiorno, vi passo la ricetta che ho appena scritto sul Ledger per la prenotazione"
            )
            + (f", con le preferenze che il paziente mi ha indicato ({_describe(prefs, 'it')}). " if tokens else ". ")
            + "Se non trovate un posto, avvisatemi così posso aiutare la persona a proseguire. Grazie!",
        )
        return True
    except Exception:  # CapError (TARGET_NOT_VISIBLE...) or network: no retry, the citizen still hears from us
        return False


def _sent(history: list[Message], agent_id: str, recipient: str, text: str) -> bool:
    return any(
        row.sender == agent_id and row.recipient == recipient and row.text == text
        for row in history
    )
