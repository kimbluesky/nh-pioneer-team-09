"""거래 API 호출이 실패했을 때 쓰는 예외.

세 가지를 구분합니다.

    TransportError  응답을 받지 못함 (연결 실패, 시간 초과)
    HttpError       HTTP 오류 응답을 받음
    BusinessError   업무 처리가 거부됨 (상태 코드와 무관)
    GatewayError    게이트웨이·실시간 접속 쪽 거절 (EG·EGW·EST 코드)

**HTTP 200 은 성공이 아닙니다.** 주문 계열 응답은 200 으로 오면서 본문의
`msg_cd` 에 거부 사유를 담아 보냅니다. 그래서 상태 코드만 보고 성공으로
판단하지 않습니다.
"""
from __future__ import annotations

from typing import Any

from backend.nh_api import error_codes

#: 실패의 갈래와 코드를 화면에 알리는 헤더. 상태 코드와 문장만으로는 「다시
#: 해 볼 거절」과 「모의에서는 늘 안 되는 것」을 화면이 가를 수 없습니다.
ERROR_KIND_HEADER = "x-nh-error-kind"
ERROR_CODE_HEADER = "x-nh-error-code"

#: 모의투자에서 부를 수 없는 조회를 불렀을 때 화면에 서는 문장.
PAPER_UNAVAILABLE_DETAIL = "모의투자에서는 NH선물이 이 조회를 제공하지 않습니다. 실전 환경에서 확인해 주세요."


class NHApiError(Exception):
    """거래 API 호출 실패."""

    def __init__(self, api_id: str, message: str) -> None:
        super().__init__(message)
        self.api_id = api_id
        self.message = message


class TransportError(NHApiError):
    """응답을 받지 못한 경우입니다.

    **시간 초과는 실패와 다릅니다.** 주문을 보내고 시간이 초과되면 접수 여부를
    알 수 없으므로, 같은 주문을 다시 보내기 전에 주문내역으로 확인해야 합니다.
    """


class HttpError(NHApiError):
    """HTTP 오류 응답을 받은 경우입니다.

    본문은 `errorTime`, `errorMessage`, `errorStatus`, `errorPath` 네 항목으로
    옵니다. 서버가 보낸 설명을 그대로 `body` 에 남깁니다.
    """

    def __init__(self, api_id: str, status: int, body: Any) -> None:
        detail = ""
        if isinstance(body, dict):
            detail = str(body.get("errorMessage") or "").strip()
        message = f"{api_id} 호출이 거부되었습니다. (HTTP {status})"
        if detail:
            message = f"{message} {detail}"
        super().__init__(api_id, message)
        self.status = status
        self.body = body


class BusinessError(NHApiError):
    """업무 처리가 거부된 경우입니다.

    **HTTP 상태와 상관없습니다.** 200 으로 오면서 본문에 거부 사유가 담기는
    자리가 있습니다. 주문 계열의 `msg_cd`·`err_cd` 와, 숫자로 된 `errorCode`
    가 그렇습니다 — 둘 다 「서버가 무엇을 못 했는지 말해 준」 자리입니다.

    `kind` 는 사용자가 **무엇을 해야 하는지**입니다. 자세한 것은
    `backend/nh_api/error_codes.py` 에 있습니다.
    """

    def __init__(
        self, api_id: str, code: str, detail: str, body: Any, kind: str = ""
    ) -> None:
        message = f"{api_id} 처리가 거부되었습니다. ({code})"
        if detail:
            message = f"{message} {detail}"
        super().__init__(api_id, message)
        self.code = code
        self.detail = detail
        self.body = body
        self.kind = kind


def paper_unavailable(exc: BaseException) -> bool:
    """모의투자에서는 NH선물이 그 API 를 제공하지 않는다는 거절인지(`EGW053`)."""
    return (
        isinstance(exc, GatewayError)
        and error_codes.kind_of(exc.code) == error_codes.PAPER_UNAVAILABLE
    )


class GatewayError(NHApiError):
    """게이트웨이·실시간 접속 쪽이 거절한 경우입니다(`EG`·`EGW`·`EST` 코드).

    **업무 거부와 다른 자리입니다.** 업무 거부는 「서버가 보고 안 된다고 한
    것」이라 주문 화면이 「거절되었습니다」로 적어도 맞지만, 이쪽은 **요청이
    처리됐는지 아닌지 모르는** 자리입니다. 주문을 이것으로 「거절」이라고
    적으면 이미 나간 주문을 안 나갔다고 말하게 됩니다.

    `kind` 는 사용자가 무엇을 해야 하는지입니다(재발급·잠시 뒤·설정 확인 등).
    """

    def __init__(
        self, api_id: str, code: str, detail: str, body: Any, kind: str = ""
    ) -> None:
        message = f"{api_id} 요청이 거절되었습니다. ({code})"
        if detail:
            message = f"{message} {detail}"
        super().__init__(api_id, message)
        self.code = code
        self.detail = detail
        self.body = body
        self.kind = kind
