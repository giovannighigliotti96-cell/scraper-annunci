"""
Personalizzazione CV per offerte ad alto match (>=80%).

Attivato solo per le offerte che il match semantico di scraper.py ha già
valutato >=80%. Per queste:
  1. il title sotto il nome diventa il titolo esatto dell'annuncio
  2. il paragrafo PROFILO PROFESSIONALE viene riformulato nello stesso
     framework (struttura di frase) ma calibrato sul job specifico
  3. alcuni bullet delle esperienze vengono riformulati nella sostanza
     esistente (mai aggiunti, mai inventati)

Vincoli non negoziabili:
  - FORMAZIONE/CERTIFICAZIONI, titoli di ruolo/azienda storici e date NON
    vengono mai toccati (sono fatti, non narrativa)
  - ogni modifica proposta deve essere riconducibile a un'esperienza reale
    già presente nel CV originale — una seconda chiamata Claude fa da
    verificatore indipendente e scarta qualunque modifica non verificabile
  - se una qualunque fase fallisce (LLM, LibreOffice, file mancanti) non
    viene generato/allegato nulla — mai un CV non verificato

Il documento sorgente è cv_template/Giovanni Ghigliotti CV___2026.docx.
Gli indici dei paragrafi bullet modificabili sono stati verificati
manualmente il 2026-07-16: l'ordine dei paragrafi nell'XML del docx non
coincide con l'ordine di lettura visivo (il layout a due colonne interlaccia
paragrafi della sidebar sinistra con quelli della colonna destra), quindi
non è affidabile derivarli a runtime da un semplice range di indici.
"""
import os
import re
import json
import shutil
import logging
import subprocess
import tempfile

import docx
from dotenv import load_dotenv

try:
    import anthropic
    _ANTHROPIC_SDK_AVAILABLE = True
except ImportError:
    _ANTHROPIC_SDK_AVAILABLE = False

load_dotenv()

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CV_DOCX_TEMPLATE = os.path.join(_BASE_DIR, "cv_template", "Giovanni Ghigliotti CV___2026.docx")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
MATCH_LLM_MODEL = "claude-sonnet-5"

# Indici verificati manualmente (vedi docstring del modulo).
INDICE_TITLE = 15
INDICE_INTRO = 18
INDICI_BULLET_MODIFICABILI = [
    35, 36, 37,
    48, 49, 50, 51,
    63, 64, 65, 66,
    75, 76, 77, 78, 79, 80, 81,
    88, 89, 90, 91, 92, 93, 94, 95, 96, 97,
]

_client = None

def _get_client():
    global _client
    if _client is None and _ANTHROPIC_SDK_AVAILABLE and ANTHROPIC_API_KEY:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=180.0)
    return _client


# ==========================================
# UTILITY DOCX
# ==========================================
def formatta_title_spaziato(titolo: str) -> str:
    """Riproduce il pattern del title del CV: lettere separate da spazio
    dentro ogni parola, parole separate da tab — es. 'Head of Growth' diventa
    'H e a d\\tof\\tG r o w t h' seguendo esattamente il pattern originale
    ('D i g i t a l\\tS a l e s\\t&\\tM a r k e t i n g\\tM a n a g e r')."""
    parole = titolo.split()
    return "\t".join(" ".join(list(p)) for p in parole)


def _sostituisci_testo_paragrafo(paragraph, nuovo_testo):
    """Sostituisce il testo di un paragrafo mantenendo la formattazione
    (font, size, bold, colore) del primo run esistente."""
    if not paragraph.runs:
        paragraph.add_run(nuovo_testo)
        return
    primo = paragraph.runs[0]
    bold, italic, underline = primo.bold, primo.italic, primo.underline
    font_name = primo.font.name
    font_size = primo.font.size
    font_color = None
    try:
        if primo.font.color and primo.font.color.type is not None:
            font_color = primo.font.color.rgb
    except Exception:
        pass
    for extra in paragraph.runs[1:]:
        extra.text = ""
    primo.text = nuovo_testo
    primo.bold, primo.italic, primo.underline = bold, italic, underline
    if font_name:
        primo.font.name = font_name
    if font_size:
        primo.font.size = font_size
    if font_color:
        primo.font.color.rgb = font_color


