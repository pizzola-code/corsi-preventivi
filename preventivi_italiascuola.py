# -*- coding: utf-8 -*-
"""Da richiesta a preventivo: corsi Italiascuola (landing spaggiari.eu/corsi-italiascuola).

Motore GEMELLO di preventivi_corsi.py, separato apposta: il motore dei corsi Spaggiari gira ogni minuto in
produzione e non va toccato. Qui si riusano i suoi strumenti (hs, lega, figli, dati_scuola, agente_di_zona...) ma
cambiano il modulo (form Italiascuola 80432743-...), il listino (SKU completo nella riga, quantita' di partecipanti,
nessuno sconto), i testi, il mittente e il registro (inviati_italiascuola.txt).

STATO: ATTIVO dal 16/10/2026 (Andrea), dopo un collaudo con una richiesta vera: workflow preventivi-italiascuola.yml,
battito ogni minuto da cron-job.org (workflow_dispatch) e cron interno ogni 10 minuti come rete di sicurezza.

Valori PROVVISORI da confermare (cercare «DA CONFERMARE»):
  - pipeline e stadi: si usa Formazione come per i corsi Spaggiari, trattativa «Italiascuola N - scuola»
  - proprietario: agente di zona, altrimenti turno Maestri/Benassi/Tonelli (come i corsi Spaggiari)
  - testi IVA / modalita' d'acquisto / ragione sociale in preventivo (distribuzione Spaggiari di corsi Italiascuola)
  - mittente: no_reply@spaggiari.eu col nome «Italiascuola.it»; risposte a info@italiascuola.it

Riga del carrello (scritta dalla landing):
  <Titolo> — <Modalita'> · <n> partecipante/i[ xQ] — <totale> EUR[ — <quando>] — cod. <SKU completo>
Il titolo puo' contenere lo stesso trattino lungo che separa i campi (corsi in presenza: «Tema — Citta'»): il titolo
e' tutto cio' che precede la modalita' (Diretta live / Registrazione / Illimitato / Iscrizione).

Uso:  python preventivi_italiascuola.py            lavora le richieste nuove
      python preventivi_italiascuola.py --prova    elenca cosa farebbe, senza fare nulla
      python preventivi_italiascuola.py --test     prova il parser e i testi, senza rete
"""
import datetime
import io
import json
import os
import re
import sys
import time
import urllib.request
import uuid

QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, QUI)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if "--test" in sys.argv:
    os.environ.setdefault("HUBSPOT_TOKEN", "test")
import preventivi_corsi as C                                  # noqa: E402
from email.message import EmailMessage                        # noqa: E402
import smtplib                                                # noqa: E402
import ssl                                                    # noqa: E402

MODULO = "80432743-422f-405f-83bf-df4184a4ee4c"              # form Italiascuola (non quello dei corsi Spaggiari)
REGISTRO = os.path.join(QUI, "inviati_italiascuola.txt")
MITTENTE = "no_reply@spaggiari.eu"
NOME_MITTENTE = "Italiascuola.it"
RISPOSTE = "info@italiascuola.it"
SEP = C.SEP
VERDE = "#00733a"
# Il motore nasce con la landing: tutto cio' che e' arrivato prima (prove) non si lavora.
DA_QUANDO = 1792108800000                                     # 16/10/2026 00:00 UTC

# Pipeline separata dai corsi Spaggiari (16/10/2026): report, solleciti e chiusure dei corsi Spaggiari non la vedono.
PIPELINE_IS = "4229750005"
STADIO_RICHIESTA_IS = "6238864575"       # Richiesta ricevuta
STADIO_INVIATO_IS = "6238864576"         # Preventivo inviato

MODALITA = re.compile(r"^(Diretta live|Registrazione|Illimitato|Iscrizione)\b", re.I)

def carica_negozio():
    try:
        d = json.load(io.open(os.path.join(QUI, "shop_scuole.json"), encoding="utf-8"))
        return d.get("home") or "https://www.scuolashop.it/scuole/", d.get("prodotti") or {}
    except Exception:
        return "https://www.scuolashop.it/scuole/", {}


NEGOZIO_HOME, NEGOZIO = carica_negozio()


