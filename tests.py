import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
os.environ["DB_FILE"] = os.path.join(tempfile.gettempdir(), "quant_v3_test.db")

from main import (
    poisson_matrix,
    model_from_form,
    monte_carlo,
    true_ev,
    fractional_kelly,
    extract_real_odds,
    validate_event,
    Ineligible,
    cache_set, cache_get, _cache,
)


def form(team):
    return {
        "team": team, "team_id": team, "sample": 10,
        "last5_gf": 1.8, "last5_ga": 1.0,
        "last10_gf": 1.5, "last10_ga": 1.1,
        "home_gf": 2.0, "home_ga": 0.9,
        "away_gf": 1.2, "away_ga": 1.3,
        "matches": [], "source": "test",
    }


def test_math():
    m = poisson_matrix(1.6, 1.1)
    assert abs(sum(sum(r) for r in m) - 1) < 1e-9
    model = model_from_form(form("A"), form("B"))
    assert 0 < model["lambda_home"] <= 4.5
    assert 0 < model["lambda_away"] <= 4.5
    mc = monte_carlo(model["lambda_home"], model["lambda_away"], model["seed"], 2000)
    assert 99.0 <= mc["prob_home"] + mc["prob_draw"] + mc["prob_away"] <= 101.0


def test_ev_kelly():
    assert true_ev(55, 2.0) == 10.0
    pct, stake = fractional_kelly(55, 2.0, 1000)
    assert pct > 0 and stake > 0 and pct <= 2.0


def test_no_fake_odds():
    event = {"home":"A", "away":"B"}
    payload = {"bookmakers": []}
    try:
        extract_real_odds(event, payload)
        raise AssertionError("empty odds must be rejected")
    except Exception as exc:
        assert exc.__class__.__name__ == "NoData"


def test_negative_cache_is_not_stored():
    _cache.clear()
    cache_set("empty", [], 60)
    assert cache_get("empty") is None


def test_event_validation():
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    e = validate_event({"id":"x", "home_team":"A", "away_team":"B", "commence_time":future, "sport_key":"soccer_test", "sport_title":"Test"})
    assert e["event_id"] == "x"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
    print("ALL TESTS PASSED")
