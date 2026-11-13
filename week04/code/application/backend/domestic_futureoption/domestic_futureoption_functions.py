"""국내 선물/옵션 REST API 함수.

NH선물 Open API 를 부르고 **응답을 손대지 않고 그대로** 돌려줍니다. 화면이
쓰는 모양으로 바꾸는 일은 `domestic_quotes.py` 가 합니다. 두 가지를 나눈
이유는, 원문이 무엇이었는지 확인할 자리가 한 곳은 있어야 하기 때문입니다.

함수 이름은 명세 문서 이름을 따릅니다. 호출 제한과 접속 토큰, 성공 여부
판단은 전부 공통 호출부(`backend.nh_api.client`)가 합니다.

    price              현재가
    price_all_futures  선물전체시세 (품목 하나의 월물 전부)
    price_all_option   옵션전체시세 (주간)
    price_all_option_night 옵션전체시세 (야간)
    trend_investor     투자자직전매매동향
    price_bid          호가
    chart_daily        차트(일)
    chart_minute       차트(분)
    price_night        현재가(야간)
    deposit            예수금조회
    open_interest      잔고내역
    order_history      주문내역
    order_error        주문오류내역
    orderable_quantity 주문가능수량
    order              신규주문 (주간·야간)
    modify             정정주문 (주간·야간)
    cancel             취소주문 (주간·야간)
    product_detail     품목별세부정보 (거래시간·최근월물 만기일)
"""
from __future__ import annotations

from typing import Any

from backend.nh_api.client import TradeClient
from backend.nh_api.runtime import client as default_client

# 차트를 한 번에 받아 올 개수. 화면이 처음 그릴 때 160봉을 쓰므로 넉넉히 둡니다.
CHART_ROWS = "300"

# 연속조회를 처음 부를 때 넣는 값. 이어 받을 때는 직전 응답의 키를 넣습니다.
FIRST_PAGE = "99999999"

# 주간인지 야간인지. 차트 조회가 이 값을 받습니다.
DAY = "1"
NIGHT = "2"


def price(sym: str, *, api: TradeClient | None = None, environment: str | None = None) -> dict[str, Any]:
    """현재가. 응답 원문 그대로."""
    api = api or default_client()
    return api.call("domestic_price", {"sym": sym}, environment=environment).body


def price_night(sym: str, *, api: TradeClient | None = None, environment: str | None = None) -> dict[str, Any]:
    """현재가(야간). 응답 원문 그대로."""
    api = api or default_client()
    return api.call("domestic_price-night", {"sym": sym}, environment=environment).body


# 투자자 매매동향을 수량으로 볼지 금액으로 볼지. 명세의 기본은 수량입니다.
QUANTITY = "1"
AMOUNT = "2"


