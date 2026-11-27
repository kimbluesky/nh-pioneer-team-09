"""Same OHLC bars and frozen forecasts, with a selectable risk policy."""

import json
from bisect import bisect_left
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
import numpy as np
from risk import (
    FEE,
    MAX_DAY,
    MULT,
    TICK,
    add_price_ok,
    add_stop,
    ceil_price,
    component_key,
    entry_side,
    fee_floor,
    forecast_action,
    pullback_level,
    room,
    stop_distance,
    trail_after_partial,
)

ROOT = Path(__file__).resolve().parents[1]
KST = timezone(timedelta(hours=9))


def run(sample, rule, slip, arm="scaled", save=False):
    days, rows = sample["days"], sample["rows"]
    legs = rule.get("components", [rule])
    columns = [sample["scores"][leg["horizon"], leg["model"]] for leg in legs]
    if len(legs) > 1:
        strength = np.stack(
            [
                np.where(
                    (np.abs(v) > leg["cut"]) & (np.abs(v) * sample["atr"] * MULT >= fee_floor(leg)),
                    np.abs(v) / leg["cut"],
                    0.0,
                )
                for leg, v in zip(legs, columns)
            ]
        )
        leading = strength.argmax(axis=0)
        allowing = sample["demo"] & (strength.max(axis=0) > 0)
    else:
        leading = np.zeros(len(rows), dtype=np.int8)
        allowing = sample["demo"] & (np.abs(columns[0]) > legs[0]["cut"])
        allowing &= np.abs(columns[0]) * sample["atr"] * MULT >= fee_floor(legs[0])
    entries = np.flatnonzero(allowing).tolist()
    net = np.zeros(len(days))
    trades = np.zeros(len(days), dtype=int)
    adds = np.zeros_like(trades)
    partials = np.zeros_like(trades)
    locked = set()
    exits, directions = Counter(), Counter()
    finished, ledger = [], []
    total = peak = drawdown = 0.0
    drift = slip * TICK
    pos = None
    attempts = untriggered = 0

    def register(phase, price, reason, quantity, gain=None):
        if save:
            ledger.append(
                dict(
                    kst=datetime.fromtimestamp(t, KST).isoformat(timespec="minutes"),
                    phase=phase,
                    side=pos["side"] if phase in ("first", "add") else -pos["side"],
                    qty=quantity,
                    price=price,
                    reason=reason,
                    idea_start=pos["start"],
                    net=gain,
                    remaining=len(pos["lots"]),
                    model=component_key(legs[pos["component"]]),
                    stop=None if arm != "scaled" else pos["stop"],
                )
            )

    def close_at(mark, reason):
        nonlocal total, pos
        partial = reason in ("scale_price", "model_reduce") and len(pos["lots"]) == 2
        quantity = 1 if partial else len(pos["lots"])
        price = mark - pos["side"] * drift
        gain = round(sum((price - p) * pos["side"] * MULT - 2 * FEE for p in pos["lots"][:quantity]), 2)
        del pos["lots"][:quantity]
        pos["gain"] += gain
        net[di] += gain
        total += gain
        exits[reason] += 1
        register("partial" if partial else "exit", price, reason, quantity, gain)
        if arm == "scaled" and net[di] <= -MAX_DAY:
            locked.add(di)
        if partial:
            partials[di] += 1
            pos["target"] = pos["trigger"] = None
        else:
            finished.append(pos["gain"])
            trades[di] += 1
            pos = None

    def check_price(mark):
        side = pos["side"]
        if arm == "scaled":
            marked = day_net() + sum((mark - e) * side * MULT - 2 * FEE for e in pos["lots"])
            if marked <= -MAX_DAY:
                if di >= 0:
                    locked.add(di)
                pos["trigger"] = "daily_stop"
            elif side * (mark - pos["stop"]) <= 0 and pos["trigger"] in (
                None,
                "model_reduce",
                "scale_price",
            ):
                pos["trigger"] = "stop"
        if pos["target"] is not None and side * (mark - pos["target"]) >= 0 and not pos["trigger"]:
            pos["trigger"] = "scale_price"

    def day_net():
        return float(net[di]) if di >= 0 else 0.0

    i = 0
    while i < len(rows):
        if pos is None:
            next_ix = bisect_left(entries, i)
            if next_ix == len(entries):
                break
            i = entries[next_ix]
        t, di, demo, o, h, l, c, last, atr = rows[i]
        owner = pos["component"] if pos else int(leading[i])
        leg = legs[owner]
        score = columns[owner][i]
        valid = atr > 0
        i += 1
        if pos is None:
            if arm == "scaled" and di in locked:
                continue
            distance = stop_distance(atr, day_net() if arm == "scaled" else 0.0, rule)
            if not distance:
                continue
            side = entry_side(score, atr, leg)
            level = pullback_level(leg, last, atr, side)
            attempts += 1
            if level is not None and ((l > level - drift) if side == 1 else (h < level + drift)):
                untriggered += 1
                continue
            # A gap through the level starts at the observed open. Inside the candle
            # use the trigger as arrival price and pay market slippage on every leg.
            arrival = o if level is None or side * (o - level) <= 0 else level
            price = arrival + side * drift
            lots = [price] * (2 if arm == "forecast_only" else 1)
            pos = dict(
                start=t,
                side=side,
                lots=lots,
                first=price,
                stop=price - side * distance,
                r=distance,
                component=owner,
                maxq=2,
                budget=room(day_net()) if arm == "scaled" else 150.0,
                added=False,
                target=None,
                partial_distance=ceil_price(rule["partial_r"] * distance),
                trigger=None,
                gain=0.0,
            )
            directions["long" if side == 1 else "short"] += 1
            register("first", price, "minute_forecast", len(lots))
        else:
            side = pos["side"]
            check_price(o)
            if arm != "forecast_only" and valid:
                pos["stop"] = trail_after_partial(rule, pos, last)
                check_price(o)
            if valid and pos["trigger"] in (None, "scale_price", "model_reduce"):
                if arm == "forecast_only":
                    reason = "opposite_signal" if side * score < -leg["cut"] else None
                else:
                    hypothetical = sum((o - side * TICK - p) * side * MULT - 2 * FEE for p in pos["lots"])
                    reason = forecast_action(leg, pos, score, hypothetical)
                if reason and (not pos["trigger"] or reason == "opposite_signal"):
                    pos["trigger"] = reason
            if pos["trigger"] and demo:
                close_at(o, pos["trigger"])
            elif (
                demo
                and valid
                and arm != "forecast_only"
                and not pos["trigger"]
                and (arm != "scaled" or di not in locked)
                and side * score >= 0.2 * leg["cut"]
                and add_price_ok(rule, pos, o + side * drift, day_net() if arm == "scaled" else 0.0)
            ):
                price = o + side * drift
                pos["lots"].append(price)
                pos.update(
                    added=True,
                    stop=add_stop(pos, day_net() if arm == "scaled" else 0.0),
                    target=pos["first"] + side * pos["partial_distance"],
                )
                adds[di] += 1
                register("add", price, "favorable_confirmation", 1)
        if pos:
            side = pos["side"]
            if arm == "scaled":
                q = len(pos["lots"])
                daily_px = sum(pos["lots"]) / q - side * (MAX_DAY + day_net() - 2 * FEE * q) / (q * MULT)
                stop = max(daily_px, pos["stop"]) if side == 1 else min(daily_px, pos["stop"])
                stop = np.ceil(stop / TICK) * TICK if side == 1 else np.floor(stop / TICK) * TICK
                stop_hit = l <= stop if side == 1 else h >= stop
                if stop_hit:
                    price = min(o, stop) if side == 1 else max(o, stop)
                    check_price(price)
                    if not demo:
                        check_price(l if side == 1 else h)
                    if demo and pos["trigger"]:
                        close_at(price, pos["trigger"])
            if (
                pos
                and pos["target"] is not None
                and (h >= pos["target"] if side == 1 else l <= pos["target"])
            ):
                target = pos["target"]
                if pos["trigger"] is None:
                    pos["trigger"] = "scale_price"
                if demo and pos["trigger"] == "scale_price":
                    close_at(target, pos["trigger"])
        mark = total
        if pos:
            mark += sum((c - pos["side"] * drift - e) * pos["side"] * MULT - 2 * FEE for e in pos["lots"])
        peak = max(peak, mark)
        drawdown = max(drawdown, peak - mark)
    unrealized = 0.0
    if pos:
        last = rows[-1][6]
        unrealized = sum(
            (last - pos["side"] * drift - e) * pos["side"] * MULT - 2 * FEE for e in pos["lots"]
        )
    n = len(days)
    numbers = np.array(finished)
    result = dict(
        days=n,
        completed=int(trades.sum()),
        entry_ideas=sum(directions.values()),
        directions=dict(directions),
        flat_entry_signals=attempts,
        untriggered_pullbacks=untriggered,
        additions=int(adds.sum()),
        partials=int(partials.sum()),
        addition_days=int((adds > 0).sum()),
        partial_days=int((partials > 0).sum()),
        active_days=int((trades > 0).sum()),
        multiple_trade_days=int((trades >= 2).sum()),
        median_daily_trades=float(np.median(trades)),
        median_daily_realized=float(np.median(net)),
        realized=float(total),
        open_quantity=len(pos["lots"]) if pos else 0,
        terminal_net_of_exit_cost=float(unrealized),
        marked_net=float(total + unrealized),
        worst_idea=float(numbers.min()) if len(numbers) else None,
        wins=int((numbers > 0).sum()),
        max_close_drawdown=round(drawdown, 2),
        reasons=dict(exits),
        days_locked=len(locked),
        daily=[
            dict(
                day=str(d),
                net=float(net[k]),
                completed=int(trades[k]),
                adds=int(adds[k]),
                partials=int(partials[k]),
            )
            for k, d in enumerate(days)
        ],
    )
    return result, ledger


