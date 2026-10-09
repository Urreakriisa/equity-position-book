import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app import analytics, ideas
from app.av import (AVError, parse_calendar, parse_daily, parse_earnings, parse_estimates, parse_etf,
                    parse_news, parse_overview, parse_quote, rating_label)
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
    assert st["alerts"] == {"default": None, "watchDefault": None, "by": {}, "earnDays": 3} and st["alertEvents"] == []

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
    assert st["alerts"] == {"default": None, "watchDefault": None, "by": {small: 0.0001}, "earnDays": 3}
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


def test_chart_any_ticker(client):
    assert client.get("/api/lookup?symbol=TSM").status_code == 401
    client.post("/login", data={"password": "view-pass"})          # viewers may look tickers up
    r = client.get("/api/lookup?symbol=tsm")
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "TSM" and len(body["hist"]["c"]) == 320 and len(body["hist"]["o"]) == 320
    assert body["quote"]["last"] > 0 and body["quote"]["name"] and body["live"]["day"] == "2026-10-07"
    st = client.get("/api/state").json()
    assert "TSM" not in st["quotes"] and "TSM" not in st["watchlist"] and "TSM" not in st["watch"]
    assert client.get("/api/lookup?symbol=BADTICKER").status_code == 404
    assert client.get("/api/lookup?symbol=a%20b").status_code == 400
    assert client.get("/api/lookup").status_code == 400


SUB = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc123", "keys": {"p256dh": "BPubKey", "auth": "authsecret"}}


def test_push_subscribe_alert_and_prune(client, monkeypatch):
    import app.push as push_mod
    push = client.app.state.push
    sent = []

    class Gone(Exception):
        response = type("R", (), {"status_code": 410})()

    def fake_webpush(**kw):
        if "dead" in kw["subscription_info"]["endpoint"]:
            raise Gone()
        sent.append(kw)

    monkeypatch.setattr(push_mod, "_webpush", fake_webpush)
    monkeypatch.setattr(push, "send", lambda *a: push.deliver(*a))       # deliver inline, no thread

    assert client.get("/api/push/pubkey").status_code == 401
    assert client.post("/api/push/subscribe", json=SUB).status_code == 401
    assert client.get("/sw.js").status_code == 200 and "showNotification" in client.get("/sw.js").text
    client.post("/login", data={"password": "view-pass"})                 # any signed-in device may opt in
    key = client.get("/api/push/pubkey").json()["key"]
    assert len(key) == 87 and client.get("/api/push/pubkey").json()["key"] == key    # 65-byte point, stable
    assert "private" not in client.get("/api/push/pubkey").text

    for bad in ({"endpoint": "https://evil.example.com/x", "keys": SUB["keys"]},
                {"endpoint": "http://fcm.googleapis.com/x", "keys": SUB["keys"]},
                {"endpoint": SUB["endpoint"]}, {"endpoint": SUB["endpoint"], "keys": {"p256dh": "", "auth": "a"}}, {}):
        assert client.post("/api/push/subscribe", json=bad).status_code == 400
    assert client.post("/api/push/subscribe", json=SUB).json() == {"devices": 1}
    assert client.post("/api/push/subscribe", json=SUB).json() == {"devices": 1}      # same device twice = once
    dead = {"endpoint": "https://web.push.apple.com/dead", "keys": SUB["keys"]}
    assert client.post("/api/push/subscribe", json=dead).json() == {"devices": 2}

    assert client.post("/api/push/test", json={"endpoint": SUB["endpoint"]}).json() == {"ok": True}
    assert json.loads(sent[-1]["data"])["title"] == "Test alert" and sent[-1]["vapid_claims"]["sub"].startswith("https://")
    assert client.post("/api/push/test", json={"endpoint": "https://fcm.googleapis.com/other"}).status_code == 404

    # a price alert reaches the live device, and the dead one is dropped
    client.post("/logout")
    client.post("/login", data={"password": "edit-pass"})
    held = sorted({l["ticker"] for l in client.get("/api/state").json()["lots"]})
    load(client.engine, held)
    sent.clear()
    st = client.put("/api/alerts", json={"default": 0.0001}).json()
    assert len(st["alertEvents"]) == len(held) == len(sent)
    note = json.loads(sent[0]["data"])
    assert "% today" in note["title"] and note["body"].startswith("Past your 0.0001% alert. Price ")
    assert [s["endpoint"] for s in push.subscriptions()] == [SUB["endpoint"]]
    # the same crossings do not notify again
    sent.clear()
    load(client.engine, held)
    assert sent == []
    assert client.post("/api/push/unsubscribe", json={"endpoint": SUB["endpoint"]}).json() == {"devices": 0}


