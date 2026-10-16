# -*- coding: utf-8 -*-
"""Pagina catalogo corsi: sui corsi GIA' SVOLTI con registrazione su Vimeo la data della diretta diventa «Registrazione disponibile»
(stesso codice, stesso prezzo: l'ordine a corso finito e' una registrazione, vedi corsi_ordini/registrazioni_illimitate).
Aggiorna la bozza della pagina 473385072859 e la pubblica solo se qualcosa cambia. Idempotente: si puo' lanciare a ogni giro.
  python catalogo_registrazioni.py --prova      elenca cosa cambierebbe
Segnala (senza toccare) i corsi passati SENZA registrazione: sono schede da togliere o da tenere d'occhio."""
import datetime
import json
import os
import re
import sys
import urllib.request

PAGINA = "473385072859"
ETICHETTA = "Registrazione disponibile"
H = os.path.expanduser("~")


def _tok(var, f):
    return os.environ.get(var) or open(os.path.join(H, f)).read().strip()


def api(percorso, metodo="GET", corpo=None, pagine=True):
    t = _tok("HS_PAGES_TOKEN", ".hs_pages_token.txt") if pagine else _tok("HUBSPOT_TOKEN", ".hubspot_pat.txt")
    r = urllib.request.Request("https://api.hubapi.com" + percorso, method=metodo, data=json.dumps(corpo, ensure_ascii=False).encode("utf-8") if corpo is not None else None,
                               headers={"Authorization": "Bearer " + t, "Content-Type": "application/json; charset=utf-8"})
    x = urllib.request.urlopen(r, timeout=60).read().decode("utf-8")
    return json.loads(x) if x else {}


def schede(j):
    """Le schede-corso: lista `cards` del modulo griglia-card della pagina (il primo con titolo «palinsesto»)."""
    def cerca(o):
        if isinstance(o, dict):
            if isinstance(o.get("cards"), list) and o["cards"] and "spg-corso-scheda" in str(o["cards"][0].get("c_desc")):
                return o["cards"]
            for v in o.values():
                r = cerca(v)
                if r is not None:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = cerca(v)
                if r is not None:
                    return r
    return cerca(j["layoutSections"])


def main(prova):
    pal = {x["base"]: x for x in json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "corsi_palinsesto.json"), encoding="utf-8"))}
    ora = datetime.datetime.now(datetime.timezone.utc)
    bozza = api("/cms/v3/pages/site-pages/%s/draft" % PAGINA)
    cards = schede(bozza)
    cambi, senza = [], []
    con_reg, dopo = set(), None                       # una sola ricerca: eventi dei corsi con registrazione su Vimeo
    for _ in range(10):
        r = api("/crm/v3/objects/2-143900361/search", "POST", {"filterGroups": [{"filters": [
            {"propertyName": "external_id", "operator": "CONTAINS_TOKEN", "value": "CF-*"},
            {"propertyName": "recording_link", "operator": "HAS_PROPERTY"}]}], "properties": ["external_id"], "limit": 100,
            **({"after": dopo} if dopo else {})}, pagine=False)
        con_reg |= {(x["properties"].get("external_id") or "").upper() for x in r.get("results", [])}
        dopo = (r.get("paging") or {}).get("next", {}).get("after")
        if not dopo:
            break
    for c in cards:
        d = c.get("c_desc") or ""
        m = re.search(r'data-codice="WBRELE([A-Z0-9]+)"', d)
        v = pal.get(m.group(1)) if m else None
        if not v or ETICHETTA in d:
            continue
        a, mm, g = map(int, v["data"].split("-"))
        h, mi = map(int, v["fine"].split(":"))
        fine = datetime.datetime(a, mm, g, h - (2 if datetime.datetime(a, mm, g) < datetime.datetime(2026, 10, 25) else 1), mi, tzinfo=datetime.timezone.utc)
        if fine > ora:
            continue                                    # diretta ancora da fare: resta la data
        if "CF-%s-%02d%02d" % (v["base"], g, mm) not in con_reg:
            senza.append(v["codice"])
            continue
        nuovo = re.sub(r'(<select class="spg-corso-data"[^>]*>).*?(</select>)', r'\1<option value="%s">%s</option>\2' % (ETICHETTA, ETICHETTA), d, flags=re.S)
        nuovo = re.sub(r'data-quando="[^"]*"', 'data-quando="%s"' % ETICHETTA, nuovo)
        nuovo = nuovo.replace('<span class="spg-corso-relatore-et">Quando</span>', '<span class="spg-corso-relatore-et">Registrazione</span>', 1)
        c["c_desc"] = nuovo
        cambi.append(v["codice"])
    print("schede da portare a «%s»: %s" % (ETICHETTA, ", ".join(cambi) or "nessuna"))
    if senza:
        print("corsi passati SENZA registrazione (invariati): %s" % ", ".join(senza))
    if prova or not cambi:
        return
    api("/cms/v3/pages/site-pages/%s/draft" % PAGINA, "PATCH", {"layoutSections": bozza["layoutSections"]})
    api("/cms/v3/pages/site-pages/%s/draft/push-live" % PAGINA, "POST")
    print("pagina aggiornata e pubblicata")


if __name__ == "__main__":
    main("--prova" in sys.argv)
