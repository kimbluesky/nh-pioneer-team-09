"""해외 시세 응답을 화면이 쓰는 모양으로 바꿉니다.

국내(`domestic_quotes.py`)와 **같은 낱말**을 냅니다 — 화면의 시세 카드·호가창·
차트가 시장을 가리지 않고 같은 이름을 읽습니다. 값 읽기와 호가 사다리는
`backend/market_values.py`, 봉과 종목 대조는 `backend/quotes.py` 에 있습니다.

## 국내와 다른 자리

**현재가는 여러 종목이 한 번에 옵니다.** 요청도 배열이고 응답도 배열입니다.
국내는 종목마다 한 번씩 불러야 하는데, 해외는 관심종목 여덟 개를 한 번에
물어볼 수 있어 시세 호출 제한(초당 열 건)을 훨씬 덜 씁니다.

**거래소코드가 필요합니다.** 선물이면 `F`, 옵션이면 `O` 에 거래소를 붙입니다
(`FCME`·`OCME`). 화면이 종목 마스터에서 만들어 보냅니다.

**응답에 없는 것이 많습니다.** 미결제약정·이론가·내재변동성·상하한·현물은
해외 응답에 아예 없습니다. 화면 타입이 고정 칸이라 **`null` 로 채워 냅니다** —
칸을 빼면 화면이 `undefined` 를 만납니다. 예수금(항목 목록)과 다른 자리입니다.

**해외에만 있는 것도 있습니다.** 정산가·전일종가·거래소·실시간종목코드·
상승/하강거래량이 그렇습니다. 있는 대로 더해 냅니다.

## 이름 함정 둘

**`occurs1_diff_rt` 는 등락률이 아닙니다.** 이름이 `rt` 로 끝나지만 한글명이
「전일대비」이고 값도 등락폭입니다. 등락률은 `occurs1_rt` 입니다.

**`occurs1_pre_sett_dy` 를 최종결제일로 쓰지 않습니다.** 명세의 한글명은
「최종결제일」인데, 값은 만기가 아니라 **조회일의 직전 영업일로 보입니다.** 그래서
`prevSettlementDate` 라는 제 이름으로만 내보내고, 화면의 최종결제일 칸
(`lastTradingDay`)은 비워 둡니다.

## 봉의 시각은 한국 시간으로 봅니다

응답에 시간대가 안 적혀 있습니다. 분봉 응답의 두 가지가 같은 곳을 가리킵니다.

**06시대가 통째로 비어 있습니다.** `060000` 과 `070000` 사이에 줄이 하나도
없습니다. CME 정비 시간(16:00~17:00 CT)이 한국의 06:00~07:00 입니다.

**해외영업일이 정확히 `070000` 에서 바뀝니다.** 그 앞은 전날, 뒤는 다음 날로
적혀 옵니다. CME 가 17:00 CT 에 다음 거래일을 여는 자리와 같습니다.

그래서 `exch_tm` 을 **거래소 현지가 아니라 한국 시간**으로 읽습니다 —
`quotes.timestamp()` 가 이 컴퓨터 시간대로 읽는 것과 맞습니다.

**둘 중 앞의 것만으로는 못 가립니다.** 현지 시간이어도 16~17시가 비고 17시에
영업일이 바뀝니다 — 관계는 같고 **자리만 다릅니다.** 그래서 시험은 관계(쉬는
한 시간이 영업일 바뀌는 자리 바로 앞)와 자리(그 경계가 한국의 아침)를 함께
봅니다. 관계만 보면 시간대를 못 가리고, 자리만 못박으면 겨울에 헛되이
깨집니다.

미국 서머타임 동안은 16:00 CT 가 06:00 KST 이고, 겨울에는 같은 장면이
07:00~08:00 KST 로 옮겨 갑니다. **응답이 현지 시간이라면 계절과 상관없이 16~17시대가
비어야 합니다** — 겨울 응답으로 두 경우가 갈립니다.

## 화면이 이어 붙이는 법

화면은 **국내와 같은 상태 셋**(`loading`·`ready`·`failed`)으로 잇습니다. 부르는
자리는 셋입니다.

    POST /api/overseas/price         현재가 — **여러 종목을 한 번에**
    POST /api/overseas/bid-price     호가 — 다섯 단계
    POST /api/overseas/chart-minute  분봉 — 오래된 것부터

국내와 다른 자리가 셋입니다.

**현재가는 배열로 옵니다.** `quotes[]` 를 종목별로 흩어 담고 줄마다 있는
`code` 로 대조합니다. 홈 관심종목처럼 여럿을 보는 자리는 한 번만 부르면 됩니다.

**거래소코드를 함께 보냅니다.** 종목 마스터의 `exch` 와 `kind` 로 만듭니다 —
선물이면 `F`, 옵션이면 `O` 를 앞에 붙입니다.

**빈 칸은 `null` 로 옵니다.** 미결제약정·이론가처럼 해외에 없는 값은 국내와
같은 이름으로 오되 값이 `null` 입니다. 화면은 국내에서 하던 그대로 비우면
됩니다.
"""
from __future__ import annotations

from typing import Any

from backend import market_values, quotes

MARKET = "overseas"

#: 해외 호가는 다섯 단계까지 옵니다(국내는 열 단계).
BOOK_DEPTH = 5


