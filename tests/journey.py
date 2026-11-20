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
        self.refuse_checkin = sc.get("refuse_checkin")
        self.full = self.now + timedelta(days=sc.get("full_days", 0))  # slot prima di qui: pieni
        self.slow_agenda, self.agendas = sc.get("slow_agenda", 0), 0  # secondi, dalla 2a agenda
        self.n = 0

    def advance(self, days: int) -> None:
        self.now += timedelta(days=days)

    def expired(self, number: str | None) -> bool:
        r = self.rx.get(number or "")
        return bool(r) and self.rules["P1"] == "on" and self.now - r["issued"] > timedelta(days=2)

    def late(self, text: str) -> float:
        if (text.split() or [""])[0] != "agenda":
            return 0
        self.agendas += 1
        return self.slow_agenda if self.agendas > 1 else 0

    def taken(self, slot_id: str) -> bool:
        if datetime.fromisoformat(self.slots[slot_id]["at"]) < self.full:
            return True
        return any(h["slot_id"] == slot_id and h["state"] != "cancelled" for h in self.holds.values())

    def answer(self, by: str, need: str | None, text: str, i: int) -> str:
        op = (text.split() or [""])[0]
        f = fields(text)
        if op not in self.WRITES:
            return self.read(op, f)
        code, reply, injected = self.write(op, by, need, f)
        self.writes.append(dict(i=i, by=self.folder.get(by, by), op=op, f=f, code=code, injected=injected))
        if code is None:
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
                return "SLOT_TAKEN", "", True
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
        if op == "commit":
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
        if to in b.down or self.me in b.down:
            b.bounced.append((self.me, to, body.get("text", "")))
            raise CapError("TARGET_NOT_VISIBLE", to)
        m = b.add(self.me, to, body, need_id, conversation_id)
        if to == LEDGER:  # la risposta è subito nel log: il polling nel turno la vede
            text = b.ledger.answer(self.me, need_id, m.text, len(b.log) - 1)
            if delay := b.ledger.late(m.text):  # sc["slow_agenda"]: arriva dopo, come in città
                asyncio.get_running_loop().call_later(delay, b.reply, self.me, text, need_id, conversation_id)
            else:
                b.reply(self.me, text, need_id, conversation_id)
        else:
            b.queue.append(m)
        return m.msg_id

    async def history(self, *, peer, need_id):
        return [m for m in self.bus.log if self.me in (m.sender, m.recipient)
                and (peer is None or peer in (m.sender, m.recipient))
                and (need_id is None or m.need_id == need_id)]

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

    def name(self, party: str) -> str:
        return self.agents[party].folder if party in self.agents else party

    def add(self, sender, to, body, need=NEED, conv=None, kind="chat") -> Message:
        lang = self.citizen.sc.get("confirm_lang")  # il centro manda lang= nel confirm all'ospedale
        if (lang and sender == self.ids["booking_centre"] and to == self.ids["hospital"]
                and str(body.get("text", "")).startswith("confirm")):
            body = {**body, "text": f"{re.sub(r' ?lang=\S+', '', body['text'])} lang={lang}"}
        m = Message(type=kind, sender=sender, recipient=to, msg_id=f"m-{len(self.log)}",
                    need_id=need, conversation_id=conv, body=body)
        self.log.append(m)
        self.turn_of[m.msg_id] = self.turn
        self.stamp[m.msg_id] = time.monotonic()
        return m

    def reply(self, to: str, text: str, need: str | None, conv: str | None) -> None:
        r = self.add(LEDGER, to, {"text": text}, need, conv)
        if self.ledger_turns:
            self.queue.append(r)

    async def deliver(self, m: Message) -> None:
        a = self.agents[m.recipient]
        if a.main is None:
            self.crashes.append(f"{a.folder}: import fallito: {a.error}")
            return
        port = Port(self, a.id)
        city = City(index=port, messages=port, history=port, agent_id=a.id, inbound=m, llm=self.llm)
        with a.active():
            try:
                await a.main(Need(summary=m.text, message=m), city)
            except OutOfScope:
                self.oos.add(m.msg_id)
            except Exception:
                self.crashes.append(f"crash {a.folder}: {traceback.format_exc().strip().splitlines()[-1]}")

    async def run(self, start: tuple[str, str]) -> None:
        # sc["after"]: messaggi che il cittadino scrive al gp a percorso fermo (livello 3: grazie,
        # domande, un secondo disturbo, un rinnovo chiesto a parole).
        # sc["foreign_after"]: altre richieste del centro dell'altra ASST all'ospedale.
        for to, text in (start, *(("gp", t) for t in self.citizen.sc.get("after", ())),
                         *(("hospital", t) for t in self.citizen.sc.get("foreign_after", ()))):
            if self.citizen.sc.get("foreign"):  # cross-ASST: apre il centro di un'altra ASST
                self.queue.append(self.add(FOREIGN, self.ids[to], {"text": text}))
            else:
                self.citizen.say(self.ids[to], text)
            while self.queue and self.turn < MAX_TURNS:
                m = self.queue.popleft()
                self.turn += 1
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
        told("gp", "escalate.emergency", cite="R:242"), no_write("prescribe", "R:442")]),
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
        handed_over, wrote("commit", "R:241"), told("booking_centre", "booking.receipt", cite="R:241")]),
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
        expect=[cross_reply("R-8", "B"), cross_doctors("R-8", "h1", "h3")]),
    "cross_asst_tutto_pieno": dict(
        start=("hospital", XFIS.format(rid="R-9", extra="")), foreign=True, full_days=15,
        expect=[cross_reply("R-9", None), no_write("hold", "R:192")]),
    # P2: l'hold porta il consenso cross-ASST, il commit quello della prenotazione.
    "cross_asst_consenso_p2": dict(
        start=("hospital", XFIS.format(rid="R-10", extra="")), foreign=True, xconsent=True,
        expect=[wrote("hold", "P2", consent_ref="xc-X-1"), wrote("commit", "R:194", consent_ref="cons-X-1")]),
}


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
