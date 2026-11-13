from copy import deepcopy

import pytest

from backend.strategy import runs, paper, service as service_lib
from backend.strategy.blend_mock import BlendMock, ContinuousMockService
from backend.strategy.blend_runtime import BlendService
from backend.strategy.journal import Journal
from backend.strategy.spread import SpreadConfig, LegConfig, leg_draft


AT = 1794549600000


class Sender:
    def __init__(self):
        self.orders = []
        self.cancels = []

    def order(self, **kw):
        row = dict(id=str(1000 + len(self.orders)), code=kw['run'].code,
            side=kw['side'], quantity=kw['quantity'], filled=0,
            remaining=kw['quantity'], status='accepted')
        self.orders.append(row)
        return {'id': row['id']}

    def cancel(self, **kw):
        self.cancels.append(kw['order'].order_no)
        return {'id': 'cancel'}

    def fill(self, engine, index, cumulative, price):
        row = self.orders[index]
        row.update(filled=cumulative, remaining=row['quantity']-cumulative,
            avgFillPrice=price, status='filled' if cumulative==row['quantity'] else 'partial')
        engine.on_order(dict(row, fillPrice=price), at=AT+1000)


@pytest.fixture
def pair(tmp_path, monkeypatch):
    import backend.strategy.blend_mock as mock_module
    import backend.strategy.blend_runtime as runtime_module
    monkeypatch.setattr(mock_module, 'now_ms', lambda: AT+2000)
    monkeypatch.setattr(runtime_module, 'now_ms', lambda: AT)
    monkeypatch.setattr(mock_module.runtime, 'active_environment', lambda: 'paper')
    monkeypatch.setattr(mock_module.runtime, 'replaying', lambda: False)
    monkeypatch.setattr(service_lib, 'trading_day_of', lambda *a: None)
    sender = Sender()
    legs = ContinuousMockService(store=runs.RunStore(tmp_path/'legs.json'),
        journal=Journal(tmp_path/'leg-events.jsonl'), sender=sender,
        account=lambda run: {'orders': [], 'positions': []}, history=lambda run: [],
        environment=lambda: 'paper', outcome=lambda *a: None)
    legs.instrument = lambda *a: dict(tick=.25, daysLeft=30, margin=4634)
    legs._row = lambda run_id, engine=None: {'state': (engine or legs._engine(run_id)).run.state}
    config = SpreadConfig(name='fixed', leg_a=LegConfig(code='MNQZ26'), leg_b=LegConfig(code='MESZ26'))
    engines = []
    for leg, qty, multiplier, price, side in ((config.leg_a, 2, 2, 28000., paper.BUY),
                                             (config.leg_b, 3, 5, 7000., paper.SELL)):
        draft = leg_draft(config, leg)
        draft['aux'].update(dailyLoss='', lossStreak='')
        run = runs.Run(id=leg.code, version_id='test', code=leg.code, market='overseas',
            session='overseas', mode=runs.SIM, environment='paper', approved_at=AT,
            started_at=AT, state=runs.ARMED, controller='spread', cash=100000,
            qty=qty, multiplier=multiplier, fee_per_contract=2, draft=draft)
        engine = legs._stand(run)
        engine.feed='live';engine.book.last_price=price
        engine._send(side, qty, price, 'entry', AT)
        engines.append(engine)
    forward=BlendService(tmp_path/'forward');forward.start()
    mock=BlendMock(forward);forward.mock=mock
    mock.legs=legs;mock.ready=True;mock.run_id=forward.engine.data['id']
    mock.last_signal=AT-60000
    mock.trade=dict(at=AT, expected={'MNQZ26': 2, 'MESZ26': -3}, base=0,
        risk=600, target=600, fills_before=0)
    return forward, mock, sender, engines


