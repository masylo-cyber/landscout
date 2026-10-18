"""Wzbogacanie oferty danymi publicznymi: geokodowanie, działka (ULDK), MPZP (KIMPZP)."""
from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from .collectors.base import client, get, log
from .config import Area, areas
from .db import Listing

NOMINATIM = "https://nominatim.openstreetmap.org/search"
GUGIK_GEOCODE = "https://services.gugik.gov.pl/uug/"
ULDK = "https://uldk.gugik.gov.pl/"
KIMPZP = "https://integracja.gugik.gov.pl/cgi-bin/KrajowaIntegracjaMiejscowychPlanowZagospodarowaniaPrzestrzennego"

ZONING = re.compile(r"\b(\d{0,3}\s?(?:MN[a-zA-Z/]*|MW[a-zA-Z/]*|U[a-zA-Z/]*|MN/U|U/MN|RM|R|ZL|ZP|ZN|P|PU|KD[A-Z-]*|WS|ZD))\b")

_last_nominatim = 0.0


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = 6371.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def geocode(text: str) -> tuple[float, float] | None:
    global _last_nominatim
    if not text:
        return None
    q = text if "polska" in text.lower() else f"{text}, Polska"
    with client() as c:
        # 1) GUGiK
        try:
            r = get(c, GUGIK_GEOCODE, params={"request": "GetAddress", "address": text, "srid": "4326"})
            res = (r.json() or {}).get("results") or {}
            if isinstance(res, dict):
                res = list(res.values())
            if res:
                first = res[0]
                return float(first["y"]), float(first["x"])
        except Exception as e:  # noqa: BLE001
            log.debug("gugik geocode: %s", e)
        # 2) Nominatim (limit 1 req/s)
        wait = 1.1 - (time.time() - _last_nominatim)
        if wait > 0:
            time.sleep(wait)
        _last_nominatim = time.time()
        try:
            r = get(c, NOMINATIM, params={"q": q, "format": "json", "limit": 1, "countrycodes": "pl"},
                    headers={"User-Agent": "LandScout/0.1 (kontakt: office@bauro.pl)"})
            js = r.json()
            if js:
                return float(js[0]["lat"]), float(js[0]["lon"])
        except Exception as e:  # noqa: BLE001
            log.debug("nominatim: %s", e)
    return None


def parcel_by_xy(lat: float, lon: float) -> dict:
    """ULDK: identyfikator działki, gmina, obręb, geometria."""
    try:
        with client() as c:
            r = get(c, ULDK, params={"request": "GetParcelByXY", "xy": f"{lon},{lat},4326",
                                      "result": "id,voivodeship,county,commune,region,parcel,geom_wkt"})
        lines = r.text.strip().splitlines()
        if not lines or lines[0].strip() != "0" or len(lines) < 2:
            return {}
        f = lines[1].split("|")
        out = {"id": f[0], "voivodeship": f[1], "county": f[2], "commune": f[3], "region": f[4],
               "parcel": f[5], "wkt": f[6] if len(f) > 6 else None}
        if out["wkt"]:
            try:
                from shapely import wkt as shp_wkt
                # ULDK zwraca geometrię w EPSG:2180 (metry) z prefiksem "SRID=2180;"
                g = shp_wkt.loads(out["wkt"].split(";")[-1])
                out["area_m2_geom"] = round(g.area)
                mrr = g.minimum_rotated_rectangle
                xs, ys = mrr.exterior.coords.xy
                edges = sorted({round(math.dist((xs[i], ys[i]), (xs[i + 1], ys[i + 1]))) for i in range(4)})
                out["width_m"], out["depth_m"] = (edges[0], edges[-1]) if edges else (None, None)
                out["compactness"] = round(g.area / mrr.area, 2) if mrr.area else None
            except Exception as e:  # noqa: BLE001
                log.debug("geom: %s", e)
            out["wkt"] = out["wkt"][:4000]
        return out
    except Exception as e:  # noqa: BLE001
        log.debug("uldk: %s", e)
        return {}


MPZP_LAYERS = ["plany,raster,wektor-str,wektor-lzb", "plany", "wektor-str,wektor-lzb"]


