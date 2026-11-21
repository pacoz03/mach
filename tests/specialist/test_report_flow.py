"""Specialista: referto. Il referto va archiviato subito dopo il check-in, una volta sola, senza
chiedere il consenso, e ogni messaggio del cittadino riceve risposta.
R: = mach-hackathon-kit/docs/kit/e19-rules.md."""

import asyncio



from conftest import AGENT, BOOKING, CITIZEN, LEDGER, FakeCity, msg, run, turn


def _checked_in_case() -> list:
    return [
        msg(CITIZEN, "Buongiorno, ho la visita cardiologica."),
        msg(LEDGER, f"checked_in booking_id={BOOKING}", recipient=AGENT),
    ]


# --- dopo il check-in: il referto subito, senza chiedere il consenso --------------------------

def test_checked_in_files_the_report_at_once(brain, fac):
    # Live journeys (wave 1): citizens never answered the consent ask, and the judge scored
    # "report_missing" 3 times out of 3. The report is filed right after the check-in.
    chat = _checked_in_case()
    fake = FakeCity(chat)
    run(brain, fake, chat[1])
    assert len(fake.to_citizen("checkin")) == 1
    assert fake.to_citizen("consent") == []
    assert fake.reports() == [f"report booking_id={BOOKING} outcome_code=VISIT"]
    assert len(fake.to_citizen("report")) == 1


# --- turni in parallelo dello stesso caso -----------------------------------------------------

def test_citizen_checkin_and_ledger_push_ask_once(brain, fac):
    chat = [msg(CITIZEN, "Buongiorno, ho la visita cardiologica.")]
    fake = FakeCity(chat, yields=True)
    checkin = msg(CITIZEN, f"checkin booking_id={BOOKING}")
    push = msg(LEDGER, f"checked_in booking_id={BOOKING}", recipient=AGENT)

    async def both() -> None:
        await asyncio.gather(turn(brain, fake, checkin), turn(brain, fake, push))

    asyncio.run(both())
    assert len(fake.to_citizen("checkin")) == 1
    assert len([t for to, t in fake.sent if to == LEDGER and t.startswith("checkin")]) == 1
    assert len(fake.reports()) == 1


# --- dopo il referto ---------------------------------------------------------------------------

def test_message_after_report_gets_a_reply_and_no_second_report(brain, fac):
    chat = _checked_in_case()
    chat.append(msg(AGENT, f"checkin booking_id={BOOKING} Grazie.", recipient=CITIZEN))
    chat.append(msg(AGENT, f"report booking_id={BOOKING} outcome_code=VISIT", recipient=LEDGER))
    chat.append(msg(LEDGER, f"reported booking_id={BOOKING}", recipient=AGENT))
    chat.append(msg(AGENT, f"report booking_id={BOOKING} outcome_code=VISIT Archiviato.", recipient=CITIZEN))
    fake = FakeCity(chat)
    run(brain, fake, msg(CITIZEN, "Grazie mille, arrivederci"))
    assert fake.reports() == []
    assert [to for to, _ in fake.sent] == [CITIZEN]
