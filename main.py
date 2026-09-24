from __future__ import annotations

import html
import json
import logging
import math
import os
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote_plus, urljoin, urlparse, parse_qs, unquote
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, redirect, make_response

try:
    from google import genai
    from google.genai import types as genai_types
except Exception:
    genai = None
    genai_types = None

APP_VERSION = "quant-terminal-2026.09.24-pure-analysis"
MODEL_VERSION = "poisson-form-gemini-research-v2"
VN = ZoneInfo("Asia/Ho_Chi_Minh")
UTC = timezone.utc
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
APP_ENV = os.getenv("APP_ENV", "production").strip().lower()
MAX_SEARCH_RESULTS = max(4, min(int(os.getenv("MAX_SEARCH_RESULTS", "10")), 20))
MAX_SOURCE_PAGES = max(6, min(int(os.getenv("MAX_SOURCE_PAGES", "24")), 40))
HTTP_TIMEOUT = max(3, min(int(os.getenv("HTTP_TIMEOUT", "6")), 15))
COLLECTOR_TIMEOUT = max(25, min(int(os.getenv("COLLECTOR_TIMEOUT", "55")), 75))
SEARCH_TIMEOUT = max(2, min(int(os.getenv("SEARCH_TIMEOUT", "5")), 10))
MAX_PAGE_CHARS = 18000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("quant-terminal")
app = Flask(__name__)

# ---------- Free public web collector (no provider API key) ----------
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36 QuantTerminal/2026"


def clean_text(raw: str) -> str:
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", html.unescape(text))
    return text[:MAX_PAGE_CHARS]


def _norm_name(value: str) -> str:
    value = html.unescape(str(value or "")).lower()
    value = re.sub(r"[^a-z0-9]+", " ", value).strip()
    aliases = {
        "wales": "wales", "portugal": "portugal",
        "czech republic": "czechia", "czechia": "czechia",
        "turkiye": "turkey", "türkiye": "turkey",
        "usa": "united states", "us": "united states",
    }
    return aliases.get(value, value)


def _name_match(actual: str, requested: str) -> bool:
    a, b = _norm_name(actual), _norm_name(requested)
    if not a or not b:
        return False
    if a == b:
        return True
    # Conservative token containment for official names such as "Portugal U21".
    return (len(a) >= 5 and len(b) >= 5 and (a in b or b in a))


def _competition_match(actual: str, requested: str) -> bool:
    a, b = _norm_name(actual), _norm_name(requested)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    at, bt = set(a.split()), set(b.split())
    return len(at & bt) >= max(1, min(len(at), len(bt)) // 2)


def _requested_dt(date: str, kickoff: str) -> Optional[datetime]:
    try:
        return datetime.strptime(f"{date} {kickoff}", "%Y-%m-%d %H:%M").replace(tzinfo=VN)
    except Exception:
        return None


def _event_dt_from_timestamp(ts: Any) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC).astimezone(VN)
    except Exception:
        return None


def _json_source(url: str, title: str, payload: Any, source_type: str = "public_json") -> dict:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return {"url": url, "title": title, "text": text[:MAX_PAGE_CHARS], "fetched_at": iso_now(), "status_code": 200, "source_type": source_type}


def _walk_dicts(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_dicts(v)


def discover_sofascore(date: str, a: str, b: str, comp: str) -> tuple[list[dict], list[dict], list[str]]:
    """Keyless public Sofascore web endpoint. Used only as a discovery/evidence source."""
    url = f"https://www.sofascore.com/api/v1/sport/football/scheduled-events/{quote_plus(date)}"
    errors = []
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}, timeout=(2, HTTP_TIMEOUT))
        log.info("[COLLECTOR] Sofascore scheduled-events status=%s elapsed=%.2fs", r.status_code, 0)
        if r.status_code >= 400:
            return [], [], [f"sofascore HTTP {r.status_code}"]
        data = r.json()
    except Exception as exc:
        log.warning("[COLLECTOR] Sofascore discovery failed: %s", exc)
        return [], [], [f"sofascore: {exc}"]

    candidates, sources = [], []
    for e in _walk_dicts(data):
        home = ((e.get("homeTeam") or {}).get("name") if isinstance(e.get("homeTeam"), dict) else None)
        away = ((e.get("awayTeam") or {}).get("name") if isinstance(e.get("awayTeam"), dict) else None)
        if not home or not away or "id" not in e:
            continue
        if not (_name_match(home, a) and _name_match(away, b)):
            continue
        tournament = ((e.get("tournament") or {}).get("name") if isinstance(e.get("tournament"), dict) else "") or ""
        if not _competition_match(tournament, comp):
            continue
        dt = _event_dt_from_timestamp(e.get("startTimestamp"))
        req = _requested_dt(date, "00:00")
        if dt and req and dt.date() != req.date():
            continue
        item = {"source":"Sofascore","event_id":str(e.get("id")),"home":home,"away":away,"competition":tournament,"start_vn":dt.isoformat() if dt else None,"home_score":e.get("homeScore"),"away_score":e.get("awayScore"),"status":e.get("status")}
        candidates.append(item)
        event_id = str(e.get("id"))
        event_url = f"https://www.sofascore.com/api/v1/event/{event_id}"
        sources.append(_json_source(url, f"Sofascore scheduled events — {home} vs {away}", item))
        try:
            er = requests.get(event_url, headers={"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}, timeout=(2, HTTP_TIMEOUT))
            if er.status_code < 400:
                sources.append(_json_source(event_url, f"Sofascore event {event_id} — {home} vs {away}", er.json()))
        except Exception as exc:
            errors.append(f"sofascore event {event_id}: {exc}")
    return candidates, sources, errors


def discover_fotmob(date: str, a: str, b: str, comp: str) -> tuple[list[dict], list[dict], list[str]]:
    """Keyless public FotMob web endpoint. Best-effort secondary discovery source."""
    urls = [
        f"https://www.fotmob.com/api/matches?date={quote_plus(date)}",
        f"https://www.fotmob.com/api/matches?date={quote_plus(date.replace('-', ''))}",
    ]
    errors = []
    for url in urls:
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}, timeout=(2, HTTP_TIMEOUT))
            if r.status_code >= 400:
                continue
            data = r.json()
            candidates, sources = [], [_json_source(url, f"FotMob matches {date}", data)]
            for e in _walk_dicts(data):
                hobj, aobj = e.get("homeTeam"), e.get("awayTeam")
                home = hobj.get("name") if isinstance(hobj, dict) else e.get("homeTeamName")
                away = aobj.get("name") if isinstance(aobj, dict) else e.get("awayTeamName")
                if not home or not away or not (_name_match(home, a) and _name_match(away, b)):
                    continue
                tournament = str(e.get("leagueName") or e.get("tournamentName") or e.get("competition") or "")
                if tournament and not _competition_match(tournament, comp):
                    continue
                candidates.append({"source":"FotMob","event_id":str(e.get("id") or e.get("matchId") or ""),"home":home,"away":away,"competition":tournament,"start_vn":None,"status":e.get("status")})
            if candidates:
                return candidates, sources, errors
        except Exception as exc:
            errors.append(f"fotmob: {exc}")
    return [], [], errors


