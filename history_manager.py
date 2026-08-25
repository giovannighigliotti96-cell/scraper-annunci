import json
import os
from datetime import datetime
from typing import List, Dict, Any
from bs4 import BeautifulSoup

class HistoryManager:
    def __init__(self, workspace_dir: str = None):
        self.workspace_dir = workspace_dir or os.path.dirname(os.path.abspath(__file__))
        self.history_file = os.path.join(self.workspace_dir, "best_deals_history.json")
        self.public_dir = os.path.join(self.workspace_dir, "public")
        self.public_data_file = os.path.join(self.public_dir, "data.json")
        os.makedirs(self.public_dir, exist_ok=True)

    def load_history(self) -> Dict[str, Any]:
        """Carica il registro storico dei record."""
        if not os.path.exists(self.history_file):
            return {"records": {}, "last_weekly_check": None}
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "records" not in data:
                    return {"records": data, "last_weekly_check": None}
                return data
        except Exception as e:
            print(f"[!] Errore lettura history: {e}")
            return {"records": {}, "last_weekly_check": None}

    def save_history(self, history_data: Dict[str, Any]):
        """Salva il registro storico."""
        try:
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(history_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[!] Errore salvataggio history: {e}")

    def should_verify_links(self) -> bool:
        """Controlla se è passata almeno una settimana dall'ultima verifica."""
        history = self.load_history()
        last_check = history.get("last_weekly_check")
        if not last_check:
            return True
        try:
            last_date = datetime.strptime(last_check, "%Y-%m-%d")
            return (datetime.now() - last_date).days >= 7
        except ValueError:
            return True

    async def verify_records(self, page) -> int:
        """Verifica se i link dei record storici sono ancora attivi. Ritorna il numero di record rimossi."""
        history = self.load_history()
        records = history.get("records", {})

        if not records:
            print("[*] Nessun record storico da verificare.")
            history["last_weekly_check"] = datetime.now().strftime("%Y-%m-%d")
            self.save_history(history)
            return 0

        print("\n==================================================")
        print("🔍 VERIFICA SETTIMANALE RECORD (ANNUNCI SCADUTI)")
        print("==================================================")

        removed = []
        for key, record in list(records.items()):
            url = record.get("url", "")
            if not url:
                continue

            print(f"[*] Verifica {key}...")
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                status = response.status if response else 0
                html = await page.content()
                text = BeautifulSoup(html, "html.parser").get_text().lower()

                is_expired = (
                    status == 404 or
                    status == 410 or
                    "non è più disponibile" in text or
                    "annuncio non disponibile" in text or
                    "pagina non trovata" in text or
                    "questo annuncio non esiste" in text or
                    (response and response.url and "/lst/" in response.url and "/annunci/" not in response.url)
                )

                if is_expired:
                    print(f"  [🗑️] {key} → RIMOSSO (annuncio scaduto)")
                    removed.append(key)
                else:
                    print(f"  [✓] {key} → ancora attivo")
            except Exception as e:
                print(f"  [!] Errore verifica {key}: {e}")

        for key in removed:
            del records[key]

        history["records"] = records
        history["last_weekly_check"] = datetime.now().strftime("%Y-%m-%d")
        self.save_history(history)
        print(f"[✓] Verifica completata. Rimossi: {len(removed)}\n")
        return len(removed)

    def clean_expired_records(self, records: Dict[str, Dict[str, Any]]) -> List[str]:
        """Controlla velocemente via HTTP gli URL dei record ed elimina quelli scaduti/reindirizzati."""
        import urllib.request
        removed = []
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

        for key, rec in list(records.items()):
            url = rec.get("url", "")
            img = rec.get("image_url", "")
            is_expired = False

            if url:
                try:
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        final_url = resp.geturl()
                        if "/lst/" in final_url or "/annunci/" not in final_url:
                            is_expired = True
                except Exception:
                    is_expired = True

            if not is_expired and img:
                try:
                    req_img = urllib.request.Request(img, headers=headers)
                    with urllib.request.urlopen(req_img, timeout=5) as resp:
                        if resp.status != 200:
                            is_expired = True
                except Exception:
                    is_expired = True

            if is_expired:
                print(f"  [🗑️ Auto-Clean] Record scaduto rimosso: {key}")
                removed.append(key)
                del records[key]

        return removed

    def update_records(self, best_deals: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Aggiorna i record storici rimuovendo annunci scaduti ed inserendo nuovi minimi."""
        history = self.load_history()
        records = history.get("records", {})
        today = datetime.now().strftime("%Y-%m-%d %H:%M")

        # Pulizia automatica record con link o foto scadute
        self.clean_expired_records(records)

        # Reset flag is_new_record per tutti i record esistenti
        for key in records:
            records[key]["is_new_record"] = False

        for deal in best_deals:
            key = f"{deal['brand']} {deal['model']}"
            price = deal.get("price", 0)
            if price <= 0:
                continue

            if key not in records:
                records[key] = {**deal, "record_date": today, "is_new_record": True}
                print(f"[🏆 NUOVO] {key} → € {price:,.0f}")
            else:
                old_price = records[key].get("price", 0)
                if old_price <= 0 or price < old_price:
                    records[key] = {**deal, "record_date": today, "is_new_record": True, "previous_record_price": old_price}
                    print(f"[🏆 RECORD] {key}: € {old_price:,.0f} → € {price:,.0f}")

        history["records"] = records
        self.save_history(history)
        return records

    def export_web_data(self, records: Dict[str, Dict[str, Any]], all_listings: List[Dict[str, Any]]):
        """Genera public/data.json per la Landing Page Vercel."""
        # Raggruppa tutti gli annunci per modello
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for listing in all_listings:
            key = f"{listing['brand']} {listing['model']}"
            grouped.setdefault(key, []).append(listing)

        for key in grouped:
            grouped[key].sort(key=lambda x: (x.get("price", 0) <= 0, x.get("price", 0)))

        records_list = sorted(records.values(), key=lambda x: (x.get("brand", ""), x.get("price", 0)))

        web_data = {
            "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "total_models_tracked": len(records_list),
            "total_active_listings": len(all_listings),
            "records": records_list,
            "all_listings_by_model": grouped
        }

        try:
            with open(self.public_data_file, "w", encoding="utf-8") as f:
                json.dump(web_data, f, ensure_ascii=False, indent=2)
            print(f"[✓] Dati web esportati in: {self.public_data_file}")
        except Exception as e:
            print(f"[!] Errore export data.json: {e}")
