"""시간 구간 읽기와 한국시간.

NH선물 시간표는 `08:45~15:45`·`18:00~익일06:00`(국내)·`07:00 ~ 06:00`(해외)처럼
글자로 옵니다. 여기서 한 모양으로 읽습니다.

    {"open": "08:45", "close": "06:00", "nextDay": true}

`nextDay` 는 닫는 시각이 여는 시각보다 앞이면 참입니다 — 자정을 넘는 구간입니다.
국내는 「익일」이 글자로 붙어 오고 해외는 안 붙어 오는데, 어느 쪽이든 시각 순서로
같은 답이 나오므로 글자에 기대지 않습니다.

**시간대는 한국시간 하나입니다.** 해외 시간표도 NH선물이 한국시간으로(서머타임을
반영해서) 주므로 거래소 현지 시간을 셈할 일이 없습니다. 한국은 서머타임이 없어
고정 오프셋으로 충분하고, 그래서 시간대 자료가 없는 환경에서도 돕니다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

KST = timezone(timedelta(hours=9), "KST")

_RANGE = re.compile(
    r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*~\s*(?:익일)?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$"
)


@dataclass(frozen=True)
class TimeRange:
    """하루 안의 시간 구간. 자정을 넘으면 `next_day` 가 참입니다."""

    open: time
    close: time

    @property
    def next_day(self) -> bool:
        return self.close < self.open

    @property
    def is_point(self) -> bool:
        """여는 시각과 닫는 시각이 같은 한 점. 정산가 산정시간이 이렇게 옵니다."""
        return self.close == self.open

    def as_dict(self) -> dict[str, Any]:
        return {
            "open": _text(self.open),
            "close": _text(self.close),
            "nextDay": self.next_day,
        }

    def starting(self, day: date) -> tuple[datetime, datetime]:
        """`day` 에 여는 구간의 (시작, 끝) 시각."""
        start = datetime.combine(day, self.open, KST)
        end = datetime.combine(day + timedelta(days=1 if self.next_day else 0), self.close, KST)
        return start, end

    def contains(self, moment: datetime, day: date) -> bool:
        """`day` 에 연 구간 안에 `moment` 가 드는지. 끝 시각은 안 듭니다."""
        start, end = self.starting(day)
        return start <= moment < end


def _text(value: time) -> str:
    return value.strftime("%H:%M:%S") if value.second else value.strftime("%H:%M")


def time_range(text: Any) -> TimeRange | None:
    """`HH:MM~HH:MM` 꼴 글자를 구간으로. 비었거나 못 읽으면 `None`.

    `00:00 ~ 00:00` 은 해외 시간표가 「없음」을 적는 방식이라 `None` 입니다. 그 밖에
    두 시각이 같은 것은 한 점입니다(`17:35:00 ~ 17:35:00` 정산가 산정 시각).
    """
    found = _RANGE.match(str(text or ""))
    if found is None:
        return None
    h1, m1, s1, h2, m2, s2 = found.groups()
    try:
        opened = time(int(h1), int(m1), int(s1 or 0))
        closed = time(int(h2), int(m2), int(s2 or 0))
    except ValueError:
        return None
    if opened == closed == time(0, 0):
        return None
    return TimeRange(opened, closed)


def range_dict(text: Any) -> dict[str, Any] | None:
    """화면에 내보낼 모양. 없으면 `None`."""
    made = time_range(text)
    return made.as_dict() if made is not None else None


def from_dict(value: Any) -> TimeRange | None:
    """`range_dict` 가 낸 모양을 다시 구간으로. 받아 둔 시간표를 판정이 읽을 때 씁니다."""
    if not isinstance(value, dict):
        return None
    try:
        return TimeRange(
            time.fromisoformat(str(value["open"])), time.fromisoformat(str(value["close"]))
        )
    except (KeyError, ValueError, TypeError):
        return None


def now_kst() -> datetime:
    """지금, 한국시간. 컴퓨터의 시간대 설정과 무관합니다."""
    return datetime.now(tz=KST)


def to_kst(moment: datetime) -> datetime:
    """시각을 한국시간으로. 시간대가 없는 값은 한국시간으로 봅니다."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=KST)
    return moment.astimezone(KST)


def iso(moment: datetime | None) -> str | None:
    """화면에 내보낼 시각 글자. `2026-09-16T08:45:00+09:00`."""
    return None if moment is None else to_kst(moment).isoformat(timespec="seconds")


def is_weekend(day: date) -> bool:
    return day.weekday() >= 5
