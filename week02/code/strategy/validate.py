"""저장할 때 거르는 것.

**두 갈래를 가릅니다.**

    error    적을 수 없는 값. **담지 않습니다** — 배지만 달고 받으면 손절
             「abc」가 저장된 값이 됩니다.
    warning  담기는 담되, 그대로는 **돌지 않는** 설정. 조건을 아직 안 넣은
             규칙, 진입 규칙이 둘 다 꺼진 전략이 그렇습니다. 고치던 것을
             저장하지 못하면 사용자가 적어 둔 조건을 잃으므로 막지 않습니다.

「손절 없음」을 따로 두지 않습니다. 손절은 비워 둘 선택지가 아니라 **1틱 이상
적어야 하는 값**이라, 비어 있으면 그냥 `error` 입니다.

문구는 고객이 읽습니다. **화면이 이미 쓰는 말과 같게 적습니다** — 같은 자리를
두고 화면은 「1 이상의 정수로 적습니다」, 백엔드는 다른 말을 하면 사용자가 같은
것을 두 번 읽습니다(`frontend/src/screens/strategy/StrategySettings.tsx` 의
`numberWhy`). 칸 이름(`field`)은 화면이 그 자리로 데려다 놓으라고 함께 냅니다.

고를 것이 정해진 칸은 **목록을 여기 적어 둡니다.** 화면이 드롭다운으로 막고
있어도 저장 요청은 화면 밖에서도 올 수 있고, 목록에 없는 값이 담기면 엔진이
「5분봉」이 아닌 기준 봉으로 돌라는 말을 듣게 됩니다.
"""
from __future__ import annotations

from typing import Any

from backend import market_values
from backend.strategy import instrument, paper
from backend.strategy import rules as rule_lib
from backend.strategy.rules import (
    BOOK_PRICE_IDS,
    BOOK_QTY_IDS,
    CLOCK,
    CONTEXT_IDS,
    CROSS_LEFT_IDS,
    CROSS_LINE_IDS,
    CROSS_OPERATORS,
    INTEGER_IDS,
    OPERAND_IDS,
    OPERATOR_IDS,
    RULE_KEYS,
    STEP_JOINS,
    TIME_IDS,
    parse_clock,
)

#: 갈래 둘.
ERROR = "error"
WARNING = "warning"

#: 규칙 하나가 가질 수 있는 조건 수. 화면의 `MAX_CONDS` 와 같은 값입니다.
MAX_CONDS = 5

#: 진입 규칙 둘. 하나는 켜져 있어야 실행을 시작할 수 있습니다.
ENTRY_KEYS = ("buyEntry", "sellEntry")

#: 오른쪽 자리에만 서는 피연산자. 「이전 1봉의 고가」는 지금 봉과 견주라고 있는
#: 값이라, 왼쪽에 놓으면 무엇과 견주는지가 사라집니다.
RIGHT_ONLY_IDS = ("prevHigh", "prevLow")

#: 기준 봉. 화면의 `BASE_BARS` 와 같은 목록입니다.
BASE_BARS = ("1분봉", "5분봉", "15분봉", "30분봉", "60분봉", "일봉")

#: 주문유형. **시장마다 다릅니다** — 화면의 `ORDER_TYPES` 와 같은 목록이고,
#: 그 목록은 주문 화면이 쓰는 것과 같습니다.
ORDER_TYPES: dict[str, tuple[str, ...]] = {
    "domestic": ("지정가", "시장가", "조건부지정가", "최유리지정가"),
    "overseas": ("시장가", "지정가", "STOP-M", "STOP-L"),
}

#: 값 칸을 쓰지 않는 주문유형. 화면의 `needsPriceFor` 와 같은 규칙입니다 —
#: 시장가와 STOP-M 은 값을 안 받고 시장가로 나갑니다.
NO_PRICE_ORDER_TYPES = ("시장가", "STOP-M")

#: 주문 가격 기준과 스탑 가격 기준. 화면의 `PRICE_BASES` 하나를 둘이 함께 씁니다.
PRICE_BASES = ("신호가", "신호가 + 1틱", "신호가 − 1틱")

#: 국내 체결조건. 해외 주문에는 붙이지 않아 빈 값도 받습니다.
FILL_CONDS = ("FAS", "FOK", "FAK")

#: 시장가 주문이 받는 국내 체결조건. 국내 주문 명세(`rest_api_docs/domestic/domestic_order.md`,
#: `domestic_order-night.md`)는 체결조건 세 가지를 나열할 뿐 주문유형과의 조합은 적지
#: 않지만, 서버는 시장가 주문의 FOK·FAK 를 거부합니다. 해외 주문 명세
#: (`rest_api_docs/overseas/overseas_order.md`)에는 체결조건 칸이 없어 해외에는 이 규칙이
#: 없습니다. 화면의 `fillCondsFor` 와 같은 규칙입니다.
MARKET_ORDER_TYPE = "시장가"
MARKET_FILL_CONDS = ("FAS",)

#: 전략이 도는 세션. 화면의 `sessionsFor` 가 내는 이름이고, 홈의 장 시간 줄과
#: 같은 말입니다.
SESSIONS: dict[str, tuple[str, ...]] = {
    "domestic": ("국내 주간", "국내 야간"),
    "overseas": ("해외 CME",),
}

#: 평가 모드. 「마감」은 봉이 닫힐 때 한 번, 「틱」은 체결 시세마다 진행 중인 봉까지
#: 넣어 판정합니다. 그 밖의 값·빈 값·칸 없음은 「마감」으로 담습니다(`normalized`).
EVAL_MODE = "마감"
EVAL_MODES = (EVAL_MODE, "틱")

#: 해외 시장 이름. 해외 주문에는 체결조건을 붙이지 않습니다.
OVERSEAS = "overseas"

