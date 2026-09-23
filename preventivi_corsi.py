# -*- coding: utf-8 -*-
"""Da richiesta a preventivo: il motore del catalogo corsi.

Ogni pochi minuti guarda le richieste arrivate dal modulo del catalogo
(spaggiari.eu/corsi-formazione) e, per quelle non ancora lavorate:

  1. apre la trattativa nella pipeline Formazione;
  2. crea le righe con corso, data, licenza, prezzo e CODICE ARTICOLO;
  3. genera il preventivo numerato, con lo sconto e le note d'acquisto;
  4. lo pubblica e manda alla scuola l'email con il PDF allegato,
     mettendo in copia l'ufficio MePA;
  5. porta la trattativa allo stadio "Preventivo inviato".

Le note cambiano con il numero di corsi, perche' cambia la strada d'acquisto:
un corso si ordina da soli su MePA col codice, piu' corsi passano dalla
trattativa diretta che apriamo noi. E' lo stesso testo che la scuola legge in
pagina, nel carrello e nel modulo: si dice una volta sola, nello stesso modo.

⚠️ Due cose imparate a spese nostre:
 · un preventivo PUBBLICATO non si modifica piu', quindi mittente, lingua e
   note vanno scritti alla creazione;
 · senza hs_locale le date escono in inglese su un documento italiano.

Per non lavorare due volte la stessa richiesta ogni trattativa porta in
`chiave_richiesta_corsi` l'email piu' l'orario dell'invio: se la chiave c'e'
gia', la richiesta si salta.

Uso:  python preventivi_corsi.py            lavora le richieste nuove
      python preventivi_corsi.py --prova    dice cosa farebbe, senza fare nulla
"""
import datetime
import io
import json
import os
import sys
import time
import urllib.request
import uuid

QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, QUI)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import smtplib                                            # noqa: E402
import ssl                                                # noqa: E402
from email.message import EmailMessage                    # noqa: E402
from mailer import get_graph_token, graph_send_mail       # noqa: E402

TOK = os.environ["HUBSPOT_TOKEN"]
MODULO = "67e0ada1-87d7-446e-9773-2d5581fac9c5"
PIPELINE = "4128670920"                  # Formazione
STADIO_RICHIESTA = "6059680979"          # Richiesta ricevuta
STADIO_INVIATO = "6059680980"            # Preventivo inviato
MODELLO = "1032846189811"                # Modello - V1
PROPRIETARIO = "35980393"                # Emma Zecca
DOMINIO = "eventi.spaggiari.eu"
MEPA = "mepa@spaggiari.eu"
# Il preventivo e' firmato dall'Amministratore Delegato: non puo' partire da una
# casella personale. Con i token SMTP transazionali di HubSpot il mittente lo
# decidiamo noi e gli allegati passano lo stesso.
MITTENTE = "no_reply@spaggiari.eu"
SMTP_HOST, SMTP_PORTA = "smtp.hubapi.com", 587
SEP = " — "
PETROLIO, ORO = "#06484b", "#e8b547"
ORE_INDIETRO = 24
# Il motore nasce oggi: le richieste precedenti sono collaudi fatti a mano e
# vanno lasciate stare - alcune hanno indirizzi finti, e un preventivo mandato
# a un indirizzo che non esiste torna indietro e sporca la casella.
# La soglia e' il momento del lancio e NON si sposta piu': spostarla in avanti
# per escludere una prova ha gia' lasciato fuori una richiesta vera (Bertozzi,
# 23/09 ore 09:27, esclusa da una soglia messa alle 09:31). Le prove si
# riconoscono dal nome della scuola, qui sotto.
DA_QUANDO = 1790148300000   # 23/09/2026 09:25, lancio del catalogo

NOTE_UNO = ("Il codice articolo indicato in ciascuna riga è quello dell'ordine diretto (ODA) "
            "su MePA. Questo preventivo è arrivato anche al nostro ufficio MePA: per procedere "
            "è sufficiente rispondere a questa email.")
