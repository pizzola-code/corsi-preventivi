# -*- coding: utf-8 -*-
"""Chiusura automatica delle trattative della pipeline «Formazione» (Andrea, 5 ott 2026).

La richiesta di preventivo dei corsi resta in pipeline Formazione. Quando la scuola ordina, l'ERP crea in HubSpot
l'affare «<numero> - ERP» con le stesse righe (codici WBREL…): da quel momento
  · ordine in «Chiuso Vinto»  -> la trattativa Formazione passa a «Ordine confermato» (chiusa vinta);
  · ordine in «Chiuso Perso»  -> la trattativa Formazione passa a «Chiusa persa».
Abbinamento: stessa scuola, ordine creato dopo la richiesta, almeno un codice corso in comune. Se un ordine potrebbe
appartenere a piu' trattative con lo stesso numero di codici in comune, non si tocca niente e si segnala.

  python chiusura_trattative.py --prova     elenca cosa farebbe
  python chiusura_trattative.py             applica
"""
import datetime
import os
import re
import sys

QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, QUI)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import preventivi_corsi as C                                  # noqa: E402

PIPELINE = "4128670920"
STADI_APERTI = ["6059680979", "6059680980", "6059680981"]
VINTA, PERSA = "6059680982", "6059680983"
ERP = re.compile(r"-\s*ERP\s*$", re.I)


def stadi_erp():
    pl = C.hs("/crm/v3/pipelines/deals")
    out = {}
    for p in pl["results"]:
        if p["id"] == PIPELINE:
            continue
        for s in p["stages"]:
            et = (s["label"] or "").lower()
            out[s["id"]] = "vinto" if "vinto" in et else ("perso" if "perso" in et else "aperto")
    return out


_CODICI = {}


def precarica_codici(deal_ids):
    """Codici corso di molte trattative in due passaggi a blocchi (29/10/2026) invece di due chiamate per trattativa."""
    ids = [str(i) for i in deal_ids if str(i) not in _CODICI]
    if not ids:
        return
    righe = C.assoc_a_blocchi("deals", "line_items", ids)
    sku = C.leggi_a_blocchi("line_items", sorted({r for v in (righe or {}).values() for r in v}), ["hs_sku"]) if righe is not None else None
    if righe is None or sku is None:
        return                                          # HubSpot rifiuta il blocco: codici() ripiega sul metodo per trattativa
    for i in ids:
        _CODICI[i] = {sku[r].get("hs_sku") for r in righe.get(i, []) if r in sku and (sku[r].get("hs_sku") or "").startswith("WBREL")}


def codici(deal_id):
    if str(deal_id) in _CODICI:
        return _CODICI[str(deal_id)]
    li = C.hs("/crm/v4/objects/deals/%s/associations/line_items" % deal_id).get("results", [])
    if not li:
        return set()
    r = C.hs("/crm/v3/objects/line_items/batch/read", {"properties": ["hs_sku"], "inputs": [{"id": str(x["toObjectId"])} for x in li]}, "POST")
    return {x["properties"].get("hs_sku") for x in r.get("results", []) if (x["properties"].get("hs_sku") or "").startswith("WBREL")}


def trattative_aperte():
    out, dopo = [], None
    for _ in range(20):
        r = C.hs("/crm/v3/objects/deals/search", {"filterGroups": [{"filters": [
            {"propertyName": "pipeline", "operator": "EQ", "value": PIPELINE},
            {"propertyName": "dealstage", "operator": "IN", "values": STADI_APERTI}]}],
            "properties": ["dealname", "createdate", "dealstage"], "limit": 100, **({"after": dopo} if dopo else {})}, "POST")
        out += r.get("results", [])
        dopo = (r.get("paging") or {}).get("next", {}).get("after")
        if not dopo:
            break
    return out


