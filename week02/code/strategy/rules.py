"""조건식 판정.

화면의 `frontend/src/data/strategyRules.ts` 와 **같은 낱말**을 씁니다 — 피연산자,
연산자, 규칙 넷(`buyEntry`·`sellEntry`·`buyExit`·`sellExit`). 이름이 갈리면
사용자가 화면에서 만든 문장과 엔진이 보는 문장이 달라집니다.

**서버가 먼저 받고 화면이 뒤따릅니다.** 호가·시간·포지션 범주의 값과 「같음」
연산자는 서버가 판정할 줄 알지만, 화면의 조건 편집기에서 고를 수 있게 되는 것은
그 뒤입니다. 그 전까지는 화면이 이 값을 쓴 조건을 만들지 않습니다.

## 호가·시간·포지션 값

지표처럼 **봉마다 하나씩** 있습니다. 규칙은 봉이 닫힐 때마다 처음부터 다시
굴러가므로(아래), 지금 값 하나만으로는 앞 봉에서의 판정을 되풀이할 수 없습니다.

    bid1·ask1            그 봉이 닫힐 때의 매수·매도 1호가
    bid1Qty·ask1Qty      그 1호가의 잔량
    positionQty          그 봉이 닫힐 때의 보유 수량(크기 — 방향 없이)
    openPl               그 봉 종가로 센 평가손익(통화). 보유가 없으면 0
    clock                봉 **시작** 시각(분). 야간 세션은 자정 뒤 시각에
                         24시간을 더해 18:00 부터 다음 날 새벽까지 한 줄로 셉니다
    barOrder             그 거래일·세션에서 몇 번째 봉 칸인지(1부터)

호가·포지션은 실행이 돌면서 적은 값이라, 실행을 세우기 전의 봉(조회로 채운 준비
봉)에는 없습니다. **없는 값은 거짓**이고, 1단계의 전이도 직전 봉 값이 없으면
전이로 치지 않습니다 — 그러지 않으면 「보유 수량 0」 같은 상태 조건이 실행을
세운 첫 봉에서 바로 걸립니다.

## 규칙은 봉을 하나씩 먹는 상태 기계입니다

단계가 여럿인 규칙은 「어느 봉을 보면 걸리나」를 봉 하나만 보고는 답할 수
없습니다. 1단계가 채워진 뒤부터 유효기간이 흐르고, 못 채우면 만료되어 1단계로
돌아가기 때문입니다. 그래서 `advance()` 가 **상태 하나와 봉 하나를 받아 다음
상태를 내고**, `fired_bars()` 가 그것을 봉 열에 대고 굴립니다.

    1단계   판정식이 **직전 봉에 거짓, 이 봉에 참**일 때만 채워집니다. 「RSI14
            55 이상」 같은 상태 조건은 한 번 참이 되면 여러 봉 동안 참이라,
            전이를 보지 않으면 봉마다 주문이 나갑니다. 교차 연산자는 정의부터
            전이라 달라지는 것이 없습니다. 첫 봉은 직전이 없어 전이가 아닙니다.
    2단계~  앞 단계가 채워진 **다음 봉부터 `withinBars` 봉 안에**(마지막 봉
            포함) **처음 참이 되는 봉**에서 채워집니다. 창이 이미 시간을 잘라
            주므로 전이를 따로 보지 않습니다. 같은 봉에 두 단계를 채우지
            않습니다.
    만료    창의 마지막 봉까지 못 채우면 1단계로 돌아갑니다. 돌아간 1단계는
            **만료 봉 다음 봉부터** 다시 전이를 봅니다 — 만료된 그 봉에서 바로
            켜지지 않습니다.
    발사    마지막 단계가 채워진 봉에서 규칙이 걸립니다. 그 다음 봉부터 다시
            1단계입니다 — 한 봉에 한 규칙은 한 번만 발사합니다.

## 시간 초과 초기화(`resetBars`)

단계마다의 창(`withinBars`)과 별도로 **규칙 전체의 시한**을 둘 수 있습니다.
1단계가 s 번째 봉에서 채워졌으면 마지막 단계는 **s + resetBars 번째 봉까지**
채워져야 하고, 그 봉을 넘기면 1단계로 돌아갑니다. 단계 창과 함께 서므로 실제
기한은 **둘 중 이른 쪽**입니다. 돌아간 뒤의 차례는 단계 창이 만료될 때와
같습니다. 칸이 없거나 0 이면 시한이 없습니다.

## 조건이 하나도 없는 규칙은 걸리지 않습니다

화면의 씨앗은 청산 규칙을 `on: true, steps: []` 로 두는데, 「빈 관문은 늘 통과」로
읽으면 **진입한 그 봉에 바로 청산**됩니다. 켜 두기만 하고 아직 안 적은 상태를
신호로 보지 않습니다.
"""
from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any

from backend.sessions.hours import KST
from backend.strategy import indicators
from backend.strategy.bars import NIGHT_CONTEXT, day_slot_of
from backend.strategy.indicators import Bar

#: 규칙 넷. 화면의 `RULE_KEYS` 와 같은 차례입니다.
RULE_KEYS: tuple[str, ...] = ("buyEntry", "sellEntry", "buyExit", "sellExit")

#: 단계 안 조건을 잇는 방법. `all` 은 모두 참, `any` 는 하나라도 참입니다. 칸이
#: 없거나 비어 있으면 `all` 입니다(옛 저장본). 저장 검사가 같은 묶음을 봅니다.
JOIN_ALL = "all"
JOIN_ANY = "any"
STEP_JOINS: tuple[str, ...] = (JOIN_ALL, JOIN_ANY)