def disponibilita(righe):
    """Divide le righe del preventivo: ordinabili sullo shop (con il link alla pagina) e non ancora ordinabili."""
    disp, man = [], []
    for r in righe:
        u = NEGOZIO.get(r["codice"])
        (disp if u else man).append((r, u))
    return disp, man


def titolo_riga(r):
    return r["corso"] + ((" (" + r["quando"] + ")") if r["quando"] and not r["licenza"].lower().startswith("registr") else "")


def nota_preventivo(righe):
    """Testo «Come ordinare» scritto sul preventivo (non si modifica piu' dopo la pubblicazione)."""
    disp, man = disponibilita(righe)
    h = ("<b>Come ordinare su ScuolaShop</b><br>"
         '1. Accedi al canale Scuole di ScuolaShop: <a href="%s">www.scuolashop.it/scuole</a>. '
         "Se la scuola non è ancora registrata, si registra dalla stessa pagina.<br>"
         "2. Cerca i codici articolo di questo preventivo, oppure apri i collegamenti qui sotto, e aggiungili al carrello.<br>"
         "3. Per ogni partecipante indica nome, cognome, funzione ed e-mail professionale diretta, e scegli il bonifico differito.<br>"
         "Dopo l'ordine il link di accesso arriva a quegli indirizzi, senza altri passaggi; con la diretta il link vale anche per la registrazione.<br>" % NEGOZIO_HOME)
    if disp:
        h += "<br><b>Apri i corsi sullo shop</b><br>" + "<br>".join(
            '%s: <a href="%s">%s</a>' % (r["codice"], u, titolo_riga(r)) for r, u in disp) + "<br>"
    if man:
        h += ("<br>Per %s l'ordine passa da noi: scrivi a %s e ti aiutiamo."
              % (", ".join(r["codice"] for r, _ in man), RISPOSTE))
    return h


def blocco_ordine_email(righe):
    """Blocco «Come ordinare» dell'e-mail: pulsante verso lo shop e collegamenti ai singoli corsi."""
    disp, man = disponibilita(righe)
    h = ('<div style="background:#f2f7f6;border-left:3px solid %s;padding:12px 14px"><b>Come ordinare su ScuolaShop</b>'
         '<ol style="margin:6px 0 0;padding-left:20px">'
         '<li>Accedi al canale Scuole di ScuolaShop (se la scuola non è ancora registrata, si registra dalla stessa pagina).</li>'
         '<li>Cerca i codici articolo del preventivo, oppure apri i corsi dai collegamenti qui sotto, e aggiungili al carrello.</li>'
         '<li>Per ogni partecipante indica nome, cognome, funzione ed e-mail professionale diretta, e scegli il bonifico differito.</li></ol>'
         '<p style="margin:8px 0 0">Dopo l&rsquo;ordine il link di accesso arriva a quegli indirizzi, senza altri passaggi. '
         'Con la diretta il link vale anche per la registrazione.</p>'
         '<p style="margin:14px 0 4px"><a href="%s" style="background:#e8b547;color:%s;text-decoration:none;font-weight:700;'
         'padding:12px 20px;border-radius:10px;display:inline-block">ORDINA SU SCUOLASHOP</a></p>' % (VERDE, NEGOZIO_HOME, VERDE))
    if disp:
        h += '<p style="margin:10px 0 0">' + "<br>".join(
            '<a href="%s" style="color:%s">%s</a> &middot; cod. %s' % (u, VERDE, titolo_riga(r), r["codice"]) for r, u in disp) + "</p>"
    if man:
        h += ('<p style="margin:10px 0 0">Per %s l&rsquo;ordine passa da noi: scrivi a %s e ti aiutiamo.</p>'
              % (", ".join(r["codice"] for r, _ in man), RISPOSTE))
    return h + "</div>"


CONDIZIONI = ("Offerta valida 30 giorni. I corsi sono erogati da Italiascuola.it e distribuiti da "
              "Gruppo Spaggiari Parma S.p.A. Importi in euro; il regime IVA applicabile è indicato in fattura.")  # DA CONFERMARE


