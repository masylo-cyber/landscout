"""Diagnostyka źródeł: co zwracają portale (status, rozmiar, czy są znaczniki JSON)."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

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
            print("OLX przez Apify Proxy: brak APIFY_PROXY_PASSWORD w .env (Console -> Proxy -> Password)")

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

        print("\n== MPZP (KIMPZP) – test Magdalenka 52.065,20.962 ==")
        from .enrich import KIMPZP, _mpzp_requests, mpzp_by_xy, parcel_by_xy
        try:
            r = c.get(KIMPZP, params={"SERVICE": "WMS", "REQUEST": "GetCapabilities", "VERSION": "1.3.0"})
            names = re.findall(r"<Name>([^<]+)</Name>", r.text)
            print(f"GetCapabilities: status={r.status_code} len={len(r.text)} warstwy={names[:15]}")
        except Exception as e:  # noqa: BLE001
            print("GetCapabilities błąd:", e)
        for i, params in enumerate(_mpzp_requests(52.065, 20.962)):
            try:
                r = c.get(KIMPZP, params=params)
                txt = re.sub(r"<[^>]+>", " ", r.text)
                txt = re.sub(r"\s+", " ", txt).strip()
                print(f"wariant {i} ({params.get('VERSION')}, {params['LAYERS'][:20]}): status={r.status_code} len={len(r.text)} tekst={txt[:160]!r}")
            except Exception as e:  # noqa: BLE001
                print(f"wariant {i} błąd:", e)
        for name, la, lo in (("Magdalenka centrum", 52.0597, 20.9572), ("Łazy", 52.1015, 20.9915), ("Konstancin", 52.0818, 21.1179),
                             ("Wilanów Zawady", 52.1655, 21.1130), ("Złotokłos", 52.0010, 20.9470)):
            mp = mpzp_by_xy(la, lo)
            print(f"mpzp {name}: has_plan={mp.get('has_plan')} gmina={mp.get('gmina')} symbols={mp.get('symbols')} fields={list((mp.get('fields') or {}).items())[:4]}")

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
                    calls = re.findall(r'strapiFetch\)?\(\s*[`"\']([^`"\']{1,80})', js)
                    print("  strapiFetch(...):", sorted(set(calls))[:40])
                    print("  stringi z '?populate'/'filters':", sorted(set(re.findall(r'[`"\']([^`"\']{0,60}(?:populate|filters|pagination)[^`"\']{0,60})', js)))[:20])
                    print("  ścieżki /xxx w pobliżu 'strapi':", sorted(set(re.findall(r'strapi[^;]{0,200}?["`\'](/[a-z][a-z0-9_\-/]{2,40})', js)))[:30])
                except Exception as e:  # noqa: BLE001
                    print("  skrypt błąd:", e)
            ids = sorted({int(m) for m in re.findall(r'href="/licytacje/(\d+)/', html)})
            print("ID licytacji na stronie głównej:", ids[:5], "...", ids[-5:])
            if ids:
                r2 = c.get(f"https://licytacje.komornik.pl/licytacje/{ids[-1]}/x")
                soup = BeautifulSoup(r2.text, "lxml")
                for t in soup(["script", "style", "noscript", "svg"]):
                    t.decompose()
                txt = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
                print(f"strona {ids[-1]}: status={r2.status_code} len_html={len(r2.text)} len_txt={len(txt)}")
                print("TEKST:", txt[:2500])
                print("H1/H2:", [h.get_text(' ', strip=True)[:80] for h in soup.find_all(["h1", "h2", "h3"])][:15])
                print("meta:", {m.get("property") or m.get("name"): (m.get("content") or "")[:120] for m in soup.find_all("meta") if (m.get("property") or m.get("name") or "").startswith(("og:", "description"))})
                r3 = c.get(f"https://licytacje.komornik.pl/licytacje/{ids[-1] + 50}/x")
                print(f"strona {ids[-1] + 50} (poza zakresem?): status={r3.status_code} len={len(r3.text)}")
        except Exception as e:  # noqa: BLE001
            print("błąd:", e)
