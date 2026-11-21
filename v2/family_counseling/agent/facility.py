"""Brain delle tre strutture (ospedale, specialista, consultorio). Fonte unica: ``v2/facility.py``;
``v2/sync.sh`` lo copia nei tre agenti. Ogni ``agent.py`` chiama ``handle(ruolo, need, city)``.

Una struttura tiene e conferma i posti sul Ledger, consegna la ricevuta, registra l'arrivo; lo
specialista archivia il referto; il consultorio prenota da sé, senza ricetta.

Ogni turno fa due cose: gestisce il messaggio arrivato, poi ``advance`` rilegge il caso e compie
ogni passo dovuto e non ancora fatto (commit dopo ``held``, ricevuta dopo ``committed``, annullo
dopo un rifiuto, conferma dopo ``checked_in``, referto). Così non importa se la risposta del Ledger
arriva nello stesso turno, in un turno suo o dopo minuti: il passo si fa una volta sola.
Una ricevuta parte SOLO dopo un ``committed`` del Ledger.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections import OrderedDict

from city_sdk.city import Message

import core
from core import LEDGER, TEAM, Clock, World, fields, once, pick, say, sent, tell, words

PROCEDURES = {"hospital": ("VIS-FIS",), "specialist_care": ("VIS-CARD", "CTRL-CARD"), "family_counseling": ("CONS-GIN",)}
PLACE = {"hospital": ("l'ospedale", "the hospital"), "specialist_care": ("l'ambulatorio di cardiologia", "the cardiology clinic"),
         "family_counseling": ("il consultorio", "the family counselling centre")}
VISIT = {"hospital": ("visita fisiatrica", "physiatry visit"), "specialist_care": ("visita cardiologica", "cardiology visit"),
         "family_counseling": ("visita ginecologica", "gynaecology visit")}
GONE = ("EXPIRED", "NO_CAPACITY", "SLOT_TAKEN", "STALE_FENCE", "FACILITY_CANCELLED")
_ARRIVED = re.compile(r"\bcheck[\s-]?in\b|\bsono\s+(?:arrivat[oaie]|qui|qua|in\s+(?:sala|accettazione|struttura|ospedale|ambulatorio))\b"
                      r"|\bsiamo\s+(?:arrivat[ie]|qui)\b|\beccomi\b|\ballo\s+sportello\b|\bin\s+accettazione\b"
                      r"|\bi\s*'?\s*m\s+here\b|\bi\s+am\s+here\b|\bi(?:\s*'?ve|\s+have)\s+(?:just\s+)?arrived\b|\bjust\s+arrived\b|\bat\s+the\s+(?:desk|reception)\b")
_CODE = re.compile(r"\b(?:bk-[0-9a-z-]{6,}|H-\d+)\b")
_ASKS = re.compile(r"\?|\b(?:a che ora|quando|dove|come|what time|when|where|how)\b")
BOOKERS = {"ids": set(), "at": 0.0}  # i centri prenotazioni che l'Index mostra (per dieci minuti)
WAITING: dict[str, tuple] = {}       # casi del consultorio in attesa dell'agenda
# Canale interno: il confirm del nostro centro arriva dalla memoria condivisa in pochi secondi.
# Per parlare in un caso serve un City di quel caso: lo dà il primo messaggio che vi riceviamo
# (l'avviso del centro quando propone i nostri posti).
CITIES: OrderedDict = OrderedDict()  # need_id -> City dell'ultimo turno di quel caso
SIDE: dict[str, list] = {}           # need_id -> confirm arrivati dal canale interno
SIDE_DELAY, SIDE_EVERY = 60.0, 2.5   # si parte un minuto dopo l'avvio (mai durante la conformance)
_TASKS: set = set()


async def handle(role: str, need, city) -> None:
    async def turn(message, text: str) -> None:
        CITIES[message.need_id] = city
        CITIES.move_to_end(message.need_id)
        while len(CITIES) > 3000:
            CITIES.popitem(last=False)
        if core.side_ready() and once("side", role):
            keep(asyncio.ensure_future(side_loop(role, city.agent_id)))
        if message.sender == LEDGER:
            learn(message.text)
        elif core.is_agent(message.sender) and not relevant(message):
            return
        if once("warm", role):  # primo turno del processo: orologio e posti in memoria prima che servano
            await Clock.ensure(city)
            await World.refresh(city, PROCEDURES[role][0])
        async with core.turn_lock(message.need_id):
            chat = await case(city, message)
            if core.is_citizen(message.sender):
                await on_citizen(role, need, city, message, chat)
            elif core.is_agent(message.sender):
                await on_agent(role, city, message, chat)
            elif message.need_id in WAITING:
                await desk(role, city, WAITING[message.need_id][1], chat)
            await progress(role, city, message)
        wake(role, message.need_id)

    await core.run(need, city, turn)


async def progress(role: str, city, message) -> None:
    """Finché il Ledger risponde in fretta si prosegue nello stesso turno; se no, dal suo turno."""
    for _ in range(3):
        chat = await case(city, message)
        await advance(role, city, message, chat)
        mark = waiting(chat, city.agent_id)
        if not mark:
            break
        await core.settle(city, message, lambda rows: waiting(rows, city.agent_id) != mark, 2.0)


async def case(city, message) -> list:
    """La storia del caso, più i confirm arrivati dal canale interno (in ordine di tempo)."""
    rows = await core.case(city, message)
    extra = [m for m in SIDE.get(message.need_id, []) if all(r.msg_id != m.msg_id for r in rows)]
    if not extra:
        return rows
    ordered = rows + extra
    ordered.sort(key=lambda r: core.when(r.sent_at) or core.when("2000-01-01T00:00:00+00:00"))
    return ordered


def keep(task) -> None:
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def side_loop(role: str, me: str) -> None:
    """Legge il canale interno ogni pochi secondi: un confirm del nostro centro diventa subito un
    ``hold``, senza aspettare che il Relay consegni lo stesso confirm (che arriverà comunque)."""
    await asyncio.sleep(SIDE_DELAY)
    while True:
        try:
            for need_id, item in (await core.side_inbox(me)).items():
                text, city = str(item.get("text", "")), CITIES.get(need_id)
                digest = hashlib.sha256(text.encode()).hexdigest()[:16]
                if city is None or "confirm" not in words(text) or not once("side", need_id, digest):
                    continue
                row = Message(type="chat", sender=TEAM["booking_centre"], recipient=me, msg_id=f"side-{digest}",
                              need_id=need_id, body={"text": text}, sent_at=str(item.get("at") or ""))
                SIDE.setdefault(need_id, []).append(row)
                keep(asyncio.ensure_future(side_turn(role, city, row)))
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(SIDE_EVERY)


async def side_turn(role: str, city, row) -> None:
    try:
        async with core.turn_lock(row.need_id):
            await on_agent(role, city, row, await case(city, row))
            await progress(role, city, row)
    except Exception:  # noqa: BLE001
        pass


def relevant(message) -> bool:
    """Un agente ci scrive qualcosa che ci riguarda: una prenotazione, un annullo, una ricerca."""
    got, kind = fields(message.text), set(words(message.text))
    return bool(kind & {"confirm", "cancel"}) or ("need.request" in kind and bool(got.get("request_id")) and bool(got.get("procedure")))


def learn(text: str) -> None:
    word = core.head(text)
    if word == "agenda":
        Clock.learn(text)
    elif word in ("slots", "world"):
        World.learn(text)


def wake(role: str, current: str | None) -> None:
    for need_id, (city, message) in list(WAITING.items()):
        if need_id != current and Clock.now() is not None and World.age(PROCEDURES[role][0]) < 1e8:
            WAITING.pop(need_id, None)
            asyncio.ensure_future(_resume(role, city, message))


async def _resume(role: str, city, message) -> None:
    try:
        async with core.turn_lock(message.need_id):
            await desk(role, city, message, await case(city, message))
    except Exception:  # noqa: BLE001
        pass


# --- lo stato del caso, dalla storia ---------------------------------------------------------------


def bookings(chat, me: str) -> dict[str, dict]:
    """Per ogni posto di questo caso: chi l'ha chiesto, con che consenso, e fin dove è arrivato.
    ``state``: asked -> holding -> held -> committing -> booked | refused | cancelled | released.
    Un rifiuto vero del Ledger non porta identificativi: vale per la più vecchia scrittura di
    quel tipo ancora senza risposta (la coda del Ledger è ordinata)."""
    out: dict[str, dict] = {}
    pending: dict[str, list[str]] = {"hold": [], "commit": [], "checkin": [], "report": []}

    def find(got: dict) -> dict | None:
        for key in ("slot_id", "hold_id", "booking_id"):
            for entry in out.values():
                if got.get(key) and got[key] in (entry["slot"], entry.get("hold"), entry.get("booking")):
                    return entry
        return None

    for row in chat:
        got, word, kind = fields(row.text), core.head(row.text), set(words(row.text))
        if core.is_agent(row.sender) and "confirm" in kind and got.get("slot_id"):
            entry = out.setdefault(got["slot_id"], {"slot": got["slot_id"], "state": "asked", "holds": 0})
            entry["confirm_id"] = row.msg_id
            if entry["state"] in ("asked", "refused", "cancelled", "released"):
                entry.update(party=row.sender, offer=got.get("offer_id", ""), consent=got.get("consent_ref", ""),
                             cross=got.get("cross_asst_consent_ref", ""), number=got.get("prescription_number", ""),
                             citizen=got.get("citizen", ""), lang=got.get("lang", ""), request=got.get("request_id", ""))
        elif row.sender == me and row.recipient == LEDGER:
            entry = find(got)
            if entry is None and word == "hold" and got.get("slot_id"):  # scelta diretta del cittadino (consultorio)
                entry = out[got["slot_id"]] = {"slot": got["slot_id"], "state": "asked", "holds": 0, "party": got.get("citizen", ""),
                                               "citizen": got.get("citizen", ""), "consent": got.get("consent_ref", ""),
                                               "offer": f"of-{got['slot_id']}"}
            elif entry is None and word == "checkin" and got.get("booking_id"):  # un codice che questo caso non conosce
                entry = out[got["booking_id"]] = {"slot": got["booking_id"], "booking": got["booking_id"], "state": "external",
                                                  "holds": 0, "citizen": got.get("citizen", "")}
            if word in pending and entry is not None:
                pending[word].append(entry["slot"])
            if word == "hold" and entry is not None and entry["state"] != "booked":
                entry.update(state="holding", holds=entry["holds"] + 1)
            elif word == "commit" and entry is not None and entry["state"] != "booked":
                entry["state"] = "committing"  # un commit ripetuto non riporta indietro una prenotazione fatta
            elif word == "cancel" and entry is not None:
                entry["state"] = "released"
            elif word == "checkin" and entry is not None:
                entry.update(checkin="pending", tries=entry.get("tries", 0) + 1)
            elif word == "report" and entry is not None:
                entry["report"] = "pending"
        elif row.sender == LEDGER and word in ("held", "committed", "checked_in", "reported", "refused", "cancelled", "expired", "cancel"):
            op = {"held": "hold", "committed": "commit", "checked_in": "checkin", "reported": "report"}.get(word) \
                or got.get("op") or next((o for o in pending if o in kind), "")
            entry = find(got)
            if entry is None and op in pending and pending[op]:
                entry = out.get(pending[op][0])
            if entry is None:
                continue
            if op in pending and entry["slot"] in pending[op]:
                pending[op].remove(entry["slot"])
            if word == "held":
                entry.update(state="held", hold=got.get("hold_id", ""), fence=got.get("fence", "1"))
            elif word == "committed":
                entry.update(state="booked", booking=got.get("booking_id") or entry.get("hold", ""), ref=got.get("ledger_ref", ""),
                             at=got.get("agenda_date", ""))
            elif word == "checked_in":
                entry["checkin"] = "done"
            elif word == "reported":
                entry["report"] = "done"
            elif word == "refused" and op in ("hold", "commit"):
                code = got.get("code") or next((w.upper() for w in kind if w.upper() in GONE + ("DUPLICATE",)), "REFUSED")
                if code != "DUPLICATE":  # un secondo commit rifiutato: la prenotazione vale
                    entry.update(state="refused", code=code)
            elif word == "refused" and op == "checkin":
                entry.update(checkin="refused", code=got.get("code", "REFUSED"))
            elif word == "refused" and op == "report":
                entry["report"] = "refused"
            elif word in ("cancelled", "expired", "cancel") and entry["state"] != "released":
                entry.update(state="cancelled", code=got.get("reason") or got.get("code") or word)
    return out


def waiting(chat, me: str) -> list:
    """Le scritture di questo caso ancora senza risposta del Ledger."""
    return [(e["slot"], e["state"], e.get("checkin"), e.get("report")) for e in bookings(chat, me).values()
            if e["state"] in ("holding", "committing") or "pending" in (e.get("checkin"), e.get("report"))]


def past(at: str | None) -> bool:
    """Il posto è già passato per l'orologio di agenda: un hold sarebbe rifiutato dopo minuti."""
    moment, now = core.when(at), Clock.now()
    return moment is not None and now is not None and moment <= now


