"""Independent single-index controls and ablations for the rotation hypothesis.

These calculate signals/positions, not fills or performance. All controls have
the same observation windows and execution-cost model; their entry times are
allowed to differ. A single-index control never reads the other index's return.
"""
from __future__ import annotations

import math
import statistics as stats
from dataclasses import replace
from typing import Any

from backend.strategy.spread_rotation import (
    RotationSettings, _book_prices, _combine_routes, evaluate_strength, plan_sizes,
)


def single_signal(rows: list[dict[str, float]], family: str, at: int,
                  c: RotationSettings = RotationSettings()) -> dict[str, Any]:
    if family not in ('a', 'b'):
        raise ValueError('Unknown index')
    closed = [r for r in rows if r['time'] + 60_000 <= at]
    need = c.training_returns + c.signal_returns + 1
    result = {'eligible': False, 'direction': 0, 'family': family, 'reason': 'history',
              'model': 'independent single-index time-series momentum'}
    if len(closed) < need:
        return result
    selected = closed[-need:]
    if any(not math.isfinite(r[family]) or r[family] <= 0 for r in selected):
        return {**result, 'reason': 'invalid_price'}
    if any(y['time'] - x['time'] != 60_000 for x, y in zip(selected, selected[1:])):
        return {**result, 'reason': 'missing_or_duplicate_minute'}
    returns = [y[family] / x[family] - 1 for x, y in zip(selected, selected[1:])]
    training, recent = returns[:c.training_returns], returns[c.training_returns:]
    sigma = stats.pstdev(training)
    if sigma <= 1e-10:
        return {**result, 'reason': 'zero_variation'}
    net = sum(recent)
    direction = 1 if net > 0 else -1 if net < 0 else 0
    strength = net / (sigma * math.sqrt(len(recent)))
    breadth = sum(direction * value > 0 for value in recent) / len(recent)
    path = [0.0]
    for value in recent:
        path.append(path[-1] + value)
    pivot = min(path[-4:]) if direction > 0 else max(path[-4:])
    stop = max(2 * sigma, direction * (path[-1] - pivot) + sigma)
    eligible = (direction * strength >= c.min_strength and breadth >= c.min_positive_fraction
                and all(direction * value > 0 for value in recent[-2:]))
    return {**result, 'eligible': eligible, 'direction': direction if eligible else 0,
            'reason': 'signal' if eligible else 'weak_or_expired_strength', 'sigma': sigma,
            'strength': strength, 'breadth': breadth, 'stop_return': stop,
            'training_end': selected[c.training_returns]['time'],
            'signal_start': selected[c.training_returns + 1]['time'],
            'signal_end': selected[-1]['time']}


