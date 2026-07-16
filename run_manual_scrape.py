#!/usr/bin/env python3
"""
Script per eseguire manualmente uno scraping completo.
Simula l'esecuzione schedulata delle 09:00 o 12:00.
"""
import sys
import os
from datetime import datetime
import logging

sys.path.insert(0, os.path.dirname(__file__))

from scraper import (
    LinkedInScraper, MichaelPageScraper, PagePersonnelScraper,
    WyserScraper, LhhScraper, GiGroupScraper, ManpowerScraper,
    IQMSelezioneScraper,
    CITIES, load_viste, save_viste, load_giornaliere,
    save_giornaliere, filtra_offerte_per_citta, get_match_type,
    get_job_id
)

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

    # Deduplicazione
    viste = load_viste()
    nuove_offerte = []
    seen_titles = set()
    seen_snippets = set()

    import re
    def clean_sig(text):
        return re.sub(r'[^a-z0-9]', '', str(text).lower())

    for job in tutte_le_offerte:
        job.match_type = get_match_type(job.title)
        job_id = get_job_id(job.link)

        if job_id in viste:
            continue

        norm_title = clean_sig(job.title)
        norm_company = clean_sig(job.company)
        norm_city = clean_sig(job.city)
        norm_snippet = clean_sig(job.snippet[:60]) if job.snippet else ""

        title_sig = (norm_title, norm_company, norm_city)
        snippet_sig = (norm_company, norm_city, norm_snippet) if norm_snippet else None

        if title_sig in seen_titles:
            continue
        if snippet_sig and snippet_sig in seen_snippets:
            continue

        # match_level/work_mode/probabilita/motivazione sono già stati calcolati
        # una volta dentro scraper.scrape(): ricalcolarli qui raddoppierebbe
        # fetch HTTP e chiamate LLM e rischierebbe di sovrascriverli con un
        # risultato diverso (calcolato su job.snippet, spesso più povero del
        # testo originale già usato).
        nuove_offerte.append(job)
        viste.add(job_id)
        seen_titles.add(title_sig)
        if snippet_sig:
            seen_snippets.add(snippet_sig)

    save_viste(viste)

    giornaliere = load_giornaliere()
    for job in nuove_offerte:
        giornaliere.append(job.to_dict())
    save_giornaliere(giornaliere)

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
