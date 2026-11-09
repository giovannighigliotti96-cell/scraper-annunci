# -*- coding: utf-8 -*-
"""Il testo di un annuncio non recuperato non deve piu' diventare un "50 neutro" spacciato per valutazione (09/11/2026).

Il 09/11/2026 tre annunci LinkedIn (ALINE, NewPrinces, Poletti Usato) sono arrivati in mail con fetch_status "http_error" e un testo di 31-47 caratteri
(solo titolo e azienda): il modello, vedendo solo il titolo, rispondeva il 50 neutro, e la produzione lo registrava come valutazione "llm"
(mai riprovata, mai filtrata). Esecuzione:  python test_testo_annuncio.py
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scraper as S  # noqa: E402

HTML_BUONO = "<html><body><h1>Titolo</h1><p>" + ("Descrizione dell'annuncio con tutti i requisiti. " * 30) + "</p></body></html>"
URL_LI = "https://it.linkedin.com/jobs/view/senior-global-marketing-manager-at-aline-bond-talent-to-business-4488864779"


class Risposta:
    def __init__(self, codice, testo="", redirect=False):
        self.status_code, self.text, self.is_redirect, self.headers = codice, testo, redirect, {}


class FakeRete:
    """Sostituisce S._safe_get: una lista di risposte (o eccezioni) per chiamata."""
    def __init__(self, *esiti):
        self.esiti, self.url_chiesti = list(esiti), []

    def __call__(self, url, headers, timeout, max_redirects=5):
        self.url_chiesti.append(url)
        e = self.esiti.pop(0) if self.esiti else Risposta(500)
        if isinstance(e, Exception):
            raise e
        return e


class TestScaricaHtml(unittest.TestCase):
    def setUp(self):
        S._budget_retry[0] = 300.0
        self.p = [mock.patch.object(S.time, "sleep", lambda s: None)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def prova(self, rete, url=URL_LI):
        with mock.patch.object(S, "_safe_get", rete):
            return S._scarica_html(url)

    def test_riesce_al_secondo_tentativo_dopo_un_429(self):
        rete = FakeRete(Risposta(429), Risposta(200, HTML_BUONO))
        html, stato = self.prova(rete)
        self.assertEqual((html, stato, len(rete.url_chiesti)), (HTML_BUONO, "ok", 2))

    def test_linkedin_ultima_via_e_l_endpoint_guest_con_l_id_dell_offerta(self):
        rete = FakeRete(Risposta(999), Risposta(429), Risposta(200, HTML_BUONO))
        html, stato = self.prova(rete)
        self.assertEqual(stato, "ok")
        self.assertIn("jobs-guest/jobs/api/jobPosting/4488864779", rete.url_chiesti[-1])

    def test_una_pagina_inesistente_non_si_ritenta(self):
        rete = FakeRete(Risposta(404))
        html, stato = self.prova(rete)
        self.assertEqual((html, stato, len(rete.url_chiesti)), (None, "http_error", 1))

    def test_un_timeout_resta_un_timeout_se_non_si_recupera(self):
        import requests
        rete = FakeRete(requests.exceptions.Timeout(), requests.exceptions.Timeout(), requests.exceptions.Timeout())
        html, stato = self.prova(rete)
        self.assertEqual((html, stato), (None, "timeout"))

    def test_finito_il_tetto_di_tempo_si_prova_una_volta_sola(self):
        S._budget_retry[0] = 0.0
        rete = FakeRete(Risposta(429), Risposta(200, HTML_BUONO))
        html, stato = self.prova(rete)
        self.assertEqual((html, len(rete.url_chiesti)), (None, 1))

    def test_pagina_che_va_bene_al_primo_colpo_una_sola_richiesta(self):
        rete = FakeRete(Risposta(200, HTML_BUONO))
        html, stato = self.prova(rete)
        self.assertEqual((stato, len(rete.url_chiesti)), ("ok", 1))


def offerta(testo, valutato_da="euristica", prob=40):
    return S.ScrapedJob("Senior Global Marketing Manager", "ALINE", "LinkedIn", URL_LI, "2026-11-09", "", "Base", 0, "Milano", "unverified",
                        "http_error", prob, "heuristica", testo, valutato_da=valutato_da)


class TestValutazioneSenzaTesto(unittest.TestCase):
    def setUp(self):
        S._budget_retry[0] = 300.0
        self.chiamate = []

        def finto_llm(testo):
            self.chiamate.append(testo)
            return (70, "Valutazione vera.", "ibrido", "", "")
        self.patch = [mock.patch.object(S.time, "sleep", lambda s: None),
                      mock.patch.object(S, "valuta_match_semantico", finto_llm),
                      mock.patch.object(S, "_get_anthropic_client", lambda: object()),
                      mock.patch.object(S, "CV_TESTO_COMPLETO", "CV di prova"),
                      mock.patch.object(S, "_ULTIMO_MODELLO", "Claude di prova", create=True)]
        for p in self.patch:
            p.start()

    def tearDown(self):
        for p in self.patch:
            p.stop()

    def esegui(self, job, rete):
        with mock.patch.object(S, "_safe_get", rete):
            return S.arricchisci_offerte_con_llm([job])

    def test_testo_mancante_ma_recuperato_al_secondo_giro_si_valuta_il_testo_intero(self):
        job = offerta("ALINE Senior Global Marketing Manager")
        out = self.esegui(job, FakeRete(Risposta(429), Risposta(200, HTML_BUONO)))
        self.assertEqual(len(self.chiamate), 1)
        self.assertGreater(len(self.chiamate[0]), 1000)                 # al modello arriva l'annuncio, non il titolo
        self.assertEqual((out[0].valutato_da, out[0].probabilita), ("llm", 70))

    def test_testo_che_non_si_recupera_non_va_al_modello_e_non_diventa_un_50_neutro(self):
        job = offerta("ALINE Senior Global Marketing Manager", prob=40)
        out = self.esegui(job, FakeRete(Risposta(429), Risposta(429), Risposta(429)))
        self.assertEqual(self.chiamate, [])                              # nessuna valutazione dal solo titolo
        self.assertNotEqual(out[0].valutato_da, "llm")                   # cosi' alle 18:05 viene riprovata
        self.assertEqual(out[0].probabilita, 40)                         # punteggio euristico invariato
        self.assertIn("non recuperato", out[0].motivazione.lower())
        self.assertEqual(len(out), 1)                                    # e non viene scartata

    def test_un_annuncio_con_testo_completo_non_cambia_comportamento(self):
        job = offerta(HTML_BUONO * 2, prob=40)
        rete = FakeRete()
        out = self.esegui(job, rete)
        self.assertEqual((len(self.chiamate), len(rete.url_chiesti), out[0].valutato_da), (1, 0, "llm"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
