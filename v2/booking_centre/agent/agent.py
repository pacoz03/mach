"""Centro prenotazioni. Ricetta -> posti a ventaglio in UN messaggio -> ``confirm`` alla struttura ->
ricevuta al cittadino. Non scrive mai ``hold`` o ``commit`` (li fa la struttura) e non compone mai
una ricevuta: inoltra solo quella che la struttura manda dopo il commit.

Regole imparate in città:
- la ricetta arriva dal nostro medico (passaggio) prima del cittadino: i posti sono già in memoria
  e l'offerta parte nel turno del suo primo messaggio; la verifica sul Ledger corre in parallelo;
- il cittadino tace giorni e medico preferiti e li dice solo rifiutando (``reason=gender|day|lang|
  deadline|past``): si offre a ventaglio, si impara da ogni rifiuto, si risponde SEMPRE nello stesso turno;
- lingua, finestra della priorità e riserva urgente sono vincoli duri; se da noi non c'è nulla si
  dice, si offre l'alternativa più vicina dichiarata come tale e si chiede alle ASST partner.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import core  # noqa: E402
from core import LEDGER, TEAM, Clock, World, fields, once, pick, say, sent, tell, when, words  # noqa: E402

OURS = {TEAM["hospital"], TEAM["specialist_care"], TEAM["family_counseling"]}
VISIT = {"it": {"VIS-FIS": "visita fisiatrica", "VIS-CARD": "visita cardiologica", "CTRL-CARD": "controllo cardiologico",
                "CONS-GIN": "visita ginecologica"},
         "en": {"VIS-FIS": "physiatry visit", "VIS-CARD": "cardiology visit", "CTRL-CARD": "cardiology follow-up",
                "CONS-GIN": "gynaecology visit"}}
TIMING = {"it": {"U": "da fare in giornata", "B": "da fare entro 2 giorni", "D": "da fare entro 5 giorni"},
          "en": {"U": "due today", "B": "due within 2 days", "D": "due within 5 days"}}
FIND = {"hospital": "hospital physiatry", "specialist_care": "specialist cardiology",
        "family_counseling": "consultorio gynecology"}
# Strutture partner che hanno pubblicato la loro lingua rara (Discord): id -> lingua.
PARTNER_LANG = {"agent:h6r1v45esk77": "ka", "agent:nanpr5mgm2py": "ja", "agent:gpr1zqe343gb": "ur",
                "agent:xtj1dk9x3qej": "si", "agent:bb01ggxymm05": "ti"}
_RX = re.compile(r"(?<![\w-])prescription_number-[0-9A-Za-z-]{4,40}")
_CHANGE = re.compile(r"spostar\w*|annull\w*|disdir\w*|disdett\w*|cambiar\w* (?:data|giorno|appuntamento)|non (?:posso|riesco) piu"
                     r"|reschedul\w*|move (?:my|the) (?:visit|appointment)|change (?:my|the) (?:visit|appointment|date)|can(?:not|'t) (?:come|make it)")

RULES: set[str] = set()                    # regole P1-P3 in vigore, dall'ultima risposta ``rules``
STATE = {"rules_asked": 0.0, "rules_known": False}
KNOWN: dict[str, dict[str, str]] = {}      # numero di ricetta -> ciò che ne sappiamo
WAITING: dict[str, tuple] = {}             # casi fermi in attesa di posti, orologio o ricetta


async def main(need, city) -> None:
    async def turn(message, text: str) -> None:
        if message.sender == LEDGER:
            learn(message.text)
        elif core.is_agent(message.sender) and not relevant(message):
            return
        if Clock.anchor is None:  # appena partiti: senza orologio non si riconosce un posto già passato
            await Clock.ensure(city)
        async with core.turn_lock(message.need_id):
            chat = await core.case(city, message)
            if core.is_citizen(message.sender):
                await on_citizen(need, city, message, chat)
            elif core.is_agent(message.sender):
                await on_agent(city, message, chat)
            else:
                await on_ledger(city, message, chat)
        wake(message.need_id)

    await core.run(need, city, turn)


def relevant(message) -> bool:
    """Un agente ci scrive: il passaggio del nostro medico, o una struttura su una nostra richiesta."""
    got, kind = fields(message.text), set(words(message.text))
    if message.sender == TEAM["gp"]:
        return got.get("handoff") == "gp"
    return bool(kind & {"booking.receipt", "cancel", "cancelled", "offer"})


def learn(text: str) -> None:
    """Ogni riga del Ledger insegna qualcosa a tutti i casi: orologio, posti, regole."""
    word = core.head(text)
    if word == "agenda":
        Clock.learn(text)
    elif word in ("slots", "world"):
        World.learn(text)
    elif word == "rules":
        RULES.clear()
        RULES.update(core.rules_in_force(text))
        STATE["rules_known"] = True
    elif word == "prescription":
        got = fields(text)
        if got.get("prescription_number") and got.get("procedure"):
            KNOWN.setdefault(got["prescription_number"], {}).update(got, verified="yes")


def wake(current: str | None) -> None:
    """I casi in attesa ripartono appena arriva ciò che mancava (anche dal turno di un altro caso)."""
    for need_id, (city, message) in list(WAITING.items()):
        if need_id != current and Clock.now() is not None:
            WAITING.pop(need_id, None)
            asyncio.ensure_future(_resume(city, message))


async def _resume(city, message) -> None:
    try:
        async with core.turn_lock(message.need_id):
            chat = await core.case(city, message)
            await serve(city, message, chat)
    except Exception:  # noqa: BLE001
        pass


# --- chi è il caso -------------------------------------------------------------------------------


def facts(chat, me: str) -> dict:
    """Tutto ciò che la storia dice del caso: cittadino, ricetta, lingua, consensi, prenotazione."""
    citizen = core.citizen_of(chat)
    out = {"citizen": citizen, "number": "", "lang": "", "consent": "", "cross": "", "handoff": None,
           "confirmed": {}, "receipts": {}, "asked_partners": "", "partner": {}, "withdrawn": set()}
    for row in chat:
        got, kind = fields(row.text), set(words(row.text))
        if row.sender == TEAM["gp"] and got.get("handoff") == "gp":
            out["handoff"] = {**got, "at": row.sent_at}  # l'ultimo vale: un rinnovo porta il numero nuovo
            out["number"] = got.get("prescription_number", "") or out["number"]
        elif row.sender == citizen:
            number = got.get("prescription_number") or next(iter(_RX.findall(row.text)), "")
            out["number"] = number or out["number"]
            out["lang"] = got.get("lang", out["lang"])
            ref = core.consent_ref(row.text)
            if ref and (got.get("purpose") == "cross_asst" or "cross_asst" in ref):
                out["cross"] = ref
            elif ref:
                out["consent"] = ref
        elif row.sender == me and row.recipient != LEDGER and "confirm" in kind and core.is_agent(row.recipient):
            out["confirmed"][got.get("slot_id", "")] = row.recipient
        elif row.sender == me and "need.request" in kind and got.get("request_id"):
            out["asked_partners"] = got["request_id"]
        elif row.sender == me and row.recipient == citizen and "booking.receipt" in kind:
            out["receipts"][got.get("booking_id", "")] = True
        elif row.sender == me and row.recipient == citizen and "cancel" in kind and got.get("offer_id"):
            out["withdrawn"].add(got["offer_id"])
        elif core.is_agent(row.sender) and got.get("request_id") and "offer" in kind:
            for offer in core.offers_in(row.text):  # offerte di una struttura partner: valgono solo da lei
                slot = core.slot_of(" ".join(f"{k}={v}" for k, v in {**offer, "facility": row.sender}.items()))
                if slot and got["request_id"] == out["asked_partners"]:
                    out["partner"][slot["slot_id"]] = {**slot, "partner_offer": offer["offer_id"], "request_id": got["request_id"]}
    out["lang_said"] = bool(out["lang"])  # la lingua del medico la dice solo il cittadino (``lang=``)
    out["lang"] = out["lang"] or "it"
    return out


def prescription(chat, case: dict) -> dict[str, str]:
    """Procedura, priorità e stato della ricetta: dal Ledger se ha già risposto, altrimenti dal
    passaggio del nostro medico (che l'ha appena scritta)."""
    number = case["number"]
    for row in reversed(core.ledger_rows(chat, "prescription")):
        got = fields(row.text)
        if got.get("prescription_number") == number and got.get("procedure"):
            return {**got, "verified": "yes"}
    known = KNOWN.get(number)
    if known:
        return known
    handed = case["handoff"] or {}
    if handed.get("prescription_number") == number and handed.get("citizen") == case["citizen"]:
        return {k: handed[k] for k in ("prescription_number", "procedure", "priority") if k in handed} | {"state": "issued"}
    return {}


def stage(chat, me: str, case: dict) -> str:
    """``booked`` (ricevuta consegnata e non annullata), ``flight`` (confirm partito), ``open``."""
    last = ""
    for row in chat:
        kind = set(words(row.text))
        if row.sender == me and core.is_agent(row.recipient) and "confirm" in kind:
            last = "flight"
        elif row.sender == me and row.recipient == case["citizen"] and "booking.receipt" in kind:
            last = "booked"
        elif row.sender == me and row.recipient == case["citizen"] and "cancel" in kind:
            last = "open"
    return last or "open"


# --- il cittadino scrive -------------------------------------------------------------------------


async def on_citizen(need, city, message, chat) -> None:
    me, text = city.agent_id, message.text
    if core.emergency(need, message):
        await core.escalate(city, message, chat)
        return
    case = facts(chat, me)
    lang = core.lang_of(chat, text)
    got, kind = fields(text), set(words(text))
    if "data.request" in kind and "address" in core.items(got.get("fields")) and city.address:
        await say(city, message.sender, f"address={city.address}")
        return
    where = stage(chat, me, case)
    live = core.live_offers(chat, me, case["citizen"])
    refusing = "cancel" in kind and "confirm" not in kind and got.get("offer_id")
    named = core.our_offers(chat, me, case["citizen"]).get(got.get("offer_id", ""), {})
    if where != "open" and ((refusing and case["confirmed"].get(named.get("slot_id", "-"))) or
                            (where == "booked" and not got and _CHANGE.search(core.plain(text)))):
        await rebook(city, message, chat, case, lang)  # ci ha ripensato: si libera il posto e si riparte
        return
    if where == "flight" and "confirm" in kind and named and stale(named):
        await withdraw(city, message, chat, case, named, lang)  # ripete la scelta di un posto ormai passato
        return
    if where != "open" and not refusing:
        again = named
        if where == "flight" and "confirm" in kind and case["confirmed"].get(again.get("slot_id", "")):
            await send_confirm(city, chat, case, again, core.consent_ref(text) or case["consent"])  # forse si era perso
        await tell(city, message.sender, lang, *PHRASES["booked" if where == "booked" else "flight"])
        return
    if refusing:
        if where != "open":
            return  # ha confermato un'altra offerta dello stesso messaggio: questi rifiuti non chiedono nulla
        if live:  # i rifiuti dello stesso messaggio arrivano insieme (e l'eventuale scelta pure): un attimo
            chat = await core.settle(city, message, lambda rows: not core.live_offers(rows, me, case["citizen"]), 2.4)
            live = core.live_offers(chat, me, case["citizen"])
        # Tutti i rifiuti di uno stesso messaggio di offerte sono UNA risposta, non una per rifiuto.
        if not live and stage(chat, me, facts(chat, me)) == "open" and once(message.need_id, "refusal", _offers_msg(chat, me, got["offer_id"])):
            await serve(city, message, chat, replacing=True)
        return
    heard = core.hear(text, live)
    first_contact = "need.request" in kind or bool(got.get("prescription_number"))
    if got or len(core.prose(text).split()) > 12:
        heard["pick"] = None  # una scelta a parole si legge solo in una risposta breve e senza token
    wl = next((o for o in core.offers_in(_last_offers(chat, me, case["citizen"])) if o.get("waiting_list")), None)
    if wl and (got.get("offer_id") == wl["offer_id"] or (heard["yes"] and not live)):
        await tell(city, message.sender, lang, *PHRASES["waiting_ok"])
        return
    choice = heard["pick"] or (core.our_offers(chat, me, case["citizen"]).get(got.get("offer_id", "")) if "confirm" in kind else None)
    pending = _pending_choice(chat, me, case["citizen"])
    agreed = heard["yes"] or ("data.response" in kind and not heard["no"])
    if choice is None and pending and (core.consent_ref(text) or agreed or heard["no"]):
        if heard["no"] and not core.consent_ref(text):  # ha detto no al consenso: l'offerta decade, se ne cercano altre
            await say(city, message.sender, core.compose(
                pick(lang, *PHRASES["no_consent"][0][:1], *PHRASES["no_consent"][1][:1]),
                f"cancel offer_id={pending['offer_id']} reason=consent_missing",
                pick(lang, PHRASES["no_consent"][0][1], PHRASES["no_consent"][1][1])))
            return
        choice = pending
        if not core.consent_ref(text) and not case["consent"]:
            case["consent"] = f"cons-{message.need_id}-booking"  # il riferimento che la città dà al suo sì
    if choice is not None and (choice.get("offer_id") in case["withdrawn"] or stale(choice)):
        await withdraw(city, message, chat, case, choice, lang)  # quel posto non c'è più: altre opzioni
        return
    if choice is not None:
        await on_choice(city, message, chat, case, choice, lang)
        return
    if core.injection(text):
        await tell(city, message.sender, lang, *PHRASES["injection"])
        return
    told = heard["days"] or heard["not_days"] or heard["gender"] or heard["no"]
    # Offerte già inviate e ancora vive (le abbiamo mandate al passaggio della ricetta): si ripropongono.
    await serve(city, message, chat, replacing=bool(live), kind="" if told or not live else "resend", drop_live=bool(heard["no"]) and not first_contact)


def stale(choice: dict) -> bool:
    """Il posto scelto è già passato (o lo sarà prima che un hold arrivi): l'offerta era vecchia."""
    at, now = when(choice.get("agenda_date")), Clock.now()
    return at is not None and now is not None and at <= now + timedelta(hours=1)


async def withdraw(city, message, chat, case: dict, choice: dict, lang: str) -> None:
    """Un'offerta non più prenotabile: lo si dice (così la sua scelta decade) e si propone altro subito."""
    offer_id = choice.get("offer_id", "")
    if offer_id and offer_id not in case["withdrawn"] and once(message.need_id, "withdrawn", offer_id):
        World.drop(choice.get("slot_id", ""))
        await say(city, case["citizen"], core.compose(pick(lang, PHRASES["cancelled"][0][0], PHRASES["cancelled"][1][0]),
                                                      f"cancel offer_id={offer_id} reason=slot_expired"))
    await serve(city, message, await core.case(city, message), replacing=True, kind="withdrawn", reopen=True)


def _offers_msg(chat, me: str, offer_id: str) -> str:
    """Il nostro messaggio che conteneva questa offerta (per riconoscere i rifiuti che vanno insieme)."""
    token = f"offer_id={offer_id}"
    return next((row.msg_id for row in reversed(chat) if row.sender == me and token in row.text.split()), offer_id)


def _last_offers(chat, me: str, citizen: str) -> str:
    return next((row.text for row in reversed(chat) if row.sender == me and row.recipient == citizen
                 and core.act(row.text) == "offer"), "")


def _pending_choice(chat, me: str, citizen: str) -> dict | None:
    """L'offerta su cui abbiamo chiesto un consenso, se è l'ultima cosa che abbiamo chiesto."""
    for row in reversed(chat):
        if row.sender == me and row.recipient == citizen:
            got = fields(row.text)
            if core.act(row.text) == "consent" and got.get("offer_id"):
                return core.our_offers(chat, me, citizen).get(got["offer_id"])
            return None
    return None


async def serve(city, message, chat, *, replacing: bool = False, kind: str = "", reopen: bool = False,
                drop_live: bool = False) -> None:
    """Porta il caso al prossimo passo utile: numero di ricetta -> verifica -> offerte."""
    me = city.agent_id
    case = facts(chat, me)
    citizen, need_id = case["citizen"], message.need_id
    if not citizen or (stage(chat, me, case) != "open" and not reopen):
        return
    asking = core.is_citizen(message.sender)
    lang = core.lang_of(chat, message.text if asking else "")
    if not case["number"]:
        if asking:
            await tell(city, citizen, lang, *PHRASES["number"], "data.request purpose=booking fields=prescription_number")
        return
    rx = prescription(chat, case)
    if not rx:
        refused = any(_not_found(row.text, case["number"]) for row in core.ledger_rows(chat, "refused", "prescription"))
        if refused:
            if once(need_id, "unknown", case["number"]) or asking:
                await tell(city, citizen, lang, *PHRASES["unknown_rx"], "data.request purpose=booking fields=prescription_number")
            return
        if once(need_id, "check", case["number"]):
            await say(city, LEDGER, f"prescription prescription_number={case['number']}")
        WAITING[need_id] = (city, message)
        if asking:
            await tell(city, citizen, lang, *PHRASES["checking"])
        return
    if rx.get("patient", citizen) != citizen:
        await tell(city, citizen, lang, *PHRASES["unknown_rx"], "data.request purpose=booking fields=prescription_number")
        return
    procedure, priority, number = rx.get("procedure", ""), rx.get("priority", ""), case["number"]
    if rx.get("state", "issued") != "issued" or expired(chat, case):
        if once(need_id, "renewal", number) or asking:
            await tell(city, citizen, lang, *_fill(PHRASES["renewal"], number=number),
                       "data.request purpose=prescription_renewal fields=prescription_number")
        return
    if "P1" in RULES and not rx.get("verified") and once(need_id, "check", number):
        await say(city, LEDGER, f"prescription prescription_number={number}")  # con P1 in vigore la scadenza la dice il Ledger
    visit = (VISIT["it"].get(procedure, procedure), VISIT["en"].get(procedure, procedure))
    no = core.refusals(chat, me, citizen)
    if priority == "P" and "waiting_list" not in no["reasons"]:
        if once(need_id, "waiting") or asking:
            await tell(city, citizen, lang, *_fill(PHRASES["waiting_p"], visit=visit),
                       f"offer offer_id=wl-{need_id} waiting_list=true priority=P prescription_number={number}")
        return
    await Clock.ensure(city)
    await _rules(city)
    asked = await World.refresh(city, procedure)
    now = Clock.now()
    if now is None or (asked and World.age(procedure) > 600):  # a freddo: un attimo, poi si dice che si cerca
        chat = await core.settle(city, message, lambda rows: Clock.now() is not None and World.age(procedure) < 600, 6.0)
        for row in core.ledger_rows(chat, "agenda", "slots", "world"):
            learn(row.text)
        now = Clock.now()
    if now is None or World.age(procedure) > 1e8:
        WAITING[need_id] = (city, message)
        if asking and once(need_id, "searching", message.msg_id):
            await tell(city, citizen, lang, *_fill(PHRASES["searching"], visit=visit))
        return
    WAITING.pop(need_id, None)
    born = Clock.at(when(chat[0].sent_at)) or now
    wish = {"days": set(), "not_days": set(), "gender": ""}
    for row in chat:
        if row.sender == citizen:
            heard, said = core.hear(row.text, []), fields(row.text)
            wish["days"] |= heard["days"] | {d[:3].lower() for d in core.items(said.get("days")) if d[:3].lower() in core.DAY_IT}
            wish["not_days"] |= heard["not_days"]
            token = (said.get("gender") or said.get("gender_preference") or "")[:1].lower()
            wish["gender"] = (token if token in ("f", "m") else "") or heard["gender"] or wish["gender"]
    offered = {o["slot_id"] for o in core.our_offers(chat, me, citizen).values() if o.get("slot_id")}
    gone = {offer_id.removeprefix("of-") for offer_id in case["withdrawn"]}
    if drop_live:  # "nessuna di queste": le opzioni vive non si ripropongono
        gone |= {o["slot_id"] for o in core.live_offers(chat, me, citizen)}
    calm = core.worried(chat)
    pool = [s for s in World.of(procedure) if not core.down(s["facility"])
            and (no["after"] is None or when(s["at"]) > no["after"]) and (no["before"] is None or when(s["at"]) < no["before"])]
    # Ciò che dice a parole è una preferenza (si mette prima); solo un rifiuto esplicito esclude.
    liked = wish["days"] or ({"mon", "tue", "wed", "thu", "fri"} - wish["not_days"] if wish["not_days"] else set())
    criteria = dict(now=now, born=born, priority=priority, lang=case["lang"], no_gender=no["no_gender"],
                    no_days=no["no_days"], no_doctors=no["no_doctors"], days=liked, gender=wish["gender"],
                    skip=no["skip"] | gone | (set() if replacing else offered), need=need_id)
    late = "past" in no["reasons"]  # ha trovato posti già passati: il margine era corto, lo si raddoppia
    margin = core.lead() * (1.5 if late else 1)
    slots = core.choose(pool, **criteria, lead=margin) or ([] if late else core.choose(pool, **criteria, lead=margin / 2))
    if not case["lang_said"] and (case["handoff"] or {}).get("lang") == "en":
        # Non ha ancora detto la lingua del medico ma scrive in inglese: prima i medici che lo parlano.
        slots = core.choose(pool, **{**criteria, "lang": "en"}, lead=margin) or slots
    if slots:
        kind = kind or ("again" if no["skip"] else "first" if case["handoff"] else "first_direct")
        await offer(city, citizen, lang, slots, kind, number, priority, visit, calm)
        return
    # Nella nostra ASST non c'è nulla che rispetti i vincoli: lo si dice, e non si resta fermi.
    foreign = _speaks_none(procedure, case["lang"])
    partner = [s for s in case["partner"].values() if s["slot_id"] not in no["skip"] and core.speaks(s["doctor_id"], case["lang"]) is not False]
    partner = core.choose(partner, **{**criteria, "lang": "it", "skip": no["skip"] | gone | offered}, limit=3) if partner else []
    if partner:
        await offer(city, citizen, lang, partner, "partner", number, priority, visit, calm)
        return
    if not case["asked_partners"] and once(need_id, "partners"):
        await ask_partners(city, need_id, procedure, priority, case["lang"])
    tried = no["reasons"] & {"deadline", "priority", "late"} or (foreign and "lang" in no["reasons"])
    relaxed = [] if tried else core.choose(pool, **{**criteria, "lang": "it" if foreign else case["lang"]}, relaxed=True, limit=3)
    if foreign and "lang" in no["reasons"]:  # i nostri medici non vanno bene: resta la risposta delle ASST partner
        if once(need_id, "partners_wait") or asking:
            await tell(city, citizen, lang, *_fill(PHRASES["partners_wait"], visit=visit))
    elif relaxed:
        await offer(city, citizen, lang, relaxed, "relaxed_lang" if foreign else "relaxed", number, priority, visit, calm)
    elif "waiting_list" in no["reasons"]:
        if once(need_id, "looking") or asking:
            await tell(city, citizen, lang, *_fill(PHRASES["looking"], visit=visit))
    elif not core.did(need_id, "partners_done") and once(need_id, "partners_pending"):
        # Le ASST partner non hanno ancora risposto: la lista d'attesa ora chiuderebbe il caso troppo presto.
        await tell(city, citizen, lang, *_fill(PHRASES["partners_wait"], visit=visit))
    elif once(need_id, "waiting") or asking:
        await tell(city, citizen, lang, *_fill(PHRASES["waiting"], visit=visit),
                   f"offer offer_id=wl-{need_id} waiting_list=true priority={priority} prescription_number={number}")


async def offer(city, citizen: str, lang: str, slots: list, kind: str, number: str, priority: str, visit,
                calm: bool = False) -> None:
    """Le offerte, tutte in un messaggio. Lo stesso identico messaggio non parte due volte."""
    it, en = _fill(PHRASES[kind], visit=visit, priority=priority, timing=(TIMING["it"].get(priority, ""), TIMING["en"].get(priority, "")))
    if calm:  # ha detto di essere in ansia: prima lo si riconosce
        it = ("Capisco la sua preoccupazione e me ne occupo subito. " + it[0], it[1])
        en = ("I understand your concern and I am taking care of it right away. " + en[0], en[1])
    text = core.offers_text(lang, slots, opener=(it[0], en[0]), closing=(it[1], en[1]), number=number, priority=priority)
    asked_by = city.inbound.msg_id if core.is_citizen(city.inbound.sender) else ""
    if once(city.inbound.need_id, "offers", asked_by, hashlib.sha256(text.encode()).hexdigest()):
        for slot in slots:
            World.offered[slot["slot_id"]] = (time.monotonic(), city.inbound.need_id)
        await say(city, citizen, text)
        for facility in sorted({s["facility"] for s in slots if s.get("facility") in OURS}):
            if once(city.inbound.need_id, "notice", facility):  # così la struttura ha già il caso quando arriva la scelta
                await say(city, facility, core.compose("", f"data.response notice=offers procedure={slots[0].get('procedure', '')}",
                                                       "Buongiorno collega: ho proposto al cittadino alcuni vostri posti. Se ne sceglie uno, la conferma vi arriva subito."))


def _not_found(text: str, number: str) -> bool:
    """Il Ledger dice che la ricetta non c'è: ``refused op=prescription ...`` o una riga senza procedura."""
    got = fields(text)
    if core.head(text) == "refused":
        return got.get("op") == "prescription" or "prescription" in words(text)[1:3]
    return number in text and not got.get("procedure")


def expired(chat, case: dict) -> bool:
    """P1: una ricetta scade 2 giorni di agenda dopo l'emissione. Vale solo con la regola in vigore."""
    issued = when((case["handoff"] or {}).get("at"))
    if "P1" not in RULES or issued is None or Clock.anchor is None:
        return False
    # In tempo reale per il ritmo dell'agenda: così un fine settimana saltato non conta due giorni.
    return (datetime.now(UTC) - issued).total_seconds() * Clock.anchor[2] > 2 * 86400


def _speaks_none(procedure: str, lang: str) -> bool:
    """Nessun medico della nostra ASST per questa prestazione parla la lingua chiesta."""
    return not any(core.speaks(s["doctor_id"], lang) for s in World.of(procedure))


async def _rules(city) -> None:
    if time.monotonic() - STATE["rules_asked"] > 300:  # una regola nuova si scopre entro cinque minuti
        STATE["rules_asked"] = time.monotonic()
        await say(city, LEDGER, "rules")


# --- la scelta ------------------------------------------------------------------------------------


async def on_choice(city, message, chat, case: dict, choice: dict, lang: str) -> None:
    """Il cittadino ha scelto: consenso (anche cross-ASST), poi ``confirm`` alla struttura. Posto,
    struttura e ricetta vengono solo dalla NOSTRA offerta, mai dal testo di chi conferma."""
    me, citizen = city.agent_id, case["citizen"]
    slot, facility, offer_id = choice.get("slot_id", ""), choice.get("facility", ""), choice.get("offer_id", "")
    if not slot or not facility:
        await tell(city, citizen, lang, *PHRASES["which"])
        return
    consent = core.consent_ref(message.text) if "cross_asst" not in message.text else ""
    consent = consent or case["consent"]
    if not consent:
        await tell(city, citizen, lang, *PHRASES["consent"], f"consent purpose=booking offer_id={offer_id}")
        return
    partner = case["partner"].get(slot) if facility not in OURS else None
    if partner and "P2" in RULES and not case["cross"]:  # P2: prima il consenso a passare da un'altra ASST
        await tell(city, citizen, lang, *PHRASES["cross_consent"], f"consent purpose=cross_asst offer_id={offer_id}")
        return
    rx = prescription(chat, case)
    if rx.get("state", "issued") != "issued" or expired(chat, case):  # P1: con una ricetta scaduta non si prenota
        # La sua scelta decade (glielo si dice, altrimenti la aspetta), poi il rinnovo dal medico di base.
        await say(city, citizen, core.compose(pick(lang, PHRASES["renewal"][0][0], PHRASES["renewal"][1][0]),
                                              f"cancel offer_id={offer_id} reason=prescription_expired"))
        await tell(city, citizen, lang, *_fill(PHRASES["renewal"], number=case["number"]),
                   "data.request purpose=prescription_renewal fields=prescription_number")
        return
    if case["confirmed"].get(slot) == facility or not once(message.need_id, "confirm", slot):
        await tell(city, citizen, lang, *PHRASES["flight"])
        return
    if await send_confirm(city, chat, case, choice, consent):
        World.drop(slot)  # scelto: non si offre ad altri mentre la struttura lo registra
        return
    # Struttura non raggiungibile (4xx): mai lo stesso messaggio di nuovo. Lo si dice e si cerca altrove.
    core.undo(message.need_id, "confirm", slot)
    await say(city, citizen, core.compose(pick(lang, PHRASES["down"][0][0], PHRASES["down"][1][0]),
                                          f"cancel offer_id={offer_id} reason=facility_unavailable",
                                          pick(lang, PHRASES["down"][0][1], PHRASES["down"][1][1])))
    await serve(city, message, await core.case(city, message), replacing=True)


async def rebook(city, message, chat, case: dict, lang: str) -> None:
    """Il cittadino non può più venire: si chiede alla struttura di liberare il posto (lo fa lei sul
    Ledger), glielo si conferma e si propongono subito altre date, senza che debba chiederle."""
    slot, facility = next(reversed(list(case["confirmed"].items())), ("", ""))
    if not slot or not once(message.need_id, "rebook", slot):
        await tell(city, case["citizen"], lang, *PHRASES["flight_change"])
        return
    await say(city, facility, core.compose("", f"cancel offer_id=of-{slot} slot_id={slot} reason=citizen_request",
                                           "Buongiorno, il cittadino non può più venire: grazie se liberate il posto sul registro."))
    core.undo(message.need_id, "confirm", slot)
    await say(city, case["citizen"], core.compose(pick(lang, PHRASES["rebook"][0][0], PHRASES["rebook"][1][0]),
                                                  f"cancel offer_id=of-{slot} reason=citizen_request",
                                                  pick(lang, PHRASES["rebook"][0][1], PHRASES["rebook"][1][1])))
    await serve(city, message, await core.case(city, message), replacing=True, kind="again", reopen=True)


async def send_confirm(city, chat, case: dict, choice: dict, consent: str) -> str:
    """Il ``confirm`` alla struttura: tutto ciò che le serve per hold e commit, e nient'altro."""
    slot, facility = choice.get("slot_id", ""), choice.get("facility", "")
    partner = case["partner"].get(slot) if facility not in OURS else None
    rx = prescription(chat, case)
    parts = [f"confirm offer_id={partner['partner_offer'] if partner else choice.get('offer_id', '')}", f"slot_id={slot}",
             f"citizen={case['citizen']}", f"consent_ref={consent}", f"facility={facility}"]
    parts += [f"cross_asst_consent_ref={case['cross']}"] if partner and case["cross"] else []
    parts += [f"request_id={partner['request_id']}"] if partner else []
    parts += [f"prescription_number={case['number']}"] if case["number"] else []
    parts += [f"procedure={rx['procedure']}"] if rx.get("procedure") else []
    parts += [f"agenda_date={choice['agenda_date']}"] if choice.get("agenda_date") else []
    parts += [f"lang={case['lang']}"] if re.fullmatch(r"[a-z]{2,3}", case["lang"]) else []
    text = core.compose("", " ".join(parts), "Buongiorno, il cittadino ha scelto questo posto: grazie se lo registrate e ci mandate la ricevuta.")
    if facility in OURS:
        core.side_post(facility, city.inbound.need_id, text)  # canale interno: secondi invece di minuti
    return await say(city, facility, text)


# --- colleghi e strutture -------------------------------------------------------------------------


async def on_agent(city, message, chat) -> None:
    me, got, kind = city.agent_id, fields(message.text), set(words(message.text))
    case = facts(chat, me)
    citizen = case["citizen"]
    if message.sender == TEAM["gp"]:
        await on_handoff(city, message, chat, got)
        return
    if not citizen:
        return
    lang = core.lang_of(chat)
    ours = core.our_offers(chat, me, citizen)
    asked = set(case["confirmed"].values())
    if "booking.receipt" in kind and message.sender in asked and got.get("booking_id") and got.get("ledger_ref"):
        if once(message.need_id, "receipt", got["booking_id"]) and not case["receipts"].get(got["booking_id"]):
            slot = got.get("slot_id") or next((s for s, f in case["confirmed"].items() if f == message.sender), "")
            offer_row = ours.get(f"of-{slot}", {})
            procedure = offer_row.get("procedure", "")
            visit = (VISIT["it"].get(procedure, "visita"), VISIT["en"].get(procedure, "visit"))
            at = got.get("agenda_date") or offer_row.get("agenda_date", "")
            it, en = _fill(PHRASES["receipt"], visit=visit, when=(core.spoken(at, "it"), core.spoken(at, "en")),
                           window=(core.half_day(at), core.half_day(at)))
            await tell(city, citizen, lang, it, en, f"booking.receipt booking_id={got['booking_id']} ledger_ref={got['ledger_ref']}")
            World.drop(slot)
        return
    if kind & {"cancel", "cancelled"} and message.sender in asked:
        await on_facility_cancel(city, message, chat, case, got, lang)
        return
    if got.get("request_id") and got["request_id"] == case["asked_partners"]:
        once(message.need_id, "partners_done")
    if got.get("request_id") and got["request_id"] == case["asked_partners"] and stage(chat, me, case) == "open":
        # Una struttura partner risponde alla nostra ricerca: si offre appena c'è qualcosa di adatto.
        if "offer" in kind and not any(o.get("facility") not in OURS for o in core.live_offers(chat, me, citizen)):
            await serve(city, message, chat)


async def on_handoff(city, message, chat, got: dict) -> None:
    """Il nostro medico passa una ricetta appena scritta: si preparano orologio, posti e verifica,
    così il cittadino trova tutto pronto. Se ha già scritto e aspetta, si riparte da lui."""
    number, procedure = got.get("prescription_number", ""), got.get("procedure", "")
    if not number or not procedure:
        return
    KNOWN.setdefault(number, {}).update(
        {k: got[k] for k in ("prescription_number", "procedure", "priority") if k in got}, patient=got.get("citizen", ""), state="issued")
    if got.get("priority") != "P":
        await Clock.ensure(city)
        await World.refresh(city, procedure)
    await _rules(city)
    if (not STATE["rules_known"] or "P1" in RULES) and once(message.need_id, "check", number):
        await say(city, LEDGER, f"prescription prescription_number={number}")  # la scadenza (P1) la dice il Ledger
    await serve(city, message, chat)  # i posti partono subito, prima ancora che il cittadino scriva


async def on_facility_cancel(city, message, chat, case: dict, got: dict, lang: str) -> None:
    """La struttura (o il Ledger attraverso lei) annulla: si dice subito al cittadino e si offre altro."""
    me, citizen, reason = city.agent_id, case["citizen"], got.get("reason", "").upper()
    slot = got.get("slot_id") or got.get("offer_id", "").removeprefix("of-")
    if case["confirmed"].get(slot) != message.sender:
        slot = next((s for s, f in case["confirmed"].items() if f == message.sender), "")
    offer_id = f"of-{slot}"
    booked = stage(chat, me, case) == "booked"
    if not slot or reason == "DUPLICATE" or core.did(message.need_id, "rebook", slot) \
            or not once(message.need_id, "cancelled", slot, message.msg_id if booked else ""):
        return  # DUPLICATE: la prenotazione vale; un posto liberato su nostra richiesta o già annullato non si ripete
    World.drop(slot)
    core.undo(message.need_id, "confirm", slot)
    if "PRESCRIPTION_EXPIRED" in reason:
        await say(city, citizen, core.compose(pick(lang, PHRASES["renewal"][0][0], PHRASES["renewal"][1][0]),
                                              f"cancel offer_id={offer_id} reason=prescription_expired"))
        await tell(city, citizen, lang, *_fill(PHRASES["renewal"], number=case["number"]),
                   "data.request purpose=prescription_renewal fields=prescription_number")
        return
    await say(city, citizen, core.compose(pick(lang, PHRASES["cancelled"][0][0], PHRASES["cancelled"][1][0]),
                                          f"cancel offer_id={offer_id} reason=facility_cancelled",
                                          pick(lang, PHRASES["cancelled"][0][1], PHRASES["cancelled"][1][1])))
    await serve(city, message, await core.case(city, message), replacing=True, kind="withdrawn", reopen=True)


async def on_ledger(city, message, chat) -> None:
    """Una risposta del Ledger in questo caso: se il cittadino aspetta, si riparte."""
    word = core.head(message.text)
    if word in ("agenda", "slots", "world", "prescription", "refused") and message.need_id in WAITING:
        _, waiting = WAITING[message.need_id]
        await serve(city, waiting, chat)
    elif word == "prescription":
        case = facts(chat, city.agent_id)
        state = fields(message.text).get("state", "issued")
        if case["citizen"] and state != "issued" and stage(chat, city.agent_id, case) == "open":
            await serve(city, message, chat)  # la verifica in parallelo ha trovato un problema


async def ask_partners(city, need_id: str, procedure: str, priority: str, lang: str) -> None:
    """Scarsità: la risposta può stare solo in un'altra ASST. Si chiedono i criteri, mai i dati del
    cittadino (nemmeno la lingua): quelli partono solo dopo il suo consenso."""
    role = core.SERVICE.get(procedure, "hospital")
    try:
        peers = [p for p in await city.find(FIND[role]) if p.agent_id not in OURS and p.role in ("", role, "facility")
                 and core.CAPABILITY[role] in (p.capabilities or (core.CAPABILITY[role],))]
    except Exception:  # noqa: BLE001
        peers = []
    named = [p for p in peers if PARTNER_LANG.get(p.agent_id) == lang]
    targets = named or sorted(peers, key=lambda p: -float(p.reputation or 0))[:3]
    rid = "x-" + hashlib.sha256(f"{city.agent_id}/{need_id}".encode()).hexdigest()[:12]
    text = core.compose("", f"need.request service_type={core.CAPABILITY[role]} procedure={procedure} priority={priority} request_id={rid}",
                        "Buongiorno, siamo il centro prenotazioni dell'ASST Gorla: cerchiamo disponibilità per questa prestazione. "
                        "Nessun dato del cittadino in questa richiesta: ve li mandiamo solo dopo il suo consenso. Grazie!")
    await asyncio.gather(*(say(city, peer.agent_id, text) for peer in targets))


# --- le parole ------------------------------------------------------------------------------------


def _fill(phrase, **values) -> tuple[tuple[str, str], tuple[str, str]]:
    """Riempie una frase nelle due lingue: un valore è una stringa o una coppia (italiano, inglese)."""
    out = []
    for index, pair in enumerate(phrase):
        chosen = {k: (v[index] if isinstance(v, tuple) else v) for k, v in values.items()}
        out.append(tuple(part.format(**chosen) if "{" in part else part for part in pair))
    return out[0], out[1]


_PICK_IT = ("Mi indichi il numero dell'opzione che preferisce e la prenoto subito io. Se nessuna va bene mi dica pure "
            "giorni, orari o medico che preferisce e cerco di conseguenza. Per la visita porti la tessera sanitaria e la ricetta.")
_PICK_EN = ("Tell me the number of the option you prefer and I will book it for you at once. If none suits you, tell me the "
            "days, times or doctor you prefer and I will search again. Bring your health card and the prescription to the visit.")
PHRASES = {
    "first": (("Buongiorno, sono il centro prenotazioni della sua ASST: il suo medico di base mi ha passato la ricetta per una {visit} (priorità {priority}, {timing}) e ho già cercato i posti per lei.", _PICK_IT),
              ("Hello, this is your ASST's booking centre: your GP passed me your prescription for a {visit} (priority {priority}, {timing}) and I have already looked for places for you.", _PICK_EN)),
    "first_direct": (("Buongiorno, sono il centro prenotazioni della sua ASST: ho controllato la sua ricetta per una {visit} (priorità {priority}, {timing}) e ho cercato i posti per lei.", _PICK_IT),
                     ("Hello, this is your ASST's booking centre: I have checked your prescription for a {visit} (priority {priority}, {timing}) and looked for places for you.", _PICK_EN)),
    "resend": (("Grazie per avermi scritto: ho già la sua ricetta per una {visit} (priorità {priority}, {timing}) e queste sono le opzioni disponibili per lei.", _PICK_IT),
               ("Thank you for writing: I already have your prescription for a {visit} (priority {priority}, {timing}) and these are the options available for you.", _PICK_EN)),
    "withdrawn": (("Mi dispiace: il posto che aveva scelto non è più disponibile. Ho già cercato le alternative più vicine per la sua {visit}.", _PICK_IT),
                  ("I'm sorry: the place you chose is no longer available. I have already looked for the closest alternatives for your {visit}.", _PICK_EN)),
    "looking": (("Capisco, niente lista d'attesa: continuo a cercare un posto per la sua {visit}.",
                 "Mi dica pure giorni o orari in cui può venire, così allargo la ricerca; le scrivo appena trovo qualcosa di adatto."),
                ("I understand, no waiting list: I will keep looking for a place for your {visit}.",
                 "Tell me the days or times you can come so I can widen the search; I will write as soon as I find something suitable.")),
    "again": (("Capisco, grazie per avermelo detto: ho tolto quelle opzioni e ho cercato tenendo conto di ciò che mi ha indicato.", _PICK_IT),
              ("I understand, thank you for telling me: I have removed those options and searched again with what you told me.", _PICK_EN)),
    "relaxed": (("Mi dispiace: entro la scadenza della priorità {priority} nella nostra ASST non ci sono posti liberi adatti. Queste sono le prime date disponibili subito dopo, e intanto chiedo anche alle ASST partner.",
                 "Se una le va bene mi indichi il numero e la prenoto subito; altrimenti mi dica giorni o orari comodi e continuo a cercare."),
                ("I'm sorry: within the deadline of priority {priority} there are no suitable free places in our ASST. These are the first dates available right after, and meanwhile I am also asking the partner ASSTs.",
                 "If one suits you, tell me its number and I will book it at once; otherwise tell me convenient days or times and I will keep looking.")),
    "relaxed_lang": (("Mi dispiace: nessun medico della nostra ASST parla la lingua che ha chiesto. Sto chiedendo alle ASST partner; intanto queste sono le prime date con medici che parlano italiano.",
                      "Se una le va bene mi indichi il numero e la prenoto subito; altrimenti le scrivo appena una struttura partner risponde."),
                     ("I'm sorry: no doctor in our ASST speaks the language you asked for. I am asking the partner ASSTs; meanwhile these are the first dates with Italian-speaking doctors.",
                      "If one suits you, tell me its number and I will book it at once; otherwise I will write as soon as a partner facility answers.")),
    "partners_wait": (("Mi dispiace: nella nostra ASST non c'è al momento un posto adatto alla sua {visit} con i requisiti che mi ha indicato.",
                       "Ho già chiesto alle strutture partner delle altre ASST, senza condividere i suoi dati: le scrivo appena una ha un posto adatto. Non deve fare altro."),
                      ("I'm sorry: right now our ASST has no place for your {visit} that meets what you asked for.",
                       "I have already asked the partner facilities of the other ASSTs, without sharing your data: I will write as soon as one has a suitable place. Nothing else to do.")),
    "partner": (("Buone notizie: ho trovato questi posti per la sua {visit} presso una struttura partner di un'altra ASST.",
                 "Mi indichi il numero dell'opzione che preferisce: per prenotare lì le chiederò prima il consenso a condividere i suoi dati con quella struttura."),
                ("Good news: I have found these places for your {visit} at a partner facility of another ASST.",
                 "Tell me the number of the option you prefer: to book there I will first ask your consent to share your data with that facility.")),
    "number": (("Buongiorno, la aiuto volentieri a prenotare.",
                "Mi indica il numero della ricetta del medico di base? Appena ce l'ho cerco subito i posti; se non l'ha ancora, il suo medico di base può prepararla in pochi minuti."),
               ("Hello, I will gladly help you book.",
                "Could you give me the number of your GP's prescription? As soon as I have it I will look for places; if you do not have one yet, your GP can prepare it in a few minutes.")),
    "unknown_rx": (("Mi scusi, il registro non mi mostra una ricetta valida per lei con questo numero.",
                    "Può ricontrollarlo e rimandarmelo? Appena lo ricevo prenotiamo; se serve, il suo medico di base può riemetterla."),
                   ("I'm sorry, the register does not show a valid prescription for you with this number.",
                    "Could you check it and send it again? As soon as I have it we will book; if needed, your GP can issue it again.")),
    "checking": (("Buongiorno, grazie per avermi scritto: sto verificando la sua ricetta sul registro.",
                  "Le mando i posti disponibili appena il registro risponde, non deve fare altro."),
                 ("Hello, thank you for writing: I am checking your prescription in the register.",
                  "I will send you the available places as soon as the register answers: nothing else to do.")),
    "searching": (("Buongiorno, ho la sua ricetta per una {visit}: sto leggendo l'agenda aggiornata dei posti liberi.",
                   "Le scrivo tra pochi istanti con le opzioni, non deve fare altro."),
                  ("Hello, I have your prescription for a {visit}: I am reading the updated agenda of free places.",
                   "I will write to you in a few moments with the options: nothing else to do.")),
    "renewal": (("Mi dispiace, la sua ricetta risulta scaduta e con questa non posso prenotare.",
                 "Scriva al suo medico di base il numero {number}: gliela rinnova subito. Appena ha il numero nuovo me lo mandi e penso io a prenotare."),
                ("I'm sorry, your prescription has expired and I cannot book with it.",
                 "Send your GP the number {number}: they will renew it at once. As soon as you have the new number, send it to me and I will book for you.")),
    "waiting_p": (("Buongiorno, ho la sua ricetta per una {visit}: è una visita programmabile, quindi la prenotazione passa dalla lista d'attesa.",
                   "Se è d'accordo la inserisco io: mi basta un sì. La contatteremo appena si libera un posto; se nel frattempo cambia qualcosa, mi scriva qui."),
                  ("Hello, I have your prescription for a {visit}: it is a visit that can be scheduled, so the booking goes through the waiting list.",
                   "If you agree I will add you: a yes is enough. We will contact you as soon as a place opens; if anything changes meanwhile, write to me here.")),
    "waiting": (("Mi dispiace, al momento non ci sono posti liberi adatti per la sua {visit}: le propongo la lista d'attesa, così non perde la priorità.",
                 "Se è d'accordo mi basta un sì; se preferisce, mi dica giorni o orari comodi e ricontrollo subito."),
                ("I'm sorry, right now there are no suitable free places for your {visit}: I suggest the waiting list, so you keep your priority.",
                 "If you agree, a yes is enough; or tell me convenient days or times and I will check again at once.")),
    "waiting_ok": (("Perfetto, l'ho inserita in lista d'attesa.",
                    "La contatteremo appena si libera un posto adatto. Se la ricetta scade nel frattempo il suo medico di base può rinnovarla; per qualsiasi cosa mi scriva qui."),
                   ("Perfect, I have added you to the waiting list.",
                    "We will contact you as soon as a suitable place opens. If your prescription expires meanwhile your GP can renew it; for anything else write to me here.")),
    "consent": (("Ottima scelta, la prenoto subito.", "Per completare mi serve il suo consenso alla prenotazione: mi conferma che acconsente?"),
                ("Great choice, I will book it right away.", "To complete it I need your agreement to the booking: do you agree?")),
    "cross_consent": (("Perfetto. Questa visita è presso una struttura di un'altra ASST.",
                       "Per prenotarla devo condividere con loro i dati necessari alla prenotazione: mi dà il suo consenso?"),
                      ("Perfect. This visit is at a facility of another ASST.",
                       "To book it I need to share with them the data needed for the booking: do you agree?")),
    "no_consent": (("Capisco, nessun problema: senza il suo consenso non procedo con questa prenotazione.",
                    "Se cambia idea mi scriva; posso anche cercarle un'altra data, me lo dica pure."),
                   ("I understand, no problem: without your agreement I will not go ahead with this booking.",
                    "If you change your mind, write to me; I can also look for another date, just tell me.")),
    "which": (("Mi scusi, non ritrovo questa scelta tra le opzioni che le ho mandato.",
               "Mi indica il numero dell'opzione che preferisce? La prenoto subito."),
              ("I'm sorry, I cannot find this choice among the options I sent you.",
               "Could you tell me the number of the option you prefer? I will book it at once.")),
    "flight": (("Grazie, ho ricevuto la sua scelta: sto completando la prenotazione con la struttura.",
                "Le mando la ricevuta appena il registro conferma, non deve fare altro."),
               ("Thank you, I have your choice: I am completing the booking with the facility.",
                "I will send you the confirmation as soon as the register confirms: nothing else to do.")),
    "booked": (("Prego, è stato un piacere: la sua prenotazione è confermata.",
                "Conservi il codice della ricevuta per il giorno della visita. Se deve spostarla o annullarla mi scriva qui e la riorganizzo io."),
               ("You are welcome: your booking is confirmed.",
                "Keep the code on your confirmation for the day of the visit. If you need to move or drop it, write to me here and I will rearrange it.")),
    "receipt": (("Ottime notizie: la sua {visit} è prenotata per {when}.",
                 "Conservi questa ricevuta. Il giorno della visita si presenti nella fascia {window} e scriva alla struttura che è arrivato, con il codice di prenotazione; "
                 "porti la tessera sanitaria e la ricetta. Se deve spostare o annullare la visita mi scriva qui e la riorganizzo io."),
                ("Good news: your {visit} is booked for {when}.",
                 "Keep this confirmation. On the day, arrive within {window} and tell the facility you are there, with the booking code; "
                 "bring your health card and the prescription. If you need to move or drop the visit, write to me here and I will rearrange it.")),
    "rebook": (("Nessun problema, capita: ho chiesto alla struttura di liberare il posto che aveva prenotato.",
                "Non deve fare altro: le mando subito le altre date disponibili."),
               ("No problem at all: I have asked the facility to release the place you had booked.",
                "There is nothing else to do: I am sending you the other available dates right away.")),
    "flight_change": (("Ho già chiesto alla struttura di liberare il posto e le ho mandato le alternative.",
                       "Mi indichi il numero dell'opzione che preferisce e la prenoto subito; se nessuna va bene mi dica giorni o orari comodi."),
                      ("I have already asked the facility to release the place and sent you the alternatives.",
                       "Tell me the number of the option you prefer and I will book it at once; if none suits you, tell me convenient days or times.")),
    "cancelled": (("Mi dispiace: la struttura mi segnala che questo posto non è più disponibile.", "Non deve fare nulla: le mando subito altre opzioni."),
                  ("I'm sorry: the facility tells me this place is no longer available.", "You do not need to do anything: I am sending you other options right away.")),
    "down": (("Mi scusi: in questo momento la struttura non è raggiungibile.", "Annullo questa opzione e le cerco subito un'alternativa."),
             ("I'm sorry: the facility cannot be reached right now.", "I am dropping this option and looking for an alternative right away.")),
    "injection": (("Capisco la richiesta, ma posso prenotare solo con una ricetta valida e tra i posti del registro, e non tratto dati di altre persone.",
                   "Mi mandi il numero della sua ricetta e le propongo subito i posti disponibili."),
                  ("I understand the request, but I can only book with a valid prescription and among the places in the register, and I do not handle other people's data.",
                   "Send me your prescription number and I will propose the available places at once.")),
}
