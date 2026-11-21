"""Consultorio: la scelta del cittadino tra le offerte. Dal live (000123, 000150): le offerte
partono in più messaggi e il consultorio riconosceva solo quelle dell'ultimo; e chiedeva la
scelta con data.request fields=offer_id, a cui il cittadino risponde data.response vuoto, in loop."""

from conftest import AGENT, CITIZEN, LEDGER, FakeCity, msg, run

NEED = "N-1"


def _offer(slot: str, at: str, day: str) -> str:
    return (f"offer offer_id=of-{slot} slot_id={slot} doctor_id=asst-gorla-c1 facility={AGENT} "
            f"procedure=CONS-GIN agenda_date={at} agenda_day={day} expiry={at}")


A = _offer("S-a", "2026-11-13T10:30:00.000Z", "fri")
B = _offer("S-b", "2026-11-13T12:00:00.000Z", "fri")
C = _offer("S-c", "2026-11-16T14:30:00.000Z", "mon")
D = _offer("S-d", "2026-11-16T16:00:00.000Z", "mon")


def _offered() -> list:
    return [
        msg(CITIZEN, "Buongiorno, vorrei prenotare una visita al consultorio, va bene qualsiasi data."),
        msg(AGENT, "\n".join([A, B]), recipient=CITIZEN),
        msg(AGENT, "\n".join([C, D]), recipient=CITIZEN),
    ]


def _ledger(to: str, text: str, fake: FakeCity) -> None:
    if to != LEDGER:
        return
    fields = dict(t.split("=", 1) for t in text.split() if "=" in t)
    head = text.split()[0]
    if head == "hold":
        fake.chat.append(msg(LEDGER, f"held hold_id=H-1 slot_id={fields.get('slot_id')} fence=1", recipient=AGENT))
    elif head == "commit":
        fake.chat.append(msg(LEDGER, "committed booking_id=H-1 ledger_ref=LR-1 slot_id=S-a", recipient=AGENT))


def _holds(fake: FakeCity) -> list[str]:
    return [t for to, t in fake.sent if to == LEDGER and t.startswith("hold")]


def test_confirm_of_an_offer_from_an_earlier_message_books_it(brain):
    fake = FakeCity(_offered(), on_post=_ledger)
    run(brain, fake, msg(CITIZEN, f"confirm consent_ref=cons-{NEED}-booking offer_id=of-S-a"))
    assert len(_holds(fake)) == 1 and "slot_id=S-a" in _holds(fake)[0].split()
    assert not any("data.request" in t for _, t in fake.sent)


def test_empty_data_response_never_loops(brain):
    fake = FakeCity(_offered(), on_post=_ledger)
    for _ in range(3):
        run(brain, fake, msg(CITIZEN, "data.response purpose=booking"))
    to_citizen = [t for to, t in fake.sent if to == CITIZEN]
    # The choice is made with confirm: never a data.request for offer_id, at most one prompt.
    assert not any(t.startswith("data.request") for t in to_citizen)
    assert len(to_citizen) <= 1
    assert _holds(fake) == []
