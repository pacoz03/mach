"""Livello 5, "Vincoli combinati": le richieste dei partner possono nominare lingua, genere del
medico e giorni. Lo specialista mette prima gli slot che li rispettano, legge anche la seconda
pagina di world e, se nella finestra di priorità non c'è nulla, offre le prime date dopo."""

from conftest import AGENT, LEDGER, FakeCity, msg, run

PARTNER = "agent:partnerbook01"
NOW = "2026-11-16T08:00:00+00:00"  # lunedì


def _slot(sid: str, doctor: str, at: str, day: str) -> str:
    return (f"slot_id={sid} doctor_id=asst-gorla-{doctor} facility=agent:spec "
            f"procedure=VIS-CARD at={at} day={day} free=1")


# s1: uomo, it/en, lun-mer-ven; s2: donna, solo it, mar-gio (registro reale, _DOCTORS).
S2_TUE = _slot("S-s2-tue", "s2", "2026-11-17T10:00:00+00:00", "tue")
S1_WED = _slot("S-s1-wed", "s1", "2026-11-18T10:00:00+00:00", "wed")
S2_THU = _slot("S-s2-thu", "s2", "2026-11-19T10:00:00+00:00", "thu")
S1_FRI = _slot("S-s1-fri", "s1", "2026-11-20T10:00:00+00:00", "fri")
PAST = _slot("S-past", "s1", "2026-11-09T10:00:00+00:00", "mon")


def _ledger(pages: list[list[str]]):
    def reply(to: str, text: str, fake: FakeCity) -> None:
        if to != LEDGER:
            return
        fields = dict(t.split("=", 1) for t in text.split() if "=" in t)
        head = text.split()[0]
        if head == "agenda":
            answer = f"agenda agenda_now={NOW} agenda_day=mon"
        elif head == "world":
            n = int(fields.get("page", "1"))
            rows = pages[n - 1] if n <= len(pages) else []
            answer = "\n".join([f"slots page={n}/{len(pages)} count={len(rows)}", *rows])
        else:
            return
        fake.chat.append(msg(LEDGER, answer, recipient=AGENT, need_id="N-X"))

    return reply


def _ask(extra: str, rid: str = "x-5", priority: str = "P") -> list[str]:
    return [f"need.request service_type=specialist_care.cardiology procedure=VIS-CARD "
            f"priority={priority} request_id={rid} {extra}".strip()]


def _offered(fake: FakeCity) -> list[str]:
    replies = [t for to, t in fake.sent if to == PARTNER]
    assert len(replies) == 1
    return [line.split()[2].split("=", 1)[1] for line in replies[0].splitlines() if line.startswith("offer ")]


def _run(brain, pages, extra="", priority="P") -> list[str]:
    fake = FakeCity([], on_post=_ledger(pages))
    run(brain, fake, msg(PARTNER, _ask(extra, priority=priority)[0], need_id="N-X"))
    return _offered(fake)


def test_reads_page_two_when_page_one_has_nothing_ahead(brain):
    assert _run(brain, [[PAST], [S2_TUE, S1_WED]]) == ["S-s2-tue", "S-s1-wed"]


def test_language_puts_a_doctor_who_speaks_it_first(brain):
    assert _run(brain, [[S2_TUE, S1_WED, S2_THU]], "lang=en")[0] == "S-s1-wed"


def test_gender_puts_a_matching_doctor_first(brain):
    assert _run(brain, [[S1_WED, S2_THU, S1_FRI]], "gender=f")[0] == "S-s2-thu"


def test_combined_constraints_rank_full_matches_first(brain):
    # lang=en and Friday: only s1 on Friday meets both; s1 Wednesday meets one.
    offered = _run(brain, [[S2_TUE, S1_WED, S2_THU, S1_FRI]], "lang=en days=fri")
    assert offered[:2] == ["S-s1-fri", "S-s1-wed"]


def test_empty_priority_window_offers_the_first_dates_after(brain):
    # Priority U is today only: nothing today, so the first free slots after (relaxed, R:232).
    fake = FakeCity([], on_post=_ledger([[S2_TUE, S1_WED]]))
    run(brain, fake, msg(PARTNER, _ask("", rid="x-6", priority="U")[0], need_id="N-X"))
    reply = [t for to, t in fake.sent if to == PARTNER][0]
    assert reply.startswith("offer ")
    assert "Entro i tempi della priorità U" in reply


def test_gender_preference_field_is_read_too(brain):
    # Our teams' booking centres name it gender_preference (booking_centre, consultorio).
    assert _run(brain, [[S1_WED, S2_THU, S1_FRI]], "gender_preference=f")[0] == "S-s2-thu"
