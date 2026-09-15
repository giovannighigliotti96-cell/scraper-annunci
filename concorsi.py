# -*- coding: utf-8 -*-
"""Concorsi pubblici pertinenti, letti da inPA e valutati sul bando.

Perche' esiste. I concorsi sono una rete di sicurezza, non un canale: su
Genova ci sono una decina di bandi aperti in tutto, e un Funzionario negli
enti locali parte da 24-28k lordi contro i 40-65k delle offerte private che
arrivano dai portali. Ma un bando giusto — comunicazione, marketing,
transizione digitale, amministrativo con laurea triennale — vale la
candidatura per la stabilita', e va trovato prima della scadenza.

Fonte: l'API JSON del portale inPA (quella usata dal sito stesso, non
documentata ma stabile). Non ha un filtro per regione che funzioni: si
scaricano tutti i bandi aperti (17-18 pagine da 100) e si filtra qui per
sede: Genova, Milano, o ente regionale ligure/lombardo.

Niente ricerca del lavoro da remoto, ed e' una scelta verificata (15/09/2026):
nei bandi "remoto" vuol dire prova d'esame telematica o monitoraggio remoto
di impianti, mai la modalita' di lavoro. Nella PA il lavoro agile si concorda
dopo l'assunzione e non compare nel bando.

Per ogni bando pertinente si scarica il PDF e lo si legge con il modello:
titolo di studio richiesto e se la laurea triennale basta, trattamento
economico, prove, requisiti bloccanti, quante candidature ha gia' ricevuto,
e un verdetto. Giovanni ha una Laurea Triennale (L-18, Business Management &
Digital Economy): e' il requisito che apre o chiude quasi tutto.

Uso:
    python concorsi.py                 nuovi bandi pertinenti, con scheda (stampa)
    python concorsi.py --tutti         anche quelli gia' visti
    python concorsi.py --email         invia la sezione come email a se' stante
"""
import io
import json
import logging
import re
import smtplib
import sys
import time
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import PyPDF2
from curl_cffi import requests as cr

import state_io
import scraper as S

STATO_CONCORSI_FILE = "concorsi_stato.json"
API = "https://portale.inpa.gov.it/concorsi-smart/api/concorso-public-area"
MEDIA = "https://portale.inpa.gov.it/api/media/{}"
PAGINA_PUBBLICA = "https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id={}"

# Sedi: il campo "sedi" di inPA e' una lista tipo ["Lombardia", "Bergamo"].
# Si tiene il bando se nomina Genova o Milano, oppure se indica la sola
# regione (ente regionale, senza provincia). "Lombardia, Brescia" resta fuori:
# non e' pendolabile.
REGIONI_OK = {"liguria", "lombardia"}
CITTA_OK = {"genova", "milano"}

# Profili che hanno senso per un Digital Sales & Marketing Manager con laurea
# triennale in economia. Si cerca nel titolo, nella figura ricercata e nella
# descrizione breve.
PROFILI_OK = re.compile(
    r"comunicazion|marketing|digital|informatic|amministrativ|gestional|economic|"
    r"commercial|innovazion|transizione digitale|trasformazione digitale|"
    r"progett(?:i|azione) europe|pnrr|relazioni (?:esterne|con il pubblico)|"
    r"ufficio stampa|social|web|customer|servizi generali|risorse umane|"
    r"funzionario(?! tecnic)|specialista|esperto|elevata qualificazione|"
    r"istruttore (?:amministrativ|direttiv)",
    re.IGNORECASE,
)
# Profili che escludono a prescindere, anche se il titolo contiene una parola buona.
PROFILI_NO = re.compile(
    r"medic|infermier|sanitari|oss\b|operatore socio|veterinar|farmac|biolog|"
    r"ingegner|geometra|perito|architett|\btecnic[oi]\b|polizia|vigil|"
    r"agente|docent|insegnant|educator|asilo|nido|ricercator|professor|dottorato|"
    r"assegno di ricerca|borsa di studio|operai|autist|cuoc|manutent|elettricist|"
    r"idraulic|giardinier|necrofor|bidell|collaboratore scolastic|avvocat|notai|"
    r"magistrat|militar|carabinier|marina|aeronautic|esercito|dirigente medic|"
    r"psicolog|assistente social|fisioterap|ostetric|radiolog|laborator",
    re.IGNORECASE,
)

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://www.inpa.gov.it",
    "Referer": "https://www.inpa.gov.it/",
}


