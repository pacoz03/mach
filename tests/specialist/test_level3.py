"""Livello 3, "Bisogni in linguaggio naturale": il cittadino scrive in prosa, senza token."""

from conftest import AGENT, CITIZEN, LEDGER, FakeCity, msg, run

B = "H-2"


def _booked() -> list:
    return [
        msg(CITIZEN, "Buongiorno, ho la ricetta per la visita cardiologica."),
        msg(AGENT, f"commit hold_id={B} consent_ref=cons-N-1-booking", recipient=LEDGER),
        msg(LEDGER, f"committed booking_id={B} ledger_ref=LR-3 slot_id=S-1", recipient=AGENT),
    ]


def _asked() -> list:
    return _booked() + [
        msg(LEDGER, f"checked_in booking_id={B}", recipient=AGENT),
        msg(AGENT, f"checkin booking_id={B} Grazie.", recipient=CITIZEN),
        msg(AGENT, f"consent purpose=report_sharing booking_id={B} Posso?", recipient=CITIZEN),
    ]


def _ledger_checkins(fake: FakeCity) -> list[str]:
    return [t for to, t in fake.sent if to == LEDGER and t.startswith("checkin")]


# --- check-in in prosa ------------------------------------------------------------------------

def test_prose_arrival_checks_in_our_booking(brain):
    fake = FakeCity(_booked())
    run(brain, fake, msg(CITIZEN, "Buongiorno, sono arrivata per la visita cardiologica."))
    assert _ledger_checkins(fake) == [f"checkin booking_id={B} citizen={CITIZEN}"]
    assert fake.to_citizen("consent")


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


# --- consenso in prosa ------------------------------------------------------------------------

def test_prose_consent_code_shares_the_report(brain):
    fake = FakeCity(_asked())
    run(brain, fake, msg(CITIZEN, "Acconsento, il codice del consenso è cons-N-1-report_sharing."))
    assert fake.reports() == [f"report booking_id={B} outcome_code=VISIT consent_ref=cons-N-1-report_sharing"]


def test_prose_yes_without_code_asks_for_it_once(brain):
    fake = FakeCity(_asked())
    run(brain, fake, msg(CITIZEN, "Sì, va bene, può condividerlo."))
    assert fake.reports() == []
    assert len(fake.to_citizen("consent")) == 1
    run(brain, fake, msg(CITIZEN, "Sì sì, certo."))
    assert fake.reports() == [f"report booking_id={B} outcome_code=VISIT"]
    assert len(fake.to_citizen("consent")) == 1


def test_prose_no_files_unshared_report(brain):
    fake = FakeCity(_asked())
    run(brain, fake, msg(CITIZEN, "No, preferisco di no."))
    assert fake.reports() == [f"report booking_id={B} outcome_code=VISIT"]
