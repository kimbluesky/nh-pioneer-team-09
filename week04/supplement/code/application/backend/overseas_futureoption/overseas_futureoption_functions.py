"""해외 선물/옵션 REST API 함수.

NH선물 Open API 를 부르고 **응답을 손대지 않고 그대로** 돌려줍니다. 화면이
쓰는 모양으로 바꾸는 일은 `overseas_account.py` 가 합니다. 두 가지를 나눈
이유는, 원문이 무엇이었는지 확인할 자리가 한 곳은 있어야 하기 때문입니다.

함수 이름은 명세 문서 이름을 따릅니다. 호출 제한과 접속 토큰, 성공 여부
판단은 전부 공통 호출부(`backend.nh_api.client`)가 합니다.

    deposit            예수금조회
    open_interest      잔고내역
    order_history      주문내역
    orderable_quantity 주문가능수량
    order              주문·정정·취소 (하나로 묶여 있음)
    price              현재가 (여러 종목을 한 번에)
    bid_price          호가
    chart_minute       차트(분)
    chart_daily        차트(일)
    order_error        주문오류내역
    cumulative_pl      누적손익
    market_operations  장운영정보 (품목별 거래시간, 한국시간)
    product_detail     품목별세부정보 (계약단위·틱사이즈·틱값)
    product_expiry     만기일조회
"""
from __future__ import annotations

from typing import Any

from backend.nh_api.client import TradeClient
from backend.nh_api.runtime import client as default_client

# 환산구분. 통화를 나눠 볼지 하나로 합쳐 볼지.
BY_CURRENCY = "1"
CONVERTED = "2"

# 합쳐 볼 때 어느 통화로 합칠지의 기본값.
#
# 기본값(`KRW` 와 환산구분 `2`)으로 부르면 원화로 합친 답이 옵니다. 화면 머리의
# 「오늘 예탁자산」이 이 기준으로 서고, 추이도 이 기준으로만 쌓습니다
# (`backend/deposit_history.py`).
HOME_CURRENCY = "KRW"


