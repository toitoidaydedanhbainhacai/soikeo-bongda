from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import secrets
import statistics
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests
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

APP_VERSION = "quant-terminal-2026.09.24"
MODEL_VERSION = "poisson-form-gemini-research-v1"
VN = ZoneInfo("Asia/Ho_Chi_Minh")
UTC = timezone.utc
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "").strip()
RAPIDAPI_HOST = "google-search74.p.rapidapi.com"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
APP_ENV = os.getenv("APP_ENV", "production").strip().lower()
REQUIRE_ACCESS_KEY = os.getenv("REQUIRE_ACCESS_KEY", "1").strip().lower() not in {"0", "false", "no"}
MAX_SEARCH_PAGES = max(1, min(int(os.getenv("MAX_SEARCH_PAGES", "2")), 5))
HTTP_TIMEOUT = 15

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("quant-terminal")
app = Flask(__name__)

# ---------------- DB ----------------
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
    """Create the current schema and migrate older Render/Postgres schemas safely.

    The previous deployment could have created an access_keys table without user_id.
    CREATE TABLE IF NOT EXISTS does not alter an existing table, so indexes/FKs that
    reference new columns would fail during startup. This migration adds missing
    columns first and preserves legacy keys/data where possible.
    """
    with db() as conn:
        if DATABASE_URL:
            # Create base tables first. Keep columns nullable during migration so old
            # rows can be upgraded without requiring impossible NOT NULL values.
            conn.execute("""CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS access_keys (
                key_hash TEXT PRIMARY KEY,
                key_prefix TEXT,
                user_id TEXT,
                max_uses INTEGER DEFAULT 0,
                uses INTEGER DEFAULT 0,
                expires_at TEXT,
                revoked INTEGER DEFAULT 0,
                created_at TEXT,
                last_used_at TEXT
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS analyses (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                team_a TEXT,
                team_b TEXT,
                competition TEXT,
                match_date TEXT,
                kickoff TEXT,
                status TEXT,
                result_json TEXT,
                created_at TEXT
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS usage_logs (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                action TEXT,
                status TEXT,
                detail TEXT,
                created_at TEXT
            )""")

            # Add every column expected by the current application to legacy tables.
            migrations = {
                "users": {
                    "display_name": "TEXT",
                    "created_at": "TEXT",
                },
                "access_keys": {
                    "key_hash": "TEXT",
                    "key_prefix": "TEXT",
                    "user_id": "TEXT",
                    "max_uses": "INTEGER DEFAULT 0",
                    "uses": "INTEGER DEFAULT 0",
                    "expires_at": "TEXT",
                    "revoked": "INTEGER DEFAULT 0",
                    "created_at": "TEXT",
                    "last_used_at": "TEXT",
                },
                "analyses": {
                    "id": "TEXT",
                    "user_id": "TEXT",
                    "team_a": "TEXT",
                    "team_b": "TEXT",
                    "competition": "TEXT",
                    "match_date": "TEXT",
                    "kickoff": "TEXT",
                    "status": "TEXT",
                    "result_json": "TEXT",
                    "created_at": "TEXT",
                },
                "usage_logs": {
                    "id": "TEXT",
                    "user_id": "TEXT",
                    "action": "TEXT",
                    "status": "TEXT",
                    "detail": "TEXT",
                    "created_at": "TEXT",
                },
            }
            for table, columns in migrations.items():
                existing = {r["column_name"] for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=%s", (table,)
                ).fetchall()}
                for column, definition in columns.items():
                    if column not in existing:
                        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')

            # Ensure a legacy-user bucket exists for keys created by older builds.
            legacy_user = "legacy_admin"
            now = iso_now()
            conn.execute(
                "INSERT INTO users(id,display_name,created_at) VALUES(%s,%s,%s) "
                "ON CONFLICT (id) DO NOTHING",
                (legacy_user, "Legacy / migrated user", now),
            )

            # Migrate plaintext legacy key_code/status columns when they exist.
            access_cols = {r["column_name"] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='access_keys'"
            ).fetchall()}
            if "key_code" in access_cols:
                legacy_rows = conn.execute(
                    "SELECT key_code, status, expires_at, created_at FROM access_keys "
                    "WHERE key_code IS NOT NULL"
                ).fetchall()
                for legacy in legacy_rows:
                    key_code = legacy["key_code"]
                    status = legacy["status"]
                    expires_at = legacy["expires_at"]
                    created_at = legacy["created_at"]
                    if not key_code:
                        continue
                    kh = sha256(str(key_code))
                    revoked = 0 if str(status or "ACTIVE").upper() == "ACTIVE" else 1
                    prefix = str(key_code)
                    conn.execute(
                        "UPDATE access_keys SET key_hash=%s,key_prefix=%s,user_id=COALESCE(user_id,%s),"
                        "max_uses=COALESCE(max_uses,0),uses=COALESCE(uses,0),expires_at=COALESCE(expires_at,%s),"
                        "revoked=COALESCE(revoked,%s),created_at=COALESCE(created_at,%s) WHERE key_code=%s",
                        (kh, prefix, legacy_user, expires_at, revoked, created_at or now, key_code),
                    )

            # Fill missing values on rows from the partially migrated schema.
            conn.execute("UPDATE access_keys SET user_id=%s WHERE user_id IS NULL", (legacy_user,))
            conn.execute("UPDATE access_keys SET max_uses=0 WHERE max_uses IS NULL")
            conn.execute("UPDATE access_keys SET uses=0 WHERE uses IS NULL")
            conn.execute("UPDATE access_keys SET revoked=0 WHERE revoked IS NULL")
            conn.execute("UPDATE access_keys SET created_at=%s WHERE created_at IS NULL", (now,))

            conn.execute("CREATE INDEX IF NOT EXISTS idx_access_user ON access_keys(user_id)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_access_key_hash ON access_keys(key_hash) WHERE key_hash IS NOT NULL")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_analysis_user ON analyses(user_id, created_at DESC)")
            conn.commit()
        else:
            import sqlite3
            conn.executescript(SCHEMA)
            conn.commit()


