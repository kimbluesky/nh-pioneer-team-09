"""Closed-bar relative trend, actual pullback, and cost-aware risk levels.

This is a hypothesis to forward-test, not an estimate of expected profit.
Only synchronized samples already observed by the coordinator are accepted.
"""
from __future__ import annotations

import math
import statistics
from typing import Any


def update_trend(pair: dict[str, Any], config: Any, at: int, spread: float) -> None:
    """Use completed 60-second bars; never include the current bar in ATR/trend."""
    bucket = at // 60_000
    bar = pair.get("trend_open_bar")
    bars = pair.setdefault("trend_history", [])
    if bar and bucket > bar["bucket"]:
        if (bucket == bar["bucket"] + 1 and bar["count"] >= max(3, math.ceil(36 / config.sample_seconds))
                and bar["last_at"] - bar["first_at"] >= 30_000):
            bars.append(bar)
            del bars[:-max(config.trend_bars + 1, 8)]
        else:
            bars.clear()
            pair.pop("setup", None)
        bar = None
    if bar is None:
        bar = {"bucket": bucket, "open": spread, "high": spread, "low": spread,
               "close": spread, "count": 0, "first_at": at, "last_at": at}
        pair["trend_open_bar"] = bar
    if at < bar["last_at"] or (at == bar["last_at"] and bar["count"]):
        return
    bar.update(high=max(bar["high"], spread), low=min(bar["low"], spread),
               close=spread, count=bar["count"] + 1, last_at=at)
    pair.update(trend_count=len(bars), trend_direction=0, trend_atr=None, trend_efficiency=None)
    if len(bars) < config.trend_bars:
        return
    window = bars[-config.trend_bars:]
    prior = bars[-config.trend_bars - 1]["close"] if len(bars) > config.trend_bars else window[0]["open"]
    true_ranges, changes = [], []
    first = prior
    for one in window:
        true_ranges.append(max(one["high"] - one["low"], abs(one["high"] - prior), abs(one["low"] - prior)))
        changes.append(abs(one["close"] - prior))
        prior = one["close"]
    atr = statistics.fmean(true_ranges)
    delta = window[-1]["close"] - first
    efficiency = abs(delta) / sum(changes) if sum(changes) > 1e-12 else 0.0
    direction = (1 if delta > 0 else -1) if atr > 1e-12 and abs(delta) >= config.trend_min_atr * atr and efficiency >= config.min_trend_efficiency else 0
    pair.update(trend_atr=atr, trend_efficiency=min(1.0, efficiency), trend_direction=direction,
                trend_range=max(one["high"] for one in window) - min(one["low"] for one in window))


def trend_entry(pair: dict[str, Any], config: Any, at: int) -> tuple[dict[str, float] | None, str]:
    direction, atr = pair.get("trend_direction", 0), pair.get("trend_atr")
    if not direction or not atr:
        pair.pop("setup", None)
        return None, "완료된 1분봉의 상대 추세를 확인합니다"
    setup = pair.get("setup")
    if setup and (setup["sign"] != direction or at - setup["at"] > 300_000):
        pair.pop("setup", None)
        return None, "추세가 바뀌거나 후보 시간이 지나 재평가합니다"
    price = direction * pair["spread"]
    if setup is None:
        pair["setup"] = {"at": at, "last_at": at, "sign": direction, "peak": price, "trough": price,
                         "phase": "trend", "atr": atr, "confirmations": 0,
                         "mean": pair["mean"], "deviation": pair["deviation"]}
        return None, "상대 추세 확인 · 눌림을 기다립니다"
    if at <= setup["last_at"]:
        return None, "새 표본을 기다립니다"
    setup["last_at"] = at
    atr = setup["atr"]
    if setup["phase"] == "trend":
        setup["peak"] = max(setup["peak"], price)
        if setup["peak"] - price < config.pullback_atr * atr:
            return None, "추세 고점·저점에서 눌림을 기다립니다"
        setup.update(phase="pullback", trough=price)
    if setup["peak"] - price > config.max_pullback_atr * atr:
        pair.pop("setup", None)
        return None, "눌림이 너무 깊어 후보를 취소합니다"
    if price < setup["trough"]:
        setup.update(trough=price, confirmations=0)
    resumed = price - setup["trough"]
    setup["confirmations"] = setup["confirmations"] + 1 if resumed >= config.resume_atr * atr else 0
    if setup["confirmations"] < 2:
        return None, "눌림 이후 추세 방향 재진행을 확인합니다"
    return {"direction": direction, "atr": atr, "spread": pair["spread"],
            "stop_spread": direction * (setup["trough"] - .25 * atr),
            "range": pair["trend_range"], "z": pair["z"], "mean": setup["mean"],
            "deviation": setup["deviation"]}, ""


def risk_plan(signal: dict[str, float], config: Any, cost: float, value_per_point: float) -> dict[str, float] | None:
    """Freeze a technical stop with >=1 ATR room, including estimated round-trip costs."""
    distance = max(signal["atr"], signal["direction"] * (signal["spread"] - signal["stop_spread"]))
    risk = distance * value_per_point + cost
    if not math.isfinite(risk) or risk <= 0 or risk > config.stop_loss:
        return None
    return {"risk": risk, "target": max(config.reward_risk * risk, 2 * cost),
            "stop_spread": signal["spread"] - signal["direction"] * distance,
            "atr_usd": signal["atr"] * value_per_point, "entry_cost": cost}


def trend_exit(pair: dict[str, Any], config: Any, cycle: float, spread: float | None) -> str | None:
    risk, target = pair["trade_risk"], pair["trade_target"]
    pair["trade_peak"] = max(pair.get("trade_peak", cycle), cycle)
    if cycle <= -risk:
        return "변동성 손절"
    if cycle >= target:
        return "위험금액 대비 목표 익절"
    if spread is not None and pair["direction"] * (spread - pair["trade_stop_spread"]) <= 0:
        return "눌림 기준 이탈 손절"
    if pair["trade_peak"] >= risk:
        pair["trade_trail"] = max(pair.get("trade_trail") if pair.get("trade_trail") is not None else -risk,
                                  pair["trade_peak"] - config.trail_distance_r * risk)
        if cycle <= pair["trade_trail"]:
            return "이익 추적 청산"
    return None
