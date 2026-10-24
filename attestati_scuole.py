# -*- coding: utf-8 -*-
"""Attestati dei corsi: riepilogo alla SCUOLA (Andrea, 24/10/2026).

Dopo che il giro dei partecipanti (eventi-dashboard-cloud/attestati_corsi.js) ha elaborato un corso, ogni scuola che ha mandato
persone riceve UNA e-mail per ordine e per corso: chi ha ricevuto l'attestato (con il link al PDF), chi ha seguito meno del 50%
della durata e chi non si e' collegato (solo lo stato, nessun testo commerciale). Destinatari come la nomina: chi ha chiesto il
preventivo dal sito e, per le statali, l'indirizzo istituzionale <codice meccanografico>@istruzione.it (corsi_ordini.destinatari).
Mittente no_reply@spaggiari.eu, risposte a info@. Se il corso e' finito da piu' di 36 ore la mail porta una riga di scuse.
Una volta sola per ordine e corso: si segna `attestato_scuola_inviato` sulle iscrizioni PRIMA di inviare.

  python attestati_scuole.py                   prova: elenca cosa partirebbe, non spedisce
  SCUOLE_AUTOMATICHE=1 python attestati_scuole.py   spedisce (anche ogni giorno dal workflow)
  PROVA_A=<e-mail> [EVENTO=<id>] python attestati_scuole.py   manda il primo riepilogo trovato SOLO a quell'indirizzo (non segna nulla)
Alla fine, se qualcosa e' partito o e' rimasto in sospeso, riepilogo del giorno a Andrea Pizzola e Alessia Malerba.
"""
import datetime
import os
import re
import smtplib
import ssl
import sys
from email.message import EmailMessage

if not os.environ.get("HUBSPOT_TOKEN"):
    os.environ["HUBSPOT_TOKEN"] = open(os.path.expanduser("~/.hs_pages_token.txt")).read().strip()
import preventivi_corsi as P  # noqa: E402
import corsi_ordini as O  # noqa: E402

EV, REG = "2-143900361", "2-143900355"
SOGLIA = int(float(os.environ.get("ATTESTATI_SOGLIA", "0.5")) * 100)
AUTOMATICO = os.environ.get("SCUOLE_AUTOMATICHE") == "1"
PROVA_A = os.environ.get("PROVA_A", "")
EVENTO = os.environ.get("EVENTO", "")
RISPOSTE = "info@spaggiari.eu"
NOSTRI = re.compile(r"@(spaggiari\.eu|spaggiarinet\.eu|gruppospaggiari\.it)$", re.I)
RIEPILOGO_A = ["pizzola@spaggiari.eu", "malerba@spaggiari.eu"]


def esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def bello(s):
    return re.sub(r"(^|[\s'’-])([a-zàèéìòù])", lambda m: m.group(1) + m.group(2).upper(), " ".join(str(s or "").split()).lower())