def adapt_sql(sql: str) -> str:
    return sql.replace("?", "%s") if DATABASE_URL else sql


def qone(sql: str, params=()):
    with db() as conn:
        cur = conn.execute(adapt_sql(sql), params)
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None


def qall(sql: str, params=()):
    with db() as conn:
        cur = conn.execute(adapt_sql(sql), params)
        rows = [dict(x) for x in cur.fetchall()]
        return rows


def qexec(sql: str, params=()):
    with db() as conn:
        cur = conn.execute(adapt_sql(sql), params)
        conn.commit()
        return cur.rowcount


# ---------------- Auth / keys ----------------
def sha256(s: str) -> str:
    return hashlib.sha256(s.strip().encode()).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(10)}"


def new_access_key() -> str:
    # Human-copyable but cryptographically random.
    raw = secrets.token_urlsafe(24).replace("-", "").replace("_", "")[:28].upper()
    return "QT-" + "-".join(raw[i:i+7] for i in range(0, 28, 7))


def require_admin() -> Optional[Any]:
    if not ADMIN_TOKEN:
        return jsonify({"status": "ADMIN_NOT_CONFIGURED", "message": "ADMIN_TOKEN is not configured."}), 503
    supplied = request.headers.get("X-Admin-Token", "") or request.args.get("admin_token", "")
    if not supplied or not secrets.compare_digest(supplied, ADMIN_TOKEN):
        return jsonify({"status": "ADMIN_UNAUTHORIZED", "message": "Invalid admin token."}), 401
    return None


def access_from_request() -> str:
    return (request.headers.get("X-Access-Key", "") or request.args.get("access_key", "") or request.cookies.get("quant_access_key", "")).strip()


