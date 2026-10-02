# -*- coding: utf-8 -*-
"""Elenco serale dei corsi in diretta: preventivati, venduti, partecipanti inseriti (per scuola e per corso).

Andrea, 2/10/2026: «ogni sera elenco con corsi preventivati, corsi venduti e partecipanti inseriti (numero per
scuola per corso)»; i tassi di partecipazione si aggiungono dopo.

Da dove vengono i numeri (tutti da HubSpot, in sola lettura):
  · righe d'ordine con codice WBRELE<corso>-1|3|I (catalogo di oggi, vedi corsi_palinsesto.json), collegate
    all'affare e alla scuola. L'affare e' «venduto» in uno stadio «Chiuso Vinto» / «Ordine confermato»,
    «preventivato» se ha un preventivo in corso (non perso, non ancora richiesta/potenziale);
  · partecipanti = iscrizioni attive agli eventi dei corsi (`ordine_corso` valorizzato), contate per ordine.
Se una scuola ha lo stesso corso sia preventivato sia venduto conta come venduto.

  python report_corsi_sera.py            manda la mail (destinatari: variabile CORSI_REPORT_A, di default Pizzola)
  python report_corsi_sera.py --prova    stampa e non manda nulla
"""
import collections
import datetime
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage

QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, QUI)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import preventivi_corsi as C                                  # noqa: E402

REG, EV = "2-143900355", "2-143900361"
CORSO = re.compile(r"^WBRELE?([A-Z0-9]+)-(1|3|I)$")
PROVA = "--prova" in sys.argv
NON_PREVENTIVATI = ("potenziali", "richiesta", "da rinnovare", "in lavorazione")   # nessun preventivo ancora
ORDINE_POSTI = {"1": 1, "3": 3, "I": 99}
GIORNI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
MESI = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]


def pulito(nome):
    n = re.sub(r"^[A-Z0-9]{8,12}\s*-\s*", "", nome or "")
    n = re.sub(r"SUPERIORE(?=[A-Z])", "SUPERIORE ", n)
    return re.sub(r"(^|[\s'.\-])([a-zàèéìòù])", lambda m: m.group(1) + m.group(2).upper(), n.lower())


def a_blocchi(lista, n=100):
    for i in range(0, len(lista), n):
        yield lista[i:i + n]


def righe_corso():
    palinsesto = {x["base"]: x for x in json.load(open(os.path.join(QUI, "corsi_palinsesto.json"), encoding="utf-8"))}
    out, dopo = [], None
    for _ in range(30):
        r = C.hs("/crm/v3/objects/line_items/search", {"filterGroups": [{"filters": [
            {"propertyName": "hs_sku", "operator": "CONTAINS_TOKEN", "value": "WBRELE*"},
            {"propertyName": "createdate", "operator": "GTE", "value": str(int((time.time() - 150 * 86400) * 1000))}]}],
            "properties": ["hs_sku", "quantity", "price"], "limit": 100, **({"after": dopo} if dopo else {})}, "POST")
        for x in r.get("results", []):
            m = CORSO.match(x["properties"].get("hs_sku") or "")
            if m and m.group(1) in palinsesto:
                out.append({"id": x["id"], "base": m.group(1), "tier": m.group(2)})
        dopo = (r.get("paging") or {}).get("next", {}).get("after")
        if not dopo:
            break
    return out, palinsesto


def associati(da, a, ids):
    """{id: [id collegati]} con le associazioni v4 in blocco"""
    res = {}
    for blocco in a_blocchi(sorted(set(ids))):
        r = C.hs("/crm/v4/associations/%s/%s/batch/read" % (da, a), {"inputs": [{"id": str(i)} for i in blocco]}, "POST")
        for x in r.get("results", []):
            res[str(x["from"]["id"])] = [str(t["toObjectId"]) for t in x.get("to", [])]
    return res


def leggi(oggetto, ids, props):
    res = {}
    for blocco in a_blocchi(sorted(set(ids))):
        r = C.hs("/crm/v3/objects/%s/batch/read" % oggetto, {"properties": props, "inputs": [{"id": str(i)} for i in blocco]}, "POST")
        for x in r.get("results", []):
            res[str(x["id"])] = x["properties"]
    return res