def visit_time(entry: dict) -> str:
    """Giorno e ora della visita: dal ``committed`` del Ledger, altrimenti dalla riga del posto."""
    return entry.get("at") or World.slots.get(entry["slot"], {}).get("at", "")


# --- i passi dovuti ------------------------------------------------------------------------------


async def advance(role: str, city, message, chat) -> None:
    me, need = city.agent_id, message.need_id
    for entry in bookings(chat, me).values():
        slot, party, state = entry["slot"], entry.get("party", ""), entry["state"]
        citizen = entry.get("citizen", "")
        if state == "held" and entry.get("hold") and once(need, "commit", entry["hold"]):
            consent = entry.get("consent") or f"cons-{need}-booking"
            text = f"commit hold_id={entry['hold']} consent_ref={consent} fence={entry.get('fence', '1')}"
            await say(city, LEDGER, text + (f" prescription_number={entry['number']}" if entry.get("number") and role != "family_counseling" else ""))
        elif state == "booked" and entry.get("booking") and entry.get("ref"):
            World.drop(slot)
            await receipt(role, city, chat, entry)
        elif state in ("refused", "cancelled") and party and once(need, "lost", slot, str(entry["holds"])):
            code = str(entry.get("code", "facility_cancelled"))
            if code.upper() in GONE:
                World.drop(slot)
            offer = entry.get("offer") or f"of-{slot}"
            if core.is_citizen(party):  # consultorio: lo si dice alla persona e si propongono subito altre date
                lang = core.lang_of(chat)
                await say(city, party, core.compose(pick(lang, *LOST[0]), f"cancel offer_id={offer} reason={code}", pick(lang, *LOST[1])))
                await desk(role, city, message, await case(city, message), replacing=True, reopen=True)
            else:
                await say(city, party, core.compose("", f"cancel offer_id={offer} slot_id={slot} reason={code}",
                                                    "Ci dispiace, il registro non ha confermato questo posto: vi chiediamo di proporne un altro al cittadino."))
                if citizen and sent(chat, me, citizen, "booking.receipt"):  # gli avevamo già scritto: lo sappia da noi
                    lang = core.lang_of(chat)
                    await tell(city, citizen, lang, *CANCELLED)
                if party != TEAM["booking_centre"] and entry.get("request"):  # a un centro partner: altre date per la stessa richiesta
                    await propose(role, city, party, entry["request"], World.slots.get(slot, {}).get("procedure") or PROCEDURES[role][0],
                                  note="Per la stessa richiesta abbiamo ancora queste disponibilità. ")
        if entry.get("checkin") == "done" and citizen and once(need, "arrived", entry.get("booking", slot)):
            await arrived(role, city, chat, entry)
        elif entry.get("checkin") == "refused" and citizen and once(need, "refused", entry.get("booking", slot), str(entry.get("tries", 0))):
            window = core.half_day(visit_time(entry))
            await tell(city, citizen, core.lang_of(chat), *fill(TOO_EARLY, window=(window or "08:00-13:00 o 13:00-18:00", window or "08:00-13:00 or 13:00-18:00")),
                       f"booking_id={entry['booking']} reason={entry.get('code', 'refused')}")
        if role == "specialist_care" and entry.get("checkin") == "done" and "report" not in entry and once(need, "report", entry.get("booking", slot)):
            shared = next((core.consent_ref(row.text) for row in chat if row.sender == citizen and "report_sharing" in row.text), "")
            await say(city, LEDGER, f"report booking_id={entry['booking']} outcome_code=VISIT" + (f" consent_ref={shared}" if shared else ""))
        if entry.get("report") == "done" and citizen and once(need, "reported", entry.get("booking", slot)):
            lang = core.lang_of(chat)
            await tell(city, citizen, lang, *REPORTED, f"report booking_id={entry['booking']} outcome_code=VISIT")


