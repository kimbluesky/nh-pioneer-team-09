"""시세 값을 화면 모양으로 바꿀 때 두 경로가 함께 쓰는 규칙.

같은 종목의 같은 값이 **조회로도 오고 실시간으로도 옵니다.** 둘은 항목 이름도
모양도 다릅니다 — 조회 호가는 배열 하나로 오고 실시간 호가는 `ask_pric_1` 처럼
번호가 붙은 칸으로 옵니다. 그래도 화면에 서는 모양은 **하나여야 합니다.**

규칙을 양쪽에 각각 적어 두면 한쪽만 고쳐지는 날이 옵니다. 그날 호가창은
조회로 열면 맞고 실시간이 들어오면 뒤집힌 채로 섭니다. 그래서 규칙을 여기
한 자리에 둡니다. 부르는 쪽은 자기 응답에서 값을 꺼내 오기만 합니다.

여기 담긴 규칙은 셋입니다.

**숫자는 숫자로 냅니다.** 값이 비었거나 숫자로 읽히지 않으면 `None` 입니다.
소수 자릿수는 채우지 않습니다 — 몇 자리로 적을지는 종목 마스터가 알고 있고
화면이 그 자리에서 정합니다.

**호가는 매도를 뒤집어 냅니다.** NH선물은 최우선 호가부터 담아 보내는데 화면은
매도를 먼 값부터 받습니다. 주문 화면이 매수·매도 단추 가격을 배열의 끝에서
뽑으므로, 뒤집지 않으면 단추에 가장 나쁜 호가가 붙습니다.

**값이 안 채워진 호가 단계는 뺍니다.** 그 종목에 없는 단계는 가격 0·잔량 0 으로
옵니다. 그대로 넘기면 호가창에 `0.00` 줄이 섭니다. **그래서 호가 배열이 비어 올
수 있습니다** — 받는 쪽이 빈 배열을 다룰 수 있어야 합니다.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any


def date(text: Any) -> str | None:
    """`YYYYMMDD` 를 `YYYY-MM-DD` 로. 날짜가 아니면 `None`.

    **자리 수만 세지 않고 실제로 있는 날인지 봅니다.** 잘라 붙이기만 하면
    `20261345` 가 `2026-13-45` 로 화면에 섭니다. 값이 이상한 것과 날짜가 없는
    것은 화면에서 같게 다루면 되므로 둘 다 `None` 입니다.
    """
    body = str(text or "").strip()
    if len(body) != 8 or not body.isdigit():
        return None
    try:
        return datetime.strptime(body, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def clock(text: Any) -> str | None:
    """`HHMMSS` 를 `HH:MM:SS` 로. 시각이 아니면 `None`.

    **아홉 자리로 오는 자리가 있습니다.** 주문내역의 거래소 접수·체결 시각이
    그렇습니다. 명세 **안에서 두 칸이 서로 어긋나 있습니다** — 길이 칸은 이미
    9 인데 설명 칸만 `HHMMSS` 로 적혀 있고, 실제로 오는 것은 밀리초 세 자리가
    붙은 `HHMMSSmmm` 입니다. 여섯 자리만
    받으면 그 시각이 통째로 `None` 이 되어 화면에서 칸이 빕니다.

    그래서 여섯과 아홉을 모두 받고 앞 여섯 자리를 씁니다. 밀리초가 필요해지면
    원문에 그대로 있습니다.

    **전부 0 이면 시각이 아니라 「없음」입니다.** 아직 체결되지 않은 주문의
    체결시각이 그렇게 올 수 있습니다. 그대로 두면 화면에 「00:00:00 에 체결」이
    섭니다 — 가격 0 을 없음으로 보는 것과 같은 자리입니다.
    """
    body = str(text or "").strip()
    if len(body) not in (6, 9) or not body.isdigit():
        return None
    if not body.strip("0"):
        return None
    return f"{body[0:2]}:{body[2:4]}:{body[4:6]}"


def stamp(text: Any) -> tuple[str | None, str | None]:
    """`YYYYMMDDHHMMSS` 를 (날짜, 시각)으로. 아니면 (None, None).

    **해외 주문내역은 시각을 열네 자리로 냅니다** — 국내가 날짜와 시각을 따로
    주는 것과 달리 한 칸에 붙여 옵니다. `clock()` 은 여섯·아홉 자리만 받으므로
    그대로 넘기면 시각이 통째로 사라집니다.

    전부 0 이면 「없음」입니다 — 체결되지 않은 주문의 체결시각이 그렇게 올 수
    있습니다. 가격 0 을 없음으로 보는 것과 같은 자리입니다.

    **한쪽만 읽히면 읽힌 쪽만 냅니다.** 날짜가 있을 수 없는 날이어도 시각은
    그대로 씁니다 — 못 읽는 날짜 때문에 멀쩡한 시각까지 버릴 이유가 없습니다.
    """
    body = str(text or "").strip()
    if len(body) != 14 or not body.isdigit() or not body.strip("0"):
        return None, None
    return date(body[:8]), clock(body[8:])


#: 천 단위 쉼표로 묶인 숫자의 모양(`1,234.25`·`-1,234`). 묶음이 틀리면(`1,2`) 숫자가 아닙니다.
_GROUPED = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$")


def _ungrouped(body: str) -> str | None:
    """천 단위 쉼표를 뺀 글자. 쉼표가 없으면 그대로이고, 묶음이 틀리면 `None` 입니다."""
    if "," not in body:
        return body
    return body.replace(",", "") if _GROUPED.match(body) else None


def number(text: Any) -> float | None:
    """문자열로 온 값을 숫자로. 비었거나 숫자가 아니면 `None`.

    **천 단위 쉼표가 붙어 오는 칸이 있습니다**(해외 보유포지션만기조회의 체결가격
    `1,234.25`). 올바른 천 단위 묶음만 받고, 묶음이 틀린 쉼표(`1,2`)는 숫자가
    아닌 것으로 봅니다. 서버 답이라 모양이 틀리면 읽지 않습니다 — 사람이 적는
    숫자(`plain_number`)는 쉼표를 모두 지우고 읽어 규칙이 다릅니다.
    """
    if text is None:
        return None
    body = _ungrouped(str(text).strip())
    if not body:
        return None
    try:
        value = float(body)
    except ValueError:
        return None
    # **음수 0 은 0 으로 폅니다.** 해외 호가에 `-0.00` 이 실제로 옵니다. 그대로
    # 두면 화면이 「−0.00」으로 적고 내림세 색을 칠합니다 — 오르지도 내리지도
    # 않은 값인데 읽는 사람은 내렸다고 봅니다.
    return 0.0 if value == 0 else value


#: 사람이 칸에 적는 숫자의 모양 — 부호, 숫자, 소수점만(쉼표는 먼저 뺍니다).
_PLAIN_NUMBER = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)$")


def plain_number(value: Any) -> float | None:
    """사람이 적은 숫자를 읽습니다. **유한한 보통 십진수만** 받고, 아니면 `None`.

    `float()` 가 받아 주는 `nan`·`inf`·`infinity`·지수(`1e3`)·16진수는 사람이
    칸에 적는 숫자가 아니라 거절합니다 — `nan` 은 크기 비교가 늘 거짓이라 「0 보다
    큰가」 같은 검사를 빠져나가고, `inf` 는 정수로 바꾸는 자리에서 터집니다.
    천 단위 쉼표(`1,000`)는 받습니다. 사람이 적는 값이라 쉼표를 모두 지우고 읽습니다 —
    서버 답을 읽는 `number` 는 올바른 묶음만 받아 규칙이 다릅니다. 빈 값도 `None` 입니다.
    """
    body = str(value if value is not None else "").strip().replace(",", "")
    if not _PLAIN_NUMBER.match(body):
        return None
    number = float(body)
    return number if math.isfinite(number) else None


def count(text: Any) -> int | None:
    """개수처럼 정수로 쓰는 값. 비었거나 숫자가 아니면 `None`."""
    value = number(text)
    return None if value is None else int(value)


def ladder(levels: Iterable[tuple[Any, Any]]) -> list[dict[str, float]]:
    """호가 한 쪽을 화면 모양으로.

    받는 것은 **최우선 단계부터** 늘어놓은 (가격, 잔량) 짝입니다. 값이 안 채워진
    단계는 빠지므로 결과가 빈 배열일 수 있습니다.

    뒤집는 것은 부르는 쪽의 몫입니다 — 매도만 뒤집고 매수는 그대로 두기
    때문입니다. `asks()` 와 `bids()` 를 쓰면 그 판단까지 함께 옵니다.
    """
    made: list[dict[str, float]] = []
    for price, quantity in levels:
        value = number(price)
        if value is None or value == 0:
            # 그 종목에 없는 단계입니다. 가격 0 을 호가로 그리면 안 됩니다.
            continue
        made.append({"price": value, "quantity": count(quantity) or 0})
    return made


def asks(levels: Iterable[tuple[Any, Any]]) -> list[dict[str, float]]:
    """매도 호가. 먼 값이 앞에 오도록 뒤집어 냅니다."""
    made = ladder(levels)
    made.reverse()
    return made


def bids(levels: Iterable[tuple[Any, Any]]) -> list[dict[str, float]]:
    """매수 호가. 가까운 값이 앞에 오는 순서 그대로입니다."""
    return ladder(levels)