#: 목록 줄이 설정에서 받아 쓰는 값. `목록 이름 → 설정 이름` 입니다.
#:
#: **설정(`aux`)이 참입니다.** 같은 값을 목록 줄에도 적어 두면 둘이 갈리는 날
#: 어느 쪽이 그 전략의 한도인지 고를 수 없습니다. 화면이 목록 줄에 무엇을 적어
#: 보내든 설정 값으로 덮습니다.
FROM_AUX: dict[str, str] = {
    "marginBudget": "marginBudget",
    "orderQty": "perOrder",
    "dailyLoss": "dailyLoss",
}

# 화면이 쓰는 말 그대로.
_WHOLE_NUMBER = "1 이상의 정수로 적습니다"
_POSITIVE_NUMBER = "0보다 큰 수로 적습니다"
_STOP_TICKS = "손절은 1틱 이상으로 적습니다"
_HOLD_UNDER_ORDER = "최대 보유는 주문당 수량 이상으로 적습니다"
_NO_CONDS = "조건이 없어 발동하지 않습니다"
_EMPTY_STEP = "조건이 없는 단계가 있습니다 — 조건을 넣거나 단계를 지우세요"
_UNFILLED_COND = "안 채운 조건 줄이 있습니다"
_WITHIN_BARS = "다음 단계 유효기간은 1봉 이상으로 적습니다"
_JOIN = "단계 잇기는 「모두 충족」·「하나라도 충족」 가운데 하나입니다"
#: 보유 중에 **반대 방향 진입 규칙**이 참이 됐을 때 할 일(`aux.oppositeSignal`).
#:
#:     ignore   진입하지 않고 「안 낸 신호」로 남깁니다
#:     exit     보유 전량을 닫습니다. 반대 방향으로는 들어가지 않습니다
#:     reverse  보유 전량을 닫고, 다 닫히면 반대 방향으로 진입합니다
#:
#: 칸이 없거나 모르는 값이면 `ignore` 입니다 — 이 칸이 생기기 전에 담은 설정은
#: 동작이 그대로입니다.
OPPOSITE_SIGNAL_IGNORE = "ignore"
OPPOSITE_SIGNAL_EXIT = "exit"
OPPOSITE_SIGNAL_REVERSE = "reverse"
OPPOSITE_SIGNALS: tuple[str, ...] = (
    OPPOSITE_SIGNAL_IGNORE,
    OPPOSITE_SIGNAL_EXIT,
    OPPOSITE_SIGNAL_REVERSE,
)
_OPPOSITE_SIGNAL = "보유 중 반대 신호는 「무시」·「청산」·「반전」 가운데 하나입니다"


def opposite_signal(aux: Any) -> str:
    """설정의 「보유 중 반대 신호」. 칸이 없거나 모르는 값이면 `ignore` 입니다."""
    if not isinstance(aux, dict):
        return OPPOSITE_SIGNAL_IGNORE
    value = aux.get("oppositeSignal")
    value = value.strip() if isinstance(value, str) else ""
    return value if value in OPPOSITE_SIGNALS else OPPOSITE_SIGNAL_IGNORE


#: 켜고 끄는 칸. 칸이 없으면 끈 것이고, 적었다면 참·거짓이어야 합니다.
_SWITCHES: tuple[str, ...] = ("addWhileOpen", "sessionEndFlat")
_SWITCH = "켜기·끄기로만 적습니다"
_SAME_CROSS = "같은 값끼리는 교차할 수 없습니다"
_CROSS_SIDES = "교차는 가격이나 선이 선을 지날 때 씁니다"
_RIGHT_ONLY = "이전 1봉의 고가·저가는 오른쪽에만 놓습니다"
_TOO_MANY_CONDS = f"조건은 규칙마다 {MAX_CONDS}개까지입니다"
_CLOCK_FORMAT = "시각은 HH:MM(00:00~23:59)으로 적습니다"
_CLOCK_ONLY = "HH:MM 은 시각에만 씁니다"
_DAILY_TIME = "일봉에서는 시각·봉 순서를 쓸 수 없습니다"
_EQ_INTEGER = "같음은 정수 값(잔량·보유 수량·시각·봉 순서)에만 씁니다"
_EQ_WHOLE = "같음은 정수와 견줍니다"
_CONTEXT_OPERAND = "호가는 호가끼리만 견줍니다 — 시간·보유는 수와 견줍니다"
_PERIOD_RANGE = "기간은 2~200 사이 정수로 적습니다"
_MULTIPLIER_RANGE = "배수는 0.5~5 사이, 소수 한 자리까지 적습니다"
_FAST_SLOW = "단기는 장기보다 짧게 적습니다"
_SMOOTH_RANGE = "평활은 1~50 사이 정수로 적습니다"
_SHARED_PARAMS = "MACD·스토캐스틱끼리 견줄 때는 같은 설정이어야 합니다"

#: 매개변수마다 받는 범위 `(가장 작은 값, 가장 큰 값, 정수만인지)`.
_PARAM_RANGES: dict[str, tuple[float, float, bool]] = {
    "period": (2, 200, True),
    "fast": (2, 200, True),
    "slow": (2, 200, True),
    "signal": (2, 200, True),
    "multiplier": (0.5, 5, False),
    "k": (1, 50, True),
    "d": (1, 50, True),
}

#: 범위를 벗어났을 때 하는 말.
_PARAM_WHY: dict[str, str] = {
    "period": _PERIOD_RANGE,
    "fast": _PERIOD_RANGE,
    "slow": _PERIOD_RANGE,
    "signal": _PERIOD_RANGE,
    "multiplier": _MULTIPLIER_RANGE,
    "k": _SMOOTH_RANGE,
    "d": _SMOOTH_RANGE,
}

