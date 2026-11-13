from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
import threading

import pytest

from backend.nh_api.client import BusinessError
from backend.strategy import runs, paper, service as service_lib
from backend.strategy.blend_mock import BlendMock, ContinuousMockService
from backend.strategy.blend_recovery import KST
from backend.strategy.journal import Journal
from backend.strategy.spread import SpreadConfig, LegConfig, leg_draft
from backend.strategy.spread_blend import RULES, blend_settings

AT = int(datetime(2026, 11, 13, 15, 0, tzinfo=KST).timestamp() * 1000)


class Broker:
    """A broker which accepts before its reply is lost, or rejects before acceptance."""
    def __init__(self):
        self.orders = []
        self.signed = 0
        self.fail = None
        self.calls = 0
        self.read_fails = 0
        self.cancel_calls = 0

    def order(self, **wanted):
        self.calls += 1
        if self.fail == 'reject':
            self.fail = None
            raise BusinessError('overseas_order', 'TEST', 'Known rejection', {})
        n = str(1000 + len(self.orders))
        self.orders.append(dict(id=n, code=wanted['run'].code, side=wanted['side'],
            quantity=wanted['quantity'], filled=0, remaining=wanted['quantity'], status='accepted',
            orderKind='new', orderType=wanted['order_type'], receivedDate='2026-11-13', receivedTime='15:00:00'))
        if self.fail == 'lost_reply':
            self.fail = None
            raise TimeoutError('Response lost after acceptance')
        return {'id': n}

    def fill(self, cumulative, average=28000.):
        order = self.orders[-1]
        self.signed += (1 if order['side'] == paper.BUY else -1) * (cumulative-order['filled'])
        order.update(filled=cumulative, remaining=order['quantity']-cumulative,
            avgFillPrice=average, status='filled' if cumulative==order['quantity'] else 'partial')

    def history(self, run):
        if self.read_fails:
            self.read_fails -= 1
            raise ConnectionError('Disconnected')
        return deepcopy(self.orders)

    def snapshot(self, run):
        return {'orders':[deepcopy(o) for o in self.orders if o['remaining']],
                'positions':([dict(code=run.code, quantity=abs(self.signed),
                    side='long' if self.signed>0 else 'short')] if self.signed else [])}

    def cancel(self, **kw):
        self.cancel_calls += 1
        return {'id': 'cancel'}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    broker = Broker()
    monkeypatch.setattr(service_lib, 'trading_day_of', lambda *a: None)
    svc = ContinuousMockService(store=runs.RunStore(tmp_path/'runs.json'),
        journal=Journal(tmp_path/'events.jsonl'), account=broker.snapshot,
        history=broker.history, sender=broker, environment=lambda:'paper', outcome=lambda *a:None)
    svc.instrument=lambda *a:dict(tick=.25, daysLeft=30, margin=4634)
    svc._row=lambda run_id,engine=None:{'state':(engine or svc._engine(run_id)).run.state}
    cfg=SpreadConfig(name='fixed', leg_a=LegConfig(code='MNQZ26'), leg_b=LegConfig(code='MESZ26'))
    draft=leg_draft(cfg,cfg.leg_a);draft['aux'].update(dailyLoss='',lossStreak='')
    run=runs.Run(id='owned-a',version_id='test',code='MNQZ26',market='overseas',session='overseas',
        mode=runs.SIM,environment='paper',approved_at=AT,started_at=AT,state=runs.ARMED,
        controller='spread',cash=100000,qty=2,multiplier=2,fee_per_contract=2,draft=draft)
    engine=svc._stand(run);engine.feed='live';engine.book.last_price=28000.
    return broker,svc,engine


def test_lost_new_reply_recovers_its_fill_without_resubmitting(setup):
    broker,svc,e=setup;broker.fail='lost_reply'
    assert e._send(paper.BUY,2,28000.,'entry',AT) is None
    assert e.run.state==runs.ATTENTION and len(e.book.uncertain)==1
    broker.fill(2,28001.)
    assert svc.recover(e.run.id,AT+10000)
    assert broker.calls==1 and not e.book.uncertain
    assert e.ledger.position.quantity==2 and e.ledger.position.avg_price==28001.
    assert sum(f['quantity'] for f in e.fills)==2
    assert svc.recover(e.run.id,AT+12000)
    assert broker.calls==1 and sum(f['quantity'] for f in e.fills)==2


def test_unknown_ack_is_not_cleared_just_because_account_is_empty(setup):
    broker,svc,e=setup;broker.fail='lost_reply'
    e._send(paper.BUY,2,28000.,'entry',AT)
    broker.orders=[]
    for elapsed in (5,30,120,3600):
        assert not svc.recover(e.run.id,AT+elapsed*1000)
    assert broker.calls==1 and e.book.uncertain and e.run.state==runs.ATTENTION


def test_unknown_ack_requires_unique_matching_current_order(setup):
    broker,svc,e=setup;broker.fail='lost_reply'
    e._send(paper.BUY,2,28000.,'entry',AT)
    old=deepcopy(broker.orders[0]);old.update(id='old',receivedDate='2026-11-12')
    duplicate=deepcopy(broker.orders[0]);duplicate['id']='ambiguous'
    broker.orders += [old,duplicate]
    assert not svc.recover(e.run.id,AT+10000)
    assert not e.book.orders and broker.calls==1
    broker.orders.remove(duplicate)
    # An unrelated old pending order prevents release even after binding the
    # right new order. It is never imported as this strategy's execution.
    assert not svc.recover(e.run.id,AT+15000)
    assert len(e.book.orders)==1 and e.book.orders[0].order_no=='1000'
    assert not e.fills


