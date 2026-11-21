"""Audit della review del giudice (tono, proattività): dal live, il Ledger conferma un check-in in
~34 s e lo specialista taceva; il messaggio di check-in era "una conferma standard". E una
ricevuta doppia al centro prenotazioni nello stesso secondo."""

import asyncio

from conftest import AGENT, CITIZEN, LEDGER, FakeCity, msg, run, turn
from test_level4 import WORLD, _ledger

B = "H-2"
SLOT = "S-c1"
OUR_BOOKING = "agent:sbdpbpqz5sxe"


def _booked(first: str = "Buongiorno, ho la visita cardiologica.") -> list:
    return [
        msg(CITIZEN, first),
        msg(AGENT, f"hold slot_id={SLOT} citizen={CITIZEN}", recipient=LEDGER),
        msg(LEDGER, f"held hold_id={B} slot_id={SLOT} fence=1", recipient=AGENT),
        msg(LEDGER, f"committed booking_id={B} ledger_ref=LR-3 slot_id={SLOT}", recipient=AGENT),
        msg(LEDGER, "\n".join(["slots page=1/1 count=1",
                               f"slot_id={SLOT} doctor_id=D-s1 facility=F-card procedure=VIS-CARD "
                               "at=2026-11-19T10:30:00+00:00 day=thu free=0"]), recipient=AGENT),
    ]


def test_slow_ledger_checkin_waits_for_the_ledger_in_silence(brain):
    """Live 21/11 08:36: any reply to the check-in ends the citizen's journey and the judge scores
    it a minute later. A holding reply before checked_in turned checkin_honored +2 into
    receipt_refused -2. The checkin goes to the Ledger; the citizen hears nothing until it answers."""
    fake = FakeCity(_booked(), auto=("report",))  # checked_in does not come back in time
    run(brain, fake, msg(CITIZEN, f"checkin booking_id={B} Buongiorno, sono arrivata."))
    assert [t for to, t in fake.sent if to == CITIZEN] == []
    assert any(to == LEDGER and t.startswith(f"checkin booking_id={B}") for to, t in fake.sent)
    assert fake.reports() == []


def test_checkin_note_names_the_visit_and_its_time(brain):
    fake = FakeCity(_booked())
    run(brain, fake, msg(CITIZEN, f"checkin booking_id={B}"))
    note = fake.to_citizen("checkin")[0]
    assert "cardiologia" in note and "19/11 alle 10:30" in note


def test_worried_citizen_is_acknowledged_without_clinical_advice(brain):
    fake = FakeCity(_booked())
    run(brain, fake, msg(CITIZEN, f"checkin booking_id={B} Sono qui, sono un po' in ansia per il cuore."))
    note = fake.to_citizen("checkin")[0]
    assert "preoccupazione" in note
    assert not any(w in note.lower() for w in ("diagnosi", "farmac", "dose", "mg"))


def test_confirm_and_committed_push_send_one_receipt(brain):
    # Live (000169): the confirm turn and the Ledger's committed push both sent the receipt.
    fake = FakeCity([], on_post=_ledger(WORLD), yields=True)
    confirm = msg(OUR_BOOKING, f"confirm offer_id=of-{SLOT} slot_id={SLOT} citizen={CITIZEN} "
                  "consent_ref=cons-N-X-booking prescription_number=prescription_number-ABCD1234",
                  need_id="N-X")

    async def push_when_committed() -> None:
        # The push opens its own turn as soon as the Ledger writes committed.
        while True:
            row = next((r for r in fake.chat if r.sender == LEDGER and r.text.startswith("committed")), None)
            if row is not None:
                break
            await asyncio.sleep(0)
        await turn(brain, fake, row)

    async def both() -> None:
        await asyncio.gather(turn(brain, fake, confirm), push_when_committed())

    asyncio.run(both())
    receipts = [t for to, t in fake.sent if to == OUR_BOOKING and t.startswith("booking.receipt")]
    assert len(receipts) == 1
