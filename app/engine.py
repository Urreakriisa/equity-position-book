"""Keeps market data fresh in the background and assembles what the page shows."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import time
from zoneinfo import ZoneInfo

from .av import AVError
from .store import Store

log = logging.getLogger("engine")
NY = ZoneInfo("America/New_York")
HIST_ROWS = 760                       # about three years of sessions
# Each index is charted through the ETF that tracks it (index data itself
# needs a higher Alpha Vantage plan).
INDEX_ETFS = {"SPX": ("S&P 500", "SPY"), "DJI": ("Dow Jones", "DIA"),
              "IXIC": ("Nasdaq Composite", "ONEQ"), "RUT": ("Russell 2000", "IWM")}
CHART_INDEXES = ("SPX", "DJI", "IXIC")
DEFAULT_WATCH = ["STX", "MU", "IREN", "USAR", "META", "BABA", "STRL", "TSM", "TTMI", "SIMO"]


def now_ny() -> dt.datetime:
    return dt.datetime.now(NY)


def market_open(now: dt.datetime | None = None) -> bool:
    now = now or now_ny()
    return now.weekday() < 5 and dt.time(9, 30) <= now.time() < dt.time(16, 20)


def last_session(now: dt.datetime | None = None) -> str:
    """Date of the most recent session that should be complete (holidays are
    not known here; the staleness check below is throttled to allow for them)."""
    now = now or now_ny()
    day = now.date()
    if now.weekday() >= 5 or now.time() < dt.time(16, 20):
        day -= dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day.isoformat()


def sma(values: list[float], n: int) -> float | None:
    return round(sum(values[-n:]) / n, 4) if len(values) >= n else None


class Engine:
    def __init__(self, store: Store, av, quote_interval: int = 60, delayed: bool = True):
        self.store, self.av = store, av
        self.quote_interval = max(15, quote_interval)
        self.delayed = delayed
        self._failed: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []
        self.last_error = ""

    # ---- symbols -----------------------------------------------------------
    def held(self) -> list[str]:
        return sorted({l["ticker"] for l in self.store.list_lots()})

    def etfs(self) -> list[str]:
        return [etf for _, etf in INDEX_ETFS.values()]

    def watchlist(self) -> list[str]:
        return self.store.list_watch()

    # ---- lifecycle ---------------------------------------------------------
    def start(self):
        if self.av is None:
            return
        self._tasks = [asyncio.create_task(self._quote_loop()), asyncio.create_task(self._slow_loop())]

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def kick(self, symbols: list[str]):
        """A position was just added: load its data now instead of waiting."""
        if self.av is None or not symbols:
            return
        async def run():
            for s in symbols:
                self._failed = {k: v for k, v in self._failed.items() if not k.endswith(":" + s)}
                await self._fresh("quote", s, 0, self._load_quote)
                await self._fresh("ov", s, 0, self._load_overview)
                await self._ensure_history(s)
                await self._fresh("news", s, 0, self._load_news)
        self._tasks.append(asyncio.create_task(run()))

    async def _quote_loop(self):
        while True:
            try:
                symbols = await asyncio.to_thread(lambda: self.held() + self.etfs())
                await asyncio.gather(*(self._fresh("quote", s, 0, self._load_quote) for s in dict.fromkeys(symbols)))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("quote loop")
            await asyncio.sleep(self.quote_interval if market_open() else 900)

    async def _slow_loop(self):
        while True:
            try:
                held, watch = await asyncio.to_thread(self.held), await asyncio.to_thread(self.watchlist)
                for s in dict.fromkeys(held + self.etfs()):
                    await self._ensure_history(s)
                for s in dict.fromkeys(held + watch):
                    await self._fresh("ov", s, 24 * 3600, self._load_overview)
                for s in watch:
                    if s not in held:
                        await self._fresh("quote", s, 900 if market_open() else 3600, self._load_quote)
                for s in held:
                    await self._fresh("news", s, 3600, self._load_news)
                await self._fresh("rates", "", 12 * 3600, self._load_rates)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("slow loop")
            await asyncio.sleep(120)

    # ---- loaders -----------------------------------------------------------
    async def _fresh(self, kind: str, sym: str, max_age: float, loader):
        key = f"{kind}:{sym}" if sym else kind
        cur = self.store.get(key)
        if cur and max_age and time.time() - cur.get("at", 0) < max_age:
            return
        if time.time() - self._failed.get(key, 0) < (60 if kind == "quote" else 600):
            return                                     # back off after a failure
        try:
            value = await loader(sym)
            value["at"] = time.time()
            await asyncio.to_thread(self.store.put, key, value)
            self._failed.pop(key, None)
            if kind == "quote":
                self.last_error = ""
        except AVError as e:
            self._failed[key] = time.time()
            self.last_error = f"{sym or kind}: {e}"
            log.warning("%s failed: %s", key, e)

    async def _load_quote(self, sym):
        return await self.av.quote(sym)

    async def _load_overview(self, sym):
        return await self.av.overview(sym)

    async def _load_news(self, sym):
        return {"items": await self.av.news(sym)}

    async def _load_rates(self, _):
        return await self.av.tbill()

    async def _ensure_history(self, sym: str):
        cur = self.store.get(f"hist:{sym}")
        want = last_session()
        if cur and cur.get("rows"):
            if cur["rows"][-1][0] >= want:
                return
            # Stale: retry soon just after the close, otherwise sparingly
            # (a market holiday looks stale all day).
            now = now_ny()
            soon = now.weekday() < 5 and dt.time(16, 20) <= now.time() < dt.time(19, 0)
            if time.time() - cur.get("at", 0) < (900 if soon else 6 * 3600):
                return
        async def load(s):
            rows = await self.av.daily(s)
            today = now_ny().date().isoformat()
            if rows and rows[-1][0] >= today and last_session() < today:
                rows = [r for r in rows if r[0] < today]      # drop the unfinished session
            if len(rows) < 2:
                raise AVError("too little history")
            return {"rows": rows[-HIST_ROWS:]}
        await self._fresh("hist", sym, 0, load)

    # ---- what the page reads -----------------------------------------------
    def _quote_view(self, sym: str) -> dict | None:
        """Latest price for a symbol: the live quote, else the last close."""
        q, h = self.store.get(f"quote:{sym}"), self.store.get(f"hist:{sym}")
        rows = (h or {}).get("rows") or []
        if q:
            at = dt.datetime.fromtimestamp(q["at"], NY)
            if self.delayed and market_open(at):
                at -= dt.timedelta(minutes=15)
            closed = q["day"] < at.date().isoformat() or at.time() >= dt.time(16, 0)
            stamp = f'{q["day"]} 16:00 ET' if closed else f'{q["day"]} {at:%H:%M} ET'
            return {"last": q["last"], "prevClose": q["prevClose"], "asOf": stamp}
        if len(rows) >= 2:
            return {"last": rows[-1][4], "prevClose": rows[-2][4], "asOf": f"{rows[-1][0]} 16:00 ET"}
        return None

    def _live(self, sym: str) -> dict | None:
        q = self.store.get(f"quote:{sym}")
        if not q:
            return None
        return {"last": q["last"], "prevClose": q["prevClose"], "day": q["day"],
                "o": q.get("o"), "h": q.get("h"), "l": q.get("l"), "at": int(q["at"] * 1000)}

    def state(self, can_write: bool) -> dict:
        lots = self.store.list_lots()
        held = sorted({l["ticker"] for l in lots})
        quotes, live, news = {}, {}, []
        for s in held:
            view = self._quote_view(s)
            if not view:
                continue
            ov = self.store.get(f"ov:{s}") or {}
            closes = [r[4] for r in (self.store.get(f"hist:{s}") or {}).get("rows", [])]
            quotes[s] = {**view, "name": ov.get("name") or s, "target": ov.get("target"),
                         "beta5y": ov.get("beta5y"), "mktcap": ov.get("mktcap"),
                         "pe": ov.get("pe"), "fpe": ov.get("fpe"),
                         "ma50": sma(closes, 50) or ov.get("ma50"),
                         "ma200": sma(closes, 200) or ov.get("ma200")}
            if (lv := self._live(s)):
                live[s] = lv
            news += [{k: n.get(k) for k in ("ticker", "title", "source", "url", "date")}
                     for n in (self.store.get(f"news:{s}") or {}).get("items", [])]
        idx, tape = {}, []
        for code, (name, etf) in INDEX_ETFS.items():
            view = self._quote_view(etf)
            if not view:
                continue
            if (lv := self._live(etf)):
                live[etf] = lv
            if code in CHART_INDEXES:
                idx[code] = {**view, "name": name, "etf": etf}
            tape.append({"sym": code, "name": f"{name} ({etf})", "last": view["last"],
                         "chgPct": round((view["last"] / view["prevClose"] - 1) * 100, 2),
                         "asOf": view["asOf"][5:]})
        watch = {}
        for s in self.watchlist():
            ov, view = self.store.get(f"ov:{s}") or {}, self._quote_view(s)
            if view and (ov.get("target") or {}).get("avg"):
                watch[s] = {"name": ov.get("name") or s, "last": view["last"], "asOf": view["asOf"],
                            "target": ov["target"], "beta5y": ov.get("beta5y"), "ma200": ov.get("ma200")}
        rates = self.store.get("rates") or {}
        stamps = [v["at"] for v in live.values()]
        version = hashlib.sha1("|".join(
            f'{s}:{len(r)}:{r[-1][0] if r else ""}' for s in held + self.etfs()
            for r in [(self.store.get(f"hist:{s}") or {}).get("rows", [])]).encode()).hexdigest()[:12]
        return {
            "lots": lots, "quotes": quotes, "live": live, "idx": idx, "watch": watch,
            "watchlist": self.watchlist(), "news": news,
            "market": {"indexes": {"list": tape},
                       "rates": {"tbill3m": rates.get("tbill3m"), "asOf": rates.get("asOf")},
                       "meta": {"updated": max(stamps) if stamps else None}},
            "canWrite": can_write, "delayed": self.delayed, "histVersion": version,
            "configured": self.av is not None, "error": self.last_error,
        }

    def history(self) -> dict:
        out = {}
        for s in dict.fromkeys(self.held() + self.etfs()):
            rows = (self.store.get(f"hist:{s}") or {}).get("rows") or []
            if rows:
                out[s] = {"d": [r[0] for r in rows], "o": [r[1] for r in rows], "h": [r[2] for r in rows],
                          "l": [r[3] for r in rows], "c": [r[4] for r in rows]}
        return out