def _mpzp_requests(lat: float, lon: float):
    d = 0.0005
    for layers in MPZP_LAYERS:
        # WMS 1.3.0 / EPSG:4326 -> oś lat,lon
        yield {"SERVICE": "WMS", "REQUEST": "GetFeatureInfo", "VERSION": "1.3.0", "CRS": "EPSG:4326",
               "LAYERS": layers, "QUERY_LAYERS": layers, "STYLES": "",
               "BBOX": f"{lat - d},{lon - d},{lat + d},{lon + d}", "WIDTH": "101", "HEIGHT": "101", "I": "50", "J": "50",
               "INFO_FORMAT": "text/html", "FEATURE_COUNT": "10"}
        # WMS 1.1.1 / EPSG:4326 -> oś lon,lat
        yield {"SERVICE": "WMS", "REQUEST": "GetFeatureInfo", "VERSION": "1.1.1", "SRS": "EPSG:4326",
               "LAYERS": layers, "QUERY_LAYERS": layers, "STYLES": "",
               "BBOX": f"{lon - d},{lat - d},{lon + d},{lat + d}", "WIDTH": "101", "HEIGHT": "101", "X": "50", "Y": "50",
               "INFO_FORMAT": "text/html", "FEATURE_COUNT": "10"}


def mpzp_by_xy(lat: float, lon: float) -> dict:
    """KIMPZP (GUGiK) GetFeatureInfo w punkcie – nazwa planu, status, link do uchwały, symbol przeznaczenia (jeśli jest)."""
    html = ""
    try:
        with client() as c:
            for params in _mpzp_requests(lat, lon):
                r = c.get(KIMPZP, params=params)
                if r.status_code == 200 and "ServiceException" not in r.text and len(r.text.strip()) > 60:
                    html = r.text
                    break
    except Exception as e:  # noqa: BLE001
        log.debug("kimpzp: %s", e)
        return {}
    if not html:
        return {}
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n", strip=True)
    if not text or "brak" in text.lower()[:80]:
        return {"has_plan": False, "raw": text[:500]}
    kv: dict[str, str] = {}
    for tr in soup.select("tr"):
        tds = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if len(tds) >= 2 and tds[0]:
            kv[tds[0][:60]] = tds[1][:300]
    links = [a.get("href") for a in soup.select("a[href]") if a.get("href", "").startswith("http")]
    syms = sorted({m.group(1).replace(" ", "") for m in ZONING.finditer(text)})
    return {"has_plan": True, "fields": kv, "links": links[:5], "symbols": syms[:10], "raw": text[:1500]}


def match_area(lat: float, lon: float) -> tuple[str | None, str | None, str | None]:
    """Zwraca (profil-źródło, nazwa obszaru, ring) najbliższego pasującego obszaru; profile 'galano' i 'bauro' są sprawdzane obie."""
    best: tuple[float, str, Area] | None = None
    for prof, lst in areas().items():
        for a in lst:
            dkm = haversine_km((lat, lon), a.center)
            if dkm <= a.radius_km and (best is None or dkm < best[0]):
                best = (dkm, prof, a)
    if best is None:
        return None, None, None
    return best[1], best[2].name, best[2].ring


def in_profile_area(lat: float, lon: float, profile: str) -> bool:
    return any(haversine_km((lat, lon), a.center) <= a.radius_km for a in areas().get(profile, []))


def enrich(row: Listing) -> None:
    if row.lat is None or row.lon is None:
        hint = row.location_text or row.title
        pt = geocode(hint)
        if pt:
            row.lat, row.lon = pt
    if row.lat is not None and row.lon is not None:
        _, row.area_name, row.ring = match_area(row.lat, row.lon)
        if not row.parcel:
            row.parcel = parcel_by_xy(row.lat, row.lon)
            row.parcel_id = (row.parcel or {}).get("id")
            if not row.area_m2 and row.parcel.get("area_m2_geom"):
                row.area_m2 = float(row.parcel["area_m2_geom"])
        if not row.mpzp:
            row.mpzp = mpzp_by_xy(row.lat, row.lon)
    row.enriched_at = datetime.now(timezone.utc)
