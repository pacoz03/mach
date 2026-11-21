"""Livello 4, "Scarsità sul ring": il centro prenotazioni di un'altra ASST chiede i nostri slot di
cardiologia con soli criteri (R:191-192). Una sola risposta, entro la risposta veloce (R:246)."""

from conftest import AGENT, LEDGER, FakeCity, msg, run

PARTNER = "agent:partnerbook01"
NOW = "2026-11-16T08:00:00+00:00"  # lunedì, ora di agenda
WORLD = "\n".join([
    "slots page=1/1 count=3",
    "slot_id=S-c1 doctor_id=D-s2 facility=F-card procedure=VIS-CARD at=2026-11-17T09:00:00+00:00 day=tue free=1",
    "slot_id=S-c2 doctor_id=D-s2 facility=F-card procedure=VIS-CARD at=2026-11-17T10:00:00+00:00 day=tue free=1",
    "slot_id=S-f1 doctor_id=D-h1 facility=F-osp procedure=VIS-FIS at=2026-11-16T15:00:00+00:00 day=mon free=1",
])
EMPTY = "slots page=1/1 count=0"


def _ledger(world: str):
    """Risposte del Ledger finto: agenda, world, hold, commit."""

    def reply(to: str, text: str, fake: FakeCity) -> None:
        if to != LEDGER:
            return
        head = text.split()[0]
        fields = dict(t.split("=", 1) for t in text.split() if "=" in t)
        answer = {
            "agenda": f"agenda agenda_now={NOW} agenda_day=mon",
            "world": world,
            "hold": f"held hold_id=H-9 slot_id={fields.get('slot_id', '')} fence=1",
            "commit": "committed booking_id=H-9 ledger_ref=LR-9 slot_id=S-c1 receipt=sig-LR-9",
        }.get(head)
        if answer:
            fake.chat.append(msg(LEDGER, answer, recipient=AGENT, need_id="N-X"))

    return reply


def _request(procedure: str = "VIS-CARD", rid: str = "x-1"):
    return msg(
        PARTNER,
        f"need.request service_type=specialist_care.cardiology procedure={procedure} "
        f"priority=B request_id={rid}",
        need_id="N-X",
    )


def _to_partner(fake: FakeCity) -> list[str]:
    return [t for to, t in fake.sent if to == PARTNER]


def _offered() -> list:
    """Our offer of S-c1 to the partner in this case: only an offered slot is confirmed by an agent."""
    return [msg(AGENT, "offer offer_id=of-S-c1 slot_id=S-c1 doctor_id=D-s2 procedure=VIS-CARD request_id=x-1",
                recipient=PARTNER, need_id="N-X")]


def test_cardiology_request_gets_our_offers_once(brain):
    fake = FakeCity([], on_post=_ledger(WORLD))
    run(brain, fake, _request())
    replies = _to_partner(fake)
    assert len(replies) == 1
    offers = [line for line in replies[0].splitlines() if line.startswith("offer ")]
    assert [o.split()[2] for o in offers] == ["slot_id=S-c1", "slot_id=S-c2"]
    assert all("request_id=x-1" in o.split() and "procedure=VIS-CARD" in o.split() for o in offers)
    # Solo attributi scritti dal Ledger: niente lingua o genere del medico (R:244, -2).
    assert not any(k in replies[0] for k in ("lang=", "gender=", "language="))


def test_no_free_cardiology_slot_gets_no_availability(brain):
    fake = FakeCity([], on_post=_ledger(EMPTY))
    run(brain, fake, _request(rid="x-2"))
    replies = _to_partner(fake)
    assert len(replies) == 1
    assert replies[0].startswith("cancel reason=no_availability request_id=x-2")


def test_other_procedure_is_not_ours(brain):
    fake = FakeCity([], on_post=_ledger(WORLD))
    run(brain, fake, _request(procedure="VIS-FIS", rid="x-3"))
    replies = _to_partner(fake)
    assert len(replies) == 1
    assert replies[0].startswith("cancel reason=no_availability request_id=x-3")
    assert "S-f1" not in replies[0]


def test_repeated_request_is_answered_once(brain):
    fake = FakeCity([], on_post=_ledger(WORLD))
    request = _request(rid="x-4")
    run(brain, fake, request)
    run(brain, fake, request)
    assert len(_to_partner(fake)) == 1


def test_partner_confirm_holds_with_their_consent_and_returns_receipt(brain):
    fake = FakeCity(_offered(), on_post=_ledger(WORLD))
    run(brain, fake, msg(
        PARTNER,
        "confirm offer_id=of-S-c1 slot_id=S-c1 citizen=citizen:T9 consent_ref=cons-x-cross "
        "prescription_number=prescription_number-ABCD1234",
        need_id="N-X",
    ))
    holds = [t for to, t in fake.sent if to == LEDGER and t.startswith("hold")]
    assert len(holds) == 1 and "consent_ref=cons-x-cross" in holds[0].split()
    assert any(t.startswith("booking.receipt") for t in _to_partner(fake))


def test_partner_confirm_without_consent_is_cancelled_never_held(brain):
    fake = FakeCity(_offered(), on_post=_ledger(WORLD))
    run(brain, fake, msg(PARTNER, "confirm offer_id=of-S-c1 slot_id=S-c1 citizen=citizen:T9", need_id="N-X"))
    assert not any(to == LEDGER and t.startswith("hold") for to, t in fake.sent)
    assert _to_partner(fake) and _to_partner(fake)[0].startswith("cancel offer_id=of-S-c1 reason=missing_consent_ref")


def test_conformance_probe_without_criteria_is_ignored(brain):
    # The deploy's conformance suite sends {"service": "lab"} as a need.request from an agent.
    # Answering it with late world reads and a cancel failed the hospital's deploys (0e2d784).
    from city_sdk.city import Message

    probe = Message(type="need.request", sender=PARTNER, msg_id="probe-1", need_id="N-P",
                    body={"service": "lab", "text": "need lab"})
    fake = FakeCity([], on_post=_ledger(WORLD))
    run(brain, fake, probe)
    assert fake.sent == []