def test_push_real_encryption_and_vapid_signing(tmp_path, monkeypatch):
    """Run the real library (without sending) to prove the stored key signs."""
    monkeypatch.chdir(tmp_path)          # the library writes its payload file to the working directory
    pytest.importorskip("pywebpush")
    import base64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from pywebpush import webpush
    from app.push import Push
    b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    device = ec.generate_private_key(ec.SECP256R1())
    sub = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc",
           "keys": {"p256dh": b64(device.public_key().public_bytes(serialization.Encoding.X962,
                                                                   serialization.PublicFormat.UncompressedPoint)),
                    "auth": b64(b"0123456789abcdef")}}
    push = Push(Store(f"sqlite:///{tmp_path}/p.db"), "https://example.test")
    key = push._vapid()
    curl = webpush(subscription_info=sub, data='{"title":"t"}', vapid_private_key=key["private"],
                   vapid_claims={"sub": push.subject}, ttl=60, curl=True)
    assert "fcm.googleapis.com" in curl and "vapid" in curl.lower() and key["public"] in curl
    # a second app instance on the same database reuses the same key pair
    assert Push(push.store, "x")._vapid()["public"] == key["public"]


# ---- build 10: earnings, valuation, analytics, ideas ---------------------------
def test_parse_fundamentals():
    ov = parse_overview({"Symbol": "NVDA", "Name": "NVIDIA Corporation", "Sector": "TECHNOLOGY", "Industry": "SEMICONDUCTORS",
                         "MarketCapitalization": "5734188188000", "SharesOutstanding": "24147000000", "PEGRatio": "0.479",
                         "ForwardPE": "24.88", "EVToEBITDA": "23.18", "ProfitMargin": "0.637", "ReturnOnEquityTTM": "1.172",
                         "QuarterlyRevenueGrowthYOY": "1.059", "52WeekHigh": "243.37", "52WeekLow": "163.9",
                         "ExDividendDate": "None", "DividendYield": "None"})
    assert ov["px"] == 237.47 and ov["industry"] == "SEMICONDUCTORS"
    v = ov["val"]
    assert (v["peg"], v["evEbitda"], v["margin"], v["roe"], v["hi52"], v["lo52"]) == (0.479, 23.18, 0.637, 1.172, 243.37, 163.9)
    assert v["exDiv"] is None and v["divYield"] is None

    q = parse_earnings({"quarterlyEarnings": [
        {"fiscalDateEnding": "2026-07-31", "reportedDate": "2026-08-26", "reportedEPS": "2.22", "estimatedEPS": "2.09",
         "surprisePercentage": "6.2201", "reportTime": "post-market"},
        {"fiscalDateEnding": "2006-07-31", "reportedDate": "2006-08-10", "reportedEPS": "0.0038", "estimatedEPS": "None",
         "surprisePercentage": "None", "reportTime": "pre-market"},
        {"fiscalDateEnding": "x", "reportedDate": "2000-01-01", "reportedEPS": "None"}]})
    assert q[0] == {"fiscal": "2026-07-31", "date": "2026-08-26", "eps": 2.22, "est": 2.09, "surp": 6.2201, "time": "post-market"}
    assert len(q) == 2 and q[1]["est"] is None and q[1]["surp"] is None

    cal = parse_calendar("symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay\r\n"
                         "NVDA,NVIDIA CORP,2026-11-18,2026-10-31,2.47,USD,\r\n"
                         "NVDA,NVIDIA CORP,2027-02-24,2027-01-31,,USD,post-market\r\n"
                         "MSFT,MICROSOFT,2026-10-27,2026-09-30,,USD,post-market\r\n")
    assert cal["NVDA"] == {"date": "2026-11-18", "fiscal": "2026-10-31", "est": 2.47, "time": None}
    assert cal["MSFT"]["est"] is None and cal["MSFT"]["time"] == "post-market"
    with pytest.raises(AVError):
        parse_calendar("")

    e = lambda date, horizon, eps, d30, d90: {
        "date": date, "horizon": horizon, "eps_estimate_average": eps, "eps_estimate_analyst_count": "50.0",
        "eps_estimate_average_30_days_ago": d30, "eps_estimate_average_90_days_ago": d90,
        "eps_estimate_revision_up_trailing_30_days": "39.0", "eps_estimate_revision_down_trailing_30_days": None,
        "revenue_estimate_average": "411556323240.00"}
    est = parse_estimates({"estimates": [
        e("2028-01-31", "fiscal year", "15.6951", "15.4043", "12.7102"), e("2027-01-31", "fiscal year", "9.3073", "9.2944", "8.9374"),
        e("2026-10-31", "fiscal quarter", "2.4733", "2.4698", "0.2"), e("2026-07-31", "fiscal quarter", "2.09", "2.08", "2.07")]},
        "2026-10-08")
    assert est["fy1"]["date"] == "2027-01-31" and est["fy2"]["date"] == "2028-01-31" and est["q1"]["date"] == "2026-10-31"
    assert est["fy1"]["chg30"] == round(9.3073 / 9.2944 - 1, 4) and est["fy1"]["up30"] == 39 and est["fy1"]["down30"] == 0
    assert est["q1"]["chg90"] is None                      # a 12x jump is a data glitch, not a revision
    assert parse_estimates({"estimates": []}, "2026-10-08") == {}

    assert parse_etf({"holdings": [{"symbol": "NVDA"}, {"symbol": "n/a", "description": "CASH"}, {"symbol": "BRK.B"},
                                   {"symbol": "NVDA"}]}) == ["NVDA", "BRK-B"]
    with pytest.raises(AVError):
        parse_etf({"holdings": []})
    n = parse_news({"feed": [{"title": "T", "url": "https://x.test/a", "time_published": "20261007T211957",
                              "ticker_sentiment": [{"ticker": "LLY", "relevance_score": "0.9", "ticker_sentiment_score": "0.31",
                                                    "ticker_sentiment_label": "Bullish"}]}]}, "LLY")
    assert n[0]["sent"] == 0.31 and n[0]["label"] == "Bullish"


