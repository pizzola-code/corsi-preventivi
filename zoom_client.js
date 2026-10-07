// Client Zoom Server-to-Server OAuth.
// In cloud legge le credenziali dalle variabili d'ambiente, in locale da ~/.zoom_s2s.json.
const fs = require('fs'), https = require('https'), os = require('os'), path = require('path');

function credenziali() {
  if (process.env.ZOOM_ACCOUNT_ID && process.env.ZOOM_CLIENT_ID && process.env.ZOOM_CLIENT_SECRET) {
    return {
      account_id: process.env.ZOOM_ACCOUNT_ID.trim(),
      client_id: process.env.ZOOM_CLIENT_ID.trim(),
      client_secret: process.env.ZOOM_CLIENT_SECRET.trim()
    };
  }
  const f = path.join(os.homedir(), '.zoom_s2s.json');
  if (!fs.existsSync(f)) throw new Error('credenziali Zoom assenti (né variabili d\'ambiente né ' + f + ')');
  const c = JSON.parse(fs.readFileSync(f, 'utf8').replace(/^﻿/, ''));
  for (const k of ['account_id', 'client_id', 'client_secret']) if (!c[k]) throw new Error('manca ' + k);
  return c;
}

function richiesta(opzioni, corpo) {
  return new Promise((res, rej) => {
    const r = https.request(opzioni, x => { let b = ''; x.on('data', c => b += c); x.on('end', () => res({ code: x.statusCode, body: b })); });
    r.on('error', rej); if (corpo) r.write(corpo); r.end();
  });
}

let cache = null;
async function token() {
  if (cache && cache.scade > Date.now()) return cache.valore;
  const c = credenziali();
  const auth = Buffer.from(c.client_id + ':' + c.client_secret).toString('base64');
  const corpo = new URLSearchParams({ grant_type: 'account_credentials', account_id: c.account_id }).toString();
  const r = await richiesta({
    hostname: 'zoom.us', path: '/oauth/token', method: 'POST',
    headers: { Authorization: 'Basic ' + auth, 'Content-Type': 'application/x-www-form-urlencoded', 'Content-Length': Buffer.byteLength(corpo) }
  }, corpo);
  const j = JSON.parse(r.body || '{}');
  if (!j.access_token) throw new Error('token Zoom non ottenuto (' + r.code + '): ' + (j.reason || j.message || ''));
  cache = { valore: j.access_token, scade: Date.now() + (j.expires_in - 60) * 1000 };
  return cache.valore;
}

async function api(metodo, percorso, corpo, tent = 0) {
  const t = await token();
  const d = corpo ? JSON.stringify(corpo) : null;
  const h = { Authorization: 'Bearer ' + t, 'Content-Type': 'application/json' };
  if (d) h['Content-Length'] = Buffer.byteLength(d);
  const r = await richiesta({ hostname: 'api.zoom.us', path: '/v2' + percorso, method: metodo, headers: h }, d);
  if (r.code === 429 && tent < 4) {
    await new Promise(x => setTimeout(x, 2000 * (tent + 1)));
    return api(metodo, percorso, corpo, tent + 1);
  }
  let j = null; try { j = r.body ? JSON.parse(r.body) : null; } catch { j = { raw: r.body }; }
  if (r.code >= 400) {
    const e = new Error('Zoom ' + metodo + ' ' + percorso + ' -> ' + r.code + ': ' + ((j && (j.message || j.reason)) || ''));
    e.code = r.code; throw e;
  }
  return j;
}

module.exports = { api, token };
