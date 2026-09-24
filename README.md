# Quant Terminal V3

A strict fail-closed football quantitative engine.

## Data flow

`The Odds API event discovery -> exact event odds -> TheSportsDB real recent form -> eligibility gate -> data-derived Poisson -> deterministic Monte Carlo -> EV/Kelly -> persistence/UI`

## Hard rules

- No DEFAULT_ODDS.
- No fixed lambda fallback.
- No fabricated matches, dates, odds, form, Monte Carlo, or picks.
- Event identity and odds are tied to the same The Odds API `event_id`.
- Empty/negative radar results are never cached.
- HTTP 429 is surfaced as `RATE_LIMITED`, not silently converted to no-data.
- Missing/ambiguous team form rejects the event.
- Gemini is explanation-only; Python remains the numeric source of truth.

## Run

```bash
pip install -r requirements.txt
set ODDS_API_KEY=YOUR_KEY
set TELEGRAM_TOKEN=YOUR_TOKEN
set GEMINI_API_KEY=YOUR_KEY
python main.py
```

Linux/macOS uses `export` instead of `set`.

`/health` reports which optional credentials are configured.

## Tests

The included tests do not call external APIs and verify the numerical engine and fail-closed behavior:

```bash
python tests.py
```

Live API verification requires your own credentials and network access; the package intentionally does not ship credentials.
