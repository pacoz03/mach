# Percorsi veri della città, anonimizzati

Fonte: console `team/journeys/<need_id>` della wave W01QQQ, 2026-11-21 04:02-05:23 UTC (v1 fino a 04:16 per il gp e fino a 04:22 per il centro, poi v2). La wave ha chiuso intorno alle 05:17-05:23. Un file JSON per classe di errore, con 1-3 casi.

- Il file ha `class`, `summary`, `count` (percorsi della classe sui 193 letti), `roles`, `v1` (si applica? `file:riga`), `covered_by` e `cases`.
- Il caso ha `source` (livello, stato, punteggio, `S_size` e `S_pref_size` del giudice, versione per ruolo), `rubric_lost`, `rubric_kept`, `ledger_facts` (hold, cancellazioni ed escalation del verdetto), `messages` ed `expected`.
- Ogni messaggio ha `t` (secondi reali dal primo), `agenda` (ora di agenda calcolata dall'àncora della wave, weekend saltati), `from`/`to` per ruolo, `acts`, `tokens` (`key=value` in ordine), `text`, `status` e, per i nostri agenti, `version`.
- Il testo del cittadino è **riscritto**: stessa lingua, intenzione e parole chiave, seguito dall'atto e dai token veri. Il testo dei nostri agenti è il loro, tagliato a 600 caratteri.
- Gli id sono finti e coerenti nel file: `N-TEST-<tag>-NN`, `citizen:T-<tag>-NN`, `prescription_number-FAKE<TAG>NN`, `bk-FAKE-<tag>-NN`. Slot, medici, offer_id, date e id degli agenti restano veri: sono dati del mondo, non del cittadino.
- La console mostra solo i messaggi tra cittadino e nostri agenti (niente Ledger né partner) e a volte solo l'ultima busta di un messaggio che il cittadino ha rimandato.
- Il grezzo non sta nel repo. Si rigenera dalla console in sola lettura; gli script stanno nello scratchpad della sessione, non qui.
