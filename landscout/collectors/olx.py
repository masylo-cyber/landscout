"""OLX – kategoria 'Działki > Sprzedaż' per miejscowość/gmina.

Strona wyników osadza pełny stan w window.__PRERENDERED_STATE__ (JSON zakodowany jako string),
z którego czytamy listę ogłoszeń. To stabilniejsze niż scraping HTML-a kart.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Iterable

from ..config import olx_regions
from .base import Collector, RawOffer, area_from_text, client, get, log, to_float

BASE = "https://www.olx.pl/nieruchomosci/dzialki/sprzedaz/{region}/"
PARAMS = "?search%5Bfilter_float_m%3Afrom%5D=600&search%5Border%5D=created_at%3Adesc&page={page}"
STATE_RE = re.compile(r'window\.__PRERENDERED_STATE__\s*=\s*"(.*?)";\s*\n', re.S)


def _state(html: str) -> dict | None:
    m = STATE_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(json.loads('"' + m.group(1) + '"'))
    except Exception:  # noqa: BLE001
        return None


def _ads(state: dict) -> list[dict]:
    try:
        return state["listing"]["listing"]["ads"]
    except Exception:  # noqa: BLE001
        return []


def _param(ad: dict, key: str):
    for p in ad.get("params", []):
        if p.get("key") == key:
            v = p.get("value")
            if isinstance(v, dict):
                return v.get("key") or v.get("label") or v.get("value")
            return v
    return None


class OlxCollector(Collector):
    name = "olx"

    def __init__(self, regions: list[str] | None = None, pages: int = 3):
        self.regions = regions or olx_regions()
        self.pages = pages

    def collect(self) -> Iterable[RawOffer]:
        with client() as c:
            for region in self.regions:
                for page in range(1, self.pages + 1):
                    url = BASE.format(region=region) + PARAMS.format(page=page)
                    try:
                        html = get(c, url).text
                    except Exception as e:  # noqa: BLE001
                        log.warning("olx %s p%s: %s", region, page, e)
                        break
                    st = _state(html)
                    ads = _ads(st) if st else []
                    if not ads:
                        break
                    for ad in ads:
                        yield self._to_offer(ad, region)
                    if len(ads) < 30:
                        break

    @staticmethod
    def _to_offer(ad: dict, region: str) -> RawOffer:
        loc = ad.get("location", {}) or {}
        loc_text = ", ".join(x for x in [loc.get("cityName"), loc.get("districtName"), loc.get("regionName")] if x)
        area = to_float(_param(ad, "m"))
        price = None
        pr = ad.get("price") or {}
        if isinstance(pr, dict):
            price = to_float(pr.get("regularPrice", {}).get("value") if isinstance(pr.get("regularPrice"), dict) else pr.get("value"))
        if price is None:
            price = to_float(_param(ad, "price"))
        desc = ad.get("description") or ""
        if area is None:
            area = area_from_text(f"{ad.get('title','')} {desc}")
        pub = None
        for k in ("createdTime", "lastRefreshTime"):
            if ad.get(k):
                try:
                    pub = datetime.fromisoformat(str(ad[k]).replace("Z", "+00:00"))
                    break
                except ValueError:
                    pass
        m = ad.get("map") or {}
        return RawOffer(
            source="olx",
            external_id=str(ad.get("id")),
            url=ad.get("url") or f"https://www.olx.pl/d/oferta/{ad.get('id')}",
            title=ad.get("title", ""),
            description=desc,
            price_pln=price,
            area_m2=area,
            location_text=loc_text,
            lat=m.get("lat"), lon=m.get("lon"),
            images=[p.get("link", "").replace("{width}", "1200").replace("{height}", "900")
                    for p in (ad.get("photos") or []) if isinstance(p, dict)][:8],
            published_at=pub,
            raw={"region": region, "params": {p.get("key"): p.get("value") for p in ad.get("params", []) if isinstance(p, dict)}},
        )
