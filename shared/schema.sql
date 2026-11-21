-- =============================================================================
-- Memoria condivisa dei nostri 5 agenti — data model (Supabase, Postgres 15+).
-- Idempotente: Supabase -> SQL Editor -> incolla tutto -> Run. Si può rilanciare.
--
-- Tre livelli:
--   episodes  cosa è successo: ogni messaggio visto o inviato da un nostro agente. Immutabile.
--             È anche la traccia per gli eval (versione del brain, latenza, errori).
--   facts     cosa è vero ora: fatti (subject, predicate) -> value, bi-temporali, con fonte e
--             messaggio d'origine. Un fatto nuovo chiude il vecchio (valid_to), non lo cancella.
--   lessons   come comportarsi: regole distillate dagli eval, attive solo dopo la nostra review.
--   outcomes  le valutazioni del giudice (console -> Journeys), da unire alle tracce.
--
-- Contro i guasti tipici della memoria condivisa (Margalit et al., arXiv 2606.24535):
--   contesto vecchio     -> valid_to ed expires_at: il contesto mostra solo fatti ancora validi
--   contraddizioni       -> un solo fatto corrente per (subject, predicate)
--   provenienza persa    -> source e source_msg_id su ogni fatto
--   fughe/avvelenamento  -> autorità delle fonti (ledger > agent > citizen > web), nessun dato
--                           personale, la publishable key passa solo dalle tre funzioni in fondo
--   righe in ritardo     -> una riga vecchia del Ledger non riporta indietro lo stato
-- Limite accettato: observe() si fida del chiamante, quindi chi ha la publishable key può
-- dichiarare righe del Ledger. Per questo la chiave non sta in git.
-- Modello bi-temporale ispirato a Zep/Graphiti (Rasmussen et al., arXiv 2501.13956).
--
-- Soggetti: need:<need_id>, slot:<id>, hold:<id>, booking:<id>, prescription:<numero>,
-- city:<cosa> per lo stato della città (regole, strutture giù), valido per ogni caso.
-- =============================================================================

create table if not exists public.episodes (
  id         uuid primary key default gen_random_uuid(),
  msg_id     text,                           -- msg_id della città: una volta per agente (vedi indice)
  at         timestamptz not null default now(),
  agent      text not null,                  -- nostro ruolo: gp, booking_centre, hospital, ...
  version    text,                           -- versione del brain (card.yaml): confronto negli eval
  direction  text not null check (direction in ('in', 'out')),
  peer       text not null,                  -- mittente (in) o destinatario (out)
  peer_kind  text generated always as (
               case when peer like 'citizen:%' then 'citizen'
                    when peer = 'city:ledger' then 'ledger'
                    when peer like 'city:%' then 'city'
                    else 'agent' end) stored,
  need_id    text,
  citizen    text,                           -- citizen:<id>: unisce le tappe del percorso
  act        text not null default '',       -- parola d'atto
  fields     jsonb not null default '{}'::jsonb,  -- token key=value, già filtrati
  body       text,                           -- solo i NOSTRI messaggi in uscita
  latency_ms integer,                        -- out: ms dall'arrivo del messaggio del turno
  error      text                            -- out fallito: codice CapError (TARGET_NOT_VISIBLE, ...)
);
-- Un messaggio tra due nostri agenti ha lo stesso msg_id ai due capi: uno per lato.
create unique index if not exists episodes_msg on public.episodes (msg_id, agent);
alter table public.episodes drop constraint if exists episodes_msg_id_key;  -- vecchio unique (msg_id)
-- in: sent_at del mittente (suo orologio). at - sent_at = ritardo del Relay; sent_at della risposta
-- del Ledger - at della nostra richiesta = ritardo del Ledger (docs/refactor/misure.sql).
alter table public.episodes add column if not exists sent_at timestamptz;
create index if not exists episodes_need on public.episodes (need_id, at);
create index if not exists episodes_citizen on public.episodes (citizen, at);

