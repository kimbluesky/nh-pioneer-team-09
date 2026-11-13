"""V8 candidate: frozen residual channel, observed turn, executable-cost hurdle.

The historical mean is a price target, not an assertion of cointegration or an
expected profit. Parameters are fixed before replay; replay is development data.
"""
from __future__ import annotations

import math
import statistics as stats
from dataclasses import replace

from backend.strategy.spread_rotation import RotationSettings, _book_prices, _fit, plan_sizes
from backend.strategy.spread_comparison import single_sizes
from backend.strategy.spread_scalp import reference_products

MODEL = 'residual-turn-scalp-v8'
RULES = dict(training_returns=30, channel_closes=20, minimum_z=1.0,
             turn_seconds=10, pivot_seconds=30, turn_sigma=.10,
             stop_buffer_sigma=.25, minimum_net_reward_risk=1.0,
             minimum_gross_target_cost=2.0)


def reversion_settings():
    return replace(RotationSettings(), training_returns=30, signal_returns=20,
                   sizing_mode='max_affordable', min_strength=1, min_correlation=.3,
                   require_both_regimes=False, reward_risk=1,
                   max_cost_fraction_of_risk=1.)


def update_turn_samples(samples, products, at):
    refs = reference_products(products)
    if samples and at - samples[-1]['at'] < 5000:
        return
    if any(p is None or not 0 <= at - p.get('quote_at', 0) <= 5000
           or not _book_prices(p, 1) for p in refs.values()):
        return
    if abs(refs['a']['quote_at'] - refs['b']['quote_at']) > 2000:
        return
    if samples and any(samples[-1][f'{f}_quote'] >= p['quote_at'] for f, p in refs.items()):
        return
    samples.append({'at': at, **{f: sum(_book_prices(p, 1)) / 2 for f, p in refs.items()},
                    **{f'{f}_quote': p['quote_at'] for f, p in refs.items()}})
    del samples[:-12]


