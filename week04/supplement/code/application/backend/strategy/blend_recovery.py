"""Recovery for this competition's mock orders only.

Never retry an unacknowledged NEW order. Recover a uniquely matching broker
record, replay only missing cumulative fills, then reconcile the account. Old
account trades are neither imported nor included in strategy P&L.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta

from backend.overseas_futureoption import overseas_account
from backend.overseas_futureoption import overseas_futureoption_functions as api
from backend.strategy import sim, runs
from backend.strategy.service import RunService

KST = timezone(timedelta(hours=9))


def clock_ms():
    return int(time.time() * 1000)


def order_snapshot(run):
    if run.environment != 'paper' or run.market != 'overseas':
        raise ValueError('Overseas mock account required')
    rows, following = [], ''
    for _ in range(20):
        page = overseas_account.order_history(api.order_history(
            inq_div=api.ALL, next_key=following, environment='paper'))
        rows.extend(page['orders'])
        following = page.get('nextKey') or ''
        if not following:
            return rows
    raise ValueError('Incomplete order snapshot')


def received_at(row):
    try:
        return int(datetime.fromisoformat(
            row['receivedDate'] + 'T' + row['receivedTime']).replace(tzinfo=KST).timestamp() * 1000)
    except (KeyError, TypeError, ValueError):
        return None


class RecoverableMockBroker(sim.SimBroker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.engine = None
        self.uncertain = []
        self.cancel_attempts = {}

    def place(self, order_id, **wanted):
        if self.uncertain:
            raise sim.SimRefused('이전 주문의 접수 결과를 확인 중입니다', reason='no-reply')
        try:
            return super().place(order_id, **wanted)
        except sim.SimRefused as exc:
            if exc.reason in ('no-reply', 'no-order-no'):
                self.uncertain.append({
                    'id': order_id, **wanted,
                    'intent': 'exit' if self.engine.ledger.position.quantity else 'entry',
                })
                self.engine._say(wanted['at'], 'reconcile', '접수 응답 유실 · 재전송 없이 계좌 확인')
            raise

    def cancel(self, order, reason):
        at = clock_ms()
        if at - self.cancel_attempts.get(order.id, 0) < 2000:
            return False
        self.cancel_attempts[order.id] = at
        return super().cancel(order, reason)


class RecoverableMockService(RunService):
    def __init__(self, *, account, history=None, **kwargs):
        self._current_account = account
        self._history = history or order_snapshot
        super().__init__(account=self._recovery_account, **kwargs)

    def _broker(self, run):
        if run.mode != runs.SIM or run.environment != 'paper' or run.market != 'overseas':
            raise ValueError('Recovery service is overseas mock only')
        return RecoverableMockBroker(run, sender=self._sender, environment=self._environment,
            tick=self.instrument(run.market, run.code, run.session)['tick'] or 1.)

    def _stand(self, run, *, tick=None):
        made = super()._stand(run, tick=tick)
        made.book.engine = made
        return made

    def _compare(self, made, at):
        with made.lock:
            found = super()._compare(made, at)
            if made.book.uncertain:
                found.append('접수 불명 주문 확인 중 · 중복 신규 주문 차단')
            return found

    def _recovery_account(self, run):
        made = self._engine(run.id)
        with made.lock:
            book = made.book
            owned = []
            if book.open_orders or book.uncertain:
                rows = self._history(run)
                known = self._order_numbers('paper', 'overseas')
                for attempt in list(book.uncertain):
                    matches = [r for r in rows if r.get('id') and r['id'] not in known
                        and r.get('code') == run.code and r.get('side') == attempt['side']
                        and r.get('quantity') == attempt['quantity']
                        and r.get('orderKind') == 'new' and r.get('orderType') == attempt['order_type']
                        and received_at(r) is not None
                        and attempt['at'] // 1000 * 1000 <= received_at(r) <= attempt['at'] + 120000]
                    if len(matches) != 1:
                        continue  # Missing or ambiguous evidence is never permission to resend.
                    row = matches[0]
                    order = sim.SimOrder(id=attempt['id'], side=attempt['side'],
                        quantity=attempt['quantity'], order_type=attempt['order_type'],
                        price=attempt.get('price'), stop_price=attempt.get('stop_price'),
                        fill_condition=attempt.get('fill_condition', ''), created=attempt['at'],
                        tick=book.tick, order_no=row['id'])
                    book.orders.append(order)
                    made._purpose[order.id] = attempt['intent']
                    made._snap[order.id] = None
                    book.uncertain.remove(attempt)
                    known.add(row['id'])
                    made._say(clock_ms(), 'reconcile', '접수 유실 주문번호 확인 · 기존 주문 복구')

                for order in list(book.open_orders):
                    row = next((r for r in rows if r.get('id') == order.order_no
                                and r.get('code') == run.code), None)
                    if row is None:
                        # A post-acceptance rejection may exist only in the
                        # broker's error endpoint. Query this owned order ID.
                        self.confirm(run.id, order.order_no, clock_ms())
                        continue
                    if row.get('side') != order.side or row.get('quantity') != order.quantity:
                        raise ValueError('Broker order identity mismatch')
                    owned.append(row)
                    filled = row.get('filled')
                    if not isinstance(filled, int) or not order.filled <= filled <= order.quantity:
                        raise ValueError('Invalid cumulative fill count')
                    event = dict(row)
                    if filled > order.filled:
                        average = row.get('avgFillPrice')
                        if not average and filled == 1:
                            average = row.get('fillPrice')
                        if not isinstance(average, (int, float)) or average <= 0:
                            raise ValueError('Confirmed average fill price required')
                        booked = sum(f['quantity'] * f['price'] for f in order.fills)
                        delta_price = (average * filled - booked) / (filled - order.filled)
                        if delta_price <= 0:
                            raise ValueError('Invalid recovered fill price')
                        event['fillPrice'] = delta_price
                        event['execNo'] = f'rest-{order.order_no}-{filled}'
                    made.on_order(event, at=clock_ms())
                    # A lost cancel acknowledgement cannot create a second NEW
                    # order. Retry only that original order after fresh evidence.
                    if order.open and order.cancel_sent and row.get('remaining', 0) > 0:
                        if clock_ms() - book.cancel_attempts.get(order.id, clock_ms()) >= 10000:
                            order.cancel_sent = False
                self._save()
            current = self._current_account(run)
            combined = {r['id']: r for r in owned}
            combined.update({r['id']: r for r in current['orders']})
            return {**current, 'orders': list(combined.values())}

    def recover(self, run_id, at):
        """Retry reads indefinitely; only a verified match releases the engine."""
        made = self._engine(run_id)
        if made.run.state == runs.ATTENTION:
            self.acknowledge(run_id, at)
        elif made.run.state == runs.RECONCILING:
            self.reconcile(run_id, at)
        else:
            self.audit(run_id, at)
        return made.run.state not in runs.QUIET_STATES and not made.book.uncertain
