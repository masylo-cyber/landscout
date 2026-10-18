from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Iterable

import httpx
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger("landscout.collect")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")


class RawOffer(BaseModel):
    source: str
    external_id: str
    url: str
    title: str = ""
    description: str = ""
    price_pln: float | None = None
    area_m2: float | None = None
    location_text: str = ""
    lat: float | None = None
    lon: float | None = None
    images: list[str] = []
    published_at: datetime | None = None
    raw: dict = {}


class Collector:
    name = "base"

    def collect(self) -> Iterable[RawOffer]:  # pragma: no cover
        raise NotImplementedError


def client(**kw) -> httpx.Client:
    return httpx.Client(headers={"User-Agent": UA, "Accept-Language": "pl-PL,pl;q=0.9"},
                        timeout=30, follow_redirects=True, **kw)


def apify_proxy_url(residential: bool = True) -> str | None:
    """Apify Proxy – do portali blokujących IP serwerowe (wymaga APIFY_TOKEN)."""
    import os
    # Apify Proxy używa osobnego hasła (Console -> Proxy -> Password), nie tokena API
    pwd = os.environ.get("APIFY_PROXY_PASSWORD", "")
    if not pwd:
        return None
    user = "groups-RESIDENTIAL,country-PL" if residential else "auto"
    return f"http://{user}:{pwd}@proxy.apify.com:8000"


def proxied_client(residential: bool = True, **kw) -> httpx.Client:
    proxy = apify_proxy_url(residential)
    return httpx.Client(headers={"User-Agent": UA, "Accept-Language": "pl-PL,pl;q=0.9"}, timeout=45,
                        follow_redirects=True, proxy=proxy, **kw)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=20), reraise=True)
def get(c: httpx.Client, url: str, **kw) -> httpx.Response:
    r = c.get(url, **kw)
    r.raise_for_status()
    return r


_num = re.compile(r"[^\d,.\-]")


def to_float(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = _num.sub("", str(v)).replace(",", ".")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def area_from_text(text: str) -> float | None:
    """Wyłuskuje powierzchnię działki z tekstu: '1200 m2', '0,45 ha', '12 arów'."""
    t = text.lower().replace("\xa0", " ")
    m = re.search(r"(\d[\d\s]{0,6}(?:[.,]\d+)?)\s*(m2|m²|mkw|m kw)", t)
    if m:
        return to_float(m.group(1).replace(" ", ""))
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*ha\b", t)
    if m:
        v = to_float(m.group(1))
        return v * 10000 if v else None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*ar(?:ów|y|a)?\b", t)
    if m:
        v = to_float(m.group(1))
        return v * 100 if v else None
    return None


def all_collectors(only: str | None = None) -> list[Collector]:
    from .olx import OlxCollector
    from .otodom import OtodomCollector
    from .apify_generic import apify_collectors
    from .komornik import KomornikCollector
    from .morizon import MorizonCollector
    from .domiporta import DomiportaCollector
    from .adresowo import AdresowoCollector

    cols: list[Collector] = [OtodomCollector(), MorizonCollector(), DomiportaCollector(), AdresowoCollector(),
                             OlxCollector(), KomornikCollector(), *apify_collectors()]
    if only:
        cols = [c for c in cols if c.name == only]
    return cols
