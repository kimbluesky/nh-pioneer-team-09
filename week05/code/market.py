"""SK hynix five-minute market, causal features and cash value of a futures trade."""

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

MULT = 10
FEE = 0.000085
ANNOUNCED_EXPIRIES = {(2025, 10): date(2025, 10, 2)}


def tick(p):
    return (1, 5, 10, 50, 100, 500, 1000)[bisect_right((2000, 5000, 20000, 50000, 200000, 500000), p)]


def cost(p, delta):
    if not delta:
        return 0.0
    step = tick(p if delta > 0 else p - 1)
    fill = p + (step if delta > 0 else -step)
    return abs(delta) * MULT * (step + FEE * fill)


def is_expiry(day):
    if day is None:
        return False
    first = day.replace(day=1)
    regular = first + timedelta(days=(3 - first.weekday()) % 7 + 7)
    expiry = ANNOUNCED_EXPIRIES.get((day.year, day.month), regular)
    return day == expiry


CLOCK = tuple(range(570, 871, 30))
SIDES = np.array([-1, 1, -1, 1])


def close_at(day):
    return 900 if is_expiry(day) else 925


def due(day, minute, action):
    return min(minute + 45, close_at(day)) if action in (1, 2) else close_at(day)


def prints(bars):
    return [(m, b) for m, b in sorted(bars.items()) if b[4] > 0]


def outcomes(day, bars):
    rows = prints(bars)
    times = np.array([m for m, _ in rows], int)
    prices = np.array([b[0] for _, b in rows], float)
    data = np.full((len(CLOCK), 2, 4), np.nan)
    for s, start in enumerate(CLOCK):
        i = times.searchsorted(start)
        if i == len(times) or times[i] >= close_at(day) or (times[i] != start and s != len(CLOCK) - 1):
            continue
        for h, a in enumerate((1, 3)):
            j = times.searchsorted(due(day, int(times[i]), a))
            if j < len(times) and times[j] > times[i]:
                data[s, h] = [times[i], times[j], prices[i], prices[j]]
    return data


def past_day(day, bars):
    p = prints(bars)
    hi, lo = max(b[1] for _, b in p), min(b[2] for _, b in p)
    op, cl = p[0][1][0], p[-1][1][3]
    width = max(hi - lo, tick(op) * 2)
    payoff = outcomes(day, bars)
    signed = (payoff[..., 3] - payoff[..., 2]) / width
    return dict(
        day=day,
        close=cl,
        range_bps=10000 * width / op,
        move=(cl - op) / width,
        qty=np.median([b[4] for _, b in p]),
        signed=signed,
    )


def morning(history, day):
    recent = history[-64:]
    if not recent:
        return dict(risk_bps=200.0, last=0.0, move=0.0, qty=1.0, drifts=np.zeros((2, len(CLOCK), 2)))
    signed = np.stack([d["signed"] for d in recent])
    drifts = []
    for half in (8, 32):
        weight = np.exp2((np.arange(len(recent)) - len(recent) + 1) / half)[:, None, None]
        den = (np.isfinite(signed) * weight).sum(axis=0) + 2.0
        drifts.append((weight * np.nan_to_num(signed).clip(-2, 2)).sum(axis=0) / den)
    past = recent[-20:]
    return dict(
        risk_bps=max(5.0, np.median([d["range_bps"] for d in past])),
        last=0.0 if is_expiry(day) or is_expiry(past[-1]["day"]) else past[-1]["close"],
        move=np.mean([d["move"] for d in past[-5:]]),
        qty=max(1.0, np.median([d["qty"] for d in past])),
        drifts=np.stack(drifts),
    )


def state(day, s, bars, prior):
    at = CLOCK[s]
    visible = [(t, b) for t, b in prints(bars) if t + 5 <= at - 5]
    if visible:
        rows = np.array([b for _, b in visible])
        op, ref = rows[0, 0], rows[-1, 3]
        near = [b for t, b in visible if t >= at - 25]
        m15 = (ref - near[0][0]) / ref if near else 0.0
        hi, lo = rows[:, 1].max(), rows[:, 2].min()
        loc = (2 * ref - hi - lo) / max(hi - lo, tick(ref))
        volume = np.log1p(rows[-3:, 4].mean()) - np.log1p(prior["qty"])
        age = min(at - 10 - visible[-1][0], 90.0) / 30.0
    else:
        op = ref = max(1.0, prior["last"])
        m15 = loc = volume = 0.0
        hi = lo = ref
        age = 3.0
    scale = max(2 * MULT * tick(ref), ref * MULT * prior["risk_bps"] / 10000)
    factor = MULT / scale
    drift = prior["drifts"][:, s]
    x = np.array(
        [
            prior["move"],
            (ref - op) * factor,
            (op - prior["last"]) * factor if prior["last"] else 0.0,
            m15 * ref * factor,
            (hi - lo) * factor,
            loc,
            volume,
            age,
            s / (len(CLOCK) - 1),
            int(is_expiry(day)),
        ]
    ).clip(-4, 4)
    friction = np.array([cost(ref, side) + cost(ref, -side) for side in SIDES]) / scale
    return x, drift, friction, scale


def allowed(day, s):
    distinct = CLOCK[s] + 45 < close_at(day)
    return np.array([s < len(CLOCK) - 1, True, True, distinct, distinct])


def exchange_cash(side, op, cl):
    entry = op + side * tick(op if side > 0 else op - 1)
    exit_price = cl - side * tick(cl - 1 if side > 0 else cl)
    gross = side * MULT * (cl - op)
    fee = FEE * MULT * (entry + exit_price)
    slip = MULT * (abs(entry - op) + abs(cl - exit_price))
    return float(gross), float(slip), float(fee), float(side * MULT * (exit_price - entry) - fee)


@dataclass
class Tape:
    days: list
    calendar: list
    x: np.ndarray
    drift: np.ndarray
    friction: np.ndarray
    scale: np.ndarray
    quotes: np.ndarray
    legal: np.ndarray
    gross_move: np.ndarray
    reward: np.ndarray


def build(stock, calendar, first, stop):
    requested = sorted(d for d in calendar if first <= d < stop)
    days = [d for d in requested if stock.get(d)]
    history = []
    records = []
    for d in sorted(stock):
        if d >= stop:
            break
        if d >= first:
            prior = morning(history, d)
            features = [state(d, s, stock[d], prior) for s in range(len(CLOCK))]
            x, drift, friction, scale = map(np.stack, zip(*features, strict=True))
            quotes = outcomes(d, stock[d])
            gross = (quotes[..., 3] - quotes[..., 2]) * MULT / scale[:, None]
            rew = np.full((len(CLOCK), 4), np.nan)
            for s in range(len(CLOCK)):
                for a, side in enumerate(SIDES):
                    op, cl = quotes[s, a // 2, 2:]
                    if np.isfinite(op):
                        rew[s, a] = (side * MULT * (cl - op) - cost(op, side) - cost(cl, -side)) / scale[
                            s
                        ]
            records.append(
                (
                    x,
                    drift,
                    friction,
                    scale,
                    quotes,
                    np.stack([allowed(d, s) for s in range(len(CLOCK))]),
                    gross,
                    rew,
                )
            )
        history.append(past_day(d, stock[d]))
    if not records or len(records) != len(days):
        raise ValueError("Inconsistent target and index calendars")
    return Tape(days, requested, *(np.stack(v) for v in zip(*records, strict=True)))