#: 「같음」 연산자. 정수 값에만 받습니다 — 가격을 같음으로 견주면 소수점 끝자리
#: 때문에 영영 안 걸립니다.
EQ = "eq"

#: 일봉. 하루가 한 봉이라 시각·봉 순서의 뜻이 없습니다.
DAILY_BAR = "일봉"

#: **자리가 둘이라 말도 둘입니다.** 저장할 때는 「이대로 두면 안 돈다」를
#: 알리는 것이라 전략 전체를 두고 말하고, 시작할 때는 못 하는 까닭을 짧게
#: 답합니다. 같은 문장을 두 자리에 쓰면 저장 배지가 「못 시작한다」로 읽힙니다.
_NO_ENTRY = "진입 규칙이 없어 이 전략은 주문을 내지 않습니다"
NO_ENTRY_START = "시작할 수 없습니다 — 진입 규칙이 없어 이 전략은 주문을 내지 않습니다"

#: 켜 두었지만 조건이 없어 영영 발동하지 않는 규칙뿐일 때. 저장은 경고로 받아
#: 주지만(적던 것을 잃지 않게) **시작은 못 합니다** — 「감시 중」으로 서서
#: 아무 일도 하지 않고, 왜 그런지 알려 주는 자리가 없습니다.
NO_CONDS_START = "시작할 수 없습니다 — 조건이 없어 발동하지 않습니다"

# 고를 것이 정해진 칸이 목록 밖일 때.
_BASE_BAR = "기준 봉을 고르세요"
_ORDER_TYPE = "주문유형을 고르세요"
_PRICE_BASIS = "주문 가격 기준을 고르세요"
_BUY_PRICE_BASIS = "매수 주문 가격 기준을 고르세요"
_SELL_PRICE_BASIS = "매도 주문 가격 기준을 고르세요"
_STOP_BASIS = "스탑 가격 기준을 고르세요"
#: 체결조건 칸을 고쳐야 할 때 하는 말. 계좌 실행을 시작할 때도 같은 말을 씁니다.
FILL_COND_WHY = "체결조건을 고르세요"
_FILL_COND = FILL_COND_WHY
#: 시장가에 FAS 밖의 체결조건을 골랐을 때. 화면의 같은 칸도 이 말을 씁니다.
MARKET_FILL_COND_WHY = "시장가는 체결조건을 FAS 로 두어야 합니다"
_SESSION = "세션을 고르세요"

#: 수로 적어야 하는 칸. `(이름, 정수인지, 문구)` — 화면의 `auxProblems` 와 같은 묶음입니다.
#: 손절·익절은 단위에 따라 받는 수가 달라 따로 봅니다(`_EXIT_DISTANCES`).
_NUMBERS: tuple[tuple[str, bool, str], ...] = (
    ("perOrder", True, _WHOLE_NUMBER),
    ("maxHold", True, _WHOLE_NUMBER),
    ("dailyLoss", False, _POSITIVE_NUMBER),
    ("marginBudget", False, _POSITIVE_NUMBER),
)

#: 비워 둘 수 있는 정수 칸. 비어 있으면 그 기능을 쓰지 않는 것이고, 적었다면
#: 1 이상의 정수여야 합니다. 비워 둘 수 있는 거리 칸(익절·추적 청산)은 `_EXIT_DISTANCES` 에.
#:
#:     lossStreak   연속 손실 한도 — 손실 거래가 이만큼 이어지면 새 진입을 막습니다
_OPTIONAL_WHOLE: tuple[str, ...] = ("lossStreak",)

#: 손절·익절·추적 청산의 거리 칸. `(값 칸, 단위 칸, 꼭 적어야 하는지, 틱일 때의 말)`.
#:
#: **값 칸 이름은 옛 것이고 뜻은 단위가 정합니다**(`stopTicks` 에 % 를 적을 수
#: 있습니다). 단위 칸이 비면 틱이라 옛 전략은 그대로 읽힙니다.
#:
#: 손절만 꼭 적어야 합니다. 익절·추적 청산은 비워 두면 그 청산을 쓰지 않는 것이고,
#: 적었다면 그 단위의 범위를 지켜야 합니다.
_EXIT_DISTANCES: tuple[tuple[str, str, bool, str], ...] = (
    ("stopTicks", "stopUnit", True, _STOP_TICKS),
    ("takeTicks", "takeUnit", False, _WHOLE_NUMBER),
    ("trailTicks", "trailUnit", False, _WHOLE_NUMBER),
)

#: 거리 단위. 엔진의 `engine.UNITS` 와 같은 값입니다.
EXIT_UNITS: tuple[str, ...] = ("tick", "price", "pct", "atr")

_UNIT = "단위를 고르세요"
_TICK_RANGE = "틱은 1~1000 사이 정수로 적습니다"
_PCT_RANGE = "%는 0.1~50 사이, 소수 한 자리까지 적습니다"
_ATR_RANGE = "ATR 배수는 0.5~10 사이, 소수 한 자리까지 적습니다"
_SESSION_END_MINUTES = "세션 끝 정리는 1~60분 사이 정수로 적습니다"
_WINDOW_SAME = "신규 진입 시간의 시작과 끝이 같습니다"
_WAIT_RANGE = "청산 뒤 대기는 0~50 사이 정수로 적습니다"
_MAX_ENTRIES = "하루 최대 진입은 1~20 사이 정수로 적습니다"

#: 방향별 주문 가격 기준. `(칸 이름, 문구)` — 비어 있으면 공용 칸(`priceBasis`)을 씁니다.
_SIDE_PRICE_BASES: tuple[tuple[str, str], ...] = (
    ("buyPriceBasis", _BUY_PRICE_BASIS),
    ("sellPriceBasis", _SELL_PRICE_BASIS),
)

#: 규칙 전체의 시한이 쓰이지 않는 규칙. 막을 값은 아니라 경고입니다.
_RESET_ONE_STEP = "단계가 하나뿐이라 처음으로 되돌리기가 쓰이지 않습니다"


