# -*- coding: utf-8 -*-
"""Da richiesta a preventivo: il motore del catalogo corsi.

Ogni pochi minuti guarda le richieste arrivate dal modulo del catalogo
(spaggiari.eu/corsi-formazione) e, per quelle non ancora lavorate:

  1. apre la trattativa nella pipeline Formazione;
  2. crea le righe con corso, data, licenza, prezzo e CODICE ARTICOLO;
  3. genera il preventivo numerato, con lo sconto e le note d'acquisto;
  4. lo pubblica e manda alla scuola l'email con il PDF allegato,
     senza copia all'ufficio MePA (Andrea 24/9/2026: riceve gia' la notifica della
     trattativa diretta; le risposte della scuola arrivano col Reply-To);
  5. porta la trattativa allo stadio "Preventivo inviato".

Le note cambiano con il numero di corsi, perche' cambia la strada d'acquisto:
un corso si ordina da soli su MePA col codice, piu' corsi passano dalla
trattativa diretta che avvia la scuola. E' lo stesso testo che la scuola legge in
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
import hashlib
import re

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

# Andrea 24/9/2026: la scuola procede IN AUTONOMIA su MePA col preventivo in mano, indica il
# CPV 80500000-9; l'esenzione IVA poggia sull'essere Ente Certificato; niente attestato.
NOTE_UNO = ("Con un solo corso, la Scuola procede in autonomia con un ordine diretto (ODA) su "
            "MePA, usando il codice indicato in questo preventivo e il CPV 80500000-9. Per "
            "qualsiasi domanda può scrivere al nostro "
            "ufficio MePA: " + MEPA + ".")
# Tonelli (22/09/2026): la trattativa diretta la avvia la scuola; noi possiamo
# accettarla confermando la quotazione, oppure rifiutarla se l'importo non
# corrisponde. Il testo dice esattamente questo, senza promettere una conferma
# automatica.
NOTE_PIU = ("Con più corsi, la Scuola apre in autonomia una trattativa diretta su MePA con "
            "Gruppo Spaggiari Parma S.p.A., indicando i codici e l'importo di questo preventivo "
            "e il CPV 80500000-9. Noi verifichiamo l'importo e confermiamo l'offerta.")
# solo quando uno sconto c'e': senza, la frase parlerebbe di qualcosa che non c'e'
NOTA_SCONTO = (" Lo sconto vale solo con la trattativa diretta: con l'ordine diretto (ODA) si "
               "applica il prezzo di listino.")
NOTA_CONTATTO = (" Per qualsiasi domanda può scrivere al nostro ufficio MePA: " + MEPA + ".")
# Il catalogo e' rivolto alle scuole statali: per le paritarie e' allo studio un
# palinsesto diverso (Emanuela Dalla Rizza, 22/09/2026). Resta per compatibilita'
# con gli script che la importano.
PARITARIE = ""
CONDIZIONI = ("Offerta valida 30 giorni. I corsi si svolgono online nelle date indicate. "
              "Importi in euro, esenti da IVA: Gruppo Spaggiari Parma S.p.A. è Ente Certificato "
              "per erogare la Formazione del Personale della Scuola.")


def invia(a, copia, oggetto, html, allegato):
    # copia=None: nessuna copia (dal 24/9/2026 il preventivo non va piu' a mepa@)
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
        graph_send_mail(t["access_token"], "%s,%s" % (a, copia) if copia else a, oggetto, html,
                        [allegato])
        return "casella personale"
    m = EmailMessage()
    m["From"] = "Spaggiari <%s>" % MITTENTE
    m["To"] = a
    if copia:
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
    """Legge le righe del carrello: corso - licenza X - NN EUR - data - cod. CODICE.

    Il titolo del corso puo' contenere lo stesso trattino lungo che separa i
    campi ("PLS - Progettiamo la Scuola: ..."): prima il motore tagliava li' e
    sul preventivo i due corsi PLS risultavano entrambi "PLS". Il titolo e'
    quindi tutto cio' che precede il campo della licenza, che c'e' sempre."""
    fuori = []
    for riga in (testo or "").split("\n"):
        if not riga.strip():
            continue
        p = [x.strip() for x in riga.split(SEP)]
        lic = next((i for i, x in enumerate(p) if x.lower().startswith("licenza ")), None)
        if not lic:
            lic = 1                      # riga senza licenza: vale il vecchio schema
        v = {"corso": SEP.join(p[:lic]), "licenza": "", "prezzo": 0.0, "quando": "", "codice": ""}
        for pezzo in p[lic:]:
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

