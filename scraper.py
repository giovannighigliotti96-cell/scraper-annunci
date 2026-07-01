import os
import time
import json
import logging
from datetime import datetime
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import schedule
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import PyPDF2
import cloudscraper
from curl_cffi import requests as curl_requests

# ==========================================
# CONFIGURAZIONE INIZIALE
# ==========================================
load_dotenv(override=True)

GMAIL_USER = os.getenv("GMAIL_USER", "").strip().lstrip('﻿').strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").replace('"', '').replace("'", "").lstrip('﻿').strip()
DESTINATION_EMAIL = os.getenv("DESTINATION_EMAIL", "").strip().lstrip('﻿').strip()
INCLUDE_UNVERIFIED = True
PENDING_SCRAPER_EMAIL_FILE = "pending_scraper_email.json"

import sys


CITIES = {
    "Genova": {
        "lat": 44.449518,
        "lon": 8.892783,
        "filter_hybrid_only": False,
        "wyser_slug": "genova-ci",
        "linkedin_location": "Genoa, Italy",
        "lhh_location": "Genova%2C+GE%2C+Italia",
        "glassdoor_url": "https://www.glassdoor.it/Lavoro/genova-marketing-manager-lavori-SRCH_IL.0,6_IC3177962_KO7,24.htm"
    },
    "Milano": {
        "lat": 45.464204,
        "lon": 9.189982,
        "filter_hybrid_only": True,
        "wyser_slug": "milano-ci",
        "linkedin_location": "Milan, Italy",
        "lhh_location": "Milano%2C+MI%2C+Italia",
        "glassdoor_url": "https://www.glassdoor.it/Lavoro/milano-marketing-manager-lavori-SRCH_IL.0,6_IC2802090_KO7,24.htm"
    },
    "Torino": {
        "lat": 45.070312,
        "lon": 7.686856,
        "filter_hybrid_only": True,
        "wyser_slug": "torino-to",
        "linkedin_location": "Turin, Italy",
        "lhh_location": "Torino%2C+TO%2C+Italia",
        "glassdoor_url": "https://www.glassdoor.it/Lavoro/torino-marketing-manager-lavori-SRCH_IL.0,6_IC2810526_KO7,24.htm"
    }
}

LOG_FILE = "scraping_log.txt"
VISTE_FILE = "offerte_viste.json"
GIORNALIERE_FILE = "offerte_giornaliere.json"
CV_FILE = "cv_ghigliotti.pdf"

def valida_credenziali_email():
    """Verifica che le credenziali email siano configurate correttamente.
    Legge la variabile d'ambiente in modo dinamico (non usa la costante di modulo)
    così funziona correttamente anche durante i test con monkeypatch.
    Ritorna True se valide, False altrimenti.
    Non chiama sys.exit() — chi la invoca decide come gestire il fallimento.
    """
    pwd = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").replace('"', '').replace("'", "").strip()
    if not pwd or len(pwd) != 16:
        logging.warning("ATTENZIONE: La GMAIL_APP_PASSWORD nel .env non è valida. Deve contenere esattamente 16 caratteri.")
        return False
    return True

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
# In GitHub Actions (no TTY) stampa WARNING+ anche su stdout per visibilità nei log
if not sys.stdout.isatty():
    _stdout_handler = logging.StreamHandler(sys.stdout)
    _stdout_handler.setLevel(logging.WARNING)
    _stdout_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logging.getLogger().addHandler(_stdout_handler)

# ==========================================
# LOGICA CV E MATCHING COMPETENZE
# ==========================================
TARGET_SKILLS = [
    "hubspot", "crm", "go-to-market", "lead generation", "pipeline", "funnel", 
    "b2b", "digital marketing", "sales", "growth", "e-commerce", 
    "marketing automation", "revenue", "kpi", "team management"
]

