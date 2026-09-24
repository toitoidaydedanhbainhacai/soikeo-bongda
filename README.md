# Soi Kèo AI / Quant Terminal 2026

## Architecture
**User input → Free Web Collector → Gemini Flash evidence normalization → Match Validator → Deterministic Quant Engine → EV / NO BET**

This build intentionally does **not** use:
- RapidAPI
- Google Search Grounding
- The Odds API
- any second provider API key

Only `GEMINI_API_KEY` is required for the AI layer. The collector uses ordinary HTTP requests against public web pages/search pages and therefore has no provider API key.

## Important data policy
The collector can be blocked by Cloudflare, JavaScript challenges, robots policies, rate limits, or changes to public page markup. Those failures are treated as insufficient evidence. The system must return `NO_WEB_EVIDENCE`, `MATCH_IDENTITY_UNVERIFIED`, `FORM_INSUFFICIENT`, `ODDS_NOT_FOUND`, or `NO_BET` rather than inventing data.

Odds movement is only considered a verified movement snapshot when the source explicitly provides a numeric price, timestamp/date, market and source URL. A single undated price is not movement.

Gemini only normalizes supplied evidence. It does not calculate the final betting value and is prohibited from inventing missing numeric data. The Quant Engine independently derives Poisson lambdas from at least five verified scorelines per side.

## Render environment variables
- `GEMINI_API_KEY` — required
- `GEMINI_MODEL` — optional, default `gemini-2.5-flash`
- `ADMIN_TOKEN` — private admin secret
- `DATABASE_URL` — Render PostgreSQL Internal Database URL
- `REQUIRE_ACCESS_KEY=1` — recommended
- `APP_ENV=production`
- `MAX_SEARCH_RESULTS=10`
- `MAX_SOURCE_PAGES=18`
- `HTTP_TIMEOUT=12`

No `.env` file is required in Render. Set variables in Render Environment Variables.

## Access keys
Open `/admin`, enter `ADMIN_TOKEN`, create a user key, then use that generated `QT-...` key on `/app`. `ADMIN_TOKEN` is not a user Access Key.

## Deploy
Build command:
`pip install -r requirements.txt`

Start command:
`gunicorn main:app`

## UI
Responsive mobile-first premium dark UI:
- deep `#080c14` background
- glassmorphism cards
- gold/emerald accents
- rounded 2xl/3xl surfaces
- subtle hover/press micro-interactions
- Inter + Plus Jakarta Sans
- desktop navigation + mobile bottom navigation
- research pipeline states
- Primary Pick / Model / Market / EV / Full Analysis
