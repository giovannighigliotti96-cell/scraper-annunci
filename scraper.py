import os
import re
import time
import json
import shutil
import logging
import socket
import ipaddress
import urllib.parse
from datetime import datetime
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import PyPDF2
from curl_cffi import requests as curl_requests
from email.mime.application import MIMEApplication
import state_io
import llm_utils
try:
    import anthropic
    ANTHROPIC_SDK_AVAILABLE = True
except ImportError:
    ANTHROPIC_SDK_AVAILABLE = False
try:
    from cv_personalizzazione import genera_cv_per_offerta
    CV_PERSONALIZZAZIONE_DISPONIBILE = True
except ImportError:
    CV_PERSONALIZZAZIONE_DISPONIBILE = False

# ==========================================
# CONFIGURAZIONE INIZIALE
# ==========================================
load_dotenv(override=True)

# User-Agent condiviso da tutti gli scraper (e importato da cv_personalizzazione.py
# per il suo fetch di fallback): prima era copiato in ~10 punti diversi, con almeno
# una versione Chrome rimasta indietro (120 invece di 124) senza che nessuno se ne
# accorgesse finché una code review non l'ha ritrovata.
USER_AGENT_CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

GMAIL_USER = os.getenv("GMAIL_USER", "").strip().lstrip('﻿').strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").replace('"', '').replace("'", "").lstrip('﻿').strip()
DESTINATION_EMAIL = os.getenv("DESTINATION_EMAIL", "").strip().lstrip('﻿').strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
INCLUDE_UNVERIFIED = True
SOGLIA_CV_PERSONALIZZATO = 80  # probabilita >= a questa soglia attiva la personalizzazione CV
CV_PERSONALIZZAZIONE_BUDGET_SECONDI = 480  # tempo massimo totale dedicato alla personalizzazione CV per run email

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
        # Nessuno slug città Wyser: verificato dal vivo che "torino-to" non esiste
        # nel menu reale del sito (Torino non è tra le città disponibili) — un
        # slug non riconosciuto fa fallback silenzioso mostrando TUTTI gli annunci
        # nazionali senza errore, causando offerte di altre città etichettate
        # "Torino". WyserScraper gestisce wyser_slug assente scaricando la
        # pagina nazionale e filtrando per "torino" nel campo città di ogni card.
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
    pwd = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").replace('"', '').replace("'", "").lstrip('﻿').strip()
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

        if not cv_skills:
            # Estrazione riuscita ma zero keyword trovate (es. un problema di
            # legature/kerning di PyPDF2 che spezza le parole): senza questo
            # fallback CV_SKILLS resterebbe [] per l'intera durata del processo,
            # forzando ogni offerta del giorno a match_level "Base".
            logging.warning(f"Estrazione CV riuscita ma nessuna competenza trovata in {pdf_path}: uso la lista base completa come fallback.")
            return TARGET_SKILLS

        logging.info(f"Trovate {len(cv_skills)} competenze nel CV: {', '.join(cv_skills)}")
        return list(cv_skills)
    except Exception as e:
        logging.error(f"Errore lettura CV: {e}")
        return TARGET_SKILLS

# Carica competenze dal CV all'avvio
CV_SKILLS = estrai_keyword_cv(CV_FILE)

def estrai_testo_cv(pdf_path):
    """Estrae il testo integrale del CV (non solo le keyword), per il match
    semantico via LLM in valuta_match_candidato(). Non abbassa il case: i nomi
    propri/acronimi aiutano il modello a leggere meglio il documento."""
    if not os.path.exists(pdf_path):
        return ""
    try:
        testo_cv = ""
        with open(pdf_path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                testo = page.extract_text()
                if testo:
                    testo_cv += testo + "\n"
        return testo_cv.strip()
    except Exception as e:
        logging.error(f"Errore estrazione testo integrale CV: {e}")
        return ""

# Testo integrale del CV, caricato una sola volta all'avvio per il match LLM
CV_TESTO_COMPLETO = estrai_testo_cv(CV_FILE)

# ==========================================
# MATCH SEMANTICO CV-ANNUNCIO VIA CLAUDE
# ==========================================
_anthropic_client = None
_anthropic_warning_shown = False

def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None and ANTHROPIC_SDK_AVAILABLE and ANTHROPIC_API_KEY:
        # timeout allineato a effort "high" (ragionamento più lento): 30s era troppo
        # basso e causava fallback silenzioso all'euristica a keyword sotto carico normale.
        # max_retries lasciato al default SDK (2, cioè 3 tentativi): un giro precedente
        # lo aveva ridotto a 1 per limitare il caso peggiore, ma test reali su questo
        # stesso progetto hanno mostrato errori 529 Overloaded genuini durante il normale
        # funzionamento — ridurre i retry aumenta la frequenza di fallback silenzioso
        # all'euristica a keyword proprio nei momenti di sovraccarico transitorio
        # dell'API, senza nemmeno garantire un tempo massimo reale (nessun budget di
        # tempo complessivo esiste per questo loop, a differenza della personalizzazione CV).
        _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=120.0)
    return _anthropic_client

# Sonnet 4.6 (non 5) + effort "medium" (non "high"): scelta esplicita dell'utente
# dopo l'esaurimento crediti Anthropic del 19-20/07, per contenere il consumo.
MATCH_LLM_MODEL = "claude-sonnet-4-6"

# Istruzioni di ruolo fisse + CV: separate dal testo dell'annuncio (che cambia
# ad ogni chiamata) e messe nel system prompt con cache_control, così le ~5-6
# chiamate al giorno condividono la cache invece di ripagare l'intero CV ogni volta.
_MATCH_LLM_SYSTEM_TEMPLATE = """Sei un recruiter esperto che valuta la compatibilità tra un candidato e un'offerta di lavoro, leggendo l'intero testo di entrambi — non un confronto di parole chiave.

Per ogni annuncio che ricevi, analizzalo nel suo complesso e valuta la compatibilità del candidato considerando:
- Requisiti espliciti (titolo di studio, anni di esperienza, settore di provenienza specifico, hard skill) — se il CV non li soddisfa chiaramente, segnalalo come gap concreto, non ignorarlo
- Valori o cultura aziendale menzionati nell'annuncio (es. "spirito imprenditoriale", "mentalità start-up", "orientamento al cliente") e se il CV li dimostra anche implicitamente, tramite esperienze equivalenti anche se descritte con parole diverse (es. aver fondato un'azienda dimostra imprenditorialità anche se quella parola non compare nel CV)
- Esperienza trasferibile che risponde allo spirito della richiesta anche senza corrispondenza letterale di keyword

Dai sempre un punteggio 0-100 di probabilità di essere richiamato per un colloquio, e una motivazione breve (massimo 3 righe, in italiano) che citi sia i punti di forza concreti sia eventuali gap reali rilevati nell'annuncio. Sii onesto sui gap: non gonfiare il punteggio per compiacere, un punteggio basso ben motivato è più utile di uno ottimistico e vago.

CV DEL CANDIDATO:
{cv_testo}"""

def valuta_match_llm(job_text: str) -> tuple:
    """Legge l'intero testo dell'annuncio (non solo keyword) e lo confronta con
    l'intero CV tramite Claude, per cogliere anche match impliciti/qualitativi
    (es. un valore aziendale come "spirito imprenditoriale" soddisfatto da
    un'esperienza da founder, anche se la parola non compare nel CV) e segnalare
    esplicitamente i requisiti che il candidato non soddisfa (settore di
    provenienza, titolo di studio specifico, ecc.).
    Ritorna (probabilita, motivazione) oppure None se la chiamata non è
    disponibile o fallisce — il chiamante deve ricadere sull'euristica a
    keyword in questo caso, per non bloccare mai lo scraping.
    """
    global _anthropic_warning_shown
    client = _get_anthropic_client()
    if client is None or not CV_TESTO_COMPLETO:
        if not _anthropic_warning_shown:
            if not ANTHROPIC_SDK_AVAILABLE:
                logging.warning("Match LLM disabilitato: pacchetto 'anthropic' non installato, uso l'euristica a keyword.")
            elif not ANTHROPIC_API_KEY:
                logging.warning("Match LLM disabilitato: ANTHROPIC_API_KEY mancante, uso l'euristica a keyword.")
            elif not CV_TESTO_COMPLETO:
                logging.warning("Match LLM disabilitato: testo del CV non disponibile, uso l'euristica a keyword.")
            _anthropic_warning_shown = True
        return None

    system_prompt = _MATCH_LLM_SYSTEM_TEMPLATE.format(cv_testo=CV_TESTO_COMPLETO[:6000])

    try:
        response = client.messages.create(
            model=MATCH_LLM_MODEL,
            # 4096 era comunque stretto: nessun parametro thinking è impostato, quindi
            # il ragionamento adattivo è attivo di default e consuma lo stesso budget
            # della risposta JSON finale. 16000 allinea il margine reale a quello già
            # usato in cv_personalizzazione.py.
            max_tokens=16000,
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }],
            messages=[{
                "role": "user",
                "content": f"TESTO INTEGRALE DELL'OFFERTA DI LAVORO:\n{job_text[:8000]}",
            }],
            output_config={
                "effort": "medium",
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "probabilita": {"type": "integer"},
                            "motivazione": {"type": "string"},
                        },
                        "required": ["probabilita", "motivazione"],
                        "additionalProperties": False,
                    },
                },
            },
        )
        testo_risposta = llm_utils.estrai_testo_risposta(response)
        dati = json.loads(testo_risposta)
        probabilita = max(0, min(100, int(dati["probabilita"])))
        motivazione = dati["motivazione"].strip()
        return probabilita, motivazione
    except Exception as e:
        logging.warning(f"Match LLM fallito, ricado sull'euristica a keyword: {type(e).__name__}: {e}")
        return None

