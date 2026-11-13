"""Isolated breakout app-paper research runtime. No broker execution calls."""
from __future__ import annotations
import asyncio
import copy
import json
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from backend import quotes
from backend.nh_api import runtime
from backend.overseas_futureoption import overseas_quotes
from backend.overseas_futureoption.overseas_futureoption_functions import bid_price, chart_minute
from backend.strategy.rotation_engine import fingerprint, ExecutionSettings
from backend.strategy.spread_rotation import RotationSettings
from backend.strategy.rotation_runtime import RotationService, RotationRuntime, now_ms, atomic_json, quote_stamp, KST, ROOT
from backend.strategy.fast_breakout_engine import FastBreakoutEngine as BreakoutEngine, MODEL

OUT = ROOT / 'week04' / 'results' / 'breakout-v9-nq-es'
SPECS = [('MNQZ26', 'a', 2, 2), ('MESZ26', 'b', 5, 2)]
SOURCES = [Path(__file__).with_name(n) for n in ('spread_fast_breakout.py', 'fast_breakout_engine.py', 'spread_breakout.py', 'breakout_engine.py', 'breakout_runtime.py',
    'breakout_app.py', 'spread_reversion.py', 'spread_scalp.py', 'rotation_engine.py', 'rotation_runtime.py', 'spread_rotation.py', 'spread_comparison.py', 'paper.py')]

class BreakoutService(RotationService):
    def __init__(self, folder=OUT):
        super().__init__(folder)

    def load(self):
        with self.lock:
            pointer = self.folder / 'latest.json'
            if not pointer.exists():
                return
            run_id = json.loads(pointer.read_text(encoding='utf-8'))['id']
            if not run_id.isalnum():
                raise ValueError('Invalid saved run ID')
            saved = json.loads((self.folder / run_id / 'state.json').read_text(encoding='utf-8'))
            self.engine = BreakoutEngine.restore(saved, now_ms())
            if saved.get('sources') != fingerprint(SOURCES):
                self.engine.data['entry_lock'] = True
            self.save()

    def start(self):
        if self.external_owner:
            raise ValueError('다른 운용 서버가 실행 중입니다. http://127.0.0.1:47103/?view=rotation 에서 조작하세요.')
        if runtime.replaying():
            raise ValueError('실제 NH 시세 연결이 필요합니다')
        with self.lock:
            if self.engine and self.engine.data['enabled']:
                return self.view()  # idempotent double click
            at = now_ms()
            self.engine = BreakoutEngine(uuid.uuid4().hex, at)
            self.engine.data['sources'] = fingerprint(SOURCES)
            self.engine.data['fee_basis'] = {'micro_per_side': 2, 'mini_per_side': 8,
                'account_specific_verified': False, 'source': 'https://futures.co.kr/content/Getcontent.do?content=3000016'}
            self.engine.event('session_started', at, model=MODEL)
            dest = self.folder / self.engine.data['id']
            (dest / 'source').mkdir(parents=True, exist_ok=True)
            for source in SOURCES:
                (dest / 'source' / source.name).write_bytes(source.read_bytes())
            atomic_json(dest / 'manifest.json', {'id': self.engine.data['id'], 'model': MODEL,
                'started_at': at, 'settings': self.engine.data['settings'], 'execution': self.engine.data['execution'],
                'sources': self.engine.data['sources'], 'mode': 'paper', 'quote_environment': 'real',
                'broker_orders_sent': False, 'account_history_requested': False,
                'execution_policy': self.engine.data['execution_policy'],
                'strategy_rules': self.engine.data['strategy_rules'],
                'comparison': '8 isolated $100k portfolios; MNQ/MES only; pair shares one $100k budget; independent entries'})
            atomic_json(self.folder / 'latest.json', {'id': self.engine.data['id']})
            self.save()
            return self.view()

    def view(self):
        with self.lock:
            if self.external_owner:
                pointer = self.folder / 'latest.json'
                if pointer.exists():
                    run_id = json.loads(pointer.read_text(encoding='utf-8'))['id']
                    if run_id.isalnum():
                        saved = json.loads((self.folder / run_id / 'state.json').read_text(encoding='utf-8'))
                        self.engine = BreakoutEngine(saved['id'], saved['started_at'], RotationSettings(**saved['settings']), ExecutionSettings(**saved['execution']))
                        self.engine.data = {k: v for k, v in saved.items() if k != '_paper_books'}
            return {'run': self.engine.snapshot() if self.engine else None, 'server_now': now_ms(),
                    'capabilities': {'quote_environment': 'real', 'execution': 'paper', 'real_money_orders': False,
                                     'mock_broker_orders': False, 'independent_portfolios': len(self.engine.data['arms']) if self.engine else 8}}


class BreakoutRuntime(RotationRuntime):
    def _get_book(self, spec):
        code, family, multiplier, fee = spec
        raw = bid_price(code, exch_cd='FCME', api=runtime.client(), environment='real')
        book = overseas_quotes.book(raw, asked=code)
        at = now_ms()
        return {**book, 'family': family, 'multiplier': multiplier, 'tick': .25, 'fee': fee,
                'fee_confirmed': False, 'received_at': at, 'quote_at': quote_stamp(book.get('time'), at)}

    async def _books(self):
        while True:
            if self.service.active():
                replies = await asyncio.gather(*(asyncio.to_thread(self._get_book, p) for p in SPECS), return_exceptions=True)
                products = [p for p in replies if isinstance(p, dict)]
                failed = [type(p).__name__ for p in replies if isinstance(p, Exception)]
                with self.service.lock:
                    self.service.products = products
                    self.service.feed_error = ', '.join(failed) if failed else None
                    self.service.record('books', products)
            await asyncio.sleep(1)

    def _get_history(self):
        day = datetime.now(KST).strftime('%Y%m%d')
        histories = {}
        for code, family in (('MNQZ26', 'a'), ('MESZ26', 'b')):
            raw = chart_minute(code, exch_cd='FCME', inq_strt_dt=day, inq_ed_dt=day,
                               api=runtime.client(), environment='real')
            histories[family] = {r['timestamp']: r['close'] for r in quotes.bars(raw, minute=True, asked=code)['bars'] if r.get('close')}
        return [{'time': t, 'a': histories['a'][t], 'b': histories['b'][t]}
                for t in sorted(histories['a'].keys() & histories['b'].keys())]

    def _get_margins(self):
        fields = {'fts_opt_div_cd': 'F', 'cmdt_grp_cd': '', 'exch_vcd': '', 'curr_cd': 'USD',
                  'srch_div_cd': '2', 'cmdt_nm': ''}
        req = urllib.request.Request('https://futures.co.kr/DepositOsFutures/api/SGBS7108Q01.do',
                                     data=urllib.parse.urlencode(fields).encode())
        with urllib.request.urlopen(req, timeout=20) as reply:
            body = json.load(reply)
        return {p['cmdtcd']: float(p['ctr_mrgn_amt']) for p in json.loads(body['data'])
                if p['cmdtcd'] in ('MNQ', 'MES')}


service = BreakoutService()
