"""Forward-only, isolated paper portfolios for conditional relative momentum.

This module cannot call a broker. Each arm has its own $100k ledger and its own
copy of displayed liquidity (counterfactual alternatives, not one shared account).
Fills use a later exchange-stamped quote, displayed depth and adverse slippage.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from typing import Any

from backend.strategy.paper import PaperBook, PaperOrder, MARKET_ORDER
from backend.strategy.spread_rotation import RotationSettings, _book_prices, evaluate_strength, plan_sizes
from backend.strategy.spread_comparison import single_signal, single_sizes

MODEL = 'rotation-forward-v1'
ARM_NAMES = {
    'spread': '상대강도 스프레드',
    'nasdaq_only': '나스닥 단독',
    'sp500_only': 'S&P 500 단독',
    'spread_without_regime_gate': '상승·하락 확인 제외',
    'spread_micro_only': '마이크로만 사용',
}
REASONS = {
    'history': '기준 분봉 수집', 'invalid_price': '가격 확인',
    'missing_or_duplicate_minute': '분봉 누락 확인', 'zero_variation': '변동성 부족',
    'unstable_hedge': '헤지 관계 불안정', 'both_regimes_required': '상승·하락 표본 부족',
    'direction_dependent_strength': '상승·하락 상대강도 불일치',
    'weak_or_expired_strength': '추세 강도 부족', 'signal': '진입 조건 충족',
    'verified_margin_and_fresh_capacity_required': '주문 가능 수량 확인',
    'available_cash_required': '가용 자금 확인', 'no_reference': '기준 자료 확인',
    'risk_hedge_depth_or_freshness_constraints': '수량·헤지·호가 조건 대기',
    'constraints': '수량·호가 조건 대기', 'cost': '거래비용 초과',
    'warmup': '시세 점검', 'stale': '최신 시세 대기', 'stale_history': '최신 분봉 대기',
    'cooldown': '재진입 대기', 'same_signal': '다음 분봉 대기',
    'session_loss_limit': '손실 한도 · 신규 진입 중지', 'entry_cutoff': '신규 진입 종료',
    'entering': '진입 체결 중', 'holding': '보유', 'exiting': '청산 체결 중',
    'stopped': '종료', 'restored': '재접속 · 잔여 보유 정리', 'source_changed': '코드 변경 · 신규 진입 중지',
    'stop_loss': '손절', 'structural_stop': '추세 기준 이탈', 'take_profit': '익절',
    'trailing_stop': '수익 보호', 'time_exit': '최대 보유 시간',
    'leg_timeout': '부분 체결 정리', 'feed_gap': '시세 공백 정리',
    'session_end': '운용 종료 정리', 'user_stop': '중지 요청 정리',
}


@dataclass(frozen=True)
class ExecutionSettings:
    duration_seconds: int = 10800
    warmup_seconds: int = 300
    entry_cutoff_before_end: int = 600
    flatten_before_end: int = 180
    max_hold_seconds: int = 600
    leg_timeout_seconds: int = 10
    cooldown_seconds: int = 30
    trail_start_r: float = 1
    trail_distance_r: float = .5
    quote_max_age_ms: int = 5000
    quote_max_skew_ms: int = 2000
    feed_gap_exit_seconds: int = 30


def fresh(products: list[dict], at: int, e: ExecutionSettings) -> bool:
    if not products or any(not 0 <= at - p.get('quote_at', 0) <= e.quote_max_age_ms for p in products):
        return False
    if max(p['quote_at'] for p in products) - min(p['quote_at'] for p in products) > e.quote_max_skew_ms:
        return False
    return all(_book_prices(p, 1) is not None for p in products)


def empty_arm(name: str, mode: str) -> dict:
    return {'id': mode + ':' + name, 'name': name, 'label': ARM_NAMES[name], 'sizing_mode': mode,
            'state': 'watching', 'reason': 'warmup', 'positions': {}, 'orders': [], 'fills': [],
            'trades': [], 'trade': None, 'signal': {}, 'plan': {}, 'realized': 0., 'fees': 0.,
            'slippage': 0., 'net': 0., 'peak_net': 0., 'max_drawdown': 0., 'mark_stale': False,
            'last_exit': 0, 'last_signal': None, 'last_decision': 0, 'loss_locked': False,
            'blocked': {}, 'curve': [], 'last_good_at': 0, 'attribution': {'a': 0., 'b': 0.}}


class RotationEngine:
    def __init__(self, run_id: str, at: int, settings: RotationSettings | None = None,
                 execution: ExecutionSettings | None = None):
        self.settings = settings or replace(RotationSettings(), sizing_mode='max_affordable')
        self.execution = execution or ExecutionSettings()
        self.data = {'id': run_id, 'model': MODEL, 'mode': 'paper', 'quote_environment': 'real',
                     'broker_orders_sent': False, 'account_history_requested': False,
                     'started_at': at, 'end_at': at + self.execution.duration_seconds * 1000,
                     'updated_at': at, 'enabled': True, 'stop_requested': False,
                     'settings': asdict(self.settings), 'execution': asdict(self.execution),
                     'arms': [empty_arm(name, mode) for mode in ('max_affordable', 'risk_budget') for name in ARM_NAMES],
                     'events': [], 'markets': [], 'bars': [], 'feed_ok': False, 'feed_error': None,
                     'warmup_valid_ms': 0, 'last_warmup_at': at, 'last_market_point': 0,
                     'market_curve': [], 'sources': {}, 'entry_lock': False}
        self.books: dict[tuple[str, str], PaperBook] = {}
        self.last_book: dict[tuple[str, str], int] = {}
        self.new_records: list[dict] = []

    def event(self, kind: str, at: int, arm: dict | None = None, **values):
        row = {'kind': kind, 'at': at, 'elapsed_seconds': max(0, (at - self.data['started_at']) / 1000),
               'arm': arm['id'] if arm else None, **values}
        self.data['events'].append(row)
        self.data['events'] = self.data['events'][-150:]
        self.new_records.append(row)

    def book(self, arm: dict, code: str) -> PaperBook:
        return self.books.setdefault((arm['id'], code), PaperBook(delay=.3))

    def _order(self, arm, code, side, quantity, purpose, at):
        if quantity <= 0:
            return
        oid = f"{self.data['id']}-{arm['id']}-{len(arm['orders']) + 1}"
        order = self.book(arm, code).place(oid, side=side, quantity=quantity, order_type=MARKET_ORDER, at=at)
        row = {**asdict(order), 'code': code, 'purpose': purpose}
        arm['orders'].append(row)
        self.event('order', at, arm, order=copy.deepcopy(row))

    def _cancel(self, arm, at, reason):
        for (aid, code), book in self.books.items():
            if aid != arm['id']:
                continue
            for order in book.cancel_open(reason):
                row = next(r for r in arm['orders'] if r['id'] == order.id)
                row.update(status=order.status, reason=reason)
                self.event('cancel', at, arm, code=code, order_id=order.id, reason=reason)

    def _fill(self, arm, p, fill, received_at):
        order = next(r for r in arm['orders'] if r['id'] == fill['orderId'])
        side = 1 if fill['side'] == 'buy' else -1
        adverse = p['tick'] * self.settings.extra_ticks_per_side
        price, qty = fill['price'] + side * adverse, fill['quantity']
        fee, slip = qty * p['fee'], qty * p['multiplier'] * adverse
        row = {**fill, 'code': p['code'], 'family': p['family'], 'purpose': order['purpose'],
               'observed_price': fill['price'], 'price': price, 'fee': fee, 'slippage': slip,
               'received_at': received_at, 'multiplier': p['multiplier'], 'virtual': True}
        pos = arm['positions'].setdefault(p['code'], {'code': p['code'], 'family': p['family'],
            'quantity': 0, 'average': 0., 'multiplier': p['multiplier'], 'fee': p['fee'], 'tick': p['tick']})
        old_qty = pos['quantity']
        gross = 0.
        if not old_qty or old_qty * side > 0:
            pos['average'] = (pos['average'] * abs(old_qty) + price * qty) / (abs(old_qty) + qty)
        else:
            if qty > abs(old_qty):
                raise RuntimeError('Paper exit cannot reverse a holding')
            gross = (price - pos['average']) * qty * p['multiplier'] * (1 if old_qty > 0 else -1)
        pos['quantity'] += side * qty
        if not pos['quantity']:
            pos['average'] = 0.
        row['realized'] = gross
        arm['realized'] += gross
        arm['fees'] += fee
        arm['slippage'] += slip
        arm['attribution'][p['family']] += gross - fee
        arm['fills'].append(row)
        order['filled'] += qty
        order['status'] = 'filled' if order['filled'] == order['quantity'] else 'partial'
        self.event('fill', received_at, arm, fill=row)

    def _consume(self, arm, products, at):
        for p in products:
            key = arm['id'], p['code']
            if not fresh([p], at, self.execution) or p['quote_at'] <= self.last_book.get(key, 0):
                continue
            self.last_book[key] = p['quote_at']
            # Use exchange time: an old snapshot received after an order cannot fill it.
            for fill in self.book(arm, p['code']).on_book(p, at=p['quote_at']):
                self._fill(arm, p, fill, at)

    def _mark(self, arm, products, at):
        held = [p for p in arm['positions'].values() if p['quantity']]
        by_code = {p['code']: p for p in products}
        relevant = [by_code[p['code']] for p in held if p['code'] in by_code]
        arm['mark_stale'] = bool(held) and (len(relevant) != len(held) or not fresh(relevant, at, self.execution))
        net = arm['realized'] - arm['fees']
        attribution = dict(arm['attribution'])
        for pos in held:
            p = by_code.get(pos['code'])
            prices = _book_prices(p, abs(pos['quantity'])) if p else None
            if prices is None:
                arm['mark_stale'] = True
                return
            side = 1 if pos['quantity'] > 0 else -1
            exit_price = (prices[0] if side > 0 else prices[1]) - side * p['tick'] * self.settings.extra_ticks_per_side
            value = (exit_price - pos['average']) * pos['quantity'] * pos['multiplier'] - abs(pos['quantity']) * pos['fee']
            pos['mark_price'] = exit_price
            pos['unrealized_net'] = value
            net += value
            attribution[pos['family']] += value
        if arm['mark_stale']:
            return
        arm.update(net=net, marked_at=at, marked_attribution=attribution)
        arm['peak_net'] = max(arm['peak_net'], net)
        arm['max_drawdown'] = max(arm['max_drawdown'], arm['peak_net'] - net)
        if self.settings.session_loss_fraction > 0 and net <= -self.settings.cash * self.settings.session_loss_fraction:
            arm['loss_locked'] = True

    def _exit(self, arm, at, reason):
        if arm['state'] == 'exiting':
            return
        self._cancel(arm, at, reason)
        arm.update(state='exiting', reason=reason)
        if arm['trade']:
            arm['trade']['exit_reason'] = reason
        for pos in arm['positions'].values():
            self._order(arm, pos['code'], 'sell' if pos['quantity'] > 0 else 'buy', abs(pos['quantity']), 'exit', at)
        self.event('exit_signal', at, arm, reason=reason)

    def _finish(self, arm, at):
        trade = arm['trade']
        if trade:
            trade.update(closed_at=at, net=arm['realized'] - arm['fees'] - trade['starting_net'],
                         fees=arm['fees'] - trade['starting_fees'],
                         slippage=arm['slippage'] - trade['starting_slippage'],
                         hold_seconds=(at - trade['entry_at']) / 1000,
                         filled=any(f['time'] >= trade['entry_at'] for f in arm['fills']))
            arm['trades'].append(trade)
            self.event('trade_closed', at, arm, trade=copy.deepcopy(trade))
        arm.update(state='watching', trade=None, last_exit=at)

    def _enter(self, arm, signal, plan, products, at):
        chosen = copy.deepcopy(plan['chosen'])
        direction = signal['direction']
        routes = [(chosen['leg'], direction)] if 'leg' in chosen else [(chosen['leg_a'], direction), (chosen['leg_b'], -direction)]
        expected = {}
        for route, sign in routes:
            for ticket in route['tickets']:
                expected[ticket['code']] = sign * ticket['quantity']
        mids = {p['family']: sum(_book_prices(p, 1)) / 2 for p in products if p['code'].startswith(('NQ', 'ES'))}
        arm['trade'] = {'entry_at': at, 'opened_at': None, 'direction': direction, 'beta': signal.get('beta'),
            'family': signal.get('family'), 'stop_return': signal['stop_return'], 'base_prices': mids,
            'risk': chosen['risk'], 'target_net': chosen['target_net'], 'plan': chosen,
            'signal': copy.deepcopy(signal), 'expected': expected, 'starting_net': arm['realized'] - arm['fees'],
            'starting_fees': arm['fees'], 'starting_slippage': arm['slippage'], 'peak_net': 0.,
            'exit_reason': None, 'hedge_error_pct': chosen.get('hedge_error_pct')}
        arm.update(state='entering', reason='entering', last_signal=signal.get('signal_end'))
        self.event('entry_signal', at, arm, signal=copy.deepcopy(signal), plan=chosen)
        for code, quantity in expected.items():
            self._order(arm, code, 'buy' if quantity > 0 else 'sell', abs(quantity), 'entry', at)

    def _structural_move(self, trade, products):
        mids = {p['family']: sum(_book_prices(p, 1)) / 2 for p in products
                if p['code'].startswith(('NQ', 'ES')) and _book_prices(p, 1)}
        base = trade['base_prices']
        family = trade.get('family')
        if family:
            return trade['direction'] * (mids[family] / base[family] - 1) if family in mids and family in base else None
        if not all(f in mids and f in base for f in ('a', 'b')):
            return None
        return trade['direction'] * (mids['a'] / base['a'] - 1 - trade['beta'] * (mids['b'] / base['b'] - 1))

    def _block(self, arm, reason, at):
        if arm['reason'] != reason:
            self.event('waiting', at, arm, reason=reason)
        arm['reason'] = reason
        # Count decision samples, not arbitrary UI polling requests.
        arm['blocked'][reason] = arm['blocked'].get(reason, 0) + 1

    def step(self, rows: list[dict], products: list[dict], at: int, *, feed_error: str | None = None):
        if not self.data['enabled']:
            return
        e, c, d = self.execution, self.settings, self.data
        d.update(updated_at=at, markets=copy.deepcopy(products), bars=rows[-180:], feed_error=feed_error)
        d['feed_ok'] = len(products) == 4 and fresh(products, at, e)
        delta = max(0, min(at - d['last_warmup_at'], 5000))
        if d['feed_ok']:
            d['warmup_valid_ms'] = min(e.warmup_seconds * 1000, d['warmup_valid_ms'] + delta)
        d['last_warmup_at'] = at
        elapsed = (at - d['started_at']) / 1000
        closed = [r for r in rows if r['time'] + 60000 <= at]
        history_fresh = bool(closed) and 0 <= at - (closed[-1]['time'] + 60000) <= 90000
        signals = {
            'spread': evaluate_strength(rows, at, c),
            'spread_without_regime_gate': evaluate_strength(rows, at, replace(c, require_both_regimes=False)),
            'nasdaq_only': single_signal(rows, 'a', at, c), 'sp500_only': single_signal(rows, 'b', at, c),
        }
        signals['spread_micro_only'] = signals['spread']
        for arm in d['arms']:
            # Lifecycle is evaluated before fills, so an expired entry is cancelled
            # before the first quote after a disconnect can fill it.
            trade = arm['trade']
            if arm['state'] == 'entering' and trade and at - trade['entry_at'] >= e.leg_timeout_seconds * 1000:
                self._exit(arm, at, 'leg_timeout')
            if (d['stop_requested'] or elapsed >= e.duration_seconds - e.flatten_before_end) and trade:
                self._exit(arm, at, 'user_stop' if d['stop_requested'] else 'session_end')
            self._consume(arm, products, at)
            self._mark(arm, products, at)
            held = any(p['quantity'] for p in arm['positions'].values())
            if arm['state'] == 'exiting' and not held:
                self._cancel(arm, at, 'flat')
                self._finish(arm, at)
            trade = arm['trade']
            if arm['state'] == 'entering' and trade:
                if all(arm['positions'].get(code, {}).get('quantity') == qty for code, qty in trade['expected'].items()):
                    trade['opened_at'] = at
                    arm.update(state='holding', reason='holding', last_good_at=at)
                    self.event('position_opened', at, arm)
            if arm['state'] in ('holding', 'entering') and trade:
                needed = [p for p in products if p['code'] in trade['expected']]
                good = len(needed) == len(trade['expected']) and fresh(needed, at, e) and not arm['mark_stale']
                if good:
                    arm['last_good_at'] = at
                if not good and at - max(arm['last_good_at'], trade['entry_at']) >= e.feed_gap_exit_seconds * 1000:
                    self._exit(arm, at, 'feed_gap')
                elif good:
                    net = arm['net'] - trade['starting_net']
                    trade['current_net'] = net
                    trade['peak_net'] = max(trade['peak_net'], net)
                    move = self._structural_move(trade, products)
                    if arm['loss_locked']:
                        self._exit(arm, at, 'session_loss_limit')
                    elif net <= -trade['risk']:
                        self._exit(arm, at, 'stop_loss')
                    elif move is not None and move <= -trade['stop_return']:
                        self._exit(arm, at, 'structural_stop')
                    elif net >= trade['target_net']:
                        self._exit(arm, at, 'take_profit')
                    elif trade['peak_net'] >= trade['risk'] * e.trail_start_r and net <= trade['peak_net'] - trade['risk'] * e.trail_distance_r:
                        self._exit(arm, at, 'trailing_stop')
                    elif at - trade['entry_at'] >= e.max_hold_seconds * 1000:
                        self._exit(arm, at, 'time_exit')
                continue
            if arm['state'] == 'exiting':
                continue  # Keep exit orders alive until fresh depth actually fills them.
            if at - arm['last_decision'] < 5000:
                continue
            arm['last_decision'] = at
            signal = signals[arm['name']]
            arm['signal'] = signal
            subset = [p for p in products if p['multiplier'] in (2, 5)] if arm['name'] == 'spread_micro_only' else products
            settings = replace(c, sizing_mode=arm['sizing_mode'])
            planner = single_sizes if arm['name'] in ('nasdaq_only', 'sp500_only') else plan_sizes
            # This invocation uses each portfolio's own realized P&L.
            plan = planner(signal, subset, at=at, c=settings, session_net=arm['realized'] - arm['fees'])
            arm['plan'] = {k: v for k, v in plan.items() if k not in ('candidates', 'limitations')}
            reason = None
            if d['stop_requested'] or elapsed >= e.duration_seconds - e.entry_cutoff_before_end:
                reason = 'entry_cutoff'
            elif d['entry_lock']:
                reason = 'source_changed'
            elif arm['loss_locked']:
                reason = 'session_loss_limit'
            elif d['warmup_valid_ms'] < e.warmup_seconds * 1000:
                reason = 'warmup'
            elif not d['feed_ok']:
                reason = 'stale'
            elif not history_fresh:
                reason = 'stale_history'
            elif not signal['eligible']:
                reason = signal['reason']
            elif at - arm['last_exit'] < e.cooldown_seconds * 1000:
                reason = 'cooldown'
            elif signal.get('signal_end') == arm['last_signal']:
                reason = 'same_signal'
            elif not plan.get('chosen'):
                reason = plan['reason']
            elif not plan['chosen']['cost_acceptable']:
                reason = 'cost'
            elif any(p.get('margin') is None for p in subset):
                reason = 'verified_margin_and_fresh_capacity_required'
            if reason:
                self._block(arm, reason, at)
            else:
                self._enter(arm, signal, plan, products, at)
        if at - d['last_market_point'] >= 5000:
            d['last_market_point'] = at
            point = {'elapsed': elapsed}
            for p in products:
                prices = _book_prices(p, 1)
                if prices:
                    point[p['code']] = sum(prices) / 2
            point['fresh'] = d['feed_ok']
            d['market_curve'].append(point)
            for arm in d['arms']:
                arm['curve'].append({'elapsed': elapsed, 'net': arm['net'],
                                     'return_pct': 100 * arm['net'] / c.cash, 'fresh': not arm['mark_stale']})
        all_flat = all(not a['trade'] and not any(p['quantity'] for p in a['positions'].values()) for a in d['arms'])
        if all_flat and (d['stop_requested'] or elapsed >= e.duration_seconds):
            d.update(enabled=False, ended_at=at)
            for arm in d['arms']:
                arm.update(state='stopped', reason='stopped')
            self.event('session_completed', at)

    def stop(self, at):
        self.data['stop_requested'] = True
        for arm in self.data['arms']:
            if arm['trade']:
                self._exit(arm, at, 'user_stop')
        self.event('stop_requested', at)

    def snapshot(self, *, full=False):
        out = copy.deepcopy(self.data)
        for arm in out['arms']:
            arm['reason_label'] = REASONS.get(arm['reason'], arm['reason'])
            arm['return_pct'] = 100 * arm['net'] / self.settings.cash
            arm['drawdown_pct'] = 100 * arm['max_drawdown'] / self.settings.cash
            done = [t for t in arm['trades'] if t['filled']]
            arm['completed_trades'] = len(done)
            arm['wins'] = sum(t['net'] > 0 for t in done)
            arm['average_entry_risk'] = sum(t['risk'] for t in done) / len(done) if done else None
            arm['fill_count'] = len(arm['fills'])
            if not full:
                arm['fills'] = arm['fills'][-12:]
                arm['orders'] = arm['orders'][-12:]
                arm['trades'] = arm['trades'][-20:]
                step = max(1, math.ceil(len(arm['curve']) / 500))
                arm['curve'] = arm['curve'][::step] + (arm['curve'][-1:] if step > 1 else [])
        if not full:
            step = max(1, math.ceil(len(out['market_curve']) / 500))
            out['market_curve'] = out['market_curve'][::step] + (out['market_curve'][-1:] if step > 1 else [])
        return out

    def checkpoint(self):
        saved = copy.deepcopy(self.data)
        saved['_paper_books'] = [{'arm': aid, 'code': code, 'orders': [asdict(o) for o in book.orders],
            'eaten': [[side, price, *values] for (side, price), values in book._eaten.items()],
            'last_book': self.last_book.get((aid, code), 0)} for (aid, code), book in self.books.items()]
        return saved

    @classmethod
    def restore(cls, saved: dict, at: int):
        engine = cls(saved['id'], saved['started_at'], RotationSettings(**saved['settings']), ExecutionSettings(**saved['execution']))
        engine.data = {k: v for k, v in copy.deepcopy(saved).items() if k != '_paper_books'}
        for one in saved.get('_paper_books', []):
            key = one['arm'], one['code']
            book = PaperBook()
            book.orders = [PaperOrder(**o) for o in one['orders']]
            book._eaten = {(x[0], x[1]): [x[2], x[3]] for x in one['eaten']}
            engine.books[key] = book
            engine.last_book[key] = one['last_book']
        if engine.data['enabled']:
            # Preserve deadline and equity. Resume exits; never replay old entries.
            for arm in engine.data['arms']:
                if arm['trade']:
                    engine._exit(arm, at, 'restored')
            engine.event('restored', at)
        return engine


def fingerprint(paths):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
