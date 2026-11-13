"""V10 entry revision: five-minute breakout, three fresh quotes, economic chase filter.

The lifecycle loop is deliberately frozen from V6 so the active V6 source and
its audit hashes remain unchanged. Accounting and paper execution are inherited.
No broker order API is reachable from this engine.
"""
from __future__ import annotations
import copy
from dataclasses import replace
from backend.strategy.rotation_engine import RotationEngine, ExecutionSettings, fresh
from backend.strategy.spread_rotation import _book_prices
from backend.strategy.spread_scalp import reference_products
from backend.strategy.spread_fast_breakout import MODEL, RULES, fast_settings, fast_signal, fast_sizes, update_fast_samples

ALLOWED_CODES = frozenset(('MNQZ26', 'MESZ26'))
EXECUTION_POLICY = {'version': 'micro-only-v1', 'allowed_codes': sorted(ALLOWED_CODES),
                    'cash_per_portfolio': 100000, 'pair_budget_is_combined': True}
LABELS = {'spread': '가격차 돌파', 'nasdaq_only': '나스닥 단독',
          'sp500_only': 'S&P 500 단독', 'spread_without_regime_gate': '돌파 확인 제외',
          'spread_micro_only': '마이크로만 사용'}

class FastBreakoutEngine(RotationEngine):
    def __init__(self, run_id, at, settings=None, execution=None):
        super().__init__(run_id, at, settings or fast_settings(), execution or replace(
            ExecutionSettings(), warmup_seconds=0, max_hold_seconds=0, cooldown_seconds=5,
            trail_start_r=.8, trail_distance_r=.4, entry_cutoff_before_end=180))
        self.data['arms'] = [a for a in self.data['arms'] if a['name'] != 'spread_micro_only']
        self.data.update(model=MODEL, pressure_samples=[], strategy_rules=copy.deepcopy(RULES), execution_policy=copy.deepcopy(EXECUTION_POLICY),
            market_families={'a': {'label': '나스닥 100', 'codes': ['MNQZ26']},
                             'b': {'label': 'S&P 500', 'codes': ['MESZ26']}},
            hypothesis='Five-minute residual breakout with two-second confirmation; twenty-minute target and invalidation buffer')
        for arm in self.data['arms']:
            arm['label'] = LABELS[arm['name']]

    @classmethod
    def restore(cls, saved, at):
        # A reviewed, one-use entry-policy migration retains open PAPER orders
        # and each trade's frozen exits. Ordinary crash recovery still flattens.
        revision = saved.get('pending_entry_revision_resume')
        if not revision:
            return super().restore(saved, at)
        if (saved.get('mode') != 'paper' or saved.get('broker_orders_sent')
                or revision.get('version') != RULES['version']
                or revision.get('end_at') != saved['end_at']):
            raise ValueError('Invalid paper entry-policy handoff')
        shadow = copy.deepcopy(saved)
        enabled = shadow['enabled']
        shadow['enabled'] = False
        engine = super().restore(shadow, at)
        engine.data['enabled'] = enabled
        engine.data.pop('pending_entry_revision_resume', None)
        engine.event('entry_revision_resumed', at, version=RULES['version'],
                     open_trades_keep_frozen_exits=True)
        return engine

    def _enter(self, arm, signal, plan, products, at):
        products = [p for p in products if p['code'] in ALLOWED_CODES]
        chosen = plan['chosen']
        routes = [chosen[k] for k in ('leg_a', 'leg_b', 'leg') if k in chosen]
        if any(t['code'] not in ALLOWED_CODES for route in routes for t in route['tickets']):
            raise ValueError('Scalp entries are restricted to MNQ and MES')
        super()._enter(arm, signal, plan, products, at)
        arm['trade']['base_prices'] = {f: sum(_book_prices(p, 1)) / 2
            for f, p in reference_products(products).items() if p and _book_prices(p, 1)}

    def _structural_move(self, trade, products):
        products = [p for p in products if p['code'] in ALLOWED_CODES]
        mids = {f: sum(_book_prices(p, 1)) / 2 for f, p in reference_products(products).items()
                if p and _book_prices(p, 1)}
        base, family = trade['base_prices'], trade.get('family')
        if family:
            return trade['direction'] * (mids[family] / base[family] - 1) if family in mids and family in base else None
        if not all(f in mids and f in base for f in ('a', 'b')):
            return None
        return trade['direction'] * (mids['a'] / base['a'] - 1 - trade['beta'] * (mids['b'] / base['b'] - 1))

    def _finish(self, arm, at):
        super()._finish(arm, at)
        arm['wait_new_excursion'] = False

    def snapshot(self, *, full=False):
        out = super().snapshot(full=full)
        labels = {'inside_range': '범위 돌파 대기', 'turn_history': '실시간 돌파 확인',
                  'breakout_confirm': '돌파 유지 확인', 'breakout_chase': '추격 진입 제한',
                  'target_below_cost_and_risk': '목표 여유·손익비 부족',
                  'new_excursion': '새 돌파 대기', 'structural_stop': '돌파 실패 손절'}
        for arm in out['arms']:
            arm['reason_label'] = labels.get(arm['reason'], arm['reason_label'])
            # Chart points cover all orders, not just the last twelve partial fills.
            source = next(a for a in self.data['arms'] if a['id'] == arm['id'])
            grouped = {}
            for fill in source['fills']:
                key = (fill['orderId'], fill['code'], fill['purpose'], fill['side'])
                row = grouped.get(key)
                if row is None:
                    grouped[key] = copy.deepcopy(fill)
                else:
                    quantity = row['quantity'] + fill['quantity']
                    row['price'] = (row['price'] * row['quantity'] + fill['price'] * fill['quantity']) / quantity
                    row['quantity'] = quantity
                    row['fee'] += fill['fee']
            arm['chart_fills'] = list(grouped.values())
        return out

    def step(self, rows: list[dict], products: list[dict], at: int, *, feed_error: str | None = None):
        if not self.data['enabled']:
            return
        products = [p for p in products if p['code'] in ALLOWED_CODES]
        e, c, d = self.execution, self.settings, self.data
        d.update(updated_at=at, markets=copy.deepcopy(products), bars=rows[-180:], feed_error=feed_error)
        d['feed_ok'] = {p['code'] for p in products} == ALLOWED_CODES and fresh(products, at, e)
        delta = max(0, min(at - d['last_warmup_at'], 5000))
        if d['feed_ok']:
            d['warmup_valid_ms'] = min(e.warmup_seconds * 1000, d['warmup_valid_ms'] + delta)
        d['last_warmup_at'] = at
        elapsed = (at - d['started_at']) / 1000
        closed = [r for r in rows if r['time'] + 60000 <= at]
        history_fresh = bool(closed) and 0 <= at - (closed[-1]['time'] + 60000) <= 90000
        samples = d['pressure_samples']
        update_fast_samples(samples, products, at)
        signals = {
            'spread': fast_signal(rows, products, at, samples, c),
            'spread_without_regime_gate': fast_signal(rows, products, at, samples, c, confirm_turn=False),
            'nasdaq_only': fast_signal(rows, products, at, samples, c, family='a'),
            'sp500_only': fast_signal(rows, products, at, samples, c, family='b'),
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
                    elif net >= trade['target_net'] or (move is not None and move >= trade['signal']['target_return']):
                        self._exit(arm, at, 'take_profit')
                    elif trade['peak_net'] >= .6 * trade['target_net'] and net <= .5 * trade['peak_net']:
                        self._exit(arm, at, 'trailing_stop')
                continue
            if arm['state'] == 'exiting':
                continue  # Keep exit orders alive until fresh depth actually fills them.
            if at - arm['last_decision'] < 1000:
                continue
            arm['last_decision'] = at
            signal = signals[arm['name']]
            arm['signal'] = signal
            if signal.get('reason') == 'inside_range':
                arm['wait_new_excursion'] = False
            subset = [p for p in products if p['multiplier'] in (2, 5)] if arm['name'] == 'spread_micro_only' else products
            settings = replace(c, sizing_mode=arm['sizing_mode'])
            planner = fast_sizes
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
                reason = 'target_below_cost_and_risk'
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
