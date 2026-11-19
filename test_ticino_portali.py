# -*- coding: utf-8 -*-
"""Ricerca allargata al Canton Ticino (19/11/2026), lato portali (scraper.py): LinkedIn cerca anche in Ticino, la sede Ticino e' una sede target come Milano
e Genova, Zurigo/Ginevra no. Nessuna rete, nessuna mail. Esecuzione:  python test_ticino_portali.py"""
import os
import sys
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scraper as S  # noqa: E402

OGGI = date.today().isoformat()


def offerta(titolo, citta, testo="", modo="in sede", snippet=""):
    return S.ScrapedJob(titolo, "Azienda", "LinkedIn", "https://esempio.it/" + titolo.replace(" ", "-"), OGGI, snippet, "Base", 0, citta, modo, "ok", 50, "euristica", testo)


class TestConfigurazioneTicino(unittest.TestCase):
    def test_il_ticino_e_tra_le_citta_cercate(self):
        self.assertIn("Ticino", S.CITIES)
        self.assertIn("Ticino", S.CITTA_TARGET_PATTERN)

    def test_linkedin_riceve_una_sede_svizzera(self):
        loc = S.CITIES["Ticino"]["linkedin_location"]
        self.assertTrue("switzerland" in loc.lower() or "svizzera" in loc.lower(), loc)

    def test_milano_e_genova_non_cambiano(self):
        self.assertEqual(S.CITIES["Genova"]["linkedin_location"], "Genoa, Italy")
        self.assertEqual(S.CITIES["Milano"]["linkedin_location"], "Milan, Italy")
        self.assertTrue(S.CITIES["Milano"]["filter_hybrid_only"])
        self.assertFalse(S.CITIES["Genova"]["filter_hybrid_only"])

    def test_in_ticino_si_accetta_anche_il_lavoro_in_sede(self):
        """Decisione del 19/11/2026: il frontaliere lavora in sede, scartarlo taglierebbe quasi tutto."""
        self.assertFalse(S.CITIES["Ticino"]["filter_hybrid_only"])


class TestLinkedInCercaInTicino(unittest.TestCase):
    def test_la_richiesta_porta_la_localita_svizzera(self):
        richieste = []

        class Risposta:
            status_code = 404
            text = ""

        def finta_get(url, params=None, headers=None, timeout=None):
            richieste.append(dict(params or {}))
            return Risposta()
        with mock.patch.object(S.requests, "get", finta_get), mock.patch("time.sleep", lambda *_: None):
            S.LinkedInScraper().scrape("Ticino", S.CITIES["Ticino"])
        self.assertTrue(richieste)
        self.assertTrue(all(r["location"] == S.CITIES["Ticino"]["linkedin_location"] for r in richieste))


class TestWyserNonSprecaRichiestePerIlTicino(unittest.TestCase):
    def test_wyser_non_scarica_nulla_per_il_ticino(self):
        def vietata(*a, **k):
            raise AssertionError("Wyser e' un portale italiano: per il Ticino non deve fare richieste")
        with mock.patch.object(S, "_get_con_un_riprova", vietata):
            self.assertEqual(S.WyserScraper().scrape("Ticino", S.CITIES["Ticino"]), [])


