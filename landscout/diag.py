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

        print("\n== OTODOM ==")
        url = OTODOM_URL.format(estate="dzialka", loc="mazowieckie/piaseczynski", extra="areaMin=600", page=1)
        try:
            r = c.get(url)
            html = r.text
            items = _items(html)
            print(f"status={r.status_code} len={len(html)} final_url={r.url}")
            print(f"__NEXT_DATA__: {'tak' if '__NEXT_DATA__' in html else 'nie'}; items={len(items)}")
            if "__NEXT_DATA__" in html and not items:
                m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
                js = m.group(1) if m else ""
                print("klucze w pageProps:", re.findall(r'"(\w+)":', js[js.find('"pageProps"'):js.find('"pageProps"') + 400])[:15])
            if not items:
                print("tytuł strony:", re.search(r"<title>(.*?)</title>", html, re.S).group(1)[:120] if "<title>" in html else "-")
                print("fragment:", html[:300].replace("\n", " "))
        except Exception as e:  # noqa: BLE001
            print("błąd:", e)

        print("\n== KOMORNIK ==")
        try:
            r = c.get("https://elicytacje.komornik.pl/items?category=nieruchomosci&province=mazowieckie&page=0")
            print(f"status={r.status_code} len={len(r.text)} final_url={r.url}")
            print("fragment:", r.text[:300].replace("\n", " "))
        except Exception as e:  # noqa: BLE001
            print("błąd:", e)
