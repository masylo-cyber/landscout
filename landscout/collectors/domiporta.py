"""Domiporta – karty .sneakpeak na liście wyników (HTML renderowany serwerowo)."""
from __future__ import annotations

import re
from typing import Iterable

from bs4 import BeautifulSoup

from .base import Collector, RawOffer, area_from_text, client, get, log, to_float

LOCATIONS = ["mazowieckie/piaseczynski", "mazowieckie/pruszkowski", "mazowieckie/grodziski", "mazowieckie/warszawski-zachodni",
             "mazowieckie/grojecki", "mazowieckie/otwocki", "mazowieckie/minski", "mazowieckie/warszawa"]
URL = "https://www.domiporta.pl/dzialke/sprzedam/{loc}?Surface.From=600&SortingOrder=InsertionDate&PageNumber={page}"


class DomiportaCollector(Collector):
    name = "domiporta"

    def __init__(self, pages: int = 3):
        self.pages = pages

    def collect(self) -> Iterable[RawOffer]:
        with client() as c:
            for loc in LOCATIONS:
                for page in range(1, self.pages + 1):
                    try:
                        soup = BeautifulSoup(get(c, URL.format(loc=loc, page=page)).text, "lxml")
                    except Exception as e:  # noqa: BLE001
                        log.warning("domiporta %s p%s: %s", loc, page, e)
                        break
                    cards = [a for a in soup.select("article.sneakpeak[data-detail-id]")]
                    if not cards:
                        break
                    n = 0
                    for card in cards:
                        o = self._parse(card)
                        if o:
                            n += 1
                            yield o
                    if len(cards) < 30:
                        break

    @staticmethod
    def _parse(card) -> RawOffer | None:
        ext = card.get("data-detail-id")
        a = card.select_one("a.sneakpeak__title")
        if not ext or not a or not a.get("href"):
            return None
        href = a["href"]
        if "/nowe/inwestycja/" in href:   # reklamy inwestycji deweloperskich
            return None
        url = href if href.startswith("http") else "https://www.domiporta.pl" + href
        title = a.get_text(" ", strip=True)
        area_el = card.select_one(".sneakpeak__details_item--area")
        area = area_from_text(area_el.get_text(" ", strip=True) + " m2") if area_el else None
        price_el = card.select_one(".sneakpeak__price_value")
        price = to_float(price_el.get_text(" ", strip=True)) if price_el else None
        if price and price < 1000:   # "zapytaj o cenę" itp.
            price = None
        loc_el = card.select_one(".sneakpeak__title--inblock")
        desc_el = card.select_one(".sneakpeak__description")
        img = card.select_one("img.sneakpeak__picture_cover")
        imgs = [img.get("data-src") or img.get("src")] if img else []
        return RawOffer(source="domiporta", external_id=str(ext), url=url, title=title,
                        description=desc_el.get_text(" ", strip=True) if desc_el else "",
                        price_pln=price, area_m2=area or area_from_text(title),
                        location_text=loc_el.get_text(" ", strip=True) if loc_el else "",
                        images=[i for i in imgs if i], raw={})