class TestFiltroSedeTicino(unittest.TestCase):
    def test_un_annuncio_ticinese_in_sede_passa(self):
        j = offerta("Head of E-commerce", "Ticino", "Sede di lavoro: Lugano. Gestione del canale digitale.", modo="in sede")
        self.assertEqual([x.title for x in S.filtra_offerte_per_citta([j], S.CITIES["Ticino"])], ["Head of E-commerce"])

    def test_un_annuncio_ticinese_ibrido_passa(self):
        j = offerta("Digital Marketing Manager", "Ticino", "Lugano, 2 giorni di smart working.", modo="ibrido")
        self.assertEqual(len(S.filtra_offerte_per_citta([j], S.CITIES["Ticino"])), 1)

    def test_un_annuncio_attribuito_al_ticino_ma_di_zurigo_viene_scartato(self):
        """LinkedIn con «Ticino» restituisce anche il resto della Svizzera: la citta' viene dalla query, non dall'annuncio."""
        j = offerta("Head of Marketing", "Ticino", "Our office in Zurich. Zurich headquarters, Zurich team. Zurigo.", modo="in sede")
        self.assertEqual(S.filtra_offerte_per_citta([j], S.CITIES["Ticino"]), [])

    def test_un_annuncio_ticinese_che_cita_zurigo_una_volta_passa(self):
        j = offerta("Head of Marketing", "Ticino", "Sede Lugano, riporto alla casa madre di Zurigo.", modo="in sede")
        self.assertEqual(len(S.filtra_offerte_per_citta([j], S.CITIES["Ticino"])), 1)

    def test_un_annuncio_milanese_che_cita_zurigo_non_cambia(self):
        """La lista delle citta' svizzere vale solo per il Ticino: un annuncio di Milano che nomina la sede di Zurigo non deve diventare «fuori area»."""
        j = offerta("Marketing Manager", "Milano", "Sede: Milano. Riporto a Zurich e Zurich e Zurich e Zurich.", modo="ibrido")
        self.assertEqual(len(S.filtra_offerte_per_citta([j], S.CITIES["Milano"])), 1)


class TestSedeDaiPortaliNazionali(unittest.TestCase):
    def test_un_portale_nazionale_con_lugano_nel_titolo_e_ticino(self):
        self.assertEqual(S.rileva_citta_offerta(offerta("Digital Sales Manager - Lugano", "Italia")), "Ticino")

    def test_milano_e_genova_si_riconoscono_come_prima(self):
        self.assertEqual(S.rileva_citta_offerta(offerta("Marketing Manager Milano", "Italia")), "Milano")
        self.assertEqual(S.rileva_citta_offerta(offerta("Sales Manager Genova", "Italia")), "Genova")

    def test_roma_resta_fuori(self):
        self.assertIsNone(S.rileva_citta_offerta(offerta("Marketing Manager Roma", "Italia")))


class TestPromptDiValutazioneAmmetteIlTicino(unittest.TestCase):
    """Il prompt diceva «cerca solo a Genova e Milano» e trattava la sede fuori dalle citta' come requisito bloccante (punteggio sotto 40):
    ogni offerta ticinese sarebbe stata bocciata dal modello. Il regolamento della sede deve nominare il Ticino."""

    def riga_sede(self):
        righe = [r for r in S._MATCH_LLM_SYSTEM_TEMPLATE.splitlines() if r.lstrip("- ").startswith("Sede:")]
        self.assertEqual(len(righe), 1)
        return righe[0]

    def test_la_sede_ammessa_include_il_ticino(self):
        r = self.riga_sede()
        for parola in ("Genova", "Milano", "Ticino"):
            self.assertIn(parola, r)

    def test_il_lavoro_in_sede_in_ticino_e_ammesso(self):
        self.assertIn("in sede", self.riga_sede().lower())

    def test_il_resto_della_svizzera_resta_fuori(self):
        r = self.riga_sede().lower()
        self.assertTrue("zurigo" in r or "resto della svizzera" in r, r)

    def test_il_full_remote_resta_ammesso(self):
        self.assertIn("full remote", self.riga_sede())


class TestAvvisoPresenzaTicino(unittest.TestCase):
    def test_in_sede_in_ticino_segnala_frontalierato_o_trasferimento(self):
        a = S.avviso_presenza(offerta("Head of E-commerce", "Ticino", modo="in sede"))
        self.assertIn("Ticino", a)
        self.assertTrue("frontalier" in a.lower() or "trasferimento" in a.lower(), a)

    def test_ibrido_in_ticino_non_segnala(self):
        self.assertEqual(S.avviso_presenza(offerta("Head of E-commerce", "Ticino", modo="ibrido")), "")

    def test_milano_non_cambia(self):
        self.assertEqual(S.avviso_presenza(offerta("Marketing Manager", "Milano", modo="in sede")), "presenza quotidiana a Milano, valuta il pendolarismo")

    def test_genova_non_segnala(self):
        self.assertEqual(S.avviso_presenza(offerta("Marketing Manager", "Genova", modo="in sede")), "")