def estrai_keyword_cv(pdf_path):
    cv_skills = set()
    if not os.path.exists(pdf_path):
        logging.warning(f"File CV {pdf_path} non trovato. Verrà usata la lista base completa.")
        return TARGET_SKILLS
        
    try:
        testo_cv = ""
        with open(pdf_path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                testo = page.extract_text()
                if testo:
                    testo_cv += testo.lower() + " "
                    
        for skill in TARGET_SKILLS:
            if skill in testo_cv:
                cv_skills.add(skill)
                
        logging.info(f"Trovate {len(cv_skills)} competenze nel CV: {', '.join(cv_skills)}")
        return list(cv_skills)
    except Exception as e:
        logging.error(f"Errore lettura CV: {e}")
        return TARGET_SKILLS

# Carica competenze dal CV all'avvio
CV_SKILLS = estrai_keyword_cv(CV_FILE)

# ==========================================
# SCORING PROBABILITÀ RICHIAMATA
# ==========================================
# Competenze che Giovanni HA nel CV
_CV_HA = {
    "HubSpot": ["hubspot"],
    "CRM": ["crm"],
    "Lead Generation": ["lead generation", "lead gen", "generazione lead", "mql", "sql", "pipeline marketing"],
    "Demand Generation": ["demand generation", "demand gen"],
    "Go-to-Market": ["go to market", "go-to-market", "gtm", "lancio prodotto", "product launch"],
    "Marketing Automation": ["marketing automation", "automazione marketing", "nurturing"],
    "B2B": ["b2b", "business to business", "enterprise sales", "corporate"],
    "Digital Marketing": ["digital marketing", "performance marketing", "marketing digitale"],
    "Funnel / CRO": ["funnel", "cro", "conversion rate", "tasso di conversione", "ottimizzazione conversioni"],
    "Revenue / P&L": ["revenue", "p&l", "fatturato", "ricavi", "obiettivi commerciali"],
    "Team Management": ["team management", "gestione team", "people management", "leadership team", "coordinamento team"],
    "Budget": ["budget", "gestione budget", "budget marketing"],
    "Google Ads / SEM": ["google ads", "google adwords", "sem", "paid search", "campagne search"],
    "Meta Ads": ["meta ads", "facebook ads", "instagram ads", "paid social", "social advertising"],
    "Analytics / KPI": ["analytics", "kpi", "dashboard", "reporting", "google analytics", "ga4", "looker"],
    "E-commerce": ["e-commerce", "ecommerce", "e commerce"],
    "Business Development": ["business development", "sviluppo commerciale", "bizdev"],
    "Stakeholder C-level": ["c-level", "board", "direzione generale", "stakeholder", "cfo", "ceo"],
    "Brand / Positioning": ["brand strategy", "posizionamento", "positioning", "brand awareness"],
    "Product Marketing": ["product marketing", "marketing di prodotto", "lancio prodotto"],
    "RevOps": ["revops", "revenue operations", "sales operations"],
    "SEO": ["seo", "search engine optimization", "ottimizzazione motori"],
    "Social Selling / Outbound": ["social selling", "outbound", "cold outreach", "prospecting"],
    "Startup / Scale-up": ["startup", "scale-up", "scaling", "crescita accelerata", "pivot"],
    "Inglese": ["inglese", "english", "fluent english", "c1", "lingua inglese"],
}

# Requisiti che l'offerta può richiedere e che Giovanni NON ha nel CV principale
_CV_GAP = {
    "Salesforce": ["salesforce"],
    "Marketo / Adobe Campaign": ["marketo", "adobe campaign", "adobe marketo", "eloqua"],
    "SAP": ["sap marketing", "sap crm"],
    "SQL / Python": ["sql avanzato", "python", "r programming", "data engineering", "power bi developer"],
    "Settore Pharma": ["farmaceut", "pharma", "medicale", "dispositivi medici", "life science"],
    "Settore Luxury/Fashion": ["luxury", "fashion", "moda", "lusso", "alta moda"],
    "Settore Finance": ["bancario", "banking", "assicurativo", "fintech", "credito al consumo"],
    "10+ anni esperienza": ["10 anni di esperienza", "almeno 10 anni", "10+ anni", "dieci anni di"],
    "Adobe Analytics": ["adobe analytics", "adobe experience cloud", "adobe campaign"],
}

def calcola_probabilita_callback(testo: str) -> tuple:
    """Stima la probabilità (0-100) di essere richiamato, confrontando il testo
    dell'offerta con il profilo di Giovanni. Ritorna (probabilita, motivazione)."""
    t = testo.lower()

    trovati = [label for label, kws in _CV_HA.items() if any(kw in t for kw in kws)]
    gap = [label for label, kws in _CV_GAP.items() if any(kw in t for kw in kws)]

    total = len(trovati) + len(gap)
    if total == 0:
        return 55, "Testo offerta non analizzabile"

    coverage = len(trovati) / total
    score = int(15 + coverage * 80)
    if len(trovati) >= 8:
        score = min(95, score + 5)
    score = max(15, min(95, score))

    ha_str = ", ".join(trovati[:4])
    if len(trovati) > 4:
        ha_str += f" (+{len(trovati) - 4} altri)"

    if gap:
        motivazione = f"Hai: {ha_str}. Gap: {', '.join(gap[:3])}"
    elif trovati:
        motivazione = f"Hai: {ha_str}. Nessun gap rilevato"
    else:
        motivazione = "Nessuna competenza rilevata nel testo"

    return score, motivazione


def detect_work_mode(text: str) -> str:
    """
    Rileva la modalità di lavoro dal testo.
    Ritorna: 'ibrido' | 'da remoto' | 'in sede' | 'unverified'
    """
    t = text.lower()
    
    # Ibrido — controlla PRIMA di in_sede. "smart working" in Italia = parziale = ibrido
    hybrid_patterns = ["ibrido", "ibrida", "hybrid", "lavoro misto", "presenza e remoto",
                       "remoto e presenza", "flessibile", "flessibilità",
                       "smart working", "smart work", " sw "]
    if any(p in t for p in hybrid_patterns):
        return "ibrido"

    # Remoto pieno (senza giorni in ufficio)
    remote_patterns = ["da remoto", "full remote", "100% remoto", "lavoro remoto",
                       "remote work", "telelavoro", "lavoro da casa",
                       "work from home", "wfh"]
    if any(p in t for p in remote_patterns):
        return "da remoto"

    # In sede — "in presenza" rimosso perché ambiguo nei contratti ibridi
    onsite_patterns = ["in sede", "on-site", "onsite", "presenza obbligatoria",
                       "lavoro in ufficio", "presenza in ufficio", "giorni in ufficio",
                       "giorni a settimana in ufficio", "presso la sede", "sede di lavoro",
                       "5 giorni su 5", "5 days"]
    if any(p in t for p in onsite_patterns):
        return "in sede"
    
    return "unverified"

def calcola_punteggio_e_modalita(url, snippet):
    """Scarica il testo dell'offerta (se possibile), calcola le skill e rileva la modalità di lavoro."""
    testo_completo = snippet.lower()
    fetch_status = "no_attempt"
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            testo_completo += " " + soup.get_text(" ", strip=True).lower()
            fetch_status = "ok"
        else:
            fetch_status = "http_error"
    except requests.exceptions.Timeout:
        fetch_status = "timeout"
        logging.warning(f"Timeout scaricamento testo completo per {url}")
    except Exception as e:
        fetch_status = "http_error"
        logging.warning(f"Impossibile scaricare testo completo per {url}: {e}")
        
    count = 0
    for skill in CV_SKILLS:
        if skill in testo_completo:
            count += 1
            
    work_mode = detect_work_mode(testo_completo)
    probabilita, motivazione = calcola_probabilita_callback(testo_completo)

    if count >= 3:
        return "Alto", count, work_mode, fetch_status, probabilita, motivazione
    elif count >= 1:
        return "Medio", count, work_mode, fetch_status, probabilita, motivazione
    else:
        return "Base", count, work_mode, fetch_status, probabilita, motivazione

# ==========================================
# TARGET JOB TITLES E MATCHER
# ==========================================
MATCHING_MODE = os.getenv("MATCHING_MODE", "moderate").lower()

# Lista titoli ESATTI: il titolo deve contenere ESATTAMENTE una di queste stringhe
# (match case-insensitive, sottostringa OK solo per le stringhe in PARTIAL_KEYWORDS)
EXACT_TITLES = [
    # English target titles
    "digital sales and marketing manager",
    "growth marketing manager",
    "head of growth",
    "digital marketing manager",
    "revenue growth manager",
    "go to market manager",
    "go-to-market manager",
    "demand generation manager",
    "b2b marketing manager",
    "performance marketing manager",
    "customer acquisition manager",
    "crm and marketing automation manager",
    "crm & marketing automation manager",
    "commercial strategy manager",
    "digital sales manager",
    "marketing and sales manager",
    "marketing & sales manager",
    "growth and gtm manager",
    "growth & gtm manager",
    "marketing manager",
    
    # Italian target titles
    "responsabile marketing & sales",
    "responsabile marketing e sales",
    "responsabile marketing"
]

PARTIAL_KEYWORDS = []

# Combinazioni: ENTRAMBE le parole devono essere nel titolo
COMBINED_KEYWORDS = []

# Esclusioni esplicite: titoli che matchano le regole sopra ma NON vogliamo
# Queste vengono controllate DOPO il match positivo
TITLE_EXCLUSIONS = [
    "social media marketing manager",
    "event marketing manager",
    "channel marketing manager",
    "field marketing manager",
    "influencer marketing manager",
    "content marketing manager",
    "affiliate marketing manager",
    "email marketing manager",
    "product marketing manager",
    "brand marketing manager",
    "trade marketing manager",
    "sales & marketing manager",
    "sales and marketing manager",
    "international marketing manager",
    "category manager",
    "store manager",
    "account manager",
    "project manager",
    
    # Seniority non target (junior/stage)
    "stage", "tirocinio", "junior", "internship", "trainee", "entry level", "unpaid", "apprendistato", "apprendista",
    
    # Ruoli retail/negozio/venditore non digital
    "commesso", "addetto vendita", "addetta vendita", "cassiere", "cassiera", "scaffalista",
    "promoter", "hostess", "steward", "call center", "operatore telefonico", "operatrice telefonica",
    "agente di commercio", "monomandatario", "plurimandatario", "sales representative", "consulente commerciale",
    "venditore", "venditrice", "front office", "receptionist",
    
    # Ruoli content/social media puri
    "social media manager", "content creator", "copywriter", "graphic designer", "web designer",
    "event planner", "event specialist", "event manager", "public relations", "pr specialist"
]

def is_valid_job_title(title: str) -> bool:
    """
    Restituisce True se il titolo corrisponde a uno dei ruoli target.
    Logica: EXACT_TITLES match AND NOT EXCLUSION match.
    """
    t = title.lower().strip()
    
    # 1. Controlla esclusioni prima di tutto
    for excl in TITLE_EXCLUSIONS:
        if excl in t:
            # Eccezione per "sales and marketing manager" e "sales & marketing manager"
            # se fanno parte dei nostri titoli target digitali
            if excl in ("sales and marketing manager", "sales & marketing manager"):
                if "digital sales and marketing manager" in t or "digital sales & marketing manager" in t:
                    continue
            return False
    
    # 2. Match sui titoli target (sottostringa)
    for exact in EXACT_TITLES:
        if exact in t:
            return True
            
    return False

def get_match_type(title: str) -> str:
    """
    Determina se un titolo corrisponde al matching esatto (Livello A) o simile (Livello B).
    """
    return "esatto"

# ==========================================
# GESTIONE DATI E DEDUPLICAZIONE
# ==========================================
VISTE_MAX_AGE_DAYS = 90  # Pulisce automaticamente URL più vecchi di 90 giorni

def load_viste() -> set:
    """Carica gli URL già visti come SET per lookup O(1)."""
    if not os.path.exists(VISTE_FILE):
        return set()
    try:
        with open(VISTE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Supporta sia il vecchio formato (lista di stringhe) 
        # che il nuovo formato (dict {url: timestamp})
        if isinstance(data, list):
            # Migrazione automatica dal vecchio formato
            logging.info(f"Migrazione offerte_viste dal vecchio formato lista ({len(data)} URL)")
            return set(data)
        elif isinstance(data, dict):
            # Pulizia entry scadute
            now = datetime.now().timestamp()
            cutoff = now - (VISTE_MAX_AGE_DAYS * 86400)
            fresh = {url for url, ts in data.items() if ts > cutoff}
            if len(fresh) < len(data):
                logging.info(f"Pulizia offerte_viste: rimossi {len(data)-len(fresh)} URL scaduti")
            return fresh
        return set()
    except (json.JSONDecodeError, Exception) as e:
        logging.error(f"Errore load_viste: {e}")
        return set()

def save_viste(viste: set):
    """Salva il set come dict {url: timestamp} per supportare la pulizia temporale."""
    now = datetime.now().timestamp()
    try:
        # Leggi timestamps esistenti per non sovrascrivere le date originali
        existing = {}
        if os.path.exists(VISTE_FILE):
            with open(VISTE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
                if isinstance(raw, dict):
                    existing = raw
        
        # Merge: mantieni timestamp originali, aggiungi nuovi con timestamp attuale
        merged = {url: existing.get(url, now) for url in viste}
        
        with open(VISTE_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f)
    except Exception as e:
        logging.error(f"Errore save_viste: {e}")

def load_giornaliere():
    if not os.path.exists(GIORNALIERE_FILE): return []
    with open(GIORNALIERE_FILE, "r", encoding="utf-8") as f:
        try: return json.load(f)
        except json.JSONDecodeError: return []

def save_giornaliere(jobs_dict_list):
    with open(GIORNALIERE_FILE, "w", encoding="utf-8") as f:
        json.dump(jobs_dict_list, f, indent=4)

def clear_giornaliere():
    save_giornaliere([])

# ==========================================
# CLASSI SCRAPERS PER SINGOLI PORTALI
# ==========================================

class ScrapedJob:
    def __init__(self, title, company, portal, link, date="", snippet="", match_level="Base", match_count=0, city="", work_mode="unverified", fetch_status="no_attempt", match_type="esatto", probabilita=0, motivazione=""):
        self.title = title.strip()
        self.company = company.strip() if company else "Azienda non specificata"
        self.portal = portal
        self.link = link
        self.date = date.strip() if date else "Data non disponibile"
        self.snippet = snippet.strip()
        self.match_level = match_level
        self.match_count = match_count
        self.city = city
        self.work_mode = work_mode
        self.fetch_status = fetch_status
        self.match_type = match_type
        self.probabilita = probabilita
        self.motivazione = motivazione

    def to_dict(self):
        return {
            "title": self.title,
            "company": self.company,
            "portal": self.portal,
            "link": self.link,
            "date": self.date,
            "snippet": self.snippet,
            "match_level": self.match_level,
            "match_count": self.match_count,
            "city": self.city,
            "work_mode": self.work_mode,
            "fetch_status": self.fetch_status,
            "match_type": self.match_type,
            "probabilita": self.probabilita,
            "motivazione": self.motivazione,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            data["title"], data["company"], data["portal"], data["link"],
            data.get("date", ""), data.get("snippet", ""),
            data.get("match_level", "Base"), data.get("match_count", 0),
            data.get("city", ""), data.get("work_mode", "unverified"), data.get("fetch_status", "no_attempt"),
            data.get("match_type", "esatto"),
            data.get("probabilita", 0), data.get("motivazione", ""),
        )

class BaseScraper:
    def __init__(self, portal_name):
        self.portal_name = portal_name
        
    def scrape(self, city_name, city_config):
        return []

class LinkedInScraper(BaseScraper):
    """
    LinkedIn Jobs — usa l'endpoint HTML pubblico /jobs/search/ con parsing JSON-LD.
    NON usare /jobs-guest/jobs/api/seeMoreJobPostings/ (deprecato, HTTP 403).
    Aggiunge 2s di delay tra le keyword per evitare rate limiting.
    """
    def __init__(self):
        super().__init__("LinkedIn")

    def scrape(self, city_name, city_config):
        import json as _json
        import time
        jobs = []
        
        keywords = [
            "Head of Growth",
            "Growth Marketing Manager",
            "Digital Marketing Manager",
            "Marketing Manager",
            "Responsabile Marketing",
        ]
        
        seen_links = set()
        
        # LinkedIn geolocalizza in modo errato le città italiane con l'italiano.
        # "Milan, Italy" / "Turin, Italy" / "Genoa, Italy" funzionano correttamente.
        linkedin_location = city_config.get("linkedin_location", f"{city_name}, Italy")

        for kw in keywords:
            try:
                url = "https://www.linkedin.com/jobs/search/"
                params = {
                    "keywords": kw,
                    "location": linkedin_location,
                    "f_TPR": "r86400",  # ultime 24h
                    "position": 1,
                    "pageNum": 0,
                }
                # Per Milano/Torino: filtra direttamente su LinkedIn per lavoro ibrido
                if city_config.get("filter_hybrid_only", False):
                    params["f_WT"] = "3"  # 3=Hybrid su LinkedIn
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Accept-Language": "it-IT,it;q=0.9",
                }
                response = requests.get(url, params=params, headers=headers, timeout=12)
                logging.info(f"{self.portal_name} ({kw} - {city_name}): HTTP {response.status_code}")
                
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, "html.parser")
                    
                    # Strategia 1: JSON-LD JobPosting
                    for script in soup.find_all("script", type="application/ld+json"):
                        try:
                            data = _json.loads(script.string or "{}")
                            items = data if isinstance(data, list) else [data]
                            for item in items:
                                if item.get("@type") == "JobPosting":
                                    title = item.get("title", "")
                                    if not is_valid_job_title(title):
                                        continue
                                    link = item.get("url", "")
                                    if not link or link in seen_links:
                                        continue
                                    seen_links.add(link)
                                    company = item.get("hiringOrganization", {}).get("name", "")
                                    date = item.get("datePosted", "")
                                    desc = item.get("description", "")
                                    match_level, match_count, work_mode, fetch_status, probabilita, motivazione = calcola_punteggio_e_modalita(link, desc)
                                    jobs.append(ScrapedJob(title, company, self.portal_name, link,
                                                           date=date, match_level=match_level,
                                                           match_count=match_count, city=city_name,
                                                           work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione))
                        except Exception:
                            pass
                    
                    # Strategia 2: card HTML standard LinkedIn (classe base-card)
                    if not jobs:
                        for card in soup.find_all("div", class_=lambda c: c and "base-card" in c):
                            title_elem = card.find(class_=lambda c: c and "base-search-card__title" in (c or ""))
                            company_elem = card.find(class_=lambda c: c and "base-search-card__subtitle" in (c or ""))
                            link_elem = card.find("a", class_=lambda c: c and "base-card__full-link" in (c or ""))
                            if title_elem and link_elem:
                                title = title_elem.get_text(strip=True)
                                if not is_valid_job_title(title):
                                    continue
                                link = link_elem["href"].split("?")[0]
                                if link in seen_links:
                                    continue
                                seen_links.add(link)
                                company = company_elem.get_text(strip=True) if company_elem else ""
                                match_level, match_count, work_mode, fetch_status, probabilita, motivazione = calcola_punteggio_e_modalita(link, title)
                                jobs.append(ScrapedJob(title, company, self.portal_name, link,
                                                       match_level=match_level, match_count=match_count,
                                                       city=city_name, work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione))
                
                time.sleep(2)
                
            except Exception as e:
                logging.error(f"Errore {self.portal_name} keyword '{kw}': {e}")
                
        return jobs

class IndeedScraper(BaseScraper):
    """
    Indeed IT — USA l'API ufficiale Indeed Publisher se INDEED_PUBLISHER_ID è nel .env.
    Se la variabile non è presente, lo scraper si disabilita con un warning (no silent fail).
    Registrazione gratuita: https://ads.indeed.com/jobroll/xmlfeed
    """
    def __init__(self):
        super().__init__("Indeed")
        self.publisher_id = os.getenv("INDEED_PUBLISHER_ID", "")
        if not self.publisher_id:
            logging.warning(
                "IndeedScraper disabilitato: INDEED_PUBLISHER_ID mancante nel .env. "
                "Registrati su https://ads.indeed.com/jobroll/xmlfeed per ottenerlo gratuitamente."
            )

    def scrape(self, city_name, city_config):
        import xml.etree.ElementTree as ET
        
        if not self.publisher_id:
            return []
        
        jobs = []
        keywords = ["marketing manager", "head of growth", "responsabile marketing"]
        
        for kw in keywords:
            try:
                url = "https://api.indeed.com/ads/apisearch"
                params = {
                    "publisher": self.publisher_id,
                    "q": kw,
                    "l": city_name,
                    "co": "it",
                    "v": "2",
                    "format": "xml",
                    "limit": 25,
                    "sort": "date",
                }
                response = requests.get(url, params=params, timeout=12)
                logging.info(f"{self.portal_name} ({kw} - {city_name}): HTTP {response.status_code}")
                
                if response.status_code == 200:
                    try:
                        root = ET.fromstring(response.text)
                        for result in root.findall(".//result"):
                            title = result.findtext("jobtitle", "")
                            if not is_valid_job_title(title):
                                continue
                            company = result.findtext("company", "")
                            link = result.findtext("url", "")
                            date = result.findtext("date", "")
                            snippet = result.findtext("snippet", "")
                            match_level, match_count, work_mode, fetch_status, probabilita, motivazione = calcola_punteggio_e_modalita(link, snippet)
                            jobs.append(ScrapedJob(title, company, self.portal_name, link,
                                                   date=date, snippet=snippet[:150],
                                                   match_level=match_level, match_count=match_count,
                                                   city=city_name, work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione))
                    except ET.ParseError as e:
                        logging.error(f"{self.portal_name}: XML parse error: {e}")
                else:
                    logging.error(f"{self.portal_name}: HTTP {response.status_code}")
                    
                time.sleep(1)
                
            except Exception as e:
                logging.error(f"Errore {self.portal_name} keyword '{kw}': {e}")
                
        return jobs

class InfojobsScraper(BaseScraper):
    """
    Infojobs IT — DISABILITATO.
    InfoJobs ha chiuso il servizio in Italia (pagina 'InfoJobs - Grazie Italia').
    """
    def __init__(self):
        super().__init__("Infojobs")

    def scrape(self, city_name, city_config):
        logging.warning("InfojobsScraper: servizio chiuso in Italia, scraper disabilitato.")
        return []


class MichaelPageScraper(BaseScraper):
    """
    MichaelPage IT — gli URL per-città restituiscono 404.
    Usa /jobs/marketing e /jobs/sales-marketing (tutte offerte IT) e filtra per titolo.
    """
    def __init__(self):
        super().__init__("MichaelPage")

    def scrape(self, city_name, city_config):
        jobs = []
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "it-IT,it;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        # Scrape solo per Genova per evitare duplicati (offerte italiane senza filtro città)
        if city_name != "Genova":
            return []
        urls = [
            "https://www.michaelpage.it/jobs/marketing",
            "https://www.michaelpage.it/jobs/sales-marketing",
        ]
        seen = set()
        for url in urls:
            try:
                response = requests.get(url, headers=headers, timeout=12)
                logging.info(f"{self.portal_name}: HTTP {response.status_code} ({url})")
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, "html.parser")
                    jobs.extend(self._parse_json_ld(soup, url, city_name))
                    for a in soup.find_all("a", href=lambda h: h and "/job-detail/" in h):
                        title = a.get_text(strip=True)
                        if title and title != "Candidati" and title not in seen:
                            seen.add(title)
                            if is_valid_job_title(title):
                                link = a["href"]
                                if not link.startswith("http"):
                                    link = "https://www.michaelpage.it" + link
                                match_level, match_count, work_mode, fetch_status, probabilita, motivazione = calcola_punteggio_e_modalita(link, "")
                                jobs.append(ScrapedJob(title, "", self.portal_name, link,
                                                       match_level=match_level, match_count=match_count,
                                                       city="Italia", work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione))
                else:
                    logging.error(f"{self.portal_name}: HTTP {response.status_code}")
            except requests.exceptions.Timeout:
                logging.error(f"{self.portal_name}: timeout della richiesta ({url})")
            except Exception as e:
                logging.error(f"Errore scraping {self.portal_name}: {e}")
        if not jobs:
            logging.info(f"{self.portal_name}: 0 offerte valide trovate dopo i filtri.")
        return jobs

    def _parse_json_ld(self, soup, base_url, city_name):
        import json
        jobs = []
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") == "JobPosting":
                        title = item.get("title", "")
                        if is_valid_job_title(title):
                            link = item.get("url", base_url)
                            company = item.get("hiringOrganization", {}).get("name", "")
                            date = item.get("datePosted", "")
                            match_level, match_count, work_mode, fetch_status, probabilita, motivazione = calcola_punteggio_e_modalita(link, item.get("description", ""))
                            jobs.append(ScrapedJob(title, company, self.portal_name, link,
                                                   date=date, match_level=match_level, match_count=match_count, city=city_name, work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione))
            except Exception:
                pass
        return jobs


