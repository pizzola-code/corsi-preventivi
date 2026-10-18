# -*- coding: utf-8 -*-
"""Promemoria progressivi a chi ha chiesto un preventivo di corsi (Andrea, 18/10/2026).

«Sollecita i richiedenti, non gli agenti, con promemoria progressivi nel flusso automatico ogni 7 giorni.»

Per ogni trattativa della pipeline Formazione ferma su «Preventivo inviato» si guarda da quanti giorni e' partito il
preventivo (`preventivo_inviato_il`) e si manda a chi l'ha chiesto il passo che tocca:
  passo 1 a  7 giorni · il preventivo a portata di mano, con i corsi e le date aggiornate
  passo 2 a 14 giorni · come si acquista su MePA (ODA o trattativa diretta, CPV, esenzione IVA)
  passo 3 a 21 giorni · i prossimi appuntamenti; i corsi gia' svolti sono disponibili in registrazione
  passo 4 a 28 giorni · il preventivo scade: se serve piu' tempo, se ne manda uno aggiornato
Un passo solo per giro e mai all'indietro: chi entra nel giro con un preventivo di 20 giorni riceve il passo 2, poi il 3
e il 4 a distanza di 7 giorni l'uno dall'altro. Il conteggio sta sulla trattativa (`solleciti_preventivo`, numero
dell'ultimo passo inviato; `sollecito_preventivo_il`, data), quindi un giro ripetuto non manda doppioni.
Si ferma da solo appena la trattativa esce da «Preventivo inviato» (ordine, trattativa su MePA, persa).
Stesso canale del preventivo: no_reply@spaggiari.eu, risposte all'ufficio MePA. Solo nei giorni feriali, 9-17.

  python solleciti_preventivi.py --prova          dice a chi manderebbe cosa, scrive le anteprime, non manda nulla
  python solleciti_preventivi.py                  manda (solo con SOLLECITI_PREVENTIVI=1)
"""
import datetime
import html as H
import io
import json
import os
import re
import smtplib
import ssl
import sys
from email.message import EmailMessage

import preventivi_corsi as C

PROVA = "--prova" in sys.argv
ATTIVO = os.environ.get("SOLLECITI_PREVENTIVI") == "1"
PASSI_GIORNI = [7, 14, 21, 28]
CAMPO_N, CAMPO_IL = "solleciti_preventivo", "sollecito_preventivo_il"
QUI = os.path.dirname(os.path.abspath(__file__))
PETROLIO, ORO = "#06484b", "#e8b547"
MESI = ["", "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre",
        "novembre", "dicembre"]
GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
ROMA = datetime.timezone(datetime.timedelta(hours=2))       # fino al 25/10; poi il giro gira comunque di giorno


def data_it(d):
    return "%s %d %s" % (GIORNI[d.weekday()], d.day, MESI[d.month])


def assicura_campi():
    for nome, etichetta, tipo, campo in ((CAMPO_N, "Promemoria preventivo inviati", "number", "number"),
                                         (CAMPO_IL, "Ultimo promemoria preventivo", "datetime", "date")):
        if C.hs("/crm/v3/properties/deals/%s" % nome).get("name"):
            continue
        C.hs("/crm/v3/properties/deals", {"name": nome, "label": etichetta, "type": tipo, "fieldType": campo,
                                          "groupName": "dealinformation",
                                          "description": "Promemoria automatici al richiedente (solleciti_preventivi.py)"}, "POST")


def palinsesto():
    p = json.load(io.open(os.path.join(QUI, "corsi_palinsesto.json"), encoding="utf-8"))
    return {x["codice"].upper(): x for x in p}


def registrati():
    """Codici dei corsi svolti con «Registrazione disponibile» sul catalogo (fonte: la pagina, come catalogo_registrazioni)."""
    try:
        import catalogo_registrazioni as K
        cards = K.schede(K.api("/cms/v3/pages/site-pages/%s" % K.PAGINA))
    except Exception as e:
        print("  catalogo non letto (%s): nessun corso considerato in registrazione" % type(e).__name__)
        return set()
    return {cod for cod in palinsesto() if any(cod in str(k.get("c_desc")) and K.ETICHETTA in str(k.get("c_desc")) for k in cards)}


