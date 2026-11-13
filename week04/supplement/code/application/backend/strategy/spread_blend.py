"""Exploratory partial-hedge reversal with a round-trip cost buffer.

This is an in-sample selected hypothesis, not independently validated alpha.
Signals use completed bars, exits use net P&L, and both legs share $100,000.
"""
from __future__ import annotations
import math
import statistics as stats
from dataclasses import replace
from datetime import datetime,timezone,timedelta
from backend.strategy.spread_rotation import RotationSettings, _book_prices

MODEL = 'partial-hedge-blend-v15'
RULES = dict(version=MODEL, revision='cost-buffer-v5', lookback=120, hedge_fraction=.25, entry_z=1.5,
             entry_style='turn', require_common_move=True,
             target_sigma=1.5, stop_sigma=.75, failed_breakout_bars=6,
             common_move_bars=5, minimum_target_cost=1.25,
             max_trade_loss=600, max_trade_profit=600, session_loss=None, loss_streak_pause=False,
             max_cost_fraction_of_risk=.4,
             history_source='completed API minutes', selection='observed_quote_replay_development',
             max_hold_seconds=0, cash=100000, margin_budget='combined_pair',
             cost_ticks_per_side=1, fee_per_contract_side=2)
ALLOWED = {'MNQZ26': ('a', 2), 'MESZ26': ('b', 5)}

def observed_gap_ok(a,b):
    gap=b['time']-a['time']
    if gap==60000:return True
    # An exchange maintenance break contains no minute bars. It is not padded
    # and not confused with an arbitrary intraday data outage.
    x=datetime.fromtimestamp(a['time']/1000,timezone(timedelta(hours=9)))
    y=datetime.fromtimestamp(b['time']/1000,timezone(timedelta(hours=9)))
    return gap in (3600000,3660000) and x.minute in (0,59) and y.minute in (0,1) and (x.hour,y.hour) in ((5,7),(6,7),(6,8),(7,8))

def blend_settings():
    return replace(RotationSettings(), sizing_mode='max_affordable', cash=100000,
                   margin_fraction=1., extra_ticks_per_side=1., max_contracts=100,
                   session_loss_fraction=0., hedge_error_limit=.1,
                   max_cost_fraction_of_risk=RULES['max_cost_fraction_of_risk'])

def blend_signal(rows, products, at, samples=None, c=None, *, family=None, confirm_turn=True):
    window=RULES['lookback'];hedge=RULES['hedge_fraction'];required=window+2
    closed=[r for r in rows if r['time']+60000<=at][-required:]
    out=dict(eligible=False, direction=0, reason='history', model=MODEL, family=family,
             required_closes=required, available_closes=len(closed), beta=hedge if not family else None)
    if len(closed)<required:return out
    if not 0<=at-closed[-1]['time']-60000<=90000:return {**out,'reason':'stale_history'}
    # Keep observed bars across the scheduled daily exchange break; never fill
    # missing bars with invented prices. The trigger's last 12 minutes must align.
    if any(not observed_gap_ok(a,b) for a,b in zip(closed[-12:],closed[-11:])):
        return {**out,'reason':'missing_or_duplicate_minute'}
    if any(not observed_gap_ok(a,b) for a,b in zip(closed,closed[1:])):
        return {**out,'reason':'missing_or_duplicate_minute'}
    fields=(family,) if family else ('a','b')
    if any(not isinstance(r.get(f),(int,float)) or not math.isfinite(r[f]) or r[f]<=0 for r in closed for f in fields):
        return {**out,'reason':'invalid_price'}
    anchor=closed[0]
    path=[math.log(r[family]/anchor[family]) if family else
          math.log(r['a']/anchor['a'])-hedge*math.log(r['b']/anchor['b']) for r in closed]
    previous=path[-window-1:-1];mu=stats.fmean(previous);sd=stats.pstdev(previous)
    if sd<=1e-10:return {**out,'reason':'zero_variation'}
    x,prior=path[-1],path[-2];z=(x-mu)/sd
    turn=-1 if z>=RULES['entry_z'] and x<prior else 1 if z<=-RULES['entry_z'] and x>prior else 0
    old=path[-RULES['failed_breakout_bars']-2:-2]
    fail=-1 if prior>max(old) and min(old)<=x<max(old) and x>mu else (
         1 if prior<min(old) and min(old)<x<=max(old) and x<mu else 0)
    direction=turn if RULES.get('entry_style')=='turn' else turn or fail
    ra=closed[-1]['a']/closed[-6]['a']-1 if not family else 0.
    rb=closed[-1]['b']/closed[-6]['b']-1 if not family else 0.
    common=family is not None or ra*rb>=0
    out.update(direction=direction, strength=z, z=z, sigma=sd, common_move=common,
               signal_end=closed[-1]['time'], signal_start=closed[-12]['time'],
               training_end=closed[-2]['time'], signal_source='turn' if turn else ('waiting' if RULES['entry_style']=='turn' else 'failed_breakout'),
               target_return=RULES['target_sigma']*sd, stop_return=RULES['stop_sigma']*sd, last_volume={f:closed[-1].get('v'+f) for f in fields},
               horizon_minutes=window, mean=mu, relative_move_bps=(x-mu)*10000)
    if not direction:return {**out,'reason':'reversal_wait'}
    if not common and confirm_turn and RULES.get('require_common_move',True):return {**out,'reason':'common_move_wait'}
    return {**out,'eligible':True,'reason':'signal'}