def raccogli():
    righe, palinsesto = righe_corso()
    stadi = {s["id"]: s["label"].lower() for p in C.hs("/crm/v3/pipelines/deals")["results"] for s in p["stages"]}
    r2d = associati("line_items", "deals", [x["id"] for x in righe])
    deals = leggi("deals", [d for v in r2d.values() for d in v], ["dealname", "dealstage", "createdate", "amount"])
    d2c = associati("deals", "companies", list(deals))
    aziende = leggi("companies", [c for v in d2c.values() for c in v], ["name", "codice_cliente"])

    voci = {}      # (scuola, base) -> dati
    ordini = {}    # numero ordine -> (scuola, base)
    for x in righe:
        for d in r2d.get(x["id"], []):
            deal, comp = deals.get(d), (d2c.get(d) or [None])[0]
            if not deal or not comp:
                continue
            st = stadi.get(deal.get("dealstage"), "")
            if "perso" in st:
                continue
            venduto = "vinto" in st or "confermato" in st
            if not venduto and any(st.startswith(n) for n in NON_PREVENTIVATI):
                continue
            chiave = (comp, x["base"])
            v = voci.setdefault(chiave, {"stato": "preventivato", "tier": x["tier"], "ordini": set(), "fase": st, "importo": deal.get("amount")})
            if venduto and v["stato"] != "venduto":
                v.update({"stato": "venduto", "tier": x["tier"], "fase": st})
            if ORDINE_POSTI[x["tier"]] > ORDINE_POSTI[v["tier"]] and (venduto or v["stato"] != "venduto"):
                v["tier"] = x["tier"]
            if venduto:
                num = re.sub(r"\s*-\s*ERP\s*$", "", deal.get("dealname") or "")
                v["ordini"].add(num)
                ordini[num] = chiave
    # partecipanti: iscrizioni attive con ordine_corso
    parte = collections.Counter()
    dopo = None
    reg = []
    for _ in range(40):
        r = C.hs("/crm/v3/objects/%s/search" % REG, {"filterGroups": [{"filters": [
            {"propertyName": "ordine_corso", "operator": "HAS_PROPERTY"}, {"propertyName": "status", "operator": "NEQ", "value": "Canceled"}]}],
            "properties": ["ordine_corso", "event_id"], "limit": 100, **({"after": dopo} if dopo else {})}, "POST")
        reg += r.get("results", [])
        dopo = (r.get("paging") or {}).get("next", {}).get("after")
        if not dopo:
            break
    eventi = leggi("2-143900361", [x["properties"].get("event_id") for x in reg if x["properties"].get("event_id")], ["external_id"])
    for x in reg:
        p = x["properties"]
        ext = (eventi.get(p.get("event_id")) or {}).get("external_id") or ""
        m = re.match(r"^CF-([A-Z0-9]+)-\d{4}$", ext.upper())
        chiave = ordini.get(p.get("ordine_corso"))
        if chiave and m and chiave[1] == m.group(1):
            parte[chiave] += 1
    return voci, parte, aziende, palinsesto


def quando(voce):
    d = datetime.date.fromisoformat(voce["data"])
    return "%s %d %s" % (GIORNI[d.weekday()], d.day, MESI[d.month - 1])


def costruisci():
    voci, parte, aziende, pal = raccogli()
    oggi = datetime.date.today()
    per_corso = collections.defaultdict(lambda: {"prev": 0, "ven": 0, "posti": 0, "ill": 0, "ins": 0})
    for (comp, base), v in voci.items():
        c = per_corso[base]
        if v["stato"] == "venduto":
            c["ven"] += 1
            if v["tier"] == "I":
                c["ill"] += 1
            else:
                c["posti"] += int(v["tier"])
            c["ins"] += parte.get((comp, base), 0)
        else:
            c["prev"] += 1
    return voci, parte, aziende, pal, per_corso, oggi


def tabella(intest, righe, larghezze=None):
    stile_th = "text-align:left;padding:7px 10px;background:#0E2A4D;color:#fff;font-size:13px;white-space:nowrap"
    stile_td = "padding:7px 10px;border-bottom:1px solid #e3e8ee;font-size:14px;vertical-align:top"
    h = "<table style='border-collapse:collapse;width:100%%;margin:6px 0 22px 0'><tr>%s</tr>" % "".join("<th style='%s'>%s</th>" % (stile_th, i) for i in intest)
    for r in righe:
        h += "<tr>%s</tr>" % "".join("<td style='%s'>%s</td>" % (stile_td, c) for c in r)
    return h + "</table>"


