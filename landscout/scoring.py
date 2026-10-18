"""Scoring reguł: osobno Galano (pojemność w segmentach) i BAURO (premium)."""
from __future__ import annotations

import math
import re

from .config import profiles
from .db import Listing
from .enrich import in_profile_area_parcel

RING_DEFAULT = "dalej"

# dowody, że na działce wolno budować (bez tego – najwyżej "do przeglądu")
BUILD_EVIDENCE = re.compile(r"budowlan|\bMN\b|MN[a-z/]|warunk\w* zabudowy|\bWZ\b|pozwoleni\w* na budow|praw\w* zabudowy|inwestycyjn|pod zabudow|"
                            r"pod bli[źz]niak|pod szereg|pod dom|deweloper|mpzp|plan\w* zagospodarowania", re.I)
# sygnały gruntu rolnego / leśnego bez planu
RURAL = re.compile(r"\brolna\b|\brolne\b|siedlisk|\bRM\b|\bR\d|\bZL\b|leśn|\bLs\b|łąk|pastwisk|orna", re.I)
NUISANCE = re.compile(r"\bS7\b|\bS2\b|\bS8\b|\bA2\b|przy trasie|przy drodze krajowej|linia (?:WN|wysokiego)|tory|kolej|lotnisk|oczyszczal|składowisk", re.I)


NON_RESIDENTIAL = re.compile(r"usługow|przemysł|produkcyj|magazynow|logistyc|handlow|komercyjn|\bU\b|\bP/U\b|\bPU\b", re.I)
RESIDENTIAL = re.compile(r"\bMN|mieszkan|jednorodzinn|bli[źz]niak|szereg|\bdom", re.I)


def non_residential(row: Listing) -> bool:
    text = f"{row.title} {row.description}"
    syms = " ".join((row.mpzp or {}).get("symbols") or [])
    zon = (row.analysis or {}).get("zoning_symbol") or ""
    if re.search(r"\bMN", syms) or zon.upper().startswith("MN"):
        return False
    return bool(NON_RESIDENTIAL.search(row.title) or NON_RESIDENTIAL.search(zon)) and not RESIDENTIAL.search(text)


# zabudowa ekstensywna / rezydencjonalna / wolnostojąca – segmenty wykluczone
NO_SEGMENTS = re.compile(r"\bMNE\d*\b|\bMNe\b|\bMNR\b|ekstensywn|rezydencjonaln|tylko (?:zabudowa )?wolnostoj|wy[łl][ąa]cznie (?:zabudowa )?wolnostoj|"
                         r"zakaz (?:zabudowy )?(?:bli[źz]niacz|szeregow)|bez mo[żz]liwo[śs]ci (?:zabudowy )?(?:bli[źz]niacz|szeregow)|min\.? ?(?:pow\.? )?dzia[łl]ki (?:1[2-9]|[2-9]\d)\d\d ?m", re.I)


def no_segments(row: Listing) -> bool:
    a = row.analysis or {}
    allowed = a.get("allowed_types") or []
    if allowed and "blizniak" not in allowed and "szeregowy" not in allowed:
        return True
    syms = " ".join((row.mpzp or {}).get("symbols") or []) + " " + (a.get("zoning_symbol") or "")
    return bool(NO_SEGMENTS.search(f"{row.title} {row.description} {syms}"))


def build_evidence(row: Listing) -> bool:
    a = row.analysis or {}
    mp = row.mpzp or {}
    syms = " ".join(mp.get("symbols") or [])
    if re.search(r"\bMN", syms) or (a.get("zoning_symbol") or "").upper().startswith("MN"):
        return True
    if a.get("has_mpzp") or a.get("has_wz") or a.get("is_development_land"):
        return True
    return bool(BUILD_EVIDENCE.search(f"{row.title} {row.description}"))


