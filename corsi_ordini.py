# -*- coding: utf-8 -*-
"""Ordini dei corsi in diretta -> evento, link di nomina, e-mail alla scuola (da HubSpot).

Per ogni ordine di corso arrivato (affare «<numero> - ERP» in «Chiuso Vinto», riga con codice
WBRELE<corso>-1|3|I, stesso anno):
  1. l'evento riservato del corso c'e'? Se manca lo crea dal palinsesto (corsi_palinsesto.json);
  2. prepara il link di nomina firmato (stesso codice di eventi-dashboard/link_nomina.js);
  3. sceglie i destinatari: chi ha chiesto il preventivo dal sito (affare «Formazione»), altrimenti il
     dirigente e la DSGA della scuola in HubSpot. Se i candidati non sono univoci NON sceglie: segnala;
  4. manda l'e-mail dal portale HubSpot (invio transazionale, modello «CORSI - Nomina partecipanti»);
  5. scrive nel registro `ordini_corsi_gestiti.txt` cosi' non parte due volte.

L'invio alla scuola e' spento finche' la variabile CORSI_INVIO_AUTOMATICO non vale 1: senza, il giro
prepara tutto e avvisa Andrea Pizzola e Malerba con un riepilogo (una volta per ordine).

Uso:
  python corsi_ordini.py                       giro normale
  python corsi_ordini.py --prova               elenca senza creare ne' inviare niente
  python corsi_ordini.py --ordine 2026EN16480 --a mail1,mail2 --invia
                                               invio manuale con destinatari scelti da una persona
"""
import base64
import datetime
import hashlib
import hmac
import io
import json
import os
import re
import sys
import time
import smtplib
import ssl
import urllib.parse
from email.message import EmailMessage

QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, QUI)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import preventivi_corsi as C                                  # noqa: E402

EV, REG = "2-143900361", "2-143900355"
EMAIL_ID = 483372784867                  # «CORSI - Nomina partecipanti (invio da API)» = modello per UN corso
# Un modello per ogni numero di corsi (1-8): le condizioni {% if %} di HubSpot nelle e-mail automatiche risultano sempre vere,
# quindi i blocchi dei corsi 2-8 comparivano vuoti anche con un corso solo. Senza condizioni, ogni modello ha esattamente i suoi blocchi.
EMAIL_PER_N = {1: 483372784867, 2: 485623493847, 3: 485623493841, 4: 485683370198,
               5: 485683370193, 6: 485683370187, 7: 485623493828, 8: 485615264984}
PAGINA = "https://www.spaggiari.eu/indica-partecipanti-corso"
CORSO = re.compile(r"^WBRELE?([A-Z0-9]+)-(1|3|I)$")
CC_FISSI = []                              # in copia visibile solo l'agente di zona
# indirizzi nelle variabili del repository (CORSI_BCC, CORSI_AVVISO_A): il codice e' pubblico, gli indirizzi no
BCC = [x for x in os.environ.get("CORSI_BCC", "").replace(" ", "").split(",") if x]
AVVISO_A = [x for x in os.environ.get("CORSI_AVVISO_A", "").replace(" ", "").split(",") if x]
CHIUSURA_MIN = 30                          # nomine aperte fino a 30 minuti prima dell'inizio (come la funzione di nomina)
REGISTRO = os.path.join(QUI, "ordini_corsi_gestiti.txt")
GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre",
        "novembre", "dicembre"]
MAIL_LIBERE = re.compile(r"@(gmail|yahoo|libero|hotmail|outlook|live|icloud|tiscali|virgilio|alice|fastweb)\.", re.I)