def corsi(descrizione, pal, reg, adesso):
    out = []
    for r in C.righe_da(descrizione or ""):
        if not r.get("codice"):
            continue
        cod_pieno = r["codice"].upper().split()[0]
        cod = re.sub(r"-(1|3|I)$", "", cod_pieno)
        x = pal.get(cod)
        stato, quando = "", r.get("quando") or ""
        if x:
            a, m, g = map(int, x["data"].split("-"))
            giorno = datetime.date(a, m, g)
            quando = "%s · %s–%s" % (data_it(giorno), x["inizio"], x["fine"])
            passato = giorno < (adesso + datetime.timedelta(hours=2)).date()
            if passato:
                if cod in reg:
                    stato = "registrazione"
                    quando = "già svolto · disponibile in registrazione"
                else:
                    stato = "svolto"
                    quando = "già svolto"
            else:
                stato = "in programma"
        out.append({"corso": r["corso"], "licenza": r.get("licenza") or "", "codice": cod_pieno, "prezzo": r.get("prezzo") or 0,
                    "quando": quando, "stato": stato, "illimitata": cod_pieno.endswith("-I")})
    return out


def tabella(righe):
    t = "".join(
        '<tr><td style="padding:10px 12px;border-bottom:1px solid #e3e9e8"><b style="color:#0f2b2c">%s</b><br>'
        '<span style="color:#6d817f;font-size:13px">%s &middot; %s &middot; cod. %s</span></td>'
        '<td style="padding:10px 12px;border-bottom:1px solid #e3e9e8;text-align:right;white-space:nowrap;color:#0f2b2c">%s</td></tr>'
        % (H.escape(r["corso"]), H.escape(r["quando"]), H.escape(r["licenza"]), H.escape(r["codice"]), C.euro(r["prezzo"]))
        for r in righe)
    return '<table style="border-collapse:collapse;width:100%%;font:14px/1.5 Arial,sans-serif;margin:16px 0">%s</table>' % t


def bottone(link, testo="APRI IL PREVENTIVO"):
    return ('<p style="margin:22px 0"><a href="%s" style="background:%s;color:%s;text-decoration:none;font-weight:700;'
            'padding:13px 22px;border-radius:10px;display:inline-block">%s</a></p>' % (link, ORO, PETROLIO, testo))


def box(titolo, testo):
    return ('<div style="background:#f2f7f6;border-left:3px solid %s;padding:12px 14px;margin:16px 0"><b>%s</b>%s</div>'
            % (PETROLIO, titolo, testo))


def come_si_acquista(righe):
    if len(righe) == 1:
        come = ("<p style=\"margin:4px 0 0\">Con un solo corso la Scuola procede con un <b>ordine diretto (ODA)</b> su MePA, "
                "usando il codice indicato nel preventivo.</p>")
    else:
        come = ("<p style=\"margin:4px 0 0\">Con pi&ugrave; corsi l&rsquo;acquisto avviene con una <b>trattativa diretta</b> su MePA:</p>"
                "<ol style=\"margin:6px 0 0;padding-left:20px\"><li>la Scuola apre la trattativa con Gruppo Spaggiari Parma S.p.A. "
                "e indica i codici e l&rsquo;importo del preventivo;</li><li>noi verifichiamo l&rsquo;importo e confermiamo "
                "l&rsquo;offerta.</li></ol>")
    return box("Come si acquista", come +
               "<p style=\"margin:8px 0 0\">Nell&rsquo;ordine si indica il <b>CPV 80500000-9</b> (servizi di formazione). "
               "Gli importi sono <b>esenti da IVA</b>: Gruppo Spaggiari Parma S.p.A. &egrave; Ente Certificato per erogare "
               "la Formazione del Personale della Scuola.</p>")


def nota_registrazioni(righe):
    reg = [r for r in righe if r["stato"] == "registrazione"]
    if not reg:
        return ""
    t = ("<p style=\"margin:4px 0 0\">I corsi gi&agrave; svolti si possono acquistare <b>in registrazione</b>, con lo stesso codice "
         "e lo stesso prezzo: chi partecipa riceve un link personale per guardarli quando preferisce.</p>")
    if any(r["illimitata"] for r in reg):
        t += ("<p style=\"margin:8px 0 0\">Le registrazioni si acquistano per 1 o 3 partecipanti: se le serve il preventivo "
              "aggiornato, risponda a questa e-mail.</p>")
    return box("Disponibili in registrazione", t)


def firma():
    mepa = '<a href="mailto:%s" style="color:%s">%s</a>' % (C.MEPA, PETROLIO, C.MEPA)
    return ("<p>Per qualsiasi domanda pu&ograve; rispondere a questa e-mail o scrivere al nostro ufficio MePA: %s.</p>"
            "<p>Cordiali saluti</p>"
            "<p style=\"color:#6d817f;font-size:13px;margin-top:26px\">Gruppo Spaggiari Parma S.p.A. &middot; Via Bernini 22/A, "
            "43126 Parma &middot; P.IVA 00150470342</p>" % mepa)