async def receipt(role: str, city, chat, entry: dict) -> None:
    """Dopo il ``committed``: ricevuta a chi ha chiesto la prenotazione e benvenuto al cittadino
    (che è esso stesso una ricevuta: quando, dove, il codice, come registrare l'arrivo)."""
    me, need, party, citizen = city.agent_id, city.inbound.need_id, entry.get("party", ""), entry.get("citizen", "")
    booking, ref = entry["booking"], entry["ref"]
    if core.is_agent(party) and once(need, "receipt", booking, entry.get("confirm_id", "")):
        text = f"booking.receipt booking_id={booking} ledger_ref={ref} offer_id={entry.get('offer', '')} slot_id={entry['slot']}"
        await say(city, party, core.compose("", text + (f" agenda_date={entry['at']}" if entry.get("at") else ""),
                                            "Prenotazione registrata sul Ledger: grazie della collaborazione."))
    if citizen and once(need, "welcome", booking) and not sent(chat, me, citizen, "booking.receipt", f"booking_id={booking}"):
        lang = (entry.get("lang") if entry.get("lang") in ("it", "en") else "") or core.lang_of(chat)
        at, street = visit_time(entry), (city.address or "").replace("=", " ").strip()
        values = dict(place=PLACE[role], visit=VISIT[role], code=booking,
                      when=((" per " + core.spoken(at, "it")) if at else "", (" for " + core.spoken(at, "en")) if at else ""),
                      where=((" Ci trova in " + street + ".") if street else "", (" You will find us at " + street + ".") if street else ""),
                      window=(core.half_day(at) or "08:00-13:00 o 13:00-18:00", core.half_day(at) or "08:00-13:00 or 13:00-18:00"))
        await tell(city, citizen, lang, *fill(WELCOME, **values), f"booking.receipt booking_id={booking} ledger_ref={ref}")


