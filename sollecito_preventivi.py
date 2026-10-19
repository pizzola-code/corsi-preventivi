# -*- coding: utf-8 -*-
"""Seguito ai preventivi di corsi ancora aperti (Andrea, 19/10/2026): alla scuola che ha chiesto il preventivo e non ha ancora ordinato
si ricorda, in positivo, che la registrazione di un corso gia' svolto e' disponibile allo stesso prezzo (gruppo A) oppure che un corso
ancora da fare si puo' seguire in diretta o in registrazione (gruppo B).

  - mittente come il preventivo: no_reply@spaggiari.eu dal relay SMTP di HubSpot, Reply-To mepa@; l'agente della trattativa in copia;
    mai dalla casella di Andrea;
  - una e-mail per TRATTATIVA e gruppo, una volta sola (registro `ordini_corsi_gestiti.txt`, chiave `<trattativa>|sollecito_prev_A|B`);
  - gruppo A: parte subito per i corsi svolti con registrazione su Vimeo e non ordinati dalla scuola;
    gruppo B: parte a 48 ore dal primo corso non ordinato (o subito se e' piu' vicino), mai a meno di un'ora dall'inizio;
  - escluse: scuole che hanno gia' ordinato quel corso, prove interne (nessuna scuola, indirizzi Spaggiari, nome scuola con «spaggiari»/«deledda»),
    scuole con una richiesta di sole registrazioni arrivata dal 16/10 (hanno gia' il loro preventivo).
  python sollecito_preventivi.py            elenco di chi riceverebbe cosa (non invia)
  python sollecito_preventivi.py --invia    invia (serve il via libera di Andrea; con SOLLECITO_PREVENTIVI=1 parte dal giro orario)
"""
import datetime
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
from email.message import EmailMessage

import corsi_ordini as O
import preventivi_corsi as P
import preventivi_in_scadenza as PS

C = P
STADI_APERTI = PS.STADI_APERTI
GIORNI, MESI = O.GIORNI, O.MESI
INTERNI = re.compile(r"spaggiari|deledda|spaggiarinet|gruppospaggiari", re.I)
ORE_B = 48
DAL_REGISTRAZIONI = datetime.datetime(2026, 10, 16, 0, 0, tzinfo=datetime.timezone.utc)


def _inizio_fine(voce):
    a, m, g = map(int, voce["data"].split("-"))
    leg = datetime.datetime(a, m, g) < datetime.datetime(2026, 10, 25)
    off = 2 if leg else 1
    h, mi = map(int, voce["inizio"].split(":"))
    h2, mi2 = map(int, voce["fine"].split(":"))
    u = datetime.timezone.utc
    return (datetime.datetime(a, m, g, h - off, mi, tzinfo=u), datetime.datetime(a, m, g, h2 - off, mi2, tzinfo=u))


def _quando(voce):
    a, m, g = map(int, voce["data"].split("-"))
    d = datetime.date(a, m, g)
    return "%s %d %s" % (GIORNI[d.weekday()], g, MESI[m - 1])


def _euro(x):
    return ("%d" % x) if float(x) == int(float(x)) else ("%.2f" % x).replace(".", ",")


def _registrazioni_su_vimeo():
    """basi dei corsi svolti con recording_link nell'evento."""
    out, dopo = set(), None
    for _ in range(10):
        r = C.hs("/crm/v3/objects/%s/search" % O.EV, {"filterGroups": [{"filters": [
            {"propertyName": "external_id", "operator": "CONTAINS_TOKEN", "value": "CF-*"},
            {"propertyName": "recording_link", "operator": "HAS_PROPERTY"}]}], "properties": ["external_id"], "limit": 100,
            **({"after": dopo} if dopo else {})}, "POST")
        for x in r.get("results", []):
            m = re.match(r"^CF-([A-Z0-9]+)-\d{4}$", (x["properties"].get("external_id") or "").upper())
            if m:
                out.add(m.group(1))
        dopo = (r.get("paging") or {}).get("next", {}).get("after")
        if not dopo:
            break
    return out


