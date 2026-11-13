"""국내 시세 응답을 화면이 쓰는 모양으로 바꿉니다.

NH선물 응답은 값이 전부 문자열이고, 부호가 따로 오고, 호가 순서가 화면과
반대입니다. 그대로 넘기면 화면마다 같은 함정을 다시 풀게 되고 **그 함정은
조용히 틀립니다.** 그래서 여기서 한 번만 바꿉니다.

## 이 파일이 지키는 것

**숫자는 숫자로 냅니다.** 값이 비었거나 숫자로 읽히지 않으면 `null` 입니다.
소수 자릿수는 채우지 않습니다 — 몇 자리로 적을지는 종목 마스터가 알고 있고
화면이 그 자리에서 정합니다.

**부호는 값에서 만듭니다.** NH 는 `chg` 같은 값에 부호를 담아 보내면서
`chg_sign` 도 따로 보냅니다. 값에 이미 부호가 있으므로 값을 씁니다. 둘이
어긋나면 경고를 남기고 값을 따릅니다 — 부호칸을 적용하면 0 이 −0 이 됩니다.

**호가는 화면 순서로 뒤집습니다.** NH 는 최우선부터 담아 보내는데 화면은
매도를 먼 값부터 받습니다. 주문 화면이 매수·매도 단추 가격을 배열의 끝에서
뽑으므로, 뒤집지 않으면 단추에 가장 나쁜 호가가 붙습니다.

**값이 안 채워진 호가 단계는 뺍니다.** 한 번에 열 단계가 오지만 그 종목에
없는 단계는 가격 0·잔량 0 으로 옵니다. 그대로 넘기면 호가창에 `0.00` 줄이
섭니다. **그래서 호가 배열이 비어 올 수 있습니다** — 받는 쪽이 빈 배열을
다룰 수 있어야 합니다.

**봉은 오래된 것부터 냅니다.** NH 는 최신부터 보내는데 차트는 반대입니다.

**「없음」과 0 을 가릅니다.** 야간에는 현물가가 `0.00` 으로 옵니다. 그대로
0 을 내면 화면에 「현물가 0원」이 섭니다. 이런 자리는 `null` 로 냅니다.

**모든 응답이 종목코드를 함께 냅니다.** 화면은 한 종목의 현재가·호가·봉을
따로 부르고, 답이 오는 차례는 부른 차례와 다를 수 있습니다. 종목을 옮긴
직후에 앞 종목의 답이 늦게 닿으면 **화면에 두 종목의 값이 섞입니다.** 답마다
종목이 적혀 있으면 화면이 대조해서 버릴 수 있습니다.

원문에서 종목을 담는 칸의 이름이 응답마다 다릅니다 — 현재가는 `sym_exch`,
호가와 차트는 `sym` 입니다(현재가의 `spt_sym` 은 현물 대상 종목이라 다른
값입니다). 어느 쪽이든 화면이 보는 이름은 `code` 하나입니다.

**원문 값과 부를 때 쓴 값이 다르면 원문이 이깁니다.** 저장해 둔 응답으로 도는
동안에는 요청한 종목의 기록이 없어 다른 종목의 기록으로 답할 수 있는데, 그때
답에 적힌 종목은 **정말 그 기록의 종목**입니다. 부른 값으로 덮으면 화면이
대조해 봐야 늘 맞다고 나와서 대조하는 뜻이 사라집니다. 부를 때 쓴 코드는
원문 칸이 **비어 올 때만** 씁니다.

## 화면이 채우는 것

시세 응답에 없는 값이 있습니다. 소수 자릿수, 호가단위, 분수 표기, 품목
이름, 거래시간이 그렇습니다. 전부 **종목 마스터**에 있고 화면이 이미 받아
두었으므로 여기서 다시 보내지 않습니다.
"""
from __future__ import annotations

import logging
from typing import Any

from backend import market_values, quotes

logger = logging.getLogger(__name__)

# 부호칸이 말하는 것. 값과 어긋나면 값을 따르고 경고를 남깁니다.
MINUS = "-"

# 호가는 한 번에 이만큼 옵니다. 채워지지 않은 단계는 빼고 냅니다.
BOOK_DEPTH = 10


