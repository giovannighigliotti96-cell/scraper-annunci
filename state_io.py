"""Utility di I/O JSON condivise tra scraper.py e gli step "Salva memoria"/"Salva
stato post-email" dei workflow GitHub Actions (scraping.yml, email.yml).

Prima di questo modulo, la stessa logica di caricamento sicuro e scrittura
atomica era duplicata in tre punti indipendenti (scraper.py + due heredoc
Python embedded nei workflow YAML) e già diventata fonte di bug ricorrenti
nelle code review di questo progetto: un fix applicato in un punto non si
propagava agli altri due. Nessuna dipendenza esterna (solo stdlib), così può
essere importato sia dal processo Python normale sia dagli step dei workflow
(che eseguono `python3 -` dalla working directory del checkout).
"""
import json
import os
import tempfile


def load_json_or_raise(path, default):
    """Ritorna `default` SOLO se il file non esiste. Se il file esiste ma è
    illeggibile/corrotto, l'eccezione (es. json.JSONDecodeError) si propaga
    invece di essere scambiata silenziosamente per "file assente" — un file
    di stato reale corrotto non va mai sostituito con un default vuoto senza
    che chi chiama se ne accorga."""
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def atomic_write_json(path, data, **json_kwargs):
    """Scrive JSON in modo atomico: prima su un file temporaneo nella stessa
    cartella, poi rename. Un processo ucciso a metà scrittura (es. GitHub
    Actions con concurrency: cancel-in-progress) non lascia mai il file di
    destinazione troncato/corrotto — os.replace è atomico sia su POSIX che
    su Windows quando sorgente e destinazione sono sullo stesso filesystem."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, **json_kwargs)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