# ==========================================
# STEP 1 — PROPOSTA MODIFICHE (Claude)
# ==========================================
_SYSTEM_PROPOSTA = """Sei un copywriter esperto di CV che riformula (senza mai inventare) i contenuti di un CV per allinearlo al linguaggio di un annuncio di lavoro specifico.

REGOLE ASSOLUTE:
- Non puoi aggiungere fatti, competenze, strumenti, numeri o esperienze che non siano già presenti nel CV originale, nemmeno se l'annuncio li richiede. Se manca un requisito (es. l'annuncio chiede Salesforce e il CV non lo cita), NON aggiungerlo: ometti semplicemente quel punto.
- Puoi invece rendere ESPLICITO ciò che nel CV è già vero ma implicito, usando il linguaggio dell'annuncio — es. se l'annuncio chiede "gestione di persone" e il candidato ha guidato team in più esperienze, puoi riformulare un bullet esistente per usare quel linguaggio, perché il fatto è già vero e documentato.
- Ogni bullet che proponi di modificare deve restare sostanzialmente della stessa lunghezza dell'originale (max +/-15%): non stai aggiungendo contenuto, stai riformulando quello che c'è.
- Non toccare numeri, percentuali, importi o date: vanno riportati identici.
- Per il paragrafo PROFILO PROFESSIONALE: mantieni la stessa struttura/framework della versione originale (stesso ordine di concetti: titolo professionale, track record, azioni chiave, chiusura su cosa cerca), riformulando solo l'enfasi e il linguaggio per allinearli all'annuncio.
- Se un bullet è già ben allineato all'annuncio così com'è, non modificarlo — proponi solo le modifiche che hanno un impatto reale sul match.

Per ogni modifica che proponi, spiega in "fonte" da quale punto esatto del CV originale deriva il fatto (per permettere una verifica indipendente)."""

def _proponi_modifiche_cv(job_title, job_text, intro_originale, bullet_originali):
    client = _get_client()
    if client is None:
        return None

    bullet_elenco = "\n".join(f"[{idx}] {testo}" for idx, testo in bullet_originali.items())
    user_content = f"""TITOLO DELL'ANNUNCIO: {job_title}

TESTO INTEGRALE DELL'ANNUNCIO:
{job_text[:8000]}

PARAGRAFO PROFILO PROFESSIONALE ORIGINALE:
{intro_originale}

BULLET DELLE ESPERIENZE ORIGINALI (indice: testo):
{bullet_elenco}

Proponi le riformulazioni che avvicinano il CV al linguaggio di questo annuncio, seguendo tutte le regole del tuo ruolo."""

    try:
        with client.messages.stream(
            model=MATCH_LLM_MODEL,
            max_tokens=16000,
            system=_SYSTEM_PROPOSTA,
            messages=[{"role": "user", "content": user_content}],
            output_config={
                "effort": "medium",
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "intro_riformulato": {"type": "string"},
                            "modifiche_bullet": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "indice": {"type": "integer"},
                                        "nuovo_testo": {"type": "string"},
                                        "fonte": {"type": "string"},
                                    },
                                    "required": ["indice", "nuovo_testo", "fonte"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["intro_riformulato", "modifiche_bullet"],
                        "additionalProperties": False,
                    },
                },
            },
        ) as stream:
            response = stream.get_final_message()
        testo = next(b.text for b in response.content if b.type == "text")
        return json.loads(testo)
    except Exception as e:
        logging.warning(f"Proposta modifiche CV fallita: {e}")
        return None


# ==========================================
# STEP 2 — VERIFICA ANTI-FABBRICAZIONE (Claude, ruolo separato)
# ==========================================
_SYSTEM_VERIFICA = """Sei un revisore scettico e indipendente. Il tuo unico compito è controllare che delle modifiche proposte a un CV non introducano NESSUN fatto, competenza, numero o affermazione che non sia già presente — anche solo implicitamente ma inequivocabilmente — nel testo integrale del CV originale che ti viene fornito.

Per ogni modifica proposta:
- Approvala SOLO se ogni singola affermazione nel nuovo testo è verificabile nel CV originale.
- Se anche un solo dettaglio non è riconducibile con certezza al CV originale, respingila (approvato=false) e nel testo_finale riporta il testo ORIGINALE invariato (mai il tuo testo, mai quello proposto se scartato).
- Sii scettico di default: in caso di dubbio, respingi. Meglio un CV meno ottimizzato che un CV con anche un solo dettaglio non verificabile.
- Non correggere né migliorare il testo proposto: approvi o respingi così com'è."""