# Registro degli invii: l'impronta (sha256) di ogni richiesta gia' servita.
# Vive nel repository, quindi non dipende dal CRM: se qualcuno cancella la
# trattativa, la scuola non riceve il preventivo una seconda volta. Contiene
# solo impronte, nessun indirizzo.
REGISTRO = os.path.join(QUI, "inviati.txt")


def impronta(chiave):
    return hashlib.sha256(chiave.encode("utf-8")).hexdigest()


def registro():
    try:
        return {r.strip() for r in io.open(REGISTRO, encoding="utf-8") if r.strip()}
    except FileNotFoundError:
        return set()


def registra(chiave):
    with io.open(REGISTRO, "a", encoding="utf-8") as f:
        f.write(impronta(chiave) + "\n")


# Una richiesta, una trattativa, e la crea solo questo motore. Fino al 23/09 lo
# faceva anche il repository corsi-trattative ("Corsi N - scuola"), poi spento
# per decisione di Andrea: le sue regole vivono qui - nome, referente della
# scuola o turno Emma/Laura, descrizione, marcatore invio_form_corsi. Se una
# trattativa con quel marcatore esiste gia' (le vecchie, o una riaccensione
# per errore dell'altro), si riusa invece di aprirne una seconda.
MARCATORE = "invio_form_corsi"
VENDITORI = [("35980393", "Emma Zecca"), ("37524294", "Laura Primiceri")]


def trattativa_dell_invio(submitted_at):
    r = hs("/crm/v3/objects/deals/search", {"filterGroups": [{"filters": [
        {"propertyName": MARCATORE, "operator": "EQ", "value": str(submitted_at)}]}],
        "properties": ["dealname", "hubspot_owner_id"], "limit": 1}, "POST")
    return (r.get("results") or [None])[0]


TEAM_AGENTI = "Sales - Agenti"
# Andrea 25/9/2026: senza agente di zona, trattativa e task vanno a turno a loro (chi
# ne ha meno aperti); sono anche gli unici che ricevono la notifica del modulo.
RIPIEGO = [("30267680", "Camilla Maestri"), ("31296437", "Stefano Benassi"),
           ("78283682", "Alessandro Tonelli")]


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


def agente_di_zona(azienda, meccanografico):
    """Andrea 25/9/2026: trattativa e task vanno SOLO agli agenti. Prima si usava il
    proprietario del CONTATTO (che puo' essere l'assistenza: l'IISS Galilei di Bolzano
    e' finito a Mattia Ciabattoni, Software Assistance) e, senza, il turno Emma/Laura.
    Ora: il proprietario della SCUOLA (azienda collegata, oppure trovata dal codice
    meccanografico), e solo se sta nel team Sales - Agenti. Altrimenti None."""
    candidati = []
    if azienda:
        candidati.append(str(azienda))
    mecc = (meccanografico or "").strip().upper()
    if mecc:
        r = hs("/crm/v3/objects/companies/search", {"filterGroups": [{"filters": [
            {"propertyName": "name", "operator": "CONTAINS_TOKEN", "value": mecc}]}],
            "properties": ["name"], "limit": 5}, "POST")
        candidati += [x["id"] for x in r.get("results", [])
                      if (x["properties"].get("name") or "").upper().startswith(mecc)]
    for cid in candidati:
        s = hs("/crm/v3/objects/companies/%s?properties=hubspot_owner_id" % cid)
        oid = (s.get("properties") or {}).get("hubspot_owner_id")
        if not oid:
            continue
        o = hs("/crm/v3/owners/%s?idProperty=id" % oid)
        if any(x.get("name") == TEAM_AGENTI for x in o.get("teams", [])):
            return oid, cid
    return None, (candidati[0] if candidati else None)


