# -*- coding: utf-8 -*-
"""Una chiamata LLM che non torna mai non deve piu' tenere appeso il run dello scraping (13-17/11/2026).

Dal 13/11/2026 il run "Scraping offerte" si fermava ogni volta subito dopo "Groq ha esaurito il tetto giornaliero": la valutazione passava a Gemini, una chiamata
restava appesa e il budget di 25 minuti, controllato solo TRA un'offerta e l'altra, non scattava mai. Il run arrivava al limite di 160 minuti, GitHub lo cancellava
e lo stato (scritto solo all'ultimo passo) non veniva salvato: nessuna offerta dei portali in mail. Esecuzione:  python test_scraping_senza_blocchi.py
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scraper as S  # noqa: E402

TESTO = "Descrizione dell'annuncio con tutti i requisiti. " * 30


def offerta(titolo, prob=40):
    return S.ScrapedJob(titolo, "Azienda", "LinkedIn", "https://esempio.it/" + titolo.replace(" ", "-"), "2026-11-17", "", "Base", 0, "Milano", "unverified",
                        "ok", prob, "euristica", TESTO, valutato_da="euristica")


class Base(unittest.TestCase):
    def setUp(self):
        self.chiamate = []
        self.sblocco = threading.Event()
        self.patch = [mock.patch.object(S, "valuta_match_semantico", self.llm),
                      mock.patch.object(S, "_get_anthropic_client", lambda: object()),
                      mock.patch.object(S, "CV_TESTO_COMPLETO", "CV di prova"),
                      mock.patch.object(S, "_ULTIMO_MODELLO", "Modello di prova", create=True),
                      mock.patch.object(S, "LIMITE_CHIAMATA_LLM_S", 0.4, create=True)]
        for p in self.patch:
            p.start()

    def tearDown(self):
        self.sblocco.set()          # libera i thread rimasti appesi
        for p in self.patch:
            p.stop()

    def llm(self, testo):
        raise NotImplementedError

    def esegui(self, offerte, scadenza=None, massimo_s=8.0):
        """Esegue la fase in un thread: se si appende, il test fallisce invece di bloccare tutto."""
        esito = {}

        def corpo():
            esito["out"] = S.arricchisci_offerte_con_llm(offerte, scadenza=scadenza)
        t0 = time.monotonic()
        t = threading.Thread(target=corpo, daemon=True)
        t.start()
        t.join(massimo_s)
        esito["durata"] = time.monotonic() - t0
        esito["appesa"] = t.is_alive()
        return esito


class TestChiamataAppesa(Base):
    def llm(self, testo):
        self.chiamate.append(testo)
        self.sblocco.wait(60)       # simula la chiamata che non torna
        return (70, "Mai raggiunta.", "ibrido", "", "")

    def test_una_chiamata_che_non_torna_non_blocca_la_fase(self):
        e = self.esegui([offerta("Marketing Manager")], scadenza=time.monotonic() + 600)
        self.assertFalse(e["appesa"], "la fase di valutazione e' rimasta appesa sulla chiamata")
        self.assertLess(e["durata"], 5)

    def test_l_offerta_della_chiamata_appesa_tiene_il_punteggio_euristico_e_non_e_filtrata(self):
        e = self.esegui([offerta("Marketing Manager", prob=40)], scadenza=time.monotonic() + 600)
        self.assertFalse(e["appesa"])
        self.assertEqual(len(e["out"]), 1)
        self.assertEqual((e["out"][0].probabilita, e["out"][0].valutato_da), (40, "euristica"))

    def test_dopo_due_chiamate_appese_di_fila_il_fornitore_e_considerato_morto_e_si_smette(self):
        offerte = [offerta(f"Marketing Manager {i}") for i in range(6)]
        e = self.esegui(offerte, scadenza=time.monotonic() + 600)
        self.assertFalse(e["appesa"])
        self.assertEqual(len(self.chiamate), 2)                     # non 6 attese da 5 minuti l'una
        self.assertEqual(len(e["out"]), 6)                          # nessuna offerta persa
        self.assertTrue(all(j.valutato_da != "llm" for j in e["out"]))


class TestComportamentoNormale(Base):
    def llm(self, testo):
        self.chiamate.append(testo)
        time.sleep(0.05)
        return (70, "Valutazione vera.", "ibrido", "", "")

    def test_con_un_fornitore_che_risponde_nulla_cambia(self):
        e = self.esegui([offerta("A"), offerta("B")], scadenza=time.monotonic() + 600)
        self.assertEqual(len(self.chiamate), 2)
        self.assertEqual([(j.valutato_da, j.probabilita) for j in e["out"]], [("llm", 70), ("llm", 70)])

    def test_senza_scadenza_la_chiamata_si_aspetta_come_prima(self):
        e = self.esegui([offerta("A")], scadenza=None)
        self.assertEqual(e["out"][0].valutato_da, "llm")


class TestChiamataLentaMaViva(Base):
    def llm(self, testo):
        self.chiamate.append(testo)
        time.sleep(0.25)            # lenta ma sotto il limite: non va scambiata per appesa
        return (66, "Lenta ma valida.", "ibrido", "", "")

    def test_una_chiamata_lenta_ma_sotto_il_limite_viene_valutata(self):
        e = self.esegui([offerta("A")], scadenza=time.monotonic() + 600)
        self.assertEqual((e["out"][0].valutato_da, e["out"][0].probabilita), ("llm", 66))


class TestErroreDelFornitore(Base):
    def llm(self, testo):
        self.chiamate.append(testo)
        raise RuntimeError("503")

    def test_un_errore_resta_gestito_come_prima_e_non_ferma_le_altre(self):
        e = self.esegui([offerta("A"), offerta("B")], scadenza=time.monotonic() + 600)
        self.assertEqual(len(self.chiamate), 2)
        self.assertEqual(len(e["out"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
