"""Equity Position Book: a small FastAPI app serving a portfolio dashboard."""
from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import build
from .av import AlphaVantage, AVError
from .engine import PRESET_WATCH, Engine
from .push import Push, clean_subscription
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("app")
HERE = Path(__file__).parent
TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
MAX_LOTS = 500


def create_app(store: Store | None = None, av="env") -> FastAPI:
    env = os.environ
    edit_pw, view_pw = env.get("APP_PASSWORD", ""), env.get("VIEW_PASSWORD", "")
    open_access = env.get("ALLOW_NO_AUTH") == "1"          # local development only
    on_https = bool(env.get("RAILWAY_ENVIRONMENT") or env.get("FORCE_HTTPS"))
    secret = env.get("SECRET_KEY") or secrets.token_urlsafe(32)
    if not env.get("SECRET_KEY"):
        log.warning("SECRET_KEY is not set: everyone is signed out whenever the app restarts")
    if not edit_pw and not open_access:
        log.warning("APP_PASSWORD is not set: the app will refuse sign-ins until it is")

    store = store or Store()
    if av == "env":
        key = env.get("ALPHAVANTAGE_API_KEY", "").strip()
        av = AlphaVantage(key, env.get("AV_ENTITLEMENT", "delayed"), int(env.get("AV_RPM", "60"))) if key else None
        if not key:
            log.warning("ALPHAVANTAGE_API_KEY is not set: no market data will load")
    delayed = env.get("AV_ENTITLEMENT", "delayed") != "realtime"
    engine = Engine(store, av, int(env.get("QUOTE_INTERVAL", "60")), delayed)
    # Push notifications identify the sender by a contact address: the app's own URL.
    domain = env.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    push = Push(store, env.get("VAPID_SUBJECT") or (f"https://{domain}" if domain else "https://github.com/Urreakriisa/equity-position-book"))

    def notify(e: dict) -> None:
        where = " (watchlist)" if e.get("watch") else ""
        push.send(f'{e["ticker"]} {e["pct"]:+.2f}% today{where}',
                  f'Past your {e["threshold"]:g}% alert. Price {e["last"]:,.2f}.', e["id"])

    engine.on_alert = notify

    # First run: load the starting positions and watchlist, once.
    if not store.get("seeded"):
        # Starting positions come from the SEED_LOTS variable (a JSON list), or
        # from seed/lots.json when that file is present. Neither is required.
        seed, seed_file = env.get("SEED_LOTS", "").strip(), HERE.parent / "seed" / "lots.json"
        if not seed and seed_file.exists():
            seed = seed_file.read_text()
        if seed and not store.list_lots():
            try:
                store.replace_lots([{"ticker": str(l["ticker"]).upper(), "qty": float(l["qty"]),
                                     "cost": float(l["cost"]), "date": str(l["date"])} for l in json.loads(seed)])
            except (ValueError, KeyError, TypeError):
                log.warning("SEED_LOTS could not be read; starting with no positions")
        store.put("seeded", {"at": time.time()})
    # The watchlist holds only tickers the user adds. Earlier builds started it
    # with a preset list; remove that list once if it was never changed.
    if not store.get("watch_manual"):
        if set(store.list_watch()) == set(PRESET_WATCH):
            store.replace_watch([])
        store.put("watch_manual", {"at": time.time()})

    @asynccontextmanager
    async def lifespan(_):
        engine.start()
        yield
        await engine.stop()
        if av is not None and hasattr(av, "aclose"):
            await av.aclose()

    app = FastAPI(title="Equity Position Book", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine, app.state.store, app.state.push = engine, store, push
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="epb_session",
                       max_age=30 * 24 * 3600, same_site="lax", https_only=on_https)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        if on_https:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        if not request.url.path.startswith("/static"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    def role(request: Request) -> str | None:
        return "edit" if open_access else request.session.get("role")

    def need(request: Request, edit: bool = False) -> str:
        r = role(request)
        if not r:
            raise HTTPException(401, "Sign in first")
        if edit and r != "edit":
            raise HTTPException(403, "You have view-only access")
        return r

    # ---- sign-in -----------------------------------------------------------
    fails: dict[str, list[float]] = {}

    def login_page(message: str = "", status: int = 200) -> HTMLResponse:
        html = (HERE / "templates" / "login.html").read_text()
        return HTMLResponse(html.replace("{{message}}", message), status_code=status)

    @app.get("/login")
    def login_form(request: Request):
        return RedirectResponse("/", 303) if role(request) else login_page()

    @app.post("/login")
    async def login(request: Request):
        ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0].strip()
        recent = [t for t in fails.get(ip, []) if time.time() - t < 600]
        if len(recent) >= 8:
            return login_page("Too many attempts. Wait ten minutes and try again.", 429)
        form = await request.form()
        pw = str(form.get("password", ""))
        got = None
        if edit_pw and hmac.compare_digest(pw.encode(), edit_pw.encode()):
            got = "edit"
        elif view_pw and hmac.compare_digest(pw.encode(), view_pw.encode()):
            got = "view"
        if not got:
            fails[ip] = recent + [time.time()]
            msg = "That password is not right." if edit_pw else "This app has no password set yet. Add APP_PASSWORD in Railway."
            return login_page(msg, 401)
        fails.pop(ip, None)
        request.session.clear()
        request.session["role"] = got
        return RedirectResponse("/", 303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", 303)

    # ---- pages and data ----------------------------------------------------
    @app.get("/")
    def index(request: Request):
        if not role(request):
            return RedirectResponse("/login", 303)
        return FileResponse(HERE / "templates" / "index.html", media_type="text/html")

    @app.get("/sw.js")
    def service_worker():
        # Served from the root so it can receive pushes for the whole app.
        return FileResponse(HERE / "static" / "sw.js", media_type="text/javascript",
                            headers={"Service-Worker-Allowed": "/"})

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "build": build.BUILD, "commit": build.COMMIT}

    @app.get("/api/state")
    def api_state(request: Request):
        return engine.state(need(request) == "edit")

    @app.get("/api/history")
    def api_history(request: Request):
        need(request)
        return engine.history()

    lookups: list[float] = []

    @app.get("/api/lookup")
    async def api_lookup(request: Request, symbol: str = ""):
        need(request)
        sym = symbol.strip().upper()
        if not TICKER.match(sym):
            raise HTTPException(400, "Enter one ticker, for example MU")
        now = time.time()
        lookups[:] = [t for t in lookups if now - t < 60]
        if len(lookups) >= 20:
            raise HTTPException(429, "Too many lookups in a minute. Try again shortly")
        lookups.append(now)
        try:
            return await engine.lookup(sym)
        except AVError as e:
            raise HTTPException(404 if "No price data" in str(e) else 503, str(e)) from None

    async def json_body(request: Request) -> dict:
        if "application/json" not in request.headers.get("content-type", ""):
            raise HTTPException(415, "Send JSON")
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "Bad JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(400, "Bad JSON")
        return body

    @app.put("/api/lots")
    async def put_lots(request: Request):
        need(request, edit=True)
        raw = (await json_body(request)).get("lots")
        if not isinstance(raw, list) or len(raw) > MAX_LOTS:
            raise HTTPException(400, "Send a list of lots")
        clean = []
        for lot in raw:
            try:
                ticker = str(lot["ticker"]).strip().upper()
                qty, cost, date = float(lot["qty"]), float(lot["cost"]), str(lot["date"])
                dt.date.fromisoformat(date)
            except (KeyError, TypeError, ValueError):
                raise HTTPException(400, "Each lot needs a ticker, shares, entry price and date") from None
            if not TICKER.match(ticker) or not (0 < qty < 1e12) or not (0 < cost < 1e9):
                raise HTTPException(400, f"Lot for {ticker[:12]} is not valid")
            lot_id = lot.get("id")
            clean.append({"id": lot_id if isinstance(lot_id, int) else None,
                          "ticker": ticker, "qty": qty, "cost": cost, "date": date})
        before = set(await asyncio.to_thread(engine.held))
        await asyncio.to_thread(store.replace_lots, clean)
        engine.kick(sorted(set(await asyncio.to_thread(engine.held)) - before))
        return await asyncio.to_thread(engine.state, True)

    @app.put("/api/watchlist")
    async def put_watch(request: Request):
        need(request, edit=True)
        raw = (await json_body(request)).get("tickers")
        if not isinstance(raw, list) or len(raw) > 40:
            raise HTTPException(400, "Send up to 40 tickers")
        tickers = [str(t).strip().upper() for t in raw if str(t).strip()]
        bad = [t for t in tickers if not TICKER.match(t)]
        if bad:
            raise HTTPException(400, f"Not a ticker: {bad[0][:12]}")
        before = set(await asyncio.to_thread(store.list_watch))
        await asyncio.to_thread(store.replace_watch, tickers)
        engine.kick([t for t in tickers if t not in before])
        return await asyncio.to_thread(engine.state, True)

    @app.put("/api/alerts")
    async def put_alerts(request: Request):
        need(request, edit=True)
        body = await json_body(request)

        def level(v):
            if v is None or v == "":
                return None
            try:
                x = float(v)
            except (TypeError, ValueError):
                raise HTTPException(400, "Alert levels are percentages between 0 and 100") from None
            if not (0 < x <= 100):
                raise HTTPException(400, "Alert levels are percentages between 0 and 100")
            return x

        raw = body.get("by") or {}
        if not isinstance(raw, dict) or len(raw) > MAX_LOTS:
            raise HTTPException(400, "Send alert levels by ticker")
        by = {}
        for t, v in raw.items():
            t = str(t).strip().upper()
            if not TICKER.match(t):
                raise HTTPException(400, f"Not a ticker: {t[:12]}")
            if (x := level(v)) is not None:
                by[t] = x
        await asyncio.to_thread(engine.set_alerts, level(body.get("default")), level(body.get("watchDefault")), by)
        # Check the new levels against the prices already loaded.
        for sym in await asyncio.to_thread(lambda: engine.held() + engine.watchlist()):
            if (q := store.get(f"quote:{sym}")):
                await asyncio.to_thread(engine._check_alert, sym, q)
        return await asyncio.to_thread(engine.state, True)

    # ---- push notifications (per device, opt-in) ----------------------------
    @app.get("/api/push/pubkey")
    def push_pubkey(request: Request):
        need(request)
        return {"key": push.public_key(), "devices": len(push.subscriptions())}

    @app.post("/api/push/subscribe")
    async def push_subscribe(request: Request):
        need(request)
        sub = clean_subscription(await json_body(request))
        if not sub:
            raise HTTPException(400, "That is not a valid push subscription")
        return {"devices": await asyncio.to_thread(push.subscribe, sub)}

    @app.post("/api/push/unsubscribe")
    async def push_unsubscribe(request: Request):
        need(request)
        endpoint = (await json_body(request)).get("endpoint")
        if not isinstance(endpoint, str):
            raise HTTPException(400, "Send the subscription endpoint")
        return {"devices": await asyncio.to_thread(push.unsubscribe, endpoint)}

    @app.post("/api/push/test")
    async def push_test(request: Request):
        need(request)
        endpoint = (await json_body(request)).get("endpoint")
        if not isinstance(endpoint, str) or not any(s["endpoint"] == endpoint for s in push.subscriptions()):
            raise HTTPException(404, "This device is not subscribed")
        result = await asyncio.to_thread(push.deliver, "Test alert", "Price alerts are working on this device.",
                                         "test", endpoint)
        if not result["sent"]:
            raise HTTPException(502, "The push service did not accept the test. Turn alerts off and on again")
        return {"ok": True}

    @app.exception_handler(HTTPException)
    async def http_error(_, exc: HTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app

