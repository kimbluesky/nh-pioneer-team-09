"""지난 봉으로 돌려 보기 — 봉 체결 모형 `bar-v1`.

저장 전 설정이든 저장한 설정이든, **받아 둔 지난 봉**에 규칙을 대 보고 「그때
돌렸으면 어땠을까」를 셉니다. 아무것도 남기지 않습니다 — 전략·버전·실행 목록·
기록·한도 어디에도 쓰지 않고, 계좌에 닿지 않습니다.

## 판정은 실행과 같은 코드입니다

검증 실행이 쓰는 엔진(`engine.RunEngine`)을 그대로 세웁니다. 봉 하나를 체결
몇 개로 풀어 넣으면, 엔진은 실시간과 똑같이 봉을 닫고 규칙을 판정하고 손절·
익절·추적 청산을 봅니다. 그래서 같은 봉 목록이면 **규칙이 걸리는 봉은 실행과
같습니다.** 다른 것은 체결을 채우는 장부 하나입니다(`BarBook`).

## 봉 체결 모형 `bar-v1` — 지난 봉에는 호가가 없습니다

종이 실행의 모형(`paper.MODEL`)은 실시간 호가 잔량과 체결 흐름으로 채웁니다.
지난 봉에는 시가·고가·저가·종가뿐이라 그대로 쓸 수 없어, 따로 가정을 두고 이름을
붙입니다(`MODEL`). 답의 `assumptions` 가 같은 내용을 싣습니다.

1. **봉 안의 길** — 시가 → 불리한 극값 → 유리한 극값 → 종가 차례로 흘렀다고
   봅니다. 보유가 있으면 그 보유에 불리한 쪽이 먼저입니다(매수 보유는 저가 먼저).
   보유가 없으면 양봉은 저가 먼저, 음봉은 고가 먼저입니다. 그래서 한 봉에서
   손절과 익절이 함께 닿으면 **손절**입니다.
2. **값을 끼웁니다** — 한 구간을 흐르기 전에 그 구간 안에 놓인 손절·익절·추적
   청산 값과 걸어 둔 주문의 값을 점으로 끼웁니다. 손절은 그 값에서 발동합니다.
   시가가 이미 넘어 있으면(갭) 시가에서 발동합니다.
3. **시장가는 그 점의 값에 슬리피지를 더해** 즉시 전량 채웁니다. 규칙 진입은
   신호 다음 봉의 시가, 손절은 손절 값(갭이면 시가)이 그 점입니다. 슬리피지는
   불리한 쪽으로 `slippageTicks` 틱입니다.
4. **지정가는 낸 순간 이미 유리하면** 그 점의 값(슬리피지 포함)에 채우고, 아니면
   기다렸다가 **값이 지정가를 넘어갈 때만** 지정가에 채웁니다. 닿기만 해서는
   채우지 않습니다 — 그 가격에 먼저 서 있던 주문이 앞에 있기 때문입니다.
   FOK·FAK 는 낸 순간 채워지지 않으면 취소합니다.
5. **세션이 바뀌면 대기 주문을 거둡니다** — 종이 실행의 세션 마감과 같은
   자리(`engine.close_session`)입니다. 세션 끝 정리를 켠 전략은 **뒤에 다른 세션이
   이어지는** 세션에서만 정리합니다. 그 세션의 마지막 봉이 닫히는 시각을 끝으로
   봅니다. 자료의 마지막 세션은 아직 안 끝났을 수 있어 정리하지 않고, 남은 보유는
   마지막 종가로 평가해 따로 답합니다(`openPosition`).
6. **일봉 신호의 주문은 다음 세션 첫 체결에 나갑니다** — 실행과 같은
   규칙입니다(`engine.RunEngine._deferring`). 다음 봉의 시가가 그 점이고, 지정가·
   STOP 의 값은 판정한 봉의 종가로 셉니다. 자료의 마지막 봉에서 걸린 신호는
   다음 봉이 없어 나가지 않습니다.
   신규 진입 시간을 둔 일봉 전략은 **그 세션 안에 시간이 열린다고 보고** 다음 봉
   시가에 냅니다(`assumptions.dailyEntryWindow`). 일봉의 시각은 그날 0시라 그 시각으로
   대 보면 늘 시간 밖이기 때문입니다. 실행은 시간 안에 든 첫 체결의 값에 냅니다.
7. **증거금 예산 검사를 건너뛰는 전략**(해외 선물·스프레드처럼 계약당 증거금을
   모름, 해외 옵션 매도)은 실행과 같은 판정으로 건너뛰고 그 사실을 답에 적습니다
   (`assumptions.budgetUnchecked`).

세션의 개장 시각은 그 세션 첫 봉의 시각으로 봅니다(지난 봉에는 시간표가 없습니다).
봉 순서 조건은 그 값으로 셉니다. 호가 값(`bid1`·`ask1`)을 쓰는 조건은 지난 봉에 그
값이 없어 판정되지 않습니다.
만기일 진입 막기는 실행과 같습니다 — 봉의 거래일이 종목 만기일(마스터) 이상이면
신규 진입을 막습니다. 만기일을 모르면 막지 않습니다(`assumptions.expiryBlock`).
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from backend.sessions.hours import KST
from backend.strategy import bars as bar_lib
from backend.strategy import engine as engine_lib
from backend.strategy import paper, rules, runs
from backend.strategy.indicators import Bar
from backend.strategy.records import CURRENCIES

logger = logging.getLogger(__name__)

#: 모형 이름. 가정이 바뀌면 이름도 바꿉니다.
MODEL = "bar-v1"

#: 슬리피지(틱). 요청이 주지 않으면 이 값이고, 받는 범위는 0~`SLIPPAGE_MAX` 입니다.
SLIPPAGE_TICKS = 1
SLIPPAGE_MAX = 10

#: 한 번에 셈하는 봉 수 상한. 실행이 담아 두는 봉 수(`bars.BarBuilder`)와 같습니다.
MAX_BARS = 2000

#: 범위 — 마지막 몇 세션을 볼지. `None` 은 받은 만큼입니다.
RANGES: dict[str, int | None] = {"1d": 1, "3d": 3, "all": None}

#: 셈 시간 상한(초). 넘으면 멈추고 실패로 답합니다. 1분봉 1,701개·규칙 넷이 1초
#: 안쪽이라 느린 PC 를 헤아려 넉넉히 둡니다.
TIME_LIMIT_SECONDS = 3.0

#: 해외는 세션을 날짜로 가를 수 없습니다(한국 시각 자정을 넘깁니다). 봉 사이가
#: 이만큼(분) 넘게 비면 거기서 세션이 바뀐 것으로 봅니다.
OVERSEAS_SESSION_GAP_MINUTES = 45

#: 봉 안의 길. 답의 `assumptions.path` 입니다.
PATH = "open-adverse-favorable-close"

BUSY = "다른 백테스트가 돌고 있습니다. 끝난 뒤에 다시 셉니다."
WHY_TIME = "셈이 {seconds:g}초를 넘어 멈췄습니다."
WHY_RANGE = "범위는 1d·3d·all 가운데 하나입니다."
WHY_SLIPPAGE = f"슬리피지는 0~{SLIPPAGE_MAX} 틱의 정수입니다."
WHY_FEE = "계약당 수수료는 0 이상입니다."
WHY_TICK = "그 종목의 틱 크기를 몰라 셈할 수 없습니다."
WHY_BARS = "지난 봉을 받지 못했습니다."

#: 동시에 하나만 셉니다. 셈은 CPU 를 쓰고 봉 조회는 시세 제한을 씁니다.
_busy = threading.Lock()


class BacktestError(Exception):
    """셈을 못 한 까닭. `status` 는 라우트가 내는 HTTP 상태입니다."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


