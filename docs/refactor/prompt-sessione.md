# Prompt per la sessione di refactoring

Incolla il testo qui sotto come primo messaggio di una sessione nuova di Claude Code aperta in `/Users/thomas/Documenti/GitHub/mach`.

---

Sei l'orchestratore del refactoring degli agenti di Milano Agentic City. Il codice lo scrivono i subagent: tu scomponi, deleghi, verifichi, integri e tieni aggiornato `HANDOFF.md`. Le regole di lavoro sono in `AGENT.md` e `CLAUDE.local.md`, e valgono tutte: massimo 3 subagent attivi insieme, brief autosufficienti, verifichi tu prima di dire «fatto».

## 1. Riallineati prima di tutto

- `HANDOFF.md` è già nel contesto. Leggi per intero `docs/refactor/piano.md`: è il piano da eseguire, step 0-7 con file e criteri di «fatto».
- Lancia `git status --short`, `git fetch` e `git log --oneline -10 origin/master`. Il collega giuseppesica può pushare sul booking: se master è avanti, fai merge prima di tutto.
- Controlla le versioni live:
  ```
  for a in gp booking_centre hospital specialist_care family_counseling; do city status $a | grep Deployed; done
  ```
  Confrontale con le card e con la tabella «Stato» di `HANDOFF.md`.
- Gli agenti in città funzionano e fanno punti. **Il refactoring non deve mai peggiorarli.** Ogni step non cambia il comportamento, salvo correzioni dichiarate.

## 2. Obiettivo

Fondamenta stabili e leggibili, allineate all'architettura del kit:
- la history del caso è la verità;
- un solo modo di leggere i messaggi: `understood()` dell'SDK più la regola della prima parola d'atto;
- un solo `say` che gestisce tutti gli errori della città;
- smistamento per (chi scrive, parola d'atto);
- lo stato in memoria è solo cache dichiarata;
- il turno non aspetta il Ledger.

I perché, i numeri e i vincoli sono nel piano. Non riaprire le decisioni prese lì.

## 3. Ordine e parallelismo

Massimo 3 subagent attivi. I subagent che scrivono lavorano ciascuno nel suo worktree (`isolation: "worktree"`) e **non condividono file**. I test nuovi vanno in file nuovi (`tests/unit/test_<modulo>.py`), così i conflitti si riducono. Se un subagent deve per forza toccare `tests/journey.py`, aggiunge un blocco in coda; tu integri con `git merge-file` usando la base, tieni entrambi i blocchi e chiudi ogni `SCENARIOS.update({...})`.

| Giro | Subagent in parallelo | Dipende da |
|---|---|---|
| A | **Step 0** da solo: `tests/check.sh`, fixture vere da `episodes` in `tests/fixtures/` (solo SELECT su Supabase, niente dati personali), baseline dei conteggi | — |
| B | **Step 1** (`shared/protocol.py` + `tests/unit/test_protocol.py`)<br>**Step 3** (`shared/clock.py`, `shared/world.py` + unit test)<br>**Step 6** (misure: gp su `episodes`, `sent_at` delle risposte del Ledger) | A |
| C | **Step 2** (`shared/case.py` + `tests/unit/test_case.py`) | 1 |
| D | **Step 4+5** sul booking (`booking_centre/agent/agent.py`)<br>**Step 4+5** sulle strutture (`shared/facility*.py`)<br>I due subagent non toccano gli stessi file | 1, 2, 3 |
| E | **Step 4+5** sul gp, poi **step 7** (pulizia) | D in città senza errori |

Il primo compito di B, C, D ed E è **adottare** i moduli comuni negli agenti: importare `protocol`, `case`, `clock` e `world`, e cancellare i duplicati. Costruire i moduli senza usarli non conta come fatto.

Dopo ogni modulo nuovo in `shared/`, aggiorna `shared/sync.sh` perché lo copi nelle cartelle degli agenti che lo usano, con il controllo `cmp`. Le copie non si modificano mai a mano.

## 4. Brief per ogni subagent

Nel brief metti:
- l'obiettivo e il criterio di «fatto» dello step, copiati dal piano;
- i file da leggere: `AGENT.md`, `HANDOFF.md` (sezione «Gotcha»), `docs/refactor/piano.md`, `mach-hackathon-kit/city-sdk/src/city_sdk/city.py`, `mach-hackathon-kit/templates/examples/agent.py`, i file da cambiare;
- i vincoli:
  - `mach-hackathon-kit/` in sola lettura;
  - solo stdlib più `city_sdk`;
  - niente segreti;
  - niente commit, push o deploy;
  - comportamento invariato;
- la verifica: `bash tests/check.sh`, o finché non esiste le 4 modalità di `tests/journey.py` (`~/.local/share/uv/tools/city-cli/bin/python -B tests/journey.py` con `--ledger-turns` e/o `--ledger-format sdk`), più `uv run --no-project --with city-sdk==0.9.202 --with pytest python -m pytest tests/specialist tests/consultorio family_counseling/tests -q` e `city card validate <agente>`;
- il formato del report: file cambiati con `file:riga`, i conteggi dei test prima e dopo, i dubbi.

## 5. Verifica, commit e deploy

- **Verifica tu:** il diff, `check.sh` sul repo principale dopo l'integrazione, i conteggi uguali o migliori della baseline e 0 FAIL.
- **Commit e push** di ogni step, file per file. Mai `git add -A`: nel repo ci sono file dell'utente non tracciati.
- **Deploy:**
  - L'utente ha detto: «gli agenti sono nostri, se dobbiamo aggiornare aggiorniamo». Quindi dopo i check verdi deploya senza chiedere, con la procedura di `HANDOFF.md` (worktree di deploy, loop di retry, card registrata dopo `promoted`).
  - Ordine: consultorio, specialista, ospedale, booking, gp. Tra un agente e il successivo aspetta 10 minuti e controlla che non ci siano errori in `episodes`.
  - Dopo il deploy di booking e gp rifai `city scale <agente> --min 1 --max 1 --concurrency 40`; per l'ospedale usa concurrency 20.
  - **Chiedi conferma all'utente prima di deployare il gp**: fa quasi tutti i punti.
  - Dopo ogni `promoted`: `city conform --url` sull'agente. Su un FAIL fai rollback (`city rollback agent:<id> --to <versione>`) e torni allo step.

## 6. Chiusura di ogni giro

- Aggiorna `HANDOFF.md`: stato degli agenti, step fatti, decisioni con il perché in una riga, gotcha nuovi. Resta sotto le ~200 righe e scrivi solo ciò che è vero adesso.
- Segna lo stato nel piano: `docs/refactor/piano.md` ha una colonna per step che dice fatto o in corso, con il commit.
- Riassumi all'utente in poche righe cosa è cambiato, cosa è in città e cosa resta.

Comincia dal punto 1, poi mostrami il piano del giro A in massimo 5 righe e parti.
