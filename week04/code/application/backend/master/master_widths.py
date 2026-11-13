"""값 칸 폭을 잡기 위한 후보 행.

표의 값 칸을 몇 글자로 잡을지는 **그 칸에 들어올 수 있는 가장 긴 값**이
정합니다. 좁으면 잘리고, 넓으면 옆 칸이 밀립니다.

자리 수가 늘수록 넓어지는 것이 아닙니다. 소수를 많이 쓰는 상품은 값 자체가
작아서(국채 110.03125) 네 자리짜리 곡물(1,288.0000)보다 오히려 짧습니다.
그래서 계산식이 아니라 실제 행을 보고 정해야 합니다.

**여기서는 후보만 고르고 그리지 않습니다.** 쉼표를 어디 찍고 부호를 어떻게
붙일지는 화면의 규칙이라, 백엔드가 흉내 내면 두 곳이 어긋납니다. 무리마다
가장 긴 행을 골라 원문 그대로 넘기면, 화면이 제 규칙으로 그려 보고 재서
폭을 정합니다.

## 무리를 가르는 축

**소수 자릿수**와 **표기 방법**입니다.

자릿수가 같아도 소수로 적는 상품과 분수로 적는 상품(국채의 1/32 꼴)은 길이가
다릅니다. 한 무리로 두면 분수 상품의 긴 값이 소수 상품의 칸까지 넓힙니다.

자릿수를 정하는 규칙은 **화면과 같아야 합니다.** 다르면 후보를 아무리 잘 골라도
그 무리에 해당하지 않는 값을 재게 됩니다.

    국내   `p_dec` 을 그대로 씁니다. 마스터가 적을 자리 수를 직접 줍니다
    해외   가격표시방법(`pind`)과 호가단위(`tsiz`) 중 **큰 쪽**입니다.
           호가단위가 더 잘게 오는 품목이 있어, 표시 쪽만 쓰면 한 틱 움직여도
           화면 숫자가 안 바뀝니다. `pind` 가 글자면 분수로 적는 상품이고
           그때 자리 수는 호가단위에서 셉니다

## 어느 값을 보는가

국내 마스터에는 가격 필드가 없어 상·하한가와 행사가로 재고, 해외는 전일종가로
잽니다. 거래량·미결제·거래대금 같은 시세 값은 시세 조회가 붙은 뒤에 따로 다룹니다.

## 가장 긴 행을 고르는 방법

같은 무리는 소수 자릿수가 같으므로, **정수부가 긴 값이 곧 가장 긴 값**입니다.
원문에는 꼬리 0 이 붙어 있어(`1288.000000000`) 글자 수를 그대로 세면 안 됩니다.

음수는 부호 한 자리를 더 쓰므로 양수와 따로 고릅니다. 국내 스프레드에만
있고, 지금 자료에서는 양수가 더 길지만 판본이 바뀌면 뒤집힐 수 있습니다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DOMESTIC = "domestic"
OVERSEAS = "overseas"

#: 소수로 적는 상품과 분수로 적는 상품.
DECIMAL = "decimal"
FRACTION = "fraction"

POSITIVE = "positive"
NEGATIVE = "negative"

#: 후보를 고르는 열. 국내 마스터에는 가격 필드가 없어 상·하한가와 행사가를 봅니다.
DOMESTIC_FIELDS = ("upper_p_lv1", "lower_p_lv1", "strike_p")
OVERSEAS_FIELDS = ("prev",)

#: 종목코드를 담은 원문 열. 어느 행에서 나온 후보인지 남깁니다.
CODE_FIELDS = {DOMESTIC: "series", OVERSEAS: "code"}


def price_decimals(pind: str | None) -> int | None:
    """가격표시방법이 말하는 소수 자릿수.

    `1` 이 소수점 없음이고 `9` 가 여덟 자리입니다. 글자는 분수로 적는 상품이라
    자리 수를 말하지 않으므로 `None` 입니다.
    """
    value = (pind or "").strip()
    if len(value) != 1 or not value.isdigit() or value == "0":
        return None
    return int(value) - 1


def tick_decimals(tsiz: str | None) -> int | None:
    """호가단위에서 센 소수 자릿수."""
    value = (tsiz or "").strip()
    if not value:
        return None
    if "." not in value:
        return 0
    return len(value.split(".", 1)[1].rstrip("0"))


def is_fraction(pind: str | None) -> bool:
    """분수로 적는 상품인지. 가격표시방법이 글자면 분수입니다."""
    value = (pind or "").strip()
    return len(value) == 1 and value.isalpha()


def decimals_of(market: str, row: dict[str, str]) -> int | None:
    """이 종목을 몇 자리로 적는가. 화면과 같은 규칙입니다."""
    if market == DOMESTIC:
        value = (row.get("p_dec") or "").strip()
        return int(value) if value.isdigit() else None

    shown = price_decimals(row.get("pind"))
    tick = tick_decimals(row.get("tsiz"))
    if shown is None:
        return tick
    if tick is None:
        return shown
    return max(shown, tick)


def integer_digits(value: str) -> int | None:
    """정수부가 몇 자리인가. 숫자로 읽을 수 없으면 `None`.

    `0.65` 는 한 자리로 봅니다 — 화면에 `0` 이 한 자 서기 때문입니다.
    """
    text = (value or "").strip()
    if not text:
        return None

    body = text.lstrip("+-")
    whole, _, fraction = body.partition(".")
    if not whole.isdigit() or (fraction and not fraction.isdigit()):
        return None
    return len(whole.lstrip("0")) or 1


@dataclass
class Candidate:
    """무리 하나에서 고른 가장 긴 행."""

    value: str
    sign: str
    field: str
    code: str
    integer_digits: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "sign": self.sign,
            "field": self.field,
            "code": self.code,
        }


@dataclass
class Group:
    """자릿수와 표기가 같은 종목 묶음."""

    market: str
    decimals: int
    notation: str
    rows: int = 0
    candidates: dict[str, Candidate] = field(default_factory=dict)
    # 후보를 고르는 것과 따로 세어 두는 최댓값. 자체검사에서 맞대 봅니다.
    longest: dict[str, int] = field(default_factory=dict)

    def offer(self, *, value: str, sign: str, source: str, code: str, digits: int) -> None:
        if digits > self.longest.get(sign, 0):
            self.longest[sign] = digits
        current = self.candidates.get(sign)
        if current is None or digits > current.integer_digits:
            self.candidates[sign] = Candidate(value, sign, source, code, digits)

    def as_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "decimals": self.decimals,
            "notation": self.notation,
            "rows": self.rows,
            "candidates": [
                self.candidates[sign].as_dict()
                for sign in (POSITIVE, NEGATIVE)
                if sign in self.candidates
            ],
        }


class WidthCandidates:
    """마스터를 훑으면서 무리마다 가장 긴 행을 모읍니다.

    색인을 만드는 순회 안에서 같이 돌므로 파일을 다시 읽지 않습니다.
    """

    def __init__(self) -> None:
        self._groups: dict[tuple[str, int, str], Group] = {}
        self._unreadable = 0
        self._no_decimals = 0
        self._tick_finer = 0

    def add(self, market: str, row: dict[str, str]) -> None:
        """종목 한 줄을 셉니다."""
        decimals = decimals_of(market, row)
        if decimals is None:
            self._no_decimals += 1
            return

        if market == OVERSEAS:
            shown = price_decimals(row.get("pind"))
            tick = tick_decimals(row.get("tsiz"))
            if shown is not None and tick is not None and tick > shown:
                # 호가단위가 표시 자릿수보다 잘게 오는 자리입니다. 화면이 큰
                # 쪽을 쓰기로 한 근거라, 몇 건인지 세어 둡니다.
                self._tick_finer += 1

        notation = FRACTION if (market == OVERSEAS and is_fraction(row.get("pind"))) else DECIMAL
        key = (market, decimals, notation)
        group = self._groups.get(key)
        if group is None:
            group = self._groups[key] = Group(market, decimals, notation)
        group.rows += 1

        code = str(row.get(CODE_FIELDS[market]) or "")
        for source in DOMESTIC_FIELDS if market == DOMESTIC else OVERSEAS_FIELDS:
            raw = str(row.get(source) or "").strip()
            if not raw:
                continue
            digits = integer_digits(raw)
            if digits is None:
                self._unreadable += 1
                continue
            sign = NEGATIVE if raw.startswith("-") else POSITIVE
            group.offer(value=raw, sign=sign, source=source, code=code, digits=digits)

    def checks(self) -> dict[str, Any]:
        """스스로 재 본 결과.

        `mismatched` 는 고른 후보의 정수부 자릿수가 따로 세어 둔 그 무리의
        최댓값과 다른 경우입니다. **비어 있어야 정상입니다.** 같은 순회 안에서
        두 갈래로 세어 맞대 보는 것이라, 고르는 코드가 어긋나면 걸리고 마스터
        자체가 이상한 것까지 잡지는 못합니다.
        """
        mismatched = []
        empty = []
        for group in self._groups.values():
            name = f"{group.market}/{group.decimals}/{group.notation}"
            if not group.candidates:
                empty.append(name)
            for sign, candidate in group.candidates.items():
                if candidate.integer_digits != group.longest.get(sign):
                    mismatched.append(f"{name}/{sign}")
        return {
            "mismatched": sorted(mismatched),
            "without_candidate": sorted(empty),
            "unreadable_values": self._unreadable,
            "rows_without_decimals": self._no_decimals,
            "tick_finer_than_display": self._tick_finer,
        }

    def payload(self) -> dict[str, Any]:
        """색인에 실을 형태."""
        groups = [
            group.as_dict()
            for key, group in sorted(self._groups.items())
            if group.candidates
        ]
        return {"groups": groups, "checks": self.checks()}
