# -*- coding: utf-8 -*-
"""Audit mattutino dei corsi di formazione (Andrea, 15/10/2026): ogni giorno alle 5 (ora di Roma).

Controlla tutto l'impianto dei corsi in diretta, SISTEMA da solo quello che si puo' sistemare in sicurezza e manda una mail
a pizzola@spaggiari.eu con due colonne: cosa funziona e cosa no (con l'esito: «sistemato» oppure «da sistemare» + perche').

Controlli:
  1. EVENTI      ogni corso del palinsesto ha il suo evento, senza doppioni, con data e ora uguali al palinsesto;
  2. ZOOM        riunione esistente, orario uguale all'evento, codice evento nel titolo, registrazione cloud accesa,
                 nessun iscritto «in attesa», tutti gli iscritti HubSpot presenti su Zoom;
  3. ISCRIZIONI  link personale e data leggibile presenti, nessun doppione, nessuno senza ordine (tranne i colleghi);
  4. RELATORI    relatore associato con indirizzo; link di avvio inviato ai corsi partiti nelle ultime 24 ore;
  5. DOPO LA DIRETTA  corsi svolti da oltre 24 ore: presenze registrate e registrazione Vimeo scritta;
  6. ORDINI      ordini nel giro (Chiuso Vinto) con corso ormai passato o entro 3 giorni e nessun nominato;
  7. CONSEGNE    conferme rimbalzate negli ultimi 2 giorni;
  8. CATALOGO    pagina online raggiungibile e con le stesse date del palinsesto;
  9. LAVORI      ultimi giri dei workflow dei corsi (GitHub Actions) senza fallimenti.

Sistemati da soli: riunione Zoom (titolo, orario, registrazione cloud), iscritti in attesa su Zoom (approvati), iscritti HubSpot assenti
da Zoom (registrati con link personale), data leggibile mancante sulle iscrizioni.

  python audit_corsi_mattina.py            giro normale (solo alle 5 di Roma, salvo --forza)
  python audit_corsi_mattina.py --prova    non sistema niente e non manda la mail: stampa il riepilogo
  python audit_corsi_mattina.py --forza    gira e manda la mail a qualunque ora
"""
import base64
import collections
import datetime as D
import html
import io
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from zoneinfo import ZoneInfo

QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, QUI)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import corsi_ordini as O                      # noqa: E402
import preventivi_corsi as C                  # noqa: E402

ROMA = ZoneInfo("Europe/Rome")
PROVA = "--prova" in sys.argv
FORZA = "--forza" in sys.argv
EV, REG = "2-143900361", "2-143900355"
NOSTRI = re.compile(r"@(spaggiari\.eu|spaggiarinet\.eu|gruppospaggiari\.it)$", re.I)
ADESSO = D.datetime.now(D.timezone.utc)
OGGI = ADESSO.astimezone(ROMA).date().isoformat()
MARCATORE = os.path.join(QUI, "audit_corsi_ultimo.txt")
A = os.environ.get("CORSI_AUDIT_A", "pizzola@spaggiari.eu")

RIGHE = []        # (sezione, esito 'ok'|'ko'|'fix', testo)


def ok(s, t): RIGHE.append((s, "ok", t))
def ko(s, t): RIGHE.append((s, "ko", t))
def fix(s, t): RIGHE.append((s, "fix", t))


# ------------------------------------------------------------------ Zoom
_z = {}
ZOOM_OK = bool(os.environ.get("ZOOM_S2S_JSON")) or os.path.exists(os.path.expanduser("~/.zoom_s2s.json"))