def unique(found: list[dict[str, str]]) -> list[dict[str, str]]:
    """같은 칸·같은 말이 두 번 든 줄을 하나로. 처음 나온 차례를 지킵니다.

    시작 검사와 지난 봉 셈은 담을 때의 오류와 「돌 수 있는지」를 함께 모읍니다. 두
    자리가 같은 까닭을 같은 말로 짚으면 화면에 같은 줄이 두 번 서지 않게 합니다.
    """
    seen: set[tuple[str, str]] = set()
    made: list[dict[str, str]] = []
    for one in found:
        key = (str(one.get("field") or ""), str(one.get("message") or ""))
        if key in seen:
            continue
        seen.add(key)
        made.append(one)
    return made


def problems(draft: dict[str, Any], *, market: str = "") -> list[dict[str, str]]:
    """그 설정의 문제들. `severity` 가 `error` 인 것이 있으면 담지 않습니다.

    `market` 은 주문유형·세션처럼 **시장마다 목록이 다른 칸**을 보는 데 씁니다.
    모르면 그 둘을 건너뜁니다 — 어느 목록에 대고 재야 할지 고를 수 없는데
    아무 목록에나 대면 맞는 값을 틀렸다고 합니다.
    """
    found: list[dict[str, str]] = []
    rules = draft.get("rules") if isinstance(draft.get("rules"), dict) else {}
    aux = draft.get("aux") if isinstance(draft.get("aux"), dict) else {}

    _check_bar(draft, found)
    _check_numbers(aux, found)
    _check_choices(aux, market, found)
    daily = str(draft.get("baseBar") or "").strip() == DAILY_BAR
    for key in RULE_KEYS:
        _check_rule(key, rules.get(key), found, daily=daily)
    _check_entries(rules, found)
    return found


def blocked(found: list[dict[str, str]]) -> bool:
    """이 목록 때문에 담지 못하는지."""
    return any(one.get("severity") == ERROR for one in found)


def normalized(version: dict[str, Any], draft: dict[str, Any] | None) -> dict[str, Any] | None:
    """담기 전에 다듬습니다. **알리지 않고 조용히 맞추는 자리입니다.**

    평가 모드는 「마감」·「틱」 둘이고, 그 밖의 값이면 마감으로 읽습니다.
    체결조건은 국내 주문에만 있는 칸이라 해외 버전에서는 비웁니다 — 화면이
    세우지 않는 칸이 남아 있으면 주문을 낼 때 국내 규격이 해외로 따라갑니다.
    「보유 중 반대 신호」는 앞뒤 빈칸을 덜어 냅니다. 모르는 값은 담기 검사가
    막습니다(`_check_choices`).
    """
    if not isinstance(draft, dict):
        return draft
    made = dict(draft)
    if str(made.get("evalMode") or "") not in EVAL_MODES:
        made["evalMode"] = EVAL_MODE
    aux = made.get("aux")
    market = str(version.get("market") or "")
    if isinstance(aux, dict) and market == OVERSEAS and aux.get("fillCond"):
        aux = made["aux"] = {**aux, "fillCond": ""}
    opposite = aux.get("oppositeSignal") if isinstance(aux, dict) else None
    if isinstance(opposite, str) and opposite != opposite.strip():
        made["aux"] = {**aux, "oppositeSignal": opposite.strip()}
    return made


def aligned(version: dict[str, Any], draft: dict[str, Any] | None) -> dict[str, Any]:
    """목록 줄의 한도 셋을 **설정 값으로 채운 줄.** 까닭은 `FROM_AUX` 에.

    설정을 함께 안 보낸 저장(목록에만 있는 줄)은 그대로 둡니다 — 채울 값이
    없는데 비우면 이미 적혀 있던 한도가 사라집니다.
    """
    aux = (draft or {}).get("aux")
    if not isinstance(aux, dict):
        return dict(version)
    made = dict(version)
    for name, field in FROM_AUX.items():
        number = _number(aux.get(field))
        made[name] = None if number is None else _whole(number)
    return made


def _whole(number: float) -> float | int:
    """딱 떨어지면 정수로. 수량이 `1.0` 으로 적히면 화면이 그대로 그립니다."""
    return int(number) if number == int(number) else number


def _add(found: list[dict[str, str]], field: str, severity: str, message: str) -> None:
    found.append({"field": field, "severity": severity, "message": message})


def _check_bar(draft: dict[str, Any], found: list[dict[str, str]]) -> None:
    if str(draft.get("baseBar") or "").strip() not in BASE_BARS:
        _add(found, "baseBar", ERROR, _BASE_BAR)


def _check_numbers(aux: dict[str, Any], found: list[dict[str, str]]) -> None:
    for name, whole, why in _NUMBERS:
        if not _ok(aux.get(name), whole):
            _add(found, f"aux.{name}", ERROR, why)
    for name in _OPTIONAL_WHOLE:
        if not _blank(aux.get(name)) and not _ok(aux.get(name), whole=True):
            _add(found, f"aux.{name}", ERROR, _WHOLE_NUMBER)
    _check_exits(aux, found)
    _check_filters(aux, found)

    # 주문당 수량보다 적게 들고 있을 수는 없습니다 — 첫 주문부터 한도를 넘습니다.
    hold = _number(aux.get("maxHold"))
    per_order = _number(aux.get("perOrder"))
    if hold is not None and per_order is not None and 0 < hold < per_order:
        _add(found, "aux.maxHold", ERROR, _HOLD_UNDER_ORDER)