def join_of(step: dict[str, Any]) -> str:
    """그 단계의 잇기. `any` 가 아니면 `all` 입니다."""
    return JOIN_ANY if str(step.get("join") or JOIN_ALL) == JOIN_ANY else JOIN_ALL

#: 호가 넷. 봉이 닫힐 때의 매수·매도 1호가와 그 잔량입니다.
BOOK_PRICE_IDS: tuple[str, ...] = ("bid1", "ask1")
BOOK_QTY_IDS: tuple[str, ...] = ("bid1Qty", "ask1Qty")
BOOK_IDS: tuple[str, ...] = BOOK_PRICE_IDS + BOOK_QTY_IDS

#: 포지션 둘. 봉이 닫힐 때의 보유 수량과 평가손익입니다.
POSITION_IDS: tuple[str, ...] = ("positionQty", "openPl")

#: 시간 둘. 봉의 시각에서 나옵니다.
CLOCK = "clock"
BAR_ORDER = "barOrder"
TIME_IDS: tuple[str, ...] = (CLOCK, BAR_ORDER)

#: 실행이 돌면서 봉마다 적어 두는 값. 실행 전의 봉에는 없습니다.
RECORDED_IDS: tuple[str, ...] = BOOK_IDS + POSITION_IDS

#: 호가·시간·포지션 값 전부. 1단계 전이에서 「직전 봉 값이 없으면 전이가 아님」을
#: 이 값들에만 씁니다 — 지표 판정은 그대로입니다.
CONTEXT_IDS: tuple[str, ...] = RECORDED_IDS + TIME_IDS

#: 정수로만 서는 값. 「같음」(`eq`)은 이 값들에만 뜻이 있습니다 — 가격을 같음으로
#: 견주면 소수점 끝자리 때문에 영영 안 걸립니다.
INTEGER_IDS: tuple[str, ...] = BOOK_QTY_IDS + ("positionQty",) + TIME_IDS

#: 피연산자. 화면의 `OPERANDS` 와 **같은 id** 여야 합니다. 새 값은 **끝에** 붙여
#: 앞의 차례(체결 줄의 당시 지표 차례이기도 합니다)가 바뀌지 않게 합니다.
OPERAND_IDS: tuple[str, ...] = (
    "close",
    "open",
    "high",
    "low",
    "ema20",
    "ema50",
    "ma20",
    "rsi14",
    "atr14",
    "bbUpper",
    "bbLower",
    "volume",
    "prevHigh",
    "prevLow",
) + CONTEXT_IDS + (
    # 매개변수를 받는 지표. **끝에** 붙여 앞 차례를 바꾸지 않습니다.
    "ema",
    "ma",
    "rsi",
    "atr",
    "bbMiddle",
    "vwap",
    "macdLine",
    "macdSignal",
    "macdHist",
    "stochK",
    "stochD",
)

#: 연산자. 화면의 `OPERATORS` 와 같은 id 입니다. `eq` 는 정수 값에만 씁니다.
OPERATOR_IDS: tuple[str, ...] = ("crossUp", "crossDown", "gte", "lte", "gt", "lt", "eq")

#: 시각을 적는 모양. 두 자리 시와 두 자리 분입니다.
_CLOCK_TEXT = re.compile(r"^(\d{2}):(\d{2})$")

#: 교차는 직전 봉과 이 봉을 함께 봅니다. 나머지는 이 봉만 봅니다.
CROSS_OPERATORS = frozenset({"crossUp", "crossDown"})

#: 가격과 같은 축에 서는 선. **가격 묶음 교차의 오른쪽은 이 선들뿐입니다.**
#: 앞의 다섯은 매개변수가 생기기 전의 이름입니다(`LEGACY_OPERANDS`).
CROSS_LINE_IDS: tuple[str, ...] = (
    "ema20",
    "ema50",
    "ma20",
    "bbUpper",
    "bbLower",
    "ema",
    "ma",
    "bbMiddle",
    "vwap",
)

#: 가격 묶음 교차의 왼쪽. 선에 가격 넷을 더합니다 — 「종가가 볼린저 하단을 아래로
#: 뚫음」처럼 가격이 선을 지나는 것이 교차의 흔한 쓰임입니다.
CROSS_LEFT_IDS: tuple[str, ...] = CROSS_LINE_IDS + ("close", "open", "high", "low")

#: 가격 축이 아닌 지표끼리 교차할 수 있는 짝. 같은 묶음 안에서만 교차가 섭니다.
OSCILLATOR_CROSSES: tuple[frozenset[str], ...] = (
    frozenset({"rsi"}),
    frozenset({"stochK", "stochD"}),
    frozenset({"macdLine", "macdSignal"}),
)

#: MACD 세 값과 스토캐스틱 두 값은 **매개변수 묶음 하나**를 함께 씁니다.
MACD_IDS: tuple[str, ...] = ("macdLine", "macdSignal", "macdHist")
STOCH_IDS: tuple[str, ...] = ("stochK", "stochD")
BOLLINGER_IDS: tuple[str, ...] = ("bbUpper", "bbMiddle", "bbLower")

