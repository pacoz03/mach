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
from httpx import ReadTimeout  # noqa: E402  (quello che solleva city.say quando il Relay supera 10 s)

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
# La riga di slot vera (episodes, 21/11 02:33) ha gli stessi campi del kit: at/day/free.
REAL_SLOT = REPLY["slot"]
DOCTOR_ID = "D-{key}"  # i brain usano l'ultimo pezzo dopo '-' come chiave di _DOCTORS
# Medici e giorni copiati da _DOCTORS dei brain, procedure dal PROTOCOL del gp: anche questi
# vanno confrontati con il `world` vero.
STAFF = {
    "hospital": ("VIS-FIS", {"h1": "mon tue wed", "h2": "wed thu fri", "h3": "tue thu"}),
    "specialist_care": ("VIS-CARD", {"s1": "mon wed fri", "s2": "tue thu"}),
    "family_counseling": ("CONS-GIN", {"c1": "mon tue wed thu fri"}),
}
PAGE = 10
# Rifiuto vero di un checkin fuori dalla mezza giornata dello slot (R:91; tests/fixtures/refused.txt).
CHECKIN_REFUSED = "POLICY_VIOLATION detail=outside"
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
        # late_days: slot tutti passati; late_hours: l'agenda è già avanti di qualche ora
        self.now = START + timedelta(days=sc.get("late_days", 0), hours=sc.get("late_hours", 0))
        # sc["agenda_lag"]: secondi reali di età della lettura di agenda quando arriva (città 21/11:
        # una risposta di 26 minuti prima, cioè 26 ore di agenda): agenda_now ed epoch_real indietro.
        self.agenda_lag = sc.get("agenda_lag", 0)
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
        # slot prima di qui: pieni. Senza late_hours: world elenca ancora gli slot di stamattina
        # come liberi (città 21/11: from=data di oggi), come quelli già passati.
        self.full = START + timedelta(days=sc.get("late_days", 0) + sc.get("full_days", 0))
        self.slow_agenda, self.agendas = sc.get("slow_agenda", 0), 0  # secondi, dalla 2a agenda
        self.slow_world = sc.get("slow_world", 0)  # secondi, ogni world (città 21/11: ~12 s)
        self.slow_writes = sc.get("slow_writes", 0)  # secondi, risposta a hold e commit (21/11: ~30 s)
        self.slow_prescribe = sc.get("slow_prescribe", 0)  # secondi, risposta a prescribe (21/11: minuti)
        self.push_expired = sc.get("push_expired", 0)  # commit che arrivano a hold già scaduto
        self.steal = sc.get("steal")  # slot preso da un'altra ASST subito dopo la prima risposta a world
        self.flipped: set[str] = set()  # regole accese a metà scenario (sc["rule_at"]): i loro rifiuti sono iniettati
        self.pods = bool(sc.get("pods"))  # due pod senza lock comune: un commit doppio (DUPLICATE) è la corsa iniettata
        # sc["reserve_marked"]: world marca la riserva U anche con P3 spenta (città 21/11: rules count=0)
        self.reserve_marked = sc.get("reserve_marked", False)
        self.n = 0

    def advance(self, days: int) -> None:
        self.now += timedelta(days=days)

    def expired(self, number: str | None) -> bool:
        r = self.rx.get(number or "")
        return bool(r) and self.rules["P1"] == "on" and self.now - r["issued"] > timedelta(days=2)

    def late(self, text: str) -> float:
        if (text.split() or [""])[0] in ("hold", "commit"):
            return self.slow_writes
        if (text.split() or [""])[0] == "prescribe":
            return self.slow_prescribe
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
                return "SLOT_TAKEN", "", s["slot_id"] == self.steal
            # Il Ledger vero rifiuta la riserva U sull'hold (episodes: refused op=hold code=NO_CAPACITY).
            if (self.rules["P3"] == "on" and s["reserve"]
                    and self.rx.get(f.get("prescription_number", ""), {}).get("priority") != "U"):
                return "NO_CAPACITY", "", "P3" in self.flipped
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
                return "DUPLICATE", "", self.pods
            if h is not None and h["state"] == "expired":  # sc["expire_holds"]: il commit arriva tardi
                return "EXPIRED", "", True
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
        if op == "agenda":  # rate 60: agenda_lag secondi reali = agenda_lag minuti di agenda
            at = self.now - timedelta(minutes=self.agenda_lag)
            return REPLY["agenda"].format(now=at.isoformat(), day=DAYS[at.weekday()],
                                          real=(datetime.now(UTC) - timedelta(seconds=self.agenda_lag)).isoformat())
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
                marked = self.rules["P3"] == "on" or self.reserve_marked
                lines.append(line + (" urgent_reserve=true" if marked and s["reserve"] else ""))
            if self.steal in self.slots:  # sc["steal"]: un'altra ASST lo conferma appena dopo questa risposta
                self.holds.setdefault("H-ALTRA", dict(hold_id="H-ALTRA", slot_id=self.steal, need_id=None, state="committed"))
            return "\n".join(lines)
        return REPLY["refused"].format(op=op or "?", code="UNKNOWN_OP")


def real_page(L: Ledger) -> str:
    """Pagina 1 di world VIS-FIS nel formato vero (episodes, 21/11 02:33): intestazione con page,
    matching, asst, from, until e wave_id; una riga free=0; urgent_reserve=true; un secondo page=
    per la pagina dopo. In episodes ci sono solo i campi: la riga di quel page=2 è un'ipotesi."""
    rows = sorted((s for s in L.slots.values() if s["procedure"] == "VIS-FIS"), key=lambda s: (s["at"], s["slot_id"]))
    head = (f"slots page=1/{-(-len(rows) // PAGE)} matching={len(rows)} asst=asst-gorla from={L.now.date()} "
            f"until={(L.now + timedelta(days=14)).date()} wave_id=W01QQQ")
    lines = [REAL_SLOT.format(**s, free=0 if i == 0 else 1) + (" urgent_reserve=true" if s["reserve"] else "")
             for i, s in enumerate(rows[:PAGE])]
    return "\n".join([head, *lines, "more: world procedure=VIS-FIS page=2"])


class Agent:
    """Un brain caricato per percorso, con le SUE copie dei moduli fratelli (tools, facility...)."""

    def __init__(self, folder: str) -> None:
        self.folder, self.dir, self.card = folder, REPO / folder / "agent", REPO / folder / "card.yaml"
        card = self.card.read_text(encoding="utf-8")
        self.id = re.search(r"^agent_id:\s*(\S+)", card, re.M).group(1)
        self.role = (re.search(r"^role:\s*(\w+)", card, re.M) or [None, ""])[1]
        self.caps = tuple(re.findall(r"^\s+-\s+([\w.]+)\s*$", card.split("capabilities:", 1)[-1].split("\n\n")[0], re.M))
        # Moduli fratelli e pacchetto mach/ (mach, mach.protocol): ognuno con le sue copie e il suo tools.
        self.sibs = {".".join(p.relative_to(self.dir).with_suffix("").parts).removesuffix(".__init__")
                     for p in self.dir.rglob("*.py")} - {"agent"}
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
                if hasattr(m, "_PARTNER_ROUND"):  # il giro dai partner (90 s, U 45 s) nemmeno: qui i partner tacciono
                    m._PARTNER_ROUND, m._PARTNER_ROUND_U = 0.3, 0.1
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
        op = (str(body.get("text", "")).split() or [""])[0]
        # sc["say_delay"]: secondi per ogni city.say (o {op: secondi}); il Relay vero ci mette 1-5 s.
        delay = b.say_delay if isinstance(b.say_delay, (int, float)) else b.say_delay.get(op, 0)
        if delay:
            await asyncio.sleep(delay)
        if op and op == b.timeout_on:  # sc["say_timeout_on"]: la prima say con questa parola d'atto va oltre 10 s
            b.timeout_on = None
            raise ReadTimeout("relay send")
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
                if b.expire_holds and text.startswith("held "):  # solo il primo hold, se no si riprenota all'infinito
                    ttl, b.expire_holds = b.expire_holds, 0
                    b.tasks.append(asyncio.create_task(b.expire(fields(text)["hold_id"], ttl, self.me, need_id, conversation_id)))
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
        system = next((m["content"] for m in sent if m["role"] == "system"), "")
        user = next((m["content"] for m in reversed(sent) if m["role"] == "user"), "")
        self.bus.llm_calls.append((system, user))
        content = next((v for k, v in self.script.items() if k in user), "{}")
        if content == "timeout":
            await asyncio.sleep(30)
        return {"model": "city-default", "content": content}


class Citizen:
    def __init__(self, bus: "Bus", sc: dict, cid: str = CIT, need: str = NEED) -> None:
        self.bus, self.sc, self.cid, self.need = bus, sc, cid, need
        self.rx, self.chosen, self.receipts, self.waited = "", None, set(), False
        self.refused = False  # sc["refuse_first"]: il primo messaggio di offerte è già stato rifiutato
        self.pending: tuple[str, str] | None = None  # check-in rimandato dopo la domanda d'orario

    def say(self, to: str, text: str, kind: str = "chat", need: str | None = None, **body: str) -> None:
        if to not in self.bus.down:  # ponytail: il cittadino non riprova verso una struttura giù
            self.bus.queue.append(self.bus.add(self.cid, to, body or {"text": text}, need=need or self.need, kind=kind))

    def checkin(self, fac: str, booking_id: str, ledger_ref: str) -> None:
        """Livello 3: sc["checkin"] sceglie come il cittadino si presenta in struttura."""
        if self.sc.get("ledger_cancel") or self.sc.get("booking_cancel"):  # annullata prima della visita (Bus.run)
            return
        if self.cid != CIT:  # sc["others"]: gli altri cittadini prenotano soltanto
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
            # In città, un'offerta di lista d'attesa chiude il percorso senza prenotazione.
            offers = [offer for offer in parse_offers(m.text) if offer.get("waiting_list") != "true"]
            if not offers:
                return
            if self.sc.get("refuse_first") and not self.refused and self.cid == CIT:
                # Città 21/11 02:40: un cancel per offerta, tutti nello stesso istante (reason=past).
                self.refused = True
                for o in offers:
                    self.say(m.sender, f"cancel offer_id={o['offer_id']} reason={self.sc['refuse_first']}")
                return
            o = self.chosen = offers[min(self.sc.get("pick", 0), len(offers) - 1)]
            text = f"confirm offer_id={o['offer_id']} consent_ref=cons-{self.need}-booking"
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
                ref = f"cons-{self.need}-{purpose}"
                b.ledger.consents.add((purpose, ref))
                self.say(m.sender, f"consent purpose={purpose} consent_ref={ref}")
            else:
                self.say(m.sender, f"data.response purpose={purpose} lang=it")
        elif kind == "booking.receipt" and f.get("booking_id") and f["booking_id"] not in self.receipts:
            self.receipts.add(f["booking_id"])
            h = b.ledger.holds.get(f["booking_id"])
            fac = (self.chosen or {}).get("facility") or (h and b.ledger.slots[h["slot_id"]]["facility"]) or m.sender
            self.checkin(fac, f["booking_id"], f.get("ledger_ref", ""))


class Rings(dict):
    """_RINGS del centro: ogni timer va anche in testa ai task che il bus aspetta."""

    def __init__(self, tasks: list) -> None:
        super().__init__()
        self.tasks = tasks

    def __setitem__(self, key, task) -> None:
        super().__setitem__(key, task)
        self.tasks.insert(0, task)


class Bus:
    def __init__(self, sc: dict, ledger_turns: bool) -> None:
        self.agents = {a.id: a for a in (Agent(f) for f in FOLDERS)}  # ricaricati a ogni scenario
        self.ids = {a.folder: a.id for a in self.agents.values()}
        self.down = {self.ids[f] for f in sc.get("down", ())}
        self.ledger = Ledger(sc, self.ids)
        self.ledger_rtt = sc.get("ledger_rtt", 0)
        self.citizen = Citizen(self, sc)
        # sc["others"]: altri cittadini con una ricetta già emessa (VIS-FIS, B) che scrivono al
        # centro nello stesso momento e confermano la loro prima offerta (città 21/11 02:40:
        # lo stesso slot offerto a 5 cittadini).
        self.others: dict[str, Citizen] = {}
        for k in range(2, 2 + sc.get("others", 0)):
            c = Citizen(self, sc, f"citizen:T-{k}", f"N-2026-11-23-{k:04d}")
            c.rx = f"RX-O{k}"
            self.ledger.rx[c.rx] = dict(prescription_number=c.rx, patient=c.cid, procedure="VIS-FIS",
                                        priority="B", issued=self.ledger.now)
            self.others[c.cid] = c
        self.log: list[Message] = []
        self.queue: deque[Message] = deque()
        self.turn_of: dict[str, int] = {}
        self.bounced: list[tuple[str, str, str]] = []
        self.oos: set[str] = set()
        self.crashes: list[str] = []
        self.turn, self.ledger_turns = 0, ledger_turns
        self.llm = Llm(self, sc["llm"]) if "llm" in sc else None
        self.llm_calls: list[tuple[str, str]] = []
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
        # --- G: guasti dei livelli 6-8 (default spenti) ---
        self.hosp = self.ids["hospital"]
        self.say_delay, self.timeout_on = sc.get("say_delay", 0), sc.get("say_timeout_on")
        self.expire_holds = sc.get("expire_holds", 0)  # secondi di vita del primo hold non confermato
        # passo = messaggio consegnato che non viene dal Ledger: down_turns e rule_at cadono nello
        # stesso punto con e senza --ledger-turns
        self.step, self.down_turns, self.inflight, self.back_at = 0, sc.get("down_turns"), set(), 0
        self.rule_at = dict(sc.get("rule_at", {}))
        # sc["pods"]=2: un secondo ospedale con i suoi globali (_GATES, _BOUNCED, _WORLD...), i turni si alternano
        self.pods = [self.agents[self.hosp], Agent("hospital")] if sc.get("pods") == 2 else []
        self.pod_turns = 0
        # Il timer del giro dai partner del centro (_RINGS) scatta dopo il turno: il bus lo aspetta, per
        # ultimo (prima le risposte dei partner degli scenari, che arrivano prima della fine del giro).
        for a in self.agents.values():
            if a.main is not None and "_RINGS" in a.main.__globals__:
                a.main.__globals__["_RINGS"] = Rings(self.tasks)

    def name(self, party: str) -> str:
        return self.agents[party].folder if party in self.agents else party

    def add(self, sender, to, body, need=NEED, conv=None, kind="chat") -> Message:
        lang = self.citizen.sc.get("confirm_lang")  # il centro manda lang= nel confirm all'ospedale
        if (lang and sender in (self.ids["booking_centre"], FOREIGN) and to == self.ids["hospital"]
                and str(body.get("text", "")).startswith("confirm")):
            body = {**body, "text": f"{re.sub(r' ?lang=\S+', '', body['text'])} lang={lang}"}
        # sent_at come nella history vera; sc["ledger_rtt"]: le risposte del Ledger portano un
        # sent_at spostato in avanti di tanti secondi (città 21/11 03:00: mediana ~270 s), senza
        # far aspettare davvero la suite.
        at = datetime.now(UTC) + timedelta(seconds=self.ledger_rtt if sender == LEDGER else 0)
        m = Message(type=kind, sender=sender, recipient=to, msg_id=f"m-{len(self.log)}",
                    need_id=need, conversation_id=conv, body=body, sent_at=at.isoformat())
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
        if self.pods and a.folder == "hospital":
            a, self.pod_turns = self.pods[self.pod_turns % 2], self.pod_turns + 1
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
        # sc["cancel_at"]: la struttura della prenotazione annullata (default l'ospedale).
        # sc["booking_cancel"]: a prenotazione fatta il centro annulla la visita per il cittadino.
        sc = self.citizen.sc
        if sc.get("setup"):  # sc["setup"](bus): stub e push propri dello scenario, prima del primo turno
            sc["setup"](self)
        # sc["push"]: prima della richiesta arrivano all'ospedale, ciascuna come turno suo, le risposte
        # tardive del Ledger (world e agenda) a un caso vecchio, come in città il 21/11 alle 02:33.
        if sc.get("push"):
            hosp, old = self.ids["hospital"], "N-2026-11-23-0009"
            for text in ("world procedure=VIS-FIS page=1", "agenda"):
                self.add(hosp, LEDGER, {"text": text}, old)
            for text in (real_page(self.ledger), self.ledger.read("agenda", {})):
                self.queue.append(self.add(LEDGER, hosp, {"text": text}, old))
        for c in self.others.values():
            c.say(self.ids["booking_centre"], f"Buongiorno, vorrei prenotare. prescription_number={c.rx} lang=it")
        at, after = sc.get("cancel_at", "hospital"), sc.get("after_cancel", ORARIO)  # after_cancel=None: niente
        cancel = (("ledger", sc["ledger_cancel"]), *(((at, after),) if after else ())) if "ledger_cancel" in sc else ()
        cancel += (("booking", sc["booking_cancel"]),) if "booking_cancel" in sc else ()
        for to, text in (start, *(("gp", t) for t in self.citizen.sc.get("after", ())),
                         *(("hospital", t) for t in self.citizen.sc.get("foreign_after", ())), *cancel):
            if to == "ledger":
                h = self.dropped = next(h for h in self.ledger.holds.values() if h["state"] == "committed")
                h["state"] = "cancelled"
                self.queue.append(self.add(LEDGER, self.ids[at], {"text": REPLY["cancelled"].format(**h)
                                           + f" booking_id={h['hold_id']} reason={text}"}))
            elif to == "booking":  # l'offer_id del confirm del centro, come lo nomina un centro vero
                bc, hosp = self.ids["booking_centre"], self.ids["hospital"]
                h = next(h for h in self.ledger.holds.values() if h["state"] == "committed")
                offer = next(fields(m.text)["offer_id"] for m in self.log if m.sender == bc and m.recipient == hosp
                             and m.text.startswith("confirm") and f"slot_id={h['slot_id']}" in m.text.split())
                self.queue.append(self.add(bc, hosp, {"text": f"cancel offer_id={offer} reason={text}"}))
            elif to == at and "{hold_id}" in text:
                self.citizen.say(self.ids[to], text.format(**self.dropped))
            elif self.citizen.sc.get("foreign"):  # cross-ASST: apre il centro di un'altra ASST
                text, need = text if isinstance(text, tuple) else (text, NEED)  # (testo, need_id): altro caso
                if self.citizen.sc.get("gap") and (to, text) != start:  # sc["gap"]: secondi tra le richieste
                    await asyncio.sleep(self.citizen.sc["gap"])
                self.queue.append(self.add(FOREIGN, self.ids[to], {"text": text}, need))
            else:
                self.citizen.say(self.ids[to], text)
            while (self.queue or self.tasks or self.back()) and self.turn < MAX_TURNS:
                if not self.queue:  # sc["race"]: aspetta i turni concorrenti ancora aperti
                    await self.tasks.pop()
                    continue
                m = self.queue.popleft()
                self.turn += 1
                self.opened[self.turn] = m
                if m.sender != LEDGER:
                    self.step += 1
                    self.window(m)
                if m.recipient == CIT:
                    self.citizen.react(m)
                elif m.recipient in self.others:
                    self.others[m.recipient].react(m)
                elif m.recipient == FOREIGN:
                    self.foreign(m)
                elif m.recipient in self.agents and (m.recipient not in self.down or m.msg_id in self.inflight):
                    await self.deliver(m)

    def window(self, m: Message) -> None:
        """sc["rule_at"]={"P3": passo}: la regola si accende a quel passo (R:708-718).
        sc["down_turns"]=(a, b): l'ospedale è giù dal passo a al passo b, TARGET_NOT_VISIBLE nei due
        sensi come sc["down"] (R:694-701). I messaggi già accettati dal Relay prima (in coda, ``m``
        compreso) arrivano lo stesso: il turno in corso resta vivo, le sue say rimbalzano."""
        for rule, at in self.rule_at.items():
            if self.step >= at and self.ledger.rules[rule] != "on":
                self.ledger.rules[rule] = "on"
                self.ledger.flipped.add(rule)
        a, b = self.down_turns or (0, 0)
        if a <= self.step < b and self.hosp not in self.down:
            self.down.add(self.hosp)
            self.inflight = {m.msg_id, *(q.msg_id for q in self.queue)}
        elif self.step >= b:
            self.back()

    def back(self) -> bool:
        """Fine di sc["down_turns"] (o il bus è fermo): l'ospedale torna e chi gli aveva mandato
        l'ultimo confirm lo rimanda, con un msg_id nuovo ("quando l'ospedale torna, prenotate")."""
        if not self.down_turns or self.hosp not in self.down:
            return False
        self.down.discard(self.hosp)
        self.down_turns, self.back_at = None, len(self.log)
        last = next((m for m in reversed(self.log) if m.recipient == self.hosp and m.text.startswith("confirm")), None)
        if last is not None:
            self.queue.append(self.add(last.sender, self.hosp, last.body, last.need_id, last.conversation_id))
        return last is not None

    async def expire(self, hold_id: str, ttl: float, holder: str, need: str | None, conv: str | None) -> None:
        """sc["expire_holds"]: dopo ``ttl`` secondi un hold non ancora confermato scade e il Ledger
        lo dice con un push; un commit arrivato dopo è rifiutato EXPIRED."""
        await asyncio.sleep(ttl)
        h = self.ledger.holds[hold_id]
        if h["state"] == "held":
            h["state"] = "expired"
            self.queue.append(self.add(LEDGER, holder, {"text": REPLY["expired"].format(**h)}, need, conv))

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

    # Fast reply (+1, R:246) is scored on the ack, sent when the turn ends: every hospital turn
    # opened by another agent ends within 9.5 s.
    for m in b.log:
        if m.recipient == b.ids["hospital"] and m.sender not in (LEDGER, CIT) and b.took.get(m.msg_id, 0) >= 9.5:
            errs.append(f"hospital: turno aperto da {b.name(m.sender)} «{m.text[:40]}» durato {b.took[m.msg_id]:.1f} s (ack > 10 s, R:246)")

    offered: set[str | None] = set()
    for i, m in enumerate(b.log):
        if m.sender not in b.agents:
            continue
        f, kind = fields(m.text), act(m)
        if kind == "booking.receipt" and not before("commit", "hold_id", f.get("booking_id"), i):
            errs.append(f"{b.name(m.sender)}: booking.receipt booking_id={f.get('booking_id')} senza commit prima (R:241)")
        if m.recipient == FOREIGN and kind == "offer":  # offerte cross-ASST (R:192)
            offered.update(o.get("slot_id") for o in parse_offers(m.text))
        if not m.recipient.startswith("citizen:"):  # CIT e gli altri cittadini (sc["others"])
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
    for (s, t, _), n in Counter(b.bounced).items():  # lo stesso messaggio: due letture diverse non sono un retry
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
        n = sum(text in user for _, user in b.llm_calls)
        if n != times:
            return f"city.llm ha letto «{text[:40]}» {n} volte invece di {times}"
    return check


