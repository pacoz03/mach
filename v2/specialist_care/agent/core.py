"""Core condiviso dei cinque brain v2. Fonte unica: ``v2/core.py``; ``v2/sync.sh`` lo copia in
ogni ``<agente>/agent/``. Solo stdlib e ``city_sdk.city``.

Cosa sappiamo della città vera (21/11), e come il core ci si adatta:
- ogni messaggio apre un turno e i turni dello stesso caso girano insieme: ``turn_lock`` li mette
  in fila, poi ogni turno rilegge la storia e fa solo ciò che manca (``once``);
- il Ledger risponde in secondi o in minuti, da una coda sola per tutta la squadra: si scrive e
  non si aspetta; la risposta apre un turno suo; le letture (``world``, ``agenda``) si rifanno di
  rado e si tengono in memoria (``World``, ``Clock``);
- il cittadino decide dai token ``chiave=valore``, il giudice legge parola d'atto e token, il
  revisore legge il tono: ogni messaggio è ``apertura umana · atto e token · seguito utile``;
- un fatto che non viene dal Ledger o dal cittadino non si scrive.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import unicodedata
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

from city_sdk.city import LEDGER, CapError, City, Message, Need, OutOfScope, clinical

try:  # traccia per gli eval (memoria condivisa): facoltativa, mai bloccante
    import tools as _trace
except Exception:  # noqa: BLE001
    _trace = None

_LOG = logging.getLogger("v2")

# I nostri cinque agenti (ASST Gorla). L'Index non dice la squadra di un peer: l'id è l'unico
# modo di riconoscere i nostri.
TEAM = {
    "gp": "agent:ck9w60ejwnvw",
    "booking_centre": "agent:sbdpbpqz5sxe",
    "hospital": "agent:m6py14a1ej77",
    "specialist_care": "agent:h6krzxx6es83",
    "family_counseling": "agent:yg8debxe3x0p",
}
ROLE_OF = {agent: role for role, agent in TEAM.items()}
ASST = "gorla"
# procedura -> ruolo della struttura che la eroga, e capability con cui la si cerca sull'Index
SERVICE = {"VIS-FIS": "hospital", "VIS-CARD": "specialist_care", "CTRL-CARD": "specialist_care",
           "CONS-GIN": "family_counseling"}
CAPABILITY = {"hospital": "hospital.physiatry", "specialist_care": "specialist_care.cardiology",
              "family_counseling": "family_counseling.gynecology"}
# Registry dei medici (console, ASST world): chiave = ultimo pezzo del doctor_id.
# (lingue, genere, giorni); "*" = la lingua rara dell'ASST del medico.
DOCTORS = {
    "h1": ({"it", "en"}, "f", {"mon", "tue", "wed"}),
    "h2": ({"it"}, "m", {"wed", "thu", "fri"}),
    "h3": ({"it", "*"}, "m", {"tue", "thu"}),
    "s1": ({"it", "en"}, "m", {"mon", "wed", "fri"}),
    "s2": ({"it"}, "f", {"tue", "thu"}),
    "c1": ({"it", "en"}, "f", {"mon", "tue", "wed", "thu", "fri"}),
}
# Lingua rara per ASST, come pubblicata dalle squadre (Discord): il doctor_id contiene il nome.
RARE = {"gorla": "sq", "giambellino": "ka", "nord": "ur", "lambrate": "ja", "greco": "si",
        "bicocca": "ti", "corvetto": "fr", "brera": "fa", "isola": "tl", "ortica": "wo",
        "forlanini": "de"}
DEADLINE_DAYS = {"B": 2, "D": 5}  # U: stesso giorno di agenda; P: lista d'attesa
PROBE = "agent:t05/osp-x"  # la sonda della conformance: nessun effetto, mai
ACTS = frozenset({
    "need.request", "offer", "confirm", "cancel", "prescription", "consent", "data.request",
    "data.response", "checkin", "report", "booking.receipt", "escalate.emergency", "hold",
    "case.closed",
})
# Nomi letti anche dall'harness di test, che li azzera: non rinominare.
_PULL_PAUSE = 0.4
_LLM_TIMEOUT = 2.5

# --- lettura dei messaggi -----------------------------------------------------------------------

_TRIM = ".,;:!?()\"'«»"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_IT = {"mon": "lunedì", "tue": "martedì", "wed": "mercoledì", "thu": "giovedì", "fri": "venerdì"}
DAY_EN = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday"}


def fields(text: str) -> dict[str, str]:
    """I token ``chiave=valore``: vince il primo per chiave; le virgole restano nel valore."""
    found: dict[str, str] = {}
    for token in (text or "").split():
        key, eq, value = token.partition("=")
        key, value = key.lstrip(_TRIM), value.rstrip(_TRIM)
        if eq and key and value and key not in found:
            found[key] = value
    return found


def items(value: str | None) -> list[str]:
    """Una lista scritta come ``a,b``, ``a/b`` o ``a|b``."""
    return [part for part in re.split(r"[,/|;]", value or "") if part]


def words(text: str) -> list[str]:
    return [token.strip(_TRIM).lower() for token in (text or "").split()]


def act(text: str) -> str:
    """La prima parola d'atto del testo (i cittadini veri scrivono prosa prima e token dopo)."""
    for word in words(text):
        if word in ACTS:
            return word
    return ""


def head(text: str) -> str:
    """La prima parola: è così che si riconosce una riga del Ledger."""
    parts = words(text)
    return parts[0] if parts else ""


def offers_in(text: str) -> list[dict[str, str]]:
    """Ogni ``offer_id=`` apre un'offerta: un messaggio ne porta diverse."""
    found: list[dict[str, str]] = []
    for token in (text or "").split():
        key, eq, value = token.partition("=")
        if eq and key == "offer_id":
            found.append({})
        if eq and found and value:
            found[-1].setdefault(key, value.rstrip(_TRIM))
    return found


def is_citizen(party: str) -> bool:
    return (party or "").startswith("citizen:")


def is_agent(party: str) -> bool:
    return (party or "").startswith("agent:")


