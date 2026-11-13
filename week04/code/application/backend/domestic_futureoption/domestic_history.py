"""국내 분봉을 **영업일 단위**로 모읍니다.

차트를 왼쪽으로 끌 때마다 앞 영업일을 하나씩 이어 붙이는 자리입니다.

## 왜 달력 날짜로는 안 되나

한 영업일은 **전날 18:00 야간 개장부터 그날 15:45 주간 마감까지**입니다. 그런데
분봉 조회는 달력 날짜로 자릅니다 — 야간을 날짜 하나로 부르면 그 날짜의
`00:00~06:00`(앞 영업일 야간의 **뒷**부분)과 `18:00~23:59`(다음 영업일 야간의
**앞**부분)이 한 응답에 섞여 옵니다. 그대로 그리면 두 영업일의 반쪽이 한 덩어리로
붙고, 그 영업일 야간의 앞 여섯 시간은 아예 빠집니다.

그래서 한 영업일을 **조회 셋**으로 모읍니다.

    18:00~23:59   야간 앞부분   (달력 날짜 = **앞 영업일**)
    00:00~06:00   야간 뒷부분   (달력 날짜 = **야간이 열린 날의 다음 날**)
    08:45~15:45   주간          (달력 날짜 = 그 영업일)

「전날」이 아니라 **앞 영업일**입니다. 월요일 영업일이면 이렇게 나갑니다.

    20260918 180000~235959 야간   ← 금요일 저녁
    20260919 000000~060000 야간   ← 토요일 새벽
    20260921 000000~235959 주간

야간이 없는 품목은 주간 하나뿐입니다 — 야간을 부르지 않습니다.

## 자정을 넘는 요청은 안 씁니다

`18:00~06:00` 처럼 시각 범위가 자정을 넘는 요청을 NH선물이 어떻게 읽는지
**명세에 없습니다.** 그래서 각 조회가 한 달력 날짜 안에 들어오게 나눕니다.

## 휴장일 달력이 없습니다

앞 영업일은 **주말만 건너뜁니다**(`sessions/hours.py` 의 판정과 같은 한계입니다).
휴장일에도 영업일로 세고, 그날은 봉이 없는 것으로 드러납니다.

**휴장일 저녁에는 야간이 안 열립니다.** 휴장일은 주간도 저녁 야간도 0줄이고,
다음 영업일 새벽도 0줄입니다. 그래서 휴장일이 낀 자리는 이렇게 섭니다 — **자료는
안 잃고 조회 둘만 헛돕니다.** 아래는 월요일이 휴장일인 예입니다.

* **휴장일 앞 영업일의 밤 봉은 「휴장일 날짜」 페이지에 붙습니다.** 월요일 페이지의
  창 둘이 금요일 `18:00~23:59` 와 토요일 `00:00~06:00` 이고, 뒤엣것에 금요일 밤
  세션의 새벽 봉이 들어 있습니다 — **금요일 밤 세션이 자정에서 안 잘립니다.**
  그 페이지의 주간 칸만 빕니다. 이름이 휴장일 날짜인 것은 휴장일 달력이 없어
  그 이상 가릴 수가 없기 때문입니다.
* **휴장일 다음 영업일은 야간 창 둘이 빈 답입니다.** 화요일 페이지의 창 둘이
  월요일 `18:00~23:59`(휴장일 저녁 — 안 열림)와 화요일 `00:00~06:00`(그 밤이 없으니
  비어 있음)이라, 조회 둘을 헛씁니다. 빠지는 봉은 없습니다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from backend.sessions.hours import KST, is_weekend

#: 야간이 열리는 시각과 다음 날 닫는 시각. 한 영업일의 야간이 이 사이입니다.
NIGHT_OPEN = time(18, 0)
NIGHT_CLOSE = time(6, 0)

#: 조회에 실을 시각 범위. `HHMMSS` 여섯 자리입니다.
DAY_START = "000000"
DAY_END = "235959"
NIGHT_HEAD_START = "180000"
NIGHT_TAIL_END = "060000"

#: 앞 영업일을 며칠까지 거슬러 찾아보는지. 주말과 연휴를 건너뛸 만큼.
_LOOK_BACK = 10


def as_day(text: str) -> date | None:
    """`YYYYMMDD` 를 날짜로. 모양이 아니면 `None` 입니다."""
    wanted = str(text or "").strip()
    if len(wanted) != 8 or not wanted.isdigit():
        return None
    try:
        return date(int(wanted[:4]), int(wanted[4:6]), int(wanted[6:]))
    except ValueError:
        return None


def as_text(day: date) -> str:
    """날짜를 `YYYYMMDD` 로."""
    return day.strftime("%Y%m%d")


def latest_business_day(now: datetime) -> date:
    """지금이 **어느 영업일의 창 안에** 있는지.

    **18:00 을 넘기면 다음 영업일이 시작된 것입니다** — 그 시각의 봉은 다음
    영업일의 야간 앞부분입니다.

    주말이면 **다음** 영업일입니다. 금요일 저녁 18:00 에 열리는 야간은 월요일
    영업일의 앞부분이고, 토요일 06:00 까지 이어집니다 — 주말에 차트를 열면
    그 반쪽이 이미 있는 자리라 뒤가 아니라 앞을 가리킵니다. 더 과거는 화면이
    `before` 로 한 칸씩 물어 옵니다.
    """
    made = now.astimezone(KST)
    day = made.date()
    if made.time() >= NIGHT_OPEN:
        day = day + timedelta(days=1)
    return day if not is_weekend(day) else next_business_day(day - timedelta(days=1))


def previous_business_day(day: date) -> date:
    """그 날짜 **앞의** 영업일. 주말만 건너뜁니다(휴장일 달력이 없습니다)."""
    made = day - timedelta(days=1)
    for _ in range(_LOOK_BACK):
        if not is_weekend(made):
            return made
        made = made - timedelta(days=1)
    return made


def next_business_day(day: date) -> date:
    """그 날짜 **뒤의** 영업일. 주말만 건너뜁니다."""
    made = day + timedelta(days=1)
    for _ in range(_LOOK_BACK):
        if not is_weekend(made):
            return made
        made = made + timedelta(days=1)
    return made


@dataclass(frozen=True)
class Window:
    """조회 하나 — 어느 달력 날짜의 어느 시각 범위를, 주간으로 부를지 야간으로 부를지.

    `expect` 는 **이 시각부터는 봉이 있어야 한다**는 자리입니다(`HHMMSS`). 비어
    있으면 앞쪽을 못 받았는지 안 봅니다 — 그 창의 첫 봉이 언제여야 하는지 정할
    수 없기 때문입니다.

    주간 창이 그렇습니다. 달력 날짜 전체로 부르고 장이 언제 여는지는 품목마다
    다릅니다(개장이 08:45 인 품목과 09:00 인 품목이 함께 옵니다). 00:00 을 기준으로
    재면 **늘** 「못 받았다」가 되고, 개장 시각을 기준으로 재려면 품목별세부정보가
    있어야 하는데 못 받았을 수도 있습니다.

    야간 앞부분만 `18:00` 을 답니다. 한 응답이 잘린다면 **가장 오래된 쪽**이
    잘리므로, 볼 값어치가 있는 자리도 거기입니다.
    """

    day: date
    start: str
    end: str
    night: bool
    expect: str = ""

    @property
    def date_text(self) -> str:
        return as_text(self.day)


def windows(day: date, *, has_night: bool) -> list[Window]:
    """한 영업일을 모으는 데 필요한 조회들. **시각순**입니다.

    야간이 없는 품목은 주간 하나뿐입니다 — 빈 답이 올 조회를 내지 않습니다.
    시세 조회에는 초당 제한이 있고, 주간만 있는 품목이 훨씬 많습니다.
    """
    made: list[Window] = []
    if has_night:
        opened = previous_business_day(day)
        made.append(
            Window(opened, NIGHT_HEAD_START, DAY_END, night=True, expect=NIGHT_HEAD_START)
        )
        # **야간 뒷부분은 「열린 날의 다음 날」입니다.** 평일은 그것이 이 영업일과
        # 같지만 월요일은 다릅니다 — 금요일 18:00 에 열린 야간은 **토요일**
        # 06:00 에 닫힙니다. 이 영업일 날짜로 부르면 월요일 새벽을 묻게 되고,
        # 일요일 밤 세션이 없어 늘 빈 답이 옵니다.
        made.append(
            Window(opened + timedelta(days=1), DAY_START, NIGHT_TAIL_END, night=True)
        )
    made.append(Window(day, DAY_START, DAY_END, night=False))
    return made


def span(day: date) -> tuple[int, int]:
    """그 영업일이 담는 시각의 처음과 끝(밀리초).

    **앞 영업일 18:00 부터 그날 18:00 직전까지**입니다. 뒤를 주간 마감(15:45)이
    아니라 18:00 으로 두는 것은, 최종거래일처럼 마감이 이른 날에도 그날 봉을 다
    담기 위해서입니다 — 그리고 그날 18:00 부터는 **다음** 영업일의 야간이라 그
    자리에서 끊어야 두 영업일이 한 덩어리로 붙지 않습니다.
    """
    opened = datetime.combine(previous_business_day(day), NIGHT_OPEN, KST)
    closed = datetime.combine(day, NIGHT_OPEN, KST)
    return int(opened.timestamp() * 1000), int(closed.timestamp() * 1000) - 1


def assemble(pieces: list[dict[str, Any]], day: date) -> list[dict[str, Any]]:
    """조회 몇 벌을 한 영업일의 봉 한 줄로. **그 영업일 밖의 봉은 버립니다.**

    야간 조회는 달력 날짜로 잘려 오므로 옆 영업일의 반쪽이 함께 옵니다. 시각으로
    잘라야 한 영업일이 한 덩어리가 됩니다.

    같은 시각의 봉이 두 벌에 다 있으면 **뒤엣것**이 남습니다 — 세션 경계의 한 봉이
    두 조회에 함께 실려 오는 경우입니다.
    """
    first, last = span(day)
    by_time: dict[int, dict[str, Any]] = {}
    for piece in pieces:
        for bar in piece.get("bars") or []:
            stamp = int(bar["timestamp"])
            if first <= stamp <= last:
                by_time[stamp] = bar
    return [by_time[stamp] for stamp in sorted(by_time)]


def short(piece: dict[str, Any], window: Window) -> bool:
    """이 조회가 **창의 앞쪽을 못 받았을 수 있는지.**

    받은 첫 봉이 요청한 시작보다 늦으면 참입니다. 한 응답이 몫을 다 내지 못한
    자리를 이것으로 알아챕니다 — 이어받기 키는 믿을 수 없습니다(같은 키로 다시
    불러도 같은 줄이 옵니다).

    **거래가 없어 늦게 시작한 것과 구별하지 못합니다.** 조용한 종목은 18:00 에
    첫 체결이 없을 수 있고, 그때도 참이 됩니다. 그래서 이 값은 「잘렸다」가 아니라
    **「앞쪽을 다 받았다고 보증하지 못한다」**는 뜻입니다.

    첫 봉이 언제여야 하는지 모르는 창(`expect` 가 빈 창)은 안 봅니다 — 까닭은
    `Window` 에.
    """
    rows = piece.get("bars") or []
    if not rows or not window.expect:
        return False
    return int(rows[0]["timestamp"]) > _stamp_of(window.day, window.expect)


def _stamp_of(day: date, clock: str) -> int:
    made = datetime.combine(
        day, time(int(clock[:2]), int(clock[2:4]), int(clock[4:])), KST
    )
    return int(made.timestamp() * 1000)
