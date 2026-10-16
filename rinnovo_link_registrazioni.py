# -*- coding: utf-8 -*-
"""Una tantum (16/10/2026): gli ordini di registrazione partiti col vecchio link Vimeo DIRETTO (chiave `registrazione-inviata`)
ora trovano il video bloccato (incorporabile solo da spaggiari.eu). Alla scuola parte la e-mail di nomina della registrazione:
indica chi guarda e ogni persona riceve il link personale. Una volta sola per ordine (`ordine|codice|rinnovo-link`).
  python rinnovo_link_registrazioni.py --prova      mostra a chi partirebbe"""
import sys

import corsi_ordini as O


def main(prova):
    gia = O.registro()
    for o in O.ordini_nuovi(30):
        if "%s|%s|registrazione-inviata" % (o["num"], o["sku"]) not in gia:
            continue
        k = "%s|%s|rinnovo-link" % (o["num"], o["sku"])
        if k in gia:
            continue
        v, motivo = O.prepara(o, True)
        if not v or v.get("mancante"):
            print("  %s: non pronto (%s)" % (o["num"], motivo))
            continue
        dest, mot = O.destinatari(v["azienda_id"])
        if not dest:
            print("  %s: destinatari da decidere (%s)" % (o["num"], mot))
            continue
        print("  %s %s -> %d destinatari (%s)%s" % (o["num"], o["sku"], len(dest), mot, " [prova]" if prova else ""))
        if prova:
            continue
        agente = O.email_agente(o["deal"])
        cc = O.CC_FISSI + ([agente] if agente and agente not in O.CC_FISSI else [])
        dati = O.dati_email([v])
        dati["oggetto"] = "Registrazione del corso «%s»: ora il link è personale, indica chi la guarda" % v["corso"]
        r = O.C.hs("/marketing/v3/transactional/single-email/send", {"emailId": O.EMAIL_NOMINA_REG, "message": {"to": dest[0][0],
                   "cc": [x for x in ([e for e, _ in dest[1:]] + cc) if x], "bcc": O.BCC}, "customProperties": dati}, "POST")
        print("     inviata:", r.get("status"))
        if "_err" not in r:
            O.segna(k)


if __name__ == "__main__":
    main("--prova" in sys.argv)
