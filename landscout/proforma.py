"""Wstępna pro forma inwestycji: PUM, przychód, koszty, marża – dla obu profili."""
from __future__ import annotations

from functools import lru_cache

import yaml

from .config import CONFIG_DIR, profiles
from .db import Listing


@lru_cache
def market() -> dict:
    return yaml.safe_load((CONFIG_DIR / "market.yml").read_text(encoding="utf-8"))


def sale_price(profile: str, area_name: str | None) -> tuple[float, str]:
    tbl = market()["sale_price_pln_m2"][profile]
    name = (area_name or "").lower()
    best = None
    for k, v in tbl.items():
        if k == "default":
            continue
        if name.startswith(k.lower()) or k.lower() in name:
            if best is None or len(k) > len(best[0]):
                best = (k, v)
    if best:
        return float(best[1]), best[0]
    if profile in ("mw", "bauro"):
        # obszar spoza tabeli: nie zgadujemy cen premium/mieszkań – bierzemy lokalną cenę domów (tabela Galano)
        v, k = sale_price("galano", area_name)
        return v, f"lokalnie ({k})"
    return float(tbl["default"]), "default"


def _mpzp_params(row: Listing) -> dict:
    """Parametry zabudowy: z analizy modelu (odczytane z ogłoszenia/planu) lub puste."""
    a = row.analysis or {}
    mp = a.get("mpzp_params") or {}
    return {
        "far": mp.get("floor_area_ratio"),              # intensywność zabudowy (PUM/pow. działki) – jeśli plan podaje
        "coverage": mp.get("max_coverage_pct"),         # maks. % zabudowy
        "floors": mp.get("max_floors"),
        "height_m": mp.get("max_height_m"),
        "min_plot_unit": mp.get("min_plot_per_unit_m2"),
        "min_plot": mp.get("min_plot_m2"),
        "bio_pct": mp.get("min_bio_pct"),
        "types": a.get("allowed_types") or [],
    }


def estimate_pum(row: Listing, profile: str) -> tuple[float, str, str]:
    """Zwraca (PUM m², typ zabudowy, metoda). Metody: 'mpzp' (intensywność z planu), 'segmenty', 'domyslna'."""
    m = market()
    area = row.area_m2 or 0
    if not area:
        return 0.0, "?", "brak"
    p = _mpzp_params(row)
    types = p["types"]
    if profile == "mw":
        kind = "wielorodzinna"
        if p["far"]:
            return round(area * float(p["far"]) * 0.75), kind, "mpzp"
        if p["coverage"] and p["floors"]:
            return round(area * float(p["coverage"]) / 100.0 * float(p["floors"]) * 0.75), kind, "mpzp"
        return round(area * m["defaults"]["floor_area_ratio_mw"] * 0.75), kind, "domyslna"
    if profile == "galano":
        if "szeregowy" in types:
            kind = "szereg"
        elif "blizniak" in types or not types:
            kind = "blizniak"
        else:
            kind = "wolnostojacy_galano"
    else:
        kind = "wolnostojacy_bauro"

    # 1) intensywność z MPZP (najpewniejsza)
    if p["far"]:
        pum = area * float(p["far"])
        # intensywność w planie liczona zwykle do pow. całkowitej; PUM ≈ 85% + ograniczenie kondygnacjami
        pum *= 0.85
        return round(pum), kind, "mpzp"
    # 2) % zabudowy × kondygnacje
    if p["coverage"] and p["floors"]:
        pum = area * float(p["coverage"]) / 100.0 * float(p["floors"]) * 0.8
        return round(pum), kind, "mpzp"
    # 3) z liczby segmentów (Galano) / domów (BAURO)
    if profile == "galano" and row.segments_est:
        return round(row.segments_est * m["unit_pum_m2"][kind]), kind, "segmenty"
    if profile == "bauro":
        per = float(p["min_plot_unit"] or p["min_plot"] or 1000)
        houses = max(1, int(area * m["defaults"]["usable_factor"] // per))
        if area < 1800:
            houses = 1
        return round(houses * m["unit_pum_m2"]["wolnostojacy_bauro"]), kind, "segmenty"
    # 4) domyślna intensywność
    far = m["defaults"][f"floor_area_ratio_{profile}"]
    return round(area * far), kind, "domyslna"


def proforma(row: Listing, profile: str) -> dict:
    m = market()
    pum, kind, method = estimate_pum(row, profile)
    if not pum or not row.price_pln:
        return {}
    price_m2_gross, price_key = sale_price(profile, row.area_name)
    try:
        from .comps import market_sale_price
        mk = market_sale_price(row.market or {}, profile)
    except Exception:  # noqa: BLE001
        mk = None
    if mk:
        price_m2_gross, price_key = mk[0], f"rynek {((row.market or {}).get('radius_km') or 2):g} km, n={mk[1]}"
    # korekta ręczna: standard / otoczenie (1–5, 3 = typowo) ±5 % na stopień
    grade_adj = 1.0 + 0.05 * ((row.standard_grade or 3) - 3) + 0.05 * ((row.location_grade or 3) - 3)
    price_m2_gross *= grade_adj
    price_m2 = price_m2_gross / (1 + float(m.get("sale_vat", 0.08)))   # netto
    build = m["build_cost_pln_m2"][profile]
    revenue = pum * price_m2
    cost_build = pum * build
    cost_soft = cost_build * m["soft_costs_pct"]
    cost_sales = revenue * m["sales_cost_pct"]
    land = float(row.price_pln)
    land_tax = land * 0.02  # PCC – przybliżenie
    total = land + land_tax + cost_build + cost_soft + cost_sales
    margin = revenue - total
    if profile == "galano":
        units = row.segments_est
    elif profile == "mw":
        units = max(1, round(pum / 55))   # ~55 m² średnie mieszkanie
    else:
        units = max(1, round(pum / m["unit_pum_m2"]["wolnostojacy_bauro"]))
    return {
        "pum_m2": pum, "unit_kind": kind, "method": method, "units": units,
        "sale_price_m2": round(price_m2_gross), "sale_price_m2_net": round(price_m2), "sale_price_key": price_key,
        "revenue": round(revenue), "land": round(land), "build": round(cost_build), "soft": round(cost_soft),
        "sales_cost": round(cost_sales), "total_cost": round(total),
        "margin": round(margin), "margin_pct": round(margin / revenue * 100, 1) if revenue else None,
        "land_share_pct": round(land / revenue * 100, 1) if revenue else None,
        "land_per_pum": round(land / pum) if pum else None,
        "mpzp_params": _mpzp_params(row), "grade_adj": round(grade_adj, 2),
    }


def fmt_mln(v: float | None) -> str:
    if v is None:
        return "?"
    return f"{v / 1e6:.2f} mln" if abs(v) >= 1e6 else f"{v / 1e3:.0f} tys."


def proforma_line(pf: dict) -> str:
    if not pf:
        return ""
    tag = {"mpzp": "wg MPZP", "segmenty": "szac.", "domyslna": "zgrubnie"}.get(pf["method"], "")
    return (f"PUM ~{pf['pum_m2']:.0f} m² ({tag}) · sprzedaż {pf['sale_price_m2']:,.0f} zł/m² brutto · "
            f"marża <b>{fmt_mln(pf['margin'])}</b> ({pf['margin_pct']}%) · grunt {pf['land_share_pct']}% przychodu").replace(",", " ")
