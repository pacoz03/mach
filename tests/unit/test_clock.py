"""shared/mach/clock.py: formula on the live anchor, weekend skip, margin, paused, bad input, the
city:clock fact, the cache, and parity with the two clocks it replaces (copied below).

uv run --no-project --with city-sdk==0.9.202 --with pytest python -m pytest tests/unit -q
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))  # the package shared/mach
from mach import clock  # noqa: E402

UTC = timezone.utc
# A real Ledger agenda reply (21/11, wave W01QQQ): clock fields only, nothing personal.
LIVE = (
    "agenda rate=60.0 paused=false wave_id=W01QQQ agenda_day=tue agenda_now=2026-12-01T11:02:52.971Z "
    "epoch_real=2026-11-20T19:15:09.977Z epoch_agenda=2026-11-02T00:00:00.000Z"
)
# The city:clock value on Supabase (facts, written by the hospital 21/11 02:38 UTC).
LIVE_FACT = {
    "rate": "60.0",
    "paused": "false",
    "epoch_real": "2026-11-20T19:15:09.977Z",
    "epoch_agenda": "2026-11-02T00:00:00.000Z",
}


def fields(text: str) -> dict[str, str]:
    return dict(token.split("=", 1) for token in text.split() if "=" in token)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def make(epoch_agenda: str, **extra: str) -> clock.Anchor:
    found = clock.anchor({"epoch_real": "2026-11-20T19:15:00Z", "epoch_agenda": epoch_agenda,
                          "rate": "60", "paused": "false", **extra})
    assert found is not None
    return found


@pytest.fixture(autouse=True)
def empty_cache(monkeypatch):
    monkeypatch.setattr(clock, "_CACHE", None)


# --- formula --------------------------------------------------------------------------------


def test_formula_on_the_live_anchor():
    live = clock.anchor(fields(LIVE))
    # 8h39m05.023s real since epoch_real = 21 d 15:05:01.38 of weekdays: Tuesday of the 5th week.
    assert clock.now(live, at("2026-11-21T03:54:15Z")) == at("2026-12-01T15:05:01.380Z")


def test_the_live_reading_is_the_formula_at_its_send_time():
    live = clock.anchor(fields(LIVE))
    # The real instant this reply's agenda_now stands for (21 d 11:02:52.971 / 60 after epoch_real).
    got = clock.now(live, at("2026-11-21T03:50:12.859850Z"))
    assert abs(got - live.agenda_now) < timedelta(milliseconds=1)
    assert got.strftime("%a").lower() == fields(LIVE)["agenda_day"]


@pytest.mark.parametrize("seen, agenda_now", [
    # episodes 21/11, hospital: when we read the reply, and its agenda_now (Relay out of order).
    ("2026-11-21T04:08:01.99269Z", "2026-12-02T00:57:28.958Z"),
    ("2026-11-21T04:08:11.077928Z", "2026-12-02T00:51:07.489Z"),
    ("2026-11-21T04:08:30.339831Z", "2026-12-02T01:21:26.204Z"),
])
def test_live_replies_were_read_after_their_stamp(seen, agenda_now):
    late = clock.now(clock.anchor(LIVE_FACT), at(seen)) - at(agenda_now)
    assert timedelta(0) <= late <= timedelta(hours=10)  # 0-10 real minutes of Relay


# --- weekend skip ---------------------------------------------------------------------------


@pytest.mark.parametrize("real_minutes, expected", [
    (0, "2026-11-02T00:00:00Z"),       # Monday 00:00
    (119.5, "2026-11-06T23:30:00Z"),   # Friday 23:30
    (120, "2026-11-09T00:00:00Z"),     # 5 agenda days later: next Monday, no weekend
    (120.5, "2026-11-09T00:30:00Z"),
    (-1, "2026-10-30T23:00:00Z"),      # before the epoch: the Friday before
])
def test_weekend_skip(real_minutes, expected):
    anchor = make("2026-11-02T00:00:00Z")
    assert clock.now(anchor, anchor.epoch_real + timedelta(minutes=real_minutes)) == at(expected)


def test_an_epoch_on_saturday_counts_from_next_monday():
    anchor = make("2026-11-07T10:00:00Z")
    assert clock.now(anchor, anchor.epoch_real) == at("2026-11-09T00:00:00Z")
    assert clock.now(anchor, anchor.epoch_real + timedelta(minutes=1)) == at("2026-11-09T01:00:00Z")


# --- margin ---------------------------------------------------------------------------------


def test_margin():
    assert clock.margin(timedelta(hours=4)) == timedelta(hours=4)
    # B: 1 h + a 270 s round trip at 60x = 1 h + 4.5 agenda hours.
    assert clock.margin(timedelta(hours=1), 270.0, 60.0) == timedelta(hours=5, minutes=30)


def test_bookable_edges():
    now, ahead = at("2026-12-01T10:00:00Z"), timedelta(hours=1)
    assert not clock.bookable(now + ahead, now, ahead)  # strictly after now + margin
    assert clock.bookable(now + ahead + timedelta(microseconds=1), now, ahead)
    assert not clock.bookable(now, now, ahead)
    assert not clock.bookable(None, now, ahead)
    assert not clock.bookable(now + 2 * ahead, None, ahead)
    assert not clock.bookable(datetime(2026, 12, 2), now, ahead)  # naive slot: no exception


# --- paused and bad input -------------------------------------------------------------------


def test_paused_is_the_frozen_reading():
    paused = clock.anchor({**fields(LIVE), "paused": "true"})
    assert paused.paused
    assert clock.now(paused, at("2026-11-25T00:00:00Z")) == at("2026-12-01T11:02:52.971Z")
    assert clock.now(clock.anchor({**LIVE_FACT, "paused": "TRUE"})) is None  # no reading to freeze
    assert clock.from_fact({**LIVE_FACT, "paused": "true"}) is None


@pytest.mark.parametrize("bad", [
    None, "agenda rate=60", ["rate", "60"], {},
    {**LIVE_FACT, "rate": ""}, {**LIVE_FACT, "rate": "abc"}, {**LIVE_FACT, "rate": "0"},
    {**LIVE_FACT, "rate": "-60"}, {**LIVE_FACT, "rate": "nan"}, {**LIVE_FACT, "rate": "inf"},
    {**LIVE_FACT, "rate": "1e400"}, {**LIVE_FACT, "rate": None},
    {k: v for k, v in LIVE_FACT.items() if k != "epoch_real"},
    {**LIVE_FACT, "epoch_real": "2026-11-20T19:15:09"},  # naive
    {**LIVE_FACT, "epoch_agenda": "yesterday"}, {**LIVE_FACT, "epoch_agenda": 12},
])
def test_malformed_fields_give_none(bad):
    assert clock.anchor(bad) is None
    assert clock.from_fact(bad) is None
    assert clock.now(clock.anchor(bad)) is None


def test_tolerant_formats():
    found = clock.anchor({"epoch_real": "2026-11-20T20:15:09.977+01:00", "epoch_agenda": "2026-11-02T00:00:00Z",
                          "rate": 60, "paused": False, "agenda_date": "2026-12-01T11:02:52.971Z"})
    live = clock.anchor(fields(LIVE))
    assert (found.epoch_real, found.epoch_agenda, found.rate, found.paused) == (
        live.epoch_real, live.epoch_agenda, live.rate, live.paused)
    assert found.agenda_now == live.agenda_now  # agenda_date stands in, as in B:2065 and F:622
    assert not clock.anchor({**LIVE_FACT, "paused": "maybe"}).paused
    assert clock.anchor({**LIVE_FACT, "agenda_now": "soon"}).agenda_now is None


def test_now_never_raises():
    live = clock.anchor(LIVE_FACT)
    assert clock.now(None) is None
    assert clock.now(live, datetime(2026, 11, 21, 3, 54)) is None  # naive real time
    assert clock.now(clock.anchor({**LIVE_FACT, "rate": "1e300"}), at("2026-11-21T03:54:15Z")) is None
    assert clock.now(live) > at("2026-12-01T00:00:00Z")  # default: the real clock


# --- city:clock fact ------------------------------------------------------------------------


def test_fact_round_trip_keeps_the_live_format():
    live = clock.anchor(fields(LIVE))
    assert clock.to_fact(live) == LIVE_FACT  # byte for byte what F:632 noted, old pods read it
    back = clock.from_fact(clock.to_fact(live))
    assert back == live._replace(agenda_now=None)
    assert clock.from_fact({**LIVE_FACT, "agenda_now": "2026-12-01T11:02:52.971Z"}).agenda_now is None
    assert clock.from_fact({"rate": 60.0, "paused": False, "epoch_real": "2026-11-20T19:15:09.977Z",
                            "epoch_agenda": "2026-11-02T00:00:00.000Z"}) is not None
    assert clock.FACT == ("city:clock", "anchor")


# --- cache ----------------------------------------------------------------------------------


def test_cache_keeps_the_newest_anchor():
    reply = lambda agenda_now, **kw: clock.anchor({**LIVE_FACT, "agenda_now": agenda_now, **kw})  # noqa: E731
    fact = clock.from_fact(LIVE_FACT)
    assert clock.keep() is None
    assert clock.keep(None) is None
    assert clock.keep(fact) is fact                      # a cold pod: the fact fills the cache
    first = reply("2026-12-02T00:57:28.958Z")
    assert clock.keep(first) is first                    # a reply replaces the fact
    assert clock.keep(fact) is first                     # the fact never replaces a reply
    assert clock.keep(reply("2026-12-02T00:51:07.489Z")) is first  # older, delivered later
    same = reply("2026-12-02T00:57:28.958Z")
    assert clock.keep(same) is same                      # equal counts as newer (B:2067, F:624)
    latest = reply("2026-12-02T01:21:26.204Z")
    assert clock.keep(latest) is latest
    assert clock.keep() is latest
    rebased = reply("2026-11-02T00:10:00Z", epoch_real="2026-11-21T05:00:00Z",
                    epoch_agenda="2026-11-02T00:00:00Z")
    assert clock.keep(rebased) is rebased                # a later epoch wins with an earlier reading
    assert clock.keep(latest) is rebased


# --- parity with the clocks clock.py replaces ------------------------------------------------
# Copies of the code at 9024ad2, only _CLOCK (a module dict) and the real time made parameters.


def _when(value):  # B:1919, F:383
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def booking_agenda_at(_CLOCK, real):  # B:2072-2093 (_agenda_at, _rate)
    def _rate():
        try:
            return float(_CLOCK.get("rate") or 60.0)
        except ValueError:
            return 60.0

    if _CLOCK.get("epoch_real") and _CLOCK.get("epoch_agenda"):
        base, start = _when(_CLOCK["epoch_agenda"]), _when(_CLOCK["epoch_real"])
    else:
        base, start = _when(_CLOCK.get("agenda_now", "")), _when(_CLOCK.get("sent_at", ""))
    if base is None or start is None or _CLOCK.get("paused") == "true":
        return _when(_CLOCK.get("agenda_now", ""))
    monday = (base - timedelta(days=base.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    week = timedelta(days=5)
    weeks, rest = divmod(min(base - monday, week) + (real - start) * _rate(), week)
    return monday + weeks * timedelta(days=7) + rest


def facility_city_now(_CLOCK, now):  # F:662-676 (_city_now), datetime.now(...) -> now
    real, base = _when(_CLOCK.get("epoch_real", "")), _when(_CLOCK.get("epoch_agenda", ""))
    try:
        rate = float(_CLOCK.get("rate", ""))
    except ValueError:
        return None
    if real is None or base is None or _CLOCK.get("paused") == "true":
        return None
    monday = (base - timedelta(days=base.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    week = timedelta(days=5)
    weeks, rest = divmod(min(base - monday, week) + (now - real) * rate, week)
    return monday + weeks * timedelta(days=7) + rest


ANCHORS = [
    fields(LIVE),
    {**fields(LIVE), "epoch_agenda": "2026-11-04T15:30:00+00:00", "epoch_real": "2026-11-20T19:15:09.977000+00:00"},
    {**fields(LIVE), "epoch_agenda": "2026-11-07T10:00:00Z"},  # Saturday
    {**fields(LIVE), "epoch_agenda": "2026-11-08T23:59:59.999Z", "rate": "30"},  # Sunday, slower
    {**fields(LIVE), "rate": "120.5"},
]
REALS = ["2026-11-20T19:15:09.977Z", "2026-11-20T19:16:00Z", "2026-11-21T03:54:15Z",
         "2026-11-21T07:00:00.123456Z", "2026-11-23T12:00:00Z", "2026-11-20T18:00:00Z"]


@pytest.mark.parametrize("anchor_fields", ANCHORS)
@pytest.mark.parametrize("real", REALS)
def test_parity_with_booking_and_facilities(anchor_fields, real):
    got = clock.now(clock.anchor(anchor_fields), at(real))
    assert got == booking_agenda_at(anchor_fields, at(real))
    assert got == facility_city_now(anchor_fields, at(real))


def test_declared_differences():
    real = at("2026-11-21T03:54:15Z")
    paused = {**fields(LIVE), "paused": "true"}
    assert clock.now(clock.anchor(paused), real) == booking_agenda_at(paused, real)  # B: the reading
    assert facility_city_now(paused, real) is None                                  # F: none
    no_rate = {k: v for k, v in fields(LIVE).items() if k != "rate"}
    assert clock.anchor(no_rate) is None
    assert facility_city_now(no_rate, real) is None                                 # F: none
    assert booking_agenda_at(no_rate, real) is not None                             # B: rate 60
    no_epoch = {"agenda_now": "2026-12-01T11:02:52.971Z", "sent_at": "2026-11-21T03:50:12.860Z"}
    assert clock.anchor(no_epoch) is None                                           # B used sent_at


def test_margin_parity():
    now = at("2026-12-01T10:00:00Z")
    for minutes in range(0, 600, 7):
        slot = now + timedelta(minutes=minutes)
        for rtt in (0.0, 4.5, 270.0, 600.0):
            lead = timedelta(hours=1) + timedelta(seconds=rtt * 60.0)  # B:2117 _lead
            assert clock.bookable(slot, now, clock.margin(timedelta(hours=1), rtt)) == (now + lead < slot)  # B:1817
        assert clock.bookable(slot, now, timedelta(hours=4)) == (now + timedelta(hours=4) < slot)  # F:580