def load_fundamentals(engine, symbols):
    async def run():
        for s in symbols:
            await engine._fresh("earn", s, 0, engine._load_earnings)
            await engine._fresh("est", s, 0, engine._load_estimates)
        await engine._fresh("ecal", "", 0, engine._load_calendar)
    asyncio.run(run())


def test_earnings_valuation_and_earnings_alerts(client, monkeypatch):
    push = client.app.state.push
    sent = []
    monkeypatch.setattr(push, "send", lambda *a: sent.append(a))
    client.post("/login", data={"password": "edit-pass"})
    held = sorted({l["ticker"] for l in client.get("/api/state").json()["lots"]})
    load(client.engine, held)
    load_fundamentals(client.engine, held)
    st = client.get("/api/state").json()
    q = st["quotes"]["NVDA"]
    assert q["sector"] in ("Technology", "Healthcare", "Financial Services", "Utilities") and q["val"]["peg"] > 0
    assert q["val"]["hi52"] > q["val"]["lo52"] and q["fpe"] > 0 and q["mktcap"] > 0
    assert len(q["earn"]["hist"]) == 4 and q["earn"]["hist"][0]["eps"] == 1.0
    assert q["earn"]["next"]["days"] >= 0 and q["earn"]["next"]["date"] and q["est"]["fy1"]["eps"] == 8.0
    assert st["news"][0]["label"] and st["feeds"]["earn"]["state"] == "ok" and st["dataVersion"]

    # holdings that report within the alert window get one alert each, with a push
    days = {t: st["quotes"][t]["earn"]["next"]["days"] for t in held}
    assert st["alertEvents"] == []                           # nothing checked yet
    client.engine.check_earnings()
    due = {t for t in held if days[t] <= 3}
    events = client.get("/api/state").json()["alertEvents"]
    assert {e["ticker"] for e in events} == due and all(e["kind"] == "earnings" for e in events)
    assert len(sent) == len(due)
    if sent:
        assert "reports earnings" in sent[0][0] and "after the close" in sent[0][1] and "EPS estimate" in sent[0][1]
    # widening the window adds the rest of that window once; saving again adds nothing
    wide = max(days.values())
    st = client.put("/api/alerts", json={"earnDays": wide}).json()
    assert st["alerts"]["earnDays"] == wide and {e["ticker"] for e in st["alertEvents"]} == set(held)
    assert len(sent) == len(held)
    assert len(client.put("/api/alerts", json={}).json()["alertEvents"]) == len(held)
    assert client.get("/api/state").json()["alerts"]["earnDays"] == wide      # kept when not sent
    assert client.put("/api/alerts", json={"earnDays": 0}).json()["alerts"]["earnDays"] == 0
    for bad in (-1, 31, "soon"):
        assert client.put("/api/alerts", json={"earnDays": bad}).status_code == 400

    # the lookup carries the same fundamentals, and fetches a date for a stock nobody follows
    body = client.get("/api/lookup?symbol=TSM").json()
    assert body["quote"]["val"]["peg"] > 0 and body["quote"]["earn"]["next"]["date"] and len(body["quote"]["earn"]["hist"]) == 4


