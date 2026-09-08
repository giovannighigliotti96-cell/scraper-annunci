#!/usr/bin/env python3
"""Tracciamento delle candidature inviate.

Perché esiste: lo scraper sa cosa ti ha PROPOSTO (offerte_viste.json), ma non
sa a cosa ti sei davvero CANDIDATO. Senza questa distinzione non è possibile
né evitare una seconda candidatura alla stessa posizione ricomparsa da un
portale diverso (URL diverso = il dedup non la intercetta), né sapere quali
candidature sono ferme da settimane e meriterebbero un sollecito.

Uso:
    python candidature.py list [N]              elenca le ultime N offerte ricevute via email
    python candidature.py add <n|link> [stato]  registra una candidatura (default: inviata)
    python candidature.py stato <n|link> <stato> [--nota "..."]
    python candidature.py report                riepilogo + candidature da sollecitare

<n> è il numero mostrato da `list`. In alternativa si può incollare il link
dell'annuncio, così si può registrare anche un'offerta trovata fuori dallo scraper.

Dopo ogni modifica ricordati di committare candidature.json, altrimenti i run
su GitHub Actions non vedranno l'aggiornamento (girano su un checkout del repo).
"""
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scraper import (
    carica_storico_offerte, carica_candidature, salva_candidature,
    get_job_id,
)

# Stati ammessi, in ordine di avanzamento. "inviata" è il punto di partenza;
# gli stati terminali non compaiono più tra le candidature da sollecitare.
STATI = ["inviata", "risposta", "colloquio", "finale", "offerta", "rifiutata", "chiusa"]
STATI_TERMINALI = {"offerta", "rifiutata", "chiusa"}

# Dopo quanti giorni senza avanzamento una candidatura va sollecitata.
GIORNI_PER_SOLLECITO = 10


def _oggi():
    return datetime.now().strftime("%Y-%m-%d")


def _giorni_da(data_str):
    try:
        return (datetime.now().date() - datetime.strptime(data_str, "%Y-%m-%d").date()).days
    except Exception:
        return None


def _risolvi(riferimento, storico):
    """Da un indice di `list` o da un link, ricava (job_id, dati_offerta).
    dati_offerta è None se il link non è nello storico (offerta esterna): si
    registra comunque, con i soli dati disponibili."""
    if riferimento.isdigit():
        indice = int(riferimento)
        if not 1 <= indice <= len(storico):
            print(f"Numero {indice} fuori intervallo: lo storico ha {len(storico)} offerte.")
            return None, None
        voce = storico[indice - 1]
        return voce.get("job_id"), voce

    job_id = get_job_id(riferimento)
    for voce in storico:
        if voce.get("job_id") == job_id:
            return job_id, voce
    return job_id, None


def comando_list(argomenti):
    storico = carica_storico_offerte()
    if not storico:
        print("Nessuna offerta nello storico: si popola dopo il primo invio email riuscito.")
        return 0
    quante = int(argomenti[0]) if argomenti and argomenti[0].isdigit() else 30
    candidature = carica_candidature()
    print(f"Ultime {min(quante, len(storico))} offerte ricevute (di {len(storico)} in storico):\n")
    for i, voce in enumerate(storico[:quante], 1):
        marcatore = ""
        if voce.get("job_id") in candidature:
            marcatore = f"  [{candidature[voce['job_id']].get('stato', '?')}]"
        print(f"{i:3d}. [{voce.get('probabilita', 0):3d}%] {str(voce.get('titolo'))[:52]:52s} "
              f"{str(voce.get('azienda'))[:22]:22s} {str(voce.get('citta'))[:8]:8s} "
              f"{voce.get('data_invio', '')}{marcatore}")
    return 0