#: 매개변수를 받는 지표의 매개변수 이름과 기본값. 기본값은 화면 차트 지표의 기본값과
#: 같습니다 — 조건의 EMA(20)과 차트의 EMA(20)이 같은 선이어야 합니다.
PARAM_SPECS: dict[str, tuple[tuple[str, float], ...]] = {
    "ema": (("period", 20),),
    "ma": (("period", 20),),
    "rsi": (("period", 14),),
    "atr": (("period", 14),),
    **{one: (("period", 20), ("multiplier", 2.0)) for one in BOLLINGER_IDS},
    **{one: (("fast", 12), ("slow", 26), ("signal", 9)) for one in MACD_IDS},
    **{one: (("period", 9), ("k", 3), ("d", 3)) for one in STOCH_IDS},
}

#: 소수를 받는 매개변수. 나머지는 정수입니다.
FLOAT_PARAMS = frozenset({"multiplier"})

#: 매개변수가 생기기 전의 이름 → `(지표, 매개변수)`. **읽을 때만** 옮기고 파일은
#: 다시 쓰지 않습니다. 화면도 이 표 하나로 옮깁니다(`legacy_table`).
#: 볼린저 둘은 이름이 그대로라 매개변수가 없으면 기본값(20, 2)으로 읽힙니다.
LEGACY_OPERANDS: dict[str, tuple[str, dict[str, float]]] = {
    "ema20": ("ema", {"period": 20}),
    "ema50": ("ema", {"period": 50}),
    "ma20": ("ma", {"period": 20}),
    "rsi14": ("rsi", {"period": 14}),
    "atr14": ("atr", {"period": 14}),
    "bbUpper": ("bbUpper", {"period": 20, "multiplier": 2.0}),
    "bbLower": ("bbLower", {"period": 20, "multiplier": 2.0}),
}

#: 지표마다의 기간. 준비 봉(`indicators.warmup_bars`)을 이 값으로 셉니다.
PERIODS: dict[str, int] = {
    "ema20": 20,
    "ema50": 50,
    "ma20": 20,
    "rsi14": 14,
    "atr14": 14,
    "bbUpper": 20,
    "bbLower": 20,
}


def legacy_table() -> dict[str, dict[str, Any]]:
    """옮김표를 화면이 읽는 모양으로. `{옛 id: {id, params}}` 입니다."""
    return {
        old: {"id": new, "params": dict(params)} for old, (new, params) in LEGACY_OPERANDS.items()
    }


def resolve(operand: Any, params: Any = None) -> tuple[str, dict[str, float]]:
    """피연산자 하나를 `(지표 id, 매개변수)` 로 풉니다.

    옛 이름은 **늘** 옮김표로 옮기고, 함께 적힌 매개변수가 있으면 그 값이 옛 이름의
    값을 덮습니다(`ema20` + 기간 5 → EMA(5)). 옛 이름을 그대로 둔 채 기간만 고친
    조건이 열을 못 찾아 조용히 죽지 않게 합니다. 매개변수를 받는 지표는 안 적은
    매개변수를 기본값으로 채웁니다. 매개변수가 없는 값(가격·호가·시간·VWAP)은 빈
    매개변수입니다.
    """
    name = str(operand or "")
    given = params if isinstance(params, dict) else {}
    if name in LEGACY_OPERANDS:
        name, given = LEGACY_OPERANDS[name][0], {**LEGACY_OPERANDS[name][1], **given}
    spec = PARAM_SPECS.get(name)
    if spec is None:
        return name, {}
    made: dict[str, float] = {}
    for key, default in spec:
        value = given.get(key, default)
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = float(default)
        made[key] = number if key in FLOAT_PARAMS else int(number)
    return name, made


def operand_key(name: str, params: dict[str, float]) -> str:
    """값 열의 이름. 매개변수가 있으면 「ema(20)」 「bbUpper(20,2)」 꼴입니다."""
    spec = PARAM_SPECS.get(name)
    if not spec:
        return name
    return f"{name}({','.join(_number_text(params[key]) for key, _ in spec)})"


def side_key(cond: dict[str, Any], side: str) -> str:
    """조건의 왼쪽(`left`)·오른쪽(`right`) 값이 읽는 열 이름.

    판정은 봉마다 처음 봉부터 다시 굴리므로 같은 조건을 봉 수만큼 묻습니다. 풀이는
    값이 바뀌지 않는 셈이라 `(이름, 매개변수)` 로 기억해 둡니다.
    """
    params = cond.get(side + "Params")
    if not params:
        return _plain_key(cond.get(side))
    return _key_of(str(cond.get(side) or ""), _frozen(params))


@lru_cache(maxsize=4096)
def _plain_key(name: Any) -> str:
    """매개변수를 안 적은 피연산자의 열 이름."""
    return operand_key(*resolve(name))


def _frozen(params: Any) -> tuple[tuple[str, str], ...]:
    """매개변수를 기억의 열쇠로 쓸 수 있는 모양으로. 없으면 빈 묶음입니다."""
    if not isinstance(params, dict) or not params:
        return ()
    return tuple(sorted((str(key), str(value)) for key, value in params.items()))


@lru_cache(maxsize=4096)
def _key_of(name: str, frozen: tuple[tuple[str, str], ...]) -> str:
    return operand_key(*resolve(name, dict(frozen)))


def side_params(cond: dict[str, Any], side: str) -> dict[str, float] | None:
    """조건 한쪽의 풀린 매개변수. 매개변수가 없는 값이면 `None` 입니다."""
    name, params = resolve(cond.get(side), cond.get(f"{side}Params"))
    return params if name in PARAM_SPECS else None