def authenticate_access(key: str, consume: bool = False) -> Optional[dict]:
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


def require_user(consume: bool = False):
    if not REQUIRE_ACCESS_KEY:
        return {"id": "dev_user", "display_name": "Development"}
    row = authenticate_access(access_from_request(), consume=consume)
    if not row:
        return None
    return qone("SELECT * FROM users WHERE id=?", (row["user_id"],))


# ---------------- RapidAPI Google Search74 ----------------
def rapid_search(query: str, limit: int = 10) -> dict:
    if not RAPIDAPI_KEY:
        raise RuntimeError("RAPIDAPI_KEY is not configured")
    headers = {"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": RAPIDAPI_HOST}
    all_results = []
    cursor = None
    for _ in range(MAX_SEARCH_PAGES):
        params = {"query": query, "limit": min(max(1, limit), 100), "related_keywords": "false"}
        if cursor:
            params["cursor"] = cursor
        r = requests.get("https://google-search74.p.rapidapi.com/", headers=headers, params=params, timeout=HTTP_TIMEOUT)
        if r.status_code in (401, 403):
            raise RuntimeError("RAPIDAPI_AUTH_ERROR")
        if r.status_code == 429:
            raise RuntimeError("RAPIDAPI_RATE_LIMITED")
        if r.status_code >= 500:
            raise RuntimeError(f"RAPIDAPI_UPSTREAM_ERROR:{r.status_code}")
        if r.status_code >= 400:
            raise RuntimeError(f"RAPIDAPI_HTTP_{r.status_code}")
        data = r.json()
        for item in data.get("results") or []:
            if isinstance(item, dict) and item.get("url"):
                all_results.append({
                    "url": item.get("url"),
                    "title": item.get("title"),
                    "description": item.get("description"),
                    "timestamp": item.get("timestamp"),
                })
        cursor = data.get("next_cursor")
        if not cursor:
            break
    # Deduplicate URLs while preserving order.
    seen, out = set(), []
    for x in all_results:
        if x["url"] in seen:
            continue
        seen.add(x["url"]); out.append(x)
    return {"query": query, "results": out}


def build_queries(a: str, b: str, comp: str, date: str) -> list[str]:
    base = f'"{a}" "{b}" "{comp}" "{date}"'
    return [
        base,
        f'{base} odds Asian handicap over under',
        f'{base} predicted lineup injuries suspension team news',
        f'{base} xG xGA statistics form',
        f'{base} site:sofascore.com OR site:fotmob.com',
        f'{base} site:fbref.com OR site:understat.com',
    ]


def collect_research(a: str, b: str, comp: str, date: str) -> dict:
    sources = []
    errors = []
    for q in build_queries(a, b, comp, date):
        try:
            res = rapid_search(q, limit=10)
            sources.extend(res["results"])
        except Exception as exc:
            errors.append(str(exc))
    # URL dedupe
    seen, unique = set(), []
    for s in sources:
        if s["url"] not in seen:
            seen.add(s["url"]); unique.append(s)
    return {"sources": unique[:80], "errors": errors}


