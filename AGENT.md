# AGENT.md — istruzioni per qualsiasi assistente AI

Questo è il file unico di istruzioni del progetto. È indipendente dallo strumento: `CLAUDE.md` rimanda qui, così Claude Code, Cursor, Copilot, Codex o un umano leggono le stesse regole.

## Contesto

Hackathon "Milano Agentic City". Questo workspace contiene cinque agenti creati con `city init agent` e il kit pubblico dell'hackathon.

| Cartella | `role` (card.yaml e city.yaml) | `capabilities` |
|---|---|---|
| `booking_centre/` | `booking_centre` | `booking_centre` |
| `gp/` | `gp` | `gp` |
| `hospital/` | `facility` | `hospital.physiatry` |
| `specialist_care/` | `facility` | `specialist_care.cardiology` |
| `family_counseling/` | `facility` | `family_counseling.gynecology` |

`mach-hackathon-kit/` è una copia di sola lettura del kit pubblico (docs, sorgenti `city-sdk` e `city-cli`, template). Non ha `.git` proprio.

## Regole di lavoro

1. **Non modificare `mach-hackathon-kit/`.** Leggilo come riferimento, non cambiarlo.
2. In ogni agente modifica solo `card.yaml`, `city.yaml` e `agent/agent.py`. Lascia il `Dockerfile` com'è: non ci si caricano file di dati.
3. `role` in `card.yaml` e in `city.yaml` devono coincidere ed essere quelli della tabella sopra.
4. Il codice raggiunge la città solo tramite `city_sdk.city` (stdlib come `json` e `re` sono ammesse). Verbi disponibili: `city.find(query)`, `city.say(to, text)`, `city.history(...)`, `city.llm(messages, need_id=...)`.
5. Dati che non stanno nel codice: leggili da un'API o da una fonte esterna, non da file nel container.
6. Scrivi sul Ledger (`city:ledger`) prima di avvisare il cittadino. Nel testo usa la parola d'atto e i token `key=value` (es. `hold slot_id=S-12`).
7. Nessuna indicazione clinica: richieste cliniche vanno rifiutate (`raise OutOfScope()`).
8. Mai segreti nel repo. Le credenziali stanno in `~/.config/city/teams/<squadra>/`: non copiarle né committarle.

## Comandi

```bash
city login                         # una tantum
city card validate <cartella>      # valida la card
city run <cartella>                # test locale contro Index/Ledger in memoria
city deploy <cartella>             # rilascio (specialist_care prima di hospital)
```

Esegui i comandi dalla cartella padre degli agenti (o usa `--root` con percorso assoluto).

## Dove leggere

- `mach-hackathon-kit/docs/guides/develop-agents.md` — percorso login → init → deploy
- `mach-hackathon-kit/docs/kit/e19-rules.md` — regole del care journey
- `mach-hackathon-kit/docs/kit/levels.md` — gli otto livelli
- `mach-hackathon-kit/docs/kit/<ruolo>.md` — note per ruolo
- `<agente>/SKILL.md` — richieste che il Ledger capisce
- `mach-hackathon-kit/templates/examples/agent.py` — brain di esempio con il care protocol
