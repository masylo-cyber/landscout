"""Diagnostyka źródeł: co zwracają portale (status, rozmiar, czy są znaczniki JSON)."""
from __future__ import annotations

import re

from .collectors.base import client
from .collectors.olx import BASE as OLX_BASE, PARAMS as OLX_PARAMS, _ads, _state
from .collectors.otodom import URL as OTODOM_URL, _items


def diag() -> None:
    with client() as c:
        print("== OLX ==")
        url = OLX_BASE.format(region="piaseczno") + OLX_PARAMS.format(page=1)
        try:
            r = c.get(url)
            html = r.text
            st = _state(html)
            print(f"status={r.status_code} len={len(html)} final_url={r.url}")
            print(f"PRERENDERED_STATE: {'tak' if st else 'nie'}; ads={len(_ads(st)) if st else 0}")
            if not st:
                m = re.findall(r"window\.__\w+__", html)[:10]
                print("znalezione zmienne window.__*__:", m)
                print("tytuł strony:", re.search(r"<title>(.*?)</title>", html, re.S).group(1)[:120] if "<title>" in html else "-")
                print("fragment:", html[:300].replace("\n", " "))
        except Exception as e:  # noqa: BLE001
            print("błąd:", e)
        from .collectors.olx import API, API_HEADERS
        try:
            r = c.get(API.format(offset=0, q="piaseczno"), headers=API_HEADERS)
            print(f"OLX API: status={r.status_code} len={len(r.text)} fragment={r.text[:160]!r}")
        except Exception as e:  # noqa: BLE001
            print("OLX API błąd:", e)

        from .collectors.base import apify_proxy_url, proxied_client
        if apify_proxy_url():
            for res in (True, False):
                try:
                    with proxied_client(residential=res) as pc:
                        r = pc.get(OLX_BASE.format(region="piaseczno") + OLX_PARAMS.format(page=1))
                        st = _state(r.text)
                        print(f"OLX przez Apify Proxy (residential={res}): status={r.status_code} len={len(r.text)} state={'tak' if st else 'nie'} ads={len(_ads(st)) if st else 0}")
                        if r.status_code != 200:
                            print("   ", r.text[:160].replace("\n", " "))
                except Exception as e:  # noqa: BLE001
                    print(f"OLX proxy (residential={res}) błąd:", e)
        else:
            print("OLX przez Apify Proxy: brak APIFY_TOKEN w .env")

        print("\n== OTODOM ==")
        url = OTODOM_URL.format(estate="dzialka", loc="mazowieckie/piaseczynski", extra="areaMin=600", page=1)
        try:
            r = c.get(url)
            html = r.text
            items = _items(html)
            print(f"status={r.status_code} len={len(html)} final_url={r.url}")
            print(f"__NEXT_DATA__: {'tak' if '__NEXT_DATA__' in html else 'nie'}; items={len(items)}")
            if "__NEXT_DATA__" in html and not items:
                import json
                m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
                try:
                    data = json.loads(m.group(1))
                    pp = data.get("props", {}).get("pageProps", {})
                    print("klucze pageProps:", list(pp.keys())[:25])
                    for k, v in pp.items():
                        if isinstance(v, dict):
                            print(f"  {k}: {list(v.keys())[:15]}")
                    print("wystąpienia 'searchAds':", m.group(1).count("searchAds"), " 'areaInSquareMeters':", m.group(1).count("areaInSquareMeters"))
                except Exception as e:  # noqa: BLE001
                    print("JSON błąd:", e)
            else:
                print("przykład:", {k: items[0].get(k) for k in list(items[0].keys())[:12]} if items else "-")
            if not items:
                print("tytuł strony:", re.search(r"<title>(.*?)</title>", html, re.S).group(1)[:120] if "<title>" in html else "-")
                print("fragment:", html[:300].replace("\n", " "))
        except Exception as e:  # noqa: BLE001
            print("błąd:", e)

        print("\n== KOMORNIK ==")
        try:
            r = c.get("https://licytacje.komornik.pl/")
            html = r.text
            print(f"status={r.status_code} len={len(html)} final_url={r.url}")
            print("linki /api i ścieżki:", sorted(set(re.findall(r'["\'](/(?:api|v\d|items|nieruchomosci|obwieszczenia)[^"\'\s<>]{0,80})', html)))[:25])
            print("hosty api:", sorted(set(re.findall(r'https?://[a-z0-9.-]*api[a-z0-9.-]*\.[a-z]+[^"\'\s<>]{0,60}', html)))[:10])
            scripts = re.findall(r'src="([^"]+\.js)"', html)[:3]
            print("skrypty:", scripts)
            for sc in scripts:
                try:
                    js = c.get("https://licytacje.komornik.pl" + sc if sc.startswith("/") else sc).text
                    paths = sorted(set(re.findall(r'["`\'](/api/[a-zA-Z0-9_\-/]{2,60})', js)))
                    print(f"  {sc}: len={len(js)} /api/*: {paths[:40]}")
                    print("  strapi:", sorted(set(re.findall(r'strapi[a-zA-Z0-9_\-./]{0,60}', js)))[:15])
                except Exception as e:  # noqa: BLE001
                    print("  skrypt błąd:", e)
            for probe in ("/api/auctions?pagination[pageSize]=1", "/api/items?pagination[pageSize]=1", "/api/announcements?pagination[pageSize]=1",
                          "/api/obwieszczenia?pagination[pageSize]=1", "/api/licytacje?pagination[pageSize]=1"):
                try:
                    r2 = c.get("https://elicytacje-strapi.komornik.pl" + probe)
                    print(f"  probe {probe}: {r2.status_code} {r2.text[:120]!r}")
                except Exception as e:  # noqa: BLE001
                    print("  probe błąd:", probe, e)
        except Exception as e:  # noqa: BLE001
            print("błąd:", e)