class GiGroupScraper(BaseScraper):
    """
    GiGroup — La pagina è SSR (WordPress) con dati job embedded nel tag a[data-job].
    Il filtro città via querystring non funziona: si filtra per province nel JSON data-job.
    """
    def __init__(self):
        super().__init__("GiGroup")

    def scrape(self, city_name, city_config):
        import json as _json
        jobs = []
        url = f"https://www.gigroup.it/offerte-lavoro/?q=marketing+manager"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "it-IT,it;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        # Scrape solo per Genova per evitare 3x chiamate identiche (no filtro città)
        if city_name != "Genova":
            return []
        try:
            response = requests.get(url, headers=headers, timeout=12)
            logging.info(f"{self.portal_name}: HTTP {response.status_code}")
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                seen = set()
                # a[data-job] contiene JSON con offerTitle e province
                for a in soup.find_all("a", attrs={"data-job": True}):
                    try:
                        dj = _json.loads(a.get("data-job", "{}"))
                        title = dj.get("offerTitle", "")
                        href = a.get("href", "")
                        if not title or not href or href in seen:
                            continue
                        # Salta link esterni (mygigroup.com ecc.)
                        if "gigroup.it" not in href and href.startswith("http"):
                            continue
                        if not is_valid_job_title(title):
                            continue
                        seen.add(href)
                        link = href if href.startswith("http") else "https://www.gigroup.it" + href
                        province = dj.get("province", "")
                        job_city = province if province else "Italia"
                        match_level, match_count, work_mode, fetch_status, probabilita, motivazione = calcola_punteggio_e_modalita(link, "")
                        jobs.append(ScrapedJob(title, "GiGroup", self.portal_name, link,
                                               match_level=match_level, match_count=match_count,
                                               city=job_city, work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione))
                    except Exception:
                        pass
                if not jobs:
                    logging.info(f"{self.portal_name}: 0 offerte valide trovate dopo i filtri.")
            else:
                logging.error(f"{self.portal_name}: HTTP {response.status_code}")
        except requests.exceptions.Timeout:
            logging.error(f"{self.portal_name}: timeout della richiesta")
        except Exception as e:
            logging.error(f"Errore scraping {self.portal_name}: {e}")
        return jobs


