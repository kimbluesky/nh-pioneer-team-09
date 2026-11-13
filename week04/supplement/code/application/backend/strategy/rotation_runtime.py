"""NH real quotes -> independent app-paper portfolios; no broker order API.

Only quote, chart and mock NEW-capacity functions are imported. No execution
history is read. The heartbeat is separate from network workers so disconnects
cannot suspend deadlines, entry cancellation, or exit management.
"""
from __future__ import annotations

import asyncio
import copy
import csv
import io
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend import quotes
from backend.nh_api import runtime
from backend.overseas_futureoption import overseas_quotes, overseas_account
from backend.overseas_futureoption.overseas_futureoption_functions import bid_price, chart_minute, orderable_quantity
from backend.strategy.rotation_engine import RotationEngine, ExecutionSettings, fingerprint, MODEL
from backend.strategy.spread_rotation import RotationSettings

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'week04' / 'results' / 'rotation-v6-live'
KST = timezone(timedelta(hours=9))
SPECS = [('MNQZ26', 'a', 2, 2), ('NQZ26', 'a', 20, 8), ('MESZ26', 'b', 5, 2), ('ESZ26', 'b', 50, 8)]
SOURCES = [Path(__file__).with_name(n) for n in ('rotation_engine.py', 'rotation_runtime.py',
           'spread_rotation.py', 'spread_comparison.py', 'paper.py')]


def now_ms():
    return int(time.time() * 1000)


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    temp.replace(path)


def quote_stamp(clock, received):
    try:
        h, m, s = map(int, clock.split(':'))
        current = datetime.fromtimestamp(received / 1000, KST)
        stamp = current.replace(hour=h, minute=m, second=s, microsecond=0)
        if stamp > current + timedelta(seconds=1):
            stamp -= timedelta(days=1)
        return int(stamp.timestamp() * 1000)
    except (ValueError, TypeError, AttributeError):
        return 0


