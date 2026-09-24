# Quant Terminal 2026

Production-oriented Flask app for Render.

## Environment variables
- `RAPIDAPI_KEY`: RapidAPI Google Search74 key.
- `GEMINI_API_KEY`: Google Gemini API key.
- `GEMINI_MODEL`: Gemini model name; override if your account uses another current model.
- `ADMIN_TOKEN`: private admin secret used only on `/admin` and admin API routes.
- `DATABASE_URL`: Render PostgreSQL **Internal Database URL**.
- `REQUIRE_ACCESS_KEY=1`: require user Access Key (recommended).

## Removed
The Odds API has been removed from this build. There is no `ODDS_API_KEY` dependency.

## Flow
User Access Key -> Google Search74 -> Gemini evidence extraction -> deterministic Quant Engine -> No Bet.
Gemini is instructed to extract only explicitly supported values; it is not allowed to invent missing odds/form/stats.

## Admin
Open `/admin`, enter `ADMIN_TOKEN`, set user name, max uses (0 = unlimited), expiry days (0 = no expiry), then generate an Access Key.

## Render
Use `gunicorn main:app`. Add the four required environment variables (`RAPIDAPI_KEY`, `GEMINI_API_KEY`, `ADMIN_TOKEN`, `DATABASE_URL`).
