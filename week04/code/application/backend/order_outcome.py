"""접수된 주문이 **그 뒤에** 거절됐는지 확인하는 자리.

NH선물은 주문·정정·취소에 동기 응답으로 「접수」(주문번호)만 줍니다. 거절은
그 뒤에 계좌 쪽 기록에만 남는 경우가 있습니다.

    국내  주문내역의 그 주문 줄이 상태 `9`(거부)가 되고 거부코드·거부사유가
          채워집니다. 예: 스프레드 취소가 「정정취소잔량없음」(0804)으로 거부됨.
    해외  주문내역에는 줄이 없고 주문오류내역에만 남습니다. 예: 지원하지 않는
          종목의 신규 주문이 「Not Found Symbol」로 거절됨.

그래서 접수 답을 받은 뒤 **짧게 기다렸다가 그 주문번호를 조회해** 결과를 하나로
정합니다(`settle`). 결과는 셋 가운데 하나입니다.

    accepted  주문내역에 그 주문이 거부 아닌 상태로 있습니다.
    rejected  주문내역의 그 줄이 거부이거나, 주문오류내역에 그 번호가 있습니다.
              `code`·`message` 는 NH선물이 준 그대로입니다.
    unknown   기다리는 동안 어느 쪽에서도 찾지 못했거나 조회가 실패한 경우입니다.
              **접수를 뒤집지 않습니다** — 주문은 나갔을 수 있습니다. 주문내역에서
              확인해야 합니다.

**왜 실시간 주문 알림을 기다리지 않는가.** 알림에도 처리구분 `9`(거부)와 에러코드
칸이 있지만 거부 문구 칸은 없고, 알림은 구독해 둔 동안에만 옵니다. 조회는 주문을
낸 자리에서 바로 할 수 있고 문구까지 받습니다.

**기다리는 시간과 호출 수.** 첫 조회는 접수 뒤 `WAITS[0]` 초, 거기서 못 찾으면
`WAITS[1]` 초 뒤에 한 번 더 봅니다. 한 번에 국내는 조회 하나(주문내역), 해외는
둘(주문오류내역·주문내역)이라 주문 하나에 많아야 넷입니다. 계좌 조회는 호출
제한을 지나므로(`backend.nh_api.rate_limit`) 넘치지 않고 차례를 기다립니다.

**거부가 기다리는 시간보다 늦게 기록될 수 있습니다.** 그때는 `unknown` 이 되고,
주문내역이 뒤에 그 결과를 보입니다.

**첫 쪽만 봅니다.** 주문내역을 이어 받지 않으므로, 그날 주문이 많아 그 주문이
첫 쪽에 없으면 `unknown` 입니다.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from backend.nh_api import error_codes
from backend.sessions.hours import now_kst

logger = logging.getLogger(__name__)

ACCEPTED = "accepted"
REJECTED = "rejected"
UNKNOWN = "unknown"

#: 시장. 접수 답의 `market` 과 같은 낱말입니다.
DOMESTIC = "domestic"
OVERSEAS = "overseas"

#: 결과를 어디서 읽었는지.
FROM_HISTORY = "order-history"
FROM_ERRORS = "order-error"

#: 조회 사이에 기다리는 초. 첫 값은 접수 답을 받은 뒤 첫 조회까지입니다.
#: 비우면 조회하지 않고 `unknown` 입니다.
WAITS: tuple[float, ...] = (0.7, 2.0)

#: 주문내역의 「거부」 상태코드. 국내·해외 모두 같은 값입니다.
REJECTED_STATUS_CODE = "9"

#: 바꿔 낄 수 있게 둔 기다리기. 시험이 실제로 기다리지 않고 지나갑니다.
sleep: Callable[[float], None] = time.sleep


def unknown() -> dict[str, Any]:
    """결과를 정하지 못한 답."""
    return {"state": UNKNOWN}


def settle(look: Callable[[], dict[str, Any] | None]) -> dict[str, Any]:
    """`WAITS` 대로 기다리며 `look` 을 부릅니다. 결과가 나오면 그것을, 끝까지 없으면 `unknown`.

    `look` 이 예외를 내면 그 회차는 「못 찾음」으로 봅니다 — 조회가 실패했다고
    접수된 주문을 실패로 답하지 않습니다.
    """
    for wait in WAITS:
        sleep(wait)
        try:
            found = look()
        except Exception:  # noqa: BLE001 — 확인을 못 한 것이지 주문이 실패한 것이 아닙니다.
            logger.warning("주문 결과를 확인하는 조회가 실패했습니다.", exc_info=True)
            continue
        if found is not None:
            return found
    return unknown()


def _same_no(left: Any, right: Any) -> bool:
    """주문번호가 같은지. 앞자리 0 채움의 차이는 같은 번호로 봅니다."""
    one = str(left or "").strip().lstrip("0")
    two = str(right or "").strip().lstrip("0")
    return bool(one) and one == two


def _rejected(code: Any, message: Any, source: str) -> dict[str, Any]:
    said = error_codes.describe(code, message)
    made: dict[str, Any] = {"state": REJECTED, "source": source}
    if said["code"]:
        made["code"] = said["code"]
    if said["message"]:
        made["message"] = said["message"]
    return made


def _from_row(row: dict[str, Any], code_field: str) -> dict[str, Any]:
    """주문내역 줄 하나의 결과. 거부면 그 코드·사유, 아니면 접수."""
    if str(row.get("statusCode") or "") == REJECTED_STATUS_CODE:
        return _rejected(row.get(code_field), row.get("rejectReason"), FROM_HISTORY)
    return {"state": ACCEPTED, "source": FROM_HISTORY}


def judge_domestic(history: dict[str, Any], order_no: str) -> dict[str, Any] | None:
    """국내 주문내역(화면 모양)에서 그 주문의 결과. 줄이 없으면 `None`.

    **주문오류내역은 보지 않습니다.** 국내 주문오류내역 줄에는 주문번호가 없고
    원주문번호뿐이라(`domestic_account.order_errors`) 신규 주문은 종목·시각으로만
    짝지을 수 있습니다. 같은 종목을 잇달아 내면 다른 주문의 거절을 이 주문의
    거절로 읽게 되고, 그러면 계좌에 살아 있는 주문을 「거절」로 보여 같은 주문을
    다시 내게 만듭니다. 정정·취소도 같은 원주문에 앞서 낸 요청의 줄과 가려지지
    않습니다. 접수 뒤 거부된 국내 취소는 주문내역에 거부 줄로 남고, 주문오류내역에는
    남지 않을 수 있습니다.
    """
    for row in history.get("orders") or []:
        if _same_no(row.get("id"), order_no):
            return _from_row(row, "rejectCode")
    return None


def judge_overseas(
    history: dict[str, Any],
    errors: dict[str, Any],
    order_no: str,
    *,
    cancelling: bool = False,
    original_no: str = "",
) -> dict[str, Any] | None:
    """해외 주문오류내역·주문내역(화면 모양)에서 그 주문의 결과. 못 정하면 `None`.

    차례는 **주문오류내역 → 주문내역**입니다. 접수 뒤 거절된 해외 주문은
    주문내역에 줄이 없고 주문오류내역에만 남습니다.

    **해외 취소는 줄이 남지 않을 수 있습니다** — 취소가 받아들여지면 원주문 줄이
    주문내역에서 빠집니다. 그래서 취소는 「오류내역에 없고, 주문내역에 취소
    번호도 원주문 번호도 없음」을 접수로 봅니다. 원주문이 아직 보이면 정하지
    않습니다.
    """
    for row in errors.get("errors") or []:
        if _same_no(row.get("orderNo"), order_no):
            return _rejected(row.get("messageCode"), row.get("message"), FROM_ERRORS)
    rows = history.get("orders") or []
    for row in rows:
        if _same_no(row.get("id"), order_no):
            return _from_row(row, "errorCode")
    if cancelling and original_no and not any(_same_no(row.get("id"), original_no) for row in rows):
        return {"state": ACCEPTED, "source": FROM_HISTORY}
    return None


def look(
    market: str,
    order_no: str,
    *,
    cancelling: bool = False,
    original_no: str = "",
    **call: Any,
) -> dict[str, Any] | None:
    """그 주문의 결과를 **한 번** 조회해 정합니다. 못 정하면 `None`.

    `call` 은 조회 함수에 그대로 넘기는 인자입니다 — 화면 주문은 호출부(`api=`),
    전략 실행은 실행이 선 환경(`environment=`)을 넘깁니다.
    """
    if market == OVERSEAS:
        from backend.overseas_futureoption import overseas_account
        from backend.overseas_futureoption import overseas_futureoption_functions as api

        errors = overseas_account.order_errors(api.order_error(now_kst().strftime("%Y%m%d"), **call))
        history = overseas_account.order_history(api.order_history(**call))
        return judge_overseas(
            history, errors, order_no, cancelling=cancelling, original_no=original_no
        )

    from backend.domestic_futureoption import domestic_account
    from backend.domestic_futureoption import domestic_futureoption_functions as api

    history = domestic_account.order_history(api.order_history(**call))
    return judge_domestic(history, order_no)


def apply(reply: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    """접수 답에 결과를 싣습니다. 거절이면 줄의 상태도 거부로 바꿉니다.

    `accepted` 는 **NH선물이 접수 번호를 줬다는 뜻으로 그대로 둡니다.** 결과는
    `outcome` 으로 봅니다. 거절이면 줄이 미체결 표에 「접수」로 남지 않도록 상태를
    주문내역과 같은 낱말(`rejected`·`9`)로 바꾸고 잔량을 비웁니다.
    """
    reply["outcome"] = outcome
    if outcome.get("state") == REJECTED:
        reply["status"] = REJECTED
        reply["statusCode"] = REJECTED_STATUS_CODE
        reply["remaining"] = 0
        reply["editable"] = False
    return reply