def righe_is(testo):
    fuori = []
    for riga in (testo or "").split("\n"):
        if not riga.strip():
            continue
        p = [x.strip() for x in riga.split(SEP)]
        i = next((k for k, x in enumerate(p) if k > 0 and MODALITA.match(x)), None)
        if i is None:
            continue
        v = {"corso": SEP.join(p[:i]), "licenza": p[i], "qta": 1, "totale": 0.0, "quando": "", "codice": ""}
        m = re.search(r"\sx(\d+)$", v["licenza"])
        if m:
            v["qta"] = max(1, int(m.group(1)))
            v["licenza"] = v["licenza"][:m.start()]
        for pezzo in p[i + 1:]:
            b = pezzo.lower()
            if b.endswith("eur"):
                v["totale"] = float(pezzo.split()[0].replace(",", "."))
            elif b.startswith("cod. "):
                v["codice"] = pezzo[5:].strip()
            else:
                v["quando"] = pezzo
        v["unitario"] = round(v["totale"] / v["qta"], 2) if v["qta"] else v["totale"]
        fuori.append(v)
    return fuori


def corpo_email(d):
    righe = "".join(
        '<tr><td style="padding:10px 12px;border-bottom:1px solid #e3e9e8"><b style="color:#0f2b2c">%s</b><br>'
        '<span style="color:#6d817f;font-size:13px">%s%s &middot; cod. %s</span></td>'
        '<td style="padding:10px 12px;border-bottom:1px solid #e3e9e8;text-align:right;white-space:nowrap;'
        'color:#0f2b2c">%s</td></tr>'
        % (r["corso"], (r["quando"] + " &middot; ") if r["quando"] and not r["licenza"].lower().startswith("registr") else "",
           r["licenza"] + (" &times; %d" % r["qta"] if r["qta"] > 1 else ""), r["codice"], C.euro(r["totale"]))
        for r in d["righe"])
    return """<div style="font:15px/1.6 Arial,sans-serif;color:#3f5453;max-width:660px">
<p>Gentile %(nome)s,</p>
<p>Le inviamo in allegato il <b>preventivo n. %(numero)s</b> intestato a %(scuola)s, valido 30 giorni.</p>
<table style="border-collapse:collapse;width:100%%;font:14px/1.5 Arial,sans-serif;margin:18px 0">%(righe)s
<tr><td style="padding:12px;font-weight:700;color:%(p)s">Totale</td>
<td style="padding:12px;text-align:right;font-weight:700;font-size:17px;color:%(p)s">%(tot)s</td></tr></table>
%(ordine)s
<p style="margin:14px 0 0;color:#6d817f;font-size:13px">I corsi sono erogati da Italiascuola.it e distribuiti da Spaggiari.</p>
<p>Per qualsiasi domanda pu&ograve; rispondere a questa e-mail o scrivere a %(ris)s.</p>
<p>Cordiali saluti</p>
<p style="margin:26px 0"><a href="%(link)s" style="background:#e8b547;color:%(p)s;text-decoration:none;font-weight:700;
padding:13px 22px;border-radius:10px;display:inline-block">APRI IL PREVENTIVO</a></p>
<p style="color:#6d817f;font-size:13px;margin-top:26px">Italiascuola.it &middot; distribuito da Spaggiari</p></div>""" % {
        "nome": d["nome"], "numero": d["numero"], "scuola": d["scuola"], "righe": righe, "tot": C.euro(d["totale"]),
        "link": d["link"], "ris": RISPOSTE, "p": VERDE, "ordine": blocco_ordine_email(d["righe"])}


def invia_is(a, oggetto, html, allegato):
    utente, chiave = os.environ.get("SMTP_CORSI_USER"), os.environ.get("SMTP_CORSI_PASS")
    if not (utente and chiave):
        raise RuntimeError("credenziali SMTP assenti")           # niente ripiego sulla casella personale
    m = EmailMessage()
    m["From"] = "%s <%s>" % (NOME_MITTENTE, MITTENTE)
    m["To"] = a
    m["Reply-To"] = RISPOSTE
    m["Subject"] = oggetto
    m.set_content("Il preventivo e' in allegato. Per leggerlo serve un lettore di posta che mostri i messaggi in HTML.")
    m.add_alternative(html, subtype="html")
    nome, dati = allegato
    m.add_attachment(dati, maintype="application", subtype="pdf", filename=nome)
    s = smtplib.SMTP(C.SMTP_HOST, C.SMTP_PORTA, timeout=60)
    s.starttls(context=ssl.create_default_context())
    s.login(utente, chiave)
    s.send_message(m)
    s.quit()