def html_report(voci, parte, aziende, pal, per_corso, oggi):
    def nome(comp):
        grezzo = (aziende.get(comp) or {}).get("name") or ""
        cod = re.match(r"^([A-Z]{2}[A-Z0-9]{8})\s*-", grezzo)
        return html.escape(((cod.group(1) + " · ") if cod else "") + pulito(grezzo))
    def posti(v):
        return "illimitati" if v["tier"] == "I" else "%s" % v["tier"]
    # 1. per corso
    righe1 = []
    for base in sorted(per_corso, key=lambda b: pal[b]["data"]):
        c = per_corso[base]
        posti_tot = " + ".join(x for x in (("%d" % c["posti"]) if c["posti"] else "", ("%d illim." % c["ill"]) if c["ill"] else "") if x) or "-"
        righe1.append([html.escape(pal[base]["titolo"][:70]), quando(pal[base]), c["prev"], c["ven"], posti_tot if c["ven"] else "-", "<b>%d</b>" % c["ins"] if c["ven"] else "-"])
    # 2. venduti per scuola
    ven = sorted([(k, v) for k, v in voci.items() if v["stato"] == "venduto"], key=lambda kv: (pal[kv[0][1]]["data"], nome(kv[0][0])))
    righe2 = []
    for (comp, base), v in ven:
        n = parte.get((comp, base), 0)
        giorni = (datetime.date.fromisoformat(pal[base]["data"]) - oggi).days
        avviso = " <span style='color:#B3261E'>⚠ nessun nominativo</span>" if n == 0 and 0 <= giorni <= 7 else ""
        righe2.append([html.escape(pal[base]["titolo"][:55]), quando(pal[base]), nome(comp), posti(v), "<b>%d</b>%s" % (n, avviso)])
    # 3. preventivati
    prev = sorted([(k, v) for k, v in voci.items() if v["stato"] == "preventivato"], key=lambda kv: (pal[kv[0][1]]["data"], nome(kv[0][0])))
    righe3 = [[html.escape(pal[b]["titolo"][:55]), quando(pal[b]), nome(c), posti(v), html.escape(v["fase"].capitalize())] for (c, b), v in prev]
    tot_ven = sum(1 for _, v in ven), sum(parte.values())
    corpo = ("<div style='font-family:Arial,sans-serif;color:#0E2A4D;max-width:860px'>"
             "<h2 style='margin:0 0 4px 0'>Corsi in diretta · situazione della sera</h2>"
             "<div style='color:#51606E;font-size:14px;margin:0 0 18px 0'>%d corsi venduti a scuole, %d partecipanti inseriti · %d preventivi in corso · aggiornato al %s</div>"
             % (tot_ven[0], tot_ven[1], len(prev), oggi.strftime("%d/%m/%Y")))
    corpo += "<h3 style='margin:14px 0 2px 0'>Per corso</h3>" + tabella(["Corso", "Data", "Preventivati", "Venduti", "Posti venduti", "Inseriti"], righe1)
    corpo += "<h3 style='margin:14px 0 2px 0'>Corsi venduti: scuole e partecipanti inseriti</h3>" + (
        tabella(["Corso", "Data", "Scuola", "Posti", "Inseriti"], righe2) if righe2 else "<p>Nessun corso venduto per ora.</p>")
    corpo += "<h3 style='margin:14px 0 2px 0'>Corsi preventivati</h3>" + (
        tabella(["Corso", "Data", "Scuola", "Posti", "Fase"], righe3) if righe3 else "<p>Nessun preventivo in corso.</p>")
    corpo += "<p style='color:#51606E;font-size:12px'>Fonte: HubSpot (righe d'ordine dei corsi, affari ERP e pipeline Formazione, iscrizioni agli eventi). I tassi di partecipazione si aggiungono dopo le dirette.</p></div>"
    return corpo, (len(prev), tot_ven)


def invia(corpo):
    a = [x for x in os.environ.get("CORSI_REPORT_A", "pizzola@spaggiari.eu").replace(" ", "").split(",") if x]
    utente, pw = os.environ.get("SMTP_CORSI_USER"), os.environ.get("SMTP_CORSI_PASS")
    if not (utente and pw):
        raise RuntimeError("credenziali SMTP assenti")
    m = EmailMessage()
    m["From"] = "Spaggiari <%s>" % C.MITTENTE
    m["To"] = ", ".join(a)
    m["Subject"] = "Corsi in diretta · situazione del %s" % datetime.date.today().strftime("%d/%m")
    m.set_content("Elenco serale dei corsi: serve un lettore di posta HTML.")
    m.add_alternative(corpo, subtype="html")
    s = smtplib.SMTP(C.SMTP_HOST, C.SMTP_PORTA, timeout=60)
    s.starttls(context=ssl.create_default_context())
    s.login(utente, pw)
    s.send_message(m)
    s.quit()


def main():
    voci, parte, aziende, pal, per_corso, oggi = costruisci()
    corpo, riepilogo = html_report(voci, parte, aziende, pal, per_corso, oggi)
    print("preventivati %d · venduti %d · partecipanti inseriti %d" % (riepilogo[0], riepilogo[1][0], riepilogo[1][1]))
    if PROVA:
        testo = re.sub(r"</tr>", "\n", corpo)
        testo = re.sub(r"</t[dh]>", " | ", testo)
        print(re.sub(r"<[^>]+>", "", html.unescape(testo)))
        return
    invia(corpo)
    print("inviato")


if __name__ == "__main__":
    main()
