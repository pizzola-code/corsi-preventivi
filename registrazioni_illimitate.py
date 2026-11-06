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


ATTESA_PRESENZE = datetime.timedelta(hours=18)    # le presenze Zoom si scrivono 3 volte al giorno (refresh.yml): prima di allora «0 minuti» non e' una misura
MINUTI_MIN = 30


def _non_ha_seguito(p, fine_ev, ora):
    """Nominato (ordine da 1-3 posti) che non ha seguito la diretta: nessun minuto o al massimo 30, a presenze gia' aggiornate.
    Non riguarda chi ha gia' ricevuto il link personale (campo registrazione_link_inviato_il)."""
    if p.get("registrazione_link_inviato_il"):
        return False
    try:
        fine = datetime.datetime.fromisoformat(fine_ev.replace("Z", "+00:00"))
    except ValueError:
        return False
    if ora - fine < ATTESA_PRESENZE:
        return False
    m = (p.get("attestato_minuti") or "").strip()
    try:
        return int(float(m)) <= MINUTI_MIN if m else True
    except ValueError:
        return True


def nomine_tardive(os_, auto, prova=False):
    """Ordine arrivato PRIMA della diretta, scuola invitata, ma nessuno nominato (il link di nomina chiude 30 minuti prima dell'inizio):
    a corso finito con registrazione su Vimeo la scuola riceve l'e-mail «indica chi la guarderà» (stesso modello degli ordini a corso finito).
    Una volta sola per riga d'ordine (registro `<ordine>|<sku>|nomina-reg-tardiva`). Gli ordini arrivati a corso finito li gestisce gia' lavora_ordine."""
    gia = O.registro()
    per_base = {}
    for o in os_:
        m = O.CORSO.match(o["sku"])
        if not m or not o.get("creato"):
            continue
        if "%s|%s|inviato" % (o["num"], o["sku"]) not in gia:
            continue                                  # non ancora invitato: se ne occupa lavora_ordine
        if any(("%s|%s|%s" % (o["num"], o["sku"], s)) in gia for s in ("nomina-reg-tardiva", "nomina-reg-ok", "registrazione-inviata")):
            continue
        per_base.setdefault(m.group(1), []).append(o)
    if not per_base:
        return 0
    ora = datetime.datetime.now(datetime.timezone.utc)
    eventi = _cerca(EV, [{"propertyName": "external_id", "operator": "CONTAINS_TOKEN", "value": "CF-*"},
                         {"propertyName": "end_datetime", "operator": "GTE", "value": DAL.strftime("%Y-%m-%dT%H:%M:%SZ")},
                         {"propertyName": "end_datetime", "operator": "LTE", "value": (ora - ATTESA).strftime("%Y-%m-%dT%H:%M:%SZ")},
                         {"propertyName": "recording_link", "operator": "HAS_PROPERTY"}],
                    ["name", "external_id", "end_datetime", "annullato"])
    n = 0
    for e in eventi:
        p = e["properties"]
        if p.get("annullato") == "true" or not (p.get("name") or "").startswith("Corso |"):
            continue
        m = re.match(r"^CF-([A-Z0-9]+)-\d{4}$", (p.get("external_id") or "").upper())
        for o in (per_base.get(m.group(1)) if m else None) or []:
            fine_ev = p.get("end_datetime") or ""
            if not o.get("creato") or o["creato"][:19] >= fine_ev[:19]:
                continue                              # ordine arrivato a corso finito: nomina gia' mandata da lavora_ordine
            isc = _cerca(REG, [{"propertyName": "event_id", "operator": "EQ", "value": e["id"]},
                               {"propertyName": "status", "operator": "NEQ", "value": "Canceled"},
                               {"propertyName": "ordine_corso", "operator": "EQ", "value": o["num"]}], ["email"])
            if isc:
                if not prova:
                    O.segna("%s|%s|nomina-reg-ok" % (o["num"], o["sku"]))     # qualcuno e' stato nominato: la registrazione la gestisce il giro normale
                continue
            n += 1
            print("  nomina tardiva · %s %s: ordine anteriore al corso, nessun nominato" % (o["num"], o["sku"]))
            if prova or not auto:
                continue
            v, _ = O.prepara(o, False)
            if not v or v.get("mancante") or not v.get("registrazione"):
                continue
            if O.invia_nomina_registrazione([(o, v)], True, False, gia - {"%s|%s|inviato" % (o["num"], o["sku"])}):
                O.segna("%s|%s|nomina-reg-tardiva" % (o["num"], o["sku"]))
    return n


def giro(os_, auto, prova=False):
    """os_: ordini nel giro (corsi_ordini.ordini_nuovi). Restituisce quante e-mail ha inviato (o inviarebbe)."""
    import os
    if os.path.exists(os.path.join(O.QUI, "pausa_registrazioni.txt")):
        print("  registrazione illimitati: IN PAUSA (file pausa_registrazioni.txt)")
        return 0
    illim, singoli = {}, {}                       # base corso -> numeri d'ordine illimitati / da 1-3 posti (registrazione a corso finito)
    for o in os_:
        m = O.CORSO.match(o["sku"])
        if m:
            (illim if m.group(2) == "I" else singoli).setdefault(m.group(1), set()).add(o["num"])
    if not illim and not singoli:
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
        ordini = ((illim.get(m.group(1)) or set()) | (singoli.get(m.group(1)) or set())) if m else None
        if not ordini:
            continue
        fine_ev = p.get("end_datetime") or ""
        isc = _cerca(REG, [{"propertyName": "event_id", "operator": "EQ", "value": e["id"]},
                           {"propertyName": "status", "operator": "NEQ", "value": "Canceled"},
                           {"propertyName": "ordine_corso", "operator": "IN", "values": sorted(ordini)}],
                     ["email", "first_name", "ordine_corso", "hs_createdate", "status", "attestato_minuti", "registrazione_link_inviato_il"])
        # ordini da 1-3 posti: riceve la registrazione chi e' stato nominato a corso finito (ordine arrivato dopo la diretta)
        # e, dal 6/11/2026, chi era nominato ma non ha seguito la diretta (assente o meno di 30 minuti): vedi _non_ha_seguito.
        isc = [i for i in isc if i["properties"].get("ordine_corso") in illim.get(m.group(1), set())
               or (i["properties"].get("hs_createdate") or "") >= fine_ev
               or _non_ha_seguito(i["properties"], fine_ev, ora)]
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
    os__ = O.ordini_nuovi(30)
    n = giro(os__, True, prova)
    print("registrazioni agli illimitati:", n, "(prova)" if prova else "")
    print("nomine tardive:", nomine_tardive(os__, True, True))