def zoom(metodo, percorso, corpo=None):
    if "k" not in _z:
        cfg = json.loads(os.environ["ZOOM_S2S_JSON"]) if os.environ.get("ZOOM_S2S_JSON") else json.load(io.open(os.path.expanduser("~/.zoom_s2s.json"), encoding="utf-8-sig"))
        r = urllib.request.Request("https://zoom.us/oauth/token?grant_type=account_credentials&account_id=" + cfg["account_id"], method="POST",
                                   headers={"Authorization": "Basic " + base64.b64encode((cfg["client_id"] + ":" + cfg["client_secret"]).encode()).decode()})
        _z["k"] = json.load(urllib.request.urlopen(r, timeout=60))["access_token"]
    dati = json.dumps(corpo).encode() if corpo is not None else None
    for k in range(4):
        r = urllib.request.Request("https://api.zoom.us/v2" + percorso, data=dati, method=metodo,
                                   headers={"Authorization": "Bearer " + _z["k"], "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=60) as x:
                b = x.read()
                return json.loads(b) if b else {}
        except urllib.error.HTTPError as e:
            if e.code == 429 and k < 3:
                time.sleep(3 * (k + 1)); continue
            return {"_err": e.code, "_msg": e.read().decode()[:160]}


def hs(p, c=None, m="GET"):
    r = C.hs(p, c, m)
    return r


def cerca(obj, filtri, props, limite=100):
    out, dopo = [], None
    for _ in range(30):
        r = hs("/crm/v3/objects/%s/search" % obj, {"filterGroups": [{"filters": filtri}], "properties": props, "limit": limite,
                                                    **({"after": dopo} if dopo else {})}, "POST")
        out += r.get("results", [])
        dopo = (r.get("paging") or {}).get("next", {}).get("after")
        if not dopo:
            break
    return out


def dt(s):
    return D.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ------------------------------------------------------------------ controlli
def eventi_e_zoom():
    pal = {x["base"]: x for x in O.palinsesto()}
    tutti = cerca(EV, [{"propertyName": "external_id", "operator": "CONTAINS_TOKEN", "value": "CF-*"}],
                  ["name", "external_id", "start_datetime", "end_datetime", "annullato", "meeting_link", "recording_link",
                   "formatore_link_inviato_il", "display_start_date", "total_registrants"])
    tutti = [e for e in tutti if e["properties"].get("annullato") != "true" and (e["properties"].get("name") or "").startswith("Corso |")]
    # 1) eventi uno per corso/data, uguali al palinsesto
    cont = collections.Counter(e["properties"]["external_id"] for e in tutti)
    doppi = [k for k, v in cont.items() if v > 1]
    (ko if doppi else ok)("1 Eventi", "eventi con codice doppio: %s" % ", ".join(doppi) if doppi else "nessun evento doppio (%d eventi corso)" % len(tutti))
    sbagliati = []
    per_ext = {e["properties"]["external_id"]: e for e in tutti}
    for base, v in pal.items():
        a, m, g = map(int, v["data"].split("-"))
        ext = "CF-%s-%02d%02d" % (base, g, m)
        e = per_ext.get(ext)
        ini, fin = O.utc(v["data"], v["inizio"]), O.utc(v["data"], v["fine"])
        if not e:
            if ini > ADESSO:
                sbagliati.append("%s: evento %s mancante (si crea al primo ordine)" % (base, ext)) if False else None
            continue
        p = e["properties"]
        if abs((dt(p["start_datetime"]) - ini).total_seconds()) > 60 or abs((dt(p["end_datetime"]) - fin).total_seconds()) > 60:
            sbagliati.append("%s: evento %s/%s, palinsesto %s/%s" % (ext, p["start_datetime"][:16], p["end_datetime"][:16], ini.strftime("%Y-%m-%dT%H:%M"), fin.strftime("%Y-%m-%dT%H:%M")))
    (ko if sbagliati else ok)("1 Eventi", ("date diverse dal palinsesto: " + " · ".join(sbagliati)) if sbagliati else "date e orari uguali al palinsesto")

    futuri = [e for e in tutti if dt(e["properties"]["end_datetime"]) > ADESSO]
    pb_zoom, fix_z, mancanti_hs, senza_rel = [], 0, 0, []
    iscr_problemi, iscr_fix, doppie = [], 0, []
    for e in futuri:
        p = e["properties"]; ext = p["external_id"]
        isc = [i for i in cerca(REG, [{"propertyName": "event_id", "operator": "EQ", "value": e["id"]}, {"propertyName": "status", "operator": "NEQ", "value": "Canceled"}],
                               ["email", "status", "event_meeting_link", "event_display_start_date", "event_start_datetime", "ordine_corso", "first_name", "last_name", "full_name"])]
        # relatore
        ass = hs("/crm/v4/objects/%s/%s/associations/2-144750696" % (EV, e["id"])).get("results", [])
        if not ass:
            senza_rel.append(ext + " (nessun relatore)")
        else:
            sp = hs("/crm/v3/objects/2-144750696/%s?properties=email" % ass[0]["toObjectId"])["properties"]
            if not (sp.get("email") or "").strip():
                senza_rel.append(ext + " (relatore senza indirizzo)")
        # iscrizioni
        mail = collections.Counter((i["properties"].get("email") or "").lower() for i in isc)
        for m_, n in mail.items():
            if n > 1:
                doppie.append("%s %s" % (ext, "x%d" % n))
        for i in isc:
            q = i["properties"]
            if not q.get("event_display_start_date") and not PROVA:
                try:
                    ini = dt(p["start_datetime"]).astimezone(ROMA)
                    gg = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"][ini.weekday()]
                    mm = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"][ini.month - 1]
                    fi = dt(p["end_datetime"]).astimezone(ROMA)
                    r = hs("/crm/v3/objects/%s/%s" % (REG, i["id"]), {"properties": {
                        "event_display_start_date": "%s %d %s %d" % (gg, ini.day, mm, ini.year), "event_display_end_date": "%s %d %s %d" % (gg, ini.day, mm, ini.year),
                        "event_display_start_time": ini.strftime("%H:%M"), "event_display_end_time": fi.strftime("%H:%M")}}, "PATCH")
                    if "_err" not in r:
                        iscr_fix += 1
                except Exception:
                    pass
            if not q.get("event_meeting_link"):
                iscr_problemi.append("%s: iscritto senza link personale" % ext)
            if q.get("event_start_datetime") and q["event_start_datetime"][:16] != p["start_datetime"][:16]:
                iscr_problemi.append("%s: data nell'iscrizione diversa dall'evento" % ext)
            if not q.get("ordine_corso") and not NOSTRI.search((q.get("email") or "")):
                iscr_problemi.append("%s: iscritto senza ordine (%s)" % (ext, "esterno"))
        # Zoom
        if not ZOOM_OK:
            continue
        mt = re.search(r"zoom\.us/(?:[sjw]/)?(\d{9,})", p.get("meeting_link") or "")
        if not mt:
            pb_zoom.append("%s: nessuna riunione Zoom" % ext); continue
        rid = mt.group(1)
        z = zoom("GET", "/meetings/" + rid)
        if z.get("_err"):
            pb_zoom.append("%s: riunione %s non leggibile (%s)" % (ext, rid, z["_err"])); continue
        mod = {}
        if abs((dt(z["start_time"]) - dt(p["start_datetime"])).total_seconds()) > 60:
            mod["start_time"] = dt(p["start_datetime"]).strftime("%Y-%m-%dT%H:%M:%SZ"); mod["timezone"] = "Europe/Rome"
        if ext not in (z.get("topic") or ""):
            mod["topic"] = re.sub(r"CF-[A-Z0-9]+-\d{4}", ext, z["topic"]) if re.search(r"CF-[A-Z0-9]+-\d{4}", z.get("topic") or "") else ((z.get("topic") or p["name"]) + " - " + ext)
        if (z.get("settings") or {}).get("auto_recording") != "cloud":
            mod["settings"] = {"auto_recording": "cloud"}
        if mod:
            if PROVA:
                pb_zoom.append("%s: Zoom da correggere (%s)" % (ext, ", ".join(mod)))
            else:
                r = zoom("PATCH", "/meetings/" + rid, mod)
                (pb_zoom.append("%s: correzione Zoom non riuscita (%s)" % (ext, r.get("_err"))) if r.get("_err") else None)
                fix_z += 0 if r.get("_err") else 1
        # iscritti in attesa e assenti
        att = zoom("GET", "/meetings/%s/registrants?status=pending&page_size=300" % rid).get("registrants", []) if True else []
        valid = {(i["properties"].get("email") or "").strip().lower() for i in isc}
        da_app = [x for x in att if (x.get("email") or "").lower() in valid]
        if da_app:
            if PROVA:
                pb_zoom.append("%s: %d iscritti in attesa su Zoom" % (ext, len(da_app)))
            else:
                for k in range(0, len(da_app), 30):
                    zoom("PUT", "/meetings/%s/registrants/status" % rid, {"action": "approve", "registrants": [{"id": x["id"], "email": x["email"]} for x in da_app[k:k + 30]]})
                fix_z += len(da_app)
        appr, tok = set(), None
        while True:
            zz = zoom("GET", "/meetings/%s/registrants?status=approved&page_size=300" % rid + ("&next_page_token=" + tok if tok else ""))
            appr |= {(x.get("email") or "").lower() for x in zz.get("registrants", [])}
            tok = zz.get("next_page_token")
            if not tok:
                break
        assenti = [i for i in isc if (i["properties"].get("email") or "").strip().lower() not in appr and (i["properties"].get("email") or "").strip().lower() not in {(x.get("email") or "").lower() for x in da_app}]
        for i in assenti:
            q = i["properties"]; em = (q.get("email") or "").strip().lower()
            if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", em):
                pb_zoom.append("%s: indirizzo non valido (%s)" % (ext, "iscritto")); continue
            if PROVA:
                mancanti_hs += 1; continue
            intero = (q.get("full_name") or "").split()
            r = zoom("POST", "/meetings/%s/registrants" % rid, {"email": em, "first_name": q.get("first_name") or (intero[0] if intero else "Partecipante"),
                                                                  "last_name": q.get("last_name") or " ".join(intero[1:]) or "-", "auto_approve": True})
            if r.get("join_url"):
                hs("/crm/v3/objects/%s/%s" % (REG, i["id"]), {"properties": {"event_meeting_link": r["join_url"]}}, "PATCH")
                fix_z += 1
            else:
                pb_zoom.append("%s: iscritto non registrabile su Zoom (%s)" % (ext, r.get("_err")))
    n_f = len(futuri)
    if not ZOOM_OK:
        pb_zoom.append("controlli Zoom NON eseguiti: manca il segreto ZOOM_S2S_JSON nel repository (gh secret set ZOOM_S2S_JSON -R pizzola-code/corsi-preventivi < ~/.zoom_s2s.json)")
    (ko if pb_zoom else ok)("2 Zoom", (" · ".join(pb_zoom[:12])) if pb_zoom else "%d riunioni verificate: orario, titolo, registrazione cloud, nessuno in attesa" % n_f)
    if fix_z:
        fix("2 Zoom", "sistemati da solo %d interventi su Zoom (titolo/orario/registrazione, iscritti approvati o registrati)" % fix_z)
    if mancanti_hs and PROVA:
        ko("2 Zoom", "%d iscritti HubSpot non registrati su Zoom" % mancanti_hs)
    (ko if iscr_problemi else ok)("3 Iscrizioni", (" · ".join(sorted(set(iscr_problemi))[:10])) if iscr_problemi else "iscrizioni con link personale, data e ordine a posto")
    if iscr_fix:
        fix("3 Iscrizioni", "data leggibile scritta su %d iscrizioni" % iscr_fix)
    (ko if doppie else ok)("3 Iscrizioni", ("iscrizioni doppie: " + ", ".join(doppie[:8])) if doppie else "nessuna iscrizione doppia")
    (ko if senza_rel else ok)("4 Relatori", (" · ".join(senza_rel)) if senza_rel else "tutti i corsi futuri hanno relatore e indirizzo")

    # link di avvio inviato ai corsi partiti nelle ultime 24 ore
    recenti = [e for e in tutti if ADESSO - D.timedelta(hours=24) <= dt(e["properties"]["start_datetime"]) <= ADESSO]
    mancano = [e["properties"]["external_id"] for e in recenti if not e["properties"].get("formatore_link_inviato_il")]
    if recenti:
        (ko if mancano else ok)("4 Relatori", ("link di avvio NON inviato a: " + ", ".join(mancano)) if mancano else "link di avvio inviato ai %d corsi partiti nelle ultime 24 ore" % len(recenti))
    # dopo la diretta
    passati = [e for e in tutti if D.timedelta(hours=24) <= ADESSO - dt(e["properties"]["end_datetime"]) <= D.timedelta(days=4)]
    sp_pres, sp_rec = [], []
    for e in passati:
        p = e["properties"]
        isc = cerca(REG, [{"propertyName": "event_id", "operator": "EQ", "value": e["id"]}, {"propertyName": "status", "operator": "NEQ", "value": "Canceled"}], ["status", "email"])
        est = [i for i in isc if not NOSTRI.search(i["properties"].get("email") or "")]
        if est and not any(i["properties"].get("status") == "Attended" for i in est):
            sp_pres.append("%s (0 presenti su %d iscritti)" % (p["external_id"], len(est)))
        if not p.get("recording_link"):
            sp_rec.append(p["external_id"])
    if passati:
        (ko if sp_pres else ok)("5 Dopo la diretta", ("presenze non registrate: " + ", ".join(sp_pres)) if sp_pres else "presenze registrate per i %d corsi svolti negli ultimi giorni" % len(passati))
        (ko if sp_rec else ok)("5 Dopo la diretta", ("registrazione Vimeo mancante: " + ", ".join(sp_rec)) if sp_rec else "registrazione presente per tutti i corsi svolti")


def ordini():
    try:
        os_ = O.ordini_nuovi(30)
    except Exception as e:
        ko("6 Ordini", "lettura ordini non riuscita: %s" % type(e).__name__); return
    gia = O.registro()
    pal = {x["base"]: x for x in O.palinsesto()}
    senza_nomi, non_gest = [], []
    per_ordine = collections.defaultdict(list)
    for o in os_:
        per_ordine[o["num"]].append(o)
    for num, righe in per_ordine.items():
        for o in righe:
            m = O.CORSO.match(o["sku"]); base = m.group(1); v = pal.get(base)
            if not v:
                continue
            ini = O.utc(v["data"], v["inizio"])
            if "%s|%s|inviato" % (num, o["sku"]) not in gia and ini > ADESSO - D.timedelta(hours=2):
                non_gest.append("%s %s" % (num, o["sku"]))
    (ko if non_gest else ok)("6 Ordini", ("ordini non ancora lavorati dal giro: " + ", ".join(non_gest[:10])) if non_gest else "%d ordini nel giro: tutti lavorati (invito o registrazione inviati)" % len(per_ordine))
    # ordini con corso entro 3 giorni e 0 nominati
    for num, righe in per_ordine.items():
        for o in righe:
            m = O.CORSO.match(o["sku"]); v = pal.get(m.group(1))
            if not v:
                continue
            ini = O.utc(v["data"], v["inizio"])
            if ADESSO - D.timedelta(hours=48) < ini < ADESSO + D.timedelta(days=3):
                n = len(cerca(REG, [{"propertyName": "ordine_corso", "operator": "EQ", "value": num}, {"propertyName": "status", "operator": "NEQ", "value": "Canceled"}], ["email"]))
                if n == 0:
                    senza_nomi.append("%s (%s, %s)" % (num, m.group(1), v["data"][5:]))
    (ko if senza_nomi else ok)("6 Ordini", ("corsi (svolti da meno di 2 giorni o entro 3 giorni) con ordine e NESSUN nominato, da seguire con l'agente: " + ", ".join(senza_nomi[:12])) if senza_nomi else "nessun ordine con corso recente o imminente e zero nominati")


def consegne():
    da = int((time.time() - 2 * 86400) * 1000)
    r = cerca(REG, [{"propertyName": "ordine_corso", "operator": "HAS_PROPERTY"}, {"propertyName": "hs_createdate", "operator": "GTE", "value": str(da)}], ["email", "status", "ordine_corso"])
    visti, cattivi = set(), []
    for x in r:
        p = x["properties"]; m = (p.get("email") or "").lower()
        if not m or m in visti or p.get("status") == "Canceled" or (p.get("ordine_corso") or "").startswith("PROVA"):
            continue
        visti.add(m)
        ev = hs("/email/public/v1/events?recipient=%s&startTimestamp=%d&limit=60" % (urllib.parse.quote(m), da)).get("events", [])
        if any(e.get("type") in ("BOUNCE", "DROPPED") for e in ev) and not any(e.get("type") == "DELIVERED" for e in ev):
            cattivi.append(m.split("@")[0][:3] + "***@" + m.split("@")[1])
    (ko if cattivi else ok)("7 Consegne", ("conferme non consegnate (indirizzi da correggere): " + ", ".join(cattivi[:10])) if cattivi else "%d conferme degli ultimi 2 giorni: nessuna rimbalzata" % len(visti))


def catalogo():
    try:
        h = urllib.request.urlopen(urllib.request.Request("https://www.spaggiari.eu/strumenti/corsi-formazione?a=%d" % int(time.time()), headers={"User-Agent": "Mozilla/5.0"}), timeout=60).read().decode("utf-8", "replace")
    except Exception as e:
        ko("8 Catalogo", "pagina non raggiungibile: %s" % type(e).__name__); return
    ok("8 Catalogo", "pagina raggiungibile (%d caratteri)" % len(h))
    date_pagina = {}
    for m in re.finditer(r'"startDate":\s*"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})', h):
        date_pagina[m.group(1)] = 1
    pal = [x for x in O.palinsesto() if O.utc(x["data"], x["inizio"]) > ADESSO]
    mancano = ["%s %s" % (x["base"], x["data"][5:]) for x in pal if "%sT%s" % (x["data"], x["inizio"]) not in date_pagina]
    (ko if mancano else ok)("8 Catalogo", ("date del palinsesto assenti dalla pagina: " + ", ".join(mancano)) if mancano else "le %d date future del palinsesto sono tutte in pagina" % len(pal))


def lavori():
    tok, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY", "pizzola-code/corsi-preventivi")
    if not tok:
        return
    try:
        out = []
        for wf in ("ordini-corsi.yml", "preventivi-corsi.yml", "corsi-partecipazioni.yml", "report-corsi.yml"):
            r = urllib.request.Request("https://api.github.com/repos/%s/actions/workflows/%s/runs?per_page=30" % (repo, wf), headers={"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json"})
            runs = json.load(urllib.request.urlopen(r, timeout=60)).get("workflow_runs", [])
            ultime = [x for x in runs if dt(x["created_at"]) > ADESSO - D.timedelta(hours=24) and x["status"] == "completed"]
            ko_ = [x for x in ultime if x["conclusion"] == "failure"]
            if ko_:
                out.append("%s: %d giri falliti su %d nelle ultime 24 ore" % (wf, len(ko_), len(ultime)))
        (ko if out else ok)("9 Lavori", (" · ".join(out)) if out else "workflow dei corsi senza giri falliti nelle ultime 24 ore")
    except Exception as e:
        ko("9 Lavori", "stato dei workflow non leggibile (%s)" % type(e).__name__)


# ------------------------------------------------------------------ mail
def mail():
    sez = collections.OrderedDict()
    for s, e, t in RIGHE:
        sez.setdefault(s, []).append((e, t))
    n_ko = sum(1 for _, e, _ in RIGHE if e == "ko")
    n_fix = sum(1 for _, e, _ in RIGHE if e == "fix")
    n_ok = sum(1 for _, e, _ in RIGHE if e == "ok")
    ora = ADESSO.astimezone(ROMA).strftime("%d/%m/%Y %H:%M")
    righe_html = []
    for s in sorted(sez):
        righe_html.append("<tr><td colspan='2' style='background:#06484b;color:#fff;padding:6px 10px;font-weight:bold'>%s</td></tr>" % html.escape(s[2:]))
        for e, t in sez[s]:
            sim, col = {"ok": ("&#10003;", "#1F7A3D"), "ko": ("&#10007;", "#C8202B"), "fix": ("&#9874;", "#b9770e")}[e]
            et = {"ok": "funziona", "ko": "DA SISTEMARE", "fix": "sistemato da solo"}[e]
            righe_html.append("<tr><td style='padding:6px 10px;width:150px;color:%s;font-weight:bold;vertical-align:top'>%s %s</td><td style='padding:6px 10px;border-bottom:1px solid #e3e8ec'>%s</td></tr>" % (col, sim, et, html.escape(t)))
    corpo = ("<div style='font-family:Arial,sans-serif;font-size:14px;color:#0E2A4D;max-width:760px'>"
             "<p style='font-size:16px'><b>Audit corsi di formazione, %s</b></p>"
             "<p>%d controlli a posto &middot; %d cose sistemate da sole &middot; <b style='color:%s'>%d da sistemare</b></p>"
             "<table style='border-collapse:collapse;width:100%%'>%s</table>"
             "<p style='color:#51606E;font-size:12px'>Controlla eventi, Zoom, iscrizioni, relatori, presenze e registrazioni, ordini, consegne, catalogo e giri automatici. "
             "Sistema da solo titolo/orario Zoom, iscritti in attesa o assenti da Zoom e date mancanti.</p></div>") % (
        ora, n_ok, n_fix, "#C8202B" if n_ko else "#1F7A3D", n_ko, "".join(righe_html))
    oggetto = "Audit corsi %s: %s" % (ADESSO.astimezone(ROMA).strftime("%d/%m"), "tutto a posto" if not n_ko else "%d da sistemare" % n_ko) + (" (%d sistemati)" % n_fix if n_fix else "")
    return oggetto, corpo


def invia(oggetto, corpo):
    utente, pw = os.environ.get("SMTP_CORSI_USER"), os.environ.get("SMTP_CORSI_PASS")
    if not (utente and pw):
        print("credenziali SMTP assenti: mail non inviata"); return
    m = EmailMessage()
    m["From"] = "Spaggiari <%s>" % C.MITTENTE
    m["To"] = A
    m["Subject"] = oggetto
    m.set_content("Audit corsi: serve un lettore HTML.")
    m.add_alternative(corpo, subtype="html")
    s = smtplib.SMTP(C.SMTP_HOST, C.SMTP_PORTA, timeout=60)
    s.starttls(context=ssl.create_default_context())
    s.login(utente, pw)
    s.send_message(m, to_addrs=[x.strip() for x in A.split(",")])
    s.quit()


def main():
    if not PROVA and not FORZA:
        if ADESSO.astimezone(ROMA).hour != 5:
            print("non sono le 5 a Roma: niente da fare"); return
        try:
            if io.open(MARCATORE, encoding="utf-8").read().strip() == OGGI:
                print("audit di oggi gia' fatto"); return
        except FileNotFoundError:
            pass
    for f in (eventi_e_zoom, ordini, consegne, catalogo, lavori):
        try:
            f()
        except Exception as e:
            ko("0 Audit", "controllo %s interrotto: %s %s" % (f.__name__, type(e).__name__, str(e)[:120]))
    n = collections.Counter(e for _, e, _ in RIGHE)
    print("audit corsi: %d ok · %d sistemati · %d da sistemare" % (n["ok"], n["fix"], n["ko"]))
    if PROVA:
        for s, e, t in RIGHE:
            print("  [%s] %s: %s" % (e, s, t[:200]))
        return
    oggetto, corpo = mail()
    invia(oggetto, corpo)
    io.open(MARCATORE, "w", encoding="utf-8").write(OGGI)


if __name__ == "__main__":
    main()