def citizen_of(chat: list[Message]) -> str:
    """Il cittadino del caso: chi ha scritto, oppure quello nominato da ``citizen=``/``patient=``."""
    for row in chat:
        if is_citizen(row.sender):
            return row.sender
    for row in chat:
        if is_citizen(row.recipient):
            return row.recipient
    for row in chat:
        got = fields(row.text)
        for key in ("citizen", "patient"):
            if is_citizen(got.get(key, "")):
                return got[key]
    return ""


def plain(text: str) -> str:
    """Minuscole senza accenti, per leggere parole scritte in qualsiasi tono."""
    folded = unicodedata.normalize("NFKD", (text or "").replace("’", "'")).casefold()
    return "".join(ch for ch in folded if not unicodedata.combining(ch))


def prose(text: str) -> str:
    """Ciò che la persona ha scritto: il testo senza token e senza parole d'atto."""
    kept = [t for t in (text or "").split() if "=" not in t and t.strip(_TRIM) not in ACTS]
    return " ".join(kept)


# --- sicurezza: emergenze e tentativi di comando -----------------------------------------------

_PAIN = (r"(?:dolor\w*|mal(?:e|issimo)?\b|fitt\w*|strett\w*|string\w*|oppress\w*|bruci\w*|pain\w*"
         r"|hurt\w*|ach\w*|tight\w*|pressure|squeez\w*|douleur\w*|duele|schmerz\w*)")
_CHEST = r"(?:petto|torace|chest|pecho|poitrine|brust)"
_NEAR = r"\W+(?:\w+\W+){0,4}?"
_RED_FLAG = re.compile("|".join((
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
    r"(?:perdita di forza|pers\w* (?:la )?forza|perdo (?:la )?forza|debolezza|non sento)\W+(?:\w+\W+){0,3}?gamb\w*",
    r"gamb\w*\W+(?:\w+\W+){0,2}?(?:cedono|non (?:mi )?(?:reggono|rispondono)|paralizzat\w*)",
    r"(?:loss of strength|losing strength|lost strength|weakness|can'?t feel)\W+(?:\w+\W+){0,3}?legs?\b",
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
)))
_NEGATED = re.compile(r"\b(?:non|nessun\w*|senza|niente|mai|no|not|never|without|don'?t|doesn'?t"
                      r"|didn'?t|haven'?t)\s+(?:\w+\s+){0,2}$")
_INJECTION = re.compile(
    r"ignor\w+ (?:all |any |le |tutte le |your |the |previous |prior )*(?:istruzion\w*|regol\w*|instruction\w*|rules?|prompt)"
    r"|system prompt|prompt di sistema|jailbreak|developer mode|modalita sviluppatore"
    r"|(?:sei|you are|act as|agisci come|fingi di essere|pretend to be) (?:ora |now )?(?:un |an? )?(?:admin\w*|amministrator\w*|root|ledger|city hall)"
    r"|(?:dammi|mostrami|rivela\w*|give me|show me|reveal|list|elenca\w*)\W+(?:\w+\W+){0,4}?(?:altr\w+ pazient\w*|other patients?|another patient|altri cittadini|other citizens?|tutti i pazienti|all patients)"
    r"|(?:senza|without|skip\w*|salta\w*|bypass\w*)\W+(?:\w+\W+){0,3}?(?:ricetta|prescription|consenso|consent|verific\w*)"
)


def red_flag(text: str) -> bool:
    """Un segnale d'allarme detto ORA, in qualsiasi punto del testo; una frase negata non conta."""
    flat = plain(text)
    for hit in _RED_FLAG.finditer(flat):
        if not _NEGATED.search(flat[max(0, hit.start() - 40):hit.start()]):
            return True
    return False


def injection(text: str) -> bool:
    return _INJECTION.search(plain(text)) is not None


_WORRIED = re.compile(r"\b(?:ansia|ansios[oa]|preoccupa\w*|paura|agitat[oa]|nervos[oa]|spaventat[oa]|angosci\w*"
                      r"|worried|anxious|nervous|scared|afraid|concerned)\b")


def worried(chat: list[Message]) -> bool:
    """Il cittadino ha detto di essere in ansia: lo si riconosce prima di tutto il resto."""
    return any(is_citizen(row.sender) and _WORRIED.search(plain(row.text)) for row in chat)


def emergency(need: Need, message: Message) -> bool:
    if not is_citizen(message.sender):
        return False
    token = fields(message.text).get("is_emergency", "").lower() == "true"
    return bool(need.is_emergency) or token or message.body.get("is_emergency") is True or red_flag(message.text)


ESCALATION = {
    "it": ("Mi dispiace, dev'essere un momento difficile: quello che descrive va visto subito.",
           "Chiami ora il 112 e spieghi all'operatore che cosa le sta succedendo. Se c'è qualcuno "
           "vicino, gli chieda di chiamare per lei e di restarle accanto. Passata l'urgenza mi "
           "riscriva: la aiuto io con i prossimi passi."),
    "en": ("I'm sorry you're going through this: what you describe needs to be seen right away.",
           "Call 112 now and tell the operator what is happening. If someone is nearby, ask them to "
           "make the call for you and stay with you. Once the emergency is over, write to me again "
           "and I will help you with the next steps."),
}


async def escalate(city: City, message: Message, chat: list[Message]) -> None:
    """``escalate.emergency`` al cittadino, una volta per caso, e ci si ferma."""
    if not once(message.need_id or message.msg_id, "escalate") or sent(chat, city.agent_id, "", "escalate.emergency"):
        return
    opener, body = ESCALATION["it" if lang_of(chat, message.text) == "it" else "en"]
    await say(city, message.sender, compose(opener, "escalate.emergency reason=red_flag", body))


