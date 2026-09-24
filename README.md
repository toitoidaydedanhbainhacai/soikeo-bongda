# Quant Terminal 2026

Production-oriented football betting intelligence terminal.

## Pipeline
User match inputs → real-time validation → RapidAPI Google Search74 → source collection → exact match verification with Gemini → deterministic Quant Engine → No-Bet Engine → user-specific history/watchlist.

## Key-based multi-user data isolation
Every user accesses the terminal with an access key. The server stores only a SHA-256 hash of each key. A key is bound to its own `user_id`; all analyses, usage logs, watchlist rows, bet tracking and odds snapshots are filtered by that user ID.

Admins can create keys with optional `max_uses` and `expires_at`:

`POST /api/admin/keys` with header `X-Admin-Token` and JSON such as `{"note":"VIP 30 days","max_uses":100,"expires_at":"2026-10-24T23:59:59+07:00"}`.

Revoke:

`POST /api/admin/revoke-key` with `X-Admin-Token` and `{"key":"QT-..."}`.

List:

`GET /api/admin/keys` with `X-Admin-Token`.

## Production persistence
Render web services use an ephemeral filesystem by default. For long-term data, use PostgreSQL via `DATABASE_URL`, or attach a Render Persistent Disk and point `DB_FILE` inside the mounted path. PostgreSQL is the preferred multi-user production option.

## No The Odds API
There is no The Odds API dependency, environment variable, endpoint or default odds table in this build.

## RapidAPI
Google Search74 is server-side only:
- Base: `https://google-search74.p.rapidapi.com/`
- Header: `x-rapidapi-key`
- Header: `x-rapidapi-host: google-search74.p.rapidapi.com`
- Query: `query`, `limit`, `related_keywords`, `cursor`

## Quant integrity
Gemini is an evidence extraction/reconciliation layer. It is not allowed to create odds, xG, lambda, probability, EV or Kelly. The deterministic backend calculates those values only from verified structured evidence. If evidence is insufficient or conflicting, the result is NO BET / DATA CONFLICT.

## Important
Google Search results and public pages can be blocked or incomplete. The application treats unavailable sources as unavailable and never fabricates missing values.
