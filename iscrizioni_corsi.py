# -*- coding: utf-8 -*-
"""Rimette la data sulle iscrizioni ai CORSI in diretta.

L'email di conferma legge quando e' l'evento dai campi `event_display_*` dell'ISCRIZIONE, che hapily
compila da solo solo per le iscrizioni nate dal suo modulo. Le iscrizioni dei corsi nascono dalla
pagina di nomina (via API) e hapily, dopo pochi secondi, svuota quei campi: la mail direbbe
"data da definire dalle ore -- alle ore --".

Questo giro (ogni minuto) legge gli eventi il cui nome inizia per "Corso |" e riscrive i campi
sulle iscrizioni che li hanno vuoti. E' idempotente: se sono gia' pieni non tocca niente.

Uso:  python iscrizioni_corsi.py            sistema le iscrizioni
      python iscrizioni_corsi.py --prova    elenca senza scrivere
"""
import datetime
import os
import re
import sys
import urllib.parse

QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, QUI)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import preventivi_corsi as C                                  # noqa: E402

EV, REG = "2-143900361", "2-143900355"
CAMPI_EV = ["name", "start_datetime", "end_datetime", "display_start_date", "display_end_date", "display_start_time",
            "display_end_time", "display_timezone", "display_timezone_alt", "venue"]


def gcal(nome, inizio, fine):
    def f(t):
        return re.sub(r"\.\d+", "", t.replace("-", "").replace(":", ""))
    return ("https://calendar.google.com/calendar/render?action=TEMPLATE&text=" + urllib.parse.quote(nome) +
            "&dates=%s/%s&details=" % (f(inizio), f(fine or inizio)) +
            urllib.parse.quote("Diretta del corso. Il link personale per entrare è nella e-mail di conferma."))


GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre",
        "novembre", "dicembre"]


def locale(iso):
    """data e ora italiane da un orario ISO (se l'evento ha i campi vuoti)"""
    from zoneinfo import ZoneInfo
    d = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Rome"))
    return "%s %d %s %d" % (GIORNI[d.weekday()], d.day, MESI[d.month - 1], d.year), d.strftime("%H:%M"), d


def main():
    prova = "--prova" in sys.argv
    da = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    eventi = C.hs("/crm/v3/objects/%s/search" % EV, {
        "filterGroups": [{"filters": [{"propertyName": "start_datetime", "operator": "GTE", "value": da}]}],
        "sorts": [{"propertyName": "start_datetime", "direction": "ASCENDING"}], "properties": CAMPI_EV, "limit": 100}, "POST").get("results", [])
    corsi = [e for e in eventi if re.match(r"^\s*corso\s*\|", e["properties"].get("name") or "", re.I)]
    sistemate = 0
    for e in corsi:
        p = dict(e["properties"])
        if not p.get("display_start_date") and p.get("start_datetime"):
            g, o, d0 = locale(p["start_datetime"])
            p["display_start_date"], p["display_start_time"] = g, o
            p["display_timezone_alt"] = p.get("display_timezone_alt") or ("GMT+%d" % (d0.utcoffset().total_seconds() // 3600))
            if p.get("end_datetime"):
                p["display_end_date"], p["display_end_time"], _ = locale(p["end_datetime"])
        iscr = C.hs("/crm/v3/objects/%s/search" % REG, {
            "filterGroups": [{"filters": [{"propertyName": "event_id", "operator": "EQ", "value": e["id"]},
                                          {"propertyName": "event_display_start_date", "operator": "NOT_HAS_PROPERTY"}]}],
            "properties": ["email"], "limit": 100}, "POST").get("results", [])
        for x in iscr:
            print("  %s: data da rimettere a %s" % (p.get("name"), x["properties"].get("email")))
            if prova:
                continue
            C.hs("/crm/v3/objects/%s/%s" % (REG, x["id"]), {"properties": {
                "event_display_start_date": p.get("display_start_date") or "",
                "event_display_end_date": p.get("display_end_date") or p.get("display_start_date") or "",
                "event_display_start_time": p.get("display_start_time") or "",
                "event_display_end_time": p.get("display_end_time") or "",
                "event_display_timezone": p.get("display_timezone") or "Europe/Amsterdam",
                "event_display_timezone_alt": p.get("display_timezone_alt") or "GMT+2",
                "event_venue": p.get("venue") or "Evento online",
                "event_gcal_link": gcal(p.get("name") or "", p.get("start_datetime"), p.get("end_datetime"))}}, "PATCH")
            sistemate += 1
    print("corsi in programma: %d · iscrizioni sistemate: %d" % (len(corsi), sistemate))


if __name__ == "__main__":
    main()
