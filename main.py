from __future__ import annotations

import hashlib
import html
import json
import logging
import math
import os
import re
import secrets
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote_plus, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, redirect, make_response

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None

try:
    from google import genai
    from google.genai import types as genai_types
except Exception:
    genai = None
    genai_types = None

APP_VERSION = "quant-terminal-2026.09.24-free-web-collector"
MODEL_VERSION = "poisson-form-gemini-research-v2"
VN = ZoneInfo("Asia/Ho_Chi_Minh")
UTC = timezone.utc
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
APP_ENV = os.getenv("APP_ENV", "production").strip().lower()
REQUIRE_ACCESS_KEY = os.getenv("REQUIRE_ACCESS_KEY", "1").strip().lower() not in {"0", "false", "no"}
MAX_SEARCH_RESULTS = max(4, min(int(os.getenv("MAX_SEARCH_RESULTS", "10")), 20))
MAX_SOURCE_PAGES = max(4, min(int(os.getenv("MAX_SOURCE_PAGES", "18")), 30))
HTTP_TIMEOUT = max(3, min(int(os.getenv("HTTP_TIMEOUT", "6")), 15))
COLLECTOR_TIMEOUT = max(15, min(int(os.getenv("COLLECTOR_TIMEOUT", "35")), 60))
SEARCH_TIMEOUT = max(2, min(int(os.getenv("SEARCH_TIMEOUT", "5")), 10))
MAX_PAGE_CHARS = 18000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("quant-terminal")
app = Flask(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS access_keys (
    key_hash TEXT PRIMARY KEY,
    key_prefix TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    max_uses INTEGER NOT NULL DEFAULT 0,
    uses INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    revoked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_access_user ON access_keys(user_id);
CREATE TABLE IF NOT EXISTS analyses (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    team_a TEXT NOT NULL,
    team_b TEXT NOT NULL,
    competition TEXT,
    match_date TEXT,
    kickoff TEXT,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analysis_user ON analyses(user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS usage_logs (
    id TEXT PRIMARY KEY,
    user_id TEXT,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);
"""


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat()


def db():
    if DATABASE_URL:
        if psycopg is None:
            raise RuntimeError("psycopg is not installed")
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    import sqlite3
    path = os.getenv("DB_FILE", "database.db")
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        if DATABASE_URL:
            tables = {
                "users": {
                    "id": "TEXT", "display_name": "TEXT", "created_at": "TEXT"
                },
                "access_keys": {
                    "key_hash": "TEXT", "key_prefix": "TEXT", "user_id": "TEXT",
                    "max_uses": "INTEGER DEFAULT 0", "uses": "INTEGER DEFAULT 0",
                    "expires_at": "TEXT", "revoked": "INTEGER DEFAULT 0",
                    "created_at": "TEXT", "last_used_at": "TEXT"
                },
                "analyses": {
                    "id": "TEXT", "user_id": "TEXT", "team_a": "TEXT", "team_b": "TEXT",
                    "competition": "TEXT", "match_date": "TEXT", "kickoff": "TEXT",
                    "status": "TEXT", "result_json": "TEXT", "created_at": "TEXT"
                },
                "usage_logs": {
                    "id": "TEXT", "user_id": "TEXT", "action": "TEXT", "status": "TEXT",
                    "detail": "TEXT", "created_at": "TEXT"
                },
            }
            for table, cols in tables.items():
                pk = " PRIMARY KEY" if table in {"users", "analyses"} else (" PRIMARY KEY" if table == "access_keys" else "")
                if table == "users":
                    conn.execute("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, display_name TEXT, created_at TEXT)")
                elif table == "access_keys":
                    conn.execute("CREATE TABLE IF NOT EXISTS access_keys (key_hash TEXT PRIMARY KEY, key_prefix TEXT, user_id TEXT, max_uses INTEGER DEFAULT 0, uses INTEGER DEFAULT 0, expires_at TEXT, revoked INTEGER DEFAULT 0, created_at TEXT, last_used_at TEXT)")
                elif table == "analyses":
                    conn.execute("CREATE TABLE IF NOT EXISTS analyses (id TEXT PRIMARY KEY, user_id TEXT, team_a TEXT, team_b TEXT, competition TEXT, match_date TEXT, kickoff TEXT, status TEXT, result_json TEXT, created_at TEXT)")
                else:
                    conn.execute("CREATE TABLE IF NOT EXISTS usage_logs (id TEXT PRIMARY KEY, user_id TEXT, action TEXT, status TEXT, detail TEXT, created_at TEXT)")
                existing = {r["column_name"] for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s", (table,)
                ).fetchall()}
                for col, definition in cols.items():
                    if col not in existing:
                        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col}" {definition}')
            legacy_user = "legacy_admin"
            now = iso_now()
            conn.execute("INSERT INTO users(id,display_name,created_at) VALUES(%s,%s,%s) ON CONFLICT (id) DO NOTHING", (legacy_user, "Legacy / migrated user", now))
            conn.execute("UPDATE access_keys SET user_id=%s WHERE user_id IS NULL", (legacy_user,))
            conn.execute("UPDATE access_keys SET max_uses=0 WHERE max_uses IS NULL")
            conn.execute("UPDATE access_keys SET uses=0 WHERE uses IS NULL")
            conn.execute("UPDATE access_keys SET revoked=0 WHERE revoked IS NULL")
            conn.execute("UPDATE access_keys SET created_at=%s WHERE created_at IS NULL", (now,))
            conn.execute("CREATE INDEX IF NOT EXISTS idx_access_user ON access_keys(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_analysis_user ON analyses(user_id, created_at DESC)")
            conn.commit()
        else:
            conn.executescript(SCHEMA)
            conn.commit()


def adapt_sql(sql: str) -> str:
    return sql.replace("?", "%s") if DATABASE_URL else sql


def qone(sql: str, params=()):
    with db() as conn:
        row = conn.execute(adapt_sql(sql), params).fetchone()
        conn.commit()
        return dict(row) if row else None


def qall(sql: str, params=()):
    with db() as conn:
        return [dict(x) for x in conn.execute(adapt_sql(sql), params).fetchall()]


def qexec(sql: str, params=()):
    with db() as conn:
        n = conn.execute(adapt_sql(sql), params).rowcount
        conn.commit()
        return n


def sha256(s: str) -> str:
    return hashlib.sha256(s.strip().encode()).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(10)}"


def new_access_key() -> str:
    raw = secrets.token_urlsafe(24).replace("-", "").replace("_", "")[:28].upper()
    return "QT-" + "-".join(raw[i:i + 7] for i in range(0, 28, 7))


def require_admin():
    if not ADMIN_TOKEN:
        return jsonify({"status": "ADMIN_NOT_CONFIGURED", "message": "ADMIN_TOKEN is not configured."}), 503
    supplied = request.headers.get("X-Admin-Token", "") or request.args.get("admin_token", "")
    if not supplied or not secrets.compare_digest(supplied, ADMIN_TOKEN):
        return jsonify({"status": "ADMIN_UNAUTHORIZED", "message": "Invalid admin token."}), 401
    return None


def access_from_request() -> str:
    return (request.headers.get("X-Access-Key", "") or request.args.get("access_key", "") or request.cookies.get("quant_access_key", "")).strip()


def authenticate_access(key: str, consume=False):
    if not key:
        return None
    row = qone("SELECT * FROM access_keys WHERE key_hash=? AND revoked=0", (sha256(key),))
    if not row:
        return None
    now = utc_now()
    if row.get("expires_at"):
        try:
            if datetime.fromisoformat(row["expires_at"]) <= now:
                return None
        except Exception:
            return None
    if int(row.get("max_uses") or 0) > 0 and int(row.get("uses") or 0) >= int(row["max_uses"]):
        return None
    if consume:
        qexec("UPDATE access_keys SET uses=uses+1,last_used_at=? WHERE key_hash=?", (iso_now(), sha256(key)))
    return row


def require_user(consume=False):
    if not REQUIRE_ACCESS_KEY:
        return {"id": "dev_user", "display_name": "Development"}
    row = authenticate_access(access_from_request(), consume=consume)
    if not row:
        return None
    return qone("SELECT * FROM users WHERE id=?", (row["user_id"],))


# ---------- Free public web collector (no provider API key) ----------
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36 QuantTerminal/2026"


def clean_text(raw: str) -> str:
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", html.unescape(text))
    return text[:MAX_PAGE_CHARS]


def public_search(query: str) -> list[dict]:
    """Best-effort keyless HTML search. Never allowed to block the whole analysis."""
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,vi;q=0.8"}
    endpoints = [
        "https://html.duckduckgo.com/html/?q=" + quote_plus(query),
        "https://www.google.com/search?q=" + quote_plus(query) + "&num=8",
    ]
    out = []
    for endpoint in endpoints:
        started = time.monotonic()
        host = urlparse(endpoint).netloc
        log.info("[COLLECTOR] search START %s", host)
        try:
            r = requests.get(endpoint, headers=headers, timeout=(2, SEARCH_TIMEOUT), allow_redirects=True)
            log.info("[COLLECTOR] search END %s status=%s elapsed=%.2fs", host, r.status_code, time.monotonic()-started)
            if r.status_code >= 400:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.select("a[href]"):
                href = a.get("href", "")
                title = a.get_text(" ", strip=True)
                if not href or not title or len(title) < 8:
                    continue
                if href.startswith("//"):
                    href = "https:" + href
                elif href.startswith("/"):
                    href = urljoin(endpoint, href)
                p = urlparse(href)
                if p.scheme not in {"http", "https"}:
                    continue
                if any(x in p.netloc.lower() for x in ["duckduckgo.com", "google.com", "gstatic.com"]):
                    continue
                out.append({"url": href.split("#")[0], "title": title[:300], "description": "", "engine": host})
            if len(out) >= MAX_SEARCH_RESULTS:
                break
        except requests.Timeout:
            log.warning("[COLLECTOR] search TIMEOUT %s after %.2fs", host, time.monotonic()-started)
        except requests.RequestException as exc:
            log.warning("[COLLECTOR] search ERROR %s: %s", host, exc)
        except Exception as exc:
            log.warning("[COLLECTOR] search PARSE_ERROR %s: %s", host, exc)
    seen, deduped = set(), []
    for x in out:
        if x["url"] in seen:
            continue
        seen.add(x["url"]); deduped.append(x)
    return deduped[:MAX_SEARCH_RESULTS]


def build_queries(a: str, b: str, comp: str, date: str) -> list[str]:
    base = f'"{a}" "{b}" "{comp}" "{date}"'
    return [
        base,
        f'{base} fixture lineup injuries',
        f'{base} odds over under handicap',
        f'{base} xG statistics form site:sofascore.com OR site:fotmob.com OR site:fbref.com',
    ]


def fetch_public_page(url: str) -> Optional[dict]:
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,vi;q=0.8"}, timeout=(2, HTTP_TIMEOUT), allow_redirects=True)
        ctype = r.headers.get("content-type", "")
        if r.status_code >= 400 or "text/html" not in ctype and "application/xhtml" not in ctype:
            return None
        text = clean_text(r.text)
        if len(text) < 80:
            return None
        return {"url": r.url, "title": BeautifulSoup(r.text, "html.parser").title.get_text(" ", strip=True)[:300] if BeautifulSoup(r.text, "html.parser").title else "", "text": text, "fetched_at": iso_now(), "status_code": r.status_code}
    except Exception as exc:
        log.info("[COLLECTOR] fetch failed %s: %s", url, exc)
        return None


def collect_research(a: str, b: str, comp: str, date: str) -> dict:
    """Concurrent, deadline-bounded collector. A blocked search source must never hang /api/analyze."""
    started = time.monotonic()
    deadline = started + COLLECTOR_TIMEOUT
    log.info("[COLLECTOR] start: %s vs %s | %s | %s | deadline=%ss", a, b, comp, date, COLLECTOR_TIMEOUT)
    queries = build_queries(a, b, comp, date)
    candidates, errors = [], []

    # Search in parallel so one blocked engine/query cannot serialize 4 x 2 timeouts.
    workers = min(8, max(2, len(queries)))
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="collector-search")
    futures = {pool.submit(public_search, q): q for q in queries}
    try:
        remaining = max(0.1, deadline - time.monotonic())
        for fut in as_completed(futures, timeout=remaining):
            q = futures[fut]
            log.info("[COLLECTOR] public search DONE: %s", q)
            try:
                candidates.extend(fut.result())
            except Exception as exc:
                errors.append(f"search: {exc}")
    except TimeoutError:
        errors.append("collector search deadline reached")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    # Any unfinished futures are abandoned; their socket calls have their own timeout.
    if time.monotonic() >= deadline:
        errors.append("collector search deadline reached")

    seen, unique = set(), []
    for s in candidates:
        u = s["url"]
        if u in seen:
            continue
        seen.add(u); unique.append(s)
    preferred = [s for s in unique if any(d in urlparse(s["url"]).netloc.lower() for d in ["sofascore.com", "fotmob.com", "fbref.com", "understat.com"])]
    rest = [s for s in unique if s not in preferred]
    ordered = preferred + rest

    pages = []
    remaining = max(0.1, deadline - time.monotonic())
    if ordered and remaining > 0.5:
        max_pages = min(MAX_SOURCE_PAGES, len(ordered))
        pool = ThreadPoolExecutor(max_workers=min(8, max_pages), thread_name_prefix="collector-fetch")
        futures = {pool.submit(fetch_public_page, s["url"]): s for s in ordered[:max_pages]}
        try:
            for fut in as_completed(futures, timeout=remaining):
                src = futures[fut]
                try:
                    page = fut.result()
                except Exception as exc:
                    page = None; errors.append(f"fetch {src['url']}: {exc}")
                if page:
                    page.update({"search_title": src.get("title", ""), "search_engine": src.get("engine", "")})
                    pages.append(page)
        except TimeoutError:
            errors.append("collector page-fetch deadline reached")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    elapsed = time.monotonic() - started
    log.info("[COLLECTOR] end: candidates=%s fetched_pages=%s errors=%s elapsed=%.2fs", len(unique), len(pages), len(errors), elapsed)
    return {"sources": pages, "search_results": unique[:MAX_SEARCH_RESULTS * 4], "errors": errors, "elapsed_seconds": round(elapsed, 2), "deadline_seconds": COLLECTOR_TIMEOUT}


# ---------- Gemini evidence normalization ----------
def gemini_extract(research: dict, a: str, b: str, comp: str, date: str, kickoff: str) -> dict:
    if genai is None or genai_types is None or not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_NOT_CONFIGURED")
    client = genai.Client(api_key=GEMINI_API_KEY)
    evidence_parts = []
    for i, s in enumerate(research.get("sources", []), 1):
        evidence_parts.append(f"SOURCE {i}\nURL: {s.get('url')}\nTITLE: {s.get('title') or s.get('search_title')}\nFETCHED_AT: {s.get('fetched_at')}\nTEXT:\n{s.get('text','')}")
    evidence = "\n\n".join(evidence_parts)
    schema = {
        "match_identity": {"verified": False, "home": a, "away": b, "competition": comp, "date": date, "kickoff": kickoff, "evidence_sources": []},
        "form": {"home_last5": [], "away_last5": [], "home_last10": [], "away_last10": []},
        "odds": {"home": None, "draw": None, "away": None, "over_2_5": None, "under_2_5": None, "asian_handicap": []},
        "stats": {"home_xg": None, "away_xg": None, "home_xga": None, "away_xga": None, "home_corners": None, "away_corners": None, "home_cards": None, "away_cards": None, "home_possession": None, "away_possession": None, "home_ppda": None, "away_ppda": None},
        "team_news": [], "injuries": [], "suspensions": [], "expected_lineups": [],
        "odds_snapshots": [], "source_notes": [], "freshness": "PARTIAL", "confidence": "LOW"
    }
    prompt = f"""You are the evidence-normalization layer of a football quant system. You DO NOT browse the web and you MUST NOT invent facts.

MATCH REQUEST
Home/Team A: {a}
Away/Team B: {b}
Competition: {comp}
Date: {date}
Kickoff GMT+7: {kickoff}

PUBLIC WEB EVIDENCE COLLECTED WITHOUT API KEYS:
{evidence}

Return ONLY valid JSON matching this exact structure:
{json.dumps(schema, ensure_ascii=False)}

STRICT RULES
1. Use only explicit facts in the supplied source text. Missing = null/[]; never estimate.
2. Every extracted score object must be {{"gf": number, "ga": number, "date": string|null, "opponent": string|null, "source_url": string}}.
3. Form must belong to the requested teams. Do not mix similarly named teams.
4. match_identity.verified=true only when team identity, home/away, competition and date/kickoff are sufficiently supported by evidence. If uncertain, false.
5. Odds require an explicit price. A price without a timestamp/source is a current/undated price, NOT a movement snapshot.
6. odds_snapshots require explicit numeric odds + timestamp/date + source_url + market. Never manufacture movement.
7. Do not infer xG, xGA, corners, cards, possession, PPDA, lineups, injuries or suspensions from general knowledge.
8. source_notes should contain concise facts with source_url. Preserve URLs exactly.
9. freshness is VERIFIED only if key identity evidence is recent and consistent; otherwise PARTIAL or INSUFFICIENT.
10. Never output a betting recommendation. Quant Engine outside Gemini makes that decision mechanically.
"""
    cfg = genai_types.GenerateContentConfig(temperature=0, response_mime_type="application/json")
    log.info("[GEMINI] sending evidence (%s pages)", len(research.get("sources", [])))
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=cfg)
    raw = response.text if response and response.text else ""
    try:
        data = json.loads(raw)
    except Exception as exc:
        log.error("[GEMINI] invalid JSON")
        raise RuntimeError("GEMINI_INVALID_JSON") from exc
    return data


# ---------- deterministic Quant Engine ----------
def poisson_p(lmbda: float, goals: int) -> float:
    return (lmbda ** goals) * math.exp(-lmbda) / math.factorial(goals)


def poisson_probs(lh: float, la: float, max_goals: int = 10):
    ph = [poisson_p(lh, i) for i in range(max_goals + 1)]
    pa = [poisson_p(la, i) for i in range(max_goals + 1)]
    matrix = [[ph[i] * pa[j] for j in range(max_goals + 1)] for i in range(max_goals + 1)]
    z = sum(map(sum, matrix))
    matrix = [[x / z for x in row] for row in matrix]
    home = sum(matrix[i][j] for i in range(11) for j in range(11) if i > j)
    draw = sum(matrix[i][j] for i in range(11) for j in range(11) if i == j)
    away = sum(matrix[i][j] for i in range(11) for j in range(11) if i < j)
    over25 = sum(matrix[i][j] for i in range(11) for j in range(11) if i + j >= 3)
    return home, draw, away, over25


def deterministic_model(research_json: dict) -> Optional[dict]:
    form = research_json.get("form") or {}
    hf = form.get("home_last5") or []
    af = form.get("away_last5") or []
    h = [x for x in hf if isinstance(x, dict) and x.get("gf") is not None and x.get("ga") is not None]
    a = [x for x in af if isinstance(x, dict) and x.get("gf") is not None and x.get("ga") is not None]
    if len(h) < 5 or len(a) < 5:
        return None
    try:
        hgf = [float(x["gf"]) for x in h[:5]]; hga = [float(x["ga"]) for x in h[:5]]
        agf = [float(x["gf"]) for x in a[:5]]; aga = [float(x["ga"]) for x in a[:5]]
        lh = max(0.15, min(4.5, 0.65 * statistics.mean(hgf) + 0.35 * statistics.mean(aga)))
        la = max(0.15, min(4.5, 0.65 * statistics.mean(agf) + 0.35 * statistics.mean(hga)))
        ph, pd, pa, po = poisson_probs(lh, la)
        return {"lambda_home": round(lh, 6), "lambda_away": round(la, 6), "prob_home": ph, "prob_draw": pd, "prob_away": pa, "prob_over_2_5": po, "data_quality": "FORM_VERIFIED_5+", "model_version": MODEL_VERSION}
    except Exception:
        return None


def ev(prob: float, odds: float) -> float:
    return prob * odds - 1.0


def pick_from_odds(model: dict, odds: dict) -> Optional[dict]:
    candidates = []
    for key, pkey in [("home", "prob_home"), ("draw", "prob_draw"), ("away", "prob_away"), ("over_2_5", "prob_over_2_5")]:
        try:
            o = float(odds[key]) if odds.get(key) is not None else None
        except Exception:
            o = None
        if o and o > 1 and model.get(pkey) is not None:
            p = float(model[pkey]); value = ev(p, o)
            candidates.append({"market": key, "probability": round(p * 100, 4), "market_probability": round((1 / o) * 100, 4), "odds": o, "ev": round(value * 100, 4)})
    if not candidates:
        return None
    positive = [x for x in candidates if x["ev"] > 0]
    if not positive:
        return None
    # Primary pick is deterministic: highest EV only among verified, explicit odds.
    positive.sort(key=lambda x: (x["ev"], x["probability"]), reverse=True)
    best = positive[0]
    best["status"] = "VALUE"
    return best


def validate_future_kickoff(date: str, kickoff: str) -> tuple[bool, str]:
    try:
        dt = datetime.strptime(f"{date} {kickoff}", "%Y-%m-%d %H:%M").replace(tzinfo=VN)
    except ValueError:
        return False, "INVALID_DATE_OR_KICKOFF"
    now = datetime.now(VN)
    if dt <= now:
        return False, "MATCH_NOT_FUTURE"
    return True, dt.isoformat()


def save_analysis(user_id: str, payload: dict):
    aid = new_id("an")
    qexec("INSERT INTO analyses(id,user_id,team_a,team_b,competition,match_date,kickoff,status,result_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (aid, user_id, payload["team_a"], payload["team_b"], payload["competition"], payload["match_date"], payload["kickoff"], payload["status"], json.dumps(payload, ensure_ascii=False), iso_now()))
    return aid


@app.get("/health")
def health():
    db_ok = True
    try: qone("SELECT 1 AS ok")
    except Exception: db_ok = False
    return jsonify({"status": "OK" if db_ok else "DEGRADED", "version": APP_VERSION, "database": "postgresql" if DATABASE_URL else "sqlite_fallback", "database_ok": db_ok, "free_web_collector": True, "collector_timeout_seconds": COLLECTOR_TIMEOUT, "search_timeout_seconds": SEARCH_TIMEOUT, "gemini_configured": bool(GEMINI_API_KEY), "admin_configured": bool(ADMIN_TOKEN), "rapidapi_configured": False, "search_grounding": False, "odds_api_configured": False})


@app.get("/")
def root(): return redirect("/app")

@app.get("/app")
def app_page(): return make_response(USER_HTML)

@app.get("/admin")
def admin_page(): return make_response(ADMIN_HTML)

@app.post("/api/admin/create-key")
def admin_create_key():
    denied = require_admin()
    if denied: return denied
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "User").strip()[:100]
    max_uses = max(0, int(data.get("max_uses") or 0)); expiry_days = max(0, int(data.get("expiry_days") or 0))
    user_id = new_id("usr"); key = new_access_key(); now = iso_now(); expires = (utc_now() + timedelta(days=expiry_days)).isoformat() if expiry_days else None
    qexec("INSERT INTO users(id,display_name,created_at) VALUES(?,?,?)", (user_id, name, now))
    qexec("INSERT INTO access_keys(key_hash,key_prefix,user_id,max_uses,uses,expires_at,revoked,created_at) VALUES(?,?,?,?,?,?,0,?)", (sha256(key), key[:11], user_id, max_uses, 0, expires, now))
    return jsonify({"status":"OK","user_id":user_id,"display_name":name,"access_key":key,"max_uses":max_uses,"expiry_days":expiry_days,"expires_at":expires})

@app.get("/api/admin/keys")
def admin_keys():
    denied = require_admin()
    if denied: return denied
    return jsonify(qall("SELECT k.key_prefix,k.max_uses,k.uses,k.expires_at,k.revoked,k.created_at,k.last_used_at,u.id user_id,u.display_name FROM access_keys k JOIN users u ON u.id=k.user_id ORDER BY k.created_at DESC"))

@app.post("/api/admin/revoke-key")
def admin_revoke_key():
    denied = require_admin()
    if denied: return denied
    prefix = str((request.get_json(silent=True) or {}).get("key_prefix") or "").strip()
    if not prefix: return jsonify({"status":"INVALID_REQUEST"}), 400
    return jsonify({"status":"OK","revoked":qexec("UPDATE access_keys SET revoked=1 WHERE key_prefix=?", (prefix,))})

@app.post("/api/login")
def login():
    key = str((request.get_json(silent=True) or {}).get("access_key") or "").strip()
    row = authenticate_access(key)
    if not row: return jsonify({"status":"INVALID","message":"Invalid or inactive access key"}), 401
    user = qone("SELECT * FROM users WHERE id=?", (row["user_id"],))
    resp = jsonify({"status":"OK","user":user,"uses":row["uses"],"max_uses":row["max_uses"],"expires_at":row["expires_at"]})
    resp.set_cookie("quant_access_key", key, httponly=True, samesite="Lax", secure=(APP_ENV == "production"))
    return resp

@app.get("/api/me")
def me():
    u = require_user()
    if not u: return jsonify({"status":"UNAUTHORIZED","message":"Invalid or inactive access key"}), 401
    return jsonify({"status":"OK","user":u})

@app.post("/api/analyze")
def analyze():
    user = require_user()
    if not user: return jsonify({"status":"UNAUTHORIZED","message":"Invalid or inactive access key"}), 401
    data = request.get_json(silent=True) or {}
    a = str(data.get("team_a") or "").strip(); b = str(data.get("team_b") or "").strip(); comp = str(data.get("competition") or "").strip(); date = str(data.get("match_date") or "").strip(); kickoff = str(data.get("kickoff") or "").strip()
    if not all([a,b,comp,date,kickoff]): return jsonify({"status":"INVALID_REQUEST","message":"Thiếu Team A, Team B, Competition, Date hoặc Kickoff GMT+7."}), 400
    future_ok, parsed = validate_future_kickoff(date, kickoff)
    if not future_ok: return jsonify({"status":parsed,"message":"Chỉ phân tích trận có kickoff trong tương lai theo giờ Việt Nam."}), 400
    row = authenticate_access(access_from_request(), consume=True)
    if REQUIRE_ACCESS_KEY and not row: return jsonify({"status":"INVALID","message":"Access Key hết lượt hoặc không còn hoạt động."}), 401
    log.info("[ANALYZE] Request received: %s vs %s | %s | %s %s", a,b,comp,date,kickoff)
    t0 = time.monotonic()
    try:
        log.info("[ANALYZE] Step 1/5 FREE WEB COLLECTOR")
        research = collect_research(a,b,comp,date)
        if not research["sources"]:
            payload = {"status":"NO_WEB_EVIDENCE","message":"Free Web Collector không lấy được trang công khai đủ bằng chứng. Hệ thống không bịa dữ liệu.","research":research,"pipeline":{"research":"FAILED","validation":"NOT_RUN","quant":"NOT_RUN"}}
        else:
            log.info("[ANALYZE] Step 2/5 GEMINI NORMALIZATION")
            extracted = gemini_extract(research,a,b,comp,date,kickoff)
            identity = extracted.get("match_identity") or {}
            log.info("[ANALYZE] Step 3/5 MATCH VALIDATION: %s", "VERIFIED" if identity.get("verified") else "UNVERIFIED")
            if not identity.get("verified"):
                payload = {"status":"MATCH_IDENTITY_UNVERIFIED","message":"Chưa xác minh được chính xác trận đấu từ nguồn công khai.","research":extracted,"sources":research["sources"],"pipeline":{"research":"VERIFIED_PAGES","validation":"FAILED","quant":"NOT_RUN"}}
            else:
                log.info("[ANALYZE] Step 4/5 DETERMINISTIC QUANT ENGINE")
                model = deterministic_model(extracted)
                odds = extracted.get("odds") or {}
                pick = pick_from_odds(model, odds) if model else None
                if not model:
                    status = "FORM_INSUFFICIENT"
                    msg = "Chưa có ít nhất 5 scorelines xác minh cho mỗi đội; Quant Engine không tự tạo lambda."
                elif not any(odds.get(k) is not None for k in ["home","draw","away","over_2_5"]):
                    status = "ODDS_NOT_FOUND"
                    msg = "Không tìm thấy odds hiện tại được ghi rõ trong nguồn công khai; không tính EV."
                elif not pick:
                    status = "NO_BET"
                    msg = "Có model và odds nhưng không có EV dương theo quy tắc deterministic."
                else:
                    status = "OK"; msg = "Primary Pick được phát hành từ dữ liệu đã xác minh."
                log.info("[ANALYZE] Step 5/5 RESULT: %s", status)
                payload = {"status":status,"message":msg,"match_identity":identity,"research":extracted,"model":model,"pick":pick,"sources":research["sources"],"research_errors":research["errors"],"pipeline":{"research":"VERIFIED_PAGES","validation":"VERIFIED","quant":"CALCULATED" if model else "INSUFFICIENT_DATA"}}
        payload.update({"team_a":a,"team_b":b,"competition":comp,"match_date":date,"kickoff":kickoff,"request_time_vn":datetime.now(VN).isoformat(),"elapsed_seconds":round(time.monotonic()-t0,2),"architecture":"Gemini + Free Web Collector + Quant Engine"})
        aid=save_analysis(user["id"],payload); payload["analysis_id"]=aid
        qexec("INSERT INTO usage_logs(id,user_id,action,status,detail,created_at) VALUES(?,?,?,?,?,?)",(new_id("log"),user["id"],"analysis",payload["status"],None,iso_now()))
        return jsonify(payload)
    except Exception as exc:
        log.exception("[ANALYZE] FAILED")
        return jsonify({"status":"RESEARCH_FAILED","message":str(exc),"elapsed_seconds":round(time.monotonic()-t0,2)}),503

@app.get("/api/history")
def history():
    user=require_user()
    if not user: return jsonify({"status":"UNAUTHORIZED"}),401
    return jsonify(qall("SELECT id,team_a,team_b,competition,match_date,kickoff,status,created_at FROM analyses WHERE user_id=? ORDER BY created_at DESC LIMIT 100",(user["id"],)))


USER_HTML = r'''<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#080c14"><title>Soi Kèo AI — Quant Terminal</title><link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Plus+Jakarta+Sans:wght@500;600;700;800&display=swap" rel="stylesheet"><style>
:root{--bg:#080c14;--panel:rgba(17,24,39,.62);--panel2:#0d1420;--line:rgba(255,255,255,.09);--text:#edf2f7;--muted:#8995a8;--gold:#f5b84b;--gold2:#ffe08a;--green:#26d391;--red:#ff6f7d;--cyan:#57d7e8;--shadow:0 24px 80px rgba(0,0,0,.38)}*{box-sizing:border-box}html{background:var(--bg)}body{margin:0;min-height:100vh;background:radial-gradient(circle at 15% -5%,rgba(245,184,75,.12),transparent 30%),radial-gradient(circle at 90% 10%,rgba(37,211,145,.09),transparent 28%),var(--bg);color:var(--text);font-family:Inter,system-ui,sans-serif}button,input{font:inherit}.app{max-width:1240px;margin:auto;padding:22px 22px 100px}.nav{height:64px;display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:34px}.brand{display:flex;align-items:center;gap:11px;font-family:'Plus Jakarta Sans';font-weight:800;letter-spacing:-.03em}.logo{width:38px;height:38px;border-radius:13px;background:linear-gradient(135deg,var(--gold),#fff0ad);display:grid;place-items:center;color:#10151d;box-shadow:0 8px 30px rgba(245,184,75,.18)}.navlinks{display:flex;gap:5px}.navlinks button,.ghost{background:transparent;border:0;color:var(--muted);padding:10px 12px;border-radius:11px;cursor:pointer}.navlinks button:hover,.ghost:hover{color:var(--text);background:rgba(255,255,255,.05)}.status{display:flex;align-items:center;gap:8px;color:#9ba7b9;font-size:12px}.dot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 14px var(--green)}.hero{margin:10px 0 22px}.eyebrow{font-size:11px;letter-spacing:.18em;color:var(--gold);font-weight:700}.hero h1{font-family:'Plus Jakarta Sans';font-size:clamp(30px,5vw,54px);line-height:1.02;letter-spacing:-.055em;margin:9px 0}.hero p{color:var(--muted);max-width:650px;margin:0}.glass,.card{background:linear-gradient(145deg,rgba(255,255,255,.055),rgba(255,255,255,.018));border:1px solid var(--line);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);box-shadow:var(--shadow);border-radius:26px}.research{padding:25px}.cardhead{display:flex;justify-content:space-between;align-items:center;gap:15px;margin-bottom:20px}.cardhead h2,.cardhead h3{margin:0;font-family:'Plus Jakarta Sans';letter-spacing:-.03em}.sub{font-size:12px;color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:12px}.field{grid-column:span 4}.field.small{grid-column:span 3}.field label{display:block;color:#9ca9ba;font-size:11px;margin:0 0 7px 2px}.field input{width:100%;padding:13px 14px;background:rgba(5,10,18,.62);border:1px solid var(--line);border-radius:14px;color:var(--text);outline:none;transition:.2s}.field input:focus{border-color:rgba(245,184,75,.6);box-shadow:0 0 0 4px rgba(245,184,75,.08)}.cta{border:0;border-radius:15px;padding:14px 20px;margin-top:15px;background:linear-gradient(110deg,var(--gold),var(--gold2));color:#12161c;font-weight:800;cursor:pointer;box-shadow:0 12px 32px rgba(245,184,75,.17);transition:.2s}.cta:hover{transform:translateY(-2px);box-shadow:0 16px 40px rgba(245,184,75,.24)}.cta:active{transform:translateY(0) scale(.985)}.pipeline{display:flex;gap:7px;flex-wrap:wrap;margin-top:16px}.step{font-size:10px;color:#6f7d90;border:1px solid var(--line);border-radius:999px;padding:6px 9px}.step.active{color:var(--gold2);border-color:rgba(245,184,75,.35);background:rgba(245,184,75,.07)}.dashboard{display:grid;grid-template-columns:1.1fr 1fr;gap:15px;margin-top:15px}.card{padding:22px}.pick{min-height:210px;display:flex;flex-direction:column;justify-content:space-between}.pickmain{font-family:'Plus Jakarta Sans';font-size:32px;font-weight:800;letter-spacing:-.045em}.badge{display:inline-flex;width:max-content;padding:7px 10px;border-radius:999px;font-size:10px;font-weight:800;letter-spacing:.09em;border:1px solid var(--line)}.verified{color:var(--green);background:rgba(38,211,145,.07)}.nobet{color:var(--gold2);background:rgba(245,184,75,.06)}.danger{color:var(--red)}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.metric{padding:14px;border:1px solid var(--line);background:rgba(255,255,255,.025);border-radius:16px}.metric span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.08em}.metric b{display:block;margin-top:6px;font-size:22px}.tabs{display:flex;gap:7px;overflow:auto;padding-bottom:4px}.tab{border:1px solid var(--line);background:rgba(255,255,255,.025);color:var(--muted);padding:9px 12px;border-radius:11px;white-space:nowrap}.tab.active{color:var(--text);border-color:rgba(245,184,75,.32);background:rgba(245,184,75,.06)}pre{white-space:pre-wrap;word-break:break-word;color:#aeb9c9;font-size:11px;line-height:1.6}.historyrow{display:flex;justify-content:space-between;gap:10px;padding:12px 0;border-bottom:1px solid var(--line);font-size:12px}.muted{color:var(--muted)}.hidden{display:none!important}.login{max-width:600px;margin:14vh auto}.error{color:var(--red);font-size:12px;margin-top:10px}.bottom{display:none}
@media(max-width:760px){.app{padding:14px 14px 92px}.nav{margin-bottom:25px}.navlinks{display:none}.hero h1{font-size:36px}.field,.field.small{grid-column:span 12}.research,.card{border-radius:21px;padding:18px}.dashboard{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(3,1fr)}.metric{padding:11px}.metric b{font-size:18px}.bottom{position:fixed;display:flex;z-index:20;bottom:10px;left:10px;right:10px;justify-content:space-around;padding:8px;border:1px solid var(--line);background:rgba(9,14,23,.82);backdrop-filter:blur(20px);border-radius:20px;box-shadow:var(--shadow)}.bottom button{border:0;background:transparent;color:var(--muted);font-size:10px;padding:8px}.bottom button:first-child{color:var(--gold2)}}
</style></head><body><main class="app"><header class="nav"><div class="brand"><div class="logo">Q</div><span>SOI KÈO AI</span></div><div class="navlinks"><button>HOME</button><button>MATCHES</button><button>PICKS</button><button>MARKETS</button><button>INTELLIGENCE</button></div><div class="status"><span class="dot"></span>SYSTEM ONLINE</div></header>
<section id="login" class="login glass card"><div class="eyebrow">PRIVATE ACCESS</div><h1>Quant Intelligence</h1><p class="muted">Nhập Access Key để mở terminal.</p><div style="display:flex;gap:8px;margin-top:18px"><input id="key" class="field input" style="flex:1;padding:14px;background:#080e17;border:1px solid var(--line);border-radius:14px;color:white" placeholder="QT-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX"><button class="cta" style="margin:0" onclick="login()">ENTER</button></div><div id="loginmsg"></div></section>
<section id="terminal" class="hidden"><section class="hero"><div class="eyebrow">FOOTBALL INTELLIGENCE</div><h1>Research. Verify. Quantify.</h1><p>Gemini chỉ chuẩn hóa bằng chứng. Free Web Collector thu thập nguồn công khai. Quant Engine tự tính toán — không bịa kèo.</p></section>
<section class="glass research"><div class="cardhead"><div><h2>Research Match</h2><div class="sub">Live public-web research · GMT+7</div></div><span id="badge" class="badge">READY</span></div><div class="grid"><div class="field"><label>ĐỘI A</label><input id="a" placeholder="Home team"></div><div class="field"><label>ĐỘI B</label><input id="b" placeholder="Away team"></div><div class="field"><label>GIẢI ĐẤU</label><input id="comp" placeholder="Competition"></div><div class="field small"><label>NGÀY THI ĐẤU</label><input id="date" type="date"></div><div class="field small"><label>KICKOFF GMT+7</label><input id="kick" type="time"></div></div><button class="cta" onclick="analyze()">⌁ FIND & ANALYZE</button><div id="msg" class="sub" style="margin-top:12px"></div><div class="pipeline"><span id="s1" class="step">WEB RESEARCH</span><span id="s2" class="step">GEMINI</span><span id="s3" class="step">MATCH VERIFY</span><span id="s4" class="step">QUANT ENGINE</span><span id="s5" class="step">VALUE / NO BET</span></div></section>
<section id="out"></section><section class="card" style="margin-top:15px"><div class="cardhead"><div><h3>Recent Analyses</h3><div class="sub">Private history for this Access Key</div></div><button class="ghost" onclick="historyLoad()">Refresh</button></div><div id="hist" class="muted">Chưa tải.</div></section></section></main><nav class="bottom"><button>⌂<br>Home</button><button>◉<br>Matches</button><button>◆<br>Picks</button><button>⌁<br>Market</button><button>•••<br>More</button></nav>
<script>const $=id=>document.getElementById(id);const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));function setStep(n){for(let i=1;i<=5;i++)$('s'+i).classList.toggle('active',i===n)}async function login(){let r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({access_key:$('key').value.trim()})});let d=await r.json();if(!r.ok){$('loginmsg').innerHTML='<div class="error">'+esc(d.message||'Invalid Access Key')+'</div>';return}$('login').classList.add('hidden');$('terminal').classList.remove('hidden');historyLoad()}async function analyze(){let p={team_a:$('a').value.trim(),team_b:$('b').value.trim(),competition:$('comp').value.trim(),match_date:$('date').value,kickoff:$('kick').value};$('out').innerHTML='';$('badge').textContent='RESEARCHING';$('msg').textContent='Đang thu thập nguồn công khai…';setStep(1);let r;let controller=new AbortController();let timer=setTimeout(()=>controller.abort(),75000);try{r=await fetch('/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p),signal:controller.signal});$('msg').textContent='Đang xác minh và chạy Quant Engine…';setStep(3)}catch(e){$('badge').textContent=e.name==='AbortError'?'TIMEOUT':'FAILED';$('msg').textContent=e.name==='AbortError'?'Nghiên cứu vượt quá thời gian cho phép. Hệ thống đã dừng request để tránh loading vô hạn.':'Network error';return}finally{clearTimeout(timer)}let d=await r.json();if(d.status==='OK'){setStep(5);$('badge').textContent='VALUE'}else if(d.status==='NO_BET'){setStep(5);$('badge').textContent='NO BET'}else{$('badge').textContent=d.status;setStep(d.pipeline&&d.pipeline.quant==='CALCULATED'?4:3)}if(!r.ok){$('out').innerHTML='<div class="card error">'+esc(d.message||d.status)+'</div>';return}render(d)}function render(d){let p=d.pick,m=d.model;let html='<div class="dashboard"><section class="card pick"><div><span class="badge '+(d.status==='OK'?'verified':'nobet')+'">'+esc(d.status)+'</span><div class="sub" style="margin-top:17px">PRIMARY PICK</div><div class="pickmain">'+esc(p?p.market.replaceAll('_',' ').toUpperCase():'NO BET')+'</div><p class="muted">'+esc(d.message||'')+'</p></div><div class="metrics"><div class="metric"><span>Model</span><b>'+esc(p?p.probability.toFixed(2)+'%':'—')+'</b></div><div class="metric"><span>Market</span><b>'+esc(p?p.market_probability.toFixed(2)+'%':'—')+'</b></div><div class="metric"><span>EV</span><b>'+esc(p?(p.ev>=0?'+':'')+p.ev.toFixed(2)+'%':'—')+'</b></div></div></section><section class="card"><div class="cardhead"><div><h3>'+esc(d.team_a)+' vs '+esc(d.team_b)+'</h3><div class="sub">'+esc(d.competition)+' · '+esc(d.match_date)+' · '+esc(d.kickoff)+' GMT+7</div></div><span class="badge">'+esc(d.pipeline?.validation||'—')+'</span></div><div class="tabs"><span class="tab active">MODEL</span><span class="tab">FORM</span><span class="tab">XG</span><span class="tab">LINEUP</span><span class="tab">ODDS</span><span class="tab">NEWS</span></div><div style="margin-top:16px" class="metrics"><div class="metric"><span>λ Home</span><b>'+esc(m?m.lambda_home.toFixed(2):'—')+'</b></div><div class="metric"><span>λ Away</span><b>'+esc(m?m.lambda_away.toFixed(2):'—')+'</b></div><div class="metric"><span>Data</span><b style="font-size:12px">'+esc(m?m.data_quality:'INSUFFICIENT')+'</b></div></div></section></div><section class="card" style="margin-top:15px"><div class="cardhead"><div><h3>Research Evidence</h3><div class="sub">Gemini-normalized public sources · no fabricated values</div></div></div><details><summary>View Full Analysis JSON</summary><pre>'+esc(JSON.stringify(d,null,2))+'</pre></details></section>';$('out').innerHTML=html}async function historyLoad(){let r=await fetch('/api/history');if(!r.ok)return;let d=await r.json();$('hist').innerHTML=d.length?d.map(x=>'<div class="historyrow"><span>'+esc(x.team_a)+' vs '+esc(x.team_b)+'</span><span>'+esc(x.status)+' · '+esc(x.created_at)+'</span></div>').join(''):'Chưa có lịch sử'}(async()=>{let r=await fetch('/api/me');if(r.ok){$('login').classList.add('hidden');$('terminal').classList.remove('hidden');historyLoad()}})();</script></body></html>'''

ADMIN_HTML = r'''<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#080c14"><title>Admin — Soi Kèo AI</title><style>body{margin:0;background:#080c14;color:#edf2f7;font:14px Inter,system-ui,sans-serif}.wrap{max-width:1050px;margin:auto;padding:25px}.card{background:rgba(17,24,39,.7);border:1px solid rgba(255,255,255,.09);border-radius:24px;padding:20px;margin:14px 0;backdrop-filter:blur(16px)}input,button{padding:12px;border-radius:12px;border:1px solid rgba(255,255,255,.1);background:#0b121e;color:#fff}button{background:linear-gradient(110deg,#f5b84b,#ffe08a);color:#111;font-weight:800;cursor:pointer}table{width:100%;border-collapse:collapse}td,th{padding:11px;text-align:left;border-bottom:1px solid rgba(255,255,255,.08)}.mono{font-family:monospace;word-break:break-all}.ok{color:#26d391}.bad{color:#ff6f7d}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}</style></head><body><div class="wrap"><h1>🔐 SOI KÈO AI — ADMIN</h1><p style="color:#8995a8">Access Key management · Gemini + Free Web Collector · Quant Engine</p><section class="card"><div class="grid"><input id="admin" placeholder="ADMIN_TOKEN"><input id="name" placeholder="Tên user"><input id="uses" type="number" min="0" value="100"><input id="days" type="number" min="0" value="30"></div><br><button onclick="createKey()">Generate Access Key</button> <button onclick="loadKeys()">Refresh</button><div id="msg"></div></section><section id="newkey" class="card" style="display:none"></section><section class="card"><table><thead><tr><th>User</th><th>Key</th><th>Uses</th><th>Expiry</th><th>Status</th></tr></thead><tbody id="rows"></tbody></table></section></div><script>const $=x=>document.getElementById(x);async function createKey(){let r=await fetch('/api/admin/create-key',{method:'POST',headers:{'Content-Type':'application/json','X-Admin-Token':$('admin').value},body:JSON.stringify({name:$('name').value,max_uses:Number($('uses').value||0),expiry_days:Number($('days').value||0)})});let d=await r.json();if(!r.ok){$('msg').innerHTML='<span class="bad">'+(d.message||d.status)+'</span>';return}$('newkey').style.display='block';$('newkey').innerHTML='<b>ACCESS KEY — copy now</b><p class="mono ok">'+d.access_key+'</p><small>Chỉ hiển thị một lần.</small>';$('msg').textContent='Created';loadKeys()}async function loadKeys(){let r=await fetch('/api/admin/keys',{headers:{'X-Admin-Token':$('admin').value}});let d=await r.json();if(!r.ok){$('msg').innerHTML='<span class="bad">'+(d.message||d.status)+'</span>';return}$('rows').innerHTML=d.map(x=>'<tr><td>'+x.display_name+'</td><td class="mono">'+x.key_prefix+'…</td><td>'+x.uses+' / '+(x.max_uses||'∞')+'</td><td>'+(x.expires_at||'∞')+'</td><td>'+(!x.revoked?'ACTIVE':'REVOKED')+'</td></tr>').join('')}</script></body></html>'''

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