def _verifica_modifiche_cv(cv_testo_completo, proposta):
    client = _get_client()
    if client is None:
        return None

    modifiche_elenco = "\n".join(
        f"[{m['indice']}] PROPOSTO: {m['nuovo_testo']}\n     FONTE DICHIARATA: {m['fonte']}"
        for m in proposta.get("modifiche_bullet", [])
    )
    user_content = f"""CV ORIGINALE INTEGRALE (verità di riferimento):
{cv_testo_completo[:8000]}

PROFILO PROFESSIONALE PROPOSTO (da verificare):
{proposta.get('intro_riformulato', '')}

BULLET PROPOSTI (da verificare uno per uno):
{modifiche_elenco}

Verifica ogni elemento secondo le tue regole."""

    try:
        with client.messages.stream(
            model=MATCH_LLM_MODEL,
            max_tokens=16000,
            system=_SYSTEM_VERIFICA,
            messages=[{"role": "user", "content": user_content}],
            output_config={
                "effort": "medium",
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "intro": {
                                "type": "object",
                                "properties": {
                                    "approvato": {"type": "boolean"},
                                    "testo_finale": {"type": "string"},
                                },
                                "required": ["approvato", "testo_finale"],
                                "additionalProperties": False,
                            },
                            "bullet": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "indice": {"type": "integer"},
                                        "approvato": {"type": "boolean"},
                                        "testo_finale": {"type": "string"},
                                    },
                                    "required": ["indice", "approvato", "testo_finale"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["intro", "bullet"],
                        "additionalProperties": False,
                    },
                },
            },
        ) as stream:
            response = stream.get_final_message()
        testo = next(b.text for b in response.content if b.type == "text")
        return json.loads(testo)
    except Exception as e:
        logging.warning(f"Verifica modifiche CV fallita: {e}")
        return None


# ==========================================
# STEP 3 — CONVERSIONE DOCX -> PDF (LibreOffice headless)
# ==========================================
def _converti_docx_in_pdf(docx_path, output_dir):
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        logging.warning("LibreOffice (soffice) non trovato nel PATH: impossibile generare il PDF personalizzato.")
        return None
    try:
        result = subprocess.run(
            [soffice, "--headless", "--norestore", "--convert-to", "pdf", "--outdir", output_dir, docx_path],
            capture_output=True, text=True, timeout=90,
        )
        if result.returncode != 0:
            logging.error(f"Conversione LibreOffice fallita (codice {result.returncode}): {result.stderr}")
            return None
        base = os.path.splitext(os.path.basename(docx_path))[0]
        pdf_path = os.path.join(output_dir, base + ".pdf")
        return pdf_path if os.path.exists(pdf_path) else None
    except Exception as e:
        logging.error(f"Errore conversione docx->pdf: {e}")
        return None


