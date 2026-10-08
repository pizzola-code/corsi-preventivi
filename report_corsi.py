# -*- coding: utf-8 -*-
"""Panoramica quotidiana delle richieste di preventivo dal catalogo corsi.

Andrea 27/9/2026: "sì, tienila aggiornata ogni giorno". Ogni mattina una mail a
pizzola@spaggiari.eu con:
  · le richieste nuove delle ultime 24 ore;
  · il totale dal lancio: richieste, scuole, importo, per fase, per agente, per fonte;
  · le scuole con piu' di una richiesta (i doppioni da far chiudere all'agente);
  · i numeri delle DEM di lancio corsi (inviate, aperture, clic).

Fonte unica: la pipeline Formazione. Ogni richiesta del modulo diventa una trattativa
«Corsi N - Scuola» creata da preventivi_corsi.py; le prove sono gia' state eliminate.

Uso:  python report_corsi.py            manda la mail
      python report_corsi.py --prova    stampa il riepilogo, senza mandare nulla
"""
import collections
import datetime
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from email.message import EmailMessage

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
TOK = os.environ["HUBSPOT_TOKEN"]
PIPELINE = "4128670920"
# destinatari: variabile del repository CORSI_REPORT_A (stessa del riepilogo della sera); il repository e' pubblico, niente indirizzi nel codice
A = ", ".join(x.strip() for x in os.environ.get("CORSI_REPORT_A", "").split(",") if x.strip()) or "pizzola@spaggiari.eu"
MITTENTE = "no_reply@spaggiari.eu"
PROVA = "--prova" in sys.argv
PETROLIO = "#06484b"


def hs(p, c=None, m="GET"):
    for t in range(4):
        try:
            r = urllib.request.Request("https://api.hubapi.com" + p,
                                       data=json.dumps(c).encode() if c is not None else None,
                                       method=m, headers={"Authorization": "Bearer " + TOK,
                                                          "Content-Type": "application/json"})
            x = urllib.request.urlopen(r, timeout=120).read()
            return json.loads(x) if x else {}
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and t < 3:
                time.sleep(3 + 3 * t)
                continue
            if e.code == 404:
                return {}
            raise


_owner = {}


def chi(o):
    if not o:
        return "nessuno"
    if o not in _owner:
        x = hs("/crm/v3/owners/%s?idProperty=id" % o) or hs(
            "/crm/v3/owners/%s?idProperty=id&archived=true" % o)
        _owner[o] = ("%s %s" % (x.get("firstName") or "", x.get("lastName") or "")).strip().title() or o
    return _owner[o]


def fonte(cp):
    s, d = cp.get("hs_latest_source") or "", cp.get("hs_latest_source_data_2") or ""
    url = cp.get("hs_analytics_last_url") or ""
    if "utm_source=piattaforme" in url:
        return "banner piattaforme"
    if s == "EMAIL_MARKETING":
        return "DEM di lancio" if d in LANCI_CAMPAGNE else "altra email Spaggiari"
    return {"DIRECT_TRAFFIC": "accesso diretto", "REFERRALS": "altro sito",
            "ORGANIC_SEARCH": "Google", "PAID_SEARCH": "Google Ads",
            "SOCIAL_MEDIA": "social"}.get(s, s.lower() or "sconosciuta")


def euro(v):
    return ("{:,.0f}".format(v)).replace(",", ".") + " €"


# --- DEM di lancio corsi: numeri e id di campagna per riconoscere la fonte --------------
LANCI, LANCI_CAMPAGNE = [], set()
dopo = None
while True:
    r = hs("/marketing/v3/emails?limit=100&includeStats=true" + ("&after=" + dopo if dopo else ""))
    for e in r.get("results", []):
        if (e.get("name") or "").startswith("CORSI 2026 - Lancio") and e.get("state") == "PUBLISHED":
            k = (e.get("stats") or {}).get("counters", {})
            LANCI.append((e.get("publishDate", "")[:10], k.get("sent", 0), k.get("open", 0),
                          k.get("click", 0)))
            LANCI_CAMPAGNE.update(str(x) for x in (e.get("allEmailCampaignIds") or []))
    dopo = (r.get("paging") or {}).get("next", {}).get("after")
    if not dopo:
        break
