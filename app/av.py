"""Alpha Vantage client: rate-limited, and careful never to leak the API key."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import time

import httpx

BASE = "https://www.alphavantage.co/query"
log = logging.getLogger("av")
SYMBOL = re.compile(r"^[A-Z][A-Z0-9\-]{0,9}$")
# httpx logs full request URLs (including the apikey) at INFO level.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class AVError(Exception):
    pass


def _f(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def parse_quote(data: dict, symbol: str) -> dict:
    """GLOBAL_QUOTE -> plain dict. The wrapper key varies with the entitlement
    ("Global Quote", "Global Quote - DATA DELAYED BY 15 MINUTES")."""
    q = next((v for v in data.values() if isinstance(v, dict) and "05. price" in v), None)
    if not q:
        raise AVError("no quote returned")
    last, prev = _f(q.get("05. price")), _f(q.get("08. previous close"))
    if str(q.get("01. symbol", "")).upper() != symbol.upper():
        raise AVError("quote is for a different symbol")
    if not last or not prev or last <= 0 or prev <= 0 or abs(last / prev - 1) > 0.5:
        raise AVError("quote failed sanity check")
    return {"last": last, "prevClose": prev, "day": str(q.get("07. latest trading day", "")),
            "o": _f(q.get("02. open")), "h": _f(q.get("03. high")), "l": _f(q.get("04. low"))}


def parse_daily(data: dict) -> list[list]:
    """TIME_SERIES_DAILY_ADJUSTED -> ascending [date, o, h, l, c], adjusted for
    splits only (dividends are left alone so prices match what was quoted)."""
    series = next((v for k, v in data.items() if k.startswith("Time Series") and isinstance(v, dict)), None)
    if not series:
        raise AVError("no daily series returned")
    out, factor = [], 1.0
    for day in sorted(series, reverse=True):          # newest first
        r = series[day]
        o, h, l, c = (_f(r.get(k)) for k in ("1. open", "2. high", "3. low", "4. close"))
        if None in (o, h, l, c) or min(o, h, l, c) <= 0:
            continue
        out.append([day, round(o / factor, 4), round(h / factor, 4), round(l / factor, 4), round(c / factor, 4)])
        split = _f(r.get("8. split coefficient")) or 1.0
        if split > 0 and split != 1.0:
            factor *= split                            # applies to all earlier days
    out.reverse()
    return out


def rating_label(sb: int, b: int, h: int, s: int, ss: int) -> str | None:
    """One consensus word from the analyst split. Strong Buy needs 80% of
    analysts at Buy or better; Buy needs 55%."""
    n = sb + b + h + s + ss
    if n == 0:
        return None
    buy, sell = (sb + b) / n, (s + ss) / n
    if buy >= 0.8:
        return "Strong Buy"
    if buy >= 0.55:
        return "Buy"
    if sell >= 0.4:
        return "Sell"
    return "Hold"


def parse_overview(data: dict) -> dict:
    if not data.get("Symbol"):
        return {}                                      # ETFs return an empty object
    counts = [int(_f(data.get(k)) or 0) for k in (
        "AnalystRatingStrongBuy", "AnalystRatingBuy", "AnalystRatingHold",
        "AnalystRatingSell", "AnalystRatingStrongSell")]
    cap, shares = _f(data.get("MarketCapitalization")), _f(data.get("SharesOutstanding"))
    return {
        "name": data.get("Name"),
        "sector": data.get("Sector"),
        "target": {"avg": _f(data.get("AnalystTargetPrice")), "analysts": sum(counts),
                   "rating": rating_label(*counts), "split": counts},
        "beta5y": _f(data.get("Beta")),
        "ma50": _f(data.get("50DayMovingAverage")),
        "ma200": _f(data.get("200DayMovingAverage")),
        "mktcap": cap, "pe": _f(data.get("PERatio")), "fpe": _f(data.get("ForwardPE")),
        "industry": data.get("Industry"),
        # An approximate price (market value over share count), for screens
        # that have no quote of their own.
        "px": round(cap / shares, 2) if cap and shares else None,
        "val": {
            "peg": _f(data.get("PEGRatio")), "evEbitda": _f(data.get("EVToEBITDA")),
            "evRev": _f(data.get("EVToRevenue")), "ps": _f(data.get("PriceToSalesRatioTTM")),
            "pb": _f(data.get("PriceToBookRatio")), "eps": _f(data.get("EPS")),
            "revGrowth": _f(data.get("QuarterlyRevenueGrowthYOY")),
            "epsGrowth": _f(data.get("QuarterlyEarningsGrowthYOY")),
            "margin": _f(data.get("ProfitMargin")), "opMargin": _f(data.get("OperatingMarginTTM")),
            "roe": _f(data.get("ReturnOnEquityTTM")), "roa": _f(data.get("ReturnOnAssetsTTM")),
            "divYield": _f(data.get("DividendYield")), "revenue": _f(data.get("RevenueTTM")),
            "hi52": _f(data.get("52WeekHigh")), "lo52": _f(data.get("52WeekLow")),
            "exDiv": data.get("ExDividendDate") if str(data.get("ExDividendDate", ""))[:1].isdigit() else None,
        },
    }


def parse_news(data: dict, symbol: str, limit: int = 6) -> list[dict]:
    items = []
    for it in data.get("feed") or []:
        rel = next((_f(t.get("relevance_score")) for t in it.get("ticker_sentiment") or []
                    if str(t.get("ticker", "")).upper() == symbol.upper()), None)
        url, title, tp = it.get("url"), it.get("title"), str(it.get("time_published") or "")
        if not rel or rel < 0.3 or not title or not str(url).startswith("http"):
            continue
        date = f"{tp[:4]}-{tp[4:6]}-{tp[6:8]}" if len(tp) >= 8 else None
        mine = next((t for t in it.get("ticker_sentiment") or []
                     if str(t.get("ticker", "")).upper() == symbol.upper()), {})
        items.append({"ticker": symbol, "title": title, "source": it.get("source"),
                      "url": url, "date": date, "ts": tp,
                      "sent": _f(mine.get("ticker_sentiment_score")),
                      "label": mine.get("ticker_sentiment_label")})
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:limit]


def parse_earnings(data: dict, limit: int = 8) -> list[dict]:
    """EARNINGS -> the latest quarters, newest first, with the surprise against
    the estimate (percent)."""
    out = []
    for q in (data.get("quarterlyEarnings") or [])[:limit]:
        eps = _f(q.get("reportedEPS"))
        if eps is None or not q.get("reportedDate"):
            continue
        out.append({"fiscal": q.get("fiscalDateEnding"), "date": q.get("reportedDate"), "eps": eps,
                    "est": _f(q.get("estimatedEPS")), "surp": _f(q.get("surprisePercentage")),
                    "time": q.get("reportTime") or None})
    return out


def parse_calendar(text: str) -> dict[str, dict]:
    """EARNINGS_CALENDAR (a CSV) -> {symbol: next report}. The earliest date wins."""
    out: dict[str, dict] = {}
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or "reportDate" not in rows[0]:
        raise AVError("no earnings calendar returned")
    col = {name: i for i, name in enumerate(rows[0])}
    get = lambda r, k: r[col[k]].strip() if k in col and col[k] < len(r) else ""
    for r in rows[1:]:
        sym, day = get(r, "symbol").upper(), get(r, "reportDate")
        if not sym or len(day) != 10:
            continue
        if sym not in out or day < out[sym]["date"]:
            out[sym] = {"date": day, "fiscal": get(r, "fiscalDateEnding") or None,
                        "est": _f(get(r, "estimate")), "time": get(r, "timeOfTheDay") or None}
    return out


def parse_estimates(data: dict, today: str) -> dict:
    """EARNINGS_ESTIMATES -> the current and next fiscal year and the next
    quarter, each with where the EPS estimate stood 30 and 90 days ago."""
    def row(e):
        eps = _f(e.get("eps_estimate_average"))
        if not eps:
            return None
        d30, d90 = _f(e.get("eps_estimate_average_30_days_ago")), _f(e.get("eps_estimate_average_90_days_ago"))
        chg = lambda old: round(eps / old - 1, 4) if old and old > 0 and eps > 0 and abs(eps / old - 1) < 0.5 else None
        return {"date": e.get("date"), "eps": eps, "n": int(_f(e.get("eps_estimate_analyst_count")) or 0),
                "d30": d30, "d90": d90, "chg30": chg(d30), "chg90": chg(d90),
                "up30": int(_f(e.get("eps_estimate_revision_up_trailing_30_days")) or 0),
                "down30": int(_f(e.get("eps_estimate_revision_down_trailing_30_days")) or 0),
                "rev": _f(e.get("revenue_estimate_average"))}
    ahead = sorted((e for e in data.get("estimates") or [] if str(e.get("date", "")) >= today),
                   key=lambda e: e["date"])
    years = [r for e in ahead if "year" in str(e.get("horizon")) and (r := row(e))]
    quarters = [r for e in ahead if "quarter" in str(e.get("horizon")) and (r := row(e))]
    if not years and not quarters:
        return {}
    return {"fy1": years[0] if years else None, "fy2": years[1] if len(years) > 1 else None,
            "q1": quarters[0] if quarters else None}


def parse_etf(data: dict) -> list[str]:
    """ETF_PROFILE -> the tickers it holds (cash and futures lines are dropped)."""
    out = []
    for h in data.get("holdings") or []:
        sym = str(h.get("symbol", "")).strip().upper().replace(".", "-")
        if SYMBOL.match(sym) and sym not in out:
            out.append(sym)
    if not out:
        raise AVError("no ETF holdings returned")
    return out


class AlphaVantage:
    def __init__(self, key: str, entitlement: str = "delayed", rpm: int = 60):
        self.key = key
        self.entitlement = "" if entitlement in ("", "none") else entitlement
        self.gap = 60.0 / max(1, rpm)
        self._lock = asyncio.Lock()
        self._next = 0.0
        self._client = httpx.AsyncClient(timeout=40)

    async def aclose(self):
        await self._client.aclose()

    async def _get(self, market: bool, params: dict) -> tuple[int, str]:
        async with self._lock:                         # one shared pace for every caller
            wait = self._next - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next = max(time.monotonic(), self._next) + self.gap
        if market and self.entitlement:
            params["entitlement"] = self.entitlement
        try:
            r = await self._client.get(BASE, params={**params, "apikey": self.key})
            return r.status_code, r.text
        except httpx.HTTPError as e:
            raise AVError(f"request failed ({type(e).__name__})") from None   # never echo the URL

    def _refusal(self, data) -> None:
        """Alpha Vantage reports errors, rate limits and plan limits in a
        one-field JSON body with a 200 status."""
        if isinstance(data, dict):
            for k in ("Error Message", "Note", "Information"):
                if k in data and len(data) <= 2:
                    raise AVError(str(data[k])[:160].replace(self.key, "***"))

    async def _call(self, market: bool = False, **params) -> dict:
        status, text = await self._get(market, params)
        try:
            data = json.loads(text) if status == 200 else None
        except ValueError:
            data = None
        if status != 200 or not isinstance(data, dict):
            raise AVError(f"HTTP {status}")
        self._refusal(data)
        return data

    async def _call_csv(self, **params) -> str:
        status, text = await self._get(False, params)
        if status != 200:
            raise AVError(f"HTTP {status}")
        if text.lstrip().startswith("{"):              # a refusal arrives as JSON
            try:
                self._refusal(json.loads(text))
            except ValueError:
                pass
            raise AVError("no CSV returned")
        return text

    async def quote(self, symbol: str) -> dict:
        return parse_quote(await self._call(True, function="GLOBAL_QUOTE", symbol=symbol), symbol)

    async def daily(self, symbol: str) -> list[list]:
        return parse_daily(await self._call(True, function="TIME_SERIES_DAILY_ADJUSTED",
                                            symbol=symbol, outputsize="full"))

    async def overview(self, symbol: str) -> dict:
        return parse_overview(await self._call(function="OVERVIEW", symbol=symbol))

    async def news(self, symbol: str) -> list[dict]:
        return parse_news(await self._call(function="NEWS_SENTIMENT", tickers=symbol,
                                           limit=50, sort="LATEST"), symbol)

    async def tbill(self) -> dict:
        data = await self._call(function="TREASURY_YIELD", interval="daily", maturity="3month")
        for row in data.get("data") or []:
            v = _f(row.get("value"))
            if v is not None:
                return {"tbill3m": v, "asOf": row.get("date")}
        raise AVError("no treasury yield returned")

    async def earnings(self, symbol: str) -> list[dict]:
        return parse_earnings(await self._call(function="EARNINGS", symbol=symbol))

    async def earnings_calendar(self, symbol: str | None = None) -> dict[str, dict]:
        params = {"function": "EARNINGS_CALENDAR", "horizon": "3month"}
        if symbol:
            params["symbol"] = symbol
        return parse_calendar(await self._call_csv(**params))

    async def estimates(self, symbol: str, today: str) -> dict:
        return parse_estimates(await self._call(function="EARNINGS_ESTIMATES", symbol=symbol), today)

    async def etf_holdings(self, symbol: str) -> list[str]:
        return parse_etf(await self._call(function="ETF_PROFILE", symbol=symbol))
