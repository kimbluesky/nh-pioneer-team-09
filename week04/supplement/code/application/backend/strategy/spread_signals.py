"""Price-only research filters; none of these estimates promise a future profit."""
from __future__ import annotations

import math
import statistics
from typing import Any


def return_correlation(prices: list[tuple[float, float]]) -> float | None:
    if len(prices) < 20 or any(not math.isfinite(p) or p <= 0 for row in prices for p in row):
        return None
    changes = [[right[leg] / left[leg] - 1 for left, right in zip(prices, prices[1:])]
               for leg in (0, 1)]
    if any(statistics.pstdev(row) <= 1e-12 for row in changes):
        return None
    value = statistics.correlation(*changes)
    return max(-1.0, min(1.0, value)) if math.isfinite(value) else None


def book_snapshot(one: Any, at: int, max_age: int) -> dict[str, float] | None:
    quote = one.quote_now
    names = ("bid1", "ask1", "bid1Qty", "ask1Qty", "quoteAt")
    if any(not isinstance(quote.get(key), (int, float)) or not math.isfinite(quote[key]) for key in names):
        return None
    if not 0 <= at - quote["quoteAt"] <= max_age * 1000:
        return None
    if not 0 < quote["bid1"] <= quote["ask1"] or min(quote["bid1Qty"], quote["ask1Qty"]) <= 0:
        return None
    return quote


def reversal(pair: dict[str, Any], config: Any, at: int) -> tuple[dict[str, float] | None, str]:
    """Confirm a retreat using the SAME mean/std as the initial excursion.

    Otherwise a moving average catching up to an unchanged price can masquerade
    as a reversal. A setup expires after two minutes and is never persisted as
    an instruction that can automatically resume after a restart.
    """
    setup = pair.get("setup")
    if setup and at - setup["at"] > 120_000:
        pair.pop("setup", None)
        setup = None
    if setup is None:
        if not config.entry_z <= abs(pair["z"]) <= config.max_entry_z:
            return None, "가격차가 진입 범위에 들어오기를 기다립니다"
        setup = {"at": at, "mean": pair["mean"], "deviation": pair["deviation"],
                 "sign": 1 if pair["z"] > 0 else -1, "peak": abs(pair["z"])}
        pair["setup"] = setup
        return None, "간격이 벌어졌습니다. 실제로 좁아지기 시작하는지 기다립니다"
    z = (pair["spread"] - setup["mean"]) / setup["deviation"]
    distance = z * setup["sign"]
    floor = config.min_entry_z or config.entry_z
    if distance < floor or distance > config.max_entry_z:
        pair.pop("setup", None)
        return None, "진입 범위를 벗어나 이번 신호를 취소했습니다"
    setup["peak"] = max(setup["peak"], distance)
    if setup["peak"] - distance + 1e-9 < config.pullback_z:
        return None, "가격차가 좁아지는 움직임을 기다립니다"
    return {"z": z, "mean": setup["mean"], "deviation": setup["deviation"]}, ""


def entry_cost(engines: list[Any], config: Any, at: int) -> tuple[float | None, str]:
    """Round trip estimate: both bid/ask widths, fees and extra slippage.

    Today's top-of-book is an estimate of exit costs, never a fill guarantee.
    Quantities must fit on BOTH sides of the displayed best prices.
    """
    quotes = [book_snapshot(one, at, config.quote_age_seconds) for one in engines]
    if any(quote is None for quote in quotes):
        return None, "최신 양쪽 매수·매도 호가를 기다립니다"
    if abs(quotes[0]["quoteAt"] - quotes[1]["quoteAt"]) > config.quote_skew_seconds * 1000:
        return None, "두 레그의 호가 시각 차이가 큽니다"
    cost = 0.0
    for one, quote, leg in zip(engines, quotes, (config.leg_a, config.leg_b), strict=True):
        width = quote["ask1"] - quote["bid1"]
        if config.max_book_spread_ticks and width > one.tick * config.max_book_spread_ticks + 1e-9:
            return None, f"{leg.code}: 매수·매도 호가 간격이 너무 큽니다"
        if min(quote["bid1Qty"], quote["ask1Qty"]) < leg.quantity:
            return None, f"{leg.code}: 최우선 호가 잔량이 주문 수량보다 적습니다"
        cost += leg.quantity * (one.run.multiplier * (width + 2 * config.slippage_ticks * one.tick)
                                + 2 * config.fee_per_contract)
    return cost, ""


def breakout(pair: dict[str, Any], config: Any, at: int) -> tuple[dict[str, float] | None, str]:
    """Confirm a range breakout against a frozen, strictly prior reference.

    A second fresh sample must stay beyond the boundary AND advance farther.
    Recent range is an activity filter, not a forecast of attainable profit.
    """
    setup = pair.get("setup")
    if setup and at - setup["at"] > 60_000:
        pair.pop("setup", None)
        setup = None
    if setup is None:
        high, low = pair.get("range_high"), pair.get("range_low")
        if high is None or low is None or not config.entry_z <= abs(pair["z"]) <= config.max_entry_z:
            return None, "최근 가격차 범위를 벗어나는 움직임을 기다립니다"
        direction = 1 if pair["spread"] > high else -1 if pair["spread"] < low else 0
        if not direction:
            return None, "최근 가격차의 고점·저점 돌파를 기다립니다"
        pair["setup"] = {"at": at, "last_at": at, "mean": pair["mean"], "deviation": pair["deviation"],
                         "sign": direction, "boundary": high if direction > 0 else low,
                         "trigger": pair["spread"], "range": high - low, "confirmations": 1}
        return None, "가격차 범위를 돌파했습니다. 다음 시세의 지속 움직임을 확인합니다"
    direction = setup["sign"]
    fixed_z = (pair["spread"] - setup["mean"]) / setup["deviation"]
    if direction * (pair["spread"] - setup["boundary"]) <= 0 or direction * fixed_z > config.max_entry_z:
        pair.pop("setup", None)
        return None, "돌파가 유지되지 않거나 지나치게 벌어져 이번 신호를 취소했습니다"
    if at > setup["last_at"]:
        setup["confirmations"] += 1
        setup["last_at"] = at
    follow = direction * (pair["spread"] - setup["trigger"]) / setup["deviation"]
    if setup["confirmations"] < config.breakout_confirm_samples or follow < config.breakout_follow_z:
        return None, "돌파 방향으로 추가 움직임이 나오는지 확인합니다"
    return {"z": fixed_z, "mean": setup["mean"], "deviation": setup["deviation"],
            "range": setup["range"], "boundary": setup["boundary"]}, ""