def email(passo, d):
    """(oggetto, html) del passo 1-4. d: nome, scuola, numero, link, scadenza, righe."""
    righe, n = d["righe"], d["numero"]
    prossimi = [r for r in righe if r["stato"] == "in programma"]
    apri = "<div style=\"font:15px/1.6 Arial,sans-serif;color:#3f5453;max-width:660px\"><p>Gentile %s,</p>" % H.escape(d["nome"])
    if passo == 1:
        ogg = "Preventivo n. %s · i corsi scelti per %s" % (n, d["scuola"])
        corpo = ("<p>Le riportiamo il <b>preventivo n. %s</b> dei corsi di formazione richiesti per %s, cos&igrave; lo ha a portata "
                 "di mano. Il preventivo &egrave; valido fino a %s.</p>" % (n, H.escape(d["scuola"]), d["scadenza"])
                 + tabella(righe) + come_si_acquista(righe) + nota_registrazioni(righe) + bottone(d["link"]))
    elif passo == 2:
        ogg = "Preventivo n. %s · come si acquista su MePA" % n
        corpo = ("<p>Le riassumiamo i passaggi per acquistare su MePA i corsi del <b>preventivo n. %s</b>, intestato a %s. "
                 "Il nostro ufficio MePA &egrave; a disposizione per accompagnarla in ogni passaggio.</p>"
                 % (n, H.escape(d["scuola"])) + come_si_acquista(righe) + tabella(righe) + nota_registrazioni(righe)
                 + bottone(d["link"]))
    elif passo == 3:
        ogg = "Preventivo n. %s · i prossimi appuntamenti" % n
        if prossimi and len(prossimi) == len(righe):
            testa = ("<p>I corsi del <b>preventivo n. %s</b> sono in calendario nelle prossime settimane. Con l&rsquo;ordine "
                     "su MePA, chi partecipa riceve il link per collegarsi alla diretta.</p>" % n)
        elif prossimi:
            testa = ("<p>Ecco a che punto sono i corsi del <b>preventivo n. %s</b>: alcuni sono in calendario nelle prossime "
                     "settimane, altri si sono gi&agrave; svolti e sono disponibili in registrazione.</p>" % n)
        else:
            testa = "<p>Le riportiamo lo stato dei corsi del <b>preventivo n. %s</b>.</p>" % n
        corpo = (testa + tabella(righe) + nota_registrazioni(righe)
                 + "<p>Se servono altri corsi o un numero diverso di partecipanti, risponda a questa e-mail: aggiorniamo "
                   "il preventivo.</p>" + bottone(d["link"]))
    else:
        ogg = "Preventivo n. %s · valido fino a %s" % (n, d["scadenza"])
        corpo = ("<p>Il <b>preventivo n. %s</b> per %s &egrave; valido fino a <b>%s</b>. Se alla Scuola serve pi&ugrave; tempo, "
                 "risponda a questa e-mail: Le inviamo un preventivo aggiornato.</p>" % (n, H.escape(d["scuola"]), d["scadenza"])
                 + tabella(righe) + nota_registrazioni(righe) + bottone(d["link"]))
    return ogg, apri + corpo + firma() + "</div>"


def manda(a, oggetto, html):
    m = EmailMessage()
    m["From"] = "Spaggiari <%s>" % C.MITTENTE
    m["To"] = a
    m["Reply-To"] = C.MEPA
    m["Subject"] = oggetto
    m.set_content("Il messaggio e' in HTML.")
    m.add_alternative(html, subtype="html")
    s = smtplib.SMTP(C.SMTP_HOST, C.SMTP_PORTA, timeout=60)
    s.starttls(context=ssl.create_default_context())
    s.login(os.environ["SMTP_CORSI_USER"], os.environ["SMTP_CORSI_PASS"])
    s.send_message(m)
    s.quit()


