#!/usr/bin/env python3
"""Script one-shot per inviare la mail giornaliera e svuotare le offerte accumulate."""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from scraper import invia_email_job, valida_credenziali_email, email_gia_inviata_oggi

# Il workflow schedula molti tentativi per aggirare il ritardo del cron di
# GitHub Actions: qui ci si ferma se il riepilogo di oggi e' gia' partito, cosi'
# i tentativi successivi non producono una seconda email.
if email_gia_inviata_oggi():
    print("Riepilogo di questa giornata gia' inviato: non serve un altro invio.")
    sys.exit(0)

if not valida_credenziali_email():
    print("ERRORE: GMAIL_APP_PASSWORD non valida o mancante.")
    sys.exit(1)

invia_email_job()

# Sentinella per il workflow: solo se siamo arrivati fin qui l'invio e' davvero
# avvenuto, e solo allora ha senso togliere dalla coda le offerte spedite.
# Senza questo marcatore lo step di salvataggio svuotava la coda anche quando
# lo script usciva prima di mandare qualcosa, e le offerte sparivano senza
# essere mai arrivate (successo il 01/10/2026).
with open("/tmp/email_inviata", "w", encoding="utf-8") as f:
    f.write("1")
