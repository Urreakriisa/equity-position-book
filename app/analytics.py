"""Portfolio analytics on daily closes: allocation, correlation, returns
against the S&P 500, drawdown and attribution. Pure functions, no I/O."""
from __future__ import annotations

import math

PERIODS = (("1M", "1 month"), ("YTD", "Year to date"), ("1Y", "1 year"))


def _corr(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 20:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)


def _drawdown(index: list[float], dates: list[str]) -> dict | None:
    """Largest peak-to-trough fall of an index series."""
    if len(index) < 2:
        return None
    peak, peak_i, worst = index[0], 0, (0.0, 0, 0)
    for i, v in enumerate(index):
        if v > peak:
            peak, peak_i = v, i
        fall = v / peak - 1
        if fall < worst[0]:
            worst = (fall, peak_i, i)
    return {"pct": round(worst[0], 4), "peak": dates[worst[1]], "trough": dates[worst[2]]}


def _vol(returns: list[float]) -> float | None:
    n = len(returns)
    if n < 20:
        return None
    m = sum(returns) / n
    return round(math.sqrt(sum((r - m) ** 2 for r in returns) / (n - 1) * 252), 4)


def actual_series(lots: list[dict], closes: dict[str, dict[str, float]], dates: list[str]) -> dict | None:
    """Time-weighted index of the book as it was actually built: each lot
    joins on its purchase date at its entry price, and money added does not
    count as return. Positions that were sold are not known to the app."""
    lots = [l for l in lots if l["ticker"] in closes and l.get("date")]
    if not lots or not dates:
        return None
    first = min(l["date"] for l in lots)
    start = next((i for i, d in enumerate(dates) if d >= first), None)
    if start is None:
        return None
    entry = {}                                           # lot -> index of the session it joins on
    for k, l in enumerate(lots):
        entry[k] = next((i for i in range(start, len(dates)) if dates[i] >= l["date"]), None)
    prev_px: dict[str, float] = {}
    for sym in {l["ticker"] for l in lots}:              # last close before the start, for lots already held
        before = [closes[sym][d] for d in dates[:start] if d in closes[sym]]
        if before:
            prev_px[sym] = before[-1]
    out_d = [dates[start - 1] if start > 0 else dates[start]]
    index, rets, value_prev = [100.0], [], 0.0
    for i in range(start, len(dates)):
        d = dates[i]
        px = {sym: closes[sym].get(d, prev_px.get(sym)) for sym in {l["ticker"] for l in lots}}
        pnl, base, value = 0.0, value_prev, 0.0
        for k, l in enumerate(lots):
            p = px.get(l["ticker"])
            if entry[k] is None or entry[k] > i or p is None:
                continue
            if entry[k] == i:
                pnl += l["qty"] * (p - l["cost"])
                base += l["qty"] * l["cost"]
            else:
                pnl += l["qty"] * (p - prev_px.get(l["ticker"], p))
            value += l["qty"] * p
        r = pnl / base if base > 0 else 0.0
        rets.append(r)
        index.append(index[-1] * (1 + r))
        out_d.append(d)
        value_prev = value
        prev_px.update({s: p for s, p in px.items() if p is not None})
    return {"dates": out_d, "index": index, "returns": rets}


def backcast(weights: dict[str, float], closes: dict[str, dict[str, float]], dates: list[str]) -> dict | None:
    """Index of today's holdings held at today's weights (rebalanced daily)
    over `dates`. A what-if, not what the book earned."""
    syms = [s for s in weights if s in closes]
    use = [d for d in dates if all(d in closes[s] for s in syms)]
    total = sum(weights[s] for s in syms)
    if len(use) < 2 or total <= 0:
        return None
    index, rets = [100.0], []
    for a, z in zip(use, use[1:]):
        r = sum(weights[s] / total * (closes[s][z] / closes[s][a] - 1) for s in syms)
        rets.append(r)
        index.append(index[-1] * (1 + r))
    return {"dates": use, "index": index, "returns": rets}