def trattative():
    righe, dopo = [], None
    for _ in range(10):
        r = C.hs("/crm/v3/objects/deals/search", {"filterGroups": [{"filters": [
            {"propertyName": "pipeline", "operator": "EQ", "value": PS.PIPELINE_FORMAZIONE},
            {"propertyName": "dealstage", "operator": "IN", "values": STADI_APERTI}]}],
            "properties": ["dealname", "dealstage", "createdate", "description", "hubspot_owner_id"], "limit": 100,
            **({"after": dopo} if dopo else {})}, "POST")
        righe += r.get("results", [])
        dopo = (r.get("paging") or {}).get("next", {}).get("after")
        if not dopo:
            break
    return righe


def destinatario(deal_id):
    """(email, nome) del contatto che ha chiesto il preventivo; None se interno o assente."""
    a = C.hs("/crm/v4/objects/deals/%s/associations/contacts" % deal_id).get("results", [])
    for x in a:
        c = C.hs("/crm/v3/objects/contacts/%s?properties=email,firstname,lastname" % x["toObjectId"])["properties"]
        mail = (c.get("email") or "").strip().lower()
        if mail and not INTERNI.search(mail.split("@")[-1]):
            return mail, (c.get("firstname") or "").strip().title()
    return None, ""


def agente(owner_id):
    if not owner_id:
        return ""
    try:
        return (C.hs("/crm/v3/owners/%s" % owner_id).get("email") or "").strip().lower()
    except Exception:
        return ""


def costruisci():
    """lista di invii: dict(deal, scuola, email, nome, gruppo, righe, quando_b)"""
    adesso = datetime.datetime.now(datetime.timezone.utc)
    pal = {x["base"]: x for x in O.palinsesto()}
    con_reg = _registrazioni_su_vimeo()
    ordinati = PS._ordinati(O.ordini_nuovi(30))
    deals = trattative()
    # scuole che hanno gia' una richiesta di sole registrazioni dal 16/10
    gia_reg = set()
    for d in deals:
        if "Registrazione disponibile" in (d["properties"].get("description") or "") and \
                d["properties"]["createdate"] >= DAL_REGISTRAZIONI.strftime("%Y-%m-%dT%H:%M:%S"):
            for x in C.hs("/crm/v4/objects/deals/%s/associations/companies" % d["id"]).get("results", []):
                gia_reg.add(str(x["toObjectId"]))
    out = []
    for d in deals:
        p = d["properties"]
        desc = p.get("description") or ""
        righe = P.righe_da("\n".join(l for l in desc.split("\n") if "cod." in l and "EUR" in l))
        az = C.hs("/crm/v4/objects/deals/%s/associations/companies" % d["id"]).get("results", [])
        if not az or not righe:
            continue
        cid = str(az[0]["toObjectId"])
        nome_sc = O.pulito(C.hs("/crm/v3/objects/companies/%s?properties=name" % cid)["properties"].get("name") or "")
        if INTERNI.search(nome_sc or "") or cid in gia_reg:
            continue
        svolti, futuri = [], []
        for rg in righe:
            cod = (rg["codice"] or "").upper().split()[0] if rg["codice"] else ""
            base = re.sub(r"^WBRELE?", "", re.sub(r"-(1|3|I)$", "", cod))
            v = pal.get(base)
            if not v or (cid, base) in ordinati:
                continue
            ini, fine = _inizio_fine(v)
            voce = {"base": base, "titolo": v["titolo"], "quando": _quando(v), "codice": cod, "licenza": rg["licenza"], "prezzo": rg["prezzo"], "ini": ini}
            if fine < adesso:
                if base in con_reg and not cod.endswith("-I"):       # l'illimitata non si vende in registrazione
                    svolti.append(voce)
            elif ini - datetime.timedelta(hours=1) > adesso:
                futuri.append(voce)
        email, nome = destinatario(d["id"])
        if not email:
            continue
        base_info = {"deal": d["id"], "scuola": nome_sc, "email": email, "nome": nome, "agente": agente(p.get("hubspot_owner_id"))}
        if svolti:
            out.append(dict(base_info, gruppo="A", righe=svolti, futuri=sorted(futuri, key=lambda x: x["ini"])))
        elif futuri:
            primo = min(futuri, key=lambda x: x["ini"])
            quando_b = primo["ini"] - datetime.timedelta(hours=ORE_B)
            out.append(dict(base_info, gruppo="B", righe=sorted(futuri, key=lambda x: x["ini"]), quando_b=quando_b))
    return out


