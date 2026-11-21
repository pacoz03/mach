-- Builds tests/fixtures/*.txt from Supabase episodes (SELECT only). One output row per file:
-- `content` is the file body (the hand-written header comments excluded), `md5` covers its blocks without comments.
-- Run it with the Supabase MCP execute_sql or the SQL editor; see README.md.
with l as (
  select id, at, agent, act, fields f from episodes
  where direction = 'in' and peer = 'city:ledger' and agent <> 'unknown'
),
cm as (select distinct on (f->>'booking_id') at, agent, f->>'booking_id' bk from l where act = 'committed' order by f->>'booking_id', at),
cmx as (select cm.*,
  exists (select 1 from l where act = 'checked_in' and f->>'booking_id' = cm.bk) ci,
  exists (select 1 from l where act = 'reported' and f->>'booking_id' = cm.bk) rp,
  exists (select 1 from l where act = 'held' and f->'hold_id'->>0 = cm.bk) hd
  from cm),
chain as (
  (select bk, 'specialist_care: held, committed, checked_in, reported' why from cmx where agent = 'specialist_care' and hd and ci and rp order by at desc limit 3)
  union all (select bk, 'hospital: held, committed, checked_in (no report)' from cmx where agent = 'hospital' and hd and ci order by at desc limit 2)
  union all (select bk, 'hospital: held, committed, no check-in' from cmx where agent = 'hospital' and hd and not ci order by at desc limit 1)
  union all (select bk, 'family_counseling, direct access (no prescription_number): held, committed, checked_in' from cmx where agent = 'family_counseling' and hd and ci order by at desc limit 1)
  union all (select bk, 'family_counseling, direct access (no prescription_number): held, committed' from cmx where agent = 'family_counseling' and hd and not ci order by at desc limit 1)
),
sl as (select id, at, agent, f,
  case when jsonb_typeof(f->'page') = 'array' then f->'page'->>0 else f->>'page' end pg,
  jsonb_typeof(f->'page') = 'array' more,
  jsonb_array_length(f->'procedure') > jsonb_array_length(f->'slot_id') proc_more,
  case when jsonb_typeof(f->'urgent_reserve') = 'array' then jsonb_array_length(f->'urgent_reserve') else 1 end nur,
  (select count(*) from jsonb_array_elements_text(f->'free') v where v = '0') nfree0,
  (select count(distinct v) from jsonb_array_elements_text(f->'day') v) ndays
  from l where act = 'slots'),
slp as (
  select 1 o, (select id from sl where agent = 'hospital' and pg like '1/%' and proc_more and nur = 2 and ndays = 1 and nfree0 = 0 order by at desc limit 1) id
  union all select 2, (select id from sl where agent = 'hospital' and pg like '1/%' and proc_more and nur = 3 and ndays > 1 order by at desc limit 1)
  union all select 3, (select id from sl order by nfree0 desc, at desc limit 1)
  union all select 4, (select id from sl where nur = 1 and more order by at desc limit 1)
  union all select 5, (select id from sl where nur = 1 and not more order by at desc limit 1)
  union all select 6, (select id from sl where not more and f->'procedure'->>0 = 'VIS-CARD' order by at desc limit 1)
  union all select 7, (select id from sl where agent = 'specialist_care' and pg like '1/%' and proc_more order by at desc limit 1)
  union all select 8, (select id from sl where agent = 'family_counseling' and nfree0 > 0 order by at desc limit 1)
  union all select 9, (select id from sl where more and not proc_more and pg like '1/%' order by at desc limit 1)
),
pk0 as (
  select 'slots' file, s.o::text ord, s.id, '' note from slp s
  union all select 'held', '1' || c.bk, (select id from l where act = 'held' and f->'hold_id'->>0 = c.bk order by at limit 1), 'later committed: same id in committed.txt (' || c.why || ')' from chain c
  union all select 'held', '2' || agent, id, 'no committed reply for this hold in episodes (commits stopped after 01:20 UTC)' from (
    select distinct on (agent) id, agent from l h where act = 'held' and at > '2026-11-21 01:30'
      and not exists (select 1 from l c where c.act = 'committed' and c.f->>'booking_id' = h.f->'hold_id'->>0) order by agent, at desc) x
  union all select 'committed', c.bk, (select id from l where act = 'committed' and f->>'booking_id' = c.bk order by at limit 1), c.why from chain c
  union all select 'checked_in', c.bk, (select id from l where act = 'checked_in' and f->>'booking_id' = c.bk order by at limit 1), c.why from chain c join cmx using (bk) where cmx.ci
  union all select 'reported', '1' || c.bk, (select id from l where act = 'reported' and f->>'booking_id' = c.bk order by at limit 1), c.why from chain c join cmx using (bk) where cmx.rp
  union all (select 'reported', '2' || at::text, id, 'specialist_care report outside the chains above' from l where act = 'reported' and f->>'booking_id' not in (select bk from chain) order by at desc limit 2)
  union all select 'prescription', '1' || pr, id, 'priority/procedure/symptom combination ' || pr from (
    select distinct on (pr) id, pr from (select id, at, (f->>'priority') || ' ' || (f->>'procedure') || ' ' || (f->>'symptom_code') pr from l where act = 'prescription') y order by pr, at desc) x
  union all (select 'prescription', '2', id, 'patient id with the longer shape citizen:<wave>-<asst>-<nnnn>-<nnnnnn>' from l where act = 'prescription' and f->>'patient' ~ '-\d{4}-\d{6}$' order by at desc limit 1)
  union all (select 'prescription', '3', id, 'need_id with the 4-digit attempt suffix (journey restarted)' from l where act = 'prescription' and f->>'need_id' ~ '^N-\d{4}-\d{2}-\d{2}-\d{16}$' order by at desc limit 1)
  union all select 'agenda', d, id, 'agenda_day=' || d from (select distinct on (f->>'agenda_day') id, f->>'agenda_day' d from l where act = 'agenda' order by f->>'agenda_day', at desc) x
  union all (select 'rules', '1', id, 'the only shape seen so far: no rule active' from l where act = 'rules' order by at desc limit 1)
  union all select 'refused', k, id, '' from (
    select distinct on (k) id, k from (select id, at, concat_ws(' ', f->>'op', f->>'code', case when f->>'detail' ~ '^W01QQQ-' then 'slot:' || substring(f->>'detail' from '-([hsc])\d-') else f->>'detail' end) k from l where act = 'refused') y order by k, at desc) x
),
pk as (select distinct on (p.file, p.id) p.file, p.ord, p.note, l.* from pk0 p join l on l.id = p.id order by p.file, p.id, p.ord),
vals as (
  select 'bk' kind, v orig, p.at from pk p, jsonb_array_elements_text(case jsonb_typeof(p.f->'hold_id') when 'array' then p.f->'hold_id' else '[]'::jsonb end) v
  union all select 'bk', p.f->>'booking_id', p.at from pk p
  union all select 'ref', p.f->>'ledger_ref', p.at from pk p
  union all select 'rx', p.f->>'prescription_number', p.at from pk p
  union all select 'cit', substring(p.f->>'need_id' from '^N-\d{4}-\d{2}-\d{2}-\d{6}(\d{6})'), p.at from pk p
  union all select 'cit', substring(p.f->>'patient' from '(\d{6})$'), p.at from pk p
),
m as (select kind, orig, row_number() over (partition by kind order by min(at), orig) n from vals where orig is not null group by kind, orig),
fk as (select kind, orig, case kind
    when 'bk' then 'bk-00000000-0000-4000-8000-' || lpad(n::text, 12, '0')
    when 'ref' then 'aaaaaaaa-0000-4000-8000-' || lpad(n::text, 12, '0')
    when 'rx' then 'prescription_number-FACE' || lpad(n::text, 20, '0')
    when 'cit' then (900000 + n)::text end fake, n from m),
txt as (
  select p.file, p.ord, p.at, p.agent, p.note, case p.act
  when 'slots' then
    'slots page=' || (case when jsonb_typeof(f->'page') = 'array' then f->'page'->>0 else f->>'page' end) || ' matching=' || (f->>'matching') || ' asst=' || (f->>'asst')
    || ' from=' || (f->>'from') || ' until=' || (f->>'until') || ' wave_id=' || (f->>'wave_id')
    || (select string_agg(E'\nslot_id=' || (f->'slot_id'->>i) || ' doctor_id=' || (f->'doctor_id'->>i) || ' facility=' || (f->'facility'->>i)
        || ' procedure=' || (f->'procedure'->>i) || ' at=' || (f->'at'->>i) || ' day=' || (f->'day'->>i) || ' free=' || (f->'free'->>i)
        || case when (f->'slot_id'->>i) like '%-0900' then ' urgent_reserve=true' else '' end, '' order by i)
        from generate_series(0, jsonb_array_length(f->'slot_id') - 1) i)
    || case when jsonb_typeof(f->'page') = 'array' then E'\nmore: world'
         || case when jsonb_array_length(f->'procedure') > jsonb_array_length(f->'slot_id') then ' procedure=' || (f->'procedure'->>-1) else '' end
         || ' page=' || (f->'page'->>1) else '' end
  when 'agenda' then 'agenda rate=' || (f->>'rate') || ' paused=' || (f->>'paused') || ' agenda_day=' || (f->>'agenda_day') || ' agenda_now=' || (f->>'agenda_now')
    || ' epoch_real=' || (f->>'epoch_real') || ' epoch_agenda=' || (f->>'epoch_agenda') || ' wave_id=' || (f->>'wave_id')
  when 'rules' then 'rules count=' || (f->>'count')
  when 'refused' then 'refused ' || (f->>'op') || ' code=' || (f->>'code') || ' detail=' || (f->>'detail')
  when 'prescription' then 'prescription prescription_number=' || (select fake from fk where kind = 'rx' and orig = f->>'prescription_number')
    || ' patient=' || regexp_replace(f->>'patient', '\d{6}$', (select fake from fk where kind = 'cit' and orig = substring(f->>'patient' from '(\d{6})$')))
    || ' procedure=' || (f->>'procedure') || ' priority=' || (f->>'priority') || ' state=' || (f->>'state') || ' symptom_code=' || (f->>'symptom_code')
    || ' issued_by=' || (f->>'issued_by')
    || ' need_id=' || substring(f->>'need_id' from '^(N-\d{4}-\d{2}-\d{2}-\d{6})') || (select fake from fk where kind = 'cit' and orig = substring(f->>'need_id' from '^N-\d{4}-\d{2}-\d{2}-\d{6}(\d{6})')) || coalesce(substring(f->>'need_id' from '^N-\d{4}-\d{2}-\d{2}-\d{12}(\d+)$'), '')
    || ' ledger_ref=' || (select fake from fk where kind = 'ref' and orig = f->>'ledger_ref')
  when 'held' then 'held hold_id=' || (select fake from fk where kind = 'bk' and orig = f->'hold_id'->>0) || ' slot_id=' || (f->>'slot_id')
    || ' need_id=' || substring(f->>'need_id' from '^(N-\d{4}-\d{2}-\d{2}-\d{6})') || (select fake from fk where kind = 'cit' and orig = substring(f->>'need_id' from '^N-\d{4}-\d{2}-\d{2}-\d{6}(\d{6})')) || coalesce(substring(f->>'need_id' from '^N-\d{4}-\d{2}-\d{2}-\d{12}(\d+)$'), '')
    || ' fence=' || (f->>'fence') || E'\ncommit hold_id=' || (select fake from fk where kind = 'bk' and orig = f->'hold_id'->>1) || ' consent_ref=' || (f->>'consent_ref')
  when 'committed' then 'committed booking_id=' || (select fake from fk where kind = 'bk' and orig = f->>'booking_id') || ' ledger_ref=' || (select fake from fk where kind = 'ref' and orig = f->>'ledger_ref')
    || ' slot_id=' || (f->>'slot_id') || ' agenda_date=' || (f->>'agenda_date')
    || coalesce(' prescription_number=' || (select fake from fk where kind = 'rx' and orig = f->>'prescription_number'), '')
    || ' signature=' || split_part(f->>'signature', '..', 1) || '..' || rpad('FAKE' || lpad((select n from fk where kind = 'bk' and orig = f->>'booking_id')::text, 4, '0'), length(split_part(f->>'signature', '..', 2)), 'A')
  when 'checked_in' then 'checked_in booking_id=' || (select fake from fk where kind = 'bk' and orig = f->>'booking_id') || ' checkin_id=ci-' || (select fake from fk where kind = 'bk' and orig = f->>'booking_id')
    || ' ledger_ref=' || (select fake from fk where kind = 'ref' and orig = f->>'ledger_ref') || ' agenda_time=' || (f->>'agenda_time')
  when 'reported' then 'reported booking_id=' || (select fake from fk where kind = 'bk' and orig = f->>'booking_id') || ' report_id=rp-' || (select fake from fk where kind = 'bk' and orig = f->>'booking_id')
    || ' ledger_ref=' || (select fake from fk where kind = 'ref' and orig = f->>'ledger_ref')
  end body,
  (p.act <> 'checked_in' or f->>'checkin_id' = 'ci-' || (f->>'booking_id')) and (p.act <> 'reported' or f->>'report_id' = 'rp-' || (f->>'booking_id'))
  and (p.act <> 'held' or f->'hold_id'->>0 = f->'hold_id'->>1)
  and (p.act <> 'slots' or (select count(*) from jsonb_array_elements_text(f->'slot_id') v where v like '%-0900') = case when jsonb_typeof(f->'urgent_reserve') = 'array' then jsonb_array_length(f->'urgent_reserve') else 1 end) ok
  from pk p
)
select file, count(*) n, bool_and(ok and body is not null) ok, md5(string_agg(body, E'\n---\n' order by ord)) md5,
  string_agg('# ' || agent || ', ' || to_char(at at time zone 'UTC', 'YYYY-MM-DD HH24:MI') || ' UTC' || case when note <> '' then ': ' || note else '' end || E'\n' || body, E'\n---\n' order by ord) content
from txt group by file order by file
