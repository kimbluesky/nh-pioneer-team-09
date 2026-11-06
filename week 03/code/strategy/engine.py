"""검증 실행 하나를 돌리는 루프.

봉·종이 장부·가상 장부를 한데 묶는 자리입니다. 받는 것은 **허브가 내는 메시지
그대로**이고(`quote` 의 체결, `book` 의 호가, `status`), 내는 것은 화면이 그리는
줄입니다. 조회를 부르거나 구독을 거는 것은 여기서 하지 않습니다 — 그 일을 함께
지면 봉 하나를 손으로 지어 넣고 결과를 보는 시험을 쓸 수 없습니다.

## 한 봉이 닫히면 보는 차례

    1. 청산 규칙 — 보유 방향에 맞는 것 하나. 걸리면 **그 봉의 진입은 보지 않습니다.**
    2. 진입 규칙 — 보유가 없으면 둘 다, 있으면 같은 방향 하나만.

틱마다 보는 것은 손절·추적 청산·익절입니다. **마지막 체결가**가 기준가에서
틱 거리만큼 움직이면 시장가 전량으로 닫습니다. 같은 틱에 여럿이 함께 닿으면
**손절 → 추적 청산 → 익절** 차례로 하나만 적습니다 — 어느 쪽이 먼저였는지 모르는데
좋은 쪽을 골라 적으면 성적이 부풀어 오릅니다.

추적 청산의 기준은 **보유가 열린 뒤 가장 유리했던 값**입니다. 보유 평균가에서
출발해 유리한 쪽으로 움직인 만큼 따라가고, 거기서 정한 틱만큼 되돌면 닫습니다.

## 두 곳에서 들어옵니다

시세는 **이벤트 루프**가 넣고, 중지·청산·종료는 **라우트 스레드**가 부릅니다.
그래서 엔진마다 잠금(`lock`)을 하나 두고 들어오는 자리마다 겁니다. 루프로
넘겨 순서를 세우지 않는 것은, 라우트가 답을 바로 내놓아야 하는데 루프에 줄을
서면 **느린 실행 하나가 모든 라우트를 기다리게** 하기 때문입니다.

## 값이 없으면 판정하지 않습니다

지표가 설 만큼 봉이 모이지 않았으면 「준비 중」만 남기고 넘어갑니다. 시세가
끊긴 동안(`GAP_SECONDS`)도 판정·체결·감시를 멈춥니다. 모르는 구간을 아는 것처럼
메우면 없던 신호와 없던 체결이 생깁니다.
"""
from __future__ import annotations

import math
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from backend.sessions.hours import KST
from backend.strategy import bars as bar_lib
from backend.strategy import indicators, paper, rules, runs, sim, validate
from backend.strategy.runs import Ledger, Run

#: 오프셋. 화면의 `PRICE_BASES` 와 같은 말이고 값은 **틱 수**입니다.
#:
#: 부호는 **공격 방향**입니다 — `+1틱` 은 체결되기 쉬운 쪽이라 매수는
#: 한 칸 위, 매도는 한 칸 아래입니다. 「싸게 사고 비싸게 판다」로 읽으면 같은
#: 설정이 두 방향에서 반대로 움직입니다.
PRICE_OFFSETS: dict[str, int] = {"신호가": 0, "신호가 + 1틱": 1, "신호가 − 1틱": -1}

#: 값 칸을 쓰지 않는 주문유형. 가격 없이 나갑니다.
NO_PRICE = (paper.MARKET_ORDER, paper.STOP_MARKET)

#: 스탑 가격을 함께 받는 주문유형. 화면의 `needsStopFor` 와 같은 규칙입니다.
STOP_TYPES = (paper.STOP_MARKET, paper.STOP_LIMIT)

# 이벤트 갈래. 화면이 「조건·이벤트」 표에서 가리는 값입니다.
READY = "ready"
SIGNAL = "signal"
#: 평가 방식 「틱」 — 체결 시세마다 **진행 중인 봉까지** 넣어 판정합니다.
#: 그 밖의 값은 봉이 닫힐 때 판정합니다(「마감」).
EVAL_TICK = "틱"
#: 「틱」 판정에서 난 것에 다는 표지 칸들(`RunEngine._tick_mark`).
TICK_MARK_FIELDS = ("evalMode", "partialBar", "judgedAt", "barTime")
ORDER = "order"
FILL = "fill"
BLOCKED = "blocked"
GAP = "gap"
STATE = "state"
SESSION = "session"
RECONCILE = "reconcile"
#: 실행을 시작한 첫 판정 봉에서 1단계가 **이미 참**이라 전이를 기다리는 규칙.
#: 글은 규칙 이름이고, 문장은 화면이 씁니다. 규칙마다 한 번입니다.
ALREADY_TRUE = "already-true"

#: 조건 표의 상태. 판정한 봉·준비 봉이 모자란 봉·시세가 끊긴 봉입니다.
TABLE_READY = "ready"
TABLE_WARMUP = "warmup"
TABLE_GAP = "gap"

#: 체결이 한동안 안 올 때의 까닭. 시장이 조용한 것일 수도 있습니다.
NO_TRADE = "체결이 오지 않습니다"
#: 시장은 흐르는데 이 실행만 못 받을 때 적는 말. 무엇을 하면 되는지까지 적습니다.
FEED_LOST = "시세 연결이 끊긴 것 같습니다 — 실행을 멈췄다 재개해 주세요"
#: 「확인 필요」 동안 시세를 보지 않았던 공백을 확인 뒤에 적는 말(`after_attention`).
ATTENTION_GAP = "확인을 기다리는 동안 시세를 받지 않았습니다"

#: 청산이 왜 나갔는지. 화면이 그대로 적습니다.
#:
#: **`RULE_EXIT` 만 버전의 주문유형으로 나갑니다.** 나머지는 「지금 닫는다」는
#: 뜻이라 시장가 전량입니다 — 손절을 지정가로 걸면 값이 지나가 버려 안 닫힙니다.
STOP_LOSS = "stop"
TRAIL_STOP = "trail"
TAKE_PROFIT = "take"
RULE_EXIT = "exit"
#: 보유 중에 반대 방향 진입 규칙이 걸려 닫은 청산(`aux.oppositeSignal` 이 청산·반전).
#: 청산 규칙과 같이 버전의 주문유형과 체결조건으로 나갑니다.
OPPOSITE_EXIT = "opposite"
FLATTEN = "flatten"
#: 시장가 전량 청산(손절·익절·추적·세션 끝·사용자 청산)의 국내 체결조건. **늘 FAS** 입니다 —
#: FOK·FAK 면 한 번에 다 채워지지 않을 때 남은 수량이 취소돼 보유가 남습니다.
EXIT_FILL_CONDITION = "FAS"
#: 세션 끝 N분 전에 보유를 모두 닫은 청산.
SESSION_END = "session-end"

#: 손절·익절·추적 청산 거리의 단위. 값은 `stopTicks`·`takeTicks`·`trailTicks` 칸에
#: 있습니다 — **칸 이름은 옛 것이고 뜻은 단위가 정합니다.** 단위가 없으면 틱입니다.
#:
#:     tick    틱 수
#:     price   가격 거리
#:     pct     기준 값(손절·익절은 평균가, 추적은 가장 유리했던 값)의 %
#:     atr     마지막으로 닫힌 판정 봉의 ATR(14) 의 배수 — 봉마다 갱신됩니다
UNIT_TICK = "tick"
UNIT_PRICE = "price"
UNIT_PCT = "pct"
UNIT_ATR = "atr"
UNITS = (UNIT_TICK, UNIT_PRICE, UNIT_PCT, UNIT_ATR)

#: 거리 칸과 그 단위 칸.
_DISTANCES = (("stopTicks", "stopUnit"), ("takeTicks", "takeUnit"), ("trailTicks", "trailUnit"))

#: ATR 단위가 쓰는 지표. 기간은 고정입니다.
ATR_FOR_UNIT = ("atr", {"period": 14})

#: 기록 파일에 남기는 줄 갈래 둘 — 거래일 잔고와 주문 결과.
DAY_ROW = "day"
ORDER_ROW = "order"

#: 주문 결과. 주문 결과 줄의 `outcome` 입니다.
#:
#:     filled     전량 체결
#:     partial    일부 체결된 뒤 나머지가 취소·만료됨
#:     cancelled  체결 없이 취소·만료됨
#:     rejected   계좌가 거절함(모의 계좌 알림의 상태 글자로 가림)
#:     refused    접수되기 전에 막힘(승인 없음·환경 다름·답 없음 같은 것)
FILLED = "filled"
PARTIAL = "partial"
CANCELLED = "cancelled"
REJECTED = "rejected"
REFUSED = "refused"

#: 주문 거부로 실행을 중지하면서 열린 진입 주문을 거둘 때 그 주문에 적는 까닭.
HALT_WITHDRAW_TEXT = "주문 거부로 중지하면서 취소했습니다"

#: 진입 체결에 적는 까닭. 청산 넷과 **한 갈래의 값**입니다.
ENTRY_REASON = "signal"

#: 일봉 주문을 미룰 때와 미룬 것을 버릴 때의 말(`RunEngine._deferring`).
DEFERRED_TEXT = "일봉 신호 — 다음 세션 첫 체결에서 주문합니다"
WHY_DEFERRED_STATE = "실행 상태가 바뀌어 미룬 진입을 내지 않았습니다"
WHY_DEFERRED_EXIT = "보유가 바뀌어 미룬 청산을 내지 않았습니다"
WHY_DEFERRED_EXPIRED = "다음 세션에 내지 못해 미룬 주문을 버렸습니다"

#: 반전 예약을 버릴 때의 말. 반대 신호로 낸 청산이 보유를 다 닫기 전에 청산이
#: 끝나거나(일부 체결·취소), 실행 상태가 바뀌거나, 세션이 끝나거나, 다른 규칙이
#: 걸린 때입니다(`RunEngine._reverse`).
WHY_REVERSE_DROPPED = "보유가 다 안 닫혀 반전 진입을 내지 않았습니다"

#: 규칙이 걸렸는데 주문을 내지 않은 사건(`BLOCKED`)의 글 머리. 뒤에 까닭이 붙습니다.
UNSENT_HEAD = "조건이 참이었지만 주문을 내지 않았습니다 — "

#: 주문을 내지 않은 까닭의 코드. 사건 줄의 `reason` 칸입니다 — 화면은 글 대신
#: 이 코드로 가립니다(한도 정지 등). 까닭 글이 **이 표의 글과 같을 때만** 짝짓고,
#: 표에 없는 글은 `other` 입니다. 주문 거절은 거절한 자리가 코드를 직접 적습니다
#: (`sim.SimRefused.reason`).
BLOCK_REASONS: dict[str, str] = {
    runs.WHY_STATE: "state",
    runs.WHY_GAP: "feed-gap",
    runs.WHY_EXPIRY: "expiry",
    runs.WHY_DAILY: "daily-orders",
    runs.WHY_DAILY_LOSS: "daily-loss",
    runs.WHY_LOSS_STREAK: "loss-streak",
    runs.WHY_OPPOSITE: "opposite",
    runs.WHY_ADD: "add-while-open",
    runs.WHY_MAX_HOLD: "max-hold",
    runs.WHY_MARGIN_UNKNOWN: "margin-unknown",
    runs.WHY_BUDGET: "margin-budget",
    runs.WHY_SESSION_END: "session-end",
    runs.WHY_ENTRY_WINDOW: "entry-window",
    runs.WHY_REENTRY_WAIT: "reentry-wait",
    runs.WHY_DAILY_ENTRIES: "daily-entries",
    runs.WHY_ATR_NOT_READY: "atr-not-ready",
    WHY_DEFERRED_STATE: "deferred-state",
    WHY_DEFERRED_EXIT: "deferred-exit",
    WHY_DEFERRED_EXPIRED: "deferred-expired",
    WHY_REVERSE_DROPPED: "reverse-dropped",
}
OTHER_REASON = "other"

#: 까닭 → 사람이 읽는 말.
#:
#: **체결 줄의 `reason` 에는 안 씁니다.** 거기 실리는 것은 값이고, 화면이
#: 제 말로 옮깁니다 — 「진입 · 조건 충족」처럼 갈래와 까닭을 붙여 세우는
#: 규칙이 화면 것이라, 백엔드가 말을 정해 보내면 두 자리가 어긋납니다.
#: 여기 표는 **실행에 남기는 문장**에만 씁니다. 그 문장은 값이 아니라 산문이라
#: 「stop — 매도 1계약 시장가 전량」처럼 섞여 나오면 읽히지 않습니다.
REASON_LABELS: dict[str, str] = {
    ENTRY_REASON: "조건 충족",
    STOP_LOSS: "손절",
    TRAIL_STOP: "추적 청산",
    TAKE_PROFIT: "익절",
    RULE_EXIT: "청산 규칙",
    OPPOSITE_EXIT: "반대 신호",
    FLATTEN: "사용자 청산",
    SESSION_END: "세션 끝 정리",
}

#: 계약당 값이 **어떻게 나온 것인지**. 화면이 이 값으로 문구를 고릅니다.
#:
#: 숫자만 내면 화면이 어림을 확정값으로 읽습니다. 그래서 값과 **근거를 함께**
#: 냅니다 — 이름만 보고도 어떤 값인지 알 수 있는 이름입니다.
#:
#:     margin        계약당 개시증거금. 품목별세부정보에서 온 **고정값**
#:     premium-now   프리미엄 × 승수를 **지금 값으로 잰 것**. 틱마다 바뀝니다
#:     sell-unknown  옵션 매도 — 거래소가 셈하는 값이라 앱이 못 냅니다
#:     mixed         진입 방향이 갈려 하나로 못 냅니다(옵션 매수·매도가 함께 켜짐)
#:     no-margin-field  해외 선물·스프레드 — 조회에 계약당 증거금 칸이 없어 못 냅니다.
#:                   이때 증거금 예산 검사는 하지 않습니다(`runs.budget_unchecked`)
#:
#: `margin`·`premium-now` 인데 값이 비면 **그 방법으로는 셀 수 있는데 재료가
#: 없는 것**입니다(표를 아직 못 받았거나, 체결가가 아직 없거나).
BASIS_MARGIN = "margin"
BASIS_PREMIUM_NOW = "premium-now"
BASIS_NO_MARGIN_FIELD = "no-margin-field"
BASIS_SELL_UNKNOWN = "sell-unknown"
BASIS_MIXED = "mixed"

#: 규칙 이름 → 사람이 읽는 말.
#:
#: **실행에 남기는 문장에만 씁니다.** 규칙 이름은 설정의 열쇠라 값으로는 그대로
#: 두고(`rules` 의 칸 이름, 저장 검사의 `field`), 사람이 읽는 자리에서만 옮깁니다 —
#: 체결 줄의 까닭과 같은 규칙입니다(`REASON_LABELS`). 「buyEntry 규칙이
#: 걸렸습니다」처럼 설정의 열쇠 이름을 화면에 그대로 세우지 않습니다.
#:
#: **말은 화면의 `strategyRules.ts` 와 같습니다.** 둘이 갈리면 설정 화면의
#: 「매수 청산」과 실행 이벤트의 다른 말이 같은 규칙을 가리키게 됩니다.
RULE_LABELS: dict[str, str] = {
    "buyEntry": "매수 진입",
    "sellEntry": "매도 진입",
    "buyExit": "매수 청산",
    "sellExit": "매도 청산",
}

_ENTRY_RULES = ("buyEntry", "sellEntry")
_EXIT_OF = {paper.BUY: "buyExit", paper.SELL: "sellExit"}
_SIDE_OF = {"buyEntry": paper.BUY, "sellEntry": paper.SELL}
#: 청산은 보유와 **반대 방향**으로 나갑니다.
_CLOSE_SIDE = {paper.BUY: paper.SELL, paper.SELL: paper.BUY}


def order_price(signal: float, side: str, basis: str, tick: float) -> float:
    """신호가에 오프셋을 얹고 틱 격자에 맞춘 값.

    격자에 안 맞는 값은 **공격 방향으로 한 칸** 맞춥니다 — 매수는 올리고 매도는
    내립니다. 반대로 맞추면 한 틱 차이로 안 채워지는 주문이 됩니다.
    """
    steps = PRICE_OFFSETS.get(str(basis or "").strip(), 0)
    tick = abs(float(tick)) or 1.0
    way = 1 if side == paper.BUY else -1
    made = signal + steps * tick * way
    grid = made / tick
    # 0.05 같은 틱은 이진수로 딱 떨어지지 않아 격자 위의 값도 한 칸 어긋나게
    # 나옵니다. 거의 정수면 정수로 봅니다 — 안 그러면 「신호가」가 한 틱 밀립니다.
    if abs(grid - round(grid)) < 1e-9:
        grid = float(round(grid))
    return (math.ceil(grid) if way > 0 else math.floor(grid)) * tick