def comando_add(argomenti):
    if not argomenti:
        print("Uso: python candidature.py add <n|link> [stato]")
        return 1
    storico = carica_storico_offerte()
    job_id, voce = _risolvi(argomenti[0], storico)
    if not job_id:
        return 1
    stato = argomenti[1] if len(argomenti) > 1 else "inviata"
    if stato not in STATI:
        print(f"Stato '{stato}' non valido. Ammessi: {', '.join(STATI)}")
        return 1

    candidature = carica_candidature()
    if job_id in candidature:
        print(f"Candidatura già registrata il {candidature[job_id].get('data')} "
              f"(stato: {candidature[job_id].get('stato')}). Usa 'stato' per aggiornarla.")
        return 1

    candidature[job_id] = {
        "data": _oggi(),
        "stato": stato,
        "aggiornata": _oggi(),
        "titolo": (voce or {}).get("titolo", ""),
        "azienda": (voce or {}).get("azienda", ""),
        "citta": (voce or {}).get("citta", ""),
        "link": (voce or {}).get("link", argomenti[0] if not argomenti[0].isdigit() else ""),
        "note": "",
    }
    salva_candidature(candidature)
    etichetta = candidature[job_id]["titolo"] or job_id
    print(f"Registrata: {etichetta} — stato '{stato}'.")
    print("Ricordati di committare candidature.json.")
    return 0


def comando_stato(argomenti):
    if len(argomenti) < 2:
        print("Uso: python candidature.py stato <n|link> <stato> [--nota \"...\"]")
        return 1
    storico = carica_storico_offerte()
    job_id, _ = _risolvi(argomenti[0], storico)
    if not job_id:
        return 1
    nuovo_stato = argomenti[1]
    if nuovo_stato not in STATI:
        print(f"Stato '{nuovo_stato}' non valido. Ammessi: {', '.join(STATI)}")
        return 1

    candidature = carica_candidature()
    if job_id not in candidature:
        print("Nessuna candidatura registrata per questa offerta: usa prima 'add'.")
        return 1

    nota = ""
    if "--nota" in argomenti:
        posizione = argomenti.index("--nota")
        if posizione + 1 < len(argomenti):
            nota = argomenti[posizione + 1]

    precedente = candidature[job_id].get("stato")
    candidature[job_id]["stato"] = nuovo_stato
    candidature[job_id]["aggiornata"] = _oggi()
    if nota:
        candidature[job_id]["note"] = nota
    salva_candidature(candidature)
    print(f"{candidature[job_id].get('titolo') or job_id}: {precedente} -> {nuovo_stato}.")
    print("Ricordati di committare candidature.json.")
    return 0


def comando_report(_argomenti):
    candidature = carica_candidature()
    if not candidature:
        print("Nessuna candidatura registrata.")
        return 0

    per_stato = {}
    for dati in candidature.values():
        per_stato[dati.get("stato", "?")] = per_stato.get(dati.get("stato", "?"), 0) + 1

    print(f"Candidature totali: {len(candidature)}")
    for stato in STATI:
        if per_stato.get(stato):
            print(f"  {stato:12s} {per_stato[stato]}")
    altri = {s: n for s, n in per_stato.items() if s not in STATI}
    for stato, n in altri.items():
        print(f"  {stato:12s} {n}  (stato non standard)")

    attive = [d for d in candidature.values() if d.get("stato") not in STATI_TERMINALI]
    if attive:
        conclusi = sum(1 for d in candidature.values() if d.get("stato") in ("offerta", "colloquio", "finale", "risposta"))
        print(f"\nTasso di risposta: {conclusi}/{len(candidature)} "
              f"({round(100 * conclusi / len(candidature))}%)")

    da_sollecitare = []
    for dati in attive:
        giorni = _giorni_da(dati.get("aggiornata") or dati.get("data", ""))
        if giorni is not None and giorni >= GIORNI_PER_SOLLECITO:
            da_sollecitare.append((giorni, dati))
    if da_sollecitare:
        print(f"\nDa sollecitare (ferme da {GIORNI_PER_SOLLECITO}+ giorni):")
        for giorni, dati in sorted(da_sollecitare, reverse=True, key=lambda x: x[0]):
            print(f"  {giorni:3d}gg  {str(dati.get('titolo'))[:48]:48s} {str(dati.get('azienda'))[:24]:24s} "
                  f"[{dati.get('stato')}]")
            if dati.get("link"):
                print(f"         {dati['link']}")
    return 0


COMANDI = {"list": comando_list, "add": comando_add, "stato": comando_stato, "report": comando_report}


def main(argv):
    if len(argv) < 2 or argv[1] not in COMANDI:
        print(__doc__)
        return 1
    return COMANDI[argv[1]](argv[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
