"""Data layer for the Highcharts dashboard.

Reuses the existing :class:`Ticker` and :class:`Nasdaq_Leap` classes from
``pc_utils`` for network access and Black-Scholes IV math, but returns plain
JSON-serialisable dictionaries instead of Plotly figures so the plotting can be
done client-side with Highcharts.

Only the two dashboards requested are reproduced:

* Option chain (PUT/CALL open-interest, volume, price and IV per expiry).
* LEAP options (long-dated call theta curves with click-through drill-downs).

Replay history, per-contract option history and the target-closing-price model
are intentionally omitted.
"""

from __future__ import annotations

import math
import re
import threading
import time
import traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from option_tracker.utils.pc_utils import (
    Ticker,
    Nasdaq_Leap,
    db,
    run_dt_yyyy_mm_dd,
    get_risk_free_rate,
    get_headers,
    et_now,
)


# Created lazily on first request so importing the app never triggers network I/O.
_ticker = None

# Shared payload cache so many WebSocket clients don't each hammer the Nasdaq API.
_cache_lock = threading.Lock()
_cache = {"ts": 0.0, "payload": None}

# Reuse Black-Scholes IV across cycles while spot barely moves (recompute anchor).
IV_PRICE_THRESHOLD = 0.50
_iv_state = {"price": None, "date": None, "strike_hash": None, "by_expiry": {}}


def get_cached_option_chain(max_age_seconds=14):
    """Return a recent option-chain payload, rebuilding at most once per window.

    The build is serialised under the lock so concurrent callers share one fetch.
    """
    now = time.time()
    with _cache_lock:
        if _cache["payload"] is not None and now - _cache["ts"] < max_age_seconds:
            return _cache["payload"]
        try:
            payload = build_option_chain_payload()
        except Exception:
            # After-hours / throttled Nasdaq: keep serving the last good snapshot
            # instead of blanking the dashboard. Only surface the error cold.
            if _cache["payload"] is not None:
                traceback.print_exc()
                return _cache["payload"]
            raise
        _cache["payload"] = payload
        _cache["ts"] = time.time()
        return payload


def _get_ticker():
    global _ticker
    if _ticker is None:
        _ticker = Ticker("TSLA")
    return _ticker


