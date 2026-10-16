# -*- coding: utf-8 -*-
"""Corsi frequentati e iscrizioni sulla scheda contatto (Andrea, 14/10/2026).

Per ogni diretta «Corso | ...» (external_id CF-<corso>-<ggmm>) legge le iscrizioni non annullate e scrive sul CONTATTO:
  corsi_frequentati          corsi svolti con stato Attended (presenza Zoom): un corso per riga «gg/mm/aaaa - titolo»
  numero_corsi_frequentati   quanti
  ultimo_corso_frequentato   titolo dell'ultimo
  data_ultimo_corso          data dell'ultimo
  corsi_iscritto_prossimi    corsi futuri a cui e' iscritta (si svuota da sola quando il corso passa)
  corsi_non_frequentati      corsi svolti a cui era iscritta e non e' stata presente (dopo 18 ore, il tempo delle presenze Zoom)
Idempotente: si ricalcola tutto a ogni giro e scrive solo dove il valore cambia. Gli indirizzi interni non contano.
Lo stato delle iscrizioni non si tocca (i flussi di hapily ci sono agganciati).

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
    {"name": "corsi_iscritto_prossimi", "label": "Corsi di formazione: iscritto ai prossimi", "type": "string", "fieldType": "textarea",
     "description": "Dirette dei corsi future a cui la persona e' iscritta (data - titolo). Scritto in automatico."},
    {"name": "corsi_non_frequentati", "label": "Corsi di formazione: iscritto e non presente", "type": "string", "fieldType": "textarea",
     "description": "Corsi gia' svolti a cui la persona era iscritta e non ha partecipato (data - titolo). Scritto in automatico."},
]
CAMPI_LETTI = [c["name"] for c in CAMPI]


def crea_campi():
    gruppo = C.hs("/crm/v3/properties/contacts/corsi_richiesti")["groupName"]
    for c in CAMPI:
        r = C.hs("/crm/v3/properties/contacts/" + c["name"])
        if "_err" in r:
            n = C.hs("/crm/v3/properties/contacts", {**c, "groupName": gruppo}, "POST")
            print("  creato campo", c["name"], "->", "ok" if "_err" not in n else n)


def titolo(nome):
    return re.sub(r"^\s*Corso\s*\|\s*", "", nome or "").strip()


def eventi_corsi():
    out, dopo = [], None
    for _ in range(10):
        r = C.hs("/crm/v3/objects/%s/search" % EV, {"filterGroups": [{"filters": [
            {"propertyName": "external_id", "operator": "CONTAINS_TOKEN", "value": "CF-*"}]}],
            "properties": ["name", "start_datetime", "end_datetime", "external_id", "annullato"], "limit": 100, **({"after": dopo} if dopo else {})}, "POST")
        out += [e for e in r.get("results", []) if e["properties"].get("annullato") != "true"
                and (e["properties"].get("name") or "").startswith("Corso |") and e["properties"].get("start_datetime")]
        dopo = (r.get("paging") or {}).get("next", {}).get("after")
        if not dopo:
            break
    return out


def iscrizioni(ev_id):
    """[(id iscrizione, email, stato)] non annullate."""
    out, dopo = [], None
    for _ in range(20):
        r = C.hs("/crm/v3/objects/%s/search" % REG, {"filterGroups": [{"filters": [
            {"propertyName": "event_id", "operator": "EQ", "value": ev_id},
            {"propertyName": "status", "operator": "NEQ", "value": "Canceled"}]}],
            "properties": ["email", "status", "solo_registrazione"], "limit": 100, **({"after": dopo} if dopo else {})}, "POST")
        # 16/10/2026: chi guarda la registrazione (nominato a corso finito) non e' un assente della diretta
        out += [(x["id"], (x["properties"].get("email") or "").strip().lower(), x["properties"].get("status")) for x in r.get("results", [])
                if not x["properties"].get("solo_registrazione")]
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


def riga(d, t):
    return "%s - %s" % (d.strftime("%d/%m/%Y"), t)


def giro(prova=False):
    adesso = datetime.datetime.now(datetime.timezone.utc)
    ora_it = datetime.timezone(datetime.timedelta(hours=2 if adesso < datetime.datetime(2026, 10, 25, 1, 0, tzinfo=datetime.timezone.utc) else 1))
    frequentati, prossimi, assenti = {}, {}, {}          # id contatto -> {id evento: (data, titolo)}
    for e in eventi_corsi():
        p = e["properties"]
        inizio = datetime.datetime.fromisoformat(p["start_datetime"].replace("Z", "+00:00"))
        data = inizio.astimezone(ora_it).date()
        isc = [(i, m, st) for i, m, st in iscrizioni(e["id"]) if m and not NOSTRI.search(m)]
        if not isc:
            continue
        mappa = contatti_di([i for i, _, _ in isc])
        passato = inizio < adesso
        assenza_valida = inizio < adesso - datetime.timedelta(hours=18)     # il tempo che arrivino le presenze di Zoom
        for i, m, st in isc:
            cid = mappa.get(i)
            if not cid:
                continue
            voce = (data, titolo(p["name"]))
            if st == "Attended":
                if passato:
                    frequentati.setdefault(cid, {})[e["id"]] = voce
            elif not passato:
                prossimi.setdefault(cid, {})[e["id"]] = voce
            elif assenza_valida:
                assenti.setdefault(cid, {})[e["id"]] = voce
    universo = set(frequentati) | set(prossimi) | set(assenti)
    # contatti che hanno gia' un valore ma non risultano piu' (iscrizione annullata, corso passato): vanno svuotati
    for campo in ("corsi_iscritto_prossimi", "corsi_non_frequentati"):
        dopo = None
        for _ in range(20):
            r = C.hs("/crm/v3/objects/contacts/search", {"filterGroups": [{"filters": [{"propertyName": campo, "operator": "HAS_PROPERTY"}]}],
                     "properties": [campo], "limit": 100, **({"after": dopo} if dopo else {})}, "POST")
            universo |= {str(x["id"]) for x in r.get("results", [])}
            dopo = (r.get("paging") or {}).get("next", {}).get("after")
            if not dopo:
                break
    ids = sorted(universo)
    scritti = invariati = 0
    for i in range(0, len(ids), 100):
        blocco = ids[i:i + 100]
        att = C.hs("/crm/v3/objects/contacts/batch/read", {"properties": CAMPI_LETTI, "inputs": [{"id": x} for x in blocco]}, "POST").get("results", [])
        attuali = {str(x["id"]): x["properties"] for x in att}
        da_scrivere = []
        for cid in blocco:
            nuovo = {}
            fr = sorted(frequentati.get(cid, {}).values())
            if fr:
                nuovo.update({"corsi_frequentati": "\n".join(riga(d, t) for d, t in fr), "numero_corsi_frequentati": str(len(fr)),
                              "ultimo_corso_frequentato": fr[-1][1], "data_ultimo_corso": fr[-1][0].isoformat()})
            nuovo["corsi_iscritto_prossimi"] = "\n".join(riga(d, t) for d, t in sorted(prossimi.get(cid, {}).values()))
            nuovo["corsi_non_frequentati"] = "\n".join(riga(d, t) for d, t in sorted(assenti.get(cid, {}).values()))
            a = attuali.get(cid, {})
            diverso = {k: v for k, v in nuovo.items()
                       if (str(a.get(k) or "")[:10] if k == "data_ultimo_corso" else str(a.get(k) or "")) != v}
            if diverso:
                da_scrivere.append({"id": cid, "properties": diverso})
            else:
                invariati += 1
        if da_scrivere and not prova:
            r = C.hs("/crm/v3/objects/contacts/batch/update", {"inputs": da_scrivere}, "POST")
            if "_err" in r:
                print("  ERRORE scrittura:", r)
                continue
        scritti += len(da_scrivere)
    print("corsi: frequentati %d · iscritti ai prossimi %d · non presenti %d contatti · %s %d · invariati %d" % (
        len(frequentati), len(prossimi), len(assenti), "da scrivere" if prova else "scritti", scritti, invariati))


if __name__ == "__main__":
    if "--campi" in sys.argv:
        crea_campi()
    else:
        giro("--prova" in sys.argv)
