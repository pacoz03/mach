"""Livello 3, "Bisogni in linguaggio naturale": il cittadino scrive in prosa, senza token."""

from conftest import AGENT, CITIZEN, LEDGER, FakeCity, msg, run

B = "H-2"


def _booked() -> list:
    return [
        msg(CITIZEN, "Buongiorno, ho la ricetta per la visita cardiologica."),
        msg(AGENT, f"commit hold_id={B} consent_ref=cons-N-1-booking", recipient=LEDGER),
        msg(LEDGER, f"committed booking_id={B} ledger_ref=LR-3 slot_id=S-1", recipient=AGENT),
    ]


def _ledger_checkins(fake: FakeCity) -> list[str]:
    return [t for to, t in fake.sent if to == LEDGER and t.startswith("checkin")]


# --- check-in in prosa ------------------------------------------------------------------------

def test_prose_arrival_checks_in_our_booking(brain):
    fake = FakeCity(_booked())
    run(brain, fake, msg(CITIZEN, "Buongiorno, sono arrivata per la visita cardiologica."))
    assert _ledger_checkins(fake) == [f"checkin booking_id={B} citizen={CITIZEN}"]
    assert fake.reports() == [f"report booking_id={B} outcome_code=VISIT"]


def test_prose_booking_code_checks_in(brain):
    fake = FakeCity(_booked())
    run(brain, fake, msg(CITIZEN, f"Sono allo sportello, il codice della prenotazione è {B}."))
    assert _ledger_checkins(fake) == [f"checkin booking_id={B} citizen={CITIZEN}"]


def test_prose_reschedule_request_is_not_a_checkin(brain):
    fake = FakeCity(_booked())
    run(brain, fake, msg(CITIZEN, "Posso spostare la visita a giovedì?"))
    assert _ledger_checkins(fake) == []


def test_arrival_without_our_booking_is_not_a_checkin(brain):
    fake = FakeCity([msg(CITIZEN, "Buongiorno.")])
    run(brain, fake, msg(CITIZEN, "Sono arrivata."))
    assert _ledger_checkins(fake) == []
