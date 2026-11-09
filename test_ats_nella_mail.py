# -*- coding: utf-8 -*-
"""Le offerte degli ATS diretti, valutate, entrano nella mail unica "Job Alert" senza doppioni (09/11/2026).
Esecuzione:  python test_ats_nella_mail.py
Punti di prova: coda_ats.prepara (voci per la coda, soglia, doppioni con la stessa logica dei portali) e
scraper.togli_copie_portale_di_ats (al momento dell'invio).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scraper as S  # noqa: E402
import coda_ats as C  # noqa: E402


def cand(n, azienda="ShippyPro", titolo="Key Account Manager", citta="Milano", link=None, data="2026-11-09"):
    return {"n": n, "azienda": azienda, "titolo": titolo, "citta": citta, "luogo": citta, "data": data, "ats": "factorial",
            "link": link or f"https://ats.example/{n}", "testo_pieno": "Testo intero dell'annuncio. " * 20}


def val(p=75, m="Ruolo B2B SaaS in scale-up.", mod="ibrido", ral=""):
    return {"p": p, "m": m, "mod": mod, "ral": ral}


def job_portale(titolo, azienda, citta="Milano", portale="LinkedIn", link="https://linkedin.example/1"):
    return S.ScrapedJob(titolo, azienda, portale, link, "2026-11-09", "", "Alto", 3, citta, "unverified", "ok", 60, "x", "", valutato_da="llm")


class TestPrepara(unittest.TestCase):
    def setUp(self):
        self._vecchio = (S.segnature_storico, S.carica_storico_offerte)
        S.segnature_storico = lambda: {}                      # nessuno storico, salvo dove il test lo dice
        S.carica_storico_offerte = lambda: []

    def tearDown(self):
        S.segnature_storico, S.carica_storico_offerte = self._vecchio

    def test_voce_leggibile_da_ScrapedJob_con_i_campi_giusti(self):
        voci, _, _ = C.prepara([cand(1)], {"1": val(80, "Perfetta per te.", "ibrido", "45-55k")}, viste=set(), coda=[])
        self.assertEqual(len(voci), 1)
        j = S.ScrapedJob.from_dict(voci[0])
        self.assertEqual((j.title, j.company, j.city, j.probabilita), ("Key Account Manager", "ShippyPro", "Milano", 80))
        self.assertEqual(j.valutato_da, "llm")                # cosi' la seconda valutazione automatica non la rifa'
        self.assertIn("Claude", j.modello)                    # la mail dice chi ha dato il punteggio
        self.assertEqual(j.motivazione, "Perfetta per te.")
        self.assertEqual(j.ral, "45-55k")
        self.assertEqual(j.work_mode, "ibrido")
        self.assertIn("ATS", j.portal)
        self.assertTrue(j.testo_completo.startswith("Testo intero"))     # testo intero, nessun limite

    def test_sotto_soglia_non_entra_e_manca_la_valutazione_non_entra(self):
        voci, _, _ = C.prepara([cand(1), cand(2, titolo="Altro"), cand(3, titolo="Terzo")], {"1": val(49), "2": val(50)}, viste=set(), coda=[])
        self.assertEqual([v["title"] for v in voci], ["Altro"])         # 49 fuori, 50 dentro, senza valutazione fuori

    def test_ripubblicazione_gia_recapitata_viene_scartata(self):
        S.segnature_storico = lambda: {(S._pulisci_per_segnatura("Key Account Manager"), S._pulisci_per_segnatura("ShippyPro")): "2026-11-02"}
        voci, _, scartate = C.prepara([cand(1)], {"1": val()}, viste=set(), coda=[])
        self.assertEqual(voci, [])
        self.assertEqual(len(scartate), 1)

    def test_nome_azienda_con_forma_societaria_diversa_e_lo_stesso_doppione(self):
        # storico: "AXITEA SPA" - l'ATS dice "AXITEA" (successo davvero: Head of Commercial Partnership, gia' mandata il 28/10)
        S.carica_storico_offerte = lambda: [{"titolo": "Head of Commercial Partnership and Ecosystem", "azienda": "AXITEA SPA", "data_invio": "2026-10-28"}]
        voci, _, scartate = C.prepara([cand(1, azienda="AXITEA", titolo="Head of Commercial Partnership and Ecosystem")], {"1": val()}, viste=set(), coda=[])
        self.assertEqual(voci, [])
        self.assertEqual(len(scartate), 1)

    def test_aziende_con_nome_simile_ma_diverso_non_sono_doppioni(self):
        S.carica_storico_offerte = lambda: [{"titolo": "Key Account Manager", "azienda": "Philips Healthcare", "data_invio": "2026-11-01"}]
        voci, _, _ = C.prepara([cand(1, azienda="Philips", titolo="Key Account Manager")], {"1": val()}, viste=set(), coda=[])
        self.assertEqual(len(voci), 1)

    def test_nella_coda_la_variante_del_nome_e_lo_stesso_doppione(self):
        coda = [job_portale("Sales Manager", "Acme S.p.A.").to_dict()]
        voci, _, _ = C.prepara([cand(1, azienda="ACME", titolo="Sales Manager")], {"1": val()}, viste=set(), coda=coda)
        self.assertEqual(voci, [])

    def test_link_gia_visto_dai_portali_viene_scartato(self):
        link = "https://ats.example/1"
        voci, _, _ = C.prepara([cand(1, link=link)], {"1": val()}, viste={S.get_job_id(link)}, coda=[])
        self.assertEqual(voci, [])

    def test_gia_nella_coda_come_offerta_di_portale_non_si_duplica(self):
        coda = [job_portale("Key Account Manager", "ShippyPro").to_dict()]
        voci, _, scartate = C.prepara([cand(1)], {"1": val()}, viste=set(), coda=coda)
        self.assertEqual(voci, [])
        self.assertEqual(len(scartate), 1)

    def test_stessa_offerta_su_due_siti_dello_stesso_ats_una_sola_volta(self):
        voci, _, _ = C.prepara([cand(1, link="https://a/1"), cand(2, link="https://b/2")], {"1": val(), "2": val()}, viste=set(), coda=[])
        self.assertEqual(len(voci), 1)

    def test_azienda_diversa_non_viene_scambiata_per_doppione(self):
        voci, _, _ = C.prepara([cand(1, azienda="ShippyPro"), cand(2, azienda="Altra Spa")], {"1": val(), "2": val()}, viste=set(), coda=[])
        self.assertEqual(len(voci), 2)


class TestTogliCopiePortale(unittest.TestCase):
    def ats(self, titolo, azienda, citta="Milano"):
        j = job_portale(titolo, azienda, citta, portale=C.PORTALE_ATS, link="https://ats.example/x")
        return j

    def test_se_esiste_la_copia_ats_si_toglie_quella_del_portale(self):
        batch = [job_portale("Key Account Manager", "ShippyPro"), self.ats("Key Account Manager", "ShippyPro")]
        tenute = S.togli_copie_portale_di_ats(batch)
        self.assertEqual([j.portal for j in tenute], [C.PORTALE_ATS])

    def test_la_variante_del_nome_azienda_e_la_stessa_offerta(self):
        batch = [job_portale("Sales Manager", "AXITEA SPA"), self.ats("Sales Manager", "AXITEA")]
        self.assertEqual([j.portal for j in S.togli_copie_portale_di_ats(batch)], [C.PORTALE_ATS])

    def test_aziende_simili_ma_diverse_restano_entrambe(self):
        batch = [job_portale("Sales Manager", "Philips Healthcare"), self.ats("Sales Manager", "Philips")]
        self.assertEqual(len(S.togli_copie_portale_di_ats(batch)), 2)

    def test_non_tocca_i_doppioni_tra_portali_ne_aziende_diverse(self):
        batch = [job_portale("Sales Manager", "Acme", link="https://l/1"), job_portale("Sales Manager", "Acme", portale="Hays", link="https://h/1"),
                 job_portale("Sales Manager", "Beta", link="https://l/2"), self.ats("Sales Manager", "Gamma")]
        self.assertEqual(len(S.togli_copie_portale_di_ats(batch)), 4)

    def test_senza_azienda_nota_non_si_toglie_niente(self):
        batch = [job_portale("Sales Manager", ""), self.ats("Sales Manager", "")]
        self.assertEqual(len(S.togli_copie_portale_di_ats(batch)), 2)

    def test_citta_diversa_non_e_doppione(self):
        batch = [job_portale("Sales Manager", "Acme", "Genova"), self.ats("Sales Manager", "Acme", "Milano")]
        self.assertEqual(len(S.togli_copie_portale_di_ats(batch)), 2)

    def test_ordine_e_contenuto_invariati_senza_ats(self):
        batch = [job_portale("A", "X", link="https://l/1"), job_portale("B", "Y", link="https://l/2")]
        self.assertEqual(S.togli_copie_portale_di_ats(batch), batch)


class TestAttesaAts(unittest.TestCase):
    """La mail aspetta gli ATS se il giro li ha dichiarati attesi e non sono ancora consegnati; altrimenti parte come sempre."""

    def setUp(self):
        import json, tempfile
        self.json = json
        self.cwd = os.getcwd()
        self.d = tempfile.mkdtemp()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)

    def stato(self, giornata, stato):
        self.json.dump({"giornata": giornata, "stato": stato}, open("ats_stato.json", "w", encoding="utf-8"))

    def alle(self, giorno, ora, minuto=0):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime(2026, 11, giorno, ora, minuto, tzinfo=ZoneInfo("Europe/Rome"))

    def test_attesi_e_non_consegnati_la_mail_aspetta_anche_alle_otto_di_sera(self):
        self.stato("2026-11-09", "atteso")
        self.assertTrue(S.ats_in_attesa(self.alle(9, 18, 5)))
        self.assertTrue(S.ats_in_attesa(self.alle(9, 20, 0)))

    def test_consegnati_la_mail_parte(self):
        self.stato("2026-11-09", "consegnato")
        self.assertFalse(S.ats_in_attesa(self.alle(9, 18, 5)))

    def test_dichiarazione_di_un_altro_giorno_non_blocca(self):
        self.stato("2026-11-08", "atteso")
        self.assertFalse(S.ats_in_attesa(self.alle(9, 18, 5)))

    def test_file_assente_o_rotto_non_blocca_mai(self):
        self.assertFalse(S.ats_in_attesa(self.alle(9, 18, 5)))
        open("ats_stato.json", "w").write("{non json")
        self.assertFalse(S.ats_in_attesa(self.alle(9, 18, 5)))

    def test_oltre_le_23_30_la_mail_parte_comunque_e_dopo_mezzanotte_pure(self):
        self.stato("2026-11-09", "atteso")
        self.assertTrue(S.ats_in_attesa(self.alle(9, 23, 29)))
        self.assertFalse(S.ats_in_attesa(self.alle(9, 23, 30)))
        self.assertFalse(S.ats_in_attesa(self.alle(10, 1, 0)))        # dopo mezzanotte la giornata e' ancora quella del 9, ma il limite e' passato

    def test_run_email_job_esce_senza_inviare_se_gli_ats_sono_attesi(self):
        import subprocess
        from datetime import datetime
        from zoneinfo import ZoneInfo
        adesso = datetime.now(ZoneInfo("Europe/Rome"))
        if adesso.hour + adesso.minute / 60 >= 23.5 or adesso.hour < 6:
            self.skipTest("fuori dalla finestra di attesa")
        self.stato(S.giornata_riepilogo(adesso), "atteso")
        # SICUREZZA: nel processo figlio smtplib e' SOSTITUITO da un blocco che fallisce. Svuotare le credenziali non basta (il .env le rimette: provato il 09/11/2026),
        # e senza questo, se il blocco "ATS attesi" non c'e' (fase rossa del test) run_email_job.py spedisce una mail VERA: e' successo due volte, alle 14:51 e alle 18:19.
        codice = ("import smtplib, runpy, sys\n"
                  "class _Blocco:\n"
                  "    def __init__(self, *a, **k): raise RuntimeError('INVIO BLOCCATO DAL TEST')\n"
                  "smtplib.SMTP = _Blocco; smtplib.SMTP_SSL = _Blocco\n"
                  f"runpy.run_path({os.path.join(self.cwd, 'run_email_job.py')!r}, run_name='__main__')\n")
        r = subprocess.run([sys.executable, "-c", codice], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertIn("In attesa degli ATS", r.stdout)


class TestValutazioniCoda(unittest.TestCase):
    """La mia valutazione sostituisce quella di Groq su TUTTE le voci della coda, nel formato di produzione: una sola mail, con un solo valutatore."""

    def voce(self, titolo, azienda, link, prob=70, portale="LinkedIn", da="llm", modello="Groq openai/gpt-oss-120b"):
        return {"title": titolo, "company": azienda, "portal": portale, "link": link, "date": "2026-11-09", "snippet": "", "match_level": "Alto", "match_count": 3,
                "city": "Milano", "work_mode": "unverified", "fetch_status": "ok", "probabilita": prob, "motivazione": "motivazione Groq", "testo_completo": "t" * 400,
                "ral": "", "dettaglio_modalita": "", "valutato_da": da, "modello": modello, "recruiter": ""}

    def test_sostituisce_punteggio_motivazione_modalita_e_valutatore(self):
        coda = [self.voce("Marketing Manager", "IED", "https://l.example/jobs/view/1")]
        nuova, rimosse, senza = C.applica_valutazioni(coda, {S.get_job_id("https://l.example/jobs/view/1"): {"p": 68, "m": "Mia motivazione.", "mod": "in sede", "ral": "40.000-50.000 €"}})
        v = nuova[0]
        self.assertEqual((v["probabilita"], v["motivazione"], v["work_mode"], v["ral"], v["valutato_da"]), (68, "Mia motivazione.", "in sede", "40.000-50.000 €", "llm"))
        self.assertIn("Claude", v["modello"])
        self.assertEqual((rimosse, senza), ([], []))
        self.assertEqual(v["testo_completo"], "t" * 400)                  # il resto della voce non si tocca

    def test_sotto_soglia_esce_dalla_coda_e_si_dice(self):
        coda = [self.voce("Head of Sales", "Reverse", "https://r.example/1", prob=68), self.voce("GTM Manager", "Contents", "https://l.example/2", prob=75)]
        vals = {S.get_job_id("https://r.example/1"): {"p": 25, "m": "Vendita pura.", "mod": "da remoto", "ral": ""},
                S.get_job_id("https://l.example/2"): {"p": 80, "m": "Centrata.", "mod": "non indicata", "ral": ""}}
        nuova, rimosse, senza = C.applica_valutazioni(coda, vals)
        self.assertEqual([v["company"] for v in nuova], ["Contents"])
        self.assertEqual([(v["company"], p) for v, p in rimosse], [("Reverse", 25)])

    def test_le_voci_senza_mia_valutazione_restano_e_vengono_segnalate(self):
        coda = [self.voce("A", "X", "https://x.example/1"), self.voce("B", "Y", "https://y.example/2")]
        nuova, rimosse, senza = C.applica_valutazioni(coda, {S.get_job_id("https://x.example/1"): {"p": 60, "m": "ok", "mod": "ibrido", "ral": ""}})
        self.assertEqual(len(nuova), 2)
        self.assertEqual([v["company"] for v in senza], ["Y"])
        self.assertEqual(nuova[1]["modello"], "Groq openai/gpt-oss-120b")     # intatta

    def test_il_link_con_parametri_di_tracking_corrisponde_lo_stesso(self):
        coda = [self.voce("A", "X", "https://x.example/jobs/view/9?trackingId=abc&refId=1")]
        nuova, rimosse, senza = C.applica_valutazioni(coda, {S.get_job_id("https://x.example/jobs/view/9"): {"p": 60, "m": "ok", "mod": "ibrido", "ral": ""}})
        self.assertEqual((len(senza), nuova[0]["probabilita"]), (0, 60))

    def test_modalita_non_indicata_diventa_unverified(self):
        coda = [self.voce("A", "X", "https://x.example/1")]
        nuova, _, _ = C.applica_valutazioni(coda, {S.get_job_id("https://x.example/1"): {"p": 60, "m": "ok", "mod": "non indicata", "ral": ""}})
        self.assertEqual(nuova[0]["work_mode"], "unverified")


class TestAvviaMail(unittest.TestCase):
    """Dopo la consegna ATS il workflow della mail parte subito SOLO se l'orario normale (18:05) e' gia' passato e la mail di oggi non e' uscita.
    Prima delle 18:05 non serve: la mail parte da sola al suo orario, con anche l'ultimo scraping dei portali (17:30)."""

    def setUp(self):
        import pubblica_offerte_ats as P
        self.P = P
        self.chiamate = []
        self._run = P.subprocess.run

        class R:
            returncode, stderr = 0, ""
        P.subprocess.run = lambda cmd, **k: (self.chiamate.append(cmd), R())[1]

    def tearDown(self):
        self.P.subprocess.run = self._run

    def alle(self, ora, minuto=0, giorno=9):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime(2026, 11, giorno, ora, minuto, tzinfo=ZoneInfo("Europe/Rome"))

    def test_dopo_le_1805_parte_se_la_mail_di_oggi_non_e_uscita(self):
        self.P.avvia_mail({"giornata": "2000-01-01"}, self.alle(18, 30))
        self.assertEqual(self.chiamate, [["gh", "workflow", "run", "email.yml", "-R", self.P.REPO_GITHUB]])

    def test_prima_delle_1805_non_parte_la_mail_ha_il_suo_orario(self):
        self.P.avvia_mail({"giornata": "2000-01-01"}, self.alle(16, 45))
        self.P.avvia_mail({"giornata": "2000-01-01"}, self.alle(18, 4))
        self.assertEqual(self.chiamate, [])

    def test_non_parte_se_la_mail_di_oggi_e_gia_uscita(self):
        self.P.avvia_mail({"giornata": S.giornata_riepilogo(self.alle(19, 0))}, self.alle(19, 0))
        self.assertEqual(self.chiamate, [])

    def test_parte_anche_senza_ultimo_invio_e_a_notte_dopo_mezzanotte(self):
        self.P.avvia_mail({}, self.alle(20, 0))
        self.P.avvia_mail({}, self.alle(1, 0, giorno=10))        # dopo mezzanotte e' ancora la giornata del 9
        self.assertEqual(len(self.chiamate), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
