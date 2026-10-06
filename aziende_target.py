#!/usr/bin/env python3
"""Monitoraggio delle pagine "lavora con noi" di una lista di aziende target.

Perché esiste: i tredici portali coprono ciò che viene pubblicato sui canali
pubblici, ma il target con il fit più alto — PMI strutturate di Genova e Milano
che devono ancora costruire la funzione commerciale — spesso non pubblica lì.
O pubblica solo sul proprio sito, o non pubblica affatto e assume su
candidatura spontanea. Verificato dal vivo il 10/09/2026: fra le aziende
arrivate dagli annunci, quelle piccole non hanno nemmeno un percorso careers
standard, mentre le scale-up usano ATS di terze parti.

Cosa produce, in ordine di valore:
1. le offerte trovate sul sito aziendale che NON sono già arrivate dai portali;
2. quando non ci sono offerte aperte, il link alla pagina per l'autocandidatura,
   segnalato UNA VOLTA per azienda — che per una PMI è spesso l'unico modo di
   entrare, e arriva prima che l'annuncio esista.

La lista vive in aziende_target.json ed è pensata per essere ampliata a mano.
"""
import json
import logging
import os
import re
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor

import state_io

AZIENDE_FILE = "aziende_target.json"
STATO_AZIENDE_FILE = "stato_aziende.json"

# Quante aziende controllare per ogni run. Con 359 aziende in lista e 4 run al
# giorno, una rotazione da 50 le copre tutte in poco meno di due giorni senza
# mai fare centinaia di richieste in blocco: nessun sito viene martellato e il
# run non si allunga. I primi giri sono i piu' lenti perche' devono ancora
# scoprire dove sta la pagina careers; dopo, ogni azienda costa una richiesta.
AZIENDE_PER_RUN = 50

# Ogni quanto riproporre l'autocandidatura di un'azienda che non ha offerte
# aperte. Una volta segnalata, ripeterla ogni giorno sarebbe solo rumore.
GIORNI_RIPETI_AUTOCANDIDATURA = 120

# Percorsi con cui i siti italiani espongono le posizioni aperte, in ordine di
# frequenza osservata. Si prova solo la prima volta: una volta trovata, la
# pagina viene memorizzata nello stato e i controlli successivi vanno diretti.
PERCORSI_CAREERS = [
    "/lavora-con-noi", "/careers", "/carriere", "/jobs", "/posizioni-aperte",
    "/it/lavora-con-noi", "/it/careers", "/chi-siamo/lavora-con-noi",
    "/azienda/lavora-con-noi", "/work-with-us", "/join-us", "/opportunita",
]

# ATS di terze parti e come leggerne le offerte. Lo slug dell'azienda non è
# deducibile dal nome: si estrae dalla pagina careers, dove l'ATS è incorporato.
ATS = {
    "greenhouse": (r"boards\.greenhouse\.io/([a-zA-Z0-9_-]+)",
                   "https://boards-api.greenhouse.io/v1/boards/{}/jobs"),
    "lever": (r"jobs\.lever\.co/([a-zA-Z0-9_-]+)",
              "https://api.lever.co/v0/postings/{}?mode=json"),
    "recruitee": (r"([a-zA-Z0-9-]+)\.recruitee\.com",
                  "https://{}.recruitee.com/api/offers/"),
    "workable": (r"apply\.workable\.com/([a-zA-Z0-9_-]+)",
                 "https://apply.workable.com/api/v1/widget/accounts/{}?details=true"),
    "inrecruiting": (r"([a-zA-Z0-9-]+)\.inrecruiting\.(?:it|com)",
                     "https://{}.inrecruiting.it/jobs.json"),
}

