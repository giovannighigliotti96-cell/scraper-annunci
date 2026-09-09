import os
import re
import time
import calendar
import json
import shutil
import logging
import html as html_lib
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
# Salute dei portali: quante offerte GREZZE (prima dei filtri città/modalità) ha
# restituito ciascun portale nell'ultimo run, e se ha sollevato un errore.
# Serve da canarino: il modo in cui questa piattaforma si è rotta in passato non
# è stato un crash ma il silenzio — Adami e IQMSelezione restituivano 0 offerte
# da settimane per un selettore morto e una URL sbagliata, e nulla lo segnalava
# perché "0 offerte" è indistinguibile da "oggi non c'era nulla" nei log.
STATO_PORTALI_FILE = "stato_portali.json"

# Storico delle offerte effettivamente RECAPITATE via email. Serve perché
# offerte_giornaliere.json viene svuotato subito dopo ogni invio riuscito: senza
# questo file non esisterebbe più, il giorno dopo, alcuna traccia di cosa è stato
# proposto, e `candidature.py` non avrebbe una lista da cui far scegliere.
STORICO_OFFERTE_FILE = "storico_offerte.json"
STORICO_OFFERTE_MAX = 400  # tetto: tiene le più recenti, il file resta piccolo e committabile

# Candidature inviate davvero, indicizzate per job_id. Le aggiorna `candidature.py`.
CANDIDATURE_FILE = "candidature.json"

# Quante offerte mostrare nella sezione "da guardare per prime" in cima all'email.
TOP_OFFERTE_IN_EVIDENZA = 10
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

# Il CV .docx è la stessa versione del PDF ma con testo ESTRAIBILE PULITO.
# PyPDF2 sul PDF restituisce testo spezzato dal kerning ("m arketing",
# "R id e finizio ne", "b usine ss"): quel testo è la base sia delle keyword di
# match sia del CV inviato all'LLM, quindi il danno si propaga a tutto lo
# scoring — verificato dal vivo l'08/09/2026 confrontando le due estrazioni
# ("marketing automation" e "crm" non risultavano presenti nel CV pur essendoci).
# Si legge quindi il .docx quando disponibile, con il PDF come fallback.
CV_DOCX_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "cv_template", "Giovanni Ghigliotti CV___2026.docx")


def _estrai_testo_docx(docx_path):
    """Testo integrale del CV dal .docx (paragrafi + tabelle). Ritorna "" se il
    file non c'è o python-docx non è installato: chi chiama ricade sul PDF."""
    if not os.path.exists(docx_path):
        return ""
    try:
        import docx as _docx
        documento = _docx.Document(docx_path)
        parti = [p.text for p in documento.paragraphs if p.text and p.text.strip()]
        for tabella in documento.tables:
            for riga in tabella.rows:
                for cella in riga.cells:
                    if cella.text and cella.text.strip():
                        parti.append(cella.text)
        return "\n".join(parti).strip()
    except Exception as e:
        logging.warning(f"Estrazione CV da .docx fallita ({docx_path}): {e} — uso il PDF.")
        return ""


def estrai_keyword_cv(pdf_path):
    cv_skills = set()
    testo_docx = _estrai_testo_docx(CV_DOCX_FILE)
    if testo_docx:
        testo_docx_lower = testo_docx.lower()
        for skill in TARGET_SKILLS:
            if skill in testo_docx_lower:
                cv_skills.add(skill)
        if cv_skills:
            logging.info(f"Trovate {len(cv_skills)} competenze nel CV (.docx): {', '.join(sorted(cv_skills))}")
            return list(cv_skills)

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
    propri/acronimi aiutano il modello a leggere meglio il documento.
    Preferisce il .docx (testo pulito) al PDF, vedi _estrai_testo_docx."""
    testo_docx = _estrai_testo_docx(CV_DOCX_FILE)
    if testo_docx:
        return testo_docx
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
# ANONIMIZZAZIONE DEL CV PER I FORNITORI LLM
# ==========================================
# Il CV viene inviato per intero a ogni chiamata di valutazione semantica. Sul
# free tier di Gemini i termini dicono che Google usa i contenuti inviati per
# migliorare i propri modelli e raccomandano di non inviare dati personali:
# i dati anagrafici vengono quindi rimossi prima dell'invio.
# Al modello non servono — per stimare la compatibilità con un annuncio contano
# competenze ed esperienze, non nome, telefono o email — quindi toglierli non
# degrada il match.
# Resta un limite onesto: la storia lavorativa non è anonimizzabile senza
# distruggere proprio l'informazione che serve, e resta potenzialmente
# riconducibile alla persona.
# NOTA: si applica SOLO al testo inviato per il match. cv_personalizzazione.py
# continua a usare il CV completo, perché lì il nome serve davvero (finisce nel
# .docx generato) e va a Claude, dove i dati non sono usati per il training.
_NOMI_DA_RIMUOVERE = ["giovanni ghigliotti", "ghigliotti giovanni", "ghigliotti", "giovanni"]
_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
# Numeri di telefono: prefisso internazionale opzionale e almeno 8 cifre, con
# separatori liberi. Volutamente prudente per non intaccare cifre di business
# (fatturati, percentuali, anni) che al modello servono.
_RE_TELEFONO = re.compile(r"(?:\+\d{1,3}[\s.-]?)?(?:\d[\s.-]?){8,13}\d")


def anonimizza_cv_per_llm(testo: str) -> str:
    """Toglie dal CV i dati anagrafici prima di inviarlo a un fornitore esterno.
    Rimuove email, numeri di telefono e il nome del candidato in tutte le forme
    in cui compare nel documento, sostituendoli con segnaposto."""
    if not testo:
        return ""
    pulito = _RE_EMAIL.sub("[email rimossa]", testo)
    pulito = _RE_TELEFONO.sub("[telefono rimosso]", pulito)
    for nome in _NOMI_DA_RIMUOVERE:
        pulito = re.sub(re.escape(nome), "[nome rimosso]", pulito, flags=re.IGNORECASE)
    return pulito


# Versione del CV effettivamente inviata ai fornitori LLM.
CV_TESTO_PER_MATCH = anonimizza_cv_per_llm(CV_TESTO_COMPLETO)

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
_MATCH_LLM_SYSTEM_TEMPLATE = """Sei un recruiter senior. Devi dire se vale la pena che questo candidato si candidi a un annuncio, leggendo entrambi i testi per intero — non è un confronto di parole chiave.

VINCOLI DEL CANDIDATO (non deducibili in modo affidabile dal solo CV, tienili sempre presenti):
- Lingue: italiano madrelingua e inglese C1. NESSUN'ALTRA LINGUA. Se l'annuncio ne richiede una terza (tedesco, francese, spagnolo, cinese...) è un requisito bloccante non soddisfatto, anche quando è presentato come "gradito".
- Seniority: manager con circa 6-8 anni complessivi, con riporto diretto al board. Non è un profilo junior, e non è un direttore generale o un VP di multinazionale.
- Contesti in cui ha davvero lavorato: PMI e scale-up italiane, più un'azienda tech fondata e ceduta. NON ha esperienza dentro multinazionali strutturate, né nei settori farmaceutico, bancario o assicurativo.
- Sede: cerca solo a Genova, Milano e Torino, e non è disponibile al full remote né al trasferimento.

COME ASSEGNARE IL PUNTEGGIO (probabilità realistica di essere richiamato per un colloquio):
- 85-100 → soddisfa i requisiti principali e il ruolo è centrato sulle sue aree forti; nessun requisito bloccante mancante.
- 65-84 → buona corrispondenza, manca qualcosa di secondario (uno strumento specifico, un settore diverso ma affine).
- 40-64 → corrispondenza parziale: il ruolo è affine ma chiede requisiti che non ha, o è su un livello diverso.
- 15-39 → uno o più requisiti bloccanti non soddisfatti (una lingua che non parla, un settore molto distante, seniority molto sopra o sotto).
- 0-14 → ruolo fuori perimetro.
Non concentrare i punteggi nella fascia alta, ma distingui i requisiti BLOCCANTI da quelli DESIDERATI: i selezionatori applicano le liste di requisiti con flessibilità, e un requisito mancante non equivale a una porta chiusa.
- Sono BLOCCANTI (portano sotto 40): una lingua che il candidato non parla, una sede fuori dalle tre città o il full remote, un'abilitazione o un titolo obbligatorio che non ha, un settore fortemente regolamentato dove l'esperienza specifica è un prerequisito reale (farmaceutico, bancario, assicurativo), una seniority enormemente distante (ruoli da CEO/DG di grande gruppo, oppure junior).
- NON sono bloccanti (abbassano di 10-20 punti, non affossano): qualche anno di esperienza in più rispetto ai suoi, uno strumento specifico che non ha usato (es. Salesforce al posto di HubSpot), un modello di vendita o un tipo di struttura che non ha praticato, un settore diverso ma non regolamentato, una dimensione aziendale maggiore.
Calibrazione verificata su un caso reale: un "Digital Sales Manager" che chiedeva 8-10 anni ed esperienza come manager di manager — requisiti che il candidato non soddisfa alla lettera — lo ha in realtà portato fino all'ultimo step della selezione. Un annuncio così merita 70-80, non 50.

MOTIVAZIONE (massimo 3 righe, in italiano):
Cita elementi concreti e specifici presi DALL'ANNUNCIO, non impressioni generiche. Dì (1) qual è il requisito principale e se il candidato lo soddisfa, e (2) qual è il gap più rilevante, nominando ciò che l'annuncio chiede.
NON scrivere motivazioni come "Buon profilo con esperienza nel marketing", "Discreta compatibilità", "Il candidato ha competenze rilevanti": non aiutano a decidere se candidarsi.
Scrivi invece motivazioni come: "Chiedono di gestire un budget media da 2M, tu ne hai gestito uno da 400k: ordine di grandezza diverso. In compenso la pipeline HubSpot costruita da zero è esattamente il punto 2 della loro job description."

MODALITÀ DI LAVORO (campo "modalita"):
Leggi cosa dice l'annuncio sulla presenza in ufficio e rispondi con UNA di queste parole: "ibrido", "in sede", "da remoto", "non indicata". Usa "non indicata" solo se l'annuncio davvero non ne parla — non tirare a indovinare dal settore o dal ruolo. In "dettaglio_modalita" riporta in poche parole quello che l'annuncio dice davvero (es. "2 giorni in ufficio", "hybrid, autonomia sui giorni", "presenza quotidiana richiesta"), o lascia stringa vuota se non c'è nulla.

RETRIBUZIONE (campo "ral"):
Se l'annuncio indica una retribuzione, riportala come la scrive lui (es. "66.000-86.000 € inclusa variabile", "RAL 50-60k"). Se non la indica, stringa vuota. Non stimarla mai: una cifra inventata è peggio di nessuna cifra.