def avviso_is(scuola, responsabile, motivo, trattativa, n, totale):
    utente, chiave = os.environ.get("SMTP_CORSI_USER"), os.environ.get("SMTP_CORSI_PASS")
    if not (utente and chiave):
        return
    nome = "nessuno"
    if responsabile:
        o = C.hs("/crm/v3/owners/%s?idProperty=id" % responsabile)
        nome = ("%s %s" % (o.get("firstName") or "", o.get("lastName") or "")).strip() or responsabile
    m = EmailMessage()
    m["From"] = "%s <%s>" % (NOME_MITTENTE, MITTENTE)
    m["To"] = C.CONTROLLO
    m["Subject"] = "Italiascuola corsi: %s -> %s" % (scuola, nome)
    m.set_content("Richiesta preventivo corsi Italiascuola da %s (%d corsi, %s).\n\nTrattativa assegnata a: %s\nPerche': %s\n\n"
                  "Trattativa: https://app-eu1.hubspot.com/contacts/144406271/record/0-3/%s\n"
                  % (scuola, n, C.euro(totale), nome, motivo, trattativa))
    s = smtplib.SMTP(C.SMTP_HOST, C.SMTP_PORTA, timeout=60)
    s.starttls(context=ssl.create_default_context())
    s.login(utente, chiave)
    s.send_message(m)
    s.quit()


def registro():
    try:
        return {r.strip() for r in io.open(REGISTRO, encoding="utf-8") if r.strip()}
    except FileNotFoundError:
        return set()


def registra(chiave):
    with io.open(REGISTRO, "a", encoding="utf-8") as f:
        f.write(C.impronta(chiave) + "\n")


def stato_richiesta(chiave):
    if C.impronta(chiave) in registro():
        return "fatta", None
    r = C.hs("/crm/v3/objects/deals/search", {"filterGroups": [{"filters": [
        {"propertyName": "chiave_richiesta_corsi", "operator": "EQ", "value": chiave}]}],
        "properties": ["preventivo_inviato_il"], "limit": 1}, "POST")
    if r.get("results"):
        t = r["results"][0]
        return ("fatta", None) if t["properties"].get("preventivo_inviato_il") else ("a_meta", t["id"])
    c = C.hs("/crm/v3/objects/contacts/search", {"filterGroups": [{"filters": [
        {"propertyName": "ultimo_preventivo_corsi", "operator": "EQ", "value": chiave}]}], "limit": 1}, "POST")
    return ("fatta", None) if c.get("results") else ("nuova", None)


