#!/usr/bin/env python3
"""
Script per eseguire manualmente uno scraping completo.
Simula l'esecuzione schedulata delle 09:00 o 12:00.
"""
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from scraper import (
    LinkedInScraper, MichaelPageScraper, PagePersonnelScraper,
    WyserScraper, LhhScraper, GiGroupScraper, ManpowerScraper,
    IQMSelezioneScraper, PraxiScraper, AntalScraper,
    CITIES, load_viste, save_viste, load_giornaliere,
    save_giornaliere, filtra_offerte_per_citta,
    dedup_offerte
)
from state_io import atomic_write_json

if __name__ == "__main__":
    print("="*70)
    print(f"SCRAPING MANUALE — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*70)
    print()

    scrapers = [
        LinkedInScraper(),
        MichaelPageScraper(),
        PagePersonnelScraper(),
        WyserScraper(),
        LhhScraper(),
        GiGroupScraper(),
        ManpowerScraper(),
        IQMSelezioneScraper(),
        PraxiScraper(),
        AntalScraper(),
    ]

    print(f"Avvio scraping con {len(scrapers)} portali...")
    print()

    tutte_le_offerte = []

    for city_name, city_config in CITIES.items():
        print(f"  {city_name}:")
        for scraper in scrapers:
            print(f"    {scraper.portal_name}...", end=" ", flush=True)
            try:
                offerte_scraper = scraper.scrape(city_name, city_config)
                tutte_le_offerte.extend(filtra_offerte_per_citta(offerte_scraper, city_config))
                print(f"OK {len(offerte_scraper)} offerte")
            except Exception as e:
                print(f"ERRORE: {str(e)[:80]}")
        print()

    # Deduplicazione (logica condivisa con esegui_scraping_job in scraper.py:
    # match_level/work_mode/probabilita/motivazione sono già stati calcolati una
    # volta dentro scraper.scrape(), dedup_offerte non li ricalcola).
    viste = load_viste()
    nuove_offerte = dedup_offerte(tutte_le_offerte, viste)
    save_viste(viste)

    giornaliere = load_giornaliere()
    for job in nuove_offerte:
        giornaliere.append(job.to_dict())
    save_giornaliere(giornaliere)

    # Delta di SOLE le offerte trovate in QUESTO run (non l'intero accumulo
    # locale, che include offerte già presenti prima di questo run): il workflow
    # lo usa per il merge invece di offerte_giornaliere.json intero, altrimenti
    # riaggiungerebbe al remoto offerte già rimosse nel frattempo da un run
    # email.yml concorrente (l'email.yml le rimuove dal remoto dopo l'invio,
    # ma questo processo le aveva già caricate in memoria prima di quel momento).
    atomic_write_json("nuove_offerte_run.json", [job.to_dict() for job in nuove_offerte], ensure_ascii=False, indent=2)

    print()
    print("="*70)
    print("SCRAPING COMPLETATO")
    print("="*70)
    print()
    print(f"Totale offerte trovate (inclusi duplicati): {len(tutte_le_offerte)}")
    print(f"Nuove offerte (dopo dedup): {len(nuove_offerte)}")
    print(f"Offerte giornaliere accumulate: {len(giornaliere)}")

    if nuove_offerte:
        print()
        print("Nuove offerte:")
        for i, job in enumerate(nuove_offerte, 1):
            print(f"  {i}. [{job.portal}] {job.title} — {job.company} ({job.city}, {job.work_mode})")
            print(f"     {job.link[:100]}")
    else:
        print()
        print("Nessuna nuova offerta trovata (tutte gia' viste in precedenza).")

    print()
