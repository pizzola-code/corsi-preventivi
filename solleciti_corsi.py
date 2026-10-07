# -*- coding: utf-8 -*-
"""Solleciti ai corsi senza partecipanti nominati (Andrea, 5 ott 2026).

Per ogni ordine di corso (riga d'ordine = ordine + corso) con evento in programma e NESSUNA persona nominata:
  - 3 giorni prima dell'inizio: promemoria alla scuola dal portale HubSpot (stessi destinatari dell'invito),
    solo se l'invito e' partito da almeno 24 ore;
  - 1 giorno prima: secondo promemoria alla scuola (se l'invito e' partito da almeno 6 ore) e avviso interno
    al team con l'elenco dei corsi ancora senza partecipanti;
  - 6 ore prima: ultimo avviso interno (la nomina si chiude 4 ore prima).
Ogni passo parte una volta sola (registro `ordini_corsi_gestiti.txt`, chiavi `ordine|codice|sollecito3`, `|sollecito1`,
`|allarme1`, `|allarme6`). Con CORSI_INVIO_AUTOMATICO diverso da 1 non parte nessuna e-mail alla scuola: solo l'avviso interno.
"""
import datetime
import os
import sys

import corsi_ordini as O

C = O.C
EV, REG = "2-143900361", "2-143900355"
EMAIL_PROMEMORIA = 485046044875          # «CORSI - Promemoria nomina partecipanti (invio da API)»
ORE_3G, ORE_1G, ORE_ALLARME = 72, 24, 6


def _ore_a(iso):
    ini = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (ini - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 3600.0


def _ore_da(iso):
    d = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (datetime.datetime.now(datetime.timezone.utc) - d).total_seconds() / 3600.0


def nominati(evento_id, ordine):
    r = C.hs("/crm/v3/objects/%s/search" % REG, {"filterGroups": [{"filters": [
        {"propertyName": "event_id", "operator": "EQ", "value": str(evento_id)},
        {"propertyName": "ordine_corso", "operator": "EQ", "value": ordine},
        {"propertyName": "status", "operator": "NEQ", "value": "Canceled"}]}], "properties": ["email"], "limit": 1}, "POST")
    return r.get("total", len(r.get("results", [])))


def allerta_agente(deal_id, azienda_id, scuola, corsi, chiave):
    """Compito in HubSpot + SMS all'agente dell'ordine (proprietario dell'affare ERP): la scuola non ha ancora indicato i
    partecipanti. Una volta per chiave (registro). In prova (ALLERTA_PROVA_OWNER=<id proprietario>) tutto va a quella persona."""
    gia = O.registro()
    if chiave in gia:
        return False
    owner = os.environ.get("ALLERTA_PROVA_OWNER") or C.hs("/crm/v3/objects/deals/%s?properties=hubspot_owner_id" % deal_id)["properties"].get("hubspot_owner_id")
    if not owner:
        print("  [allerta agente] %s: l'ordine non ha un agente" % scuola)
        return False
    O.segna(chiave)          # PRIMA di creare compito e SMS: al massimo una volta, anche se un passo seguente va in errore
    # seconda difesa: se il registro non si e' salvato, HubSpot dice comunque se l'agente ha gia' il compito (ultime 20 ore)
    try:
        esistenti = C.hs("/crm/v4/objects/deals/%s/associations/tasks" % deal_id).get("results", [])
        if esistenti:
            r = C.hs("/crm/v3/objects/tasks/batch/read", {"properties": ["hs_task_subject", "hs_createdate"],
                     "inputs": [{"id": str(x["toObjectId"])} for x in esistenti[:50]]}, "POST").get("results", [])
            limite = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=20)).isoformat()
            for x in r:
                p = x["properties"]
                if (p.get("hs_task_subject") or "").startswith("Corsi:") and "non ha ancora indicato" in (p.get("hs_task_subject") or "")                         and (p.get("hs_createdate") or "") >= limite[:19]:
                    print("  [allerta agente] %s: compito gia' presente, non ne creo un altro" % scuola)
                    return False
    except Exception as e:
        print("  [allerta agente] controllo compiti esistenti non riuscito (%s)" % type(e).__name__)
    domani = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)).strftime("%Y-%m-%dT06:00:00Z")
    elenco = "\n".join("- %s (%s)" % (c["corso"], c["quando"]) for c in corsi)
    corpo = ("La scuola non ha ancora indicato chi partecipa ai corsi ordinati:\n%s\n\n"
             "L'invito con il link per indicare i partecipanti e' gia' partito alla scuola. Puoi ricordarglielo con una telefonata: "
             "basta il link nell'e-mail ricevuta (anche nella posta indesiderata). Se la scuola ti manda i nomi, inoltrali all'assistenza corsi "
             "con scuola, corso, nome, cognome ed e-mail." % elenco)
    t = C.hs("/crm/v3/objects/tasks", {"properties": {
        "hs_task_subject": "Corsi: %s non ha ancora indicato i partecipanti" % scuola,
        "hs_task_body": corpo.replace("\n", "<br>"), "hs_task_type": "CALL", "hs_task_priority": "HIGH",
        "hs_task_status": "NOT_STARTED", "hubspot_owner_id": owner, "hs_timestamp": domani}}, "POST")
    if t.get("id"):
        for tipo, idx in (("deals", deal_id), ("companies", azienda_id)):
            if not idx:
                continue
            try:
                C.lega("tasks", t["id"], tipo, idx)
            except Exception as e:
                print("  [allerta agente] collegamento %s non riuscito (%s)" % (tipo, type(e).__name__))
    # SMS: il cellulare dell'agente e' nella sua scheda contatto (stesso indirizzo e-mail del proprietario)
    esito = "senza cellulare"
    try:
        mail = C.hs("/crm/v3/owners/%s?idProperty=id" % owner).get("email")
        r = C.hs("/crm/v3/objects/contacts/search", {"filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": mail}]}],
                 "properties": ["mobilephone", "phone"], "limit": 1}, "POST").get("results", [])
        num = None
        for k in ("mobilephone", "phone"):
            num = num or C.numero_cellulare((r[0]["properties"].get(k) if r else None) or "")
        if num:
            testo = ("Spaggiari: la scuola %s non ha ancora indicato i partecipanti ai corsi ordinati. "
                     "Puoi ricordarglielo? Dettagli nel compito su HubSpot." % scuola[:40])
            esito = C.invia_sms(num, testo[:300])
    except Exception as e:
        esito = "errore SMS (%s)" % type(e).__name__
    print("  [allerta agente] %s -> compito %s, SMS: %s" % (scuola, t.get("id"), esito))
    return True


