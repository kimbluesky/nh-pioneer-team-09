import asyncio
from copy import deepcopy
import json

import pytest

from backend.strategy.blend_runtime import BlendService
from backend.strategy.blend_mock import BlendMock


def test_reset_clears_every_portfolio_and_mock_without_orders_or_clock(tmp_path, monkeypatch):
    import backend.strategy.blend_runtime as module
    clock=[10000000]
    monkeypatch.setattr(module,'now_ms',lambda:clock[0])
    service=BlendService(tmp_path)
    service.start(1800)
    service.engine.data['enabled']=False
    old_id=service.engine.data['id']
    for arm in service.engine.data['arms']:
        arm.update(net=-1234,realized=-1000,fees=234,curve=[{'elapsed':1,'net':-1234}],
            fills=[{'quantity':2}],trades=[{'filled':True,'net':-1234,'risk':600}])
    service.save()
    old_file=tmp_path/old_id/'state.json';old_bytes=old_file.read_bytes()
    rules=deepcopy(service.engine.data['strategy_rules'])
    service.mock=BlendMock(service)
    service.mock.view.update(net=-300,orders=4,fills=10,completed_trades=1,max_drawdown=400)
    checks=[]
    service.mock.preflight=lambda:checks.append(True) or {'positions':0,'pending_orders':0}

    out=asyncio.run(service.reset());run=out['run']
    assert checks==[True] and run['id']!=old_id
    assert not run['enabled'] and run['session_phase']=='ready'
    assert run['settings']['cash']==100000 and run['execution']['duration_seconds']==10800
    assert run['strategy_rules']==rules and not run['market_curve']
    for arm in run['arms']:
        assert (arm['net'],arm['return_pct'],arm['fees'],arm['completed_trades'],arm['fill_count'],arm['drawdown_pct'])==(0,0,0,0,0,0)
        assert not any(arm[k] for k in ('orders','fills','trades','positions','curve'))
        assert arm['reason_label']=='시작 대기'
    assert not service.mock.ready and service.mock.run_id is None
    assert out['mock']['state']=='ready'
    assert all(out['mock'][k]==0 for k in ('net','orders','fills','completed_trades','max_drawdown'))
    assert old_file.read_bytes()==old_bytes
    assert json.loads((tmp_path/'latest.json').read_text())['id']==run['id']

    restored=BlendService(tmp_path);restored.load()
    assert restored.view()['run']['session_phase']=='ready' and not restored.active()
    clock[0]+=7200000
    started=service.start()['run']
    assert started['started_at']==clock[0] and started['end_at']==clock[0]+10800000
    assert started['session_phase']=='running' and started['enabled']


def test_reset_refuses_active_session(tmp_path):
    service=BlendService(tmp_path);before=service.start()['run']['id']
    with pytest.raises(ValueError):asyncio.run(service.reset())
    assert service.engine.data['id']==before and service.active()


def test_failed_account_check_preserves_session_and_does_not_reset(tmp_path):
    service=BlendService(tmp_path);service.start();service.engine.data['enabled']=False
    before=service.engine.data['id']
    service.mock=BlendMock(service)
    def not_empty():raise ValueError('Account has a pending order')
    service.mock.preflight=not_empty
    with pytest.raises(ValueError):asyncio.run(service.reset())
    assert service.engine.data['id']==before and not service.resetting
