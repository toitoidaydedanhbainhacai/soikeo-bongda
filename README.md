# Quant Terminal V3.1

Render-ready Flask/Gunicorn deployment.

## Render
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn main:app`
- Health check path: `/health`
- UI: `/`

## Environment
Set `ODDS_API_KEY`. Optional: `TELEGRAM_TOKEN`, `GEMINI_API_KEY`, `REQUIRE_LICENSE`.

The engine is fail-closed: no real odds/form means no EV/pick is generated.

## Local test
`python tests.py`
`python -m py_compile main.py tests.py`