# ==========================================
# ORCHESTRAZIONE
# ==========================================
def genera_cv_personalizzato(job_title: str, job_text: str, output_dir: str = None):
    """Genera un CV .docx + .pdf personalizzato per un annuncio specifico.
    Ritorna {"pdf_path": ..., "docx_path": ..., "riepilogo": [righe leggibili]}
    oppure None se una qualunque fase fallisce — in tal caso nessun file
    viene generato/allegato, mai un CV non verificato."""
    if not os.path.exists(CV_DOCX_TEMPLATE):
        logging.warning(f"Template CV .docx non trovato: {CV_DOCX_TEMPLATE}")
        return None
    if not job_title or not job_text or len(job_text.strip()) < 50:
        logging.warning("Testo annuncio insufficiente per la personalizzazione del CV.")
        return None

    try:
        doc = docx.Document(CV_DOCX_TEMPLATE)
    except Exception as e:
        logging.error(f"Errore apertura template CV: {e}")
        return None

    if len(doc.paragraphs) <= max(INDICI_BULLET_MODIFICABILI + [INDICE_TITLE, INDICE_INTRO]):
        logging.error("Il template CV .docx non ha la struttura attesa (paragrafi mancanti) — personalizzazione annullata.")
        return None

    bullet_originali = {i: doc.paragraphs[i].text.strip() for i in INDICI_BULLET_MODIFICABILI}
    intro_originale = doc.paragraphs[INDICE_INTRO].text.strip()
    cv_testo_completo = "\n".join(p.text for p in doc.paragraphs if p.text.strip())

    proposta = _proponi_modifiche_cv(job_title, job_text, intro_originale, bullet_originali)
    if proposta is None:
        return None

    verificata = _verifica_modifiche_cv(cv_testo_completo, proposta)
    if verificata is None:
        return None

    riepilogo = []

    # Title: sostituzione deterministica, sempre applicata (non richiede verifica LLM,
    # è il titolo dell'annuncio stesso, non un'affermazione sul candidato)
    _sostituisci_testo_paragrafo(doc.paragraphs[INDICE_TITLE], formatta_title_spaziato(job_title))
    riepilogo.append(f'Title aggiornato a: "{job_title}"')

    intro_info = verificata.get("intro", {})
    if intro_info.get("approvato") and intro_info.get("testo_finale", "").strip():
        nuovo_intro = intro_info["testo_finale"].strip()
        if nuovo_intro != intro_originale:
            _sostituisci_testo_paragrafo(doc.paragraphs[INDICE_INTRO], nuovo_intro)
            riepilogo.append("Profilo professionale riformulato per l'annuncio")

    for voce in verificata.get("bullet", []):
        idx = voce.get("indice")
        if idx not in INDICI_BULLET_MODIFICABILI or not voce.get("approvato"):
            continue
        nuovo = voce.get("testo_finale", "").strip()
        originale = bullet_originali.get(idx, "")
        if not nuovo or nuovo == originale:
            continue
        _sostituisci_testo_paragrafo(doc.paragraphs[idx], nuovo)
        riepilogo.append(f'Bullet riformulato: "{originale[:70]}..." -> "{nuovo[:70]}..."')

    if len(riepilogo) <= 1:
        logging.info("Nessuna modifica verificata per questo annuncio (oltre al title) — CV personalizzato non generato.")
        return None

    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="cv_personalizzato_")
    else:
        os.makedirs(output_dir, exist_ok=True)

    docx_out = os.path.join(output_dir, "cv_personalizzato.docx")
    try:
        doc.save(docx_out)
    except Exception as e:
        logging.error(f"Errore salvataggio docx personalizzato: {e}")
        return None

    pdf_path = _converti_docx_in_pdf(docx_out, output_dir)
    if pdf_path is None:
        return None

    return {"pdf_path": pdf_path, "docx_path": docx_out, "riepilogo": riepilogo}


def genera_cv_per_offerta(job_title: str, job_link: str, output_dir: str = None):
    """Punto d'ingresso usato dallo scraper: riscarica il testo integrale
    dell'annuncio da job_link (il testo usato per il match originale non
    viene persistito in offerte_giornaliere.json) e genera il CV personalizzato.
    Ritorna None su qualunque fallimento, senza sollevare eccezioni verso il chiamante."""
    try:
        import requests
        from bs4 import BeautifulSoup
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        resp = requests.get(job_link, headers=headers, timeout=10)
        if resp.status_code != 200:
            logging.warning(f"Impossibile riscaricare l'annuncio per la personalizzazione CV ({job_link}): HTTP {resp.status_code}")
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        job_text = soup.get_text(" ", strip=True)
    except Exception as e:
        logging.warning(f"Errore riscaricamento annuncio per personalizzazione CV: {e}")
        return None

    try:
        return genera_cv_personalizzato(job_title, job_text, output_dir=output_dir)
    except Exception as e:
        logging.error(f"Errore imprevisto nella generazione del CV personalizzato: {e}")
        return None
