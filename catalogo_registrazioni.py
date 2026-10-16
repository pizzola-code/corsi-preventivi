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
    t = _tok("HUBSPOT_TOKEN", ".hubspot_pat.txt")      # il PAT scrive anche le pagine (verificato 16/10)
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


def _trova_lead(o):
    """Il testo `lead` del modulo, che contiene la barra dei filtri."""
    if isinstance(o, dict):
        if isinstance(o.get("lead"), str) and "spg-filtri" in o["lead"]:
            return o
        for v in o.values():
            r = _trova_lead(v)
            if r is not None:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _trova_lead(v)
            if r is not None:
                return r


NOMI_NUMERO = {20: "Venti", 21: "Ventuno", 22: "Ventidue", 23: "Ventitre", 24: "Ventiquattro", 25: "Venticinque", 26: "Ventisei"}


def assicura_filtri(bozza, cards):
    """Barra dei filtri: conteggi sempre allineati alle schede e pulsante «Registrazioni» (corsi gia' svolti, acquistabili in registrazione).
    Il pulsante si somma ai prodotti (mostra solo le registrazioni di quel prodotto). True se ha modificato qualcosa."""
    mod = _trova_lead(bozza["layoutSections"])
    if mod is None:
        return False
    lead = mod["lead"]
    nuovo = lead
    fam = {}
    n_reg = 0
    for c in cards:
        d = c.get("c_desc") or ""
        m = re.search(r"spg-fam-([a-z0-9]+)", d)
        if m:
            fam[m.group(1)] = fam.get(m.group(1), 0) + 1
        if ETICHETTA in d:
            n_reg += 1

    def conta(testo, selettore, n):
        return re.sub(r"(" + selettore + r"[^>]*>[^<]*<b>)\d+(</b>)", lambda m: m.group(1) + str(n) + m.group(2), testo)

    nuovo = conta(nuovo, r'class="spg-filtro spg-filtro--tutti"', len(cards))
    for f, n in fam.items():
        nuovo = conta(nuovo, r'data-fam="%s"' % f, n)
    if 'data-fam="registrazioni"' in nuovo:
        nuovo = conta(nuovo, r'data-fam="registrazioni"', n_reg)
    elif n_reg:
        bottone = ('<button type="button" class="spg-filtro spg-fam-registrazioni" data-fam="registrazioni" aria-pressed="false">'
                   'Registrazioni <b>%d</b></button>' % n_reg)
        nuovo = nuovo.replace('</div><p class="spg-filtri-esito"', bottone + '</div><p class="spg-filtri-esito"', 1)
    nome = NOMI_NUMERO.get(len(cards))
    if nome:
        nuovo = re.sub(r"Venti(?:due|uno|tre|quattro|cinque|sei)? corsi online|Ventuno corsi online|Venti corsi online", nome + " corsi online", nuovo)
    frase = " I corsi gi&agrave; svolti restano disponibili in registrazione, allo stesso prezzo: ogni partecipante riceve il proprio link personale, valido 30 giorni."
    if n_reg and "restano disponibili in registrazione" not in nuovo:
        nuovo = re.sub(r"(scende di prezzo\.)(</p>)", lambda m: m.group(1) + frase + m.group(2), nuovo, count=1)
    cambiato = nuovo != lead
    mod["lead"] = nuovo
    # script dei filtri (footerHtml): il filtro «registrazioni» si somma alla famiglia
    f = bozza.get("footerHtml") or ""
    if "soloReg" not in f and "var schede = [];" in f:
        a = "schede.push({nodo: art, fam: m ? m[1] : ''});"
        b = ("schede.push({nodo: art, fam: m ? m[1] : '', "
             "reg: !!(dentro && dentro.textContent.indexOf('Registrazione disponibile') >= 0)});")
        c = "var mostra = !scelte.length || scelte.indexOf(s.fam) >= 0;"
        d = ("var fams = scelte.filter(function(x){ return x !== 'registrazioni'; }), soloReg = scelte.indexOf('registrazioni') >= 0;\n"
             "      var mostra = (!fams.length || fams.indexOf(s.fam) >= 0) && (!soloReg || s.reg);")
        if a in f and c in f:
            bozza["footerHtml"] = f.replace(a, b, 1).replace(c, d, 1)
            cambiato = True
    return cambiato