def _check_exits(aux: dict[str, Any], found: list[dict[str, str]]) -> None:
    """손절·익절·추적 청산의 거리. 단위마다 받는 수가 다릅니다."""
    for value_key, unit_key, required, tick_why in _EXIT_DISTANCES:
        unit = str(aux.get(unit_key) or "").strip() or "tick"
        if unit not in EXIT_UNITS:
            _add(found, f"aux.{unit_key}", ERROR, _UNIT)
            continue
        value = aux.get(value_key)
        if _blank(value) and not required:
            continue
        number = _number(value)
        field = f"aux.{value_key}"
        if unit == "tick":
            if not _ok(value, whole=True):
                _add(found, field, ERROR, tick_why)
            elif number is not None and number > 1000:
                _add(found, field, ERROR, _TICK_RANGE)
        elif unit == "price":
            if not _ok(value, whole=False):
                _add(found, field, ERROR, _POSITIVE_NUMBER)
        elif not _in_range(number, *((0.1, 50) if unit == "pct" else (0.5, 10)), places=1):
            _add(found, field, ERROR, _PCT_RANGE if unit == "pct" else _ATR_RANGE)


def _check_filters(aux: dict[str, Any], found: list[dict[str, str]]) -> None:
    """세션 끝 정리·신규 진입 시간·청산 뒤 대기·하루 최대 진입. 모두 비워 둘 수 있습니다.

    신규 진입 시간이 **그 세션 안**인지는 품목 시간표를 알아야 해서 실행을 세울 때
    봅니다(`service.start_problems`). 여기서는 모양만 봅니다.
    """
    for name in _SWITCHES:
        if not _switch(aux.get(name)):
            _add(found, f"aux.{name}", ERROR, _SWITCH)
    minutes = aux.get("sessionEndMinutes")
    if not _blank(minutes) and not _whole_in(minutes, 1, 60):
        _add(found, "aux.sessionEndMinutes", ERROR, _SESSION_END_MINUTES)
    elif aux.get("sessionEndFlat") is True and _blank(minutes):
        # 켜짐이 참·거짓이 아니면 위에서 그 칸을 짚었습니다. 분 칸까지 짚지 않습니다.
        _add(found, "aux.sessionEndMinutes", ERROR, _SESSION_END_MINUTES)

    clocks = []
    for name in ("entryStart", "entryEnd"):
        text = aux.get(name)
        if _blank(text):
            continue
        parsed = parse_clock(text)
        if parsed is None:
            _add(found, f"aux.{name}", ERROR, _CLOCK_FORMAT)
        else:
            clocks.append(parsed)
    if len(clocks) == 2 and clocks[0] == clocks[1]:
        _add(found, "aux.entryEnd", ERROR, _WINDOW_SAME)

    wait = aux.get("reentryWaitBars")
    if not _blank(wait) and not _whole_in(wait, 0, 50):
        _add(found, "aux.reentryWaitBars", ERROR, _WAIT_RANGE)
    most = aux.get("maxEntriesPerDay")
    if not _blank(most) and not _whole_in(most, 1, 20):
        _add(found, "aux.maxEntriesPerDay", ERROR, _MAX_ENTRIES)


def _in_range(number: float | None, low: float, high: float, *, places: int) -> bool:
    """`low`~`high` 안이고 소수 `places` 자리까지인지."""
    return number is not None and low <= number <= high and round(number, places) == number


def _whole_in(value: Any, low: int, high: int) -> bool:
    """`low`~`high` 사이의 정수인지."""
    number = _number(value)
    return number is not None and number == int(number) and low <= number <= high


def _check_choices(aux: dict[str, Any], market: str, found: list[dict[str, str]]) -> None:
    """고를 것이 정해진 칸들. 비워 둘 수 있는 칸은 적었을 때만 봅니다."""
    order_type = str(aux.get("orderType") or "").strip()
    if market in ORDER_TYPES and order_type not in ORDER_TYPES[market]:
        _add(found, "aux.orderType", ERROR, _ORDER_TYPE)
    elif order_type and not paper.supported(order_type):
        # 실행이 다루지 않는 유형입니다. 담을 때 막아, 시작해 보고서야 알게 되지 않게
        # 합니다. 예전에 담아 둔 것은 그대로 열립니다 — 읽기는 이 검사를 지나지 않습니다.
        _add(found, "aux.orderType", ERROR, paper.unsupported_why(order_type))
    if market in SESSIONS and str(aux.get("session") or "").strip() not in SESSIONS[market]:
        _add(found, "aux.session", ERROR, _SESSION)
    # 비워 두면 「무시」입니다. 적었다면 셋 가운데 하나여야 합니다.
    opposite = aux.get("oppositeSignal")
    if not _blank(opposite) and (
        not isinstance(opposite, str) or opposite.strip() not in OPPOSITE_SIGNALS
    ):
        _add(found, "aux.oppositeSignal", ERROR, _OPPOSITE_SIGNAL)

    # 값을 받는 주문유형이면 기준이 있어야 값을 만들 수 있습니다. 값을 안 받는
    # 주문유형이면 비워 둬도 되지만, 적었다면 아는 값이어야 합니다.
    #
    # 기준은 **주문 방향마다** 정해집니다 — 방향 칸, 비었으면 공용 칸. 그래서
    # 방향 칸을 둘 다 적었으면 공용 칸은 비어도 되고, 하나만 적었으면 빈 쪽은
    # 공용 칸으로 값을 만들어야 합니다.
    price_basis = str(aux.get("priceBasis") or "").strip()
    needs_price = order_type not in NO_PRICE_ORDER_TYPES
    sides = [
        (name, why, str(aux.get(name) or "").strip()) for name, why in _SIDE_PRICE_BASES
    ]
    if price_basis and price_basis not in PRICE_BASES:
        _add(found, "aux.priceBasis", ERROR, _PRICE_BASIS)
    for name, why, own in sides:
        if own and own not in PRICE_BASES:
            _add(found, f"aux.{name}", ERROR, why)
    if needs_price:
        if not any(own for _, _, own in sides):
            if not price_basis:
                _add(found, "aux.priceBasis", ERROR, _PRICE_BASIS)
        elif not price_basis:
            # 공용 칸이 모르는 값이면 위에서 그 칸 하나에 이미 달았습니다 — 원인이
            # 하나인데 빈 방향 칸에 또 달면 같은 것을 두 번 고치라는 말이 됩니다.
            for name, why, own in sides:
                if not own:
                    _add(found, f"aux.{name}", ERROR, why)

    # 스탑 가격 기준은 STOP 주문에만 뜻이 있어 화면이 그때만 세웁니다.
    stop_basis = str(aux.get("stopBasis") or "").strip()
    if stop_basis and stop_basis not in PRICE_BASES:
        _add(found, "aux.stopBasis", ERROR, _STOP_BASIS)

    # 체결조건은 국내에만 있습니다. 해외 버전은 비어 온 것이 맞습니다.
    fill_cond = str(aux.get("fillCond") or "").strip()
    if fill_cond and fill_cond not in FILL_CONDS:
        _add(found, "aux.fillCond", ERROR, _FILL_COND)
    elif (
        fill_cond
        and market != OVERSEAS
        and order_type == MARKET_ORDER_TYPE
        and fill_cond not in MARKET_FILL_CONDS
    ):
        # 담아 두면 진입 신호 때 서버가 주문을 거부합니다. 시작 검사도 이 검사를
        # 지나므로 예전에 담아 둔 설정은 시작할 때 막힙니다.
        _add(found, "aux.fillCond", ERROR, MARKET_FILL_COND_WHY)