def test_manual_close_flattens_both_legs_once_and_keeps_session(pair):
    forward, mock, sender, engines=pair
    sender.fill(engines[0],0,2,28000.);sender.fill(engines[1],1,3,7000.)
    mock._step()
    before=deepcopy(forward.engine.data)
    assert mock.snapshot()['can_manual_close']
    assert mock.snapshot()['trade_at']==AT
    for _ in range(2):
        out=forward.manual_close(mock.run_id,AT)
        assert out['mock']['close_requested'] and not out['mock']['can_manual_close']
    assert len(sender.orders)==2  # HTTP only queues; execution stays in the coordinator.
    mock._step();mock._step()
    assert [(o['code'],o['side'],o['quantity']) for o in sender.orders[2:]]==[
        ('MNQZ26',paper.SELL,2),('MESZ26',paper.BUY,3)]
    sender.fill(engines[0],2,2,28010.);sender.fill(engines[1],3,3,6990.)
    mock._step()
    assert all(e.ledger.position.quantity==0 for e in engines)
    assert mock.trade is None and mock.close_reason is None
    assert mock.view['completed_trades']==1 and mock.view['last_trade_net']==170.
    assert mock.view['last_exit_reason']=='수동 청산'
    assert not mock.snapshot()['can_manual_close'] and not mock.ended
    assert forward.active() and not forward.engine.data['stop_requested']
    assert forward.engine.data['end_at']==before['end_at']
    assert forward.engine.data['strategy_rules']==before['strategy_rules']
    assert forward.engine.data['arms']==before['arms']  # Independent comparisons stay automatic.
    assert len([e for e in forward.engine.data['events'] if e['kind']=='manual_close_requested'])==1
    assert mock.last_signal==AT-60000


def test_manual_close_cancels_entries_then_closes_late_partial_fills(pair):
    forward,mock,sender,engines=pair
    sender.fill(engines[0],0,1,28000.)
    mock._step();forward.manual_close(mock.run_id,AT)
    mock._step();mock._step()
    assert sender.cancels==['1000','1001'] and len(sender.orders)==2
    # Extra fills can arrive while cancellation is pending. Exit the confirmed
    # quantity, including these fills, instead of the planned or displayed size.
    sender.fill(engines[0],0,2,28000.);sender.fill(engines[1],1,1,7000.)
    engines[1].book.close_cancelled(engines[1].book.orders[0],by='test')
    mock._step();mock._step()
    assert [(o['side'],o['quantity']) for o in sender.orders[2:]]==[(paper.SELL,2),(paper.BUY,1)]
    sender.fill(engines[0],2,2,28010.);sender.fill(engines[1],3,1,6990.)
    mock._step()
    assert not mock.close_reason and mock.view['completed_trades']==1
    assert all(not e.ledger.position.quantity and not e.book.open_orders for e in engines)


def test_manual_close_rejects_stale_empty_or_real_requests(pair,monkeypatch):
    import backend.strategy.blend_mock as module
    forward,mock,sender,engines=pair
    with pytest.raises(ValueError):forward.manual_close(mock.run_id,AT)  # No fill yet.
    sender.fill(engines[0],0,2,28000.);sender.fill(engines[1],1,3,7000.)
    with pytest.raises(ValueError):forward.manual_close('older-run',AT)
    with pytest.raises(ValueError):forward.manual_close(mock.run_id,AT-1)
    monkeypatch.setattr(module.runtime,'active_environment',lambda:'real')
    with pytest.raises(ValueError):forward.manual_close(mock.run_id,AT)
    assert mock.close_reason is None and len(sender.orders)==2


def test_manual_close_waits_for_reconciliation_and_does_not_end_session(pair):
    forward,mock,sender,engines=pair
    sender.fill(engines[0],0,2,28000.);sender.fill(engines[1],1,3,7000.)
    mock._step();engines[0].run.state=runs.ATTENTION
    forward.manual_close(mock.run_id,AT);mock._step()
    assert mock.close_reason=='수동 청산' and mock.view['state']=='recovering'
    assert len(sender.orders)==3 and sender.orders[2]['code']=='MESZ26'
    assert forward.active() and not mock.ended
    with pytest.raises(ValueError):forward.manual_close(mock.run_id,AT+1)
