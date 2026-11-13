"""Research-only conditional relative strength and account-risk sizing.

No broker, network, order, or active-run dependency. A signal is a hypothesis,
not a probability estimate. Historical bars estimate beta; the disjoint recent
window must outperform in BOTH rising and falling benchmark intervals.
"""
from __future__ import annotations

import math
import statistics as stats
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RotationSettings:
    training_returns: int = 60
    signal_returns: int = 10
    min_each_regime: int = 3
    min_correlation: float = .5
    beta_min: float = .25
    beta_max: float = 4
    beta_change_limit: float = .5
    min_strength: float = 1.5
    min_positive_fraction: float = .6
    require_both_regimes: bool = True
    sizing_mode: str = 'risk_budget'
    cash: float = 100_000
    trade_risk_fraction: float = .005
    session_loss_fraction: float = .02
    gross_notional_multiple: float = 12
    unhedged_stress_fraction: float = .01
    margin_fraction: float = .6
    hedge_error_limit: float = .05
    max_contracts: int = 100
    extra_ticks_per_side: float = 1
    max_cost_fraction_of_risk: float = .4
    reward_risk: float = 2


def _fit(returns: list[tuple[float, float]]) -> dict[str, float] | None:
    a, b = zip(*returns)
    ma, mb = stats.fmean(a), stats.fmean(b)
    va, vb = stats.pvariance(a), stats.pvariance(b)
    if min(va, vb) <= 1e-18:
        return None
    cov = stats.fmean((x - ma) * (y - mb) for x, y in returns)
    beta = cov / vb
    residual = [x - beta * y for x, y in returns]
    return {'beta': beta, 'correlation': cov / math.sqrt(va * vb),
            'sigma_a': math.sqrt(va), 'sigma_b': math.sqrt(vb),
            'sigma_residual': stats.pstdev(residual)}


