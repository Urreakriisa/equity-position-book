"""Alpha Vantage client: rate-limited, and careful never to leak the API key."""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

BASE = "https://www.alphavantage.co/query"
log = logging.getLogger("av")
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
    cap = _f(data.get("MarketCapitalization"))
    return {
        "name": data.get("Name"),
        "sector": data.get("Sector"),
        "target": {"avg": _f(data.get("AnalystTargetPrice")), "analysts": sum(counts),
                   "rating": rating_label(*counts), "split": counts},
        "beta5y": _f(data.get("Beta")),
        "ma50": _f(data.get("50DayMovingAverage")),
        "ma200": _f(data.get("200DayMovingAverage")),
        "mktcap": cap, "pe": _f(data.get("PERatio")), "fpe": _f(data.get("ForwardPE")),
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
        items.append({"ticker": symbol, "title": title, "source": it.get("source"),
                      "url": url, "date": date, "ts": tp})
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:limit]


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

    async def _call(self, market: bool = False, **params) -> dict:
        async with self._lock:                         # one shared pace for every caller
            wait = self._next - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next = max(time.monotonic(), self._next) + self.gap
        if market and self.entitlement:
            params["entitlement"] = self.entitlement
        try:
            r = await self._client.get(BASE, params={**params, "apikey": self.key})
            status = r.status_code
            data = r.json() if status == 200 else None
        except (httpx.HTTPError, ValueError) as e:
            raise AVError(f"request failed ({type(e).__name__})") from None   # never echo the URL
        if status != 200 or not isinstance(data, dict):
            raise AVError(f"HTTP {status}")
        for k in ("Error Message", "Note", "Information"):
            if k in data and len(data) <= 2:
                raise AVError(str(data[k])[:160].replace(self.key, "***"))
        return data

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