GIORNI_SOLLECITO_ORDINE = 3      # primo sollecito: 3 giorni dopo l'ordine, per i corsi ancora senza partecipanti e lontani piu' di 72 ore


def sollecito_anticipato(ordini, gia, auto, prova):
    """Una sola e-mail per ORDINE (non per corso), con i corsi che non hanno ancora nessun partecipante indicato e che
    iniziano fra piu' di 72 ore (quelli piu' vicini li seguono i solleciti a 3 e 1 giorno). Una volta sola per ordine."""
    per_ordine = {}
    for o in ordini:                                   # solo righe gia' invitate e ordini non ancora sollecitati
        if "%s|%s|inviato" % (o["num"], o["sku"]) not in gia or "%s|sollecito_ordine" % o["num"] in gia:
            continue
        per_ordine.setdefault(o["num"], []).append(o)
    for num, righe in per_ordine.items():
        deal = righe[0]["deal"]
        creato = C.hs("/crm/v3/objects/deals/%s?properties=createdate" % deal)["properties"].get("createdate")
        if not creato or _ore_da(creato) < GIORNI_SOLLECITO_ORDINE * 24:
            continue
        voci = []
        for o in righe:
            base = O.CORSO.match(o["sku"]).group(1)
            ev, _ = O.evento_per(base, crea=False, prova=True)
            if not ev:
                continue
            p = ev["properties"] if "start_datetime" in ev["properties"] else C.hs(
                "/crm/v3/objects/%s/%s?properties=name,start_datetime,end_datetime,annullato" % (EV, ev["id"]))["properties"]
            if not p.get("start_datetime") or p.get("annullato") == "true" or _ore_a(p["start_datetime"]) <= ORE_3G:
                continue
            if nominati(ev["id"], o["num"]) > 0:
                continue
            v, _ = O.prepara(o, True)
            if v and not v.get("mancante"):
                voci.append(v)
        if not voci:
            continue
        voci = voci[:8]
        dest, motivo = O.destinatari(voci[0]["azienda_id"])
        if not dest:
            print("  [solleciti] %s: destinatari da decidere (%s)" % (num, motivo))
            continue
        agente = O.email_agente(deal)
        cc = O.CC_FISSI + ([agente] if agente and agente not in O.CC_FISSI else [])
        dati = O.dati_email(voci)
        dati["oggetto"] = ("Promemoria: indicate chi partecipa al corso «%s»" % voci[0]["corso"]) if len(voci) == 1 else "Promemoria: indicate chi partecipa ai vostri corsi"
        print("  [solleciti] ordine %s: %d corsi senza partecipanti -> %s%s" % (num, len(voci), ", ".join(e for e, _ in dest), " [prova]" if prova or not auto else ""))
        if prova or not auto:
            continue
        O.invia([e for e, _ in dest], cc, dati)
        O.segna("%s|sollecito_ordine" % num)
        allerta_agente(deal, voci[0]["azienda_id"], O.pulito(voci[0]["azienda"].get("name")), voci, "%s|allerta_agente" % num)