class WyserScraper(BaseScraper):
    """
    Wyser — WordPress SSR. Ogni card è article.card-job con:
    - p.card-title (titolo) dentro a.dettaglio (link)
    - li.posto (città)
    URL per-città già filtra i risultati; timeout 25s per connessione lenta.
    """
    def __init__(self):
        super().__init__("Wyser")

    def scrape(self, city_name, city_config):
        jobs = []
        wyser_slug = city_config.get("wyser_slug", f"{city_name.lower()}")
        url = f"https://it.wyser-search.com/offerte-lavoro/{wyser_slug}/?wy_position=MARKETING"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "it-IT,it;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        try:
            response = requests.get(url, headers=headers, timeout=25)
            logging.info(f"{self.portal_name}: HTTP {response.status_code}")
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                cards = soup.find_all("article", class_=lambda c: c and "card-job" in c)
                for card in cards:
                    link_elem = card.find("a", class_="dettaglio")
                    title_elem = card.find("p", class_=lambda c: c and "card-title" in (c or ""))
                    date_elem = card.find("p", class_=lambda c: c and "size-16" in (c or "") and "blue" in (c or ""))
                    if not link_elem or not title_elem:
                        continue
                    title = title_elem.get_text(strip=True)
                    if not is_valid_job_title(title):
                        continue
                    link = link_elem["href"]
                    if not link.startswith("http"):
                        link = "https://it.wyser-search.com" + link
                    date = date_elem.get_text(strip=True) if date_elem else ""
                    match_level, match_count, work_mode, fetch_status, probabilita, motivazione = calcola_punteggio_e_modalita(link, "")
                    jobs.append(ScrapedJob(title, "", self.portal_name, link, date=date,
                                           match_level=match_level, match_count=match_count,
                                           city=city_name, work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione))
                if not jobs:
                    logging.info(f"{self.portal_name}: 0 offerte valide trovate dopo i filtri.")
            else:
                logging.error(f"{self.portal_name}: HTTP {response.status_code}")
        except requests.exceptions.Timeout:
            logging.error(f"{self.portal_name}: timeout della richiesta")
        except Exception as e:
            logging.error(f"Errore scraping {self.portal_name}: {e}")
        return jobs