# ---------------- Gemini extraction ----------------
def gemini_extract(research: dict, a: str, b: str, comp: str, date: str, kickoff: str) -> dict:
    if genai is None or genai_types is None or not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_NOT_CONFIGURED")
    client = genai.Client(api_key=GEMINI_API_KEY)
    evidence = "\n".join(
        f"[{i+1}] {s.get('title','')} | {s.get('url','')} | {s.get('description','')} | {s.get('timestamp','')}"
        for i, s in enumerate(research["sources"])
    )
    schema = {
        "match_identity": {"verified": False, "home": a, "away": b, "competition": comp, "date": date, "kickoff": kickoff},
        "form": {"home_last5": [], "away_last5": [], "home_last10": [], "away_last10": []},
        "odds": {"home": None, "draw": None, "away": None, "over_2_5": None, "under_2_5": None, "asian_handicap": []},
        "stats": {"home_xg": None, "away_xg": None, "home_xga": None, "away_xga": None, "home_corners": None, "away_corners": None, "home_cards": None, "away_cards": None},
        "team_news": [], "injuries": [], "suspensions": [], "expected_lineups": [],
        "odds_snapshots": [], "source_notes": [], "confidence": "LOW"
    }
    prompt = f"""You are a strict evidence extraction engine for a football research system.\n\nMATCH: {a} vs {b}\nCOMPETITION: {comp}\nDATE: {date}\nKICKOFF GMT+7: {kickoff}\n\nSEARCH RESULTS (snippets only; URLs are evidence references):\n{evidence}\n\nReturn ONLY valid JSON matching this schema:\n{json.dumps(schema, ensure_ascii=False)}\n\nRules:\n1) Extract only facts explicitly supported by the supplied snippets. Never guess or fill missing numbers.\n2) A numeric field must remain null unless a snippet explicitly states it.\n3) Never treat a search result as an odds movement snapshot unless it contains an explicit odds value plus timestamp/date/source.\n4) match_identity.verified=true only when team names, competition and date/kickoff are sufficiently consistent.\n5) Do not create lineups, injuries, form results, odds or xG from general knowledge.\n6) Preserve source URL in source_notes for important extracted facts.\n"""
    cfg = genai_types.GenerateContentConfig(temperature=0, response_mime_type="application/json")
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=cfg)
    text = response.text if response and response.text else ""
    try:
        data = json.loads(text)
    except Exception as exc:
        raise RuntimeError("GEMINI_INVALID_JSON") from exc
    return data


# ---------------- Quant engine ----------------
def poisson_p(lmbda: float, goals: int) -> float:
    return (lmbda ** goals) * math.exp(-lmbda) / math.factorial(goals)


def poisson_probs(lh: float, la: float, max_goals: int = 10) -> tuple[float, float, float, float]:
    ph = [poisson_p(lh, i) for i in range(max_goals + 1)]
    pa = [poisson_p(la, i) for i in range(max_goals + 1)]
    m = [[ph[i] * pa[j] for j in range(max_goals + 1)] for i in range(max_goals + 1)]
    z = sum(map(sum, m))
    m = [[x / z for x in row] for row in m]
    home = sum(m[i][j] for i in range(len(m)) for j in range(len(m)) if i > j)
    draw = sum(m[i][j] for i in range(len(m)) for j in range(len(m)) if i == j)
    away = sum(m[i][j] for i in range(len(m)) for j in range(len(m)) if i < j)
    over25 = sum(m[i][j] for i in range(len(m)) for j in range(len(m)) if i + j >= 3)
    return home, draw, away, over25


def model_from_verified_form(form: dict) -> Optional[dict]:
    # Requires at least five explicit scorelines for each team.
    def vals(key):
        arr = form.get(key) or []
        out=[]
        for x in arr:
            try: out.append(float(x))
            except Exception: pass
        return out
    hg = vals("home_last5"); ag = vals("away_last5")
    if len(hg) < 5 or len(ag) < 5:
        return None
    # The extractor stores total goals only if explicitly provided; support dict scorelines too.
    if all(isinstance(x, (int,float)) for x in hg+ag):
        # This branch is intentionally not used because a bare list is not enough to derive attack/defence.
        return None
    return None


