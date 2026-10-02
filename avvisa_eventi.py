# -*- coding: utf-8 -*-
"""Avviso interno su ogni evento nuovo.

Decisione di Andrea (1/10/2026): ogni evento creato nel portale va segnalato a Pari, Bertozzi e
Dalla Rizza. Il giro legge gli eventi nati dopo DA_QUANDO (oggetto eventi di hapily), e per ciascuno
manda UNA email con titolo, data, tipo, marchio, pubblicazione e link Zoom.

  · l'avviso aspetta il link Zoom (lo crea il motore entro ~10 minuti): si manda quando il link c'e',
    oppure dopo 30 minuti anche senza (eventi in presenza o fuori perimetro);
  · il registro `avvisati_eventi.txt` impedisce il doppio invio.

Uso:  python avvisa_eventi.py            manda gli avvisi dovuti
      python avvisa_eventi.py --prova    elenca senza inviare
"""
import datetime
import html
import io
import os
import sys
import smtplib
import ssl
from email.message import EmailMessage

QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, QUI)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import preventivi_corsi as C                                  # noqa: E402

EV = "2-143900361"
DESTINATARI = ["pari@spaggiari.eu", "bertozzi@spaggiari.eu", "dallarizza@spaggiari.eu", "malerba@spaggiari.eu"]
# gli eventi nati prima del lancio (1/10/2026 ~23:00 italiane) non si segnalano
DA_QUANDO = datetime.datetime(2026, 10, 1, 21, 0, tzinfo=datetime.timezone.utc)
ATTESA_LINK_MIN = 30
REGISTRO = os.path.join(QUI, "avvisati_eventi.txt")
PORTALE = "144406271"


def registro():
    try:
        return {r.strip() for r in io.open(REGISTRO, encoding="utf-8") if r.strip()}
    except FileNotFoundError:
        return set()


def quando(iso):
    try:
        d = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return iso or "da definire"
    # ora italiana: +2 fino al 25/10/2026, poi +1
    off = 2 if d.replace(tzinfo=None) < datetime.datetime(2026, 10, 25, 1, 0) else 1
    l = d + datetime.timedelta(hours=off)
    giorni = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
    mesi = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
            "settembre", "ottobre", "novembre", "dicembre"]
    return "%s %d %s %d, ore %02d:%02d" % (giorni[l.weekday()], l.day, mesi[l.month - 1], l.year, l.hour, l.minute)


def manda(oggetto, corpo_html):
    utente = os.environ.get("SMTP_CORSI_USER")
    chiave = os.environ.get("SMTP_CORSI_PASS")
    if not (utente and chiave):
        raise RuntimeError("credenziali SMTP assenti")
    m = EmailMessage()
    m["From"] = "Spaggiari <%s>" % C.MITTENTE
    m["To"] = ", ".join(DESTINATARI)
    m["Subject"] = oggetto
    m.set_content("Nuovo evento nel portale: il dettaglio e' in questo messaggio, serve un lettore di posta HTML.")
    m.add_alternative(corpo_html, subtype="html")
    s = smtplib.SMTP(C.SMTP_HOST, C.SMTP_PORTA, timeout=60)
    s.starttls(context=ssl.create_default_context())
    s.login(utente, chiave)
    s.send_message(m)
    s.quit()


def riga(etichetta, valore):
    return ("<tr><td style='padding:4px 14px 4px 0;color:#51606E;vertical-align:top'>%s</td>"
            "<td style='padding:4px 0;color:#0E2A4D'>%s</td></tr>" % (etichetta, valore))


def corpo(p, eid):
    pubblicato = "sì" if p.get("is_published") == "true" else "no (riservato)"
    link = p.get("meeting_link") or ""
    link_html = '<a href="%s">%s</a>' % (html.escape(link), html.escape(link)) if link else "in preparazione"
    record = "https://app-eu1.hubspot.com/contacts/%s/record/%s/%s" % (PORTALE, EV, eid)
    righe = "".join([
        riga("Evento", "<b>%s</b>" % html.escape(p.get("name") or "")),
        riga("Quando", quando(p.get("start_datetime"))),
        riga("Tipo", html.escape(p.get("type") or "-")),
        riga("Marchio", html.escape(p.get("brand") or "-")),
        riga("Argomento", html.escape(p.get("tag") or "-")),
        riga("Pubblicato sul sito", pubblicato),
        riga("Collegamento", link_html),
        riga("Codice", html.escape(p.get("external_id") or "-")),
        riga("Scheda", '<a href="%s">apri in HubSpot</a>' % record),
    ])
    return ("<div style='font-family:Arial,sans-serif;font-size:15px;color:#0E2A4D'>"
            "<p>È stato creato un nuovo evento nel portale.</p>"
            "<table style='border-collapse:collapse;font-size:15px'>%s</table>"
            "<p style='color:#51606E;font-size:13px'>Messaggio automatico di Spaggiari.</p></div>" % righe)


def main():
    prova = "--prova" in sys.argv
    gia = registro()
    soglia = int(DA_QUANDO.timestamp() * 1000)
    r = C.hs("/crm/v3/objects/%s/search" % EV, {
        "filterGroups": [{"filters": [{"propertyName": "hs_createdate", "operator": "GTE", "value": str(soglia)}]}],
        "properties": ["name", "start_datetime", "type", "brand", "tag", "is_published", "meeting_link",
                       "external_id", "annullato", "hs_createdate"],
        "sorts": [{"propertyName": "hs_createdate", "direction": "ASCENDING"}], "limit": 100}, "POST")
    nuovi = [x for x in r.get("results", []) if x["id"] not in gia]
    print("eventi nati dopo il lancio: %d, da avvisare: %d" % (len(r.get("results", [])), len(nuovi)))
    adesso = datetime.datetime.now(datetime.timezone.utc)
    for x in nuovi:
        p = x["properties"]
        nato = datetime.datetime.fromisoformat((p.get("hs_createdate") or "").replace("Z", "+00:00"))
        minuti = (adesso - nato).total_seconds() / 60
        if not p.get("meeting_link") and minuti < ATTESA_LINK_MIN:
            print("  %s: aspetto il link Zoom (%d min)" % (p.get("name"), minuti))
            continue
        oggetto = "Nuovo evento: %s" % (p.get("name") or "senza titolo")
        print("  AVVISO: %s" % oggetto)
        if prova:
            continue
        manda(oggetto, corpo(p, x["id"]))
        with io.open(REGISTRO, "a", encoding="utf-8") as f:
            f.write(x["id"] + "\n")


if __name__ == "__main__":
    main()
