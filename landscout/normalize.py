from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .collectors.base import RawOffer
from .db import Listing

TEARDOWN = re.compile(r"do (rozbi[óo]rki|wyburzenia)|rozbi[óo]rk|wyburz|do kapitalnego remontu|ruina|"
                      r"stan surowy otwarty bez|wartość stanowi (głównie )?dzia", re.I)
HOUSE = re.compile(r"\bdom\b|\bdomu\b|budynek mieszkalny|segment|bli[źz]niak|szeregow", re.I)
PLOT = re.compile(r"dzia[łl]k|grunt|teren|parcel", re.I)


def classify(title: str, desc: str, raw: dict) -> str:
    t = f"{title} {desc}"
    est = (raw.get("estate") or "").lower()
    if TEARDOWN.search(t):
        return "dom_rozbiorka"
    if est == "dzialka" or PLOT.search(title):
        return "dzialka"
    if est == "dom" or HOUSE.search(title):
        return "dom"
    return "dzialka" if PLOT.search(t) else "inne"


def cluster_key(o: RawOffer) -> str | None:
    """Klucz do łączenia tej samej działki z różnych portali: zaokrąglone współrzędne + powierzchnia."""
    if o.lat and o.lon and o.area_m2:
        return f"{round(o.lat, 3)}|{round(o.lon, 3)}|{int(round(o.area_m2 / 50.0))}"
    if o.area_m2 and o.location_text:
        loc = re.sub(r"[^a-ząćęłńóśźż]", "", o.location_text.lower())[:24]
        return f"{loc}|{int(round(o.area_m2 / 25.0))}"
    return None


def upsert(s: Session, o: RawOffer) -> tuple[Listing, bool]:
    """Zapisuje ofertę; zwraca (rekord, czy_nowa)."""
    now = datetime.now(timezone.utc)
    row = s.scalar(select(Listing).where(Listing.source == o.source, Listing.external_id == o.external_id))
    if row is None:
        row = Listing(
            source=o.source, external_id=o.external_id, url=o.url, title=o.title[:500],
            description=o.description, price_pln=o.price_pln, area_m2=o.area_m2,
            location_text=o.location_text[:500], lat=o.lat, lon=o.lon, images=o.images,
            raw=o.raw, kind=classify(o.title, o.description, o.raw), published_at=o.published_at,
            price_history=[{"t": now.isoformat(), "p": o.price_pln}] if o.price_pln else [],
            cluster_key=cluster_key(o),
        )
        s.add(row)
        s.flush()
        return row, True

    row.last_seen = now
    row.active = True
    if o.price_pln and o.price_pln != row.price_pln:
        hist = list(row.price_history or [])
        hist.append({"t": now.isoformat(), "p": o.price_pln})
        row.price_history = hist
        row.price_pln = o.price_pln
    if o.description and len(o.description) > len(row.description or ""):
        row.description = o.description
        row.kind = classify(row.title, row.description, row.raw)
    if o.area_m2 and not row.area_m2:
        row.area_m2 = o.area_m2
    if o.lat and not row.lat:
        row.lat, row.lon = o.lat, o.lon
    if not row.cluster_key:
        row.cluster_key = cluster_key(o)
    return row, False