async def run(need: Need, city: City, handler) -> None:
    """Apertura e chiusura di ogni turno. ``handler(message, text)`` fa il lavoro.

    - sonda della conformance (nessun ``need_id``): nessun effetto, nessuna eccezione;
    - richiesta clinica senza allarme: ``OutOfScope``, nulla è inviato né conservato (l'allarme
      si guarda prima: un red flag non si perde per una parola come "dose");
    - nessun'altra eccezione esce: un turno rifiutato non viene mai rieseguito.
    """
    message = city.inbound
    if message is None:
        return
    text = message.text or need.summary or ""
    if text == "__error__":
        raise RuntimeError("application")
    if message.need_id is None or message.sender in (PROBE, city.agent_id) or not text.strip():
        return
    if is_citizen(message.sender) and clinical(text) and not emergency(need, message):
        raise OutOfScope()
    if _trace is not None:
        try:
            _trace.start_turn(message)
        except Exception:  # noqa: BLE001
            pass
    note_delay(message)
    try:
        await handler(message, text)
    except OutOfScope:
        raise
    except Exception:  # noqa: BLE001
        _LOG.exception("turn failed")


# --- lingua e modello ---------------------------------------------------------------------------

# Solo parole che spagnolo e francese non condividono: "la", "un", "no" non dicono la lingua.
_IT = frozenset("il di da per non che ma gli della anche quando ho sono vorrei grazie buongiorno "
                "buonasera salve ciao posso devo alla alle dal nel nella mio mia questo questa sto "
                "più già perché può giorni oggi domani settimana dottore dottoressa visita ricetta "
                "schiena ginocchio cuore prenotare prego gentile cortesemente".split())
_EN = frozenset("the and i'm i've my is am are was have has been to for of at with when since can "
                "you your need would like please hello hi thanks thank this that not doctor pain "
                "back knee heart appointment visit good morning could".split())


def guess_lang(text: str) -> str:
    """``it``, ``en`` o ``""`` (non so) dalle parole di ciò che la persona ha scritto."""
    said = re.findall(r"[a-zà-ù']+", prose(text).lower())
    it, en = sum(w in _IT for w in said), sum(w in _EN for w in said)
    if it == en:
        return ""
    return "it" if it > en else "en"


def lang_of(chat: list[Message], text: str = "") -> str:
    """La lingua in cui rispondere: quella in cui il cittadino scrive; il suo ``lang=`` (la lingua
    che chiede al medico) e quello passato da un collega fanno da ripiego. Default italiano."""
    guessed = guess_lang(text)
    if guessed:
        return guessed
    token = ""
    for row in reversed(chat):
        if is_citizen(row.sender):
            guessed = guess_lang(row.text)
            if guessed:
                return guessed
            token = token or fields(row.text).get("lang", "")
    for row in chat:
        if is_agent(row.sender):
            token = token or fields(row.text).get("lang", "")
    return token if re.fullmatch(r"[a-z]{2,3}", token) else "it"


async def llm(city: City, system: str, user: str, *, timeout: float | None = None) -> str:
    """Una risposta del modello, o ``""``: mai un'eccezione, mai oltre ``timeout``."""
    budget = _LLM_TIMEOUT if timeout is None else timeout
    if _LLM_TIMEOUT < 1:  # i test accorciano l'attesa
        budget = min(budget, _LLM_TIMEOUT)
    try:
        reply = await asyncio.wait_for(
            city.llm([{"role": "system", "content": system}, {"role": "user", "content": user[:1200]}]), budget)
    except Exception:  # noqa: BLE001  (CapError, timeout: si va avanti senza modello)
        return ""
    content = reply.get("content") if isinstance(reply, dict) else ""
    return re.sub(r"<think>.*?</think>", "", content if isinstance(content, str) else "", flags=re.S).strip()


async def llm_json(city: City, system: str, user: str, *, timeout: float | None = None) -> dict:
    found = re.search(r"\{.*\}", await llm(city, system, user, timeout=timeout), re.S)
    try:
        data = json.loads(found.group(0)) if found else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


_TRANSLATE = ("Translate this message from a public health service into the language with ISO 639-1 "
              "code {lang}. Keep every number, date, time, code, name and the marker [[7]] exactly. "
              "Do not add, remove or change any information. Output only the translation.")
_MARK = "[[7]]"


def pick(lang: str, it: str, en: str) -> str:
    return it if lang == "it" else en


def _bare(text: str, own: str = "") -> str:
    """Una parola d'atto nuda nella prosa, diversa da quella del messaggio, cambierebbe l'atto
    letto dal giudice (vale l'ultima): prende la maiuscola. Nessun ``=`` in prosa."""
    return " ".join(t.capitalize() if t in ACTS and t != own else t for t in (text or "").replace("=", ":").split())


def compose(opener: str, tokens: str, body: str = "") -> str:
    """``apertura · atto e token · seguito``, entro i 2000 caratteri del Relay (si accorcia il seguito)."""
    own = act(tokens)
    opener, body = _bare(opener, own), _bare(body, own)
    room = 1990 - len(opener) - len(tokens) - 2
    return " ".join(part for part in (opener, tokens, body[:max(0, room)]) if part)


async def tell(city: City, to: str, lang: str, it: tuple[str, str], en: tuple[str, str], tokens: str = "",
               *, translate: float = 6.0) -> str:
    """Un messaggio al cittadino nella sua lingua: italiano e inglese scritti a mano, le altre
    tradotte dal modello (apertura e seguito insieme; se il modello sgarra resta l'inglese)."""
    opener, body = it if lang == "it" else en
    if lang not in ("it", "en") and translate > 0:
        out = await llm(city, _TRANSLATE.format(lang=lang), f"{opener} {_MARK} {body}", timeout=translate)
        source = f"{opener} {body}"
        same = sorted(re.findall(r"\d+", out.replace(_MARK, ""))) == sorted(re.findall(r"\d+", source))
        if _MARK in out and "=" not in out and same and 0.4 <= len(out) / max(1, len(source)) <= 2.5:
            opener, body = (part.strip() for part in out.split(_MARK, 1))
    return await say(city, to, compose(opener, tokens, body))


# --- parlare -------------------------------------------------------------------------------------

REFUSED: dict[str, tuple[str, float]] = {}  # destinatario -> (codice, quando) dell'ultimo 4xx