# ---------------------------------------------------------------- stato

def carica_stato():
    try:
        dati = state_io.load_json_or_raise(STATO_CONCORSI_FILE, {})
        return dati if isinstance(dati, dict) else {}
    except Exception as e:
        logging.error(f"Errore lettura {STATO_CONCORSI_FILE}: {e}")
        return {}


def salva_stato(stato):
    state_io.atomic_write_json(STATO_CONCORSI_FILE, stato, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- inPA

def _sessione():
    return cr.Session(impersonate="chrome124")


def _cerca(sessione, testo, pagina, size=100):
    r = sessione.post(f"{API}/search-better?page={pagina}&size={size}",
                      json={"text": testo, "status": ["OPEN"]}, headers=_HEADERS, timeout=40)
    if r.status_code != 200:
        raise RuntimeError(f"inPA search HTTP {r.status_code}")
    return r.json()


def bandi_aperti(sessione):
    """Tutti i bandi aperti con sede ammessa (vedi sede_ammessa)."""
    trovati = {}
    pagina, ultime = 0, False
    while not ultime and pagina < 40:
        d = _cerca(sessione, "", pagina)
        for c in d.get("content", []):
            if sede_ammessa(c):
                trovati[c["id"]] = c
        ultime = d.get("last", True)
        pagina += 1
        time.sleep(0.4)
    return list(trovati.values())


def sede_ammessa(c):
    sedi = {str(x).lower() for x in (c.get("sedi") or [])}
    if sedi & CITTA_OK:
        return True
    return bool(sedi) and sedi <= REGIONI_OK


# Procedure riservate a chi e' gia' dipendente pubblico o interne all'ente:
# un esterno non puo' partecipare, e' inutile valutarle.
RISERVATI = re.compile(r"mobilit[aà]|interpello|riservat[oa] (?:esclusivamente )?al personale|"
                       r"progressione|comando|distacco|interno|scorrimento|graduatoria",
                       re.IGNORECASE)


def pertinente(c):
    """True se il profilo puo' interessare. Prima le esclusioni: un titolo
    come "Funzionario tecnico ingegnere" contiene "funzionario" ma non e' per
    noi."""
    testo = " ".join(str(c.get(k) or "") for k in ("titolo", "figuraRicercata", "descrizioneBreve"))
    if RISERVATI.search(str(c.get("titolo") or "") + " " + str(c.get("categorie") or "")):
        return False
    if PROFILI_NO.search(testo):
        return False
    return PROFILI_OK.search(testo) is not None


def dettaglio(sessione, cid):
    r = sessione.get(f"{API}/{cid}", headers=_HEADERS, timeout=40)
    return r.json() if r.status_code == 200 else {}


def testo_bando(sessione, det):
    """Il testo del PDF del bando (primo allegato di tipo BANDO_CONCORSO)."""
    allegati = det.get("allegati") or []
    allegati.sort(key=lambda a: (a.get("tipo") != "BANDO_CONCORSO", a.get("sequence") or 0))
    for a in allegati[:2]:
        mid = a.get("mediaId") or a.get("id")
        if not mid:
            continue
        try:
            r = sessione.get(MEDIA.format(mid), headers={"Referer": "https://www.inpa.gov.it/"}, timeout=60)
            if r.status_code != 200 or r.content[:4] != b"%PDF":
                continue
            lettore = PyPDF2.PdfReader(io.BytesIO(r.content))
            testo = " ".join((p.extract_text() or "") for p in lettore.pages)
            testo = re.sub(r"\s+", " ", testo)
            if len(testo) > 500:
                return testo
        except Exception as e:
            logging.warning(f"PDF bando non leggibile ({mid}): {type(e).__name__}")
    return ""


def _estratto_per_llm(testo, massimo=22000):
    """I bandi sono lunghi (20+ pagine). Si tiene l'inizio, dove stanno posti e
    requisiti, piu' le finestre attorno a trattamento economico e prove."""
    if len(testo) <= massimo:
        return testo
    pezzi = [testo[:14000]]
    for chiave in ("trattamento economico", "prova scritta", "prova orale", "titolo di studio", "laurea"):
        i = testo.lower().find(chiave, 14000)
        if i > 0:
            pezzi.append(testo[max(0, i - 400): i + 1600])
    return "\n[...]\n".join(pezzi)[:massimo]


# ---------------------------------------------------------------- valutazione

_ISTRUZIONI = """Sei un esperto di concorsi pubblici italiani. Leggi il bando e rispondi SOLO con il JSON richiesto, in italiano, senza inventare: se un dato non c'e' scrivi "non indicato".

IL CANDIDATO: Digital Sales & Marketing Manager, Laurea Triennale L-18 (Business Management & Digital Economy, Universita' Mercatorum, 2024), Master (non universitario) in Marketing & Sales Management, italiano madrelingua, inglese C1, nessuna altra lingua, nessuna esperienza nella PA, nessuna abilitazione professionale. Esperienza: fondazione e scaling di un marketplace B2B2C, gestione team fino a 10 persone, go-to-market e pipeline in PMI e startup.

Campi:
- titolo_studio_richiesto: cosa chiede il bando, con le classi di laurea se indicate.
- laurea_triennale_basta: "si" se una laurea triennale in economia/management (L-18 o classe 28/17 vecchio ordinamento, o "qualsiasi laurea") e' ammessa; "no" se serve la magistrale, una classe diversa, un'abilitazione o un'esperienza in PA; "dubbio" se non e' chiaro.
- ral_annua: stipendio lordo annuo stimato. Se il bando indica solo l'inquadramento, stima: Area Funzionari enti locali 24.000-28.000; Area Istruttori 21.000-24.000; Elevata Qualificazione 30.000-40.000; Ministeri/Agenzie Funzionario 28.000-35.000. Scrivi la cifra e da cosa la deduci.
- prove: elenco sintetico (preselezione, scritto, orale, materie principali).
- requisiti_bloccanti: requisiti che il candidato NON ha, se ce ne sono. Altrimenti "nessuno".
- ruolo_in_pratica: cosa farebbe davvero, in una frase.
- verdetto: "fai", "valuta" o "lascia".
- motivazione: due frasi, oneste: perche' il verdetto, e cosa pesa di piu'.

BANDO:
{bando}
"""

_SCHEMA = {
    "type": "object",
    "properties": {k: {"type": "string"} for k in (
        "titolo_studio_richiesto", "laurea_triennale_basta", "ral_annua", "prove",
        "requisiti_bloccanti", "ruolo_in_pratica", "verdetto", "motivazione")},
    "required": ["titolo_studio_richiesto", "laurea_triennale_basta", "ral_annua", "prove",
                 "requisiti_bloccanti", "ruolo_in_pratica", "verdetto", "motivazione"],
}


def valuta_bando(testo):
    client = S._get_gemini_client()
    if client is None or not testo:
        return None
    prompt = _ISTRUZIONI.format(bando=_estratto_per_llm(testo))
    for modello in S.GEMINI_MODELLI:
        try:
            r = client.interactions.create(
                model=modello, input=prompt,
                response_format={"type": "text", "mime_type": "application/json", "schema": _SCHEMA})
            return json.loads(r.output_text)
        except Exception as e:
            logging.warning(f"Valutazione bando: {modello} fallito: {type(e).__name__}")
            time.sleep(2)
    return None


# ---------------------------------------------------------------- pipeline

def _scadenza(c):
    s = str(c.get("dataScadenza") or "")[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def nuovi_bandi(tutti=False):
    """Schede dei bandi pertinenti non ancora visti, entro scadenza."""
    stato = carica_stato()
    sessione = _sessione()
    candidati = [c for c in bandi_aperti(sessione) if pertinente(c)]
    schede = []
    for c in candidati:
        cid = c["id"]
        if cid in stato and not tutti:
            continue
        scad = _scadenza(c)
        if scad and scad < date.today():
            continue
        det = dettaglio(sessione, cid)
        testo = testo_bando(sessione, det)
        val = valuta_bando(testo) if testo else None
        schede.append({
            "id": cid, "titolo": c.get("titolo", ""), "figura": c.get("figuraRicercata") or "",
            "sedi": [str(x) for x in (c.get("sedi") or [])], "posti": c.get("numPosti"),
            "scadenza": scad.isoformat() if scad else "non indicata",
            "candidature": det.get("numCandidatureSubmitted"),
            "tipo": c.get("tipoProcedura") or "",
            "link": PAGINA_PUBBLICA.format(cid), "valutazione": val,
            "bando_letto": bool(testo),
        })
        stato[cid] = {"visto_il": date.today().isoformat(),
                      "verdetto": (val or {}).get("verdetto", "non valutato"),
                      "titolo": c.get("titolo", "")[:120]}
        time.sleep(1)
    salva_stato(stato)
    schede.sort(key=lambda s: ({"fai": 0, "valuta": 1}.get((s["valutazione"] or {}).get("verdetto"), 2), s["scadenza"]))
    return schede


def testo_sezione(schede):
    if not schede:
        return ""
    righe = [f"CONCORSI PUBBLICI — {len(schede)} bandi nuovi pertinenti (Genova, Milano)",
             "Rete di sicurezza, non canale: RAL PA 24-40k contro 40-65k del privato. Ma la stabilita' conta.", ""]
    for s in schede:
        v = s["valutazione"] or {}
        verdetto = v.get("verdetto", "?").upper()
        righe += [f"[{verdetto}] {s['titolo'][:110]}",
                  f"   {s['figura'][:60]} · sedi {', '.join(s['sedi']) or '?'} · "
                  f"posti {s['posti']} · scade {s['scadenza']} · {s['tipo']}"
                  + (f" · candidature finora {s['candidature']}" if s.get("candidature") is not None else "")]
        if v:
            righe += [f"   Titolo di studio: {v.get('titolo_studio_richiesto', '')[:160]}",
                      f"   Triennale basta: {v.get('laurea_triennale_basta', '')}",
                      f"   RAL: {v.get('ral_annua', '')[:120]}",
                      f"   Prove: {v.get('prove', '')[:160]}",
                      f"   Requisiti che mancano: {v.get('requisiti_bloccanti', '')[:140]}",
                      f"   In pratica: {v.get('ruolo_in_pratica', '')[:160]}",
                      f"   -> {v.get('motivazione', '')[:260]}"]
        elif not s["bando_letto"]:
            righe.append("   (PDF del bando non leggibile: apri il link e valuta a mano)")
        else:
            righe.append("   (modello non disponibile: bando letto ma non valutato)")
        righe += [f"   {s['link']}", ""]
    return "\n".join(righe)


def invia_email_concorsi(testo, quanti):
    msg = MIMEMultipart("mixed")
    msg["From"] = S.GMAIL_USER
    msg["To"] = S.DESTINATION_EMAIL
    msg["Subject"] = f"[Concorsi] {quanti} bandi pertinenti — {date.today().strftime('%d/%m')}"
    msg.attach(MIMEText(testo, "plain", "utf-8"))
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(S.GMAIL_USER, S.GMAIL_APP_PASSWORD)
        server.send_message(msg)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    schede = nuovi_bandi(tutti="--tutti" in sys.argv)
    testo = testo_sezione(schede) or "Nessun bando nuovo pertinente."
    print(testo)
    if "--email" in sys.argv and schede:
        invia_email_concorsi(testo, len(schede))
        print(f"\nEmail inviata: {len(schede)} bandi.")
