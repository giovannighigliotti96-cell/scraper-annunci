# -*- coding: utf-8 -*-
"""Offerte lette direttamente dai siti careers delle grandi aziende.

Perche' esiste. LinkedIn e i portali sono intermediari: pubblicano in ritardo,
non tutto, e con centinaia di candidati gia' in coda. La fonte e' il sistema
di recruiting dell'azienda (l'ATS), e i grandi gruppi ne usano quattro o
cinque in tutto. Qui non c'e' uno scraper per azienda ma uno per ATS; ogni
azienda e' una riga di configurazione. Le offerte entrano nella stessa
pipeline dei portali — filtro sui titoli, citta', modalita', freschezza,
valutazione LLM — quindi arrivano in email con gli stessi criteri.

ATS supportati (tutti verificati dal vivo il 17/09/2026):
- workday        API JSON pubblica del career site (TeamSystem, Leonardo)
- successfactors Career Site Builder, pagina /search/ in HTML (Intesa Sanpaolo,
                 EssilorLuxottica)
- bnp            pagina di gruppo BNP Paribas, card HTML (Arval)
- ashby          API JSON pubblica del job board (Satispay)
- lutech         tabella HTML del sito Lutech, con sede e modalita' in chiaro

Per aggiungere un'azienda: python aziende_dirette.py --scopri <url careers>
stampa la riga da incollare in AZIENDE, se l'ATS e' riconosciuto.
"""
import logging
import re
import sys
import time
from datetime import date, timedelta

from bs4 import BeautifulSoup
from curl_cffi import requests as cr

# Config: una riga per azienda. "sede" e' un'etichetta di cortesia per i log.
AZIENDE = [
    {"nome": "TeamSystem", "ats": "workday",
     "host": "teamsystem.wd103.myworkdayjobs.com", "tenant": "teamsystem", "sito": "TeamSystem"},
    {"nome": "Leonardo", "ats": "workday",
     "host": "leonardocompany.wd3.myworkdayjobs.com", "tenant": "leonardocompany", "sito": "LeonardoCareerSite"},
    {"nome": "Intesa Sanpaolo", "ats": "successfactors", "base": "https://jobs.intesasanpaolo.com"},
    {"nome": "EssilorLuxottica", "ats": "successfactors", "base": "https://careers.essilorluxottica.com"},
    {"nome": "Arval", "ats": "bnp", "entita": "arval"},
    # Tech company italiane simili a TeamSystem (aggiunte il 17/09/2026)
    {"nome": "Cerved", "ats": "workday",
     "host": "cerved.wd3.myworkdayjobs.com", "tenant": "cerved", "sito": "Cerved"},
    {"nome": "Tinexta / InfoCert", "ats": "successfactors", "base": "https://job.tinexta.com"},
    {"nome": "Satispay", "ats": "ashby", "slug": "satispay"},
    {"nome": "Lutech", "ats": "lutech"},
]

# Citta' target come compaiono nei campi "location" di questi ATS.
CITTA = {
    "Genova": ["genova", "genoa"],
    "Milano": ["milano", "milan", "assago", "sesto san giovanni", "segrate", "rho",
               "san donato milanese", "cologno monzese", "peschiera borromeo"],
    "Torino": ["torino", "turin"],
}
PAESE_ITALIA = re.compile(r",\s*IT\b|\bIT\s*-|ital", re.IGNORECASE)
MAX_OFFERTE_PER_AZIENDA = 300

_HEADERS_JSON = {"Content-Type": "application/json", "Accept": "application/json"}


def _sessione():
    return cr.Session(impersonate="chrome124")


# ---------------------------------------------------------------- utilita'

def citta_da_testo(luogo):
    """"Genova"/"Milano"/"Torino" se il luogo le nomina; "Italia" se e' in
    Italia senza citta' target; None se e' chiaramente altrove."""
    l = (luogo or "").lower()
    for citta, varianti in CITTA.items():
        if any(v in l for v in varianti):
            return citta
    if PAESE_ITALIA.search(l) or not l.strip():
        return "Italia"
    return None


