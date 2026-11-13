"""`/api/domestic/…` 라우트 — 국내 시세.

주소는 NH선물 API 이름을 그대로 씁니다(`/api/{시장}/{API 이름}`). 응답은
원문이 아니라 **화면이 쓰는 모양**입니다 — 무엇을 어떻게 바꾸는지는
`domestic_quotes.py` 첫머리에 있습니다.

**오류는 화면용 문구로 냅니다.** 화면에는 무엇을 못 했는지를 알립니다.
NH선물이 오류 코드를 주면 그 코드와 서버 문구를 문장에 함께 싣습니다
(`orders.lookup_refusal`). 코드가 없는 실패는 서버 문구를 기록에만 남깁니다 —
요청에 실어 보낸 값이 되비쳐 브라우저의 네트워크 탭에 남지 않게 하려는 것입니다.

예외가 둘 있습니다. 모의투자에서 제공하지 않는 조회(`EGW053`)와 시세 사용 신청이
필요한 거래소(`ETR010`)는 서버 문구 대신 정해진 문장을 내고, 까닭을 헤더
`x-nh-error-kind` 로 답니다(`orders.paper_refusal`·`orders.quote_subscription_refusal`).

    504  응답을 받지 못했습니다 (연결 실패·시간 초과)
    502  NH선물이 요청을 거절했거나 업무 처리가 거부되었습니다
    503  시장의 참고 자료를 받을 인증 정보가 어느 환경에도 없습니다
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator, model_validator

from backend import market_values, order_error_days
from backend.domestic_futureoption import domestic_account
from backend.domestic_futureoption import domestic_chain
from backend.domestic_futureoption import domestic_history as history
from backend.domestic_futureoption import domestic_hours
from backend.domestic_futureoption import domestic_futureoption_functions as api
from backend.domestic_futureoption import domestic_quotes
from backend.domestic_futureoption import domestic_trend
from backend import deposit_history, order_outcome, orders, quotes
from backend.config import ENVIRONMENT_PATTERN, ConfigError
from backend.nh_api import runtime as nh_runtime
from backend.nh_api.client import TradeClient, last_environment
from backend.nh_api.reference import (
    MISSING_KEYS_STATUS,
    MissingMarketKeys,
    reference_environment,
)
from backend.nh_api.errors import (
    BusinessError,
    GatewayError,
    HttpError,
    NHApiError,
    TransportError,
    paper_unavailable,
)
from backend.nh_api import error_codes
from backend.nh_api.replay import MATCH_HEADER, RECORD_HEADER, SYMBOL_HEADER, last_replay
from backend.nh_api.runtime import client as default_client
from backend.master import master_index, master_service
from backend.sessions import service as session_service
from backend.sessions.hours import iso, now_kst

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/domestic", tags=["domestic"])

# 화면에 적는 실패 문구. **시세와 계좌를 가릅니다** — 예수금을 못 받았는데
# 「시세를 받지 못했습니다」가 서면, 사용자는 종목을 다시 고르거나 시세 화면을
# 들여다보게 됩니다. 무엇을 못 했는지가 다르면 할 일도 다릅니다.
_NOT_RECEIVED = "시세를 받지 못했습니다. 잠시 뒤 다시 시도해 주세요."
_REJECTED = "시세 조회가 거절되었습니다. 종목과 조회 조건을 확인해 주세요."

_ACCOUNT_NOT_RECEIVED = "계좌 정보를 받지 못했습니다. 잠시 뒤 다시 시도해 주세요."
_ACCOUNT_REJECTED = "계좌 조회가 거절되었습니다. 조회 조건을 확인해 주세요."

_HOURS_NOT_RECEIVED = "거래시간을 받지 못했습니다. 잠시 뒤 다시 시도해 주세요."
_HOURS_REJECTED = "거래시간 조회가 거절되었습니다. 잠시 뒤 다시 시도해 주세요."

# 조회 갈래마다 실패 문구와 기록에 적는 말. 무엇을 못 했는지가 다르면 할 일도 다릅니다.
_FAILURES = {
    "quote": (_NOT_RECEIVED, _REJECTED, "시세"),
    "account": (_ACCOUNT_NOT_RECEIVED, _ACCOUNT_REJECTED, "계좌"),
    "hours": (_HOURS_NOT_RECEIVED, _HOURS_REJECTED, "거래시간"),
}


def get_client() -> TradeClient:
    """앱이 쓰는 호출부. 테스트는 이 의존성을 바꿔 끼웁니다."""
    return default_client()


class SymbolRequest(BaseModel):
    """종목 하나를 묻는 요청."""

    sym: str = Field(min_length=1, description="종목 표준 코드")


class BookRequest(SymbolRequest):
    exch_cd: str = "KSP"


class ChartRequest(SymbolRequest):
    """차트 조회. 날짜는 `YYYYMMDD` 여덟 자리입니다."""

    inq_strt_dt: str = Field(min_length=8, max_length=8)
    inq_ed_dt: str = Field(min_length=8, max_length=8)
    night: bool = False
    nxt_key: str = api.FIRST_PAGE


class MinuteChartRequest(ChartRequest):
    """차트(분). `session` 을 주면 `night` 보다 앞섭니다.

    `both` 는 주간과 야간을 **같은 날짜 범위로 두 번** 불러 한 벌로 냅니다. 야간 봉의
    날짜는 거래일이 아니라 달력 날짜(18:00~23:59 는 그날, 00:00~06:00 은 다음 날)라
    시각순으로 세우기만 하면 순서가 맞습니다.
    """

    strt_tm: str = "000000"
    ed_tm: str = "235959"
    dcnt: str = "1"
    session: str | None = Field(default=None, pattern=r"^(day|night|both)$")

    @property
    def sessions(self) -> tuple[bool, ...]:
        """부를 세션들. 야간이면 참. `both` 면 주간 다음 야간입니다."""
        if self.session == "both":
            return (False, True)
        if self.session is not None:
            return (self.session == "night",)
        return (self.night,)


@router.post("/price")
def read_price(
    ask: SymbolRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """현재가."""
    raw = _call(lambda: api.price(ask.sym, api=api_client), response)
    return domestic_quotes.quote(raw, asked=ask.sym, spread=_is_spread(ask.sym))


@router.post("/price-night")
def read_price_night(
    ask: SymbolRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """현재가(야간). 야간에는 현물가가 오지 않아 그 자리가 비어 옵니다."""
    raw = _call(lambda: api.price_night(ask.sym, api=api_client), response)
    return domestic_quotes.quote(raw, asked=ask.sym, spread=_is_spread(ask.sym))


def _is_spread(sym: str) -> bool:
    """종목 마스터가 그 종목을 스프레드라고 하는지. **마스터가 모르면 거짓**입니다 —
    코드 모양으로 짐작하지 않습니다(짐작이 틀리면 선물의 값이 지워집니다)."""
    try:
        row = master_service.service.locate(session_service.DOMESTIC, sym)
    except Exception:  # noqa: BLE001 — 마스터를 못 읽었다고 현재가를 막지 않습니다.
        logger.warning("종목 %s 의 종류를 마스터에서 읽지 못했습니다.", sym, exc_info=True)
        return False
    return bool(row) and row.get("kind") == master_index.SPREAD


class AllFuturesRequest(BaseModel):
    """선물전체시세 조회. 품목코드 두 자리 하나뿐입니다.

    **비울 수 없습니다** — 명세에 필수이고 「전체」에 해당하는 값이 없습니다.
    어느 품목인지는 화면이 정합니다(옵션 체인과 같은 자리). 백엔드가 종목코드로
    마스터를 뒤져 품목을 푸는 길은 두지 않습니다 — 그러면 시세가 마스터 적재
    상태에 묶입니다.
    """

    prd: str = Field(pattern=r"^[A-Z0-9]{2}$", description="품목코드 두 자리. 예 01 코스피200")


@router.post("/price-all")
def read_price_all(
    ask: AllFuturesRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """선물전체시세 — 품목 하나의 월물이 한 번에 옵니다.

    만기 목록이 쓰는 자리입니다. 월물마다 현재가를 따로 부르지 않아도 되고,
    줄의 칸 이름은 현재가 조회와 같습니다(`domestic_quotes.py` 첫머리).

    **야간에는 마감값입니다.** 선물전체시세에 야간 API 가 없어(옵션에는 있습니다)
    밤에도 주간 값이 옵니다. `session` 이 늘 `day` 인 것은 그 사실을 화면이
    알아보라는 표지입니다 — 야간 값이 필요하면 월물마다 야간 현재가를 불러야
    합니다. `asOf` 는 이 응답을 만든 한국시간입니다.
    """
    raw = _call(lambda: api.price_all_futures(ask.prd, api=api_client), response)
    made = domestic_quotes.all_futures(raw, asked=ask.prd)
    made["session"] = "day"
    made["asOf"] = iso(now_kst())
    return made


class TrendInvestorRequest(BaseModel):
    """투자자 매매동향 조회.

    **NH선물은 품목과 선물·콜·풋을 붙인 네 자리(`opt_prd`)로 받습니다.** 화면은
    이미 품목코드를 들고 있으므로 품목과 종류를 따로 받아 여기서 붙입니다 —
    붙이는 규칙이 화면마다 있으면 한쪽만 고쳐지는 날이 옵니다.
    """

    prd: str = Field(pattern=r"^[A-Z0-9]{2}$", description="품목코드 두 자리. 예 75 미국달러")
    kind: str = Field(default="futures", pattern=r"^(futures|call|put)$")
    # 자리 수만 세면 `2026abcd` 가 그대로 NH선물로 나갑니다.
    ref_dt: str = Field(default="", pattern=r"^(\d{8})?$", description="기준일 YYYYMMDD. 비우면 오늘")
    inq_tp: str = Field(default=api.QUANTITY, pattern=r"^[12]$", description="1 수량, 2 금액")

    @property
    def opt_prd(self) -> str:
        """실어 보낼 네 자리. 품목코드 + 선물옵션구분입니다."""
        return f"{self.prd}{domestic_trend.KIND_CODE[self.kind]}"

    @property
    def date(self) -> str:
        """부를 때 실을 기준일. 비어 있으면 **한국시간**의 오늘입니다.

        까닭은 예수금 라우트의 `DepositRequest.date` 와 같습니다 — 화면이 날짜를
        지어내면 자정 언저리에 화면마다 다른 날을 묻게 됩니다.
        """
        return self.ref_dt or now_kst().strftime("%Y%m%d")


@router.post("/trend-investor")
def read_trend_investor(
    ask: TrendInvestorRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """투자자 매매동향 — 무리마다 매수·매도·순매수와 직전 대비.

    **무리를 합치지 않고 온 대로 열하나를 냅니다.** 「기관」처럼 여럿을 더한 값은
    응답에 없고, 무엇을 더해야 그것이 되는지 명세가 말하지 않습니다. 고르는 일은
    화면이 합니다 — 까닭은 `domestic_trend.py` 첫머리에.

    `unit` 이 계약 수(`quantity`)인지 원(`amount`)인지 알려 줍니다. 저장해 둔
    응답이 없으면 `investors` 가 빈 목록입니다.

    **실전 서버에서 받습니다.** 매매동향은 시장의 통계라 거래시간표와 같은 규칙을
    따릅니다 — 거래시간표는 모의 서버 값이 실전과 다릅니다. 국내 실전 인증 정보가 없을 때만
    모의 서버에서 받고, 어느 쪽에서 받았는지는 `environment` 에 적습니다.
    `x-nh-environment` 헤더는 화면에서 고른 환경입니다(`backend.nh_api.reference`).
    """
    # 한 번만 읽습니다. 두 번 읽으면 자정을 넘기는 순간 NH 에 물은 날과 답에
    # 적는 날이 갈립니다.
    asked_date = ask.date

    def fetch() -> tuple[dict[str, Any], str]:
        with reference_environment(api_client, session_service.DOMESTIC) as environment:
            raw = api.trend_investor(
                ask.opt_prd,
                ref_dt=asked_date,
                inq_tp=ask.inq_tp,
                api=api_client,
                environment=environment,
            )
        return raw, environment

    raw, environment = _call(fetch, response)
    made = domestic_trend.investor_trend(raw, inq_tp=ask.inq_tp, ref_dt=asked_date)
    made["environment"] = environment
    return made


class OptionChainRequest(BaseModel):
    """옵션 체인 조회. 품목코드 두 자리와 **만기일 뒤 여섯 자리**(YYMMDD)로 묻습니다.

    `prd` 는 01 코스피200·05 미니코스피200·06 코스닥150 이고, `exp_mon` 은 종목
    마스터의 옵션 만기일 여덟 자리 중 뒤 여섯 자리입니다. `session` 이 `night`
    이면 야간 API 를 부릅니다 — 야간에는 시세가 오지 않습니다.
    """

    prd: str = Field(pattern=r"^[A-Z0-9]{2}$", description="품목코드 두 자리")
    exp_mon: str = Field(
        pattern=r"^\d{6}$",
        description="만기일 여덟 자리의 뒤 여섯 자리(YYMMDD). 예 2026-10-08 만기 → 261008. 년월(202610)이 아님",
    )
    session: str = Field(default="day", pattern=r"^(day|night)$")


@router.post("/option-chain")
def read_option_chain(
    ask: OptionChainRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """옵션 체인 — 행사가마다 콜·풋 한 줄, 실제 종목코드와 함께.

    서버가 한 번에 다 못 주면 다음 행사가로 이어 받아 **한 덩이로** 냅니다(시세
    호출 두세 건). 모양과 야간의 빈 칸 규칙은 `domestic_chain.py` 첫머리에 있습니다.
    `asOf` 는 이 응답을 만든 한국시간입니다 — 화면이 언제 값인지 적을 수 있게.
    """
    night = ask.session == "night"
    fetch = api.price_all_option_night if night else api.price_all_option
    pages = _call(
        lambda: domestic_chain.collect(
            lambda key: fetch(ask.prd, exp_mon=ask.exp_mon, nxt_stkr_pric=key, api=api_client),
            first_key=api.FIRST_PAGE,
        ),
        response,
    )
    made = domestic_chain.chain(pages, night=night)
    made["asOf"] = iso(now_kst())
    return made


@router.post("/price-bid")
def read_price_bid(
    ask: BookRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """호가.

    `asks` 는 먼 값부터, `bids` 는 가까운 값부터 옵니다. **값이 안 채워진
    단계는 빠지므로 두 배열 다 비어 올 수 있습니다.**
    """
    raw = _call(lambda: api.price_bid(ask.sym, exch_cd=ask.exch_cd, api=api_client), response)
    return domestic_quotes.book(raw, asked=ask.sym)


@router.post("/chart-daily")
def read_chart_daily(
    ask: ChartRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """차트(일). 봉은 오래된 것부터 옵니다.

    저장해 둔 응답으로 도는 재생 모드에서는 **일봉이 0개로 나옵니다.** 저장본의
    일봉은 봉의 날짜 자리가 가려져 있습니다 — 연속조회 키를 가명으로 바꿀 때 그
    키와 글자가 같은 날짜까지 함께 바뀝니다. 실서버로 부르면 날짜가 그대로 옵니다.
    """
    raw = _call(
        lambda: api.chart_daily(
            ask.sym,
            start_date=ask.inq_strt_dt,
            end_date=ask.inq_ed_dt,
            night=ask.night,
            next_key=ask.nxt_key,
            api=api_client,
        ),
        response,
    )
    made = domestic_quotes.bars(raw, minute=False, asked=ask.sym)
    # 야간 일봉에는 휴장 전날 자리를 채운 봉이 섞여 옵니다 — 거르는 규칙은
    # `domestic_quotes.drop_placeholders` 에. 주간 일봉은 건드리지 않습니다.
    return domestic_quotes.drop_placeholders(made) if ask.night else made


@router.post("/chart-minute")
def read_chart_minute(
    ask: MinuteChartRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """차트(분). 봉은 오래된 것부터 옵니다.

    `session: "both"` 면 주간·야간 두 벌을 시각순 한 벌로 냅니다. 그때 `nextKey` 는
    빈 값입니다 — 두 벌의 이어 받기 키를 하나로 낼 수 없습니다.
    """
    made: list[dict[str, Any]] = []
    for night in ask.sessions:
        raw = _call(
            lambda: api.chart_minute(
                ask.sym,
                start_date=ask.inq_strt_dt,
                end_date=ask.inq_ed_dt,
                start_time=ask.strt_tm,
                end_time=ask.ed_tm,
                minutes=ask.dcnt,
                night=night,
                next_key=ask.nxt_key,
                api=api_client,
            ),
            response,
        )
        made.append(domestic_quotes.bars(raw, minute=True, asked=ask.sym))
    return quotes.merge_bars(made, sessions=["night" if n else "day" for n in ask.sessions])


class HistoryRequest(SymbolRequest):
    """분봉을 **영업일 한 칸**씩 받는 요청.

    `before` 는 「이 영업일보다 **앞선** 영업일을 주세요」입니다 — 비우면 지금이
    속한 영업일입니다. 답의 `before` 를 그대로 다시 넣으면 한 칸 더 과거로
    갑니다. 차트를 왼쪽 끝까지 끌 때마다 한 번씩 부르는 자리입니다.
    """

    dcnt: str = "1"
    before: str = Field(default="", pattern=r"^(\d{8})?$", description="이 영업일 앞을 주세요")


@router.post("/chart-history")
def read_chart_history(
    ask: HistoryRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """분봉 한 **영업일**. 야간이 앞, 주간이 뒤로 한 줄에 섭니다.

    한 영업일은 **전날 18:00 야간 개장부터 그날 주간 마감까지**입니다. 분봉
    조회는 달력 날짜로 자르므로 조회 셋을 모아 한 영업일을 만듭니다 — 까닭과
    나누는 규칙은 `domestic_history.py` 첫머리에.

    **야간이 없는 품목은 주간 하나만 부릅니다.** 종목 마스터가 그 품목에 야간
    줄이 있는지 알고 있어(`night_products`), 빈 답이 올 조회를 내지 않습니다.

    `partial` 은 **이 영업일을 다 받았다고 보증하지 못한다**는 뜻입니다. 둘 중
    하나면 참입니다 — 받은 첫 봉이 요청한 시작보다 늦거나
    (`domestic_history.short`), 조회 셋 중 하나가 **두 번 다 실패**했거나.
    거래가 없어 늦게 시작한 것과는 구별하지 못합니다.

    **「더 과거가 있는지」를 말하는 값은 없습니다.** NH선물이 어디까지 주는지
    모르기 때문입니다 — 화면은 빈 답이 **연속 다섯 영업일** 올 때까지 `before` 로
    갑니다. 한 번 비었다고 멈추면 안 되는 것은 **휴장일이 빈 영업일로 나오기**
    때문입니다(휴장일 달력이 없습니다). `partial` 은 그 판단에 쓰는 값이 아닙니다.

    `nextKey` 는 **늘 빈 값**입니다. 조회 셋의 이어받기 키를 하나로 낼 수 없고,
    같은 키로 다시 불러도 같은 줄이 옵니다 — 더 과거는 `before` 로 갑니다.
    """
    day = history.as_day(ask.before)
    day = history.previous_business_day(day) if day else history.latest_business_day(now_kst())

    product = master_service.service.product_of(session_service.DOMESTIC, ask.sym)
    has_night = str((product or {}).get("product_code") or "") in (
        master_service.service.night_products()
    )

    pieces: list[dict[str, Any]] = []
    partial = False
    for window in history.windows(day, has_night=has_night):
        piece = _history_piece(ask, window, response, api_client)
        if piece is None:
            # **조회 하나가 죽었다고 그 영업일을 통째로 버리지 않습니다.**
            # 한 번 더 보고, 그래도 안 되면 받은 만큼 내고 못 받았다고 알립니다.
            piece = _history_piece(ask, window, response, api_client)
        if piece is None:
            partial = True
            continue
        if history.short(piece, window) and piece.get("nextKey"):
            # 한 번만 더 봅니다. 이어받기 키를 믿고 돌 수 없어(같은 줄이 옵니다)
            # 한 번 더 부른 뒤에도 안 늘면 그때는 모자란 채로 냅니다. 키가 비면
            # 같은 요청을 한 번 더 내는 것이라 부르지 않습니다.
            again = _history_piece(ask, window, response, api_client, piece["nextKey"])
            if again is not None and len(again.get("bars") or []) > len(
                piece.get("bars") or []
            ):
                piece = again
        partial = partial or history.short(piece, window)
        pieces.append(piece)

    return {
        "code": next((str(one.get("code") or "") for one in pieces if one.get("code")), ask.sym),
        "bars": history.assemble(pieces, day),
        "sessions": ["night", "day"] if has_night else ["day"],
        "skipped": sum(int(one.get("skipped") or 0) for one in pieces),
        "businessDay": history.as_text(day),
        # 화면이 한 칸 더 과거로 갈 때 그대로 다시 실어 보내는 값입니다.
        "before": history.as_text(day),
        "partial": partial,
        "nextKey": "",
    }


def _history_piece(
    ask: HistoryRequest,
    window: history.Window,
    response: Response,
    api_client: TradeClient,
    next_key: str = "",
) -> dict[str, Any] | None:
    """조회 하나. **못 받으면 `None` 입니다** — 부르는 쪽이 다시 볼지 정합니다.

    한 영업일이 조회 셋이라, 하나가 죽었다고 그대로 올리면 나머지 둘이 멀쩡한
    날도 통째로 못 봅니다.
    """
    try:
        raw = _call(
            lambda: api.chart_minute(
                ask.sym,
                start_date=window.date_text,
                end_date=window.date_text,
                start_time=window.start,
                end_time=window.end,
                minutes=ask.dcnt,
                night=window.night,
                next_key=next_key or api.FIRST_PAGE,
                api=api_client,
            ),
            response,
        )
    except HTTPException as exc:
        logger.warning(
            "국내 분봉 %s %s~%s 를 받지 못했습니다: %s",
            window.date_text,
            window.start,
            window.end,
            exc.detail,
        )
        return None
    return domestic_quotes.bars(raw, minute=True, asked=ask.sym)


@router.post("/product-detail")
def read_product_detail(
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """품목별세부정보 — 거래시간·단일가 시간·최근월물 만기일·증거금.

    **하루 한 번 받아 둔 것을 냅니다.** 거래시간은 날마다 바뀌는 값이 아니라
    요청마다 NH선물에 묻지 않습니다. 새로 받지 못하면 받아 둔 것을 그대로 냅니다.
    모양은 `domestic_hours.py` 첫머리에 있습니다.
    """
    return _call(
        lambda: session_service.service.table(session_service.DOMESTIC, api=api_client),
        response,
        kind="hours",
    )


#: 종목 정보를 못 채운 까닭. **화면이 그대로 적습니다.**
#:
#: 그래서 읽는 사람은 이 앱을 쓰는 분입니다 — 코드가 받아 오는 자료의 이름
#: (「종목 마스터」·「품목별세부정보」·「품목 줄」)을 여기 쓰지 않습니다. 화면에서
#: 부르는 이름(「종목 목록」·「품목정보」)으로 적습니다. **표 이름은 하나입니다** —
#: 같은 표를 자리마다 다른 말로 부르면 읽는 사람이 둘을 맞춰 봐야 합니다.
_NO_MASTER = "종목 목록을 아직 받지 못했습니다"
_NO_SYMBOL = "종목 목록에서 이 종목을 찾지 못했습니다"
_NO_TABLE = "품목정보가 비어 있습니다"
_TABLE_FAILED = "품목정보를 받지 못했습니다. 잠시 뒤 다시 시도해 주세요."
_NO_KEYS = "국내 인증 정보가 없어 증거금을 받지 못했습니다. 설정에서 국내 App Key 를 넣어 주세요."
_NO_PRODUCT = "이 종목이 속한 품목을 품목정보에서 찾지 못했습니다"
_NO_MARGIN = "이 품목의 증거금은 품목정보에 없습니다"
_NO_KIND = "이 종목이 선물인지 옵션인지 몰라 품목을 고르지 못했습니다"
_NO_TICK = "이 품목의 호가단위는 품목정보에 없습니다"

#: `why` 문장 → 갈래 코드. 화면이 함께 받습니다.
#:
#: **문장 글자로 갈래를 가르지 않게 하려는 것입니다.** `why` 는 고객이 읽는 말이라
#: 다듬는 자리인데, 화면이 그 글자로 「어느 까닭인가」를 가리면 문장을 다듬을 때마다
#: 화면의 갈림이 소리 없이 어긋납니다. 코드는 안 바뀌고 문장은 자유로워집니다.
_WHY_CODES: dict[str, str] = {
    _NO_MASTER: "no_master",
    _NO_SYMBOL: "no_symbol",
    _NO_TABLE: "no_table",
    _TABLE_FAILED: "table_failed",
    _NO_KEYS: "no_keys",
    _NO_PRODUCT: "no_product",
    _NO_MARGIN: "no_margin",
    _NO_KIND: "no_kind",
    _NO_TICK: "no_tick",
}


def _with_why_code(made: dict[str, Any]) -> dict[str, Any]:
    """답에 까닭의 갈래 코드를 답니다. 다 채웠으면 `None` 입니다."""
    made["whyCode"] = _WHY_CODES.get(made.get("why") or "")
    return made



@router.post("/product-info")
def read_product_info(
    ask: SymbolRequest,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """종목 하나의 품목 값 — 증거금 넷과 그 종목이 어느 품목인지.

    **화면이 종목 옆에 증거금을 그리는 자리입니다.** 품목별세부정보는 품목
    단위라 종목코드로는 바로 못 찾습니다 — 마스터에서 그 종목의 거래대상코드
    (`trd_code`)와 갈래를 꺼내 품목 줄과 잇습니다(`domestic_hours.product_row`).

    **시세 라우트에 얹지 않습니다.** 현재가는 마스터를 안 보는 자리이고
    (`AllFuturesRequest` 첫머리), 증거금을 거기 얹으면 시세가 마스터 적재
    상태에 묶입니다.

    **낡았으면 다시 받습니다**(`session_service.table`). 증거금은 날마다
    바뀌는 값이라 받아 둔 것만 내면 07:00 경계를 넘긴 뒤로는 어제 값을 새
    값처럼 내게 됩니다 — 바탕화면 앱은 며칠씩 열어 두는 자리입니다. 나가는
    것은 **경계 뒤 첫 한 건뿐**입니다(그 뒤는 `stale()` 이, 실패는
    `RETRY_AFTER` 가 막습니다).

    **계약당 유지증거금은 늘 빕니다** — 응답에 그 칸이 없습니다. 까닭은
    `domestic_hours.WHY_NO_MAINTENANCE` 에 있고 유지증거금률로는 볼 수 있습니다.

    **호가단위·틱값·거래승수도 함께 냅니다**(`tickSize`·`tickValue`·
    `multiplier`). 가격을 한 칸 올리고 내리는 화면이 그 종목의 호가단위로
    움직이려면 있어야 하는 값인데, **국내 종목 마스터에는 그 칸이 없어**
    품목 표에서만 옵니다. 해외는 마스터가 종목마다 호가단위와 틱값을 주므로
    (`tsiz`·`tval`) 해외 `product-info` 는 이 셋을 내지 않습니다 — 같은 값을
    두 자리에서 내면 어느 쪽이 맞는지 화면이 고르게 됩니다.

    못 채우면 값이 비고 `why` 가 까닭을 말합니다 — 「마스터에 없다」와 「표를
    아직 못 받았다」는 사용자가 할 일이 다릅니다.
    """
    made: dict[str, Any] = {
        "code": ask.sym,
        "productCode": None,
        "productName": None,
        "productKind": None,
        # **품목 표에만 있는 값입니다.** 국내 마스터에는 호가단위 칸이 없어,
        # 화면은 이 자리 말고는 그 종목이 얼마씩 움직이는지 알 길이 없습니다.
        "tickSize": None,
        "tickValue": None,
        "multiplier": None,
        **domestic_hours.margins(None),
        # 값을 찾았을 때만 답니다 — 종목을 못 찾았는데 유지증거금 까닭이 함께
        # 오면 화면이 무엇을 적을지 갈립니다.
        "maintenanceMarginWhy": None,
        "why": "",
        "whyCode": None,
    }

    # **마스터에 없는 종목도 코드 모양으로 풉니다**(`product_of`) — 만기가 지나
    # 마스터에서 빠진 옵션이 그렇습니다. 그 줄에는 갈래가 없어, 한 품목에 선물
    # 줄과 옵션 줄이 함께 있으면 `product_row` 가 답하지 않습니다(찍지 않습니다).
    if master_service.service.index() is None:
        # **「없다」와 「아직 못 받았다」는 다릅니다.** 앱이 뜨고 열여덟 만 줄을
        # 읽는 동안 모든 종목이 「마스터에 없다」는 답을 받으면, 사용자는 코드를
        # 잘못 적은 줄 알고 고치려 듭니다.
        made["why"] = _NO_MASTER
        return _with_why_code(made)
    row = master_service.service.product_of(session_service.DOMESTIC, ask.sym)
    if row is None:
        made["why"] = _NO_SYMBOL
        return _with_why_code(made)
    made["productCode"] = str(row.get("product_code") or "") or None
    made["productKind"] = str(row.get("kind") or "") or None

    # **못 받아도 이 자리는 안 죽습니다.** 답 모양을 하나로 지키고 `why` 로
    # 말합니다 — 증거금을 보던 사용자에게 「거래시간을 받지 못했습니다」가 서면
    # 엉뚱한 자리를 봅니다. 받아 둔 표가 있으면 `table()` 이 그것을 내주므로,
    # 여기로 오는 것은 **한 번도 못 받은 채 받기까지 실패한** 자리뿐입니다.
    try:
        table = session_service.service.table(session_service.DOMESTIC, api=api_client)
    except ConfigError as exc:
        # **기다려도 안 되는 자리입니다.** 그 시장의 인증 정보가 없는 것이라
        # 「잠시 뒤 다시」가 아니라 무엇을 넣어야 하는지를 말해야 합니다 —
        # 해외 키만 넣은 사용자가 국내 화면을 열면 늘 여기입니다.
        logger.warning("국내 인증 정보가 없어 품목별세부정보를 받지 않습니다: %s", exc)
        made["why"] = _NO_KEYS
        return _with_why_code(made)
    except NHApiError as exc:
        logger.warning("국내 품목별세부정보를 받지 못했습니다: %s", exc)
        made["why"] = _TABLE_FAILED
        return _with_why_code(made)
    products = (table or {}).get("products")
    products = [one for one in products if isinstance(one, dict)] if isinstance(products, list) else []
    if not products:
        made["why"] = _NO_TABLE
        return _with_why_code(made)

    # **창은 안 가립니다.** 증거금은 주간 줄과 야간 줄이 같은 값입니다 —
    # 예를 들어 (야간)코스피200선물은 주간 줄과 같은 액수로 옵니다.
    found = domestic_hours.product_row(
        products, row, domestic_hours.DAY, any_window=True
    )
    if found is None:
        # **「줄이 없다」와 「어느 줄인지 모른다」는 다릅니다.** 갈래를 모르는
        # 종목이 선물 줄과 옵션 줄이 함께 있는 품목에 오면, 줄은 있는데 고를
        # 수가 없습니다 — 사용자가 할 일도 다릅니다.
        code = str(row.get("trd_code") or "").strip()
        same = [one for one in products if str(one.get("productCode") or "") == code]
        made["why"] = _NO_KIND if same else _NO_PRODUCT
        return _with_why_code(made)
    made["maintenanceMarginWhy"] = domestic_hours.WHY_NO_MAINTENANCE
    made["productName"] = str(found.get("name") or "") or None
    # **표의 값 그대로입니다** — 여기서 셈하지 않습니다. 셈해서 내면 그 나눗셈이
    # 맞는지 늘 함께 믿어야 합니다.
    made["tickSize"] = found.get("tick")
    made["tickValue"] = found.get("tickValue")
    made["multiplier"] = found.get("multiplier")
    # 마스터가 갈래를 안 줬으면 고른 품목 줄의 갈래가 그 답입니다 — 그 품목에
    # 갈래가 하나뿐일 때만 여기까지 옵니다.
    made["productKind"] = made["productKind"] or str(found.get("kind") or "") or None
    made.update(domestic_hours.margins(found))
    if made["contractInitialMargin"] is None:
        # 옵션은 칸이 비는 것이 맞습니다 — 까닭은 `domestic_hours.margins` 에.
        made["why"] = _NO_MARGIN
    if made["tickSize"] is None:
        # **먼저 적힌 까닭을 덮지 않습니다.** 값이 빈 것은 화면이 칸을 보고
        # 알고, `why` 는 그 종목에 대해 **가장 먼저 걸린** 까닭 하나입니다.
        made["why"] = made["why"] or _NO_TICK
    return _with_why_code(made)


class DepositRequest(BaseModel):
    """예수금 조회.

    **비우면 오늘로 봅니다.** 화면이 날짜를 지어내지 않게 하려는 것입니다 —
    「오늘이 며칠인가」는 화면과 백엔드가 서로 다른 시계를 볼 수 있는 자리이고,
    화면마다 각자 채우면 자정 근처에서 서로 다른 날을 묻게 됩니다.

    **오늘이 휴일일 때 NH선물의 답은 명세에 없습니다** — 빈 표일 수도, 직전
    영업일 값일 수도, 거절일 수도 있습니다. 날짜를 정해 부르고 싶으면 `biz_dt` 에
    넣으면 됩니다.
    """

    # 자리 수만 세면 `2026abcd` 가 그대로 NH선물로 나갑니다.
    biz_dt: str = Field(
        default="", pattern=r"^(\d{8})?$", description="영업일자 YYYYMMDD. 비우면 오늘"
    )

    @property
    def date(self) -> str:
        """부를 때 실을 날짜. 비어 있으면 오늘입니다 — **한국시간**의 오늘.

        서버의 영업일은 한국 날짜이므로, 이 컴퓨터가 다른 시간대에 있어도
        자정 언저리에 어제·내일로 어긋나지 않게 합니다.
        """
        return self.biz_dt or now_kst().strftime("%Y%m%d")


@router.post("/deposit")
def read_deposit(
    ask: DepositRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """예수금.

    `items` 가 표의 줄이고 `values` 는 총액·현금·대용·외화 차례입니다.
    **계좌번호는 내보내지 않습니다** — 까닭은 `domestic_account.py` 첫머리에.
    """
    # 한 번만 읽습니다. 두 번 읽으면 자정을 넘기는 순간 NH 에 물은 날과
    # 화면에 적는 날이 갈립니다.
    asked_date = ask.date
    raw = _call(lambda: api.deposit(asked_date, api=api_client), response, account=True)
    made = domestic_account.deposit(raw, asked_date=asked_date)
    _keep_deposit(api_client, asked_date, made)
    return made


def _keep_deposit(api_client: TradeClient, day: str, made: dict[str, Any]) -> None:
    """그날의 예수금을 쌓아 둡니다. **추이는 이렇게 모은 값으로 그립니다.**

    NH선물에 날짜별 줄을 주는 계좌 API 가 없어, 화면이 빠진 날을 하루씩 물으면
    그때마다 여기 쌓입니다(`deposit_history.py` 첫머리).

    **어느 환경에서 받은 값인지 모르면 안 쌓습니다** — 모의와 실전이 한 그래프에
    섞이면 그 그래프는 어느 계좌의 것도 아닙니다.
    """
    environment = orders.environment_of(api_client)
    if not environment:
        return
    kept = deposit_history.store
    if deposit_history.is_empty(made):
        kept.keep_empty(environment=environment, market="domestic", day=day)
        return
    kept.keep(environment=environment, market="domestic", day=day, row=made)


@router.get("/deposit-history")
def read_deposit_history(
    first: str = Query(alias="from", pattern=r"^\d{8}$", description="시작 영업일"),
    last: str = Query(alias="to", pattern=r"^\d{8}$", description="끝 영업일"),
) -> dict[str, Any]:
    """쌓아 둔 날짜별 예수금과 **아직 안 받은 날들**.

    `missing` 은 화면이 하루씩 물어 채울 날입니다 — 주말과 「받았는데 값이 없던
    날」은 빠집니다. 채우는 길은 `POST /api/domestic/deposit` 이고, 그 답이
    나갈 때 여기 쌓입니다(따로 저장하는 라우트는 없습니다).

    `refresh` 는 기간 안의 최근 두 영업일(오늘 포함)입니다. 쌓여 있어도 다시
    물어 덮을 날입니다 — 최근 날의 값은 정산 뒤에 달라질 수 있습니다.

    `store` 는 **쌓아 둔 파일을 읽었는지**입니다. 못 읽으면 지난 값이 통째로
    비어 보이므로 화면이 그 사실을 알아야 합니다.
    """
    made = deposit_history.answer(
        first, last, market="domestic", environment=str(nh_runtime.active_environment() or "")
    )
    if made is None:
        raise HTTPException(status_code=422, detail=deposit_history.BAD_PERIOD)
    return made


class PageRequest(BaseModel):
    """이어 받기만 받는 요청. 처음에는 빈 값입니다."""

    nxt_key: str = ""


@router.post("/open-interest")
def read_open_interest(
    ask: PageRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """잔고내역(보유 포지션).

    줄마다 `market` 이 주간인지 야간인지 알려 줍니다. 만기·잔존일수·소수
    자릿수·통화는 이 응답에 없어 화면이 종목 마스터에서 채웁니다.
    """
    raw = _call(
        lambda: api.open_interest(next_key=ask.nxt_key, api=api_client),
        response,
        account=True,
    )
    return domestic_account.positions(raw)


class OrderHistoryRequest(PageRequest):
    """주문내역 조회.

    **미체결도 이 주소로 받습니다.** `exec_div` 로 고릅니다 — `0` 전체,
    `1` 체결, `2` 미체결, `3` 미접수.
    """

    exec_div: str = Field(default=api.ALL, pattern=r"^[0-3]$")
    trd_div: str = Field(default=api.ALL, pattern=r"^[0-2]$")
    trd_tm_div_cd: str = Field(default=api.ALL, pattern=r"^[0-2]$")
    ord_div_cd: str = Field(default=api.ALL, pattern=r"^[0-2]$")


@router.post("/order-history")
def read_order_history(
    ask: OrderHistoryRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """주문내역과 미체결.

    줄 하나가 주문 하나이고 진행 상태가 함께 들어 있습니다. 화면의 두 표가
    같은 목록을 다르게 읽습니다.
    """
    raw = _call(
        lambda: api.order_history(
            exec_div=ask.exec_div,
            trd_div=ask.trd_div,
            trd_tm_div_cd=ask.trd_tm_div_cd,
            ord_div_cd=ask.ord_div_cd,
            next_key=ask.nxt_key,
            api=api_client,
        ),
        response,
        account=True,
    )
    return domestic_account.order_history(raw)


class OrderErrorRequest(PageRequest):
    """주문오류내역 조회.

    **비우면 오늘로 봅니다.** 예수금·해외 주문오류와 같은 규칙입니다 —
    「오늘이 며칠인가」를 화면마다 채우면 **자정 근처에서 화면과 백엔드가
    다른 날을 묻게 됩니다.**

    **오늘이 휴일일 때 NH선물의 답은 명세에 없습니다** — 빈 표일 수도, 직전
    영업일 값일 수도, 거절일 수도 있습니다. 날짜를 정해 부르고 싶으면 `inq_dt` 에
    넣으면 됩니다.
    """

    inq_dt: str = Field(
        default="", pattern=r"^(\d{8})?$", description="조회일자 YYYYMMDD. 비우면 오늘"
    )
    trd_div_cd: str = Field(default=api.ALL, pattern=r"^[0-2]$")
    # 기간으로 물을 때. 둘 다 비우면 위의 하루 조회입니다.
    inq_strt_dt: str = Field(default="", pattern=r"^(\d{8})?$", description="기간 시작일 YYYYMMDD")
    inq_ed_dt: str = Field(default="", pattern=r"^(\d{8})?$", description="기간 끝날 YYYYMMDD")

    @field_validator("inq_strt_dt", "inq_ed_dt")
    @classmethod
    def _real_date(cls, value: str) -> str:
        # 자리 수만 맞는 `20261345` 는 서버에 보내기 전에 여기서 거릅니다.
        if value and market_values.date(value) is None:
            raise ValueError("있는 날짜여야 합니다")
        return value

    @model_validator(mode="after")
    def _period_shape(self):
        problem = order_error_days.period_problem(
            self.inq_dt, self.nxt_key, self.inq_strt_dt, self.inq_ed_dt
        )
        if problem:
            raise ValueError(problem)
        if self.inq_strt_dt:
            # 끝날이 오늘보다 뒤면 오늘로 자릅니다. 자른 기간으로 길이를 봅니다.
            order_error_days.resolve(self.inq_strt_dt, self.inq_ed_dt, now_kst().date())
        return self

    @property
    def period(self) -> bool:
        return bool(self.inq_strt_dt)

    @property
    def date(self) -> str:
        """부를 때 실을 날짜. 비어 있으면 오늘(한국시간)입니다 — 예수금과 같은 규칙."""
        return self.inq_dt or now_kst().strftime("%Y%m%d")


@router.post("/order-error")
def read_order_error(
    ask: OrderErrorRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """주문오류내역.

    **줄에 주문번호와 주야간 구분이 없습니다** — 화면의 목록 열쇠와 시장은
    이 응답으로 못 채웁니다. 까닭은 `domestic_account.order_errors()` 에.

    **기간으로도 물을 수 있습니다**(`inq_strt_dt`·`inq_ed_dt`, 31일까지) — 날마다
    차례로 불러 합칩니다(`backend/order_error_days.py`). 줄에 날짜 칸이 없어 **물은
    날**을 `date` 로 붙입니다. 날마다 다른 조회라 겹침은 거르지 않습니다.
    """
    if ask.period:
        return _order_error_period(ask, response, api_client)
    raw = _call(
        lambda: api.order_error(
            ask.date,
            trd_div_cd=ask.trd_div_cd,
            next_key=ask.nxt_key,
            api=api_client,
        ),
        response,
        account=True,
    )
    return domestic_account.order_errors(raw)


def _order_error_period(
    ask: OrderErrorRequest, response: Response, api_client: TradeClient
) -> dict[str, Any]:
    first, last = order_error_days.resolve(ask.inq_strt_dt, ask.inq_ed_dt, now_kst().date())
    made = order_error_days.collect(
        first,
        last,
        lambda day, key: _call(
            lambda: api.order_error(
                day.strftime("%Y%m%d"), trd_div_cd=ask.trd_div_cd, next_key=key, api=api_client
            ),
            response,
            account=True,
        ),
        shape=lambda raw: domestic_account.order_errors(raw)["errors"],
        # `nxt_data_yn` 이 `N` 이면 그 날은 끝입니다. 칸이 없으면 다음 키로 봅니다.
        more=lambda raw: str(raw.get("nxt_data_yn") or "").strip().upper() != "N",
    )
    body = order_error_days.answer(
        made, first, last, response, date_of=lambda one, day: day.isoformat()
    )
    body["total"] = len(body["errors"])
    body["hasMore"] = False
    return body


class OrderableRequest(SymbolRequest):
    """주문가능수량 조회.

    **방향과 가격을 함께 물어봅니다.** 매수와 매도의 답이 다르고, 값이
    가격에 따라 달라지기 때문입니다.

    가격을 **문자열로 받습니다** — 화면에 보이는 글자를 그대로 NH선물에
    보내기 위해서입니다. 숫자로 받아 다시 글자로 만들면 자릿수가 달라져
    보낸 값과 보이는 값이 어긋날 수 있습니다. 시장가에도 가격이 필요하므로
    (명세상 필수) 화면이 쓰는 가격을 그대로 넣어 부릅니다. 값이 없으면
    부르지 않습니다.

    주간인지 야간인지는 **화면 맥락**이 정합니다. 법인 계좌 구분(처리구분
    2·4)은 쓰지 않습니다 — 이 프로그램은 개인 계좌만 다룹니다.
    """

    side: str = Field(pattern=r"^(buy|sell)$", description="화면 낱말. 매수 buy, 매도 sell")
    ord_pric: str = Field(pattern=r"^-?\d{1,15}(\.\d{1,8})?$", description="주문가격")
    context: str = Field(
        default="domestic-day",
        pattern=r"^domestic-(day|night)$",
        description="주간이면 domestic-day, 야간이면 domestic-night",
    )


@router.post("/orderable-quantity")
def read_orderable_quantity(
    ask: OrderableRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """주문가능수량.

    한 방향의 값만 냅니다 — 물어본 방향입니다. 화면이 매수·매도를 바꾸면
    다시 물어봅니다.
    """
    raw = _call(
        lambda: api.orderable_quantity(
            ask.sym,
            trd_div=domestic_account.SIDE_CODE[ask.side],
            ord_pric=ask.ord_pric,
            ord_sts=api.NIGHT_RETAIL if ask.context.endswith("night") else api.DAY_RETAIL,
            api=api_client,
        ),
        response,
        account=True,
    )
    return domestic_account.orderable_quantity(raw, code=ask.sym, side=ask.side)


# ── 주문·정정·취소 ──────────────────────────────────────────
#
# **되돌릴 수 없는 자리입니다.** 무엇을 다르게 다루는지는 `backend/orders.py`
# 첫머리에 있습니다. 여기서는 국내에만 있는 것 둘을 답니다.
#
#   주간·야간이 **API 자체가 다릅니다** — `context` 로 고릅니다.
#   시장가·최유리는 주문가격을 **0 으로** 실어 보냅니다(명세).
#
# 세 요청 모두 **화면이 믿는 환경**을 실어야 합니다. 서버의 활성 환경과 다르면
# 보내지 않습니다 — 까닭은 `backend/orders.py` 첫머리에 있습니다.


# 접수 답에 적는 주문 종류. 주문내역 줄의 `orderKind` 와 같은 낱말입니다.
_NEW = domestic_account.ORDER_KIND["1"]
_MODIFY = domestic_account.ORDER_KIND["2"]
_CANCEL = domestic_account.ORDER_KIND["3"]


def _environment_field() -> Any:
    return Field(
        pattern=ENVIRONMENT_PATTERN,
        description="화면이 믿는 환경. 서버의 활성 환경과 다르면 안 보냄",
    )


class OrderRequest(SymbolRequest):
    """신규주문.

    가격은 문자열로 받습니다 — 화면에 보이는 글자를 그대로 보내기 위해서입니다.
    시장가·최유리(`2`·`4`)면 가격을 안 봅니다(명세대로 `0` 을 싣습니다).
    지정가·조건부인데 가격이 없으면 부르지 않습니다.
    """

    environment: str = _environment_field()
    side: str = Field(pattern=r"^(buy|sell)$", description="화면 낱말. 매수 buy, 매도 sell")
    ord_qty: int = Field(gt=0, description="주문수량")
    ord_tp_cd: str = Field(
        pattern=r"^[1-4]$", description="1 지정가, 2 시장가, 3 조건부, 4 최유리"
    )
    exec_cnd_cd: str = Field(default=api.FAS, pattern=r"^[1-3]$", description="1 FAS, 2 FOK, 3 FAK")
    ord_pric: str = Field(default="", pattern=r"^(-?\d{1,15}(\.\d{1,8})?)?$")
    context: str = Field(default="domestic-day", pattern=r"^domestic-(day|night)$")

    @property
    def night(self) -> bool:
        return self.context.endswith("night")

    @property
    def price(self) -> str:
        """실어 보낼 주문가격. 값을 안 받는 유형이면 `0` 입니다."""
        return "0" if self.ord_tp_cd in api.ZERO_PRICE_TYPES else self.ord_pric

    @model_validator(mode="after")
    def _price_when_needed(self) -> "OrderRequest":
        if self.ord_tp_cd not in api.ZERO_PRICE_TYPES and not self.ord_pric:
            raise ValueError("지정가·조건부 주문은 주문가격이 필요합니다")
        return self


class ModifyRequest(OrderRequest):
    """정정주문. 원주문번호와 정정수량이 더 필요합니다."""

    org_ord_no: str = Field(min_length=1, description="원주문번호. 주문내역 줄의 id")


class CancelRequest(SymbolRequest):
    """취소주문. **고칠 값이 없어 받는 것이 다섯뿐입니다.**"""

    environment: str = _environment_field()
    side: str = Field(pattern=r"^(buy|sell)$")
    ord_qty: int = Field(gt=0, description="취소수량. 원주문 총수량 이하")
    org_ord_no: str = Field(min_length=1, description="원주문번호. 주문내역 줄의 id")
    context: str = Field(default="domestic-day", pattern=r"^domestic-(day|night)$")

    @property
    def night(self) -> bool:
        return self.context.endswith("night")


@router.post("/order")
def place_order(
    ask: OrderRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """신규주문."""
    raw = _order(
        lambda: api.order(
            ask.sym,
            trd_div=domestic_account.SIDE_CODE[ask.side],
            ord_tp_cd=ask.ord_tp_cd,
            ord_qty=str(ask.ord_qty),
            ord_pric=ask.price,
            exec_cnd_cd=ask.exec_cnd_cd,
            night=ask.night,
            api=api_client,
        ),
        api_client,
        response,
        claimed=ask.environment,
    )
    return _result(raw, ask, api_client, kind=_NEW)


@router.post("/modify")
def modify_order(
    ask: ModifyRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """정정주문. 원주문번호는 여기서 열 자리로 채웁니다."""
    raw = _order(
        lambda: api.modify(
            ask.sym,
            org_ord_no=orders.order_no(ask.org_ord_no),
            trd_div=domestic_account.SIDE_CODE[ask.side],
            ord_tp_cd=ask.ord_tp_cd,
            ord_qty=str(ask.ord_qty),
            ord_pric=ask.price,
            exec_cnd_cd=ask.exec_cnd_cd,
            night=ask.night,
            api=api_client,
        ),
        api_client,
        response,
        claimed=ask.environment,
    )
    return _result(raw, ask, api_client, kind=_MODIFY)


@router.post("/cancel")
def cancel_order(
    ask: CancelRequest,
    response: Response,
    api_client: TradeClient = Depends(get_client),
) -> dict[str, Any]:
    """취소주문."""
    raw = _order(
        lambda: api.cancel(
            ask.sym,
            org_ord_no=orders.order_no(ask.org_ord_no),
            trd_div=domestic_account.SIDE_CODE[ask.side],
            ord_qty=str(ask.ord_qty),
            night=ask.night,
            api=api_client,
        ),
        api_client,
        response,
        claimed=ask.environment,
    )
    return _result(raw, ask, api_client, kind=_CANCEL)


def _result(
    raw: dict[str, Any] | None,
    ask: OrderRequest | CancelRequest,
    api_client: TradeClient,
    *,
    kind: str,
) -> dict[str, Any]:
    """접수됐으면 미체결 표 한 줄로. 거절이면 `_order` 가 이미 그 모양을 만듭니다.

    **주문번호가 비어 오면 접수라고 답하지 않습니다.** 무엇을 담은 줄인지는
    `domestic_account.order_reply()` 에, 왜 504 인지는 `orders.NO_ORDER_NUMBER` 에
    있습니다.

    접수됐으면 **주문내역으로 결과를 확인해** `outcome` 에 싣습니다 — 접수 뒤에
    거부되는 주문이 있습니다. 거부면 줄의 상태도 거부로 바뀝니다. 확인하느라
    답이 1~3초 늦을 수 있습니다(`backend/order_outcome.py`).
    """
    if raw is not None and raw.get("accepted") is False:
        return raw
    body = raw or {}
    order_id = str(body.get("ord_no") or "").strip()
    if not order_id:
        # 원문을 통째로 남기지 않습니다 — 요청에 실어 보낸 값이 되비쳐 올 수 있습니다.
        logger.warning("접수 답에 주문번호가 없습니다. 받은 칸: %s", sorted(body))
        raise HTTPException(
            status_code=504,
            detail=orders.NO_ORDER_NUMBER,
            headers=orders.environment_headers(api_client),
        )
    reply = domestic_account.order_reply(
        order_id=order_id,
        kind=kind,
        environment=orders.environment_of(api_client),
        code=ask.sym,
        side=ask.side,
        quantity=ask.ord_qty,
        context=ask.context,
        now=now_kst(),
        # 취소 요청에는 없는 칸들입니다. 없으면 답에도 안 실립니다.
        order_type_code=getattr(ask, "ord_tp_cd", ""),
        fill_condition_code=getattr(ask, "exec_cnd_cd", ""),
        price=getattr(ask, "price", ""),
        # **보낸 모양 그대로 냅니다**(열 자리로 채운 것). 뒤에 오는 알림과 조회가
        # 그 모양으로 말하므로, 화면이 같은 글자로 줄을 이을 수 있습니다.
        original_order_no=_original_order_no(ask),
    )
    return order_outcome.apply(reply, _outcome(order_id, api_client))


def _outcome(order_id: str, api_client: TradeClient) -> dict[str, Any]:
    """접수된 주문의 결과를 주문내역으로 확인합니다(`backend/order_outcome.py`).

    접수 답이 「접수」여도 NH선물이 뒤에 거부할 수 있습니다 — 그 거부는 주문내역의
    그 줄에만 남습니다. **저장해 둔 응답으로 도는 중에는 확인하지 않습니다** — 저장본의
    주문내역은 방금 낸 주문과 상관이 없습니다.
    """
    if orders.is_replay(api_client):
        return order_outcome.unknown()
    return order_outcome.settle(
        lambda: order_outcome.look(order_outcome.DOMESTIC, order_id, api=api_client)
    )


def _original_order_no(ask: OrderRequest | CancelRequest) -> str:
    """정정·취소가 가리키는 원주문번호. 신규에는 없습니다."""
    given = getattr(ask, "org_ord_no", "")
    return orders.order_no(given) if given else ""


def _order(
    action, api_client: TradeClient, response: Response, *, claimed: str
) -> dict[str, Any]:
    """주문 하나를 냅니다. **실패해도 다시 보내지 않습니다.**

    거절(업무 거부)은 실패가 아니라 답이라 200 으로 냅니다. 응답을 못 받은
    것과는 다릅니다 — 그때는 접수됐는지 알 수 없어 화면이 확인을 청해야 합니다.

    `claimed` 는 화면이 믿는 환경입니다. 서버의 활성 환경과 다르거나, 환경을
    바꾸는 중이거나, 환경을 못 읽으면 **보내지 않습니다.** 실전인지 모의인지로
    막는 것이 아닙니다 — 화면과 서버가 같은 환경을 말할 때만 보내는 것입니다.
    """
    refused = orders.refusal(api_client, claimed)
    if refused is not None:
        status, detail = refused
        logger.warning("주문을 내지 않았습니다(%s): %s", status, detail)
        raise HTTPException(
            status_code=status, detail=detail, headers=orders.environment_headers(api_client)
        )

    last_replay.set(None)
    last_environment.set(None)
    try:
        return action()
    except BusinessError as exc:
        logger.info("주문이 거절되었습니다: %s", exc.code)
        return orders.rejected(
            code=exc.code, message=exc.detail, environment=orders.environment_of(api_client)
        )
    except TransportError as exc:
        if orders.is_replay(api_client):
            logger.info("재생 모드에 그 주문 기록이 없습니다: %s", exc)
            raise HTTPException(status_code=409, detail=orders.REPLAY_REFUSED) from exc
        logger.warning("주문 결과를 받지 못했습니다: %s", exc)
        raise HTTPException(status_code=504, detail=orders.NOT_RECEIVED) from exc
    except GatewayError as exc:
        if paper_unavailable(exc):
            # 모의투자에서는 그 주문 API 가 없다는 답입니다 — 처리되지 않은 것이
            # 확실하므로 「알 수 없음」이 아니라 거절로 냅니다.
            logger.info("모의투자에서 부를 수 없는 주문입니다: %s", exc.code)
            return orders.rejected(
                code=exc.code,
                message=exc.detail,
                kind=error_codes.PAPER_UNAVAILABLE,
                environment=orders.environment_of(api_client),
            )
        # **처리됐는지 모르는 자리입니다.** 게이트웨이가 거절한 것은 「서버가
        # 보고 안 된다고 한 것」이 아니라 「무엇이라 했는지 모르는 것」이라,
        # 「잠시 뒤 다시」로 적으면 이미 나갔을 수 있는 주문을 한 번 더 내라는
        # 말이 됩니다. 응답을 못 받은 것과 같은 결로 냅니다.
        logger.warning("주문 결과를 알 수 없습니다(%s): %s", exc.code, exc)
        raise HTTPException(status_code=504, detail=orders.NOT_RECEIVED) from exc
    except (HttpError, NHApiError) as exc:
        logger.warning("주문을 내지 못했습니다: %s", exc)
        raise HTTPException(status_code=502, detail=orders.REJECTED) from exc
    finally:
        _stamp(response)


def _call(
    action, response: Response | None = None, *, account: bool = False, kind: str = "quote"
) -> dict[str, Any]:
    """부르고, 실패는 화면용 문구로 바꿉니다.

    거절에 NH선물의 오류 코드가 실려 오면 문장에 코드와 원문을 함께 싣고 헤더로도
    답니다(`orders.lookup_refusal`). 코드가 없는 실패는 화면용 문구만 내고 원문은
    기록에 남깁니다.

    저장본으로 돌 때는 **무엇으로 답했는지를 응답 헤더에 실어 보냅니다.**
    같은 API 의 다른 기록으로 대신 답할 수 있는데, 화면 쪽에서 그것을 알 수
    없으면 「값이 이상하다」의 원인이 화면인지 자료인지 가려지지 않습니다.
    실서버로 돌 때는 붙지 않습니다.

    `kind` 는 **무슨 조회인지**(`quote`·`account`·`hours`)입니다. 실패 문구가
    갈립니다 — 예수금을 못 받았는데 「시세를 받지 못했습니다」가 서면 사용자가
    엉뚱한 자리를 봅니다. `account=True` 는 `kind="account"` 와 같습니다.
    """
    # 기록에 적는 말도 함께 갈립니다. 로그만 보고도 어느 쪽이 죽었는지 가려야 합니다.
    not_received, rejected, what = _FAILURES["account" if account else kind]

    last_replay.set(None)
    last_environment.set(None)
    try:
        return action()
    except MissingMarketKeys as exc:
        # 시장의 참고 자료(거래시간표·매매동향)를 받을 인증 정보가 어느 환경에도
        # 없습니다. 기다려도 안 되는 자리라 무엇을 넣으면 되는지를 답합니다.
        logger.info("국내 %s 조회를 하지 않습니다: %s", what, exc)
        raise HTTPException(status_code=MISSING_KEYS_STATUS, detail=str(exc)) from exc
    except TransportError as exc:
        logger.warning("국내 %s 조회 실패: %s", what, exc)
        raise HTTPException(status_code=504, detail=not_received) from exc
    except (HttpError, BusinessError) as exc:
        logger.warning("국내 %s 조회 거절: %s", what, exc)
        if orders.quote_subscription(exc):
            # 시세 사용 신청이 먼저인 자리입니다. 무엇을 하면 되는지 알립니다.
            raise orders.quote_subscription_refusal(exc) from exc
        raise orders.lookup_refusal(exc, what, rejected) from exc
    except NHApiError as exc:
        if paper_unavailable(exc):
            # 모의투자에서는 NH선물이 이 조회를 제공하지 않습니다(`EGW053`).
            logger.info("국내 %s 는 모의투자에서 부를 수 없습니다: %s", what, exc)
            raise orders.paper_refusal(exc) from exc
        logger.warning("국내 %s 응답을 다루지 못했습니다: %s", what, exc)
        raise orders.lookup_refusal(exc, what, rejected) from exc
    finally:
        _stamp(response)


def _stamp(response: Response | None) -> None:
    """어느 환경의 값인지, 저장본으로 답했으면 그 사실을 헤더로 남깁니다."""
    if response is None:
        return
    nh_runtime.stamp_environment(response)
    note = last_replay.get()
    if not note:
        return
    response.headers[MATCH_HEADER] = note["match"]
    response.headers[SYMBOL_HEADER] = note["symbol"]
    if note["record"]:
        response.headers[RECORD_HEADER] = note["record"]