# Tonelli (22/09/2026): la trattativa diretta la avvia la scuola; noi possiamo
# accettarla confermando la quotazione, oppure rifiutarla se l'importo non
# corrisponde. Il testo dice esattamente questo, senza promettere una conferma
# automatica.
NOTE_PIU = ("Con più corsi l'ordine passa da una trattativa diretta su MePA: la avvia la scuola "
            "verso Gruppo Spaggiari Parma indicando i codici articolo e l'importo di questo "
            "preventivo; noi verifichiamo che l'importo corrisponda e confermiamo la quotazione. "
            "Lo sconto vale in trattativa diretta: con l'ordine diretto (ODA) il prezzo è quello "
            "di catalogo. Per domande: " + MEPA + ".")
# Il catalogo e' rivolto alle scuole statali: per le paritarie e' allo studio un
# palinsesto diverso (Emanuela Dalla Rizza, 22/09/2026). Resta per compatibilita'
# con gli script che la importano.
PARITARIE = ""
CONDIZIONI = ("Offerta valida 30 giorni dalla data di emissione. I corsi si svolgono online nelle "
              "date indicate in ciascuna riga; l'attestato di partecipazione viene rilasciato al "
              "termine. Importi in euro, esenti IVA in quanto formazione rivolta alle istituzioni "
              "scolastiche.")


def invia(a, copia, oggetto, html, allegato):
    """Spedisce come no_reply@spaggiari.eu con il PDF in allegato.

    Se le credenziali SMTP mancano ripiega sul canale vecchio, che pero' parte
    dalla casella di chi ha autenticato: meglio un mittente sbagliato che un
    preventivo che non arriva, ma il caso va visto nei log."""
    utente = os.environ.get("SMTP_CORSI_USER")
    chiave = os.environ.get("SMTP_CORSI_PASS")
    if not (utente and chiave):
        print("  ATTENZIONE: credenziali SMTP assenti, invio dalla casella personale")
        t = get_graph_token(os.environ["GRAPH_TENANT_ID"], os.environ["GRAPH_CLIENT_ID"],
                            os.environ["GRAPH_REFRESH_TOKEN"])
        graph_send_mail(t["access_token"], "%s,%s" % (a, copia), oggetto, html, [allegato])
        return "casella personale"
    m = EmailMessage()
    m["From"] = "Spaggiari <%s>" % MITTENTE
    m["To"] = a
    m["Cc"] = copia
    m["Reply-To"] = MEPA
    m["Subject"] = oggetto
    m.set_content("Il preventivo e' in allegato. Per leggerlo serve un lettore di posta "
                  "che mostri i messaggi in HTML.")
    m.add_alternative(html, subtype="html")
    nome, dati = allegato
    m.add_attachment(dati, maintype="application", subtype="pdf", filename=nome)
    s = smtplib.SMTP(SMTP_HOST, SMTP_PORTA, timeout=60)
    s.starttls(context=ssl.create_default_context())
    s.login(utente, chiave)
    s.send_message(m)
    s.quit()
    return MITTENTE