def deterministic_model(research_json: dict) -> Optional[dict]:
    # Accept explicit per-match score objects: {gf,ga}. No fixed lambda.
    hf = research_json.get("form", {}).get("home_last5") or []
    af = research_json.get("form", {}).get("away_last5") or []
    if len(hf) < 5 or len(af) < 5:
        return None
    try:
        hgf=[float(x["gf"]) for x in hf if "gf" in x and "ga" in x]
        hga=[float(x["ga"]) for x in hf if "gf" in x and "ga" in x]
        agf=[float(x["gf"]) for x in af if "gf" in x and "ga" in x]
        aga=[float(x["ga"]) for x in af if "gf" in x and "ga" in x]
        if min(map(len,[hgf,hga,agf,aga])) < 5: return None
        lh=max(0.15,min(4.5,0.65*statistics.mean(hgf)+0.35*statistics.mean(aga)))
        la=max(0.15,min(4.5,0.65*statistics.mean(agf)+0.35*statistics.mean(hga)))
        ph,pd,pa,po=poisson_probs(lh,la)
        return {"lambda_home":lh,"lambda_away":la,"prob_home":ph,"prob_draw":pd,"prob_away":pa,"prob_over_2_5":po,"data_quality":"FORM_VERIFIED_5+"}
    except Exception:
        return None


def ev(prob: float, odds: float) -> float:
    return prob * odds - 1.0


def pick_from_odds(model: dict, odds: dict) -> Optional[dict]:
    candidates=[]
    for key, pkey in [("home","prob_home"),("draw","prob_draw"),("away","prob_away"),("over_2_5","prob_over_2_5")]:
        try: o=float(odds[key]) if odds.get(key) is not None else None
        except Exception: o=None
        if o and o > 1 and model.get(pkey) is not None:
            candidates.append((ev(float(model[pkey]),o),key,float(model[pkey]),o))
    if not candidates: return None
    candidates.sort(reverse=True)
    best=candidates[0]
    if best[0] <= 0: return None
    return {"market":best[1],"probability":round(best[2]*100,4),"odds":best[3],"ev":round(best[0]*100,4),"status":"VALUE"}


# ---------------- API ----------------
def save_analysis(user_id: str, payload: dict):
    aid = new_id("an")
    qexec("INSERT INTO analyses(id,user_id,team_a,team_b,competition,match_date,kickoff,status,result_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
          (aid,user_id,payload["team_a"],payload["team_b"],payload["competition"],payload["match_date"],payload["kickoff"],payload["status"],json.dumps(payload,ensure_ascii=False),iso_now()))
    return aid


@app.get("/health")
def health():
    db_ok=True
    try:
        qone("SELECT 1 AS ok")
    except Exception:
        db_ok=False
    return jsonify({"status":"OK" if db_ok else "DEGRADED","version":APP_VERSION,"database":"postgresql" if DATABASE_URL else "sqlite_fallback","database_ok":db_ok,"rapidapi_configured":bool(RAPIDAPI_KEY),"gemini_configured":bool(GEMINI_API_KEY),"admin_configured":bool(ADMIN_TOKEN),"odds_api_configured":False})


@app.get("/")
def root():
    return redirect("/app")


@app.get("/app")
def app_page():
    return make_response(USER_HTML)


@app.get("/admin")
def admin_page():
    return make_response(ADMIN_HTML)


@app.post("/api/admin/create-key")
def admin_create_key():
    denied=require_admin()
    if denied: return denied
    data=request.get_json(silent=True) or {}
    name=str(data.get("name") or "User").strip()[:100]
    max_uses=max(0,int(data.get("max_uses") or 0))
    expiry_days=max(0,int(data.get("expiry_days") or 0))
    user_id=new_id("usr")
    key=new_access_key()
    now=iso_now()
    expires=(utc_now()+timedelta(days=expiry_days)).isoformat() if expiry_days else None
    qexec("INSERT INTO users(id,display_name,created_at) VALUES(?,?,?)",(user_id,name,now))
    qexec("INSERT INTO access_keys(key_hash,key_prefix,user_id,max_uses,uses,expires_at,revoked,created_at) VALUES(?,?,?,?,?,?,0,?)",
          (sha256(key),key[:11],user_id,max_uses,0,expires,now))
    return jsonify({"status":"OK","user_id":user_id,"display_name":name,"access_key":key,"max_uses":max_uses,"expiry_days":expiry_days,"expires_at":expires})