def durata(min_):
    h, m = int(min_ // 60), int(round(min_ % 60))
    return (("%d %s" % (h, "ora" if h == 1 else "ore")) if h else "") + (" e " if h and m else "") + (("%d minuti" % m) if m else "") or "0 minuti"


def cerca(oggetto, corpo):
    out, after = [], None
    while True:
        b = dict(corpo, limit=100)
        if after:
            b["after"] = after
        r = P.hs("/crm/v3/objects/%s/search" % oggetto, b, "POST")
        out += r.get("results", [])
        after = (r.get("paging") or {}).get("next", {}).get("after")
        if not after:
            return out


def azienda_di(ordine):
    d = P.hs("/crm/v3/objects/deals/search", {"filterGroups": [{"filters": [{"propertyName": "dealname", "operator": "EQ", "value": ordine + " - ERP"}]}],
                                              "properties": ["dealname"], "limit": 1}, "POST").get("results", [])
    if not d:
        return None
    a = P.hs("/crm/v4/objects/deals/%s/associations/companies" % d[0]["id"]).get("results", [])
    return str(a[0]["toObjectId"]) if a else None


def scuola_nome(azienda):
    try:
        n = P.hs("/crm/v3/objects/companies/%s?properties=name" % azienda)["properties"].get("name") or ""
        return bello(re.sub(r"^[A-Z0-9]{8,12}\s*-\s*", "", n))
    except Exception:
        return ""


def corpo_mail(corso, quando, ordine, scuola, ok, sotto, assenti, tardi, ore_corso):
    righe_t, righe_h = [], []
    saluto = "Buongiorno,"
    intro = "vi inviamo il riepilogo degli attestati di partecipazione al corso «%s» (%s)%s." % (
        corso, quando, (", seguito da persone della vostra scuola (ordine %s)" % ordine) if ordine else "")
    scuse = "Ci scusiamo per il ritardo con cui vi scriviamo." if tardi else ""
    righe_t += [saluto, "", intro]
    righe_h += ["<p>%s</p>" % saluto, "<p>%s</p>" % esc(intro)]
    if scuse:
        righe_t += ["", scuse]
        righe_h.append("<p>%s</p>" % esc(scuse))
    if ok:
        righe_t += ["", "Hanno ricevuto l'attestato (presenza di almeno il %d%% della durata):" % SOGLIA]
        righe_h.append("<p><b>Hanno ricevuto l&rsquo;attestato</b> (presenza di almeno il %d%% della durata):</p><ul>" % SOGLIA)
        for nome, pres, link in ok:
            righe_t.append("- %s, presenza %s: %s" % (nome, pres, link))
            righe_h.append('<li>%s, presenza %s &middot; <a href="%s">scarica l&rsquo;attestato</a></li>' % (esc(nome), esc(pres), esc(link)))
        righe_h.append("</ul>")
    if sotto:
        righe_t += ["", "Presenza inferiore al %d%% della durata (l'attestato non viene emesso):" % SOGLIA]
        righe_h.append("<p><b>Presenza inferiore al %d%% della durata</b> (l&rsquo;attestato non viene emesso):</p><ul>" % SOGLIA)
        for nome, pres in sotto:
            righe_t.append("- %s, presenza %s su %s" % (nome, pres, ore_corso))
            righe_h.append("<li>%s, presenza %s su %s</li>" % (esc(nome), esc(pres), esc(ore_corso)))
        righe_h.append("</ul>")
    if assenti:
        righe_t += ["", "Non collegati alla diretta:"]
        righe_h.append("<p><b>Non collegati alla diretta:</b></p><ul>")
        for nome in assenti:
            righe_t.append("- " + nome)
            righe_h.append("<li>%s</li>" % esc(nome))
        righe_h.append("</ul>")
    chiusa = "Per qualsiasi domanda potete scrivere a %s." % RISPOSTE
    righe_t += ["", chiusa, "", "Un cordiale saluto,", "Spaggiari"]
    righe_h += ["<p>Per qualsiasi domanda potete scrivere a <a href=\"mailto:%s\">%s</a>.</p>" % (RISPOSTE, RISPOSTE), "<p>Un cordiale saluto,<br>Spaggiari</p>"]
    html = '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.55;color:#0E2A4D;max-width:600px">' + "".join(righe_h) + "</div>"
    return "\n".join(righe_t), html


def spedisci(a, oggetto, testo, html, cc=None):
    u, k = os.environ.get("SMTP_CORSI_USER"), os.environ.get("SMTP_CORSI_PASS")
    if not (u and k):
        raise RuntimeError("credenziali SMTP assenti")
    m = EmailMessage()
    m["From"] = "Spaggiari <%s>" % P.MITTENTE
    m["To"] = ", ".join(a) if isinstance(a, (list, tuple)) else a
    if cc:
        m["Cc"] = ", ".join(cc)
    m["Reply-To"] = RISPOSTE
    m["Subject"] = oggetto
    m.set_content(testo)
    m.add_alternative(html, subtype="html")
    s = smtplib.SMTP(P.SMTP_HOST, P.SMTP_PORTA, timeout=60)
    s.starttls(context=ssl.create_default_context())
    s.login(u, k)
    s.send_message(m)
    s.quit()


CACHE_AZ = {}


def main():
    adesso = datetime.datetime.now(datetime.timezone.utc)
    if EVENTO:
        filtri = [{"propertyName": "hs_object_id", "operator": "EQ", "value": EVENTO}]
    else:
        filtri = [{"propertyName": "start_datetime", "operator": "GTE", "value": "2026-10-05T00:00:00Z"},
                  {"propertyName": "attestati_elaborati_il", "operator": "HAS_PROPERTY"}]
    eventi = [e for e in cerca(EV, {"filterGroups": [{"filters": filtri}], "properties": ["name", "start_datetime", "end_datetime", "annullato"],
                                    "sorts": [{"propertyName": "start_datetime", "direction": "ASCENDING"}]})
              if re.match(r"^\s*corso\s*\|", e["properties"].get("name") or "", re.I) and e["properties"].get("annullato") != "true"]
    print("corsi con attestati elaborati: %d%s" % (len(eventi), "" if AUTOMATICO or PROVA_A else "   [prova: nessun invio]"))
    inviate, senza_dest, errori, pronte = [], [], [], 0
    for e in eventi:
        q = e["properties"]
        corso = re.sub(r"^\s*corso\s*\|\s*", "", q["name"], flags=re.I)
        fine = datetime.datetime.fromisoformat(q["end_datetime"].replace("Z", "+00:00"))
        ore_corso = durata((fine - datetime.datetime.fromisoformat(q["start_datetime"].replace("Z", "+00:00"))).total_seconds() / 60)
        tardi = (adesso - fine).total_seconds() > 36 * 3600
        quando = O.quando(q["start_datetime"], q["end_datetime"])
        regs = cerca(REG, {"filterGroups": [{"filters": [{"propertyName": "event_id", "operator": "EQ", "value": e["id"]},
                                                         {"propertyName": "status", "operator": "NEQ", "value": "Canceled"}]}],
                           "properties": ["email", "first_name", "last_name", "full_name", "ordine_corso", "solo_registrazione", "attestato_esito",
                                          "attestato_minuti", "attestato_link", "attestato_scuola_inviato"]})
        gruppi = {}
        for x in regs:
            p = x["properties"]
            if NOSTRI.search((p.get("email") or "").strip()) or p.get("solo_registrazione") in ("si", "true"):
                continue
            gruppi.setdefault(p.get("ordine_corso") or "", []).append(x)
        # una scuola con piu' ordini sullo stesso corso riceve UNA mail (24/10/2026)
        per_scuola = {}
        for ordine, gente in sorted(gruppi.items()):
            if not ordine or ordine.startswith("EXTRA-"):
                continue
            if ordine not in CACHE_AZ:
                CACHE_AZ[ordine] = azienda_di(ordine)
            azienda = CACHE_AZ[ordine]
            g = per_scuola.setdefault(azienda or "ORD:" + ordine, {"azienda": azienda, "ordini": [], "gente": []})
            g["ordini"].append(ordine)
            g["gente"] += gente
        for chiave_s, g in per_scuola.items():
            azienda, gente = g["azienda"], g["gente"]
            ordine = ", ".join(sorted(g["ordini"]))
            if any(x["properties"].get("attestato_scuola_inviato") for x in gente):
                continue
            if not all(x["properties"].get("attestato_esito") for x in gente):
                continue                                           # il giro dei partecipanti non ha ancora valutato tutti
            ok, sotto, assenti = [], [], []
            for x in gente:
                p = x["properties"]
                nome = bello(p.get("full_name") or "%s %s" % (p.get("first_name") or "", p.get("last_name") or ""))
                m = float(p.get("attestato_minuti") or 0)
                if p["attestato_esito"] == "inviato":
                    ok.append((nome, durata(m), p.get("attestato_link") or ""))
                elif p["attestato_esito"] == "sotto_soglia":
                    sotto.append((nome, durata(m)))
                else:
                    assenti.append(nome)
            dest, motivo = O.destinatari(azienda) if azienda else ([], "ordine senza scuola in HubSpot")
            a = [m for m, _ in dest]
            etichetta = "%s · %s · ordine %s" % (corso[:40], scuola_nome(azienda) if azienda else "?", ordine)
            if not a and not PROVA_A:
                senza_dest.append("%s (%s)" % (etichetta, motivo))
                continue
            testo, html = corpo_mail(corso, quando, ordine, scuola_nome(azienda) if azienda else "", ok, sotto, assenti, tardi, ore_corso)
            oggetto = "Attestati di partecipazione: %s" % corso
            pronte += 1
            print("  %s -> %s | attestati %d · sotto soglia %d · non collegati %d%s" % (etichetta, ", ".join(a) or "(nessuno)", len(ok), len(sotto), len(assenti), " [scuse]" if tardi else ""))
            if PROVA_A:
                spedisci(PROVA_A, "[PROVA] " + oggetto, testo, html)
                print("  prova inviata a", PROVA_A)
                return
            if not AUTOMATICO:
                continue
            ora = adesso.strftime("%Y-%m-%dT%H:%M:%SZ")
            for x in gente:
                P.hs("/crm/v3/objects/%s/%s" % (REG, x["id"]), {"properties": {"attestato_scuola_inviato": ora}}, "PATCH")
            try:
                spedisci(a, oggetto, testo, html)
                inviate.append(etichetta)
            except Exception as ex:
                for x in gente:
                    P.hs("/crm/v3/objects/%s/%s" % (REG, x["id"]), {"properties": {"attestato_scuola_inviato": ""}}, "PATCH")
                errori.append("%s: %s" % (etichetta, str(ex)[:80]))
    for v in senza_dest:
        print("  SENZA DESTINATARIO:", v)
    print("riepiloghi alle scuole: pronti %d · inviati %d · senza destinatario %d · errori %d" % (pronte, len(inviate), len(senza_dest), len(errori)))
    if AUTOMATICO and not PROVA_A and (inviate or errori):
        oggi = adesso.strftime("%Y-%m-%d")
        att = len(cerca(REG, {"filterGroups": [{"filters": [{"propertyName": "attestato_inviato", "operator": "GTE", "value": oggi + "T00:00:00Z"}]}], "properties": ["email"]}))
        righe = ["Attestati dei corsi, riepilogo del %s." % oggi, "", "Attestati inviati ai partecipanti oggi: %d." % att, "Riepiloghi inviati alle scuole: %d." % len(inviate)]
        for t, lst in (("Riepiloghi inviati", inviate), ("Senza destinatario (da decidere a mano)", senza_dest), ("Errori", errori)):
            if lst:
                righe += ["", "%s (%d):" % (t, len(lst))] + ["- " + v for v in lst]
        t = "\n".join(righe)
        try:
            spedisci(RIEPILOGO_A, "Attestati dei corsi · riepilogo %s" % oggi, t, '<pre style="font:14px/1.5 Arial,sans-serif;white-space:pre-wrap">%s</pre>' % esc(t))
        except Exception as ex:
            print("riepilogo non inviato:", str(ex)[:80])


if __name__ == "__main__":
    main()
