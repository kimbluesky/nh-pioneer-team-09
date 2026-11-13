"""국내 투자자 매매동향 응답을 화면 모양으로 바꿉니다.

**응답이 줄로 오지 않습니다.** 다른 조회는 `Occurs1` 배열로 오는데 이 응답은
납작한 한 벌이고, 투자자 무리를 **칸 이름의 접두사**로 가릅니다 — 개인은
`ind_buy`·`ind_sel`, 외국인은 `frgn_buy`·`frgn_sel` 하는 식으로 무리 열하나에
일곱 칸씩 일흔일곱 칸입니다. 그대로 넘기면 화면이 접두사를 다시 풀어야 하고,
그 풀이가 화면마다 조금씩 달라집니다. 그래서 여기서 **목록으로 세웁니다.**

무리를 합치지 않습니다. 「기관」처럼 여럿을 더한 값은 이 응답에 없고, 무엇을
더해야 그것이 되는지 명세가 말하지 않습니다. 여기서 합쳐 내면 그 합계의 근거를
밝힐 수 없으므로 **온 대로 열하나를 내고 고르는 일은 화면이 합니다.**

**수량과 금액은 다른 조회입니다.** 같은 품목·같은 날이라도 `inq_tp` 에 따라
값이 자릿수째 다릅니다(개인매수 3,165 계약 ↔ 53,165,173,000 원). 어느 쪽인지
모르면 읽을 수 없는 숫자라 응답에 `unit` 으로 적어 냅니다.
"""
from __future__ import annotations

from typing import Any

from backend import market_values
from backend.domestic_futureoption import domestic_futureoption_functions as api

#: 화면이 부르는 종류 → 명세의 선물옵션구분 한 자리. `opt_prd` 의 끝자리입니다.
KIND_CODE: dict[str, str] = {"futures": "1", "call": "2", "put": "3"}

#: 조회구분 → 화면 낱말. 값이 계약 수인지 원인지입니다.
UNITS: dict[str, str] = {api.QUANTITY: "quantity", api.AMOUNT: "amount"}

#: 투자자 무리. **원문 접두사 → 화면 이름**이고 차례가 화면에 서는 차례입니다.
#:
#: 이름은 명세의 한글명을 그대로 옮긴 것입니다. 접두사만 보면 뜻을 짐작하기
#: 어려운 것이 있어(`mb` 는 **회원이 아니라 종금**입니다) 옮긴 표를 적어 둡니다.
#:
#:     ind      개인             individual
#:     frgn     외국인           foreign
#:     scrt     증권선물         securitiesFutures
#:     ins      보험             insurance
#:     invt     투신             investmentTrust
#:     bank     은행             bank
#:     penfnd   연기금           pensionFund
#:     cntry    국가기금공제     nationalFund
#:     mb       종금             merchantBank
#:     etc      기타             other
#:     grs      합계             total
INVESTOR_GROUPS: tuple[tuple[str, str], ...] = (
    ("ind", "individual"),
    ("frgn", "foreign"),
    ("scrt", "securitiesFutures"),
    ("ins", "insurance"),
    ("invt", "investmentTrust"),
    ("bank", "bank"),
    ("penfnd", "pensionFund"),
    ("cntry", "nationalFund"),
    ("mb", "merchantBank"),
    ("etc", "other"),
    ("grs", "total"),
)

#: 화면 이름 → 원문 칸의 접미사.
_FIELDS: tuple[tuple[str, str], ...] = (
    ("buy", "_buy"),
    ("sell", "_sel"),
    ("netBuy", "_net_buy"),
    ("previousBuy", "_pre_buy"),
    ("previousSell", "_pre_sel"),
    ("buyPct", "_buy_rt"),
    ("sellPct", "_sel_rt"),
)

#: 비율로 읽는 칸. 나머지는 계약 수나 원이라 정수로 냅니다.
_RATE_FIELDS = frozenset({"buyPct", "sellPct"})

#: **보험만 순매수 칸 이름이 뒤집혀 있습니다** — 열 무리는 `…_net_buy` 인데
#: 보험은 `ins_buy_net` 입니다. 명세도 실제 응답도 그렇습니다.
#:
#: **대소문자도 어긋납니다.** 3차 명세는 보험 칸을 `Ins_buy` 처럼 대문자로
#: 적었는데 실제로 오는 것은 소문자입니다. 명세를 들고 코드와 대조할 때 걸리는
#: 자리입니다.
_ODD_FIELDS: dict[tuple[str, str], str] = {("ins", "netBuy"): "ins_buy_net"}


def investor_trend(raw: dict[str, Any], *, inq_tp: str, ref_dt: str) -> dict[str, Any]:
    """투자자 매매동향 응답을 `{time, instrument, unit, date, investors}` 로.

    `investors` 는 무리마다 한 줄이고 차례는 `INVESTOR_GROUPS` 와 같습니다.
    **읽힌 값이 하나도 없는 무리는 빼므로 목록이 비어 올 수 있습니다** — 저장해
    둔 응답이 없을 때 재생이 내는 빈 응답이 그렇습니다.

    `ref_dt` 는 실제로 보낸 기준일입니다. 화면이 날짜를 비워 물을 수 있어,
    어느 날 것인지 답이 스스로 말하게 합니다.
    """
    body = raw if isinstance(raw, dict) else {}
    made = [one for one in (_group(body, prefix, name) for prefix, name in INVESTOR_GROUPS) if one]
    return {
        "time": market_values.clock(body.get("tm")),
        # 거래소가 쓰는 상품 식별자입니다(`KRDRVFUUSD`). 종목 마스터의 품목코드가 아닙니다.
        "instrument": str(body.get("inst_cd") or "").strip() or None,
        "unit": UNITS.get(str(inq_tp or "").strip()),
        "date": market_values.date(ref_dt),
        "investors": made,
    }


def _group(body: dict[str, Any], prefix: str, name: str) -> dict[str, Any] | None:
    """무리 하나. **읽힌 값이 하나도 없으면** `None` 입니다.

    칸이 있는지가 아니라 값이 읽혔는지로 봅니다 — 일흔아홉 칸이 전부 빈 글자로
    와도 그 무리는 아무것도 말하지 않은 것이라 목록에 세우지 않습니다. `0` 은
    「0 계약」이라는 값이므로 그대로 세웁니다.
    """
    made: dict[str, Any] = {"group": name}
    found = False
    for field, suffix in _FIELDS:
        origin = _ODD_FIELDS.get((prefix, field), f"{prefix}{suffix}")
        value = body.get(origin)
        read = market_values.number(value) if field in _RATE_FIELDS else market_values.count(value)
        made[field] = read
        found = found or read is not None
    return made if found else None