def _to_float(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _series(values):
    return [_to_float(v) for v in values]


def _expiry_label(key):
    """Normalise a groupby key (tuple/Timestamp/str) to ``%b-%d-%Y``."""
    if isinstance(key, tuple):
        key = key[0]
    if isinstance(key, str):
        return key
    return pd.to_datetime(key).strftime("%b-%d-%Y")


def _merge_daily_volume(df):
    """Attach start-of-day volume so daily volume = current - open snapshot.

    Falls back to total volume when no snapshot exists for today.
    """
    try:
        df_vol = db.query_sql_data(
            "with st_tm as (select min(load_tm) as tm from tsla_nasdaq "
            f"where load_dt = '{run_dt_yyyy_mm_dd}') "
            "select * from st_tm, tsla_nasdaq "
            f"where load_dt = '{run_dt_yyyy_mm_dd}' and load_tm = st_tm.tm"
        )
        df_vol["p_Volume_1"] = pd.to_numeric(df_vol["p_Volume"].astype(str), errors="coerce").fillna(0)
        df_vol["c_Volume_1"] = pd.to_numeric(df_vol["c_Volume"].astype(str), errors="coerce").fillna(0)
        df = df.merge(
            df_vol[["expiryDate", "strike", "p_Volume_1", "c_Volume_1"]],
            on=["expiryDate", "strike"],
            how="left",
        )
        df["c_Volume_1"] = df["c_Volume"] - df["c_Volume_1"]
        df["p_Volume_1"] = df["p_Volume"] - df["p_Volume_1"]
    except Exception:
        pass
    if "c_Volume_1" not in df.columns:
        df["c_Volume_1"] = df["c_Volume"]
        df["p_Volume_1"] = df["p_Volume"]
    return df


def _bs_gamma(S, K, T, sigma):
    """Black-Scholes gamma (per share); same for calls and puts."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    vs = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / vs
    return math.exp(-0.5 * d1 * d1) / (math.sqrt(2 * math.pi) * S * vs)


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_call_delta(S, K, T, sigma, r):
    """Black-Scholes call delta N(d1); put delta = this - 1."""
    if S <= 0 or K <= 0 or sigma <= 0 or T <= 0:
        return 1.0 if S > K else 0.0
    vs = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / vs
    return _ncdf(d1)


def _expiry_walls(g, spot, bus_days):
    """Per-expiry gamma walls: strikes of max (+) and min (-) net GEX for one expiry."""
    if not spot or spot <= 0 or not bus_days or bus_days <= 0:
        return None, None
    T = bus_days / 252.0
    by = {}
    for _, row in g.iterrows():
        K = _to_float(row.get("strike"))
        if K is None or K <= 0:
            continue
        civ = _to_float(row.get("c_IV"))
        piv = _to_float(row.get("p_IV"))
        coi = _to_float(row.get("c_Openinterest")) or 0
        poi = _to_float(row.get("p_Openinterest")) or 0
        gg = (_bs_gamma(spot, K, T, civ) * coi if civ else 0) - (_bs_gamma(spot, K, T, piv) * poi if piv else 0)
        by[K] = by.get(K, 0.0) + gg
    if not by:
        return None, None
    return max(by, key=by.get), min(by, key=by.get)


def _wheel_candidates(g, expirydt, spot, prev_spot, lower_2sigma, upper_2sigma):
    """Premium-selling candidates for one expiry: OTM cash-secured puts and covered calls.

    Uses the bid (what a seller actually collects) and each strike's IV to derive
    Black-Scholes delta, probability of expiring OTM, and annualized return on capital.
    Only the sellable delta band (0.05-0.45) is kept.
    """
    rows = []
    if spot is None or spot <= 0:
        return rows
    try:
        expiry_close = pd.to_datetime(expirydt, format="%b-%d-%Y").replace(hour=16)
    except (ValueError, TypeError):
        return rows
    cal_days = max((expiry_close - et_now()).total_seconds() / 86400.0, 0.01)
    T = max(cal_days / 365.0, 1e-6)
    r = get_risk_free_rate(cal_days)
    dte = int(round(cal_days))

    def _annual(prem, cap, days):
        return round(prem / cap * (365.0 / days) * 100.0, 1) if (prem and prem > 0 and cap and days > 0) else None

    for _, row in g.iterrows():
        K = _to_float(row.get("strike"))
        if K is None or K <= 0:
            continue
        put = K < spot  # OTM put -> CSP;  OTM call -> CC
        iv = _to_float(row.get("p_IV" if put else "c_IV"))
        bid = _to_float(row.get("p_Bid" if put else "c_Bid"))
        if not iv or iv <= 0 or not bid or bid <= 0:
            continue
        vs = iv * math.sqrt(T)
        d1 = (math.log(spot / K) + (r + 0.5 * iv * iv) * T) / vs
        delta = _ncdf(d1) - 1.0 if put else _ncdf(d1)
        adelta = abs(delta)
        if adelta < 0.05 or adelta > 0.45:
            continue
        # Today's mark and yesterday's close, kept in the payload (mark not plotted).
        ask = _to_float(row.get("p_Ask" if put else "c_Ask"))
        last = _to_float(row.get("p_Last" if put else "c_Last"))
        change = _to_float(row.get("p_Change" if put else "c_Change"))
        mark = (bid + ask) / 2.0 if (ask and ask > 0) else last
        prev = (last - change) if (last is not None and change is not None) else None
        prev_cal = cal_days + 1.0  # one more calendar day to expiry yesterday
        cap_today = K if put else spot
        cap_prev = K if put else (prev_spot if (prev_spot and prev_spot > 0) else spot)
        rows.append({
            "side": "CSP" if put else "CC", "expiry": expirydt, "strike": K, "dte": dte,
            "delta": round(adelta, 3), "pop": round((1 - adelta) * 100, 1),
            "bid": round(bid, 2),
            "ann_roc": _annual(bid, cap_today, cal_days),
            "ann_roc_mark": _annual(mark, cap_today, cal_days),
            "ann_roc_prev": _annual(prev, cap_prev, prev_cal),
            "breakeven": round((K - bid) if put else (K + bid), 2),
            "cushion": round(((spot - K) if put else (K - spot)) / spot * 100, 1),
            "beyond_move": bool((lower_2sigma is not None and K < lower_2sigma) if put
                                else (upper_2sigma is not None and K > upper_2sigma)),
        })
    return rows


def _compute_gex(expiries, spot):
    """Aggregate dealer gamma exposure (GEX) across expiries.

    Convention: dealers long calls / short puts, so call gamma adds and put
    gamma subtracts. Per-strike value is dollar gamma per 1% spot move
    (Γ·OI·100·S²·0.01). Returns the profile plus gamma-flip and wall levels.
    """
    if not spot or spot <= 0:
        return None
    # Flatten to (strike, tenor, call_iv, call_oi, put_iv, put_oi) legs.
    legs = []
    for e in expiries:
        bd = e.get("bus_days")
        if not bd or bd <= 0:
            continue
        T = bd / 252.0
        strikes, c_oi, p_oi = e.get("strikes") or [], e.get("c_oi") or [], e.get("p_oi") or []
        c_iv, p_iv = e.get("c_iv") or [], e.get("p_iv") or []
        for i, K in enumerate(strikes):
            if K is None:
                continue
            civ = c_iv[i] / 100.0 if i < len(c_iv) and c_iv[i] else None
            piv = p_iv[i] / 100.0 if i < len(p_iv) and p_iv[i] else None
            coi = c_oi[i] if i < len(c_oi) and c_oi[i] else 0
            poi = p_oi[i] if i < len(p_oi) and p_oi[i] else 0
            if (civ is None and piv is None) or (coi == 0 and poi == 0):
                continue
            legs.append((K, T, civ, coi, piv, poi))
    if not legs:
        return None

    MULT = 100 * 0.01  # contract multiplier × 1% move

    def total_at(S):
        s2 = S * S
        tot = 0.0
        for K, T, civ, coi, piv, poi in legs:
            g = 0.0
            if civ:
                g += _bs_gamma(S, K, T, civ) * coi
            if piv:
                g -= _bs_gamma(S, K, T, piv) * poi
            tot += g * s2
        return tot * MULT

    # Per-strike net GEX at the current spot, summed across expiries.
    s2 = spot * spot
    by_strike = {}
    for K, T, civ, coi, piv, poi in legs:
        g = 0.0
        if civ:
            g += _bs_gamma(spot, K, T, civ) * coi
        if piv:
            g -= _bs_gamma(spot, K, T, piv) * poi
        by_strike[K] = by_strike.get(K, 0.0) + g * s2 * MULT
    strikes_sorted = sorted(by_strike)

    # Total GEX vs hypothetical spot: build the curve and every zero-crossing.
    lo, hi, n = spot * 0.80, spot * 1.18, 96
    curve, flips = [], []
    prev_s = prev_v = None
    for i in range(n):
        S = lo + (hi - lo) * i / (n - 1)
        v = total_at(S) / 1e6
        curve.append([_to_float(S), _to_float(v)])
        if prev_v is not None:
            if v == 0:
                flips.append(S)
            elif prev_v * v < 0:
                flips.append(prev_s + (S - prev_s) * (-prev_v) / (v - prev_v))
        prev_s, prev_v = S, v
    flip = min(flips, key=lambda f: abs(f - spot)) if flips else None

    total_spot = total_at(spot)
    call_wall = max(strikes_sorted, key=lambda k: by_strike[k]) if strikes_sorted else None
    put_wall = min(strikes_sorted, key=lambda k: by_strike[k]) if strikes_sorted else None
    return {
        "spot": _to_float(spot),
        "flip": _to_float(flip),
        "flips": [_to_float(f) for f in flips],
        "call_wall": _to_float(call_wall),
        "put_wall": _to_float(put_wall),
        "regime": "long" if total_spot >= 0 else "short",
        "total_mm": _to_float(total_spot / 1e6),
        "strikes": [_to_float(k) for k in strikes_sorted],
        "net_mm": [_to_float(by_strike[k] / 1e6) for k in strikes_sorted],
        "curve": curve,
    }


def _compute_charm(expiries, spot):
    """Charm-driven dealer hedge flow between now and today's 4pm close.

    Charm = delta decay: with spot and IV held fixed, option deltas drift as time
    passes, forcing dealers to re-hedge. Convention matches GEX (dealers long
    calls / short puts). Positive flow = dealers must BUY into the close (upward
    pin drift); negative = must SELL (downward). Reported in $mm.
    """
    if not spot or spot <= 0:
        return None
    now = et_now()
    close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    hours = (close - now).total_seconds() / 3600.0
    if hours <= 0.01:
        return None  # after the close: no remaining intraday decay
    dt_year = (hours / 24.0) / 365.0

    by_strike = {}
    for e in expiries:
        try:
            ec = pd.to_datetime(e["expiry"], format="%b-%d-%Y").replace(hour=16)
        except (KeyError, ValueError, TypeError):
            continue
        cal_days = (ec - now).total_seconds() / 86400.0
        if cal_days <= 0:
            continue
        T0 = cal_days / 365.0
        T1 = max(T0 - dt_year, 1e-9)
        r = get_risk_free_rate(cal_days)
        strikes = e.get("strikes") or []
        c_oi, p_oi = e.get("c_oi") or [], e.get("p_oi") or []
        c_iv, p_iv = e.get("c_iv") or [], e.get("p_iv") or []
        for i, K in enumerate(strikes):
            if K is None:
                continue
            coi = c_oi[i] if i < len(c_oi) and c_oi[i] else 0
            poi = p_oi[i] if i < len(p_oi) and p_oi[i] else 0
            civ = c_iv[i] / 100.0 if i < len(c_iv) and c_iv[i] else None
            piv = p_iv[i] / 100.0 if i < len(p_iv) and p_iv[i] else None
            if coi == 0 and poi == 0:
                continue

            def dealer_delta(T):
                d = 0.0
                if civ:
                    d += _bs_call_delta(spot, K, T, civ, r) * coi          # long calls
                if piv:
                    d += -(_bs_call_delta(spot, K, T, piv, r) - 1.0) * poi  # short puts
                return d

            change = dealer_delta(T1) - dealer_delta(T0)  # per-share delta drift
            hedge = -change * 100.0                        # shares dealers trade to stay neutral
            by_strike[K] = by_strike.get(K, 0.0) + hedge * spot  # dollar flow

    if not by_strike:
        return None
    strikes_sorted = sorted(by_strike)
    total = sum(by_strike.values())
    return {
        "hours_to_close": round(hours, 2),
        "flow_mm": _to_float(total / 1e6),
        "direction": "up" if total >= 0 else "down",
        "strikes": [_to_float(k) for k in strikes_sorted],
        "flow_by_strike_mm": [_to_float(by_strike[k] / 1e6) for k in strikes_sorted],
    }


def build_option_chain_payload():
    """Return the option-chain dashboard data for every tracked expiry."""
    _ticker = _get_ticker()
    _ticker.get_lastSalePrice()
    last_price = _ticker.lastSalePrice
    df = _ticker.oic_api_call()
    if df is None or df.empty:
        raise RuntimeError("No option-chain data returned from Nasdaq.")

    numeric_cols = df.filter(regex="c_|p_|strike").columns
    df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors="coerce")
    df = _merge_daily_volume(df)

    # IV is the expensive part; reuse it while spot stays within threshold.
    strike_hash = _ticker._compute_strike_hash(df)
    iv_valid = (
        bool(_iv_state["by_expiry"])
        and _iv_state["price"] is not None
        and abs(last_price - _iv_state["price"]) < IV_PRICE_THRESHOLD
        and _iv_state["date"] == _ticker._get_et_date()
        and _iv_state["strike_hash"] == strike_hash
    )
    fresh_iv = {}

    expiries = []
    wheel_rows = []
    for key, grp in df.sort_values(by=["expirygroup"]).groupby(["expirygroup"]):
        expirydt = _expiry_label(key)
        g = grp.sort_values(by="strike").copy()

        cached = _iv_state["by_expiry"].get(expirydt) if iv_valid else None
        if cached is not None:
            g = g.merge(cached, on="strike", how="left")
        else:
            g = _ticker.add_implied_volatility_columns(expirydt, g)
        iv_cols = [c for c in ("strike", "c_IV", "p_IV", "c_IV_%", "p_IV_%") if c in g.columns]
        if len(iv_cols) > 1:
            fresh_iv[expirydt] = g[iv_cols].copy()

        g["c_1"] = g["c_Last"] - g["c_Change"]
        g["p_1"] = g["p_Last"] - g["p_Change"]

        atm = None
        upper_2sigma = lower_2sigma = bus_days = move = prob = None
        if "c_IV_%" in g.columns and g["c_IV_%"].notna().any():
            atm_idx = (g["strike"] - last_price).abs().idxmin()
            atm_strike = _to_float(g.loc[atm_idx, "strike"])
            call_iv = _to_float(g.loc[atm_idx, "c_IV_%"])
            put_iv = _to_float(g.loc[atm_idx, "p_IV_%"])
            avg_iv = None
            if call_iv is not None and put_iv is not None:
                avg_iv = (call_iv + put_iv) / 2
            atm = {
                "strike": atm_strike,
                "call_iv": call_iv,
                "put_iv": put_iv,
                "avg_iv": avg_iv,
                "call_price": _to_float(g.loc[atm_idx, "c_Last"]),
                "put_price": _to_float(g.loc[atm_idx, "p_Last"]),
            }

            if avg_iv is not None:
                # One total volatility (σ√T) on the trading-day tenor drives BOTH the
                # 2σ move and the probability, so 3σ stays consistent with 2σ.
                expiry_close = pd.to_datetime(expirydt, format="%b-%d-%Y").replace(hour=16)
                bus_days = max(float(np.busday_count(et_now().date(), expiry_close.date())), 0.25)
                cal_days = max((expiry_close - et_now()).total_seconds() / 86400.0, 0.01)
                sigma_dec = avg_iv / 100.0
                T = bus_days / 252.0
                sig = sigma_dec * np.sqrt(T)  # total vol to expiry
                move = last_price * sig * 2
                upper_2sigma = last_price + move
                lower_2sigma = last_price - move

                # Probability of expiring: lognormal ±3σ using the same total vol.
                r_prob = get_risk_free_rate(cal_days)
                drift = (r_prob - 0.5 * sigma_dec ** 2) * T  # risk-neutral, q=0 (TSLA)
                z = 3.0
                p_tail = 0.5 * (1 - math.erf(z / np.sqrt(2)))
                prob = {
                    "sigma": z,
                    "lower": _to_float(last_price * np.exp(drift - z * sig)),
                    "upper": _to_float(last_price * np.exp(drift + z * sig)),
                    "p_below": round(p_tail * 100, 2),
                    "p_between": round((1 - 2 * p_tail) * 100, 2),
                    "p_above": round(p_tail * 100, 2),
                    "cal_days": _to_float(cal_days),
                    "rate": _to_float(r_prob),
                    "drift": _to_float(drift),
                }

        _ew_cw, _ew_pw = _expiry_walls(g, last_price, bus_days) if bus_days else (None, None)
        expiries.append({
            "expiry": expirydt,
            "strikes": _series(g["strike"].values),
            "c_oi": _series(g["c_Openinterest"].values),
            "p_oi": _series(g["p_Openinterest"].values),
            "c_vol": _series(g["c_Volume_1"].values),
            "p_vol": _series(g["p_Volume_1"].values),
            "c_last": _series(g["c_Last"].values),
            "c_prev": _series(g["c_1"].values),
            "p_last": _series(g["p_Last"].values),
            "p_prev": _series(g["p_1"].values),
            "c_iv": _series(g["c_IV_%"].values) if "c_IV_%" in g.columns else [],
            "p_iv": _series(g["p_IV_%"].values) if "p_IV_%" in g.columns else [],
            "atm": atm,
            "upper_2sigma": _to_float(upper_2sigma),
            "lower_2sigma": _to_float(lower_2sigma),
            "bus_days": _to_float(bus_days),
            "move": _to_float(move),
            "prob": prob,
            "call_wall": _to_float(_ew_cw),
            "put_wall": _to_float(_ew_pw),
        })
        wheel_rows.extend(_wheel_candidates(g, expirydt, last_price, _ticker.prev_busday_close_price, lower_2sigma, upper_2sigma))

    wheel_rows.sort(key=lambda x: x["ann_roc"], reverse=True)

    if not iv_valid:
        _iv_state.update({
            "price": last_price,
            "date": _ticker._get_et_date(),
            "strike_hash": strike_hash,
            "by_expiry": fresh_iv,
        })

    return {
        "ticker": _ticker.ticker,
        "lastSalePrice": _to_float(last_price),
        "prevClose": _to_float(_ticker.prev_busday_close_price),
        "marketStatus": _ticker.marketStatus,
        "dataSource": _ticker.dataSource,
        "timestamp": et_now().strftime("%I:%M %p"),
        "expiries": expiries,
        "gex": _compute_gex(expiries, last_price),
        "charm": _compute_charm(expiries, last_price),
        "wheel": wheel_rows,
    }


def _fetch_symbol_chart(symbol, assetclass):
    """Intraday [epoch_ms, price] points from the Nasdaq chart endpoint."""
    url = f"https://api.nasdaq.com/api/quote/{symbol}/chart?assetclass={assetclass}"
    resp = requests.get(url, headers=get_headers(), timeout=15)
    resp.raise_for_status()
    chart = (resp.json().get("data") or {}).get("chart") or []
    return [[int(p["x"]), _to_float(p["y"])] for p in chart if p.get("y") is not None]


def _fetch_symbol_quote(symbol, assetclass):
    """Return (last_price, prev_close) from the Nasdaq info endpoint."""
    url = f"https://api.nasdaq.com/api/quote/{symbol}/info?assetclass={assetclass}"
    resp = requests.get(url, headers=get_headers(), timeout=15)
    resp.raise_for_status()
    d = (resp.json().get("data") or {}).get("primaryData") or {}
    last = prev = None
    m = re.findall(r"\d+\.\d+", str(d.get("lastSalePrice", "")))
    if m:
        last = float(m[0])
    try:
        nc = float(str(d.get("netChange", "")).replace(",", ""))
        if last is not None:
            prev = last - nc
    except (ValueError, AttributeError):
        pass
    return last, prev


def build_spot_payload():
    """Return TSLA + SPY intraday spot series bounded to the 9:30-16:00 ET session."""
    points = _fetch_symbol_chart("TSLA", "stocks")
    tk = _get_ticker()
    if tk.prev_busday_close_price is None:  # populate prev close on first spot load
        try:
            tk.get_lastSalePrice()
        except Exception:
            pass
    spy_points, spy_last, spy_prev = [], None, None
    try:
        spy_points = _fetch_symbol_chart("SPY", "etf")
        spy_last, spy_prev = _fetch_symbol_quote("SPY", "etf")
    except Exception:
        traceback.print_exc()
    if spy_last is None and spy_points:
        spy_last = spy_points[-1][1]
    return {
        "ticker": "TSLA",
        "points": points,
        "last": points[-1][1] if points else None,
        "prevClose": _to_float(tk.prev_busday_close_price),
        "sessionStart": _et_open_epoch_ms(),
        "sessionEnd": _et_close_epoch_ms(),
        "spy_points": spy_points,
        "spy_last": _to_float(spy_last),
        "spy_prevClose": _to_float(spy_prev),
    }


def _et_open_epoch_ms():
    """Today's 9:30 AM ET as an epoch in Nasdaq's ET-as-UTC convention."""
    op = datetime.now(ZoneInfo("America/New_York")).replace(hour=9, minute=30, second=0, microsecond=0)
    return int(op.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _et_close_epoch_ms():
    """Today's 4:00 PM ET as an epoch in Nasdaq's ET-as-UTC convention."""
    close = datetime.now(ZoneInfo("America/New_York")).replace(hour=16, minute=0, second=0, microsecond=0)
    return int(close.replace(tzinfo=timezone.utc).timestamp() * 1000)


# Shared short-TTL cache so many spot-stream clients don't each hit the quote API.
_spot_lock = threading.Lock()
_spot_cache = {"ts": 0.0, "price": None, "status": None}


def get_cached_spot_price(max_age_seconds=4.0):
    """Return (last_price, market_status), refreshing at most every few seconds."""
    now = time.time()
    with _spot_lock:
        if _spot_cache["price"] is not None and now - _spot_cache["ts"] < max_age_seconds:
            return _spot_cache["price"], _spot_cache["status"]
        tk = _get_ticker()
        tk.get_lastSalePrice()
        _spot_cache["price"] = _to_float(tk.lastSalePrice)
        _spot_cache["status"] = tk.marketStatus
        _spot_cache["ts"] = time.time()
        return _spot_cache["price"], _spot_cache["status"]


_spy_lock = threading.Lock()
_spy_cache = {"ts": 0.0, "price": None}


def get_cached_spy_price(max_age_seconds=4.0):
    """Return SPY's live last price, refreshing at most every few seconds."""
    now = time.time()
    with _spy_lock:
        if _spy_cache["price"] is not None and now - _spy_cache["ts"] < max_age_seconds:
            return _spy_cache["price"]
        try:
            last, _ = _fetch_symbol_quote("SPY", "etf")
        except Exception:
            last = None
        if last is not None:
            _spy_cache["price"] = _to_float(last)
            _spy_cache["ts"] = time.time()
        return _spy_cache["price"]


def build_leap_payload():
    """Return the LEAP-options theta curves plus per-point drill-down URLs."""
    nl = Nasdaq_Leap()
    df, dict_color = nl.get_nasdaq_leap_option_chain()
    if df is None or df.empty:
        raise RuntimeError("No LEAP data returned from Nasdaq.")

    df["c_Volume_1"] = pd.to_numeric(df["c_Volume"].astype(str), errors="coerce").fillna(0)
    df["c_Openinterest"] = pd.to_numeric(df["c_Openinterest"].astype(str), errors="coerce").fillna(0)
    spot = df["strike"].min()

    def marker_radius(strike, oi):
        if strike > spot:
            return max(oi / 600.0, 0)
        return 0

    series = []
    for expirydt, grp in df.groupby("expirygroup"):
        g = grp.sort_values(by="strike")
        days = int((pd.to_datetime(expirydt, format="%b %d %Y") - pd.Timestamp.today()) / np.timedelta64(1, "D"))
        green = int(dict_color.get(expirydt, 128))
        points = []
        for _, row in g.iterrows():
            strike = _to_float(row["strike"])
            last = _to_float(row["c_Last"])
            oi = _to_float(row["c_Openinterest"]) or 0
            vol = _to_float(row["c_Volume_1"]) or 0
            points.append({
                "x": strike,
                "y": last,
                "oi": oi,
                "vol": vol,
                "radius": round(marker_radius(strike or 0, oi), 2),
                "url": row.get("drillDownURL", ""),
            })
        series.append({
            "expiry": expirydt,
            "color": f"rgb(0,{green},0)",
            "days_to_expiry": days,
            "points": points,
        })

    # Order legend the way the Plotly version did: nearest expiry first.
    series.sort(key=lambda s: s["days_to_expiry"])
    return {
        "ticker": "TSLA",
        "timestamp": datetime.today().strftime("%I:%M %p"),
        "series": series,
    }
