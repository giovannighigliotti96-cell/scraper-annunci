# -*- coding: utf-8 -*-
"""Pacchetto settimanale di candidature proattive verso le PMI in lista.

Perche' esiste. Il monitoraggio delle pagine careers (aziende_target.py) dice
DOVE candidarsi, ma una spontanea infilata in un form la legge nessuno: in una
PMI da 5-50 milioni assume il titolare o il direttore, e si convince con un
messaggio scritto sul SUO business, non con un CV generico. Questo modulo
prepara ogni settimana dieci aziende con: cosa fanno (letto dal loro sito), a
chi scrivere (nomi e ruoli trovati nelle pagine "chi siamo"/"team"/"contatti"),
i recapiti pubblici, e un messaggio di sei righe pronto da mandare. Giovanni
lo manda; il sistema tiene traccia di chi e' gia' stato proposto.

Uso:
    python outreach.py            prepara e invia il pacchetto (10 aziende)
    python outreach.py --quante 5 --niente-email   prova a video
"""
import json
import logging
import os
import re
import smtplib
import sys
import time
from datetime import date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import state_io
import scraper as S

AZIENDE_FILE = "aziende_target.json"
STATO_OUTREACH_FILE = "outreach_stato.json"
AZIENDE_PER_SETTIMANA = 10

# Quanto un settore ha bisogno di un digital + sales: decide l'ordine con cui
# le aziende vengono proposte. Stessi pesi usati per costruire la lista.
PESO_SETTORE = {
    "software e IT": 10, "servizi informativi": 10, "pubblicita' e ricerche di mercato": 10,
    "editoria": 9, "produzione audiovisiva": 9, "telecomunicazioni": 9,
    "commercio al dettaglio": 9, "istruzione e formazione": 8, "turismo": 8,
    "commercio all'ingrosso": 7, "consulenza gestionale": 7, "alimentare": 7,
    "bevande": 7, "abbigliamento": 7, "pelle e calzature": 7, "mobili": 7,
}

# Pagine dove una PMI racconta se stessa e nomina le persone.
PAGINE_UTILI = ["/chi-siamo", "/about", "/about-us", "/azienda", "/team", "/il-team",
                "/contatti", "/contact", "/contacts", "/storia", "/la-nostra-storia",
                "/it/chi-siamo", "/it/azienda", "/it/contatti", "/management"]

# Ruoli che decidono un'assunzione in una PMI, in ordine di preferenza.
RUOLI_DECISORI = [
    "amministratore delegato", "amministratore unico", "ceo", "managing director",
    "direttore generale", "general manager", "presidente", "founder", "fondatore",
    "co-founder", "titolare", "owner", "direttore commerciale", "sales director",
    "direttore marketing", "marketing director", "cmo", "responsabile marketing",
    "responsabile commerciale", "hr manager", "responsabile risorse umane",
    "direttore risorse umane", "head of people", "talent",
]
_RE_RUOLO = re.compile(r"\b(?:" + "|".join(re.escape(r) for r in RUOLI_DECISORI) + r")\b", re.IGNORECASE)
_RE_NOME = re.compile(r"\b([A-ZÀ-Ý][a-zà-ÿ']{2,}(?:\s+[A-ZÀ-Ý][a-zà-ÿ']{2,}){1,2})\b")
_RE_EMAIL = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_RE_LINKEDIN = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/company/[A-Za-z0-9_\-%.]+")

_HEADERS = {
    "User-Agent": S.USER_AGENT_CHROME,
    "Accept-Language": "it-IT,it;q=0.9",
}


# ---------------------------------------------------------------- stato

def carica_stato():
    try:
        dati = state_io.load_json_or_raise(STATO_OUTREACH_FILE, {})
        return dati if isinstance(dati, dict) else {}
    except Exception as e:
        logging.error(f"Errore lettura {STATO_OUTREACH_FILE}: {e}")
        return {}


def salva_stato(stato):
    state_io.atomic_write_json(STATO_OUTREACH_FILE, stato, ensure_ascii=False, indent=2)


def _min_dipendenti(fascia):
    try:
        return int(str(fascia or "0").split("-")[0].rstrip("+"))
    except ValueError:
        return 0