def lavora(inv, prova):
    v = {c["name"]: c["value"] for c in inv["values"]}
    quando = datetime.datetime.fromtimestamp(inv["submittedAt"] / 1000)
    chiave = "IS|%s|%s" % (v.get("email", ""), inv["submittedAt"])
    # persone fisiche (Carta del Docente, PayPal...): senza scuola il preventivo e' intestato alla persona
    nome_persona = " ".join(x for x in (v.get("firstname"), v.get("lastname")) if x).strip()
    scuola = (v.get("scuola") or nome_persona or "Persona fisica").strip()
    v["scuola"] = scuola
    righe = righe_is(v.get("corsi_richiesti"))
    if not righe or not v.get("email"):
        print("  salto (richiesta senza righe o senza email)")
        return
    if re.match(r"PROVA\b", scuola.upper()):
        print("  salto una prova")
        return
    stato, ripresa = stato_richiesta(chiave)
    if stato == "fatta":
        return
    totale = round(sum(r["totale"] for r in righe), 2)
    print("\n%s  richiesta Italiascuola %s - %d corsi, %s" % (quando.strftime("%d/%m %H:%M"), C.mascherato(v.get("email")),
                                                          len(righe), C.euro(totale)))
    if prova:
        for r in righe:
            print("    %s | %s x%d | %s | %s" % (r["corso"][:40], r["licenza"], r["qta"], C.euro(r["totale"]), r["codice"]))
        print("  --prova: mi fermo qui")
        return

    cerca = C.hs("/crm/v3/objects/contacts/search", {"filterGroups": [{"filters": [
        {"propertyName": "email", "operator": "EQ", "value": v["email"]}]}],
        "properties": ["associatedcompanyid", "codice_meccanografico", "codice_cliente", "address", "city", "zip", "state",
                       "hubspot_owner_id"], "limit": 1}, "POST")
    dati_contatto = cerca["results"][0]["properties"] if cerca.get("results") else {}
    contatto = cerca["results"][0]["id"] if cerca.get("results") else None
    azienda = dati_contatto.get("associatedcompanyid")
    dest = C.dati_scuola(v, azienda, dati_contatto)
    agente, scuola_id = C.agente_di_zona(azienda, v.get("codice_meccanografico") or dati_contatto.get("codice_meccanografico"))
    if not agente:
        agente, motivo = C.ripiego(), "scuola senza agente di zona attivo: a turno"
    else:
        motivo = "agente di zona della scuola"

    if ripresa:
        trattativa = ripresa
    else:
        corpo = ("Richiesta dalla landing corsi Italiascuola.\n\nCorsi richiesti (%d):\n%s\n\nTotale: %s euro\n\n"
                 "Chi scrive: %s %s - %s\nTelefono: %s\nE-mail: %s\n\nNote: %s"
                 % (len(righe), v.get("corsi_richiesti") or "", totale, v.get("firstname", ""), v.get("lastname", ""),
                    v.get("ruolo", ""), v.get("mobilephone", ""), v.get("email", ""), v.get("message", "")))
        d = C.hs("/crm/v3/objects/deals", {"properties": {
            "dealname": ("Italiascuola %d - %s" % (len(righe), scuola))[:200], "pipeline": PIPELINE_IS,
            "dealstage": STADIO_RICHIESTA_IS, "amount": str(totale), "hubspot_owner_id": agente or "",
            "description": corpo[:60000], "chiave_richiesta_corsi": chiave}}, "POST")
        if "_err" in d:
            print("  trattativa NON creata:", d["_msg"])
            return
        trattativa = d["id"]
    responsabile = (C.hs("/crm/v3/objects/deals/%s?properties=hubspot_owner_id" % trattativa)
                    .get("properties", {}).get("hubspot_owner_id") or "")

    ids = C.figli(trattativa, "line_items") if ripresa else []
    for r in (righe if not ids else []):
        li = C.hs("/crm/v3/objects/line_items", {"properties": {
            "name": " — ".join(x for x in (r["corso"], r["licenza"], r["quando"]) if x),
            "hs_sku": r["codice"], "price": str(r["unitario"]), "quantity": str(r["qta"]),
            "description": "Corso Italiascuola - %s" % r["licenza"]}}, "POST")
        if "_err" in li:
            print("  riga NON creata:", li["_msg"])
            continue
        ids.append(li["id"])
        C.lega("line_items", li["id"], "deals", trattativa)

    prev_esistente = (C.figli(trattativa, "quotes") or [None])[0] if ripresa else None
    scadenza = int((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)).timestamp() * 1000)
    q = {"id": prev_esistente} if prev_esistente else C.hs("/crm/v3/objects/quotes", {"properties": {
        "hs_title": "Corsi di formazione Italiascuola - %s" % scuola,
        "hs_expiration_date": str(scadenza), "hs_status": "DRAFT",
        "hs_language": "it", "hs_locale": "it-IT", "hs_currency": "EUR",
        "hs_comments": nota_preventivo(righe), "hs_terms": CONDIZIONI,
        "hs_sender_company_name": "Gruppo Spaggiari Parma S.p.A.",                    # DA CONFERMARE (distributore)
        "hs_sender_company_address": "Via Bernini 22/A", "hs_sender_company_city": "Parma",
        "hs_sender_company_zip": "43126", "hs_sender_company_state": "PR", "hs_sender_company_country": "Italia",
        "hs_sender_company_domain": "spaggiari.eu",
        "hs_sender_firstname": "Nicola", "hs_sender_lastname": "de Cesare", "hs_sender_jobtitle": "Amministratore Delegato",
        # l'impaginazione «corsi» del modello si attiva da questo indirizzo: va lasciato cosi' finche' il modello non cambia
        "hs_sender_email": C.MEPA,
        "spg_dest_nome": dest["nome"], "spg_dest_indirizzo": dest["indirizzo"], "spg_codice_cliente": dest["codice_cliente"],
        "spg_codice_meccanografico": dest["meccanografico"], "spg_codice_fiscale": dest["codice_fiscale"],
        "spg_piva": dest["piva"]}}, "POST")
    if "_err" in q:
        print("  preventivo NON creato:", q["_msg"])
        return
    prev = q["id"]
    for tipo, ident in ([] if prev_esistente else (("quote_template", C.MODELLO), ("deals", trattativa))):
        C.lega("quotes", prev, tipo, ident)
    for i in (ids if not prev_esistente else []):
        C.lega("quotes", prev, "line_items", i)
    if contatto:
        C.lega("quotes", prev, "contacts", contatto)
        C.lega("deals", trattativa, "contacts", contatto)
        if azienda or scuola_id:
            C.lega("quotes", prev, "companies", azienda or scuola_id)
            C.lega("deals", trattativa, "companies", azienda or scuola_id)

    gia_online = prev_esistente and C.hs("/crm/v3/objects/quotes/%s?properties=hs_quote_link" % prev
                                         ).get("properties", {}).get("hs_quote_link")
    if not gia_online:
        r = C.hs("/crm/v3/objects/quotes/%s" % prev, {"properties": {
            "hs_slug": uuid.uuid4().hex[:20], "hs_domain": C.DOMINIO, "hubspot_owner_id": responsabile,
            "hs_status": "APPROVAL_NOT_NEEDED"}}, "PATCH")
        if "_err" in r:
            print("  preventivo NON pubblicato:", r["_msg"])
            return
    dati = {}
    for _ in range(12):
        p = C.hs("/crm/v3/objects/quotes/%s?properties=hs_quote_link,hs_quote_number,hs_pdf_download_link,"
                 "hs_pdf_generation_status" % prev).get("properties", {})
        if p.get("hs_quote_link") and p.get("hs_pdf_generation_status") == "PDF_GENERATED":
            dati = p
            break
        time.sleep(5)
    if not dati:
        print("  preventivo creato ma PDF non pronto: email non inviata")
        return
    try:
        pdf = urllib.request.urlopen(urllib.request.Request(dati["hs_pdf_download_link"], headers={"User-Agent": "Mozilla/5.0"}),
                                     timeout=120).read()
    except Exception as e:
        print("  PDF non scaricato (%s): riprovo al prossimo giro" % type(e).__name__)
        return
    testo = corpo_email({"nome": " ".join(x for x in (v.get("firstname"), v.get("lastname")) if x).strip() or "Dirigente",
                         "scuola": scuola, "numero": dati["hs_quote_number"], "link": dati["hs_quote_link"],
                         "righe": righe, "totale": totale})
    try:
        invia_is(v["email"], "Preventivo n. %s · Corsi di formazione Italiascuola" % dati["hs_quote_number"], testo,
                 ("Preventivo-%s.pdf" % dati["hs_quote_number"], pdf))
    except Exception as e:
        print("  email NON partita (%s): riprovo al prossimo giro" % type(e).__name__)
        return
    registra(chiave)
    ora = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    C.hs("/crm/v3/objects/deals/%s" % trattativa, {"properties": {"dealstage": STADIO_INVIATO_IS, "preventivo_inviato_il": ora}}, "PATCH")
    if contatto:
        C.hs("/crm/v3/objects/contacts/%s" % contatto, {"properties": {"ultimo_preventivo_corsi": chiave}}, "PATCH")
    print("  preventivo %s inviato a %s" % (dati["hs_quote_number"], C.mascherato(v["email"])))
    try:
        avviso_is(scuola, responsabile, motivo, trattativa, len(righe), totale)
    except Exception as e:
        print("  avviso di controllo non inviato (%s)" % type(e).__name__)


