"""Scoring reguł: osobno Galano (pojemność w segmentach) i BAURO (premium)."""
from __future__ import annotations

import math
import re

from .config import profiles
from .db import Listing
from .enrich import in_profile_area

RING_DEFAULT = "dalej"


def _kw(text: str, words: list[str]) -> int:
    t = text.lower()
    return sum(1 for w in words if w.lower() in t)


def _excluded(text: str, words: list[str]) -> bool:
    t = text.lower()
    return any(w.lower() in t for w in words)


def estimate_segments(row: Listing, prof: dict) -> tuple[int, str]:
    """Zwraca (liczba segmentów, typ zabudowy przyjęty do wyliczenia)."""
    a = row.analysis or {}
    area = row.area_m2 or a.get("plot_area_m2") or 0
    if not area:
        return 0, "brak_powierzchni"
    per = prof["m2_per_segment"]
    allowed = a.get("allowed_types") or []
    text = f"{row.title} {row.description}".lower()
    if "szeregowy" in allowed or "szereg" in text:
        kind, m2 = "szereg", per["szereg"]
    elif "blizniak" in allowed or "bliźnia" in text or "bliznia" in text:
        kind, m2 = "blizniak", per["blizniak"]
    else:
        # bez wskazówek zakładamy bliźniak jako typową zabudowę Galano
        kind, m2 = "blizniak", per["blizniak"]
    if a.get("min_plot_per_unit_m2_from_text"):
        m2 = max(m2, float(a["min_plot_per_unit_m2_from_text"]))
    usable = area * prof.get("usable_factor", 0.85)
    n = int(math.floor(usable / m2))
    # dla bardzo dużych działek liczba segmentów rośnie wolniej (drogi wewnętrzne)
    if n > 12:
        n = 12 + int((n - 12) * 0.7)
    if a.get("galano_segments_estimate"):
        # średnia ważona reguły i oceny modelu
        n = int(round(0.6 * n + 0.4 * float(a["galano_segments_estimate"])))
    return max(n, 0), kind


def score_galano(row: Listing) -> tuple[float, str, int]:
    prof = profiles()["galano"]
    text = f"{row.title} {row.description}"
    a = row.analysis or {}
    if row.lat and row.lon and not in_profile_area(row.lat, row.lon, "galano"):
        return 0.0, "reject", 0
    if row.kind not in ("dzialka", "dom_rozbiorka", "unknown", "inne") and a.get("kind") not in ("dzialka", "dom_rozbiorka"):
        return 0.0, "reject", 0
    if _excluded(text, prof["exclude_keywords"]) and not a.get("is_development_land"):
        return 5.0, "reject", 0
    if row.area_m2 and row.area_m2 < prof["min_area_m2"]:
        return 10.0, "reject", 0
    if row.price_pln and row.price_pln > prof["max_price_pln"]:
        return 15.0, "review", 0

    n, _ = estimate_segments(row, prof)
    ring = row.ring or RING_DEFAULT
    need = prof["min_segments_by_ring"].get(ring, prof["min_segments_by_ring"]["dalej"])

    score = 0.0
    score += min(n / max(need, 1), 2.0) * 35            # pojemność vs próg (max 70)
    score += min(_kw(text, prof["boost_keywords"]), 5) * 3  # słowa kluczowe (max 15)
    mp = row.mpzp or {}
    syms = " ".join(mp.get("symbols") or [])
    if a.get("has_mpzp") or mp.get("has_plan"):
        score += 8
        if re.search(r"\bMN", syms) or (a.get("zoning_symbol") or "").startswith("MN"):
            score += 7
        if re.search(r"\b(R|RM|ZL|ZP|U|P)\b", syms) and not re.search(r"\bMN", syms):
            score -= 20
    if a.get("galano_fit") is not None:
        score = 0.7 * score + 0.3 * float(a["galano_fit"])
    if a.get("road_access") == "brak":
        score -= 25
    if a.get("divisible") is False:
        score -= 10
    score = max(0.0, min(100.0, score))

    if n >= need and score >= 50:
        verdict = "alert"
    elif n >= max(need - 1, 1) or score >= 40:
        verdict = "review"
    else:
        verdict = "reject"
    return round(score, 1), verdict, n


def score_bauro(row: Listing) -> tuple[float, str]:
    prof = profiles()["bauro"]
    text = f"{row.title} {row.description}"
    a = row.analysis or {}
    if row.lat and row.lon and not in_profile_area(row.lat, row.lon, "bauro"):
        return 0.0, "reject"
    if row.kind == "dom" and a.get("kind") != "dom_rozbiorka":
        return 0.0, "reject"
    if _excluded(text, prof["exclude_keywords"]) and not a.get("is_development_land"):
        return 5.0, "reject"
    if row.area_m2 and row.area_m2 < prof["min_area_m2"]:
        return 10.0, "reject"
    if row.price_pln and row.price_pln > prof["max_price_pln"]:
        return 15.0, "review"

    score = 30.0
    lo, hi = prof["preferred_area_m2"]
    if row.area_m2:
        if lo <= row.area_m2 <= hi:
            score += 25
        elif row.area_m2 > hi:
            score += 15
        else:
            score += 8
    score += min(_kw(text, prof["boost_keywords"]), 5) * 4
    if a.get("forest_or_green_nearby"):
        score += 8
    if a.get("nuisances"):
        score -= 6 * min(len(a["nuisances"]), 3)
    mp = row.mpzp or {}
    syms = " ".join(mp.get("symbols") or [])
    if re.search(r"\bMN", syms) or (a.get("zoning_symbol") or "").startswith("MN"):
        score += 8
    if row.ring == "warszawa":
        score += 5
    if a.get("bauro_fit") is not None:
        score = 0.6 * score + 0.4 * float(a["bauro_fit"])
    if a.get("road_access") == "brak":
        score -= 25
    score = max(0.0, min(100.0, score))
    verdict = "alert" if score >= prof["alert_score"] else ("review" if score >= 45 else "reject")
    return round(score, 1), verdict


def score(row: Listing) -> None:
    g, vg, n = score_galano(row)
    b, vb = score_bauro(row)
    row.score_galano, row.verdict_galano, row.segments_est = g, vg, n
    row.score_bauro, row.verdict_bauro = b, vb