def warmup_of(name: str, params: dict[str, float]) -> int:
    """그 지표의 준비 봉. 지표가 아니면 0 입니다. 셈은 `indicators` 첫머리에."""
    period = _period_of(name, params)
    return indicators.warmup_bars(period) if period else 0


def wanted_of(rule_set: Any) -> list[tuple[str, dict[str, float]]]:
    """규칙들이 쓰는 지표 `(id, 매개변수)` 들. **같은 것은 한 번만** 셉니다."""
    made: dict[str, tuple[str, dict[str, float]]] = {}
    for cond in _conds_of(rule_set):
        sides = ["left"]
        if str(cond.get("rightKind") or "") != "number":
            sides.append("right")
        for side in sides:
            name, params = resolve(cond.get(side), cond.get(f"{side}Params"))
            if name in PARAM_SPECS or name == "vwap":
                made.setdefault(operand_key(name, params), (name, params))
    return list(made.values())


def _conds_of(rule_set: Any) -> list[dict[str, Any]]:
    """규칙 넷의 조건 전부(켜짐과 상관없이 단계 전부)."""
    made: list[dict[str, Any]] = []
    for rule in rule_set.values() if isinstance(rule_set, dict) else []:
        if not isinstance(rule, dict):
            continue
        for step in rule.get("steps") or []:
            if not isinstance(step, dict):
                continue
            made.extend(one for one in step.get("conds") or [] if isinstance(one, dict))
    return made


def _period_of(name: str, params: dict[str, float]) -> int:
    """준비 봉을 셀 때 기간으로 보는 값. 매개변수가 여럿이면 더합니다."""
    if name in MACD_IDS:
        return int(params["slow"]) + int(params["signal"])
    if name in STOCH_IDS:
        return int(params["period"]) + int(params["k"]) + int(params["d"])
    if name in PARAM_SPECS:
        return int(params["period"])
    return 0


def _number_text(value: float) -> str:
    """매개변수를 열 이름에 적는 글자. `2.0` 은 `2`, `2.5` 는 `2.5` 입니다."""
    number = float(value)
    return str(int(number)) if number == int(number) else f"{number:g}"


#: 매개변수가 생기기 전에 늘 셌던 지표들. 규칙 없이 열을 묻는 자리가 쓰는 기본입니다.
_LEGACY_WANTED = [resolve(old) for old in LEGACY_OPERANDS]


@dataclass(frozen=True)
class Series:
    """봉 하나하나의 피연산자 값. 값이 아직 없는 자리는 `None` 입니다."""

    values: dict[str, list[float | None]]
    length: int
    #: 실행이 붙은 세션(`domestic-night` 등). 조건 오른쪽에 적은 시각을 이
    #: 세션의 셈으로 읽습니다 — 야간이면 「01:00」이 자정 뒤입니다.
    session: str = ""

    def at(self, operand: str, index: int) -> float | None:
        """`index` 번째 봉의 값. 모르는 피연산자나 범위 밖이면 `None`."""
        column = self.values.get(operand)
        if column is None or not 0 <= index < len(column):
            return None
        return column[index]


@dataclass(frozen=True)
class Stage:
    """규칙이 지금 무엇을 기다리는지. 봉을 하나 먹을 때마다 새로 나옵니다."""

    #: 기다리는 단계의 자리. 0 이면 1단계(전이를 봅니다).
    step: int = 0
    #: 그 단계를 채울 수 있는 마지막 봉. 1단계를 기다리는 동안은 뜻이 없습니다.
    deadline: int = -1
    #: 1단계가 채워진 봉. 규칙 전체의 시한(`resetBars`)을 여기서부터 셉니다.
    origin: int = -1


def series_of(
    bars: list[Bar],
    *,
    sessions: list[str] | None = None,
    marks: dict[int, dict[str, float | None]] | None = None,
    session: str = "",
    wanted: list[tuple[str, dict[str, float]]] | None = None,
    day_of: Callable[[int, str], Any] | None = None,
) -> Series:
    """봉 목록에서 피연산자를 모두 셉니다.

    `sessions` 는 봉마다의 세션(봉 목록과 같은 길이)으로, 시각 열을 셀 때 씁니다.
    `marks` 는 **봉 시각 → 값들**이고, 실행이 봉마다 적어 둔 호가·포지션 값과
    봉 순서가 여기서 옵니다. 봉 시각을 열쇠로 두는 것은, 끊긴 뒤 다시 채울 때
    봉이 가운데에 끼고 봉 수 상한에서 앞이 잘려 자리 번호가 밀리기 때문입니다.
    적힌 것이 없는 봉은 `None` 입니다. `session` 은 실행이 붙은 세션입니다.

    **한 번에 다 셉니다.** 조건마다 필요한 것만 세면 같은 지표를 규칙 수만큼
    다시 세게 되고, 규칙이 늘수록 느려지는 자리가 됩니다.

    지표 열은 **준비 봉이 찰 때까지 `None`** 입니다(`indicators.warmed`). 값이
    나오기는 기간만큼만 모여도 나오지만, 앞쪽 값은 시드에 끌려다녀 같은 자리에서
    다른 수가 됩니다. 가격·거래량은 셀 것이 없어 그대로 둡니다.

    `wanted` 는 셀 지표 `(id, 매개변수)` 들입니다(`wanted_of`). 안 주면 매개변수가
    생기기 전에 늘 셌던 일곱을 셉니다. 열 이름은 `operand_key` 이고, 옛 이름과
    같은 지표면 옛 이름(`ema20` 등)으로도 같은 열을 둡니다.
    """
    closes = [bar.close for bar in bars]
    values: dict[str, list[float | None]] = {
        "close": list(closes),
        "open": [bar.open for bar in bars],
        "high": [bar.high for bar in bars],
        "low": [bar.low for bar in bars],
        "volume": [bar.volume for bar in bars],
        # 「이전 1봉의 고가·저가」 — 한 칸 밀어 둡니다. 그래야 조건이 이 봉의
        # 값과 바로 견줄 수 있습니다.
        "prevHigh": [None] + [bar.high for bar in bars[:-1]],
        "prevLow": [None] + [bar.low for bar in bars[:-1]],
    }
    contexts = sessions if sessions is not None and len(sessions) == len(bars) else None
    for name, params in _LEGACY_WANTED if wanted is None else wanted:
        key = operand_key(name, params)
        if key in values:
            continue
        values[key] = _indicator_column(name, params, bars, closes, contexts, day_of)
    for old, (new, params) in LEGACY_OPERANDS.items():
        key = operand_key(new, params)
        if key in values and old != key:
            values[old] = values[key]

    values[CLOCK] = [
        float(bar_clock(bar.timestamp, contexts[at] if contexts else "")) for at, bar in enumerate(bars)
    ]
    held = marks or {}
    for name in RECORDED_IDS + (BAR_ORDER,):
        values[name] = [(held.get(bar.timestamp) or {}).get(name) for bar in bars]
    return Series(values=values, length=len(bars), session=session)