class RotationService:
    def __init__(self, folder: Path = OUT):
        self.folder = folder
        self.lock = threading.RLock()
        self.engine: RotationEngine | None = None
        self.products = []
        self.rows = []
        self.margins = {}
        self.limits = {}
        self.margin_at = 0
        self.feed_error = None
        self.capacity_error = None
        self.last_save = 0
        self.external_owner = False

    def load(self):
        with self.lock:
            pointer = self.folder / 'latest.json'
            if not pointer.exists():
                return
            run_id = json.loads(pointer.read_text(encoding='utf-8'))['id']
            if not run_id.isalnum():
                raise ValueError('Invalid saved run ID')
            saved = json.loads((self.folder / run_id / 'state.json').read_text(encoding='utf-8'))
            self.engine = RotationEngine.restore(saved, now_ms())
            if saved.get('sources') != fingerprint(SOURCES):
                self.engine.data['entry_lock'] = True
            self.save()

    def start(self):
        if self.external_owner:
            raise ValueError('다른 운용 서버가 실행 중입니다. http://127.0.0.1:47100/?view=rotation 에서 조작하세요.')
        if runtime.replaying():
            raise ValueError('실제 NH 시세 연결이 필요합니다')
        with self.lock:
            if self.engine and self.engine.data['enabled']:
                return self.view()  # idempotent double click
            at = now_ms()
            self.engine = RotationEngine(uuid.uuid4().hex, at)
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
                'comparison': '10 isolated $100k counterfactual portfolios; independent entries; same data and fill model'})
            atomic_json(self.folder / 'latest.json', {'id': self.engine.data['id']})
            self.save()
            return self.view()

    def active(self):
        with self.lock:
            return self.engine is not None and self.engine.data['enabled']

    def save(self):
        if not self.engine:
            return
        dest = self.folder / self.engine.data['id']
        atomic_json(dest / 'state.json', self.engine.checkpoint())
        if self.engine.new_records:
            with (dest / 'events.jsonl').open('a', encoding='utf-8') as f:
                for row in self.engine.new_records:
                    f.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
            self.engine.new_records.clear()
        self.last_save = now_ms()

    def record(self, kind, value):
        if not self.engine or not self.engine.data['enabled']:
            return
        with (self.folder / self.engine.data['id'] / 'market-data.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps({'kind': kind, 'received_at': now_ms(), 'value': value}, ensure_ascii=False, allow_nan=False) + '\n')

    def step(self):
        with self.lock:
            if not self.engine or not self.engine.data['enabled']:
                return
            at = now_ms()
            products = copy.deepcopy(self.products)
            for p in products:
                margin = self.margins.get(p['code'][:-3])
                limit = self.limits.get(p['code'], {})
                p.update(margin=margin if at - self.margin_at <= 86400000 else None, margin_at=self.margin_at,
                         margin_source='NH public standalone initial margin', **limit)
            self.engine.data['capacity_error'] = self.capacity_error
            self.engine.step(self.rows, products, at, feed_error=self.feed_error)
            if self.engine.new_records or at - self.last_save >= 5000:
                self.save()

    def view(self):
        with self.lock:
            if self.external_owner:
                pointer = self.folder / 'latest.json'
                if pointer.exists():
                    run_id = json.loads(pointer.read_text(encoding='utf-8'))['id']
                    if run_id.isalnum():
                        saved = json.loads((self.folder / run_id / 'state.json').read_text(encoding='utf-8'))
                        self.engine = RotationEngine(saved['id'], saved['started_at'], RotationSettings(**saved['settings']), ExecutionSettings(**saved['execution']))
                        self.engine.data = {k: v for k, v in saved.items() if k != '_paper_books'}
            return {'run': self.engine.snapshot() if self.engine else None, 'server_now': now_ms(),
                    'capabilities': {'quote_environment': 'real', 'execution': 'paper', 'real_money_orders': False,
                                     'mock_broker_orders': False, 'independent_portfolios': 10}}

    def stop(self):
        with self.lock:
            if self.external_owner:
                raise ValueError('다른 운용 서버가 실행 중입니다. 47100 화면에서 중지하세요.')
            if self.engine:
                self.engine.stop(now_ms())
                self.save()
            return self.view()

    def export(self):
        with self.lock:
            if not self.engine:
                raise ValueError('저장된 실행이 없습니다')
            if not self.external_owner:
                self.save()
            run = self.engine.snapshot(full=True)
            folder = self.folder / run['id']
            memory = io.BytesIO()
            with zipfile.ZipFile(memory, 'w', zipfile.ZIP_DEFLATED) as z:
                z.writestr('results.json', json.dumps(run, ensure_ascii=False, indent=2))
                keys = ('label', 'sizing_mode', 'return_pct', 'net', 'drawdown_pct', 'completed_trades',
                        'fill_count', 'fees', 'slippage', 'average_entry_risk', 'mark_stale')
                stream = io.StringIO(newline='')
                writer = csv.DictWriter(stream, fieldnames=keys, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(run['arms'])
                z.writestr('comparison.csv', '\ufeff' + stream.getvalue())
                for name in ('orders', 'fills', 'trades'):
                    records = [{'arm': a['id'], **r} for a in run['arms'] for r in a[name]]
                    z.writestr(name + '.json', json.dumps(records, ensure_ascii=False, indent=2))
                for file in folder.rglob('*'):
                    if file.is_file() and file.suffix in ('.json', '.jsonl', '.py') and file.name != 'state.json':
                        z.write(file, file.relative_to(folder).as_posix())
                z.writestr('READ_ME.txt', '실전 시세 · 앱 가상 체결. 실제 모의계좌 체결이 아닙니다.\n'
                    '각 비교군은 독립된 $100,000 계정입니다. 합산 수익률을 계산하지 않습니다.\n'
                    '수익률 = 수수료 및 청산 예상 비용 차감 손익 / $100,000.\n'
                    '신규 실행 이후의 시세만 체결에 사용했습니다. 과거 계좌 체결을 가져오지 않았습니다.\n'
                    '분봉은 과거 API 자료로 초기화합니다. 최근 미완성 봉은 신호에서 제외합니다.\n'
                    '호가 잔량·후속 호가·0.3초 이상 지연·편도 1틱 추가 불리한 체결을 가정합니다.\n'
                    '수수료는 마이크로 $2 / 미니 $8 편도 가정이며 계좌별 검증 전입니다.\n'
                    '최대 수량은 합산 개별 증거금·비용 예비금·모의 NEW 수량·호가·헤지오차 조건을 적용합니다.\n'
                    '위험 제한 비교군의 거래당 $500은 계획 상한이며 체결 후 실제 손실 상한이 아닙니다.\n'
                    '시세 공백에서 임의 청산하지 않습니다. 잔여 보유는 실제 후속 호가를 기다립니다.\n'
                    '원시 기록은 감사용 절대시각을 보존합니다. 화면과 발표 그림은 경과시간만 사용합니다.\n')
            return memory.getvalue()


class RotationRuntime:
    def __init__(self, service: RotationService):
        self.service = service
        self.tasks = []
        self.lock_file = None
        self.awake = False

    async def start(self):
        self.service.folder.mkdir(parents=True, exist_ok=True)
        self.lock_file = (self.service.folder / 'runtime.lock').open('a+b')
        try:
            # Windows locks also prohibit reading the locked byte. All access,
            # including initialization, must be inside the ownership attempt.
            self.lock_file.seek(0)
            if not self.lock_file.read(1):
                self.lock_file.write(b'1'); self.lock_file.flush()
            self.lock_file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock_file.close(); self.lock_file = None
            self.service.external_owner = True
            return
        try:
            await asyncio.to_thread(self.service.load)
        except Exception:
            log.exception('Rotation checkpoint could not be restored')
            self.service.feed_error = '저장된 실행 복원 실패'
        self.tasks = [asyncio.create_task(self._beat()), asyncio.create_task(self._books()),
                      asyncio.create_task(self._history()), asyncio.create_task(self._capacity())]

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if not self.service.external_owner:
            with self.service.lock:
                self.service.save()
        if self.lock_file:
            self.lock_file.close(); self.lock_file = None
        self._keep_awake(False)

    def _keep_awake(self, active):
        if os.name == 'nt' and active != self.awake:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if active else 0x80000000)
            self.awake = active

    async def _beat(self):
        while True:
            try:
                self._keep_awake(self.service.active())
                await asyncio.to_thread(self.service.step)
            except Exception:
                # Keep monitoring and prohibit new entries after an internal failure.
                log.exception('Rotation heartbeat failed')
                with self.service.lock:
                    if self.service.engine:
                        self.service.engine.data['entry_lock'] = True
                    self.service.feed_error = '실행 처리 오류 · 신규 진입 중지'
            await asyncio.sleep(.5)

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
        for code, family in (('NQZ26', 'a'), ('ESZ26', 'b')):
            raw = chart_minute(code, exch_cd='FCME', inq_strt_dt=day, inq_ed_dt=day,
                               api=runtime.client(), environment='real')
            histories[family] = {r['timestamp']: r['close'] for r in quotes.bars(raw, minute=True, asked=code)['bars'] if r.get('close')}
        return [{'time': t, 'a': histories['a'][t], 'b': histories['b'][t]}
                for t in sorted(histories['a'].keys() & histories['b'].keys())]

    async def _history(self):
        while True:
            if self.service.active():
                try:
                    rows = await asyncio.to_thread(self._get_history)
                    with self.service.lock:
                        self.service.rows = rows
                        self.service.record('minute_bars', rows)
                except Exception as exc:
                    with self.service.lock:
                        self.service.feed_error = '분봉 ' + type(exc).__name__
            await asyncio.sleep(12)

    def _get_margins(self):
        fields = {'fts_opt_div_cd': 'F', 'cmdt_grp_cd': '', 'exch_vcd': '', 'curr_cd': 'USD',
                  'srch_div_cd': '2', 'cmdt_nm': ''}
        req = urllib.request.Request('https://futures.co.kr/DepositOsFutures/api/SGBS7108Q01.do',
                                     data=urllib.parse.urlencode(fields).encode())
        with urllib.request.urlopen(req, timeout=20) as reply:
            body = json.load(reply)
        return {p['cmdtcd']: float(p['ctr_mrgn_amt']) for p in json.loads(body['data'])
                if p['cmdtcd'] in ('MNQ', 'NQ', 'MES', 'ES')}

    def _get_limits(self, products):
        limits = {}
        for p in products:
            if not p.get('asks'):
                continue
            price = min(x['price'] for x in p['asks'] if x.get('price'))
            raw = orderable_quantity(p['code'], glb_ord_tp_cd='1', ord_pric=str(price),
                                     api=runtime.client(), environment='paper')
            buy = overseas_account.orderable_quantity(raw, code=p['code'], side='buy')
            sell = overseas_account.orderable_quantity(raw, code=p['code'], side='sell')
            counts = buy.get('newQuantity'), sell.get('newQuantity')
            cap = min(counts) if all(isinstance(x, int) and x >= 0 for x in counts) else None
            limits[p['code']] = {'new_capacity': cap, 'capacity_at': now_ms(), 'available_amount': buy.get('amount')}
        return limits

    async def _capacity(self):
        while True:
            if self.service.active():
                try:
                    if now_ms() - self.service.margin_at >= 6 * 3600000:
                        margins = await asyncio.to_thread(self._get_margins)
                        with self.service.lock:
                            self.service.margins, self.service.margin_at = margins, now_ms()
                            self.service.record('public_margins', margins)
                    with self.service.lock:
                        products = copy.deepcopy(self.service.products)
                    if products:
                        limits = await asyncio.to_thread(self._get_limits, products)
                        with self.service.lock:
                            self.service.limits = limits
                            self.service.capacity_error = None
                            self.service.record('mock_NEW_capacity', limits)
                except Exception as exc:
                    with self.service.lock:
                        self.service.capacity_error = type(exc).__name__
            await asyncio.sleep(20 if self.service.products else 2)


service = RotationService()
