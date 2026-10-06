# -*- coding: utf-8 -*-
"""Rivaluta con le regole attuali offerte gia' scartate in passato.

Perche' esiste. Quando si cambia il modo di assegnare il punteggio — una
soglia, il peso di un requisito, il testo del prompt — l'unico modo onesto di
sapere se la modifica funziona e' riapplicarla a casi veri di cui si conosce
gia' l'esito. Qui i casi stanno in casi_calibrazione.json: titolo, azienda,
punteggio ricevuto allora e link all'annuncio.

Gira su GitHub Actions (workflow "Diagnostica"), dove c'e' la chiave Gemini:
in locale il .env non la contiene.

Uso:
    python verifica_punteggi.py                 usa casi_calibrazione.json
    python verifica_punteggi.py altri_casi.json
"""
import contextlib
import json
import logging
import signal
import sys
import time

import requests
from bs4 import BeautifulSoup

import scraper as S

FILE_CASI = "casi_calibrazione.json"

# Oltre questo tempo la diagnostica si ferma e riporta cio' che ha misurato.
# Il 05/10/2026 e' rimasta appesa sei ore su un caso e l'ha uccisa il limite
# di GitHub, senza lasciare nulla: meglio un risultato parziale e leggibile.
BUDGET_TOTALE_S = 20 * 60

# Tetto sul singolo caso. Il budget complessivo si controlla solo tra un caso
# e l'altro: se uno si blocca dentro, non scatta mai. E' successo due volte di
# fila (05-06/10/2026), con 1-2 misure su 8 e il job ucciso dal limite di sei
# ore di GitHub.
BUDGET_CASO_S = 150


@contextlib.contextmanager
def tetto_di_tempo(secondi):
    """Interrompe il blocco se supera i secondi indicati.

    Usa SIGALRM, che esiste solo su Unix: in locale su Windows il contesto non
    fa nulla e il caso puo' durare quanto vuole — accettabile, perche' la
    diagnostica gira su GitHub (Linux) e in locale la si guarda a vista."""
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def scaduto(signum, frame):
        raise TimeoutError(f"caso oltre {secondi}s")

    precedente = signal.signal(signal.SIGALRM, scaduto)
    signal.alarm(secondi)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, precedente)


def testo_annuncio(link):
    r = requests.get(link, headers={"User-Agent": S.USER_AGENT_CHROME,
                                    "Accept-Language": "it-IT,it;q=0.9"}, timeout=25)
    if r.status_code != 200:
        return ""
    return BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)[:9000]


def main(percorso=FILE_CASI):
    casi = json.load(open(percorso, encoding="utf-8"))
    print(f"Rivaluto {len(casi)} offerte gia' scartate, con le regole di oggi.\n")
    saliti = scesi = invariati = falliti = 0
    scadenza = time.monotonic() + BUDGET_TOTALE_S
    for indice, c in enumerate(casi):
        if time.monotonic() > scadenza:
            print(f"\nTempo esaurito: {len(casi) - indice} casi non misurati.")
            break
        testo = ""
        try:
            with tetto_di_tempo(BUDGET_CASO_S // 3):
                testo = testo_annuncio(c["link"])
        except Exception as e:
            print(f"  {c['titolo'][:44]:46s} annuncio non raggiungibile ({type(e).__name__})")
        if not testo:
            # Un annuncio rimosso non e' un fallimento del punteggio: si dice e
            # si prosegue, senza inquinare il conteggio.
            falliti += 1
            continue
        try:
            with tetto_di_tempo(BUDGET_CASO_S):
                risultato = S.valuta_match_semantico(testo)
        except TimeoutError:
            print(f"  {c['titolo'][:44]:46s} valutazione oltre {BUDGET_CASO_S}s, salto")
            falliti += 1
            continue
        if risultato is None:
            print(f"  {c['titolo'][:44]:46s} nessun fornitore LLM disponibile")
            falliti += 1
            continue
        nuovo, motivazione = risultato[0], risultato[1]
        vecchio = c["vecchio"]
        if nuovo > vecchio:
            segno, saliti = "SALE  ", saliti + 1
        elif nuovo < vecchio:
            segno, scesi = "SCENDE", scesi + 1
        else:
            segno, invariati = "UGUALE", invariati + 1
        soglia = " -> ORA PASSA" if nuovo >= S.SOGLIA_MINIMA_PUNTEGGIO > vecchio else ""
        print(f"  {vecchio:3d}% -> {nuovo:3d}%  {segno}  {c['titolo'][:42]:44s} "
              f"{c['azienda'][:20]}{soglia}")
        print(f"        {motivazione[:200]}")
    print(f"\nSalite {saliti} | scese {scesi} | invariate {invariati} | non valutabili {falliti}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else FILE_CASI))
