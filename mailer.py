"""
mailer.py — invio email riusabile via Microsoft Graph (refresh_token delegato).
Porting da borsa-monitor/mailer.js. Aggira le Security Defaults del tenant
che bloccano la basic auth SMTP. Se il refresh_token ruota, lo scrive in
new_refresh_token.txt: il workflow lo ripersiste come GitHub secret.
"""
import os
import json
import urllib.request
import urllib.parse


def _http_post(url, headers, body):
    req = urllib.request.Request(url, data=body.encode() if isinstance(body, str) else body,
                                  headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def get_graph_token(tenant, client_id, refresh_token):
    form = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": "https://graph.microsoft.com/Mail.Send offline_access",
    }
    status, body = _http_post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        {"Content-Type": "application/x-www-form-urlencoded"},
        urllib.parse.urlencode(form),
    )
    j = json.loads(body)
    if "access_token" not in j:
        raise RuntimeError("Graph token error: " + body)
    return j


def graph_send_mail(token, to, subject, html, allegati=None):
    """allegati: lista di (nome, bytes). Graph li vuole in base64 dentro il
    messaggio, e sopra i ~4 MB complessivi rifiuta: per file piu' grandi
    servirebbe una sessione di upload, che qui non serve."""
    import base64
    msg = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": a.strip()}} for a in to.split(",")],
        },
        "saveToSentItems": True,
    }
    if allegati:
        msg["message"]["attachments"] = [{
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": nome,
            "contentBytes": base64.b64encode(dati).decode(),
        } for nome, dati in allegati]
    status, body = _http_post(
        "https://graph.microsoft.com/v1.0/me/sendMail",
        {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        json.dumps(msg),
    )
    if status != 202:
        raise RuntimeError(f"Graph sendMail HTTP {status}: {body}")


# Destinatari delle mail OPERATIVE del motore (approvazione e conferma invii):
# solo chi decide e chi esegue. Andrea, 28/08/2026: "mandala solo a me e Abir".
# Il riepilogo giornaliero del calendario NON usa questa lista: va a tutto il team
# (lista interna 4236 + copia fissa) e resta cosi'.
OPERATIVI = "pizzola@spaggiari.eu,mohacht@spaggiarinet.eu"


def send_report(subject, html, to=None, allegati=None):
    dest = to or os.environ.get("MAIL_TO") or os.environ.get("GRAPH_FROM")
    tenant = os.environ.get("GRAPH_TENANT_ID")
    client_id = os.environ.get("GRAPH_CLIENT_ID")
    refresh_token = os.environ.get("GRAPH_REFRESH_TOKEN")
    if not (tenant and client_id and refresh_token):
        print("(Nessun canale email configurato: nessuna email inviata)")
        return
    tok = get_graph_token(tenant, client_id, refresh_token)
    graph_send_mail(tok["access_token"], dest, subject, html, allegati)
    new_rt = tok.get("refresh_token")
    if new_rt and new_rt != refresh_token:
        with open("new_refresh_token.txt", "w") as f:
            f.write(new_rt)
    print(f"Email inviata via Graph a {dest}")
