"""shared/mach/calendar.py, the eval levels a-c of docs/refactor/calendario.md §6, one pod:

a. unit on the real Ledger replies of tests/fixtures/*.txt, parity with the fitting() it
   replaces (verbatim copies below), every difference a declared change; describe() and menu();
b. properties against a Ledger oracle with the live semantics, seeds 0..199: I1-I4, I6-I8;
c. concurrency in one event loop (C1, with the control that must fail), permutations (C4).

uv run --no-project --with city-sdk==0.9.202 --with pytest python -m pytest tests/unit/test_calendar.py -q -s
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import math
import random
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from city_sdk.city import LEDGER, Message
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared"))
from mach import calendar, clock, harness, protocol  # noqa: E402
from mach.calendar import Calendar, Pick, Prefs  # noqa: E402

UTC = timezone.utc
ME, CITIZEN, PARTNER = "agent:me", "citizen:W01QQQ-24-900001", "agent:partner"
HOSPITAL, SPECIALIST, CONSULTORIO = "agent:m6py14a1ej77", "agent:h6krzxx6es83", "agent:yg8debxe3x0p"
N1, N2, N3 = "N-2026-11-02-890924900001", "N-2026-11-02-890924900002", "N-2026-11-02-890924900003"
HOUR = timedelta(hours=1)
DEC1_07 = datetime(2026, 12, 1, 7, tzinfo=UTC)
H1, H3 = "W01QQQ-asst-gorla-h1-20261201-", "W01QQQ-asst-gorla-h3-20261201-"
EVERY = [doc + hhmm for hhmm in ("0900", "1030", "1200", "1430", "1600") for doc in (H1, H3)]  # the 04:03 page


def blocks(name: str) -> list[str]:
    text = (ROOT / "tests" / "fixtures" / f"{name}.txt").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    return [b.strip() for b in "\n".join(lines).split("\n---\n") if b.strip()]


SLOTS, AGENDA, HELD, COMMITTED, REFUSED = (blocks(n) for n in ("slots", "agenda", "held", "committed", "refused"))


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def stamp(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


class Wall:
    """A fake wall clock: the calendar's only real time (claim lifetimes, page freshness)."""

    def __init__(self, start: str = "2026-11-21T04:03:30Z") -> None:
        self.t = at(start)

    def __call__(self) -> datetime:
        return self.t

    def tick(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def msg(sender: str, text: str, mid: str, *, sent: str = "2026-11-21T04:03:00Z", to: str = ME,
        need: str | None = None) -> Message:
    return Message(type="chat", sender=sender, recipient=to, msg_id=mid, need_id=need, body={"text": text}, sent_at=sent)


def fed(*texts: str, wall: Wall | None = None, sent: str = "2026-11-21T04:03:00Z") -> Calendar:
    cal = Calendar(wall=wall or Wall())
    for i, text in enumerate(texts):
        cal.learn(msg(LEDGER, text, f"w{i}", sent=sent), me=ME)
    return cal


def pick(cal: Calendar, **kw) -> Pick:
    args = {"procedure": "VIS-FIS", "priority": "B", "now": DEC1_07, "margin": HOUR, "case": "c", **kw}
    return cal.candidates(**args)


def ids(found: Pick) -> list[str]:
    return [slot["slot_id"] for slot in found.slots]


def line(sid: str, *, free: int = 1) -> str:
    """A world row in the live format for a slot id W01QQQ-asst-gorla-<doctor>-<yyyymmdd>-<hhmm>."""

    doctor, day, hhmm = sid.split("-")[-3:]
    when = datetime.strptime(day + hhmm, "%Y%m%d%H%M").replace(tzinfo=UTC)
    procedure, facility = {"h": ("VIS-FIS", HOSPITAL), "s": ("VIS-CARD", SPECIALIST), "c": ("CONS-GIN", CONSULTORIO)}[doctor[0]]
    reserve = " urgent_reserve=true" if hhmm == "0900" else ""
    name = f"{when:%a}".lower()
    return (f"slot_id={sid} doctor_id=asst-gorla-{doctor} facility={facility} procedure={procedure} "
            f"at={when:%Y-%m-%dT%H:%M:%S}.000Z day={name} free={free}{reserve}")


def page(*rows: str, number: str = "1/1", wave: str = "W01QQQ") -> str:
    return "\n".join([f"slots page={number} matching={len(rows)} asst=asst-gorla wave_id={wave}", *rows])


def ledger(cal: Calendar) -> dict:
    """The Ledger's word the calendar keeps, versioned (I8): rows and marks, not the wall time."""

    return {sid: (e.row, e.row_v, e.mark, e.mark_v) for sid, e in cal._slots.items()}


def dump(cal: Calendar) -> tuple:
    return ledger(cal), dict(cal._claims)


# --- a. the real Ledger replies ------------------------------------------------------------------


def test_every_world_row_is_learned_as_the_ledger_wrote_it():
    cal = fed(*SLOTS)
    rows = {protocol.fields_of(r)["slot_id"]: protocol.fields_of(r) for b in SLOTS for r in b.splitlines() if "slot_id=" in r}
    assert cal.errors == 0 and set(cal._slots) == set(rows) and len(rows) == 75
    for sid, tokens in rows.items():
        assert cal._slots[sid].row == tokens  # R:243: the offer carries the Ledger's own tokens
        assert (tokens.get("urgent_reserve") == "true") == sid.endswith("-0900")  # the reserve is the 09:00 slot


def test_a_page_with_every_slot_taken_is_full():
    cal = fed(SLOTS[2])  # 00:51: Thursday 19 November, free=0 everywhere
    found = pick(cal, priority="U", relaxed=True, now=at("2026-11-18T08:00:00Z"))
    assert found.slots == [] and len(found.why) == 10 and set(found.why.values()) == {"full"}


def test_the_consultorio_page_with_one_full_slot():
    cal = fed(SLOTS[7])
    found = pick(cal, procedure="CONS-GIN", priority="U")
    day = "W01QQQ-asst-gorla-c1-20261201-"
    assert ids(found) == [day + "0900", day + "1200", day + "1430", day + "1600"]
    assert found.why[day + "1030"] == "full" and found.why["W01QQQ-asst-gorla-c1-20261202-1030"] == "deadline"


@pytest.mark.parametrize("priority", ["B", "D", "U"])
def test_exact_result_on_the_0403_block(priority):
    anchor = clock.anchor(protocol.fields_of(AGENDA[3]))  # booking, 04:03 UTC: Tuesday 1 December, 20:49
    cal = fed(SLOTS[0])  # hospital, 04:03 UTC: page 1/8 from 2026-12-01
    # The reply's own reading: page 1 starts "today", and today is over (R:91, 08-18): nothing left.
    late = pick(cal, priority=priority, now=anchor.agenda_now)
    assert late.slots == [] and late.why == dict.fromkeys(EVERY, "past")
    # The same anchor 16'50" real earlier: 07:00 of that Tuesday, the ambulatories not open yet.
    now = clock.now(anchor, at("2026-11-21T03:46:09.977Z"))
    assert now == DEC1_07
    found = pick(cal, priority=priority, now=now)
    want = EVERY if priority == "U" else EVERY[2:]  # U may use the reserve, B and D never (R:718, D3)
    assert ids(found) == want
    assert found.why == ({} if priority == "U" else {H1 + "0900": "reserve", H3 + "0900": "reserve"})
    tokens = {protocol.fields_of(r)["slot_id"]: protocol.fields_of(r) for r in SLOTS[0].splitlines() if "slot_id=" in r}
    assert all(slot == tokens[slot["slot_id"]] for slot in found.slots)
    albanian = pick(cal, priority=priority, now=now, prefs=Prefs("sq"))  # only h3 speaks sq: S_pref first
    assert ids(albanian) == [s for s in want if s.startswith(H3)] + [s for s in want if s.startswith(H1)]


def test_the_deadline_counts_from_birth_and_relaxed_drops_only_it():
    cal = fed(SLOTS[1], SLOTS[3], SLOTS[4])  # Monday 30 Nov - Tuesday 1 Dec, Thu 3 - Fri 4, Thu 10 - Fri 11
    now, born = at("2026-11-30T07:00:00Z"), at("2026-11-28T00:00:00Z")
    days = lambda found: sorted({sid.split("-")[-2] for sid in ids(found)})  # noqa: E731
    fresh = pick(cal, priority="D", now=now)  # from now: before Sat 5 Dec 07:00
    aged = pick(cal, priority="D", now=now, born=born)  # from birth: before Thu 3 Dec 00:00 (R:253)
    relaxed = pick(cal, priority="D", now=now, born=born, relaxed=True)
    assert days(fresh) == ["20261130", "20261201", "20261203", "20261204"]
    assert days(aged) == ["20261130", "20261201"]
    assert {aged.why[s] for s in ids(fresh) if s not in ids(aged)} == {"deadline"}
    assert days(relaxed) == ["20261130", "20261201", "20261203", "20261204", "20261210", "20261211"]
    assert "deadline" not in relaxed.why.values()
    assert {s for s, why in relaxed.why.items() if why == "reserve"} == {s for s in cal._slots if s.endswith("-0900")}
    assert days(pick(cal, priority="U", now=now)) == ["20261130"]  # U: the same agenda day
    assert set(pick(cal, priority="U", now=now, born=at("2026-11-27T12:00:00Z")).why.values()) <= {"deadline", "past"}


def test_priority_p_and_a_prescription_not_issued_get_no_slot():
    cal = fed(SLOTS[0])
    for found, reason in ((pick(cal, priority="P"), "waiting_list"), (pick(cal, priority="P", relaxed=True), "waiting_list"),
                          (pick(cal, prescription="expired"), "prescription"), (pick(cal, prescription="used"), "prescription")):
        assert found.slots == [] and found.why == dict.fromkeys(EVERY, reason)  # R:91, R:716
    assert ids(pick(cal, prescription="issued")) == ids(pick(cal)) == EVERY[2:]


def test_held_then_committed_is_seen_by_every_other_case():
    held, committed = protocol.fields_of(HELD[2]), protocol.fields_of(COMMITTED[2])  # hospital, 00:45-00:46
    sid, need = held["slot_id"], held["need_id"]
    assert committed["slot_id"] == sid
    wall = Wall("2026-11-21T00:46:30Z")
    cal = fed(page(line(sid)), wall=wall, sent="2026-11-21T00:40:00Z")
    now = at("2026-11-20T08:00:00Z")
    cal.learn(msg(LEDGER, HELD[2], "h", sent="2026-11-21T00:45:00Z"), me=ME)
    assert pick(cal, priority="U", now=now, case=N2).why[sid] == f"held:{need}"
    assert ids(pick(cal, priority="U", now=now, case=need)) == [sid]  # our own hold
    cal.learn(msg(LEDGER, COMMITTED[2], "c", sent="2026-11-21T00:46:00Z"), me=ME)
    assert pick(cal, priority="U", now=now, case=N2).why[sid] == "booked"
    assert pick(cal, priority="U", now=now, case=need).why[sid] == "booked"
    wall.tick(3600)  # the hold lapsed long ago; booked stays until the slot is past
    assert pick(cal, priority="U", now=now, case=N2).why[sid] == "booked"


@pytest.mark.parametrize("index", [7, 8, 9])
def test_refused_no_capacity_marks_the_slot_in_detail(index):
    sid = protocol.fields_of(REFUSED[index])["detail"]
    procedure = protocol.fields_of(line(sid))["procedure"]
    now = datetime.strptime(sid.split("-")[-2], "%Y%m%d").replace(hour=7, tzinfo=UTC)
    cal = fed(page(line(sid)), sent="2026-11-21T03:00:00Z")
    cal.learn(msg(LEDGER, REFUSED[index], "r", sent="2026-11-21T03:01:00Z"), me=ME)
    for case in (N1, N2):
        assert pick(cal, procedure=procedure, priority="U", now=now, case=case).why[sid] == "gone:NO_CAPACITY"
    cal.learn(msg(LEDGER, page(line(sid)), "w-late", sent="2026-11-21T03:00:30Z"), me=ME)  # older page, late
    assert pick(cal, procedure=procedure, priority="U", now=now).why[sid] == "gone:NO_CAPACITY"
    cal.learn(msg(LEDGER, page(line(sid)), "w-new", sent="2026-11-21T03:02:00Z"), me=ME)  # newer page: free again (I4)
    assert ids(pick(cal, procedure=procedure, priority="U", now=now)) == [sid]
    cal.learn(msg(LEDGER, page(line(sid, free=0)), "w-0", sent="2026-11-21T03:03:00Z"), me=ME)
    assert pick(cal, procedure=procedure, priority="U", now=now).why[sid] == "full"
    cal.learn(msg(LEDGER, page(line(sid)), "w-older", sent="2026-11-21T03:02:30Z"), me=ME)  # free=1, older: no (I3)
    assert pick(cal, procedure=procedure, priority="U", now=now).why[sid] == "full"


def test_refused_expired_needs_a_slot_or_ends_the_cases_hold():
    assert protocol.fields_of(REFUSED[5]) == {"code": "EXPIRED", "detail": "slot"}
    x = H1 + "1030"
    hold = msg(ME, f"hold slot_id={x} need_id={N1}", "hold", to=LEDGER, need=N1, sent="2026-11-21T04:02:00Z")
    refused = msg(LEDGER, REFUSED[5], "r", need=N1, sent="2026-11-21T04:02:40Z")
    cal = fed(SLOTS[0])
    cal.learn(hold, me=ME, case=N1)
    assert pick(cal, case=N2).why[x] == f"held:{N1}"
    cal.learn(refused, me=ME, case=N1)  # detail=slot names no slot: N1's hold ends, the slot is not marked
    assert x in ids(pick(cal, case=N2)) and cal._slots[x].mark == ""
    named = fed(SLOTS[0])
    named.learn(hold, me=ME, case=N1)
    named.learn(refused, me=ME, case=N1, slot=x)  # the caller paired it with our hold: gone for good
    assert pick(named, case=N2).why[x] == "gone:EXPIRED"
    named.learn(msg(LEDGER, SLOTS[0], "w-new", sent="2026-11-21T04:10:00Z"), me=ME)
    assert pick(named, case=N2).why[x] == "gone:EXPIRED"  # never reopened, even by a newer page


@pytest.mark.parametrize("index", [3, 4, 6])
def test_stale_fence_and_facility_cancelled_end_only_that_cases_hold(index):
    x, y = H1 + "1030", H3 + "1030"
    cal = fed(SLOTS[0])
    cal.learn(msg(ME, f"hold slot_id={x}", "h1", to=LEDGER, need=N1, sent="2026-11-21T04:02:00Z"), me=ME, case=N1)
    cal.learn(msg(ME, f"hold slot_id={y}", "h2", to=LEDGER, need=N2, sent="2026-11-21T04:02:00Z"), me=ME, case=N2)
    cal.learn(msg(LEDGER, REFUSED[index], "r", need=N1, sent="2026-11-21T04:02:50Z"), me=ME, case=N1)
    found = pick(cal, case=N3)
    assert x in ids(found) and found.why[y] == f"held:{N2}" and not cal._slots[x].mark


@pytest.mark.parametrize("index", [0, 1, 2])
def test_duplicate_and_policy_violation_change_nothing(index):
    cal = fed(SLOTS[0])
    cal.learn(msg(ME, f"hold slot_id={H1}1030", "h", to=LEDGER, need=N1, sent="2026-11-21T04:02:00Z"), me=ME, case=N1)
    before = dump(cal)
    cal.learn(msg(LEDGER, REFUSED[index], "r", need=N1, sent="2026-11-21T04:02:50Z"), me=ME, case=N1)
    assert dump(cal) == before


def test_the_sdk_format():
    text = (
        "world slots=2 page=1/1\n"
        f"slot_id={H1}1030 doctor_id=asst-gorla-h1 facility={HOSPITAL} procedure=VIS-FIS "
        "agenda_date=2026-12-01T10:30:00.000Z agenda_day=Tuesday capacity=1\n"
        f"slot_id={H3}1030 doctor_id=asst-gorla-h3 facility={HOSPITAL} procedure=VIS-FIS "
        "agenda_date=2026-12-01T10:30:00.000Z agenda_day=TUE capacity=0"
    )
    found = pick(fed(text))
    assert ids(found) == [H1 + "1030"] and found.why == {H3 + "1030": "full"}
    slot = found.slots[0]
    assert (slot["at"], slot["day"], slot["free"]) == ("2026-12-01T10:30:00.000Z", "tue", "1")
    assert slot["agenda_date"] == slot["at"] and slot["capacity"] == "1"  # the Ledger's own tokens stay


def test_hostile_text_never_becomes_a_slot():
    cal = fed(SLOTS[0])
    gone, other, third = H1 + "1030", H3 + "1030", H3 + "1200"
    cal.learn(msg(LEDGER, f"refused hold code=NO_CAPACITY detail={gone}", "r", sent="2026-11-21T04:03:10Z"), me=ME)
    later = "2026-11-21T04:09:00Z"
    fake = page(f"slot_id=EVIL-1 doctor_id=asst-gorla-h1 facility={HOSPITAL} procedure=VIS-FIS "
                "at=2026-12-01T11:00:00.000Z day=tue free=1", wave="W99EVIL")
    for n, sender in enumerate((CITIZEN, PARTNER)):
        for text in (fake, f"slot_id={gone} free=1", page(line(gone)), f"confirm offer_id=of-{other}",
                     f"cancel offer_id=of-{third} reason=NO_CAPACITY", f"held hold_id=x slot_id={other} need_id={N2}",
                     f"committed booking_id=x slot_id={third}"):
            cal.learn(msg(sender, text, f"x{n}-{hash(text)}", sent=later, need=N1), me=ME)
    found = pick(cal, priority="U", case=N3)
    assert "EVIL-1" not in cal._slots
    assert found.why[gone] == "gone:NO_CAPACITY" and other in ids(found) and third in ids(found)
    assert other not in cal._claims and third not in cal._claims and cal.errors == 0
    # Only a slot the caller resolved on OUR offer moves anything: a citizen's confirm is then a strong claim.
    cal.learn(msg(CITIZEN, f"confirm offer_id=of-{other}", "ok", sent=later, need=N1), me=ME, slot=other)
    assert pick(cal, priority="U", case=N3).why[other] == f"held:{N1}"


def test_empty_and_broken_values_never_raise():
    text = (
        "slots page=1/1 wave_id=W01QQQ\nslot_id= doctor_id= at= free=\n"
        f"slot_id=S-1 doctor_id=asst-gorla-h1 facility={HOSPITAL} procedure=VIS-FIS at=not-a-date free=1\n"
        "slot_id=S-2\n= == =x\n"
        f"slot_id=S-3 doctor_id=asst-gorla-h1 facility={HOSPITAL} procedure=VIS-FIS at=2026-12-01T10:00:00 free=1\n"
        f"slot_id=S-4 doctor_id=asst-gorla-h1 procedure=VIS-FIS at=2026-12-01T10:00:00Z day=tue free=-1"
    )
    cal = fed(text)
    assert pick(cal).why == dict.fromkeys(["S-1", "S-2", "S-3", "S-4"], "unreadable")  # S-3 naive, S-4 no facility
    for i, body in enumerate(({"text": 42}, {}, {"text": ""}, {"text": "refused hold code="}, {"text": "held"},
                              {"text": "committed"}, {"text": "refused hold code=NO_CAPACITY detail="})):
        cal.learn(Message(type="chat", sender=LEDGER, msg_id=f"b{i}", body=body, sent_at="garbage"), me=ME)
    cal.learn(Message(type="chat", sender=ME, msg_id="", body={"text": "offer offer_id= slot_id="}), me=ME)
    assert cal.claim("", N1) is False and cal.check("nope", now=None, margin=HOUR, case="") == ""
    cal.mark("", "DOWN")
    cal.release("nope", N1)
    assert calendar.describe({}) == calendar.describe({"at": "x", "doctor_id": "asst-gorla-h1"}) == ""
    assert calendar.menu(Pick([], {})) == {}
    assert cal.errors == 0 and pick(cal).why == dict.fromkeys(["S-1", "S-2", "S-3", "S-4"], "unreadable")
    # A caller's bug (a naive now) is caught, counted and answered with nothing: never raised.
    assert pick(cal, now=datetime(2026, 12, 1, 7)) == Pick([], {}) and cal.errors == 1


def test_a_10000_line_page_is_fast():
    rows = [f"slot_id=BIG-{i} doctor_id=asst-gorla-x{i % 7} facility={HOSPITAL} procedure=VIS-FIS "
            f"at={stamp(DEC1_07 + timedelta(minutes=10 + i))} day=tue free=1" for i in range(10_000)]
    cal = Calendar(wall=Wall())
    start = time.perf_counter()
    cal.learn(msg(LEDGER, page(*rows), "big"), me=ME)
    learned = time.perf_counter() - start
    start = time.perf_counter()
    found = pick(cal, relaxed=True)
    answered = time.perf_counter() - start
    assert cal.errors == 0 and len(cal._slots) == 10_000 and set(found.why.values()) == {"margin"}
    assert len(found.slots) + len(found.why) == 10_000 and learned < 2.0 and answered < 0.5, (learned, answered)


def test_learning_twice_is_learning_once():
    cal = fed(*SLOTS)
    events = [msg(LEDGER, b, f"w{i}") for i, b in enumerate(SLOTS)] + [
        msg(LEDGER, HELD[9], "h", sent="2026-11-21T04:03:20Z"), msg(LEDGER, REFUSED[7], "r", sent="2026-11-21T04:03:21Z"),
        msg(ME, f"offer offer_id=of-{H1}1200 slot_id={H1}1200 offer_id=of-{H3}1200 slot_id={H3}1200", "o", to=CITIZEN, need=N1),
    ]
    for event in events:
        cal.learn(event, me=ME)
    once = dump(cal)
    for event in events:
        cal.learn(event, me=ME)  # redelivered: the versions make it a no-op
    assert dump(cal) == once and len(once[1]) == 3


def test_claims_d1_and_their_lifetimes():
    wall = Wall()
    cal = fed(SLOTS[0], wall=wall)
    x, y = H1 + "1030", H3 + "1030"
    assert ids(pick(cal, case=N1))[:2] == [x, y]
    assert cal.claim(x, N2) and cal.claim(x, N3)  # D1: an offer is weak, other cases may be offered x too
    assert ids(pick(cal, case=N1))[-1] == x  # ...but after every slot nobody else was offered
    english = ids(pick(cal, case=N1, prefs=Prefs("en")))  # S_pref (h1 speaks en) still comes first
    assert english[:4] == [H1 + "1200", H1 + "1430", H1 + "1600", x] and english[4:] == [s for s in EVERY[2:] if s.startswith(H3)]
    assert cal.claim(x, N1, "confirm")  # strong
    assert not cal.claim(x, N2, "hold") and not cal.claim(x, N2)  # blocks every other case, offers too
    assert pick(cal, case=N2).why[x] == f"held:{N1}" and x in ids(pick(cal, case=N1))
    assert cal.claim(x, N1) and cal._claims[x][:2] == (N1, "hold")  # an offer never weakens N1's own hold
    wall.tick(1199)
    assert pick(cal, case=N2).why[x] == f"held:{N1}"
    wall.tick(1)  # 1200 s: the Ledger never answered, the claim lapses
    assert x in ids(pick(cal, case=N2)) and cal.claim(x, N2, "hold")
    cal.release(x, N2)
    assert cal.claim(x, N3, "hold")
    assert cal.claim(y, N2)
    wall.tick(599)
    assert ids(pick(cal, case=N1))[-1] == y
    wall.tick(1)  # 600 s: the offer lapses
    assert ids(pick(cal, case=N1))[0] == y
    cal.mark(y, "BOOKED")
    assert not cal.claim(y, N1) and not cal.claim(y, N1, "hold")


def test_a_facility_marked_down_comes_back_after_600_s():
    wall = Wall()
    cal = fed(SLOTS[0], wall=wall)
    cal.mark(HOSPITAL, "DOWN")
    assert set(pick(cal).why.values()) == {"down", "reserve"}
    wall.tick(599)
    assert pick(cal).slots == []
    wall.tick(1)
    assert ids(pick(cal)) == EVERY[2:]


def test_check_before_the_hold():
    wall = Wall()
    cal = fed(SLOTS[7], wall=wall)  # consultorio: c1 1 Dec 10:30 has free=0
    day = "W01QQQ-asst-gorla-c1-20261201-"

    def check(sid, case=N1, now=DEC1_07):
        return cal.check(sid, now=now, margin=HOUR, case=case)

    assert check(day + "1030") == "full"
    wall.tick(59)
    assert check(day + "1030") == "full"
    wall.tick(1)  # D4: a page 60 s old does not stop a hold, the Ledger decides
    assert check(day + "1030") == ""
    assert check(day + "1200", now=at("2026-12-01T12:00:00Z")) == "past"
    assert check(day + "1200", now=at("2026-12-01T11:00:00Z")) == "margin"
    assert check("W01QQQ-asst-gorla-c1-20261201-0800") == ""  # never seen
    assert cal.claim(day + "1200", N2, "hold")
    assert check(day + "1200") == f"held:{N2}" and check(day + "1200", case=N2) == ""
    cal.learn(msg(LEDGER, f"refused hold code=NO_CAPACITY detail={day}1430", "r", sent="2026-11-21T04:03:10Z"), me=ME)
    cal.learn(msg(LEDGER, f"committed booking_id=b slot_id={day}1600", "c", sent="2026-11-21T04:03:10Z"), me=ME)
    assert check(day + "1430") == "gone:NO_CAPACITY" and check(day + "1600") == "booked"


# --- describe() and menu(): what the LLM may say and pick ----------------------------------------

ROW = {protocol.fields_of(r)["slot_id"]: calendar.row(r) for b in SLOTS for r in b.splitlines() if "slot_id=" in r}


@pytest.mark.parametrize(("doctor", "it", "en"), [
    ("h1", "medico donna, parla italiano e inglese", "female doctor, speaks Italian and English"),
    ("h2", "medico uomo, parla italiano", "male doctor, speaks Italian"),
    ("h3", "medico uomo, parla italiano e albanese", "male doctor, speaks Italian and Albanian"),
])
def test_describe_says_only_what_the_row_and_the_registry_say(doctor, it, en):
    slot = calendar.row(line(f"W01QQQ-asst-gorla-{doctor}-20261202-1430"))
    assert calendar.describe(slot, "it") == f"mercoledì 2 dicembre, ore 14:30, {it}"
    assert calendar.describe(slot, "en") == f"Wednesday 2 December, 14:30, {en}"
    assert calendar.describe(slot, "sq") == calendar.describe(slot, "en")  # we write Italian or English
    # A doctor we do not know, or another ASST's h3 (another person): the time only, never a guess.
    for other in ("asst-gorla-x9", f"asst-giambellino-{doctor}"):
        assert calendar.describe({**slot, "doctor_id": other}, "it") == "mercoledì 2 dicembre, ore 14:30"
    assert not calendar.speaks(f"asst-giambellino-{doctor}", "it")


def test_describe_every_real_row():
    for slot in ROW.values():
        for lang, words in (("it", calendar.LANGS.values()), ("en", calendar.LANGS.values())):
            text = calendar.describe(slot, lang)
            spec = calendar.DOCTORS[slot["doctor_id"].rsplit("-", 1)[-1]]
            named = {code for code, names in calendar.LANGS.items() if names[lang == "en"] in text}
            assert named == spec[0] and text.startswith(calendar._WEEK[lang == "en"][at(slot["at"]).weekday()])
            assert not any(bad in text.lower() for bad in ("=", "reserve", "urgent", "slav", "free", "slot"))
    sdk = calendar.row("slot_id=S doctor_id=asst-gorla-c1 agenda_date=2026-12-04T09:00:00.000Z agenda_day=Friday capacity=1")
    assert calendar.describe(sdk, "en") == "Friday 4 December, 09:00, female doctor, speaks Italian and English"


def test_menu_never_holds_a_forbidden_slot():
    cal, seen = fed(*SLOTS), {"menus": 0, "reserve_for_u": 0, "empty": 0}
    for now in [at("2026-11-18T06:00:00Z") + timedelta(hours=13 * i) for i in range(30)]:
        for procedure, priority, rx, lang in itertools.product(
            ["VIS-FIS", "VIS-CARD", "CONS-GIN"], ["U", "B", "D", "P"], [None, "issued", "expired"], ["it", "en"]
        ):
            found = pick(cal, procedure=procedure, priority=priority, now=now, prescription=rx, relaxed=priority == "B")
            menu = calendar.menu(found, lang)
            if priority == "P" or rx == "expired" or not found.slots:  # R:91, R:716: no offer at all
                assert menu == {}
                seen["empty"] += 1
                continue
            labels = menu["offer"]["slot_ids"]
            assert list(menu) == ["offer"] and list(labels) == ids(found)[:calendar.MENU_MAX]
            for sid, label in labels.items():
                slot = ROW[sid]
                assert priority == "U" or slot.get("urgent_reserve") != "true", (sid, priority)  # R:718
                assert at(slot["at"]) > now + HOUR and slot["free"] != "0" and slot["procedure"] == procedure
                assert label == calendar.describe(slot, lang)
                seen["reserve_for_u"] += slot.get("urgent_reserve") == "true"
            seen["menus"] += 1
    assert seen["menus"] > 100 and seen["reserve_for_u"] > 0 and seen["empty"] > 100  # the test sees both sides


class Slots(BaseModel):
    slot_ids: list[str] = Field(min_length=1, max_length=3)


@harness.tool(description="send 1 to 3 slots to the citizen in one message")
def offer(args: Slots, ctx: Any) -> None: ...


def test_menu_renders_in_the_harness_without_ids():
    menu = calendar.menu(pick(fed(SLOTS[0]), priority="U"), "it")
    agent = harness.Agent(role="booking_centre", tools=[offer], rules="Offer slots of the MENU.", llm=None)
    turn = harness.Turn("booking_centre", {"priority": "U"}, menu, "vorrei un posto", "it")
    turn.aliases = harness._aliases(menu)
    prompt = harness.render(agent, turn)[1]["content"]
    assert sorted(turn.aliases.values()) == ["A", "B", "C", "D", "E", "F"]  # MENU_MAX, all aliased
    assert '"C" = martedì 1 dicembre, ore 10:30, medico donna, parla italiano e inglese' in prompt
    assert not any(sid in prompt for sid in ROW) and "urgent" not in prompt


# --- parity with the fitting() the calendar replaces --------------------------------------------
# Verbatim copies at 5a13e63, each run in its own namespace (same names): booking_centre/agent/
# agent.py:91-100 (_DOCTORS, _DEADLINE_DAYS), 1706-1716 (_RARE_LANG, _speaks), 1752-1835
# (_preferred, fitting, _IT_DAYS, _slot); shared/facility.py:37-50 (_LEAD, _DOCTORS, _RARE,
# _DEADLINE_DAYS), 340-353 (_IT_DAYS, _slot), 456-511 (_speaks, fitting). Only B's _lead() is a
# parameter (B:1956: 1 h + the Ledger's round trip).

BOOKING = '''
_DOCTORS: dict[str, tuple[frozenset[str], str, frozenset[str]]] = {
    "h1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed"})),
    "h2": (frozenset({"it"}), "m", frozenset({"wed", "thu", "fri"})),
    "h3": (frozenset({"it", "*"}), "m", frozenset({"tue", "thu"})),
    "s1": (frozenset({"it", "en"}), "m", frozenset({"mon", "wed", "fri"})),
    "s2": (frozenset({"it"}), "f", frozenset({"tue", "thu"})),
    "c1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed", "thu", "fri"})),
}
# Priority -> agenda days to the deadline. U is the same agenda day, P is the waiting list.
_DEADLINE_DAYS = {"B": 2, "D": 5}
_RARE_LANG = "sq"


def _speaks(doctor_id: str, lang: str) -> bool:
    spec = _DOCTORS.get(doctor_id.rsplit("-", 1)[-1])
    if spec is None:
        return False
    if lang in spec[0]:
        return True
    return "*" in spec[0] and lang == _RARE_LANG


def _preferred(slot: dict[str, str], lang: str, gender: str | None, days: frozenset[str]) -> bool:
    doctor = slot.get("doctor_id", "")
    spec = _DOCTORS.get(doctor.rsplit("-", 1)[-1])
    return (
        spec is not None and _speaks(doctor, lang)
        and (not gender or spec[1] == gender) and (not days or slot.get("day") in days)
    )


def fitting(
    lines: list[str],
    *,
    now: datetime,
    priority: str | None,
    lang: str,
    gender: str | None = None,
    procedure: str | None = None,
    days: frozenset[str] = frozenset(),
    reserve: bool = True,
    relaxed: bool = False,
    born: datetime | None = None,
) -> list[dict[str, str]]:
    """The answer set S: future, free, in the priority window, outside the urgent reserve when
    ``reserve``. S_pref first (doctor speaks ``lang``, is ``gender``, on one of ``days``), then
    the rest of S, earliest first. ``relaxed`` drops the priority window (S is empty)."""

    if relaxed or priority == "P":
        horizon: datetime | None = None
    elif priority == "U":
        horizon = (born or now).replace(hour=23, minute=59, second=59, microsecond=0)
    else:
        # The deadline runs from the need's birth, not from now: the judge's S is computed when
        # the need is born, and a slow chain eats the window (journey 0001340002: refused for
        # the deadline a slot that was inside ours).
        horizon = (born or now) + timedelta(days=_DEADLINE_DAYS.get(priority or "", 14))
    earliest = now + _lead()
    chosen: list[tuple[bool, datetime, str, dict[str, str]]] = []
    for line in lines:
        slot = _slot(line)
        slot_id = slot.get("slot_id")
        doctor = slot.get("doctor_id")
        at = protocol.when(slot.get("at", ""))
        if not slot_id or not doctor or not slot.get("facility") or at is None:
            continue
        if procedure and slot.get("procedure") not in (None, procedure):
            continue
        free = slot.get("free")
        if free is not None and free.isdigit() and int(free) <= 0:
            continue
        # The urgent reserve is only for priority U (rule P3).
        if reserve and slot.get("urgent_reserve", "").lower() == "true" and priority != "U":
            continue
        if not earliest < at or (horizon is not None and at >= horizon):
            continue
        spec = _DOCTORS.get(doctor.rsplit("-", 1)[-1])
        day = slot.get("day", "")
        if spec is not None and day and day not in spec[2]:
            continue
        # A doctor missing from our table is still in S; we just cannot call it preferred.
        preferred = _preferred(slot, lang, gender, days)
        chosen.append((not preferred, at, slot_id, slot))
    chosen.sort(key=lambda item: item[:3])
    return [item[3] for item in chosen]


_IT_DAYS = {"lun": "mon", "mar": "tue", "mer": "wed", "gio": "thu", "ven": "fri", "sab": "sat", "dom": "sun"}


def _slot(line: str) -> dict[str, str]:
    """A Ledger slot row with our keys ``at``/``day``/``free``, from the kit example's format or
    the Ledger schema's ``agenda_date``/``agenda_day``/``capacity`` (ledger.py Slot)."""

    slot = fields_of(line)
    for key, alias in (("at", "agenda_date"), ("day", "agenda_day"), ("free", "capacity")):
        if key not in slot and alias in slot:
            slot[key] = slot[alias]
    if "day" in slot:
        day = slot["day"].lower()[:3]  # Monday, MON, lunedì -> mon
        slot["day"] = _IT_DAYS.get(day, day)
    return slot
'''

FACILITY = '''
_LEAD = timedelta(hours=4)
# Doctor key (the last segment of doctor_id) -> languages, gender, days, from the registry
# (console team/world). The Ledger's slot line names the doctor; it does not repeat this table.
_DOCTORS: dict[str, tuple[frozenset[str], str, frozenset[str]]] = {
    "h1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed"})),
    "h2": (frozenset({"it"}), "m", frozenset({"wed", "thu", "fri"})),
    "h3": (frozenset({"it", "sq"}), "m", frozenset({"tue", "thu"})),
    "s1": (frozenset({"it", "en"}), "m", frozenset({"mon", "wed", "fri"})),
    "s2": (frozenset({"it"}), "f", frozenset({"tue", "thu"})),
    "c1": (frozenset({"it", "en"}), "f", frozenset({"mon", "tue", "wed", "thu", "fri"})),
}
# Our ASST's rare language: only h3 speaks it, and partners ask us for it (registry).
_RARE = "sq"
_DEADLINE_DAYS = {"B": 2, "D": 5}

_IT_DAYS = {"lun": "mon", "mar": "tue", "mer": "wed", "gio": "thu", "ven": "fri", "sab": "sat", "dom": "sun"}


def _slot(line: str) -> dict[str, str]:
    """A Ledger slot row with our keys ``at``/``day``/``free``, from the kit example's format or
    the Ledger schema's ``agenda_date``/``agenda_day``/``capacity`` (ledger.py Slot)."""

    slot = fields_of(line)
    for key, alias in (("at", "agenda_date"), ("day", "agenda_day"), ("free", "capacity")):
        if key not in slot and alias in slot:
            slot[key] = slot[alias]
    if "day" in slot:
        day = slot["day"].lower()[:3]  # Monday, MON, lunedì -> mon
        slot["day"] = _IT_DAYS.get(day, day)
    return slot


def _speaks(doctor_id: str, lang: str) -> bool:
    spec = _DOCTORS.get(doctor_id.rsplit("-", 1)[-1])
    return spec is not None and lang in spec[0]


def fitting(
    lines: list[str],
    *,
    now: datetime,
    priority: str | None,
    lang: str,
    gender: str | None = None,
    procedure: str | None = None,
) -> list[dict[str, str]]:
    """Slots a citizen can accept: future, free, in the priority window, spoken language.

    A doctor missing from ``_DOCTORS`` is not filtered on language, gender or days: we do not
    know them, so we neither drop the slot nor claim anything about the doctor.
    """

    if priority == "U":
        horizon: datetime | None = now.replace(hour=23, minute=59, second=59, microsecond=0)
    elif priority == "P":
        horizon = None
    else:
        horizon = now + timedelta(days=_DEADLINE_DAYS.get(priority or "", 14))
    earliest = now + _LEAD
    chosen: list[tuple[datetime, dict[str, str]]] = []
    for line in lines:
        slot = _slot(line)
        slot_id = slot.get("slot_id")
        doctor = slot.get("doctor_id")
        at = protocol.when(slot.get("at", ""))
        if not slot_id or not doctor or at is None:
            continue
        if procedure and slot.get("procedure") not in (None, procedure):
            continue
        free = slot.get("free")
        if free is not None and free.isdigit() and int(free) <= 0:
            continue
        reserve = slot.get("urgent_reserve", "").lower() == "true"
        if reserve and priority != "U":
            continue
        if not earliest < at or (horizon is not None and at >= horizon):
            continue
        spec = _DOCTORS.get(doctor.rsplit("-", 1)[-1])
        if spec is not None:
            if not _speaks(doctor, lang):
                continue
            day = slot.get("day", "")
            if day and day not in spec[2]:
                continue
            if gender and spec[1] != gender:
                continue
        chosen.append((at, slot))
    chosen.sort(key=lambda item: (item[0], item[1].get("slot_id", "")))
    return [slot for _, slot in chosen]
'''



def load(source: str, **names) -> SimpleNamespace:
    space = {"datetime": datetime, "timedelta": timedelta, "protocol": protocol, "fields_of": protocol.fields_of, **names}
    exec(textwrap.dedent(source), space)  # noqa: S102 - our own verbatim copy
    return SimpleNamespace(**space)


LEAD = [HOUR]
B = load(BOOKING, _lead=lambda: LEAD[0])
F = load(FACILITY)
# Every real row, once per slot id, plus rows the fixtures lack: a doctor we do not know, the sdk
# format, a free=0 row.
ROWS = list({protocol.fields_of(r)["slot_id"]: r for b in SLOTS for r in b.splitlines() if "slot_id=" in r}.values()) + [
    f"slot_id=W01QQQ-asst-gorla-x9-20261202-1030 doctor_id=asst-gorla-x9 facility={HOSPITAL} procedure=VIS-FIS "
    "at=2026-12-02T10:30:00.000Z day=wed free=1",
    f"slot_id=W01QQQ-asst-gorla-s1-20261202-1600 doctor_id=asst-gorla-s1 facility={SPECIALIST} procedure=VIS-CARD "
    "agenda_date=2026-12-02T16:00:00.000Z agenda_day=Wednesday capacity=1",
    line("W01QQQ-asst-gorla-c1-20261203-1200", free=0),
]
WORKS = {"h1": "mon tue wed", "h2": "wed thu fri", "h3": "tue thu", "s1": "mon wed fri", "s2": "tue thu",
         "c1": "mon tue wed thu fri"}  # the registry's days, which the calendar no longer reads
NOWS = [at("2026-11-18T06:00:00Z") + timedelta(hours=13 * i) for i in range(45)]  # 18 Nov - 12 Dec
PREFS = [Prefs("it"), Prefs("en", "f"), Prefs("sq", "m", frozenset({"tue", "thu"})), Prefs("fr", None, frozenset({"mon"}))]


@pytest.mark.parametrize("procedure", ["VIS-FIS", "VIS-CARD", "CONS-GIN"])
def test_parity_with_the_booking(procedure):
    lines, cal, compared = ROWS, fed(page(*ROWS)), 0
    for now in NOWS:  # increasing: the calendar may drop rows a day past, never needed again
        for priority, prefs, relaxed, born, lead in itertools.product(
            ["U", "B", "D", "P", None], PREFS, [False, True], [None, now - timedelta(hours=30)], [HOUR, 5.5 * HOUR]
        ):
            LEAD[0] = lead
            old = B.fitting(lines, now=now, priority=priority, lang=prefs.lang, gender=prefs.gender,
                            procedure=procedure, days=prefs.days, relaxed=relaxed, born=born)
            new = cal.candidates(procedure=procedure, priority=priority, now=now, margin=lead, case="c",
                                 born=born, prefs=prefs, relaxed=relaxed)
            assert new.slots == ([] if priority == "P" else old), (now, priority, prefs, relaxed, born, lead)
            compared += len(old) * (priority != "P")
    assert compared > 1000 and cal.errors == 0


@pytest.mark.parametrize("procedure", ["VIS-FIS", "VIS-CARD", "CONS-GIN"])
def test_parity_with_the_facilities(procedure):
    """F filters on language and gender; the calendar only orders by them (prefs): the caller
    keeps the filter, on Pick.slots. F sorts by time; the calendar puts S_pref first."""

    def rigid(slot: dict[str, str], prefs: Prefs) -> bool:  # F:502-510 on a slot the calendar kept
        spec = calendar.DOCTORS.get(slot["doctor_id"].rsplit("-", 1)[-1])
        return spec is None or (calendar.speaks(slot["doctor_id"], prefs.lang) and (not prefs.gender or spec[1] == prefs.gender))

    cal, compared = fed(page(*ROWS)), 0
    for now in NOWS:
        for priority, prefs in itertools.product(["U", "B", "D", "P", None], PREFS):
            old = F.fitting(ROWS, now=now, priority=priority, lang=prefs.lang, gender=prefs.gender, procedure=procedure)
            new = cal.candidates(procedure=procedure, priority=priority, now=now, margin=4 * HOUR, case="c",
                                 prefs=Prefs(prefs.lang, prefs.gender))
            kept = sorted((s for s in new.slots if rigid(s, prefs)), key=lambda s: (s["at"], s["slot_id"]))
            assert kept == ([] if priority == "P" else old), (now, priority, prefs)
            compared += len(old) * (priority != "P")
    assert compared > 300 and cal.errors == 0


def test_declared_differences():
    LEAD[0] = HOUR
    now = at("2026-11-30T07:00:00Z")
    real = list(dict.fromkeys(r for r in SLOTS[1].splitlines()[1:]))
    cal = fed(SLOTS[1])
    # 1. D3: the booking offered the 09:00 reserve to B, D and P while it read P3 off (B:731,
    #    B:1501, B:1802 reserve=False); the calendar never does (R:718), like F:496 and K:376.
    open_reserve = B.fitting(real, now=now, priority="B", lang="it", procedure="VIS-FIS", reserve=False)
    assert any(s.get("urgent_reserve") for s in open_reserve)
    assert {s for s, why in pick(cal, now=now).why.items() if why == "reserve"} == {
        protocol.fields_of(r)["slot_id"] for r in real if "urgent_reserve" in r}
    # 2. A row without procedure= passed any procedure in B (B:1797) and F (F:496); now `procedure`.
    bare = f"slot_id=BARE-1 doctor_id=asst-gorla-h1 facility={HOSPITAL} at=2026-11-30T12:00:00.000Z day=mon free=1"
    assert [s["slot_id"] for s in B.fitting([bare], now=now, priority="B", lang="it", procedure="VIS-CARD")] == ["BARE-1"]
    assert [s["slot_id"] for s in F.fitting([bare], now=now, priority="B", lang="it", procedure="VIS-CARD")] == ["BARE-1"]
    assert pick(fed(page(bare)), procedure="VIS-CARD", now=now).why == {"BARE-1": "procedure"}
    # 3. A row without facility= passed F (F:492); B dropped it (B:1795), the calendar too: it
    #    cannot be offered (protocol.offer_line needs facility=).
    nowhere = f"slot_id=NOWHERE-1 doctor_id=asst-gorla-h1 procedure=VIS-FIS at=2026-11-30T12:00:00.000Z day=mon free=1"
    assert [s["slot_id"] for s in F.fitting([nowhere], now=now, priority="B", lang="it")] == ["NOWHERE-1"]
    assert B.fitting([nowhere], now=now, priority="B", lang="it") == []
    assert pick(fed(page(nowhere)), now=now).why == {"NOWHERE-1": "unreadable"}
    # 4. F dropped a doctor who does not speak the language or is not the gender (F:503-510); the
    #    calendar keeps them after S_pref, like B (B:1811): the facility filters Pick.slots itself.
    english = Prefs("en", "m")  # no doctor on this page is both: h1 is f, h2/h3 speak no en
    assert F.fitting(real, now=now, priority="D", lang="en", gender="m", procedure="VIS-FIS") == []
    assert ids(pick(cal, now=now, priority="D", prefs=english)) == [
        s["slot_id"] for s in B.fitting(real, now=now, priority="D", lang="en", gender="m", procedure="VIS-FIS")]
    assert ids(pick(cal, now=now, priority="D", prefs=english))
    # 5. F sorted by time only (F:511); the calendar puts S_pref first, like B (B:1813).
    albanian = ids(pick(fed(SLOTS[0]), priority="U", prefs=Prefs("sq")))
    assert albanian[0] == H3 + "0900" and albanian != sorted(albanian, key=lambda s: s[-4:])
    # 6. F counted the deadline from now (F:477-481); the calendar from `born` when given, like B
    #    (B:1786). The facilities pass no born: same as before (parity above).
    born = at("2026-11-28T00:00:00Z")
    assert len(ids(pick(cal, now=now, priority="B", born=born))) < len(F.fitting(real, now=now, priority="B", lang="it"))
    # 7. B and F dropped a doctor on a day off the registry (B:1836, F:506); the calendar keeps it:
    #    on the fixtures that filter dropped 0 real rows of 75, the Ledger's row is the truth.
    off = line("W01QQQ-asst-gorla-h1-20261203-1030")  # h1 works Monday to Wednesday: Thursday
    assert B.fitting([off], now=now, priority="D", lang="it") == F.fitting([off], now=now, priority="D", lang="it") == []
    assert ids(pick(fed(page(off)), now=now, priority="D")) == ["W01QQQ-asst-gorla-h1-20261203-1030"]
    assert all(s["day"] in WORKS[s["doctor_id"][-2:]].split() for s in map(calendar.row, ROWS) if s["doctor_id"][-2:] != "x9")
    # 8. Priority P: B and F returned slots (B:1778, F:646); the calendar none, the offer is the
    #    waiting list (R:91, B:656 already did so before fitting).
    assert B.fitting(real, now=now, priority="P", lang="it", procedure="VIS-FIS")
    assert set(pick(cal, now=now, priority="P").why.values()) == {"waiting_list"}
    # 9. Another ASST's h3 is not ours: B said it speaks sq (B:1712, last segment only); the
    #    calendar knows nothing about another ASST's doctors.
    assert B._speaks("asst-giambellino-h3", "sq") and not calendar.speaks("asst-giambellino-h3", "sq")


# --- b. properties against a Ledger oracle ------------------------------------------------------

REAL0 = at("2026-11-21T00:00:00Z")
LIVE = clock.anchor({"epoch_real": stamp(REAL0), "epoch_agenda": "2026-11-23T07:00:00Z", "rate": "60", "paused": "false"})
STAFF = {"VIS-FIS": (HOSPITAL, ("h1", "h2", "h3")), "CONS-GIN": (CONSULTORIO, ("c1",))}
NEVER = (datetime.min.replace(tzinfo=UTC), "")


def world() -> dict[str, dict[str, str]]:
    """The ASST's catalogue for two agenda weeks: every doctor-day, 09:00 the reserve."""

    rows: dict[str, dict[str, str]] = {}
    for offset in range(10):
        day = datetime(2026, 11, 23, tzinfo=UTC) + timedelta(days=offset)
        name = f"{day:%a}".lower()
        for procedure, (facility, doctors) in STAFF.items():
            for doctor in doctors:
                if name not in WORKS[doctor]:
                    continue
                for hhmm in ("0900", "1030", "1200", "1430", "1600"):
                    when = day.replace(hour=int(hhmm[:2]), minute=int(hhmm[2:]))
                    sid = f"W01QQQ-asst-gorla-{doctor}-{when:%Y%m%d}-{hhmm}"
                    rows[sid] = {"slot_id": sid, "doctor_id": f"asst-gorla-{doctor}", "facility": facility,
                                 "procedure": procedure, "at": f"{when:%Y-%m-%dT%H:%M:%S}.000Z", "day": name}
                    if hhmm == "0900":
                        rows[sid]["urgent_reserve"] = "true"
    return rows


WORLD = world()


class Oracle:
    """The live Ledger as measured (calendario.md §0, §6b). A request is processed after a
    lognormal delay (median 270 s, p90 540 s); its answer is stamped then and reaches us 0-30 s
    later, so answers overtake each other; 5% are lost, 3% delivered twice. At agenda time T of
    processing: a hold on a slot that started (at <= T) is EXPIRED; on a slot held or booked, or
    in the reserve for a non-U with P3 on, NO_CAPACITY; else held for HOLD s, after which the
    commit is STALE_FENCE. world gives free=0 for slots held or booked."""

    HOLD = 600.0

    def __init__(self, rng: random.Random, p3: bool) -> None:
        self.rng, self.p3 = rng, p3
        self.holds: dict[str, tuple[str, float]] = {}  # hold_id -> slot, until
        self.booked: set[str] = set()
        self.todo: list = []
        self.inbox: list = []
        self.seq = self.made = 0

    def ask(self, t: float, op: str, case: str, **args) -> None:
        self.seq += 1
        heapq.heappush(self.todo, (t + self.rng.lognormvariate(math.log(270), 0.541), self.seq, op, case, args))

    def taken(self, sid: str, t: float) -> bool:
        return sid in self.booked or any(s == sid and until > t for s, until in self.holds.values())

    def run(self, t: float) -> None:
        while self.todo and self.todo[0][0] <= t:
            done, seq, op, case, args = heapq.heappop(self.todo)
            text, truth = getattr(self, op)(done, case, **args)
            if not text or self.rng.random() < 0.05:
                continue  # someone else's answer, or lost
            reply = Message(type="chat", sender=LEDGER, recipient=ME, msg_id=f"L{seq}", need_id=case,
                            body={"text": text}, sent_at=stamp(REAL0 + timedelta(seconds=done)))
            for _ in range(2 if self.rng.random() < 0.03 else 1):
                self.seq += 1
                heapq.heappush(self.inbox, (done + self.rng.uniform(0, 30), self.seq, reply, truth))

    def due(self, t: float) -> list:
        out = []
        while self.inbox and self.inbox[0][0] <= t:
            out.append(heapq.heappop(self.inbox)[2:])
        return out

    def world(self, done: float, case: str, procedure: str) -> tuple[str, tuple]:
        rows = [r for r in WORLD.values() if r["procedure"] == procedure]
        text = "\n".join([f"slots page=1/1 matching={len(rows)} asst=asst-gorla wave_id=W01QQQ"] + [
            " ".join(f"{k}={v}" for k, v in r.items()) + f" free={0 if self.taken(r['slot_id'], done) else 1}" for r in rows
        ])
        return text, ("page", "")

    def hold(self, done: float, case: str, sid: str, priority: str) -> tuple[str, tuple]:
        if at(WORLD[sid]["at"]) <= clock.now(LIVE, REAL0 + timedelta(seconds=done)):
            return "refused hold code=EXPIRED detail=slot", ("ended", sid)
        if self.taken(sid, done) or (self.p3 and "urgent_reserve" in WORLD[sid] and priority != "U"):
            return f"refused hold code=NO_CAPACITY detail={sid}", ("gone", sid)
        self.made += 1
        hold_id = f"bk-{self.made:05d}"
        self.holds[hold_id] = (sid, done + self.HOLD)
        return f"held hold_id={hold_id} slot_id={sid} need_id={case} fence=1", ("held", sid)

    def take(self, done: float, case: str, sid: str) -> tuple[str, tuple]:
        """Another team books ``sid`` behind our back: we never see the answer."""

        if not self.taken(sid, done):
            self.booked.add(sid)
        return "", ("", "")

    def commit(self, done: float, case: str, hold_id: str) -> tuple[str, tuple]:
        sid, until = self.holds.pop(hold_id, ("", 0.0))
        if until <= done:
            return "refused commit code=STALE_FENCE detail=expired", ("ended", sid)
        self.booked.add(sid)
        return f"committed booking_id={hold_id} slot_id={sid} agenda_date={WORLD[sid]['at']}", ("booked", sid)


class Run:
    """One pod (booking and facility at once) against the oracle: citizens arrive, are offered up
    to 3 slots, confirm one; the hold goes through check() and claim(); held is committed. Every
    candidates() is checked against what the run learned, kept apart from the calendar's own state
    (the truth: latest world row, latest NO_CAPACITY/BOOKED)."""

    def __init__(self, seed: int, steps: int = 150) -> None:
        self.seed, self.steps, self.rng = seed, steps, random.Random(seed)
        self.t = 0.0
        self.cal = Calendar(wall=self.wall)
        self.oracle = Oracle(self.rng, p3=self.rng.random() < 0.5)
        self.margin = self.rng.choice([HOUR, 4 * HOUR, clock.margin(HOUR, 270)])
        self.cases: dict[str, dict] = {}
        self.events: list[tuple[Message, str]] = []
        self.rows: dict[str, tuple] = {}  # truth: slot -> (version, tokens) of the latest world row
        self.gone: dict[str, tuple] = {}  # truth: slot -> version of the latest NO_CAPACITY / committed
        self.reacted: set[str] = set()
        self.asked: dict[str, float] = {}
        self.n = 0
        self.checks = 0

    def wall(self) -> datetime:
        return REAL0 + timedelta(seconds=self.t)

    def now(self) -> datetime:
        return clock.now(LIVE, self.wall())

    def fail(self, what: str) -> None:
        pytest.fail(f"seed {self.seed}, t={self.t:.0f}s: {what}")

    def learn(self, m: Message, case: str, truth: tuple = ("", "")) -> None:
        self.events.append((m, case))
        self.cal.learn(m, me=ME, case=case)
        version, (kind, sid) = (at(m.sent_at), m.msg_id), truth
        if kind == "page":
            for text in m.text.splitlines()[1:]:
                tokens = protocol.fields_of(text)
                if version >= self.rows.get(tokens["slot_id"], (NEVER,))[0]:
                    self.rows[tokens["slot_id"]] = (version, tokens)
        elif kind in ("gone", "booked"):
            self.gone[sid] = max(self.gone.get(sid, NEVER), version)

    def say(self, to: str, text: str, case: dict) -> Message:
        self.n += 1
        m = Message(type="chat", sender=ME, recipient=to, msg_id=f"M{self.n}", need_id=case["id"],
                    body={"text": text}, sent_at=stamp(self.wall()))
        self.learn(m, case["id"])
        return m

    def verify(self, found: Pick, case: dict, now: datetime, relaxed: bool) -> None:
        self.checks += 1
        wall, prio, born = self.wall(), case["priority"], case["born"]
        horizon = None if relaxed else {"U": born.replace(hour=23, minute=59, second=59, microsecond=0),
                                         "B": born + timedelta(days=2), "D": born + timedelta(days=5)}[prio]
        classes = []
        for slot in found.slots:
            sid = slot["slot_id"]
            version, tokens = self.rows.get(sid, (NEVER, None))
            when = at(slot["at"])
            if not when > now + self.margin:
                self.fail(f"I1 {sid} at {when} <= {now} + margin")
            if prio != "U" and slot.get("urgent_reserve") == "true":
                self.fail(f"I2 reserve {sid} to {prio}")
            if slot["procedure"] != case["procedure"] or tokens is None or tokens["free"] == "0":
                self.fail(f"I3 {sid}: {tokens}")
            if self.gone.get(sid, NEVER) >= version:
                self.fail(f"I4 {sid}: gone at {self.gone[sid]}, page at {version}")
            if slot != tokens:
                self.fail(f"I6 {sid}: {slot} != {tokens}")
            if horizon is not None and not when < horizon:
                self.fail(f"I7 {sid} at {when} past {horizon}")
            if sid in case["offered"]:
                self.fail(f"skip {sid}")
            who, what = self.cal._live(sid, wall)
            classes.append((not calendar.preferred(slot, case["prefs"]), what == "offer" and who != case["id"]))
        if classes != sorted(classes):
            self.fail(f"order: a slot offered to another case before a free one {classes}")
        if self.cal.errors:
            self.fail(f"errors {self.cal.errors}")

    def offer(self, case: dict) -> None:
        now = self.now()
        for relaxed in (False, True):
            found = self.cal.candidates(procedure=case["procedure"], priority=case["priority"], now=now,
                                        margin=self.margin, case=case["id"], born=case["born"], prefs=case["prefs"],
                                        relaxed=relaxed, skip=frozenset(case["offered"]))
            self.verify(found, case, now, relaxed)
            if found.slots:
                break
        if self.t - self.asked.get(case["procedure"], -1e9) > 300:
            self.asked[case["procedure"]] = self.t
            self.say(LEDGER, f"world procedure={case['procedure']} page=1", case)
            self.oracle.ask(self.t, "world", case["id"], procedure=case["procedure"])
        chosen = found.slots[:3]
        for slot in chosen:
            if not self.cal.claim(slot["slot_id"], case["id"]):
                self.fail(f"claim of a fresh candidate {slot['slot_id']} failed")
        case["tries"] += 1
        if chosen:
            self.say(CITIZEN, " ".join(protocol.offer_line(s, None, case["priority"]) for s in chosen), case)
            case["offers"] = [s["slot_id"] for s in chosen]
            case["offered"] |= set(case["offers"])
            case["state"] = "offered"

    def confirm(self, case: dict) -> None:
        sid = self.rng.choice(case["offers"])
        if self.cal.check(sid, now=self.now(), margin=self.margin, case=case["id"]) or not self.cal.claim(sid, case["id"], "hold"):
            self.cal.release(sid, case["id"])
            case["state"] = "retry"
            return
        self.say(LEDGER, f"hold slot_id={sid} need_id={case['id']}", case)
        self.oracle.ask(self.t, "hold", case["id"], sid=sid, priority=case["priority"])
        case["state"] = "holding"

    def hostile(self) -> None:
        sid = self.rng.choice(list(WORLD))
        text = self.rng.choice([
            f"slot_id={sid} free=1 doctor_id=asst-gorla-evil",
            page(f"slot_id=EVIL-{self.n} doctor_id=asst-gorla-h1 facility={HOSPITAL} procedure=VIS-FIS "
                 f"at={stamp(self.now() + 6 * HOUR)} day=mon free=1", wave="W09EVIL"),
            f"confirm offer_id=of-{sid}", f"cancel offer_id=of-{sid} reason=NO_CAPACITY",
            f"held hold_id=x slot_id={sid} need_id=N-EVIL", f"committed booking_id=x slot_id={sid}",
        ])
        self.n += 1
        sender = self.rng.choice([CITIZEN, PARTNER])
        self.learn(Message(type="chat", sender=sender, recipient=ME, msg_id=f"X{self.n}", need_id="N-EVIL",
                           body={"text": text}, sent_at=stamp(self.wall())), "N-EVIL")

    def step(self) -> None:
        self.t += self.rng.uniform(1, 45)
        self.oracle.run(self.t)
        for reply, truth in self.oracle.due(self.t):
            self.learn(reply, reply.need_id, truth)
            if reply.msg_id in self.reacted:
                continue
            self.reacted.add(reply.msg_id)
            case, (kind, _) = self.cases.get(reply.need_id), truth
            if case is None or kind == "page":
                continue
            if kind == "held":
                hold_id = protocol.fields_of(reply.text)["hold_id"]
                self.say(LEDGER, f"commit hold_id={hold_id} consent_ref=<ref>", case)
                self.oracle.ask(self.t, "commit", case["id"], hold_id=hold_id)
            else:
                case["state"] = "done" if kind == "booked" else "retry"
        roll = self.rng.random()
        if roll < 0.3 or not self.cases:
            self.n += 1
            case = {"id": f"N-{self.seed}-{self.n:04d}", "priority": self.rng.choice("UBBDD"),
                    "procedure": self.rng.choice(list(STAFF)), "born": self.now(), "offered": set(), "offers": [],
                    "state": "new", "tries": 0, "prefs": Prefs(self.rng.choice(["it", "en", "sq"]),
                                                               self.rng.choice([None, "f", "m"]),
                                                               frozenset(self.rng.sample(["mon", "tue", "wed", "thu", "fri"], self.rng.randint(0, 2))))}
            self.cases[case["id"]] = case
        elif roll < 0.6:
            offered = [c for c in self.cases.values() if c["state"] == "offered"]
            if offered:
                self.confirm(self.rng.choice(offered))
        elif roll < 0.67:
            self.hostile()
        elif roll < 0.72 and self.events:
            m, case = self.rng.choice(self.events)
            self.cal.learn(m, me=ME, case=case)  # a redelivery
        elif roll < 0.85:
            self.oracle.ask(self.t, "take", "N-OUTSIDE", sid=self.rng.choice(list(WORLD)))
        waiting = [c for c in self.cases.values() if c["state"] in ("new", "retry") and c["tries"] < 8]
        for case in self.rng.sample(waiting, min(3, len(waiting))):
            self.offer(case)

    def go(self) -> "Run":
        for _ in range(self.steps):
            self.step()
        return self


def replay(run: Run, events: list[tuple[Message, str]]) -> Calendar:
    """A fresh calendar that learns ``events`` in this order."""

    cal = Calendar(wall=run.wall)
    for m, case in events:
        cal.learn(m, me=ME, case=case)
    return cal


def test_properties_against_the_ledger_oracle():
    started, totals = time.perf_counter(), {"checks": 0, "events": 0, "booked": 0}
    for seed in range(200):
        run = Run(seed).go()
        totals["checks"] += run.checks
        totals["events"] += len(run.events)
        totals["booked"] += len(run.oracle.booked)
        if seed % 2 == 0:  # I8: the same events in any order, some twice: the same Ledger state
            shuffled = run.events + run.rng.sample(run.events, len(run.events) // 5)
            run.rng.shuffle(shuffled)
            if ledger(replay(run, shuffled)) != ledger(replay(run, run.events)):
                run.fail("I8 the order of the events changed the state")
        if run.cal.errors:
            run.fail("errors")
        for m, _ in run.events:
            if m.sender == LEDGER:
                head = protocol.first_word(m.text)
                key = protocol.fields_of(m.text).get("code", head) if head == "refused" else head
                totals[key] = totals.get(key, 0) + 1
            elif m.sender != ME:
                totals["hostile"] = totals.get("hostile", 0) + 1
    elapsed = time.perf_counter() - started
    print(f"\nproperties: 200 seeds, {elapsed:.1f}s: " + ", ".join(f"{k} {v}" for k, v in totals.items()))
    assert totals["checks"] > 10_000 and totals["booked"] > 100
    assert all(totals.get(code, 0) > 50 for code in ("NO_CAPACITY", "EXPIRED", "STALE_FENCE", "held", "committed", "hostile"))


# --- c. concurrency ------------------------------------------------------------------------------

C1_ROWS = [line(f"W01QQQ-asst-gorla-h1-20261201-{h:02d}{m:02d}") for h in range(10, 18) for m in (0, 30)][:20]


def c1(seed: int, gap: bool) -> tuple[int, int]:
    """50 cases in one event loop, random pauses: candidates() then claim(hold). With ``gap``
    an await sits between them (the control). Returns (double holds seen, lost claims)."""

    rng, cal = random.Random(seed), fed(page(*C1_ROWS))
    seen, holding = {"doubles": 0, "lost": 0}, {}  # holding: slot -> the case whose hold claim stands

    async def pause() -> None:
        for _ in range(rng.randint(0, 3)):
            await asyncio.sleep(0)

    async def citizen(case: str) -> None:
        for _ in range(3):
            await pause()
            found = cal.candidates(procedure="VIS-FIS", priority="U", now=DEC1_07, margin=HOUR, case=case)
            if not found.slots:
                continue
            sid = found.slots[0]["slot_id"]
            if gap:
                await asyncio.sleep(0)
            if not cal.claim(sid, case, "hold"):
                seen["lost"] += 1
                continue
            seen["doubles"] += holding.get(sid, case) != case
            holding[sid] = case
            await pause()
            if rng.random() < 0.3:
                cal.release(sid, case)
                holding.pop(sid, None)

    async def main() -> None:
        await asyncio.gather(*(citizen(f"N-{i:02d}") for i in range(50)))

    asyncio.run(main())
    return seen["doubles"], seen["lost"]


def c1_check(gap: bool) -> None:
    results = [c1(seed, gap) for seed in range(100)]
    assert sum(d for d, _ in results) == 0, "two holds granted on one slot"
    assert sum(lost for _, lost in results) == 0, "a claim lost the slot candidates() had just given"


def test_c1_one_event_loop_never_double_holds():
    c1_check(gap=False)


def test_c1_control_an_await_between_candidates_and_claim_is_seen():
    with pytest.raises(AssertionError, match="lost the slot"):  # the test sees the race when it exists
        c1_check(gap=True)


def c4_case(events: list[Message], case: str) -> list:
    results = []
    for order in itertools.permutations(events):
        cal = Calendar(wall=Wall("2026-11-21T04:10:00Z"))
        for m in order:
            cal.learn(m, me=ME, case=N1)
        found = pick(cal, priority="U", case=case)
        results.append((ids(found), found.why, dump(cal)))
    return results


def at_(minute: int, second: int = 0) -> str:
    return f"2026-11-21T04:{minute:02d}:{second:02d}Z"


X = H1 + "1030"
# A refusal that names no slot and overtakes its own held is not here: claims are learned in
# arrival order (no versions), so that hold lapses after 1200 s instead.
C4_RACES = {
    # A. a free=1 page against a refusal: the latest sent_at wins (I4), whatever arrives first.
    "page_vs_refusal_gone": ([
        msg(LEDGER, page(line(X)), "p1", sent=at_(0)),
        msg(LEDGER, f"refused hold code=NO_CAPACITY detail={X}", "r1", sent=at_(1)),
        msg(LEDGER, page(line(X)), "p2", sent=at_(2)),
        msg(LEDGER, f"refused hold code=NO_CAPACITY detail={X}", "r2", sent=at_(3)),
        msg(LEDGER, page(line(X, free=0)), "p0", sent=at_(0, 30)),
    ], "gone:NO_CAPACITY"),
    "page_vs_refusal_free": ([
        msg(LEDGER, page(line(X)), "p1", sent=at_(0)),
        msg(LEDGER, f"refused hold code=NO_CAPACITY detail={X}", "r1", sent=at_(1)),
        msg(LEDGER, f"committed booking_id=b slot_id={X}", "c1", sent=at_(2)),
        msg(LEDGER, page(line(X, free=0)), "p2", sent=at_(3)),
        msg(LEDGER, page(line(X)), "p3", sent=at_(4)),
    ], ""),
    # B. an old hold's refusal against a new hold of the same case: the new one stands.
    "old_refusal_new_hold": ([
        msg(LEDGER, page(line(X)), "p", sent=at_(0)),
        msg(ME, f"hold slot_id={X}", "h1", to=LEDGER, need=N1, sent=at_(1)),
        msg(LEDGER, "refused commit code=STALE_FENCE detail=expired", "r", need=N1, sent=at_(2)),
        msg(ME, f"hold slot_id={X}", "h2", to=LEDGER, need=N1, sent=at_(3)),
        msg(LEDGER, f"held hold_id=b2 slot_id={X} need_id={N1} fence=1", "held2", sent=at_(4)),
    ], f"held:{N1}"),
}


@pytest.mark.parametrize("race", list(C4_RACES))
def test_c4_every_permutation_gives_the_same_state(race):
    events, expected = C4_RACES[race]
    results = c4_case(events, N2)
    assert len(results) == 120
    assert all(result == results[0] for result in results)
    found, why, _ = results[0]
    assert why.get(X, "") == expected and (X in found) == (expected == "")