def deposit(
    *,
    cur_cd: str = HOME_CURRENCY,
    cvs_div: str = CONVERTED,
    biz_dt: str = "",
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """예수금조회. 응답 원문 그대로.

    **통화와 환산구분이 둘 다 필수입니다.** 통화를 안 정하면 부를 수 없어
    기본값을 둡니다 — 합쳐 보기(`2`)로, 원화 기준입니다. 통화를 나눠 보려면
    `cvs_div="1"` 과 그 통화 코드를 함께 넘깁니다.

    기본값의 뜻은 위 `HOME_CURRENCY` 에 있습니다.

    `biz_dt` 는 명세상 선택입니다. 안 넘기면 서버가 정하는 날의 값이 옵니다.
    """
    api = api or default_client()
    payload: dict[str, Any] = {"cur_cd": cur_cd, "cvs_div": cvs_div}
    if biz_dt:
        payload["biz_dt"] = biz_dt
    return api.call("overseas_deposit", payload, environment=environment).body


# 주문내역 조회구분. **국내와 다른 체계입니다** — 국내는 체결구분(0 전체·
# 1 체결·2 미체결·3 미접수)이고, 해외는 아래처럼 「주문」이 따로 있고 미체결이
# 둘로 갈립니다.
ALL = "0"
ORDERS_ONLY = "1"
EXECUTED = "2"
PENDING = "3"
PENDING_CLOSE = "4"

# 매수매도구분.
BOTH_SIDES = "0"


def open_interest(
    *,
    next_key: str = "",
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """해외 잔고내역. 응답 원문 그대로.

    요청은 이어받기 키뿐입니다. `next_key` 는 처음에 빈 값입니다.
    """
    api = api or default_client()
    return api.call(
        "overseas_open-interest", {"nxt_key": next_key}, environment=environment
    ).body


#: 보유포지션만기조회를 처음 부를 때의 이어받기 키. 명세의 기본값이 「000」입니다
#: (설명 칸은 「공백」이라 적었지만, 서버가 받는 첫 쪽 키는 빈 값이 아닌 이 값입니다).
EXPIRY_FIRST_PAGE = "000"


def open_interest_expiry(
    *,
    und_sym: str = "",
    inq_dy_cnt: str = "",
    next_key: str = EXPIRY_FIRST_PAGE,
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """해외 보유포지션 만기 조회. 응답 원문 그대로.

    보유 중인 포지션마다 최초통보일·만기일·잔존일수와 인수도 결제 구분이 옵니다.

    `und_sym` 은 기초자산 품목코드(예 `6A`)로 거르는 값이고 비우면 전체입니다.
    `inq_dy_cnt` 는 잔존일 상한이고 비우면 서버 기본(2000일)입니다. 둘 다 빈 값이면
    안 보냅니다. `next_key` 는 처음에 `EXPIRY_FIRST_PAGE` 이고, 다음 쪽은 응답의
    키를 그대로 넘깁니다.
    """
    api = api or default_client()
    payload: dict[str, Any] = {"nxt_key": next_key or EXPIRY_FIRST_PAGE}
    if und_sym:
        payload["und_sym"] = und_sym
    if inq_dy_cnt:
        payload["inq_dy_cnt"] = inq_dy_cnt
    return api.call("overseas_open-interest-expiry", payload, environment=environment).body


def order_history(
    *,
    inq_div: str = ALL,
    trd_div_cd: str = BOTH_SIDES,
    prd_cd: str = "",
    next_key: str = "",
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """해외 주문내역. 응답 원문 그대로.

    **미체결도 여기서 받습니다.** `inq_div` 로 고릅니다 — `0` 전체, `1` 주문,
    `2` 체결, `3` 미체결, `4` 미체결(지정청산). 국내는 같은 뜻을 `exec_div`
    (0 전체·1 체결·2 미체결·3 미접수)로 받아 **이름도 값도 다릅니다.**

    `prd_cd`(품목코드)는 선택입니다. 빈 값이면 안 보냅니다.
    """
    api = api or default_client()
    payload: dict[str, Any] = {
        "inq_div": inq_div,
        "trd_div_cd": trd_div_cd,
        "nxt_key": next_key,
    }
    if prd_cd:
        payload["prd_cd"] = prd_cd
    return api.call("overseas_order-history", payload, environment=environment).body


# 해외주문유형. **주문가능수량을 부를 때 쓰는 값**입니다. 화면에 적는 글자는
# `overseas_account.ORDER_TYPE` 에 따로 있습니다 — 여기는 요청 코드입니다.
MARKET_PRICE = "1"
LIMIT_PRICE = "2"
STOP_MARKET = "3"
STOP_LIMIT = "4"

# STOP 가격이 있어야 하는 주문유형.
STOP_TYPES = (STOP_MARKET, STOP_LIMIT)


def orderable_quantity(
    sym: str,
    *,
    glb_ord_tp_cd: str,
    ord_pric: str,
    stop_pric: str = "",
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """해외 주문가능수량. 응답 원문 그대로.

    **방향을 안 물어봅니다** — 매수와 매도의 값이 한 응답에 함께 옵니다.
    국내는 매매구분을 받아 그 방향만 답하므로 부르는 법이 다릅니다.

    `ord_pric`(주문가격)은 명세상 필수이고 **시장가에도 값이 있어야 합니다.**
    STOP 두 유형은 `stop_pric`(스탑가격)이 함께 필요합니다.

    스탑가격 칸은 STOP 이 아닐 때도 **빈 값으로 실어 보냅니다** — 서버가 받는
    모양이 그렇습니다. 칸을 빼고 보내는 모양은 쓰지 않습니다.
    """
    api = api or default_client()
    return api.call(
        "overseas_orderable-quantity",
        {
            "sym": sym,
            "glb_ord_tp_cd": glb_ord_tp_cd,
            "ord_pric": ord_pric,
            "stop_pric": stop_pric,
        },
        environment=environment,
    ).body


# 호가구분코드. **해외는 신규·정정·취소가 API 하나입니다** — 국내가 여섯으로
# 나뉘어 있는 것과 다릅니다.
NEW = "1"
MODIFY = "2"
CANCEL = "3"

# 신규주문일 때 원주문번호 자리에 넣는 값. 명세의 기본값입니다.
NO_ORIGINAL = "0"


def order(
    sym: str,
    *,
    qt_div_cd: str,
    trd_div_cd: str,
    glb_ord_tp_cd: str,
    ord_qty: str,
    ord_pric: str = "",
    stop_pric: str = "",
    org_ord_no: str = NO_ORIGINAL,
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """해외 주문·정정·취소. 응답 원문 그대로.

    `qt_div_cd` 가 무엇을 할지 정합니다 — `1` 신규, `2` 정정, `3` 취소.
    정정·취소는 `org_ord_no`(원주문번호)가 필요하고, 신규는 `0` 입니다.

    가격 두 칸은 주문유형에 따라 채웁니다 — 시장가·STOP시장가면 주문가격이
    `0`, 시장가·지정가면 스탑가격이 빈 값입니다.

    **응답에 거부 사유가 `err_cd`·`err_msg` 로 옵니다.** 국내는 같은 자리를
    `msg_cd`·`msg_cntn` 으로 보내므로 두 시장을 한 자리에서 판정할 수 없습니다.

    **되돌릴 수 없는 자리입니다.** 응답을 못 받았을 때 다시 보내지 않습니다.
    """
    api = api or default_client()
    return api.call(
        "overseas_order",
        {
            "qt_div_cd": qt_div_cd,
            "sym": sym,
            "trd_div_cd": trd_div_cd,
            "glb_ord_tp_cd": glb_ord_tp_cd,
            "ord_qty": ord_qty,
            "ord_pric": ord_pric,
            "stop_pric": stop_pric,
            "org_ord_no": org_ord_no,
        },
        environment=environment,
    ).body


# 차트를 한 번에 받아 올 개수와 처음 부를 때 넣는 이어받기 키. 명세 기본값입니다.
CHART_ROWS = "300"
FIRST_PAGE = "99999999"

# 해외에는 연결선물이 없습니다. 명세가 그렇게 적습니다.
NO_CONTINUOUS = "0"

# 라우트가 한 요청에 받는 종목 수. **서버는 호출 한 번에 한 종목만 받으므로**
# 이만큼을 종목마다 한 번씩 불러 합칩니다 — 요청이 한없이 커지는 것을 막는
# 값입니다.
MAX_SYMBOLS = 100


def price(
    symbols: list[tuple[str, str]],
    *,
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """해외 현재가. 응답 원문 그대로.

    요청과 응답이 배열이지만 **서버는 한 번에 한 종목만 받습니다** — 명세에는
    없는 제약으로, 두 종목을 넣으면 같은 거래소의 멀쩡한 종목이라도 거절됩니다.
    여러 종목은 부르는 쪽이 한 종목씩 나눠 부릅니다.

    `symbols` 는 (거래소코드, 종목코드) 짝의 목록입니다. 거래소코드는 선물이면
    `F`, 옵션이면 `O` 에 거래소를 붙인 값입니다(`FCME`·`OCME`).
    """
    api = api or default_client()
    return api.call(
        "overseas_price",
        {
            "Iccurs1": [
                {"iccurs1_exch_cd": exch_cd, "iccurs1_sym": sym} for exch_cd, sym in symbols
            ]
        },
        environment=environment,
    ).body


def price_all_option(
    sym: str,
    *,
    exch_cd: str,
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """옵션전체시세. 응답 원문 그대로.

    `sym` 은 행사가 없는 시리즈 코드(`O_ESU26` — 옵션품목 + 월물 글자 + 연도
    두 자리), `exch_cd` 는 `O` 에 거래소를 붙인 값(`OCME`)입니다. 한 줄이 행사가
    하나이고 콜·풋이 함께 오며, 한 번에 다 옵니다(이어받기 없음).
    """
    api = api or default_client()
    return api.call(
        "overseas_price-all-option",
        {"exch_cd": exch_cd, "sym": sym},
        environment=environment,
    ).body


def bid_price(
    sym: str,
    *,
    exch_cd: str,
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """해외 호가. 응답 원문 그대로.

    **다섯 단계**가 평평한 칸으로 옵니다(`ask_pric_1`…). 국내처럼 배열로 오지
    않습니다. 현재가 한 벌도 함께 실려 오는데 화면은 현재가를 따로 부릅니다.
    """
    api = api or default_client()
    return api.call(
        "overseas_bid-price", {"exch_cd": exch_cd, "sym": sym}, environment=environment
    ).body


def chart_minute(
    sym: str,
    *,
    exch_cd: str,
    inq_strt_dt: str,
    inq_ed_dt: str,
    strt_tm: str = "000000",
    ed_tm: str = "235959",
    dcnt: str = "1",
    req_qty: str = CHART_ROWS,
    next_key: str = FIRST_PAGE,
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """해외 차트(분). 응답 원문 그대로.

    `dcnt` 는 몇 분을 한 봉으로 묶을지입니다(1이면 1분봉).

    **줄이 최신부터 옵니다.** 오래된 것부터로 뒤집는 일은
    `backend/quotes.py` 의 `bars()` 가 합니다.
    """
    api = api or default_client()
    return api.call(
        "overseas_chart-minute",
        {
            "exch_cd": exch_cd,
            "sym": sym,
            "inq_strt_dt": inq_strt_dt,
            "strt_tm": strt_tm,
            "inq_ed_dt": inq_ed_dt,
            "ed_tm": ed_tm,
            "req_qty": req_qty,
            "dcnt": dcnt,
            "cidx_yn": NO_CONTINUOUS,
            "nxt_key": next_key,
        },
        environment=environment,
    ).body


def chart_daily(
    sym: str,
    *,
    exch_cd: str,
    inq_strt_dt: str,
    inq_ed_dt: str,
    req_qty: str = CHART_ROWS,
    next_key: str = FIRST_PAGE,
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """해외 차트(일). 응답 원문 그대로.

    분봉과 같은 인자에서 시각과 봉 분만 없습니다. 줄이 최신부터 오는 것도
    같아서 뒤집는 일은 `backend/quotes.py` 의 `bars()` 가 합니다.
    """
    api = api or default_client()
    return api.call(
        "overseas_chart-daily",
        {
            "exch_cd": exch_cd,
            "sym": sym,
            "inq_strt_dt": inq_strt_dt,
            "inq_ed_dt": inq_ed_dt,
            "req_qty": req_qty,
            "cidx_yn": NO_CONTINUOUS,
            "nxt_key": next_key,
        },
        environment=environment,
    ).body


# 누적손익을 어느 통화로 환산해 볼지. **명세는 이 둘만 받습니다.**
CONVERT_KRW = "KRW"
CONVERT_USD = "USD"


def order_error(
    inq_dt: str,
    *,
    next_key: str = "",
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """해외 주문오류내역. 응답 원문 그대로.

    `inq_dt` 는 조회일자(`YYYYMMDD`)이고 명세상 필수입니다. 국내와 달리
    매수매도 구분으로 거르는 인자가 없습니다.
    """
    api = api or default_client()
    return api.call(
        "overseas_order-error",
        {"inq_dt": inq_dt, "nxt_key": next_key},
        environment=environment,
    ).body


def cumulative_pl(
    *,
    inq_strt_dt: str,
    inq_ed_dt: str,
    cvs_cur_cd: str = CONVERT_KRW,
    cur_cd: str = "",
    prd_cd: str = "",
    next_key: str = "",
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """해외 누적손익. 응답 원문 그대로.

    **기간은 실제 날짜로 주어야 합니다.** 명세는 시작일 `00000000` 을
    「처음부터」로 적지만 서버가 거절합니다 — 부르는 쪽이 날짜를 채웁니다
    (`overseas_routes.CumulativePlRequest`).

    `cvs_cur_cd` 는 **표시할 통화**입니다(원화 또는 달러). `cur_cd` 는 어느 통화의
    손익만 볼지 거르는 값이고 비우면 전체입니다 — 둘은 다른 자리입니다.

    `prd_cd`(품목코드)도 선택입니다. 빈 값이면 안 보냅니다.

    **줄의 마지막이 총합입니다.** 나누는 일은 `overseas_account.cumulative_pl()`
    이 합니다.
    """
    api = api or default_client()
    payload: dict[str, Any] = {
        "inq_strt_dt": inq_strt_dt,
        "inq_ed_dt": inq_ed_dt,
        "cvs_cur_cd": cvs_cur_cd,
        "nxt_key": next_key,
    }
    if cur_cd:
        payload["cur_cd"] = cur_cd
    if prd_cd:
        payload["prd_cd"] = prd_cd
    return api.call("overseas_cumulative-pl", payload, environment=environment).body


def market_operations(
    *,
    fut_opt_div_cd: str = "",
    prd_grp_cd: str = "",
    exch_cd: str = "",
    cur_cd: str = "",
    prd_cd: str = "",
    prd_nm: str = "",
    next_key: str = "",
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """장운영정보. 응답 원문 그대로.

    품목별 거래시간(1차·2차·익일)과 정산가 산정시간이 **한국시간**으로 옵니다.
    거르는 인자는 전부 비워도 되고, 비우면 전체입니다. **일곱 칸을 늘 다 싣습니다** —
    저장해 둔 응답이 이 모양으로 기록되어 있어, 칸을 빼고 보내면 재생 모드가
    같은 요청으로 알아보지 못합니다.

    한 번에 100줄까지 옵니다. 전체를 받으려면 `nxt_key` 로 이어 받거나 품목그룹
    (`prd_grp_cd` FG1~FG9)으로 나눠 부릅니다.
    """
    api = api or default_client()
    return api.call(
        "overseas_market-operations",
        {
            "cur_cd": cur_cd,
            "exch_cd": exch_cd,
            "fut_opt_div_cd": fut_opt_div_cd,
            "nxt_key": next_key,
            "prd_cd": prd_cd,
            "prd_grp_cd": prd_grp_cd,
            "prd_nm": prd_nm,
        },
        environment=environment,
    ).body


def product_detail(
    *,
    fut_opt_div_cd: str = "",
    prd_grp_cd: str = "",
    exch_cd: str = "",
    cur_cd: str = "",
    sett_cd: str = "",
    prd_cd: str = "",
    prd_nm: str = "",
    next_key: str = "",
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """품목별세부정보. 응답 원문 그대로.

    품목마다 계약단위·틱사이즈·틱값·인수도구분·적용일자가 옵니다. 월물이 아니라
    **품목** 단위입니다 — 종목코드가 아니라 품목코드(`6A`·`O_4TF`·`S_6E`)로 옵니다.

    거르는 인자는 전부 비워도 되고, 비우면 전체입니다. **여덟 칸을 늘 다 싣습니다** —
    저장해 둔 응답이 이 모양으로 기록되어 있어, 칸을 빼고 보내면 재생 모드가 같은
    요청으로 알아보지 못합니다.

    한 번에 100줄까지 옵니다. 그보다 많으면 `nxt_key` 가 채워져 오고, 그 값을
    다음 요청에 실어 이어 받습니다.
    """
    api = api or default_client()
    return api.call(
        "overseas_product-detail",
        {
            "cur_cd": cur_cd,
            "exch_cd": exch_cd,
            "fut_opt_div_cd": fut_opt_div_cd,
            "nxt_key": next_key,
            "prd_cd": prd_cd,
            "prd_grp_cd": prd_grp_cd,
            "prd_nm": prd_nm,
            "sett_cd": sett_cd,
        },
        environment=environment,
    ).body


# 만기일 조회의 잔존일 기준. 서버는 만기까지 남은 날이 이 값 이하인 월물만 냅니다.
# 명세의 기본은 30 인데, 품목정보는 먼 월물도 보여야 해서 크게 둡니다.
REMAINING_DAYS_ALL = "999"


def product_expiry(
    sym: str,
    *,
    fut_opt_div_cd: str = "",
    rem_dy: str = REMAINING_DAYS_ALL,
    next_key: str = "",
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """해외 만기일 조회. 응답 원문 그대로.

    종목마다 최초통보일·만기일(최종거래일)·최종결제일과 잔존일수가 옵니다.
    **날짜만 있고 만기일의 거래시간은 없습니다.**

    `rem_dy` 는 잔존일 기준입니다 — 만기까지 남은 날이 이 값 이하인 줄만 옵니다.
    명세의 요청 칸(기본 30)이고 서버가 그대로 거릅니다. `sym` 은 종목코드입니다 —
    품목코드나 빈 값을 넣었을 때의 답은 명세에 없습니다. `fut_opt_div_cd` 는 공백이 전체, F 선물·O 옵션·S 스프레드·
    Z 기타입니다.
    """
    api = api or default_client()
    return api.call(
        "overseas_product-expiry",
        {
            "fut_opt_div_cd": fut_opt_div_cd,
            "nxt_key": next_key,
            "rem_dy": rem_dy,
            "sym": sym,
        },
        environment=environment,
    ).body
