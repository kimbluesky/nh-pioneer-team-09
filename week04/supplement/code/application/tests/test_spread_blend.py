from copy import deepcopy
from dataclasses import replace
import math
import pytest
from backend.strategy.spread_blend import blend_signal,blend_sizes,blend_settings,observed_gap_ok,RULES
from backend.strategy.blend_engine import BlendEngine
from backend.strategy.blend_mock import MockOnlySender,BlendMock,ContinuousMockLimits,ContinuousMockService
from backend.strategy.sim import NotSent
from types import SimpleNamespace

def fixture():
    rows=[dict(time=1700000000000+i*60000,a=20000+i*.5,b=5000+i*.02,va=5000,vb=5000) for i in range(242)]
    rows[-2]['a']+=60;rows[-1]['a']+=55
    at=rows[-1]['time']+60000
    products=[]
    for code,f,m,margin in [('MNQZ26','a',2,4634),('MESZ26','b',5,2935)]:
        x=rows[-1][f]
        products.append(dict(code=code,family=f,multiplier=m,margin=margin,tick=.25,fee=2,
                             quote_at=at,received_at=at,new_capacity=100,capacity_at=at,
                             bids=[dict(price=x-.125,quantity=100)],asks=[dict(price=x+.125,quantity=100)]))
    return rows,products,at

def test_completed_bars_only_and_partial_hedge_signal():
    rows,p,at=fixture();s=blend_signal(rows,p,at)
    assert s['eligible'] and s['direction']==-1 and s['beta']==.25
    future=dict(rows[-1],time=at,a=1.)
    assert blend_signal(rows+[future],p,at)==s
    assert s['target_return']==pytest.approx(2*s['stop_return'])

def test_history_is_api_seeded_and_latest_gap_is_blocked():
    rows,p,at=fixture()
    assert blend_signal(rows,p,at)['available_closes']==RULES['lookback']+2
    rows[-5]['time']-=60000
    assert blend_signal(rows,p,at)['reason']=='missing_or_duplicate_minute'

def test_singles_do_not_depend_on_other_index():
    rows,p,at=fixture();before=blend_signal(rows,p,at,family='a')
    for r in rows:r['b']=1e9
    assert blend_signal(rows,p,at,family='a')==before

def test_combined_budget_and_opposite_quantity_hedge():
    rows,p,at=fixture();s=blend_signal(rows,p,at)
    plan=blend_sizes(s,p,at=at)['chosen'];assert plan
    assert plan['margin']+plan['cost']<=100000
    assert plan['leg_a']['quantity']>0 and plan['leg_b']['quantity']>0
    assert plan['hedge_error_pct']<=10
    assert plan['target_net']==pytest.approx(plan['target_gross']-plan['cost'])
    assert 0<plan['target_net']<=600 and 0<plan['risk']<=600
    assert plan['cost']<=.4*plan['risk']
    assert plan['cost_fraction_of_risk']==pytest.approx(plan['cost']/plan['risk'])
    p[1]['new_capacity']=0
    assert blend_sizes(s,p,at=at,broker=True)['chosen'] is None

def test_stale_or_insufficient_depth_cannot_enter():
    rows,p,at=fixture();s=blend_signal(rows,p,at)
    assert blend_sizes(s,p,at=at+6000)['reason']=='stale'
    for one in p:
        one['bids'][0]['quantity']=0;one['asks'][0]['quantity']=0
    assert not blend_sizes(s,p,at=at)['chosen']

def test_three_independent_portfolios_and_opposite_fill_directions():
    rows,p,at=fixture();e=BlendEngine('test',at)
    e.step(rows,p,at);arm=e.data['arms'][0]
    assert len(e.data['arms'])==3 and e.execution.max_hold_seconds==0
    assert arm['trade'] and arm['trade']['expected']['MNQZ26']<0<arm['trade']['expected']['MESZ26']
    for one in p:one['quote_at']=at+2000
    e.step(rows,p,at+2000)
    assert arm['state']=='holding' and len(arm['fills'])==2
    for one in p:
        one['quote_at']=at+3000
        if one['family']=='a':
            for side in ('bids','asks'):one[side][0]['price']-=100
    e.step(rows,p,at+3000)
    assert arm['state']=='exiting' and arm['reason']=='take_profit'
    for one in p:one['quote_at']=at+5000
    e.step(rows,p,at+5000)
    assert arm['trades'][-1]['net']>0 and not any(x['quantity'] for x in arm['positions'].values())

def test_mock_sender_rejects_real_environment_before_network():
    sender=MockOnlySender()
    with pytest.raises(NotSent):
        sender.order(run=SimpleNamespace(mode='real',environment='real',code='MNQZ26'))

def test_maintenance_gap_keeps_observed_bars_but_not_arbitrary_outages():
    from datetime import datetime,timezone,timedelta
    def stamp(h,m):return int(datetime(2026,11,13,h,m,tzinfo=timezone(timedelta(hours=9))).timestamp()*1000)
    assert observed_gap_ok({'time':stamp(6,59)},{'time':stamp(8,0)})
    assert observed_gap_ok({'time':stamp(7,0)},{'time':stamp(8,1)})
    assert not observed_gap_ok({'time':stamp(14,59)},{'time':stamp(16,0)})

def test_thirty_minute_run_has_one_minute_cleanup(tmp_path):
    from backend.strategy.blend_runtime import BlendService
    s=BlendService(tmp_path);x=s.start(1800)['run']
    assert x['end_at']-x['started_at']==1800000
    assert s.engine.execution.flatten_before_end==60 and s.engine.execution.warmup_seconds==0