@app.get("/api/admin/keys")
def admin_keys():
    denied=require_admin()
    if denied: return denied
    rows=qall("SELECT k.key_prefix,k.max_uses,k.uses,k.expires_at,k.revoked,k.created_at,k.last_used_at,u.id user_id,u.display_name FROM access_keys k JOIN users u ON u.id=k.user_id ORDER BY k.created_at DESC")
    return jsonify(rows)


@app.post("/api/admin/revoke-key")
def admin_revoke_key():
    denied=require_admin()
    if denied: return denied
    data=request.get_json(silent=True) or {}
    prefix=str(data.get("key_prefix") or "").strip()
    if not prefix: return jsonify({"status":"INVALID_REQUEST"}),400
    n=qexec("UPDATE access_keys SET revoked=1 WHERE key_prefix=?",(prefix,))
    return jsonify({"status":"OK","revoked":n})


@app.post("/api/login")
def login():
    key=str((request.get_json(silent=True) or {}).get("access_key") or "").strip()
    row=authenticate_access(key, consume=False)
    if not row: return jsonify({"status":"INVALID","message":"Invalid or inactive access key"}),401
    user=qone("SELECT * FROM users WHERE id=?",(row["user_id"],))
    resp=jsonify({"status":"OK","user":user,"uses":row["uses"],"max_uses":row["max_uses"],"expires_at":row["expires_at"]})
    resp.set_cookie("quant_access_key",key,httponly=True,samesite="Lax",secure=(APP_ENV=="production"))
    return resp


@app.get("/api/me")
def me():
    u=require_user(False)
    if not u: return jsonify({"status":"UNAUTHORIZED","message":"Invalid or inactive access key"}),401
    return jsonify({"status":"OK","user":u})


@app.post("/api/analyze")
def analyze():
    user=require_user(consume=False)
    if not user: return jsonify({"status":"UNAUTHORIZED","message":"Invalid or inactive access key"}),401
    data=request.get_json(silent=True) or {}
    a=str(data.get("team_a") or "").strip(); b=str(data.get("team_b") or "").strip(); comp=str(data.get("competition") or "").strip(); date=str(data.get("match_date") or "").strip(); kickoff=str(data.get("kickoff") or "").strip()
    if not all([a,b,comp,date,kickoff]): return jsonify({"status":"INVALID_REQUEST","message":"Thiếu Team A, Team B, Competition, Date hoặc Kickoff GMT+7."}),400
    # Consume exactly one use only after basic validation; failed research still counts as an analysis request.
    key=access_from_request(); row=authenticate_access(key,consume=True)
    if not row: return jsonify({"status":"INVALID","message":"Access Key hết lượt hoặc không còn hoạt động."}),401
    try:
        research=collect_research(a,b,comp,date)
        if not research["sources"]:
            payload={"status":"NO_SEARCH_RESULTS","message":"Không có kết quả tìm kiếm đủ để xác minh trận.","sources":[],"research_errors":research["errors"]}
        else:
            extracted=gemini_extract(research,a,b,comp,date,kickoff)
            identity=extracted.get("match_identity") or {}
            if not identity.get("verified"):
                payload={"status":"MATCH_IDENTITY_UNVERIFIED","message":"Chưa xác minh được chính xác trận đấu.","research":extracted,"sources":research["sources"]}
            else:
                model=deterministic_model(extracted)
                odds=extracted.get("odds") or {}
                pick=pick_from_odds(model,odds) if model else None
                status="OK" if pick else ("FORM_INSUFFICIENT" if not model else "NO_BET")
                payload={"status":status,"match_identity":identity,"research":extracted,"model":model,"pick":pick,"sources":research["sources"],"research_errors":research["errors"]}
        payload.update({"team_a":a,"team_b":b,"competition":comp,"match_date":date,"kickoff":kickoff})
        aid=save_analysis(user["id"],payload); payload["analysis_id"]=aid
        qexec("INSERT INTO usage_logs(id,user_id,action,status,detail,created_at) VALUES(?,?,?,?,?,?)",(new_id("log"),user["id"],"analysis",payload["status"],None,iso_now()))
        return jsonify(payload)
    except Exception as exc:
        log.exception("analysis failed")
        return jsonify({"status":"RESEARCH_FAILED","message":str(exc)}),503