def solleciti(ordini, auto, prova):
    sollecito_anticipato(ordini, O.registro(), auto, prova)
    gia = O.registro()
    ora_ordine = {}
    senza = []                                  # per l'avviso interno
    for o in ordini:
        if "%s|%s|inviato" % (o["num"], o["sku"]) not in gia:
            continue                            # l'invito non e' ancora partito: niente promemoria
        base = O.CORSO.match(o["sku"]).group(1)
        ev, _ = O.evento_per(base, crea=False, prova=True)
        if not ev:
            continue
        p = ev["properties"] if "start_datetime" in ev["properties"] else C.hs(
            "/crm/v3/objects/%s/%s?properties=name,start_datetime,end_datetime,annullato" % (EV, ev["id"]))["properties"]
        if not p.get("start_datetime") or p.get("annullato") == "true":
            continue
        ore = _ore_a(p["start_datetime"])
        if ore <= 0 or ore > ORE_3G:
            continue
        if nominati(ev["id"], o["num"]) > 0:
            continue
        if o["deal"] not in ora_ordine:
            ora_ordine[o["deal"]] = C.hs("/crm/v3/objects/deals/%s?properties=createdate" % o["deal"])["properties"].get("createdate")
        eta_ordine = _ore_da(ora_ordine[o["deal"]]) if ora_ordine[o["deal"]] else 999
        k3, k1, a1, a6 = ("%s|%s|%s" % (o["num"], o["sku"], x) for x in ("sollecito3", "sollecito1", "allarme1", "allarme6"))
        v, _ = O.prepara(o, True)
        if not v or v.get("mancante"):
            continue
        da_mandare = None
        if ore <= ORE_1G and k1 not in gia and eta_ordine >= 6:
            da_mandare = k1
        elif ore <= ORE_3G and ore > ORE_1G and k3 not in gia and eta_ordine >= 24:
            da_mandare = k3
        # avvisi al team
        nome = O.pulito(v["azienda"].get("name"))
        if ore <= ORE_1G and not prova and auto:
            allerta_agente(o["deal"], v["azienda_id"], nome, [v], "%s|%s|allerta_agente_1g" % (o["num"], o["sku"]))
        if ore <= ORE_ALLARME and a6 not in gia:
            senza.append((a6, "<b>%s</b> · %s · %s · il corso inizia fra circa %d ore, la nomina si chiude 4 ore prima" % (nome, v["corso"], v["quando"], round(ore))))
        elif ore <= ORE_1G and a1 not in gia and ore > ORE_ALLARME:
            senza.append((a1, "<b>%s</b> · %s · %s · nessun partecipante indicato a meno di 24 ore dall'inizio" % (nome, v["corso"], v["quando"])))
        if not da_mandare:
            continue
        if not auto:
            print("  [solleciti] %s %s: promemoria da mandare (invio automatico spento)" % (o["num"], o["sku"]))
            continue
        dest, motivo = O.destinatari(v["azienda_id"])
        if not dest:
            print("  [solleciti] %s %s: destinatari da decidere (%s)" % (o["num"], o["sku"], motivo))
            continue
        agente = O.email_agente(o["deal"])
        cc = O.CC_FISSI + ([agente] if agente and agente not in O.CC_FISSI else [])
        dati = O.dati_email([v])
        dati["oggetto"] = "Promemoria: corso «%s», indicare chi partecipa" % v["corso"]
        print("  [solleciti] %s %s -> %s (%s)%s" % (o["num"], o["sku"], ", ".join(e for e, _ in dest), "3 giorni" if da_mandare == k3 else "1 giorno", " [prova]" if prova else ""))
        if prova:
            continue
        corpo = {"emailId": EMAIL_PROMEMORIA, "message": {"to": dest[0][0], "cc": [x for x in ([e for e, _ in dest[1:]] + cc) if x], "bcc": O.BCC},
                 "customProperties": dati}
        C.hs("/marketing/v3/transactional/single-email/send", corpo, "POST")
        O.segna(da_mandare)
    if senza and not prova:
        righe = [r for _, r in senza]
        O.avviso_interno("Corsi in diretta senza partecipanti indicati", righe + [
            "La scuola ha ricevuto l'invito con il link di nomina. Se serve si puo' indicare qualcuno a mano dalla funzione di nomina."])
        for k, _ in senza:
            O.segna(k)


if __name__ == "__main__":
    prova = "--prova" in sys.argv
    solleciti(O.ordini_nuovi(60), os.environ.get("CORSI_INVIO_AUTOMATICO") == "1", prova)
    O.spedisci_riepilogo()
