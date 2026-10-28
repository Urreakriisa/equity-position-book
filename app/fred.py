"""Index closes from FRED (the St. Louis Fed's data service): free, no key,
daily closes with about a one-day lag. Used to show the S&P 500, Dow and Nasdaq
Composite at their own levels, since Alpha Vantage index data needs a higher plan."""
from __future__ import annotations

import csv
import datetime as dt
import io

import httpx

from .av import AVError

URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
SERIES = {"SPX": "SP500", "DJI": "DJIA", "IXIC": "NASDAQCOM"}


def parse(text: str) -> dict[str, float]:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or len(rows[0]) < 2:
        raise AVError("no index data returned")
    out = {}
    for r in rows[1:]:
        if len(r) < 2 or len(r[0]) != 10:
            continue
        try:
            out[r[0]] = float(r[1])
        except ValueError:
            continue                                   # "." marks a holiday
    if len(out) < 20:
        raise AVError("too little index data")
    return out


async def closes(code: str, years: int = 3) -> dict[str, float]:
    series = SERIES.get(code)
    if not series:
        raise AVError("no FRED series for this index")
    start = (dt.date.today() - dt.timedelta(days=365 * years + 30)).isoformat()
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            r = await client.get(URL, params={"id": series, "cosd": start})
    except httpx.HTTPError as e:
        raise AVError(f"FRED request failed ({type(e).__name__})") from None
    if r.status_code != 200:
        raise AVError(f"FRED HTTP {r.status_code}")
    return parse(r.text)
