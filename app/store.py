"""Persistence: positions, watchlist and a JSON cache of market data.

Postgres on Railway (DATABASE_URL), SQLite locally. All access is synchronous
and short; callers on the event loop wrap it in asyncio.to_thread.
"""
from __future__ import annotations

import json
import os
import threading
import time

from sqlalchemy import (Column, Float, Integer, MetaData, String, Table, Text,
                        create_engine, delete, insert, select, update)

metadata = MetaData()

lots = Table(
    "lots", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ticker", String(12), nullable=False),
    Column("qty", Float, nullable=False),
    Column("cost", Float, nullable=False),
    Column("date", String(10), nullable=False),
)
watch = Table("watch", metadata, Column("ticker", String(12), primary_key=True))
cache = Table(
    "cache", metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", Float, nullable=False),
)


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return "sqlite:///" + os.environ.get("SQLITE_PATH", "portfolio.db")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Store:
    def __init__(self, url: str | None = None):
        url = url or database_url()
        kwargs = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        self.engine = create_engine(url, **kwargs)
        metadata.create_all(self.engine)
        self._lock = threading.Lock()
        # In-memory mirror of the cache table: reads never touch the database.
        self.mem: dict[str, dict] = {}
        with self.engine.connect() as c:
            for key, value in c.execute(select(cache.c.key, cache.c.value)):
                try:
                    self.mem[key] = json.loads(value)
                except ValueError:
                    pass

    # ---- cache -----------------------------------------------------------
    def get(self, key: str):
        return self.mem.get(key)

    def put(self, key: str, value: dict) -> None:
        self.mem[key] = value
        text = json.dumps(value, separators=(",", ":"))
        with self._lock, self.engine.begin() as c:
            done = c.execute(update(cache).where(cache.c.key == key)
                             .values(value=text, updated_at=time.time()))
            if done.rowcount == 0:
                c.execute(insert(cache).values(key=key, value=text, updated_at=time.time()))

    # ---- lots ------------------------------------------------------------
    def list_lots(self) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(lots).order_by(lots.c.ticker, lots.c.date, lots.c.id))
            return [dict(r._mapping) for r in rows]

    def replace_lots(self, new: list[dict]) -> list[dict]:
        """Make the table match `new`: rows with an id are updated, rows
        without one are inserted, and ids that are absent are deleted."""
        with self._lock, self.engine.begin() as c:
            existing = {r.id for r in c.execute(select(lots.c.id))}
            keep = set()
            for lot in new:
                vals = {k: lot[k] for k in ("ticker", "qty", "cost", "date")}
                if lot.get("id") in existing:
                    keep.add(lot["id"])
                    c.execute(update(lots).where(lots.c.id == lot["id"]).values(**vals))
                else:
                    c.execute(insert(lots).values(**vals))
            gone = existing - keep
            if gone:
                c.execute(delete(lots).where(lots.c.id.in_(gone)))
        return self.list_lots()

    # ---- watchlist -------------------------------------------------------
    def list_watch(self) -> list[str]:
        with self.engine.connect() as c:
            return [r.ticker for r in c.execute(select(watch).order_by(watch.c.ticker))]

    def replace_watch(self, tickers: list[str]) -> list[str]:
        with self._lock, self.engine.begin() as c:
            c.execute(delete(watch))
            for t in dict.fromkeys(tickers):
                c.execute(insert(watch).values(ticker=t))
        return self.list_watch()
