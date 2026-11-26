"""Compute the study tables and validation learning curve from the submitted data."""

import csv
from datetime import date
from math import ceil
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np

from backtest import FixedSide, HoldToDeadline, replay, series
from market import MULT
from reproduce import TEST, VALID, check, check_fitted_weights, fit_through


METHODS = {"policy": "net_krw", "short": "fixed_short_net_krw", "long": "fixed_long_net_krw"}
SPLITS = ("validation", "test")


def write_csv(path, rows):
    with NamedTemporaryFile(
        dir=path.parent, suffix=".tmp", mode="w", delete=False, encoding="utf-8", newline=""
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0], lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        temp = Path(stream.name)
    temp.replace(path)


def equity_curve(profits, notional):
    equity = np.r_[0.0, np.cumsum(profits)]
    nav = notional + equity
    peak = np.maximum.accumulate(nav)
    return 100 * equity / notional, 100 * (nav / peak - 1), peak - nav


def block_interval(profits):
    """Percentile interval for the mean daily P&L using overlapping five-session blocks."""
    profits = np.asarray(profits, dtype=float)
    width, samples = 5, 4000
    rng = np.random.default_rng(261119)
    starts = rng.integers(len(profits) - width + 1, size=(samples, ceil(len(profits) / width)))
    indices = (starts[:, :, None] + np.arange(width)).reshape(samples, -1)[:, : len(profits)]
    return np.quantile(profits[indices].mean(axis=1), [0.025, 0.975])


def compare(ledger, study):
    table = []
    for split in SPLITS:
        rows = [r for r in ledger if r["split"] == split]
        notional = MULT * int(rows[0]["entry_reference"])
        short_profits = np.array([float(r[METHODS["short"]]) for r in rows])
        for method, col in METHODS.items():
            profits = np.array([float(r[col]) for r in rows])
            returns, drawdown, loss = equity_curve(profits, notional)
            gains, losses = profits[profits > 0].sum(), -profits[profits < 0].sum()
            summary = study["backtest"][split][method]
            wins = int((profits > 0).sum())
            check(len(rows) == summary["trades"] and wins == summary["wins"], "Trade count")
            np.testing.assert_allclose(
                [profits.sum(), loss.max()],
                [summary["net_krw"], summary["drawdown_krw"]],
                atol=1e-6,
            )
            lo, hi = block_interval(profits)
            paired = profits - short_profits
            paired_low, paired_high = block_interval(paired)
            table.append(
                dict(
                    split=split,
                    method=method,
                    trades=len(rows),
                    wins=wins,
                    starting_notional_krw=notional,
                    gross_krw=f"{summary['gross_krw']:.2f}",
                    slippage_krw=f"{summary['slippage_krw']:.2f}",
                    fee_krw=f"{summary['fee_krw']:.2f}",
                    net_krw=f"{profits.sum():.2f}",
                    return_pct=f"{returns[-1]:.6f}",
                    max_drawdown_krw=f"{loss.max():.2f}",
                    max_drawdown_pct=f"{-drawdown.min():.6f}",
                    win_pct=f"{100 * wins / len(rows):.6f}",
                    profit_factor=f"{gains / losses:.6f}",
                    mean_daily_krw=f"{profits.mean():.2f}",
                    mean_daily_ci_low_krw=f"{lo:.2f}",
                    mean_daily_ci_high_krw=f"{hi:.2f}",
                    vs_short_mean_daily_krw=f"{paired.mean():.2f}",
                    vs_short_ci_low_krw=f"{paired_low:.2f}",
                    vs_short_ci_high_krw=f"{paired_high:.2f}",
                )
            )
            if split == "test" and method == "policy":
                np.testing.assert_allclose([lo, hi], study["test_stress"]["daily_5day_block95_krw"])
    return table


def learning_curve(stock, calendar, frozen, ledger):
    """Expand the training history, freeze each fit and score it on the same validation tape."""
    cutoffs = [
        date.fromisoformat(s)
        for s in ("2024-08-19", "2025-02-19", "2025-08-19", "2026-02-19", "2026-08-19")
    ]
    validation = series(stock, calendar, VALID, TEST)
    short, _, _, _ = replay(validation, FixedSide(-1), HoldToDeadline())
    valid_rows = [r for r in ledger if r["split"] == "validation"]
    notional = MULT * int(valid_rows[0]["entry_reference"])
    rows = []
    for stop in cutoffs:
        models, counts = fit_through(stock, calendar, stop)
        summary, daily, trades, _ = replay(validation, *models)
        check(len(daily) == len(valid_rows), "Validation dates differ")
        check([d["day"] for d in daily] == [r["date"] for r in valid_rows], "Validation calendar")
        if stop == VALID:
            check_fitted_weights(models, frozen)
            np.testing.assert_allclose(
                [v["net_krw"] for v in daily], [float(r["net_krw"]) for r in valid_rows], atol=1e-6
            )
        rows.append(
            dict(
                cutoff_exclusive=str(stop),
                **counts,
                validation_days=len(daily),
                trades=summary["trades"],
                wins=summary["wins"],
                early_exits=summary["early_exits"],
                first_checkpoint_exits=sum(
                    t["exit"] == t["entry"] + 30 and t["exit"] < t["original_exit"] for t in trades
                ),
                validation_net_krw=f"{summary['net_krw']:.2f}",
                validation_return_pct=f"{100 * summary['net_krw'] / notional:.6f}",
                validation_max_drawdown_krw=f"{summary['drawdown_krw']:.2f}",
                fixed_short_return_pct=f"{100 * short['net_krw'] / notional:.6f}",
            )
        )
        print(
            f"Fit through {counts['last_session']}: {counts['entry_days']} / "
            f"{counts['exit_days']} sessions, validation {rows[-1]['validation_return_pct']}%.",
            flush=True,
        )
    return rows