async def arrived(role: str, city, chat, entry: dict) -> None:
    lang, at = core.lang_of(chat), visit_time(entry)
    it, en = fill(ARRIVED_REPORT if role == "specialist_care" else ARRIVED, visit=VISIT[role],
                  when=((" di " + core.spoken(at, "it")) if at else "", (" on " + core.spoken(at, "en")) if at else ""))
    if core.worried(chat):
        it = (it[0] + " Capisco la sua preoccupazione: ora è in buone mani.", it[1])
        en = (en[0] + " I understand your concern: you are in good hands now.", en[1])
    await tell(city, entry["citizen"], lang, it, en, f"checkin booking_id={entry['booking']}")


# --- un agente scrive ----------------------------------------------------------------------------


async def on_agent(role: str, city, message, chat) -> None:
    got, kind = fields(message.text), set(words(message.text))
    if "confirm" in kind:
        await on_confirm(role, city, message, chat, got)
    elif "need.request" in kind:
        await on_search(role, city, message, chat, got)
    elif "cancel" in kind:
        await release(city, message, chat, got)


async def may_book(city, chat, party: str) -> bool:
    """Chi può chiederci una prenotazione: il nostro centro, un centro a cui abbiamo risposto in
    questo caso, o un centro prenotazioni che l'Index mostra. Gli altri no (impostori)."""
    if party == TEAM["booking_centre"] or sent(chat, city.agent_id, party, "offer"):
        return True
    if time.monotonic() - BOOKERS["at"] > 600:
        try:
            BOOKERS["ids"] = {peer.agent_id for peer in await city.find("booking centre") if peer.role in ("", "booking_centre")}
            BOOKERS["at"] = time.monotonic()
        except Exception:  # noqa: BLE001  (Index giù: non si blocca una prenotazione vera)
            return True
    return party in BOOKERS["ids"]


async def on_confirm(role: str, city, message, chat, got: dict) -> None:
    """``confirm`` di un centro prenotazioni: un solo ``hold`` per posto, poi fa tutto ``advance``."""
    me, need, party = city.agent_id, message.need_id, message.sender
    slot, offer = got.get("slot_id", ""), got.get("offer_id", "")
    citizen = got.get("citizen", "")
    missing = "slot_id" if not slot else "consent_ref" if not got.get("consent_ref") else "citizen" if not core.is_citizen(citizen) else ""
    known = World.slots.get(slot)
    expired = not missing and past(got.get("agenda_date") or (known or {}).get("at"))
    if missing or expired or (known and known["facility"] != me) or not await may_book(city, chat, party):
        reason = f"missing_{missing}" if missing else "EXPIRED" if expired else "not_our_slot" if known and known["facility"] != me else "unknown_sender"
        if once(need, "bad", message.msg_id):
            await say(city, party, core.compose("", f"cancel offer_id={offer} reason={reason}",
                                                "Ci dispiace, non possiamo registrare questa richiesta: ricontrollate i dati e rimandatela."))
        return
    entry = bookings(chat, me).get(slot, {})
    if entry.get("state") == "booked":  # confirm doppio: la stessa ricevuta, mai un secondo hold
        return
    if entry.get("state") in ("holding", "held", "committing") or not once(need, "hold", slot, str(entry.get("holds", 0))):
        return
    consent = got.get("cross_asst_consent_ref") or got["consent_ref"]
    text = f"hold slot_id={slot} citizen={citizen}"
    text += f" prescription_number={got['prescription_number']}" if got.get("prescription_number") and role != "family_counseling" else ""
    if not await say(city, LEDGER, f"{text} consent_ref={consent}"):
        core.undo(need, "hold", slot, str(entry.get("holds", 0)))
        return
    # Se il Ledger è svelto si chiude qui; altrimenti il suo ``held`` apre un turno e si riparte da lì.
    await core.settle(city, message, lambda rows: bookings(rows, me).get(slot, {}).get("state") in ("held", "refused"), 2.5)