def is_rural(row: Listing) -> bool:
    a = row.analysis or {}
    if a.get("is_development_land"):
        return False
    text = f"{row.title} {row.description}"
    return bool(RURAL.search(text)) and not re.search(r"budowlan|\bMN", text, re.I)


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
    if row.lat and row.lon and not in_profile_area_parcel(row.lat, row.lon, "galano", row.parcel, row.location_text):
        return 0.0, "reject", 0
    if row.kind not in ("dzialka", "dom_rozbiorka", "unknown", "inne") and a.get("kind") not in ("dzialka", "dom_rozbiorka"):
        return 0.0, "reject", 0
    if _excluded(text, prof["exclude_keywords"]) and not a.get("is_development_land") and not build_evidence(row):
        return 5.0, "reject", 0
    if row.area_m2 and row.area_m2 < prof["min_area_m2"]:
        return 10.0, "reject", 0
    if row.price_pln and row.price_pln > prof["max_price_pln"]:
        return 15.0, "review", 0

    n, _ = estimate_segments(row, prof)
    ring = row.ring or RING_DEFAULT
    need = prof["min_segments_by_ring"].get(ring, prof["min_segments_by_ring"]["dalej"])
    evidence = build_evidence(row)
    rural = is_rural(row)
    if non_residential(row):
        return 10.0, "reject", 0
    if no_segments(row):
        # MN ekstensywne / tylko wolnostojące – dla Galano najwyżej do przeglądu (domy pojedyncze)
        n_single = int((row.area_m2 or 0) * prof.get("usable_factor", 0.85) // prof["m2_per_segment"]["wolnostojacy"])
        return round(min(40.0, 10 + n_single * 6), 1), ("review" if n_single >= 3 else "reject"), n_single
    if rural and not evidence:
        n = min(n, 2)   # bez planu/WZ nie liczymy segmentów z czystej powierzchni

    score = 0.0
    score += min(n / max(need, 1), 2.0) * 35            # pojemność vs próg (max 70)
    if not evidence:
        score -= 20
    if rural:
        score -= 15
    if NUISANCE.search(f"{row.title} {row.description}"):
        score -= 10
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

    if n >= need and score >= 50 and evidence and not rural:
        verdict = "alert"
    elif (n >= max(need - 1, 1) and score >= 25) or score >= 40:
        verdict = "review"
    else:
        verdict = "reject"
    return round(score, 1), verdict, n


def score_bauro(row: Listing) -> tuple[float, str]:
    prof = profiles()["bauro"]
    text = f"{row.title} {row.description}"
    a = row.analysis or {}
    if row.lat and row.lon and not in_profile_area_parcel(row.lat, row.lon, "bauro", row.parcel, row.location_text):
        return 0.0, "reject"
    if row.kind == "dom" and a.get("kind") != "dom_rozbiorka":
        return 0.0, "reject"
    if _excluded(text, prof["exclude_keywords"]) and not a.get("is_development_land") and not build_evidence(row):
        return 5.0, "reject"
    if row.area_m2 and row.area_m2 < prof["min_area_m2"]:
        return 10.0, "reject"
    if row.price_pln and row.price_pln > prof["max_price_pln"]:
        return 15.0, "review"

    rural = is_rural(row)
    evidence = build_evidence(row)
    if non_residential(row):
        return 10.0, "reject"
    score = 30.0
    if rural:
        score -= 15
    if not evidence:
        score -= 8
    if NUISANCE.search(text):
        score -= 12
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
    verdict = "alert" if score >= prof["alert_score"] and not rural else ("review" if score >= 40 else "reject")
    return round(score, 1), verdict


def score(row: Listing) -> None:
    from .proforma import proforma
    g, vg, n = score_galano(row)
    b, vb = score_bauro(row)
    row.score_galano, row.verdict_galano, row.segments_est = g, vg, n
    row.score_bauro, row.verdict_bauro = b, vb
    row.proforma_galano = proforma(row, "galano") if vg != "reject" else {}
    row.proforma_bauro = proforma(row, "bauro") if vb != "reject" else {}
    # marża koryguje wynik i werdykt
    for prof in ("galano", "bauro"):
        pf = getattr(row, f"proforma_{prof}")
        if not pf or pf.get("margin_pct") is None:
            continue
        sc = getattr(row, f"score_{prof}") or 0.0
        mp = pf["margin_pct"]
        if mp < 10:
            sc -= 20
        elif mp < 18:
            sc -= 8
        elif mp > 30:
            sc += 6
        sc = max(0.0, min(100.0, sc))
        setattr(row, f"score_{prof}", round(sc, 1))
        if mp < 8 and getattr(row, f"verdict_{prof}") == "alert":
            setattr(row, f"verdict_{prof}", "review")
