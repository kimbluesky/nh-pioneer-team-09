"""계좌(모의·실전)로 나가는 주문.

종이 장부(`paper.py`)는 「그때 냈으면 어떻게 채워졌을까」를 이 앱이 지어냅니다.
여기서는 **실제로 냅니다** — 실행이 선 계좌로. 채우는 것은 계좌이고 이 앱은 받아
적습니다. 그래서 같은 자리를 두 벌로 두되 **엔진이 보는 얼굴은 하나**입니다
(`place`·`cancel`·`open_orders`·`last_price`·`gap`).

**묶음 취소는 여기 없습니다.** 종이 장부에는 열린 주문을 한꺼번에 거두는 자리가
있지만(`paper.PaperBook.cancel_open`, 세션 마감에서 부릅니다) 계좌 갈래에는 두지
않습니다 — 그 취소는 계좌로 나가는 요청이고, **한 줄로 전부 거두는 자리가 있으면
갈래를 안 가린 한 줄이 그대로 계좌를 칩니다.** 거둘 것은 주문마다 고릅니다
(`service._pause`).

## 보내기 전에 넷을 봅니다

    1. 환경을 바꾸는 중이면 안 보냅니다.
    2. 활성 환경을 못 읽으면 안 보냅니다.
    3. **실행이 선 환경**(`Run.environment`)과 활성 환경이 다르면 안 보냅니다.
       모의에서 세운 실행이 실전 계좌로, 실전에서 세운 실행이 모의 계좌로
       가는 길을 여기서 막습니다.
    4. 실행에 **사용자 승인 표지**(`approved_at`)가 있어야 합니다. 엔진은 사람이
       단추를 누르지 않아도 주문을 내는 자리라, 그 실행을 사람이 한 번 봤다는
       표지가 없으면 첫 건부터 안 냅니다.

앞의 셋은 일반 주문과 **같은 판정**입니다(`orders.refusal_of`). 넷 다 **주문을
만들기 전에** 보고, 걸리면 한 건도 안 보내고 「확인 필요」로 갑니다.

## 보낼 때는 실행의 환경을 적습니다

주문·취소는 **실행이 선 환경을 인자로 적어** 부릅니다(`NhSender`). 적지 않으면
그 순간의 활성 환경으로 나가는데, 검사와 전송 사이에 환경이 바뀌면 다른 계좌로
갈 수 있습니다.

## 계좌가 답을 안 주면 멈춥니다

접수 답에 주문번호가 없거나 답 자체를 못 받으면 **다시 보내지 않습니다**(재전송
금지). 같은 주문이 둘 나가는 것보다 한 건이 확인 안 된 채로 남는 편이 낫습니다. 그때는 실행이 「확인 필요」로 가고 사용자가 풀어야 합니다.

**계좌가 거부한 것은 답을 받은 것입니다.** 서버가 코드와 문구로 주문을 받지
않겠다고 답하면(`rejection_of`) 계좌에는 아무것도 남지 않았으므로 「결과를 받지
못했습니다」로 적지 않고, 사건 글에 그 코드와 서버 문구를 그대로 싣습니다.

**보내기 전에 앱이 막은 것은 거부(앱 검사)입니다**(`NotSent`). 주문유형을
계좌가 받는 코드로 못 옮기거나 실행이 선 환경을 모르면 아무것도 나가지 않습니다.

## 접수 뒤의 거절도 잡습니다

접수 답(주문번호)을 받은 주문도 계좌가 뒤에 거절할 수 있습니다. 화면 주문과
같은 확인(`backend/order_outcome.py`)을 **펌프가 루프 밖에서** 합니다 — 접수 뒤
정해 둔 시간이 지난 주문을 `confirm_wanted` 로 내주면, 조회한 결과를 엔진이
받습니다(`engine.RunEngine.on_outcome`). 거절이면 그 주문을 거절로 닫습니다.
실시간 알림이 먼저 거절을 알려 주면 그쪽으로 닫히고, 확인은 할 일이 없어집니다.

## 취소는 요청과 확인이 갈립니다

종이는 취소가 즉시지만 계좌는 요청을 받고 나중에 답합니다. 그래서 취소를 낸
주문은 **아직 열려 있고**(`pending`) 확인이 와야 닫힙니다 — 거부된 취소를
끝난 것으로 적으면 화면에 없는 주문이 계좌에 남습니다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from backend import order_outcome
from backend import orders as orders_lib
from decimal import ROUND_HALF_UP, Decimal

from backend.nh_api import error_codes
from backend.nh_api.errors import BusinessError, GatewayError, HttpError, paper_unavailable
from backend.orders import order_no as padded_order_no
from backend.sessions.hours import now_kst
from backend.strategy import runs
from backend.strategy.paper import CANCELLED, FILLED, OPEN_STATUS, PARTIAL, WAITING, fill_no

logger = logging.getLogger(__name__)

#: 모형 이름. 장부에 적어 두어 종이 성적과 섞이지 않게 합니다.
MODEL = "sim-v1"

#: 접수 답에 주문번호가 없을 때. 다시 보내지 않고 확인을 청합니다.
WHY_NO_ORDER_NO = "주문은 나갔는데 주문번호를 받지 못했습니다"

#: 답 자체를 못 받았을 때.
WHY_NO_REPLY = "주문 결과를 받지 못했습니다"

#: 계좌가 주문을 받지 않겠다고 답했을 때의 글 머리. 뒤에 코드와 서버 문구가 붙습니다.
WHY_REJECTED = "주문 거부"

#: 계좌가 거부했다는 까닭 코드. 실행 사건 줄의 `reason` 칸에 실립니다.
REJECTED = "rejected"

#: 취소를 냈지만 아직 확인이 안 온 주문.
PENDING = "pending"

#: 보내기 전에 앱이 막은 주문의 까닭 코드와 글 머리. 계좌로는 아무것도 나가지 않습니다.
NOT_SENT = "app-check"
WHY_NOT_SENT = "거부(앱 검사)"


class NotSent(ValueError):
    """보내기 전에 앱이 막은 경우입니다. **계좌로는 아무것도 나가지 않습니다.**

    다른 실패와 갈라 두는 것은, 같은 값으로 다시 내도 같은 자리에서 막히는데
    「결과를 받지 못했습니다」로 적으면 나갔을지 모르는 주문처럼 읽히기 때문입니다.
    """


class SimRefused(Exception):
    """주문을 내지 못한 경우입니다. `flag` 가 참이면 「확인 필요」로 가야 합니다.

    `reason` 은 까닭의 코드입니다. 실행 사건 줄의 `reason` 칸에 그대로 실립니다.
    계좌가 거부한 것(`REJECTED`)이면 `code`·`detail` 에 서버가 준 코드와 문구가
    있습니다.
    """

    def __init__(
        self,
        message: str,
        *,
        flag: bool = True,
        reason: str = "other",
        code: str = "",
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.flag = flag
        self.reason = reason
        self.code = code
        self.detail = detail


def rejection_of(exc: BaseException) -> tuple[str, str] | None:
    """계좌가 **주문을 받지 않았다고 분명히 답한** 실패면 (코드, 서버 문구), 아니면 `None`.

    주문 화면이 거절과 「결과를 모름」을 가르는 기준과 같습니다.

    - 업무 거부(`BusinessError`)는 서버가 주문을 보고 안 된다고 한 것입니다.
    - 게이트웨이 거절(`GatewayError`) 가운데 「모의투자에서는 제공하지 않는 API」
      는 처리되지 않은 것이 확실합니다.
    - HTTP 4xx(`HttpError`)는 요청이 문 앞에서 거절된 것입니다.

    그 밖 — 응답을 못 받음, 그 밖의 게이트웨이 거절, HTTP 5xx — 는 주문이 접수됐는지
    알 수 없는 자리라 거부로 적지 않습니다. 거부로 적으면 이미 나간 주문을 안
    나갔다고 말하게 됩니다.
    """
    if isinstance(exc, BusinessError) or (isinstance(exc, GatewayError) and paper_unavailable(exc)):
        code = str(exc.code or "").strip()
        said = str(exc.detail or "").strip() or error_codes.message_of(code)
        return code, said
    if isinstance(exc, HttpError) and 400 <= int(exc.status) < 500:
        body = exc.body if isinstance(exc.body, dict) else {}
        said = str(body.get("errorMessage") or "").strip()
        return f"HTTP {exc.status}", said
    return None


def rejected_text(code: str, detail: str) -> str:
    """거부 사건의 글. 예: `주문 거부 5039 — 처리불가 [시장가는 FOK, FAK 주문불가입니다 ]`."""
    head = f"{WHY_REJECTED} {code}".strip()
    return f"{head} — {detail}" if detail else head


@dataclass
class SimOrder:
    """계좌에 낸 주문 하나. 채워지는 것은 계좌가 알려 줍니다."""

    id: str
    side: str
    quantity: int
    order_type: str
    price: float | None = None
    #: 스탑 가격. STOP 주문에만 있습니다. **계좌로 함께 나간 값**이라 이 장부도
    #: 들고 있습니다 — 안 적어 두면 화면이 걸어 둔 주문의 발동가를 못 읽습니다.
    stop_price: float | None = None
    fill_condition: str = ""
    created: int = 0
    #: 낼 때의 틱. 해외 취소는 원주문의 가격을 다시 실어 보내야 해 같은 글자로 적습니다.
    tick: float = 0.0
    #: 계좌가 준 주문번호. 알림을 이 값으로 갈라 넣습니다.
    order_no: str = ""
    filled: int = 0
    status: str = WAITING
    reason: str = ""
    #: 취소를 냈는지. 확인이 오기 전까지 주문은 아직 열려 있습니다.
    cancel_sent: bool = False
    #: 무엇이 이 주문을 닫았는지. 계좌 알림이 아니라 대사가 닫았으면 `reconcile` 입니다.
    closed_by: str = ""
    fills: list[dict[str, Any]] = field(default_factory=list)
    #: 계좌 알림이 준 상태 글자 원문(`rejected`·`cancelled` 같은 것). 주문의
    #: 상태(`status`)는 거절과 취소를 한 갈래(취소)로 닫지만, 기록에는 계좌가
    #: 무엇이라고 답했는지를 그대로 남깁니다.
    last_status: str = ""
    #: 접수 뒤 거절로 닫혔을 때 계좌가 준 코드와 문구. 알림으로 닫혔으면 비어 있습니다
    #: — 실시간 알림에는 거절 문구 칸이 없습니다.
    server_code: str = ""
    server_message: str = ""
    #: 접수 뒤 결과를 다음에 확인할 시각(밀리초). 확인할 것이 없으면 `None` 입니다.
    confirm_due: int | None = None
    #: 아직 남은 확인 횟수.
    confirm_left: int = 0

    @property
    def remaining(self) -> int:
        return max(self.quantity - self.filled, 0)

    @property
    def open(self) -> bool:
        return self.status in OPEN_STATUS

    @property
    def pending(self) -> bool:
        """취소를 냈는데 아직 확인이 안 온 주문."""
        return self.cancel_sent and self.open


class SimBroker:
    """실행 하나가 계좌(모의·실전)에 내는 주문들.

    보내는 일은 `sender` 가 집니다 — 바꿔 낄 수 있게 둔 것은, 시험이 계좌에
    닿지 않고도 이 자리를 다 지나갈 수 있어야 하기 때문입니다.
    """

    def __init__(self, run: runs.Run, *, sender: Any, environment: Any, tick: float = 1.0) -> None:
        self.run = run
        self.sender = sender
        #: 그 종목의 틱. 주문가격을 **틱 자릿수만큼** 소수로 적는 데 씁니다.
        self.tick = abs(float(tick)) or 1.0
        #: 지금 활성 환경을 답하는 자리. 주문을 만들기 **전에** 봅니다.
        self.environment = environment
        self.orders: list[SimOrder] = []
        #: 마지막 체결가. 종이와 같은 자리입니다 — 손절·익절이 보는 값입니다.
        self.last_price: float | None = None
        #: 시세가 끊겼는지. 종이와 달리 채우는 것은 계좌라 값만 따라갑니다 —
        #: **공백 중에도 계좌 알림은 받습니다**(`on_order`).
        self.gap = False

    # ── 보기 ────────────────────────────────────────────

    @property
    def open_orders(self) -> list[SimOrder]:
        return [one for one in self.orders if one.open]

    @property
    def pending(self) -> int:
        """취소를 냈는데 확인이 안 온 주문 수. **종료를 막는 값**입니다."""
        return len([one for one in self.orders if one.pending])

    # ── 넣기 ────────────────────────────────────────────

    def place(
        self,
        order_id: str,
        *,
        side: str,
        quantity: int,
        order_type: str,
        price: float | None = None,
        stop_price: float | None = None,
        fill_condition: str = "",
        at: int,
    ) -> SimOrder:
        """주문 하나를 실행이 선 계좌로 냅니다.

        Raises:
            SimRefused: 환경·승인이 안 맞거나 계좌가 답을 안 준 경우.
        """
        self._allowed()
        made = SimOrder(
            id=order_id,
            side=side,
            quantity=max(int(quantity), 0),
            order_type=str(order_type or "").strip(),
            price=price,
            stop_price=stop_price,
            fill_condition=str(fill_condition or "").strip(),
            created=int(at),
            tick=self.tick,
        )
        try:
            reply = self.sender.order(
                run=self.run,
                side=side,
                quantity=made.quantity,
                order_type=made.order_type,
                price=price,
                stop_price=stop_price,
                fill_condition=made.fill_condition,
                tick=self.tick,
            )
        except NotSent as exc:
            # 아무것도 나가지 않았습니다. 계좌와 장부가 어긋날 자리가 없어 「확인
            # 필요」로 세우지 않고, 실행을 어디로 옮길지는 엔진이 정합니다.
            logger.info("보내기 전에 막은 주문입니다: %s", exc)
            raise SimRefused(f"{WHY_NOT_SENT} — {exc}", flag=False, reason=NOT_SENT) from exc
        except Exception as exc:  # noqa: BLE001 — 답을 못 받은 것도 하나의 결과입니다.
            refused = rejection_of(exc)
            if refused is not None:
                # 계좌가 받지 않은 것이 확실합니다. 계좌와 장부가 어긋날 자리가
                # 없으므로 「확인 필요」로 세우지 않습니다(`flag=False`). 실행을
                # 어디로 옮길지는 엔진이 주문의 목적을 보고 정합니다.
                code, detail = refused
                logger.info("계좌가 주문을 거부했습니다: %s %s", code, detail)
                raise SimRefused(
                    rejected_text(code, detail),
                    flag=False,
                    reason=REJECTED,
                    code=code,
                    detail=detail,
                ) from exc
            logger.warning("계좌 주문을 내지 못했습니다: %s", exc, exc_info=True)
            raise SimRefused(f"{WHY_NO_REPLY} — {type(exc).__name__}: {exc}", reason="no-reply") from exc

        given = str((reply or {}).get("id") or "").strip()
        if not given:
            # **다시 보내지 않습니다**(재전송 금지) — 나갔는지 모르는 주문을
            # 한 번 더 내면 같은 자리에 둘이 들어갑니다.
            raise SimRefused(WHY_NO_ORDER_NO, reason="no-order-no")

        made.order_no = given
        # 접수 뒤의 거절을 확인할 차례를 잡아 둡니다. 확인은 펌프가 루프 밖에서 합니다.
        waits = order_outcome.WAITS
        made.confirm_left = len(waits)
        made.confirm_due = int(at) + int(waits[0] * 1000) if waits else None
        self.orders.append(made)
        return made

    def confirm_wanted(self, now: int) -> list[SimOrder]:
        """접수 뒤 결과를 확인할 때가 된 주문들. 닫혔거나 취소를 낸 주문은 빼고 확인을 접습니다."""
        made: list[SimOrder] = []
        for one in self.orders:
            if one.confirm_due is None:
                continue
            if not one.open or one.cancel_sent:
                # 알림이 이미 닫았거나 이 앱이 거두는 중입니다. 결과는 그쪽이 정합니다.
                one.confirm_due = None
                continue
            if one.confirm_due <= now:
                made.append(one)
        return made

    def confirmed(self, order: SimOrder, outcome: dict[str, Any] | None, *, at: int) -> bool:
        """확인 한 번의 결과를 받습니다. **거절로 닫았으면 참**입니다.

        못 정했으면(`None`·`unknown`) 다음 확인을 잡고, 남은 확인이 없으면 접습니다
        — 그 뒤에는 실시간 알림과 대사가 결과를 정합니다.
        """
        state = (outcome or {}).get("state")
        if state == order_outcome.REJECTED and order.open:
            order.confirm_due = None
            order.server_code = str(outcome.get("code") or "")
            order.server_message = str(outcome.get("message") or "")
            order.reason = rejected_text(order.server_code, order.server_message)
            self._close_cancelled(order, "rejected")
            return True
        if state == order_outcome.ACCEPTED:
            order.confirm_due = None
            return False
        order.confirm_left = max(order.confirm_left - 1, 0)
        waits = order_outcome.WAITS
        if order.confirm_left and waits:
            step = waits[len(waits) - order.confirm_left]
            order.confirm_due = int(at) + int(step * 1000)
        else:
            order.confirm_due = None
        return False

    def cancel(self, order: SimOrder, reason: str) -> bool:
        """주문 하나에 **취소를 냅니다.** 닫는 것은 확인이 왔을 때입니다.

        종이는 이 자리에서 바로 닫지만(`paper.PaperBook.cancel`) 계좌는 요청을
        받고 나중에 답합니다. 거부된 취소를 끝난 것으로 적으면 화면에 없는
        주문이 계좌에 남습니다.
        """
        if not order.open or order.cancel_sent:
            return False
        try:
            self.sender.cancel(run=self.run, order=order)
        except Exception as exc:  # noqa: BLE001 — 취소를 못 냈다고 멈추지 않습니다.
            logger.warning("계좌 취소를 내지 못했습니다: %s", exc, exc_info=True)
            return False
        order.cancel_sent = True
        order.reason = reason
        return True

    # ── 받기 ────────────────────────────────────────────

    def on_trade(self, trade: dict[str, Any], *, at: int) -> list[dict[str, Any]]:
        """체결 하나. **여기서는 아무것도 안 채웁니다** — 채우는 것은 계좌입니다.

        값만 따라갑니다. 손절·익절이 보는 마지막 체결가가 이 자리에서 옵니다.
        """
        price = _float(trade.get("price"))
        if price is not None:
            self.last_price = price
        return []

    def on_book(self, book: dict[str, Any], *, at: int) -> list[dict[str, Any]]:
        """호가 한 벌. 계좌 갈래는 호가로 채우지 않습니다."""
        return []

    def on_order(self, event: dict[str, Any], *, at: int) -> list[dict[str, Any]]:
        """주문/체결 알림 한 건. 그 주문번호의 주문에만 넣습니다.

        **알림의 체결 수량은 누적입니다.** 늘어난 만큼만 체결로 적습니다 —
        그대로 더하면 한 주문이 여러 번 세어집니다.

        **시세 공백 중에도 받습니다.** 공백은 시세가 안 들어온다는 뜻이고, 계좌의
        체결·취소는 그와 상관없이 실제로 일어난 일입니다. 버리면 그 주문이 장부에서
        닫히지 않습니다. 공백 중에 막히는 것은 규칙 판정과 손절·익절 감시입니다
        (엔진이 막습니다).
        """
        given = str(event.get("id") or "").strip()
        found = next((one for one in self.orders if one.order_no == given), None) if given else None
        if found is None:
            # 취소 확인은 **새 주문번호**로 오고 원주문번호(`org_ord_no`)가 이 앱의 주문을
            # 가리킵니다(명세). 취소 수량이 남은 수량을 다 덮는 알림만 이 길로 닫습니다 —
            # 체결 알림은 이 길로 짝짓지 않습니다.
            target = self._cancel_target(event)
            if target is not None:
                self._close_cancelled(target, str(event.get("status") or "cancelled"))
            return []

        made: list[dict[str, Any]] = []
        filled = _int(event.get("filled"))
        if filled is not None and filled > found.filled:
            grown = filled - found.filled
            found.filled = filled
            price = _float(event.get("fillPrice")) or found.price or self.last_price or 0.0
            row = {
                "orderId": found.id,
                "orderNo": given,
                # **계좌가 준 체결번호**입니다. 한 주문이 나뉘어 채워지면 줄을
                # 가릴 열쇠가 주문번호만으로는 모자랍니다. 알림에 안 실려 오면
                # 장부가 매깁니다 — 비워 두면 화면이 시각으로 가리게 되어 같은
                # 밀리초의 두 체결이 뭉칩니다.
                "execNo": str(event.get("execNo") or "").strip()
                or fill_no(found.id, len(found.fills) + 1),
                "time": int(at),
                "price": float(price),
                "quantity": int(grown),
                "side": found.side,
            }
            found.fills.append(row)
            made.append(row)

        self._settle(found, event)
        return made

    # ── 안쪽 ────────────────────────────────────────────

    def _settle(self, order: SimOrder, event: dict[str, Any]) -> None:
        """알림의 상태로 주문을 닫거나 열어 둡니다."""
        status = str(event.get("status") or "")
        if status:
            order.last_status = status
        if order.remaining == 0:
            order.status = FILLED
            return
        if status == REJECTED:
            # 접수 뒤 계좌가 거절했습니다. 알림에는 거절 문구 칸이 없어 에러코드와
            # 사전의 예비 문구로 적습니다 — 접수 뒤 확인(`confirmed`)과 같은 모양입니다.
            order.confirm_due = None
            code = str(event.get("errorCode") or "").strip()
            order.server_code = order.server_code or code
            if not order.reason:
                order.reason = rejected_text(code, error_codes.message_of(code))
            self._close_cancelled(order, status)
            return
        if status in ("cancelled", "auto-cancelled"):
            self._close_cancelled(order, status)
            return
        if order.filled:
            order.status = PARTIAL

    def close_cancelled(self, order: SimOrder, *, by: str) -> None:
        """취소를 낸 주문을 닫습니다. 확인 알림 없이 대사가 닫을 때 씁니다(`by`).

        알림으로 닫을 때와 **같은 자리**(`_close_cancelled`)를 지납니다 — 결과 줄·
        상태 옮김은 엔진이 끝난 주문을 거둘 때 같은 길로 씁니다.
        """
        self._close_cancelled(order, "cancelled", by=by)

    def _close_cancelled(self, order: SimOrder, status: str, *, by: str = "") -> None:
        order.last_status = status
        order.status = CANCELLED
        if not order.reason:
            order.reason = f"계좌가 {status} 로 답했습니다"
        if by:
            order.closed_by = by

    def _cancel_target(self, event: dict[str, Any]) -> SimOrder | None:
        """취소 확인 알림이 가리키는 이 장부의 열린 주문. 원주문번호 + 취소 수량 ≥ 남은 수량일 때만."""
        original = str(event.get("originalOrderNo") or "").strip()
        cancelled = _int(event.get("cancelled")) or 0
        if not original or cancelled <= 0:
            return None
        found = next((one for one in self.orders if one.order_no == original), None)
        if found is None or not found.open or cancelled < found.remaining:
            return None
        return found

    def _allowed(self) -> None:
        """낼 수 있는 자리인지. **주문을 만들기 전에** 봅니다.

        전환 중·못 읽음·환경 다름은 일반 주문과 같은 판정입니다(`orders.refusal_of`).
        환경 다름의 문구만 실행의 말로 바꿉니다 — 여기서 대는 것은 화면이 아니라
        실행이 선 환경입니다.
        """
        if self.run.mode not in runs.ACCOUNT_MODES:
            raise SimRefused(runs.WHY_NOT_ACCOUNT_MODE, reason="not-account-mode")
        active = str(self.environment() or "").strip() or None
        refused = orders_lib.refusal_of(active, self.run.environment)
        if refused is not None:
            status, text = refused
            raise SimRefused(
                runs.why_environment(self.run.environment, active or "") if status == 400 else text,
                reason="environment",
            )
        if not self.run.approved_at:
            raise SimRefused(runs.WHY_NOT_APPROVED, reason="not-approved")


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class NhSender:
    """실제 계좌로 보내는 자리. **실행이 선 환경을 적어** 부릅니다.

    **라우트가 아니라 주문 함수를 바로 부릅니다.** 라우트는 화면의 요청 모양을
    푸는 자리라 엔진이 그것을 흉내 낼 까닭이 없고, 흉내 내면 화면이 바뀔 때
    엔진이 따라 깨집니다.

    접수 답은 화면과 **같은 함수**(`domestic_account.order_reply`)로 읽습니다 —
    미체결 표 한 줄 모양이라 그대로 장부에 얹힙니다.
    """

    def order(
        self,
        *,
        run: runs.Run,
        side: str,
        quantity: int,
        order_type: str,
        price: float | None,
        stop_price: float | None,
        fill_condition: str,
        tick: float = 1.0,
    ) -> dict[str, Any]:
        if run.market == "overseas":
            return self._overseas_order(
                run=run,
                side=side,
                quantity=quantity,
                order_type=order_type,
                price=price,
                stop_price=stop_price,
                tick=tick,
            )
        from backend.domestic_futureoption import domestic_account
        from backend.domestic_futureoption import domestic_futureoption_functions as api

        night = run.session == "domestic-night"
        type_code = _code_of(domestic_account.ORDER_TYPE, order_type)
        condition_code = _code_of(domestic_account.FILL_CONDITION, fill_condition)
        text = price_text(price, tick, type_code)
        raw = api.order(
            run.code,
            trd_div=domestic_account.SIDE_CODE[side],
            ord_tp_cd=type_code,
            ord_qty=str(quantity),
            ord_pric=text,
            exec_cnd_cd=condition_code,
            night=night,
            environment=_environment_of(run),
        )
        return domestic_account.order_reply(
            order_id=str((raw or {}).get("ord_no") or "").strip(),
            kind="new",
            environment=run.environment,
            code=run.code,
            side=side,
            quantity=quantity,
            context=run.session,
            now=now_kst(),
            order_type_code=type_code,
            fill_condition_code=condition_code,
            price=text,
        )

    def _overseas_order(
        self,
        *,
        run: runs.Run,
        side: str,
        quantity: int,
        order_type: str,
        price: float | None,
        stop_price: float | None,
        tick: float,
    ) -> dict[str, Any]:
        """해외 신규 주문. 신규·정정·취소가 API 하나이고 `qt_div_cd` 로 가릅니다.

        가격 두 칸은 화면 주문과 같은 규칙입니다 — 시장가·STOP시장가는 주문가격
        `0`, 시장가·지정가는 스탑가격이 빈 값입니다. 값은 틱에 맞춘 정확한 십진
        글자입니다(`decimal_text`).
        """
        from backend.overseas_futureoption import overseas_account
        from backend.overseas_futureoption import overseas_futureoption_functions as api

        type_code = OVERSEAS_ORDER_TYPE.get(str(order_type or "").strip(), "")
        if not type_code:
            raise NotSent(f"해외 주문이 받지 않는 주문유형입니다: {order_type}")
        raw = api.order(
            run.code,
            qt_div_cd=api.NEW,
            trd_div_cd=overseas_account.SIDE_CODE[side],
            glb_ord_tp_cd=type_code,
            ord_qty=str(quantity),
            ord_pric="0" if type_code in (api.MARKET_PRICE, api.STOP_MARKET) else decimal_text(price, tick),
            stop_pric=decimal_text(stop_price, tick) if type_code in api.STOP_TYPES else "",
            org_ord_no=api.NO_ORIGINAL,
            environment=_environment_of(run),
        )
        _raise_overseas_refusal(raw)
        return overseas_account.order_result(
            raw or {},
            code=run.code,
            side=side,
            quantity=quantity,
            kind=overseas_account.ORDER_KIND[api.NEW],
            now=now_kst(),
            environment=run.environment,
            order_type_code=type_code,
        )

    def _overseas_cancel(self, *, run: runs.Run, order: SimOrder) -> dict[str, Any]:
        """해외 취소. **원주문의 가격 두 칸을 신규와 같은 규칙으로 다시 싣습니다** — 명세가
        주문유형에 따라 그 값을 요구하고, 화면 주문도 같은 몸을 보냅니다."""
        from backend.overseas_futureoption import overseas_account
        from backend.overseas_futureoption import overseas_futureoption_functions as api

        type_code = OVERSEAS_ORDER_TYPE.get(order.order_type, api.LIMIT_PRICE)
        tick = order.tick or 0.0
        raw = api.order(
            run.code,
            qt_div_cd=api.CANCEL,
            trd_div_cd=overseas_account.SIDE_CODE[order.side],
            glb_ord_tp_cd=type_code,
            ord_qty=str(order.remaining),
            ord_pric="0" if type_code in (api.MARKET_PRICE, api.STOP_MARKET) else decimal_text(order.price, tick),
            stop_pric=decimal_text(order.stop_price, tick) if type_code in api.STOP_TYPES else "",
            org_ord_no=padded_order_no(order.order_no),
            environment=_environment_of(run),
        )
        _raise_overseas_refusal(raw)
        return {"id": str((raw or {}).get("ord_no") or "").strip()}

    def cancel(self, *, run: runs.Run, order: SimOrder) -> dict[str, Any]:
        if run.market == "overseas":
            return self._overseas_cancel(run=run, order=order)
        from backend.domestic_futureoption import domestic_account
        from backend.domestic_futureoption import domestic_futureoption_functions as api

        raw = api.cancel(
            run.code,
            org_ord_no=padded_order_no(order.order_no),
            trd_div=domestic_account.SIDE_CODE[order.side],
            ord_qty=str(order.remaining),
            night=run.session == "domestic-night",
            environment=_environment_of(run),
        )
        return {"id": str((raw or {}).get("ord_no") or "").strip()}


#: 엔진의 주문유형 → 해외 주문유형 코드(`glb_ord_tp_cd`).
OVERSEAS_ORDER_TYPE: dict[str, str] = {"시장가": "1", "지정가": "2", "STOP-M": "3", "STOP-L": "4"}


def decimal_text(value: float | None, tick: float) -> str:
    """틱에 맞춘 **정확한 십진 글자**. 값이 없으면 빈 글자입니다.

    해외 품목에는 1/32·1/64 처럼 십진으로 떨어지는 분수 틱이 있습니다. 부동소수를
    그대로 적으면 `112.54687500000001` 같은 찌꺼기나 지수 표기가 나갑니다. 값을
    틱 단위로 반올림한 뒤 십진으로 적고, 끝의 0 은 뺍니다(`112.546875`, `4510`).
    """
    if value is None:
        return ""
    step = Decimal(str(abs(tick))) if tick else Decimal("0")
    if step > 0:
        steps = (Decimal(str(value)) / step).to_integral_value(rounding=ROUND_HALF_UP)
        made = (steps * step).normalize()
    else:
        # 틱을 모르면 주문 몸이 받는 자리(소수 8자리)에서 반올림합니다 — 부동소수
        # 찌꺼기가 그 자리를 넘겨 나가지 않게 합니다.
        made = Decimal(str(value)).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP).normalize()
    text = format(made, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def price_text(price: float | None, tick: float, type_code: str) -> str:
    """실어 보낼 주문가격 글자.

    **값을 안 받는 주문유형은 `0` 입니다** — 시장가와 최유리가 그렇습니다.
    명세가 그때 주문가격을 0 으로 받고, 저장해 둔 모의 주문 둘도 시장가에
    `0` 으로 실려 있습니다. 빈 글자를 보내면 화면이 내는 주문과 달라집니다
    (`domestic_routes.OrderRequest.price` 와 같은 규칙).

    값을 받는 유형은 **틱 자릿수만큼** 소수로 적습니다. `405.25` 를 `405.25`
    로, 틱이 1 인 품목의 `406.0` 은 `406` 으로 — 자릿수가 들쭉날쭉하면 같은
    값이 요청마다 다른 글자로 나갑니다.
    """
    from backend.domestic_futureoption import domestic_futureoption_functions as api

    if type_code in api.ZERO_PRICE_TYPES:
        return "0"
    if price is None:
        return ""
    return f"{price:.{_places(tick)}f}"


def _places(tick: float) -> int:
    """그 틱이 쓰는 소수 자릿수. `0.05` 면 둘, `1` 이면 없음."""
    body = f"{abs(float(tick)):.10f}".rstrip("0")
    return len(body.split(".")[1]) if "." in body else 0


def environment_now() -> str:
    """지금 활성 환경. 주문을 만들기 전에 보는 값입니다. 못 읽으면 빈 글자입니다."""
    from backend.nh_api import runtime

    return str(runtime.active_environment() or "")


def _environment_of(run: runs.Run) -> str:
    """계좌로 부를 때 적는 환경. **실행이 선 환경**이고, 없으면 부르지 않습니다.

    빈 값으로 부르면 그 순간의 활성 환경으로 나갑니다 — 그 길을 두지 않습니다.
    보내기 전 검사가 이미 막는 자리지만, 부르는 자리에서도 한 번 더 지킵니다.
    """
    environment = str(run.environment or "").strip()
    if not environment:
        raise NotSent("실행이 선 환경을 몰라 계좌로 부르지 않습니다.")
    return environment


def _raise_overseas_refusal(raw: Any) -> None:
    """해외 주문 답이 거절이면 업무 거부로 올립니다.

    해외는 거절을 `err_cd`·`err_msg` 로 싣고, 접수에도 `00000` 을 실어 보냅니다
    (`orders.failed`). 공통 판정은 `msg_cd` 만 보므로 여기서 봅니다 — 화면 주문
    라우트와 같은 자리입니다. 안 보면 거절된 주문을 주문번호와 함께 접수로 읽거나,
    거절된 취소를 확인을 기다리는 취소로 붙들어 둡니다.
    """
    body = raw if isinstance(raw, dict) else {}
    code = str(body.get("err_cd") or "").strip()
    if orders_lib.failed(code):
        raise BusinessError(
            "overseas_order", code, str(body.get("err_msg") or "").strip(), body
        )


def _code_of(table: dict[str, str], name: str) -> str:
    """이름에서 코드로. 표에 없으면 빈 값입니다."""
    body = str(name or "").strip()
    return next((code for code, one in table.items() if one == body), "")