def blend_sizes(signal, products, *, at, c=None, session_net=0., broker=False):
    c=c or blend_settings()
    out={'chosen':None,'reason':signal.get('reason','history')}
    if not signal.get('eligible'):return out
    family=signal.get('family');fields=(family,) if family else ('a','b')
    refs={p['family']:p for p in products if p['code'] in ALLOWED}
    if any(f not in refs for f in fields):return {**out,'reason':'no_reference'}
    refs={f:refs[f] for f in fields}
    if any(not 0<=at-p.get('quote_at',0)<=5000 for p in refs.values()) or max(p['quote_at'] for p in refs.values())-min(p['quote_at'] for p in refs.values())>2000:
        return {**out,'reason':'stale'}
    if any(not isinstance(p.get('margin'),(int,float)) or p['margin']<=0 for p in refs.values()):
        return {**out,'reason':'verified_margin_and_fresh_capacity_required'}
    cash=min(c.cash,c.cash+session_net)
    limits={};mids={}
    for f,p in refs.items():
        prices=_book_prices(p,1);volume=signal['last_volume'].get(f)
        if not prices or not isinstance(volume,(int,float)) or volume<0:return {**out,'reason':'history'}
        mids[f]=sum(prices)/2
        limit=min(int(cash//p['margin']),int(volume*.05),c.max_contracts)
        if broker:
            if not isinstance(p.get('new_capacity'),int) or not 0<=at-p.get('capacity_at',0)<=30000:
                return {**out,'reason':'verified_margin_and_fresh_capacity_required'}
            limit=min(limit,p['new_capacity'])
        limits[f]=max(0,limit)
    if family:choices=[{family:q} for q in range(1,limits[family]+1)]
    else:
        ratio=signal['beta']*2*mids['a']/(5*mids['b']);choices=[]
        for qa in range(1,limits['a']+1):
            qb=round(qa*ratio)
            if 0<qb<=limits['b'] and abs(qb-qa*ratio)<=.1*qa*ratio:choices.append({'a':qa,'b':qb})
    best=None
    for qty in choices:
        margin=cost=gross=0.;routes={};valid=True
        for f,q in qty.items():
            p=refs[f];book=_book_prices(p,q)
            if not book:valid=False;break
            bid,ask=book
            cost+=q*(2*p['fee']+(ask-bid+2*c.extra_ticks_per_side*p['tick'])*p['multiplier'])
            margin+=q*p['margin'];notional=q*p['multiplier']*mids[f];gross+=notional
            routes[f]={'tickets':[{'code':p['code'],'quantity':q}], 'code':p['code'],
                       'quantity':q,'equivalent_micro_quantity':q,'notional':notional,
                       'margin':q*p['margin'],'multiplier':p['multiplier'],'family':f}
        if not valid or margin+cost>cash:continue
        dollars=mids[family or 'a']*refs[family or 'a']['multiplier']*qty[family or 'a']
        target=min(dollars*signal['target_return'],RULES['max_trade_profit']+cost)
        if target<1.25*cost:continue
        risk=min(RULES['max_trade_loss'],dollars*signal['stop_return']+cost)
        # The round-trip friction must leave room for an adverse market move.
        # Previously a $190 cost estimate was allowed with a $200 net stop,
        # causing ordinary entry friction to consume almost the entire stop.
        if cost>c.max_cost_fraction_of_risk*risk:continue
        chosen={'margin':margin,'cost':cost,'round_trip_cost':cost,'gross_notional':gross,
                'risk':risk,'target_net':target-cost,
                'estimated_round_trip_cost':cost,'cost_fraction_of_risk':cost/max(1.,risk),
                'target_gross':target,'net_reward_risk':(target-cost)/max(1.,risk),
                'cost_acceptable':True,'available_cash':cash,'margin_utilization_pct':100*margin/cash,
                'hedge_error_pct':0. if family else abs(qty['b']-qty['a']*ratio)/(qty['a']*ratio)*100}
        if family:chosen['leg']=routes[family]
        else:chosen.update(leg_a=routes['a'],leg_b=routes['b'])
        if best is None or chosen['gross_notional']>best['gross_notional']:best=chosen
    return {'chosen':best,'reason':'signal' if best else 'cost_depth_or_budget'}
