# -*- coding: utf-8 -*-
"""Chiusura automatica delle trattative della pipeline «Formazione Italiascuola» (16/10/2026, Andrea).

Gemello di chiusura_trattative.py (corsi Spaggiari): quello riconosce solo i codici WBREL…, questo i codici
WBRITA… e ITAC… e lavora solo sulla pipeline Italiascuola, quindi non si pestano.

Quando la scuola ordina su ScuolaShop (o l'ordine passa dall'ERP) in HubSpot compare una trattativa d'ordine:
  · «<numero> - ERP»         nelle pipeline Rinnovi Licenze / Nuove Installazioni / Scuola Shop… (ha sempre la scuola collegata)
  · «<numero> - ScuolaShop»  nella pipeline Scuola Shop V2 (ha solo il contatto di chi ha ordinato)
Se l'ordine e' concluso la trattativa del preventivo passa a «Ordine confermato»; se e' annullato a «Chiusa persa».

Stato dell'ordine -> esito:  «Chiuso Vinto» / «Ordine effettuato» = vinto (il bonifico differito si paga dopo l'ordine);
                            «Chiuso Perso» / «Ordine cancellato» = perso; «Add to Cart» / «In attesa di pagamento» = ancora aperto.
Abbinamento: stessa scuola (o stesso contatto), ordine creato dopo la richiesta (con 2 giorni di tolleranza) e almeno un codice
in comune. Se un ordine potrebbe appartenere a piu' trattative con lo stesso numero di codici in comune non si tocca niente e
si avvisa Andrea (una volta sola, registro chiusure_italiascuola.txt).

  python chiusura_italiascuola.py --prova     elenca cosa farebbe
  python chiusura_italiascuola.py             applica
  python chiusura_italiascuola.py --test      prova la logica senza rete
"""
import datetime
import hashlib
import io
import os
import re
import sys

QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, QUI)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if "--test" in sys.argv:
    os.environ.setdefault("HUBSPOT_TOKEN", "test")
import preventivi_corsi as C                                  # noqa: E402
import smtplib                                                # noqa: E402
import ssl                                                    # noqa: E402
from email.message import EmailMessage                        # noqa: E402

PIPELINE_IS = "4229750005"
STADI_APERTI = ["6238864575", "6238864576", "6238864577"]    # Richiesta ricevuta, Preventivo inviato, In trattativa
VINTA, PERSA = "6238864578", "6238864579"
PIPELINE_SPAGGIARI = "4128670920"                             # Formazione Spaggiari: mai toccata da qui
ORDINE = re.compile(r"-\s*(ERP|ScuolaShop)\s*$", re.I)
CODICE = re.compile(r"^(WBRITA|ITAC)", re.I)
TOLLERANZA_MS = 2 * 86400000
REGISTRO = os.path.join(QUI, "chiusure_italiascuola.txt")


def esito_stadio(etichetta):
    e = (etichetta or "").lower()
    if "vinto" in e or "ordine effettuato" in e:
        return "vinto"
    if "perso" in e or "cancellato" in e:
        return "perso"
    return "aperto"


def scegli_mosse(candidati):
    """candidati: {id ordine: {"nome", "esito", "trattative": [(id trattativa, n codici in comune, nome)]}}
    -> (mosse {id trattativa: (stadio, ordine)}, ambigui [(ordine, esito, [nomi])])."""
    mosse, ambigui = {}, []
    for c in candidati.values():
        c["trattative"].sort(key=lambda t: -t[1])
        migliore = c["trattative"][0]
        if len(c["trattative"]) > 1 and c["trattative"][1][1] == migliore[1]:
            ambigui.append((c["nome"], c["esito"], [t[2] for t in c["trattative"]]))
            continue
        nuovo = VINTA if c["esito"] == "vinto" else PERSA
        prec = mosse.get(migliore[0])
        if prec and prec[0] == VINTA:                         # una vinta vale piu' di una persa
            continue
        mosse[migliore[0]] = (nuovo, c["nome"])
    return mosse, ambigui


def registro():
    try:
        return {r.strip() for r in io.open(REGISTRO, encoding="utf-8") if r.strip()}
    except FileNotFoundError:
        return set()


def segna(chiave):
    with io.open(REGISTRO, "a", encoding="utf-8") as f:
        f.write(hashlib.sha256(chiave.encode("utf-8")).hexdigest() + "\n")


OPERATIVA = "svilupposcuola@spaggiari.eu"


