"""The city's agenda clock: one definition for every agent (step 3 of docs/refactor/piano.md).

Agenda time = ``epoch_agenda`` + (real - ``epoch_real``) x ``rate``, weekdays only: after Friday
comes Monday (e19-rules R:80, ADR-124). The anchor comes from a Ledger ``agenda`` reply
(``agenda rate=60.0 paused=false agenda_day=tue agenda_now=… epoch_real=… epoch_agenda=…``) or
from the ``city:clock`` fact in team memory. Pure: stdlib and city_sdk, no network, no
tools.py (the agent does the I/O), never raises. ``None`` means we do not know the time.

Choices where the booking (B, booking_centre/agent/agent.py @9024ad2) and the facilities
(F, shared/facility.py @9024ad2) disagreed:
- Fields: ``epoch_real``, ``epoch_agenda`` and a positive, finite ``rate`` are required. B fell
  back to ``agenda_now`` + the row's ``sent_at`` (B:2080) and to rate 60 (B:2091); F gave None
  (F:666-672). We follow F: no invented rate. Live, all 2761 agenda replies have every field.
- Paused: B returned the reading's ``agenda_now`` (B:2081-2082), F None (F:671). The kit only
  says the clock stops (ledger.py ``AgendaNow.paused``), so the frozen ``agenda_now`` is the
  time: we follow B. An anchor from the fact has no ``agenda_now``, so paused gives None there.
- Which reply: the one with the latest ``agenda_now`` (B:2058 ``_read_clock``), since the Relay
  delivers out of order (episodes 21/11 04:08: 00:51 arrived after 00:57). F kept the last row
  in history order of each read, then the same comparison (F:618-626). New in both: a later
  ``epoch_real`` (new wave, rebase, end of a pause) wins even with an earlier ``agenda_now``;
  before, an agent kept the old epoch until its pod restarted.
- Time zone: city_sdk's ``agenda_time`` turns every stamp into UTC, so Monday 00:00 is UTC
  midnight. B and F used the stamp's own offset; the Ledger always writes ``Z``.
- Margin (how far ahead a slot must start): a parameter, not unified. B passes 1 h + the last
  Ledger round trip x rate (B:47, B:2114); F passes 4 h (F:35).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple

from city_sdk.city import CapError, agenda_time

KEYS = ("epoch_real", "epoch_agenda", "rate", "paused")  # the city:clock value, as the Ledger writes them
FACT = ("city:clock", "anchor")  # (subject, predicate) of the fact in team memory
FACT_TTL = 1800  # seconds an anchor nobody renews stays readable (F:645)
RATE = 60.0  # the kit's agenda rate (R:80): only a default for margin()
_WEEK = timedelta(days=5)


class Anchor(NamedTuple):
    epoch_real: datetime
    epoch_agenda: datetime
    rate: float
    paused: bool
    agenda_now: datetime | None  # the reply's own reading; None from the fact
    raw: dict[str, str]  # KEYS as received: to_fact() writes them back unchanged


def _time(value: Any) -> datetime | None:
    try:
        return agenda_time(str(value).strip())
    except CapError:
        return None


def anchor(fields: Any) -> Anchor | None:
    """The anchor in an ``agenda`` reply's ``key=value`` fields; None if one is missing or bad."""

    if not isinstance(fields, Mapping):
        return None
    try:
        rate = float(fields.get("rate"))
    except (TypeError, ValueError):
        return None
    real, base = _time(fields.get("epoch_real")), _time(fields.get("epoch_agenda"))
    if real is None or base is None or not (math.isfinite(rate) and rate > 0):
        return None
    return Anchor(
        epoch_real=real,
        epoch_agenda=base,
        rate=rate,
        paused=str(fields.get("paused", "")).strip().lower() == "true",
        agenda_now=_time(fields.get("agenda_now") or fields.get("agenda_date")),
        raw={key: str(fields.get(key, "")) for key in KEYS},
    )


def now(anchor: Anchor | None, real_now: datetime | None = None) -> datetime | None:
    """Agenda time at the real instant ``real_now`` (default: now). Paused: the reading itself."""

    if anchor is None:
        return None
    if anchor.paused:
        return anchor.agenda_now
    try:
        real = real_now or datetime.now(timezone.utc)
        base = anchor.epoch_agenda
        monday = (base - timedelta(days=base.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        # A base on Saturday or Sunday counts as Friday's end: min() slides it to next Monday.
        weeks, rest = divmod(min(base - monday, _WEEK) + (real - anchor.epoch_real) * anchor.rate, _WEEK)
        return monday + weeks * timedelta(days=7) + rest
    except (ArithmeticError, TypeError, ValueError):  # naive real_now, an overflowing rate or date
        return None


def margin(base: timedelta, round_trip: float = 0.0, rate: float = RATE) -> timedelta:
    """How far past now a slot must start: ``base``, plus ``round_trip`` real seconds of the
    Ledger turned into agenda time (B: the hold lands that much later)."""

    return base + timedelta(seconds=round_trip * rate)


def bookable(at: datetime | None, now: datetime | None, margin: timedelta) -> bool:
    """A slot at ``at`` is still ahead: strictly after ``now`` + ``margin`` (B:1817, F:580)."""

    try:
        return at is not None and now is not None and at > now + margin
    except (ArithmeticError, TypeError):
        return False


def to_fact(anchor: Anchor) -> dict[str, str]:
    """The ``city:clock`` value: KEYS with the Ledger's own strings, as F:627-632 noted them."""

    return dict(anchor.raw)


def from_fact(value: Any) -> Anchor | None:
    """The anchor in a ``city:clock`` value; None if it is unreadable or paused (F:651-659)."""

    found = anchor({key: value.get(key) for key in KEYS}) if isinstance(value, Mapping) else None
    return None if found is None or found.paused else found


# In-process cache (one per pod): the newest anchor this process has seen, from any case. Size 1,
# no expiry: the anchor is fixed for a wave (one all night on 21/11, 1856 replies) and a newer one
# replaces it. Rebuilt by keep() from the next agenda reply, or on a cold pod from the city:clock
# fact (from_fact). Lost on restart, which is why the facilities note it in team memory.
_CACHE: Anchor | None = None


def keep(found: Anchor | None = None) -> Anchor | None:
    """Cache ``found`` if it is newer, then return the cached anchor (``keep()`` only reads).

    Newer: a later ``epoch_real``, or the same epoch and a later or equal ``agenda_now``. An
    anchor without ``agenda_now`` (the fact) only fills an empty cache; a reply replaces it."""

    global _CACHE
    old = _CACHE
    if found is not None and (old is None or (found.agenda_now is not None and (
        old.agenda_now is None
        or (found.epoch_real, found.agenda_now) >= (old.epoch_real, old.agenda_now)
    ))):
        _CACHE = found
    return _CACHE
