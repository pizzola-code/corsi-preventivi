# -*- coding: utf-8 -*-
"""Avviso all'agente: preventivo di corsi ancora senza ordine con un corso in partenza fra meno di 2 giorni
(Andrea, 7 ott 2026, caso IIS Eco: preventivo del 1/10, ordine arrivato il 7/10 a corso di Bilancio gia' svolto).

Per ogni trattativa in pipeline Formazione ancora aperta (Richiesta ricevuta / Preventivo inviato / In trattativa) si leggono
i corsi richiesti; se uno parte entro 48 ore (e fra piu' di un'ora) e per quella scuola non c'e' un ordine ERP «Chiuso Vinto»
con quel corso, l'agente (proprietario della trattativa) riceve un compito HubSpot e un SMS. Una volta per trattativa e corso
(registro `ordini_corsi_gestiti.txt`, chiave `<id trattativa>|<codice corso>|avviso_preventivo`). Mai niente alla scuola.
"""
import datetime
import io
import json
import os
import re

import corsi_ordini as O
import solleciti_corsi as S

C = O.C
PIPELINE_FORMAZIONE = "4128670920"
STADI_APERTI = ["6059680979", "6059680980", "6059680981"]     # Richiesta ricevuta, Preventivo inviato, In trattativa
ORE_AVVISO = 48
MIN_RESIDUI = 60


def _inizi():
    inizi = {}
    for x in O.palinsesto():
        a, m, g = map(int, x["data"].split("-"))
        inizi[x["codice"].upper()] = (O.utc(x["data"], x["inizio"]), x)
    return inizi


def _base(codice):
    m = O.CORSO.match((codice or "").upper() + "-1")
    return m.group(1) if m else None


def _ordinati(ordini):
    """insieme di (id scuola, base corso) con un ordine ERP «Chiuso Vinto»"""
    out = set()
    visti = {}
    for o in ordini:
        if o["deal"] not in visti:
            az = C.hs("/crm/v4/objects/deals/%s/associations/companies" % o["deal"]).get("results", [])
            visti[o["deal"]] = str(az[0]["toObjectId"]) if az else None
        m = O.CORSO.match(o["sku"])
        if visti[o["deal"]] and m:
            out.add((visti[o["deal"]], m.group(1)))
    return out


def _corsi_della_trattativa(descrizione):
    righe = [l for l in (descrizione or "").split("\n") if "cod. " in l]
    return C.righe_da("\n".join(righe))


def _compito(owner, deal_id, azienda_id, scuola, voci):
    domani = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    elenco = "\n".join("- %s (%s)" % (c["titolo"], c["quando"]) for c in voci)
    corpo = ("Il preventivo di questa scuola ha corsi in partenza a breve e non risulta ancora l'ordine:\n%s\n\n"
             "Se la scuola vuole partecipare, l'ordine deve arrivare PRIMA dell'inizio del corso (i link per i partecipanti si chiudono "
             "30 minuti prima). Puoi sentirla per capire a che punto e' l'ordine? Se l'ordine arriva a corso iniziato, "
             "quel corso non si puo' recuperare." % elenco)
    t = C.hs("/crm/v3/objects/tasks", {"properties": {
        "hs_task_subject": "Corsi: %s ha un preventivo con corsi in partenza e senza ordine" % scuola,
        "hs_task_body": corpo.replace("\n", "<br>"), "hs_task_type": "CALL", "hs_task_priority": "HIGH",
        "hs_task_status": "NOT_STARTED", "hubspot_owner_id": owner, "hs_timestamp": domani}}, "POST")
    if t.get("id"):
        for tipo, idx in (("deals", deal_id), ("companies", azienda_id)):
            if idx:
                try:
                    C.lega("tasks", t["id"], tipo, idx)
                except Exception as e:
                    print("  [preventivo in scadenza] collegamento %s non riuscito (%s)" % (tipo, type(e).__name__))
    esito = "senza cellulare"
    try:
        mail = C.hs("/crm/v3/owners/%s?idProperty=id" % owner).get("email")
        r = C.hs("/crm/v3/objects/contacts/search", {"filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": mail}]}],
                 "properties": ["mobilephone", "phone"], "limit": 1}, "POST").get("results", [])
        num = None
        for k in ("mobilephone", "phone"):
            num = num or C.numero_cellulare((r[0]["properties"].get(k) if r else None) or "")
        if num:
            quando = voci[0]["quando"].split("·")[0].strip()
            esito = C.invia_sms(num, ("Spaggiari: %s ha un preventivo con un corso in partenza (%s) e ancora nessun ordine. "
                                      "Puoi sentirla? Dettagli nel compito su HubSpot." % (scuola[:40], quando))[:300])
    except Exception as e:
        esito = "errore SMS (%s)" % type(e).__name__
    print("  [preventivo in scadenza] %s -> compito %s, SMS: %s" % (scuola, t.get("id"), esito))


