from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import sqlite3
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

try:
    from flask import Flask, jsonify, request
except Exception:  # pragma: no cover
    Flask = None
    jsonify = None
    request = None

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

try:
    import telebot
except Exception:  # pragma: no cover
    telebot = None

try:
    from google import genai
    from google.genai import types as genai_types
except Exception:  # pragma: no cover
    genai = None
    genai_types = None

# ============================================================
# QUANT TERMINAL V3
# Strict provenance: no synthetic event/odds/form/model output.
# ============================================================

APP_VERSION = "quant-terminal-v3.0.0"
MODEL_VERSION = "poisson-form-montecarlo-v3"
DB_FILE = os.getenv("DB_FILE", "database.db")
UTC = timezone.utc
VN = ZoneInfo("Asia/Ho_Chi_Minh")
DB_TIMEOUT = 15
HTTP_TIMEOUT = 10
ODDS_CACHE_TTL = 45
EVENTS_CACHE_TTL = 60
TEAM_CACHE_TTL = 900
SPORTS_CACHE_TTL = 300
MAX_KELLY_PCT = 2.0
DEFAULT_FRACTIONAL_KELLY = 0.25

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("quant-v3")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

bot = telebot.TeleBot(TELEGRAM_TOKEN) if (telebot and TELEGRAM_TOKEN) else None
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if (genai and GEMINI_API_KEY) else None


class QuantError(RuntimeError):
    code = "ERROR"


class RateLimited(QuantError):
    code = "RATE_LIMITED"


class UpstreamError(QuantError):
    code = "UPSTREAM_ERROR"


class NoData(QuantError):
    code = "NO_DATA"


class InvalidData(QuantError):
    code = "INVALID_DATA"


class Ineligible(QuantError):
    code = "INELIGIBLE"


_cache: Dict[str, Tuple[float, int, Any]] = {}
_cache_lock = RLock()


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return now_utc().isoformat()


def normalize_name(value: Any) -> str:
    import re
    s = str(value or "").lower().strip()
    s = s.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def same_team(a: Any, b: Any) -> bool:
    na, nb = normalize_name(a), normalize_name(b)
    return bool(na and nb and (na == nb or na in nb or nb in na))


def finite_positive(value: Any, minimum: float = 0.0) -> bool:
    try:
        x = float(value)
        return math.isfinite(x) and x > minimum
    except (TypeError, ValueError):
        return False


def cache_get(key: str) -> Any:
    with _cache_lock:
        item = _cache.get(key)
        if not item:
            return None
        created, ttl, value = item
        if time.monotonic() - created > ttl:
            _cache.pop(key, None)
            return None
        return value


def cache_set(key: str, value: Any, ttl: int) -> Any:
    # Important: never cache empty/negative results. This prevents stale
    # "no candidates" responses from being reused after a fresh search.
    if value is None or value == [] or value == {}:
        return value
    with _cache_lock:
        _cache[key] = (time.monotonic(), ttl, value)
    return value


def _http_json(url: str, params: Optional[dict] = None, *, cache_key: Optional[str] = None,
               cache_ttl: int = 0, retries: int = 2) -> Any:
    if requests is None:
        raise UpstreamError("Python package 'requests' is not installed")
    if cache_key:
        cached = cache_get(cache_key)
        if cached is not None:
            return cached

    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=HTTP_TIMEOUT,
                headers={"User-Agent": f"QuantTerminal/{APP_VERSION}"},
            )
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise UpstreamError(str(exc)) from exc

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                delay = min(10.0, max(0.5, float(retry_after))) if retry_after else min(10.0, 0.75 * (2 ** attempt))
            except ValueError:
                delay = min(10.0, 0.75 * (2 ** attempt))
            if attempt < retries:
                time.sleep(delay)
                continue
            raise RateLimited("Upstream returned HTTP 429 after retries")

        if response.status_code >= 400:
            raise UpstreamError(f"HTTP {response.status_code}: {response.text[:300]}")
        try:
            data = response.json()
        except Exception as exc:
            raise UpstreamError("Upstream returned invalid JSON") from exc
        return cache_set(cache_key, data, cache_ttl) if cache_key else data

    raise UpstreamError(str(last_error or "Unknown upstream error"))


