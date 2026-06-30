#!/usr/bin/env python3
"""Script one-shot per inviare la mail giornaliera e svuotare le offerte accumulate."""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from scraper import invia_email_job, valida_credenziali_email

if not valida_credenziali_email():
    print("ERRORE: GMAIL_APP_PASSWORD non valida o mancante.")
    sys.exit(1)

invia_email_job()