def data_da_posted(testo):
    """Workday scrive "Posted 3 Days Ago", "Posted Today", "Posted 30+ Days
    Ago": si traduce in una data ISO. "30+" diventa 31 giorni fa, che il filtro
    di freschezza scarta — corretto, e' un annuncio vecchio."""
    t = (testo or "").lower()
    oggi = date.today()
    if "today" in t or "oggi" in t:
        return oggi.isoformat()
    if "yesterday" in t or "ieri" in t:
        return (oggi - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s*(?:day|giorn)", t)
    if m:
        giorni = int(m.group(1)) + (1 if "+" in t else 0)
        return (oggi - timedelta(days=giorni)).isoformat()
    return ""


def _testo_html(html):
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


# ---------------------------------------------------------------- ATS: Workday

def _workday(sessione, az):
    """POST /wday/cxs/<tenant>/<sito>/jobs pagina a blocchi di 20; il dettaglio
    /wday/cxs/<tenant>/<sito>/<externalPath> ha la descrizione in HTML."""
    base = f"https://{az['host']}/wday/cxs/{az['tenant']}/{az['sito']}"

    def pagina(testo, offset):
        r = sessione.post(f"{base}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": offset,
                                                 "searchText": testo}, headers=_HEADERS_JSON, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"workday HTTP {r.status_code}")
        return r.json()

    def leggi(testo):
        # "total" c'e' solo nella prima risposta: va tenuto da parte, altrimenti
        # la seconda pagina lo legge come 0 e la lettura si ferma a 40.
        d = pagina(testo, 0)
        totale = d.get("total") or 0
        posting, offset = list(d.get("jobPostings") or []), 20
        while offset < min(totale, MAX_OFFERTE_PER_AZIENDA):
            time.sleep(0.3)
            blocco = pagina(testo, offset).get("jobPostings") or []
            if not blocco:
                break
            posting.extend(blocco)
            offset += 20
        return totale, posting

    # Un tenant mondiale (Leonardo: migliaia di posizioni) non si legge tutto:
    # si cerca per citta' target. Un tenant piccolo (TeamSystem: 42) si legge
    # intero, cosi' non si perde chi scrive la sede in modo creativo.
    totale, posting = leggi("")
    if totale > 150:
        posting = []
        for citta in CITTA:
            posting.extend(leggi(citta)[1])
            time.sleep(0.3)

    out, visti = [], set()
    for j in posting:
        path = j.get("externalPath", "")
        if not path or path in visti:
            continue
        visti.add(path)
        out.append({
            "titolo": j.get("title", ""),
            "luogo": j.get("locationsText", ""),
            "data": data_da_posted(j.get("postedOn", "")),
            "link": f"https://{az['host']}/{az['sito']}{path}",
            "_dettaglio": f"{base}{path}",
        })
    return out


def _workday_testo(sessione, offerta):
    r = sessione.get(offerta["_dettaglio"], headers={"Accept": "application/json"}, timeout=30)
    if r.status_code != 200:
        return "", ""
    info = r.json().get("jobPostingInfo") or {}
    luoghi = " ".join(filter(None, [info.get("location", ""),
                                    " ".join(info.get("additionalLocations") or [])]))
    return f"{luoghi}\n{_testo_html(info.get('jobDescription', ''))}", ""


# ---------------------------------------------------------------- ATS: SuccessFactors CSB

def _successfactors(sessione, az):
    """/search/?q=&locationsearch=<citta>: HTML server-side. Ogni annuncio
    compare due volte (layout desktop e mobile): si deduplica per link."""
    out, visti = [], set()
    for citta in ("Genova", "Milano"):
        startrow = 0
        while startrow < MAX_OFFERTE_PER_AZIENDA:
            r = sessione.get(f"{az['base']}/search/", params={"q": "", "locationsearch": citta,
                                                              "startrow": startrow}, timeout=30)
            if r.status_code != 200:
                raise RuntimeError(f"successfactors HTTP {r.status_code}")
            soup = BeautifulSoup(r.text, "html.parser")
            righe = soup.select("tr.data-row")
            if not righe:
                break
            for tr in righe:
                a = tr.select_one("a.jobTitle-link")
                loc = tr.select_one("span.jobLocation")
                dt = tr.select_one("span.jobDate")
                if not a:
                    continue
                link = a.get("href", "")
                link = link if link.startswith("http") else az["base"] + link
                if link in visti:
                    continue
                visti.add(link)
                out.append({"titolo": a.get_text(strip=True), "luogo": loc.get_text(strip=True) if loc else citta,
                            "data": _data_csb(dt.get_text(strip=True) if dt else ""), "link": link})
            startrow += len(righe)
            if len(righe) < 25:
                break
            time.sleep(0.3)
    return out


