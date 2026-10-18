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
                  description="MPZP MN, podział możliwy, droga asfaltowa", area_m2=3000, price_pln=1_200_000, area_name="Łazy",
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
