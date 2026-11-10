"""Controllo dei corsi di formazione (ordini -> invito -> nominati -> trattative). SOLA LETTURA.

Uso: python -I controllo_corsi.py [giorni]      (default: dal lancio del catalogo, 23/09/2026)
Legge i token da file nella cartella utente (mai stampati). Usa il repository C:\\Users\\pizzola\\corsi-preventivi.

Controlli (ogni problema e' una riga «!!!!» con il rimedio):
  1. ordine di corso chiuso vinto senza invito nel registro del motore
  2. richiesta Formazione ancora aperta per un ordine gia' arrivato (duplicati / ambigui)
  3. richieste Formazione doppie (stessa scuola, stessi corsi, tutte aperte)
  4. ordine senza nessun nominato da oltre 7 giorni
  5. ordine di corso senza scuola associata o con importo diverso dalle righe oltre lo sconto
"""
import datetime
import os
import re
import sys
from collections import defaultdict

REPO = os.path.dirname(os.path.abspath(__file__))   # nel cloud e sul PC: la cartella del repository
sys.path.insert(0, REPO)
sys.stdout.reconfigure(encoding="utf-8")
if not os.environ.get("HUBSPOT_TOKEN"):      # sul PC il token sta in un file; nel cloud arriva dal segreto del workflow
    os.environ["HUBSPOT_TOKEN"] = open(os.path.expanduser("~/.hs_pages_token.txt"), encoding="utf-8-sig").read().strip()
import corsi_ordini as O          # noqa: E402
import preventivi_corsi as C      # noqa: E402

FORM = "4128670920"
DA = 1790148300000 - 2 * 86400000             # 21/09/2026, due giorni prima del lancio
if len(sys.argv) > 1 and sys.argv[1].isdigit():
    import time
    DA = int((time.time() - int(sys.argv[1]) * 86400) * 1000)
problemi = []


def prob(t, rimedio):
    problemi.append((t, rimedio))
    print("  !!!!  %s\n        rimedio: %s" % (t, rimedio))


# --- ordini di corso dal lancio
pl = C.hs("/crm/v3/pipelines/deals")
stadio = {s["id"]: s["label"] for p in pl["results"] for s in p["stages"]}
righe, dopo = [], None
for _ in range(30):
    r = C.hs("/crm/v3/objects/line_items/search", {"filterGroups": [{"filters": [
        {"propertyName": "hs_sku", "operator": "CONTAINS_TOKEN", "value": "WBREL*"},
        {"propertyName": "createdate", "operator": "GTE", "value": str(DA)}]}],
        "properties": ["hs_sku", "quantity", "price"], "limit": 100, **({"after": dopo} if dopo else {})}, "POST")
    righe += r.get("results", [])
    dopo = (r.get("paging") or {}).get("next", {}).get("after")
    if not dopo:
        break
attuali = {x["base"] for x in O.palinsesto()}
righe = [x for x in righe if O.CORSO.match(x["properties"].get("hs_sku") or "") and O.CORSO.match(x["properties"]["hs_sku"]).group(1) in attuali]
coll = C.assoc_a_blocchi("line_items", "deals", [x["id"] for x in righe]) or {}
deals = C.leggi_a_blocchi("deals", sorted({d for v in coll.values() for d in v}), ["dealname", "dealstage", "pipeline", "createdate", "amount"]) or {}
ordini = {}
for x in righe:
    for d in coll.get(x["id"], []):
        p = deals.get(d)
        if not p or not re.search(r"-\s*ERP\s*$", p.get("dealname") or ""):
            continue
        o = ordini.setdefault(d, {"id": d, "num": re.sub(r"\s*-\s*ERP\s*$", "", p["dealname"]), "stadio": stadio.get(p["dealstage"], "?"),
                                  "creato": p["createdate"], "importo": float(p.get("amount") or 0), "righe": []})
        o["righe"].append((x["properties"]["hs_sku"], float(x["properties"].get("price") or 0) * float(x["properties"].get("quantity") or 1)))
vinti = [o for o in ordini.values() if "vinto" in o["stadio"].lower() and o["num"].startswith(str(datetime.date.today().year))]
az = C.assoc_a_blocchi("deals", "companies", [o["id"] for o in vinti]) or {}
for o in vinti:
    o["company"] = (az.get(o["id"]) or [None])[0]
print("CORSI: %d ordini chiusi vinti dal lancio (%d righe corso)" % (len(vinti), sum(len(o["righe"]) for o in vinti)))
print("       importo %.2f EUR" % sum(o["importo"] for o in vinti))

# 1. invito nel registro
gia = O.registro()
senza = [o for o in vinti if any("%s|%s|inviato" % (o["num"], s) not in gia for s, _ in o["righe"])]
if senza:
    prob("ordini senza invito completo nel registro: %s" % ", ".join(o["num"] for o in senza[:10]),
         "il giro ordini li prende da solo; se restano, `python corsi_ordini.py --prova` in corsi-preventivi e guardare il motivo (doppione, revocato, codice non a catalogo)")
else:
    print("  OK    1. invito partito per tutti gli ordini")

# 5. scuola e importo
nosc = [o["num"] for o in vinti if not o["company"]]
strani = []
for o in vinti:
    tot = sum(p for _, p in o["righe"])
    if tot and o["importo"] > tot + 0.5:
        strani.append("%s (importo %.2f > righe %.2f)" % (o["num"], o["importo"], tot))
if nosc:
    prob("ordini senza scuola associata: %s" % ", ".join(nosc), "associare la scuola alla trattativa ERP (per codice meccanografico)")
if strani:
    prob("importo ordine maggiore della somma delle righe: %s" % "; ".join(strani), "controllare le righe d'ordine nell'ERP/HubSpot")
