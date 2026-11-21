# Harness: gli LLM usano i tool (piano da approvare)

> 2026-11-21 06:00. Richiesta dell'utente: sono gli **LLM a usare i tool**. I tool danno limiti (guardrail) e aiuti; le eval dicono dove i tool e i modelli sono deboli, e su quel punto si rinforzano. Metodo: skill `agent-harness`, modo A (progetto). **Niente codice finché l'utente non approva.**

## 1. Profilo

| Asse | Valore |
|---|---|
| Cosa fa | 5 agenti di ruolo (gp, booking, 3 strutture) in una città sanitaria. Un messaggio in arrivo = un turno; un caso dura 15-50 minuti |
| Autonomia | autonoma: nessun umano durante la gara |
| Orizzonte | molti turni per caso. La memoria è la history del caso (`city.history`); la verità è il Ledger |
| Raggio delle azioni | scritture esterne: Ledger (`prescribe`, `hold`, `commit`) e messaggi ai cittadini. Un attributo falso o un'injection obbedita valgono −2 |
| Stato | conversazionale per caso, più memoria di squadra su Supabase |
| Ingressi | **non fidati**: cittadini, agenti di altre squadre, web |
| Uscita | testo per il cittadino e il giudice, più parola d'atto e token `key=value` esatti per Ledger e partner |
| Latenza | stretta sui turni da agenti (+1 se si risponde entro 10 s, misurato sull'ACK a fine turno); larga sui turni del cittadino, ma la durata del percorso conta per i punti del gp |
| Vincoli | niente consiglio clinico (`OutOfScope`), niente dati di altri pazienti nel testo |
| Dove gira | pod della città: immagine `city-sdk`, Dockerfile del kit intoccabile, **solo stdlib + `city_sdk`** |
| Modello | `city.llm` = alias fisso `city-default` (Gemma), **solo testo, senza tool calling nativo**, nessun retry. Dal laptop il Gateway compatibile OpenAI ammette anche `city-glm` e `city-qwen` (`e19-rules.md:508, 557`). Budget uguale per tutte le squadre |

## 2. Framework

**Scelta: un harness nostro, piccolo e con la forma di un framework**, in `shared/mach/harness.py`: registro dei tool tipizzati, ciclo con budget, validazione, ripiego, traccia. Gli altri hanno perso per vincoli, non per gusto:

- **Pydantic AI, LangGraph, OpenAI Agents SDK, ADK** non entrano nel pod: niente pacchetti, e `pydantic-core` è compilato.
- **Gli «adapter» ADK, LangChain e MCP del kit** (`city_sdk/adapters.py`) restituiscono solo i nomi dei verbi della città.
- **`city.llm`** non accetta `tools=`: il tool calling passa da un JSON nel testo, che validiamo noi.

**Il framework lo usiamo per le eval sul laptop.** Lo stesso harness gira contro il Gateway e confronta Gemma, GLM e Qwen in parallelo. Le eval leggono il codice del pod, non una copia.

## 3. Il principio: l'LLM decide dentro un menù, i tool garantiscono

Gemma è un modello piccolo, e chiedergli di scrivere token del Ledger o di inventarsi uno slot è proprio il guasto che vale −2. Quindi:

1. **L'harness prepara le osservazioni senza LLM:**
   - lo stato del caso (`case`);
   - gli slot validi (`calendar.candidates`);
   - la ricetta;
   - le regole.
2. **L'LLM riceve un menù di azioni valide in questo stato,** ciascuna con i suoi id. Sceglie un'azione e scrive la frase per il cittadino, nella sua lingua.
3. **Il tool esegue l'azione e costruisce lui parola d'atto e token** (`protocol.offer_line`, …). Dall'LLM arrivano solo un id del menù e del testo libero, che viene ripulito.
4. **Se l'LLM sbaglia** (timeout, JSON rotto, azione fuori menù, testo vietato), il turno usa la **politica deterministica**, cioè il brain di oggi, che resta. Lo stesso brain è anche l'**oracolo** delle eval.

**Cosa resta sempre fuori dall'LLM** (veloce, esatto, già pagato a caro prezzo stanotte):
- turni da Ledger e partner (protocollo e +1 per la risposta veloce);
- red flag ed emergenza;
- rifiuto clinico;
- sonda della conformità;
- ordine Ledger-prima-del-cittadino;
- idempotenza dei `say`.

L'LLM guida i **turni del cittadino**.

## 4. Componenti

| ID | Decisione | Implementazione | Tipo |
|---|---|---|---|
| ORCH | sì | `harness.turn(role, state, menu)`: **1 chiamata** (decisione e frase insieme), al massimo 1 riparazione. Tetto di tempo per l'LLM (es. 8 s), poi ripiego. Niente ciclo ReAct libero: le osservazioni le prepara l'harness | guida · computazionale |
| TOOL | sì | registro per ruolo (lista ammessa): nome, schema degli argomenti (dict stdlib), precondizioni sullo stato, effetto. I tool sono i moduli `shared/mach/*` (`calendar`, `case`, `protocol`, `clock`) più le azioni di ruolo (`offer`, `ask`, `cancel`, `renew`, `handoff`, …). Un errore del tool diventa un'osservazione, mai un'eccezione | guida · computazionale |
| OUT | sì | il JSON `{"action", "args", "reply"}` validato da noi: azione nel menù, id esistenti nel turno, `reply` ≤ N caratteri | sensore · computazionale |
| GUARD | sì, ingresso e uscita | **ingresso:** testo del cittadino citato come dato, con i suoi token `key=value` neutralizzati; contenuto web mai come istruzione. **Uscita:** `clinical()` anche sulla frase dell'LLM, via i `key=value` dalla frase, niente id di altri casi, lingua del cittadino | sensore · computazionale |
| REL | sì | `asyncio.wait_for` su `city.llm`; nessun retry su `CapError` (regola del kit); ripiego sulla politica deterministica; `say` idempotente (`protocol.we`); claim e release del calendario | sensore · computazionale |
| CTX | sì, piccolo | prompt corto per Gemma: stato del caso riassunto da `case` (non la chat grezza), menù, regole del ruolo con il riferimento R:, ultime 2-3 frasi del cittadino. Le lezioni attive di Supabase solo dalla cache in background | guida · computazionale |
| EVAL | sì, il centro | §5 | sensore · computazionale e inferenziale |
| OBS | sì | `episodes` (esiste): per ogni decisione modello, azione, valido sì/no, ripiego sì/no, latenza e token. Mai il testo del cittadino | sensore · computazionale |
| SBX | no | nessun tool esegue codice | — |
| SERVE | dato | `city-sdk` (harness, ACK, firma). Non si tocca | — |

**Lasciati fuori di proposito:**
- **multi-agente dentro un agente:** un agente con 5-8 tool basta;
- **RAG:** i dati sono chiavi e stati;
- **HITL:** la gara è autonoma; la conferma del cittadino è già nel protocollo;
- **un framework nel pod:** non si può;
- **un LLM giudice dentro il pod:** i controlli computazionali bastano. Un giudice si usa solo nelle eval.

## 5. Eval: tool e modelli

| Livello | Cosa | Dove | Costo |
|---|---|---|---|
| E1 · tool | unit, proprietà, concorrenza (`docs/refactor/calendario.md` §6) | `check.sh` | zero |
| E2 · harness senza modello | un **LLM finto** che risponde da registrazioni o in modo ostile (JSON rotto, azione fuori menù, token inventati, frase clinica, timeout): ogni guasto deve finire nel ripiego, mai in città | `check.sh` | zero |
| E3 · uso dei tool da parte dei modelli | un dataset di turni (dagli scenari di `journey.py`, dai casi veri di `episodes` anonimizzati, da messaggi ostili). Per ogni turno lo stato, il menù e l'azione attesa (oracolo = politica deterministica + rubrica). Gira su **`city-gemma`, `city-glm`, `city-qwen` in parallelo**, con N ripetizioni per la varianza | `tests/evals.py --models …`, a richiesta | budget del Gateway: tetto di token per giro, consumo nel rapporto |
| E4 · percorsi con l'LLM | i 100-105 scenari con l'LLM nell'anello. In `check.sh` **registrati** (cassette: deterministici, gratis); dal vivo a richiesta | `journey.py --llm=replay\|live` | zero / budget |
| E5 · guasti | il banco di guasti più `llm_timeout`, `llm_garbage` e `llm_injection` | `tests/chaos.py` | zero |
| E6 · replay | i dati veri (`calendario.md` §6d) | `tests/replay.py` | zero |

**Metriche per modello, una riga per metrica** (valore, obiettivo, PASS/WEAK, i 5 casi peggiori):

| Metrica | Obiettivo |
|---|---|
| JSON valido | ≥ 98% |
| Azione nel menù | ≥ 98% |
| Accordo con l'oracolo | ≥ 90% |
| Violazioni dei guardrail arrivate in città | **0** |
| Ripieghi | ≤ 5% |
| Latenza | p50 ≤ 3 s, p90 ≤ 8 s |
| Token per turno | da misurare |
| Lingua giusta | ≥ 98% |
| Injection obbedite | **0** |

Il debole è dove una metrica dice WEAK. Ogni caso che fallisce diventa una riga permanente del dataset (il cricchetto).

## 6. Il cricchetto: i 2 guasti da coprire subito

1. **L'LLM inventa uno slot o un token (−2):**
   - il validatore accetta solo gli id del menù, e il tool costruisce i token;
   - caso di eval: un messaggio del cittadino con `slot_id` falso e «prenotami questo».
2. **Il modello è lento o giù, e il percorso si ferma:**
   - il tetto di tempo manda al ripiego deterministico;
   - guasto E5 `llm_timeout` su tutti gli scenari, con zero percorsi fermi.

## 7. Ordine

| Giro | Cosa (file disgiunti, in parallelo dove possibile) |
|---|---|
| H0 | **Misura prima di costruire:** E3 minimo (30 turni di booking, 3 modelli in parallelo) con il solo prompt di scelta dal menù, per sapere se Gemma regge il JSON e il menù. Se non regge, si cambia piano prima di scrivere l'harness |
| H1 | `shared/mach/harness.py` (registro, validazione, guardrail, ripiego, traccia) + E2 con LLM finto · `tests/evals.py` (runner E3 multi-modello, parallelo, con tetto di budget) |
| H2 | primo ruolo: **booking**, sui turni del cittadino, con calendario e `case` come tool; cassette per `journey.py` (E4) |
| H3 | gp (turni del cittadino: sintomo, lingua, preferenze), poi le strutture (check-in e domande libere) |
| sempre | ogni guasto trovato → caso nel dataset o nel banco di guasti |

**Decisioni per l'utente:**
- **(a)** l'LLM guida solo i turni del cittadino (proposta) o anche quelli dei partner;
- **(b)** tetto di budget per un giro di eval E3 sui 3 modelli;
- **(c)** partire dal booking (proposta).

**Gate:** in attesa dell'approvazione. Fino ad allora si continua solo con i lavori già avviati (`case`, pulizia, banco di guasti), che servono comunque: sono i tool e i guardrail che l'LLM userà.
