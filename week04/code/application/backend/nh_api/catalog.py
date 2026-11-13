"""NH선물 Open API 목록 — 호출 경로, HTTP 방식, 호출 제한 등급.

각 API 명세의 「기본 정보」 표를 그대로 옮긴 것입니다. 이름은 NH선물 원문을 씁니다.

호출 제한은 등급으로 묶여 있습니다. 시세와 계좌가 초당 10건, 주문이 5건,
인증이 3건이며, 실시간은 건수가 아니라 동시 구독 20개로 제한됩니다.

LME 는 담지 않습니다. 설정 파일의 시장 항목이 국내와 해외뿐이라
접속 토큰을 받을 자리가 없습니다.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

DOMESTIC = "domestic"
OVERSEAS = "overseas"

# 접속 토큰 발급처럼 시장을 가리지 않는 API 입니다. 호출할 때는 어느 시장의
# APP Key 로 받은 토큰을 쓸지 호출하는 쪽이 정합니다.
COMMON = "common"


class Limit(str, Enum):
    """호출 제한 등급."""

    QUOTE = "quote"
    ACCOUNT = "account"
    ORDER = "order"
    AUTH = "auth"


# 등급별 초당 허용 건수.
LIMIT_PER_SECOND: dict[Limit, int] = {
    Limit.QUOTE: 10,
    Limit.ACCOUNT: 10,
    Limit.ORDER: 5,
    Limit.AUTH: 3,
}

# 실시간은 초당 건수가 아니라 동시에 유지할 수 있는 구독 수로 제한됩니다.
STREAM_SUBSCRIPTION_LIMIT = 20


# 계좌와 주문에 관한 API 입니다. 응답에 예수금·잔고·손익·주문 내역처럼 그
# 계좌에서만 나오는 값이 담깁니다. 나머지는 누가 물어도 같은 값을 받는
# 시장 정보입니다.
#
# **이 구분은 명세에 있는 것이 아니라 이 프로그램에서 나눈 것입니다.** 호출
# 제한 등급과도 다릅니다 — 품목별 세부정보와 장운영정보는 계좌 등급으로
# 제한되지만 담기는 값은 시장 정보입니다.
ACCOUNT_APIS = frozenset(
    {
        "domestic_order",
        "domestic_modify",
        "domestic_cancel",
        "domestic_order-night",
        "domestic_modify-night",
        "domestic_cancel-night",
        "domestic_orderable-quantity",
        "domestic_open-interest",
        "domestic_order-error",
        "domestic_order-history",
        "domestic_deposit",
        "overseas_order",
        "overseas_orderable-quantity",
        "overseas_order-error",
        "overseas_order-history",
        "overseas_open-interest",
        "overseas_open-interest-expiry",
        "overseas_deposit",
        "overseas_cumulative-pl",
        "O1_K",
        "O1",
    }
)


@dataclass(frozen=True)
class ApiSpec:
    """HTTP 로 부르는 API 하나."""

    api_id: str
    name: str
    market: str
    method: str
    path: str
    limit: Limit

    @property
    def is_account(self) -> bool:
        """계좌나 주문에 관한 API 인지."""
        return self.api_id in ACCOUNT_APIS


@dataclass(frozen=True)
class StreamSpec:
    """실시간으로 받는 API 하나.

    `action_code` 는 구독 메시지 본문에 넣는 값입니다. 국내 주문/체결은
    이름이 `O1_K` 이지만 본문에 넣는 값은 `O1` 이라 서로 다릅니다.
    """

    api_id: str
    name: str
    market: str
    action_code: str

    @property
    def is_account(self) -> bool:
        """계좌나 주문에 관한 API 인지."""
        return self.api_id in ACCOUNT_APIS


# 실시간 접속 경로. 발급받은 웹소켓 접속키를 질의 문자열로 붙입니다.
STREAM_PATH = "/trade/ws-stream"

HTTP_APIS: dict[str, ApiSpec] = {
    spec.api_id: spec
    for spec in (
        ApiSpec("token", "접근토큰 발급", COMMON, "POST", "/auth-service/v1/token", Limit.AUTH),
        ApiSpec("revoke", "접근토큰 폐기", COMMON, "POST", "/auth-service/v1/revoke", Limit.AUTH),
        ApiSpec("web-socket-key", "웹소켓 키 발급", COMMON, "GET", "/permission/v1/web-socket-key", Limit.AUTH),
        ApiSpec("domestic_price", "국내_현재가", DOMESTIC, "POST", "/trade/v1/domestic/price", Limit.QUOTE),
        ApiSpec("domestic_price-bid", "국내_호가", DOMESTIC, "POST", "/trade/v1/domestic/price-bid", Limit.QUOTE),
        ApiSpec("domestic_chart-daily", "국내_차트(일)", DOMESTIC, "POST", "/trade/v1/domestic/chart-daily", Limit.QUOTE),
        ApiSpec("domestic_chart-minute", "국내_차트(분)", DOMESTIC, "POST", "/trade/v1/domestic/chart-minute", Limit.QUOTE),
        ApiSpec("domestic_price-all-futures", "국내_선물전체시세", DOMESTIC, "POST", "/trade/v1/domestic/price-all-futures", Limit.QUOTE),
        ApiSpec("domestic_price-all-option", "국내_옵션전체시세", DOMESTIC, "POST", "/trade/v1/domestic/price-all-option", Limit.QUOTE),
        ApiSpec("domestic_trend-investor", "국내_투자자직전매매동향", DOMESTIC, "POST", "/trade/v1/domestic/trend-investor", Limit.QUOTE),
        ApiSpec("domestic_price-expect-fill", "국내_종목예상체결가", DOMESTIC, "POST", "/trade/v1/domestic/price-expect-fill", Limit.QUOTE),
        ApiSpec("domestic_product-detail", "국내_품목별세부정보", DOMESTIC, "POST", "/trade/v1/domestic/product-detail", Limit.ACCOUNT),
        ApiSpec("domestic_price-night", "국내_현재가(야간)", DOMESTIC, "POST", "/trade/v1/domestic/price-night", Limit.QUOTE),
        ApiSpec("domestic_price-all-option-night", "국내_옵션전체시세(야간)", DOMESTIC, "POST", "/trade/v1/domestic/price-all-option-night", Limit.QUOTE),
        ApiSpec("domestic_order", "국내_신규주문", DOMESTIC, "POST", "/trade/v1/domestic/order", Limit.ORDER),
        ApiSpec("domestic_modify", "국내_정정주문", DOMESTIC, "POST", "/trade/v1/domestic/modify", Limit.ORDER),
        ApiSpec("domestic_cancel", "국내_취소주문", DOMESTIC, "POST", "/trade/v1/domestic/cancel", Limit.ORDER),
        ApiSpec("domestic_orderable-quantity", "국내_주문가능수량", DOMESTIC, "POST", "/trade/v1/domestic/orderable-quantity", Limit.ACCOUNT),
        ApiSpec("domestic_open-interest", "국내_잔고내역", DOMESTIC, "POST", "/trade/v1/domestic/open-interest", Limit.ACCOUNT),
        ApiSpec("domestic_order-error", "국내_주문오류내역", DOMESTIC, "POST", "/trade/v1/domestic/order-error", Limit.ACCOUNT),
        ApiSpec("domestic_order-history", "국내_주문내역", DOMESTIC, "POST", "/trade/v1/domestic/order-history", Limit.ACCOUNT),
        ApiSpec("domestic_deposit", "국내_예수금조회", DOMESTIC, "POST", "/trade/v1/domestic/deposit", Limit.ACCOUNT),
        ApiSpec("domestic_order-night", "국내_신규주문(야간)", DOMESTIC, "POST", "/trade/v1/domestic/order-night", Limit.ORDER),
        ApiSpec("domestic_modify-night", "국내_정정주문(야간)", DOMESTIC, "POST", "/trade/v1/domestic/modify-night", Limit.ORDER),
        ApiSpec("domestic_cancel-night", "국내_취소주문(야간)", DOMESTIC, "POST", "/trade/v1/domestic/cancel-night", Limit.ORDER),
        ApiSpec("overseas_price", "해외_현재가", OVERSEAS, "POST", "/trade/v1/overseas/price", Limit.QUOTE),
        ApiSpec("overseas_bid-price", "해외_호가", OVERSEAS, "POST", "/trade/v1/overseas/bid-price", Limit.QUOTE),
        ApiSpec("overseas_chart-daily", "해외_차트(일)", OVERSEAS, "POST", "/trade/v1/overseas/chart-daily", Limit.QUOTE),
        ApiSpec("overseas_chart-minute", "해외_차트(분)", OVERSEAS, "POST", "/trade/v1/overseas/chart-minute", Limit.QUOTE),
        ApiSpec("overseas_price-all-option", "해외_옵션전체시세", OVERSEAS, "POST", "/trade/v1/overseas/price-all-option", Limit.QUOTE),
        ApiSpec("overseas_product-detail", "해외_품목별세부정보", OVERSEAS, "POST", "/trade/v1/overseas/product-detail", Limit.ACCOUNT),
        ApiSpec("overseas_market-operations", "해외_장운영정보", OVERSEAS, "POST", "/trade/v1/overseas/market-operations", Limit.ACCOUNT),
        ApiSpec("overseas_order", "해외_주문", OVERSEAS, "POST", "/trade/v1/overseas/order", Limit.ORDER),
        ApiSpec("overseas_orderable-quantity", "해외_주문가능수량", OVERSEAS, "POST", "/trade/v1/overseas/orderable-quantity", Limit.ACCOUNT),
        ApiSpec("overseas_order-error", "해외_주문오류내역", OVERSEAS, "POST", "/trade/v1/overseas/order-error", Limit.ACCOUNT),
        ApiSpec("overseas_order-history", "해외_주문내역", OVERSEAS, "POST", "/trade/v1/overseas/order-history", Limit.ACCOUNT),
        ApiSpec("overseas_open-interest", "해외_잔고내역", OVERSEAS, "POST", "/trade/v1/overseas/open-interest", Limit.ACCOUNT),
        ApiSpec("overseas_deposit", "해외_예수금조회", OVERSEAS, "POST", "/trade/v1/overseas/deposit", Limit.ACCOUNT),
        ApiSpec("overseas_product-expiry", "해외_만기일조회", OVERSEAS, "POST", "/trade/v1/overseas/product-expiry", Limit.ACCOUNT),
        ApiSpec("overseas_open-interest-expiry", "해외_보유포지션만기조회", OVERSEAS, "POST", "/trade/v1/overseas/open-interest-expiry", Limit.ACCOUNT),
        ApiSpec("overseas_cumulative-pl", "해외_누적손익현황", OVERSEAS, "POST", "/trade/v1/overseas/cumulative-pl", Limit.ACCOUNT),
    )
}

STREAM_APIS: dict[str, StreamSpec] = {
    spec.api_id: spec
    for spec in (
        StreamSpec("KA", "국내_시세체결", DOMESTIC, "KA"),
        StreamSpec("KB", "국내_시세호가", DOMESTIC, "KB"),
        StreamSpec("KC", "국내_시세체결(야간)", DOMESTIC, "KC"),
        StreamSpec("KD", "국내_시세호가(야간)", DOMESTIC, "KD"),
        StreamSpec("O1_K", "국내_주문/체결", DOMESTIC, "O1"),
        StreamSpec("FA", "해외_시세체결", OVERSEAS, "FA"),
        StreamSpec("FB", "해외_시세호가", OVERSEAS, "FB"),
        StreamSpec("O1", "해외_주문/체결", OVERSEAS, "O1"),
    )
}


def http_spec(api_id: str) -> ApiSpec:
    """HTTP API 하나의 명세를 찾습니다."""
    try:
        return HTTP_APIS[api_id]
    except KeyError:
        raise ValueError(f"모르는 API 입니다: {api_id!r}") from None


def stream_spec(api_id: str) -> StreamSpec:
    """실시간 API 하나의 명세를 찾습니다."""
    try:
        return STREAM_APIS[api_id]
    except KeyError:
        raise ValueError(f"모르는 실시간 API 입니다: {api_id!r}") from None


def is_known(api_id: str) -> bool:
    """이름을 아는 API 인지."""
    return api_id in HTTP_APIS or api_id in STREAM_APIS


def is_account_api(api_id: str) -> bool:
    """계좌나 주문에 관한 API 인지. 모르는 이름은 계좌로 봅니다."""
    if api_id in ACCOUNT_APIS:
        return True
    if is_known(api_id):
        return False
    # 아직 이름을 모르는 API 는 계좌 값이 담길 수 있다고 보고 다룹니다.
    return True