if not nosc and not strani:
    print("  OK    5. tutti con scuola associata e importi coerenti con le righe (sconti esclusi)")

# 2-3. richieste Formazione
comp = sorted({o["company"] for o in vinti if o["company"]})
ass = C.assoc_a_blocchi("companies", "deals", comp) or {}
dl = C.leggi_a_blocchi("deals", sorted({d for v in ass.values() for d in v}), ["dealname", "dealstage", "pipeline", "createdate"]) or {}
fd = {d: p for d, p in dl.items() if p.get("pipeline") == FORM}
rl = C.assoc_a_blocchi("deals", "line_items", list(fd)) or {}
sk = C.leggi_a_blocchi("line_items", sorted({r for v in rl.values() for r in v}), ["hs_sku"]) or {}
cod = {d: {sk[r]["hs_sku"] for r in rl.get(d, []) if r in sk} for d in fd}
APERTI = ("Richiesta ricevuta", "Preventivo inviato", "Negoziazione")
aperte_ord = []
for o in vinti:
    oc = {s for s, _ in o["righe"]}
    cand = [d for d in ass.get(o["company"], []) if d in fd and cod[d] & oc and fd[d]["createdate"][:10] <= o["creato"][:10]]
    chiuse_ok = [d for d in cand if stadio.get(fd[d]["dealstage"]) == "Ordine confermato"]
    aperte = [d for d in cand if stadio.get(fd[d]["dealstage"]) in APERTI]
    if aperte:
        aperte_ord.append("%s: %s" % (o["num"], ", ".join("%s (%s)" % (d, fd[d]["dealname"][:28]) for d in aperte)))
if aperte_ord:
    prob("richieste Formazione ancora aperte per ordini gia' arrivati (%d): %s" % (len(aperte_ord), " | ".join(aperte_ord[:6])),
         "se sono doppioni della stessa persona: tenere quella del nominato in «Ordine confermato», le altre «Chiusa persa» (elenco da mostrare ad Andrea prima)")
else:
    print("  OK    2. nessuna richiesta aperta per ordini gia' arrivati")
tutte, dopo = [], None
for _ in range(10):
    r = C.hs("/crm/v3/objects/deals/search", {"filterGroups": [{"filters": [
        {"propertyName": "pipeline", "operator": "EQ", "value": FORM}, {"propertyName": "dealstage", "operator": "IN", "values": ["6059680979", "6059680980", "6059680981"]}]}],
        "properties": ["dealname", "createdate"], "limit": 100, **({"after": dopo} if dopo else {})}, "POST")
    tutte += r["results"]
    dopo = (r.get("paging") or {}).get("next", {}).get("after")
    if not dopo:
        break
a2 = C.assoc_a_blocchi("deals", "companies", [d["id"] for d in tutte]) or {}
l2 = C.assoc_a_blocchi("deals", "line_items", [d["id"] for d in tutte]) or {}
s2 = C.leggi_a_blocchi("line_items", sorted({r for v in l2.values() for r in v}), ["hs_sku"]) or {}
gruppi = defaultdict(list)
for d in tutte:
    c = frozenset(s2[r]["hs_sku"] for r in l2.get(d["id"], []) if r in s2)
    gruppi[((a2.get(d["id"]) or ["?"])[0], c)].append(d)
doppie = [l for k, l in gruppi.items() if len(l) > 1 and k[0] != "?"]      # 7/11/2026: senza scuola associata non si puo' dire «stessa scuola» (falso allarme Assisi/Fiorentino)
senza_scuola = sum(1 for k, l in gruppi.items() if k[0] == "?" for _ in l)
if senza_scuola:
    print("  NOTA  %d richieste aperte senza scuola associata: non confrontabili per doppioni" % senza_scuola)
if doppie:
    prob("%d gruppi di richieste aperte doppie (stessa scuola, stessi corsi): %s" % (len(doppie), " | ".join(", ".join(d["id"] for d in l) for l in doppie[:6])),
         "tenere una richiesta per gruppo, chiudere le altre «Chiusa persa» dopo l'ok di Andrea")
else:
    print("  OK    3. nessuna richiesta aperta doppia (%d richieste aperte)" % len(tutte))

# 4. nominati
regs, dopo = [], None
for _ in range(60):
    r = C.hs("/crm/v3/objects/%s/search" % O.REG, {"filterGroups": [{"filters": [{"propertyName": "ordine_corso", "operator": "HAS_PROPERTY"}]}],
        "properties": ["status", "ordine_corso"], "limit": 100, **({"after": dopo} if dopo else {})}, "POST")
    regs += r["results"]
    dopo = (r.get("paging") or {}).get("next", {}).get("after")
    if not dopo:
        break
nom = defaultdict(int)
for x in regs:
    p = x["properties"]
    if p.get("status") != "Canceled" and not (p.get("ordine_corso") or "").startswith("PROVA"):
        nom[p["ordine_corso"]] += 1
limite = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
revocati = C.ordini_revocati()
fermi = [o["num"] for o in vinti if o["creato"][:10] <= limite and o["num"] not in revocati and not any(k.startswith(o["num"]) for k in nom)]
if fermi:
    prob("ordini da oltre 7 giorni senza nessun nominato: %s" % ", ".join(fermi),
         "il sollecito e' automatico solo per corsi imminenti: telefonare/scrivere alla scuola o verificare che abbia ricevuto il link")
else:
    print("  OK    4. ogni ordine con piu' di 7 giorni ha almeno un nominato")
print("\nDA SISTEMARE: %d" % len(problemi))
for t, _ in problemi:
    print("  -", t[:150])
print("(Sola lettura: nessuna modifica fatta.)")
