# -*- coding: utf-8 -*-
"""Da richiesta a preventivo: il motore della pagina «Comfort scolastico».

Stesso schema del catalogo corsi (preventivi_corsi.py, di cui riusa le funzioni), con tre
differenze decise da Andrea il 1/10/2026:

  · il preventivo NON va alla scuola: va all'AGENTE di zona e all'Ufficio Preventivi
    (preventivi@spaggiari.eu), e basta. La scuola la ricontatta l'agente;
  · la strada d'acquisto non e' scritta: la concorda l'agente con la scuola;
  · senza agente di zona la trattativa va a turno a Tonelli o Zecca (chi ha meno aperte);
    Andrea Pizzola e Laura Primiceri ricevono un avviso su ogni richiesta (visibilita' su tutto).

Per ogni richiesta del modulo:
  1. apre la trattativa nella pipeline «Comfort scolastico» (nome «Comfort N pz - scuola»);
  2. crea le righe (codice articolo, quantita', prezzo IVA esclusa);
  3. genera il preventivo numerato e il PDF;
  4. manda PDF + riepilogo all'agente e all'Ufficio Preventivi;
  5. porta la trattativa a «Preventivo emesso» e crea il task di richiamata per l'agente;
  6. avvisa Andrea e Laura.

Una scuola il cui nome comincia per PROVA e' una prova: la trattativa si apre (e va cancellata
a mano), ma tutto arriva solo ad Andrea Pizzola.

Uso:  python preventivi_comfort.py            lavora le richieste nuove
      python preventivi_comfort.py --prova    dice cosa farebbe, senza fare nulla
"""
import datetime
import io
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.request
import uuid
from email.message import EmailMessage

QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, QUI)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import preventivi_corsi as C                                  # noqa: E402

hs, lega, euro, figli = C.hs, C.lega, C.euro, C.figli

MODULO = "9ced884d-a74f-449e-9bb4-201c5d8cbc5d"
PIPELINE = "4199356649"                  # Comfort scolastico
STADIO_RICHIESTA = "6168570058"          # Richiesta ricevuta
STADIO_EMESSO = "6168570059"             # Preventivo emesso
UFFICIO = "preventivi@spaggiari.eu"
RIPIEGO = [("78283682", "Alessandro Tonelli"), ("35980393", "Emma Zecca")]
CONTROLLO_ID = ["1713490776", "37524294"]   # Andrea Pizzola, Laura Primiceri: indirizzi dal CRM, non nel codice
PROVA_PROPRIETARIO = "1713490776"        # Andrea Pizzola, per le prove
MARCATORE = "chiave_richiesta_comfort"
ORE_INDIETRO = 24
# il motore nasce il 1/10/2026: cio' che e' arrivato prima e' collaudo. Non si sposta piu'.
DA_QUANDO = int(os.environ.get("COMFORT_DA_QUANDO", "1790844794000"))   # 01/10/2026 ~10:33, lancio
REGISTRO = os.path.join(QUI, "inviati_comfort.txt")

NOTE = ("Importi in euro, IVA esclusa. Modalità d’acquisto, consegna, installazione e pagamento "
        "si concordano con l’agente di riferimento. Per qualsiasi domanda: " + UFFICIO + ".")
CONDIZIONI = "Offerta valida fino al 31 dicembre 2026, come la promozione Comfort scolastico. Importi in euro, IVA esclusa."


def impronta(chiave):
    return C.impronta(chiave)


def registro():
    try:
        return {r.strip() for r in io.open(REGISTRO, encoding="utf-8") if r.strip()}
    except FileNotFoundError:
        return set()


def registra(chiave):
    with io.open(REGISTRO, "a", encoding="utf-8") as f:
        f.write(impronta(chiave) + "\n")


RIGA = re.compile(r"^(\S+) — (.+) — (\d+) × ([\d.]+) EUR")


def righe_da(testo):
    """Le righe della lista: «ZRF001 — nome — 3 × 169.58 EUR = 508.74 EUR»."""
    fuori = []
    for riga in (testo or "").split("\n"):
        m = RIGA.match(riga.strip())
        if m:
            fuori.append({"codice": m.group(1), "nome": m.group(2), "qta": int(m.group(3)),
                          "prezzo": float(m.group(4))})
    return fuori


