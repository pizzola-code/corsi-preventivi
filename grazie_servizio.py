# -*- coding: utf-8 -*-
"""Mail A, parte di SERVIZIO (Andrea, 21/10/2026): un grazie a chi ha frequentato un corso e NON ha il consenso marketing.

Nessun blocco commerciale (chi ha il consenso riceve la bozza HubSpot «Grazie e prossimo corso»). Esclusi: opposizioni art.21
(lista 5979) e disiscritti. Una volta per persona e per corso: chiave `grazie_servizio|<contatto>|<data ultimo corso>` nel registro.

  python grazie_servizio.py            prova: elenca chi riceverebbe cosa, non spedisce
  --fino AAAA-MM-GG / --dal AAAA-MM-GG   limitano per data dell'ultimo corso frequentato (es. due tranche: 21/10 fino al 16/10, 22/10 il 19/10)
  python grazie_servizio.py --invia    spedisce (da no_reply@spaggiari.eu, Reply-To mepa@); MAX=<n> limita i messaggi

NON e' ancora nel giro automatico: si accende solo con l'ok di Andrea.
"""
import datetime
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage

if not os.environ.get("HUBSPOT_TOKEN"):
    os.environ["HUBSPOT_TOKEN"] = open(os.path.expanduser("~/.hs_pages_token.txt")).read().strip()
import preventivi_corsi as P  # noqa: E402

LISTA_OPPOSIZIONI = "5979"


def destinatari():
    opp, after = set(), None
    while True:
        r = P.hs("/crm/v3/lists/%s/memberships?limit=250" % LISTA_OPPOSIZIONI + ("&after=" + after if after else ""))
        opp |= {x["recordId"] for x in r.get("results", [])}
        after = (r.get("paging") or {}).get("next", {}).get("after")
        if not after:
            break
    out, after = [], None
    while True:
        b = {"filterGroups": [{"filters": [{"propertyName": "numero_corsi_frequentati", "operator": "GT", "value": "0"}]}],
             "properties": ["email", "firstname", "hs_marketable_status", "hs_email_optout", "ultimo_corso_frequentato",
                            "data_ultimo_corso"], "limit": 100}
        if after:
            b["after"] = after
        r = P.hs("/crm/v3/objects/contacts/search", b, "POST")
        for c in r.get("results", []):
            p = c["properties"]
            if c["id"] in opp or p.get("hs_email_optout") == "true" or not p.get("email"):
                continue
            if p.get("hs_marketable_status") == "true":
                continue                                  # ha il consenso: riceve la mail HubSpot
            out.append((c["id"], p))
        after = (r.get("paging") or {}).get("next", {}).get("after")
        if not after:
            break
    return out


def corpo(p):
    corso = (p.get("ultimo_corso_frequentato") or "il corso").strip()
    d = (p.get("data_ultimo_corso") or "")[:10]
    data = datetime.date.fromisoformat(d).strftime("%d/%m/%Y") if d else ""
    quando = " del %s" % data if data else ""
    nome = (p.get("firstname") or "").strip()
    saluto = ("Gentile %s," % nome) if nome else "Buongiorno,"
    testo = ("%s\ngrazie per aver partecipato a «%s»%s.\n\nSe la vostra scuola ha domande sul corso o sull'utilizzo del software, "
             "rispondete a questa e-mail: il nostro team è a disposizione.\n\nSpaggiari" % (saluto, corso, quando))
    html = ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.6;color:#0f2b2c;">'
            '<p>%s</p><p>grazie per aver partecipato a &laquo;%s&raquo;%s.</p>'
            '<p>Se la vostra scuola ha domande sul corso o sull&rsquo;utilizzo del software, rispondete a questa e-mail: '
            'il nostro team &egrave; a disposizione.</p><p>Spaggiari</p></div>'
            % (saluto.replace("\n", "<br>"), corso, quando))
    return "Grazie per aver seguito il corso", testo, html


def invia(a, oggetto, testo, html):
    m = EmailMessage()
    m["From"] = "Spaggiari <%s>" % P.MITTENTE
    m["To"] = a
    m["Reply-To"] = P.MEPA
    m["Subject"] = oggetto
    m.set_content(testo)
    m.add_alternative(html, subtype="html")
    s = smtplib.SMTP(P.SMTP_HOST, P.SMTP_PORTA, timeout=60)
    s.starttls(context=ssl.create_default_context())
    s.login(os.environ["SMTP_CORSI_USER"], os.environ["SMTP_CORSI_PASS"])
    s.send_message(m)
    s.quit()


def main():
    davvero = "--invia" in sys.argv
    massimo = int(os.environ.get("MAX", "0") or 0)
    gia = P.registro()
    pronti = []
    fino = sys.argv[sys.argv.index("--fino") + 1] if "--fino" in sys.argv else None
    dal = sys.argv[sys.argv.index("--dal") + 1] if "--dal" in sys.argv else None
    for cid, p in destinatari():
        d = (p.get("data_ultimo_corso") or "")[:10]
        if (fino and d > fino) or (dal and d < dal):
            continue
        k = "grazie_servizio|%s|%s" % (cid, (p.get("data_ultimo_corso") or "")[:10])
        if P.impronta(k) in gia:
            continue
        pronti.append((cid, p, k))
    print("riceverebbero la parte di servizio: %d" % len(pronti))
    if pronti:
        print("--- esempio ---\n%s" % corpo(pronti[0][1])[1])
    if not davvero:
        return
    if not (os.environ.get("SMTP_CORSI_USER") and os.environ.get("SMTP_CORSI_PASS")):
        print("credenziali SMTP assenti: nessun invio")
        return
    n = 0
    for cid, p, k in pronti:
        if massimo and n >= massimo:
            break
        oggetto, testo, html = corpo(p)
        invia(p["email"], oggetto, testo, html)
        P.registra(k)
        n += 1
    print("inviati: %d" % n)


if __name__ == "__main__":
    main()