create table if not exists public.facts (
  id            uuid primary key default gen_random_uuid(),
  subject       text not null,
  predicate     text not null,
  value         jsonb not null,
  source        text not null check (source in ('ledger', 'agent', 'citizen', 'web')),
  agent         text not null,               -- ruolo che l'ha registrato
  need_id       text,
  citizen       text,
  source_msg_id text,                        -- provenienza: episodes.msg_id
  valid_from    timestamptz not null default now(),
  valid_to      timestamptz,                 -- null = ancora vero
  expires_at    timestamptz                  -- scadenza naturale (null = mai)
);
create unique index if not exists facts_current on public.facts (subject, predicate) where valid_to is null;
create index if not exists facts_need on public.facts (need_id) where valid_to is null;
create index if not exists facts_citizen on public.facts (citizen) where valid_to is null;
create index if not exists facts_history on public.facts (subject, predicate, valid_from);

create table if not exists public.lessons (
  id         uuid primary key default gen_random_uuid(),
  role       text not null default '*',      -- ruolo a cui si applica, '*' = tutti
  trigger    text not null default '*',      -- quando: parola d'atto o codice (refused:NO_CAPACITY)
  lesson     text not null,                  -- cosa fare, in una frase verificabile
  evidence   jsonb not null default '[]'::jsonb,  -- record_id / need_id degli eval
  status     text not null default 'proposed' check (status in ('proposed', 'active', 'retired')),
  created_at timestamptz not null default now()
);

create table if not exists public.outcomes (
  id        uuid primary key default gen_random_uuid(),
  record_id text unique,                     -- record_id della valutazione in console
  need_id   text,
  citizen   text,
  agent     text,                            -- nostro ruolo valutato
  rater     text,                            -- citizen, city:ledger, city:audit, city:review, ...
  fact      text not null,                   -- riga di rubrica
  points    numeric not null,
  reason    text,
  at        timestamptz not null default now(),
  raw       jsonb not null default '{}'::jsonb
);

-- Livelli della gara (colonna Il Token della Sera): una riga a mano quando City Hall alza il
-- livello. Gli eval dividono i risultati per livello: cosa si rompe salendo di difficoltà.
create table if not exists public.levels (
  level       integer primary key,
  started_at  timestamptz not null,
  description text
);

-- Righe di eval con versione del brain e livello del caso: regressioni tra deploy e livelli.
create or replace view public.eval_rows with (security_invoker = true) as
select o.*,
       (select e.version from public.episodes e
         where e.agent = o.agent and (e.need_id = o.need_id or e.citizen = o.citizen)
         order by e.at desc limit 1) as version,
       (select l.level from public.levels l
         where l.started_at <= coalesce((select min(e.at) from public.episodes e
                                          where e.need_id = o.need_id), o.at)
         order by l.started_at desc limit 1) as level
  from public.outcomes o;

-- --- logica ------------------------------------------------------------------------------------

create or replace function public.source_rank(p_source text) returns integer
language sql immutable set search_path = '' as $$
  select case p_source when 'ledger' then 3 when 'agent' then 2 when 'citizen' then 1 else 0 end
$$;

-- Afferma un fatto. 'inserted' | 'superseded' | 'unchanged' | 'rejected' (fonte meno affidabile
-- di quella del fatto corrente ancora valido). Interna: non esposta alla publishable key.
create or replace function public._assert(
  p_subject text, p_predicate text, p_value jsonb, p_source text, p_agent text,
  p_need_id text, p_citizen text, p_source_msg_id text, p_expires_at timestamptz
) returns text
language plpgsql security definer set search_path = '' as $$
declare
  cur public.facts%rowtype;
  outcome text := 'inserted';