# ---------------------------------------------------------------- portale svizzero jobs.ch
ADESSO = date.today().isoformat() + "T09:00:00+01:00"
DESCRIZIONE = "<p>" + ("Descrizione completa dell'annuncio con tutti i requisiti e le responsabilita'. " * 60) + "</p><p>Ultima frase: sede di lavoro Lugano.</p>"


def doc(job_id, titolo, azienda="Acme SA", place="Lugano", lingue=(("it", 4),), data=ADESSO):
    return {"job_id": job_id, "title": titolo, "company_name": azienda, "place": place, "publication_date": data, "preview": titolo,
            "language_skills": [{"language": l, "level": n} for l, n in lingue],
            "_links": {"detail_en": {"href": f"https://www.jobs.ch/en/vacancies/detail/{job_id}/"}}}


class Risposta:
    def __init__(self, js=None, html="", codice=200):
        self._js, self.text, self.status_code, self.encoding = js, html, codice, None

    def json(self):
        return self._js


class FintoJobsCh:
    """Sostituisce requests.get: API per (sede, pagina) e pagine di dettaglio con il JSON-LD."""

    def __init__(self, per_sede, errori=()):
        self.per_sede, self.errori, self.api, self.dettagli = per_sede, set(errori), [], []

    def __call__(self, url, params=None, headers=None, timeout=None):
        if "/api/v1/public/search" in url:
            sede, pagina = params["location"], params.get("page", 1)
            self.api.append((sede, pagina))
            if sede in self.errori:
                return Risposta(codice=500)
            pagine = self.per_sede.get(sede, [[]])
            idx = min(pagina, len(pagine)) - 1
            return Risposta({"total_hits": sum(len(p) for p in pagine), "num_pages": len(pagine), "current_page": pagina,
                             "documents": pagine[idx] if pagina <= len(pagine) else []})
        self.dettagli.append(url)
        import json as _j
        ld = _j.dumps({"@type": "JobPosting", "description": DESCRIZIONE})
        return Risposta(html=f'<html><body><script type="application/ld+json">{ld}</script><p>menu cookie</p></body></html>')


