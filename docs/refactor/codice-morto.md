# Codice morto, fuori kit e testi doppi

> 2026-11-21 05:20, HEAD `eab8fc5`. F e B sono identici a `c07fcd4`. G e T hanno modifiche non committate di un'altra sessione (step 6): le loro righe sono quelle di HEAD. Solo analisi, nessun codice cambiato. Non ripete `inventario.md` (13 helper, stato di modulo, `_DOCTORS`, orologio): lo cita.
> Sigle: **F** = `shared/facility.py`, **B** = `booking_centre/agent/agent.py`, **G** = `gp/agent/agent.py`, **T** = `shared/tools.py`, **J** = `tests/journey.py`, **K** = `mach-hackathon-kit/templates/examples/agent.py`, **R:** = riga di `mach-hackathon-kit/docs/kit/e19-rules.md`.
> **Metodo.** Raggiungibilità AST da `main`, `handle` ed `emergency` sui 4 file, con `grep` su tutto il repo (test inclusi). Poi lettura dei rami i cui chiamanti passano un valore fisso. Dal vivo: `episodes`, solo SELECT (8524 righe del Ledger in ingresso, 20:03 → 04:13 UTC).
> **Limiti della misura.** Il gp non scrive su `episodes`, quindi i suoi rami non si misurano. Il booking ci scrive solo dalle 00:57 UTC. I `city.say` diretti B:1053 e B:1439 non passano da `tools.said`.

## Riepilogo (righe che possono andare)

| File | Righe | Sicure | Con test da togliere | Probabili (da verificare) |
|---|---|---|---|---|
| F | 3785 | ~150 | ~120 (consenso referto, `purpose=alternative`) | ~115 (follow-up, inoltro `confirm`, teste Ledger mai viste) |
| B | 2798 | ~40 | — | ~50 (partner scritti nel codice, formato sdk) |
| G | 1049 | 0 | — | 0 (un'incoerenza con B: §5.2) |
| T | 499 | 0 | — | ~45 (`web_search`: da decidere) |
| test | — | — | J ~60, pytest ~65 (§4) | — |

**In totale:** ~190 righe sicure, ~500 possibili, più i commenti da riscrivere (§3).

## 1. Codice morto

| file:righe | cosa | evidenza | verdetto | righe |
|---|---|---|---|---|
| F:1776-1785 | `_ARRIVED` dell'ospedale (la regex ricca) | È ridefinita a F:3556 e all'import vince la seconda. Oggi quindi `_arrival` (F:2323, ospedale) e `_prose_checkin` (F:3595, specialista) usano la regex dello specialista, e togliere F:1776 non cambia nulla. Se invece lo step 4 la dà all'ospedale, il comportamento cambia (provato sulle due regex): diventano arrivi «eccomi», «sono giunta», «siamo qua», «just arrived», «I'm at the hospital», «sono all'ospedale»; smettono di esserlo «sono allo sportello», «in accettazione», «I arrived», «at the desk» | togliere, oppure adottarla per l'ospedale come cambio dichiarato | 10 |
| F:2145-2156 | `if owed:` dopo il lock in `_cross_asst` | `owed > 0` solo dentro `if procedure in procedures` (F:1977), dove `hospital` è definito. Dentro il lock F:2132 ritorna se è l'ospedale, F:2138 se non lo è: il blocco è morto per costruzione | togliere | 12 |
| F:1857-1865 | ramo «sonda» di `_hospital` | `handle` (F:252-262) ritorna già per ogni `need.request` di un agente senza `request_id`/`procedure`/`service_type` e senza «confirm». Arriva qui solo un `cancel` che contenga sia «need.request» sia «confirm» | togliere | 9 |
| F:926-939, F:1259-1273, F:1275-1295 | i rami di `_desk` diversi dal Ledger, più `_forward_receipt` e `_partner_cancel` | `_desk` ha un solo chiamante, F:1509, dentro `if message.sender == LEDGER` (F:1491): i rami per cittadino e agente non partono mai. `_forward_receipt` e `_partner_cancel` hanno come unico chiamante `_desk`. È la parte «centro prenotazioni» di K rimasta nelle strutture. `_desk` si riduce a `_advance(..., fixed="CONS-GIN")` e il suo parametro `hold_self` sparisce | togliere | 50 |
| F:981-998, F:1001-1022, F:549-552, F:759-766 | ramo ricetta di `_advance_alone`: lista d'attesa P, richiesta `prescription` al Ledger, `_has_procedure`, `_number` | `fixed` vale sempre `"CONS-GIN"`: chiamanti F:941→F:1509, F:1198, F:1545, F:1610, F:1623, F:1667, F:3282 (F:1294 è morto). Quindi `reply`, `priority` e `number` sono sempre `None` e `procedure` non è mai vuota. Dal vivo le strutture non chiedono mai `prescription` al Ledger (0 righe su 8524) | togliere, insieme al parametro `fixed` e alle condizioni `fixed == "CONS-GIN"` (F:970-973, 1053, 1059, 1073, 1079, 1088, 1123, 1137) | ~60 |
| F:1344-1348, F:1361-1366, F:1543-1549 | lettura della risposta a `data.request purpose=alternative` | Nessuno la manda più: F:1137-1140 la sostituisce con un `offer`, e il consultorio non ha nessun `data.request` in `episodes` | togliere con i test (§4) | 18 |
| F:1734-1744, F:3490-3554, F:3562-3572, F:3613-3627 | consenso `report_sharing`: il ramo di `_facility_dispatch`, `_after_report`, `_consent_asked`, `_on_consent`, `_CONSENT_CODE`, `_YES_PROSE`, `_NO_PROSE`, `_consent_asks` | Il referto si archivia subito, senza chiedere (F:3431-3434). Il circolo è chiuso: `_consent_asked` è vero solo dopo un nostro `consent purpose=report_sharing`, quel messaggio lo manda solo `_on_consent`, e `_on_consent` parte solo se `_consent_asked` è vero. Dal vivo lo specialista ha 21 `report` e 0 `consent` | togliere con i test (§4). È una scelta contro R:197 e R:208: se si torna al flusso del kit, si tiene | 102 |
| F:1326-1331 | `_family_date` | Dà lo stesso testo di `_in_words` (F:796-799): giorno, `%d/%m`, at/alle, `%H:%M` | togliere e usare `_in_words` (F:1090, 1144, 3180) | 6 |
| B:1772-1781 | `_past_window` | 0 riferimenti nel repo (AST e grep, test compresi) | togliere | 10 |
| B:2640-2650, B:2683-2687 | `_asked_preferences` e la sua clausola in `_hear` | Dalla 1.0.17 (`89e5bd9`) il booking non manda più `data.request fields=days/gender`. L'ultimo in `episodes` è delle 03:53 UTC (prima ce n'erano 231). La funzione è sempre falsa | togliere | 16 |
| B:2003-2004, B:2065, F:622 | ripiego `agenda_date` → `agenda_now` nella risposta `agenda` | Lo schema del Ledger ha `agenda_now` (`city-sdk/src/cap/core/generated/clients/ledger.py:231`). Dal vivo `agenda_now` c'è sempre (inventario §4) e K non lo fa. L'`agenda_date` dello slot in `_slot` è un'altra cosa e resta | togliere | 4 |