# --------------------------- DB -----------------------------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=DB_TIMEOUT)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS access_keys (
            key_hash TEXT PRIMARY KEY,
            note TEXT,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at TEXT,
            expires_at TEXT,
            bound_user_id TEXT
        );
        CREATE TABLE IF NOT EXISTS match_backtest (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            event_id TEXT NOT NULL,
            match_name TEXT NOT NULL,
            league TEXT,
            market TEXT NOT NULL,
            selection TEXT NOT NULL,
            model_probability REAL NOT NULL,
            odds REAL NOT NULL,
            entry_odds REAL NOT NULL,
            closing_odds REAL,
            ev REAL NOT NULL,
            stake REAL NOT NULL DEFAULT 0,
            result TEXT NOT NULL DEFAULT 'PENDING',
            profit_loss REAL NOT NULL DEFAULT 0,
            actual_score TEXT,
            model_version TEXT NOT NULL,
            odds_source TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            model_inputs TEXT NOT NULL,
            created_at TEXT NOT NULL,
            settled_at TEXT,
            settlement_source TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_backtest_event ON match_backtest(event_id);
        CREATE INDEX IF NOT EXISTS idx_backtest_result ON match_backtest(result);
        """)


init_db()


def hash_key(value: str) -> str:
    return hashlib.sha256(value.strip().encode()).hexdigest()


def access_ok(key: str, user_id: str = "guest_user", bind: bool = False) -> bool:
    if not key:
        return False
    kh = hash_key(key)
    with db() as conn:
        row = conn.execute("SELECT * FROM access_keys WHERE key_hash=? AND status='ACTIVE'", (kh,)).fetchone()
        if not row:
            return False
        if row["expires_at"]:
            try:
                if datetime.fromisoformat(row["expires_at"]) < now_utc():
                    return False
            except ValueError:
                return False
        if row["bound_user_id"] and row["bound_user_id"] != user_id:
            return False
        if bind and user_id != "guest_user" and not row["bound_user_id"]:
            conn.execute("UPDATE access_keys SET bound_user_id=? WHERE key_hash=?", (user_id, kh))
            conn.commit()
        return True


# ------------------------ Odds API --------------------------

def require_odds_key() -> None:
    if not ODDS_API_KEY:
        raise UpstreamError("ODDS_API_KEY is not configured")


def get_soccer_sports() -> List[dict]:
    require_odds_key()
    data = _http_json(
        "https://api.the-odds-api.com/v4/sports/",
        {"apiKey": ODDS_API_KEY},
        cache_key="odds:sports",
        cache_ttl=SPORTS_CACHE_TTL,
    )
    sports = [x for x in (data or []) if str(x.get("key", "")).startswith("soccer_") and x.get("active")]
    return sports


def get_events(sport_key: str) -> List[dict]:
    require_odds_key()
    return _http_json(
        f"https://api.the-odds-api.com/v4/sports/{sport_key}/events/",
        {"apiKey": ODDS_API_KEY},
        cache_key=f"events:{sport_key}",
        cache_ttl=EVENTS_CACHE_TTL,
    ) or []


def get_event_odds(sport_key: str, event_id: str) -> dict:
    require_odds_key()
    data = _http_json(
        f"https://api.the-odds-api.com/v4/sports/{sport_key}/events/{event_id}/odds/",
        {
            "apiKey": ODDS_API_KEY,
            "regions": "eu",
            "markets": "h2h,totals",
            "oddsFormat": "decimal",
        },
        cache_key=f"event_odds:{event_id}",
        cache_ttl=ODDS_CACHE_TTL,
    )
    if not isinstance(data, dict):
        raise InvalidData("Event odds payload is not an object")
    return data


def validate_event(event: dict) -> dict:
    event_id = str(event.get("id") or "").strip()
    home = str(event.get("home_team") or "").strip()
    away = str(event.get("away_team") or "").strip()
    commence = str(event.get("commence_time") or "").strip()
    sport_key = str(event.get("sport_key") or "").strip()
    if not all([event_id, home, away, commence, sport_key]):
        raise InvalidData("Event missing id/team/kickoff/sport_key")
    try:
        dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidData("Invalid event commence_time") from exc
    if dt <= now_utc():
        raise Ineligible("Event has already started")
    return {
        "event_id": event_id,
        "home": home,
        "away": away,
        "sport_key": sport_key,
        "league": str(event.get("sport_title") or sport_key),
        "commence_time": dt.astimezone(VN).strftime("%H:%M - %d/%m/%Y"),
        "commence_iso": dt.isoformat(),
    }


def _best_decimal(values: Iterable[float]) -> Optional[float]:
    vals = [float(v) for v in values if finite_positive(v, 1.0)]
    return max(vals) if vals else None


def extract_real_odds(event: dict, odds_payload: dict) -> dict:
    home = event["home"]
    away = event["away"]
    h, d, a, over25 = [], [], [], []
    bookmakers: List[str] = []
    snapshots: List[dict] = []

    for bookmaker in odds_payload.get("bookmakers") or []:
        title = str(bookmaker.get("title") or bookmaker.get("key") or "").strip()
        if title:
            bookmakers.append(title)
        for market in bookmaker.get("markets") or []:
            key = market.get("key")
            last_update = market.get("last_update")
            if last_update:
                snapshots.append({"bookmaker": title, "market": key, "last_update": last_update})
            if key == "h2h":
                for outcome in market.get("outcomes") or []:
                    name, price = outcome.get("name"), outcome.get("price")
                    if not finite_positive(price, 1.0):
                        continue
                    if same_team(name, home): h.append(float(price))
                    elif same_team(name, away): a.append(float(price))
                    elif normalize_name(name) == "draw": d.append(float(price))
            elif key == "totals":
                for outcome in market.get("outcomes") or []:
                    name, point, price = outcome.get("name"), outcome.get("point"), outcome.get("price")
                    try: point_f = float(point)
                    except (TypeError, ValueError): continue
                    if normalize_name(name).startswith("over") and abs(point_f - 2.5) < 1e-9 and finite_positive(price, 1.0):
                        over25.append(float(price))

    result = {
        "home_odds": _best_decimal(h),
        "draw_odds": _best_decimal(d),
        "away_odds": _best_decimal(a),
        "over_2_5_odds": _best_decimal(over25),
        "bookmakers": bookmakers,
        "source": "The Odds API",
        "snapshots": snapshots,
    }
    if not any(result[k] for k in ("home_odds", "draw_odds", "away_odds", "over_2_5_odds")):
        raise NoData("No real bookmaker odds for this event")
    return result


# ----------------------- Form source ------------------------

def get_team_profile(team_name: str) -> dict:
    query = {"t": team_name}
    data = _http_json(
        "https://www.thesportsdb.com/api/v1/json/3/searchteams.php",
        query,
        cache_key=f"team_search:{normalize_name(team_name)}",
        cache_ttl=TEAM_CACHE_TTL,
    )
    teams = data.get("teams") or []
    if not teams:
        raise NoData(f"No team profile found for {team_name}")
    exact = [t for t in teams if normalize_name(t.get("strTeam")) == normalize_name(team_name)]
    if len(exact) == 1:
        team = exact[0]
    elif len(teams) == 1:
        team = teams[0]
    else:
        raise Ineligible(f"Ambiguous team mapping for {team_name}")
    tid = str(team.get("idTeam") or "")
    if not tid:
        raise InvalidData(f"Team {team_name} has no idTeam")
    return {"id": tid, "name": team.get("strTeam") or team_name}


def get_team_form(team_name: str, limit: int = 10) -> dict:
    profile = get_team_profile(team_name)
    data = _http_json(
        "https://www.thesportsdb.com/api/v1/json/3/eventslast.php",
        {"id": profile["id"]},
        cache_key=f"form:{profile['id']}",
        cache_ttl=TEAM_CACHE_TTL,
    )
    events = data.get("results") or []
    rows: List[dict] = []
    for ev in events:
        try:
            hs, aws = int(ev.get("intHomeScore")), int(ev.get("intAwayScore"))
        except (TypeError, ValueError):
            continue
        date = str(ev.get("dateEvent") or "")
        if not date:
            continue
        if same_team(profile["name"], ev.get("strHomeTeam")):
            gf, ga, venue = hs, aws, "home"
            opponent = ev.get("strAwayTeam")
        elif same_team(profile["name"], ev.get("strAwayTeam")):
            gf, ga, venue = aws, hs, "away"
            opponent = ev.get("strHomeTeam")
        else:
            continue
        rows.append({
            "date": date, "gf": gf, "ga": ga, "venue": venue,
            "league": ev.get("strLeague"), "event_id": ev.get("idEvent"), "opponent": opponent,
        })
    rows.sort(key=lambda x: x["date"], reverse=True)
    rows = rows[:limit]
    if len(rows) < 5:
        raise Ineligible(f"Insufficient real form sample for {profile['name']}: {len(rows)}/5")

    def avg(key: str, subset: Optional[List[dict]] = None) -> Optional[float]:
        vals = [(r[key]) for r in (subset if subset is not None else rows)]
        return round(sum(vals) / len(vals), 4) if vals else None

    home_rows = [r for r in rows if r["venue"] == "home"]
    away_rows = [r for r in rows if r["venue"] == "away"]
    return {
        "team": profile["name"], "team_id": profile["id"], "sample": len(rows), "matches": rows,
        "last5_gf": avg("gf", rows[:5]), "last5_ga": avg("ga", rows[:5]),
        "last10_gf": avg("gf"), "last10_ga": avg("ga"),
        "home_gf": avg("gf", home_rows), "home_ga": avg("ga", home_rows),
        "away_gf": avg("gf", away_rows), "away_ga": avg("ga", away_rows),
        "source": "TheSportsDB",
    }


# ---------------------- Quant model -------------------------

def poisson_p(lmbda: float, goals: int) -> float:
    if lmbda <= 0 or goals < 0:
        return 0.0
    return (lmbda ** goals) * math.exp(-lmbda) / math.factorial(goals)


def poisson_matrix(lambda_home: float, lambda_away: float, max_goals: int = 10) -> List[List[float]]:
    if not (finite_positive(lambda_home) and finite_positive(lambda_away)):
        raise InvalidData("Model lambda must be real and positive")
    ph = [poisson_p(lambda_home, i) for i in range(max_goals + 1)]
    pa = [poisson_p(lambda_away, i) for i in range(max_goals + 1)]
    matrix = [[ph[i] * pa[j] for j in range(max_goals + 1)] for i in range(max_goals + 1)]
    total = sum(sum(row) for row in matrix)
    if not finite_positive(total):
        raise InvalidData("Invalid Poisson probability matrix")
    return [[v / total for v in row] for row in matrix]


def model_from_form(home: dict, away: dict) -> dict:
    # No fixed lambda. Every lambda is derived from real observed goals.
    h_attack = 0.65 * home["last5_gf"] + 0.35 * home["last10_gf"]
    h_def = 0.65 * home["last5_ga"] + 0.35 * home["last10_ga"]
    a_attack = 0.65 * away["last5_gf"] + 0.35 * away["last10_gf"]
    a_def = 0.65 * away["last5_ga"] + 0.35 * away["last10_ga"]

    # Venue-specific values are used only when the source has observations.
    home_attack = home["home_gf"] if home["home_gf"] is not None else h_attack
    home_def = home["home_ga"] if home["home_ga"] is not None else h_def
    away_attack = away["away_gf"] if away["away_gf"] is not None else a_attack
    away_def = away["away_ga"] if away["away_ga"] is not None else a_def

    lambda_home = max(0.15, min(4.5, 0.55 * home_attack + 0.45 * away_def))
    lambda_away = max(0.15, min(4.5, 0.55 * away_attack + 0.45 * home_def))
    matrix = poisson_matrix(lambda_home, lambda_away)
    home_p = sum(matrix[i][j] for i in range(11) for j in range(11) if i > j)
    draw_p = sum(matrix[i][i] for i in range(11))
    away_p = sum(matrix[i][j] for i in range(11) for j in range(11) if i < j)
    over25 = sum(matrix[i][j] for i in range(11) for j in range(11) if i + j >= 3)
    btts = sum(matrix[i][j] for i in range(1, 11) for j in range(1, 11))

    seed_material = json.dumps({"h": home, "a": away, "model": MODEL_VERSION}, sort_keys=True, default=str).encode()
    input_hash = hashlib.sha256(seed_material).hexdigest()
    seed = int(input_hash[:16], 16)
    return {
        "lambda_home": round(lambda_home, 6), "lambda_away": round(lambda_away, 6),
        "prob_home": round(home_p * 100, 4), "prob_draw": round(draw_p * 100, 4),
        "prob_away": round(away_p * 100, 4), "prob_over_2_5": round(over25 * 100, 4),
        "prob_btts": round(btts * 100, 4), "input_hash": input_hash, "seed": seed,
        "home_form": home, "away_form": away,
        "data_quality": min(10, int(home["sample"] / 2) + int(away["sample"] / 2)),
    }


def monte_carlo(lambda_home: float, lambda_away: float, seed: int, simulations: int = 50000) -> dict:
    if simulations < 1000 or simulations > 500000:
        raise InvalidData("simulations must be between 1000 and 500000")
    if not (finite_positive(lambda_home) and finite_positive(lambda_away)):
        raise InvalidData("Monte Carlo requires real model lambdas")
    rng = random.Random(seed)
    home_w = draw = away_w = over25 = btts = 0
    for _ in range(simulations):
        # Inverse-CDF Poisson sampler; no numpy dependency required.
        def sample(lam: float) -> int:
            limit = math.exp(-lam)
            k, p = 0, 1.0
            while p > limit and k < 20:
                k += 1
                p *= rng.random()
            return k - 1
        hg, ag = sample(lambda_home), sample(lambda_away)
        if hg > ag: home_w += 1
        elif hg == ag: draw += 1
        else: away_w += 1
        if hg + ag >= 3: over25 += 1
        if hg >= 1 and ag >= 1: btts += 1
    return {
        "simulations": simulations, "seed": seed,
        "prob_home": round(home_w / simulations * 100, 4),
        "prob_draw": round(draw / simulations * 100, 4),
        "prob_away": round(away_w / simulations * 100, 4),
        "prob_over_2_5": round(over25 / simulations * 100, 4),
        "prob_btts": round(btts / simulations * 100, 4),
    }


def true_ev(probability_pct: float, odds: float) -> float:
    if not (finite_positive(probability_pct) and finite_positive(odds, 1.0)):
        raise InvalidData("EV requires real probability and odds")
    return round(((probability_pct / 100.0) * odds - 1.0) * 100.0, 4)


def fractional_kelly(probability_pct: float, odds: float, bankroll: float, fraction: float = DEFAULT_FRACTIONAL_KELLY) -> Tuple[float, float]:
    if not (finite_positive(probability_pct) and finite_positive(odds, 1.0) and finite_positive(bankroll) and finite_positive(fraction)):
        return 0.0, 0.0
    p = max(0.0, min(1.0, probability_pct / 100.0))
    b = odds - 1.0
    raw = ((b * p) - (1 - p)) / b
    safe = max(0.0, min(MAX_KELLY_PCT / 100.0, raw * fraction))
    return round(safe * 100, 4), round(bankroll * safe, 2)


def market_candidates(model: dict, odds: dict) -> List[dict]:
    pairs = [
        ("Home Win", model["prob_home"], odds.get("home_odds")),
        ("Draw", model["prob_draw"], odds.get("draw_odds")),
        ("Away Win", model["prob_away"], odds.get("away_odds")),
        ("Over 2.5", model["prob_over_2_5"], odds.get("over_2_5_odds")),
    ]
    out = []
    for market, prob, odd in pairs:
        if not finite_positive(odd, 1.0):
            continue
        ev = true_ev(prob, float(odd))
        out.append({"market": market, "probability": prob, "odds": float(odd), "ev": ev})
    return out


# --------------------- Eligibility gate ---------------------

def analyze_event(event: dict, bankroll: float = 0.0, simulations: int = 50000) -> dict:
    e = validate_event(event)
    odds_payload = get_event_odds(e["sport_key"], e["event_id"])
    odds = extract_real_odds(e, odds_payload)
    home = get_team_form(e["home"])
    away = get_team_form(e["away"])
    model = model_from_form(home, away)
    mc = monte_carlo(model["lambda_home"], model["lambda_away"], model["seed"], simulations)
    markets = market_candidates(model, odds)
    if not markets:
        raise Ineligible("No validated market with real bookmaker odds")
    positive = [m for m in markets if m["ev"] > 0]
    if not positive:
        return {
            "status": "NO_VALUE", "event": e, "odds": odds, "model": model,
            "monte_carlo": mc, "markets": markets, "pick": None,
        }
    pick = max(positive, key=lambda x: x["ev"])
    kelly_pct, stake = fractional_kelly(pick["probability"], pick["odds"], bankroll) if bankroll > 0 else (0.0, 0.0)
    return {
        "status": "OK", "event": e, "odds": odds, "model": model,
        "monte_carlo": mc, "markets": markets,
        "pick": {**pick, "kelly_pct": kelly_pct, "stake": stake},
    }


def discover_events(limit: int = 12) -> List[dict]:
    sports = get_soccer_sports()
    events: List[dict] = []
    seen = set()
    for sport in sports:
        try:
            raw_events = get_events(sport["key"])
        except RateLimited:
            raise
        except QuantError as exc:
            log.warning("Skipping sport %s: %s", sport.get("key"), exc)
            continue
        for raw in raw_events:
            raw = {**raw, "sport_key": sport.get("key"), "sport_title": sport.get("title")}
            try:
                ev = validate_event(raw)
            except QuantError:
                continue
            if ev["event_id"] in seen:
                continue
            seen.add(ev["event_id"])
            events.append(ev)
    events.sort(key=lambda x: x["commence_iso"])
    return events[:limit]


def build_radar(limit: int = 12, top_n: int = 5, bankroll: float = 0.0) -> dict:
    # No result cache here. Each search starts from current event/odds snapshots.
    events = discover_events(limit)
    results: List[dict] = []
    diagnostics = {"events_seen": len(events), "no_odds": 0, "no_form": 0, "no_value": 0, "errors": 0}
    for event in events:
        try:
            result = analyze_event(event, bankroll=bankroll, simulations=20000)
        except RateLimited:
            raise
        except (NoData, Ineligible) as exc:
            if "odds" in str(exc).lower(): diagnostics["no_odds"] += 1
            else: diagnostics["no_form"] += 1
            continue
        except QuantError as exc:
            diagnostics["errors"] += 1
            log.warning("Event %s rejected: %s", event["event_id"], exc)
            continue
        if result["status"] == "NO_VALUE":
            diagnostics["no_value"] += 1
            continue
        results.append(result)
    results.sort(key=lambda x: x["pick"]["ev"], reverse=True)
    return {"status": "OK" if results else "NO_CANDIDATES", "candidates": results[:top_n], "diagnostics": diagnostics}


# ---------------------- Persistence -------------------------

def save_bet(user_id: str, result: dict) -> int:
    pick = result["pick"]
    event = result["event"]
    model = result["model"]
    input_hash = model["input_hash"]
    model_inputs = json.dumps({
        "lambda_home": model["lambda_home"], "lambda_away": model["lambda_away"],
        "probability": pick["probability"], "mc": result["monte_carlo"],
        "input_hash": input_hash,
    }, sort_keys=True)
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO match_backtest
            (user_id,event_id,match_name,league,market,selection,model_probability,odds,entry_odds,ev,stake,
             model_version,odds_source,input_hash,model_inputs,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, event["event_id"], f'{event["home"]} vs {event["away"]}', event["league"],
             pick["market"], pick["market"], pick["probability"], pick["odds"], pick["odds"],
             pick["ev"], pick.get("stake", 0), MODEL_VERSION, result["odds"]["source"], input_hash,
             model_inputs, iso_now()),
        )
        return int(cur.lastrowid)


# ---------------------- Gemini explanation ------------------

def explain_with_gemini(result: dict) -> Optional[str]:
    # Gemini receives already-computed numbers only. It cannot create a pick.
    if not gemini_client or not genai_types:
        return None
    safe_payload = {
        "event": result["event"], "pick": result["pick"],
        "model": {k: result["model"][k] for k in ("lambda_home", "lambda_away", "prob_home", "prob_draw", "prob_away", "prob_over_2_5", "data_quality")},
        "odds": result["odds"], "mc": result["monte_carlo"],
    }
    prompt = json.dumps(safe_payload, ensure_ascii=False)
    try:
        cfg = genai_types.GenerateContentConfig(
            temperature=0.1,
            system_instruction=("Explain only the supplied quantitative result. "
                                "Never invent odds, probabilities, injuries, lineups, RLM, form or events. "
                                "Python is the sole source of numeric truth."),
        )
        response = gemini_client.models.generate_content(model="gemini-3.5-flash-lite", contents=prompt, config=cfg)
        return response.text if response and response.text else None
    except Exception as exc:
        log.warning("Gemini explanation unavailable: %s", exc)
        return None


# -------------------------- Flask ----------------------------

if Flask is not None:
    app = Flask(__name__)
else:  # pragma: no cover
    app = None


def user_id_from_request() -> str:
    if request is None:
        return "guest_user"
    return str(request.headers.get("X-User-ID") or "guest_user")[:100]


def auth_required() -> Optional[tuple]:
    if request is None:
        return None
    # Authentication is optional when no license key exists; deployment can enforce it.
    required = os.getenv("REQUIRE_LICENSE", "0") == "1"
    if not required:
        return None
    key = request.headers.get("X-Access-Key") or request.args.get("access_key", "")
    if not access_ok(key, user_id_from_request(), bind=True):
        return jsonify({"status": "UNAUTHORIZED", "message": "License key không hợp lệ hoặc đã hết hạn."}), 401
    return None


if app:
    @app.get("/health")
    def health():
        return jsonify({
            "status": "OK", "version": APP_VERSION, "model_version": MODEL_VERSION,
            "odds_api_configured": bool(ODDS_API_KEY),
            "telegram_configured": bool(bot), "gemini_configured": bool(gemini_client),
        })

    @app.get("/")
    def root():
        return jsonify({"service": "Quant Terminal", "version": APP_VERSION, "status": "OK"})

    @app.get("/api/auto-radar")
    def api_auto_radar():
        denied = auth_required()
        if denied: return denied
        try:
            result = build_radar(limit=min(int(request.args.get("limit", 12)), 30), top_n=5)
            if result["status"] == "NO_CANDIDATES":
                return jsonify({
                    **result,
                    "message": "Chưa có trận vừa đủ dữ liệu odds + form để tính EV. Hệ thống không bịa kèo."
                })
            return jsonify(result)
        except RateLimited as exc:
            return jsonify({"status": exc.code, "message": str(exc)}), 429
        except QuantError as exc:
            return jsonify({"status": exc.code, "message": str(exc)}), 503
        except Exception as exc:
            log.exception("radar failure")
            return jsonify({"status": "INTERNAL_ERROR", "message": str(exc)}), 500

    @app.post("/api/analyze-pre")
    def api_analyze_pre():
        denied = auth_required()
        if denied: return denied
        data = request.get_json(silent=True) or {}
        event_id = str(data.get("event_id") or "").strip()
        bankroll = float(data.get("bankroll") or 0)
        if not event_id:
            return jsonify({"status": "INVALID_REQUEST", "message": "event_id is required"}), 400
        sport_key = str(data.get("sport_key") or "").strip()
        if not sport_key:
            return jsonify({"status": "INVALID_REQUEST", "message": "sport_key is required"}), 400
        try:
            # Re-fetch event identity from the same provider before analyzing.
            events = get_events(sport_key)
            raw = next((e for e in events if str(e.get("id")) == event_id), None)
            if not raw:
                raise NoData("Event ID not found in current provider snapshot")
            raw["sport_key"] = sport_key
            raw["sport_title"] = data.get("league") or sport_key
            result = analyze_event(raw, bankroll=bankroll)
            bet_id = save_bet(user_id_from_request(), result) if result.get("pick") else None
            explanation = explain_with_gemini(result)
            result["bet_id"] = bet_id
            result["explanation"] = explanation
            return jsonify(result)
        except RateLimited as exc:
            return jsonify({"status": exc.code, "message": str(exc)}), 429
        except QuantError as exc:
            return jsonify({"status": exc.code, "message": str(exc)}), 503
        except Exception as exc:
            log.exception("pre-match analysis failure")
            return jsonify({"status": "INTERNAL_ERROR", "message": str(exc)}), 500

    @app.get("/api/history")
    def api_history():
        denied = auth_required()
        if denied: return denied
        uid = user_id_from_request()
        with db() as conn:
            rows = conn.execute("SELECT * FROM match_backtest WHERE user_id=? ORDER BY id DESC LIMIT 100", (uid,)).fetchall()
        return jsonify([dict(r) for r in rows])


def run() -> None:
    if app is None:
        raise SystemExit("Flask is not installed. Run: pip install -r requirements.txt")
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    run()