def test_reconnect_recovers_only_missing_partial_fill_value(setup):
    broker,svc,e=setup
    e._send(paper.BUY,2,28000.,'entry',AT)
    broker.fill(1,28000.)
    e.on_order(dict(broker.orders[-1],fillPrice=28000.),at=AT+1000)
    e.run.move(runs.RECONCILE)
    broker.fill(2,28001.)
    broker.read_fails=2
    assert not svc.recover(e.run.id,AT+5000)
    assert not svc.recover(e.run.id,AT+10000)
    assert svc.recover(e.run.id,AT+20000)
    assert e.ledger.position.quantity==2 and e.ledger.position.avg_price==28001.
    assert [f['price'] for f in e.fills]==[28000.,28002.]
    assert broker.calls==1


def test_known_exit_rejection_can_reconcile_then_retry_without_duplicate(setup):
    broker,svc,e=setup
    e._send(paper.BUY,2,28000.,'entry',AT);broker.fill(2)
    e.on_order(dict(broker.orders[-1],fillPrice=28000.),at=AT+1000)
    broker.fail='reject'
    assert not e.managed_unwind(AT+2000)
    assert e.run.state==runs.ATTENTION and not e.book.uncertain
    assert svc.recover(e.run.id,AT+5000)
    assert not e.managed_unwind(AT+6000)
    assert broker.calls==3  # Entry, rejected exit, successful exit.
    assert not e.managed_unwind(AT+7000) and broker.calls==3
    broker.fill(2)
    assert svc.recover(e.run.id,AT+10000)
    assert e.managed_unwind(AT+11000) and e.ledger.position.quantity==0


def test_lost_exit_reply_uses_exit_intent_and_realized_pnl(setup):
    broker,svc,e=setup
    e._send(paper.BUY,2,28000.,'entry',AT);broker.fill(2)
    e.on_order(dict(broker.orders[-1],fillPrice=28000.),at=AT+1000)
    broker.fail='lost_reply'
    e.managed_unwind(AT)
    broker.fill(2,28010.)
    assert svc.recover(e.run.id,AT+10000)
    assert broker.calls==2 and e.ledger.position.quantity==0
    assert e.fills[-1]['intent']=='exit' and e.ledger.profit(28010.)==32.


def test_old_or_real_orders_cannot_enter_recovery(setup):
    _,svc,e=setup
    e.run.environment='real'
    with pytest.raises(ValueError):svc._broker(e.run)


def test_presentation_rules_are_fixed():
    assert {k:RULES[k] for k in ('lookback','hedge_fraction','entry_z','entry_style',
        'require_common_move','target_sigma','stop_sigma','max_trade_profit','max_trade_loss',
        'max_cost_fraction_of_risk','max_hold_seconds','session_loss','loss_streak_pause')} == dict(
        lookback=120,hedge_fraction=.25,entry_z=1.5,entry_style='turn',require_common_move=True,
        target_sigma=1.5,stop_sigma=.75,max_trade_profit=600,max_trade_loss=600,
        max_cost_fraction_of_risk=.4,max_hold_seconds=0,session_loss=None,loss_streak_pause=False)
    c=blend_settings()
    assert c.cash==100000 and c.extra_ticks_per_side==1 and c.session_loss_fraction==0


def test_three_hour_clock_does_not_reset_or_stop_after_losses(monkeypatch):
    import backend.strategy.blend_mock as module
    now=[AT]
    monkeypatch.setattr(module,'now_ms',lambda:now[0])
    monkeypatch.setattr(module.runtime,'active_environment',lambda:'paper')
    monkeypatch.setattr(module,'blend_signal',lambda *a,**k:{'eligible':False})
    e=[]
    for code in ('MNQZ26','MESZ26'):
        one=SimpleNamespace(lock=threading.RLock(),run=SimpleNamespace(code=code,state=runs.ARMED),
            ledger=SimpleNamespace(position=SimpleNamespace(quantity=0,avg_price=0),profit=lambda p:-3000),
            book=SimpleNamespace(last_price=28000,orders=[],open_orders=[],uncertain=[]),
            managed_unwind=lambda at:True)
        e.append(one)
    run={'id':'three-hours','stop_requested':False,'end_at':AT+10800000,'execution':{'flatten_before_end':60}}
    m=BlendMock(SimpleNamespace(engine=SimpleNamespace(data=run),rows=[]))
    m.ready=True;m.run_id=run['id'];m.legs=SimpleNamespace(engines=lambda:e)
    for sec in (0,60,3600,7200,10739):
        now[0]=AT+sec*1000;m._step()
        assert not m.ended and run['end_at']==AT+10800000
    now[0]=AT+10740000;m._step();assert not m.ended
    now[0]=AT+10800000;m._step();assert m.ended


def test_start_rejects_nonempty_mock_account(monkeypatch):
    import backend.strategy.blend_mock as module
    monkeypatch.setattr(module.runtime,'active_environment',lambda:'paper')
    monkeypatch.setattr(module,'current_account',lambda run:{'orders':[],
        'positions':[{'code':'OTHER','quantity':1}]})
    with pytest.raises(ValueError):BlendMock(SimpleNamespace()).preflight()