REGOLE:
- Non gonfiare il punteggio per compiacere: un punteggio basso ben motivato vale più di uno alto e vago.
- Se l'annuncio è troppo scarno per valutarlo davvero, dillo esplicitamente nella motivazione e usa un punteggio intorno a 50.
- Non riassumere l'annuncio: spiega cosa significa per QUESTO candidato.
- Valuta anche i requisiti impliciti e culturali (es. "spirito imprenditoriale" è soddisfatto da chi ha fondato un'azienda, anche se quella parola nel CV non compare).

CV DEL CANDIDATO:
{cv_testo}"""

# Modalita' di lavoro ammesse nella risposta del modello, allineate ai valori che
# il resto del codice gia' usa (detect_work_mode, filtra_offerte_per_citta).
_MODALITA_AMMESSE = {"ibrido", "in sede", "da remoto"}


def _leggi_valutazione(dati: dict) -> tuple:
    """Normalizza la risposta JSON di un fornitore in
    (probabilita, motivazione, modalita, dettaglio_modalita, ral).

    Condivisa da Claude e Gemini: ricevono lo stesso schema e devono produrre la
    stessa struttura, quindi anche la lettura e' una sola. I campi aggiunti dopo
    (modalita, RAL) sono letti in modo tollerante: una risposta che li omette
    resta valida e vale come "non indicata", cosi' un fornitore che ignorasse
    parte dello schema non fa perdere il punteggio."""
    probabilita = max(0, min(100, int(dati["probabilita"])))
    motivazione = str(dati["motivazione"]).strip()
    modalita = str(dati.get("modalita", "")).strip().lower()
    if modalita not in _MODALITA_AMMESSE:
        modalita = ""
    return (probabilita, motivazione, modalita,
            str(dati.get("dettaglio_modalita", "")).strip(),
            str(dati.get("ral", "")).strip())


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

    system_prompt = _MATCH_LLM_SYSTEM_TEMPLATE.format(cv_testo=CV_TESTO_PER_MATCH[:6000])

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
                            "modalita": {"type": "string"},
                            "dettaglio_modalita": {"type": "string"},
                            "ral": {"type": "string"},
                        },
                        "required": ["probabilita", "motivazione", "modalita", "dettaglio_modalita", "ral"],
                        "additionalProperties": False,
                    },
                },
            },
        )
        testo_risposta = llm_utils.estrai_testo_risposta(response)
        return _leggi_valutazione(json.loads(testo_risposta))
    except Exception as e:
        logging.warning(f"Match LLM Claude fallito: {type(e).__name__}: {e}")
        return None


# ==========================================
# MATCH SEMANTICO VIA GEMINI (fallback gratuito)
# ==========================================
# Secondo fornitore, usato quando Claude non è disponibile — oggi tipicamente
# per credito esaurito. Il free tier di Gemini regge ampiamente il fabbisogno
# (circa 15 richieste al minuto e oltre 1000 al giorno, contro le 5-20
# giornaliere che restano dopo aver spostato la valutazione a valle dei filtri).
#
# ATTENZIONE, limite noto e accettato dall'utente: i termini del free tier
# dicono che Google usa i contenuti inviati per migliorare i propri modelli e
# raccomandano di non inviare informazioni personali. Il CV viaggia in ogni
# chiamata. Sul tier a pagamento questo non avviene.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# Cascata di modelli, dal piu' capace al piu' leggero. Verificato dal vivo il
# 09/09/2026 che ogni modello ha una quota SEPARATA: con gemini-3.8-flash gia'
# esaurito, gli altri tre rispondevano ancora. Su 429 conviene quindi passare al
# modello successivo (risposta immediata) invece di attendere che si liberi la
# finestra del primo, e la capacita' complessiva del free tier si moltiplica.
GEMINI_MODELLI = [
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]
GEMINI_MODEL = GEMINI_MODELLI[0]  # compatibilita' con chi legge il singolo nome

try:
    from google import genai as google_genai
    GEMINI_SDK_AVAILABLE = True
except ImportError:
    GEMINI_SDK_AVAILABLE = False

_gemini_client = None
_gemini_warning_shown = False

# Richieste al minuto ammesse dal free tier. Misurato dal vivo il 09/09/2026:
# il 429 di gemini-3.8-flash riporta testualmente "limit: 5" — un terzo di
# quanto indicavano le fonti di terze parti, che davano 15. Senza spaziare le
# chiamate, dalla sesta in poi fallivano tutte: in un run con 20 offerte da
# valutare significava perderne 15.
# 4 e non 5: il limite dichiarato e' 5, ma e' misurato su una finestra
# scorrevole e i retry consumano anch'essi slot. Un margine sotto la soglia
# costa ~3 secondi in piu' per offerta e evita di rimbalzare sul 429.
GEMINI_RICHIESTE_AL_MINUTO = 4
_GEMINI_INTERVALLO_MINIMO_S = 60.0 / GEMINI_RICHIESTE_AL_MINUTO
_gemini_ultima_chiamata = 0.0

# Quanti tentativi fare quando il server risponde 429. Il messaggio d'errore
# indica quanti secondi attendere ("Please retry in 36.7s"): si rispetta quel
# valore invece di indovinare un backoff.
GEMINI_TENTATIVI_SU_429 = 3
_RE_ATTESA_429 = re.compile(r"retry in (\d+(?:\.\d+)?)s")


def _attendi_slot_gemini():
    """Spaziatura proattiva tra le chiamate, per restare sotto il limite al
    minuto invece di scoprirlo con un errore."""
    global _gemini_ultima_chiamata
    da_aspettare = _GEMINI_INTERVALLO_MINIMO_S - (time.monotonic() - _gemini_ultima_chiamata)
    if da_aspettare > 0:
        time.sleep(da_aspettare)
    _gemini_ultima_chiamata = time.monotonic()


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None and GEMINI_SDK_AVAILABLE and GEMINI_API_KEY:
        _gemini_client = google_genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client


def valuta_match_gemini(job_text: str) -> tuple:
    """Stessa valutazione di valuta_match_llm ma via Gemini. Ritorna
    (probabilita, motivazione) oppure None se non disponibile o fallita, così
    il chiamante può ricadere sull'euristica."""
    global _gemini_warning_shown
    client = _get_gemini_client()
    if client is None or not CV_TESTO_COMPLETO:
        if not _gemini_warning_shown:
            if not GEMINI_SDK_AVAILABLE:
                logging.warning("Gemini non disponibile: pacchetto 'google-genai' non installato.")
            elif not GEMINI_API_KEY:
                logging.warning("Gemini non disponibile: GEMINI_API_KEY mancante.")
            _gemini_warning_shown = True
        return None

    istruzioni = _MATCH_LLM_SYSTEM_TEMPLATE.format(cv_testo=CV_TESTO_PER_MATCH[:6000])
    ultimo_errore = None
    for indice, modello in enumerate(GEMINI_MODELLI):
        _attendi_slot_gemini()
        try:
            risultato = _chiama_gemini(client, istruzioni, job_text, modello)
            if indice:
                logging.info(f"Match Gemini servito da {modello} (i modelli precedenti erano in quota).")
            return risultato
        except Exception as e:
            ultimo_errore = e
            if not _e_errore_di_quota(e):
                logging.warning(f"Match Gemini fallito su {modello}: {type(e).__name__}: {e}")
                return None
            logging.info(f"{modello} in quota, provo il modello successivo.")

    # Tutti i modelli sono in quota: si aspetta il tempo indicato dall'ultimo
    # errore e si riprova una volta sola con il modello migliore. Oltre questo
    # punto conviene arrendersi e tenere il punteggio euristico, invece di
    # allungare il run per un'offerta.
    attesa = _secondi_di_attesa(ultimo_errore)
    logging.info(f"Tutti i modelli Gemini in quota, attendo {attesa:.0f}s e riprovo una volta.")
    time.sleep(attesa)
    try:
        return _chiama_gemini(client, istruzioni, job_text, GEMINI_MODELLI[0])
    except Exception as e:
        logging.warning(f"Match Gemini: quota esaurita su tutti i modelli. Ultimo errore: {type(e).__name__}")
        return None


def _e_errore_di_quota(errore) -> bool:
    testo = str(errore)
    return "429" in testo or "RESOURCE_EXHAUSTED" in testo or "too_many_requests" in testo


def _secondi_di_attesa(errore) -> float:
    """Secondi indicati dal server nel messaggio 429 ("Please retry in 18.6s"),
    con un margine di 1s: ripartire spaccando il secondo rifallisce."""
    trovato = _RE_ATTESA_429.search(str(errore or ""))
    return float(trovato.group(1)) + 1 if trovato else 30.0


def _chiama_gemini(client, istruzioni, job_text, modello):
    """La singola richiesta a Gemini. Separata da valuta_match_gemini perché
    quest'ultima la ritenta: tenere insieme retry e costruzione della richiesta
    rendeva il flusso di controllo difficile da seguire. Le eccezioni si
    propagano al chiamante, che decide se ritentare."""
    risposta = client.interactions.create(
        model=modello,
        input=(f"{istruzioni}\n\n"
               f"TESTO INTEGRALE DELL'OFFERTA DI LAVORO:\n{job_text[:8000]}"),
        response_format={
            "type": "text",
            "mime_type": "application/json",
            "schema": {
                "type": "object",
                "properties": {
                    "probabilita": {"type": "integer"},
                    "motivazione": {"type": "string"},
                    "modalita": {"type": "string"},
                    "dettaglio_modalita": {"type": "string"},
                    "ral": {"type": "string"},
                },
                "required": ["probabilita", "motivazione", "modalita", "dettaglio_modalita", "ral"],
            },
        },
    )
    return _leggi_valutazione(json.loads(risposta.output_text))


def valuta_match_semantico(job_text: str) -> tuple:
    """Valutazione semantica con i fornitori disponibili, in ordine: prima
    Claude (qualità migliore, a pagamento), poi Gemini (free tier). Ritorna
    None se nessuno dei due è utilizzabile, e in quel caso il chiamante tiene
    il punteggio dell'euristica."""
    risultato = valuta_match_llm(job_text)
    if risultato is not None:
        return risultato
    return valuta_match_gemini(job_text)

def valuta_match_candidato(job_text: str) -> tuple:
    """Scoring applicato a OGNI annuncio durante lo scraping: solo l'euristica,
    nessuna chiamata LLM.

    La valutazione semantica costa una chiamata a pagamento e prima girava qui,
    cioè su ogni titolo valido — 97 chiamate in un run reale (08/09/2026) per 19
    offerte che poi arrivavano davvero in email: l'80% del costo bruciato su
    annunci scartati subito dopo da città, freschezza o deduplica.
    Ora l'LLM interviene solo su ciò che è sopravvissuto a tutti i filtri, in
    arricchisci_offerte_con_llm()."""
    return calcola_probabilita_callback(job_text.lower())


def arricchisci_offerte_con_llm(offerte):
    """Rivaluta con l'LLM le offerte che arriveranno davvero in email,
    sostituendo probabilità e motivazione calcolate dall'euristica.

    Va chiamata DOPO tutti i filtri e la deduplica: è il punto in cui il numero
    di annunci è minimo e ognuno vale la spesa. Se l'LLM non è disponibile o
    fallisce su una singola offerta, quella conserva il punteggio euristico e il
    ciclo prosegue — un problema con l'API non deve mai bloccare l'invio.
    """
    if not offerte:
        return offerte
    if not CV_TESTO_COMPLETO or (_get_anthropic_client() is None and _get_gemini_client() is None):
        logging.info(f"Nessun fornitore semantico disponibile: {len(offerte)} offerte restano con il punteggio euristico.")
        return offerte

    riuscite = 0
    for job in offerte:
        testo = job.testo_completo or f"{job.title} {job.company} {job.snippet}"
        try:
            risultato = valuta_match_semantico(testo)
        except Exception as e:
            logging.error(f"Valutazione LLM fallita per '{job.title}': {e}")
            risultato = None
        if risultato is not None:
            probabilita, motivazione, modalita, dettaglio, ral = risultato
            job.probabilita, job.motivazione = probabilita, motivazione
            if ral:
                job.ral = ral
            # La modalita' letta dall'LLM sostituisce quella dedotta dai pattern
            # testuali solo quando il modello ne ha trovata una esplicita: legge
            # l'annuncio per intero e capisce le forme che detect_work_mode non
            # copre ("autonomia sui giorni in ufficio"). Se dice "non indicata"
            # si tiene quella precedente, per non perdere informazione.
            if modalita:
                if modalita != job.work_mode:
                    logging.info(f"Modalita' corretta dall'LLM per '{job.title}': "
                                 f"{job.work_mode} -> {modalita}")
                job.work_mode = modalita
                job.dettaglio_modalita = dettaglio
            riuscite += 1
    logging.info(f"Valutazione LLM: {riuscite}/{len(offerte)} offerte rivalutate semanticamente.")
    return offerte

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

# ==========================================
# REQUISITI BLOCCANTI E PENALITÀ
# ==========================================
# Il vecchio punteggio era una pura copertura di keyword:
#     coverage = trovati / (trovati + gap);  score = 15 + coverage * 80
# Contava le parole del CV presenti nell'annuncio, quindi un annuncio che ne
# nominava molte e non intercettava alcun gap arrivava a 95 A PRESCINDERE dai
# requisiti che il candidato non soddisfa. È così che un "Responsabile
# Commerciale - INGLESE/TEDESCO" ha preso 95: nel testo c'erano B2B, budget,
# KPI, stakeholder, e il tedesco non pesava nulla.
# Le penalità qui sotto rendono il punteggio una stima di IDONEITÀ, non di
# somiglianza: ogni requisito esplicito che il profilo non soddisfa sottrae
# punti e finisce nella motivazione, così si vede subito perché.

# Anni di esperienza richiesti dall'annuncio. Il CV copre il 2022-oggi con
# ruoli manageriali (più la fondazione dell'azienda): oltre gli 8 anni la
# richiesta non è soddisfatta.
ANNI_ESPERIENZA_CV = 8
_RE_ANNI_ESPERIENZA = re.compile(
    r"(?:almeno\s+|minimo\s+|min\.?\s*|oltre\s+|)(\d{1,2})\s*\+?\s*"
    r"(?:anni|years?)(?:\s+di)?\s*(?:esperienz|experience|seniority)",
    re.IGNORECASE,
)

# Requisiti che, se presenti nell'annuncio, il profilo NON soddisfa.
# Il peso è la penalità in punti percentuali.
_PENALITA_REQUISITI = {
    # Radici, non parole intere: gli annunci declinano al femminile ("ottima
    # conoscenza della lingua tedesca") e con "tedesco" secco la penalita' non
    # scattava — verificato con un caso reale che restava a 95.
    "Lingua non posseduta": (25, [
        "tedesc", "german", "deutsch", "frances", "french", "spagnol",
        "spanish", "lingua russ", "cines", "chinese", "portoghes", "arab",
    ]),
    "Salesforce richiesto": (10, ["salesforce"]),
    "Marketo / Eloqua / Adobe": (8, ["marketo", "eloqua", "adobe campaign", "adobe analytics"]),
    "SQL / Python avanzati": (8, ["sql avanzato", "python", "power bi developer", "data engineering"]),
    "Settore regolamentato o distante": (12, [
        "farmaceut", "pharma", "dispositivi medici", "life science",
        "bancario", "banking", "assicurativ", "credito al consumo",
    ]),
    "Settore luxury / fashion": (8, ["luxury", "alta moda", "haute couture"]),
}

# Segnali che l'annuncio cerca un profilo di seniority molto superiore. Non
# sono un gap di competenza ma di scala: candidarsi non porta a nulla.
_SEGNALI_SOVRA_SENIORITY = [
    "chief executive officer", "amministratore delegato", "direttore generale",
    "general manager", "vice president", "svp ", "evp ",
]


def _penalita_esperienza(testo: str):
    """(penalità, etichetta) per gli anni di esperienza richiesti in eccesso.
    Si prende la richiesta PIÙ ALTA presente nel testo: un annuncio che cita
    più soglie ("3 anni nel ruolo, 10 nel settore") è vincolato dalla maggiore."""
    anni_richiesti = [int(m) for m in _RE_ANNI_ESPERIENZA.findall(testo)]
    anni_richiesti = [a for a in anni_richiesti if 1 <= a <= 30]
    if not anni_richiesti:
        return 0, None
    massimo = max(anni_richiesti)
    if massimo <= ANNI_ESPERIENZA_CV:
        return 0, None
    # 4 punti per ogni anno mancante, fino a un tetto di 20.
    return min(20, (massimo - ANNI_ESPERIENZA_CV) * 4), f"{massimo} anni richiesti"


def calcola_probabilita_callback(testo: str) -> tuple:
    """Stima la probabilità (0-100) di essere richiamato, confrontando il testo
    dell'offerta con il profilo di Giovanni. Ritorna (probabilita, motivazione).

    Due componenti: quanto il profilo copre ciò che l'annuncio chiede, MENO le
    penalità per i requisiti espliciti che non soddisfa."""
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

    # --- penalità ---
    penalita = []
    for etichetta, (peso, chiavi) in _PENALITA_REQUISITI.items():
        if any(k in t for k in chiavi):
            score -= peso
            penalita.append(etichetta)

    peso_anni, etichetta_anni = _penalita_esperienza(t)
    if peso_anni:
        score -= peso_anni
        penalita.append(etichetta_anni)

    if any(seg in t for seg in _SEGNALI_SOVRA_SENIORITY):
        score -= 15
        penalita.append("seniority molto superiore")

    # Un annuncio che non nomina quasi nulla del profilo non merita un punteggio
    # alto solo perché non ha gap: senza questo, tre keyword generiche e zero gap
    # davano coverage 1.0 e quindi 95.
    if len(trovati) <= 2:
        score = min(score, 60)

    score = max(5, min(95, score))

    ha_str = ", ".join(trovati[:4])
    if len(trovati) > 4:
        ha_str += f" (+{len(trovati) - 4} altri)"

    parti = []
    if trovati:
        parti.append(f"Hai: {ha_str}")
    else:
        parti.append("Nessuna competenza rilevata nel testo")
    if gap:
        parti.append(f"Gap: {', '.join(gap[:3])}")
    if penalita:
        parti.append(f"Non soddisfi: {', '.join(penalita[:3])}")
    if not gap and not penalita and trovati:
        parti.append("Nessun gap rilevato")

    return score, ". ".join(parti)


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

    # In sede — "in presenza" rimosso perché ambiguo nei contratti ibridi.
    # "sede di lavoro" e "presso la sede" RIMOSSI: non sono indicatori di modalità
    # ma etichette di LUOGO presenti praticamente in ogni annuncio italiano
    # ("Sede di lavoro: Milano"). Classificavano quindi come "in sede" la quasi
    # totalità degli annunci, che filtra_offerte_per_citta scarta silenziosamente
    # per Milano e Torino (filter_hybrid_only=True) — cioè i due mercati più
    # grandi perdevano quasi tutte le offerte prima ancora di essere valutate.
    # Restano solo i pattern che descrivono davvero la modalità di lavoro.
    onsite_patterns = ["in sede", "on-site", "onsite", "presenza obbligatoria",
                       "lavoro in ufficio", "presenza in ufficio", "giorni in ufficio",
                       "giorni a settimana in ufficio",
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

# Date di pubblicazione lette dal JSON-LD della pagina di dettaglio, indicizzate
# per URL. Molti portali (Hays, ReverseGroup, MichaelPage) non mostrano la data
# nella pagina di elenco ma la espongono nel JSON-LD del dettaglio, che
# calcola_punteggio_e_modalita scarica comunque per lo scoring: leggerla li'
# costa zero richieste in piu'.
# E' una cache di processo, non uno stato persistente: viene popolata durante lo
# scraping e letta subito dopo da filtra_offerte_per_citta, nello stesso run.
# L'alternativa era aggiungere un ottavo valore di ritorno e toccare tutti e 13
# i chiamanti, con il rischio di sbagliarne uno in silenzio.
_DATE_ANNUNCIO_DA_JSONLD = {}

_RE_DATE_POSTED = re.compile(r'"datePosted"\s*:\s*"(\d{4}-\d{2}-\d{2})')


def _estrai_date_posted(html: str) -> str:
    """Data di pubblicazione dal JSON-LD grezzo della pagina. Si legge dall'HTML
    e non dal testo estratto: BeautifulSoup.get_text() non restituisce il
    contenuto dei tag <script>, dove il JSON-LD vive (verificato dal vivo su
    Hays e MichaelPage l'09/09/2026)."""
    match = _RE_DATE_POSTED.search(html or "")
    return match.group(1) if match else ""


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
            data_ld = _estrai_date_posted(resp.text)
            if data_ld:
                _DATE_ANNUNCIO_DA_JSONLD[url] = data_ld
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

    # --- Livello "Head of" e C-level ---
    # Verificato dal vivo (test end-to-end 08/09/2026, MichaelPage e Hays):
    # "Head of Marketing" e "Head of Digital Marketing FMCG" venivano SCARTATI,
    # pur essendo esattamente il livello del candidato — l'unica variante
    # "head of" presente era "head of growth". Un intero livello di seniority
    # (quello a cui il CV punta) era strutturalmente invisibile allo scraper.
    "head of marketing",
    "head of digital marketing",
    "head of sales",
    "head of sales and marketing",
    "head of sales & marketing",
    "head of marketing and sales",
    "head of marketing & sales",
    "head of commercial",
    # "head of revenue" RIMOSSA: troppo larga, faceva passare "Executive Head of
    # Revenue Operations & Growth, Europe" — un ruolo RevOps, lontano dal profilo.
    "head of e-commerce",
    "head of ecommerce",
    "head of demand generation",
    "head of performance marketing",
    "head of b2b marketing",
    "chief marketing officer",
    "chief revenue officer",
    "chief commercial officer",

    # --- Commerciale ---
    # Il business development è stato ESCLUSO su indicazione esplicita
    # dell'utente (09/09/2026): non è un lavoro che vuole fare, e nella prima
    # email post-fix ne era pieno. Restano i ruoli commerciali di direzione,
    # che il CV giustifica con l'ownership su revenue/P&L e il riporto al board.
    # Le esclusioni per "business develop*" sono in TITLE_EXCLUSIONS, così un
    # titolo misto tipo "Sales & Business Development Manager" non rientra
    # dalla finestra tramite un'altra voce della lista.
    # "sales director", "direttore vendite", "responsabile vendite" e
    # "direttore commerciale" RIMOSSI il 09/09/2026 sulla base delle offerte
    # realmente recapitate: portavano vendita tradizionale senza componente
    # digitale, cioe' il terreno dove il profilo compete peggio. Punteggi medi
    # misurati: responsabile vendite 12, direttore commerciale 30, sales
    # director 42, contro una media di 83 per "country manager" e 88 per
    # "head of marketing". "direttore vendite" non aveva mai prodotto nulla.
    "commercial manager",
    "responsabile commerciale",
    "country manager",

    # --- Digital / E-commerce / Revenue ---
    "e-commerce manager",
    "ecommerce manager",
    "e-commerce director",
    # "digital manager" e "responsabile digital" RIMOSSE: genericissime, facevano
    # passare qualunque ruolo con "digital" nel titolo (es. "Responsabile Digital
    # Payments", un ruolo di prodotto finance). Al loro posto la sola variante
    # utile davvero: l'ordine invertito di "digital sales manager".
    "sales digital manager",
    "digital director",
    "responsabile e-commerce",
    # "revenue manager" e "revenue operations manager" RIMOSSE: la prima nel
    # mercato italiano indica quasi sempre il revenue management alberghiero,
    # la seconda un ruolo RevOps — entrambe fuori perimetro.
    "marketing lead",
    "growth lead",

    # --- Varianti di grado dei ruoli gia' in lista (aggiunte 09/09/2026) ---
    # Nessun ruolo NUOVO: solo altre forme e altri gradi di titoli che l'utente
    # cerca gia'. "sales manager" e "general manager" sono stati volutamente
    # LASCIATI FUORI: il primo e' larghissimo in Italia (si porterebbe dietro
    # Area/Technical/Product Sales Manager), il secondo sta sopra il livello
    # attuale del profilo.
    "growth director",
    "chief growth officer",
    "vp marketing",
    "vp sales",
    "head of gtm",
    "responsabile go to market",
    "head of customer acquisition",
    "head of crm",
    "responsabile crm",
]

# Titoli che valgono SOLO se il titolo dell'annuncio corrisponde esattamente,
# non come sottostringa. Richiesta esplicita dell'utente per "head of digital"
# ("o sono scritti così o niente"): come sottostringa catturerebbe qualunque
# "Head of Digital <qualcosa>" — Transformation, Innovation, Operations — che
# sono ruoli diversi. È lo stesso errore gia' fatto con "digital manager".
# Il confronto ignora il suffisso descrittivo dopo un separatore, perche' i
# portali lo aggiungono quasi sempre ("Head of Digital - Fashion Brand"):
# conta la parte di titolo prima di "-", "|", "(" o ",".
TITOLI_MATCH_ESATTO = [
    "head of digital",
]

_SEPARATORI_TITOLO = re.compile(r"\s*[-–—|(/,:]")


def _titolo_base(titolo: str) -> str:
    """Parte significativa del titolo, prima del suffisso descrittivo che i
    portali aggiungono dopo un separatore. "Head of Digital - Fashion Brand"
    -> "head of digital"."""
    return _SEPARATORI_TITOLO.split(titolo.lower().strip(), 1)[0].strip()

# Keyword di ricerca da inviare alle API/search box dei portali che supportano
# la ricerca per titolo (LinkedIn, LHH, GiGroup). Una voce per ciascun titolo
# target distinto in EXACT_TITLES (varianti di punteggiatura "&"/"e"/"and"
# accorpate in una sola voce). I portali che invece scaricano un'intera
# pagina categoria e filtrano lato client (MichaelPage, PagePersonnel, Wyser,
# Manpower, IQMSelezione) vedono già tutti i titoli tramite is_valid_job_title
# e non hanno bisogno di questa lista.
SEARCH_KEYWORDS = [
    # Query INVIATE ai portali con ricerca per testo (LinkedIn, LHH, Hays,
    # GiGroup). Rifatta il 08/09/2026: la lista precedente aveva 24 voci quasi
    # tutte long-tail ("Growth and GTM Manager", "CRM and Marketing Automation
    # Manager", ...) che su motori a matching fuzzy restituiscono in pratica lo
    # stesso set generico di "Marketing Manager" — costo pieno in richieste,
    # recall aggiuntiva nulla — e nessuna copriva le famiglie di ruoli aggiunte
    # a EXACT_TITLES (Head of / BizDev / commerciale / e-commerce), che quindi
    # restavano invisibili proprio sui portali dove la ricerca la fa la query.
    # Qui stanno solo query ampie, una per famiglia: il filtro fine lo applica
    # comunque is_valid_job_title sui titoli che tornano.
    "Marketing Manager",
    "Marketing Director",
    "Head of Marketing",
    "Digital Marketing Manager",
    "Growth Manager",
    "Head of Growth",
    "Sales & Marketing Manager",
    "Digital Sales Manager",
    # Le formule con cui le aziende descrivono i ruoli che uniscono digitale e
    # vendita: sono la categoria con la resa piu' alta (media 91 sulle offerte
    # realmente recapitate, contro 81 del marketing puro e 70 del sales puro)
    # ma anche la piu' rara nel flusso — 1 offerta su 50. Cercarle
    # esplicitamente e' l'unico modo per aumentarne il numero.
    "Digital Sales & Marketing Manager",
    "Revenue Marketing Manager",
    "Responsabile Marketing e Vendite",
    "Head of Sales",
    # "Business Development Manager" RIMOSSA: il BizDev e' escluso da
    # TITLE_EXCLUSIONS, quindi questa query spendeva richieste per raccogliere
    # annunci che venivano scartati subito dopo.
    "Demand Generation Manager",
    "B2B Marketing Manager",
    "E-commerce Manager",
    "Chief Marketing Officer",
    "Country Manager",
    "Responsabile Marketing",
    "Responsabile Commerciale",
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
    "event planner", "event specialist", "event manager", "public relations", "pr specialist",

    # Ruoli che richiedono una LINGUA che il candidato non parla. Il CV dichiara
    # italiano madrelingua e inglese C1, nient'altro: un annuncio che mette una
    # terza lingua nel titolo la considera un requisito di primo piano, non un
    # "gradito". Osservato dal vivo nella mail dell'08/09: "Responsabile
    # Commerciale - INGLESE/TEDESCO o FRANCESE/INGLESE" arrivava al 95%, ma
    # entrambe le combinazioni richiedono una lingua che il candidato non ha.
    # Radici anche qui, per coerenza con _PENALITA_REQUISITI: un titolo puo'
    # declinare al femminile ("lingua tedesca") esattamente come il corpo.
    "tedesc", "german", "deutsch", "frances", "french", "spagnol", "spanish",
    "portoghes", "cines", "chinese", "arabo", "arabic",
    "olandes", "dutch", "polacc", "polish", "madrelingua", "native speaker",

    # Ruoli di prodotto/settore finance e RevOps: fuori perimetro per esplicita
    # indicazione dell'utente ("digital payments non è una mia posizione, e
    # finance principalmente").
    "digital payments", "revenue operations", "revops",
    # Business development: escluso su indicazione esplicita dell'utente
    # (09/09/2026). Sta tra le ESCLUSIONI e non semplicemente fuori da
    # EXACT_TITLES perché così blocca anche i titoli misti, che altrimenti
    # passerebbero grazie all'altra meta' del titolo (es. "Sales & Business
    # Development Manager" contiene "sales & marketing manager"? no, ma
    # "Head of Sales & Business Development" contiene "head of sales").
    "business development", "business developer", "business develop",
    "sviluppo commerciale", "bizdev", "biz dev",
    "private banker", "credit manager", "financial controller", "risk manager",
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
    """Età in giorni di una data di pubblicazione. Ritorna None se mancante o
    non parsabile — un formato inatteso non deve mai escludere un annuncio
    per errore, solo non applicare il filtro di freschezza a quello specifico.

    Formati riconosciuti:
    - ISO YYYY-MM-DD, anche come prefisso di un datetime (YYYY-MM-DDTHH:MM:SSZ)
    - gg/mm/aaaa e gg-mm-aaaa, usati dai portali italiani (PRAXI pubblica
      "Data pubblicazione: 03/09/2026"): prima venivano ignorati silenziosamente,
      quindi la data c'era ma il filtro di freschezza non si applicava mai.
    """
    if not date_str:
        return None
    testo = str(date_str).strip()
    oggi = datetime.now().date()

    try:
        d = datetime.strptime(testo[:10], "%Y-%m-%d").date()
        return (oggi - d).days
    except Exception:
        pass

    match = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", testo)
    if match:
        giorno, mese, anno = (int(g) for g in match.groups())
        try:
            return (oggi - datetime(anno, mese, giorno).date()).days
        except ValueError:
            return None
    return None


# Età massima di un annuncio perché arrivi in email, per i portali diversi da
# LinkedIn (che ha una soglia propria: vedi LinkedInScraper.MAX_ETA_GIORNI).
# 7 giorni, non 30: con 30 arrivavano ancora annunci chiaramente stantii — un
# "Business Developer" di Hays presentato al 95% risultava pubblicato 60 giorni
# prima leggendone il JSON-LD. Candidarsi a una ricerca vecchia di settimane
# vale poco, quindi la finestra utile e' quella della settimana.
# Le agenzie di ricerca e selezione lasciano gli annunci online per mesi anche
# quando la ricerca è di fatto chiusa: verificato dal vivo l'08/09/2026 che tra
# i risultati MichaelPage comparivano ancora ref "jn-052026", cioè annunci di
# maggio, presentati come novità del giorno. Candidarsi a una ricerca vecchia di
# mesi è tempo sprecato. Il filtro si applica SOLO quando la data è nota: un
# annuncio senza data non viene mai scartato per questo motivo.
MAX_ETA_GIORNI_ANNUNCIO = 7


def data_pubblicazione_effettiva(job) -> str:
    """Data di pubblicazione più attendibile disponibile per l'offerta.

    Ordine di preferenza: il datePosted del JSON-LD della pagina di dettaglio,
    poi la data che lo scraper ha letto dalla pagina di elenco. Il JSON-LD vince
    perché è la data dichiarata dal portale stesso, mentre quella di elenco a
    volte è assente e a volte è un'approssimazione (per MichaelPage si ricava
    dal ref nell'URL, che ha precisione solo mensile)."""
    data_ld = _DATE_ANNUNCIO_DA_JSONLD.get(job.link, "")
    return data_ld or job.date


def offerta_troppo_vecchia(job) -> bool:
    """True se l'annuncio ha una data di pubblicazione nota e più vecchia della
    soglia. LinkedIn è escluso perché applica già la propria soglia (3 giorni)
    a monte, dentro lo scraper. Un annuncio senza data non viene mai scartato."""
    if job.portal == "LinkedIn":
        return False
    eta = _eta_giorni_da_data(data_pubblicazione_effettiva(job))
    return eta is not None and eta > MAX_ETA_GIORNI_ANNUNCIO

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

    # 3. Titoli ammessi solo per corrispondenza esatta (vedi TITOLI_MATCH_ESATTO)
    if _titolo_base(t) in TITOLI_MATCH_ESATTO:
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


# Numero di run consecutivi a zero offerte grezze oltre il quale un portale viene
# segnalato come probabilmente rotto nell'email. Con 4 run al giorno, 12 equivale
# a circa tre giorni consecutivi senza che il portale restituisca NULLA: abbastanza
# per escludere una giornata di mercato scarsa, abbastanza poco per accorgersene
# entro pochi giorni invece che dopo settimane.
RUN_A_ZERO_PER_ALLARME = 12


def aggiorna_stato_portali(conteggi_grezzi, errori=None):
    """Aggiorna il contatore di run consecutivi a zero per ogni portale.
    `conteggi_grezzi` è {nome_portale: n_offerte_grezze} sommato su tutte le città
    (grezze = prima dei filtri città/modalità: un portale che scarica annunci ma li
    scarta tutti per titolo funziona, non è rotto).
    """
    try:
        stato = state_io.load_json_or_raise(STATO_PORTALI_FILE, {})
        if not isinstance(stato, dict):
            stato = {}
    except Exception as e:
        logging.error(f"Errore lettura {STATO_PORTALI_FILE}, riparto da zero: {e}")
        stato = {}

    errori = errori or {}
    for portale, n in conteggi_grezzi.items():
        voce = stato.get(portale) or {}
        voce["ultime_offerte"] = n
        voce["run_a_zero"] = 0 if n > 0 else int(voce.get("run_a_zero", 0)) + 1
        voce["ultimo_run"] = datetime.now().isoformat(timespec="seconds")
        voce["ultimo_errore"] = errori.get(portale, "")
        stato[portale] = voce

    try:
        _atomic_write_json(STATO_PORTALI_FILE, stato, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Errore scrittura {STATO_PORTALI_FILE}: {e}")
    return stato


def carica_storico_offerte():
    """Offerte già recapitate via email, dalla più recente. Lista di dict."""
    try:
        dati = state_io.load_json_or_raise(STORICO_OFFERTE_FILE, [])
        return dati if isinstance(dati, list) else []
    except Exception as e:
        logging.error(f"Errore lettura {STORICO_OFFERTE_FILE}: {e}")
        return []


def registra_offerte_inviate(offerte):
    """Aggiunge in testa allo storico le offerte appena recapitate, senza
    duplicare quelle già presenti (stesso job_id) e tenendo solo le più recenti.
    Chiamata SOLO dopo un invio SMTP riuscito: lo storico deve riflettere ciò che
    è davvero arrivato nella casella, non ciò che si stava per inviare."""
    storico = carica_storico_offerte()
    gia_presenti = {voce.get("job_id") for voce in storico if isinstance(voce, dict)}
    oggi = datetime.now().strftime("%Y-%m-%d")

    nuove = []
    for job in offerte:
        try:
            job_id = get_job_id(job.link)
        except Exception:
            continue
        if not job_id or job_id in gia_presenti:
            continue
        gia_presenti.add(job_id)
        nuove.append({
            "job_id": job_id,
            "data_invio": oggi,
            "titolo": job.title,
            "azienda": job.company,
            "citta": job.city,
            "portale": job.portal,
            "probabilita": _prob_ordinabile(job),
            "link": job.link,
        })

    if not nuove:
        return storico
    aggiornato = (nuove + storico)[:STORICO_OFFERTE_MAX]
    try:
        _atomic_write_json(STORICO_OFFERTE_FILE, aggiornato, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Errore scrittura {STORICO_OFFERTE_FILE}: {e}")
    return aggiornato


def segnature_storico():
    """{segnatura titolo+azienda: data del primo invio} dallo storico.

    Serve a riconoscere le RIPUBBLICAZIONI. LinkedIn (e non solo) rimette online
    lo stesso annuncio con un URL nuovo e una data di pubblicazione azzerata:
    la card dice "1 giorno fa" ma la posizione è aperta da settimane. Il dedup
    lavora sull'URL e non le intercetta, e il filtro di freschezza legge la data
    falsa. Confrontare titolo+azienda con ciò che è già stato recapitato è
    l'unico segnale disponibile, e almeno permette di dirlo in email invece di
    presentare come nuova una posizione già vista."""
    segnature = {}
    for voce in carica_storico_offerte():
        if not isinstance(voce, dict):
            continue
        titolo = _pulisci_per_segnatura(voce.get("titolo", ""))
        azienda = _pulisci_per_segnatura(voce.get("azienda", ""))
        # Senza azienda la segnatura collasserebbe titoli generici di aziende
        # diverse ("Marketing Manager") e segnalerebbe come ripubblicazione
        # un'offerta nuova di un'altra azienda.
        if not titolo or not azienda or azienda == _pulisci_per_segnatura("Azienda non specificata"):
            continue
        chiave = (titolo, azienda)
        if chiave not in segnature:
            segnature[chiave] = voce.get("data_invio", "")
    return segnature


def carica_candidature():
    """Candidature inviate, come dict {job_id: dati}."""
    try:
        dati = state_io.load_json_or_raise(CANDIDATURE_FILE, {})
        return dati if isinstance(dati, dict) else {}
    except Exception as e:
        logging.error(f"Errore lettura {CANDIDATURE_FILE}: {e}")
        return {}


def salva_candidature(candidature):
    _atomic_write_json(CANDIDATURE_FILE, candidature, ensure_ascii=False, indent=2)


def candidature_per_job_id():
    """Come carica_candidature(), ma pensata per l'email: usata per segnalare
    un'offerta che ricompare da un portale diverso (URL diverso, quindi non
    intercettata dal dedup) e per cui però ti sei già candidato — senza questo
    avviso si rischia una seconda candidatura alla stessa posizione."""
    return carica_candidature()


def portali_sospetti():
    """Portali fermi a zero offerte grezze da troppi run consecutivi, cioè
    probabilmente rotti (selettore cambiato, URL morta, blocco anti-bot).
    Ritorna una lista di stringhe già pronte per l'email."""
    try:
        stato = state_io.load_json_or_raise(STATO_PORTALI_FILE, {})
    except Exception:
        return []
    if not isinstance(stato, dict):
        return []
    righe = []
    for portale, voce in sorted(stato.items()):
        if not isinstance(voce, dict):
            continue
        zeri = int(voce.get("run_a_zero", 0) or 0)
        if zeri >= RUN_A_ZERO_PER_ALLARME:
            errore = voce.get("ultimo_errore") or ""
            righe.append(f"{portale}: 0 offerte da {zeri} run consecutivi"
                         + (f" (ultimo errore: {errore[:80]})" if errore else ""))
    return righe

# ==========================================
# CLASSI SCRAPERS PER SINGOLI PORTALI
# ==========================================

class ScrapedJob:
    def __init__(self, title, company, portal, link, date="", snippet="", match_level="Base", match_count=0, city="", work_mode="unverified", fetch_status="no_attempt", probabilita=0, motivazione="", testo_completo="", ral="", dettaglio_modalita=""):
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
        # Retribuzione dichiarata dall'annuncio, quando c'e': la estrae l'LLM
        # durante la valutazione semantica (vedi _leggi_valutazione). Sapere
        # subito che un ruolo e' sotto la propria fascia evita di aprire il link.
        self.ral = ral if ral else ""
        # Cosa dice l'annuncio sulla presenza in ufficio, con le sue parole
        # ("2 giorni in sede", "autonomia sui giorni"): work_mode da solo dice
        # la categoria, questo dice il dettaglio che serve per decidere se il
        # pendolarismo e' sostenibile.
        self.dettaglio_modalita = dettaglio_modalita if dettaglio_modalita else ""

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
            "ral": self.ral,
            "dettaglio_modalita": self.dettaglio_modalita,
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
            data.get("testo_completo", ""), data.get("ral", ""),
            data.get("dettaglio_modalita", ""),
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
                                                       date=self._data_da_ref(link),
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

    @staticmethod
    def _data_da_ref(link):
        """Data di pubblicazione ricavata dal riferimento nell'URL, es.
        ".../ref/jn-052026-7024452" -> maggio 2026. Il percorso di fallback HTML
        di questo portale non espone alcuna data (le pagine categoria non hanno
        JSON-LD, verificato dal vivo), quindi senza questo il filtro di
        freschezza non potrebbe mai applicarsi a MichaelPage — che è proprio il
        portale su cui si sono osservati annunci di mesi prima presentati come
        nuovi. Ritorna "" se il ref manca o ha un formato inatteso: nessuna data
        significa nessun filtro, mai uno scarto.

        Si restituisce l'ULTIMO giorno del mese, non il primo: il ref dà solo la
        precisione del mese, e approssimare al primo giorno gonfia l'età fino a
        30 giorni: nel test dell'08/09/2026 tutti gli annunci di agosto
        risultavano "di 38 giorni" e venivano scartati, mentre un annuncio del
        30 agosto ne aveva 9. Con la fine del mese la stima dell'età è sempre la
        più prudente possibile, quindi il filtro non scarta mai un annuncio che
        potrebbe essere recente — continua invece a tagliare i mesi davvero
        vecchi, che è ciò che serve.
        """
        match = re.search(r"/ref/jn-(\d{2})(\d{4})-", link or "")
        if not match:
            return ""
        mese, anno = int(match.group(1)), int(match.group(2))
        if not (1 <= mese <= 12):
            return ""
        ultimo_giorno = calendar.monthrange(anno, mese)[1]
        try:
            data = datetime(anno, mese, ultimo_giorno).date()
        except ValueError:
            return ""
        # Mai una data futura: per il mese CORRENTE la fine del mese non è ancora
        # arrivata, e in email compariva "Data: 2026-09-30" su un'offerta ricevuta
        # l'8 settembre (visto nella mail reale dell'08/09). Si taglia a oggi: la
        # stima dell'età resta la più prudente possibile e la data mostrata resta
        # una data plausibile di pubblicazione.
        oggi = datetime.now().date()
        return min(data, oggi).isoformat()

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
        # ricerche-in-corso.php, NON posizioni-aperte-in-iqmselezione.php:
        # quest'ultima (usata fin qui) è la pagina delle posizioni aperte DENTRO
        # IQM stessa e contiene un solo annuncio, mentre l'elenco delle ricerche
        # svolte per i clienti — quello che serve — sta su ricerche-in-corso.php.
        # Verificato dal vivo l'08/09/2026: 196 annunci contro 1.
        url = "https://www.iqmselezione.it/ricerche-in-corso.php"
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


class AntalScraper(BaseScraper):
    """Antal International — API nascosta POST /_sf/api/v1/jobs/search.json
    (piattaforma SourceFlow, la stessa che serve le pagine del sito). Trovata
    analizzando i bundle JS: i dati embedded staticamente nel bundle erano
    incompleti (11 annunci Italia contro i 79 mostrati dall'interfaccia) e
    NON vengono usati — questa è la vera API che il sito chiama dal vivo.
    location={"address": "Italy"} filtra correttamente per paese (verificato:
    100% dei risultati con region_code="IT" nel campo derived_info) — un
    country/region esplicito nel payload dà invece "unpermitted parameter".
    jobs_per_page è ignorato oltre 50 (la API tronca comunque a 50/richiesta),
    quindi si pagina con offset finché non si raggiunge total_size.
    La città di ogni annuncio si legge da
    derived_info.locations[0].postal_address.locality quando presente — non
    sempre: alcuni annunci sono specificati solo a livello Paese
    (location_type="COUNTRY"), in quel caso restano "Italia" come per
    MichaelPage/GiGroup/IQMSelezione/PRAXI. Antal usa a volte il nome inglese
    della città (es. "Milan") a volte l'italiano ("Milano") per lo stesso
    annuncio: la mappa alias sotto normalizza entrambi.
    La risposta include già la description completa in HTML: viene ripulita
    e passata direttamente come testo per lo scoring, senza bisogno di
    riscaricare la pagina di dettaglio (diversamente da quasi tutti gli
    altri scraper). Gira una sola volta durante l'iterazione Genova.
    """
    _ALIAS_CITTA = {
        "genova": "Genova", "genoa": "Genova",
        "milano": "Milano", "milan": "Milano",
        "torino": "Torino", "turin": "Torino",
    }

    def __init__(self):
        super().__init__("Antal")

    def scrape(self, city_name, city_config):
        jobs = []
        if city_name != "Genova":
            return []
        url = "https://www.antal.com/_sf/api/v1/jobs/search.json"
        headers = {
            "User-Agent": USER_AGENT_CHROME,
            "Content-Type": "application/json",
        }
        seen = set()
        offset = 0
        MAX_JOBS = 200  # tetto di sicurezza sulla paginazione
        try:
            while offset < MAX_JOBS:
                payload = {
                    "job_search": {
                        "query": "",
                        "location": {"address": "Italy"},
                        "filters": {},
                        "commute_filter": {},
                        "offset": offset,
                        "jobs_per_page": 50,
                    }
                }
                response = requests.post(url, headers=headers, json=payload, timeout=15)
                logging.info(f"{self.portal_name} (offset={offset}): HTTP {response.status_code}")
                if response.status_code != 200:
                    logging.error(f"{self.portal_name}: HTTP {response.status_code}")
                    break
                data = response.json()
                results = data.get("results", [])
                total = data.get("total_size", len(results))
                if not results:
                    break
                for item in results:
                    try:
                        job = item.get("job", {})
                        title = _safe_str(job, "title")
                        if not title or not is_valid_job_title(title):
                            continue
                        url_slug = job.get("url_slug", "")
                        if not url_slug:
                            continue
                        link = f"https://www.antal.com/job-search/{url_slug}"
                        if link in seen:
                            continue
                        seen.add(link)

                        locations = (job.get("derived_info") or {}).get("locations") or []
                        locality = ""
                        if locations:
                            locality = (locations[0].get("postal_address") or {}).get("locality", "") or ""
                        job_city = self._ALIAS_CITTA.get(locality.lower().strip(), "Italia")

                        desc_html = _safe_str(job, "description")
                        desc_text = BeautifulSoup(desc_html, "html.parser").get_text(" ", strip=True) if desc_html else ""
                        # published_at/created_at non sono sempre stringhe ISO: alcuni
                        # annunci (verificato dal vivo) li espongono come timestamp Unix
                        # in secondi (int) invece che come stringa — ScrapedJob si aspetta
                        # sempre una stringa (chiama .strip() su date), quindi va convertito.
                        date_raw = job.get("published_at") or job.get("created_at") or ""
                        if isinstance(date_raw, (int, float)):
                            try:
                                date = datetime.fromtimestamp(date_raw).strftime("%Y-%m-%d")
                            except Exception:
                                date = ""
                        else:
                            date = str(date_raw)

                        match_level, match_count, work_mode, fetch_status, probabilita, motivazione, testo_completo = calcola_punteggio_e_modalita(link, desc_text)
                        jobs.append(ScrapedJob(title, "", self.portal_name, link, date=date,
                                               snippet=desc_text[:150] + "..." if desc_text else "",
                                               match_level=match_level, match_count=match_count,
                                               city=job_city, work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione, testo_completo=testo_completo))
                    except Exception as e:
                        logging.error(f"{self.portal_name}: annuncio scartato per errore di parsing: {e}")
                offset += len(results)
                if offset >= total:
                    break
                time.sleep(1)
            if not jobs:
                logging.info(f"{self.portal_name}: 0 offerte valide trovate dopo i filtri.")
        except requests.exceptions.Timeout:
            logging.error(f"{self.portal_name}: timeout della richiesta")
        except Exception as e:
            logging.error(f"Errore scraping {self.portal_name}: {e}")
        return jobs


class HaysScraper(BaseScraper):
    """Hays Italia — Angular Universal, pagina di ricerca renderizzata
    server-side (contenuto reale già nell'HTML iniziale, verificato dal vivo:
    "Plant Manager"/"Fornovo" compaiono nell'HTML grezzo, non serve eseguire
    JavaScript). L'app chiama poi un'API interna autenticata
    (moat.hays.com/.../hla/int/s/.../master/browse/v1/jobs, richiede un
    token via un endpoint separato) per paginazione/filtri lato client —
    troppo complessa da replicare in modo affidabile, quindi non viene usata:
    si legge invece direttamente l'HTML SSR, come per la maggior parte degli
    altri scraper di questo file.

    Limite noto, accettato: la pagina SSR mostra sempre e solo i primi 10
    risultati (verificato dal vivo: nessun parametro page/pageSize/size/
    count/limit/resultsPerPage cambia questo numero) — a differenza degli
    altri portali qui non c'è paginazione accessibile senza l'API
    autenticata. Per questo si combina "locationf" (filtro città ESCLUSIVO,
    verificato dal vivo: 10/10 risultati sempre della città richiesta, a
    differenza di "location" che fa solo boosting mescolato a risultati
    generici) con ciascuna keyword di SEARCH_KEYWORDS come "q", così i 10
    slot disponibili per città sono già mirati ai ruoli target invece che
    ai primi 10 annunci generici di quella città.
    """
    def __init__(self):
        super().__init__("Hays")

    def scrape(self, city_name, city_config):
        jobs = []
        url = "https://www.hays.it/ricerca-offerte"
        headers = {
            "User-Agent": USER_AGENT_CHROME,
            "Accept-Language": "it-IT,it;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        seen = set()
        for kw in SEARCH_KEYWORDS:
            try:
                params = {"q": kw, "locationf": city_name}
                response = requests.get(url, params=params, headers=headers, timeout=12)
                logging.info(f"{self.portal_name} ({kw} - {city_name}): HTTP {response.status_code}")
                # Anche 301 è un body valido con le card reali (verificato dal vivo:
                # nessun header Location, il body è pagina HTML completa identica a
                # una risposta 200 — sembra un artefatto della CDN/cache di Hays, non
                # un vero redirect). Trattarlo come errore avrebbe scartato metà delle
                # risposte reali osservate durante il test.
                if response.status_code in (200, 301):
                    soup = BeautifulSoup(response.text, "html.parser")
                    for card in soup.select("div.job-listing div.job-container"):
                        try:
                            a = card.select_one("div.job-descp h3 a")
                            if not a:
                                continue
                            title = a.get_text(strip=True)
                            if not title or not is_valid_job_title(title):
                                continue
                            href = a.get("href", "")
                            if not href:
                                continue
                            link = href.split("?")[0]
                            if link in seen:
                                continue
                            seen.add(link)
                            snippet_elem = card.select_one("div.job-descp p")
                            snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""
                            match_level, match_count, work_mode, fetch_status, probabilita, motivazione, testo_completo = calcola_punteggio_e_modalita(link, snippet)
                            jobs.append(ScrapedJob(title, "", self.portal_name, link,
                                                   snippet=snippet[:150] + "..." if snippet else "",
                                                   match_level=match_level, match_count=match_count,
                                                   city=city_name, work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione, testo_completo=testo_completo))
                        except Exception as e:
                            logging.error(f"{self.portal_name}: annuncio scartato per errore di parsing: {e}")
                else:
                    logging.error(f"{self.portal_name} ({kw}): HTTP {response.status_code}")
            except requests.exceptions.Timeout:
                logging.error(f"{self.portal_name} ({kw}): timeout della richiesta")
            except Exception as e:
                logging.error(f"Errore scraping {self.portal_name} ({kw}): {e}")
            time.sleep(2)
        if not jobs:
            logging.info(f"{self.portal_name}: 0 offerte valide trovate dopo i filtri.")
        return jobs


class ReverseGroupScraper(BaseScraper):
    """Reverse Group — React SSR (Vite, dati reali già nell'HTML iniziale,
    verificato dal vivo). Piattaforma PAN-EUROPEA (Italia, Spagna, Francia
    osservate direttamente negli annunci) senza alcun filtro geografico
    testuale funzionante: "location"/"q"/"city"/"country" come query param
    non cambiano mai i risultati — la ricerca per città nel sito usa Google
    Places (chiave Maps API vista nell'HTML) per convertire il testo in
    coordinate, meccanismo non replicabile in modo affidabile senza chiave
    propria. "distance" (raggio) esiste ma senza coordinate non filtra nulla.

    Per questo si scarica in sequenza fino a MAX_PAGES pagine (10 annunci
    ciascuna, nessun parametro pageSize/perPage/limit/itemsPerPage le
    aumenta, verificato dal vivo) e si filtra lato client — sia per titolo
    che per città. La città si legge dall'ultima voce della lista dettagli
    di ogni card (icona location): se corrisponde a una delle 3 città
    target la si usa, se è letteralmente "Italy"/"Italia" (annunci
    nazionali/remoti) si etichetta "Italia" — QUALSIASI altra città viene
    scartata invece di ricadere su "Italia" come per gli altri scraper
    nazionali: qui il fallback sarebbe pericoloso, dato che potrebbe
    trattarsi di un annuncio spagnolo o francese (osservato dal vivo:
    "Barcelona", "Paris" compaiono nello stesso flusso di risultati). Limite
    noto e accettato: con 441 annunci totali su più paesi e nessun filtro
    geografico, un tetto di pagine ragionevole non garantisce di vedere
    tutti gli annunci italiani ad ogni run — si affida alla ripetizione dei
    run (4-6 al giorno) e alla deduplica per accumulare copertura nel tempo.
    Gira una sola volta durante l'iterazione Genova (nessun filtro città
    server-side da sfruttare comunque).
    """
    MAX_PAGES = 15  # tetto di sicurezza: 150 annunci scansionati per run

    def __init__(self):
        super().__init__("ReverseGroup")

    def scrape(self, city_name, city_config):
        jobs = []
        if city_name != "Genova":
            return []
        url = "https://public.reversegroup.hr/jobs"
        headers = {
            "User-Agent": USER_AGENT_CHROME,
            "Accept-Language": "it-IT,it;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        citta_lookup = {c.lower(): c for c in CITIES.keys()}
        seen = set()
        for page in range(1, self.MAX_PAGES + 1):
            try:
                params = {"currentPage": page, "distance": 30}
                response = requests.get(url, params=params, headers=headers, timeout=15)
                logging.info(f"{self.portal_name} (pagina {page}): HTTP {response.status_code}")
                if response.status_code != 200:
                    logging.error(f"{self.portal_name}: HTTP {response.status_code}")
                    break
                soup = BeautifulSoup(response.text, "html.parser")
                cards = soup.find_all(class_="app-jobs-search-list-item")
                if not cards:
                    break
                for card in cards:
                    try:
                        h3 = card.select_one("h3")
                        a = card.select_one("a.app-jobs-search-list-item__link") or card.find("a")
                        if not h3 or not a:
                            continue
                        title = h3.get_text(strip=True)
                        if not title or not is_valid_job_title(title):
                            continue
                        href = a.get("href", "")
                        if not href:
                            continue
                        link = href if href.startswith("http") else "https://public.reversegroup.hr" + href
                        if link in seen:
                            continue
                        seen.add(link)

                        recap_items = card.select("ul.app-job-details-recap li")
                        citta_testo = recap_items[-1].get_text(strip=True) if recap_items else ""
                        citta_lower = citta_testo.lower().strip()
                        job_city = citta_lookup.get(citta_lower)
                        if job_city is None:
                            if citta_lower in ("italy", "italia"):
                                job_city = "Italia"
                            else:
                                # Città non tra le 3 target e non genericamente "Italia":
                                # potrebbe essere un annuncio di un altro paese (Spagna,
                                # Francia osservati dal vivo sulla stessa piattaforma) —
                                # scartato invece di rischiare un'etichetta sbagliata.
                                continue

                        desc_elem = card.select_one("div.app-jobs-search-list-item__description p")
                        desc_text = desc_elem.get_text(" ", strip=True) if desc_elem else ""

                        match_level, match_count, work_mode, fetch_status, probabilita, motivazione, testo_completo = calcola_punteggio_e_modalita(link, desc_text)
                        jobs.append(ScrapedJob(title, "", self.portal_name, link,
                                               snippet=desc_text[:150] + "..." if desc_text else "",
                                               match_level=match_level, match_count=match_count,
                                               city=job_city, work_mode=work_mode, fetch_status=fetch_status, probabilita=probabilita, motivazione=motivazione, testo_completo=testo_completo))
                    except Exception as e:
                        logging.error(f"{self.portal_name}: annuncio scartato per errore di parsing: {e}")
                if len(cards) < 10:
                    break
            except requests.exceptions.Timeout:
                logging.error(f"{self.portal_name}: timeout della richiesta (pagina {page})")
                break
            except Exception as e:
                logging.error(f"Errore scraping {self.portal_name} (pagina {page}): {e}")
                break
            time.sleep(1)
        if not jobs:
            logging.info(f"{self.portal_name}: 0 offerte valide trovate dopo i filtri.")
        return jobs


class AdamiScraper(BaseScraper):
    """Adami & Associati — WordPress/Elementor, SSR.

    RISCRITTO il 08/09/2026: il sito è stato ristrutturato e la vecchia
    implementazione trovava 0 annunci su ogni città (verificato dal vivo:
    `article.posizioni_aperte`, la classe su cui si basava, non esiste più
    nell'HTML — restituisce 0 elementi su tutte e tre le città). Ogni annuncio
    è ora un singolo `<a class="jc">` che contiene `<h3>` (titolo),
    `<span class="jst">` (settore) e `<span class="jm">` con due `<span>`
    annidati (città e regione).

    Anche l'archivio di tassonomia per città (/localita_posizioni/{slug}/) NON
    filtra più: verificato dal vivo che restituisce lo stesso identico elenco
    nazionale della pagina generica (annunci di Roma, Treviso, Parma, Verona
    presenti nella pagina "milano"). Si scarica quindi UNA sola volta la
    pagina nazionale e si filtra sulla città letta dalla card, che è il dato
    reale. Nessuna paginazione server-side disponibile: /page/2/ e ?paged=2
    restituiscono entrambi la stessa prima pagina (30 annunci totali).
    """
    def __init__(self):
        super().__init__("Adami")

    def scrape(self, city_name, city_config):
        jobs = []
        url = "https://www.adamiassociati.com/posizioni_aperte/"
        headers = {
            "User-Agent": USER_AGENT_CHROME,
            "Accept-Language": "it-IT,it;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        try:
            response = requests.get(url, headers=headers, timeout=15)
            logging.info(f"{self.portal_name} ({city_name}): HTTP {response.status_code}")
            if response.status_code != 200:
                logging.error(f"{self.portal_name}: HTTP {response.status_code}")
                return jobs
            soup = BeautifulSoup(response.text, "html.parser")
            cards = soup.find_all("a", class_="jc", href=True)
            if not cards:
                logging.error(f"{self.portal_name}: nessuna card 'a.jc' trovata — struttura del sito probabilmente cambiata di nuovo.")
                return jobs
            seen = set()
            for card in cards:
                try:
                    h3 = card.find("h3")
                    if not h3:
                        continue
                    title = h3.get_text(strip=True)
                    if not title or not is_valid_job_title(title):
                        continue
                    # Città della card: primo <span> dentro span.jm (il secondo
                    # è la regione). Si confronta con la città del ciclo perché
                    # la pagina è nazionale.
                    jm = card.find("span", class_="jm")
                    citta_card = ""
                    if jm:
                        inner = jm.find("span")
                        if inner:
                            citta_card = inner.get_text(strip=True)
                    if citta_card.strip().lower() != city_name.lower():
                        continue
                    link = card["href"].split("?")[0]
                    if link in seen:
                        continue
                    seen.add(link)
                    settore_elem = card.find("span", class_="jst")
                    snippet = settore_elem.get_text(strip=True) if settore_elem else ""
                    match_level, match_count, work_mode, fetch_status, probabilita, motivazione, testo_completo = calcola_punteggio_e_modalita(link, snippet)
                    jobs.append(ScrapedJob(title, "", self.portal_name, link,
                                           snippet=snippet[:150],
                                           match_level=match_level, match_count=match_count,
                                           city=city_name, work_mode=work_mode, fetch_status=fetch_status,
                                           probabilita=probabilita, motivazione=motivazione, testo_completo=testo_completo))
                except Exception as e:
                    logging.error(f"{self.portal_name}: annuncio scartato per errore di parsing: {e}")
        except requests.exceptions.Timeout:
            logging.error(f"{self.portal_name} ({city_name}): timeout della richiesta")
        except Exception as e:
            logging.error(f"Errore scraping {self.portal_name} ({city_name}): {e}")
        if not jobs:
            logging.info(f"{self.portal_name}: 0 offerte valide trovate dopo i filtri.")
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

def _prob_ordinabile(job):
    """int(job.probabilita) con fallback a 0: un record malformato/legacy con
    probabilita non numerica (round-trip JSON) non deve far crashare con
    TypeError l'ordinamento e quindi l'intero invio email."""
    try:
        return int(job.probabilita)
    except (TypeError, ValueError):
        return 0


def _fascia_probabilita(prob):
    """(etichetta testuale, colore HTML) per la fascia di probabilità."""
    if prob >= 75:
        return "ALTA", "#1a7f37"
    if prob >= 50:
        return "MEDIA", "#9a6700"
    return "BASSA", "#b42318"


def _prepara_allegati_cv(offerte_ordinate):
    """Genera i CV personalizzati per le offerte sopra soglia, entro il budget di
    tempo, e ritorna (allegati, info_per_offerta).

    Separato dal rendering del corpo email perché ora le versioni testo e HTML
    sono due: generare i CV dentro il loop di rendering, com'era prima, li
    avrebbe generati due volte (due chiamate LLM a pagamento per offerta).
    `info_per_offerta` è indicizzato per id() dell'oggetto job, chiave stabile
    e priva di collisioni finché la lista resta viva nel chiamante.

    Le offerte arrivano già ordinate per punteggio decrescente, quindi se il
    budget di tempo si esaurisce a essere tagliate fuori sono le offerte con
    match più basso, non quelle arrivate per ultime da uno scraper qualsiasi.
    """
    allegati = []
    info = {}
    if not CV_PERSONALIZZAZIONE_DISPONIBILE:
        return allegati, info

    # Ogni CV personalizzato può richiedere fino a due chiamate LLM sequenziali
    # (proposta + verifica) di diversi minuti ciascuna: con più offerte >=80% lo
    # stesso giorno il tempo si somma senza limite. Questo budget evita che l'invio
    # email si blocchi per troppo tempo — oltre la soglia, le offerte restanti
    # vengono comunque incluse nell'email ma senza CV personalizzato allegato.
    scadenza = time.monotonic() + CV_PERSONALIZZAZIONE_BUDGET_SECONDI
    budget_esaurito_loggato = False

    for job in offerte_ordinate:
        prob = _prob_ordinabile(job)
        if prob < SOGLIA_CV_PERSONALIZZATO:
            continue
        if time.monotonic() >= scadenza:
            if not budget_esaurito_loggato:
                logging.warning(
                    f"Budget di tempo per la personalizzazione CV esaurito "
                    f"({CV_PERSONALIZZAZIONE_BUDGET_SECONDI}s): le offerte >= {SOGLIA_CV_PERSONALIZZATO}% "
                    f"restanti vengono incluse nell'email senza CV personalizzato allegato."
                )
                budget_esaurito_loggato = True
            continue
        try:
            risultato_cv = genera_cv_per_offerta(job.title, job.link, job_city=job.city, job_text=job.testo_completo)
        except Exception as e:
            logging.error(f"Errore imprevisto personalizzazione CV per '{job.title}': {e}")
            continue
        docx_path = risultato_cv.get("docx_path") if risultato_cv else None
        if risultato_cv and docx_path:
            indice = len(allegati) + 1
            nome_file = f"CV_Ghigliotti_{indice}_{re_sub_nome_file(job.company)}.docx"
            allegati.append({"docx_path": docx_path, "nome_file": nome_file})
            info[id(job)] = {"indice": indice, "riepilogo": risultato_cv.get("riepilogo", [])}
        elif risultato_cv:
            # risultato_cv presente ma senza docx_path: forma inattesa, non deve
            # mai far crashare invia_email (perderebbe l'intera email del giorno).
            logging.error(f"genera_cv_per_offerta ha ritornato una forma inattesa per '{job.title}': {risultato_cv!r}")
    return allegati, info


def _carica_prospects():
    """Prospect del giorno da daily_prospects.json. Il file viene solo letto qui,
    MAI svuotato: se l'invio fallisse dopo aver già svuotato il file, i prospect
    andrebbero persi senza essere mai stati recapitati. Lo svuotamento avviene
    solo dopo un invio SMTP riuscito."""
    percorso = "daily_prospects.json"
    if not os.path.exists(percorso):
        return []
    try:
        with open(percorso, "r", encoding="utf-8") as f:
            return json.load(f) or []
    except Exception as e:
        logging.error(f"Errore caricamento prospect: {e}")
        return []


def _corpo_testo(offerte_ordinate, offerte_per_citta, info_cv, prospects, sospetti, gia_candidato, ripubblicate):
    """Versione testo semplice del corpo email: fallback per i client che non
    renderizzano HTML, e copia leggibile del contenuto."""
    body = ""
    if not offerte_ordinate:
        body += "Nessuna nuova offerta oggi.\n\n"
    else:
        body += f"Trovate {len(offerte_ordinate)} nuove offerte oggi, ordinate per affinità col CV:\n\n"

        top = offerte_ordinate[:TOP_OFFERTE_IN_EVIDENZA]
        if len(offerte_ordinate) > len(top):
            body += "===============================\n"
            body += f"LE {len(top)} DA GUARDARE PER PRIME\n"
            body += "===============================\n\n"
            for i, job in enumerate(top, 1):
                prob = _prob_ordinabile(job)
                etichetta, _ = _fascia_probabilita(prob)
                body += f"{i}. [{prob}% {etichetta}] {job.title} - {job.company} ({job.city})\n"
                body += f"   {job.link}\n"
            body += "\n"

        for citta, offerte_citta in offerte_per_citta.items():
            body += "===============================\n"
            body += f"{citta.upper()} ({len(offerte_citta)} offerte)\n"
            body += "===============================\n\n"

            for i, job in enumerate(offerte_citta, 1):
                prob = _prob_ordinabile(job)
                etichetta, _ = _fascia_probabilita(prob)
                body += f"{i}. {job.title}\n"
                body += f"   Azienda: {job.company}\n"
                body += f"   Città: {job.city}\n"
                modalita_display = "Modalità non specificata nell'annuncio" if job.work_mode == "unverified" else job.work_mode.upper()
                if job.dettaglio_modalita:
                    modalita_display += f" ({job.dettaglio_modalita})"
                body += f"   Modalità: {modalita_display}\n"
                if job.ral:
                    body += f"   Retribuzione: {job.ral}\n"
                body += f"   Portale: {job.portal}\n"
                body += f"   Probabilità richiamata: {prob}% - {etichetta}\n"
                body += f"   -> {job.motivazione}\n"
                body += f"   Match CV: {job.match_level} ({job.match_count} keyword)\n"
                body += f"   Data: {job.date}\n"
                body += f"   Link: {job.link}\n"
                if job.snippet:
                    body += f"   Snippet: {job.snippet}\n"
                prima_volta = ripubblicate.get((_pulisci_per_segnatura(job.title),
                                                _pulisci_per_segnatura(job.company)))
                if prima_volta:
                    body += (f"   ATTENZIONE: gia' proposta il {prima_volta} - annuncio ripubblicato, "
                             f"la data qui sopra e' quella della ripubblicazione, non dell'apertura\n")
                precedente = gia_candidato.get(get_job_id(job.link))
                if precedente:
                    body += (f"   ATTENZIONE: già in candidature dal {precedente.get('data', '?')} "
                             f"(stato: {precedente.get('stato', '?')})\n")
                dati_cv = info_cv.get(id(job))
                if dati_cv:
                    body += (f"   CV personalizzato allegato in Word (allegato {dati_cv['indice']}) - "
                             f"apri in Word ed esporta in PDF prima di candidarti. Modifiche:\n")
                    for riga in dati_cv["riepilogo"]:
                        body += f"      - {riga}\n"
                body += "\n"

    body += f"Totale offerte: {len(offerte_ordinate)}.\n\n"

    if sospetti:
        body += "=========================================================\n"
        body += "PORTALI DA CONTROLLARE (nessun risultato da giorni)\n"
        body += "=========================================================\n"
        for riga in sospetti:
            body += f"  - {riga}\n"
        body += "\n"

    if prospects:
        body += "=========================================================\n"
        body += f"COMPANY PROSPECTOR: {len(prospects)} AZIENDE TARGET SELEZIONATE OGGI\n"
        body += "=========================================================\n\n"
        for p in prospects:
            body += f"Azienda: {p.get('company', 'N/D')}\n"
            body += f"Città: {p.get('city', 'N/D')}\n"
            body += f"Settore: {p.get('sector', 'N/D')}\n"
            body += f"Lavora con noi: {p.get('career_url', 'N/D')}\n"
            body += f"Candidatura Spontanea: {p.get('spontaneous_application', 'N/D')}\n"
            body += f"Contatto Chiave (LinkedIn): {p.get('key_person', 'N/D')}\n"
            body += "---------------------------------------------------------\n\n"
    return body


def _corpo_html(offerte_ordinate, offerte_per_citta, info_cv, prospects, sospetti, gia_candidato, ripubblicate, data_oggi):
    """Versione HTML del corpo email: titolo cliccabile, punteggio a colpo
    d'occhio e una sezione "da guardare per prime" in cima.
    Con decine di offerte al giorno il testo semplice diventa una parete in cui
    le offerte migliori restano sepolte a metà elenco.
    Stili inline e nessun <style>: i client email, Gmail in testa, rimuovono o
    ignorano i fogli di stile nel <head>."""
    def esc(valore):
        return html_lib.escape(str(valore or ""))

    font = "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"
    parti = [
        f'<div style="{font};font-size:14px;color:#1f2328;max-width:760px;margin:0 auto;padding:8px">',
        f'<h1 style="font-size:19px;margin:0 0 4px">Nuove offerte &mdash; {esc(data_oggi)}</h1>',
    ]

    if not offerte_ordinate:
        parti.append('<p style="color:#59636e">Nessuna nuova offerta oggi.</p>')
    else:
        parti.append(f'<p style="color:#59636e;margin:0 0 18px">{len(offerte_ordinate)} offerte, ordinate per affinità col CV.</p>')

        top = offerte_ordinate[:TOP_OFFERTE_IN_EVIDENZA]
        if len(offerte_ordinate) > len(top):
            parti.append(f'<h2 style="font-size:15px;margin:22px 0 8px">Le {len(top)} da guardare per prime</h2>')
            parti.append('<ol style="margin:0 0 20px;padding-left:22px">')
            for job in top:
                prob = _prob_ordinabile(job)
                _, colore = _fascia_probabilita(prob)
                parti.append(
                    f'<li style="margin-bottom:7px">'
                    f'<b style="color:{colore}">{prob}%</b> '
                    f'<a href="{esc(job.link)}" style="color:#0969da;text-decoration:none">{esc(job.title)}</a>'
                    f'<span style="color:#59636e"> &mdash; {esc(job.company)} ({esc(job.city)})</span>'
                    f'</li>'
                )
            parti.append('</ol>')

        for citta, offerte_citta in offerte_per_citta.items():
            parti.append(
                f'<h2 style="font-size:15px;margin:26px 0 10px;padding-bottom:5px;'
                f'border-bottom:2px solid #d1d9e0">{esc(citta.upper())} '
                f'<span style="color:#59636e;font-weight:normal">({len(offerte_citta)})</span></h2>'
            )
            for job in offerte_citta:
                prob = _prob_ordinabile(job)
                etichetta, colore = _fascia_probabilita(prob)
                modalita = "modalità non specificata" if job.work_mode == "unverified" else job.work_mode
                if job.dettaglio_modalita:
                    modalita += f" ({job.dettaglio_modalita})"
                parti.append(
                    f'<div style="border:1px solid #d1d9e0;border-left:4px solid {colore};'
                    f'border-radius:6px;padding:12px 14px;margin-bottom:12px">'
                )
                parti.append(
                    f'<div style="font-size:15px;margin-bottom:3px">'
                    f'<a href="{esc(job.link)}" style="color:#0969da;text-decoration:none;font-weight:600">{esc(job.title)}</a>'
                    f'</div>'
                )
                parti.append(
                    f'<div style="color:#59636e;margin-bottom:8px">{esc(job.company)} &middot; '
                    f'{esc(modalita)} &middot; {esc(job.portal)} &middot; {esc(job.date)}</div>'
                )
                parti.append(
                    f'<div style="margin-bottom:6px"><b style="color:{colore}">{prob}% {esc(etichetta)}</b>'
                    f'<span style="color:#59636e"> &middot; match CV {esc(job.match_level)} '
                    f'({job.match_count} keyword)</span></div>'
                )
                if job.ral:
                    parti.append(f'<div style="margin-bottom:6px"><b>Retribuzione:</b> {esc(job.ral)}</div>')
                if job.motivazione:
                    parti.append(f'<div style="margin-bottom:6px">{esc(job.motivazione)}</div>')
                if job.snippet:
                    parti.append(f'<div style="color:#59636e;font-size:13px">{esc(job.snippet)}</div>')
                prima_volta = ripubblicate.get((_pulisci_per_segnatura(job.title),
                                                _pulisci_per_segnatura(job.company)))
                if prima_volta:
                    parti.append(
                        f'<div style="margin-top:8px;padding:7px 9px;background:#fff8c5;'
                        f'border-radius:5px;font-size:13px">Gi&agrave; proposta il {esc(prima_volta)} '
                        f'&mdash; annuncio ripubblicato: la data sopra &egrave; quella della '
                        f'ripubblicazione, non dell&rsquo;apertura della posizione.</div>'
                    )
                precedente = gia_candidato.get(get_job_id(job.link))
                if precedente:
                    parti.append(
                        f'<div style="margin-top:8px;padding:7px 9px;background:#fff8c5;'
                        f'border-radius:5px;font-size:13px">Gi&agrave; in candidature dal '
                        f'{esc(precedente.get("data", "?"))} (stato: {esc(precedente.get("stato", "?"))})</div>'
                    )
                dati_cv = info_cv.get(id(job))
                if dati_cv:
                    righe = "".join(f"<li>{esc(r)}</li>" for r in dati_cv["riepilogo"])
                    parti.append(
                        f'<div style="margin-top:8px;padding:8px 10px;background:#ddf4ff;'
                        f'border-radius:5px;font-size:13px"><b>CV personalizzato allegato '
                        f'(allegato {dati_cv["indice"]})</b> &mdash; apri in Word ed esporta in PDF '
                        f'prima di candidarti.<ul style="margin:6px 0 0;padding-left:18px">{righe}</ul></div>'
                    )
                parti.append('</div>')

    if sospetti:
        righe = "".join(f"<li>{esc(r)}</li>" for r in sospetti)
        parti.append(
            f'<div style="margin-top:26px;padding:12px 14px;background:#fff8c5;'
            f'border-radius:6px"><b>Portali da controllare</b> (nessun risultato da giorni)'
            f'<ul style="margin:6px 0 0;padding-left:20px">{righe}</ul></div>'
        )

    if prospects:
        parti.append(f'<h2 style="font-size:15px;margin:26px 0 10px">Aziende target di oggi ({len(prospects)})</h2>')
        for p in prospects:
            parti.append(
                f'<div style="border:1px solid #d1d9e0;border-radius:6px;padding:11px 13px;margin-bottom:10px">'
                f'<b>{esc(p.get("company", "N/D"))}</b>'
                f'<div style="color:#59636e">{esc(p.get("city", "N/D"))} &middot; {esc(p.get("sector", "N/D"))}</div>'
                f'<div style="margin-top:5px"><a href="{esc(p.get("career_url", ""))}" '
                f'style="color:#0969da">Lavora con noi</a></div>'
                f'<div>{esc(p.get("spontaneous_application", "N/D"))}</div>'
                f'<div>{esc(p.get("key_person", "N/D"))}</div>'
                f'</div>'
            )

    parti.append('</div>')
    return "".join(parti)


# Data dell'ultimo riepilogo giornaliero inviato con successo. Serve a poter
# schedulare MOLTI tentativi di invio senza rischiare piu' email nello stesso
# giorno: il cron di GitHub Actions ritarda i workflow in modo imprevedibile
# (misurati 2-4.6 ore di ritardo sull'orario target su 12 giorni consecutivi),
# quindi l'unico modo per ricevere la mail vicino all'orario voluto e' provarci
# piu' volte e fermarsi al primo tentativo andato a buon fine.
ULTIMO_INVIO_FILE = "ultimo_invio.json"


def email_gia_inviata_oggi() -> bool:
    """True se il riepilogo di oggi e' gia' partito. In caso di file illeggibile
    ritorna False: meglio una mail doppia che nessuna mail."""
    try:
        dati = state_io.load_json_or_raise(ULTIMO_INVIO_FILE, {})
        return isinstance(dati, dict) and dati.get("data") == datetime.now().strftime("%Y-%m-%d")
    except Exception as e:
        logging.warning(f"Errore lettura {ULTIMO_INVIO_FILE}: {e}")
        return False


def registra_invio_email():
    """Segna che il riepilogo di oggi e' stato inviato. Chiamata solo dopo un
    invio SMTP riuscito."""
    try:
        _atomic_write_json(ULTIMO_INVIO_FILE,
                           {"data": datetime.now().strftime("%Y-%m-%d"),
                            "orario": datetime.now().strftime("%H:%M:%S")},
                           ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Errore scrittura {ULTIMO_INVIO_FILE}: {e}")


# Soglia oltre la quale un'offerta merita di essere segnalata SUBITO, senza
# aspettare il riepilogo serale. Alta di proposito: due o tre avvisi al giorno
# restano un segnale, dieci diventano rumore e si smette di aprirli.
SOGLIA_ALERT_IMMEDIATO = 85


def invia_alert_immediato(offerte):
    """Manda subito una mail breve per le offerte sopra soglia, invece di farle
    aspettare fino alle 18:05.

    Il motivo e' il tempismo: lo scraping gira dalle 09:17, quindi un'offerta
    trovata al mattino restava ferma nove ore e la candidatura partiva il giorno
    dopo, quando la selezione ha gia' raccolto centinaia di profili. Per i ruoli
    manageriali le shortlist si formano nei primi giorni, ed essere tra i primi
    conta piu' di qualunque altra ottimizzazione fatta finora.

    Non sostituisce il riepilogo serale: quelle offerte ci compaiono comunque.
    Ritorna il numero di offerte segnalate."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD or not DESTINATION_EMAIL:
        return 0
    da_segnalare = [j for j in offerte if _prob_ordinabile(j) >= SOGLIA_ALERT_IMMEDIATO]
    if not da_segnalare:
        return 0
    da_segnalare.sort(key=_prob_ordinabile, reverse=True)

    if len(da_segnalare) == 1:
        job = da_segnalare[0]
        oggetto = f"[Candidati oggi] {job.title[:60]} — {job.city}"
    else:
        oggetto = f"[Candidati oggi] {len(da_segnalare)} offerte ad alta affinità"

    righe = ["Offerte appena trovate che meritano una candidatura oggi, "
             "senza aspettare il riepilogo di stasera.", ""]
    for job in da_segnalare:
        righe.append(f"{_prob_ordinabile(job)}% — {job.title}")
        righe.append(f"   {job.company} · {job.city} · {job.portal}")
        if job.ral:
            righe.append(f"   Retribuzione: {job.ral}")
        if job.motivazione:
            righe.append(f"   {job.motivazione}")
        righe.append(f"   {job.link}")
        righe.append("")
    righe.append("Le trovi comunque nel riepilogo delle 18:05.")

    msg = MIMEMultipart("mixed")
    msg["From"] = GMAIL_USER
    msg["To"] = DESTINATION_EMAIL
    msg["Subject"] = oggetto
    msg.attach(MIMEText("\n".join(righe), "plain", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        logging.info(f"Alert immediato inviato per {len(da_segnalare)} offerte sopra {SOGLIA_ALERT_IMMEDIATO}%.")
        return len(da_segnalare)
    except Exception as e:
        # Nessun retry e nessuna propagazione: e' una notifica accessoria, le
        # offerte arrivano comunque nel riepilogo serale. Bloccare o rallentare
        # lo scraping per un alert non inviato sarebbe sproporzionato.
        logging.warning(f"Alert immediato non inviato: {type(e).__name__}: {e}")
        return 0


def invia_email(nuove_offerte):
    if not GMAIL_USER or not GMAIL_APP_PASSWORD or not DESTINATION_EMAIL:
        logging.error("Credenziali email mancanti. Controlla il file .env")
        return False

    data_oggi = datetime.now().strftime("%d/%m/%Y")

    # Un solo ordinamento globale, riusato per tre cose: la sezione "da guardare
    # per prime", l'ordine dentro ogni città e l'ordine di generazione dei CV.
    offerte_ordinate = sorted(nuove_offerte, key=_prob_ordinabile, reverse=True)

    offerte_per_citta = {}
    for job in offerte_ordinate:
        offerte_per_citta.setdefault(job.city or "Altro", []).append(job)

    allegati_cv, info_cv = _prepara_allegati_cv(offerte_ordinate)

    try:
        sospetti = portali_sospetti()
    except Exception as e:
        logging.error(f"Errore calcolo salute portali: {e}")
        sospetti = []

    try:
        gia_candidato = candidature_per_job_id()
    except Exception as e:
        logging.error(f"Errore lettura candidature: {e}")
        gia_candidato = {}

    try:
        ripubblicate = segnature_storico()
    except Exception as e:
        logging.error(f"Errore lettura storico per le ripubblicazioni: {e}")
        ripubblicate = {}

    prospects = _carica_prospects()

    testo = _corpo_testo(offerte_ordinate, offerte_per_citta, info_cv, prospects, sospetti, gia_candidato, ripubblicate)
    corpo_html = _corpo_html(offerte_ordinate, offerte_per_citta, info_cv, prospects, sospetti, gia_candidato, ripubblicate, data_oggi)

    # mixed( alternative(plain, html), allegati... ): dentro "alternative" le
    # parti vanno dalla meno preferita alla più preferita, i client mostrano
    # l'ultima che sanno renderizzare.
    msg = MIMEMultipart("mixed")
    msg["From"] = GMAIL_USER
    msg["To"] = DESTINATION_EMAIL
    msg["Subject"] = f"[Job Alert] Nuove offerte Multi-City - {data_oggi}"
    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(testo, "plain", "utf-8"))
    alternative.attach(MIMEText(corpo_html, "html", "utf-8"))
    msg.attach(alternative)

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
            if prospects:
                try:
                    _atomic_write_json("daily_prospects.json", [])
                except Exception as e:
                    logging.error(f"Errore svuotamento daily_prospects.json dopo invio riuscito: {e}")
            # Storico delle offerte effettivamente recapitate: è la base su cui
            # `candidature.py` fa scegliere quali marcare come candidatura, dato
            # che offerte_giornaliere.json viene svuotato subito dopo l'invio.
            try:
                registra_offerte_inviate(offerte_ordinate)
            except Exception as e:
                logging.error(f"Errore aggiornamento storico offerte inviate: {e}")
            registra_invio_email()
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
# Città target dell'utente e loro varianti/provincia, usate per riconoscere la
# sede reale negli annunci dei portali NAZIONALI (MichaelPage, IQMSelezione,
# PRAXI, Antal, ReverseGroup, GiGroup): questi scaricano una pagina unica non
# filtrata per città ed etichettano tutto come city="Italia".
CITTA_TARGET_PATTERN = {
    "Genova": ["genova", "genoa"],
    "Milano": ["milano", "milan ", "milan,", "milan)", "assago", "sesto san giovanni", "rho ", "segrate"],
    "Torino": ["torino", "turin"],
}

# Luoghi italiani NON target: se l'annuncio nazionale nomina solo uno di questi
# e nessuna città target, la sede è quasi certamente altrove e l'offerta è rumore.
#
# Include anche le REGIONI, non solo le città: molti annunci indicano solo l'area
# ("Business Developer (Veneto - Friuli)", osservato dal vivo nel test dell'08/09
# e passato come "Italia" perché la lista conteneva solo nomi di città). È lo
# stesso difetto che in passato faceva arrivare come "Genova" o "Milano" annunci
# che poi risultavano in Emilia-Romagna.
#
# Liguria, Lombardia e Piemonte sono volutamente ASSENTI: sono le regioni delle
# città target, nominarle non è motivo di scarto.
ALTRE_CITTA_ITALIANE = [
    # città
    "roma", "napoli", "firenze", "bologna", "venezia", "verona", "padova",
    "bari", "palermo", "catania", "bergamo", "brescia", "parma", "modena",
    "reggio emilia", "vicenza", "treviso", "trieste", "udine", "ancona",
    "perugia", "pescara", "cagliari", "salerno", "trento", "bolzano",
    "varese", "como", "monza", "novara", "lecco", "pisa", "livorno",
    "barberino", "prato", "arezzo", "siena", "rimini", "ravenna", "forli",
    # Citta' vicine alle tre target: sono quelle che generano lo scambio piu'
    # insidioso, perche' un annuncio a La Spezia o a Savona "sembra" Genova.
    # La Spezia mancava, ed e' costata un falso positivo reale il 09/09/2026:
    # un "Responsabile vendite" di La Spezia recapitato come offerta di Genova.
    "la spezia", "savona", "imperia", "alessandria", "asti", "cuneo",
    "pavia", "cremona", "piacenza", "biella", "vercelli", "aosta",
    # regioni e macro-aree
    "veneto", "friuli", "emilia-romagna", "emilia romagna", "toscana", "lazio",
    "campania", "puglia", "sicilia", "sardegna", "marche", "umbria", "abruzzo",
    "calabria", "basilicata", "molise", "trentino", "alto adige", "sud tirolo",
    "valle d'aosta", "val d'aosta",
]


def rileva_citta_offerta(job):
    """Per un'offerta di un portale nazionale (city="Italia"), cerca nel testo
    integrale già scaricato quale sia la sede reale.
    Ritorna il nome della città target trovata, oppure None se l'annuncio va
    scartato — sia quando nomina chiaramente solo luoghi NON target, sia quando
    la sede non è deducibile affatto.

    Nessun fallback "Italia": su richiesta esplicita dell'utente (09/09) le
    offerte senza sede accertata non arrivano più in email. Un annuncio che
    potrebbe essere ovunque in Italia non è candidabile e costringe comunque ad
    aprire il link per scoprirlo — meno offerte ma affidabili vale più di un
    elenco più lungo da verificare a mano.

    Serve perché questi portali etichettano tutto come "Italia" e le offerte
    arrivavano in email senza alcun controllo geografico: nel test end-to-end
    del 08/09/2026 un "Marketing Manager - Outlet Village Barberino" (Firenze)
    veniva recapitato tra i risultati di Genova."""
    def _cerca(testo):
        """(città target trovata, città non-target trovata) in un testo."""
        target = next((c for c, varianti in CITTA_TARGET_PATTERN.items()
                       if any(v in testo for v in varianti)), None)
        altra = any(c in testo for c in ALTRE_CITTA_ITALIANE)
        return target, altra

    # Prima il titolo (+ snippet): è il segnale ad alta precisione, senza il
    # boilerplate della pagina. Il testo integrale scaricato contiene infatti
    # anche header, footer e i riferimenti alle sedi dell'agenzia: verificato
    # dal vivo che un "Marketing Manager - Outlet Village Barberino" (Firenze)
    # veniva etichettato "Milano" solo perché la pagina MichaelPage nomina
    # Milano nel proprio footer.
    target, altra = _cerca(f"{job.title} {job.snippet}".lower())
    if target:
        return target
    if altra:
        return None

    # Solo se il titolo non dice nulla si guarda il testo integrale, che è più
    # rumoroso: qui una città target vale solo se nessuna città non-target
    # compare, altrimenti non è distinguibile dal boilerplate.
    target, altra = _cerca(job.testo_completo.lower())
    if target and not altra:
        return target
    return None


def citta_coerente_col_testo(job) -> bool:
    """True se la città attribuita all'offerta è compatibile con il suo testo.

    Ritorna False solo quando il testo nomina città diverse da quella attribuita
    e NON nomina quella attribuita: è il caso in cui l'annuncio è quasi
    certamente altrove. Con un testo che non nomina città (o non scaricato) la
    risposta è True, perché non c'è modo di smentire l'attribuzione."""
    testo = f"{job.title} {job.snippet} {job.testo_completo}".lower()
    if not testo.strip():
        return True
    varianti = CITTA_TARGET_PATTERN.get(job.city, [])
    if any(v in testo for v in varianti):
        return True
    return not any(c in testo for c in ALTRE_CITTA_ITALIANE)


def filtra_offerte_per_citta(offerte_scraper, city_config):
    """Filtra le offerte in base alla configurazione della città.
    Genova (filter_hybrid_only=False): accetta in sede e ibrido, esclude da remoto.
    Milano/Torino (filter_hybrid_only=True): accetta solo ibrido (non da remoto, non in sede).
    In entrambi i casi include unverified se INCLUDE_UNVERIFIED=True.
    """
    offerte_filtrate = []
    for job in offerte_scraper:
        # `cfg` locale, non `city_config`: riassegnare il parametro dentro il
        # ciclo lo lascerebbe cambiato anche per tutte le offerte successive
        # dello stesso batch.
        cfg = city_config
        # Coerenza geografica per i portali che assegnano gia' una citta' target
        # (LinkedIn, Wyser, Adami...): quella citta' viene dalla QUERY di ricerca,
        # non dalla sede reale dell'annuncio. LinkedIn in particolare restituisce
        # spesso annunci dei dintorni: verificato dal vivo il 09/09/2026 un
        # "Responsabile vendite" con sede a La Spezia recapitato come offerta di
        # Genova — esattamente il difetto per cui l'utente apriva un annuncio e
        # trovava un'altra regione.
        # Si scarta solo nel caso inequivocabile: la citta' assegnata NON compare
        # nel testo dell'annuncio, ma ne compaiono altre. Se il testo non nomina
        # alcuna citta' (o non e' stato scaricato) l'offerta si tiene, perche'
        # l'assenza di prova non e' prova di assenza.
        if job.city in CITTA_TARGET_PATTERN and not citta_coerente_col_testo(job):
            logging.info(f"Offerta scartata (sede reale diversa da {job.city}): {job.title} — {job.link}")
            continue

        # Freschezza: un annuncio pubblicato mesi fa è quasi sempre una ricerca
        # già chiusa lasciata online. Il controllo sta qui perché questa funzione
        # è il gate comune a entrambi gli entry point (esegui_scraping_job e
        # run_manual_scrape.py), quindi vale per tutti i portali senza doverlo
        # ripetere in ognuno.
        # Se il JSON-LD del dettaglio ha dato una data e lo scraper non ne aveva
        # trovata una nella pagina di elenco, la si porta sull'offerta: serve al
        # filtro qui sotto ma soprattutto la si vede in email, dove finora
        # compariva "Data non disponibile" pur avendo il dato in mano.
        data_reale = data_pubblicazione_effettiva(job)
        if data_reale and data_reale != job.date:
            if not job.date or job.date == "Data non disponibile" or job.portal == "MichaelPage":
                job.date = data_reale

        if offerta_troppo_vecchia(job):
            logging.info(f"Offerta scartata (pubblicata da oltre {MAX_ETA_GIORNI_ANNUNCIO} giorni, "
                         f"data={job.date}): {job.title} — {job.link}")
            continue

        # Portali nazionali: risolvi la sede reale dal testo prima di applicare
        # i filtri di modalità, e scarta ciò che è chiaramente fuori area.
        if job.city == "Italia":
            citta_reale = rileva_citta_offerta(job)
            if citta_reale is None:
                logging.info(f"Offerta scartata (sede fuori dalle città target): {job.title} — {job.link}")
                continue
            job.city = citta_reale
        # La policy work-mode è quella della città REALE dell'offerta, non quella
        # del ciclo. I portali nazionali (MichaelPage, GiGroup, IQMSelezione,
        # PRAXI, Antal, ReverseGroup, LHH) girano tutti durante l'iterazione
        # "Genova" — la più permissiva — quindi un'offerta con sede a Milano o
        # Torino saltava del tutto il filtro "solo ibrido" di quelle città e
        # arrivava in email anche se full time in ufficio. PRAXI, che la sede
        # reale la leggeva già correttamente dalla card, era il caso più visibile.
        if job.city in CITIES:
            cfg = CITIES[job.city]

        if cfg.get("filter_hybrid_only", False):
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
        AntalScraper(),
        HaysScraper(),
        ReverseGroupScraper(),
        AdamiScraper(),
    ]

    tutte_le_offerte = []
    conteggi_grezzi = {s.portal_name: 0 for s in scrapers}
    errori_portali = {}

    for city_name, city_config in CITIES.items():
        print(f"  -> Scraping per città: {city_name}")
        for scraper in scrapers:
            # try/except per singolo portale, come già fa run_manual_scrape.py:
            # senza, un'eccezione non gestita in UNO scraper (es. un sito che
            # cambia struttura) interrompe l'intero run e fa perdere anche le
            # offerte di tutti i portali e le città successive.
            try:
                offerte_scraper = scraper.scrape(city_name, city_config)
            except Exception as e:
                logging.error(f"Portale {scraper.portal_name} fallito su {city_name}, lo salto: {e}")
                print(f"     {scraper.portal_name}: ERRORE — {str(e)[:100]}")
                errori_portali[scraper.portal_name] = f"{type(e).__name__}: {e}"
                continue

            conteggi_grezzi[scraper.portal_name] += len(offerte_scraper)
            # Filtro modalità ibrida/unverified se richiesto dalla città
            tutte_le_offerte.extend(filtra_offerte_per_citta(offerte_scraper, city_config))

    aggiorna_stato_portali(conteggi_grezzi, errori_portali)

    viste = load_viste()  # ora è un set
    nuove_offerte = dedup_offerte(tutte_le_offerte, viste)
    save_viste(viste)

    # Valutazione semantica solo sulle offerte superstiti: qui sono poche e
    # ognuna arrivera' in email, quindi ogni chiamata a pagamento e' spesa bene.
    arricchisci_offerte_con_llm(nuove_offerte)
    invia_alert_immediato(nuove_offerte)

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
