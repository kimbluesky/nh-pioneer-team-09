"""MNQ features and 12-minute inference from completed one-minute bars."""

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import joblib
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from risk import route, component_key

ROOT = Path(__file__).resolve().parents[1]
KST = timezone(timedelta(hours=9))
ET = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
START, END = 360, 1440
EPOCH = date(1970, 1, 1)
CONFIG = {"instruments": {"MNQ": {"tick": 0.25, "multiplier": 2}}}


@dataclass
class Tape:
    market: str
    days: list
    price: np.ndarray  # day, KST clock minute minus START, OHLC in integer ticks
    volume: np.ndarray
    x: np.ndarray
    atr: np.ndarray  # trailing true range in ticks, known at the decision time
    valid: np.ndarray
    ny: np.ndarray
    london: np.ndarray


def from_rows(market, days, t, values):
    ids = np.array([(d - EPOCH).days for d in days])
    day = (t + 9 * 3600) // 86400
    minute = (t // 60 + 540) % 1440
    minute_ok = (minute >= START) & np.isin(day, ids)
    o, h, l, c, v = values.T
    valid = (
        np.isfinite(values).all(axis=1)
        & (v > 0)
        & (l > 0)
        & (h >= np.maximum(o, c) - 1e-7)
        & (l <= np.minimum(o, c) + 1e-7)
    )
    use = minute_ok & valid
    if np.any(t[use] % 60) or len(np.unique(t[use])) != use.sum():
        raise ValueError("Unaligned or duplicate minute in " + market)
    days_idx = np.searchsorted(ids, day[use])
    shape = (len(days), END - START)
    px = np.zeros((*shape, 4), dtype=np.int32)
    vol = np.zeros(shape, dtype=np.float32)
    tick = CONFIG["instruments"][market]["tick"]
    px[days_idx, minute[use] - START] = np.rint(values[use, :4] / tick).astype(np.int32)
    vol[days_idx, minute[use] - START] = v[use]
    return make_tape(market, days, px, vol)


def lag(a, count=1):
    out = np.zeros_like(a)
    out[:, count:] = a[:, :-count]
    return out


def preceding_sum(a, width):
    """At column i, sum the previous width rows; row i has never been observed."""
    cs = np.empty((len(a), a.shape[1] + 1), dtype=np.float64)
    cs[:, 0] = 0
    np.cumsum(a, axis=1, dtype=np.float64, out=cs[:, 1:])
    out = cs[:, :-1].copy()
    out[:, width:] -= cs[:, : -width - 1]
    return out


def past_extreme(a, width, high):
    fill = 0 if high else np.iinfo(np.int32).max
    arr = np.where(a > 0, a, fill)
    windows = sliding_window_view(np.pad(arr, ((0, 0), (width, 0)), constant_values=fill), width, axis=1)
    return (windows.max if high else windows.min)(axis=-1)[:, :-1]


def local_minutes(days, zone):
    offsets = [
        int(datetime.combine(d, time(12), KST).astimezone(zone).utcoffset().total_seconds() / 60) - 540
        for d in days
    ]
    return (np.arange(START, END)[None, :] + np.array(offsets)[:, None]) % 1440


def make_tape(market, days, price, volume):
    o, h, l, c = np.moveaxis(price, 2, 0)
    valid_px = c > 0
    last = lag(c)
    prev = np.where(last > 0, last, o)
    tr = np.where(valid_px, np.maximum(h - l, np.maximum(h - prev, prev - l)), 0)
    n14 = preceding_sum(valid_px, 14)
    atr = np.maximum(1.0, preceding_sum(tr, 14) / np.maximum(n14, 1))
    n60 = preceding_sum(valid_px, 60)
    valid = (last > 0) & (n14 >= 10) & (n60 >= 48) & (np.arange(START, END) >= 420)
    ny, london = local_minutes(days, ET), local_minutes(days, LONDON)
    columns = []

    def push(a):
        columns.append(np.clip(a, -12.0, 12.0).astype(np.float32))

    for width in (1, 3, 8, 15, 30, 59):
        anchor = lag(c, width + 1)
        push(np.where(anchor > 0, (last - anchor) / atr / np.sqrt(width), 0))
    for width in (8, 30, 60):
        top, low = past_extreme(h, width, True), past_extreme(l, width, False)
        push(2 * (last - low) / np.maximum(top - low, 1) - 1)
        weights = preceding_sum(volume, width)
        vwap = preceding_sum(
            volume.astype(np.float64) * (h.astype(np.float64) + l + c) / 3, width
        ) / np.maximum(weights, 1)
        push((last - vwap) / atr)
    push((last - lag(o)) / atr)
    push((lag(h) - last) / atr)
    push((last - lag(l)) / atr)
    push(np.log1p(lag(volume)) - np.log1p(preceding_sum(volume, 30) / 30))
    push(np.log(atr / np.maximum(1.0, preceding_sum(tr, 60) / np.maximum(n60, 1))))
    tick_value = CONFIG["instruments"][market]["tick"] * CONFIG["instruments"][market]["multiplier"]
    push(np.log1p(atr * tick_value))
    push(np.log1p(10000 * atr / np.maximum(last, 1)))
    for local in (ny, london):
        phase = 2 * np.pi * local / 1440
        for harmonic in (1, 2, 4):
            push(np.sin(phase * harmonic))
            push(np.cos(phase * harmonic))
    for local, marks in ((ny, (510, 570, 600)), (london, (480, 630, 900, 960))):
        for mark in marks:
            delta = (local - mark + 720) % 1440 - 720
            push(np.exp(-0.5 * (delta / 18) ** 2))
    return Tape(
        market, days, price, volume, np.stack(columns, axis=-1), atr.astype(np.float32), valid, ny, london
    )


def mirror(x):
    # Returns, range location, VWAP displacement and candle body change sign.
    # Upper/lower wicks exchange places. Activity and clock features keep their meaning.
    result = x.copy()
    result[:, :13] *= -1
    result[:, [13, 14]] = x[:, [14, 13]]
    return result


def forecast(model, x, ny, london, name):
    seasonal = (model["ny"][ny] + model["london"][london]) / 2
    if name == "ridge_clock":
        return model["ridge"].predict(x) + seasonal
    flip = mirror(x)
    if name == "barrier":
        # Expected signed two-ATR barrier in a first-touch conditional model.
        return 2 * (
            model["barrier"].predict(x, num_threads=2) - model["barrier"].predict(flip, num_threads=2)
        )
    tree = model["huber"]
    return (tree.predict(x, num_threads=2) - tree.predict(flip, num_threads=2)) * 0.5 + seasonal


def targets(tape, rows, slots, horizon):
    px = tape.price
    anchor = px[rows, slots, 0]
    atr = tape.atr[rows, slots]
    opens = px[rows[:, None], slots[:, None] + np.array([horizon - 1, horizon, horizon + 1]), 0]
    y = np.clip((np.median(opens, axis=1) - anchor) / atr, -6, 6).astype(np.float32)
    width = np.maximum(2 * atr, 10)
    offsets = slots[:, None] + np.arange(horizon)
    up = px[rows[:, None], offsets, 1] >= (anchor + width)[:, None]
    down = px[rows[:, None], offsets, 2] <= (anchor - width)[:, None]
    u = np.where(up.any(axis=1), up.argmax(axis=1), horizon)
    d = np.where(down.any(axis=1), down.argmax(axis=1), horizon)
    # Do not infer the ordering of two barriers inside one minute.
    barrier = np.sign(d - u).astype(np.int8)
    return y, barrier


def reproduce(hhmm):
    """Rescore a decision whose full lookback is present in the session journal."""
    with (ROOT / "results" / "session.jsonl").open(encoding="utf-8") as journal:
        signals = [event for line in journal if (event := json.loads(line))["event"] == "forecast"]
    target = next((row for row in signals if row["kst"][11:16] == hhmm), None)
    if target is None:
        raise ValueError("No recorded decision at that time")
    stamp = target["bar"]
    price_history = {
        r["closed_bar_utc"]: r["closed_ohlcv"]
        for r in signals
        if target["input_first_utc"] <= r["closed_bar_utc"] < stamp
    }
    times = sorted(price_history)
    if len(times) != target["history_bars"] or target["pre_pause_bars"]:
        raise ValueError("Select a time with a full intraday lookback, for example 19:05")
    clock = datetime.fromtimestamp(stamp, KST)
    tape = from_rows("MNQ", [clock.date()], np.array(times), np.array([price_history[t] for t in times]))
    slot = clock.hour * 60 + clock.minute - START
    models = joblib.load(ROOT / "model" / "forecasts.joblib")[12]
    rule = json.loads((ROOT / "strategy.json").read_text(encoding="utf-8"))["rule"]
    idx = (0, slice(slot, slot + 1))
    scores = {
        component_key(leg): float(
            forecast(models, tape.x[idx], tape.ny[idx], tape.london[idx], leg["model"])[0]
        )
        for leg in rule["components"]
    }
    for key, value in scores.items():
        if not np.isclose(value, target["component_scores"][key], atol=1e-6, rtol=1e-5):
            raise ValueError(f"Archived closing bars differ from the decision-time data for {key}")
    atr = round(float(tape.atr[0, slot]) * 0.25, 6)
    owner = route(rule, scores, atr)
    print(clock.isoformat(), "ATR", atr, "raw scores", scores)
    if owner:
        key = component_key(owner)
        print("Selected", key, "score / threshold", scores[key] / owner["cut"])
    else:
        print("No entry signal")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--time", default="19:05", metavar="HH:MM", help="KST on 2026-11-27")
    reproduce(parser.parse_args().time)
