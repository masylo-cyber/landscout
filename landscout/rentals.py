"""Wynajem: domy wolnostojące i bliźniaki (bez szeregów), min. 220 m², rok budowy >= 2020.
Źródła: Otodom (filtr areaMin/buildYearMin + weryfikacja na stronie oferty), Morizon, Domiporta (weryfikacja na stronie).
Nowe trafienia idą od razu na czat Telegram „wynajem” (wynajmy znikają szybko – bez paczek co 4 dni)."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from html import escape

from bs4 import BeautifulSoup
from sqlalchemy import select

from .collectors.base import client, get, to_float
from .collectors.otodom import NEXT_RE, _items
from .db import Rental, session

log = logging.getLogger("landscout.rentals")

MIN_AREA = 220
MIN_YEAR = 2020
MAGDALENKA = (52.0597, 20.9572)
MAX_KM = 15            # okolice Magdalenki / gm. Lesznowola – im bliżej, tym wyżej; Warszawa wyłączona
WARSAW = re.compile(r"warszaw|wilanów|ursynów|mokotów|wawer|białołęka|bielany|bemowo|targówek|wesoła|rembertów|ursus|włochy|"
                    r"ochota|(?<![a-ząćęłńóśźż])wola\b|żoliborz|śródmieście|praga|saska kępa|sadyba|powsin|zawady|kabaty|natolin|stegny", re.I)
OTODOM_LOCS = ["mazowieckie/warszawa/warszawa/warszawa", "mazowieckie/piaseczynski", "mazowieckie/pruszkowski",
               "mazowieckie/warszawski-zachodni", "mazowieckie/otwocki", "mazowieckie/grodziski", "mazowieckie/legionowski",
               "mazowieckie/minski", "mazowieckie/wolominski"]
OTODOM_URL = ("https://www.otodom.pl/pl/wyniki/wynajem/dom/{loc}?areaMin={area}&buildYearMin={year}"
              "&by=LATEST&direction=DESC&limit=72&page={page}")
MORIZON_LOCS = ["warszawa", "piaseczynski", "pruszkowski", "warszawski-zachodni", "otwocki", "grodziski", "legionowski"]
MORIZON_URL = "https://www.morizon.pl/do-wynajecia/domy/{loc}/"
DOMIPORTA_LOCS = ["mazowieckie/warszawa", "mazowieckie/piaseczynski", "mazowieckie/pruszkowski", "mazowieckie/warszawski-zachodni",
                  "mazowieckie/otwocki", "mazowieckie/grodziski", "mazowieckie/legionowski"]
DOMIPORTA_URL = "https://www.domiporta.pl/dom/wynajme/{loc}?Surface.From={area}&SortingOrder=InsertionDate&PageNumber={page}"

TYPE_PL = {"detached": "wolnostojący", "semi_detached": "bliźniak", "ribbon": "szeregowiec", "terraced": "szeregowiec"}


def _otodom_list(c) -> list[dict]:
    out = []
    for loc in OTODOM_LOCS:
        for page in (1, 2):
            try:
                items = _items(get(c, OTODOM_URL.format(loc=loc, area=MIN_AREA, year=MIN_YEAR, page=page)).text)
            except Exception as e:  # noqa: BLE001
                log.warning("otodom wynajem %s: %s", loc, e)
                break
            for it in items:
                if it.get("transaction") != "RENT" or it.get("estate") != "HOUSE":
                    continue
                loc_ = it.get("location") or {}
                rg = (loc_.get("reverseGeocoding") or {}).get("locations") or []
                full = max((x.get("fullName") or "" for x in rg), key=len, default="")
                co = loc_.get("coordinates") or {}
                tp = it.get("totalPrice") or {}
                imgs = it.get("images") or []
                out.append(dict(source="otodom", external_id=str(it.get("id")), url=f"https://www.otodom.pl/pl/oferta/{it.get('slug')}",
                                title=it.get("title", ""), price_pln=to_float(tp.get("value")) if isinstance(tp, dict) else None,
                                area_m2=to_float(it.get("areaInSquareMeters")), plot_m2=to_float(it.get("terrainAreaInSquareMeters")),
                                location_text=full, lat=to_float(co.get("latitude")), lon=to_float(co.get("longitude")),
                                image=(imgs[0].get("medium") or imgs[0].get("large")) if imgs and isinstance(imgs[0], dict) else None))
            if len(items) < 72:
                break
    return out


def _morizon_list(c) -> list[dict]:
    from .collectors.morizon import _offers
    out = []
    for loc in MORIZON_LOCS:
        try:
            offers = _offers(get(c, MORIZON_URL.format(loc=loc)).text)
        except Exception as e:  # noqa: BLE001
            log.warning("morizon wynajem %s: %s", loc, e)
            continue
        for o in offers:
            item = o.get("itemOffered") or {}
            area = to_float((item.get("floorSize") or {}).get("value"))
            if area and area < MIN_AREA:
                continue
            addr = item.get("address") or {}
            geo = item.get("geo") or {}
            img = o.get("image")
            out.append(dict(source="morizon", external_id=o["url"].rstrip("/").split("-")[-1][:120], url=o["url"], title=o.get("name", ""),
                            price_pln=to_float(o.get("price")), area_m2=area,
                            location_text=", ".join(x for x in [addr.get("streetAddress"), addr.get("addressLocality")] if x),
                            lat=to_float(geo.get("latitude")), lon=to_float(geo.get("longitude")),
                            image=img if isinstance(img, str) else None))
    return out


def _domiporta_list(c) -> list[dict]:
    from .collectors.domiporta import DomiportaCollector
    out = []
    for loc in DOMIPORTA_LOCS:
        try:
            soup = BeautifulSoup(get(c, DOMIPORTA_URL.format(loc=loc, area=MIN_AREA, page=1)).text, "lxml")
        except Exception as e:  # noqa: BLE001
            log.warning("domiporta wynajem %s: %s", loc, e)
            continue
        for card in soup.select("article.sneakpeak[data-detail-id]"):
            o = DomiportaCollector._parse(card)
            if not o or (o.area_m2 and o.area_m2 < MIN_AREA):
                continue
            out.append(dict(source="domiporta", external_id=o.external_id, url=o.url, title=o.title, price_pln=o.price_pln,
                            area_m2=o.area_m2, location_text=o.location_text, image=(o.images or [None])[0]))
    return out


def _details(c, r: Rental) -> None:
    """Rok budowy, rodzaj zabudowy, metraż, dostępność – ze strony oferty."""
    html = get(c, r.url).text
    if r.source == "otodom":
        m = NEXT_RE.search(html)
        if m:
            ad = (json.loads(m.group(1)).get("props", {}).get("pageProps", {}) or {}).get("ad") or {}
            ch = {x.get("key"): x.get("value") for x in ad.get("characteristics") or []}
            r.build_year = int(ch["build_year"]) if str(ch.get("build_year", "")).isdigit() else r.build_year
            r.building_type = ch.get("building_type") or r.building_type
            r.rooms = int(ch["rooms_num"]) if str(ch.get("rooms_num", "")).isdigit() else r.rooms
            r.free_from = ch.get("free_from") or r.free_from
            if ch.get("m") and not r.area_m2:
                r.area_m2 = to_float(ch["m"])
            if ch.get("terrain_area") and not r.plot_m2:
                r.plot_m2 = to_float(ch["terrain_area"])
            return
    text = re.sub(r"\s+", " ", BeautifulSoup(html, "lxml").get_text(" ", strip=True))
    y = re.search(r"[Rr]ok budowy\D{0,15}((?:19|20)\d\d)", text) or re.search(r"(?:wybudowan\w*|budowy|oddan\w*)\D{0,20}(20[2-3]\d)", text)
    if y:
        r.build_year = int(y.group(1))
    low = text.lower()
    if re.search(r"szeregow|segment środkowy", low):
        r.building_type = "ribbon"
    elif re.search(r"bliźniak|bliźniacz|blizniak", low):
        r.building_type = "semi_detached"
    elif re.search(r"wolnostoj", low):
        r.building_type = "detached"
    if not r.area_m2:
        a = re.search(r"[Pp]owierzchnia\D{0,20}(\d[\d ,.]{1,7})\s*m", text)
        r.area_m2 = to_float(a.group(1)) if a else None


def _km(r: Rental) -> float | None:
    from .comps import _dist_km
    return _dist_km(MAGDALENKA[0], MAGDALENKA[1], r.lat, r.lon) if r.lat and r.lon else None


def _matches(r: Rental) -> bool:
    if not (r.build_year and r.build_year >= MIN_YEAR and (r.area_m2 or 0) >= MIN_AREA
            and (r.building_type or "") not in ("ribbon", "terraced")):
        return False
    if WARSAW.search(f"{r.location_text} {r.title}"):
        return False
    km = _km(r)
    return km is not None and km <= MAX_KM


def card(r: Rental) -> str:
    t = TYPE_PL.get(r.building_type or "", r.building_type or "?")
    price = f"{r.price_pln:,.0f} zł/mies.".replace(",", " ") if r.price_pln else "cena ?"
    km = _km(r)
    parts = [f"<b>🏡 WYNAJEM</b> · {escape((r.location_text or '?')[:60])}" + (f" · <b>{km:.1f} km</b> od Magdalenki" if km is not None else ""),
             f"<b>{price}</b> · {r.area_m2 or 0:.0f} m² · {t} · rok {r.build_year or '?'}"
             + (f" · działka {r.plot_m2:.0f} m²" if r.plot_m2 else "") + (f" · {r.rooms} pok." if r.rooms else ""),
             escape(r.title[:120])]
    if r.free_from:
        parts.append(f"dostępny od {r.free_from}")
    parts.append(f'<a href="{escape(r.url)}">▶ oferta ({r.source})</a>'
                 + (f' · <a href="https://maps.google.com/?q={r.lat},{r.lon}">mapa</a>' if r.lat and r.lon else "") + f" · W{r.id}")
    return "\n".join(parts)


def run_rentals(send: bool = True) -> dict:
    from .notify import send_telegram
    now = datetime.now(timezone.utc)
    st = {"rent_seen": 0, "rent_new": 0, "rent_matches": 0, "rent_sent": 0}
    with client() as c, session() as db:
        found = []
        for fn in (_otodom_list, _morizon_list, _domiporta_list):
            try:
                found += fn(c)
            except Exception as e:  # noqa: BLE001
                log.warning("wynajem %s: %s", fn.__name__, e)
        for d in found:
            st["rent_seen"] += 1
            r = db.scalar(select(Rental).where(Rental.source == d["source"], Rental.external_id == d["external_id"]))
            if r is None:
                r = Rental(**d)
                db.add(r)
                st["rent_new"] += 1
            else:
                for k, v in d.items():
                    if v is not None:
                        setattr(r, k, v)
            r.last_seen, r.active = now, True
        db.commit()
        from .enrich import _geocode_variants, geocode
        for r in db.scalars(select(Rental).where(Rental.active.is_(True), Rental.lat.is_(None))):
            for hint in _geocode_variants(r.location_text or r.title):
                pt = geocode(hint)
                if pt:
                    r.lat, r.lon = pt
                    break
        db.commit()
        for r in db.scalars(select(Rental).where(Rental.active.is_(True), Rental.checked.is_(True))):
            r.matches = _matches(r)
        db.commit()
        for r in db.scalars(select(Rental).where(Rental.active.is_(True), Rental.checked.is_(False))):
            if r.area_m2 and r.area_m2 < MIN_AREA:
                r.checked = True
                continue
            try:
                _details(c, r)
            except Exception as e:  # noqa: BLE001
                log.debug("wynajem szczegóły %s: %s", r.url, e)
                continue
            r.checked = True
            r.matches = _matches(r)
            db.commit()
        for r in db.scalars(select(Rental).where(Rental.active.is_(True), Rental.last_seen < now - timedelta(days=3))):
            r.active = False
        st["rent_matches"] = db.query(Rental).filter(Rental.active.is_(True), Rental.matches.is_(True)).count()
        if send:
            # ten sam dom na kilku portalach -> jedna wiadomość (klucz: cena, metraż, rok)
            key = lambda r: (round(r.price_pln or 0, -2), round(r.area_m2 or 0), r.build_year)  # noqa: E731
            sent_keys = {key(r) for r in db.scalars(select(Rental).where(Rental.alerted.is_(True)))}
            pending = db.scalars(select(Rental).where(Rental.active.is_(True), Rental.matches.is_(True), Rental.alerted.is_(False))).all()
            for r in sorted(pending, key=lambda x: _km(x) or 99):
                if key(r) in sent_keys:
                    r.alerted = True
                    continue
                if send_telegram(card(r), "wynajem"):
                    r.alerted = True
                    sent_keys.add(key(r))
                    st["rent_sent"] += 1
        db.commit()
    log.info("wynajem: %s", st)
    return st