_HEADERS = {
    "Accept-Language": "it-IT,it;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _headers():
    """User-Agent condiviso con gli scraper dei portali, importato al volo per
    non creare una dipendenza circolare fra i due moduli."""
    from scraper import USER_AGENT_CHROME
    return {"User-Agent": USER_AGENT_CHROME, **_HEADERS}


def carica_aziende():
    """Lista delle aziende da monitorare. Ogni voce: nome, sito, città."""
    try:
        dati = state_io.load_json_or_raise(AZIENDE_FILE, [])
        return dati if isinstance(dati, list) else []
    except Exception as e:
        logging.error(f"Errore lettura {AZIENDE_FILE}: {e}")
        return []


def carica_stato():
    try:
        dati = state_io.load_json_or_raise(STATO_AZIENDE_FILE, {})
        return dati if isinstance(dati, dict) else {}
    except Exception as e:
        logging.error(f"Errore lettura {STATO_AZIENDE_FILE}: {e}")
        return {}


def salva_stato(stato):
    try:
        state_io.atomic_write_json(STATO_AZIENDE_FILE, stato, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Errore scrittura {STATO_AZIENDE_FILE}: {e}")


def trova_pagina_careers(sito):
    """Cerca la pagina delle posizioni aperte provando i percorsi noti.
    Ritorna (url, html) oppure (None, None). Si accontenta della prima risposta
    valida: provare tutti i percorsi per ogni azienda moltiplicherebbe le
    richieste senza aggiungere nulla."""
    base = sito.rstrip("/")
    for percorso in PERCORSI_CAREERS:
        try:
            r = requests.get(base + percorso, headers=_headers(), timeout=12, allow_redirects=True)
            # Molti siti rispondono 200 con la home su URL inesistenti: si chiede
            # anche che la pagina parli davvero di lavoro, altrimenti si finisce
            # per memorizzare la home come "pagina careers".
            if r.status_code != 200 or len(r.text) < 2000:
                continue
            # Soft 404: molti siti rispondono 200 servendo una pagina di errore.
            # Verificato dal vivo su satispay.com, che restituiva 200 su
            # /lavora-con-noi ma reindirizzava a /404.html — e senza questo
            # controllo la pagina d'errore finiva memorizzata come careers.
            if re.search(r"/404|/errore|/not-found|/page-not-found", r.url, re.I):
                continue
            testo = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True).lower()
            if re.search(r"pagina non trovata|page not found|errore 404|404 error", testo):
                continue
            if any(k in testo for k in ("lavora con noi", "posizioni aperte", "careers",
                                        "candidatura", "candidati", "opportunità",
                                        "join our", "we are hiring", "open positions")):
                return r.url, r.text
        except Exception:
            continue
    return None, None


def _offerte_da_jsonld(html):
    """Offerte dichiarate in JSON-LD schema.org. È il formato più affidabile ma
    il meno diffuso: su un campione di PMI e scale-up italiane non lo usava
    nessuna, quindi resta un percorso opportunistico."""
    titoli = []
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            dati = json.loads(script.string or "{}")
        except Exception:
            continue
        for item in (dati if isinstance(dati, list) else [dati]):
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                titolo = item.get("title")
                if titolo:
                    titoli.append((str(titolo).strip(), item.get("url", ""),
                                   _luogo_da_valore(item.get("jobLocation"))))
    return titoli


def _luogo_da_valore(valore):
    """Riduce a testo il campo "sede" di un annuncio, qualunque forma abbia.

    Ogni ATS lo scrive a modo suo: una stringa ("Milano"), un oggetto
    ({"name": "Berlin"}), un oggetto annidato schema.org ({"address":
    {"addressLocality": "Wien", "addressCountry": "AT"}}), o una lista di
    sedi. Qui si raccolgono tutte le stringhe che contiene, in un unico testo
    minuscolo: a decidere se e' Italia pensa poi _offerta_in_italia."""
    pezzi = []

    def raccogli(v):
        if isinstance(v, str):
            pezzi.append(v)
        elif isinstance(v, dict):
            for k, x in v.items():
                if k in ("@type", "@context", "id", "url", "geo", "latitude", "longitude"):
                    continue
                raccogli(x)
        elif isinstance(v, list):
            for x in v:
                raccogli(x)

    raccogli(valore)
    return " ".join(pezzi).lower().strip()


def _offerte_da_ats(html, url_careers=""):
    """Offerte lette dall'API dell'ATS incorporato nella pagina. Copre le
    aziende strutturate, che raramente elencano le posizioni in HTML proprio."""
    titoli = []
    for nome_ats, (pattern, api) in ATS.items():
        # Si cerca anche nell'URL: alcuni siti reindirizzano direttamente al
        # dominio dell'ATS (jobs.azienda.it), dove il nome del fornitore non
        # compare nel corpo della pagina ma solo nell'indirizzo.
        match = re.search(pattern, html) or re.search(pattern, url_careers)
        if not match:
            continue
        slug = match.group(1)
        # Slug generici che compaiono negli URL degli ATS ma non identificano
        # un'azienda: interrogare l'API con questi restituisce sempre errore.
        if slug.lower() in ("www", "app", "api", "static", "assets", "cdn"):
            continue
        try:
            r = requests.get(api.format(slug), headers=_headers(), timeout=15)
            if r.status_code != 200:
                continue
            dati = r.json()
            elenco = dati if isinstance(dati, list) else (
                dati.get("jobs") or dati.get("offers") or dati.get("data") or [])
            for job in elenco:
                if not isinstance(job, dict):
                    continue
                titolo = job.get("title") or job.get("text") or job.get("name")
                link = job.get("absolute_url") or job.get("hostedUrl") or job.get("careers_url") or ""
                # Greenhouse: location.name; Lever: categories.location;
                # Recruitee: location / city / country; Workable: location.{city,country}
                luogo = _luogo_da_valore([job.get("location"), job.get("city"),
                                          job.get("country"), job.get("country_code"),
                                          (job.get("categories") or {}).get("location")
                                          if isinstance(job.get("categories"), dict) else None])
                if titolo:
                    titoli.append((str(titolo).strip(), link, luogo))
            if titoli:
                logging.info(f"Offerte lette da {nome_ats} (slug {slug}): {len(titoli)}")
                return titoli
        except Exception:
            continue
    return titoli


def _offerte_da_html(html, url_base):
    """Ultima risorsa: titoli plausibili estratti dalla pagina. Volutamente
    grossolano — a decidere cosa è pertinente è comunque is_valid_job_title,
    che scarta tutto ciò che non è un ruolo target."""
    soup = BeautifulSoup(html, "html.parser")
    titoli = []
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "a", "li"]):
        testo = tag.get_text(" ", strip=True)
        if 12 <= len(testo) <= 90:
            link = tag.get("href") if tag.name == "a" else ""
            if link and not link.startswith("http"):
                from urllib.parse import urljoin
                link = urljoin(url_base, link)
            titoli.append((testo, link or url_base, ""))
    return titoli


