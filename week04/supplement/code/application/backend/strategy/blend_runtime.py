"""Fresh quote forward run, separate from prior research ledgers."""
from __future__ import annotations
import asyncio, json, uuid
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from backend import quotes
from backend.nh_api import runtime
from backend.overseas_futureoption.overseas_futureoption_functions import chart_minute
from backend.strategy.rotation_runtime import RotationService, now_ms, atomic_json, KST, ROOT
from backend.strategy.breakout_runtime import BreakoutRuntime
from backend.strategy.rotation_engine import fingerprint,ExecutionSettings
from backend.strategy.blend_engine import BlendEngine
from backend.strategy.spread_blend import MODEL

OUT=ROOT/'week04/results/blend-forward-v15'
SOURCES=[Path(__file__).with_name(n) for n in ('spread_blend.py','blend_engine.py','blend_runtime.py','blend_mock.py','blend_recovery.py','blend_app.py','rotation_engine.py','paper.py')]

class BlendService(RotationService):
    def __init__(self,folder=OUT):
        super().__init__(folder)
        self.mock=None
        self.resetting=False

    def save(self):
        if self.engine and self.mock and self.mock.run_id==self.engine.data['id']:
            self.engine.data['mock_summary']=self.mock.snapshot()
        super().save()

    def load(self):
        with self.lock:
            pointer=self.folder/'latest.json'
            if not pointer.exists():return
            rid=json.loads(pointer.read_text(encoding='utf-8'))['id']
            if not rid.isalnum():raise ValueError('Invalid run ID')
            saved=json.loads((self.folder/rid/'state.json').read_text(encoding='utf-8'))
            self.engine=BlendEngine.restore(saved,now_ms())
            if saved.get('sources')!=fingerprint(SOURCES):self.engine.data['entry_lock']=True
            self.save()

    def start(self,duration_seconds=10800):
        if duration_seconds not in (1800,10800):raise ValueError('Choose 30 minutes or 3 hours')
        if self.external_owner:raise ValueError('This run already has a process owner')
        if runtime.replaying():raise ValueError('Fresh NH quotes required')
        with self.lock:
            if self.resetting:raise ValueError('대회 초기화가 진행 중입니다')
            if self.engine and self.engine.data['enabled']:return self.view()
            if self.mock and not self.mock.can_start_new():
                raise ValueError('이전 모의 운용의 보유·주문 정리를 기다려 주세요')
            account_check=self.mock.preflight() if self.mock else None
            at=now_ms()
            cleanup=60
            execution=replace(ExecutionSettings(),duration_seconds=duration_seconds,warmup_seconds=0,
                              max_hold_seconds=0,cooldown_seconds=5,entry_cutoff_before_end=cleanup,
                              flatten_before_end=cleanup)
            self.engine=BlendEngine(uuid.uuid4().hex,at,execution=execution)
            self.engine.data['sources']=fingerprint(SOURCES)
            self.engine.data['selection_note']='발표 전략 고정 · 120분 평균회귀 · 익절·손절 최대 600달러 · 비용 40% 이내'
            self.engine.data['live_exit_policy']='진입 시 고정한 순손익 익절·손절 · 마지막 60초 잔여 정리'
            self.engine.data['competition_locked']=True
            self.engine.data['session_phase']='running'
            self.engine.data['account_preflight']=account_check
            self.engine.event('session_started',at,model=MODEL)
            atomic_json(self.folder/'latest.json',{'id':self.engine.data['id']})
            self.save();return self.view()

    def manual_close(self,run_id,trade_at):
        with self.lock:
            if self.external_owner:raise ValueError('다른 운용 서버가 실행 중입니다')
            if self.resetting or not self.mock:raise ValueError('모의계좌 연결을 확인 중입니다')
            if self.mock.request_manual_close(run_id,trade_at):
                self.engine.event('manual_close_requested',now_ms(),trade_at=trade_at,
                    reason='수동 청산',execution='mock',automatic_trading_continues=True)
                self.save()
            return self.view()

    async def reset(self):
        """Create an empty $100k competition session without starting its clock.

        The former session remains archived; the active pointer and all visible
        counters move to a new empty run. Broker balances are never rewritten.
        """
        with self.lock:
            if self.external_owner:raise ValueError('다른 운용 서버가 실행 중입니다')
            if self.resetting:raise ValueError('대회 초기화가 진행 중입니다')
            if self.engine and self.engine.data['enabled']:
                raise ValueError('운용을 중지하고 잔여 보유를 정리한 뒤 초기화하세요')
            if self.mock and not self.mock.can_start_new():
                raise ValueError('모의 주문·보유 확인이 끝난 뒤 초기화할 수 있습니다')
            self.resetting=True
        try:
            account_check=await asyncio.to_thread(self.mock.preflight) if self.mock else None
            if self.mock and not await self.mock._change_run():
                raise ValueError('이전 모의 주문·보유가 남아 있습니다')
            with self.lock:
                at=now_ms()
                execution=replace(ExecutionSettings(),duration_seconds=10800,warmup_seconds=0,
                    max_hold_seconds=0,cooldown_seconds=5,entry_cutoff_before_end=60,flatten_before_end=60)
                self.engine=BlendEngine(uuid.uuid4().hex,at,execution=execution)
                self.engine.data.update(enabled=False,session_phase='ready',competition_locked=True,
                    sources=fingerprint(SOURCES),account_preflight=account_check)
                for arm in self.engine.data['arms']:
                    arm.update(state='ready',reason='ready')
                self.products=[];self.rows=[];self.limits={}
                self.feed_error=self.capacity_error=None
                if self.mock:
                    self.mock.view.update(state='ready',note='시작 대기 · 초기화 완료')
                atomic_json(self.folder/'latest.json',{'id':self.engine.data['id']})
                self.save()
                return self.view()
        finally:
            with self.lock:self.resetting=False

    def view(self):
        with self.lock:
            return {'run':self.engine.snapshot() if self.engine else None,'server_now':now_ms(),
                    'mock':self.mock.snapshot() if self.mock else None,
                    'capabilities':{'quote_environment':'real','execution':'paper','real_money_orders':False,
                                    'mock_broker_orders':True,'independent_portfolios':3}}

class BlendRuntime(BreakoutRuntime):
    def _get_history(self):
        end=datetime.now(KST);start=end-timedelta(days=4);histories={}
        for code,family in (('MNQZ26','a'),('MESZ26','b')):
            raw=chart_minute(code,exch_cd='FCME',inq_strt_dt=start.strftime('%Y%m%d'),
                             inq_ed_dt=end.strftime('%Y%m%d'),req_qty='500',next_key='9'*30,
                             api=runtime.client(),environment='real')
            histories[family]={r['timestamp']:r for r in quotes.bars(raw,minute=True,asked=code)['bars'] if r.get('close')}
        return [{'time':t,'a':histories['a'][t]['close'],'b':histories['b'][t]['close'],
                 'va':histories['a'][t].get('volume'),'vb':histories['b'][t].get('volume')}
                for t in sorted(histories['a'].keys()&histories['b'].keys())]

service=BlendService()
