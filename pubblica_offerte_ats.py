# -*- coding: utf-8 -*-
"""Consegna alla mail unica "Job Alert" le offerte degli ATS diretti gia' VALUTATE (09/11/2026).

Aggiunge le voci a offerte_giornaliere.json (la coda che la mail delle 18:05 invia e svuota) e i loro link a offerte_viste.json, nel repo su GitHub,
come fa uno scraping: commit + push. Non tocca workflow ne' codice. Lavora in un worktree temporaneo su origin/main (il tuo albero locale
con le modifiche in corso non viene toccato). Se il push viene rifiutato perche' un altro run ha scritto nel frattempo, riparte da origin/main
aggiornato e ricalcola (i criteri sui doppioni vedono la coda nuova): fino a 5 tentativi.

Uso:  python pubblica_offerte_ats.py <cartella_giro_con_candidati_e_valutazioni>            prova: mostra cosa farebbe, NON pubblica
      python pubblica_offerte_ats.py <cartella_giro> --invia                              pubblica
      opzione --remoto <url_o_percorso>  (per i test: un repo locale al posto di GitHub)
Dopo la pubblicazione segna come viste (segna_viste.py) le offerte valutate del giro.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import shutil

QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, QUI)

COMMIT_FINE = "\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"


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


def costruisci(wt, candidati, valutazioni):
    """Dentro il worktree (cwd = wt): calcola cosa aggiungere con i criteri dei portali. Ritorna (voci, ids, scartate, coda, viste)."""
    os.chdir(wt)
    import coda_ats as C
    coda = leggi(os.path.join(wt, "offerte_giornaliere.json"), [])
    viste = leggi(os.path.join(wt, "offerte_viste.json"), {})
    voci, ids, scartate = C.prepara(candidati, valutazioni, set(viste), coda)
    return voci, ids, scartate, coda, viste


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cartella")
    ap.add_argument("--invia", action="store_true")
    ap.add_argument("--remoto", default="origin")
    ap.add_argument("--ramo", default="main")
    a = ap.parse_args()
    cart = os.path.abspath(a.cartella)
    candidati = leggi(os.path.join(cart, "candidati.json"), [])
    valutazioni = leggi(os.path.join(cart, "valutazioni.json"), {})
    if not candidati or not valutazioni:
        sys.exit("Mancano candidati.json o valutazioni.json nella cartella del giro: nessuna pubblicazione.")
    wt = tempfile.mkdtemp(prefix="wt_ats_")
    shutil.rmtree(wt)
    esito = None
    try:
        for tentativo in range(1, 6):
            git("fetch", a.remoto, a.ramo, cwd=QUI)
            if tentativo == 1:
                git("worktree", "add", "--detach", wt, f"{a.remoto}/{a.ramo}", cwd=QUI)
            else:
                git("reset", "--hard", f"{a.remoto}/{a.ramo}", cwd=wt)
            voci, ids, scartate, coda, viste = costruisci(wt, candidati, valutazioni)
            os.chdir(QUI)
            print(f"tentativo {tentativo}: da aggiungere {len(voci)} | scartate {len(scartate)} | coda attuale {len(coda)} voci")
            for j, motivo in scartate:
                print(f"   scartata: {j.company} | {j.title} | {motivo}")
            for v in voci:
                print(f"   AGGIUNGO {v['probabilita']:3}% {v['company']} | {v['title']} | {v['city']} | {v['link']}")
            if not a.invia:
                print("PROVA: nessuna pubblicazione (manca --invia).")
                return
            if not voci:
                print("Niente da pubblicare.")
                esito = "vuoto"
                break
            scrivi(os.path.join(wt, "offerte_giornaliere.json"), coda + voci)
            now = time.time()
            for i in ids:
                viste[i] = now
            scrivi(os.path.join(wt, "offerte_viste.json"), viste)
            git("add", "offerte_giornaliere.json", "offerte_viste.json", cwd=wt)
            git("commit", "-m", f"ATS diretti: {len(voci)} offerte valutate per la mail unica [skip ci]{COMMIT_FINE}", cwd=wt)
            r = git("push", a.remoto, f"HEAD:{a.ramo}", cwd=wt, check=False)
            if r.returncode == 0:
                print(f"PUBBLICATE {len(voci)} offerte (tentativo {tentativo}).")
                esito = "ok"
                break
            print("push rifiutato (un altro run ha scritto): riparto da origin aggiornato.", r.stderr.strip()[:120])
            time.sleep(3)
        else:
            sys.exit("Push non riuscito dopo 5 tentativi: nulla e' stato pubblicato.")
    finally:
        os.chdir(QUI)
        subprocess.run(["git", "worktree", "remove", "--force", wt], cwd=QUI, capture_output=True)
        shutil.rmtree(wt, ignore_errors=True)
    if esito in ("ok", "vuoto"):                     # valutate = viste, anche quelle scartate come doppioni (non devono tornare domani)
        sys.path.insert(0, os.path.join(QUI, "ats_diretto"))
        import segna_viste
        print(f"segnate come viste in locale {segna_viste.segna(cart)} offerte valutate ({segna_viste.file_visti(cart)})")


if __name__ == "__main__":
    main()
