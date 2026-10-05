# -*- coding: utf-8 -*-
"""Solleciti ai corsi senza partecipanti nominati (Andrea, 5 ott 2026).

Per ogni ordine di corso (riga d'ordine = ordine + corso) con evento in programma e NESSUNA persona nominata:
  - 3 giorni prima dell'inizio: promemoria alla scuola dal portale HubSpot (stessi destinatari dell'invito),
    solo se l'invito e' partito da almeno 24 ore;
  - 1 giorno prima: secondo promemoria alla scuola (se l'invito e' partito da almeno 6 ore) e avviso interno
    al team con l'elenco dei corsi ancora senza partecipanti;
  - 6 ore prima: ultimo avviso interno (la nomina si chiude 4 ore prima).
Ogni passo parte una volta sola (registro `ordini_corsi_gestiti.txt`, chiavi `ordine|codice|sollecito3`, `|sollecito1`,
`|allarme1`, `|allarme6`). Con CORSI_INVIO_AUTOMATICO diverso da 1 non parte nessuna e-mail alla scuola: solo l'avviso interno.
"""
import datetime
import os
import sys

import corsi_ordini as O

C = O.C
EV, REG = "2-143900361", "2-143900355"
EMAIL_PROMEMORIA = 485046044875          # «CORSI - Promemoria nomina partecipanti (invio da API)»
ORE_3G, ORE_1G, ORE_ALLARME = 72, 24, 6


def _ore_a(iso):
    ini = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (ini - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 3600.0


def _ore_da(iso):
    d = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (datetime.datetime.now(datetime.timezone.utc) - d).total_seconds() / 3600.0


def nominati(evento_id, ordine):
    r = C.hs("/crm/v3/objects/%s/search" % REG, {"filterGroups": [{"filters": [
        {"propertyName": "event_id", "operator": "EQ", "value": str(evento_id)},
        {"propertyName": "ordine_corso", "operator": "EQ", "value": ordine},
        {"propertyName": "status", "operator": "NEQ", "value": "Canceled"}]}], "properties": ["email"], "limit": 1}, "POST")
    return r.get("total", len(r.get("results", [])))


def solleciti(ordini, auto, prova):
    gia = O.registro()
    ora_ordine = {}
    senza = []                                  # per l'avviso interno
    for o in ordini:
        if "%s|%s|inviato" % (o["num"], o["sku"]) not in gia:
            continue                            # l'invito non e' ancora partito: niente promemoria
        base = O.CORSO.match(o["sku"]).group(1)
        ev, _ = O.evento_per(base, crea=False, prova=True)
        if not ev:
            continue
        p = ev["properties"] if "start_datetime" in ev["properties"] else C.hs(
            "/crm/v3/objects/%s/%s?properties=name,start_datetime,end_datetime,annullato" % (EV, ev["id"]))["properties"]
        if not p.get("start_datetime") or p.get("annullato") == "true":
            continue
        ore = _ore_a(p["start_datetime"])
        if ore <= 0 or ore > ORE_3G:
            continue
        if nominati(ev["id"], o["num"]) > 0:
            continue
        if o["deal"] not in ora_ordine:
            ora_ordine[o["deal"]] = C.hs("/crm/v3/objects/deals/%s?properties=createdate" % o["deal"])["properties"].get("createdate")
        eta_ordine = _ore_da(ora_ordine[o["deal"]]) if ora_ordine[o["deal"]] else 999
        k3, k1, a1, a6 = ("%s|%s|%s" % (o["num"], o["sku"], x) for x in ("sollecito3", "sollecito1", "allarme1", "allarme6"))
        v, _ = O.prepara(o, True)
        if not v or v.get("mancante"):
            continue
        da_mandare = None
        if ore <= ORE_1G and k1 not in gia and eta_ordine >= 6:
            da_mandare = k1
        elif ore <= ORE_3G and ore > ORE_1G and k3 not in gia and eta_ordine >= 24:
            da_mandare = k3
        # avvisi al team
        nome = O.pulito(v["azienda"].get("name"))
        if ore <= ORE_ALLARME and a6 not in gia:
            senza.append((a6, "<b>%s</b> · %s · %s · il corso inizia fra circa %d ore, la nomina si chiude 4 ore prima" % (nome, v["corso"], v["quando"], round(ore))))
        elif ore <= ORE_1G and a1 not in gia and ore > ORE_ALLARME:
            senza.append((a1, "<b>%s</b> · %s · %s · nessun partecipante indicato a meno di 24 ore dall'inizio" % (nome, v["corso"], v["quando"])))
        if not da_mandare:
            continue
        if not auto:
            print("  [solleciti] %s %s: promemoria da mandare (invio automatico spento)" % (o["num"], o["sku"]))
            continue
        dest, motivo = O.destinatari(v["azienda_id"])
        if not dest:
            print("  [solleciti] %s %s: destinatari da decidere (%s)" % (o["num"], o["sku"], motivo))
            continue
        agente = O.email_agente(o["deal"])
        cc = O.CC_FISSI + ([agente] if agente and agente not in O.CC_FISSI else [])
        dati = O.dati_email([v])
        dati["oggetto"] = "Promemoria: corso «%s», indicare chi partecipa" % v["corso"]
        print("  [solleciti] %s %s -> %s (%s)%s" % (o["num"], o["sku"], ", ".join(e for e, _ in dest), "3 giorni" if da_mandare == k3 else "1 giorno", " [prova]" if prova else ""))
        if prova:
            continue
        corpo = {"emailId": EMAIL_PROMEMORIA, "message": {"to": dest[0][0], "cc": [x for x in ([e for e, _ in dest[1:]] + cc) if x], "bcc": O.BCC},
                 "customProperties": dati}
        C.hs("/marketing/v3/transactional/single-email/send", corpo, "POST")
        O.segna(da_mandare)
    if senza and not prova:
        righe = [r for _, r in senza]
        O.avviso_interno("Corsi in diretta senza partecipanti indicati", righe + [
            "La scuola ha ricevuto l'invito con il link di nomina. Se serve si puo' indicare qualcuno a mano dalla funzione di nomina."])
        for k, _ in senza:
            O.segna(k)


if __name__ == "__main__":
    prova = "--prova" in sys.argv
    solleciti(O.ordini_nuovi(60), os.environ.get("CORSI_INVIO_AUTOMATICO") == "1", prova)
    O.spedisci_riepilogo()
