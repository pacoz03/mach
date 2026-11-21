"""The slot calendar: which slots an agent may offer or hold, why not the others, and how to say
them. The LLM's offer menu (harness.py) is built here: a forbidden slot never reaches it.

Design: docs/refactor/calendario.md; source in ``shared/mach/``, copied by ``shared/sync.sh``.
Pure, no network, one pod. Never raises: an error is counted in ``errors``, the answer is empty.
learn() trusts world rows only from city:ledger, claims from our own messages, anyone else only
through a ``slot`` the caller resolved on OUR offers. World rows and Ledger marks carry a version
(sent_at, msg_id) and the latest wins: an old page delivered late never puts free=1 back on a
slot a newer page gives free=0. Claims are a map slot -> (case, kind, instant) that lapses.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple

from city_sdk.city import LEDGER, Message

from . import protocol
from .case import offers_in
from .protocol import fields_of, first_word

_LOG = logging.getLogger("care")
Slot = dict[str, str]  # a world row's tokens exactly (R:243); sdk rows also get at/day/free
Version = tuple[datetime, str]  # (sent_at, msg_id) of the event
ASST = "gorla"
# Doctor key (last segment of doctor_id) -> languages, gender: the registry (console team/world),
# which the live world never sends. Only for OUR ASST: another ASST's h1 is another person.
DOCTORS: dict[str, tuple[frozenset[str], str]] = {
    "h1": (frozenset({"it", "en"}), "f"), "h2": (frozenset({"it"}), "m"), "h3": (frozenset({"it", "sq"}), "m"),
    "s1": (frozenset({"it", "en"}), "m"), "s2": (frozenset({"it"}), "f"), "c1": (frozenset({"it", "en"}), "f"),
}
LANGS = {"it": ("italiano", "Italian"), "en": ("inglese", "English"), "sq": ("albanese", "Albanian")}
_WEEK = ("lunedì martedì mercoledì giovedì venerdì sabato domenica".split(),
         "Monday Tuesday Wednesday Thursday Friday Saturday Sunday".split())
_MONTHS = ("gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre".split(),
           "January February March April May June July August September October November December".split())
DEADLINE_DAYS = {"B": 2, "D": 5}  # agenda days; U is the same day, P has none (R:91)
OFFER, HOLD = "offer", "hold"
TTL = {OFFER: 600.0, HOLD: 1200.0}  # real seconds a claim lives: weak (offer), strong (confirm/hold)
DOWN_TTL = 600.0
FULL_FRESH = 60.0  # check() trusts free=0 only from a page read this recently (D4)
ROW_TTL = timedelta(days=1)  # agenda time a row outlives its slot
MENU_MAX = 6  # slots in the LLM's menu, best first: it offers 1 to 3 (R:95)
GONE = frozenset({"NO_CAPACITY", "EXPIRED", "SLOT_TAKEN"})  # the slot is lost for every case
ENDS = frozenset({"STALE_FENCE", "FACILITY_CANCELLED", "RELEASE"})  # only the case's hold ended
_DAYS = {"lun": "mon", "mar": "tue", "mer": "wed", "gio": "thu", "ven": "fri", "sab": "sat", "dom": "sun"}
_NEVER: Version = (datetime.min.replace(tzinfo=timezone.utc), "")


class Prefs(NamedTuple):
    """What the citizen (or the gp's hand-off) asked for. Order only: S_pref first."""
    lang: str = "it"
    gender: str | None = None
    days: frozenset[str] = frozenset()


class Pick(NamedTuple):
    slots: list[Slot]  # S, best first; copies, safe to change
    why: dict[str, str]  # slot_id -> the first reason that left it out


def row(line: str) -> Slot:
    """A world row's tokens; the sdk format's agenda_date/agenda_day/capacity also as at/day/free."""
    slot = fields_of(line)
    for key, alias in (("at", "agenda_date"), ("day", "agenda_day"), ("free", "capacity")):
        if key not in slot and alias in slot:
            slot[key] = slot[alias]
    if "day" in slot:
        day = slot["day"].lower()[:3]  # Monday, MON, lunedì -> mon
        slot["day"] = _DAYS.get(day, day)
    return slot


def _doctor(doctor_id: str) -> tuple[frozenset[str], str] | None:
    if "asst-" in doctor_id and f"asst-{ASST}-" not in doctor_id:
        return None  # a partner's doctor: we know nothing about them
    return DOCTORS.get(doctor_id.rsplit("-", 1)[-1])


def speaks(doctor_id: str, lang: str) -> bool:
    spec = _doctor(doctor_id)
    return spec is not None and lang in spec[0]


def preferred(slot: Slot, prefs: Prefs) -> bool:
    """In S_pref: a doctor we know who speaks the language, is the gender, on one of the days."""
    spec = _doctor(slot.get("doctor_id", ""))
    return (spec is not None and prefs.lang in spec[0] and (not prefs.gender or spec[1] == prefs.gender)
            and (not prefs.days or slot.get("day") in prefs.days))


def deadline(priority: str | None, start: datetime | None) -> datetime | None:
    """The first agenda instant past the priority window (R:91) from ``start`` (the need's birth,
    R:253, or now); None for P or without a start. Unknown priority: 14 days."""
    if start is None or priority == "P":
        return None
    if priority == "U":
        return start.replace(hour=23, minute=59, second=59, microsecond=0)
    return start + timedelta(days=DEADLINE_DAYS.get(priority or "", 14))


def describe(slot: Slot, lang: str = "it") -> str:
    """The slot in words, from the Ledger's row and our registry only: day, date, time and, for
    our own doctors, gender and languages in full. Never an attribute the slot does not have
    (-2, R:243). Italian for "it", else English (the booking's _talk)."""
    en, at = int(lang != "it"), protocol.when(slot.get("at", ""))
    if at is None:
        return ""
    text = f"{_WEEK[en][at.weekday()]} {at.day} {_MONTHS[en][at.month - 1]}, {('ore ', '')[en]}{at:%H:%M}"
    spec = _doctor(slot.get("doctor_id", ""))
    if spec is None:
        return text
    names = [LANGS[code][en] for code in LANGS if code in spec[0]]
    spoken = names[0] if len(names) == 1 else ", ".join(names[:-1]) + (" e ", " and ")[en] + names[-1]
    female = spec[1] == "f"
    who = ("medico donna" if female else "medico uomo", "female doctor" if female else "male doctor")[en]
    return f"{text}, {who}, {('parla', 'speaks')[en]} {spoken}"


def menu(pick: Pick, lang: str = "it") -> dict[str, dict[str, dict[str, str]]]:
    """The harness Menu of an offer: slot_id -> describe() for the best MENU_MAX slots; {} when
    nothing may be offered, so the action is not in the menu. The harness aliases the ids."""
    slots = {slot["slot_id"]: describe(slot, lang) for slot in pick.slots[:MENU_MAX]}
    return {"offer": {"slot_ids": slots}} if slots else {}


def _safe(empty: Callable[[], Any]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def wrap(method: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(method)
        def run(self: Calendar, *args: Any, **kwargs: Any) -> Any:
            try:
                return method(self, *args, **kwargs)
            except Exception:  # noqa: BLE001 - a bad event or a caller's bug must not end the turn
                self.errors += 1
                _LOG.exception("calendar.%s.failed", method.__name__)
                return empty()
        return run
    return wrap


class _Slot:
    __slots__ = ("row", "at", "row_v", "read", "mark", "mark_v")

    def __init__(self) -> None:
        self.row: Slot | None = None
        self.at: datetime | None = None
        self.row_v, self.read = _NEVER, _NEVER[0]  # read: wall time we learned the row (check)
        self.mark, self.mark_v = "", _NEVER  # BOOKED or a GONE code, from the Ledger


class Calendar:
    """One per pod. Sync and without I/O: no ``await`` between candidates() and claim()."""

    def __init__(self, *, wall: Callable[[], datetime] | None = None) -> None:
        self.errors = 0
        self._wall = wall or (lambda: datetime.now(timezone.utc))
        self._slots: dict[str, _Slot] = {}
        self._claims: dict[str, tuple[str, str, datetime]] = {}  # slot -> (case, kind, instant)
        self._down: dict[str, datetime] = {}  # facility -> when marked DOWN

    @_safe(lambda: None)
    def learn(self, msg: Message, *, me: str, case: str = "", slot: str = "") -> None:
        """One message of a case history (``case`` = its need_id) or a push. ``slot``: the slot
        of OUR offer that a citizen's or partner's message names, resolved by the caller."""
        text, case = msg.text, case or msg.need_id or ""
        head, got = first_word(text), fields_of(text)
        version = (protocol.when(msg.sent_at or "") or self._wall(), msg.msg_id or "")
        if msg.sender == LEDGER:
            return self._ledger(text, head, got, version, got.get("need_id") or case, slot)
        word, at = (protocol.act(msg) if msg.sender == me or slot else ""), version[0]
        if msg.sender == me and word == "offer":
            for offer in offers_in(text):
                self._claim(offer.get("slot_id", ""), case, OFFER, at)
        elif word in ("confirm", "hold"):
            self._claim(got.get("slot_id") or slot, case, HOLD, at)
        elif word == "cancel":
            ours = got.get("offer_id", "")  # protocol.offer_line: offer_id=of-<slot_id>
            target = got.get("slot_id") or slot or (ours[3:] if ours.startswith("of-") else "")
            if msg.sender != me and got.get("reason", "").upper() in GONE:
                self._mark(target, got["reason"].upper(), version)
            self._release(target, case, at)

    def _ledger(self, text: str, head: str, got: dict[str, str], version: Version, case: str, slot: str) -> None:
        if head in ("slots", "world"):
            for line in text.splitlines():
                found = row(line) if "slot_id=" in line else {}
                entry = self._slots.setdefault(found["slot_id"], _Slot()) if found.get("slot_id") else None
                if entry is not None and version >= entry.row_v:
                    entry.row, entry.row_v, entry.read = found, version, self._wall()
                    entry.at = protocol.when(found.get("at", ""))
        elif head == "held":
            self._claim(got.get("slot_id", ""), case, HOLD, version[0])
        elif head == "committed":
            self._mark(got.get("slot_id", ""), "BOOKED", version)
        elif head in ("refused", "cancelled", "expired"):
            code = got.get("code", "").upper() if head == "refused" else "RELEASE"
            detail = got.get("detail", "")  # a slot id, or the first word of a sentence
            target = got.get("slot_id") or slot or (detail if any(ch.isdigit() for ch in detail) else "")
            if code not in GONE and code not in ENDS:
                return  # DUPLICATE, POLICY_VIOLATION, ...: nothing changes
            if code in GONE:
                self._mark(target, code, version)
            if target:
                self._release(target, case, version[0])
            else:  # no slot named: every hold of the case sent before it ends
                for sid in [s for s, c in self._claims.items() if c[0] == case and c[1] == HOLD and c[2] <= version[0]]:
                    del self._claims[sid]

    def _mark(self, slot_id: str, code: str, version: Version) -> None:
        entry = self._slots.setdefault(slot_id, _Slot()) if slot_id else None
        if entry and entry.mark != "EXPIRED" and (code == "EXPIRED" or version >= entry.mark_v):  # EXPIRED: for good
            entry.mark, entry.mark_v = code, version

    def _live(self, slot_id: str, wall: datetime) -> tuple[str, str]:
        """(case, kind) of the slot's claim while it lives, else ("", "")."""
        claim = self._claims.get(slot_id)
        return claim[:2] if claim and (wall - claim[2]).total_seconds() < TTL[claim[1]] else ("", "")

    def _claim(self, slot_id: str, case: str, kind: str, at: datetime) -> None:
        """The latest claim wins, but an offer never replaces a hold alive at its instant."""
        old = self._claims.get(slot_id)
        if slot_id and case and not (old and (old[1] == kind and old[2] > at or old[1] == HOLD and kind == OFFER
                                              and (at - old[2]).total_seconds() < TTL[HOLD])):
            self._claims[slot_id] = (case, kind, at)

    def _release(self, slot_id: str, case: str, at: datetime | None = None) -> None:
        old = self._claims.get(slot_id)
        if old and old[0] == case and (at is None or old[2] <= at):
            del self._claims[slot_id]

    @_safe(lambda: None)
    def mark(self, key: str, code: str, *, at: datetime | None = None) -> None:
        """An event the caller resolved: ``DOWN`` (key = facility, for DOWN_TTL s), ``BOOKED`` or a
        GONE code (key = slot). ``at``: its sent_at."""
        code, wall = code.upper(), self._wall()
        if code == "DOWN":
            self._down = {k: t for k, t in self._down.items() if (wall - t).total_seconds() < DOWN_TTL}
            self._down[key] = at or wall
        elif code in GONE or code == "BOOKED":
            self._mark(key, code, (at or wall, ""))

    @_safe(lambda: False)
    def claim(self, slot_id: str, case: str, kind: str = OFFER) -> bool:
        """Check-and-set: False when the slot is gone, booked or held by another case. ``offer`` is
        weak (D1: offered to others too, after every free slot); ``confirm``/``hold`` strong."""
        wall, entry = self._wall(), self._slots.get(slot_id)
        who, what = self._live(slot_id, wall)
        gone = entry is not None and self._state(entry) not in ("", "full")
        if not (slot_id and case) or gone or (what == HOLD and who != case):
            return False
        self._claim(slot_id, case, OFFER if kind == OFFER else HOLD, wall)
        return True

    @_safe(lambda: None)
    def release(self, slot_id: str, case: str) -> None:
        """Drop ``case``'s claim: our say failed, or the citizen cancelled."""
        self._release(slot_id, case)

    def _state(self, entry: _Slot) -> str:
        """The Ledger's word on the slot: the latest of its world row and its mark."""
        if entry.mark == "EXPIRED":
            return "gone:EXPIRED"
        if entry.mark and entry.mark_v >= entry.row_v:
            return "booked" if entry.mark == "BOOKED" else "gone:" + entry.mark
        free = entry.row.get("free", "") if entry.row is not None else ""
        return "full" if free.isdigit() and int(free) <= 0 else ""

    @_safe(lambda: Pick([], {}))
    def candidates(
        self, *, procedure: str | None, priority: str | None, now: datetime | None, margin: timedelta, case: str,
        born: datetime | None = None, prefs: Prefs = Prefs(), facility: str | None = None, relaxed: bool = False,
        skip: frozenset[str] | set[str] = frozenset(), prescription: str | None = None,
    ) -> Pick:
        """S for this case, best first: S_pref (R:230), then slots offered to no other case, then
        time; ``why`` for the rest. ``relaxed`` drops only the deadline (R:232). ``now``: agenda
        time; ``margin``: the caller's (D2); ``prescription``: its Ledger state, None if unknown."""
        wall = self._wall()
        if now is not None:  # a slot a day past is never needed again
            self._slots = {sid: e for sid, e in self._slots.items() if e.at is None or e.at + ROW_TTL >= now}
        self._claims = {sid: c for sid, c in self._claims.items() if self._live(sid, wall)[0]}
        horizon = None if relaxed else deadline(priority, born or now)
        rule = "waiting_list" if priority == "P" else "" if prescription in (None, "issued") else "prescription"
        down = {key for key, at in self._down.items() if (wall - at).total_seconds() < DOWN_TTL}
        ranked: list[tuple[bool, bool, datetime, str, Slot]] = []
        why: dict[str, str] = {}
        for sid, entry in self._slots.items():
            found, at, (who, what) = entry.row, entry.at, self._live(sid, wall)
            if found is None:
                continue
            if not (found.get("doctor_id") and found.get("facility") and at is not None):
                why[sid] = "unreadable"
            elif procedure and found.get("procedure") != procedure:
                why[sid] = "procedure"
            elif facility and found["facility"] != facility:
                why[sid] = "facility"
            elif rule or now is None:
                why[sid] = rule or "no_clock"
            elif at <= now:
                why[sid] = "past"
            elif at <= now + margin:
                why[sid] = "margin"
            elif horizon is not None and at >= horizon:
                why[sid] = "deadline"
            elif priority != "U" and found.get("urgent_reserve", "").lower() == "true":
                why[sid] = "reserve"
            elif state := (self._state(entry) or (f"held:{who}" if what == HOLD and who != case else "")
                           or ("down" if found["facility"] in down else "") or ("skip" if sid in skip else "")):
                why[sid] = state
            else:
                ranked.append((not preferred(found, prefs), what == OFFER and who != case, at, sid, found))
        ranked.sort(key=lambda item: item[:4])
        return Pick([dict(item[4]) for item in ranked], why)

    @_safe(lambda: "")
    def check(self, slot_id: str, *, now: datetime | None, margin: timedelta, case: str) -> str:
        """The guard right before a hold (R:678, D4): "" or why not (past, margin, full from a page
        read less than FULL_FRESH s ago, gone, booked, held). Never seen: "" (the Ledger decides)."""
        wall, entry = self._wall(), self._slots.get(slot_id)
        who, what = self._live(slot_id, wall)
        if entry is not None and now is not None and entry.at is not None and entry.at <= now + margin:
            return "past" if entry.at <= now else "margin"
        state = self._state(entry) if entry is not None else ""
        if state == "full" and (wall - entry.read).total_seconds() >= FULL_FRESH:
            state = ""
        return state or (f"held:{who}" if what == HOLD and who != case else "")


CALENDAR = Calendar()  # one per pod