def _data_csb(testo):
    """Le date CSB sono "16 set 2026" o "Sep 16, 2026" o "16/09/2026"."""
    t = (testo or "").strip()
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", t)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    mesi = {"gen": 1, "feb": 2, "mar": 3, "apr": 4, "mag": 5, "giu": 6, "lug": 7, "ago": 8, "set": 9,
            "ott": 10, "nov": 11, "dic": 12, "jan": 1, "may": 5, "jun": 6, "jul": 7, "aug": 8,
            "sep": 9, "oct": 10, "dec": 12}
    m = re.search(r"(\d{1,2})\s+([a-z]{3})[a-z]*\.?\s+(\d{4})", t, re.IGNORECASE) or \
        re.search(r"([a-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", t, re.IGNORECASE)
    if m:
        g = m.groups()
        giorno, mese, anno = (g[0], g[1], g[2]) if g[0].isdigit() else (g[1], g[0], g[2])
        mm = mesi.get(mese.lower()[:3])
        if mm:
            return f"{anno}-{mm:02d}-{int(giorno):02d}"
    return ""


_RE_DATE_POSTED = re.compile(r'"datePosted"\s*:\s*"(\d{4}-\d{2}-\d{2})')


def _html_testo(sessione, offerta):
    """Testo della pagina di dettaglio e, se c'e', la data dal JSON-LD."""
    r = sessione.get(offerta["link"], timeout=30)
    if r.status_code != 200:
        return "", ""
    m = _RE_DATE_POSTED.search(r.text)
    return _testo_html(r.text), (m.group(1) if m else "")


# ---------------------------------------------------------------- ATS: gruppo BNP Paribas

def _bnp(sessione, az):
    """Pagina di gruppo, una card per offerta. La sede sta nel testo della card
    (citta' e paese): la si passa cosi' com'e' a citta_da_testo."""
    out = []
    for pagina in range(1, 6):
        r = sessione.get(f"https://group.bnpparibas/en/careers/all-job-offers/{az['entita']}",
                         params={"page": pagina}, timeout=30)
        if r.status_code != 200:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select(".card-content")
        if not cards:
            break
        for c in cards:
            h = c.find(["h2", "h3"])
            if not h:
                continue
            a = c.find("a", href=True) or c.find_parent("a", href=True)
            link = a["href"] if a else ""
            link = link if link.startswith("http") else "https://group.bnpparibas" + link
            luogo = c.select_one(".offer-location")
            out.append({"titolo": h.get_text(" ", strip=True),
                        "luogo": luogo.get_text(" ", strip=True) if luogo else "",
                        "data": "", "link": link})
        time.sleep(0.3)
    return out


# ---------------------------------------------------------------- ATS: Ashby