def rows(start, moves, first="2026-01-05"):
    import datetime as dt
    day, out, px = dt.date.fromisoformat(first), [], start
    for m in moves:
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        px = round(px * (1 + m), 6)
        out.append([day.isoformat(), px, px, px, px])
        day += dt.timedelta(days=1)
    return out


def test_analytics_returns_sectors_and_correlation():
    n = 60
    up, flat = rows(100, [0.0] + [0.01] * (n - 1)), rows(50, [0.0] * n)
    wave = rows(20, [0.0] + [0.02 if i % 2 else -0.02 for i in range(n - 1)])
    spy = rows(400, [0.0] + [0.005] * (n - 1))
    d = [r[0] for r in spy]
    # 10 shares of UP bought at the close of day 10 (so no gain that day), then FLAT added on day 30 at a 10% discount
    lots = [{"ticker": "UP", "qty": 10, "cost": up[10][4], "date": d[10]},
            {"ticker": "FLAT", "qty": 40, "cost": 45.0, "date": d[30]}]
    a = analytics.compute(lots, {"UP": up, "FLAT": flat, "WAVE": wave}, spy,
                          {"UP": "TECHNOLOGY", "FLAT": None}, {"UP": "Up Inc"})
    assert a["ready"] and a["asOf"] == d[-1]
    w_up = 10 * up[-1][4] / (10 * up[-1][4] + 40 * 50)
    assert a["sectors"][0]["name"] in ("Technology", "Unclassified") and {s["name"] for s in a["sectors"]} == {"Technology", "Unclassified"}
    assert abs(sum(s["weight"] for s in a["sectors"]) - 1) < 1e-3
    assert a["concentration"]["count"] == 2 and abs(a["concentration"]["top1"] - max(w_up, 1 - w_up)) < 1e-3
    assert 1 < a["concentration"]["effective"] <= 2

    # time-weighted: UP alone compounds 1% a day for 19 sessions; on day 30 the book earns UP's 1% on its value
    # plus FLAT's (50-45)*40 gain over the money then at work; afterwards FLAT dilutes UP's 1%
    act = a["actual"]
    assert act["dates"][0] == d[9] and act["dates"][-1] == d[-1] and act["sessions"] == n - 10
    idx, value = 100.0, 10 * up[10][4]
    for i in range(11, n):
        if i == 30:
            pnl, base = 10 * (up[i][4] - up[i - 1][4]) + 40 * (50 - 45), value + 40 * 45
        else:
            pnl, base = 10 * (up[i][4] - up[i - 1][4]), value
        idx *= 1 + pnl / base
        value = 10 * up[i][4] + (40 * 50 if i >= 30 else 0)
    assert abs(act["port"][-1] - idx) < 0.02
    since = next(p for p in a["periods"] if p["key"] == "ALL")
    assert since["mode"] == "actual" and abs(since["port"] - (idx / 100 - 1)) < 1e-3
    assert abs(since["bench"] - (spy[-1][4] / spy[9][4] - 1)) < 1e-3
    # the 1-month window starts before FLAT was bought but after the first purchase: still the actual book
    m1 = next(p for p in a["periods"] if p["key"] == "1M")
    assert m1["mode"] == "actual" and m1["from"] == d[-22]
    assert act["drawdown"]["pct"] == 0 and a["backcast"]["drawdown"]["pct"] == 0     # nothing ever fell

    # a book bought on the last day has no month of its own: the month is a backcast at today's weights
    late = analytics.compute([{"ticker": "UP", "qty": 1, "cost": up[-1][4], "date": d[-1]},
                              {"ticker": "WAVE", "qty": 5, "cost": wave[-1][4], "date": d[-1]}],
                             {"UP": up, "WAVE": wave}, spy, {})
    m1 = next(p for p in late["periods"] if p["key"] == "1M")
    assert m1["mode"] == "backcast" and late["backcast"]["drawdown"]["pct"] < 0
    c = late["correlation"]
    assert c["tickers"] == sorted(["UP", "WAVE"], key=lambda s: -(up[-1][4] if s == "UP" else 5 * wave[-1][4]))
    assert c["matrix"][0][0] == 1.0 and c["matrix"][0][1] == c["matrix"][1][0] and c["sessions"] == n - 1
    att = {r["t"]: r for r in late["attribution"]}
    assert att["UP"]["pl"] == 0 and att["UP"]["ret1y"] > 0.7 and abs(att["UP"]["contrib1y"] - att["UP"]["weight"] * att["UP"]["ret1y"]) < 1e-3
    assert analytics.compute([], {}, spy, {}) == {"ready": False}

    # drawdown: 100 -> 120 -> 90 -> 130 is a 25% fall from the second point to the third
    assert analytics._drawdown([100, 120, 90, 130], ["a", "b", "c", "d"]) == {"pct": -0.25, "peak": "b", "trough": "c"}


