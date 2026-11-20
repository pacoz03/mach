-- Sentiment dei messaggi dei nostri agenti. Idempotente: si rilancia senza danni.
-- Una riga per episodio (episodes.id). Scrive solo il job pianificato dal laptop con la
-- secret key (tools/sentiment.py): niente grant ad anon, RLS attiva.
-- Privacy: qui non entra mai il testo del messaggio, solo punteggio, etichetta e una
-- ragione breve senza dati personali.

create table if not exists public.sentiments (
  id           uuid primary key default gen_random_uuid(),
  episode_id   uuid not null unique references public.episodes(id) on delete cascade,
  msg_id       text,
  agent        text not null,
  direction    text not null check (direction in ('in', 'out')),
  peer         text,
  need_id      text,
  at           timestamptz not null,                 -- quando è stato visto/inviato il messaggio
  score        numeric(3,2) not null check (score between -1 and 1),
  label        text not null check (label in ('positive', 'neutral', 'negative')),
  emotion      text,                                 -- es. satisfied, frustrated, confused, urgent
  confidence   numeric(3,2) check (confidence between 0 and 1),
  basis        text not null check (basis in ('text', 'act')), -- testo vero o solo act + token
  reason       text check (char_length(reason) <= 200),
  model        text,
  analyzed_at  timestamptz not null default now()
);

create index if not exists sentiments_agent_at on public.sentiments (agent, at desc);
create index if not exists sentiments_need on public.sentiments (need_id);
alter table public.sentiments enable row level security;
revoke all on public.sentiments from anon, authenticated;

-- Più fonti oltre a episodes: commenti pubblici della scoreboard e journey della console.
-- source_ref identifica il messaggio nella sua fonte (episode id, comment id, msg_id console).
alter table public.sentiments alter column episode_id drop not null;
alter table public.sentiments add column if not exists source text not null default 'episode';
alter table public.sentiments add column if not exists source_ref text;
alter table public.sentiments add column if not exists citizen_score numeric; -- voto del cittadino, se la fonte lo dà
update public.sentiments set source_ref = episode_id::text where source_ref is null;
do $$ begin
  alter table public.sentiments add constraint sentiments_source_check
    check (source in ('episode', 'comment', 'journey'));
exception when duplicate_object then null; end $$;
create unique index if not exists sentiments_source_ref on public.sentiments (source, source_ref);

-- Commenti pubblici dei nostri cittadini (scoreboard → comments, need_id con -890924 = ASST Gorla).
-- La scoreboard tiene solo gli ultimi 40 di tutta la città: li salva il collector ogni 30 s.
-- Testo pubblico, scritto dal cittadino simulato; leggibile solo con la secret key.
create table if not exists public.citizen_comments (
  id            uuid primary key default gen_random_uuid(),
  need_id       text not null,
  at            timestamptz not null,
  citizen_score numeric,
  text          text not null,
  first_seen    timestamptz not null default now(),
  unique (need_id, at)
);
alter table public.citizen_comments enable row level security;
revoke all on public.citizen_comments from anon, authenticated;

-- Episodi non ancora analizzati (input del job).
create or replace view public.sentiment_pending
with (security_invoker = true) as
select e.id as episode_id, e.msg_id, e.at, e.agent, e.version, e.direction, e.peer,
       e.peer_kind, e.need_id, e.act, e.fields, e.body, e.error
from public.episodes e
left join public.sentiments s on s.episode_id = e.id
where s.id is null;

-- Commenti pubblici non ancora analizzati.
create or replace view public.comment_pending
with (security_invoker = true) as
select c.id as comment_id, c.need_id, c.at, c.citizen_score, c.text
from public.citizen_comments c
left join public.sentiments s on s.source = 'comment' and s.source_ref = c.id::text
where s.id is null;
revoke all on public.comment_pending from anon, authenticated;

-- Andamento per agente e direzione, a finestre di 5 minuti.
create or replace view public.sentiment_by_agent
with (security_invoker = true) as
select agent, direction,
       date_bin('5 minutes', at, timestamptz '2026-01-01') as bucket,
       count(*)                                   as messages,
       round(avg(score), 2)                       as avg_score,
       count(*) filter (where label = 'negative') as negatives,
       count(*) filter (where label = 'positive') as positives
from public.sentiments
group by 1, 2, 3;

revoke all on public.sentiment_pending, public.sentiment_by_agent from anon, authenticated;