def _ashby(sessione, az):
    """api.ashbyhq.com/posting-api/job-board/<slug>: tutto in un JSON, con
    sede, data e descrizione gia' dentro."""
    r = sessione.get(f"https://api.ashbyhq.com/posting-api/job-board/{az['slug']}",
                     headers={"Accept": "application/json"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"ashby HTTP {r.status_code}")
    out = []
    for j in r.json().get("jobs") or []:
        luogo = " ".join(filter(None, [j.get("location", ""), j.get("workplaceType", "")]))
        out.append({"titolo": j.get("title", ""), "luogo": luogo,
                    "data": str(j.get("publishedAt") or "")[:10], "link": j.get("jobUrl", ""),
                    "_testo": _testo_html(j.get("descriptionHtml") or "")})
    return out


def _testo_incorporato(sessione, offerta):
    return offerta.get("_testo", ""), ""


# ---------------------------------------------------------------- Lutech

def _lutech(sessione, az):
    """Tabella server-side: posizione | profilo | contratto (Ibrido/In sede) |
    sede | area. La modalita' scritta in chiaro si mette nel testo, cosi'
    detect_work_mode la legge senza aprire il dettaglio."""
    r = sessione.get("https://www.lutech.group/it/careers/search", timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"lutech HTTP {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for tr in soup.select("table tbody tr"):
        celle = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        td0 = tr.find("td")
        if len(celle) < 4 or not td0 or not td0.get("data-href"):
            continue
        link = "https://www.lutech.group" + td0["data-href"]
        out.append({"titolo": celle[0], "luogo": celle[3], "data": "", "link": link,
                    "_testo": f"Sede: {celle[3]}. Contratto: {celle[2]}. Profilo: {celle[1]}. Area: {celle[4] if len(celle) > 4 else ''}"})
    return out


def _lutech_testo(sessione, offerta):
    testo, data = _html_testo(sessione, offerta)
    return f"{offerta.get('_testo', '')}\n{testo}", data


LETTORI = {
    "workday": (_workday, _workday_testo),
    "successfactors": (_successfactors, _html_testo),
    "bnp": (_bnp, _html_testo),
    "ashby": (_ashby, _testo_incorporato),
    "lutech": (_lutech, _lutech_testo),
}


# ---------------------------------------------------------------- scraper per la pipeline

def _scraper_class():
    """Costruita a runtime per non importare scraper.py all'import di questo
    modulo (scraper.py carica il CV e i client LLM: pesante, e circolare)."""
    import scraper as S

    class AziendeDiretteScraper(S.BaseScraper):
        """Un portale solo agli occhi della pipeline, molte aziende dentro.
        Gira una volta sola (iterazione Genova) e assegna a ogni offerta la
        citta' letta dalla sede; chi non e' in Italia viene scartato qui."""

        def __init__(self):
            super().__init__("AziendeDirette")

        def scrape(self, city_name, city_config):
            if city_name != "Genova":
                return []
            sessione = _sessione()
            jobs = []
            for az in AZIENDE:
                elenco, testo_di = LETTORI[az["ats"]]
                try:
                    offerte = elenco(sessione, az)
                except Exception as e:
                    logging.warning(f"{az['nome']}: lettura fallita: {type(e).__name__}: {e}")
                    continue
                logging.info(f"{az['nome']}: {len(offerte)} offerte lette")
                for o in offerte:
                    if not S.is_valid_job_title(o["titolo"]):
                        continue
                    citta = citta_da_testo(o["luogo"])
                    if citta is None:
                        continue
                    try:
                        testo, data_dettaglio = testo_di(sessione, o)
                    except Exception:
                        testo, data_dettaglio = "", ""
                    if not o["data"] and data_dettaglio:
                        o["data"] = data_dettaglio
                    (match_level, match_count, work_mode, fetch_status,
                     probabilita, motivazione, testo_completo) = S.calcola_punteggio_e_modalita("", o["luogo"])
                    if testo:
                        testo_completo = f"{o['luogo']} {testo}"
                        work_mode = S.detect_work_mode(testo_completo.lower())
                        probabilita, motivazione = S.valuta_match_candidato(testo_completo)
                        fetch_status = "ok"
                    jobs.append(S.ScrapedJob(
                        o["titolo"], az["nome"], self.portal_name, o["link"], date=o["data"],
                        snippet=o["luogo"], match_level=match_level, match_count=match_count,
                        city=citta, work_mode=work_mode, fetch_status=fetch_status,
                        probabilita=probabilita, motivazione=motivazione, testo_completo=testo_completo))
                    time.sleep(0.4)
            return jobs

    return AziendeDiretteScraper


def AziendeDiretteScraper():
    return _scraper_class()()


# ---------------------------------------------------------------- scoperta ATS

def scopri(url):
    """Riconosce l'ATS di una pagina careers e stampa la riga per AZIENDE."""
    s = _sessione()
    r = s.get(url, timeout=30, allow_redirects=True)
    h = r.text
    m = re.search(r"https://([a-z0-9]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", h + " " + r.url)
    if m:
        tenant, wd, sito = m.groups()
        print(f'{{"nome": "?", "ats": "workday", "host": "{tenant}.{wd}.myworkdayjobs.com", '
              f'"tenant": "{tenant}", "sito": "{sito}"}},')
        return
    if "successfactors" in h.lower() and re.search(r"/search/\?", h):
        base = re.match(r"https://[^/]+", r.url).group(0)
        print(f'{{"nome": "?", "ats": "successfactors", "base": "{base}"}},')
        return
    if "group.bnpparibas" in r.url:
        print('{"nome": "?", "ats": "bnp", "entita": "<slug dalla url all-job-offers/...>"},')
        return
    print("ATS non riconosciuto. Indizi:",
          [k for k in ["myworkdayjobs", "successfactors", "smartrecruiters", "taleo", "icims",
                       "phenom", "oraclecloud", "greenhouse", "lever.co", "teamtailor"] if k in h.lower()])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if "--scopri" in sys.argv:
        scopri(sys.argv[sys.argv.index("--scopri") + 1])
    else:
        s = _sessione()
        for az in AZIENDE:
            elenco, _ = LETTORI[az["ats"]]
            try:
                offerte = elenco(s, az)
            except Exception as e:
                print(f"{az['nome']:18s} ERRORE {type(e).__name__}: {e}")
                continue
            in_italia = [o for o in offerte if citta_da_testo(o["luogo"])]
            print(f"{az['nome']:18s} {len(offerte):4d} offerte, {len(in_italia):4d} in Italia")
            for o in in_italia[:3]:
                print(f"     {o['titolo'][:55]:57s} {o['luogo'][:30]:32s} {o['data']}")