def trend_investor(
    opt_prd: str,
    *,
    ref_dt: str,
    inq_tp: str = QUANTITY,
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """투자자직전매매동향. 응답 원문 그대로.

    **줄이 아니라 납작한 한 벌로 옵니다** — 투자자 무리마다 칸 이름에 접두사가
    붙습니다(`ind_buy`·`frgn_sel`…). 무리를 목록으로 세우는 일은
    `domestic_trend.py` 가 합니다.

    `opt_prd` 는 네 자리입니다 — **품목코드 두 자리 + 선물옵션구분 한 자리**
    (1 선물·2 콜·3 풋). `751` 이면 미국달러 선물입니다. `ref_dt` 는 기준일
    여덟 자리이고 명세상 필수입니다. `inq_tp` 는 수량인지 금액인지이고, 같은
    요청이라도 값이 자릿수째 달라집니다.
    """
    api = api or default_client()
    return api.call(
        "domestic_trend-investor",
        {"opt_prd": opt_prd, "ref_dt": ref_dt, "inq_tp": inq_tp},
        environment=environment,
    ).body


def price_all_futures(
    prd: str, *, api: TradeClient | None = None, environment: str | None = None
) -> dict[str, Any]:
    """선물전체시세. 응답 원문 그대로.

    **품목 하나의 월물이 한 번에 옵니다** — 줄 하나가 월물이고 현재가·전일대비·
    등락율·거래량·미결제약정이 함께 실려 옵니다. 월물마다 현재가를 따로 부르지
    않아도 되는 자리입니다.

    `prd` 는 두 자리 품목코드입니다(01 KOSPI·05 MKOSPI·06 KOSDAQ·75 USD·65 KTB
    등 스물둘). **비울 수 없습니다** — 명세에 필수이고 「전체」에 해당하는 값이
    없습니다. 이어 받기도 없습니다.
    """
    api = api or default_client()
    return api.call("domestic_price-all-futures", {"prd": prd}, environment=environment).body


def price_all_option(
    prd: str,
    *,
    exp_mon: str,
    nxt_stkr_pric: str = FIRST_PAGE,
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """옵션전체시세(주간). 응답 원문 그대로 — 한 쪽입니다.

    `prd` 는 두 자리 품목코드(01 코스피200·05 미니코스피200·06 코스닥150),
    `exp_mon` 은 만기일 여덟 자리의 뒤 여섯 자리입니다. 한 줄이 행사가 하나이고
    콜·풋이 함께 옵니다. 한 번에 다 못 주면 `nxt_stkr_pric` 에 다음 행사가가
    실려 오고, 그 값을 넣어 이어 받습니다 — 이어 받아 모으는 일은
    `domestic_chain.collect()` 가 합니다.
    """
    api = api or default_client()
    return api.call(
        "domestic_price-all-option",
        {"exp_mon": exp_mon, "nxt_stkr_pric": nxt_stkr_pric, "prd": prd},
        environment=environment,
    ).body


def price_all_option_night(
    prd: str,
    *,
    exp_mon: str,
    nxt_stkr_pric: str = FIRST_PAGE,
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """옵션전체시세(야간). 응답 원문 그대로 — 한 쪽입니다.

    **야간에는 시세가 오지 않습니다.** 종목코드·이름·전일종가·미결제약정과
    기초자산 값만 옵니다(명세). 인자는 주간과 같습니다.
    """
    api = api or default_client()
    return api.call(
        "domestic_price-all-option-night",
        {"exp_mon": exp_mon, "nxt_stkr_pric": nxt_stkr_pric, "prd": prd},
        environment=environment,
    ).body


def price_bid(
    sym: str,
    *,
    exch_cd: str = "KSP",
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """호가. 응답 원문 그대로.

    한 번에 열 단계가 오고, 그 종목에 없는 단계는 값이 0 으로 채워져 옵니다.
    """
    api = api or default_client()
    return api.call(
        "domestic_price-bid", {"sym": sym, "exch_cd": exch_cd}, environment=environment
    ).body


def chart_daily(
    sym: str,
    *,
    start_date: str,
    end_date: str,
    night: bool = False,
    next_key: str = FIRST_PAGE,
    rows: str = CHART_ROWS,
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """차트(일). 응답 원문 그대로.

    날짜는 `YYYYMMDD` 여덟 자리입니다.
    """
    api = api or default_client()
    return api.call(
        "domestic_chart-daily",
        {
            "sym": sym,
            "inq_strt_dt": start_date,
            "inq_ed_dt": end_date,
            "nght_tp": NIGHT if night else DAY,
            "cidx_yn": "0",
            "fut_cidx_exp_mod": "0",
            "nxt_key": next_key,
            "req_qty": rows,
        },
        environment=environment,
    ).body


def chart_minute(
    sym: str,
    *,
    start_date: str,
    end_date: str,
    start_time: str = "000000",
    end_time: str = "235959",
    minutes: str = "1",
    night: bool = False,
    next_key: str = FIRST_PAGE,
    rows: str = CHART_ROWS,
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """차트(분). 응답 원문 그대로.

    `minutes` 는 봉 하나가 몇 분인지입니다. 시각은 `HHMMSS` 여섯 자리입니다.
    """
    api = api or default_client()
    return api.call(
        "domestic_chart-minute",
        {
            "sym": sym,
            "inq_strt_dt": start_date,
            "inq_ed_dt": end_date,
            "strt_tm": start_time,
            "ed_tm": end_time,
            "dcnt": minutes,
            "nght_tp": NIGHT if night else DAY,
            "cidx_yn": "0",
            "fut_cidx_exp_mod": "0",
            "nxt_key": next_key,
            "req_qty": rows,
        },
        environment=environment,
    ).body


def deposit(
    biz_dt: str,
    *,
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """예수금조회. 응답 원문 그대로.

    `biz_dt` 는 영업일자(`YYYYMMDD`)이고 명세상 필수입니다. 어느 날을 물을지는
    부르는 쪽이 정합니다 — 오늘이 영업일이 아닐 수 있어 여기서 정하지 않습니다.
    """
    api = api or default_client()
    return api.call("domestic_deposit", {"biz_dt": biz_dt}, environment=environment).body


def open_interest(
    *,
    next_key: str = "",
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """잔고내역. 응답 원문 그대로.

    **주간과 야간 줄이 한 응답에 섞여 옵니다.** 줄의 거래소코드가 어느 장의
    것인지 알려 줍니다.

    `next_key` 는 이어 받을 때 직전 응답의 값을 넣습니다. 처음에는 빈 값입니다 —
    시세 조회의 첫 장 표시(`FIRST_PAGE`)와 값이 다르니 옮겨 쓰지 마세요.
    """
    api = api or default_client()
    return api.call(
        "domestic_open-interest", {"nxt_key": next_key}, environment=environment
    ).body


# 주문내역 조회 조건. 명세에 뜻이 적혀 있는 값입니다.
ALL = "0"
EXECUTED = "1"
PENDING = "2"
NOT_ACCEPTED = "3"

DAY_SESSION = "1"
NIGHT_SESSION = "2"


def order_history(
    *,
    exec_div: str = ALL,
    trd_div: str = ALL,
    trd_tm_div_cd: str = ALL,
    ord_div_cd: str = ALL,
    next_key: str = "",
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """주문내역. 응답 원문 그대로.

    **미체결도 여기서 받습니다.** 미체결 조회는 따로 없고 이 하나로
    받습니다. `exec_div` 로 고릅니다 — `0` 전체, `1` 체결, `2` 미체결,
    `3` 미접수.

    나머지 셋도 명세상 필수라 기본값을 「전체」로 둡니다. `trd_div` 는 매매구분
    (1 매수·2 매도), `trd_tm_div_cd` 는 거래시간(1 주간·2 야간), `ord_div_cd` 는
    주문구분(1 거부·2 오류)입니다.
    """
    api = api or default_client()
    return api.call(
        "domestic_order-history",
        {
            "exec_div": exec_div,
            "trd_div": trd_div,
            "trd_tm_div_cd": trd_tm_div_cd,
            "ord_div_cd": ord_div_cd,
            "nxt_key": next_key,
        },
        environment=environment,
    ).body


def order_error(
    inq_dt: str,
    *,
    trd_div_cd: str = ALL,
    next_key: str = "",
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """주문오류내역. 응답 원문 그대로.

    `inq_dt` 는 조회일자(`YYYYMMDD`)이고 명세상 필수입니다. `trd_div_cd` 는
    매수매도구분(0 전체·1 매수·2 매도)입니다.

    **이어 받기 항목이 다른 조회와 다릅니다** — 응답에 전체 건수와 다음 여부가
    함께 옵니다. 바꾸는 일은 `domestic_account.order_errors()` 가 합니다.
    """
    api = api or default_client()
    return api.call(
        "domestic_order-error",
        {"inq_dt": inq_dt, "trd_div_cd": trd_div_cd, "nxt_key": next_key},
        environment=environment,
    ).body


# 처리구분. 명세는 넷을 적어 두었지만 이 프로그램은 **개인 계좌만** 다뤄
# 일반 둘만 씁니다(법인은 2·4).
DAY_RETAIL = "1"
NIGHT_RETAIL = "3"

# 가능수량계산구분. 1 은 금액만, 2 는 금액과 수량입니다. 화면이 수량을
# 물어보는 자리라 늘 2 로 부릅니다.
#
# **명세는 필수라고 적었는데 이 칸 없이 부른 응답도 수량까지 옵니다.**
# 그래도 명세대로 실어 보냅니다 — 서버가 언제 기본값을 바꿔도 이 요청은
# 뜻이 그대로입니다.
WITH_QUANTITY = "2"


def orderable_quantity(
    sym: str,
    *,
    trd_div: str,
    ord_pric: str,
    ord_sts: str = DAY_RETAIL,
    rt_tp: str = WITH_QUANTITY,
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """주문가능수량. 응답 원문 그대로.

    **매수와 매도를 따로 물어봅니다** — `trd_div` 가 1 이면 매수, 2 면 매도이고
    응답도 그 방향의 값입니다. 해외는 한 번에 양쪽을 다 주므로 두 시장의
    부르는 법이 다릅니다.

    `ord_pric`(주문가격)은 명세상 필수입니다. 시장가에도 값이 있어야 하므로
    화면이 쓰는 가격을 그대로 넣습니다. 문자열로 받는 것은 **화면에 보이는
    글자를 그대로 보내기 위해서**입니다 — 숫자로 바꿔 담으면 자릿수가 달라져
    보낸 값과 보이는 값이 어긋날 수 있습니다.
    """
    api = api or default_client()
    return api.call(
        "domestic_orderable-quantity",
        {
            "ord_sts": ord_sts,
            "rt_tp": rt_tp,
            "sym": sym,
            "trd_div": trd_div,
            "ord_pric": ord_pric,
        },
        environment=environment,
    ).body


# 주문유형코드.
LIMIT = "1"
MARKET_PRICE = "2"
CONDITIONAL = "3"
BEST = "4"

# 값을 안 받는 주문유형. **명세가 이때 주문가격을 0 으로 받습니다** — 저장해
# 둔 모의 주문 둘도 시장가에 `0` 으로 실려 있습니다.
ZERO_PRICE_TYPES = (MARKET_PRICE, BEST)

# 체결조건구분코드.
FAS = "1"
FOK = "2"
FAK = "3"

# 주간·야간이 **API 자체가 다릅니다.** 같은 이름에 `-night` 가 붙습니다.
_NIGHT = "-night"


def _order_api(name: str, night: bool) -> str:
    return f"{name}{_NIGHT}" if night else name


def order(
    sym: str,
    *,
    trd_div: str,
    ord_tp_cd: str,
    ord_qty: str,
    ord_pric: str,
    exec_cnd_cd: str = FAS,
    night: bool = False,
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """신규주문. 응답 원문 그대로.

    **되돌릴 수 없는 자리입니다.** 시간이 초과되어 응답을 못 받으면 접수됐는지
    알 수 없으므로, 같은 주문을 다시 보내기 전에 주문내역으로 확인해야 합니다.
    여기서 다시 보내지 않습니다.

    응답은 세 칸입니다 — 주문번호와 메시지 코드·내용. **성공이면 메시지 두
    칸이 빈 문자열로 옵니다**(저장해 둔 모의 주문 둘에서 확인).
    """
    api = api or default_client()
    return api.call(
        _order_api("domestic_order", night),
        {
            "sym": sym,
            "trd_div": trd_div,
            "ord_tp_cd": ord_tp_cd,
            "ord_qty": ord_qty,
            "ord_pric": ord_pric,
            "exec_cnd_cd": exec_cnd_cd,
        },
        environment=environment,
    ).body


def modify(
    sym: str,
    *,
    org_ord_no: str,
    trd_div: str,
    ord_tp_cd: str,
    ord_qty: str,
    ord_pric: str,
    exec_cnd_cd: str = FAS,
    night: bool = False,
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """정정주문. 응답 원문 그대로.

    `ord_qty` 는 **정정수량**이고 원주문의 총수량보다 클 수 없습니다.
    `org_ord_no`(원주문번호)는 앞자리를 0 으로 채운 열 자리입니다 —
    채우는 일은 `backend.orders.order_no()` 가 합니다.
    """
    api = api or default_client()
    return api.call(
        _order_api("domestic_modify", night),
        {
            "sym": sym,
            "trd_div": trd_div,
            "ord_tp_cd": ord_tp_cd,
            "ord_qty": ord_qty,
            "ord_pric": ord_pric,
            "exec_cnd_cd": exec_cnd_cd,
            "org_ord_no": org_ord_no,
        },
        environment=environment,
    ).body


def cancel(
    sym: str,
    *,
    org_ord_no: str,
    trd_div: str,
    ord_qty: str,
    night: bool = False,
    api: TradeClient | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """취소주문. 응답 원문 그대로.

    **고칠 값이 없어 요청 칸이 넷뿐입니다** — 종목·매매구분·취소수량·원주문번호.
    취소수량도 원주문의 총수량보다 클 수 없습니다.
    """
    api = api or default_client()
    return api.call(
        _order_api("domestic_cancel", night),
        {
            "sym": sym,
            "trd_div": trd_div,
            "ord_qty": ord_qty,
            "org_ord_no": org_ord_no,
        },
        environment=environment,
    ).body


def product_detail(
    *, api: TradeClient | None = None, environment: str | None = None
) -> dict[str, Any]:
    """품목별세부정보. 응답 원문 그대로.

    받는 인자가 없고, 주간·야간 줄이 한 번에 옵니다. 거래시간·단일가 시간·
    최종거래일 거래시간·최근월물 만기일·증거금이 품목마다 한 줄씩입니다.
    """
    api = api or default_client()
    return api.call("domestic_product-detail", {}, environment=environment).body
