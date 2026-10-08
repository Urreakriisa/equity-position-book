import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.av import AVError, parse_daily, parse_news, parse_overview, parse_quote, rating_label
from app.main import create_app
from app.store import Store
from tests.fake_av import FakeAV


def test_parse_quote_delayed_wrapper():
    data = {"Global Quote - DATA DELAYED BY 15 MINUTES": {
        "01. symbol": "NVDA", "02. open": "237.6500", "03. high": "239.0800", "04. low": "236.3800",
        "05. price": "237.0650", "07. latest trading day": "2026-10-07", "08. previous close": "239.2400"}}
    q = parse_quote(data, "NVDA")
    assert q == {"last": 237.065, "prevClose": 239.24, "day": "2026-10-07", "o": 237.65, "h": 239.08, "l": 236.38}
    with pytest.raises(AVError):
        parse_quote(data, "AMZN")
    with pytest.raises(AVError):
        parse_quote({"Global Quote": {}}, "NVDA")


def test_parse_daily_adjusts_for_splits_only():
    row = lambda o, c, split="1.0": {"1. open": str(o), "2. high": str(max(o, c)), "3. low": str(min(o, c)),
                                     "4. close": str(c), "7. dividend amount": "0.5", "8. split coefficient": split}
    data = {"Meta Data": {}, "Time Series (Daily)": {
        "2026-01-02": row(100, 102), "2026-01-05": row(102, 104), "2026-01-06": row(52, 53, "2.0"), "2026-01-07": row(53, 54)}}
    rows = parse_daily(data)
    assert [r[0] for r in rows] == ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]
    assert rows[0][1:] == [50, 51, 50, 51] and rows[1][4] == 52      # halved before the split
    assert rows[2][4] == 53 and rows[3][4] == 54                      # untouched from the split on


def test_ratings_and_overview():
    assert rating_label(10, 48, 2, 1, 0) == "Strong Buy"
    assert rating_label(2, 10, 8, 0, 0) == "Buy"
    assert rating_label(0, 3, 9, 1, 0) == "Hold"
    assert rating_label(0, 1, 3, 4, 2) == "Sell"
    assert rating_label(0, 0, 0, 0, 0) is None
    ov = parse_overview({"Symbol": "NVDA", "Name": "NVIDIA Corporation", "AnalystTargetPrice": "328.72",
                         "AnalystRatingStrongBuy": "10", "AnalystRatingBuy": "48", "AnalystRatingHold": "2",
                         "AnalystRatingSell": "1", "AnalystRatingStrongSell": "0", "Beta": "2.216", "PERatio": "None"})
    assert ov["target"] == {"avg": 328.72, "analysts": 61, "rating": "Strong Buy", "split": [10, 48, 2, 1, 0]}
    assert ov["beta5y"] == 2.216 and ov["pe"] is None
    assert parse_overview({}) == {}


def test_parse_news_filters_by_relevance():
    feed = {"feed": [
        {"title": "About LLY", "url": "https://x.test/a", "time_published": "20261007T211957", "source": "S",
         "ticker_sentiment": [{"ticker": "LLY", "relevance_score": "0.9"}]},
        {"title": "Barely LLY", "url": "https://x.test/b", "time_published": "20261007T100000", "source": "S",
         "ticker_sentiment": [{"ticker": "LLY", "relevance_score": "0.05"}, {"ticker": "PFE", "relevance_score": "1.0"}]},
        {"title": "Bad link", "url": "javascript:alert(1)", "time_published": "20261007T100000",
         "ticker_sentiment": [{"ticker": "LLY", "relevance_score": "0.9"}]}]}
    items = parse_news(feed, "LLY")
    assert [i["title"] for i in items] == ["About LLY"] and items[0]["date"] == "2026-10-07"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "edit-pass")
    monkeypatch.setenv("VIEW_PASSWORD", "view-pass")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.delenv("ALLOW_NO_AUTH", raising=False)
    monkeypatch.setenv("SEED_LOTS", json.dumps([{"ticker": t, "qty": 10 + i, "cost": 100.0 + i, "date": "2026-10-06"}
                                                for i, t in enumerate(["GOOG", "GOOG", "CEG", "AMZN", "BAC", "AVGO", "CRDO", "LLY", "MSFT", "NVDA"])]))
    store = Store(f"sqlite:///{tmp_path}/t.db")
    av = FakeAV()
    app = create_app(store, av=None)                 # no background loops in tests
    app.state.engine.av = av
    c = TestClient(app, follow_redirects=False)
    c.fake, c.engine = av, app.state.engine
    return c