class RunEngine:
    """실행 하나. 허브 메시지를 넣으면 이벤트·체결이 쌓입니다."""

    def __init__(
        self,
        run: Run,
        *,
        tick: float = 1.0,
        delay: float = paper.DELAY_SECONDS,
        gap_seconds: float = runs.GAP_SECONDS,
        history: int = 500,
        record: Callable[[str, dict[str, Any]], None] | None = None,
        book: Any = None,
    ) -> None:
        self.run = run
        #: 남길 것이 생길 때마다 부릅니다 — `(갈래, 줄)`. 안 주면 안 남깁니다.
        #: 엔진이 파일을 직접 다루지 않는 것은, 봉 하나를 넣어 보는 시험이
        #: 디스크를 건드리지 않게 하려는 것입니다.
        self.record = record
        #: 루프와 라우트 스레드가 함께 들어오는 자리를 지킵니다 — 까닭은 첫머리에.
        self.lock = threading.RLock()
        self.tick = abs(float(tick)) or 1.0
        self.gap_ms = int(gap_seconds * 1000)
        self.aux = run.draft.get("aux") if isinstance(run.draft.get("aux"), dict) else {}
        self.rules = run.draft.get("rules") if isinstance(run.draft.get("rules"), dict) else {}
        self.bars = bar_lib.BarBuilder(str(run.draft.get("baseBar") or ""))
        #: 주문을 채우는 자리. 종이는 이 앱이 지어내고(`paper.PaperBook`),
        #: 모의는 계좌가 채웁니다(`sim.SimBroker`). **엔진이 보는 얼굴은
        #: 하나**라 여기만 갈아 끼우면 나머지가 그대로 돕니다.
        self.book = book if book is not None else paper.PaperBook(delay=delay)
        # 승수를 모르면 장부는 1포인트 = 1 로 셈하되, 밖으로 내는 손익은 모름입니다
        # (`money_known`) — 1 로 센 값을 돈인 척 내지 않습니다.
        self.ledger = Ledger(
            multiplier=run.multiplier if run.multiplier is not None else 1.0,
            fee_per_contract=run.fee_per_contract,
        )
        self.events: list[dict[str, Any]] = []
        self.fills: list[dict[str, Any]] = []
        self.curve: list[dict[str, Any]] = []
        #: 마지막으로 체결을 본 시각. 공백 판정이 보는 값입니다.
        self.last_trade_at = 0
        self.feed = "live"
        #: 실시간으로 마지막에 본 누적 거래량. 체결이 안 올 때 시장이 흘렀는지를
        #: 현재가 조회와 견주는 값입니다. 아직 못 봤으면 `None`.
        self.last_volume: int | None = None
        #: 「체결이 오지 않습니다」 공백에 든 시각. 그 공백이 아니면 0 입니다.
        self.quiet_since = 0
        #: 이 공백에 시장이 흘렀는지 이미 물어봤는지. 공백 한 번에 한 번만 묻습니다.
        self.probe_asked = False
        #: 「확인 필요」 동안 든 공백이라 아직 사건으로 적지 않았는지(`_gap`).
        self._gap_unsaid = False
        #: 구독을 NH선물 쪽에 다시 걸어 달라는 표지. 재개할 때 답니다 — 서버에서
        #: 조용히 끊긴 구독은 멈췄다 재개하는 것만으로는 되살아나지 않습니다.
        self.refresh_wanted = False
        #: 만기일인지. 참이면 신규 진입을 막습니다 — 값은 종목 색인이 주고
        #: 실행 목록(`service.py`)이 날마다 다시 세워 줍니다.
        self.expiring = False
        self._history = max(int(history), 1)
        #: 판정에 필요한 봉 수. 화면이 「준비 중 — 필요 N」으로 씁니다.
        self.warmup = _warmup_of(self.rules)
        self._told_ready = False
        #: 마지막 판정 봉의 조건 표(`condition_table`). **마지막 한 벌만** 들고
        #: 있고 봉이 닫힐 때마다 통째로 갈아 끼웁니다. 기록·파일에는 쓰지
        #: 않습니다 — 첫 판정 봉 전과 앱을 다시 켠 뒤에는 `None` 입니다.
        self._conditions: dict[str, Any] | None = None
        #: 판정한 봉이 있었는지. 「시작할 때 이미 참」을 첫 판정 봉에서만 냅니다.
        self._judged_once = False
        #: 「안 낸 신호」를 센 거래일(KST 날짜)과 그 수. **규칙 신호에서 난 막힘만**
        #: 셉니다 — 손절·익절·추적 청산·사용자 청산이 막힌 것은 이벤트만 남깁니다.
        self._unsent_day = ""
        self._unsent_count = 0
        #: 이 실행이 선 뒤의 성과 수. 규칙이 걸린 횟수(안 낸 신호 포함)·체결 줄 수·
        #: 마지막으로 걸린 시각입니다. 이벤트·체결 목록은 잘리므로 수를 따로 들고
        #: 있습니다. 앱을 다시 켜 되살린 실행은 앞의 수를 모르므로 `None` 입니다
        #: (`forget_stats`).
        self.signal_count: int | None = 0
        self.fill_count: int | None = 0
        self.last_signal_at: int | None = None
        #: 지금 규칙 판정 중인지. 이 동안 난 막힘이 「안 낸 신호」입니다.
        self._judging = False
        #: 세션 마감에서 봉을 닫는 중인지. 이 동안 걸린 일봉 규칙은 주문을 미룹니다.
        self._closing_session = False
        #: 봉 시각 → 거래일을 답하는 자리. 해외 실행에만 답니다(`service._stand`) —
        #: 해외 VWAP 의 세션을 시간표의 거래 창으로 가릅니다. 없으면 `bars.day_slot_of`.
        self.trading_day: Callable[[int], Any] | None = None
        #: 봉 시각마다 받은 거래일. **새 봉에 한 번만** 묻습니다(`_day_of`).
        self._days: dict[int, Any] = {}
        #: 되살린 실행의 거래일 열쇠를 처음 봉에서 한 번 맞춰 봤는지(`_on_bar`).
        self._day_checked = False
        #: 끊긴 뒤 첫 체결인지. 그 체결의 손절·익절은 그 값에 바로 채웁니다(`paper` 가정 9).
        self._after_gap = False
        #: 미룬 일봉 주문. `entry`·`exit` 마다 많아야 하나이고 메모리에만 둡니다 —
        #: 앱을 다시 켜면 사라집니다(되살린 실행은 중지·대사로 섭니다).
        self._deferred: dict[str, dict[str, Any]] = {}
        #: 반전 예약. 반대 신호로 낸 청산(`order`)이 보유를 다 닫으면 `side` 로 진입합니다.
        #: `rule` 은 그 신호를 낸 진입 규칙입니다. 메모리에만 둡니다.
        self._reverse: dict[str, str] | None = None
        #: 보유를 마지막으로 0 으로 만든 체결의 주문 id. 반전은 그 주문이 예약된
        #: 청산일 때만 들어갑니다(`_turn`).
        self._flat_by = ""
        #: 아직 안 닫힌 주문이 무엇을 하려던 것인지. `orderId` → `entry`/`exit`.
        self._purpose: dict[str, str] = {}
        #: 아직 안 닫힌 주문을 낸 순간의 지표 값. `orderId` → 체결 줄의 `snapshot`.
        #: `_purpose` 와 같은 때에 담고 같은 때에 비웁니다(`_forget`).
        self._snap: dict[str, dict[str, Any]] = {}
        #: 체결 줄의 `snapshot` 에 싣는 값의 이름들 — 규칙 넷이 쓰는 피연산자와
        #: 종가. 버전은 실행 중에 바뀌지 않으므로 한 번만 셉니다.
        self._snap_ids = _snapshot_ids(self.rules)
        #: 규칙들이 쓰는 지표 `(id, 매개변수)`. 봉마다 이것만 셉니다(`rules.wanted_of`).
        self._wanted = rules.wanted_of(self.rules)
        #: ATR 단위를 쓰는지. 쓰면 ATR(14) 를 함께 세고, 그것이 설 때까지 진입을
        #: 막습니다 — 손절을 걸 수 없는 보유를 만들지 않습니다. 값을 비워 둔 칸은
        #: 그 청산을 쓰지 않는 것이라 단위가 ATR 이어도 세지 않습니다.
        self._uses_atr = any(
            not _blank(self.aux.get(value)) and str(self.aux.get(unit) or "") == UNIT_ATR
            for value, unit in _DISTANCES
        )
        if self._uses_atr:
            key = rules.operand_key(*ATR_FOR_UNIT)
            if all(rules.operand_key(*one) != key for one in self._wanted):
                self._wanted.append(ATR_FOR_UNIT)
            self.warmup = max(self.warmup, rules.warmup_of(*ATR_FOR_UNIT))
        #: 마지막으로 닫힌 판정 봉의 ATR(14). ATR 단위가 아니거나 아직 없으면 `None`.
        self._atr: float | None = None
        #: 세션 끝 정리를 한 세션의 끝 시각(밀리초). 그 시각까지 새 진입을 막습니다.
        self._closing_until = 0
        #: 「끝 시각을 몰라 정리하지 못합니다」를 남긴 거래일. 그날은 다시 남기지 않습니다.
        self._session_end_unknown_day: int | None = None
        #: 청산 뒤 남은 대기 봉 수. 0 이면 기다리지 않습니다.
        self._wait_left = 0
        #: 지금 판정 중인 봉의 `(series, 자리)`. 봉 판정 밖에서는 `None` 입니다.
        self._seen: tuple[rules.Series, int] | None = None
        #: 평가 방식이 「틱」인지. 실행에 담긴 설정에서 읽어, 도는 동안 바뀌지 않습니다.
        self.eval_tick = str(run.draft.get("evalMode") or "") == EVAL_TICK
        #: 지금 진행 중인 봉을 틱으로 판정하는 중인지(`judge_tick`).
        self._ticking = False
        #: 틱 판정을 쉬기 시작한 시각(밀리초). 쉬지 않으면 `None` 입니다. 그 뒤에
        #: 끝나는 봉이 닫히면 풉니다 — 공백에서 풀린 뒤와 앱을 다시 켜 되살린
        #: 뒤입니다. 끊긴 동안의 봉은 다시 받기 전까지 비어 있고, 되살린 실행은
        #: 같은 봉에서 이미 냈는지 모릅니다.
        self._tick_rest_from: int | None = None
        #: 지금 진행 중인 봉의 시작 시각과, 그 봉에서 이미 걸린 규칙들. 규칙은
        #: **한 봉에 한 번** 겁니다. 규칙 주문이 나갔으면 그 봉에서 더 판정하지
        #: 않습니다(`_tick_done`).
        self._tick_bar: int | None = None
        self._tick_fired: set[str] = set()
        self._tick_judged: set[str] = set()
        self._tick_done = False
        #: 지금 틱 판정의 시각(밀리초). 표지에 싣습니다.
        self._tick_at = 0
        #: 직전 닫힌 봉까지 굴린 규칙 상태. 진행 중인 봉 동안은 바뀌지 않아 봉마다
        #: 한 번 셉니다. `(닫힌 봉 수, 마지막 닫힌 봉 시각, {규칙: 상태})`.
        self._tick_base: tuple[int, int, dict[str, rules.Stage]] | None = None
        #: 틱에서 걸린 봉 — `{규칙: {봉 시각}}`. 봉이 닫힌 뒤 규칙을 다시 굴릴 때
        #: 그 봉은 **걸린 것으로** 확정합니다. 마감 값으로 다시 재면 안 걸린 것으로
        #: 되돌아가, 같은 신호가 다음 봉에서 한 번 더 걸릴 수 있습니다.
        self._tick_hits: dict[str, set[int]] = {}
        #: 닫힌 봉까지 센 판정 값과 그 봉들의 시각. 「틱」 실행이 진행 중인 봉 한
        #: 칸만 이어 붙여 셉니다(`rules.extend_series`). 봉 시각이 어긋나면 버리고
        #: 처음부터 셉니다.
        self._tick_closed: tuple[rules.Series, list[int]] | None = None
        self._exit_reason = ""
        #: 추적 청산의 기준 — 지금 보유가 열린 뒤 가장 유리했던 값. 아직 감시를
        #: 시작하지 않았으면 `None` 이고, 첫 감시에서 그때의 평균가로 섭니다.
        #: 메모리에만 둡니다: 앱을 다시 켜면 장부가 비어 이어 볼 보유가 없습니다.
        self._trail_best: float | None = None
        #: 마지막으로 받은 1호가. `bid1`·`ask1`·`bid1Qty`·`ask1Qty` 이고, 아직 못
        #: 받았으면 `None` 입니다.
        self._quote: dict[str, float | None] | None = None
        #: 그 1호가를 받은 시각(밀리초). 아직 못 받았으면 0 입니다.
        self._quote_at = 0
        #: 지금 받는 체결이 들어오기 전의 장부 `(부호 있는 수량, 평균가, 실현,
        #: 수수료)`. 그 체결이 봉을 닫을 때 닫히는 봉에 적을 값입니다. 체결
        #: 밖에서는 `None`.
        self._held_at_close: tuple[int, float, float, float] | None = None
        #: 마지막으로 닫힌 봉에서 잡아 둔 **거래일 끝 값**. 그 거래일의 잔고 줄을
        #: 쓸 때 씁니다(`_catch_day`).
        self._day_seen: dict[str, Any] | None = None
        #: 잔고 줄을 이미 쓴 거래일(0시 밀리초). 한 거래일에 한 줄입니다.
        self._days_written: set[int] = set()
        #: 거래일마다의 최대 증거금 사용률(0~1).
        self._margin_peak: dict[int, float] = {}
        #: 취소를 보내고 계좌 확인을 기다리는 모의 주문. `orderId` → 목적.
        #: 확인이 와서 닫히면 그때 결과 줄을 씁니다.
        self._cancel_wait: dict[str, str] = {}
        #: 봉마다 적어 둔 호가·포지션 값. **봉 시작 시각** → 값들.
        #: 봉 목록에서 빠진 시각은 지웁니다(`_tidy`).
        self._marks: dict[int, dict[str, float | None]] = {}
        #: 거래일·세션마다의 **개장 시각**(`note_open`). 봉 순서(`barOrder`)를
        #: 여기서부터 봉 길이로 셉니다. 모르는 날의 봉 순서는 `None` 입니다.
        self._opens: dict[tuple[int, str], int] = {}
        #: 거래일이 바뀌어 계좌와 다시 맞춰 보아야 하는지.
        #:
        #: **엔진은 계좌를 조회하지 않습니다** — 판정 한가운데에서 통신을 하면
        #: 그 실행의 봉이 응답을 기다리는 동안 멈춥니다. 여기서는 표시만 하고,
        #: 조회는 실행 목록이 루프 밖에서 합니다.
        self.audit_wanted = False

    # ── 받기 ────────────────────────────────────────────

    def on_trade(self, trade: dict[str, Any], context: str, *, at: int) -> None:
        """체결 하나. 종이 주문을 채우고, 손절·익절을 보고, 봉을 잇습니다.

        **끝난 실행은 받지 않습니다.** 봉·조건 표·곡선·기록이 끝난 그 자리에 멈춰
        있어야 합니다(펌프도 끝난 실행에는 시세를 넘기지 않습니다).
        """
        with self.lock:
            if self.run.state == runs.ENDED:
                return
            self.last_trade_at = at
            if self.feed == GAP:
                self._recover(at)
            if self.bars.daily and self._window_open(context, at):
                # 일봉 실행의 거래일은 **그 실행 세션이 열린 뒤 첫 체결**에서 넘깁니다.
                # 미룬 주문을 내기 전이라, 미룬 진입이 새 날의 한도로 셈해집니다.
                self._roll_daily(at)
            if self._deferred and self._window_open(context, at):
                self._release(at)

            # 이 체결은 **다음 봉의 첫 체결**일 수 있습니다. 그것으로 난 종이
            # 체결은 다음 봉의 일이라, 닫히는 봉에는 그 전의 보유를 적습니다.
            held = self.ledger.position
            self._held_at_close = (
                held.quantity,
                held.avg_price,
                self.ledger.realized,
                self.ledger.fee,
            )
            try:
                self._take(self.book.on_trade(trade, at=at), at)
                if self._after_gap:
                    self._after_gap = False
                    self._watch_after_gap(at)
                else:
                    self._watch(at)
                self._close_bars(self.bars.add(trade, context), at)
            finally:
                self._held_at_close = None

    def on_book(self, book: dict[str, Any], *, at: int) -> None:
        """호가 한 벌. 시장가와 도착한 지정가가 여기서 채워집니다.

        장부에 넘기기 **전에** 1호가를 적어 둡니다. 봉이 닫힐 때 그 봉의 호가
        값(`bid1`·`ask1`·잔량)으로 쓰입니다. 종이 장부와 모의 계좌 모두 여기를
        지납니다.
        """
        with self.lock:
            self._quote = _best_of(book)
            self._quote_at = int(at)
            self._take(self.book.on_book(book, at=at), at)

    def seed(self, rows: list[dict[str, Any]], context: str) -> set[int]:
        """조회로 받은 봉을 넣고, 읽은 봉의 시각들을 냅니다(`bars.BarBuilder.seed`).

        넣은 뒤 **봉 목록에 없는 시각의 기록을 지웁니다.** 같은 시각의 봉이 바뀌어
        들어오면 그 시각에 적어 둔 값은 그대로 둡니다 — 같은 봉 칸입니다.
        """
        with self.lock:
            read = self.bars.seed(rows, context)
            self._tidy(seeded=True)
            self._tick_closed = None
            return read

    def on_order(self, event: dict[str, Any], *, at: int) -> None:
        """계좌가 보낸 주문/체결 알림 한 건.

        **종이 실행은 이 자리를 안 씁니다** — 채우는 것이 이 앱이라서입니다.
        계좌 갈래(모의·실전) 실행만 여기서 체결을 받습니다.

        **접수 뒤 거절(처리구분 9)이 알림으로 오면 접수 자리의 거부와 같이
        다룹니다**(`_rejected`) — 진입이면 중지, 청산이면 확인 필요이고, 사건에
        계좌가 준 코드를 싣습니다. 취소로 읽고 감시로 돌아가면 다음 신호에 같은
        주문이 같은 까닭으로 다시 나갑니다.
        """
        taking = getattr(self.book, "on_order", None)
        if taking is None:
            return
        with self.lock:
            given = str(event.get("id") or "").strip()
            target = next(
                (one for one in self.book.orders if one.open and given and getattr(one, "order_no", "") == given),
                None,
            )
            purpose = self._purpose.get(target.id, "entry") if target is not None else "entry"
            self._take(taking(event, at=at), at)
            if target is not None and not target.open and target.last_status == sim.REJECTED:
                self._halt_rejected(target, purpose, at)

    def _halt_rejected(self, order: Any, purpose: str, at: int) -> None:
        """접수 뒤 거절로 닫힌 주문 하나의 사건을 세우고 실행을 멈춥니다(`_halt`)."""
        text = order.reason or sim.rejected_text(order.server_code, "")
        self._say(
            at,
            BLOCKED,
            text,
            extra={"reason": sim.REJECTED, "why": text, "code": order.server_code},
        )
        self._halt(purpose, at)

    def mark_live(self) -> None:
        """그 종목의 시세가 들어왔을 때. **공백은 여기서 풀지 않습니다.**

        공백 중이거나, 공백 표지를 든 채 시세를 못 받는 상태(`idle`)로 있었으면
        공백으로 둡니다 — 공백은 첫 체결에서 풉니다(`on_trade`). 여기서 살아
        있다고 적으면 공백이 끝난 것을 몰라, 장부의 공백 표지와 공백 뒤 손절
        감시가 그대로 남습니다. 체결 없이 값만 바뀐 시세로는 공백을 풀지 않습니다.
        """
        with self.lock:
            if self.feed == GAP or self.book.gap:
                self.feed = GAP
            else:
                self.feed = "live"

    def judge_tick(self, at: int) -> None:
        """체결 묶음 하나를 받은 뒤 **진행 중인 봉까지** 넣어 판정합니다. 「틱」 실행만입니다.

        체결 하나하나가 아니라 묶음마다 한 번이고, 값은 묶음의 마지막 체결가(진행
        중인 봉의 종가)입니다. 규칙은 **한 봉에 한 번** 겁니다 — 한 번 걸린 규칙은
        그 봉이 끝날 때까지 다시 걸지 않고, 규칙 주문이 나간 봉에서는 더 판정하지
        않습니다(청산 뒤 같은 봉 진입·진입 뒤 같은 봉 청산 없음). 손절·익절 감시는
        이와 따로 체결마다 돕니다.

        판정하지 않는 때: 시세 공백, 쉬는 중(공백에서 풀린 뒤·되살린 뒤 첫 봉 마감
        전), 다른 세션 창의 봉, 준비 봉이 덜 찼을 때.

        규칙 상태는 직전 닫힌 봉까지 굴린 것(봉마다 한 번 셈)에서 진행 중인 봉 한
        걸음만 더 굴립니다. 그래서 한 봉에서 단계가 나아가는 폭은 마감과 같습니다.
        """
        if not self.eval_tick:
            return
        with self.lock:
            if self.run.state == runs.ENDED:
                return
            bar = self.bars.open_bar
            context = self.bars.open_session
            if bar is None or self.feed == GAP or self._tick_rest_from is not None:
                return
            if not self.bars.daily and context != self.run.session:
                return
            if not self.bars.ready(self.warmup):
                return
            if self._tick_bar != bar.timestamp:
                self._tick_bar = bar.timestamp
                self._tick_fired = set()
                self._tick_judged = set()
                self._tick_done = False
            if self._tick_done:
                return
            series = self._live_series(bar, context)
            index = len(self.bars.bars)
            base = self._tick_stages(series)
            results: dict[str, tuple[rules.Stage, bool]] = {}
            for key, rule in self._rules_on():
                stage = base.get(key) or rules.Stage()
                if key in self._tick_fired:
                    results[key] = (stage, False)
                    continue
                results[key] = rules.advance(stage, rule, series, index)
            if self._uses_atr:
                self._atr = _finite(series.at(rules.operand_key(*ATR_FOR_UNIT), index - 1))
            before = len(self.book.orders)
            judged: set[str] = set()
            self._seen = (series, index)
            self._ticking = True
            self._tick_at = int(at)
            self._judging = True
            try:
                if not self._exit_rule(bar, at, results, judged):
                    self._entry_rules(bar, at, results, judged)
            finally:
                self._judging = False
                self._ticking = False
                self._seen = None
            for key in judged:
                if results.get(key, (None, False))[1]:
                    self._tick_fired.add(key)
                    self._tick_hits.setdefault(key, set()).add(bar.timestamp)
            self._tick_judged |= judged
            if len(self.book.orders) > before:
                self._tick_done = True
            table = self._table(series, index, bar, results, judged)
            table["partialBar"] = True
            self._conditions = table

    def rest_ticks(self) -> None:
        """다음 봉이 닫힐 때까지 틱 판정을 쉽니다. 앱을 다시 켜 되살린 실행에 씁니다.

        앞서 돌던 동안 그 봉에서 이미 규칙이 걸렸는지는 메모리에만 있어 다시 켜면 모릅니다.
        지난 봉은 조회로 받고(`seed`, 봉 마감이 아님) 실시간은 진행 중인 봉부터
        오므로, 다시 켠 뒤 처음 닫히는 봉이 앞서 돌던 엔진이 보던 그 봉입니다.
        """
        with self.lock:
            self._tick_rest_from = 0

    def on_status(self, live: bool, *, at: int) -> None:
        """허브가 알린 연결 상태. `live` 가 아니면 공백입니다.

        **연결이 붙었다고 체결이 온 것은 아닙니다.** 체결이 안 와서 든 공백
        (`quiet_since`)은 연결이 다시 붙어도 그대로 두고, 첫 체결에서 풉니다
        (`on_trade`). 여기서 풀면 체결 하나 없이 「시세가 다시 들어옵니다」가
        적히고, 다음 박자에 같은 공백이 다시 걸립니다.
        """
        with self.lock:
            if live:
                if not self.quiet_since:
                    self._recover(at)
                return
            self._gap(at, "연결이 끊겼습니다")

    def tick_clock(self, at: int) -> None:
        """시계만 흐른 순간. 마지막 체결이 오래됐으면 공백으로 봅니다."""
        with self.lock:
            if self.feed == GAP or not self.last_trade_at:
                return
            if at - self.last_trade_at >= self.gap_ms:
                self._gap(at, NO_TRADE)
                self.quiet_since = at
                self.probe_asked = False

    def on_volume(self, volume: Any) -> None:
        """실시간이 알려 준 누적 거래량. 수가 아니면 버립니다."""
        if isinstance(volume, (int, float)) and not isinstance(volume, bool):
            with self.lock:
                self.last_volume = int(volume)

    def note_feed_lost(self, at: int) -> None:
        """시장은 흐르는데 이 실행에 체결이 안 옵니다. 알리기만 합니다.

        멈추거나 다시 걸지 않습니다 — 사용자가 멈췄다 재개하면 그때 구독을
        다시 겁니다(`refresh_wanted`). 공백이 그 사이 풀렸으면 적지 않습니다.
        """
        with self.lock:
            if self.feed == GAP and self.quiet_since:
                self._say(at, GAP, FEED_LOST)

    def close_session(self, at: int) -> None:
        """세션이 끝났을 때. 열린 봉을 닫습니다. **나머지는 갈래가 다릅니다.**

        **종이 장부는 거둡니다.** 지어낸 주문이라 내보낼 곳이 없고, 거두지 않으면
        다음 세션 첫 호가에 채워져 **현실에 없던 체결**이 성적에 들어갑니다. 거둔
        뒤에는 나가 있는 주문을 기다리던 자리(`entering`·`exiting`)에서 내려옵니다.

        **모의 주문은 이 앱이 거두지 않습니다.** 그 취소는 계좌로 나가는 요청인데,
        장이 닫혔는지는 **이 앱의 시계로 판정한 것**이라 그 판정이 틀리면 살아 있는
        주문을 이 앱이 없애게 됩니다. 주문의 수명은 계좌가 정합니다 — 만료되면 그
        알림이 `(entering, entry-cancelled)` 로 내려놓고, 알림이 없으면 대사가
        어긋난 것으로 잡습니다(`service.reconcile`). **상태도 그대로 둡니다** —
        나가 있는 주문을 기다리는 자리가 맞고, 그 주문이 다음 세션에 채워지면
        `(entering, entry-filled)` 로 보유가 됩니다.
        """
        with self.lock:
            # 미룬 일봉 주문은 **바로 다음 세션까지**만 삽니다. 그 세션에 못 냈으면
            # (개장 시각을 못 받은 날 등) 며칠 묵은 신호가 주문이 되지 않게 버립니다.
            # 이 마감에서 새로 걸리는 것은 아래 판정이 다시 담습니다.
            for purpose in list(self._deferred):
                del self._deferred[purpose]
                self._drop(at, WHY_DEFERRED_EXPIRED)
            self._closing_session = True
            try:
                self._close_bars(self.bars.close(), at)
            finally:
                self._closing_session = False
            # 그 거래일의 잔고 줄. 방금 닫은 봉까지의 값입니다.
            if self._day_seen is not None:
                self._write_day(self._day_seen["dayKey"], at)
            if self.virtual:
                for order in self.book.cancel_open("세션이 끝나 취소했습니다"):
                    self._forget(order.id, at)
                    self._say(at, ORDER, f"세션이 끝나 주문을 취소했습니다({order.id})")
                if self.run.state == runs.ENTERING:
                    self._move(runs.ENTRY_CANCELLED, at)
                elif self.run.state == runs.EXITING:
                    self._move(runs.EXIT_CANCELLED, at)
            else:
                left = len(self.book.open_orders)
                if left:
                    self._say(
                        at,
                        ORDER,
                        f"세션이 닫힌 것으로 보임 — 미체결 {left}건은 서버 판정을 따릅니다",
                    )
            # 반전은 그 세션 안에서만 잇습니다. 다 닫히기 전에 세션이 끝났으면 버립니다.
            self._drop_reverse(at)
            self._say(at, SESSION, "세션이 끝났습니다")

    # ── 봉 ──────────────────────────────────────────────

    def _close_bars(self, closed: list[indicators.Bar], at: int) -> None:
        """방금 닫힌 봉들을 판정합니다.

        **맥락은 닫힌 봉의 것을 씁니다.** 새로 온 체결의 맥락이 아닙니다 —
        세션이 바뀌면서 앞 봉이 닫히는 자리에서는 둘이 다르고, 새 맥락으로
        보면 야간 실행이 마지막 주간 봉을 제 봉으로 읽습니다.
        """
        start = len(self.bars.sessions) - len(closed)
        for step, one in enumerate(closed):
            self._on_bar(one, self.bars.sessions[start + step], at)

    def _on_bar(self, bar: indicators.Bar, context: str, at: int) -> None:
        """봉 하나가 닫혔을 때."""
        if self._tick_rest_from is not None and self._closed_at(bar) > self._tick_rest_from:
            # 쉬기 시작한 뒤에 끝나는 봉입니다. 다음 봉부터 다시 틱으로 판정합니다.
            self._tick_rest_from = None
        day = self._trading_slot(bar.timestamp, context)
        if not self._day_checked:
            self._day_checked = True
            # 거래일을 시간표로 세기 전에 담긴 해외 실행은 열쇠가 달력 날짜입니다.
            # 그 값이 이 봉의 달력 날과 같으면 같은 거래일을 옛 셈으로 적은 것이라
            # 넘어가지 않고 열쇠만 고쳐 씁니다 — 안 그러면 다시 켠 것만으로 그날의
            # 하루 손실·진입 수가 풀립니다.
            if (
                self.trading_day is not None
                and self.run.day_key
                and day != self.run.day_key
                and self.run.day_key == bar_lib.day_slot_of(bar.timestamp, context)
            ):
                self.run.day_key = day
        # 거래일이 바뀌었는데 앞 거래일의 잔고 줄을 아직 못 썼습니다(세션 끝
        # 신호를 못 받은 날). **앞 봉을 닫을 때 잡아 둔 값**으로 씁니다 — 지금
        # 장부에는 새 날 첫 봉 동안의 체결이 이미 들어 있습니다.
        if self.run.day_key and day != self.run.day_key:
            self._write_day(self.run.day_key, at, late=True)
        # **판정 여부와 상관없이 먼저 적습니다.** 판정하지 않는 봉(다른 창·준비
        # 중)도 뒤 봉의 전이 판정에서 「직전 봉」이 되기 때문입니다.
        self._mark(bar, context)
        self._catch_day(bar, day)
        self.run.last_bar_at = bar.timestamp
        # **첫 봉은 경계가 아닙니다.** 그때는 거래일을 처음 적는 것이라, 이것을
        # 바뀜으로 읽으면 실행을 세울 때마다 대사가 한 번 더 붙습니다.
        if self.run.mode in runs.ACCOUNT_MODES and self.run.day_key and day != self.run.day_key:
            self.audit_wanted = True
        self.run.roll_day(day, self.ledger, self.book.last_price)
        self.curve.append({"time": bar.timestamp, "equity": self.run.cash + self._profit()})
        if len(self.curve) > self._history:
            del self.curve[0 : len(self.curve) - self._history]

        if not self.bars.daily and context != self.run.session:
            # 다른 창의 봉입니다. 지표에는 쓰고 판정에는 쓰지 않습니다.
            # 일봉은 거래일이 단위라 창으로 가리지 않습니다(`bars.judged`).
            return
        if not self.bars.ready(self.warmup):
            if not self._told_ready:
                self._told_ready = True
                self._say(
                    at,
                    READY,
                    f"준비 중 — 필요 {self.warmup} · 확보 {len(self.bars.bars)}",
                )
            self._conditions = self._blank_table(TABLE_WARMUP, bar)
            return
        if self.feed == GAP:
            self._conditions = self._blank_table(TABLE_GAP, bar)
            return

        series = self._closed_series() if self.eval_tick else self._series()
        index = len(self.bars.bars) - 1
        if self._uses_atr:
            self._atr = _finite(series.at(rules.operand_key(*ATR_FOR_UNIT), index))
        # 이 봉에서 주문이 나가면 **방금 센 값**을 그대로 싣습니다 — 다시 세지 않습니다.
        self._seen = (series, index)
        try:
            if self.eval_tick:
                # 「틱」은 봉 안에서 이미 판정했습니다. 닫힌 값으로 다시 판정하지
                # 않고 그 봉까지의 상태만 확정합니다.
                self._settle_tick(series, index, bar, at)
            else:
                self._judge(series, index, bar, at)
        finally:
            self._seen = None
            # 청산 뒤 대기는 **판정 봉**을 셉니다. 이 봉까지 막았으니 하나 줄입니다.
            if self._wait_left > 0:
                self._wait_left -= 1

    def _mark(self, bar: indicators.Bar, context: str) -> None:
        """닫힌 봉의 호가·포지션 값을 봉 시각으로 적습니다.

        보유는 **그 봉이 닫힐 때**의 것입니다 — 봉을 닫은 체결로 난 종이 체결은
        다음 봉의 일이라 넣지 않습니다(`on_trade`). 평가손익은 그 봉의 종가로
        셉니다. 마지막 체결가는 이미 다음 봉의 값이기 때문입니다. 보유가 없으면
        평가손익은 0 입니다.

        **보유 수량은 크기입니다**(매수·매도 구별 없이). 규칙이 방향마다 따로라
        (매수 청산은 매수 보유만 봅니다) 방향이 없어도 뜻이 서고, 「같음 0 =
        보유 없음」·「초과 0 = 보유 있음」으로 읽힙니다.

        시세가 끊긴 동안 닫힌 봉에는 호가를 적지 않습니다(`None`) — 끊기기 전의
        묵은 호가를 그 봉의 값으로 둘 수 없습니다.
        """
        quantity, avg, _, _ = self._held_now()
        self._marks[bar.timestamp] = self._mark_values(bar.close, quantity, avg)
        self._tidy()

    def _mark_values(self, close: float, quantity: int, avg: float) -> dict[str, float | None]:
        """봉 하나에 적는 호가·보유 값. 평가손익은 `close` 로 셉니다."""
        way = 1 if quantity > 0 else -1
        made: dict[str, float | None] = {
            name: None for name in rules.BOOK_IDS
        }
        if self._quote is not None and self.feed != GAP:
            made.update(self._quote)
        made["positionQty"] = float(abs(quantity))
        made["openPl"] = (
            (close - avg) * abs(quantity) * self.ledger.multiplier * way if quantity else 0.0
        )
        return made

    def _held_now(self) -> tuple[int, float, float, float]:
        """봉을 닫는 순간의 장부 `(부호 있는 수량, 평균가, 실현, 수수료)`.

        봉을 닫은 체결이 들어오기 **전** 값입니다(`on_trade` 가 잡아 둠). 체결
        밖에서 닫히는 봉(세션 끝)은 지금 장부 값입니다.
        """
        if self._held_at_close is not None:
            return self._held_at_close
        held = self.ledger.position
        return held.quantity, held.avg_price, self.ledger.realized, self.ledger.fee

    def _catch_day(self, bar: indicators.Bar, day: int) -> None:
        """그 봉이 닫힐 때의 거래일 끝 값을 잡아 둡니다. 잔고 줄이 이 값을 씁니다.

        평가손익은 **그 봉의 종가**로 셉니다 — 거래일 경계에서 마지막 체결가는
        이미 새 날의 값입니다.
        """
        quantity, avg, realized, fee = self._held_now()
        way = 1 if quantity > 0 else -1
        unrealized = (
            (bar.close - avg) * abs(quantity) * self.ledger.multiplier * way if quantity else 0.0
        )
        self._note_margin(day, quantity)
        self._day_seen = {
            "dayKey": day,
            "realized": realized,
            "fee": fee,
            "unrealized": unrealized,
            "position": quantity,
            "avgPrice": avg if quantity else None,
            "closePrice": bar.close,
        }

    def _note_margin(self, day: int, quantity: int | None = None) -> None:
        """그 거래일의 최대 증거금 사용률을 올립니다.

        사용률 = (보유 + 진입 미체결) × 계약당 ÷ 증거금 예산 입니다 — 진입을 막는
        예산 검사(`runs.entry_block`)와 같은 셈입니다. 예산이 없거나 계약당 값을
        모르면(옵션 매도·해외) 셀 수 없어 올리지 않습니다.
        """
        budget = self.run.margin_budget
        each = self.per_contract
        if not day or not budget or budget <= 0 or each is None:
            return
        held = abs(self.ledger.position.quantity if quantity is None else quantity)
        used = (held + self.open_entry) * each / budget
        if used > self._margin_peak.get(day, -1.0):
            self._margin_peak[day] = used

    def _write_day(self, day: int, at: int, *, late: bool = False) -> None:
        """그 거래일의 잔고 줄 한 줄. **거래일마다 한 번**입니다.

        값은 그 거래일의 마지막 닫힌 봉에서 잡아 둔 것이고(`_catch_day`),
        잡아 둔 것이 없는 날(봉이 하나도 안 닫힌 날)은 쓰지 않습니다. `late` 는
        세션 끝 신호를 못 받아 다음 거래일 첫 봉에서 쓴 줄이라는 표지입니다 —
        값은 앞 봉에서 잡아 둔 것이라 그대로 쓸 수 있습니다.
        """
        seen = self._day_seen
        if not day or day in self._days_written or seen is None or seen["dayKey"] != day:
            return
        if not self.money_known:
            # 잔고 줄은 돈의 값입니다. 승수를 모르면 쓰지 않습니다(곡선은 실현 기준으로).
            return
        self._days_written.add(day)
        peak = self._margin_peak.pop(day, None)
        for old in [one for one in self._margin_peak if one < day]:
            del self._margin_peak[old]
        self._keep(
            DAY_ROW,
            {
                "day": datetime.fromtimestamp(day / 1000, tz=KST).date().isoformat(),
                "time": int(at),
                "realized": seen["realized"],
                "fee": seen["fee"],
                "unrealized": seen["unrealized"],
                "equity": seen["realized"] - seen["fee"] + seen["unrealized"],
                "position": seen["position"],
                "avgPrice": seen["avgPrice"],
                "closePrice": seen["closePrice"],
                "marginPeak": peak,
                "marginBudget": self.run.margin_budget,
                "late": late,
            },
        )

    def note_open(self, context: str, since: int) -> None:
        """그 거래일·세션의 **개장 시각**(밀리초)을 적습니다. 봉 순서의 기준입니다.

        장 시간은 품목마다 달라(개장 08:45 인 품목과 09:00 인 품목이 함께 옵니다)
        엔진이 따로 셈하지 않고, 화면이 쓰는 세션 판정에서 받습니다
        (`pump._opening_now`). 실행을 언제 세웠든 같은 봉은 같은 번호가 됩니다.
        """
        with self.lock:
            self._opens[self._day_key(int(since), context)] = int(since)
            # 지난 봉의 봉 순서가 바뀔 수 있습니다. 이어 세던 값을 버립니다.
            self._tick_closed = None

    def open_known(self, at: int, context: str) -> bool:
        """`at` 이 드는 거래일·세션의 개장 시각을 이미 받았는지."""
        return self._day_key(int(at), context) in self._opens

    def _tidy(self, *, seeded: bool = False) -> None:
        """봉 목록에 없는 시각의 기록을 지웁니다. 안 지우면 실행 내내 쌓입니다.

        개장 시각은 그 거래일·세션의 봉이 목록에 하나도 없고 **지나간 날의
        것일 때만** 지웁니다 — 오늘 개장 시각은 첫 봉이 닫히기 전에 들어옵니다.
        """
        if len(self._marks) > len(self.bars.bars) or len(self._days) > len(self.bars.bars) or seeded:
            stamps = {bar.timestamp for bar in self.bars.bars}
            for key in [one for one in self._marks if one not in stamps]:
                del self._marks[key]
            # 마지막 봉보다 뒤의 시각은 남깁니다 — 지난 봉 셈은 받은 봉 전부를 먼저 묻습니다.
            newest = self.bars.bars[-1].timestamp if self.bars.bars else 0
            for key in [one for one in self._days if one not in stamps and one <= newest]:
                del self._days[key]
        if len(self._opens) > 2 and self.bars.bars:
            alive = {
                self._day_key(bar.timestamp, context)
                for bar, context in zip(self.bars.bars, self.bars.sessions)
            }
            newest = max(key[0] for key in alive) if alive else 0
            for key in [one for one in self._opens if one not in alive and one[0] < newest]:
                del self._opens[key]

    def _series(self) -> rules.Series:
        """판정에 쓰는 값 한 벌 — 지표와 봉마다 적어 둔 값, 봉 순서까지.

        봉 순서 = (봉 시작 − 개장이 드는 칸의 시작) ÷ 봉 길이 + 1 입니다. 봉 칸은
        정각부터 봉 길이로 끊으므로(`bars.slot_of`), 개장이 08:45 이고 30분봉이면
        08:30 칸이 개장을 담은 첫 봉(1번)입니다 — 개장 시각 자체와 견주면 그 봉이
        「개장 전」으로 빠집니다. 체결이 없던 칸도 셉니다.

        개장 시각을 모르거나, 봉이 개장을 담은 칸보다 앞이면 `None` 입니다(그
        조건은 거짓). 0 이하의 번호는 만들지 않습니다. 일봉은 하루가 한 봉이라
        뜻이 없어 `None` 입니다.
        """
        return self._series_of(self.bars.bars, self.bars.sessions)

    def _series_of(
        self,
        bars: list[indicators.Bar],
        sessions: list[str],
        live: dict[str, float | None] | None = None,
    ) -> rules.Series:
        """그 봉들로 센 판정 값 한 벌. `live` 는 진행 중인 봉(맨 끝)에 그 순간 적을 값입니다."""
        firsts: dict[int, int] = {}
        marks = {
            bar.timestamp: self._mark_row(bar, context, firsts)
            for bar, context in zip(bars, sessions)
        }
        if live is not None and bars:
            marks[bars[-1].timestamp] = {**live, **marks[bars[-1].timestamp]}
        return rules.series_of(
            bars,
            sessions=sessions,
            marks=marks,
            session=self.run.session,
            wanted=self._wanted,
            day_of=self._day_of if self.trading_day is not None else None,
        )

    def _day_of(self, timestamp: int, context: str) -> Any:
        """그 봉의 거래일. 처음 보는 봉 시각에서만 묻고 기억합니다.

        판정 값 한 벌은 봉이 닫힐 때마다 봉 전부로 다시 세므로, 기억하지 않으면
        봉마다 봉 수만큼 묻게 됩니다.
        """
        if timestamp not in self._days:
            self._days[timestamp] = self.trading_day(timestamp) if self.trading_day else None
        return self._days[timestamp]

    def _trading_slot(self, timestamp: int, context: str) -> int:
        """그 시각이 드는 **거래일**의 0시(KST, 밀리초). 거래일 열쇠는 모두 이 값입니다.

        국내는 `bars.day_slot_of`(야간은 다음 날)입니다. 해외는 시간표의 거래 창이
        시작한 날입니다(`_day_of`) — 한국 시각 자정을 넘기는 세션도 한 거래일이라,
        하루 손실·진입 수·잔고 줄이 세션 한가운데서 넘어가지 않습니다. 창 밖(정비·
        쉼)이면 앞 봉의 거래일을 잇고, 앞 봉도 모르면 달력 날짜입니다.
        """
        if self.trading_day is None:
            return bar_lib.day_slot_of(timestamp, context)
        day = self._day_of(timestamp, context)
        if day is None:
            for bar in reversed(self.bars.bars):
                if bar.timestamp < timestamp and self._days.get(bar.timestamp) is not None:
                    day = self._days[bar.timestamp]
                    break
        if day is None:
            return bar_lib.day_slot_of(timestamp, context)
        return int(datetime(day.year, day.month, day.day, tzinfo=KST).timestamp() * 1000)

    def _day_key(self, timestamp: int, context: str) -> tuple[int, str]:
        """`(거래일, 세션)` — 개장 시각과 봉 순서를 묶는 열쇠(`_trading_slot`)."""
        return self._trading_slot(timestamp, context), context

    def _day_text(self, timestamp: int) -> str | None:
        """기록 줄에 싣는 거래일 글자. 해외만이고(국내는 기록이 셈합니다) 국내는 `None` 입니다.

        남길 자리가 없으면(지난 봉 셈 등) 묻지 않습니다 — 판정은 봉마다 한 번이면 됩니다.
        """
        if self.trading_day is None or self.record is None:
            return None
        slot = self._trading_slot(timestamp, self.run.session)
        return datetime.fromtimestamp(slot / 1000, tz=KST).date().isoformat()

    def _mark_row(
        self, bar: indicators.Bar, context: str, firsts: dict[int, int]
    ) -> dict[str, float | None]:
        """봉 하나에 적어 둔 값들과 그 봉의 봉 순서. `firsts` 는 개장 칸을 담아 두는 자리입니다."""
        made = dict(self._marks.get(bar.timestamp) or {})
        minutes = self.bars.minutes or 0
        step = minutes * 60_000
        opened = self._opens.get(self._day_key(bar.timestamp, context))
        first = None
        if step and opened is not None:
            first = firsts.get(opened)
            if first is None:
                first = firsts[opened] = bar_lib.slot_of(opened, minutes)
        made[rules.BAR_ORDER] = (
            float((bar.timestamp - first) // step + 1)
            if first is not None and bar.timestamp >= first
            else None
        )
        return made

    def _stage_of(
        self, key: str, rule: dict[str, Any], series: rules.Series, index: int
    ) -> tuple[rules.Stage, bool]:
        """규칙 하나를 그 봉까지 굴린 `(상태, 발사했는지)`. 처음 봉부터 굴립니다(`rules.stage_at`)."""
        return rules.stage_at(rule, series, index)

    def _judge(self, series: rules.Series, index: int, bar: indicators.Bar, at: int) -> None:
        """봉 하나를 판정합니다. **규칙마다 한 번만 굴립니다**(`rules.stage_at`).

        판정과 조건 표가 그 한 번의 결과를 나눠 씁니다 — 표가 따로 굴리면 봉마다
        일이 두 배가 되고, 둘이 다른 답을 낼 자리가 생깁니다.
        """
        results = {
            key: self._stage_of(key, rule, series, index) for key, rule in self._rules_on()
        }
        judged: set[str] = set()
        self._judging = True
        try:
            if self._exit_rule(bar, at, results, judged):
                # 청산이 나간 봉에서는 진입을 보지 않습니다.
                return
            self._entry_rules(bar, at, results, judged)
        finally:
            self._judging = False
            # 표는 **통째로** 갈아 끼웁니다 — 반쯤 채운 표가 화면으로 나가지 않게.
            self._conditions = self._table(series, index, bar, results, judged)
            self._first_judged(at)

    def _settle_tick(
        self, series: rules.Series, index: int, bar: indicators.Bar, at: int
    ) -> None:
        """「틱」 실행에서 봉이 닫혔을 때. **새로 판정하지 않고** 그 봉까지의 상태만 확정합니다.

        봉 안에서 걸린 규칙은 걸린 것으로 확정합니다(`_rolled`). 확정한 상태는 다음
        봉의 틱 판정이 출발점으로 씁니다.
        """
        first = self.bars.bars[0].timestamp if self.bars.bars else 0
        for key in list(self._tick_hits):
            kept = {one for one in self._tick_hits[key] if one >= first}
            if kept:
                self._tick_hits[key] = kept
            else:
                del self._tick_hits[key]
        results = self._settled(series, index, bar)
        judged = self._tick_judged if self._tick_bar == bar.timestamp else set()
        self._tick_base = (
            len(self.bars.bars),
            bar.timestamp,
            {key: stage for key, (stage, _) in results.items()},
        )
        self._conditions = self._table(series, index, bar, results, set(judged))
        self._first_judged(at)

    def _settled(
        self, series: rules.Series, index: int, bar: indicators.Bar
    ) -> dict[str, tuple[rules.Stage, bool]]:
        """방금 닫힌 봉까지의 규칙 상태. **앞 봉까지 확정한 상태에서 한 걸음만** 굴립니다.

        앞 상태가 없거나 봉이 어긋났으면 처음부터 굴립니다(`_rolled`). 봉 수 상한에서
        앞이 잘렸으면 상태의 자리 번호를 그만큼 당깁니다. 틱에서 걸린 봉은 걸린 것으로
        둡니다.
        """
        cached = self._tick_base
        before = self.bars.bars[index - 1].timestamp if index > 0 else None
        if cached is None or before is None or cached[1] != before:
            return {key: self._rolled(key, rule, series, index) for key, rule in self._rules_on()}
        drop = cached[0] + 1 - len(self.bars.bars)
        if drop < 0:
            return {key: self._rolled(key, rule, series, index) for key, rule in self._rules_on()}
        made: dict[str, tuple[rules.Stage, bool]] = {}
        for key, rule in self._rules_on():
            stage = _shifted(cached[2].get(key) or rules.Stage(), drop)
            if bar.timestamp in (self._tick_hits.get(key) or ()):
                made[key] = (rules.Stage(), True)
                continue
            made[key] = rules.advance(stage, rule, series, index)
        return made

    def _closed_series(self) -> rules.Series:
        """닫힌 봉까지의 판정 값. 앞서 센 값에 새로 닫힌 봉 한 칸만 붙입니다(「틱」 실행)."""
        closed = self.bars.bars
        times = [one.timestamp for one in closed]
        cached = self._tick_closed
        if cached is not None and closed:
            series, held = cached
            drop = len(held) + 1 - len(closed)
            if 0 <= drop <= len(held) and held[drop:] == times[:-1]:
                made = rules.extend_series(
                    series,
                    closed,
                    drop=drop,
                    sessions=self.bars.sessions,
                    mark=self._mark_row(closed[-1], self.bars.sessions[-1], {}),
                    session=self.run.session,
                    wanted=self._wanted,
                    day_of=self._day_of if self.trading_day is not None else None,
                )
                self._tick_closed = (made, times)
                return made
        made = self._series()
        self._tick_closed = (made, times)
        return made

    def _live_series(self, bar: indicators.Bar, context: str) -> rules.Series:
        """진행 중인 봉까지의 판정 값. 닫힌 봉까지 센 값에 그 봉 한 칸만 붙입니다.

        그 봉의 호가·보유 값은 지금 것이고, 평가손익은 그 봉의 지금 값으로 셉니다.
        """
        closed = self.bars.bars
        cached = self._tick_closed
        if cached is None or cached[1] != [one.timestamp for one in closed]:
            self._tick_closed = (self._series(), [one.timestamp for one in closed])
        base = self._tick_closed[0]
        held = self.ledger.position
        mark = {
            **self._mark_values(bar.close, held.quantity, held.avg_price),
            **self._mark_row(bar, context, {}),
        }
        return rules.extend_series(
            base,
            [*closed, bar],
            sessions=[*self.bars.sessions, context],
            mark=mark,
            session=self.run.session,
            wanted=self._wanted,
            day_of=self._day_of if self.trading_day is not None else None,
        )

    def _tick_stages(self, series: rules.Series) -> dict[str, rules.Stage]:
        """직전 닫힌 봉까지 굴린 규칙 상태. 진행 중인 봉 동안은 같아 봉마다 한 번 셉니다."""
        closed = self.bars.bars
        last = closed[-1].timestamp if closed else 0
        cached = self._tick_base
        if cached is not None and cached[0] == len(closed) and cached[1] == last:
            return cached[2]
        index = len(closed) - 1
        made = {key: self._rolled(key, rule, series, index)[0] for key, rule in self._rules_on()}
        self._tick_base = (len(closed), last, made)
        return made

    def _rolled(
        self, key: str, rule: dict[str, Any], series: rules.Series, index: int
    ) -> tuple[rules.Stage, bool]:
        """규칙을 처음 봉부터 그 봉까지 굴립니다. **틱에서 걸린 봉은 걸린 것으로** 둡니다.

        걸린 뒤의 상태는 마감에서 걸렸을 때와 같습니다(처음 단계로 돌아감).
        """
        hits = self._tick_hits.get(key) or set()
        closed = self.bars.bars
        stage, fired = rules.Stage(), False
        for place in range(index + 1):
            stage, fired = rules.advance(stage, rule, series, place)
            if hits and place < len(closed) and closed[place].timestamp in hits:
                stage, fired = rules.Stage(), True
        return stage, fired

    def _tick_mark(self) -> dict[str, Any] | None:
        """틱 판정에서 난 것에 다는 표지. 마감 판정이면 `None` 입니다.

        분석·검증 화면이 봉 마감 신호와 가를 수 있게 평가 방식, 진행 중인 봉이었는지,
        판정한 순간(그 묶음의 마지막 체결을 받은 시각), 그 진행 중인 봉의 **시작
        시각**(`barTime`, 조건 표·주문 snapshot 의 것과 같은 단위)을 싣습니다.
        """
        if not self._ticking:
            return None
        bar = self.bars.open_bar
        return {
            "evalMode": EVAL_TICK,
            "partialBar": True,
            "judgedAt": self._tick_at,
            "barTime": bar.timestamp if bar is not None else None,
        }

    def _rules_on(self) -> list[tuple[str, dict[str, Any]]]:
        """판정하는 규칙들. 켜져 있고 조건이 있는 것만, `RULE_KEYS` 차례입니다."""
        return [
            (key, self.rules[key])
            for key in rules.RULE_KEYS
            if isinstance(self.rules.get(key), dict) and rules.steps_of(self.rules[key])
        ]

    def _table(
        self,
        series: rules.Series,
        index: int,
        bar: indicators.Bar,
        results: dict[str, tuple[rules.Stage, bool]],
        judged: set[str],
    ) -> dict[str, Any]:
        """그 봉의 조건 표. 판정이 쓴 값·상태를 그대로 옮깁니다.

        `fired` 는 「규칙이 참」이고 주문이 나갔다는 뜻이 아닙니다. `judged` 는 그
        봉에 판정이 그 규칙을 실제로 봤는지입니다 — 청산 규칙은 보유가 없으면
        보지 않고, 진입은 청산이 나간 봉에서 보지 않습니다.
        """
        return {
            "barTime": bar.timestamp,
            "state": TABLE_READY,
            "need": self.warmup,
            "have": len(self.bars.bars),
            "rules": [
                {
                    "rule": key,
                    "judged": key in judged,
                    **rules.condition_table(rule, series, index, *results[key]),
                }
                for key, rule in self._rules_on()
                if key in results
            ],
        }

    def _blank_table(self, state: str, bar: indicators.Bar) -> dict[str, Any]:
        """판정하지 않은 봉(준비 중·시세 끊김)의 표. 값과 참·거짓은 모두 모름입니다."""
        made = []
        for key, rule in self._rules_on():
            steps = rules.steps_of(rule)
            made.append(
                {
                    "rule": key,
                    "judged": False,
                    "waitingStep": 1,
                    "steps": len(steps),
                    "barsLeft": None,
                    "resetLeft": None,
                    "fired": None,
                    "stepsDetail": [
                        {
                            "join": "any" if str(step.get("join") or "all") == "any" else "all",
                            "withinBars": rules._window_of(step),
                            "conds": [
                                {
                                    "left": str(cond.get("left") or ""),
                                    "op": str(cond.get("op") or ""),
                                    "right": cond.get("right"),
                                    "rightKind": str(cond.get("rightKind") or ""),
                                    "leftValue": None,
                                    "rightValue": None,
                                    "now": None,
                                    "before": None,
                                }
                                for cond in (step.get("conds") or [])
                                if isinstance(cond, dict)
                            ],
                            **(
                                {"stepNow": None, "stepBefore": None, "beforeUnknown": None}
                                if place == 0
                                else {}
                            ),
                        }
                        for place, step in enumerate(steps)
                    ],
                }
            )
        return {
            "barTime": bar.timestamp,
            "state": state,
            "need": self.warmup,
            "have": len(self.bars.bars),
            "rules": made,
        }

    def _first_judged(self, at: int) -> None:
        """첫 판정 봉에서 1단계가 **이미 참**인 규칙마다 한 번 알립니다.

        1단계는 거짓에서 참으로 **바뀌는 순간**에만 채워지므로, 시작할 때 이미
        참이면 한 번 거짓이 됐다가 다시 참이 될 때까지 기다립니다. 그 사실을
        이벤트로 남겨 「왜 안 나가나」를 사용자가 읽을 수 있게 합니다.
        """
        if self._judged_once:
            return
        self._judged_once = True
        for row in (self._conditions or {}).get("rules") or []:
            first = (row.get("stepsDetail") or [{}])[0]
            # 지금 참인데 직전도 참이었거나(흔한 경우 — 지표가 이미 선을 넘어 있음)
            # 직전 값을 몰라 전이로 치지 않은 경우입니다.
            already = first.get("stepNow") is True and (
                first.get("stepBefore") is True or first.get("beforeUnknown") is True
            )
            if row.get("waitingStep") == 1 and already:
                key = str(row.get("rule") or "")
                self._say(at, ALREADY_TRUE, RULE_LABELS.get(key, key))

    @property
    def conditions(self) -> dict[str, Any] | None:
        """마지막 판정 봉의 조건 표. 첫 판정 봉 전이면 `None` 입니다."""
        return self._conditions

    def _count_signal(self, at: int) -> None:
        """규칙 하나가 판정에서 걸렸을 때. 주문을 냈든 안 냈든 셉니다."""
        if self.signal_count is not None:
            self.signal_count += 1
            self.last_signal_at = int(at)

    def forget_stats(self) -> None:
        """성과 수를 「모름」으로 둡니다. 앱을 다시 켜 되살린 실행에 씁니다.

        앞서 돌던 동안의 수는 메모리에만 있어 다시 켜면 사라집니다. 0 부터
        다시 세면 그 실행의 수가 아닌 값을 아는 값처럼 내게 됩니다.
        """
        self.signal_count = None
        self.fill_count = None
        self.last_signal_at = None

    @property
    def stats(self) -> dict[str, Any]:
        """실행 줄에 싣는 성과 수. 손익 둘은 장부에서 셉니다(`runs.row_of`)."""
        return {
            "signalCount": self.signal_count,
            "fillCount": self.fill_count,
            "lastSignalAt": self.last_signal_at,
        }

    def unsent_today(self, at: int) -> int:
        """그 순간(밀리초)이 속한 날(KST)의 「안 낸 신호」 수."""
        return self._unsent_count if self._unsent_day == _kst_day(at) else 0

    @staticmethod
    def _clock_now() -> int:
        """지금(밀리초)."""
        return int(datetime.now(KST).timestamp() * 1000)

    def _exit_rule(
        self,
        bar: indicators.Bar,
        at: int,
        results: dict[str, tuple[rules.Stage, bool]],
        judged: set[str],
    ) -> bool:
        held = self.ledger.position
        if held.side is None or self.run.state not in runs.EXIT_STATES:
            return False
        key = _EXIT_OF[held.side]
        judged.add(key)
        rule = self.rules.get(key)
        if not isinstance(rule, dict) or not results.get(key, (None, False))[1]:
            return False
        said = RULE_LABELS.get(_EXIT_OF[held.side], _EXIT_OF[held.side])
        self._count_signal(at)
        self._say(at, SIGNAL, f"{said} 규칙이 걸렸습니다", extra=self._tick_mark())
        self._other_signal(key, at)
        if self._deferring:
            self._defer("exit", held.side, bar.close, at)
            return True
        self._close(held.size, bar.close, RULE_EXIT, at)
        return True

    def _entry_rules(
        self,
        bar: indicators.Bar,
        at: int,
        results: dict[str, tuple[rules.Stage, bool]],
        judged: set[str],
    ) -> None:
        for key in _ENTRY_RULES:
            judged.add(key)
            rule = self.rules.get(key)
            if not isinstance(rule, dict) or not results.get(key, (None, False))[1]:
                continue
            side = _SIDE_OF[key]
            self._count_signal(at)
            self._say(
                at, SIGNAL, f"{RULE_LABELS.get(key, key)} 규칙이 걸렸습니다", extra=self._tick_mark()
            )
            self._other_signal(key, at)
            if self._opposite(key, side, bar.close, at):
                return
            why = runs.entry_block(
                self.run,
                self.ledger,
                side=side,
                open_entry=self.open_entry,
                max_hold=_int(self.aux.get("maxHold")) or 0,
                add_while_open=bool(self.aux.get("addWhileOpen")),
                gap=self.feed == GAP,
                expiring=self.expiring,
                last_price=self.book.last_price,
                # 옵션 매수의 계약당 소요액은 **그 봉의 프리미엄**으로 셉니다.
                signal=bar.close,
                streak_limit=_int(self.aux.get("lossStreak")) or 0,
            )
            if why is None:
                # 틱 판정은 **그 체결 시각**으로 봅니다 — 주문이 그 순간 나갑니다.
                why = self._entry_gate(None, moment=at) if self._ticking else self._entry_gate(bar)
            if why is not None:
                self._say_unsent(at, why)
                continue
            if self._deferring:
                self._defer("entry", side, bar.close, at)
                return
            self.run.count_intent()
            self._send(side, self.run.qty, bar.close, "entry", at)
            return

    # ── 보유 중 반대 신호 ────────────────────────────────

    def _opposite(self, key: str, side: str, signal: float, at: int) -> bool:
        """보유와 반대 방향의 진입 규칙이 걸렸을 때. 여기서 맡았으면 참입니다.

        설정의 「보유 중 반대 신호」(`aux.oppositeSignal`)가 정합니다.

        - 「무시」면 맡지 않습니다. 진입 검사가 반대 방향이라 막고 「안 낸 신호」로
          남깁니다(`runs.WHY_OPPOSITE`).
        - 「청산」이면 보유 전량을 닫습니다. 청산 규칙과 같은 길이라 버전의
          주문유형·체결조건으로 나갑니다. 반대 방향으로는 들어가지 않습니다.
        - 「반전」이면 「청산」과 같이 닫고, 그 청산이 보유를 **다 닫은 체결**에서
          반대 방향으로 진입합니다(`_turn`).

        닫는 것은 진입 규칙을 판정하는 상태(감시 중·보유 중)이고 시세가 이어질
        때뿐입니다. 그 밖이면 진입 검사가 그 까닭으로 막습니다. 하루 주문 수·하루
        손실·만기 같은 **새 진입의 한도는 청산을 막지 않습니다** — 반전의 진입은
        그 한도를 모두 다시 거칩니다. 이미 나가 있는 청산이 있으면 또 내지 않습니다.
        """
        held = self.ledger.position
        if not held.quantity or held.side == side:
            return False
        mode = validate.opposite_signal(self.aux)
        if mode == validate.OPPOSITE_SIGNAL_IGNORE:
            return False
        if self.run.state not in runs.ENTRY_STATES or self.feed == GAP:
            return False
        if any(self._purpose.get(one.id) == "exit" for one in self.book.open_orders):
            return True
        turn = side if mode == validate.OPPOSITE_SIGNAL_REVERSE else None
        if self._deferring:
            self._defer("exit", held.side, signal, at, reason=OPPOSITE_EXIT, reverse=turn, rule=key)
            return True
        self._close_opposite(held.size, signal, at, turn=turn, rule=key)
        return True

    def _close_opposite(
        self, quantity: int, signal: float, at: int, *, turn: str | None, rule: str
    ) -> None:
        """반대 신호의 청산을 냅니다. 반전이면 낸 청산 주문에 진입을 예약합니다.

        주문을 내는 자리에서는 체결이 오지 않으므로(체결은 시세·알림이 가져옵니다)
        낸 뒤에 예약해도 늦지 않습니다. 청산을 못 냈으면 예약 대신 사건 한 줄입니다.
        """
        order_id = self._close(quantity, signal, OPPOSITE_EXIT, at)
        if turn is None:
            return
        if order_id is None:
            self._unsent_now(at, WHY_REVERSE_DROPPED)
            return
        self._reverse = {"order": order_id, "side": turn, "rule": rule}

    def _turn(self, order: Any, at: int) -> None:
        """청산 주문 하나가 끝났을 때. 그 주문에 반전이 예약돼 있으면 처리합니다.

        보유가 0 이 됐으면 반대 방향으로 진입합니다. 가격 기준은 그 청산의 마지막
        체결가입니다. 진입은 보통 진입과 같은 검사(`runs.entry_block`·`_entry_gate`)
        를 다시 거치고, 막히면 「안 낸 신호」로 남깁니다 — 청산 뒤 재진입 대기도
        여기서 막습니다.

        보유를 0 으로 만든 것이 **이 주문의 체결**이어야 합니다(`_flat_by`). 다른
        청산이 먼저 닫았거나, 보유가 남았거나, 실행 상태가 감시 중이 아니면 예약을
        버립니다.
        """
        if self._reverse is None or self._reverse.get("order") != order.id:
            return
        side = self._reverse["side"]
        if self.ledger.position.quantity or self._flat_by != order.id or self.run.state != runs.ARMED:
            self._drop_reverse(at)
            return
        self._reverse = None
        fills = getattr(order, "fills", None) or []
        price = _float(fills[-1].get("price")) if fills else None
        if price is None:
            price = self.book.last_price
        if price is None:
            self._unsent_now(at, WHY_REVERSE_DROPPED)
            return
        self._reverse_entry(side, float(price), at)

    def _reverse_entry(self, side: str, signal: float, at: int) -> None:
        """반전의 진입. 보통 진입과 같은 검사를 거칩니다."""
        why = runs.entry_block(
            self.run,
            self.ledger,
            side=side,
            open_entry=self.open_entry,
            max_hold=_int(self.aux.get("maxHold")) or 0,
            add_while_open=bool(self.aux.get("addWhileOpen")),
            gap=self.feed == GAP,
            expiring=self.expiring,
            last_price=self.book.last_price,
            signal=signal,
            streak_limit=_int(self.aux.get("lossStreak")) or 0,
        )
        if why is None:
            why = self._entry_gate(None, moment=at)
        if why is not None:
            self._unsent_now(at, why)
            return
        self.run.count_intent()
        self._send(side, self.run.qty, signal, "entry", at)

    def _other_signal(self, key: str, at: int) -> None:
        """규칙이 걸렸을 때. 반전을 예약한 규칙이 아니면 그 예약을 버립니다."""
        if self._reverse is not None and self._reverse.get("rule") != key:
            self._drop_reverse(at)

    def _drop_reverse(self, at: int) -> None:
        """반전 예약을 버리고 사건 한 줄을 남깁니다."""
        if self._reverse is None:
            return
        self._reverse = None
        self._unsent_now(at, WHY_REVERSE_DROPPED)

    def _unsent_now(self, at: int, why: str) -> None:
        """「안 낸 신호」 한 줄. 판정 중이면 그 판정이 세고, 밖이면 여기서 셉니다."""
        if self._judging:
            self._say_unsent(at, why)
        else:
            self._drop(at, why)

    # ── 일봉 주문 미루기 ─────────────────────────────────

    @property
    def _deferring(self) -> bool:
        """지금 걸린 규칙의 주문을 미뤄야 하는지.

        **일봉은 세션 마감에서만 닫힙니다.** 그 자리에서 주문을 내면 종이 장부는
        같은 마감에서 대기 주문으로 거두고, 계좌 갈래는 장이 닫힌 뒤에 주문을
        냅니다. 그래서 일봉이 세션 마감에서 걸리면 주문을 담아 두었다가 **다음
        거래 창이 열린 뒤의 첫 체결**에서 냅니다. 분봉은 그대로입니다 — 세션
        마지막 봉의 주문은 마감에서 거둡니다(`paper` 의 가정 5).
        """
        return self.bars.daily and self._closing_session

    def _defer(
        self,
        purpose: str,
        side: str,
        signal: float,
        at: int,
        *,
        reason: str = RULE_EXIT,
        reverse: str | None = None,
        rule: str = "",
    ) -> None:
        """주문 하나를 미룹니다. 같은 갈래(진입·청산)는 새 것으로 갈아 끼웁니다.

        가격 기준은 **판정한 봉의 종가**입니다 — 지정가·STOP 의 값도 그 종가로
        셉니다. 다음 거래 창이 갭으로 열리면 지정가가 곧바로 유리해 채워지거나
        멀어져 기다립니다.

        청산이면 무엇이 낸 청산인지(`reason`)와, 반대 신호의 반전이면 다 닫은 뒤
        들어갈 방향(`reverse`)을 함께 담습니다.
        """
        made: dict[str, Any] = {"side": side, "signal": float(signal), "at": int(at)}
        if purpose == "exit":
            made.update({"reason": reason, "reverse": reverse, "rule": rule})
        self._deferred[purpose] = made
        self._say(at, ORDER, DEFERRED_TEXT)

    def _window_open(self, context: str, at: int) -> bool:
        """미룬 주문을 낼 수 있는 체결인지. **그 실행의 거래 창이 열린 뒤**여야 합니다.

        체결 맥락이 실행의 세션이어야 합니다 — 일봉은 창으로 가리지 않아 주간
        실행에도 야간 체결이 들어옵니다. 그 거래일·세션의 개장 시각을 받았고
        그 뒤여야 합니다 — 개장 시각은 실행 목록이 세션 판정에서 거래 창일 때만
        넣으므로(`pump._opening_now`), 동시호가의 체결에서는 내지 않습니다.
        시세가 끊긴 동안에도 내지 않습니다.
        """
        if self.feed == GAP or context != self.run.session:
            return False
        opened = self._opens.get(self._day_key(int(at), context))
        return opened is not None and at >= opened

    def _release(self, at: int) -> None:
        """미룬 주문을 냅니다. 청산이 먼저입니다. 내기 전에 지금 상태로 다시 봅니다.

        그 사이 상태가 바뀌었으면(중지·종료·확인 필요, 손절로 이미 나감 등) 내지
        않고 「안 낸 신호」로 남깁니다. 규칙이 걸린 수는 판정 때 이미 셌습니다.

        **진입은 실행 필터 전체를 이 체결 시각으로 다시 봅니다**(`_entry_gate`) —
        판정은 전날 마감이고 내는 것은 오늘입니다. 신규 진입 시간 밖이면 미룬 채 두고
        그 세션에서 시간 안에 든 첫 체결에 냅니다. 다른 까닭이면 버립니다.

        거래일은 이 앞에서 이미 이 체결의 거래일로 넘어와 있습니다(`_roll_daily`) —
        하루 최대 진입·하루 손실이 새 날 값으로 셈해집니다.
        """
        leaving = self._deferred.pop("exit", None)
        if leaving is not None:
            held = self.ledger.position
            reason = str(leaving.get("reason") or RULE_EXIT)
            turn = leaving.get("reverse")
            if self.run.state in runs.EXIT_STATES and held.side == leaving["side"]:
                if reason == OPPOSITE_EXIT:
                    self._close_opposite(
                        held.size, leaving["signal"], at, turn=turn, rule=str(leaving.get("rule") or "")
                    )
                else:
                    self._close(held.size, leaving["signal"], reason, at)
            else:
                self._drop(at, WHY_DEFERRED_EXIT)
                if turn:
                    # 미룬 반전 — 그 사이 보유가 이미 0 이면 진입만 냅니다. 다른 보유가
                    # 섰으면 버립니다.
                    if held.side is None and self.run.state == runs.ARMED:
                        self._reverse_entry(turn, leaving["signal"], at)
                    else:
                        self._unsent_now(at, WHY_REVERSE_DROPPED)
        entering = self._deferred.pop("entry", None)
        if entering is None:
            return
        if self.run.state != runs.ARMED:
            self._drop(at, WHY_DEFERRED_STATE)
            return
        why = runs.entry_block(
            self.run,
            self.ledger,
            side=entering["side"],
            open_entry=self.open_entry,
            max_hold=_int(self.aux.get("maxHold")) or 0,
            add_while_open=bool(self.aux.get("addWhileOpen")),
            gap=self.feed == GAP,
            expiring=self.expiring,
            last_price=self.book.last_price,
            signal=entering["signal"],
            streak_limit=_int(self.aux.get("lossStreak")) or 0,
        )
        if why is None:
            why = self._entry_gate(None, moment=at)
        if why == runs.WHY_ENTRY_WINDOW:
            # 아직 신규 진입 시간 전(또는 뒤)입니다. 그 세션 안에서 다시 봅니다.
            self._deferred["entry"] = entering
            return
        if why is not None:
            self._drop(at, why)
            return
        self.run.count_intent()
        self._send(entering["side"], self.run.qty, entering["signal"], "entry", at)

    def _roll_daily(self, at: int) -> None:
        """일봉 실행의 거래일을 이 체결의 거래일로 넘깁니다. 이미 그 날이면 아무 일도 없습니다.

        일봉은 세션 마감에서야 봉이 닫혀, 봉으로만 넘기면 하루 내내 어제 날로 셉니다.
        그래서 거래 창이 열린 뒤 첫 체결에서 넘깁니다(시간표를 몰라 개장 시각이 없으면
        마감 봉에서 넘깁니다).

        **하루 손실의 기준은 어제 일봉의 종가로 셉니다** — 보유의 평가를 그 값으로
        기준에 넣으므로, 밤사이 갭으로 난 손익이 **오늘 몫**이 되어 하루 손실 한도에
        잡힙니다. 닫힌 일봉이 없으면 이 체결 값입니다.
        """
        today = self._trading_slot(at, self.run.session)
        if today == self.run.day_key:
            return
        if self.run.day_key and self.run.mode in runs.ACCOUNT_MODES:
            self.audit_wanted = True
        closed = self.bars.bars
        base = closed[-1].close if closed else self.book.last_price
        self.run.roll_day(today, self.ledger, base)

    def close_missing_cancel(self, order: Any, at: int) -> None:
        """취소를 냈는데 계좌 주문내역에 없는 주문을 닫습니다. 대사가 부릅니다.

        닫은 뒤의 결과 줄·상태 옮김은 계좌 알림으로 닫혔을 때와 같은 길입니다(`_take`).
        """
        with self.lock:
            self.book.close_cancelled(order, by="reconcile")
            self._take([], at)

    def _drop(self, at: int, why: str) -> None:
        """미룬 주문을 내지 않습니다. 「안 낸 신호」로 셉니다."""
        self._note_unsent(at)
        self._say_unsent(at, why)

    # ── 주문 ────────────────────────────────────────────

    def _send(self, side: str, quantity: int, signal: float, purpose: str, at: int) -> str | None:
        """주문 하나를 버전의 주문유형으로 냅니다. 낸 주문의 id, 못 냈으면 `None` 입니다."""
        order_type, price, stop_price = self._order_prices(side, signal)
        order_id = f"{self.run.id}-{len(self.book.orders) + 1}"
        if not self._placed(
            order_id,
            side=side,
            quantity=quantity,
            order_type=order_type,
            price=price,
            stop_price=stop_price,
            fill_condition=str(self.aux.get("fillCond") or ""),
            at=at,
            purpose=purpose,
        ):
            return None
        self._purpose[order_id] = purpose
        self._snap[order_id] = self._snapshot_now()
        # 진입 주문의 남은 수량도 사용률에 듭니다(예산 검사와 같은 셈).
        self._note_margin(self.run.day_key)
        self._move(runs.ENTRY_SENT if purpose == "entry" else runs.EXIT_SENT, at)
        shown = "시장가" if price is None else f"{price:g}"
        self._say(at, ORDER, f"{side} {quantity}계약 {order_type} {shown}")
        return order_id

    def _order_prices(self, side: str, signal: float) -> tuple[str, float | None, float | None]:
        """버전의 주문유형으로 낼 주문의 (주문유형, 주문가격, 스탑가격).

        값 칸을 쓰지 않는 유형(시장가·STOP-M)은 주문가격이 `None` 입니다. STOP 은
        스탑 가격이 있어야 발동할 값이 정해져, 스탑 가격 기준(`aux.stopBasis`)으로
        함께 셉니다 — 없이 보내면 켜지지 않는 주문이 됩니다. 주문을 낼 때와 예상
        주문가(`expected_prices`)가 이 셈 하나를 씁니다.
        """
        order_type = str(self.aux.get("orderType") or paper.MARKET_ORDER).strip()
        price = None
        if order_type not in NO_PRICE:
            price = order_price(signal, side, self._price_basis(side), self.tick)
        stop_price = None
        if order_type in STOP_TYPES:
            stop_price = order_price(signal, side, str(self.aux.get("stopBasis") or ""), self.tick)
        return order_type, price, stop_price

    def expected_prices(self) -> dict[str, dict[str, Any] | None]:
        """지금 가격으로 규칙이 걸리면 낼 주문. `buy`·`sell`·`exit` 세 칸입니다.

        신호가 자리에 마지막 체결가(`book.last_price`)를 넣고 실제 주문과 같은 셈
        (`_order_prices`)을 거칩니다. 시장가·STOP-M 의 `price` 는 `None` 입니다.

        - `buy`·`sell` — 그 방향으로 버전의 주문유형으로 낼 주문
          `{orderType, price, stopPrice}`. 규칙이 있든 없든 **두 방향을 늘** 냅니다.
          진입으로 읽을지 청산으로 읽을지는 보유를 보고 가립니다. 마지막 체결가를
          모르면 값이 `None` 입니다.
        - `exit` — 보유를 청산 규칙으로 닫을 때 낼 주문 `{side, orderType, price,
          stopPrice}`. 청산 규칙은 진입과 **같은 주문유형·같은 셈**(`_close` →
          `_send`)이라, 값은 보유 반대 방향의 `buy`·`sell` 칸과 같습니다. 보유가 없거나
          마지막 체결가를 모르면 `None` 입니다. 손절·익절·사용자 청산은 시장가
          전량이라 이 값과 다릅니다.
        """
        last = self.book.last_price
        made: dict[str, dict[str, Any] | None] = {}
        for side in (paper.BUY, paper.SELL):
            if last is None:
                made[side] = None
                continue
            order_type, price, stop_price = self._order_prices(side, float(last))
            made[side] = {"orderType": order_type, "price": price, "stopPrice": stop_price}
        held = self.ledger.position
        leaving = made.get(_CLOSE_SIDE[held.side]) if held.side is not None else None
        made["exit"] = {"side": _CLOSE_SIDE[held.side], **leaving} if leaving is not None else None
        return made

    @property
    def quote_now(self) -> dict[str, Any]:
        """화면에 내는 1호가와 받은 시각(`quoteAt`, 밀리초).

        시세가 끊긴 동안과 아직 못 받았을 때는 모두 `None` 입니다 — 끊기기 전의
        호가를 지금 값으로 그리지 않게 합니다(봉에 적는 호가와 같은 규칙).
        """
        made: dict[str, Any] = {name: None for name in QUOTE_FIELDS}
        if self._quote is not None and self.feed != GAP:
            made.update(self._quote)
            made["quoteAt"] = self._quote_at or None
        return made

    def _price_basis(self, side: str) -> str:
        """그 **주문 방향**의 가격 기준. 방향 칸이 비었으면 공용 칸(`priceBasis`)입니다.

        방향은 규칙이 아니라 주문의 것입니다 — 매수 보유를 닫는 매도 주문도
        매도 기준(`sellPriceBasis`)을 씁니다.
        """
        own = "buyPriceBasis" if side == paper.BUY else "sellPriceBasis"
        return str(self.aux.get(own) or "").strip() or str(self.aux.get("priceBasis") or "")

    def _close(self, quantity: int, signal: float, reason: str, at: int) -> str | None:
        """보유를 닫습니다. 낸 청산 주문의 id, 못 냈으면 `None` 입니다.

        **버전의 주문유형으로 나가는 것은 청산 규칙과 반대 신호뿐입니다.** 손절·익절·사용자
        청산은 「지금 닫는다」는 뜻이라 시장가 전량으로 나갑니다 — 지정가로 걸면
        값이 지나가 버려 들고 있는 채로 남습니다.

        체결조건도 같습니다. 설정의 체결조건(`aux.fillCond`)은 청산 규칙과 진입에만
        쓰고, 시장가 전량 청산은 국내면 늘 FAS(`EXIT_FILL_CONDITION`)로 냅니다. 해외
        주문에는 체결조건이 없습니다.

        반대 신호가 아닌 청산(청산 규칙·손절·익절·추적·세션 끝·사용자 청산)을 내면
        걸려 있던 반전 예약을 버립니다 — 그 청산이 닫은 보유에 반전 진입이 붙지
        않게 합니다.
        """
        if reason != OPPOSITE_EXIT:
            self._drop_reverse(at)
        held = self.ledger.position
        if held.side is None or quantity <= 0:
            return None
        side = _CLOSE_SIDE[held.side]
        self._exit_reason = reason
        if reason in (RULE_EXIT, OPPOSITE_EXIT):
            return self._send(side, quantity, signal, "exit", at)
        order_id = f"{self.run.id}-{len(self.book.orders) + 1}"
        if not self._placed(
            order_id,
            side=side,
            quantity=quantity,
            order_type=paper.MARKET_ORDER,
            fill_condition=EXIT_FILL_CONDITION if self.run.market == "domestic" else "",
            at=at,
            purpose="exit",
        ):
            return None
        self._purpose[order_id] = "exit"
        self._snap[order_id] = self._snapshot_now()
        self._move(runs.EXIT_SENT, at)
        label = REASON_LABELS.get(reason, reason)
        self._say(at, ORDER, f"{label} — {side} {quantity}계약 시장가 전량")
        return order_id

    def _snapshot_now(self) -> dict[str, Any]:
        """지금 낼 주문에 묶을 지표 값 — **마지막으로 닫힌 봉**의 것입니다.

        봉이 닫혀 규칙이 걸린 자리에서는 방금 판정한 그 봉이고, 틱에서 나간
        청산(손절·추적 청산·익절)과 사용자 청산은 그 순간 마지막으로 닫힌
        봉입니다. 어느 세션 창의 봉인지는 가리지 않습니다 — 판정하는 창의
        마지막 봉을 고르면 청산한 순간보다 오래된 값을 「그때의 값」으로 싣게
        됩니다. 봉은 **시작 시각**(`barTime`)으로 가리킵니다.

        값은 `_snap_ids` 의 이름마다 하나이고, 지표가 아직 준비되지 않았으면
        칸을 두고 `None` 입니다. 봉이 하나도 없으면 시각도 값도 `None` 입니다.

        평가 방식이 「틱」이면 판정한 그 순간 **진행 중인 봉**의 값이고, 표지
        (`evalMode`·`partialBar`·`judgedAt`)를 함께 싣습니다(`_tick_mark`).
        """
        if self._seen is not None:
            series, index = self._seen
        elif self.bars.bars:
            # 틱에서 나가는 청산입니다. 낼 때 한 번만 셉니다.
            series = self._series()
            index = len(self.bars.bars) - 1
        else:
            return {"barTime": None, "values": {name: None for name in self._snap_ids}}
        live = self.bars.open_bar if index >= len(self.bars.bars) else None
        made = {
            "barTime": live.timestamp if live is not None else self.bars.bars[index].timestamp,
            "values": {name: _finite(series.at(name, index)) for name in self._snap_ids},
        }
        return {**made, **(self._tick_mark() or {})}

    def _forget(self, order_id: str, at: int = 0) -> None:
        """끝난 주문에 붙여 둔 것을 비우고 결과 줄을 씁니다. 안 비우면 실행 내내 쌓입니다.

        **아직 열려 있는 주문**(취소를 보냈는데 계좌 확인이 안 온 모의 주문)은
        결과를 적지 않고 기다립니다(`_cancel_wait`). 확인 전에 체결되거나
        거절될 수 있어, 보낸 순간에 「취소」로 적으면 틀린 결과가 남습니다.
        """
        purpose = self._purpose.pop(order_id, None)
        self._snap.pop(order_id, None)
        if purpose is None:
            return
        order = next((one for one in self.book.orders if one.id == order_id), None)
        if order is None:
            return
        if order.open:
            self._cancel_wait[order_id] = purpose
            return
        self._write_order(order, purpose, at)

    def _placed(self, order_id: str, **wanted: Any) -> bool:
        """주문 하나를 냅니다. 못 내면 **「확인 필요」로 가고 거짓**입니다.

        계좌 갈래는 거절할 수 있습니다 — 승인 표지가 없거나, 실행이 선 환경이 지금
        활성 환경과 다르거나, 계좌가 답을 안 주거나. 그때 상태를 옮기지 않고 그냥 두면
        엔진은 다음 봉에도 같은 주문을 냅니다.

        계좌가 주문을 **받고 거부한** 것은 다른 자리입니다(`_rejected`) — 진입 거부는
        「확인 필요」가 아니라 중지로 갑니다.
        """
        at = wanted["at"]
        purpose = wanted.pop("purpose", "entry")
        try:
            self.book.place(order_id, **wanted)
        except sim.SimRefused as exc:
            if exc.reason == sim.REJECTED:
                self._rejected(order_id, purpose, exc, wanted)
                return False
            self._say_unsent(at, str(exc), reason=exc.reason)
            # 접수되기 전에 막힌 주문도 결과 줄 하나입니다(거절률의 분자).
            # 계좌 주문번호는 없고, 까닭은 **앱이 막은 문장**이라 `why` 에 둡니다.
            self._keep(
                ORDER_ROW,
                {
                    "orderId": order_id,
                    "orderNo": None,
                    "side": wanted.get("side"),
                    "intent": purpose,
                    "reason": self._reason_of(purpose),
                    "orderType": wanted.get("order_type"),
                    "quantity": wanted.get("quantity"),
                    "filled": 0,
                    "price": wanted.get("price"),
                    "outcome": REFUSED,
                    "why": str(exc),
                    "serverStatus": None,
                    "serverMessage": None,
                    "placedAt": int(at),
                    "endedAt": int(at),
                },
            )
            if exc.flag:
                self._move(runs.FLAG, at)
            elif exc.reason == sim.NOT_SENT:
                # 같은 값으로 다시 내도 같은 자리에서 막힙니다. 계좌 거부와 같이 멈춥니다.
                self._halt(purpose, at)
            return False
        return True

    def _halt(self, purpose: str, at: int) -> None:
        """주문이 거부돼 실행을 멈춥니다. 진입이면 **중지**, 청산이면 **확인 필요**입니다.

        중지로 갈 때는 **열려 있는 진입 주문도 거둡니다** — 사용자가 「중지」를 누를
        때와 같은 규칙입니다(`_withdraw_entries`). 거두지 않으면 중지된 실행의 진입
        주문이 계좌에 남아 체결될 수 있습니다. 청산 주문은 거두지 않습니다 — 들고
        있는 것을 닫으려던 주문입니다.
        """
        if purpose == "entry":
            # 「확인 필요」·「대사 중」에는 계좌로 아무 요청도 내지 않습니다 — 접수 뒤
            # 확인이 그 상태에서 거절을 알려 올 수 있습니다.
            if self.run.state not in runs.QUIET_STATES:
                self._withdraw_entries(at, HALT_WITHDRAW_TEXT)
            self._move(runs.PAUSE, at)
            return
        self._move(runs.FLAG, at)

    def _withdraw_entries(self, at: int, why: str) -> None:
        """열려 있는 진입 주문을 거둡니다. **장부에 맡깁니다** — 종이는 바로 닫히고
        계좌는 취소를 내고 확인을 기다립니다(`_forget`)."""
        for order in list(self.book.open_orders):
            if self._purpose.get(order.id) != "entry":
                continue
            if self.book.cancel(order, why):
                self._forget(order.id, at)

    def confirm_wanted(self, now: int) -> list[str]:
        """접수 뒤 결과를 확인할 때가 된 계좌 주문들의 주문번호. 종이 실행은 늘 비어 있습니다."""
        wanted = getattr(self.book, "confirm_wanted", None)
        if wanted is None:
            return []
        with self.lock:
            return [one.order_no for one in wanted(now)]

    def on_outcome(self, order_no: str, outcome: dict[str, Any] | None, at: int) -> bool:
        """접수 뒤 결과 확인 한 번의 답(`backend/order_outcome.py`). 거절로 닫았으면 참.

        **거절이면 접수 자리에서 거부된 것과 같이 다룹니다**(`_rejected`) — 사건 글은
        계좌가 준 코드와 문구 그대로이고, 진입이면 중지, 청산이면 확인 필요입니다.
        결과 줄은 그 주문이 닫힌 자리에서 거절로 적힙니다(`_write_order`).
        """
        taking = getattr(self.book, "confirmed", None)
        if taking is None:
            return False
        with self.lock:
            found = next(
                (one for one in self.book.orders if one.order_no == order_no), None
            )
            if found is None:
                return False
            purpose = self._purpose.get(found.id, "entry")
            if not taking(found, outcome, at=at):
                return False
            self._take([], at)
            self._halt_rejected(found, purpose, at)
            return True

    def _rejected(self, order_id: str, purpose: str, exc: Any, wanted: dict[str, Any]) -> None:
        """계좌가 **거부한** 주문. 답을 받은 것이라 「결과를 모름」과 다르게 다룹니다.

        사건 글은 서버가 준 코드와 문구 그대로입니다(「주문 거부 5039 — …」). 「안 낸
        신호」와 같은 갈래(`BLOCKED`)로 달아 알림도 같이 서지만, 「주문을 내지
        않았습니다」 머리는 붙이지 않습니다 — 주문은 나갔고 계좌가 받지 않은 것입니다.

        **실행은 멈춥니다.** 그대로 두면 다음 신호에 같은 주문이 같은 까닭으로 다시
        거부됩니다. 어디로 멈출지는 주문의 목적으로 가릅니다.

        - 진입이 거부되면 **중지**입니다. 계좌에 남은 것이 없어 맞춰 볼 것이 없고,
          중지에서도 보유의 손절·익절 감시는 돕니다. 설정을 고친 뒤 재개하면 됩니다.
          앞서 나가 있던 진입 주문이 열려 있으면 함께 거둡니다(`_halt`).
        - 청산이 거부되면 **확인 필요**입니다. 보유를 닫지 못한 채이고, 중지로 두면
          손절·익절 감시가 체결마다 같은 청산을 다시 내 같은 거부를 되풀이합니다.
          확인 필요에서는 사용자가 볼 때까지 아무 요청도 나가지 않습니다.
        """
        at = wanted["at"]
        text = str(exc)
        self._say(
            at,
            BLOCKED,
            text,
            extra={"reason": sim.REJECTED, "why": text, "code": exc.code},
        )
        self._keep(
            ORDER_ROW,
            {
                "orderId": order_id,
                "orderNo": None,
                "side": wanted.get("side"),
                "intent": purpose,
                "reason": self._reason_of(purpose),
                "orderType": wanted.get("order_type"),
                "quantity": wanted.get("quantity"),
                "filled": 0,
                "price": wanted.get("price"),
                "outcome": REJECTED,
                "why": text,
                "serverStatus": REJECTED,
                "serverCode": exc.code or None,
                "serverMessage": exc.detail or None,
                "placedAt": int(at),
                "endedAt": int(at),
            },
        )
        self._halt(purpose, at)

    def _write_order(self, order: Any, purpose: str, at: int) -> None:
        """끝난 주문 하나의 결과 줄.

        결과는 다섯 가운데 하나입니다(`FILLED`·`PARTIAL`·`CANCELLED`·`REJECTED`·
        `REFUSED`). 모의 주문의 거절은 주문 상태로는 취소와 한 갈래라, **계좌
        알림이 준 상태 글자**(`serverStatus`)로 가립니다. 계좌 알림에는 서버 문장이
        실려 오지 않아 알림으로 닫힌 주문은 `serverMessage` 가 비어 있습니다. 접수
        뒤 확인(`on_outcome`)으로 거절을 잡았으면 `serverCode`·`serverMessage` 에
        계좌가 준 값이 있습니다. 앱이 남긴 까닭(세션 마감 취소, FOK 취소 같은 것)은
        `why` 에 둡니다.
        """
        filled = int(getattr(order, "filled", 0) or 0)
        quantity = int(getattr(order, "quantity", 0) or 0)
        status = str(getattr(order, "last_status", "") or "")
        if quantity and filled >= quantity:
            outcome = FILLED
        elif status == "rejected":
            outcome = REJECTED
        elif filled > 0:
            outcome = PARTIAL
        else:
            outcome = CANCELLED
        self._keep(
            ORDER_ROW,
            {
                "orderId": order.id,
                "orderNo": getattr(order, "order_no", "") or None,
                "side": order.side,
                "intent": purpose,
                "reason": self._reason_of(purpose),
                "orderType": order.order_type,
                "quantity": quantity,
                "filled": filled,
                "price": order.price,
                "outcome": outcome,
                "why": getattr(order, "reason", "") or None,
                "serverStatus": status or None,
                # 접수 뒤 확인으로 거절을 잡았으면 계좌가 준 코드·문구입니다. 알림으로
                # 닫혔으면 비어 있습니다 — 알림에는 거절 문구 칸이 없습니다.
                **(
                    {"serverCode": code}
                    if (code := getattr(order, "server_code", "")) else {}
                ),
                "serverMessage": getattr(order, "server_message", "") or None,
                "placedAt": int(getattr(order, "created", 0) or 0),
                "endedAt": int(at),
                **({"tradingDay": day} if (day := self._day_text(int(at))) is not None else {}),
                # 계좌 알림 없이 대사가 닫은 주문이면 그 표지(`reconcile`).
                **({"closedBy": by} if (by := getattr(order, "closed_by", "")) else {}),
            },
        )

    def _watch(self, at: int) -> None:
        """손절·추적 청산·익절을 틱마다 봅니다. 함께 닿으면 **그 차례로 하나만**입니다.

        추적 청산의 기준(가장 유리했던 체결가)은 **여기서만** 올립니다 — 감시하지
        않는 상태이거나 시세가 끊긴 동안에는 들어온 값을 기준에 넣지 않습니다.
        청산 주문이 나가 있는 동안(`exiting`)도 감시하지 않으므로 두 번째 청산이
        나가지 않습니다.
        """
        if self.run.state not in runs.WATCH_STATES or self.feed == GAP:
            return
        held = self.ledger.position
        last = self.book.last_price
        if held.side is None or last is None:
            return
        way = 1 if held.side == paper.BUY else -1
        stop, take, trail, best = self._exit_lines(last)
        if best is not None:
            self._trail_best = best

        if stop is not None and ((last <= stop) if way > 0 else (last >= stop)):
            self._close(held.size, last, STOP_LOSS, at)
            return
        if trail is not None and _reached(last, trail, -way, self.tick):
            self._close(held.size, last, TRAIL_STOP, at)
            return
        if take is not None and ((last >= take) if way > 0 else (last <= take)):
            self._close(held.size, last, TAKE_PROFIT, at)

    def _watch_after_gap(self, at: int) -> None:
        """끊긴 뒤 첫 체결의 감시. 손절·익절이 나가면 **그 체결 값에** 바로 채웁니다.

        끊긴 동안에는 호가가 없어, 지연 뒤 첫 호가를 기다리면 그 값이 아직 없는
        값입니다. 종이 장부만 이렇게 채웁니다 — 계좌 갈래는 계좌가 채웁니다.
        """
        before = len(self.book.orders)
        self._watch(at)
        fill_now = getattr(self.book, "fill_now", None)
        last = self.book.last_price
        if fill_now is None or last is None or not self.virtual:
            return
        for order in self.book.orders[before:]:
            if self._purpose.get(order.id) == "exit":
                self._take(fill_now(order, last, at=at), at)

    @property
    def money_known(self) -> bool:
        """손익을 돈으로 셀 수 있는지(계약 승수를 아는지)."""
        return self.run.multiplier is not None

    @property
    def model(self) -> str:
        """이 실행의 체결 모형 이름. 실행에 적힌 것이 먼저입니다."""
        if self.run.model:
            return self.run.model
        return sim.MODEL if self.run.mode in runs.ACCOUNT_MODES else paper.MODEL

    def set_auction(self, span: tuple[int, int] | None) -> None:
        """단일가 구간 `[시작, 끝)`(밀리초)을 알려 줍니다. 종이 장부만 그 시각의 체결을 미룹니다(`paper` 가정 8)."""
        with self.lock:
            if hasattr(self.book, "auction"):
                self.book.auction = span

    def on_limits(self, upper: Any, lower: Any) -> None:
        """실시간 상한가·하한가. 수가 아니면 그 값은 그대로 둡니다(`paper` 가정 8)."""
        with self.lock:
            if not hasattr(self.book, "upper"):
                return
            if isinstance(upper, (int, float)) and not isinstance(upper, bool):
                self.book.upper = float(upper)
            if isinstance(lower, (int, float)) and not isinstance(lower, bool):
                self.book.lower = float(lower)

    def _exit_lines(
        self, last: float | None
    ) -> tuple[float | None, float | None, float | None, float | None]:
        """지금 보유의 손절·익절·추적 청산 값과 추적의 기준(가장 유리했던 값).

        칸이 비어 있는 것은 `None` 입니다. `last` 를 주면 추적의 기준에 넣어 셉니다
        — 주지 않으면 지금까지의 기준 그대로입니다. 기준을 바꿔 담지는 않습니다.
        """
        held = self.ledger.position
        if held.side is None:
            return None, None, None, None
        way = 1 if held.side == paper.BUY else -1
        stop_gap = self._distance("stopTicks", "stopUnit", held.avg_price)
        take_gap = self._distance("takeTicks", "takeUnit", held.avg_price)
        stop = held.avg_price - stop_gap * way if stop_gap else None
        take = held.avg_price + take_gap * way if take_gap else None

        trail = best = None
        if self._distance("trailTicks", "trailUnit", held.avg_price):
            best = self._trail_best if self._trail_best is not None else held.avg_price
            if last is not None:
                best = max(best, last) if way > 0 else min(best, last)
            trail = best - (self._distance("trailTicks", "trailUnit", best) or 0) * way
        return stop, take, trail, best

    def exit_lines(self) -> list[float]:
        """지금 감시 중인 손절·익절·추적 청산 값. 감시하지 않으면 빈 목록입니다.

        지난 봉으로 돌려 보기(`backtest.py`)가 봉 안의 길에 이 값들을 점으로 끼워,
        손절이 그 값에서 발동하게 합니다.
        """
        with self.lock:
            if self.run.state not in runs.WATCH_STATES:
                return []
            return [one for one in self._exit_lines(None)[:3] if one is not None]

    def _distance(self, value_key: str, unit_key: str, base: float) -> float | None:
        """손절·익절·추적 청산의 거리(가격). 칸이 비었거나 셀 수 없으면 `None` 입니다.

        틱 단위는 **정수 틱 × 틱 크기**로 셉니다. `base` 는 %의 기준
        값입니다. ATR 단위는 ATR 이 아직 없으면 `None` — 그때는 진입 자체를 막아
        두므로(`_entry_gate`) 손절 없는 보유가 생기지 않습니다.
        """
        unit = str(self.aux.get(unit_key) or UNIT_TICK)
        if unit == UNIT_TICK:
            ticks = _int(self.aux.get(value_key)) or 0
            return ticks * self.tick if ticks > 0 else None
        value = _float(self.aux.get(value_key))
        if value is None or value <= 0:
            return None
        if unit == UNIT_PRICE:
            return value
        if unit == UNIT_PCT:
            return abs(base) * value / 100.0
        if unit == UNIT_ATR:
            return value * self._atr if self._atr else None
        return None

    def _entry_gate(self, bar: indicators.Bar | None, *, moment: int | None = None) -> str | None:
        """이 전략의 실행 필터가 막는 까닭. 막을 것이 없으면 `None` 입니다.

        `runs.entry_block` 이 본 것(상태·공백·한도·보유) 뒤에 봅니다. 청산은 이
        자리를 지나지 않으므로 막지 않습니다.

        `moment` 를 주면 그 시각(주문을 내는 순간)으로 봅니다 — 미룬 일봉 진입을 낼
        때입니다. 안 주면 판정한 봉으로 보고, **일봉은 신규 진입 시간을 보지
        않습니다** — 일봉의 주문은 다음 세션에 나가므로 그때 봅니다(`_release`).
        """
        at = moment if moment is not None else (bar.timestamp if bar is not None else 0)
        if self._closing_until and at < self._closing_until:
            return runs.WHY_SESSION_END
        if moment is not None:
            if not self._in_window_at(moment):
                return runs.WHY_ENTRY_WINDOW
        elif bar is not None and not self.bars.daily and not self._in_window(bar, self._closed_at(bar)):
            return runs.WHY_ENTRY_WINDOW
        if self._wait_left > 0:
            return runs.WHY_REENTRY_WAIT
        most = _int(self.aux.get("maxEntriesPerDay"))
        if most and self.run.intents_today >= most:
            return runs.WHY_DAILY_ENTRIES
        if self._uses_atr and self._atr is None:
            return runs.WHY_ATR_NOT_READY
        return None

    def _closed_at(self, bar: indicators.Bar) -> int:
        """그 봉이 **닫히는** 시각(밀리초) — 진입 주문이 나가는 때입니다.

        분봉은 봉 시작 + 봉 길이이고, 일봉은 봉 길이가 정해져 있지 않아 지금
        판정하는 순간(마지막 체결 시각)입니다.
        """
        minutes = bar_lib.minutes_of(self.bars.base_bar)
        if minutes:
            return bar.timestamp + minutes * 60_000
        return self.last_trade_at or bar.timestamp

    def _in_window(self, bar: indicators.Bar, closed_at: int | None = None) -> bool:
        """그 봉이 **닫히는 시각**이 신규 진입 시간 안인지. 시작은 넣고 끝은 뺍니다.

        주문은 봉이 닫힌 뒤에 나가므로 봉 마감 시각으로 봅니다 — 봉 시작으로 보면
        긴 봉의 주문이 창 끝을 넘겨 나갑니다. 시각은 조건의 시각 값과 같은 셈
        (`rules.clock_of`, 야간은 자정 뒤에 하루를 더함)이라 세션 안의 차례로
        견줍니다. 끝이 시작보다 이르면 다음 날까지입니다. 칸이 비면 그쪽 끝이
        없습니다(세션 시작·끝).
        """
        return self._in_window_at(closed_at if closed_at is not None else self._closed_at(bar))

    def _in_window_at(self, moment: int) -> bool:
        """그 시각(밀리초)이 신규 진입 시간 `[시작, 끝)` 안인지. 봉과 미룬 진입이 같은 셈을 씁니다."""
        start = rules.parse_clock(self.aux.get("entryStart"))
        end = rules.parse_clock(self.aux.get("entryEnd"))
        if start is None and end is None:
            return True
        clock = rules.bar_clock(moment, self.run.session)
        low = rules.clock_of(*start, self.run.session) if start else None
        high = rules.clock_of(*end, self.run.session) if end else None
        if low is not None and high is not None and high <= low:
            # 다음 날까지 이어지는 창입니다.
            return clock >= low or clock < high
        return (low is None or clock >= low) and (high is None or clock < high)

    def note_session_end_unknown(self, at: int, text: str) -> None:
        """세션 끝 정리를 켰는데 끝 시각을 몰라 정리하지 못합니다. **거래일마다 한 번** 남깁니다.

        날은 실행의 거래일 열쇠로 셉니다(`_trading_slot`) — 시간표를 모르면 그 열쇠도
        달력 날짜로 내려가므로 셀 수 있습니다. 박자마다 남기면 사건 목록이 이 줄로 찹니다.
        """
        with self.lock:
            day = self._trading_slot(int(at), self.run.session)
            if day == self._session_end_unknown_day:
                return
            self._session_end_unknown_day = day
            self._say(at, SESSION, text)

    def session_end(self, at: int, until: int) -> bool:
        """세션 끝 정리 — 진입 미체결을 거두고, 보유를 닫고, `until` 까지 새 진입을 막습니다.

        **거두기와 알림은 세션마다 한 번**입니다(같은 `until` 이면 다시 하지 않습니다).
        **닫기는 창 안에서 부를 때마다** 봅니다 — 계좌 갈래는 취소가 요청일 뿐이라
        닿기 전에 체결이 오면 정리 뒤에 보유가 생기고, 청산이 거절되거나 일부만
        채워져도 보유가 남습니다. 그 보유를 세션 끝까지 두지 않습니다. 청산 주문이
        나가 있는 동안(`exiting`)과 확인을 기다리는 동안은 감시 상태가 아니라 다시
        내지 않습니다. 계좌 갈래는 보내기 전 검사를 그대로 지나고, 막히면 「확인
        필요」입니다. 한 일이 있으면 참입니다.
        """
        with self.lock:
            did = False
            if self._closing_until != until:
                self._closing_until = until
                for order in list(self.book.open_orders):
                    if self._purpose.get(order.id) != "entry":
                        continue
                    # 종이는 취소가 바로 닫혀 여기서 결과를 적습니다. 계좌는 취소가
                    # 요청이라 주문을 그대로 붙들어 둡니다 — 확인이 오면 체결 자리가
                    # 결과를 적고 상태를 옮기고, 취소가 닿기 전에 채워졌으면 보유가
                    # 되어 아래 닫기가 집어 듭니다.
                    if self.book.cancel(order, "세션 끝 정리로 취소했습니다") and self.virtual:
                        self._forget(order.id, at)
                if self.virtual and self.run.state == runs.ENTERING:
                    self._move(runs.ENTRY_CANCELLED, at)
                self._say(at, SESSION, "세션 끝 정리 — 보유를 닫고 새로 들어가지 않습니다")
                did = True
            held = self.ledger.position
            if held.side is not None and self.run.state in runs.WATCH_STATES:
                last = self.book.last_price or held.avg_price
                self._close(held.size, last, SESSION_END, at)
                did = True
            return did

    def _follow(self, before: str | None, fill: dict[str, Any]) -> None:
        """체결 하나로 추적 청산의 기준을 맞춥니다.

        보유가 **새로 열리거나 방향이 뒤집히면** 기준을 비웁니다. 첫 감시
        (`_watch`)가 그때의 **평균가**로 세웁니다 — 한 주문이 여러 값에 나눠
        채워져도 출발점은 평균가 하나입니다.

        이미 감시 중인 보유에 같은 방향으로 더 들어가면 기준을 그대로 두고,
        **새 평균가가 더 유리할 때만** 그 값으로 옮깁니다 — 평균가가 나빠졌다고
        따라온 거리를 버리지 않고, 평균가가 기준을 넘어섰으면 추적선이 평균가
        뒤에 처지지 않게 합니다. 보유가 0 이 되면 지웁니다.
        """
        held = self.ledger.position
        if held.side is None or held.side != before:
            self._trail_best = None
            return
        if self._trail_best is None or fill.get("side") != held.side:
            return
        if held.side == paper.BUY:
            self._trail_best = max(self._trail_best, held.avg_price)
        else:
            self._trail_best = min(self._trail_best, held.avg_price)

    def _take(self, made: list[dict[str, Any]], at: int) -> None:
        """종이 체결을 장부에 넣고 상태를 옮깁니다."""
        for one in made:
            order_id = str(one.get("orderId") or "")
            purpose = self._purpose.get(order_id, "entry")
            row = {
                **one,
                "runId": self.run.id,
                "intent": purpose,
                "reason": self._reason_of(purpose),
                "virtual": self.virtual,
                # 그 주문을 낸 순간의 지표 값. **이 실행이 내지 않은 주문의 체결이면
                # `None` 입니다** — 칸을 빼지 않는 것은, 칸이 없는 옛 기록과
                # 「이 체결에는 값이 없다」를 갈라 읽게 하려는 것입니다.
                "snapshot": self._snap.get(order_id),
                # 이 체결 한 건의 수수료. 장부가 누계와 거래 합계에 더하는 값과
                # **같은 셈**입니다(`Ledger.cost_of`). 기록에 쓰기 전에 붙여 기록
                # 줄에도 실립니다. 체결을 알려 준 쪽이 같은 이름의 값을 실어
                # 와도 이 값이 이깁니다 — 장부의 누계와 맞아야 하기 때문입니다.
                "fee": self.ledger.cost_of(one.get("quantity")),
                # 수수료의 거래소 몫과 청산 몫. API 에 둘을 나눈 칸이 없어 설정값
                # 전부가 거래소 몫이고 청산 몫은 모릅니다(`paper` 첫머리).
                "feeExchange": self.ledger.cost_of(one.get("quantity")),
                "feeClearing": None,
                # 이 체결을 채운 모형. 모형이 바뀌어도 옛 체결은 그때 이름 그대로입니다.
                "model": self.model,
            }
            # **장부에 먼저 넣고** 이 체결의 실현손익을 줄에 싣습니다. 체결 목록·방송·
            # 기록이 모두 이 한 줄(같은 dict)을 쓰므로 어디서 보든 같은 값입니다.
            before = self.ledger.position.side
            realized_before = self.ledger.realized
            closed = self.ledger.add(row)
            # 이 체결로 줄어든 보유의 실현손익 — 장부 실현손익이 늘어난 몫이라, 체결
            # 줄들을 더하면 장부 누계와 같습니다. **수수료 전**이고(수수료는 `fee`),
            # 실행 통화·승수 그대로입니다. 보유를 늘리기만 한 체결(진입)은 `None` 이고
            # 본전 청산은 0 입니다. 종이·계좌 갈래 모두 장부가 받은 체결로 셉니다.
            reduced = before is not None and row.get("side") != before
            row["realized"] = (
                self.ledger.realized - realized_before if reduced and self.money_known else None
            )
            self.fills.append(row)
            if self.fill_count is not None:
                self.fill_count += 1
            if len(self.fills) > self._history:
                del self.fills[0 : len(self.fills) - self._history]
            self._keep(FILL, row)
            self._follow(before, row)
            if before is not None and self.ledger.position.side is None:
                self._flat_by = order_id
                # 보유가 0 이 됐습니다. 어떤 청산이었든 그 뒤 판정 봉 N개는 쉽니다.
                self._wait_left = _int(self.aux.get("reentryWaitBars")) or 0
            if closed is not None:
                closed["model"] = self.model
                if not self.money_known:
                    # 승수를 몰라 손익을 돈으로 셀 수 없습니다. 분석은 이 거래를 합계에서 뺍니다.
                    closed["grossPl"] = closed["netPl"] = None
                traded_on = self._day_text(int(closed["closedAt"]))
                if traded_on is not None:
                    # 해외 거래의 거래일. 기록이 시각만으로는 셀 수 없습니다(`records.day_of_row`).
                    closed["tradingDay"] = traded_on
                self._keep("trade", closed)
            self._say(at, FILL, f"{one['quantity']}계약 {one['price']:g} 체결")
        if made:
            # 체결로 보유가 늘면 그 순간의 사용률도 그 거래일 최대에 듭니다.
            self._note_margin(self.run.day_key)

        for order in list(self._purpose):
            found = next((one for one in self.book.orders if one.id == order), None)
            if found is None or found.open:
                continue
            purpose = self._purpose[order]
            self._forget(order, at)
            self._settle(purpose, found, at)

        # 취소를 보내고 확인을 기다리던 주문이 닫혔으면 이제 결과를 적습니다.
        for order in list(self._cancel_wait):
            found = next((one for one in self.book.orders if one.id == order), None)
            if found is None or found.open:
                continue
            self._write_order(found, self._cancel_wait.pop(order), at)

    def _settle(self, purpose: str, order: paper.PaperOrder, at: int) -> None:
        if purpose == "entry":
            if order.filled:
                self._move(runs.ENTRY_FILLED, at)
            else:
                self._move(runs.ENTRY_CANCELLED, at)
            return
        if self.ledger.position.quantity == 0:
            self._exit_reason = ""
            self._move(runs.EXIT_FILLED, at)
            self._turn(order, at)
            return
        # 주문은 끝났는데 아직 들고 있습니다. **보유로 돌려놓습니다** — 그대로
        # 두면 채울 주문이 없는 채로 청산 중에 머물러 다음 봉도 판정하지
        # 않습니다. 사용자가 보고 다시 청산할 수 있어야 합니다.
        self._move(runs.EXIT_CANCELLED, at)
        self._say(at, ORDER, f"청산이 다 안 됐습니다 — {self.ledger.position.size}계약 남았습니다")
        self._turn(order, at)

    @property
    def per_contract(self) -> float | None:
        """계약 하나에 드는 돈. **모르면 `None` 입니다.**

        화면의 「사용 증거금 = (보유 + 진입 미체결 + 추가) × **계약당**」이 서버
        예산 검사와 같은 값으로 서게 하는 칸입니다. 셈하는 자리는 검사와
        하나입니다(`runs.per_contract`).

        **선물·스프레드는 고정값**(계약당 개시증거금)이지만 **옵션 매수는 지금
        값으로 잰 것**입니다 — 프리미엄 × 승수라 틱마다 바뀝니다. 검사는 그
        봉의 종가로 세고 여기서는 마지막 체결가로 세므로, 둘은 **같은 규칙의
        다른 순간**입니다. 화면이 확정값으로 읽으면 안 됩니다.

        **방향이 갈리면 `None` 입니다.** 줄에는 방향이 없는데 옵션은 매수와
        매도의 답이 다릅니다(매도는 거래소가 셈하는 값이라 모릅니다). 두 방향이
        다 **발사할 수 있는** 옵션 실행은 다음 신호가 어느 쪽일지 모르므로,
        숫자 하나를 내면 그중 한쪽에는 거짓이 됩니다.

        세는 잣대는 「켜짐」이 아니라 **「발사할 수 있나」**입니다 — 켜져 있어도
        조건이 0 개면 그 규칙은 안 나갑니다(`rules.steps_of`).
        """
        return self._per_contract()[0]

    @property
    def per_contract_basis(self) -> str:
        """계약당 값이 **어떻게 나온 것인지**(`BASIS_*`).

        숫자만 내면 화면이 어림을 확정값으로 읽습니다. 옵션 매수는 마지막
        체결가로 잰 값이라 틱마다 바뀌고, 매도는 아예 못 내는데, 셋 다 그냥
        숫자나 `null` 로 나가면 화면이 구별할 수 없습니다.
        """
        return self._per_contract()[1]

    def _per_contract(self) -> tuple[float | None, str]:
        """계약당 값과 그 근거. 두 프로퍼티가 같은 셈을 나눠 씁니다."""
        # **「켜짐」이 아니라 「발사할 수 있나」입니다.** 규칙이 ON 인데 조건이
        # 0 개인 설정을 저장이 받아 주고(저장 검사는 경고만 냅니다), 그 규칙은 절대
        # 발사하지 않습니다. `on` 칸만 보면 나갈 수 없는 방향까지 세어, 매수만
        # 나갈 수 있는 옵션 실행이 「방향이 갈린다」로 잡힙니다. 판정은 엔진의
        # 발사 경로가 쓰는 그 함수로 합니다(`rules.steps_of`).
        sides = {
            _SIDE_OF[key]
            for key in _ENTRY_RULES
            if isinstance(self.rules.get(key), dict) and rules.steps_of(self.rules[key])
        }
        if not sides:
            # 나갈 수 있는 방향이 없으면 어느 쪽으로도 안 나갑니다 — 둘 다 봅니다.
            sides = set(_SIDE_OF.values())

        if self.run.kind != runs.OPTION:
            if runs.budget_unchecked(self.run):
                return None, BASIS_NO_MARGIN_FIELD
            return runs.per_contract(self.run, paper.BUY, self.book.last_price), BASIS_MARGIN
        if sides == {paper.SELL}:
            return None, BASIS_SELL_UNKNOWN
        if sides != {paper.BUY}:
            return None, BASIS_MIXED
        return (
            runs.per_contract(self.run, paper.BUY, self.book.last_price),
            BASIS_PREMIUM_NOW,
        )

    @property
    def open_entry(self) -> int:
        """나가 있는 **진입** 주문의 남은 수량.

        **한도 검사가 보는 값과 같습니다**(`runs.entry_block`). 화면이 「사용
        증거금」을 그릴 때 이 값을 쓰는데, 서버가 막는 셈과 화면이 보이는 셈이
        다르면 사용자는 막힌 까닭을 못 찾습니다 — 그래서 따로 세지 않고 한
        자리에서 꺼냅니다.

        취소를 낸 주문도 **확인이 오기 전까지는** 여기 들어옵니다(`open_orders`).
        계좌에서 아직 살아 있는 주문이라, 그 잔량은 여전히 자리를 잡고 있습니다.
        """
        return self._open_quantity("entry")

    @property
    def open_exit(self) -> int:
        """나가 있는 **청산** 주문의 남은 수량(`open_entry` 와 같은 셈)."""
        return self._open_quantity("exit")

    def _open_quantity(self, purpose: str) -> int:
        return sum(
            one.remaining
            for one in self.book.open_orders
            if self._purpose.get(one.id) == purpose
        )

    @property
    def open_orders(self) -> list[dict[str, Any]]:
        """아직 안 채워진 가상 주문들. **장부의 주문이 아니라 화면이 그리는 줄**입니다.

        **체결 줄과 이름이 같습니다**(`orderId`·`time`·`side`·`intent`·`reason`).
        화면이 「가상 체결」과 「가상 미체결」을 한 표로 그리는데 칸 이름이
        갈리면 같은 주문이 두 탭에서 다르게 보입니다.

        **지금 열려 있는 것 전부**입니다 — 쌓이는 값이 아니라 그 순간의 모습
        이라, 받는 쪽은 이 목록을 **덮어씁니다**. 이어 붙이면 채워진 주문이
        표에서 안 사라집니다.
        """
        return [self._order_row(one) for one in self.book.open_orders]

    def _order_row(self, order: paper.PaperOrder) -> dict[str, Any]:
        """열린 주문 하나를 화면이 읽는 줄로.

        낸 수량과 남은 수량을 **둘 다** 냅니다 — 부분체결이면 「3 중 1 남음」이
        되어야 하는데, 남은 것만으로는 그 말을 세울 수 없습니다.

        값이 없는 칸은 `None` 입니다. 시장가에 가격을 0 으로 적으면 화면이
        「0원에 걸었다」로 읽습니다.
        """
        purpose = self._purpose.get(order.id, "entry")
        made = {
            "orderId": order.id,
            "runId": self.run.id,
            # 낸 시각입니다. 체결 줄의 `time` 은 채워진 시각이고, 이 줄은 아직
            # 안 채워진 주문이라 그 값이 없습니다.
            "time": order.created,
            "side": order.side,
            "intent": purpose,
            "reason": self._reason_of(purpose),
            "quantity": order.quantity,
            "remaining": order.remaining,
            "price": order.price,
            "stopPrice": order.stop_price,
            "orderType": order.order_type,
            "status": order.status,
            "virtual": self.virtual,
            # 계좌가 준 주문번호. **모의 주문에만 있습니다** — 종이 주문은 계좌로
            # 나가지 않아 번호가 없고 `None` 입니다. 이름은 체결 줄의 것과 같습니다.
            "orderNo": getattr(order, "order_no", "") or None,
        }
        # 「틱」 판정에서 나간 주문이면 그 표지를 함께 싣습니다(`_tick_mark`). 봉
        # 마감 판정의 주문은 줄 모양이 그대로입니다.
        taken = self._snap.get(order.id) or {}
        if taken.get("evalMode"):
            for name in TICK_MARK_FIELDS:
                made[name] = taken.get(name)
        return made

    def _reason_of(self, purpose: str) -> str:
        """그 주문이 나간 까닭. 체결 줄과 열린 주문 줄이 같은 값을 씁니다."""
        return self._exit_reason if purpose == "exit" else ENTRY_REASON

    @property
    def virtual(self) -> bool:
        """이 실행의 체결과 주문이 **이 앱이 지어낸 것인지.**

        종이 실행은 참입니다 — 장부도 체결도 이 앱이 만듭니다. **계좌 갈래(모의·실전) 실행은
        거짓입니다** — 주문이 계좌로 나가고 체결도 계좌가 채웁니다. 그것을
        「가상」으로 적으면 사용자가 **실제로 난 체결을 지어낸 것으로** 읽습니다.

        체결 줄과 열린 주문 줄이 **같은 자리에서** 가져갑니다. 둘이 갈리면 한
        주문이 표마다 다른 표지를 답니다.
        """
        return self.run.mode not in runs.ACCOUNT_MODES

    # ── 알리기 ──────────────────────────────────────────

    def _gap(self, at: int, why: str) -> None:
        """시세 공백에 듭니다.

        **「확인 필요」 동안의 공백은 사건으로 적지 않습니다.** 그 상태의 실행은
        시세를 보지 않아 구독을 놓고(`service.watching`), 그 시장에 이 실행 말고
        보는 것이 없으면 실시간 연결도 닫힙니다. 그것은 끊긴 것이 아니라 이 앱이
        놓은 것이라 「연결이 끊겼습니다」로 적으면 사실과 다릅니다. 공백 표지는
        그대로 세워 둡니다 — 확인 뒤 다시 볼 때 그 사이 받지 못한 시세를 공백으로
        다뤄야 하고, 그 일은 확인하는 자리가 적습니다(`after_attention`).
        """
        if self.feed == GAP:
            return
        self.feed = GAP
        self.book.gap = True
        if self.run.state == runs.ATTENTION:
            self._gap_unsaid = True
            return
        seen = self.last_trade_at or at
        self._say(at, GAP, f"데이터 공백 — {why}(마지막 수신 {_clock(seen)})")

    def _recover(self, at: int) -> None:
        if self.feed != GAP:
            return
        self.feed = "live"
        self.book.gap = False
        self.quiet_since = 0
        self._after_gap = True
        self._tick_rest_from = int(at)
        if self._gap_unsaid:
            # 적지 않은 공백이 풀린 것입니다. 「다시 들어옵니다」만 홀로 서지 않게 합니다.
            self._gap_unsaid = False
            return
        self._say(at, GAP, "시세가 다시 들어옵니다")

    def after_attention(self, at: int) -> None:
        """「확인 필요」에서 풀린 직후. 그동안 적지 않은 공백이 있으면 지금 적습니다.

        풀린 실행은 다시 시세를 보지만, 확인을 기다리는 동안의 시세는 받지 않았습니다.
        그 공백을 여기서 사건으로 적고 「체결이 오지 않습니다」 공백과 같은 자리로
        세웁니다 — 다시 건 구독에 체결이 오면 첫 체결에서 풀리고(`_recover`), 오지
        않으면 시장이 흘렀는지 묻는 길(`pump.probes_wanted`)을 탑니다.
        """
        with self.lock:
            if not self._gap_unsaid or self.feed != GAP:
                self._gap_unsaid = False
                return
            self._gap_unsaid = False
            self.quiet_since = int(at)
            self.probe_asked = False
            seen = self.last_trade_at or at
            self._say(at, GAP, f"데이터 공백 — {ATTENTION_GAP}(마지막 수신 {_clock(seen)})")

    def _move(self, event: str, at: int) -> None:
        before = self.run.state
        after = self.run.move(event)
        if after != before:
            self._say(at, STATE, f"{runs.STATE_LABEL.get(after, after)}")

    def _say_unsent(self, at: int, why: str, *, reason: str | None = None) -> None:
        """주문을 내지 않은 사건 한 줄. 글은 머리 + 까닭이고, 코드와 까닭을 칸으로도 싣습니다."""
        code = reason or BLOCK_REASONS.get(why, OTHER_REASON)
        self._say(at, BLOCKED, UNSENT_HEAD + why, extra={"reason": code, "why": why})

    def _say(
        self,
        at: int,
        kind: str,
        text: str,
        *,
        keep: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """실행에 한 줄 답니다. 화면이 보고, 기록에도 남습니다.

        **`keep=False` 면 화면에만 답니다.** 거래에서 일어난 일이 아니라 앱이
        값을 못 꺼냈다는 **운영 사고**가 그렇습니다 — 그런 줄이 거래 기록에
        섞이면 나중에 그 실행이 무엇을 했는지 읽을 때 사이에 끼어듭니다.
        """
        made = {"time": int(at), "runId": self.run.id, "kind": kind, "text": text, **(extra or {})}
        if kind == BLOCKED and self._judging and made.get("reason") != sim.REJECTED:
            # 규칙이 걸렸는데 주문을 안 낸 것을 거래일(KST 날짜)마다 셉니다. 이벤트
            # 목록은 잘리므로 화면이 그것을 세지 않게 수를 따로 들고 있습니다
            # (`unsent_today`). 판정 밖에서 난 막힘(손절 등)은 세지 않습니다.
            # **계좌의 거부는 세지 않습니다** — 주문은 나갔고 계좌가 받지 않은
            # 것입니다. 접수 자리의 거부든 뒤늦은 거절이든 같게 셉니다.
            self._note_unsent(at)
        self.events.append(made)
        if len(self.events) > self._history:
            del self.events[0 : len(self.events) - self._history]
        if keep:
            self._keep("event", made)

    def _note_unsent(self, at: int) -> None:
        """「안 낸 신호」 하나를 그 순간이 속한 날(KST)에 셉니다."""
        day = _kst_day(at)
        if day != self._unsent_day:
            self._unsent_day, self._unsent_count = day, 0
        self._unsent_count += 1

    def _keep(self, kind: str, row: dict[str, Any]) -> None:
        """기록에 한 줄 남깁니다. 남기지 못해도 돌던 것을 멈추지 않습니다."""
        if self.record is None:
            return
        self.record(kind, row)

    def _profit(self) -> float:
        return self.ledger.profit(self.book.last_price)

    # ── 내기 ────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """화면이 검증 화면에 그리는 한 벌. 이름은 프론트가 쓰는 것과 같습니다."""
        with self.lock:
            return self._snapshot()

    def _snapshot(self) -> dict[str, Any]:
        return {
            "run": runs.row_of(
                self.run,
                self.ledger,
                feed=self.feed,
                feed_at=self.last_trade_at,
                last_price=self.book.last_price,
                lastPrice=self.book.last_price,
                bars=len(self.bars.bars),
                warmup=self.warmup,
                model=self.model,
                unsentToday=self.unsent_today(self._clock_now()),
                **self.stats,
            ),
            # 마지막 판정 봉의 조건 표 — 봉마다 쌓지 않습니다.
            "conditions": self._conditions,
            "fills": list(self.fills),
            "events": list(self.events),
            "openOrders": self.open_orders,
            "curve": list(self.curve),
            "trades": list(self.ledger.trades),
        }


def _shifted(stage: rules.Stage, drop: int) -> rules.Stage:
    """봉 목록의 앞이 `drop` 개 잘렸을 때 상태의 자리 번호를 그만큼 당깁니다."""
    if not drop:
        return stage
    return rules.Stage(
        step=stage.step,
        deadline=stage.deadline - drop if stage.deadline >= 0 else stage.deadline,
        origin=stage.origin - drop if stage.origin >= 0 else stage.origin,
    )


def _warmup_of(rule_set: dict[str, Any]) -> int:
    """그 규칙들이 쓰는 지표 중 가장 긴 준비 봉. 아무 지표도 안 쓰면 2 입니다.

    지표마다 **적힌 매개변수로** 셉니다(`rules.warmup_of`). 매개변수가 생기기 전의
    이름(`ema20` 등)은 같은 기간으로 옮겨 세므로 그 이름으로 셀 때와 같은 수가 나옵니다.
    """
    wanted = {2}
    for name, params in rules.wanted_of(rule_set):
        wanted.add(rules.warmup_of(name, params))
    return max(wanted)


def _snapshot_ids(rule_set: dict[str, Any]) -> tuple[str, ...]:
    """체결 줄에 실을 값의 이름. 규칙 넷(켜짐과 상관없이 단계 전부)이 쓰는
    피연산자와 종가입니다. 차례는 `rules.OPERAND_IDS` 를 따릅니다.

    **발사한 규칙의 것만이 아닙니다** — 손절처럼 규칙 없이 나간 청산도 같은
    칸을 가져야, 체결마다 표의 모양이 바뀌지 않습니다. 수로 적은 오른쪽은
    값이 아니라 빼고, 값을 견준 오른쪽(교차의 선 등)은 넣습니다.

    이름은 **값 열의 이름**(`rules.operand_key`)입니다 — 매개변수가 있으면
    「ema(20)」 꼴이고, 매개변수가 생기기 전의 이름(`ema20`)으로 적은 전략도 이
    꼴로 싣습니다. 앞서 쌓인 체결 줄의 옛 이름은 그대로 남습니다.
    """
    wanted: dict[str, str] = {"close": "close"}
    for rule in rule_set.values() if isinstance(rule_set, dict) else []:
        if not isinstance(rule, dict):
            continue
        for step in rule.get("steps") or []:
            if not isinstance(step, dict):
                continue
            for cond in step.get("conds") or []:
                if not isinstance(cond, dict):
                    continue
                sides = ["left"]
                if str(cond.get("rightKind") or "") != "number":
                    sides.append("right")
                for side in sides:
                    raw = str(cond.get(side) or "")
                    name, params = rules.resolve(raw, cond.get(f"{side}Params"))
                    if name in rules.OPERAND_IDS:
                        # 차례는 **적힌 이름**의 자리입니다 — 옛 이름으로 적은 전략도
                        # 칸 차례가 바뀌지 않습니다.
                        wanted.setdefault(
                            rules.operand_key(name, params),
                            raw if raw in rules.OPERAND_IDS else name,
                        )
    order = {name: at for at, name in enumerate(rules.OPERAND_IDS)}
    return tuple(sorted(wanted, key=lambda key: (order[wanted[key]], key)))


#: 실행 줄에 싣는 1호가 칸과 그 시각(`RunEngine.quote_now`).
QUOTE_FIELDS = ("bid1", "ask1", "bid1Qty", "ask1Qty", "quoteAt")


def _best_of(book: dict[str, Any]) -> dict[str, float | None]:
    """호가 한 벌의 1호가와 잔량. 종이 장부가 호가를 읽는 규칙(`paper._levels`)과
    같은 함수로 읽어, 매수·매도의 가까운 쪽이 뒤바뀌지 않게 합니다. 한쪽이
    비었으면 그쪽 값은 `None` 입니다."""
    asks = paper._levels(book.get("asks"), nearest_last=True)
    bids = paper._levels(book.get("bids"), nearest_last=False)
    return {
        "bid1": bids[0][0] if bids else None,
        "ask1": asks[0][0] if asks else None,
        "bid1Qty": float(bids[0][1]) if bids else None,
        "ask1Qty": float(asks[0][1]) if asks else None,
    }


#: 기록에 실을 수 있는 값. NaN·무한대는 `None` 입니다 — JSON 에 담기지 않습니다.
#: 조건 표와 같은 자리(`rules._finite`)를 씁니다.
_finite = rules._finite


def _kst_day(at: int) -> str:
    """그 순간(밀리초)의 KST 날짜 글자(`YYYY-MM-DD`)."""
    return datetime.fromtimestamp(int(at) / 1000, tz=KST).date().isoformat()


def _reached(last: float, line: float, way: int, tick: float) -> bool:
    """마지막 체결가가 선에 닿았거나 넘었는지. `way` 가 +1 이면 위로, −1 이면 아래로.

    선은 틱을 곱하고 빼서 나온 값이라 0.05 같은 틱에서는 격자 위의 값도 끝자리가
    어긋납니다. 틱의 아주 작은 몫만큼은 닿은 것으로 봅니다 — 안 그러면 선과 같은
    값의 체결이 와도 청산이 안 나갑니다.
    """
    slack = abs(tick) * 1e-9
    return last >= line - slack if way > 0 else last <= line + slack


def _blank(value: Any) -> bool:
    """칸이 비었는지. 없는 칸과 빈 글자는 같은 뜻입니다 — 그 기능을 안 씁니다.

    전략 검사(`validate._blank`)와 같은 기준입니다.
    """
    return value is None or str(value).strip() == ""


def _int(value: Any) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    """수로 읽히는 값. 비었거나 수가 아니거나 유한하지 않으면 `None` 입니다."""
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clock(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp / 1000, tz=KST).strftime("%H:%M:%S")
