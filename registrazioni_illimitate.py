# -*- coding: utf-8 -*-
"""Registrazione ai partecipanti delle licenze ILLIMITATE (Andrea, 16/10/2026).

Per ogni corso in diretta (evento «Corso | ...», codice CF-<corso>-<ggmm>) con almeno un ordine ILLIMITATO (riga WBRELE<corso>-I):
un'ora dopo la fine, o appena la registrazione Vimeo e' scritta nell'evento (`recording_link`, lo fa vimeo_link_eventi.js ogni ora),
OGNI iscritto non annullato di quegli ordini riceve da HubSpot l'e-mail «Registrazione disponibile» con il link.
Una sola volta per persona e per evento (registro `RECI|<evento>|<indirizzo>` in ordini_corsi_gestiti.txt).
Vale per tutti i corsi del nuovo catalogo (dal 5/10/2026) e per tutti i futuri; i corsi con licenza da 1 o 3 posti non rientrano
(per quelli resta la regola dell'ordine arrivato a corso finito).

Gira dentro corsi_ordini.py (ogni 5 minuti). Uso a mano:
  python registrazioni_illimitate.py --prova      elenca chi riceverebbe la registrazione, senza inviare
"""
import datetime
import re
import sys
import time

import corsi_ordini as O
import preventivi_corsi as C
import registrazioni_personali as P

EV, REG = O.EV, O.REG
NOSTRI = re.compile(r"@(spaggiari\.eu|spaggiarinet\.eu|gruppospaggiari\.it)$", re.I)
DAL = datetime.datetime(2026, 10, 1, 0, 0, tzinfo=datetime.timezone.utc)       # tutti i corsi del nuovo catalogo (il primo e il 5/10): esteso il 16/10
ATTESA = datetime.timedelta(minutes=60)


def _cerca(obj, filtri, props):
    out, dopo = [], None
    for _ in range(30):
        r = C.hs("/crm/v3/objects/%s/search" % obj, {"filterGroups": [{"filters": filtri}], "properties": props, "limit": 100,
                                                      **({"after": dopo} if dopo else {})}, "POST")
        out += r.get("results", [])
        dopo = (r.get("paging") or {}).get("next", {}).get("after")
        if not dopo:
            break
    return out


def giro(os_, auto, prova=False):
    """os_: ordini nel giro (corsi_ordini.ordini_nuovi). Restituisce quante e-mail ha inviato (o inviarebbe)."""
    import os
    if os.path.exists(os.path.join(O.QUI, "pausa_registrazioni.txt")):
        print("  registrazione illimitati: IN PAUSA (file pausa_registrazioni.txt)")
        return 0
    illim = {}                                    # base corso -> numeri d'ordine con licenza illimitata
    for o in os_:
        m = O.CORSO.match(o["sku"])
        if m and m.group(2) == "I":
            illim.setdefault(m.group(1), set()).add(o["num"])
    if not illim:
        return 0
    ora = datetime.datetime.now(datetime.timezone.utc)
    eventi = _cerca(EV, [{"propertyName": "external_id", "operator": "CONTAINS_TOKEN", "value": "CF-*"},
                         {"propertyName": "end_datetime", "operator": "GTE", "value": DAL.strftime("%Y-%m-%dT%H:%M:%SZ")},
                         {"propertyName": "end_datetime", "operator": "LTE", "value": (ora - ATTESA).strftime("%Y-%m-%dT%H:%M:%SZ")},
                         {"propertyName": "recording_link", "operator": "HAS_PROPERTY"}],
                    ["name", "external_id", "start_datetime", "end_datetime", "recording_link", "annullato"])
    gia = O.registro()
    tot = 0
    for e in eventi:
        p = e["properties"]
        if p.get("annullato") == "true" or not (p.get("name") or "").startswith("Corso |"):
            continue
        m = re.match(r"^CF-([A-Z0-9]+)-\d{4}$", (p.get("external_id") or "").upper())
        ordini = illim.get(m.group(1)) if m else None
        if not ordini:
            continue
        isc = _cerca(REG, [{"propertyName": "event_id", "operator": "EQ", "value": e["id"]},
                           {"propertyName": "status", "operator": "NEQ", "value": "Canceled"},
                           {"propertyName": "ordine_corso", "operator": "IN", "values": sorted(ordini)}],
                     ["email", "first_name", "ordine_corso"])
        ini = O.ora_italiana(p["start_datetime"])
        giorno = "%s %d %s %d" % (O.GIORNI[ini.weekday()], ini.day, O.MESI[ini.month - 1], ini.year)
        visti, inviate, nuove = set(), 0, 0
        for i in isc:
            mail = (i["properties"].get("email") or "").strip().lower()
            if not mail or mail in visti or NOSTRI.search(mail) or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", mail):
                continue
            visti.add(mail)
            chiave = "RECP|%s|%s" % (e["id"], mail)          # link PERSONALE (pagina guarda-registrazione), dal 16/10
            if chiave in gia:
                continue
            rinnovo = ("RECI|%s|%s" % (e["id"], mail)) in gia   # aveva gia' ricevuto il link diretto: gli arriva quello personale
            tot += 1
            nuove += 1
            if prova or not auto:
                continue
            scad = P.scadenza_iscrizione(i["id"])
            url, scad = P.link(i["id"], e["id"], scad)
            r = C.hs("/marketing/v3/transactional/single-email/send", {
                "emailId": O.EMAIL_REG, "message": {"to": mail},
                "customProperties": {"nome": (i["properties"].get("first_name") or "").strip().title(),
                                     "evento": p["name"] + (" (link personale)" if rinnovo else ""),
                                     "quando": giorno, "link": url}}, "POST")
            if "_err" in r:
                print("  registrazione illimitata: invio non riuscito (%s)" % r.get("_err"))
                continue
            O.segna(chiave)
            iso = lambda ms: datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            C.hs("/crm/v3/objects/%s/%s" % (REG, i["id"]), {"properties": {"registrazione_scadenza": iso(scad),
                 "registrazione_link_inviato_il": iso(time.time() * 1000)}}, "PATCH")
            inviate += 1
            time.sleep(0.3)
        print("  registrazione illimitati · %s: %d iscritti degli ordini illimitati, %s %d" % (
            p.get("external_id"), len(visti), "da inviare" if (prova or not auto) else "inviate", nuove if (prova or not auto) else inviate))
    return tot


if __name__ == "__main__":
    prova = "--prova" in sys.argv
    n = giro(O.ordini_nuovi(30), True, prova)
    print("registrazioni agli illimitati:", n, "(prova)" if prova else "")
