# -*- coding: utf-8 -*-
"""Test di regressione del motore, nati dalla diagnosi del 05/11/2026.

Si lanciano con `python test_regressioni_motore.py` (niente pytest: la repo non lo
usa). Nessuna rete, nessun LLM, nessuno stato scritto.

Ogni test blocca un difetto trovato rigiocando i filtri su 251 offerte vere:

1. Le offerte "da remoto" venivano scartate dal filtro finale SENZA nessun log,
   prima che Groq le leggesse. Oggi erano 5, fra cui un Sales Manager Hays a
   Milano a 55-80k con "la presenza in sede NON e' obbligatoria".

2. Il prompt chiede al modello, per un annuncio troppo scarno, un punteggio
   "intorno a 50", e il filtro scarta tutto cio' che sta SOTTO 50: il modello
   risponde 45 e l'offerta sparisce. 9 offerte in 4 settimane sono finite
   esattamente al 45%, fra cui una a Genova.
"""
import re
import sys
from datetime import date

import scraper as s

OGGI = date.today().isoformat()
fallimenti = []


def verifica(nome, condizione, dettaglio=""):
    if condizione:
        print(f"  ok     {nome}")
    else:
        print(f"  FALLITO {nome}  {dettaglio}")
        fallimenti.append(nome)


def offerta(citta, modalita, titolo="Sales Manager", testo=None):
    testo = testo or f"{titolo}. Sede di lavoro: {citta}. Cerchiamo una persona per il team commerciale."
    j = s.ScrapedJob(titolo, "Azienda Prova", "LinkedIn", f"https://esempio.it/{citta}/{modalita}",
                     date=OGGI, city=citta, work_mode=modalita, probabilita=60, motivazione="")
    j.testo_completo = testo
    return j


print("Difetto 1 - il filtro per modalita' non butta piu' le offerte 'da remoto'")
for citta in ("Milano", "Genova"):
    cfg = s.CITIES[citta]
    for modalita in ("in sede", "ibrido", "da remoto"):
        passate = s.filtra_offerte_per_citta([offerta(citta, modalita)], cfg)
        verifica(f"{citta} / {modalita} arriva all'LLM", len(passate) == 1,
                 "(scartata in silenzio)")

# la correzione non deve aprire altre porte: la geografia resta un filtro
altrove = offerta("Milano", "da remoto", titolo="Marketing Manager",
                  testo="Marketing Manager. " + "Bergamo, sede di Bergamo. " * 12)
verifica("una 'da remoto' che l'annuncio colloca a Bergamo viene comunque scartata",
         len(s.filtra_offerte_per_citta([altrove], s.CITIES["Milano"])) == 0)

print("\nDifetto 2 - il prompt non chiede un punteggio che il filtro scarta")
m = re.search(r"troppo scarno[^\n]*?punteggio\s+(?:intorno a |di |pari a )?(\d+)",
              s._MATCH_LLM_SYSTEM_TEMPLATE)
verifica("il prompt dice quale punteggio dare a un annuncio scarno", m is not None)
if m:
    punteggio_scarno = int(m.group(1))
    verifica(f"punteggio per annuncio scarno ({punteggio_scarno}) >= soglia minima ({s.SOGLIA_MINIMA_PUNTEGGIO})",
             punteggio_scarno >= s.SOGLIA_MINIMA_PUNTEGGIO)
    j = offerta("Milano", "ibrido")
    j.valutato_da, j.probabilita = "llm", punteggio_scarno
    verifica("un'offerta valutata a quel punteggio non e' sotto soglia", not s.offerta_sotto_soglia(j))
verifica("il prompt non dice piu' 'intorno a 50'", "intorno a 50" not in s._MATCH_LLM_SYSTEM_TEMPLATE)

print()
if fallimenti:
    print(f"{len(fallimenti)} TEST FALLITI")
    sys.exit(1)
print("TUTTI I TEST PASSANO")
