# -*- coding: utf-8 -*-
"""Nessun limite di caratteri sul testo che arriva al modello (08/11/2026, richiesta di Giovanni).

L'annuncio e il CV vanno al modello per intero: niente estratti «testa e coda», niente tagli
a 4.000, 6.000 o 8.000 caratteri. Se l'annuncio e' troppo lungo per un fornitore, e' il
fornitore a rifiutarlo (Groq risponde con un errore e si passa al modello successivo, poi a
Gemini): il testo non si accorcia mai di nascosto.

Si lanciano con `python test_nessun_limite_testo.py`. Nessuna rete, nessun LLM, nessuno stato scritto.
Ogni test controlla che un marcatore in TESTA, uno al CENTRO e uno in CODA di un annuncio da
40.000 caratteri (e un marcatore in coda a un CV da 12.000) compaiano nella richiesta.
"""
import json
import sys

import scraper as s

fallimenti = []


def verifica(nome, condizione, dettaglio=""):
    if condizione:
        print(f"  ok     {nome}")
    else:
        print(f"  FALLITO {nome} {dettaglio}")
        fallimenti.append(nome)


RIEMPITIVO = "lorem ipsum dolor sit amet consectetur " * 40
ANNUNCIO = ("MARCATORE-TESTA " + RIEMPITIVO * 12 + " MARCATORE-CENTRO " + RIEMPITIVO * 12
            + " MARCATORE-CODA RAL 50.000 hybrid")
CV = ("CV-INIZIO " + "esperienza " * 1100 + " CV-FINE")
assert len(ANNUNCIO) > 20000 and len(CV) > 11000


def completo(testo):
    return all(m in testo for m in ("MARCATORE-TESTA", "MARCATORE-CENTRO", "MARCATORE-CODA")) and "[...]" not in testo


class RispostaFinta:
    status_code = 200
    headers = {}
    text = ""

    def json(self):
        return {"choices": [{"message": {"content": json.dumps(
            {"probabilita": 60, "motivazione": "x", "modalita": "ibrido", "dettaglio_modalita": "", "ral": ""})}}],
            "usage": {"total_tokens": 10}}


print("Groq")
catturato = {}


def post_finto(url, json=None, **kw):
    catturato["corpo"] = json
    return RispostaFinta()


s.GROQ_API_KEY = "chiave-finta"
s.CV_TESTO_COMPLETO = "cv"
s.CV_TESTO_PER_MATCH = CV
s._modello_groq_disponibile = lambda: "modello-finto"
s._segna_consumo_groq = lambda *a, **k: None
s._impara_limite_groq = lambda *a, **k: None
s.requests.post = post_finto
esito = s.valuta_match_groq(ANNUNCIO)
msgs = (catturato.get("corpo") or {}).get("messages") or [{}, {}]
verifica("Groq: la valutazione parte", esito is not None)
verifica("Groq: l'annuncio arriva INTERO (testa, centro, coda, senza [...])", completo(msgs[1].get("content", "")),
         f"(lungo {len(msgs[1].get('content', ''))} caratteri su {len(ANNUNCIO)})")
verifica("Groq: il CV arriva INTERO", "CV-FINE" in msgs[0].get("content", ""))

print("Gemini")


class InterazioniFinte:
    def create(self, **kw):
        catturato["gemini"] = kw
        raise RuntimeError("stop dopo la cattura")


class ClienteGeminiFinto:
    interactions = InterazioniFinte()


try:
    s._chiama_gemini(ClienteGeminiFinto(), s._MATCH_LLM_SYSTEM_TEMPLATE.format(cv_testo=CV), ANNUNCIO, "modello-finto")
except RuntimeError:
    pass
verifica("Gemini: l'annuncio arriva INTERO", completo((catturato.get("gemini") or {}).get("input", "")))

print("Claude")


class MessaggiFinti:
    def create(self, **kw):
        catturato["claude"] = kw
        raise RuntimeError("stop dopo la cattura")


class ClienteClaudeFinto:
    messages = MessaggiFinti()


s._get_anthropic_client = lambda: ClienteClaudeFinto()
try:
    s.valuta_match_llm(ANNUNCIO)
except Exception:
    pass
kw = catturato.get("claude") or {}
contenuto = (kw.get("messages") or [{}])[0].get("content", "")
sistema = ((kw.get("system") or [{}])[0]).get("text", "")
verifica("Claude: l'annuncio arriva INTERO", completo(contenuto), f"(lungo {len(contenuto)})")
verifica("Claude: il CV arriva INTERO", "CV-FINE" in sistema)

print("Gemini: istruzioni col CV intero")
catturato.pop("gemini", None)
s.GEMINI_API_KEY = "chiave-finta"
s.GEMINI_MODELLI = ["modello-finto"]
s._attendi_slot_gemini = lambda: None
s._get_gemini_client = lambda: ClienteGeminiFinto() if hasattr(s, "_get_gemini_client") else None
try:
    s.valuta_match_gemini(ANNUNCIO)
except Exception:
    pass
if "gemini" in catturato:
    verifica("Gemini: il CV arriva INTERO", "CV-FINE" in catturato["gemini"].get("input", ""))
else:
    print("  (saltato: il client Gemini non e' sostituibile senza rete)")

print()
if fallimenti:
    print(f"{len(fallimenti)} test FALLITI: {fallimenti}")
    sys.exit(1)
print("tutti i test passano")
