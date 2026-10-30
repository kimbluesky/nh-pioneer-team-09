"""검증 실행을 만들고 세우고 끝냅니다.

라우트와 엔진 사이의 자리입니다. 라우트는 **무엇을 해 달라는지**만 넘기고,
여기서 **할 수 있는 일인지**(안전 세트)를 보고 실행 목록을 건사합니다.

## 시세는 여기서 물리지 않습니다

실행을 만들면 엔진이 서지만 **허브 구독을 여기서 걸지 않습니다.** 봉·체결을
넣어 주는 것은 펌프(`pump.RunPump`)이고, 여기서는 실행의 삶(추가·중지·재개·청산·종료)과
안전 검사를 맡습니다. 그래서 `feed` 는 만든 직후 `idle` 입니다 — 「살아 있는데
값이 안 온다」와 「아직 안 붙였다」가 화면에서 갈려야 합니다.

## 자리가 정해져 있습니다

실시간 구독은 스무 자리이고 실행 하나가 **시세·호가 둘**을 씁니다. 일반주문
화면 몫 여섯을 빼면 실행은 일곱까지입니다(`runs.MAX_RUNS`). 여덟째는 막고
까닭을 냅니다 — 자리가 모자라면 이미 돌던 실행의 시세가 끊깁니다.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from datetime import date, datetime
from typing import Any

from backend.master import master_service
from backend.master.master_index import days_left_of
from backend import market_values, order_outcome, quotes
from backend.config import ENVIRONMENT_LABELS
from backend.sessions import service as session_service
from backend.sessions.hours import KST, now_kst
from backend.domestic_futureoption import domestic_hours
from backend.domestic_futureoption.domestic_hours import DAY, NIGHT
from backend.strategy import bars as bar_lib
from backend.strategy import engine as engine_lib
from backend.strategy import journal as journal_lib
from backend.strategy import limits as limit_lib
from backend.strategy import rules as rule_lib
from backend.strategy import paper, runs, sim, validate
from backend.strategy.runs import Run

logger = logging.getLogger(__name__)

#: 시세를 아직 안 물린 실행. 「값이 안 온다」와 갈립니다.
IDLE = "idle"

#: 만기 당일. `days_left` 가 이 값이면 신규 진입을 막습니다(만기 당일이 1).
EXPIRY_DAY = 1

# 못 하는 요청에 하는 말.
WHY_NOT_FOUND = "그 실행을 찾지 못했습니다."
WHY_HOLDING = "보유가 남아 있어 끝낼 수 없습니다. 먼저 청산해 주세요."
WHY_NOT_ENDED = "끝난 실행만 지울 수 있습니다."
WHY_NOTHING = "닫을 보유가 없습니다."

# 시작할 수 없는 값에 하는 말. 저장할 때와 같은 규약으로 냅니다 — 화면이 한
# 갈래로 읽습니다(`validate.problems` 와 같은 `{field, severity, message}`).
WHY_QTY = "주문당 수량은 1 이상이고 최대 보유 이하로 적습니다"
#: 세션 끝 정리를 켰는데 그 종목의 장 시간표를 모를 때. 계좌 갈래는 이것으로 시작을
#: 막고, 종이는 세우되 실행에 한 줄 남깁니다. 앱을 막 켜 시간표를 아직 받지 못한
#: 동안도 이렇습니다.
WHY_SESSION_END_UNKNOWN = (
    "장 시간표를 아직 몰라 세션 끝 정리를 할 수 없습니다 — 시간표를 받은 뒤(잠시 뒤) 다시 시작해 주세요"
)
#: 실행 중에 같은 까닭으로 정리하지 못할 때 실행에 남기는 줄. 거래일마다 한 번입니다.
SESSION_END_UNKNOWN_NOTE = "세션 끝 시각을 몰라 세션 끝 정리를 하지 못합니다"
WHY_CASH = "종이 자금은 0보다 큰 수로 적습니다"
WHY_FEE = "수수료는 0 이상으로 적습니다"
WHY_CODE = "종목 목록에서 이 종목을 찾지 못했습니다"
WHY_NO_MULTIPLIER = "승수를 종목 마스터에서 셀 수 없습니다"
WHY_SESSION = "이 시장에 없는 세션입니다"
WHY_RUN_ID = "이 실행은 이미 세워져 있습니다"
WHY_MODE = "실행 갈래는 가상·모의 계좌·실전 계좌 가운데 하나입니다"
#: 계좌 실행을 열지 않은 거래소. 그 품목은 앱 시뮬레이션으로만 돌립니다.
CLOSED_EXCHANGES = frozenset({"ICE", "JPX"})
WHY_ACCOUNT_EXCHANGE = "이 거래소의 품목은 계좌로 주문을 내는 실행을 열지 않았습니다"
#: 실행을 세우는 시장이 그 전략 버전의 시장과 다를 때 하는 말.
WHY_VERSION_MARKET = "이 전략의 시장과 다른 시장으로 실행을 세울 수 없습니다"
#: 저장소에 없는 전략 버전으로 실행을 세우려 할 때 하는 말.
WHY_VERSION_UNKNOWN = "저장된 전략 버전으로만 실행을 세울 수 있습니다"
#: 계좌 갈래 실행을 확인창 없이 세우려 할 때 하는 말.
WHY_UNAPPROVED = "확인창을 거치지 않은 계좌 실행은 세울 수 없습니다"
WHY_CURRENCY = "이 품목의 통화를 알 수 없어 계좌로 주문을 내는 실행을 세우지 않습니다"
WHY_WINDOW_OUTSIDE = "신규 진입 시간이 그 세션 밖입니다"

#: 실행 세션 → 국내 세션 판정의 창 이름.
_JUDGE_WINDOW = {"domestic-day": "day", "domestic-night": "night"}

#: 신규 진입 시간이 세션 안인지 볼 때 견주는 날 수. 주말·연휴를 넘길 만큼입니다.
_WINDOW_DAYS = 7


def _inside(market: str, session: str, answer: dict[str, Any]) -> bool:
    """세션 판정 하나가 그 실행의 거래 창 안인지."""
    from backend.sessions import judge

    state = str(answer.get("state") or "")
    if market == judge.DOMESTIC:
        window = _JUDGE_WINDOW.get(session, "")
        return state == window and str(answer.get("session") or "") == window
    return state in (judge.OPEN, judge.OPEN_SECOND, judge.NEXT_DAY)
DOMESTIC_NIGHT_CONTEXT = "domestic-night"
#: 계좌 갈래가 뜻하는 환경과 서버의 활성 환경이 다를 때. 갈래 이름과 환경을 넣어 씁니다.
WHY_ACCOUNT_ENVIRONMENT = "활성 환경이 {environment}이 아니라 {mode} 실행을 세울 수 없습니다"
#: 계좌 갈래 이름. 문구에 넣어 씁니다.
ACCOUNT_MODE_LABELS = {runs.SIM: "모의 계좌", runs.REAL: "실전 계좌"}
WHY_PENDING = "취소 확인을 기다리는 주문이 있어 끝낼 수 없습니다."

#: 시작을 막을 때 화면에 적는 머리말.
NOT_STARTED = "검증 실행을 시작하지 못했습니다."

# 준비 봉을 못 채웠을 때 실행에 남기는 말. 뒤에 까닭 한 줄이 붙습니다.
WHY_NO_BARS = "준비 봉을 받지 못했습니다 —"

# 계약당 증거금을 못 채웠을 때. 뒤에 까닭 한 줄이 붙고 **실행마다 한 번**입니다.
WHY_NO_MARGIN = "계약당 증거금을 받지 못했습니다 —"
WHY_NO_TABLE = "품목별세부정보를 아직 못 받았습니다"
WHY_NO_PRODUCT = "그 품목 줄을 품목별세부정보에서 찾지 못했습니다"
WHY_NO_MARGIN_FIELD = "이 품목에는 계약당 증거금 칸이 없습니다"
#: 증거금 예산이 있는데 계약당 증거금을 알 길이 없어 그 검사를 건너뛸 때(해외 선물·
#: 스프레드). 위의 한 번 알림 자리를 이 문장이 대신합니다(`runs.budget_unchecked`).
WHY_BUDGET_UNCHECKED = runs.WHY_BUDGET_UNCHECKED
#: 통화를 모르는 해외 실행을 세울 때 한 번 남기는 말과 코드(`reason`).
WHY_CURRENCY_UNKNOWN_LIMIT = "통화를 몰라 계좌 손실 한도 합계에 넣지 않습니다"
CURRENCY_UNKNOWN_REASON = "currency-unknown"

#: 환경을 바꾸려 할 때 도는 실행이 있으면 하는 말. 수를 넣어 씁니다.
WHY_SWITCH = "전략 실행 {count}개가 남아 있어 환경을 바꿀 수 없습니다."

#: 계좌 전체 손실 한도에 닿아 그 시장을 통째로 멈출 때 남기는 말과 코드(`reason`).
#: 화면은 코드로 가르고 글은 보여 주기만 합니다.
WHY_ACCOUNT_LOSS = "계좌 손실 한도에 닿아 이 시장의 전략을 모두 중지했습니다"
ACCOUNT_LOSS_REASON = "account-loss"

# ── 대사 ────────────────────────────────────────────────

#: 「확인」을 받을 수 없는 상태일 때 하는 말.
WHY_NOT_ATTENTION = "확인이 필요한 실행이 아닙니다."

#: 요청을 안 내는 상태에서 무엇을 하려 할 때 하는 말.
WHY_QUIET = "확인이 필요하거나 대사 중이라 이 실행에는 요청을 보내지 않습니다."

#: 「확인 필요」 실행을 끝내려는데 이 실행의 장부에 남은 것이 있을 때의 머리.
#: 뒤에 남은 것이 쉼표로 이어 붙습니다(`_left_in`).
WHY_ATTENTION_LEFT = "확인이 필요한 실행에 남은 것이 있어 끝낼 수 없습니다 —"

#: 「확인」 뒤에도 어긋나 있지만 이 실행의 장부에 남은 것이 없을 때 남기는 말.
ACK_CAN_END = (
    "이 실행에는 남은 보유·주문이 없어 「실행 종료」로 끝낼 수 있습니다. "
    "계좌에 남은 것은 잔고 탭에서 봅니다"
)

#: 대사를 시작·끝낼 때 실행에 남기는 말.
RECONCILE_START = "계좌와 맞춰 보는 중입니다"
RECONCILE_DONE = "계좌와 장부가 맞습니다"

#: 대사가 어긋났을 때의 머리. 뒤에 어긋난 까닭이 한 줄씩 붙습니다.
WHY_MISMATCH = "대사 불일치 —"

#: 계좌를 못 읽었을 때의 머리. **못 읽은 것도 불일치로 봅니다** — 맞는지
#: 모르는 채로 판정을 다시 열면, 계좌에 무엇이 있는지 모르고 주문을 냅니다.
WHY_NO_ACCOUNT = "계좌를 읽지 못했습니다 —"

#: 대사를 마쳤는데 대사 전 상태를 몰라 중지로 내려놓을 때 하는 말.
WHY_NO_RESUME = "대사를 마쳤습니다. 중지 상태이니 「재개」를 눌러 주세요"
#: 재접속 뒤 대사가 맞아 대사 전 상태로 돌아갈 때 하는 말과 코드(`reason`). 화면은
#: 코드로 가르고 글은 보여 주기만 합니다. 글은 화면 문구와 글자까지 같습니다.
RECONCILE_RESUME = "재접속 뒤 대사 일치 — 감시를 이어 갑니다"
RECONCILE_RESUME_REASON = "reconcile-resume"
RECONCILE_KEPT_PAUSED = "재접속 뒤 대사 일치 — 중지 상태 그대로입니다"
RECONCILE_KEPT_PAUSED_REASON = "reconcile-kept-paused"

#: 앱을 껐다 켜서 되살린 모의 실행을 대사 뒤 끝낼 때 하는 말.
WHY_RESTORED_END = "앱을 껐다 켜서 이 실행은 끝냈습니다. 이어서 돌리려면 새로 세워 주세요"

#: 대사에서 어긋난 자리마다의 말.
WHY_ORDER_GONE = "계좌에 이 앱의 미체결 주문 {no} 이(가) 없습니다"
WHY_ORDER_FILLED = "계좌의 주문 {no} 은(는) {theirs}계약 체결인데 장부는 {ours}계약입니다"
WHY_UNKNOWN_ORDERS = "계좌에 이 앱이 모르는 {code} 미체결 주문이 {count}건 있습니다"
WHY_POSITION = "계좌 보유는 {theirs}계약인데 장부는 {ours}계약입니다({code})"


class StrategyRunError(Exception):
    """할 수 없는 요청입니다.

    `problems` 는 **저장할 때와 같은 모양**입니다 — 화면이 두 자리를 한 갈래로
    읽습니다. 칸을 짚을 수 없는 요청(없는 실행, 자리 부족)에는 비어 있습니다.
    """

    def __init__(self, message: str, problems: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.problems = list(problems or [])


def ready_text(*, rows: int, new: int, updated: int | None, held: int, unread: int) -> str:
    """준비 봉을 받은 뒤 남기는 한 줄.

    **새 봉**은 이번에 처음 생긴 시각의 봉, **갱신**은 이미 있던 시각을 조회 봉으로
    덮은 수입니다. 끊겼다 다시 받을 때는 받은 줄 대부분이 이미 있던 봉이라 새 봉이
    0 일 수 있습니다. 처음 받을 때(`updated` 가 None)는 갱신을 적지 않습니다.
    **지금**은 받은 뒤 들고 있는 봉 수입니다 — 담아 두는 수에 상한이 있어 앞쪽이
    잘리면 새 봉 수와 차이가 납니다. 읽지 못한 줄이 있으면 끝에 붙입니다.
    """
    made = f"준비 봉 조회 {rows}줄 — 새 봉 {new}개"
    if updated is not None:
        made += f", 갱신 {updated}개"
    made += f"(지금 {held}개)"
    if unread:
        made += f", 못 읽은 줄 {unread}"
    return made


class RunService:
    """돌고 있는 검증 실행 전부."""

    def __init__(
        self,
        *,
        store: runs.RunStore | None = None,
        journal: journal_lib.Journal | None = None,
        master: Any = None,
        hours: Any = None,
        fetch: Any = None,
        account: Any = None,
        sender: Any = None,
        environment: Any = None,
        groups: Any = None,
        judged: Any = None,
        markets: Any = None,
        outcome: Any = None,
    ) -> None:
        self.store = store if store is not None else runs.store
        #: 계좌 주문 하나의 접수 뒤 결과를 한 번 조회하는 자리. `(실행, 주문) → 결과`
        #: 입니다(`backend/order_outcome.py`). 바꿔 낄 수 있게 둔 것은, 시험이 계좌에
        #: 닿지 않고 확인을 지나갈 수 있어야 하기 때문입니다.
        self._outcome = outcome if outcome is not None else _look_outcome
        #: 세션 판정 `(시장, 종목, 시각) → 판정`. 신규 진입 시간이 그 세션 안인지 볼 때
        #: 씁니다. 바꿔 낄 수 있게 둔 것은, 시험이 시간표 없이 이 길을 지나가야 해서입니다.
        self._judged = judged
        #: 버전 id → 그 버전을 묶는 전략 id(`groupId`). 실행과 기록 줄에 적어
        #: 버전이 지워진 뒤에도 기록을 전략으로 묶습니다. 안 주면 비워 두고,
        #: 기록을 읽을 때 버전으로 찾아 붙입니다(`records`).
        self._groups = groups if groups is not None else (lambda version_id: "")
        #: 버전 id → 저장소에 담긴 그 버전의 시장(`domestic`·`overseas`). 못 찾으면
        #: 빈 글자입니다. 주면 **저장된 버전으로만** 실행을 세우고, 그 시장과 요청
        #: 시장이 같아야 합니다. 안 주면(저장소 없이 도는 시험) 보지 않습니다.
        self._markets = markets
        self.journal = journal if journal is not None else journal_lib.journal
        self._master = master if master is not None else master_service.service
        self._hours = hours if hours is not None else session_service.service
        #: 준비 봉을 받아 오는 자리. `(실행, 봉 길이, 봉 수) → 봉 줄들` 입니다.
        #: 바꿔 낄 수 있게 둔 것은, 시험이 통신 없이 봉을 넣어 볼 수 있어야
        #: 하기 때문입니다.
        self._fetch = fetch if fetch is not None else _fetch_bars
        #: 모의 주문을 보내는 자리와 활성 환경을 답하는 자리. 바꿔 낄 수 있게
        #: 둔 것은, 시험이 계좌에 닿지 않고 이 길을 다 지나가야 하기 때문입니다.
        #: 대사에 쓰는 계좌 두 벌을 받아 오는 자리. `(실행) → {orders, positions}`
        #: 입니다. 바꿔 낄 수 있게 둔 것은, 시험이 계좌에 닿지 않고 대사를
        #: 지나갈 수 있어야 하기 때문입니다.
        self._account = account if account is not None else _fetch_account
        self._sender = sender if sender is not None else sim.NhSender()
        self._environment = environment if environment is not None else sim.environment_now
        self._engines: dict[str, engine_lib.RunEngine] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._loaded = False
        #: 시간표를 못 읽었다고 한 번 남겼는지.
        self._told_hours = False
        #: 증거금을 못 받았다고 한 번 남긴 실행들.
        self._told_margin: set[str] = set()
        #: 지금 대사 중인 실행들. 대사는 라우트 스레드와 펌프 양쪽에서 시작될
        #: 수 있어, 표시가 없으면 같은 실행에 계좌 조회가 두 벌 나갑니다.
        self._reconciling: set[str] = set()
        #: 지금 세우는 중인 계좌 실행들. 세울 때 봉을 받고 첫 대사를 지나기까지
        #: 다른 길(감시 고리·재접속)의 대사가 끼어들지 않게 합니다 — 끼어들면 갓
        #: 세운 실행이 「재접속 뒤 대사」로 읽혀 중지로 내려앉습니다(`add`).
        self._starting: set[str] = set()

    # ── 읽기 ────────────────────────────────────────────

    def load(self) -> None:
        """담아 둔 실행을 읽어 세웁니다. **돌던 종이 실행은 중지 상태입니다**.

        **모의 실행은 대사로 섭니다**(`runs._restored`) — 앱이 꺼져 있던 동안
        계좌에서 무슨 일이 있었는지 이 앱은 모릅니다. 맞춰 보기 전까지는 판정도
        주문도 열지 않고, 조회는 여기서 하지 않습니다(통신이라 루프 밖의
        일입니다). **맞춰 본 뒤에는 끝납니다** — 장부가 이어지지 않아 이어 갈
        수 없습니다(`reconcile`). 그 전략을 계속 돌리려면 실행을 새로 세웁니다.
        """
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            changed = False
            for run in self.store.read():
                made = self._stand(run)
                # 앞서 돌던 동안의 성과 수는 메모리에만 있어 다시 켜면 남지 않습니다.
                made.forget_stats()
                # 같은 봉에서 이미 규칙이 걸렸는지도 메모리에만 있어 남지 않습니다(「틱」 실행).
                made.rest_ticks()
                changed = self._renew_model(made) or changed
            if changed:
                self._save()

    @staticmethod
    def _renew_model(made: engine_lib.RunEngine) -> bool:
        """되살린 종이 실행의 모형을 지금 모형으로 고칩니다. 고쳤으면 참입니다.

        종이 장부는 지금 모형의 코드 하나만 있어, 앞 모형으로 담긴 실행도 이제부터는
        지금 모형으로 채웁니다. 바뀌는 순간을 이벤트로 남기고 고친 이름을 담아 두어,
        다시 켜도 또 남기지 않습니다. 앞서 난 체결은 그때 이름 그대로입니다. 끝난
        실행은 더 채울 것이 없어 담긴 이름 그대로 둡니다.
        """
        run = made.run
        if run.mode in runs.ACCOUNT_MODES:
            return False
        if run.state == runs.ENDED:
            if not run.model:
                run.model = paper.LEGACY_MODEL
                return True
            return False
        if run.model == paper.MODEL:
            return False
        run.model = paper.MODEL
        made._say(_now(), engine_lib.STATE, f"체결 모델이 {paper.MODEL} 로 바뀜")
        return True

    def engines(self) -> list[engine_lib.RunEngine]:
        """세워 둔 엔진 전부. 펌프가 시세를 나눠 넣을 때 씁니다."""
        self.load()
        with self._lock:
            return [self._engines[one] for one in self._order if one in self._engines]

    def watching(self) -> list[Run]:
        """시세가 있어야 하는 실행들.

        **중지한 실행도 봅니다** — 들고 있는 것의 손절·익절 감시는 중지해도
        돌아야 합니다. 끝난 실행과 확인 필요만 뺍니다.
        """
        return [
            one.run
            for one in self.engines()
            if one.run.state not in (runs.ENDED, runs.ATTENTION)
        ]

    def running(self) -> list[str]:
        """아직 돌고 있는 실행들의 id.

        **중지·확인 필요·종료는 빼고 셉니다.** 모의와 실전은 다른 계좌라
        환경이 바뀌면 시세도 장부도 다른 것이 되는데, 판정 중인 실행은 그
        경계를 모른 채 이어 갑니다.
        """
        self.load()
        with self._lock:
            return [
                one
                for one in self._order
                if self._engines[one].run.state
                not in (runs.PAUSED, runs.ATTENTION, runs.ENDED)
            ]

    def rows(self) -> list[dict[str, Any]]:
        """화면이 검증 목록에 그리는 줄들."""
        self.load()
        with self._lock:
            return [self._row(one) for one in self._order if one in self._engines]

    def bare_row(self, made: engine_lib.RunEngine) -> dict[str, Any]:
        """**값을 못 꺼내는 실행의 줄. 칸은 다 있고 값만 비어 있습니다.**

        첫 박자부터 막힌 실행은 세울 앞 값이 없습니다. 그렇다고 목록에서 빼면
        사용자는 실행이 없어진 줄 알고, **그것을 멈출 방법까지 잃습니다** —
        중지·종료 단추가 그 줄에 섭니다. 그래서 장부를 안 보고 아는 것만으로
        줄을 세우고, 나머지 칸은 비웁니다. 0 으로 채우면 화면이 「보유 없음·
        손익 0」을 **아는 값**으로 그립니다.
        """
        run = made.run
        # 장부에서 오는 값은 바로 아래에서 다시 비웁니다(`_UNKNOWN`). 줄의 **칸**을
        # 성한 줄과 똑같이 맞추려고 같은 자리를 거칩니다 — 화면이 칸이 없는 줄과
        # 값이 빈 줄을 다르게 그립니다.
        known = runs.row_of(
            run,
            made.ledger,
            feed=made.feed,
            feed_at=made.last_trade_at,
            model=made.model,
        )
        return {**known, **{key: None for key in _UNKNOWN}}

    def row(self, made: engine_lib.RunEngine) -> dict[str, Any]:
        """엔진 하나가 내는 줄. **방송이 실행마다 따로 꺼낼 때** 씁니다.

        한 번에 다 세면(`rows`) 한 실행이 값을 못 낼 때 묶음이 통째로 못
        나갑니다. 실행별로 꺼낼 수 있어야 터진 실행만 빠집니다.
        """
        return self._row(made.run.id, engine=made)

    def snapshot(self, run_id: str) -> dict[str, Any]:
        """실행 하나의 한 벌 — 줄·체결·이벤트·잔고 곡선.

        `run` 은 **목록이 내는 줄과 같은 칸**입니다. 화면이 두 답을 같은 줄로
        쓰는데 칸이 갈리면, 목록에서 보이던 값이 상세로 들어가는 순간 사라집니다.
        """
        made = self._engine(run_id)
        return {**made.snapshot(), "run": self._row(run_id, engine=made)}

    # ── 만들기 ──────────────────────────────────────────

    def add(
        self,
        *,
        run_id: str,
        version_id: str,
        draft: dict[str, Any],
        market: str,
        code: str,
        session: str,
        qty: int,
        cash: float,
        fee_per_contract: float = 0.0,
        mode: str = runs.PAPER,
        approved_at: int = 0,
        environment: str = "",
    ) -> dict[str, Any]:
        """실행 하나를 세웁니다.

        **시작할 수 없는 설정은 여기서 막습니다** — 저장은 「담되 안 돈다」를
        받아 주지만(`validate.py`), 시작은 돌 수 있는 설정만 받습니다.

        `environment` 는 **화면이 믿는 활성 환경**입니다. 계좌 갈래는 갈래가 뜻하는
        환경·이 값·서버의 활성 환경이 셋 다 같아야 섭니다(`start_problems`). 선
        환경은 실행에 적히고 바뀌지 않습니다. 승인(`approved_at`)도 이 요청에만
        실리므로, 승인은 늘 선 환경에 묶입니다.
        """
        self.load()
        # 저장할 때와 같은 정리를 거칩니다 — 화면이 저장 전 설정을 보내도 시장에
        # 없는 값(해외의 체결조건 같은 것)이 실행에 실리지 않습니다. 시장은 요청
        # 시장이고, 아래에서 저장된 버전의 시장과 같은지 봅니다(다르면 세우지 않음).
        draft = validate.normalized({"market": market}, draft) or draft
        with self._lock:
            # **끝난 실행은 안 셉니다.** 이 한도가 대는 까닭은 실시간 구독
            # 자리이고, 끝난 실행은 시세를 안 받습니다(`watching`). 세면 파일에
            # 남은 끝난 실행 일곱만으로 새 실행을 아예 못 세우게 됩니다.
            #
            # 확인이 필요한 실행은 셉니다 — 사용자가 풀면 다시 자리를 씁니다.
            standing = [
                one for one in self._engines.values() if one.run.state != runs.ENDED
            ]
            if len(standing) >= runs.MAX_RUNS:
                raise StrategyRunError(runs.WHY_RUNS)
            if run_id in self._engines:
                raise StrategyRunError(WHY_RUN_ID, [_one("id", WHY_RUN_ID)])

            aux = draft.get("aux") if isinstance(draft.get("aux"), dict) else {}
            found = self.start_problems(
                draft=draft,
                market=market,
                code=code,
                session=session,
                qty=qty,
                cash=cash,
                fee_per_contract=fee_per_contract,
                mode=mode,
                environment=environment,
                approved=bool(approved_at),
            )
            if self._markets is not None:
                # 화면은 저장된 버전으로만 시작합니다. 저장소에 없는 버전을 받아 주면
                # 그 실행이 어느 시장 전략인지 맞춰 볼 길이 없습니다.
                known = _market_or_blank(self._markets, version_id)
                if not known:
                    found.insert(0, _one("versionId", WHY_VERSION_UNKNOWN))
                elif known != market:
                    found.insert(0, _one("market", WHY_VERSION_MARKET))
            if found:
                raise StrategyRunError(found[0]["message"], found)

            facts = self.instrument(market, code, runs.session_key(session) or "")
            currency = self._currency_of(market, code)
            run = Run(
                id=run_id,
                version_id=version_id,
                group_id=_group_or_blank(self._groups, version_id),
                code=code,
                session=runs.session_key(session) or str(session),
                market=market,
                exchange=str(facts["exchange"] or ""),
                kind=str(facts["kind"] or ""),
                draft=draft,
                qty=max(int(qty), 1),
                cash=float(cash),
                fee_per_contract=float(fee_per_contract),
                # 해외는 승수를 모르면 `None` 입니다 — 1 로 세우지 않습니다(계좌 실행은
                # 시작에서 막힙니다). 국내는 모르면 1 로 셉니다.
                multiplier=(
                    facts["multiplier"]
                    if market == validate.OVERSEAS
                    else facts["multiplier"] or 1.0
                ),
                margin=facts["margin"],
                margin_budget=_number(aux.get("marginBudget")),
                daily_loss=_number(aux.get("dailyLoss")),
                # **계좌 갈래는 대사부터입니다** — 계좌에 이 앱이 모르는
                # 주문이나 보유가 있는지 보고 나서 판정을 엽니다. 종이 실행은
                # 맞춰 볼 계좌가 없어 바로 봅니다.
                state=runs.RECONCILING if mode in runs.ACCOUNT_MODES else runs.ARMED,
                mode=mode,
                model=sim.MODEL if mode in runs.ACCOUNT_MODES else paper.MODEL,
                currency=currency,
                currency_unknown=market == validate.OVERSEAS and not currency,
                approved_at=int(approved_at or 0),
                # 계좌 갈래가 선 환경. 위 검사가 셋이 같을 때만 여기 오게 합니다.
                environment=runs.ENVIRONMENT_OF.get(mode, ""),
            )
            made = self._stand(run, tick=facts["tick"])
            self._save()
            made._say(_now(), engine_lib.STATE, "검증 실행을 세웠습니다")
            if run.currency_unknown:
                made._say(
                    _now(),
                    engine_lib.READY,
                    WHY_CURRENCY_UNKNOWN_LIMIT,
                    extra={"reason": CURRENCY_UNKNOWN_REASON},
                )
            if self.session_end_unknown(market, code, aux):
                made._say(_now(), engine_lib.SESSION, WHY_SESSION_END_UNKNOWN)
            if mode in runs.ACCOUNT_MODES:
                self._starting.add(run_id)

        if mode not in runs.ACCOUNT_MODES:
            # **잠금 밖에서 받아 옵니다.** 조회는 통신이라 제한기에서 기다릴 수
            # 있는데, 그동안 목록 잠금을 쥐고 있으면 다른 요청이 다 멈춥니다.
            self.refill(run_id)
            return self._row(run_id)
        try:
            self.refill(run_id)
            return self.reconcile(run_id, _now(), first=True)
        finally:
            # 무슨 일이 있어도 뺍니다 — 남으면 그 실행은 대사를 영영 못 받습니다.
            with self._lock:
                self._starting.discard(run_id)

    def start_problems(
        self,
        *,
        draft: dict[str, Any],
        market: str,
        code: str,
        session: str,
        qty: Any,
        cash: Any,
        fee_per_contract: Any,
        mode: str = runs.PAPER,
        environment: str = "",
        approved: bool = True,
    ) -> list[dict[str, str]]:
        """시작을 막을 까닭들. 비어 있으면 세울 수 있습니다.

        `approved` 는 화면 확인창을 지났는지입니다. 계좌 갈래는 그것 없이 세우지
        않습니다 — 세워 두고 주문만 막으면 아무것도 안 하는 실행이 목록에 남습니다.

        **저장 검사보다 좁습니다.** 저장은 「담되 안 돈다」를 받아 주지만
        (`validate.py` 의 `warning`), 시작은 **돌 수 있는 것**만 받습니다 —
        켜진 진입 규칙이 없는 전략, 모형이 못 다루는 주문유형이 그렇습니다.
        여기서 더 보는 것은 저장 때는 없던 값들입니다(종목·세션·수량·자금).
        """
        found: list[dict[str, str]] = [
            one for one in validate.problems(draft, market=market) if validate.blocked([one])
        ]
        aux = draft.get("aux") if isinstance(draft.get("aux"), dict) else {}
        found.extend(runnable_problems(draft))

        if mode not in runs.MODES:
            found.append(_one("mode", WHY_MODE))
        elif mode in runs.ACCOUNT_MODES:
            # 계좌에 실제 주문이 남는 갈래입니다. 환경과 거래소·통화를 여기서 좁힙니다.
            if market == validate.OVERSEAS:
                if self.instrument(market, code).get("multiplier") is None and self._master.locate(market, code) is not None:
                    found.append(_one("code", runs.WHY_MULTIPLIER_UNKNOWN))
                exchange, currency = self._overseas_identity(code)
                if exchange in CLOSED_EXCHANGES:
                    found.append(_one("market", WHY_ACCOUNT_EXCHANGE))
                if not currency:
                    found.append(_one("market", WHY_CURRENCY))
            found.extend(self._environment_problems(mode, environment))
            if not approved:
                found.append(_one("approved", WHY_UNAPPROVED))
        if (
            mode in runs.MODES
            and market != validate.OVERSEAS
            and not str(aux.get("fillCond") or "").strip()
        ):
            # 국내 주문은 체결조건이 꼭 있어야 합니다 — 비어 있으면 서버가 주문을
            # 거부합니다. 종이 실행도 같은 설정으로 계좌 실행과 같은 결과를 내야 하므로
            # 같이 막습니다. 저장은 빈 칸을 받아 줍니다(고치던 설정을 잃지 않게).
            found.append(_one("aux.fillCond", validate.FILL_COND_WHY))

        if self._master.locate(market, code) is None:
            found.append(_one("code", WHY_CODE))
        if runs.session_key(session) not in runs.MARKET_SESSIONS.get(market, ()):
            found.append(_one("session", WHY_SESSION))
        else:
            found.extend(
                self._window_problems(market, code, runs.session_key(session) or "", aux)
            )

        # 세션 끝 정리를 켰는데 장 시간표를 모르면 정리 시각이 없습니다. 계좌 갈래는
        # 정리 안 된 보유가 계좌에 남으므로 막습니다. 종이는 세우고 실행에 남깁니다(`add`).
        if mode in runs.ACCOUNT_MODES and self.session_end_unknown(market, code, aux):
            found.append(_one("aux.sessionEndFlat", WHY_SESSION_END_UNKNOWN))

        found.extend(qty_problems(qty, aux))

        money = _number(cash)
        if money is None or money <= 0:
            found.append(_one("cash", WHY_CASH))
        fee = _number(fee_per_contract)
        if fee is None or fee < 0:
            found.append(_one("feePerContract", WHY_FEE))
        return validate.unique(found)

    def session_end_unknown(self, market: str, code: str, aux: dict[str, Any]) -> bool:
        """세션 끝 정리를 켰는데 그 종목의 장 시간표를 모르는지(`timetable_unknown`)."""
        if not aux.get("sessionEndFlat") or not (_whole(aux.get("sessionEndMinutes")) or 0) > 0:
            return False
        return timetable_unknown(market, code, judged=self._judged)

    def _overseas_identity(self, code: str) -> tuple[str, str]:
        """해외 종목의 `(거래소, 통화)`. 모르면 빈 글자입니다.

        거래소는 종목 마스터의 거래소 코드를, 통화는 장운영정보의 그 품목 줄을
        씁니다 — 세션 판정이 쓰는 표와 같은 줄입니다.
        """
        from backend.sessions import judge

        product_of = getattr(self._master, "product_of", None)
        contract = product_of(validate.OVERSEAS, code) if product_of is not None else None
        exchange = judge.exchange_of(str((contract or {}).get("exch") or "")) if contract else ""
        try:
            table = self._hours.cached(validate.OVERSEAS)
        except Exception:  # noqa: BLE001 — 표를 못 읽으면 통화를 모르는 것입니다.
            table = None
        row = judge.overseas_row(contract, table)
        currency = str((row or {}).get("currency") or "").strip().upper()
        return exchange, currency

    def _currency_of(self, market: str, code: str) -> str:
        """새 실행에 적는 통화. 국내는 원이고, 해외는 품목 줄의 통화(모르면 빈 글자)입니다."""
        if market != validate.OVERSEAS:
            return runs.LEGACY_CURRENCY["domestic"]
        return self._overseas_identity(code)[1]

    def account_state(self) -> dict[str, Any]:
        """지금 세울 수 있는 계좌 갈래(`accountModes`)와 서버의 활성 환경.

        환경을 바꾸는 중이거나 활성 환경을 못 읽으면 갈래는 비어 있습니다 — 화면은
        이때 확인창을 열지 않습니다.
        """
        from backend.nh_api import runtime

        active = str(self._environment() or "").strip() or None
        modes = [] if runtime.switching() or active is None else runs.account_modes(active)
        return {"accountModes": modes, "activeEnvironment": active}

    def _window_problems(
        self, market: str, code: str, session: str, aux: dict[str, Any]
    ) -> list[dict[str, str]]:
        """신규 진입 시간이 **그 품목·그 세션 안**인지. 시간표를 모르면 막지 않습니다.

        화면이 쓰는 세션 판정(`sessions_routes.judged`)에 그 시각을 물어 봅니다. 시작은
        그 시각이 세션 안이어야 하고, 끝은 구간에서 빠지므로 1분 앞이 안이어야 합니다.
        야간 세션의 정오 전 시각은 다음 날로 셉니다(자정을 넘는 창).
        """
        from datetime import datetime, timedelta

        from backend.sessions import judge

        judged = self._judged
        if judged is None:
            from backend.sessions.sessions_routes import judged
        today = now_kst().date()
        made: list[dict[str, str]] = []
        for name, back in (("entryStart", 0), ("entryEnd", 1)):
            parsed = rule_lib.parse_clock(aux.get(name))
            if parsed is None:
                continue
            # 오늘이 주말·휴장일이면 맞는 시각도 닫힘으로 답합니다. 이레 안에 그
            # 시각이 세션 안인 날이 하루라도 있으면 맞는 시각으로 봅니다. 시간표를
            # 모르면(판정 원천이 없음) 거기서 그치고 막지 않습니다.
            outside = True
            for offset in range(_WINDOW_DAYS):
                base = today + timedelta(days=offset)
                night_next = session == DOMESTIC_NIGHT_CONTEXT and parsed[0] < 12
                day = base + timedelta(days=1) if night_next else base
                moment = datetime(day.year, day.month, day.day, *parsed, tzinfo=KST) - timedelta(
                    minutes=back
                )
                answer = judged(market, code, moment)
                if str(answer.get("source") or "") == judge.UNKNOWN or _inside(market, session, answer):
                    outside = False
                    break
            if outside:
                made.append(_one(f"aux.{name}", WHY_WINDOW_OUTSIDE))
        return made

    def _environment_problems(self, mode: str, claimed: str) -> list[dict[str, str]]:
        """계좌 갈래를 세울 수 있는 환경인지. 셋이 같아야 합니다.

        갈래가 뜻하는 환경(`runs.ENVIRONMENT_OF`) · 화면이 믿는 환경 · 서버의 활성
        환경입니다. 화면과 서버를 대는 판정(전환 중·못 읽음·다름)은 일반 주문과
        같은 자리를 씁니다(`orders.refusal_of`) — 화면이 옛 환경을 든 채 실전
        실행을 세우는 길을 막습니다.
        """
        from backend import orders

        active = str(self._environment() or "").strip() or None
        refused = orders.refusal_of(active, str(claimed or "").strip())
        if refused is not None:
            return [_one("environment", refused[1])]
        wanted = runs.ENVIRONMENT_OF[mode]
        if active != wanted:
            label = ENVIRONMENT_LABELS.get(wanted, wanted)
            return [
                _one(
                    "mode",
                    WHY_ACCOUNT_ENVIRONMENT.format(
                        environment=label, mode=ACCOUNT_MODE_LABELS[mode]
                    ),
                )
            ]
        return []

    def instrument(self, market: str, code: str, session: str = "") -> dict[str, Any]:
        """그 종목의 틱·승수·증거금·남은 날. **모르는 것은 `None` 입니다.**

        증거금은 국내 품목별세부정보에만 있습니다(해외는 그 칸이 없습니다).
        0 으로 답하면 「한 계약도 못 든다」가 되므로 모른다고 답합니다.

        증거금은 넷입니다 — 계약당 개시증거금(`margin`)과 두 비율, 그리고
        계약당 유지증거금(`maintenanceMargin`)입니다. **마지막 하나는 응답에
        없어 늘 `None`** 이고 까닭은 `domestic_hours.WHY_NO_MAINTENANCE` 에
        있습니다. 비율로는 볼 수 있습니다.

        `why` 로 **못 채운 까닭**을 함께 냅니다 — 「표를 아직 못 받았다」와
        「그 품목에 그 칸이 없다」는 사용자가 할 일이 다릅니다.
        """
        made: dict[str, Any] = {
            "tick": None,
            "multiplier": None,
            "margin": None,
            "initialMarginRate": None,
            "maintenanceMarginRate": None,
            "maintenanceMargin": None,
            "daysLeft": None,
            # 해외 차트 조회가 **반드시** 받는 값입니다. 국내에는 없습니다.
            "exchange": "",
            # 선물·옵션·스프레드. 계약당 드는 돈이 갈립니다(`runs.per_contract`).
            "kind": "",
            "why": "",
        }
        row = self._master.locate(market, code)
        if row is None:
            made["why"] = WHY_CODE
            return made
        made["daysLeft"] = days_left_of(row, market, date.today())
        made["kind"] = str(row.get("kind") or "").strip()
        if market != "domestic":
            made["tick"] = _number(row.get("tsiz"))
            made["multiplier"] = overseas_multiplier(row)
            made["exchange"] = str(row.get("ExchName") or "").strip()
            made["why"] = WHY_NO_MARGIN_FIELD if made["multiplier"] else WHY_NO_MULTIPLIER
            return made

        made["multiplier"] = _number(row.get("ctrt_size"))
        found = self._product_row(row, session)
        if found is None:
            made["why"] = WHY_NO_TABLE if not self._products() else WHY_NO_PRODUCT
            return made
        made["tick"] = _number(found.get("tick"))
        # 증거금 넷은 한 자리에서 꺼냅니다 — 화면에 내는 라우트와 같은 값입니다.
        money = domestic_hours.margins(found)
        made["margin"] = money["contractInitialMargin"]
        made["initialMarginRate"] = money["initialMarginRate"]
        made["maintenanceMarginRate"] = money["maintenanceMarginRate"]
        made["maintenanceMargin"] = money["maintenanceMargin"]
        if _number(found.get("multiplier")):
            made["multiplier"] = _number(found.get("multiplier"))
        if made["margin"] is None:
            # 옵션은 칸이 비는 것이 맞습니다 — 매수는 프리미엄으로 세고 매도는
            # 거래소 계산이라 셀 수 없습니다(`runs.per_contract`).
            made["why"] = (
                runs.WHY_SOLD_OPTION if made["kind"] == runs.OPTION else WHY_NO_MARGIN_FIELD
            )
        return made

    def _product_row(self, row: dict[str, Any], session: str) -> dict[str, Any] | None:
        """그 종목이 속한 품목 줄. 없으면 `None`.

        **고르는 규칙은 한 곳에 있습니다**(`domestic_hours.product_row`) — 세션
        판정도 같은 규칙으로 그 품목의 시간 구간을 찾습니다. 두 벌로 적으면
        같은 종목을 두고 증거금은 선물 줄에서, 시간은 옵션 줄에서 오는 날이
        옵니다.

        **창이 없으면 다른 창의 줄로 답합니다**(`any_window`) — 여기서 읽는 틱과
        증거금은 창이 달라도 그 품목의 값입니다.
        """
        wanted = NIGHT if session == bar_lib.NIGHT_CONTEXT else DAY
        return domestic_hours.product_row(
            self._products(), row, wanted, any_window=True
        )

    # ── 다루기 ──────────────────────────────────────────

    def pause(self, run_id: str, at: int) -> dict[str, Any]:
        """신규 진입 중지. **보유의 손절·익절 감시는 그대로 돕니다.**

        거두는 것은 **진입 주문뿐입니다.** 나가 있는 청산까지 거두면 들고 있는
        것을 감시할 주문이 사라져, 중지를 누른 순간 보유가 무방비가 됩니다.
        청산 중이면 상태는 `exiting` 그대로 두고 중지를 기억해 두었다가
        청산이 끝난 뒤에 듭니다(`runs.TRANSITIONS`).
        """
        made = self._engine(run_id)
        self._quiet(made)
        with made.lock:
            self._pause(made, at)
        self._after(made, at, "신규 진입을 중지했습니다")
        return self._row(run_id, engine=made)

    def _pause(self, made: engine_lib.RunEngine, at: int) -> None:
        """진입 주문만 거둡니다. **장부에 맡깁니다** — 종이는 즉시 닫히고
        모의는 계좌에 취소를 내고 확인을 기다립니다.

        여기서 상태를 직접 바꾸면 모의 실행이 「화면에는 없는데 계좌에는 있는」
        주문을 만듭니다.
        """
        # 종이는 여기서 닫혀 결과 줄이 바로 남고, 모의는 계좌 확인이 온 뒤에
        # 남습니다(`engine._forget`). 주문 거부로 엔진이 스스로 멈출 때도 같은
        # 자리(`_withdraw_entries`)를 지납니다.
        made._withdraw_entries(at, "신규 진입 중지로 취소했습니다")
        made.run.move(runs.PAUSE)

    def resume(self, run_id: str, at: int) -> dict[str, Any]:
        """재개. **다음 봉부터** 판정합니다 — 이미 참이던 조건으로 바로 걸리지 않습니다.

        재개할 때 이 실행의 구독을 NH선물 쪽에 **다시 걸어 달라고** 표시합니다.
        서버 쪽에서 조용히 끊긴 구독은 장부에 걸린 채로 남아, 멈췄다 재개하는
        것만으로는 되살아나지 않습니다. 거는 일은 실행 목록을 돌리는 자리가
        합니다(`pump.RunPump`).
        """
        made = self._engine(run_id)
        with made.lock:
            made.run.move(runs.RESUME)
            made.refresh_wanted = True
        self._after(made, at, "재개했습니다")
        return self._row(run_id, engine=made)

    def flatten(self, run_id: str, at: int) -> dict[str, Any]:
        """보유를 전부 닫습니다. 시장가 전량이고 진입 규칙은 그동안 발사하지 않습니다."""
        made = self._engine(run_id)
        self._quiet(made)
        with made.lock:
            held = made.ledger.position
            if held.side is None:
                raise StrategyRunError(WHY_NOTHING)
            last = made.book.last_price or held.avg_price
            made._close(held.size, last, engine_lib.FLATTEN, at)
        self._after(made, at, "선택 전략 청산을 냈습니다")
        return self._row(run_id, engine=made)

    def end(self, run_id: str, at: int) -> dict[str, Any]:
        """실행을 끝냅니다. **보유가 있으면 못 끝냅니다** — 먼저 청산해야 합니다.

        **「확인 필요」 실행도 끝낼 수 있습니다** — 이 실행의 장부에 보유도 열린
        주문(취소 확인 대기 포함)도 없을 때만입니다. 끝내기는 계좌에 아무 요청도
        보내지 않으므로, 이 실행 몫으로 계좌에 남는 것이 없으면 계좌와 장부가
        어긋난 채여도 안전합니다. 어긋난 자리(이 실행이 모르는 주문·보유)는 이
        실행의 것이 아니고 잔고 탭에서 봅니다 — 앱을 다시 켤 때 대사 뒤 끝내는
        규칙(`_retire`)과 같은 판단입니다. 남은 것이 있으면 막고, **무엇이 남았는지**
        를 문구에 싣습니다. 「대사 중」은 계좌와 맞춰 보는 도중이라 그대로 막습니다.
        """
        made = self._engine(run_id)
        with made.lock:
            if made.run.state == runs.ATTENTION:
                left = _left_in(made)
                if left:
                    raise StrategyRunError(f"{WHY_ATTENTION_LEFT} {', '.join(left)}")
            else:
                self._quiet(made)
                # **확인 대기를 먼저 봅니다.** 취소를 낸 주문은 미체결이기도 한데,
                # 「청산해 주세요」보다 「취소 확인을 기다리는 중」이 사용자가 할
                # 일을 정확히 말해 줍니다. 계좌는 취소를 받고 나중에 답합니다.
                if getattr(made.book, "pending", 0):
                    raise StrategyRunError(WHY_PENDING)
                if made.ledger.position.quantity or made.book.open_orders:
                    raise StrategyRunError(WHY_HOLDING)
            made.run.move(runs.END)
            made.run.stopped_at = at
        self._after(made, at, "실행을 끝냈습니다")
        return self._row(run_id, engine=made)

    def delete(self, run_id: str) -> None:
        """끝난 실행을 목록에서 지웁니다. **기록은 남습니다.**"""
        made = self._engine(run_id)
        if made.run.state not in runs.DELETABLE:
            raise StrategyRunError(WHY_NOT_ENDED)
        with self._lock:
            self._engines.pop(run_id, None)
            self._order = [one for one in self._order if one != run_id]
            self._save()

    def account_loss(
        self, limits: dict[str, float], at: int, *, environment: str = limit_lib.PAPER
    ) -> list[str]:
        """계좌 전체 손실 한도를 봅니다. 닿은 시장의 실행을 **전부** 중지합니다.

        `environment` 는 **어느 계좌의 한도인지**입니다(`limits.REAL`·`PAPER`). 실전
        계좌 실행의 손실은 실전 한도로만, 나머지는 모의 쪽 한도로만 셉니다 — 두
        계좌의 손익을 한 합계로 더하지 않습니다.

        한도는 **통화마다 따로**입니다. 원·달러·홍콩달러의 손익을 더하면 뜻이 없는
        수가 나옵니다 — 환율로 맞추지 않습니다. `limits` 는 `{통화: 한도}` 입니다.

        전략별 한도(`entry_block`)가 그 실행 하나를 멈추는 것과 달리, 이 한도는
        **같은 시장의 실행을 다 멈춥니다.** 한 전략이 낸 손실이 아니라 계좌가
        진 손실이기 때문입니다.
        """
        made: list[str] = []
        for key, limit in limits.items():
            # 시장 이름 열쇠(`domestic`·`overseas`)는 원·달러로 읽습니다.
            currency = limit_lib.currency_of_key(key)
            if currency is None or limit is None or limit <= 0:
                continue
            group = [
                one
                for one in self.engines()
                if runs.currency_of(one.run) == currency
                and limit_lib.environment_of(one.run) == environment
                # 승수를 모르는 실행은 손익을 돈으로 셀 수 없어 합에 넣지 않습니다.
                and one.run.multiplier is not None
                # 통화를 모르는 실행도 어느 통화 합계에 넣을지 몰라 빼 둡니다.
                and not one.run.currency_unknown
            ]
            lost = sum(
                runs.day_profit(one.run, one.ledger, one.book.last_price) for one in group
            )
            if lost > -float(limit):
                continue
            for one in group:
                if one.run.state in (runs.PAUSED, runs.ATTENTION, runs.ENDED):
                    continue
                self.pause(one.run.id, at)
                one._say(
                    at, engine_lib.STATE, WHY_ACCOUNT_LOSS, extra={"reason": ACCOUNT_LOSS_REASON}
                )
                made.append(one.run.id)
        return made

    def stop_all(self, at: int, modes: Iterable[str] | None = None) -> dict[str, Any]:
        """전체 신규 진입 중지. 이미 중지·확인 필요·끝난 실행은 빼고 셉니다.

        `modes` 를 주면 그 갈래(`paper`·`sim`·`real`)의 실행만 멈춥니다 — 검증 탭은
        종이 실행만, 진행 탭은 계좌 실행만 멈추는 단추를 둡니다. 안 주면 전부입니다.
        `pausedIds` 는 이번에 멈춘 실행들이고, `modes` 는 적용한 갈래입니다(안 주면 전 갈래)
        — 화면이 갈래를 가려 멈추는 서버인지 이 칸으로 압니다.
        """
        self.load()
        picked = set(modes) if modes is not None else None
        with self._lock:
            wanted = [
                one
                for one in self.running()
                if picked is None or self._engines[one].run.mode in picked
            ]
        for one in wanted:
            self.pause(one, at)
        applied = [one for one in runs.MODES if picked is None or one in picked]
        return {"paused": len(wanted), "pausedIds": wanted, "modes": applied, "runs": self.rows()}

    # ── 대사 ────────────────────────────────────────────

    def reconcile(self, run_id: str, at: int, *, first: bool = False) -> dict[str, Any]:
        """계좌와 장부를 맞춰 보고 실행을 내려놓습니다. **시작·재기동·재접속**입니다.

        처음 시작하는 실행은 맞으면 `armed` 로 엽니다. **재접속 뒤**에는 맞으면
        대사에 들기 직전 상태로 돌아갑니다 — 감시 중이던 실행은 감시를 이어 가고,
        사용자가 중지해 둔 실행은 중지 그대로입니다(`Run.keep_after_reconcile`). 그
        상태를 모르면 중지로 내려놓습니다. **앱을 다시 켠 뒤에는 자동 재개가
        없습니다** — 꺼져 있던 동안 손절 감시가 돌지 않았습니다(`_retire`).

        어긋나면 `attention` 이고, 그동안 이 실행은 **어떤 요청도 내지
        않습니다**. 푸는 것은 사용자의 「확인」입니다(`acknowledge`).

        **종이 실행은 여기를 지나지 않습니다** — 맞춰 볼 계좌가 없습니다. 봉만
        다시 받고 이어 갑니다.

        **끝난 실행도 지나지 않습니다.** 맞춰 볼 장부가 없고, 어긋난 것으로
        보이면 끝낸 실행이 「확인 필요」로 되살아납니다.

        **앱을 껐다 켜서 되살린 모의 실행은 여기를 한 번 지나고 끝납니다**
        (`_retire`).
        """
        made = self._engine(run_id)
        if made.run.mode not in runs.ACCOUNT_MODES or made.run.state == runs.ENDED:
            return self._row(run_id, engine=made)
        with self._lock:
            if run_id in self._reconciling:
                return self._row(run_id, engine=made)
            if not first and run_id in self._starting:
                # 세우는 중입니다. 그 실행의 첫 대사는 세우는 쪽이 곧 합니다(`add`).
                return self._row(run_id, engine=made)
            self._reconciling.add(run_id)

        try:
            with made.lock:
                made.run.move(runs.RECONCILE)
            self._after(made, at, RECONCILE_START)

            found = self._compare(made, at)
            if made.run.restored:
                return self._retire(made, at, found)
            if found:
                return self._flag(made, at, found)

            if first:
                with made.lock:
                    made.run.move(runs.STARTED)
                self._after(made, at, RECONCILE_DONE)
                return self._row(run_id, engine=made)
            with made.lock:
                known = made.run.before_reconcile in runs.KEEP_STATES
                back = made.run.keep_after_reconcile(
                    held=made.ledger.position.side is not None,
                    open_entry=made.open_entry > 0,
                    open_exit=made.open_exit > 0,
                )
            if not known:
                self._after(made, at, WHY_NO_RESUME)
            elif back == runs.PAUSED:
                self._resumed(made, at, RECONCILE_KEPT_PAUSED, RECONCILE_KEPT_PAUSED_REASON)
            else:
                self._resumed(made, at, RECONCILE_RESUME, RECONCILE_RESUME_REASON)
            return self._row(run_id, engine=made)
        finally:
            with self._lock:
                self._reconciling.discard(run_id)

    def audit(self, run_id: str, at: int) -> list[str]:
        """맞춰 보기만 합니다. **상태를 내려놓지 않습니다** — 거래일 경계와
        화면이 진행 탭을 열 때의 자리입니다.

        여기서도 `paused` 로 내리면, 탭을 여는 것만으로 돌던 전략이 멈춥니다.
        어긋났을 때만 `attention` 으로 갑니다. 어긋난 까닭들을 냅니다.
        """
        made = self._engine(run_id)
        # 되살린 실행은 건너뜁니다 — 대사가 곧 그 실행을 끝냅니다(`_retire`).
        # 여기서 먼저 어긋난 것을 보면 「확인 필요」로 세워 버립니다.
        if (
            made.run.mode not in runs.ACCOUNT_MODES
            or made.run.restored
            or made.run.state in (runs.ENDED, runs.ATTENTION)
        ):
            made.audit_wanted = False
            return []
        found = self._compare(made, at)
        made.audit_wanted = False
        if found:
            self._flag(made, at, found)
        return found

    def audit_all(self, at: int) -> dict[str, Any]:
        """모의 실행을 **차례로** 맞춰 봅니다. 화면이 진행 탭을 열 때 부릅니다.

        한꺼번에 부르지 않는 것은 계좌 조회에 초당 제한이 있어서입니다 — 실행
        하나가 조회 둘(주문내역·잔고내역)을 씁니다.
        """
        made: dict[str, list[str]] = {}
        for one in self.engines():
            if one.run.mode not in runs.ACCOUNT_MODES or one.run.state in (runs.ENDED, runs.ATTENTION):
                continue
            made[one.run.id] = self.audit(one.run.id, at)
        return {
            "checked": list(made),
            "mismatched": {key: why for key, why in made.items() if why},
            # 줄까지 함께 냅니다 — 화면이 답 하나로 목록을 다시 그립니다.
            "runs": self.rows(),
        }

    def reconcile_wanted(self) -> list[str]:
        """아직 대사를 못 지난 모의 실행들의 id.

        앱을 켜면 담아 둔 모의 실행이 여기 서 있습니다(`load`). 재접속 뒤에도
        같은 자리를 지납니다 — 상태가 곧 표시라 따로 깃발을 두지 않습니다.
        """
        return [
            one.run.id
            for one in self.engines()
            if one.run.mode in runs.ACCOUNT_MODES
            and one.run.state == runs.RECONCILING
            and one.run.id not in self._reconciling
            and one.run.id not in self._starting
        ]

    def audit_wanted(self) -> list[str]:
        """거래일이 바뀌어 맞춰 보아야 하는 실행들의 id.

        **여기서 조회하지 않습니다** — 누가 맞춰 보아야 하는지만 답하고, 부르는
        쪽이 루프 밖으로 넘깁니다(`refill` 과 같은 규칙입니다).
        """
        return [one.run.id for one in self.engines() if one.audit_wanted]

    def confirm_wanted(self, now: int) -> list[tuple[str, str]]:
        """접수 뒤 결과를 확인할 때가 된 계좌 주문들 — `(실행 id, 주문번호)`.

        **여기서 조회하지 않습니다**(`audit_wanted` 와 같은 규칙). 끝난 실행은 뺍니다.
        """
        made: list[tuple[str, str]] = []
        for one in self.engines():
            if one.run.mode not in runs.ACCOUNT_MODES or one.run.state == runs.ENDED:
                continue
            made.extend((one.run.id, no) for no in one.confirm_wanted(now))
        return made

    def confirm(self, run_id: str, order_no: str, at: int, *, look: bool = True) -> bool:
        """계좌 주문 하나의 접수 뒤 결과를 확인합니다(`backend/order_outcome.py`). 거절이면 참.

        조회는 **실행이 선 환경**으로 나갑니다. 조회가 실패하면 「못 정함」으로 보고
        다음 확인을 잡습니다 — 확인을 못 했다고 주문을 거절로 적지 않습니다.
        `look=False` 면 조회하지 않고 「못 정함」으로 넘깁니다(저장해 둔 응답으로
        도는 중).
        """
        made = self._engine(run_id)
        found = next(
            (one for one in made.book.orders if getattr(one, "order_no", "") == order_no), None
        )
        if found is None:
            return False
        outcome: dict[str, Any] | None = None
        if look:
            try:
                outcome = self._outcome(made.run, found)
            except Exception:  # noqa: BLE001 — 확인을 못 한 것이지 주문이 실패한 것이 아닙니다.
                logger.warning(
                    "실행 %s 의 주문 %s 결과를 확인하지 못했습니다.", run_id, order_no, exc_info=True
                )
        if not made.on_outcome(order_no, outcome, at):
            return False
        self._save()
        return True

    def acknowledge(self, run_id: str, at: int) -> dict[str, Any]:
        """사용자의 「확인」. **대사가 맞을 때만** 풀립니다.

        상태만 풀어 주면 계좌와 어긋난 채로 판정이 다시 열립니다 — 확인은
        「보았다」이지 「맞다」가 아닙니다. 그래서 여기서 한 번 더 맞춰 봅니다.

        그래도 어긋나 있는데 이 실행의 장부에 남은 것이 없으면, 「실행 종료」로
        끝낼 수 있다는 줄을 함께 남깁니다(`end`). 계좌 쪽 어긋남이 이 실행 밖의
        것이면 확인을 몇 번 눌러도 같은 답이라, 빠져나갈 길을 알려야 합니다.
        """
        made = self._engine(run_id)
        if made.run.state != runs.ATTENTION:
            raise StrategyRunError(WHY_NOT_ATTENTION)

        found = self._compare(made, at) if made.run.mode in runs.ACCOUNT_MODES else []
        if found:
            for why in found:
                made._say(at, engine_lib.RECONCILE, f"{WHY_MISMATCH} {why}")
            with made.lock:
                empty = made.run.state == runs.ATTENTION and not _left_in(made)
            if empty:
                made._say(at, engine_lib.RECONCILE, ACK_CAN_END)
            self._save()
            return self._row(run_id, engine=made)

        with made.lock:
            # 맞춰 보는 사이에 다른 요청(「실행 종료」)이 먼저 상태를 옮겼으면
            # 그 상태 그대로 둡니다.
            still = made.run.state == runs.ATTENTION
            if still:
                made.run.move(runs.ACKNOWLEDGE)
                # 확인을 기다리는 동안 시세를 보지 않았던 공백을 이제 적습니다.
                made.after_attention(at)
        if not still:
            return self._row(run_id, engine=made)
        self._after(made, at, "확인을 받아 중지 상태로 두었습니다")
        return self._row(run_id, engine=made)

    def _quiet(self, made: engine_lib.RunEngine) -> None:
        """요청을 안 내는 상태면 막습니다.

        「확인 필요」·「대사 중」은 계좌와 장부가 맞는지 모르는 자리입니다.
        그 상태에서 중지·청산·종료를 받으면 **취소나 주문이 계좌로 나갑니다** —
        무엇을 취소하는지 모르는 채로. 종료만은 「확인 필요」에서 장부가 빈
        실행에 한해 따로 받습니다(`end`) — 그때는 계좌로 나가는 요청이 없습니다.
        """
        if made.run.state in runs.QUIET_STATES:
            raise StrategyRunError(WHY_QUIET)

    def _retire(self, made: engine_lib.RunEngine, at: int, found: list[str]) -> dict[str, Any]:
        """앱을 껐다 켜서 되살린 모의 실행을 대사 뒤 끝냅니다.

        **어긋나도 끝냅니다.** 어긋난 자리는 한 줄씩 남기지만 「확인 필요」로
        세우지는 않습니다 — 확인 한 번이면 중지가 되고, 그러면 앱을 껐다 켠
        것만으로 「재개」 한 번에 다시 주문이 나갑니다. 계좌를 못 읽었을 때도
        같습니다. **계좌에 남은 것은 잔고 탭에서 봅니다.**
        """
        for why in found:
            made._say(at, engine_lib.RECONCILE, f"{WHY_MISMATCH} {why}")
        with made.lock:
            made.run.restored = False
            made.run.move(runs.END)
        self._after(made, at, WHY_RESTORED_END)
        return self._row(made.run.id, engine=made)

    def _flag(self, made: engine_lib.RunEngine, at: int, found: list[str]) -> dict[str, Any]:
        """어긋난 까닭을 **한 줄씩** 남기고 「확인 필요」로 세웁니다.

        첫 까닭만 남기면 화면에서 무엇이 어긋났는지 한 가지밖에 못 봅니다.
        상태는 한 번만 옮깁니다.
        """
        for why in found:
            made._say(at, engine_lib.RECONCILE, f"{WHY_MISMATCH} {why}")
        with made.lock:
            made.run.move(runs.FLAG)
        self._after(made, at, runs.STATE_LABEL[runs.ATTENTION])
        return self._row(made.run.id, engine=made)

    def _compare(self, made: engine_lib.RunEngine, at: int) -> list[str]:
        """계좌와 장부를 견줍니다. 어긋난 까닭들을 냅니다 — 비면 맞는 것입니다.

        견주는 것은 셋입니다.

            1. 이 앱이 아는 **미체결 주문**이 계좌에도 있는가
            2. 그 주문의 **체결 수량**이 같은가
            3. 그 종목의 **보유 수량**이 같은가, 계좌에 이 앱이 모르는 미체결이 있는가

        **모르는 주문번호는 실행 하나가 아니라 모의 실행 전체를 놓고 봅니다.**
        계좌는 한 벌이고 주문번호도 계좌 단위라, 실행 하나만 놓고 보면 옆
        실행이 낸 주문이 「모르는 주문」이 됩니다.

        끝난 주문은 안 봅니다 — 주문내역은 그날 것이라, 어제 체결된 주문은
        오늘 조회에 없는 것이 정상입니다.
        """
        try:
            held = self._account(made.run)
        except Exception as exc:  # noqa: BLE001 — 못 읽은 것도 답이라 실행을 세웁니다.
            logger.warning("실행 %s 의 대사를 위해 계좌를 읽지 못했습니다.", made.run.id, exc_info=True)
            return [f"{WHY_NO_ACCOUNT} {type(exc).__name__}: {exc}"]

        code = made.run.code
        every = {str(one.get("id") or "") for one in _account_rows(held, "orders")}
        rows = [one for one in _account_rows(held, "orders") if one.get("code") == code]
        spots = [one for one in _account_rows(held, "positions") if one.get("code") == code]
        theirs = {str(one.get("id") or ""): one for one in rows}
        live = {key: one for key, one in theirs.items() if _order_open(one)}

        found: list[str] = []
        gone_cancels: list[Any] = []
        for order in made.book.open_orders:
            no = str(getattr(order, "order_no", "") or "")
            if not no:
                # 주문번호 없는 주문은 접수 자리에서 이미 「확인 필요」입니다.
                continue
            if no not in theirs:
                if getattr(order, "cancel_sent", False) and no not in every:
                    # 취소를 냈고 주문내역 어디에도 없습니다. 보유도 맞으면 아래에서 닫습니다.
                    gone_cancels.append(order)
                else:
                    found.append(WHY_ORDER_GONE.format(no=no))
                continue
            filled = _whole(theirs[no].get("filled")) or 0
            if filled != int(order.filled):
                found.append(
                    WHY_ORDER_FILLED.format(no=no, theirs=filled, ours=int(order.filled))
                )

        known = self._order_numbers(made.run.environment, made.run.market)
        strangers = [key for key in live if key not in known]
        if strangers:
            found.append(WHY_UNKNOWN_ORDERS.format(code=code, count=len(strangers)))

        theirs_qty = sum(_signed(one) for one in spots)
        ours_qty = sum(
            one.ledger.position.quantity
            for one in self.engines()
            # **같은 계좌의 실행만** 셉니다 — 모의와 실전은 다른 계좌입니다.
            if one.run.mode in runs.ACCOUNT_MODES
            and one.run.environment == made.run.environment
            and one.run.code == code
        )
        if theirs_qty != ours_qty:
            found.append(WHY_POSITION.format(theirs=theirs_qty, ours=ours_qty, code=code))
        # 취소를 낸 주문이 계좌에 없으면 취소된 것으로 닫습니다 — 확인 알림이 안 온
        # 경우입니다. **보유가 「그 주문은 안 채워짐」과 맞을 때만**입니다. 취소가 닿기
        # 전에 채워졌는데 주문내역이 늦은 것이면 보유가 어긋나므로 닫지 않고 알립니다.
        for order in gone_cancels:
            if theirs_qty == ours_qty:
                made.close_missing_cancel(order, at)
            else:
                found.append(WHY_ORDER_GONE.format(no=order.order_no))
        return found

    def _order_numbers(self, environment: str, market: str = "domestic") -> set[str]:
        """그 계좌(환경 × 시장)의 실행들이 아는 주문번호 전부. 「모르는 주문」을 가리는 잣대입니다.

        국내와 해외는 다른 계좌라 주문번호도 따로 매겨집니다. 다른 시장 실행의 번호가
        우연히 같아 「아는 주문」으로 섞이지 않게 시장까지 봅니다.
        """
        made: set[str] = set()
        for one in self.engines():
            if (
                one.run.mode not in runs.ACCOUNT_MODES
                or one.run.environment != environment
                or one.run.market != market
            ):
                continue
            for order in one.book.orders:
                no = str(getattr(order, "order_no", "") or "")
                if no:
                    made.add(no)
        return made

    # ── 안쪽 ────────────────────────────────────────────

    def refill(self, run_id: str, at: int = 0) -> int:
        """조회 봉으로 앞을 다시 채웁니다. 채운 봉 수를 냅니다.

        끊겼다 다시 붙으면 **끊긴 동안의 봉을 실시간이 메워 주지 않습니다.**
        실행을 세울 때와 다시 붙을 때 이 자리로 앞을 채우고, 그 뒤로는
        실시간으로 세운 봉을 잇습니다. **시각을 주지 않으면 지금으로 적습니다**
        — 기록에 시각 없는 줄을 남기지 않기 위해서입니다.

        **몇 줄을 받아 몇 개가 늘었는지 실행에 적어 둡니다.** 봉이 0 개일 때
        「조회가 빈 답을 준 것」과 「부르다 막힌 것」을 화면에서 가릴 수 있어야
        합니다 — 저장해 둔 응답으로 도는 중에는 그 기준 봉의 기록이 없어 빈
        답이 오고, 그것은 결함이 아닙니다.
        """
        at = at or _now()
        made = self._engine(run_id)
        minutes = bar_lib.minutes_of(made.bars.base_bar)
        if minutes is None and not made.bars.daily:
            # 목록에 없는 기준 봉입니다. 저장할 때 막는 값이라(`validate.py`)
            # 여기까지 오면 설정이 아니라 보낸 쪽이 잘못된 것입니다.
            made._say(at, engine_lib.READY, f"{WHY_NO_BARS} {made.bars.base_bar}")
            return 0
        try:
            rows = self._fetch(made.run, minutes, made.warmup)
        except Exception as exc:  # noqa: BLE001 — 뒤채움이 안 됐다고 실행을 멈추지 않습니다.
            logger.warning("실행 %s 의 준비 봉을 받지 못했습니다.", run_id, exc_info=True)
            made._say(at, engine_lib.READY, f"{WHY_NO_BARS} {type(exc).__name__}: {exc}")
            return 0
        with made.lock:
            count = len(made.bars.bars)
            before = {one.timestamp for one in made.bars.bars}
            dropped = made.bars.dropped
            read = made.seed(rows, made.run.session)
            after = {one.timestamp for one in made.bars.bars}
            grown = len(made.bars.bars) - count
            unread = made.bars.dropped - dropped
        made._say(
            at,
            engine_lib.READY,
            ready_text(
                rows=len(rows),
                new=len(after - before),
                updated=len(read & before) if before else None,
                held=len(after),
                unread=unread,
            ),
        )
        return grown

    def _stand(self, run: Run, *, tick: float | None = None) -> engine_lib.RunEngine:
        if tick is None:
            tick = self.instrument(run.market, run.code)["tick"]
        made = engine_lib.RunEngine(
            run,
            tick=tick or 1.0,
            record=lambda kind, row: self.journal.write(kind, run, row),
            book=self._broker(run),
        )
        made.feed = IDLE
        made.expiring = _expiring(self.instrument(run.market, run.code)["daysLeft"])
        made.trading_day = trading_day_of(run.market, run.code)
        self._engines[run.id] = made
        if run.id not in self._order:
            self._order.append(run.id)
        return made

    def _broker(self, run: Run) -> Any:
        """그 실행이 주문을 낼 자리. **갈래가 여기서 갈립니다.**

        종이는 이 앱이 지어내고 모의는 계좌가 채웁니다. 갈리는 자리를 하나로
        모아 두면 「어디서 실계좌로 갈 수 있나」를 한 곳에서 답할 수 있습니다.
        """
        if run.mode not in runs.ACCOUNT_MODES:
            return None
        return sim.SimBroker(
            run,
            sender=self._sender,
            environment=self._environment,
            tick=self.instrument(run.market, run.code, run.session)["tick"] or 1.0,
        )

    def _engine(self, run_id: str) -> engine_lib.RunEngine:
        self.load()
        with self._lock:
            made = self._engines.get(run_id)
        if made is None:
            raise StrategyRunError(WHY_NOT_FOUND)
        return made

    def _row(self, run_id: str, engine: engine_lib.RunEngine | None = None) -> dict[str, Any]:
        made = engine if engine is not None else self._engines[run_id]
        facts = self.instrument(made.run.market, made.run.code, made.run.session)
        # 날이 바뀌면 만기까지 남은 날도 바뀝니다. 줄을 낼 때마다 다시 세웁니다.
        made.expiring = _expiring(facts["daysLeft"])
        self._keep_margin(made, facts)
        each = made.per_contract
        used = runs.margin_used(made.ledger, made.open_entry, each)
        budget = made.run.margin_budget
        return runs.row_of(
            made.run,
            made.ledger,
            feed=made.feed,
            feed_at=made.last_trade_at,
            last_price=made.book.last_price,
            lastPrice=made.book.last_price,
            tickSize=made.tick,
            multiplier=made.run.multiplier,
            daysLeft=facts["daysLeft"],
            expiring=_expiring(facts["daysLeft"]),
            # 증거금은 화면이 예산 옆에 그립니다. 계약당 유지증거금은 응답에
            # 없어 늘 비어 나갑니다(`domestic_hours.WHY_NO_MAINTENANCE`).
            initialMarginRate=facts["initialMarginRate"],
            maintenanceMarginRate=facts["maintenanceMarginRate"],
            maintenanceMargin=facts["maintenanceMargin"],
            model=made.model,
            bars=len(made.bars.bars),
            warmup=made.warmup,
            pending=int(getattr(made.book, "pending", 0)),
            # 나가 있는 진입 주문의 남은 수량. 화면이 「사용 증거금 = (보유 +
            # 진입 미체결 + 추가) × 계약당」을 **서버 한도 검사와 같은 값**으로
            # 세는 데 씁니다(`engine.RunEngine.open_entry`).
            openEntry=made.open_entry,
            # 계약 하나에 드는 돈과 **그 값이 어떻게 나온 것인지**. 숫자만
            # 내면 화면이 어림을 확정값으로 읽습니다(`engine.BASIS_*`).
            perContract=each,
            perContractBasis=made.per_contract_basis,
            # 증거금 예산과 지금 묶인 돈·남은 돈. 묶인 돈은 (보유 + 열린 진입) ×
            # 계약당이고 진입 검사와 같은 셈입니다(`runs.margin_used`). 계약당 값을
            # 모르면 둘 다 `None` 이고, 그 까닭은 `perContractBasis`·`budgetUnchecked`
            # 가 말합니다. 예산이 없으면 남은 돈도 `None` 입니다.
            marginBudget=budget,
            marginUsed=used,
            marginLeft=budget - used if budget is not None and used is not None else None,
            # 마지막 1호가와 받은 시각. 시세가 끊긴 동안은 비웁니다.
            **made.quote_now,
            # 지금 가격으로 낼 주문 — 매수·매도 두 방향과 보유를 닫는 청산.
            expectedOrder=made.expected_prices(),
            # 오늘(KST) 규칙이 걸렸는데 주문을 안 낸 수. 이벤트 목록은 잘리므로
            # 화면이 세지 않게 따로 냅니다(`engine.RunEngine.unsent_today`).
            unsentToday=made.unsent_today(_now()),
            # 규칙이 걸린 횟수·체결 줄 수·마지막으로 걸린 시각. 이벤트·체결 목록은
            # 잘리므로 화면이 세지 않게 따로 냅니다.
            **made.stats,
        )

    def _keep_margin(self, made: engine_lib.RunEngine, facts: dict[str, Any]) -> None:
        """계약당 증거금을 실행에 담아 둡니다.

        **시작할 때 못 받아도 뒤에 받으면 채웁니다** — 품목별세부정보는 앱이
        뜬 뒤에 받아 오므로, 그 전에 세운 실행은 처음엔 모릅니다. 줄을 낼
        때마다 보아 값이 생기면 그때 담습니다.

        끝내 모르면 **실행마다 한 번** 까닭을 남깁니다 — 「주문을 내지 않았습니다 —
        증거금을 몰라…」만 되풀이되면 왜 모르는지는 알 수가 없습니다.
        """
        margin = facts.get("margin")
        if margin is not None:
            made.run.margin = margin
            return
        if made.run.margin is not None or made.run.id in self._told_margin:
            return
        self._told_margin.add(made.run.id)
        if made.run.margin_budget is not None and runs.budget_unchecked(made.run):
            made._say(_now(), engine_lib.READY, WHY_BUDGET_UNCHECKED)
            return
        made._say(_now(), engine_lib.READY, f"{WHY_NO_MARGIN} {facts.get('why') or WHY_NO_TABLE}")

    def _resumed(self, made: engine_lib.RunEngine, at: int, text: str, reason: str) -> None:
        """재접속 뒤 대사가 맞아 돌아간 것을 한 줄 적습니다. 코드(`reason`)를 함께 싣습니다."""
        made._say(at, engine_lib.STATE, text, extra={"reason": reason})
        self._save()

    def _after(self, made: engine_lib.RunEngine, at: int, text: str) -> None:
        """실행에 한 줄 적고 담아 둡니다.

        **기록에 따로 쓰지 않습니다.** `_say` 가 엔진의 기록 자리를 거쳐 이미
        파일까지 갑니다 — 여기서 한 번 더 쓰면 같은 일이 두 줄로 남고, 그
        둘의 시각 칸 이름마저 달라집니다(`time` 과 `at`).
        """
        made._say(at, engine_lib.STATE, text)
        self._save()

    def _save(self) -> None:
        try:
            self.store.save([one.run for one in self._engines.values()])
        except OSError as exc:
            logger.warning("검증 실행을 담지 못했습니다: %s", exc)

    def _products(self) -> list[dict[str, Any]]:
        """국내 품목별세부정보의 줄들. 받아 둔 것이 없으면 빈 목록입니다."""
        try:
            held = self._hours.cached(session_service.DOMESTIC)
        except Exception as exc:  # noqa: BLE001 — 시간표가 없다고 실행을 못 세울 것은 아닙니다.
            # **한 번만 남깁니다.** 줄을 낼 때마다 부르는 자리라(박자에 열 번)
            # 그대로 두면 같은 줄이 기록을 덮습니다. 결과는 틱·증거금을
            # 「모름」으로 두는 것이고, 그것은 실행 줄에 그대로 보입니다.
            if not self._told_hours:
                self._told_hours = True
                logger.warning("국내 품목별세부정보를 읽지 못해 틱·증거금을 모릅니다: %s", exc)
            return []
        rows = (held or {}).get("products")
        return [one for one in rows if isinstance(one, dict)] if isinstance(rows, list) else []


def _fetch_bars(run: Run, minutes: int | None, count: int) -> list[dict[str, Any]]:
    """준비 봉을 받아 옵니다. **시장과 봉 길이에 맞는 차트 함수**를 씁니다.

    `minutes` 가 `None` 이면 일봉입니다 — 분봉과 다른 함수를 부릅니다.

    **화면이 쓰는 차트와 같은 함수**입니다 — 두 벌로 두면 엔진이 본 봉과
    차트에 그려진 봉이 갈립니다.
    """
    if minutes is None:
        return _fetch_daily(run, count)
    start, end = bar_lib.minute_span(count, minutes)
    if run.market == validate.OVERSEAS:
        from backend.overseas_futureoption import overseas_futureoption_functions as api

        # **해외는 인자 이름이 다릅니다.** 거래소는 받지 않으면 부를 수 없는
        # 값이라, 없으면 부르지 않고 무엇이 없는지 말합니다 — 그냥 부르면
        # `TypeError` 가 나고 그것은 「왜 봉이 없나」를 안 알려 줍니다.
        if not run.exchange:
            raise ValueError(f"{run.code} 의 거래소를 몰라 해외 분봉을 부를 수 없습니다.")
        raw = api.chart_minute(
            run.code,
            exch_cd=run.exchange,
            inq_strt_dt=start,
            inq_ed_dt=end,
            dcnt=str(minutes),
        )
    else:
        from backend.domestic_futureoption import domestic_futureoption_functions as api

        # 국내는 주간·야간을 인자로 가릅니다. 해외에는 그 칸이 없습니다.
        raw = api.chart_minute(
            run.code,
            start_date=start,
            end_date=end,
            minutes=str(minutes),
            night=run.session == bar_lib.NIGHT_CONTEXT,
        )
    return _rows(raw, run, minute=True)


def _fetch_daily(run: Run, count: int) -> list[dict[str, Any]]:
    """일봉 조회로 준비 봉을 받아 옵니다.

    **국내 일봉에는 주간·야간 인자가 있습니다.** 「일봉 = 거래일 하나」이고
    야간은 다음 거래일에 속하므로, 야간으로 도는 실행도 **주간 일봉**을
    청합니다 — 야간 일봉은 그 창의 값만 담은 다른 줄입니다.
    """
    start, end = bar_lib.day_span(count)
    if run.market == validate.OVERSEAS:
        from backend.overseas_futureoption import overseas_futureoption_functions as api

        if not run.exchange:
            raise ValueError(f"{run.code} 의 거래소를 몰라 해외 일봉을 부를 수 없습니다.")
        raw = api.chart_daily(run.code, exch_cd=run.exchange, inq_strt_dt=start, inq_ed_dt=end)
    else:
        from backend.domestic_futureoption import domestic_futureoption_functions as api

        raw = api.chart_daily(run.code, start_date=start, end_date=end)
    return _rows(raw, run, minute=False)


def _rows(raw: dict[str, Any], run: Run, *, minute: bool) -> list[dict[str, Any]]:
    """차트 응답에서 봉 줄만. 모양이 다르면 빈 목록입니다.

    **`minute` 을 맞춰 주어야 합니다.** 분봉 줄은 시각 칸을 함께 읽고 일봉
    줄에는 그 칸이 없어, 일봉을 분봉으로 읽으면 **모든 줄이 버려집니다.**
    """
    made = quotes.bars(raw, minute=minute, asked=run.code)
    rows = made.get("bars")
    return [one for one in rows if isinstance(one, dict)] if isinstance(rows, list) else []


def _look_outcome(run: Run, order: Any) -> dict[str, Any] | None:
    """계좌 주문 하나의 접수 뒤 결과를 **실행이 선 환경으로** 한 번 조회합니다.

    화면 주문과 같은 판정입니다(`order_outcome.look`). 전략은 신규 주문만 이
    길로 확인합니다 — 취소는 확인 알림과 대사가 닫습니다.
    """
    return order_outcome.look(run.market, order.order_no, environment=run.environment)


def _fetch_account(run: Run) -> dict[str, Any]:
    """대사에 쓰는 계좌 두 벌 — 주문내역과 잔고내역.

    **화면과 같은 함수로 읽습니다**(`domestic_account`) — 조회 응답을 푸는
    규칙이 두 벌이 되면, 화면이 보는 미체결과 대사가 보는 미체결이 달라집니다.

    주문내역은 **그 실행의 창**(주간·야간)만 묻습니다. 한 계좌의 주간 줄과
    야간 줄이 한 응답에 섞여 올 수 있어, 창을 안 좁히면 야간 주문이 주간
    실행의 「모르는 주문」이 됩니다.

    해외 실행은 해외 주문내역·잔고를 **다음 쪽까지 다** 묻습니다(`_fetch_overseas`).

    **실행이 선 환경의 계좌를** 묻습니다(`sim._environment_of`). 그 순간의 활성
    환경으로 물으면, 환경이 바뀐 사이에 다른 계좌를 대고 견주게 됩니다.
    """
    if run.market == validate.OVERSEAS:
        return _fetch_overseas(run)
    from backend.domestic_futureoption import domestic_account
    from backend.domestic_futureoption import domestic_futureoption_functions as api

    environment = sim._environment_of(run)
    window = api.NIGHT_SESSION if run.session == "domestic-night" else api.DAY_SESSION
    orders = domestic_account.order_history(
        api.order_history(trd_tm_div_cd=window, environment=environment)
    )
    positions = domestic_account.positions(api.open_interest(environment=environment))
    return {"orders": orders.get("orders"), "positions": positions.get("positions")}


#: 해외 조회를 다음 쪽까지 이어 부르는 상한. 끝없이 이어지는 키를 막습니다.
_OVERSEAS_PAGES = 20


def _fetch_overseas(run: Run) -> dict[str, Any]:
    """해외 계좌 두 벌 — 주문내역과 잔고내역. 화면과 같은 함수로 풉니다(`overseas_account`)."""
    from backend.overseas_futureoption import overseas_account
    from backend.overseas_futureoption import overseas_futureoption_functions as api

    environment = sim._environment_of(run)
    orders: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    following = ""
    for _page in range(_OVERSEAS_PAGES):
        got = overseas_account.order_history(api.order_history(next_key=following, environment=environment))
        orders.extend(got.get("orders") or [])
        following = str(got.get("nextKey") or "")
        if not following:
            break
    following = ""
    for _page in range(_OVERSEAS_PAGES):
        got = overseas_account.positions(api.open_interest(next_key=following, environment=environment))
        positions.extend(got.get("positions") or [])
        following = str(got.get("nextKey") or "")
        if not following:
            break
    for one in orders:
        one.setdefault("market", validate.OVERSEAS)
    return {"orders": orders, "positions": positions}


def _left_in(made: engine_lib.RunEngine) -> list[str]:
    """이 실행의 장부에 남은 것을 한 마디씩. **비면 이 실행 몫으로 계좌에 남는 것이 없습니다.**

    보유, 열린 주문(번호와 함께), 그 가운데 취소 확인을 기다리는 수입니다.
    번호는 계좌가 준 주문번호이고, 종이 실행의 주문은 계좌 번호가 없어 앱이 붙인
    주문 id 를 적습니다.
    """
    made_list: list[str] = []
    held = made.ledger.position
    if held.side is not None:
        side = "매수" if held.side == paper.BUY else "매도"
        made_list.append(f"보유 {side} {held.size}계약")
    orders = list(made.book.open_orders)
    if orders:
        numbers = [str(getattr(one, "order_no", "") or one.id) for one in orders]
        made_list.append(f"미체결 주문 {len(orders)}건({', '.join(numbers)})")
    pending = int(getattr(made.book, "pending", 0) or 0)
    if pending:
        made_list.append(f"그 가운데 취소 확인 대기 {pending}건")
    return made_list


def _account_rows(held: Any, key: str) -> list[dict[str, Any]]:
    """계좌 답에서 줄들만. 모양이 아니면 빈 목록입니다."""
    rows = held.get(key) if isinstance(held, dict) else None
    return [one for one in rows if isinstance(one, dict)] if isinstance(rows, list) else []


def _order_open(row: dict[str, Any]) -> bool:
    """계좌의 주문 줄이 아직 살아 있는지.

    **잔량이 0 이면 끝난 주문입니다** — 상태 코드보다 먼저 봅니다. 취소가
    확인된 주문은 주문상태가 「3 확인」으로 남는데, 「3」은 아직 살아 있는
    주문에도 붙는 값이라 상태만으로는 둘을 가를 수 없습니다. 취소한 뒤에는
    원주문 줄과 취소 주문 줄이 둘 다 잔량 0 으로 남으므로 둘 다 끝난 것으로
    셉니다.
    잔량 칸이 없거나 숫자로 못 읽은 줄은 상태 코드로만 판정합니다.

    **끝난 상태의 표는 조회를 푸는 자리 것을 그대로 씁니다** — 같은 이름의 칸이
    API 마다 다른 뜻이던 전례가 있어, 여기서 표를 다시 적지 않습니다.
    """
    remaining = row.get("remaining")
    if isinstance(remaining, (int, float)) and not isinstance(remaining, bool) and remaining <= 0:
        return False
    if str(row.get("market") or "") == validate.OVERSEAS:
        from backend.overseas_futureoption import overseas_account

        return str(row.get("statusCode") or "") not in overseas_account.FINISHED_STATUS
    from backend.domestic_futureoption import domestic_account

    return str(row.get("statusCode") or "") not in domestic_account.FINISHED_STATUS


def _signed(row: dict[str, Any]) -> int:
    """잔고 줄의 보유를 부호 있는 수량으로. 매도 보유는 음수입니다.

    장부의 `Position.quantity` 와 **같은 부호 규칙**이라 그대로 견줍니다.
    """
    quantity = _whole(row.get("quantity")) or 0
    return -quantity if str(row.get("side") or "") == "short" else quantity


def timetable_unknown(market: str, code: str, *, judged: Any = None) -> bool:
    """그 종목의 장 시간표를 모르는지. **세션 판정이 「모름」이라 답하면** 참입니다.

    시간표를 아직 받지 못했거나 그 품목 줄이 없을 때입니다. 「지금 거래 창이 아님」과
    다릅니다 — 그때는 시간표를 알고, 창이 열리면 끝 시각도 압니다. 실행을 세울 때의
    검사와 실행 중 세션 끝 정리(`pump`)가 이 판정 하나를 씁니다.
    """
    from backend.sessions import judge

    if judged is None:
        from backend.sessions.sessions_routes import judged
    made = judged(market, code, now_kst())
    return str((made or {}).get("source") or "") == judge.UNKNOWN


def _now() -> int:
    """지금(밀리초). 기록의 시각 칸은 **한 이름으로만** 씁니다 — `time` 입니다."""
    return int(now_kst().timestamp() * 1000)


#: 장부를 못 읽으면 못 채우는 칸. **`bare_row` 가 비우는 자리**입니다.
#:
#: 앞엣것들은 보유·손익이라 시세와 체결이 있어야 알고, 뒤엣것들은 종목 색인과
#: 장부가 있어야 압니다. 「모른다」와 「0」은 다른 말이라 `None` 으로 냅니다.
_UNKNOWN = (
    "position",
    "side",
    "avgPrice",
    "pl",
    "dayPl",
    "lossStreak",
    "fee",
    "lastPrice",
    "tickSize",
    "multiplier",
    "daysLeft",
    "expiring",
    "initialMarginRate",
    "maintenanceMarginRate",
    "maintenanceMargin",
    "bars",
    "warmup",
    "pending",
    "openEntry",
    "perContract",
    "perContractBasis",
    "marginBudget",
    "marginUsed",
    "marginLeft",
    "bid1",
    "ask1",
    "bid1Qty",
    "ask1Qty",
    "quoteAt",
    "expectedOrder",
    "unsentToday",
    "realizedPl",
    "openPl",
    "signalCount",
    "fillCount",
    "lastSignalAt",
)


def trading_day_of(market: str, code: str) -> Callable[[int], Any] | None:
    """봉 시각(밀리초) → 거래일을 답하는 함수. 해외만이고 국내는 `None` 입니다.

    해외 세션은 한국 시각 자정을 넘기는 것이 많아 달력 날짜로는 한 세션이 둘로
    갈립니다. 화면이 쓰는 세션 판정의 시간표로 그 봉이 든 거래 창의 날을 씁니다.
    """
    if market != validate.OVERSEAS:
        return None

    def find(timestamp: int) -> Any:
        from backend.sessions.sessions_routes import trading_day

        return trading_day(market, code, datetime.fromtimestamp(timestamp / 1000, tz=KST))

    return find


def overseas_multiplier(row: dict[str, Any]) -> float | None:
    """해외 종목의 승수(1포인트가 몇 통화인지). 모르면 `None` 입니다.

    해외 마스터에는 계약크기(`ctrt_size`) 칸이 비어 있어, 틱값(`tval`, 가격변동폭)을
    호가단위(`tsiz`)로 나눠 셉니다 — 마이크로 S&P 는 1.25 ÷ 0.25 = 5 입니다. 계약크기가
    있으면 그것이 먼저입니다. 둘 중 하나라도 없거나 0 이면 셀 수 없습니다.
    """
    given = _number(row.get("ctrt_size"))
    if given:
        return given
    value = _number(row.get("tval"))
    step = _number(row.get("tsiz"))
    if not value or not step:
        return None
    # 십진으로 나눕니다 — 부동소수로 나누면 12.5 ÷ 0.0001 이 125000.00000000001 이 됩니다.
    from decimal import Decimal

    return float((Decimal(str(value)) / Decimal(str(step))).normalize())


def runnable_problems(draft: dict[str, Any]) -> list[dict[str, str]]:
    """그 설정으로 엔진이 **돌 수 있는지**. 실행 시작과 지난 봉 셈이 같이 봅니다.

    모형이 못 다루는 주문유형과, 켜진 진입 규칙이 없거나 조건이 없는 설정을
    막습니다. 저장은 이 둘을 받아 주므로(`validate.py` 의 `warning`) 따로 봅니다.
    """
    found: list[dict[str, str]] = []
    aux = draft.get("aux") if isinstance(draft.get("aux"), dict) else {}
    rules = draft.get("rules") if isinstance(draft.get("rules"), dict) else {}

    if not paper.supported(str(aux.get("orderType") or "")):
        found.append(_one("aux.orderType", paper.unsupported_why(str(aux.get("orderType") or ""))))
    # **켜져 있는 것만으로는 못 돕니다.** 조건이 하나도 없는 규칙은 켜
    # 두어도 영영 발동하지 않아, 그대로 시작하면 「감시 중」으로 선 채
    # 아무 일도 일어나지 않습니다. 그래서 판정이 보는 자리를 그대로
    # 봅니다(`rules.steps_of` — 꺼져 있거나 조건이 없으면 빈 목록).
    on = [
        key
        for key in validate.ENTRY_KEYS
        if isinstance(rules.get(key), dict) and rules[key].get("on")
    ]
    if not on:
        found.append(_one("rules", validate.NO_ENTRY_START))
    elif not any(rule_lib.steps_of(rules[key]) for key in on):
        # 칸 이름은 **켜 둔 규칙**입니다. 조건을 적을 자리가 그 탭이라,
        # 여기에 「진입 규칙이 없어」를 세우면 이미 켜 둔 규칙을 또 켜러
        # 갑니다. 그런 규칙이 둘이면 앞엣것을 듭니다.
        found.append(_one(f"rules.{on[0]}", validate.NO_CONDS_START))
    return found


def qty_problems(qty: Any, aux: dict[str, Any]) -> list[dict[str, str]]:
    """주문당 수량을 막을 까닭. 실행 시작과 백테스트가 함께 씁니다.

    1 이상의 정수여야 하고, 최대 보유를 넘으면 안 됩니다 — 넘으면 첫 주문부터 한도를
    넘어, 실행은 진입을 못 하고 백테스트는 거래가 0 으로 나옵니다.
    """
    wanted = _whole(qty)
    hold = _whole(aux.get("maxHold"))
    if wanted is None or wanted < 1 or (hold is not None and wanted > hold):
        return [_one("qty", WHY_QTY)]
    return []


def _one(field: str, message: str) -> dict[str, str]:
    """저장 검사와 같은 모양의 줄 하나. 시작을 막는 것은 다 `error` 입니다."""
    return {"field": field, "severity": validate.ERROR, "message": message}


def _whole(value: Any) -> int | None:
    number = _number(value)
    if number is None or number != int(number):
        return None
    return int(number)


def _expiring(days_left: int | None) -> bool:
    """만기 당일인지. 모르면 막지 않습니다 — 모른다고 못 하게 할 수는 없습니다."""
    return days_left is not None and days_left <= EXPIRY_DAY


def _number(value: Any) -> float | None:
    # 유한한 보통 십진수만 — `nan`·`inf`·지수는 숫자가 아닌 것과 같은 갈래입니다.
    return market_values.plain_number(value)


def _group_or_blank(groups: Any, version_id: str) -> str:
    """그 버전을 묶는 전략 id. 못 찾으면 빈 글자입니다 — 실행을 세우는 일을
    막을 까닭이 아니라, 기록을 읽을 때 버전으로 다시 찾습니다."""
    try:
        return str(groups(version_id) or "")
    except Exception:  # noqa: BLE001 — 묶음을 못 찾은 것이 실행을 막을 까닭은 아닙니다.
        logger.warning("버전 %s 의 전략 묶음을 찾지 못했습니다.", version_id, exc_info=True)
        return ""


def _market_or_blank(markets: Any, version_id: str) -> str:
    """그 버전의 시장. 못 찾거나 저장소를 못 읽으면 빈 글자입니다(실행을 세우지 않음)."""
    try:
        return str(markets(version_id) or "")
    except Exception:  # noqa: BLE001 — 못 읽은 것은 「없는 버전」과 같이 다룹니다.
        logger.warning("버전 %s 의 시장을 찾지 못했습니다.", version_id, exc_info=True)
        return ""


def _market_from_store(version_id: str) -> str:
    """전략 저장소에서 그 버전 줄의 `market`."""
    from backend.strategy.store import store as strategy_store

    for row in strategy_store.read().get("versions") or []:
        if isinstance(row, dict) and row.get("id") == version_id:
            return str(row.get("market") or "")
    return ""


def _group_from_store(version_id: str) -> str:
    """전략 저장소에서 그 버전 줄의 `groupId`."""
    from backend.strategy.store import store as strategy_store

    for row in strategy_store.read().get("versions") or []:
        if isinstance(row, dict) and row.get("id") == version_id:
            return str(row.get("groupId") or "")
    return ""


#: 앱이 쓰는 실행 목록. 테스트는 제 것을 만들어 씁니다.
service = RunService(groups=_group_from_store, markets=_market_from_store)