def scegli_aziende(quante, stato):
    """Le prossime aziende da proporre: mai proposte, per priorita' di settore,
    alternando Genova e Milano cosi' il pacchetto non e' tutto di una citta'."""
    try:
        aziende = state_io.load_json_or_raise(AZIENDE_FILE, [])
    except Exception as e:
        logging.error(f"Errore lettura {AZIENDE_FILE}: {e}")
        return []
    # Sopra i 500 dipendenti non decide il titolare e il messaggio diretto non
    # ha lo stesso effetto: quelle aziende passano dai portali e dalle careers.
    nuove = [a for a in aziende if a["nome"] not in stato
             and _min_dipendenti(a.get("dipendenti")) < 500]
    nuove.sort(key=lambda a: (-PESO_SETTORE.get(a.get("settore", ""), 3),
                              -float(a.get("fatturato_mln") or 0)))
    per_citta = {"Genova": [a for a in nuove if a.get("citta") == "Genova"],
                 "Milano": [a for a in nuove if a.get("citta") == "Milano"]}
    scelte = []
    while len(scelte) < quante and any(per_citta.values()):
        for citta in ("Genova", "Milano"):
            if per_citta[citta] and len(scelte) < quante:
                scelte.append(per_citta[citta].pop(0))
    return scelte


# ---------------------------------------------------------------- lettura del sito