def reversion_signal(rows, products, at, samples, c=None, *, family=None, confirm_turn=True):
    c = c or reversion_settings()
    closed = [r for r in rows if r['time'] + 60000 <= at]
    need = RULES['training_returns'] + RULES['channel_closes'] + 1
    out = dict(eligible=False, direction=0, reason='history', family=family,
               model=MODEL, required_closes=need, available_closes=len(closed),
               turn_confirmation=confirm_turn)
    if len(closed) < need:
        return out
    selected = closed[-need:]
    fields = (family,) if family else ('a', 'b')
    if any(not isinstance(r.get(f), (int, float)) or not math.isfinite(r[f]) or r[f] <= 0
           for r in selected for f in fields):
        return {**out, 'reason': 'invalid_price'}
    if any(b['time'] - a['time'] != 60000 for a, b in zip(selected, selected[1:])):
        return {**out, 'reason': 'missing_or_duplicate_minute'}
    if not 0 <= at - selected[-1]['time'] - 60000 <= 90000:
        return {**out, 'reason': 'stale_history'}
    refs = reference_products(products)
    if any(refs[f] is None or not 0 <= at - refs[f].get('quote_at', 0) <= 5000
           or not _book_prices(refs[f], 1) for f in fields):
        return {**out, 'reason': 'stale'}
    if not family and abs(refs['a']['quote_at'] - refs['b']['quote_at']) > 2000:
        return {**out, 'reason': 'stale'}
    current = {f: sum(_book_prices(refs[f], 1)) / 2 for f in fields}
    training = selected[:RULES['training_returns'] + 1]
    beta = 0.
    if family:
        sigma = stats.pstdev(b[family] / a[family] - 1 for a, b in zip(training, training[1:]))
        out['sigma'] = sigma
    else:
        fitted = _fit([(b['a']/a['a']-1, b['b']/a['b']-1) for a, b in zip(training, training[1:])])
        if fitted is None:
            return {**out, 'reason': 'zero_variation'}
        out.update(fitted)
        beta = fitted['beta']
        if not c.beta_min <= beta <= c.beta_max or fitted['correlation'] < c.min_correlation:
            return {**out, 'reason': 'unstable_hedge'}
    anchor = training[-1]
    def level(row):
        return row[family]/anchor[family]-1 if family else row['a']/anchor['a']-1-beta*(row['b']/anchor['b']-1)
    channel = [level(r) for r in selected[RULES['training_returns'] + 1:]]
    center, scale = stats.fmean(channel), stats.pstdev(channel)
    if scale <= 1e-10:
        return {**out, 'reason': 'zero_variation'}
    now = level(current)
    z = (now - center) / scale
    direction = -1 if z > 0 else 1
    factor = current[family or 'a'] / anchor[family or 'a']
    # Convert anchor-relative residuals into entry-relative hedge and distances.
    if not family:
        out.update(beta_fit=beta, beta=beta * (current['b']/anchor['b']) / factor,
                   sigma_stop_floor=2 * fitted['sigma_residual'])
    recent = [s for s in samples if 0 <= at - s['at'] <= RULES['pivot_seconds'] * 1000]
    contiguous = len(recent) >= 3 and recent[-1]['at'] - recent[-3]['at'] >= 10000
    contiguous = contiguous and all(b['at'] - a['at'] <= 8000 for a, b in zip(recent, recent[1:]))
    levels = [level(r) for r in recent] + [now]
    extreme = min(levels) if direction > 0 else max(levels)
    tick = sum((refs[f]['tick']/anchor[f]) * (beta if f == 'b' and not family else 1) for f in fields)
    rebound = direction * (now - extreme)
    turn_required = max(2 * tick, RULES['turn_sigma'] * scale)
    buffer = max(2 * tick, RULES['stop_buffer_sigma'] * scale)
    stop = (max(0., rebound) + buffer) / factor
    target = abs(center - now) / factor
    turned = contiguous and rebound >= turn_required and direction * (now - level(recent[-3])) > 0
    # New exchange-stamped opportunities can occur within a minute. After an
    # exit the engine requires a NEW excursion; a persistent quote cannot churn.
    out.update(direction=direction, strength=z, channel_center=center, channel_level=now,
               channel_sigma=scale, relative_move_bps=(now-center)*10000,
               stop_return=stop, target_return=target, turn_bps=rebound/factor*10000,
               required_turn_bps=turn_required/factor*10000, turn_confirmed=turned,
               training_end=training[-1]['time'], signal_start=selected[-20]['time'],
               signal_end=min(refs[f]['quote_at'] for f in fields), horizon_minutes=20)
    if abs(z) < RULES['minimum_z']:
        return {**out, 'reason': 'inside_channel'}
    if confirm_turn and not contiguous:
        return {**out, 'reason': 'turn_history'}
    if confirm_turn and not turned:
        return {**out, 'reason': 'turn_wait'}
    return {**out, 'eligible': True, 'reason': 'signal'}


def reversion_sizes(signal, products, *, at, c, session_net=0):
    """Same audited max-margin sizing; hurdle uses an observed target, not R*1.5."""
    planner = single_sizes if signal.get('family') else plan_sizes
    plan = planner(signal, products, at=at, c=c, session_net=session_net)
    chosen = plan.get('chosen')
    if chosen is None or 'target_return' not in signal:
        return plan
    notional = chosen.get('leg', chosen.get('leg_a'))['notional']
    gross = notional * signal['target_return']
    cost = chosen['estimated_round_trip_cost']
    # Integer hedge mismatch increases downside risk in the inherited planner.
    # Reserve the same allowance against the target; it is not free alpha.
    hedge_allowance = 0.
    if not signal.get('family'):
        a, b = chosen['leg_a']['notional'], chosen['leg_b']['notional']
        hedge_allowance = 3 * signal['sigma_b'] * abs(b - signal['beta'] * a)
    target = gross - cost - hedge_allowance
    rr = target / chosen['risk'] if chosen['risk'] > 0 else 0.
    acceptable = (gross >= RULES['minimum_gross_target_cost'] * cost
                  and rr >= RULES['minimum_net_reward_risk'])
    chosen.update(target_net=target, target_gross=gross, target_return=signal['target_return'],
                  net_reward_risk=rr, cost_acceptable=acceptable, hedge_target_allowance=hedge_allowance)
    plan.update(research_entry_allowed=signal['eligible'] and acceptable,
                reason='indicative_plan' if acceptable else 'target_below_cost_and_risk')
    return plan