def main():
    adesso = datetime.datetime.now(datetime.timezone.utc)
    ora_it = adesso + datetime.timedelta(hours=2)
    if not PROVA and (ora_it.weekday() >= 5 or not (9 <= ora_it.hour < 17)):
        print("solleciti preventivi: fuori orario (feriali 9-17), niente invii")
        return
    if not PROVA:
        assicura_campi()
    pal, reg = palinsesto(), registrati()
    trattative, dopo = [], None
    while True:
        r = C.hs("/crm/v3/objects/deals/search", {"filterGroups": [{"filters": [
            {"propertyName": "pipeline", "operator": "EQ", "value": C.PIPELINE},
            {"propertyName": "dealstage", "operator": "EQ", "value": C.STADIO_INVIATO}]}],
            "properties": ["dealname", "description", "preventivo_inviato_il", CAMPO_N, CAMPO_IL], "limit": 100,
            **({"after": dopo} if dopo else {})}, "POST")
        trattative += r.get("results", [])
        dopo = (r.get("paging") or {}).get("next", {}).get("after")
        if not dopo:
            break
    cartella = os.path.join(QUI, "anteprime_solleciti")
    mandati = 0
    for d in trattative:
        p = d["properties"]
        if not p.get("preventivo_inviato_il"):
            continue
        giorni = (adesso - datetime.datetime.fromisoformat(p["preventivo_inviato_il"].replace("Z", "+00:00"))).days
        fatti = int(float(p.get(CAMPO_N) or 0))
        dovuti = sum(1 for g in PASSI_GIORNI if giorni >= g)
        if dovuti <= fatti or fatti >= len(PASSI_GIORNI):
            continue
        if p.get(CAMPO_IL):        # un passo ogni 7 giorni al massimo, anche se il preventivo e' vecchio
            ultimo = datetime.datetime.fromisoformat(p[CAMPO_IL].replace("Z", "+00:00"))
            if (adesso - ultimo).days < 7:
                continue
        passo = fatti + 1
        q = C.hs("/crm/v4/objects/deals/%s/associations/quotes" % d["id"]).get("results", [])
        ct = C.hs("/crm/v4/objects/deals/%s/associations/contacts" % d["id"]).get("results", [])
        if not q or not ct:
            print("  %s: manca il preventivo o il contatto, salto" % p.get("dealname"))
            continue
        qp = C.hs("/crm/v3/objects/quotes/%s?properties=hs_quote_link,hs_quote_number,hs_expiration_date" % q[-1]["toObjectId"]).get("properties", {})
        cp = C.hs("/crm/v3/objects/contacts/%s?properties=email,firstname,lastname" % ct[0]["toObjectId"]).get("properties", {})
        if not cp.get("email") or not qp.get("hs_quote_link"):
            continue
        scad = datetime.datetime.fromisoformat(qp["hs_expiration_date"].replace("Z", "+00:00")).date() if qp.get("hs_expiration_date") else None
        if scad and scad < ora_it.date():
            print("  %s: preventivo scaduto il %s, niente promemoria" % (p.get("dealname"), scad))
            continue
        righe = corsi(p.get("description"), pal, reg, adesso)
        if righe and all(r["stato"] == "svolto" for r in righe):
            print("  %s: tutti i corsi svolti e senza registrazione, niente promemoria" % p.get("dealname"))
            continue
        dati = {"nome": " ".join(x for x in (cp.get("firstname"), cp.get("lastname")) if x).strip() or "Dirigente",
                "scuola": (p.get("dealname") or "").split(" - ", 1)[-1], "numero": qp.get("hs_quote_number"),
                "link": qp["hs_quote_link"], "scadenza": data_it(scad) if scad else "30 giorni dall'invio", "righe": righe}
        ogg, html = email(passo, dati)
        print("  %s · %d giorni · passo %d · %s -> %s" % (dati["scuola"][:45], giorni, passo, ogg, C.mascherato(cp["email"])))
        if PROVA:
            os.makedirs(cartella, exist_ok=True)
            io.open(os.path.join(cartella, "passo%d_%s.html" % (passo, d["id"])), "w", encoding="utf-8").write(
                '<!doctype html><meta charset="utf-8"><div style="font:12px Arial;color:#666;margin:10px">Oggetto: %s</div>%s' % (H.escape(ogg), html))
            continue
        if not ATTIVO:
            print("    (SOLLECITI_PREVENTIVI spento: non mando)")
            continue
        C.hs("/crm/v3/objects/deals/%s" % d["id"], {"properties": {CAMPO_N: passo, CAMPO_IL: adesso.strftime("%Y-%m-%dT%H:%M:%SZ")}}, "PATCH")
        try:                         # segnato PRIMA di mandare: al massimo un invio per passo, anche se qualcosa va storto
            manda(cp["email"], ogg, html)
            mandati += 1
        except Exception as e:
            print("    invio non riuscito (%s)" % type(e).__name__)
    print("solleciti preventivi: %d trattative aperte, %d promemoria %s" % (len(trattative), mandati, "(prova)" if PROVA else "inviati"))


if __name__ == "__main__":
    main()
