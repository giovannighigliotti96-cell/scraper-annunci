# -*- coding: utf-8 -*-
"""Le offerte degli ATS diretti, gia' VALUTATE, entrano nella coda della mail unica "Job Alert" (offerte_giornaliere.json) - 09/11/2026.

Non invia nulla e non scrive nulla da solo: `prepara` e' una funzione pura che da candidati + valutazioni produce le voci per la coda,
con GLI STESSI CRITERI dei portali: soglia 50, link gia' visto, ripubblicazioni gia' recapitate (titolo+azienda dallo storico), doppioni nel lotto.
La pubblicazione su git e' in pubblica_offerte_ats.py.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scraper as S  # noqa: E402

PORTALE_ATS = S.PORTALE_ATS
SOGLIA = 50
MODELLO = "Claude Sonnet 5.5 (testo intero dell'annuncio, valutato in chat)"


def _modalita(testo):
    t = (testo or "").strip().lower()
    return "unverified" if t in ("", "non indicata", "n.d.", "non specificata") else t


def voce_coda(o, v):
    """Una voce nel formato di offerte_giornaliere.json (lo stesso di ScrapedJob.to_dict)."""
    p = int(v["p"])
    return {
        "title": o["titolo"], "company": o["azienda"], "portal": PORTALE_ATS, "link": o["link"],
        "date": o.get("data") or "", "snippet": "",
        "match_level": "Alto" if p >= 75 else "Medio" if p >= 60 else "Base", "match_count": 0,
        "city": o["citta"], "work_mode": _modalita(v.get("mod")), "fetch_status": "ok",
        "probabilita": p, "motivazione": v.get("m", ""), "testo_completo": o.get("testo_pieno", ""),
        "ral": v.get("ral", "") or "", "dettaglio_modalita": "", "valutato_da": "llm", "modello": MODELLO, "recruiter": "",
    }


def prepara(candidati, valutazioni, viste, coda, soglia=SOGLIA):
    """-> (voci per la coda, job_id da aggiungere a offerte_viste, scartate [(ScrapedJob, motivo)])"""
    voci = []
    for o in candidati:
        v = valutazioni.get(str(o["n"]))
        if v is not None and int(v["p"]) >= soglia:
            voci.append(voce_coda(o, v))
    jobs = [S.ScrapedJob.from_dict(x) for x in voci]
    firme_coda = set()
    for d in coda:
        try:
            firme_coda.add(S.firma_forte(d.get("title", ""), d.get("company", ""), d.get("city", "")))
        except Exception:
            continue
    # storico delle offerte gia' RECAPITATE: qui il confronto e' sul nome base dell'azienda e sul titolo, senza la citta' (nello storico spesso e' diversa/mancante)
    firme_storico = {S.firma_forte(x.get("titolo", ""), x.get("azienda", ""))[:2] for x in S.carica_storico_offerte()
                     if isinstance(x, dict) and S.firma_forte(x.get("titolo", ""), x.get("azienda", ""))}
    nuove = S.dedup_offerte(jobs, set(viste))              # link gia' visto, ripubblicazione gia' recapitata, doppione nel lotto: come per i portali
    ids = {id(j) for j in nuove}
    tenute, scartate = [], []
    for j in jobs:
        if id(j) not in ids:
            scartate.append((j, "gia' visto, ripubblicazione gia' recapitata o doppione nel lotto"))
            continue
        f = S.firma_forte(j.title, j.company, j.city)
        if f and f[:2] in firme_storico:
            scartate.append((j, "ripubblicazione: stesso titolo e azienda (anche con altra forma societaria) gia' recapitati"))
            continue
        if f and f in firme_coda:
            scartate.append((j, "gia' nella coda di oggi (offerta di portale)"))
            continue
        tenute.append(j)
    return [j.to_dict() for j in tenute], [S.get_job_id(j.link) for j in tenute], scartate
