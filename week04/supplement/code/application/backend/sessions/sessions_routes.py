"""`/api/session` — 이 종목은 지금 어느 세션인가.

**주소에 시장 칸이 없습니다.** `/api/{시장}/{API 이름}` 은 NH선물 서버에 그대로
물어보는 조회·주문 API 의 규칙이고, 이것은 받아 둔 시간표와 마스터로 **여기서
셈한 답**이라 그 규칙에 들어가지 않습니다. `/api/stream`·`/api/ui` 와 같은 자리입니다.

시간표는 받으러 가지 않습니다 — 앱이 뜰 때 받아 둔 것(`sessions.service`)을 씁니다.
받아 둔 것이 없으면 국내는 내장 표로 답하고 해외는 「모름」입니다. 답의 모양과
낱말은 `judge` 첫머리에 있습니다.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from backend.master import master_service
from backend.sessions import judge
from backend.sessions import service as session_service
from backend.sessions.hours import iso, now_kst

router = APIRouter(prefix="/api/session", tags=["session"])


@router.get("")
def read_session(
    market: str = Query(pattern=r"^(domestic|overseas)$", description="시장"),
    sym: str = Query(min_length=1, description="종목 표준 코드"),
) -> dict[str, Any]:
    """종목 하나의 지금 세션.

    종목이 마스터에 없어도 **코드 모양으로 품목을 풀어** 그 품목의 창으로 답합니다 —
    만기가 지난 옵션, 해외의 행사가 없는 시리즈 코드(`O_ESU26`)가 그렇습니다. 그때는
    만기를 몰라 `lastTradingDay` 가 늘 `null` 입니다. 품목도 못 풀면 `source` 가
    `unknown` 입니다.

    국내 답에는 **시간 구간을 어느 층에서 읽었는지**가 `hoursLayer` 로 세션마다
    실립니다(`judge.LAYER_PRODUCT` 참고). 해외와 품목을 못 푼 답에는 그 칸이
    **없습니다** — 국내 층 걷기를 지나지 않은 답이라 말할 값이 없습니다.

    `source` 는 받은 시간표로 답했으면 **어느 서버에서 받았는지**까지 적습니다 —
    `api:real`(실전)·`api:paper`(모의). 시간표는 실전 인증 정보가 있으면 실전에서
    받습니다(`sessions.service` 첫머리). 내장 표로 답했으면 `table`, 품목을 못
    풀었으면 `unknown` 입니다.
    """
    now = now_kst()
    made = judged(market, sym, now)
    if made.get("source") == judge.FROM_API:
        made["source"] = _api_source(session_service.service.cached(market))
    return {"market": market, "sym": sym, "now": iso(now), **made}


def _api_source(table: dict[str, Any] | None) -> str:
    """받은 시간표의 출처 — `api:real`·`api:paper`. 환경이 안 적힌 표면 `api` 그대로입니다."""
    environment = str((table or {}).get("environment") or "").strip()
    return f"{judge.FROM_API}:{environment}" if environment else judge.FROM_API


def trading_day(market: str, sym: str, at: Any) -> Any:
    """해외 종목의 그 시각이 든 거래 창의 거래일(창 밖이면 `None`). 판정과 같은 표를 씁니다.

    국내는 거래일을 시간표 없이 셀 수 있어(`strategy.bars.day_slot_of`) 여기를
    쓰지 않습니다. 해외에서 종목을 못 찾으면 한국 시각의 달력 날짜입니다.
    """
    contract = master_service.service.product_of(market, sym)
    table = session_service.service.cached(market)
    return judge.overseas_trading_day(contract, table=table, at=at)


def trading_day_end(market: str, sym: str, now: Any) -> Any:
    """해외 종목의 지금 거래일의 마지막 구간 끝(`judge.overseas_day_end`). 판정과 같은 표입니다."""
    contract = master_service.service.product_of(market, sym)
    table = session_service.service.cached(market)
    return judge.overseas_day_end(contract, table=table, now=now)


def judged(market: str, sym: str, now: Any) -> dict[str, Any]:
    """판정 본체. 마스터 줄(없으면 코드로 푼 품목 줄)과 받아 둔 시간표를 `judge` 에 넘깁니다."""
    contract = master_service.service.product_of(market, sym)
    table = session_service.service.cached(market)
    if contract is None:
        made = judge.judge_overseas(None, table=None, now=now)
    elif market == judge.DOMESTIC:
        product = str(contract.get("product_code") or "")
        has_night = product in master_service.service.night_products()
        made = judge.judge_domestic(contract, has_night=has_night, table=table, now=now)
    else:
        made = judge.judge_overseas(contract, table=table, now=now)
    made["holidayAware"] = False
    return made
