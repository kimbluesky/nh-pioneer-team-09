"""Reconcile the NH fill export, order events, FIFO P&L and closing inventory."""

import csv
import json
from collections import Counter, deque
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def reconcile():
    settings = json.loads((ROOT / "strategy.json").read_text(encoding="utf-8"))
    cutoff = datetime.fromisoformat(settings["cutoff"])
    multiplier = Decimal(str(settings["usd_per_point"]))
    fee = Decimal(str(settings["fee_per_contract_side_usd"]))
    with (ROOT / "results" / "session.jsonl").open(encoding="utf-8") as stream:
        events = [json.loads(line) for line in stream]
    with (ROOT / "results" / "fills.csv").open(encoding="utf-8-sig", newline="") as stream:
        fills = list(csv.DictReader(stream))
    by_type = Counter(e["event"] for e in events)
    acknowledged = {e["ref"] for e in events if e["event"] in ("order_ack", "ack_recovered")}
    confirmed = {e["ref"]: e for e in events if e["event"] == "confirmed_fill"}
    assert len(confirmed) == by_type["confirmed_fill"] == len(fills)
    assert {f["ref"] for f in fills} == set(confirmed)
    assert not any(
        e["event"] == "order_intent" and e["role"] == "entry" and datetime.fromisoformat(e["kst"]) >= cutoff
        for e in events
    )
    lots = deque()
    transactions = []
    cumulative = gross = fees = Decimal("0")
    held = timedelta()
    for fill in fills:
        event = confirmed[fill["ref"]]
        assert fill["ref"] in acknowledged and event["kst"] == fill["kst"]
        price = Decimal(fill["price"])
        side = int(fill["side"])
        when = datetime.fromisoformat(fill["kst"])
        assert Decimal(str(event["price"])) == price and int(fill["qty"]) == event["qty"] == 1
        fees += fee
        if fill["role"] == "entry":
            if not lots:
                start, previous = when, cumulative
            assert not lots or lots[0][0] == side
            lots.append((side, price))
        else:
            entry_side, entry_price = lots.popleft()
            assert entry_side == -side
            change = (price - entry_price) * multiplier * entry_side
            gross += change
            gain = change - 2 * fee
            assert gain == Decimal(fill["trade_net"])
            cumulative += gain
            if not lots:
                held += when - start
                transactions.append((start.time(), when.time(), cumulative - previous))
        assert len(lots) == int(fill["remaining_qty"]) <= 2
        assert cumulative == Decimal(fill["day_net"])
    close = events[-1]
    assert (close["event"], close["flat"], close["quantity"], close["pending"]) == (
        "session_end",
        True,
        0,
        0,
    )
    assert datetime.fromisoformat(close["kst"]) >= cutoff and not lots and gross - fees == cumulative
    summary = json.loads((ROOT / "results" / "close.json").read_text(encoding="utf-8"))
    assert summary["source"] == "NH_DEMO" and summary["done"] and summary["broker_flat"]
    assert summary["window_closed_kst"] == close["kst"]
    assert summary["fills"] == len(fills) and summary["completed"] == len(transactions)
    assert summary["quantity"] == 0 and not summary["pending"]
    assert summary["forecasts"] == by_type["forecast"]
    assert Decimal(str(summary["realized_net"])) == cumulative
    assert summary["scale_in_fills"] == sum(f["phase"] == "add" for f in fills)
    assert summary["partial_exit_fills"] == sum(f["reason"] in ("scale_price", "model_reduce") for f in fills)
    return transactions, gross, fees, cumulative, held, by_type["forecast"], close["kst"]


if __name__ == "__main__":
    transactions, gross, fees, net, held, minutes, closed = reconcile()
    print("Start KST / Close KST / Net USD")
    for start, end, profit in transactions:
        print(start, end, f"{profit:+.2f}", sep=" / ")
    print(f"Gross {gross:+.2f} | Fees {fees:.2f} | Net {net:+.2f} USD")
    print(f"{minutes} forecasts | Holding time {held} | Flat at {closed}")