def test_turn_requires_one_point_five_sigma_and_prior_bar_reversal():
    import statistics
    rows,p,at=fixture()
    for r in rows:r['b']=5000.
    path=[math.log(r['a']/rows[0]['a']) for r in rows]
    history=path[-RULES['lookback']-1:-1]
    x=statistics.fmean(history)+1.75*statistics.pstdev(history)
    rows[-1]['a']=rows[0]['a']*math.exp(x)
    s=blend_signal(rows,p,at)
    assert s['eligible'] and s['direction']==-1 and s['signal_source']=='turn'
    assert s['z']==pytest.approx(1.75)
    rows[-1]['a']=rows[0]['a']*math.exp(statistics.fmean(history)+1.25*statistics.pstdev(history))
    assert not blend_signal(rows,p,at)['eligible']


def test_entry_cost_leaves_sixty_percent_of_stop_for_price_risk():
    rows,p,at=fixture();s=blend_signal(rows,p,at)
    s.update(target_return=.01,stop_return=.005)
    # Even when maximum size fits the cash budget, expensive book liquidity
    # must not consume the net stop at the instant both legs fill.
    for one in p:
        mid=(one['bids'][0]['price']+one['asks'][0]['price'])/2
        one['bids'][0]['price']=mid-2
        one['asks'][0]['price']=mid+2
    plan=blend_sizes(s,p,at=at)['chosen']
    assert plan and plan['cost']<=plan['risk']*.4
    disabled=blend_sizes(s,p,at=at,c=replace(blend_settings(),max_cost_fraction_of_risk=1.))['chosen']
    assert disabled['gross_notional']>plan['gross_notional']

def test_losses_do_not_disable_subsequent_paper_entries():
    rows,p,at=fixture();e=BlendEngine('losses',at);arm=e.data['arms'][0]
    arm.update(realized=-3500.,net=-3500.,loss_locked=True)
    e.step(rows,p,at)
    assert arm['state']=='entering' and not arm['loss_locked']
    assert arm['trade']['starting_net']==-3500.
    assert arm['trade']['risk']>0
    assert e.data['enabled'] and RULES['session_loss'] is None

def test_three_hour_duration_and_loss_policy(tmp_path):
    from backend.strategy.blend_runtime import BlendService
    s=BlendService(tmp_path);x=s.start()['run']
    assert x['end_at']-x['started_at']==10800000
    assert s.engine.execution.entry_cutoff_before_end==60
    assert s.engine.settings.session_loss_fraction==0
    assert ContinuousMockLimits().read('paper')=={}

def test_mock_loss_does_not_end_run(monkeypatch):
    import threading
    from backend.strategy import runs
    from backend.strategy import blend_mock as module
    rows,p,at=fixture()
    # No signal here: this tests that a prior loss does not start a liquidation
    # cycle or disable subsequent evaluations.
    monkeypatch.setattr(module,'now_ms',lambda:at)
    monkeypatch.setattr(module.runtime,'active_environment',lambda:'paper')
    monkeypatch.setattr(module,'blend_signal',lambda *a,**kw:{'eligible':False,'reason':'reversal_wait'})
    engines=[]
    for f,code in [('a','MNQZ26'),('b','MESZ26')]:
        engines.append(SimpleNamespace(lock=threading.RLock(),
            run=SimpleNamespace(code=code,state=runs.ARMED),
            ledger=SimpleNamespace(position=SimpleNamespace(quantity=0,avg_price=0),profit=lambda mark:-1500.),
            book=SimpleNamespace(last_price=20000.,orders=[],open_orders=[])))
    forward=SimpleNamespace(engine=SimpleNamespace(data={'id':'test','stop_requested':False,
        'end_at':at+10800000,'execution':{'flatten_before_end':60}}),rows=rows)
    m=BlendMock(forward);m.ready=True;m.run_id='test';m.products=p
    m.legs=SimpleNamespace(engines=lambda:engines)
    m._step()
    assert m.view['net']==-3000. and m.view['state']=='watching'
    assert not m.ended and m.close_reason is None
    m.carry={'net':-149.5,'orders':4,'fills':46}
    m._step()
    assert m.view['net']==-3149.5 and m.view['orders']==4 and m.view['fills']==46

def test_disabling_daily_loss_only_relaxes_that_field_for_mock_coordinator(monkeypatch):
    from backend.strategy import runs
    from backend.strategy.service import RunService
    findings=[{'field':'aux.dailyLoss'},{'field':'code'}]
    monkeypatch.setattr(RunService,'start_problems',lambda self,**kw:findings)
    service=ContinuousMockService()
    kw=dict(managed=True,mode=runs.SIM,environment='paper',draft={'aux':{'dailyLoss':''}})
    assert service.start_problems(**kw)==[{'field':'code'}]
    assert service.start_problems(**{**kw,'environment':'real'})==findings

def test_new_mock_run_rebinds_ended_coordinator_without_resetting_running_orders():
    import asyncio
    from backend.strategy import runs
    stopped=[]
    class Pump:
        async def stop(self):stopped.append(True)
    e=SimpleNamespace(run=SimpleNamespace(state=runs.ARMED),
        ledger=SimpleNamespace(position=SimpleNamespace(quantity=0)),book=SimpleNamespace(open_orders=[]))
    m=BlendMock(SimpleNamespace());m.legs=SimpleNamespace(engines=lambda:[e]);m.ready=True;m.ended=True;m.pump=Pump()
    e.book.open_orders=[object()]
    assert not asyncio.run(m._change_run())
    assert not stopped and m.ready
    e.book.open_orders=[]
    assert asyncio.run(m._change_run()) and stopped
    assert not m.ready and not m.ended and m.run_id is None