def testo(s):
    """(oggetto, html, testo semplice)"""
    generico = {"segreteria", "dsga", "scuola", "ufficio", "amministrazione", "personale", "dirigente", "istituto", "protocollo"}
    nome_ok = s["nome"] and s["nome"].lower() not in generico and not re.search(r"\d|@", s["nome"])
    saluto = "Buongiorno %s," % s["nome"] if nome_ok else "Buongiorno,"
    def elenco(righe):
        return [("«%s» del %s — licenza %s — %s € — codice %s" % (r["titolo"], r["quando"], r["licenza"], _euro(r["prezzo"]), r["codice"])) for r in righe]
    if s["gruppo"] == "A":
        r = s["righe"]
        if len(r) == 1:
            oggetto = "Registrazione disponibile: «%s»" % r[0]["titolo"]
            apertura = ("la richiesta di preventivo della vostra scuola comprendeva «%s», che si è svolto %s. "
                        "La registrazione è disponibile allo stesso prezzo del preventivo (%s €)." % (r[0]["titolo"], r[0]["quando"], _euro(r[0]["prezzo"])))
            righe = []
        else:
            oggetto = "Registrazioni disponibili dei corsi del vostro preventivo"
            apertura = ("la richiesta di preventivo della vostra scuola comprendeva corsi che si sono già svolti. "
                        "Le loro registrazioni sono disponibili agli stessi prezzi del preventivo:")
            righe = elenco(r)
        corpo = ([[apertura] + righe] if righe else [apertura]) + [
            "Ogni partecipante riceve il proprio link personale, valido 30 giorni, da guardare quando preferite.",
            "Per procedere basta l'ordine su MePA con il codice indicato. Appena arriva, vi scriviamo per indicare chi guarderà la registrazione."]
        if s.get("futuri"):
            corpo.append(["Gli altri corsi del preventivo restano in programma nelle date indicate:"] + elenco(s["futuri"]))
    else:
        r = s["righe"][0]
        oggetto = "«%s» del %s: in diretta o in registrazione" % (r["titolo"], r["quando"])
        apertura = "«%s» del preventivo della vostra scuola si svolge %s." % (r["titolo"], r["quando"])
        corpo = [apertura,
                 "Chi non riesce a collegarsi in diretta può guardarlo dopo: la registrazione resta disponibile allo stesso prezzo (%s €), con un link personale valido 30 giorni." % _euro(r["prezzo"]),
                 "Per procedere basta l'ordine su MePA con il codice %s." % r["codice"]]
        if len(s["righe"]) > 1:
            corpo.append(["Gli altri corsi del preventivo:"] + elenco(s["righe"][1:]))
    corpo.append("Restiamo a disposizione per qualsiasi domanda.")
    stile_p = "margin:0 0 12px 0;font-size:15px;line-height:1.55;color:#1a1a1a"
    h = ["<div style=\"font-family:Arial,Helvetica,sans-serif;max-width:620px\">", "<p style=\"%s\">%s</p>" % (stile_p, html.escape(saluto))]
    t = [saluto, ""]
    for b in corpo:
        if isinstance(b, list):
            intro, voci = b[0], b[1:]
            h.append("<p style=\"%s\">%s</p><ul style=\"margin:0 0 12px 18px;padding:0;font-size:14px;line-height:1.5;color:#1a1a1a\">%s</ul>" % (
                stile_p, html.escape(intro), "".join("<li>%s</li>" % html.escape(v) for v in voci)))
            t += [intro] + ["- " + v for v in voci] + [""]
        else:
            h.append("<p style=\"%s\">%s</p>" % (stile_p, html.escape(b)))
            t += [b, ""]
    h.append("<p style=\"%s\">Spaggiari</p></div>" % stile_p)
    t.append("Spaggiari")
    return oggetto, "".join(h), "\n".join(t)


