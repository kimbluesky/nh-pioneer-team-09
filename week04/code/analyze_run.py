"""Derive README statistics from the submitted run. No broker calls or orders."""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import statistics as stats
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))


def read_json(path):
    data = gzip.decompress(path.read_bytes()) if path.suffix == '.gz' else path.read_bytes()
    return json.loads(data)


def read_rows(path):
    data = gzip.decompress(path.read_bytes()) if path.suffix == '.gz' else path.read_bytes()
    return [json.loads(line) for line in data.decode('utf-8').splitlines()]


def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, KST).isoformat(timespec='milliseconds')


def milliseconds(value):
    return round(datetime.fromisoformat(value).timestamp() * 1000)


def distribution(values):
    values = list(values)
    return {'count': len(values), 'min': min(values), 'median': stats.median(values),
            'mean': stats.fmean(values), 'max': max(values)} if values else None


def analyze(folder):
    raw = folder / 'raw'
    events = read_rows(raw / 'events.jsonl.gz')
    market = read_rows(raw / 'market-data.jsonl.gz')
    journal = read_rows(raw / 'mock-events.jsonl')
    state = read_json(raw / 'state.json.gz')
    intent = read_json(raw / 'mock-intent.json')
    with (folder / 'nh-mock/trades.csv').open(encoding='utf-8-sig', newline='') as handle:
        trades = list(csv.DictReader(handle))
    fills = [r for r in journal if r['kind'] == 'fill']
    orders = [r for r in journal if r['kind'] == 'order']
    messages = [r for r in journal if r['kind'] == 'event']
    assert len(trades) == state['mock_summary']['completed_trades'] == 7
    assert len(orders) == len(fills) == 28
    assert not state['enabled'] and state['stop_requested']
    assert all(r['versionId'] == state['id'] and not r['virtual'] for r in fills)
    started, ended = state['started_at'], max(r['time'] for r in fills)

    nets = [float(r['net_usd']) for r in trades]
    positive, negative = [v for v in nets if v > 0], [v for v in nets if v < 0]
    hold = [float(r['hold_seconds']) for r in trades]
    average_win, average_loss = stats.fmean(positive), -stats.fmean(negative)
    leg_results = {}
    for code in ('MNQZ26', 'MESZ26'):
        relevant = [r for r in fills if r['code'] == code]
        price_pnl = sum(r['quantity'] * r['price'] * (1 if r['side'] == 'sell' else -1)
                        for r in relevant) * (2 if code == 'MNQZ26' else 5)
        fees = sum(r['fee'] for r in relevant)
        recorded = sum(float(r[code + '_net_usd']) for r in trades)
        assert abs(price_pnl - fees - recorded) < .001
        leg_results[code] = {'gross_usd': price_pnl, 'modeled_fees_usd': fees,
                            'net_usd': recorded, 'fill_records': len(relevant)}

    latency = []
    for order in orders:
        matching = [r for r in fills if r['orderId'] == order['orderId']]
        assert sum(r['quantity'] for r in matching) == order['quantity'] == order['filled']
        latency.append({'code': order['code'], 'intent': order['intent'], 'order_no': order['orderNo'],
                        'quantity': order['quantity'], 'placed_at_kst': stamp(order['placedAt']),
                        'last_fill_at_kst': stamp(max(r['time'] for r in matching)),
                        'elapsed_ms': max(r['time'] for r in matching) - order['placedAt'],
                        'fill_records': len(matching)})
    pair_skew = []
    for trade in trades:
        lower, upper = milliseconds(trade['entry_at_kst']), milliseconds(trade['exit_at_kst'])
        for purpose in ('entry', 'exit'):
            relevant = [r for r in fills if lower <= r['time'] <= upper and r['intent'] == purpose]
            pair_skew.append({'trade_no': int(trade['trade_no']), 'intent': purpose,
                              'fill_time_difference_ms': max(r['time'] for r in relevant) - min(r['time'] for r in relevant)})

    first = trades[0]
    da = float(first['MNQZ26_entry']) * 2 * int(first['MNQZ26_quantity'])
    db = float(first['MESZ26_entry']) * 5 * int(first['MESZ26_quantity'])
    gaps = [{'code': r['code'], 'time_kst': stamp(r['time']), 'text': r['text']}
            for r in messages if r['event'] == 'gap']
    reconnects = [{'code': r['code'], 'time_kst': stamp(r['time']), 'text': r['text']}
                  for r in messages if '\uc7ac\uc811\uc18d \ub4a4 \ub300\uc0ac \uc77c\uce58' in r['text']]
    first_bars = next(r for r in market if r['kind'] == 'minute_bars')
    waits = [{'previous_trade': i, 'next_trade': i+1,
              'seconds': (milliseconds(b['entry_at_kst']) - milliseconds(a['exit_at_kst'])) / 1000}
             for i, (a, b) in enumerate(zip(trades, trades[1:]), 1)]
    source_files = [raw / n for n in ('events.jsonl.gz', 'market-data.jsonl.gz', 'mock-events.jsonl', 'state.json.gz', 'mock-intent.json')]
    source_files.append(folder / 'nh-mock/trades.csv')
    return {
        'run_id': state['id'],
        'scope': 'Post-run descriptive analysis of original 2026-11-13 records. Trading rules and original records are unchanged.',
        'source_sha256': {str(p.relative_to(folder)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files},
        'trade_statistics': {
            'net_usd': sum(nets), 'mean_net_usd': stats.fmean(nets),
            'winning_net_usd': sum(positive), 'losing_net_usd': sum(negative),
            'mean_win_usd': average_win, 'mean_loss_abs_usd': average_loss,
            'profit_factor': sum(positive) / -sum(negative),
            'realized_payoff_ratio': average_win / average_loss,
            'breakeven_win_rate_from_sample_pct': 100 * average_loss / (average_win + average_loss),
            'hold_seconds': distribution(hold),
            'holding_time_pct': 100 * sum(hold) / ((ended - started) / 1000),
            'waits_between_trades': waits,
            'legs': leg_results,
        },
        'execution': {
            'orders': len(orders), 'outcomes': dict(Counter(r['outcome'] for r in orders)),
            'fill_records': len(fills), 'total_contracts': sum(r['quantity'] for r in fills),
            'orders_with_multiple_fill_records': sum(r['fill_records'] > 1 for r in latency),
            'send_to_fill_ms': distribution(r['elapsed_ms'] for r in latency),
            'latency_scope': 'Client order timestamp to client-observed final fill; not exchange matching latency.',
            'order_timings': latency,
            'pair_fill_time_difference_ms': {purpose: distribution(r['fill_time_difference_ms'] for r in pair_skew if r['intent'] == purpose) for purpose in ('entry', 'exit')},
            'pair_fill_times': pair_skew,
            'gap_messages': gaps, 'reconnect_matches': reconnects,
        },
        'market_data': {
            'record_counts': dict(Counter(r['kind'] for r in market)),
            'record_count_scope': 'Saved snapshots, not a count of all HTTP calls or websocket messages.',
            'first_history_after_start_seconds': (first_bars['received_at'] - started) / 1000,
            'first_history_rows': len(first_bars['value']),
            'first_mock_fill_after_start_seconds': (min(r['time'] for r in fills) - started) / 1000,
        },
        'first_trade_notional_at_fill': {
            'MNQ_usd': da, 'MES_usd': db, 'MES_over_MNQ_pct': 100 * db / da,
            'gross_usd': da + db, 'net_long_usd': da - db,
        },
        'last_trade_frozen_exit': {
            'entry_requested_at_kst': stamp(intent['at']), 'target_net_usd': intent['target'],
            'risk_usd': intent['risk'], 'estimated_round_trip_cost_usd': intent['estimated_round_trip_cost'],
            'cost_over_risk_pct': 100 * intent['estimated_round_trip_cost'] / intent['risk'],
        },
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    result = analyze(args.results)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print('Analysis saved:', args.output)