async def release(city, message, chat, got: dict) -> None:
    """Chi aveva prenotato chiede di liberare il posto: lo si libera sul Ledger e lo si conferma
    senza la parola ``cancel`` (verrebbe letta come un nostro annullo)."""
    me, need = city.agent_id, message.need_id
    slot = got.get("slot_id") or got.get("offer_id", "").removeprefix("of-")
    entry = bookings(chat, me).get(slot)
    if entry is None or entry.get("party") != message.sender or not entry.get("hold") or entry["state"] not in ("held", "committing", "booked"):
        return
    if once(need, "release", slot) and await say(city, LEDGER, f"cancel hold_id={entry['hold']}"):
        World.gone.pop(slot, None)
        await say(city, message.sender, core.compose("", f"released slot_id={slot}", "Posto liberato sul registro, come richiesto. Grazie della segnalazione."))


async def on_search(role: str, city, message, chat, got: dict) -> None:
    """Un centro di un'altra ASST cerca posti (solo criteri, nessun dato del cittadino): una risposta
    sola, subito, dai posti in memoria. Solo ciò che il Ledger ha scritto sul posto."""
    me, need, rid, procedure = city.agent_id, message.need_id, got["request_id"], got.get("procedure", "")
    if not once(need, "search", message.sender, rid) or sent(chat, me, message.sender, f"request_id={rid}"):
        return
    ours = procedure in PROCEDURES[role] and got.get("service_type", core.CAPABILITY[role]) == core.CAPABILITY[role]
    if ours:
        await Clock.ensure(city)
        if await World.refresh(city, procedure) and World.age(procedure) > 1e8 or Clock.now() is None:
            rows = await core.settle(city, message, lambda found: Clock.now() is not None and World.age(procedure) < 1e8, 4.0)
            for row in core.ledger_rows(rows, "agenda", "slots", "world"):
                learn(row.text)
    gender = (got.get("gender") or got.get("gender_preference") or "")[:1].lower()
    if not ours or not await propose(role, city, message.sender, rid, procedure, priority=got.get("priority", ""), lang=got.get("lang", "it"),
                                     gender=gender if gender in ("f", "m") else "", days={d[:3].lower() for d in core.items(got.get("days"))}):
        await say(city, message.sender, core.compose("", f"cancel reason=no_availability request_id={rid}",
                                                     "Ci dispiace, al momento non abbiamo disponibilità adatte a questa richiesta. Riscriveteci pure più avanti: ricontrolliamo subito l'agenda."))


async def propose(role: str, city, to: str, rid: str, procedure: str, *, priority: str = "", lang: str = "it", gender: str = "",
                  days: set = frozenset(), note: str = "Buongiorno, ecco le nostre prime disponibilità per la vostra richiesta. ") -> bool:
    """Fino a quattro nostri posti per la richiesta ``rid`` di un altro centro. False se non c'è nulla."""
    now, need = Clock.now(), city.inbound.need_id
    if now is None:
        return False
    want = dict(now=now, born=now, priority=priority, lang=lang, limit=4, need=need, gender=gender, days=set(days))
    pool = World.of(procedure, facility=city.agent_id)
    inside = core.choose(pool, **want)
    slots = inside or core.choose(pool, **want, relaxed=True)
    if not slots:
        return False
    for slot in slots:
        World.offered[slot["slot_id"]] = (time.monotonic(), need)
    lines = "\n".join(core.offer_line(slot, priority=priority, request_id=rid) for slot in slots)
    late = "" if inside else "Entro i tempi della priorità non abbiamo posti liberi: queste sono le prime date subito dopo. "
    await say(city, to, core.compose("", lines + "\n", note + late + "Per prenotarne una mandateci la scelta con identificativo dell'offerta e del posto, "
                                     "cittadino, numero di ricetta e riferimento del consenso alla prenotazione (più quello cross-ASST se la regola lo chiede): "
                                     "la registriamo sul Ledger e vi mandiamo subito la ricevuta."))
    return True


# --- il cittadino scrive --------------------------------------------------------------------------


async def on_citizen(role: str, need, city, message, chat) -> None:
    me, text, citizen = city.agent_id, message.text, message.sender
    if core.emergency(need, message):
        await core.escalate(city, message, chat)
        return
    got, kind, lang = fields(text), set(words(text)), core.lang_of(chat, text)
    if "data.request" in kind and "address" in core.items(got.get("fields")) and city.address:
        await say(city, citizen, f"address={city.address}")
        return
    booked = [e for e in bookings(chat, me).values() if e.get("citizen") == citizen and e["state"] == "booked"]
    code = got.get("booking_id") or next(iter(_CODE.findall(text)), "")
    asked_code = sent(chat, me, citizen, "data.request", "fields=booking_id")
    if "checkin" in kind or got.get("booking_id") or _ARRIVED.search(core.plain(text)) or (asked_code and code and not kind & {"confirm", "cancel"}):
        entry = next((e for e in booked if e.get("booking") == code), None) or (booked[-1] if booked and (not code or len(booked) == 1) else None)
        await arrive(role, city, message, chat, entry, code, lang)
        return
    if core.injection(text):
        await tell(city, citizen, lang, *HOSTILE)
        return
    if role == "family_counseling" and not booked:
        await desk(role, city, message, chat)
        return
    await answer(role, city, message, chat, booked, lang)


