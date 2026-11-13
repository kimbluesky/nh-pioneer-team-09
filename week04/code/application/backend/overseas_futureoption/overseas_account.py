"""해외 계좌 응답을 화면이 쓰는 모양으로 바꿉니다.

국내(`backend/domestic_futureoption/domestic_account.py`)와 같은 값 규칙을
따릅니다 — 비었거나 숫자로 안 읽히면 `null`, 소수 자릿수는 채우지 않습니다.
계좌번호와 계좌명도 같은 이유로 버립니다.

## 예수금이 격자가 아닙니다

국내 예수금은 항목 일곱이 총액·현금·대용·외화 네 칸을 다 가져서 표 하나로
그려집니다. **해외는 다릅니다.** 네 구분이 다 있는 항목은 **개시예탁과 예탁
둘뿐**이고 나머지 마흔 남짓은 총액 하나로만 옵니다. 게다가 해외에만 있는
항목이 많습니다 — 주문증거금(선물·옵션), 포지션증거금, 사후증거금, 환전가능,
총계정자산가치, 위험비율.

그래서 격자로 만들지 않고 **「항목 목록 + 있는 구분만」** 으로 냅니다. 없는
칸을 `null` 로 채우면 화면에 빈 칸이 수십 개 서는데, 그건 「값이 0」이 아니라
**「그런 칸이 없다」** 입니다. 네 구분이 있는 항목만 `values` 를 들고, 나머지는
`value` 하나를 듭니다. 화면은 `values` 가 있는지로 두 모양을 가릅니다.

## 통화를 정해야 부를 수 있습니다

요청에 **`cur_cd`(통화코드)와 `cvs_div`(환산구분)가 둘 다 필수**입니다.
`cvs_div` 는 통화를 나눠 볼지(`1` 개별) 하나로 합쳐 볼지(`2` 환산)이고,
합쳐 볼 때도 어느 통화로 합칠지를 `cur_cd` 가 정합니다.

**어느 쪽으로 받은 것인지 응답에 함께 적습니다.** 표에 적힌 수가 원화로 합친
값인지 그 통화의 값인지 화면이 알아야 합니다.

## 자릿수와 환율은 응답이 알려 줍니다

`cur_fgr_cnt`(통화자리수)와 `ref_exrt`(기준환율)가 응답에 옵니다. 종목 마스터를
찾지 않아도 되고, **그 시점의 값이라 마스터보다 정확합니다.**
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

import logging

from backend import market_values
from backend import orders
from backend.nh_api import error_codes
from backend.sessions.hours import iso

# 네 구분(총액·현금·대용·외화)을 다 가진 항목. 국내와 달리 **둘뿐**입니다.
#
#     (열쇠, 줄 이름, 총액, 현금, 대용, 외화)
#
# 개시예탁의 외화 칸 이름이 규칙에서 벗어나는 것은 국내와 같습니다.
#
# **총액이 나머지 세 칸의 합이라고 보면 안 됩니다.** 국내 예수금은 일곱 항목
# 모두 총액 = 현금 + 대용 + 외화 이지만, 해외는 **예탁 총액이 세 칸의 합보다 클
# 수 있습니다**(개시예탁은 맞습니다). 총액에 세 칸 밖의 무언가가 더 들어간다는
# 뜻인데, 그것이 무엇인지는 명세에 없습니다. 그래서 **더하지 않고 응답 값을 그대로
# 냅니다** — 서버가 세는 방식이 셈한 값과 다를 수 있고, 그때는 서버 값이 맞습니다.
DEPOSIT_GROUPS: tuple[tuple[str, str, str, str, str, str], ...] = (
    (
        "start",
        "개시예탁",
        "strt_dps_tot_amt",
        "strt_dps_csh_amt",
        "strt_dps_sbst_amt",
        "strt_dps_fcur_blc_amt",
    ),
    (
        "assets",
        "예탁",
        "dps_tot_amt",
        "dps_csh_amt",
        "dps_sbst_amt",
        "dps_fcur_amt",
    ),
)

# 총액 하나로만 오는 항목. 이름은 명세 한글명 그대로입니다 — 원문 약자를
# 영어로 짐작해 옮기면 뜻이 뒤집힙니다(`fut_cls_pl_amt` 의 `cls` 는 청산이
# 아니라 「장중선물순손실상당액」입니다).
DEPOSIT_SINGLES: tuple[tuple[str, str, str], ...] = (
    ("orderMargin", "주문증거금", "ord_mrgn"),
    ("futuresOrderMargin", "선물주문증거금", "fut_ord_mrgn"),
    ("optionOrderMargin", "옵션주문증거금", "opt_ord_mrgn"),
    ("positionMargin", "포지션증거금", "pstn_mrgn"),
    ("initialMargin", "위탁증거금", "init_mrgn"),
    ("maintenanceMargin", "유지증거금", "mtn_mrgn"),
    ("additionalMargin", "추가증거금", "adt_mrgn"),
    ("afterMargin", "사후증거금", "aff_mrgn_amt"),
    ("orderable", "주문가능금액", "ord_psb_amt"),
    ("withdrawable", "인출가능금액", "wdw_psb_amt"),
    ("exchangeable", "환전가능금액", "fx_psb_amt"),
    ("assetsValue", "예탁자산평가금액", "dps_aset_amt"),
    ("totalAccountValue", "총계정자산가치금액", "tot_acc_aset_wth_amt"),
    ("totalAccountValueWithCollateral", "담보포함총계정자산가치금액", "mgg_incd_tot_acc_aset_wth_amt"),
    ("unrealisedPl", "평가손익금액", "pl_amt"),
    ("futuresUnrealisedPl", "선물평가손익", "fut_opint_pl_amt"),
    ("optionUnrealisedPl", "옵션평가손익금액", "opt_pl_amt"),
    ("realisedPl", "실현손익", "cls_pl_amt"),
    # 손익 값입니다 — 양수는 이익, 음수는 손실입니다. 이름이 「손실」이라 뜻이
    # 반대로 읽히기 쉽지만 부호는 같은 규칙입니다.
    ("futuresIntradayLoss", "장중선물순손실상당액", "fut_cls_pl_amt"),
    ("optionMarketValue", "옵션시장가치금액", "opt_mkt_val_amt"),
    ("fee", "위탁수수료", "fee"),
    ("futuresFee", "선물수수료", "fut_fee"),
    ("optionFee", "옵션수수료", "opt_fee"),
    ("etcFee", "기타수수료", "etc_fee"),
    ("unpaid", "미수금액", "urv_amt"),
    ("nextDaySettlement", "익일결제예정금액", "nxd_stl_pla_amt"),
    # 명세에 단위가 적혀 있지 않습니다(비율인지 퍼센트인지 — 소수 여섯 자리로
    # 옵니다). 받은 값 그대로 냅니다.
    ("riskRate", "위험비율", "rsk_rt"),
)

# 환산구분. 통화를 나눠 볼지 하나로 합쳐 볼지.
BY_CURRENCY = "1"
CONVERTED = "2"
CONVERSION: dict[str, str] = {BY_CURRENCY: "by-currency", CONVERTED: "converted"}


def deposit(raw: dict[str, Any], *, cur_cd: str = "", cvs_div: str = "") -> dict[str, Any]:
    """해외 예수금 응답을 화면 모양으로.

    `groups` 는 네 구분을 다 가진 항목이고 `values` 가 **총액·현금·대용·외화**
    차례입니다. `items` 는 총액 하나로만 오는 항목이라 `value` 를 답니다.
    **둘을 한 표로 합치지 않습니다** — 까닭은 이 파일 첫머리에.

    `cur_cd`·`cvs_div` 는 부를 때 쓴 값입니다. 응답에도 통화가 오지만 환산
    구분은 안 오므로, **무엇으로 물었는지는 물어본 쪽만 압니다.**
    """
    groups = [
        {
            "key": key,
            "label": label,
            "values": [
                market_values.number(raw.get(total)),
                market_values.number(raw.get(cash)),
                market_values.number(raw.get(substitute)),
                market_values.number(raw.get(foreign)),
            ],
        }
        for key, label, total, cash, substitute, foreign in DEPOSIT_GROUPS
    ]

    items = [
        {"key": key, "label": label, "value": market_values.number(raw.get(field))}
        for key, label, field in DEPOSIT_SINGLES
    ]

    asked = str(cvs_div or "").strip()
    return {
        "date": market_values.date(raw.get("biz_dt")),
        # 응답이 알려 주는 통화. 안 오면 부를 때 쓴 값을 적습니다.
        "currency": str(raw.get("cur_cd") or "").strip() or str(cur_cd or "").strip(),
        # 자릿수와 환율은 응답이 스스로 냅니다 — 마스터를 안 찾아도 됩니다.
        "decimals": market_values.count(raw.get("cur_fgr_cnt")),
        "rate": market_values.number(raw.get("ref_exrt")),
        # 표에 적힌 수가 합친 값인지 그 통화의 값인지.
        "conversionCode": asked or None,
        "conversion": CONVERSION.get(asked),
        "groups": groups,
        "items": items,
    }


# 매수매도구분. 국내는 `trd_div` 인데 해외는 `trd_tp` 입니다 — 이름이 다릅니다.
POSITION_SIDE: dict[str, str] = {"1": "long", "2": "short"}
ORDER_SIDE: dict[str, str] = {"1": "buy", "2": "sell"}

# 화면 낱말 → NH 매수매도구분. 위 표를 뒤집어 만듭니다 — 한쪽만 고쳐지면
# 조회와 주문이 서로 다른 방향을 가리킵니다.
SIDE_CODE: dict[str, str] = {name: code for code, name in ORDER_SIDE.items()}

# 주문상태. 값은 국내와 같지만 API 가 달라 따로 둡니다.
ORDER_STATUS: dict[str, str] = {
    "1": "accepted",
    "3": "confirmed",
    "4": "partial",
    "6": "filled",
    "8": "auto-cancelled",
    "9": "rejected",
}

# 주문구분. 국내 `ord_div_cd` 와 값은 같고 칸 이름이 `qt_tp` 로 다릅니다.
ORDER_KIND: dict[str, str] = {"1": "new", "2": "modify", "3": "cancel"}

# 주문유형.
#
# **명세 표기와 다른 글자를 냅니다.** 명세는 「3:STOP-M, 4:STOP-L」인데 실시간
# 주문/체결 알림은 같은 것을 「STOP시장가·STOP지정가」로 적습니다. 화면이
# 주문내역 목록 위에 실시간 알림을 이어 붙이므로 **두 경로가 같은 글자여야**
# 합니다. 국내에서 정한 것과 같은 원칙이라 실시간 쪽 글자를 씁니다.
#
# 명세에는 `5`(OCO)가 없습니다 — 실시간 표에는 있습니다. 안 오는 값이라 여기
# 넣지 않고, 오면 원문 코드만 남습니다.
ORDER_TYPE: dict[str, str] = {
    "1": "시장가",
    "2": "지정가",
    "3": "STOP시장가",
    "4": "STOP지정가",
}

#: 접수된 주문의 상태코드. `ORDER_STATUS` 의 열쇠입니다.
ACCEPTED_STATUS_CODE = "1"

#: 취소 접수의 상태. **`ORDER_STATUS` 에 없는 낱말입니다** — 그 표는 서버가 준
#: 상태코드를 옮긴 것이고, 취소에는 짝이 되는 코드가 없습니다(코드 `8` 은 이
#: 프로그램이 낸 취소가 아니라 서버가 스스로 지운 `auto-cancelled` 입니다). 국내와 같은
#: 낱말을 씁니다 — 화면이 두 시장의 줄을 한 표에 세웁니다.
CANCELLED_STATUS = "cancelled"

# 주문 유효기간. 국내에는 없는 값입니다.
ORDER_VALIDITY: dict[str, str] = {"0": "DAY", "1": "GTC", "6": "GTD"}

# 선물옵션구분. 국내 주문내역의 상품구분과 **값이 다릅니다**(국내는 1선물·
# 2콜옵션·3풋옵션·4스프레드).
PRODUCT_KIND: dict[str, str] = {"1": "futures", "2": "option", "3": "spread"}

# 매체구분. 국내와 같은 값이지만 API 가 달라 따로 둡니다.
ORDER_SOURCE: dict[str, str] = {
    "A": "API",
    "R": "REST API",
    "B": "Back",
    "D": "DMA",
    "F": "FRONT",
    "H": "HTS",
    "M": "MTS",
    "I": "WTS",
    "Y": "YES",
    "X": "Expert",
    "Z": "FIX",
}

# 이 프로그램이 주문을 낼 때 쓰는 길. 이 길로 나간 주문만 화면에서 고칩니다.
OWN_SOURCE = "R"

# 이 자리로 오는 것은 전부 해외입니다. 응답에 시장 칸이 없지만 **어느 시장인지는
# 부른 주소가 정합니다** — 지어낸 값이 아닙니다.
MARKET = "overseas"

logger = logging.getLogger(__name__)


def _positive(text: Any) -> float | None:
    """0 을 「없음」으로 보는 값. 시장가 주문가격이나 미체결 체결가격입니다."""
    value = market_values.number(text)
    return None if value is None or value == 0 else value


def _put(into: dict[str, Any], key: str, value: Any) -> None:
    """값이 있을 때만 담습니다."""
    if value is not None:
        into[key] = value


def _coded(
    into: dict[str, Any], row: dict[str, Any], field: str, name: str, table: dict[str, str]
) -> None:
    """코드는 늘 남기고, 뜻을 아는 값이면 이름도 답니다."""
    code = str(row.get(field) or "").strip()
    if not code:
        return
    into[f"{name}Code"] = code
    _put(into, name, table.get(code))


#: 보유포지션만기조회의 매매구분. **코드가 아니라 글자로 옵니다**(명세 「매도/매수」).
#: 여기 없는 글자는 방향을 `null` 로 두고 글자만 그대로 냅니다.
EXPIRY_SIDE: dict[str, str] = {"매수": "buy", "매도": "sell"}


def expiry_positions(raw: dict[str, Any]) -> dict[str, Any]:
    """해외 보유포지션 만기 조회를 화면 모양으로. 줄 하나가 보유 포지션 하나입니다.

    잔고내역(`positions`)에 없는 **최초통보일**과 인수도 결제 구분이 여기 옵니다.
    `nextKey` 는 원문 그대로 넘깁니다 — 다음 쪽은 화면이 이 키로 다시 부릅니다.
    `count` 는 이 응답에 담은 줄 수입니다.
    """
    rows = raw.get("Occurs1") if isinstance(raw, dict) else None
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    made: list[dict[str, Any]] = []
    for row in rows:
        side_name = str(row.get("occurs1_cd_nm") or "").strip()
        made.append(
            {
                "market": MARKET,
                "code": str(row.get("occurs1_sym") or "").strip() or None,
                "name": str(row.get("occurs1_sym_nm") or "").strip() or None,
                "sideName": side_name or None,
                "side": EXPIRY_SIDE.get(side_name),
                "quantity": market_values.count(row.get("occurs1_pstn_qty")),
                # 명세의 체결가격 칸입니다. 천 단위 쉼표를 붙여 옵니다 — 읽는 규칙은
                # `market_values.number` 에.
                "fillPrice": market_values.number(row.get("occurs1_exec_pric")),
                "fillDate": market_values.date(row.get("occurs1_exec_dt")),
                "currency": str(row.get("occurs1_cur_cd") or "").strip() or None,
                "delivery": str(row.get("occurs1_dlv_sett_div_nm") or "").strip() or None,
                "firstNotice": market_values.date(row.get("occurs1_fst_ntf_dt")),
                "expiry": market_values.date(row.get("occurs1_exp_dt")),
                # 만기일까지 남은 날 — **만기일 − 오늘**입니다. 명세는 「만기일 포함」이라
                # 적지만 실제 값은 만기일을 포함하지 않습니다. 해외 만기일 조회·현재가의
                # 잔존일과 같은 셈입니다. 받은 값 그대로이고 다시 세지 않습니다.
                "daysLeft": market_values.count(row.get("occurs1_rem_dy")),
            }
        )

    return {
        "positions": made,
        "count": len(made),
        "nextKey": str(raw.get("nxt_key") or "").strip() if isinstance(raw, dict) else "",
    }


def positions(raw: dict[str, Any]) -> dict[str, Any]:
    """해외 잔고내역을 화면의 보유 포지션 목록으로.

    국내와 다른 자리가 넷입니다.

        가격    **현재가** — 국내는 종가라 장중에 뜻이 다릅니다
        만기    응답에 옵니다 — 국내는 없어 마스터에서 찾습니다
        통화    응답에 옵니다(통화코드와 원화환산환율)
        종목명  **응답에 없습니다** — 마스터에서 찾아야 합니다

    **종목명이 비는 것을 결함으로 보지 마세요.** 해외 잔고 응답에는 이름 칸이
    아예 없습니다. 화면이 종목코드로 마스터에서 찾습니다.

    손익률도 응답에 없습니다(국내는 옵니다). 화면이 평균가와 현재가로 셈합니다.

    **청산가능수량(`closableQuantity`)은 여기서만 냅니다.** 국내 잔고에도 같은
    값이 오지만 쓰는 화면이 없어 담지 않습니다.
    """
    rows = raw.get("Occurs1")
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    made: list[dict[str, Any]] = []
    for row in rows:
        one: dict[str, Any] = {
            "code": str(row.get("occurs1_sym") or "").strip(),
            "market": MARKET,
        }
        _coded(one, row, "occurs1_trd_tp", "side", POSITION_SIDE)
        _coded(one, row, "occurs1_fut_opt_tp", "productKind", PRODUCT_KIND)

        _put(one, "quantity", market_values.count(row.get("occurs1_opint_qty")))
        _put(one, "closableQuantity", market_values.count(row.get("occurs1_cls_psb_qty")))
        _put(one, "avgPrice", market_values.number(row.get("occurs1_avg_pric")))
        # 국내는 종가, 해외는 현재가입니다.
        _put(one, "lastPrice", market_values.number(row.get("occurs1_last_pric")))
        _put(one, "pl", market_values.number(row.get("occurs1_pl_amt")))
        _put(one, "expiry", market_values.date(row.get("occurs1_exp_dt")))
        _put(one, "currency", str(row.get("occurs1_cur_cd") or "").strip() or None)
        _put(one, "rate", market_values.number(row.get("occurs1_usd_exrt")))
        _put(one, "multiplier", market_values.number(row.get("occurs1_ctr_unt_amt")))

        made.append(one)

    return {"positions": made, "nextKey": str(raw.get("nxt_key") or "").strip()}


#: **이 상태가 되면 더 고칠 수 없습니다** — 체결완료·자동취소·거부.
#: 값은 이 시장의 `ORDER_STATUS` 것이고, 다른 시장과 같아 보여도 표를 함께
#: 쓰지 않습니다. 같은 이름이 API 마다 다른 뜻이던 전례가 있습니다.
#:
#: 주문내역 명세의 주문상태(`occurs1_ord_sts`)는 1 접수·3 확인·4 부분체결·
#: 6 전체체결·8 자동취소·9 거부입니다. 3 은 살아 있는 주문에도 붙으므로 이
#: 표에 넣지 않고, 끝났는지는 잔량(0 이면 끝남)과 함께 봅니다. 국내에서는
#: 취소가 확인된 주문이 「3 확인」·잔량 0 으로 남습니다. 해외도 같은 규칙으로
#: 봅니다.
FINISHED_STATUS = frozenset({"6", "8", "9"})


def _editable(one: dict[str, Any], source: str) -> bool:
    """화면에서 고칠 수 있는 주문인지.

    셋을 다 만족해야 참입니다.

        이 매체      이 프로그램이 낸 주문만 고칩니다.
        잔량 남음    고칠 것이 남아 있어야 합니다.
        끝나지 않음  체결완료·취소·거부는 더 손댈 수 없습니다.

    **매체만 보면 전체 조회의 체결 끝난 이 프로그램의 주문도 참이 됩니다.** 화면은
    이 값 하나로 단추를 세우므로, 그러면 이미 끝난 주문에 정정 단추가 섭니다.

    **잔량을 모르면 거짓입니다.** 「잔량이 0」과 「잔량 칸이 안 왔다」는 다른
    일이지만 답은 같습니다 — 고칠 수 있다고 말할 근거가 없습니다.
    """
    if source != OWN_SOURCE:
        return False

    remaining = one.get("remaining")
    # `None` 은 칸이 없거나 숫자로 못 읽은 것이고, 0 은 남은 것이 없는 것입니다.
    if remaining is None or remaining <= 0:
        return False

    return one.get("statusCode") not in FINISHED_STATUS


def order_history(raw: dict[str, Any]) -> dict[str, Any]:
    """해외 주문내역을 화면의 주문 목록으로.

    국내와 같은 생각입니다 — **줄 하나가 주문 하나이고 진행 상태가 함께**
    들어 있어, 화면의 미체결 표와 체결 표가 같은 목록을 다르게 읽습니다.
    어느 것을 받을지는 부를 때 조회구분으로 고릅니다.

    **정정·취소하면 국내와 달리 줄이 안 쌓입니다.** 정정 뒤 전체 조회에는
    **새 줄 하나만** 남고(원주문 줄이 사라집니다), 취소 뒤에는 **0줄**입니다.
    국내는 같은 자리에 세 줄이 남습니다 — 두
    시장의 목록을 한 표에 세우는 화면이라면 그 차이를 알아야 합니다.

    국내와 다른 자리가 셋입니다.

        시각    **열네 자리**(날짜와 시각이 한 칸에) — 국내는 아홉 자리입니다
        이름    칸 이름이 다릅니다(매매구분·주문구분·주문유형·유효기간)
        종목명  **응답에 없습니다** — 마스터에서 찾아야 합니다

    유효기간(DAY·GTC·GTD)은 국내에 없는 값입니다.

    **거부 사유 칸 이름이 시장마다 다릅니다.** 국내는 거부코드와 거부사유가
    따로 와서 `rejectCode`·`rejectReason` 이고, 해외는 오류코드와 거부사유라
    `errorCode`·`rejectReason` 입니다. 원문이 가리키는 것이 달라 한 이름으로
    합치지 않습니다 — 화면은 둘 다 「왜 안 됐나」를 적는 자리에 씁니다.
    """
    rows = raw.get("Occurs1")
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    made: list[dict[str, Any]] = []
    for row in rows:
        one: dict[str, Any] = {
            "id": str(row.get("occurs1_ord_no") or "").strip(),
            "code": str(row.get("occurs1_sym") or "").strip(),
            "market": MARKET,
        }
        _put(one, "originalOrderNo", orders.original_no(row.get("occurs1_org_ord_no"), one["id"]))
        _put(one, "date", market_values.date(row.get("occurs1_ord_dt")))
        _put(one, "originalOrderDate", market_values.date(row.get("occurs1_org_ord_dt")))
        _put(one, "endDate", market_values.date(row.get("occurs1_ord_ed_dt")))

        _coded(one, row, "occurs1_trd_tp", "side", ORDER_SIDE)
        _coded(one, row, "occurs1_ord_sts", "status", ORDER_STATUS)
        _coded(one, row, "occurs1_qt_tp", "orderKind", ORDER_KIND)
        _coded(one, row, "occurs1_ord_tp", "orderType", ORDER_TYPE)
        _coded(one, row, "occurs1_ord_div", "validity", ORDER_VALIDITY)
        _coded(one, row, "occurs1_fut_opt_tp", "productKind", PRODUCT_KIND)
        _coded(one, row, "occurs1_med_div", "source", ORDER_SOURCE)

        _put(one, "quantity", market_values.count(row.get("occurs1_ord_qty")))
        _put(one, "filled", market_values.count(row.get("occurs1_exec_qty")))
        # 미체결 표가 「주문수량·잔량」을 읽는 자리입니다. 국내 주문내역과
        # 실시간 알림도 같은 이름으로 냅니다.
        _put(one, "remaining", market_values.count(row.get("occurs1_ord_rem_qty")))
        _put(one, "modified", market_values.count(row.get("occurs1_mod_qty")))
        _put(one, "cancelled", market_values.count(row.get("occurs1_cxl_qty")))
        _put(one, "price", _positive(row.get("occurs1_ord_pric")))
        _put(one, "fillPrice", _positive(row.get("occurs1_exec_pric")))
        _put(one, "avgFillPrice", _positive(row.get("occurs1_ord_avg_pric")))
        _put(one, "stopPrice", _positive(row.get("occurs1_stop_pric")))
        _put(one, "currency", str(row.get("occurs1_cur_cd") or "").strip() or None)

        # 열네 자리라 날짜와 시각이 한 칸에 붙어 옵니다.
        received_date, received_time = market_values.stamp(row.get("occurs1_ord_rcv_tm"))
        fill_date, fill_time = market_values.stamp(row.get("occurs1_exec_tm"))
        _put(one, "receivedDate", received_date)
        _put(one, "receivedTime", received_time)
        _put(one, "fillDate", fill_date)
        _put(one, "fillTime", fill_time)
        # 화면이 쓰는 시각 하나 — 국내·실시간과 같은 규칙입니다.
        _put(one, "time", fill_time or received_time)

        if "sourceCode" in one:
            # **잔량과 상태를 다 담은 뒤에 봅니다** — 매체만으로는 못 정합니다.
            one["editable"] = _editable(one, one["sourceCode"])

        _put(one, "errorCode", str(row.get("occurs1_err_cd") or "").strip() or None)
        _put(one, "rejectReason", str(row.get("occurs1_rejt_rsn") or "").strip() or None)

        made.append(one)

    return {"orders": made, "nextKey": str(raw.get("nxt_key") or "").strip()}


# ── 주문가능수량 ────────────────────────────────────────────
#
# 응답이 **양쪽을 다 줍니다** — 매수 셋(신규·청산·합)과 매도 셋, 그리고
# 주문가능금액·품목코드·통화코드. 화면은 한 방향만 쓰므로 물어본 쪽을 골라
# 국내와 같은 낱말로 냅니다.

# 방향별로 어느 칸을 읽을지. 합·신규·청산 차례입니다.
ORDERABLE_FIELDS: dict[str, tuple[str, str, str]] = {
    "buy": ("buy_ord_psb_qty", "new_buy_ord_psb_qty", "buy_cls_psb_qty"),
    "sell": ("sel_ord_psb_qty", "new_sel_ord_psb_qty", "sel_cls_psb_qty"),
}


def orderable_quantity(
    raw: dict[str, Any], *, code: str, side: str, product_code: str | None = None
) -> dict[str, Any]:
    """해외 주문가능수량 응답을 화면 모양으로.

    국내와 **같은 낱말**을 씁니다(`quantity`·`newQuantity`·`closeQuantity`·
    `amount`). 화면의 주문 폼이 시장을 가리지 않고 같은 자리에 적기 때문입니다.
    해외에만 통화와 품목코드가 더 옵니다.

    합(`quantity`)은 명세가 「신규가능과 청산가능의 합」이라고 적은 칸입니다.
    보유 포지션이 없으면 청산가능이 0 이라 합과 신규가 같은 값입니다.

    모르는 방향을 물으면 수량 칸 없이 냅니다 — 지어내지 않습니다.
    """
    made: dict[str, Any] = {"code": code, "market": MARKET, "side": side}
    fields = ORDERABLE_FIELDS.get(side)
    if fields:
        total, opened, closed = fields
        _put(made, "quantity", market_values.count(raw.get(total)))
        _put(made, "newQuantity", market_values.count(raw.get(opened)))
        _put(made, "closeQuantity", market_values.count(raw.get(closed)))
    _put(made, "amount", market_values.number(raw.get("ord_psb_amt")))
    _put(made, "currency", str(raw.get("cur_cd") or "").strip() or None)
    # 품목코드는 **종목 마스터의 품목**입니다(`product_code`). 응답의 품목코드
    # 칸(`prd_cd`)에는 통화 글자가 실려 올 수 있어 그 칸은 쓰지 않습니다.
    # 마스터를 모르면 비웁니다.
    made["productCode"] = product_code or None
    return made


# ── 주문 결과 ───────────────────────────────────────────────


def order_result(
    raw: dict[str, Any],
    *,
    code: str,
    side: str,
    quantity: int,
    kind: str,
    now: datetime,
    environment: str | None = None,
    order_type_code: str = "",
    original_order_no: str = "",
) -> dict[str, Any]:
    """접수된 해외 주문 하나를 **미체결 표 한 줄**로.

    화면은 이 답을 목록 위에 그대로 얹고, 뒤이어 오는 실시간 알림과 주문내역
    조회가 같은 `id` 로 그 줄을 덮습니다. 그래서 칸 이름과 값 체계를
    `order_history` 와 한 벌로 맞춥니다 — **국내 접수 답과도 같은 칸**입니다
    (`domestic_account.order_reply`). 한 표에 두 시장의 줄이 섭니다.

    **국내보다 응답이 넉넉합니다** — 국내는 주문번호와 메시지 세 칸뿐인데
    해외는 영업일자·모주문번호·원주문번호·종목·가격·수량이 함께 옵니다.
    그래서 종목·수량·가격은 **응답 값이 먼저**이고 없으면 요청에 실어 보낸 값을
    씁니다. 상태·잔량·주문유형처럼 서버가 안 주는 것은 보낸 값을
    적습니다 — 지어내는 것이 아니라 무엇을 보냈는지 적는 것이고, 서버가 아는
    값(체결 수량·실제 상태)은 알림과 조회가 채웁니다.

    **시각은 이 프로그램의 시계입니다.** 응답에 접수 시각 칸이 없어 `now`(한국시간)를
    적습니다 — 서버가 접수한 순간이 아니라 **접수 답을 받은 순간**이라,
    실시간 알림의 접수시각과 초 단위로 다를 수 있습니다.

    **거절 판정은 라우트가 먼저 합니다**(`err_cd`). 여기 오는 것은 접수된
    주문입니다.

    **정정·취소의 `ord_no` 는 새 번호입니다** — 국내와 같습니다. `originalOrderNo`
    는 요청이 가리켜 보낸 **직전에 살아 있던 주문**의 번호입니다.

    응답에는 호가구분·매수매도구분(`qt_div_cd`·`trd_div_cd`)도 되비쳐 옵니다.
    **그 둘은 안 씁니다** — 무엇을 보냈는지는 요청이 이미 알고, 되비치는 값이
    보낸 값과 같다는 보장은 명세에 없습니다. 보낸 값을 되비친 값으로 덮지
    않습니다.
    """
    extra: dict[str, Any] = {
        "orderKind": kind,
        # `time` 은 **화면이 줄에 그리는 시각 하나**입니다 — 조회 줄과 알림이
        # 같은 이름으로 냅니다. `acceptedAt` 은 날짜까지 한 값으로 볼 때 씁니다.
        "acceptedAt": iso(now),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "filled": 0,
    }
    if kind == ORDER_KIND["3"]:
        extra["status"] = CANCELLED_STATUS
        extra["remaining"] = 0
    else:
        extra["status"] = ORDER_STATUS[ACCEPTED_STATUS_CODE]
        extra["statusCode"] = ACCEPTED_STATUS_CODE
        extra["remaining"] = market_values.count(raw.get("ord_qty")) or quantity

    type_code = str(order_type_code or "").strip()
    if type_code:
        extra["orderTypeCode"] = type_code
        _put(extra, "orderType", ORDER_TYPE.get(type_code))

    # 해외에만 있는 칸들입니다. 응답이 준 것을 그대로 냅니다.
    _put(extra, "businessDate", market_values.date(raw.get("biz_dt")))
    _put(extra, "parentOrderNo", str(raw.get("mo_ord_no") or "").strip() or None)
    # **보낸 모양 그대로 냅니다**(열 자리로 채운 것). 응답이 그 칸을 채워 오면
    # 그쪽이 먼저입니다 — 서버가 무엇을 가리켰다고 보는지가 더 정확합니다.
    _put(
        extra,
        "originalOrderNo",
        orders.original_no(
            str(raw.get("org_ord_no") or "").strip() or original_order_no, raw.get("ord_no")
        ),
    )
    # 시장가·STOP시장가는 가격을 `0` 으로 실어 보냅니다. 가격이 없는 것이 맞습니다.
    _put(extra, "price", _positive(raw.get("ord_pric")))
    # 이 프로그램이 낸 주문이라 화면에서 고칠 수 있습니다. 조회 줄과 같은 판정입니다.
    extra["sourceCode"] = OWN_SOURCE
    _put(extra, "source", ORDER_SOURCE.get(OWN_SOURCE))
    extra["editable"] = _editable(extra, OWN_SOURCE)

    return orders.accepted(
        order_id=raw.get("ord_no"),
        market=MARKET,
        environment=environment,
        code=str(raw.get("sym") or "").strip() or code,
        side=side,
        quantity=market_values.count(raw.get("ord_qty")) or quantity,
        **extra,
    )


# ── 주문오류내역 ────────────────────────────────────────────
#
# **국내와 달리 코드가 안 옵니다.** 매매구분·주문구분·주문유형·매체구분이 전부
# 이름(`…_nm`)으로만 실려 옵니다. 국내는 코드와 이름이 함께 와서 `sideCode` 와
# `side` 를 둘 다 냈는데, 여기서는 이름만 낼 수 있습니다.
#
# **이름을 코드로 되짚지 않습니다.** 「매수」가 `1` 이라는 표를 여기서 만들면,
# 서버가 문구를 조금만 바꿔도 조용히 틀립니다. 화면이 두 시장 오류를 한 표에
# 섞어 놓는 자리라 **이름 칸은 같은 이름**(`side`·`orderKind`·`orderType`·
# `source`)으로 냅니다.

#: 이름만 오는 칸들. 화면 이름과 원문 칸의 짝입니다.
ERROR_NAMES: tuple[tuple[str, str], ...] = (
    ("side", "occurs1_trd_div_nm"),
    ("orderKind", "occurs1_qt_div_cd_nm"),
    ("orderType", "occurs1_glb_ord_tp_cd_nm"),
    # 해외주문구분코드명. **무엇을 가리키는지 명세에 설명이 없습니다** —
    # 받은 글자를 그대로 올리고 뜻은 정하지 않습니다.
    ("orderDivision", "occurs1_glb_ord_div_cd_nm"),
    ("source", "occurs1_ord_med_div_cd_nm"),
)


def order_errors(raw: dict[str, Any]) -> dict[str, Any]:
    """해외 주문오류내역을 화면 모양으로.

    국내와 다른 자리가 넷입니다.

        열쇠    **있습니다** — 일련번호(`id`)와 주문번호를 따로 냅니다.
                국내는 줄에 주문번호가 없어 열쇠를 못 만듭니다.
        시각    **둘입니다** — 주문 쪽과 국내 쪽. 화면이 쓰는 `date`·`time` 은
                국내 쪽입니다(사용자가 보는 시계).
        코드    안 옵니다. 이름만 옵니다(위 참고).
        진법    `priceNotationCode` 가 옵니다 — 가격을 정수로 적을지 소수로
                적을지, 분수로 적을지가 이 코드에 있습니다.

    **진법 코드의 뜻 표는 여기서 만들지 않습니다.** 같은 표가 종목 마스터
    (`pind`)에도 있고, 두 벌을 만들면 어긋납니다. 화면이
    마스터 쪽 규칙으로 그리면 됩니다.

    **주문 IP 는 내보내지 않습니다.** 줄에 실려 오지만 화면이 쓸 일이 없습니다.

    `messageCode`·`message` 는 **국내 주문오류와 같은 이름·같은 자리**입니다.
    오류 코드 사전이 붙는 날 두 시장이 한 자리에서 받게 하려는 것입니다.
    """
    rows = raw.get("Occurs1")
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    made: list[dict[str, Any]] = []
    for row in rows:
        # 문구 차례는 **원문 → 사전 → 코드만**입니다(국내와 같은 규칙).
        said = error_codes.describe(
            row.get("occurs1_msg_cd"), row.get("occurs1_err_msg")
        )
        one: dict[str, Any] = {
            "id": str(row.get("occurs1_seq_no") or "").strip(),
            "code": str(row.get("occurs1_sym") or "").strip(),
            "market": MARKET,
            "message": said["message"],
        }
        _put(one, "messageCode", said["code"] or None)
        _put(one, "kind", said["kind"] if said["code"] else None)
        if error_codes.needs_detail(said["code"]):
            one["needsDetail"] = True
        _put(one, "orderNo", str(row.get("occurs1_ord_no") or "").strip() or None)
        _put(
            one,
            "originalOrderNo",
            orders.original_no(row.get("occurs1_org_ord_no"), row.get("occurs1_ord_no")),
        )

        for name, field in ERROR_NAMES:
            _put(one, name, str(row.get(field) or "").strip() or None)

        _put(one, "quantity", market_values.count(row.get("occurs1_ord_qty")))
        _put(one, "price", _positive(row.get("occurs1_ord_pric")))
        _put(one, "stopPrice", _positive(row.get("occurs1_stop_pric")))
        _put(one, "priceNotationCode", str(row.get("occurs1_pric_ind") or "").strip() or None)

        # 화면이 쓰는 시각은 국내 쪽입니다.
        _put(one, "date", market_values.date(row.get("occurs1_dom_dt")))
        _put(one, "time", market_values.clock(row.get("occurs1_dom_tm")))
        _put(one, "orderDate", market_values.date(row.get("occurs1_inq_dt")))
        _put(one, "orderTime", _order_clock(row.get("occurs1_inp_tm")))

        made.append(one)

    return {
        "errors": made,
        "count": market_values.count(raw.get("nrec")),
        "nextKey": str(raw.get("nxt_key") or "").strip(),
    }


def _order_clock(text: Any) -> str | None:
    """주문시간. 못 읽으면 기록에 남깁니다.

    **명세 안에서 두 칸이 어긋나 있습니다** — 길이 칸은 8 인데 설명은
    `HHMMSS`(여섯)입니다. 국내 주문내역의 「길이 9·설명 6」과 같은 모양이고,
    국내는 실제로 아홉 자리(밀리초)가 옵니다. 그래서 **여섯·아홉 자리만 읽고**,
    여덟 자리로 오면 못 읽은 채 지나갑니다. 조용히 사라지지 않게 그때 기록을
    남깁니다.
    """
    read = market_values.clock(text)
    body = str(text or "").strip()
    if read is None and body:
        logger.warning("주문시간을 읽지 못했습니다(%d자리): %s", len(body), body)
    return read


# ── 누적손익 ────────────────────────────────────────────────

#: 총합 줄의 품목코드. 명세가 「마지막 행데이터 총합값」이라고 적어 둔 자리입니다.
TOTAL_MARK = "-"


#: 누적손익을 이어 받을 때 부르는 쪽 수의 상한. 쪽마다 계좌 호출 제한을 지나가므로,
#: 서버가 끝없이 다음 키를 주더라도 여기서 멈춥니다.
MAX_PL_PAGES = 20


def collect_pl(
    fetch: Callable[[str], dict[str, Any]], *, first_key: str = ""
) -> tuple[list[dict[str, Any]], bool]:
    """누적손익의 다음 키를 따라 쪽을 **차례대로** 다 모읍니다. `(원문 쪽들, 잘림)`.

    **다음 키가 비었거나 이미 쓴 키가 다시 오면 끝입니다** — 마지막 쪽에도 빈 값이
    아닌 키를 주는 조회가 있어, 같은 키가 되돌아온 것을 마지막 쪽으로 봅니다.
    앞 쪽 다음에 온 답이 **줄이 없거나 바로 앞 쪽과
    줄이 같으면** 그 답은 버리고 앞 쪽을 마지막 쪽으로 봅니다 — 끝 표지로 한 번 더
    부르면 같은 쪽이나 빈 쪽이 올 수 있어, 줄이 두 번 서거나 총합이 빈 쪽 때문에
    사라지지 않게 합니다.

    **잘림**은 상한(`MAX_PL_PAGES`)에 닿았는데 새 다음 키가 남은 경우뿐입니다.

    쪽 하나라도 실패하면 `fetch` 가 올린 예외가 그대로 나갑니다 — 앞 쪽만으로
    답하면 총합 없는 일부가 전부처럼 보입니다.
    """
    pages: list[dict[str, Any]] = []
    used: set[str] = {first_key} if first_key else set()
    key = first_key
    for _ in range(MAX_PL_PAGES):
        raw = fetch(key)
        raw = raw if isinstance(raw, dict) else {}
        if pages and (not _raw_pl_rows(raw) or _raw_pl_rows(raw) == _raw_pl_rows(pages[-1])):
            return pages, False
        pages.append(raw)
        following = str(raw.get("nxt_key") or "").strip()
        if not following or following in used:
            return pages, False
        used.add(following)
        key = following
    logger.warning("누적손익이 %d쪽을 넘어 멈춥니다.", MAX_PL_PAGES)
    return pages, True


def _raw_pl_rows(raw: Any) -> list[dict[str, Any]]:
    """원문 한 쪽의 줄들. 모양이 아니면 빈 목록입니다."""
    rows = raw.get("Occurs1") if isinstance(raw, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def cumulative_pl(
    pages: dict[str, Any] | list[dict[str, Any]], *, truncated: bool = False
) -> dict[str, Any]:
    """해외 누적손익을 화면 모양으로. 원문 한 쪽이나 모은 쪽들을 받습니다.

    줄 하나가 **품목 × 통화**입니다. 통화가 섞여 오므로 화면이 합계를 그리려면
    환산통화로 부른 값을 봐야 합니다.

    **마지막 줄은 품목이 아니라 총합입니다.** 명세가 그 줄의 품목코드를 대시로
    채운다고 적습니다. 목록에 그대로 두면 **화면이 합을 다시 더해 두 배로
    적습니다.** 그래서 빼서 `total` 로 따로 냅니다. 총합은 **마지막 쪽에서만**
    받습니다 — 중간 쪽에 온 총합 줄은 목록에도 두지 않고 기록에만 남깁니다.
    끝까지 받지 못했으면(`truncated`) 총합이 오지 않은 것이라 `null` 입니다.

    「품목코드가 대시뿐인 줄」만 총합으로 보고, 아니면 그냥 줄로 둡니다.
    표시가 다른 모양으로 오면 그 줄은 목록에 남고, 총합은 `null` 이 됩니다.

    `count` 는 **이 응답에 담은 줄 수**(총합 제외)이고, `pages` 는 받아 쓴 쪽 수입니다
    (버린 겹친 쪽은 세지 않습니다).
    쪽은 이미 다 모았으므로 `nextKey` 는 늘 빈 값입니다.
    """
    pages = [pages] if isinstance(pages, dict) else list(pages)

    made: list[dict[str, Any]] = []
    total: dict[str, Any] | None = None
    for index, raw in enumerate(pages):
        last = index == len(pages) - 1
        for row in _raw_pl_rows(raw):
            one = _pl_row(row)
            if _is_total(row):
                if last:
                    total = one
                else:
                    logger.warning("누적손익 %d쪽 가운데에 총합 줄이 와서 뺍니다.", index + 1)
                continue
            made.append(one)

    return {
        "rows": made,
        "total": None if truncated else total,
        "count": len(made),
        "nextKey": "",
        "pages": len(pages),
        "truncated": truncated,
    }


def _is_total(row: dict[str, Any]) -> bool:
    """총합 줄인지. 품목코드가 대시로만 채워져 오면 총합입니다."""
    code = str(row.get("occurs1_prd_cd") or "").strip()
    return bool(code) and set(code) == {TOTAL_MARK}


def _pl_row(row: dict[str, Any]) -> dict[str, Any]:
    """누적손익 한 줄."""
    one: dict[str, Any] = {"market": MARKET}
    _put(one, "productCode", str(row.get("occurs1_prd_cd") or "").strip() or None)
    _put(one, "productName", str(row.get("occurs1_prd_nm") or "").strip() or None)
    # 그 품목의 원래 통화입니다(환산통화가 아님). 줄 금액이 어느 통화인지는 명세에
    # 없습니다 — 환산통화는 응답 머리의 `conversionCurrency`.
    _put(one, "currency", str(row.get("occurs1_cur_cd") or "").strip() or None)
    # 실현손익(선물은 청산손익)과 평가손익, 위탁수수료, 순손익입니다.
    _put(one, "realisedPl", market_values.number(row.get("occurs1_cls_pl_amt")))
    _put(one, "unrealisedPl", market_values.number(row.get("occurs1_pl_amt")))
    _put(one, "fee", market_values.number(row.get("occurs1_fee")))
    _put(one, "netPl", market_values.number(row.get("occurs1_net_pl_amt")))
    return one
