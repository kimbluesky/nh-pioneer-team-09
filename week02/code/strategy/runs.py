"""검증 실행 — 상태 기계·가상 장부·안전 검사.

**실행 하나 = 버전 하나를 종목·세션에 붙여 돌리는 것**입니다. 같은 버전을 두
종목에 붙이면 실행이 둘이고 장부도 둘입니다.

## 상태가 여덟인 까닭

「돌고 있다·멈췄다」 둘로는 **주문이 나가 있는 동안**을 말할 수 없습니다. 그
사이에 또 신호가 서면 같은 자리에 두 번 들어가게 되므로, 주문이 나가 있는
상태(`entering`·`exiting`)를 따로 둡니다. 나머지 넷은 사람이 손을 대야 하는
자리(`paused`·`attention`)와 시작·끝(`reconciling`·`ended`)입니다.

전이는 표(`TRANSITIONS`)로 두고 코드가 그 표만 봅니다. 곳곳에 `if` 로 흩으면
「어디서 이 상태가 되나」를 읽어서 찾아야 하고, 빠진 자리가 생겨도 드러나지
않습니다.

## 실행에는 갈래가 셋입니다

`paper` 는 **종이 장부**로만 돕니다 — 주문도 체결도 앱이 지어내고 계좌에 아무
것도 보내지 않습니다. `sim` 은 **모의 계좌로**, `real` 은 **실전 계좌로** 실제
주문을 냅니다. 둘을 묶어 계좌 갈래(`ACCOUNT_MODES`)라고 부릅니다.

엔진은 사람이 단추를 누르지 않아도 주문을 내는 자리라, 계좌 갈래에는 문이
여럿 있습니다.

    세울 때       갈래가 뜻하는 환경 · 화면이 믿는 환경 · 서버의 활성 환경이
                  셋 다 같아야 섭니다(`service.py`). 선 환경은 실행에 적히고
                  바뀌지 않습니다(`Run.environment`).
    보내기 전     환경 전환 중 · 활성 환경을 못 읽음 · 실행의 환경과 활성
                  환경이 다름 · 승인 없음 — 넷 중 하나라도면 한 건도 안
                  보냅니다(`sim.py`). 일반 주문과 같은 판정입니다.
    보낼 때       주문·취소·계좌 조회에 **실행의 환경을 적어** 부릅니다. 검사와
                  전송 사이에 활성 환경이 바뀌어도 다른 계좌로 새지 않습니다.
    다시 켤 때    계좌 갈래 실행은 대사로 서고 대사 뒤 끝납니다(`_restored`).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.nh_api.session import current_mode
from backend.storage import BROKEN, ORIGIN, ReadState, from_other_app, read_json, write_json
from backend.strategy.paper import BUY, SELL
from backend.strategy.store import run_state_dir
from backend.strategy.validate import EVAL_MODE, EVAL_MODES

logger = logging.getLogger(__name__)

#: 실행이 도는 세 갈래.
#:
#: `paper` 는 종이 장부로만 채우고 계좌에 아무것도 보내지 않습니다. `sim` 은
#: **모의 계좌**로, `real` 은 **실전 계좌**로 실제 주문을 냅니다. 모의와 실전을
#: 한 값으로 두고 환경에 따라 읽지 않는 것은, 기록과 성적이 섞이지 않게 하려는
#: 것입니다 — 한 실행의 장부는 한 계좌의 것입니다.
PAPER = "paper"
SIM = "sim"
REAL = "real"
MODES = (PAPER, SIM, REAL)

#: 계좌로 실제 주문을 내는 갈래. 종이와 갈리는 자리는 모두 이 묶음으로 봅니다.
ACCOUNT_MODES = (SIM, REAL)

#: 계좌 갈래가 도는 활성 환경. 이 환경이 아니면 세우지도 보내지도 않습니다.
ENVIRONMENT_OF: dict[str, str] = {SIM: "paper", REAL: "real"}


def account_modes(active: str | None) -> list[str]:
    """그 활성 환경에서 세울 수 있는 계좌 갈래. 환경을 모르면 빈 목록입니다."""
    return [mode for mode, environment in ENVIRONMENT_OF.items() if environment == active]

# ── 세션 ────────────────────────────────────────────────

#: 화면이 쓰는 문구 → **허브 맥락 키**.
#:
#: 실행의 `session` 은 **키**입니다. 허브에 구독을 걸 때 그대로 쓰는 값이라
#: 문구(「국내 주간」)를 담아 두면 구독을 세우는 자리에서 터집니다. 문구는
#: 화면이 읽을 것이라 `sessionLabel` 로 따로 냅니다.
#:
#: 해외 키가 `overseas-cme` 가 아니라 **`overseas`** 입니다 — 허브의 맥락
#: 목록(`realtime/feeds.CONTEXTS`)이 그렇게 셋입니다.
SESSION_KEYS: dict[str, str] = {
    "국내 주간": "domestic-day",
    "국내 야간": "domestic-night",
    "해외 CME": "overseas",
}

#: 키 → 문구. 화면에 적을 말입니다.
SESSION_LABELS: dict[str, str] = {key: label for label, key in SESSION_KEYS.items()}

#: 시장마다 쓸 수 있는 창.
MARKET_SESSIONS: dict[str, tuple[str, ...]] = {
    "domestic": ("domestic-day", "domestic-night"),
    "overseas": ("overseas",),
}


def session_key(value: Any) -> str | None:
    """키든 문구든 **허브 맥락 키**로. 어느 쪽도 아니면 `None` 입니다.

    화면은 설정에 문구를 담고 실행을 세울 때는 키를 보내는데, 둘 다 받아
    한 가지로 맞춰 둡니다 — 어느 쪽이 올지로 갈리면 그 자리가 곧 결함입니다.
    """
    body = str(value or "").strip()
    if body in SESSION_LABELS:
        return body
    return SESSION_KEYS.get(body)

# ── 상태 여덟 ────────────────────────────────────────────

ARMED = "armed"
ENTERING = "entering"
HOLDING = "holding"
EXITING = "exiting"
PAUSED = "paused"
ATTENTION = "attention"
RECONCILING = "reconciling"
ENDED = "ended"

#: 화면이 적는 말. 상태 이름을 화면마다 옮기지 않게 여기서 한 번만 답니다.
STATE_LABEL: dict[str, str] = {
    ARMED: "감시 중",
    ENTERING: "진입 주문 중",
    HOLDING: "보유 중",
    EXITING: "청산 요청",
    PAUSED: "중지",
    ATTENTION: "확인 필요",
    RECONCILING: "대사 중",
    ENDED: "종료",
}

#: 봉 마감에 **진입 규칙을 판정하는** 상태.
ENTRY_STATES = frozenset({ARMED, HOLDING})

#: 봉 마감에 **청산 규칙을 판정하는** 상태. 중지해도 들고 있는 것은 닫을 수 있어야 합니다.
EXIT_STATES = frozenset({ENTERING, HOLDING, PAUSED})

#: 손절·익절을 **틱마다 보는** 상태. 청산 주문이 나가 있으면 그 주문이 곧 감시입니다.
WATCH_STATES = frozenset({HOLDING, PAUSED})

#: **어떤 요청도 내지 않는 상태**. 확인이 필요한 실행에는 사용자가
#: 「확인」을 누를 때까지, 대사 중인 실행에는 계좌와 맞춰 볼 때까지 주문도
#: 취소도 나가지 않습니다 — 계좌와 장부가 맞는지 모르는 채로 보내는 요청은
#: 어긋난 자리를 하나 더 만듭니다.
QUIET_STATES = frozenset({ATTENTION, RECONCILING})

# ── 사건 ────────────────────────────────────────────────

STARTED = "started"
RECONCILE = "reconcile"
RECONCILED = "reconciled"
ENTRY_SENT = "entry-sent"
ENTRY_FILLED = "entry-filled"
ENTRY_PARTIAL = "entry-partial"
ENTRY_CANCELLED = "entry-cancelled"
EXIT_SENT = "exit-sent"
EXIT_FILLED = "exit-filled"
EXIT_PARTIAL = "exit-partial"
EXIT_CANCELLED = "exit-cancelled"
PAUSE = "pause"
RESUME = "resume"
END = "end"
FLAG = "flag"
ACKNOWLEDGE = "acknowledge"

#: 전이표 — `(에서, 사건) → 으로`. 표에 없는 짝은 상태를 바꾸지 않습니다.
#:
#: `exiting` 이 전량 체결로 빠져나가는 자리는 표에 두 줄입니다. 중지 중에 청산이
#: 끝나면 **중지로 돌아가야** 합니다 — 사용자가 멈춰 둔 것을 청산 한 번으로
#: 되살리면, 안 보는 사이에 다시 진입합니다.
TRANSITIONS: dict[tuple[str, str], str] = {
    (RECONCILING, RECONCILED): PAUSED,
    (RECONCILING, STARTED): ARMED,
    (RECONCILING, FLAG): ATTENTION,
    (RECONCILING, RECONCILE): RECONCILING,
    # 앱을 껐다 켜서 되살린 계좌 갈래 실행이 대사를 지나는 자리입니다. **맞든
    # 어긋나든 끝납니다** — 까닭은 `_restored` 에.
    (RECONCILING, END): ENDED,
    # 대사에 드는 자리들. **판정도 주문도 멈춥니다** — 계좌와 장부가 맞는지
    # 아직 모르는 동안 새 주문을 내면 맞춰 볼 대상이 늘어납니다.
    (ARMED, RECONCILE): RECONCILING,
    (HOLDING, RECONCILE): RECONCILING,
    (ENTERING, RECONCILE): RECONCILING,
    (EXITING, RECONCILE): RECONCILING,
    (PAUSED, RECONCILE): RECONCILING,
    (ARMED, ENTRY_SENT): ENTERING,
    (ENTERING, ENTRY_FILLED): HOLDING,
    (ENTERING, ENTRY_PARTIAL): HOLDING,
    (ENTERING, ENTRY_CANCELLED): ARMED,
    (ENTERING, EXIT_SENT): EXITING,
    (HOLDING, ENTRY_SENT): ENTERING,
    (HOLDING, EXIT_SENT): EXITING,
    (EXITING, EXIT_FILLED): ARMED,
    (EXITING, EXIT_PARTIAL): EXITING,
    (EXITING, EXIT_CANCELLED): HOLDING,
    (ARMED, PAUSE): PAUSED,
    (HOLDING, PAUSE): PAUSED,
    (ENTERING, PAUSE): PAUSED,
    # 청산 주문이 나가 있는 동안의 중지는 **상태를 바꾸지 않습니다.** 나가 있는
    # 청산을 거두면 들고 있는 것을 감시할 주문도 사라집니다. 중지는 기억해
    # 두었다가(`pause_requested`) 청산이 끝난 뒤에 듭니다.
    (EXITING, PAUSE): EXITING,
    (PAUSED, EXIT_SENT): EXITING,
    (PAUSED, RESUME): ARMED,
    (PAUSED, END): ENDED,
    (ARMED, END): ENDED,
    (ATTENTION, ACKNOWLEDGE): PAUSED,
    # 「확인 필요」에서 바로 끝내는 자리입니다. **이 실행의 장부에 보유도 열린
    # 주문도 없을 때만** 지나갑니다(`service.end`) — 끝내기는 계좌에 아무 요청도
    # 보내지 않으므로, 이 실행 몫으로 계좌에 남는 것이 없으면 어긋남이 풀리지
    # 않아도 끝낼 수 있습니다.
    (ATTENTION, END): ENDED,
}

#: 어느 상태에서든 갈 수 있는 자리. 응답 유실·귀속 불일치입니다.
ANY_STATE_EVENTS: dict[str, str] = {FLAG: ATTENTION}

#: 재접속 뒤 대사가 맞으면 돌아갈 수 있는 상태 — 대사에 드는 자리들입니다.
KEEP_STATES = (ARMED, HOLDING, ENTERING, EXITING, PAUSED)

#: 지울 수 있는 상태. 그 밖은 막고 까닭을 냅니다.
DELETABLE = (ENDED,)

# ── 안전 세트 ─────────────────────────────────────────────

#: 실행 하나가 거래일에 낼 수 있는 의도 수. 막힌 것은 세지 않습니다.
DAILY_INTENTS = 20

#: 동시에 돌 수 있는 실행 수. 구독이 실행마다 둘(시세·호가)이라 자리가 정해집니다.
MAX_RUNS = 7

#: 실행 하나가 잡는 구독 수.
FEEDS_PER_RUN = 2

#: 시세가 이만큼 조용하면 공백으로 봅니다(초).
GAP_SECONDS = 30

# 막힌 까닭. 실행 사건은 「조건이 참이었지만 주문을 내지 않았습니다 — 」 뒤에 이
# 글을 그대로 붙이고, 코드(`engine.BLOCK_REASONS`)를 따로 싣습니다.
WHY_DAILY = "하루 주문 건수를 다 썼습니다"
WHY_MAX_HOLD = "최대 보유를 넘습니다"
WHY_BUDGET = "증거금 예산을 넘습니다"
WHY_MARGIN_UNKNOWN = "계약당 증거금을 몰라 한도를 확인하지 못했습니다"
WHY_GAP = "시세가 끊겨 있습니다"
WHY_EXPIRY = "만기일이라 신규 진입을 막습니다"
WHY_STATE = "지금 상태에서는 진입하지 않습니다"
WHY_OPPOSITE = "보유와 반대 방향이라 진입하지 않습니다"
WHY_ADD = "보유 중에는 추가 진입을 하지 않습니다"
WHY_DAILY_LOSS = "하루 손실 한도에 닿았습니다"
WHY_LOSS_STREAK = "연속 손실 한도에 닿았습니다"
WHY_SESSION_END = "세션 끝 정리 시간이라 새로 들어가지 않습니다"
WHY_ENTRY_WINDOW = "신규 진입 시간이 아닙니다"
WHY_REENTRY_WAIT = "청산 뒤 대기 중이라 다시 들어가지 않습니다"
WHY_DAILY_ENTRIES = "하루 최대 진입 횟수를 다 썼습니다"
WHY_ATR_NOT_READY = "ATR 이 아직 없어 손절을 걸 수 없어 진입하지 않습니다"
WHY_RUNS = f"실행은 {MAX_RUNS}개까지입니다(실시간 시세 한도)"

#: 승인 없이 주문을 내려 했을 때. `attention` 으로 갑니다.
WHY_NOT_APPROVED = "승인 없이 주문을 낼 수 없습니다"

#: 계좌 갈래가 아닌 실행이 계좌로 주문을 내려 할 때.
WHY_NOT_ACCOUNT_MODE = "계좌로 주문을 내는 실행이 아닙니다"


def why_environment(run_environment: str, active: str) -> str:
    """실행의 환경과 서버의 활성 환경이 달라 주문을 안 낼 때의 문구."""
    from backend.config import ENVIRONMENT_LABELS

    return (
        f"실행의 환경({ENVIRONMENT_LABELS.get(run_environment, run_environment or '모름')})과 "
        f"서버의 활성 환경({ENVIRONMENT_LABELS.get(active, active)})이 달라 주문을 내지 않았습니다"
    )


#: 옵션. 계약당 드는 돈이 선물과 다릅니다 — `per_contract` 에.
OPTION = "option"

#: 옵션 매도의 증거금을 모른다고 할 때 덧붙이는 말.
WHY_SOLD_OPTION = "옵션 매도 증거금은 미리 알 수 없습니다"

#: 해외 실행의 시장 값.
OVERSEAS = "overseas"


#: 증거금 예산 검사를 건너뛰는 실행·백테스트에 붙이는 말. 실행 사건과 백테스트 가정이 같은 말입니다.
WHY_BUDGET_UNCHECKED = "계약당 증거금을 몰라 증거금 예산 검사를 하지 않습니다"


def budget_unchecked(run: "Run") -> bool:
    """증거금 예산 검사를 **건너뛰는** 실행인지.

    NH선물의 해외 조회에는 계약당 증거금 칸이 없어, 해외 선물·스프레드는 계약당
    값을 알 길이 없습니다. 모른다고 진입을 막으면 예산을 둔 해외 실행은 한 번도
    들어가지 못하므로, **예산 검사만 건너뛰고** 나머지 검사는 그대로 합니다.

    판정은 그때그때 계약당 값을 보고 합니다 — 값이 생기면 곧바로 검사로
    돌아갑니다. 옵션은 여기 들지 않습니다: 산 옵션은 프리미엄으로 셀 수 있고,
    판 옵션은 모르는 채로 막습니다. 국내는 품목별세부정보에 칸이 있어, 값이
    없으면 표를 아직 못 받은 것이라 막습니다.
    """
    return run.market == OVERSEAS and run.kind != OPTION and run.margin is None


def budget_skipped(run: "Run", side: str) -> bool:
    """그 방향의 진입에서 증거금 예산 검사를 건너뛰는지. 실행과 백테스트가 함께 씁니다.

    `budget_unchecked`(해외 선물·스프레드, 계약당 증거금 모름)에 더해 **해외 옵션
    매도**도 건너뜁니다 — 계약당 증거금을 거래소가 셈해 알 길이 없습니다. 국내 옵션
    매도는 건너뛰지 않고 모르는 채로 막습니다.
    """
    return budget_unchecked(run) or (run.market == OVERSEAS and run.kind == OPTION and side == SELL)


def margin_used(
    ledger: "Ledger", open_entry: int, each: float | None, *, adding: int = 0
) -> float | None:
    """묶이는 돈 — (보유 + 열린 진입 주문의 남은 수량 + `adding`) × 계약당 값.

    진입 검사는 낼 주문의 수량까지 넣어(`adding`) 예산과 견주고, 실행 줄은 지금
    묶인 돈(`adding` 없이)을 냅니다. 두 자리가 이 함수 하나로 셉니다. 계약당
    값(`each`, `per_contract`)을 모르면 `None` 입니다.
    """
    if each is None:
        return None
    return (ledger.position.size + max(int(open_entry), 0) + max(int(adding), 0)) * each


def per_contract(run: "Run", side: str, signal: float | None) -> float | None:
    """계약 하나에 드는 돈. **모르면 `None` 입니다.**

    셋이 갈립니다.

        선물·스프레드   품목별세부정보의 **계약당 개시증거금**
        옵션 매수       **프리미엄 × 승수** — 실제로 나가는 돈이 그것입니다
        옵션 매도       **모릅니다.** 거래소가 계산하는 값이라 앱이 셀 수 없습니다

    옵션 매수에 증거금을 쓰지 않는 것은, 산 옵션에는 증거금이 걸리지 않고
    프리미엄을 치르는 것이 전부이기 때문입니다. 그 자리에 선물 증거금을 넣으면
    예산이 실제보다 수십 배 크게 잡힙니다.
    """
    if run.kind != OPTION:
        return run.margin
    if side == SELL:
        return None
    if signal is None or signal <= 0:
        return None
    return signal * (run.multiplier or 0.0) or None


def next_state(state: str, event: str, *, pause_requested: bool = False) -> str:
    """그 상태에서 그 사건이 나면 어느 상태가 되는지. 표에 없으면 그대로입니다.

    `pause_requested` 는 **중지를 눌러 둔 채 청산이 끝난** 경우를 가립니다.
    """
    if event in ANY_STATE_EVENTS:
        return ANY_STATE_EVENTS[event]
    made = TRANSITIONS.get((state, event))
    if made is None:
        return state
    if made == ARMED and pause_requested:
        return PAUSED
    return made


@dataclass
class Position:
    """가상 보유 하나. 수량의 부호가 방향입니다 — 양수 매수, 음수 매도."""

    quantity: int = 0
    avg_price: float = 0.0

    @property
    def side(self) -> str | None:
        if self.quantity > 0:
            return BUY
        if self.quantity < 0:
            return SELL
        return None

    @property
    def size(self) -> int:
        return abs(self.quantity)


@dataclass
class Ledger:
    """한 실행의 가상 장부.

    **포지션을 따로 저장하지 않고 체결에서 셉니다** — 같은 방향은 더하고, 반대
    방향은 덜어 내며 남는 수량은 평균가를 물려받습니다. 저장한 포지션과 체결
    목록이 어긋나는 날 어느 쪽이 맞는지 고를 수 없게 되는 것을 막습니다.

    원가 배분은 **평균가**뿐입니다. FIFO 는 쓰지 않습니다.
    """

    multiplier: float = 1.0
    fee_per_contract: float = 0.0
    position: Position = field(default_factory=Position)
    realized: float = 0.0
    fee: float = 0.0
    trades: list[dict[str, Any]] = field(default_factory=list)
    #: 아직 안 닫힌 거래. 포지션이 0 으로 돌아올 때 `trades` 로 옮깁니다.
    _open: dict[str, Any] | None = None

    def add(self, fill: dict[str, Any]) -> dict[str, Any] | None:
        """체결 하나를 넣습니다. **거래 하나가 닫히면** 그 거래를 냅니다.

        거래는 **포지션 0 에서 시작해 다시 0 이 될 때까지 한 덩어리**입니다.
        나눠 들어가고 나눠 나온 것을 한 줄로 셉니다 — 마지막 청산
        한 조각만 적으면 승률과 평균 손익이 그 조각의 것이 됩니다.
        """
        quantity = int(fill.get("quantity") or 0)
        price = float(fill.get("price") or 0.0)
        if quantity <= 0:
            return None
        signed = quantity if fill.get("side") == BUY else -quantity
        at = int(fill.get("time") or 0)
        cost = self.cost_of(quantity)
        self.fee += cost

        held = self.position
        if held.quantity == 0:
            self._start(signed, price, at, cost)
            return None

        if (held.quantity > 0) == (signed > 0):
            # 같은 방향입니다. 평균가를 다시 세고 거래에 더 답니다.
            total = held.size + quantity
            avg = (held.avg_price * held.size + price * quantity) / total
            self.position = Position(quantity=held.quantity + signed, avg_price=avg)
            self._grow("entry", quantity, price, cost)
            return None

        closed = min(held.size, quantity)
        direction = 1 if held.quantity > 0 else -1
        self.realized += (price - held.avg_price) * closed * self.multiplier * direction
        self._grow("exit", closed, price, cost)
        left = held.quantity + signed

        if left != 0 and (left > 0) == (held.quantity > 0):
            # 아직 들고 있습니다. 평균가는 남은 것의 것 그대로입니다.
            self.position = Position(quantity=left, avg_price=held.avg_price)
            return None

        made = self._finish(at)
        if left == 0:
            self.position = Position()
            return made
        # 반대로 넘어갔습니다. 남는 수량으로 새 거래를 엽니다.
        self._start(left, price, at, 0.0)
        return made

    def cost_of(self, quantity: Any) -> float:
        """체결 한 건의 수수료. 계약 수 × 계약당 수수료입니다.

        **이 값을 셈하는 자리는 여기 하나입니다.** 장부의 누계와 거래 합계,
        체결 줄의 `fee` 가 모두 이것을 씁니다 — 두 자리에서 따로 셈하면 둘이
        갈리는 날 어느 쪽이 맞는지 가릴 수 없습니다.

        보유를 넘겨 반대 방향으로 뒤집는 체결은 **그 체결의 수수료 전부가 닫히는
        거래**에 들어가고, 새로 열린 거래는 수수료 0 에서 시작합니다. 체결 줄에는
        그 체결의 전체 값이 실리므로, 그 경우 새 거래 하나만 놓고 보면 체결
        수수료의 합과 거래 수수료가 다릅니다. 실행 누계는 늘 체결 합과 같습니다.
        """
        return max(int(quantity or 0), 0) * self.fee_per_contract

    def _start(self, signed: int, price: float, at: int, cost: float) -> None:
        self.position = Position(quantity=signed, avg_price=price)
        self._open = {
            "openedAt": at,
            "side": BUY if signed > 0 else SELL,
            "entryQty": abs(signed),
            "entryValue": abs(signed) * price,
            "exitQty": 0,
            "exitValue": 0.0,
            "fee": cost,
        }

    def _grow(self, which: str, quantity: int, price: float, cost: float) -> None:
        held = self._open
        if held is None:
            return
        held[f"{which}Qty"] += quantity
        held[f"{which}Value"] += quantity * price
        held["fee"] += cost

    def _finish(self, at: int) -> dict[str, Any] | None:
        held = self._open
        self._open = None
        if held is None or not held["exitQty"]:
            return None
        avg_entry = held["entryValue"] / held["entryQty"]
        avg_exit = held["exitValue"] / held["exitQty"]
        direction = 1 if held["side"] == BUY else -1
        gross = (avg_exit - avg_entry) * held["exitQty"] * self.multiplier * direction
        made = {
            "openedAt": held["openedAt"],
            "closedAt": at,
            "side": held["side"],
            "quantity": held["exitQty"],
            "avgEntry": avg_entry,
            "avgExit": avg_exit,
            "grossPl": gross,
            "fee": held["fee"],
            "netPl": gross - held["fee"],
        }
        self.trades.append(made)
        return made

    def unrealized(self, last_price: float | None) -> float:
        """평가손익. 마지막 체결가로 셉니다. 값을 모르면 0 입니다."""
        held = self.position
        if last_price is None or held.quantity == 0:
            return 0.0
        direction = 1 if held.quantity > 0 else -1
        return (last_price - held.avg_price) * held.size * self.multiplier * direction

    def profit(self, last_price: float | None) -> float:
        """실현 + 평가 − 수수료. 손실 한도가 보는 값입니다."""
        return self.realized + self.unrealized(last_price) - self.fee


@dataclass
class Run:
    """검증 실행 하나. 시작한 뒤에는 설정이 바뀌지 않습니다."""

    id: str
    version_id: str
    code: str
    session: str
    market: str
    #: 해외 거래소 이름(`FCME` 같은 것). 해외 조회가 **반드시** 받는 값이라
    #: 시작할 때 종목 마스터에서 떠 담아 둡니다. 국내는 빈 값입니다.
    exchange: str = ""
    #: 버전을 묶는 전략 id(전략 저장소의 `groupId`). 기록 줄에 함께 적어
    #: 버전이 지워진 뒤에도 그 기록을 전략으로 묶습니다. 모르면 빈 글자입니다 —
    #: 이 칸이 없던 때 담아 둔 실행도 빈 글자로 읽힙니다.
    group_id: str = ""
    #: 사용자가 이 실행의 주문을 승인한 순간(밀리초). **계좌 갈래는 이것이 없으면
    #: 한 건도 안 냅니다** — 승인 없이 내려 하면 `attention` 으로 갑니다.
    approved_at: int = 0
    #: 이 실행의 체결 모형(`paper.MODEL`·`sim.MODEL`). 모형이 바뀐 뒤 되살린 종이
    #: 실행은 새 모형으로 고쳐 담깁니다(`service.load`).
    model: str = ""
    #: 품목의 통화(`KRW`·`USD`·`HKD`…). 손익·손실 한도가 이 통화입니다. 이 칸이
    #: 없던 때의 실행은 시장으로 읽습니다(`currency_of`).
    currency: str = ""
    #: 세울 때 품목의 통화를 찾지 못한 해외 실행인지. 그런 실행의 손익은 어느 통화의
    #: 계좌 손실 한도 합계에도 넣지 않습니다(달러로 더하면 다른 통화 손익이 섞입니다).
    #: 이 칸이 없는 해외 실행 줄은 거짓으로 읽혀 달러로 셉니다.
    currency_unknown: bool = False
    #: 계좌 갈래가 선 환경(`paper`·`real`). **세운 뒤 바뀌지 않습니다** — 주문·취소·
    #: 계좌 조회가 모두 이 값으로 나갑니다. 종이 실행은 빈 값입니다.
    environment: str = ""
    #: 선물·옵션·스프레드. **계약당 얼마가 드는지가 갈립니다** — `per_contract` 에.
    kind: str = ""
    #: 기준 봉·규칙·보조 설정. 시작 순간의 설정을 그대로 물고 갑니다.
    draft: dict[str, Any] = field(default_factory=dict)
    mode: str = PAPER
    qty: int = 1
    cash: float = 0.0
    fee_per_contract: float = 0.0
    #: 계약 승수(1포인트가 몇 통화인지). 모르면 `None` 이고 손익을 돈으로 세지 않습니다.
    multiplier: float | None = 1.0
    margin: float | None = None
    margin_budget: float | None = None
    daily_loss: float | None = None
    state: str = RECONCILING
    #: 앱을 껐다 켠 뒤 **파일에서 되살린** 실행인지. 파일에는 안 담습니다 —
    #: 앱이 뜰 때마다 새로 정해지는 값입니다(`RunStore.read`).
    restored: bool = False
    pause_requested: bool = False
    started_at: int = 0
    stopped_at: int = 0
    stop_reason: str = ""
    last_bar_at: int = 0
    #: 거래일마다 다시 세는 값들. 열쇠는 거래일 0시(밀리초)입니다.
    day_key: int = 0
    intents_today: int = 0
    #: 거래일이 시작할 때의 `실현 − 수수료`. 하루 손익을 여기서부터 셉니다.
    day_base: float = 0.0
    #: 거래일이 시작할 때 닫혀 있던 거래 수. 연속 손실을 그 뒤의 거래로만 셉니다.
    #:
    #: **파일에 담지 않습니다**(`_SAVED`). 되살린 실행은 장부가 빈 채로 서므로,
    #: 옛 값을 들고 오면 거래 수보다 큰 기준이 되어 연속 손실을 영영 못 셉니다.
    streak_base: int = 0
    #: 대사에 들기 직전의 상태. 재접속 뒤 대사가 맞으면 그 상태로 돌아갑니다
    #: (`keep_after_reconcile`). **파일에 담지 않습니다** — 앱을 다시 켜 되살린 실행은
    #: 이 길이 아니라 대사 뒤 끝납니다.
    before_reconcile: str = ""

    def move(self, event: str) -> str:
        """사건 하나로 상태를 옮기고 새 상태를 냅니다.

        **「재개」·「종료」는 중지 기억을 먼저 지웁니다.** 지우지 않고 옮기면
        중지 기억이 재개까지 되돌려 놓아, 눌러도 안 도는 단추가 됩니다.
        """
        if event in (RESUME, END):
            self.pause_requested = False
        made = next_state(self.state, event, pause_requested=self.pause_requested)
        if event == PAUSE:
            self.pause_requested = True
        if made == RECONCILING and self.state != RECONCILING:
            # 대사 중에 또 대사가 오면 덮지 않습니다 — 덮으면 「대사 중」으로 돌아가
            # 대사가 끝나지 않습니다.
            self.before_reconcile = self.state
        elif self.state == RECONCILING and made != RECONCILING:
            self.before_reconcile = ""
        self.state = made
        return made

    def keep_after_reconcile(self, *, held: bool, open_entry: bool, open_exit: bool) -> str:
        """재접속 뒤 대사가 맞았을 때. 대사에 들기 직전 상태로 돌아갑니다.

        **주문이 나가 있던 상태는 대사 도중의 체결·취소를 반영해 다시 정합니다** —
        평소 상태표와 같은 결과입니다. 진입 주문 중이었는데 보유가 생겼으면(부분
        체결 포함) `holding` 입니다. 안 그러면 보유가 있는데 「진입 주문 중」에 남아
        손절 감시가 돌지 않습니다. 보유 없이 진입 주문이 남았으면 그대로, 주문도
        없으면 `armed` 입니다. 청산 주문 중이었으면 그 주문이 남았을 때만 그대로이고,
        닫혔으면 보유가 있으면 `holding`, 없으면 `armed` 입니다. 중지를 눌러 두었으면
        `armed` 대신 `paused` 입니다(`next_state` 와 같은 규칙).

        대사 전 상태를 모르면(대사 중에 멈췄던 실행 등) 중지로 갑니다 — 판정을 다시
        열지는 사용자가 정합니다.
        """
        if self.state != RECONCILING:
            return self.state
        back = self.before_reconcile
        self.before_reconcile = ""
        if back not in KEEP_STATES:
            back = PAUSED
        elif back == ENTERING:
            back = HOLDING if held else (ENTERING if open_entry else ARMED)
        elif back == EXITING and not open_exit:
            back = HOLDING if held else ARMED
        if back == ARMED and self.pause_requested:
            back = PAUSED
        self.state = back
        return back

    def roll_day(self, day: int, ledger: "Ledger", last_price: float | None = None) -> None:
        """거래일이 바뀌었으면 하루치로 세는 것들을 다시 엽니다.

        **보유는 넘어갑니다** — 날이 바뀐다고 들고 있던 것이 사라지지 않습니다.
        다시 세는 것은 건수와 하루 손익, 연속 손실입니다.

        넘어온 보유의 **그 순간 평가손익까지 기준에 넣습니다.** 안 넣으면 어제
        벌어 둔 평가손익이 오늘 아침에 오늘 몫으로 잡혀, 아무것도 안 했는데
        하루 한도가 차거나 하루 성적이 부풀어 오릅니다.
        """
        if day == self.day_key:
            return
        self.day_key = day
        self.intents_today = 0
        self.day_base = ledger.realized - ledger.fee + ledger.unrealized(last_price)
        self.streak_base = len(ledger.trades)

    def count_intent(self) -> None:
        """의도 하나를 셉니다. 막힌 의도는 세지 않습니다.""" 
        self.intents_today += 1


def day_profit(run: Run, ledger: Ledger, last_price: float | None) -> float:
    """그 실행의 **거래일** 손익. 실현 + 평가 − 수수료입니다.

    실행을 시작한 뒤의 누적이 아니라 하루치입니다 — 한도는 날마다 다시 섭니다.
    """
    return (ledger.realized - ledger.fee) - run.day_base + ledger.unrealized(last_price)


def open_profit(ledger: Ledger, last_price: float | None) -> float | None:
    """보유분의 평가손익. 보유가 없으면 0, 보유가 있는데 마지막가를 모르면 `None` 입니다."""
    if ledger.position.quantity == 0:
        return 0.0
    if last_price is None:
        return None
    return ledger.unrealized(last_price)


def loss_streak(run: Run, ledger: Ledger) -> int:
    """그 거래일에 **끝에서부터 이어진 손실 거래 수**.

    손실은 수수료까지 뺀 손익(`netPl`)이 0 보다 작은 거래입니다. **손익이 딱
    0 인 거래는 연속을 끊습니다** — 잃지 않은 거래를 손실로 세면 본전으로 나온
    거래가 진입을 막는 쪽으로 셈해집니다.

    거래일이 바뀌면 다시 셉니다(`Run.roll_day`).
    """
    made = 0
    for trade in reversed(ledger.trades[max(run.streak_base, 0) :]):
        if float(trade.get("netPl") or 0.0) >= 0:
            break
        made += 1
    return made


def entry_block(
    run: Run,
    ledger: Ledger,
    *,
    side: str,
    open_entry: int,
    max_hold: int,
    add_while_open: bool,
    gap: bool,
    expiring: bool,
    last_price: float | None,
    signal: float | None = None,
    streak_limit: int = 0,
) -> str | None:
    """진입 의도를 막을 까닭. 막을 것이 없으면 `None` 입니다.

    **차례가 뜻을 가집니다.** 상태·공백·만기처럼 「지금은 아무것도 안 낸다」가
    먼저이고, 방향·수량처럼 「이 의도만 안 낸다」가 뒤입니다. 화면에 적히는 것은
    첫 까닭 하나라, 큰 것이 먼저 나와야 사용자가 무엇을 고칠지 압니다.
    """
    if run.state not in ENTRY_STATES:
        return WHY_STATE
    if gap:
        return WHY_GAP
    if expiring:
        return WHY_EXPIRY
    if run.intents_today >= DAILY_INTENTS:
        return WHY_DAILY
    # 승수를 모르는 실행은 손익을 돈으로 셀 수 없어 하루 손실 한도를 보지 않습니다.
    if (
        run.daily_loss is not None
        and run.multiplier is not None
        and day_profit(run, ledger, last_price) <= -run.daily_loss
    ):
        return WHY_DAILY_LOSS
    # 막는 것은 **새 진입뿐**입니다. 청산·손절·추적 청산은 이 검사를 거치지 않습니다.
    if streak_limit > 0 and loss_streak(run, ledger) >= streak_limit:
        return WHY_LOSS_STREAK

    holding = ledger.position
    if holding.quantity and holding.side != side:
        return WHY_OPPOSITE
    if holding.quantity and not add_while_open:
        return WHY_ADD
    if holding.size + open_entry + run.qty > max(max_hold, 0):
        return WHY_MAX_HOLD

    # 해외 선물·스프레드(계약당 증거금 모름)와 해외 옵션 매도는 예산 검사를 건너뜁니다
    # (`budget_skipped`). 화면이 그 근거(`perContractBasis`)를 보고 안내합니다.
    if run.margin_budget is not None and not budget_skipped(run, side):
        each = per_contract(run, side, signal if signal is not None else last_price)
        wanted = margin_used(ledger, open_entry, each, adding=run.qty)
        if wanted is None:
            return WHY_MARGIN_UNKNOWN
        if wanted > run.margin_budget:
            return WHY_BUDGET
    return None


#: 계약 승수를 모를 때의 말. 계좌 실행은 이 까닭으로 세우지 않습니다.
WHY_MULTIPLIER_UNKNOWN = "계약 승수를 몰라 손익을 셀 수 없습니다"

#: 통화 칸이 없는 실행 줄의 통화. 그런 줄의 해외 손익은 달러입니다.
LEGACY_CURRENCY: dict[str, str] = {"domestic": "KRW", "overseas": "USD"}


def currency_of(run: Run) -> str:
    """그 실행의 손익 통화. 실행에 적힌 것이 먼저이고, 없으면 시장으로 읽습니다."""
    return str(run.currency or "").strip() or LEGACY_CURRENCY.get(run.market, "")


def eval_mode_of(run: Run) -> str:
    """그 실행의 평가 방식. 설정에 없거나 모르는 값이면 「마감」입니다."""
    draft = run.draft if isinstance(run.draft, dict) else {}
    made = str(draft.get("evalMode") or "")
    return made if made in EVAL_MODES else EVAL_MODE


def row_of(
    run: Run,
    ledger: Ledger,
    *,
    feed: str,
    feed_at: int,
    last_price: float | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """화면이 검증 목록에 그리는 줄 하나.

    이름은 프론트가 이미 쓰는 것과 같습니다 — 백엔드가 다시 지으면 화면이
    옮겨 적는 자리가 한 겹 더 생깁니다.
    """
    held = ledger.position
    made: dict[str, Any] = {
        "id": run.id,
        "version": run.version_id,
        "code": run.code,
        "session": run.session,
        "sessionLabel": SESSION_LABELS.get(run.session, run.session),
        "market": run.market,
        "mode": run.mode,
        "approvedAt": run.approved_at or None,
        "state": run.state,
        "stateLabel": STATE_LABEL.get(run.state, run.state),
        "feed": feed,
        "feedAt": feed_at,
        "qty": run.qty,
        "cash": run.cash,
        "margin": run.margin,
        "position": held.size,
        "side": held.side,
        "avgPrice": held.avg_price if held.quantity else None,
        "pl": ledger.profit(last_price),
        # 손익을 실현과 평가로 나눈 것. 둘을 더하면 `pl` 입니다 — 낸 수수료는
        # 실현 쪽에 듭니다. 보유가 있는데 마지막가를 모르면 평가는 `None` 입니다.
        "realizedPl": ledger.realized - ledger.fee,
        "openPl": open_profit(ledger, last_price),
        "dayPl": day_profit(run, ledger, last_price),
        # 그 거래일에 이어진 손실 거래 수. 한도는 설정(`aux.lossStreak`)에 있습니다.
        "lossStreak": loss_streak(run, ledger),
        "fee": ledger.fee,
        # 계좌 갈래가 선 환경. 종이 실행은 `null` 입니다.
        "environment": run.environment or None,
        # 증거금 예산이 있는데 계약당 증거금을 몰라 그 검사를 하지 않는 실행인지.
        # 저장하지 않고 줄을 낼 때마다 셉니다(`budget_unchecked`).
        "budgetUnchecked": run.margin_budget is not None and budget_unchecked(run),
        # 계약 승수를 몰라 손익을 돈으로 못 세는 까닭. 알면 `None` 입니다 — 그때 손익
        # 칸(`pl`·`dayPl`·`realizedPl`·`openPl`)도 `None` 입니다(1 로 세지 않음).
        "multiplierWhy": WHY_MULTIPLIER_UNKNOWN if run.multiplier is None else None,
        # 손익·한도의 통화. 해외는 품목마다 다릅니다(달러·홍콩달러·유로…).
        "currency": currency_of(run),
        # 평가 방식. 봉 마감에 판정(「마감」)하는지 체결마다 진행 중인 봉까지
        # 판정(「틱」)하는지입니다. 설정에 없거나 모르는 값이면 「마감」입니다.
        "evalMode": eval_mode_of(run),
    }
    if run.multiplier is None:
        for name in ("pl", "dayPl", "realizedPl", "openPl"):
            made[name] = None
    made.update(extra)
    return made


# ── 저장 ────────────────────────────────────────────────

#: 실행 설정을 남기는 자리. 전략 파일과 같은 상태 폴더입니다.
#:
#: 규격은 `~/NHFutures/strategies/runs/<runId>/run.json` 을 적었는데, 전략
#: 파일이 이미 `state/` 에 있어 **같은 뿌리에 둡니다** — 폴더 둘로 나누면
#: 「전략 관련 파일이 어디 있나」가 두 자리가 됩니다.
#:
#: **뿌리는 재생이냐 아니냐로 갈립니다**(`store.run_state_dir`) — 저장해 둔
#: 응답으로 띄운 앱이 실서버에서 돌던 실행을 이어받지 않게 합니다.
RUNS_PATH = run_state_dir() / "strategy-runs.json"

#: 파일 형식. 담는 것이 달라지면 올립니다.
VERSION = 1

#: 한 파일에 담을 실행 수. 끝난 실행까지 쌓여도 파일이 끝없이 커지지 않게 합니다.
MAX_RECORDS = 200

#: 파일에 남기는 칸. **장부는 안 남깁니다** — 까닭은 `RunStore` 에.
_SAVED = (
    "id",
    "version_id",
    "group_id",
    "code",
    "session",
    "market",
    "exchange",
    "approved_at",
    "kind",
    "draft",
    "mode",
    "environment",
    "qty",
    "cash",
    "fee_per_contract",
    "model",
    "currency",
    "currency_unknown",
    "multiplier",
    "margin",
    "margin_budget",
    "daily_loss",
    "started_at",
    "stopped_at",
    "stop_reason",
    # **상태도 담습니다.** 안 담으면 끝낸 실행이 다시 켤 때 중지로 서고,
    # 사용자는 끝낸 것을 또 끝내야 합니다 — 되살아난 것처럼 보입니다.
    # 되살릴 때의 규칙은 `_restored()` 에 있습니다.
    "state",
)


def _restored(state: str, mode: str = PAPER) -> str:
    """담긴 상태로 다시 세울 때의 상태.

    앱을 껐다 켜면 시세도 장부도 이어지지 않으므로, 돌던 종이 실행은 어느
    상태였든 중지로 내려놓습니다 — 자동 재개는 없습니다. 끝난 실행은
    다릅니다. 더 할 것이 없어 이어 갈 것도 없고, 중지로 세우면 목록에서 「끝」이
    아니라 「멈춰 있음」으로 보입니다.

    **계좌 갈래(모의·실전) 실행은 대사로 서고, 대사를 지나면 끝납니다**
    (`service.reconcile`). 계좌로 실제 주문이 나가는 자리인데 그 승인(`approved_at`)이 파일에
    함께 남아 있어, 중지로 세우면 앱을 껐다 켠 것만으로 「재개」 한 번에 다시
    주문이 나갈 수 있습니다. 껐다 켠 뒤의 장부도 비어 있어 이어 갈 수 있는 값이
    아닙니다. 그래도 **계좌와 한 번은 맞춰 봅니다** — 꺼져 있던 동안 계좌에
    남은 것이 있으면 끝내기 전에 알려야 합니다. 그 전략을 계속 돌리려면 실행을
    새로 세우고 다시 승인하면 됩니다.

    **모드를 모르는 줄은 종이로 봅니다** — 모드 칸이 없는 줄이 그렇습니다.
    """
    if state == ENDED:
        return ENDED
    return RECONCILING if mode in ACCOUNT_MODES else PAUSED


class RunStore:
    """검증 실행 설정을 사용자 홈에 남기고 읽습니다.

    **설정만 남깁니다.** 봉·체결·이벤트·장부는 담지 않습니다 — 종이거래 장부는
    그 실행이 도는 동안의 것이고, 앱을 껐다 켜면 시세가 이어지지 않아 이어 볼
    수 있는 값이 아닙니다. 담아 두면 화면에는 성적이 남는데 그 성적이 어느
    구간의 것인지 말할 수 없게 됩니다.

    **돌던 종이 실행은 다시 읽으면 `paused` 입니다**. 자동 재개가 없는
    것은, 꺼져 있던 동안 손절 감시가 없었기 때문입니다 — 사용자가 지금 값을 보고
    이어 갈지 정해야 합니다. **계좌 갈래 실행은 대사로 서고 그 뒤 끝납니다** — 까닭은
    `_restored` 에.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else RUNS_PATH
        self._lock = threading.Lock()
        #: 마지막으로 읽었을 때 어땠는지. 화면이 「못 읽어 옆에 두었습니다」를
        #: 말할 수 있게 답에 실립니다(`GET /api/strategy/runs`).
        self.state = ReadState()

    def read(self) -> list[Run]:
        """담겨 있는 실행 전부. **계좌 갈래는 대사로, 나머지는 중지로 세웁니다**(`_restored`).

        **못 읽는 줄은 건너뜁니다.** 한 줄에 칸이 모자라도 나머지는 읽습니다 —
        그러지 않으면 줄 하나 때문에 실행 목록이 통째로 안 서고, 화면은 담아
        둔 실행이 전부 사라진 것으로 봅니다. 전략 파일(`store._current`)과
        기록 파일(`journal.read`)도 같은 결입니다. 닿는 길은 손으로 고친
        파일입니다 — 앱이 쓰다 멈춰도 앞 파일이 그대로 남습니다(`write_json`).

        **파일이 통째로 안 읽히면 그것도 남깁니다.** 그때는 실행이 전부
        사라지는데, `read_json` 이 예외를 안 내고 옆으로 치운 뒤 빈 내용으로
        답하므로 기록이 없으면 아무도 모릅니다 — 사용자는 담아 둔 실행이
        없어진 것만 봅니다.
        """
        with self._lock:
            held, status = read_json(self.path, state=self.state)
        if status == BROKEN:
            logger.warning(
                "검증 실행 파일을 읽지 못했습니다(%s): %s. "
                "담아 둔 실행이 목록에 서지 않습니다.",
                self.state.kept(),
                self.path,
            )
        if from_other_app(held, current_mode(), state=self.state):
            logger.warning(
                "검증 실행 파일을 다른 앱(%s)이 남겼습니다: %s. 담긴 실행을 세우지 "
                "않습니다 — 저장해 둔 응답으로 도는 앱과 실제 서버에 붙는 앱은 "
                "돌던 것을 나눠 담습니다.",
                self.state.other_app,
                self.path,
            )
            return []
        rows = held.get("runs") if isinstance(held, dict) else None
        made: list[Run] = []
        missed: list[str] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            kept = {key: row[key] for key in _SAVED if key in row}
            # 상태도 모드도 없는 줄은 종이 실행의 중지로 섭니다.
            kept["state"] = _restored(
                str(kept.get("state") or ""), str(kept.get("mode") or PAPER)
            )
            # 환경 칸이 없는 모의 실행은 모의 계좌에서 선 것으로 봅니다.
            if kept.get("mode") == SIM and not kept.get("environment"):
                kept["environment"] = ENVIRONMENT_OF[SIM]
            try:
                # 파일에서 온 줄이라는 표지입니다. 대사를 지나면 끝낼 실행인지
                # 여기서만 알 수 있습니다(`service.reconcile`).
                made.append(Run(**kept, restored=True))
            except TypeError:
                missed.append(str(row.get("id") or "이름 없는 줄"))
        if missed:
            logger.warning(
                "검증 실행 %d개를 읽지 못해 건너뜁니다 — 칸이 모자랍니다(%s). "
                "그 실행은 목록에 서지 않고, 나머지는 그대로 읽습니다.",
                len(missed),
                ", ".join(missed),
            )
        return made

    def save(self, rows: list[Run]) -> None:
        """실행 목록을 통째로 다시 씁니다. 끝난 것부터 넘치는 만큼 버립니다."""
        kept = [one for one in rows if one.state != ENDED] + [
            one for one in rows if one.state == ENDED
        ]
        with self._lock:
            write_json(
                self.path,
                {
                    "version": VERSION,
                    # 어느 앱이 썼는지. 다른 앱은 이 파일을 안 읽습니다(`storage.from_other_app`).
                    ORIGIN: current_mode(),
                    "runs": [
                        {key: getattr(one, key) for key in _SAVED} for one in kept[:MAX_RECORDS]
                    ],
                },
            )
        # 다시 담았으면 「못 읽었다」는 알림은 할 일을 다 한 것입니다.
        self.state.wrote()


#: 앱이 쓰는 저장소. 테스트는 제 것을 만들어 씁니다.
store = RunStore()