async def arrive(role: str, city, message, chat, entry: dict | None, code: str, lang: str) -> None:
    """Arrivo: il check-in non lo blocchiamo mai noi, decide il Ledger. La parola ``checkin`` al
    cittadino parte solo dopo ``checked_in``; intanto gli si dice che lo stiamo registrando."""
    need, citizen = message.need_id, message.sender
    if entry is None and code:  # un codice che questo caso non conosce: decide il Ledger, non noi
        entry = bookings(chat, city.agent_id).get(code) or {"booking": code, "slot": code, "citizen": citizen}
    if entry is None:
        await tell(city, citizen, lang, *NO_CODE, "data.request purpose=booking fields=booking_id")
        return
    booking = entry["booking"]
    if entry.get("checkin") == "done":
        await tell(city, citizen, lang, *ALREADY, f"booking_id={booking}")
        return
    if entry.get("checkin") == "refused" and not once(need, "retry", booking, message.msg_id):
        return
    if entry.get("checkin") != "pending" and once(need, "checkin", booking, str(entry.get("tries", 0))):
        if not await say(city, LEDGER, f"checkin booking_id={booking} citizen={citizen}"):
            core.undo(need, "checkin", booking, str(entry.get("tries", 0)))
    # Nessuna risposta d'attesa: chiuderebbe il percorso del cittadino prima che il Ledger scriva il
    # check-in (in città, 21/11 08:36: verdetto 64 s dopo l'attesa, checked_in 43 s dopo il verdetto,
    # receipt_refused -2). La conferma parte con il checked_in.
    # Se il Ledger è svelto la conferma parte in questo turno (``advance``); altrimenti dal suo ``checked_in``.
    await core.settle(city, message, lambda rows: any(e.get("booking") == booking and e.get("checkin") in ("done", "refused")
                                                      for e in bookings(rows, city.agent_id).values()), 2.0)


async def answer(role: str, city, message, chat, booked: list, lang: str) -> None:
    """Qualsiasi altra cosa: i fatti della sua prenotazione da noi, oppure a chi rivolgersi."""
    citizen = message.sender
    if booked:
        entry, at = booked[-1], visit_time(booked[-1])
        street = (city.address or "").replace("=", " ").strip()
        values = dict(code=entry["booking"], visit=VISIT[role],
                      when=((" è " + core.spoken(at, "it")) if at else " è confermata", (" is on " + core.spoken(at, "en")) if at else " is confirmed"),
                      where=((" Ci trova in " + street + ".") if street else "", (" You will find us at " + street + ".") if street else ""),
                      window=(core.half_day(at) or "08:00-13:00 o 13:00-18:00", core.half_day(at) or "08:00-13:00 or 13:00-18:00"))
        await tell(city, citizen, lang, *fill(FACTS, **values), f"booking_id={entry['booking']}")
    elif once(message.need_id, "pointer", message.msg_id):
        await tell(city, citizen, lang, *fill(POINTER, place=PLACE[role]))


# --- il consultorio prenota da sé, senza ricetta ---------------------------------------------------


async def desk(role: str, city, message, chat, *, replacing: bool = False, reopen: bool = False) -> None:
    """Accesso diretto: offerte dei nostri posti in UN messaggio, scelta, consenso, ``hold``.
    Mai una ricetta chiesta o nominata come requisito."""
    me, need = city.agent_id, message.need_id
    citizen = core.citizen_of(chat)
    if not citizen:
        return
    text = message.text if core.is_citizen(message.sender) else ""
    asking = bool(text)
    lang = core.lang_of(chat, text)
    got, kind = fields(text), set(words(text))
    mine = {s: e for s, e in bookings(chat, me).items() if e.get("party") == citizen}
    flying = [e for e in mine.values() if e["state"] in ("asked", "holding", "held", "committing")]
    if flying and not reopen:
        if asking and once(need, "busy", message.msg_id):
            await tell(city, citizen, lang, *BUSY)
        return
    live = core.live_offers(chat, me, citizen)
    ours = core.our_offers(chat, me, citizen)
    if "cancel" in kind and "confirm" not in kind and got.get("offer_id"):
        if live:  # i rifiuti dello stesso messaggio arrivano insieme: un attimo
            chat = await core.settle(city, message, lambda rows: not core.live_offers(rows, me, citizen), 2.4)
            live = core.live_offers(chat, me, citizen)
        if live:
            return
        replacing = True
    elif asking and not replacing:
        heard = core.hear(text, live)
        if got and "confirm" not in kind or len(core.prose(text).split()) > 12:
            heard["pick"] = None
        choice = heard["pick"] or (ours.get(got.get("offer_id", "")) if "confirm" in kind else None)
        pending = next((ours.get(fields(r.text).get("offer_id", "")) for r in reversed(chat)
                        if r.sender == me and r.recipient == citizen and core.act(r.text) == "consent"), None) \
            if sent(chat, me, citizen, "consent") and not live_after_consent(chat, me, citizen) else None
        consent = core.consent_ref(text)
        if choice is None and pending and (consent or heard["yes"] or ("data.response" in kind and not heard["no"])):
            choice, consent = pending, consent or f"cons-{need}-booking"
        if choice is not None and choice.get("slot_id"):
            if past(choice.get("agenda_date")):  # un'offerta vecchia: quel posto è già passato, altre date subito
                if once(need, "stale", choice["offer_id"]):
                    await say(city, citizen, core.compose(pick(lang, *LOST[0]), f"cancel offer_id={choice['offer_id']} reason=slot_expired", pick(lang, *LOST[1])))
                await desk(role, city, message, await case(city, message), replacing=True, reopen=True)
                return
            if not consent:
                await tell(city, citizen, lang, *CONSENT, f"consent purpose=booking offer_id={choice['offer_id']}")
                return
            if once(need, "hold", choice["slot_id"], str(mine.get(choice["slot_id"], {}).get("holds", 0))):
                # Il posto viene dalla NOSTRA offerta; la scelta si registra come un confirm del cittadino.
                held = await say(city, LEDGER, f"hold slot_id={choice['slot_id']} citizen={citizen} consent_ref={consent}")
                if held and once(need, "busy", message.msg_id):
                    World.drop(choice["slot_id"])
                    await tell(city, citizen, lang, *BUSY)
            return
        replacing = replacing or bool(live and (heard["no"] or heard["days"] or heard["not_days"] or heard["gender"]))
    procedure = PROCEDURES[role][0]
    await Clock.ensure(city)
    asked = await World.refresh(city, procedure, deep=replacing)
    now = Clock.now()
    if now is None or (asked and World.age(procedure) > 600):
        rows = await core.settle(city, message, lambda found: Clock.now() is not None and World.age(procedure) < 600, 6.0)
        for row in core.ledger_rows(rows, "agenda", "slots", "world"):
            learn(row.text)
        now = Clock.now()
    if now is None or World.age(procedure) > 1e8:
        WAITING[need] = (city, message)
        if asking and once(need, "searching", message.msg_id):
            await tell(city, citizen, lang, *SEARCHING)
        return
    WAITING.pop(need, None)
    no = core.refusals(chat, me, citizen)
    days, gender, doctor_lang = set(), "", "it"
    for row in chat:
        if row.sender == citizen:
            heard, said = core.hear(row.text, []), fields(row.text)
            days |= heard["days"] | {d[:3].lower() for d in core.items(said.get("days")) if d[:3].lower() in core.DAY_IT}
            token = (said.get("gender") or said.get("gender_preference") or "")[:1].lower()
            gender = (token if token in ("f", "m") else "") or heard["gender"] or gender
            doctor_lang = said.get("lang", doctor_lang)
    offered = {o["slot_id"] for o in ours.values() if o.get("slot_id")}
    lost = {s for s, e in mine.items() if e["state"] in ("refused", "cancelled")}
    pool = [s for s in World.of(procedure, facility=me) if (no["after"] is None or core.when(s["at"]) > no["after"])
            and (no["before"] is None or core.when(s["at"]) < no["before"])]
    slots = core.choose(pool, now=now, born=now, priority="", lang=doctor_lang, no_gender=no["no_gender"], no_days=no["no_days"],
                        no_doctors=no["no_doctors"], days=days, gender=gender, need=need,
                        skip=no["skip"] | lost | (set() if replacing or (asking and live) else offered))
    if not slots:
        if once(need, "none", message.msg_id if asking else ""):
            await say(city, citizen, core.compose(pick(lang, *NONE[0]), "cancel reason=no_slots", pick(lang, *NONE[1])))
        return
    kind_of = "again" if no["skip"] or lost else "first"
    opener, closing = OFFERS[kind_of]
    if core.worried(chat):
        opener = ("Capisco la sua preoccupazione e me ne occupo subito. " + opener[0], "I understand your concern and I am taking care of it right away. " + opener[1])
    body = core.offers_text(lang, slots, opener=opener, closing=closing)
    if once(need, "offers", message.msg_id if asking else "", body):
        for slot in slots:
            World.offered[slot["slot_id"]] = (time.monotonic(), need)
        await say(city, citizen, body)