def compute(lots: list[dict], hist: dict[str, list[list]], spy: list[list],
            sectors: dict[str, str | None], names: dict[str, str] | None = None) -> dict:
    """`hist` maps ticker -> ascending [date, o, h, l, c] rows; `spy` is the benchmark."""
    names = names or {}
    closes = {s: {r[0]: r[4] for r in rows} for s, rows in hist.items() if rows}
    held: dict[str, dict] = {}
    for l in lots:
        if l["ticker"] not in closes:
            continue
        p = held.setdefault(l["ticker"], {"qty": 0.0, "cost": 0.0})
        p["qty"] += l["qty"]
        p["cost"] += l["qty"] * l["cost"]
    if not held:
        return {"ready": False}
    last = {s: hist[s][-1][4] for s in held}
    mv = {s: held[s]["qty"] * last[s] for s in held}
    total = sum(mv.values())
    w = {s: mv[s] / total for s in held}
    order = sorted(held, key=lambda s: -w[s])

    # ---- allocation and concentration
    by_sector: dict[str, list[str]] = {}
    for s in order:
        by_sector.setdefault((sectors.get(s) or "Unclassified").title(), []).append(s)
    sector_rows = sorted(({"name": k, "weight": round(sum(w[s] for s in v), 4), "tickers": v}
                          for k, v in by_sector.items()), key=lambda r: -r["weight"])
    hhi = sum(x * x for x in w.values())
    conc = {"top1": round(w[order[0]], 4), "top3": round(sum(w[s] for s in order[:3]), 4),
            "effective": round(1 / hhi, 1), "count": len(order), "topTicker": order[0]}

    # ---- correlation of daily returns, last 126 sessions every holding has
    common = sorted(set.intersection(*(set(closes[s]) for s in order)))[-127:]
    rets = {s: [closes[s][z] / closes[s][a] - 1 for a, z in zip(common, common[1:])] for s in order}
    matrix, pairs = [], []
    for i, a in enumerate(order):
        row = []
        for j, b in enumerate(order):
            c = 1.0 if i == j else _corr(rets[a], rets[b])
            row.append(None if c is None else round(c, 2))
            if j > i and c is not None:
                pairs.append((c, a, b))
        matrix.append(row)
    pairs.sort(reverse=True)
    corr = {"tickers": order, "matrix": matrix, "sessions": max(0, len(common) - 1),
            "avg": round(sum(p[0] for p in pairs) / len(pairs), 2) if pairs else None,
            "highest": [{"a": a, "b": b, "c": round(c, 2)} for c, a, b in pairs[:3]],
            "lowest": [{"a": a, "b": b, "c": round(c, 2)} for c, a, b in pairs[-3:][::-1]] if len(pairs) > 3 else []}

    # ---- returns against the benchmark
    spy_c = {r[0]: r[4] for r in spy}
    dates = [r[0] for r in spy]
    out = {"ready": True, "asOf": dates[-1] if dates else None, "sectors": sector_rows,
           "concentration": conc, "correlation": corr, "periods": [], "attribution": []}
    if len(dates) < 2:
        return out
    bench = lambda a, z: spy_c[z] / spy_c[a] - 1
    act = actual_series(lots, closes, dates)
    inception = act["dates"][0] if act else None
    year = dates[-1][:4]
    starts = {"1M": dates[-22] if len(dates) > 22 else None,
              "YTD": next((d for d in reversed(dates) if d[:4] < year), None),
              "1Y": dates[-253] if len(dates) > 253 else None}
    back_1y = None
    for key, label in PERIODS:
        a = starts[key]
        if not a:
            continue
        if act and inception <= a and a in act["dates"]:
            i = act["dates"].index(a)
            port, mode = act["index"][-1] / act["index"][i] - 1, "actual"
        else:
            b = backcast(w, closes, [d for d in dates if d >= a])
            if not b:
                continue
            a, port, mode = b["dates"][0], b["index"][-1] / 100 - 1, "backcast"
            if key == "1Y":
                back_1y = b
        out["periods"].append({"key": key, "label": label, "from": a, "mode": mode,
                               "port": round(port, 4), "bench": round(bench(a, dates[-1]), 4)})
    if act and len(act["dates"]) >= 2:
        a = act["dates"][0]
        out["periods"].append({"key": "ALL", "label": "Since first purchase", "from": a, "mode": "actual",
                               "port": round(act["index"][-1] / 100 - 1, 4),
                               "bench": round(bench(a, dates[-1]), 4) if a in spy_c else None})
        base = spy_c.get(a)
        out["actual"] = {"dates": act["dates"], "port": [round(x, 2) for x in act["index"]],
                         "bench": [round(spy_c[d] / base * 100, 2) if base and d in spy_c else None for d in act["dates"]],
                         "sessions": len(act["returns"]), "vol": _vol(act["returns"]),
                         "drawdown": _drawdown(act["index"], act["dates"])}
    if back_1y is None and len(dates) > 253:
        back_1y = backcast(w, closes, dates[-253:])
    if back_1y is None:
        back_1y = backcast(w, closes, dates)
    if back_1y:
        d0 = back_1y["dates"][0]
        out["backcast"] = {"dates": back_1y["dates"], "port": [round(x, 2) for x in back_1y["index"]],
                           "bench": [round(spy_c[d] / spy_c[d0] * 100, 2) for d in back_1y["dates"]],
                           "sessions": len(back_1y["returns"]), "vol": _vol(back_1y["returns"]),
                           "drawdown": _drawdown(back_1y["index"], back_1y["dates"]),
                           "benchDrawdown": _drawdown([spy_c[d] for d in back_1y["dates"]], back_1y["dates"])}

    # ---- attribution: who made the money, and who would have over a year
    pl = {s: mv[s] - held[s]["cost"] for s in held}
    total_pl, total_cost = sum(pl.values()), sum(p["cost"] for p in held.values())
    span = back_1y["dates"] if back_1y else dates
    for s in order:
        a = next((d for d in span if d in closes[s]), None)
        r1 = closes[s][span[-1]] / closes[s][a] - 1 if a and span[-1] in closes[s] else None
        out["attribution"].append({
            "t": s, "name": names.get(s) or s, "weight": round(w[s], 4), "pl": round(pl[s], 2),
            "ret": round(pl[s] / held[s]["cost"], 4) if held[s]["cost"] else None,
            "contrib": round(pl[s] / total_cost, 4) if total_cost else None,
            "share": round(pl[s] / total_pl, 4) if abs(total_pl) > 1e-9 else None,
            "ret1y": None if r1 is None else round(r1, 4),
            "contrib1y": None if r1 is None else round(w[s] * r1, 4)})
    out["attributionFrom"] = span[0]
    return out
