import json
import os
import sys
import asyncio
from autoscout_scraper import AutoScoutScraper
from history_manager import HistoryManager
from playwright.async_api import async_playwright

def load_config() -> dict:
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if not os.path.exists(config_path):
        print(f"[!] Errore: '{config_path}' non trovato.")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)

async def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)

    config = load_config()
    criteria = config["search_criteria"]

    print("==================================================")
    print(" 🏎️ AUTOSCOUT24 VERCEL TRACKER & RECORD NOTIFIER")
    print("==================================================")
    brands = ", ".join([b["name"] for b in criteria.get("brands", [])])
    print(f"Marchi: {brands}")
    print(f"Filtri: Anno >= {criteria['year_min']} | Max {criteria['km_max']} KM | Automatico")
    print("--------------------------------------------------")

    # 1. Scraping
    scraper = AutoScoutScraper(config)
    best_deals = await scraper.run()

    # 2. Verifica automatica immediata ed aggiornamento record (ad OGNI esecuzione)
    hm = HistoryManager()
    print("\n==================================================")
    print("🔍 VERIFICA AUTOMATICA ATTIVITÀ ANNUNCI AUTOSCOUT24")
    print("==================================================")
    records = hm.update_records(best_deals)

    # 4. Export dati per Vercel Landing Page
    hm.export_web_data(records, scraper.all_scraped_listings)

    # 5. Riepilogo
    print(f"\n[+] {len(best_deals)} migliori offerte (1 per modello):")
    for d in best_deals:
        print(f" • [{d['brand']} {d['model']}] {d['price_formatted']} | {d['year']}")

    print("\n[✓] Completato!")

if __name__ == "__main__":
    asyncio.run(main())