def live_after_consent(chat, me: str, citizen: str) -> bool:
    """Dopo la nostra richiesta di consenso abbiamo già scritto altro al cittadino?"""
    last = max((i for i, r in enumerate(chat) if r.sender == me and r.recipient == citizen and core.act(r.text) == "consent"), default=-1)
    return any(r.sender == me and r.recipient == citizen for r in chat[last + 1:])


# --- le parole ------------------------------------------------------------------------------------


def fill(phrase, **values) -> tuple[tuple[str, str], tuple[str, str]]:
    """Riempie una frase nelle due lingue: un valore è una stringa o una coppia (italiano, inglese)."""
    out = []
    for index, pair in enumerate(phrase):
        chosen = {k: (v[index] if isinstance(v, tuple) else v) for k, v in values.items()}
        out.append(tuple(part.format(**chosen) for part in pair))
    return out[0], out[1]


WELCOME = (("Buongiorno, sono {place} dove si svolgerà la sua {visit}: la sua prenotazione è confermata e registrata{when}.",
            "Il suo codice di prenotazione è {code}.{where} Il giorno della visita si presenti nella fascia {window} e mi scriva qui che è arrivato: "
            "registro io il suo arrivo, non deve fare altro. Porti con sé la tessera sanitaria e un documento e arrivi qualche minuto prima. "
            "Per qualsiasi domanda mi scriva pure, sono a sua disposizione."),
           ("Hello, this is {place} where your {visit} will take place: your booking is confirmed and registered{when}.",
            "Your booking code is {code}.{where} On the day, come within {window} and write to me here that you have arrived: "
            "I will register your arrival, there is nothing else to do. Bring your health card and an ID and arrive a few minutes early. "
            "For any question just write to me, I am here to help."))
ARRIVED = (("Benvenuto, grazie di essere qui: il suo arrivo per la {visit}{when} è registrato.",
            "Non deve fare altro: si accomodi in sala d'attesa, la chiameremo noi. Tenga a portata di mano la tessera sanitaria. Le auguro una buona visita; per qualsiasi cosa mi scriva qui."),
           ("Welcome, thank you for being here: your arrival for the {visit}{when} is registered.",
            "There is nothing else to do: please take a seat in the waiting room, we will call you. Keep your health card at hand. I wish you a good visit; for anything else write to me here."))
ARRIVED_REPORT = (("Benvenuto, grazie di essere qui: il suo arrivo per la {visit}{when} è registrato.",
                   "Non deve fare altro: si accomodi, lo specialista la riceverà a breve. Subito dopo la visita archivio io il referto e le scrivo qui, così sa sempre a che punto è."),
                  ("Welcome, thank you for being here: your arrival for the {visit}{when} is registered.",
                   "There is nothing else to do: please take a seat, the specialist will see you shortly. Right after the visit I will file the summary and write to you here, so you always know where things stand."))
ALREADY = (("Il suo arrivo è già registrato, è tutto a posto.", "Non deve fare altro: si accomodi pure, le auguro una buona visita."),
           ("Your arrival is already registered, everything is in order.", "There is nothing else to do: please take a seat, I wish you a good visit."))