begin
  perform pg_advisory_xact_lock(hashtext(p_subject || '|' || p_predicate));
  select * into cur from public.facts
   where subject = p_subject and predicate = p_predicate and valid_to is null;
  if found then
    if cur.expires_at is not null and cur.expires_at <= now() then
      update public.facts set valid_to = cur.expires_at where id = cur.id;
    elsif cur.value = p_value then
      if p_expires_at is distinct from cur.expires_at
         and public.source_rank(p_source) >= public.source_rank(cur.source) then
        update public.facts set expires_at = p_expires_at where id = cur.id;  -- riconferma
      end if;
      return 'unchanged';
    elsif public.source_rank(p_source) < public.source_rank(cur.source) then
      return 'rejected';
    else
      update public.facts set valid_to = now() where id = cur.id;
      outcome := 'superseded';
    end if;
  end if;
  insert into public.facts
    (subject, predicate, value, source, agent, need_id, citizen, source_msg_id, expires_at)
  values
    (p_subject, p_predicate, p_value, p_source, p_agent, p_need_id, p_citizen, p_source_msg_id,
     p_expires_at);
  return outcome;
end $$;

-- Un fatto deciso da un agente (o detto dal cittadino, o letto sul web). I fatti del Ledger
-- entrano solo da observe(), da una riga vera del Ledger.
create or replace function public.assert_fact(
  p_subject text, p_predicate text, p_value jsonb, p_agent text,
  p_source text default 'agent', p_need_id text default null, p_citizen text default null,
  p_source_msg_id text default null, p_ttl_seconds integer default null
) returns text
language plpgsql security definer set search_path = '' as $$
begin
  if p_source not in ('agent', 'citizen', 'web') then
    raise exception 'source % non ammessa: i fatti del ledger entrano solo da observe()', p_source
      using errcode = '22023';
  end if;
  return public._assert(p_subject, p_predicate, p_value, p_source, p_agent, p_need_id, p_citizen,
                        p_source_msg_id, now() + make_interval(secs => p_ttl_seconds));
end $$;

