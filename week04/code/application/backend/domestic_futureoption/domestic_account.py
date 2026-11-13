"""국내 계좌 응답을 화면이 쓰는 모양으로 바꿉니다.

시세(`domestic_quotes.py`)와 같은 규칙을 따릅니다 — 값이 비었거나 숫자로
읽히지 않으면 `null`, 소수 자릿수는 채우지 않습니다.

## 계좌번호는 내보내지 않습니다

줄이 있는 응답(잔고·주문내역)에는 줄마다 `occurs1_ac_no`(계좌번호)와
`occurs1_ac_nm`(계좌명)이 실려 옵니다. **버립니다.** 요청에는 계좌번호를 넣지
않고(APP Key 가 계좌를 정합니다) 화면에 계좌가 하나뿐이라 쓸 데가 없습니다.
내보내면 캡처·로그·브라우저 기록에 계좌번호가 돌아다니게 됩니다.

예수금 응답에는 그 두 칸이 **아예 없습니다**(명세에도 없습니다). 그래서
예수금에서는 버릴 것이 없습니다.

## 예수금 표 — 항목 일곱 × 구분 넷

**국내는 격자이고 해외는 아닙니다.** 국내는 항목 일곱이 총액·현금·대용·외화
네 칸을 다 가져서 표 하나로 그려집니다. 해외는 네 구분이 다 있는 항목이
둘(개시예탁·예탁)뿐이고 나머지는 총액만 오며, 해외에만 있는 항목(주문증거금
선물·옵션, 포지션증거금, 사후증거금, 환전가능, 총계정자산가치)도 있습니다.
그래서 해외는 **「항목 목록 + 있는 구분만」** 으로 냅니다 — 없는 칸을 `null` 로
채우면 화면에 빈 칸이 스물 몇 개 서는데, 그건 「값이 0」이 아니라 **「그런 칸이
없다」** 입니다. 없는 것은 아예 안 담는 편이 맞습니다.

해외 요청에는 `cvs_div`(1 개별, 2 환산)가 있어 통화를 나눠 볼지 원화로 합쳐
볼지 고릅니다. **화면이 고르는 값**이라 라우트 인자로 받고, 어느 쪽으로 받은
것인지 응답에 함께 적습니다 — 표에 적힌 수가 무엇인지 화면이 알아야 합니다.

한 항목이 총액·현금·대용·외화 네 칸을 가집니다. 그런데 **원문 칸 이름이
항목마다 고르지 않습니다.** 세 자리가 규칙에서 벗어납니다.

    개시예탁 외화   strt_dps_fcur_blc_amt   (다른 항목은 `_fcur_amt`)
    유지증거금 총액  mtn_mrgn_tam_amt        (다른 항목은 `_tot_amt`)
    예탁자산 총액   dps_aset_tot_amt        (아래 참고)

**`dps_amt` 은 예탁자산 총액이 아닙니다.** 이름이 그럴듯하지만 명세의 한글명이
「입금액」이고, 실제 응답에서도 예탁 잔고와 값이 다릅니다. 예탁자산의
총액은 `dps_aset_tot_amt`(당일예탁자산금액)입니다. 이 자리를 `dps_amt` 으로
잡으면 **예수금이 0 으로 보입니다** — 화면은 멀쩡해 보이고 값만 틀립니다.

일곱 항목 모두 **총액 칸은 나머지 세 칸의 합과 같습니다.** 그래도 총액을 따로
더하지 않고 응답 값을 그대로 씁니다 — 서버가 세는 방식이 다를 수 있고, 그때는
서버 값이 맞습니다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from backend import market_values, orders
from backend.nh_api import error_codes
from backend.sessions.hours import iso

# 예수금 표의 한 줄. 화면은 `label` 을 줄 이름으로, `values` 를 네 칸으로 씁니다.
#
#     (열쇠, 줄 이름, 총액, 현금, 대용, 외화)
#
# 순서가 곧 표에 서는 순서입니다. 화면이 일부만 골라 쓸 수 있게 `key` 를 함께
# 냅니다 — 줄 이름은 바뀔 수 있어도 열쇠는 그대로입니다.
DEPOSIT_ITEMS: tuple[tuple[str, str, str, str, str, str], ...] = (
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
        "예탁자산",
        "dps_aset_tot_amt",
        "dps_csh_amt",
        "dps_sbst_amt",
        "dps_fcur_amt",
    ),
    (
        "orderable",
        "주문가능",
        "ord_psb_tot_amt",
        "ord_psb_csh_amt",
        "ord_psb_sbst_amt",
        "ord_psb_fcur_amt",
    ),
    (
        "withdrawable",
        "인출가능",
        "wdw_psb_tot_amt",
        "wdw_psb_csh_amt",
        "wdw_psb_sbst_amt",
        "wdw_psb_fcur_amt",
    ),
    (
        "initialMargin",
        "위탁증거금",
        "init_mrgn_tot_amt",
        "init_mrgn_csh_amt",
        "init_mrgn_sbst_amt",
        "init_mrgn_fcur_amt",
    ),
    (
        "maintenanceMargin",
        "유지증거금",
        "mtn_mrgn_tam_amt",
        "mtn_mrgn_csh_amt",
        "mtn_mrgn_sbst_amt",
        "mtn_mrgn_fcur_amt",
    ),
    (
        "additionalMargin",
        "추가증거금",
        "adt_mrgn_tot_amt",
        "adt_mrgn_csh_amt",
        "adt_mrgn_sbst_amt",
        "adt_mrgn_fcur_amt",
    ),
)

# 표에 안 들어가는 낱개 값.
#
# **이름은 명세 한글명에서 옵니다.** 원문 약자를 영어로 짐작해 옮기면 뜻이
# 뒤집힙니다 — `fut_cls_pl_amt` 의 `cls` 는 청산(closed)이 아니라 「장중선물
# 순손실상당액」이고, `fut_rez_pl_amt` 의 `rez` 는 미실현이 아니라 **확정**
# 손익입니다. 값은 맞게 나오고 이름만 반대라, 화면은 멀쩡히 서고 읽는 사람만
# 틀립니다.
DEPOSIT_SINGLES: tuple[tuple[str, str], ...] = (
    ("fee", "fee"),  # 수수료
    ("nightFee", "nght_fee"),  # 야간수수료
    # 아래 세 칸(선물정산차금·야간정산차금·장중선물순손실상당액)은 **손익 값입니다** —
    # 양수는 이익, 음수는 손실입니다. 장중선물순손실상당액은 이름이 「손실」이라
    # 뜻이 반대로 읽히기 쉽지만 부호는 같은 규칙입니다.
    ("futuresSettlement", "fut_sett_amt"),  # 선물정산차금
    ("nightSettlement", "nght_sett_amt"),  # 야간정산차금
    ("futuresIntradayLoss", "fut_cls_pl_amt"),  # 장중선물순손실상당액
    ("futuresRealisedPl", "fut_rez_pl_amt"),  # 선물확정손익금액
    ("optionUnrealisedPl", "opt_pl_amt"),  # 옵션평가손익금액
    ("optionRealisedPl", "opt_rez_pl_amt"),  # 옵션확정손익금액
    ("totalPl", "pl_tot_pric"),  # 손익총액금액
    ("nextDaySettlement", "nxd_stl_tot_amt"),  # 익일결제총금액
    ("unpaid", "urv_tot_amt"),  # 미수총금액
    # 추가증거금발생구분명. **안 생겼을 때도 글자가 옵니다** — 추가증거금이 0 이어도
    # 「발생 없음」이 채워져 옵니다. 그래서 이 값이
    # 있다고 해서 발생한 것이 아닙니다. 발생 여부가 필요하면 글자가 아니라
    # 추가증거금 총액이 0 보다 큰지로 보세요.
    ("marginCallReason", "adt_mrgn_ocu_div_nm"),
)


# 잔고 줄의 매매구분. 명세에 뜻이 적혀 있는 값입니다.
POSITION_SIDE: dict[str, str] = {"1": "long", "2": "short"}

# 잔고 줄의 거래소코드. **한 응답에 주간 줄과 야간 줄이 섞여 옵니다** — 명세가
# 「결과 값 리스트의 구분자로 주간/야간 구분」이라고 적고 있습니다. 화면은
# 줄마다 어느 장의 것인지 알아야 해서 이 값을 화면 낱말로 바꿉니다.
POSITION_CONTEXT: dict[str, str] = {"KRX": "domestic-day", "KRX_NDV": "domestic-night"}

# 잔고 줄의 상품구분. 낱말은 해외 잔고(`overseas_account.PRODUCT_KIND`)와 같게
# 씁니다 — 두 시장의 같은 표를 화면이 한 벌로 읽습니다.
#
# **코드 `3` 을 옮기지 않습니다.** 명세가 「1:선물 2:옵션 3:그외」라고만 적어
# 「그외」가 무엇인지 밝히지 않습니다. 주문내역의 같은 이름 칸은 「4:스프레드」를
# 따로 두고 있어, 잔고의 `3` 이 스프레드라고 짐작하면 스프레드가 아닌 줄까지
# 스프레드로 읽힐 수 있습니다. 코드는 `productKindCode` 로 늘 냅니다.
POSITION_PRODUCT_KIND: dict[str, str] = {"1": "futures", "2": "option"}

# 날짜 읽기는 조회·실시간과 같은 자리를 씁니다. 세 벌을 두면 한쪽만 고쳐집니다.
_date = market_values.date


def deposit(raw: dict[str, Any], *, asked_date: str = "") -> dict[str, Any]:
    """예수금 응답을 화면 모양으로.

    `items` 는 표의 줄이고 `values` 는 **총액·현금·대용·외화** 차례입니다.
    안 온 칸은 `null` 입니다 — 「0원」과 갈려야 합니다.

    **응답에 영업일자가 없습니다.** 명세에서 `biz_dt` 는 요청에만 있고 답에는
    실려 오지 않습니다. 그래서 어느 날의 값인지는 **물어본 쪽만 압니다.**
    화면이 「언제 기준인가」를 적으려면 그 값이 필요하므로 `asked_date` 로
    받아 그대로 냅니다. 안 넘기면 `date` 가 `null` 입니다.
    """
    items = [
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
        for key, label, total, cash, substitute, foreign in DEPOSIT_ITEMS
    ]

    singles: dict[str, Any] = {}
    for name, field in DEPOSIT_SINGLES:
        value = raw.get(field)
        number = market_values.number(value)
        if number is not None:
            singles[name] = number
            continue
        text = str(value or "").strip()
        # 숫자가 아닌 값(추가증거금 발생 구분명)은 글자 그대로 냅니다.
        singles[name] = text or None

    return {"date": _date(asked_date), "items": items, **singles}


def positions(raw: dict[str, Any]) -> dict[str, Any]:
    """잔고내역 응답을 화면의 보유 포지션 목록으로.

    **줄마다 계좌번호와 위탁계좌명이 실려 오는데 버립니다** — 까닭은 이 파일
    첫머리에.

    화면의 포지션에는 여기서 못 채우는 값이 있습니다. **만기·잔존일수·소수
    자릿수·통화는 이 응답에 없습니다.** 국내 잔고는 종목코드만 주므로 나머지는
    **종목 마스터**에서 찾습니다. 화면이 이미 마스터를 받아 두었으므로 여기서
    다시 보내지 않습니다(해외 잔고에는 만기가 실려 옵니다 — 국내에만 없습니다).

    **가격은 종가입니다.** 국내 잔고가 현재가가 아니라 종가(`occurs1_close_pric`)를
    주므로 장중에는 지금 값과 다릅니다. 화면이 실시간으로 덮을 자리입니다.

    응답에 있지만 **안 내는 값** 셋이 있습니다. 쓰는 화면이 없습니다.

        occurs1_cls_psb_qty      청산가능수량 — 청산 주문 수량을 막을 때 필요해집니다
        occurs1_opint_amt        미결제금액

    필요해지면 여기서 꺼내면 됩니다. 미리 내보내 두면 쓰지도 않는 값이 화면
    계약에 남습니다.
    """
    rows = raw.get("Occurs1")
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    made: list[dict[str, Any]] = []
    for row in rows:
        one: dict[str, Any] = {
            "code": str(row.get("occurs1_sym") or "").strip(),
            "name": str(row.get("occurs1_ko_sym_nm") or "").strip(),
            "quantity": market_values.count(row.get("occurs1_opint_qty")),
            "avgPrice": market_values.number(row.get("occurs1_avg_pric")),
            # 국내는 종가로 옵니다. 해외는 현재가라 이름도 값도 다릅니다.
            "lastPrice": market_values.number(row.get("occurs1_close_pric")),
            "pl": market_values.number(row.get("occurs1_pl_amt")),
            "plPct": market_values.number(row.get("occurs1_pl_rt")),
        }

        side_code = str(row.get("occurs1_trd_div") or "").strip()
        if side_code:
            one["sideCode"] = side_code
            side = POSITION_SIDE.get(side_code)
            if side is not None:
                one["side"] = side

        exchange = str(row.get("occurs1_exch_cd") or "").strip()
        if exchange:
            one["exchangeCode"] = exchange
            context = POSITION_CONTEXT.get(exchange)
            if context is not None:
                one["market"] = context

        kind_code = str(row.get("occurs1_fut_opt_div_cd") or "").strip()
        if kind_code:
            one["productKindCode"] = kind_code
            _put(one, "productKind", POSITION_PRODUCT_KIND.get(kind_code))

        made.append(one)

    return {
        "positions": made,
        # 이어 받을 때 쓰는 값입니다. 지금 화면은 연속조회를 하지 않습니다.
        "nextKey": str(raw.get("nxt_key") or "").strip(),
    }


# 주문내역 줄의 코드 표.
#
# **값은 실시간 주문/체결 알림(`backend/realtime/feeds.py`)과 같지만 함께 쓰지
# 않습니다.** NH선물은 같은 이름의 칸을 API 마다 다른 뜻으로 씁니다 — 바로 아래
# 상품구분이 그렇습니다. 잔고에서는 「1 선물·2 옵션·3 그외」인데 주문내역에서는
# 「1 선물·2 콜옵션·3 풋옵션·4 스프레드」입니다. 표 하나를 돌려 쓰면 그런 자리에서
# 조용히 틀립니다. 값이 같은 것은 우연이 아니라 같은 칸이어서이고, 그래도 API 가
# 갈리면 따로 적는 편이 안전합니다.
ORDER_SIDE: dict[str, str] = {"1": "buy", "2": "sell"}

ORDER_STATUS: dict[str, str] = {
    "1": "accepted",
    "3": "confirmed",
    "4": "partial",
    "6": "filled",
    "8": "auto-cancelled",
    "9": "rejected",
}

# 주문구분코드. 신규인지 정정인지 취소인지.
ORDER_KIND: dict[str, str] = {"1": "new", "2": "modify", "3": "cancel"}

# 주문내역 줄의 상품구분. **잔고의 같은 이름 칸과 표가 다릅니다**(위 참고) —
# 이쪽은 콜과 풋을 갈라 세 번째 자리까지 옵션입니다. 화면은 콜·풋을 종목코드로
# 이미 가르므로 둘 다 `option` 한 낱말로 냅니다. 낱말은 해외와 같게 씁니다.
ORDER_PRODUCT_KIND: dict[str, str] = {
    "1": "futures",
    "2": "option",
    "3": "option",
    "4": "spread",
}

# 스프레드처리구분코드(`occurs1_spd_proc_div_cd`). 「S:스프레드, G:나머지」로
# 주문내역에만 있는 칸입니다. 상품구분이 스프레드라고 말하지 않아도 이 칸이 `S`
# 면 스프레드로 읽습니다 — 둘이 어긋나면 스프레드라고 짚어 말하는 쪽이 더 좁은
# 값이기 때문입니다.
SPREAD_PROCESS_CODE = "S"
SPREAD_KIND = "spread"

# 체결조건코드.
FILL_CONDITION: dict[str, str] = {"1": "FAS", "2": "FOK", "3": "FAK"}

# 주문유형코드. **주문을 낼 때 싣는 칸**이고, 주문내역·실시간 알림이 가격조건
# (`pric_cnd`)으로 내는 이름과 같은 네 값입니다. 접수 답에 주문 종류를 적을 때
# 씁니다 — 방금 무엇을 보냈는지이고 서버가 준 값이 아닙니다.
#
# **주문오류내역의 주문유형(`ERROR_ORDER_TYPE`)과는 다른 표입니다** — 그쪽은
# 「1·2·7」이라 코드가 겹치면서 뜻이 어긋납니다. 표를 돌려 쓰지 않는 까닭은
# 위 `ORDER_SIDE` 첫머리에 있습니다.
ORDER_TYPE: dict[str, str] = {"1": "지정가", "2": "시장가", "3": "조건부", "4": "최유리"}

#: 접수 직후의 상태. `ORDER_STATUS` 의 같은 낱말이고 코드도 그 표의 것입니다.
ACCEPTED_STATUS_CODE = "1"

#: 취소 접수의 상태. **`ORDER_STATUS` 에 없는 낱말입니다** — 그 표는 서버가 준
#: 상태코드를 옮긴 것이고, 취소에는 짝이 되는 코드가 없습니다(코드 `8` 은 이
#: 프로그램이 낸 취소가 아니라 서버가 스스로 지운 `auto-cancelled` 입니다). 그래서 코드 없이
#: 낱말만 냅니다. 취소가 **접수된** 것이지 원주문이 사라진 것을 본 것은 아니므로,
#: 실제 결과는 주문내역 조회와 실시간 알림이 뒤에 덮어씁니다.
CANCELLED_STATUS = "cancelled"

# 매체구분. 이 주문이 어디서 나갔는지입니다.
#
# 실시간 알림의 표와 **같은 값**입니다(명세가 두 곳에 같게 적습니다).
# 위의 상품구분처럼 API 마다 갈리는 칸이 있어 표를 돌려 쓰지는 않되,
# `tests/test_domestic_account.py` 가 둘이 갈라지는 것을 봅니다.
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


def _positive(text: Any) -> float | None:
    """0 을 「없음」으로 보는 값.

    시장가 주문은 주문가격이, 체결되지 않은 주문은 체결가격이 0 으로 옵니다.
    그대로 내면 화면에 「0원」이 섭니다.
    """
    value = market_values.number(text)
    return None if value is None or value == 0 else value


def _put(into: dict[str, Any], key: str, value: Any) -> None:
    """값이 있을 때만 담습니다."""
    if value is not None:
        into[key] = value


#: **이 상태가 되면 더 고칠 수 없습니다** — 체결완료·자동취소·거부.
#: 값은 이 시장의 `ORDER_STATUS` 것이고, 다른 시장과 같아 보여도 표를 함께
#: 쓰지 않습니다. 같은 이름이 API 마다 다른 뜻이던 전례가 있습니다.
#:
#: 주문내역 명세의 주문상태(`occurs1_ord_sts`)는 1 접수·3 확인·4 부분체결·
#: 6 전체체결·8 자동취소·9 거부입니다. **취소가 확인된 주문은 「3 확인」으로
#: 남습니다.** 3 은 살아 있는 주문에도 붙으므로 이 표에 넣지 않고, 끝났는지는
#: 잔량(0 이면 끝남)과 함께 봅니다.
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
    """주문내역 응답을 화면의 주문 목록으로.

    **줄 하나가 주문 하나이고, 그 안에 진행 상태가 함께 들어 있습니다.** 화면의
    「미체결」 표와 「체결」 표는 **같은 목록**을 다르게 읽습니다 — 미체결 표는
    주문수량과 잔량을, 체결 표는 체결가격과 체결시각을 봅니다. 그래서 목록을
    둘로 나누지 않고 한 줄에 둘 다 담습니다. 어느 것을 받을지는 부를 때
    체결구분으로 고릅니다(0 전체·1 체결·2 미체결·3 미접수).

    **정정·취소하면 줄이 늘어납니다.** 신규→정정→취소를 내면 전체
    조회(체결구분 0)에 **세 줄**이 남습니다 — 원주문·정정·취소가 각자
    한 줄이고 번호가 다릅니다. **정정 줄에는 원주문번호 칸이 없고**(그 줄은
    제 번호만 압니다), 미체결 조회(2)의 새 번호 줄에는 있습니다. 그래서 화면이
    「어느 줄이 어느 주문의 뒤를 잇는가」를 **그 칸의 유무로 가르면 안 됩니다**.

    **주문 종류는 코드가 아니라 이름에서 옵니다.** 명세가 가격조건코드
    (`occurs1_pric_cnd_cd`)를 **「(미사용)」** 이라고 적어 두어 뜻을 안 밝힙니다.
    값 자체는 채워져 옵니다. 그래도 **명세가 뜻을 안 밝힌 코드를 짐작해 옮기지
    않습니다.** 이름
    칸(`occurs1_pric_cnd_nm`)에 「지정가」「시장가」처럼 그대로 들어오므로
    그것을 씁니다.

    계좌번호·위탁계좌명·사용자IP·주문ID 는 **내보내지 않습니다** — 이 파일
    첫머리 참고.

    **상품구분(`productKind`)은 칸 둘에서 옵니다.** 상품구분코드
    (`occurs1_fut_opt_div_cd`)가 기본이고, 스프레드처리구분코드
    (`occurs1_spd_proc_div_cd`)가 `S` 면 스프레드로 덮습니다. 스프레드 주문은
    다리 둘이 한 줄로 오므로 화면이 그 줄을 다르게 읽어야 합니다.

    **`id` 는 주문번호라 한 주문이 나눠 체결되면 줄마다 같은 값이 됩니다.**
    지금 화면은 주문 단위로 보므로 문제가 없지만, 체결을 건별로 세우는 표를
    그릴 때는 열쇠가 모자랍니다. 그때는 거래소체결번호(`occurs1_exch_exec_no`)를
    함께 쓰면 됩니다 — 응답에 있고 지금은 내지 않습니다.
    """
    rows = raw.get("Occurs1")
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    made: list[dict[str, Any]] = []
    for row in rows:
        one: dict[str, Any] = {
            "id": str(row.get("occurs1_ord_no") or "").strip(),
            "code": str(row.get("occurs1_sym") or "").strip(),
            "name": str(row.get("occurs1_sym_nm") or "").strip(),
        }

        _put(one, "date", market_values.date(row.get("occurs1_trd_dt")))
        _put(one, "originalOrderNo", orders.original_no(row.get("occurs1_org_ord_no"), one["id"]))
        _put(one, "originalOrderDate", market_values.date(row.get("occurs1_org_ord_dt")))

        exchange = str(row.get("occurs1_exch_cd") or "").strip()
        if exchange:
            one["exchangeCode"] = exchange
            _put(one, "market", POSITION_CONTEXT.get(exchange))

        # 화면 이름, 원문 칸, 코드 표. 코드는 늘 남기고 뜻을 아는 값이면 이름도.
        for name, field, table in (
            ("side", "occurs1_trd_div", ORDER_SIDE),
            ("status", "occurs1_ord_sts", ORDER_STATUS),
            ("orderKind", "occurs1_ord_div_cd", ORDER_KIND),
            ("fillCondition", "occurs1_exec_cnd_cd", FILL_CONDITION),
            ("productKind", "occurs1_fut_opt_div_cd", ORDER_PRODUCT_KIND),
        ):
            code = str(row.get(field) or "").strip()
            if code:
                one[f"{name}Code"] = code
                _put(one, name, table.get(code))

        # 상품구분이 스프레드라고 말하지 않아도 이 칸이 말하면 스프레드입니다.
        if str(row.get("occurs1_spd_proc_div_cd") or "").strip() == SPREAD_PROCESS_CODE:
            one["productKind"] = SPREAD_KIND

        # 주문 종류는 코드가 아니라 이름 칸에서 옵니다(위 참고).
        _put(one, "orderType", str(row.get("occurs1_pric_cnd_nm") or "").strip() or None)
        _put(one, "statusName", str(row.get("occurs1_ord_sts_nm") or "").strip() or None)

        _put(one, "quantity", market_values.count(row.get("occurs1_ord_qty")))
        _put(one, "filled", market_values.count(row.get("occurs1_exec_qty")))
        _put(one, "remaining", market_values.count(row.get("occurs1_rem_ord_qty")))
        _put(one, "modified", market_values.count(row.get("occurs1_mod_qty")))
        _put(one, "cancelled", market_values.count(row.get("occurs1_cxl_qty")))

        _put(one, "price", _positive(row.get("occurs1_ord_pric")))
        _put(one, "fillPrice", _positive(row.get("occurs1_exec_pric")))
        _put(one, "avgFillPrice", _positive(row.get("occurs1_avg_exec_pric")))
        _put(one, "stopPrice", _positive(row.get("occurs1_stop_pric")))

        received = market_values.clock(row.get("occurs1_exch_rcv_tm"))
        filled_at = market_values.clock(row.get("occurs1_exch_exec_tm"))
        _put(one, "receivedTime", received)
        _put(one, "fillTime", filled_at)
        # 화면이 쓰는 시각 하나. 실시간 알림과 **같은 규칙**입니다 — 체결됐으면
        # 체결시각, 아니면 접수시각. 두 경로가 규칙을 따로 두면 화면이 조회 줄과
        # 알림 줄에 시각을 다르게 적게 됩니다.
        _put(one, "time", filled_at or received)

        source = str(row.get("occurs1_med_div") or "").strip()
        if source:
            one["sourceCode"] = source
            _put(one, "source", ORDER_SOURCE.get(source))
            one["editable"] = _editable(one, source)

        _put(one, "rejectCode", str(row.get("occurs1_rejt_rsn_cd") or "").strip() or None)
        _put(one, "rejectReason", str(row.get("occurs1_ord_rejt_rsn") or "").strip() or None)

        made.append(one)

    return {
        "orders": made,
        # 이어 받을 때 쓰는 값입니다. 지금 화면은 연속조회를 하지 않습니다.
        "nextKey": str(raw.get("nxt_key") or "").strip(),
    }


#: 접수 답의 `market`. **줄의 `market` 과 다른 칸입니다** — 주문내역 줄은 거기에
#: 맥락(`domestic-day`·`domestic-night`)을 담고, 접수 답은 실시간 주문 알림과 같이
#: 시장만 담습니다. 맥락은 `context` 로 따로 냅니다.
MARKET = "domestic"


def order_reply(
    *,
    order_id: str,
    kind: str,
    environment: str | None,
    code: str,
    side: str,
    quantity: int,
    context: str,
    now: datetime,
    order_type_code: str = "",
    fill_condition_code: str = "",
    price: str = "",
    original_order_no: str = "",
) -> dict[str, Any]:
    """접수된 주문 하나를 **미체결 표 한 줄**로.

    화면은 이 답을 목록 위에 그대로 얹고, 뒤이어 오는 실시간 알림과 주문내역
    조회가 같은 `id` 로 그 줄을 덮습니다. 그래서 칸 이름과 값 체계를 `order_history`
    와 한 벌로 맞춥니다 — 한쪽을 고치면 다른 쪽도 함께 고쳐야 합니다.

    **여기 담기는 값은 대부분 방금 요청에 실어 보낸 것입니다.** 국내 주문 응답은
    주문번호와 메시지 두 칸뿐이라(`ord_no`·`msg_cd`·`msg_cntn`) 종목·방향·수량·
    주문유형은 서버가 주지 않습니다. 지어내는 것이 아니라 무엇을 보냈는지 적는
    것이고, 서버가 아는 값(체결 수량·실제 상태)은 알림과 조회가 채웁니다.

    **시각도 이 프로그램의 시계입니다.** 응답에 접수 시각 칸이 없어 `now`(한국시간)를
    적습니다. 서버가 접수한 순간이 아니라 **접수 답을 받은 순간**이라,
    실시간 알림의 접수시각과 초 단위로 다를 수 있습니다.

    `kind` 는 `ORDER_KIND` 의 낱말(`new`·`modify`·`cancel`)입니다. 취소는 잔량이
    0 이고 상태가 `cancelled` 입니다 — 그 낱말의 사정은 `CANCELLED_STATUS` 에.

    **정정·취소의 `ord_no` 는 새 번호입니다.** `originalOrderNo` 는 요청이 가리켜
    보낸 번호 — **직전에 살아 있던 주문**입니다. 정정하면 새 번호가 서고, 그 뒤
    취소는 정정된 번호를 가리킵니다.
    """
    cancelling = kind == ORDER_KIND["3"]
    extra: dict[str, Any] = {
        "orderKind": kind,
        "context": context,
        # `time` 은 **화면이 줄에 그리는 시각 하나**입니다 — 조회 줄과 알림이
        # 같은 이름으로 냅니다. `acceptedAt` 은 날짜까지 한 값으로 볼 때 씁니다.
        "acceptedAt": iso(now),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "filled": 0,
    }
    if cancelling:
        extra["status"] = CANCELLED_STATUS
        extra["remaining"] = 0
    else:
        extra["status"] = ORDER_STATUS[ACCEPTED_STATUS_CODE]
        extra["statusCode"] = ACCEPTED_STATUS_CODE
        extra["remaining"] = quantity

    type_code = str(order_type_code or "").strip()
    if type_code:
        extra["orderTypeCode"] = type_code
        _put(extra, "orderType", ORDER_TYPE.get(type_code))

    condition_code = str(fill_condition_code or "").strip()
    if condition_code:
        extra["fillConditionCode"] = condition_code
        _put(extra, "fillCondition", FILL_CONDITION.get(condition_code))

    # 시장가·최유리는 가격을 `0` 으로 실어 보냅니다. 가격이 없는 것이 맞습니다.
    _put(extra, "price", _positive(price))
    _put(extra, "originalOrderNo", orders.original_no(original_order_no, order_id))
    # 이 프로그램이 낸 주문이라 화면에서 고칠 수 있습니다. 조회 줄과 같은 판정입니다.
    extra["sourceCode"] = OWN_SOURCE
    _put(extra, "source", ORDER_SOURCE.get(OWN_SOURCE))
    extra["editable"] = _editable(extra, OWN_SOURCE)

    return orders.accepted(
        order_id=order_id,
        market=MARKET,
        environment=environment,
        code=code,
        side=side,
        quantity=quantity,
        **extra,
    )


# 주문오류내역 줄의 주문유형코드.
#
# **주문내역과 다른 체계입니다** — 주문내역은 가격조건(1 지정가·2 시장가·3
# 조건부·4 최유리)인데 여기는 「1 지정가·2 시장가·**7** STOP주문」입니다. 같은
# 「주문 종류」를 말하면서 코드가 다르므로 표를 돌려 쓰면 안 됩니다.
#
# 응답에 이름 칸(`occurs1_ord_tp_nm`)이 함께 오므로 그것을 먼저 쓰고, 비어
# 올 때만 이 표를 씁니다.
ERROR_ORDER_TYPE: dict[str, str] = {"1": "지정가", "2": "시장가", "7": "STOP주문"}

# 주문형태코드. 주문유형과 **다른 칸**이고 둘 다 STOP 을 말합니다. 뜻을 합치지
# 않고 코드와 이름을 그대로 냅니다 — 둘이 어떻게 함께 오는지 명세에 없습니다.
ERROR_ORDER_FORM: dict[str, str] = {"1": "일반주문", "2": "STOP주문"}


def _named(row: dict[str, Any], name_field: str, code: str, table: dict[str, str]) -> str | None:
    """이름 칸을 먼저 쓰고, 비어 있으면 표에서 찾습니다."""
    text = str(row.get(name_field) or "").strip()
    return text or table.get(code)


def order_errors(raw: dict[str, Any]) -> dict[str, Any]:
    """주문오류내역 응답을 화면 모양으로.

    **응답에 주문번호와 주야간 구분이 없습니다.** 화면의 오류 한 건에는 목록
    열쇠(`id`)와 시장(`market`)이 있는데 이 응답으로는 **둘 다 못 채웁니다** —
    줄에 있는 번호는 원주문번호뿐이고 그마저 신규 주문이 거부되면 비어 옵니다.
    거래소코드 같은 주야간 구분 칸도 없고 요청에도 없습니다.

    없는 값을 접수시각과 종목으로 지어내면 그럴듯한 열쇠가 되지만, **지어낸
    값은 뒤에 진짜와 구분되지 않습니다.** 그래서 만들지 않습니다.
    목록 열쇠가 필요하면 받은 순서로 화면이 만들면 됩니다.

    **이어 받기 항목이 다른 조회와 다릅니다.** 다른 응답은 `nxt_key`·`nrec` 뿐인데
    여기는 전체 건수(`data_tot_cnt`)와 다음 여부(`nxt_data_yn`, Y/N)가 더 옵니다.
    화면이 「몇 건 중 몇 건」을 적을 수 있으므로 그대로 냅니다.
    """
    rows = raw.get("Occurs1")
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    made: list[dict[str, Any]] = []
    for row in rows:
        # 문구 차례는 **원문 → 사전 → 코드만**입니다. 사전이 원문을 덮지
        # 않습니다 — 서버가 문구를 고친 날 화면이 옛말을 하게 됩니다.
        said = error_codes.describe(
            row.get("occurs1_msg_cd"), row.get("occurs1_msg")
        )
        one: dict[str, Any] = {
            "code": str(row.get("occurs1_sym") or "").strip(),
            "name": str(row.get("occurs1_sym_nm") or "").strip(),
            "message": said["message"],
        }
        _put(one, "messageCode", said["code"] or None)
        _put(one, "kind", said["kind"] if said["code"] else None)
        if error_codes.needs_detail(said["code"]):
            # 그 코드 하나에 서로 다른 사유가 붙어 옵니다.
            one["needsDetail"] = True
        _put(one, "time", market_values.clock(row.get("occurs1_rcv_tm")))
        _put(
            one,
            "originalOrderNo",
            orders.original_no(row.get("occurs1_org_ord_no"), row.get("occurs1_ord_no")),
        )

        side_code = str(row.get("occurs1_trd_div_cd") or "").strip()
        if side_code:
            one["sideCode"] = side_code
            _put(one, "side", ORDER_SIDE.get(side_code))

        kind_code = str(row.get("occurs1_ord_div_cd") or "").strip()
        if kind_code:
            one["orderKindCode"] = kind_code
            _put(one, "orderKind", ORDER_KIND.get(kind_code))

        condition_code = str(row.get("occurs1_exec_cnd_cd") or "").strip()
        if condition_code:
            one["fillConditionCode"] = condition_code
            _put(one, "fillCondition", FILL_CONDITION.get(condition_code))

        type_code = str(row.get("occurs1_ord_tp_cd") or "").strip()
        if type_code:
            one["orderTypeCode"] = type_code
            _put(one, "orderType", _named(row, "occurs1_ord_tp_nm", type_code, ERROR_ORDER_TYPE))

        form_code = str(row.get("occurs1_ord_form_cd") or "").strip()
        if form_code:
            one["orderFormCode"] = form_code
            _put(one, "orderForm", _named(row, "occurs1_ord_form_nm", form_code, ERROR_ORDER_FORM))

        source = str(row.get("occurs1_med_div") or "").strip()
        if source:
            one["sourceCode"] = source
            _put(one, "source", _named(row, "occurs1_med_div_nm", source, ORDER_SOURCE))

        _put(one, "quantity", market_values.count(row.get("occurs1_ord_qty")))
        _put(one, "price", _positive(row.get("occurs1_ord_pric")))

        made.append(one)

    return {
        "errors": made,
        "total": market_values.count(raw.get("data_tot_cnt")),
        # `Y` 면 이어 받을 것이 남았습니다.
        "hasMore": str(raw.get("nxt_data_yn") or "").strip().upper() == "Y",
        "nextKey": str(raw.get("nxt_key") or "").strip(),
    }


# ── 주문가능수량 ────────────────────────────────────────────
#
# **두 시장이 부르는 법부터 다릅니다.** 국내는 매수인지 매도인지 물어보고 그
# 방향만 답합니다. 해외는 방향을 안 물어보고 양쪽을 한 번에 답합니다. 화면은
# 한 방향만 쓰므로 두 시장 모두 **한 방향으로 냅니다** — 해외는 받은 양쪽에서
# 물어본 쪽을 골라 냅니다(`overseas_account.orderable_quantity`).

# 화면 낱말 → NH 매매구분. 주문내역이 쓰는 표를 뒤집어 만듭니다. 한쪽만
# 고쳐지면 조회와 주문이 서로 다른 방향을 가리키게 됩니다.
SIDE_CODE: dict[str, str] = {name: code for code, name in ORDER_SIDE.items()}


def orderable_quantity(raw: dict[str, Any], *, code: str, side: str) -> dict[str, Any]:
    """주문가능수량 응답을 화면 모양으로.

    응답은 네 칸입니다 — 주문가능수량·청산가능수량·총가능수량·주문가능금액.

    **화면 한 줄에 쓰는 값은 총가능수량입니다.** 명세의 한글명만 보면
    「주문가능수량」이 그 값 같지만, 해외 명세가 같은 자리를 「신규가능과
    청산가능의 합」이라고 적어 두어 국내도 같은 짜임으로 봅니다. 신규분과
    청산분도 함께 내므로 화면이 나눠 보고 싶으면 그대로 있습니다.

    보유 포지션이 없으면 청산가능수량이 0 이라 합과 신규가 같은 값으로 봅니다
    (해외 명세의 짜임을 따릅니다).

    응답에 계좌번호 칸이 없습니다(명세에도 없습니다). 버릴 것이 없습니다.
    """
    made: dict[str, Any] = {"code": code, "market": "domestic", "side": side}
    _put(made, "quantity", market_values.count(raw.get("tot_psb_qty")))
    _put(made, "newQuantity", market_values.count(raw.get("ord_psb_qty")))
    _put(made, "closeQuantity", market_values.count(raw.get("cls_psb_qty")))
    _put(made, "amount", market_values.number(raw.get("ord_psb_amt")))
    return made
