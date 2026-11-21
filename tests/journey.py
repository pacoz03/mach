"""Percorsi multi-agente in locale: i 5 brain veri in un processo, bus in memoria, Ledger finto.

    ~/.local/share/uv/tools/city-cli/bin/python -B tests/journey.py [-k nome] [-v] [--ledger-turns] [--ledger-format sdk]

Ogni scenario ricarica i brain, fa parlare un cittadino scriptato e controlla le scritture sul
Ledger e i messaggi contro la rubrica. R:<riga> = mach-hackathon-kit/docs/kit/e19-rules.md.
Memoria ed Exa restano spente: CITY_ENV e TEAM_MEMORY vengono tolti dall'ambiente.
"""

import sys

sys.dont_write_bytecode = True

import argparse  # noqa: E402
import asyncio  # noqa: E402
import importlib.util  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
from collections import Counter, deque  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402

from city_sdk.city import LEDGER, CapError, City, Message, Need, OutOfScope, Peer, understood  # noqa: E402

# --- IPOTESI: sostituire con risposte reali ---------------------------------------------------
# I testi del Ledger reale non sono documentati. Queste forme vengono da come li leggono i brain
# (templates/examples/agent.py: _ledger, _latest_slots, _facility; righe ~308, 467, 503, 901-938)
# e dal Ledger locale dell'SDK (city_sdk/ports.py: held/committed, con booking_id = hold_id).
# Lo Slot del Ledger (ledger.py:107-121) ha agenda_date/agenda_day/capacity, ma i brain leggono
# at=/day=/free=: quando la città riparte, copiare qui una risposta vera per ogni operazione.
REPLY = {
    "prescribed": "prescribed prescription_number={prescription_number} patient={patient} "
    "procedure={procedure} priority={priority} state=issued",
    "prescription": "prescription prescription_number={prescription_number} patient={patient} "
    "procedure={procedure} priority={priority} state={state}",
    # Campi di una risposta vera (episodes, 2026-11-20): il brain ne ricava l'orologio da sé.
    "agenda": "agenda rate=60.0 paused=false agenda_day={day} agenda_now={now} epoch_real={real} "
    "epoch_agenda={now}",
    "rules": "rules version=1 P1={P1} P2={P2} P3={P3}",
    "slots": "slots page={page}/{pages} count={count}",  # intestazione, poi una riga per slot
    "slot": "slot_id={slot_id} doctor_id={doctor_id} facility={facility} procedure={procedure} "
    "at={at} day={day} free={free}",  # + " urgent_reserve=true" con P3 attiva
    "held": "held hold_id={hold_id} slot_id={slot_id} need_id={need_id} fence=1",
    "committed": "committed booking_id={hold_id} ledger_ref={ledger_ref} slot_id={slot_id} "
    "receipt=sig-{ledger_ref}",
    "cancelled": "cancelled hold_id={hold_id} slot_id={slot_id}",
    # Nessun `expired` nel kit (LocalLedger non lo ha) né in episodes al 21/11: forma ipotetica,
    # la stessa di cancelled.
    "expired": "expired hold_id={hold_id} slot_id={slot_id}",
    "checked_in": "checked_in booking_id={booking_id} citizen={citizen} slot_id={slot_id}",
    "reported": "reported booking_id={booking_id} outcome_code={outcome_code} shared={shared}",
    "refused": "refused {op} code={code}",  # + i token identificativi della richiesta
}
# --ledger-format sdk: `world slots=N` (city_sdk/ports.py:107) e lo Slot dello schema
# (ledger.py:107-121, capacity>=1: gli slot presi non compaiono).
SDK_REPLY = {
    "slots": "world slots={count} page={page}/{pages}",
    "slot": "slot_id={slot_id} doctor_id={doctor_id} facility={facility} procedure={procedure} "
    "agenda_date={at} agenda_day={day} capacity={free}",
}
DOCTOR_ID = "D-{key}"  # i brain usano l'ultimo pezzo dopo '-' come chiave di _DOCTORS
# Medici e giorni copiati da _DOCTORS dei brain, procedure dal PROTOCOL del gp: anche questi
# vanno confrontati con il `world` vero.
STAFF = {
    "hospital": ("VIS-FIS", {"h1": "mon tue wed", "h2": "wed thu fri", "h3": "tue thu"}),
    "specialist_care": ("VIS-CARD", {"s1": "mon wed fri", "s2": "tue thu"}),
    "family_counseling": ("CONS-GIN", {"c1": "mon tue wed thu fri"}),
}
PAGE = 10
# Codice ipotetico del rifiuto di checkin: la finestra è la mezza giornata dello slot (R:91).
CHECKIN_REFUSED = "OUTSIDE_WINDOW"
# ----------------------------------------------------------------------------------------------

REPO = Path(__file__).resolve().parent.parent
FOLDERS = ("gp", "booking_centre", "hospital", "specialist_care", "family_counseling")
FACILITIES = {"hospital", "specialist_care", "family_counseling"}
AGENT_DIRS = {str(REPO / f / "agent") for f in FOLDERS}
START = datetime(2026, 11, 23, 8, tzinfo=UTC)  # lunedì 08:00 di agenda
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
CIT, NEED = "citizen:T-1", "N-2026-11-23-0001"
# Centro prenotazioni di un'altra ASST: fuori dal bus, scriptato in Bus.foreign (R:191-195).
FOREIGN = "agent:asst-altra-booking"
MAX_TURNS = 200