# ---------------------------------------------------------------- utilita'
def b64u(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def chiave():
    k = os.environ.get("CHIAVE_NOMINE_CORSI")
    if not k:
        k = io.open(os.path.expanduser("~/.chiave_nomine_corsi.txt"), encoding="utf-8").read()
    return k.strip()


def codice(ordine, cliente, scuola, evento, posti, azienda, minuti_prima=None):
    dati = {"o": ordine, "c": cliente, "s": scuola, "e": str(evento), "n": posti, "k": azienda}
    if minuti_prima is not None:
        dati["x"] = minuti_prima          # nomina aperta fino a N minuti prima (ordine arrivato a ridosso della diretta)
    payload = b64u(json.dumps(dati, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    firma = b64u(hmac.new(chiave().encode(), payload.encode(), hashlib.sha256).digest())[:32]
    return payload + "." + firma


def pulito(nome):
    n = re.sub(r"^[A-Z0-9]{8,12}\s*-\s*", "", nome or "")
    n = re.sub(r"SUPERIORE(?=[A-Z])", "SUPERIORE ", n)
    return re.sub(r"(^|[\s'.\-])([a-zàèéìòù])", lambda m: m.group(1) + m.group(2).upper(), n.lower())


def registro():
    try:
        return {r.strip() for r in io.open(REGISTRO, encoding="utf-8") if r.strip()}
    except FileNotFoundError:
        return set()


def segna(chiave_):
    with io.open(REGISTRO, "a", encoding="utf-8") as f:
        f.write(chiave_ + "\n")


def ora_italiana(iso):
    from zoneinfo import ZoneInfo
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Rome"))


def quando(iso_inizio, iso_fine):
    a, b = ora_italiana(iso_inizio), ora_italiana(iso_fine)
    return "%s %d %s %d, ore %s–%s" % (GIORNI[a.weekday()], a.day, MESI[a.month - 1], a.year, a.strftime("%H:%M"), b.strftime("%H:%M"))


MINUTI_ORDINE_TARDIVO = 30   # ordine arrivato dopo la chiusura normale: si nomina fino a 30 minuti prima


def e_tardivo(iso_inizio):
    return False        # con la chiusura a 30 minuti per tutti il codice speciale non serve piu'


def chiusura(iso_inizio, minuti_prima=None):
    c = ora_italiana(iso_inizio) - (datetime.timedelta(minutes=minuti_prima) if minuti_prima is not None else datetime.timedelta(minutes=CHIUSURA_MIN))
    return "alle %s di %s %d %s" % (c.strftime("%H:%M"), GIORNI[c.weekday()], c.day, MESI[c.month - 1])


# ---------------------------------------------------------------- evento
def palinsesto():
    return json.load(io.open(os.path.join(QUI, "corsi_palinsesto.json"), encoding="utf-8"))


def utc(data, ora):
    a, m, g = map(int, data.split("-"))
    h, mi = map(int, ora.split(":"))
    legale = datetime.datetime(a, m, g, h, mi) < datetime.datetime(2026, 10, 25, 1, 0)
    return datetime.datetime(a, m, g, h - (2 if legale else 1), mi, tzinfo=datetime.timezone.utc)


def relatori_da_landing(voce):
    """I relatori del corso come li mostra la landing /corsi-formazione (gia' collegati a hapily): fa fede la pagina.
    Se la pagina non risponde si usa il campo `relatori` del palinsesto."""
    try:
        import urllib.request
        h = urllib.request.urlopen("https://www.spaggiari.eu/corsi-formazione", timeout=30).read().decode("utf-8")
        for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', h, re.S):
            try:
                j = json.loads(m.group(1))
            except Exception:
                continue
            if isinstance(j, dict) and j.get("@type") == "ItemList":
                for it in j["itemListElement"]:
                    ci = (it.get("item") or it).get("hasCourseInstance") or {}
                    if (ci.get("startDate") or "").startswith("%sT%s" % (voce["data"], voce["inizio"])):
                        nomi = [x.get("name") for x in ci.get("instructor", []) if x.get("name")]
                        if nomi:
                            return nomi
    except Exception:
        pass
    return voce.get("relatori", [])


def evento_per(base, crea, prova):
    """(id, proprieta') dell'evento del corso; lo crea dal palinsesto se manca. None se non si puo'."""
    voce = next((x for x in palinsesto() if x["base"] == base), None)
    if not voce:
        return None, "codice corso fuori dal palinsesto"
    a, m, g = map(int, voce["data"].split("-"))
    ext = "CF-%s-%02d%02d" % (voce["base"], g, m)
    r = C.hs("/crm/v3/objects/%s/search" % EV, {"filterGroups": [{"filters": [
        {"propertyName": "external_id", "operator": "EQ", "value": ext}]}],
        "properties": ["name", "start_datetime", "end_datetime", "meeting_link"], "limit": 1}, "POST").get("results", [])
    if r:
        return r[0], None
    ini, fin = utc(voce["data"], voce["inizio"]), utc(voce["data"], voce["fine"])
    if ini < datetime.datetime.now(datetime.timezone.utc):
        return None, "la data del corso (%s) e' gia' passata" % voce["data"]
    if not crea or prova:
        return None, "evento da creare (%s)" % ext
    d = datetime.date(a, m, g)
    props = {
        "name": "Corso | " + voce["titolo"], "brand": "Spaggiari", "type": "Virtual", "external_id": ext,
        "start_datetime": ini.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "end_datetime": fin.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "display_start_date": "%s %d %s %d" % (GIORNI[d.weekday()], g, MESI[m - 1], a),
        "display_end_date": "%s %d %s %d" % (GIORNI[d.weekday()], g, MESI[m - 1], a),
        "display_start_time": voce["inizio"], "display_end_time": voce["fine"], "display_timezone": "Europe/Amsterdam",
        "display_timezone_alt": "GMT+2" if ini < datetime.datetime(2026, 10, 25, 1, 0, tzinfo=datetime.timezone.utc) else "GMT+1",
        "locale": "it_IT", "venue": "Evento online", "sessions": "Yes", "paid_event": "false", "is_published": "false",
        "tag": "Formazione", "description": "Corso di formazione riservato a chi ha acquistato la licenza. Codice %s." % voce["codice"]}
    n = C.hs("/crm/v3/objects/%s" % EV, {"properties": props}, "POST")
    props["id"] = n["id"]
    # i relatori del corso (oggetto «hapily speaker»), dal palinsesto: da loro il link di avvio un'ora prima
    for nome in relatori_da_landing(voce):
        sp = C.hs("/crm/v3/objects/2-144750696/search", {"filterGroups": [{"filters": [
            {"propertyName": "name", "operator": "EQ", "value": nome}]}], "properties": ["name"], "limit": 1}, "POST").get("results", [])
        if sp:
            C.hs("/crm/v4/objects/%s/%s/associations/2-144750696/%s" % (EV, n["id"], sp[0]["id"]),
                 [{"associationCategory": "USER_DEFINED", "associationTypeId": 343}], "PUT")
    return {"id": n["id"], "properties": props}, None


# ---------------------------------------------------------------- destinatari
def mail_istituzionale(azienda_id):
    """<codice meccanografico>@istruzione.it: l'indirizzo istituzionale di ogni scuola STATALE, letto dalla segreteria e
    quello a cui arriva la posta degli ordini MePA. Si aggiunge sempre ai destinatari: chi ha chiesto il preventivo
    spesso non e' chi gestisce il corso (Andrea, 6/10/2026: le scuole non trovano l'invito)."""
    try:
        p = C.hs("/crm/v3/objects/companies/%s?properties=name,descrizione_tipo_cliente" % azienda_id)["properties"]
    except Exception:
        return None
    m = re.match(r"^\s*([A-Za-z]{4}[A-Za-z0-9]{6})\b", p.get("name") or "")
    if m and (p.get("descrizione_tipo_cliente") or "").startswith("Pub-"):
        return m.group(1).lower() + "@istruzione.it"
    return None


def destinatari(azienda_id):
    """Come _destinatari_base, piu' l'indirizzo istituzionale della scuola statale; se non si riesce a scegliere nessuno
    (dirigente/DSGA ambigui) l'indirizzo istituzionale basta: il giro non si ferma piu' ad aspettare una persona."""
    out, motivo = _destinatari_base(azienda_id)
    ist = mail_istituzionale(azienda_id)
    if ist and ist not in [e.lower() for e, _ in out]:
        out = list(out) + [(ist, "Segreteria")]
        motivo = (motivo + " + indirizzo istituzionale della scuola") if len(out) > 1 else "indirizzo istituzionale della scuola"
    return out, motivo


def _destinatari_base(azienda_id):
    """(lista di (email, nome), motivo). Lista vuota = da decidere a mano."""
    # 1. chi ha chiesto il preventivo dal sito (affare «Formazione»)
    ass = C.hs("/crm/v4/objects/companies/%s/associations/deals" % azienda_id).get("results", [])
    for x in ass:
        d = C.hs("/crm/v3/objects/deals/%s?properties=pipeline,createdate" % x["toObjectId"])
        if d["properties"].get("pipeline") == "4128670920":
            cs = C.hs("/crm/v4/objects/deals/%s/associations/contacts" % x["toObjectId"]).get("results", [])
            if cs:
                r = C.hs("/crm/v3/objects/contacts/batch/read", {"properties": ["email", "firstname", "lastname"],
                         "inputs": [{"id": str(c["toObjectId"])} for c in cs[:3]]}, "POST")
                out = [(k["properties"]["email"], ((k["properties"].get("firstname") or "") + " " + (k["properties"].get("lastname") or "")).strip())
                       for k in r.get("results", []) if k["properties"].get("email")]
                if out:
                    return out, "ha chiesto il preventivo dal sito"
    # 2. dirigente e DSGA, se univoci
    cs = C.hs("/crm/v4/objects/companies/%s/associations/contacts?limit=100" % azienda_id).get("results", [])
    if not cs:
        return [], "la scuola non ha contatti"
    r = C.hs("/crm/v3/objects/contacts/batch/read", {"properties": ["email", "firstname", "lastname", "jobtitle", "ruolo",
             "hs_email_last_open_date", "hs_email_last_click_date", "hs_email_bounce", "hs_email_optout"],
             "inputs": [{"id": str(c["toObjectId"])} for c in cs[:100]]}, "POST").get("results", [])
    ruoli = {"dirigente": {}, "dsga": {}}
    for k in r:
        p = k["properties"]
        jt = ((p.get("jobtitle") or "") + " " + (p.get("ruolo") or "")).lower()
        ruolo = "dirigente" if "dirig" in jt else ("dsga" if ("dsga" in jt or "direttore" in jt) else None)
        if not ruolo or not p.get("email") or p.get("hs_email_optout") == "true" or p.get("hs_email_bounce"):
            continue
        persona = ((p.get("firstname") or "") + " " + (p.get("lastname") or "")).strip().lower()
        attivita = max(p.get("hs_email_last_open_date") or "", p.get("hs_email_last_click_date") or "")
        ruoli[ruolo].setdefault(persona, []).append((attivita, p["email"]))
    out, motivi = [], []
    for ruolo, persone in ruoli.items():
        if len(persone) == 1:
            nome, mails = next(iter(persone.items()))
            mails.sort(key=lambda t: (not MAIL_LIBERE.search(t[1]), t[0]), reverse=True)
            out.append((mails[0][1], nome.title()))
        elif len(persone) > 1:
            motivi.append("%d persone diverse con ruolo %s (%s)" % (len(persone), ruolo, ", ".join(sorted(persone))))
        else:
            motivi.append("nessun contatto con ruolo %s" % ruolo)
    if motivi:
        return [], "; ".join(motivi)
    return out, "dirigente e DSGA della scuola"


def email_agente(deal_id):
    d = C.hs("/crm/v3/objects/deals/%s?properties=hubspot_owner_id" % deal_id)["properties"]
    if not d.get("hubspot_owner_id"):
        return None
    try:
        return C.hs("/crm/v3/owners/%s" % d["hubspot_owner_id"]).get("email")
    except Exception:
        return None


# ---------------------------------------------------------------- invio da HubSpot
def invia(a, cc, oggetto_dati):
    n = 1 + sum(1 for i in range(2, 9) if oggetto_dati.get("corso_%d" % i))
    corpo = {"emailId": EMAIL_PER_N.get(n, EMAIL_ID), "message": {"to": a[0], "cc": [x for x in (a[1:] + cc) if x], "bcc": BCC},
             "customProperties": oggetto_dati}
    return C.hs("/marketing/v3/transactional/single-email/send", corpo, "POST")


RIEPILOGO = []


def avviso_interno(oggetto, righe):
    """accoda l'avviso: a fine giro ne parte UNO solo con tutti gli ordini"""
    RIEPILOGO.append((oggetto, righe))


def spedisci_riepilogo():
    if not RIEPILOGO:
        return
    righe = []
    for oggetto, rr in RIEPILOGO:
        righe.append("<b style='color:#06484b'>%s</b>" % oggetto)
        righe += rr
        righe.append("&nbsp;")
    _manda("Ordini corsi in diretta: %d novità" % len(RIEPILOGO), righe)


def _manda(oggetto, righe):
    utente, pw = os.environ.get("SMTP_CORSI_USER"), os.environ.get("SMTP_CORSI_PASS")
    if not (utente and pw):
        print("  (avviso interno non inviato: credenziali SMTP assenti)")
        return
    m = EmailMessage()
    m["From"] = "Spaggiari <%s>" % C.MITTENTE
    m["To"] = ", ".join(AVVISO_A)
    m["Subject"] = oggetto
    corpo = "".join("<p style='margin:0 0 8px 0'>%s</p>" % r for r in righe)
    m.set_content("Riepilogo ordini corsi: serve un lettore HTML.")
    m.add_alternative("<div style='font-family:Arial,sans-serif;font-size:15px;color:#0E2A4D'>%s</div>" % corpo, subtype="html")
    s = smtplib.SMTP(C.SMTP_HOST, C.SMTP_PORTA, timeout=60)
    s.starttls(context=ssl.create_default_context())
    s.login(utente, pw)
    s.send_message(m)
    s.quit()


# ---------------------------------------------------------------- ordini
def ordini_nuovi(giorni=30):
    pl = C.hs("/crm/v3/pipelines/deals")
    stadio = {s["id"]: s["label"] for p in pl["results"] for s in p["stages"]}
    righe, dopo = [], None
    for _ in range(10):
        r = C.hs("/crm/v3/objects/line_items/search", {"filterGroups": [{"filters": [
            {"propertyName": "hs_sku", "operator": "CONTAINS_TOKEN", "value": "WBREL*"},
            {"propertyName": "createdate", "operator": "GTE", "value": str(int((time.time() - giorni * 86400) * 1000))}]}],
            "properties": ["hs_sku", "name", "quantity"], "limit": 100, **({"after": dopo} if dopo else {})}, "POST")
        righe += [x for x in r.get("results", []) if CORSO.match(x["properties"].get("hs_sku") or "")]
        dopo = (r.get("paging") or {}).get("next", {}).get("after")
        if not dopo:
            break
    anno = str(datetime.date.today().year)
    attuali = {x["base"] for x in palinsesto()}      # solo i corsi del catalogo di oggi (non i vecchi codici)
    righe = [x for x in righe if CORSO.match(x["properties"]["hs_sku"]).group(1) in attuali]
    out = {}
    for x in righe:
        for d in C.hs("/crm/v4/objects/line_items/%s/associations/deals" % x["id"]).get("results", []):
            deal = C.hs("/crm/v3/objects/deals/%s?properties=dealname,dealstage" % d["toObjectId"])["properties"]
            num = re.sub(r"\s*-\s*ERP\s*$", "", deal.get("dealname") or "")
            if not num.startswith(anno) or "vinto" not in (stadio.get(deal.get("dealstage"), "")).lower():
                continue
            voce = out.setdefault((num, x["properties"]["hs_sku"]), {"deal": d["toObjectId"], "num": num, "sku": x["properties"]["hs_sku"], "qta": 0})
            try:
                voce["qta"] += max(1, int(float(x["properties"].get("quantity") or 1)))     # 2 copie di un corso da 1 posto = 2 posti
            except ValueError:
                voce["qta"] += 1
    return list(out.values())


def prepara(o, prova):
    """Dati di UNA riga d'ordine (un corso): evento, link, orari. (None, motivo) se non e' pronta."""
    m = CORSO.match(o["sku"])
    base, tier = m.group(1), m.group(2)
    posti = 0 if tier == "I" else int(tier) * max(1, o.get("qta") or 1)
    az = C.hs("/crm/v4/objects/deals/%s/associations/companies" % o["deal"]).get("results", [])
    if not az:
        return None, "affare senza scuola collegata"
    azienda = C.hs("/crm/v3/objects/companies/%s?properties=name,codice_cliente" % az[0]["toObjectId"])["properties"]
    azienda_id = az[0]["toObjectId"]
    ev, perche = evento_per(base, crea=not prova, prova=prova)
    print("  %s · %s · posti %s · evento: %s" % (o["num"], o["sku"], posti or "illimitati", ev["id"] if ev else perche))
    if not ev:
        return {"azienda": azienda, "azienda_id": azienda_id, "mancante": perche}, perche
    p = ev["properties"] if "start_datetime" in ev["properties"] else C.hs(
        "/crm/v3/objects/%s/%s?properties=name,start_datetime,end_datetime" % (EV, ev["id"]))["properties"]
    tardi = MINUTI_ORDINE_TARDIVO if e_tardivo(p["start_datetime"]) else None
    link = PAGINA + "?o=" + codice(o["num"], azienda.get("codice_cliente") or "", pulito(azienda.get("name")), ev["id"], posti, azienda_id, tardi)
    return {"azienda": azienda, "azienda_id": azienda_id, "sku": o["sku"],
            "corso": re.sub(r"^Corso \| ", "", p.get("name") or base), "quando": quando(p["start_datetime"], p["end_datetime"]),
            "chiusura": chiusura(p["start_datetime"], tardi), "link": link, "inizio_iso": p["start_datetime"]}, None


def dati_email(voci):
    """Proprieta' dell'e-mail: il primo corso nei campi base, gli altri come corso_2, quando_2, link_2, ..."""
    primo = voci[0]
    d = {"corso": primo["corso"], "scuola": pulito(primo["azienda"].get("name")), "quando": primo["quando"],
         "chiusura": primo["chiusura"], "link": primo["link"],
         "oggetto": ("Corso «%s»: indica chi partecipa" % primo["corso"]) if len(voci) == 1 else "I tuoi corsi in diretta: indica chi partecipa"}
    for i in range(2, 9):      # il modello vuole TUTTE le proprieta' in ogni invio: quelle senza corso vanno vuote
        v = voci[i - 1] if i - 1 < len(voci) else None
        d["corso_%d" % i], d["quando_%d" % i], d["link_%d" % i] = (v["corso"], v["quando"], v["link"]) if v else ("", "", "")
    return d


def possibile_duplicato(num, azienda_id, skus, gia):
    """Numero di un altro ordine della stessa scuola, con gli stessi corsi, gia' invitato e creato negli ultimi 10 giorni."""
    try:
        tutti = {}
        for o in ordini_nuovi(30):
            if o["num"] != num and "%s|%s|inviato" % (o["num"], o["sku"]) in gia:
                tutti.setdefault(o["num"], {"deal": o["deal"], "sku": set()})["sku"].add(o["sku"])
        limite = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=10)).isoformat()
        for n, x in tutti.items():
            if not skus <= x["sku"]:
                continue
            az = C.hs("/crm/v4/objects/deals/%s/associations/companies" % x["deal"]).get("results", [])
            if not az or str(az[0]["toObjectId"]) != str(azienda_id):
                continue
            creato = C.hs("/crm/v3/objects/deals/%s?properties=createdate" % x["deal"])["properties"].get("createdate") or ""
            if creato >= limite[:19]:
                return n
    except Exception as e:
        print("  controllo doppioni non riuscito (%s)" % type(e).__name__)
    return None


def lavora_ordine(righe, auto, prova, destinatari_forzati=None):
    """Tutte le righe-corso di UN ordine: una sola e-mail alla scuola, con un blocco e un pulsante per corso."""
    num = righe[0]["num"]
    gia = registro()
    righe = [o for o in righe if "%s|%s|inviato" % (o["num"], o["sku"]) not in gia]
    if not righe:
        return
    voci, mancanti = [], []
    for o in righe:
        v, motivo = prepara(o, prova)
        if v and not v.get("mancante"):
            voci.append((o, v))
        elif v:
            mancanti.append((o, v, motivo))
    for o, v, motivo in mancanti:
        k = "%s|%s|avvisato-evento" % (o["num"], o["sku"])
        if k not in gia and not prova:
            avviso_interno("Ordine di corso senza evento: %s" % o["num"], [
                "L'ordine <b>%s</b> (%s) riguarda il corso %s, ma %s." % (o["num"], pulito(v["azienda"].get("name")), o["sku"], motivo),
                "Quando l'evento esiste il giro prepara da solo il link per la scuola."])
            segna(k)
    # Guardia 1 (audit 7/10): corso gia' iniziato o a meno di 30 minuti dall'inizio -> il link di nomina sarebbe gia' chiuso.
    # Non si invita la scuola: avviso interno, si decide a mano (nuova data, partecipazione fuori orario con lo strumento interno).
    ora = datetime.datetime.now(datetime.timezone.utc)
    validi = []
    for o, v in voci:
        ini = datetime.datetime.fromisoformat(v["inizio_iso"].replace("Z", "+00:00"))
        if not destinatari_forzati and ini - datetime.timedelta(minutes=CHIUSURA_MIN) <= ora:
            k = "%s|%s|avvisato-corso-iniziato" % (o["num"], o["sku"])
            if k not in gia and not prova:
                avviso_interno("Ordine per un corso gia' iniziato o imminente: %s" % o["num"], [
                    "L'ordine <b>%s</b> (%s) riguarda <b>%s</b> (%s), arrivato a corso iniziato o a meno di 30 minuti dall'inizio." % (
                        o["num"], pulito(v["azienda"].get("name")), v["corso"], v["quando"]),
                    "Non ho mandato l'invito alla scuola: il link di nomina sarebbe gia' chiuso. Serve scegliere a mano: nuova data del corso oppure partecipazione "
                    "subito con lo strumento interno (strumenti/assistenza-nomine-corsi)."])
                segna(k)
            print("     %s: corso gia' iniziato o imminente, nessun invito" % o["sku"])
            continue
        validi.append((o, v))
    voci = validi
    # Guardia 2 (audit 7/10): stesso insieme di corsi gia' ordinato dalla stessa scuola da meno di 10 giorni (doppione dell'ERP):
    # niente seconda e-mail alla scuola; avviso interno, si decide a mano (--ordine N --a ... --invia).
    if voci and not destinatari_forzati and not prova:
        dup = possibile_duplicato(num, voci[0][1]["azienda_id"], {o["sku"] for o, _ in voci}, gia)
        if dup:
            k = "%s|avvisato-duplicato" % num
            if k not in gia:
                avviso_interno("Ordine di corsi forse doppio: %s" % num, [
                    "L'ordine <b>%s</b> di %s ha gli stessi corsi dell'ordine <b>%s</b>, gia' invitato." % (num, pulito(voci[0][1]["azienda"].get("name")), dup),
                    "Non ho mandato un secondo invito. Se e' davvero un ordine nuovo (altri posti): <code>python corsi_ordini.py --ordine %s --invia</code>." % num])
                segna(k)
            print("     ordine %s: possibile doppione di %s, nessun invito" % (num, dup))
            return
    if not voci:
        return
    schede = [v for _, v in voci]
    dati = dati_email(schede)
    if destinatari_forzati:
        dest, motivo = [(e, "") for e in destinatari_forzati], "scelti da una persona"
    else:
        dest, motivo = destinatari(schede[0]["azienda_id"])
    agente = email_agente(voci[0][0]["deal"])
    cc = CC_FISSI + ([agente] if agente and agente not in CC_FISSI else [])
    elenco = "; ".join("%s (%s)" % (v["corso"], v["quando"]) for v in schede)
    if not dest:
        print("     destinatari: DA DECIDERE (il motivo e' nel riepilogo interno)")
        k = "%s|avvisato-destinatari" % num
        if k not in gia and not prova:
            avviso_interno("Ordine di corso: da scegliere a chi mandare il link (%s)" % num, [
                "L'ordine <b>%s</b> di %s riguarda: %s." % (num, dati["scuola"], elenco),
                "Non riesco a scegliere i destinatari da solo: %s." % motivo,
                "Per inviare: <code>python corsi_ordini.py --ordine %s --a mail1,mail2 --invia</code> (o scrivi ad Andrea Pizzola)." % num])
            segna(k)
        return
    print("     destinatari: %d (%s) · cc: %d · ccn: %d · corsi nella stessa e-mail: %d" % (len(dest), motivo, len(cc), len(BCC), len(schede)))
    if prova:
        return
    if not auto:
        k = "%s|pronto" % num
        if k not in gia:
            avviso_interno("Ordine di corso pronto per l'invio: %s" % num, [
                "L'ordine <b>%s</b> di %s riguarda: %s." % (num, dati["scuola"], elenco),
                "Destinatari previsti: %s (%s). In copia: %s. In copia nascosta: voi." % (", ".join(e for e, _ in dest), motivo, ", ".join(cc) or "nessuno"),
                "L'invio automatico e' spento: per mandare l'e-mail dal portale HubSpot dai l'ok ad Andrea Pizzola."])
            segna(k)
        return
    r = invia([e for e, _ in dest], cc, dati)
    print("     inviata da HubSpot:", r.get("status"), r.get("statusId", "")[:20])
    for o, _ in voci:
        segna("%s|%s|inviato" % (o["num"], o["sku"]))


def controllo_consegne():
    """Una nomina con l'indirizzo sbagliato (es. un refuso nel dominio) non riceve mai il link: lo segnala.
    Guarda i partecipanti nominati nelle ultime 48 ore; se la conferma risulta rimbalzata o bloccata avvisa
    Andrea Pizzola e Malerba, una volta sola per indirizzo."""
    gia = registro()
    da = int((time.time() - 48 * 3600) * 1000)
    r = C.hs("/crm/v3/objects/%s/search" % REG, {"filterGroups": [{"filters": [
        {"propertyName": "ordine_corso", "operator": "HAS_PROPERTY"},
        {"propertyName": "hs_createdate", "operator": "GTE", "value": str(da)}]}],
        "properties": ["email", "full_name", "status", "ordine_corso"], "limit": 100}, "POST").get("results", [])
    visti, righe = set(), []
    for x in r:
        p = x["properties"]
        mail = (p.get("email") or "").lower()
        if not mail or mail in visti or p.get("status") == "Canceled" or p.get("ordine_corso", "").startswith("PROVA"):
            continue
        visti.add(mail)
        if "rimbalzo|" + mail in gia:
            continue
        ev = C.hs("/email/public/v1/events?recipient=%s&startTimestamp=%d&limit=100" % (urllib.parse.quote(mail), da)).get("events", [])
        cattivi = [e for e in ev if e.get("type") in ("BOUNCE", "DROPPED", "DEFERRED")]
        consegnata = any(e.get("type") == "DELIVERED" for e in ev)
        if cattivi and not consegnata:
            motivo = str(cattivi[-1].get("response") or cattivi[-1].get("status") or cattivi[-1]["type"])[:120]
            righe.append("<b>%s</b> (%s, ordine %s): la conferma con il link non risulta consegnata - %s" % (
                mail, p.get("full_name") or "", p.get("ordine_corso"), motivo))
            segna("rimbalzo|" + mail)
    if righe:
        avviso_interno("Link non consegnati: controllare gli indirizzi", righe + [
            "Probabile refuso nell'indirizzo indicato dalla scuola: serve farselo correggere e rifare la nomina dalla stessa pagina."])


def main():
    prova = "--prova" in sys.argv
    forzato = None
    if "--ordine" in sys.argv:
        num = sys.argv[sys.argv.index("--ordine") + 1]
        a = sys.argv[sys.argv.index("--a") + 1].split(",") if "--a" in sys.argv else None
        todo = [o for o in ordini_nuovi(60) if o["num"] == num]
        if not todo:
            print("ordine %s non trovato fra gli ordini di corsi in «Chiuso Vinto»" % num)
            return
        lavora_ordine(todo, auto="--invia" in sys.argv, prova=prova, destinatari_forzati=a)
        spedisci_riepilogo()
        return
    auto = os.environ.get("CORSI_INVIO_AUTOMATICO") == "1"
    # ogni cinque minuti basta: il giro gira al minuto per i preventivi
    if not prova and datetime.datetime.now().minute % 5 != 0 and not os.environ.get("FORZA"):
        return
    os_ = ordini_nuovi()
    print("ordini di corsi in «Chiuso Vinto» negli ultimi 30 giorni: %d (invio automatico: %s)" % (len(os_), "ACCESO" if auto else "spento"))
    per_ordine = {}
    for o in os_:
        per_ordine.setdefault(o["num"], []).append(o)
    for num, righe in per_ordine.items():
        try:
            lavora_ordine(righe, auto, prova)
        except Exception as e:
            print("  ERRORE su %s: %s %s" % (num, type(e).__name__, str(e)[:160]))
    try:
        controllo_consegne()
    except Exception as e:
        print("  ERRORE nel controllo consegne: %s %s" % (type(e).__name__, str(e)[:160]))
    try:
        import chiusura_trattative
        chiusura_trattative.main(prova)
    except Exception as e:
        print("  ERRORE nella chiusura trattative: %s %s" % (type(e).__name__, str(e)[:160]))
    try:
        import solleciti_corsi
        solleciti_corsi.solleciti(os_, auto, prova)
    except Exception as e:
        print("  ERRORE nei solleciti: %s %s" % (type(e).__name__, str(e)[:160]))
    spedisci_riepilogo()


if __name__ == "__main__":
    main()
