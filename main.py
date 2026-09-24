from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

try:
    from flask import Flask, jsonify, request
except Exception:
    Flask = None
    jsonify = None
    request = None

try:
    import requests
except Exception:
    requests = None

try:
    from google import genai
    from google.genai import types as genai_types
except Exception:
    genai = None
    genai_types = None

APP_VERSION = "quant-terminal-2026.1.0"
MODEL_VERSION = "poisson-form-montecarlo-v4"
VN = ZoneInfo("Asia/Ho_Chi_Minh")
UTC = timezone.utc
DB_FILE = os.getenv("DB_FILE", "/var/data/database.db")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "").strip()
RAPIDAPI_HOST = "google-search74.p.rapidapi.com"
RAPIDAPI_BASE = "https://google-search74.p.rapidapi.com/"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
REQUIRE_LICENSE = os.getenv("REQUIRE_LICENSE", "1").lower() in {"1", "true", "yes", "on"}
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))
SEARCH_LIMIT = int(os.getenv("SEARCH_LIMIT", "10"))
MAX_SEARCH_PAGES = int(os.getenv("MAX_SEARCH_PAGES", "3"))
MAX_SOURCE_PAGES = int(os.getenv("MAX_SOURCE_PAGES", "8"))
MAX_KELLY_PCT = 2.0
DEFAULT_FRACTIONAL_KELLY = 0.25

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("quant-terminal")
_cache: Dict[str, Tuple[float, int, Any]] = {}
_cache_lock = RLock()

if Flask is not None:
    app = Flask(__name__)
else:
    app = None

if genai and GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as exc:
        log.warning("Gemini init failed: %s", exc)
        gemini_client = None
else:
    gemini_client = None


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
class AuthError(QuantError):
    code = "UNAUTHORIZED"


def now_utc() -> datetime:
    return datetime.now(UTC)

def now_vn() -> datetime:
    return now_utc().astimezone(VN)

def iso_now() -> str:
    return now_utc().isoformat()

def parse_dt(value: str) -> datetime:
    s = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

def normalize_name(value: Any) -> str:
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

def hash_key(value: str) -> str:
    return hashlib.sha256(value.strip().encode()).hexdigest()

def make_key() -> str:
    return "QT-" + secrets.token_urlsafe(24).replace("-", "_")

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
    if value is None or value == [] or value == {}:
        return value
    with _cache_lock:
        _cache[key] = (time.monotonic(), ttl, value)
    return value


# --------------------------- Database ---------------------------

def _is_pg() -> bool:
    return DATABASE_URL.startswith(("postgres://", "postgresql://"))