@app.get("/api/history")
def history():
    user=require_user(False)
    if not user: return jsonify({"status":"UNAUTHORIZED"}),401
    rows=qall("SELECT id,team_a,team_b,competition,match_date,kickoff,status,created_at FROM analyses WHERE user_id=? ORDER BY created_at DESC LIMIT 100",(user["id"],))
    return jsonify(rows)


USER_HTML = r'''<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Quant Terminal</title><style>body{margin:0;background:#080d18;color:#eaf0fb;font-family:Inter,system-ui,sans-serif}.wrap{max-width:1100px;margin:auto;padding:24px}.card{background:#101827;border:1px solid #26344e;border-radius:16px;padding:18px;margin:14px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}.field{display:flex;flex-direction:column;gap:7px}input,button{padding:12px;border-radius:10px;border:1px solid #33435f;background:#0c1422;color:#fff}button{background:#2563eb;font-weight:700;cursor:pointer}.muted{color:#8d9ab0}.pick{padding:14px;border:1px solid #1e7257;background:#0c211b;border-radius:12px}.bad{color:#ff9c9c}.ok{color:#7be6b5}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.hidden{display:none}.mono{font-family:ui-monospace,monospace;word-break:break-all}.metric{background:#151f31;border-radius:10px;padding:10px}.metric b{display:block;margin-top:5px}.top{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}</style></head><body><div class="wrap"><div class="top"><div><h1>⚽ Quant Terminal</h1><div class="muted">RapidAPI Google Search74 → Gemini → deterministic Quant Engine → No-Bet</div></div><a href="/admin" style="color:#8fb7ff">Admin</a></div><section id="login" class="card"><h2>Access Key</h2><div class="row"><input id="key" class="mono" style="flex:1" placeholder="QT-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX"><button onclick="login()">Đăng nhập</button></div><div id="loginmsg" class="muted"></div></section><section id="terminal" class="hidden"><section class="card"><h2>Research Match</h2><div class="grid"><div class="field"><label>Đội A</label><input id="a"></div><div class="field"><label>Đội B</label><input id="b"></div><div class="field"><label>Giải đấu</label><input id="comp"></div><div class="field"><label>Ngày thi đấu</label><input id="date" type="date"></div><div class="field"><label>Kickoff GMT+7</label><input id="kick" type="time"></div></div><br><button onclick="analyze()">🔎 Tìm & Phân tích</button><span id="msg" class="muted" style="margin-left:12px"></span></section><section id="out"></section><section class="card"><h3>History</h3><button onclick="historyLoad()">Refresh</button><pre id="hist" class="mono muted"></pre></section></section></div><script>const $=id=>document.getElementById(id);function esc(v){return String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]))}async function login(){let r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({access_key:$('key').value})});let d=await r.json();if(!r.ok){$('loginmsg').innerHTML='<span class="bad">'+esc(d.message)+'</span>';return}$('login').classList.add('hidden');$('terminal').classList.remove('hidden');$('loginmsg').textContent=''}async function analyze(){let p={team_a:$('a').value,team_b:$('b').value,competition:$('comp').value,match_date:$('date').value,kickoff:$('kick').value};$('msg').textContent='Đang nghiên cứu…';$('out').innerHTML='';let r=await fetch('/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});let d=await r.json();$('msg').textContent=d.status;if(!r.ok){$('out').innerHTML='<div class="card bad">'+esc(d.message||'Lỗi')+'</div>';return}let html='<section class="card"><div class="top"><h2>'+esc(d.team_a)+' vs '+esc(d.team_b)+'</h2><b>'+esc(d.status)+'</b></div>';if(d.pick){html+='<div class="pick"><b>Primary Pick: '+esc(d.pick.market)+'</b><br>Odds '+Number(d.pick.odds).toFixed(2)+' · Model '+Number(d.pick.probability).toFixed(2)+'% · EV '+Number(d.pick.ev).toFixed(2)+'%</div>'}else html+='<p class="muted">NO BET — hệ thống không đủ bằng chứng/giá trị để phát hành kèo.</p>';html+='<details><summary>Full research JSON</summary><pre class="mono">'+esc(JSON.stringify(d,null,2))+'</pre></details></section>';$('out').innerHTML=html}async function historyLoad(){let r=await fetch('/api/history');let d=await r.json();$('hist').textContent=JSON.stringify(d,null,2)}(async()=>{let r=await fetch('/api/me');if(r.ok){$('login').classList.add('hidden');$('terminal').classList.remove('hidden')}})();</script></body></html>'''