def valuta_match_candidato(job_text: str) -> tuple:
    """Punto d'ingresso unico per lo scoring di compatibilità: prova il match
    semantico via LLM (valuta_match_llm) e, solo se non disponibile o fallito,
    ricade sull'euristica a keyword esistente (calcola_probabilita_callback) —
    così un problema con l'API Claude non blocca mai lo scraping."""
    risultato_llm = valuta_match_llm(job_text)
    if risultato_llm is not None:
        return risultato_llm
    return calcola_probabilita_callback(job_text.lower())

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
    # "flessibile"/"flessibilità" rimossi: in italiano indicano quasi sempre orario
    # di lavoro flessibile ("orario flessibile"), non modalità ibrida/remota — un
    # annuncio completamente in sede con questa dicitura veniva erroneamente
    # etichettato "ibrido" e passava il filtro solo-ibrido di Milano/Torino.
    hybrid_patterns = ["ibrido", "ibrida", "hybrid", "lavoro misto", "presenza e remoto",
                       "remoto e presenza",
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

def _url_is_safe_to_fetch(url: str) -> bool:
    """Protezione SSRF: gli URL scaricati qui provengono da dati JSON-LD/API di
    terze parti (i portali di lavoro), non generati da noi — un portale
    compromesso o un bug di parsing potrebbe altrimenti far puntare una
    richiesta HTTP in uscita verso un indirizzo interno/privato inatteso."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname
        if not host:
            return False
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            # host è un dominio, non un IP letterale: risolvilo per bloccare anche
            # un dominio che punta deliberatamente a un indirizzo privato/interno.
            try:
                ip = ipaddress.ip_address(socket.gethostbyname(host))
            except Exception:
                # Risoluzione DNS fallita qui: lascia fallire la richiesta HTTP vera
                # e propria con il suo errore di connessione, non un blocco silenzioso.
                return True
        return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)
    except Exception:
        return False

def _safe_get(url, headers, timeout, max_redirects=5):
    """Come requests.get, ma con allow_redirects=False e ri-validazione manuale
    di ogni hop: un url pubblico che supera _url_is_safe_to_fetch potrebbe
    comunque rispondere con un 302 verso un indirizzo interno/privato, e
    allow_redirects=True (default di requests) lo seguirebbe silenziosamente
    senza mai ripassare dal controllo SSRF sulla destinazione reale."""
    for _ in range(max_redirects):
        if not _url_is_safe_to_fetch(url):
            raise ValueError(f"URL non sicuro da scaricare: {url}")
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=False)
        if resp.is_redirect and resp.headers.get("Location"):
            url = urllib.parse.urljoin(url, resp.headers["Location"])
            continue
        return resp
    raise ValueError(f"Troppi redirect ({max_redirects}) seguendo: {url}")

def calcola_punteggio_e_modalita(url, snippet):
    """Scarica il testo dell'offerta (se possibile), calcola le skill e rileva la modalità di lavoro.
    Ritorna anche testo_originale (snippet + testo scaricato) come ultimo elemento,
    così i chiamanti possono riusarlo (es. personalizzazione CV) senza doverlo
    riscaricare da capo."""
    # snippet può arrivare None quando un JSON-LD ha "description": null: senza
    # questa guardia .lower() più sotto solleverebbe AttributeError non catturato.
    testo_originale = snippet or ""
    fetch_status = "no_attempt"
    if not _url_is_safe_to_fetch(url):
        logging.warning(f"URL scartato (non http/https o punta a un indirizzo privato/interno): {url}")
        return "Base", 0, "unverified", "http_error", 0, "", testo_originale
    try:
        headers = {"User-Agent": USER_AGENT_CHROME}
        resp = _safe_get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            testo_originale += " " + soup.get_text(" ", strip=True)
            fetch_status = "ok"
        else:
            fetch_status = "http_error"
    except requests.exceptions.Timeout:
        fetch_status = "timeout"
        logging.warning(f"Timeout scaricamento testo completo per {url}")
    except Exception as e:
        fetch_status = "http_error"
        logging.warning(f"Impossibile scaricare testo completo per {url}: {e}")

    testo_completo = testo_originale.lower()
    count = 0
    for skill in CV_SKILLS:
        if skill in testo_completo:
            count += 1

    work_mode = detect_work_mode(testo_completo)
    probabilita, motivazione = valuta_match_candidato(testo_originale)

    if count >= 3:
        return "Alto", count, work_mode, fetch_status, probabilita, motivazione, testo_originale
    elif count >= 1:
        return "Medio", count, work_mode, fetch_status, probabilita, motivazione, testo_originale
    else:
        return "Base", count, work_mode, fetch_status, probabilita, motivazione, testo_originale

# ==========================================
# TARGET JOB TITLES E MATCHER
# ==========================================
MATCHING_MODE = os.getenv("MATCHING_MODE", "moderate").lower()

# Lista titoli ESATTI: il titolo deve contenere una di queste stringhe (match
# case-insensitive, sottostringa)
EXACT_TITLES = [
    # English target titles
    "digital sales and marketing manager",
    "digital sales & marketing manager",
    # "sales and/& digital marketing manager": stesso ruolo di quello sopra ma
    # con "digital" spostato dopo "sales" — verificato dal vivo (MichaelPage)
    # che un titolo reale con questo ordine di parole ("Sales and Digital
    # Marketing Manager") non veniva trovato dalla sola versione con "digital"
    # in testa, essendo il match una sottostringa esatta non robusta all'ordine.
    "sales and digital marketing manager",
    "sales & digital marketing manager",
    "growth marketing manager",
    # "growth manager" puro (senza "marketing"): stesso ruolo scritto in forma
    # più corta, visto ricorrere in annunci reali durante un giro di verifica
    # dei titoli scartati — decisione esplicita dell'utente di includerlo.
    "growth manager",
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
    # "sales and/& marketing manager" (ordine invertito, generico non-digital):
    # prima esclusa di default salvo qualificatore "digital" esplicito, perché
    # un caso reale trovato nell'audit ("Director of Sales & Marketing - Luxury
    # Hospitality") era chiaramente fuori target. Decisione esplicita
    # dell'utente: non escludere più a priori per titolo, il doppio controllo
    # sul settore lo fa già lo scoring a valle leggendo il testo integrale
    # dell'annuncio (euristica a keyword su _CV_GAP "Settore Luxury/Fashion"
    # ecc., o l'LLM che segnala il gap in motivazione) — verificato che questo
    # meccanismo funziona già (es. gap "settore sportivo" per Volée Football).
    "sales and marketing manager",
    "sales & marketing manager",
    "growth and gtm manager",
    "growth & gtm manager",
    "marketing manager",

    # Varianti "Director" degli stessi ruoli sopra: verificato dal vivo (MichaelPage)
    # un annuncio reale "Director of Sales & Marketing" scartato perché nessuna
    # variante con "Director" esisteva in questa lista — l'intero livello di
    # seniority "Director" era strutturalmente escluso su tutti i portali.
    "digital sales and marketing director",
    "digital sales & marketing director",
    "sales and digital marketing director",
    "sales & digital marketing director",
    "growth marketing director",
    "digital marketing director",
    "revenue growth director",
    "go to market director",
    "go-to-market director",
    "demand generation director",
    "b2b marketing director",
    "performance marketing director",
    "customer acquisition director",
    "crm and marketing automation director",
    "crm & marketing automation director",
    "commercial strategy director",
    "digital sales director",
    "growth and gtm director",
    "growth & gtm director",
    "marketing director",
    # "sales and/& marketing director" (ordine invertito, generico): stessa
    # decisione di cui sopra per il livello Manager, estesa a Director.
    "sales and marketing director",
    "sales & marketing director",
    # "Director of X" (Director in testa, non in coda): pattern diverso da
    # "X Director" sopra, non coperto dal semplice controllo per sottostringa.
    # Aggiunto esplicitamente perché l'esempio reale discusso con l'utente
    # ("Director of Sales & Marketing - Luxury Hospitality") usa proprio
    # questo ordine.
    "director of sales and marketing",
    "director of sales & marketing",
    "director of marketing and sales",
    "director of marketing & sales",

    # Italian target titles
    "responsabile marketing & sales",
    "responsabile marketing e sales",
    "responsabile marketing",
    # "responsabile sales & marketing" (ordine invertito): stessa decisione
    # di cui sopra, versione italiana.
    "responsabile sales & marketing",
    "responsabile sales e marketing",
    "direttore marketing",
]

# Keyword di ricerca da inviare alle API/search box dei portali che supportano
# la ricerca per titolo (LinkedIn, LHH, GiGroup). Una voce per ciascun titolo
# target distinto in EXACT_TITLES (varianti di punteggiatura "&"/"e"/"and"
# accorpate in una sola voce). I portali che invece scaricano un'intera
# pagina categoria e filtrano lato client (MichaelPage, PagePersonnel, Wyser,
# Manpower, IQMSelezione) vedono già tutti i titoli tramite is_valid_job_title
# e non hanno bisogno di questa lista.
SEARCH_KEYWORDS = [
    "Digital Sales and Marketing Manager",
    "Growth Marketing Manager",
    "Head of Growth",
    "Digital Marketing Manager",
    "Revenue Growth Manager",
    "Go to Market Manager",
    "Demand Generation Manager",
    "B2B Marketing Manager",
    "Performance Marketing Manager",
    "Customer Acquisition Manager",
    "CRM and Marketing Automation Manager",
    "Commercial Strategy Manager",
    "Digital Sales Manager",
    "Marketing and Sales Manager",
    "Sales & Marketing Manager",
    "Growth and GTM Manager",
    "Growth Manager",
    "Marketing Manager",
    "Responsabile Marketing & Sales",
    "Responsabile Sales & Marketing",
    "Responsabile Marketing",
    # Varianti "Director" aggiunte insieme al livello di seniority in EXACT_TITLES:
    # solo un sottoinsieme mirato (non tutte le 20 varianti Manager sopra) per non
    # raddoppiare il volume di richieste a LinkedIn/LHH, già aumentato dalla
    # paginazione introdotta sugli stessi portali.
    "Marketing Director",
    "Digital Marketing Director",
    "Digital Sales and Marketing Director",
]

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
    # "sales and/& marketing manager/director" (generico, non-digital) NON è
    # più escluso qui: prima veniva scartato salvo qualificatore "digital",
    # ma decisione esplicita dell'utente è di lasciarlo passare sempre e
    # affidare il controllo settore allo scoring a valle (keyword _CV_GAP o
    # LLM), che legge il testo integrale dell'annuncio invece del solo
    # titolo — un doppio controllo più preciso di un'esclusione cieca sul
    # titolo (vedi commento in EXACT_TITLES).
    "international marketing manager",
    "responsabile marketing eventi",
    "responsabile marketing di prodotto",
    "responsabile marketing prodotto",
    "responsabile marketing contenuti",
    "responsabile marketing e comunicazione",
    "responsabile marketing digitale social",
    "responsabile social media",
    "responsabile ufficio stampa",
    "responsabile pubbliche relazioni",
    "responsabile trade marketing",
    "responsabile marketing canale",
    "responsabile marketing affiliazioni",
    "responsabile email marketing",
    "responsabile marketing internazionale",
    "category manager",
    "store manager",
    "account manager",
    "project manager",
    
    # Seniority non target (junior/stage). "graduate" aggiunto insieme a
    # "growth manager": senza, "Graduate Growth Manager" (visto in un annuncio
    # reale durante un giro di verifica dei titoli scartati) sarebbe passato.
    "stage", "tirocinio", "junior", "internship", "trainee", "entry level", "unpaid", "apprendistato", "apprendista", "graduate",
    
    # Ruoli retail/negozio/venditore non digital
    "commesso", "addetto vendita", "addetta vendita", "cassiere", "cassiera", "scaffalista",
    "promoter", "hostess", "steward", "call center", "operatore telefonico", "operatrice telefonica",
    "agente di commercio", "monomandatario", "plurimandatario", "sales representative", "consulente commerciale",
    "venditore", "venditrice", "front office", "receptionist",
    
    # Ruoli content/social media puri
    "social media manager", "content creator", "copywriter", "graphic designer", "web designer",
    "event planner", "event specialist", "event manager", "public relations", "pr specialist"
]

def _safe_str(d, key, default=""):
    """dict.get(key, default) applica il default SOLO se la chiave manca, non
    se è presente con valore JSON null esplicito (es. "title": null, comune
    per annunci anonimi/agenzia). Questo helper copre entrambi i casi, ed è il
    punto unico da correggere invece di ripetere `x.get(k) or default` in ogni
    scraper — più scraper avevano lo stesso identico buco duplicato."""
    val = d.get(key)
    return val if val else default

def _eta_giorni_da_data(date_str: str):
    """Età in giorni di una data ISO (YYYY-MM-DD, anche come prefisso di un
    datetime completo tipo YYYY-MM-DDTHH:MM:SSZ). Ritorna None se mancante o
    non parsabile — un formato inatteso non deve mai escludere un annuncio
    per errore, solo non applicare il filtro di freschezza a quello specifico."""
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        return (datetime.now().date() - d).days
    except Exception:
        return None

def is_valid_job_title(title: str) -> bool:
    """
    Restituisce True se il titolo corrisponde a uno dei ruoli target.
    Logica: EXACT_TITLES match AND NOT EXCLUSION match.
    """
    t = title.lower().strip()
    
    # 1. Controlla esclusioni prima di tutto
    for excl in TITLE_EXCLUSIONS:
        if excl in t:
            return False
    
    # 2. Match sui titoli target (sottostringa)
    for exact in EXACT_TITLES:
        if exact in t:
            return True
            
    return False

# ==========================================
# GESTIONE DATI E DEDUPLICAZIONE
# ==========================================
VISTE_MAX_AGE_DAYS = 90  # Pulisce automaticamente URL più vecchi di 90 giorni

# La scrittura atomica vive in state_io.py (dipendenze zero, solo stdlib) così
# gli step "Salva memoria"/"Salva stato post-email" dei workflow GitHub Actions
# possono importare la STESSA implementazione invece di duplicarla — un fix
# qui si applica automaticamente anche lì, senza bisogno di replicarlo a mano.
_atomic_write_json = state_io.atomic_write_json

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

        _atomic_write_json(VISTE_FILE, merged)
    except Exception as e:
        logging.error(f"Errore save_viste: {e}")

def load_giornaliere():
    # except Exception (non solo JSONDecodeError) copre anche FileNotFoundError,
    # per una finestra TOCTOU tra il check os.path.exists e l'open qui sotto —
    # coerente con load_viste(), che già degrada a un default vuoto invece di
    # propagare l'eccezione e far crashare l'intero run.
    try:
        if not os.path.exists(GIORNALIERE_FILE):
            return []
        with open(GIORNALIERE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"Errore load_giornaliere: {e}")
        return []

def save_giornaliere(jobs_dict_list):
    # Come save_viste: un'eccezione qui non deve propagarsi e interrompere il
    # chiamante (es. run_manual_scrape.py scriverebbe comunque nuove_offerte_run.json
    # subito dopo, sulla base di `viste` già salvato — un crash a questo punto
    # perderebbe quelle offerte, marcate "viste" ma mai registrate da nessuna parte).
    try:
        _atomic_write_json(GIORNALIERE_FILE, jobs_dict_list, indent=4, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Errore save_giornaliere: {e}")

def clear_giornaliere():
    save_giornaliere([])

# ==========================================
# CLASSI SCRAPERS PER SINGOLI PORTALI
# ==========================================

class ScrapedJob:
    def __init__(self, title, company, portal, link, date="", snippet="", match_level="Base", match_count=0, city="", work_mode="unverified", fetch_status="no_attempt", probabilita=0, motivazione="", testo_completo=""):
        # title/snippet guardati come company/date: un valore None (es. da un record
        # legacy con "title": null in offerte_giornaliere.json) non deve far crashare
        # il costruttore con AttributeError su .strip().
        self.title = title.strip() if title else ""
        self.company = company.strip() if company else "Azienda non specificata"
        self.portal = portal
        # Stessa guardia null di title/company/date/snippet, estesa a tutti i campi
        # stringa: prima solo un sottoinsieme era protetto, un record legacy/corrotto
        # con uno qualunque di questi a null (round-trip JSON) faceva crashare più
        # avanti (es. link.split() in get_job_id, work_mode.upper() nell'email).
        self.link = link if link else ""
        self.date = date.strip() if date else "Data non disponibile"
        self.snippet = snippet.strip() if snippet else ""
        self.match_level = match_level if match_level else "Base"
        self.match_count = match_count
        self.city = city
        self.work_mode = work_mode if work_mode else "unverified"
        self.fetch_status = fetch_status if fetch_status else "no_attempt"
        # probabilita è numerica (confrontata con >= altrove): un null/valore non
        # convertibile deve degradare a 0, non propagarsi come None nei confronti.
        try:
            self.probabilita = int(probabilita)
        except (TypeError, ValueError):
            self.probabilita = 0
        self.motivazione = motivazione if motivazione else ""
        # Testo integrale dell'annuncio già scaricato durante lo scoring: permette
        # alla personalizzazione CV di riusarlo invece di riscaricare la pagina
        # ore dopo (quando potrebbe essere stata rimossa/modificata).
        self.testo_completo = testo_completo if testo_completo else ""

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
            "probabilita": self.probabilita,
            "motivazione": self.motivazione,
            "testo_completo": self.testo_completo,
        }

    @classmethod
    def from_dict(cls, data):
        # _safe_str per "city": a differenza degli altri campi stringa qui sotto,
        # non passa per un ulteriore `if x else default` dentro __init__, quindi un
        # null esplicito persistito (round-trip JSON) sopravvivrebbe come None con
        # un semplice data.get(..., default) invece di essere normalizzato.
        return cls(
            data["title"], data["company"], data["portal"], data["link"],
            data.get("date", ""), data.get("snippet", ""),
            data.get("match_level", "Base"), data.get("match_count", 0),
            _safe_str(data, "city", ""), data.get("work_mode", "unverified"), data.get("fetch_status", "no_attempt"),
            data.get("probabilita", 0), data.get("motivazione", ""),
            data.get("testo_completo", ""),
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
    Aggiunge 2s di delay tra le richieste per evitare rate limiting.

    f_TPR=r604800 (ultima settimana, non più r86400/24h): verificato dal vivo che
    la finestra 24h perde annunci genuini più vecchi di un giorno — se un run di
    scraping salta un giorno (es. per il guard DST), quegli annunci non vengono
    mai più visti da nessun run futuro con la finestra stretta.
    f_WT=3 (filtro Hybrid) rimosso: verificato dal vivo che sull'endpoint
    anonimo/senza login non restringe in modo affidabile i risultati (a volte
    nessun effetto, a volte risultati diversi da una richiesta identica a pochi
    minuti di distanza) — il controllo reale sulla modalità di lavoro resta
    comunque il testo (detect_work_mode) applicato a valle su ogni annuncio.
    pageNum ora cicla su 2 pagine per keyword+città: verificato dal vivo che la
    pagina 1 aggiunge in media circa metà risultati validi in più rispetto alla
    sola pagina 0 (prima presa da sola).

    MAX_ETA_GIORNI: la finestra di ricerca resta larga (7gg) per non perdere
    annunci se un run di scraping salta, ma un annuncio più vecchio di questa
    soglia viene scartato comunque prima di arrivare in email — candidarsi
    entro i primi giorni dalla pubblicazione conta per entrare nel processo di
    selezione. Verificato dal vivo (campione reale, finestra 7gg): quasi metà
    dei titoli validi trovati avevano più di 3 giorni. La data usata è quella
    reale della card (<time class="job-search-card__listdate" datetime="...">
    per la Strategia 2, datePosted del JSON-LD per la Strategia 1) — LinkedIn
    non distingue pubblicamente un annuncio nuovo da uno ripubblicato/rinnovato,
    quindi questo filtra per età mostrata, non per "genuinamente nuovo".
    """
    MAX_PAGES = 2
    MAX_ETA_GIORNI = 3

    def __init__(self):
        super().__init__("LinkedIn")

    def scrape(self, city_name, city_config):
        import json as _json
        import time
        jobs = []

        keywords = SEARCH_KEYWORDS

        seen_links = set()

        # LinkedIn geolocalizza in modo errato le città italiane con l'italiano.
        # "Milan, Italy" / "Turin, Italy" / "Genoa, Italy" funzionano correttamente.
        linkedin_location = city_config.get("linkedin_location", f"{city_name}, Italy")

        for kw in keywords:
            for page_num in range(self.MAX_PAGES):
                try:
                    url = "https://www.linkedin.com/jobs/search/"
                    params = {
                        "keywords": kw,
                        "location": linkedin_location,
                        "f_TPR": "r604800",  # ultima settimana
                        "position": 1,
                        "pageNum": page_num,
                    }
                    headers = {
                        "User-Agent": USER_AGENT_CHROME,
                        "Accept-Language": "it-IT,it;q=0.9",
                    }
                    response = requests.get(url, params=params, headers=headers, timeout=12)
                    logging.info(f"{self.portal_name} ({kw} - {city_name}, pagina {page_num}): HTTP {response.status_code}")

                    if response.status_code != 200:
                        break

                    soup = BeautifulSoup(response.text, "html.parser")
                    jobs_before_strategia1 = len(jobs)
                    card_count = 0

                    # Strategia 1: JSON-LD JobPosting
                    for script in soup.find_all("script", type="application/ld+json"):
                        try:
                            data = _json.loads(script.string or "{}")
                            items = data if isinstance(data, list) else [data]
                        except Exception:
                            continue
                        # Try/except per singolo item: un JobPosting con un campo di
                        # tipo inatteso (es. title come lista invece di stringa, un
                        # quirk reale di alcuni CMS JSON-LD) non deve far scartare
                        # anche tutti gli altri JobPosting validi dello stesso blocco.
                        for item in items:
                            card_count += 1
                            try:
                                if item.get("@type") == "JobPosting":
                                    title = _safe_str(item, "title")
                                    if not is_valid_job_title(title):
                                        continue
                                    link = _safe_str(item, "url")
                                    if not link or link in seen_links:
                                        continue
                                    date = item.get("datePosted", "")
                                    eta = _eta_giorni_da_data(date)
                                    if eta is not None and eta > self.MAX_ETA_GIORNI:
                                        continue
                                    seen_links.add(link)
                                    company = (item.get("hiringOrganization") or {}).get("name", "")
                                    desc = _safe_str(item, "description")
                                    match_level, match_count, work_mode, fetch_status, probabilita, motivazione, testo_completo = calcola_punteggio_e_modalita(link, desc)
                                    jobs.append(ScrapedJob(title, company, self.portal_name, link,
                                                           date=date, match_level=match_level,
                                                           match_count=match_count, city=city_name,
                                                           work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione, testo_completo=testo_completo))
                            except Exception:
                                pass

                    # Strategia 2: card HTML standard LinkedIn (classe base-card)
                    # Solo se QUESTA pagina non ha trovato nulla via JSON-LD — non deve
                    # dipendere dall'accumulatore globale, altrimenti una pagina/keyword
                    # precedente che trova anche un solo risultato disattiva il fallback.
                    if len(jobs) == jobs_before_strategia1:
                        cards = soup.find_all("div", class_=lambda c: c and "base-card" in c)
                        card_count += len(cards)
                        # Try/except per singola card (come WyserScraper): un link_elem
                        # senza attributo href (es. contenuto lazy-loaded via JS) non deve
                        # scartare anche tutte le card successive della stessa keyword.
                        for card in cards:
                            try:
                                title_elem = card.find(class_=lambda c: c and "base-search-card__title" in (c or ""))
                                company_elem = card.find(class_=lambda c: c and "base-search-card__subtitle" in (c or ""))
                                link_elem = card.find("a", class_=lambda c: c and "base-card__full-link" in (c or ""))
                                if not title_elem or not link_elem:
                                    continue
                                title = title_elem.get_text(strip=True)
                                if not is_valid_job_title(title):
                                    continue
                                href = link_elem.get("href", "")
                                if not href:
                                    continue
                                link = href.split("?")[0]
                                if link in seen_links:
                                    continue
                                time_elem = card.find("time", class_=lambda c: c and "listdate" in (c or ""))
                                date = time_elem.get("datetime", "") if time_elem else ""
                                eta = _eta_giorni_da_data(date)
                                if eta is not None and eta > self.MAX_ETA_GIORNI:
                                    continue
                                seen_links.add(link)
                                company = company_elem.get_text(strip=True) if company_elem else ""
                                match_level, match_count, work_mode, fetch_status, probabilita, motivazione, testo_completo = calcola_punteggio_e_modalita(link, title)
                                jobs.append(ScrapedJob(title, company, self.portal_name, link, date=date,
                                                       match_level=match_level, match_count=match_count,
                                                       city=city_name, work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione, testo_completo=testo_completo))
                            except Exception as e:
                                logging.error(f"{self.portal_name}: card scartata per errore di parsing: {e}")

                    # Nessuna card affatto su questa pagina (non solo "nessun match
                    # valido"): è un segnale reale di fine risultati, si evita di
                    # richiedere anche la pagina successiva per questa keyword.
                    if card_count == 0:
                        time.sleep(2)
                        break

                    time.sleep(2)

                except Exception as e:
                    logging.error(f"Errore {self.portal_name} keyword '{kw}' pagina {page_num}: {e}")
                    break

        return jobs

class MichaelPageScraper(BaseScraper):
    """
    MichaelPage IT — gli URL per-città restituiscono 404.
    Usa 3 categorie nazionali e filtra per titolo. "sales-marketing"/"commercial"
    (usate in precedenza) sono URL morte: verificato via Archive.org che non
    hanno mai avuto uno snapshot valido, mentre "marketing"/"sales"/
    "digital-new-media" sono le categorie reali del menu del sito (200,
    snapshot recenti) — "sales" da sola ha ~192 annunci mai interrogati prima.
    Paginazione: Drupal Views standard con ?page=N (0-indexed, verificato dal
    link "Pagination" nell'HTML); prima si prendeva sempre e solo la prima
    pagina, perdendo l'84%+ dei risultati sulle categorie più popolate.
    """
    def __init__(self):
        super().__init__("MichaelPage")

    def scrape(self, city_name, city_config):
        jobs = []
        headers = {
            "User-Agent": USER_AGENT_CHROME,
            "Accept-Language": "it-IT,it;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        # Scrape solo per Genova per evitare duplicati (offerte italiane senza filtro città)
        if city_name != "Genova":
            return []
        urls = [
            "https://www.michaelpage.it/jobs/marketing",
            "https://www.michaelpage.it/jobs/sales",
            "https://www.michaelpage.it/jobs/digital-new-media",
        ]
        # Tiene i LINK già visti, non i titoli: due annunci distinti (aziende
        # diverse) possono condividere lo stesso titolo generico (es. "Sales
        # Manager"), sia sulla stessa pagina sia su categorie diverse — il link
        # è l'unico identificativo affidabile del singolo annuncio.
        seen = set()
        MAX_PAGES = 10
        for base_url in urls:
            for page in range(MAX_PAGES):
                url = base_url if page == 0 else f"{base_url}?page={page}"
                try:
                    response = requests.get(url, headers=headers, timeout=12)
                    logging.info(f"{self.portal_name}: HTTP {response.status_code} ({url})")
                    if response.status_code != 200:
                        if page == 0:
                            logging.error(f"{self.portal_name}: HTTP {response.status_code}")
                        break
                    soup = BeautifulSoup(response.text, "html.parser")
                    prima = len(seen)
                    jobs_json_ld = self._parse_json_ld(soup, url)
                    jobs.extend(j for j in jobs_json_ld if j.link not in seen)
                    seen.update(j.link for j in jobs_json_ld)
                    for a in soup.find_all("a", href=lambda h: h and "/job-detail/" in h):
                        title = a.get_text(strip=True)
                        href = a.get("href", "")
                        if not href:
                            continue
                        link = href if href.startswith("http") else "https://www.michaelpage.it" + href
                        if title and title != "Candidati" and link not in seen:
                            seen.add(link)
                            if is_valid_job_title(title):
                                match_level, match_count, work_mode, fetch_status, probabilita, motivazione, testo_completo = calcola_punteggio_e_modalita(link, "")
                                jobs.append(ScrapedJob(title, "", self.portal_name, link,
                                                       match_level=match_level, match_count=match_count,
                                                       city="Italia", work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione, testo_completo=testo_completo))
                    # Nessun link nuovo su questa pagina: oltre l'ultima pagina reale
                    # Drupal ripropone contenuto già visto invece di un 404 pulito.
                    if len(seen) == prima:
                        break
                except requests.exceptions.Timeout:
                    logging.error(f"{self.portal_name}: timeout della richiesta ({url})")
                    break
                except Exception as e:
                    logging.error(f"Errore scraping {self.portal_name}: {e}")
                    break
        if not jobs:
            logging.info(f"{self.portal_name}: 0 offerte valide trovate dopo i filtri.")
        return jobs

    def _parse_json_ld(self, soup, base_url):
        import json
        jobs = []
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
                items = data if isinstance(data, list) else [data]
            except Exception:
                continue
            # Try/except per singolo item: un JobPosting con un campo di tipo
            # inatteso non deve far scartare anche gli altri item validi dello
            # stesso blocco (vedi stesso fix in LinkedInScraper).
            for item in items:
                try:
                    if item.get("@type") == "JobPosting":
                        title = _safe_str(item, "title")
                        if is_valid_job_title(title):
                            link = _safe_str(item, "url", base_url)
                            company = (item.get("hiringOrganization") or {}).get("name", "")
                            date = item.get("datePosted", "")
                            desc = _safe_str(item, "description")
                            match_level, match_count, work_mode, fetch_status, probabilita, motivazione, testo_completo = calcola_punteggio_e_modalita(link, desc)
                            # city="Italia" (non city_name, sempre "Genova" nella pratica
                            # dato il guard sopra): coerente col percorso di fallback HTML
                            # qui sotto, che etichetta "Italia" per lo stesso tipo di
                            # contenuto nazionale non filtrato per città.
                            jobs.append(ScrapedJob(title, company, self.portal_name, link,
                                                   date=date, match_level=match_level, match_count=match_count, city="Italia", work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione, testo_completo=testo_completo))
                except Exception:
                    pass
        return jobs


class GiGroupScraper(BaseScraper):
    """
    GiGroup — La pagina è SSR (WordPress) con dati job embedded nel tag a[data-job].
    Il filtro città via querystring non funziona: gira solo durante l'iterazione
    Genova e ogni offerta viene etichettata city="Italia" a prescindere dal campo
    dj["province"] presente nel JSON (vedi commento più sotto sul perché non lo si
    usa: eviterebbe di applicare erroneamente la policy work-mode di un'altra città).
    Il parametro di ricerca per titolo è "job" (verificato 2026-07-15 tramite il
    <form>: <input name="job" placeholder="POSIZIONE">) — "q" (usato in precedenza)
    non ha alcun effetto sul risultato server-side, viene ignorato silenziosamente
    e restituisce sempre lo stesso listato generico non filtrato.
    Aggiunge 2s di delay tra le 18 keyword per evitare rate limiting (osservato
    dal vivo il 2026-07-15: 11/18 richieste consecutive senza delay sono andate
    in timeout dopo uso intensivo del sito nella stessa sessione).
    """
    def __init__(self):
        super().__init__("GiGroup")

    def scrape(self, city_name, city_config):
        import json as _json
        import urllib.parse as _urlparse
        import time
        jobs = []
        search_keywords = SEARCH_KEYWORDS
        headers = {
            "User-Agent": USER_AGENT_CHROME,
            "Accept-Language": "it-IT,it;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        # Scrape solo per Genova per evitare 3x chiamate identiche (no filtro città)
        if city_name != "Genova":
            return []
        seen = set()
        for kw in search_keywords:
          url = f"https://www.gigroup.it/offerte-lavoro/?job={_urlparse.quote_plus(kw)}"
          try:
            response = requests.get(url, headers=headers, timeout=12)
            logging.info(f"{self.portal_name} ({kw}): HTTP {response.status_code}")
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
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
                        # Etichettato "Italia" come MichaelPage/IQMSelezione, non con
                        # dj["province"]: questo scraper gira solo durante l'iterazione
                        # Genova e viene filtrato con la policy (lenient) di quella città,
                        # quindi una città reale nel campo "city" farebbe apparire l'annuncio
                        # nella sezione email di un'altra città senza aver mai applicato
                        # la sua policy work-mode (es. "solo ibrido" per Milano/Torino).
                        job_city = "Italia"
                        match_level, match_count, work_mode, fetch_status, probabilita, motivazione, testo_completo = calcola_punteggio_e_modalita(link, "")
                        jobs.append(ScrapedJob(title, "GiGroup", self.portal_name, link,
                                               match_level=match_level, match_count=match_count,
                                               city=job_city, work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione, testo_completo=testo_completo))
                    except Exception:
                        pass
            else:
                logging.error(f"{self.portal_name} ({kw}): HTTP {response.status_code}")
          except requests.exceptions.Timeout:
              logging.error(f"{self.portal_name} ({kw}): timeout della richiesta")
          except Exception as e:
              logging.error(f"Errore scraping {self.portal_name} ({kw}): {e}")
          # 4s, non più 2s: SEARCH_KEYWORDS è cresciuta da 18 (quando il delay di
          # 2s fu validato il 2026-07-15) a 24 keyword, e il 21/07 si sono
          # osservati timeout ricorrenti su più run nella stessa giornata,
          # concentrati verso la seconda metà della sequenza di keyword — lo
          # stesso pattern di rallentamento a uso intensivo già documentato
          # sopra, solo con più richieste in sequenza a innescarlo prima.
          time.sleep(4)
        if not jobs:
            logging.info(f"{self.portal_name}: 0 offerte valide trovate dopo i filtri.")
        return jobs


class WyserScraper(BaseScraper):
    """
    Wyser — WordPress SSR. Ogni card è article.card-job con:
    - p.card-title (titolo) dentro a.dettaglio (link)
    - li.posto (città)
    Niente più wy_position=MARKETING: verificato dal vivo che è un campo di
    ricerca testuale libera sul titolo, non un filtro di categoria — escludeva
    strutturalmente qualunque titolo target che non contenesse letteralmente
    "marketing" (es. "Digital Sales Manager", "Head of Growth"). Il filtro
    reale resta is_valid_job_title() sui risultati non filtrati.
    Paginazione: il sito pagina a 15 risultati/pagina via ?pages=N (verificato
    dal vivo, senza questo si perdevano sistematicamente i risultati oltre la
    prima pagina); si segue finché una pagina non risponde più 200 con card.
    Torino non ha uno slug città sul sito (vedi CITIES): city_config non ha
    "wyser_slug", quindi si scarica la pagina nazionale e si tengono solo le
    card il cui campo città (li.posto) contiene il nome della città richiesta.
    """
    def __init__(self):
        super().__init__("Wyser")

    def scrape(self, city_name, city_config):
        jobs = []
        wyser_slug = city_config.get("wyser_slug")
        base_url = (f"https://it.wyser-search.com/offerte-lavoro/{wyser_slug}/"
                    if wyser_slug else "https://it.wyser-search.com/offerte-lavoro/")
        headers = {
            "User-Agent": USER_AGENT_CHROME,
            "Accept-Language": "it-IT,it;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        seen_links = set()
        MAX_PAGES = 10
        for page in range(1, MAX_PAGES + 1):
            url = base_url if page == 1 else f"{base_url}?pages={page}"
            try:
                response = requests.get(url, headers=headers, timeout=25)
                logging.info(f"{self.portal_name} (pagina {page}): HTTP {response.status_code}")
                if response.status_code != 200:
                    break
                soup = BeautifulSoup(response.text, "html.parser")
                cards = soup.find_all("article", class_=lambda c: c and "card-job" in c)
                if not cards:
                    break
                for card in cards:
                    # Try/except per singola card: senza questo, un'eccezione su una
                    # card malformata (es. href mancante) troncava silenziosamente
                    # tutte le card successive sulla pagina, non solo quella incriminata.
                    try:
                        link_elem = card.find("a", class_="dettaglio")
                        title_elem = card.find("p", class_=lambda c: c and "card-title" in (c or ""))
                        date_elem = card.find("p", class_=lambda c: c and "size-16" in (c or "") and "blue" in (c or ""))
                        posto_elem = card.find("li", class_=lambda c: c and "posto" in (c or ""))
                        if not link_elem or not title_elem:
                            continue
                        posto = posto_elem.get_text(strip=True) if posto_elem else ""
                        if wyser_slug is None and city_name.lower() not in posto.lower():
                            continue
                        title = title_elem.get_text(strip=True)
                        if not is_valid_job_title(title):
                            continue
                        link = link_elem.get("href", "")
                        if not link:
                            continue
                        if not link.startswith("http"):
                            link = "https://it.wyser-search.com" + link
                        if link in seen_links:
                            continue
                        seen_links.add(link)
                        date = date_elem.get_text(strip=True) if date_elem else ""
                        match_level, match_count, work_mode, fetch_status, probabilita, motivazione, testo_completo = calcola_punteggio_e_modalita(link, "")
                        jobs.append(ScrapedJob(title, "", self.portal_name, link, date=date,
                                               match_level=match_level, match_count=match_count,
                                               city=city_name, work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione, testo_completo=testo_completo))
                    except Exception as e:
                        logging.error(f"{self.portal_name}: card scartata per errore di parsing: {e}")
            except requests.exceptions.Timeout:
                logging.error(f"{self.portal_name}: timeout della richiesta (pagina {page})")
                break
            except Exception as e:
                logging.error(f"Errore scraping {self.portal_name} (pagina {page}): {e}")
                break
        if not jobs:
            logging.info(f"{self.portal_name}: 0 offerte valide trovate dopo i filtri.")
        return jobs



class PagePersonnelScraper(BaseScraper):
    """Page Personnel IT — RITIRATO. Verificato dal vivo (curl diretto, curl_cffi
    impersonate-Chrome, WebFetch da IP diverso) che pagepersonnel.it fa redirect
    301 permanente su OGNI URL categoria verso michaelpage.it (il brand è stato
    assorbito): /jobs/marketing/{città} redirige sempre a michaelpage.it/jobs/marketing
    (già coperta da MichaelPageScraper), le altre categorie a una ricerca generica
    senza filtro. requests segue il redirect quindi lo status finale non è mai 404,
    e il vecchio controllo "404 -> fallback nazionale" non scattava mai: risultato,
    ogni offerta veniva etichettata con la città richiesta anche se il contenuto
    era in realtà quello nazionale non filtrato (mislabeling sistematico).
    Dato che il sito non ha più contenuto proprio, questo scraper rileva il
    redirect fuori dominio e si ferma, invece di produrre dati duplicati e
    mal etichettati che MichaelPageScraper copre già correttamente."""
    def __init__(self):
        super().__init__("PagePersonnel")

    def scrape(self, city_name, city_config):
        headers = {
            "User-Agent": USER_AGENT_CHROME,
            "Accept-Language": "it-IT,it;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        url = f"https://www.pagepersonnel.it/jobs/marketing/{city_name.lower()}"
        try:
            response = requests.get(url, headers=headers, timeout=12)
            final_host = urllib.parse.urlparse(response.url).hostname or ""
            if "pagepersonnel.it" not in final_host:
                logging.info(f"{self.portal_name}: {url} reindirizza fuori dominio ({response.url}) — "
                              f"il sito ha assorbito il brand, contenuto già coperto da MichaelPageScraper. Salto.")
                return []
            logging.info(f"{self.portal_name}: HTTP {response.status_code}")
            if response.status_code != 200:
                logging.error(f"{self.portal_name}: HTTP {response.status_code}")
                return []
            # Il sito non reindirizza (ha ancora contenuto proprio, scenario non
            # osservato negli ultimi test ma gestito per non perdere dati se
            # dovesse ripristinarsi): stesso parsing JSON-LD/fallback link usato
            # da MichaelPageScraper.
            jobs = []
            soup = BeautifulSoup(response.text, "html.parser")
            seen = set()
            import json as _json
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = _json.loads(script.string or "{}")
                    items = data if isinstance(data, list) else [data]
                except Exception:
                    continue
                for item in items:
                    try:
                        if item.get("@type") == "JobPosting":
                            title = _safe_str(item, "title")
                            if is_valid_job_title(title):
                                link = _safe_str(item, "url", url)
                                if link in seen:
                                    continue
                                seen.add(link)
                                company = (item.get("hiringOrganization") or {}).get("name", "")
                                date = item.get("datePosted", "")
                                desc = _safe_str(item, "description")
                                match_level, match_count, work_mode, fetch_status, probabilita, motivazione, testo_completo = calcola_punteggio_e_modalita(link, desc)
                                jobs.append(ScrapedJob(title, company, self.portal_name, link,
                                                       date=date, match_level=match_level, match_count=match_count, city=city_name, work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione, testo_completo=testo_completo))
                    except Exception:
                        pass
            if not jobs:
                for a in soup.find_all("a", href=lambda h: h and "/job-detail/" in h):
                    title = a.get_text(strip=True)
                    href = a.get("href", "")
                    if not href:
                        continue
                    link = href if href.startswith("http") else "https://www.pagepersonnel.it" + href
                    if title and title != "Candidati" and link not in seen:
                        seen.add(link)
                        if is_valid_job_title(title):
                            match_level, match_count, work_mode, fetch_status, probabilita, motivazione, testo_completo = calcola_punteggio_e_modalita(link, "")
                            jobs.append(ScrapedJob(title, "", self.portal_name, link,
                                                   match_level=match_level, match_count=match_count, city=city_name, work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione, testo_completo=testo_completo))
            if not jobs:
                logging.info(f"{self.portal_name}: 0 offerte valide trovate dopo i filtri.")
            return jobs
        except requests.exceptions.Timeout:
            logging.error(f"{self.portal_name}: timeout della richiesta")
            return []
        except Exception as e:
            logging.error(f"Errore scraping {self.portal_name}: {e}")
            return []


class ManpowerScraper(BaseScraper):
    """
    Manpower IT — SSR con link /it/annuncio-lavoro/ e titoli h2.
    Verificato 2026-07-15: il sito è stato ristrutturato, h2 e <a> non sono più
    in relazione antenato/discendente (h2.find_parent("a") non trova più nulla)
    — ora entrambi sono figli diretti dello stesso contenitore div.job-position.
    """
    def __init__(self):
        super().__init__("Manpower")

    def scrape(self, city_name, city_config):
        jobs = []
        url = f"https://www.manpower.it/it/trova-lavoro/citta/{city_name.lower()}"
        headers = {
            "User-Agent": USER_AGENT_CHROME,
            "Accept-Language": "it-IT,it;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        try:
            response = requests.get(url, headers=headers, timeout=12)
            logging.info(f"{self.portal_name}: HTTP {response.status_code}")
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                
                # Manpower: ogni annuncio è un div.job-position con h2 (titolo) e
                # a[href*=annuncio-lavoro] come figli diretti (non annidati tra loro).
                for card in soup.find_all("div", class_=lambda c: c and "job-position" in c):
                    h2 = card.find("h2")
                    a = card.find("a", href=lambda h: h and "/annuncio-lavoro/" in h)
                    if h2 and a:
                        title = h2.get_text(strip=True)
                        if is_valid_job_title(title):
                            link = a["href"]
                            if not link.startswith("http"):
                                link = "https://www.manpower.it" + link
                            match_level, match_count, work_mode, fetch_status, probabilita, motivazione, testo_completo = calcola_punteggio_e_modalita(link, "")
                            jobs.append(ScrapedJob(title, "", self.portal_name, link,
                                                   match_level=match_level, match_count=match_count, city=city_name, work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione, testo_completo=testo_completo))
                
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
    """
    IQM Selezione — SSR PHP. Parsing dei link a dettaglio.php?annuncio=XXX nella
    pagina posizioni aperte (unica pagina nazionale, non filtrata per città).
    Verificato 2026-07-15: i titoli non contengono mai il nome della città
    (es. "Docente Logistica e Magazzino", nessun riferimento geografico), quindi
    il vecchio filtro `city_name.lower() in title.lower()` scartava sempre
    tutto — scarica una sola volta (Genova, come MichaelPage/GiGroup) ed
    etichetta le offerte come "Italia".
    """
    def __init__(self):
        super().__init__("IQMSelezione")

    def scrape(self, city_name, city_config):
        jobs = []
        # Pagina unica nazionale: scarica una sola volta per evitare 3x chiamate identiche
        if city_name != "Genova":
            return []
        url = "https://www.iqmselezione.it/posizioni-aperte-in-iqmselezione.php"
        headers = {
            "User-Agent": USER_AGENT_CHROME,
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
                    if title and is_valid_job_title(title):
                        link = a["href"]
                        if not link.startswith("http"):
                            link = "https://www.iqmselezione.it/" + link
                        match_level, match_count, work_mode, fetch_status, probabilita, motivazione, testo_completo = calcola_punteggio_e_modalita(link, "")
                        jobs.append(ScrapedJob(title, "", self.portal_name, link,
                                               match_level=match_level, match_count=match_count, city="Italia", work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione, testo_completo=testo_completo))
                
                if not jobs:
                    logging.info(f"{self.portal_name}: 0 offerte valide trovate dopo i filtri.")
            else:
                logging.error(f"{self.portal_name}: HTTP {response.status_code}")
        except Exception as e:
            logging.error(f"Errore scraping {self.portal_name}: {e}")
        return jobs


class PraxiScraper(BaseScraper):
    """PRAXI Recruitment — SSR .NET. Una sola richiesta con ?AnnunciPerPagina=999
    restituisce TUTTI gli annunci del sito in una pagina sola (verificato dal
    vivo: il contatore dichiarato dal sito, "76 annunci", coincide esattamente
    con le card estratte — nessuna paginazione necessaria).
    Ogni card (div.annuncioSingolo) espone titolo, link, data di pubblicazione
    reale (formato gg/mm/aaaa) e sede (es. "Genova", "MILANO", "Provincia di
    Milano Nord-est") — a differenza di IQMSelezione/GiGroup, qui la città
    reale è disponibile per ogni singolo annuncio, non solo a livello di sito.
    Gira una sola volta (durante l'iterazione Genova, come MichaelPage/GiGroup/
    IQMSelezione/LHH) invece che 3 volte sullo stesso identico set nazionale;
    la città di ogni offerta si determina cercando il nome di una delle 3
    città target come sottostringa case-insensitive nel campo sede (stesso
    approccio usato per il fallback nazionale di WyserScraper), etichettando
    "Italia" se nessuna delle 3 compare — così la policy work-mode di Genova
    (la più permissiva) si applica solo alle offerte davvero non attribuibili
    a una città specifica, non a quelle di un'altra città italiana qualsiasi.
    """
    def __init__(self):
        super().__init__("PRAXI")

    def scrape(self, city_name, city_config):
        jobs = []
        if city_name != "Genova":
            return []
        url = "https://recruitment.praxi/RicercheAperte/Ricerca?AnnunciPerPagina=999"
        headers = {
            "User-Agent": USER_AGENT_CHROME,
            "Accept-Language": "it-IT,it;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        citta_lookup = {c.lower(): c for c in CITIES.keys()}
        seen = set()
        try:
            response = requests.get(url, headers=headers, timeout=20)
            logging.info(f"{self.portal_name}: HTTP {response.status_code}")
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                for card in soup.find_all("div", class_="annuncioSingolo"):
                    try:
                        titolo_div = card.find("div", class_="titolo")
                        a = titolo_div.find("a") if titolo_div else None
                        if not a:
                            continue
                        title = a.get_text(strip=True)
                        if not title or not is_valid_job_title(title):
                            continue
                        href = a.get("href", "")
                        if not href:
                            continue
                        link = href if href.startswith("http") else "https://recruitment.praxi" + href
                        if link in seen:
                            continue
                        seen.add(link)

                        sede_elem = card.find("div", class_="sede")
                        sede = sede_elem.get_text(strip=True) if sede_elem else ""
                        sede_lower = sede.lower()
                        job_city = "Italia"
                        for nome_lower, nome in citta_lookup.items():
                            if nome_lower in sede_lower:
                                job_city = nome
                                break

                        date = ""
                        for span in card.find_all("span", class_="fs20"):
                            if span.contents and "Data pubblicazione" in str(span.contents[0]):
                                strong = span.find("strong")
                                date = strong.get_text(strip=True) if strong else ""
                                break

                        anteprima_elem = card.find("div", class_="anteprima")
                        snippet = anteprima_elem.get_text(strip=True) if anteprima_elem else ""

                        match_level, match_count, work_mode, fetch_status, probabilita, motivazione, testo_completo = calcola_punteggio_e_modalita(link, snippet)
                        jobs.append(ScrapedJob(title, "", self.portal_name, link, date=date,
                                               snippet=snippet[:150] + "..." if snippet else "",
                                               match_level=match_level, match_count=match_count,
                                               city=job_city, work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione, testo_completo=testo_completo))
                    except Exception as e:
                        logging.error(f"{self.portal_name}: annuncio scartato per errore di parsing: {e}")
                if not jobs:
                    logging.info(f"{self.portal_name}: 0 offerte valide trovate dopo i filtri.")
            else:
                logging.error(f"{self.portal_name}: HTTP {response.status_code}")
        except requests.exceptions.Timeout:
            logging.error(f"{self.portal_name}: timeout della richiesta")
        except Exception as e:
            logging.error(f"Errore scraping {self.portal_name}: {e}")
        return jobs


class LhhScraper(BaseScraper):
    """LHH (Lee Hecht Harrison) — API nascosta POST /api/data/jobs/summarized.
    Il filtro server jobLocation+radius NON funziona: verificato dal vivo che
    pagination.total e il pool di annunci restituiti sono identici a parità di
    keyword qualunque sia la città richiesta, e i cityName reali dei job sono
    sparsi per tutta Italia (Verona, Napoli, Bari...) a prescindere dalla città
    cercata. Per questo lo scraping gira una sola volta (durante l'iterazione
    Genova, come MichaelPage/GiGroup/IQMSelezione) invece che 3 volte con lo
    stesso identico pool nazionale, e la città di ogni offerta si determina dal
    campo cityName della risposta invece che dal parametro di ricerca: se
    corrisponde a Genova/Milano/Torino viene etichettata di conseguenza,
    altrimenti "Italia" (stessa convenzione già usata per le altre offerte
    nazionali di questo file, filtrate con la policy lenient di Genova).
    Paginazione: la risposta espone pagination.total (risultati reali per la
    query); prima si prendeva sempre e solo range=0 (i primi ~10), perdendo
    fino al 90%+ dei risultati per keyword popolari. Ora si avanza range del
    numero di job realmente restituiti a ogni chiamata, fino al totale reale
    o a un tetto di sicurezza.
    """
    MAX_JOBS_PER_KEYWORD = 200  # tetto di sicurezza sulla paginazione

    def __init__(self):
        super().__init__("LHH")

    def scrape(self, city_name, city_config):
        import urllib.parse as _urlparse
        jobs = []
        if city_name != "Genova":
            return []
        keywords = SEARCH_KEYWORDS
        citta_lookup = {c.lower(): c for c in CITIES.keys()}

        url = "https://www.lhh.com/api/data/jobs/summarized"
        headers = {
            "User-Agent": USER_AGENT_CHROME,
            "Content-Type": "application/json",
            "Origin": "https://www.lhh.com",
            "Referer": "https://www.lhh.com/it-it/cerca-lavoro"
        }
        seen_links = set()

        for kw in keywords:
            range_offset = 0
            while range_offset < self.MAX_JOBS_PER_KEYWORD:
                # queryString è un pseudo-querystring "&key=value&key=value" fatto a mano:
                # kw va URL-encodato (quote_plus) perché una keyword con "&" letterale
                # (es. "Responsabile Marketing & Sales") spezzerebbe il parsing lato server.
                # jobLocation/radius restano "Italia"/molto ampio: il filtro server è
                # comunque inefficace (vedi docstring), meglio non fingere una precisione
                # geografica che l'API non fornisce davvero.
                payload = {
                    "queryString": f"&q={_urlparse.quote_plus(kw)}&jobLocation=Italia&radius=1000&sort=PostedDate desc",
                    "filtersToDisplay": "{AEEBD4FE-DCF4-4D9B-8895-6EE4C1C31F95}|{9D842325-FA99-45EE-9197-AC1749D579DF}|{F4AA5EF6-7E6B-4BBA-B1E3-38E840537688}|{A5D28A27-7525-4F9C-813F-53E1B58D955F}|{366A4861-5C5C-4C12-9776-8CE4789960E0}|{26CA3CFC-0C11-4919-883F-2C8DB522BADC}",
                    "range": range_offset,
                    "siteName": "lhh",
                    "brand": "lhh",
                    "countryCode": "IT",
                    "languageCode": "it-IT"
                }

                try:
                    response = curl_requests.post(url, json=payload, headers=headers, impersonate="chrome124", timeout=15)
                    logging.info(f"{self.portal_name} ({kw}, range={range_offset}): HTTP {response.status_code}")

                    if response.status_code != 200:
                        logging.error(f"{self.portal_name}: Errore API HTTP {response.status_code}")
                        break

                    data = response.json()
                    jobs_data = data.get("jobs", [])
                    total = (data.get("pagination") or {}).get("total", len(jobs_data))
                    logging.info(f"{self.portal_name} ({kw}): {len(jobs_data)}/{total} offerte dalla API a range={range_offset}")
                    if not jobs_data:
                        break

                    for job in jobs_data:
                        try:
                            title = _safe_str(job, "jobTitle")
                            if not is_valid_job_title(title):
                                continue
                            company = job.get("brandName", "LHH")
                            job_id_lhh = job.get("jobId")
                            if not job.get("applyUri") and not job_id_lhh:
                                # Senza applyUri né jobId non c'è nulla che identifichi
                                # univocamente l'annuncio: costruire un link con "id=None"
                                # farebbe collassare ogni offerta simile sullo stesso job_id.
                                continue
                            link = job.get("applyUri") or f"https://www.lhh.com/it-it/cerca-lavoro/job-description/?id={job_id_lhh}"
                            if link in seen_links:
                                continue
                            seen_links.add(link)
                            citta_reale = citta_lookup.get(_safe_str(job, "cityName").lower().strip(), "Italia")
                            date = job.get("postedDate", "")
                            desc = job.get("description", "") or ""
                            match_level, match_count, work_mode, fetch_status, probabilita, motivazione, testo_completo = calcola_punteggio_e_modalita(link, desc)
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
                                city=citta_reale,
                                work_mode=work_mode,
                                fetch_status=fetch_status,
                                probabilita=probabilita,
                                motivazione=motivazione, testo_completo=testo_completo,
                            ))
                        except Exception as e:
                            logging.error(f"{self.portal_name}: offerta scartata per errore di parsing: {e}")

                    range_offset += len(jobs_data)
                    if range_offset >= total:
                        break
                except Exception as e:
                    logging.error(f"Errore scraping {self.portal_name} per '{kw}' (range={range_offset}): {e}")
                    break

                # Tra pagine della stessa keyword: delay più breve di quello tra
                # keyword diverse, per non allungare troppo il run totale.
                time.sleep(1)

            # Come GiGroup (vedi commento nel suo scraper): richieste consecutive
            # senza delay sono andate in timeout dopo uso intensivo. Stesso pattern
            # di carico (18 keyword, ora anche con più pagine ciascuna).
            time.sleep(2)

        return jobs



# ==========================================
# LOGICA EMAIL
# ==========================================
def re_sub_nome_file(testo: str) -> str:
    """Riduce un nome azienda a uno slug sicuro per un nome file allegato."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", (testo or "azienda").strip()).strip("_")
    return slug[:40] if slug else "azienda"

def invia_email(nuove_offerte):
    if not GMAIL_USER or not GMAIL_APP_PASSWORD or not DESTINATION_EMAIL:
        logging.error("Credenziali email mancanti. Controlla il file .env")
        return False

    data_oggi = datetime.now().strftime("%d/%m/%Y")
    
    msg = MIMEMultipart()
    msg['From'] = GMAIL_USER
    msg['To'] = DESTINATION_EMAIL
    msg['Subject'] = f"[Job Alert] Nuove offerte Multi-City - {data_oggi}"
    
    body = ""
    allegati_cv = []  # lista di dict {"docx_path": ..., "nome_file": ...} da allegare dopo il body
    # Ogni CV personalizzato può richiedere fino a due chiamate LLM sequenziali
    # (proposta + verifica) di diversi minuti ciascuna: con più offerte >=80% lo
    # stesso giorno il tempo si somma senza limite. Questo budget evita che l'invio
    # email si blocchi per troppo tempo — oltre la soglia, le offerte restanti
    # vengono comunque incluse nell'email ma senza CV personalizzato allegato.
    cv_personalizzazione_scadenza = time.monotonic() + CV_PERSONALIZZAZIONE_BUDGET_SECONDI
    cv_budget_esaurito_loggato = False
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
            # int(...) con fallback a 0 invece di affidarsi al tipo grezzo: un record
            # malformato/legacy con probabilita non numerica (round-trip JSON) non deve
            # far crashare con TypeError l'ordinamento e quindi l'intero invio email.
            def _prob_ordinabile(job):
                try:
                    return int(job.probabilita)
                except (TypeError, ValueError):
                    return 0
            offerte_citta.sort(key=_prob_ordinabile, reverse=True)
            body += f"===============================\n"
            body += f"📍 {citta.upper()} ({len(offerte_citta)} offerte)\n"
            body += f"===============================\n\n"

            for i, job in enumerate(offerte_citta, 1):
                # Stessa conversione sicura della sort key sopra: job.probabilita non
                # protetto qui bloccherebbe con TypeError l'intero invio email su un
                # record legacy/corrotto con probabilita non numerica.
                prob = _prob_ordinabile(job)
                if prob >= 75:
                    prob_label = "🟢 ALTA"
                elif prob >= 50:
                    prob_label = "🟡 MEDIA"
                else:
                    prob_label = "🔴 BASSA"
                body += f"{i}. {job.title}\n"
                body += f"   Azienda: {job.company}\n"
                body += f"   Città: {job.city}\n"
                modalita_display = "Modalità non specificata nell'annuncio" if job.work_mode == "unverified" else job.work_mode.upper()
                body += f"   Modalità: {modalita_display}\n"
                body += f"   Portale: {job.portal}\n"
                body += f"   Probabilità richiamata: {prob}% — {prob_label}\n"
                body += f"   → {job.motivazione}\n"
                body += f"   Match CV: {job.match_level} ({job.match_count} keyword)\n"
                body += f"   Data: {job.date}\n"
                body += f"   Link: {job.link}\n"
                if job.snippet:
                    body += f"   Snippet: {job.snippet}\n"

                if CV_PERSONALIZZAZIONE_DISPONIBILE and prob >= SOGLIA_CV_PERSONALIZZATO and time.monotonic() >= cv_personalizzazione_scadenza:
                    if not cv_budget_esaurito_loggato:
                        logging.warning(
                            f"Budget di tempo per la personalizzazione CV esaurito "
                            f"({CV_PERSONALIZZAZIONE_BUDGET_SECONDI}s): le offerte >= 80% restanti "
                            f"vengono incluse nell'email senza CV personalizzato allegato."
                        )
                        cv_budget_esaurito_loggato = True
                elif CV_PERSONALIZZAZIONE_DISPONIBILE and prob >= SOGLIA_CV_PERSONALIZZATO:
                    try:
                        risultato_cv = genera_cv_per_offerta(job.title, job.link, job_city=job.city, job_text=job.testo_completo)
                    except Exception as e:
                        logging.error(f"Errore imprevisto personalizzazione CV per '{job.title}': {e}")
                        risultato_cv = None
                    docx_path = risultato_cv.get("docx_path") if risultato_cv else None
                    if risultato_cv and docx_path:
                        indice_allegato = len(allegati_cv) + 1
                        nome_file = f"CV_Ghigliotti_{indice_allegato}_{re_sub_nome_file(job.company)}.docx"
                        body += f"   📎 CV personalizzato allegato in Word (allegato {indice_allegato}) — apri in Word ed esporta in PDF prima di candidarti. Modifiche:\n"
                        for riga in risultato_cv.get("riepilogo", []):
                            body += f"      - {riga}\n"
                        allegati_cv.append({"docx_path": docx_path, "nome_file": nome_file})
                    elif risultato_cv:
                        # risultato_cv presente ma senza docx_path: forma inattesa, non deve
                        # mai far crashare invia_email (perderebbe l'intera email del giorno).
                        logging.error(f"genera_cv_per_offerta ha ritornato una forma inattesa per '{job.title}': {risultato_cv!r}")
                body += "\n"

    body += f"Totale offerte: {len(nuove_offerte)}.\n\n"
    
    # --- SEZIONE COMPANY PROSPECTOR ---
    # Il file viene solo letto qui, MAI svuotato: se l'invio fallisse dopo aver
    # già svuotato il file, i prospect andrebbero persi senza che siano mai stati
    # recapitati. Lo svuotamento avviene solo dopo un invio SMTP riuscito, più sotto.
    prospects_file = "daily_prospects.json"
    prospects_da_svuotare = False
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
                prospects_da_svuotare = True
        except Exception as e:
            logging.error(f"Errore caricamento prospect: {e}")

    msg.attach(MIMEText(body, 'plain', 'utf-8'))

    for allegato in allegati_cv:
        try:
            with open(allegato["docx_path"], "rb") as f:
                parte = MIMEApplication(f.read(), _subtype="vnd.openxmlformats-officedocument.wordprocessingml.document")
            parte.add_header("Content-Disposition", "attachment", filename=allegato["nome_file"])
            msg.attach(parte)
        except Exception as e:
            logging.error(f"Errore allegato CV personalizzato ({allegato['nome_file']}): {e}")
        finally:
            # I file sono già letti in memoria dentro msg: la cartella temporanea
            # con il CV personalizzato può essere ripulita subito, a prescindere
            # dall'esito dell'invio.
            try:
                cartella = os.path.dirname(allegato["docx_path"])
                if cartella and os.path.basename(cartella).startswith("cv_personalizzato_"):
                    shutil.rmtree(cartella, ignore_errors=True)
            except Exception:
                pass

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
            if prospects_da_svuotare:
                try:
                    _atomic_write_json(prospects_file, [])
                except Exception as e:
                    logging.error(f"Errore svuotamento {prospects_file} dopo invio riuscito: {e}")
            return True
        except Exception as e:
            import traceback
            logging.warning(f"Tentativo {attempt+1}/{len(retry_delays)} invio email fallito: {e}\n{traceback.format_exc()}")
            if attempt < len(retry_delays) - 1:
                time.sleep(delay)

    # Non si salva un file "pending" separato: offerte_giornaliere.json non viene
    # svuotato dal chiamante in caso di fallimento (vedi invia_email_job), quindi
    # queste stesse offerte restano committate e verranno ritentate al prossimo
    # invio pianificato. Un pending_scraper_email.json separato sarebbe comunque
    # inutilizzabile in CI: è gitignored e ogni runner GitHub Actions è effimero,
    # quindi non sopravvivrebbe mai da un run all'altro.
    logging.error("Impossibile inviare l'email dopo tutti i tentativi. Le offerte restano in offerte_giornaliere.json per il prossimo tentativo.")
    return False

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
            # Genova: in sede o ibrido, mai da remoto (unverified solo se INCLUDE_UNVERIFIED,
            # come per Milano/Torino — la vecchia condizione "!= da remoto" includeva
            # unverified incondizionatamente, ignorando il flag)
            if job.work_mode in ("in sede", "ibrido"):
                offerte_filtrate.append(job)
            elif job.work_mode == "unverified" and INCLUDE_UNVERIFIED:
                offerte_filtrate.append(job)
    return offerte_filtrate

def get_job_id(link: str) -> str:
    """Restituisce un ID univoco per l'offerta basato sul link.
    Per IQMSelezione (che usa annuncio come parametro query, es.
    dettaglio.php?annuncio=123) si estrae il parametro specifico che
    identifica l'annuncio — per questo portale il path da solo è identico
    per TUTTI gli annunci, quindi rimuovere la query string con
    .split("?")[0] collasserebbe ogni annuncio sullo stesso id e ne
    lascerebbe passare solo il primo mai visto.
    Per gli altri portali rimuove semplicemente i parametri query per evitare duplicati da tracking.
    """
    if "iqmselezione.it" in link and "annuncio=" in link:
        import urllib.parse as urlparse
        try:
            parsed = urlparse.urlparse(link)
            params = urlparse.parse_qs(parsed.query)
            annuncio_id = params.get("annuncio", [""])[0]
            if annuncio_id:
                return f"iqmselezione_{annuncio_id}"
        except Exception:
            pass
    if "lhh.com" in link and "id=" in link:
        # Il fallback di LhhScraper (quando applyUri manca) costruisce un link con
        # ?id={jobId}: senza questo caso speciale, .split("?")[0] rimuove l'id e
        # collassa ogni annuncio caduto nel fallback sullo stesso path, facendo
        # scartare come "già visto" ogni offerta successiva a quella collisione.
        import urllib.parse as urlparse
        try:
            parsed = urlparse.urlparse(link)
            params = urlparse.parse_qs(parsed.query)
            lhh_id = params.get("id", [""])[0]
            if lhh_id and lhh_id != "None":
                return f"lhh_{lhh_id}"
        except Exception:
            pass
    return link.split("?")[0]


def _pulisci_per_segnatura(text):
    return re.sub(r'[^a-z0-9]', '', str(text).lower())


def dedup_offerte(tutte_le_offerte, viste):
    """Deduplica una lista di ScrapedJob: prima per job_id (URL/parametro univoco,
    già visto in run precedenti tramite `viste`), poi per segnatura di contenuto
    (titolo+azienda+città, o azienda+città+inizio-snippet) per intercettare
    ripubblicazioni con URL diverso ma contenuto identico nello stesso run.
    Muta `viste` in place aggiungendo i job_id delle nuove offerte. Ritorna la
    lista delle nuove offerte (non ancora viste in nessuna forma).

    Questa logica era duplicata quasi identica tra esegui_scraping_job (qui sotto)
    e run_manual_scrape.py: un fix applicato a una copia non si propagava
    automaticamente all'altra — centralizzarla qui lo risolve alla radice."""
    nuove_offerte = []
    seen_titles = set()
    seen_snippets = set()

    for job in tutte_le_offerte:
        try:
            job_id = get_job_id(job.link)
            if job_id in viste:
                continue

            norm_title = _pulisci_per_segnatura(job.title)
            norm_company = _pulisci_per_segnatura(job.company)
            norm_city = _pulisci_per_segnatura(job.city)
            norm_snippet = _pulisci_per_segnatura(job.snippet[:60]) if job.snippet else ""

            # Con company vuota (alcuni scraper, es. IQMSelezione/Manpower e i
            # fallback HTML di MichaelPage/PagePersonnel, non la valorizzano mai)
            # la signature per titolo collasserebbe due offerte di aziende
            # realmente diverse ma con lo stesso titolo generico nella stessa
            # città: senza company a disambiguare, si salta la dedup per titolo
            # e ci si affida solo a job_id e all'eventuale signature per snippet.
            title_sig = (norm_title, norm_company, norm_city) if norm_company else None
            snippet_sig = (norm_company, norm_city, norm_snippet) if norm_snippet else None

            if title_sig and title_sig in seen_titles:
                continue
            if snippet_sig and snippet_sig in seen_snippets:
                continue

            nuove_offerte.append(job)
            viste.add(job_id)
            if title_sig:
                seen_titles.add(title_sig)
            if snippet_sig:
                seen_snippets.add(snippet_sig)
        except Exception as e:
            # Un'offerta malformata non deve far fallire la dedup dell'intero
            # batch per entrambi i chiamanti (scraping.yml e run_manual_scrape.py).
            logging.error(f"Errore dedup su un'offerta ({getattr(job, 'link', '?')}), la salto: {e}")

    return nuove_offerte


def esegui_scraping_job(orario_label):
    print(f"[{datetime.now()}] Avvio scraping delle {orario_label} in corso...")

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
    ]

    tutte_le_offerte = []
    
    for city_name, city_config in CITIES.items():
        print(f"  -> Scraping per città: {city_name}")
        for scraper in scrapers:
            offerte_scraper = scraper.scrape(city_name, city_config)
            
            # Filtro modalità ibrida/unverified se richiesto dalla città
            tutte_le_offerte.extend(filtra_offerte_per_citta(offerte_scraper, city_config))
        
    viste = load_viste()  # ora è un set
    nuove_offerte = dedup_offerte(tutte_le_offerte, viste)
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
    # Un singolo record malformato/legacy non deve mai far crashare l'invio
    # dell'intera email del giorno: viene scartato con un log, non l'intero batch.
    offerte_da_inviare = []
    for d in giornaliere_dicts:
        try:
            offerte_da_inviare.append(ScrapedJob.from_dict(d))
        except Exception as e:
            logging.error(f"Voce malformata in offerte_giornaliere.json scartata: {e} — dati: {d!r}")

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


# NOTA: non esiste un blocco scheduler `if __name__ == "__main__":` in questo file.
# In produzione GitHub Actions invoca direttamente run_manual_scrape.py (scraping)
# e run_email_job.py (invio email) via cron nei rispettivi workflow — `python
# scraper.py` non viene mai eseguito. Un precedente blocco basato sulla libreria
# `schedule` (polling loop + concorsi_module + reset notturno) è stato rimosso
# perché non girava mai in CI e dava l'impressione fuorviante che concorsi_module
# e un reset di sicurezza a mezzanotte fossero attivi.
