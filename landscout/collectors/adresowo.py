"""Adresowo – oferty bez pośredników; lista HTML z linkami /o/<slug> i opisem; szczegóły (cena, m²) ze strony oferty (JSON-LD)."""
from __future__ import annotations

import json
import re
from typing import Iterable

from bs4 import BeautifulSoup

from .base import Collector, RawOffer, area_from_text, client, get, log, to_float

LOCATIONS = ["piaseczno", "lesznowola", "konstancin-jeziorna", "gora-kalwaria", "prazmow", "tarczyn", "nadarzyn", "raszyn",
             "michalowice", "pruszkow", "piastow", "brwinow", "grodzisk-mazowiecki", "ozarow-mazowiecki", "blonie", "leszno",
             "jozefow", "otwock", "wiazowna", "karczew", "celestynow", "halinow", "sulejowek", "warszawa"]
URL = "https://adresowo.pl/dzialki/{loc}/?page={page}"
LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


class AdresowoCollector(Collector):
    name = "adresowo"

    def __init__(self, pages: int = 2, max_details: int = 120):
        self.pages = pages
        self.max_details = max_details

    def collect(self) -> Iterable[RawOffer]:
        seen: set[str] = set()
        details = 0
        with client() as c:
            for loc in LOCATIONS:
                for page in range(1, self.pages + 1):
                    try:
                        soup = BeautifulSoup(get(c, URL.format(loc=loc, page=page)).text, "lxml")
                    except Exception as e:  # noqa: BLE001
                        log.warning("adresowo %s p%s: %s", loc, page, e)
                        break
                    links = [a for a in soup.select('a[href^="/o/"]') if a.get("data-track") == "offer-link" or a.select_one("span")]
                    if not links:
                        break
                    for a in links:
                        href = a["href"].split("?")[0]
                        slug = href.rsplit("/", 1)[-1]
                        if slug in seen:
                            continue
                        seen.add(slug)
                        spans = [s.get_text(" ", strip=True) for s in a.select("span") if s.get_text(strip=True)]
                        title = " · ".join(spans[:2]) or a.get_text(" ", strip=True)
                        box = a.find_parent("div")
                        desc_el = box.find_next("p") if box else None
                        desc = desc_el.get_text(" ", strip=True) if desc_el else ""
                        url = "https://adresowo.pl" + href
                        price = area = None
                        imgs: list[str] = []
                        if details < self.max_details:
                            details += 1
                            price, area, imgs, full_desc, loc_txt = self._details(c, url)
                            if full_desc:
                                desc = full_desc
                        yield RawOffer(source="adresowo", external_id=slug, url=url, title=title[:300], description=desc,
                                       price_pln=price, area_m2=area or area_from_text(f"{title} {desc}"),
                                       location_text=(loc_txt if details and loc_txt else " ".join(spans[:2]) or loc),
                                       images=imgs, raw={"bez_posrednikow": True})
                    if len(links) < 20:
                        break

    @staticmethod
    def _details(c, url: str):
        try:
            html = get(c, url).text
        except Exception:  # noqa: BLE001
            return None, None, [], "", ""
        price = area = None
        imgs: list[str] = []
        desc = loc = ""
        for m in LD_RE.finditer(html):
            try:
                d = json.loads(m.group(1))
            except Exception:  # noqa: BLE001
                continue
            nodes = d if isinstance(d, list) else [d]
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                off = n.get("offers") if isinstance(n.get("offers"), dict) else n
                price = price or to_float(off.get("price"))
                if n.get("image"):
                    im = n["image"]
                    imgs = [im] if isinstance(im, str) else [i for i in im if isinstance(i, str)]
                desc = desc or (n.get("description") or "")
                fs = n.get("floorSize") or {}
                area = area or to_float(fs.get("value"))
                ad = n.get("address") or {}
                if isinstance(ad, dict) and ad.get("addressLocality"):
                    loc = ", ".join(x for x in [ad.get("streetAddress"), ad.get("addressLocality")] if x)
        if price is None:
            m = re.search(r"([\d\s]{5,})\s*zł", html)
            price = to_float(m.group(1).replace(" ", "")) if m else None
        if area is None:
            area = area_from_text(html[:20000])
        return price, area, imgs[:8], desc[:6000], loc