class GlassdoorScraper(BaseScraper):
    """Glassdoor IT — usa cloudscraper per bypassare la protezione Cloudflare."""
    def __init__(self):
        super().__init__("Glassdoor")

    def scrape(self, city_name, city_config):
        jobs = []
        url = city_config.get("glassdoor_url", f"https://www.glassdoor.it/Job/{city_name.lower()}-marketing-manager-jobs-SRCH_IL.0,6_KO7,24.htm")
        try:
            scraper = cloudscraper.create_scraper(
                browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
            )
            response = scraper.get(url, timeout=15)
            logging.info(f"{self.portal_name}: HTTP {response.status_code}")
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                # Glassdoor usa li con data-test o article
                cards = (
                    soup.find_all("li", attrs={"data-test": True}) or
                    soup.find_all("article") or
                    soup.find_all("div", class_=lambda c: c and ("jobCard" in c or "JobCard" in c or "job-listing" in c))
                )
                for card in cards:
                    title_elem = card.find(["a", "span", "h3"], attrs={"data-test": lambda v: v and "job-title" in v.lower()}) or \
                                 card.find(["h3", "a"], class_=lambda c: c and ("title" in c.lower() or "jobTitle" in c.lower()))
                    link_elem = card.find("a", href=True)
                    company_elem = card.find(attrs={"data-test": lambda v: v and "employer-name" in v.lower()}) or \
                                   card.find(class_=lambda c: c and "employer" in c.lower())
                    if title_elem:
                        title = title_elem.get_text(strip=True)
                        if is_valid_job_title(title):
                            link = link_elem["href"] if link_elem else url
                            if not link.startswith("http"):
                                link = "https://www.glassdoor.it" + link
                            company = company_elem.get_text(strip=True) if company_elem else ""
                            match_level, match_count, work_mode, fetch_status, probabilita, motivazione = calcola_punteggio_e_modalita(link, "")
                            jobs.append(ScrapedJob(title, company, self.portal_name, link, match_level=match_level, match_count=match_count, city=city_name, work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione))
                if not jobs:
                    logging.info(f"{self.portal_name}: 0 offerte valide trovate dopo i filtri.")
            else:
                logging.error(f"{self.portal_name}: HTTP {response.status_code}")
        except Exception as e:
            logging.error(f"Errore scraping {self.portal_name}: {e}")
        return jobs


class RandstadScraper(BaseScraper):
    """
    Randstad IT — utilizza l'API GraphQL ufficiale (api.randstadservices.com).
    """
    def __init__(self):
        super().__init__("Randstad")

    def scrape(self, city_name, city_config):
        jobs = []
        url = "https://api.randstadservices.com/job/V1"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "client-id": os.getenv("RANDSTAD_CLIENT_ID", "F9FB4BD32A4FADCF1B7E361227151"),
            "content-type": "application/json",
            "accept": "application/json"
        }
        
        # Coordinate per la città
        lat = city_config.get("lat", 44.449518)
        lon = city_config.get("lon", 8.892783)
        
        search_query = {
            "query": """
            query ($search_mainSearchJobs: SearchInput, $query_mainSearchJobs: QueryInput, $opcoCodes_mainSearchJobs: [String!]!, $language_mainSearchJobs: LanguageCode!) {
                search: searchJobs (search: $search_mainSearchJobs, query: $query_mainSearchJobs, opcoCodes: $opcoCodes_mainSearchJobs, language: $language_mainSearchJobs) {
                    count
                    results {
                        document {
                            jobTitle
                            clientDetail {
                                name
                            }
                            webDetails {
                                postedUrl {
                                    href
                                }
                            }
                            postingDetail {
                                postingTime
                            }
                            description {
                                shortDescription
                            }
                        }
                    }
                }
            }
            """,
            "variables": {
                "search_mainSearchJobs": {
                    "searchValues": ["marketing manager", "responsabile marketing", "head of growth", "head of marketing", "digital marketing manager"]
                },
                "query_mainSearchJobs": {
                    "sort": {
                        "type": ["Relevance"]
                    },
                    "location": {
                        "latitude": lat,
                        "longitude": lon,
                        "distance": 30,
                        "unit": "km"
                    },
                    "range": {
                        "start": 0,
                        "end": 50
                    },
                },
                "opcoCodes_mainSearchJobs": ["IT-RS", "LOCAL-IT-RS"],
                "language_mainSearchJobs": "it"
            }
        }
        
        try:
            response = requests.post(url, json=search_query, headers=headers, timeout=12)
            logging.info(f"{self.portal_name}: HTTP {response.status_code}")
            if response.status_code in [401, 403]:
                logging.error(f"{self.portal_name}: HTTP {response.status_code} - client-id Randstad probabilmente scaduto/ruotato, verificare manualmente.")
                return []
            if response.status_code == 200:
                res2 = response.json()
                jobs_data = (res2.get("data") or {}).get("search", {})
                results = jobs_data.get("results", [])
                
                for res in results:
                    doc = res.get("document", {})
                    title = doc.get("jobTitle", "")
                    if is_valid_job_title(title):
                        company = doc.get("clientDetail", {}).get("name") or ""
                        posted_urls = doc.get("webDetails", {}).get("postedUrl", [])
                        link = posted_urls[0].get("href") if posted_urls else ""
                        if not link:
                            continue
                        date = doc.get("postingDetail", {}).get("postingTime", "")
                        desc = doc.get("description", {}).get("shortDescription", "")
                        
                        match_level, match_count, work_mode, fetch_status, probabilita, motivazione = calcola_punteggio_e_modalita(link, desc)
                        jobs.append(ScrapedJob(
                            title=title, 
                            company=company, 
                            portal=self.portal_name, 
                            link=link,
                            date=date, 
                            snippet=desc,
                            match_level=match_level, 
                            match_count=match_count,
                            city=city_name,
                            work_mode=work_mode,
                            fetch_status=fetch_status,
                            probabilita=probabilita,
                            motivazione=motivazione,
                        ))
                if not jobs:
                    logging.info(f"{self.portal_name}: 0 offerte valide trovate dopo i filtri.")
            else:
                logging.error(f"{self.portal_name}: HTTP {response.status_code} - Errore API Randstad GraphQL.")
        except requests.exceptions.Timeout:
            logging.error(f"{self.portal_name}: timeout della richiesta")
        except Exception as e:
            logging.error(f"Errore scraping {self.portal_name}: {e}")
            
        return jobs