LANCI.sort()

# --- le richieste: trattative della pipeline Formazione ----------------------------------
stadi = {s["id"]: s["label"] for s in hs("/crm/v3/pipelines/deals/%s" % PIPELINE)["stages"]}
righe, dopo = [], None
while True:
    corpo = {"filterGroups": [{"filters": [{"propertyName": "pipeline", "operator": "EQ",
                                             "value": PIPELINE}]}],
             "properties": ["dealname", "hubspot_owner_id", "dealstage", "amount", "createdate"],
             "sorts": [{"propertyName": "createdate", "direction": "ASCENDING"}], "limit": 100}
    if dopo:
        corpo["after"] = dopo
    r = hs("/crm/v3/objects/deals/search", corpo, "POST")
    for d in r.get("results", []):
        p = d["properties"]
        co = [x["toObjectId"] for x in hs("/crm/v4/objects/deals/%s/associations/companies"
                                          % d["id"]).get("results", [])]
        scuola = (hs("/crm/v3/objects/companies/%s?properties=name" % co[0]).get("properties") or {}
                  ).get("name") if co else None
        ct = [x["toObjectId"] for x in hs("/crm/v4/objects/deals/%s/associations/contacts"
                                          % d["id"]).get("results", [])]
        cp = (hs("/crm/v3/objects/contacts/%s?properties=ruolo,hs_latest_source,"
                 "hs_latest_source_data_2,hs_analytics_last_url" % ct[0]).get("properties") or {}
              ) if ct else {}
        nome = p.get("dealname") or ""
        righe.append({
            "id": d["id"], "quando": p["createdate"], "nome": nome,
            "scuola": scuola or nome.split(" - ", 1)[-1], "chiave": co[0] if co else nome,
            "importo": float(p.get("amount") or 0), "fase": stadi.get(p.get("dealstage"), "?"),
            "agente": chi(p.get("hubspot_owner_id")), "ruolo": cp.get("ruolo") or "-",
            "fonte": fonte(cp)})
    dopo = (r.get("paging") or {}).get("next", {}).get("after")
    if not dopo:
        break

