-- Eval dei percorsi: cosa ha deciso il giudice, cosa abbiamo perso, cosa ne abbiamo imparato.
-- Idempotente: si rilancia nel SQL Editor (o via Management API). Dipende da schema.sql.
--
--   journeys          un percorso della console (Journeys): stato, livello, punteggio, fatti del
--                     verdetto e trascrizione ripulita (testo solo dei nostri agenti).
--   outcomes          (schema.sql) una riga per riga di rubrica e per voto di review
--                     (review.collaboration / review.proactivity / review.domain).
--   journey_analyses  la nostra diagnosi di un percorso: punti persi, causa, correzione.
--   missed_points     vista: ogni punto o bonus lasciato sul tavolo, per percorso e agente.
--   eval_summary      vista: per livello, ruolo e riga quanti punti presi e quanti persi.
--
-- Scrive solo tools/journeys_sync.py dal laptop (secret key). Mai testo del cittadino.

create table if not exists public.journeys (
  need_id     text primary key,
  wave_id     text,
  level       integer,
  citizen     text,
  state       text,                          -- open, booked, done, escalated, failed
  score       numeric,
  verdict_at  timestamptz,                   -- null = il giudice non ha ancora deciso
  answer_set  jsonb,                         -- size, pref_size, effective_size
  facts       jsonb not null default '[]'::jsonb,   -- verdict.facts (hold, commit, receipt, ...)
  rule_versions jsonb not null default '[]'::jsonb,
  transcript  jsonb not null default '[]'::jsonb,   -- messaggi ripuliti, in ordine
  first_at    timestamptz,
  last_at     timestamptz,
  n_messages  integer,
  updated_at  text,                          -- cursore della console: cambia se cambia il percorso
  fetched_at  timestamptz not null default now()
);
create index if not exists journeys_level_state on public.journeys (level, state);

create table if not exists public.journey_analyses (
  id          uuid primary key default gen_random_uuid(),
  need_id     text not null references public.journeys (need_id),
  role        text not null default '*',     -- ruolo a cui imputiamo la perdita ('*' = percorso)
  category    text not null,                 -- es. no_collaboration, report_missing, failed_timeout
  missed      numeric not null default 0,    -- punti persi stimati per questa causa
  diagnosis   text not null,                 -- cosa è successo, verificabile sulla trascrizione
  fix         text,                          -- cosa cambiare (file/funzione) o la regola da seguire
  lesson_id   uuid references public.lessons (id),
  status      text not null default 'open' check (status in ('open', 'fixed', 'wontfix')),
  analyst     text not null default 'claude',
  created_at  timestamptz not null default now(),
  unique (need_id, role, category)
);

-- Ogni punto lasciato sul tavolo. "missed" = quanto avremmo potuto prendere in più:
--   bonus di review a 0 -> 1 punto; riga negativa -> |punti| (più il premio mancato, non stimato);
--   fatto del verdetto con ok=false -> 1; percorso failed -> una riga sola, missed null.
create or replace view public.missed_points with (security_invoker = true) as
select o.need_id, j.level, j.state, o.agent as role, o.fact, o.points,
       case when o.fact like 'review.%' then 1 - o.points else -o.points end as missed,
       o.reason, j.verdict_at
  from public.outcomes o join public.journeys j using (need_id)
 where coalesce((o.raw ->> 'cancelled')::boolean, false) = false
   and ((o.fact like 'review.%' and o.points < 1) or (o.fact not like 'review.%' and o.points <= 0))
union all
select j.need_id, j.level, j.state, f ->> 'role', 'fact.' || (f ->> 'kind'), 0, 1,
       coalesce(f -> 'detail' ->> 'reason', f ->> 'ref'), j.verdict_at
  from public.journeys j, jsonb_array_elements(j.facts) f
 where (f ->> 'ok') = 'false'
union all
select j.need_id, j.level, j.state, '*', 'journey.' || j.state, 0, null, null, j.verdict_at
  from public.journeys j
 where j.state = 'failed';

create or replace view public.eval_summary with (security_invoker = true) as
select j.level, o.agent as role, o.fact,
       count(*)                                         as rows,
       count(distinct o.need_id)                        as journeys,
       sum(o.points)                                    as points,
       count(*) filter (where o.points > 0)             as earned,
       count(*) filter (where o.points <= 0)            as not_earned
  from public.outcomes o join public.journeys j using (need_id)
 where coalesce((o.raw ->> 'cancelled')::boolean, false) = false
 group by 1, 2, 3;

alter table public.journeys enable row level security;
alter table public.journey_analyses enable row level security;
revoke all on public.journeys, public.journey_analyses, public.missed_points, public.eval_summary
  from anon, authenticated;
