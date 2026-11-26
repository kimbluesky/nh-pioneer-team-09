"""Reconstruct the validation grids and the original training-period trade examples."""

import hashlib
import json
from datetime import date, timedelta
from itertools import product
from math import prod

import numpy as np

from backtest import HoldToDeadline, replay, series
from market import build
from policy import learn, paths, train
from reproduce import (
    FIRST,
    RESULTS,
    TEST,
    THREE,
    VALID,
    check,
    check_fitted_weights,
    fit_through,
    load_market,
    load_models,
    read_json,
)


def check_grid_row(summary, expected, name):
    for key in ("net_krw", "capped_krw", "zero_days", "early_exits"):
        if key in expected:
            np.testing.assert_allclose(
                summary[key], expected[key], rtol=0, atol=0.005, err_msg=f"{name} validation {key}"
            )


def reproduce_grid(stock, calendar, evidence, frozen):
    # Remove the entire test interval before building either training or validation inputs.
    stock = {d: bars for d, bars in stock.items() if d < TEST}
    calendar = [d for d in calendar if d < TEST]
    tape = build(stock, calendar, FIRST, VALID)
    validation = series(stock, calendar, VALID, TEST)
    check(len(validation) == 39 and max(tape.days) < VALID, "Validation split")
    ids = {
        months: np.array([i for i, d in enumerate(tape.days) if d >= start])
        for months, start in ((33, THREE), (45, FIRST))
    }
    entry_rows = {}
    entry_models = {}
    for months, half, residual in product((33, 45), (8, 32), (False, True)):
        name = f"{3 if months == 33 else 4}y_{half}_{'residual' if residual else 'drift'}"
        entry, _ = train(tape, ids[months], half, residual)
        summary = replay(validation, entry, HoldToDeadline())[0]
        row = evidence["entry"]["rows"][name]
        check(
            (months, half, residual) == (row["months"], row["drift_half_life"], row["residual"]),
            f"Entry settings {name}",
        )
        check_grid_row(summary, row, name)
        entry_rows[name] = summary
        entry_models[name] = entry

    def entry_rank(name):
        s = entry_rows[name]
        return (
            s["zero_days"],
            -(s["net_krw"] > 0),
            -s["capped_krw"],
            -s["net_krw"],
            -evidence["entry"]["rows"][name]["months"],
            name,
        )

    chosen_entry = min(entry_rows, key=entry_rank)
    check(chosen_entry == evidence["entry"]["selected"], "Entry selection differs")
    samples = paths(tape, stock)
    base = entry_rows[chosen_entry]
    check_grid_row(base, evidence["exit"]["rows"]["base"], "base")
    exit_rows = {}
    exit_models = {}
    for months, margin in product((33, 45), (0, 1)):
        name = f"{3 if months == 33 else 4}y_se{margin}"
        model = learn(samples, ids[months], margin)
        summary = replay(validation, entry_models[chosen_entry], model)[0]
        row = evidence["exit"]["rows"][name]
        check((months, margin) == (row["months"], row["margin"]), f"Exit settings {name}")
        check_grid_row(summary, row, name)
        exit_rows[name] = summary
        exit_models[name] = model
    eligible = [
        k
        for k, s in exit_rows.items()
        if s["zero_days"] == 0
        and s["net_krw"] > base["net_krw"]
        and s["capped_krw"] > base["capped_krw"]
    ]
    chosen_exit = min(
        eligible,
        key=lambda k: (-exit_rows[k]["capped_krw"], -exit_rows[k]["net_krw"], k),
        default="base",
    )
    check(chosen_exit == evidence["exit"]["selected"], "Exit selection differs")
    check_fitted_weights((entry_models[chosen_entry], exit_models[chosen_exit]), frozen)
    print(
        f"39 validation sessions: 8 entry fits, 4 exit fits and the scheduled-exit control matched. "
        f"Selected {chosen_entry}, {chosen_exit}.",
        flush=True,
    )


def check_final_screen(evidence):
    screen = evidence["inner_screen"]
    for grid, count in (
        ("entry_grid", "entry_count"),
        ("mc_grid", "mc_count"),
        ("trace_grid", "trace_count"),
    ):
        check(prod(len(axis) for axis in screen[grid].values()) == screen[count], grid)
    final = evidence["final_validation"]
    rows = final["rows"]
    check(len(rows) == screen["finalist_count"] == 16, "Finalist count")
    base = rows[final["incumbent"]]
    eligible = [
        k
        for k, s in rows.items()
        if s["zero_days"] == 0
        and s["net_krw"] > base["net_krw"]
        and s["capped_krw"] > base["capped_krw"]
        and s["double_cost_krw"] > 0
        and s["drawdown_krw"] <= base["drawdown_krw"]
    ]
    selected = min(
        eligible,
        key=lambda k: (
            -(rows[k]["capped_krw"] - 0.25 * rows[k]["drawdown_krw"]),
            -rows[k]["net_krw"],
            k,
        ),
        default=final["incumbent"],
    )
    check(selected == final["selected"], "Final validation decision differs")
    print(
        f"16 archived validation results: {len(eligible)} challengers passed; selected {selected}."
    )


def training_examples(stock, calendar, evidence):
    """Each panel uses the first trade in an archived 56-session training-only forward block."""
    examples = []
    for fold in evidence["inner_screen"]["folds"]:
        start = date.fromisoformat(fold["from"])
        stop = date.fromisoformat(fold["to"]) + timedelta(days=1)
        check(stop <= VALID, "Inner screen crossed into validation")
        history = {d: bars for d, bars in stock.items() if d < start}
        models, counts = fit_through(history, [d for d in calendar if d < start], start)
        check(
            counts["last_session"] == fold["fit_through"], "Inner fit includes a different session"
        )
        # Match the first-pass screen used that evening. The 16-finalist audit and paper
        # strategy use the stricter input-age check, which is replay()'s default.
        stats, daily, trades, _ = replay(
            series(stock, calendar, start, stop), *models, enforce_freshness=False
        )
        check(
            len(daily) == fold["sessions"] == 56 and daily[0]["day"] == str(start), "Inner sample"
        )
        by_day = {r["day"]: (r["entry"], r["side"], r["exit"]) for r in trades}
        pattern = [by_day.get(r["day"]) for r in daily]
        fingerprint = hashlib.sha256(
            json.dumps(pattern, separators=(",", ":")).encode()
        ).hexdigest()
        check(fingerprint == fold["incumbent_action_sha256"], "Archived inner trade path differs")
        np.testing.assert_allclose(stats["net_krw"], fold["incumbent_net_krw"], atol=0.005, rtol=0)
        examples.append(trades[0])
    return examples


if __name__ == "__main__":
    study = read_json(RESULTS / "study.json")
    evidence = read_json(RESULTS / "tuning.json")
    stock, calendar = load_market(study)
    reproduce_grid(stock, calendar, evidence, load_models(study))
    check_final_screen(evidence)