def sposta_pulsante(bozza):
    """Il pulsante «Registrazioni» sta accanto ai pulsanti della data (Ordina per data), in colore diverso dai filtri per prodotto.
    Il filtro continua a funzionare perche' i gestori leggono i pulsanti in tutta la pagina. True se ha modificato qualcosa."""
    f = bozza.get("footerHtml") or ""
    if "spg-reg-sposta" in f or "var schede = [];" not in f:
        return False
    sost = [
        ("barra.querySelectorAll('.spg-filtro')", "document.querySelectorAll('.spg-filtri .spg-filtro, .spg-ordina .spg-filtro')"),
        ("barra.addEventListener('click', function(ev){", "document.addEventListener('click', function(ev){"),
        ("barra.parentNode.insertBefore(gruppo, barra.nextSibling);",
         "barra.parentNode.insertBefore(gruppo, barra.nextSibling);\n"
         "  var regBtn = barra.querySelector('.spg-fam-registrazioni');   // spg-reg-sposta\n"
         "  if (regBtn) { regBtn.classList.add('spg-reg'); gruppo.appendChild(regBtn); }"),
        (".spg-ord:focus-visible{outline:3px solid #e8b547;outline-offset:2px}",
         ".spg-ord:focus-visible{outline:3px solid #e8b547;outline-offset:2px}\n"
         ".spg-ordina .spg-filtro.spg-reg{margin-left:10px;background:#6fc2e7!important;border-color:#6fc2e7!important;color:#06484b!important}\n"
         ".spg-ordina .spg-filtro.spg-reg:hover{background:#8fd0ee!important;border-color:#8fd0ee!important}\n"
         '.spg-ordina .spg-filtro.spg-reg[aria-pressed="true"]{background:#06484b!important;border-color:#06484b!important;color:#fff!important}'),
    ]
    nuovo = f
    for a, b in sost:
        if a not in nuovo:
            return False                    # struttura cambiata: meglio non toccare niente
        nuovo = nuovo.replace(a, b)        # il primo gruppo di righe compare due volte (filtri e azzeramento): tutte
    bozza["footerHtml"] = nuovo
    return True


def compatta_filtri(bozza):
    """Filtri per prodotto e pulsanti della data su meno righe: la colonna del testo introduttivo resta stretta, ma la barra dei filtri
    puo' usare tutta la larghezza (spg-filtri-larghi). True se ha modificato qualcosa."""
    f = bozza.get("footerHtml") or ""
    ancora = ".spg-ord:focus-visible{outline:3px solid #e8b547;outline-offset:2px}"
    if "spg-filtri-larghi" in f or ancora not in f:
        return False
    righe = [ancora, "/* spg-filtri-larghi */",
             "#hs_cos_wrapper_module_ss_intro .dcards__head,#hs_cos_wrapper_module_ss_intro .dcards__lead{max-width:none!important}",
             "#hs_cos_wrapper_module_ss_intro .dcards__lead>p{max-width:620px}",
             "#hs_cos_wrapper_module_ss_intro .spg-filtri{gap:6px!important}",
             "#hs_cos_wrapper_module_ss_intro .spg-filtri .spg-filtro{padding:8px 10px!important;font-size:13px!important}",
             "#hs_cos_wrapper_module_ss_intro .spg-ordina .spg-ord{padding:8px 13px}"]
    bozza["footerHtml"] = f.replace(ancora, chr(10).join(righe), 1)
    return True


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
    filtri = assicura_filtri(bozza, cards)
    filtri = sposta_pulsante(bozza) or filtri
    filtri = compatta_filtri(bozza) or filtri
    print("schede da portare a «%s»: %s%s" % (ETICHETTA, ", ".join(cambi) or "nessuna", " · filtri/conteggi da aggiornare" if filtri else ""))
    if senza:
        print("corsi passati SENZA registrazione (invariati): %s" % ", ".join(senza))
    if prova or not (cambi or filtri):
        return
    api("/cms/v3/pages/site-pages/%s/draft" % PAGINA, "PATCH", {"layoutSections": bozza["layoutSections"], "footerHtml": bozza.get("footerHtml") or ""})
    api("/cms/v3/pages/site-pages/%s/draft/push-live" % PAGINA, "POST")
    print("pagina aggiornata e pubblicata")


if __name__ == "__main__":
    main("--prova" in sys.argv)
