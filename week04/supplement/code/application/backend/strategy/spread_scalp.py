"""Short-horizon relative momentum confirmed by observed quote depth.

Depth snapshots are NOT trade aggressor flow or event-level OFI. Price and book
conditions are falsifiable hypotheses; neither estimates a winning probability.
"""
from __future__ import annotations

import math
import statistics as stats
from dataclasses import replace

from backend.strategy.spread_rotation import RotationSettings, _book_prices, _fit


def scalp_settings():
    return replace(RotationSettings(), sizing_mode='max_affordable', training_returns=30,
                   signal_returns=3, min_strength=1.0, min_positive_fraction=.5,
                   min_correlation=.3, require_both_regimes=False, reward_risk=1.5)


def depth_pressure(product):
    """Distance-weighted top-three displayed-depth imbalance, in [-1, 1]."""
    values = []
    for name, reverse in (('bids', True), ('asks', False)):
        levels = sorted((x for x in product.get(name, [])
                         if all(isinstance(x.get(k), (float, int)) and math.isfinite(x[k])
                                and x[k] > 0 for k in ('price', 'quantity'))),
                        key=lambda x: x['price'], reverse=reverse)[:3]
        values.append(sum(x['quantity'] / (i + 1) for i, x in enumerate(levels)))
    return (values[0] - values[1]) / sum(values) if min(values) > 0 else None


def reference_products(products):
    # The larger contract in each reviewed family supplies the signal book.
    return {family: max((p for p in products if p['family'] == family),
                        key=lambda p: p['multiplier'], default=None) for family in ('a', 'b')}


def update_pressure(samples, products, at):
    if samples and at - samples[-1]['at'] < 5000:
        return
    refs = reference_products(products)
    if any(p is None or not 0 <= at - p.get('quote_at', 0) <= 5000
           or _book_prices(p, 1) is None for p in refs.values()):
        return
    if abs(refs['a']['quote_at'] - refs['b']['quote_at']) > 2000:
        return
    pressure = {family: depth_pressure(p) for family, p in refs.items()}
    if any(v is None for v in pressure.values()):
        return
    if samples and all(samples[-1].get(f'{family}_quote') == refs[family]['quote_at'] for family in refs):
        return
    samples.append({'at': at, **pressure, **{f'{f}_quote': p['quote_at'] for f, p in refs.items()}})
    del samples[:-60]


def pressure_signal(samples, at, family=None):
    recent = [p for p in samples if 0 <= at - p['at'] <= 20000][-3:]
    if len(recent) < 3 or recent[-1]['at'] - recent[0]['at'] < 10000:
        return None
    if any(b['at'] - a['at'] > 8000 for a, b in zip(recent, recent[1:])):
        return None
    values = [p[family] if family else (p['a'] - p['b']) / 2 for p in recent]
    return stats.fmean(values)


def scalp_signal(rows, products, at, samples, c=None, *, family=None, confirm_depth=True):
    c = c or scalp_settings()
    closed = [r for r in rows if r['time'] + 60000 <= at]
    need = c.training_returns + c.signal_returns + 1
    out = {'eligible': False, 'direction': 0, 'reason': 'history', 'family': family,
           'model': 'three-minute momentum plus persistent displayed-depth confirmation',
           'required_closes': need, 'available_closes': len(closed), 'depth_confirmation': confirm_depth}
    if len(closed) < need:
        return out
    selected = closed[-need:]
    if at - (selected[-1]['time'] + 60000) > 90000:
        return {**out, 'reason': 'stale_history'}
    fields = (family,) if family else ('a', 'b')
    if any(not math.isfinite(r[k]) or r[k] <= 0 for r in selected for k in fields):
        return {**out, 'reason': 'invalid_price'}
    if any(b['time'] - a['time'] != 60000 for a, b in zip(selected, selected[1:])):
        return {**out, 'reason': 'missing_or_duplicate_minute'}
    refs = reference_products(products)
    if any(refs[k] is None or not 0 <= at - refs[k]['quote_at'] <= 5000 for k in fields):
        return {**out, 'reason': 'stale'}
    if len(fields) == 2 and abs(refs['a']['quote_at'] - refs['b']['quote_at']) > 2000:
        return {**out, 'reason': 'stale'}
    prices = {k: _book_prices(refs[k], 1) for k in fields}
    if any(p is None for p in prices.values()):
        return {**out, 'reason': 'invalid_price'}
    mid = {k: sum(p) / 2 for k, p in prices.items()}
    returns = [{k: b[k] / a[k] - 1 for k in fields} for a, b in zip(selected, selected[1:])]
    training, recent = returns[:c.training_returns], returns[c.training_returns:]
    base = selected[c.training_returns]
    horizon = (at - base['time'] - 60000) / 60000
    if family:
        sigma = stats.pstdev(p[family] for p in training)
        increments = [p[family] for p in recent]
        move = mid[family] / base[family] - 1
    else:
        fitted = _fit([(r['a'], r['b']) for r in training])
        if fitted is None:
            return {**out, 'reason': 'zero_variation'}
        out.update(fitted)
        beta = fitted['beta']
        if not c.beta_min <= beta <= c.beta_max or fitted['correlation'] < c.min_correlation:
            return {**out, 'reason': 'unstable_hedge'}
        sigma = fitted['sigma_residual']
        increments = [p['a'] - beta * p['b'] for p in recent]
        move = mid['a'] / base['a'] - 1 - beta * (mid['b'] / base['b'] - 1)
        out.update(sigma_stop_floor=2 * sigma)
    if sigma <= 1e-10 or horizon <= 0:
        return {**out, 'reason': 'zero_variation'}
    if family:
        out.update(sigma=sigma)
    direction = 1 if move > 0 else -1 if move < 0 else 0
    strength = move / (sigma * math.sqrt(horizon))
    breadth = sum(direction * p > 0 for p in increments) / len(increments)
    pressure = pressure_signal(samples, at, family)
    path = [0.0]
    for value in increments:
        path.append(path[-1] + value)
    path.append(move)
    pivot = min(path[-4:]) if direction > 0 else max(path[-4:])
    stop = max(2 * sigma, direction * (move - pivot) + sigma)
    out.update(strength=strength, breadth=breadth, relative_move_bps=move * 10000,
               book_pressure=pressure, stop_return=stop,
               training_end=base['time'], signal_start=selected[c.training_returns + 1]['time'],
               signal_end=selected[-1]['time'], horizon_minutes=horizon)
    if (direction * strength < c.min_strength or breadth < c.min_positive_fraction
            or direction * increments[-1] <= 0):
        return {**out, 'reason': 'weak_or_expired_strength'}
    if confirm_depth and pressure is None:
        return {**out, 'reason': 'book_pressure_history'}
    if confirm_depth and direction * pressure < .05:
        return {**out, 'reason': 'book_pressure_disagrees'}
    return {**out, 'eligible': True, 'direction': direction, 'reason': 'signal'}
