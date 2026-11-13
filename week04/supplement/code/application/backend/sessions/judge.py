"""지금이 어느 세션인지. **순수 함수**입니다 — 시각을 받아서 답합니다.

시계를 안에서 읽지 않는 이유는 둘입니다. 시험이 어느 시각이든 넣어 볼 수 있어야
하고, 허브·라우트·화면이 **같은 순간**을 물었을 때 같은 답이 나와야 합니다.

## 국내

세션은 주간·야간 둘이고 시간은 품목과 무관하게 세션마다 같습니다(시간표 응답이
그렇습니다). 품목마다 다른 것은 둘뿐입니다.

* 야간이 있는가 — 마스터에 야간 줄이 있는 품목(열 개 남짓)만 야간이 섭니다.
* 최종거래일 — 그날은 주간 거래가 일찍 끝납니다(지수 15:20 · 통화·국채 11:30).
  개별주식 선물옵션은 시간표에 없어 지수와 같은 15:20 으로 봅니다. 그 마감 뒤에는
  **이 종목의 세션이 더 없습니다** — 그날 야간도 다음 날도
  없으므로 `nextOpen` 이 비고 `lastTradingDay` 가 그 까닭을 말합니다. 화면은 그것으로
  「다음 개장」 대신 「최종거래일 마감」을 세웁니다.

상태 낱말은 `closed`·`pre-open`·`day`·`closing-auction`·`pre-night`·`night` 이고,
`session` 은 그 상태가 어느 세션에 속하는지(`day`·`night`·없음)라 화면이 실시간
맥락을 고를 때 씁니다.

## 해외

품목마다 1차·2차·익일 구간이 다르고 전부 한국시간입니다. 상태는 `open`·`open-2`·
`next-day`(각 구간 안), `break`(같은 날의 두 구간 사이), `closed` 입니다.

## 한계

**휴장일 달력이 없습니다.** 주말만 압니다. 휴장일에도 개장으로 답하고, 그날은
실시간이 안 오는 것으로 드러납니다. 응답의 `holidayAware` 가 늘 거짓인 이유입니다.
해외 주말 규칙은 거래소마다 다릅니다. 여기서는 평일에 시작하는 구간만 인정하고,
금요일에 시작한 구간이 토요일 아침까지 이어지는 것은 그대로 둡니다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from backend.overseas_futureoption.overseas_hours import exchange_of
from backend.sessions.hours import KST, TimeRange, from_dict, is_weekend, iso, time_range, to_kst

DOMESTIC = "domestic"
OVERSEAS = "overseas"

DAY = "day"
NIGHT = "night"

# 국내 상태
CLOSED = "closed"
PRE_OPEN = "pre-open"
CLOSING_AUCTION = "closing-auction"
PRE_NIGHT = "pre-night"

# 해외 상태
OPEN = "open"
OPEN_SECOND = "open-2"
NEXT_DAY = "next-day"
BREAK = "break"

# 시간표의 출처. `api` 는 오늘 받은 표, `table` 은 못 받아 내장 표, `unknown` 은 종목을 못 찾음.
FROM_API = "api"
FROM_TABLE = "table"
UNKNOWN = "unknown"

#: 국내 시간 구간을 **어느 층에서 읽었는지**. 판정은 그 종목의 품목 줄 → 시장 공통
#: 표 → 내장 표 차례로 내려옵니다.
#:
#: **`source` 로는 못 가립니다.** 앞의 두 층이 모두 오늘 받은 표라 `source` 는 둘 다
#: `api` 라고 말합니다. 품목 줄을 찾았어도 그 세션의 거래시간을 못 읽으면 공통 표로
#: 내려가므로, 품목 줄이 있는지로 짐작하면 틀립니다.
#:
#: 품목 줄은 보통 거래시간이 차서 오고, 비어 올 때만 아래 층으로 내려갑니다. 그
#: 길을 지났는지는 이 칸만 말할 수 있습니다.
LAYER_PRODUCT = "product"
LAYER_COMMON = "common"
LAYER_BUILTIN = "builtin"

#: 시간표를 못 받았을 때 쓰는 내장 표. NH선물 시간표 응답과 같은 값입니다.
DOMESTIC_FALLBACK: dict[str, dict[str, str]] = {
    DAY: {
        "trading": "08:45~15:45",
        "openingAuction": "08:30~08:45",
        "closingAuction": "15:35~15:45",
    },
    NIGHT: {
        "trading": "18:00~익일06:00",
        "openingAuction": "17:50~18:00",
        "closingAuction": "05:50~06:00",
    },
}

#: 최종거래일의 주간 거래 종료 시각, 상품 분류(`mrkt_code`)별. 시간표 응답에서 지수
#: 계열은 15:20, 통화·국채는 11:30 입니다. 표에 없는 분류는 15:20 으로 봅니다.
LAST_DAY_CLOSE: dict[str, time] = {
    "01": time(15, 20),  # 유가증권(주식) — 시간표에 없어 지수와 같이 봄
    "02": time(15, 20),  # 코스닥(주식) — 위와 같음
    "03": time(15, 20),  # 지수
    "04": time(15, 20),  # 코스닥 지수
    "05": time(11, 30),  # 국채
    "06": time(11, 30),  # 통화
}
DEFAULT_LAST_DAY_CLOSE = time(15, 20)

#: 앞뒤로 며칠까지 창을 만들어 보는지. 주말과 연휴를 건너 다음 개장을 찾을 만큼.
_LOOK_BACK = 2
_LOOK_AHEAD = 10


@dataclass(frozen=True)
class Window:
    """한 상태가 이어지는 시간 창."""

    state: str
    session: str | None
    start: datetime
    end: datetime
    #: 거래 창인지(단일가·휴식은 아님). `nextOpen` 은 거래 창의 시작만 봅니다.
    trading: bool
    #: 어느 날 시작한 세션에 속하는지. 해외의 `break` 판정에 씁니다.
    day: date


# ── 국내 ─────────────────────────────────────────────────


def judge_domestic(
    contract: dict[str, Any] | None,
    *,
    has_night: bool,
    table: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    """국내 종목 하나의 지금 세션.

    `contract` 는 마스터의 종목 줄(없으면 품목과 무관한 세션 시계로 답합니다 —
    허브가 경계를 알릴 때 그렇게 씁니다). `table` 은 받아 둔 시간표(`sessions` 와
    `products` 를 담은 것)이고 없으면 내장 표를 씁니다.

    **시간 구간은 그 종목의 품목 줄에서 옵니다.** 같은 세션에서도 개장이
    08:45 인 품목과 09:00 인 품목이 함께 옵니다 — 시장 하나의 표로 답하면 그중
    한쪽은 늘 틀립니다. 품목 줄이 없을 때만 공통 표로, 그것도 없으면 내장
    표로 갑니다.

    **어느 층에서 읽었는지는 `hoursLayer` 가 세션마다 말합니다**(`LAYER_PRODUCT`
    참고). `source` 로는 못 가립니다 — 앞의 두 층이 모두 오늘 받은 표라 둘 다
    `api` 입니다. 야간이 없는 품목은 야간 층이 `None` 입니다.
    """
    now = to_kst(now)
    sessions, source, layers = _domestic_sessions(table, contract, has_night=has_night)
    last_day_close = _last_day_close(contract, now.date())

    windows: list[Window] = []
    for offset in range(-_LOOK_BACK, _LOOK_AHEAD + 1):
        day = now.date() + timedelta(days=offset)
        if is_weekend(day):
            continue
        if last_day_close is not None and offset == 0:
            # 최종거래일. 이른 마감 뒤에는 이 종목의 세션이 더 없습니다 — 그날 야간도,
            # 다음 날도 없으므로 창을 여기서 끝내 `nextOpen` 이 비게 둡니다.
            windows.extend(
                _domestic_day_windows(sessions[DAY], day, early_close=last_day_close)
            )
            break
        windows.extend(_domestic_day_windows(sessions[DAY], day, early_close=None))
        if has_night:
            windows.extend(_domestic_night_windows(sessions[NIGHT], day))
    windows.sort(key=lambda w: w.start)

    made = _answer(windows, now)
    made["hasNight"] = has_night
    made["lastTradingDay"] = (
        {"closeAt": iso(datetime.combine(now.date(), last_day_close, KST))}
        if last_day_close is not None
        else None
    )
    made["source"] = source
    # **야간이 없는 품목은 야간 층이 빕니다.** 위에서 야간 창을 안 세웠으므로,
    # 층만 남겨 두면 「이 종목에 야간이 있다」로 읽힙니다.
    made["hoursLayer"] = {DAY: layers[DAY], NIGHT: layers[NIGHT] if has_night else None}
    return made


def domestic_edges(table: dict[str, Any] | None, now: datetime) -> str | None:
    """국내 시간표의 **다음 경계**. 품목마다 다른 경계를 통틀어 가장 이른 것입니다.

    허브의 세션 시계가 쓰는 자리입니다. 시장 공통 표는 품목 줄들의 최다값이라,
    그것 하나로 재면 **소수 쪽 품목의 경계에서는 시계가 울리지 않습니다** —
    개장이 08:45 인 품목과 09:00 인 품목이 아홉 대 아홉으로 오면 절반은 15분
    늦거나 이르게 바뀝니다.

    **품목 줄마다 판정을 돌립니다.** 창을 다시 셈하지 않고 `judge_domestic` 을
    그대로 쓰는 것은, 야간이 있는 품목인지처럼 경계를 정하는 규칙이 이미 그
    안에 있기 때문입니다. 줄이 하나도 없으면 품목과 무관한 판정의 경계를
    그대로 냅니다(그때는 공통 표나 내장 표에서 옵니다).

    **최종거래일의 이른 마감은 안 실립니다** — 그것은 품목이 아니라 종목마다
    다른 날짜라 여기서 가릴 수 없습니다. 화면은 이 경계를 「무언가 바뀌었다」는
    신호로만 쓰고 세션은 종목마다 다시 묻습니다(`/api/session`).

    품목 줄이 모두 같은 시각이면(주간 08:45~15:45, 야간 18:00~06:00) 이 함수의
    답은 공통 표의 경계와 같습니다.

    답은 `until` 과 같은 모양의 글자이고, 더 볼 경계가 없으면 `None` 입니다.
    """
    products = table.get("products") if isinstance(table, dict) else None
    rows = [one for one in products if isinstance(one, dict)] if isinstance(products, list) else []

    nights = {
        (str(one.get("productCode") or "").strip(), str(one.get("kind") or "").strip())
        for one in rows
        if one.get("session") == NIGHT
    }
    made: list[str] = []
    seen: set[tuple[str, str]] = set()
    for one in rows:
        # 품목 하나를 한 번만 봅니다. 주간 줄과 야간 줄이 따로 오므로 열쇠는
        # (품목코드, 갈래)이고, 야간 줄이 있는지는 위에서 미리 모아 둡니다.
        # 품목코드를 모르는 줄은 건너뜁니다 — 그 줄로는 판정을 돌릴 수 없습니다.
        key = (str(one.get("productCode") or "").strip(), str(one.get("kind") or "").strip())
        if not key[0] or key in seen:
            continue
        seen.add(key)
        got = judge_domestic(
            {"market": DOMESTIC, "trd_code": key[0], "kind": key[1]},
            has_night=key in nights,
            table=table,
            now=now,
        )
        if got.get("until"):
            made.append(str(got["until"]))
    if made:
        # 모두 한국시간이 붙은 같은 모양의 글자라, 글자끼리 견줘도 시각 순입니다.
        return min(made)
    # 줄이 없으면 품목과 무관한 판정 그대로입니다 — 공통 표나 내장 표의 경계.
    return judge_domestic(None, has_night=True, table=table, now=now).get("until")


def _domestic_sessions(
    table: dict[str, Any] | None,
    contract: dict[str, Any] | None = None,
    *,
    has_night: bool,
) -> tuple[dict[str, dict[str, TimeRange]], str, dict[str, str]]:
    """세션별 세 구간. **그 종목의 품목 줄 → 공통 표 → 내장 표** 차례입니다.

    품목 줄을 먼저 보는 것은 같은 세션 안에서도 품목마다 시간이 다르기
    때문입니다. 공통 표는 그 줄들의 최다값이라, 소수 쪽 품목에는 늘 틀립니다.

    **한 칸씩 내려옵니다.** 품목 줄을 찾았어도 거기서 거래시간을 못 읽으면 공통
    표를 한 번 더 봅니다 — 건너뛰면 오늘 받은 값을 두고 내장 표로 갑니다.

    **그 창의 줄이 없으면 다른 창의 줄로 답하지 않습니다** — 야간 줄이 없는
    품목에 주간 구간을 물리면 18시에 열려야 할 것이 08시 45분에 열립니다.

    **야간이 없는 품목은 주간만 셈합니다.** 야간 구간을 찾다 못 찾아 내장 표로
    내려가면, 주간을 오늘 받은 표에서 읽었는데도 답이 「시간표를 못 받았다」
    (`source` 가 `table`)가 됩니다. 시간표에 야간 구간이 하나도 없으면 그렇게
    됩니다. 야간이 없는 품목의 답에는 야간 구간이 없고, 판정도 야간 창을 세우지
    않으므로 쓰는 곳이 없습니다.
    """
    from backend.domestic_futureoption.domestic_hours import product_row

    products = table.get("products") if isinstance(table, dict) else None
    products = [one for one in products if isinstance(one, dict)] if isinstance(products, list) else []

    common = table.get("sessions", {}) if isinstance(table, dict) else {}

    made: dict[str, dict[str, TimeRange]] = {}
    layers: dict[str, str] = {}
    source = FROM_API
    for session in (DAY, NIGHT) if has_night else (DAY,):
        # **차례대로 봅니다.** 품목 줄이 있어도 거래시간을 못 읽으면 공통 표로
        # 한 칸 내려옵니다 — 그 자리에서 바로 내장 표로 가면 오늘 받은 값을
        # 두고 작년에 적어 둔 값을 쓰게 됩니다.
        ranges: dict[str, TimeRange | None] = {}
        layers[session] = LAYER_BUILTIN
        for layer, got in (
            (LAYER_PRODUCT, product_row(products, contract, session)),
            (LAYER_COMMON, common.get(session)),
        ):
            got = got if isinstance(got, dict) else {}
            ranges = {
                field: from_dict(got.get(field))
                for field in ("trading", "openingAuction", "closingAuction")
            }
            if ranges["trading"] is not None:
                layers[session] = layer
                break
        if ranges["trading"] is None:
            source = FROM_TABLE
            ranges = {
                field: time_range(text) for field, text in DOMESTIC_FALLBACK[session].items()
            }
        made[session] = {k: v for k, v in ranges.items() if v is not None}
    return made, source, layers


def _last_day_close(contract: dict[str, Any] | None, today: date) -> time | None:
    """오늘이 이 종목의 최종거래일이면 그날 주간 거래가 끝나는 시각."""
    if not contract:
        return None
    expiry = str(contract.get("expiry_date") or "").strip()
    if expiry != today.strftime("%Y%m%d"):
        return None
    group = str(contract.get("mrkt_code") or "").strip()
    return LAST_DAY_CLOSE.get(group, DEFAULT_LAST_DAY_CLOSE)


def _domestic_day_windows(
    ranges: dict[str, TimeRange], day: date, *, early_close: time | None
) -> list[Window]:
    trading = ranges["trading"]
    start, end = trading.starting(day)
    made: list[Window] = []
    opening = ranges.get("openingAuction")
    if opening is not None:
        o_start, o_end = opening.starting(day)
        made.append(Window(PRE_OPEN, DAY, o_start, min(o_end, start), False, day))
    if early_close is not None:
        # 최종거래일은 일찍 끝나고, 그날의 종가 단일가는 시간표가 말하지 않습니다.
        end = datetime.combine(day, early_close, KST)
        made.append(Window(DAY, DAY, start, end, True, day))
        return made
    closing = ranges.get("closingAuction")
    if closing is not None:
        c_start, _ = closing.starting(day)
        if start < c_start < end:
            made.append(Window(DAY, DAY, start, c_start, True, day))
            made.append(Window(CLOSING_AUCTION, DAY, c_start, end, False, day))
            return made
    made.append(Window(DAY, DAY, start, end, True, day))
    return made


def _domestic_night_windows(ranges: dict[str, TimeRange], day: date) -> list[Window]:
    trading = ranges["trading"]
    start, end = trading.starting(day)
    made: list[Window] = []
    opening = ranges.get("openingAuction")
    if opening is not None:
        o_start, o_end = opening.starting(day)
        made.append(Window(PRE_NIGHT, NIGHT, o_start, min(o_end, start), False, day))
    closing = ranges.get("closingAuction")
    if closing is not None:
        # 종가 단일가는 세션이 끝나는 날(다음 날 새벽)에 있습니다.
        c_start, _ = closing.starting(end.date())
        if start < c_start < end:
            made.append(Window(NIGHT, NIGHT, start, c_start, True, day))
            made.append(Window(CLOSING_AUCTION, NIGHT, c_start, end, False, day))
            return made
    made.append(Window(NIGHT, NIGHT, start, end, True, day))
    return made


# ── 해외 ─────────────────────────────────────────────────


def judge_overseas(
    contract: dict[str, Any] | None,
    *,
    table: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    """해외 종목 하나의 지금 세션. 시간표에 그 품목이 없으면 `unknown` 으로 마감입니다."""
    now = to_kst(now)
    row = _overseas_row(contract, table)
    if row is None:
        made = _answer([], now)
        made["hasNight"] = False
        made["lastTradingDay"] = None
        made["source"] = UNKNOWN
        return made

    made = _answer(_overseas_windows(row, now), now, breaks=True)
    made["hasNight"] = False
    made["lastTradingDay"] = None
    made["source"] = FROM_API
    return made


def overseas_trading_day(
    contract: dict[str, Any] | None,
    *,
    table: dict[str, Any] | None,
    at: datetime,
) -> date | None:
    """`at` 이 든 거래 창의 **거래일**. 창 밖(정비·쉼)이면 `None` 입니다.

    거래일은 그 창이 시작한 날입니다 — 한국 시각 자정을 넘기는 세션(07:00~익일
    06:00 등)도 한 거래일입니다. 시간표에 그 품목이 없으면 한국 시각의 달력
    날짜로 답합니다(시간표를 모르는 것과 창 밖은 다릅니다).
    """
    at = to_kst(at)
    row = _overseas_row(contract, table)
    if row is None:
        return at.date()
    for window in _overseas_windows(row, at):
        if window.start <= at < window.end:
            return window.day
    return None


def overseas_row(contract: dict[str, Any] | None, table: dict[str, Any] | None) -> dict[str, Any] | None:
    """그 종목의 장운영정보 품목 줄(통화·구간이 든 줄). 못 찾으면 `None` 입니다."""
    return _overseas_row(contract, table)


def overseas_day_end(
    contract: dict[str, Any] | None,
    *,
    table: dict[str, Any] | None,
    now: datetime,
) -> datetime | None:
    """`now` 가 든 거래 창이 속한 **거래일의 마지막 구간 끝**. 창 밖이면 `None` 입니다.

    구간이 여럿인 품목(홍콩 등)은 첫 구간이 끝나도 그 거래일이 끝난 것이 아닙니다.
    세션 끝 정리는 이 시각을 봅니다.
    """
    now = to_kst(now)
    row = _overseas_row(contract, table)
    if row is None:
        return None
    windows = _overseas_windows(row, now)
    current = next((one for one in windows if one.start <= now < one.end), None)
    if current is None:
        return None
    return max(one.end for one in windows if one.day == current.day)


def _overseas_windows(row: dict[str, Any], now: datetime) -> list[Window]:
    """그 품목 줄의 거래 창들. `now` 앞뒤 며칠을 시각 차례로 세웁니다."""
    first = from_dict(row.get("trading"))
    next_day = from_dict(row.get("nextDayTrading"))
    slots = [
        (OPEN, first),
        (OPEN_SECOND, from_dict(row.get("second"))),
        (NEXT_DAY, next_day),
    ]
    # 「익일」 구간이 **그 거래일의 다음 날 새벽**에 있는지. 이름만으로는 못 가립니다 —
    # 홍콩은 익일 구간이 그 날 저녁에 열려 자정을 넘기고(18:15~익일 04:00), 미국
    # 변동성 지수는 다음 날 새벽에 따로 섭니다(05:30~06:00). 그래서 시각으로 가릅니다:
    # 자정을 넘지 않고 1차보다 일찍 여는 익일 구간은 다음 날의 것입니다. 그래야
    # 금요일 거래일의 그 구간이 토요일 새벽에 서고, 월요일 새벽에는 서지 않습니다.
    next_morning = (
        next_day is not None
        and first is not None
        and not next_day.next_day
        and next_day.open < first.open
    )
    windows: list[Window] = []
    for offset in range(-_LOOK_BACK, _LOOK_AHEAD + 1):
        day = now.date() + timedelta(days=offset)
        if is_weekend(day):
            continue
        for state, found in slots:
            if found is None:
                continue
            start, end = found.starting(day)
            if state == NEXT_DAY and next_morning:
                start, end = start + timedelta(days=1), end + timedelta(days=1)
            # `day` 는 달력 날이 아니라 **거래일**입니다 — 다음 날로 옮긴 구간도 그
            # 거래일에 속해, 그 사이 틈이 같은 거래일의 쉼(`break`)으로 읽힙니다.
            windows.append(Window(state, None, start, end, True, day))
    windows.sort(key=lambda w: w.start)
    return windows


def _overseas_row(
    contract: dict[str, Any] | None, table: dict[str, Any] | None
) -> dict[str, Any] | None:
    if not contract or not isinstance(table, dict):
        return None
    wanted = (exchange_of(str(contract.get("exch") or "")), str(contract.get("comd") or "").strip())
    rows = [row for row in table.get("products") or [] if isinstance(row, dict)]
    for key in (wanted, _SAME_PRODUCT.get(wanted)):
        if key is None:
            continue
        for row in rows:
            if (row.get("exchange"), row.get("productCode")) == key:
                return row
    return None


#: 종목 마스터와 장운영정보가 **거래소를 다르게 적는** 품목. 확인된 짝만 둡니다 —
#: 마스터는 ICE 로 적는 이 품목을 장운영정보는 LIFFE 로 줍니다. 마스터의 거래소로
#: 찾는 것이 먼저이고, 없을 때만 이 짝으로 한 번 더 찾습니다.
_SAME_PRODUCT: dict[tuple[str, str], tuple[str, str]] = {
    ("ICE", "MEM"): ("LIFFE", "MEM"),
}


# ── 공통 ─────────────────────────────────────────────────


def _answer(windows: list[Window], now: datetime, *, breaks: bool = False) -> dict[str, Any]:
    """창 목록에서 지금 상태를 고릅니다. 어느 창에도 안 들면 마감입니다."""
    current = next((w for w in windows if w.start <= now < w.end), None)
    upcoming = [w for w in windows if w.start > now]
    next_open = next((w.start for w in upcoming if w.trading), None)

    if current is not None:
        return {
            "state": current.state,
            "session": current.session,
            "since": iso(current.start),
            "until": iso(current.end),
            "nextOpen": iso(next_open),
        }

    previous = next((w for w in reversed(windows) if w.end <= now), None)
    following = upcoming[0] if upcoming else None
    state = CLOSED
    if (
        breaks
        and previous is not None
        and following is not None
        and previous.day == following.day
    ):
        state = BREAK
    return {
        "state": state,
        "session": None,
        "since": iso(previous.end) if previous is not None else None,
        "until": iso(following.start) if following is not None else None,
        "nextOpen": iso(next_open),
    }