class AdzunaScraper(BaseScraper):
    """
    Adzuna IT — i job sono in article[data-aid] (SSR, non lazy-loaded).
    Struttura: h2 > a[data-js="jobLink"] per titolo+link,
               div.ui-company per azienda, div.ui-location per città,
               span.max-snippet-height per snippet.
    JSON-LD non più presente nel DOM statico.
    """
    def __init__(self):
        super().__init__("Adzuna")

    def scrape(self, city_name, city_config):
        import json as _json
        jobs = []
        url = f"https://www.adzuna.it/search?q=marketing+manager&w={city_name}&sort_by=date"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "it-IT,it;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        try:
            response = requests.get(url, headers=headers, timeout=12)
            logging.info(f"{self.portal_name}: HTTP {response.status_code}")
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")

                # --- Strategia 1: JSON-LD (manteniamo per compatibilità futura) ---
                for script in soup.find_all("script", type="application/ld+json"):
                    try:
                        data = _json.loads(script.string or "{}")
                        items = data if isinstance(data, list) else [data]
                        for item in items:
                            if item.get("@type") == "JobPosting":
                                title = item.get("title", "")
                                if is_valid_job_title(title):
                                    link = item.get("url", url)
                                    company = item.get("hiringOrganization", {}).get("name", "")
                                    date = item.get("datePosted", "")
                                    desc = item.get("description", "")
                                    match_level, match_count, work_mode, fetch_status, probabilita, motivazione = calcola_punteggio_e_modalita(link, desc)
                                    jobs.append(ScrapedJob(title, company, self.portal_name,
                                                           link, date=date,
                                                           match_level=match_level, match_count=match_count,
                                                           city=city_name, work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione))
                    except Exception:
                        pass

                # --- Strategia 2: article[data-aid] (struttura attuale Adzuna) ---
                if not jobs:
                    import re as _re
                    seen = set()
                    for article in soup.find_all("article", attrs={"data-aid": True}):
                        # Titolo: h2 > a[data-js="jobLink"]
                        title_a = article.find("h2").find("a", attrs={"data-js": "jobLink"}) if article.find("h2") else None
                        if not title_a:
                            continue
                        # get_text(" ") evita concatenazione senza spazio per inline tags (<strong> ecc.)
                        title = _re.sub(r'\s+', ' ', title_a.get_text(" ", strip=True)).strip()
                        link = title_a.get("href", "")
                        if not title or not link or link in seen:
                            continue
                        if not link.startswith("http"):
                            link = "https://www.adzuna.it" + link
                        if not is_valid_job_title(title):
                            continue
                        seen.add(link)
                        company_el = article.find(class_="ui-company")
                        company = company_el.get_text(strip=True) if company_el else ""
                        snippet_el = article.find(class_="max-snippet-height")
                        snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
                        match_level, match_count, work_mode, fetch_status, probabilita, motivazione = calcola_punteggio_e_modalita(link, snippet)
                        jobs.append(ScrapedJob(title, company, self.portal_name, link,
                                               snippet=snippet[:150],
                                               match_level=match_level, match_count=match_count,
                                               city=city_name, work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione))

                if not jobs:
                    logging.error(f"{self.portal_name}: 0 offerte trovate. HTML preview: {response.text[:200]}")
            else:
                logging.error(f"{self.portal_name}: HTTP {response.status_code}")
        except requests.exceptions.Timeout:
            logging.error(f"{self.portal_name}: timeout della richiesta")
        except Exception as e:
            logging.error(f"Errore scraping {self.portal_name}: {e}")
        return jobs


class PagePersonnelScraper(BaseScraper):
    """Page Personnel IT — SSR Drupal (stessa infrastruttura di MichaelPage). Parsing dei link /job-detail/."""
    def __init__(self):
        super().__init__("PagePersonnel")

    def scrape(self, city_name, city_config):
        jobs = []
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "it-IT,it;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        # Prova URL per-città; se 404 (es. Genova), fallback all'URL nazionale
        city_url = f"https://www.pagepersonnel.it/jobs/marketing/{city_name.lower()}"
        national_url = "https://www.pagepersonnel.it/jobs/marketing"
        try:
            response = requests.get(city_url, headers=headers, timeout=12)
            if response.status_code == 404:
                logging.info(f"{self.portal_name}: {city_url} → 404, fallback a URL nazionale")
                response = requests.get(national_url, headers=headers, timeout=12)
                effective_city = "Italia"
            else:
                effective_city = city_name
            logging.info(f"{self.portal_name}: HTTP {response.status_code}")
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                
                # Prima prova: JSON-LD
                import json as _json
                for script in soup.find_all("script", type="application/ld+json"):
                    try:
                        data = _json.loads(script.string or "{}")
                        items = data if isinstance(data, list) else [data]
                        for item in items:
                            if item.get("@type") == "JobPosting":
                                title = item.get("title", "")
                                if is_valid_job_title(title):
                                    link = item.get("url", city_url)
                                    company = item.get("hiringOrganization", {}).get("name", "")
                                    date = item.get("datePosted", "")
                                    match_level, match_count, work_mode, fetch_status, probabilita, motivazione = calcola_punteggio_e_modalita(link, item.get("description", ""))
                                    jobs.append(ScrapedJob(title, company, self.portal_name, link,
                                                           date=date, match_level=match_level, match_count=match_count, city=effective_city, work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione))
                    except Exception:
                        pass
                
                # Seconda prova: link /job-detail/
                if not jobs:
                    seen = set()
                    for a in soup.find_all("a", href=lambda h: h and "/job-detail/" in h):
                        title = a.get_text(strip=True)
                        if title and title != "Candidati" and title not in seen:
                            seen.add(title)
                            if is_valid_job_title(title):
                                link = a["href"]
                                if not link.startswith("http"):
                                    link = "https://www.pagepersonnel.it" + link
                                match_level, match_count, work_mode, fetch_status, probabilita, motivazione = calcola_punteggio_e_modalita(link, "")
                                jobs.append(ScrapedJob(title, "", self.portal_name, link,
                                                       match_level=match_level, match_count=match_count, city=effective_city, work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione))
                if not jobs:
                    logging.info(f"{self.portal_name}: 0 offerte valide trovate dopo i filtri.")
            else:
                logging.error(f"{self.portal_name}: HTTP {response.status_code}")
        except requests.exceptions.Timeout:
            logging.error(f"{self.portal_name}: timeout della richiesta")
        except Exception as e:
            logging.error(f"Errore scraping {self.portal_name}: {e}")
        return jobs


class ManpowerScraper(BaseScraper):
    """Manpower IT — SSR con link /it/annuncio-lavoro/ e titoli h2. Cerca su Genova."""
    def __init__(self):
        super().__init__("Manpower")

    def scrape(self, city_name, city_config):
        jobs = []
        url = f"https://www.manpower.it/it/trova-lavoro/citta/{city_name.lower()}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "it-IT,it;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        try:
            response = requests.get(url, headers=headers, timeout=12)
            logging.info(f"{self.portal_name}: HTTP {response.status_code}")
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                
                # Manpower usa h2 con link a /it/annuncio-lavoro/
                for h2 in soup.find_all("h2"):
                    parent_a = h2.find_parent("a", href=True)
                    if parent_a and "/annuncio-lavoro/" in parent_a["href"]:
                        title = h2.get_text(strip=True)
                        if is_valid_job_title(title):
                            link = parent_a["href"]
                            if not link.startswith("http"):
                                link = "https://www.manpower.it" + link
                            match_level, match_count, work_mode, fetch_status, probabilita, motivazione = calcola_punteggio_e_modalita(link, "")
                            jobs.append(ScrapedJob(title, "", self.portal_name, link,
                                                   match_level=match_level, match_count=match_count, city=city_name, work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione))
                
                if not jobs:
                    logging.info(f"{self.portal_name}: 0 offerte valide trovate dopo i filtri.")
            else:
                logging.error(f"{self.portal_name}: HTTP {response.status_code}")
        except requests.exceptions.Timeout:
            logging.error(f"{self.portal_name}: timeout della richiesta")
        except Exception as e:
            logging.error(f"Errore scraping {self.portal_name}: {e}")
        return jobs


class IQMSelezioneScraper(BaseScraper):
    """IQM Selezione — SSR PHP. Parsing dei link a dettaglio.php?annuncio=XXX nella pagina posizioni aperte."""
    def __init__(self):
        super().__init__("IQMSelezione")

    def scrape(self, city_name, city_config):
        jobs = []
        url = "https://www.iqmselezione.it/posizioni-aperte-in-iqmselezione.php"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "it-IT,it;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        try:
            response = curl_requests.get(url, headers=headers, impersonate="chrome124", timeout=12)
            logging.info(f"{self.portal_name}: HTTP {response.status_code}")
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                
                for a in soup.find_all("a", href=lambda h: h and "dettaglio.php" in h):
                    title = a.get_text(strip=True)
                    # Filtriamo brutalmente per città nel titolo o nel testo del link se presente
                    if title and is_valid_job_title(title) and city_name.lower() in title.lower():
                        link = a["href"]
                        if not link.startswith("http"):
                            link = "https://www.iqmselezione.it/" + link
                        match_level, match_count, work_mode, fetch_status, probabilita, motivazione = calcola_punteggio_e_modalita(link, "")
                        jobs.append(ScrapedJob(title, "", self.portal_name, link,
                                               match_level=match_level, match_count=match_count, city=city_name, work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione))
                
                if not jobs:
                    logging.info(f"{self.portal_name}: 0 offerte valide trovate dopo i filtri.")
            else:
                logging.error(f"{self.portal_name}: HTTP {response.status_code}")
        except Exception as e:
            logging.error(f"Errore scraping {self.portal_name}: {e}")
        return jobs


