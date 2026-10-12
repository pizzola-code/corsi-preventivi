# -*- coding: utf-8 -*-
"""Persone interne iscritte di default a ogni diretta dei corsi (Andrea, 12/10/2026: Roberto Pancani).

Per ogni evento futuro «Corso | ...» (external_id CF-<corso>-<ggmm>) la persona viene iscritta con la stessa catena
della pagina di nomina (iscrizione collegata a contatto ed evento): riceve il proprio link personale Zoom e la conferma.
Senza `ordine_corso`: non compare nelle pagine di nomina degli ordini, non entra nei conteggi per scuola, e le guardie sugli
iscritti liberi la lasciano stare perche' e' un indirizzo @spaggiari.eu.
Se esiste gia' una sua iscrizione all'evento (anche annullata) non si rifa': chi la annulla a mano la toglie davvero.

  python iscritti_default.py            giro normale (gira anche dentro corsi_ordini.py)
  python iscritti_default.py --prova    elenca cosa farebbe
"""
import datetime
import sys
import urllib.parse

import preventivi_corsi as C

EV, REG = "2-143900361", "2-143900355"
DEFAULT = [("pancani@spaggiari.eu", "Roberto", "Pancani")]


def _gcal(ev):
    f = lambda t: datetime.datetime.fromisoformat(t.replace("Z", "+00:00")).strftime("%Y%m%dT%H%M%SZ")
    return ("https://calendar.google.com/calendar/render?action=TEMPLATE&text=" + urllib.parse.quote(ev.get("name") or "", safe="") +
            "&dates=" + f(ev["start_datetime"]) + "/" + f(ev.get("end_datetime") or ev["start_datetime"]) +
            "&details=" + urllib.parse.quote("Diretta del corso. Il link personale per entrare è nella e-mail di conferma.", safe=""))


def iscrivi(ev, email, nome, cognome, prova):
    """True se ha creato l'iscrizione."""
    p = ev["properties"]
    cs = C.hs("/crm/v3/objects/contacts/batch/read", {"idProperty": "email", "properties": ["email"], "inputs": [{"id": email}]}, "POST").get("results", [])
    if not cs:
        print("  contatto %s non trovato: salto" % email)
        return False
    cid = cs[0]["id"]
    legate = [str(x["toObjectId"]) for x in C.hs("/crm/v4/objects/contacts/%s/associations/%s?limit=100" % (cid, REG)).get("results", [])]
    if legate:
        g = C.hs("/crm/v3/objects/%s/batch/read" % REG, {"properties": ["event_id"], "inputs": [{"id": i} for i in legate[:100]]}, "POST").get("results", [])
        if any(x["properties"].get("event_id") == ev["id"] for x in g):
            return False
    print("  %s <- %s (%s)" % (p.get("name", "")[:60], email, p["start_datetime"][:10]))
    if prova:
        return True
    C.hs("/crm/v3/objects/%s" % REG, {
        "properties": {
            "name": ("%s %s" % (nome, cognome)).upper() + " - " + (p.get("name") or ""), "email": email, "first_name": nome, "last_name": cognome,
            "full_name": "%s %s" % (nome, cognome), "event_id": ev["id"], "event_name": p.get("name") or "", "status": "Registered",
            "event_display_start_date": p.get("display_start_date") or "", "event_display_end_date": p.get("display_end_date") or p.get("display_start_date") or "",
            "event_display_start_time": p.get("display_start_time") or "", "event_display_end_time": p.get("display_end_time") or "",
            "event_display_timezone": p.get("display_timezone") or "Europe/Amsterdam", "event_display_timezone_alt": p.get("display_timezone_alt") or "GMT+2",
            "event_start_datetime": p["start_datetime"], "event_end_datetime": p.get("end_datetime") or "",
            "event_venue": p.get("venue") or "Evento online", "event_gcal_link": _gcal({"name": p.get("name"), **p})},
        "associations": [
            {"to": {"id": cid}, "types": [{"associationCategory": "USER_DEFINED", "associationTypeId": t} for t in (240, 257)]},
            {"to": {"id": ev["id"]}, "types": [{"associationCategory": "USER_DEFINED", "associationTypeId": t} for t in (244, 296)]}]}, "POST")
    return True


def giro(prova=False):
    adesso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = C.hs("/crm/v3/objects/%s/search" % EV, {"filterGroups": [{"filters": [
        {"propertyName": "external_id", "operator": "CONTAINS_TOKEN", "value": "CF-*"},
        {"propertyName": "start_datetime", "operator": "GT", "value": adesso}]}],
        "properties": ["name", "start_datetime", "end_datetime", "external_id", "annullato", "display_start_date", "display_end_date",
                       "display_start_time", "display_end_time", "display_timezone", "display_timezone_alt", "venue"], "limit": 100}, "POST").get("results", [])
    n = 0
    for ev in r:
        if ev["properties"].get("annullato") == "true" or not (ev["properties"].get("name") or "").startswith("Corso |"):
            continue
        for email, nome, cognome in DEFAULT:
            try:
                n += iscrivi(ev, email, nome, cognome, prova)
            except Exception as e:
                print("  ERRORE %s su %s: %s %s" % (email, ev["id"], type(e).__name__, str(e)[:120]))
    print("iscritti di default: %d nuove iscrizioni su %d eventi" % (n, len(r)))


if __name__ == "__main__":
    giro("--prova" in sys.argv)