def ripiego():
    carico = {}
    for oid, _ in RIPIEGO:
        r = hs("/crm/v3/objects/deals/search", {"filterGroups": [{"filters": [
            {"propertyName": "pipeline", "operator": "EQ", "value": PIPELINE},
            {"propertyName": "hubspot_owner_id", "operator": "EQ", "value": oid},
            {"propertyName": "hs_is_closed", "operator": "EQ", "value": "false"}]}],
            "limit": 1}, "POST")
        carico[oid] = r.get("total", 0)
    return min(RIPIEGO, key=lambda x: (carico.get(x[0], 0), RIPIEGO.index(x)))[0]


def proprietario(oid):
    o = hs("/crm/v3/owners/%s?idProperty=id" % oid)
    nome = ("%s %s" % (o.get("firstName") or "", o.get("lastName") or "")).strip()
    return nome or oid, o.get("email") or ""


def stato_richiesta(chiave):
    if impronta(chiave) in registro():
        return "fatta", None
    r = hs("/crm/v3/objects/deals/search", {"filterGroups": [{"filters": [
        {"propertyName": MARCATORE, "operator": "EQ", "value": chiave}]}],
        "properties": ["preventivo_inviato_il"], "limit": 1}, "POST")
    if r.get("results"):
        t = r["results"][0]
        return ("fatta", None) if t["properties"].get("preventivo_inviato_il") else ("a_meta", t["id"])
    return "nuova", None


def manda(a, cc, oggetto, html, allegato=None, reply_to=UFFICIO):
    utente = os.environ.get("SMTP_CORSI_USER")
    chiave = os.environ.get("SMTP_CORSI_PASS")
    if not (utente and chiave):
        raise RuntimeError("credenziali SMTP assenti")
    m = EmailMessage()
    m["From"] = "Spaggiari <%s>" % C.MITTENTE
    m["To"] = a
    if cc:
        m["Cc"] = cc
    m["Reply-To"] = reply_to
    m["Subject"] = oggetto
    m.set_content("Il riepilogo della richiesta e' in questo messaggio; serve un lettore di posta "
                  "che mostri i messaggi in HTML.")
    m.add_alternative(html, subtype="html")
    if allegato:
        m.add_attachment(allegato[1], maintype="application", subtype="pdf", filename=allegato[0])
    s = smtplib.SMTP(C.SMTP_HOST, C.SMTP_PORTA, timeout=60)
    s.starttls(context=ssl.create_default_context())
    s.login(utente, chiave)
    s.send_message(m)
    s.quit()


def corpo_agente(d):
    righe = "".join(
        '<tr><td style="padding:9px 12px;border-bottom:1px solid #e3e9e8"><b style="color:#0f2b2c">%s</b>'
        '<br><span style="color:#6d817f;font-size:13px">cod. %s &middot; %d &times; %s</span></td>'
        '<td style="padding:9px 12px;border-bottom:1px solid #e3e9e8;text-align:right;white-space:nowrap">%s</td></tr>'
        % (r["nome"], r["codice"], r["qta"], euro(r["prezzo"]), euro(r["qta"] * r["prezzo"])) for r in d["righe"])
    ref = "<br>".join(x for x in (
        "<b>%s</b>%s" % (d["referente"], " &middot; %s" % d["ruolo"] if d["ruolo"] else ""),
        '<a href="mailto:%(e)s" style="color:%(p)s">%(e)s</a> &middot; %(t)s' % {"e": d["email"], "t": d["tel"], "p": C.PETROLIO}) if x)
    note = ('<p style="margin:12px 0 0"><b>Note della scuola</b><br>%s</p>' % d["note"]) if d["note"] else ""
    return """<div style="font:15px/1.6 Arial,sans-serif;color:#3f5453;max-width:660px">
<p>Nuova richiesta di preventivo dalla pagina <b>Comfort scolastico</b>.</p>
<div style="background:#f2f7f6;border-left:3px solid %(p)s;padding:12px 14px">
<b style="font-size:16px;color:#0f2b2c">%(scuola)s</b>%(mecc)s<br>%(ref)s%(note)s</div>
<table style="border-collapse:collapse;width:100%%;font:14px/1.5 Arial,sans-serif;margin:18px 0">%(righe)s
<tr><td style="padding:12px;font-weight:700;color:%(p)s">Totale, IVA esclusa</td>
<td style="padding:12px;text-align:right;font-weight:700;font-size:17px;color:%(p)s">%(tot)s</td></tr></table>
<p>In allegato il <b>preventivo n. %(numero)s</b>, intestato alla scuola. <b>La scuola non ha ricevuto nulla</b>:
la contatta l&rsquo;agente per concordare modalit&agrave; d&rsquo;acquisto, consegna e pagamento.</p>
<p style="color:#6d817f;font-size:13px">Assegnata a: %(chi)s (%(motivo)s).</p>
<p style="margin:22px 0"><a href="%(trat)s" style="background:%(o)s;color:%(p)s;text-decoration:none;font-weight:700;
padding:13px 22px;border-radius:10px;display:inline-block">APRI LA TRATTATIVA</a></p></div>""" % {
        "scuola": d["scuola"], "mecc": (" &middot; %s" % d["mecc"]) if d["mecc"] else "", "ref": ref, "note": note,
        "righe": righe, "tot": euro(d["netto"]), "numero": d["numero"], "chi": d["chi"], "motivo": d["motivo"],
        "trat": d["trattativa_link"], "p": C.PETROLIO, "o": C.ORO}


