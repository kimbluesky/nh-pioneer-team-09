"""NH mock execution of the frozen candidate; account fills stay separate.

Only current positions and pending orders are queried. Past account fills are
never imported. Existing SimBroker/RunPump handles acknowledgments and fills.
"""
from __future__ import annotations
import asyncio, copy, threading
from pathlib import Path
from backend.nh_api import runtime
from backend.master import master_service
from backend.sessions import service as sessions
from backend.overseas_futureoption import overseas_futureoption_functions as api,overseas_account,overseas_quotes
from backend.strategy import runs,sim,paper
from backend.strategy.blend_recovery import RecoverableMockService
from backend.strategy.journal import Journal
from backend.strategy.pump import RunPump
from backend.strategy.spread import SpreadConfig,LegConfig,leg_draft
from backend.strategy.spread_blend import blend_signal,blend_sizes,blend_settings
from backend.strategy.rotation_runtime import now_ms,quote_stamp,atomic_json
from backend.strategy.rotation_engine import fresh,ExecutionSettings
from backend.strategy.spread_rotation import _book_prices

class ContinuousMockLimits:
    """This isolated mock session has per-trade stops, not daily loss pauses."""
    def read(self, environment='paper'):
        return {}

class ContinuousMockService(RecoverableMockService):
    def __init__(self, **kwargs):
        kwargs.setdefault('account', current_account)
        super().__init__(**kwargs)

    def start_problems(self, **kwargs):
        found=super().start_problems(**kwargs)
        # Regular strategy drafts require a positive daily loss limit. Only this
        # explicitly selected mock coordinator allows that field to be unset.
        if (kwargs.get('managed') and kwargs.get('mode')==runs.SIM
                and kwargs.get('environment')=='paper'
                and kwargs['draft'].get('aux',{}).get('dailyLoss') in (None,'')):
            found=[p for p in found if p['field']!='aux.dailyLoss']
        return found

class MockOnlySender(sim.NhSender):
    def order(self,**kw):
        r=kw['run']
        if r.mode!=runs.SIM or r.environment!='paper' or r.code not in ('MNQZ26','MESZ26'):
            raise sim.NotSent('Only approved MNQ/MES mock orders are allowed')
        return super().order(**kw)
    def cancel(self,**kw):
        r=kw['run']
        if r.mode!=runs.SIM or r.environment!='paper':raise sim.NotSent('Mock only')
        return super().cancel(**kw)

def current_account(run):
    if run.environment!='paper':raise ValueError('Mock account required')
    result={}
    for key,call,normal in (
        ('orders',lambda **kw:api.order_history(inq_div=api.PENDING,**kw),overseas_account.order_history),
        ('positions',api.open_interest,overseas_account.positions)):
        rows=[];following=''
        for _ in range(20):
            got=normal(call(next_key=following,environment='paper'))
            rows.extend(got.get(key) or []);following=got.get('nextKey') or ''
            if not following:break
        if following:raise ValueError('Incomplete account snapshot')
        result[key]=rows
    return result

