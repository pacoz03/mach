# Ledger reply fixtures

Real `city:ledger` replies, one file per reply act: `slots.txt` (answer to `world`, 9), `agenda.txt` (5), `prescription.txt` (5), `rules.txt` (1), `refused.txt` (10), `held.txt` (11), `committed.txt` (8), `checked_in.txt` (6), `reported.txt` (5). One message per block, blocks separated by a `---` line, `#` lines are comments. Taken on 2026-11-21 from rows written between 2026-11-20 22:38 and 2026-11-21 04:03 UTC.

**Source.** Supabase `episodes`, `direction='in' and peer='city:ledger'`. The text of inbound rows is not stored (`body` is null), so `fixtures.sql` rebuilds it from `act` + `fields` (the tokens `shared/tools.py:parse` kept, with repeated keys as arrays in text order). `tools.parse(block)` returns the stored `fields` exactly: checked for every slots, agenda, rules and refused block.

**Not verbatim.** jsonb does not keep key order, so token order inside a line is our choice (the same as `tests/journey.py` `REPLY`). Also reconstructed: in `slots`, which scalar tokens sit in the header and the wording of the `more:` line (the arrays prove it comes after the slot lines); the `commit hold_id=… consent_ref=<ref>` line in `held` (`<ref>` is the Ledger's own placeholder); `urgent_reserve=true` on the 09:00 slots. `detail=` in `refused` keeps only its first word: the free text after it is lost.

**Anonymized**, with the same mapping in every file, so a hold, its commit, the check-in and the report still match. `hold_id` and `booking_id` become `bk-00000000-0000-4000-8000-0000000000NN`, and the `ci-`/`rp-` ids are built from them. `ledger_ref` becomes `aaaaaaaa-…-NN`, `prescription_number` becomes `prescription_number-FACE…NN`, and the signature part of the JWS becomes `FAKE…`. The citizen number in `need_id` and `patient` becomes `9000NN`; the ASST code and the attempt suffix stay. Agent, slot and doctor ids, dates and `wave_id` are kept. No personal keys and no citizen text.

**Regenerate.** Run `fixtures.sql` (SELECT only) with the Supabase MCP `execute_sql`. Paste each row's `content` under that file's hand-written header comments, then check that `md5` matches the blocks with the comments stripped. Samples change with the data.