def offerte_pertinenti(html, url_careers):
    """Titoli di offerte che superano il filtro sui ruoli target, provando in
    ordine le tre strade dalla più affidabile alla più approssimativa."""
    from scraper import is_valid_job_title

    candidati = (_offerte_da_jsonld(html)
                 or _offerte_da_ats(html, url_careers)
                 or _offerte_da_html(html, url_careers))
    viste = set()
    risultato = []
    for titolo, link, luogo in candidati:
        chiave = titolo.lower().strip()
        if chiave in viste or not is_valid_job_title(titolo):
            continue
        if not _offerta_in_italia(titolo, luogo):
            logging.info(f"Offerta estera scartata: {titolo} [{luogo or 'sede non indicata'}]")
            continue
        viste.add(chiave)
        risultato.append({"titolo": titolo, "link": link or url_careers})
    return risultato


# Segni nel titolo che tradiscono un annuncio scritto per un altro paese. Le
# formule di parita' di genere tedesche ("(m/w/d)", "(Mensch)") e francesi
# ("(H/F)") sono le piu' frequenti, ma bastano anche le parole comuni.
_TITOLO_ESTERO = re.compile(
    r"\((?:m/w/d|w/m/d|m/f/d|f/m/d|m/w/x|mensch|h/f|f/h|m/f/x|m/f/h)\)"
    r"|\b(?:und|für|fuer|mit|kampagnen|leiter|leitung|mitarbeiter|vertrieb|"
    r"chargé|chargée|responsable|chef de|alternance|stage|prácticas|gerente)\b",
    re.IGNORECASE,
)

# "Italia" nelle lingue degli ATS, piu' la sigla ISO "IT" come parola intera:
# senza il confine di parola combacerebbe con "un-it-ed kingdom".
_ITALIA = re.compile(r"\b(?:ital(?:ia|y|ie|ien)|it)\b")


def _offerta_in_italia(titolo, luogo):
    """False se l'annuncio e' chiaramente per un altro paese.

    Le pagine careers delle multinazionali con sede italiana (WPP Media Srl e'
    il caso reale, 13/09/2026) elencano le posizioni di tutto il gruppo: senza
    questo controllo arrivavano in email undici annunci tedeschi etichettati
    "Milano" solo perche' Milano e' la sede dell'azienda in lista.

    Regola: se la sede e' indicata deve essere italiana (citta' target, altra
    citta' italiana o la parola Italia); se non e' indicata si tiene, a meno che
    il titolo non sia scritto per un altro paese. Il caso "sede assente" resta
    permissivo perche' e' quello delle PMI con pagina HTML semplice, che sono il
    bersaglio di questo monitoraggio e non hanno sedi all'estero."""
    from scraper import CITTA_TARGET_PATTERN, ALTRE_CITTA_ITALIANE

    if _TITOLO_ESTERO.search(titolo):
        return False
    if not luogo:
        return True
    if _ITALIA.search(luogo):
        return True
    if any(v in luogo for varianti in CITTA_TARGET_PATTERN.values() for v in varianti):
        return True
    if any(c in luogo for c in ALTRE_CITTA_ITALIANE):
        return True
    return False