def load_sample():
    with np.load(ROOT / "results" / "replay_51d.npz", allow_pickle=False) as data:
        rows = list(
            zip(
                data["timestamp"].tolist(),
                data["day_index"].tolist(),
                data["demo"].tolist(),
                *data["prices"].T.tolist(),
                data["atr"].tolist(),
            )
        )
        return dict(
            days=data["days"].tolist(),
            rows=rows,
            demo=data["demo"],
            atr=data["atr"],
            scores={(12, key): data[key] for key in ("huber", "ridge_clock", "barrier")},
        )


if __name__ == "__main__":
    sample = load_sample()
    rule = json.loads((ROOT / "strategy.json").read_text(encoding="utf-8"))["rule"]
    metrics = (
        "completed",
        "additions",
        "partials",
        "open_quantity",
        "marked_net",
        "max_close_drawdown",
        "worst_idea",
    )
    print("arm / slip_ticks / " + " / ".join(metrics))
    for mode, ticks in (("forecast_only", 1), ("staged_no_hard_stop", 1), ("scaled", 1), ("scaled", 2)):
        result, _ = run(sample, rule, ticks, arm=mode)
        print(mode, ticks, *(result[k] for k in metrics), sep=" / ")

    print("\nfirst target (R) / additions / price-target exits / all partial exits / marked net / max drawdown")
    for target_r in (rule["partial_r"], 1.25):
        result, _ = run(sample, dict(rule, partial_r=target_r), 1)
        print(
            target_r,
            result["additions"],
            result["reasons"].get("scale_price", 0),
            result["partials"],
            result["marked_net"],
            result["max_close_drawdown"],
            sep=" / ",
        )