# 숫자 읽기와 호가 사다리 세우기는 `backend/market_values.py` 에 있습니다.
# 같은 값이 조회로도 오고 실시간으로도 와서, 규칙을 두 벌 두면 한쪽만 고쳐지는
# 날에 호가창이 경로에 따라 다르게 섭니다.
_number = market_values.number
_count = market_values.count


def _signed(raw: dict[str, str], field: str, sign_field: str) -> float | None:
    """부호가 붙은 값. 값의 부호를 쓰고, 부호칸과 어긋나면 경고를 남깁니다."""
    value = _number(raw.get(field))
    if value is None:
        return None

    sign = str(raw.get(sign_field) or "").strip()
    if sign == MINUS and value > 0:
        logger.warning(
            "%s 의 부호칸은 %r 인데 값은 %s 입니다. 값을 따릅니다.", field, sign, value
        )
    return value


_date = market_values.date
_code = quotes.code_of


def _spot(raw: dict[str, str]) -> tuple[float | None, float | None]:
    """현물가와 현물가대비.

    야간에는 현물가가 `0.00` 으로 옵니다. 「0원」이 아니라 「이 세션에는
    없다」는 뜻이라 둘 다 `null` 로 냅니다.
    """
    price = _number(raw.get("spt_pric"))
    if not _has_spot(price):
        return None, None
    return price, _signed(raw, "spt_chg", "spt_chg_sign")


def _has_spot(price: float | None) -> bool:
    """현물가가 있는 응답인지. 없으면(야간의 `0.00`·빈 값) 현물가에 기대는
    값(현물가대비·이론 베이시스·실제 베이시스)도 함께 비웁니다 — 현물가를
    가리면서 그 값으로 센 베이시스만 내면 화면이 알 수 없는 값을 그립니다."""
    return price is not None and price != 0


def traded(volume: int | None) -> bool:
    """그 응답의 세션에 체결이 있었는지. **체결이 0 이면 시가·고가·저가는 없습니다.**

    NH 는 체결이 없던 월물의 시가·고가·저가를 `0` 으로 보냅니다. 그대로 내면
    화면이 「가격 0」으로 그리므로 `null` 로 냅니다. 값 0 이 아니라 **거래량**으로
    가르는 것은, 스프레드 가격은 0 이나 음수가 정상이기 때문입니다.

    야간 현재가는 **야간의 거래량**으로 가릅니다 — 주간에 체결이 있었어도 야간에
    체결이 없으면 야간 시가·고가·저가는 없습니다.

    거래량을 모르면(빈 값) 가리지 않습니다.
    """
    return volume is None or volume != 0


#: 스프레드에는 뜻이 없어 NH 가 `0` 으로 채워 보내는 칸. 스프레드 종목이면 `null`
#: 입니다. 선물에서는 0 이 정상값일 수 있어(괴리율 0 등) 값이 아니라 **종목이
#: 스프레드인지**로 가릅니다.
SPREAD_BLANK: tuple[str, ...] = (
    "upperLimit",
    "lowerLimit",
    "theory",
    "disparity",
    "iv",
    "basisTheory",
    "basisActual",
)


