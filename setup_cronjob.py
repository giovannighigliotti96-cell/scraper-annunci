#!/usr/bin/env python3
"""Crea (o aggiorna) su cron-job.org i job che innescano i workflow GitHub.

Perché esiste: il cron di GitHub Actions slitta di 2-4.6 ore sull'orario
richiesto (misurato su 12 giorni), mentre un workflow_dispatch via API parte in
circa 5 secondi. cron-job.org fa da sveglia puntuale, e lavorando in fuso
Europe/Rome gestisce da solo il cambio di ora legale. Vedi SCHEDULING.md.

Uso:
    python setup_cronjob.py            elenca i job esistenti e mostra cosa farebbe
    python setup_cronjob.py --applica  crea davvero i job mancanti

Servono due chiavi nel .env (nessuna delle due finisce nel repo, che è gitignored):
    CRONJOB_API_KEY   da cron-job.org -> Settings -> API
    GH_DISPATCH_TOKEN PAT GitHub fine-grained con Actions: read and write

Lo script è idempotente: riconosce i job già creati dal titolo e non li duplica.
"""
import os
import sys
import time
import json
import urllib.request
import urllib.error

from dotenv import load_dotenv

load_dotenv(override=True)

CRONJOB_API = "https://api.cron-job.org"
REPO = "giovannighigliotti96-cell/scraper-annunci"
FUSO = "Europe/Rome"

# Orari in ora italiana. Gli stessi che prima erano cron entry di GitHub, ma qui
# vengono rispettati davvero. Lo scraping gira quattro volte per intercettare gli
# annunci nell'arco della giornata; l'email chiude la giornata alle 18:05.
JOB = [
    ("Scraper — scraping 09:15", "scraping.yml", 9, 15),
    ("Scraper — scraping 12:15", "scraping.yml", 12, 15),
    ("Scraper — scraping 15:15", "scraping.yml", 15, 15),
    ("Scraper — scraping 17:30", "scraping.yml", 17, 30),
    ("Scraper — email riepilogo 18:05", "email.yml", 18, 5),
]


def _chiama(metodo, percorso, api_key, corpo=None):
    dati = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(
        f"{CRONJOB_API}{percorso}", data=dati, method=metodo,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            testo = r.read().decode()
            return r.status, (json.loads(testo) if testo.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, {"errore": e.read().decode()[:300]}


def definizione_job(titolo, workflow, ora, minuto, gh_token):
    """Un job che chiama l'endpoint workflow_dispatch di GitHub.

    requestMethod 1 = POST. In schedule, -1 significa "ogni": ogni giorno del
    mese, ogni mese, ogni giorno della settimana — cioè tutti i giorni all'ora
    indicata, nel fuso specificato."""
    return {
        "job": {
            "url": f"https://api.github.com/repos/{REPO}/actions/workflows/{workflow}/dispatches",
            "title": titolo,
            "enabled": True,
            "saveResponses": True,  # utile per capire un fallimento silenzioso
            "requestMethod": 1,
            "extendedData": {
                "headers": {
                    "Authorization": f"Bearer {gh_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Content-Type": "application/json",
                },
                "body": '{"ref":"main"}',
            },
            "schedule": {
                "timezone": FUSO,
                "hours": [ora],
                "minutes": [minuto],
                "mdays": [-1],
                "months": [-1],
                "wdays": [-1],
            },
        }
    }


def main():
    applica = "--applica" in sys.argv
    api_key = os.getenv("CRONJOB_API_KEY", "").strip()
    gh_token = os.getenv("GH_DISPATCH_TOKEN", "").strip()

    mancanti = [n for n, v in (("CRONJOB_API_KEY", api_key),
                               ("GH_DISPATCH_TOKEN", gh_token)) if not v]
    if mancanti:
        print(f"Mancano nel .env: {', '.join(mancanti)}")
        return 1

    stato, risposta = _chiama("GET", "/jobs", api_key)
    if stato != 200:
        print(f"Impossibile leggere i job esistenti (HTTP {stato}): {risposta}")
        return 1

    esistenti = {j.get("title"): j for j in risposta.get("jobs", [])}
    print(f"Job già presenti su cron-job.org: {len(esistenti)}")
    for titolo in esistenti:
        print(f"   - {titolo}")
    print()

    creati = saltati = falliti = 0
    for titolo, workflow, ora, minuto in JOB:
        if titolo in esistenti:
            print(f"  = {titolo} — già presente, non lo tocco")
            saltati += 1
            continue
        if not applica:
            print(f"  + {titolo} — da creare ({ora:02d}:{minuto:02d} {FUSO} -> {workflow})")
            continue
        # Pausa tra una creazione e l'altra: l'API di cron-job.org risponde 429
        # se le richieste arrivano ravvicinate (osservato dal vivo: 2 job su 5
        # rifiutati creandoli in sequenza senza attesa).
        if creati:
            time.sleep(3)
        stato, risposta = _chiama("PUT", "/jobs", api_key,
                                  definizione_job(titolo, workflow, ora, minuto, gh_token))
        if stato in (200, 201):
            print(f"  + {titolo} — creato (id {risposta.get('jobId', '?')})")
            creati += 1
        else:
            print(f"  ! {titolo} — FALLITO (HTTP {stato}): {risposta}")
            falliti += 1

    print()
    if applica:
        print(f"Creati: {creati} | già presenti: {saltati} | falliti: {falliti}")
    else:
        print("Anteprima soltanto. Rilancia con --applica per creare i job.")
    return 1 if falliti else 0


if __name__ == "__main__":
    sys.exit(main())
