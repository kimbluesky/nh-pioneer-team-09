"""Executable historical replay using the same input age and decision rules as the paper engine."""

from collections import Counter

import numpy as np

from market import CLOCK, SIDES, allowed, due, exchange_cash, morning, past_day, state, tick
from policy import checkpoints, exit_state


def capped_score(values):
    x = np.array(values, dtype=float)
    pos = np.sort(x[x > 0])[::-1]
    cap = pos[2] if len(pos) >= 3 else 0.0
    return float(np.minimum(x, cap).sum())


class FixedSide:
    def __init__(self, side):
        self.side = side

    def act(self, i, obs, drifts, friction, legal, ordinal):
        through_close = 3 if self.side < 0 else 4
        return through_close if legal[through_close] else through_close - 2


class HoldToDeadline:
    def value(self, features):
        return 1.0


def series(stock, calendar, first, stop, stale_previous=False):
    hist = [past_day(d, stock[d]) for d in sorted(stock) if d < first][-65:]
    tape = []
    for d in sorted(v for v in calendar if first <= v < stop):
        b = stock.get(d, {})
        tape.append((d, b, morning(hist[:-1] if stale_previous else hist, d)))
        if b:
            hist.append(past_day(d, b))
    return tape


def visible_at(bars, m):
    return {t: b for t, b in bars.items() if t + 5 <= m - 5 and b[4] > 0}


def positive(bars, m):
    return m in bars and bars[m][4] > 0


def replay(tape, entry, ender, *, enforce_freshness=True):
    """The original training-only screen predated the 15-minute execution audit."""
    daily, trades, decisions = [], [], []
    issues = Counter()
    for day, bars, prior in tape:
        net = 0.0
        traded = False
        for i, m in enumerate(CLOCK):
            visible = visible_at(bars, m)
            latest = max(visible, default=-1)
            point = {
                "day": str(day),
                "minute": m,
                "kind": "entry",
                "last_input_start": latest,
                "action": None,
                "hold_value": None,
            }
            if enforce_freshness and latest < m - 15:
                issues["stale_entry"] += 1
                decisions.append(dict(point, status="skipped_stale_input"))
                continue
            x, dr, fr, unit = state(day, i, visible, prior)
            a = int(entry.act(i, x, dr, fr, allowed(day, i), day.toordinal()))
            decisions.append(dict(point, status="decided", action=a))
            if not a:
                continue
            if not positive(bars, m):
                issues["unpriced_entry"] += 1
                # An attempted market order on this bar could have filled. Never invent a second one.
                break
            side = int(SIDES[a - 1])
            opening = bars[m][0]
            fill = opening + side * tick(opening if side > 0 else opening - 1)
            original = due(day, m, a)
            intended = original
            for at in checkpoints(m, original):
                visible = visible_at(bars, at)
                latest = max(visible, default=-1)
                pt = {
                    "day": str(day),
                    "minute": at,
                    "kind": "exit",
                    "last_input_start": latest,
                    "action": None,
                    "hold_value": None,
                }
                if enforce_freshness and latest < at - 15:
                    issues["stale_exit"] += 1
                    decisions.append(dict(pt, status="continued_stale_input"))
                    continue
                value = float(ender.value(exit_state(visible, at, m, fill, side, original, unit)))
                decisions.append(dict(pt, status="decided", action=int(value < 0), hold_value=value))
                if value < 0:
                    intended = at
                    break
            if not positive(bars, intended):
                issues["unpriced_exit"] += 1
                break
            close = bars[intended][0]
            gross, slip, fee, net = exchange_cash(side, opening, close)
            manual_close = close - side * tick(close - 1 if side > 0 else close)
            manual = side * 10 * (manual_close - fill) - 0.000085 * 10 * (manual_close + fill)
            if abs(manual - net) > 1e-6:
                raise ArithmeticError("Signed cash differs from costed fills")
            trades.append(
                dict(
                    day=str(day),
                    decision=m,
                    entry=m,
                    exit=intended,
                    original_exit=original,
                    action=a,
                    side=side,
                    entry_reference=opening,
                    exit_reference=close,
                    entry_fill=fill,
                    exit_fill=manual_close,
                    gross_krw=gross,
                    slippage_krw=slip,
                    fee_krw=fee,
                    net_krw=net,
                )
            )
            traded = True
            break
        daily.append(dict(day=str(day), trades=int(traded), net_krw=net))
    cash = np.array([r["net_krw"] for r in daily])
    equity = np.r_[0.0, cash.cumsum()]
    gross = sum(t["gross_krw"] for t in trades)
    slip = sum(t["slippage_krw"] for t in trades)
    fee = sum(t["fee_krw"] for t in trades)
    stats = dict(
        net_krw=float(cash.sum()),
        capped_krw=capped_score(cash),
        drawdown_krw=float((np.maximum.accumulate(equity) - equity).max()),
        double_cost_krw=float(cash.sum() - slip - fee),
        gross_krw=gross,
        slippage_krw=slip,
        fee_krw=fee,
        days=len(daily),
        trades=len(trades),
        zero_days=len(daily) - len(trades),
        wins=int((cash > 0).sum()),
        longs=sum(t["side"] > 0 for t in trades),
        early_exits=sum(t["exit"] < t["original_exit"] for t in trades),
        execution_diagnostics=dict(issues),
    )
    return stats, daily, trades, decisions
