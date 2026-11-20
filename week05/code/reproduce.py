"""Replay the published study without an NH account. All paths are relative to this file."""

import argparse
import csv
import gzip
import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np

from backtest import FixedSide, HoldToDeadline, replay, series
from market import CLOCK, MULT, allowed, build, due, exchange_cash, morning, past_day, state
from policy import ExitPolicy, Policy, checkpoints, exit_state, learn, paths, train


RESULTS = Path(__file__).resolve().parents[1] / "results"
FIRST, THREE, VALID, TEST, END = map(
    date.fromisoformat, ("2022-11-19", "2023-11-19", "2026-08-19", "2026-10-19", "2026-11-19")
)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def check(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_market(study):
    source = RESULTS / "hynix_5m.csv.gz"
    coverage = study["collection"]
    check(digest(source) == coverage["csv_sha256"], "Market archive hash mismatch")
    stock = defaultdict(dict)
    previous = ""
    with gzip.open(source, "rt", encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            stamp = row["time_kst"]
            t = datetime.fromisoformat(stamp)
            check(
                stamp > previous
                and t.utcoffset() == timedelta(hours=9)
                and t.minute % 5 == t.second == 0,
                f"Duplicate or off-grid bar {stamp}",
            )
            previous = stamp
            values = tuple(float(row[k]) for k in ("open", "high", "low", "close"))
            op, high, low, close = values
            volume = int(row["volume"])
            check(0 < low <= min(op, close) <= max(op, close) <= high and volume >= 0, stamp)
            stock[t.date()][t.hour * 60 + t.minute] = (*values, volume)
    missing = {date.fromisoformat(d) for d in coverage["missing_vs_index"]}
    calendar = sorted(set(stock) | missing)
    check(
        not set(stock) & missing
        and len(calendar) == coverage["daily_reference_days"]
        and len(stock) == coverage["unique_days"]
        and sum(map(len, stock.values())) == coverage["unique_5m"],
        "Coverage mismatch",
    )
    return dict(stock), calendar


def load_models(study):
    models = RESULTS / "models"
    for name, expected in study["models"].items():
        check(digest(models / (name + ".npz")) == expected["sha256"], name + " hash")
    return Policy.load(models / "entry.npz"), ExitPolicy.load(models / "exit.npz")


def fit_through(stock, calendar, stop):
    """Fit the released specification using only sessions before the exclusive cutoff."""
    check(THREE < stop <= VALID, "Training cutoff outside the study")
    train_tape = build(stock, calendar, FIRST, stop)
    entry_days = np.arange(len(train_tape.days))
    exit_days = np.array([i for i, d in enumerate(train_tape.days) if d >= THREE])
    check(max(train_tape.days) < stop, "Future training session")
    fitted_entry, _ = train(train_tape, entry_days, 32, False)
    samples = paths(train_tape, stock)
    fitted_exit = learn(samples, exit_days, 0)
    counts = dict(last_session=train_tape.days[-1].isoformat(), entry_days=len(entry_days),
                  exit_days=len(exit_days), exit_states=int(np.isin(samples.day, exit_days).sum()))
    return (fitted_entry, fitted_exit), counts


def check_fitted_weights(fitted_models, frozen_models):
    fitted_entry, fitted_exit = fitted_models
    entry, exit_model = frozen_models
    check((fitted_entry.half, fitted_entry.residual, fitted_exit.margin)
          == (entry.half, entry.residual, exit_model.margin), "Policy parameters differ")
    for fitted, saved in (
        (fitted_entry.entry, entry.entry),
        (fitted_entry.wait, entry.wait),
        (fitted_exit.weights, exit_model.weights),
    ):
        np.testing.assert_allclose(fitted, saved, rtol=1e-8, atol=1e-10)


def refit(stock, calendar, entry, exit_model):
    fitted, counts = fit_through(stock, calendar, VALID)
    check((counts["entry_days"], counts["exit_days"], counts["exit_states"]) == (907, 661, 100658),
          "Training sample counts changed")
    check_fitted_weights(fitted, (entry, exit_model))
    print("Refit matched frozen weights: 907 entry days, 661 exit days, 100658 exit states.")


def clock(minute):
    return f"{minute // 60:02d}:{minute % 60:02d}"


def kst_time(stamp):
    return datetime.fromisoformat(stamp)


def verify_paper(stock, entry, exit_model, study):
    log = read_json(RESULTS / "strategy-runs.json")
    (run,) = log["runs"]
    path = RESULTS / "trades.jsonl"
    check(digest(path) == run["trade_log_sha256"], "Fill journal hash")
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    check(all(e["runId"] == run["id"] for e in events + log["events"]), "Mixed runs")
    ledger = []
    for role, side in (("entry", -1), ("exit", 1)):
        intent, ack, fill = (e for e in events if e["event"]["role"] == role)
        check(
            [e["kind"] for e in (intent, ack, fill)] == ["order_intent", "ack", "confirmed_fill"]
            and intent["event"]["side"] == side
            and intent["event"]["environment"] == "paper"
            and intent["event"]["symbol"] == run["code"]
            and ack["event"]["order_ref"] == fill["event"]["order_ref"]
            and intent["event"]["quantity"] == fill["event"]["quantity"] == 1,
            "Order history mismatch",
        )
        ledger.append(fill["event"])
    sell, buy = ledger
    gross = (Decimal(str(sell["price"])) - Decimal(str(buy["price"]))) * MULT
    fee = (Decimal(str(sell["price"])) + Decimal(str(buy["price"]))) * MULT * Decimal("0.000085")
    net = gross - fee
    np.testing.assert_allclose(
        [float(gross), float(fee), float(net)],
        [
            run["realized_gross_krw"],
            run["estimated_fees_krw"],
            run["estimated_net_from_closed_trades_krw"],
        ],
    )

    day = date.fromisoformat(run["date"])
    with (RESULTS / "paper_bars.csv").open(encoding="utf-8", newline="") as stream:
        bars = list(csv.DictReader(stream))

    def visible(d, as_of):
        return {
            int(r["minute"]): (
                *(float(r[k]) for k in ("open", "high", "low", "close")),
                int(r["volume"]),
            )
            for r in bars
            if r["date"] == str(d) and kst_time(r["first_seen_kst"]) <= as_of
        }

    (signal,) = [e for e in log["events"] if e["kind"] == "signal"]
    start = kst_time(signal["time"])
    previous = day - timedelta(days=1)
    history = [past_day(d, stock[d]) for d in sorted(stock)[-63:]]
    prior = morning(history + [past_day(previous, visible(previous, start))], day)
    s = signal["event"]
    i = CLOCK.index(s["minute"])
    x, drift, friction, scale = state(day, i, visible(day, start), prior)
    np.testing.assert_allclose(entry.score(i, x, drift, friction), s["scores"], rtol=1e-8, atol=1e-10)
    check(
        int(entry.act(i, x, drift, friction, allowed(day, i), day.toordinal())) == s["action"] == 3,
        "Actual entry signal differs",
    )
    hh, mm, _ = map(int, sell["broker_fill_time"].split(":"))
    filled_minute = (hh * 60 + mm) // 5 * 5
    deadline = due(day, filled_minute, s["action"])
    exits = [e for e in log["events"] if e["kind"] == "paper_exit_signal"]
    check(
        [e["event"]["minute"] for e in exits] == list(checkpoints(filled_minute, deadline)),
        "Exit decision coverage",
    )
    for event in exits:
        p = event["event"]
        obs = exit_state(
            visible(day, kst_time(event["time"])),
            p["minute"],
            filled_minute,
            sell["price"],
            -1,
            deadline,
            scale,
        )
        v = float(exit_model.value(obs))
        np.testing.assert_allclose(v, p["incremental_hold_value"], rtol=1e-8, atol=1e-10)
        check((v < 0) == p["exit"], "Exit action mismatch")
    (end,) = [e["event"] for e in log["events"] if e["kind"] == "session_end"]
    check(
        end["pending_count"] == 0
        and not end["final_position"]
        and run["broker_final_flat"]
        and run["closed_round_trips"] == 1
        and run["confirmed_fills"] == 2,
        "Final account mismatch",
    )
    completed = visible(day, kst_time(run["observed_kst"]))
    opening, close = completed[s["minute"]][0], completed[deadline][0]
    shadow = study["paper"]["quote_only_comparison"]
    check(
        (opening, close, shadow["orders_sent"])
        == (shadow["entry_reference"], shadow["exit_reference"], 0),
        "Reference quote mismatch",
    )
    np.testing.assert_allclose(
        exchange_cash(-1, opening, close)[3], shadow["scheduled_simulated_net_krw"]
    )
    print(
        f"NH paper: entry and {len(exits)} exit scores matched, 2 fills, gross {gross:,.2f},"
        f" estimated fees {fee:,.2f}, estimated net {net:,.2f} KRW, final position 0."
    )


def verify_backtests(stock, calendar, models, study):
    records = []
    for phase, first, stop in (("validation", VALID, TEST), ("test", TEST, END)):
        tape = series(stock, calendar, first, stop)
        controls = {
            "policy": models,
            "short": (FixedSide(-1), HoldToDeadline()),
            "long": (FixedSide(1), HoldToDeadline()),
            "hold": (models[0], HoldToDeadline()),
        }
        outputs = {}
        for name, pair in controls.items():
            summary, daily, trades, decisions = replay(tape, *pair)
            if name == "policy":
                stale = [
                    (d["day"], d["minute"], d["last_input_start"], d["status"])
                    for d in decisions
                    if "stale" in d["status"]
                ]
                reference = [
                    (d["day"], int(d["minute"]), int(d["last_input_start"]), d["status"])
                    for d in study["execution_check"]["stale_decisions"][phase]
                ]
                check(stale == reference, "Freshness audit differs")
            for key, expected in study["backtest"][phase][name].items():
                np.testing.assert_allclose(
                    summary[key], expected, rtol=1e-12, atol=1e-6, err_msg=f"{phase}/{name}/{key}"
                )
            outputs[name] = (daily, {t["day"]: t for t in trades})
        for i, row in enumerate(outputs["policy"][0]):
            trade = outputs["policy"][1][row["day"]]
            records.append(
                dict(
                    split=phase,
                    date=row["day"],
                    entry=clock(trade["entry"]),
                    exit=clock(trade["exit"]),
                    scheduled_exit=clock(trade["original_exit"]),
                    action=trade["action"],
                    direction="short" if trade["side"] < 0 else "long",
                    entry_reference=int(trade["entry_reference"]),
                    exit_reference=int(trade["exit_reference"]),
                    gross_krw=f"{trade['gross_krw']:.2f}",
                    slippage_krw=f"{trade['slippage_krw']:.2f}",
                    fee_krw=f"{trade['fee_krw']:.2f}",
                    net_krw=f"{trade['net_krw']:.2f}",
                    fixed_short_net_krw=f"{outputs['short'][0][i]['net_krw']:.2f}",
                    fixed_long_net_krw=f"{outputs['long'][0][i]['net_krw']:.2f}",
                    entry_fill=int(trade["entry_fill"]),
                    exit_fill=int(trade["exit_fill"]),
                    trade_return_pct=f"{100 * trade['net_krw'] / (MULT * trade['entry_reference']):.6f}",
                )
            )
        if phase == "test":
            trades = list(outputs["policy"][1].values())
            delayed = []
            for trade in trades:
                bars = stock[date.fromisoformat(trade["day"])]
                en, ex = trade["entry"] + 5, trade["exit"] + 5
                if en in bars and ex in bars and bars[en][4] > 0 and bars[ex][4] > 0:
                    delayed.append(exchange_cash(trade["side"], bars[en][0], bars[ex][0])[3])
            check(
                len(delayed) == study["test_stress"]["five_minute_later_fills_days"],
                "Delayed fill count",
            )
            without_two = sum(sorted(t["net_krw"] for t in trades)[:-2])
            np.testing.assert_allclose(
                [sum(delayed), without_two],
                [
                    study["test_stress"]["five_minute_later_fills_net_krw"],
                    study["test_stress"]["without_two_best_days_krw"],
                ],
            )
        s = study["backtest"][phase]["policy"]
        print(
            f"{phase}: {s['trades']} round trips, net {s['net_krw']:,.2f}, max drawdown {s['drawdown_krw']:,.2f} KRW"
        )
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--train", action="store_true", help="Refit both models on pre-validation history"
    )
    parser.add_argument(
        "--export", type=Path, help="Also write the reproduced trade ledger to this path"
    )
    args = parser.parse_args()
    study = read_json(RESULTS / "study.json")
    stock, calendar = load_market(study)
    print(f"{sum(map(len, stock.values())):,} unique bars, {len(stock)}/{len(calendar)} sessions.")
    models = load_models(study)
    if args.train:
        refit(stock, calendar, *models)
    rows = verify_backtests(stock, calendar, models, study)
    with (RESULTS / "backtest.csv").open(encoding="utf-8", newline="") as stream:
        check(
            list(csv.DictReader(stream)) == [{k: str(v) for k, v in r.items()} for r in rows],
            "Published ledger differs",
        )
    verify_paper(stock, *models, study)
    if args.export:
        with args.export.open("x", encoding="utf-8", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=rows[0], lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)


if __name__ == "__main__":
    main()