def quote(raw: dict[str, Any], *, asked: str = "", spread: bool = False) -> dict[str, Any]:
    """현재가 응답 하나를 화면 모양으로.

    주간과 야간 응답이 같은 항목을 쓰므로 둘 다 이 함수를 지납니다.

    `asked` 는 부를 때 쓴 종목코드입니다. 원문 종목 칸이 비어 올 때 씁니다.
    `spread` 는 **종목 마스터가 스프레드라고 한** 종목인지입니다(`SPREAD_BLANK`).
    마스터를 모르면 거짓으로 두어 원문 값을 그대로 냅니다.
    """
    spot, spot_change = _spot(raw)
    volume = _count(raw.get("trd_qty"))
    made = {
        "code": _code(raw, "sym_exch", asked),
        "name": str(raw.get("sym_nm") or "").strip(),
        "last": _number(raw.get("last_pric")),
        "change": _signed(raw, "chg", "chg_sign"),
        "changePct": _number(raw.get("rt")),
        "open": _number(raw.get("open_pric")) if traded(volume) else None,
        "high": _number(raw.get("high_pric")) if traded(volume) else None,
        "low": _number(raw.get("low_pric")) if traded(volume) else None,
        "volume": volume,
        # 거래대금은 백만 단위로 옵니다. 원 단위로 바꾸지 않습니다 — 화면이
        # 백만을 받아 「억원」으로 적습니다.
        "turnover": _number(raw.get("trd_amt")),
        "openInterest": _count(raw.get("opint")),
        "openInterestChange": _signed(raw, "opint_chg", "opint_chg_sign"),
        # 상한가·하한가가 아니라 **실시간** 상·하한입니다. 화면의 그 줄이
        # 이 값을 그립니다.
        "upperLimit": _number(raw.get("up_act_pric")),
        "lowerLimit": _number(raw.get("lo_act_pric")),
        "spot": spot,
        "spotChange": spot_change,
        # **상장 이후** 최고·최저입니다(`life_*`). 한 달 범위가 아닙니다.
        "lifeHigh": _number(raw.get("life_high_pric")),
        "lifeLow": _number(raw.get("life_low_pric")),
        "theory": _number(raw.get("theo_pric")),
        "disparity": _number(raw.get("dev_rt")),
        "iv": _number(raw.get("ivol")),
        "basisTheory": _number(raw.get("theo_bas")) if spot is not None else None,
        "basisActual": _number(raw.get("fact_bas")) if spot is not None else None,
        "lastTradingDay": _date(raw.get("lst_trd_dt")),
        "daysLeft": _count(raw.get("rem_dy")),
        "multiplier": _count(raw.get("mltplr")),
    }
    if spread:
        for name in SPREAD_BLANK:
            made[name] = None
    return made


def all_futures(raw: dict[str, Any], *, asked: str = "") -> dict[str, Any]:
    """선물전체시세 응답을 **월물 목록**으로.

    줄 하나가 월물입니다. 만기 목록(만기 드롭다운·차트 칸 배지·월물 줄)이 쓰는
    값만 담고, 이론가·베이시스·괴리도처럼 그 목록에 자리가 없는 칸은 안 냅니다 —
    쓸 곳이 생기면 원문에 그대로 있습니다.

    **칸 이름은 `quote()` 와 같습니다.** 같은 월물을 목록에서 보다가 종목 화면에서
    다시 보는 자리라, 두 경로가 다른 이름으로 같은 값을 말하면 화면이 합칠 수
    없습니다.

    `asked` 는 부를 때 쓴 품목코드입니다. 줄이 하나도 없으면 원문에서 품목을
    읽을 곳이 없어 이 값을 씁니다.
    """
    rows = raw.get("Occurs1")
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    made = [_month(row) for row in rows]
    product = next((one["product"] for one in made if one["product"]), "") or asked.strip()
    # **`count` 는 이 응답에 담은 줄 수입니다.** 원문 건수(`nrec`)를 그대로 옮기면 줄을
    # 빼거나 못 읽은 날 화면이 「7개」라고 적으면서 여섯 줄을 그립니다.
    said = _count(raw.get("nrec"))
    if said is not None and said != len(made):
        logger.warning("선물전체시세 건수가 %s 인데 줄은 %d 개입니다.", said, len(made))
    return {"product": product, "rows": made, "count": len(made)}


def _month(row: dict[str, Any]) -> dict[str, Any]:
    """월물 한 줄. 숫자로 못 읽는 값과 빈 값은 `null` 입니다.

    체결이 없던 월물은 시가·고가·저가가 `null` 입니다(`traded` — 현재가와 같은
    규칙). 현재가와 목록이 같은 월물을 다르게 말하지 않게 합니다.
    """
    volume = _count(row.get("occurs1_trd_qty"))
    live = traded(volume)
    return {
        "code": str(row.get("occurs1_sym_exch") or "").strip(),
        "name": str(row.get("occurs1_sym_nm") or "").strip(),
        "product": str(row.get("occurs1_prd") or "").strip(),
        # **명세와 다르게 옵니다.** 명세는 `YYYYMM` 이라 적었는데 실제 값은
        # `26/09`(YY/MM)입니다. 화면은 종목코드로 월물을 맞추므로 이 칸은 받은
        # 그대로 실어 보내고 가공하지 않습니다.
        "expiryMonth": str(row.get("occurs1_exp_mon") or "").strip(),
        "last": _number(row.get("occurs1_last_pric")),
        "change": _signed(row, "occurs1_chg", "occurs1_chg_sign"),
        "changePct": _number(row.get("occurs1_rt")),
        "open": _number(row.get("occurs1_open_pric")) if live else None,
        "high": _number(row.get("occurs1_high_pric")) if live else None,
        "low": _number(row.get("occurs1_low_pric")) if live else None,
        "volume": volume,
        "openInterest": _count(row.get("occurs1_opint")),
    }