def _gia_arrivata_dai_portali(titolo, nome_azienda):
    """True se la stessa posizione è già stata recapitata da un portale. Il
    senso di questo monitoraggio è trovare ciò che i portali NON danno: senza
    questo controllo la sezione ripeterebbe offerte già lette."""
    from scraper import carica_storico_offerte, _pulisci_per_segnatura

    t = _pulisci_per_segnatura(titolo)
    a = _pulisci_per_segnatura(nome_azienda)
    for voce in carica_storico_offerte():
        if not isinstance(voce, dict):
            continue
        if _pulisci_per_segnatura(voce.get("titolo", "")) == t:
            return True
        # Stessa azienda e titolo molto simile: i portali riscrivono spesso i
        # titoli aggiungendo sede o codice di riferimento.
        if a and _pulisci_per_segnatura(voce.get("azienda", "")) == a and t[:18] in _pulisci_per_segnatura(voce.get("titolo", "")):
            return True
    return False


# Quante aziende interrogare insieme. Il 06/10/2026 questa fase ha preso 20
# minuti su 50 aziende: ognuna prova fino a 12 indirizzi candidati con timeout
# 12s, e il caso peggiore e' l'azienda SENZA pagina careers, che li sbaglia
# tutti e dodici prima di arrendersi. Fanno fino a 600 richieste in fila. Le
# aziende sono indipendenti fra loro, quindi la fila non serviva a nulla.
# Otto e non piu': sono 50 domini diversi, quindi non si insiste su un host solo
# — il rischio di farsi bloccare che consigliava prudenza sui portali qui non
# c'e'.
CAREERS_OPERAI = 8

# Dopo quanti giorni riprovare un'azienda che non ha una pagina careers.
# Il flag "senza_careers" veniva scritto dal 2026 e mai letto da nessuno: quelle
# aziende rifacevano tutti e dodici i tentativi a ogni passaggio nella
# rotazione, ed e' il caso piu' costoso che esista — dodici richieste che
# finiscono tutte in niente. Non si smette per sempre perche' un'azienda puo'
# aprire una pagina "lavora con noi" in qualunque momento: un mese e' un
# compromesso fra non insistere e non perdere una novita'.
GIORNI_RIPROVA_SENZA_CAREERS = 30


def _senza_careers_di_recente(voce, oggi) -> bool:
    """True se si e' gia' verificato di recente che questa azienda non ha una
    pagina careers. Il valore True secco e' il vecchio formato, scritto quando
    il flag non si leggeva: si tratta come "da ricontrollare", cosi' al primo
    passaggio prende una data e da li' in poi funziona."""
    segnato = voce.get("senza_careers")
    if not segnato or segnato is True:
        return False
    try:
        quando = datetime.strptime(str(segnato), "%Y-%m-%d")
    except ValueError:
        return False
    return (oggi - quando).days < GIORNI_RIPROVA_SENZA_CAREERS


def _cerca_careers_in_parallelo(da_controllare, stato):
    """Scarica in parallelo la pagina careers di ogni azienda.

    Ritorna {nome azienda: (url, html)}. Solo rete e parsing: lo stato NON si
    tocca qui, perche' aggiornarlo da piu' thread sarebbe il modo piu' facile di
    corrompere stato_aziende.json. Le decisioni restano nel ciclo sequenziale che
    legge questo dizionario, esattamente come prima."""
    def cerca(azienda):
        nome = azienda.get("nome", "").strip()
        sito = azienda.get("sito", "").strip()
        if not nome or not sito:
            return nome, (None, None)
        # Se un indirizzo careers e' gia' noto si prova quello, che e' una sola
        # richiesta invece di dodici.
        noto = (stato.get(nome, {}) or {}).get("url_careers")
        if noto:
            try:
                r = requests.get(noto, headers=_headers(), timeout=12)
                if r.status_code == 200:
                    return nome, (noto, r.text)
            except Exception:
                pass
        try:
            return nome, trova_pagina_careers(sito)
        except Exception as e:
            logging.error(f"Ricerca careers di {nome} fallita: {e}")
            return nome, (None, None)

    with ThreadPoolExecutor(max_workers=CAREERS_OPERAI) as pool:
        return dict(pool.map(cerca, da_controllare))