def lavora(inv, prova):
    v = {c["name"]: c["value"] for c in inv["values"]}
    quando = datetime.datetime.fromtimestamp(inv["submittedAt"] / 1000)
    chiave = "%s|%s" % (v.get("email", ""), inv["submittedAt"])
    scuola = (v.get("scuola") or "Scuola").strip()
    righe = righe_da(v.get("prodotti_comfort_richiesti"))
    if not righe or not v.get("email"):
        print("  salto (richiesta senza righe o senza email)")
        return
    di_prova = bool(re.match(r"PROVA\b", scuola.upper()))
    stato, ripresa = stato_richiesta(chiave)
    if stato == "fatta":
        return
    netto = round(sum(r["qta"] * r["prezzo"] for r in righe), 2)
    pezzi = sum(r["qta"] for r in righe)
    print("\n%s  %s <%s> - %s - %d pezzi, %s%s" % (quando.strftime("%d/%m %H:%M"), v.get("firstname"), v.get("email"),
                                                scuola, pezzi, euro(netto), "  [PROVA]" if di_prova else ""))
    if prova:
        print("  --prova: mi fermo qui")
        return

    cerca = hs("/crm/v3/objects/contacts/search", {"filterGroups": [{"filters": [
        {"propertyName": "email", "operator": "EQ", "value": v["email"]}]}],
        "properties": ["associatedcompanyid", "codice_meccanografico", "codice_cliente",
                       "address", "city", "zip", "state", "hubspot_owner_id"], "limit": 1}, "POST")
    dati_contatto = cerca["results"][0]["properties"] if cerca.get("results") else {}
    contatto = cerca["results"][0]["id"] if cerca.get("results") else None
    azienda = dati_contatto.get("associatedcompanyid")
    destinatario = C.dati_scuola(v, azienda, dati_contatto)
    mecc = v.get("codice_meccanografico") or dati_contatto.get("codice_meccanografico")
    if di_prova:
        agente, scuola_id, motivo = PROVA_PROPRIETARIO, azienda, "prova"
    else:
        agente, scuola_id = C.agente_di_zona(azienda, mecc)
        if not agente:
            agente, motivo = ripiego(), "scuola senza agente di zona: a turno Tonelli o Zecca"
        else:
            motivo = "agente di zona della scuola"
    print("  assegnata a %s (%s)" % (agente, motivo))

    if ripresa:
        trattativa = ripresa
        print("  riprendo la trattativa %s rimasta a meta'" % trattativa)
    else:
        corpo = ("Richiesta dalla pagina Comfort scolastico.\n\nProdotti richiesti (%d pezzi):\n%s\n\n"
                 "Totale IVA esclusa: %s\n\nChi scrive: %s %s - %s\nTelefono: %s\nE-mail: %s\n\nNote: %s"
                 % (pezzi, "\n".join("%s - %s - %d x %s" % (r["codice"], r["nome"], r["qta"], euro(r["prezzo"]))
                                     for r in righe), euro(netto), v.get("firstname", ""), v.get("lastname", ""),
                    v.get("ruolo", ""), v.get("mobilephone", ""), v.get("email", ""), v.get("message", "")))
        d = hs("/crm/v3/objects/deals", {"properties": {
            "dealname": ("Comfort %d pz - %s" % (pezzi, scuola))[:200], "pipeline": PIPELINE,
            "dealstage": STADIO_RICHIESTA, "amount": str(netto), "hubspot_owner_id": agente or "",
            "description": corpo[:60000], MARCATORE: chiave}}, "POST")
        if "_err" in d:
            print("  trattativa NON creata:", d["_msg"])
            return
        trattativa = d["id"]

    responsabile = (hs("/crm/v3/objects/deals/%s?properties=hubspot_owner_id" % trattativa)
                    .get("properties", {}).get("hubspot_owner_id") or "")
    ids = figli(trattativa, "line_items") if ripresa else []
    for r in (righe if not ids else []):
        li = hs("/crm/v3/objects/line_items", {"properties": {
            "name": r["nome"], "hs_sku": r["codice"], "price": str(r["prezzo"]),
            "quantity": str(r["qta"]), "description": "Comfort scolastico - IVA esclusa"}}, "POST")
        if "_err" in li:
            print("  riga NON creata:", li["_msg"])
            continue
        ids.append(li["id"])
        lega("line_items", li["id"], "deals", trattativa)

    prev_esistente = (figli(trattativa, "quotes") or [None])[0] if ripresa else None
    fine_anno = int(datetime.datetime(2026, 12, 31, 21, 59, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    scadenza = max(fine_anno, int((datetime.datetime.now(datetime.timezone.utc)
                                   + datetime.timedelta(days=7)).timestamp() * 1000))
    q = {"id": prev_esistente} if prev_esistente else hs("/crm/v3/objects/quotes", {"properties": {
        "hs_title": "Comfort scolastico Spaggiari - %s" % scuola,
        "hs_expiration_date": str(scadenza), "hs_status": "DRAFT",
        "hs_language": "it", "hs_locale": "it-IT", "hs_currency": "EUR",
        "hs_comments": NOTE, "hs_terms": CONDIZIONI,
        "hs_sender_company_name": "Gruppo Spaggiari Parma S.p.A.",
        "hs_sender_company_address": "Via Bernini 22/A", "hs_sender_company_city": "Parma",
        "hs_sender_company_zip": "43126", "hs_sender_company_state": "PR",
        "hs_sender_company_country": "Italia", "hs_sender_company_domain": "spaggiari.eu",
        "hs_sender_firstname": "Nicola", "hs_sender_lastname": "de Cesare",
        "hs_sender_jobtitle": "Amministratore Delegato", "hs_sender_email": UFFICIO,
        "spg_dest_nome": destinatario["nome"], "spg_dest_indirizzo": destinatario["indirizzo"],
        "spg_codice_cliente": destinatario["codice_cliente"],
        "spg_codice_meccanografico": destinatario["meccanografico"],
        "spg_codice_fiscale": destinatario["codice_fiscale"], "spg_piva": destinatario["piva"]}}, "POST")
    if "_err" in q:
        print("  preventivo NON creato:", q["_msg"])
        return
    prev = q["id"]
    for tipo, ident in ([] if prev_esistente else (("quote_template", C.MODELLO), ("deals", trattativa))):
        lega("quotes", prev, tipo, ident)
    for i in (ids if not prev_esistente else []):
        lega("quotes", prev, "line_items", i)
    if contatto:
        lega("quotes", prev, "contacts", contatto)
        lega("deals", trattativa, "contacts", contatto)
        if azienda or scuola_id:
            lega("quotes", prev, "companies", azienda or scuola_id)
            lega("deals", trattativa, "companies", azienda or scuola_id)

    gia_online = prev_esistente and hs("/crm/v3/objects/quotes/%s?properties=hs_quote_link" % prev
                                       ).get("properties", {}).get("hs_quote_link")
    if not gia_online:
        r = hs("/crm/v3/objects/quotes/%s" % prev, {"properties": {
            "hs_slug": uuid.uuid4().hex[:20], "hs_domain": C.DOMINIO,
            "hubspot_owner_id": responsabile, "hs_status": "APPROVAL_NOT_NEEDED"}}, "PATCH")
        if "_err" in r:
            print("  preventivo NON pubblicato:", r["_msg"])
            return
    dati = {}
    for _ in range(12):
        p = hs("/crm/v3/objects/quotes/%s?properties=hs_quote_link,hs_quote_number,"
               "hs_pdf_download_link,hs_pdf_generation_status" % prev).get("properties", {})
        if p.get("hs_quote_link") and p.get("hs_pdf_generation_status") == "PDF_GENERATED":
            dati = p
            break
        time.sleep(5)
    if not dati:
        print("  preventivo creato ma PDF non pronto: riprovo al prossimo giro")
        return
    try:
        pdf = urllib.request.urlopen(urllib.request.Request(
            dati["hs_pdf_download_link"], headers={"User-Agent": "Mozilla/5.0"}), timeout=120).read()
    except Exception as e:
        print("  PDF non scaricato (%s): riprovo al prossimo giro" % type(e).__name__)
        return

    nome_ag, email_ag = proprietario(responsabile) if responsabile else ("nessuno", "")
    link_trat = "https://app-eu1.hubspot.com/contacts/144406271/record/0-3/%s" % trattativa
    testo = corpo_agente({
        "scuola": scuola, "mecc": destinatario["meccanografico"],
        "referente": " ".join(x for x in (v.get("firstname"), v.get("lastname")) if x).strip() or "Referente",
        "ruolo": v.get("ruolo", ""), "email": v["email"], "tel": v.get("mobilephone", ""),
        "note": (v.get("message") or "").split("\n\nRichiesta preventivo")[0].strip(),
        "righe": righe, "netto": netto, "numero": dati["hs_quote_number"], "chi": nome_ag,
        "motivo": motivo, "trattativa_link": link_trat})
    if di_prova:
        a, cc = proprietario(PROVA_PROPRIETARIO)[1], None
    else:
        a = email_ag or UFFICIO
        cc = UFFICIO if email_ag and email_ag.lower() != UFFICIO else None
    try:
        manda(a, cc, "%sComfort scolastico · %s · preventivo n. %s" % ("[PROVA] " if di_prova else "", scuola, dati["hs_quote_number"]),
              testo, ("Preventivo-%s.pdf" % dati["hs_quote_number"], pdf))
    except Exception as e:
        print("  email NON partita (%s): riprovo al prossimo giro" % type(e).__name__)
        return
    registra(chiave)
    ora = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    hs("/crm/v3/objects/deals/%s" % trattativa, {"properties": {"dealstage": STADIO_EMESSO, "preventivo_inviato_il": ora}}, "PATCH")
    print("  preventivo %s mandato a %s%s" % (dati["hs_quote_number"], a, " (cc %s)" % cc if cc else ""))

    # il task di richiamata per l'agente: HubSpot lo notifica a chi lo riceve
    if responsabile:
        try:
            domani = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)).strftime("%Y-%m-%dT05:00:00Z")
            n = hs("/crm/v3/objects/tasks", {"properties": {
                "hs_task_subject": "Comfort scolastico: %s" % scuola,
                "hs_task_body": "Richiesta di preventivo dalla pagina Comfort scolastico (%d pezzi, %s IVA esclusa). "
                                "La scuola non ha ricevuto il preventivo: contattarla per concordare modalita' d'acquisto, "
                                "consegna e pagamento. Dettagli nella trattativa." % (pezzi, euro(netto)),
                "hs_task_type": "CALL", "hs_task_priority": "HIGH", "hs_task_status": "NOT_STARTED",
                "hubspot_owner_id": responsabile, "hs_timestamp": domani}}, "POST")
            if n.get("id"):
                lega("tasks", n["id"], "deals", trattativa)
                if contatto:
                    lega("tasks", n["id"], "contacts", contatto)
                print("  task di richiamata per %s" % nome_ag)
        except Exception as e:
            print("  task non creato (%s)" % type(e).__name__)
    # visibilita' su tutto: Andrea e Laura (non per le prove)
    if not di_prova:
        try:
            manda(", ".join(e for e in (proprietario(i)[1] for i in CONTROLLO_ID) if e), None, "Comfort: %s -> %s" % (scuola, nome_ag),
                  "<p>Richiesta Comfort scolastico da <b>%s</b> (%d pezzi, %s IVA esclusa).</p>"
                  "<p>Assegnata a: <b>%s</b> (%s).</p><p><a href=\"%s\">Trattativa</a></p>"
                  % (scuola, pezzi, euro(netto), nome_ag, motivo, link_trat))
        except Exception as e:
            print("  avviso di controllo non inviato (%s)" % type(e).__name__)


def main():
    prova = "--prova" in sys.argv
    da = int((datetime.datetime.now() - datetime.timedelta(hours=ORE_INDIETRO)).timestamp() * 1000)
    soglia = max(da, DA_QUANDO)
    nuovi, dopo = [], None
    for _ in range(20):
        pagina = hs("/form-integrations/v1/submissions/forms/%s?limit=50%s" % (MODULO, "&after=" + dopo if dopo else ""))
        risultati = pagina.get("results", [])
        nuovi += [x for x in risultati if x["submittedAt"] >= soglia]
        dopo = (pagina.get("paging") or {}).get("next", {}).get("after")
        if not dopo or not risultati or risultati[-1]["submittedAt"] < soglia:
            break
    print("richieste comfort nelle ultime %d ore: %d" % (ORE_INDIETRO, len(nuovi)))
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


if __name__ == "__main__":
    main()