def single_sizes(signal: dict[str, Any], products: list[dict[str, Any]], at: int,
                 c: RotationSettings, session_net: float = 0) -> dict[str, Any]:
    maximum = c.sizing_mode == 'max_affordable'
    remaining = c.cash * c.session_loss_fraction + min(0, session_net)
    budget = min(c.cash * c.trade_risk_fraction, remaining)
    out = {'orders_authorized': False, 'sizing_mode': c.sizing_mode, 'cash': c.cash,
           'entry_signal': signal['eligible'], 'risk_budget': None if maximum else max(0, budget),
           'remaining_session_loss_budget': max(0, remaining), 'reason': 'no_reference', 'candidates': []}
    if remaining <= 0:
        return {**out, 'reason': 'session_loss_limit'}
    if 'sigma' not in signal:
        return out
    selected = [p for p in products if p['family'] == signal['family']]
    cash = c.cash + min(0, session_net)
    if maximum:
        if not selected or any(not isinstance(p.get('margin'), (int, float)) or not math.isfinite(p['margin'])
                               or p['margin'] <= 0 or not isinstance(p.get('new_capacity'), int)
                               or p['new_capacity'] < 0 or not 0 <= at - p.get('capacity_at', 0) <= 60000
                               for p in selected):
            return {**out, 'reason': 'verified_margin_and_fresh_capacity_required'}
        if any(not isinstance(p.get('available_amount'), (int, float))
               or not math.isfinite(p['available_amount']) or p['available_amount'] <= 0 for p in selected):
            return {**out, 'reason': 'available_cash_required'}
        cash = min(cash, *(p['available_amount'] for p in selected))
    options = []
    base = 2 if signal['family'] == 'a' else 5
    units_limit = (sum(p['new_capacity'] * int(p['multiplier'] / base) for p in selected)
                   if maximum else c.max_contracts)
    for p in selected:
        if not 0 <= at - p.get('quote_at', 0) <= 5000:
            continue
        one = _book_prices(p, 1)
        if not one:
            continue
        units = p['multiplier'] / base
        if units not in (1, 10):
            continue
        margin = p.get('margin')
        known_margin = isinstance(margin, (int, float)) and math.isfinite(margin) and margin > 0
        max_qty = p['new_capacity'] if maximum else int(c.max_contracts / units)
        if known_margin:
            max_qty = min(max_qty, int(cash / margin))
        for qty in range(1, max_qty + 1):
            prices = _book_prices(p, qty)
            if not prices:
                break
            cost = (qty * p['multiplier'] * (prices[1] - prices[0] + 2 * p['tick'] * c.extra_ticks_per_side)
                    + 2 * qty * p['fee'])
            options.append({'code': p['code'], 'quantity': qty, 'micro_units': int(units),
                'notional': qty * p['multiplier'] * sum(one) / 2, 'cost': cost,
                'quote_at': p['quote_at'], 'commission': 2 * qty * p['fee'],
                'margin': qty * margin if known_margin else None,
                'fee_confirmed': bool(p.get('fee_confirmed'))})
    candidates = []
    for route in _combine_routes(options, units_limit):
        risk = route['notional'] * signal['stop_return'] + route['cost']
        stress = 3 * route['notional'] * signal['sigma']
        if maximum:
            if route['margin'] is None or route['margin'] + route['cost'] > cash:
                continue
        elif (risk > budget or stress > c.cash * c.unhedged_stress_fraction
              or route['notional'] > c.cash * c.gross_notional_multiple
              or (route['margin'] is not None and route['margin'] > c.cash * c.margin_fraction)):
            continue
        candidates.append({'leg': route, 'risk': risk, 'risk_pct': 100 * risk / c.cash,
            'target_net': c.reward_risk * risk, 'estimated_round_trip_cost': route['cost'],
            'cost_fraction_of_risk': route['cost'] / risk,
            'cost_acceptable': route['cost'] / risk <= c.max_cost_fraction_of_risk,
            'risk_exceeds_session_budget': risk > remaining,
            'gross_notional': route['notional'], 'margin': route['margin'],
            'cost_bps': route['cost'] / route['notional'] * 10_000})
    if not candidates:
        return {**out, 'reason': 'constraints'}
    acceptable = [row for row in candidates if row['cost_acceptable']]
    candidates = acceptable or candidates
    if maximum:
        candidates.sort(key=lambda x: (-x['gross_notional'], x['estimated_round_trip_cost']))
    else:
        largest = max(x['gross_notional'] for x in candidates)
        candidates = [x for x in candidates if x['gross_notional'] >= .8 * largest]
        candidates.sort(key=lambda x: (x['cost_bps'], -x['gross_notional']))
    chosen = candidates[0]
    return {**out, 'reason': 'indicative_plan', 'chosen': chosen,
            'candidates': candidates[:3], 'research_entry_allowed': signal['eligible'] and chosen['cost_acceptable']}


def comparison_snapshot(rows, products, at, c=RotationSettings()):
    """Two panels prevent confusing bigger positions with better signals.

    Main controls: independent Nasdaq and S&P momentum. Ablations: remove only
    the two-regime gate, or remove only mini/micro route selection.
    """
    strength = evaluate_strength(rows, at, c)
    ungated = evaluate_strength(rows, at, replace(c, require_both_regimes=False))
    nasdaq = single_signal(rows, 'a', at, c)
    sp = single_signal(rows, 'b', at, c)
    result = []
    for mode in ('max_affordable', 'risk_budget'):
        settings = replace(c, sizing_mode=mode)
        for name, signal, subset, single in (
            ('spread', strength, products, False),
            ('nasdaq_only', nasdaq, products, True),
            ('sp500_only', sp, products, True),
            ('spread_without_regime_gate', ungated, products, False),
            ('spread_micro_only', strength, [p for p in products if p['multiplier'] in (2, 5)], False),
        ):
            plan = single_sizes(signal, subset, at, settings) if single else plan_sizes(signal, subset, at=at, c=settings)
            result.append({'arm': name, 'sizing_mode': mode, 'starting_equity': settings.cash,
                           'signal': signal, 'plan': plan, 'performance': None})
    return {'status': 'signals_and_position_plans_only', 'arms': result,
            'execution_model_required': 'same next-quote delay, displayed depth, fees and slippage assumptions',
            'comparison_period_required': 'same forward start and stop; no old account fills',
            'risk_matching': 'risk_budget panel uses the same cap; actual integer-sized risk must also be reported',
            'metrics_required': ['net_return_pct', 'net_profit', 'max_drawdown', 'completed_trades',
                                 'fees', 'entry_risk', 'gross_notional', 'margin_used', 'rejections'],
            'profitability_evidence': False}
