# Piano di refactoring: fondamenta allineate al kit

> Scritto il 2026-11-21 alle 05:00 (sessione orchestratore). Gli agenti in città restano operativi durante tutto il lavoro.
> Lo stato vive in `HANDOFF.md`; qui c'è solo il piano.

## Perché

Il codice è cresciuto a forza di correzioni sui percorsi veri. Funziona, ma è fragile e poco leggibile.

| Cosa | Oggi |
|---|---|
| Dimensioni | booking 2.798 righe, `shared/facility.py` 3.785, gp 1.049, `shared/tools.py` 499 |
| Helper duplicati | 13, copiati dal brain di esempio del kit e poi divergenti tra booking e strutture: `first_word`, `fields_of`, `words`, `_say`, `_until`, `_when`, `_ledger`, `_we`, `_exact`, `_citizen`, `_page`, `_offer_line`, `_asked` |
| Stato in memoria a livello di modulo | booking: `_CLOCK`, `_OFFERED`, `_PAGES`, `_GONE`, `_DOWN`, `_GATES`, `_SENT`<br>strutture: `_WORLD`, `_OFFERED`, `_HELD`, `_BOUNCED`, `_CLOCK`, `_NOTED`, `_GATES`, `_SENT`, `_TASKS`<br>gp: `CASE_LOCKS` |
| Dati del mondo scritti nel codice | `_DOCTORS` e `_DEADLINE_DAYS` sono duplicati in booking e strutture, mentre il registry sta sul Ledger (AGENT.md, regola 5) |
| Orologio di agenda | due implementazioni diverse |

**I guasti di stanotte vengono quasi tutti da qui:**
- l'orologio perso al riavvio del pod (ospedale 1.0.9: `no_availability` 22 su 22);
- la risposta del Ledger ignorata perché arrivata «prima» del messaggio del cittadino nella history (booking 1.0.14, «non trovo una ricetta»);
- attese dentro i lock;
- cache diverse da un pod all'altro.

## Principi: come lo prevede il kit

