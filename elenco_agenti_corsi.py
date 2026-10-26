# -*- coding: utf-8 -*-
"""Elenco settimanale per gli agenti: le loro trattative sui corsi con il preventivo aperto (piano iscrizioni 26/10/2026, D5).

Ogni lunedi' ogni agente riceve la lista delle sue trattative in «Preventivo inviato» / «In trattativa» (pipeline Formazione), con in
cima le scuole che hanno un corso nei prossimi 14 giorni: una telefonata ricorda corso, data e come procedere con l'ordine
(MePA o conferma a mepa@). Escluse le prove interne (scuola «Spaggiari», contatti @spaggiari).

  python elenco_agenti_corsi.py                  prova: stampa chi riceverebbe cosa
  AGENTI_AUTOMATICI=1 python elenco_agenti_corsi.py   spedisce a ciascun agente (copia a Andrea)
  PROVA_A=<e-mail> python elenco_agenti_corsi.py     manda il primo elenco SOLO a quell'indirizzo
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
from email.message import EmailMessage

if not os.environ.get("HUBSPOT_TOKEN"):
    os.environ["HUBSPOT_TOKEN"] = open(os.path.expanduser("~/.hs_pages_token.txt")).read().strip()
import preventivi_corsi as P  # noqa: E402

QUI = os.path.dirname(os.path.abspath(__file__))
AUTOMATICO = os.environ.get("AGENTI_AUTOMATICI") == "1"
PROVA_A = os.environ.get("PROVA_A", "")
COPIA = ["pizzola@spaggiari.eu"]
OGGI = datetime.date.today()
GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]


def esc(t):
    return html.escape(str(t))


def data_it(iso):
    d = datetime.date.fromisoformat(iso)
    return "%s %d %s" % (GIORNI[d.weekday()], d.day, MESI[d.month - 1])


def trattative():
    pal = {x["codice"].upper(): x for x in json.load(open(os.path.join(QUI, "corsi_palinsesto.json"), encoding="utf-8"))}
    deals, after = [], None
    while True:
        b = {"filterGroups": [{"filters": [{"propertyName": "pipeline", "operator": "EQ", "value": P.PIPELINE},
                                           {"propertyName": "dealstage", "operator": "IN", "values": ["6059680980", "6059680981"]}]}],
             "properties": ["dealname", "amount", "createdate", "preventivo_inviato_il", "hubspot_owner_id"], "limit": 100}
        if after:
            b["after"] = after
        r = P.hs("/crm/v3/objects/deals/search", b, "POST")
        deals += r.get("results", [])
        after = (r.get("paging") or {}).get("next", {}).get("after")
        if not after:
            break
    out = []
    for d in deals:
        p = d["properties"]
        if re.search(r"spaggiari", p.get("dealname") or "", re.I) or not p.get("hubspot_owner_id"):
            continue
        corsi = []
        for i in P.figli(d["id"], "line_items"):
            x = P.hs("/crm/v3/objects/line_items/%s?properties=hs_sku,name" % i)["properties"]
            v = pal.get(re.sub(r"-(1|3|I)$", "", (x.get("hs_sku") or "").upper()))
            if v:
                corsi.append((v["data"], v["titolo"].replace("�", "-")[:60]))
        futuri = sorted(c for c in corsi if c[0] >= OGGI.isoformat())
        inv = (p.get("preventivo_inviato_il") or p["createdate"])[:10]
        out.append({"id": d["id"], "nome": (p["dealname"] or "")[:60], "owner": p["hubspot_owner_id"], "inviato": inv,
                    "giorni": (OGGI - datetime.date.fromisoformat(inv)).days, "importo": p.get("amount") or "",
                    "prossimo": futuri[0][0] if futuri else "", "corsi": corsi, "futuri": futuri})
    return out


def corpo(nome_agente, righe):
    vicini = [r for r in righe if r["prossimo"] and (datetime.date.fromisoformat(r["prossimo"]) - OGGI).days <= 14]
    altri = [r for r in righe if r not in vicini]

    def tab(rr):
        if not rr:
            return "<p>Nessuna.</p>"
        t = '<table style="border-collapse:collapse;font-size:14px"><tr>' + "".join(
            '<th style="text-align:left;padding:6px 10px;border-bottom:2px solid #06484b">%s</th>' % h for h in ("Scuola", "Corsi", "Prossimo corso", "Preventivo", "Importo")) + "</tr>"
        for r in rr:
            corsi = "<br>".join("%s · %s" % (esc(data_it(d) if d >= OGGI.isoformat() else "registrazione"), esc(t_)) for d, t_ in r["corsi"])
            t += '<tr><td style="padding:6px 10px;border-bottom:1px solid #ddd">%s</td><td style="padding:6px 10px;border-bottom:1px solid #ddd">%s</td>' \
                 '<td style="padding:6px 10px;border-bottom:1px solid #ddd">%s</td><td style="padding:6px 10px;border-bottom:1px solid #ddd">inviato %d giorni fa</td>' \
                 '<td style="padding:6px 10px;border-bottom:1px solid #ddd">%s €</td></tr>' % (
                     esc(r["nome"]), corsi, esc(data_it(r["prossimo"]) if r["prossimo"] else "-"), r["giorni"], esc(r["importo"]))
        return t + "</table>"
    testo_t = ["Ciao %s," % nome_agente, "", "ecco l'elenco delle tue trattative sui corsi in «Preventivo inviato» (%d), ordinato dalla data più vicina." % len(righe),
               "In cima trovi le scuole con un corso nei prossimi 14 giorni (%d): una telefonata ricorda il corso, la data e come procedere con l'ordine (MePA o conferma a mepa@spaggiari.eu)." % len(vicini), ""]
    for r in vicini + altri:
        testo_t.append("- %s | %s | prossimo: %s | preventivo inviato %d giorni fa | %s €" % (r["nome"], "; ".join(t_ for _, t_ in r["corsi"]), r["prossimo"] or "-", r["giorni"], r["importo"]))
    testo_t += ["", "Le scuole che hanno già un ordine ma non hanno ancora indicato i partecipanti ricevono il promemoria dal sistema.", "", "Spaggiari"]
    h = ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.55;color:#0E2A4D">'
         "<p>Ciao %s,</p><p>ecco l&rsquo;elenco delle tue trattative sui corsi in &laquo;Preventivo inviato&raquo; (%d), dalla data pi&ugrave; vicina.</p>"
         "<p><b>Con un corso nei prossimi 14 giorni (%d)</b>: una telefonata ricorda il corso, la data e come procedere con l&rsquo;ordine "
         "(MePA o conferma a <a href=\"mailto:mepa@spaggiari.eu\">mepa@spaggiari.eu</a>).</p>%s<p><b>Altre trattative (%d)</b></p>%s"
         "<p>Le scuole che hanno gi&agrave; un ordine ma non hanno ancora indicato i partecipanti ricevono il promemoria dal sistema.</p><p>Spaggiari</p></div>"
         % (esc(nome_agente), len(righe), len(vicini), tab(vicini), len(altri), tab(altri)))
    return "\n".join(testo_t), h


def spedisci(a, oggetto, testo, html_, cc=None):
    u, k = os.environ.get("SMTP_CORSI_USER"), os.environ.get("SMTP_CORSI_PASS")
    if not (u and k):
        raise RuntimeError("credenziali SMTP assenti")
    m = EmailMessage()
    m["From"] = "Spaggiari <%s>" % P.MITTENTE
    m["To"] = a
    if cc:
        m["Cc"] = ", ".join(cc)
    m["Reply-To"] = "pizzola@spaggiari.eu"
    m["Subject"] = oggetto
    m.set_content(testo)
    m.add_alternative(html_, subtype="html")
    s = smtplib.SMTP(P.SMTP_HOST, P.SMTP_PORTA, timeout=60)
    s.starttls(context=ssl.create_default_context())
    s.login(u, k)
    s.send_message(m)
    s.quit()


def main():
    per = collections.defaultdict(list)
    for t in trattative():
        per[t["owner"]].append(t)
    print("agenti con trattative: %d · trattative: %d%s" % (len(per), sum(len(v) for v in per.values()), "" if AUTOMATICO or PROVA_A else "   [prova: nessun invio]"))
    inviati = 0
    for owner, righe in sorted(per.items(), key=lambda kv: -len(kv[1])):
        o = P.hs("/crm/v3/owners/%s" % owner)
        mail = o.get("email")
        nome = (o.get("firstName") or "").strip().title() or mail
        righe.sort(key=lambda r: (r["prossimo"] or "9999", r["giorni"]))
        vicini = sum(1 for r in righe if r["prossimo"] and (datetime.date.fromisoformat(r["prossimo"]) - OGGI).days <= 14)
        print("  %-22s %2d trattative · %d con corso entro 14 giorni · %s" % (nome[:22], len(righe), vicini, mail))
        if not mail:
            continue
        testo, h = corpo(nome, righe)
        oggetto = "Corsi: le tue trattative con il preventivo aperto"
        if PROVA_A:
            spedisci(PROVA_A, "[PROVA] " + oggetto, testo, h)
            print("  prova inviata a", PROVA_A)
            return
        if AUTOMATICO:
            spedisci(mail, oggetto, testo, h, COPIA)
            inviati += 1
    if AUTOMATICO:
        print("elenchi inviati: %d" % inviati)


if __name__ == "__main__":
    main()