def load(engine, symbols):
    async def run():
        for s in symbols:
            await engine._fresh("quote", s, 0, engine._load_quote)
            await engine._fresh("ov", s, 0, engine._load_overview)
            await engine._ensure_history(s)
            await engine._fresh("news", s, 0, engine._load_news)
        await engine._fresh("rates", "", 0, engine._load_rates)
    asyncio.run(run())


def test_everything_needs_a_sign_in(client):
    assert client.get("/").status_code == 303
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/history").status_code == 401
    assert client.put("/api/lots", json={"lots": []}).status_code == 401
    hz = client.get("/healthz").json()
    assert hz["ok"] is True and isinstance(hz["build"], int) and hz["build"] >= 3
    assert client.post("/login", data={"password": "nope"}).status_code == 401
    assert client.get("/api/state").status_code == 401


def test_seed_edit_and_state(client):
    assert client.post("/login", data={"password": "edit-pass"}).status_code == 303
    st = client.get("/api/state").json()
    assert len(st["lots"]) == 10 and st["canWrite"] is True and st["quotes"] == {}
    assert st["build"]["number"] >= 3 and st["build"]["started"] > 0
    held = sorted({l["ticker"] for l in st["lots"]})
    load(client.engine, held + client.engine.etfs())
    st = client.get("/api/state").json()
    assert sorted(st["quotes"]) == held and set(st["idx"]) == {"SPX", "DJI", "IXIC"}
    q = st["quotes"]["NVDA"]
    assert q["last"] > 0 and q["prevClose"] > 0 and q["target"]["avg"] > 0 and q["ma200"] > 0
    assert st["live"]["NVDA"]["day"] == "2026-10-07" and st["market"]["rates"]["tbill3m"] == 4.21
    assert len(st["market"]["indexes"]["list"]) == 4 and len(st["news"]) == 3 * len(held)
    hist = client.get("/api/history").json()
    assert set(hist) == set(held) | {"SPY", "DIA", "ONEQ", "IWM"} and len(hist["NVDA"]["c"]) == 320
    assert hist["NVDA"]["d"][-1] == "2026-10-06"

    # edit: change one lot, drop one, add one
    lots = st["lots"]
    lots[0]["qty"] = 1
    new = lots[:-1] + [{"ticker": "tsm", "qty": 5, "cost": 470.5, "date": "2026-10-07"}]
    r = client.put("/api/lots", json={"lots": new})
    assert r.status_code == 200
    after = r.json()["lots"]
    assert len(after) == 10 and any(l["ticker"] == "TSM" for l in after)
    assert next(l for l in after if l["id"] == lots[0]["id"])["qty"] == 1
    assert lots[-1]["id"] not in {l["id"] for l in after}

    for bad in ([{"ticker": "NVDA", "qty": -1, "cost": 1, "date": "2026-10-07"}],
                [{"ticker": "<script>", "qty": 1, "cost": 1, "date": "2026-10-07"}],
                [{"ticker": "NVDA", "qty": 1, "cost": 1, "date": "tomorrow"}], "x"):
        assert client.put("/api/lots", json={"lots": bad}).status_code == 400
    assert client.put("/api/lots", content="lots=1", headers={"content-type": "text/plain"}).status_code == 415
    assert client.put("/api/watchlist", json={"tickers": ["mu", "STX"]}).json()["watchlist"] == ["MU", "STX"]


def test_view_password_cannot_edit(client):
    client.post("/login", data={"password": "view-pass"})
    assert client.get("/api/state").json()["canWrite"] is False
    assert client.put("/api/lots", json={"lots": []}).status_code == 403
    assert client.put("/api/watchlist", json={"tickers": []}).status_code == 403
    assert client.post("/logout").status_code == 303
    assert client.get("/api/state").status_code == 401


def test_failed_symbol_does_not_break_state(client):
    client.post("/login", data={"password": "edit-pass"})
    client.put("/api/lots", json={"lots": [{"ticker": "BADTICKER", "qty": 1, "cost": 1, "date": "2026-10-07"}]})
    load(client.engine, ["BADTICKER"])
    st = client.get("/api/state").json()
    assert st["quotes"] == {} and "BADTICKER" in st["error"]


def test_login_lockout(client):
    for _ in range(8):
        assert client.post("/login", data={"password": "x"}).status_code == 401
    assert client.post("/login", data={"password": "edit-pass"}).status_code == 429