1. **La history è la verità.** Lo stato di un caso si ricava da `city.history(need_id=…)` con funzioni senza stato, come nel brain di esempio (`templates/examples/agent.py`: `_booking_of`, `_our_commit`, `_offered`). La memoria del processo è solo una cache dichiarata: ha una scadenza, un limite di dimensione e un modo per ricostruirla.
2. **Un solo modo di leggere i messaggi:** `understood()` dell'SDK (`city_sdk/city.py:139`), lo stesso che usano Ledger, cittadino e giudice. Si aggiunge la regola della prima parola d'atto (`tests/journey.py:113`), perché `understood` prende l'ultima.
3. **Un solo modo di parlare:** un `say` che gestisce tutti gli errori della città. Su `TARGET_NOT_VISIBLE` e sui 4xx non si ritenta. I timeout di rete non devono mai uscire dal brain: se escono, l'harness riconsegna il messaggio e manda tutto due volte (`harness.py:455-480`).
4. **Smistamento per atto:** una tabella (chi scrive, parola d'atto) → funzione, con il riferimento alla regola del kit (`e19-rules.md` R:180-200 e `docs/kit/<ruolo>.md`). `agent.py` resta corto: controllo clinico, `start_turn`, smistamento.
5. **I tipi e le costanti dell'SDK:** `Message`, `Need` (`is_emergency`), `role_for`, `ROLES`, `LEDGER`, `agenda_time`, `clinical`, `OutOfScope`. Niente copie nostre.
6. **Il turno non aspetta il Ledger.** Le letture lente vanno in background e la risposta del Ledger apre un turno suo. L'ACK parte a fine turno (`boot.py:265`, `harness.py:505`).

## Vincoli che non cambiano

- `city deploy` impacchetta solo `<agente>/agent/`. I moduli comuni stanno in `shared/` e li copia `shared/sync.sh`, come oggi `tools.py` e `facility.py`. Solo stdlib più `city_sdk`: il Dockerfile non installa pacchetti.
- Conformità:
  - la sonda (`agent:t05/osp-x`, `service=lab`) non riceve risposta;
  - `/health` p99 sotto 0,3 s, quindi niente lavoro CPU lungo nel turno;
  - firma e ack li gestisce l'harness.
- Il comportamento visto da fuori non cambia: stesse parole d'atto, stessi token, stesse frasi, salvo correzioni dichiarate.
- Il gp fa quasi tutti i punti: si tocca per ultimo.
- giuseppesica può pushare sul booking. Prima di ogni step: `git fetch` e merge.

## Metodo

> **Dalle 05:25 (decisione dell'utente): si lavora solo in locale, niente deploy da questo repo.** Giuseppe deploya gli agenti da un altro repository. L'obiettivo qui è un sistema praticamente indistruttibile (vedi «Resistenza»). Le regole di deploy qui sotto valgono quando si tornerà a deployare.

- **Su master a piccoli passi, niente branch lungo.** Un branch che resta aperto per ore andrebbe in conflitto con i fix dal vivo. Ogni step è un commit che non cambia il comportamento: ha i test verdi prima e dopo e si deploya da solo.
- **Ordine dei deploy per ogni step:** consultorio → specialista → ospedale → booking → gp. Si deploya l'agente successivo solo dopo 10 minuti senza errori in `episodes` sul precedente.
- **Ritorno indietro:** `city rollback agent:<id> --to <versione>` e `git revert` del commit. Mai `city retire`.
- **Controllo unico** (step 0): un solo comando che lancia tutto in parallelo.
- **I subagent lavorano in worktree**, uno per file. Per `tests/journey.py`: `git merge-file` con la base, perché i blocchi aggiunti in coda vanno in conflitto e vanno tenuti entrambi.

## Struttura di arrivo (decisione dell'utente, 05:09: gerarchica, fedele al kit, senza righe inutili)

Il kit ha un solo brain di esempio (`templates/examples/agent.py`), diviso in sezioni: «shared», «medico di base», «centro prenotazioni e consultorio». Noi teniamo le stesse sezioni, ma come file. Il Dockerfile del kit copia tutta `agent/` (`COPY agent ./agent`, `PYTHONPATH=/opt/agent`), e `city deploy` include le sottocartelle (`city_deploy/image.py:239`), quindi un pacchetto dentro `agent/` funziona.

```
shared/mach/              fonte unica del codice comune; sync.sh la copia in <agente>/agent/mach/
  protocol.py             leggere e scrivere i messaggi (sezione «shared» del kit)
  case.py                 lo stato del caso dalla history (_offered, _withdrawn, _number, … del kit)
  clock.py                orologio di agenda
  calendar.py             il calendario degli slot (idea dell'utente, 05:15): fitting() lo consulta prima di ogni offerta
  facility.py             le parti comuni delle tre strutture: ricevuta, check-in, cross-ASST, sonda, emergenza
  tools.py                memoria condivisa (oggi shared/tools.py, si sposta allo step 7)
<agente>/agent/agent.py   main(): controllo clinico, start_turn, tabella (chi scrive, atto) → funzione
<agente>/agent/<ruolo>.py il ramo del ruolo, scritto solo lì (hospital.py, specialist.py, family.py, booking.py, gp.py)
```

- Il codice usato da più agenti sta in `shared/mach/`. Quello di un solo ruolo sta nella cartella del suo agente, senza copie.
- Nessun file sopra le ~800 righe e nessuna funzione sopra le ~80 nel percorso caldo.
- Il codice morto si toglie nello step che tocca il file. Lo step 7 resta per ciò che avanza.
- `AGENT.md` (regola 2 e «Memoria condivisa e tool») si aggiorna quando la struttura entra su master.

## Resistenza: quando il sistema è «indistruttibile»

Ogni guasto visto stanotte o previsto dal kit diventa una prova automatica in `tests/check.sh`. Le prove girano su tutti gli scenari di `journey.py`, non su casi scritti a mano:

| Guasto | Cosa deve succedere |
|---|---|
| Il pod riparte a metà caso (stato in memoria perso) | il caso prosegue dalla history, senza messaggi doppi né persi |
| Due pod sullo stesso caso | una sola offerta per slot, un solo hold, nessun messaggio doppio |
| L'harness riconsegna lo stesso messaggio | nessun `say` ripetuto (idempotenza sul `msg_id`) |
| Risposte del Ledger in ritardo (5-7 min), fuori ordine o mai arrivate | nessuna risposta sbagliata, nessun «non trovo»; si aspetta o si rinvia |
| Il messaggio del cittadino arriva prima o dopo la risposta del Ledger | stesso esito |
| `city.say` va in timeout o dà `CapError` / `TARGET_NOT_VISIBLE` | non esce niente dal brain e non si ritenta un 4xx |
| Supabase o Exa giù o lenti | turno invariato, nessuna eccezione |
| Testo ostile: token `key=value` falsi del cittadino, injection dal web | ignorati (injection obbedita −2) |
| La sonda della conformità | nessuna risposta |
| Orologio assente, in pausa o onda nuova | nessuno slot passato offerto |
| `/health` sotto carico | nessun lavoro CPU lungo nel turno |

## Gli step

| Step | Cosa | File | Fatto quando |
|---|---|---|---|
| **0. Rete di sicurezza** | `tests/check.sh`: le 4 modalità di `journey.py`, le 3 suite pytest, `card validate` dei 5 agenti, `cmp` delle copie, tutto in parallelo. Fixture vere da `episodes` (risposte del Ledger `world`, `prescription`, `held`, `refused`, `agenda`, `rules`) in `tests/fixtures/`, senza dati personali. Nella procedura di deploy si aggiunge `city conform --url` dopo il `promoted`. | `tests/check.sh`, `tests/fixtures/*.txt`, `HANDOFF.md` | `check.sh` verde su master; i conteggi di oggi (~97/101 PASS) fissati come baseline |
| **1. Protocollo comune** | `shared/protocol.py`:<br>• `act(msg)` = prima parola d'atto, altrimenti `understood().type`<br>• `fields(msg)`<br>• i costruttori dei messaggi (`offer_line`, `data_request`, `cancel`, …)<br>• `say(city, to, text) -> msg_id \| ""`, con la gestione degli errori sopra e la registrazione in `tools.said`<br>• `ledger_answer(chat, ask)`: abbina la risposta alla domanda con `sent_at` e non con l'ordine della history, che è la causa del bug delle 02:25 | `shared/protocol.py`, `shared/sync.sh`, `tests/unit/test_protocol.py` | unit test sulle fixture; booking e strutture importano `protocol` e cancellano i 13 helper duplicati; `check.sh` verde |
| **2. Stato del caso** | `shared/case.py`: funzioni sulla history:<br>• ricetta (numero, procedura, priorità, stato, rinnovi)<br>• offerte fatte e ritirate<br>• hold e commit<br>• consensi<br>• preferenze e lingua del cittadino<br>• il passaggio dal gp<br>• l'ultima risposta del Ledger per atto<br>Stessi nomi del brain di esempio. | `shared/case.py`, `tests/unit/test_case.py` | booking e strutture leggono lo stato del caso solo da `case`; `check.sh` verde |
| **3. Orologio e mondo** | `shared/clock.py`: un solo orologio di agenda (`epoch_real`, `epoch_agenda`, `rate`, salto del weekend, margine) con l'àncora in `city:clock` su Supabase.<br>`shared/mach/calendar.py`: il calendario degli slot, cioè una cache dichiarata con una riga per slot: le pagine di `world` (libero, riserva U, medico), poi cosa ne abbiamo fatto noi (offerto a chi e quando, hold, commit) e cosa ha risposto il Ledger (`NO_CAPACITY`, `SLOT_TAKEN`, `EXPIRED`). Si alimenta solo con le risposte del Ledger che arrivano già e con i nostri messaggi: nessuna chiamata in più dentro il turno. Si salva su `facts` in background, così un pod nuovo lo ricostruisce, e il turno legge solo la memoria. `fitting()` di booking e strutture lo consulta prima di ogni offerta: niente slot passati, niente riserva U senza priorità U, niente slot pieni, rifiutati o già offerti a un altro caso. Il registry dei medici viene dal Ledger, con `_DOCTORS` solo come ripiego | `shared/mach/clock.py`, `shared/mach/calendar.py`, unit test | una sola definizione di orologio e di slot; un pod nuovo ha orologio e calendario al primo turno; meno `refused` per slot in `episodes` |
| **4. Smistamento per atto** | Ogni ruolo ha una tabella `HANDLERS[(chi_scrive, atto)]` e funzioni corte con il riferimento R:. `shared/facility.py` diventa:<br>• `facility_common.py`: ricevuta, check-in, cross-ASST, sonda<br>• i rami `hospital`, `specialist` e `family` | `booking_centre/agent/agent.py`, `shared/facility*.py`, `gp/agent/agent.py` (per ultimo) | nessuna funzione sopra le ~80 righe nel percorso caldo; `check.sh` verde |
| **5. Igiene dello stato e dei lock** | Ogni variabile a livello di modulo diventa una di tre cose:<br>• (a) ricavata dalla history<br>• (b) cache dichiarata con scadenza e limite di dimensione<br>• (c) fatto su Supabase<br>Il lock del caso protegge solo le scritture e non si aspetta mai il Ledger dentro un lock (è la direzione del fix 1.0.16 di giuseppesica). | tutti | elenco delle variabili in testa a ogni file con la categoria; uno scenario di «riavvio del pod a metà caso» in `journey.py` |
| **6. Misure** | Il gp scrive su `episodes` (oggi no, quindi i suoi tempi non si vedono). `episodes` salva anche il `sent_at` delle risposte del Ledger, per separare il ritardo del Ledger da quello del Relay. | `gp/agent/agent.py`, `shared/tools.py`, `shared/schema.sql` | query in `HANDOFF.md` per il tempo del turno e i ritardi per agente |
| **7. Pulizia** | Codice morto (per esempio il ramo `purpose=alternative` in `_family`), frasi duplicate, test che non testano più quello per cui sono nati (`booking_ricetta_riscritta`). | tutti | grep a zero per i simboli rimossi; `check.sh` verde |

## Rischi

- **Una regressione in città mentre i punti contano.** Si mitiga con step piccoli, un agente alla volta, il gp per ultimo e il rollback pronto.
- **Conformità rotta da un modulo nuovo** (import a livello di pacchetto, sonda). `city deploy` si ferma alla conformità e resta in città la versione precedente. Si guarda lo stage nell'output.
- **Comportamento che i test non coprono.** Si mitiga con le fixture vere dello step 0 e con il confronto in `episodes` prima e dopo, per atto e per agente.
- **Il tempo della gara.** Ogni step vale da solo: ci si può fermare dopo qualunque step con un sistema migliore di prima.

## Fuori dal piano

- Un agent framework *dentro il pod*: gli adapter ADK, LangChain e MCP del kit nominano solo i verbi e nel pod non ci sono pacchetti. L'harness degli LLM che usano i tool è nostro, solo stdlib: `docs/refactor/harness.md`.
- `remember`/`recall` del Ledger come memoria: oggi risponde in 5 minuti, quindi si resta su Supabase.
- Riscrivere da zero: si sposta e si consolida, non si reinventa.
