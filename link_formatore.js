/*
 * Un'ora prima di ogni corso in diretta, il formatore riceve il link per AVVIARE la riunione.
 *
 * Perche' il giorno stesso: il link di avvio di Zoom (start_url) vale poche ore, non si puo' mandare
 * giorni prima. L'e-mail parte dal portale HubSpot (invio transazionale, modello
 * «CORSI - Link per il formatore»), come le altre comunicazioni dei corsi.
 *
 * Quali eventi: nome che inizia per «Corso |», con riunione Zoom, che iniziano entro 100 minuti (e secondo invio a 25 minuti dall inizio)
 * (e non da piu' di 10) e per cui `formatore_link_inviato_il` e' vuoto. Il formatore e' il relatore associato
 * all'evento (oggetto «hapily speaker», proprieta' e-mail); se manca l'indirizzo l'e-mail va a Malerba
 * con Andrea Pizzola in copia, con la scritta che il formatore non e' indicato.
 *
 *   node link_formatore.js                       giro normale
 *   ANTEPRIMA=1 node link_formatore.js           elenca senza inviare
 *   EVENTO=<id> PROVA_A=indirizzo node link_formatore.js
 *                                                prova: manda a PROVA_A, qualunque ora, senza segnare
 */
const fs = require('fs'), os = require('os'), path = require('path');
const { api: zoom } = require('./zoom_client');
const TOKEN = (process.env.HUBSPOT_TOKEN || fs.readFileSync(path.join(os.homedir(), '.hubspot_pat.txt'), 'utf8')).trim();
const EV = '2-143900361', REG = '2-143900355';
const EMAIL_ID = 483372785852;          // «CORSI - Link per il formatore (invio da API)»
const ANTEPRIMA = process.env.ANTEPRIMA === '1';
const PROVA_A = process.env.PROVA_A || '';
const EVENTO = process.env.EVENTO || '';
const FALLBACK = { a: 'malerba@spaggiari.eu', cc: ['pizzola@spaggiari.eu'] };
const GIORNI = ['domenica', 'lunedì', 'martedì', 'mercoledì', 'giovedì', 'venerdì', 'sabato'];
const MESI = ['gennaio', 'febbraio', 'marzo', 'aprile', 'maggio', 'giugno', 'luglio', 'agosto', 'settembre', 'ottobre', 'novembre', 'dicembre'];
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function hs(m, p, c, t = 0) {
  const r = await fetch('https://api.hubapi.com' + p, { method: m, headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' }, body: c ? JSON.stringify(c) : undefined });
  if ((r.status === 429 || r.status >= 500) && t < 5) { await sleep(1200 * (t + 1)); return hs(m, p, c, t + 1); }
  const x = await r.text(); if (!r.ok) throw new Error(m + ' ' + p.slice(0, 60) + ' ' + r.status + ' ' + x.slice(0, 160)); return x ? JSON.parse(x) : {};
}
function quando(iso, fine) {
  // ora italiana: legale fino al 25/10/2026 (+2), poi +1
  const off = d => (d.getTime() < Date.UTC(2026, 9, 25, 1, 0) ? 2 : 1);
  const a = new Date(iso), b = new Date(fine || iso);
  const la = new Date(a.getTime() + off(a) * 3600000), lb = new Date(b.getTime() + off(b) * 3600000);
  const hh = d => String(d.getUTCHours()).padStart(2, '0') + ':' + String(d.getUTCMinutes()).padStart(2, '0');
  return `${GIORNI[la.getUTCDay()]} ${la.getUTCDate()} ${MESI[la.getUTCMonth()]} ${la.getUTCFullYear()}, ore ${hh(la)}–${hh(lb)}`;
}

(async () => {
  const adesso = Date.now();
  const filtri = EVENTO
    ? [{ propertyName: 'hs_object_id', operator: 'EQ', value: EVENTO }]
    : [{ propertyName: 'start_datetime', operator: 'BETWEEN', value: new Date(adesso - 10 * 60000).toISOString(), highValue: new Date(adesso + 100 * 60000).toISOString() }];
  const r = await hs('POST', `/crm/v3/objects/${EV}/search`, { filterGroups: [{ filters: filtri }],
    properties: ['name', 'start_datetime', 'end_datetime', 'external_id', 'meeting_link', 'annullato', 'formatore_link_inviato_il', 'formatore_promemoria_inviato_il'], limit: 50 });
  const corsi = (r.results || []).filter(e => /^\s*corso\s*\|/i.test(e.properties.name || '') && e.properties.annullato !== 'true' &&
    (EVENTO || !e.properties.formatore_link_inviato_il ||
      (!e.properties.formatore_promemoria_inviato_il && Date.parse(e.properties.start_datetime) - adesso <= 25 * 60000)));
  console.log(`corsi in partenza a breve senza link al formatore: ${corsi.length}`);
  for (const e of corsi) {
    const p = e.properties;
    const mt = String(p.meeting_link || '').match(/zoom\.us\/(?:[sjw]\/)?(\d{9,})/i);
    if (!mt) { console.log('  ' + p.name + ': senza riunione Zoom'); continue; }
    // il relatore e' quello associato all'evento (oggetto «hapily speaker»); l'indirizzo sta nella sua proprieta' e-mail
    const ass = ((await hs('GET', `/crm/v4/objects/${EV}/${e.id}/associations/2-144750696`)).results || []).map(x => ({ id: String(x.toObjectId) }));
    const rel = ass.length ? ((await hs('POST', '/crm/v3/objects/2-144750696/batch/read', { properties: ['name', 'email'], inputs: ass })).results || []) : [];
    const v = { formatore: rel.map(x => x.properties.name).join(', ') };
    const mail = rel.map(x => String(x.properties.email || '').trim()).filter(Boolean);
    const iscr = await hs('POST', `/crm/v3/objects/${REG}/search`, { filterGroups: [{ filters: [{ propertyName: 'event_id', operator: 'EQ', value: e.id }] }], properties: ['status'], limit: 200 });
    const iscritti = (iscr.results || []).filter(x => x.properties.status !== 'Canceled').length;
    const riun = await zoom('GET', '/meetings/' + mt[1]);
    if (!riun.start_url) { console.log('  ' + p.name + ': Zoom non ha dato il link di avvio'); continue; }
    const promemoria = !EVENTO && !!p.formatore_link_inviato_il;     // secondo invio, poco prima dell'inizio, con un link di avvio nuovo
    const dati = { corso: String(p.name).replace(/^Corso \| /, ''), quando: quando(p.start_datetime, p.end_datetime) + (promemoria ? ' · promemoria: nuovo link di avvio' : ''), iscritti: String(iscritti), link_host: riun.start_url };
    let a, cc = [];
    if (PROVA_A) { a = PROVA_A; }
    else if (mail.length) { a = mail[0]; cc = [...mail.slice(1), 'malerba@spaggiari.eu']; }
    else { a = FALLBACK.a; cc = FALLBACK.cc; dati.corso += ' (formatore non indicato)'; }
    if (process.env.LOG_RISERVATO === '1') console.log(`  corso in partenza: link ${promemoria ? 'di promemoria ' : ''}al formatore, iscritti ${iscritti}`);   // repository pubblico: niente nomi né indirizzi nei log
    else console.log(`  ${p.name} · ${dati.quando} · iscritti ${iscritti} · a ${a}${cc.length ? ' (cc ' + cc.join(', ') + ')' : ''}${v.formatore ? ' · relatore ' + v.formatore : ''}`);
    if (ANTEPRIMA) continue;
    const res = await hs('POST', '/marketing/v3/transactional/single-email/send', { emailId: EMAIL_ID, message: { to: a, cc }, customProperties: dati });
    console.log('     inviata da HubSpot:', res.status);
    if (!PROVA_A) await hs('PATCH', `/crm/v3/objects/${EV}/${e.id}`, { properties: promemoria ? { formatore_promemoria_inviato_il: String(Date.now()) } : { formatore_link_inviato_il: String(Date.now()) } });
  }
})().catch(e => { console.error(e.message); process.exit(1); });
