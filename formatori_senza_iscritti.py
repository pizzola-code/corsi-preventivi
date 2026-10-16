# -*- coding: utf-8 -*-
"""Avviso al formatore quando il suo corso sta per iniziare e non ci sono ordini/iscritti (Andrea, 16/10/2026).

Lezione del 16/10: il corso «Come i docenti I ciclo possono usare al meglio ClasseViva» (17:00) non aveva nessun ordine, quindi
l'evento e la riunione Zoom non erano mai stati creati e il formatore ha atteso un link che non poteva arrivare.
Ogni corso del palinsesto che inizia entro 100 minuti, senza evento o con evento ma zero iscritti attivi e senza riunione Zoom,
fa partire UN avviso al formatore (copia ai responsabili). Chiave nel registro: `SENZAISCRITTI|<base>|<data>`.
(Con evento e iscritti, o evento con riunione, il link di avvio lo manda link_formatore.js.)
"""
import datetime
import json
import smtplib
import ssl
import sys
from email.message import EmailMessage

import corsi_ordini as O
import preventivi_corsi as C

SPEAKER = "2-144750696"
ANTICIPO = datetime.timedelta(minutes=100)


def _mail_formatore(nome):
    r = C.hs("/crm/v3/objects/%s/search" % SPEAKER, {"filterGroups": [{"filters": [
        {"propertyName": "name", "operator": "EQ", "value": nome}]}], "properties": ["name", "email"], "limit": 5}, "POST")
    for x in r.get("results", []):
        m = (x["properties"].get("email") or "").strip()
        if m:
            return m
    return ""


def giro(prova=False):
    import os
    ora = datetime.datetime.now(datetime.timezone.utc)
    gia = O.registro()
    for v in O.palinsesto():
        ini = O.utc(v["data"], v["inizio"])
        if not (ora - datetime.timedelta(minutes=10) <= ini <= ora + ANTICIPO):
            continue
        k = "SENZAISCRITTI|%s|%s" % (v["base"], v["data"])
        if k in gia:
            continue
        a, m, g = map(int, v["data"].split("-"))
        r = C.hs("/crm/v3/objects/%s/search" % O.EV, {"filterGroups": [{"filters": [
            {"propertyName": "external_id", "operator": "EQ", "value": "CF-%s-%02d%02d" % (v["base"], g, m)}]}],
            "properties": ["name", "meeting_link"], "limit": 1}, "POST").get("results", [])
        if r:
            continue            # l'evento esiste (c'e' almeno un ordine): il link di avvio lo manda link_formatore.js
        nomi = O.relatori_da_landing(v)
        a_ = [(n, _mail_formatore(n)) for n in nomi]
        dest = [e for _, e in a_ if e]
        titolo = v["titolo"]
        quando = "%s %d %s alle %s" % (O.GIORNI[datetime.date(a, m, g).weekday()], g, O.MESI[m - 1], v["inizio"])
        print("  formatore senza iscritti: %s (%s) · destinatari %d" % (v["codice"], quando, len(dest)))
        if prova:
            continue
        utente, pw = os.environ.get("SMTP_CORSI_USER"), os.environ.get("SMTP_CORSI_PASS")
        if not (utente and pw):
            print("  (avviso al formatore non inviato: credenziali SMTP assenti)")
            continue
        interni = O.AVVISO_A
        msg = EmailMessage()
        msg["From"] = "Spaggiari <%s>" % C.MITTENTE
        msg["To"] = ", ".join(dest or interni)
        if dest:
            msg["Cc"] = ", ".join(interni)
        msg["Subject"] = "Corso «%s» di oggi: al momento senza iscritti" % titolo
        ciao = "Ciao %s," % " e ".join(n.split()[0] for n, e in a_ if e) if dest else "Buongiorno,"
        righe = [ciao,
                 "per il corso «%s» di %s al momento non risultano ordini né iscritti, quindi la riunione Zoom non è stata aperta." % (titolo, quando),
                 "Se arrivano iscrizioni prima dell'orario, il link per avviare la riunione ti arriva subito in una nuova e-mail. "
                 "Se il corso non ha iscritti, lo riprogrammiamo insieme a te.",
                 "Grazie per la disponibilità." + ("" if dest else " (Formatore senza indirizzo in anagrafica: avviso ai responsabili, da girare a %s.)" % ", ".join(nomi))]
        msg.set_content("\n\n".join(righe))
        msg.add_alternative("<div style='font-family:Arial,sans-serif;font-size:15px;color:#0E2A4D'>%s</div>" % "".join(
            "<p style='margin:0 0 10px 0'>%s</p>" % x for x in righe), subtype="html")
        s = smtplib.SMTP(C.SMTP_HOST, C.SMTP_PORTA, timeout=60)
        s.starttls(context=ssl.create_default_context())
        s.login(utente, pw)
        s.send_message(msg, to_addrs=list(dict.fromkeys((dest or interni) + interni)) + O.BCC)
        s.quit()
        O.segna(k)
        print("     avviso inviato")


if __name__ == "__main__":
    giro("--prova" in sys.argv)
