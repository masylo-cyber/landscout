"""Analiza pojedynczej oferty z linku (dowolny portal): pobranie strony -> zapis -> wzbogacenie -> LLM -> scoring -> radar rynku."""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from sqlalchemy import select

from .collectors.base import RawOffer, area_from_text, client, get, to_float
from .db import Listing, session

log = logging.getLogger("landscout.adhoc")
LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def _ld(html: str) -> dict:
    """Najbogatszy obiekt JSON-LD (Offer/Product/RealEstateListing)."""
    best: dict = {}
    for m in LD_RE.finditer(html):
        try:
            data = json.loads(m.group(1))
        except Exception:  # noqa: BLE001
            continue
        stack = [data]
        while stack:
            n = stack.pop()
            if isinstance(n, dict):
                if any(k in n for k in ("offers", "price", "description", "address", "geo")) and len(json.dumps(n)) > len(json.dumps(best)):
                    best = n
                stack.extend(v for v in n.values() if isinstance(v, (dict, list)))
            elif isinstance(n, list):
                stack.extend(n)
    return best


def fetch_offer(url: str) -> RawOffer:
    host = urlparse(url).netloc.replace("www.", "")
    source = host.split(".")[0]
    ext = re.search(r"(\d{6,})", url.rstrip("/").rsplit("/", 1)[-1]) or re.search(r"(\d{6,})", url)
    ext_id = ext.group(1) if ext else url.rstrip("/").rsplit("/", 1)[-1][:120]
    with client() as c:
        html = get(c, url).text
    soup = BeautifulSoup(html, "lxml")
    ld = _ld(html)
    title = (soup.find("meta", property="og:title") or {}).get("content") or (soup.title.get_text(strip=True) if soup.title else "")
    meta_desc = (soup.find("meta", attrs={"name": "description"}) or {}).get("content") or ""
    for t in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "form"]):
        t.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    desc = ld.get("description") or ""
    if len(desc) < 300:
        # opis = fragment strony od tytułu w dół (bez nawigacji); LLM radzi sobie z szumem
        i = text.find(title[:30]) if title else -1
        desc = (desc + "\n" + meta_desc + "\n" + text[max(i, 0): max(i, 0) + 7000]).strip()
    offers = ld.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = to_float(offers.get("price") or ld.get("price"))
    if not price:
        m = re.search(r"(\d[\d  ]{4,}) ?zł", text)
        price = to_float(m.group(1)) if m else None
    area = to_float(((ld.get("floorSize") or {}).get("value")) if isinstance(ld.get("floorSize"), dict) else None) \
        or area_from_text(title) or area_from_text(text[:3000])
    addr = ld.get("address") or {}
    loc = ", ".join(x for x in [addr.get("streetAddress"), addr.get("addressLocality"), addr.get("addressRegion")] if x) if isinstance(addr, dict) else ""
    if not loc:
        m = re.search(r"(?:Warszawa|[A-ZŁŚŻ][a-ząęółśżźćń]+)[^|•]{0,60}?(?:, ?(?:mazowieckie|Warszawa))", title + " " + meta_desc)
        loc = m.group(0) if m else title
    geo = ld.get("geo") or {}
    imgs = [(soup.find("meta", property="og:image") or {}).get("content") or ""]
    return RawOffer(source=source, external_id=str(ext_id), url=url, title=title[:300], description=desc[:12000],
                    price_pln=price, area_m2=area, location_text=loc[:300],
                    lat=to_float(geo.get("latitude")), lon=to_float(geo.get("longitude")),
                    images=[i for i in imgs if i], raw={"adhoc": True})


def analyze_url(url: str, force: bool = False) -> Listing:
    """Pełna analiza jednej oferty; jeśli jest w bazie – dociąga pełny opis i liczy od nowa."""
    from .analyze import analyze
    from .comps import market_stats
    from .enrich import enrich
    from .normalize import upsert
    from .scoring import score
    offer = fetch_offer(url)
    with session() as db:
        row = db.scalar(select(Listing).where(Listing.url == url)) or db.scalar(
            select(Listing).where(Listing.external_id == offer.external_id, Listing.source == offer.source))
        if row is None:
            row, _ = upsert(db, offer)
        else:
            if len(offer.description) > len(row.description or ""):
                row.description = offer.description
            if offer.price_pln and not row.price_pln:
                row.price_pln = offer.price_pln
            if offer.area_m2 and not row.area_m2:
                row.area_m2 = offer.area_m2
            if offer.location_text and "," in offer.location_text and "," not in (row.location_text or ""):
                row.location_text, row.lat, row.lon, row.enriched_at = offer.location_text, None, None, None
        db.commit()
        if force or not row.enriched_at or not row.lat:
            row.enriched_at = None
            enrich(row)
        if force or not row.analysis:
            analyze(row)
        score(row)
        if row.lat and row.lon:
            row.market = market_stats(db, row.lat, row.lon)
            score(row)
        db.commit()
        return row
