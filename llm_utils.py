"""Utility condivise per le chiamate all'API Claude, usate sia da scraper.py
(match-scoring semantico) sia da cv_personalizzazione.py (proposta e verifica
anti-fabbricazione del CV).

`estrai_testo_risposta` era duplicata in tre punti indipendenti (uno per
funzione), ed è stata la sede di un bug reale: il fix "next() con default
esplicito" applicato in un punto non era stato applicato negli altri due,
finché una code review successiva non l'ha ritrovato. Centralizzarla qui
significa che un fix futuro si applica automaticamente ovunque.
"""


def estrai_testo_risposta(response):
    """Estrae il primo blocco di testo da una risposta Claude (client.messages.create
    o .stream().get_final_message()). Solleva ValueError con il motivo (stop_reason)
    se non c'è alcun blocco di testo — es. per troncamento da max_tokens quando il
    ragionamento adattivo consuma l'intero budget — invece di lasciare che un
    generator esaurito sollevi un generico StopIteration senza messaggio utile."""
    testo = next((b.text for b in response.content if b.type == "text"), None)
    if testo is None:
        raise ValueError(f"nessun blocco di testo nella risposta (stop_reason={response.stop_reason})")
    return testo
