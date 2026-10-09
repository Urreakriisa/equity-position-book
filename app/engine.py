"""Keeps market data fresh in the background and assembles what the page shows."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import time
from zoneinfo import ZoneInfo

from . import analytics, build, ideas
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
# The list earlier builds pre-filled; kept only so it can be cleared once.
OV_VERSION = 2                        # bump when the stored overview gains fields
UNIVERSE_ETFS = ("SPY", "QQQ")        # the screen covers what these two hold
WEEK = 7 * 24 * 3600
FEEDS = {"quote": "Prices", "hist": "Price history", "ov": "Fundamentals and targets", "news": "News",
         "rates": "Treasury yield", "earn": "Earnings history", "ecal": "Earnings calendar",
         "est": "Earnings estimates", "etf": "Index members"}
PRESET_WATCH = ["STX", "MU", "IREN", "USAR", "META", "BABA", "STRL", "TSM", "TTMI", "SIMO"]


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
        self.on_alert = None            # called with each new alert event
        self._tasks: list[asyncio.Task] = []
        self.last_error = ""
        self.feeds: dict[str, dict] = {}     # per data feed: last success, last failure
        self.universe_pause = 1.5            # seconds between stocks in the weekly screen
        self._est_off_until = 0.0
        self._cache: dict[str, tuple] = {}

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
        self._tasks = [asyncio.create_task(self._quote_loop()), asyncio.create_task(self._slow_loop()),
                       asyncio.create_task(self._universe_loop())]

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
                await self._fresh("earn", s, 0, self._load_earnings)
                await self._fresh("est", s, 0, self._load_estimates)
            await self._fresh("ecal", "", 0, self._load_calendar)
            await asyncio.to_thread(self.check_earnings)
        self._tasks.append(asyncio.create_task(run()))

    async def _quote_loop(self):
        while True:
            try:
                symbols = await asyncio.to_thread(lambda: self.held() + self.etfs() + self.watchlist())
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
                for s in dict.fromkeys(held + self.etfs() + watch):
                    await self._ensure_history(s)
                for s in dict.fromkeys(held + watch):
                    await self._fresh("ov", s, 24 * 3600, self._load_overview, ver=OV_VERSION)
                for s in held:
                    await self._fresh("news", s, 3600, self._load_news)
                await self._fresh("rates", "", 12 * 3600, self._load_rates)
                await self._fresh("ecal", "", 12 * 3600, self._load_calendar)
                for s in dict.fromkeys(held + watch):
                    await self._fresh("earn", s, 24 * 3600, self._load_earnings)
                    await self._fresh("est", s, 24 * 3600, self._load_estimates)
                await asyncio.to_thread(self.check_earnings)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("slow loop")
            await asyncio.sleep(120)

    async def _universe_loop(self):
        """Once a week, refresh fundamentals and estimates for every member of
        the S&P 500 and Nasdaq 100, a few stocks at a time so prices keep their
        share of the rate limit."""
        await asyncio.sleep(60)                        # the book's own data goes first
        while True:
            try:
                await self.screen_pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("universe loop")
            await asyncio.sleep(1800)

    async def screen_pass(self) -> int:
        """Reload whatever the screen's data is missing or older than a week.
        Returns how many stocks were touched."""
        for etf in UNIVERSE_ETFS:
            await self._fresh("etf", etf, WEEK, self._load_etf, quiet=True)
        touched = 0
        for s in self.universe():
            a = await self._fresh("ov", s, WEEK, self._load_overview, ver=OV_VERSION, quiet=True)
            b = None
            if a is not False and time.time() >= self._est_off_until:
                b = await self._fresh("est", s, WEEK, self._load_estimates, quiet=True)
            if a is not None or b is not None:
                touched += 1
                await asyncio.sleep(self.universe_pause)
        return touched

    def universe(self) -> list[str]:
        out: dict[str, None] = {}
        for etf in UNIVERSE_ETFS:
            out.update(dict.fromkeys((self.store.get(f"etf:{etf}") or {}).get("symbols") or []))
        return list(out)

    # ---- loaders -----------------------------------------------------------
    async def _fresh(self, kind: str, sym: str, max_age: float, loader, ver: int | None = None,
                     quiet: bool = False) -> bool | None:
        """Reload one cached item if it is missing or old. Returns True when it
        was reloaded, False when the reload failed, None when nothing was due."""
        key = f"{kind}:{sym}" if sym else kind
        cur = self.store.get(key)
        if cur and max_age and time.time() - cur.get("at", 0) < max_age and (ver is None or cur.get("v") == ver):
            return None
        if time.time() - self._failed.get(key, 0) < (60 if kind == "quote" else 3 * 3600 if quiet else 600):
            return None                                # back off after a failure
        feed = self.feeds.setdefault(kind, {})
        try:
            value = await loader(sym)
            value["at"] = time.time()
            if ver is not None:
                value["v"] = ver
            await asyncio.to_thread(self.store.put, key, value)
            self._failed.pop(key, None)
            feed.update(ok=time.time(), streak=0)
            if kind == "quote":
                self.last_error = ""
            return True
        except AVError as e:
            self._failed[key] = time.time()
            feed.update(failed=time.time(), error=str(e), symbol=sym, streak=feed.get("streak", 0) + 1)
            if kind == "est" and feed["streak"] >= 5:  # not on the plan, or down: stop asking for a while
                self._est_off_until = time.time() + 6 * 3600
            if not quiet:
                self.last_error = f"{sym or kind}: {e}"
            log.warning("%s failed: %s", key, e)
            return False

    def feed_status(self, detail: bool = False) -> dict:
        """ok / failed / pending for each data feed. A feed counts as failed
        when its latest attempt failed and it has not worked since."""
        out = {}
        for kind, label in FEEDS.items():
            f = self.feeds.get(kind) or {}
            state = "pending" if not f else "failed" if f.get("failed", 0) > f.get("ok", 0) else "ok"
            if state == "failed" and f.get("ok"):
                state = "partial"                      # worked for some symbols, failed for the latest
            out[kind] = {"label": label, "state": state, "error": f.get("error") if state != "ok" else None,
                         "symbol": f.get("symbol") if state != "ok" else None} if detail else state
        return out

    async def _load_quote(self, sym):
        q = await self.av.quote(sym)
        await asyncio.to_thread(self._check_alert, sym, q)
        return q

    # ---- price alerts ------------------------------------------------------
    def alerts(self) -> dict:
        a = self.store.get("alerts") or {}
        return {"default": a.get("default"), "watchDefault": a.get("watchDefault"), "by": a.get("by") or {},
                "earnDays": a.get("earnDays", 3)}       # 0 turns earnings alerts off

    def set_alerts(self, default: float | None, watch_default: float | None, by: dict[str, float],
                   earn_days: int | None = None) -> None:
        if earn_days is None:
            earn_days = self.alerts()["earnDays"]
        self.store.put("alerts", {"default": default, "watchDefault": watch_default, "by": by,
                                  "earnDays": earn_days})

    def _check_alert(self, sym: str, q: dict) -> None:
        """Record the first time a stock's move for the day reaches its alert
        level. One event per stock, per trading day, per level. A stock's own
        level wins; otherwise holdings and watchlist names each have a general one."""
        cfg = self.alerts()
        if sym in self.held():
            general = cfg["default"]
        elif sym in self.watchlist():
            general = cfg["watchDefault"]
        else:
            return
        level = cfg["by"].get(sym, general)
        if not level:
            return
        move = (q["last"] / q["prevClose"] - 1) * 100
        if abs(move) < level:
            return
        events = (self.store.get("alert_events") or {}).get("items", [])
        eid = f'{sym}-{q["day"]}-{level:g}'
        if any(e["id"] == eid for e in events):
            return
        event = {"id": eid, "ticker": sym, "day": q["day"], "pct": round(move, 2),
                 "threshold": level, "last": q["last"], "at": time.time(),
                 "watch": sym not in self.held()}
        events.append(event)
        self.store.put("alert_events", {"items": events[-100:]})
        if self.on_alert:
            try:
                self.on_alert(event)
            except Exception:                              # a notifier must never break pricing
                log.exception("alert notifier failed")
        log.info("alert: %s moved %.2f%% (level %s%%)", sym, move, level)

    async def _load_overview(self, sym):
        return {**await self.av.overview(sym), "v": OV_VERSION}

    async def _load_news(self, sym):
        return {"items": await self.av.news(sym)}

    async def _load_rates(self, _):
        return await self.av.tbill()

    async def _load_earnings(self, sym):
        return {"q": await self.av.earnings(sym)}

    async def _load_estimates(self, sym):
        return await self.av.estimates(sym, now_ny().date().isoformat())

    async def _load_etf(self, sym):
        return {"symbols": await self.av.etf_holdings(sym)}

    async def _load_calendar(self, _):
        """One call returns every scheduled report for three months; keep the
        ones for stocks the app follows."""
        cal = await self.av.earnings_calendar()
        keep = set(await asyncio.to_thread(lambda: self.held() + self.watchlist())) | set(self.universe())
        return {"by": {s: v for s, v in cal.items() if s in keep}}

    # ---- earnings ----------------------------------------------------------
    def next_earnings(self, sym: str) -> dict | None:
        today = now_ny().date().isoformat()
        for key in ("ecal", f"ecal1:{sym}"):
            e = ((self.store.get(key) or {}).get("by") or {}).get(sym)
            if e and e["date"] >= today:
                days = (dt.date.fromisoformat(e["date"]) - now_ny().date()).days
                return {**e, "days": days}
        return None

    def check_earnings(self) -> None:
        """Record one alert per stock and report date once the report is
        within the chosen number of days."""
        days = self.alerts()["earnDays"]
        if not days:
            return
        held = self.held()
        events = (self.store.get("alert_events") or {}).get("items", [])
        new = []
        for sym in dict.fromkeys(held + self.watchlist()):
            e = self.next_earnings(sym)
            if not e or e["days"] > days:
                continue
            eid = f'earn-{sym}-{e["date"]}'
            if any(x["id"] == eid for x in events):
                continue
            new.append({"id": eid, "kind": "earnings", "ticker": sym, "day": now_ny().date().isoformat(),
                        "date": e["date"], "days": e["days"], "est": e.get("est"), "time": e.get("time"),
                        "at": time.time(), "watch": sym not in held})
        if not new:
            return
        self.store.put("alert_events", {"items": (events + new)[-100:]})
        for event in new:
            if self.on_alert:
                try:
                    self.on_alert(event)
                except Exception:
                    log.exception("alert notifier failed")
            log.info("alert: %s reports earnings on %s", event["ticker"], event["date"])

    def fundamentals(self, sym: str) -> dict:
        """Valuation, earnings and estimates for one stock, as the page shows them."""
        ov = self.store.get(f"ov:{sym}") or {}
        return {"sector": (ov.get("sector") or "").title() or None,
                "industry": (ov.get("industry") or "").title() or None,
                "val": ov.get("val"), "pe": ov.get("pe"), "fpe": ov.get("fpe"), "mktcap": ov.get("mktcap"),
                "earn": {"next": self.next_earnings(sym), "hist": (self.store.get(f"earn:{sym}") or {}).get("q") or []},
                "est": {k: v for k, v in (self.store.get(f"est:{sym}") or {}).items() if k in ("fy1", "fy2", "q1")}}

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

    async def lookup(self, sym: str) -> dict:
        """Price, history and consensus for any ticker, fetched on demand for
        the chart's ticker box. Nothing is added to the portfolio or watchlist."""
        if self.av is None:
            raise AVError("Market data is off")
        await self._fresh("quote", sym, 55, self._load_quote)
        await self._ensure_history(sym)
        await self._fresh("ov", sym, 24 * 3600, self._load_overview, ver=OV_VERSION)
        await self._fresh("earn", sym, 24 * 3600, self._load_earnings, quiet=True)
        await self._fresh("est", sym, 24 * 3600, self._load_estimates, quiet=True)
        if not self.next_earnings(sym):
            async def one(s):
                return {"by": await self.av.earnings_calendar(s)}
            await self._fresh("ecal1", sym, 24 * 3600, one, quiet=True)
        view, rows = self._quote_view(sym), (self.store.get(f"hist:{sym}") or {}).get("rows") or []
        if not view or len(rows) < 2:
            raise AVError(f"No price data found for {sym}")
        ov = self.store.get(f"ov:{sym}") or {}
        return {"symbol": sym, "live": self._live(sym),
                "quote": {**view, "name": ov.get("name") or sym, "target": ov.get("target"),
                          "beta5y": ov.get("beta5y"), **self.fundamentals(sym)},
                "hist": {"d": [r[0] for r in rows], "o": [r[1] for r in rows], "h": [r[2] for r in rows],
                         "l": [r[3] for r in rows], "c": [r[4] for r in rows]}}

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
                         "beta5y": ov.get("beta5y"), **self.fundamentals(s),
                         "ma50": sma(closes, 50) or ov.get("ma50"),
                         "ma200": sma(closes, 200) or ov.get("ma200")}
            if (lv := self._live(s)):
                live[s] = lv
            news += [{k: n.get(k) for k in ("ticker", "title", "source", "url", "date", "sent", "label")}
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
        watch, watchlist = {}, self.watchlist()
        for s in watchlist:
            ov, view = self.store.get(f"ov:{s}") or {}, self._quote_view(s)
            if not view:
                continue
            closes = [r[4] for r in (self.store.get(f"hist:{s}") or {}).get("rows", [])]
            watch[s] = {**view, "name": ov.get("name") or s, "target": ov.get("target"),
                        "beta5y": ov.get("beta5y"), "ma200": sma(closes, 200) or ov.get("ma200"),
                        **self.fundamentals(s)}
            if s not in live and (lv := self._live(s)):
                live[s] = lv
        rates = self.store.get("rates") or {}
        stamps = [v["at"] for v in live.values()]
        version = hashlib.sha1("|".join(
            f'{s}:{len(r)}:{r[-1][0] if r else ""}' for s in dict.fromkeys(held + self.etfs() + watchlist)
            for r in [(self.store.get(f"hist:{s}") or {}).get("rows", [])]).encode()).hexdigest()[:12]
        return {
            "lots": lots, "quotes": quotes, "live": live, "idx": idx, "watch": watch,
            "watchlist": watchlist, "news": news,
            "market": {"indexes": {"list": tape},
                       "rates": {"tbill3m": rates.get("tbill3m"), "asOf": rates.get("asOf")},
                       "meta": {"updated": max(stamps) if stamps else None}},
            "alerts": self.alerts(),
            "alertEvents": [e for e in (self.store.get("alert_events") or {}).get("items", [])
                            if e["ticker"] in held or e["ticker"] in watchlist][-30:],
            "canWrite": can_write, "delayed": self.delayed, "histVersion": version,
            "configured": self.av is not None, "error": self.last_error,
            "feeds": self.feed_status(detail=True), "dataVersion": self.data_version(lots, version),
            "build": build.info(),
        }

    def history(self) -> dict:
        out = {}
        for s in dict.fromkeys(self.held() + self.etfs() + self.watchlist()):
            rows = (self.store.get(f"hist:{s}") or {}).get("rows") or []
            if rows:
                out[s] = {"d": [r[0] for r in rows], "o": [r[1] for r in rows], "h": [r[2] for r in rows],
                          "l": [r[3] for r in rows], "c": [r[4] for r in rows]}
        return out

    # ---- analytics and ideas -------------------------------------------------
    def data_version(self, lots: list[dict], hist_version: str) -> str:
        """Changes when positions, price history or sector data change, so the
        page knows when to ask for analytics again."""
        text = hist_version + "|" + "|".join(f'{l["ticker"]}:{l["qty"]}:{l["cost"]}:{l["date"]}' for l in lots)
        text += "|" + ",".join(str((self.store.get(f"ov:{s}") or {}).get("sector")) for s in sorted({l["ticker"] for l in lots}))
        return hashlib.sha1(text.encode()).hexdigest()[:12]

    def analytics(self) -> dict:
        lots = self.store.list_lots()
        held = sorted({l["ticker"] for l in lots})
        hist = {s: (self.store.get(f"hist:{s}") or {}).get("rows") or [] for s in held}
        spy = (self.store.get("hist:SPY") or {}).get("rows") or []
        key = self.data_version(lots, f'{len(spy)}:{spy[-1][0] if spy else ""}:' +
                                ",".join(f'{s}{len(r)}{r[-1][0] if r else ""}' for s, r in hist.items()))
        if self._cache.get("analytics", (None,))[0] != key:
            ovs = {s: self.store.get(f"ov:{s}") or {} for s in held}
            self._cache["analytics"] = (key, analytics.compute(
                lots, hist, spy, {s: o.get("sector") for s, o in ovs.items()},
                {s: o.get("name") for s, o in ovs.items()}))
        return self._cache["analytics"][1]

    def ideas(self) -> dict:
        """The ranked screen, recomputed at most once a minute."""
        held, watch = set(self.held()), set(self.watchlist())
        uni = self.universe()
        ovs = {s: o for s in uni if (o := self.store.get(f"ov:{s}")) and o.get("v") == OV_VERSION}
        ests = {s: e for s in uni if (e := self.store.get(f"est:{s}"))}
        mark = (held, watch, len(uni), len(ovs), len(ests))
        cached = self._cache.get("ideas")
        if cached and time.time() - cached[0] < 60 and cached[1] == mark:
            return cached[2]
        out = ideas.rank(uni, ovs, ests, held, watch)
        for row in out["picks"] + out["held"]:
            row["earn"] = self.next_earnings(row["t"])
        stamps = [o["at"] for o in ovs.values()]
        out.update(loaded=len(ovs), updated=max(stamps) if stamps else None,
                   oldest=min(stamps) if stamps else None,
                   feeds={k: self.feed_status()[k] for k in ("etf", "ov", "est")})
        self._cache["ideas"] = (time.time(), mark, out)
        return out
