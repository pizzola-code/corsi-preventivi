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
from email.message import EmailMessage

QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, QUI)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import preventivi_corsi as C                                  # noqa: E402

EV, REG = "2-143900361", "2-143900355"
EMAIL_ID = 483372784867                  # «CORSI - Nomina partecipanti (invio da API)»
PAGINA = "https://www.spaggiari.eu/indica-partecipanti-corso"
CORSO = re.compile(r"^WBRELE?([A-Z0-9]+)-(1|3|I)$")
CC_FISSI = []                              # in copia visibile solo l'agente di zona
BCC = ["pizzola@spaggiari.eu", "malerba@spaggiari.eu", "primiceri@spaggiari.eu", "bertozzi@spaggiari.eu", "maestri@spaggiari.eu"]
AVVISO_A = ["pizzola@spaggiari.eu", "malerba@spaggiari.eu"]
CHIUSURA_ORE = 4
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


def codice(ordine, cliente, scuola, evento, posti, azienda):
    payload = b64u(json.dumps({"o": ordine, "c": cliente, "s": scuola, "e": str(evento), "n": posti, "k": azienda},
                              separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
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


def chiusura(iso_inizio):
    c = ora_italiana(iso_inizio) - datetime.timedelta(hours=CHIUSURA_ORE)
    return "alle %s di %s %d %s" % (c.strftime("%H:%M"), GIORNI[c.weekday()], c.day, MESI[c.month - 1])


# ---------------------------------------------------------------- evento
def palinsesto():
    return json.load(io.open(os.path.join(QUI, "corsi_palinsesto.json"), encoding="utf-8"))


def utc(data, ora):
    a, m, g = map(int, data.split("-"))
    h, mi = map(int, ora.split(":"))
    legale = datetime.datetime(a, m, g, h, mi) < datetime.datetime(2026, 10, 25, 1, 0)
    return datetime.datetime(a, m, g, h - (2 if legale else 1), mi, tzinfo=datetime.timezone.utc)


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
    return {"id": n["id"], "properties": props}, None


# ---------------------------------------------------------------- destinatari
def destinatari(azienda_id):
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
    corpo = {"emailId": EMAIL_ID, "message": {"to": a[0], "cc": [x for x in (a[1:] + cc) if x], "bcc": BCC},
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
            {"propertyName": "hs_sku", "operator": "CONTAINS_TOKEN", "value": "WBRELE*"},
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
            out.setdefault((num, x["properties"]["hs_sku"]), {"deal": d["toObjectId"], "num": num, "sku": x["properties"]["hs_sku"]})
    return list(out.values())


def lavora(o, auto, prova, destinatari_forzati=None):
    m = CORSO.match(o["sku"])
    base, tier = m.group(1), m.group(2)
    posti = 0 if tier == "I" else int(tier)
    chiave_ordine = "%s|%s" % (o["num"], o["sku"])
    gia = registro()
    if chiave_ordine + "|inviato" in gia:
        return
    az = C.hs("/crm/v4/objects/deals/%s/associations/companies" % o["deal"]).get("results", [])
    if not az:
        print("  %s: affare senza scuola collegata" % o["num"])
        return
    azienda = C.hs("/crm/v3/objects/companies/%s?properties=name,codice_cliente" % az[0]["toObjectId"])["properties"]
    azienda_id = az[0]["toObjectId"]
    ev, perche = evento_per(base, crea=not prova, prova=prova)
    nome_corso = (ev or {}).get("properties", {}).get("name", "").replace("Corso | ", "") if ev else base
    print("  %s · %s · %s · posti %s · evento: %s" % (o["num"], azienda.get("codice_cliente"), o["sku"], posti or "illimitati",
                                                      ev["id"] if ev else perche))
    if not ev:
        if chiave_ordine + "|avvisato-evento" not in gia and not prova:
            avviso_interno("Ordine di corso senza evento: %s" % o["num"], [
                "L'ordine <b>%s</b> (%s) riguarda il corso %s, ma %s." % (o["num"], pulito(azienda.get("name")), o["sku"], perche),
                "Quando l'evento esiste il giro prepara da solo il link per la scuola."])
            segna(chiave_ordine + "|avvisato-evento")
        return
    p = ev["properties"] if "start_datetime" in ev["properties"] else C.hs(
        "/crm/v3/objects/%s/%s?properties=name,start_datetime,end_datetime" % (EV, ev["id"]))["properties"]
    if destinatari_forzati:
        dest, motivo = [(e, "") for e in destinatari_forzati], "scelti da una persona"
    else:
        dest, motivo = destinatari(azienda_id)
    link = PAGINA + "?o=" + codice(o["num"], azienda.get("codice_cliente") or "", pulito(azienda.get("name")), ev["id"], posti, azienda_id)
    dati = {"corso": re.sub(r"^Corso \| ", "", p.get("name") or nome_corso), "scuola": pulito(azienda.get("name")),
            "quando": quando(p["start_datetime"], p["end_datetime"]), "chiusura": chiusura(p["start_datetime"]), "link": link}
    agente = email_agente(o["deal"])
    cc = CC_FISSI + ([agente] if agente and agente not in CC_FISSI else [])
    if not dest:
        print("     destinatari: DA DECIDERE (%s)" % motivo)
        if chiave_ordine + "|avvisato-destinatari" not in gia and not prova:
            avviso_interno("Ordine di corso: da scegliere a chi mandare il link (%s)" % o["num"], [
                "L'ordine <b>%s</b> di %s riguarda il corso «%s» (%s)." % (o["num"], dati["scuola"], dati["corso"], dati["quando"]),
                "Non riesco a scegliere i destinatari da solo: %s." % motivo,
                "Per inviare: <code>python corsi_ordini.py --ordine %s --a mail1,mail2 --invia</code> (o scrivi ad Andrea Pizzola)." % o["num"]])
            segna(chiave_ordine + "|avvisato-destinatari")
        return
    print("     destinatari: %s (%s) · cc: %s · ccn: %s" % (", ".join(e for e, _ in dest), motivo, ", ".join(cc) or "-", ", ".join(BCC)))
    if prova:
        return
    if not auto:
        if chiave_ordine + "|pronto" not in gia:
            avviso_interno("Ordine di corso pronto per l'invio: %s" % o["num"], [
                "L'ordine <b>%s</b> di %s riguarda il corso «%s» (%s)." % (o["num"], dati["scuola"], dati["corso"], dati["quando"]),
                "Destinatari previsti: %s (%s). In copia: %s. In copia nascosta: voi." % (", ".join(e for e, _ in dest), motivo, ", ".join(cc) or "nessuno"),
                "L'invio automatico e' spento: per mandare l'e-mail dal portale HubSpot dai l'ok ad Andrea Pizzola."])
            segna(chiave_ordine + "|pronto")
        return
    r = invia([e for e, _ in dest], cc, dati)
    print("     inviata da HubSpot:", r.get("status"), r.get("statusId", "")[:20])
    segna(chiave_ordine + "|inviato")


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
        for o in todo:
            lavora(o, auto="--invia" in sys.argv, prova=prova, destinatari_forzati=a)
        spedisci_riepilogo()
        return
    auto = os.environ.get("CORSI_INVIO_AUTOMATICO") == "1"
    # ogni cinque minuti basta: il giro gira al minuto per i preventivi
    if not prova and datetime.datetime.now().minute % 5 != 0 and not os.environ.get("FORZA"):
        return
    os_ = ordini_nuovi()
    print("ordini di corsi in «Chiuso Vinto» negli ultimi 30 giorni: %d (invio automatico: %s)" % (len(os_), "ACCESO" if auto else "spento"))
    for o in os_:
        try:
            lavora(o, auto, prova)
        except Exception as e:
            print("  ERRORE su %s: %s %s" % (o["num"], type(e).__name__, str(e)[:160]))
    spedisci_riepilogo()


if __name__ == "__main__":
    main()