def test():
    ok = 0
    riga = ("Gestione Incarichi — Firenze — Iscrizione · 1 partecipante — 125 EUR — venerdì 23 ottobre 2026 — cod. ITACNES/10\n"
            "Rendicontazione PNRR — Diretta live · 1 partecipante x3 — 270 EUR — giovedì 22 ottobre 2026 — cod. WBRITARUS1-1\n"
            "Lavoro a distanza nella scuola — Illimitato · tutta la scuola — 394 EUR — lunedì 19 ottobre 2026 — cod. WBRITALDS1-I\n"
            "Cessazioni — Registrazione · 1 partecipante x2 — 180 EUR — Registrazione disponibile subito — cod. WBRITACSS3-1R")
    r = righe_is(riga)
    assert len(r) == 4, r
    assert r[0]["corso"] == "Gestione Incarichi — Firenze" and r[0]["codice"] == "ITACNES/10" and r[0]["qta"] == 1
    assert r[1]["qta"] == 3 and r[1]["unitario"] == 90.0 and r[1]["licenza"].startswith("Diretta live")
    assert r[2]["licenza"].startswith("Illimitato") and r[2]["totale"] == 394.0
    assert r[3]["qta"] == 2 and r[3]["unitario"] == 90.0
    html = corpo_email({"nome": "Mario Rossi", "scuola": "IC Prova", "numero": "123", "link": "https://x", "righe": r,
                        "totale": sum(x["totale"] for x in r)})
    totale = sum(x["totale"] for x in r)
    assert totale == 969.0, totale
    assert "ITACNES/10" in html and "&times; 3" in html and "969,00" in html.replace(" ", " "), html[:300]
    assert "Registrazione disponibile subito" not in html          # la data non si stampa sulle registrazioni
    # ordine su ScuolaShop: due codici con pagina nel canale Scuole e uno senza
    campione = righe_is("Palestre e impianti sportivi — Registrazione · per partecipante x2 — 180 EUR — cod. WBRITAPCB2-1R\n"
                        "Gestione Incarichi — Firenze — Iscrizione · per partecipante — 125 EUR — venerdì 23 ottobre 2026 — cod. ITACNES/10\n"
                        "Corso inventato — Registrazione · per partecipante — 90 EUR — cod. WBRITAZZZ9-1R")
    disp, man = disponibilita(campione)
    assert [r["codice"] for r, _ in disp] == ["WBRITAPCB2-1R", "ITACNES/10"] and [r["codice"] for r, _ in man] == ["WBRITAZZZ9-1R"], (disp, man)
    nota = nota_preventivo(campione)
    assert "www.scuolashop.it/scuole" in nota and "WBRITAPCB2-1R:" in nota and "Per WBRITAZZZ9-1R l'ordine passa da noi" in nota, nota
    assert "bonifico differito" in nota and "e-mail professionale diretta" in nota and "anche per la registrazione" in nota
    mail = blocco_ordine_email(campione)
    assert "ORDINA SU SCUOLASHOP" in mail and mail.count("<a href=") == 3 and "WBRITAZZZ9-1R" in mail, mail
    assert all(u.startswith("https://www.scuolashop.it/scuole/") for _, u in disp)
    print("test ok: 4 righe, quantita', trattino nel titolo, email, note e link ScuolaShop (2 ordinabili, 1 da noi)")


