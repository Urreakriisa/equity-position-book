"""A built-in calendar of the US economic events that move the whole market,
taken from the agencies' own published schedules. Dates are fixed well in
advance, so no data feed is needed; extend the lists when the agencies publish
the next year (BLS and the Fed in autumn, BEA and Census late in the year).

Sources: federalreserve.gov/monetarypolicy/fomccalendars.htm,
bls.gov/schedule/news_release/{cpi,empsit,ppi}.htm, bea.gov/news/schedule,
census.gov/retail/release_schedule.html, nyse.com/markets/hours-calendars.
"""
from __future__ import annotations

import datetime as dt

KINDS = {
    "fomc": "Fed decision", "cpi": "CPI", "jobs": "Jobs report", "ppi": "PPI", "gdp": "GDP",
    "pce": "PCE inflation", "retail": "Retail sales", "closed": "Market closed", "early": "Early close",
}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]

# (decision day, Summary of Economic Projections)
FOMC = [("2026-01-28", False), ("2026-03-18", True), ("2026-04-29", False), ("2026-06-17", True),
        ("2026-07-29", False), ("2026-09-16", True), ("2026-10-28", False), ("2026-12-09", True),
        ("2027-01-27", False), ("2027-03-17", True), ("2027-04-28", False), ("2027-06-09", True),
        ("2027-07-28", False), ("2027-09-15", True), ("2027-10-27", False), ("2027-12-08", True)]
# release date -> reference month (year-month)
CPI = {"2026-10-14": "2026-09", "2026-11-10": "2026-10", "2026-12-10": "2026-11", "2027-01-13": "2026-12", "2027-02-11": "2027-01",
       "2027-03-10": "2027-02", "2027-04-13": "2027-03", "2027-05-12": "2027-04", "2027-06-10": "2027-05",
       "2027-07-14": "2027-06", "2027-08-11": "2027-07", "2027-09-14": "2027-08", "2027-10-14": "2027-09",
       "2027-11-10": "2027-10", "2027-12-10": "2027-11"}
JOBS = {"2026-10-02": "2026-09", "2026-11-06": "2026-10", "2026-12-04": "2026-11", "2027-01-08": "2026-12", "2027-02-05": "2027-01",
        "2027-03-05": "2027-02", "2027-04-02": "2027-03", "2027-05-07": "2027-04", "2027-06-04": "2027-05",
        "2027-07-02": "2027-06", "2027-08-06": "2027-07", "2027-09-03": "2027-08", "2027-10-08": "2027-09",
        "2027-11-05": "2027-10", "2027-12-03": "2027-11"}
PPI = {"2026-10-15": "2026-09", "2026-11-13": "2026-10", "2026-12-15": "2026-11", "2027-01-14": "2026-12", "2027-02-12": "2027-01",
       "2027-03-12": "2027-02", "2027-04-14": "2027-03", "2027-05-13": "2027-04", "2027-06-11": "2027-05",
       "2027-07-15": "2027-06", "2027-08-10": "2027-07", "2027-09-10": "2027-08", "2027-10-15": "2027-09",
       "2027-11-12": "2027-10", "2027-12-15": "2027-11"}
GDP = {"2026-10-29": "Q3 2026, advance estimate", "2026-11-25": "Q3 2026, second estimate",
       "2026-12-23": "Q3 2026, third estimate"}
PCE = {"2026-10-29": "2026-09", "2026-11-25": "2026-10", "2026-12-23": "2026-11"}
RETAIL = {"2026-10-15": "2026-09", "2026-11-17": "2026-10", "2026-12-16": "2026-11"}
CLOSED = {"2026-11-26": "Thanksgiving Day", "2026-12-25": "Christmas Day",
          "2027-01-01": "New Year's Day", "2027-01-18": "Martin Luther King, Jr. Day",
          "2027-02-15": "Washington's Birthday", "2027-03-26": "Good Friday", "2027-05-31": "Memorial Day",
          "2027-06-18": "Juneteenth (observed)", "2027-07-05": "Independence Day (observed)",
          "2027-09-06": "Labor Day", "2027-11-25": "Thanksgiving Day", "2027-12-24": "Christmas Day (observed)"}
EARLY = {"2026-11-27": "Day after Thanksgiving", "2026-12-24": "Christmas Eve", "2027-11-26": "Day after Thanksgiving"}


def _month(ym: str) -> str:
    y, m = ym.split("-")
    return f"{MONTHS[int(m) - 1]} {y}"


def events() -> list[dict]:
    """Every known event, oldest first."""
    out = []
    for day, sep in FOMC:
        out.append({"date": day, "kind": "fomc", "title": "Fed rate decision",
                    "detail": "2:00 PM ET, with new economic projections and a press conference" if sep
                    else "2:00 PM ET, with a press conference"})
    for series, kind, title in ((CPI, "cpi", "Consumer prices (CPI)"), (JOBS, "jobs", "Jobs report"),
                                (PPI, "ppi", "Producer prices (PPI)"), (PCE, "pce", "PCE inflation and spending"),
                                (RETAIL, "retail", "Retail sales")):
        for day, ref in series.items():
            out.append({"date": day, "kind": kind, "title": title, "detail": f"{_month(ref)} data, 8:30 AM ET"})
    for day, what in GDP.items():
        out.append({"date": day, "kind": "gdp", "title": "GDP", "detail": f"{what}, 8:30 AM ET"})
    for day, what in CLOSED.items():
        out.append({"date": day, "kind": "closed", "title": "US markets closed", "detail": what})
    for day, what in EARLY.items():
        out.append({"date": day, "kind": "early", "title": "US markets close at 1:00 PM ET", "detail": what})
    order = {k: i for i, k in enumerate(KINDS)}
    out.sort(key=lambda e: (e["date"], order[e["kind"]]))
    return out


def upcoming(today: dt.date, days: int = 14) -> list[dict]:
    """Events from today through `days` ahead, with the days until each."""
    start, end = today.isoformat(), (today + dt.timedelta(days=days)).isoformat()
    return [{**e, "days": (dt.date.fromisoformat(e["date"]) - today).days}
            for e in events() if start <= e["date"] <= end]


def coverage() -> dict[str, str]:
    """The last date each series is known for, so the page can say when the
    built-in schedule runs out."""
    last = lambda d: max(d) if d else None
    return {"fomc": max(d for d, _ in FOMC), "cpi": last(CPI), "jobs": last(JOBS), "ppi": last(PPI),
            "gdp": last(GDP), "pce": last(PCE), "retail": last(RETAIL), "closed": last(CLOSED)}