**Parametri sempre uguali.** Oltre a `fixed` e `_desk(hold_self)`, ci sono parametri mai passati:
- `tools.redirect(role)`: unico chiamante F:1766;
- `tools.note(source, need_id, citizen)`: unico chiamante F:632;
- `tools.context(need_id, citizen)`: unico chiamante F:655.

Sono API documentate in `AGENT.md`: tenere.

## 2. Fuori kit

| file:righe | cosa | riferimento | verdetto | righe |
|---|---|---|---|---|
| F:1177, F:1239-1256 | `_confirm_alone` inoltra un `confirm` a `facility` quando non prenota lui | È logica da centro prenotazioni di K. `facility` viene dalla nostra offerta oppure dal token `facility=` del messaggio. Un cittadino che scrive all'ospedale `confirm slot_id=X facility=agent:altro consent_ref=…` fa partire verso `agent:altro` un `confirm` con `citizen=` e il consenso: injection obbedita e dati a terzi (R:243, −2). Le strutture non hanno mai inviato un `confirm` in `episodes` | togliere, e prendere `facility` solo dalla nostra offerta | 18 |
| F:3628-3677, F:3692-3697, F:3757-3785 | ricetta di controllo dello specialista (`_follow_up`, `_prescribe_follow_up` e il blocco follow-up di `_on_reported`) | Parte solo con un token `follow_up=` in una riga del Ledger: 0 righe su 8524. R:197 dice che lo decide lo specialista «quando il percorso lo richiede», ma il kit non dà il segnale. Lo specialista non ha mai mandato un `prescribe` | da verificare: il +2 «follow-up per protocollo» (R:236) esiste, l'innesco no | ~80 |
| F:1877, F:1885-1887, F:2416-2425, F:2466, F:2546 | teste `cancelled`, `cancel` ed `expired` in arrivo dal Ledger | Mai viste: dal vivo il Ledger manda solo `slots`, `agenda`, `prescription`, `rules`, `refused`, `held`, `committed`, `checked_in` e `reported`. K non le gestisce, e J:51 chiama `expired` «forma ipotetica». `cancelled` sarebbe la risposta al nostro `cancel hold_id` (SKILL.md:45), che dal vivo non abbiamo mai mandato | da verificare: tenere `cancelled`, togliere `expired` | ~10 |
| F:697-699, B:2014-2016 (più gli alias in `_slot`, F:361 e B:1893) | formato `world slots=N` e slot con `agenda_date`/`agenda_day`/`capacity` | È il LocalLedger dell'SDK (`ports.py:107`, `ledger.py:107-121`). Il Ledger vero risponde sempre `slots` con `at`/`day`/`free` (4155 risposte su 4155). Serve solo a `--ledger-format sdk` in `check.sh` | tenere finché `check.sh` lo usa. Togliere insieme modalità e codice dimezza i giri di J | ~8 |
| F:1578 | `data.request purpose=checkin` del consultorio | R:188: gli scopi di `data.request` sono `booking`, `prescription_renewal` e `cross_asst`. L'ospedale, per la stessa domanda, usa `purpose=booking fields=booking_id` (F:2635). Mai inviato dal vivo | uniformare a `purpose=booking` | 1 |
| F:2009 | il consultorio risponde al partner `request_id=… Ricevuto: sto verificando…`, senza parola d'atto | R:192: dalla struttura al centro prenotazioni vanno `offer` o `cancel`. Dal vivo 21 invii con atto vuoto verso agenti | da verificare: vale per il fast-reply di R:246? | 1 |
| F:2529, F:2557, F:2561, F:2563 | `_release` risponde senza atto, oppure con `cancelled …`, al centro che annulla | Il kit non definisce questa risposta; il docstring spiega perché non si usa `cancel` | tenere | — |
| F:3137-3141 | `_on_held` inventa `consent_ref=cons-<need_id>-booking` quando non lo trova | AGENT.md regola 9 («mai inventare»). Il consenso lo dà il cittadino (R:194). È il formato del simulatore (J:521) | da verificare su `episodes`, probabilmente togliere | 5 |
| B:1169-1210 | `_RARE_BY_ASST`, `_KNOWN_PARTNERS` (presi da Discord), più «ASST Gorla» scritto nel testo B:1430 | Sono dati del mondo nel codice (AGENT.md regola 5); il kit dà `city.find(service)` e lascia la scelta a noi. L'anello parte solo per una lingua che nessun nostro medico parla (B:737); con la 1.0.17 il cittadino ha ricevuto 10 avvisi «strutture partner» | da verificare: tenere se l'anello rende | ~45 |
| B:842-854, G:143, G:966-997 | protocollo nostro tra gp e booking: `handoff=gp`, `handoff=accepted`, `notify_if=no_slots`, `data.response purpose=booking_follow_up` | Manca nella tabella R:183-199: non c'è nessuna riga gp → centro prenotazioni. `handoff=accepted` è partito 280 volte, `booking_follow_up` mai | tenere (collaborazione, R:249) | — |
| T:280-314, T:39, T:43, T:468-471 | `web_search` (Exa) | Nessun brain la chiama, solo `_smoke`. Eppure `sync.sh` mette `EXA_KEY` in ogni pod, e `AGENT.md` la documenta come tool | da decidere: togliere insieme a `AGENT.md`, `sync.sh` e `_secrets.py`, oppure tenere | ~45 |