def extend_series(
    base: Series,
    bars: list[Bar],
    *,
    drop: int = 0,
    sessions: list[str] | None = None,
    mark: dict[str, float | None] | None = None,
    session: str = "",
    wanted: list[tuple[str, dict[str, float]]] | None = None,
    day_of: Callable[[int, str], Any] | None = None,
) -> Series:
    """`series_of` 와 **같은 값**을, 앞서 센 열에 마지막 봉 한 칸만 붙여 셉니다.

    `base` 는 `bars` 의 앞에서 `drop` 개를 더한 목록(마지막 봉 빼고)으로 센 값입니다
    — 봉 수 상한에서 앞이 잘린 만큼이 `drop` 입니다. `mark` 는 마지막 봉에 적힌
    값(호가·보유·봉 순서)입니다.

    가격·시각·적힌 값처럼 봉마다 따로인 칸은 이어 붙이고, **지표 열은 봉 전부로
    다시 셉니다** — 지표는 앞쪽 값을 기억하는 것(EMA 등)이 있어 마지막 칸만 고칠 수
    없습니다. 봉마다 다시 만드는 칸이 판정 값 셈의 대부분이라, 진행 중인 봉을
    체결마다 판정할 때 이 길을 씁니다.
    """
    contexts = sessions if sessions is not None and len(sessions) == len(bars) else None
    last = bars[-1]
    before = bars[-2] if len(bars) > 1 else None
    tail: dict[str, float | None] = {
        "close": last.close,
        "open": last.open,
        "high": last.high,
        "low": last.low,
        "volume": last.volume,
        "prevHigh": before.high if before is not None else None,
        "prevLow": before.low if before is not None else None,
        CLOCK: float(bar_clock(last.timestamp, contexts[-1] if contexts else "")),
    }
    held = mark or {}
    for name in RECORDED_IDS + (BAR_ORDER,):
        tail[name] = held.get(name)
    values: dict[str, list[float | None]] = {}
    for name, value in tail.items():
        column = list(base.values.get(name) or [])[drop:]
        column.append(value)
        values[name] = column
    if drop and len(bars) > 1:
        # 맨 앞 봉의 「이전 봉」은 이제 목록에 없습니다(`series_of` 와 같게).
        values["prevHigh"][0] = None
        values["prevLow"][0] = None
    closes = values["close"]
    for name, params in _LEGACY_WANTED if wanted is None else wanted:
        key = operand_key(name, params)
        if key in values:
            continue
        values[key] = _indicator_column(name, params, bars, closes, contexts, day_of)
    for old, (new, params) in LEGACY_OPERANDS.items():
        key = operand_key(new, params)
        if key in values and old != key:
            values[old] = values[key]
    return Series(values=values, length=len(bars), session=session)


def _vwap_marks(
    bars: list[Bar],
    contexts: list[str] | None,
    day_of: Callable[[int, str], Any] | None,
) -> list[tuple[Any, str]]:
    """VWAP 의 세션 표지 — 봉의 거래일과 세션 창(`indicators` 첫머리).

    `day_of` 가 없으면 거래일은 `bars.day_slot_of` 입니다(국내 야간은 다음 날).
    있으면 그것이 답한 거래일입니다(해외는 시간표의 창). 그것이 `None`(창 밖)이면
    **앞 봉의 거래일을 잇습니다** — 정비 시간에 찍힌 봉에서 VWAP 이 새로 서지
    않게 합니다. 첫 봉부터 창 밖이면 달력 날짜입니다.
    """
    made: list[tuple[Any, str]] = []
    before: Any = None
    for at, bar in enumerate(bars):
        context = contexts[at] if contexts else ""
        day = day_of(bar.timestamp, context) if day_of is not None else None
        if day is None:
            day = before if day_of is not None and before is not None else day_slot_of(bar.timestamp, context)
        made.append((day, context))
        before = day
    return made


