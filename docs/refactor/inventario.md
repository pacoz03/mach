# Inventario per il refactoring

> 2026-11-21 05:05, HEAD `c07fcd4`. Mappa per gli step di `docs/refactor/piano.md`.
> Sigle: **B** = `booking_centre/agent/agent.py`, **F** = `shared/facility.py`, **G** = `gp/agent/agent.py`, **T** = `shared/tools.py`, **K** = `mach-hackathon-kit/templates/examples/agent.py`, **SDK** = `mach-hackathon-kit/city-sdk/src/city_sdk/city.py`.
> Chiamate = call site per file (AST, definizione esclusa). «Da verificare» = non provato.

## 1. Helper duplicati

### I 13 del piano

| Helper | Definizioni | Chiamate B/F | Differenze | Verso `shared/protocol.py` |
|---|---|---|---|---|
| `words` | B:1869, F:337, K:190 | 13/24 | identiche: minuscolo, strip `.,;:!?()"'`. Serve a riconoscere l'atto come **parola ovunque nel testo** (`"confirm" in words(text)` in `_desk` B:409, F:928): né prima né ultima | sposta così |
| `first_word` | B:1873, F:341, K:194 | 19/65 | identiche. Su `a=b ...` restituisce `"a=b"` (T:75 `parse` dà `""`). È l'unico modo di leggere le teste del Ledger (`slots`, `agenda`, `held`, `refused`, `committed`, `prescribed`, `checked_in`), che **non** sono in `_ACTS` dell'SDK | `act(msg)`: prima parola se è un atto o una testa del Ledger, altrimenti `understood().type` |
| `fields_of` | B:1878, F:346, K:199 | 32/60 | identiche: **vince il primo valore** di una chiave ripetuta, toglie `.,;:!?` in coda, salta i valori vuoti, spezza sulle virgole. `understood()` invece: vince l'ultimo, niente strip (`slot_id=S-1.` resta `S-1.`), tiene `x=` vuoto, `true/false` diventano bool. T:75 `parse`: chiave ripetuta → lista, strip anche `()"'` | tieni la semantica di `fields_of`. «Primo vince» regge i messaggi con più offerte: non sostituire con `understood` |
| `_say` | B:1980 (→ `msg_id` o `""`), F:440 (→ bool), K:247 | 27/69 | **B** cattura ogni `Exception`, non ritenta, `tools.said(error=code o nome)`, e su un cittadino azzera `_SENT[need].pending`. **F** cattura `CapError`. Su un'altra eccezione: l'ospedale ritenta una volta, **specialista e consultorio la rilanciano** (timeout fuori dal brain, riconsegna, doppio invio: `harness.py:455-480`). Ospedale e cittadino con `TARGET_NOT_VISIBLE`: niente invii per 60 s (`_BOUNCED`). `city.say` diretti: B:359, B:1053 (solo `CapError`), B:1439 (ogni `Exception`), F:276. **G non ha `_say`**: 10 `city.say` diretti (G:341, 396, 410, 780, 782, 859, 865, 939, 963, 1025), nessuno passa da `tools.said` | variante B: `say() -> msg_id \| ""`. Il reset di `pending` resta nel booking come hook. `_BOUNCED` diventa un parametro. Togliere il retry dell'ospedale è un cambio di comportamento da dichiarare |
| `_until` | B:2189, F:482, K:263 | 8/9 | **B**: si ferma al primo dei due limiti, `tries` e `deadline`; a ogni lettura chiama `_keep_pages(fresh)`, che scrive `_PAGES`. **F**: con `deadline`, `tries` viene **ignorato** (F:501). `_cross_asst` F:2022 passa `tries=_SHORT_TRIES` e la `deadline`, e conta solo la `deadline`. Tutte: `CapError` → `chat`; le altre eccezioni escono | semantica B con callback `on_read` facoltativa; poi ricontrollare F:2022, 2038, 2149 |
| `_when` | B:1919, F:383, K:211 | 16/20 | stessa semantica: `None` se non è una data o se è senza fuso | SDK: `agenda_time` solleva `CapError` sul vuoto e sul naive (provato), quindi `_when = try agenda_time except CapError: None` |
| `_ledger` | B:1998, F:616, K:492 | 3/11 | tutte prendono l'**ultima** riga del Ledger con quella testa **nell'ordine della history**: né `sent_at` né abbinamento alla domanda (causa del bug delle 02:25). B: per `agenda` copia `agenda_date` in `agenda_now`. **F ha un effetto collaterale**: con `head="agenda"` aggiorna `_CLOCK` e chiama `tools.note("city:clock")` | funzione pura `ledger_answer(chat, ask)` abbinata con `sent_at`; l'aggiornamento dell'orologio va in `clock.py`, chiamato apertamente. Oggi si appoggiano all'effetto: F:289, 328, 974, 1023, 1838, 1990 e `_has_agenda` F:545 |
| `_we` | B:1965, F:406, K:234 | 11/21 | identiche. Il token deve essere esatto in `text.split()` (con la punteggiatura attaccata non lo trova); la parola d'atto può stare ovunque | sposta così |
| `_exact` | B:1976, F:415, K:243 | 10/4 | identiche: qualsiasi destinatario, `text.strip()`. G:1045 `_sent(history, agent, recipient, text)` fa lo stesso con il destinatario e senza strip | sposta; quella di G diventa `exact(..., to=)` |
| `_citizen` | B:1948, F:393, K:221 | 5/11 | F = K. B, prima della scansione generica, guarda `citizen=` nel passaggio del gp (`_fields`, quindi anche nel body). T:100 `_citizen_of` fa lo stesso su una riga | variante B. Da verificare su F: le strutture non ricevono il passaggio del gp |
| `_page` | B:2216, F:704, K:508 | 3/7 | identiche: `page=` dalla prima riga, default `(1,1)` | sposta |
| `_offer_line` | B:1847, F:833, K:586 | 1/4 | F = K: `procedure`, `at` e `day` obbligatori (altrimenti `KeyError`). B: `procedure`, `agenda_date` e `agenda_day` facoltativi; `at` resta obbligatorio per `expiry` | variante B |
| `_asked` | B:2224, F:712, K:516 | 3/2 | identiche: qualsiasi nostro messaggio con `page=N` e `procedure=X`, non solo quelli al Ledger | sposta |

### Altri doppioni (stesso nome o stesso compito in 2 o più file)

| Nome | Dove | Differenze e verdetto |
|---|---|---|
| `_slot`, `_IT_DAYS` | B:1893/1890, F:361/358 | identici: spostare |
| `_fields` | B:1907, F:375 | B unisce anche `body["provided"]`; F solo il body. Variante B |
| `_number` | B:2319, F:759 | B: `_fields`, scarta i numeri con spazi, regex `_GP_RX` nella prosa, `None` se ambiguo, passaggio del gp. F: solo il token nel testo dell'ultimo cittadino. Unirle cambia F: da verificare |
| `_lang` | B:2338, F:768, G:1000 | B: token, poi `_heard` (LLM), poi passaggio del gp. F: token (anche dal body). **G ha un altro compito** (lingua di risposta it/en). In comune c'è solo il passo del token |
| `_gender` | B:2382, F:822 | B aggiunge il testo libero (dal messaggio più recente) |
| `_offered`, `_withdrawn`, `_our_offer` | B:2234/2253/2273 (con `_offers_in` B:2282); F:733/737/852 (con `_our_offers` F:721) | B divide le offerte sui token `offer_id=` di un messaggio che contiene la parola `offer`. F prende le righe che iniziano con `offer`. F `_withdrawn` toglie anche gli slot dei `refused ... slot_id=` del Ledger, B no. K legge solo la prima offerta. Per i nostri messaggi danno lo stesso risultato; per i formati dei partner da verificare |
| `_has_agenda` | B:2120, F:544 | identica, ma in F fa partire l'effetto su `_CLOCK` |
| `_speaks` | B:1729, F:554 | B: `"*"` + `_RARE_LANG`; F: `sq` scritto. Oggi stesso risultato |
| `fitting` | B:1791, F:559, K:344 | **Non si uniscono senza parametri.** B: `days`, `reserve`, `relaxed`, `born`; scadenza dalla nascita del caso; `_lead()` dinamico; esige `facility`; lingua e genere solo come ordinamento (S_pref). F: filtro rigido su lingua, genere e giorni del medico; riserva U sempre esclusa; `_LEAD` 4 h; scadenza da adesso |
| `_spread` | B:1738, F:2203 | **stesso nome, compito diverso**: B sparpaglia giorni e medici, F evita le corse tra partner (cross-ASST). Rinominare |
| `_Gate`, `_Sent`, `_sent`, `_GATES`, `_SENT` | B:466-519, F:856-898, K:613-654 | B: `_Sent` ha 9 campi e `_Gate.wake`. F: `_Sent.holds` e il context manager `_gated()` F:902. **G:1045 `_sent` ha un altro compito** (vedi `_exact`) |
| `_answered` | B:2243 `(chat, agent, message)`, F:2349 `(chat, agent, msg_id, to)` | stesso compito, firme diverse |
| `_tell` | B:393, G:954 | compiti diversi (B: una volta, poi rinvio; G: `localize`). Non unire |
| `red_flag`, `_PAIN`, `_CHEST`, `_NEAR`, `_RED_FLAG`, `_NEGATED` | B:287-334, F:90-180, G:42-79 e 485 | pattern compilati **identici** (verificato). `ESCALATION` diverso: B solo italiano; F it/en da `lang=`; G dizionario it/en. Innesco: B `understood().body["is_emergency"] is True`, F `fields_of(...)=="true"`, G `understood` più dedup; **F rimanda sempre** (niente dedup, F:196). Candidato per `shared/redflag.py` |
| `_asks_address`, `_own_street` | B:2768/2794, F:200/226, K:93/119 | equivalenti; F `_own_street` legge anche `door.txt` accanto al brain (un file nel container) |
| `_WORRIED` | B:1534, F:3439 | regex diverse |
| `_DAY_WORDS` / `_DAY_NAMES` | B:1544, F:781 | stesse tuple |
| cache delle pagine di `world` | B `_world_page` 2008, `_keep_pages` 2022, `_fresh_page` 2043; F `_latest_slots` 690, `_keep_world` 2193, `_keep_world_reply` 2179 | stesso riconoscimento (testa `slots`, oppure `world` con slot). Dal vivo la testa è sempre `slots` (3796 righe in `episodes`). B: ultima risposta per pagina N, la più recente per `sent_at`. F: ultima di qualunque pagina, vince chi scrive per ultimo. Andrà in `world.py` |
| risposta del Ledger sulla ricetta | B `_prescription` 2125, G `_ledger_rx` 822, F `_ledger(chat,"prescription")` | B: un `refused` senza numero vale per **qualunque** numero. G: il numero deve coincidere, `{}` per il refused. F: nessun filtro sul numero. Tre semantiche |
| lettura con l'LLM | B `_read_with_model` 2704 (3 s), G 457 (15 s) | prompt diversi: in comune solo il wrapper |
| `_background` | F:2163, T:143 | compiti diversi |
| costanti | `_PULL_TRIES`, `_PULL_PAUSE`, `_SENT_NEEDS`, `_MAX_PAGE` uguali (B:41/84-86, F:32/52-53/81); **`_LEAD` B 1 h, F 4 h** | |
| `_latest`, `_await_line`, `_has_procedure` | solo F (e K) | spostabili in `protocol` |

**Cosa dà già l'SDK** (si cancella invece di spostare):
- `LEDGER`, `clinical`, `OutOfScope`, `Need.is_emergency`: già usati.
- `is_citizen()`: sostituisce `.startswith("citizen:")` (B 28 volte, F 32, G 4).
- `agenda_time`: il cuore di `_when`.
- `MAX_TEXT = 2000`: B `_SAY_MAX = 1900`.
- `ROLES`, `FACILITY_ROLES`.
- `role_for(service)` va da servizio a ruolo: non sostituisce `_SERVICES` (da procedura a servizio).
- **`understood()` non sostituisce `fields_of` né `first_word`**: legge l'ultima parola d'atto tra le sue `_ACTS`, dove mancano le teste del Ledger. `refused hold code=X` diventa `type="hold"` (provato).

**Nomi definiti due volte nello stesso file:** `_ARRIVED` F:1776 (regex ricca dell'ospedale) e F:3556 (specialista). Vince la seconda, quindi `_arrival` (F:2323, ospedale) usa oggi la regex dello specialista, e quella di F:1776 è morta. Se lo step 4 separa i rami, l'ospedale cambia comportamento senza che nessuno se ne accorga.

## 2. Stato mutabile a livello di modulo

Categorie dello step 5: **a** = ricavabile da `city.history(need_id=…)`, **b** = cache dichiarata, **c** = fatto su Supabase. Colonna **R/2P**: si rompe con un riavvio del pod a metà caso (R) o con due pod (2P).

| Var | File:riga | Contiene | Scrive | Legge | Limite oggi | Cat. proposta | R/2P |
|---|---|---|---|---|---|---|---|
| `_CLOCK` | B:49 | ultima risposta `agenda` di qualunque caso, più `sent_at` | `_read_clock` (da `_now`, `_born`) | `_agenda_at`, `_rate` | nessuno, sostituito dal più nuovo | c `city:clock` + copia b | **R**: niente offerta rapida finché il caso non ha una sua `agenda` (~5 min) |
| `_RTT` | B:53 (`global` B:2100) | ultimo giro del Ledger (s, max 600) | `_round_trip` | `_lead` | nessuno | a (per caso) o b | R: `_lead` torna a 1 h |
| `_OFFERED` | B:58 | slot → (cittadino, monotonic) | `_offer`, `_citizen_cancel` (del) | `_shared` → `_spread`, `_fresh_first` | 900 s, potato in `_offer` | c (`slot:<id>`) o b dichiarata per pod | **2P**: stesso slot a più cittadini |
| `_PAGES` | B:66 | (procedura, pagina) → (monotonic, sent_at, testo) | `_keep_pages` (da `main`, da ogni `_until`) | `_fresh_page` | fresco 240 s in lettura, mai rimosso (piccolo) | b, TTL 240 s, ~4×8 chiavi; si ricostruisce con `world` | R: cold start più lento |
| `_GONE` | B:70 | slot persi o prenotati: mai più offerti | `_on_confirm`, `_partner_cancel` | `_advance_alone`, `_partner_answer` | **nessuno: cresce per sempre** | c (`slot:<id> status`, già ricavato da `observe`) o b con TTL | R/2P: slot già presi riofferti, poi refused |
| `_DOWN` | B:92 | struttura → monotonic del rifiuto | `_on_confirm`, `_ask_partners` | `_down` | 600 s in lettura, mai rimosso | b, TTL 600 s | lieve |
| `_RULES` | B:95 (`global` B:2166) | regole P1-P3 dall'ultima risposta `rules` di qualunque caso | `_rules` | `_rules` | nessuno | a + c `city:rules` (già ricavato da `observe`) | R: `None`, gestito (prudente) |
| `_GATES` | B:507 | need → `_Gate` (lock, turni, `wake`) | `_advance` | `_advance` | si pulisce da solo | lock per processo | **2P**: nessuna esclusione |
| `_SENT` | B:508 | need → `_Sent`: `offers`, `cancelled`, `said`, `trigger`, `again`, `mark`, `before`, `pending`, `reread` | `_advance_alone`, `_offer`, `_say`, `_forward_receipt`, `_partner_cancel`, `_citizen_cancel` | gli stessi | LRU 4096 | a per `offers`, `said`, `cancelled`, `mark`; b (read-your-writes, TTL ~15 min) per il ritardo della history; `reread` è locale al turno | **R: `pending` perso = silenzio** (0189, 0191) |
| `_HEARD` | B:2409 | msg_id → lettura LLM (giorni, genere, lingua) | `_hear` | `_heard` → `_lang`, `_days`, `_gender` | LRU 4096 | b (si ricostruisce rifacendo la chiamata da 3 s) o c | R: la lingua del caso può tornare `it` |
| `_WORLD` | F:71 | (procedura, pagina) → (monotonic, testo) | `_keep_world`, `_on_refused` (toglie gli slot `NO_CAPACITY`, F:3224) | `_cross_asst.warm`, `_alternatives` | fresco 300 s in lettura, mai rimosso | b, TTL 300 s | R: risposte cross-ASST a freddo (4 s) |
| `_OFFERED` | F:78 | slot → [monotonic] (offerte cross-ASST) | `_cross_asst`, `_spread` (potatura) | `_spread` | 60 s | b | 2P: corse tra partner |
| `_HELD` | F:79 | slot → monotonic dell'hold | `_confirm_alone`, `_unhold`, `_spread` | `_spread` | 600 s | c (`slot:<id>`) o b | 2P |
| `_BOUNCED` | F:428 | (need, a, testo) → monotonic | `_say` | `_say` | 60 s, potato in scrittura | b | no |
| `_CLOCK` | F:637 | ultima `agenda` più recente | `_ledger` (effetto), `_recall_clock` | `_city_now`, `handle` | nessuno | c `city:clock` (scritto da `_ledger`, TTL 1800) + b | R: coperto dal recall |
| `_NOTED` | F:647 | ultima àncora scritta su Supabase e quando | `_ledger` | `_ledger` | 300 s | b | no |
| `_RECALLED` | F:648 (`global` F:263) | recall già tentato | `handle` | `handle` | **una volta per processo, anche se fallisce** | b | R: recall fallito, freddo fino alla prima `agenda` |
| `_GATES` | F:886 | chiave → `_Gate`; chiavi: need, `confirm:`, `report:`, `book:`, `cross:`, `arrive:`, `refused:`, `welcome:` | `_gated` | `_gated` | si pulisce da solo | lock | **2P**: commit e ricevute doppi (lo dice F:1870) |
| `_SENT` | F:887 | need → `offers`, `trigger`, `again`, `holds`; chiave `cross:` = richiesta già risposta | `_advance_alone`, `_confirm_alone`, `_cross_asst` | gli stessi | LRU 4096 | a + b | R: un `confirm` duplicato può rifare l'hold (non ospedale) |
| `_TASKS` | F:2160 | task in background | `_background` | (tiene vivi i task) | si pulisce da solo | non è stato | no |
| `CASE_LOCKS` | G:137 | need → `asyncio.Lock` (`WeakValueDictionary`) | `_case_lock` | `_case_lock` | si pulisce da solo | lock | 2P |
| `_PENDING` | T:46 | scritture Supabase in volo | `_background` | `flush` | si pulisce da solo | non è stato | no |
| `_WRITES` | T:47 (`global` T:136) | lock pigro: una scrittura Supabase alla volta | `_write` | `_write` | — | lock | coda sotto carico (3 s per scrittura) |
| `MEMORY_ON` | T:40 (`global` T:477) | interruttore della memoria | `_smoke` (solo test) | `_rpc` | — | costante | no |

Non sono stato condiviso: le `ContextVar` `_HOSPITAL`, `_BOOK_BY` (F:423, 437), `_HEARD`, `_LANG` (G:23, 25), `_TURN` (T:49), e `tools.me` (`lru_cache`).

## 3. Dati del mondo scritti nel codice

| Tabella | Dove | Differenze tra le copie | Il Ledger la dà? |
|---|---|---|---|
| `_DOCTORS` (medico → lingue, genere, giorni) | B:74, F:38, K:29 | uguali tranne h3: B `{"it","*"}` + `_RARE_LANG="sq"` (B:1726), F `{"it","sq"}` + `_RARE="sq"` (F:47) | e19-rules R:78, R:166 e `SKILL.md:44`: `world` contiene «slot, **medici**, catalogo, protocollo». **Dal vivo no**: in 24 h, 3796 risposte `slots` hanno solo `asst, at, day, doctor_id, facility, free, from, matching, page, procedure, slot_id, until, urgent_reserve, wave_id`, e nessuna risposta del Ledger ha chiavi su lingua o genere. Da verificare come si ottiene il registry (`world` senza `procedure=`? `catalogue`? `help`?) |
| `_DEADLINE_DAYS` | B:83, F:48, K:55 | uguali `{"B":2,"D":5}`, default 14. U = fine giornata: B da `born`, F da `now` | no (sta nelle regole R:91) |
| `PROTOCOL` (sintomo → procedura, priorità) | G:28 | unica | R:78: righe di protocollo in `world`; mai viste dal vivo |
| id dei nostri agenti | B:1160 `_OUR_FACILITIES`, B:1927 `_GP`; F:1800 `_OUR_GP`, F:1802 `_OUR_BOOKING`; G:141 `BOOKING_CENTRE` | stessi id, 5 copie | no (dalle card) |
| `_WHEELCHAIR` | B:1162 | unica | registry (console), non nel Ledger |
| `_SERVICES` (procedura → servizio) | B:1163 | unica; F ha `_PROCEDURE`/`_CARDIOLOGY`/`_CONTROL` (F:1805, 1807, 86) | no |
| `_RARE_BY_ASST`, `_KNOWN_PARTNERS` | B:1171, B:1180 | unici (da Discord) | no |
| nomi delle procedure | B:1538 `_PROCEDURES`, G `_VISIT` | testi diversi | — |
| giorni | `_IT_DAYS` B:1890 = F:358; `_WEEKDAYS` B:2335; `_FAMILY_DAYS` F:1314; `_DAY_WORDS` B:1544 = `_DAY_NAMES` F:781; `_DAY_NAMES` G:537 (regex) | | il Ledger dà `day` per slot |
| ruoli | T:320 `WHO` | unica | no |

## 4. Orologio di agenda

| | B `_agenda_at(real)` B:2072 (via `_now` B:2048, `_born` B:1762) | F `_city_now()` F:662 |
|---|---|---|
| Formula | `monday + weeks·7g + rest`, con `(weeks, rest) = divmod(min(base − monday, 5g) + (real − start)·rate, 5g)` | identica, con `real = now` |
| Salto del weekend | sì (stessa aritmetica; una `base` nel weekend scivola a lunedì) | sì |
| Àncora | `epoch_agenda`/`epoch_real` se ci sono; altrimenti `agenda_now` + `sent_at` della riga | solo `epoch_*`, altrimenti `None` |
| `rate` mancante o invalido | 60 | `None` |
| `paused=true` | restituisce `agenda_now` così com'è (vecchio) | `None` |
| Da dove arriva | `_read_clock` scandisce le righe `agenda` del caso e tiene la più alta per `agenda_now`; solo processo, niente Supabase | effetto di `_ledger(chat,"agenda")` sull'**ultima** riga (non la più alta); scrive `city:clock` (F:632, ogni ≥300 s, TTL 1800); al primo turno di un pod `_recall_clock` F:651 la rilegge in background |
| Margine | `_lead()` = 1 h + RTT·rate (RTT max 600 s, quindi fino a +10 h); `_BORN_MARGIN` 1 h (B:1759) | `_LEAD` 4 h fisso |

Altri «adesso» in F:
- **`_advance_alone` F:975 e F:1024 usa `agenda_now` grezzo dell'ultima risposta del caso**, senza `_city_now`: è la lettura vecchia che il booking ha corretto (percorso 0099).
- `_agenda_now` F:2258 chiede una `agenda` nuova e aspetta ~3 s (`_refused_alone`, `_answer`).
- `_day_after(world)` F:679 è il ripiego senza orologio.
- `_cross_asst.clock` F:1989 usa `_city_now()`, altrimenti la risposta nuova.

G non ha orologio. Dal vivo tutte le 2761 risposte `agenda` hanno `epoch_real`, `epoch_agenda`, `rate`, `paused` e `agenda_now`; anche il simulatore li manda (`tests/journey.py:41`).

**Proposta per `shared/clock.py`:**
- la formula di F, con il parametro `real` di B (serve a `_born`);
- l'àncora dalla risposta `agenda` più alta (come `_read_clock` di B), più il recall e la scrittura di `city:clock` di F;
- paused o campo mancante → `None`, mai `rate = 60` inventato;
- il margine resta un parametro del chiamante (B 1 h + RTT, F 4 h): unificarlo cambia le offerte.

## 5. Lock e attese

| Lock | Dove | Cosa si aspetta tenendolo |
|---|---|---|
| `_Gate.lock` (B, per need) | `_advance` B:538 | `_until` di una lettura B:541 e B:554; `_advance_alone`: `_until` 24×0,25 s + letture (ricetta B:629, `agenda` + pagina 1 B:696, pagine B:796); `tries=1` B:784; `say` al Ledger (`agenda`, `rules`, `world`, `reread` B:550); **`_try_partners` → `_ask_partners`: `city.find` B:1396, un `say` per partner B:1439, `_until` fino a 30 s B:1460** |
| nessun lock | `_on_confirm` B:944 | due `confirm` in parallelo si fermano solo su `_we` sulla history, che è in ritardo: possibile doppio `confirm` alla struttura (da verificare) |
| `_gated(need)` | F:953 `_advance` | `_until` F:1011, 1034, 1109 (24 tentativi ciascuno, fino a 3 giri, più uno per pagina) |
| `_gated("confirm:")` | F:1164 | `_finish_booking` F:3083 e 3099: due `_await_line` da 24×0,25 s. Scadenza `_BOOK_BY` (5 s) **solo per l'ospedale**; specialista e consultorio senza |
| `_gated("book:")`, ospedale | F:1871 | `city.history`, poi `_facility` → `confirm:` annidato → `_finish_booking`; `_release` F:2552 `_until` 24; `asyncio.gather(_ask_slots, booking)` |
| `_gated("report:")`, specialista | F:1695 | `city.history`, poi `confirm:` annidato → `_finish_booking`; `_on_checkin` F:3351, `_file_report` F:3705, `_prescribe_follow_up` F:3669: un `_await_line` ciascuno |
| `_gated("cross:<partner>:<rid>")` | F:1954 | `city.history` (consultorio: `wait_for` 1 s), `say` di `agenda` e `world`, `_until` con scadenza 4-5,5 s (F:2022), consultorio fino a 20 s (F:2038), `gather` delle letture dell'ospedale F:2136 |
| `_gated("arrive:")` | F:2606 | `city.history`, `say checkin`, `_await_line` F:2692 (24) |
| `_gated("refused:")` | F:2719 | `_agenda_now` F:2748 (`say` + 12 letture) |
| `_gated("welcome:")` | F:2872 | `city.history`, `_until` 12 F:2899, `_tell_gp` → `city.find` F:2977 |
| `CASE_LOCKS` (G, per need) | G:311, 322, 343, 368, 398, 407 | `city.history` (1-2 volte), `say prescribe`, `_handoff` (`city.find` + `say`), **`_tell` → `tools.localize`: LLM fino a 2 + 15 s** (G:963), più il `say` |
| `_WRITES` (T) | T:139 | una RPC Supabase alla volta (timeout 3 s), fuori dal turno |

Senza lock, ma dentro il turno:
- G `_read_with_model` G:457 (15 s) e `tools.language` (2 s) in `main`;
- B `_hear` → `_read_with_model` B:2732 (3 s);
- B `_partner_cancel` `_until(tries=8)` B:1129;
- F `_family` `held` → `_finish_booking` F:1502 (24 + 24 letture);
- F `handle` del consultorio: `wait_for(history, 1 s)` F:269.

**Nota per `HANDOFF.md`:** `_renew` del gp non rilegge più 24 volte: ora fa una sola `city.history` dopo il `say` (G:784, dalla 1.0.13).

**Probabile codice morto (da verificare):** il blocco `if owed:` dopo il lock in `_cross_asst` (F:2145-2155). Entrambi i rami `owed` dentro il lock fanno `return`.

## 6. Funzioni del percorso caldo sopra le ~80 righe

| File | Funzione | Righe | Lunghezza |
|---|---|---|---|
| B | `_advance_alone` | 564-839 | 276 |
| B | `_on_confirm` | 944-1066 | 123 |
| B | `_ask_partners` | 1382-1462 | 81 |
| F | `_cross_asst` | 1928-2156 | 229 |
| F | `_family` | 1459-1667 | 209 |
| F | `_advance_alone` | 957-1155 | 199 |
| F | `_hospital` | 1810-1911 | 102 |
| F | `_arrive_alone` | 2614-2712 | 99 |
| F | `_welcome` | 2861-2958 | 98 |
| F | `handle` | 242-331 | 90 |
| F | `_confirm_alone` | 1168-1256 | 89 |
| F | `_on_refused` | 3199-3282 | 84 |
| G | `main` | 301-399 | 99 |
| G | classe `Prescription` (testi, `reply()` ~55 righe) | 174-271 | 98 |

Vicine alla soglia: F `_refused_alone` 77, F `_on_reported` 72, F `_release` 63. In B c'è anche la tabella `_PHRASES` (B:99-286, ~190 righe di dati).