def invia(s):
    oggetto, corpo_html, corpo_txt = testo(s)
    utente, chiave = os.environ.get("SMTP_CORSI_USER"), os.environ.get("SMTP_CORSI_PASS")
    if not (utente and chiave):
        print("   (credenziali SMTP assenti: non inviato)")
        return False
    m = EmailMessage()
    m["From"] = "Spaggiari <%s>" % P.MITTENTE
    m["To"] = s["email"]
    if s.get("agente"):
        m["Cc"] = s["agente"]
    if O.BCC:
        m["Bcc"] = ", ".join(O.BCC)
    m["Reply-To"] = P.MEPA
    m["Subject"] = oggetto
    m.set_content(corpo_txt)
    m.add_alternative(corpo_html, subtype="html")
    sm = smtplib.SMTP(P.SMTP_HOST, P.SMTP_PORTA, timeout=60)
    sm.starttls(context=ssl.create_default_context())
    sm.login(utente, chiave)
    sm.send_message(m)
    sm.quit()
    return True


def giro(invia_davvero=False, mostra=True):
    gia = O.registro()
    adesso = datetime.datetime.now(datetime.timezone.utc)
    visti = set()                      # un solo messaggio per indirizzo (due trattative della stessa scuola non raddoppiano la posta)
    pronti = []
    for s in costruisci():
        k = "%s|sollecito_prev_%s" % (s["deal"], s["gruppo"])
        if k in gia:
            continue
        if s["gruppo"] == "B" and adesso < s["quando_b"]:
            if mostra:
                print("  [B in attesa] %s · %s · parte dal %s" % (s["scuola"][:40], s["righe"][0]["base"], s["quando_b"].strftime("%d/%m %H:%M UTC")))
            continue
        kmail = "sollecito_prev|%s" % hashlib.sha1(s["email"].encode()).hexdigest()[:16]     # una e-mail per indirizzo, per sempre (nessun indirizzo in chiaro nel registro)
        if kmail in gia or s["email"] in visti:
            if mostra:
                print("  [salto: questo indirizzo ha gia' ricevuto il seguito] trattativa %s" % s["deal"])
            continue
        visti.add(s["email"])
        pronti.append((s, k, kmail))
    atteso = os.environ.get("ATTESO")
    if invia_davvero and atteso and len(pronti) != int(atteso):
        print("\nINTERROTTO: i messaggi pronti sono %d, ne erano attesi %s (elenco approvato): nessun invio" % (len(pronti), atteso))
        return 0
    inviati = 0
    for s, k, kmail in pronti:
        oggetto, _, corpo = testo(s)
        if mostra:
            print("\n=== %s · trattativa %s · a %s · cc %s ===\nOggetto: %s\n%s" % (s["gruppo"], s["deal"], s["email"], s["agente"] or "-", oggetto, corpo))
        if invia_davvero:
            if invia(s):
                O.segna(k)
                O.segna(kmail)
                inviati += 1
                print("   -> inviato (%s, trattativa %s)" % (s["gruppo"], s["deal"]))
    print("\npronti: %d | inviati: %d" % (len(pronti), inviati))
    return inviati


if __name__ == "__main__":
    giro("--invia" in sys.argv)
