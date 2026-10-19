import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("LANDSCOUT_DATA", "/tmp/landscout-test")

from landscout.collectors.base import area_from_text, to_float  # noqa: E402
from landscout.db import Listing  # noqa: E402
from landscout.enrich import haversine_km, match_area  # noqa: E402
from landscout.normalize import classify  # noqa: E402
from landscout.scoring import score  # noqa: E402


def test_area_parsing():
    assert area_from_text("Działka 1 200 m2 w Złotokłosie") == 1200
    assert area_from_text("0,45 ha pod zabudowę") == 4500
    assert area_from_text("12 arów") == 1200
    assert to_float("1 250 000 zł") == 1250000


def test_classify():
    assert classify("Dom do rozbiórki, działka 1500 m2", "", {}) == "dom_rozbiorka"
    assert classify("Działka budowlana Lesznowola", "", {"estate": "dzialka"}) == "dzialka"
    assert classify("Dom wolnostojący 180 m2", "nowy, wykończony", {"estate": "dom"}) == "dom"


def test_match_area():
    prof, name, ring = match_area(52.06, 20.96)  # Magdalenka
    assert name is not None and ring in ("pierscien", "warszawa", "dalej")
    assert haversine_km((52.0, 21.0), (52.0, 21.0)) == 0


def test_scoring_galano_alert():
    row = Listing(source="t", external_id="1", url="u", title="Działka 3000 m2 pod zabudowę bliźniaczą MN, media",
                  description="MPZP MN, podział możliwy, droga asfaltowa", area_m2=3000, price_pln=1_000_000, area_name="Łazy",
                  lat=52.09, lon=20.93, ring="pierscien", kind="dzialka", mpzp={"has_plan": True, "symbols": ["MN"]},
                  analysis={}, parcel={}, images=[], raw={}, price_history=[])
    score(row)
    assert row.segments_est >= 3
    assert row.verdict_galano == "alert"


def test_scoring_bauro_rejects_small():
    row = Listing(source="t", external_id="2", url="u", title="Działka 600 m2", description="", area_m2=600,
                  price_pln=500_000, lat=52.06, lon=20.96, ring="pierscien", kind="dzialka", mpzp={}, analysis={},
                  parcel={}, images=[], raw={}, price_history=[])
    score(row)
    assert row.verdict_bauro == "reject"


def test_market_line_and_sale_price():
    from landscout.comps import market_line, market_sale_price
    st = {"radius_km": 2, "gone_days": 90,
          "dom_primary": {"active": {"n": 7, "pm2_med": 10400, "pm2_p25": 9800, "pm2_p75": 11200, "area_med": 140, "area_p25": 120, "area_p75": 160},
                          "gone": {"n": 4, "pm2_med": 10100}},
          "investments": [{"name": "Osiedle X", "kind": "dom", "active": 5, "gone": 3, "pm2_med": 10200, "area_p25": 120, "area_p75": 150, "dist_km": 0.8}]}
    assert market_sale_price(st, "galano") == (10400.0, 7)
    assert market_sale_price(st, "mw") is None
    line = market_line(st)
    assert "domy nowe" in line and "4 zniknęło" in line and "Osiedle X" in line


def test_alert_requires_price_and_margin():
    base = dict(source="t", url="u", title="Działka 3000 m2 pod zabudowę bliźniaczą MN, media",
                description="MPZP MN, podział możliwy, droga asfaltowa", area_m2=3000, area_name="Łazy",
                lat=52.09, lon=20.93, ring="pierscien", kind="dzialka", mpzp={"has_plan": True, "symbols": ["MN"]},
                analysis={}, parcel={}, images=[], raw={}, price_history=[])
    no_price = Listing(external_id="p0", price_pln=None, **base)
    score(no_price)
    assert no_price.verdict_galano != "alert"
    thin = Listing(external_id="p1", price_pln=1_200_000, **base)   # marża < 15 %
    score(thin)
    assert thin.verdict_galano != "alert"


def test_galano_two_houses_good_margin_alerts():
    row = Listing(source="t", external_id="z2", url="u", title="Działka budowlana 1800 m2 MPZP MN, media, podział na 2 działki",
                  description="Plan MN, zabudowa jednorodzinna wolnostojąca, możliwość podziału na dwie działki, media w ulicy",
                  area_m2=1800, price_pln=150_000, area_name="Złotokłos", lat=52.00, lon=20.95, ring="dalej", kind="dzialka",
                  mpzp={"has_plan": True, "symbols": ["MN"]}, analysis={}, parcel={}, images=[], raw={}, price_history=[])
    score(row)
    assert row.verdict_galano == "alert", (row.verdict_galano, row.segments_est, (row.proforma_galano or {}).get("margin_pct"))


def test_sale_price_fallback_not_premium():
    from landscout.proforma import sale_price
    v, k = sale_price("mw", "Złotokłos")
    assert v <= 10000 and "lokalnie" in k


def test_rental_filter():
    from landscout.db import Rental
    from landscout.rentals import _matches
    ok = Rental(source="t", external_id="1", url="u", title="", area_m2=250, build_year=2022, building_type="semi_detached")
    old = Rental(source="t", external_id="2", url="u", title="", area_m2=250, build_year=2015, building_type="detached")
    row = Rental(source="t", external_id="3", url="u", title="", area_m2=260, build_year=2023, building_type="ribbon")
    small = Rental(source="t", external_id="4", url="u", title="", area_m2=180, build_year=2023, building_type="detached")
    assert _matches(ok) and not _matches(old) and not _matches(row) and not _matches(small)


def test_hala_scoring():
    from landscout.hala import score_hala
    base = dict(source="t", url="u", description="", kind="dzialka", parcel={}, images=[], raw={}, price_history=[], analysis={})
    near = Listing(external_id="h1", title="Działka pod halę magazynową 2000 m2", area_m2=2000, price_pln=1_200_000,
                   lat=52.08, lon=20.99, mpzp={"zones": [{"symbol": "3P/U", "purpose": "tereny produkcji i usług"}]}, **base)
    far = Listing(external_id="h2", title="Działka pod halę 2000 m2", area_m2=2000, price_pln=900_000,
                  lat=52.25, lon=21.30, mpzp={}, **base)
    house = Listing(external_id="h3", title="Działka budowlana pod dom", area_m2=1500, price_pln=600_000,
                    lat=52.07, lon=20.97, mpzp={"zones": [{"symbol": "MN", "purpose": "zabudowa jednorodzinna"}]}, **base)
    sn, why, km = score_hala(near)
    assert sn and sn > 80 and "plan" in why and km < 5
    assert score_hala(far)[0] is None          # > 25 km
    assert score_hala(house)[0] is None


def test_hala_rejects_housing_with_services():
    from landscout.hala import score_hala
    r = Listing(source="t", external_id="h4", url="u", title="Działka budowlana w Wilanowie", description="", kind="dzialka",
                area_m2=2000, price_pln=2_000_000, lat=52.10, lon=21.06, parcel={}, images=[], raw={}, price_history=[], analysis={},
                mpzp={"zones": [{"symbol": "3MNU", "purpose": "zabudowa jednorodzinna z usługami"}]})
    assert score_hala(r)[0] is None
