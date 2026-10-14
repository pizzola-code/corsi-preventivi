# -*- coding: utf-8 -*-
"""Corsi frequentati sulla scheda contatto (Andrea, 14/10/2026).

Per ogni diretta «Corso | ...» GIA' SVOLTA (external_id CF-<corso>-<ggmm>) legge le iscrizioni con stato Attended
(scritto dai dati di presenza di Zoom) e scrive sul CONTATTO:
  corsi_frequentati          un corso per riga: «gg/mm/aaaa - titolo»
  numero_corsi_frequentati   quanti
  ultimo_corso_frequentato   titolo dell'ultimo
  data_ultimo_corso          data dell'ultimo
Idempotente: si ricalcola tutto a ogni giro e scrive solo dove il valore cambia. Gli indirizzi interni non contano.

  python corsi_partecipazioni.py            giro normale
  python corsi_partecipazioni.py --prova    dice cosa scriverebbe, senza scrivere
  python corsi_partecipazioni.py --campi    crea i campi sul contatto se mancano
"""
import datetime
import re
import sys

import preventivi_corsi as C

EV, REG = "2-143900361", "2-143900355"
NOSTRI = re.compile(r"@(spaggiari\.eu|spaggiarinet\.eu|gruppospaggiari\.it)$", re.I)
CAMPI = [
    {"name": "corsi_frequentati", "label": "Corsi di formazione frequentati", "type": "string", "fieldType": "textarea",
     "description": "Un corso per riga (data - titolo): dirette dei corsi Spaggiari a cui la persona ha partecipato. Scritto in automatico."},
    {"name": "numero_corsi_frequentati", "label": "Numero corsi di formazione frequentati", "type": "number", "fieldType": "number"},
    {"name": "ultimo_corso_frequentato", "label": "Ultimo corso di formazione frequentato", "type": "string", "fieldType": "text"},
    {"name": "data_ultimo_corso", "label": "Data ultimo corso di formazione frequentato", "type": "date", "fieldType": "date"},
]


def crea_campi():
    gruppo = C.hs("/crm/v3/properties/contacts/corsi_richiesti")["groupName"]
    for c in CAMPI:
        r = C.hs("/crm/v3/properties/contacts/" + c["name"])
        if "_err" in r:
            n = C.hs("/crm/v3/properties/contacts", {**c, "groupName": gruppo}, "POST")
            print("  creato campo", c["name"], "->", "ok" if "_err" not in n else n)


def titolo(nome):
    return re.sub(r"^\s*Corso\s*\|\s*", "", nome or "").strip()


def eventi_svolti():
    adesso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out, dopo = [], None
    for _ in range(10):
        r = C.hs("/crm/v3/objects/%s/search" % EV, {"filterGroups": [{"filters": [
            {"propertyName": "external_id", "operator": "CONTAINS_TOKEN", "value": "CF-*"},
            {"propertyName": "start_datetime", "operator": "LT", "value": adesso}]}],
            "properties": ["name", "start_datetime", "external_id", "annullato"], "limit": 100, **({"after": dopo} if dopo else {})}, "POST")
        out += [e for e in r.get("results", []) if e["properties"].get("annullato") != "true" and (e["properties"].get("name") or "").startswith("Corso |")]
        dopo = (r.get("paging") or {}).get("next", {}).get("after")
        if not dopo:
            break
    return out


def presenti(ev_id):
    """[(id iscrizione, email)] con stato Attended."""
    out, dopo = [], None
    for _ in range(20):
        r = C.hs("/crm/v3/objects/%s/search" % REG, {"filterGroups": [{"filters": [
            {"propertyName": "event_id", "operator": "EQ", "value": ev_id},
            {"propertyName": "status", "operator": "EQ", "value": "Attended"}]}],
            "properties": ["email"], "limit": 100, **({"after": dopo} if dopo else {})}, "POST")
        out += [(x["id"], (x["properties"].get("email") or "").strip().lower()) for x in r.get("results", [])]
        dopo = (r.get("paging") or {}).get("next", {}).get("after")
        if not dopo:
            break
    return out


def contatti_di(ids):
    """id iscrizione -> id contatto, in blocchi."""
    m = {}
    for i in range(0, len(ids), 100):
        r = C.hs("/crm/v4/associations/%s/contacts/batch/read" % REG, {"inputs": [{"id": x} for x in ids[i:i + 100]]}, "POST")
        for x in r.get("results", []):
            to = x.get("to") or []
            if to:
                m[str(x["from"]["id"])] = str(to[0]["toObjectId"])
    return m


def giro(prova=False):
    per_contatto = {}                       # id contatto -> {id evento: (data, titolo)}
    for e in eventi_svolti():
        p = e["properties"]
        data = datetime.datetime.fromisoformat(p["start_datetime"].replace("Z", "+00:00")).astimezone(datetime.timezone(datetime.timedelta(hours=2)))
        pres = [(i, m) for i, m in presenti(e["id"]) if m and not NOSTRI.search(m)]
        if not pres:
            continue
        mappa = contatti_di([i for i, _ in pres])
        for i, m in pres:
            cid = mappa.get(i)
            if cid:
                per_contatto.setdefault(cid, {})[e["id"]] = (data.date(), titolo(p["name"]))
        print("  %s %s: %d presenti" % (data.strftime("%d/%m"), titolo(p["name"])[:50], len(pres)))
    scritti = invariati = 0
    ids = list(per_contatto)
    for i in range(0, len(ids), 100):
        blocco = ids[i:i + 100]
        att = C.hs("/crm/v3/objects/contacts/batch/read", {"properties": ["corsi_frequentati", "numero_corsi_frequentati", "ultimo_corso_frequentato", "data_ultimo_corso"],
                   "inputs": [{"id": x} for x in blocco]}, "POST").get("results", [])
        attuali = {str(x["id"]): x["properties"] for x in att}
        da_scrivere = []
        for cid in blocco:
            corsi = sorted(per_contatto[cid].values())
            nuovo = {"corsi_frequentati": "\n".join("%s - %s" % (d.strftime("%d/%m/%Y"), t) for d, t in corsi),
                     "numero_corsi_frequentati": str(len(corsi)), "ultimo_corso_frequentato": corsi[-1][1],
                     "data_ultimo_corso": corsi[-1][0].isoformat()}
            a = attuali.get(cid, {})
            if all(str(a.get(k) or "")[:len(v)] == v or (k == "data_ultimo_corso" and str(a.get(k) or "")[:10] == v) for k, v in nuovo.items()):
                invariati += 1
            else:
                da_scrivere.append({"id": cid, "properties": nuovo})
        if da_scrivere and not prova:
            r = C.hs("/crm/v3/objects/contacts/batch/update", {"inputs": da_scrivere}, "POST")
            if "_err" in r:
                print("  ERRORE scrittura:", r)
                continue
        scritti += len(da_scrivere)
    print("corsi frequentati: %d contatti con almeno un corso · %s %d · invariati %d" % (len(ids), "da scrivere" if prova else "scritti", scritti, invariati))


if __name__ == "__main__":
    if "--campi" in sys.argv:
        crea_campi()
    else:
        giro("--prova" in sys.argv)