def test_ideas_ranking():
    def ov(name, peg, fpe, roe, trend, off, cap=1e11):
        return {"name": name, "mktcap": cap, "px": 100.0, "fpe": fpe, "ma50": 100 * (1 + trend), "ma200": 100.0,
                "sector": "TECHNOLOGY", "target": {"avg": 120.0, "rating": "Buy"},
                "val": {"peg": peg, "roe": roe, "opMargin": roe / 2, "margin": roe / 3, "hi52": 100 / (1 + off)}}
    est = lambda chg: {"fy1": {"chg30": chg, "chg90": chg * 2, "up30": 5 if chg > 0 else 0, "down30": 0 if chg > 0 else 5, "n": 10}}
    uni = [f"S{i:02d}" for i in range(30)]
    # S00 is best on everything, S29 worst: cheaper, better returns, stronger trend, rising estimates
    ovs = {s: ov(s, 0.5 + i / 10, 10 + i, 0.6 - i / 60, 0.2 - i / 100, -i / 100) for i, s in enumerate(uni)}
    ests = {s: est(0.1 - i / 150) for i, s in enumerate(uni)}
    ovs["ETF"] = {}                                           # no fundamentals: left out
    ovs["GOOG"], ovs["GOOGL"] = ov("Alphabet C", 0.1, 5, 0.9, 0.5, 0.0), ov("Alphabet A", 0.1, 5, 0.9, 0.5, 0.0)
    ests["GOOG"] = ests["GOOGL"] = est(0.3)
    out = ideas.rank(uni + ["ETF", "GOOG", "GOOGL", "NODATA"], ovs, ests, held={"S00", "GOOG"}, watch={"S02"})
    picks = [r["t"] for r in out["picks"]]
    assert picks[:3] == ["S01", "S02", "S03"] and len(picks) == 20
    assert "S00" not in picks and "GOOG" not in picks and "GOOGL" not in picks      # held, and the other share class
    assert [r["t"] for r in out["held"]] == ["GOOG", "S00"] and out["held"][1]["rank"] == 3
    top = out["picks"][0]
    assert min(top["f"].values()) >= 80 and set(top["f"]) == {"rev", "val", "qual", "mom"}
    assert top["score"] > out["picks"][-1]["score"] and out["picks"][1]["watch"] is True and top["upside"] == 0.2
    assert out["universe"] == 34 and out["scored"] == 32 and out["revisions"] is True

    # with no estimates the screen still ranks, on three factors, and says so
    three = ideas.rank(uni, ovs, {}, set(), set())
    assert three["revisions"] is False and "rev" not in three["picks"][0]["f"] and three["picks"][0]["t"] == "S00"
    # a stock missing a required factor is not ranked
    ovs["S05"]["val"]["roe"] = ovs["S05"]["val"]["opMargin"] = ovs["S05"]["val"]["margin"] = None
    assert "S05" not in [r["t"] for r in ideas.rank(uni, ovs, ests, {"S05"}, set())["picks"]]
    assert ideas.rank(uni, ovs, ests, {"S05"}, set())["notRanked"] == ["S05"]
    assert ideas._percentiles({"a": 1, "b": 1, "c": 3}) == {"a": 25.0, "b": 25.0, "c": 100.0}