def _scarica(url, timeout=12):
    try:
        r = requests.get(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
        if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
            return r.text
    except requests.RequestException:
        pass
    return ""


def _testo(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def _descrizione(html):
    """Meta description o primo paragrafo sostanzioso: e' come l'azienda si
    presenta, ed e' la base per scriverle qualcosa di specifico."""
    soup = BeautifulSoup(html, "html.parser")
    for attr in ({"name": "description"}, {"property": "og:description"}):
        m = soup.find("meta", attrs=attr)
        if m and len(m.get("content", "")) > 40:
            return m["content"].strip()[:400]
    for p in soup.find_all("p"):
        t = p.get_text(" ", strip=True)
        if len(t) > 80:
            return t[:400]
    return ""


def _persone(testo):
    """Coppie (nome, ruolo) dove un nome proprio sta vicino a un ruolo decisore.

    Grossolano per necessita': ogni sito impagina il team a modo suo. Si tiene
    una finestra di 60 caratteri tra ruolo e nome, in entrambe le direzioni,
    e si scartano le false coppie piu' comuni (nomi di citta', "Via", mesi)."""
    trovate, visti = [], set()
    for m in _RE_RUOLO.finditer(testo):
        if testo[m.end(): m.end() + 12].lower().lstrip().startswith("effettiv"):
            continue  # "titolare effettivo": termine antiriciclaggio, non una persona
        # prima il nome PRIMA del ruolo ("Mario Rossi, Amministratore Delegato"),
        # che e' la forma italiana; poi quello dopo ("CEO: Mario Rossi")
        prima = testo[max(0, m.start() - 50): m.start()]
        dopo = testo[m.end(): m.end() + 50]
        candidati = [n.group(1) for n in _RE_NOME.finditer(prima)][::-1] + \
                    [n.group(1) for n in _RE_NOME.finditer(dopo)]
        for grezzo in candidati:
            nome = _pulisci_nome(grezzo)
            if not nome or nome.lower() in visti:
                continue
            visti.add(nome.lower())
            trovate.append((nome, m.group(0).strip()))
            break
    return trovate[:4]


# Parole che stanno nei nomi di prodotti, reparti e indirizzi ma non in quelli
# delle persone. Un sito genovese dava "Software Trading Perseo — ceo".
_NON_PERSONA = {
    "via", "piazza", "corso", "sede", "milano", "genova", "italia", "srl", "spa", "chi",
    "siamo", "cookie", "privacy", "policy", "contatti", "lavora", "con", "noi", "team",
    "software", "trading", "suite", "energy", "data", "management", "forecasting", "scopri",
    "digital", "marketing", "sales", "solutions", "solution", "services", "service", "group",
    "gruppo", "business", "consulting", "system", "systems", "platform", "cloud", "smart",
    "mobile", "web", "app", "media", "design", "studio", "company", "italy", "europe",
    "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
    "settembre", "ottobre", "novembre", "dicembre", "leggi", "tutto", "vedi", "altro",
    "amministratore", "delegato", "direttore", "generale", "presidente", "responsabile",
    "director", "manager", "head", "chief", "officer", "partner", "founder", "owner",
    "effettivo", "verifica", "impresa", "compliance", "protect", "servizi", "lingua",
    "indietro", "avanti", "home", "news", "blog", "login", "area", "riservata",
    "ultimate", "beneficial", "entrepreneur", "per", "the", "and", "our", "your",
    "product", "products", "brand", "brands", "customer", "customers", "client", "clients",
}


def _pulisci_nome(nome):
    """Il nome senza le parole di contorno che il regex trascina dentro
    ("Stefano Merchiori Director" -> "Stefano Merchiori"). None se cio' che
    resta non sembra una persona."""
    parole = nome.split()
    while parole and parole[0].lower() in _NON_PERSONA:
        parole.pop(0)
    while parole and parole[-1].lower() in _NON_PERSONA:
        parole.pop()
    if not 2 <= len(parole) <= 3 or any(p.lower() in _NON_PERSONA for p in parole):
        return None
    return " ".join(parole)


def leggi_azienda(azienda):
    """Cosa il sito dice dell'azienda e delle sue persone."""
    sito = azienda["sito"].rstrip("/")
    home = _scarica(sito)
    dossier = {"descrizione": _descrizione(home) if home else "",
               "persone": [], "email": [], "linkedin": "", "pagine_lette": []}
    testi = [_testo(home)] if home else []
    if home:
        # link interni della home che sembrano "chi siamo"/"team"/"contatti"
        soup = BeautifulSoup(home, "html.parser")
        candidati = []
        for a in soup.find_all("a", href=True):
            h = a["href"]
            if any(k in h.lower() for k in ("chi-siamo", "chisiamo", "about", "team", "azienda",
                                              "contatt", "contact", "storia", "management")):
                u = urljoin(sito + "/", h)
                if urlparse(u).netloc == urlparse(sito).netloc and u not in candidati:
                    candidati.append(u)
        for p in PAGINE_UTILI:
            candidati.append(sito + p)
        for u in candidati[:8]:
            html = _scarica(u)
            if html:
                dossier["pagine_lette"].append(u)
                testi.append(_testo(html))
                if not dossier["descrizione"]:
                    dossier["descrizione"] = _descrizione(html)
            time.sleep(0.3)
    tutto = " ".join(testi)
    dossier["persone"] = _persone(tutto)
    dominio = urlparse(sito).netloc.replace("www.", "")
    dossier["email"] = sorted({e.lower() for e in _RE_EMAIL.findall(tutto)
                               if dominio in e.lower() and not e.lower().startswith(("noreply", "no-reply"))})[:4]
    m = _RE_LINKEDIN.search(" ".join(testi) + (home or ""))
    if m:
        dossier["linkedin"] = m.group(0)
    return dossier


# ---------------------------------------------------------------- il messaggio

_ISTRUZIONI_PITCH = """Sei un consulente di carriera. Scrivi un messaggio di candidatura spontanea in italiano, da inviare via email o LinkedIn al decisore di una PMI, per conto del candidato descritto dal CV qui sotto.

Regole ferree:
- Massimo 110 parole. Sei righe. Niente formule di cortesia lunghe, niente "spero di trovarla bene".
- La prima frase parla dell'AZIENDA (di cosa fa, di un segnale di crescita o di un canale digitale che potrebbe presidiare meglio), non del candidato. Deve essere evidente che il messaggio e' scritto per loro.
- Poi in due frasi cosa il candidato porta: unire strategia digitale e sviluppo commerciale, costruire go-to-market e pipeline, guidare un piccolo team. Usa fatti del CV, non aggettivi.
- Chiudi con una richiesta concreta e leggera: 20 minuti di call, o il nome della persona giusta con cui parlare.
- Non inventare nulla sull'azienda: usa solo la descrizione fornita. Se e' povera, resta generico su di loro ma specifico sul candidato.
- Non mettere oggetto, firma, placeholder tra parentesi quadre.

CV DEL CANDIDATO:
{cv}

AZIENDA: {nome} ({citta}, settore: {settore}, fatturato circa {fatturato} milioni, {dipendenti} dipendenti)
COME SI PRESENTA SUL SITO: {descrizione}
"""


def scrivi_pitch(azienda, dossier):
    """Il messaggio, via Gemini. Se il modello non risponde, un testo di riserva
    onesto: meglio un messaggio generico chiaro che nessun messaggio."""
    client = S._get_gemini_client()
    prompt = _ISTRUZIONI_PITCH.format(
        cv=S.CV_TESTO_PER_MATCH[:5000], nome=azienda["nome"], citta=azienda.get("citta", ""),
        settore=azienda.get("settore", ""), fatturato=azienda.get("fatturato_mln", "?"),
        dipendenti=azienda.get("dipendenti", "?"),
        descrizione=dossier.get("descrizione") or "(nessuna descrizione trovata)")
    if client is not None:
        for modello in S.GEMINI_MODELLI:
            try:
                r = client.interactions.create(model=modello, input=prompt)
                testo = (r.output_text or "").strip()
                if len(testo) > 80:
                    return testo, modello
            except Exception as e:
                logging.warning(f"Pitch {azienda['nome']}: {modello} fallito: {type(e).__name__}")
                time.sleep(2)
    return (f"Buongiorno, seguo {azienda['nome']} e il vostro lavoro nel settore {azienda.get('settore', '')}. "
            "Sono un Digital Sales & Marketing Manager: ho fondato e scalato un marketplace B2B2C, "
            "guidato team fino a 10 persone e costruito go-to-market e pipeline in PMI e startup. "
            "Credo di poter dare una mano a unire marketing e vendite su un unico obiettivo di crescita. "
            "Avrebbe 20 minuti per una call, o puo' indicarmi la persona giusta con cui parlarne?"), "riserva"


# ---------------------------------------------------------------- pacchetto

def prepara_pacchetto(quante=AZIENDE_PER_SETTIMANA):
    stato = carica_stato()
    scelte = scegli_aziende(quante, stato)
    schede = []
    for i, a in enumerate(scelte, 1):
        print(f"[{i}/{len(scelte)}] {a['nome']} ({a['citta']})", flush=True)
        dossier = leggi_azienda(a)
        pitch, modello = scrivi_pitch(a, dossier)
        schede.append({"azienda": a, "dossier": dossier, "pitch": pitch, "modello": modello})
        stato[a["nome"]] = {"proposta_il": date.today().isoformat(), "citta": a["citta"],
                            "persone": dossier["persone"], "email": dossier["email"]}
    salva_stato(stato)
    return schede


def testo_pacchetto(schede):
    righe = [f"Outreach settimanale — {date.today().strftime('%d/%m/%Y')}",
             f"{len(schede)} aziende. Per ognuna: cosa fanno, a chi scrivere, il messaggio pronto.",
             "Il messaggio e' una base: cambia una frase se sai qualcosa in piu' di loro.", ""]
    for i, s in enumerate(schede, 1):
        a, d = s["azienda"], s["dossier"]
        righe += ["=" * 70,
                  f"{i}. {a['nome']} — {a['citta']} ({a.get('comune', '')})",
                  f"   {a.get('settore', '')} · {a.get('fatturato_mln', '?')}M · {a.get('dipendenti', '?')} dipendenti",
                  f"   Sito: {a['sito']}"]
        if d["linkedin"]:
            righe.append(f"   LinkedIn: {d['linkedin']}")
        if d["descrizione"]:
            righe.append(f"   Cosa fanno: {d['descrizione'][:300]}")
        if d["persone"]:
            righe.append("   A chi scrivere:")
            for nome, ruolo in d["persone"]:
                righe.append(f"      - {nome} — {ruolo}")
        else:
            righe.append("   A chi scrivere: nessun nome sul sito — cerca su LinkedIn "
                         f"\"{a['nome'].split()[0]}\" + \"amministratore\" o \"CEO\"")
        if d["email"]:
            righe.append(f"   Email pubbliche: {', '.join(d['email'])}")
        righe += ["", "   MESSAGGIO:", ""]
        righe += ["   " + r for r in s["pitch"].splitlines() if r.strip()]
        righe.append("")
    righe += ["=" * 70, "",
              "Quando ne mandi una, registrala col sito dell'azienda come riferimento:",
              "   python candidature.py add https://www.sito-azienda.it",
              "cosi' il report sa cosa e' partito e quando sollecitare."]
    return "\n".join(righe)


def invia_pacchetto(testo, quante):
    msg = MIMEMultipart("mixed")
    msg["From"] = S.GMAIL_USER
    msg["To"] = S.DESTINATION_EMAIL
    msg["Subject"] = f"[Outreach] {quante} aziende a cui scrivere questa settimana — {date.today().strftime('%d/%m')}"
    msg.attach(MIMEText(testo, "plain", "utf-8"))
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(S.GMAIL_USER, S.GMAIL_APP_PASSWORD)
        server.send_message(msg)


def main():
    quante = AZIENDE_PER_SETTIMANA
    if "--quante" in sys.argv:
        quante = int(sys.argv[sys.argv.index("--quante") + 1])
    schede = prepara_pacchetto(quante)
    if not schede:
        print("Nessuna azienda nuova da proporre: la lista e' esaurita.")
        return 0
    testo = testo_pacchetto(schede)
    print(testo)
    if "--niente-email" in sys.argv:
        return 0
    invia_pacchetto(testo, len(schede))
    print(f"\nPacchetto inviato: {len(schede)} aziende "
          f"({sum(1 for s in schede if s['modello'] != 'riserva')} messaggi scritti dal modello).")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    sys.exit(main())
