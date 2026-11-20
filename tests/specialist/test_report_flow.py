"""Specialista: consenso report_sharing e referto. Il referto va archiviato sempre, una volta
sola, e ogni messaggio del cittadino riceve risposta. R: = mach-hackathon-kit/docs/kit/e19-rules.md."""

import asyncio

from city_sdk.city import CapError

from conftest import AGENT, BOOKING, CITIZEN, LEDGER, FakeCity, msg, run, turn


def _checked_in_case() -> list:
    return [
        msg(CITIZEN, "Buongiorno, ho la visita cardiologica."),
        msg(LEDGER, f"checked_in booking_id={BOOKING}", recipient=AGENT),
    ]


def _asked_case(fac) -> list:
    """Check-in fatto e consenso già chiesto: si aspetta la risposta del cittadino."""

    chat = _checked_in_case()
    chat.append(msg(AGENT, f"checkin booking_id={BOOKING} Grazie.", recipient=CITIZEN))
    chat.append(msg(AGENT, f"consent purpose=report_sharing booking_id={BOOKING} Posso?", recipient=CITIZEN))
    return chat


# --- dopo il check-in: si chiede il consenso, il referto segue la risposta -------------------

def test_checked_in_asks_consent_and_waits_for_the_answer(brain, fac):
    chat = _checked_in_case()
    fake = FakeCity(chat)
    run(brain, fake, chat[1])
    assert len(fake.to_citizen("checkin")) == 1
    assert len(fake.to_citizen("consent")) == 1
    assert fake.reports() == []


def test_consent_ask_undeliverable_still_files_report(brain, fac):
    def consent_rejected(to: str, text: str):
        if to == CITIZEN and text.startswith("consent"):
            return CapError("SCHEMA_INVALID", "to")
        return None

    chat = _checked_in_case()
    fake = FakeCity(chat, fail=consent_rejected)
    run(brain, fake, chat[1])
    assert fake.reports() == [f"report booking_id={BOOKING} outcome_code=VISIT"]


# --- turni in parallelo dello stesso caso -----------------------------------------------------

def test_citizen_checkin_and_ledger_push_ask_once(brain, fac):
    chat = [msg(CITIZEN, "Buongiorno, ho la visita cardiologica.")]
    fake = FakeCity(chat, yields=True)
    checkin = msg(CITIZEN, f"checkin booking_id={BOOKING}")
    push = msg(LEDGER, f"checked_in booking_id={BOOKING}", recipient=AGENT)

    async def both() -> None:
        await asyncio.gather(turn(brain, fake, checkin), turn(brain, fake, push))

    asyncio.run(both())
    assert len(fake.to_citizen("consent")) == 1
    assert len(fake.to_citizen("checkin")) == 1
    assert len([t for to, t in fake.sent if to == LEDGER and t.startswith("checkin")]) == 1


# --- risposte del cittadino in un turno proprio -----------------------------------------------

def test_consent_ref_in_own_turn_files_shared_report(brain, fac):
    fake = FakeCity(_asked_case(fac))
    run(brain, fake, msg(CITIZEN, "consent consent_ref=c-9 purpose=report_sharing Va bene"))
    assert fake.reports() == [f"report booking_id={BOOKING} outcome_code=VISIT consent_ref=c-9"]


def test_plain_no_in_own_turn_files_unshared_report(brain, fac):
    fake = FakeCity(_asked_case(fac))
    run(brain, fake, msg(CITIZEN, "No, preferisco di no"))
    assert fake.reports() == [f"report booking_id={BOOKING} outcome_code=VISIT"]


def test_message_after_report_gets_a_reply_and_no_second_report(brain, fac):
    chat = _asked_case(fac)
    chat.append(msg(AGENT, f"report booking_id={BOOKING} outcome_code=VISIT", recipient=LEDGER))
    chat.append(msg(LEDGER, f"reported booking_id={BOOKING}", recipient=AGENT))
    chat.append(msg(AGENT, f"report booking_id={BOOKING} outcome_code=VISIT Archiviato.", recipient=CITIZEN))
    fake = FakeCity(chat)
    run(brain, fake, msg(CITIZEN, "Grazie mille, arrivederci"))
    assert fake.reports() == []
    assert [to for to, _ in fake.sent] == [CITIZEN]