if __name__ == "__main__":
    if "--test" in sys.argv:
        test()
        sys.exit(0)
    prova = "--prova" in sys.argv
    soglia = max(int((datetime.datetime.now() - datetime.timedelta(hours=C.ORE_INDIETRO)).timestamp() * 1000), DA_QUANDO)
    nuovi, dopo = [], None
    for _ in range(40):
        pagina = C.hs("/form-integrations/v1/submissions/forms/%s?limit=50%s" % (MODULO, "&after=" + dopo if dopo else ""))
        ris = pagina.get("results", [])
        nuovi += [x for x in ris if x["submittedAt"] >= soglia]
        dopo = (pagina.get("paging") or {}).get("next", {}).get("after")
        if not dopo or not ris or ris[-1]["submittedAt"] < soglia:
            break
    print("richieste Italiascuola nelle ultime %d ore: %d" % (C.ORE_INDIETRO, len(nuovi)))
    errori = 0
    for inv in reversed(nuovi):
        try:
            lavora(inv, prova)
        except Exception as e:
            errori += 1
            print("  ERRORE su una richiesta (%s: %s): la riprendo al prossimo giro" % (type(e).__name__, str(e)[:160]))
    if errori:
        print("\nrichieste con errore in questo giro: %d" % errori)
    print("\nfatto.")