I dati dei medici scritti nel codice (`_DOCTORS`, `_DEADLINE_DAYS`) sono già in `inventario.md` §3.

## 3. Testi ridondanti

| file:righe | cosa | evidenza | verdetto | righe |
|---|---|---|---|---|
| B:185-190 | la frase `redirect` in italiano | Identica, carattere per carattere, a `tools.redirect("booking_centre")` (provato) | usare `tools.redirect` | 6 |
| B:97, F:142-151, G:80-93 | tre testi di `escalate.emergency` | B è solo in italiano; F e G hanno it/en, con frasi diverse. Il kit chiede solo la parola d'atto e il 112 (R:212, R:442) | un solo testo in `shared/mach/`, insieme a `red_flag` (inventario §1) | ~20 |
| F:1758-1763 | `_redirect` in inglese, scritto a mano | L'italiano usa `tools.redirect()` (F:1766). L'inglese nomina solo il gp e il centro prenotazioni: le due lingue dicono cose diverse | una sola fonte (`WHO` in inglese) | 6 |
| F:1321, F:1776, F:3556 | tre regex di arrivo (consultorio, ospedale morta, specialista) | §1 | una sola | ~10 |
| F:1319-1320, F:3563-3572, B:2435-2443 | sì e no in prosa: `_FAMILY_YES/_NO`, `_YES_PROSE/_NO_PROSE` (morte, §1), `_YES/_NO` | tre coppie per la stessa cosa | una coppia comune | ~10 |
| B:2335 | `_WEEKDAYS` | sottoinsieme di `_IT_DAYS` (B:1890), nello stesso file | togliere | 3 |
| F:1136, F:1662 | stessa `data.request purpose=booking fields=days` del consultorio | due testi diversi | uno | 3 |
| B:381 e B:384 | stesso commento ripetuto in `_phrase` | — | toglierne uno | 1 |
| giorni e date | `_DAY_WORDS` B:1544, `_DAY_NAMES` F:781, `_FAMILY_DAYS` F:1314, `_DAY_RX` B:2411, G:537, G:600; formati di data `_when_words` B:1581 e `_in_words` F:796 | inventario §1 e §3 | in `protocol` o in `clock` | — |

