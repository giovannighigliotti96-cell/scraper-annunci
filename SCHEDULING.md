# Come viene innescata l'automazione

## Perché non basta il cron di GitHub Actions

I workflow schedulati di GitHub sono dichiaratamente *best-effort*: partono quando
c'è capacità, non all'orario richiesto. Misurato su questo repo, 12 giorni
consecutivi di settembre 2026:

| Orario richiesto | Partenza reale | Ritardo |
|---|---|---|
| 18:05 | 21:46 | 3.7 h |
| 18:05 | 22:23 | 4.3 h |
| 18:05 | 20:56 | 2.9 h |
| 18:05 | 20:53 | 2.8 h |

Media ~3 ore, punta 4.3. Lo stesso vale per lo scraping, dove il danno è
maggiore: l'alert immediato per le offerte ad alta affinità serve a candidarsi
lo stesso giorno, e un ritardo di mezza giornata lo rende inutile.

Un `workflow_dispatch` via API parte invece **in circa 5 secondi** (misurato).

## Architettura

**Trigger puntuale: cron-job.org** chiama l'API GitHub `workflow_dispatch` agli
orari voluti. Lavora in fuso orario locale, quindi gestisce da solo il cambio di
ora legale — il problema delle doppie cron entry stagionali sparisce alla radice.

**Rete di sicurezza: le cron entry di GitHub**, rimaste nei workflow ma
volutamente tardive. Coprono il caso in cui il trigger esterno non arrivi
(servizio giù, token scaduto). In condizioni normali trovano il lavoro già fatto
ed escono subito, grazie a due guard indipendenti dall'orario:

- `email_gia_inviata_oggi()` — legge `ultimo_invio.json`, che registra la data
  dell'ultimo riepilogo effettivamente spedito. Controlla il *fatto*, non l'ora.
- lo step "Verifica distanza dall'ultimo scraping" — salta i run a meno di 90
  minuti dall'ultimo commit di stato.

Il doppio meccanismo è deliberato: se il trigger esterno smette di funzionare in
silenzio (è già successo con il repo `scraper-scheduler`, fermo dal 24/08/2026
con il PAT scaduto), l'automazione continua a girare, solo in ritardo.

## Configurazione di cron-job.org

### 1. Il token GitHub

Serve un **fine-grained personal access token**:

- https://github.com/settings/personal-access-tokens/new
- *Repository access* → **Only select repositories** → `scraper-annunci`
- *Permissions* → *Repository permissions* → **Actions: Read and write**
- *Expiration*: la scadenza è la causa più probabile di guasto silenzioso.
  Segna in calendario la data di rinnovo, oppure scegli la scadenza più lunga
  disponibile.

Nient'altro: il token non deve poter scrivere codice, solo avviare workflow.

### 2. I cinque job

Su https://cron-job.org, per ognuno degli orari sotto crea un job con:

- **URL**
  `https://api.github.com/repos/giovannighigliotti96-cell/scraper-annunci/actions/workflows/<FILE>/dispatches`
  dove `<FILE>` è `scraping.yml` oppure `email.yml`
- **Method**: `POST`
- **Timezone**: `Europe/Rome`
- **Headers**
  - `Authorization: Bearer <IL_TUO_TOKEN>`
  - `Accept: application/vnd.github+json`
  - `X-GitHub-Api-Version: 2022-11-28`
  - `Content-Type: application/json`
- **Body**: `{"ref":"main"}`

| Job | Orario (Europe/Rome) | Workflow |
|---|---|---|
| Scraping mattina | 09:15 | `scraping.yml` |
| Scraping mezzogiorno | 12:15 | `scraping.yml` |
| Scraping pomeriggio | 15:15 | `scraping.yml` |
| Scraping sera | 17:30 | `scraping.yml` |
| Email riepilogo | 18:05 | `email.yml` |

La risposta attesa è **204 No Content**: l'API non restituisce corpo. In
cron-job.org conviene impostare "treat 2xx as success", altrimenti un 204 può
essere segnalato come anomalia.

### 3. Verifica

Dopo aver salvato, usa "Run now" su un job e controlla che compaia un run con
event `workflow_dispatch` negli Actions del repo. Se ricevi:

- **404** — il token non vede il repo, o manca il permesso Actions
- **403** — permesso insufficiente (serve *write*, non solo *read*)
- **422** — il `ref` non esiste: il branch deve essere `main`

## Il repo scraper-scheduler

Il repo pubblico `scraper-scheduler` faceva la stessa cosa **usando il cron di
GitHub Actions**, quindi ereditava esattamente il ritardo che doveva risolvere.
È fermo dal 24/08/2026 e fallisce a ogni run (`curl` exit 22: PAT non più
valido). Con cron-job.org attivo non serve più: va archiviato, per non lasciare
in giro un secondo meccanismo che sembra attivo e non lo è.
