"""국내 품목별세부정보 응답을 화면·판정이 쓰는 모양으로 바꿉니다.

응답은 품목마다 한 줄이고 **주간과 야간이 다른 줄**로 옵니다 — 야간 줄은 품목명이
`(야간)` 으로 시작합니다. 줄에는 품목 **이름만** 있고 코드가 없어, 이름을
두 자리 품목코드로 옮겨(`PRODUCT_CODES`) 마스터 종목의 `trd_code` 와 잇습니다
(`product_row`).

**시간은 품목마다 다릅니다.** 같은 주간 안에 개장이 08:45 인 품목과 09:00 인
품목이 함께 있을 수 있습니다. 그래서 **판정은 그 종목의 품목 줄을 먼저 보고**,
그 줄이 없을 때만 세션 공통값(`sessions`)으로 갑니다.

`sessions` 는 그 세션 줄들의 **최다값**입니다 — 품목을 못 찾았을 때 쓰는 시장
하나의 시계이고, 허브의 세션 방송이 그것으로 섭니다. 갈리는 것이 정상이라
`uniform` 으로 갈렸는지만 함께 냅니다.

    sessions.day    주간 최다값 — 예: 거래 08:45~15:45, 개장 단일가 08:30~08:45, 종가 단일가 15:35~15:45
    sessions.night  야간 최다값 — 거래 18:00~익일06:00, 17:50~18:00, 05:50~06:00
    products        NH선물이 준 줄 그대로(이름·품목코드·갈래·세션·구간·만기·증거금)

**한 품목에 줄이 여럿입니다.** 같은 품목코드에 선물 줄과 옵션 줄이 따로 오고
(예: `01`·`05`·`06`), 주간·야간까지 하면 네 줄이
됩니다. 그래서 줄을 고를 때는 품목코드만이 아니라 **갈래와 세션까지** 봅니다.

개별주식 선물옵션은 이 응답에 없습니다. 최종거래일 거래시간이 주간 줄에서 둘로
갈립니다(지수 15:20 · 통화·국채 11:30).
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from backend import market_values
from backend.master.master_index import FUTURES, OPTION
from backend.sessions.hours import range_dict

logger = logging.getLogger(__name__)

NIGHT_MARK = "(야간)"

#: 품목 이름에서 옵션을 가리는 글자. 갈래 칸이 따로 없어 이름으로 봅니다.
OPTION_MARK = "옵션"

DAY = "day"
NIGHT = "night"

# 세션 공통값으로 내는 세 구간. 이름은 화면 계약과 같습니다.
_SESSION_FIELDS = ("trading", "openingAuction", "closingAuction")

#: 품목 이름 → 품목코드 두 자리.
#:
#: **이 응답에는 품목코드가 없습니다.** 줄의 열쇠가 한글 이름뿐이라, 화면이 이
#: 줄을 자기가 든 품목(종목 마스터의 `trd_code`)과 이어 붙이려면 코드가 있어야
#: 합니다. 그 이음을 화면 여럿이 각자 들지 않도록 여기 한 자리에 둡니다.
#:
#: 코드는 두 곳에 맞춥니다 — 선물전체시세 명세의 품목코드 목록과 종목
#: 마스터의 `trd_code` 입니다. 위클리는 마스터가 `09` 코스피위클리 · `AF`
#: 코스피위클리M · `AJ` 코스닥위클리 · `AK` 코스닥위클리M 으로 갈라 주는데, 이
#: 응답에는 미니 위클리 줄이 없어 `09`·`AJ` 로 봅니다.
#:
#: **표에 없는 이름은 코드를 비우고 경고를 남깁니다.** 이름이 바뀌거나 품목이
#: 늘어난 날 조용히 틀리지 않게 하려는 것입니다 — 줄 자체는 그대로 냅니다.
PRODUCT_CODES: dict[str, str] = {
    "코스피200선물": "01",
    "코스피200옵션": "01",
    "코스피200위클리옵션": "09",
    "미니 코스피200선물": "05",
    "미니 코스피200옵션": "05",
    "코스닥150선물": "06",
    "코스닥150옵션": "06",
    "코스닥150위클리옵션": "AJ",
    "KRX300선물": "08",
    "코스피200변동성지수선물(VKOSPI)": "04",
    "미국달러선물(USD)": "75",
    "유로선물(EUR)": "77",
    "엔선물(JPY)": "76",
    "위안선물(CNH)": "78",
    "3년국채선물(KTB)": "65",
    "10년국채선물(10KTB)": "67",
    "5년국채선물(5KTB)": "66",
    "30년국채선물(30KTB)": "70",
    "코스닥 글로벌 지수": "AG",
    "KRX반도체 섹터지수선물": "AL",
}


def product_code_of(name: str) -> str | None:
    """품목 이름의 품목코드. 표에 없으면 `None` 입니다.

    야간 줄은 이름 앞에 `(야간)` 이 붙을 뿐 같은 품목이라 떼고 찾습니다.
    """
    body = str(name or "").strip()
    if body.startswith(NIGHT_MARK):
        body = body[len(NIGHT_MARK) :].strip()
    return PRODUCT_CODES.get(body)


def kind_of(name: str) -> str:
    """품목 이름이 선물인지 옵션인지.

    **이 응답에는 갈래 칸이 없습니다.** 이름에 「옵션」이 들어 있으면 옵션이고
    나머지는 선물입니다 — 위 `PRODUCT_CODES` 의 스무 이름이 모두 이 규칙을 따릅니다.
    선물 쪽은 대개 「…선물」로 끝나지만 `코스닥 글로벌 지수` 처럼 둘 다 없는 이름도
    있어, **없는 쪽을 선물로 봅니다.**

    화면이 이름을 뜯어보지 않게 하려고 여기서 한 번만 가릅니다 — 이름이 바뀌면
    고칠 자리도 한 곳입니다.
    """
    return OPTION if OPTION_MARK in str(name or "") else FUTURES


def product_detail(raw: dict[str, Any]) -> dict[str, Any]:
    """품목별세부정보 응답 → `{sessions, products}`."""
    rows = raw.get("Occurs1")
    rows = rows if isinstance(rows, list) else []

    products = [_product(row) for row in rows if isinstance(row, dict)]
    sessions = {
        DAY: _common(products, DAY),
        NIGHT: _common(products, NIGHT),
    }
    return {"sessions": sessions, "products": products}


def _product(row: dict[str, Any]) -> dict[str, Any]:
    name = str(row.get("occurs1_dtl_cd_ko_nm") or "").strip()
    code = product_code_of(name)
    if code is None:
        logger.warning("품목 이름 %r 의 품목코드를 모릅니다. 코드 없이 냅니다.", name)
    return {
        "name": name,
        # 이 응답에 없는 값입니다 — 까닭은 `PRODUCT_CODES` 에.
        "productCode": code,
        # 응답에 갈래 칸이 없습니다 — 까닭은 `kind_of()` 에.
        "kind": kind_of(name),
        "session": NIGHT if name.startswith(NIGHT_MARK) else DAY,
        "trading": range_dict(row.get("occurs1_cd_val_inf_4")),
        "openingAuction": range_dict(row.get("occurs1_cd_val_inf_2")),
        "closingAuction": range_dict(row.get("occurs1_cd_val_inf_3")),
        "lastDayTrading": range_dict(row.get("occurs1_cd_val_inf_5")),
        "nearestExpiry": market_values.date(row.get("occurs1_exp_dt")),
        "settlement": str(row.get("occurs1_sett_nm") or "").strip(),
        "tick": market_values.number(row.get("occurs1_cd_val_inf_1")),
        "tickValue": market_values.number(row.get("occurs1_tick_val")),
        "multiplier": market_values.number(row.get("occurs1_mltplr")),
        # 비율은 `21.75%` 처럼 글자로 옵니다. 숫자로 바꾸면 단위가 사라지므로 그대로 둡니다.
        "initialMarginRate": str(row.get("occurs1_init_mrgn") or "").strip(),
        "maintenanceMarginRate": str(row.get("occurs1_mtn_mrgn") or "").strip(),
        "contractInitialMargin": market_values.number(row.get("occurs1_ctr_init_mrgn")),
    }


def product_row(
    products: list[dict[str, Any]],
    contract: dict[str, Any] | None,
    session: str,
    *,
    any_window: bool = False,
) -> dict[str, Any] | None:
    """그 종목이 속한 품목 줄. 없으면 `None`.

    **열쇠는 마스터의 `trd_code` 입니다.** 같은 줄의 `product_code` 는 `KOSPI`
    처럼 생긴 다른 코드라, 품목별세부정보의 두 자리 품목코드와 **절대 맞지
    않습니다** — 그 코드로 찾으면 계약당 증거금이 늘 빕니다.

    갈래(선물·옵션)까지 봅니다. 코드만 보면 옵션 종목이 선물 줄의 값을 물고
    갑니다 — 한 품목에 선물 줄과 옵션 줄이 함께 오는 것이 실제 응답입니다.

    **갈래를 모르는 줄이 옵니다.** 마스터에 없는 종목(만기 지난 옵션 등)은
    코드 모양으로 푼 최소 줄로 오는데 거기에는 갈래가 없습니다. 그때 그 품목에
    갈래가 둘이면 **답하지 않습니다** — 응답에 먼저 온 줄을 내면 만기 지난
    옵션이 선물 시간을 답하고, 그것이 맞는지 틀리는지는 그날 두 값이 같았는지에
    달리게 됩니다.

    `any_window` 는 **그 창의 줄이 없을 때 다른 창의 줄로 답할지**입니다. 틱과
    증거금은 창이 달라도 그 품목의 값이라 참으로 부르고, **시간 구간은 창마다
    아예 다른 값**이라 거짓입니다 — 야간 줄이 없는 품목에 주간 구간을 물리면
    18시에 열려야 할 것이 08시 45분에 열립니다.
    """
    code = str((contract or {}).get("trd_code") or "").strip()
    if not code:
        return None
    kind = str((contract or {}).get("kind") or "").strip()
    same = [one for one in products if str(one.get("productCode") or "") == code]
    if kind:
        same = [one for one in same if str(one.get("kind") or "") == kind]
    elif len({str(one.get("kind") or "") for one in same}) > 1:
        return None
    if not same:
        return None
    found = next((one for one in same if str(one.get("session") or "") == session), None)
    if found is not None:
        return found
    return same[0] if any_window else None


#: 계약당 유지증거금을 못 내는 까닭. 값을 묻는 자리가 함께 냅니다.
#:
#: **화면이 그대로 적는 문장입니다** — 읽는 사람은 이 앱을 쓰는 분이라 「이 응답」
#: 같은 코드 쪽 말이 아니라 화면에서 부르는 이름으로 적습니다.
#:
#: **응답에 그 칸이 없습니다.** 있는 것은 계약당 **개시**증거금과 두 비율뿐이라
#: 「개시액 × 유지율 ÷ 개시율」로 셈해 볼 수는 있는데, 비율이 소수 둘째 자리까지만
#: 와서 그 값이 어긋납니다 — 예를 들어 3년국채선물이 0.98%·0.65% 로 오면, 그대로
#: 셈하면 667,312 원이고 비율이 정확히 3분의 2라면 670,735 원입니다
#: (0.5% 차이). 증거금 부족을 가리는 선을 0.5% 어긋난 값으로 그릴 수는 없습니다.
WHY_NO_MAINTENANCE = "계약당 유지증거금은 품목정보에 없습니다 — 유지증거금률로 보세요"


def margins(row: dict[str, Any] | None) -> dict[str, Any]:
    """품목 줄 하나의 증거금 값들. 줄이 없으면 전부 `None` 입니다.

    **이름은 품목 줄의 것을 그대로 씁니다**(`contractInitialMargin`) — 같은
    숫자를 라우트마다 다른 이름으로 내면 화면이 옮겨 적는 자리가 한 겹 더
    생깁니다. 「계약당」이 이름에 들어 있어야 비율과 안 헷갈립니다.

    **비율은 글자 그대로 냅니다**(`21.75%`) — 숫자로 바꾸면 단위가 사라져,
    받은 값이 비율인지 금액인지 화면이 다시 가려야 합니다.

    **옵션은 넷이 다 빕니다.** 옵션 줄은 증거금 칸이 전부 비어 옵니다 — 매수는 프리미엄 전액이고 매도는 거래소가 셈하는 값이라
    품목마다 정해 둘 수가 없습니다.
    """
    row = row if isinstance(row, dict) else {}
    return {
        "contractInitialMargin": market_values.number(row.get("contractInitialMargin")),
        "initialMarginRate": str(row.get("initialMarginRate") or "").strip() or None,
        "maintenanceMarginRate": str(row.get("maintenanceMarginRate") or "").strip() or None,
        # 응답에 없는 값입니다 — 까닭은 `WHY_NO_MAINTENANCE` 에.
        "maintenanceMargin": None,
    }


def _common(products: list[dict[str, Any]], session: str) -> dict[str, Any]:
    """한 세션의 줄들이 공유하는 구간. 갈리면 가장 많은 값을 씁니다.

    **같은 수면 여는 시각이 이른 값입니다**(`_earliest`). 최다값만 고르면 아홉
    대 아홉일 때 표에 어느 줄이 먼저 오느냐로 정해져, 같은 표를 다시 받았을
    뿐인데 공통 구간이 바뀝니다.

    **품목마다 다른 것이 정상입니다** — 같은 세션에서도 개장이 08:45 인 품목과
    09:00 인 품목이 함께 옵니다. 그래서 갈리는 것은 알림이 아니라 적어 두기만
    합니다. 판정은 종목의 품목 줄을 먼저 보고(`product_row`), 그 줄이 없을 때만
    이 표로 옵니다.

    줄이 하나도 없으면 세 구간이 다 `None` 입니다 — 판정은 그때 내장 표로 갑니다.
    """
    rows = [p for p in products if p["session"] == session]
    made: dict[str, Any] = {"uniform": True}
    for field in _SESSION_FIELDS:
        values = Counter(_frozen(p[field]) for p in rows)
        if not values:
            made[field] = None
            continue
        most = max(values.values())
        winner = min(
            (value for value, count in values.items() if count == most),
            key=_earliest,
        )
        if len(values) > 1:
            made["uniform"] = False
            # **알림이 아닙니다.** 품목마다 다른 것이 이 표의 성질이고, 판정은
            # 품목 줄을 먼저 씁니다. 어느 값이 몇 개였는지만 적어 둡니다.
            logger.info(
                "국내 %s 세션의 %s 는 품목마다 다릅니다(공통 표는 최다값): %s",
                session,
                field,
                dict(values),
            )
        made[field] = dict(winner) if winner is not None else None
    return made


def _frozen(value: dict[str, Any] | None) -> tuple[tuple[str, Any], ...] | None:
    return None if value is None else tuple(sorted(value.items()))


def _earliest(value: tuple[tuple[str, Any], ...] | None) -> tuple[Any, ...]:
    """`_common` 이 동수일 때 견주는 차례. **여는 시각이 이른 값**, 여는 시각이
    같으면 닫는 시각이 이른 값입니다.

    구간이 아예 없는 것(`None`)은 견줄 시각이 없으니 맨 뒤입니다 — 시각이 적힌
    쪽이 있으면 그것을 씁니다.
    """
    if value is None:
        return (1,)
    got = dict(value)
    return (
        0,
        str(got.get("open") or ""),
        str(got.get("close") or ""),
        bool(got.get("nextDay")),
    )
