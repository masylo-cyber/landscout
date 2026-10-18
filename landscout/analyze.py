"""Analiza oferty modelem Claude – zwraca ustrukturyzowany JSON."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from .config import settings
from .db import Listing

log = logging.getLogger("landscout.analyze")

SYSTEM = """Jesteś analitykiem gruntów dla dewelopera z okolic Warszawy. Oceniasz ogłoszenia sprzedaży
nieruchomości pod kątem dwóch celów:
1) GALANO – zabudowa bliźniacza / szeregowa / małe domy w budżetowych lokalizacjach (liczy się liczba segmentów,
   które realnie da się postawić).
2) BAURO – działki pod pojedyncze domy premium (Magdalenka, Konstancin, Wilanów, droższe dzielnice Warszawy).
Odpowiadasz WYŁĄCZNIE poprawnym JSON-em wg schematu, bez komentarzy. Jeśli czegoś nie da się ustalić, wpisz null.
Ważne reguły: symbole MNE/MNe/MNR, "ekstensywna", "rezydencjonalna", "tylko wolnostojąca" => allowed_types bez blizniak/szeregowy
i galano_segments_estimate = liczba domów wolnostojących (min. ~1000 m² na dom, chyba że plan mówi inaczej).
Bez MPZP/WZ nie zakładaj, że można budować.
allowed_types: wpisz "blizniak"/"szeregowy" TYLKO gdy ogłoszenie lub plan wprost to dopuszcza (słowa: bliźniacza, szeregowa,
segmenty, "pod deweloperkę", min. działka ≤ 600 m² na budynek). Samo "jednorodzinna" = ["wolnostojacy"].
divisible: true tylko gdy ogłoszenie mówi o podziale lub plan podaje min. działkę wyraźnie mniejszą niż oferowana. Sąsiedztwo S7/S8/A2, torów, linii WN obniża bauro_fit.
Nie zgaduj przeznaczenia w MPZP, jeśli tekst go nie podaje – wpisz null i oznacz w ryzykach.
Parametry zabudowy (mpzp_params) wypełniaj TYLKO gdy ogłoszenie lub dane rejestru je podają (intensywność, % zabudowy,
kondygnacje, wysokość, min. działka na budynek/segment, % biologicznie czynnej). W przeciwnym razie null."""

SCHEMA = """{
 "kind": "dzialka|dom_rozbiorka|dom|inne",
 "is_development_land": true/false,
 "plot_area_m2": number|null,
 "frontage_m": number|null,
 "zoning_symbol": "np. MN, MN/U, 3MNe, RM"|null,
 "has_mpzp": true/false/null,
 "has_wz": true/false/null,
 "allowed_types": ["wolnostojacy","blizniak","szeregowy"]|[],
 "min_plot_per_unit_m2_from_text": number|null,
 "mpzp_params": {"floor_area_ratio": number|null, "max_coverage_pct": number|null, "max_floors": integer|null,
                 "max_height_m": number|null, "min_plot_per_unit_m2": number|null, "min_plot_m2": number|null, "min_bio_pct": number|null,
                 "source": "ogloszenie|rejestr|null"},
 "utilities": {"water": true/false/null, "sewage": true/false/null, "gas": true/false/null, "power": true/false/null},
 "road_access": "publiczna asfalt|publiczna gruntowa|prywatna/służebność|brak|null",
 "divisible": true/false/null,
 "forest_or_green_nearby": true/false/null,
 "nuisances": ["np. trasa S7, linia WN, hałas"],
 "seller": "prywatny|agencja|deweloper|komornik|null",
 "price_negotiable_hint": true/false/null,
 "galano_segments_estimate": integer|null,
 "galano_fit": 0-100,
 "bauro_fit": 0-100,
 "risks": ["krótkie hasła"],
 "summary_pl": "maks. 2 krótkie zdania po polsku, konkret dla inwestora: werdykt (dla kogo i dlaczego) + jedna rzecz do sprawdzenia. Bez powtarzania ceny/powierzchni."
}"""


def _payload(row: Listing) -> str:
    mp = row.mpzp or {}
    pc = row.parcel or {}
    return json.dumps({
        "source": row.source, "title": row.title, "price_pln": row.price_pln, "area_m2": row.area_m2,
        "location": row.location_text, "area_matched": row.area_name, "ring": row.ring,
        "description": (row.description or "")[:6000],
        "parcel": {k: pc.get(k) for k in ("id", "commune", "region", "parcel", "area_m2_geom", "width_m", "depth_m", "compactness")},
        "mpzp_public_registry": {"has_plan": mp.get("has_plan"), "fields": mp.get("fields"), "symbols": mp.get("symbols")},
    }, ensure_ascii=False)


def analyze(row: Listing) -> dict:
    s = settings()
    if not s.anthropic_api_key:
        return {}
    try:
        import anthropic
        cl = anthropic.Anthropic(api_key=s.anthropic_api_key)
        msg = cl.messages.create(
            model=s.claude_model, max_tokens=1200, system=SYSTEM,
            messages=[{"role": "user", "content": f"Oferta (JSON):\n{_payload(row)}\n\nZwróć JSON wg schematu:\n{SCHEMA}"}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("{"):]
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        data["_model"] = s.claude_model
        row.analysis = data
        row.analyzed_at = datetime.now(timezone.utc)
        if (data.get("kind") in ("dzialka", "dom_rozbiorka", "dom")) and row.kind in ("unknown", "inne"):
            row.kind = data["kind"]
        if not row.area_m2 and data.get("plot_area_m2"):
            row.area_m2 = float(data["plot_area_m2"])
        return data
    except Exception as e:  # noqa: BLE001
        log.warning("analyze %s/%s: %s", row.source, row.external_id, e)
        return {}