class TestJobsChScraper(unittest.TestCase):
    def esegui(self, per_sede, citta="Ticino", config=None, errori=(), viste=()):
        rete = FintoJobsCh(per_sede, errori)
        cfg = config or S.CITIES.get("Ticino", {})
        with mock.patch.object(S.requests, "get", rete), mock.patch("time.sleep", lambda *_: None), mock.patch.object(S, "load_viste", lambda: set(viste)):
            self.jobs = S.JobsChScraper().scrape(citta, cfg)
        self.rete = rete
        return self.jobs

    def test_il_portale_si_chiama_jobsch(self):
        self.assertEqual(S.JobsChScraper().portal_name, "JobsCh")

    def test_la_config_del_ticino_elenca_le_sedi_da_cercare(self):
        sedi = S.CITIES["Ticino"]["jobsch_sedi"]
        for s in ("Lugano", "Mendrisio", "Bellinzona", "Locarno", "Chiasso"):
            self.assertIn(s, sedi)

    def test_legge_tutte_le_pagine_e_tiene_i_titoli_di_un_profilo_commerciale(self):
        jobs = self.esegui({"Lugano": [[doc("a1", "Digital Marketing Manager"), doc("a2", "Idraulico")], [doc("a3", "Head of E-commerce")]]})
        self.assertEqual(sorted(j.title for j in jobs), ["Digital Marketing Manager", "Head of E-commerce"])
        self.assertIn(("Lugano", 2), self.rete.api)

    def test_l_offerta_ha_citta_azienda_portale_e_link(self):
        j = self.esegui({"Lugano": [[doc("a1", "Digital Marketing Manager", azienda="Helvetia SA")]]})[0]
        self.assertEqual((j.city, j.company, j.portal, j.link), ("Ticino", "Helvetia SA", "JobsCh", "https://www.jobs.ch/en/vacancies/detail/a1/"))

    def test_il_testo_e_la_descrizione_intera_senza_il_rumore_della_pagina(self):
        j = self.esegui({"Lugano": [[doc("a1", "Digital Marketing Manager")]]})[0]
        self.assertIn("Ultima frase: sede di lavoro Lugano.", j.testo_completo)         # nessun troncamento
        self.assertGreater(len(j.testo_completo), 4000)
        self.assertNotIn("menu cookie", j.testo_completo)
        self.assertIn("Lugano", j.testo_completo)                                         # la sede e' nel testo: serve al controllo di coerenza

    def test_chi_richiede_tedesco_o_francese_si_scarta_senza_scaricare_la_pagina(self):
        jobs = self.esegui({"Lugano": [[doc("a1", "Marketing Manager", lingue=(("it", 4), ("de", 4))), doc("a2", "Sales Manager", lingue=(("fr", 3),)),
                                        doc("a3", "Digital Manager", lingue=(("it", 4), ("en", 3)))]]})
        self.assertEqual([j.title for j in jobs], ["Digital Manager"])
        self.assertEqual(len(self.rete.dettagli), 1)

    def test_un_livello_basso_di_tedesco_non_scarta(self):
        jobs = self.esegui({"Lugano": [[doc("a1", "Marketing Manager", lingue=(("it", 4), ("de", 1)))]]})
        self.assertEqual(len(jobs), 1)

    def test_la_stessa_offerta_da_due_sedi_conta_una_volta(self):
        jobs = self.esegui({"Lugano": [[doc("a1", "Marketing Manager")]], "Mendrisio": [[doc("a1", "Marketing Manager")]]})
        self.assertEqual(len(jobs), 1)

    def test_un_errore_su_una_sede_non_fa_perdere_le_altre(self):
        jobs = self.esegui({"Mendrisio": [[doc("b1", "Marketing Manager", place="Mendrisio")]]}, errori=("Lugano",))
        self.assertEqual([j.title for j in jobs], ["Marketing Manager"])

    def test_un_offerta_gia_vista_non_scarica_la_pagina(self):
        jobs = self.esegui({"Lugano": [[doc("a1", "Marketing Manager")]]}, viste={"https://www.jobs.ch/en/vacancies/detail/a1/"})
        self.assertEqual(jobs, [])
        self.assertEqual(self.rete.dettagli, [])

    def test_un_offerta_troppo_vecchia_non_scarica_la_pagina(self):
        jobs = self.esegui({"Lugano": [[doc("a1", "Marketing Manager", data="2026-01-05T09:00:00+01:00")]]})
        self.assertEqual(jobs, [])
        self.assertEqual(self.rete.dettagli, [])

    def test_per_milano_e_genova_non_fa_nessuna_richiesta(self):
        for c in ("Milano", "Genova"):
            self.esegui({"Lugano": [[doc("a1", "Marketing Manager")]]}, citta=c, config=S.CITIES[c])
            self.assertEqual((self.jobs, self.rete.api, self.rete.dettagli), ([], [], []))

    def test_l_offerta_ticinese_supera_il_filtro_per_citta(self):
        jobs = self.esegui({"Lugano": [[doc("a1", "Digital Marketing Manager")]]})
        self.assertEqual(len(S.filtra_offerte_per_citta(jobs, S.CITIES["Ticino"])), 1)

    def test_il_portale_e_tra_quelli_del_run(self):
        import inspect
        self.assertIn("JobsChScraper()", inspect.getsource(S.esegui_scraping_job))


if __name__ == "__main__":
    unittest.main(verbosity=2)