**Commenti che raccontano la storia invece del perché.** Vanno riscritti; non sono contati tra le righe tolte.
- **F:33-35:** il commento di `_LEAD` dice «480x», ma la città va a 60x: 4 ore di agenda sono 4 minuti veri. Il booking usa 1 ora (inventario §4).
- **F:82-83:** `_OUTCOME` è segnato «Hypothesis», ma dal vivo sono stati accettati 21 `report … outcome_code=VISIT`.
- **Versioni e commit dentro i commenti:**
  - F:72 «live 1.0.8»;
  - F:259-261 «1.0.9» e «1.0.6»;
  - F:297 «(0e2d784)»;
  - F:1835 «live 1.0.9»;
  - F:1864 «(1.0.9 deploy)»;
  - F:1947 «(21a6d28)»;
  - B:530-532 «Local study 21/11»;
  - B:1212-1217 «our old 12»;
  - B:2140-2141 «as before marks existed».
- **J:28-33:** l'intestazione «IPOTESI: sostituire con risposte reali» è superata, perché le risposte vere ora stanno in `tests/fixtures/`.

## 4. Test che non provano più ciò per cui sono nati

| file:righe | test | perché | verdetto |
|---|---|---|---|
| J:2010-2066 | `booking_ricetta_riscritta` (`rewriting_citizen`, `rx_trusted`) | Il cittadino riscrive il numero solo dopo un `data.request fields=days` (J:2033), che il booking non manda dalla 1.0.17. Quindi la riscrittura non avviene, il Ledger lento non entra in gioco e lo scenario passa senza provare la corsa del bug delle 02:25 | riscrivere l'innesco: dopo la prima `offer` |
| J:77, J:1055, J:1238 | `CHECKIN_REFUSED = "OUTSIDE_WINDOW"` (`checkin_rifiutato` e simili) | Il Ledger vero rifiuta con `refused checkin code=POLICY_VIOLATION detail=outside` (`tests/fixtures/refused.txt:5`; 2 casi su 2 in `episodes`). Il test prova un codice che non arriva mai (§5.3) | usare il codice vero: il test fallirà, ed è giusto |
| J:1957-1969, J:1941-1942, J:1995-1996 | `red_flag_after_prefs`, `offered_on_turn`, `booking_world_in_cache` | Nomi e docstring parlano della domanda sulle preferenze prima dell'offerta, che non c'è più. I controlli restano validi | rinominare e aggiornare i docstring |
| `tests/specialist/test_report_flow.py:18-25`, `:60-79` | `_asked_case` e tre test: `test_consent_ref_in_own_turn_files_shared_report`, `test_plain_no_in_own_turn_files_unshared_report`, `test_message_after_report_gets_a_reply_and_no_second_report` | Partono da un `consent purpose=report_sharing` che lo specialista non manda più (§1) | togliere insieme al codice. L'ultimo va riscritto: risposta dopo il referto, senza consenso chiesto |
| `tests/specialist/test_level3.py:16-21`, `:57-76` | `_asked` e tre test sul consenso scritto in prosa | stesso motivo | togliere insieme al codice |
| `family_counseling/tests/test_level5.py:23-39` | `test_alternative_requires_yes`, `test_alternative_reply_never_books` | `purpose=alternative` non si manda più (F:1137-1140) | togliere insieme al codice |
| `tests/llm_bench.py:36` | compito `intent` sul consenso `report_sharing` | È un banco manuale, fuori da `check.sh`, e quel flusso non esiste più | da verificare |
| J:1277-1279 | `gp_preferenze` | Controlla i token nel passaggio del gp, ma non che il booking li usi (§5.2) | aggiungere un controllo sull'offerta |