async def say(city: City, to: str, text: str) -> str:
    """Un messaggio. Restituisce il ``msg_id`` o ``""``. Un 4xx chiude la richiesta: mai un retry.
    Un timeout non è un rifiuto: una seconda prova sola, e solo se il messaggio non risulta partito."""
    for attempt in (1, 2):
        try:
            msg_id = await city.say(to, text[:2000]) or "sent"
        except CapError as exc:
            code = getattr(exc, "code", "") or "REFUSED"
            REFUSED[to] = (code, time.monotonic())
            _traced(to, text, None, code)
            return ""
        except Exception:  # noqa: BLE001  (timeout del Relay, rete)
            if await _went(city, to, text):
                return "sent"
            if attempt == 2:
                _traced(to, text, None, "TIMEOUT")
                return ""
            continue
        REFUSED.pop(to, None)
        _traced(to, text, msg_id, None)
        return msg_id
    return ""


def down(to: str, seconds: float = 600.0) -> bool:
    """Il Relay ha rifiutato da poco un nostro messaggio a ``to`` (struttura giù, non visibile)."""
    code, at = REFUSED.get(to, ("", 0.0))
    return bool(code) and time.monotonic() - at < seconds


async def _went(city: City, to: str, text: str) -> bool:
    try:
        rows = await city.history(to, need_id=city.inbound.need_id if city.inbound else None)
    except Exception:  # noqa: BLE001
        return False
    return any(row.sender == city.agent_id and row.text == text[:2000] for row in rows[-8:])


def _traced(to: str, text: str, msg_id: str | None, error: str | None) -> None:
    if _trace is None:
        return
    try:
        _trace.said(to, text, msg_id, error=error)
    except Exception:  # noqa: BLE001
        pass


# --- canale interno della squadra (memoria condivisa) -----------------------------------------
# Il Relay consegna in minuti; la memoria condivisa in pochi secondi. Solo tra i nostri agenti, e
# sempre in aggiunta al messaggio vero sul Relay: se la memoria è giù non si perde nulla. Ciò che
# vale punti (hold, commit, check-in, referto) resta sul Ledger.


def side_ready() -> bool:
    """La memoria condivisa è accesa (in città sì; in locale e nei test no)."""
    return _trace is not None and bool(getattr(_trace, "MEMORY_ON", False)) and bool(getattr(_trace, "SUPABASE_URL", ""))


def side_post(to: str, need_id: str | None, text: str) -> None:
    """Lascia ``text`` al collega ``to`` per il caso ``need_id`` (scade dopo un quarto d'ora)."""
    if _trace is None or not need_id:
        return
    try:
        _trace.note(f"city:inbox:{to}", need_id, {"text": text, "at": datetime.now(UTC).isoformat()},
                    need_id=need_id, ttl_seconds=900)
    except Exception:  # noqa: BLE001
        pass


async def side_inbox(me: str) -> dict[str, dict]:
    """I messaggi lasciati a ``me`` sul canale interno: need_id -> {text, at}."""
    if _trace is None:
        return {}
    try:
        found = await _trace.context("-")
    except Exception:  # noqa: BLE001
        return {}
    box = (found.get("facts") or {}).get(f"city:inbox:{me}") or {}
    return {need: item["value"] for need, item in box.items()
            if isinstance(item, dict) and isinstance(item.get("value"), dict)}


async def case(city: City, message: Message) -> list[Message]:
    """La storia di questo caso, dal più vecchio; col solo messaggio in arrivo se la lettura fallisce."""
    try:
        return await city.history(need_id=message.need_id)
    except Exception:  # noqa: BLE001
        return [message]


def sent(chat: list[Message], me: str, to: str, *tokens: str) -> bool:
    """Abbiamo già scritto (a ``to``, se dato) un messaggio con tutti questi token o parole d'atto?"""
    for row in chat:
        if row.sender == me and (not to or row.recipient == to):
            parts = set(row.text.split())
            if all(token in parts for token in tokens):
                return True
    return False


def ledger_rows(chat: list[Message], *heads: str) -> list[Message]:
    return [row for row in chat if row.sender == LEDGER and head(row.text) in heads]


async def settle(city: City, message: Message, ready, seconds: float) -> list[Message]:
    """Rilegge il caso finché ``ready(chat)`` o scadono i ``seconds``: serve dove la risposta del
    Ledger arriva in fretta o non apre un turno suo. In città di solito arriva dopo, come turno."""
    chat = await case(city, message)
    tries = int(seconds / _PULL_PAUSE) if _PULL_PAUSE else 3
    while not ready(chat) and tries > 0:
        tries -= 1
        await asyncio.sleep(_PULL_PAUSE)
        chat = await case(city, message)
    return chat


# --- un turno alla volta per caso, ogni passo una volta sola -----------------------------------

_LOCKS: dict[str, list] = {}
_DONE: OrderedDict[str, None] = OrderedDict()


class turn_lock:
    """``async with turn_lock(need_id):`` i turni dello stesso caso girano uno dopo l'altro."""

    def __init__(self, key: str | None) -> None:
        self.key = key or "-"

    async def __aenter__(self) -> None:
        entry = _LOCKS.setdefault(self.key, [asyncio.Lock(), 0])
        entry[1] += 1
        await entry[0].acquire()

    async def __aexit__(self, *exc) -> None:
        entry = _LOCKS[self.key]
        entry[0].release()
        entry[1] -= 1
        if entry[1] <= 0:
            _LOCKS.pop(self.key, None)


def once(*key: str | None) -> bool:
    """True la prima volta che questo processo incontra la chiave: la storia può non mostrare
    ancora ciò che abbiamo appena inviato, questa memoria sì."""
    name = "|".join(str(part) for part in key)
    if name in _DONE:
        return False
    _DONE[name] = None
    while len(_DONE) > 50000:
        _DONE.popitem(last=False)
    return True


def did(*key: str | None) -> bool:
    """La chiave è già stata incontrata? (senza segnarla)"""
    return "|".join(str(part) for part in key) in _DONE


def undo(*key: str | None) -> None:
    _DONE.pop("|".join(str(part) for part in key), None)


# --- orologio di agenda ---------------------------------------------------------------------------


