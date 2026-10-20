"""Grunt pod lekką halę z lokalami magazynowo-usługowymi na sprzedaż (model HLD Macierzysz: moduły 240 m², KW + udział w gruncie).
Lekkie magazynowanie – bez wymogu dojazdu TIR. Działki 1200–12000 m² (najlepiej 1500–3000) z możliwością postawienia hali
(P, U, P/U, PU, AG, usługi/produkcja/magazyn w planie lub ogłoszeniu). Priorytet: korytarz Al. Krakowskiej (DK7)
Raszyn – Janki – Sękocin – Mroków – Wola Mrokowska – Tarczyn – Grójec (np. Grzędy PU) oraz bliskość Magdalenki.
Karta liczy moduły i wstępną marżę (cena sprzedaży lokali zależy od odległości od Warszawy). Czat Telegram „hala”,
paczka co ALERT_INTERVAL_DAYS."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from html import escape

import time

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from .comps import _dist_km
from .config import settings
from .db import Listing, kv_get, kv_set, session

log = logging.getLogger("landscout.hala")

MAGDALENKA = (52.0597, 20.9572)
MAX_KM = 25           # od Magdalenki w dowolnym kierunku
MAX_KM_CORRIDOR = 40  # dalej (Tarczyn ~12 km, Grójec ~23 km) tylko wzdłuż Al. Krakowskiej
# Al. Krakowska / DK7 od Raszyna do Grójca (przybliżona linia łamana lat, lon)
KRAKOWSKA = [(52.154, 20.925), (52.134, 20.898), (52.114, 20.885), (52.071, 20.864), (52.020, 20.852),
             (51.978, 20.845), (51.930, 20.850), (51.862, 20.868)]
CORRIDOR_KM = 2.5     # tyle od Krakowskiej = „przy trasie”
CORRIDOR_MAX_KM = 6   # poza tym pasem dalekie oferty (>MAX_KM) odpadają
AREA_MIN, AREA_MAX, AREA_BEST = 1200, 12000, (1500, 3000)
# model lokali jak HLD Macierzysz (2 hale × 9 lokali po 240 m², 2,15 mln netto/lokal ≈ 8 960 zł/m²)
MODULE_M2 = 240
GLA_RATIO = 0.40      # powierzchnia lokali / powierzchnia działki (parking, manewr, 20–30% bio)
SALE_PM2_NEAR = 8000  # zł netto/m² lokalu do ~10 km od Magdalenki (Łazy, Sękocin, Mroków, Raszyn, Nadarzyn)
SALE_PM2_FAR = 6500   # zł netto/m² lokalu dalej (Grzędy, Tarczyn, Grójec, Prażmów)
BUILD_PM2 = 4000      # zł netto/m² lokalu: lekka hala stalowa, biuro, bramy, drogi, parking, media, projekt
OTHER_PCT = 0.06      # sprzedaż, finansowanie, opłaty, wyodrębnienie lokali


def _seg_km(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    """Odległość punktu od odcinka a-b (lokalne przybliżenie płaskie w km)."""
    import math
    kx = 111.32 * math.cos(math.radians(p[0]))
    ax, ay = (a[1] - p[1]) * kx, (a[0] - p[0]) * 111.32
    bx, by = (b[1] - p[1]) * kx, (b[0] - p[0]) * 111.32
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
    return math.hypot(ax + t * dx, ay + t * dy)


def krakowska_km(lat: float, lon: float) -> float:
    """Odległość od Al. Krakowskiej (DK7) między Raszynem a Grójcem."""
    return min(_seg_km((lat, lon), KRAKOWSKA[i], KRAKOWSKA[i + 1]) for i in range(len(KRAKOWSKA) - 1))


def sale_pm2(km_magdalenka: float) -> int:
    return SALE_PM2_NEAR if km_magdalenka <= 10 else SALE_PM2_FAR
HALA_TEXT = re.compile(r"pod hal|hal[aęiy]\b|magazyn|produkcyj|przemysł|składow|baz[aęy] transport|logistyc|"
                       r"aktywności gospodarczej|działalnoś\w* gospodarcz|inwestycyjn|\bP/?U\b|\bU/?P\b|\b\d*PU\b|\b\d*AG\b", re.I)
# cena gruntu: poniżej 100 zł/m² to rolna/las albo błędna lokalizacja, powyżej 2500 zł/m² hala się nie spina
PM2_MIN, PM2_MAX = 100, 2500
# Warszawa: tylko dzielnice przy Al. Krakowskiej / na południu; reszta miasta nie pasuje do modelu
WAW_OK = re.compile(r"włochy|ursus|ursynów|wilanów|wawer", re.I)
WEAK_TEXT = re.compile(r"usługow|pod usługi|komercyjn", re.I)          # tylko w tytule i bez funkcji mieszkaniowej
HOUSING = re.compile(r"jednorodzin|wielorodzin|mieszkan|rezydenc|pod dom|bl[iı]?[źz]niacz|szeregow|osiedl", re.I)
EXPLICIT = re.compile(r"pod hal|magazyn|produkc|przemysł", re.I)


def _zone_base(sym: str) -> str:
    return re.sub(r"^[\d.\s]+", "", sym or "").upper()


def _bad_zone(r: Listing) -> bool:
    """Teren w punkcie działki wyklucza halę: mieszkaniowy (M*), rolny (R, RM), las/zieleń/woda (ZL, ZP, ZN, WS)."""
    zones = (r.mpzp or {}).get("zones") or []
    if not zones:
        return False
    b = _zone_base(zones[0].get("symbol", ""))
    return b.startswith("M") or b in ("R", "RM", "RP") or b.startswith(("ZL", "ZP", "ZN", "WS"))
HALA_PURPOSE = re.compile(r"produkc|przemys|skład|magazyn|aktywno\w* gospodarcz|baz\w* ", re.I)
HALA_SYM = re.compile(r"^(?:\d+\.?)?(P|U|PU|P/U|U/P|UP|AG|P-U|U-P|PS|UC)(?:[/.-]|$)", re.I)
NOT_HALA = re.compile(r"usługi publiczne|oświat|sakraln|kultu religijnego|\bUO\b|\bUK\b|zabudowa jednorodzinna(?! z usług)", re.I)


def _evidence(r: Listing) -> tuple[int, str]:
    """Siła dowodu: 2 = plan (symbol/przeznaczenie), 1 = ogłoszenie / analiza, 0 = brak."""
    mp = r.mpzp or {}
    zones = mp.get("zones") or []
    on_road = bool(zones) and _zone_base(zones[0].get("symbol", "")).startswith("KD")   # pinezka na drodze
    if zones and not on_road:                       # teren w punkcie działki
        z = zones[0]
        pur = z.get("purpose", "")
        usl_only = re.search(r"usług", pur, re.I) and not HOUSING.search(pur)
        if (HALA_SYM.match(z.get("symbol", "").replace(" ", "")) or HALA_PURPOSE.search(pur) or usl_only) and not NOT_HALA.search(pur):
            return 2, f"plan: {z.get('symbol')} – {pur}"
    if not on_road:
        for sym in mp.get("symbols") or []:
            if HALA_SYM.match(sym):
                return 2, f"plan: {sym}"
    a = r.analysis or {}
    zs = a.get("zoning_symbol") or ""
    if zs and HALA_SYM.match(zs.replace(" ", "")):
        return 1, f"ogłoszenie/analiza: {zs}"
    # tytuł: dowolne słowo z HALA_TEXT; opis: tylko wyraźne (pod halę / magazyn / produkcja / przemysł / symbol planu)
    m = HALA_TEXT.search(r.title or "") or re.search(r"pod hal|magazyn|produkc|przemysł|\bP/?U\b|\bU/?P\b|\b\d*PU\b|\b\d*AG\b",
                                                    r.description or "", re.I)
    if m and not NOT_HALA.search(r.title) and not HOUSING.search(r.title):
        return 1, f"ogłoszenie: „{m.group(0)}”"
    w = WEAK_TEXT.search(r.title)
    if w and not HOUSING.search(r.title):
        return 1, f"ogłoszenie: „{w.group(0)}” (do potwierdzenia, czy hala dopuszczona)"
    return 0, ""


def proforma(r: Listing) -> dict:
    """Ile modułów 240 m² zmieści działka i jaka wstępna marża przy sprzedaży lokali."""
    mods = int((r.area_m2 or 0) * GLA_RATIO // MODULE_M2)
    gla = mods * MODULE_M2
    km = _dist_km(MAGDALENKA[0], MAGDALENKA[1], r.lat, r.lon) if r.lat and r.lon else 0.0
    rev = gla * sale_pm2(km)
    land = r.price_pln or 0
    cost = gla * BUILD_PM2 + land + rev * OTHER_PCT
    margin = rev - cost
    return {"modules": mods, "gla": gla, "revenue": rev, "cost": cost, "margin": margin,
            "margin_pct": round(100 * margin / rev, 1) if rev else None, "land_known": bool(land)}


def _in_warsaw_wrong_district(r: Listing) -> bool:
    p = r.parcel or {}
    where = f"{p.get('commune', '')} {p.get('region', '')} {r.location_text or ''} {r.area_name or ''}"
    return bool(re.search(r"warszawa", where, re.I)) and not WAW_OK.search(where)


def score_hala(r: Listing) -> tuple[float | None, str, float | None]:
    if not (r.lat and r.lon) or r.kind not in ("dzialka", "unknown", "inne", "dom_rozbiorka"):
        return None, "", None
    if not r.area_m2 or not (AREA_MIN <= r.area_m2 <= AREA_MAX):
        return None, "", None
    if r.price_pln and r.area_m2 and not (PM2_MIN <= r.price_pln / r.area_m2 <= PM2_MAX):
        return None, "", None
    if _in_warsaw_wrong_district(r):
        return None, "", None
    km = _dist_km(MAGDALENKA[0], MAGDALENKA[1], r.lat, r.lon)
    if km > MAX_KM_CORRIDOR or (km > MAX_KM and krakowska_km(r.lat, r.lon) > CORRIDOR_MAX_KM):
        return None, "", None
    if (HOUSING.search(r.title or "") or _bad_zone(r)) and not EXPLICIT.search(r.title or ""):
        return None, "", km
    ev, why = _evidence(r)
    if ev == 0:
        return None, "", km
    kk = krakowska_km(r.lat, r.lon)
    sc = 100 - km * 1.5 + (15 if ev == 2 else 0)
    if kk <= CORRIDOR_KM:
        sc += 15
        why = f"{why} · Al. Krakowska {kk:.1f} km"
    elif kk <= 6:
        sc += 5
    if AREA_BEST[0] <= r.area_m2 <= AREA_BEST[1]:
        sc += 10
    if re.search(r"pod hal|hala|magazyn", f"{r.title} {r.description or ''}", re.I):
        sc += 10
    if not r.price_pln or r.price_pln < 10000:
        sc -= 15
    else:
        mp = proforma(r).get("margin_pct")
        if mp is not None:
            sc += 10 if mp >= 25 else (0 if mp >= 15 else -15)
    return round(max(0.0, min(100.0, sc)), 1), why, km


def _pf_line(r: Listing) -> str:
    pf = proforma(r)
    if not pf["modules"]:
        return ""
    line = f"≈{pf['modules']} lokali × {MODULE_M2} m² · sprzedaż {pf['revenue'] / 1e6:.1f} mln netto ({pf['revenue'] // max(pf['gla'], 1):,.0f} zł/m²)".replace(",", " ")
    if pf["land_known"]:
        line += f" · marża {pf['margin'] / 1e6:.1f} mln ({pf['margin_pct']:.0f}%)"
    return line


def card(r: Listing, why: str, km: float) -> str:
    pm2 = f" · {r.price_pln / r.area_m2:,.0f} zł/m²".replace(",", " ") if r.price_pln and r.area_m2 else ""
    price = f"{r.price_pln / 1e6:.2f} mln" if r.price_pln and r.price_pln >= 1e6 else (f"{r.price_pln / 1e3:.0f} tys." if r.price_pln else "cena ?")
    return "\n".join([
        f"<b>🏭 HALA {r.score_hala:.0f}</b> · {escape((r.location_text or r.area_name or '?')[:60])} · <b>{km:.1f} km</b> od Magdalenki",
        f"<b>{price}</b>{pm2} · {r.area_m2:,.0f} m²".replace(",", " "),
        escape(why[:140]),
        _pf_line(r),
        escape(r.title[:110]),
        f'<a href="{escape(r.url)}">▶ oferta ({r.source})</a>'
        + (f' · <a href="https://maps.google.com/?q={r.lat},{r.lon}">mapa</a>' if r.lat and r.lon else "") + f" · #{r.id}",
    ])


def run_hala(send: bool = True, force: bool = False) -> dict:
    """Z ponawianiem przy deadlocku (równoległy `landscout run` aktualizuje te same wiersze)."""
    for attempt in range(3):
        try:
            return _run_hala(send, force)
        except OperationalError as e:
            if "deadlock" not in str(e).lower() or attempt == 2:
                raise
            log.warning("hala: deadlock, ponawiam za 3 s")
            time.sleep(3)
    return {}


def _run_hala(send: bool, force: bool) -> dict:
    from .notify import send_telegram, telegram_chat_id
    st = {"hala_matches": 0, "hala_sent": 0}
    with session() as db:
        rows = []
        # wiersz po wierszu, w kolejności id – bez zbiorczego UPDATE blokującego całą tabelę
        for r in db.scalars(select(Listing).where(Listing.lat.isnot(None)).where(
                (Listing.score_hala.isnot(None)) | (Listing.active.is_(True) & Listing.area_m2.between(AREA_MIN, AREA_MAX)))
                .order_by(Listing.id)):
            sc, why, km = score_hala(r) if r.active else (None, "", None)
            if r.score_hala != sc:
                r.score_hala = sc
            if sc is not None and sc >= 40:
                rows.append((r, why, km))
        db.commit()
        st["hala_matches"] = len(rows)
        if not send or not telegram_chat_id("hala"):
            return st
        last = kv_get(db, "hala_batch_at", "")
        due = force or not last or (datetime.now(timezone.utc) - datetime.fromisoformat(last)) >= timedelta(days=settings().alert_interval_days)
        new = [(r, w, k) for r, w, k in rows if not r.alerted_hala]
        if not due or not new:
            return st
        seen, out = set(), []
        for r, why, km in sorted(new, key=lambda x: -x[0].score_hala):
            key = (round(r.area_m2 or 0, -1), round(r.price_pln or 0, -4))
            if key in seen:          # ta sama działka z innego portalu
                r.alerted_hala = True
                continue
            seen.add(key)
            out.append(card(r, why, km))
            r.alerted_hala = True
        chunk = f"<b>🏭 Grunty pod lekką halę – {len(out)} nowych (korytarz Al. Krakowskiej i okolice Magdalenki na górze)</b>"
        for c in out:
            if len(chunk) + len(c) > 3800:
                send_telegram(chunk, "hala")
                chunk = ""
            chunk += "\n\n" + c
        if chunk.strip():
            send_telegram(chunk, "hala")
        st["hala_sent"] = len(out)
        kv_set(db, "hala_batch_at", datetime.now(timezone.utc).isoformat())
        db.commit()
    log.info("hala: %s", st)
    return st
