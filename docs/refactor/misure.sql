-- Misure dello step 6 (docs/refactor/piano.md). Solo SELECT: SQL Editor di Supabase o execute_sql.
-- La finestra sta in `params` di ogni query: cambia '3 hours'. Tempi in secondi.
--
-- sent_at = orologio del mittente (firma dell'envelope), at = quando il nostro agente ha visto il
-- messaggio (orologio del pod). Il sent_at sta nella colonna episodes.sent_at (shared/schema.sql)
-- oppure, per un pod con il tools.py nuovo su uno schema vecchio, in fields; to_jsonb(e) evita
-- l'errore se la colonna non esiste ancora. Prima del deploy dei pod nuovi le colonne *_ledger_* e
-- *_relay_* restano vuote: round_trip_* (richiesta -> risposta vista) c'è già.


-- 1. Tempo del turno per agente e versione: latency_ms = dall'arrivo del messaggio che ha aperto il
--    turno (tools.start_turn) a ogni nostro invio (tools.said), per destinatario.
with params as (select now() - interval '3 hours' as since)
select e.agent, e.version, e.peer_kind as to_kind, count(*) as sent,
       round((percentile_cont(0.5) within group (order by e.latency_ms) / 1000)::numeric, 1) as p50_s,
       round((percentile_cont(0.9) within group (order by e.latency_ms) / 1000)::numeric, 1) as p90_s,
       round(max(e.latency_ms) / 1000.0, 1) as max_s
  from public.episodes e, params
 where e.direction = 'out' and e.latency_ms is not null and e.at >= params.since
 group by 1, 2, 3
 order by 1, 2, 3;


-- 2. Risposte del Ledger, per agente e atto: ritardo del Ledger (nostra richiesta -> suo sent_at),
--    del Relay (suo sent_at -> l'agente la vede) e totale (richiesta -> risposta vista).
--    La richiesta è l'ultima dello stesso agente e caso con l'atto corrispondente, inviata prima di
--    vedere la risposta. Con più richieste uguali in volo (pagine di world) il ritardo del Ledger
--    risulta un po' più corto. Le righe senza need_id non trovano la richiesta.
with params as (select now() - interval '3 hours' as since),
replies as (
  select e.agent, e.need_id, e.act, e.at,
         coalesce((to_jsonb(e) ->> 'sent_at')::timestamptz,
                  case when e.fields ->> 'sent_at' ~ '^\d{4}-\d\d-\d\dT\d\d:\d\d'
                       then (e.fields ->> 'sent_at')::timestamptz end) as sent,
         case e.act when 'slots' then 'world' when 'held' then 'hold' when 'committed' then 'commit'
                    when 'cancelled' then 'cancel' when 'checked_in' then 'checkin'
                    when 'reported' then 'report' when 'prescribed' then 'prescribe'
                    when 'refused' then e.fields ->> 'op' else e.act end as asked
    from public.episodes e, params
   where e.direction = 'in' and e.peer = 'city:ledger' and e.at >= params.since
),
timed as (
  select r.agent, r.act,
         extract(epoch from r.sent - q.at) as ledger_s,
         extract(epoch from r.at - r.sent) as relay_s,
         extract(epoch from r.at - q.at) as round_trip_s
    from replies r
    left join lateral (
      select o.at from public.episodes o
       where o.agent = r.agent and o.need_id = r.need_id and o.direction = 'out'
         and o.peer = 'city:ledger' and o.act = r.asked and o.at <= r.at
       order by o.at desc limit 1) q on true
)
select agent, act, count(*) as replies, count(relay_s) as with_sent_at,
       round(percentile_cont(0.5) within group (order by ledger_s)::numeric, 1) as ledger_p50_s,
       round(percentile_cont(0.9) within group (order by ledger_s)::numeric, 1) as ledger_p90_s,
       round(percentile_cont(0.5) within group (order by relay_s)::numeric, 1) as relay_p50_s,
       round(percentile_cont(0.9) within group (order by relay_s)::numeric, 1) as relay_p90_s,
       round(percentile_cont(0.5) within group (order by round_trip_s)::numeric, 1) as round_trip_p50_s,
       round(percentile_cont(0.9) within group (order by round_trip_s)::numeric, 1) as round_trip_p90_s
  from timed
 group by 1, 2
 order by 1, 3 desc;


-- 3. Ritardo del Relay su tutto ciò che arriva, per agente e mittente (cittadino, Ledger, agente).
with params as (select now() - interval '3 hours' as since),
seen as (
  select e.agent, e.peer_kind,
         extract(epoch from e.at - coalesce((to_jsonb(e) ->> 'sent_at')::timestamptz,
                  case when e.fields ->> 'sent_at' ~ '^\d{4}-\d\d-\d\dT\d\d:\d\d'
                       then (e.fields ->> 'sent_at')::timestamptz end)) as relay_s
    from public.episodes e, params
   where e.direction = 'in' and e.at >= params.since
)
select agent, peer_kind as from_kind, count(*) as seen, count(relay_s) as with_sent_at,
       round(percentile_cont(0.5) within group (order by relay_s)::numeric, 1) as relay_p50_s,
       round(percentile_cont(0.9) within group (order by relay_s)::numeric, 1) as relay_p90_s
  from seen
 group by 1, 2
 order by 1, 2;
