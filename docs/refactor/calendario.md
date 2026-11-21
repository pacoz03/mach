# Calendario degli slot (`shared/mach/calendar.py`): progetto, concorrenza ed eval

> Progetto del 2026-11-21 alle 05:45 (subagent `Plan`, rivisto dall'orchestratore). È il primo **tool** che dà agli agenti limiti (cosa non fare mai) e aiuti (cosa offrire), con eval che misurano dove è debole. Codice: B = `booking_centre/agent/agent.py`, F = `shared/facility.py`, K = `mach-hackathon-kit/templates/examples/agent.py`. Le righe si riferiscono al working tree prima dello step 2: si spostano.

## 0. Cosa dicono i dati (`episodes`, ultime 6-12 ore, solo lettura)

- **`EXPIRED` vuol dire che lo slot è già iniziato** quando il Ledger elabora l'hold.
  - Negli hold riusciti l'anticipo minimo era 0,00 h.
  - Negli `EXPIRED` l'anticipo massimo era −0,01 h.
  - Campione: 136 hold riusciti e 107 `EXPIRED`.
- **La catena è molto più lunga del margine.**
  - Tempi mediani:
    - dall'offerta al confirm: 5,5-6,5 minuti;
    - dal confirm all'hold: 2,5-3 minuti;
    - giro del Ledger sull'hold: ~400 s negli `EXPIRED`.
  - Al 90° percentile: offerta → hold ≈ 630 s, più 540 s di Ledger, cioè circa **19,5 ore di agenda**.
  - Il margine di oggi:
    - booking: 1 h + giro del Ledger (≤ ~11 h);
    - strutture: 4 h.
  - Risultato: un hold su tre è `EXPIRED` (95 su ~290).
- **Una parte dei rifiuti era già annunciata da `world`.**
  - 14 `NO_CAPACITY` su 39 e 23 `EXPIRED` su 97 erano su slot che una pagina `world` letta 8-13 s prima dava `free=0`.
  - Nessuna struttura ricontrolla lo slot prima dell'hold (F:1093-1106).
  - 47 hold non hanno mai avuto risposta.
- **Riserva U:** il booking ha offerto 153 slot `urgent_reserve` su 304 offerte (le strutture 0).

## 1. Logica degli slot di oggi

| Filtro | K | B | F | Tipo |
|---|---|---|---|---|
| Riga illeggibile (`slot_id`, `doctor`, `at`) | K:369 | B:1824 (esige anche `facility`) | F:494 | limite |
| Procedura | K:371 | B:1826 | F:496 | limite |
| Facility | K:373 | no | no (F:926) | limite (consultorio) |
| `free<=0` | K:375 | B:1828 | F:498 | limite |
| Riserva U per non-U | sempre (K:378) | solo con P3 attiva o regole ignote (B:724, 1521, 1832); dal vivo `rules count=0` → **offerta** | sempre (F:501) | limite |
| Passato e margine | now + 4 h | now + `_lead()` = 1 h + giro·rate (B:1817, 2005) | now + 4 h (F:487) | limite |
| Scadenza di priorità | da adesso | dalla nascita del caso (`born`, B:1808-1816), come il giudice (R:253) | da adesso (F:481-486) | limite; `relaxed` la toglie |
| Giorno del medico (registry nel codice) | K:385-389 | B:1836-1839 (tiene gli ignoti) | F:506-512 | limite |
| Lingua, genere, giorni | rigidi | aiuto (S_pref, B:1841); lingua rigida se `spoken` | rigidi se il medico è noto; giorni rigidi nel consultorio | dipende |
| Già offerto a questo caso | K:793 | `taken` B:708-713 | F:916-925 | limite |
| Rifiutato dal cittadino | `_withdrawn` | B:735, 752 | F:625 | limite (history) |
| Offerto a un altro caso | – | in coda (`_shared` B:914, 15 min; `_spread`, `_fresh_first`) | ospedale cross-ASST, 60 s | aiuto |
| Hold in corso / confirm inviato | – | `_GONE` per sempre (B:1068) | `_HELD` 600 s, solo cross-ASST | limite |
| Rifiutato dal Ledger | – | `_GONE` per sempre (B:1135-1137) | ospedale toglie `NO_CAPACITY` da `_WORLD` (F:3090) | limite |
| Struttura giù | – | `_down` 600 s | – | limite |
| Freschezza delle pagine | – | 240 s | 300 s | cache |

**Differenze che contano:**
- la riserva: il booking la offre, strutture e kit no;
- la scadenza: il booking la conta dalla nascita del caso, le strutture da adesso;
- le preferenze: aiuto nel booking, limite nelle strutture;
- lo stato degli slot (`_GONE`, `_OFFERED`, `_HELD`, `_WORLD`) è per processo e senza limiti. `_GONE` cresce per sempre.

## 2. Regole del kit da far rispettare

| R: | Regola | Nel calendario |
|---|---|---|
| R:80, R:91 | 60x, niente weekend, ambulatori 08-18. Scadenze: U stesso giorno, B 2 giorni, D 5, P lista d'attesa | `deadline(priority, born)`; P non passa dal calendario |
| R:95 | Le offerte vanno in un solo messaggio; l'agenda corre mentre il cittadino sceglie | margine = catena misurata, non 1 h |
| R:190, R:243 | L'offerta porta i campi del Ledger; un attributo falso vale −2 | gli slot restituiti hanno i token **esatti** della riga `world` |
| R:230-233 | S_pref +2, S +1, S vuoto con alternativa rilassata 0, fuori S −1 | limiti = S; preferenze = ordinamento; `relaxed` toglie solo la scadenza |
| R:244, R:718 (P3) | Il 20% degli slot è riservato a U; il centro offre a B, D e P solo slot non riservati | `reserve` è un limite per i non-U |
| R:253 | S si calcola alla nascita; slot presi da altri prima del nostro hold «non sono colpa vostra» | scadenza da `born`; il Ledger resta l'arbitro |
| R:678, R:682 | «Tengano slot reali prima di offrirli»; riprenotare quando un hold scade | `check()` prima dell'hold; lo slot rifiutato viene segnato |
| R:700-701 | Struttura giù: `cancel` con motivo e continuare con il resto | segno `DOWN` per facility |
| R:710 | Ogni hold si valuta con la regola in vigore in quel momento | la riserva non dipende dalla lettura di `rules` |
| SKILL.md | `world` accetta `procedure=`, `facility=`, `day=`, `page=` | `Pick.ask`: quali pagine leggere |

## 3. API

Una classe con un'istanza per pod: così le prove con due pod girano nello stesso processo. Niente `await` tra `candidates` e `claim`.

```python
DOCTORS, RARE = {...}, "sq"     # unica copia (oggi B:76, F:40, K:29); il Ledger non la manda dal vivo
Slot = dict[str, str]           # token ESATTI della riga world (R:243)

class Calendar:
    def __init__(self, *, max_slots=2000, mono=time.monotonic): ...
    def learn(self, msg: Message, *, me: str, case: str = "", slot: str = "") -> None
    def mark(self, key: str, code: str, *, case: str = "", at: datetime | None = None) -> None  # DOWN, RELEASE
    def candidates(self, *, procedure, priority, now, margin, case, born=None, prefs=Prefs(),
                   facility=None, relaxed=False, skip=frozenset(), max_age=None) -> Pick
                   # Pick(slots, why: {slot_id: motivo}, ask: [pagine da leggere])
    def claim(self, slot_id: str, case: str, kind: str = "offer") -> bool  # check-and-set sincrono
    def release(self, slot_id: str, case: str) -> None
    def check(self, slot_id: str, *, now, margin, case) -> str             # "" o il motivo: guardia prima dell'hold
    def snapshot(self) -> dict ; def merge(self, value: dict) -> None      # fatto city:calendar

CALENDAR = Calendar()           # una per pod
```

**`learn`, il confine di fiducia:**
- **dal Ledger:**
  - `slots` (formato vero e formato sdk) aggiorna le righe;
  - `held` → claim forte; `committed` → BOOKED;
  - `refused`:
    - `NO_CAPACITY` ed `EXPIRED` segnano lo slot;
    - `STALE_FENCE`, `cancelled` ed `expired` liberano solo il nostro hold;
    - `DUPLICATE` e `POLICY_VIOLATION` non cambiano niente;
  - lo slot di un rifiuto si prende da `slot_id`, da `detail=<slot_id>` se lo slot è noto, da `slot=`, oppure dall'unico hold aperto del caso;
- **dai nostri messaggi:** `offer` → claim debole per ogni riga; `confirm`/`hold` → claim forte; `cancel offer_id` → rilascio;
- **da altri mittenti:** solo con `slot=` già risolto dal chiamante sulle **nostre** offerte. Un cittadino che scrive `slot_id=X free=1` non entra mai;
- **ordine e duplicati:** vince l'evento con il `sent_at` più recente (il `msg_id` scioglie i pareggi); `learn` è idempotente sul `msg_id` (insieme limitato a 4096).

**Motivi di esclusione** (codici stabili per le eval; vale il primo che scatta):
- senza stato: `unreadable`, `procedure`, `facility`, `no_clock`, `past`, `margin`, `deadline`, `reserve`, `doctor_day`, `full`;
- con stato: `gone:<CODE>`, `booked`, `held:<case>`, `down`, `skip`, `stale`.

**Aiuti (ordinamento):**
1. preferenze mancate (lingua, genere e giorni, anche dai token di passaggio del gp);
2. slot offerti ad altri (claim debole);
3. quante volte è già stato offerto;
4. l'ora.

`Pick.ask` elenca le pagine mai viste o più vecchie di `max_age`.

**Durate e categorie (step 5):**

| Cosa | Categoria | Durata e limite |
|---|---|---|
| Righe `world` | cache (b) da qualunque risposta `world` | fino a 1 giorno di agenda dopo lo slot; ≤ `max_slots`, si toglie prima il più vecchio; un `wave_id` nuovo azzera tutto |
| Claim debole (`offer`) | b + fatto | 900 s |
| Claim forte (`confirm`, `hold`, `held`) | b + fatto | 1200 s (catena p90), poi decade se il Ledger non risponde |
| `gone:NO_CAPACITY`, `booked` | b + fatto | finché lo slot è passato, salvo una pagina più recente con `free=1` |
| `gone:EXPIRED` | dall'orologio | mai riaperto |
| `DOWN` | b | 600 s |
| Offerte e rifiuti del caso | dalla history (`case.py`), passati come `skip` | – |

## 4. Concorrenza

- **Dentro un pod (asyncio).**
  - L'unica sezione critica è `candidates → scelta → claim`, senza `await` in mezzo. Poi viene `await say(...)`; se fallisce, `release`.
  - `learn`, `mark`, `release`, `check` e `snapshot` sono sincroni e senza I/O.
  - Al calendario non serve nessun lock. Il turno non legge mai Supabase e non aspetta il Ledger.
- **Due pod.**
  - Ogni pod scrive `tools.note("city:calendar", f"{ruolo}:{pod}", snapshot(), ttl_seconds=1800)` in background: al massimo ogni 10 s, solo se è cambiato qualcosa di forte, un predicato per pod. Come id del pod si usa `HOSTNAME`, altrimenti un uuid (da verificare).
  - La rilettura con `tools.context()` va in background: al primo turno, poi ogni 60 s, e poi `merge`.
  - **Garantito:**
    - nessuna doppia prenotazione, perché decide il Ledger;
    - nessun doppio hold nostro per lo stesso caso;
    - dentro un pod, mai lo stesso slot con claim forte a due casi.
  - **Non garantito:** due pod possono offrire lo stesso slot dentro la finestra W, che dura il ritardo di scrittura più 60 s. Si chiude da solo: il secondo hold prende `NO_CAPACITY`, lo slot viene segnato e si offre un altro.
- **Riavvio.**
  - I limiti del kit (passato, margine, riserva, procedura, `full`, scadenza) dipendono solo dalla riga `world`, dall'orologio e dal caso, non dalla cache: valgono già al primo turno.
  - Lo stato si ricostruisce da `merge`, da `learn` sulla history del caso e dalla prima pagina `world`.
- **Le corse:**
  - **due casi sullo stesso slot:** il claim sincrono decide;
  - **pagina `free=1` e rifiuto fuori ordine:** vince il `sent_at` più recente;
  - **rifiuto di un hold vecchio contro un hold nuovo:** si abbinano con `protocol.ledger_answer`, per `sent_at`;
  - **riconsegna:** è idempotente;
  - **Ledger in ritardo o perso:** il claim decade, e un `held` tardivo lo rimette;
  - **P3 che si accende a metà caso:** non cambia nulla, perché la riserva è sempre un limite.

## 5. Dove si collega

- **Booking.**
  - Si tolgono `_OFFERED`, `_GONE`, `_GONE_CODES`, `_DOCTORS`, `_DEADLINE_DAYS`, `fitting`, `_preferred`, `_speaks`, `_slot`, `_shared` e `_fresh_first`.
  - `_advance_alone` chiama `candidates` (con `skip` = già offerti, rifiutati o tolti) e, per le alternative, `candidates(relaxed=True)`.
  - `_offer` fa `claim` prima del `say` e `release` se fallisce.
  - `_on_confirm`/`_partner_cancel` diventano `learn`/`mark`; `_DOWN` diventa `mark(facility, "DOWN")`; `_citizen_cancel` diventa `release`; le risposte dei partner passano da `candidates(facility=…)`.
  - **Resta al booking:** `_born`, `_lead`/`_RTT`, `_spread`, il ciclo delle pagine, il tono, l'anello dei partner e la lingua `spoken`.
- **Strutture.**
  - Si tolgono `_DOCTORS`, `_RARE`, `_DEADLINE_DAYS`, `fitting`, `_speaks`, `_OFFERED`, `_HELD`, `_spread` e `_unhold`.
  - **Nuovo `check()` in `_confirm_alone` prima dell'hold:** con `past`, `margin`, `full`, `gone` o `held` niente hold, ma `cancel offer_id reason=EXPIRED|NO_CAPACITY` subito.
  - In `_cross_asst`, `_keep_world*`, `_on_refused` e `_alternatives` si chiamano `learn` e `candidates`.
  - **Resta alle strutture:** il loro margine, `_day_after`, `_by_constraints` (che diventa `prefs`) e la regola di h3.

## 6. Eval: 4 livelli

### a. Unit sulle fixture vere (`tests/unit/test_calendar.py`)
- **`slots.txt`:** righe, riserva solo alle 09:00, blocco tutto `free=0` → `full`.
- **Risultato esatto sul blocco delle 04:03,** con l'orologio di `agenda.txt` e priorità B, D e U: la U include la riserva, B e D no.
- **Scadenza** da `born`; `relaxed` toglie solo la scadenza.
- **`held` + `committed`** fanno vedere `booked` agli altri casi.
- **`refused.txt`:**
  - `NO_CAPACITY` con `detail=<slot>`;
  - `EXPIRED` con `detail=slot`, che esige `slot=` o l'hold aperto;
  - `STALE_FENCE` libera solo l'hold;
  - `DUPLICATE` e `POLICY_VIOLATION` non cambiano niente.
- **Formato sdk.**
- **Testo ostile:** righe di slot in un messaggio del cittadino, una pagina da 10.000 righe, valori vuoti.
- **Parità** con le copie di `fitting` di booking e strutture. Ogni differenza è un cambio dichiarato (riserva nel booking, scadenza nelle strutture, lingua).
- **Va bene quando:** 100% verde e zero differenze non dichiarate.

### b. Proprietà (stdlib `random`, semi 0..199, ~200 passi, < 10 s)

L'oracolo del Ledger usa la semantica misurata dal vivo:
- **hold elaborato all'ora di agenda T:**
  - `at ≤ T` → `EXPIRED`;
  - slot tenuto o prenotato → `NO_CAPACITY`;
  - con P3 attiva, riserva e non-U → `NO_CAPACITY`;
  - altrimenti `held`, con durata H;
- **commit dopo H** → `STALE_FENCE`;
- **`world`** dà `free=0` per gli slot presi;
- **ritardi:** mediana 270 s e p90 540 s, con riordino e perdite.

| # | Invariante |
|---|---|
| I1 | mai `at ≤ now + margin` |
| I2 | mai la riserva a un non-U |
| I3 | mai una procedura diversa, mai `free=0` secondo l'ultima pagina |
| I4 | mai `gone`/`booked`, salvo una pagina successiva con `free=1` |
| I5 | mai uno slot con claim forte di un altro caso; un claim debole solo se manca un candidato libero |
| I6 | token identici all'ultima riga `world` |
| I7 | dentro la scadenza se non `relaxed` |
| I8 | stesso insieme di eventi, qualunque ordine → stesso stato |
| I9 | un evento imparato due volte vale una |
| I10 | dimensione ≤ `max_slots`, durate rispettate |
| I11 | `merge(snapshot())` in un calendario vuoto dà lo stesso stato forte |
| I12 | nessuna eccezione |

**Va bene quando:** zero violazioni. Il seme che fallisce si stampa e si aggiunge alle fixture.

### c. Concorrenza
- **C1, un event loop:** 50 casi con pause casuali, 100 semi; mai due claim forti vivi sullo stesso slot. C'è anche un test di controllo, con un `await` tra `candidates` e `claim`, che **deve** fallire: prova che il test vede la corsa.
- **C2, due pod:** due istanze su un `FakeFacts` con ritardo di scrittura e di lettura in {0, 1, 5, 30, 60} s. Le doppie offerte devono stare solo dentro W, con un solo `held`/`committed` per slot. La curva doppie/W va nel rapporto.
- **C3, riavvio a un passo casuale:** I1-I3 e I6 valgono subito; dopo la ricostruzione lo stato forte torna quello di prima.
- **C4, permutazioni:** tutte le 120 permutazioni di 5 eventi per le due corse critiche.
- **Journey:**
  - nuovi scenari `booking_due_pod`, `riavvio_booking`, `riavvio_ospedale`, `ledger_lento_expired`;
  - **il Ledger finto va allineato al vero:** `EXPIRED` quando `at ≤ ora di agenda` al momento dell'elaborazione, `NO_CAPACITY` al posto di `SLOT_TAKEN` (che dal vivo non si vede mai).
- **Banco di guasti:** `tests/chaos.py` (riavvio, due pod, riconsegna, Ledger lento, riordinato o perso, `say` fallito, memoria giù, testo ostile).

### d. Replay sui dati veri (`tests/replay.py`)

L'export viene da `episodes` (solo SELECT), anonimizzato come `tests/fixtures/fixtures.sql`, in `tests/replay/*.jsonl`. Gli eventi entrano in ordine di `at` in un calendario per agente. A ogni offerta vera e a ogni hold vero si chiamano `candidates` e `check` con la stessa procedura e priorità, e con l'ora di agenda data da `clock.now`.

| Metrica | Come | Obiettivo |
|---|---|---|
| M1 rifiuti evitati | hold veri rifiutati (`EXPIRED`, `NO_CAPACITY`) che il calendario escludeva con il motivo giusto, contati all'hold (`check`) e all'offerta | all'hold: `EXPIRED` ≥ 90%, `NO_CAPACITY` ≥ 35%. All'offerta, con il margine scelto: `EXPIRED` ≥ 80% |
| M2 esclusioni sbagliate | hold riusciti (`held` → `committed`) che il calendario escludeva, per motivo | 0 per `full`, `gone`, `held`. Per `margin`: la curva margine → M1/M2, che serve a tarare il margine |
| M3 riserva a non-U | offerte su slot `urgent_reserve` con priorità ≠ U | 0 (oggi 153/304) |
| M4 S_pref | quota dei primi 3 in S_pref (preferenze del cittadino + token del gp) contro le offerte vere | ≥ quella vera |
| M5 offerte doppie | stesso slot a due casi vivi entro 15 min | 0 |
| M6 S perso | il calendario non trova nulla, ma uno slot vero offerto in quel momento è stato poi prenotato | ≤ 2% |
| M7 costo | `candidates` p99; slot in memoria | < 5 ms; ≤ `max_slots` |

Il replay è controfattuale: misura il filtro rispetto a quello che è successo davvero, non dice se un'altra offerta sarebbe stata accettata.

### Lo stesso schema per ogni tool

Ogni tool ha:
- **limiti**, cioè invarianti;
- **aiuti**, cioè metriche;
- **un oracolo**;
- **una sorgente di replay.**

`tests/evals.py <tool>` stampa una riga per metrica (valore, obiettivo, PASS o WEAK) e i 5 casi peggiori anonimizzati:
- **protocol:** accordo tra `act()` e `understood()` sulle righe vere, ed errori di abbinamento di `ledger_answer`;
- **clock:** errore di `clock.now` contro le risposte `agenda` successive (p99 < 1 minuto di agenda);
- **case:** lo stato ricavato dalla history contro i fatti del Ledger.

## 7. Decisioni

L'orchestratore ha scelto il default; si cambiano su richiesta dell'utente.

| # | Decisione | Default | Perché |
|---|---|---|---|
| D1 | Claim sull'offerta | **debole** sull'`offer`, forte dal `confirm` in poi | con ~8 casi vivi e 3 offerte ciascuno, un claim forte svuota S e porta al −1 «S non vuoto». Oggi le doppie dal vivo sono 0 |
| D2 | Margine | **tarato dal replay** (curva M1/M2), non scelto a mano | le stime (~20 h al booking, ~9 h al `check`) toglierebbero le U dello stesso giorno: prima i numeri |
| D3 | Riserva esclusa per i non-U anche con P3 spenta | **sì** | lo dice il kit (R:718) e lo fanno già il kit e le strutture. Costa il 20% degli slot |
| D4 | Guardia `check()` prima dell'hold nelle strutture | **sì**; `full` solo se la pagina ha meno di 60 s | R:678; 37 rifiuti erano già annunciati |
| D5 | Persistenza | **fatto `city:calendar` per pod** | nessuna modifica dello schema (da qui niente scritture su Supabase) |
| D6 | Dati del replay | **jsonl anonimizzati nel repo**, come le fixture | eval ripetibili da chiunque. Se diventano grandi, solo in locale con lo script di export nel repo |

**Da verificare:**
- la durata dell'hold prima di `STALE_FENCE`;
- se P3 rifiuta sull'hold o sul commit;
- se `free=0` compare già per un hold non confermato;
- se in città girano mai due pod dello stesso agente, e la variabile d'ambiente che identifica il pod;
- il formato di `day=`;
- se il filtro `doctor_day` sul registry scritto nel codice scarta slot veri (lo dirà M2).

**Rischi:**
- il Ledger finto di `journey.py` diverge dal vero: una journey verde non prova la semantica viva finché l'oracolo non è allineato;
- lo sfasamento tra gli orologi dei mittenti nel confronto per `sent_at` vale pochi secondi: accettabile.

## 8. Ordine di lavoro

| Giro | In parallelo (file disgiunti) | Dipende da |
|---|---|---|
| K-a | **modulo + test a, b, c** (`shared/mach/calendar.py`, `tests/unit/test_calendar.py`) · **replay** (`tests/replay.py`, `tests/replay/`), prima sulla `fitting` di oggi (baseline delle metriche), poi sul calendario | protocol, clock (fatti) |
| K-b | **oracolo del Ledger e scenari nuovi** in `tests/journey.py` | la pulizia delle strutture (che oggi ha `journey.py`) |
| K-c | **adozione nel booking** · **adozione nelle strutture** (con `check()`) | K-a, `case.py`, pulizia delle strutture |
| K-d | taratura del margine dal replay (D2), banco di guasti verde sul calendario | K-c |
