# -*- coding: utf-8 -*-
"""Link personale alla registrazione di un corso (Andrea, 16/10/2026).

Il codice e' firmato (HMAC con la chiave delle nomine, messaggio con prefisso «reg:») e porta dentro iscrizione, evento e scadenza.
La pagina https://www.spaggiari.eu/guarda-registrazione?t=<codice> chiede il video alla funzione CMS `registrazione-corso`,
che controlla firma, scadenza (30 giorni) e massimo 3 dispositivi. Il link Vimeo non viene mai scritto nelle e-mail.
"""
import base64
import hashlib
import hmac
import json
import os
import time

import corsi_ordini as O
import preventivi_corsi as C

PAGINA = "https://www.spaggiari.eu/guarda-registrazione"
GIORNI = 30
REG = O.REG


def _b64u(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def codice(iscrizione_id, evento_id, scadenza_ms):
    payload = _b64u(json.dumps({"r": str(iscrizione_id), "e": str(evento_id), "x": int(scadenza_ms)}, separators=(",", ":")).encode("utf-8"))
    firma = _b64u(hmac.new(O.chiave().encode(), ("reg:" + payload).encode(), hashlib.sha256).digest())[:32]
    return payload + "." + firma


def link(iscrizione_id, evento_id, scadenza_ms=None):
    """Link personale; la scadenza (30 giorni) si fissa la prima volta e resta quella: si legge dall'iscrizione se c'e'."""
    if scadenza_ms is None:
        scadenza_ms = int((time.time() + GIORNI * 86400) * 1000)
    return "%s?t=%s" % (PAGINA, codice(iscrizione_id, evento_id, scadenza_ms)), scadenza_ms


def scadenza_iscrizione(iscrizione_id):
    """Scadenza gia' scritta sull'iscrizione (ms) o None."""
    p = C.hs("/crm/v3/objects/%s/%s?properties=registrazione_scadenza" % (REG, iscrizione_id)).get("properties", {})
    v = p.get("registrazione_scadenza")
    if not v:
        return None
    try:
        import datetime
        return int(datetime.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return int(v) if str(v).isdigit() else None
