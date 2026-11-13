"""거래 API 를 부르는 공통 자리.

카테고리별 API 함수는 전부 이 함수를 거칩니다. 여기서 세 가지를 지킵니다.

1. 접속 토큰을 붙입니다. 국내와 해외는 APP Key 가 달라 토큰도 다릅니다.
   어느 토큰을 쓸지는 API 이름으로 정해지므로 부르는 쪽이 신경 쓰지 않습니다.
2. 호출 제한 간격을 지킵니다.
3. 성공 여부를 판단합니다. **HTTP 200 은 성공이 아닙니다.**

**다시 보내지 않습니다.** 실패하면 그대로 올립니다. 주문이 시간 초과로
실패했을 때 자동으로 다시 보내면 같은 주문이 두 번 접수될 수 있습니다.
다시 보낼지는 사람이 주문내역을 확인한 뒤에 정합니다.

응답은 손대지 않고 원문 그대로 돌려줍니다. 화면에 맞게 바꾸는 일은 각
카테고리의 함수가 합니다.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx

from backend.auth.auth_functions import AuthError
from backend.nh_api.catalog import COMMON, ApiSpec, http_spec
from backend.nh_api import error_codes
from backend.nh_api.errors import (
    BusinessError,
    GatewayError,
    HttpError,
    NHApiError,
    TransportError,
)
from backend.nh_api.rate_limit import RateLimiter
from backend.nh_api.replay import note_replay
from backend.nh_api.session import Session

# 업무 처리 결과가 담기는 항목. 주문 계열 응답이 이 두 항목으로 사유를 알립니다.
# 접속 토큰을 안 받아 줄 때 오는 상태 코드.
UNAUTHORIZED = 401

MESSAGE_CODE = "msg_cd"
MESSAGE_TEXT = "msg_cntn"

# 이 작업(요청 하나) 안에서 어느 환경으로 불렀는지. 라우트가 응답 헤더로 옮깁니다.
#
# 응답 시점의 활성 환경이 아니라 **부른 시점의 환경**을 적어 두는 것이 뜻입니다.
# 환경을 바꾸기 직전에 나간 요청의 응답은 바뀐 뒤에 도착하는데, 그때 활성
# 환경을 적으면 옛 환경 값이 새 환경 것처럼 보입니다. 요청마다 갈리는 값이라
# `ContextVar` 에 둡니다 — `replay.last_replay` 와 같은 자리입니다.
last_environment: ContextVar[str | None] = ContextVar("nh_last_environment", default=None)


@dataclass(frozen=True)
class ApiResult:
    """호출 하나의 결과. 응답 본문은 받은 그대로입니다."""

    api_id: str
    environment: str
    market: str
    http_status: int
    body: Any
    headers: Mapping[str, str]


#: 게이트웨이·실시간 접속 쪽 코드의 앞자리. 숫자로만 된 코드는 업무 쪽입니다.
_SYSTEM_PREFIXES = ("EG", "EST")


def _is_system_code(code: str) -> bool:
    """게이트웨이 쪽 코드인지. **앞자리로만 가립니다** — 문구는 안 봅니다."""
    return str(code or "").strip().upper().startswith(_SYSTEM_PREFIXES)


def _peek(response: httpx.Response) -> Any:
    """본문을 읽어 둡니다. 못 읽으면 `None` — 여기서 터뜨리지 않습니다.

    본문에 실린 오류 코드를 **다시 보내기 전에** 봐야 해서 먼저 읽습니다.
    읽은 것은 그대로 씁니다 — 같은 본문을 두 번 풀면 전체시세처럼 큰 응답에서
    그만큼이 두 배가 됩니다. 못 읽었을 때만 `_read_body` 가 다시 보고, 거기서
    문구를 갖춰 올립니다.
    """
    try:
        return response.json()
    except ValueError:
        return None


def business_failure(body: Any) -> tuple[str, str] | None:
    """업무 처리가 거부되었으면 (코드, 설명)을, 아니면 None 을 돌려줍니다.

    `msg_cd` 가 있고 비어 있지 않으면 거부로 봅니다. 정상 처리된 주문 응답에서는
    이 항목이 빈 문자열로 옵니다.

    조회 결과가 0건인 것(`nrec` 가 0)은 거부가 아닙니다. 「그 조건에 해당하는
    자료가 없다」는 정상 응답이므로 여기서 걸러내지 않습니다.
    """
    if not isinstance(body, dict):
        return None
    code = str(body.get(MESSAGE_CODE) or "").strip()
    if not code:
        return None
    return code, str(body.get(MESSAGE_TEXT) or "").strip()


class TradeClient:
    """거래 API 호출."""

    def __init__(
        self,
        session: Session,
        *,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._session = session
        self._limiter = limiter or RateLimiter()

    @property
    def session(self) -> Session:
        return self._session

    def call(
        self,
        api_id: str,
        payload: Mapping[str, Any] | None = None,
        *,
        environment: str | None = None,
    ) -> ApiResult:
        """API 하나를 한 번 부릅니다.

        Raises:
            TransportError: 응답을 받지 못한 경우.
            HttpError: HTTP 오류 응답을 받은 경우.
            BusinessError: 업무 처리가 거부된 경우(상태 코드와 무관).
            GatewayError: 게이트웨이·실시간 접속 쪽이 거절한 경우.
        """
        spec = http_spec(api_id)
        if spec.market == COMMON:
            raise ValueError(
                f"{api_id} 는 인증 API 입니다. backend.auth 의 함수를 쓰세요."
            )

        environment = environment or self._session.config.default_environment
        last_environment.set(environment)
        # 설정에 그 시장의 인증 정보가 없으면 여기서 걸립니다.
        self._session.config.market(spec.market, environment)

        self._limiter.wait(environment, spec.market, spec.limit)

        body_to_send = dict(payload or {})
        response = self._send(spec, body_to_send, environment)
        body = _peek(response)
        trouble = error_codes.failure_of(body)

        if response.status_code == UNAUTHORIZED or (
            trouble is not None and trouble["kind"] == error_codes.REISSUE
        ):
            # 만료 전이라 여겼는데 서버가 그 토큰을 안 받는 경우입니다. 시계가
            # 어긋났거나 다른 데서 폐기됐을 수 있습니다. **한 번만** 다시 받아
            # 보고, 또 거절되면 그대로 올립니다 — 반복하면 인증 호출 제한에
            # 걸리고, 그래도 안 될 일은 안 됩니다.
            #
            # **주문도 이 길로 다시 보냅니다.** 401 은 문 앞에서 거절된 것이라
            # 그 요청은 처리되지 않았습니다 — 중복 주문이 되지 않습니다. 응답을
            # 아예 못 받은 경우(`TransportError`)와 다른 자리입니다. 그때는
            # 처리됐는지 알 수 없어 **다시 보내지 않습니다.**
            #
            # **본문의 오류 코드도 같은 길입니다.** 상태가 200 이어도 토큰을
            # 다시 받으라는 코드(`EGW052` 같은 것)가 본문에 담겨 옵니다 —
            # 상태만 보면 그 자리를 놓칩니다.
            self._session.tokens.forget(spec.market, environment)
            response = self._send(spec, body_to_send, environment)
            body = _peek(response)
            trouble = error_codes.failure_of(body)

        # 저장본으로 답한 것이면 무엇으로 답했는지 남겨 둡니다. 라우트가 그것을
        # 화면 쪽 헤더로 옮깁니다. 실서버 응답이면 지워집니다.
        note_replay(response.headers)
        if body is None:
            # 살짝 읽기가 실패한 자리입니다. 여기서 제대로 읽어 보고, 그래도
            # 안 되면 그때 올립니다 — 같은 본문을 두 번 풀지 않습니다.
            body = _read_body(api_id, response)

        if trouble is not None:
            # **상태 코드보다 본문이 앞섭니다.** 200 으로 오면서 본문에만 실패가
            # 적혀 오는 자리가 있어, 상태만 보면 성공으로 읽게 됩니다.
            #
            # 게이트웨이 쪽 거절과 업무 거부를 **가려서 올립니다.** 업무 거부는
            # 「서버가 보고 안 된다고 한 것」이고, 게이트웨이 거절은 요청이
            # 처리됐는지조차 모르는 자리입니다 — 주문 화면이 둘을 같은 말로
            # 적으면 이미 나간 주문을 안 나갔다고 말하게 됩니다.
            if _is_system_code(trouble["code"]):
                raise GatewayError(
                    api_id, trouble["code"], trouble["message"], body, trouble["kind"]
                )
            raise BusinessError(
                api_id, trouble["code"], trouble["message"], body, trouble["kind"]
            )

        if response.status_code >= 400:
            raise HttpError(api_id, response.status_code, body)

        failure = business_failure(body)
        if failure is not None:
            raise BusinessError(
                api_id, failure[0], failure[1], body, error_codes.kind_of(failure[0])
            )

        return ApiResult(
            api_id=api_id,
            environment=environment,
            market=spec.market,
            http_status=response.status_code,
            body=body,
            headers=dict(response.headers),
        )

    def _send(self, spec: ApiSpec, payload: dict[str, Any], environment: str) -> httpx.Response:
        # 여기서 토큰이 없으면 새로 발급받습니다. 그 발급 요청은 이 제한기를
        # 거치지 않습니다. 인증 등급은 초당 3건이고 토큰은 시장마다 하나씩 한
        # 시간을 쓰므로 지금 구성에서는 그 제한을 넘지 않습니다.
        try:
            headers = self._session.tokens.headers(spec.market, environment)
        except AuthError as exc:
            raise TransportError(spec.api_id, f"접속 토큰을 준비하지 못했습니다: {exc}") from exc

        url = f"{self._session.base_url(environment)}{spec.path}"
        try:
            if spec.method == "GET":
                return self._session.client.get(url, headers=headers)
            return self._session.client.request(spec.method, url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise TransportError(
                spec.api_id,
                f"{spec.name} 요청이 시간 안에 끝나지 않았습니다. "
                "보내지 못한 것인지 처리된 것인지 알 수 없으므로, 다시 보내기 전에 "
                "주문내역이나 잔고로 확인해 주세요.",
            ) from exc
        except httpx.HTTPError as exc:
            raise TransportError(spec.api_id, f"{spec.name} 요청에 실패했습니다: {exc}") from exc


def _read_body(api_id: str, response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        text = response.text.strip()
        if response.status_code >= 400:
            # 오류 본문이 JSON 이 아닌 경우에도 서버가 보낸 설명을 남깁니다.
            return text
        raise NHApiError(api_id, f"{api_id} 응답을 해석할 수 없습니다: {text}") from None