def _indicator_column(
    name: str,
    params: dict[str, float],
    bars: list[Bar],
    closes: list[float],
    contexts: list[str] | None,
    day_of: Callable[[int, str], Any] | None = None,
) -> list[float | None]:
    """지표 열 하나. 준비 봉이 찰 때까지의 앞쪽은 `None` 입니다(`indicators.warmed`)."""
    if name == "vwap":
        return indicators.vwap(bars, _vwap_marks(bars, contexts, day_of))
    if name == "ema":
        column = indicators.ema(closes, int(params["period"]))
    elif name == "ma":
        column = indicators.sma(closes, int(params["period"]))
    elif name == "rsi":
        column = indicators.rsi(closes, int(params["period"]))
    elif name == "atr":
        column = indicators.atr(bars, int(params["period"]))
    elif name in BOLLINGER_IDS:
        upper, middle, lower = indicators.bollinger(
            closes, int(params["period"]), float(params["multiplier"])
        )
        column = {"bbUpper": upper, "bbMiddle": middle, "bbLower": lower}[name]
    elif name in MACD_IDS:
        line, signal, hist = indicators.macd(
            closes, int(params["fast"]), int(params["slow"]), int(params["signal"])
        )
        column = {"macdLine": line, "macdSignal": signal, "macdHist": hist}[name]
    elif name in STOCH_IDS:
        k_line, d_line = indicators.stochastic(
            bars, int(params["period"]), int(params["k"]), int(params["d"])
        )
        column = k_line if name == "stochK" else d_line
    else:
        return [None] * len(bars)
    return indicators.warmed(column, _period_of(name, params))


def clock_of(hour: int, minute: int, context: str) -> int:
    """하루 안의 시각을 **세션 안의 차례**로 쓰는 분 값.

    야간 세션은 자정을 넘어갑니다. 12시 전 시각에 하루(1440 분)를 더해 18:00
    뒤에 01:00 이 오도록 합니다 — 안 그러면 「23:00 이후」가 새벽에 거짓이 됩니다.
    경계 12시는 거래일을 가르는 규칙(`bars.day_slot_of`)과 같습니다. 봉의 시각과
    조건에 적은 시각이 **이 한 함수**를 지나므로 둘의 셈이 갈리지 않습니다.
    """
    made = hour * 60 + minute
    if context == NIGHT_CONTEXT and hour < 12:
        made += 1440
    return made


@lru_cache(maxsize=16384)
def bar_clock(timestamp: int, context: str) -> int:
    """봉 **시작** 시각의 분 값(`clock_of`).

    봉이 닫힐 때마다 봉 목록 전체를 다시 세므로 같은 봉을 여러 번 묻습니다.
    값이 바뀌지 않는 셈이라 기억해 둡니다.
    """
    moment = datetime.fromtimestamp(timestamp / 1000, tz=KST)
    return clock_of(moment.hour, moment.minute, context)


