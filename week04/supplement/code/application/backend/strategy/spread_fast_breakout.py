"""Faster entry policy, with separate entry and profit-planning horizons.

Five completed one-minute closes locate an entry; the observed twenty-minute
range sets the price objective. Shortening entry must not pretend that fees
disappear or that a tiny range can pay the two-leg round trip.
"""
from __future__ import annotations
import math
import statistics as stats
from dataclasses import replace

from backend.strategy.spread_rotation import _book_prices, _fit
from backend.strategy.spread_scalp import reference_products
from backend.strategy.spread_reversion import reversion_settings, reversion_sizes

MODEL = 'residual-range-breakout-v10'
RULES = dict(version='fast-entry-v1', training_returns=30, channel_closes=5,
             target_closes=20, confirm_seconds=2, confirm_observations=3,
             sample_seconds=1, decision_seconds=1, cooldown_seconds=5,
             max_chase_fraction=None, stop_buffer_fraction=.10, stop_range_basis='target_closes',
             target_range_multiple=1., minimum_net_reward_risk=1.,
             minimum_gross_target_cost=2., reentry='new_completed_minute')


def fast_settings():
    return replace(reversion_settings(), signal_returns=5)


def update_fast_samples(samples, products, at):
    refs = reference_products(products)
    if samples and at - samples[-1]['at'] < 1000:
        return
    if any(p is None or not 0 <= at-p.get('quote_at',0) <= 5000 or not _book_prices(p,1)
           for p in refs.values()):
        return
    if abs(refs['a']['quote_at']-refs['b']['quote_at']) > 2000:
        return
    # Two receipts of the same exchange snapshot never count as confirmation.
    if samples and any(p['quote_at'] <= samples[-1][f'{f}_quote'] for f,p in refs.items()):
        return
    samples.append({'at':at, **{f:sum(_book_prices(p,1))/2 for f,p in refs.items()},
                    **{f'{f}_quote':p['quote_at'] for f,p in refs.items()}})
    del samples[:-12]


def fast_signal(rows, products, at, samples, c=None, *, family=None, confirm_turn=True):
    c = c or fast_settings()
    ntrain, ntarget, nentry = 30, 20, 5
    need = ntrain + ntarget + 1
    closed = [r for r in rows if r['time']+60000 <= at]
    out = dict(eligible=False, direction=0, reason='history', family=family, model=MODEL,
               required_closes=need, available_closes=len(closed),
               breakout_confirmation=confirm_turn, entry_closes=nentry, target_closes=ntarget,
               confirm_seconds=2)
    if len(closed)<need:
        return out
    selected = closed[-need:]
    fields = (family,) if family else ('a','b')
    if any(not isinstance(r.get(f),(int,float)) or not math.isfinite(r[f]) or r[f]<=0
           for r in selected for f in fields):
        return {**out,'reason':'invalid_price'}
    if any(b['time']-a['time']!=60000 for a,b in zip(selected,selected[1:])):
        return {**out,'reason':'missing_or_duplicate_minute'}
    if not 0 <= at-selected[-1]['time']-60000 <= 90000:
        return {**out,'reason':'stale_history'}
    refs=reference_products(products)
    if any(refs[f] is None or not 0<=at-refs[f].get('quote_at',0)<=5000
           or not _book_prices(refs[f],1) for f in fields):
        return {**out,'reason':'stale'}
    if not family and abs(refs['a']['quote_at']-refs['b']['quote_at'])>2000:
        return {**out,'reason':'stale'}
    current={f:sum(_book_prices(refs[f],1))/2 for f in fields}
    training=selected[:ntrain+1]
    anchor=training[-1]
    beta=0.
    if family:
        sigma=stats.pstdev(b[family]/a[family]-1 for a,b in zip(training,training[1:]))
        if sigma<=1e-10:
            return {**out,'reason':'zero_variation'}
        out['sigma']=sigma
    else:
        fit=_fit([(b['a']/a['a']-1,b['b']/a['b']-1) for a,b in zip(training,training[1:])])
        if fit is None:
            return {**out,'reason':'zero_variation'}
        out.update(fit)
        beta=fit['beta']
        if not c.beta_min<=beta<=c.beta_max or fit['correlation']<c.min_correlation:
            return {**out,'reason':'unstable_hedge'}
        out.update(beta_fit=beta,sigma_stop_floor=2*fit['sigma_residual'])
    def level(row):
        return row[family]/anchor[family]-1 if family else row['a']/anchor['a']-1-beta*(row['b']/anchor['b']-1)
    path=[level(r) for r in selected[-ntarget:]]
    recent_path=path[-nentry:]
    low,high=min(recent_path),max(recent_path)
    width,profit_width=high-low,max(path)-min(path)
    if min(width,profit_width)<=1e-10:
        return {**out,'reason':'zero_variation'}
    now=level(current)
    direction=1 if now>high else -1 if now<low else 0
    boundary=high if direction>0 else low
    factor=current[family or 'a']/anchor[family or 'a']
    if not family:
        out['beta']=beta*(current['b']/anchor['b'])/factor
    distance=max(0.,direction*(now-boundary))
    tick=sum(refs[f]['tick']/anchor[f]*(beta if f=='b' and not family else 1) for f in fields)
    stop=(distance+max(2*tick,.10*profit_width))/factor
    target=max(0.,profit_width-distance)/factor
    recent=[s for s in samples if 0<=at-s['at']<=8000][-3:]
    confirmed_samples = len(recent)==3 and recent[-1]['at']-recent[0]['at']>=2000
    confirmed_samples = confirmed_samples and all(1000<=b['at']-a['at']<=4000
                          and all(b[f'{f}_quote']>a[f'{f}_quote'] for f in fields)
                          for a,b in zip(recent,recent[1:]))
    confirmed=bool(direction and confirmed_samples and all(direction*(level(s)-boundary)>0 for s in recent))
    out.update(direction=direction,channel_low=low,channel_high=high,channel_level=now,
               channel_width=width, target_channel_width=profit_width,
               channel_center=(high+low)/2, strength=(now-(high+low)/2)/(width/2),
               relative_move_bps=(now-(high+low)/2)*10000,
               stop_return=stop,target_return=target,breakout_distance=distance,
               chase_fraction=distance/width,breakout_confirmed=confirmed,
               training_end=training[-1]['time'],signal_start=selected[-nentry]['time'],
               signal_end=selected[-1]['time'],quote_end=min(refs[f]['quote_at'] for f in fields),
               horizon_minutes=nentry)
    if not direction:
        return {**out,'reason':'inside_range'}
    if confirm_turn and not confirmed_samples:
        return {**out,'reason':'turn_history'}
    if confirm_turn and not confirmed:
        return {**out,'reason':'breakout_confirm'}
    # No standalone percentage-chase veto. The executable target/risk hurdle
    # rejects late entries as upside shrinks and invalidation distance grows.
    return {**out,'eligible':True,'reason':'signal'}


fast_sizes = reversion_sizes