-- Registra un messaggio e ne ricava i fatti. 'inserted' | 'duplicate'.
--   ledger in   -> stato di slot/hold/booking/prescription (con lo slot risolto dall'hold), regole
--   citizen in  -> preferenze e consensi del caso, solo campi operativi
--   out         -> solo episodio: un messaggio con più offerte non diventa un fatto ambiguo
create or replace function public.observe(
  p_agent text, p_direction text, p_peer text,
  p_msg_id text default null, p_need_id text default null, p_citizen text default null,
  p_act text default '', p_fields jsonb default '{}'::jsonb, p_body text default null,
  p_latency_ms integer default null, p_error text default null, p_version text default null,
  p_at timestamptz default null
) returns text
language plpgsql security definer set search_path = '' as $$
declare
  personal constant text[] := array['address', 'age', 'disability', 'occupation', 'car',
    'car_euro_class', 'tax_code', 'name', 'phone', 'email'];
  citizen_keys constant text[] := array['symptom_code', 'is_emergency', 'prescription_number',
    'lang', 'area.district', 'days', 'gender', 'priority', 'procedure', 'offer_id', 'slot_id',
    'booking_id', 'ledger_ref', 'consent_ref', 'purpose'];
  need_keys constant text[] := array['symptom_code', 'is_emergency', 'prescription_number',
    'lang', 'area.district', 'days', 'gender'];
  listing constant text[] := array['world', 'catalogue', 'help', 'history', 'recall', 'agenda',
    'remembered'];
  kinds constant text[][] := array[['slot_id', 'slot'], ['hold_id', 'hold'],
    ['booking_id', 'booking'], ['prescription_number', 'prescription']];
  stages constant text[] := array['held', 'committed', 'checked_in', 'reported'];
  f jsonb;
  k text;
  slot text;
  state jsonb;
  pair text[];
  touched integer := 0;
  sent timestamptz;
begin
  -- sent_at arriva dentro p_fields (tools.py): la firma non cambia, così un pod vecchio funziona
  -- con questo schema e uno nuovo con lo schema di prima. Esce da fields e quindi dai fatti.
  begin
    sent := (p_fields ->> 'sent_at')::timestamptz;
  exception when others then
    sent := null;
  end;
  -- Solo campi scalari, mai dati personali; dal cittadino solo i campi del percorso.
  select coalesce(jsonb_object_agg(e.key, e.value), '{}'::jsonb) into f
    from jsonb_each(coalesce(p_fields, '{}'::jsonb)) e
   where e.key <> all(personal) and e.key <> 'sent_at'
     and (p_direction = 'out' or p_peer not like 'citizen:%' or e.key = any(citizen_keys));

  insert into public.episodes
    (msg_id, at, agent, version, direction, peer, need_id, citizen, act, fields, body,
     latency_ms, error, sent_at)
  values
    (p_msg_id, coalesce(p_at, now()), p_agent, p_version, p_direction, p_peer, p_need_id,
     p_citizen, coalesce(p_act, ''), f,
     case when p_direction = 'out' then left(p_body, 2000) end, p_latency_ms, p_error, sent)
  on conflict (msg_id, agent) do nothing;
  if not found then
    return 'duplicate';
  end if;
  if p_direction <> 'in' then
    return 'inserted';
  end if;
  -- I fatti di una riga si ricavano una volta: un secondo agente che la legge dopo non riporta
  -- indietro lo stato (regole, slot riassegnati).
  if exists (select 1 from public.episodes x where x.msg_id = p_msg_id and x.agent <> p_agent) then
    return 'inserted';
  end if;

  if p_peer = 'city:ledger' and p_act = 'rules' and f <> '{}'::jsonb then
    perform public._assert('city:rules', 'current', f, 'ledger', p_agent, null, null, p_msg_id, null);
  elsif p_peer = 'city:ledger' and coalesce(p_act, '') <> '' and p_act <> all(listing) then
    -- Lo slot di un hold o di una prenotazione, se la riga non lo ripete.
    slot := case when jsonb_typeof(f -> 'slot_id') = 'string' then f ->> 'slot_id' end;
    if slot is null then
      select x.value ->> 'slot_id' into slot from public.facts x
       where x.valid_to is null and x.predicate = 'status' and x.value ? 'slot_id'
         and x.subject in ('hold:' || coalesce(f ->> 'hold_id', ''),
                           'hold:' || coalesce(f ->> 'booking_id', ''),
                           'booking:' || coalesce(f ->> 'booking_id', ''))
       limit 1;
    end if;
    state := jsonb_build_object('act', p_act) || f;
    if slot is not null then
      state := state || jsonb_build_object('slot_id', slot);
    end if;
    foreach pair slice 1 in array kinds loop
      k := case when pair[1] = 'slot_id' then slot
                when jsonb_typeof(f -> pair[1]) = 'string' then f ->> pair[1]
                -- booking_id è l'id dell'hold: il committed chiude anche l'hold
                when pair[1] = 'hold_id' and jsonb_typeof(f -> 'booking_id') = 'string'
                     and exists (select 1 from public.facts x
                                  where x.subject = 'hold:' || (f ->> 'booking_id')
                                    and x.valid_to is null)
                  then f ->> 'booking_id' end;
      if k is null then
        continue;
      end if;
      touched := touched + 1;
      -- Una riga più vecchia letta in ritardo non riporta indietro lo stato (committed -> held).
      -- Stesso lock di _assert, preso prima del controllo: un commit in volo non si perde.
      perform pg_advisory_xact_lock(hashtext(pair[2] || ':' || k || '|status'));
      if coalesce(array_position(stages, p_act), 0) > 0 and exists (
           select 1 from public.facts x
            where x.subject = pair[2] || ':' || k and x.predicate = 'status'
              and x.valid_to is null and x.source = 'ledger'
              and coalesce(array_position(stages, x.value ->> 'act'), 0)
                  > array_position(stages, p_act)) then
        continue;
      end if;
      perform public._assert(pair[2] || ':' || k, 'status', state, 'ledger', p_agent,
                             p_need_id, p_citizen, p_msg_id, null);
    end loop;
    if touched = 0 and p_need_id is not null then
      perform public._assert('need:' || p_need_id, 'ledger:' || p_act, state, 'ledger', p_agent,
                             p_need_id, p_citizen, p_msg_id, null);
    end if;
  elsif p_peer like 'citizen:%' and p_need_id is not null then
    foreach k in array need_keys loop
      if jsonb_typeof(f -> k) = 'string' then
        perform public._assert('need:' || p_need_id, k, f -> k, 'citizen', p_agent, p_need_id,
                               p_citizen, p_msg_id, null);
      end if;
    end loop;
    if jsonb_typeof(f -> 'consent_ref') = 'string' then
      perform public._assert('need:' || p_need_id,
                             'consent:' || coalesce(f ->> 'purpose', 'unspecified'),
                             f -> 'consent_ref', 'citizen', p_agent, p_need_id, p_citizen,
                             p_msg_id, null);
    end if;
  end if;
  return 'inserted';
end $$;

-- Tutto ciò che la squadra sa di un caso, in una chiamata:
-- {facts: {subject: {predicate: {value, source, agent, since, expires_at}}},
--  timeline: [ultimi messaggi del caso, di tutti i nostri agenti], lessons: [...]}
create or replace function public.context(
  p_need_id text, p_agent text, p_citizen text default null, p_limit integer default 40
) returns jsonb
language sql stable security definer set search_path = '' as $$
  with scoped as (
    select f.* from public.facts f
     where f.valid_to is null
       and (f.expires_at is null or f.expires_at > now())
       and (f.need_id = p_need_id
            or (p_citizen is not null and f.citizen = p_citizen)
            or f.subject like 'city:%')
  ),
  by_subject as (
    select s.subject,
           jsonb_object_agg(s.predicate, jsonb_build_object(
             'value', s.value, 'source', s.source, 'agent', s.agent,
             'since', s.valid_from, 'expires_at', s.expires_at)) as preds
      from scoped s group by s.subject
  ),
  recent as (
    select e.at, e.agent, e.direction, e.peer_kind, e.act, e.fields, e.error
      from public.episodes e
     where e.need_id = p_need_id or (p_citizen is not null and e.citizen = p_citizen)
     order by e.at desc limit p_limit
  )
  select jsonb_build_object(
    'facts', coalesce((select jsonb_object_agg(b.subject, b.preds) from by_subject b), '{}'::jsonb),
    'timeline', coalesce((select jsonb_agg(r order by r.at) from recent r), '[]'::jsonb),
    'lessons', coalesce((select jsonb_agg(jsonb_build_object('trigger', l.trigger, 'lesson', l.lesson)
                                          order by l.created_at)
                           from public.lessons l
                          where l.status = 'active' and l.role in (p_agent, '*')), '[]'::jsonb))
$$;

-- --- permessi ----------------------------------------------------------------------------------
-- La publishable key (ruolo anon) non legge né scrive le tabelle: passa solo da observe,
-- assert_fact e context. Lezioni e outcomes si scrivono dal laptop con la secret key.

alter table public.episodes enable row level security;
alter table public.facts enable row level security;
alter table public.lessons enable row level security;
alter table public.outcomes enable row level security;
alter table public.levels enable row level security;
revoke all on public.episodes, public.facts, public.lessons, public.outcomes, public.levels,
  public.eval_rows from anon, authenticated;
revoke all on function public._assert(text, text, jsonb, text, text, text, text, text, timestamptz)
  from public, anon, authenticated;
revoke all on function public.source_rank(text) from public, anon, authenticated;
revoke all on function public.observe(text, text, text, text, text, text, text, jsonb, text, integer, text, text, timestamptz)
  from public, authenticated;
revoke all on function public.assert_fact(text, text, jsonb, text, text, text, text, text, integer)
  from public, authenticated;
revoke all on function public.context(text, text, text, integer) from public, authenticated;
grant execute on function public.observe(text, text, text, text, text, text, text, jsonb, text, integer, text, text, timestamptz)
  to anon, service_role;
grant execute on function public.assert_fact(text, text, jsonb, text, text, text, text, text, integer)
  to anon, service_role;
grant execute on function public.context(text, text, text, integer) to anon, service_role;

notify pgrst, 'reload schema';