def book(raw: dict[str, Any], *, asked: str = "") -> dict[str, Any]:
    """호가 응답을 화면 모양으로.

    `asks` 는 **먼 값부터**, `bids` 는 **가까운 값부터**입니다. 값이 안 채워진
    단계는 빠지므로 둘 다 빈 배열일 수 있습니다.

    `time` 은 호가 시각(`HH:MM:SS`)입니다 — 실시간 호가 메시지와 같은 자리라
    화면이 조회 뒤와 실시간 뒤를 같은 모양으로 그립니다. 원문에 없거나 시각이
    아니면 `null` 입니다.

    `expectedPrice` 는 예상체결가입니다 — 단일가 시간(개장·마감 동시호가)에만
    값이 있고 접속매매 중에는 `0` 으로 오므로, 0 과 빈 값은 `null` 입니다.

    `asked` 는 부를 때 쓴 종목코드입니다. 원문에 종목 칸이 비어 올 때 쓰므로
    부르는 쪽이 넘겨 주는 편이 좋습니다.
    """
    rows = raw.get("Occurs1")
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    return {
        "code": _code(raw, "sym", asked),
        "time": market_values.clock(raw.get("qt_tm")),
        "expectedPrice": _number(raw.get("est_exec_pric")) or None,
        "asks": market_values.asks(
            (row.get("occurs1_ask_best_pric"), row.get("occurs1_ask_qty")) for row in rows
        ),
        "bids": market_values.bids(
            (row.get("occurs1_bid_best_pric"), row.get("occurs1_bid_qty")) for row in rows
        ),
    }


def bars(raw: dict[str, Any], *, minute: bool, asked: str = "") -> dict[str, Any]:
    """차트 응답을 봉 목록으로. **두 시장이 같은 자리를 씁니다.**

    규칙과 까닭은 `backend/quotes.py` 에 있습니다 — 해외 차트도 칸 이름이
    같아서, 두 벌로 두면 한쪽만 고쳐지는 날 차트가 시장에 따라 달라집니다.
    """
    return quotes.bars(raw, minute=minute, asked=asked)


def drop_placeholders(shaped: dict[str, Any]) -> dict[str, Any]:
    """**야간 일봉**에서 체결이 없던 날의 자리표 봉을 뺍니다. 뺀 수는 `skipped` 에 더합니다.

    야간 일봉은 휴장 전날처럼 야간 거래가 없던 날에도 봉 하나를 보냅니다 —
    시가·고가·저가·종가가 모두 같은 값(대개 주간 종가)이고 거래량이 0 입니다.
    그대로 그리면 거래가 있었던 것처럼 가격 막대가 섭니다.

    세 조건(거래량 0, 네 가격이 모두 같음, 야간 일봉)을 **모두** 만족할 때만
    뺍니다. 주간 일봉에서는 거래가 적은 월물의 거래 0 인 날이 정상일 수 있어
    부르는 쪽이 야간 일봉에만 씁니다.
    """
    kept: list[dict[str, Any]] = []
    dropped = 0
    for bar in shaped.get("bars") or []:
        prices = {bar.get("open"), bar.get("high"), bar.get("low"), bar.get("close")}
        if bar.get("volume") == 0 and len(prices) == 1:
            dropped += 1
            continue
        kept.append(bar)
    if not dropped:
        return shaped
    return {**shaped, "bars": kept, "skipped": int(shaped.get("skipped") or 0) + dropped}