def numero_cellulare(grezzo):
    """Il numero del modulo in formato internazionale, solo se e' un cellulare
    italiano: un fisso (che inizia per 0) l'SMS non lo riceve."""
    cifre = re.sub(r"\D", "", grezzo or "")
    if cifre.startswith("0039"):
        cifre = cifre[2:]
    if cifre.startswith("39") and len(cifre) in (11, 12) and cifre[2] == "3":
        return cifre
    if cifre.startswith("3") and len(cifre) in (9, 10):
        return "39" + cifre
    return None


def testo_sms(nome, numero):
    """Poche parole e un motivo per leggerle: dice che il preventivo e' arrivato
    per e-mail e dove cercarlo se non si vede. Sotto i 160 caratteri, cosi'
    resta un SMS solo; senza simboli fuori dall'alfabeto degli SMS."""
    base = ("Le abbiamo inviato per e-mail il preventivo n. %s per i corsi di formazione "
            "Spaggiari. Se non lo trova, controlli la posta indesiderata." % numero)
    con_nome = "Gentile %s, %s" % (nome, base[0].lower() + base[1:]) if nome else base
    return con_nome if len(con_nome) <= 160 else base


def invia_sms(msisdn, testo):
    """Mitto, mittente "Spaggiari". Mai bloccante: l'SMS accompagna l'e-mail,
    non la sostituisce."""
    chiave = os.environ.get("MITTO_API_KEY")
    if not chiave:
        return "chiave SMS assente"
    corpo = {"from": "Spaggiari", "to": msisdn, "text": testo}
    if os.environ.get("SMS_PROVA") == "1":
        corpo["test"] = True
    r = urllib.request.Request("https://rest.mittoapi.com/sms?format=json",
                               data=json.dumps(corpo).encode(), method="POST",
                               headers={"X-Mitto-API-Key": chiave,
                                        "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as x:
        esito = json.loads(x.read() or b"{}")
    return "accettato" if esito.get("responseCode") == 0 else "rifiutato: %s" % esito


WA_MODELLO = "preventivo_corsi_v1"


def invia_whatsapp(msisdn, nome, numero, link):
    """Conferma su WhatsApp, solo a chi l'ha chiesta. Modello di servizio
    approvato da Meta: nome e numero del preventivo nel testo, e tre pulsanti.
    "Apri il preventivo" porta alla pagina del preventivo (il pezzo finale del
    collegamento e' la variabile); gli altri due tornano a noi come risposta e
    li gestisce il ricevitore whatsapp-in. Restituisce (riuscito, esito)."""
    chiave = os.environ.get("MITTO_CHAT_KEY")
    traffico = os.environ.get("MITTO_TRAFFIC_WA")
    if not chiave or not traffico:
        return False, "chiavi WhatsApp assenti"
    intest = {"X-Mitto-API-Key": chiave, "Content-Type": "application/json",
              "Accept": "application/json"}
    # finche' Meta non approva il modello si resta sull'SMS
    elenco = json.loads(urllib.request.urlopen(urllib.request.Request(
        "https://messaging.mittoapi.com/api/v1/trafficAccounts/%s/WhatsAppTemplates" % traffico,
        headers=intest), timeout=30).read() or b"[]")
    if not any(t.get("name") == WA_MODELLO and t.get("status") == "APPROVED"
               for t in (elenco if isinstance(elenco, list) else [])):
        return False, "modello non ancora approvato"
    slug = link.rstrip("/").rsplit("/", 1)[-1]
    corpo = {"destination": "+" + msisdn, "trafficAccountId": traffico,
             "whatsapp": {"type": "template", "template": {
                 "name": WA_MODELLO,
                 # la lingua vuole la forma oggetto: con la stringa Mitto accetta
                 # e poi fallisce con "Can't get template language code"
                 "language": {"code": "it"},
                 "components": [
                     {"type": "body", "parameters": [
                         {"type": "text", "text": nome or "Dirigente"},
                         {"type": "text", "text": numero}]},
                     {"type": "button", "sub_type": "url", "index": "0",
                      "parameters": [{"type": "text", "text": slug}]}]}}}
    r = json.loads(urllib.request.urlopen(urllib.request.Request(
        "https://messaging.mittoapi.com/api/v1.1/Messages/send",
        data=json.dumps(corpo).encode(), method="POST", headers=intest),
        timeout=30).read() or b"{}")
    ident = r.get("id") or r.get("messageId")
    if not ident:
        return False, "rifiutato: %s" % str(r.get("errors") or r)[:120]
    # l'accettazione non e' la consegna: lo stato vero arriva poco dopo
    time.sleep(5)
    st = json.loads(urllib.request.urlopen(urllib.request.Request(
        "https://messaging.mittoapi.com/api/v1.1/Messages/%s" % ident, headers=intest),
        timeout=30).read() or b"{}")
    esito = str(st.get("deliveryStatus") or "sconosciuto")
    return esito != "Failure", esito + (" - " + st["description"] if st.get("description") else "")


def vuole_whatsapp(v, contatto):
    """Consenso dalla casella del modulo di questa richiesta, oppure gia' dato
    in passato (per esempio da un pulsante WhatsApp degli eventi). Se arriva
    dalla casella, sul contatto si segnano data e provenienza."""
    if str(v.get("consenso_whatsapp", "")).lower() == "true":
        if contatto:
            hs("/crm/v3/objects/contacts/%s" % contatto, {"properties": {
                "consenso_whatsapp": "true",
                "consenso_whatsapp_data": datetime.datetime.now(datetime.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "consenso_whatsapp_origine": "modulo corsi di formazione"}}, "PATCH")
        return True
    if not contatto:
        return False
    return str(hs("/crm/v3/objects/contacts/%s?properties=consenso_whatsapp" % contatto)
               .get("properties", {}).get("consenso_whatsapp")).lower() == "true"


CONTROLLO = "pizzola@spaggiari.eu"


def avviso_controllo(scuola, responsabile, motivo, trattativa, n_corsi, netto):
    """Andrea 25/9/2026: "i task devono essere notificati anche a me, perche' devo
    controllare quello che sta succedendo". Una riga per richiesta: scuola, a chi e'
    andato il task e perche', link alla trattativa."""
    utente = os.environ.get("SMTP_CORSI_USER")
    chiave = os.environ.get("SMTP_CORSI_PASS")
    if not (utente and chiave):
        print("  avviso di controllo: credenziali SMTP assenti")
        return
    nome = "nessuno"
    if responsabile:
        o = hs("/crm/v3/owners/%s?idProperty=id" % responsabile)
        nome = ("%s %s" % (o.get("firstName") or "", o.get("lastName") or "")).strip() or responsabile
    link = "https://app-eu1.hubspot.com/contacts/144406271/record/0-3/%s" % trattativa
    m = EmailMessage()
    m["From"] = "Spaggiari <%s>" % MITTENTE
    m["To"] = CONTROLLO
    m["Subject"] = "Corsi: %s -> %s" % (scuola, nome)
    m.set_content("Richiesta preventivo corsi da %s (%d corsi, %s).\n\n"
                  "Trattativa e task assegnati a: %s\nPerche': %s\n\nTrattativa: %s\n"
                  % (scuola, n_corsi, euro(netto), nome, motivo, link))
    s = smtplib.SMTP(SMTP_HOST, SMTP_PORTA, timeout=60)
    s.starttls(context=ssl.create_default_context())
    s.login(utente, chiave)
    s.send_message(m)
    s.quit()
    print("  avviso di controllo inviato a %s" % CONTROLLO)


def allinea_richiamata(contatto, trattativa, responsabile, submitted_at, scuola):
    """L'attivita' di richiamata la crea HubSpot all'invio del modulo (flusso
    4929128670): al referente della scuola se c'e', altrimenti sempre a Emma.
    La trattativa invece, senza referente, va a turno a Emma o Laura. Qui la
    richiamata passa alla stessa persona della trattativa e ci viene agganciata,
    cosi' chi ha la trattativa ha anche la telefonata da fare."""
    if not contatto:
        return
    trovate = 0
    a = hs("/crm/v4/objects/contacts/%s/associations/tasks?limit=100" % contatto)
    for x in a.get("results", []):
        t = hs("/crm/v3/objects/tasks/%s?properties=hs_task_subject,hubspot_owner_id,"
               "hs_createdate,hs_task_status" % x["toObjectId"]).get("properties", {})
        oggetto = t.get("hs_task_subject") or ""
        if not oggetto.startswith("Preventivo corsi:") or scuola not in oggetto:
            continue
        if t.get("hs_task_status") == "COMPLETED":
            continue
        try:
            creata = datetime.datetime.fromisoformat(
                t["hs_createdate"].replace("Z", "+00:00")).timestamp() * 1000
        except Exception:
            continue
        # solo la richiamata nata da questa richiesta (pochi minuti dopo l'invio)
        if not (submitted_at - 60000 <= creata <= submitted_at + 1800000):
            continue
        if t.get("hubspot_owner_id") != responsabile:
            hs("/crm/v3/objects/tasks/%s" % x["toObjectId"],
               {"properties": {"hubspot_owner_id": responsabile}}, "PATCH")
            print("  richiamata passata a %s, come la trattativa" % responsabile)
        lega("tasks", x["toObjectId"], "deals", trattativa)
        trovate += 1
    # Andrea 25/9/2026: il task lo crea il motore, gia' assegnato all'agente di zona.
    # Il flusso 4929128670 lo assegnava al proprietario del CONTATTO (anche
    # l'assistenza) prima che qui venisse spostato: la notifica era gia' partita.
    # Finche' il flusso non viene ripulito dall'interfaccia, se il suo task c'e' si
    # riusa (sopra); se non c'e' lo crea il motore, solo quando c'e' un agente.
    if not trovate and responsabile:
        domani = (datetime.datetime.now(datetime.timezone.utc)
                  + datetime.timedelta(days=1)).strftime("%Y-%m-%dT05:00:00Z")
        n = hs("/crm/v3/objects/tasks", {"properties": {
            "hs_task_subject": "Preventivo corsi: %s" % scuola,
            "hs_task_body": "Richiesta dal catalogo corsi: il preventivo e' gia' partito verso "
                            "la scuola. Dettagli nella trattativa collegata.",
            "hs_task_type": "CALL", "hs_task_priority": "HIGH", "hs_task_status": "NOT_STARTED",
            "hubspot_owner_id": responsabile, "hs_timestamp": domani}}, "POST")
        if n.get("id"):
            lega("tasks", n["id"], "deals", trattativa)
            lega("tasks", n["id"], "contacts", contatto)
            print("  richiamata creata per l'agente %s" % responsabile)


def stato_richiesta(chiave):
    """Dice se la richiesta e' gia' servita, rimasta a meta' o ancora da fare.

    Prima bastava l'esistenza della trattativa: ma la trattativa nasce all'inizio
    del lavoro, quindi un intoppo dopo (PDF non pronto, posta giu', rete caduta)
    lasciava la scuola senza preventivo e il giro successivo tirava dritto. Ora
    il segno di "fatto" e' la data di invio, scritta solo quando l'email e'
    partita davvero: cio' che resta a meta' viene ripreso."""
    if impronta(chiave) in registro():
        return "fatta", None
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
    # le associazioni v4 restituiscono gli id come numeri: qui diventano testo,
    # come quelli delle ricerche, cosi' si possono usare ovunque negli indirizzi
    return [str(x["toObjectId"]) for x in a.get("results", [])]


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
    mepa = '<a href="mailto:%s" style="color:%s">%s</a>' % (MEPA, PETROLIO, MEPA)
    if len(d["righe"]) == 1:
        come = ("<p style=\"margin:4px 0 0\">Con un solo corso, la Scuola procede <b>in "
                "autonomia</b> con un <b>ordine diretto (ODA)</b> su MePA, usando il codice "
                "indicato nel preventivo.</p>")
    else:
        come = ("<p style=\"margin:4px 0 0\">Con pi&ugrave; corsi, l&rsquo;acquisto avviene con "
                "una <b>trattativa diretta</b> su MePA:</p>"
                "<ol style=\"margin:6px 0 0;padding-left:20px\">"
                "<li>la Scuola apre <b>in autonomia</b> la trattativa con Gruppo Spaggiari Parma "
                "S.p.A. e indica i codici e l&rsquo;importo di questo preventivo;</li>"
                "<li>noi verifichiamo l&rsquo;importo e confermiamo l&rsquo;offerta.</li></ol>")
        if d["sconto"]:
            come += ("<p style=\"margin:8px 0 0\">Lo sconto vale solo con la trattativa diretta: "
                     "con l&rsquo;ordine diretto (ODA) si applica il prezzo di listino.</p>")
    return """<div style="font:15px/1.6 Arial,sans-serif;color:#3f5453;max-width:660px">
<p>Gentile %(nome)s,</p>
<p>Le inviamo in allegato il <b>preventivo n. %(numero)s</b> intestato a %(scuola)s, valido
30 giorni.</p>
<table style="border-collapse:collapse;width:100%%;font:14px/1.5 Arial,sans-serif;margin:18px 0">
%(righe)s%(sconto)s
<tr><td style="padding:12px;font-weight:700;color:%(p)s">Totale</td>
<td style="padding:12px;text-align:right;font-weight:700;font-size:17px;color:%(p)s">%(tot)s</td></tr>
</table>
<div style="background:#f2f7f6;border-left:3px solid %(p)s;padding:12px 14px">
<b>Come si acquista</b>%(come)s
<p style="margin:8px 0 0">Nell&rsquo;ordine su MePA indichi il <b>CPV 80500000-9</b> (servizi di
formazione).</p>
<p style="margin:8px 0 0">Gli importi sono <b>esenti da IVA</b>: Gruppo Spaggiari Parma S.p.A. &egrave;
Ente Certificato per erogare la Formazione del Personale della Scuola.</p></div>
<p>Per qualsiasi domanda pu&ograve; rispondere a questa e-mail o scrivere al nostro ufficio
MePA: %(mepa)s.</p>
<p>Cordiali saluti</p>
<p style="margin:26px 0"><a href="%(link)s" style="background:%(o)s;color:%(p)s;
text-decoration:none;font-weight:700;padding:13px 22px;border-radius:10px;display:inline-block">
APRI IL PREVENTIVO</a></p>
<p style="color:#6d817f;font-size:13px;margin-top:26px">Gruppo Spaggiari Parma S.p.A. &middot;
Via Bernini 22/A, 43126 Parma &middot; P.IVA 00150470342<br>
Nicola de Cesare, Amministratore Delegato</p></div>""" % {
        "nome": d["nome"], "numero": d["numero"], "scuola": d["scuola"], "righe": righe,
        "sconto": sconto, "tot": euro(d["netto"]), "come": come, "link": d["link"],
        "mepa": mepa,
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
    # parola intera: "Provaglio d'Iseo" e' un comune con scuole vere
    if re.match(r"PROVA\b", scuola.upper()):
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

    # il contatto serve per intestare il preventivo alla scuola e per agganciare
    # la trattativa: si cerca prima di creare il documento
    cerca = hs("/crm/v3/objects/contacts/search", {"filterGroups": [{"filters": [
        {"propertyName": "email", "operator": "EQ", "value": v["email"]}]}],
        "properties": ["associatedcompanyid", "codice_meccanografico", "codice_cliente",
                       "address", "city", "zip", "state", "hubspot_owner_id"], "limit": 1}, "POST")
    dati_contatto = cerca["results"][0]["properties"] if cerca.get("results") else {}
    contatto = cerca["results"][0]["id"] if cerca.get("results") else None
    azienda = dati_contatto.get("associatedcompanyid")
    destinatario = dati_scuola(v, azienda, dati_contatto)
    agente, scuola_id = agente_di_zona(
        azienda, v.get("codice_meccanografico") or dati_contatto.get("codice_meccanografico"))
    if not agente:
        agente = ripiego()
        motivo = "scuola senza agente di zona attivo: a turno"
        print("  nessun agente di zona: a turno a %s" % agente)
    else:
        motivo = "agente di zona della scuola"
        print("  agente di zona: %s" % agente)

    # se corsi-trattative e' passato prima, la trattativa c'e' gia': si riusa
    if not ripresa:
        esistente = trattativa_dell_invio(inv["submittedAt"])
        if esistente:
            ripresa = esistente["id"]
            hs("/crm/v3/objects/deals/%s" % ripresa,
               {"properties": {"chiave_richiesta_corsi": chiave}}, "PATCH")
            print("  uso la trattativa %s gia' aperta per questa richiesta" % ripresa)

    if ripresa:
        # un giro precedente si e' fermato per strada: si riprende da li' invece
        # di rifare tutto, cosi' la scuola non riceve due preventivi
        trattativa = ripresa
        print("  riprendo la trattativa %s rimasta a meta'" % trattativa)
    else:
        numero = v.get("numero_corsi") or str(len(righe))
        totale = v.get("totale_preventivo_corsi") or str(netto)
        corpo = ("Richiesta dal catalogo corsi.\n\nCorsi richiesti (%s):\n%s\n\n"
                 "Sconto applicato: %s%%\nTotale: %s euro\n\n"
                 "Chi scrive: %s %s - %s\nTelefono: %s\nE-mail: %s\n\nNote: %s"
                 % (numero, v.get("corsi_richiesti") or "", v.get("sconto_corsi") or "0",
                    totale, v.get("firstname", ""), v.get("lastname", ""), v.get("ruolo", ""),
                    v.get("mobilephone", ""), v.get("email", ""), v.get("message", "")))
        d = hs("/crm/v3/objects/deals", {"properties": {
            "dealname": ("Corsi %s - %s" % (numero, scuola))[:200], "pipeline": PIPELINE,
            "dealstage": STADIO_RICHIESTA, "amount": totale,
            "hubspot_owner_id": agente or "",
            "description": corpo[:60000],
            MARCATORE: str(inv["submittedAt"]), "chiave_richiesta_corsi": chiave}}, "POST")
        if "_err" in d:
            print("  trattativa NON creata:", d["_msg"])
            return
        trattativa = d["id"]

    # il preventivo e' di chi segue la trattativa, chiunque l'abbia aperta
    responsabile = (hs("/crm/v3/objects/deals/%s?properties=hubspot_owner_id" % trattativa)
                    .get("properties", {}).get("hubspot_owner_id") or "")

    ids = figli(trattativa, "line_items") if ripresa else []
    for r in (righe if not ids else []):
        li = hs("/crm/v3/objects/line_items", {"properties": {
            # la data sta nel nome della riga: il modello stampa solo quello, e
            # con piu' edizioni dello stesso corso la data e' cio' che le distingue
            "name": " — ".join(x for x in (r["corso"], r["licenza"], r["quando"]) if x),
            "hs_sku": r["codice"],
            "price": str(r["prezzo"]), "quantity": "1",
            "description": "Corso online - %s" % r["quando"],
            "hs_discount_percentage": str(sconto)}}, "POST")
        if "_err" in li:
            print("  riga NON creata:", li["_msg"])
            continue
        ids.append(li["id"])
        lega("line_items", li["id"], "deals", trattativa)


    prev_esistente = (figli(trattativa, "quotes") or [None])[0] if ripresa else None
    note = (NOTE_UNO if len(righe) == 1 else
            NOTE_PIU + (NOTA_SCONTO if sconto else "") + NOTA_CONTATTO) + PARITARIE
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
        # senza azienda sul contatto vale la scuola trovata dal meccanografico:
        # l'agente deve ritrovare la trattativa sulla scheda della sua scuola
        if azienda or scuola_id:
            lega("quotes", prev, "companies", azienda or scuola_id)
            lega("deals", trattativa, "companies", azienda or scuola_id)

    # un preventivo gia' pubblicato HubSpot lo considera chiuso: ripubblicarlo
    # darebbe errore, quindi in ripresa si pubblica solo cio' che e' ancora bozza
    gia_online = prev_esistente and hs(
        "/crm/v3/objects/quotes/%s?properties=hs_quote_link" % prev
    ).get("properties", {}).get("hs_quote_link")
    if not gia_online:
        r = hs("/crm/v3/objects/quotes/%s" % prev, {"properties": {
            "hs_slug": uuid.uuid4().hex[:20], "hs_domain": DOMINIO,
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
    testo = corpo_email({"nome": " ".join(x for x in (v.get("firstname"), v.get("lastname"))
                                          if x).strip() or "Dirigente", "scuola": scuola,
                         "numero": dati["hs_quote_number"], "link": dati["hs_quote_link"],
                         "righe": righe, "lordo": lordo, "netto": netto, "sconto": sconto})
    try:
        da = invia(v["email"], None,
                   "Preventivo n. %s · Corsi di formazione Spaggiari" % dati["hs_quote_number"],
                   testo, ("Preventivo-%s.pdf" % dati["hs_quote_number"], pdf))
    except Exception as e:
        print("  email NON partita (%s): riprovo al prossimo giro" % type(e).__name__)
        return
    registra(chiave)
    ora = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    hs("/crm/v3/objects/deals/%s" % trattativa,
       {"properties": {"dealstage": STADIO_INVIATO, "preventivo_inviato_il": ora}}, "PATCH")
    if contatto:
        hs("/crm/v3/objects/contacts/%s" % contatto,
           {"properties": {"ultimo_preventivo_corsi": chiave}}, "PATCH")
    print("  preventivo %s inviato a %s da %s" % (dati["hs_quote_number"], v["email"], da))
    # Avviso sul cellulare lasciato nel modulo: dice che il preventivo e'
    # arrivato per e-mail. WhatsApp a chi l'ha chiesto, altrimenti SMS; se
    # WhatsApp non riesce si ripiega sull'SMS. Se il numero e' un fisso o
    # l'invio non riesce, pazienza: l'e-mail e' gia' partita.
    try:
        msisdn = numero_cellulare(v.get("mobilephone"))
        nome_sms = " ".join(x for x in (v.get("firstname"), v.get("lastname")) if x).strip()
        wa_ok = False
        if msisdn:
            try:
                if vuole_whatsapp(v, contatto):
                    wa_ok, esito_wa = invia_whatsapp(msisdn, nome_sms, dati["hs_quote_number"],
                                                     dati["hs_quote_link"])
                    print("  WhatsApp a +%s...%s: %s" % (msisdn[:4], msisdn[-2:], esito_wa))
            except Exception as e:
                print("  WhatsApp non inviato (%s): ripiego sull'SMS" % type(e).__name__)
        if msisdn and not wa_ok:
            esito = invia_sms(msisdn, testo_sms(nome_sms, dati["hs_quote_number"]))
            print("  SMS a +%s...%s: %s" % (msisdn[:4], msisdn[-2:], esito))
        elif not msisdn:
            print("  SMS non inviato: il numero del modulo non e' un cellulare")
    except Exception as e:
        print("  SMS non inviato (%s)" % type(e).__name__)
    try:
        allinea_richiamata(contatto, trattativa, responsabile, inv["submittedAt"], scuola)
    except Exception as e:            # mai far fallire un invio gia' riuscito
        print("  richiamata non allineata (%s)" % type(e).__name__)
    try:
        avviso_controllo(scuola, responsabile, motivo, trattativa, len(righe), netto)
    except Exception as e:            # l'avviso non deve mai bloccare il resto
        print("  avviso di controllo non inviato (%s)" % type(e).__name__)


def main():
    prova = "--prova" in sys.argv
    da = int((datetime.datetime.now() - datetime.timedelta(hours=ORE_INDIETRO)).timestamp() * 1000)
    soglia = max(da, DA_QUANDO)
    # Le richieste arrivano dalla piu' recente: si sfoglia finche' si scende sotto
    # la soglia. Con una pagina sola (50) una giornata di campagna poteva far
    # uscire dalla lista una richiesta rimasta indietro.
    nuovi, dopo = [], None
    for _ in range(40):
        pagina = hs("/form-integrations/v1/submissions/forms/%s?limit=50%s"
                    % (MODULO, "&after=" + dopo if dopo else ""))
        risultati = pagina.get("results", [])
        nuovi += [x for x in risultati if x["submittedAt"] >= soglia]
        dopo = (pagina.get("paging") or {}).get("next", {}).get("after")
        if not dopo or not risultati or risultati[-1]["submittedAt"] < soglia:
            break
    print("richieste nelle ultime %d ore: %d" % (ORE_INDIETRO, len(nuovi)))
    errori = 0
    for inv in reversed(nuovi):
        # una richiesta che va storta non deve fermare le altre: resta senza
        # data di invio e il giro successivo la riprende
        try:
            lavora(inv, prova)
        except Exception as e:
            errori += 1
            print("  ERRORE su una richiesta (%s: %s): la riprendo al prossimo giro"
                  % (type(e).__name__, str(e)[:160]))
    if errori:
        print("\nrichieste con errore in questo giro: %d" % errori)
    print("\nfatto.")


if __name__ == "__main__":
    main()