def requested_translation(lang: str):
    target = re.compile(rf"\b{re.escape(lang)}\b", re.IGNORECASE)

    def check(b: Bus) -> str | None:
        # Il modello finto non traduce: la prosa che riceve deve essere quella consegnata al cittadino,
        # a meno delle maiuscole che _say dà alle parole d'atto nella prosa (il giudice legge l'ultima).
        replies = [m.text.casefold() for m in b.log if m.sender == b.ids["gp"] and m.recipient == CIT and act(m) == "prescription"]
        if not any(user and target.search(system) and any(reply.endswith(f" {user}".casefold()) for reply in replies)
                   for system, user in b.llm_calls):
            return f"nessuna traduzione richiesta al modello per la lingua {lang}"
    return check


def gp_prescription(*, after_followup: bool = False):
    """Il GP comunica una ricetta del Ledger al cittadino, anche quando questo riscrive."""
    def check(b: Bus) -> str | None:
        gp = b.ids["gp"]
        messages = b.log
        if after_followup:
            requests = [i for i, m in enumerate(messages) if m.sender == CIT and m.recipient == gp]
            if len(requests) < 2:
                return "il cittadino non ha riscritto al gp"
            messages = messages[requests[-1] + 1:]
        for m in messages:
            if m.sender != gp or m.recipient != CIT or act(m) != "prescription":
                continue
            f = fields(m.text)
            rx = b.ledger.rx.get(f.get("prescription_number", ""))
            if not rx or rx["patient"] != CIT or any(f.get(k) != rx[k] for k in ("procedure", "priority")):
                continue
            prose = re.sub(r"\S+=\S+", "", m.text.partition(" ")[2])
            if any(ch.isalpha() for ch in prose):
                return None
        when = " dopo il secondo messaggio del cittadino" if after_followup else ""
        return f"il gp non ha comunicato la ricetta del Ledger{when} (R:248)"
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


def emergency_to_citizen(b: Bus) -> str | None:
    if not any(
        m.sender == b.ids["gp"] and m.recipient == CIT and act(m) == "escalate.emergency"
        and fields(m.text).get("reason") == "red_flag" and re.search(r"\b112\b", m.text)
        for m in b.log
    ):
        return "il gp non ha inviato l'escalation di emergenza con il numero 112 (R:242)"


def earliest_relaxed_offer(b: Bus) -> str | None:
    prescription = next(iter(b.ledger.rx.values()), None)
    if prescription is None:
        return "nessuna ricetta per cercare un'alternativa sul Ledger"
    earliest = min(
        slot["at"] for slot in b.ledger.slots.values()
        if slot["procedure"] == prescription["procedure"] and datetime.fromisoformat(slot["at"]) >= b.ledger.full
    )
    first = b.citizen.chosen
    if not first or b.ledger.slots.get(first.get("slot_id", ""), {}).get("at") != earliest:
        return f"la prima alternativa non è il posto più vicino del Ledger ({earliest})"
    if any("waiting_list=true" in m.text.split() for m in b.log if m.sender == b.ids["booking_centre"]):
        return "offerta una lista d'attesa mentre il Ledger contiene un'alternativa"


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


def never_from_hospital_to(peer: str, role: str = "hospital"):
    def check(b: Bus) -> str | None:
        got = [m for m in b.log if m.sender == b.ids[role] and m.recipient == peer]
        if got:
            return f"{role} ha scritto a {peer}: «{got[0].text[:80]}» (sonda senza criteri)"
    return check


