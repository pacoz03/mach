"""shared/mach/case.py on the real Ledger replies of tests/fixtures/*.txt and on built histories."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from city_sdk.city import LEDGER, Message

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared"))
from mach import case  # noqa: E402

ME = "agent:me"
GP = "agent:gp"
CITIZEN = "citizen:W01QQQ-24-900001"
N1 = "prescription_number-FACE00000000000000000011"
N2 = "prescription_number-FACE00000000000000000012"


def blocks(name: str) -> list[str]:
    text = (ROOT / "tests" / "fixtures" / f"{name}.txt").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    return [b.strip() for b in "\n".join(lines).split("\n---\n") if b.strip()]


def msg(sender: str, text: str, mid: str, *, to: str = ME, at: str = "", **body) -> Message:
    return Message(type="chat", sender=sender, recipient=to, msg_id=mid, body={"text": text, **body}, sent_at=at)


def t(minute: int) -> str:
    return f"2026-11-21T02:{minute:02d}:00Z"


def ask(number: str, mid: str, at: str = "") -> Message:
    return msg(ME, f"prescription prescription_number={number}", mid, to=LEDGER, at=at)


def answer(number: str, mid: str, at: str = "", state: str = "issued") -> Message:
    return msg(LEDGER, f"prescription prescription_number={number} procedure=VIS-FIS priority=B state={state}", mid, at=at)


# --- prescription: the Ledger's answer to our ask, by sent_at ----------------------------------


@pytest.mark.parametrize("block", blocks("prescription"))
def test_prescription_on_the_real_answers(block):
    number = case.protocol.fields_of(block)["prescription_number"]
    chat = [msg(CITIZEN, f"prescription_number={number}", "c", at=t(0)), ask(number, "a", t(1)),
            msg(LEDGER, block, "r", at=t(5))]
    got = case.prescription(chat, ME, number)
    assert got == case.protocol.fields_of(block) and got["state"] == "issued" and got["procedure"]
    assert case.prescription(chat, ME, number, case.named(chat, number)) == got
    assert case.prescription(chat, ME, N2) is None  # another number: never this answer
    assert case.prescription(chat, "agent:other", number) is None  # not an answer to OUR ask


def test_answer_listed_before_the_citizen_message_still_counts():
    # Live N-…0260 (booking 1.0.14): the citizen wrote the number again while our read was in
    # flight; the Relay listed the issued answer before that message. It was sent after it.
    chat = [msg(CITIZEN, f"prescription_number={N1}", "c1", at=t(0)), ask(N1, "a", t(0)),
            answer(N1, "r", t(5)), msg(CITIZEN, f"prescription_number={N1}", "c2", at=t(1))]
    since = case.named(chat, N1)
    assert since.msg_id == "c2"
    assert case.prescription(chat, ME, N1, since)["state"] == "issued"
    assert not case.asked(chat, ME, N1, since)  # our ask predates c2: a new one would go out


def test_answer_sent_before_the_citizen_message_does_not_count():
    # The number named again days later (P1 may have expired it): only a fresh answer.
    chat = [ask(N1, "a", t(0)), answer(N1, "r", t(1)), msg(CITIZEN, f"prescription_number={N1}", "c", at=t(9))]
    since = case.named(chat, N1)
    assert case.prescription(chat, ME, N1) is not None
    assert case.prescription(chat, ME, N1, since) is None
    later = [*chat, ask(N1, "a2", t(10)), answer(N1, "r2", t(14), state="expired")]
    assert case.asked(later, ME, N1, since)
    assert case.prescription(later, ME, N1, since)["state"] == "expired"


def test_without_sent_at_the_place_in_history_decides():
    chat = [ask(N1, "a"), answer(N1, "r"), msg(CITIZEN, f"prescription_number={N1}", "c")]
    assert case.prescription(chat, ME, N1, case.named(chat, N1)) is None
    chat = [msg(CITIZEN, f"prescription_number={N1}", "c"), ask(N1, "a"), answer(N1, "r")]
    assert case.prescription(chat, ME, N1, case.named(chat, N1))["state"] == "issued"
    assert case.prescription([answer(N1, "r"), ask(N1, "a")], ME, N1) is None  # before our ask


def test_refused_and_not_answered():
    refused = msg(LEDGER, "refused prescription code=NOT_FOUND", "r", at=t(3))
    chat = [ask(N1, "a", t(1)), refused]
    assert case.prescription(chat, ME, N1) == {"code": "NOT_FOUND"}  # an answer, with no procedure
    assert case.prescription([ask(N1, "a", t(1))], ME, N1) is None
    assert case.prescription([], ME, N1) is None and not case.asked([], ME, N1)


def test_renewed_prescription():
    # P1: the first number expired; the gp renewed it (hand-off renewal_of=...) and the citizen books the new one.
    chat = [
        msg(GP, f"need.request handoff=gp citizen={CITIZEN} prescription_number={N1} procedure=VIS-FIS priority=B lang=it", "h1", at=t(0)),
        ask(N1, "a1", t(1)), answer(N1, "r1", t(5), state="expired"),
        msg(GP, f"need.request handoff=gp citizen={CITIZEN} prescription_number={N2} procedure=VIS-FIS priority=B lang=it "
            f"renewal_of={N1}", "h2", at=t(6)),
    ]
    assert case.number(chat) == N2
    assert case.prescription(chat, ME, N1)["state"] == "expired"
    assert case.prescription(chat, ME, N2) is None and not case.asked(chat, ME, N2)
    assert case.handed(chat, N2, GP) == {"procedure": "VIS-FIS", "priority": "B", "state": "issued"}
    chat += [ask(N2, "a2", t(7)), answer(N2, "r2", t(9))]
    assert case.prescription(chat, ME, N2)["state"] == "issued"


# --- the number and the gp hand-off ------------------------------------------------------------


def test_number():
    prose = "La ricetta è prescription_number-0123456789abcdef01234567, grazie"
    assert case.number([msg(CITIZEN, prose, "c")]) == "prescription_number-0123456789ABCDEF01234567"
    two = prose + " oppure prescription_number-ffffffffffffffffffffffff"
    assert case.number([msg(GP, f"need.request handoff=gp prescription_number={N1}", "h"), msg(CITIZEN, two, "c")]) is None
    assert case.number([msg(CITIZEN, "ciao", "c", prescription_number="RX 1")]) is None  # spaces: not a number
    assert case.number([msg(CITIZEN, f"ciao prescription_number={N2}", "c"), msg(GP, f"need.request handoff=gp prescription_number={N1}", "h")]) == N1
    assert case.number([msg(GP, f"need.request handoff=accepted prescription_number={N1}", "h")]) is None
    assert case.number([]) is None


def test_handoff_and_handed():
    h = msg(GP, f"need.request handoff=gp citizen={CITIZEN} prescription_number={N1} procedure=VIS-FIS priority=D", "h")
    chat = [h, msg(CITIZEN, "ciao", "c")]
    assert case.handoff(chat) is h and case.handoff(chat, N1) is h and case.handoff(chat, N2) is None
    assert case.handoff(chat, gp="agent:someone") is None
    assert case.handed(chat, N1, GP)["priority"] == "D"
    assert case.handed(chat, N1, "agent:someone") is None  # procedure and priority only from our gp
    assert case.handed([msg(GP, f"need.request handoff=gp prescription_number={N1}", "h")], N1, GP) is None


# --- preferences and language: the citizen first, then the gp hand-off ------------------------

PREFS = f"need.request handoff=gp citizen={CITIZEN} prescription_number={N1} procedure=VIS-FIS priority=B lang=it notify_if=no_slots "


def test_handoff_with_preference_tokens():
    h = msg(GP, PREFS + "doctor_lang=en gender_preference=f days=thu,fri Buongiorno, vi passo la ricetta", "h")
    assert case.days([h]) == {"thu", "fri"}  # the commas would split it for fields_of
    assert case.gender([h]) == "f"
    assert case.lang([h]) == "it"  # the language the gp spoke with them: we write in it
    assert case.lang([h], doctor=True) == "en"  # the doctor's language ranks the slots


def test_handoff_without_preference_tokens():
    h = msg(GP, PREFS + "Buongiorno, vi passo la ricetta", "h")
    assert case.days([h]) == frozenset() and case.gender([h]) is None
    assert case.lang([h]) == case.lang([h], doctor=True) == "it"
    assert case.lang([]) == "it" and case.days([]) == frozenset() and case.gender([]) is None


def test_the_citizen_wins_over_the_handoff():
    h = msg(GP, PREFS + "doctor_lang=en gender_preference=f days=thu,fri", "h")
    c = msg(CITIZEN, "confirm lang=sq gender_preference=m days=lun,mar", "c")
    assert (case.lang([h, c], doctor=True), case.gender([h, c]), case.days([h, c])) == ("sq", "m", {"mon", "tue"})
    heard = {"days": frozenset({"wed"}), "gender": "m", "lang": "de"}
    free = msg(CITIZEN, "solo mercoledì, con un uomo", "f")
    assert case.days([h, free], lambda row: heard) == {"wed"}
    assert case.gender([h, free], lambda row: heard) == "m"
    assert case.lang([h, free], lambda row: heard, doctor=True) == "de"
    assert case.days([h, free], lambda row: {}) == {"thu", "fri"}  # nothing heard: the hand-off


def test_days_and_gender_tokens():
    assert case.days([msg(CITIZEN, "ok", "c", provided={"days": ["Monday", "ven"]})]) == {"mon", "fri"}
    assert case.days([msg(CITIZEN, "days=mon", "a"), msg(CITIZEN, "days=sat", "b")]) == frozenset()  # latest says
    assert case.gender([msg(CITIZEN, "gender=f", "a"), msg(CITIZEN, "gender=m", "b")]) == "f"  # first token
    assert case.gender([msg(CITIZEN, "gender_preference=x gender=f", "a")]) is None


# --- offers ------------------------------------------------------------------------------------

OFFERS = ("offer offer_id=of-S1 slot_id=S1 doctor_id=D-h1 facility=agent:h\n"
          "offer offer_id=of-S2 slot_id=S2 doctor_id=D-h2 facility=agent:h\n"
          "offer offer_id=of-S3 slot_id=S3 doctor_id=D-h3 facility=agent:h\nQuale preferisce?")


def test_offers_and_a_withdrawn_one():
    chat = [msg(ME, OFFERS, "o", to=CITIZEN), msg(CITIZEN, "cancel offer_id=of-S2 reason=lang", "x")]
    assert [o["slot_id"] for o in case.offers_in(OFFERS)] == ["S1", "S2", "S3"]
    assert case.offered(chat, ME) == {"S1", "S2", "S3"}
    assert case.withdrawn(chat, ME) == {"S2"}
    assert case.our_offer(chat, ME, "of-S3")["doctor_id"] == "D-h3"
    assert case.our_offer(chat, ME, "of-S9") == {} and case.offered(chat, "agent:other") == set()
    chat.append(msg(ME, "cancel offer_id=of-S1 reason=facility_cancelled", "y", to=CITIZEN))
    assert case.withdrawn(chat, ME) == {"S1", "S2"}


# --- consents ----------------------------------------------------------------------------------


def test_consents():
    chat = [msg(ME, OFFERS, "o", to=CITIZEN), msg(CITIZEN, "confirm offer_id=of-S1", "c"),
            msg(ME, "consent purpose=booking offer_id=of-S1 Mi serve il consenso", "k", to=CITIZEN)]
    assert case.consent_offer(chat, ME, CITIZEN) == "of-S1"
    chat.append(msg(CITIZEN, "confirm offer_id=of-S1 consent_ref=cons-1", "y"))
    assert case.booking_consent(chat, CITIZEN, "of-S1") == "cons-1" and case.cross_consent(chat, CITIZEN) == ""
    chat.append(msg(CITIZEN, "consent purpose=cross_asst offer_id=of-S1 consent_ref=cons-x", "z"))
    assert case.booking_consent(chat, CITIZEN, "of-S1") == "cons-1" and case.cross_consent(chat, CITIZEN) == "cons-x"
    chat.append(msg(ME, "confirm offer_id=of-S1 slot_id=S1 citizen=" + CITIZEN, "f", to="agent:h"))
    assert case.consent_offer(chat, ME, CITIZEN) == ""  # the booking left for the facility
    assert case.consent_offer([msg(ME, OFFERS, "o", to=CITIZEN)], ME, CITIZEN) == ""


# --- hold and commit (facility side) -----------------------------------------------------------


@pytest.mark.parametrize("block", blocks("held"))
def test_booking_of_and_our_commit(block):
    held = case.protocol.fields_of(block)
    slot, hold = held["slot_id"], held["hold_id"]
    chat = [
        msg("agent:booking", f"confirm offer_id=of-{slot} slot_id={slot} citizen={CITIZEN} consent_ref=cons-1 "
            f"prescription_number={N1}", "c"),
        msg(ME, f"hold slot_id={slot} consent_ref=cons-1 citizen={CITIZEN}", "h", to=LEDGER),
        msg(LEDGER, block, "r"),  # its commit line names the hold too, but it is not ours
    ]
    assert case.booking_of(chat, ME, slot) == ("agent:booking", "cons-1", N1)
    assert not case.our_commit(chat, ME, hold)
    chat.append(msg(ME, f"commit hold_id={hold} consent_ref=cons-1", "k", to=LEDGER))
    assert case.our_commit(chat, ME, hold) and not case.our_commit(chat, ME, "bk-other")
    alone = [msg(ME, f"hold slot_id={slot} consent_ref=cons-2 citizen={CITIZEN}", "h", to=LEDGER)]
    assert case.booking_of(alone, ME, slot) == (CITIZEN, "cons-2", None)