def _check_entries(rules: dict[str, Any], found: list[dict[str, str]]) -> None:
    """진입 규칙이 둘 다 꺼져 있는지.

    칸 이름은 **규칙 전체**입니다. 매수·매도 어느 쪽을 켜도 풀리는 일이라
    한쪽 탭에 달면 「이 탭을 고치라」로 읽힙니다.
    """
    on = [key for key in ENTRY_KEYS if isinstance(rules.get(key), dict) and rules[key].get("on")]
    if not on:
        _add(found, "rules", WARNING, _NO_ENTRY)


def _check_rule(key: str, rule: Any, found: list[dict[str, str]], *, daily: bool = False) -> None:
    if not isinstance(rule, dict) or not rule.get("on"):
        return
    steps = [one for one in (rule.get("steps") or []) if isinstance(one, dict)]
    counted = sum(len(one.get("conds") or []) for one in steps)

    if counted == 0:
        # 켜 두기만 하고 아직 안 적은 상태입니다. 고치던 것을 저장하지 못하면
        # 적어 둔 다른 규칙까지 잃으므로 담되 돌지 않는다고 알립니다.
        _add(found, f"rules.{key}", WARNING, _NO_CONDS)
        return

    if counted > MAX_CONDS:
        _add(found, f"rules.{key}", ERROR, _TOO_MANY_CONDS)

    for position, step in enumerate(steps):
        _check_step(key, position, step, len(steps), found, daily=daily)

    # 규칙 전체의 시한. 비어 있으면 없는 것입니다. 단계가 하나면 1단계가 채워지는
    # 봉에서 바로 발사하므로 되돌릴 자리가 없습니다 — 해가 없어 담기는 받습니다.
    reset = rule.get("resetBars")
    if not _blank(reset):
        if not _ok(reset, whole=True):
            _add(found, f"rules.{key}.resetBars", ERROR, _WHOLE_NUMBER)
        elif len(steps) < 2:
            _add(found, f"rules.{key}.resetBars", WARNING, _RESET_ONE_STEP)


def _switch(value: Any) -> bool:
    """켜고 끄는 칸의 값으로 받을 수 있는지. 칸이 없으면(옛 저장본) 끈 것입니다."""
    return value is None or isinstance(value, bool)


def _check_step(
    key: str,
    position: int,
    step: dict[str, Any],
    total: int,
    found: list[dict[str, str]],
    *,
    daily: bool = False,
) -> None:
    field = f"rules.{key}.steps.{position}"
    join = step.get("join")
    if join not in (None, "") and join not in STEP_JOINS:
        _add(found, f"{field}.join", ERROR, _JOIN)
    # 마지막 단계의 「몇 봉 안에」는 뜻이 없어 화면도 그리지 않습니다.
    if position < total - 1 and not _ok(step.get("withinBars"), whole=True):
        _add(found, f"{field}.withinBars", ERROR, _WITHIN_BARS)

    conds = step.get("conds") or []
    if not conds:
        # 다른 단계에는 조건이 있습니다. 빈 관문은 통과하지 못해 뒤 단계가
        # 영영 안 열리므로, 규칙 전체가 빈 것과 달리 담지 않습니다.
        _add(found, field, ERROR, _EMPTY_STEP)
        return

    for at, cond in enumerate(conds):
        _check_cond(f"{field}.conds.{at}", cond, found, daily=daily)


def _check_cond(
    field: str, cond: Any, found: list[dict[str, str]], *, daily: bool = False
) -> None:
    if not isinstance(cond, dict) or not _filled(cond):
        _add(found, field, ERROR, _UNFILLED_COND)
        return

    left = str(cond.get("left") or "")
    right = str(cond.get("right") or "").strip()
    operand_right = str(cond.get("rightKind") or "") != "number"
    op = str(cond.get("op"))

    if left in RIGHT_ONLY_IDS:
        _add(found, field, ERROR, _RIGHT_ONLY)
        return
    if _check_context(field, left, right, op, operand_right, found, daily=daily):
        return
    sides = ["left", "right"] if operand_right else ["left"]
    if _check_params(field, cond, sides, found):
        return

    if op not in CROSS_OPERATORS:
        return
    # 같은 값끼리는 교차가 성립하지 않습니다 — 늘 거짓이라 규칙이 죽습니다. 같은
    # 지표라도 매개변수가 다르면(EMA(5)·EMA(20)) 다른 값입니다.
    if operand_right and rule_lib.side_key(cond, "left") == rule_lib.side_key(cond, "right"):
        _add(found, field, ERROR, _SAME_CROSS)
    elif not operand_right or not _cross_pair(cond):
        _add(found, field, ERROR, _CROSS_SIDES)