def public_search(query: str) -> list[dict]:
    """Robust keyless public search fallback.

    Search-engine markup changes frequently. We therefore collect both links and
    the visible result/snippet text. A 200 response with zero parsed links is
    still useful evidence and is never treated as "no data" by itself.
    """
    headers = {
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    endpoints = [
        "https://www.google.com/search?q=" + quote_plus(query) + "&num=10&hl=en",
        "https://www.bing.com/search?q=" + quote_plus(query) + "&count=10&setlang=en-US",
        "https://lite.duckduckgo.com/lite/?q=" + quote_plus(query),
        "https://search.yahoo.com/search?p=" + quote_plus(query) + "&n=10",
    ]
    out=[]
    for endpoint in endpoints:
        started=time.monotonic(); host=urlparse(endpoint).netloc
        log.info("[COLLECTOR] search START %s", host)
        try:
            r=requests.get(endpoint,headers=headers,timeout=(2,SEARCH_TIMEOUT),allow_redirects=True)
            elapsed=time.monotonic()-started
            log.info("[COLLECTOR] search END %s status=%s elapsed=%.2fs",host,r.status_code,elapsed)
            if r.status_code>=400:
                continue
            soup=BeautifulSoup(r.text,"html.parser")
            parsed_here=0
            # Keep search-result text as evidence even when the engine hides links.
            blocks=[]
            for sel in ["div.g","li.b_algo","div.MjjYud","div.result","div.algo","div.dd" ,"tr"]:
                blocks.extend(soup.select(sel))
            if not blocks:
                blocks=soup.find_all(["article","li"],limit=40)
            seen_block=set()
            for block in blocks:
                txt=re.sub(r"\s+"," ",block.get_text(" ",strip=True))
                if len(txt)<30 or txt in seen_block: continue
                seen_block.add(txt)
                links=[]
                for a_tag in block.select("a[href]"):
                    href=(a_tag.get("href") or "").strip()
                    title=(a_tag.get_text(" ",strip=True) or a_tag.get("aria-label") or a_tag.get("title") or "").strip()
                    if not href: continue
                    if href.startswith("/url?") or ("/url?" in href and "google." in host):
                        qp=parse_qs(urlparse(href).query)
                        href=unquote((qp.get("q") or qp.get("url") or [""])[0])
                    elif href.startswith("//"):
                        href="https:"+href
                    elif href.startswith("/"):
                        href=urljoin(endpoint,href)
                    pp=urlparse(href)
                    if pp.scheme not in {"http","https"} or not pp.netloc: continue
                    if any(x in pp.netloc.lower() for x in ["google.com","bing.com","duckduckgo.com","yahoo.com","gstatic.com"]):
                        continue
                    if len(title)<3: title=pp.netloc+(pp.path[:120] if pp.path else "")
                    links.append((href.split("#")[0],title[:300]))
                if links:
                    for href,title in links[:3]:
                        out.append({"url":href,"title":title,"description":txt[:1000],"engine":host})
                        parsed_here+=1
                else:
                    # Evidence-only result: Gemini can normalize the snippet even
                    # if the engine did not expose a crawlable destination URL.
                    out.append({"url":endpoint,"title":f"{host} search result","description":txt[:1200],"engine":host,"evidence_only":True})
                    parsed_here+=1
            # Regex fallback catches Google/Bing redirect URLs when DOM selectors change.
            if parsed_here==0:
                raw=r.text
                for m in re.findall(r'https?://[^\"\'<>\\s]+',raw):
                    href=html.unescape(m).rstrip("\\'\"<>")
                    pp=urlparse(href)
                    if pp.scheme in {"http","https"} and pp.netloc and not any(x in pp.netloc.lower() for x in ["google.com","bing.com","duckduckgo.com","yahoo.com"]):
                        out.append({"url":href,"title":pp.netloc+pp.path[:100],"description":"","engine":host}); parsed_here+=1
                        if parsed_here>=MAX_SEARCH_RESULTS: break
            log.info("[COLLECTOR] parsed %s candidates from %s",parsed_here,host)
        except requests.Timeout:
            log.warning("[COLLECTOR] search TIMEOUT %s after %.2fs",host,time.monotonic()-started)
        except Exception as exc:
            log.warning("[COLLECTOR] search ERROR %s: %s",host,exc)
    seen=set();deduped=[]
    for x in out:
        key=(x.get("url"),x.get("description",""))
        if key in seen: continue
        seen.add(key);deduped.append(x)
    return deduped[:MAX_SEARCH_RESULTS*3]


def direct_source_urls(a: str, b: str, comp: str, date: str) -> list[dict]:
    """Known public pages that can be fetched without a provider API key.
    These are discovery/evidence pages, not guaranteed to contain the fixture.
    """
    q=quote_plus(f"{a} {b} {comp} {date}")
    pair=quote_plus(f"{a} {b}")
    return [
        {"url":f"https://www.uefa.com/search/?q={q}","title":"UEFA public search"},
        {"url":f"https://www.espn.com/soccer/search/_/q/{pair}","title":"ESPN public search"},
        {"url":f"https://www.worldfootball.net/search/?q={pair}","title":"WorldFootball public search"},
        {"url":f"https://www.11v11.com/search/?q={pair}","title":"11v11 public search"},
        {"url":f"https://www.transfermarkt.com/schnellsuche/ergebnis/schnellsuche?query={pair}","title":"Transfermarkt public search"},
    ]

def build_queries(a: str, b: str, comp: str, date: str) -> list[str]:
    base = f'"{a}" "{b}" "{comp}" "{date}"'
    pair = f'"{a}" "{b}"'
    return [
        base,
        f'{pair} fixture {comp}',
        f'{pair} match preview {date}',
        f'{pair} lineup injuries team news {date}',
        f'{pair} odds over under handicap {date}',
        f'{pair} xG xGA statistics form {date}',
        f'{pair} sofascore fotmob fbref understat {date}',
        f'{pair} site:sofascore.com',
        f'{pair} site:fotmob.com',
        f'{pair} site:fbref.com',
        f'{pair} site:understat.com',
        f'{pair} official team news {date}',
        f'{pair} bookmaker odds {date}',
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
    """Multi-layer public research collector.

    The collector no longer equates "search engine returned no links" with
    "the fixture has no data". It exhausts structured sources, direct public
    pages, multiple search engines and query variants before returning.
    """
    started=time.monotonic(); deadline=started+COLLECTOR_TIMEOUT
    log.info("[COLLECTOR] start: %s vs %s | %s | %s | deadline=%ss",a,b,comp,date,COLLECTOR_TIMEOUT)
    candidates=[]; errors=[]; pages=[]

    # 1) Structured sources first.
    with ThreadPoolExecutor(max_workers=2,thread_name_prefix="collector-structured") as pool:
        fs={pool.submit(discover_sofascore,date,a,b,comp):"Sofascore",pool.submit(discover_fotmob,date,a,b,comp):"FotMob"}
        for fut in as_completed(fs):
            name=fs[fut]
            try:
                c,srcs,errs=fut.result(timeout=max(.1,deadline-time.monotonic()))
                candidates.extend(c);pages.extend(srcs);errors.extend(errs)
                log.info("[COLLECTOR] %s discovery: matches=%s sources=%s",name,len(c),len(srcs))
            except Exception as exc: errors.append(f"{name}: {exc}")

    # 2) Direct public search pages. Fetch in parallel regardless of search-engine state.
    direct=direct_source_urls(a,b,comp,date)
    if time.monotonic()<deadline:
        with ThreadPoolExecutor(max_workers=min(8,len(direct)),thread_name_prefix="collector-direct") as pool:
            fs={pool.submit(fetch_public_page,x["url"]):x for x in direct}
            for fut in as_completed(fs):
                try:
                    page=fut.result()
                except Exception as exc:
                    page=None; errors.append(f"direct fetch: {exc}")
                if page:
                    page.update({"source":"DirectPublic","search_title":fs[fut].get("title","")})
                    pages.append(page)

    # 3) Search-engine discovery. Run query variants in small waves to avoid
    # hammering one engine and to leave time for fetching the resulting pages.
    queries=build_queries(a,b,comp,date)
    wave_size=4
    for i in range(0,len(queries),wave_size):
        if time.monotonic()>=deadline: break
        wave=queries[i:i+wave_size]
        remaining=max(.1,deadline-time.monotonic())
        with ThreadPoolExecutor(max_workers=len(wave),thread_name_prefix="collector-search") as pool:
            fs={pool.submit(public_search,q):q for q in wave}
            try:
                for fut in as_completed(fs,timeout=remaining):
                    q=fs[fut]
                    try:
                        got=fut.result();candidates.extend(got)
                        log.info("[COLLECTOR] public search DONE: %s candidates=%s",q,len(got))
                    except Exception as exc: errors.append(f"search: {exc}")
            except TimeoutError:
                errors.append(f"collector search wave {i//wave_size+1} timeout")

    # 4) Fetch crawlable candidates. Evidence-only search records are retained,
    # but are not fetched again because their URL is the search page itself.
    seen=set();unique=[]
    for x in candidates:
        u=str(x.get("url") or x.get("event_id") or "").strip()
        if not u: continue
        key=(u,x.get("description",""))
        if key in seen: continue
        seen.add(key);unique.append(x)
    html_candidates=[x for x in unique if x.get("url","").startswith(("http://","https://")) and not x.get("evidence_only") and x.get("source") not in {"Sofascore","FotMob"}]
    preferred=[x for x in html_candidates if any(d in urlparse(x["url"]).netloc.lower() for d in ["sofascore.com","fotmob.com","fbref.com","understat.com","uefa.com","espn.com","worldfootball.net","11v11.com","transfermarkt.com"])]
    ordered=preferred+[x for x in html_candidates if x not in preferred]
    remaining=max(.1,deadline-time.monotonic())
    if ordered and remaining>.5:
        max_pages=min(MAX_SOURCE_PAGES,len(ordered))
        with ThreadPoolExecutor(max_workers=min(8,max_pages),thread_name_prefix="collector-fetch") as pool:
            fs={pool.submit(fetch_public_page,x["url"]):x for x in ordered[:max_pages]}
            try:
                for fut in as_completed(fs,timeout=remaining):
                    src=fs[fut]
                    try: page=fut.result()
                    except Exception as exc: page=None;errors.append(f"fetch {src['url']}: {exc}")
                    if page:
                        page.update({"search_title":src.get("title",""),"search_engine":src.get("engine","")});pages.append(page)
            except TimeoutError: errors.append("collector page-fetch deadline reached")

    # 5) Promote structured candidates only after explicit identity checks.
    requested=_requested_dt(date,"00:00")
    verified=[]
    for c in candidates:
        if c.get("source") not in {"Sofascore","FotMob"}: continue
        if not (_name_match(c.get("home"),a) and _name_match(c.get("away"),b)): continue
        if c.get("competition") and not _competition_match(c.get("competition"),comp): continue
        dt=None
        try: dt=datetime.fromisoformat(c.get("start_vn")) if c.get("start_vn") else None
        except Exception: pass
        if dt and requested and dt.date()!=requested.date(): continue
        verified.append(c)
    # Search-result snippets are evidence too. Preserve them as first-class
    # evidence pages so Gemini can normalize facts even when a search engine
    # exposes no crawlable destination links.
    existing_urls={str(x.get("url")) for x in pages if x.get("url")}
    for x in unique:
        if not x.get("description"):
            continue
        u=str(x.get("url") or "")
        if not u or u in existing_urls:
            continue
        pages.append({
            "url":u,
            "title":x.get("title") or "Public search result",
            "text":x.get("description",""),
            "fetched_at":iso_now(),
            "status_code":200,
            "source_type":"search_result_snippet",
            "engine":x.get("engine"),
        })
        existing_urls.add(u)

    elapsed=time.monotonic()-started
    log.info("[COLLECTOR] end: candidates=%s fetched_pages=%s verified_candidates=%s errors=%s elapsed=%.2fs",len(unique),len(pages),len(verified),len(errors),elapsed)
    return {"sources":pages,"search_results":unique[:MAX_SEARCH_RESULTS*4],"errors":errors,"elapsed_seconds":round(elapsed,2),"deadline_seconds":COLLECTOR_TIMEOUT,"verified_candidates":verified}

def deterministic_identity_from_collector(research: dict, a: str, b: str, comp: str, date: str, kickoff: str) -> dict:
    """Verify fixture identity from explicit structured public-source metadata only."""
    req = _requested_dt(date, kickoff)
    matches = []
    for c in research.get("verified_candidates", []):
        if not (_name_match(c.get("home"), a) and _name_match(c.get("away"), b)):
            continue
        if c.get("competition") and not _competition_match(c.get("competition"), comp):
            continue
        cdt = None
        try:
            if c.get("start_vn"): cdt = datetime.fromisoformat(c["start_vn"])
        except Exception:
            cdt = None
        if req and cdt:
            delta = abs((cdt - req).total_seconds())
            if delta > 15 * 60:
                continue
        matches.append(c)
    if not matches:
        return {"verified": False, "home": a, "away": b, "competition": comp, "date": date, "kickoff": kickoff, "evidence_sources": []}
    # Require a unique matching fixture. Multiple exact duplicates from two sources
    # are allowed and increase evidence confidence; conflicting kickoff times do not.
    times = [m.get("start_vn") for m in matches if m.get("start_vn")]
    if req and times:
        parsed=[]
        for t in times:
            try: parsed.append(datetime.fromisoformat(t))
            except Exception: pass
        if parsed and max(abs((x-req).total_seconds()) for x in parsed) > 15*60:
            return {"verified": False, "home": a, "away": b, "competition": comp, "date": date, "kickoff": kickoff, "evidence_sources": []}
    return {
        "verified": True, "home": a, "away": b, "competition": comp, "date": date, "kickoff": kickoff,
        "evidence_sources": [m.get("source") for m in matches],
        "verified_event_ids": [m.get("event_id") for m in matches if m.get("event_id")],
        "verified_start_times": times,
    }


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
    response=None
    last_exc=None
    for attempt in range(1,4):
        try:
            response=client.models.generate_content(model=GEMINI_MODEL, contents=prompt, config=cfg)
            break
        except Exception as exc:
            last_exc=exc
            msg=str(exc).lower()
            retryable=any(token in msg for token in ["429","resource_exhausted","rate_limit","quota_exceeded","503","unavailable","timeout"])
            log.warning("[GEMINI] attempt %s/3 failed retryable=%s: %s",attempt,retryable,exc)
            if not retryable or attempt>=3:
                raise
            time.sleep(1.5*(2**(attempt-1)))
    if response is None and last_exc:
        raise last_exc
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
    """Deterministic model from verified numeric evidence. Never invents missing values.
    Uses five+ scorelines when available, otherwise the largest verified sample >=3,
    and can fall back to explicit xG/xGA values supplied by the evidence layer.
    """
    form = research_json.get("form") or {}
    hf = form.get("home_last5") or form.get("home_last10") or []
    af = form.get("away_last5") or form.get("away_last10") or []
    h = [x for x in hf if isinstance(x, dict) and x.get("gf") is not None and x.get("ga") is not None]
    a = [x for x in af if isinstance(x, dict) and x.get("gf") is not None and x.get("ga") is not None]
    try:
        if len(h) >= 3 and len(a) >= 3:
            n = min(len(h), len(a), 10)
            h = h[:n]; a = a[:n]
            hgf = [float(x["gf"]) for x in h]; hga = [float(x["ga"]) for x in h]
            agf = [float(x["gf"]) for x in a]; aga = [float(x["ga"]) for x in a]
            lh = max(0.15, min(4.5, 0.65 * statistics.mean(hgf) + 0.35 * statistics.mean(aga)))
            la = max(0.15, min(4.5, 0.65 * statistics.mean(agf) + 0.35 * statistics.mean(hga)))
            quality = f"FORM_VERIFIED_{n}"
        else:
            stats = research_json.get("stats") or {}
            hxg, axg = stats.get("home_xg"), stats.get("away_xg")
            hxga, axga = stats.get("home_xga"), stats.get("away_xga")
            if None in (hxg, axg):
                return None
            lh = float(hxg); la = float(axg)
            if hxga is not None: lh = 0.65 * lh + 0.35 * float(axga)
            if axga is not None: la = 0.65 * la + 0.35 * float(hxga)
            lh = max(0.15, min(4.5, lh)); la = max(0.15, min(4.5, la))
            quality = "XG_VERIFIED"
        ph, pd, pa, po = poisson_probs(lh, la)
        return {"lambda_home": round(lh, 6), "lambda_away": round(la, 6), "prob_home": ph, "prob_draw": pd, "prob_away": pa, "prob_over_2_5": po, "data_quality": quality, "model_version": MODEL_VERSION}
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def ev(prob: float, odds: float) -> float:
    return prob * odds - 1.0


def pick_from_odds(model: dict, odds: dict) -> Optional[dict]:
    markets = [("home", "prob_home"), ("draw", "prob_draw"), ("away", "prob_away"), ("over_2_5", "prob_over_2_5")]
    candidates = []
    for key, pkey in markets:
        p = model.get(pkey)
        if p is None: continue
        item = {"market": key, "probability": round(float(p) * 100, 4), "market_probability": None, "odds": None, "ev": None}
        try:
            o = float(odds.get(key)) if odds.get(key) is not None else None
            if o and o > 1:
                item["odds"] = o
                item["market_probability"] = round((1 / o) * 100, 4)
                item["ev"] = round(ev(float(p), o) * 100, 4)
        except (TypeError, ValueError):
            pass
        candidates.append(item)
    if not candidates: return None
    priced = [x for x in candidates if x["ev"] is not None]
    if priced:
        priced.sort(key=lambda x: (x["ev"], x["probability"]), reverse=True)
        best = priced[0]
        best["selection_type"] = "MARKET_VALUE" if best["ev"] > 0 else "MARKET_MODEL_LEAN"
        best["status"] = "CALCULATED"
        return best
    best = max(candidates, key=lambda x: x["probability"])
    best["selection_type"] = "MODEL_LEAN_NO_VERIFIED_ODDS"
    best["status"] = "CALCULATED"
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


@app.get("/health")
def health():
    return jsonify({
        "status": "OK",
        "version": APP_VERSION,
        "storage": "stateless",
        "free_web_collector": True,
        "collector_timeout_seconds": COLLECTOR_TIMEOUT,
        "search_timeout_seconds": SEARCH_TIMEOUT,
        "gemini_configured": bool(GEMINI_API_KEY),
        "rapidapi_configured": False,
        "search_grounding": False,
        "odds_api_configured": False,
    })


@app.get("/")
def root(): return redirect("/app")

@app.get("/app")
def app_page(): return make_response(USER_HTML)

@app.post("/api/analyze")
def analyze():
    data=request.get_json(silent=True) or {}
    a=str(data.get("team_a") or "").strip(); b=str(data.get("team_b") or "").strip()
    comp=str(data.get("competition") or "").strip(); date=str(data.get("match_date") or "").strip(); kickoff=str(data.get("kickoff") or "").strip()
    if not all([a,b,comp,date,kickoff]):
        return jsonify({"status":"INVALID_REQUEST","message":"Thiếu Team A, Team B, Competition, Date hoặc Kickoff GMT+7."}),400
    future_ok,parsed=validate_future_kickoff(date,kickoff)
    if not future_ok:
        return jsonify({"status":parsed,"message":"Chỉ phân tích trận có kickoff trong tương lai theo giờ Việt Nam."}),400
    log.info("[ANALYZE] Request received: %s vs %s | %s | %s %s",a,b,comp,date,kickoff)
    t0=time.monotonic()
    try:
        log.info("[ANALYZE] Step 1/5 FREE WEB COLLECTOR")
        research=collect_research(a,b,comp,date)
        collector_identity=deterministic_identity_from_collector(research,a,b,comp,date,kickoff)
        if not research.get("sources"):
            research["sources"]=[{"url":"collector://research-attempt","title":"Collector attempt","text":f"No crawlable page was returned. Queries attempted for {a} vs {b}, {comp}, {date}. Errors: {'; '.join(research.get('errors',[]))}","fetched_at":iso_now(),"status_code":0,"source_type":"collector_status"}]

        log.info("[ANALYZE] Step 2/5 GEMINI NORMALIZATION")
        try:
            extracted=gemini_extract(research,a,b,comp,date,kickoff)
        except Exception as exc:
            if not collector_identity.get("verified"):
                raise
            log.warning("[GEMINI] normalization unavailable after verified fixture: %s",exc)
            extracted={"match_identity":collector_identity,"form":{"home_last5":[],"away_last5":[],"home_last10":[],"away_last10":[]},"odds":{"home":None,"draw":None,"away":None,"over_2_5":None,"under_2_5":None,"asian_handicap":[]},"stats":{},"team_news":[],"injuries":[],"suspensions":[],"expected_lineups":[],"odds_snapshots":[],"source_notes":[],"freshness":"VERIFIED","confidence":"MEDIUM"}

        identity=extracted.get("match_identity") or {}
        if collector_identity.get("verified"):
            identity={**collector_identity,**{k:v for k,v in identity.items() if k not in {"verified","home","away","competition","date","kickoff"}}}
            identity["verified"]=True
            extracted["match_identity"]=identity
        log.info("[ANALYZE] Step 3/5 MATCH VALIDATION: %s","VERIFIED" if identity.get("verified") else "UNVERIFIED")

        if not identity.get("verified"):
            # Do not call this NO_DATA/NO_BET. The UI receives the evidence and
            # explicit research state so another run can continue from fresh sources.
            payload={"status":"RESEARCH_CONTINUES","message":"Nguồn công khai đã được thu thập và Gemini đang giữ nguyên các giá trị có bằng chứng; nhận dạng trận chưa đủ chắc chắn để tính Quant.","research":extracted,"sources":research["sources"],"research_errors":research["errors"],"pipeline":{"research":"COLLECTED","validation":"PENDING","quant":"PENDING"}}
        else:
            log.info("[ANALYZE] Step 4/5 DETERMINISTIC QUANT ENGINE")
            model=deterministic_model(extracted)
            odds=extracted.get("odds") or {}
            pick=pick_from_odds(model,odds) if model else None
            if not model:
                status="RESEARCH_CONTINUES"; msg="Đã xác minh trận; dữ liệu số đang được giữ nguyên theo nguồn và chưa đủ để tính model deterministic."
            elif pick and pick.get("selection_type")=="MARKET_VALUE":
                status="VALUE"; msg="Primary Pick có EV dương từ odds đã thu thập và model deterministic."
            elif pick and pick.get("odds") is not None:
                status="MODEL_LEAN"; msg="Model đã tính xong; odds hiện tại không tạo EV dương."
            else:
                status="MODEL_LEAN"; msg="Model đã tính xong từ dữ liệu xác minh; chưa có odds xác minh để tính EV thị trường."
            log.info("[ANALYZE] Step 5/5 RESULT: %s",status)
            payload={"status":status,"message":msg,"match_identity":identity,"research":extracted,"model":model,"pick":pick,"sources":research["sources"],"research_errors":research["errors"],"pipeline":{"research":"VERIFIED","validation":"VERIFIED","quant":"CALCULATED" if model else "CONTINUES"}}
        payload.update({"team_a":a,"team_b":b,"competition":comp,"match_date":date,"kickoff":kickoff,"request_time_vn":datetime.now(VN).isoformat(),"elapsed_seconds":round(time.monotonic()-t0,2),"architecture":"Gemini + Free Web Collector + Quant Engine"})
        return jsonify(payload)
    except Exception as exc:
        log.exception("[ANALYZE] FAILED")
        return jsonify({"status":"RESEARCH_FAILED","message":str(exc),"elapsed_seconds":round(time.monotonic()-t0,2)}),503

USER_HTML = r'''<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#080c14"><title>Soi Kèo AI — Quant Terminal</title><link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Plus+Jakarta+Sans:wght@500;600;700;800&display=swap" rel="stylesheet"><style>
:root{--bg:#080c14;--panel:rgba(17,24,39,.62);--panel2:#0d1420;--line:rgba(255,255,255,.09);--text:#edf2f7;--muted:#8995a8;--gold:#f5b84b;--gold2:#ffe08a;--green:#26d391;--red:#ff6f7d;--cyan:#57d7e8;--shadow:0 24px 80px rgba(0,0,0,.38)}*{box-sizing:border-box}html{background:var(--bg)}body{margin:0;min-height:100vh;background:radial-gradient(circle at 15% -5%,rgba(245,184,75,.12),transparent 30%),radial-gradient(circle at 90% 10%,rgba(37,211,145,.09),transparent 28%),var(--bg);color:var(--text);font-family:Inter,system-ui,sans-serif}button,input{font:inherit}.app{max-width:1240px;margin:auto;padding:22px 22px 100px}.nav{height:64px;display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:34px}.brand{display:flex;align-items:center;gap:11px;font-family:'Plus Jakarta Sans';font-weight:800;letter-spacing:-.03em}.logo{width:38px;height:38px;border-radius:13px;background:linear-gradient(135deg,var(--gold),#fff0ad);display:grid;place-items:center;color:#10151d;box-shadow:0 8px 30px rgba(245,184,75,.18)}.navlinks{display:flex;gap:5px}.navlinks button,.ghost{background:transparent;border:0;color:var(--muted);padding:10px 12px;border-radius:11px;cursor:pointer}.navlinks button:hover,.ghost:hover{color:var(--text);background:rgba(255,255,255,.05)}.status{display:flex;align-items:center;gap:8px;color:#9ba7b9;font-size:12px}.dot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 14px var(--green)}.hero{margin:10px 0 22px}.eyebrow{font-size:11px;letter-spacing:.18em;color:var(--gold);font-weight:700}.hero h1{font-family:'Plus Jakarta Sans';font-size:clamp(30px,5vw,54px);line-height:1.02;letter-spacing:-.055em;margin:9px 0}.hero p{color:var(--muted);max-width:650px;margin:0}.glass,.card{background:linear-gradient(145deg,rgba(255,255,255,.055),rgba(255,255,255,.018));border:1px solid var(--line);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);box-shadow:var(--shadow);border-radius:26px}.research{padding:25px}.cardhead{display:flex;justify-content:space-between;align-items:center;gap:15px;margin-bottom:20px}.cardhead h2,.cardhead h3{margin:0;font-family:'Plus Jakarta Sans';letter-spacing:-.03em}.sub{font-size:12px;color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:12px}.field{grid-column:span 4}.field.small{grid-column:span 3}.field label{display:block;color:#9ca9ba;font-size:11px;margin:0 0 7px 2px}.field input{width:100%;padding:13px 14px;background:rgba(5,10,18,.62);border:1px solid var(--line);border-radius:14px;color:var(--text);outline:none;transition:.2s}.field input:focus{border-color:rgba(245,184,75,.6);box-shadow:0 0 0 4px rgba(245,184,75,.08)}.cta{border:0;border-radius:15px;padding:14px 20px;margin-top:15px;background:linear-gradient(110deg,var(--gold),var(--gold2));color:#12161c;font-weight:800;cursor:pointer;box-shadow:0 12px 32px rgba(245,184,75,.17);transition:.2s}.cta:hover{transform:translateY(-2px);box-shadow:0 16px 40px rgba(245,184,75,.24)}.cta:active{transform:translateY(0) scale(.985)}.pipeline{display:flex;gap:7px;flex-wrap:wrap;margin-top:16px}.step{font-size:10px;color:#6f7d90;border:1px solid var(--line);border-radius:999px;padding:6px 9px}.step.active{color:var(--gold2);border-color:rgba(245,184,75,.35);background:rgba(245,184,75,.07)}.dashboard{display:grid;grid-template-columns:1.1fr 1fr;gap:15px;margin-top:15px}.card{padding:22px}.pick{min-height:210px;display:flex;flex-direction:column;justify-content:space-between}.pickmain{font-family:'Plus Jakarta Sans';font-size:32px;font-weight:800;letter-spacing:-.045em}.badge{display:inline-flex;width:max-content;padding:7px 10px;border-radius:999px;font-size:10px;font-weight:800;letter-spacing:.09em;border:1px solid var(--line)}.verified{color:var(--green);background:rgba(38,211,145,.07)}.nobet{color:var(--gold2);background:rgba(245,184,75,.06)}.danger{color:var(--red)}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.metric{padding:14px;border:1px solid var(--line);background:rgba(255,255,255,.025);border-radius:16px}.metric span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.08em}.metric b{display:block;margin-top:6px;font-size:22px}.tabs{display:flex;gap:7px;overflow:auto;padding-bottom:4px}.tab{border:1px solid var(--line);background:rgba(255,255,255,.025);color:var(--muted);padding:9px 12px;border-radius:11px;white-space:nowrap}.tab.active{color:var(--text);border-color:rgba(245,184,75,.32);background:rgba(245,184,75,.06)}pre{white-space:pre-wrap;word-break:break-word;color:#aeb9c9;font-size:11px;line-height:1.6}.historyrow{display:flex;justify-content:space-between;gap:10px;padding:12px 0;border-bottom:1px solid var(--line);font-size:12px}.muted{color:var(--muted)}.hidden{display:none!important}.login{max-width:600px;margin:14vh auto}.error{color:var(--red);font-size:12px;margin-top:10px}.bottom{display:none}
@media(max-width:760px){.app{padding:14px 14px 92px}.nav{margin-bottom:25px}.navlinks{display:none}.hero h1{font-size:36px}.field,.field.small{grid-column:span 12}.research,.card{border-radius:21px;padding:18px}.dashboard{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(3,1fr)}.metric{padding:11px}.metric b{font-size:18px}.bottom{position:fixed;display:flex;z-index:20;bottom:10px;left:10px;right:10px;justify-content:space-around;padding:8px;border:1px solid var(--line);background:rgba(9,14,23,.82);backdrop-filter:blur(20px);border-radius:20px;box-shadow:var(--shadow)}.bottom button{border:0;background:transparent;color:var(--muted);font-size:10px;padding:8px}.bottom button:first-child{color:var(--gold2)}}
</style></head><body><main class="app"><header class="nav"><div class="brand"><div class="logo">Q</div><span>SOI KÈO AI</span></div><div class="navlinks"><button>HOME</button><button>MATCHES</button><button>PICKS</button><button>MARKETS</button><button>INTELLIGENCE</button></div><div class="status"><span class="dot"></span>SYSTEM ONLINE</div></header>
<section id="terminal"><section class="hero"><div class="eyebrow">FOOTBALL INTELLIGENCE</div><h1>Research. Verify. Quantify.</h1><p>Gemini chỉ chuẩn hóa bằng chứng. Free Web Collector thu thập nguồn công khai. Quant Engine tự tính toán — không bịa kèo.</p></section>
<section class="glass research"><div class="cardhead"><div><h2>Research Match</h2><div class="sub">Live public-web research · GMT+7</div></div><span id="badge" class="badge">READY</span></div><div class="grid"><div class="field"><label>ĐỘI A</label><input id="a" placeholder="Home team"></div><div class="field"><label>ĐỘI B</label><input id="b" placeholder="Away team"></div><div class="field"><label>GIẢI ĐẤU</label><input id="comp" placeholder="Competition"></div><div class="field small"><label>NGÀY THI ĐẤU</label><input id="date" type="date"></div><div class="field small"><label>KICKOFF GMT+7</label><input id="kick" type="time"></div></div><button class="cta" onclick="analyze()">⌁ FIND & ANALYZE</button><div id="msg" class="sub" style="margin-top:12px"></div><div class="pipeline"><span id="s1" class="step">WEB RESEARCH</span><span id="s2" class="step">GEMINI</span><span id="s3" class="step">MATCH VERIFY</span><span id="s4" class="step">QUANT ENGINE</span><span id="s5" class="step">VALUE / MODEL LEAN</span></div></section>
<section id="out"></section><section class="card" style="margin-top:15px"><div class="cardhead"><div><h3>Session</h3><div class="sub">Không lưu server. Kết quả chỉ tồn tại trong phiên trình duyệt.</div></div><button class="ghost" onclick="clearSession()">Clear</button></div><div id="hist" class="muted">Chưa có phân tích trong phiên này.</div></section></section></main><nav class="bottom"><button>⌂<br>Home</button><button>◉<br>Matches</button><button>◆<br>Picks</button><button>⌁<br>Market</button><button>•••<br>More</button></nav>
<script>
const $=id=>document.getElementById(id);
const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
function setStep(n){for(let i=1;i<=5;i++)$('s'+i).classList.toggle('active',i===n)}
function saveSession(d){const items=JSON.parse(localStorage.getItem('quant_session')||'[]');items.unshift({team_a:d.team_a,team_b:d.team_b,status:d.status,created_at:new Date().toLocaleString('vi-VN')});localStorage.setItem('quant_session',JSON.stringify(items.slice(0,20)));loadSession()}
function loadSession(){const items=JSON.parse(localStorage.getItem('quant_session')||'[]');$('hist').innerHTML=items.length?items.map(x=>'<div class="historyrow"><span>'+esc(x.team_a)+' vs '+esc(x.team_b)+'</span><span>'+esc(x.status)+' · '+esc(x.created_at)+'</span></div>').join(''):'Chưa có phân tích trong phiên này.'}
function clearSession(){localStorage.removeItem('quant_session');$('hist').textContent='Đã xóa lịch sử phiên.'}
async function analyze(){
 const p={team_a:$('a').value.trim(),team_b:$('b').value.trim(),competition:$('comp').value.trim(),match_date:$('date').value,kickoff:$('kick').value};
 $('out').innerHTML='';$('badge').textContent='RESEARCHING';$('msg').textContent='Đang truy vấn nhiều nguồn công khai và đối chiếu dữ liệu…';setStep(1);
 let r;const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),74000);
 try{r=await fetch('/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p),signal:controller.signal})}
 catch(e){$('badge').textContent=e.name==='AbortError'?'RESEARCH TIME LIMIT':'FAILED';$('msg').textContent=e.name==='AbortError'?'Đã chạm giới hạn thời gian nghiên cứu của request.':'Network error';return}
 finally{clearTimeout(timer)}
 let d;try{d=await r.json()}catch(e){$('badge').textContent='FAILED';$('msg').textContent='Server trả về dữ liệu không hợp lệ.';return}
 if(d.pipeline?.research==='VERIFIED_PAGES')setStep(2);
 if(d.pipeline?.validation==='VERIFIED')setStep(3);
 if(d.pipeline?.quant==='CALCULATED')setStep(4);
 if(d.status==='VALUE')$('badge').textContent='VALUE';else if(d.status==='MODEL_LEAN')$('badge').textContent='MODEL LEAN';else $('badge').textContent=d.status||'RESEARCHING';
 if(!r.ok){$('out').innerHTML='<div class="card error">'+esc(d.message||d.status)+'</div>';return}
 render(d);saveSession(d)
}
function render(d){let p=d.pick,m=d.model;let pickName=p?p.market.replaceAll('_',' ').toUpperCase():'ĐANG TIẾP TỤC NGHIÊN CỨU';let badge=d.status==='VALUE'?'verified':'';let odds=p&&p.odds?Number(p.odds).toFixed(2):'—';let evv=p&&p.ev!==null&&p.ev!==undefined?((p.ev>=0?'+':'')+Number(p.ev).toFixed(2)+'%'):'—';let html='<div class="dashboard"><section class="card pick"><div><span class="badge '+badge+'">'+esc(d.status)+'</span><div class="sub" style="margin-top:17px">PRIMARY MODEL PICK</div><div class="pickmain">'+esc(pickName)+'</div><p class="muted">'+esc(d.message||'')+'</p></div><div class="metrics"><div class="metric"><span>Model</span><b>'+esc(p?p.probability.toFixed(2)+'%':'—')+'</b></div><div class="metric"><span>Odds</span><b>'+esc(odds)+'</b></div><div class="metric"><span>EV</span><b>'+esc(evv)+'</b></div></div></section><section class="card"><div class="cardhead"><div><h3>'+esc(d.team_a)+' vs '+esc(d.team_b)+'</h3><div class="sub">'+esc(d.competition)+' · '+esc(d.match_date)+' · '+esc(d.kickoff)+' GMT+7</div></div><span class="badge">'+esc(d.pipeline?.validation||'—')+'</span></div><div class="tabs"><span class="tab active">MODEL</span><span class="tab">FORM</span><span class="tab">XG</span><span class="tab">LINEUP</span><span class="tab">ODDS</span><span class="tab">NEWS</span></div><div style="margin-top:16px" class="metrics"><div class="metric"><span>λ Home</span><b>'+esc(m?m.lambda_home.toFixed(2):'—')+'</b></div><div class="metric"><span>λ Away</span><b>'+esc(m?m.lambda_away.toFixed(2):'—')+'</b></div><div class="metric"><span>Data</span><b style="font-size:12px">'+esc(m?m.data_quality:'RESEARCHING')+'</b></div></div></section></div><section class="card" style="margin-top:15px"><div class="cardhead"><div><h3>Research Evidence</h3><div class="sub">Gemini-normalized public sources · values must be evidenced</div></div></div><details><summary>View Full Analysis JSON</summary><pre>'+esc(JSON.stringify(d,null,2))+'</pre></details></section>';$('out').innerHTML=html}
loadSession();
</script></body></html>'''


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