adesso = datetime.datetime.now(datetime.timezone.utc)
ieri = (adesso - datetime.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M")
nuove = [x for x in righe if x["quando"] >= ieri]
per_scuola = collections.defaultdict(list)
for x in righe:
    per_scuola[x["chiave"]].append(x)
doppie = [v for v in per_scuola.values() if len(v) > 1]
tot = sum(x["importo"] for x in righe)


def conta(campo):
    c = collections.Counter()
    s = collections.defaultdict(float)
    for x in righe:
        c[x[campo]] += 1
        s[x[campo]] += x["importo"]
    return [(k, c[k], s[k]) for k, _ in c.most_common()]


def giorno(iso):
    d = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")) + datetime.timedelta(hours=2)
    return d.strftime("%d/%m %H:%M")


# --- la mail ------------------------------------------------------------------------------
TD = 'style="padding:6px 10px;border-bottom:1px solid #e4eaee;font:14px Arial,sans-serif;color:#33403f"'
TH = 'style="padding:6px 10px;border-bottom:2px solid %s;font:700 12px Arial,sans-serif;color:%s;text-align:left"' % (PETROLIO, PETROLIO)


def tabella(intest, dati):
    h = "<table style=\"border-collapse:collapse;width:100%;margin:6px 0 18px\"><tr>"
    h += "".join("<th %s>%s</th>" % (TH, x) for x in intest) + "</tr>"
    for r_ in dati:
        h += "<tr>" + "".join("<td %s>%s</td>" % (TD, x) for x in r_) + "</tr>"
    return h + "</table>"


def titolo(t):
    return '<p style="margin:22px 0 4px;font:700 16px Arial,sans-serif;color:%s">%s</p>' % (PETROLIO, t)


h = ('<div style="max-width:760px;font:15px/1.5 Arial,sans-serif;color:#33403f">'
     '<p style="font:700 20px Arial,sans-serif;color:%s;margin:0 0 4px">Corsi di formazione: '
     'richieste di preventivo</p><p style="margin:0 0 14px;color:#6d817f">Situazione al %s</p>'
     % (PETROLIO, (adesso + datetime.timedelta(hours=2)).strftime("%d/%m/%Y %H:%M")))
h += ('<p style="margin:0 0 6px"><b>%d richieste</b> da <b>%d scuole</b> per <b>%s</b> di preventivi. '
      '<b>%d nuove</b> nelle ultime 24 ore.</p>' % (len(righe), len(per_scuola), euro(tot), len(nuove)))

h += titolo("Nuove nelle ultime 24 ore")
h += (tabella(["Quando", "Scuola", "Importo", "Chi scrive", "Agente", "Fonte"],
              [[giorno(x["quando"]), x["scuola"][:45], euro(x["importo"]), x["ruolo"], x["agente"],
                x["fonte"]] for x in nuove])
      if nuove else "<p>Nessuna richiesta nuova.</p>")
h += titolo("Per fase")
h += tabella(["Fase", "Richieste", "Importo"], [[k, n, euro(s)] for k, n, s in conta("fase")])
h += titolo("Per agente")
h += tabella(["Agente", "Richieste", "Importo"], [[k, n, euro(s)] for k, n, s in conta("agente")])
h += titolo("Da dove arrivano")
h += tabella(["Fonte", "Richieste", "Importo"], [[k, n, euro(s)] for k, n, s in conta("fonte")])
if LANCI:
    h += titolo("DEM di lancio corsi")
    h += tabella(["Invio", "Inviate", "Aperture", "Clic"],
                 [[d, "{:,}".format(s).replace(",", "."), o, c] for d, s, o, c in LANCI])
if doppie:
    h += titolo("Scuole con piu' di una richiesta: l'agente tenga valida l'ultima")
    h += tabella(["Scuola", "Richieste", "Agente"],
                 [[v[0]["scuola"][:45], " · ".join("%s %s" % (giorno(x["quando"]), euro(x["importo"]))
                                                  for x in v), v[-1]["agente"]] for v in doppie])
h += titolo("Tutte le richieste")
h += tabella(["Quando", "Scuola", "Importo", "Fase", "Agente", "Fonte"],
             [[giorno(x["quando"]), x["scuola"][:45], euro(x["importo"]), x["fase"], x["agente"],
               x["fonte"]] for x in reversed(righe)])
h += ('<p style="color:#6d817f;font-size:13px">In HubSpot: CRM → Trattative → pipeline '
      '«Formazione».</p></div>')

oggetto = "Corsi: %d richieste, %s (%d nuove)" % (len(righe), euro(tot), len(nuove))
print(oggetto)
for x in nuove:
    print("  nuova:", giorno(x["quando"]), x["scuola"], euro(x["importo"]), "->", x["agente"])
if PROVA:
    open("report_corsi_prova.html", "w", encoding="utf-8").write(h)
    print("prova: scritto report_corsi_prova.html, nessuna mail")
    sys.exit(0)

m = EmailMessage()
m["From"] = "Spaggiari <%s>" % MITTENTE
m["To"] = A
m["Subject"] = oggetto
m.set_content("Il riepilogo e' in HTML.")
m.add_alternative(h, subtype="html")
s = smtplib.SMTP("smtp.hubapi.com", 587, timeout=60)
s.starttls(context=ssl.create_default_context())
s.login(os.environ["SMTP_CORSI_USER"], os.environ["SMTP_CORSI_PASS"])
s.send_message(m, to_addrs=[x.strip() for x in A.split(",")])
s.quit()
print("mandata a %d destinatari" % len(A.split(",")))
