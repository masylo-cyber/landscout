"""Licytacje komornicze nieruchomości (elicytacje.komornik.pl) – województwo mazowieckie.

Portal renderuje listę po stronie klienta; korzystamy z jego publicznego API JSON,
a gdy zmieni format – próbujemy wyłuskać karty z HTML. Kolektor best-effort: błędy
tylko logujemy, nie przerywają przebiegu.
"""
from __future__ import annotations

import re
from typing import Iterable

from bs4 import BeautifulSoup

from .base import Collector, RawOffer, area_from_text, client, get, log, to_float

API = ("https://elicytacje.komornik.pl/api/items?category=nieruchomosci&province=mazowieckie"
       "&itemType=grunt&page={page}&size=50")
HTML = "https://elicytacje.komornik.pl/items?category=nieruchomosci&province=mazowieckie&page={page}"
TOWNS = re.compile(r"(piaseczno|lesznowola|magdalenka|konstancin|wilan[óo]w|tarczyn|nadarzyn|brwin[óo]w|"
                   r"piast[óo]w|b[łl]onie|g[óo]ra kalwaria|pra[żz]m[óo]w|gr[óo]jec|micha[łl]owice|raszyn|"
                   r"pruszk[óo]w|grodzisk|o[żz]ar[óo]w|leszno|wawer|ursyn[óo]w|mokot[óo]w|bielany|"
                   r"j[óo]zef[óo]w|komor[óo]w|podkowa|z[łl]otok[łl]os|krakowiany|rusiec|[żz][óo]lwin|[łl]azy)", re.I)


class KomornikCollector(Collector):
    name = "komornik"

    def collect(self) -> Iterable[RawOffer]:
        with client() as c:
            for page in range(0, 6):
                items = self._api(c, page) or self._html(c, page)
                if not items:
                    break
                for it in items:
                    text = f"{it.get('title','')} {it.get('location','')} {it.get('description','')}"
                    if not TOWNS.search(text):
                        continue
                    yield RawOffer(
                        source="komornik",
                        external_id=str(it["id"]),
                        url=it["url"],
                        title=it.get("title", ""),
                        description=it.get("description", ""),
                        price_pln=to_float(it.get("price")),
                        area_m2=area_from_text(text),
                        location_text=it.get("location", ""),
                        images=it.get("images", [])[:8],
                        raw={"auction_date": it.get("date"), "type": "licytacja"},
                    )

    @staticmethod
    def _api(c, page: int) -> list[dict]:
        try:
            r = get(c, API.format(page=page), headers={"Accept": "application/json"})
            data = r.json()
        except Exception as e:  # noqa: BLE001
            log.debug("komornik api p%s: %s", page, e)
            return []
        rows = data.get("content") or data.get("items") or (data if isinstance(data, list) else [])
        out = []
        for it in rows:
            if not isinstance(it, dict) or not it.get("id"):
                continue
            out.append({
                "id": it["id"],
                "url": f"https://elicytacje.komornik.pl/items/{it['id']}",
                "title": it.get("title") or it.get("name") or "",
                "description": it.get("description") or "",
                "price": it.get("startingPrice") or it.get("price") or it.get("estimatedValue"),
                "location": ", ".join(str(it.get(k)) for k in ("city", "street", "district") if it.get(k)),
                "images": [im.get("url") if isinstance(im, dict) else im for im in (it.get("images") or [])],
                "date": it.get("auctionDate") or it.get("startDate"),
            })
        return out

    @staticmethod
    def _html(c, page: int) -> list[dict]:
        try:
            soup = BeautifulSoup(get(c, HTML.format(page=page)).text, "lxml")
        except Exception as e:  # noqa: BLE001
            log.debug("komornik html p%s: %s", page, e)
            return []
        out = []
        for a in soup.select("a[href*='/items/']"):
            href = a.get("href", "")
            m = re.search(r"/items/(\d+)", href)
            if not m:
                continue
            card = a.find_parent(["article", "div"]) or a
            text = card.get_text(" ", strip=True)
            price = None
            pm = re.search(r"([\d\s]{4,})\s*zł", text)
            if pm:
                price = to_float(pm.group(1).replace(" ", ""))
            out.append({"id": m.group(1), "url": "https://elicytacje.komornik.pl" + href if href.startswith("/") else href,
                        "title": a.get_text(" ", strip=True)[:200], "description": text[:2000],
                        "price": price, "location": "", "images": [], "date": None})
        # deduplikacja po id
        seen, uniq = set(), []
        for it in out:
            if it["id"] not in seen:
                seen.add(it["id"])
                uniq.append(it)
        return uniq
