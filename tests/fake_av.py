"""A stand-in for Alpha Vantage so the app can run and be tested offline."""
import csv
import datetime as dt
import math
import random
from pathlib import Path

from app.av import AVError, rating_label


def synthetic(seed: int, start: float, days: int = 320, end: dt.date | None = None) -> list[list]:
    rnd, rows, price = random.Random(seed), [], start
    day, dates = end or dt.date(2026, 10, 6), []
    while len(dates) < days:
        if day.weekday() < 5:
            dates.append(day)
        day -= dt.timedelta(days=1)
    for day in reversed(dates):
        o = price * (1 + rnd.gauss(0, 0.004))
        c = o * (1 + rnd.gauss(0.0005, 0.015))
        rows.append([day.isoformat(), round(o, 2), round(max(o, c) * 1.006, 2), round(min(o, c) * 0.994, 2), round(c, 2)])
        price = c
    return rows


class FakeAV:
    def __init__(self, history: dict[str, list[list]] | None = None, today: str = "2026-10-07"):
        self.history, self.today, self.calls = history or {}, today, []

    @classmethod
    def from_csv_dir(cls, folder: str, **kw):
        hist = {}
        for f in Path(folder).glob("*.csv"):
            rows = [[r[0], *map(float, r[1:5])] for r in csv.reader(f.open()) if r and r[0][:1] in "12"]
            hist[f.stem.upper()] = rows
        return cls(hist, **kw)

    def _rows(self, sym):
        if sym == "BADTICKER":
            raise AVError("no quote returned")
        if sym not in self.history:
            self.history[sym] = synthetic(sum(map(ord, sym)), 40 + sum(map(ord, sym)) % 400)
        return self.history[sym]

    async def quote(self, sym):
        self.calls.append(("quote", sym))
        rows = self._rows(sym)
        prev = rows[-1][4]
        move = math.sin(sum(map(ord, sym))) * 0.02
        last = round(prev * (1 + move), 2)
        o = round(prev * (1 + move / 3), 2)
        return {"last": last, "prevClose": prev, "day": self.today, "o": o,
                "h": round(max(o, last) * 1.004, 2), "l": round(min(o, last) * 0.996, 2)}

    async def daily(self, sym):
        self.calls.append(("daily", sym))
        return [list(r) for r in self._rows(sym)]

    async def overview(self, sym):
        self.calls.append(("ov", sym))
        if sym in ("SPY", "DIA", "ONEQ", "IWM"):
            return {}
        last = self._rows(sym)[-1][4]
        k = sum(map(ord, sym))
        counts = [k % 12, 20 + k % 25, k % 9, k % 3, 0]
        return {"name": f"{sym} Corporation", "sector": "TEST",
                "target": {"avg": round(last * (1.05 + (k % 40) / 100), 2), "analysts": sum(counts),
                           "rating": rating_label(*counts), "split": counts},
                "beta5y": round(0.5 + (k % 30) / 10, 2), "ma50": last, "ma200": last * 0.9,
                "mktcap": 1e11, "pe": 25.0, "fpe": 20.0}

    async def news(self, sym):
        self.calls.append(("news", sym))
        return [{"ticker": sym, "title": f"{sym} headline {i}", "source": "Example Wire",
                 "url": f"https://example.com/{sym.lower()}/{i}", "date": "2026-10-0%d" % (7 - i), "ts": "2026100%dT120000" % (7 - i)}
                for i in range(3)]

    async def tbill(self):
        return {"tbill3m": 4.21, "asOf": "2026-10-06"}

    async def aclose(self):
        pass