# ── 장부 ───────────────────────────────────────────────


class BarBook(paper.PaperBook):
    """봉 체결 모형으로 채우는 종이 장부. 얼굴은 `paper.PaperBook` 과 같습니다.

    엔진은 체결 하나(`on_trade`)를 넣은 뒤 바로 호가 자리(`on_book`)를 부르게
    되어 있고, 여기서는 그 둘을 이렇게 씁니다.

    - `on_trade` — 마지막 값을 적고, STOP 을 켜고, 기다리던 지정가가 관통됐으면
      지정가에 채웁니다.
    - `on_book` — 시장가와 켜진 STOP-M 을 지금 값 ± 슬리피지에 채우고, 막 도착한
      지정가는 이미 유리하면 채우고 아니면 줄을 세웁니다.
    """

    def __init__(self, *, tick: float, slippage_ticks: int) -> None:
        super().__init__(delay=0.0)
        self.slip = abs(float(tick)) * max(int(slippage_ticks), 0)

    def on_trade(self, trade: dict[str, Any], *, at: int) -> list[dict[str, Any]]:
        price = paper._float(trade.get("price"))
        if price is None:
            return []
        self.last_price = price
        made: list[dict[str, Any]] = []
        for order in self.open_orders:
            if not order.armed:
                self._arm(order, price)
                continue
            limit = order.price
            if order.order_type not in (paper.LIMIT_ORDER, paper.STOP_LIMIT):
                continue
            if not order.arrived or limit is None:
                continue
            through = price < limit if order.side == paper.BUY else price > limit
            if through:
                made.append(self._fill(order, limit, order.remaining, at))
        return made

    def on_book(self, book: dict[str, Any], *, at: int) -> list[dict[str, Any]]:
        last = self.last_price
        if last is None:
            return []
        made: list[dict[str, Any]] = []
        for order in self.open_orders:
            if not order.armed:
                continue
            price = self._worse(order.side, last)
            if order.order_type in (paper.MARKET_ORDER, paper.STOP_MARKET):
                made.append(self._fill(order, price, order.remaining, at))
                continue
            if order.arrived:
                continue
            order.arrived = True
            limit = order.price
            if limit is None:
                continue
            ready = price <= limit if order.side == paper.BUY else price >= limit
            if ready:
                made.append(self._fill(order, price, order.remaining, at))
            elif order.fill_condition in (paper.FILL_ALL_OR_NONE, paper.FILL_AND_KILL):
                order.status = paper.CANCELLED
                order.reason = f"{order.fill_condition} — 주문 즉시 채워지지 않아 취소했습니다"
        return made

    def waiting_lines(self) -> list[float]:
        """걸어 둔 주문이 움직일 값 — 기다리는 지정가와 아직 안 켜진 STOP 의 값."""
        made: list[float] = []
        for order in self.open_orders:
            if not order.armed and order.stop_price is not None:
                made.append(float(order.stop_price))
            elif order.arrived and order.price is not None:
                made.append(float(order.price))
        return made

    def _worse(self, side: str, price: float) -> float:
        return price + self.slip if side == paper.BUY else price - self.slip