def db():
    if _is_pg():
        try:
            import psycopg
            conn = psycopg.connect(DATABASE_URL, connect_timeout=10)
            conn.row_factory = psycopg.rows.dict_row
            return conn
        except Exception as exc:
            raise UpstreamError(f"PostgreSQL connection failed: {exc}") from exc
    os.makedirs(os.path.dirname(DB_FILE) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn

def db_exec(conn, sql: str, params=()):
    return conn.execute(sql, params)

def db_placeholder() -> str:
    return "%s" if _is_pg() else "?"

def init_db() -> None:
    p = db_placeholder()
    with db() as conn:
        if _is_pg():
            statements = [
                """CREATE TABLE IF NOT EXISTS access_keys (id BIGSERIAL PRIMARY KEY, key_hash TEXT UNIQUE NOT NULL, note TEXT, status TEXT NOT NULL DEFAULT 'ACTIVE', created_at TEXT NOT NULL, expires_at TEXT, max_uses BIGINT NOT NULL DEFAULT 0, used_count BIGINT NOT NULL DEFAULT 0, bound_user_id TEXT UNIQUE, last_used_at TEXT)""",
                """CREATE TABLE IF NOT EXISTS usage_logs (id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL, key_hash TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, metadata TEXT)""",
                """CREATE TABLE IF NOT EXISTS analyses (id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL, event_key TEXT, team_a TEXT NOT NULL, team_b TEXT NOT NULL, competition TEXT, kickoff TEXT, status TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS watchlist (id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL, analysis_id BIGINT, event_key TEXT, status TEXT NOT NULL DEFAULT 'WATCHING', note TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS bet_tracking (id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL, analysis_id BIGINT, event_key TEXT, market TEXT, selection TEXT, entry_odds DOUBLE PRECISION, status TEXT NOT NULL DEFAULT 'PENDING', live_state_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS odds_snapshots (id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL, analysis_id BIGINT, event_key TEXT, market TEXT, line TEXT, odds DOUBLE PRECISION, source TEXT, retrieved_at TEXT NOT NULL)""",
            ]
        else:
            statements = [
                """CREATE TABLE IF NOT EXISTS access_keys (id INTEGER PRIMARY KEY AUTOINCREMENT, key_hash TEXT UNIQUE NOT NULL, note TEXT, status TEXT NOT NULL DEFAULT 'ACTIVE', created_at TEXT NOT NULL, expires_at TEXT, max_uses INTEGER NOT NULL DEFAULT 0, used_count INTEGER NOT NULL DEFAULT 0, bound_user_id TEXT UNIQUE, last_used_at TEXT)""",
                """CREATE TABLE IF NOT EXISTS usage_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, key_hash TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, metadata TEXT)""",
                """CREATE TABLE IF NOT EXISTS analyses (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, event_key TEXT, team_a TEXT NOT NULL, team_b TEXT NOT NULL, competition TEXT, kickoff TEXT, status TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS watchlist (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, analysis_id INTEGER, event_key TEXT, status TEXT NOT NULL DEFAULT 'WATCHING', note TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS bet_tracking (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, analysis_id INTEGER, event_key TEXT, market TEXT, selection TEXT, entry_odds REAL, status TEXT NOT NULL DEFAULT 'PENDING', live_state_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS odds_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, analysis_id INTEGER, event_key TEXT, market TEXT, line TEXT, odds REAL, source TEXT, retrieved_at TEXT NOT NULL)""",
            ]
        for sql in statements:
            conn.execute(sql)
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_logs(user_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_analysis_user ON analyses(user_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_watch_user ON watchlist(user_id, updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_bet_user ON bet_tracking(user_id, updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_odds_event ON odds_snapshots(event_key, retrieved_at)",
        ]
        for sql in indexes:
            try: conn.execute(sql)
            except Exception: pass
        conn.commit()

init_db()


def rowdict(row):
    return dict(row) if row is not None else None

def log_usage(user: dict, action: str, status: str, metadata: Optional[dict] = None):
    p = db_placeholder()
    with db() as conn:
        conn.execute(f"INSERT INTO usage_logs(user_id,key_hash,action,status,created_at,metadata) VALUES ({p},{p},{p},{p},{p},{p})",
                     (user["user_id"], user["key_hash"], action, status, iso_now(), json.dumps(metadata or {}, ensure_ascii=False)))
        conn.commit()


def authenticate(raw_key: str, consume: bool = False) -> dict:
    if not raw_key:
        raise AuthError("Access key is required")
    kh = hash_key(raw_key)
    p = db_placeholder()
    with db() as conn:
        row = conn.execute(f"SELECT * FROM access_keys WHERE key_hash={p} AND status='ACTIVE'", (kh,)).fetchone()
        if not row:
            raise AuthError("Invalid or inactive access key")
        d = rowdict(row)
        if d.get("expires_at"):
            try:
                if parse_dt(d["expires_at"]) <= now_utc():
                    raise AuthError("Access key expired")
            except ValueError:
                raise AuthError("Access key has invalid expiry")
        max_uses = int(d.get("max_uses") or 0)
        used = int(d.get("used_count") or 0)
        if max_uses > 0 and used >= max_uses:
            raise AuthError("Access key usage limit reached")
        user_id = d.get("bound_user_id") or ("user_" + kh[:16])
        if not d.get("bound_user_id"):
            conn.execute(f"UPDATE access_keys SET bound_user_id={p} WHERE key_hash={p}", (user_id, kh))
            d["bound_user_id"] = user_id
        if consume:
            conn.execute(f"UPDATE access_keys SET used_count=used_count+1,last_used_at={p},bound_user_id={p} WHERE key_hash={p}", (iso_now(), user_id, kh))
        conn.commit()
    d["user_id"] = user_id
    d["key_hash"] = kh
    return d


def admin_auth() -> None:
    if not ADMIN_TOKEN:
        raise AuthError("ADMIN_TOKEN is not configured")
    supplied = (request.headers.get("X-Admin-Token") if request else None) or (request.args.get("admin_token", "") if request else "")
    if not supplied or not secrets.compare_digest(supplied, ADMIN_TOKEN):
        raise AuthError("Admin authentication failed")


def request_key() -> str:
    return (request.headers.get("X-Access-Key") if request else "") or (request.args.get("access_key", "") if request else "")


def current_user(consume: bool = False) -> dict:
    if not REQUIRE_LICENSE:
        # Development mode still gets isolated data per explicit key; without a key use a local dev user.
        key = request_key()
        if key:
            return authenticate(key, consume=consume)
        return {"user_id": "dev_user", "key_hash": "dev", "max_uses": 0, "used_count": 0}
    return authenticate(request_key(), consume=consume)


# --------------------------- HTTP / Search ---------------------------

def http_get(url: str, params=None, headers=None, timeout=HTTP_TIMEOUT) -> requests.Response:
    if requests is None:
        raise UpstreamError("requests is not installed")
    try:
        r = requests.get(url, params=params, headers=headers or {}, timeout=timeout, allow_redirects=True)
    except Exception as exc:
        raise UpstreamError(str(exc)) from exc
    if r.status_code == 429:
        raise RateLimited(f"HTTP 429 from {url}")
    if r.status_code >= 500:
        raise UpstreamError(f"HTTP {r.status_code} from {url}")
    return r


def rapid_search(query: str, limit: int = SEARCH_LIMIT, max_pages: int = MAX_SEARCH_PAGES) -> dict:
    if not RAPIDAPI_KEY:
        raise UpstreamError("RAPIDAPI_KEY is not configured")
    limit = max(1, min(int(limit), 50))
    all_results: List[dict] = []
    cursor = None
    pages = 0
    while pages < max_pages:
        params = {"query": query, "limit": str(limit), "related_keywords": "false"}
        if cursor:
            params["cursor"] = cursor
        key = "search:" + hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        cached = cache_get(key)
        if cached is not None:
            data = cached
        else:
            r = http_get(RAPIDAPI_BASE, params=params, headers={"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": RAPIDAPI_HOST})
            if r.status_code in (401, 403):
                raise UpstreamError(f"RapidAPI auth/subscription error HTTP {r.status_code}")
            if r.status_code >= 400:
                raise UpstreamError(f"RapidAPI HTTP {r.status_code}: {r.text[:300]}")
            try: data = r.json()
            except Exception as exc: raise UpstreamError("RapidAPI returned invalid JSON") from exc
            cache_set(key, data, 90)
        results = data.get("results") or []
        for i, item in enumerate(results, start=1):
            all_results.append({"rank": i, "url": item.get("url"), "title": item.get("title"), "description": item.get("description"), "timestamp": item.get("timestamp")})
        pages += 1
        cursor = data.get("next_cursor")
        if not cursor or not results:
            break
    if not all_results:
        raise NoData(f"No Google results for query: {query}")
    return {"query": query, "results": all_results, "pages": pages}


def fetch_source(url: str) -> Optional[dict]:
    if not url or not url.startswith(("http://", "https://")):
        return None
    key = "page:" + hashlib.sha256(url.encode()).hexdigest()
    cached = cache_get(key)
    if cached is not None:
        return cached
    try:
        r = http_get(url, headers={"User-Agent": "Mozilla/5.0 QuantTerminal/2026"}, timeout=12)
        if r.status_code >= 400:
            return None
        text = r.text or ""
        # Keep a bounded text representation; Gemini receives snippets plus this extract.
        title = ""
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        if m: title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()
        clean = re.sub(r"<script.*?</script>|<style.*?</style>|<noscript.*?</noscript>", " ", text, flags=re.I | re.S)
        clean = re.sub(r"<[^>]+>", " ", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        data = {"url": url, "title": title, "text": clean[:18000]}
        return cache_set(key, data, 180)
    except Exception:
        return None


def collect_research_queries(team_a: str, team_b: str, competition: str, date: str) -> List[str]:
    pair = f'"{team_a}" "{team_b}"'
    base = f'{pair} "{competition}" {date}'.strip()
    return [
        base,
        f'{base} lineup injuries suspension',
        f'{base} predicted lineup team news',
        f'{base} xG xGA stats',
        f'{base} odds Asian handicap over under',
        f'{base} corners cards BTTS',
        f'{base} recent form last 5 last 10',
        f'{base} head to head H2H',
        f'{base} site:fbref.com',
        f'{base} site:sofascore.com',
        f'{base} site:fotmob.com',
        f'{base} site:understat.com',
    ]


def build_evidence(team_a: str, team_b: str, competition: str, date: str) -> dict:
    sources = []
    errors = []
    queries = collect_research_queries(team_a, team_b, competition, date)
    seen = set()
    for q in queries:
        try:
            data = rapid_search(q)
            for r in data["results"]:
                if not r.get("url") or r["url"] in seen:
                    continue
                seen.add(r["url"])
                item = {"url": r["url"], "title": r.get("title"), "description": r.get("description"), "timestamp": r.get("timestamp"), "query": q}
                page = fetch_source(r["url"])
                if page:
                    item["page_text"] = page["text"]
                sources.append(item)
                if len(sources) >= MAX_SOURCE_PAGES:
                    break
        except QuantError as exc:
            errors.append({"query": q, "status": exc.code, "message": str(exc)})
        if len(sources) >= MAX_SOURCE_PAGES:
            break
    if not sources:
        raise NoData("No usable web sources were retrieved")
    return {"queries": queries, "sources": sources, "errors": errors, "retrieved_at": iso_now()}


# --------------------------- Match validation / Gemini ---------------------------

def validate_future_kickoff(date: str, kickoff: str) -> datetime:
    try:
        local = datetime.fromisoformat(f"{date}T{kickoff}").replace(tzinfo=VN)
    except ValueError as exc:
        raise InvalidData("date/kickoff must be valid; kickoff is GMT+7") from exc
    dt = local.astimezone(UTC)
    if dt <= now_utc():
        raise Ineligible("Fixture is already live or in the past")
    return dt


def extract_json(text: str) -> dict:
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
    try: return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m: raise InvalidData("Gemini did not return valid JSON")
        return json.loads(m.group(0))


def gemini_research(team_a: str, team_b: str, competition: str, date: str, kickoff: str, evidence: dict) -> dict:
    if not gemini_client or not genai_types:
        raise UpstreamError("GEMINI_API_KEY is not configured")
    compact_sources = []
    for s in evidence["sources"]:
        compact_sources.append({"url": s.get("url"), "title": s.get("title"), "description": s.get("description"), "page_text": (s.get("page_text") or "")[:7000]})
    schema = {
        "match_identity": {"team_a": team_a, "team_b": team_b, "competition": competition, "kickoff": kickoff, "verified": False, "evidence_urls": []},
        "form": {"team_a_last5": [], "team_a_last10": [], "team_b_last5": [], "team_b_last10": []},
        "home_away": {}, "h2h": [], "injuries": [], "suspensions": [], "expected_xi": {}, "team_news": [],
        "stats": {"xg": {}, "xga": {}, "corners": {}, "cards": {}, "btts": {}, "ou25": {}, "asian_handicap": {}},
        "odds_snapshots": [], "conflicts": [], "data_quality": "INSUFFICIENT"
    }
    prompt = f"""You are the evidence extraction layer of a football data pipeline. Return ONLY JSON matching this structure: {json.dumps(schema, ensure_ascii=False)}.
Rules: use ONLY facts explicitly supported by the supplied sources. Never invent values. Every numeric fact must include source_url and retrieved_at where possible. Exact match identity must match BOTH teams, competition, and future kickoff. If not verified, set match_identity.verified=false. If a value is unavailable, use null or []. Odds snapshots require a source URL and timestamp/date; never manufacture movement. Conflicts must list both competing values and source URLs. Do not calculate EV, Kelly, lambda, model probabilities, confidence, or pick. The deterministic backend will calculate those.
MATCH INPUT: {team_a} vs {team_b}; competition={competition}; date={date}; kickoff={kickoff}.
SOURCES: {json.dumps(compact_sources, ensure_ascii=False)}"""
    cfg = genai_types.GenerateContentConfig(temperature=0, max_output_tokens=12000, response_mime_type="application/json", system_instruction="Strict source-grounded extraction. No fabrication. No numeric modeling.")
    try:
        response = gemini_client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=cfg)
        data = extract_json(response.text if response else "")
    except Exception as exc:
        raise UpstreamError(f"Gemini research failed: {exc}") from exc
    mi = data.get("match_identity") or {}
    if not mi.get("verified"):
        raise InvalidData("Exact match identity was not verified by source evidence")
    if not same_team(mi.get("team_a"), team_a) or not same_team(mi.get("team_b"), team_b):
        raise InvalidData("Gemini returned a different team identity")
    return data


def evidence_number(x: Any) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def derive_lambdas(research: dict) -> Tuple[float, float]:
    # Derive from explicitly supplied recent/home-away goal evidence. No fixed team lambda constants.
    def avg_goals(arr, key):
        vals=[]
        for m in arr or []:
            v = evidence_number(m.get(key) if isinstance(m, dict) else None)
            if v is not None: vals.append(v)
        return sum(vals)/len(vals) if vals else None
    f = research.get("form") or {}
    a_for = avg_goals(f.get("team_a_last10"), "goals_for")
    a_against = avg_goals(f.get("team_a_last10"), "goals_against")
    b_for = avg_goals(f.get("team_b_last10"), "goals_for")
    b_against = avg_goals(f.get("team_b_last10"), "goals_against")
    if None in (a_for, a_against, b_for, b_against):
        raise NoData("Insufficient verified goal-form data to derive model inputs")
    lh = max(0.05, min(4.5, (a_for * 0.60 + b_against * 0.40)))
    la = max(0.05, min(4.5, (b_for * 0.60 + a_against * 0.40)))
    return lh, la


def poisson_p(lmbda: float, goals: int) -> float:
    return math.exp(-lmbda) * (lmbda ** goals) / math.factorial(goals)

def poisson_matrix(lambda_home: float, lambda_away: float, max_goals: int = 10) -> List[List[float]]:
    m = [[poisson_p(lambda_home, i) * poisson_p(lambda_away, j) for j in range(max_goals+1)] for i in range(max_goals+1)]
    s = sum(map(sum, m))
    return [[x/s for x in row] for row in m]

def model_probs(lambda_home: float, lambda_away: float) -> dict:
    mat = poisson_matrix(lambda_home, lambda_away)
    ph = sum(mat[i][j] for i in range(11) for j in range(11) if i>j)
    pd = sum(mat[i][j] for i in range(11) for j in range(11) if i==j)
    pa = sum(mat[i][j] for i in range(11) for j in range(11) if i<j)
    po25 = sum(mat[i][j] for i in range(11) for j in range(11) if i+j>=3)
    pbtts = sum(mat[i][j] for i in range(1,11) for j in range(1,11))
    return {"prob_home":ph*100,"prob_draw":pd*100,"prob_away":pa*100,"prob_over_2_5":po25*100,"prob_btts_yes":pbtts*100}

def monte_carlo(lambda_home: float, lambda_away: float, seed: int, simulations: int = 20000) -> dict:
    rng = random.Random(seed)
    wh=dr=wa=0
    for _ in range(simulations):
        def pois(l):
            L=math.exp(-l); k=0; p=1.0
            while p>L:
                k+=1; p*=rng.random()
            return k-1
        a,b=pois(lambda_home),pois(lambda_away)
        if a>b: wh+=1
        elif a==b: dr+=1
        else: wa+=1
    return {"simulations":simulations,"prob_home":wh/simulations*100,"prob_draw":dr/simulations*100,"prob_away":wa/simulations*100}

def true_ev(probability_pct: float, odds: float) -> float:
    return (probability_pct/100.0*odds-1.0)*100.0

def fractional_kelly(probability_pct: float, odds: float, bankroll: float, fraction: float=DEFAULT_FRACTIONAL_KELLY) -> Tuple[float,float]:
    p=probability_pct/100; b=odds-1
    if b<=0: return 0.0,0.0
    k=max(0.0,(p*odds-1)/b)*fraction
    k=min(k,MAX_KELLY_PCT/100)
    return k*100.0, bankroll*k


def extract_verified_odds(research: dict) -> List[dict]:
    out=[]
    for s in research.get("odds_snapshots") or []:
        try:
            odds=float(s.get("odds"));
            if odds<=1: continue
            if not s.get("source_url") or not s.get("timestamp"): continue
            out.append({**s,"odds":odds})
        except Exception: continue
    return out


def quant_engine(team_a: str, team_b: str, research: dict, bankroll: float=0.0) -> dict:
    lh,la=derive_lambdas(research)
    probs=model_probs(lh,la)
    seed=int(hashlib.sha256(f"{team_a}|{team_b}|{research.get('match_identity',{}).get('kickoff')}".encode()).hexdigest()[:8],16)
    mc=monte_carlo(lh,la,seed,20000)
    odds=extract_verified_odds(research)
    candidates=[]
    for o in odds:
        market=str(o.get("market") or "").lower()
        sel=str(o.get("selection") or "")
        if market in {"1x2","match result","moneyline"}:
            p = probs["prob_home"] if same_team(sel,team_a) else probs["prob_away"] if same_team(sel,team_b) else probs["prob_draw"] if normalize_name(sel)=="draw" else None
            if p is not None: candidates.append({"market":"1X2","selection":sel,"odds":o["odds"],"probability":p,"source":o.get("source_url"),"timestamp":o.get("timestamp")})
        elif market in {"over 2.5","o2.5","over/under 2.5","total 2.5"} and normalize_name(sel) in {"over","over 2 5","o 2 5"}:
            candidates.append({"market":"Over 2.5","selection":sel,"odds":o["odds"],"probability":probs["prob_over_2_5"],"source":o.get("source_url"),"timestamp":o.get("timestamp")})
    for c in candidates:
        c["market_probability"]=100.0/c["odds"]
        c["ev"]=true_ev(c["probability"],c["odds"])
        c["kelly_pct"],c["stake"]=fractional_kelly(c["probability"],c["odds"],bankroll)
    candidates=[c for c in candidates if c["ev"]>0]
    candidates.sort(key=lambda x:x["ev"],reverse=True)
    return {"lambda_home":lh,"lambda_away":la,**probs,"monte_carlo":mc,"candidates":candidates,"input_hash":hashlib.sha256(json.dumps(research,sort_keys=True).encode()).hexdigest(),"data_quality":"VERIFIED" if candidates else "INSUFFICIENT_FOR_VALUE"}


def no_bet_engine(research: dict, quant: dict) -> dict:
    conflicts=research.get("conflicts") or []
    snapshots=extract_verified_odds(research)
    if conflicts: return {"status":"NO_BET","reason":"DATA_CONFLICT"}
    if len(snapshots)<1: return {"status":"NO_BET","reason":"ODDS_SNAPSHOT_INSUFFICIENT"}
    if not quant.get("candidates"): return {"status":"NO_BET","reason":"NO_POSITIVE_VERIFIED_EV"}
    return {"status":"VALID_BET","reason":"VERIFIED_POSITIVE_EV"}


def analyze_match(team_a: str, team_b: str, competition: str, date: str, kickoff: str, bankroll: float, user: dict) -> dict:
    validate_future_kickoff(date, kickoff)
    evidence=build_evidence(team_a,team_b,competition,date)
    research=gemini_research(team_a,team_b,competition,date,kickoff,evidence)
    quant=quant_engine(team_a,team_b,research,bankroll)
    gate=no_bet_engine(research,quant)
    primary=quant["candidates"][0] if quant["candidates"] else None
    confidence=(primary["probability"] if primary else 0.0)
    result={"status":gate["status"],"no_bet":gate if gate["status"]=="NO_BET" else None,"match":{"team_a":team_a,"team_b":team_b,"competition":competition,"date":date,"kickoff":kickoff},"primary_pick":primary,"confidence":confidence,"model":quant,"research":research,"sources":evidence["sources"],"retrieved_at":evidence["retrieved_at"]}
    p=db_placeholder()
    with db() as conn:
        sql=f"INSERT INTO analyses(user_id,event_key,team_a,team_b,competition,kickoff,status,result_json,created_at) VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p})"
        params=(user["user_id"],hashlib.sha256(f"{team_a}|{team_b}|{kickoff}".encode()).hexdigest()[:24],team_a,team_b,competition,kickoff,result["status"],json.dumps(result,ensure_ascii=False),iso_now())
        if _is_pg():
            cur=conn.execute(sql + " RETURNING id", params)
            analysis_id=cur.fetchone()["id"]
        else:
            cur=conn.execute(sql, params)
            analysis_id=cur.lastrowid
        if primary:
            conn.execute(f"INSERT INTO bet_tracking(user_id,analysis_id,event_key,market,selection,entry_odds,status,live_state_json,created_at,updated_at) VALUES ({p},{p},{p},{p},{p},{p},'PENDING',{p},{p},{p})",(user["user_id"],analysis_id,result["match"]["competition"]+":"+team_a+":"+team_b+":"+kickoff,primary["market"],primary["selection"],primary["odds"],json.dumps({}),iso_now(),iso_now()))
        for o in extract_verified_odds(research):
            conn.execute(f"INSERT INTO odds_snapshots(user_id,analysis_id,event_key,market,line,odds,source,retrieved_at) VALUES ({p},{p},{p},{p},{p},{p},{p},{p})",(user["user_id"],analysis_id,result["match"]["competition"]+":"+team_a+":"+team_b+":"+kickoff,o.get("market"),o.get("line"),o.get("odds"),o.get("source_url"),o.get("timestamp") or iso_now()))
        conn.commit()
    result["analysis_id"]=analysis_id
    return result


# --------------------------- Live tracking ---------------------------

def live_track(team_a: str, team_b: str, competition: str, date: str) -> dict:
    queries=[f'"{team_a}" "{team_b}" live score {competition}',f'"{team_a}" "{team_b}" score {date}',f'"{team_a}" "{team_b}" match events']
    sources=[]
    for q in queries:
        try:
            d=rapid_search(q,limit=10,max_pages=1)
            sources.extend(d["results"][:10])
        except QuantError: pass
    if not sources: return {"status":"UNKNOWN_DATA","reason":"NO_LIVE_SOURCES"}
    compact=[{"url":x.get("url"),"title":x.get("title"),"description":x.get("description")} for x in sources[:20]]
    if not gemini_client: return {"status":"UNKNOWN_DATA","reason":"GEMINI_NOT_CONFIGURED","sources":compact}
    prompt=f"Return JSON only. Extract only explicit live/current match facts for {team_a} vs {team_b}, {competition}, date {date}. Schema: {{\"status\":\"LIVE|FINISHED|NOT_STARTED|UNKNOWN_DATA\",\"score\":{{\"home\":null,\"away\":null}},\"minute\":null,\"events\":[],\"source_urls\":[]}}. Never infer. Sources: {json.dumps(compact,ensure_ascii=False)}"
    try:
        cfg=genai_types.GenerateContentConfig(temperature=0,response_mime_type="application/json",max_output_tokens=3000)
        r=gemini_client.models.generate_content(model=GEMINI_MODEL,contents=prompt,config=cfg)
        data=extract_json(r.text if r else "")
        data["sources"]=compact
        return data
    except Exception as exc:
        return {"status":"UNKNOWN_DATA","reason":str(exc),"sources":compact}


# --------------------------- Auth / API ---------------------------

def api_error(exc: Exception, status=400):
    code=getattr(exc,"code","ERROR")
    return jsonify({"status":code,"message":str(exc)}),status


def admin_create_key(payload: dict) -> dict:
    admin_auth()
    raw=make_key(); kh=hash_key(raw); max_uses=max(0,int(payload.get("max_uses") or 0)); expires=payload.get("expires_at")
    if expires: parse_dt(expires)
    note=str(payload.get("note") or "")[:200]
    p=db_placeholder()
    with db() as conn:
        conn.execute(f"INSERT INTO access_keys(key_hash,note,status,created_at,expires_at,max_uses,used_count) VALUES ({p},{p},'ACTIVE',{p},{p},{p},0)",(kh,note,iso_now(),expires,max_uses))
        conn.commit()
    return {"key":raw,"note":note,"max_uses":max_uses,"expires_at":expires,"status":"ACTIVE"}


def admin_keys() -> list:
    admin_auth()
    with db() as conn:
        rows=conn.execute("SELECT id,note,status,created_at,expires_at,max_uses,used_count,bound_user_id,last_used_at FROM access_keys ORDER BY id DESC").fetchall()
    return [rowdict(r) for r in rows]


def admin_revoke(key_or_hash: str):
    admin_auth(); kh=key_or_hash if len(key_or_hash)==64 else hash_key(key_or_hash); p=db_placeholder()
    with db() as conn:
        conn.execute(f"UPDATE access_keys SET status='REVOKED' WHERE key_hash={p}",(kh,)); conn.commit()


if app:
    @app.get("/health")
    def health():
        return jsonify({"status":"OK","version":APP_VERSION,"model_version":MODEL_VERSION,"rapidapi_configured":bool(RAPIDAPI_KEY),"rapidapi_host":RAPIDAPI_HOST,"gemini_configured":bool(gemini_client),"gemini_model":GEMINI_MODEL,"database":"postgresql" if _is_pg() else "sqlite","license_required":REQUIRE_LICENSE})

    @app.get("/admin")
    def admin_page():
        return ADMIN_PAGE

    @app.get("/")
    def root():
        return HTML_PAGE

    @app.post("/api/admin/keys")
    def api_admin_create_key():
        try: return jsonify(admin_create_key(request.get_json(silent=True) or {}))
        except AuthError as exc: return api_error(exc,401)
        except Exception as exc: return api_error(exc,400)

    @app.get("/api/admin/keys")
    def api_admin_list_keys():
        try: return jsonify(admin_keys())
        except AuthError as exc: return api_error(exc,401)
        except Exception as exc: return api_error(exc,400)

    @app.post("/api/admin/revoke-key")
    def api_admin_revoke_key():
        try:
            admin_revoke(str((request.get_json(silent=True) or {}).get("key") or "")); return jsonify({"status":"OK"})
        except AuthError as exc: return api_error(exc,401)
        except Exception as exc: return api_error(exc,400)

    @app.post("/api/auth/check")
    def api_auth_check():
        try:
            u=current_user(False); return jsonify({"status":"OK","user_id":u["user_id"],"used_count":u.get("used_count",0),"max_uses":u.get("max_uses",0),"expires_at":u.get("expires_at")})
        except AuthError as exc: return api_error(exc,401)

    @app.post("/api/analyze")
    def api_analyze():
        try:
            user=current_user(True)
            d=request.get_json(silent=True) or {}
            fields=[str(d.get(k) or "").strip() for k in ("team_a","team_b","competition","date","kickoff")]
            if not all(fields): raise InvalidData("team_a, team_b, competition, date, kickoff are required")
            bankroll=float(d.get("bankroll") or 0)
            result=analyze_match(*fields,bankroll,user=user)
            log_usage(user,"ANALYZE",result["status"],{"analysis_id":result.get("analysis_id")})
            return jsonify(result)
        except AuthError as exc: return api_error(exc,401)
        except RateLimited as exc: return api_error(exc,429)
        except QuantError as exc: return api_error(exc,503)
        except Exception as exc: log.exception("analyze failed"); return api_error(exc,500)

    @app.get("/api/history")
    def api_history():
        try:
            u=current_user(False); p=db_placeholder()
            with db() as conn: rows=conn.execute(f"SELECT id,team_a,team_b,competition,kickoff,status,created_at FROM analyses WHERE user_id={p} ORDER BY id DESC LIMIT 200",(u["user_id"],)).fetchall()
            return jsonify([rowdict(r) for r in rows])
        except AuthError as exc: return api_error(exc,401)

    @app.get("/api/history/<int:analysis_id>")
    def api_history_detail(analysis_id):
        try:
            u=current_user(False); p=db_placeholder()
            with db() as conn: row=conn.execute(f"SELECT * FROM analyses WHERE id={p} AND user_id={p}",(analysis_id,u["user_id"])).fetchone()
            if not row: raise NoData("Analysis not found")
            return jsonify(json.loads(row["result_json"]))
        except AuthError as exc: return api_error(exc,401)
        except QuantError as exc: return api_error(exc,404)

    @app.post("/api/watchlist")
    def api_watch_add():
        try:
            u=current_user(False); d=request.get_json(silent=True) or {}; aid=int(d.get("analysis_id")); note=str(d.get("note") or "")[:500]; p=db_placeholder(); ts=iso_now()
            with db() as conn:
                row=conn.execute(f"SELECT id FROM analyses WHERE id={p} AND user_id={p}",(aid,u["user_id"])).fetchone()
                if not row: raise NoData("Analysis not found")
                conn.execute(f"INSERT INTO watchlist(user_id,analysis_id,event_key,status,note,created_at,updated_at) VALUES ({p},{p},{p},'WATCHING',{p},{p},{p})",(u["user_id"],aid,str(aid),note,ts,ts)); conn.commit()
            return jsonify({"status":"OK"})
        except AuthError as exc: return api_error(exc,401)
        except QuantError as exc: return api_error(exc,404)

    @app.get("/api/watchlist")
    def api_watch_list():
        try:
            u=current_user(False); p=db_placeholder()
            with db() as conn: rows=conn.execute(f"SELECT * FROM watchlist WHERE user_id={p} ORDER BY updated_at DESC LIMIT 200",(u["user_id"],)).fetchall()
            return jsonify([rowdict(r) for r in rows])
        except AuthError as exc: return api_error(exc,401)

    @app.post("/api/track")
    def api_track():
        try:
            u=current_user(False); d=request.get_json(silent=True) or {}
            for k in ("team_a","team_b","competition","date"):
                if not str(d.get(k) or "").strip(): raise InvalidData(f"{k} is required")
            state=live_track(str(d["team_a"]),str(d["team_b"]),str(d["competition"]),str(d["date"]))
            # Persist the current tracking state only inside this user's rows.
            p=db_placeholder()
            with db() as conn:
                rows=conn.execute(f"SELECT id,analysis_id,market,selection FROM bet_tracking WHERE user_id={p} ORDER BY id DESC LIMIT 200",(u["user_id"],)).fetchall()
                for row in rows:
                    arow=conn.execute(f"SELECT team_a,team_b,competition FROM analyses WHERE id={p} AND user_id={p}",(row["analysis_id"],u["user_id"])).fetchone()
                    if not arow or not same_team(arow["team_a"],d["team_a"]) or not same_team(arow["team_b"],d["team_b"]):
                        continue
                    status="UNKNOWN_DATA"
                    if state.get("status")=="LIVE": status="ALIVE"
                    elif state.get("status")=="FINISHED":
                        score=state.get("score") or {}
                        try:
                            h=int(score.get("home")); aw=int(score.get("away")); market=normalize_name(row["market"]); sel=normalize_name(row["selection"])
                            if market in {"1x2","match result","moneyline"}:
                                won=(sel==normalize_name(d["team_a"]) and h>aw) or (sel==normalize_name(d["team_b"]) and aw>h) or (sel=="draw" and h==aw)
                                status="WON" if won else "DEAD"
                            elif "over 2.5" in market or market in {"o2 5","total 2 5"}:
                                status="WON" if h+aw>=3 else "DEAD"
                            else: status="UNKNOWN_DATA"
                        except Exception: status="UNKNOWN_DATA"
                    conn.execute(f"UPDATE bet_tracking SET status={p},live_state_json={p},updated_at={p} WHERE id={p} AND user_id={p}",(status,json.dumps(state,ensure_ascii=False),iso_now(),row["id"],u["user_id"]))
                conn.commit()
            state["user_id"]=u["user_id"]
            state["action"]="MONITOR" if state.get("status")=="LIVE" else ("SETTLED" if state.get("status")=="FINISHED" else "VERIFY_DATA")
            return jsonify(state)
        except AuthError as exc: return api_error(exc,401)
        except QuantError as exc: return api_error(exc,503)

    @app.get("/api/usage")
    def api_usage():
        try:
            u=current_user(False); p=db_placeholder()
            with db() as conn:
                rows=conn.execute(f"SELECT action,status,created_at,metadata FROM usage_logs WHERE user_id={p} ORDER BY id DESC LIMIT 200",(u["user_id"],)).fetchall()
            return jsonify({"user": {"user_id":u["user_id"],"used_count":u.get("used_count",0),"max_uses":u.get("max_uses",0),"expires_at":u.get("expires_at")},"logs":[rowdict(r) for r in rows]})
        except AuthError as exc: return api_error(exc,401)


ADMIN_PAGE = '''<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Quant Terminal Admin</title><style>body{margin:0;background:#070b13;color:#eef3fb;font:14px system-ui;padding:24px}.box{max-width:900px;margin:auto;background:#101827;border:1px solid #263349;border-radius:16px;padding:20px}input,button{padding:10px;border-radius:9px;border:1px solid #30405a;background:#0a111c;color:#fff;margin:4px}button{cursor:pointer;font-weight:700}.key{padding:12px;background:#0b1422;border-radius:10px;margin:8px 0}.danger{color:#ff8794}table{width:100%;border-collapse:collapse;margin-top:14px}td,th{border-bottom:1px solid #263349;padding:8px;text-align:left;font-size:12px}</style></head><body><div class="box"><h2>QUANT TERMINAL · KEY ADMIN</h2><p>Create access keys with optional use limits and expiry. Raw keys are shown only once.</p><input id="t" type="password" placeholder="Admin token" style="width:60%"><button onclick="load()">Load</button><hr><input id="note" placeholder="Note"><input id="uses" type="number" min="0" placeholder="Max uses (0=unlimited)"><input id="exp" placeholder="Expiry ISO, optional"><button onclick="createKey()">Create key</button><div id="out"></div><div id="list"></div></div><script>const $=x=>document.getElementById(x);async function req(u,o={}){o.headers={...(o.headers||{}),'X-Admin-Token':$('t').value};const r=await fetch(u,o);const d=await r.json();if(!r.ok)throw Error(d.message||d.status);return d}async function createKey(){try{const d=await req('/api/admin/keys',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({note:$('note').value,max_uses:Number($('uses').value||0),expires_at:$('exp').value||null})});$('out').innerHTML='<div class="key"><b>'+d.key+'</b><br>Copy it now. Plaintext is not stored.</div>';load()}catch(e){$('out').innerHTML='<span class="danger">'+e.message+'</span>'}}async function load(){try{const d=await req('/api/admin/keys');$('list').innerHTML='<table><tr><th>ID</th><th>Note</th><th>Status</th><th>Uses</th><th>Expires</th><th>User</th></tr>'+d.map(x=>'<tr><td>'+x.id+'</td><td>'+x.note+'</td><td>'+x.status+'</td><td>'+x.used_count+'/'+(x.max_uses||'∞')+'</td><td>'+(x.expires_at||'—')+'</td><td>'+(x.bound_user_id||'—')+'</td></tr>').join('')+'</table>'}catch(e){$('list').textContent=e.message}}</script></body></html>'''

HTML_PAGE = r'''<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Quant Terminal</title><style>
:root{--bg:#070b13;--panel:#0e1420;--panel2:#111a29;--line:#243044;--text:#eef3fb;--muted:#7f8da5;--accent:#8cffc1;--accent2:#78a7ff;--danger:#ff7f8e}*{box-sizing:border-box}body{margin:0;background:radial-gradient(900px 500px at 75% -10%,#162442 0%,transparent 65%),var(--bg);color:var(--text);font:14px/1.45 Inter,system-ui,-apple-system,Segoe UI,sans-serif}.shell{max-width:1180px;margin:auto;padding:28px 18px 100px}.top{display:flex;justify-content:space-between;gap:20px;align-items:center;margin-bottom:20px}.brand{font-size:24px;font-weight:850;letter-spacing:-.03em}.sub{color:var(--muted);font-size:12px}.pill{border:1px solid var(--line);background:#0b111c;border-radius:999px;padding:7px 11px;color:var(--muted)}.panel{background:linear-gradient(180deg,#101827,#0b111b);border:1px solid var(--line);border-radius:18px;padding:18px;margin:14px 0;box-shadow:0 10px 40px #0003}.grid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}.field{display:flex;flex-direction:column;gap:6px}.field label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}input{width:100%;background:#080e17;border:1px solid #263349;color:#fff;border-radius:10px;padding:11px}button{border:1px solid #30405a;background:#162238;color:#fff;border-radius:10px;padding:11px 15px;font-weight:750;cursor:pointer}button.primary{background:linear-gradient(135deg,#79f2b3,#79a9ff);color:#07100d;border:0}.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}.result{display:grid;grid-template-columns:1.4fr .8fr .8fr .8fr;gap:10px;align-items:stretch}.hero{padding:22px;border:1px solid #31415a;border-radius:16px;background:linear-gradient(135deg,#111d2e,#0d1521)}.match{font-size:20px;font-weight:800}.pick{font-size:28px;font-weight:900;margin:10px 0;color:var(--accent)}.kpi{background:#0a111c;border:1px solid #1f2b3e;border-radius:13px;padding:14px}.kpi small{color:var(--muted)}.kpi b{display:block;font-size:20px;margin-top:4px}.tabs{display:flex;gap:8px;overflow:auto}.tab{padding:9px 13px;border-radius:999px;background:#0b111b;border:1px solid var(--line);cursor:pointer}.tab.active{border-color:#6f9bff;color:#bcd0ff}.hidden{display:none}.muted{color:var(--muted)}pre{white-space:pre-wrap;word-break:break-word;color:#aebbd0}.list{display:grid;gap:8px}.item{padding:12px;border:1px solid var(--line);border-radius:12px;background:#0b121e}.ok{color:var(--accent)}.danger{color:var(--danger)}.warn{color:#ffd38a}.small{font-size:12px;color:var(--muted)}@media(max-width:850px){.grid{grid-template-columns:1fr 1fr}.result{grid-template-columns:1fr 1fr}}@media(max-width:560px){.shell{padding:18px 12px 90px}.grid{grid-template-columns:1fr}.result{grid-template-columns:1fr}.brand{font-size:20px}}
</style></head><body><main class="shell"><div class="top"><div><div class="brand">QUANT TERMINAL</div><div class="sub">Google Search74 → Gemini → deterministic Quant Engine → No-Bet</div></div><div id="authPill" class="pill">KEY REQUIRED</div></div>
<section class="panel" id="loginPanel"><div class="field"><label>Access Key</label><input id="key" type="password" placeholder="QT-…"></div><div class="actions"><button class="primary" onclick="login()">ENTER TERMINAL</button></div><div id="loginMsg" class="small"></div></section>
<div id="app" class="hidden"><section class="panel"><div class="grid"><div class="field"><label>Team A</label><input id="a"></div><div class="field"><label>Team B</label><input id="b"></div><div class="field"><label>Competition</label><input id="comp"></div><div class="field"><label>Date</label><input id="date" type="date"></div><div class="field"><label>Kickoff GMT+7</label><input id="kickoff" type="time"></div></div><div class="actions"><button class="primary" onclick="analyze()">RUN ANALYSIS</button><button onclick="loadHistory()">HISTORY</button><button onclick="loadWatch()">WATCHLIST</button><button onclick="usage()">MY USAGE</button></div></section><section id="result"></section><section class="panel"><div class="tabs"><div class="tab active" onclick="show('detail',this)">FULL ANALYSIS</div><div class="tab" onclick="show('history',this)">HISTORY</div><div class="tab" onclick="show('watch',this)">WATCHLIST</div><div class="tab" onclick="show('usage',this)">USAGE</div></div><div id="detail" style="margin-top:14px"><div class="muted">Run an analysis to see evidence, sources and model details.</div></div><div id="history" class="hidden" style="margin-top:14px"></div><div id="watch" class="hidden" style="margin-top:14px"></div><div id="usage" class="hidden" style="margin-top:14px"></div></section></div></main><script>
const $=id=>document.getElementById(id);const keyStore='qt_access_key';let last=null;function key(){return localStorage.getItem(keyStore)||$('key').value||''}function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}function hdr(){return {'Content-Type':'application/json','X-Access-Key':key()}}async function api(url,opt={}){opt.headers={...(opt.headers||{}),'X-Access-Key':key()};const r=await fetch(url,opt);const d=await r.json();if(!r.ok)throw Error(d.message||d.status||'Request failed');return d}async function login(){try{localStorage.setItem(keyStore,$('key').value.trim());const d=await api('/api/auth/check',{method:'POST'});$('loginPanel').classList.add('hidden');$('app').classList.remove('hidden');$('authPill').textContent=d.user_id+' · '+d.used_count+'/'+(d.max_uses||'∞');$('loginMsg').textContent=''}catch(e){localStorage.removeItem(keyStore);$('loginMsg').textContent=e.message}}async function analyze(){const d={team_a:$('a').value.trim(),team_b:$('b').value.trim(),competition:$('comp').value.trim(),date:$('date').value,kickoff:$('kickoff').value,bankroll:0};if(!d.team_a||!d.team_b||!d.competition||!d.date||!d.kickoff){$('detail').innerHTML='<span class="danger">Fill all five match fields.</span>';return}$('result').innerHTML='<section class="panel">Researching and verifying sources…</section>';try{const x=await api('/api/analyze',{method:'POST',headers:hdr(),body:JSON.stringify(d)});last=x;renderResult(x);loadUsage()}catch(e){$('result').innerHTML='<section class="panel danger">'+esc(e.message)+'</section>'}}function renderResult(x){const p=x.primary_pick;const status=x.status==='VALID_BET'?'VALID BET':'NO BET';$('result').innerHTML='<section class="panel result"><div class="hero"><div class="small">'+esc(x.match.competition)+'</div><div class="match">'+esc(x.match.team_a)+' vs '+esc(x.match.team_b)+'</div><div class="small">'+esc(x.match.date)+' · '+esc(x.match.kickoff)+' GMT+7</div><div class="pick">'+(p?esc(p.market+' · '+p.selection):status)+'</div><div class="small">'+(p?'Verified positive EV':'Reason: '+esc(x.no_bet?.reason||''))+'</div></div><div class="kpi"><small>CONFIDENCE</small><b>'+Number(x.confidence||0).toFixed(1)+'%</b></div><div class="kpi"><small>MODEL P</small><b>'+(p?Number(p.probability).toFixed(1)+'%':'—')+'</b></div><div class="kpi"><small>EV</small><b>'+(p?Number(p.ev).toFixed(2)+'%':'—')+'</b></div></section>';$('detail').innerHTML='<div class="list"><div class="item"><b>Match identity</b><pre>'+esc(JSON.stringify(x.research.match_identity,null,2))+'</pre></div><div class="item"><b>Model</b><pre>'+esc(JSON.stringify(x.model,null,2))+'</pre></div><div class="item"><b>Odds snapshots</b><pre>'+esc(JSON.stringify(x.research.odds_snapshots,null,2))+'</pre></div><div class="item"><b>Conflicts</b><pre>'+esc(JSON.stringify(x.research.conflicts,null,2))+'</pre></div><div class="item"><b>Sources</b><pre>'+esc(JSON.stringify(x.sources,null,2))+'</pre></div><button onclick="addWatch()">ADD TO WATCHLIST</button></div>'}async function addWatch(){if(!last?.analysis_id)return;try{await api('/api/watchlist',{method:'POST',headers:hdr(),body:JSON.stringify({analysis_id:last.analysis_id})});loadWatch()}catch(e){alert(e.message)}}async function loadHistory(){show('history',document.querySelectorAll('.tab')[1]);try{const d=await api('/api/history');$('history').innerHTML='<div class="list">'+d.map(x=>'<div class="item"><b>#'+x.id+' '+esc(x.team_a)+' vs '+esc(x.team_b)+'</b><div class="small">'+esc(x.competition)+' · '+esc(x.kickoff)+' · '+esc(x.status)+'</div></div>').join('')+'</div>'||'<div class="muted">No history.</div>'}catch(e){$('history').textContent=e.message}}async function loadWatch(){show('watch',document.querySelectorAll('.tab')[2]);try{const d=await api('/api/watchlist');$('watch').innerHTML='<div class="list">'+d.map(x=>'<div class="item"><b>Analysis #'+x.analysis_id+'</b><div class="small">'+esc(x.status)+' · '+esc(x.updated_at)+'</div></div>').join('')+'</div>'||'<div class="muted">No watchlist.</div>'}catch(e){$('watch').textContent=e.message}}async function loadUsage(){try{const d=await api('/api/usage');$('authPill').textContent=d.user.user_id+' · '+d.user.used_count+'/'+(d.user.max_uses||'∞');}catch(e){}}async function usage(){show('usage',document.querySelectorAll('.tab')[3]);try{const d=await api('/api/usage');$('usage').innerHTML='<div class="item"><b>'+esc(d.user.user_id)+'</b><div class="small">Uses: '+d.user.used_count+'/'+(d.user.max_uses||'∞')+' · expires: '+esc(d.user.expires_at||'never')+'</div></div><pre>'+esc(JSON.stringify(d.logs,null,2))+'</pre>'}catch(e){$('usage').textContent=e.message}}function show(id,el){for(const x of ['detail','history','watch','usage'])$(x).classList.toggle('hidden',x!==id);document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));if(el)el.classList.add('active')}if(localStorage.getItem(keyStore)){$('key').value='';login().catch(()=>{})}
</script></body></html>'''


def run():
    if app is None: raise SystemExit("Flask is not installed")
    app.run(host=os.getenv("HOST","0.0.0.0"),port=int(os.getenv("PORT","8080")),debug=False)

if __name__ == "__main__": run()