ADMIN_HTML = r'''<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Quant Terminal Admin</title><style>body{margin:0;background:#080d18;color:#eaf0fb;font-family:Inter,system-ui,sans-serif}.wrap{max-width:1000px;margin:auto;padding:24px}.card{background:#101827;border:1px solid #26344e;border-radius:16px;padding:18px;margin:14px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}input,button{padding:11px;border-radius:9px;border:1px solid #33435f;background:#0c1422;color:#fff}button{background:#2563eb;font-weight:700;cursor:pointer}.mono{font-family:ui-monospace,monospace;word-break:break-all}.bad{color:#ff9c9c}.ok{color:#7be6b5}table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #26344e;text-align:left}</style></head><body><div class="wrap"><h1>🔐 Quant Terminal Admin</h1><section class="card"><div class="grid"><input id="admin" class="mono" placeholder="ADMIN_TOKEN"><input id="name" placeholder="Tên user"><input id="uses" type="number" min="0" value="100" placeholder="Max uses"><input id="days" type="number" min="0" value="30" placeholder="Expiry days"></div><br><button onclick="createKey()">Generate Access Key</button><button onclick="loadKeys()" style="margin-left:8px">Refresh</button><div id="msg"></div></section><section id="newkey" class="card hidden"></section><section class="card"><table><thead><tr><th>User</th><th>Key</th><th>Uses</th><th>Expiry</th><th>Status</th></tr></thead><tbody id="rows"></tbody></table></section></div><script>const $=x=>document.getElementById(x);async function createKey(){let t=$('admin').value;let r=await fetch('/api/admin/create-key',{method:'POST',headers:{'Content-Type':'application/json','X-Admin-Token':t},body:JSON.stringify({name:$('name').value,max_uses:Number($('uses').value||0),expiry_days:Number($('days').value||0)})});let d=await r.json();if(!r.ok){$('msg').innerHTML='<span class="bad">'+(d.message||d.status)+'</span>';return}$('newkey').classList.remove('hidden');$('newkey').innerHTML='<b>ACCESS KEY — copy and send to user:</b><p class="mono ok">'+d.access_key+'</p><small>Key này chỉ được hiển thị một lần.</small>';$('msg').textContent='Created';loadKeys()}async function loadKeys(){let t=$('admin').value;if(!t)return;let r=await fetch('/api/admin/keys',{headers:{'X-Admin-Token':t}});let d=await r.json();if(!r.ok){$('msg').innerHTML='<span class="bad">'+(d.message||d.status)+'</span>';return}$('rows').innerHTML=d.map(x=>'<tr><td>'+x.display_name+'</td><td class="mono">'+x.key_prefix+'…</td><td>'+x.uses+' / '+(x.max_uses||'∞')+'</td><td>'+(x.expires_at||'∞')+'</td><td>'+(x.revoked?'REVOKED':'ACTIVE')+'</td></tr>').join('')}</script></body></html>'''


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
