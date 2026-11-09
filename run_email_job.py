#!/usr/bin/env python3
"""Script one-shot per inviare la mail giornaliera e svuotare le offerte accumulate."""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from scraper import invia_email_job, valida_credenziali_email, email_gia_inviata_oggi, ats_in_attesa

# Il workflow schedula molti tentativi per aggirare il ritardo del cron di
# GitHub Actions: qui ci si ferma se il riepilogo di oggi e' gia' partito, cosi'
# i tentativi successivi non producono una seconda email.
if email_gia_inviata_oggi():
    print("Riepilogo di questa giornata gia' inviato: non serve un altro invio.")
    sys.exit(0)

# Il giro degli ATS diretti (in locale) ha dichiarato che le sue offerte arriveranno in questa mail: si aspetta. Chi le consegna fa
# ripartire questo workflow subito dopo (pubblica_offerte_ats.py); in ogni caso dopo le 23:30 la mail parte comunque.
# Uscita senza sentinella: la coda resta intatta, come per "gia' inviata".
if ats_in_attesa():
    print("In attesa degli ATS diretti: non invio ora. La mail parte appena sono consegnati (al piu' tardi alle 23:30).")
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