def test_screen_loads_members_and_survives_a_missing_feed(client):
    eng, fake = client.engine, client.fake
    eng.universe_pause = 0
    assert client.get("/api/ideas").status_code == 401 and client.get("/api/analytics").status_code == 401
    client.post("/login", data={"password": "view-pass"})
    empty = client.get("/api/ideas").json()
    assert empty["picks"] == [] and empty["universe"] == 0 and empty["feeds"]["etf"] == "pending"
    assert asyncio.run(eng.screen_pass()) == 0                 # no member list: nothing to do, no crash
    assert client.get("/healthz").json()["feeds"]["etf"] == "failed"

    fake.members = [f"U{i:02d}" for i in range(45)] + ["NVDA", "BADTICKER"]
    fake.no_estimates = set(fake.members)                      # the estimates feed is not on the plan
    eng._failed.clear()
    assert asyncio.run(eng.screen_pass()) == 47
    hz = client.get("/healthz").json()
    assert hz["screen"] == {"members": 47} and hz["feeds"]["etf"] == "ok" and hz["feeds"]["est"] == "failed"
    assert hz["feeds"]["ov"] == "partial" and "U00" not in json.dumps(hz)
    assert sum(1 for c in fake.calls if c[0] == "est") == 5    # stopped asking after five refusals
    out = client.get("/api/ideas").json()
    assert out["revisions"] is False and len(out["picks"]) == 20 and out["loaded"] == 46 and out["scored"] == 46
    assert "NVDA" not in [r["t"] for r in out["picks"]] and [r["t"] for r in out["held"]] == ["NVDA"]
    assert out["picks"][0]["score"] >= out["picks"][1]["score"] and out["picks"][0]["name"].endswith("Corporation")
    assert asyncio.run(eng.screen_pass()) == 0                 # all fresh for a week; failures back off

    # analytics over the seeded book
    held = sorted({l["ticker"] for l in client.get("/api/state").json()["lots"]})
    load(eng, held + ["SPY"])
    a = client.get("/api/analytics").json()
    assert a["ready"] and len(a["attribution"]) == len(held) and len(a["correlation"]["matrix"]) == len(held)
    assert {p["key"] for p in a["periods"]} >= {"1M", "YTD", "1Y"} and abs(sum(s["weight"] for s in a["sectors"]) - 1) < 1e-3
    assert a["backcast"]["drawdown"]["pct"] <= 0 and a["backcast"]["vol"] > 0