def _cross_pair(cond: dict[str, Any]) -> bool:
    """교차가 서는 짝인지. 가격 축이면 가격·선이 선을 지나고, 아니면 같은 지표의 선끼리입니다."""
    left = str(cond.get("left") or "")
    right = str(cond.get("right") or "")
    if left in CROSS_LEFT_IDS and right in CROSS_LINE_IDS:
        return True
    names = {rule_lib.resolve(left)[0], rule_lib.resolve(right)[0]}
    return any(names <= group for group in rule_lib.OSCILLATOR_CROSSES)


def _check_params(
    field: str, cond: dict[str, Any], sides: list[str], found: list[dict[str, str]]
) -> bool:
    """매개변수가 범위 안인지, 묶음을 함께 쓰는 지표끼리 같은 설정인지. 문제를 달았으면 참입니다.

    안 적은 매개변수는 기본값으로 읽으므로 문제가 아닙니다. 매개변수가 생기기 전의
    이름(`ema20` 등)으로 적은 조건은 매개변수 칸이 없어 여기서 볼 것이 없습니다.
    """
    flagged = False
    for side in sides:
        given = cond.get(f"{side}Params")
        name = rule_lib.resolve(cond.get(side), given)[0]
        spec = rule_lib.PARAM_SPECS.get(name)
        if not spec or not isinstance(given, dict):
            continue
        for key, _ in spec:
            if key not in given:
                continue
            low, high, whole = _PARAM_RANGES[key]
            number = _number(given.get(key))
            bad = number is None or not low <= number <= high
            if not bad and whole:
                bad = number != int(number)
            if not bad and not whole:
                bad = round(number, 1) != number
            if bad:
                _add(found, f"{field}.{side}Params.{key}", ERROR, _PARAM_WHY[key])
                flagged = True
        if name in rule_lib.MACD_IDS and not flagged:
            params = rule_lib.resolve(name, given)[1]
            if params["fast"] >= params["slow"]:
                _add(found, f"{field}.{side}Params.fast", ERROR, _FAST_SLOW)
                flagged = True
    if not flagged and len(sides) == 2:
        left = rule_lib.resolve(cond.get("left"), cond.get("leftParams"))
        right = rule_lib.resolve(cond.get("right"), cond.get("rightParams"))
        for group in (rule_lib.MACD_IDS, rule_lib.STOCH_IDS):
            if left[0] in group and right[0] in group and left[1] != right[1]:
                _add(found, f"{field}.rightParams", ERROR, _SHARED_PARAMS)
                flagged = True
    return flagged


def _check_context(
    field: str,
    left: str,
    right: str,
    op: str,
    operand_right: bool,
    found: list[dict[str, str]],
    *,
    daily: bool,
) -> bool:
    """호가·시간·포지션 값과 「같음」의 규칙. 문제를 달았으면 참입니다.

    교차는 여기서 보지 않습니다 — 새 값은 교차의 양쪽 목록에 없어 교차 검사가
    막습니다.
    """
    if not operand_right:
        if left == CLOCK and parse_clock(right) is None:
            _add(found, field, ERROR, _CLOCK_FORMAT)
            return True
        if left != CLOCK and ":" in right:
            _add(found, field, ERROR, _CLOCK_ONLY)
            return True

    if daily and (left in TIME_IDS or (operand_right and right in TIME_IDS)):
        _add(found, f"{field}.left", ERROR, _DAILY_TIME)
        return True

    if (
        operand_right
        and op not in CROSS_OPERATORS
        and (left in CONTEXT_IDS or right in CONTEXT_IDS)
        and not _same_book(left, right)
    ):
        _add(found, field, ERROR, _CONTEXT_OPERAND)
        return True

    if op == EQ:
        if left not in INTEGER_IDS or (operand_right and right not in INTEGER_IDS):
            _add(found, field, ERROR, _EQ_INTEGER)
            return True
        if not operand_right and left != CLOCK:
            number = _number(right)
            if number is None or number != int(number):
                _add(found, field, ERROR, _EQ_WHOLE)
                return True
    return False


def _same_book(left: str, right: str) -> bool:
    """호가끼리인지 — 가격은 가격끼리, 잔량은 잔량끼리."""
    return (left in BOOK_PRICE_IDS and right in BOOK_PRICE_IDS) or (
        left in BOOK_QTY_IDS and right in BOOK_QTY_IDS
    )


def _filled(cond: dict[str, Any]) -> bool:
    """조건 한 줄을 다 골랐는지. 화면의 `condProblems` 와 같은 판정입니다."""
    left = str(cond.get("left") or "")
    if left not in OPERAND_IDS:
        return False
    if str(cond.get("op") or "") not in OPERATOR_IDS:
        return False
    if str(cond.get("rightKind") or "") == "number":
        right = str(cond.get("right") if cond.get("right") is not None else "").strip()
        # 시각은 「HH:MM」 이라 수로 읽히지 않습니다. 적기만 했으면 채운 것으로 보고
        # 모양은 뒤에서 따로 알립니다 — 「안 채웠다」보다 무엇이 틀렸는지가 낫습니다.
        if left == CLOCK or ":" in right:
            return bool(right)
        # 화면은 빈칸만 세지만, 수로 읽히지 않는 값도 견줄 수 없기는 같습니다.
        return _number(cond.get("right")) is not None
    return str(cond.get("right") or "") in OPERAND_IDS


