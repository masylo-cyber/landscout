"""Radar rynku: oferty porównawcze (domy / mieszkania, rynek pierwotny i wtórny) z Otodom
+ statystyki w promieniu (ceny zł/m², aktywne vs zniknięte ≈ sprzedane, inwestycje w toku)."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from .collectors.base import client, get, to_float
from .collectors.otodom import LOCATIONS, _items
from .db import Comp, Listing

log = logging.getLogger("landscout.comps")

URL = "https://www.otodom.pl/pl/wyniki/sprzedaz/{estate}/{loc}?market={market}&by=LATEST&direction=DESC&limit=72&page={page}"
ESTATES = {"mieszkanie": "mieszkanie", "dom": "dom"}
MARKETS = ("PRIMARY", "SECONDARY")


def _to_comp(it: dict, kind: str, market: str) -> dict | None:
    loc = it.get("location") or {}
    coords = loc.get("coordinates") or {}
    lat, lon = to_float(coords.get("latitude")), to_float(coords.get("longitude"))
    if lat is None or lon is None:
        return None
    tp = it.get("totalPrice")
    price = to_float(tp.get("value")) if isinstance(tp, dict) else to_float(tp)
    pm2 = it.get("pricePerSquareMeter")
    pm2 = to_float(pm2.get("value")) if isinstance(pm2, dict) else to_float(pm2)
    area = to_float(it.get("areaInSquareMeters"))
    if price and area and not pm2:
        pm2 = price / area
    rg = (loc.get("reverseGeocoding") or {}).get("locations") or []
    full = max((x.get("fullName") or "" for x in rg), key=len, default="")
    dev = it.get("developmentTitle") or ((it.get("development") or {}).get("name") if isinstance(it.get("development"), dict) else None) or None
    pf = it.get("priceFromPerSquareMeter")
    raw = {k: it.get(k) for k in ("investmentUnitsNumber", "investmentState", "investmentEstimatedDelivery", "estate",
                                  "investmentUnitsAreaInSquareMeters") if it.get(k) is not None}
    if isinstance(pf, dict) and pf.get("value"):
        raw["price_from_m2"] = to_float(pf.get("value"))
        pm2 = pm2 or raw["price_from_m2"]
    ag = it.get("agency") or {}
    slug = it.get("slug") or ""
    return dict(source="otodom", external_id=str(it.get("id")), kind=kind, market=market.lower(),
                url=f"https://www.otodom.pl/pl/oferta/{slug}" if slug else f"https://www.otodom.pl/pl/oferta/{it.get('id')}",
                title=it.get("title", ""), development=dev, seller=(ag.get("name") if isinstance(ag, dict) else None),
                area_m2=area, price_pln=price, price_m2=pm2, rooms=_rooms(it.get("roomsNumber")),
                lat=lat, lon=lon, location_text=full, raw=raw)


def _rooms(v) -> int | None:
    words = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5, "SIX": 6, "SEVEN": 7, "EIGHT": 8, "NINE": 9, "TEN": 10}
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        return words.get(v.upper()) or (int(v) if v.isdigit() else None)
    return None


def collect_comps(db: Session, pages: int = 3) -> dict:
    """Pobiera oferty mieszkań i domów (pierwotny + wtórny) dla naszych lokalizacji; aktualizuje tabelę comps."""
    now = datetime.now(timezone.utc)
    n_seen = n_new = 0
    with client() as c:
        for loc in LOCATIONS:
            for kind, estate in ESTATES.items():
                for market in MARKETS:
                    for page in range(1, pages + 1):
                        try:
                            items = _items(get(c, URL.format(estate=estate, loc=loc, market=market, page=page)).text)
                        except Exception as e:  # noqa: BLE001
                            log.warning("comps %s/%s/%s p%s: %s", loc, estate, market, page, e)
                            break
                        if not items:
                            break
                        for it in items:
                            d = _to_comp(it, kind, market)
                            if not d:
                                continue
                            row = db.scalar(select(Comp).where(Comp.source == d["source"], Comp.external_id == d["external_id"]))
                            if row is None:
                                row = Comp(**d, price_history=[{"t": now.isoformat(), "p": d["price_pln"]}])
                                db.add(row)
                                n_new += 1
                            else:
                                if d["price_pln"] and row.price_pln != d["price_pln"]:
                                    row.price_history = (row.price_history or []) + [{"t": now.isoformat(), "p": d["price_pln"]}]
                                for k, v in d.items():
                                    if v is not None and k not in ("source", "external_id"):
                                        setattr(row, k, v)
                                row.active, row.gone_at = True, None
                            row.last_seen = now
                            n_seen += 1
                        db.commit()
                        if len(items) < 72:
                            break
    # zniknęły z portalu (≈ sprzedane / wycofane)
    cutoff = now - timedelta(days=10)
    n_gone = 0
    for row in db.scalars(select(Comp).where(Comp.active.is_(True), Comp.last_seen < cutoff)):
        row.active, row.gone_at = False, row.last_seen
        n_gone += 1
    db.commit()
    log.info("comps: widziane %d, nowe %d, zniknęły %d", n_seen, n_new, n_gone)
    return {"comps_seen": n_seen, "comps_new": n_new, "comps_gone": n_gone}


def _dist_km(lat1, lon1, lat2, lon2) -> float:
    p = math.pi / 180
    a = 0.5 - math.cos((lat2 - lat1) * p) / 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lon2 - lon1) * p)) / 2
    return 12742 * math.asin(math.sqrt(a))


def _bucket(rows: list[Comp]) -> dict:
    pm = sorted(r.price_m2 for r in rows if r.price_m2)
    ar = sorted(r.area_m2 for r in rows if r.area_m2)
    if not pm:
        return {"n": len(rows)}
    q = lambda xs, f: xs[min(len(xs) - 1, int(len(xs) * f))]  # noqa: E731
    return {"n": len(rows), "pm2_med": round(median(pm)), "pm2_p25": round(q(pm, 0.25)), "pm2_p75": round(q(pm, 0.75)),
            "area_med": round(median(ar)) if ar else None, "area_p25": round(q(ar, 0.25)) if ar else None,
            "area_p75": round(q(ar, 0.75)) if ar else None}


def market_stats(db: Session, lat: float, lon: float, r_km: float = 2.0, gone_days: int = 90) -> dict:
    """Statystyki rynku w promieniu: domy / mieszkania × aktywne / zniknięte (≈ sprzedane), inwestycje w toku,
    oraz działki z naszej bazy, które zniknęły (ceny ofertowe wycofanych)."""
    dlat = r_km / 111.0
    dlon = r_km / (111.0 * max(0.2, math.cos(lat * math.pi / 180)))
    since = datetime.now(timezone.utc) - timedelta(days=gone_days)
    rows = db.scalars(select(Comp).where(Comp.lat.between(lat - dlat, lat + dlat), Comp.lon.between(lon - dlon, lon + dlon))).all()
    rows = [r for r in rows if _dist_km(lat, lon, r.lat, r.lon) <= r_km and (r.active or (r.gone_at and r.gone_at >= since))]
    out: dict = {"radius_km": r_km, "gone_days": gone_days, "at": datetime.now(timezone.utc).isoformat()[:16]}
    for kind in ("dom", "mieszkanie"):
        for market in ("primary", "secondary"):
            sub = [r for r in rows if r.kind == kind and r.market == market]
            act = [r for r in sub if r.active]
            gone = [r for r in sub if not r.active]
            if sub:
                out[f"{kind}_{market}"] = {"active": _bucket(act), "gone": _bucket(gone)}
    # inwestycje w toku (rynek pierwotny) – po nazwie inwestycji
    devs: dict[str, list[Comp]] = {}
    for r in rows:
        if r.market == "primary":
            devs.setdefault(r.development or (r.seller or "?"), []).append(r)
    inv = []
    for name, rs in devs.items():
        act = [r for r in rs if r.active]
        gone = [r for r in rs if not r.active]
        b = _bucket(act or rs)
        units_total = max((int((r.raw or {}).get("investmentUnitsNumber") or 0) for r in rs), default=0) or None
        inv.append({"name": name[:60], "kind": max(("dom", "mieszkanie"), key=lambda k: sum(1 for r in rs if r.kind == k)),
                    "active": len(act), "gone": len(gone), "units_total": units_total, "pm2_med": b.get("pm2_med"),
                    "area_p25": b.get("area_p25"), "area_p75": b.get("area_p75"),
                    "dist_km": round(min(_dist_km(lat, lon, r.lat, r.lon) for r in rs), 1)})
    inv.sort(key=lambda x: (-(x["active"] + x["gone"]), x["dist_km"]))
    out["investments"] = inv[:8]
    # działki z naszej bazy, które zniknęły (≈ sprzedane / wycofane) – cena ofertowa
    pl = db.scalars(select(Listing).where(Listing.lat.between(lat - dlat, lat + dlat), Listing.lon.between(lon - dlon, lon + dlon),
                                          Listing.active.is_(False), Listing.deactivated_at >= since,
                                          Listing.price_pln.isnot(None), Listing.area_m2 > 0)).all()
    pl = [r for r in pl if _dist_km(lat, lon, r.lat, r.lon) <= r_km]
    if pl:
        pm = sorted(r.price_pln / r.area_m2 for r in pl)
        out["plots_gone"] = {"n": len(pl), "pm2_med": round(median(pm)), "pm2_min": round(pm[0]), "pm2_max": round(pm[-1])}
    return out


def market_sale_price(stats: dict, profile: str) -> tuple[float, int] | None:
    """Mediana ceny zł/m² z rynku w promieniu dla danego profilu (galano/bauro → domy, mw → mieszkania; pierwotny,
    a gdy mało danych – pierwotny+wtórny). Zwraca (cena, n) albo None gdy < 5 ofert."""
    kind = "mieszkanie" if profile == "mw" else "dom"
    prim = (stats.get(f"{kind}_primary") or {}).get("active") or {}
    if prim.get("n", 0) >= 5 and prim.get("pm2_med"):
        return float(prim["pm2_med"]), int(prim["n"])
    sec = (stats.get(f"{kind}_secondary") or {}).get("active") or {}
    n = prim.get("n", 0) + sec.get("n", 0)
    vals = [v for v in (prim.get("pm2_med"), sec.get("pm2_med")) if v]
    if n >= 5 and vals:
        return float(sum(vals) / len(vals)), int(n)
    return None


def market_line(stats: dict) -> str:
    """Jedna–dwie linijki na kartę Telegram."""
    if not stats:
        return ""
    def fmt(b: dict) -> str:
        if not b or not b.get("pm2_med"):
            return f"{b.get('n', 0)} ofert" if b else "–"
        return f"{b['pm2_p25']:,}–{b['pm2_p75']:,} (śr. {b['pm2_med']:,}) zł/m², {b['n']} akt.".replace(",", " ")
    parts = []
    for kind, label in (("dom", "domy"), ("mieszkanie", "mieszk.")):
        p = stats.get(f"{kind}_primary") or {}
        s = stats.get(f"{kind}_secondary") or {}
        if p:
            g = (p.get("gone") or {}).get("n", 0)
            parts.append(f"{label} nowe: {fmt(p.get('active'))}" + (f", {g} zniknęło/{stats['gone_days']} d" if g else ""))
        if s and (s.get("active") or {}).get("pm2_med"):
            parts.append(f"{label} wtórne: {fmt(s.get('active'))}")
    if stats.get("plots_gone"):
        pg = stats["plots_gone"]
        parts.append(f"działki wycofane: {pg['n']}, śr. {pg['pm2_med']:,} zł/m²".replace(",", " "))
    inv = [i for i in stats.get("investments") or [] if i["active"] + i["gone"] >= 3][:3]
    if inv:
        parts.append("inwestycje: " + "; ".join(f"{i['name']} ({i['kind']}, {i['active']} akt./{i['gone']} zniknęło"
                                                 + (f", {i['pm2_med']:,} zł/m²".replace(',', ' ') if i.get('pm2_med') else "") + ")" for i in inv))
    return (f"📊 {stats['radius_km']:g} km: " + " · ".join(parts)) if parts else ""
