"""Livello 5, "Vincoli combinati": le richieste dei partner possono nominare lingua, genere del
medico e giorni. Lo specialista mette prima gli slot che li rispettano, legge anche la seconda
pagina di world e, se nella finestra di priorità non c'è nulla, offre le prime date dopo."""

from datetime import datetime, timezone

from conftest import AGENT, LEDGER, FakeCity, msg, run

PARTNER = "agent:yzcs4p44ep39"  # un centro prenotazioni dell'anello (asst-corvetto): MACH-2.1 R17
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
            # As the live Ledger: the city clock fields too (epoch_real/epoch_agenda/rate).
            real = datetime.now(timezone.utc).isoformat()
            answer = (f"agenda agenda_now={NOW} agenda_day=mon epoch_real={real} "
                      f"epoch_agenda={NOW} rate=60")
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


def test_slow_world_answers_from_the_cached_page(brain):
    # Live Ledger: world comes back after ~12 s, past our wait (21a6d28). The second partner
    # request is answered from the page the first one read, not with no_availability.
    slow = {"on": False}
    base = _ledger([[S2_TUE, S1_WED]])

    def ledger(to: str, text: str, fake: FakeCity) -> None:
        if slow["on"] and to == LEDGER and text.startswith("world"):
            return  # no reply in time
        base(to, text, fake)

    fake = FakeCity([], on_post=ledger)
    run(brain, fake, msg(PARTNER, _ask("", rid="c-1")[0], need_id="N-X"))
    slow["on"] = True
    run(brain, fake, msg(PARTNER, _ask("", rid="c-2")[0], need_id="N-Y"))
    second = [t for to, t in fake.sent if to == PARTNER][1]
    assert second.startswith("offer ") and "request_id=c-2" in second.split()


def _timed(brain, fake, inbound) -> float:
    import time
    t0 = time.monotonic()
    run(brain, fake, inbound)
    return time.monotonic() - t0


def test_turn_ends_right_after_the_answer(brain):
    # The fast reply (+1, R:246) is measured on the ack, which the cloud sends at the end of the
    # turn: waiting ~20 s for world after answering lost it on every partner request.
    slow = {"on": False}
    base = _ledger([[S2_TUE, S1_WED]])

    def ledger(to: str, text: str, fake: FakeCity) -> None:
        if slow["on"] and to == LEDGER and text.startswith("world"):
            return
        base(to, text, fake)

    fake = FakeCity([], on_post=ledger)
    run(brain, fake, msg(PARTNER, _ask("", rid="t-1")[0], need_id="N-X"))
    slow["on"] = True
    took = _timed(brain, fake, msg(PARTNER, _ask("", rid="t-2")[0], need_id="N-Y"))
    assert [t for to, t in fake.sent if to == PARTNER][1].startswith("offer ")
    assert took < 3, f"il turno è rimasto vivo {took:.1f} s dopo la risposta"


def test_world_reply_turn_fills_the_cache(brain):
    # The Ledger's late world answer arrives as a turn of its own: it refreshes the cache.
    fake = FakeCity([], on_post=lambda to, text, f: None)  # the Ledger never answers in a turn
    page = "\n".join(["slots page=1/1 count=2", S2_TUE, S1_WED])
    clock = (f"agenda agenda_now={NOW} agenda_day=mon epoch_real={datetime.now(timezone.utc).isoformat()} "
             f"epoch_agenda={NOW} rate=60")
    run(brain, fake, msg(LEDGER, clock, recipient=AGENT, need_id="N-L"))
    run(brain, fake, msg(LEDGER, page, recipient=AGENT, need_id="N-L"))
    run(brain, fake, msg(PARTNER, _ask("", rid="t-3")[0], need_id="N-Z"))
    reply = [t for to, t in fake.sent if to == PARTNER][0]
    assert reply.startswith("offer ") and "request_id=t-3" in reply.split()