## 5. Comportamento dal vivo da segnalare (non è codice morto, non corretto)

**5.1 Il booking offre la riserva U.**
- **Il meccanismo:**
  - B:721-722 e B:1521 calcolano `reserve = rules is None or "P3" in rules`;
  - dal vivo il Ledger risponde sempre `rules count=0` (291 risposte su 291), quindi `_rules` restituisce `set()` e non `None`;
  - di conseguenza `reserve=False`, e `fitting` (B:1831-1833) non scarta gli slot `urgent_reserve=true`;
  - il Ledger mette quel flag sugli slot delle 09:00 (`tests/fixtures/slots.txt:3`).
- **Le misure** (`episodes`, 24 h):
  - messaggi di offerta del booking con almeno uno slot delle 09:00: 36 su 80 nella 1.0.17, 25 su 54 nella 1.0.16, 37 su 81 nella 1.0.14;
  - nelle strutture 0 su circa 1450 messaggi `offer`: F:594-596 li scarta sempre, come K:378-380;
  - finché P3 non è in vigore il Ledger li accetta: sono slot delle 09:00 38 `held` su 139, 34 `committed` su 131 e 9 `NO_CAPACITY` su 37.
- **Cosa dice il kit:**
  - R:718 (P3): «il centro prenotazioni offre solo slot non riservati per B, D e P»;
  - R:244: −1 per una «prenotazione non-U nella riserva U»;
  - K li esclude sempre.
- **I rischi:**
  - se l'`S` del giudice esclude la riserva, ogni prenotazione delle 09:00 per una priorità B o D vale −1 invece di +1 o +2. Non si può verificare: `journeys.answer_set` contiene solo le dimensioni;
  - quando P3 si arma alle 08:00 di agenda, il booking se ne accorge solo dopo una nuova risposta a `rules`, perché `_RULES` è per processo (B:95).
- **Il piano va già in questa direzione:** lo step 3 (`calendar.py`) prevede «niente riserva U senza priorità U».

**5.2 Il booking ignora le preferenze passate dal gp.**
- **Cosa manda il gp:**
  - `doctor_lang=`, `gender_preference=` e `days=` nel passaggio (G:643-653 `_preference_tokens`, G:1027-1030 `_handoff`);
  - al cittadino dice «ho già inviato la ricetta… insieme alle sue preferenze» (G:215-271, `reply`).
- **Cosa legge il booking:**
  - `_days` e `_gender` (B:2358-2397) leggono solo le righe dei `citizen:`;
  - `doctor_lang` non lo legge nessuno: `_lang` (B:2338-2355) dal gp prende solo `lang`.
- **L'effetto:** dalla 1.0.17 la prima offerta parte subito dal passaggio, quando le preferenze passate dal gp sono le uniche note. Le offerte finiscono in `S` invece che in `S_pref`: +1 invece di +2 (R:230-231).

**5.3 Check-in rifiutato fuori fascia.**
- `_WINDOW_CODE` (F:1797) cerca `WINDOW|EARLY|LATE|TIME|CLOSED|HOUR|DAY` e non riconosce il codice vero, `POLICY_VIOLATION detail=outside`.
- Così F:2744 finisce nel ramo generico (F:2789-2798), che dice «mi riscriva tra poco e riprovo» invece di indicare la fascia della visita.
- J copre il caso con un codice inventato (§4).

**5.4 Minori.**
- **Index:** a ogni benvenuto `_tell_gp` (F:2961-2996) chiama `city.find("gp")` anche dall'ospedale, che però non vede il gp (`city_index/families.py:14`): una chiamata all'Index sprecata. Per lo specialista funziona.
- **Misure:** due `city.say` diretti del booking non passano da `tools.said`: B:1053 (il `confirm` alla struttura) e B:1439 (le richieste ai partner). In `episodes` risultano 0 `confirm` e 0 `need.request` del booking, quindi questi passi non si misurano.

## Dubbi

- I rami morti del consultorio e dello specialista (§1) servono solo a casi aperti con una versione vecchia. A ore dai deploy delle 1.0.9 e 1.0.11, non dovrebbero essercene.
- `episodes` parte alle 20:03 UTC, e il booking ci scrive dalle 00:57: un ramo «mai visto» può essere partito prima.
- §5.1: il punteggio della riserva U senza P3 attiva non è verificabile da qui. Va deciso con il kit alla mano.
