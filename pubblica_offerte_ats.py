# -*- coding: utf-8 -*-
"""Collega il giro degli ATS diretti (in locale) alla mail unica "Job Alert" (09/11/2026).

Due usi, entrambi con commit + push su origin/main, in un worktree temporaneo (il tuo albero locale non viene toccato):

  python pubblica_offerte_ats.py --atteso [--invia]
      Dichiara che gli ATS di oggi sono ATTESI (ats_stato.json). La mail delle 18:05 aspetta finche' non sono consegnati
      (al piu' tardi fino alle 23:30). Lo chiama il giro ATS appena parte.

  python pubblica_offerte_ats.py <cartella_giro> [--invia] [--non-avviare-mail]
      Consegna: aggiunge le offerte VALUTATE (candidati.json + valutazioni.json della cartella) a offerte_giornaliere.json e i loro link a
      offerte_viste.json, e nello STESSO commit porta ats_stato.json a "consegnato" (anche con zero offerte: la mail non deve aspettare invano).
      Poi, se la mail di oggi non e' ancora partita, fa partire il workflow email.yml (gh workflow run) cosi' arriva subito.
      Segna come viste in locale le offerte valutate.

Senza --invia mostra cosa farebbe e non pubblica niente. Se il push viene rifiutato (un altro run ha scritto nel frattempo) riparte da
origin/main aggiornato e ricalcola: fino a 5 tentativi. --remoto/--ramo servono ai test (un repository locale al posto di GitHub).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo

QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, QUI)

COMMIT_FINE = "\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
REPO_GITHUB = "giovannighigliotti96-cell/scraper-annunci"


def git(*args, cwd=None, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r


def leggi(p, default):
    try:
        return json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError):
        return default


def rientro(p, default=2):
    """Rientro (spazi) con cui il file e' gia' scritto nel repo: si mantiene, cosi' il commit aggiunge righe senza riscrivere tutto."""
    try:
        righe = open(p, encoding="utf-8").read().splitlines()
        for r in righe[1:6]:
            if r.strip():
                return len(r) - len(r.lstrip(" "))
    except OSError:
        pass
    return default


def scrivi(p, obj, indent=None):
    indent = indent or rientro(p)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, p)


def scrivi_stato(wt, stato, offerte=0):
    import scraper as S
    adesso = datetime.now(ZoneInfo("Europe/Rome"))
    scrivi(os.path.join(wt, "ats_stato.json"), {"giornata": S.giornata_riepilogo(adesso), "stato": stato, "offerte": offerte,
                                                 "aggiornato": adesso.strftime("%Y-%m-%d %H:%M:%S")}, 2)


def applica_atteso(wt, **_):
    scrivi_stato(wt, "atteso")
    print("ats_stato.json -> atteso (la mail aspetta gli ATS)")
    return "ATS diretti attesi per la mail di oggi", 0


def applica_consegna(wt, candidati, valutazioni):
    """Dentro il worktree: calcola cosa aggiungere con i criteri dei portali e scrive coda, viste e stato 'consegnato'."""
    os.chdir(wt)
    import coda_ats as C
    coda = leggi(os.path.join(wt, "offerte_giornaliere.json"), [])
    viste = leggi(os.path.join(wt, "offerte_viste.json"), {})
    voci, ids, scartate = C.prepara(candidati, valutazioni, set(viste), coda)
    os.chdir(QUI)
    print(f"da aggiungere {len(voci)} | scartate {len(scartate)} | coda attuale {len(coda)} voci")
    for j, motivo in scartate:
        print(f"   scartata: {j.company} | {j.title} | {motivo}")
    for v in voci:
        print(f"   AGGIUNGO {v['probabilita']:3}% {v['company']} | {v['title']} | {v['city']} | {v['link']}")
    if voci:
        scrivi(os.path.join(wt, "offerte_giornaliere.json"), coda + voci)
        now = time.time()
        for i in ids:
            viste[i] = now
        scrivi(os.path.join(wt, "offerte_viste.json"), viste)
    scrivi_stato(wt, "consegnato", len(voci))
    return f"ATS diretti: {len(voci)} offerte valutate per la mail unica", len(voci)


