"""주문·정정·취소가 두 시장에서 같은 모양이 되도록 하는 규칙.

주문을 내는 자리는 **되돌릴 수 없습니다.** 그래서 조회 라우트와 다르게
다루는 것이 셋 있습니다.

**HTTP 200 은 성공이 아닙니다.** 국내는 거부 사유가 `msg_cd`·`msg_cntn` 으로,
해외는 `err_cd`·`err_msg` 로 실려 옵니다. 이름이 달라 한 자리에서 판정하지
못하므로 시장마다 자기 칸을 읽고, 화면에는 **같은 모양**으로 냅니다.

**거부 사유는 서버 문구를 그대로 냅니다.** 주문 거부는 「무엇을 고쳐야 다시
낼 수 있는지」가 그 문구에 들어 있고, 주문오류내역이 이미 같은 문구를 화면에
내고 있습니다. 한 화면에서 같은 사실을 두 낱말로 부르지 않으려고 원문을 씁니다.
조회가 거절됐을 때도 NH선물이 코드를 주면 코드와 원문을 문장에 싣습니다
(`lookup_refusal`).

**거부는 실패가 아니라 답입니다.** 주문이 거절된 것은 서버 고장이 아니라
화면이 폼 자리에 적어야 하는 결과이므로 200 으로 내고 `accepted` 로 가릅니다.
502·504 는 「NH선물이 무엇이라 했는지 모른다」는 뜻으로 남겨 둡니다 — 그때는
**같은 주문을 다시 내기 전에 주문내역으로 확인해야 합니다.**

**주문은 활성 환경으로 나갑니다.** 실전이면 실전으로 나갑니다 — 막지
않습니다. 어느 환경이 활성인지는 서버의 접속 자리 하나가 정하고
(`backend.nh_api.runtime`), 그것을 바꾸는 자리는 설정 화면입니다.

**화면이 믿는 환경을 함께 받아 대조합니다.** 화면은 배지에 「모의」를 띄운
채 주문을 냅니다. 그 배지가 서버의 활성 환경과 어긋나 있으면 모의라 믿고
낸 주문이 실전 계좌로 나갑니다. 그래서 주문 요청마다 화면이 믿는 환경을
싣게 하고, 서버의 활성 환경과 다르면 **보내지 않습니다.** 환경을 바꾸는
중에도 보내지 않습니다 — 그 사이에 낸 주문은 어느 쪽으로 나갔는지 뒤에
가리기 어렵습니다.

**다만 어느 환경인지 못 읽으면 보내지 않습니다.** 주소를 못 고른 채로
되돌릴 수 없는 요청을 내는 것이라, 모르는 채 보내는 것보다 안 보내고
알리는 편이 낫습니다.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from backend.config import ENVIRONMENT_LABELS
from backend.nh_api import error_codes, runtime
from backend.nh_api.errors import (
    ERROR_CODE_HEADER,
    ERROR_KIND_HEADER,
    PAPER_UNAVAILABLE_DETAIL,
    BusinessError,
    GatewayError,
    HttpError,
)

# 원주문번호 자리 수. 명세가 앞자리를 0 으로 채운 열 자리를 요구합니다
# (주문번호 `1` → `0000000001`). 화면은 주문내역 줄의 번호를 그대로 주고
# 채우는 일은 여기서 합니다.
ORDER_NO_WIDTH = 10

# 주문을 안 낸 채 돌려보낼 때 화면에 적는 말. 두 시장이 같은 문구를 씁니다.
UNKNOWN_ENVIRONMENT = (
    "설정을 읽지 못해 주문을 내지 않았습니다. 설정 화면에서 인증 정보를 확인해 주세요."
)
SWITCHING = "환경을 바꾸는 중이라 주문을 내지 않았습니다. 잠시 뒤 다시 시도해 주세요."
REPLAY_REFUSED = "저장해 둔 주문 기록이 없어 재생 모드에서는 낼 수 없습니다."
NOT_RECEIVED = (
    "주문 결과를 받지 못했습니다. 다시 내기 전에 주문체결상세에서 접수됐는지 확인해 주세요."
)
REJECTED = "주문을 내지 못했습니다. 잠시 뒤 다시 시도해 주세요."

# 접수 답은 왔는데 주문번호가 비어 있을 때.
#
# **거절이 아닙니다.** 서버가 거부 사유를 보내지 않았으니 주문은 나갔을 수 있고,
# 다만 그것을 가리킬 번호가 없습니다. 화면에 내는 문장이 「나갔는데」로 단정하는
# 것은 **접수 답이 돌아온 자리**여서입니다 — 답 자체를 못 받은 쪽은 다른 문장입니다. 번호 없는 접수를 화면에 얹으면 실시간
# 알림과 이어 붙지 않는 줄이 목록에 남습니다. 그래서 「무엇이라 했는지 모른다」
# 쪽(504)으로 내고, 화면은 주문내역을 다시 조회해 확인합니다.
NO_ORDER_NUMBER = (
    "주문은 나갔는데 주문번호를 받지 못했습니다. 주문체결상세에서 확인해 주세요."
)


def order_no(text: Any) -> str:
    """원주문번호를 명세 자리 수에 맞춥니다.

    이미 열 자리면 그대로입니다. 열 자리보다 길면 **자르지 않고 그대로**
    보냅니다 — 알려지지 않은 규칙으로 번호가 길어졌을 때 앞뒤를 잘라 보내면
    엉뚱한 주문을 건드릴 수 있습니다. 그 판단은 서버가 하게 둡니다.
    """
    body = str(text or "").strip()
    return body.rjust(ORDER_NO_WIDTH, "0")


def original_no(original: Any, own: Any = "") -> str | None:
    """답에 싣는 원주문번호. **원주문이 없으면 `None`** 입니다.

    서버는 원주문이 없는 자리(신규 주문)를 두 가지로 채워 보냅니다 — `0` 만
    이어진 번호(`0000000000`)이거나 **자기 주문번호와 같은 번호**입니다. 둘 다
    「원주문 없음」이라 비웁니다. 정정·취소는 늘 새 번호를 받으므로(원번호와
    다름) 이 규칙에 걸리지 않습니다.

    주문구분 칸이 없는 답(접수 답·실시간 알림)에도 같은 규칙이 서도록, 신규인지가
    아니라 **값**으로 가립니다. 국내·해외·실시간이 모두 이 함수를 지납니다.
    """
    body = str(original or "").strip()
    if not body or set(body) == {"0"}:
        return None
    mine = str(own or "").strip()
    if mine and body == mine:
        return None
    return body


def environment_of(api_client: Any) -> str | None:
    """이 호출부가 어느 환경으로 나가는지. 읽을 수 없으면 `None`.

    실전인지 모의인지로 주문을 막지 않습니다 — **주소를 고르는 데만** 씁니다.
    """
    try:
        name = api_client.session.config.default_environment
    except AttributeError:
        return None
    # 빈 값도 「못 읽음」입니다 — 설정이 반쯤 채워진 채로 주문을 보내면
    # 어느 서버로 갔는지 뒤에 못 가립니다.
    return str(name or "").strip() or None


def unknown_environment(api_client: Any) -> bool:
    """어느 환경으로 나갈지 못 읽으면 참. 이때는 주문을 보내지 않습니다.

    **모르는 채 보내는 것보다 안 보내고 알리는 편이 낫습니다.** 주문은
    되돌릴 수 없고, 어느 서버로 갔는지 모르면 확인할 자리도 못 정합니다.
    """
    return environment_of(api_client) is None


def mismatch(api_client: Any, claimed: str) -> str | None:
    """화면이 믿는 환경이 이 호출부의 환경과 다르면 화면에 적을 문구, 같으면 `None`.

    다를 때는 주문을 보내지 않습니다. 화면이 「모의」 배지를 띄운 채 낸 주문이
    실전으로 나가는 길을 여기서 막습니다. 환경을 못 읽는 경우는 여기 오기 전에
    `unknown_environment` 가 먼저 막습니다.
    """
    return _mismatch_of(environment_of(api_client), claimed)


def _mismatch_of(active: str | None, claimed: str) -> str | None:
    if active is None or active == claimed:
        return None
    return (
        f"화면의 환경({ENVIRONMENT_LABELS.get(claimed, claimed)})과 서버의 활성 환경"
        f"({ENVIRONMENT_LABELS.get(active, active)})이 달라 주문을 내지 않았습니다. "
        "화면을 새로 고친 뒤 다시 확인해 주세요."
    )


def refusal(api_client: Any, claimed: str) -> tuple[int, str] | None:
    """보내면 안 되는 주문이면 (상태 코드, 문구). 보내도 되면 `None`.

    차례가 뜻입니다. **전환 중(409)** 이 먼저입니다 — 그 사이에는 활성 환경이
    바뀌는 중이라 대조 결과 자체를 믿을 수 없습니다. 다음이 **못 읽음(500)**,
    마지막이 **불일치(400)** 입니다. 세 경우 모두 NH선물로는 아무것도 나가지
    않습니다.
    """
    return refusal_of(environment_of(api_client), claimed)


def refusal_of(active: str | None, claimed: str) -> tuple[int, str] | None:
    """`refusal` 의 판정을 **활성 환경 값 하나로** 합니다. 못 읽었으면 `None` 을 줍니다.

    전략 실행처럼 호출부 없이 활성 환경만 아는 자리도 **같은 판정**을 지나게
    하려고 둔 자리입니다 — 일반 주문과 전략 주문이 막는 규칙을 두 벌로 두지
    않습니다.
    """
    if runtime.switching():
        return 409, SWITCHING
    if not str(active or "").strip():
        return 500, UNKNOWN_ENVIRONMENT
    reason = _mismatch_of(str(active).strip(), claimed)
    if reason is not None:
        return 400, reason
    return None


def environment_headers(api_client: Any) -> dict[str, str]:
    """주문을 안 내고 돌려보내는 응답에 붙일 헤더. 서버의 활성 환경을 적습니다.

    화면은 이 값으로 배지를 바로잡을 수 있습니다. 환경을 못 읽으면 비어 있습니다.
    """
    active = environment_of(api_client)
    return {runtime.ENVIRONMENT_HEADER: active} if active else {}


def is_replay(api_client: Any) -> bool:
    """저장해 둔 응답으로 도는 중이면 참."""
    try:
        return bool(api_client.session.is_replay)
    except AttributeError:
        return False


def accepted(
    *,
    order_id: Any,
    market: str,
    environment: str | None,
    code: str,
    side: str,
    quantity: int | None,
    **extra: Any,
) -> dict[str, Any]:
    """접수된 주문 하나. 두 시장이 같은 낱말을 씁니다.

    **국내 응답에는 종목도 수량도 없습니다**(주문번호와 메시지 세 칸뿐).
    그래서 종목·방향·수량은 **요청에 실어 보낸 값을 되비쳐** 담습니다. 주소가
    시장을 정하는 것과 같은 자리입니다 — 서버가 안 준 값을 지어내는 것이 아니라,
    방금 무엇을 보냈는지 적는 것입니다.

    화면이 주문 결과를 목록 위에 얹을 때 쓰는 이름은 주문내역·실시간 알림과
    같습니다(`id`·`code`·`side`).

    **어느 환경으로 나간 주문인지 함께 냅니다.** 실전과 모의는 같은 화면에서
    같은 모양으로 접수되므로, 화면이 「실전 접수」와 「모의 접수」를 가려 적으려면
    이 칸이 있어야 합니다.
    """
    made: dict[str, Any] = {
        "accepted": True,
        "id": str(order_id or "").strip(),
        "market": market,
        "environment": environment,
        "code": code,
        "side": side,
    }
    if quantity is not None:
        made["quantity"] = quantity
    for key, value in extra.items():
        if value is not None and value != "":
            made[key] = value
    return made


def failed(code: str) -> bool:
    """그 코드가 **거절**을 뜻하는지. 비었거나 **전부 0 이면 아닙니다.**

    **정상 답에도 코드가 실려 옵니다.** 해외 주문은 접수됐을 때 `err_cd` 로
    `00000`(「주문이 정상처리 되었습니다.」)을 보냅니다 — 신규·정정·취소 모두
    그렇습니다. 「코드가 있으면 거절」로 읽으면
    **계좌에 들어간 주문을 거절이라고 답하게 됩니다.**

    자리 수를 안 세는 것은, 같은 뜻을 `0`·`0000` 처럼 다른 길이로 보내는 날
    같은 사고가 되풀이되지 않게 하려는 것입니다.
    """
    body = str(code or "").strip()
    return bool(body) and set(body) != {"0"}


def rejected(
    *, code: str, message: str, environment: str | None, kind: str = ""
) -> dict[str, Any]:
    """거절된 주문. **문구는 NH선물이 보낸 그대로입니다.**

    까닭은 이 파일 첫머리에 있습니다. 코드도 함께 내는 것은 화면이 문구를
    바꿔 적어야 할 날이 와도 같은 거절을 알아볼 수 있게 하기 위해서입니다.

    **서버가 문구를 안 보내면** 사전의 예비 문구를 씁니다(`error_codes`).
    차례는 **원문 → 사전 → 코드만**이고, 사전이 원문을 덮지 않습니다.

    `kind` 는 사용자가 무엇을 해야 하는지입니다. `needsDetail` 은 **그 코드
    하나에 서로 다른 사유가 붙어 오는 자리**라는 표지입니다 — 화면이 주문오류
    조회로 안내하면 됩니다.

    접수와 마찬가지로 **어느 환경에서 거절된 것인지** 함께 냅니다.
    """
    said = error_codes.describe(code, message)
    made: dict[str, Any] = {
        "accepted": False,
        "code": said["code"],
        "message": said["message"],
        "kind": kind or said["kind"],
        "environment": environment,
    }
    if error_codes.needs_detail(code):
        made["needsDetail"] = True
    return made


def paper_refusal(exc: GatewayError) -> HTTPException:
    """모의투자에서 막힌 **조회**의 답. 일반 거절과 같은 502 에 까닭을 헤더로 답니다.

    상태를 바꾸지 않는 것은 화면이 502 를 「다시 눌러도 같은 답」으로 읽기
    때문입니다 — 같은 거절을 되풀이해 부르지 않습니다. 무엇 때문인지는 헤더
    `x-nh-error-kind: paper-unavailable` 로 가릅니다. 두 시장의 조회 라우트가
    함께 씁니다.
    """
    return HTTPException(
        status_code=502,
        detail=PAPER_UNAVAILABLE_DETAIL,
        headers={
            ERROR_KIND_HEADER: error_codes.PAPER_UNAVAILABLE,
            ERROR_CODE_HEADER: exc.code,
        },
    )


#: 그 거래소의 시세를 쓰려면 시세 사용 신청이 먼저 필요할 때 화면에 서는 문장.
QUOTE_SUBSCRIPTION_DETAIL = "이 거래소는 시세 사용 신청이 필요합니다(HTS 9726 화면)"


def quote_subscription(exc: BaseException) -> bool:
    """시세 사용 신청이 안 된 거래소의 시세를 불렀다는 거절인지."""
    code = getattr(exc, "code", "")
    return error_codes.kind_of(code) == error_codes.QUOTE_SUBSCRIPTION


def quote_subscription_refusal(exc: BaseException) -> HTTPException:
    """시세 사용 신청이 필요한 거래소를 부른 **조회**의 답.

    일반 거절과 같은 502 이고, 무엇을 하면 되는지를 문장과 헤더
    `x-nh-error-kind: quote-subscription` 으로 알립니다. 신청하기 전에는 다시
    불러도 같은 답이라, 화면이 되풀이해 부르지 않고 안내를 세웁니다. 두 시장의
    조회 라우트가 함께 씁니다.
    """
    return HTTPException(
        status_code=502,
        detail=QUOTE_SUBSCRIPTION_DETAIL,
        headers={
            ERROR_KIND_HEADER: error_codes.QUOTE_SUBSCRIPTION,
            ERROR_CODE_HEADER: str(getattr(exc, "code", "") or ""),
        },
    )


def lookup_refusal(exc: BaseException, what: str, fallback: str) -> HTTPException:
    """**조회**가 거절됐을 때의 답(502). NH선물이 코드를 줬으면 코드와 문구를 싣습니다.

    문장은 `{what} 조회가 거절되었습니다 — {코드} {문구}` 입니다. 문구 차례는
    **원문 → 사전의 예비 문구 → 코드만**입니다(`error_codes.describe`). 코드를
    함께 내는 것은 같은 거절을 문의하거나 찾아볼 때 쓰는 열쇠가 코드이기
    때문입니다. 헤더 `x-nh-error-code`·`x-nh-error-kind` 에도 같은 코드와 갈래를
    답니다 — 모의투자에서 막힌 조회의 답과 같은 꼴입니다.

    **코드가 없으면 `fallback` 문장 그대로입니다.** 무엇이 막혔는지 모르는 채로
    지어내지 않습니다.
    """
    code, said = _refusal_of(exc)
    if not code:
        return HTTPException(status_code=502, detail=fallback)
    message = error_codes.describe(code, said)["message"]
    detail = f"{what} 조회가 거절되었습니다 — {code} {message}".rstrip()
    return HTTPException(
        status_code=502,
        detail=detail,
        headers={
            ERROR_KIND_HEADER: str(getattr(exc, "kind", "") or "") or error_codes.kind_of(code),
            ERROR_CODE_HEADER: code,
        },
    )


def _refusal_of(exc: BaseException) -> tuple[str, str]:
    """거절에 실려 온 (코드, 서버 문구). 없으면 빈 글자입니다.

    업무 거부·게이트웨이 거절은 코드와 문구를 칸으로 들고 옵니다. HTTP 오류는
    응답 본문의 `errorCode`·`errorMessage` 를 봅니다.
    """
    if isinstance(exc, (BusinessError, GatewayError)):
        return str(exc.code or "").strip(), str(exc.detail or "").strip()
    if isinstance(exc, HttpError):
        found = error_codes.failure_of(exc.body)
        if found is not None:
            return found["code"], found["message"]
        return "", ""
    return str(getattr(exc, "code", "") or "").strip(), str(getattr(exc, "detail", "") or "").strip()
