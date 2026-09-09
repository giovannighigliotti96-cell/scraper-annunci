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
    print("Riepilogo di oggi gia' inviato: non serve un altro invio.")
    sys.exit(0)

if not valida_credenziali_email():
    print("ERRORE: GMAIL_APP_PASSWORD non valida o mancante.")
    sys.exit(1)

invia_email_job()