class BlendMock:
    def __init__(self,forward):
        self.forward=forward;self.legs=None;self.pump=None;self.tasks=[];self.lock=threading.RLock()
        self.products=[];self.ready=False;self.last_attempt=0;self.last_signal=None;self.trade=None
        self.ended=False;self.last_good=0;self.close_reason=None;self.run_id=None;self.carry={}
        self.recovery_due={};self.recovery_failures={}
        self.view={'environment':'paper','state':'waiting','note':'시작 대기 · 해외 모의계좌',
                   'orders':0,'fills':0,'net':0.,'return_pct':0.,'positions':[],'completed_trades':0,
                   'peak_net':0.,'max_drawdown':0.,'drawdown_pct':0.,
                   'past_fills_imported':False,'real_money_orders':False,
                   'recovery_attempts':0,'recoveries':0,'retry_in_seconds':0}

    async def start(self):
        # Process-local activation; quote fetches remain explicitly real.
        runtime.activate('paper')
        if self.forward.external_owner:return
        self.tasks=[asyncio.create_task(self._loop()),asyncio.create_task(self._beat()),
                    asyncio.create_task(self._recover_loop())]

    async def stop(self):
        for task in self.tasks:task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        if self.pump:await self.pump.stop()

    def snapshot(self):
        with self.lock:
            out=copy.deepcopy(self.view)
            out.update(trade_at=self.trade['at'] if self.trade else None,
                close_requested=bool(self.close_reason),
                can_manual_close=bool(self.ready and not self.ended and self.trade
                    and not self.close_reason and any(p['quantity'] for p in out['positions'])))
            return out

    def request_manual_close(self,run_id,trade_at):
        """Queue an exit for exactly the pair the operator was viewing.

        The regular unwind/recovery loop cancels unfilled entries and closes
        confirmed holdings. No new broker order is sent from the HTTP handler.
        """
        if runtime.active_environment()!='paper':raise ValueError('해외 모의계좌에서만 청산할 수 있습니다')
        if not self.ready or self.ended or not self.legs:
            raise ValueError('청산할 모의계좌 보유가 없습니다')
        engines=self.legs.engines()
        if len(engines)!=2:raise ValueError('두 레그의 주문 상태를 확인 중입니다')
        with engines[0].lock,engines[1].lock:
            run=self.forward.engine.data if self.forward.engine else None
            if not run or run['id']!=run_id or self.run_id!=run_id:
                raise ValueError('실행이 변경됐습니다. 현재 화면을 확인해 주세요')
            if not self.trade or self.trade['at']!=trade_at:
                raise ValueError('해당 거래가 이미 종료됐거나 새 거래로 바뀌었습니다')
            if self.close_reason:return False  # Repeated clicks keep the same exit.
            if not any(e.ledger.position.quantity for e in engines):
                raise ValueError('청산할 모의계좌 보유가 없습니다')
            self.close_reason='수동 청산'
            self.view.update(state='exiting',note='수동 청산 요청 · 두 레그 정리 중')
            return True

    def can_start_new(self):
        if not self.legs:return True
        return not self.trade and not self.close_reason and all(
            not e.ledger.position.quantity and not e.book.open_orders
            and not getattr(e.book,'uncertain',[])
            and e.run.state not in (runs.ATTENTION,runs.RECONCILING)
            for e in self.legs.engines())

    def preflight(self):
        from types import SimpleNamespace
        if runtime.active_environment()!='paper':raise ValueError('해외 모의 환경에서만 시작할 수 있습니다')
        account=current_account(SimpleNamespace(environment='paper'))
        if account['orders'] or any(p.get('quantity') for p in account['positions']):
            raise ValueError('해외 모의계좌의 미체결·보유가 남아 있습니다. 계좌를 비운 뒤 시작하세요')
        return {'positions':0,'pending_orders':0,'checked_at':now_ms()}

    async def _change_run(self):
        if not self.can_start_new():
            self.view.update(state='attention',note='이전 실행의 보유·주문 확인 중')
            return False
        if self.pump:await self.pump.stop()
        self.legs=self.pump=None;self.products=[];self.ready=False;self.ended=False
        self.last_attempt=self.last_good=0;self.last_signal=None;self.trade=self.close_reason=None;self.run_id=None;self.carry={}
        self.recovery_due.clear();self.recovery_failures.clear()
        self.view.pop('last_exit_reason',None);self.view.pop('last_trade_net',None)
        self.view.update(state='waiting',note='새 모의 운용 연결 중',orders=0,fills=0,net=0.,return_pct=0.,positions=[],completed_trades=0,
                         peak_net=0.,max_drawdown=0.,drawdown_pct=0.,
                         recovery_attempts=0,recoveries=0,retry_in_seconds=0)
        return True

    def _prepare(self):
        if self.ready:return
        if not self.forward.engine:return
        rid=self.forward.engine.data['id'];folder=self.forward.folder/rid
        segment=self.forward.engine.data.get('mock_segment','')
        if segment and not segment.isalnum():raise ValueError('Invalid mock segment')
        suffix='-'+segment if segment else ''
        self.run_id=rid
        self.carry=self.forward.engine.data.get('mock_carry',{})
        if self.carry and self.carry.get('run_id')!=rid:raise ValueError('Carry must be from this run')
        self.view['completed_trades']=self.carry.get('completed_trades',0)
        master_service.service.reload()
        sessions.service.refresh_all()
        if self.legs is None:
            self.legs=ContinuousMockService(store=runs.RunStore(folder/f'mock-legs{suffix}.json'),journal=Journal(folder/f'mock-events{suffix}.jsonl'),
                                 account=current_account,sender=MockOnlySender())
            self.legs.load()
            if self.legs.engines():
                self.view.update(state='attention',note='재시작된 모의 실행 · 계좌 확인 필요');self.ended=True;return
        config=SpreadConfig(name='부분 헤지 평균회귀',leg_a=LegConfig(code='MNQZ26'),leg_b=LegConfig(code='MESZ26'),
                            cash=100000,max_loss=2000,fee_per_contract=2,session_seconds=10800)
        for f,leg in (('a',config.leg_a),('b',config.leg_b)):
            leg_id=f'blend-{rid}{suffix}-{f}'
            if any(e.run.id==leg_id for e in self.legs.engines()):continue
            draft=leg_draft(config,leg)
            draft['aux'].update(dailyLoss='',lossStreak='')
            self.legs.add(run_id=leg_id,version_id=rid,draft=draft,
                          market='overseas',code=leg.code,session='overseas',qty=1,cash=100000,
                          fee_per_contract=2,mode=runs.SIM,approved_at=now_ms(),environment='paper',controller='spread')
        self.ready=True;self.view.update(state='watching',note='모의계좌 연결 · 조건 대기')

    def _books(self):
        products=[];at=now_ms()
        for code,f,m in (('MNQZ26','a',2),('MESZ26','b',5)):
            b=overseas_quotes.book(api.bid_price(code,exch_cd='FCME',environment='paper'),asked=code)
            received=now_ms()
            p={**b,'family':f,'multiplier':m,'tick':.25,'fee':2.,'received_at':received,
               'quote_at':quote_stamp(b.get('time'),received),'margin':self.forward.margins.get(code[:-3])}
            limit=self.forward.limits.get(code,{})
            p.update(limit);products.append(p)
        self.products=products
        ages=[max(0,(now_ms()-p['quote_at'])/1000) for p in products]
        self.view['quote_age_seconds']=round(max(ages),1)

    def _unwind(self,at,reason):
        self.close_reason=reason;self.view.update(state='exiting',note=reason)
        return all([e.managed_unwind(at) for e in self.legs.engines()])

    def _step(self):
        at=now_ms();run=self.forward.engine.data if self.forward.engine else None
        if not run or not self.ready or self.ended or run['id']!=self.run_id:return
        engines=self.legs.engines()
        if len(engines)!=2:raise ValueError('Both managed legs required')
        if runtime.active_environment()!='paper':raise ValueError('Mock environment changed')
        with engines[0].lock,engines[1].lock:
            positions=[];net=self.carry.get('net',0.);fills=self.carry.get('fills',0);orders=self.carry.get('orders',0);marks_valid=True
            for e in engines:
                pos=e.ledger.position;p=next((p for p in self.products if p['code']==e.run.code),None)
                book=_book_prices(p,max(1,abs(pos.quantity))) if p else None
                if pos.quantity and book is None:marks_valid=False
                mark=(book[0] if pos.quantity>0 else book[1]) if book else e.book.last_price
                net+=e.ledger.profit(mark)-abs(pos.quantity)*2
                orders+=len(e.book.orders);fills+=sum(o.filled for o in e.book.orders)
                positions.append({'code':e.run.code,'quantity':pos.quantity,'average':pos.avg_price,'state':e.run.state})
            self.view.update(net=round(net,2),return_pct=round(net/1000,4),orders=orders,fills=fills,positions=positions)
            good=fresh(self.products,at,ExecutionSettings())
            if good and marks_valid:
                peak=max(self.view['peak_net'],net);dd=max(self.view['max_drawdown'],peak-net)
                self.view.update(peak_net=peak,max_drawdown=round(dd,2),drawdown_pct=round(dd/1000,4))
            if good:self.last_good=at
            flat=all(not e.ledger.position.quantity and not e.book.open_orders for e in engines)
            ending=run['stop_requested'] or at>=run['end_at']-run['execution']['flatten_before_end']*1000
            if any(e.run.state in (runs.ATTENTION,runs.RECONCILING) for e in engines):
                self.view.update(state='recovering',note='계좌·주문 대조 중 · 자동 재시도')
                if ending:self.close_reason=self.close_reason or '세션 정리'
                if self.trade and not good and at-max(self.trade['at'],self.last_good)>=30000:
                    self.close_reason=self.close_reason or '시세 공백 정리'
                if self.close_reason:
                    for e in engines:
                        if e.run.state not in runs.QUIET_STATES:e.managed_unwind(at)
                return
            if self.close_reason or ending:
                done=self._unwind(at,self.close_reason or '세션 정리')
                if done:
                    if self.trade and fills>self.trade.get('fills_before',0):
                        self.view['completed_trades']+=1
                        self.view.update(last_exit_reason=self.close_reason,
                            last_trade_net=round(net-self.trade['base'],2))
                    self.trade=None;self.close_reason=None
                    if not ending:
                        # Known entry rejections can recover after both legs are
                        # flat. Unknown acknowledgments remain ATTENTION above.
                        for e in engines:
                            if e.run.state==runs.PAUSED:self.legs.resume(e.run.id,at)
                    self.view.update(state='observing' if ending else 'watching',note='종료 대기' if ending else '다음 신호 대기')
                    if run['stop_requested'] or at>=run['end_at']:self.ended=True
                return
            if self.trade:
                t=self.trade;wanted=t['expected'];complete=all(e.ledger.position.quantity==wanted[e.run.code] and not e.book.open_orders for e in engines)
                if not complete and at-t['at']>=10000:self._unwind(at,'레그 체결 지연 정리');return
                if any(e.run.state in (runs.PAUSED,runs.ENDED) for e in engines):self._unwind(at,'주문 거절 정리');return
                if not good and at-max(t['at'],self.last_good)>=30000:self._unwind(at,'시세 공백 정리');return
                if complete and good and marks_valid:
                    self.view.update(state='holding',note='두 레그 보유')
                    value=net-t['base']
                    if value<=-t['risk']:self._unwind(at,'손절')
                    elif value>=t['target']:self._unwind(at,'익절')
                return
            if not good:self.view.update(state='waiting',note='최신 모의 호가 대기');return
            if not flat:self.view.update(state='attention',note='기존 보유·미체결 확인');return
            if run.get('entry_lock'):
                self.view.update(state='attention',note='고정 전략 파일 변경 감지 · 신규 진입 보류');return
            for e in engines:
                if e.run.state==runs.PAUSED:self.legs.resume(e.run.id,at)
            # Fill/quote connection is independently checked by RunEngine.
            signal=blend_signal(self.forward.rows,self.products,at)
            if not signal['eligible']:
                self.view.update(state='watching',note='평균회귀 조건 대기',signal=signal);return
            if signal['signal_end']==self.last_signal:return
            plan=blend_sizes(signal,self.products,at=at,session_net=net,broker=True)
            if not plan['chosen']:
                self.view.update(state='watching',note='모의 호가 비용·잔량 확인',signal=signal);return
            chosen=plan['chosen'];expected={}
            for e,f in zip(engines,('a','b')):
                qty=chosen['leg_'+f]['quantity'];e.run.qty=qty
                e.run.draft['aux'].update(perOrder=str(qty),maxHold=str(qty))
                expected[e.run.code]=qty*signal['direction']*(1 if f=='a' else -1)
            sides=[paper.BUY if expected[e.run.code]>0 else paper.SELL for e in engines]
            reasons=[e.managed_entry_reason(s,at) for e,s in zip(engines,sides)]
            if any(reasons):self.view.update(state='watching',note=' / '.join(x for x in reasons if x));return
            self.last_signal=signal['signal_end']
            self.trade={'at':at,'expected':expected,'base':net,'risk':chosen['risk'],'target':chosen['target_net'],
                        'estimated_round_trip_cost':chosen['cost'],'fills_before':fills}
            atomic_json(self.forward.folder/run['id']/'mock-intent.json',self.trade)
            self.view.update(state='entering',note='모의계좌 두 레그 주문 전송',target=chosen['target_net'],risk=chosen['risk'],
                             estimated_round_trip_cost=chosen['cost'],signal=signal)
            for e,s in zip(engines,sides):
                if e.managed_enter(s,at) is None:
                    self._unwind(at,'주문 결과 확인 · 반대 레그 정리');break
            self.legs.store.save([e.run for e in engines])

    async def _loop(self):
        while True:
            try:
                current=self.forward.engine.data['id'] if self.forward.engine else None
                if self.run_id and current!=self.run_id:
                    if not await self._change_run():
                        await asyncio.sleep(2);continue
                if (self.forward.active() or self.trade or self.close_reason) and not self.ended:
                    if not self.ready:
                        if now_ms()-self.last_attempt>=5000:
                            self.last_attempt=now_ms();await asyncio.to_thread(self._prepare)
                    if self.ready:
                        if not self.pump:
                            self.pump=RunPump(self.legs,name='blend-mock',limits=ContinuousMockLimits())
                        # start() is idempotent; a failed first subscription is
                        # retried instead of leaving ready=True without a pump.
                        await self.pump.start()
                        await asyncio.to_thread(self._books)
            except Exception as exc:
                self.view.update(state='waiting',note='모의 연결 확인 · '+type(exc).__name__)
            await asyncio.sleep(2)

    async def _beat(self):
        while True:
            try:
                if self.ready and not self.ended:await asyncio.to_thread(self._step)
            except Exception as exc:
                self.view.update(state='attention',note='모의 주문 관리 확인 · '+type(exc).__name__)
                self.close_reason=self.close_reason or '주문 관리 복구 · 잔여 보유 정리'
            await asyncio.sleep(.5)

    async def _recover_loop(self):
        while True:
            try:
                if self.ready and not self.ended:
                    for e in self.legs.engines():
                        at=now_ms();key=e.run.id
                        needed=(e.run.state in runs.QUIET_STATES or e.book.uncertain
                                or any(at-o.created>=5000 for o in e.book.open_orders))
                        if not needed or at<self.recovery_due.get(key,0):continue
                        self.view['recovery_attempts']+=1
                        was_quiet=e.run.state in runs.QUIET_STATES
                        try:ok=await asyncio.to_thread(self.legs.recover,key,at)
                        except Exception:ok=False
                        failures=0 if ok else self.recovery_failures.get(key,0)+1
                        self.recovery_failures[key]=failures
                        delay=min(15,2**min(failures+1,4))
                        self.recovery_due[key]=now_ms()+delay*1000
                        self.view['retry_in_seconds']=0 if ok else delay
                        if ok and was_quiet:
                            self.view['recoveries']+=1
                            # A reconciled, complete pair keeps its original
                            # frozen exits. An incomplete pair is unwound.
                            with e.lock:
                                if self.trade and not self.close_reason and e.run.state==runs.PAUSED:
                                    wanted=self.trade['expected'].get(e.run.code)
                                    if e.ledger.position.quantity==wanted and not e.book.open_orders:
                                        self.legs.resume(key,now_ms())
            except Exception as exc:
                self.view.update(state='recovering',note='주문 복구 재시도 · '+type(exc).__name__)
            await asyncio.sleep(1)
