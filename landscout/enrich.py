"""Wzbogacanie oferty danymi publicznymi: geokodowanie, działka (ULDK), MPZP (KIMPZP)."""
from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone

import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from .collectors.base import client, get, log
from .config import Area, areas
from .db import Listing

NOMINATIM = "https://nominatim.openstreetmap.org/search"
GUGIK_GEOCODE = "https://services.gugik.gov.pl/uug/"
ULDK = "https://uldk.gugik.gov.pl/"
KIMPZP = "https://integracja.gugik.gov.pl/cgi-bin/KrajowaIntegracjaMiejscowychPlanowZagospodarowaniaPrzestrzennego"

# symbol przeznaczenia: opcjonalny numer + litery wielkie (MN, MNU, MN/U, 3MNe, RM, ZL, U, P, KDD...) jako osobny token
ZONING = re.compile(r"(?<![A-Za-ząćęłńóśźż])(\d{0,3}(?:MN[a-zA-Z]{0,3}(?:/U)?|MW[a-zA-Z]{0,2}|U(?:/MN)?|RM|R|ZL|ZP|ZN|P|PU|KD[A-Z]{0,3}|WS|ZD))(?![a-ząćęłńóśźż])")

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
        # WMS 1.1.1 / EPSG:4326 (oś lon,lat) – ten wariant KIMPZP obsługuje poprawnie
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
                t = r.text.strip()
                if r.status_code != 200 or "ServiceException" in t or not t:
                    continue
                low = t.lower()
                if "brak wyniku" in low:
                    gmina = t.split(":")[0].strip() if ":" in t else None
                    return {"has_plan": False, "gmina": gmina, "raw": t[:200]}
                if "brak serwisu" in low:
                    continue
                html = t
                break
    except Exception as e:  # noqa: BLE001
        log.debug("kimpzp: %s", e)
        return {}
    if not html:
        return {}
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n", strip=True)
    if not text:
        return {}
    kv: dict[str, str] = {}
    for tr in soup.select("tr"):
        tds = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if len(tds) >= 2 and tds[0]:
            kv[tds[0][:60]] = tds[1][:300]
    links = [a.get("href") for a in soup.select("a[href]") if a.get("href", "").startswith("http")]
    sym_src = " ".join(v for k, v in kv.items() if re.search(r"przeznacz|symbol|oznacz|funkc", k, re.I))
    syms = sorted({m.group(1) for m in ZONING.finditer(sym_src)}) if sym_src else []
    return {"has_plan": True, "fields": kv, "links": links[:5], "symbols": syms[:10], "raw": text[:1500]}


def _norm(x: str | None) -> str:
    return (x or "").strip().lower().replace("-", " ")


def _area_hits(a: Area, lat: float, lon: float, parcel: dict | None, location_text: str = "") -> bool:
    parcel = parcel or {}
    commune = _norm(parcel.get("commune"))
    region = _norm(parcel.get("region"))
    location_text = location_text or ""
    loc = _norm(location_text)
    if a.regions:
        # obręb z ewidencji: dopasowanie po początku nazwy; tekst lokalizacji: tylko pierwsze 2 człony (miejscowość/dzielnica),
        # żeby "Obory, Konstancin-Jeziorna" nie łapało się na "Konstancin-Jeziorna"
        loc_parts = [_norm(x) for x in location_text.split(",")[:2] if not _norm(x).startswith("ul")]
        # jeśli pierwszy człon to miejscowość (nie ulica), drugi jest już gminą -> nie porównujemy go z obrębami
        if loc_parts and not _norm(location_text.split(",")[0]).startswith("ul"):
            loc_parts = loc_parts[:1]
        def hit(r: str) -> bool:
            r = _norm(r)
            return bool(r) and (region.startswith(r) or any(lp == r or lp.startswith(r + " ") for lp in loc_parts))
        if any(hit(r) for r in a.regions):
            # gmina z ewidencji (jeśli znana) musi zgadzać się z obszarem – tekst ogłoszenia sam nie wystarcza
            if commune:
                if a.district and "warszawa" not in commune:
                    return False
                if a.commune and _norm(a.commune) != commune:
                    return False
            return True
        if a.strict:
            return False
    if commune:
        if a.district and "warszawa" in commune and _norm(a.district) in (region + " " + commune):
            return True
        if a.commune and _norm(a.commune) == commune:
            return True
    return a.radius_km > 0 and haversine_km((lat, lon), a.center) <= a.radius_km


def match_area(lat: float, lon: float, parcel: dict | None = None, location_text: str = "",
               profile: str | None = None) -> tuple[str | None, str | None, str | None]:
    """Zwraca (profil, nazwa obszaru, ring) najlepiej pasującego obszaru. Priorytet: bauro (strict) > galano."""
    best: tuple[float, str, Area] | None = None
    for prof in ([profile] if profile else ["bauro", "galano"]):
        for a in areas().get(prof, []):
            if _area_hits(a, lat, lon, parcel, location_text):
                dkm = haversine_km((lat, lon), a.center)
                if best is None or dkm < best[0]:
                    best = (dkm, prof, a)
        if best and not profile:
            break
    if best is None:
        return None, None, None
    return best[1], best[2].name, best[2].ring


def in_profile_area_parcel(lat: float, lon: float, profile: str, parcel: dict | None = None, location_text: str = "") -> bool:
    return any(_area_hits(a, lat, lon, parcel, location_text) for a in areas().get(profile, []))


def enrich(row: Listing) -> None:
    if row.lat is None or row.lon is None:
        hint = row.location_text or row.title
        pt = geocode(hint)
        if pt:
            row.lat, row.lon = pt
    if row.lat is not None and row.lon is not None:
        if not row.parcel:
            row.parcel = parcel_by_xy(row.lat, row.lon)
            row.parcel_id = (row.parcel or {}).get("id")
            if not row.area_m2 and row.parcel.get("area_m2_geom"):
                row.area_m2 = float(row.parcel["area_m2_geom"])
        _, row.area_name, row.ring = match_area(row.lat, row.lon, row.parcel, row.location_text)
        if not row.mpzp:
            row.mpzp = mpzp_by_xy(row.lat, row.lon)
    row.enriched_at = datetime.now(timezone.utc)
