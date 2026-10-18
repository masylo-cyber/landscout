"""Otodom – wyniki wyszukiwania osadzają JSON w <script id="__NEXT_DATA__">."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Iterable

from .base import Collector, RawOffer, area_from_text, client, get, log, to_float

# Lokalizacje na poziomie powiatu / dzielnicy – pokrywają wszystkie gminy z areas.yml
LOCATIONS = [
    "mazowieckie/piaseczynski",
    "mazowieckie/pruszkowski",
    "mazowieckie/grodziski",
    "mazowieckie/warszawski-zachodni",
    "mazowieckie/grojecki",
    "mazowieckie/otwocki",
    "mazowieckie/warszawa/warszawa/warszawa/wawer",
    "mazowieckie/warszawa/warszawa/warszawa/wilanow",
    "mazowieckie/warszawa/warszawa/warszawa/ursynow",
    "mazowieckie/warszawa/warszawa/warszawa/mokotow",
    "mazowieckie/warszawa/warszawa/warszawa/bielany",
    "mazowieckie/warszawa/warszawa/warszawa/bialoleka",
]

SEARCHES = [
    # (typ, dodatkowe parametry)
    ("dzialka", "areaMin=600"),
    ("dom", "description=rozbi%C3%B3rki"),      # domy "do rozbiórki"
    ("dom", "description=wyburzenia"),
]
URL = "https://www.otodom.pl/pl/wyniki/sprzedaz/{estate}/{loc}?{extra}&by=LATEST&direction=DESC&limit=72&page={page}"
NEXT_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def _find_items(node, depth=0) -> list[dict]:
    """Szuka w JSON listy ogłoszeń: lista słowników z polami id + (title lub slug) + area/price."""
    if depth > 12:
        return []
    if isinstance(node, dict):
        sa = node.get("searchAds")
        if isinstance(sa, dict) and isinstance(sa.get("items"), list):
            return sa["items"]
        for v in node.values():
            r = _find_items(v, depth + 1)
            if r:
                return r
    elif isinstance(node, list) and node and all(isinstance(x, dict) for x in node[:3]):
        x = node[0]
        if "id" in x and ("slug" in x or "title" in x) and ("areaInSquareMeters" in x or "totalPrice" in x or "estate" in x):
            return node
        for v in node[:3]:
            r = _find_items(v, depth + 1)
            if r:
                return r
    return []


def _items(html: str) -> list[dict]:
    m = NEXT_RE.search(html)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:  # noqa: BLE001
        return []
    try:
        return data["props"]["pageProps"]["data"]["searchAds"]["items"]
    except Exception:  # noqa: BLE001
        return _find_items(data)


class OtodomCollector(Collector):
    name = "otodom"

    def __init__(self, pages: int = 2):
        self.pages = pages

    def collect(self) -> Iterable[RawOffer]:
        with client() as c:
            for loc in LOCATIONS:
                for estate, extra in SEARCHES:
                    for page in range(1, self.pages + 1):
                        url = URL.format(estate=estate, loc=loc, extra=extra, page=page)
                        try:
                            items = _items(get(c, url).text)
                        except Exception as e:  # noqa: BLE001
                            log.warning("otodom %s/%s p%s: %s", loc, estate, page, e)
                            break
                        if not items:
                            break
                        for it in items:
                            yield self._to_offer(it, estate)
                        if len(items) < 72:
                            break

    @staticmethod
    def _to_offer(it: dict, estate: str) -> RawOffer:
        loc = it.get("location") or {}
        addr = loc.get("address") or {}
        parts = []
        for k in ("street", "city", "county", "province"):
            v = addr.get(k)
            if isinstance(v, dict):
                v = v.get("name")
            if v:
                parts.append(str(v))
        if not parts:
            rg = (loc.get("reverseGeocoding") or {}).get("locations") or []
            parts = [x.get("fullName") for x in rg if x.get("fullName")][-1:]
        coords = loc.get("coordinates") or {}
        price = None
        tp = it.get("totalPrice")
        if isinstance(tp, dict):
            price = to_float(tp.get("value"))
        area = to_float(it.get("areaInSquareMeters") or it.get("terrainAreaInSquareMeters"))
        title = it.get("title", "")
        desc = it.get("shortDescription") or ""
        if area is None:
            area = area_from_text(f"{title} {desc}")
        pub = None
        if it.get("dateCreated"):
            try:
                pub = datetime.fromisoformat(str(it["dateCreated"]).replace("Z", "+00:00"))
            except ValueError:
                pass
        slug = it.get("slug") or ""
        return RawOffer(
            source="otodom",
            external_id=str(it.get("id")),
            url=f"https://www.otodom.pl/pl/oferta/{slug}" if slug else f"https://www.otodom.pl/pl/oferta/{it.get('id')}",
            title=title,
            description=desc,
            price_pln=price,
            area_m2=area,
            location_text=", ".join(parts),
            lat=coords.get("latitude"), lon=coords.get("longitude"),
            images=[(im.get("large") or im.get("medium") or "") for im in (it.get("images") or []) if isinstance(im, dict)][:8],
            published_at=pub,
            raw={"estate": estate, "otodom": {k: it.get(k) for k in ("estate", "transaction", "agency", "isPrivateOwner")}},
        )
