"""Statystyki do kalibracji scoringu."""
from __future__ import annotations

from collections import Counter

from sqlalchemy import func, select

from .db import Listing, session


def _hist(values: list[float], step: int = 10) -> str:
    c = Counter(int(v // step) * step for v in values if v is not None)
    return "  ".join(f"{k}-{k + step - 1}: {c[k]}" for k in sorted(c))


def stats() -> None:
    with session() as db:
        rows = db.scalars(select(Listing).where(Listing.active.is_(True))).all()
        print(f"ofert aktywnych: {len(rows)}  | wzbogaconych: {sum(1 for r in rows if r.enriched_at)}  | z analizą LLM: {sum(1 for r in rows if r.analysis)}")
        print("źródła:", dict(Counter(r.source for r in rows)))
        print("typ:", dict(Counter(r.kind for r in rows)))
        print("pierścień:", dict(Counter(r.ring or "poza obszarem" for r in rows if r.enriched_at)))
        print("geokodowanie:", f"{sum(1 for r in rows if r.lat)} z {len(rows)} ma współrzędne;",
              f"{sum(1 for r in rows if r.parcel_id)} ma nr działki;",
              f"{sum(1 for r in rows if (r.mpzp or {}).get('has_plan'))} ma MPZP w rejestrze;",
              f"{sum(1 for r in rows if (r.mpzp or {}).get('symbols'))} ma symbol MPZP")
        enr = [r for r in rows if r.enriched_at]
        for prof in ("galano", "bauro"):
            sc = [getattr(r, f"score_{prof}") for r in enr]
            vd = Counter(getattr(r, f"verdict_{prof}") for r in enr)
            print(f"\n== {prof.upper()} ==  werdykty: {dict(vd)}")
            print("  histogram wyników:", _hist(sc))
            top = sorted([r for r in enr if getattr(r, f"score_{prof}") is not None],
                         key=lambda r: -getattr(r, f"score_{prof}"))[:12]
            for r in top:
                print(f"  #{r.id:<5} {getattr(r, f'score_{prof}'):>3.0f} {getattr(r, f'verdict_{prof}'):<6} "
                      f"{(r.area_name or r.location_text or '')[:22]:<22} {r.area_m2 or 0:>7.0f}m² "
                      f"{(r.price_pln or 0)/1e6:>5.2f}M  segm={r.segments_est or '-':<3} {r.kind:<13} "
                      f"mpzp={','.join((r.mpzp or {}).get('symbols') or [])[:12]:<12} {r.title[:50]}")
        # obszary bez trafień
        print("\nobszary wg liczby ofert:", dict(Counter(r.area_name for r in enr if r.area_name).most_common(30)))
        print("poza obszarem (przykłady):", [ (r.location_text or r.title)[:30] for r in enr if not r.area_name][:10])
        n_bauro_area = sum(1 for r in enr if r.lat and r.lon and __import__("landscout.enrich", fromlist=["in_profile_area"]).in_profile_area(r.lat, r.lon, "bauro"))
        print(f"ofert w obszarze BAURO: {n_bauro_area}")