def _ok(value: Any, whole: bool) -> bool:
    """화면의 `numberWhy` 와 같은 판정 — 비었거나 0 이하면 안 되고, 정수 자리는 정수여야."""
    number = _number(value)
    if number is None or number <= 0:
        return False
    return not whole or number == int(number)


def _blank(value: Any) -> bool:
    """칸이 비었는지. 없는 칸과 빈 글자는 같은 뜻입니다 — 그 기능을 안 씁니다."""
    return value is None or str(value).strip() == ""


def _number(value: Any) -> float | None:
    # 유한한 보통 십진수만 — `nan`·`inf`·지수는 숫자가 아닌 것과 같은 갈래입니다.
    return market_values.plain_number(value)


# ── 줄이 가리키는 종목 ───────────────────────────────────

#: 종목 칸의 말. 화면이 그대로 적습니다.
_NO_PRODUCT = "품목을 고르세요"
_NO_KIND = "품목 갈래는 선물·옵션·스프레드 중 하나입니다"
_UNKNOWN_PRODUCT = "종목 마스터에서 그 품목을 찾지 못했습니다"
_OTHER_MARKET = "그 품목은 {market} 종목입니다 — 시장을 맞춰 주세요"
_UNKNOWN_CONTRACT = "종목 마스터에서 그 월물을 찾지 못했습니다"
_OTHER_PRODUCT = "고른 월물은 그 품목의 것이 아닙니다"

#: 시장 이름 → 화면에 적는 말.
_MARKET_LABELS = {"domestic": "국내", OVERSEAS: "해외"}


def instrument_problems(
    version: dict[str, Any], *, master: Any = None
) -> list[dict[str, str]]:
    """줄이 가리키는 종목의 문제들.

    보는 것은 셋입니다 — **빈 값**, **색인에 없는 품목**, **시장 불일치**.
    월물을 고른 줄은 그 월물이 정말 그 품목의 것인지도 봅니다.

    **색인을 아직 못 받았으면 모양만 봅니다.** 마스터가 없는 동안 「그 품목이
    없다」고 답하면, 앱을 켜자마자 저장하는 사용자에게 멀쩡한 품목이 틀린
    값으로 섭니다. 없는 것과 아닌 것은 다릅니다.

    **그 품목에 월물이 몇 개인지는 안 봅니다.** 색인은 주간·야간 줄의 값이
    어긋나는 종목을 통째로 빼는데(`master_index` 의 접기 충돌), 이 판본에서는
    그렇게 빠진 종목이 많아 「월물 0개」가 품목이 잘못됐다는 뜻이 아닙니다.
    """
    found: list[dict[str, str]] = []
    market = str(version.get(instrument.MARKET) or "").strip()
    code = str(version.get(instrument.PRODUCT_CODE) or "").strip()
    kind = str(version.get(instrument.PRODUCT_KIND) or "").strip()
    contract = str(version.get(instrument.CONTRACT_CODE) or "").strip()

    if not code:
        _add(found, instrument.PRODUCT_CODE, ERROR, _NO_PRODUCT)
    if kind not in instrument.KINDS:
        _add(found, instrument.PRODUCT_KIND, ERROR, _NO_KIND)

    products = _products(master)
    if products is None or not code:
        return found

    if (market, code) not in products:
        elsewhere = sorted({one for one, other in products if other == code})
        if elsewhere:
            # **짚는 칸은 시장입니다.** 품목은 있는 품목이고 갈린 것은 시장이라,
            # 품목 칸 아래에 붙이면 사용자가 멀쩡한 품목을 다시 고릅니다.
            said = _MARKET_LABELS.get(elsewhere[0], elsewhere[0])
            _add(found, instrument.MARKET, ERROR, _OTHER_MARKET.format(market=said))
        else:
            _add(found, instrument.PRODUCT_CODE, ERROR, _UNKNOWN_PRODUCT)
        return found

    if contract:
        _check_contract(master, market, code, contract, found)
    return found


def _check_contract(
    master: Any, market: str, code: str, contract: str, found: list[dict[str, str]]
) -> None:
    """고른 월물이 그 품목의 것인지.

    **만기가 지나 색인에서 빠진 월물도 코드 모양으로 풀립니다**
    (`master.product_of`) — 어제 고른 9월물이 오늘 색인에 없다고 해서 그 줄이
    잘못된 것은 아니고, 그때 답해야 하는 말은 「그 품목의 것이 아니다」가
    아니라 「못 찾았다」입니다.
    """
    try:
        held = master.product_of(market, contract)
    except Exception:  # noqa: BLE001 — 못 물어본 것을 틀렸다고 하지 않습니다.
        return
    if not isinstance(held, dict):
        _add(found, instrument.CONTRACT_CODE, ERROR, _UNKNOWN_CONTRACT)
        return
    if str(held.get("product_code") or "").strip() != code:
        _add(found, instrument.CONTRACT_CODE, ERROR, _OTHER_PRODUCT)


def _products(master: Any) -> set[tuple[str, str]] | None:
    """색인에 실린 `(시장, 품목코드)` 전부. 색인이 없으면 `None` 입니다.

    **`None` 과 빈 집합은 다릅니다** — 앞은 「아직 모른다」이고 뒤는 「하나도
    없다」입니다. 모르는 동안에는 품목을 틀렸다고 하지 않습니다.
    """
    if master is None:
        return None
    try:
        index = master.index()
    except Exception:  # noqa: BLE001 — 색인을 못 물어본 것도 「모른다」입니다.
        return None
    rows = index.get("products") if isinstance(index, dict) else None
    if not isinstance(rows, list):
        return None
    return {
        (str(one.get("market") or ""), str(one.get("product_code") or ""))
        for one in rows
        if isinstance(one, dict)
    }
