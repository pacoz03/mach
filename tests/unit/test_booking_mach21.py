"""Centro prenotazioni e MACH-2.1 (docs/protocol/mach-2.1.md): reason=taken come slot_taken, l'ordine e l'anello
delle strutture di un giro (R3, R17), il need.request con i soli criteri (R:191).

uv run --no-project --with city-sdk==0.9.202 --with pytest python -m pytest -q tests/unit/test_booking_mach21.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from city_sdk.city import Message

ROOT = Path(__file__).resolve().parents[2]
US, CIT = "agent:sbdpbpqz5sxe", "citizen:T-1"
PARTNER = "agent:h6r1v45esk77"  # ospedale di asst-giambellino
SLOT = "W01QQQ-asst-giambellino-h1-20261124-1500"


def _load():
    name = "mach21_booking_centre"
    spec = importlib.util.spec_from_file_location(name, ROOT / "booking_centre" / "agent" / "agent.py")
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _msg(sender: str, to: str, text: str, mid: str) -> Message:
    return Message(type="chat", sender=sender, recipient=to, msg_id=mid, need_id="n-1", body={"text": text},
                   sent_at="2026-11-21T10:00:00Z")


def _cancel(reason: str):
    mod = _load()
    offer = _msg(US, CIT, f"offer offer_id=of-{SLOT} slot_id={SLOT} doctor_id=asst-giambellino-h1 facility={PARTNER} "
                          "agenda_date=2026-11-24T15:00:00+00:00", "m-1")
    cancel = _msg(PARTNER, US, f"cancel offer_id=of-{SLOT} reason={reason} request_id=x-1", "m-2")
    city = SimpleNamespace(agent_id=US, inbound=cancel)
    asyncio.run(mod._partner_cancel(city, cancel, [offer, cancel]))
    return mod.CAL.check(SLOT, now=None, margin=timedelta(0), case="another-case")


def test_taken_is_slot_taken():
    # Chi è alla 2.1 manda taken: lo slot è perso per ogni caso, come con slot_taken.
    assert _cancel("taken") == "gone:SLOT_TAKEN"
    assert _cancel("slot_taken") == "gone:SLOT_TAKEN"
    assert _cancel("no_availability") == ""


def test_candidates_rare_first_ring_only():
    mod = _load()
    found = mod._candidates("hospital.physiatry", "ro", [])
    assert found[0] == "agent:s1twcdzg3296"  # Precotto (ro), dalla mappa dell'anello
    assert all(mod.in_ring(p) and p not in mod._OUR_FACILITIES for p in found)
    mod._KNOWN_PARTNERS["hospital.physiatry"] = {"agent:zzzzzzzzzzzz": "", **mod._KNOWN_PARTNERS["hospital.physiatry"]}
    assert "agent:zzzzzzzzzzzz" not in mod._candidates("hospital.physiatry", "it", ["agent:zzzzzzzzzzzz"])
    assert mod._candidates("specialist_care.cardiology", "ka", [])[0] != PARTNER  # lingua rara: solo gli ospedali


def test_request_text_criteria_only():
    mod = _load()
    now = datetime(2026, 11, 23, 8, tzinfo=timezone.utc)
    text = mod._request_text("hospital.physiatry", "VIS-FIS", "B", "x-1", now, now - timedelta(hours=1))
    tokens = dict(t.split("=", 1) for t in text.split() if "=" in t)
    assert text.startswith("need.request mach=2.1 ")
    assert tokens["deadline"] == "2026-11-25T06:30"  # nascita + 48 h - 30 min
    assert now + timedelta(hours=4) <= datetime.fromisoformat(tokens["after"]).replace(tzinfo=timezone.utc)
    assert 0 < int(tokens["lease_s"]) <= 1800
    assert not {"lang", "citizen", "patient", "prescription_number"} & set(tokens)
    u = dict(t.split("=", 1) for t in mod._request_text("hospital.physiatry", "VIS-FIS", "U", "x-2", now, now).split() if "=" in t)
    assert u["deadline"] == "2026-11-23T23:29"  # U: il giorno stesso, meno il margine
    assert datetime.fromisoformat(u["after"]) < datetime.fromisoformat(u["deadline"])
