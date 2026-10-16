# -*- coding: utf-8 -*-
"""Mappa codice articolo -> pagina del prodotto su ScuolaShop, canale Scuole (www.scuolashop.it/scuole/...).

Il motore dei preventivi la legge per mettere nel preventivo e nell'email il link a ogni corso e per sapere quali codici
NON sono ancora ordinabili online (in quel caso il testo rimanda a info@italiascuola.it).
Solo prodotti attivi WBRITA* / ITAC*. Scrive shop_scuole.json. Da rilanciare dopo ogni caricamento prodotti sullo shop.

  python genera_shop_scuole.py
Richiede le credenziali Shopware (~/.shopware_scuolashop_token.txt o variabili SW_CLIENT_ID / SW_CLIENT_SECRET).
"""
import datetime, json, os, sys
sys.stdout.reconfigure(encoding='utf8')
sys.path.insert(0, os.path.expanduser('~/shopware-scuolashop/lib'))
import sw

QUI = os.path.dirname(os.path.abspath(__file__))
BASE = 'https://www.scuolashop.it/scuole/'


def main():
    prod = {}
    for pref in ('WBRITA', 'ITAC'):
        pagina = 1
        while True:
            r = sw.search('product', {'filter': [{'type': 'prefix', 'field': 'productNumber', 'value': pref},
                                                {'type': 'equals', 'field': 'active', 'value': True}],
                                      'limit': 500, 'page': pagina, 'includes': {'product': ['id', 'productNumber', 'parentId']}})
            for p in r['data']:
                prod[p['id']] = p['productNumber']
            if len(r['data']) < 500:
                break
            pagina += 1
    ids = list(prod)
    url = {}
    for i in range(0, len(ids), 100):
        r = sw.search('seo-url', {'filter': [{'type': 'equals', 'field': 'routeName', 'value': 'frontend.detail.page'},
                                             {'type': 'equals', 'field': 'salesChannelId', 'value': sw.CHANNELS['scuole']},
                                             {'type': 'equals', 'field': 'isCanonical', 'value': True},
                                             {'type': 'equalsAny', 'field': 'foreignKey', 'value': '|'.join(ids[i:i + 100])}],
                                  'limit': 100, 'includes': {'seo_url': ['foreignKey', 'seoPathInfo']}})
        for x in r['data']:
            url[prod[x['foreignKey']]] = BASE + x['seoPathInfo'].lstrip('/')
    out = {'generato': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
           'home': BASE, 'prodotti': dict(sorted(url.items()))}
    json.dump(out, open(os.path.join(QUI, 'shop_scuole.json'), 'w', encoding='utf8'), ensure_ascii=False, indent=0)
    print(len(prod), 'prodotti attivi;', len(url), 'con pagina nel canale Scuole')


if __name__ == '__main__':
    main()