class LhhScraper(BaseScraper):
    """LHH (Lee Hecht Harrison) — API nascosta POST /api/data/jobs/summarized con filtro geo radius=10km."""
    def __init__(self):
        super().__init__("LHH")

    def scrape(self, city_name, city_config):
        jobs = []
        keywords = ["marketing"]
        lhh_location = city_config.get("lhh_location", f"{city_name}%2C+Italia")

        url = "https://www.lhh.com/api/data/jobs/summarized"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
            "Origin": "https://www.lhh.com",
            "Referer": "https://www.lhh.com/it-it/cerca-lavoro"
        }

        for kw in keywords:
            payload = {
                "queryString": f"&q={kw}&jobLocation={lhh_location}&radius=10&sort=PostedDate desc",
                "filtersToDisplay": "{AEEBD4FE-DCF4-4D9B-8895-6EE4C1C31F95}|{9D842325-FA99-45EE-9197-AC1749D579DF}|{F4AA5EF6-7E6B-4BBA-B1E3-38E840537688}|{A5D28A27-7525-4F9C-813F-53E1B58D955F}|{366A4861-5C5C-4C12-9776-8CE4789960E0}|{26CA3CFC-0C11-4919-883F-2C8DB522BADC}",
                "range": 0,
                "siteName": "lhh",
                "brand": "lhh",
                "countryCode": "IT",
                "languageCode": "it-IT"
            }

            try:
                response = curl_requests.post(url, json=payload, headers=headers, impersonate="chrome124", timeout=15)
                logging.info(f"{self.portal_name} ({kw} - {city_name}): HTTP {response.status_code}")

                if response.status_code == 200:
                    data = response.json()
                    jobs_data = data.get("jobs", [])
                    logging.info(f"{self.portal_name} ({city_name}): {len(jobs_data)} offerte dalla API")
                    for job in jobs_data:
                        title = job.get("jobTitle", "")
                        if not is_valid_job_title(title):
                            continue
                        company = job.get("brandName", "LHH")
                        link = job.get("applyUri") or f"https://www.lhh.com/it-it/cerca-lavoro/job-description/?id={job.get('jobId')}"
                        date = job.get("postedDate", "")
                        desc = job.get("description", "") or ""
                        match_level, match_count, work_mode, fetch_status, probabilita, motivazione = calcola_punteggio_e_modalita(link, desc)
                        if job.get("isRemote") and work_mode == "unverified":
                            work_mode = "da remoto"
                        jobs.append(ScrapedJob(
                            title=title,
                            company=company,
                            portal=self.portal_name,
                            link=link,
                            date=date,
                            snippet=desc[:150] + "..." if desc else "",
                            match_level=match_level,
                            match_count=match_count,
                            city=city_name,
                            work_mode=work_mode,
                            fetch_status=fetch_status,
                            probabilita=probabilita,
                            motivazione=motivazione,
                        ))
                else:
                    logging.error(f"{self.portal_name}: Errore API HTTP {response.status_code}")
            except Exception as e:
                logging.error(f"Errore scraping {self.portal_name} per '{kw}': {e}")

        return jobs



# ==========================================
# LOGICA EMAIL
# ==========================================
def invia_email(nuove_offerte):
    if not GMAIL_USER or not GMAIL_APP_PASSWORD or not DESTINATION_EMAIL:
        logging.error("Credenziali email mancanti. Controlla il file .env")
        return False

    print(f"[DEBUG EMAIL] GMAIL_USER len={len(GMAIL_USER)} repr_start={repr(GMAIL_USER[:5])}")
    print(f"[DEBUG EMAIL] DESTINATION_EMAIL len={len(DESTINATION_EMAIL)} repr_start={repr(DESTINATION_EMAIL[:5])}")
    print(f"[DEBUG EMAIL] GMAIL_APP_PASSWORD len={len(GMAIL_APP_PASSWORD)}")

    data_oggi = datetime.now().strftime("%d/%m/%Y")
    
    msg = MIMEMultipart()
    msg['From'] = GMAIL_USER
    msg['To'] = DESTINATION_EMAIL
    msg['Subject'] = f"[Job Alert] Nuove offerte Multi-City - {data_oggi}"
    
    body = ""
    if not nuove_offerte:
        body += "Nessuna nuova offerta oggi.\n\n"
    else:
        # Raggruppa per città
        offerte_per_citta = {}
        for job in nuove_offerte:
            citta = job.city if job.city else "Altro"
            if citta not in offerte_per_citta:
                offerte_per_citta[citta] = []
            offerte_per_citta[citta].append(job)
            
        body += f"Trovate {len(nuove_offerte)} nuove offerte oggi, ordinate per affinità col CV:\n\n"
        
        for citta, offerte_citta in offerte_per_citta.items():
            offerte_citta.sort(key=lambda x: x.probabilita, reverse=True)
            body += f"===============================\n"
            body += f"📍 {citta.upper()} ({len(offerte_citta)} offerte)\n"
            body += f"===============================\n\n"

            for i, job in enumerate(offerte_citta, 1):
                prob = job.probabilita
                if prob >= 75:
                    prob_label = "🟢 ALTA"
                elif prob >= 50:
                    prob_label = "🟡 MEDIA"
                else:
                    prob_label = "🔴 BASSA"
                body += f"{i}. {job.title}\n"
                body += f"   Azienda: {job.company}\n"
                body += f"   Città: {job.city}\n"
                modalita_display = "Modalità non specificata nell'annuncio" if job.work_mode == "unverified" and job.fetch_status == "ok" else job.work_mode.upper()
                body += f"   Modalità: {modalita_display}\n"
                body += f"   Portale: {job.portal}\n"
                body += f"   Probabilità richiamata: {prob}% — {prob_label}\n"
                body += f"   → {job.motivazione}\n"
                body += f"   Match CV: {job.match_level} ({job.match_count} keyword)\n"
                body += f"   Data: {job.date}\n"
                body += f"   Link: {job.link}\n"
                if job.snippet:
                    body += f"   Snippet: {job.snippet}\n"
                body += "\n"
            
    body += f"Totale offerte: {len(nuove_offerte)}.\n\n"
    
    # --- SEZIONE COMPANY PROSPECTOR ---
    prospects_file = "daily_prospects.json"
    if os.path.exists(prospects_file):
        try:
            with open(prospects_file, "r", encoding="utf-8") as f:
                prospects = json.load(f)
            if prospects:
                body += "=========================================================\n"
                body += f"🌟 COMPANY PROSPECTOR: {len(prospects)} AZIENDE TARGET SELEZIONATE OGGI\n"
                body += "=========================================================\n\n"
                for p in prospects:
                    body += f"🏢 Azienda: {p.get('company', 'N/D')}\n"
                    body += f"📍 Città: {p.get('city', 'N/D')}\n"
                    body += f"💼 Settore: {p.get('sector', 'N/D')}\n"
                    body += f"🔗 Lavora con noi: {p.get('career_url', 'N/D')}\n"
                    body += f"📩 Candidatura Spontanea: {p.get('spontaneous_application', 'N/D')}\n"
                    body += f"👤 Contatto Chiave (LinkedIn): {p.get('key_person', 'N/D')}\n"
                    body += "---------------------------------------------------------\n\n"
                
                # Svuota il file per non rimetterle domani
                with open(prospects_file, "w", encoding="utf-8") as f:
                    json.dump([], f)
        except Exception as e:
            logging.error(f"Errore caricamento prospect: {e}")
            
    msg.attach(MIMEText(body, 'plain', 'utf-8'))
    
    retry_delays = [5, 15, 30]
    for attempt, delay in enumerate(retry_delays):
        try:
            with smtplib.SMTP('smtp.gmail.com', 587, timeout=10) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                server.send_message(msg)
            logging.info(f"Email inviata con successo a {DESTINATION_EMAIL}")
            
            if os.path.exists(PENDING_SCRAPER_EMAIL_FILE):
                os.remove(PENDING_SCRAPER_EMAIL_FILE)
            return True
        except Exception as e:
            import traceback
            logging.warning(f"Tentativo {attempt+1}/{len(retry_delays)} invio email fallito: {e}\n{traceback.format_exc()}")
            if attempt < len(retry_delays) - 1:
                time.sleep(delay)
                
    logging.error("Impossibile inviare l'email dopo tutti i tentativi. Salvataggio in pending.")
    try:
        with open(PENDING_SCRAPER_EMAIL_FILE, "w", encoding="utf-8") as f:
            json.dump([job.to_dict() for job in nuove_offerte], f)
    except Exception as e:
        logging.error(f"Errore salvataggio pending email: {e}")
    return False