def main(prova=None):
    if prova is None:
        prova = "--prova" in sys.argv
    stato = stadi_erp()
    aperte = trattative_aperte()
    print("trattative Formazione ancora aperte: %d%s" % (len(aperte), "   [PROVA]" if prova else ""))
    # per ogni ordine ERP candidato: a quali trattative potrebbe appartenere
    candidati = {}                                     # id ordine -> {"nome", "stato", "trattative": [(id, in_comune)]}
    cod_f = {}
    precarica_codici([f["id"] for f in aperte])
    coppie = []                                        # (trattativa, ordine ERP chiuso) da confrontare
    for f in aperte:
        cod_f[f["id"]] = codici(f["id"])
        if not cod_f[f["id"]]:
            continue
        az = C.hs("/crm/v4/objects/deals/%s/associations/companies" % f["id"]).get("results", [])
        if not az:
            continue
        r = C.hs("/crm/v3/objects/deals/search", {"filterGroups": [{"filters": [
            {"propertyName": "associations.company", "operator": "EQ", "value": str(az[0]["toObjectId"])},
            {"propertyName": "createdate", "operator": "GTE", "value": str(int(datetime.datetime.fromisoformat(f["properties"]["createdate"].replace("Z", "+00:00")).timestamp() * 1000) - 2 * 86400000)},
            {"propertyName": "pipeline", "operator": "NEQ", "value": PIPELINE}]}],
            "properties": ["dealname", "dealstage", "createdate"], "limit": 50}, "POST").get("results", [])
        for e in r:
            if not ERP.search(e["properties"].get("dealname") or ""):
                continue
            esito = stato.get(e["properties"].get("dealstage"), "aperto")
            if esito == "aperto":
                continue
            coppie.append((f, e, esito))
    precarica_codici([e["id"] for _, e, _ in coppie])
    for f, e, esito in coppie:
        comuni = cod_f[f["id"]] & codici(e["id"])
        if not comuni:
            continue
        c = candidati.setdefault(e["id"], {"nome": e["properties"]["dealname"], "esito": esito, "trattative": []})
        c["trattative"].append((f["id"], len(comuni), f["properties"]["dealname"]))
    mosse = {}                                         # trattativa -> (nuovo stadio, ordine)
    for eid, c in candidati.items():
        c["trattative"].sort(key=lambda t: -t[1])
        migliore = c["trattative"][0]
        if len(c["trattative"]) > 1 and c["trattative"][1][1] == migliore[1]:
            print("  AMBIGUO: %s (%s) potrebbe essere di %s: non tocco niente" % (c["nome"], c["esito"], ", ".join(t[2] for t in c["trattative"])))
            if not prova:
                try:
                    import corsi_ordini as O
                    k = "ambiguo|%s" % c["nome"]
                    if k not in O.registro():
                        O.avviso_interno("Trattative Formazione da chiudere a mano: %s" % c["nome"], [
                            "L'ordine <b>%s</b> (%s) corrisponde a piu' richieste con gli stessi corsi: %s." % (c["nome"], c["esito"], "; ".join(t[2] for t in c["trattative"])),
                            "Chiudi a mano quella giusta (le altre vanno in «Chiusa persa» o restano come sono): il sistema non sceglie da solo."])
                        O.segna(k)
                except Exception as err:
                    print("  avviso non accodato:", err)
            continue
        nuovo = VINTA if c["esito"] == "vinto" else PERSA
        precedente = mosse.get(migliore[0])
        if precedente and precedente[0] == VINTA:        # una vinta vale piu' di una persa
            continue
        mosse[migliore[0]] = (nuovo, c["nome"])
    fatte = 0
    for tid, (stadio, ordine) in mosse.items():
        nome = next(f["properties"]["dealname"] for f in aperte if f["id"] == tid)
        print("  %s -> %s (ordine %s)" % (nome, "ORDINE CONFERMATO" if stadio == VINTA else "CHIUSA PERSA", ordine))
        if not prova:
            C.hs("/crm/v3/objects/deals/%s" % tid, {"properties": {"dealstage": stadio}}, "PATCH")
            fatte += 1
    print("trattative da chiudere: %d · chiuse ora: %d" % (len(mosse), fatte))


if __name__ == "__main__":
    main()