def avvisa(oggetto, testo, a=None):
    utente, chiave = os.environ.get("SMTP_CORSI_USER"), os.environ.get("SMTP_CORSI_PASS")
    if not (utente and chiave):
        print("  avviso non inviato: credenziali SMTP assenti")
        return
    m = EmailMessage()
    m["From"] = "Italiascuola.it <%s>" % C.MITTENTE
    m["To"] = a or C.CONTROLLO
    m["Subject"] = oggetto
    m.set_content(testo)
    s = smtplib.SMTP(C.SMTP_HOST, C.SMTP_PORTA, timeout=60)
    s.starttls(context=ssl.create_default_context())
    s.login(utente, chiave)
    s.send_message(m)
    s.quit()


def stadi_ordini():
    out = {}
    for p in C.hs("/crm/v3/pipelines/deals")["results"]:
        if p["id"] in (PIPELINE_IS, PIPELINE_SPAGGIARI):
            continue
        for s in p["stages"]:
            out[s["id"]] = esito_stadio(s["label"])
    return out


def codici(deal_id):
    li = C.hs("/crm/v4/objects/deals/%s/associations/line_items" % deal_id).get("results", [])
    if not li:
        return set()
    r = C.hs("/crm/v3/objects/line_items/batch/read", {"properties": ["hs_sku"],
             "inputs": [{"id": str(x["toObjectId"])} for x in li]}, "POST")
    return {x["properties"].get("hs_sku") for x in r.get("results", []) if CODICE.match(x["properties"].get("hs_sku") or "")}


def trattative_aperte():
    out, dopo = [], None
    for _ in range(20):
        r = C.hs("/crm/v3/objects/deals/search", {"filterGroups": [{"filters": [
            {"propertyName": "pipeline", "operator": "EQ", "value": PIPELINE_IS},
            {"propertyName": "dealstage", "operator": "IN", "values": STADI_APERTI}]}],
            "properties": ["dealname", "createdate", "dealstage"], "limit": 100, **({"after": dopo} if dopo else {})}, "POST")
        out += r.get("results", [])
        dopo = (r.get("paging") or {}).get("next", {}).get("after")
        if not dopo:
            break
    return out


def ordini_di(oggetto, oid, da_ms):
    return C.hs("/crm/v3/objects/deals/search", {"filterGroups": [{"filters": [
        {"propertyName": "associations.%s" % oggetto, "operator": "EQ", "value": str(oid)},
        {"propertyName": "createdate", "operator": "GTE", "value": str(da_ms)},
        {"propertyName": "pipeline", "operator": "NEQ", "value": PIPELINE_IS},
        {"propertyName": "pipeline", "operator": "NEQ", "value": PIPELINE_SPAGGIARI}]}],
        "properties": ["dealname", "dealstage", "createdate"], "limit": 50}, "POST").get("results", [])


