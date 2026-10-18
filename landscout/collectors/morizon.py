"""Morizon – lista wyników osadza JSON-LD (schema.org Offer) z pełnymi danymi ogłoszeń."""
from __future__ import annotations

import json
import re
from typing import Iterable

from .base import Collector, RawOffer, area_from_text, client, get, log, to_float

# powiaty / miasta (slug morizon)
LOCATIONS = ["piaseczynski", "pruszkowski", "grodziski", "warszawski-zachodni", "grojecki", "otwocki", "minski",
             "legionowski", "warszawa"]
URL = "https://www.morizon.pl/dzialki/najnowsze/{loc}/?page={page}"
LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def _offers(html: str) -> list[dict]:
    out: list[dict] = []
    for m in LD_RE.finditer(html):
        try:
            data = json.loads(m.group(1))
        except Exception:  # noqa: BLE001
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if node.get("@type") == "Offer" and node.get("url"):
                    out.append(node)
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
            elif isinstance(node, list):
                stack.extend(node)
    return out


class MorizonCollector(Collector):
    name = "morizon"

    def __init__(self, pages: int = 3):
        self.pages = pages

    def collect(self) -> Iterable[RawOffer]:
        with client() as c:
            for loc in LOCATIONS:
                for page in range(1, self.pages + 1):
                    try:
                        offers = _offers(get(c, URL.format(loc=loc, page=page)).text)
                    except Exception as e:  # noqa: BLE001
                        log.warning("morizon %s p%s: %s", loc, page, e)
                        break
                    if not offers:
                        break
                    for o in offers:
                        yield self._to_offer(o)
                    if len(offers) < 30:
                        break

    @staticmethod
    def _to_offer(o: dict) -> RawOffer:
        url = o["url"]
        ext = url.rstrip("/").split("-")[-1] or url
        item = o.get("itemOffered") or {}
        addr = (item.get("address") or {})
        loc = ", ".join(x for x in [addr.get("streetAddress"), addr.get("addressLocality"), addr.get("addressRegion")] if x)
        fs = item.get("floorSize") or {}
        area = to_float(fs.get("value")) or area_from_text(o.get("name", ""))
        geo = item.get("geo") or {}
        img = o.get("image")
        imgs = [img] if isinstance(img, str) else [i for i in (img or []) if isinstance(i, str)]
        return RawOffer(source="morizon", external_id=str(ext)[:120], url=url, title=o.get("name", ""),
                        description=item.get("description") or "", price_pln=to_float(o.get("price")), area_m2=area,
                        location_text=loc, lat=to_float(geo.get("latitude")), lon=to_float(geo.get("longitude")),
                        images=imgs[:8], raw={"ld": True})