# ── 엔진 ───────────────────────────────────────────────


class BacktestEngine(engine_lib.RunEngine):
    """지난 봉 셈에 쓰는 엔진. **판정·주문·감시는 실행 엔진 그대로**이고, 같은 값을
    다시 세지 않는 자리 둘만 다릅니다.

    실행 엔진은 봉이 닫힐 때마다 지표를 처음 봉부터 다시 세고 규칙을 처음 봉부터
    다시 굴립니다 — 앞쪽 봉이 다시 채워지거나 봉 수 상한에서 잘릴 수 있어서입니다.
    지난 봉 셈은 봉이 뒤에만 붙으므로 그럴 일이 없어,

    - 지표 열은 **받은 봉 전부로 한 번** 세어 두고 봉이 닫힌 만큼만 잘라 씁니다.
      지표는 그 봉까지의 값으로만 세므로(앞쪽 봉만 봄) 잘라 쓴 값이 그때 센
      값과 같습니다. 봉마다 적는 값(보유 등)은 닫힌 봉만큼 이어 붙입니다.
    - 규칙은 **바로 앞 봉까지 굴린 상태**에서 한 봉만 더 굴립니다. 앞 봉 상태가
      없거나 봉이 건너뛰었으면 처음부터 굴립니다.

    엔진이 세운 봉이 받은 봉과 하나라도 다르면 실행 엔진의 셈으로 돌아갑니다.
    """

    def __init__(
        self,
        run: runs.Run,
        *,
        every: list[Bar],
        trading_day: Callable[[int], Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(run, **kwargs)
        # 지난 봉에는 봉 안의 체결이 없습니다. 「틱」 전략도 봉 마감으로 셉니다
        # (`assumptions.evalBasis`).
        self.eval_tick = False
        self.trading_day = trading_day
        self._every = every
        self._full = rules.series_of(
            every,
            sessions=[run.session] * len(every),
            session=run.session,
            wanted=self._wanted,
            day_of=self._day_of if trading_day is not None else None,
        )
        self._recorded: dict[str, list[float | None]] = {
            name: [] for name in rules.RECORDED_IDS + (rules.BAR_ORDER,)
        }
        self._firsts: dict[int, int] = {}
        self._stages: dict[str, tuple[int, rules.Stage]] = {}

    def _series(self) -> rules.Series:
        have = self.bars.bars
        count = len(have)
        if not count or count > len(self._every) or have[-1] != self._every[count - 1] or have[0] != self._every[0]:
            self._stages.clear()
            return super()._series()
        for at in range(len(self._recorded[rules.BAR_ORDER]), count):
            row = self._mark_row(have[at], self.bars.sessions[at], self._firsts)
            for name, column in self._recorded.items():
                column.append(row.get(name))
        values = {name: column[:count] for name, column in self._full.values.items()}
        values.update({name: column[:count] for name, column in self._recorded.items()})
        return rules.Series(values=values, length=count, session=self._full.session)

    def _in_window_at(self, moment: int) -> bool:
        """일봉은 신규 진입 시간이 **그 세션 안에서 열린다고** 봅니다.

        일봉의 점은 그날 0시 무렵이라 그 시각으로 대 보면 늘 시간 밖이고, 미룬 진입이
        세션 내내 기다리다 버려집니다. 실행은 다음 세션의 실제 체결 시각으로 보아 시간
        안에 든 첫 체결에 냅니다 — 지난 일봉에는 그 체결이 없어 다음 봉 시가로 셉니다
        (`assumptions.dailyEntryWindow`). 분봉은 실행과 같은 셈입니다.
        """
        if self.bars.daily:
            return True
        return super()._in_window_at(moment)

    def _stage_of(
        self, key: str, rule: dict[str, Any], series: rules.Series, index: int
    ) -> tuple[rules.Stage, bool]:
        before = self._stages.get(key)
        if before is not None and before[0] == index - 1:
            stage, fired = rules.advance(before[1], rule, series, index)
        else:
            stage, fired = rules.stage_at(rule, series, index)
        self._stages[key] = (index, stage)
        return stage, fired


# ── 셈 ─────────────────────────────────────────────────


def compute(
    *,
    draft: dict[str, Any],
    market: str,
    code: str,
    session: str,
    bars: list[Bar],
    tick: float,
    multiplier: float = 1.0,
    margin: float | None = None,
    kind: str = "",
    qty: int = 1,
    fee_per_contract: float = 0.0,
    slippage_ticks: int = SLIPPAGE_TICKS,
    sessions: int | None = None,
    trading_day: Callable[[int], Any] | None = None,
    expiry: date | None = None,
    clock: Callable[[], float] = time.monotonic,
    time_limit: float = TIME_LIMIT_SECONDS,
) -> dict[str, Any]:
    """닫힌 봉 목록(`bars`, 시각 차례)으로 셈한 답.

    `sessions` 가 있으면 마지막 그만큼의 세션에서만 거래를 봅니다. 그 앞의 봉은
    지표 준비에만 씁니다. `clock` 은 시간 상한을 재는 데만 씁니다 — 답에는 봉
    시각만 들어가므로 같은 봉·같은 요청이면 언제 셈해도 같은 답입니다.

    `expiry` 는 종목 만기일입니다. 봉의 거래일이 그날 이상이면 실행과 같이 신규
    진입을 막습니다(만기일 진입 막기). 모르면 막지 않습니다.
    """
    aux = draft.get("aux") if isinstance(draft.get("aux"), dict) else {}
    run = runs.Run(
        id="backtest",
        version_id="",
        code=code,
        session=session,
        market=market,
        draft=draft,
        qty=max(int(qty), 1),
        fee_per_contract=float(fee_per_contract),
        multiplier=float(multiplier or 1.0),
        margin=margin,
        kind=kind,
        margin_budget=_number(aux.get("marginBudget")),
        daily_loss=_number(aux.get("dailyLoss")),
        state=runs.ARMED,
        mode=runs.PAPER,
    )
    book = BarBook(tick=tick, slippage_ticks=slippage_ticks)
    # 기록 자리를 주지 않습니다 — 셈한 것은 어디에도 남지 않습니다. 목록이 잘리지
    # 않게 넉넉히 둡니다(봉마다 이벤트 몇 줄).
    made = BacktestEngine(
        run,
        every=bars,
        trading_day=trading_day,
        tick=tick,
        history=len(bars) * 20 + 1000,
        book=book,
    )
    minutes = bar_lib.minutes_of(made.bars.base_bar)
    need = max(made.warmup, 1)
    keys = _session_keys(bars, market, session, minutes)

    start = _window_start(keys, sessions)
    begin = max(start, need - 1)
    answer = _blank(slippage_ticks, fee_per_contract, market, have=len(bars), need=need)
    answer["assumptions"]["expiryBlock"] = expiry.isoformat() if expiry is not None else None
    answer["assumptions"].update(_strategy_assumptions(made, aux, minutes))
    answer["bars"]["sessions"] = len(dict.fromkeys(keys))
    if len(bars) < need or begin >= len(bars):
        return answer

    made.seed([_row_of(one) for one in bars[:begin]], session)
    fed = bars[begin:]
    fed_keys = keys[begin:]
    ends = _session_ends(fed, fed_keys, minutes)
    flat_ms = _flat_minutes(aux) * 60_000

    began = clock()
    for index, bar in enumerate(fed):
        if index and fed_keys[index] != fed_keys[index - 1]:
            # 앞 세션이 끝났습니다. 종이 실행의 세션 마감과 같은 자리입니다.
            made.close_session(_closed_at(fed[index - 1], minutes))
        if expiry is not None:
            # 만기일 진입 막기 — 실행은 「오늘」로 세우고, 여기서는 그 봉의 거래일로
            # 세웁니다. 그 봉의 시가 점에서 앞 봉을 판정하므로 판정하는 날의 값입니다.
            slot = made._trading_slot(bar.timestamp, session)
            made.expiring = datetime.fromtimestamp(slot / 1000, tz=KST).date() >= expiry
        if not index or fed_keys[index] != fed_keys[index - 1]:
            # 세션의 개장 시각. 지난 봉에는 시간표가 없어 그 세션 첫 봉의 시각으로
            # 봅니다. 미룬 일봉 주문이 이 뒤의 첫 체결에서 나가고, 봉 순서도 여기서 셉니다.
            made.note_open(session, bar.timestamp)
        until = ends.get(fed_keys[index]) if flat_ms else None
        _walk(made, book, bar, session, until, flat_ms)
        if clock() - began > time_limit:
            raise BacktestError(WHY_TIME.format(seconds=time_limit), status=504)

    # 마지막 봉도 판정합니다. 그 신호의 주문은 다음 봉이 없어 채워지지 않습니다.
    with made.lock:
        made._close_bars(made.bars.close(), _closed_at(fed[-1], minutes))
    return _answer(answer, made, fed, minutes)


def _walk(
    made: engine_lib.RunEngine,
    book: BarBook,
    bar: Bar,
    session: str,
    until: int | None,
    flat_ms: int,
) -> None:
    """봉 하나를 점들로 풀어 엔진에 넣습니다. 점마다 1밀리초씩 뒤입니다."""
    at = bar.timestamp

    def point(price: float, quantity: int) -> None:
        nonlocal at
        if until is not None and until - flat_ms <= at < until:
            made.session_end(at, until)
            made.on_book({}, at=at)
        made.on_trade({"timestamp": at, "price": price, "quantity": quantity}, session, at=at)
        made.on_book({}, at=at)
        at += 1

    point(bar.open, int(bar.volume))
    held = made.ledger.position.side
    if held == paper.BUY:
        middle = (bar.low, bar.high)
    elif held == paper.SELL:
        middle = (bar.high, bar.low)
    else:
        middle = (bar.low, bar.high) if bar.close >= bar.open else (bar.high, bar.low)
    here = bar.open
    for there in (*middle, bar.close):
        for line in _between(here, there, made.exit_lines() + book.waiting_lines()):
            point(line, 0)
        point(there, 0)
        here = there


def _between(here: float, there: float, lines: list[float]) -> list[float]:
    """`here` 에서 `there` 로 가는 길 안에 놓인 값들을 지나는 차례로. 끝 두 값은 뺍니다."""
    low, high = min(here, there), max(here, there)
    inside = sorted({one for one in lines if low < one < high})
    return inside if there >= here else inside[::-1]


def _session_keys(bars: list[Bar], market: str, session: str, minutes: int | None) -> list[int]:
    """봉마다 그 봉이 든 세션의 번호. 번호가 바뀌는 자리가 세션 경계입니다.

    일봉은 봉 하나가 거래일 하나입니다. 국내 분봉은 거래일(야간은 다음 날)로
    가릅니다(`bars.day_slot_of`). 해외 분봉은 봉 사이가 비는 자리로 가릅니다.
    """
    if minutes is None:
        return list(range(len(bars)))
    if market == "domestic":
        return [bar_lib.day_slot_of(one.timestamp, session) for one in bars]
    made: list[int] = []
    number = 0
    gap = max(OVERSEAS_SESSION_GAP_MINUTES, minutes) * 60_000
    for index, one in enumerate(bars):
        if index and one.timestamp - bars[index - 1].timestamp > gap:
            number += 1
        made.append(number)
    return made


def _window_start(keys: list[int], sessions: int | None) -> int:
    """마지막 `sessions` 개 세션이 시작하는 봉의 자리. `None` 이면 0 입니다."""
    if not sessions or not keys:
        return 0
    kept = list(dict.fromkeys(keys))[-sessions:]
    first = kept[0]
    return keys.index(first)


def _session_ends(fed: list[Bar], keys: list[int], minutes: int | None) -> dict[int, int]:
    """뒤에 다른 세션이 이어지는 세션마다 그 끝(마지막 봉이 닫히는 시각).

    마지막 세션은 넣지 않습니다 — 아직 안 끝났을 수 있습니다. 일봉은 세션 끝
    정리를 하지 않습니다(봉 하나가 세션 하나라 끝 N분 앞을 봉으로 가를 수 없습니다).
    """
    if minutes is None:
        return {}
    last: dict[int, Bar] = {}
    for bar, key in zip(fed, keys):
        last[key] = bar
    order = list(last)
    return {key: _closed_at(last[key], minutes) for key in order[:-1]}


def _closed_at(bar: Bar, minutes: int | None) -> int:
    """봉이 닫히는 시각. 일봉은 그 봉의 마지막 점 바로 뒤입니다."""
    if minutes:
        return bar.timestamp + minutes * 60_000
    return bar.timestamp + 1_000


def _strategy_assumptions(
    made: engine_lib.RunEngine, aux: dict[str, Any], minutes: int | None
) -> dict[str, Any]:
    """그 전략의 설정에 따라 붙는 가정. 칸 뜻은 `_blank` 에."""
    sides = [
        paper.BUY if key == "buyEntry" else paper.SELL
        for key, _ in made._rules_on()
        if key in ("buyEntry", "sellEntry")
    ]
    unchecked = made.run.margin_budget is not None and any(
        runs.budget_skipped(made.run, side) for side in sides
    )
    windowed = any(str(aux.get(name) or "").strip() for name in ("entryStart", "entryEnd"))
    flat = _flat_minutes(aux) > 0
    return {
        "dailyEntryWindow": "next-session-open" if minutes is None and windowed else None,
        "budgetUnchecked": unchecked,
        "budgetUncheckedWhy": runs.WHY_BUDGET_UNCHECKED if unchecked else None,
        "sessionEndFlat": (
            None
            if not flat
            else ("not-applied-daily" if minutes is None else "followed-sessions-only")
        ),
    }


def _flat_minutes(aux: dict[str, Any]) -> int:
    if not aux.get("sessionEndFlat"):
        return 0
    value = _number(aux.get("sessionEndMinutes"))
    return int(value) if value and value > 0 else 0


# ── 답 ─────────────────────────────────────────────────


def _blank(
    slippage_ticks: int, fee_per_contract: float, market: str, *, have: int, need: int
) -> dict[str, Any]:
    return {
        "model": MODEL,
        "assumptions": {
            "delay": 0,
            "slippageTicks": int(slippage_ticks),
            "limitFill": "through",
            "sameBarStopFirst": True,
            "path": PATH,
            "sessionEnd": "followed-sessions-only",
            # 일봉 신호의 주문은 다음 세션 첫 체결(다음 봉 시가)에 나가고, 값은 판정한 봉의 종가로 셉니다.
            "dailyOrders": "next-session-first-trade",
            "dailyPriceBasis": "signal-close",
            # 만기일 진입 막기의 만기일(YYYY-MM-DD). 모르면 `None` 이고 막지 않습니다.
            "expiryBlock": None,
            "feePerContract": float(fee_per_contract),
            # 판정은 늘 봉 마감입니다. 평가 방식이 「틱」인 전략도 봉 안의 체결이 없어
            # 마감으로 셉니다 — 실제 실행과 신호 시각이 다를 수 있습니다.
            "evalBasis": "bar-close",
            # 지난 봉에 값이 없어 판정하지 못한 조건들(`unjudged`). 없으면 빈 목록입니다.
            "unjudged": [],
            # 일봉 + 신규 진입 시간: 시간이 그 세션 안에서 열린다고 보고 다음 봉 시가에
            # 냅니다(`next-session-open`). 실행은 시간 안의 첫 체결 값입니다. 해당 없으면 `None`.
            "dailyEntryWindow": None,
            # 증거금 예산 검사를 건너뛰었는지와 그 까닭(실행의 `budgetUnchecked` 와 같은 판정).
            "budgetUnchecked": False,
            "budgetUncheckedWhy": None,
            # 세션 끝 정리를 켠 전략만: 분봉은 뒤에 다른 세션이 이어지는 세션에서만
            # 정리(`followed-sessions-only`, 자료의 마지막 세션은 안 함), 일봉은 정리하지
            # 않음(`not-applied-daily`). 끈 전략은 `None` 입니다.
            "sessionEndFlat": None,
        },
        "bars": {"from": None, "to": None, "count": 0, "have": have, "need": need, "sessions": 0},
        "enough": have >= need,
        "signals": 0,
        "fills": 0,
        "trades": [],
        "pl": 0.0,
        "fee": 0.0,
        "wins": 0,
        "losses": 0,
        "winRate": None,
        "maxDrawdown": 0.0,
        "openPosition": None,
        "currency": CURRENCIES.get(market),
    }


def _answer(
    answer: dict[str, Any], made: engine_lib.RunEngine, fed: list[Bar], minutes: int | None
) -> dict[str, Any]:
    ledger = made.ledger
    # 거래를 닫은 체결의 까닭. 같은 시각의 청산 체결이 그 거래를 닫았습니다.
    reasons = {
        one["time"]: one.get("reason")
        for one in made.fills
        if one.get("intent") == "exit"
    }
    trades = [
        {
            "openedAt": one["openedAt"],
            "closedAt": one["closedAt"],
            "side": one["side"],
            "quantity": one["quantity"],
            "entryPrice": one["avgEntry"],
            "exitPrice": one["avgExit"],
            "reason": reasons.get(one["closedAt"]),
            "grossPl": one["grossPl"],
            "fee": one["fee"],
            "pl": one["netPl"],
        }
        for one in ledger.trades
    ]
    peak = total = drawdown = 0.0
    for one in trades:
        total += one["pl"]
        peak = max(peak, total)
        drawdown = max(drawdown, peak - total)

    held = ledger.position
    last = fed[-1].close
    wins = sum(1 for one in trades if one["pl"] > 0)
    answer.update(
        {
            "bars": {
                **answer["bars"],
                "from": fed[0].timestamp,
                "to": fed[-1].timestamp,
                "count": len(fed),
            },
            "signals": sum(1 for one in made.events if one["kind"] == engine_lib.SIGNAL),
            "fills": len(made.fills),
            "trades": trades,
            # 수수료를 뺀 값입니다. 열린 보유의 진입 수수료도 냈으므로 여기 듭니다.
            "pl": ledger.realized - ledger.fee,
            "fee": ledger.fee,
            "wins": wins,
            "losses": sum(1 for one in trades if one["pl"] < 0),
            "winRate": wins / len(trades) if trades else None,
            "maxDrawdown": drawdown,
            "openPosition": (
                {
                    "side": held.side,
                    "quantity": held.size,
                    "avgPrice": held.avg_price,
                    "lastPrice": last,
                    "openPl": ledger.unrealized(last),
                }
                if held.side is not None
                else None
            ),
        }
    )
    answer["assumptions"]["unjudged"] = unjudged(made)
    return answer


#: 판정하지 못한 조건이 규칙에 미치는 것. 조건 하나는 거짓으로 읽히고(`rules.cond_at`)
#: 단계·규칙을 건너뛰지는 않습니다. 그래서 단계가 묶는 방식에 따라 갈립니다.
#: 「모두」 단계는 늘 안 채워집니다.
STEP_BLOCKED = "stepBlocked"
#: 「하나라도」인 1단계는 다른 조건이 참이어도 안 채워집니다 — 1단계는 직전 봉에서
#: 거짓이다가 참이 되는 것만 보는데, 직전 봉에 그 값이 없으면 바뀐 것으로 치지
#: 않습니다. 지난 봉에는 그 값이 늘 없어 규칙이 걸리지 않습니다.
RULE_NEVER_FIRES = "ruleNeverFires"
#: 「하나라도」인 2단계부터는 그 조건만 거짓이고 다른 조건으로 채워질 수 있습니다.
COND_FALSE = "condFalse"


def unjudged(made: engine_lib.RunEngine) -> list[dict[str, Any]]:
    """지난 봉에 값이 없어 **판정하지 못한 조건**들. 호가처럼 실행이 그 순간 적는 값입니다.

    판정과 같은 자리(`rules.cond_state`)가 모든 봉에서 「모름」이라 답한 조건 가운데,
    실행이 적는 값(`rules.CONTEXT_IDS`)을 쓰는 것만 듭니다 — 기간이 받은 봉보다 긴
    지표처럼 다른 까닭으로 값이 없는 조건은 들지 않습니다. 줄마다 규칙·단계(1부터)·
    조건(1부터)·그 값 이름·미치는 것(`effect`)입니다.
    """
    series = made._series()
    made_rows: list[dict[str, Any]] = []
    for key, rule in made._rules_on():
        for place, step in enumerate(rules.steps_of(rule)):
            joined_any = str(step.get("join") or "all") == "any"
            effect = (RULE_NEVER_FIRES if place == 0 else COND_FALSE) if joined_any else STEP_BLOCKED
            for number, cond in enumerate(step.get("conds") or []):
                if not isinstance(cond, dict):
                    continue
                names = [str(cond.get("left") or "")]
                if str(cond.get("rightKind") or "") != "number":
                    names.append(str(cond.get("right") or ""))
                context = [name for name in names if name in rules.CONTEXT_IDS]
                if not context:
                    continue
                if any(
                    rules.cond_state(cond, series, index) is not None
                    for index in range(series.length)
                ):
                    continue
                made_rows.append(
                    {
                        "rule": key,
                        "step": place + 1,
                        "cond": number + 1,
                        "operand": context[0],
                        "effect": effect,
                    }
                )
    return made_rows


# ── 봉 받기 ─────────────────────────────────────────────


def gather(
    service: Any,
    *,
    market: str,
    code: str,
    session: str,
    base_bar: str,
    exchange: str,
    now: int,
) -> list[Bar]:
    """셈할 닫힌 봉들. 시각 차례이고 많아야 `MAX_BARS` 개입니다.

    같은 시장·종목·세션·기준 봉으로 도는 실행이 있으면 그 실행이 받아 둔 봉을
    씁니다. 없으면 실행이 준비 봉을 받는 그 함수(`service._fetch`)로 한 번 받습니다
    — 같은 시세 제한을 지납니다. 아직 닫히지 않은 봉(`now` 가 그 안)은 뺍니다.
    """
    minutes = bar_lib.minutes_of(base_bar)
    for made in service.engines():
        if (made.run.market, made.run.code, made.run.session, made.bars.base_bar) != (
            market,
            code,
            session,
            base_bar,
        ):
            continue
        with made.lock:
            kept = [
                one
                for one, context in zip(made.bars.bars, made.bars.sessions)
                if made.bars.daily or context == session
            ]
        if kept:
            return _closed_only(kept, minutes, session, now)[-MAX_BARS:]

    asked = runs.Run(
        id="backtest", version_id="", code=code, session=session, market=market, exchange=exchange
    )
    try:
        rows = service._fetch(asked, minutes, MAX_BARS)
    except Exception as exc:  # noqa: BLE001 — 까닭은 기록에, 화면에는 한 문장만 냅니다.
        logger.warning("백테스트 봉을 받지 못했습니다.", exc_info=True)
        raise BacktestError(WHY_BARS, status=502) from exc
    made_bars: dict[int, Bar] = {}
    for row in rows or []:
        bar = bar_lib._bar_of(row)
        if bar is not None:
            made_bars[bar.timestamp] = bar
    ordered = [made_bars[when] for when in sorted(made_bars)]
    return _closed_only(ordered, minutes, session, now)[-MAX_BARS:]


def _closed_only(bars: list[Bar], minutes: int | None, session: str, now: int) -> list[Bar]:
    if minutes:
        return [one for one in bars if one.timestamp + minutes * 60_000 <= now]
    today = bar_lib.day_slot_of(now, session)
    return [one for one in bars if one.timestamp < today]


def run_one(work: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """셈 하나를 돌립니다. **동시에 하나만** — 돌고 있으면 409 로 거절합니다."""
    if not _busy.acquire(blocking=False):
        raise BacktestError(BUSY, status=409)
    try:
        return work()
    finally:
        _busy.release()


# ── 작은 것 ─────────────────────────────────────────────


def _row_of(bar: Bar) -> dict[str, Any]:
    return {
        "timestamp": bar.timestamp,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }


def _number(value: Any) -> float | None:
    try:
        made = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return made if made == made else None
