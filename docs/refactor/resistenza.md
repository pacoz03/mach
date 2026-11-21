# Resistenza: dove siamo (tests/chaos.py)

> 2026-11-21, HEAD `1f86b30`. Misura della tabella «Resistenza» di `piano.md`: ogni scenario di `tests/journey.py`
> sotto ogni guasto, modalità plain e `--ledger-turns`. Elenco esatto dei fallimenti in `tests/chaos_baseline.txt`.

```
~/.local/share/uv/tools/city-cli/bin/python -B tests/chaos.py            # ~4 min, uscita 1 solo su un fallimento nuovo
~/.local/share/uv/tools/city-cli/bin/python -B tests/chaos.py -k <scenario> --show <guasto> plain   # trascritto
```

Senza guasto (`none`) il risultato è quello di journey: plain PASS 100, XFAIL 2, SKIP 5; turns PASS 105, XFAIL 2.

## Matrice: 427 terne che falliscono, per agente del primo controllo rosso (`ledger_late`/`_reorder` solo turns)

| guasto | gp | booking | hospital | specialist | consultorio | fail | ok | skip |
|---|---|---|---|---|---|---|---|---|
| restart | 0 | 0 | 6 | 0 | 0 | 6 | 175 | 33 |
| two_pods | 0 | 2 | 12 | 0 | 0 | 14 | 167 | 33 |
| redeliver | 27 | 4 | 16 | 0 | 0 | 47 | 158 | 9 |
| ledger_late | 3 | 2 | 21 | 1 | 3 | 30 | 62 | 122 |
| ledger_reorder | 4 | 1 | 21 | 1 | 2 | 29 | 63 | 122 |
| ledger_lost | 0 | 27 | 2 | 0 | 2 | 31 | 148 | 35 |
| clock_lost | 0 | 72 | 0 | 0 | 4 | 76 | 97 | 41 |
| say_timeout | 30 | 7 | 68 | 3 | 6 | 114 | 85 | 15 |
| say_tnv | 24 | 11 | 9 | 3 | 2 | 49 | 150 | 15 |
| memory_down | 0 | 0 | 0 | 0 | 0 | 0 | 205 | 9 |
| hostile | 11 | 12 | 8 | 0 | 0 | 31 | 174 | 9 |
| probe | 0 | 0 | 0 | 0 | 0 | 0 | 205 | 9 |

## Le debolezze vere, in ordine di danno

1. **Il booking prenota con la ricetta di un altro cittadino.** `hostile booking_ricetta_inesistente`: il cittadino scrive
   `prescription_number=RX-O9` (paziente `citizen:T-9`, priorità U); il Ledger lo dice, il booking offre a priorità U e
   l'ospedale fa hold e commit con quella ricetta. `_number` (`booking_centre/agent/agent.py:2319`) prende l'ultimo numero
   scritto dal cittadino; `_prescription` (:2125) non confronta `patient` con il cittadino del caso. Il gp lo fa già.
2. **L'ospedale risponde «no_availability» quando non sa.** Cross-ASST con la pagina di `world` non nella memoria del
   suo pod o non ancora arrivata (`two_pods cross_asst_push_reale`, `ledger_late cross_asst_prenota`, ~25 terne):
   `cancel reason=no_availability` con slot liberi. `_cross_asst`, ramo `else` a `shared/facility.py:2118`: un «non lo so» diventa un attributo falso.
3. **Le eccezioni di `city.say` escono dal brain.** `say_timeout`/`say_tnv`: crash del gp in 52 terne, poi consultorio,
   booking, specialista. `_say` del gp rilancia tutto (`gp/agent/agent.py:1050-1058`); quello delle strutture rilancia
   per specialista e consultorio (`shared/facility.py:452-453`). Con un `httpx.ReadTimeout` l'harness riconsegna il
   messaggio e il turno riparte da capo (`harness.py:455-480`).
4. **L'ospedale ritenta dopo un timeout e duplica.** `say_timeout`, 68 terne: il Relay aveva accettato il primo invio,
   il secondo è un `commit`/`hold` doppio sul Ledger o lo stesso messaggio due volte al cittadino e ai partner.
   `shared/facility.py:452-455` (un retry su ogni eccezione non `CapError`).
5. **Il booking tace se una risposta del Ledger non arriva.** `clock_lost` (72) e `ledger_lost` (27): nessuna parola al
   cittadino, nessuna nuova richiesta. `booking_centre/agent/agent.py:702-703`: `if now is None or world is None: return`.

Poi: con `redeliver` il gp ripete lo stesso messaggio al cittadino (23) e l'ospedale `hold`/`checkin` (16), niente
idempotenza sul `msg_id`; `restart`/`two_pods` danno un `commit` doppio (`ospedale_commit_unico`) e risposte cross-ASST
lente senza l'orologio in memoria (3-4 s invece di <1). `memory_down` e `probe`: nessun fallimento.

## Cosa non misura

- Orologio in pausa o onda nuova: il formato di `agenda` è globale (`REPLY` in journey); servirebbe un template per Ledger.
- `/health` sotto carico: il simulatore non ha HTTP.
- Due pod davvero in parallelo: i turni sono alternati, non concorrenti (journey ha `race` solo per il push di `held`).
- Exa giù: nessun agente chiama `web_search`, quindi `memory_down` tocca solo Supabase.
- I task in background dei brain girano solo quando il bus cede il loop: il Port di journey lo fa solo negli scenari
  con attese (`race`, `say_delay`, `slow_*`); `memory_down` cede a ogni chiamata. Altrove il background non parte.

## Aggiornamento 06:50 (orchestratore)

- **Sul master `c1e2e49`:** 416 combinazioni che falliscono, contro le 427 di `1f86b30`.
  - Le 11 risolte sono `say_timeout`: grazie a `protocol.say`, un timeout non esce più dal brain nello specialista e nel consultorio.
  - Le 2 voci in più sono lo scenario nuovo `injection_facility` sotto `redeliver`.
  - Il baseline è rigenerato su master.
- **Quanto è realistico `redeliver`.** L'harness dell'SDK scarta già i duplicati con lo stesso `msg_id` (`city_sdk/harness.py:250`, `MemoryJournal`), ma solo nello stesso pod e finché il pod è vivo. In città la consegna doppia avviene solo:
  - quando un `httpx.TimeoutException` esce dal brain (`journal.forget`, poi ack `retryable`, `harness.py:466`);
  - dopo un riavvio;
  - con due pod.

  Priorità: prima nessun timeout fuori dal brain, poi l'idempotenza tra un riavvio e l'altro.
- **Comando:** `bash tests/check.sh --chaos` aggiunge il banco ai controlli (circa 4 minuti, circa 6 GB di RAM). Si lancia all'integrazione di codice degli agenti.
