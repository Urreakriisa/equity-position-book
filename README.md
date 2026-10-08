# Equity Position Book

A private portfolio dashboard: positions, daily and accumulated P&L, candlestick
charts with 50 and 200-day averages, a chart-formation screen, portfolio beta and
Sharpe ratio, consensus targets and expected returns, add/drop suggestions, and
news for each holding.

- **Server:** Python (FastAPI). It polls Alpha Vantage in the background and
  keeps everything in a database, so every device sees the same prices.
- **Page:** one HTML file, no build step. It works on phone and desktop and can
  be added to the iPhone home screen.
- **Hosting:** Railway, deployed from this GitHub repository.

## Deploy on Railway

1. In Railway, choose **New Project → Deploy from GitHub repo** and pick this repository.
2. In the same project, add a database: **New → Database → PostgreSQL**.
3. Open the app service's **Variables** tab and add:

   | Variable | Value |
   |---|---|
   | `ALPHAVANTAGE_API_KEY` | your Alpha Vantage premium key |
   | `APP_PASSWORD` | the password for signing in (can edit positions) |
   | `SECRET_KEY` | any long random string |
   | `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` (a reference to the database service) |
   | `VIEW_PASSWORD` | optional: a second password that can look but not edit |
   | `SEED_LOTS` | optional: starting positions as a JSON list, e.g. `[{"ticker":"NVDA","qty":10,"cost":240.5,"date":"2026-10-06"}]`. Read once, on the first start |
   | `AV_ENTITLEMENT` | optional: `delayed` (default), `realtime`, or `none` |
   | `QUOTE_INTERVAL` | optional: seconds between price refreshes, default `60` |

4. In **Settings → Networking**, click **Generate Domain**. That address is the app.
5. Keep the service at **one replica**: the price refresh runs inside the app.

The first start loads the positions in `SEED_LOTS` (if set) and then fetches quotes,
about three years of daily history, analyst targets and news. Allow two or three
minutes before every panel is filled.

## What the data is

- **Prices:** Alpha Vantage `GLOBAL_QUOTE`, every minute while the US market is
  open and every 15 minutes otherwise. With the `delayed` entitlement they are
  15 minutes behind.
- **History:** `TIME_SERIES_DAILY_ADJUSTED`, adjusted for splits but not dividends.
- **Targets, ratings, beta:** `OVERVIEW`, refreshed daily. Alpha Vantage gives the
  average analyst target and the rating split, not the low and high targets.
- **Indexes:** shown through the ETFs that track them (SPY, DIA, ONEQ, IWM).
  Alpha Vantage index data needs a higher plan.
- **News:** `NEWS_SENTIMENT`, hourly.

About 15 requests a minute are used during market hours with ten holdings.

## Run it on your own computer

```
pip install -r requirements.txt
cp .env.example .env        # fill in the key and a password
set -a; . ./.env; set +a
uvicorn app.server:app --reload
```

Without `DATABASE_URL` it stores data in a local `portfolio.db` file. To try it
with made-up market data and no key, run the `tests/run_local.py` module
(password `local`). `pytest` runs the tests.

## Security notes

- Everything except the sign-in page and `/healthz` needs the password.
- The Alpha Vantage key lives only in Railway's variables. It is never sent to
  the browser or written to logs.
- No positions or keys are stored in this repository. Positions live in the
  database; enter them in the app or pass them once through `SEED_LOTS`.