def test_price_alerts(client):
    client.post("/login", data={"password": "edit-pass"})
    held = sorted({l["ticker"] for l in client.get("/api/state").json()["lots"]})
    load(client.engine, held)
    st = client.get("/api/state").json()
    moves = {t: (st["live"][t]["last"] / st["live"][t]["prevClose"] - 1) * 100 for t in held}
    big = max(held, key=lambda t: abs(moves[t]))
    small = min(held, key=lambda t: abs(moves[t]))
    assert st["alerts"] == {"default": None, "watchDefault": None, "by": {}} and st["alertEvents"] == []

    # a general level just under the biggest move: only holdings past it fire
    level = round(abs(moves[big]) - 0.01, 2)
    st = client.put("/api/alerts", json={"default": level, "by": {small: 99}}).json()
    fired = {e["ticker"]: e for e in st["alertEvents"]}
    assert big in fired and small not in fired
    assert all(abs(moves[t]) >= level for t in fired)
    assert fired[big]["threshold"] == level and abs(fired[big]["pct"] - moves[big]) < 0.01

    # the same crossing is not recorded twice on the next price refresh
    load(client.engine, held)
    again = client.get("/api/state").json()["alertEvents"]
    assert len(again) == len(st["alertEvents"])

    # a per-holding level overrides the general one
    st = client.put("/api/alerts", json={"default": None, "by": {small: 0.0001}}).json()
    assert st["alerts"] == {"default": None, "watchDefault": None, "by": {small: 0.0001}}
    assert small in {e["ticker"] for e in st["alertEvents"]}

    for bad in ({"default": -1}, {"default": "abc"}, {"default": 150}, {"by": {"<x>": 2}}, {"by": [1]}):
        assert client.put("/api/alerts", json=bad).status_code == 400
    client.post("/logout")
    client.post("/login", data={"password": "view-pass"})
    assert client.put("/api/alerts", json={"default": 2}).status_code == 403


def test_watchlist_prices_and_alerts(client):
    client.post("/login", data={"password": "edit-pass"})
    st = client.put("/api/watchlist", json={"tickers": ["MU", "STX", "NVDA"]}).json()
    assert st["watchlist"] == ["MU", "NVDA", "STX"]   # prices for new names load in the background
    load(client.engine, ["MU", "STX"])
    st = client.get("/api/state").json()
    assert set(st["watch"]) >= {"MU", "STX"} and st["watch"]["MU"]["prevClose"] > 0 and "MU" in st["live"]
    assert {"MU", "STX"} <= set(client.get("/api/history").json())
    move = lambda t: abs(st["live"][t]["last"] / st["live"][t]["prevClose"] - 1) * 100

    # the holdings level does not apply to the watchlist; the watchlist level does
    st2 = client.put("/api/alerts", json={"default": 0.0001}).json()
    assert not {"MU", "STX"} & {e["ticker"] for e in st2["alertEvents"]}
    st3 = client.put("/api/alerts", json={"watchDefault": round(min(move("MU"), move("STX")) - 0.001, 3)}).json()
    assert {"MU", "STX"} <= {e["ticker"] for e in st3["alertEvents"]}
    assert st3["alerts"]["watchDefault"] > 0 and st3["alerts"]["default"] is None
    assert client.put("/api/alerts", json={"watchDefault": 500}).status_code == 400


def test_watchlist_starts_empty_and_preset_is_cleared_once(tmp_path, monkeypatch):
    from app.engine import PRESET_WATCH
    monkeypatch.setenv("APP_PASSWORD", "edit-pass")
    url = f"sqlite:///{tmp_path}/w.db"
    assert create_app(Store(url), av=None).state.store.list_watch() == []

    # an untouched preset list from an earlier build is removed
    old = Store(f"sqlite:///{tmp_path}/old.db")
    old.put("seeded", {"at": 1})
    old.replace_watch(PRESET_WATCH)
    assert create_app(old, av=None).state.store.list_watch() == []
    # but a list the user has edited is left alone, now and on later starts
    mine = Store(f"sqlite:///{tmp_path}/mine.db")
    mine.put("seeded", {"at": 1})
    mine.replace_watch(PRESET_WATCH + ["PLTR"])
    assert "PLTR" in create_app(mine, av=None).state.store.list_watch()
    old.replace_watch(PRESET_WATCH)
    assert len(create_app(old, av=None).state.store.list_watch()) == len(PRESET_WATCH)