def fields(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for tok in text.replace(",", " ").split():
        k, eq, v = tok.partition("=")
        if eq and k and v:
            out.setdefault(k, v.strip(".,;:!?"))
    return out


def parse_offers(text: str) -> list[dict[str, str]]:
    """Ogni `offer_id=` apre un'offerta nuova: un messaggio può portarne diverse (R:95)."""
    out: list[dict[str, str]] = []
    for tok in text.replace(",", " ").split():
        k, eq, v = tok.partition("=")
        if eq and k == "offer_id":
            out.append({})
        if eq and out:
            out[-1].setdefault(k, v.strip(".,;:!?"))
    return out


# Le parole d'atto con cui i nostri agenti aprono un messaggio. understood() legge l'ULTIMA parola
# d'atto del testo: "data.request ... I have checked your prescription" diventava "prescription"
# e il cittadino finto taceva. I cittadini veri rispondono (percorso 000100): conta la prima.
ACTS = {"prescription", "offer", "cancel", "data.request", "data.response", "consent",
        "booking.receipt", "checkin", "escalate.emergency", "report", "need.request"}


def act(m: Message) -> str:
    first = (m.text or "").split()[:1]
    return first[0] if first and first[0] in ACTS else understood(m).type


def has(text: str, tokens: tuple[str, ...]) -> bool:
    ws = text.split()
    return all(any(w == t or (t.endswith("=") and w.startswith(t)) for w in ws) for t in tokens)


def make_slots(ids: dict[str, str]) -> list[dict]:
    out = []
    for folder, (proc, docs) in STAFF.items():
        n = 0
        for d in range(14):
            day = START + timedelta(days=d)
            name = DAYS[day.weekday()]
            for hour in (10, 15):
                for key, days in docs.items():
                    if name in days:
                        out.append(dict(
                            slot_id=f"S-{key}-{d:02d}-{hour}", doctor_id=DOCTOR_ID.format(key=key),
                            facility=ids[folder], procedure=proc,
                            at=day.replace(hour=hour).isoformat(), day=name, reserve=n % 5 == 1,
                        ))
                        n += 1
    return out


class Ledger:
    WRITES = {"prescribe", "hold", "commit", "cancel", "checkin", "report"}
    GRANTS = {"prescribe": {"gp", "specialist_care"}, "commit": FACILITIES, "cancel": FACILITIES,
              "checkin": FACILITIES, "report": {"specialist_care"}}

    def __init__(self, sc: dict, ids: dict[str, str]) -> None:
        self.now = START + timedelta(days=sc.get("late_days", 0))  # late_days: slot tutti passati
        self.rules = {"P1": "off", "P2": "off", "P3": "off", **sc.get("rules", {})}
        self.folder = {v: k for k, v in ids.items()}
        self.slots = {s["slot_id"]: s for s in make_slots(ids)}
        self.rx: dict[str, dict] = {}
        self.holds: dict[str, dict] = {}
        self.checkins: set[str] = set()
        self.consents: set[tuple[str, str]] = set()  # (purpose, consent_ref) dati dal cittadino
        self.writes: list[dict] = []
        self.refuse_holds = sc.get("refuse_holds", 0)
        self.refuse_code = sc.get("refuse_code", "SLOT_TAKEN")  # codice dell'hold rifiutato
        self.refuse_checkin = sc.get("refuse_checkin")
        self.full = self.now + timedelta(days=sc.get("full_days", 0))  # slot prima di qui: pieni
        self.slow_agenda, self.agendas = sc.get("slow_agenda", 0), 0  # secondi, dalla 2a agenda
        self.slow_world = sc.get("slow_world", 0)  # secondi, ogni world (città 21/11: ~12 s)
        self.slow_writes = sc.get("slow_writes", 0)  # secondi, risposta a hold e commit (21/11: ~30 s)
        self.push_expired = sc.get("push_expired", 0)  # commit che arrivano a hold già scaduto
        self.n = 0

    def advance(self, days: int) -> None:
        self.now += timedelta(days=days)

    def expired(self, number: str | None) -> bool:
        r = self.rx.get(number or "")
        return bool(r) and self.rules["P1"] == "on" and self.now - r["issued"] > timedelta(days=2)

    def late(self, text: str) -> float:
        if (text.split() or [""])[0] in ("hold", "commit"):
            return self.slow_writes
        if (text.split() or [""])[0] == "world":
            return self.slow_world
        if (text.split() or [""])[0] != "agenda":
            return 0
        self.agendas += 1
        return self.slow_agenda if self.agendas > 1 else 0

    def taken(self, slot_id: str) -> bool:
        if datetime.fromisoformat(self.slots[slot_id]["at"]) < self.full:
            return True
        return any(h["slot_id"] == slot_id and h["state"] not in ("cancelled", "expired") for h in self.holds.values())

    def answer(self, by: str, need: str | None, text: str, i: int) -> str:
        op = (text.split() or [""])[0]
        f = fields(text)
        if op not in self.WRITES:
            return self.read(op, f)
        code, reply, injected = self.write(op, by, need, f)
        self.writes.append(dict(i=i, by=self.folder.get(by, by), op=op, f=f, code=code, injected=injected))
        if code is None or reply:  # reply con un codice: il Ledger risponde altro (push_expired)
            return reply
        ids = [f"{k}={f[k]}" for k in ("prescription_number", "slot_id", "hold_id", "booking_id") if k in f]
        return " ".join([REPLY["refused"].format(op=op, code=code), *ids])

    def write(self, op: str, by: str, need: str | None, f: dict) -> tuple[str | None, str, bool]:
        if self.folder.get(by) not in self.GRANTS.get(op, set(FOLDERS)):
            return "GRANT_PERMISSION_DENIED", "", False
        self.n += 1
        if op == "prescribe":
            f.setdefault("prescription_number", f"RX-{self.n}")
            if not all(f.get(k) for k in ("patient", "procedure", "priority")):
                return "MISSING_FIELD", "", False
            if f["prescription_number"] in self.rx:
                return "DUPLICATE", "", False
            self.rx[f["prescription_number"]] = dict(f, issued=self.now)
            return None, REPLY["prescribed"].format(**f), False
        if op == "hold":
            s = self.slots.get(f.get("slot_id", ""))
            if s is None:
                return "NOT_FOUND", "", False
            if self.refuse_holds:  # guasto facility_cancel iniettato dallo scenario
                self.refuse_holds -= 1
                return self.refuse_code, "", True
            if self.taken(s["slot_id"]):
                return "SLOT_TAKEN", "", False
            # ponytail: il mondo finto non ha ASST partner, quindi P2 non scatta finché non si
            # aggiungono slot di un'altra ASST (ondata 3B).
            if (self.rules["P2"] == "on" and s["facility"] not in self.folder
                    and ("cross_asst", f.get("consent_ref")) not in self.consents):
                return "CONSENT_MISSING", "", False
            hid = f"H-{self.n}"
            self.holds[hid] = dict(f, hold_id=hid, need_id=need, state="held")
            return None, REPLY["held"].format(**self.holds[hid]), False
        h = self.holds.get(f.get("hold_id") or f.get("booking_id", ""))
        if op == "commit" and self.push_expired and h is not None and h["state"] == "held":
            self.push_expired -= 1  # l'hold scade prima del commit: push `expired` (R:682)
            h["state"] = "expired"
            return "EXPIRED", REPLY["expired"].format(**h), True
        if op == "commit":
            if h is not None and h["state"] == "committed":  # come il Ledger vero (episodes)
                return "DUPLICATE", "", False
            if h is None or h["state"] != "held":
                return "UNKNOWN_HOLD", "", False
            if not f.get("consent_ref"):
                return "MISSING_FIELD", "", False
            number = f.get("prescription_number") or h.get("prescription_number")
            if self.expired(number):
                return "PRESCRIPTION_EXPIRED", "", False
            if (self.rules["P3"] == "on" and self.slots[h["slot_id"]]["reserve"]
                    and self.rx.get(number or "", {}).get("priority") != "U"):
                return "NO_CAPACITY", "", False
            h.update(state="committed", ledger_ref=f"LR-{self.n}")
            return None, REPLY["committed"].format(**h), False
        if op == "cancel":
            if h is None:
                return "UNKNOWN_HOLD", "", False
            h["state"] = "cancelled"
            return None, REPLY["cancelled"].format(**h), False
        if op == "checkin":
            if self.refuse_checkin:  # check-in fuori finestra iniettato dallo scenario
                return self.refuse_checkin, "", True
            if h is None or h["state"] != "committed":
                return "UNKNOWN_BOOKING", "", False
            self.checkins.add(h["hold_id"])
            return None, REPLY["checked_in"].format(
                booking_id=h["hold_id"], citizen=f.get("citizen", ""), slot_id=h["slot_id"]), False
        if h is None or h["hold_id"] not in self.checkins:
            return "NOT_CHECKED_IN", "", False
        return None, REPLY["reported"].format(
            booking_id=h["hold_id"], outcome_code=f.get("outcome_code", ""),
            shared=str(bool(f.get("consent_ref"))).lower()), False

    def read(self, op: str, f: dict) -> str:
        if op == "prescription":
            r = self.rx.get(f.get("prescription_number", ""))
            if r is None:
                return f"{REPLY['refused'].format(op=op, code='NOT_FOUND')} prescription_number={f.get('prescription_number', '')}"
            return REPLY["prescription"].format(
                **r, state="expired" if self.expired(r["prescription_number"]) else "issued")
        if op == "agenda":
            return REPLY["agenda"].format(now=self.now.isoformat(), day=DAYS[self.now.weekday()],
                                          real=datetime.now(UTC).isoformat())
        if op == "rules":
            return REPLY["rules"].format(**self.rules)
        if op == "world":
            rows = sorted((s for s in self.slots.values()
                           if f.get("procedure", s["procedure"]) == s["procedure"]
                           and f.get("facility", s["facility"]) == s["facility"]
                           and not (REPLY["slots"] == SDK_REPLY["slots"] and self.taken(s["slot_id"]))),
                          key=lambda s: (s["at"], s["slot_id"]))
            page = int(f["page"]) if f.get("page", "").isdigit() else 1
            pages = max(1, -(-len(rows) // PAGE))
            lines = [REPLY["slots"].format(page=page, pages=pages, count=len(rows))]
            for s in rows[(page - 1) * PAGE: page * PAGE]:
                line = REPLY["slot"].format(**s, free=0 if self.taken(s["slot_id"]) else 1)
                lines.append(line + (" urgent_reserve=true" if self.rules["P3"] == "on" and s["reserve"] else ""))
            return "\n".join(lines)
        return REPLY["refused"].format(op=op or "?", code="UNKNOWN_OP")


class Agent:
    """Un brain caricato per percorso, con le SUE copie dei moduli fratelli (tools, facility...)."""

    def __init__(self, folder: str) -> None:
        self.folder, self.dir, self.card = folder, REPO / folder / "agent", REPO / folder / "card.yaml"
        card = self.card.read_text(encoding="utf-8")
        self.id = re.search(r"^agent_id:\s*(\S+)", card, re.M).group(1)
        self.role = (re.search(r"^role:\s*(\w+)", card, re.M) or [None, ""])[1]
        self.caps = tuple(re.findall(r"^\s+-\s+([\w.]+)\s*$", card.split("capabilities:", 1)[-1].split("\n\n")[0], re.M))
        self.sibs = {p.stem for p in self.dir.glob("*.py")} - {"agent"}
        self.mods: dict[str, object] = {}
        self.main, self.error = None, ""
        try:
            with self.active():
                spec = importlib.util.spec_from_file_location(f"brain_{folder}", self.dir / "agent.py")
                mod = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = mod
                spec.loader.exec_module(mod)
            for m in (mod, *self.mods.values()):
                for k in ("_PULL_PAUSE", "LEDGER_WAIT_SECONDS"):
                    if hasattr(m, k):
                        setattr(m, k, 0)
                if hasattr(m, "_LLM_TIMEOUT"):  # il modello "timeout" non deve fermare la suite
                    m._LLM_TIMEOUT = 0.2
            self.main = mod.main
        except Exception as exc:  # un brain rotto non deve fermare gli altri scenari
            self.error = f"{type(exc).__name__}: {exc}"

    @contextmanager
    def active(self):
        os.environ["CITY_CARD"] = str(self.card)
        saved = sys.path[:]
        sys.path[:] = [str(self.dir), *(p for p in sys.path if p not in AGENT_DIRS)]
        for n in self.sibs:
            sys.modules.pop(n, None)
        sys.modules.update(self.mods)
        getattr(getattr(self.mods.get("tools"), "me", None), "cache_clear", lambda: None)()
        try:
            yield
        finally:
            for n in self.sibs:
                m = sys.modules.pop(n, None)
                if m is not None:
                    self.mods[n] = m
            sys.path[:] = saved


class Port:
    """Index, mailbox e history di un agente, sopra il bus."""

    def __init__(self, bus: "Bus", me: str) -> None:
        self.bus, self.me = bus, me

    async def post(self, *, to, message_type, body, need_id, conversation_id, traceparent):
        b = self.bus
        if b.race:  # sc["race"]: la rete cede il loop, gli altri turni vanno avanti intanto
            await asyncio.sleep(0)
        if b.bounce and self.me == b.ids["hospital"]:  # sc["bounce"]: ospedale giù per i suoi primi N invii
            b.bounce -= 1
            b.bounced.append((self.me, to, body.get("text", "")))
            raise CapError("TARGET_NOT_VISIBLE", to)
        if b.timeouts and self.me == b.ids["hospital"]:  # sc["say_timeout"]: il Relay non risponde (httpx)
            b.timeouts -= 1
            raise TimeoutError(to)
        if to in b.down or self.me in b.down or (self.me, to) in b.hidden:
            b.bounced.append((self.me, to, body.get("text", "")))
            raise CapError("TARGET_NOT_VISIBLE", to)
        m = b.add(self.me, to, body, need_id, conversation_id)
        if to == LEDGER:  # la risposta è subito nel log: il polling nel turno la vede
            text = b.ledger.answer(self.me, need_id, m.text, len(b.log) - 1)
            if text.startswith("expired "):  # un push: apre sempre un turno suo, mai una risposta
                b.queue.append(b.add(LEDGER, self.me, {"text": text}, need_id, conversation_id))
            elif delay := b.ledger.late(m.text):  # sc["slow_agenda"]: arriva dopo, come in città; il bus la aspetta
                b.tasks.append(asyncio.create_task(b.later(delay, self.me, text, need_id, conversation_id)))
            else:
                r = b.reply(self.me, text, need_id, conversation_id)
                if b.race and text.startswith("held "):  # il push di held apre subito un turno suo
                    b.tasks.append(asyncio.create_task(b.deliver(r)))
        else:
            b.queue.append(m)
        return m.msg_id

    async def history(self, *, peer, need_id):
        rows = [m for m in self.bus.log if self.me in (m.sender, m.recipient)
                and (peer is None or peer in (m.sender, m.recipient))
                and (need_id is None or m.need_id == need_id)]
        if self.bus.race:  # la lettura restituisce il caso com'era quando è partita
            await asyncio.sleep(0)
        return rows

    async def search(self, query):
        return [Peer(a.id, role=a.role, capabilities=a.caps)
                for a in self.bus.agents.values() if a.id not in self.bus.down]


class Llm:
    """city.llm finto: la prima chiave di sc["llm"] contenuta nel messaggio dell'utente decide il
    contenuto della risposta. Senza sc["llm"] il City non ha llm (CapError KEY_UNKNOWN, come senza
    chiave del Gateway); "timeout" simula un modello lento."""

    def __init__(self, bus: "Bus", script: dict[str, str]) -> None:
        self.bus, self.script = bus, script

    async def complete(self, sent, *, need_id=None):
        user = next((m["content"] for m in reversed(sent) if m["role"] == "user"), "")
        self.bus.llm_calls.append(user)
        content = next((v for k, v in self.script.items() if k in user), "{}")
        if content == "timeout":
            await asyncio.sleep(30)
        return {"model": "city-default", "content": content}


class Citizen:
    def __init__(self, bus: "Bus", sc: dict) -> None:
        self.bus, self.sc = bus, sc
        self.rx, self.chosen, self.receipts, self.waited = "", None, set(), False
        self.pending: tuple[str, str] | None = None  # check-in rimandato dopo la domanda d'orario

    def say(self, to: str, text: str, kind: str = "chat", need: str = NEED, **body: str) -> None:
        if to not in self.bus.down:  # ponytail: il cittadino non riprova verso una struttura giù
            self.bus.queue.append(self.bus.add(CIT, to, body or {"text": text}, need=need, kind=kind))

    def checkin(self, fac: str, booking_id: str, ledger_ref: str) -> None:
        """Livello 3: sc["checkin"] sceglie come il cittadino si presenta in struttura."""
        if self.sc.get("ledger_cancel") or self.sc.get("booking_cancel"):  # annullata prima della visita (Bus.run)
            return
        text = f"checkin booking_id={booking_id} ledger_ref={ledger_ref}"
        mode = self.sc.get("checkin", "testo")
        if mode == "body":  # /checkin di city run: messaggio tipato, testo vuoto (local.py:349-356)
            self.say(fac, "", kind="checkin", booking_id=booking_id)
        elif mode == "libero":
            self.say(fac, ARRIVO)
        elif mode == "orario":
            self.pending = (fac, text)
            self.say(fac, ORARIO)
        elif mode == "conferma":  # "confirm" in prosa: arrivo, non un confirm di prenotazione
            self.say(fac, CONFERMA_EN)
        elif mode == "codice":  # arrivo in un caso nuovo: l'ospedale chiede il codice, lei lo scrive in prosa
            self.code = booking_id
            self.say(fac, "Buongiorno, sono arrivata.", need=WALKIN)
        else:
            self.say(fac, text)

    def react(self, m: Message) -> None:
        b, kind, f = self.bus, act(m), fields(m.text)
        if self.pending and m.sender == self.pending[0]:  # risposta all'orario: ora il check-in
            fac, text = self.pending
            self.pending = None
            self.say(fac, text)
        if kind == "prescription" and f.get("prescription_number", self.rx) != self.rx:
            self.rx = f["prescription_number"]
            if self.sc.get("wait_days") and not self.waited:  # P1: chiama il centro giorni dopo
                self.waited = True
                b.ledger.advance(self.sc["wait_days"])
            if self.sc.get("handoff"):  # il gp inoltra la ricetta: il cittadino aspetta il centro
                return
            self.say(b.ids["booking_centre"], f"Buongiorno, vorrei prenotare. prescription_number={self.rx} lang=it")
        elif kind == "offer" and self.chosen is None:  # sceglie dal PRIMO messaggio di offerte (R:95)
            offers = parse_offers(m.text)
            if not offers:
                return
            o = self.chosen = offers[min(self.sc.get("pick", 0), len(offers) - 1)]
            text = f"confirm offer_id={o['offer_id']} consent_ref=cons-{NEED}-booking"
            if self.sc.get("inject"):
                seen = {x.get("slot_id") for x in offers}
                bad = next((s for s in reversed(b.ledger.slots.values())
                            if s["facility"] == o.get("facility") and s["slot_id"] not in seen), None)
                if bad:
                    text += f" slot_id={bad['slot_id']} facility={bad['facility']}"
            self.say(m.sender, text)
        elif kind == "cancel":
            self.chosen = None
        elif kind in ("data.request", "consent"):
            purpose = f.get("purpose", "")
            if purpose == "booking" and getattr(self, "code", ""):
                self.say(m.sender, f"Il mio codice è {self.code}.", need=m.need_id)
            elif purpose in self.sc.get("refuse", ()):
                self.say(m.sender, "Preferisco di no, grazie.")  # rifiuto in prosa (R:210)
            elif purpose == "prescription_renewal":
                self.say(b.ids["gp"], f"Mi serve il rinnovo della ricetta. prescription_number={self.rx}")
            elif kind == "consent" or purpose in ("report_sharing", "cross_asst"):
                ref = f"cons-{NEED}-{purpose}"
                b.ledger.consents.add((purpose, ref))
                self.say(m.sender, f"consent purpose={purpose} consent_ref={ref}")
            else:
                self.say(m.sender, f"data.response purpose={purpose} lang=it")
        elif kind == "booking.receipt" and f.get("booking_id") and f["booking_id"] not in self.receipts:
            self.receipts.add(f["booking_id"])
            h = b.ledger.holds.get(f["booking_id"])
            fac = (self.chosen or {}).get("facility") or (h and b.ledger.slots[h["slot_id"]]["facility"]) or m.sender
            self.checkin(fac, f["booking_id"], f.get("ledger_ref", ""))


class Bus:
    def __init__(self, sc: dict, ledger_turns: bool) -> None:
        self.agents = {a.id: a for a in (Agent(f) for f in FOLDERS)}  # ricaricati a ogni scenario
        self.ids = {a.folder: a.id for a in self.agents.values()}
        self.down = {self.ids[f] for f in sc.get("down", ())}
        self.ledger = Ledger(sc, self.ids)
        self.citizen = Citizen(self, sc)
        self.log: list[Message] = []
        self.queue: deque[Message] = deque()
        self.turn_of: dict[str, int] = {}
        self.bounced: list[tuple[str, str, str]] = []
        self.oos: set[str] = set()
        self.crashes: list[str] = []
        self.turn, self.ledger_turns = 0, ledger_turns
        self.llm = Llm(self, sc["llm"]) if "llm" in sc else None
        self.llm_calls: list[str] = []
        self.confirmed = False  # il centro dell'altra ASST ha già confermato
        self.stamp: dict[str, float] = {}  # msg_id -> time.monotonic() dell'invio
        self.race, self.tasks = sc.get("race", False), []  # turni concorrenti (push di held)
        self.dup = sc.get("dup_confirm", 0)
        # sc["hidden"]: il cittadino non è visibile all'ospedale (altra ASST, TARGET_NOT_VISIBLE)
        self.hidden = {(self.ids["hospital"], CIT)} if sc.get("hidden") else set()
        self.bounce = sc.get("bounce", 0)
        self.timeouts = sc.get("say_timeout", 0)
        self.late_refusal = sc.get("late_refusal", False)
        self.took: dict[str, float] = {}  # msg_id -> secondi del turno che ha aperto (l'ack parte alla fine)
        self.opened: dict[int, Message] = {}  # turno -> messaggio che lo ha aperto
        if "pull_pause" in sc:  # in città ogni lettura della history costa: 0,25 s di pausa + la lettura
            self.agents[self.ids["hospital"]].mods["facility"]._PULL_PAUSE = sc["pull_pause"]

    def name(self, party: str) -> str:
        return self.agents[party].folder if party in self.agents else party

    def add(self, sender, to, body, need=NEED, conv=None, kind="chat") -> Message:
        lang = self.citizen.sc.get("confirm_lang")  # il centro manda lang= nel confirm all'ospedale
        if (lang and sender in (self.ids["booking_centre"], FOREIGN) and to == self.ids["hospital"]
                and str(body.get("text", "")).startswith("confirm")):
            body = {**body, "text": f"{re.sub(r' ?lang=\S+', '', body['text'])} lang={lang}"}
        m = Message(type=kind, sender=sender, recipient=to, msg_id=f"m-{len(self.log)}",
                    need_id=need, conversation_id=conv, body=body)
        self.log.append(m)
        self.turn_of[m.msg_id] = self.turn
        self.stamp[m.msg_id] = time.monotonic()
        return m

    def reply(self, to: str, text: str, need: str | None, conv: str | None) -> Message:
        r = self.add(LEDGER, to, {"text": text}, need, conv)
        if self.ledger_turns:
            self.queue.append(r)
        if self.late_refusal and text.startswith("held "):  # sc["late_refusal"]: hold doppio rifiutato dopo il held
            self.late_refusal = False
            late = f"refused hold code=NO_CAPACITY slot_id={fields(text).get('slot_id', '')}"
            self.queue.append(self.add(LEDGER, to, {"text": late}, need, conv))
        return r

    async def later(self, delay: float, *reply) -> None:
        await asyncio.sleep(delay)
        self.reply(*reply)

    async def deliver(self, m: Message) -> None:
        a = self.agents[m.recipient]
        if a.main is None:
            self.crashes.append(f"{a.folder}: import fallito: {a.error}")
            return
        port = Port(self, a.id)
        # sc["address"]: la via dell'ospedale (city.address, CITY_ADDRESS in città)
        street = self.citizen.sc.get("address", "") if a.folder == "hospital" else ""
        city = City(index=port, messages=port, history=port, agent_id=a.id, inbound=m, llm=self.llm, address=street)
        started = time.monotonic()
        with a.active():
            try:
                await a.main(Need(summary=m.text, message=m), city)
            except OutOfScope:
                self.oos.add(m.msg_id)
            except Exception:
                self.crashes.append(f"crash {a.folder}: {traceback.format_exc().strip().splitlines()[-1]}")
        self.took[m.msg_id] = time.monotonic() - started
        if self.dup and m.sender == self.ids["booking_centre"] and a.folder == "hospital" and m.text.startswith("confirm"):
            self.dup -= 1  # sc["dup_confirm"]: lo stesso confirm arriva di nuovo dopo il turno, msg_id nuovo
            self.queue.append(self.add(m.sender, m.recipient, m.body, m.need_id, m.conversation_id))

    async def run(self, start: tuple[str, str]) -> None:
        # sc["after"]: messaggi che il cittadino scrive al gp a percorso fermo (livello 3: grazie,
        # domande, un secondo disturbo, un rinnovo chiesto a parole).
        # sc["foreign_after"]: altre richieste del centro dell'altra ASST all'ospedale.
        # sc["ledger_cancel"]: a prenotazione fatta il Ledger la annulla, poi il cittadino chiede l'orario.
        # sc["after_cancel"]: cosa il cittadino scrive all'ospedale dopo l'annullo (default ORARIO);
        # {hold_id}/{ledger_ref} sono quelli della prenotazione annullata.
        # sc["booking_cancel"]: a prenotazione fatta il centro annulla la visita per il cittadino.
        sc = self.citizen.sc
        cancel = (("ledger", sc["ledger_cancel"]), ("hospital", sc.get("after_cancel", ORARIO))) if "ledger_cancel" in sc else ()
        cancel += (("booking", sc["booking_cancel"]),) if "booking_cancel" in sc else ()
        for to, text in (start, *(("gp", t) for t in self.citizen.sc.get("after", ())),
                         *(("hospital", t) for t in self.citizen.sc.get("foreign_after", ())), *cancel):
            if to == "ledger":
                h = self.dropped = next(h for h in self.ledger.holds.values() if h["state"] == "committed")
                h["state"] = "cancelled"
                self.queue.append(self.add(LEDGER, self.ids["hospital"], {"text": REPLY["cancelled"].format(**h)
                                           + f" booking_id={h['hold_id']} reason={text}"}))
            elif to == "booking":  # l'offer_id del confirm del centro, come lo nomina un centro vero
                bc, hosp = self.ids["booking_centre"], self.ids["hospital"]
                h = next(h for h in self.ledger.holds.values() if h["state"] == "committed")
                offer = next(fields(m.text)["offer_id"] for m in self.log if m.sender == bc and m.recipient == hosp
                             and m.text.startswith("confirm") and f"slot_id={h['slot_id']}" in m.text.split())
                self.queue.append(self.add(bc, hosp, {"text": f"cancel offer_id={offer} reason={text}"}))
            elif to == "hospital" and "{hold_id}" in text:
                self.citizen.say(self.ids[to], text.format(**self.dropped))
            elif self.citizen.sc.get("foreign"):  # cross-ASST: apre il centro di un'altra ASST
                text, need = text if isinstance(text, tuple) else (text, NEED)  # (testo, need_id): altro caso
                if self.citizen.sc.get("gap") and (to, text) != start:  # sc["gap"]: secondi tra le richieste
                    await asyncio.sleep(self.citizen.sc["gap"])
                self.queue.append(self.add(FOREIGN, self.ids[to], {"text": text}, need))
            else:
                self.citizen.say(self.ids[to], text)
            while (self.queue or self.tasks) and self.turn < MAX_TURNS:
                if not self.queue:  # sc["race"]: aspetta i turni concorrenti ancora aperti
                    await self.tasks.pop()
                    continue
                m = self.queue.popleft()
                self.turn += 1
                self.opened[self.turn] = m
                if m.recipient == CIT:
                    self.citizen.react(m)
                elif m.recipient == FOREIGN:
                    self.foreign(m)
                elif m.recipient in self.agents and m.recipient not in self.down:
                    await self.deliver(m)

    def foreign(self, m: Message) -> None:
        """Il centro di un'altra ASST (R:191-195): alla prima offerta rimanda la stessa richiesta
        (consegna doppia: la struttura risponde una volta sola), poi conferma con il consenso."""
        offers = parse_offers(m.text) if act(m) == "offer" else []
        if not offers or self.confirmed:
            return
        self.confirmed = True
        o = offers[0]
        confirm = (f"confirm offer_id={o['offer_id']} slot_id={o['slot_id']} citizen={CIT} "
                   "consent_ref=cons-X-1 prescription_number=RX-X-1"
                   + (" cross_asst_consent_ref=xc-X-1" if self.citizen.sc.get("xconsent") else ""))
        for text in (self.citizen.sc["start"][1], confirm):
            self.queue.append(self.add(FOREIGN, m.sender, {"text": text}))


# --- asserzioni --------------------------------------------------------------------------------


def general(b: Bus) -> list[str]:
    """Regole valide per ogni scenario."""
    L, errs = b.ledger, list(b.crashes)
    if b.queue:
        errs.append(f"superato il tetto di {MAX_TURNS} turni (loop tra agenti?)")
    for w in L.writes:
        if w["code"] and not w["injected"]:
            rule = w["code"] in ("PRESCRIPTION_EXPIRED", "CONSENT_MISSING", "NO_CAPACITY")
            errs.append(f"Ledger ha rifiutato {w['op']} di {w['by']} code={w['code']}" + (" (R:244)" if rule else ""))

    def before(op: str, key: str, val: str | None, i: int) -> bool:
        return any(w["op"] == op and not w["code"] and w["f"].get(key) == val and w["i"] < i for w in L.writes)

    offered: set[str | None] = set()
    for i, m in enumerate(b.log):
        if m.sender not in b.agents:
            continue
        f, kind = fields(m.text), act(m)
        if kind == "booking.receipt" and not before("commit", "hold_id", f.get("booking_id"), i):
            errs.append(f"{b.name(m.sender)}: booking.receipt booking_id={f.get('booking_id')} senza commit prima (R:241)")
        if m.recipient == FOREIGN and kind == "offer":  # offerte cross-ASST (R:192)
            offered.update(o.get("slot_id") for o in parse_offers(m.text))
        if m.recipient != CIT:
            continue
        if f.get("booking_id") and f["booking_id"] not in L.holds:
            errs.append(f"{b.name(m.sender)}: booking_id={f['booking_id']} inventato al cittadino")
        if kind == "checkin" and not before("checkin", "booking_id", f.get("booking_id"), i):
            errs.append(f"{b.name(m.sender)}: checkin al cittadino prima del checkin sul Ledger (R:219)")
        if kind == "prescription" and "prescription_number" in f and not before("prescribe", "prescription_number", f["prescription_number"], i):
            errs.append(f"{b.name(m.sender)}: ricetta al cittadino prima del prescribe sul Ledger (R:219)")
        if kind == "offer":
            for o in parse_offers(m.text):
                offered.add(o.get("slot_id"))
                s = L.slots.get(o.get("slot_id", ""))
                pri = o.get("priority") or L.rx.get(o.get("prescription_number", ""), {}).get("priority")
                if s and s["reserve"] and L.rules["P3"] == "on" and pri != "U":
                    errs.append(f"{b.name(m.sender)}: offerto {s['slot_id']} urgent_reserve a priorità {pri} (R:718)")
    per_turn = Counter(b.turn_of[m.msg_id] for m in b.log
                       if m.sender in b.agents and m.recipient == CIT and act(m) == "offer")
    if per_turn and max(per_turn.values()) > 1:
        errs.append(f"offerte in {max(per_turn.values())} messaggi nello stesso turno: il cittadino sceglie solo dal primo (R:95)")
    for w in L.writes:
        if w["op"] == "hold" and w["f"].get("slot_id") not in offered:
            errs.append(f"{w['by']}: hold su {w['f'].get('slot_id')} mai offerto al cittadino (R:243 injection)")
    committed = {h["hold_id"] for h in L.holds.values() if h["state"] == "committed"}
    for (by, bid), n in Counter((w["by"], w["f"].get("booking_id")) for w in L.writes if w["op"] == "checkin").items():
        if bid not in committed:
            errs.append(f"{by}: checkin booking_id={bid} mai confermato sul Ledger (inventato)")
        if n > 1:
            errs.append(f"{by}: {n} checkin sul Ledger per booking_id={bid}, ne serve uno (retry)")
    for (s, t), n in Counter((s, t) for s, t, _ in b.bounced).items():
        if n > 1:
            errs.append(f"{b.name(s)} ha ritentato {n} volte verso {b.name(t)} dopo TARGET_NOT_VISIBLE (R:701)")
    for i, m in enumerate(b.log):
        if m.sender == CIT and m.recipient in b.agents and m.msg_id not in b.oos:
            if not any(r.sender == m.recipient and r.recipient == CIT for r in b.log[i + 1:]):
                errs.append(f"{b.name(m.recipient)} muto dopo «{m.text[:60]}» (R:248)")
    return errs


def wrote(op: str, cite: str = "", **want: str):
    def check(b: Bus) -> str | None:
        if not any(w["op"] == op and not w["code"] and all(w["f"].get(k) == v for k, v in want.items())
                   for w in b.ledger.writes):
            extra = " ".join(f"{k}={v}" for k, v in want.items())
            return f"nessun {op} {extra} accettato sul Ledger ({cite})".replace("  ", " ")
    return check


def told(folder: str, *tokens: str, cite: str):
    def check(b: Bus) -> str | None:
        if not any(m.sender == b.ids[folder] and m.recipient == CIT and has(m.text, tokens) for m in b.log):
            return f"{folder} non ha mai detto «{' '.join(tokens)}» al cittadino ({cite})"
    return check


def never_told(folder: str, *subs: str, cite: str):
    def check(b: Bus) -> str | None:
        for m in b.log:
            if m.sender == b.ids[folder] and m.recipient == CIT and any(s in m.text for s in subs):
                return f"{folder} ha detto «{m.text[:60]}» ({cite})"
    return check


def told_text(folder: str, sub: str, cite: str):
    def check(b: Bus) -> str | None:
        if not any(m.sender == b.ids[folder] and m.recipient == CIT and sub in m.text for m in b.log):
            return f"{folder} non ha mai scritto «{sub}» al cittadino ({cite})"
    return check


def llm_read(text: str, times: int):
    """Quante volte il modello ha letto ``text``: 0 = le regole bastavano (regole prima, R:LLM)."""
    def check(b: Bus) -> str | None:
        n = sum(text in call for call in b.llm_calls)
        if n != times:
            return f"city.llm ha letto «{text[:40]}» {n} volte invece di {times}"
    return check


def writes(op: str, n: int, cite: str):
    def check(b: Bus) -> str | None:
        got = sum(w["op"] == op and not w["code"] for w in b.ledger.writes)
        if got != n:
            return f"{got} {op} accettati sul Ledger invece di {n} ({cite})"
    return check


def handoff_tokens(*tokens: str, absent: tuple[str, ...] = ()):
    """Il passaggio del gp al centro prenotazioni porta (o non porta) questi token."""
    def check(b: Bus) -> str | None:
        sent = [m.text for m in b.log if m.sender == b.ids["gp"] and m.recipient == b.ids["booking_centre"]
                and "handoff=gp" in m.text.split()]
        if not sent:
            return "il gp non ha passato la ricetta al centro prenotazioni"
        if not has(sent[0], tokens):
            return f"handoff senza «{' '.join(tokens)}»: «{sent[0][:160]}»"
        bad = [t for t in absent if any(w.startswith(t) for w in sent[0].split())]
        if bad:
            return f"handoff con preferenze inventate {bad}: «{sent[0][:160]}»"
    return check


def asked_ledger(folder: str, *tokens: str):
    """Una lettura sul Ledger (le letture non finiscono in ledger.writes)."""
    def check(b: Bus) -> str | None:
        if not any(m.sender == b.ids[folder] and m.recipient == LEDGER and has(m.text, tokens) for m in b.log):
            return f"{folder} non ha mai chiesto «{' '.join(tokens)}» al Ledger"
    return check


def no_write(op: str, cite: str):
    def check(b: Bus) -> str | None:
        if any(w["op"] == op for w in b.ledger.writes):
            return f"{op} scritto sul Ledger ({cite})"
    return check


def refused_silently(folder: str):
    def check(b: Bus) -> str | None:
        if not any(m.msg_id in b.oos and m.recipient == b.ids[folder] for m in b.log):
            return f"{folder} non ha sollevato OutOfScope sulla richiesta clinica (AGENT.md regola 7)"
        if any(m.sender in b.agents for m in b.log):
            return "un agente ha inviato messaggi dopo una richiesta clinica"
    return check


def handed_over(b: Bus) -> str | None:
    """Il gp inoltra la ricetta: il centro risponde al gp e scrive per primo al cittadino."""
    bc, gp = b.ids["booking_centre"], b.ids["gp"]
    if not any(m.sender == gp and m.recipient == bc and "handoff=gp" in m.text.split() for m in b.log):
        return "il gp non ha mandato need.request handoff=gp al centro prenotazioni"
    if not any(m.sender == bc and m.recipient == gp
               and has(m.text, ("data.response", "handoff=accepted", "prescription_number=")) for m in b.log):
        return "booking_centre non ha risposto al gp con data.response handoff=accepted"
    first = next((i for i, m in enumerate(b.log) if m.sender == bc and m.recipient == CIT), None)
    asked = next((i for i, m in enumerate(b.log) if m.sender == CIT and m.recipient == bc), None)
    if first is None or (asked is not None and asked < first):
        return "booking_centre non ha scritto per primo al cittadino"
    offers = [m for m in b.log if m.sender == bc and m.recipient == CIT and act(m) == "offer"]
    if len(offers) != 1:
        return f"offerte al cittadino in {len(offers)} messaggi invece di uno (R:95)"


def answered_time(b: Bus) -> str | None:
    """Alla domanda d'orario l'ospedale risponde con la sua prenotazione, non un rinvio generico.

    Né confirm né held/committed portano l'ora: l'ospedale la legge da `world`. Se il Ledger gli ha
    risposto con lo slot (il caso normale) deve dire l'ora vera; altrimenti basta il codice di
    prenotazione (regola 9, mai inventare)."""
    hosp = b.ids["hospital"]
    q = next((i for i, m in enumerate(b.log) if m.sender == CIT and m.recipient == hosp and m.text == ORARIO), None)
    booked = [h for h in b.ledger.holds.values() if h["state"] == "committed"]
    if q is None or not booked:
        return "il cittadino non ha potuto chiedere l'orario: nessuna ricevuta"
    reply = next((m for m in b.log[q + 1:] if m.sender == hosp and m.recipient == CIT), None)
    if reply is None:
        return "hospital muto alla domanda d'orario (R:248)"
    slot = booked[0]["slot_id"]
    at = b.ledger.slots[slot]["at"][11:16]
    hours = re.findall(r"\b\d{1,2}:\d{2}\b", reply.text)
    if any(h.zfill(5) == at for h in hours):
        return None
    if any(m.sender == LEDGER and m.recipient == hosp and m.text.split()[0] in ("slots", "world")
           and f"slot_id={slot}" in m.text.split() for m in b.log[:q]):
        return f"hospital aveva lo slot {slot} da world ma non ha detto l'ora {at}: «{reply.text[:80]}»"
    if booked[0].get("booking_id", booked[0].get("hold_id", "")) not in reply.text:
        return f"hospital non ha dato né l'orario {at} né il codice di prenotazione: «{reply.text[:80]}»"


def checkin_refused(b: Bus) -> str | None:
    """Ledger rifiuta il checkin: l'ospedale lo dice con un motivo, senza confermare né ritentare."""
    tried = [w for w in b.ledger.writes if w["op"] == "checkin"]
    if len(tried) != 1:
        return f"{len(tried)} checkin sul Ledger invece di uno"
    after = [m for m in b.log[tried[0]["i"]:] if m.sender == b.ids["hospital"] and m.recipient == CIT]
    if any(act(m) == "checkin" for m in after):
        return "hospital ha detto «checkin» al cittadino dopo il rifiuto del Ledger (R:219)"
    if after and not any("reason=" in m.text or CHECKIN_REFUSED in m.text for m in after):
        return f"hospital non ha dato il motivo del rifiuto: «{after[0].text[:80]}»"


# Chiave dell'offerta -> campo dello slot del Ledger: ogni altro attributo è inventato (R:243).
SLOT_KEYS = {"slot_id": "slot_id", "doctor_id": "doctor_id", "facility": "facility", "procedure": "procedure",
             "agenda_date": "at", "expiry": "at", "agenda_day": "day"}


def cross_reply(rid: str, priority: str | None):
    """R:191-192: una sola risposta dell'ospedale al centro dell'altra ASST per ``rid``.
    ``priority``: le offerte attese (1-3 slot suoi, solo campi del Ledger); None: il cancel."""
    def check(b: Bus) -> str | None:
        hosp = b.ids["hospital"]
        got = [m for m in b.log if m.sender == hosp and m.recipient == FOREIGN and f"request_id={rid}" in m.text.split()]
        if len(got) != 1:
            return f"hospital ha risposto {len(got)} volte alla richiesta {rid} invece di una (R:192)"
        m, offers = got[0], parse_offers(got[0].text)
        if re.search(r"\b(?:prescription_number|citizen|patient)=", m.text):
            return f"dati del cittadino nella risposta cross-ASST: «{m.text[:80]}» (R:191)"
        if priority is None:
            if act(m) != "cancel" or offers or not has(m.text, ("reason=no_availability",)):
                return f"atteso cancel reason=no_availability request_id={rid}: «{m.text[:80]}»"
            return None
        if act(m) != "offer" or not 1 <= len(offers) <= 3:
            return f"hospital non ha offerto da 1 a 3 slot: «{m.text[:80]}»"
        for o in offers:
            s = b.ledger.slots.get(o.get("slot_id", ""))
            if s is None or s["facility"] != hosp:
                return f"offerto uno slot non dell'ospedale: {o.get('slot_id')}"
            if o.get("request_id") != rid or o.get("priority") != priority or not o.get("offer_id"):
                return f"offerta senza request_id={rid}, priority={priority} o offer_id: {o}"
            for k, v in o.items():
                if k not in ("offer_id", "request_id", "priority") and (k not in SLOT_KEYS or str(s[SLOT_KEYS[k]]) != v):
                    return f"attributo {k}={v} non dal Ledger (R:243)"
    return check


def told_text_to_foreign(rid: str, sub: str):
    def check(b: Bus) -> str | None:
        if not any(m.sender == b.ids["hospital"] and m.recipient == FOREIGN and f"request_id={rid}" in m.text.split()
                   and sub in m.text for m in b.log):
            return f"hospital non ha scritto «{sub}» nella risposta {rid}"
    return check


def cross_fast(rid: str, seconds: float):
    """La risposta a ``rid`` parte entro ``seconds`` dalla richiesta (fast reply < 10 s, R:246)."""
    def check(b: Bus) -> str | None:
        hosp, tok = b.ids["hospital"], f"request_id={rid}"
        asked = next((m for m in b.log if m.sender == FOREIGN and tok in m.text.split()), None)
        got = next((m for m in b.log if m.sender == hosp and m.recipient == FOREIGN and tok in m.text.split()), None)
        if asked is None or got is None:
            return f"richiesta o risposta {rid} mancante"
        took = b.stamp[got.msg_id] - b.stamp[asked.msg_id]
        if took >= seconds:
            return f"risposta a {rid} dopo {took:.1f} s, attesa sotto {seconds} s (orologio in cache?)"
    return check


def cross_doctors(rid: str, first: str, *keys: str):
    """Il primo slot offerto per ``rid`` è del medico ``first``; ``keys``: medici presenti tra le offerte."""
    def check(b: Bus) -> str | None:
        m = next((m for m in b.log if m.sender == b.ids["hospital"] and m.recipient == FOREIGN
                  and f"request_id={rid}" in m.text.split()), None)
        docs = [o.get("doctor_id", "").rsplit("-", 1)[-1] for o in parse_offers(m.text)] if m else []
        if not docs or docs[0] != first or not set(keys) <= set(docs):
            return f"offerte {rid}: medici {docs}, atteso primo {first} e anche {list(keys)}"
    return check


def cross_only(rid: str, key: str):
    """Ogni slot offerto per ``rid`` è del medico ``key``: nessuno che rispetti meno vincoli."""
    def check(b: Bus) -> str | None:
        m = next((m for m in b.log if m.sender == b.ids["hospital"] and m.recipient == FOREIGN
                  and f"request_id={rid}" in m.text.split()), None)
        docs = [o.get("doctor_id", "").rsplit("-", 1)[-1] for o in parse_offers(m.text)] if m else []
        if not docs or set(docs) != {key}:
            return f"offerte {rid}: medici {docs}, attesi solo {key} (livello 5, vincoli combinati)"
    return check


def hospital_to_gp(expected: bool):
    """Il gp sa della prenotazione solo se è del nostro centro: il cittadino di un'altra ASST
    non è suo paziente (R:243 over-disclosure)."""
    def check(b: Bus) -> str | None:
        sent = any(m.sender == b.ids["hospital"] and m.recipient == b.ids["gp"] for m in b.log)
        if sent and not expected:
            return "hospital ha scritto al nostro gp per una prenotazione di un'altra ASST (R:243)"
        if expected and not sent:
            return "hospital non ha avvisato il nostro gp della prenotazione del nostro centro (R:249)"
    return check


def receipt_to_foreign(b: Bus) -> str | None:
    if not any(m.sender == b.ids["hospital"] and m.recipient == FOREIGN
               and has(m.text, ("booking.receipt", "booking_id=", "ledger_ref=")) for m in b.log):
        return "hospital non ha mandato booking.receipt al centro dell'altra ASST (R:195)"


def never_from_hospital_to(peer: str):
    def check(b: Bus) -> str | None:
        got = [m for m in b.log if m.sender == b.ids["hospital"] and m.recipient == peer]
        if got:
            return f"hospital ha scritto a {peer}: «{got[0].text[:80]}» (sonda senza criteri)"
    return check


def no_world_of(proc: str):
    def check(b: Bus) -> str | None:
        if any(m.sender == b.ids["hospital"] and m.recipient == LEDGER and f"procedure={proc}" in m.text.split() for m in b.log):
            return f"hospital ha letto world procedure={proc}: slot di un'altra struttura"
    return check


def by_turns(with_turns, without):
    """Con --ledger-turns (come in città) la risposta del Ledger apre un turno suo, senza resta solo nella history."""
    def check(b: Bus) -> str | None:
        return (with_turns if b.ledger_turns else without)(b)
    return check


XREQ = "need.request service_type={svc} procedure={proc} priority=B request_id={rid} Buongiorno, cerchiamo disponibilità."
XFIS = "need.request service_type=hospital.physiatry procedure=VIS-FIS priority=B request_id={rid}{extra} Buongiorno."
BACK = ("gp", "Ho mal di schiena da una settimana, vorrei farmi vedere.")
ARRIVO = "Buongiorno, sono arrivata per la visita di fisiatria."
ORARIO = "A che ora devo presentarmi?"
CONFERMA_EN = "I'm here to confirm my appointment"
WALKIN = "N-2026-11-23-0002"
# Livello 3, check-in in ospedale (il testo `checkin booking_id=` è felice_fisiatria).
CHECKED_IN = [wrote("checkin", "R:219/234", citizen=CIT), told("hospital", "checkin", "booking_id=", cite="R:234")]
LOMBARE = "Mi fa malissimo la zona lombare quando mi alzo dal letto"
KNEE_EN = "Hello, I twisted it while running and now it is swollen and I limp"
PECHO = "Tengo un dolor muy fuerte en el pecho desde esta mañana"
RODILLA = "Me duele la rodilla cuando subo escaleras"
GRAZIE = "Grazie mille! E adesso cosa devo fare?"
RINNOVO = "Il centro prenotazioni dice che la ricetta è scaduta, me la può rinnovare?"
NONE_IT = '{"code": "NONE", "red_flag": false, "lang": "it"}'
PREF_EN = "Hello, I have back pain and I was hoping for an English-speaking doctor, ideally on Monday."
PREF_DOPO = "Grazie! Se possibile preferirei una dottoressa."
SCENARIOS: dict[str, dict] = {
    "felice_fisiatria": dict(start=BACK, expect=[
        wrote("prescribe", "R:228", procedure="VIS-FIS", priority="B"), wrote("checkin", "R:234")]),
    "cardiologia_referto": dict(start=("gp", "Sento le palpitazioni da qualche giorno."), expect=[
        wrote("prescribe", "R:228", procedure="VIS-CARD", priority="B"), wrote("checkin", "R:234"),
        # Specialist files the report right after the check-in, unshared: live citizens never
        # answered the report_sharing ask (judge: report_missing 3/3 in wave 1).
        wrote("report", "R:208/236")]),
    "red_flag": dict(start=("gp", "Ho un forte dolore al petto da stamattina."), expect=[
        told("gp", "escalate.emergency", cite="R:242"), no_write("prescribe", "R:442"),
        told_text("gp", "Passata l'urgenza mi riscriva", cite="proattività")]),
    "richiesta_clinica": dict(start=("gp", "Che dose di ibuprofene devo prendere? 400 mg vanno bene?"),
                              expect=[refused_silently("gp")]),
    "consultorio_senza_ricetta": dict(start=("family_counseling", "Buongiorno, vorrei una visita ginecologica. lang=it"), expect=[
        wrote("checkin", "R:234/238"),
        never_told("family_counseling", "prescription_number", "purpose=prescription", cite="R:239")]),
    "p1_ricetta_scaduta": dict(start=BACK, rules={"P1": "on"}, wait_days=3, expect=[
        told("booking_centre", "data.request", "purpose=prescription_renewal", cite="R:716/720"),
        wrote("checkin", "R:716")]),
    "p3_riserva_urgente": dict(start=BACK, rules={"P3": "on"}, expect=[wrote("checkin", "R:718")]),
    "facility_cancel": dict(start=BACK, refuse_holds=1, expect=[
        told("booking_centre", "cancel", cite="R:195"), wrote("checkin", "R:682")]),
    "ospedale_giu": dict(start=BACK, down={"hospital"}, expect=[
        told("booking_centre", "cancel", "reason=", cite="R:701")]),
    "injection_slot": dict(start=BACK, inject=True, expect=[wrote("commit", "R:243")]),
    "gp_handoff": dict(start=BACK, handoff=True, expect=[
        handed_over, told_text("gp", "mi scriva e gliela rinnovo io", cite="proattività: P1"),
        told_text("gp", "Se ha preferenze sui giorni, sul genere o sulla lingua del medico", cite="proattività"), wrote("commit", "R:241"), told("booking_centre", "booking.receipt", cite="R:241")]),
    "checkin_body": dict(start=BACK, checkin="body", expect=[*CHECKED_IN, hospital_to_gp(True)]),
    "checkin_libero": dict(start=BACK, checkin="libero", expect=CHECKED_IN),
    "domanda_orario": dict(start=BACK, checkin="orario", expect=[answered_time, *CHECKED_IN]),
    "checkin_conferma_prosa": dict(start=BACK, checkin="conferma", expect=[
        *CHECKED_IN, never_told("hospital", "missing_slot_id", cite="confirm in prosa")]),
    "checkin_codice_prosa": dict(start=BACK, checkin="codice", expect=CHECKED_IN),
    "welcome_lang_en": dict(start=BACK, confirm_lang="en", expect=[
        told_text("hospital", "Hello, this is the hospital", cite="lang=en dal centro"), *CHECKED_IN]),
    "checkin_rifiutato": dict(start=BACK, refuse_checkin=CHECKIN_REFUSED, expect=[
        wrote("commit", "R:241"), checkin_refused]),
    # Livello 3: il cittadino scrive con parole sue. Le regole non trovano il sintomo, il modello sì.
    "gp_testo_libero": dict(start=("gp", LOMBARE), llm={LOMBARE: '{"code": "SYM-BACK", "red_flag": false, "lang": "it"}'},
                            expect=[wrote("prescribe", "R:228", procedure="VIS-FIS", priority="B"),
                                    wrote("checkin", "R:234"), llm_read(LOMBARE, 1)]),
    "gp_testo_libero_en": dict(start=("gp", KNEE_EN), llm={KNEE_EN: '<think>knee</think> {"code":"SYM-KNEE","red_flag":false,"lang":"en"}'},
                               expect=[wrote("prescribe", "R:228", procedure="VIS-FIS", priority="D"),
                                       told_text("gp", "I understand your concern", cite="lang en")]),
    # Le regole decidono il protocollo (VIS-FIS/B anche se il modello direbbe SYM-KNEE); il modello
    # viene chiamato una volta sola, per la lingua del cittadino (tools.language).
    "gp_regole_prima": dict(start=BACK, llm={BACK[1]: '{"code": "SYM-KNEE"}'}, expect=[
        wrote("prescribe", "R:228", procedure="VIS-FIS", priority="B"), llm_read(BACK[1], 1)]),
    "gp_red_flag_modello": dict(start=("gp", PECHO), llm={PECHO: '{"code": "NONE", "red_flag": true, "lang": "es"}'},
                                expect=[told("gp", "escalate.emergency", cite="R:242"), no_write("prescribe", "R:442")]),
    # Codice inventato dal modello o Gateway giù: nessuna ricetta, il gp chiede il motivo (mai muto).
    "gp_modello_inventa": dict(start=("gp", LOMBARE), llm={LOMBARE: '{"code": "SYM-HEAD", "red_flag": "no"}'},
                               expect=[no_write("prescribe", "AGENT.md 9"), told_text("gp", "disturbo", cite="R:248")]),
    "gp_modello_lento": dict(start=("gp", LOMBARE), llm={LOMBARE: "timeout"},
                             expect=[no_write("prescribe", "AGENT.md 9"), told_text("gp", "disturbo", cite="R:248")]),
    "gp_senza_modello": dict(start=("gp", LOMBARE), expect=[no_write("prescribe", "AGENT.md 9"),
                                                             told_text("gp", "disturbo", cite="R:248")]),
    # Livello 3, dopo la ricetta: il cittadino riscrive al gp. Mai muto, mai una ricetta di troppo.
    "gp_dopo_grazie": dict(start=BACK, handoff=True, after=[GRAZIE], llm={GRAZIE: NONE_IT}, expect=[
        writes("prescribe", 1, "una sola ricetta"), told_text("gp", "già pronta", cite="R:248"),
        told_text("gp", "centro prenotazioni l'ha già ricevuta", cite="handoff")]),
    "gp_stesso_sintomo": dict(start=BACK, after=["Il mal di schiena non passa, quando mi chiamano?"], expect=[
        writes("prescribe", 1, "stesso motivo"), told_text("gp", "già pronta", cite="R:248")]),
    "gp_secondo_sintomo": dict(start=BACK, handoff=True, after=["Ho anche male al ginocchio, serve un'altra ricetta?"],
                               expect=[writes("prescribe", 2, "motivo nuovo"),
                                       wrote("prescribe", "R:228", procedure="VIS-FIS", priority="D")]),
    "gp_rinnovo_a_parole": dict(start=BACK, handoff=True, after=[RINNOVO], llm={RINNOVO: NONE_IT}, expect=[
        writes("prescribe", 2, "P1 a parole"), asked_ledger("gp", "prescription", "prescription_number=")]),
    "gp_lingua_es": dict(start=("gp", RODILLA), llm={RODILLA: '{"code": "SYM-KNEE", "red_flag": false, "lang": "es"}'},
                         expect=[wrote("prescribe", "R:228", procedure="VIS-FIS", priority="D"),
                                 told_text("gp", "I understand your concern", cite="lingua")]),
    "gp_red_flag_en": dict(start=("gp", "I have a crushing chest pain since this morning"), expect=[
        told_text("gp", "Call 112", cite="R:242"), no_write("prescribe", "R:442")]),
    # Preferenze dette al gp (console, percorsi 0071/0093/0094): confermate e passate al centro.
    "gp_preferenze": dict(start=("gp", PREF_EN), handoff=True, expect=[
        handoff_tokens("lang=en", "doctor_lang=en", "days=mon"),
        told_text("gp", "I have noted your preferences (a doctor who speaks English; Monday)", cite="proattività"),
        wrote("commit", "R:241")]),
    # "Gentile Dottoressa" e "da lunedì" non sono preferenze: niente token inventati (AGENT.md 9).
    "gp_preferenze_falsi": dict(start=("gp", "Gentile Dottoressa, ho mal di schiena da lunedì."), handoff=True, expect=[
        handoff_tokens("lang=it", absent=("gender_preference=", "days=", "doctor_lang=")),
        never_told("gp", "Ho preso nota", cite="AGENT.md 9")]),
    "gp_preferenze_dopo": dict(start=BACK, handoff=True, after=[PREF_DOPO], llm={PREF_DOPO: NONE_IT}, expect=[
        writes("prescribe", 1, "una sola ricetta"), told_text("gp", "(una dottoressa)", cite="promemoria")]),
    # Cross-ASST (R:191-195): il centro di un'altra ASST chiede slot all'ospedale, poi conferma.
    "cross_asst_prenota": dict(
        start=("hospital", XREQ.format(svc="hospital.physiatry", proc="VIS-FIS", rid="R-1")), foreign=True,
        expect=[cross_reply("R-1", "B"), wrote("hold", "R:194", citizen=CIT),
                wrote("commit", "R:194", consent_ref="cons-X-1"), receipt_to_foreign, hospital_to_gp(False),
                *CHECKED_IN]),
    # La sonda della conformance (service=lab, nessun criterio cross-ASST): nessun effetto.
    "cross_asst_sonda": dict(
        start=("hospital", "need.request service=lab summary=visit district=8"), foreign=True,
        expect=[no_world_of("VIS-FIS"), never_from_hospital_to(FOREIGN)]),
    "cross_asst_nessuno": dict(
        start=("hospital", XREQ.format(svc="hospital.physiatry", proc="VIS-FIS", rid="R-2")), foreign=True,
        late_days=15, expect=[cross_reply("R-2", None), no_write("hold", "R:192")]),
    "cross_asst_altra_struttura": dict(
        start=("hospital", XREQ.format(svc="specialist_care.cardiology", proc="VIS-CARD", rid="R-3")),
        foreign=True, expect=[cross_reply("R-3", None), no_world_of("VIS-CARD")]),
    # Orologio in cache: la seconda agenda arriva dopo 5 s, ma l'ospedale non la aspetta.
    "cross_asst_orologio": dict(
        start=("hospital", XFIS.format(rid="R-4", extra="")), foreign_after=[XFIS.format(rid="R-5", extra="")],
        foreign=True, slow_agenda=5.0, expect=[cross_reply("R-4", "B"), cross_reply("R-5", "B"), cross_fast("R-5", 4.0)]),
    # Finestra B (2 giorni) tutta piena: offerte subito dopo, non cancel (alternativa rilassata, R:232).
    "cross_asst_oltre_finestra": dict(
        start=("hospital", XFIS.format(rid="R-6", extra="")), foreign=True, full_days=2,
        expect=[cross_reply("R-6", "B"), told_text_to_foreign("R-6", "subito dopo")]),
    # lang=sq: prima h3, l'unico che parla sq; lang=en: prima h1, ma un h3 resta tra le offerte.
    "cross_asst_lingua_sq": dict(
        start=("hospital", XFIS.format(rid="R-7", extra=" lang=sq")), foreign=True,
        expect=[cross_reply("R-7", "B"), cross_doctors("R-7", "h3")]),
    "cross_asst_lingua_en": dict(
        start=("hospital", XFIS.format(rid="R-8", extra=" lang=en")), foreign=True,
        expect=[cross_reply("R-8", "B"), cross_doctors("R-8", "h1")]),
    # Livello 5: lang=en e gender=f li rispetta solo h1, quindi niente h3 di rinforzo.
    "cross_asst_vincoli": dict(
        start=("hospital", XFIS.format(rid="R-13", extra=" lang=en gender=f")), foreign=True,
        expect=[cross_reply("R-13", "B"), cross_only("R-13", "h1")]),
    # Ledger lento (world dopo 12 s, città 21/11): R-11 a freddo risponde entro 9,5 s e chiude il
    # turno; R-12 arriva 6 s dopo. Con la risposta di world come turno suo (--ledger-turns, come in
    # città) R-12 trova la pagina in memoria e offre in meno di 1 s; senza, R-12 è di nuovo a freddo.
    "cross_asst_world_lento": dict(
        start=("hospital", XFIS.format(rid="R-11", extra="")), foreign_after=[XFIS.format(rid="R-12", extra="")],
        foreign=True, slow_world=12.0, gap=6.0,
        expect=[cross_reply("R-11", None), cross_fast("R-11", 9.5),
                by_turns(cross_reply("R-12", "B"), cross_reply("R-12", None)),
                by_turns(cross_fast("R-12", 1.0), cross_fast("R-12", 9.5))]),
    "cross_asst_tutto_pieno": dict(
        start=("hospital", XFIS.format(rid="R-9", extra="")), foreign=True, full_days=15,
        expect=[cross_reply("R-9", None), no_write("hold", "R:192")]),
    # P2: l'hold porta il consenso cross-ASST, il commit quello della prenotazione.
    "cross_asst_consenso_p2": dict(
        start=("hospital", XFIS.format(rid="R-10", extra="")), foreign=True, xconsent=True,
        expect=[wrote("hold", "P2", consent_ref="xc-X-1"), wrote("commit", "R:194", consent_ref="cons-X-1")]),
}


# --- Ciclo di vita della prenotazione in ospedale (commit unico, re-hold, confirm doppio, annullo)


def hospital_commits(n: int):
    """Commit dell'ospedale, anche rifiutati: il push di held e il turno del confirm non
    committano lo stesso hold due volte (in città: DUPLICATE, poi cancel sbagliati)."""
    def check(b: Bus) -> str | None:
        got = sum(w["op"] == "commit" and w["by"] == "hospital" for w in b.ledger.writes)
        if got != n:
            return f"hospital ha mandato {got} commit invece di {n}"
    return check


def hospital_receipts(n: int):
    """booking.receipt dall'ospedale al centro prenotazioni, tutte con lo stesso booking_id."""
    def check(b: Bus) -> str | None:
        got = [fields(m.text).get("booking_id") for m in b.log if m.sender == b.ids["hospital"]
               and m.recipient == b.ids["booking_centre"] and act(m) == "booking.receipt"]
        if len(got) != n or len(set(got)) != 1:
            return f"hospital ha mandato {len(got)} booking.receipt al centro {got} invece di {n} (confirm doppio)"
    return check


def rehold(b: Bus) -> str | None:
    """Hold rifiutato, poi un confirm nuovo sullo stesso slot: un hold nuovo, uno solo (R:682)."""
    holds = [w for w in b.ledger.writes if w["op"] == "hold" and w["by"] == "hospital"]
    if not holds or not holds[0]["code"]:
        return "il primo hold dell'ospedale non è stato rifiutato"
    slot = holds[0]["f"].get("slot_id")
    again = [w for w in holds[1:] if w["f"].get("slot_id") == slot]
    if len(again) != 1 or again[0]["code"]:
        return f"dopo il rifiuto {len(again)} hold su {slot} invece di uno accettato (re-hold)"


def cancel_told(peer: str, reason: str, n: int):
    def check(b: Bus) -> str | None:
        to = CIT if peer == "citizen" else b.ids[peer]
        got = [m for m in b.log if m.sender == b.ids["hospital"] and m.recipient == to and act(m) == "cancel"
               and f"reason={reason}" in m.text.split()]
        if len(got) != n:
            return f"hospital ha mandato {len(got)} cancel reason={reason} a {peer} invece di {n}"
    return check


def cancelled_not_confirmed(b: Bus) -> str | None:
    """Dopo il cancelled del Ledger l'ospedale non presenta più quella prenotazione come valida."""
    i = next((i for i, m in enumerate(b.log) if m.sender == LEDGER and m.text.startswith("cancelled ")), None)
    if i is None:
        return "nessun cancelled dal Ledger"
    bid = fields(b.log[i].text).get("booking_id", "")
    for m in b.log[i + 1:]:
        if m.sender == b.ids["hospital"] and m.recipient == CIT and act(m) != "cancel" and bid in m.text:
            return f"hospital presenta come valida la prenotazione annullata {bid}: «{m.text[:80]}»"


SCENARIOS.update({
    # Il push di held apre un turno mentre il turno del confirm aspetta la stessa riga.
    "ospedale_commit_unico": dict(start=BACK, race=True, expect=[hospital_commits(1), wrote("checkin", "R:234")]),
    # Il centro rimanda lo stesso confirm: la stessa ricevuta di nuovo, niente hold o commit in più.
    "ospedale_confirm_doppio": dict(start=BACK, dup_confirm=1, expect=[
        writes("hold", 1, "confirm doppio"), hospital_commits(1), hospital_receipts(2)]),
    # Hold rifiutato, poi di nuovo lo stesso confirm: un hold nuovo sullo stesso slot.
    "ospedale_rehold": dict(start=BACK, refuse_holds=1, dup_confirm=1, expect=[rehold, wrote("commit", "R:682")]),
    # Il Ledger annulla la prenotazione: cancel col motivo al centro e al cittadino, una volta;
    # alla domanda d'orario mai più quella prenotazione come confermata.
    "ospedale_annullata": dict(start=BACK, ledger_cancel="facility_closed", expect=[
        cancel_told("booking_centre", "facility_closed", 1), cancel_told("citizen", "facility_closed", 1),
        cancelled_not_confirmed]),
    # Cittadino di un'altra ASST invisibile all'ospedale: il benvenuto non si rimanda (R:701).
    "cross_asst_cittadino_invisibile": dict(
        start=("hospital", XFIS.format(rid="R-11", extra="")), foreign=True, hidden=True,
        expect=[wrote("commit", "R:194"), receipt_to_foreign]),
})


# --- D: ospedale, alternative a uno slot perso, via nel benvenuto, lingua del partner, rimbalzi
# (_BOUNCED) per caso e a tempo, rifiuto tardivo di un hold doppio -----------------------------


def lost_slot_alternatives(rid: str):
    """Hold rifiutato NO_CAPACITY su un'offerta cross-ASST: cancel col motivo al centro dell'altra
    ASST, poi da 1 a 3 offerte nuove con request_id e soli campi del Ledger; mai lo slot perso
    (tolto anche dalla pagina in memoria) né uno già offerto."""
    def check(b: Bus) -> str | None:
        hosp = b.ids["hospital"]
        sent = [m for m in b.log if m.sender == hosp and m.recipient == FOREIGN]
        lost = next((w["f"].get("slot_id") for w in b.ledger.writes if w["op"] == "hold" and w["code"]), None)
        i = next((i for i, m in enumerate(sent) if act(m) == "cancel" and "reason=NO_CAPACITY" in m.text.split()), None)
        if lost is None or i is None:
            return "nessun hold rifiutato o nessun cancel reason=NO_CAPACITY al centro dell'altra ASST"
        before = {o.get("slot_id") for m in sent[:i] for o in parse_offers(m.text)}
        after = [o for m in sent[i + 1:] if act(m) == "offer" and f"request_id={rid}" in m.text.split()
                 for o in parse_offers(m.text)]
        if not 1 <= len(after) <= 3:
            return f"{len(after)} offerte alternative dopo il cancel invece di 1-3"
        for o in after:
            s = b.ledger.slots.get(o.get("slot_id", ""))
            if o.get("slot_id") == lost or o.get("slot_id") in before:
                return f"alternativa {o.get('slot_id')} già persa o già offerta"
            if s is None or s["facility"] != hosp or o.get("request_id") != rid:
                return f"alternativa non dell'ospedale o senza request_id={rid}: {o}"
            for k, v in o.items():
                if k not in ("offer_id", "request_id", "priority") and (k not in SLOT_KEYS or str(s[SLOT_KEYS[k]]) != v):
                    return f"attributo {k}={v} non dal Ledger (R:243)"
    return check


def lost_not_offered(rid: str):
    """Dopo un NO_CAPACITY la pagina in memoria non offre più lo slot perso a un'altra richiesta."""
    def check(b: Bus) -> str | None:
        lost = next((w["f"].get("slot_id") for w in b.ledger.writes if w["op"] == "hold" and w["code"]), None)
        got = [o.get("slot_id") for m in b.log if m.sender == b.ids["hospital"] and m.recipient == FOREIGN
               and f"request_id={rid}" in m.text.split() for o in parse_offers(m.text)]
        if not got:
            return f"nessuna offerta per {rid}"
        if lost in got:
            return f"{rid}: offerto di nuovo lo slot perso {lost} dalla pagina in memoria"
    return check


def silent_after_refused_hold(b: Bus) -> str | None:
    hold = next((w for w in b.ledger.writes if w["op"] == "hold" and w["code"]), None)
    if hold is None:
        return "nessun hold rifiutato"
    got = [m for m in b.log[hold["i"]:] if m.sender == b.ids["hospital"] and m.recipient == FOREIGN]
    if got:
        return f"hospital ha scritto al centro dopo un hold rifiutato DUPLICATE: «{got[0].text[:80]}»"


def late_refusal_no_cancel(b: Bus) -> str | None:
    """Il Ledger rifiuta un secondo hold sullo stesso slot dopo il held del primo (secondo pod,
    Ledger lento): la prenotazione resta, nessun cancel a nessuno."""
    hosp = b.ids["hospital"]
    refused = next((i for i, m in enumerate(b.log) if m.sender == LEDGER and m.recipient == hosp
                    and m.text.startswith("refused hold")), None)
    if refused is None:
        return "nessun refused hold arrivato all'ospedale"
    got = [m for m in b.log[refused:] if m.sender == hosp and act(m) == "cancel"]
    if got:
        return f"cancel falso dopo il rifiuto tardivo: «{got[0].text[:80]}»"


SCENARIOS.update({
    # Slot perso (NO_CAPACITY) su un'offerta cross-ASST: cancel, poi altre date dalla pagina in memoria.
    "ospedale_alternative": dict(
        start=("hospital", XFIS.format(rid="R-20", extra="")), foreign=True, refuse_holds=1,
        refuse_code="NO_CAPACITY", foreign_after=[(XFIS.format(rid="R-25", extra=""), WALKIN)],
        expect=[lost_slot_alternatives("R-20"), lost_not_offered("R-25")]),
    # DUPLICATE: niente perso, niente cancel né alternative.
    "ospedale_alternative_duplicate": dict(
        start=("hospital", XFIS.format(rid="R-21", extra="")), foreign=True, refuse_holds=1,
        refuse_code="DUPLICATE", expect=[silent_after_refused_hold]),
    # Proattività: il benvenuto dice la via dell'ospedale (city.address).
    "welcome_indirizzo": dict(start=BACK, address="Via Esempio 1", expect=[
        told_text("hospital", "in Via Esempio 1", cite="proattività: indirizzo"), *CHECKED_IN]),
    # Il centro di un'altra ASST conferma con lang=en: benvenuto in inglese.
    "welcome_lang_en_partner": dict(
        start=("hospital", XFIS.format(rid="R-22", extra="")), foreign=True, confirm_lang="en",
        expect=[told_text("hospital", "Hello, this is the hospital", cite="lang=en dal partner")]),
    # Ospedale giù per il primo invio (agenda di R-23): un caso dopo (R-24) l'agenda riparte e
    # l'offerta arriva subito, non dopo l'attesa a freddo.
    "ospedale_giu_poi_su": dict(
        start=("hospital", XFIS.format(rid="R-23", extra="")), foreign=True, bounce=1,
        foreign_after=[(XFIS.format(rid="R-24", extra=""), WALKIN)],
        expect=[cross_reply("R-23", "B"), cross_reply("R-24", "B"), cross_fast("R-24", 4.0),
                asked_ledger("hospital", "agenda")]),
    # Ledger lento o secondo pod: un refused hold arriva dopo il held dello stesso slot.
    "ospedale_rifiuto_tardivo": dict(start=BACK, late_refusal=True, expect=[
        late_refusal_no_cancel, wrote("commit", "R:241"), *CHECKED_IN]),
    # Una say dell'ospedale va in timeout (Relay oltre 10 s): il turno non si interrompe a metà.
    "ospedale_say_timeout": dict(start=BACK, say_timeout=1, expect=[wrote("commit", "R:241"), *CHECKED_IN]),
})


# --- Cross-ASST: offerte distribuite e ack subito (ospedale 1.0.8 in città: 80 offerte in 11 minuti
# sugli stessi 3 slot, 3 confirm su 5 rifiutati NO_CAPACITY; ogni risposta dalla cache con ack a 12-20 s)


def _hospital_hold(b: Bus) -> str | None:
    return next((w["f"].get("slot_id") for w in b.ledger.writes
                 if w["op"] == "hold" and w["by"] == "hospital" and not w["code"]), None)


def cross_spread(*rids: str):
    """Dopo l'hold dell'ospedale quello slot non viene più offerto, e le richieste ``rids``
    ricevono più di 3 slot diversi (non sempre gli stessi 3)."""
    def check(b: Bus) -> str | None:
        hold = _hospital_hold(b)
        if hold is None:
            return "nessun hold accettato dell'ospedale"
        got = {}
        for rid in rids:
            m = next((m for m in b.log if m.sender == b.ids["hospital"] and m.recipient == FOREIGN
                      and f"request_id={rid}" in m.text.split()), None)
            got[rid] = [o.get("slot_id") for o in parse_offers(m.text)] if m else []
        again = [rid for rid, slots in got.items() if hold in slots]
        if again:
            return f"{hold}, già in hold, offerto di nuovo a {again}"
        if len(set().union(*got.values()) - {hold}) <= 3:
            return f"offerte {got}: sempre gli stessi slot"
    return check


def cache_after_hold(b: Bus) -> str | None:
    """--ledger-turns: la risposta di world letta dopo l'hold arriva in _WORLD nel suo turno."""
    if not b.ledger_turns:
        return None
    hold = _hospital_hold(b)
    page = b.agents[b.ids["hospital"]].mods["facility"]._WORLD.get(("VIS-FIS", 1))
    line = next((l.split() for l in page[1].splitlines() if f"slot_id={hold}" in l.split()), []) if page else None
    if line is None or {"free=1", "capacity=1"} & set(line):
        return f"pagina di world in memoria vecchia: {hold} ancora libero dopo l'hold"


def cross_turn_ends(rid: str, nxt: str):
    """Il +1 della risposta veloce si misura sull'ack, che parte a fine turno (R:246): dopo la
    risposta a ``rid`` il turno dell'ospedale finisce subito e la richiesta ``nxt`` parte entro 1 s."""
    def check(b: Bus) -> str | None:
        got = next((m for m in b.log if m.sender == b.ids["hospital"] and m.recipient == FOREIGN
                    and f"request_id={rid}" in m.text.split()), None)
        req = next((m for m in b.log if m.sender == FOREIGN and f"request_id={rid}" in m.text.split()), None)
        asked = next((m for m in b.log if m.sender == FOREIGN and f"request_id={nxt}" in m.text.split()), None)
        if got is None or asked is None or req is None:
            return f"risposta a {rid} o richiesta {nxt} mancante"
        # The turn's own duration (b.took): the bus also waits for slow Ledger replies before nxt.
        took = b.took[req.msg_id] - (b.stamp[got.msg_id] - b.stamp[req.msg_id]) if req.msg_id in b.took else (
            b.stamp[asked.msg_id] - b.stamp[got.msg_id])
        if took >= 1.0:
            return f"turno dell'ospedale ancora aperto {took:.1f} s dopo la risposta a {rid}: ack in ritardo"
    return check


XFIS_D = XFIS.replace("priority=B", "priority=D")  # finestra di 5 giorni: 9 slot liberi in pagina 1

SCENARIOS.update({
    # R-20 a freddo, il centro conferma la prima offerta (hold); R-21..R-23 dalla cache.
    "cross_asst_distribuite": dict(
        start=("hospital", XFIS_D.format(rid="R-20", extra="")), foreign=True,
        foreign_after=[XFIS_D.format(rid=r, extra="") for r in ("R-21", "R-22", "R-23")],
        expect=[*(cross_reply(r, "D") for r in ("R-20", "R-21", "R-22", "R-23")),
                *(cross_fast(r, 1.0) for r in ("R-21", "R-22", "R-23")),
                cross_spread("R-21", "R-22", "R-23"), cache_after_hold]),
    # world dopo 3 s: R-31 risponde dalla cache e chiude il turno senza aspettare la sua lettura.
    "cross_asst_ack_subito": dict(
        start=("hospital", XFIS.format(rid="R-30", extra="")), foreign=True, slow_world=3.0,
        foreign_after=[XFIS.format(rid=r, extra="") for r in ("R-31", "R-32")],
        expect=[cross_reply("R-31", "B"), cross_fast("R-31", 1.0), cross_turn_ends("R-31", "R-32"),
                cross_reply("R-32", "B"), cross_fast("R-32", 1.0)]),
})


# --- E: scritture lente sul Ledger, il turno del confirm si chiude prima di 10 s (ack, R:246)


def slow_booking(b: Bus) -> str | None:
    """Il turno del confirm dell'ospedale finisce in meno di 10 s (l'harness manda l'ack a fine
    turno), poi il push di held committa e quello di committed manda la ricevuta, una volta."""
    hosp, bc = b.ids["hospital"], b.ids["booking_centre"]
    took = [round(b.took[m.msg_id], 1) for m in b.log
            if m.sender == bc and m.recipient == hosp and m.text.startswith("confirm") and m.msg_id in b.took]
    if not took or max(took) >= 10:
        return f"turno del confirm dell'ospedale {took} s, serve < 10 s (ack, R:246)"
    for to, head, push in ((LEDGER, "commit", "held"), (bc, "booking.receipt", "committed")):
        sent = [m for m in b.log if m.sender == hosp and m.recipient == to and m.text.startswith(head + " ")]
        opened = [b.opened[b.turn_of[m.msg_id]].text.split()[0] for m in sent]
        if opened != [push]:
            return f"hospital: {head} nei turni aperti da {opened} invece che una volta dal push di {push}"


SCENARIOS.update({
    # Hold e commit rispondono dopo 25 s (città 21/11: ~30 s), ogni lettura della history 0,5 s.
    # Solo con --ledger-turns: senza push di held e committed nessuno finirebbe la prenotazione.
    "ospedale_scritture_lente": dict(start=BACK, slow_writes=25.0, pull_pause=0.5, ledger_turns=True,
                                     expect=[slow_booking, hospital_commits(1), wrote("checkin", "R:234")]),
})


# --- F: hold scaduto, check-in con codice annullato, annullo chiesto dal centro prenotazioni


def released_once(b: Bus) -> str | None:
    """Il centro annulla per il cittadino: un solo `cancel hold_id=` dell'ospedale sul Ledger,
    poi `cancelled` al centro (prima il Ledger). Mai `cancel` al centro: gli farebbe riofferire."""
    hosp, bc = b.ids["hospital"], b.ids["booking_centre"]
    sent = [w for w in b.ledger.writes if w["op"] == "cancel" and w["by"] == "hospital"]
    if len(sent) != 1 or sent[0]["code"] or not sent[0]["f"].get("hold_id"):
        return f"hospital ha mandato {len(sent)} cancel sul Ledger invece di un `cancel hold_id=` accettato"
    after = [m for m in b.log[sent[0]["i"] + 1:] if m.sender == hosp and m.recipient == bc]
    if not any(m.text.startswith("cancelled ") for m in after):
        return "hospital non ha confermato `cancelled` al centro dopo il Ledger"
    if any(m.sender == hosp and m.recipient == bc and act(m) == "cancel" for m in b.log):
        return "hospital ha mandato `cancel` al centro che aveva annullato lui"


def checkin_cancelled(b: Bus) -> str | None:
    """La risposta al check-in con il codice annullato dice che è annullata (non «controlli il codice»)."""
    hosp = b.ids["hospital"]
    q = max((i for i, m in enumerate(b.log) if m.sender == CIT and m.recipient == hosp and m.text.startswith("checkin")), default=None)
    reply = next((m for m in b.log[q + 1:] if m.sender == hosp and m.recipient == CIT), None) if q is not None else None
    if reply is None or "annullata" not in reply.text or act(reply) == "checkin":
        return f"hospital non ha detto che la prenotazione è annullata: «{reply.text[:80] if reply else ''}»"


SCENARIOS.update({
    # Il commit trova l'hold scaduto (push `expired`): cancel reason=expired al centro, una
    # volta; il centro riofferta e la visita si fa lo stesso (R:682).
    "ospedale_hold_scaduto": dict(start=BACK, push_expired=1, expect=[
        cancel_told("booking_centre", "expired", 1), wrote("checkin", "R:682")]),
    # Check-in con il codice di una prenotazione annullata dal Ledger: lo dice, niente checkin.
    "ospedale_checkin_annullata": dict(
        start=BACK, ledger_cancel="facility_closed", after_cancel="checkin booking_id={hold_id} ledger_ref={ledger_ref}",
        expect=[no_write("checkin", "prenotazione annullata"), checkin_cancelled]),
    # Il centro annulla per il cittadino: prima il Ledger, poi la conferma al centro.
    "ospedale_annullo_dal_centro": dict(start=BACK, booking_cancel="citizen_request", expect=[released_once]),
})


def transcript(b: Bus) -> None:
    for m in b.log:
        text = m.text or f"<{m.type}> {dict(m.body)}"
        print(f"    {b.name(m.sender):>17} -> {b.name(m.recipient):<17} {text.replace(chr(10), ' | ')[:260]}")
    for s, t, text in b.bounced:
        print(f"    {b.name(s):>17} -x {b.name(t):<17} TARGET_NOT_VISIBLE: {text[:200]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-k", help="solo gli scenari il cui nome contiene questo testo")
    ap.add_argument("-v", action="store_true", help="stampa il trascritto")
    ap.add_argument("--ledger-turns", action="store_true", help="la risposta del Ledger apre anche un turno")
    ap.add_argument("--ledger-format", choices=("kit", "sdk"), default="kit",
                    help="sdk: risposta a world nel formato dello schema del Ledger")
    args = ap.parse_args()
    if args.ledger_format == "sdk":
        REPLY.update(SDK_REPLY)
    for k in ("CITY_ENV", "TEAM_MEMORY"):
        os.environ.pop(k, None)
    failed = 0
    for name, sc in SCENARIOS.items():
        if args.k and args.k not in name:
            continue
        if sc.get("ledger_turns") and not args.ledger_turns:  # vale solo con i push del Ledger
            print(f"SKIP {name}")
            continue
        bus = Bus(sc, args.ledger_turns)
        asyncio.run(bus.run(sc["start"]))
        errs = list(dict.fromkeys(general(bus) + [e for e in (c(bus) for c in sc["expect"]) if e]))
        failed += bool(errs)
        print(f"{'FAIL' if errs else 'PASS'} {name}" + "".join(f"\n     - {e}" for e in errs))
        if args.v:
            transcript(bus)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