def pubblica(applica, invia, remoto, ramo, **dati):
    """Esegue `applica` su origin/main aggiornato, commit e push, con nuovi tentativi. Ritorna (descrizione, n, worktree_ultimo_stato)."""
    wt = tempfile.mkdtemp(prefix="wt_ats_")
    shutil.rmtree(wt)
    try:
        for tentativo in range(1, 6):
            git("fetch", remoto, ramo, cwd=QUI)
            if tentativo == 1:
                git("worktree", "add", "--detach", wt, f"{remoto}/{ramo}", cwd=QUI)
            else:
                git("reset", "--hard", f"{remoto}/{ramo}", cwd=wt)
            print(f"tentativo {tentativo}:")
            descrizione, n = applica(wt, **dati)
            if not invia:
                print("PROVA: nessuna pubblicazione (manca --invia).")
                return None
            git("add", "-A", cwd=wt)
            git("commit", "-m", f"{descrizione} [skip ci]{COMMIT_FINE}", cwd=wt)
            r = git("push", remoto, f"HEAD:{ramo}", cwd=wt, check=False)
            if r.returncode == 0:
                print(f"PUBBLICATO: {descrizione} (tentativo {tentativo}).")
                ultimo = leggi(os.path.join(wt, "ultimo_invio.json"), {})
                return descrizione, n, ultimo
            print("push rifiutato (un altro run ha scritto): riparto da origin aggiornato.", r.stderr.strip()[:120])
            time.sleep(3)
        sys.exit("Push non riuscito dopo 5 tentativi: nulla e' stato pubblicato.")
    finally:
        os.chdir(QUI)
        subprocess.run(["git", "worktree", "remove", "--force", wt], cwd=QUI, capture_output=True)
        shutil.rmtree(wt, ignore_errors=True)


def avvia_mail(ultimo_invio):
    import scraper as S
    if ultimo_invio.get("giornata") == S.giornata_riepilogo():
        print("La mail di oggi e' gia' partita: non faccio partire il workflow.")
        return
    r = subprocess.run(["gh", "workflow", "run", "email.yml", "-R", REPO_GITHUB], capture_output=True, text=True, encoding="utf-8", errors="replace")
    print("workflow email.yml avviato" if r.returncode == 0 else f"ATTENZIONE: workflow email.yml NON avviato: {r.stderr.strip()[:200]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cartella", nargs="?")
    ap.add_argument("--atteso", action="store_true")
    ap.add_argument("--invia", action="store_true")
    ap.add_argument("--non-avviare-mail", action="store_true")
    ap.add_argument("--remoto", default="origin")
    ap.add_argument("--ramo", default="main")
    a = ap.parse_args()
    if a.atteso:
        pubblica(applica_atteso, a.invia, a.remoto, a.ramo)
        return
    if not a.cartella:
        sys.exit("Indica la cartella del giro, oppure --atteso.")
    cart = os.path.abspath(a.cartella)
    candidati = leggi(os.path.join(cart, "candidati.json"), [])
    valutazioni = leggi(os.path.join(cart, "valutazioni.json"), {})
    if not candidati or not valutazioni:
        sys.exit("Mancano candidati.json o valutazioni.json nella cartella del giro: nessuna consegna.")
    esito = pubblica(applica_consegna, a.invia, a.remoto, a.ramo, candidati=candidati, valutazioni=valutazioni)
    if esito is None:
        return
    sys.path.insert(0, os.path.join(QUI, "ats_diretto"))
    import segna_viste                                  # valutate = viste, anche quelle scartate come doppioni (non devono tornare domani)
    print(f"segnate come viste in locale {segna_viste.segna(cart)} offerte valutate ({segna_viste.file_visti(cart)})")
    if not a.non_avviare_mail:
        avvia_mail(esito[2])


if __name__ == "__main__":
    main()