TOO_EARLY = (("Mi dispiace, in questo momento il registro non mi lascia registrare il suo arrivo.",
              "L'arrivo si registra nella mezza giornata della visita ({window}). Mi riscriva quando è in quella fascia e lo registro subito, il codice ce l'ho già; "
              "se non riesce a venire, il centro prenotazioni può spostare la visita."),
             ("I'm sorry, right now the register does not let me record your arrival.",
              "Arrival is recorded in the half day of the visit ({window}). Write to me again in that window and I will record it at once, I already have your code; "
              "if you cannot come, the booking centre can move the visit."))
NO_CODE = (("Benvenuto! Registro volentieri il suo arrivo.",
            "Mi serve solo il codice di prenotazione che trova sulla ricevuta: me lo scrive? Al resto penso io."),
           ("Welcome! I will gladly register your arrival.",
            "I only need the booking code you find on your confirmation: could you write it to me? I will take care of the rest."))
REPORTED = (("Grazie per essere venuto: il referto della sua visita è archiviato e resta riservato.",
             "Se lo specialista le ha indicato un controllo, il suo medico di base può prepararle la ricetta e il centro prenotazioni fissarlo; per qualsiasi domanda mi scriva qui. Le auguro una buona giornata."),
            ("Thank you for coming: the summary of your visit is filed and stays private.",
             "If the specialist recommended a follow-up, your GP can prepare the prescription and the booking centre can book it; for any question write to me here. Have a good day."))
CANCELLED = (("Mi dispiace: il registro ha annullato la prenotazione che le avevo confermato.",
              "Non deve fare nulla: il centro prenotazioni la contatterà con una nuova data. Se preferisce, gli scriva pure lei indicando giorni e orari comodi."),
             ("I'm sorry: the register has cancelled the booking I had confirmed to you.",
              "You do not need to do anything: the booking centre will contact you with a new date. If you prefer, write to them with the days and times that suit you."))
LOST = (("Mi dispiace: il registro non ha confermato questo appuntamento.", "I'm sorry: the register did not confirm this appointment."),
        ("Non deve fare nulla: le mando subito altre date.", "You do not need to do anything: I am sending you other dates right away."))
FACTS = (("Grazie del messaggio: la sua {visit} da noi{when}, codice di prenotazione {code}.",
          "{where} Il giorno della visita si presenti nella fascia {window} e mi scriva qui che è arrivato: registro io il suo arrivo. "
          "Per spostare o annullare la visita può scrivere al centro prenotazioni; per i sintomi al suo medico di base."),
         ("Thank you for your message: your {visit} with us{when}, booking code {code}.",
          "{where} On the day, come within {window} and write to me here that you have arrived: I will register your arrival. "
          "To move or drop the visit you can write to the booking centre; for symptoms, to your GP."))
POINTER = (("Buongiorno, sono {place}: la aiuto volentieri.",
            "Se ha già un appuntamento da noi, mi scriva il codice di prenotazione e registro io il suo arrivo. Per prenotare o spostare una visita può scrivere al centro prenotazioni; "
            "per sintomi e ricette al suo medico di base."),
           ("Hello, this is {place}: I am glad to help.",
            "If you already have an appointment with us, write me your booking code and I will register your arrival. To book or move a visit you can write to the booking centre; "
            "for symptoms and prescriptions, to your GP."))
HOSTILE = (("Capisco la richiesta, ma posso lavorare solo sulla sua prenotazione e non tratto dati di altre persone.",
            "Mi scriva il suo codice di prenotazione e la aiuto subito con l'arrivo o con le informazioni sulla visita."),
           ("I understand the request, but I can only work on your own booking and I do not handle other people's data.",
            "Write me your booking code and I will help you at once with your arrival or with information about the visit."))
BUSY = (("Perfetto, grazie: sto registrando la sua scelta sul registro.", "Le mando la conferma con giorno, ora e indirizzo appena risponde: non deve fare altro."),
        ("Perfect, thank you: I am recording your choice in the register.", "I will send you the confirmation with day, time and address as soon as it answers: nothing else to do."))
CONSENT = (("Ottima scelta, la fisso subito.", "Per completare mi serve solo il suo consenso alla prenotazione: mi conferma che acconsente?"),
           ("Great choice, I will book it right away.", "To complete it I only need your agreement to the booking: do you agree?"))
SEARCHING = (("Buongiorno, sono il consultorio: può prenotare la visita direttamente qui. Sto leggendo l'agenda aggiornata.",
              "Le scrivo tra pochi istanti con le date disponibili, non deve fare altro."),
             ("Hello, this is the family counselling centre: you can book your visit directly here. I am reading the updated agenda.",
              "I will write to you in a few moments with the available dates: nothing else to do."))
NONE = (("Mi dispiace, al momento non ci sono appuntamenti liberi adatti a lei.", "I'm sorry, there are no suitable free appointments right now."),
        ("Mi dica pure giorni o orari in cui può venire e ricontrollo subito l'agenda.", "Tell me the days or times you can come and I will check the agenda again at once."))
_PICK = ("Mi indichi il numero dell'opzione che preferisce e la fisso subito io. Se nessuna va bene, mi dica giorni, orari o medico che preferisce e cerco di conseguenza. "
         "Le manderò poi indirizzo e istruzioni per il giorno della visita.",
         "Tell me the number of the option you prefer and I will book it at once. If none suits you, tell me the days, times or doctor you prefer and I will search again. "
         "I will then send you the address and instructions for the day of the visit.")
OFFERS = {"first": (("Buongiorno, sono il consultorio: può prenotare la sua visita ginecologica direttamente qui, e ho già cercato le prime date per lei.",
                     "Hello, this is the family counselling centre: you can book your gynaecology visit directly here, and I have already looked for the first dates for you."), _PICK),
          "again": (("Capisco, grazie per avermelo detto: ho tolto quelle date e ho cercato tenendo conto di ciò che mi ha indicato.",
                     "I understand, thank you for telling me: I have removed those dates and searched again with what you told me."), _PICK)}
