"""Centro prenotazioni: i giorni della finestra chiesti al Ledger tutti insieme (world day=), le pagine al
loro posto quando day= è rifiutato, una pagina oltre l'ultima rifiutata (detail=page) che non si aspetta.

uv run --no-project --with city-sdk==0.9.202 --with pytest python -m pytest -q tests/unit/test_booking_days.py
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from city_sdk.city import LEDGER, Message

ROOT = Path(__file__).resolve().parents[2]
US = "agent:sbdpbpqz5sxe"
H = "agent:m6py14a1ej77"


def _load():
    name = "days_booking_centre"
    spec = importlib.util.spec_from_file_location(name, ROOT / "booking_centre" / "agent" / "agent.py")
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _at(day: int, hour: int) -> datetime:
    return datetime(2026, 11, day, hour, tzinfo=timezone.utc)


def _msg(sender: str, to: str, text: str, n: int) -> Message:
    return Message(type="chat", sender=sender, recipient=to, msg_id=f"m-{n}", need_id="n-1", body={"text": text},
                   sent_at=f"2026-11-21T10:00:{n:02d}Z")


def _slots(page: str, *ats: str) -> str:
    rows = [f"slot_id=S-{i} doctor_id=asst-gorla-h1 facility={H} procedure=VIS-FIS at={at} "
            f"day={('mon', 'tue', 'wed', 'thu', 'fri')[date.fromisoformat(at[:10]).weekday()]} free=1"
            for i, at in enumerate(ats)]
    return "\n".join([f"slots page={page} matching={len(ats)}", *rows])


def test_window_days():
    mod = _load()  # no clock, no round trip: _lead() 6 h, _offer_lead() 16 h
    # B on Monday 17:00 (born 16:00): from Tuesday 09:00 to Wednesday 16:00
    assert mod._window_days("B", _at(23, 17), _at(23, 16)) == [date(2026, 11, 24), date(2026, 11, 25)]
    # D born Wednesday 10:00: Thursday, Friday, then Monday before 10:00; no weekend
    assert mod._window_days("D", _at(25, 10), _at(25, 10)) == [date(2026, 11, 26), date(2026, 11, 27), date(2026, 11, 30)]
    # B born Friday 10:00 ends on Sunday: 16 h ahead is past it, 6 h ahead is Friday
    assert mod._window_days("B", _at(27, 10), _at(27, 10)) == [date(2026, 11, 27)]
    assert mod._window_days("U", _at(23, 17), _at(23, 16)) == [date(2026, 11, 23)]
    assert mod._window_days("P", _at(23, 17), _at(23, 16)) == []
    assert mod._window_days("B", None, None) == []
    assert len(mod._window_days(None, _at(23, 9), _at(23, 9))) == mod._DAY_MAX  # unknown priority: 14 days
    assert mod._day_key(date(2026, 11, 24)) == "tue"
    mod._DAY_ISO = True
    assert mod._day_key(date(2026, 11, 24)) == "2026-11-24"


def test_world_refusal():
    mod = _load()
    assert mod._world_refusal("refused world code=SCHEMA_INVALID detail=day") == "day"
    assert mod._world_refusal("refused world code=SCHEMA_INVALID detail=page") == "page"
    assert mod._world_refusal("refused world procedure=VIS-FIS day=wed page=2 code=SCHEMA_INVALID detail=page") == "page"
    assert mod._world_refusal("refused world code=SCHEMA_INVALID detail=unknown filter day") == "day"
    assert mod._world_refusal("refused world code=NOT_FOUND") == "other"
    assert mod._world_refusal("refused hold code=NO_CAPACITY detail=W01QQQ-asst-gorla-h1-20261201-1030") == ""
    assert mod._world_refusal("slots page=1/1 matching=0") == ""


def test_day_reads_next_page_and_empty_answers():
    mod = _load()
    tue, wed, thu = date(2026, 11, 24), date(2026, 11, 25), date(2026, 11, 26)
    rows = [
        _msg(US, LEDGER, "world procedure=VIS-FIS day=tue", 1),
        _msg(US, LEDGER, "world procedure=VIS-FIS day=wed", 2),
        _msg(US, LEDGER, "world procedure=VIS-FIS day=thu", 3),
        # day=tue lists every Tuesday: page 1 holds only an earlier one, so page 2 is needed
        _msg(LEDGER, US, _slots("1/2", "2026-11-17T10:00:00Z", "2026-11-17T15:00:00Z"), 4),
        _msg(LEDGER, US, _slots("1/1", "2026-11-25T10:00:00Z"), 5),
    ]
    asks, unread = mod._day_reads(rows, "VIS-FIS", [tue, wed, thu])
    assert asks == ["world procedure=VIS-FIS day=tue", "world procedure=VIS-FIS day=tue page=2",
                    "world procedure=VIS-FIS day=wed", "world procedure=VIS-FIS day=thu"]
    assert unread == ["world procedure=VIS-FIS day=tue page=2", "world procedure=VIS-FIS day=thu"]
    # An empty answer names no day: it answers one open read, never a wait for it
    rows.append(_msg(LEDGER, US, "slots page=1/1 matching=0", 6))
    assert mod._day_reads(rows, "VIS-FIS", [tue, wed, thu])[1] == ["world procedure=VIS-FIS day=tue page=2"]
    # So does a refused page (detail=page): the day's next page is gone
    rows.append(_msg(LEDGER, US, "refused world code=SCHEMA_INVALID detail=page", 7))
    assert mod._day_reads(rows, "VIS-FIS", [tue, wed, thu])[1] == []


def test_page_reads_refused_page_is_the_end():
    mod = _load()
    end = _at(25, 16)
    rows = [_msg(US, LEDGER, f"world procedure=VIS-FIS page={page}", page) for page in range(1, 6)]
    rows += [_msg(LEDGER, US, _slots(f"{page}/5", f"2026-11-{22 + page}T10:00:00Z"), 10 + page) for page in range(1, 5)]
    asks, unread = mod._page_reads(rows, US, "VIS-FIS", end)
    assert len(asks) == 5 and unread == ["world procedure=VIS-FIS page=5"]
    rows.append(_msg(LEDGER, US, "refused world code=SCHEMA_INVALID detail=page", 20))
    assert mod._page_reads(rows, US, "VIS-FIS", end)[1] == []
    # No page known: _PAGES_UNKNOWN pages, all at once
    assert mod._page_reads([], US, "VIS-FIS", end)[1] == [f"world procedure=VIS-FIS page={p}" for p in range(1, 5)]


def test_world_reads_switch_to_pages_on_refused_day():
    mod = _load()
    now, born = _at(23, 17), _at(23, 16)
    assert mod._world_reads([], US, "VIS-FIS", "B", now, born)[0] == [
        "world procedure=VIS-FIS day=tue", "world procedure=VIS-FIS day=wed"]
    assert mod._world_reads([], US, "VIS-FIS", "B", now, born, relaxed=True)[0] == [
        "world procedure=VIS-FIS day=thu", "world procedure=VIS-FIS day=fri", "world procedure=VIS-FIS day=mon"]
    chat = [_msg(US, LEDGER, "world procedure=VIS-FIS day=tue", 1),
            _msg(LEDGER, US, "refused world code=SCHEMA_INVALID detail=day", 2)]
    mod._learn(chat, US)
    assert mod._NO_DAY
    assert mod._world_reads(chat, US, "VIS-FIS", "B", now, born)[1] == [
        f"world procedure=VIS-FIS page={p}" for p in range(1, 5)]
    assert mod._world_reads(chat, US, "VIS-FIS", "B", now, born, relaxed=True) == ([], [])


def test_day_answer_cached_apart_from_pages():
    mod = _load()
    chat = [_msg(US, LEDGER, "world procedure=VIS-FIS day=wed", 1),
            _msg(LEDGER, US, _slots("1/1", "2026-11-25T10:00:00Z", "2026-11-25T15:00:00Z"), 2)]
    mod._learn(chat, US)
    assert not mod._PAGES and mod._fresh_day("VIS-FIS", "wed", 1) is not None
    # the same page after a page ask is page 1 of the procedure
    chat = [_msg(US, LEDGER, "world procedure=VIS-FIS page=1", 3),
            _msg(LEDGER, US, _slots("1/3", "2026-11-25T10:00:00Z"), 4)]
    mod._learn(chat, US)
    assert mod._fresh_page("VIS-FIS", 1) is not None
