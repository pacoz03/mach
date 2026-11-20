"""Percorsi multi-agente in locale: i 5 brain veri in un processo, bus in memoria, Ledger finto.

    ~/.local/share/uv/tools/city-cli/bin/python -B tests/journey.py [-k nome] [-v] [--ledger-turns]

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
    "agenda": "agenda agenda_now={now} agenda_day={day}",
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
DOCTOR_ID = "D-{key}"  # i brain usano l'ultimo pezzo dopo '-' come chiave di _DOCTORS
# Medici e giorni copiati da _DOCTORS dei brain, procedure dal PROTOCOL del gp: anche questi
# vanno confrontati con il `world` vero.
STAFF = {
    "hospital": ("VIS-FIS", {"h1": "mon tue wed", "h2": "wed thu fri", "h3": "tue thu"}),
    "specialist_care": ("VIS-CARD", {"s1": "mon wed fri", "s2": "tue thu"}),
    "family_counseling": ("CONS-GIN", {"c1": "mon tue wed thu fri"}),
}
PAGE = 10
# ----------------------------------------------------------------------------------------------

REPO = Path(__file__).resolve().parent.parent
FOLDERS = ("gp", "booking_centre", "hospital", "specialist_care", "family_counseling")
FACILITIES = {"hospital", "specialist_care", "family_counseling"}
AGENT_DIRS = {str(REPO / f / "agent") for f in FOLDERS}
START = datetime(2026, 11, 23, 8, tzinfo=UTC)  # lunedì 08:00 di agenda
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
CIT, NEED = "citizen:T-1", "N-2026-11-23-0001"
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


def act(m: Message) -> str:
    return understood(m).type


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
        self.now = START
        self.rules = {"P1": "off", "P2": "off", "P3": "off", **sc.get("rules", {})}
        self.folder = {v: k for k, v in ids.items()}
        self.slots = {s["slot_id"]: s for s in make_slots(ids)}
        self.rx: dict[str, dict] = {}
        self.holds: dict[str, dict] = {}
        self.checkins: set[str] = set()
        self.consents: set[tuple[str, str]] = set()  # (purpose, consent_ref) dati dal cittadino
        self.writes: list[dict] = []
        self.refuse_holds = sc.get("refuse_holds", 0)
        self.n = 0

    def advance(self, days: int) -> None:
        self.now += timedelta(days=days)

    def expired(self, number: str | None) -> bool:
        r = self.rx.get(number or "")
        return bool(r) and self.rules["P1"] == "on" and self.now - r["issued"] > timedelta(days=2)

    def taken(self, slot_id: str) -> bool:
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
            return REPLY["agenda"].format(now=self.now.isoformat(), day=DAYS[self.now.weekday()])
        if op == "rules":
            return REPLY["rules"].format(**self.rules)
        if op == "world":
            rows = sorted((s for s in self.slots.values()
                           if f.get("procedure", s["procedure"]) == s["procedure"]
                           and f.get("facility", s["facility"]) == s["facility"]),
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
            r = b.add(LEDGER, self.me, {"text": b.ledger.answer(self.me, need_id, m.text, len(b.log) - 1)},
                      need_id, conversation_id)
            if b.ledger_turns:
                b.queue.append(r)
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


class Citizen:
    def __init__(self, bus: "Bus", sc: dict) -> None:
        self.bus, self.sc = bus, sc
        self.rx, self.chosen, self.receipts, self.waited = "", None, set(), False

    def say(self, to: str, text: str) -> None:
        if to not in self.bus.down:  # ponytail: il cittadino non riprova verso una struttura giù
            self.bus.queue.append(self.bus.add(CIT, to, {"text": text}))

    def react(self, m: Message) -> None:
        b, kind, f = self.bus, act(m), fields(m.text)
        if kind == "prescription" and f.get("prescription_number", self.rx) != self.rx:
            self.rx = f["prescription_number"]
            if self.sc.get("wait_days") and not self.waited:  # P1: chiama il centro giorni dopo
                self.waited = True
                b.ledger.advance(self.sc["wait_days"])
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
            if purpose in self.sc.get("refuse", ()):
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
            self.say(fac, f"checkin booking_id={f['booking_id']} ledger_ref={f.get('ledger_ref', '')}")


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

    def name(self, party: str) -> str:
        return self.agents[party].folder if party in self.agents else party

    def add(self, sender, to, body, need=NEED, conv=None) -> Message:
        m = Message(type="chat", sender=sender, recipient=to, msg_id=f"m-{len(self.log)}",
                    need_id=need, conversation_id=conv, body=body)
        self.log.append(m)
        self.turn_of[m.msg_id] = self.turn
        return m

    async def deliver(self, m: Message) -> None:
        a = self.agents[m.recipient]
        if a.main is None:
            self.crashes.append(f"{a.folder}: import fallito: {a.error}")
            return
        port = Port(self, a.id)
        city = City(index=port, messages=port, history=port, agent_id=a.id, inbound=m)
        with a.active():
            try:
                await a.main(Need(summary=m.text, message=m), city)
            except OutOfScope:
                self.oos.add(m.msg_id)
            except Exception:
                self.crashes.append(f"crash {a.folder}: {traceback.format_exc().strip().splitlines()[-1]}")

    async def run(self, start: tuple[str, str]) -> None:
        self.citizen.say(self.ids[start[0]], start[1])
        while self.queue and self.turn < MAX_TURNS:
            m = self.queue.popleft()
            self.turn += 1
            if m.recipient == CIT:
                self.citizen.react(m)
            elif m.recipient in self.agents and m.recipient not in self.down:
                await self.deliver(m)


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
        if m.recipient != CIT:
            continue
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


BACK = ("gp", "Ho mal di schiena da una settimana, vorrei farmi vedere.")
SCENARIOS: dict[str, dict] = {
    "felice_fisiatria": dict(start=BACK, expect=[
        wrote("prescribe", "R:228", procedure="VIS-FIS", priority="B"), wrote("checkin", "R:234")]),
    "cardiologia_referto": dict(start=("gp", "Sento le palpitazioni da qualche giorno."), expect=[
        wrote("prescribe", "R:228", procedure="VIS-CARD", priority="B"), wrote("checkin", "R:234"),
        wrote("report", "R:208/236", consent_ref=f"cons-{NEED}-report_sharing")]),
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
}


def transcript(b: Bus) -> None:
    for m in b.log:
        print(f"    {b.name(m.sender):>17} -> {b.name(m.recipient):<17} {m.text.replace(chr(10), ' | ')[:260]}")
    for s, t, text in b.bounced:
        print(f"    {b.name(s):>17} -x {b.name(t):<17} TARGET_NOT_VISIBLE: {text[:200]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-k", help="solo gli scenari il cui nome contiene questo testo")
    ap.add_argument("-v", action="store_true", help="stampa il trascritto")
    ap.add_argument("--ledger-turns", action="store_true", help="la risposta del Ledger apre anche un turno")
    args = ap.parse_args()
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