def hs(percorso, corpo=None, metodo="GET"):
    """Chiama HubSpot, riprovando quando la rete cade.

    Il 22/09 un giro e' morto su "connection reset by peer" al primo contatto:
    con un tentativo solo una richiesta della scuola sarebbe rimasta indietro."""
    dati = json.dumps(corpo).encode() if corpo is not None else None
    ultimo = None
    for tentativo in range(3):
        r = urllib.request.Request("https://api.hubapi.com" + percorso, data=dati, method=metodo,
                                   headers={"Authorization": "Bearer " + TOK,
                                            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=120) as x:
                d = x.read()
                return json.loads(d) if d else {"_ok": x.status}
        except urllib.error.HTTPError as e:
            ultimo = {"_err": e.code, "_msg": e.read().decode()[:400]}
            if e.code not in (429, 500, 502, 503, 504):
                return ultimo
        except Exception as e:                      # rete caduta, DNS, timeout
            ultimo = {"_err": 0, "_msg": "%s: %s" % (type(e).__name__, str(e)[:200])}
        time.sleep(3 * (tentativo + 1))
    return ultimo


def lega(da, id_da, a, id_a):
    return hs("/crm/v4/objects/%s/%s/associations/default/%s/%s" % (da, id_da, a, id_a), {}, "PUT")


def euro(n):
    return ("%.2f" % n).replace(".", ",") + " €"


def righe_da(testo):
    fuori = []
    for riga in (testo or "").split("\n"):
        if not riga.strip():
            continue
        p = [x.strip() for x in riga.split(SEP)]
        v = {"corso": p[0], "licenza": "", "prezzo": 0.0, "quando": "", "codice": ""}
        for pezzo in p[1:]:
            b = pezzo.lower()
            if b.startswith("licenza "):
                v["licenza"] = pezzo[8:]
            elif b.endswith("eur"):
                v["prezzo"] = float(pezzo.split()[0].replace(",", "."))
            elif b.startswith("cod. "):
                v["codice"] = pezzo[5:]
            else:
                v["quando"] = pezzo
        fuori.append(v)
    return fuori


def stato_richiesta(chiave):
    """Dice se la richiesta e' gia' servita, rimasta a meta' o ancora da fare.

    Prima bastava l'esistenza della trattativa: ma la trattativa nasce all'inizio
    del lavoro, quindi un intoppo dopo (PDF non pronto, posta giu', rete caduta)
    lasciava la scuola senza preventivo e il giro successivo tirava dritto. Ora
    il segno di "fatto" e' la data di invio, scritta solo quando l'email e'
    partita davvero: cio' che resta a meta' viene ripreso."""
    r = hs("/crm/v3/objects/deals/search", {"filterGroups": [{"filters": [
        {"propertyName": "chiave_richiesta_corsi", "operator": "EQ", "value": chiave}]}],
        "properties": ["preventivo_inviato_il"], "limit": 1}, "POST")
    if r.get("results"):
        t = r["results"][0]
        if t["properties"].get("preventivo_inviato_il"):
            return "fatta", None
        return "a_meta", t["id"]
    c = hs("/crm/v3/objects/contacts/search", {"filterGroups": [{"filters": [
        {"propertyName": "ultimo_preventivo_corsi", "operator": "EQ", "value": chiave}]}],
        "limit": 1}, "POST")
    if c.get("results"):
        return "fatta", None
    return "nuova", None


def figli(trattativa, tipo):
    a = hs("/crm/v4/objects/deals/%s/associations/%s?limit=100" % (trattativa, tipo))
    return [x["toObjectId"] for x in a.get("results", [])]


def dati_scuola(v, azienda, contatto):
    """Raccoglie i dati che vanno nel riquadro del destinatario.

    Vengono da tre parti, perche' nessuna le ha tutte: la scheda della scuola in
    HubSpot porta codice cliente, codice fiscale e partita IVA; il modulo porta
    denominazione e codice meccanografico (sulle schede quel campo non e' mai
    compilato); l'indirizzo sta sul contatto. Cio' che manca resta vuoto e sul
    documento non compare la riga."""
    a = {}
    if azienda:
        r = hs("/crm/v3/objects/companies/%s?properties=name,codice_cliente,codice_fiscale,"
               "p_iva,codice_meccanografico_cliente,address,city,zip,state" % azienda)
        a = r.get("properties") or {}

    def prima(*valori):
        for x in valori:
            if x and str(x).strip():
                return str(x).strip()
        return ""

    via = prima(a.get("address"), contatto.get("address"))
    citta = prima(a.get("city"), contatto.get("city"))
    cap = prima(a.get("zip"), contatto.get("zip"))
    prov = prima(a.get("state"), contatto.get("state"))
    riga = ", ".join(x for x in (via, " ".join(y for y in (cap, citta) if y)) if x)
    if prov:
        riga = (riga + " (%s)" % prov).strip()
    return {
        "nome": prima(a.get("name"), (v.get("scuola") or "").strip()),
        "indirizzo": riga,
        "codice_cliente": prima(a.get("codice_cliente"), contatto.get("codice_cliente")),
        "meccanografico": prima(v.get("codice_meccanografico"),
                                contatto.get("codice_meccanografico"),
                                a.get("codice_meccanografico_cliente")),
        "codice_fiscale": prima(a.get("codice_fiscale")),
        "piva": prima(a.get("p_iva")),
    }


def corpo_email(d):
    righe = "".join(
        '<tr><td style="padding:10px 12px;border-bottom:1px solid #e3e9e8">'
        '<b style="color:#0f2b2c">%s</b><br><span style="color:#6d817f;font-size:13px">'
        '%s &middot; %s &middot; cod. %s</span></td>'
        '<td style="padding:10px 12px;border-bottom:1px solid #e3e9e8;text-align:right;'
        'white-space:nowrap;color:#0f2b2c">%s</td></tr>'
        % (r["corso"], r["quando"], r["licenza"], r["codice"], euro(r["prezzo"]))
        for r in d["righe"])
    sconto = ""
    if d["sconto"]:
        sconto = ('<tr><td style="padding:4px 12px;color:#6d817f">Sconto %d%%</td>'
                  '<td style="padding:4px 12px;text-align:right;color:#6d817f">&minus;%s</td></tr>'
                  % (d["sconto"], euro(d["lordo"] - d["netto"])))
    come = (("Il codice articolo indicato in ciascuna riga &egrave; quello dell&rsquo;ordine "
             "diretto (ODA) su MePA.") if len(d["righe"]) == 1 else
            ("Con pi&ugrave; corsi l&rsquo;ordine passa da una <b>trattativa diretta</b> su MePA: la "
             "avvia la scuola verso Gruppo Spaggiari Parma indicando i codici e l&rsquo;importo "
             "di questo preventivo; noi verifichiamo che l&rsquo;importo corrisponda e confermiamo la "
             "quotazione. Lo sconto vale in trattativa diretta: con l&rsquo;ordine diretto (ODA) il "
             "prezzo &egrave; quello di catalogo. Per domande: "
             "<a href=\"mailto:%s\" style=\"color:%s\">%s</a>." % (MEPA, PETROLIO, MEPA)))
    return """<div style="font:15px/1.6 Arial,sans-serif;color:#3f5453;max-width:660px">
<p>Gentile %(nome)s,</p>
<p>in allegato il <b>preventivo n. %(numero)s</b> intestato a %(scuola)s, valido 30 giorni.</p>
<table style="border-collapse:collapse;width:100%%;font:14px/1.5 Arial,sans-serif;margin:18px 0">
%(righe)s%(sconto)s
<tr><td style="padding:12px;font-weight:700;color:%(p)s">Totale</td>
<td style="padding:12px;text-align:right;font-weight:700;font-size:17px;color:%(p)s">%(tot)s</td></tr>
</table>
<p style="background:#f2f7f6;border-left:3px solid %(p)s;padding:12px 14px">
<b>Come si acquista</b><br>%(come)s<br>
Importi <b>esenti IVA</b>, trattandosi di formazione rivolta alle istituzioni scolastiche.<br>
Per procedere basta rispondere a questa email, oppure usare il pulsante nel preventivo.</p>
<p style="margin:26px 0"><a href="%(link)s" style="background:%(o)s;color:%(p)s;
text-decoration:none;font-weight:700;padding:13px 22px;border-radius:10px;display:inline-block">
APRI IL PREVENTIVO</a></p>
<p style="color:#6d817f;font-size:13px;margin-top:26px">Gruppo Spaggiari Parma S.p.A. &middot;
Via Bernini 22/A, 43126 Parma &middot; P.IVA 00150470342<br>
Nicola de Cesare, Amministratore Delegato</p></div>""" % {
        "nome": d["nome"], "numero": d["numero"], "scuola": d["scuola"], "righe": righe,
        "sconto": sconto, "tot": euro(d["netto"]), "come": come, "link": d["link"],
        "p": PETROLIO, "o": ORO}


def lavora(inv, prova):
    v = {c["name"]: c["value"] for c in inv["values"]}
    quando = datetime.datetime.fromtimestamp(inv["submittedAt"] / 1000)
    chiave = "%s|%s" % (v.get("email", ""), inv["submittedAt"])
    scuola = (v.get("scuola") or "Scuola").strip()
    righe = righe_da(v.get("corsi_richiesti"))
    if not righe or not v.get("email"):
        print("  salto (richiesta senza righe o senza email)")
        return
    # Le prove si chiamano PROVA: cosi' si possono cancellare senza che il
    # motore le riveda come nuove, e senza toccare la soglia.
    if scuola.upper().startswith("PROVA"):
        print("  salto la prova di %s" % scuola)
        return
    stato, ripresa = stato_richiesta(chiave)
    if stato == "fatta":
        return
    sconto = int(v.get("sconto_corsi") or 0)
    lordo = sum(r["prezzo"] for r in righe)
    netto = round(lordo * (100 - sconto) / 100, 2)
    print("\n%s  %s <%s> - %s - %d corsi, %s"
          % (quando.strftime("%d/%m %H:%M"), v.get("firstname"), v.get("email"), scuola,
             len(righe), euro(netto)))
    if prova:
        print("  --prova: mi fermo qui")
        return

    if ripresa:
        # un giro precedente si e' fermato per strada: si riprende da li' invece
        # di rifare tutto, cosi' la scuola non riceve due preventivi
        trattativa = ripresa
        print("  riprendo la trattativa %s rimasta a meta'" % trattativa)
    else:
        d = hs("/crm/v3/objects/deals", {"properties": {
            "dealname": "Corsi di formazione - %s" % scuola, "pipeline": PIPELINE,
            "dealstage": STADIO_RICHIESTA, "amount": str(netto),
            "hubspot_owner_id": PROPRIETARIO, "chiave_richiesta_corsi": chiave}}, "POST")
        if "_err" in d:
            print("  trattativa NON creata:", d["_msg"])
            return
        trattativa = d["id"]

    ids = figli(trattativa, "line_items") if ripresa else []
    for r in (righe if not ids else []):
        li = hs("/crm/v3/objects/line_items", {"properties": {
            "name": "%s - %s" % (r["corso"], r["licenza"]), "hs_sku": r["codice"],
            "price": str(r["prezzo"]), "quantity": "1",
            "description": "Corso online con attestato - %s" % r["quando"],
            "hs_discount_percentage": str(sconto)}}, "POST")
        if "_err" in li:
            print("  riga NON creata:", li["_msg"])
            continue
        ids.append(li["id"])
        lega("line_items", li["id"], "deals", trattativa)

    # il contatto serve per intestare il preventivo alla scuola e per agganciare
    # la trattativa: si cerca prima di creare il documento
    cerca = hs("/crm/v3/objects/contacts/search", {"filterGroups": [{"filters": [
        {"propertyName": "email", "operator": "EQ", "value": v["email"]}]}],
        "properties": ["associatedcompanyid", "codice_meccanografico", "codice_cliente",
                       "address", "city", "zip", "state"], "limit": 1}, "POST")
    dati_contatto = cerca["results"][0]["properties"] if cerca.get("results") else {}
    contatto = cerca["results"][0]["id"] if cerca.get("results") else None
    azienda = dati_contatto.get("associatedcompanyid")
    destinatario = dati_scuola(v, azienda, dati_contatto)

    prev_esistente = (figli(trattativa, "quotes") or [None])[0] if ripresa else None
    note = (NOTE_UNO if len(righe) == 1 else NOTE_PIU) + PARITARIE
    scadenza = int((datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(days=30)).timestamp() * 1000)
    q = {"id": prev_esistente} if prev_esistente else hs("/crm/v3/objects/quotes", {"properties": {
        "hs_title": "Corsi di formazione Spaggiari - %s" % scuola,
        "hs_expiration_date": str(scadenza), "hs_status": "DRAFT",
        "hs_language": "it", "hs_locale": "it-IT", "hs_currency": "EUR",
        "hs_comments": note, "hs_terms": CONDIZIONI,
        "hs_sender_company_name": "Gruppo Spaggiari Parma S.p.A.",
        "hs_sender_company_address": "Via Bernini 22/A", "hs_sender_company_city": "Parma",
        "hs_sender_company_zip": "43126", "hs_sender_company_state": "PR",
        "hs_sender_company_country": "Italia", "hs_sender_company_domain": "spaggiari.eu",
        "hs_sender_firstname": "Nicola", "hs_sender_lastname": "de Cesare",
        "hs_sender_jobtitle": "Amministratore Delegato", "hs_sender_email": MEPA,
        "spg_dest_nome": destinatario["nome"],
        "spg_dest_indirizzo": destinatario["indirizzo"],
        "spg_codice_cliente": destinatario["codice_cliente"],
        "spg_codice_meccanografico": destinatario["meccanografico"],
        "spg_codice_fiscale": destinatario["codice_fiscale"],
        "spg_piva": destinatario["piva"]}}, "POST")
    if "_err" in q:
        print("  preventivo NON creato:", q["_msg"])
        return
    prev = q["id"]
    for tipo, ident in ([] if prev_esistente else
                        (("quote_template", MODELLO), ("deals", trattativa))):
        lega("quotes", prev, tipo, ident)
    for i in (ids if not prev_esistente else []):
        lega("quotes", prev, "line_items", i)

    if contatto:
        lega("quotes", prev, "contacts", contatto)
        lega("deals", trattativa, "contacts", contatto)
        if azienda:
            lega("quotes", prev, "companies", azienda)
            lega("deals", trattativa, "companies", azienda)

    # un preventivo gia' pubblicato HubSpot lo considera chiuso: ripubblicarlo
    # darebbe errore, quindi in ripresa si pubblica solo cio' che e' ancora bozza
    gia_online = prev_esistente and hs(
        "/crm/v3/objects/quotes/%s?properties=hs_quote_link" % prev
    )["properties"].get("hs_quote_link")
    if not gia_online:
        r = hs("/crm/v3/objects/quotes/" + prev, {"properties": {
            "hs_slug": uuid.uuid4().hex[:20], "hs_domain": DOMINIO,
            "hubspot_owner_id": PROPRIETARIO, "hs_status": "APPROVAL_NOT_NEEDED"}}, "PATCH")
        if "_err" in r:
            print("  preventivo NON pubblicato:", r["_msg"])
            return

    dati = {}
    for _ in range(12):
        p = hs("/crm/v3/objects/quotes/%s?properties=hs_quote_link,hs_quote_number,"
               "hs_pdf_download_link,hs_pdf_generation_status" % prev)["properties"]
        if p.get("hs_quote_link") and p.get("hs_pdf_generation_status") == "PDF_GENERATED":
            dati = p
            break
        time.sleep(5)
    if not dati:
        print("  preventivo creato ma PDF non pronto: email non inviata")
        return

    try:
        pdf = urllib.request.urlopen(urllib.request.Request(
            dati["hs_pdf_download_link"], headers={"User-Agent": "Mozilla/5.0"}),
            timeout=120).read()
    except Exception as e:
        # niente panico e niente doppioni: la data di invio resta vuota, quindi
        # il giro successivo riprende questa richiesta da qui
        print("  PDF non scaricato (%s): riprovo al prossimo giro" % type(e).__name__)
        return
    testo = corpo_email({"nome": v.get("firstname") or "", "scuola": scuola,
                         "numero": dati["hs_quote_number"], "link": dati["hs_quote_link"],
                         "righe": righe, "lordo": lordo, "netto": netto, "sconto": sconto})
    try:
        da = invia(v["email"], MEPA,
                   "Preventivo n. %s · Corsi di formazione Spaggiari" % dati["hs_quote_number"],
                   testo, ("Preventivo-%s.pdf" % dati["hs_quote_number"], pdf))
    except Exception as e:
        print("  email NON partita (%s): riprovo al prossimo giro" % type(e).__name__)
        return
    ora = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    hs("/crm/v3/objects/deals/" + trattativa,
       {"properties": {"dealstage": STADIO_INVIATO, "preventivo_inviato_il": ora}}, "PATCH")
    if contatto:
        hs("/crm/v3/objects/contacts/" + contatto,
           {"properties": {"ultimo_preventivo_corsi": chiave}}, "PATCH")
    print("  preventivo %s inviato a %s (copia a %s) da %s"
          % (dati["hs_quote_number"], v["email"], MEPA, da))


def main():
    prova = "--prova" in sys.argv
    da = int((datetime.datetime.now() - datetime.timedelta(hours=ORE_INDIETRO)).timestamp() * 1000)
    s = hs("/form-integrations/v1/submissions/forms/%s?limit=50" % MODULO)
    nuovi = [x for x in s.get("results", []) if x["submittedAt"] >= max(da, DA_QUANDO)]
    print("richieste nelle ultime %d ore: %d" % (ORE_INDIETRO, len(nuovi)))
    for inv in reversed(nuovi):
        lavora(inv, prova)
    print("\nfatto.")


if __name__ == "__main__":
    main()