def controlla_aziende_target(massimo=AZIENDE_PER_RUN):
    """Controlla una fetta della lista, a rotazione, e ritorna cosa segnalare.

    Ritorna (offerte, autocandidature): la prima lista sono posizioni aperte
    pertinenti e non già viste dai portali, la seconda sono aziende con una
    pagina careers raggiungibile ma senza offerte in target — dove ha senso una
    candidatura spontanea.

    La rotazione serve a non fare 200 richieste in un colpo solo: si parte
    sempre dalle aziende controllate meno di recente.
    """
    aziende = carica_aziende()
    if not aziende:
        return [], []

    stato = carica_stato()
    oggi = datetime.now().strftime("%Y-%m-%d")

    def _ultimo(azienda):
        return (stato.get(azienda.get("nome", ""), {}) or {}).get("ultimo_controllo", "")

    oggi_dt = datetime.now()
    # Fuori dalla rotazione chi si sa non avere una pagina careers: i posti di
    # questo giro vanno ad aziende su cui c'e' qualcosa da trovare.
    candidate = [a for a in aziende
                 if not _senza_careers_di_recente(stato.get(a.get("nome", ""), {}) or {}, oggi_dt)]
    escluse = len(aziende) - len(candidate)
    da_controllare = sorted(candidate, key=_ultimo)[:massimo]
    print(f"  {len(da_controllare)} aziende target: cerco le pagine careers in parallelo"
          f"{f' ({escluse} senza careers, saltate)' if escluse else ''}...",
          flush=True)
    pagine = _cerca_careers_in_parallelo(da_controllare, stato)
    trovate_quante = sum(1 for u, _ in pagine.values() if u)
    print(f"  pagine careers raggiunte: {trovate_quante}/{len(da_controllare)}", flush=True)
    offerte, autocandidature = [], []

    for azienda in da_controllare:
        nome = azienda.get("nome", "").strip()
        sito = azienda.get("sito", "").strip()
        if not nome or not sito:
            continue
        voce = stato.setdefault(nome, {})
        voce["ultimo_controllo"] = oggi

        # La pagina l'ha gia' cercata _cerca_careers_in_parallelo: qui si legge
        # solo il risultato, e tutte le decisioni sullo stato restano in questo
        # ciclo, che e' a thread singolo.
        url_careers, html = pagine.get(nome, (None, None))
        if html is None:
            # Nessuna pagina raggiungibile: si annota CON LA DATA, cosi' il
            # controllo qui sopra puo' saltarla per un mese invece di rifare
            # dodici tentativi a vuoto a ogni giro.
            voce["senza_careers"] = oggi
            continue
        voce["url_careers"] = url_careers
        voce.pop("senza_careers", None)

        try:
            trovate = offerte_pertinenti(html, url_careers)
        except Exception as e:
            logging.error(f"Errore lettura offerte di {nome}: {e}")
            trovate = []

        gia_segnalate = set(voce.get("offerte_segnalate", []))
        nuove = []
        for offerta in trovate:
            if offerta["titolo"].lower() in gia_segnalate:
                continue
            if _gia_arrivata_dai_portali(offerta["titolo"], nome):
                continue
            nuove.append({**offerta, "azienda": nome, "citta": azienda.get("citta", "")})
            gia_segnalate.add(offerta["titolo"].lower())
        voce["offerte_segnalate"] = sorted(gia_segnalate)

        if nuove:
            offerte.extend(nuove)
            continue

        # Nessuna posizione in target: vale la candidatura spontanea, ma solo
        # se non è già stata proposta di recente.
        ultima = voce.get("autocandidatura_segnalata", "")
        proponi = True
        if ultima:
            try:
                proponi = datetime.strptime(ultima, "%Y-%m-%d") < datetime.now() - timedelta(days=GIORNI_RIPETI_AUTOCANDIDATURA)
            except Exception:
                proponi = True
        if proponi:
            autocandidature.append({"azienda": nome, "citta": azienda.get("citta", ""),
                                    "link": url_careers})
            voce["autocandidatura_segnalata"] = oggi

    salva_stato(stato)
    logging.info(f"Aziende target: controllate {len(da_controllare)}, "
                 f"{len(offerte)} offerte nuove, {len(autocandidature)} da autocandidatura.")
    return offerte, autocandidature


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    aziende = carica_aziende()
    print(f"Aziende in lista: {len(aziende)}")
    offerte, auto = controlla_aziende_target()
    print(f"\nOfferte trovate e non ancora viste dai portali: {len(offerte)}")
    for o in offerte:
        print(f"  {o['azienda']} — {o['titolo']}")
        print(f"     {o['link']}")
    print(f"\nAziende dove vale una candidatura spontanea: {len(auto)}")
    for a in auto:
        print(f"  {a['azienda']} ({a['citta']}) -> {a['link']}")