def when(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class Clock:
    """L'ora di agenda senza chiederla: l'ancora (``epoch_real``, ``epoch_agenda``, ``rate``) di una
    risposta ``agenda`` vale per tutta la corsa. ``agenda_now`` invece è vecchio di quanto il Ledger
    ci ha messo a rispondere (minuti = ore di agenda): non si usa. L'agenda non ha sabato e domenica."""

    anchor: tuple[datetime, datetime, float] | None = None
    asked = 0.0

    @classmethod
    def learn(cls, text: str) -> None:
        got = fields(text)
        real, agenda = when(got.get("epoch_real")), when(got.get("epoch_agenda"))
        if real is None or agenda is None:  # forma senza ancora: l'ora letta vale da adesso
            real, agenda = datetime.now(UTC), when(got.get("agenda_now")) or when(got.get("agenda_date"))
        if agenda is None or got.get("paused", "false").lower() == "true":
            return
        try:
            rate = float(got.get("rate", "60"))
        except ValueError:
            rate = 60.0
        cls.anchor = (real, agenda, rate)

    @classmethod
    def at(cls, real: datetime | None) -> datetime | None:
        """L'ora di agenda di un istante reale (settimane di cinque giorni)."""
        if cls.anchor is None or real is None:
            return None
        epoch_real, epoch, rate = cls.anchor
        monday = (epoch - timedelta(days=epoch.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        seconds = (epoch - monday).total_seconds() + (real - epoch_real).total_seconds() * rate
        days, rest = divmod(max(0.0, seconds), 86400)
        weeks, weekday = divmod(int(days), 5)
        return monday + timedelta(days=weeks * 7 + weekday, seconds=rest)

    @classmethod
    def now(cls) -> datetime | None:
        return cls.at(datetime.now(UTC))

    @classmethod
    async def ensure(cls, city: City, every: float = 3600.0) -> None:
        """Chiede ``agenda`` se non c'è ancora o è da rinfrescare; la risposta arriva come turno."""
        gap = time.monotonic() - cls.asked
        if gap > (20.0 if cls.anchor is None else every):
            cls.asked = time.monotonic()
            await say(city, LEDGER, "agenda")


# --- il mondo: posti in memoria -------------------------------------------------------------------


def doctor(doctor_id: str) -> tuple[set[str], str, set[str]] | None:
    """(lingue, genere, giorni) dal registry; la lingua rara è quella dell'ASST nel doctor_id."""
    spec = DOCTORS.get((doctor_id or "").rsplit("-", 1)[-1])
    if spec is None:
        return None
    langs = set(spec[0])
    if "*" in langs:  # la lingua rara è dell'ASST del medico: la nostra se l'id non ne nomina un'altra
        langs.discard("*")
        asst = re.search(r"asst-([a-z0-9_]+)-", doctor_id or "")
        langs.add(RARE.get(asst.group(1), "*") if asst else RARE[ASST])
    return langs, spec[1], set(spec[2])


def speaks(doctor_id: str, lang: str) -> bool | None:
    """True/False dal registry; None se non lo sappiamo (lingua rara di un'ASST non pubblicata)."""
    spec = doctor(doctor_id)
    if spec is None:
        return None
    if lang in spec[0]:
        return True
    return None if "*" in spec[0] and lang not in ("it", "en") else False


def slot_of(line: str) -> dict[str, str] | None:
    """Una riga di posto del Ledger (``at/day/free`` in città, ``agenda_date/agenda_day/capacity``
    nell'SDK locale), o None."""
    got = fields(line)
    at = got.get("at") or got.get("agenda_date")
    if not got.get("slot_id") or not got.get("doctor_id") or when(at) is None:
        return None
    day = (got.get("day") or got.get("agenda_day") or DAYS[when(at).weekday()])[:3].lower()
    free = got.get("free", got.get("capacity", "1"))
    return {"slot_id": got["slot_id"], "doctor_id": got["doctor_id"], "facility": got.get("facility", ""),
            "procedure": got.get("procedure", ""), "at": at, "day": day,
            "free": "0" if free.isdigit() and int(free) <= 0 else "1",
            "reserve": got.get("urgent_reserve", "false").lower()}


def _slot_chunks(lines: list[str]) -> list[str]:
    """Una riga per posto; se una riga ne porta diversi, si taglia a ogni ``slot_id=``."""
    out: list[str] = []
    for line in lines:
        if line.count("slot_id=") <= 1:
            out.append(line)
            continue
        for token in line.split():
            if token.startswith("slot_id=") or not out:
                out.append(token)
            else:
                out[-1] += " " + token
    return out


class World:
    """I posti della nostra ASST letti con ``world``, per procedura. Si chiede poco (la coda del
    Ledger è una per squadra) e si ricorda tutto: una pagina vecchia vale più di un'attesa."""

    slots: dict[str, dict[str, str]] = {}
    pages: dict[str, int] = {}
    asked: dict[tuple[str, int], float] = {}
    got: dict[tuple[str, int], float] = {}
    gone: dict[str, float] = {}       # posti che il Ledger ha rifiutato o che abbiamo prenotato
    offered: dict[str, tuple[float, str]] = {}  # posto -> (quando, a quale caso) dell'ultima offerta

    @classmethod
    def learn(cls, text: str, asked_for: str = "") -> bool:
        """Legge una risposta ``slots ...``/``world ...``. True se era una pagina di posti."""
        lines = (text or "").splitlines() or [""]
        if head(lines[0]) not in ("slots", "world"):
            return False
        page, _, pages = fields(lines[0]).get("page", "1/1").partition("/")
        rows = [slot for slot in map(slot_of, _slot_chunks(lines)) if slot]
        more = re.search(r"more:\s*world\s+procedure=(\S+)", text or "")
        procedure = asked_for or (rows[0]["procedure"] if rows else "") or (more.group(1) if more else "")
        if not procedure:
            return True
        number = int(page) if page.isdigit() else 1
        if number == 1:  # la prima pagina dice da che giorno parte il mondo: il resto è vecchio
            cls.pages[procedure] = int(pages) if pages.isdigit() else 1
        for slot in rows:
            slot.setdefault("procedure", procedure)
            slot["procedure"] = slot["procedure"] or procedure
            cls.slots[slot["slot_id"]] = slot
        asked, now = cls.asked.get((procedure, number)), time.monotonic()
        if asked and asked > cls.got.get((procedure, number), -1e9):
            note_ledger(now - asked)  # quanto ci ha messo il Ledger a rispondere a questa lettura
        cls.got[(procedure, number)] = now
        return True

    @classmethod
    def age(cls, procedure: str) -> float:
        return time.monotonic() - cls.got.get((procedure, 1), -1e9)

    @classmethod
    async def refresh(cls, city: City, procedure: str, *, every: float = 600.0, deep: bool = True) -> bool:
        """Richiede le pagine da rinfrescare, ognuna al più una volta ogni ``every`` secondi: tutte
        quelle che la prima pagina annuncia (fino a otto). Con i ritardi di consegna il primo posto
        offribile è a uno o due giorni di agenda: senza le pagine lontane non lo si vede, e si
        finisce in lista d'attesa con posti liberi. True se ha chiesto qualcosa."""
        last = min(cls.pages.get(procedure, 4), 8)
        due = []
        for page in range(1, (last if deep else min(last, 4)) + 1):
            key, now = (procedure, page), time.monotonic()
            if now - cls.asked.get(key, -1e9) > every:  # mai due richieste della stessa pagina in dieci minuti
                cls.asked[key] = now
                due.append(say(city, LEDGER, f"world procedure={procedure} page={page}"))
        return any(await asyncio.gather(*due)) if due else False

    @classmethod
    def of(cls, procedure: str, facility: str = "") -> list[dict[str, str]]:
        rows = [s for s in cls.slots.values() if s["procedure"] == procedure and (not facility or s["facility"] == facility)]
        return sorted(rows, key=lambda s: (when(s["at"]), s["slot_id"]))

    @classmethod
    def drop(cls, slot_id: str) -> None:
        if slot_id:
            cls.gone[slot_id] = time.monotonic()


# Ritardi misurati: consegna di un messaggio a noi, e giro di una richiesta al Ledger (finché non
# se ne misura uno si assume lento: in gara è arrivato a 14 minuti).
HOP = {"seconds": 0.0, "ledger": 300.0}


def note_delay(message: Message) -> None:
    """Quanto ci ha messo questo messaggio ad arrivare: in città si va da un secondo a minuti."""
    sent_at = when(message.sent_at)
    if sent_at is not None and message.sender != LEDGER:
        delay = min(900.0, max(0.0, (datetime.now(UTC) - sent_at).total_seconds()))
        HOP["seconds"] = delay if HOP["seconds"] == 0.0 else 0.7 * HOP["seconds"] + 0.3 * delay


def note_ledger(seconds: float) -> None:
    HOP["ledger"] = 0.5 * HOP["ledger"] + 0.5 * min(1800.0, max(0.0, seconds))


def lead() -> timedelta:
    """Margine tra adesso e il primo posto offribile. Prima che l'hold arrivi al Ledger devono
    passare: la risposta del cittadino e il confirm alla struttura (due consegne e mezza) e due
    giri di Ledger (hold e commit). Ogni secondo reale è un minuto di agenda. Tra 2 e 40 ore."""
    rate = Clock.anchor[2] if Clock.anchor else 60.0
    need = timedelta(seconds=(2.5 * HOP["seconds"] + 2 * HOP["ledger"]) * rate) + timedelta(hours=1)
    return min(timedelta(hours=40), max(timedelta(hours=2), need))


def deadline(born: datetime, priority: str) -> datetime | None:
    """Fine della finestra di priorità, contata dalla nascita del bisogno. None = nessuna (P)."""
    if priority == "U":
        return born.replace(hour=23, minute=59, second=59, microsecond=0)
    if priority in DEADLINE_DAYS:
        return born + timedelta(days=DEADLINE_DAYS[priority])
    return None


def choose(slots: list[dict[str, str]], *, now: datetime, born: datetime, priority: str, lang: str = "it",
           skip: set[str] = frozenset(), no_gender: set[str] = frozenset(), no_days: set[str] = frozenset(),
           no_doctors: set[str] = frozenset(), days: set[str] = frozenset(), gender: str = "",
           limit: int = 6, relaxed: bool = False, lead: timedelta | None = None, need: str = "") -> list[dict[str, str]]:
    """I posti da offrire in un messaggio solo. Vincoli duri: libero, futuro, lingua, riserva U,
    finestra di priorità (non con ``relaxed``), più tutto ciò che il cittadino ha già rifiutato.
    Preferenze dette (giorni, genere) prima. Poi a ventaglio: un posto per (medico, giorno), dal
    più vicino, perché il cittadino tace i suoi giorni e il suo medico finché non rifiuta."""
    end = None if relaxed else deadline(born, priority)
    margin = globals()["lead"]() if lead is None else lead
    fit: list[tuple[int, datetime, dict[str, str]]] = []
    for slot in slots:
        at, spec = when(slot["at"]), doctor(slot["doctor_id"])
        if slot["slot_id"] in skip or slot["free"] == "0" or time.monotonic() - World.gone.get(slot["slot_id"], -1e9) < 900:
            continue
        if slot["reserve"] == "true" and priority != "U":
            continue
        if at <= now + margin or (end is not None and at > end):
            continue
        if slot["day"] in no_days or slot["doctor_id"] in no_doctors:
            continue
        if spec is None and lang != "it":
            continue
        if spec is not None and (speaks(slot["doctor_id"], lang) is False or spec[1] in no_gender):
            continue
        liked = (not days or slot["day"] in days) and (not gender or (spec is not None and spec[1] == gender))
        offered_at, offered_to = World.offered.get(slot["slot_id"], (-1e9, ""))
        shared = offered_to != need and time.monotonic() - offered_at < 600
        fit.append((0 if liked else 1, at, {**slot, "_shared": "1" if shared else ""}))
    fit.sort(key=lambda item: (item[0], item[1], item[2]["slot_id"]))
    picked, seen = [], set()
    for want_fresh in (True, False):          # prima i posti non offerti ad altri da poco
        for _, _, slot in fit:
            combo = (slot["doctor_id"], slot["day"])
            if combo in seen or len(picked) >= limit or (want_fresh and slot["_shared"]):
                continue
            seen.add(combo)
            picked.append(slot)
    for _, _, slot in fit:                    # poi si riempie fino a tre con i più vicini
        if len(picked) >= min(limit, 3):
            break
        if slot not in picked:
            picked.append(slot)
    picked.sort(key=lambda s: (when(s["at"]), s["slot_id"]))
    return picked


def offer_line(slot: dict[str, str], *, number: str = "", priority: str = "", request_id: str = "") -> str:
    """Un'offerta: solo ciò che il Ledger ha scritto sulla riga del posto (un attributo falso = -2)."""
    parts = ["offer", f"offer_id=of-{slot['slot_id']}", f"slot_id={slot['slot_id']}", f"doctor_id={slot['doctor_id']}"]
    parts += [f"facility={slot['facility']}"] if slot.get("facility") else []
    parts += [f"procedure={slot['procedure']}"] if slot.get("procedure") else []
    parts += [f"agenda_date={slot['at']}", f"agenda_day={slot['day']}", f"expiry={slot['at']}"]
    parts += [f"priority={priority}"] if priority else []
    parts += [f"prescription_number={number}"] if number else []
    parts += [f"request_id={request_id}"] if request_id else []
    return " ".join(parts)


def spoken(at: str, lang: str) -> str:
    """``lunedì 30/11 alle 14:30``: giorno e ora della riga del Ledger, in parole."""
    moment = when(at)
    if moment is None:
        return ""
    day = (DAY_IT if lang == "it" else DAY_EN).get(DAYS[moment.weekday()], "")
    return f"{day} {moment:%d/%m} {'alle' if lang == 'it' else 'at'} {moment:%H:%M}".strip()


def half_day(at: str, lang: str = "it") -> str:
    moment = when(at)
    if moment is None:
        return ""
    return "08:00-13:00" if moment.hour < 13 else "13:00-18:00"


def rules_in_force(text: str) -> set[str]:
    """Le regole P1-P3 nominate da una risposta ``rules`` (``rules count=0`` = nessuna)."""
    found = set()
    for line in (text or "").splitlines():
        effective = when(fields(line).get("effective_agenda"))
        now = Clock.now()
        if effective is not None and now is not None and effective > now:
            continue
        for rule, value in re.findall(r"\b(P[123])\b(?:=(\S+))?", line):
            if value.lower() not in ("off", "false", "0", "no", "inactive"):
                found.add(rule)
    return found


def rx_number(*parts: str) -> str:
    """Numero di ricetta nostro e deterministico: un messaggio ripetuto non ne emette un'altra."""
    return "prescription_number-" + hashlib.sha256("/".join(parts).encode()).hexdigest()[:24].upper()


# --- la conversazione sulle offerte (centro prenotazioni e consultorio) -------------------------

_YES = re.compile(r"^\W*(?:si|ok|okay|va bene|perfetto|benissimo|confermo|accetto|prenoto|d'accordo|certo|acconsento"
                  r"|yes|yeah|yep|sure|fine|great|perfect|agreed|i agree|go ahead)\b"
                  r"|\b(?:acconsento|do il (?:mio )?consenso|autorizzo|i agree|i consent|book it|lo prendo|la prendo)\b")
_NO = re.compile(r"\b(?:no|non|nessun\w*|rifiuto|preferisco di no|not|don'?t|do not|none|neither|refuse|cannot|can'?t)\b")
_LATER = re.compile(r"ci penso|faccio sapere|ci devo pensare|non so|forse|think about|let you know|not sure|maybe")
_NAMES = r"(?:la|il|lo|l'|quell[ao]|opzione|option|numero|number|the|n\.?|#|prendo|scelgo|choose|take)"
_ORDINALS = {
    1: r"prim[oa](?!\s+(?:possibile|pomeriggio|mattin\w*|di tutto|cosa))|first(?!\s+of all)|1st|1",
    2: r"second[oa](?!\s+me\b)|second|2nd|2", 3: r"terz[oa]|third|3rd|3", 4: r"quart[oa]|fourth|4th|4",
    5: r"quint[oa]|fifth|5th|5", 6: r"sest[oa]|sixth|6th|6",
}
_DAY_RX = {"mon": r"lunedi|mondays?", "tue": r"martedi|tuesdays?", "wed": r"mercoledi|wednesdays?",
           "thu": r"giovedi|thursdays?", "fri": r"venerdi|fridays?"}
_HOUR = re.compile(r"\b(?:alle|delle|ore|at|h)\s*(\d{1,2})(?:[:.](\d{2}))?\b|\b(\d{1,2})[:.](\d{2})\b")
_CONSENT = re.compile(r"\bcons-[A-Za-z0-9_.-]+")
_FEMALE = re.compile(r"\b(?:una|la|con una) dottoressa\b|\bmedic[oa] donna\b|\bdottor\w* donna\b|\b(?:female|woman|lady) (?:doctor|physician)\b")
_MALE = re.compile(r"\bmedico uomo\b|\bdottor\w* uomo\b|\bun dottore\b|\b(?:male|man) (?:doctor|physician)\b")
_NOT_NEAR = re.compile(r"\b(?:non|no|not|never|tranne|eccetto|except|n't)\b\W+(?:\w+\W+){0,3}?$")


def _negated(flat: str, start: int) -> bool:
    """Una negazione nella stessa frase, poco prima della parola."""
    return _NOT_NEAR.search(re.split(r"[,;.!?]", flat[:start])[-1]) is not None


def consent_ref(text: str) -> str:
    """Il riferimento del consenso: il token, oppure l'unico ``cons-...`` scritto in prosa."""
    token = fields(text).get("consent_ref", "")
    codes = set(_CONSENT.findall(text or ""))
    return token or (codes.pop() if len(codes) == 1 else "")


def hear(text: str, offers: list[dict[str, str]]) -> dict:
    """Cosa dice un cittadino in prosa davanti alle offerte vive (nell'ordine in cui le ha lette).
    ``pick``: l'offerta scelta, solo se una e una sola; ``yes``/``no``; ``days``/``not_days``/``gender``
    detti come preferenza. Una domanda, una negazione o un "ci penso" non scelgono mai."""
    flat = plain(prose(text))
    out = {"pick": None, "yes": False, "no": False, "days": set(), "not_days": set(), "gender": ""}
    token = fields(text).get("offer_id", "")
    by_id = {o.get("offer_id"): o for o in offers}
    undecided = "?" in flat or _LATER.search(flat) is not None
    out["no"] = _NO.search(flat) is not None
    out["yes"] = _YES.search(flat) is not None and not out["no"] and not undecided
    for day, rx in _DAY_RX.items():
        for hit in re.finditer(rf"\b(?:{rx})\b", flat):
            (out["not_days"] if _negated(flat, hit.start())
             or re.match(r"\W+(?:\w+\W+)?(?:non|no|not)\b", flat[hit.end():hit.end() + 16]) else out["days"]).add(day)
    female, male = _FEMALE.search(flat), _MALE.search(flat)
    if bool(female) != bool(male):
        hit = female or male
        negated = _negated(flat, hit.start())
        out["gender"] = ("f" if female else "m") if not negated else ("m" if female else "f")
    if token:
        out["pick"] = by_id.get(token)
        return out
    if undecided or out["no"] or not offers:
        return out
    found: list[dict[str, str]] = []
    bare = re.fullmatch(r"\W*(?:opzione|option|numero|number|n\.?|#)?\s*([1-6])\W*", flat)
    for index, rx in _ORDINALS.items():
        if index <= len(offers) and ((bare and int(bare.group(1)) == index)
                                     or re.search(rf"\b{_NAMES}\s*(?:{rx})\b", flat)):
            found.append(offers[index - 1])
    if re.search(r"\b(?:ultim[oa]|last)\b", flat):
        found.append(offers[-1])
    on_day = [o for o in offers if o.get("agenda_day") in out["days"]]
    hour = _HOUR.search(flat)
    if hour:
        clock = f"{int(hour.group(1) or hour.group(3)):02d}:{hour.group(2) or hour.group(4) or '00'}"
        at_hour = [o for o in (on_day or offers) if (when(o.get("agenda_date")) or datetime.min).strftime("%H:%M") == clock]
        found += at_hour if len(at_hour) == 1 else []
    elif len(on_day) == 1:
        found.append(on_day[0])
    if not found and out["yes"] and len(offers) == 1:
        found.append(offers[0])
    ids = {o.get("offer_id") for o in found}
    out["pick"] = found[0] if len(ids) == 1 else None
    return out


def our_offers(chat: list[Message], me: str, to: str = "") -> dict[str, dict[str, str]]:
    """Tutte le offerte che abbiamo scritto (a ``to``, se dato), per ``offer_id``."""
    found: dict[str, dict[str, str]] = {}
    for row in chat:
        if row.sender == me and (not to or row.recipient == to) and "offer" in words(row.text):
            for offer in offers_in(row.text):
                found[offer["offer_id"]] = offer
    return found


def live_offers(chat: list[Message], me: str, citizen: str) -> list[dict[str, str]]:
    """Le offerte dell'ultimo nostro messaggio di offerte al cittadino, meno quelle che lui ha
    rifiutato o che noi abbiamo ritirato dopo."""
    last, dropped = -1, set()
    for index, row in enumerate(chat):
        if row.sender == me and row.recipient == citizen and act(row.text) == "offer":
            last = index
    if last < 0:
        return []
    for row in chat[last + 1:]:
        if "cancel" in words(row.text) and "confirm" not in words(row.text):
            dropped.add(fields(row.text).get("offer_id", ""))
    return [o for o in offers_in(chat[last].text) if o.get("offer_id") not in dropped and not o.get("waiting_list")]


def refusals(chat: list[Message], me: str, citizen: str) -> dict:
    """Ciò che i rifiuti del cittadino insegnano (``cancel offer_id=.. reason=gender|day|lang|
    deadline|past``): posti, medici, generi e giorni da non riproporre, e i limiti di data."""
    ours = our_offers(chat, me, citizen)
    out = {"skip": set(), "no_gender": set(), "no_days": set(), "no_doctors": set(),
           "before": None, "after": None, "reasons": set()}
    for row in chat:
        if row.sender != citizen or "cancel" not in words(row.text) or "confirm" in words(row.text):
            continue
        got = fields(row.text)
        offer = ours.get(got.get("offer_id", ""))
        reason = plain(got.get("reason", ""))
        if offer and not offer.get("slot_id"):
            out["reasons"].add(reason or "waiting_list")
        if not offer or not offer.get("slot_id"):
            continue
        out["skip"].add(offer["slot_id"])
        out["reasons"].add(reason)
        spec, at = doctor(offer.get("doctor_id", "")), when(offer.get("agenda_date"))
        if reason.startswith("gender") and spec:
            out["no_gender"].add(spec[1])
        elif reason.startswith("day") and offer.get("agenda_day"):
            out["no_days"].add(offer["agenda_day"])
        elif reason.startswith("lang") or reason.startswith("doctor"):
            out["no_doctors"].add(offer.get("doctor_id", ""))
        elif reason in ("deadline", "priority", "late") and at:
            out["before"] = min(out["before"] or at, at)
        elif reason in ("past", "early", "time") and at:
            out["after"] = max(out["after"] or at, at)
    if out["no_gender"] >= {"f", "m"}:  # ha rifiutato entrambi: il genere non era il vero motivo
        out["no_gender"] = set()
    return out


def offers_text(lang: str, slots: list[dict[str, str]], *, opener: tuple[str, str], closing: tuple[str, str],
                number: str = "", priority: str = "") -> str:
    """Tutte le offerte in UN messaggio (il cittadino sceglie solo da lì): apertura, una riga di
    token per posto, poi le stesse in parole, numerate, e come rispondere."""
    lines = [offer_line(slot, number=number, priority=priority) for slot in slots]
    listed = []
    for index, slot in enumerate(slots, 1):
        spec = doctor(slot["doctor_id"]) if f"-{ASST}-" in f"-{slot['doctor_id']}" else None
        who = "" if spec is None else pick(lang, " con una dottoressa" if spec[1] == "f" else " con un dottore",
                                           " with a female doctor" if spec[1] == "f" else " with a male doctor")
        listed.append(f"{index}) {spoken(slot['at'], lang)}{who}")
    body = "; ".join(listed) + ". " + pick(lang, *closing)
    return compose(pick(lang, *opener), "\n".join(lines) + "\n", body)