def parse_clock(text: Any) -> tuple[int, int] | None:
    """「HH:MM」(00:00~23:59) 을 `(시, 분)` 으로. 모양이 다르면 `None` 입니다."""
    found = _CLOCK_TEXT.match(str(text if text is not None else "").strip())
    if not found:
        return None
    hour, minute = int(found.group(1)), int(found.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def right_at(
    cond: dict[str, Any], series: Series, index: int, key: str | None = None
) -> float | None:
    """조건의 오른쪽 값. 수로 적은 것이면 그 수, 값이면 그 봉의 값입니다.

    왼쪽이 시각이면 오른쪽은 「HH:MM」 이고, 실행이 붙은 세션의 셈으로 읽습니다.
    `key` 는 오른쪽 값의 열 이름을 이미 풀어 둔 것입니다(같은 조건을 두 봉에서 볼 때).
    """
    if str(cond.get("rightKind") or "") == "number":
        if str(cond.get("left") or "") == CLOCK:
            parsed = parse_clock(cond.get("right"))
            return None if parsed is None else float(clock_of(*parsed, series.session))
        try:
            return float(str(cond.get("right") or "").strip())
        except ValueError:
            return None
    return series.at(key if key is not None else side_key(cond, "right"), index)


def cond_at(cond: dict[str, Any], series: Series, index: int) -> bool:
    """조건 하나가 그 봉에서 참인지. **값을 모르면 거짓입니다.**

    준비가 안 된 지표(`None`)를 참으로 보면 봉이 모자란 채로 주문이 나갑니다.
    """
    left_key = side_key(cond, "left")
    left = series.at(left_key, index)
    if left is None:
        return False
    right_key = side_key(cond, "right")
    right = right_at(cond, series, index, right_key)
    if right is None:
        return False

    operator = str(cond.get("op") or "")
    if operator in CROSS_OPERATORS:
        before_left = series.at(left_key, index - 1)
        before_right = right_at(cond, series, index - 1, right_key)
        if before_left is None or before_right is None:
            return False
        if operator == "crossUp":
            return before_left <= before_right and left > right
        return before_left >= before_right and left < right

    if operator == "gt":
        return left > right
    if operator == "lt":
        return left < right
    if operator == "gte":
        return left >= right
    if operator == "lte":
        return left <= right
    if operator == "eq":
        # 정수로만 서는 값에만 받습니다(`validate.py`). 가격은 여기 오지 않습니다.
        return left == right
    # 모르는 연산자는 참으로 보지 않습니다 — 조용히 걸리는 것이 가장 나쁩니다.
    return False


def step_at(step: dict[str, Any], series: Series, index: int) -> bool:
    """단계 하나가 그 봉에서 채워지는지. 조건이 없으면 거짓입니다."""
    conds = [one for one in (step.get("conds") or []) if isinstance(one, dict)]
    if not conds:
        return False
    if join_of(step) == JOIN_ANY:
        return any(cond_at(one, series, index) for one in conds)
    return all(cond_at(one, series, index) for one in conds)


def steps_of(rule: dict[str, Any]) -> list[dict[str, Any]]:
    """규칙이 실제로 판정할 단계들. 켜져 있지 않거나 조건이 없으면 빈 목록입니다."""
    if not rule.get("on"):
        return []
    steps = [one for one in (rule.get("steps") or []) if isinstance(one, dict)]
    if not steps or not any(one.get("conds") for one in steps):
        return []
    return steps


def advance(stage: Stage, rule: dict[str, Any], series: Series, index: int) -> tuple[Stage, bool]:
    """봉 하나를 먹고 `(다음 상태, 이 봉에서 발사했는지)`.

    차례는 이 파일 첫머리에 있습니다 — 만료를 먼저 보고, 기다리던 단계를 재고,
    채워졌으면 다음 단계의 창을 엽니다.
    """
    steps = steps_of(rule)
    if not steps:
        return Stage(), False

    at = stage.step
    deadline = stage.deadline
    origin = stage.origin
    if at > 0 and (at >= len(steps) or index > deadline):
        # 창의 마지막 봉까지 못 채웠습니다. 이 봉부터 1단계를 다시 봅니다.
        at, deadline, origin = 0, -1, -1

    if at == 0:
        # 1단계는 직전 봉에 거짓이었다가 이 봉에 참이 된 것만 봅니다. **첫 봉은
        # 전이가 아닙니다** — 직전이 없는 것을 거짓으로 세면, 이미 참이던 상태
        # 조건이 시작하자마자 한 번 걸립니다.
        filled = index > 0 and step_at(steps[0], series, index)
        filled = filled and not step_at(steps[0], series, index - 1)
        # 호가·포지션처럼 실행이 적는 값은 직전 봉에 **없을** 수 있습니다(실행을
        # 세우기 전의 봉). 없는 것은 거짓으로 읽히므로 그대로 두면 이 봉이
        # 전이로 보입니다 — 첫 봉과 같은 까닭으로 전이로 치지 않습니다.
        filled = filled and not _unknown_before(steps[0], series, index - 1)
    else:
        filled = step_at(steps[at], series, index)

    if not filled:
        return Stage(step=at, deadline=deadline, origin=origin), False
    if at == len(steps) - 1:
        return Stage(), True
    if at == 0:
        origin = index

    window = _window_of(steps[at])
    if window < 1:
        # 다음 단계를 볼 창이 없습니다. 저장할 때 막는 값이라(`validate.py`)
        # 여기까지 오면 설정이 아니라 보낸 쪽이 잘못된 것입니다.
        return Stage(), False
    deadline = index + window
    reset = reset_bars_of(rule)
    if reset > 0:
        # 규칙 전체의 시한이 단계 창보다 이르면 그쪽을 따릅니다.
        deadline = min(deadline, origin + reset)
    return Stage(step=at + 1, deadline=deadline, origin=origin), False


def fired_bars(rule: dict[str, Any], series: Series) -> list[int]:
    """그 규칙이 발사하는 봉들의 자리. 처음 봉부터 차례로 굴려 얻습니다."""
    stage = Stage()
    made: list[int] = []
    for index in range(series.length):
        stage, fired = advance(stage, rule, series, index)
        if fired:
            made.append(index)
    return made


def stage_at(rule: dict[str, Any], series: Series, index: int) -> tuple[Stage, bool]:
    """그 봉을 먹은 뒤의 `(상태, 그 봉에서 발사했는지)`. 범위 밖이면 빈 상태·거짓입니다.

    `fired_bars` 와 **같은 굴림**을 그 봉까지 합니다 — 단계가 여럿이면 앞선
    봉들이 상태를 만들기 때문입니다. 판정(`fires_at`)과 화면이 보는 「지금
    무엇을 기다리나」(`condition_table`)가 이 한 번의 굴림을 나눠 씁니다.
    """
    if not 0 <= index < series.length:
        return Stage(), False
    stage, fired = Stage(), False
    for at in range(index + 1):
        stage, fired = advance(stage, rule, series, at)
    return stage, fired


def fires_at(rule: dict[str, Any], series: Series, index: int) -> bool:
    """규칙이 그 봉에서 걸리는지.

    **처음 봉부터 굴려 봅니다**(`stage_at`). 단계가 여럿이면 앞선 봉들이 상태를
    만들기 때문입니다.
    """
    return stage_at(rule, series, index)[1]


def condition_table(
    rule: dict[str, Any], series: Series, index: int, stage: Stage, fired: bool
) -> dict[str, Any]:
    """규칙 하나가 그 봉에서 **무엇을 기다리고 조건마다 어떤지**. 판정을 다시 하지 않습니다.

    `stage`·`fired` 는 판정이 그 봉에서 얻은 것(`stage_at`)을 그대로 받습니다.

    **보이는 상태는 다음 봉의 판정과 같게 냅니다.** 단계 창의 마지막 봉을 먹고도
    못 채웠으면 판정은 다음 봉에서야 1단계로 돌아가지만, 여기서는 그때 기다릴 것
    (1단계)을 냅니다. `barsLeft` 와 `resetLeft` 는 **이 봉 뒤에 남은 봉 수**입니다 —
    1 이면 다음 봉까지 채울 수 있습니다. 1단계를 기다리는 동안은 둘 다 없습니다.

    1단계는 **단계 전체가** 직전 봉에 거짓이고 이 봉에 참일 때 채워지므로, 그
    단계 줄에 `stepNow`·`stepBefore`·`beforeUnknown` 을 함께 싣습니다(조건 하나의
    참·거짓만으로는 「이미 참이라 안 걸림」을 말할 수 없습니다). 값을 모르면 `None`
    이고 0 이나 거짓으로 채우지 않습니다.
    """
    steps = steps_of(rule)
    at = stage.step
    if at > 0 and (at >= len(steps) or stage.deadline <= index):
        stage = Stage()
        at = 0
    reset = reset_bars_of(rule)
    made_steps = []
    for place, step in enumerate(steps):
        row: dict[str, Any] = {
            "join": join_of(step),
            "withinBars": _window_of(step),
            "conds": [
                cond_row(cond, series, index)
                for cond in (step.get("conds") or [])
                if isinstance(cond, dict)
            ],
        }
        if place == 0 and at == 0:
            row["stepNow"] = step_state(step, series, index)
            row["stepBefore"] = step_state(step, series, index - 1) if index > 0 else None
            row["beforeUnknown"] = index <= 0 or _unknown_before(step, series, index - 1)
        made_steps.append(row)
    return {
        "waitingStep": at + 1,
        "steps": len(steps),
        "barsLeft": stage.deadline - index if at > 0 else None,
        "resetLeft": stage.origin + reset - index if at > 0 and reset > 0 else None,
        "fired": bool(fired),
        "stepsDetail": made_steps,
    }


def cond_row(cond: dict[str, Any], series: Series, index: int) -> dict[str, Any]:
    """조건 한 줄 — 적힌 그대로와 그 봉의 값, 이 봉·직전 봉의 참."""
    return {
        "left": str(cond.get("left") or ""),
        "op": str(cond.get("op") or ""),
        "right": cond.get("right"),
        "rightKind": str(cond.get("rightKind") or ""),
        "leftParams": side_params(cond, "left"),
        "rightParams": side_params(cond, "right") if str(cond.get("rightKind") or "") != "number" else None,
        "leftValue": _finite(series.at(side_key(cond, "left"), index)),
        "rightValue": _finite(right_at(cond, series, index)),
        "now": cond_state(cond, series, index),
        "before": cond_state(cond, series, index - 1) if index > 0 else None,
    }


def cond_state(cond: dict[str, Any], series: Series, index: int) -> bool | None:
    """조건 하나의 그 봉 참·거짓. **판정할 수 없으면 `None`** 입니다.

    값이 없거나(준비 전 지표·실행 전의 호가) 모르는 연산자면 판정이 거짓으로
    읽는 자리이지만, 화면에는 「거짓」이 아니라 「모름」으로 보입니다. 판정할 수
    있을 때의 답은 판정(`cond_at`)과 같습니다.
    """
    operator = str(cond.get("op") or "")
    if operator not in OPERATOR_IDS:
        return None
    left_key = side_key(cond, "left")
    if series.at(left_key, index) is None or right_at(cond, series, index) is None:
        return None
    if operator in CROSS_OPERATORS and (
        series.at(left_key, index - 1) is None or right_at(cond, series, index - 1) is None
    ):
        return None
    return cond_at(cond, series, index)


def step_state(step: dict[str, Any], series: Series, index: int) -> bool | None:
    """단계 하나의 그 봉 참·거짓(`step_at`). 봉이 범위 밖이면 `None` 입니다."""
    if not 0 <= index < series.length:
        return None
    return step_at(step, series, index)


def _finite(value: float | None) -> float | None:
    """수로 쓸 수 있는 값만. `nan`·`inf` 는 모름입니다."""
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def evaluate(rules: dict[str, Any], series: Series, index: int) -> list[str]:
    """그 봉에서 걸린 규칙들의 이름. 차례는 `RULE_KEYS` 와 같습니다."""
    return [
        key
        for key in RULE_KEYS
        if isinstance(rules.get(key), dict) and fires_at(rules[key], series, index)
    ]


def _unknown_before(step: dict[str, Any], series: Series, index: int) -> bool:
    """그 단계가 쓰는 호가·시간·포지션 값 가운데 `index` 봉에서 없는 것이 있는지.

    지표·가격 값은 보지 않습니다 — 그 판정은 전과 같습니다.
    """
    for cond in step.get("conds") or []:
        if not isinstance(cond, dict):
            continue
        names = [str(cond.get("left") or "")]
        if str(cond.get("rightKind") or "") != "number":
            names.append(str(cond.get("right") or ""))
        for name in names:
            if name in CONTEXT_IDS and series.at(name, index) is None:
                return True
    return False


def reset_bars_of(rule: dict[str, Any]) -> int:
    """규칙 전체의 시한(봉 수). 없거나 읽을 수 없거나 0 이하면 0 — 시한이 없습니다."""
    try:
        return max(int(float(str(rule.get("resetBars") or 0).strip())), 0)
    except ValueError:
        return 0


def _window_of(step: dict[str, Any]) -> int:
    try:
        return int(float(str(step.get("withinBars") or 0)))
    except ValueError:
        return 0