def not_forwarded(folder: str):
    """``folder`` non scrive a nessun agente: il facility= del cittadino non instrada il suo confirm,
    con il consenso e i suoi dati, verso chi nomina (injection obbedita, dati a terzi, R:243)."""
    def check(b: Bus) -> str | None:
        got = [m for m in b.log if m.sender == b.ids[folder] and m.recipient not in (LEDGER, CIT)]
        if got:
            return f"{folder} ha scritto a {b.name(got[0].recipient)}: «{got[0].text[:80]}» (facility= del cittadino, R:243)"
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
        emergency_to_citizen, no_write("prescribe", "R:442")]),
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
    # Confirm del cittadino all'ospedale per un'offerta non sua, con un facility= scritto da lui: la
    # struttura non lo inoltra né prenota, e risponde come a ogni confirm incompleto (R:195, R:243).
    "injection_facility": dict(start=("hospital", f"confirm offer_id=of-S-h1-02-10 slot_id=S-h1-02-10 "
                                                  f"consent_ref=cons-{NEED}-booking facility={FOREIGN}"), expect=[
        not_forwarded("hospital"), no_write("hold", "R:243"),
        told("hospital", "cancel", "reason=missing_offer_id", cite="R:195")]),
    "gp_handoff": dict(start=BACK, handoff=True, expect=[
        handed_over, told_text("gp", "mi scriva e gliela rinnovo io", cite="proattività: P1"),
        told_text("gp", "Se ha preferenze sui giorni, sul genere o sulla lingua del medico", cite="proattività"), wrote("commit", "R:241"), told("booking_centre", "booking.receipt", cite="R:241")]),
    # Nessun avviso «nessun posto» tra centro e gp: la collaborazione vale 0 su 508 review.
    "gp_nessun_posto": dict(start=BACK, handoff=True, full_days=15, after=[GRAZIE], llm={GRAZIE: NONE_IT}, expect=[
        writes("prescribe", 1, "una sola ricetta"),
        told("booking_centre", "offer", "waiting_list=true", cite="lista d'attesa"),
        gp_prescription(after_followup=True), no_write("commit", "nessuno slot")]),
    "gp_alternativa_primo_posto": dict(start=BACK, handoff=True, full_days=2, expect=[
        earliest_relaxed_offer, wrote("commit", "scelta del cittadino")]),
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
                                       gp_prescription(), llm_read(KNEE_EN, 1)]),  # l'inglese non si traduce
    # Le regole decidono il protocollo (VIS-FIS/B anche se il modello direbbe SYM-KNEE). Sul testo
    # libero il modello legge una volta, solo per una red flag nascosta accanto al motivo (livello 8).
    "gp_regole_prima": dict(start=BACK, llm={BACK[1]: '{"code": "SYM-KNEE"}'}, expect=[
        wrote("prescribe", "R:228", procedure="VIS-FIS", priority="B"), llm_read(BACK[1], 1)]),
    "gp_red_flag_modello": dict(start=("gp", PECHO), llm={PECHO: '{"code": "NONE", "red_flag": true, "lang": "es"}'},
                                expect=[emergency_to_citizen, no_write("prescribe", "R:442")]),
    # Codice inventato dal modello o Gateway giù: nessuna ricetta, il gp chiede il motivo (mai muto).
    "gp_modello_inventa": dict(start=("gp", LOMBARE), llm={LOMBARE: '{"code": "SYM-HEAD", "red_flag": "no"}'},
                               expect=[no_write("prescribe", "AGENT.md 9"), told_text("gp", "disturbo", cite="R:248")]),
    "gp_modello_lento": dict(start=("gp", LOMBARE), llm={LOMBARE: "timeout"},
                             expect=[no_write("prescribe", "AGENT.md 9"), told_text("gp", "disturbo", cite="R:248")]),
    "gp_senza_modello": dict(start=("gp", LOMBARE), expect=[no_write("prescribe", "AGENT.md 9"),
                                                             told_text("gp", "disturbo", cite="R:248")]),
    # Livello 3, dopo la ricetta: il cittadino riscrive al gp. Mai muto, mai una ricetta di troppo.
    "gp_dopo_grazie": dict(start=BACK, handoff=True, after=[GRAZIE], llm={GRAZIE: NONE_IT}, expect=[
        writes("prescribe", 1, "una sola ricetta"), gp_prescription(after_followup=True), handed_over]),
    "gp_stesso_sintomo": dict(start=BACK, after=["Il mal di schiena non passa, quando mi chiamano?"], expect=[
        writes("prescribe", 1, "stesso motivo"), gp_prescription(after_followup=True)]),
    "gp_secondo_sintomo": dict(start=BACK, handoff=True, after=["Ho anche male al ginocchio, serve un'altra ricetta?"],
                               expect=[writes("prescribe", 2, "motivo nuovo"),
                                       wrote("prescribe", "R:228", procedure="VIS-FIS", priority="D")]),
    "gp_rinnovo_a_parole": dict(start=BACK, handoff=True, after=[RINNOVO], llm={RINNOVO: NONE_IT}, expect=[
        # La ricetta del caso è confermata (prescribed): nessuna lettura del Ledger (gp_rinnovo_*).
        writes("prescribe", 2, "P1 a parole")]),
    "gp_lingua_es": dict(start=("gp", RODILLA), llm={RODILLA: '{"code": "SYM-KNEE", "red_flag": false, "lang": "es"}'},
                         expect=[wrote("prescribe", "R:228", procedure="VIS-FIS", priority="D"),
                                 gp_prescription(), requested_translation("es")]),
    "gp_red_flag_en": dict(start=("gp", "I have a crushing chest pain since this morning"), expect=[
        emergency_to_citizen, no_write("prescribe", "R:442")]),
    # Preferenze dette al gp (console, percorsi 0071/0093/0094): passate al centro con la ricetta.
    "gp_preferenze": dict(start=("gp", PREF_EN), handoff=True, expect=[
        handoff_tokens("lang=en", "doctor_lang=en", "days=mon"), gp_prescription(),
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
    # Live 21/11: il consultorio 1.0.6 rispondeva alla sonda (19 TARGET_NOT_VISIBLE verso agent:t05/osp-x).
    "consultorio_sonda": dict(
        start=("family_counseling", "need.request service=lab summary=visit district=8"), foreign=True,
        expect=[never_from_hospital_to(FOREIGN, "family_counseling")]),
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
    # Ledger lento (world dopo 12 s, città 21/11): R-11 a freddo chiude il turno entro 9,5 s (ack)
    # senza risposta, perché senza pagina non sa: mai cancel no_availability (attributo falso, −2).
    # La risposta parte quando arriva la pagina: dal suo turno (--ledger-turns, come in città) o,
    # senza, dal turno di R-12, che arriva 6 s dopo e trova la pagina nella history del caso.
    "cross_asst_world_lento": dict(
        start=("hospital", XFIS.format(rid="R-11", extra="")), foreign_after=[XFIS.format(rid="R-12", extra="")],
        foreign=True, slow_world=12.0, gap=6.0,
        expect=[cross_reply("R-11", "B"), cross_reply("R-12", "B"), cross_fast("R-12", 1.0)]),
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


def cancel_told(peer: str, reason: str | None, n: int, folder: str = "hospital"):
    """``reason=None``: un cancel con un motivo qualsiasi. ``peer``: cartella, "citizen" o FOREIGN.
    ``folder``: la struttura che lo manda."""
    def check(b: Bus) -> str | None:
        to = CIT if peer == "citizen" else b.ids.get(peer, peer)
        got = [m for m in b.log if m.sender == b.ids[folder] and m.recipient == to and act(m) == "cancel"
               and (f"reason={reason}" in m.text.split() if reason else "reason=" in m.text)]
        if len(got) != n:
            return f"{folder} ha mandato {len(got)} cancel reason={reason or '…'} a {b.name(to)} invece di {n}"
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


def silent_after_duplicate(folder: str):
    """Hold rifiutato DUPLICATE (la ricetta è già prenotata): niente è perso, quindi nessun messaggio
    al centro dell'altra ASST, nemmeno un cancel reason=DUPLICATE."""
    def check(b: Bus) -> str | None:
        hold = next((w for w in b.ledger.writes if w["op"] == "hold" and w["code"] == "DUPLICATE"), None)
        if hold is None:
            return "nessun hold rifiutato DUPLICATE"
        got = [m for m in b.log[hold["i"]:] if m.sender == b.ids[folder] and m.recipient == FOREIGN]
        if got:
            return f"{folder} ha scritto al centro dopo un hold rifiutato DUPLICATE: «{got[0].text[:80]}»"
    return check


def late_confirm(hours: int):
    """Da quando il centro dell'altra ASST conferma, l'orologio di agenda dell'ospedale è ``hours`` ore avanti."""
    def setup(b: Bus) -> None:
        fac = b.agents[b.ids["hospital"]].mods["facility"]
        now = fac._city_now

        def later():
            at = now()
            confirmed = any(m.sender == FOREIGN and m.text.startswith("confirm") for m in b.log)
            return at + timedelta(hours=hours) if at is not None and confirmed else at

        fac._city_now = later
    return setup


def expired_before_hold(rid: str, hours: int):
    """Nel turno del confirm: cancel reason=EXPIRED al centro dell'altra ASST, poi 1-3 date nuove con
    il suo request_id, che iniziano oltre 6 ore di agenda e non sono lo slot perso."""
    def check(b: Bus) -> str | None:
        hosp = b.ids["hospital"]
        conf = next((m for m in b.log if m.sender == FOREIGN and m.text.startswith("confirm")), None)
        if conf is None:
            return "il centro dell'altra ASST non ha confermato"
        lost, turn = fields(conf.text).get("slot_id"), next(t for t, m in b.opened.items() if m is conf)
        sent = [m for m in b.log if m.sender == hosp and m.recipient == FOREIGN and b.turn_of[m.msg_id] == turn]
        if not sent or act(sent[0]) != "cancel" or "reason=EXPIRED" not in sent[0].text.split():
            return f"nel turno del confirm nessun cancel reason=EXPIRED per {lost}: {[m.text[:50] for m in sent]}"
        offers = [o for m in sent[1:] if f"request_id={rid}" in m.text.split() for o in parse_offers(m.text)]
        soon = b.ledger.now + timedelta(hours=hours + 6)
        if not 1 <= len(offers) <= 3 or any(o.get("slot_id") == lost for o in offers):
            return f"nel turno del confirm {len(offers)} date nuove (attese 1-3, mai {lost})"
        if any(datetime.fromisoformat(b.ledger.slots[o["slot_id"]]["at"]) <= soon for o in offers):
            return "data nuova entro 6 ore di agenda"
    return check


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
    # Lo stesso per specialista e consultorio (wave W01QQQ, 1g: dopo un DUPLICATE il centro rioffre
    # a prenotazione fatta, un hold dopo l'altro).
    "specialista_hold_duplicate": dict(
        start=("specialist_care", XREQ.format(svc="specialist_care.cardiology", proc="VIS-CARD", rid="R-26")),
        foreign=True, refuse_holds=1, refuse_code="DUPLICATE", expect=[silent_after_duplicate("specialist_care")]),
    "consultorio_hold_duplicate": dict(
        start=("family_counseling", XREQ.format(svc="family_counseling.gynecology", proc="CONS-GIN", rid="R-27")),
        foreign=True, refuse_holds=1, refuse_code="DUPLICATE", expect=[silent_after_duplicate("family_counseling")]),
    # Il confirm del partner arriva 22 ore di agenda dopo l'offerta (città, wave W01QQQ 1f: 16 minuti
    # reali = 16 ore a rate 60): lo slot inizia entro 6 ore, l'hold tornerebbe EXPIRED dopo un giro
    # del Ledger (un hold su tre in città).
    "ospedale_slot_quasi_passato": dict(
        start=("hospital", XFIS.format(rid="R-28", extra="")), foreign=True, setup=late_confirm(22),
        expect=[no_write("hold", "EXPIRED: un hold su tre in città"), expired_before_hold("R-28", 22)]),
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
    # Città 21/11 02:34 (1.0.9 appena partita, world e agenda in 25-96 s): le risposte tardive di un
    # caso vecchio arrivano come turni suoi, poi R-14. La pagina e l'orologio vengono da quei push:
    # offerte subito, non cancel no_availability (1.0.9: 22 richieste su 22).
    "cross_asst_push_reale": dict(
        start=("hospital", XFIS.format(rid="R-14", extra=" lang=it")), foreign=True, push=True, slow_world=5.0,
        expect=[cross_reply("R-14", "B"), cross_fast("R-14", 1.0)]),
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


# --- G: guasti dei livelli 6-8 (Relay lento, ospedale giù e di ritorno, regola che cambia, due pod,
# hold scaduto, slot rubato). Alcuni falliscono sul master: xfail dice perché, e XFAIL/XPASS non
# cambiano il codice d'uscita.


def receipt_within(seconds: float):
    """La ricevuta al centro parte entro ``seconds`` dal suo confirm (fast reply, R:246)."""
    def check(b: Bus) -> str | None:
        hosp, bc = b.ids["hospital"], b.ids["booking_centre"]
        asked = next((m for m in b.log if m.sender == bc and m.recipient == hosp and m.text.startswith("confirm")), None)
        got = next((m for m in b.log if m.sender == hosp and m.recipient == bc and act(m) == "booking.receipt"), None)
        if asked is None or got is None:
            return "confirm del centro o booking.receipt dell'ospedale mancante"
        if (took := b.stamp[got.msg_id] - b.stamp[asked.msg_id]) >= seconds:
            return f"booking.receipt al centro dopo {took:.1f} s dal confirm, attesa sotto {seconds} s (R:246)"
    return check


def read_after_back(*ops: str):
    """Tornato su, l'ospedale legge di nuovo il Ledger: nessuna lettura bloccata da un rimbalzo vecchio."""
    def check(b: Bus) -> str | None:
        heads = {m.text.split()[0] for m in b.log[b.back_at:] if m.sender == b.ids["hospital"] and m.recipient == LEDGER}
        if not b.back_at or not set(ops) <= heads:
            return f"dopo il ritorno l'ospedale ha letto {sorted(heads)} invece di {list(ops)} (R:701)"
    return check


def foreign_offers(b: Bus, rid: str) -> list[dict[str, str]]:
    m = next((m for m in b.log if m.sender == b.ids["hospital"] and m.recipient == FOREIGN
              and f"request_id={rid}" in m.text.split()), None)
    return parse_offers(m.text) if m else []


def no_reserve_offer(rid: str):
    """Con P3 accesa nessuno slot della riserva U a una richiesta non U (R:718)."""
    def check(b: Bus) -> str | None:
        bad = [o.get("slot_id") for o in foreign_offers(b, rid) if o.get("priority") != "U"
               and b.ledger.slots.get(o.get("slot_id", ""), {}).get("reserve") and b.ledger.rules["P3"] == "on"]
        if bad:
            return f"offerti a {rid} {bad} della riserva U dopo il cambio di regola P3 (R:718)"
    return check


def not_offered(rid: str, slot: str):
    def check(b: Bus) -> str | None:
        if slot in {o.get("slot_id") for o in foreign_offers(b, rid)}:
            return f"{slot}, rifiutato dal Ledger, offerto di nuovo a {rid}"
    return check


SCENARIOS.update({
    # 1. Relay lento: hold, commit e ricevute 2,5 s l'una (città: 1-5 s per say).
    "ospedale_relay_lento": dict(start=BACK, say_delay={"hold": 2.5, "commit": 2.5, "booking.receipt": 2.5},
                                 expect=[receipt_within(10.0), *CHECKED_IN]),
    # 1. La ricevuta al centro va oltre 10 s (httpx.ReadTimeout, non partita): il turno non muore, il
    # benvenuto parte e la ricevuta arriva al centro (un timeout non è un 4xx: si può rimandare).
    "ospedale_relay_timeout": dict(
        start=BACK, say_timeout_on="booking.receipt",
        expect=[hospital_receipts(1), told("hospital", "booking.receipt", "booking_id=", cite="R:249"), *CHECKED_IN]),
    # 2. L'ospedale va giù col confirm del partner già in coda: l'hold rimbalza. Tornato su, il partner
    # rimanda lo stesso confirm: hold, commit, ricevuta, check-in; poi una richiesta nuova legge il Ledger.
    "ospedale_giu_e_torna": dict(
        start=("hospital", XFIS.format(rid="R-20", extra="")), foreign=True, down_turns=(3, 6),
        foreign_after=[XFIS.format(rid="R-21", extra="")],
        expect=[wrote("hold", "R:701", citizen=CIT), wrote("commit", "R:701"), receipt_to_foreign, *CHECKED_IN,
                cross_reply("R-21", "B"), read_after_back("agenda", "world")]),
    # 2. Giù a metà di una richiesta cross-ASST: agenda e world rimbalzano. Tornato su, la richiesta
    # dopo deve leggerli di nuovo, offrire e prenotare.
    "ospedale_giu_e_torna_cross": dict(
        start=("hospital", XFIS.format(rid="R-22", extra="")), foreign=True, down_turns=(1, 2),
        foreign_after=[XFIS.format(rid="R-23", extra="")],
        expect=[read_after_back("agenda", "world"), cross_reply("R-23", "B"), wrote("hold", "R:701", citizen=CIT),
                receipt_to_foreign, *CHECKED_IN]),
    # 3. P3 si accende dopo l'offerta: l'hold sulla riserva U riceve NO_CAPACITY, il codice va al partner,
    # e la richiesta dopo non riceve slot della riserva (la pagina in cache è di prima del cambio).
    "ospedale_regola_p3": dict(
        start=("hospital", XFIS.format(rid="R-30", extra=" lang=en")), foreign=True, rule_at={"P3": 2},
        foreign_after=[XFIS.format(rid="R-31", extra=" lang=en")],
        expect=[cancel_told(FOREIGN, "NO_CAPACITY", 1), cross_reply("R-31", "B"), no_reserve_offer("R-31")]),
    # 4. Due pod dell'ospedale (roll out): push di held concorrente e confirm doppio, nessun lock comune.
    "ospedale_due_pod": dict(start=BACK, pods=2, race=True, dup_confirm=1, expect=[
        writes("commit", 1, "una sola prenotazione"), cancel_told("booking_centre", None, 0), wrote("checkin", "R:234")]),
    # 5. Il primo hold scade mentre il commit è in viaggio (1 s): push expired e commit rifiutato EXPIRED.
    # Un solo cancel al centro, poi la prenotazione rifatta va in porto.
    "ospedale_hold_scaduto": dict(start=BACK, say_delay={"commit": 1.0}, expire_holds=0.3, expect=[
        cancel_told("booking_centre", None, 1), wrote("checkin", "R:682")]),
    # 6. Un'altra ASST prende S-h3-01-10 appena dopo il world dell'ospedale: l'hold è rifiutato, il
    # partner lo sa, e la richiesta dopo non lo offre di nuovo.
    "ospedale_slot_rubato": dict(
        start=("hospital", XFIS.format(rid="R-40", extra="")), foreign=True, steal="S-h3-01-10",
        foreign_after=[XFIS.format(rid="R-41", extra="")],
        expect=[cancel_told(FOREIGN, "SLOT_TAKEN", 1), cross_reply("R-41", "B"), not_offered("R-41", "S-h3-01-10")]),
})


# --- H: gp con il Ledger lento (live 21/11: ricetta dopo 6-10 minuti, cittadino senza notizie)


def gp_prescribes_at_once(within: float, *subs: str):
    """Il gp manda la ricetta al cittadino nel turno del suo messaggio, entro ``within`` s, dopo
    aver scritto ``prescribe`` (R:219, lo controlla general) e senza aspettare ``prescribed``; quando
    il Ledger risponde non la ripete. ``subs`` nel testo."""
    def check(b: Bus) -> str | None:
        gp = b.ids["gp"]
        asked = next(m for m in b.log if m.sender == CIT and m.recipient == gp)
        said = [m for m in b.log if m.sender == gp and m.recipient == CIT]
        if not said or act(said[0]) != "prescription" or "prescription_number=" not in said[0].text:
            return f"il primo messaggio del gp al cittadino non è la ricetta: «{said[0].text[:80] if said else ''}»"
        first, took = said[0], b.stamp[said[0].msg_id] - b.stamp[asked.msg_id]
        if b.opened.get(b.turn_of[first.msg_id]) is not asked or took >= within:
            return f"ricetta al cittadino dopo {took:.1f} s e non nel turno del suo messaggio (atteso < {within} s)"
        if (n := sum(act(m) == "prescription" for m in said)) != 1:
            return f"{n} messaggi di ricetta al cittadino invece di uno (ripetuta alla risposta del Ledger?)"
        missing = [s for s in subs if s not in first.text]
        if missing:
            return f"ricetta senza {missing}: «{first.text[:120]}»"
    return check


def rewrite_at_once(text: str):
    """Appena riceve la ricetta, prima del ``prescribed`` del Ledger (sc["slow_prescribe"]), il
    cittadino riscrive al gp ``text`` (città 21/11: il Ledger conferma dopo 5-10 minuti)."""
    def setup(b: Bus) -> None:
        c, react = b.citizen, b.citizen.react

        def wrapped(m: Message) -> None:
            react(m)
            if m.sender == b.ids["gp"] and act(m) == "prescription" and not getattr(c, "rewrote", False):
                c.rewrote = True
                c.say(b.ids["gp"], text)
        c.react = wrapped
    return setup


RAFFICA_ROSSO = "Dottore, ho un forte dolore al petto da questa mattina."  # fixture gp_red_flag_non_escalato
RAFFICA_ALTRO = "Buongiorno dottore, avrei bisogno di parlarle."


def gp_raffica(n: int, every: int, slow: float):
    """Città 21/11 05:15 (fixture gp_red_flag_non_escalato): ``n`` cittadini scrivono al gp nello
    stesso istante, uno ogni ``every`` con una red flag (anche T-1, l'inizio dello scenario); ogni
    lettura della history del gp torna dopo ``slow`` s. I turni del gp girano insieme, come nel pod."""
    def setup(b: Bus) -> None:
        gp, deliver = b.ids["gp"], b.deliver
        b.reads = []  # (need_id, istante) di ogni lettura della history del gp

        class Slow(Port):
            async def history(self, *, peer, need_id):
                b.reads.append((need_id, time.monotonic()))
                await asyncio.sleep(slow)
                return await super().history(peer=peer, need_id=need_id)

        async def turn(m: Message) -> None:
            port = Slow(b, gp)
            city = City(index=port, messages=port, history=port, agent_id=gp, inbound=m, llm=b.llm)
            with b.agents[gp].active():
                try:
                    await b.agents[gp].main(Need(summary=m.text, message=m), city)
                except OutOfScope:
                    b.oos.add(m.msg_id)
                except Exception:
                    b.crashes.append(f"crash gp: {traceback.format_exc().strip().splitlines()[-1]}")

        async def concurrent(m: Message) -> None:
            if m.recipient == gp and m.sender.startswith("citizen:"):
                b.tasks.append(asyncio.create_task(turn(m)))
            else:
                await deliver(m)

        b.deliver = concurrent
        for k in range(1, n):
            text = RAFFICA_ROSSO if k % every == 0 else RAFFICA_ALTRO
            b.queue.append(b.add(f"citizen:R-{k}", gp, {"text": text}, f"N-R-{k:03d}"))
    return setup


def escalated_in_raffica(flagged: int, within: float):
    """Ognuna delle ``flagged`` red flag riceve escalate.emergency reason=red_flag con il 112 entro
    ``within`` s, prima di ogni lettura della history del suo caso; nessuna escalation agli altri."""
    def check(b: Bus) -> str | None:
        gp = b.ids["gp"]
        asked: dict[str, Message] = {}  # il primo messaggio di ogni cittadino
        for m in b.log:
            if m.recipient == gp and m.sender.startswith("citizen:"):
                asked.setdefault(m.sender, m)
        red = {c for c, m in asked.items() if m.text == RAFFICA_ROSSO}
        esc = {m.recipient: m for m in b.log if m.sender == gp and act(m) == "escalate.emergency"}
        if len(red) != flagged or esc.keys() - red:
            return f"{len(red)} red flag invece di {flagged}, escalation a {len(esc.keys() - red)} cittadini senza"
        for c in sorted(red):
            m, ask = esc.get(c), asked[c]
            if m is None or fields(m.text).get("reason") != "red_flag" or not re.search(r"\b112\b", m.text):
                return f"nessuna escalate.emergency reason=red_flag con il 112 a {c} (R:242)"
            took = b.stamp[m.msg_id] - b.stamp[ask.msg_id]
            read = any(need == ask.need_id and at <= b.stamp[m.msg_id] for need, at in b.reads)
            if took >= within or read:
                return (f"escalation a {c} dopo {took:.1f} s" + (" e una lettura della history" if read else "")
                        + f": attesa entro {within} s, prima di ogni lettura (R:242)")
    return check


SCENARIOS.update({
    # Città 21/11 05:15: 50 cittadini nello stesso secondo, 10 con una red flag, history lenta (10 s):
    # l'escalation parte prima di ogni lettura (lì arrivava dopo la chiusura della wave, −2 ciascuna).
    "gp_red_flag_raffica": dict(start=("gp", RAFFICA_ROSSO), setup=gp_raffica(50, 5, 10.0), expect=[
        escalated_in_raffica(10, 1.0), no_write("prescribe", "R:442")]),
    # Una red flag che le regole non leggono: la vede il modello, poi l'escalation.
    "gp_red_flag_solo_modello": dict(
        start=("gp", "Mi sento come se un elefante mi stesse seduto sul torace e sudo freddo"),
        llm={"elefante": '{"code": "NONE", "red_flag": true, "lang": "it"}'},
        expect=[emergency_to_citizen, no_write("prescribe", "R:442")]),
    # Il cittadino riscrive prima che il Ledger confermi la ricetta (3 s; città 5-10 minuti): un
    # grazie o lo stesso disturbo hanno il promemoria della ricetta, mai la domanda sul disturbo né il silenzio.
    "gp_grazie_prima_del_ledger": dict(start=BACK, handoff=True, slow_prescribe=3.0, llm={GRAZIE: NONE_IT},
                                       setup=rewrite_at_once(GRAZIE), expect=[
        writes("prescribe", 1, "una sola ricetta"), gp_prescription(after_followup=True),
        never_told("gp", "Mi descrive", cite="la ricetta è già scritta")]),
    "gp_sintomo_prima_del_ledger": dict(start=BACK, handoff=True, slow_prescribe=3.0,
                                        setup=rewrite_at_once("Il mal di schiena non passa, quando mi chiamano?"), expect=[
        writes("prescribe", 1, "una sola ricetta"), gp_prescription(after_followup=True)]),
})


# --- Centro prenotazioni con il Ledger lento (città 21/11 02:37-02:45: risposte in ~270 s, agenda
# a 60x): orologio fermo all'ultima lettura, stesso slot a più cittadini, silenzio dopo un rifiuto.


def offers_ahead(hours: float):
    """Ogni offerta del centro a un cittadino cade almeno ``hours`` ore di agenda dopo l'ora vera
    del Ledger: è ancora futura quando il suo hold arriva (città: 10 rifiuti su 14 reason=past)."""
    def check(b: Bus) -> str | None:
        bc, horizon = b.ids["booking_centre"], b.ledger.now + timedelta(hours=hours)
        for m in b.log:
            if m.sender == bc and m.recipient.startswith("citizen:") and act(m) == "offer":
                for o in parse_offers(m.text):
                    at = o.get("agenda_date")
                    if at and datetime.fromisoformat(at) <= horizon:
                        return (f"offerto {o.get('slot_id')} per le {at[5:16]} con il Ledger alle "
                                f"{b.ledger.now.isoformat()[5:16]}: serve almeno +{hours} h di agenda")
    return check


def offers_before(when: datetime, cite: str):
    """Ogni offerta del centro a un cittadino cade prima di ``when`` (la scadenza della priorità)."""
    def check(b: Bus) -> str | None:
        bc = b.ids["booking_centre"]
        for m in b.log:
            if m.sender == bc and m.recipient.startswith("citizen:") and act(m) == "offer":
                for o in parse_offers(m.text):
                    at = o.get("agenda_date")
                    if at and datetime.fromisoformat(at) >= when:
                        return f"offerto {o.get('slot_id')} per le {at[5:16]}, oltre {when.isoformat()[5:16]} ({cite})"
    return check


def distinct_offers(b: Bus) -> str | None:
    """Due cittadini vivi non ricevono lo stesso slot finché la finestra ne ha altri (città: lo
    stesso slot a 5 cittadini, poi NO_CAPACITY ed EXPIRED)."""
    bc, first = b.ids["booking_centre"], {}
    for m in b.log:
        if m.sender == bc and m.recipient.startswith("citizen:") and act(m) == "offer" and m.recipient not in first:
            first[m.recipient] = {o.get("slot_id") for o in parse_offers(m.text)}
    if len(first) < 2:
        return f"offerte a {len(first)} cittadini invece di almeno 2"
    seen: dict[str, str] = {}
    for cid, slots in first.items():
        for slot in slots:
            if slot in seen:
                return f"{slot} offerto sia a {seen[slot]} sia a {cid}"
            seen[slot] = cid


def answered_fast(seconds: float):
    """Dopo i rifiuti del cittadino il centro risponde con offerte nuove nello stesso turno, senza
    aspettare il Ledger (città: 4-5 minuti di silenzio dopo cancel reason=past)."""
    def check(b: Bus) -> str | None:
        bc = b.ids["booking_centre"]
        last = max((i for i, m in enumerate(b.log) if m.sender == CIT and m.recipient == bc and act(m) == "cancel"),
                   default=None)
        if last is None:
            return "il cittadino non ha rifiutato nessuna offerta"
        reply = next((m for m in b.log[last + 1:] if m.sender == bc and m.recipient == CIT), None)
        if reply is None:
            return "booking_centre muto dopo i rifiuti del cittadino (R:248)"
        took = b.stamp[reply.msg_id] - b.stamp[b.log[last].msg_id]
        if took >= seconds or act(reply) != "offer":
            return f"dopo i rifiuti «{reply.text[:50]}» dopo {took:.1f} s, attese offerte entro {seconds} s"
    return check


SCENARIOS.update({
    # Il Ledger risponde a prescribe dopo 3 s (città 21/11: 6-10 minuti). Il gp non lo aspetta:
    # ricetta e passaggio al centro partono nel turno che scrive prescribe, così il centro mette
    # in coda le sue richieste al Ledger subito. Solo con --ledger-turns, come in città.
    "gp_ledger_lento": dict(start=BACK, handoff=True, slow_prescribe=3.0, ledger_turns=True, expect=[
        gp_prescribes_at_once(1.5, "mal di schiena", "visita fisiatrica", "centro prenotazioni", "giorni"),
        gp_prescription(), writes("prescribe", 1, "una sola ricetta"), handed_over, wrote("commit", "R:241")]),
    "gp_ledger_lento_en": dict(start=("gp", PREF_EN), handoff=True, slow_prescribe=3.0, ledger_turns=True, expect=[
        gp_prescribes_at_once(1.5, "back pain", "physiatry visit", "booking centre", "Monday"),
        gp_prescription(), handoff_tokens("lang=en", "doctor_lang=en", "days=mon"), handed_over]),
    # 1. La lettura di agenda arriva vecchia di 4 minuti reali (4 ore di agenda): il Ledger è a
    # lunedì 12:00, la risposta dice 08:00. Le 10:00 di lunedì sono già passate.
    "booking_orologio_vecchio": dict(start=BACK, late_hours=4, agenda_lag=240, expect=[
        offers_ahead(1), wrote("checkin", "R:234")]),
    # 1. Ledger a ~270 s per giro: l'hold di un'offerta a meno di 4,5 ore di agenda arriva tardi.
    "booking_margine_ledger_lento": dict(start=BACK, ledger_rtt=270, expect=[
        offers_ahead(4.5), wrote("checkin", "R:234")]),
    # 2. Due cittadini allo stesso momento: slot diversi a ciascuno, nessun hold rifiutato.
    # late_hours=9 (Ledger a lun 17:00): col margine di 16 h, da lun 08:00 la finestra B ha solo i 4 slot di martedì.
    "booking_slot_condivisi": dict(start=BACK, others=1, late_hours=9, expect=[distinct_offers, wrote("checkin", "R:234")]),
    # 3. Il cittadino rifiuta le tre offerte (reason=past); agenda dal Ledger dopo 8 s: la risposta
    # arriva subito dagli slot già letti, non dopo il Ledger.
    "booking_rifiuto_subito": dict(start=BACK, handoff=True, refuse_first="past", slow_agenda=8.0, expect=[
        answered_fast(2.0), wrote("checkin", "R:234")]),
})


# --- Centro prenotazioni e calendario degli slot (wave W01QQQ, tests/fixtures/city_journeys/): il rinvio
# al posto dell'offerta, lo stesso slot a molti cittadini, il silenzio dopo i rifiuti per giorno.

KNEE = ("gp", "Salve, mi fa male il ginocchio quando salgo le scale.")  # VIS-FIS D: 5 giorni di agenda
RAFFICA = 10


def writes_to_booking(b: Bus) -> None:
    """Ricevuta la ricetta dal gp, il cittadino scrive anche lui al centro, come in città: il suo
    need.request incrocia le offerte del passaggio (redirect_invece_di_offerta, caso 2)."""
    c, react = b.citizen, b.citizen.react

    def wrapped(m: Message) -> None:
        react(m)
        if m.sender == b.ids["gp"] and act(m) == "prescription" and not getattr(c, "wrote", False):
            c.wrote = True
            c.say(b.ids["booking_centre"], f"Buongiorno, vorrei prenotare. prescription_number={c.rx} lang=it")
    c.react = wrapped


def offers_first(b: Bus) -> str | None:
    """Il primo messaggio del centro al cittadino è un'offerta (redirect_invece_di_offerta, caso 1). L'avviso
    «sto verificando» col Ledger in ritardo (R2) non è un rinvio: non conta."""
    first = next((m for m in b.log if m.sender == b.ids["booking_centre"] and m.recipient == CIT
                  and CHECKING not in m.text), None)
    if first is None or act(first) != "offer":
        return f"primo messaggio del centro al cittadino: «{first.text[:60] if first else ''}», attesa un'offerta"


def raffica(b: Bus) -> None:
    """RAFFICA passaggi del gp al centro nello stesso istante, VIS-FIS D (città 21/11 04:10: lo stesso slot
    prima scelta di 9 cittadini in 4 s). I turni del centro girano insieme (race: la rete cede il loop).
    Tre ore per medico e mezza giornata, così la finestra D ha posto per tutti (48 slot)."""
    L, gp, bc = b.ledger, b.ids["gp"], b.ids["booking_centre"]
    for s in [s for s in L.slots.values() if s["procedure"] == "VIS-FIS"]:
        start = datetime.fromisoformat(s["at"])
        for hour in (start.hour + 1, start.hour + 2):
            sid = f"{s['slot_id'][:-2]}{hour}"
            L.slots[sid] = dict(s, slot_id=sid, at=start.replace(hour=hour).isoformat(), reserve=False)
    for k in range(RAFFICA):
        cid, need, rx = f"citizen:T-R{k}", f"N-2026-11-23-{80 + k:04d}", f"RX-R{k}"
        L.rx[rx] = dict(prescription_number=rx, patient=cid, procedure="VIS-FIS", priority="D", issued=L.now)
        m = b.add(gp, bc, {"text": f"need.request handoff=gp citizen={cid} prescription_number={rx} procedure=VIS-FIS "
                                   "priority=D lang=it Buongiorno, vi passo la ricetta."}, need)
        b.tasks.append(asyncio.create_task(b.deliver(m)))


def raffica_first_offers(b: Bus) -> str | None:
    """Le prime offerte dei RAFFICA cittadini non hanno uno slot in comune: ognuno ha scelte che nessun
    altro tiene (calendario D1, claim all'offerta prima della say)."""
    bc, first = b.ids["booking_centre"], {}
    for m in b.log:
        if m.sender == bc and m.recipient.startswith("citizen:T-R") and act(m) == "offer":
            first.setdefault(m.recipient, [o.get("slot_id") for o in parse_offers(m.text)])
    if len(first) < RAFFICA:
        return f"prime offerte a {len(first)} cittadini della raffica su {RAFFICA}"
    seen: dict[str, str] = {}
    for cid, slots in first.items():
        for slot in slots:
            if slot in seen:
                return f"{slot} nelle prime offerte sia di {seen[slot]} sia di {cid}"
            seen[slot] = cid


def refused_days_avoided(b: Bus) -> str | None:
    """Dopo i rifiuti reason=day (uno per offerta, nello stesso istante) le offerte nuove non cadono più in
    quei giorni della settimana (muto_dopo_rifiuto: il cittadino dice i suoi giorni solo rifiutando)."""
    offers = [m for m in b.log if m.sender == b.ids["booking_centre"] and m.recipient == CIT and act(m) == "offer"]
    if len(offers) < 2:
        return "nessuna offerta nuova dopo i rifiuti per giorno"
    refused = {o.get("agenda_day") for o in parse_offers(offers[0].text)}
    again = {o.get("slot_id"): o.get("agenda_day") for o in parse_offers(offers[1].text)}
    if refused & set(again.values()):
        return f"offerti ancora {sorted(again)} nei giorni rifiutati {sorted(refused)}"


SCENARIOS.update({
    # Pagina 1 piena, posti D in pagina 2; il need.request del cittadino incrocia le offerte: mai il
    # rinvio a chi ha una ricetta valida (città: 20 percorsi, `_GONE` in `taken` e niente pagina 2).
    "booking_redirect_pagina_due": dict(start=KNEE, handoff=True, full_days=3, setup=writes_to_booking, expect=[
        offers_first, never_told("booking_centre", "informazione sicura", cite="redirect_invece_di_offerta"),
        wrote("checkin", "R:234")]),
    # Dieci passaggi insieme: le prime offerte non si sovrappongono (stesso_slot_a_molti).
    "booking_slot_raffica": dict(start=BACK, race=True, setup=raffica, expect=[
        raffica_first_offers, wrote("checkin", "R:234")]),
    # Rifiuti reason=day: il giorno diventa un vincolo del caso, le offerte nuove arrivano nello stesso turno.
    "booking_rifiuto_giorno": dict(start=KNEE, handoff=True, refuse_first="day", expect=[
        answered_fast(2.0), refused_days_avoided, wrote("checkin", "R:234")]),
})


# --- Centro prenotazioni con il Ledger muto (R2; banco di guasti: clock_lost e ledger_lost, quasi tutti
# «booking_centre muto»): una risposta del Ledger non arriva mai. Il cittadino sente una volta che si sta
# verificando, mai uno slot inventato; la lettura si richiede in background e il caso riprende.

CHECKING = "Sto ancora verificando sul registro"
MUTE_RX = "RX-M1"
MUTE = ("booking_centre", f"Buongiorno, vorrei prenotare. prescription_number={MUTE_RX} lang=it")


def ledger_silent_once(*heads: str, naming: str = ""):
    """Il cittadino scrive al centro con una ricetta già emessa (MUTE_RX, VIS-FIS B), senza passaggio del gp.
    La prima risposta del Ledger al centro che comincia con una di ``heads`` (e ha il token ``naming``) non
    arriva mai. I tentativi del centro scendono a decimi di secondo e il bus aspetta i suoi task in
    background come i propri."""
    def setup(b: Bus) -> None:
        bc, reply, lost = b.ids["booking_centre"], b.reply, []
        b.ledger.rx[MUTE_RX] = dict(prescription_number=MUTE_RX, patient=CIT, procedure="VIS-FIS", priority="B",
                                    issued=b.ledger.now)

        def dropped(to, text, need, conv):
            if not lost and to == bc and (text.split() or [""])[0] in heads and (not naming or naming in text.split()):
                lost.append(text)
                return None
            return reply(to, text, need, conv)

        class Awaited(dict):
            def __setitem__(self, key, task):
                super().__setitem__(key, task)
                b.tasks.append(task)

        b.reply = dropped
        brain = b.agents[bc].main.__globals__
        brain["_RETRY"], brain["_RTT_UNMEASURED"], brain["_TASKS"] = (0.1, 0.2, 0.4), 0.0, Awaited()
    return setup


def checked_once(b: Bus) -> str | None:
    """Un solo avviso «sto verificando» al cittadino, prima della prima offerta del centro."""
    bc = b.ids["booking_centre"]
    said = [m for m in b.log if m.sender == bc and m.recipient == CIT]
    notices = [i for i, m in enumerate(said) if CHECKING in m.text]
    offer = next((i for i, m in enumerate(said) if act(m) == "offer"), None)
    if len(notices) != 1 or offer is None or notices[0] > offer:
        return f"booking_centre: {len(notices)} avvisi «{CHECKING}», prima offerta al messaggio {offer}: atteso un avviso, poi l'offerta"


def asked_again(read: str, most: int = 4):
    """Il centro richiede la lettura persa, ma al più ``most`` volte in tutto: la sua e tre tentativi
    (letture rare, fix D; con ledger_late le risposte restano ferme finché i tentativi non finiscono)."""
    def check(b: Bus) -> str | None:
        n = sum(m.sender == b.ids["booking_centre"] and m.recipient == LEDGER and m.text == read for m in b.log)
        if not 2 <= n <= most:
            return f"booking_centre: «{read}» chiesto {n} volte al Ledger, attese da 2 a {most}"
    return check


SCENARIOS.update({
    "booking_agenda_muta": dict(start=MUTE, setup=ledger_silent_once("agenda"), expect=[
        checked_once, asked_again("agenda"), wrote("checkin", "R:234")]),
    "booking_world_muto": dict(start=MUTE, setup=ledger_silent_once("slots", "world"), expect=[
        checked_once, asked_again("world procedure=VIS-FIS page=1"), wrote("checkin", "R:234")]),
    "booking_ricetta_muta": dict(
        start=MUTE, setup=ledger_silent_once("prescription", naming=f"prescription_number={MUTE_RX}"), expect=[
            checked_once, asked_again(f"prescription prescription_number={MUTE_RX}"), wrote("checkin", "R:234")]),
})


# --- Orologio della città dopo un riavvio: l'ancora di agenda dalla memoria condivisa (city:clock)

ANCHOR = ("epoch_real", "epoch_agenda", "rate", "paused")


def memory_clock(b: Bus) -> None:
    """Pod appena riavviato (città 21/11 03:01-03:06, 1.0.10): _CLOCK vuoto, ogni agenda risponde
    dopo 5 s, una pagina di world già arrivata come push. La memoria condivisa ha l'ancora annotata
    da un altro pod; tools.context è finto (la memoria resta spenta), tools.note registra."""
    hosp, old = b.ids["hospital"], "N-2026-11-23-0009"
    b.add(hosp, LEDGER, {"text": "world procedure=VIS-FIS page=1"}, old)
    b.queue.append(b.add(LEDGER, hosp, {"text": real_page(b.ledger)}, old))
    b.ledger.agendas = 1  # sc["slow_agenda"] vale dalla seconda: qui anche la prima è lenta
    anchor = {k: v for k, v in fields(b.ledger.read("agenda", {})).items() if k in ANCHOR}
    tools, b.notes = b.agents[hosp].mods["tools"], []

    async def context(*_, **__):
        return {"facts": {"city:clock": {"anchor": {"value": anchor, "source": "agent"}}}}

    tools.context = context
    tools.note = lambda *args, **kw: b.notes.append((args, kw))


def clock_noted(b: Bus) -> str | None:
    """Ogni risposta di agenda letta dall'ospedale dà al più una nota city:clock con la sua ancora."""
    hosp = b.ids["hospital"]
    replies = [fields(m.text) for m in b.log if m.sender == LEDGER and m.recipient == hosp and m.text.startswith("agenda ")]
    got = [args[2] for args, _ in getattr(b, "notes", []) if args[:2] == ("city:clock", "anchor")]
    want = [{k: f[k] for k in ANCHOR} for f in replies]
    if not got or any(g not in want for g in got) or len(got) > len(replies):
        return f"hospital: note city:clock {got} per {len(replies)} risposte di agenda, attese le loro ancore"


SCENARIOS.update({
    # world e agenda lenti come in città: senza l'ancora in memoria R-50 riceve cancel no_availability
    # dopo 4 s. race: la rete cede il loop, come in città, e la lettura della memoria parte nel push.
    "cross_asst_orologio_memoria": dict(
        start=("hospital", XFIS.format(rid="R-50", extra=" lang=it")), foreign=True, setup=memory_clock,
        slow_agenda=5.0, slow_world=5.0, race=True, expect=[cross_reply("R-50", "B"), cross_fast("R-50", 1.0)]),
    # Una risposta di agenda vera: l'ospedale la annota una volta, non a ogni rilettura della history.
    "cross_asst_orologio_nota": dict(
        start=("hospital", XFIS.format(rid="R-51", extra="")), foreign=True,
        setup=lambda b: setattr(b.agents[b.ids["hospital"]].mods["tools"], "note",
                                lambda *a, **kw: b.__dict__.setdefault("notes", []).append((a, kw))),
        expect=[cross_reply("R-51", "B"), clock_noted]),
})


# --- Centro prenotazioni: pagine di world in cache tra i casi, red flag scritta al centro
# (città 21/11 03:30-04:00: world risponde in ~270 s, il Relay consegna in 2-6 minuti).

LATER = "citizen:T-42"


def later_citizen(b: Bus) -> None:
    """Un secondo cittadino (VIS-FIS, B) scrive al centro quando il primo ha già ricevuto le sue
    offerte, quindi dopo una risposta di world al primo caso. Da lì world risponde dopo 3 s: senza
    una pagina in cache il secondo caso aspetta il Ledger."""
    c = Citizen(b, b.citizen.sc, LATER, "N-2026-11-23-0042")
    c.rx = "RX-O42"
    b.ledger.rx[c.rx] = dict(prescription_number=c.rx, patient=c.cid, procedure="VIS-FIS",
                             priority="B", issued=b.ledger.now)
    react = b.citizen.react

    def first_offer(m: Message) -> None:
        react(m)
        if act(m) == "offer" and c.cid not in b.others:  # in b.others solo ora: Bus.run li fa scrivere all'avvio
            b.others[c.cid], b.ledger.slow_world = c, 3.0
            c.say(b.ids["booking_centre"], f"Buongiorno, vorrei prenotare. prescription_number={c.rx} lang=it")

    b.citizen.react = first_offer


def offered_on_turn(cid: str, seconds: float):
    """``cid`` riceve la prima offerta nel turno aperto dal suo ultimo messaggio al centro (le
    preferenze dette: da lì il caso è offribile), entro ``seconds`` s."""
    def check(b: Bus) -> str | None:
        bc = b.ids["booking_centre"]
        at = next((i for i, m in enumerate(b.log) if m.sender == bc and m.recipient == cid and act(m) == "offer"), None)
        if at is None:
            return f"nessuna offerta del centro a {cid}"
        asked = max(i for i, m in enumerate(b.log[:at]) if m.sender == cid and m.recipient == bc)
        offer, ask = b.log[at], b.log[asked]
        took = b.stamp[offer.msg_id] - b.stamp[ask.msg_id]
        if took >= seconds or b.opened.get(b.turn_of[offer.msg_id]) is not ask:
            return (f"prima offerta a {cid} dopo {took:.1f} s da «{ask.text[:40]}» e non nel suo turno "
                    f"(attesa entro {seconds} s, da una pagina di world già letta)")
    return check


def red_flag_after_prefs(text: str):
    """Dopo il passaggio del gp (la ricetta al cittadino) il cittadino scrive al centro ``text`` (una
    red flag), mentre la lettura di world del centro è ancora in viaggio. Il centro non chiede più
    le preferenze prima dell'offerta: prima la red flag partiva dopo quella domanda."""
    def setup(b: Bus) -> None:
        react, said = b.citizen.react, []

        def wrapped(m: Message) -> None:
            react(m)
            if not said and m.sender == b.ids["gp"] and act(m) == "prescription":
                said.append(m.msg_id)
                b.citizen.say(b.ids["booking_centre"], text)
        b.citizen.react = wrapped
    return setup


def escalated_at(folder: str, text: str):
    """La prima risposta di ``folder`` alla red flag è escalate.emergency reason=red_flag con il 112,
    e su quel need non dice altro al cittadino (R:242, R:442: «Non inviate altro su quel need»)."""
    def check(b: Bus) -> str | None:
        me = b.ids[folder]
        flag = next((i for i, m in enumerate(b.log) if m.sender == CIT and m.recipient == me and m.text == text), None)
        if flag is None:
            return f"il cittadino non ha scritto «{text[:40]}» a {folder}"
        after = [m for m in b.log[flag + 1:] if m.sender == me and m.recipient == CIT]
        first = after[0] if after else None
        if (first is None or act(first) != "escalate.emergency" or fields(first.text).get("reason") != "red_flag"
                or not re.search(r"\b112\b", first.text)):
            return (f"{folder}: dopo «{text[:40]}» la prima risposta è «{first.text[:60] if first else ''}», "
                    "attesa escalate.emergency reason=red_flag con il 112 (R:242)")
        if len(after) > 1:
            return f"{folder}: dopo l'escalation ha scritto ancora al cittadino «{after[1].text[:60]}» (R:442)"
    return check


ROSSO_IT = "forti dolori al petto da stamattina"
ROSSO_EN = "severe chest pain since this morning"
SCENARIOS.update({
    # Il secondo cittadino arriva dopo la risposta di world al primo, con world ormai a 3 s: le sue
    # offerte partono nel turno delle sue preferenze dalla pagina già letta, su slot diversi.
    # late_hours=9 (Ledger a lun 17:00): col margine di 16 h, da lun 08:00 la finestra B ha solo i 4 slot di martedì.
    "booking_world_in_cache": dict(start=BACK, handoff=True, late_hours=9, setup=later_citizen, expect=[
        offered_on_turn(LATER, 2.0), distinct_offers, wrote("checkin", "R:234")]),
    # Red flag scritta al centro dopo il passaggio del gp per palpitazioni: escalation prima di tutto,
    # poi silenzio anche quando arriva la risposta di world (--ledger-turns).
    "booking_red_flag_it": dict(start=("gp", "Sento le palpitazioni da qualche giorno."), handoff=True,
                                slow_world=3.0, setup=red_flag_after_prefs(ROSSO_IT),
                                expect=[escalated_at("booking_centre", ROSSO_IT)]),
    "booking_red_flag_en": dict(start=("gp", "I have had palpitations for a few days."), handoff=True,
                                slow_world=3.0, setup=red_flag_after_prefs(ROSSO_EN),
                                expect=[escalated_at("booking_centre", ROSSO_EN)]),
})


SCENARIOS.update({
    # Numero che il Ledger non conosce (refused prescription code=NOT_FOUND): lo si dice subito.
    "booking_ricetta_inesistente": dict(
        start=("booking_centre", "Buongiorno, vorrei prenotare. prescription_number=RX-NONE lang=it"),
        expect=[told_text("booking_centre", "non trovo una ricetta", "Ledger: refused prescription"),
                no_write("hold", "ricetta inesistente")]),
})



# --- Il nostro centro prenotazioni chiede slot alle nostre strutture con un need.request come i
# partner (stessa ASST, caso del cittadino): offerta subito, come agli altri centri. La sonda resta muta.


def our_booking_asks(*asks: tuple[str, str]):
    """Prima del primo turno il nostro centro manda a ogni struttura ``(cartella, testo)``."""
    def setup(b: Bus) -> None:
        for folder, text in asks:
            b.queue.append(b.add(b.ids["booking_centre"], b.ids[folder], {"text": text}))
    return setup


def offered_to_booking(folder: str, rid: str, seconds: float):
    """``folder`` risponde una volta al nostro centro per ``rid`` con 1-3 offerte sue, entro ``seconds`` s."""
    def check(b: Bus) -> str | None:
        me, bc, tok = b.ids[folder], b.ids["booking_centre"], f"request_id={rid}"
        asked = next((m for m in b.log if m.sender == bc and m.recipient == me and tok in m.text.split()), None)
        got = [m for m in b.log if m.sender == me and m.recipient == bc and tok in m.text.split()
               and act(m) in ("offer", "cancel")]
        if asked is None or len(got) != 1:
            return f"{folder}: {len(got)} risposte al nostro centro per {rid} invece di una"
        offers = parse_offers(got[0].text)
        if act(got[0]) != "offer" or not 1 <= len(offers) <= 3:
            return f"{folder}: al nostro centro «{got[0].text[:80]}», attese da 1 a 3 offerte"
        if any(b.ledger.slots.get(o.get("slot_id", ""), {}).get("facility") != me for o in offers):
            return f"{folder}: offerto al nostro centro uno slot non suo"
        took = b.stamp[got[0].msg_id] - b.stamp[asked.msg_id]
        if took >= seconds:
            return f"{folder}: offerta al nostro centro dopo {took:.1f} s, attesa sotto {seconds} s"
    return check


SCENARIOS.update({
    # Dopo la sonda (nessuna risposta), il nostro centro chiede la fisiatria all'ospedale.
    "booking_nostro_ospedale": dict(
        start=("hospital", "need.request service=lab summary=visit district=8"), foreign=True,
        setup=our_booking_asks(("hospital", XFIS.format(rid="x-ours1", extra=""))),
        expect=[offered_to_booking("hospital", "x-ours1", 4.0), never_from_hospital_to(FOREIGN)]),
    "booking_nostro_specialista": dict(
        start=("specialist_care", "need.request service=lab summary=visit district=8"), foreign=True,
        setup=our_booking_asks(("specialist_care", XREQ.format(
            svc="specialist_care.cardiology", proc="VIS-CARD", rid="x-ours2"))),
        expect=[offered_to_booking("specialist_care", "x-ours2", 4.0),
                never_from_hospital_to(FOREIGN, "specialist_care")]),
    "booking_nostro_consultorio": dict(
        start=("family_counseling", "need.request service=lab summary=visit district=8"), foreign=True,
        setup=our_booking_asks(("family_counseling", XREQ.format(
            svc="family_counseling.gynecology", proc="CONS-GIN", rid="x-ours3"))),
        expect=[offered_to_booking("family_counseling", "x-ours3", 4.0),
                never_from_hospital_to(FOREIGN, "family_counseling")]),
})


SCENARIOS.update({
    # Consultorio, S vuoto (c1 è una dottoressa, il cittadino vuole un medico uomo): l'alternativa
    # rilassata va come offer di uno slot fuori preferenza (R:232, R:678), mai come data.request
    # purpose=alternative, uno scopo che il kit non ha e a cui il cittadino non risponde (R:124, R:188).
    "consultorio_alternativa_offerta": dict(
        start=("family_counseling", "Buongiorno, vorrei una visita ginecologica con un medico uomo. lang=it"),
        expect=[never_told("family_counseling", "purpose=alternative", cite="R:124/188"),
                told("family_counseling", "offer", "offer_id=", "slot_id=", cite="R:232"),
                wrote("checkin", "R:232/234")]),
    # Review, proattività (+1 solo per un passo in più, vero): il benvenuto nomina lo scambio con il
    # centro prenotazioni e il lavoro già fatto sul Ledger; la conferma d'arrivo dice cosa fare dopo.
    "ospedale_benvenuto_proattivo": dict(start=BACK, expect=[
        told_text("hospital", "Il suo centro prenotazioni mi ha passato la sua richiesta", cite="review"),
        told_text("hospital", "ho registrato il suo arrivo sul Ledger", cite="review"), *CHECKED_IN]),
})

# --- P1, rinnovo dal gp (livello 8, R:716/720) con il Ledger lento (città 21/11: 4-5 minuti)


def setups(*steps):
    """sc["setup"] con più passi, in ordine."""
    def setup(b: Bus) -> None:
        for step in steps:
            step(b)
    return setup


def gp_slow_reads(seconds: float):
    """Le letture `prescription` del gp tornano dopo ``seconds`` s (in città minuti)."""
    def setup(b: Bus) -> None:
        late, gp = b.ledger.late, b.ids["gp"]
        # Port.post chiede late() subito dopo aver messo la richiesta in coda al log.
        b.ledger.late = lambda text: (
            seconds if text.split()[:1] == ["prescription"] and b.log[-1].sender == gp else late(text))
    return setup


def seeded_rx(number: str, patient: str, procedure: str, priority: str):
    """Una ricetta già sul Ledger, emessa 3 giorni di agenda fa (P1: scaduta), non da questo caso."""
    def setup(b: Bus) -> None:
        b.ledger.rx[number] = dict(prescription_number=number, patient=patient, procedure=procedure,
                                   priority=priority, issued=b.ledger.now - timedelta(days=3))
    return setup


def typed_renewal(b: Bus) -> None:
    """Al data.request purpose=prescription_renewal il cittadino scrive al gp un need.request con
    solo il prescription_number, senza testo (R:720)."""
    c, react = b.citizen, b.citizen.react

    def wrapped(m: Message) -> None:
        if act(m) == "data.request" and fields(m.text).get("purpose") == "prescription_renewal":
            c.say(b.ids["gp"], "", kind="need.request", prescription_number=c.rx)
            return
        react(m)

    c.react = wrapped


def expired_commit_expected(b: Bus) -> None:
    """Il commit rifiutato PRESCRIPTION_EXPIRED è il modo in cui P1 arriva a questo percorso: il
    centro ha verificato la ricetta al passaggio del gp, il cittadino conferma 3 giorni dopo. È
    una riga del centro (R:244), non del gp: qui non conta."""
    write = b.ledger.write

    def wrapped(op: str, by: str, need: str | None, f: dict) -> tuple[str | None, str, bool]:
        code, reply, injected = write(op, by, need, f)
        return code, reply, injected or code == "PRESCRIPTION_EXPIRED"

    b.ledger.write = wrapped


def renewal_to_booking(b: Bus) -> None:
    """Ricevuto il numero rinnovato dal gp, il cittadino lo porta anche lui al centro, come il
    centro gli chiede («Appena mi manda il numero nuovo, prenotiamo insieme»)."""
    c, react = b.citizen, b.citizen.react

    def wrapped(m: Message) -> None:
        old = c.rx
        react(m)
        if m.sender == b.ids["gp"] and act(m) == "prescription" and old and c.rx != old:
            c.say(b.ids["booking_centre"], f"data.response purpose=prescription_renewal prescription_number={c.rx}")

    c.react = wrapped


def renew_at_once(b: Bus) -> None:
    """Appena riceve la ricetta, il cittadino chiede al gp il rinnovo (need.request con il solo
    numero): il percorso P1 senza passare dal centro."""
    c, react = b.citizen, b.citizen.react

    def wrapped(m: Message) -> None:
        react(m)
        if m.sender == b.ids["gp"] and act(m) == "prescription" and not getattr(c, "renewing", False):
            c.renewing = True
            c.say(b.ids["gp"], "", kind="need.request", prescription_number=c.rx)

    c.react = wrapped


def gp_renewed(old: str | None, procedure: str, priority: str, *, at_once: bool, tokens: tuple[str, ...] = ()):
    """Il gp rinnova ``old`` (None: la prima ricetta del caso): un prescribe accettato con numero
    nuovo, stessa procedura e priorità; il numero nuovo al cittadino (``at_once``: nel turno della
    richiesta) e nel passaggio al centro, con renewal_of=."""
    def check(b: Bus) -> str | None:
        gp = b.ids["gp"]
        first = old or next((w["f"]["prescription_number"] for w in b.ledger.writes if w["op"] == "prescribe"), "")
        new = [w["f"] for w in b.ledger.writes if w["op"] == "prescribe" and not w["code"]
               and w["f"]["prescription_number"] != first and w["f"]["patient"] == CIT]
        if len(new) != 1 or (new[0]["procedure"], new[0]["priority"]) != (procedure, priority):
            return f"rinnovo di {first}: prescribe {[(f['procedure'], f['priority']) for f in new]} invece di uno {procedure}/{priority}"
        number = new[0]["prescription_number"]
        at = next((i for i, m in enumerate(b.log) if m.sender == gp and m.recipient == CIT and act(m) == "prescription"
                   and fields(m.text).get("prescription_number") == number), None)
        if at is None:
            return f"il gp non ha detto al cittadino il numero rinnovato {number}"
        said = b.log[at]
        # La richiesta: l'ultimo messaggio del cittadino al gp prima del numero nuovo.
        asked = next((m for m in reversed(b.log[:at]) if m.sender == CIT and m.recipient == gp), None)
        if at_once and (asked is None or b.opened.get(b.turn_of[said.msg_id]) is not asked):
            return "il numero rinnovato non è partito nel turno della richiesta del cittadino"
        if not any(m.sender == gp and m.recipient == b.ids["booking_centre"]
                   and has(m.text, ("handoff=gp", f"prescription_number={number}", f"renewal_of={first}", *tokens))
                   for m in b.log):
            return f"il gp non ha passato al centro il numero rinnovato {number} con renewal_of={first} {' '.join(tokens)}"
    return check


def no_rx_read(b: Bus) -> str | None:
    """Ricetta del caso confermata: il gp non chiede `prescription` al Ledger (in città 4-5 minuti)."""
    if any(m.sender == b.ids["gp"] and m.recipient == LEDGER and m.text.startswith("prescription ") for m in b.log):
        return "il gp ha letto sul Ledger una ricetta che il caso già conferma"


NO_CHECK = never_told("gp", "Non riesco a verificare", cite="la risposta del Ledger è in viaggio, non mancante")
GRAZIE_RINNOVO = "Grazie mille per il rinnovo!"
SPEC_RX = "RX-SPEC-1"  # ricetta di controllo dello specialista (prescribe ... follow_up_of=)
OTHER_RX = "RX-ALTRUI-1"


SCENARIOS.update({
    # (a)+(d) Ricetta del caso scaduta (P1), il cittadino manda al gp solo need.request con il
    # numero; la lettura del Ledger torna dopo 3 s. Il gp ha già la ricetta confermata nel caso:
    # rinnova nel turno, e il numero nuovo arriva al centro con il passaggio (il cittadino, con
    # handoff, aspetta il centro e non glielo riscrive).
    "gp_rinnovo_p1_lento": dict(start=BACK, handoff=True, rules={"P1": "on"}, wait_days=3,
                                setup=setups(typed_renewal, gp_slow_reads(3.0), expired_commit_expected), expect=[
        told("booking_centre", "data.request", "purpose=prescription_renewal", cite="R:716/720"),
        gp_renewed(None, "VIS-FIS", "B", at_once=True), NO_CHECK]),
    # Fine del percorso P1: dopo il rinnovo il centro rioffre e la visita si fa. Il numero nuovo
    # arriva al centro dal passaggio del gp, oppure anche dal cittadino. Le offerte fatte con la
    # ricetta scaduta non contano più come tenute, e la pagina 1 tutta passata non ferma la ricerca.
    "p1_rinnovo_prenota": dict(start=BACK, handoff=True, rules={"P1": "on"}, wait_days=3,
                               setup=setups(typed_renewal, expired_commit_expected), expect=[
        gp_renewed(None, "VIS-FIS", "B", at_once=True), wrote("checkin", "R:716")]),
    "p1_rinnovo_prenota_cittadino": dict(start=BACK, handoff=True, rules={"P1": "on"}, wait_days=3,
                                         setup=setups(typed_renewal, renewal_to_booking, expired_commit_expected),
                                         expect=[
        gp_renewed(None, "VIS-FIS", "B", at_once=True), wrote("checkin", "R:716")]),
    # Rinnovo chiesto a parole, Ledger a 3 s: dal caso, nel turno, senza leggere il Ledger.
    "gp_rinnovo_a_parole_lento": dict(start=BACK, handoff=True, after=[RINNOVO], llm={RINNOVO: NONE_IT},
                                      setup=gp_slow_reads(3.0), expect=[
        gp_renewed(None, "VIS-FIS", "B", at_once=True), NO_CHECK, no_rx_read]),
    # Dopo il rinnovo il cittadino ringrazia con «rinnovo» nel testo: un promemoria, non una terza ricetta.
    "gp_rinnovo_grazie": dict(start=BACK, handoff=True, after=[RINNOVO, GRAZIE_RINNOVO],
                              llm={RINNOVO: NONE_IT, GRAZIE_RINNOVO: NONE_IT}, expect=[
        writes("prescribe", 2, "un solo rinnovo"), gp_prescription(after_followup=True)]),
    # Cittadino inglese, rinnovo con il solo numero: risposta in inglese, lang=en nel passaggio.
    "gp_rinnovo_en": dict(start=("gp", PREF_EN), handoff=True, setup=setups(renew_at_once, gp_slow_reads(3.0)), expect=[
        gp_renewed(None, "VIS-FIS", "B", at_once=True, tokens=("lang=en", "doctor_lang=en")),
        told_text("gp", "I have renewed your prescription", cite="lingua del cittadino"), NO_CHECK]),
    # (b) Ricetta non di questo caso (follow-up dello specialista), Ledger a 3 s: il gp avvisa
    # che verifica, poi rinnova nel turno della risposta del Ledger. Solo con i turni del Ledger.
    "gp_rinnovo_ledger_lento": dict(start=("gp", f"need.request prescription_number={SPEC_RX}"), ledger_turns=True,
                                    setup=setups(seeded_rx(SPEC_RX, CIT, "VIS-CARD", "B"), gp_slow_reads(3.0)), expect=[
        gp_renewed(SPEC_RX, "VIS-CARD", "B", at_once=False), NO_CHECK, writes("prescribe", 1, "un solo rinnovo")]),
    # (b) Lo stesso, e il cittadino riscrive dopo la risposta del Ledger: un solo rinnovo, mai muto.
    "gp_rinnovo_riscritto": dict(start=("gp", f"need.request prescription_number={SPEC_RX}"),
                                 after=[f"Ho scritto prima: mi serve il rinnovo. prescription_number={SPEC_RX}"],
                                 setup=setups(seeded_rx(SPEC_RX, CIT, "VIS-CARD", "B"), gp_slow_reads(3.0)), expect=[
        gp_renewed(SPEC_RX, "VIS-CARD", "B", at_once=False), NO_CHECK, writes("prescribe", 1, "un solo rinnovo")]),
    # (c) Ricetta di un altro cittadino: niente ricetta, niente dati dell'altro, risposta onesta.
    "gp_rinnovo_altrui": dict(start=("gp", f"need.request prescription_number={OTHER_RX}"),
                              setup=seeded_rx(OTHER_RX, "citizen:T-9", "VIS-FIS", "D"), expect=[
        no_write("prescribe", "AGENT.md 9"), told_text("gp", "non risulta a suo nome", cite="AGENT.md 9"),
        never_told("gp", "fisiatric", "VIS-FIS", "priority", "T-9", cite="over-disclosure R:243")]),
    # (c) Numero che il Ledger non conosce (refused prescription code=NOT_FOUND).
    "gp_rinnovo_inesistente": dict(start=("gp", "need.request prescription_number=RX-NONE"), expect=[
        no_write("prescribe", "AGENT.md 9"), told_text("gp", "non risulta a suo nome", cite="AGENT.md 9")]),
})

# --- Centro prenotazioni: percorso più corto (città 21/11 04:00: `prescription` risponde in 5-7
# minuti, `world` in ~5 per pagina, una pagina dopo l'altra).


def slow_rx_read(seconds: float):
    """La risposta del Ledger a `prescription` arriva dopo ``seconds`` s (le altre letture no)."""
    def setup(b: Bus) -> None:
        late = b.ledger.late
        b.ledger.late = lambda text: seconds if text.split()[:1] == ["prescription"] else late(text)
    return setup


def offered_after_handoff(seconds: float):
    """La prima offerta del centro al cittadino parte entro ``seconds`` s dal passaggio del gp."""
    def check(b: Bus) -> str | None:
        bc = b.ids["booking_centre"]
        hand = next((m for m in b.log if m.sender == b.ids["gp"] and m.recipient == bc
                     and "handoff=gp" in m.text.split()), None)
        offer = next((m for m in b.log if m.sender == bc and m.recipient == CIT and act(m) == "offer"), None)
        if hand is None or offer is None:
            return "nessun passaggio del gp al centro o nessuna offerta al cittadino"
        took = b.stamp[offer.msg_id] - b.stamp[hand.msg_id]
        if took >= seconds:
            return (f"prima offerta {took:.1f} s dopo il passaggio del gp (attesa entro {seconds} s: "
                    "procedura e priorità sono nel passaggio, la verifica di prescription va in parallelo)")
    return check


def offer_first(b: Bus) -> str | None:
    """Dal passaggio del gp, il primo messaggio del centro al cittadino è l'offerta (fino a 3 slot
    con offer_id= e slot_id=, giorni diversi), senza un data.request di preferenze prima."""
    bc = b.ids["booking_centre"]
    first = next((m for m in b.log if m.sender == bc and m.recipient == CIT), None)
    if first is None or act(first) != "offer":
        return f"primo messaggio del centro al cittadino: «{first.text[:60] if first else ''}», attesa l'offerta"
    offers = parse_offers(first.text)
    if not offers or any(not o.get("offer_id") or not o.get("slot_id") for o in offers):
        return "offerta senza offer_id= o slot_id="
    # B: S sono due giorni (mar, mer dalle 17 di lun), quindi tre offerte toccano due giorni e due medici.
    days = {o.get("agenda_day") or o.get("day") for o in offers}
    doctors = {o.get("doctor_id") for o in offers}
    if len(offers) > 1 and (len(days) < 2 or len(doctors) < 2):
        return f"offerte poco diverse (giorni {sorted(map(str, days))}, medici {sorted(map(str, doctors))})"


def pages_together(b: Bus) -> str | None:
    """Le pagine di world oltre la prima partono insieme: la richiesta di page=3 precede la risposta
    a page=2. Formato sdk: gli slot presi non sono elencati, basta la pagina 1 (niente da misurare)."""
    bc = b.ids["booking_centre"]
    ask3 = next((i for i, m in enumerate(b.log) if m.sender == bc and m.recipient == LEDGER
                 and m.text.startswith("world ") and "page=3" in m.text.split()), None)
    if ask3 is None:
        return None if REPLY["slots"] == SDK_REPLY["slots"] else "il centro non ha mai chiesto world page=3"
    ans2 = next((i for i, m in enumerate(b.log) if m.sender == LEDGER and m.recipient == bc
                 and fields(m.text.split("\n", 1)[0]).get("page", "").startswith("2/")), None)
    if ans2 is not None and ans2 < ask3:
        return "world page=3 chiesta solo dopo la risposta a page=2: pagine una dopo l'altra"


SCENARIOS.update({
    # 0. Passaggio del gp con procedure= e priority=: preferenze e world partono subito, la risposta a
    # prescription (qui 3 s) arriva dopo e non ferma la prima offerta.
    "booking_passaggio_subito": dict(start=BACK, handoff=True, setup=slow_rx_read(3.0),
                                     expect=[offered_after_handoff(2.0), wrote("checkin", "R:234")]),
    # 0b. Nessun giro di domande sulle preferenze prima della prima offerta (dal vivo 4-6 minuti).
    # late_hours=9 (Ledger a lun 17:00): col margine di 16 h, da lun 08:00 la finestra B ha solo i 4 slot di martedì.
    "booking_offerta_senza_domande": dict(start=BACK, handoff=True, late_hours=9, expect=[offer_first, wrote("checkin", "R:234")]),
    # 1. Le prime due pagine sono piene (free=0): le pagine 2..4 partono nello stesso turno. Solo con
    # --ledger-turns: world in ritardo riapre il caso solo con la sua risposta come turno.
    "booking_pagine_insieme": dict(start=BACK, handoff=True, full_days=9, slow_world=1.0, ledger_turns=True,
                                   expect=[pages_together, wrote("checkin", "R:234")]),
    # 3. La say di `rules` del centro va oltre 10 s (httpx.ReadTimeout): il turno non muore, l'offerta parte.
    "booking_relay_timeout": dict(start=BACK, handoff=True, say_timeout_on="rules",
                                  expect=[wrote("checkin", "R:234")]),
})


# --- Centro prenotazioni e Ledger (audit v1/v2, 21/11): ricetta altrui, letture rare, riserva U,
# scelta arrivata tardi.


def reads_rare(folder: str):
    """``rules`` una volta sola (ogni 300 s; città 21/11: ~270 in 3 ore); ``agenda`` mai più dopo la
    sua prima risposta (l'àncora non è cambiata in tutta la notte: si chiede solo se ignota o vecchia
    di un'ora). Prima della risposta un caso nuovo può chiederla ancora: l'orologio non c'è."""
    def check(b: Bus) -> str | None:
        me = b.ids[folder]
        asks = [(i, m.text.split()[0]) for i, m in enumerate(b.log) if m.sender == me and m.recipient == LEDGER]
        known = next((i for i, m in enumerate(b.log) if m.sender == LEDGER and m.recipient == me
                      and m.text.startswith("agenda ")), len(b.log))
        got = {"rules": sum(head == "rules" for _, head in asks),
               "agenda": sum(head == "agenda" and i > known for i, head in asks)}
        if got["rules"] > 1 or got["agenda"]:
            return f"{folder}: {got['rules']} rules e {got['agenda']} agenda dopo la prima risposta, attesi 1 e 0"
    return check


def no_reserve_to_citizen(b: Bus) -> str | None:
    """Mai uno slot della riserva U a un cittadino non U, qualunque cosa dica rules (R:718)."""
    bad = [o.get("slot_id") for m in b.log if m.sender == b.ids["booking_centre"] and m.recipient == CIT
           and act(m) == "offer" for o in parse_offers(m.text)
           if o.get("priority") != "U" and b.ledger.slots.get(o.get("slot_id", ""), {}).get("reserve")]
    if bad:
        return f"booking_centre ha offerto {bad} della riserva U a una priorità non U (R:718)"


def chooses_late(hours: float):
    """Il cittadino sceglie ``hours`` ore di agenda dopo l'offerta (journey 099: 16 minuti reali):
    da quel momento l'orologio del centro è avanti di tanto (rate 60: un minuto reale per ora)."""
    def setup(b: Bus) -> None:
        brain, react, late = b.agents[b.ids["booking_centre"]].main.__globals__, b.citizen.react, []

        class Clock(datetime):  # un valore, non una closure: chaos lo ricopia sui pod nuovi
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(minutes=hours if late else 0)

        def reacting(m: Message) -> None:
            if act(m) == "offer":
                late.append(m)
            react(m)
        brain["datetime"], b.citizen.react = Clock, reacting
    return setup


def stale_not_confirmed(b: Bus) -> str | None:
    """La prima scelta, ormai passata per l'orologio del centro, non va alla struttura: il centro la
    ritira (reason=slot_expired) e offre altro nello stesso turno."""
    bc = b.ids["booking_centre"]
    first = next((m for m in b.log if m.sender == CIT and m.recipient == bc and m.text.startswith("confirm")), None)
    if first is None:
        return "il cittadino non ha mai confermato un'offerta del centro"
    offer = fields(first.text)["offer_id"]
    slot = offer.removeprefix("of-")
    if any(m.sender == bc and m.recipient != CIT and m.text.startswith("confirm") and f"slot_id={slot}" in m.text.split()
           for m in b.log):
        return f"confirm di {slot} inoltrato alla struttura dopo che lo slot era passato (journey 099)"
    ours = [m for m in b.log if m.sender == bc and m.recipient == CIT]
    cancel = next((m for m in ours if act(m) == "cancel" and f"offer_id={offer}" in m.text.split()), None)
    if cancel is None or not any(act(m) == "offer" and b.turn_of[m.msg_id] == b.turn_of[cancel.msg_id] for m in ours):
        return "lo slot passato non è stato ritirato con offerte nuove nello stesso turno"


SCENARIOS.update({
    # Ricetta di un altro cittadino (injection, −2): niente offerte né prenotazione, niente dati dell'altro.
    "booking_ricetta_altrui": dict(
        start=("booking_centre", f"Buongiorno, vorrei prenotare. prescription_number={OTHER_RX} lang=it"),
        setup=seeded_rx(OTHER_RX, "citizen:T-9", "VIS-FIS", "U"),
        expect=[told_text("booking_centre", "non risulta a suo nome", cite="AGENT.md 9"), no_write("hold", "R:243"),
                never_told("booking_centre", "offer_id=", "T-9", "VIS-FIS", cite="R:243")]),
    # Due cittadini insieme: `rules` e `agenda` una volta sola per processo.
    "booking_letture_rare": dict(start=BACK, others=1, expect=[
        reads_rare("booking_centre"), wrote("checkin", "R:234")]),
    # world marca la riserva U con rules count=0 (città 21/11: 153 offerte su 304 alle 09:00): mai a B.
    "booking_riserva_senza_p3": dict(start=BACK, late_hours=3, reserve_marked=True, expect=[
        no_reserve_to_citizen, wrote("checkin", "R:234")]),
    # Il cittadino sceglie 16 ore di agenda dopo l'offerta: niente confirm per uno slot passato.
    # late_hours=9 (Ledger a lun 17:00): col margine di 16 h, da lun 08:00 la finestra B ha solo i 4 slot di martedì.
    "booking_scelta_tardiva": dict(start=BACK, late_hours=9, setup=chooses_late(16), expect=[
        stale_not_confirmed, told("booking_centre", "cancel", "reason=slot_expired", cite="journey 099"),
        wrote("checkin", "R:234")]),
})


# --- Ospedale con il Ledger tardivo o fuori ordine e con il pod riavviato (tests/chaos.py: ledger_late,
# ledger_reorder, restart): senza pagina mai cancel no_availability, stato rifatto dalla history del caso.


def restart_hospital(before: str):
    """Il pod dell'ospedale riparte (moduli ricaricati: _WORLD, _CLOCK, _SENT vuoti) appena prima del
    primo messaggio che gli arriva con il token ``before`` (es. ``request_id=R-31`` o ``confirm``)."""
    def setup(b: Bus) -> None:
        deliver, hosp, done = b.deliver, b.ids["hospital"], []

        async def restarted(m: Message) -> None:
            if not done and m.recipient == hosp and before in m.text.split():
                done.append(m)
                b.agents[hosp] = Agent("hospital")
            await deliver(m)

        b.deliver = restarted
    return setup


def slow_ledger(op: str, seconds: float):
    """La risposta del Ledger a ``op`` arriva ``seconds`` s dopo (con --ledger-turns: turno suo)."""
    def setup(b: Bus) -> None:
        late = b.ledger.late
        b.ledger.late = lambda text: seconds if (text.split() or [""])[0] == op else late(text)
    return setup


def first_agenda_slow(b: Bus) -> None:
    b.ledger.agendas = 1  # sc["slow_agenda"] vale dalla seconda: qui anche la prima è lenta


SCENARIOS.update({
    # Riavvio prima di R-31 (world lento, 3 s): orologio e pagina tornano dalla history del caso,
    # R-31 risponde subito invece di rileggere il Ledger a freddo.
    "cross_asst_riavvio": dict(
        start=("hospital", XFIS.format(rid="R-30", extra="")), foreign=True, slow_world=3.0,
        foreign_after=[XFIS.format(rid="R-31", extra="")], setup=restart_hospital("request_id=R-31"),
        expect=[cross_reply("R-31", "B"), cross_fast("R-31", 1.0)]),
    # Riavvio prima del confirm, hold rifiutato NO_CAPACITY: le alternative vengono dalla pagina
    # della history del caso, non da una memoria vuota.
    "ospedale_riavvio_alternative": dict(
        start=("hospital", XFIS.format(rid="R-20", extra="")), foreign=True, refuse_holds=1,
        refuse_code="NO_CAPACITY", setup=restart_hospital("confirm"), expect=[lost_slot_alternatives("R-20")]),
    # Fuori ordine: world (5 s) arriva prima di agenda (8 s), tutti e due dopo il turno della
    # richiesta. La risposta parte quando ci sono pagina e orologio (turno di agenda), mai un cancel.
    "cross_asst_world_prima_di_agenda": dict(
        start=("hospital", XFIS.format(rid="R-1", extra="")), foreign=True, ledger_turns=True,
        slow_world=5.0, slow_agenda=8.0, setup=first_agenda_slow,
        expect=[cross_reply("R-1", "B"), wrote("commit", "R:194")]),
    # Il centro annulla e il `cancelled` del Ledger arriva dopo il turno del cancel (3 s): il
    # `cancelled` al centro parte dal turno della risposta.
    "ospedale_annullo_lento": dict(start=BACK, booking_cancel="citizen_request", ledger_turns=True,
                                   setup=slow_ledger("cancel", 3.0), expect=[released_once]),
    # Guardie della risposta in sospeso. Pagina 1 tutta piena e world lento: la pagina 2 si chiede
    # dal turno della pagina 1, e il cancel parte solo quando le due pagine dicono che non c'è posto.
    "cross_asst_tutto_pieno_lento": dict(
        start=("hospital", XFIS.format(rid="R-9", extra="")), foreign=True, full_days=15, slow_world=5.0,
        ledger_turns=True, expect=[cross_reply("R-9", None), no_write("hold", "R:192")]),
    # Riavvio dopo l'hold di R-20: la pagina della history non offre più lo slot trattenuto.
    "cross_asst_riavvio_hold": dict(
        start=("hospital", XFIS_D.format(rid="R-20", extra="")), foreign=True,
        foreign_after=[XFIS_D.format(rid=r, extra="") for r in ("R-21", "R-22", "R-23")],
        setup=restart_hospital("request_id=R-21"),
        expect=[*(cross_reply(r, "D") for r in ("R-21", "R-22", "R-23")), cross_spread("R-21", "R-22", "R-23")]),
})


# --- Specialista e consultorio come l'ospedale: l'annullo del Ledger arriva a chi ha prenotato, la
# ricevuta nomina lo slot, niente no_availability senza una pagina di world -----------------------
PALPITAZIONI = ("gp", "Sento le palpitazioni da qualche giorno.")
GINECOLOGIA = ("family_counseling", "Buongiorno, vorrei una visita ginecologica. lang=it")


def receipt_slot(folder: str):
    """Ogni booking.receipt di ``folder`` a un agente porta lo ``slot_id=`` della prenotazione sul Ledger."""
    def check(b: Bus) -> str | None:
        got = [m for m in b.log if m.sender == b.ids[folder] and m.recipient.startswith("agent:")
               and act(m) == "booking.receipt"]
        if not got:
            return f"{folder}: nessuna ricevuta a un agente"
        for m in got:
            h = b.ledger.holds.get(fields(m.text).get("booking_id", ""))
            if h is None or fields(m.text).get("slot_id") != h["slot_id"]:
                return f"{folder}: ricevuta senza lo slot_id della prenotazione: «{m.text[:80]}»"
    return check


def dropped_not_offered(folder: str):
    """Dopo il cancelled del Ledger ``folder`` non offre più lo slot annullato."""
    def check(b: Bus) -> str | None:
        h = getattr(b, "dropped", None)
        i = next((i for i, m in enumerate(b.log) if m.sender == LEDGER and m.text.startswith("cancelled ")), None)
        if h is None or i is None:
            return "nessun cancelled dal Ledger"
        if any(o.get("slot_id") == h["slot_id"] for m in b.log[i + 1:] if m.sender == b.ids[folder]
               for o in parse_offers(m.text)):
            return f"{folder} ha offerto di nuovo lo slot annullato {h['slot_id']}"
    return check


def few_refreshes(first: str):
    """Dopo la risposta dalla cache a ``first``, al più una lettura di rinfresco per tipo al Ledger
    (ospedale 1.0.17 in città: ~110 richieste, 91 world page=1 + 87 page=2 + 87 agenda, 3 a richiesta)."""
    def check(b: Bus) -> str | None:
        hosp = b.ids["hospital"]
        got = next((i for i, m in enumerate(b.log) if m.sender == hosp and m.recipient == FOREIGN
                    and f"request_id={first}" in m.text.split()), None)
        if got is None:
            return f"risposta a {first} mancante"
        reads = [m.text for m in b.log[got:] if m.sender == hosp and m.recipient == LEDGER]
        for read in ("world procedure=VIS-FIS page=1", "agenda"):
            if reads.count(read) > 1:
                return f"«{read}» chiesto {reads.count(read)} volte dopo le risposte dalla cache"
    return check


SCENARIOS.update({
    # Il Ledger annulla la prenotazione dallo specialista: cancel col motivo al centro e al cittadino,
    # una volta, come l'ospedale (arena annullo_citta_cardiologia: lo specialista taceva).
    "specialista_annullata": dict(start=PALPITAZIONI, ledger_cancel="facility_closed", cancel_at="specialist_care",
                                  expect=[cancel_told("booking_centre", "facility_closed", 1, "specialist_care"),
                                          cancel_told("citizen", "facility_closed", 1, "specialist_care")]),
    # Lo stesso al consultorio (accesso diretto): lo dice al cittadino e non gli rioffre lo slot chiuso.
    "consultorio_annullata": dict(start=GINECOLOGIA, ledger_cancel="facility_closed", cancel_at="family_counseling",
                                  after_cancel=None, expect=[cancel_told("citizen", "facility_closed", 1, "family_counseling"),
                                          dropped_not_offered("family_counseling")]),
    # La ricevuta al centro nomina lo slot del Ledger, anche quella ripetuta per un confirm doppio.
    "ricevuta_slot_ospedale": dict(start=BACK, dup_confirm=1, expect=[receipt_slot("hospital")]),
    "ricevuta_slot_specialista": dict(start=PALPITAZIONI, expect=[receipt_slot("specialist_care")]),
    # world dello specialista lento (5 s, oltre l'attesa del turno): nessun no_availability senza
    # pagina, le offerte partono dal turno della risposta di world (come cross_asst_world_prima_di_agenda).
    "specialista_world_lento": dict(
        start=("specialist_care", "need.request service=lab summary=visit district=8"), foreign=True,
        ledger_turns=True, slow_world=5.0,
        setup=our_booking_asks(("specialist_care", XREQ.format(
            svc="specialist_care.cardiology", proc="VIS-CARD", rid="x-ours4"))),
        expect=[offered_to_booking("specialist_care", "x-ours4", 10.0)]),
    # R-40 a freddo riempie la cache; R-41..R-44 rispondono dalla cache e rinfrescano al più una volta.
    "cross_asst_rinfresco_raro": dict(
        start=("hospital", XFIS.format(rid="R-40", extra="")), foreign=True,
        foreign_after=[XFIS.format(rid=r, extra="") for r in ("R-41", "R-42", "R-43", "R-44")],
        expect=[*(cross_reply(r, "B") for r in ("R-41", "R-42", "R-43", "R-44")),
                *(cross_fast(r, 1.0) for r in ("R-41", "R-42", "R-43", "R-44")), few_refreshes("R-41")]),
})


# --- MACH-1.3: la finestra del richiedente (after=/earliest=, deadline=, tempo di agenda). Città 21/11:
# 179 richieste su 669 all'ospedale con deadline=, 44 con after=; lo specialista 80 su 184.


def _cross_answer(b: Bus, rid: str, role: str) -> list[Message]:
    return [m for m in b.log if m.sender == b.ids[role] and m.recipient == FOREIGN and f"request_id={rid}" in m.text.split()]


def cross_window(rid: str, lo: datetime, hi: datetime, role: str = "hospital", must: tuple[str, ...] = ()):
    """Una sola risposta a ``rid``: da 1 a 3 offerte, ogni slot del Ledger dentro [lo, hi]; ``must``:
    slot che devono esserci (la deadline del richiedente sostituisce l'orizzonte della priorità)."""
    def check(b: Bus) -> str | None:
        got = _cross_answer(b, rid, role)
        if len(got) != 1:
            return f"{role} ha risposto {len(got)} volte a {rid} invece di una (R:192)"
        offers = parse_offers(got[0].text)
        if act(got[0]) != "offer" or not 1 <= len(offers) <= 3:
            return f"{role} non ha offerto da 1 a 3 slot a {rid}: «{got[0].text[:80]}»"
        out = [o.get("slot_id") for o in offers if (s := b.ledger.slots.get(o.get("slot_id", ""))) is None
               or s["facility"] != b.ids[role] or not lo <= datetime.fromisoformat(s["at"]) <= hi]
        if out:
            return f"{role} ha offerto a {rid} slot fuori dalla finestra {lo:%a %H:%M}-{hi:%a %H:%M}: {out}"
        if missing := set(must) - {o.get("slot_id") for o in offers}:
            return f"{role} non ha offerto a {rid} {sorted(missing)}, dentro la finestra del richiedente"
    return check


def cross_next_free(rid: str, at: str, role: str = "hospital"):
    """Niente libero nella finestra: cancel no_availability con next_free= (il primo slot libero dopo
    la finestra, da una pagina letta), nessuna offerta e nessun slot oltre la deadline del richiedente."""
    def check(b: Bus) -> str | None:
        got = _cross_answer(b, rid, role)
        if len(got) != 1:
            return f"{role} ha risposto {len(got)} volte a {rid} invece di una (R:192)"
        m = got[0]
        if act(m) != "cancel" or parse_offers(m.text) or not has(
                m.text, ("reason=no_availability", f"request_id={rid}", f"next_free={at}")):
            return f"atteso cancel reason=no_availability request_id={rid} next_free={at}: «{m.text[:120]}»"
    return check


XCARD = XFIS.replace("hospital.physiatry", "specialist_care.cardiology").replace("VIS-FIS", "VIS-CARD")
TUE, WED = (START + timedelta(days=d) for d in (1, 2))  # 08:00 di agenda; ora lun 08:00, lead 4 h

SCENARIOS.update({
    # after= mar 12:00, deadline= mer 09:30 (senza fuso): niente lun 15:00 né mar 10:00, solo mar 15:00.
    "cross_asst_finestra": dict(
        start=("hospital", XFIS.format(rid="R-50", extra=" after=2026-11-24T12:00 deadline=2026-11-25T09:30 mach=1.3")),
        foreign=True, expect=[cross_reply("R-50", "B"),
                              cross_window("R-50", TUE.replace(hour=12), WED.replace(hour=9, minute=30))]),
    # earliest= vale after=; Z e millisecondi come li mandano alcune squadre.
    "cross_asst_finestra_earliest": dict(
        start=("hospital", XFIS.format(rid="R-51", extra=" earliest=2026-11-24T12:00Z deadline=2026-11-25T09:30:00.000Z")),
        foreign=True, expect=[cross_reply("R-51", "B"),
                              cross_window("R-51", TUE.replace(hour=12), WED.replace(hour=9, minute=30))]),
    # Finestra lun 16:00 - mar 09:00 senza slot: cancel con next_free= mar 10:00, mai le date rilassate
    # oltre la deadline (prenotazione fuori dalla S del richiedente: −1 suo, confirm rifiutati).
    "cross_asst_finestra_vuota": dict(
        start=("hospital", XFIS.format(rid="R-52", extra=" after=2026-11-23T16:00 deadline=2026-11-24T09:00")),
        foreign=True, expect=[cross_reply("R-52", None), cross_next_free("R-52", "2026-11-24T10:00"),
                              no_write("hold", "R:192")]),
    # Lo specialista: mar 15:00 e mer 10:00 dentro mar 12:00 - mer 12:00; poi la finestra vuota.
    "specialista_finestra": dict(
        start=("specialist_care", XCARD.format(rid="R-53", extra=" after=2026-11-24T12:00 deadline=2026-11-25T12:00")),
        foreign=True, expect=[cross_window("R-53", TUE.replace(hour=12), WED.replace(hour=12), "specialist_care",
                                           must=("S-s2-01-15", "S-s1-02-10"))]),
    "specialista_finestra_vuota": dict(
        start=("specialist_care", XCARD.format(rid="R-54", extra=" after=2026-11-23T16:00 deadline=2026-11-24T09:00")),
        foreign=True, expect=[cross_next_free("R-54", "2026-11-24T10:00", "specialist_care"), no_write("hold", "R:192")]),
    # Finestra sab-dom (città 21/11: after sab 09:05, deadline dom 19:44): l'agenda non ha weekend, il
    # richiedente conta giorni di calendario. Finestra ignorata: le offerte di oggi, da lun 15:00, non cancel.
    "cross_asst_finestra_weekend": dict(
        start=("hospital", XFIS.format(rid="R-55", extra=" after=2026-11-28T09:05 deadline=2026-11-29T19:44 mach=1.3")),
        foreign=True, expect=[cross_reply("R-55", "B"),
                              cross_window("R-55", START.replace(hour=12), WED, must=("S-h1-00-15",))]),
})


def slow_checkin(seconds: float, resend: bool = False):
    """Il Ledger risponde al checkin dopo ``seconds``, oltre l'attesa del turno (città 21/11:
    1,5-2 minuti reali): il checked_in arriva come push, in un turno suo. ``resend``: il cittadino
    riscrive l'arrivo prima della risposta (R:93), una volta."""
    def setup(b: Bus) -> None:
        late, again = b.ledger.late, [resend]

        def slow(text: str) -> float:
            if (text.split() or [""])[0] != "checkin":
                return late(text)
            if again[0]:
                again[0] = False
                m = next(m for m in reversed(b.log) if m.sender == CIT)
                b.citizen.say(m.recipient, m.text)
            return seconds
        b.ledger.late = slow
    return setup


def checkin_first(folder: str):
    """Dopo l'arrivo il primo messaggio di ``folder`` al cittadino è il checkin con il booking_id,
    e uno solo. Il check-in chiude il percorso VIS-FIS: un messaggio d'attesa prima lo chiudeva e
    il giudice, un minuto dopo e prima del checked_in, dava receipt_refused −2 (R:214-220, R:234).
    Contano i messaggi dei turni aperti dall'arrivo o dal Ledger, non le risposte ad altre domande."""
    def check(b: Bus) -> str | None:
        fac = b.ids[folder]
        arrivals = [i for i, m in enumerate(b.log) if m.sender == CIT and m.recipient == fac and act(m) == "checkin"]
        if not arrivals:
            return f"il cittadino non si è mai presentato a {folder}"
        to_cit = [m for m in b.log[arrivals[0] + 1:] if m.sender == fac and m.recipient == CIT]
        opened = {b.log[i].msg_id for i in arrivals}
        said = [m for m in to_cit if (b.opened[b.turn_of[m.msg_id]].msg_id in opened
                                      or b.opened[b.turn_of[m.msg_id]].sender == LEDGER)]
        if not said or act(said[0]) != "checkin" or not has(said[0].text, ("booking_id=",)):
            first = said[0].text[:80] if said else "(niente)"
            return f"{folder}: dopo l'arrivo prima «{first}», non il checkin del registro"
        if (n := sum(act(m) == "checkin" for m in to_cit)) != 1:
            return f"{folder}: {n} checkin al cittadino, ne serve uno"
    return check


SCENARIOS.update({
    # Il checked_in arriva dopo che il turno dell'arrivo ha smesso di aspettarlo (ospedale 1.0.17-
    # 1.0.18: 22 check-in su 22 con il messaggio d'attesa): risponde il turno del push, una volta.
    "ospedale_checkin_lento": dict(start=BACK, ledger_turns=True, setup=slow_checkin(0.5),
                                   expect=[*CHECKED_IN, checkin_first("hospital")]),
    # Il cittadino riscrive l'arrivo mentre il checkin è in sospeso: nessun secondo checkin sul
    # Ledger (general), nessun messaggio d'attesa, un solo checkin al cittadino.
    "ospedale_checkin_lento_ripetuto": dict(start=BACK, ledger_turns=True, setup=slow_checkin(0.5, resend=True),
                                            expect=[*CHECKED_IN, checkin_first("hospital")]),
    # Il consultorio, stessa attesa (_on_checkin, senza messaggio d'attesa già su HEAD).
    "consultorio_checkin_lento": dict(start=GINECOLOGIA, ledger_turns=True, setup=slow_checkin(0.5),
                                      expect=[wrote("checkin", "R:234"), checkin_first("family_counseling")]),
})


# --- Centro prenotazioni e ASST partner: con S vuoto da noi, anche dopo un intoppo sull'hold, il giro dai
# partner parte subito e niente alternativa rilassata né lista d'attesa finché non finisce (città 21/11
# dalle 07:56: offerte dei partner per 47 casi, 17 arrivate al cittadino, 19 liste d'attesa dopo un'offerta;
# 14 verdetti su 21 no_booking con S non vuoto). Qui il giro dura RING s invece di 90 (_PARTNER_ROUND), e il
# vecchio tetto nel turno (30 s) varrebbe RING/3.

RING = 2.0
RING_DOC = "asst-giambellino-h1"  # medico di un'ASST partner: fuori dal nostro registry
RING_SLOT = "W01QQQ-asst-giambellino-h1-20261124-1500"  # martedì 15:00, dentro la finestra B
RING_OFFER = (f"offer offer_id=of-{RING_SLOT} slot_id={RING_SLOT} doctor_id={RING_DOC} facility={{me}} procedure=VIS-FIS "
              "agenda_date=2026-11-24T15:00:00+00:00 agenda_day=tue expiry=2026-11-24T15:00:00+00:00 priority=B "
              "request_id={rid} Buongiorno, ecco la nostra disponibilità.")
RING_NONE = "cancel reason=no_availability request_id={rid} Mi dispiace, nessuna disponibilità."
RING_ACK = "ricevuto request_id={rid} Abbiamo ricevuto la richiesta, verifichiamo."  # città 21/11: info, ack, ricevuto
RING_U_RX = "RX-U9"


def ring(answers: dict[int, tuple[float, str] | list[tuple[float, str]]], round_s: float = RING, u_round: float = RING / 4):
    """I partner pubblicati della fisiatria (_KNOWN_PARTNERS del centro, in ordine) con l'indice in ``answers``
    rispondono al need.request del centro dopo (secondi, testo), o con una lista di queste coppie; {rid} è il
    request_id, {me} il loro id. Gli altri tacciono. Il giro del centro dura ``round_s`` s, ``u_round`` per la U."""
    def setup(b: Bus) -> None:
        bc = b.ids["booking_centre"]
        brain = b.agents[bc].main.__globals__
        brain["_PARTNER_ROUND"], brain["_PARTNER_ROUND_U"] = round_s, u_round
        b.partners, b.ring_answers, add = list(brain["_KNOWN_PARTNERS"]["hospital.physiatry"]), [], b.add

        async def later(delay: float, me: str, text: str, need: str) -> None:
            await asyncio.sleep(delay)
            b.ring_answers.append(b.add(me, bc, {"text": text}, need))
            b.queue.append(b.ring_answers[-1])

        def adding(sender, to, body, need=NEED, conv=None, kind="chat") -> Message:
            m = add(sender, to, body, need, conv, kind)
            if sender == bc and to in b.partners and m.text.startswith("need.request"):
                got = answers.get(b.partners.index(to), [])
                for delay, text in got if isinstance(got, list) else [got]:
                    reply = text.format(rid=fields(m.text)["request_id"], me=to)
                    b.tasks.append(asyncio.create_task(later(delay, to, reply, need)))
            return m
        b.add = adding
    return setup


def skips_ring_offers(b: Bus) -> None:
    """Il cittadino non sceglie le offerte di un partner: la prenotazione nell'altra ASST è dell'arena
    (partner_finestra_piena), qui conta che l'offerta arrivi."""
    react = b.citizen.react

    def reacting(m: Message) -> None:
        if not (act(m) == "offer" and any(o.get("facility") in b.partners for o in parse_offers(m.text))):
            react(m)
    b.citizen.react = reacting


def taken_but(slot_id: str):
    """Ogni altro slot di fisiatria è già preso: S ha solo ``slot_id`` e oltre la finestra non c'è nulla."""
    def setup(b: Bus) -> None:
        for s in b.ledger.slots.values():
            if s["procedure"] == "VIS-FIS" and s["slot_id"] != slot_id:
                b.ledger.holds[f"H-ALTRA-{s['slot_id']}"] = dict(
                    hold_id=f"H-ALTRA-{s['slot_id']}", slot_id=s["slot_id"], need_id=None, state="committed")
    return setup


def _to_citizen(b: Bus) -> list[tuple[int, Message]]:
    return [(i, m) for i, m in enumerate(b.log) if m.sender == b.ids["booking_centre"] and m.recipient == CIT]


def ring_offered_first(b: Bus) -> str | None:
    """Il primo messaggio di offerte del centro al cittadino porta lo slot del partner (il cittadino sceglie
    dal primo, R:95): nessuna alternativa rilassata né lista d'attesa prima."""
    first = next((m for _, m in _to_citizen(b) if act(m) == "offer"), None)
    if first is None or RING_SLOT not in {o.get("slot_id") for o in parse_offers(first.text)}:
        return f"primo messaggio di offerte «{first.text[:90] if first else ''}», atteso lo slot del partner {RING_SLOT}"


def ring_after_hiccup(b: Bus) -> str | None:
    """Dopo il cancel dell'offerta il cui hold è saltato, mai la lista d'attesa: il messaggio di offerte che
    segue porta lo slot del partner (R:682, R:676)."""
    said = [m for _, m in _to_citizen(b)]
    if any("waiting_list=true" in m.text.split() for m in said):
        return "lista d'attesa al cittadino con uno slot del partner in arrivo"
    cancel = next((i for i, m in enumerate(said) if act(m) == "cancel"), None)
    if cancel is None:
        return "nessun cancel al cittadino: l'hold non è saltato"
    nxt = next((m for m in said[cancel + 1:] if act(m) == "offer"), None)
    if nxt is None or RING_SLOT not in {o.get("slot_id") for o in parse_offers(nxt.text)}:
        return f"dopo il cancel «{nxt.text[:90] if nxt else ''}», atteso lo slot del partner {RING_SLOT}"


def ring_then_fallback(after: float = 0.0, within: float | None = None):
    """Con S vuoto il centro chiede subito ai partner e lo dice al cittadino; la prima offerta (alternativa
    rilassata o lista d'attesa) parte dopo l'ultima risposta dei partner, almeno ``after`` s dopo il giro
    (il tetto, se tacciono) ed entro ``within`` s (se rispondono tutti, non si aspetta il tetto)."""
    def check(b: Bus) -> str | None:
        bc = b.ids["booking_centre"]
        asks = [m for m in b.log if m.sender == bc and m.text.startswith("need.request")]
        if not asks:
            return "S vuoto e nessun need.request ai partner (R:676)"
        said = _to_citizen(b)
        offer = next(((i, m) for i, m in said if act(m) == "offer"), None)
        if offer is None:
            return "nessuna offerta al cittadino dopo il giro dai partner"
        if not any("strutture partner" in m.text for i, m in said if i < offer[0]):
            return "il cittadino non ha saputo del giro dai partner prima dell'offerta (R:248)"
        last = max((b.log.index(m) for m in getattr(b, "ring_answers", [])), default=-1)
        if offer[0] < last:
            return "offerta al cittadino prima dell'ultima risposta dei partner"
        took = b.stamp[offer[1].msg_id] - b.stamp[asks[0].msg_id]
        if took < after or (within is not None and took >= within):
            return f"offerta {took:.2f} s dopo il giro dai partner, attesa tra {after} e {within} s"
    return check


SCENARIOS.update({
    # Niente da noi entro la finestra B; un partner risponde a metà giro (45 s in città, oltre i vecchi 30 s)
    # con uno slot che va: è il primo messaggio di offerte, senza l'alternativa rilassata prima.
    "booking_partner_tardivo": dict(start=BACK, full_days=2, setup=setups(ring({0: (RING / 2, RING_OFFER)}), skips_ring_offers),
                                    expect=[ring_offered_first, ring_then_fallback(within=RING)]),
    # Il solo slot in S va a un'altra ASST tra l'offerta e l'hold (SLOT_TAKEN), oltre la finestra non c'è nulla
    # (città: 30 liste d'attesa così, tutte B o D): lo slot del partner al posto della lista d'attesa.
    "booking_partner_dopo_intoppo": dict(start=BACK, steal="S-h1-01-10",
                                         setup=setups(taken_but("S-h1-01-10"), ring({0: (RING / 4, RING_OFFER)}), skips_ring_offers),
                                         expect=[ring_after_hiccup]),
    # Tutti i partner rispondono no_availability, a scaglioni: l'alternativa rilassata solo dopo l'ultimo,
    # senza aspettare il tetto del giro.
    "booking_partner_tutti_no": dict(start=BACK, full_days=2,
                                     setup=ring({i: (0.1 * (i + 1), RING_NONE) for i in range(16)}, round_s=3 * RING),  # tutti i pubblicati
                                     expect=[ring_then_fallback(within=2 * RING), wrote("checkin", "R:234")]),
    # I partner tacciono: l'alternativa rilassata solo allo scadere del giro (in background), mai prima.
    "booking_partner_muti": dict(start=BACK, full_days=2, setup=ring({}, round_s=RING / 2),
                                 expect=[ring_then_fallback(after=RING / 2), wrote("checkin", "R:234")]),
    # Tutti i partner mandano subito un ack col request_id, poi uno solo risponde no_availability e gli altri
    # tacciono (città, N-...0001510014): un ack non è una risposta, il giro dura fino al tetto.
    "booking_partner_solo_ack": dict(
        start=BACK, full_days=2,
        setup=ring({i: [(0.05 * (i + 1), RING_ACK), *([(RING / 4, RING_NONE)] if i == 0 else [])] for i in range(7)},
                   round_s=RING / 2),
        expect=[ring_then_fallback(after=RING / 2)]),
    # Priorità U (scadenza in giornata): il giro dura _PARTNER_ROUND_U, più corto di quello di B.
    "booking_partner_muti_u": dict(
        start=("booking_centre", f"Buongiorno, vorrei prenotare. prescription_number={RING_U_RX} lang=it"), full_days=1,
        setup=setups(seeded_rx(RING_U_RX, CIT, "VIS-FIS", "U"), ring({}, round_s=3 * RING, u_round=RING / 4)),
        expect=[ring_then_fallback(after=RING / 4, within=3 * RING)]),
})



# --- Centro prenotazioni, lingua che nessuno dei nostri medici parla (città 21/11, N-...0002770010, livello 4):
# il gp passa il caso con lang=en, il centro offre h1; il need.request del cittadino con lang=sw incrocia le
# offerte e arriva a un pod appena riavviato, poi i 3 cancel reason=lang. Il giro dai partner parte nel turno
# del lang=sw, senza aspettare il Ledger (G2); le risposte a un giro vecchio contano (G5); le stesse offerte
# non partono due volte (G6).


def mother_tongue(b: Bus) -> None:
    """Alle prime offerte del centro il cittadino scrive il suo need.request con lang=sw e rifiuta ogni
    offerta con reason=lang, tutto insieme (i messaggi incrociano le offerte); poi si comporta come sempre."""
    bc, react, done = b.ids["booking_centre"], b.citizen.react, []

    def reacting(m: Message) -> None:
        if m.sender == bc and act(m) == "offer" and not done:
            done.append(m)
            b.citizen.say(bc, f"need.request prescription_number={b.citizen.rx} lang=sw I need a doctor in my mother tongue.")
            for o in parse_offers(m.text):
                b.citizen.say(bc, f"cancel offer_id={o['offer_id']} reason=lang")
            return
        react(m)
    b.citizen.react = reacting


def restart_booking(before: str, slow_rx: float):
    """Il pod del centro riparte (moduli ricaricati: CAL, _PAGES, _RULES, _CLOCK, _SENT vuoti) appena prima del
    primo messaggio con il token ``before``; da lì la risposta a `prescription` tarda ``slow_rx`` s (città: minuti)."""
    def setup(b: Bus) -> None:
        deliver, bc, done = b.deliver, b.ids["booking_centre"], []

        async def restarted(m: Message) -> None:
            if not done and m.recipient == bc and before in m.text.split():
                done.append(m)
                b.agents[bc] = Agent("booking_centre")
                b.agents[bc].main.__globals__["_RINGS"] = Rings(b.tasks)
                late = b.ledger.late
                b.ledger.late = lambda text: slow_rx if (text.split() or [""])[0] == "prescription" else late(text)
            await deliver(m)

        b.deliver = restarted
    return setup


def _partner_asks(b: Bus) -> list[Message]:
    bc = b.ids["booking_centre"]
    return [m for m in b.log if m.sender == bc and m.text.startswith("need.request") and m.recipient not in b.agents]


def partners_in_lang_turn(b: Bus) -> str | None:
    """Il need.request ai partner parte nel turno del lang=sw del cittadino, e la prima parola del centro al
    cittadino dopo il lang=sw dice che cerca presso i partner (non «sto ancora controllando»)."""
    bc = b.ids["booking_centre"]
    sw = next((m for m in b.log if m.sender == CIT and m.recipient == bc and "lang=sw" in m.text.split()), None)
    if sw is None:
        return "il cittadino non ha scritto lang=sw: il centro non ha offerto nulla prima"
    asks = _partner_asks(b)
    if not asks:
        return "nessun need.request ai partner"
    opened = b.opened.get(b.turn_of[asks[0].msg_id])
    if opened is not sw:
        return f"need.request ai partner nel turno di «{(opened.text if opened else '')[:70]}», atteso quello del lang=sw"
    first = next((m for m in b.log[b.log.index(sw):] if m.sender == bc and m.recipient == CIT), None)
    if first is None or "partner facilities" not in first.text:
        return f"dopo il lang=sw il centro dice «{first.text[:90] if first else ''}», atteso l'avviso dei partner"


def offers_once(b: Bus) -> str | None:
    """Mai lo stesso insieme di offerte due volte al cittadino."""
    seen: list[frozenset[str]] = []
    for _, m in _to_citizen(b):
        if act(m) == "offer":
            ids = frozenset(o.get("slot_id", "") for o in parse_offers(m.text))
            if ids in seen:
                return f"le stesse offerte due volte: {sorted(ids)}"
            seen.append(ids)


def first_round_late(b: Bus) -> None:
    """Il primo partner pubblicato della fisiatria risponde al PRIMO giro solo quando parte il secondo,
    con uno slot che va (città 21/11: risposte a 212-347 s, oltre un giro)."""
    bc = b.ids["booking_centre"]
    partner = next(iter(b.agents[bc].main.__globals__["_KNOWN_PARTNERS"]["hospital.physiatry"]))
    b.partners, asked, add = [partner], [], b.add

    def adding(sender, to, body, need=NEED, conv=None, kind="chat") -> Message:
        m = add(sender, to, body, need, conv, kind)
        if sender == bc and to == partner and m.text.startswith("need.request"):
            asked.append(fields(m.text)["request_id"])
            if len(asked) == 2:
                b.queue.append(add(partner, bc, {"text": RING_OFFER.format(rid=asked[0], me=partner)}, need))
        return m
    b.add = adding


def first_round_offered(b: Bus) -> str | None:
    """Due giri, e lo slot che il partner ha dato per il primo arriva al cittadino."""
    if len({fields(m.text)["request_id"] for m in _partner_asks(b)}) < 2:
        return "un solo giro dai partner: il rifiuto dell'alternativa rilassata non ne ha aperto un secondo"
    if not any(RING_SLOT in {o.get("slot_id") for o in parse_offers(m.text)} for _, m in _to_citizen(b) if act(m) == "offer"):
        return f"lo slot del partner {RING_SLOT}, risposta al primo giro, non è arrivato al cittadino"


SCENARIOS.update({
    # Il pod del centro riparte prima del lang=sw e il Ledger tace (rules e prescription lente): il giro dai
    # partner parte comunque nel turno del lang=sw, con i criteri del passaggio del gp.
    "booking_lingua_rara_riavvio": dict(
        start=("gp", PREF_EN), handoff=True,
        setup=setups(slow_ledger("rules", 3.0), mother_tongue, restart_booking("lang=sw", 3.0)),
        expect=[partners_in_lang_turn, offers_once]),
    # Lo stesso senza riavvio.
    "booking_lingua_rara": dict(
        start=("gp", PREF_EN), handoff=True, setup=setups(slow_ledger("rules", 3.0), mother_tongue),
        expect=[partners_in_lang_turn, offers_once]),
    # Giro 1 muto, alternativa rilassata rifiutata (reason=deadline), giro 2; il partner risponde al giro 1 solo
    # adesso: la sua offerta va al cittadino (G5).
    "booking_partner_giro_vecchio": dict(
        start=BACK, full_days=2, refuse_first="deadline", setup=setups(first_round_late, skips_ring_offers),
        expect=[first_round_offered]),
})


# --- Centro prenotazioni, 21/11: lo stesso slot a 13 casi in 10 minuti (le offerte deboli riempivano anche con
# slot già offerti ad altri casi aperti, poi STALE_FENCE), il confirm doppio a 22 ms, la scadenza B o D in ore
# di calendario, sabato e domenica compresi (51 rifiuti reason=deadline con il conteggio a giorni feriali).


def ring_before_shared(b: Bus) -> str | None:
    """Uno slot già offerto a un altro caso aperto va a un secondo caso solo dopo il giro dai partner."""
    bc, holder = b.ids["booking_centre"], {}
    for i, m in enumerate(b.log):
        if m.sender == bc and m.recipient.startswith("citizen:") and act(m) == "offer":
            for o in parse_offers(m.text):
                first = holder.setdefault(o.get("slot_id"), m.recipient)
                if first != m.recipient and not any(
                        x.sender == bc and x.text.startswith("need.request") and x.need_id == m.need_id for x in b.log[:i]):
                    return f"{o.get('slot_id')} offerto a {m.recipient}, già offerto a {first}, senza chiedere ai partner"


def double_confirm(b: Bus) -> None:
    """Il confirm del cittadino arriva due volte al centro nello stesso istante (città 21/11 07:35, 07:44,
    07:52: 22 ms): due turni insieme, fuori dal lock del caso (race: la rete cede il loop)."""
    bc, say, done = b.ids["booking_centre"], b.citizen.say, []

    def saying(to: str, text: str, kind: str = "chat", need: str | None = None, **body: str) -> None:
        if to != bc or not text.startswith("confirm") or done:
            return say(to, text, kind, need, **body)
        done.append(text)
        for _ in range(2):
            m = b.add(CIT, to, body or {"text": text}, need=need or b.citizen.need, kind=kind)
            b.tasks.append(asyncio.create_task(b.deliver(m)))
    b.citizen.say = saying


def one_confirm_per_slot(b: Bus) -> str | None:
    """Alla struttura arriva un solo confirm per slot (un secondo hold fa avanzare il fence: STALE_FENCE)."""
    bc, facilities = b.ids["booking_centre"], {b.ids[f] for f in FACILITIES}
    sent = Counter(fields(m.text).get("slot_id") for m in b.log
                   if m.sender == bc and m.recipient in facilities and m.text.startswith("confirm"))
    twice = {slot: n for slot, n in sent.items() if n > 1}
    if not sent or twice:
        return f"confirm alla struttura per slot: {dict(sent)}, atteso uno solo"


SCENARIOS.update({
    "booking_slot_scarsi": dict(start=BACK, others=1, setup=taken_but("S-h1-01-10"), expect=[ring_before_shared]),
    "booking_slot_scarsi_ring": dict(start=BACK, others=1,
        setup=setups(taken_but("S-h1-01-10"), ring({0: (RING / 4, RING_OFFER)}), skips_ring_offers),
        expect=[ring_before_shared, distinct_offers]),
    # Lo stesso confirm due volte insieme: un solo confirm alla struttura, e la prenotazione va.
    "booking_confirm_doppio": dict(start=BACK, race=True, setup=double_confirm, expect=[
        one_confirm_per_slot, wrote("checkin", "R:234")]),
    # B nato venerdì 27/11 08:00 di agenda: la scadenza è domenica 08:00 (48 h di calendario, sabato e domenica
    # contati anche se l'agenda non li ha; R:91). Città 21/11: ogni B rifiutato reason=deadline aveva lo slot
    # oltre 87,9 h, altre squadre da 48,3 h. Solo slot di venerdì, mai lunedì.
    "booking_b_nato_venerdi": dict(start=BACK, late_days=4, expect=[
        offers_before(datetime(2026, 11, 29, 8, tzinfo=UTC), "R:91, 48 h di calendario"), wrote("checkin", "R:234")]),
    # Lo stesso con venerdì pieno: S vuoto, alternativa rilassata (os-1: martedì rifiutato reason=deadline).
    "booking_b_nato_venerdi_pieno": dict(start=BACK, late_days=4, full_days=1, expect=[
        told_text("booking_centre", "entro la scadenza della priorità non ci sono posti", cite="R:91, R:232"),
        wrote("checkin", "R:234")]),
})


# --- Centro prenotazioni, livello 6 (città 21/11, no_booking −1 con S di 208-520 slot): il cittadino conferma,
# circa 5 minuti dopo la struttura annulla (facility_cancelled) e il centro diceva «sto controllando altri posti
# liberi», poi taceva aspettando world (3-6 minuti); il giudice chiude il percorso senza un'offerta. E le
# offerte a 3,2-4,7 ore di agenda: dall'offerta all'esito del commit passano 13-16 minuti reali (16 ore).


def taken(*slot_ids: str):
    """Questi slot sono già prenotati da un'altra ASST (world li elenca con free=0)."""
    def setup(b: Bus) -> None:
        for sid in slot_ids:
            b.ledger.holds[f"H-ALTRA-{sid}"] = dict(hold_id=f"H-ALTRA-{sid}", slot_id=sid, need_id=None, state="committed")
    return setup


def cancel_arrives_late(hours: float, slow_world: float):
    """Il cancel della struttura arriva al centro ``hours`` ore di agenda dopo il confirm (l'orologio del centro
    salta avanti lì) e da quel momento world risponde dopo ``slow_world`` s (città: 3-6 minuti)."""
    def setup(b: Bus) -> None:
        bc, hosp, deliver, late = b.ids["booking_centre"], b.ids["hospital"], b.deliver, []

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(minutes=hours if late else 0)

        async def delivering(m: Message) -> None:
            if not late and m.sender == hosp and m.recipient == bc and act(m) == "cancel":
                late.append(m)
                b.ledger.slow_world = slow_world
            await deliver(m)

        b.agents[bc].main.__globals__["datetime"], b.deliver = Clock, delivering
    return setup


SEARCHING = ("Sto controllando altri posti liberi", "I am checking other free slots")


def reoffered_in_cancel_turn(b: Bus) -> str | None:
    """Nel turno del cancel dell'offerta confermata il cittadino riceve almeno un'offerta nuova, diversa da quella
    annullata; mai «sto controllando altri posti liberi» da solo."""
    said = [m for _, m in _to_citizen(b)]
    if any(s in m.text for m in said for s in SEARCHING):
        return "«sto controllando altri posti liberi» al cittadino: una promessa senza offerta"
    cancel = next((m for m in said if act(m) == "cancel" and has(m.text, ("reason=facility_cancelled",))), None)
    if cancel is None:
        return "nessun cancel reason=facility_cancelled al cittadino: l'hold non è saltato"
    lost = fields(cancel.text).get("offer_id")
    new = [o for m in said if act(m) == "offer" and b.turn_of[m.msg_id] == b.turn_of[cancel.msg_id]
           for o in parse_offers(m.text) if o.get("offer_id") != lost and o.get("waiting_list") != "true"]
    if not new:
        return f"nessuna offerta nuova nel turno del cancel di {lost}"


def offered_today(b: Bus) -> str | None:
    """Il primo messaggio di offerte del centro porta solo slot del giorno di agenda del Ledger (U, R:91)."""
    first = next((m for _, m in _to_citizen(b) if act(m) == "offer"), None)
    days = {o.get("agenda_date", "")[:10] for o in parse_offers(first.text)} if first else set()
    if days != {b.ledger.now.date().isoformat()}:
        return f"prima offerta per i giorni {sorted(days)}, attesa solo oggi {b.ledger.now.date()} (U)"


# Della prima pagina di world della fisiatria restano liberi solo lun 10, lun 15 (h1) e mer 15 (h2).
PAGE1_TAKEN = ("S-h1-01-10", "S-h3-01-10", "S-h1-01-15", "S-h3-01-15", "S-h1-02-10", "S-h2-02-10", "S-h1-02-15")

SCENARIOS.update({
    # Ginocchio (D): il confirm, l'hold rifiutato, il cancel della struttura 54 ore di agenda dopo (lun 08 → mer 14),
    # world lento da lì. Le offerte nuove vengono dal calendario in memoria nel turno del cancel.
    "booking_riofferta_dopo_annullo": dict(
        start=KNEE, refuse_holds=1, setup=setups(taken(*PAGE1_TAKEN), cancel_arrives_late(54, 1.0)),
        expect=[reoffered_in_cancel_turn, wrote("commit", "R:682")]),
    # Nessuna offerta entro 16 ore di agenda dall'ora del Ledger (lun 08:00): niente lun 10 né lun 15.
    "booking_margine_16h": dict(start=BACK, expect=[offers_ahead(16), wrote("checkin", "R:234")]),
    # U (scadenza in giornata, R:91): oltre 16 ore non c'è nulla, quindi gli slot di oggi, mai la lista d'attesa.
    "booking_margine_u": dict(
        start=("booking_centre", "Buongiorno, vorrei prenotare. prescription_number=RX-U8 lang=it"),
        setup=seeded_rx("RX-U8", CIT, "VIS-FIS", "U"), expect=[offered_today, wrote("commit", "R:91")]),
})

# --- Specialista e consultorio (diagnosi del 21/11, punti 5-8): referto sul Ledger prima del messaggio al
# cittadino, tetto al turno del confirm di un partner, un «non» che non annulla una scelta, check-in rifiutato.


def report_first(b: Bus) -> str | None:
    """Lo specialista scrive `report` sul Ledger prima del `checkin` al cittadino, che chiude il percorso
    (ADR-119: il giudice valuta un minuto dopo, sui fatti del Ledger fin lì); il `report` al cittadino solo
    dopo il `reported` del Ledger."""
    sp = b.ids["specialist_care"]

    def at(pred) -> int | None:
        return next((i for i, m in enumerate(b.log) if pred(m)), None)
    filed = at(lambda m: m.sender == sp and m.recipient == LEDGER and m.text.startswith("report "))
    note = at(lambda m: m.sender == sp and m.recipient == CIT and act(m) == "checkin")
    done = at(lambda m: m.sender == LEDGER and m.recipient == sp and m.text.startswith("reported "))
    told_ = at(lambda m: m.sender == sp and m.recipient == CIT and act(m) == "report")
    if None in (filed, note, done, told_):
        return f"specialist_care: report {filed}, checkin al cittadino {note}, reported {done}, report al cittadino {told_}"
    if filed > note:
        return "specialist_care: checkin al cittadino prima del report sul Ledger (ADR-119, R:214-218)"
    if told_ < done:
        return "specialist_care: report al cittadino prima del reported del Ledger"


def pull_pause(folder: str, seconds: float):
    """Ogni lettura della history di ``folder`` costa ``seconds`` (in città 0,25 s di pausa più la lettura)."""
    def setup(b: Bus) -> None:
        b.agents[b.ids[folder]].mods["facility"]._PULL_PAUSE = seconds
    return setup


def partner_confirm_ends(folder: str, partner: str):
    """slow_booking per ``folder``: il turno del confirm di ``partner`` finisce entro 9,5 s (ack, R:246),
    poi il push di held committa e quello di committed manda la ricevuta al partner, una volta."""
    def check(b: Bus) -> str | None:
        fac, bc = b.ids[folder], b.ids.get(partner, partner)
        took = [round(b.took[m.msg_id], 1) for m in b.log
                if m.sender == bc and m.recipient == fac and m.text.startswith("confirm") and m.msg_id in b.took]
        if not took or max(took) >= 9.5:
            return f"{folder}: turno del confirm {took} s, serve < 9,5 s (ack, R:246)"
        for to, head, push in ((LEDGER, "commit", "held"), (bc, "booking.receipt", "committed")):
            sent = [m for m in b.log if m.sender == fac and m.recipient == to and m.text.startswith(head + " ")]
            opened = [b.opened[b.turn_of[m.msg_id]].text.split()[0] for m in sent]
            if opened != [push]:
                return f"{folder}: {head} nei turni aperti da {opened} invece che una volta dal push di {push}"
    return check


def picks_in_words(text: str):
    """Alle prime offerte del consultorio il cittadino risponde ``text``, senza token; poi come sempre."""
    def setup(b: Bus) -> None:
        fc, react, done = b.ids["family_counseling"], b.citizen.react, []

        def reacting(m: Message) -> None:
            if m.sender == fc and act(m) == "offer" and not done:
                done.append(m)
                b.citizen.chosen = parse_offers(m.text)[0]
                b.citizen.say(fc, text)
                return
            react(m)
        b.citizen.react = reacting
    return setup


def booked_first_offer(b: Bus) -> str | None:
    """Il consultorio prenota la prima delle offerte che il cittadino ha scelto a parole."""
    fc = b.ids["family_counseling"]
    first = next((m for m in b.log if m.sender == fc and m.recipient == CIT and act(m) == "offer"), None)
    slot = parse_offers(first.text)[0].get("slot_id") if first else None
    held = [w["f"].get("slot_id") for w in b.ledger.writes if w["op"] == "hold" and w["by"] == "family_counseling"]
    if held != [slot]:
        return f"family_counseling: hold su {held}, atteso uno solo sulla prima offerta {slot}"


def checkin_refused_once(folder: str):
    """Il Ledger rifiuta il checkin fuori dalla mezza giornata dello slot: ``folder`` lo dice una volta, con il
    motivo e la fascia (_checkin_refused, come l'ospedale), senza «checkin» né «controlli il codice». Contano i
    messaggi dei turni aperti dall'arrivo o dal Ledger, non le risposte ad altri messaggi (hostile)."""
    def check(b: Bus) -> str | None:
        fac = b.ids[folder]
        tried = [w for w in b.ledger.writes if w["op"] == "checkin"]
        if len(tried) != 1:
            return f"{len(tried)} checkin sul Ledger invece di uno"
        arrivals = {m.msg_id for m in b.log if m.sender == CIT and m.recipient == fac and act(m) == "checkin"}
        after = [m for m in b.log[tried[0]["i"]:] if m.sender == fac and m.recipient == CIT
                 and (b.opened[b.turn_of[m.msg_id]].msg_id in arrivals or b.opened[b.turn_of[m.msg_id]].sender == LEDGER)]
        if len(after) != 1:
            return f"{folder}: {len(after)} messaggi al cittadino dopo il checkin rifiutato, ne serve uno"
        if act(after[0]) == "checkin" or "reason=" not in after[0].text or "mezza giornata" not in after[0].text:
            return f"{folder}: dopo il checkin rifiutato «{after[0].text[:100]}»"
    return check


SCENARIOS.update({
    # 5: report sul Ledger, poi il checkin al cittadino (chiude il percorso), il report al cittadino dopo reported.
    "specialista_referto_prima": dict(start=PALPITAZIONI, expect=[report_first, wrote("report", "R:208/236")]),
    # 6: hold e commit dello specialista rispondono dopo 8 s, ogni lettura 0,5 s: il turno del confirm del
    # centro finisce entro il tetto, i push di held e committed finiscono la prenotazione.
    "specialista_scritture_lente": dict(start=PALPITAZIONI, slow_writes=8.0, ledger_turns=True,
                                        setup=pull_pause("specialist_care", 0.5),
                                        expect=[partner_confirm_ends("specialist_care", "booking_centre"),
                                                wrote("checkin", "R:234")]),
    # 6: lo stesso per il consultorio con il centro di un'altra ASST.
    "consultorio_scritture_lente": dict(
        start=("family_counseling", XREQ.format(svc="family_counseling.gynecology", proc="CONS-GIN", rid="R-60")),
        foreign=True, slow_writes=8.0, ledger_turns=True, setup=pull_pause("family_counseling", 0.5),
        expect=[partner_confirm_ends("family_counseling", FOREIGN)]),
    # 7: «va bene la prima, non vedo l'ora» sceglie la prima, non rifiuta tutto (città 10:18-10:27: 4 citizen_declined).
    "consultorio_non_vedo_lora": dict(start=GINECOLOGIA, setup=picks_in_words("Va bene la prima, non vedo l'ora!"),
                                      expect=[never_told("family_counseling", "reason=citizen_declined", cite="scelta"),
                                              booked_first_offer, wrote("checkin", "R:234")]),
    # 7: un rifiuto vero resta un rifiuto, anche con «va bene» dentro.
    "consultorio_non_mi_va_bene": dict(start=GINECOLOGIA, setup=picks_in_words("No grazie, non mi va bene nessuna."),
                                       expect=[told("family_counseling", "cancel", "reason=citizen_declined", cite="rifiuto")]),
    # 8: checkin fuori dalla mezza giornata, rifiuto nel turno dell'arrivo e come push del Ledger.
    "specialista_checkin_fuori_fascia": dict(start=PALPITAZIONI, refuse_checkin=CHECKIN_REFUSED,
                                             expect=[checkin_refused_once("specialist_care")]),
    "specialista_checkin_rifiutato_push": dict(start=PALPITAZIONI, refuse_checkin=CHECKIN_REFUSED, ledger_turns=True,
                                               setup=slow_checkin(0.5), expect=[checkin_refused_once("specialist_care")]),
    "consultorio_checkin_rifiutato_push": dict(start=GINECOLOGIA, refuse_checkin=CHECKIN_REFUSED, ledger_turns=True,
                                               setup=slow_checkin(0.5), expect=[checkin_refused_once("family_counseling")]),
})


# --- Centro prenotazioni e MACH-2.1 (docs/protocol/mach-2.1.md): il need.request porta mach, after, deadline e
# lease_s e mai lang= (R:191); 5 strutture per ondata, la seconda solo dopo il no di tutta la prima (R3); solo
# l'anello (R17): niente richieste né offerte girate fuori dall'anello o da chi non abbiamo interpellato; un
# annullamento solo da chi ha emesso l'offerta, una ricevuta solo da chi ha avuto il confirm (R18).

PRECOTTO = "agent:s1twcdzg3296"  # ospedale di asst-precotto, lingua rara ro (ring.RARE), non tra _KNOWN_PARTNERS
NOT_RING = "agent:zzzzzzzzzzzz"  # nessuna ASST della mappa
UNASKED = "agent:p90cbsk3n9e6"  # ospedale di asst-sud: nell'anello, mai interpellato
FORGER = "agent:h6r1v45esk77"  # ospedale di asst-giambellino: nell'anello, non ha emesso le nostre offerte
RX_RO = "RX-RO1"


def fake_offer(slot: str) -> str:
    return (f"offer offer_id=of-{slot} slot_id={slot} doctor_id=asst-sud-h1 facility={{me}} procedure=VIS-FIS "
            "agenda_date=2026-11-24T10:00:00+00:00 agenda_day=tue expiry=2026-11-24T10:00:00+00:00 priority=B "
            "request_id={rid} Ecco la nostra disponibilità.")


def inject(trigger, *says: tuple[str, str]):
    """Al primo messaggio del centro per cui ``trigger(b, m)`` è vero, ogni (mittente, testo) di ``says`` (fuori
    dal bus) gli scrive: {rid} è il request_id di quel messaggio, {offer_id} la sua prima offerta, {me} il mittente."""
    def setup(b: Bus) -> None:
        bc, add, done = b.ids["booking_centre"], b.add, []

        def adding(sender, to, body, need=NEED, conv=None, kind="chat") -> Message:
            m = add(sender, to, body, need, conv, kind)
            if sender == bc and not done and trigger(b, m):
                done.append(m)
                offers = parse_offers(m.text)
                for who, text in says:
                    text = text.format(rid=fields(m.text).get("request_id", ""), me=who,
                                       offer_id=offers[0].get("offer_id", "") if offers else "")
                    b.queue.append(add(who, bc, {"text": text}, need))
            return m
        b.add = adding
    return setup


def not_ring_first(b: Bus) -> None:
    """Un agente fuori dall'anello in testa ai partner pubblicati della fisiatria (una tabella a mano sbagliata)."""
    known = b.agents[b.ids["booking_centre"]].main.__globals__["_KNOWN_PARTNERS"]
    known["hospital.physiatry"] = {NOT_RING: "", **known["hospital.physiatry"]}


def mach21_request(first: str | None = None):
    """Ogni need.request ai partner: mach=2.1, i criteri, after/deadline/lease_s (R5, R6, R14), mai lang= né dati del
    cittadino (R:191); B: scadenza a 48 h meno il margine dalla nascita (lun 08:00), after almeno 4 h avanti.
    ``first``: la struttura chiesta per prima (lingua rara, R3)."""
    def check(b: Bus) -> str | None:
        asks = _partner_asks(b)
        if not asks:
            return "nessun need.request ai partner"
        if first and asks[0].recipient != first:
            return f"primo need.request a {asks[0].recipient}, atteso {first} (lingua rara prima, R3)"
        for m in asks:
            f = fields(m.text)
            missing = [k for k in ("service_type", "procedure", "priority", "after", "deadline", "lease_s", "request_id")
                       if not f.get(k)]
            leaked = [k for k in ("lang", "citizen", "patient", "prescription_number") if k in f]
            if f.get("mach") != "2.1" or missing or leaked:
                return f"need.request «{m.text[:120]}»: mach={f.get('mach')}, mancano {missing}, in più {leaked} (R:191)"
            after, deadline = (datetime.fromisoformat(f[k]).replace(tzinfo=UTC) for k in ("after", "deadline"))
            if not START + timedelta(hours=46) <= deadline <= START + timedelta(hours=47, minutes=30):
                return f"deadline={f['deadline']}: attesa nascita + 48 h - 30 min (R6)"
            if not START + timedelta(hours=4) <= after < deadline:
                return f"after={f['after']}: atteso almeno 4 h avanti e prima della scadenza (R5)"
            if not 0 < int(f["lease_s"]) <= 1800:
                return f"lease_s={f['lease_s']} fuori da 1..1800 (R14)"
    return check


def waves(total: int | None = None):
    """Un giro, un request_id: al massimo 5 strutture per ondata, la seconda ondata solo dopo il no (cancel) di
    tutte quelle della prima (R3), nessuna chiesta due volte; ``total`` richieste (None: i partner di ring(), max 10)."""
    def check(b: Bus) -> str | None:
        bc, asks = b.ids["booking_centre"], _partner_asks(b)
        want = min(10, len(b.partners)) if total is None else total
        rids = {fields(m.text).get("request_id") for m in asks}
        if len(asks) != want or len(rids) != 1 or len({m.recipient for m in asks}) != len(asks):
            return f"{len(asks)} need.request a {len({m.recipient for m in asks})} strutture, request_id {sorted(rids)}: attesi {want}, uno solo"
        if len(asks) > 5:
            nos = {m.sender for m in b.log[:b.log.index(asks[5])] if m.recipient == bc and m.text.startswith("cancel")}
            if not {m.recipient for m in asks[:5]} <= nos:
                return "seconda ondata prima del no di tutta la prima (R3)"
    return check


def never_offered(*slots: str):
    def check(b: Bus) -> str | None:
        for _, m in _to_citizen(b):
            bad = {o.get("slot_id") for o in parse_offers(m.text)} & set(slots) if act(m) == "offer" else set()
            if bad:
                return f"offerto al cittadino {sorted(bad)}, da fuori dall'anello o da chi non abbiamo interpellato (R17)"
    return check


def never_wrote_to(agent_id: str):
    def check(b: Bus) -> str | None:
        if any(m.sender == b.ids["booking_centre"] and m.recipient == agent_id for m in b.log):
            return f"il centro ha scritto a {agent_id}, fuori dall'anello (R17)"
    return check


SCENARIOS.update({
    # Lingua rara ro, che nessuno dei nostri parla: l'ospedale di Precotto (ro) per primo, anche se non è tra i
    # partner pubblicati; la richiesta con i token della 2.1 e senza lang=. I partner tacciono: 5 richieste.
    "booking_mach21_richiesta": dict(
        start=("booking_centre", f"Buongiorno, vorrei prenotare. prescription_number={RX_RO} lang=ro"),
        setup=seeded_rx(RX_RO, CIT, "VIS-FIS", "B"),
        expect=[mach21_request(first=PRECOTTO), waves(5), wrote("checkin", "R:234")]),
    # S vuoto entro la B, tutti i partner dicono no: prima ondata di 5, la seconda dopo i 5 no, stesso request_id.
    "booking_mach21_ondate": dict(start=BACK, full_days=2,
                                  setup=ring({i: (0.1 * (i + 1), RING_NONE) for i in range(12)}, round_s=3 * RING),
                                  expect=[waves(), mach21_request(), ring_then_fallback(within=2 * RING)]),
    # Uno della prima ondata tace: niente seconda ondata, il giro finisce allo scadere.
    "booking_mach21_ondata_muta": dict(start=BACK, full_days=2,
                                       setup=ring({i: (0.1 * (i + 1), RING_NONE) for i in range(4)}, round_s=RING / 2),
                                       expect=[waves(5), ring_then_fallback(after=RING / 2)]),
    # Un agente fuori dall'anello nella tabella dei partner, e un ospedale dell'anello mai interpellato, offrono
    # col nostro request_id: nessuna richiesta fuori dall'anello, nessuna delle due offerte al cittadino.
    "booking_mach21_offerta_impostore": dict(
        start=BACK, full_days=2,
        setup=setups(not_ring_first, inject(lambda b, m: m.text.startswith("need.request"),
                                            (NOT_RING, fake_offer("W01QQQ-asst-sud-h1-20261124-1000")),
                                            (UNASKED, fake_offer("W01QQQ-asst-sud-h1-20261124-1100")))),
        expect=[never_wrote_to(NOT_RING), never_offered("W01QQQ-asst-sud-h1-20261124-1000", "W01QQQ-asst-sud-h1-20261124-1100"),
                wrote("checkin", "R:234")]),
    # Un ospedale dell'anello che non ha emesso le nostre offerte le annulla: ignorato, la prenotazione va (R18).
    "booking_mach21_annullo_falso": dict(
        start=BACK, setup=inject(lambda b, m: m.recipient == CIT and act(m) == "offer",
                                 (FORGER, "cancel offer_id={offer_id} reason=facility_cancelled Il posto non c'è più.")),
        expect=[never_told("booking_centre", "reason=facility_cancelled", cite="R18"), wrote("checkin", "R:234")]),
    # Una ricevuta da chi non ha avuto il confirm: mai al cittadino (R18).
    "booking_mach21_ricevuta_falsa": dict(
        start=BACK, setup=inject(lambda b, m: m.recipient == b.ids["hospital"] and m.text.startswith("confirm"),
                                 (FORGER, "booking.receipt booking_id=B-FAKE ledger_ref=L-FAKE Prenotazione confermata.")),
        expect=[never_told("booking_centre", "B-FAKE", cite="R18"), wrote("checkin", "R:234")]),
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
    failed, known = 0, []
    for name, sc in SCENARIOS.items():
        if args.k and args.k not in name:
            continue
        if sc.get("ledger_turns") and not args.ledger_turns:  # vale solo con i push del Ledger
            print(f"SKIP {name}")
            continue
        bus = Bus(sc, args.ledger_turns)
        asyncio.run(bus.run(sc["start"]))
        errs = list(dict.fromkeys(general(bus) + [e for e in (c(bus) for c in sc["expect"]) if e]))
        # sc["xfail"]: guasto noto del brain, con il perché. XFAIL/XPASS non cambiano l'uscita.
        status = ("XFAIL" if errs else "XPASS") if sc.get("xfail") else ("FAIL" if errs else "PASS")
        failed += status == "FAIL"
        if sc.get("xfail"):
            known.append(f"{status} {name}: {sc['xfail']}")
        print(f"{status} {name}" + "".join(f"\n     - {e}" for e in errs))
        if args.v:
            transcript(bus)
    if known:
        print("\nxfail (guasti noti, togliere xfail quando passano):" + "".join(f"\n  {k}" for k in known))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
