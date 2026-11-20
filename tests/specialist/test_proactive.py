"""Proattività e collaborazione (rubrica city:review, +1 ciascuna): dopo la prenotazione lo
specialista scrive al cittadino quando e come presentarsi, e avvisa il nostro medico di base."""

from city_sdk.city import Peer

from conftest import AGENT, CITIZEN, LEDGER, FakeCity, msg, run
from test_level4 import WORLD, _ledger

OUR_BOOKING = "agent:sbdpbpqz5sxe"
OUR_GP = "agent:ck9w60ejwnvw"
PARTNER = "agent:partnerbook01"
RX = "prescription_number-ABCD1234"


def _confirm(sender: str):
    return msg(
        sender,
        f"confirm offer_id=of-S-c1 slot_id=S-c1 citizen={CITIZEN} consent_ref=cons-N-X-booking "
        f"prescription_number={RX}",
        need_id="N-X",
    )


def _fake() -> FakeCity:
    fake = FakeCity([], on_post=_ledger(WORLD))
    fake.peers = [Peer(agent_id=OUR_GP, role="gp")]
    return fake


def _welcomes(fake: FakeCity) -> list[str]:
    return [t for to, t in fake.sent if to == CITIZEN and t.startswith("booking.receipt")]


def test_after_booking_the_citizen_hears_when_and_how_to_check_in(brain):
    fake = _fake()
    run(brain, fake, _confirm(OUR_BOOKING))
    welcomes = _welcomes(fake)
    assert len(welcomes) == 1
    text = welcomes[0]
    assert "booking_id=H-9" in text.split()
    assert "agenda_date=2026-11-16T14:00:00+00:00" in text.split()
    assert "cardiologia" in text and "ospedale" not in text


def test_our_booking_notifies_our_gp_once(brain):
    fake = _fake()
    run(brain, fake, _confirm(OUR_BOOKING))
    to_gp = [t for to, t in fake.sent if to == OUR_GP]
    assert len(to_gp) == 1
    assert to_gp[0].startswith("data.response purpose=booking booking_id=H-9")
    assert f"prescription_number={RX}" in to_gp[0].split()


def test_partner_booking_never_reaches_our_gp(brain):
    fake = _fake()
    run(brain, fake, _confirm(PARTNER))
    assert not [t for to, t in fake.sent if to == OUR_GP]


def test_report_message_gives_a_next_step(brain):
    chat = [
        msg(CITIZEN, "Buongiorno, ho la visita cardiologica."),
        msg(LEDGER, "checked_in booking_id=bk-1", recipient=AGENT),
    ]
    fake = FakeCity(chat)
    run(brain, fake, chat[1])
    report = [t for to, t in fake.sent if to == CITIZEN and t.startswith("report")]
    assert len(report) == 1
    assert "centro prenotazioni" in report[0]
    assert "senza il suo consenso" not in report[0]
