import asyncio
import json
import re
from typing import List, Dict, Any
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

class AutoScoutScraper:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.criteria = config.get("search_criteria", {})
        self.base_url = "https://www.autoscout24.it"
        self.all_scraped_listings: List[Dict[str, Any]] = []

    def build_search_url(self, brand_slug: str, page_num: int = 1) -> str:
        """Costruisce l'URL di ricerca AutoScout24 con tutti i filtri."""
        year_min = self.criteria.get("year_min", 2023)
        km_max = self.criteria.get("km_max", 50000)
        transmission = self.criteria.get("transmission", "A")
        fuels = "%2C".join(self.criteria.get("fuels", ["B", "E", "2", "3"]))
        condition = self.criteria.get("condition", "U")

        return (
            f"{self.base_url}/lst/{brand_slug}?"
            f"atype=C&ustate={condition}"
            f"&fregfrom={year_min}"
            f"&kmto={km_max}"
            f"&gear={transmission}"
            f"&fuel={fuels}"
            f"&sort=price&desc=0"
            f"&page={page_num}"
        )

    def extract_model_name(self, brand: str, title: str) -> str:
        """Estrae il modello principale dall'annuncio per raggruppamenti precisi."""
        clean_title = title.replace(brand, "").strip()

        if brand == "Mercedes-Benz":
            match = re.search(r'\b(?:Classe\s+([ABCEGSVT])|([ABCEGSVT])[\s-]*Klasse)\b', clean_title, re.IGNORECASE)
            if match:
                letter = (match.group(1) or match.group(2)).upper()
                return f"Classe {letter}"
            match_letter = re.search(r'\b([ABCEGSVT])\s*\d{2,3}[a-z]?\b', clean_title, re.IGNORECASE)
            if match_letter:
                return f"Classe {match_letter.group(1).upper()}"
            match_sub = re.search(r'\b(CLA|CLE|CLS|GLA|GLB|GLC|GLE|GLS|EQA|EQB|EQC|EQE|EQS|Vito|Citan)\b', clean_title, re.IGNORECASE)
            if match_sub:
                return match_sub.group(1).upper()

        elif brand == "BMW":
            match_serie = re.search(r'\b(?:Serie\s*([1-8])|([1-8])[\s-]*Series)\b', clean_title, re.IGNORECASE)
            if match_serie:
                num = match_serie.group(1) or match_serie.group(2)
                return f"Serie {num}"
            match_num = re.search(r'\b([1-8])\d{2}[a-z]?\b', clean_title, re.IGNORECASE)
            if match_num:
                return f"Serie {match_num.group(1)}"
            match_x = re.search(r'\b(iX[13]|iX|i[3478]|X[1-7]|Z4)\b', clean_title, re.IGNORECASE)
            if match_x:
                val = match_x.group(1).upper()
                if val.startswith("IX") and len(val) > 2:
                    return f"iX{val[2]}"
                elif val.startswith("I") and len(val) == 2 and val[1].isdigit():
                    return f"i{val[1]}"
                return val

        elif brand == "Audi":
            match_q_etron = re.search(r'\b(Q[2-8])\s*e-tron\b', clean_title, re.IGNORECASE)
            if match_q_etron:
                return f"{match_q_etron.group(1).upper()} e-tron"
            match_etron = re.search(r'\be-tron\s*(GT)?\b', clean_title, re.IGNORECASE)
            if match_etron:
                return "e-tron GT" if match_etron.group(1) else "e-tron"
            match = re.search(r'\b(A[1-8]|Q[2-8]|S[1-8]|RS[3-7]|TT|R8)\b', clean_title, re.IGNORECASE)
            if match:
                return match.group(1).upper()

        elif brand == "Volkswagen":
            match = re.search(r'\b(Golf|Polo|T-Roc|T-Cross|Tiguan|Passat|ID\.3|ID\.4|ID\.5|ID\.7|ID\.\s*Buzz|Touran|Taigo|Arteon|Up!?)\b', clean_title, re.IGNORECASE)
            if match:
                val = match.group(1)
                if "ID" in val.upper():
                    return val.upper().replace(" ", "")
                return val.title()

        elif brand == "Cupra":
            match = re.search(r'\b(Formentor|Born|Leon|Ateca|Tavascan|Terramar)\b', clean_title, re.IGNORECASE)
            if match:
                return match.group(1).title()

        parts = clean_title.split()
        return " ".join(parts[:2]) if parts else "Generico"

    def _find_key_recursive(self, obj: Any, key_name: str) -> Any:
        """Cerca ricorsivamente una chiave all'interno di dict/list nidificati."""
        if isinstance(obj, dict):
            if key_name in obj and isinstance(obj[key_name], list) and len(obj[key_name]) > 0:
                return obj[key_name]
            for v in obj.values():
                res = self._find_key_recursive(v, key_name)
                if res is not None:
                    return res
        elif isinstance(obj, list):
            for item in obj:
                res = self._find_key_recursive(item, key_name)
                if res is not None:
                    return res
        return None

    def _format_mileage(self, raw_mileage) -> str:
        """Formatta il chilometraggio come 'XX.XXX km'."""
        if raw_mileage is None or raw_mileage == '' or raw_mileage == 'N/D':
            return 'N/D'
        try:
            km = int(raw_mileage)
            return f"{km:,} km".replace(",", ".")
        except (ValueError, TypeError):
            return str(raw_mileage)

    def parse_listings(self, html_content: str, brand_name: str) -> List[Dict[str, Any]]:
        """Estrae gli annunci dal JSON __NEXT_DATA__ di Next.js."""
        results = []
        soup = BeautifulSoup(html_content, 'html.parser')
        next_data_script = soup.find('script', id='__NEXT_DATA__')

        if not next_data_script or not next_data_script.string:
            return results

        try:
            data = json.loads(next_data_script.string)
            listings = self._find_key_recursive(data, "listings") or []

            for item in listings:
                if not isinstance(item, dict):
                    continue

                price_obj = item.get('price') if isinstance(item.get('price'), dict) else {}
                raw_price = price_obj.get('priceRaw') or 0
                try:
                    price_val = float(raw_price)
                except (ValueError, TypeError):
                    price_val = 0.0

                vehicle = item.get('vehicle') if isinstance(item.get('vehicle'), dict) else {}
                make = vehicle.get('make') or brand_name
                model_str = vehicle.get('model') or ''
                version_str = vehicle.get('modelVersionInput') or ''

                vehicle_details = item.get('vehicleDetails') if isinstance(item.get('vehicleDetails'), dict) else {}
                title = vehicle_details.get('title') or f"{make} {model_str} {version_str}".strip()

                # Super Prezzo badge (priceEvaluation == 1 su AutoScout24)
                price_eval = price_obj.get('priceEvaluation')
                super_deal = item.get('superDeal') if isinstance(item.get('superDeal'), dict) else {}
                is_super_price = (
                    price_eval == 1 or
                    price_eval == "1" or
                    bool(super_deal.get('isEligible'))
                )

                # Dati veicolo
                mileage = self._format_mileage(vehicle.get('mileageInKm'))
                tracking = item.get('tracking') if isinstance(item.get('tracking'), dict) else {}
                raw_reg = tracking.get('firstRegistration') or vehicle.get('firstRegistration')
                first_reg = str(raw_reg).replace('-', '/') if raw_reg else 'N/D'
                fuel = vehicle.get('fuel') or 'Benzina / Elettrica'
                transmission = vehicle.get('transmission') or 'Automatico'

                # Foto e URL
                images = item.get('images') or []
                ocs_images = item.get('ocsImagesA') or []
                image_url = None
                if images and isinstance(images, list) and len(images) > 0 and isinstance(images[0], str):
                    image_url = images[0]
                elif ocs_images and isinstance(ocs_images, list) and len(ocs_images) > 0:
                    first_ocs = ocs_images[0]
                    if isinstance(first_ocs, str):
                        image_url = first_ocs
                    elif isinstance(first_ocs, dict) and first_ocs.get('url'):
                        image_url = first_ocs.get('url')

                if image_url and image_url.startswith('//'):
                    image_url = 'https:' + image_url

                # Costruzione URL annuncio robusta
                relative_url = str(item.get('url') or vehicle_details.get('url') if isinstance(vehicle_details, dict) else item.get('url') or "").strip()
                if relative_url.startswith('http://') or relative_url.startswith('https://'):
                    full_url = relative_url
                elif relative_url.startswith('/'):
                    full_url = f"{self.base_url}{relative_url}"
                elif relative_url:
                    full_url = f"{self.base_url}/{relative_url}"
                else:
                    listing_id = item.get('id') or item.get('identifier')
                    full_url = f"{self.base_url}/annunci/{listing_id}" if listing_id else f"{self.base_url}/lst/{brand_name.lower()}"

                model_name = self.extract_model_name(brand_name, title)

                results.append({
                    "brand": brand_name,
                    "model": model_name,
                    "title": title,
                    "price": price_val,
                    "price_formatted": f"€ {price_val:,.0f}".replace(",", ".") if price_val > 0 else "N/D",
                    "mileage": mileage,
                    "year": first_reg,
                    "fuel": fuel,
                    "transmission": transmission,
                    "is_super_price": is_super_price,
                    "image_url": image_url,
                    "url": full_url
                })
        except Exception as e:
            print(f"[!] Errore parsing __NEXT_DATA__ per {brand_name}: {e}")

        return results

    async def scrape_brand(self, page, brand_info: Dict[str, str]) -> List[Dict[str, Any]]:
        """Scraping di 3 pagine per un singolo brand."""
        brand_name = brand_info["name"]
        brand_slug = brand_info["slug"]
        all_brand_listings = []

        print(f"[*] Navigazione su AutoScout24 per {brand_name} (pagine 1-3)...")
        for p_num in range(1, 4):
            search_url = self.build_search_url(brand_slug, page_num=p_num)
            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(2)

                # Gestione banner Cookie (solo prima pagina)
                if p_num == 1:
                    try:
                        cookie_btn = await page.query_selector('#onetrust-accept-btn-handler, button[id*="onetrust"][id*="accept"], button[data-testid="consent-accept"]')
                        if cookie_btn:
                            await cookie_btn.click()
                    except Exception:
                        pass

                html_content = await page.content()
                listings = self.parse_listings(html_content, brand_name)
                # Deduplicazione per URL per evitare overlap tra pagine
                existing_urls = {l['url'] for l in all_brand_listings}
                new_listings = [l for l in listings if l['url'] not in existing_urls]
                all_brand_listings.extend(new_listings)
            except Exception as e:
                print(f"[!] Errore durante lo scraping di {brand_name} pag {p_num}: {e}")

        print(f"[+] Estratti {len(all_brand_listings)} annunci totali per {brand_name}.")
        return all_brand_listings

    async def run(self) -> List[Dict[str, Any]]:
        """Esegue lo scraping completo di tutti i brand e ritorna i best deals."""
        all_listings = []
        seen_urls = set()
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(
                    headless=True,
                    channel="chrome",
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                    ]
                )
            except Exception:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                    ]
                )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )

            brands = self.criteria.get("brands", [])
            for brand_info in brands:
                page = await context.new_page()
                try:
                    brand_listings = await self.scrape_brand(page, brand_info)
                    # Deduplicazione globale tra brand diversi
                    for listing in brand_listings:
                        if listing['url'] not in seen_urls:
                            seen_urls.add(listing['url'])
                            all_listings.append(listing)
                finally:
                    await page.close()
                await asyncio.sleep(2)

            await browser.close()

        # Filtro "Super Prezzo"
        only_super = self.criteria.get("only_super_price", True)
        if only_super:
            filtered = [l for l in all_listings if l.get("is_super_price")]
            print(f"[*] Annunci con badge 'Super Prezzo': {len(filtered)} su {len(all_listings)}")
            if filtered:
                all_listings = filtered

        # Raggruppamento per Modello → prezzo più basso
        model_groups: Dict[str, Dict[str, Any]] = {}
        for listing in all_listings:
            key = f"{listing['brand']} {listing['model']}"
            if key not in model_groups:
                model_groups[key] = listing
            elif listing["price"] > 0 and (model_groups[key]["price"] == 0 or listing["price"] < model_groups[key]["price"]):
                model_groups[key] = listing

        best_deals = sorted(list(model_groups.values()), key=lambda x: (x["brand"], x["price"]))
        self.all_scraped_listings = all_listings
        print(f"[✓] Selezionate {len(best_deals)} auto migliori (1 per modello).")
        return best_deals
