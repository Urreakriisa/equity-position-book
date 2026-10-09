"""A ranked screen of the S&P 500 and Nasdaq 100 on four factors: estimate
revisions, valuation against growth, quality and momentum. Pure functions."""
from __future__ import annotations

FACTORS = ("rev", "val", "qual", "mom")
# Two share classes of one company: holding either counts as holding both.
DUAL = {"GOOGL": "GOOG", "GOOG": "GOOGL", "FOX": "FOXA", "FOXA": "FOX", "NWS": "NWSA", "NWSA": "NWS"}


def _percentiles(values: dict[str, float]) -> dict[str, float]:
    """Rank each value against the rest: 0 is the lowest, 100 the highest."""
    order = sorted(values, key=lambda k: values[k])
    n = len(order)
    if n < 2:
        return {k: 50.0 for k in order}
    out, i = {}, 0
    while i < n:                                         # ties share the middle rank
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in order[i:j + 1]:
            out[k] = (i + j) / 2 / (n - 1) * 100
        i = j + 1
    return out


def _blend(parts: list[dict[str, float]]) -> dict[str, float]:
    """Average the percentile of each measure a stock has."""
    ranked = [_percentiles(p) for p in parts if len(p) >= 10]
    out: dict[str, list[float]] = {}
    for r in ranked:
        for k, v in r.items():
            out.setdefault(k, []).append(v)
    return {k: sum(v) / len(v) for k, v in out.items()}


def measures(ov: dict, est: dict | None) -> dict:
    """The raw inputs for one stock, from its overview and estimates."""
    val, px = ov.get("val") or {}, ov.get("px")
    fy = (est or {}).get("fy1") or (est or {}).get("fy2") or {}
    m: dict[str, float | None] = {}
    # revisions: where the fiscal-year EPS estimate moved, and how many analysts moved it
    m["chg30"], m["chg90"] = fy.get("chg30"), fy.get("chg90")
    m["net30"] = (fy["up30"] - fy["down30"]) / fy["n"] if fy.get("n") else None
    # valuation against growth: PEG and forward earnings yield
    peg, fpe = val.get("peg"), ov.get("fpe")
    m["peg"] = peg if peg and 0 < peg < 20 else None
    m["fpe"] = fpe if fpe and 0 < fpe < 200 else None
    # quality
    m["roe"], m["opm"], m["margin"] = val.get("roe"), val.get("opMargin"), val.get("margin")
    # momentum: trend of the averages and distance from the 52-week high
    ma50, ma200, hi = ov.get("ma50"), ov.get("ma200"), val.get("hi52")
    m["trend"] = ma50 / ma200 - 1 if ma50 and ma200 else None
    m["offHigh"] = px / hi - 1 if px and hi else None
    target = (ov.get("target") or {}).get("avg")
    m["upside"] = target / px - 1 if target and px else None
    return m


def rank(universe: list[str], overviews: dict[str, dict], estimates: dict[str, dict],
         held: set[str], watch: set[str], top: int = 20) -> dict:
    """Score every stock that has data and return the best ones not held."""
    raw = {}
    for s in universe:
        ov = overviews.get(s) or {}
        if not ov.get("name") or not ov.get("mktcap"):
            continue
        raw[s] = measures(ov, estimates.get(s))
    pick = lambda key, sign=1: {s: sign * m[key] for s, m in raw.items() if m.get(key) is not None}
    factor = {
        "rev": _blend([pick("chg30"), pick("chg90"), pick("net30")]),
        "val": _blend([pick("peg", -1), {s: 1 / v for s, v in pick("fpe").items()}]),
        "qual": _blend([pick("roe"), pick("opm"), pick("margin")]),
        "mom": _blend([pick("trend"), pick("offHigh")]),
    }
    has_rev = len(factor["rev"]) >= max(10, len(raw) // 4)
    need = ("val", "qual", "mom")
    scored = []
    for s, m in raw.items():
        if any(s not in factor[f] for f in need):
            continue
        parts = {f: factor[f][s] for f in FACTORS if s in factor[f] and (f != "rev" or has_rev)}
        scored.append((sum(parts.values()) / len(parts), s, parts))
    scored.sort(reverse=True)
    position = {s: i + 1 for i, (_, s, _) in enumerate(scored)}

    def row(score, s, parts):
        ov, m = overviews[s], raw[s]
        r = lambda v, d=4: None if v is None else round(v, d)
        return {"t": s, "name": ov.get("name"), "sector": (ov.get("sector") or "").title() or None,
                "rank": position[s], "score": round(score, 1),
                "f": {k: round(v) for k, v in parts.items()},
                "px": ov.get("px"), "mktcap": ov.get("mktcap"), "fpe": r(m["fpe"], 1), "peg": r(m["peg"], 2),
                "chg30": r(m["chg30"]), "chg90": r(m["chg90"]), "roe": r(m["roe"]), "opm": r(m["opm"]),
                "trend": r(m["trend"]), "offHigh": r(m["offHigh"]), "upside": r(m["upside"]),
                "rating": (ov.get("target") or {}).get("rating"), "watch": s in watch}

    owned = held | {DUAL[s] for s in held if s in DUAL}
    picks, seen = [], set()
    for score, s, parts in scored:
        if s in owned or s in seen or DUAL.get(s) in seen:
            continue
        seen.add(s)
        picks.append(row(score, s, parts))
        if len(picks) >= top:
            break
    mine = [row(sc, s, parts) for sc, s, parts in scored if s in held]
    return {"picks": picks, "held": mine, "scored": len(scored), "withData": len(raw),
            "universe": len(universe), "revisions": has_rev,
            "notRanked": sorted(s for s in held if s not in position)}