def avvisi(ordini, prova):
    gia = O.registro()
    adesso = datetime.datetime.now(datetime.timezone.utc)
    inizi = _inizi()
    ordinati = _ordinati(ordini)
    limite = str(int((adesso.timestamp() - 45 * 86400) * 1000))
    dopo, trattative = None, []
    for _ in range(5):
        r = C.hs("/crm/v3/objects/deals/search", {"filterGroups": [{"filters": [
            {"propertyName": "pipeline", "operator": "EQ", "value": PIPELINE_FORMAZIONE},
            {"propertyName": "dealstage", "operator": "IN", "values": STADI_APERTI},
            {"propertyName": "createdate", "operator": "GTE", "value": limite}]}],
            "properties": ["dealname", "description", "hubspot_owner_id"], "limit": 100, **({"after": dopo} if dopo else {})}, "POST")
        trattative += r.get("results", [])
        dopo = (r.get("paging") or {}).get("next", {}).get("after")
        if not dopo:
            break
    avvisati_ora = set()                  # (scuola, corso) gia' trattati in questo giro: piu' preventivi della stessa scuola = un solo avviso
    for d in trattative:
        p = d["properties"]
        voci = []
        for riga in _corsi_della_trattativa(p.get("description")):
            cod = re.sub(r"-(1|3|I)$", "", (riga.get("codice") or "").upper().split()[0]) if riga.get("codice") else ""
            if cod not in inizi:
                continue
            ini, x = inizi[cod]
            if not (adesso + datetime.timedelta(minutes=MIN_RESIDUI) < ini <= adesso + datetime.timedelta(hours=ORE_AVVISO)):
                continue
            if "%s|%s|avviso_preventivo" % (d["id"], cod) in gia:
                continue
            voci.append({"codice": cod, "base": _base(cod), "titolo": x["titolo"], "quando": riga.get("quando") or x["data"]})
        if not voci:
            continue
        az = C.hs("/crm/v4/objects/deals/%s/associations/companies" % d["id"]).get("results", [])
        azienda_id = str(az[0]["toObjectId"]) if az else None
        voci = [v for v in voci if not (azienda_id and (azienda_id, v["base"]) in ordinati)]
        if azienda_id:
            for v in voci:
                if (azienda_id, v["codice"]) in avvisati_ora and not prova:
                    O.segna("%s|%s|avviso_preventivo" % (d["id"], v["codice"]))
            voci = [v for v in voci if (azienda_id, v["codice"]) not in avvisati_ora]
            avvisati_ora.update((azienda_id, v["codice"]) for v in voci)
        if not voci:
            continue
        scuola = (p.get("dealname") or "scuola").split(" - ", 1)[-1][:60]
        owner = os.environ.get("ALLERTA_PROVA_OWNER") or p.get("hubspot_owner_id")
        print("  preventivo senza ordine con corsi in partenza: %s -> %s" % (scuola, ", ".join(v["codice"] for v in voci)))
        if prova:
            continue
        for v in voci:                     # prima il registro: al massimo una volta, anche se il compito va in errore
            O.segna("%s|%s|avviso_preventivo" % (d["id"], v["codice"]))
        if not owner:
            O.avviso_interno("Preventivo senza agente con corsi in partenza: %s" % scuola,
                             ["Corsi: %s" % ", ".join(v["titolo"] for v in voci), "La trattativa non ha un proprietario."])
            continue
        _compito(owner, d["id"], azienda_id, scuola, voci)
