# Equity Position Book

A private portfolio dashboard in six tabs:

- **Overview:** summary strip (value, day and total P&L, the book against the
  S&P 500 today and year to date), a heat map of the holdings, upcoming earnings
  and the latest news.
- **Positions:** sortable positions table and a watchlist. Selecting any stock
  opens its detail: your lots, chart, valuation, earnings and estimates.
- **Chart:** candlesticks with 50 and 200-day averages and a chart-formation screen.
- **Performance:** time-weighted returns against the S&P 500, volatility, drawdown,
  beta and Sharpe ratio, sector weights, correlation between holdings, and
  attribution by position.
- **Research:** earnings dates and beat/miss history, valuation and fundamentals,
  consensus targets, and news grouped by holding with sentiment.
- **Ideas:** a ranked screen of S&P 500 and Nasdaq 100 members, where your own
  holdings rank in it, and rule-based add/drop suggestions.

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
- **News:** `NEWS_SENTIMENT`, hourly, with each story's sentiment for the stock.
- **Earnings:** `EARNINGS_CALENDAR` (one call covers every stock, twice a day),
  `EARNINGS` for past results against estimates and `EARNINGS_ESTIMATES` for the
  full-year estimate and its revisions, daily for holdings and the watchlist.
- **Ideas screen:** the members come from `ETF_PROFILE` for SPY and QQQ. Once a
  week the app loads `OVERVIEW` and `EARNINGS_ESTIMATES` for each member, a few
  stocks at a time (about half an hour for the first pass), and ranks them on
  estimate revisions, valuation against growth, quality and momentum. If the
  estimates feed is not on the plan the rank uses the other three factors.

About 15 requests a minute are used for prices during market hours with ten
holdings; the weekly screen adds up to about 35 a minute while it runs. Set
`AV_RPM` (default 60) to the plan's limit.

`/healthz` reports each data feed as `ok`, `partial`, `failed` or `pending`
(no positions and no messages), which is the quickest way to see whether an
Alpha Vantage endpoint is missing from the plan.

## How returns are measured

- A period that begins after the first purchase uses the book as it was built:
  each lot joins on its purchase date at its entry price, and money added does
  not count as return (time-weighted).
- A period that begins earlier is marked **backcast**: today's holdings at
  today's weights, rebalanced daily.
- Sold positions are not recorded and dividends are left out, for the book and
  for the S&P 500 (SPY) alike.

## Price alerts and notifications

Set alert levels under **Alerts** (a general level for holdings, one for
the watchlist, and per-stock overrides). When a stock's move for the day passes
its level, the app shows a banner and sends a web push to every device that has
tapped **Enable alerts**. The same window sets how many days before an earnings
report to be alerted (3 by default, 0 for none); each report alerts once.

- Push is ported from Tlalocai (Estacion Virreyes): a VAPID key pair created on
  first use, one subscription per device, delivery in a background thread, dead
  endpoints pruned. Here the key pair and subscriptions live in the database,
  not on a volume, and this app has its own key pair.
- iPhone and iPad: add the app to the Home Screen first (iOS 16.4 or later),
  open it from there, then tap **Enable alerts**.
- `VAPID_SUBJECT` (optional) overrides the contact address sent to push
  services. By default it is the app's own Railway address.

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