def quote(row: dict[str, Any], *, asked: str = "") -> dict[str, Any]:
    """현재가 한 줄을 화면 모양으로.

    국내와 같은 칸 목록을 내고, 해외에 없는 값은 `null` 입니다.
    """
    made: dict[str, Any] = {
        "code": quotes.code_of(row, "occurs1_sym", asked),
        "name": str(row.get("occurs1_sym_nm") or "").strip(),
        "last": market_values.number(row.get("occurs1_last_pric")),
        # 등락폭과 등락률입니다. 이름 함정은 이 파일 첫머리에.
        "change": market_values.number(row.get("occurs1_diff_rt")),
        "changePct": market_values.number(row.get("occurs1_rt")),
        "open": market_values.number(row.get("occurs1_open_pric")),
        "high": market_values.number(row.get("occurs1_high_pric")),
        "low": market_values.number(row.get("occurs1_low_pric")),
        "volume": market_values.count(row.get("occurs1_cum_trd_qty")),
        # **상장 이후** 최고·최저입니다(`life_*`). 한 달 범위가 아닙니다.
        "lifeHigh": market_values.number(row.get("occurs1_life_high_pric")),
        "lifeLow": market_values.number(row.get("occurs1_life_low_pric")),
    }

    # 국내에는 있고 해외 응답에는 없는 칸들. 화면 타입이 고정이라 비워서 냅니다.
    # 국내에 있고 해외에 없는 칸입니다.
    for missing in (
        "turnover",
        "openInterest",
        "openInterestChange",
        "upperLimit",
        "lowerLimit",
        "spot",
        "spotChange",
        "theory",
        "disparity",
        "iv",
        "basisTheory",
        "basisActual",
        "lastTradingDay",
        "daysLeft",
        "multiplier",
    ):
        made[missing] = None

    # 해외에만 있는 값. 있으면 더합니다.
    _put(made, "settlement", market_values.number(row.get("occurs1_sett_pric")))
    _put(made, "prevClose", market_values.number(row.get("occurs1_pre_close_pric")))
    _put(made, "upVolume", market_values.count(row.get("occurs1_ris_trd_qty")))
    _put(made, "downVolume", market_values.count(row.get("occurs1_fal_trd_qty")))
    _put(made, "exchange", str(row.get("occurs1_exch_cd") or "").strip() or None)
    # 실시간 구독이 쓰는 코드입니다. 조회 코드와 다를 수 있어 함께 냅니다.
    _put(made, "realCode", str(row.get("occurs1_real_sym") or "").strip() or None)
    _put(made, "businessDate", market_values.date(row.get("occurs1_biz_dt")))
    _put(made, "time", market_values.clock(row.get("occurs1_trd_tm")))
    _put(made, "prevSettlementDate", market_values.date(row.get("occurs1_pre_sett_dy")))
    _put(made, "lifeHighDate", market_values.date(row.get("occurs1_life_high_dt")))
    _put(made, "lifeLowDate", market_values.date(row.get("occurs1_life_low_dt")))
    made["market"] = MARKET
    return made


def quotes_of(raw: dict[str, Any], *, asked: list[str] | None = None) -> dict[str, Any]:
    """현재가 응답(여러 종목)을 화면 모양으로.

    **부른 차례와 답의 차례가 같다고 믿지 않습니다.** 줄마다 종목이 적혀 있어
    화면이 대조합니다. 원문 종목 칸이 비어 올 때만 부를 때 쓴 코드를 **같은
    자리의 것으로** 채웁니다 — 그마저 없으면 빈 값입니다.
    """
    rows = raw.get("Occurs1")
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    asked = list(asked or [])

    made = []
    for place, row in enumerate(rows):
        made.append(quote(row, asked=asked[place] if place < len(asked) else ""))
    return {"quotes": made}


def book(raw: dict[str, Any], *, asked: str = "") -> dict[str, Any]:
    """호가 응답을 화면 모양으로.

    `asks` 는 **먼 값부터**, `bids` 는 가까운 값부터입니다. 값이 안 채워진
    단계는 빠지므로 둘 다 빈 배열일 수 있습니다 — 해외는 그 단계가 빈 문자열로
    옵니다(국내는 0).

    **호가 응답에 현재가 한 벌이 딸려 옵니다**(현재가·시가·고가·저가·거래량과
    단계별 건수·등락률, 총잔량). 화면은 현재가를 따로 부르므로 여기서는 호가만
    냅니다 — 같은 값을 두 자리에서 내면 어느 쪽이 새 값인지 화면이 못 가립니다.

    `time` 은 호가접수시간 `qt_rcv_tm` 을 `HH:MM:SS` 로 낸 것으로, 실시간 호가의
    `time` 과 같은 한국시각입니다(받은 순간이 한국 09:03:36 이면 값은 `090336`).
    원문에 없거나 시각이 아니면 `null` 입니다.
    """
    return {
        "code": quotes.code_of(raw, "sym", asked),
        "market": MARKET,
        "time": market_values.clock(raw.get("qt_rcv_tm")),
        "asks": market_values.asks(_levels(raw, "ask")),
        "bids": market_values.bids(_levels(raw, "bid")),
    }


def _levels(raw: dict[str, Any], side: str) -> list[tuple[Any, Any]]:
    """호가 다섯 단계를 (가격, 잔량) 짝으로. 최우선 단계부터입니다."""
    return [
        (raw.get(f"{side}_pric_{step}"), raw.get(f"{side}_qty_{step}"))
        for step in range(1, BOOK_DEPTH + 1)
    ]


def _put(into: dict[str, Any], key: str, value: Any) -> None:
    """값이 있을 때만 담습니다."""
    if value is not None:
        into[key] = value