def tenta_invio_pending_email():
    if os.path.exists(PENDING_SCRAPER_EMAIL_FILE):
        try:
            with open(PENDING_SCRAPER_EMAIL_FILE, "r", encoding="utf-8") as f:
                pending_data = json.load(f)
            if pending_data:
                logging.info("Trovata email scraper in pending, tento l'invio...")
                offerte = [ScrapedJob.from_dict(d) for d in pending_data]
                success = invia_email(offerte) # will try again
                # invia_email manages removing the file on success
        except Exception as e:
            logging.error(f"Errore retry pending email scraper: {e}")

# ==========================================
# MAIN JOB E SCHEDULING
# ==========================================
def filtra_offerte_per_citta(offerte_scraper, city_config):
    """Filtra le offerte in base alla configurazione della città.
    Genova (filter_hybrid_only=False): accetta in sede e ibrido, esclude da remoto.
    Milano/Torino (filter_hybrid_only=True): accetta solo ibrido (non da remoto, non in sede).
    In entrambi i casi include unverified se INCLUDE_UNVERIFIED=True.
    """
    offerte_filtrate = []
    for job in offerte_scraper:
        if city_config.get("filter_hybrid_only", False):
            # Milano/Torino: solo ibrido, non remoto, non in sede
            if job.work_mode == "ibrido":
                offerte_filtrate.append(job)
            elif job.work_mode == "unverified" and INCLUDE_UNVERIFIED:
                offerte_filtrate.append(job)
        else:
            # Genova: in sede o ibrido, mai da remoto
            if job.work_mode != "da remoto":
                offerte_filtrate.append(job)
    return offerte_filtrate

def get_job_id(link: str) -> str:
    """Restituisce un ID univoco per l'offerta basato sul link.
    Per Google Jobs (che usa htidocid come parametro query) estrae il parametro specifico.
    Per gli altri portali rimuove semplicemente i parametri query per evitare duplicati da tracking.
    """
    if "google.com/search" in link or "google.it/search" in link:
        import urllib.parse as urlparse
        try:
            parsed = urlparse.urlparse(link)
            params = urlparse.parse_qs(parsed.query)
            htidocid = params.get("htidocid", [""])[0]
            if htidocid:
                return f"google_jobs_{htidocid}"
        except Exception:
            pass
    return link.split("?")[0]


def esegui_scraping_job(orario_label):
    tenta_invio_pending_email()
    print(f"[{datetime.now()}] Avvio scraping delle {orario_label} in corso...")
    
    scrapers = [
        LinkedInScraper(),
        MichaelPageScraper(),
        PagePersonnelScraper(),
        WyserScraper(),
        RandstadScraper(),
        LhhScraper(),
    ]
    
    tutte_le_offerte = []
    
    for city_name, city_config in CITIES.items():
        print(f"  -> Scraping per città: {city_name}")
        for scraper in scrapers:
            offerte_scraper = scraper.scrape(city_name, city_config)
            
            # Filtro modalità ibrida/unverified se richiesto dalla città
            tutte_le_offerte.extend(filtra_offerte_per_citta(offerte_scraper, city_config))
        
    viste = load_viste()  # ora è un set
    nuove_offerte = []
    
    seen_titles = set()
    seen_snippets = set()
    
    for job in tutte_le_offerte:
        # Popola match_type in base al titolo
        job.match_type = get_match_type(job.title)
        job_id = get_job_id(job.link)
        
        if job_id in viste:
            continue
            
        # Genera signature per deduplicazione di contenuti identici (stessa azienda + città + titolo/snippet)
        import re
        def clean_sig(text):
            return re.sub(r'[^a-z0-9]', '', str(text).lower())
            
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
            
        # Aggiunge alle offerte uniche
        nuove_offerte.append(job)
        viste.add(job_id)   # .add() invece di .append()
        seen_titles.add(title_sig)
        if snippet_sig:
            seen_snippets.add(snippet_sig)
            
    save_viste(viste)
    
    giornaliere = load_giornaliere()
    for job in nuove_offerte:
        giornaliere.append(job.to_dict())
    save_giornaliere(giornaliere)
    
    msg_log = f"[SCRAPING {orario_label}] {len(nuove_offerte)} nuove offerte trovate"
    logging.info(msg_log)
    print(msg_log)

def invia_email_job():
    print(f"[{datetime.now()}] Avvio invio email report giornaliero...")
    giornaliere_dicts = load_giornaliere()
    offerte_da_inviare = [ScrapedJob.from_dict(d) for d in giornaliere_dicts]

    success = invia_email(offerte_da_inviare)

    if success:
        msg_log = f"[EMAIL 18:00] Email inviata con successo: {len(offerte_da_inviare)} offerte totali del giorno"
        logging.info(msg_log)
        print(msg_log)
        clear_giornaliere()
    else:
        msg_err = f"[EMAIL 18:00] ERRORE: invio email fallito dopo tutti i tentativi. Le offerte restano in giornaliere.json."
        logging.error(msg_err)
        print(msg_err)
        sys.exit(1)

def reset_notturno():
    clear_giornaliere()
    logging.info("[RESET 00:00] Svuotato file offerte_giornaliere.json come sicurezza.")

# ==========================================
# SCHEDULER
# ==========================================
if __name__ == "__main__":
    if not valida_credenziali_email():
        print("ERRORE: GMAIL_APP_PASSWORD non valida o mancante! Assicurati che sia lunga 16 caratteri senza spazi.")
        sys.exit(1)

    print("Sistema di scraping avviato.")
    
    # Import sicuri dei moduli opzionali
    run_concorsi_module = None
    run_prospect_module = None
    
    try:
        from concorsi_module import run_concorsi_module
        print("[OK] concorsi_module caricato")
    except ImportError:
        logging.warning("concorsi_module non trovato — modulo concorsi disabilitato")
        print("[WARN] concorsi_module non trovato, ignorato")
    
    # try:
    #     from company_prospect_module import run_prospect_module
    #     print("[OK] company_prospect_module caricato")
    # except ImportError:
    #     logging.warning("company_prospect_module non trovato — prospecting disabilitato")
    #     print("[WARN] company_prospect_module non trovato, ignorato")
    run_prospect_module = None
    
    schedule.every().day.at("09:00").do(esegui_scraping_job, orario_label="09:00")
    
    if run_concorsi_module:
        schedule.every().day.at("10:00").do(run_concorsi_module)
    
    schedule.every().day.at("12:00").do(esegui_scraping_job, orario_label="12:00")
    schedule.every().day.at("15:00").do(esegui_scraping_job, orario_label="15:00")
    schedule.every().day.at("17:30").do(esegui_scraping_job, orario_label="17:30")
    
    if run_prospect_module:
        schedule.every().day.at("17:30").do(run_prospect_module)
    
    schedule.every().day.at("18:00").do(invia_email_job)
    schedule.every().day.at("00:00").do(reset_notturno)
    
    print("\nProssimi eventi schedulati:")
    jobs = schedule.get_jobs()
    events = sorted([j.next_run for j in jobs])
    
    for i, ev in enumerate(events[:5], 1):
        print(f" {i}. {ev.strftime('%Y-%m-%d %H:%M:%S')}")
        
    print("\nPremere Ctrl+C per interrompere.\n")
    
    while True:
        schedule.run_pending()
        time.sleep(60)