def evaluate_strength(rows: list[dict[str, float]], as_of: int,
                      c: RotationSettings = RotationSettings()) -> dict[str, Any]:
    """Rows are minute OPEN timestamps and aligned A/B closes, never padded.

    Beta estimation ends BEFORE the signal window. Current/incomplete bars and
    future rows are excluded. Enough history is required irrespective of uptime.
    """
    valid = [r for r in rows if r['time'] + 60_000 <= as_of]
    need = c.training_returns + c.signal_returns + 1
    result: dict[str, Any] = {'eligible': False, 'direction': 0, 'reason': 'history',
                              'required_closes': need, 'available_closes': len(valid)}
    if len(valid) < need:
        return result
    selected = valid[-need:]
    if any(not all(math.isfinite(r[k]) and r[k] > 0 for k in ('a', 'b')) for r in selected):
        return {**result, 'reason': 'invalid_price'}
    if any(y['time'] - x['time'] != 60_000 for x, y in zip(selected, selected[1:])):
        return {**result, 'reason': 'missing_or_duplicate_minute'}
    returns = [(y['a'] / x['a'] - 1, y['b'] / x['b'] - 1)
               for x, y in zip(selected, selected[1:])]
    training, recent = returns[:c.training_returns], returns[c.training_returns:]
    fitted = _fit(training)
    if not fitted or fitted['sigma_residual'] <= 1e-10:
        return {**result, 'reason': 'zero_variation'}
    result.update(fitted, training_end=selected[c.training_returns]['time'],
                  signal_start=selected[c.training_returns + 1]['time'],
                  signal_end=selected[-1]['time'], price_a=selected[-1]['a'], price_b=selected[-1]['b'])
    first, last = _fit(training[:len(training) // 2]), _fit(training[len(training) // 2:])
    beta = fitted['beta']
    if (not c.beta_min <= beta <= c.beta_max or fitted['correlation'] < c.min_correlation
            or not first or not last or min(first['beta'], last['beta']) <= 0
            or abs(first['beta'] - last['beta']) / beta > c.beta_change_limit):
        return {**result, 'reason': 'unstable_hedge'}
    residuals = [a - beta * b for a, b in recent]
    noise_floor = .1 * fitted['sigma_b']
    rising = [e for e, (_, b) in zip(residuals, recent) if b > noise_floor]
    falling = [e for e, (_, b) in zip(residuals, recent) if b < -noise_floor]
    result.update(up_count=len(rising), down_count=len(falling),
                  excess_return=sum(residuals), sigma_stop_floor=2 * fitted['sigma_residual'])
    if c.require_both_regimes and min(len(rising), len(falling)) < c.min_each_regime:
        return {**result, 'reason': 'both_regimes_required'}
    up, down = stats.fmean(rising) if rising else None, stats.fmean(falling) if falling else None
    if c.require_both_regimes:
        direction = 1 if min(up, down) > 0 else -1 if max(up, down) < 0 else 0
    else:
        direction = 1 if sum(residuals) > 0 else -1 if sum(residuals) < 0 else 0
    # This ratio is a descriptive threshold, NOT a t-test or win probability.
    strength = sum(residuals) / (fitted['sigma_residual'] * math.sqrt(len(residuals)))
    result.update(up_excess_bps=up * 10_000 if up is not None else None,
                  down_excess_bps=down * 10_000 if down is not None else None, strength=strength)
    if not direction:
        return {**result, 'reason': 'direction_dependent_strength'}
    groups = (rising, falling) if c.require_both_regimes else (residuals,)
    breadth = min(sum(direction * e > 0 for e in group) / len(group) for group in groups)
    if (breadth < c.min_positive_fraction or direction * strength < c.min_strength
            or any(direction * e <= 0 for e in residuals[-2:])):
        return {**result, 'reason': 'weak_or_expired_strength', 'breadth': breadth}
    path = [0.0]
    for e in residuals:
        path.append(path[-1] + e)
    pivot = min(path[-4:]) if direction > 0 else max(path[-4:])
    distance = max(2 * fitted['sigma_residual'],
                   direction * (path[-1] - pivot) + fitted['sigma_residual'])
    return {**result, 'eligible': True, 'direction': direction, 'reason': 'signal',
            'breadth': breadth, 'stop_return': distance,
            'stop_reference': 'prior three minute residual swing plus one training sigma',
            'model': 'conditional relative momentum, not convergence or information causality'}


def _book_prices(book: dict[str, Any], quantity: int) -> tuple[float, float] | None:
    prices = []
    for side, reverse in (('bids', True), ('asks', False)):
        remaining, value = quantity, 0.0
        levels = sorted(book.get(side, []), key=lambda x: x['price'], reverse=reverse)
        for level in levels:
            if not all(isinstance(level.get(k), (int, float)) and math.isfinite(level[k]) and level[k] > 0 for k in ('price', 'quantity')):
                continue
            take = min(remaining, int(level['quantity']))
            value += take * level['price']
            remaining -= take
            if not remaining:
                break
        if remaining:
            return None
        prices.append(value / quantity)
    if prices[0] > prices[1]:
        return None
    return prices[0], prices[1]


def _combine_routes(options: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """An economic leg can consist of mini tickets plus micro tickets.

    Multiplier equivalence is required. They must be the same underlying/month;
    the caller supplies the reviewed product family, rather than guessing it.
    """
    standalone = [{**x, 'tickets': [{'code': x['code'], 'quantity': x['quantity']}],
                   'equivalent_micro_quantity': x['quantity'] * x['micro_units']}
                  for x in options if x['quantity'] * x['micro_units'] <= limit]
    micros = [x for x in standalone if x['micro_units'] == 1]
    minis = [x for x in standalone if x['micro_units'] == 10]
    combined = []
    for mini in minis:
        for micro in micros:
            units = mini['equivalent_micro_quantity'] + micro['equivalent_micro_quantity']
            if units > limit or abs(mini['quote_at'] - micro['quote_at']) > 2000:
                continue
            margin = mini['margin'] + micro['margin'] if mini['margin'] is not None and micro['margin'] is not None else None
            combined.append({'code': mini['code'] + '+' + micro['code'],
                'tickets': mini['tickets'] + micro['tickets'], 'equivalent_micro_quantity': units,
                'notional': mini['notional'] + micro['notional'], 'cost': mini['cost'] + micro['cost'],
                'commission': mini['commission'] + micro['commission'], 'margin': margin,
                'quote_at': min(mini['quote_at'], micro['quote_at']),
                'quote_latest': max(mini['quote_at'], micro['quote_at']),
                'fee_confirmed': mini['fee_confirmed'] and micro['fee_confirmed']})
    return standalone + combined


def plan_sizes(reference: dict[str, Any], products: list[dict[str, Any]], *, at: int,
               session_net: float = 0, c: RotationSettings = RotationSettings()) -> dict[str, Any]:
    """Compare two economic legs (up to four tickets) under risk constraints.

    Missing margin/fee confirmation is disclosed and cannot authorize orders.
    Output is an indicative calculation, including when no entry signal exists.
    """
    if c.sizing_mode not in ('risk_budget', 'max_affordable'):
        raise ValueError('Unknown sizing mode')
    maximum = c.sizing_mode == 'max_affordable'
    remaining = c.cash * c.session_loss_fraction + min(0, session_net)
    budget = min(c.cash * c.trade_risk_fraction, remaining)
    out: dict[str, Any] = {'orders_authorized': False, 'entry_signal': bool(reference.get('eligible')),
                           'risk_budget': None if maximum else max(0, budget),
                           'remaining_session_loss_budget': max(0, remaining),
                           'sizing_mode': c.sizing_mode, 'cash': c.cash, 'candidates': [],
                           'reason': 'no_reference', 'margin_verified': False}
    if budget <= 0:
        return {**out, 'reason': 'session_loss_limit'}
    if not all(k in reference for k in ('beta', 'sigma_a', 'sigma_b', 'sigma_stop_floor')):
        return out
    if not c.beta_min <= reference['beta'] <= c.beta_max:
        return out
    stop = reference.get('stop_return', reference['sigma_stop_floor'])
    if not math.isfinite(stop) or stop <= 0:
        return out
    prepared: dict[str, list[dict[str, Any]]] = {'a': [], 'b': []}
    missing_capacity = []
    if maximum:
        for product in products:
            margin = product.get('margin')
            capacity = product.get('new_capacity')
            if (not isinstance(margin, (int, float)) or not math.isfinite(margin) or margin <= 0
                    or not isinstance(capacity, int) or capacity < 0
                    or not 0 <= at - product.get('capacity_at', 0) <= 60_000):
                missing_capacity.append(product['code'])
        if missing_capacity:
            return {**out, 'reason': 'verified_margin_and_fresh_capacity_required',
                    'missing': missing_capacity}
    cash_available = c.cash + min(0, session_net)
    if maximum:
        balances = [p.get('available_amount') for p in products]
        if any(not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 for value in balances):
            return {**out, 'reason': 'available_cash_required'}
        cash_available = min(cash_available, *balances)
        out['available_cash'] = cash_available
        out['capacity_basis'] = 'sum of public standalone margins plus round-trip cost reserve; no spread credits'
    # MNQ/NQ and MES/ES specifications. No arbitrary cross-underlying matching.
    base_multiplier = {'a': 2, 'b': 5}
    for product in products:
        quote_at = product.get('quote_at', 0)
        if product['family'] not in prepared or not 0 <= at - quote_at <= 5000:
            continue
        if not all(math.isfinite(product[k]) and product[k] > 0 for k in ('multiplier', 'tick')):
            continue
        if not math.isfinite(product['fee']) or product['fee'] < 0:
            continue
        micro_units = product['multiplier'] / base_multiplier[product['family']]
        if micro_units not in (1, 10):
            continue
        one = _book_prices(product, 1)
        if not one:
            continue
        mid = sum(one) / 2
        margin = product.get('margin')
        known_margin = isinstance(margin, (int, float)) and math.isfinite(margin) and margin > 0
        max_qty = product['new_capacity'] if maximum else int(c.max_contracts / micro_units)
        if known_margin:
            max_qty = min(max_qty, int(cash_available / margin))
        for qty in range(1, max_qty + 1):
            depth = _book_prices(product, qty)
            if not depth:
                break
            multiplier = product['multiplier']
            cost = (qty * multiplier * (depth[1] - depth[0] + 2 * product['tick'] * c.extra_ticks_per_side)
                    + 2 * qty * product['fee'])
            prepared[product['family']].append({'code': product['code'], 'quantity': qty,
                'notional': qty * multiplier * mid, 'cost': cost, 'quote_at': quote_at,
                'micro_units': int(micro_units),
                'commission': 2 * qty * product['fee'],
                'margin': qty * margin if known_margin else None,
                'fee_confirmed': bool(product.get('fee_confirmed'))})
    unit_limit = c.max_contracts
    if maximum:
        unit_limit = max((sum(p['new_capacity'] * int(p['multiplier'] / base_multiplier[family])
                             for p in products if p['family'] == family) for family in ('a', 'b')), default=0)
    prepared = {family: _combine_routes(options, unit_limit) for family, options in prepared.items()}
    candidates = []
    beta = reference['beta']
    for a in prepared['a']:
        for b in prepared['b']:
            if max(a.get('quote_latest', a['quote_at']), b.get('quote_latest', b['quote_at'])) - min(a['quote_at'], b['quote_at']) > 2000:
                continue
            hedge_error = abs(b['notional'] / (beta * a['notional']) - 1)
            if hedge_error > c.hedge_error_limit:
                continue
            gross = a['notional'] + b['notional']
            if not maximum and gross > c.cash * c.gross_notional_multiple:
                continue
            stress = max(a['notional'] * reference['sigma_a'], b['notional'] * reference['sigma_b']) * 3
            if not maximum and stress > c.cash * c.unhedged_stress_fraction:
                continue
            mismatch = abs(b['notional'] - beta * a['notional']) * 3 * reference['sigma_b']
            cost = a['cost'] + b['cost']
            risk = a['notional'] * stop + mismatch + cost
            margin = a['margin'] + b['margin'] if a['margin'] is not None and b['margin'] is not None else None
            if maximum:
                if margin is None or margin + cost > cash_available:
                    continue
            elif risk > budget or (margin is not None and margin > c.cash * c.margin_fraction):
                continue
            candidates.append({'leg_a': a, 'leg_b': b, 'risk': risk, 'risk_pct': 100 * risk / c.cash,
                'target_net': c.reward_risk * risk, 'estimated_round_trip_cost': cost,
                'commission_round_trip': a['commission'] + b['commission'], 'gross_notional': gross,
                'hedge_error_pct': 100 * hedge_error, 'unhedged_stress': stress, 'margin': margin,
                'cost_bps_of_leg_a': cost / a['notional'] * 10_000,
                'cost_fraction_of_risk': cost / risk,
                'cost_acceptable': cost / risk <= c.max_cost_fraction_of_risk,
                'balanced_notional': min(a['notional'], b['notional'] / beta),
                'risk_exceeds_session_budget': risk > remaining,
                'margin_verified': margin is not None,
                'fees_confirmed': a['fee_confirmed'] and b['fee_confirmed']})
    if not candidates:
        return {**out, 'reason': 'risk_hedge_depth_or_freshness_constraints'}
    acceptable = [row for row in candidates if row['cost_acceptable']]
    selectable = acceptable or candidates
    # Avoid picking a tiny position merely because its total fee is smallest.
    # First retain near-largest feasible A exposure; then minimize cost per dollar.
    if maximum:
        # Maximize HEDGED exposure, not the raw number of tiny contracts.
        # Cost and hedge error only break exposure ties.
        near = sorted(selectable, key=lambda x: (-x['balanced_notional'],
                      x['estimated_round_trip_cost'], x['hedge_error_pct']))
    else:
        max_exposure = max(x['leg_a']['notional'] for x in selectable)
        near = [x for x in selectable if x['leg_a']['notional'] >= .8 * max_exposure]
        near.sort(key=lambda x: (x['cost_bps_of_leg_a'], x['hedge_error_pct'], -x['leg_a']['notional']))
    best_by_route = {}
    for row in near:
        route = (row['leg_a']['code'], row['leg_b']['code'])
        best_by_route.setdefault(route, row)
    chosen = near[0]
    return {**out, 'reason': 'indicative_plan', 'stop_return': stop,
            'feasible_combinations': len(candidates), 'chosen': chosen,
            'candidates': list(best_by_route.values()),
            'research_entry_allowed': bool(reference.get('eligible')) and chosen['cost_acceptable'],
            'margin_verified': chosen['margin_verified'],
            'limitations': ['No orders or fills; risk estimates are not guaranteed loss caps.',
                            'Depth is a snapshot; exit liquidity and slippage can change.',
                            'Broker margin and account-specific fees must be checked before execution.']}