def main(prova=None):
    if prova is None:
        prova = "--prova" in sys.argv
    stato = stadi_ordini()
    aperte = trattative_aperte()
    print("trattative Italiascuola ancora aperte: %d%s" % (len(aperte), "   [PROVA]" if prova else ""))
    candidati, cod_f = {}, {}
    for f in aperte:
        cod_f[f["id"]] = codici(f["id"])
        if not cod_f[f["id"]]:
            continue
        da = int(datetime.datetime.fromisoformat(f["properties"]["createdate"].replace("Z", "+00:00")).timestamp() * 1000) - TOLLERANZA_MS
        trovati = {}
        for oggetto, plurale in (("company", "companies"), ("contact", "contacts")):   # ordini ERP: per scuola; ordini ScuolaShop: per contatto
            for x in C.hs("/crm/v4/objects/deals/%s/associations/%s" % (f["id"], plurale)).get("results", []):
                for e in ordini_di(oggetto, x["toObjectId"], da):
                    trovati[e["id"]] = e
        for e in trovati.values():
            if not ORDINE.search(e["properties"].get("dealname") or ""):
                continue
            esito = stato.get(e["properties"].get("dealstage"), "aperto")
            if esito == "aperto":
                continue
            comuni = cod_f[f["id"]] & codici(e["id"])
            if not comuni:
                continue
            c = candidati.setdefault(e["id"], {"nome": e["properties"]["dealname"], "esito": esito, "trattative": []})
            c["trattative"].append((f["id"], len(comuni), f["properties"]["dealname"]))
    mosse, ambigui = scegli_mosse(candidati)
    for nome, esito, nomi in ambigui:
        print("  AMBIGUO: %s (%s) potrebbe essere di %s: non tocco niente" % (nome, esito, ", ".join(nomi)))
        k = "ambiguo|%s" % nome
        if not prova and hashlib.sha256(k.encode("utf-8")).hexdigest() not in registro():
            try:
                avvisa("Trattative Italiascuola da chiudere a mano: %s" % nome,
                       "L'ordine %s (%s) corrisponde a piu' richieste con gli stessi corsi: %s.\n\nChiudi a mano quella giusta: "
                       "il sistema non sceglie da solo." % (nome, esito, "; ".join(nomi)))
                segna(k)
            except Exception as err:
                print("  avviso non inviato:", type(err).__name__)
    fatte = 0
    for tid, (stadio, ordine) in mosse.items():
        nome = next(f["properties"]["dealname"] for f in aperte if f["id"] == tid)
        print("  %s -> %s (ordine %s)" % (nome, "ORDINE CONFERMATO" if stadio == VINTA else "CHIUSA PERSA", ordine))
        if not prova:
            C.hs("/crm/v3/objects/deals/%s" % tid, {"properties": {"dealstage": stadio}}, "PATCH")
            fatte += 1
            ill = sorted(c for c in cod_f[tid] if c.endswith("-I"))
            k = "illimitata|%s" % ordine
            if stadio == VINTA and ill and hashlib.sha256(k.encode("utf-8")).hexdigest() not in registro():
                try:
                    avvisa("Registrazione illimitata da consegnare: %s" % nome,
                           "L'ordine %s della richiesta «%s» comprende la registrazione illimitata (%s).\n\n"
                           "Procedura: se la scuola usa ClasseViva, le persone si attivano dal pannello ClasseViva e il link viene pubblicato "
                           "dalla scuola in bacheca. Altrimenti inviare all'indirizzo istituzionale il file «partecipanti», caricare le persone "
                           "in ETB e far partire il link a ciascuna." % (ordine, nome, ", ".join(ill)), OPERATIVA)
                    segna(k)
                except Exception as err:
                    print("  avviso non inviato:", type(err).__name__)
    print("trattative da chiudere: %d · chiuse ora: %d" % (len(mosse), fatte))


def test():
    assert esito_stadio("Chiuso Vinto") == "vinto" and esito_stadio("Ordine effettuato") == "vinto"
    assert esito_stadio("Chiuso Perso") == "perso" and esito_stadio("Ordine cancellato") == "perso"
    assert esito_stadio("In attesa di pagamento") == "aperto" and esito_stadio("Add to Cart") == "aperto"
    assert ORDINE.search("2026SC03666 - ERP") and ORDINE.search("10789 - ScuolaShop") and not ORDINE.search("Italiascuola 5 - Scuola X")
    assert CODICE.match("WBRITAPCB2-1R") and CODICE.match("ITACNES/10") and not CODICE.match("WBRELEBILC-1")
    # un ordine, una trattativa
    m, a = scegli_mosse({"o1": {"nome": "1 - ERP", "esito": "vinto", "trattative": [("t1", 2, "Italiascuola 3 - A")]}})
    assert m == {"t1": (VINTA, "1 - ERP")} and not a
    # una vinta vale piu' di una persa sulla stessa trattativa
    m, a = scegli_mosse({"o1": {"nome": "1 - ERP", "esito": "perso", "trattative": [("t1", 1, "A")]},
                         "o2": {"nome": "2 - ERP", "esito": "vinto", "trattative": [("t1", 1, "A")]}})
    assert m["t1"][0] == VINTA
    m, a = scegli_mosse({"o1": {"nome": "1 - ERP", "esito": "vinto", "trattative": [("t1", 1, "A")]},
                         "o2": {"nome": "2 - ERP", "esito": "perso", "trattative": [("t1", 1, "A")]}})
    assert m["t1"][0] == VINTA
    # due trattative con lo stesso numero di codici in comune: ambiguo, nessuna mossa
    m, a = scegli_mosse({"o1": {"nome": "1 - ERP", "esito": "vinto", "trattative": [("t1", 2, "A"), ("t2", 2, "B")]}})
    assert not m and a and a[0][0] == "1 - ERP"
    # vince la trattativa con piu' codici in comune
    m, a = scegli_mosse({"o1": {"nome": "1 - ERP", "esito": "vinto", "trattative": [("t1", 1, "A"), ("t2", 3, "B")]}})
    assert m == {"t2": (VINTA, "1 - ERP")} and not a
    print("test ok: stati, nomi ordine, codici, abbinamenti (singolo, vinta>persa, ambiguo, piu' codici)")


if __name__ == "__main__":
    if "--test" in sys.argv:
        test()
        sys.exit(0)
    main()
