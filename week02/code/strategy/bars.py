"""체결을 봉으로 세웁니다.

실행 하나가 보는 봉은 **두 군데에서 옵니다.** 시작할 때는 지표가 설 만큼을
분봉 조회로 받아 깔고, 그 뒤로는 실시간 체결을 이어 붙입니다. 두 벌이 한 줄로
이어져야 차트에 그려진 선과 엔진이 본 값이 같습니다.

## 세션은 허브가 붙여 줍니다

봉은 **세션 창으로 가릅니다** — 앞 창의 마지막 봉과 다음 창의 첫 봉을 이어
붙이지 않습니다. 창을 여기서 다시 계산하지 않고 허브가 메시지마다 붙여 주는
맥락(`domestic-day`·`domestic-night`·`overseas-cme`)을 그대로 씁니다. 같은
경계를 두 곳에서 세면 한쪽만 고쳐지는 날 봉이 화면과 달라집니다.

**야간은 별도 세션입니다.** 야간으로 도는 실행은 야간 봉만 판정하고 주간 봉은
지표를 세우는 데만 씁니다. 그래서 봉과 나란히 맥락을 들고 다닙니다
(`sessions`) — 판정할 봉을 고르는 것은 엔진의 몫입니다.

## 비는 분은 봉이 아닙니다

체결이 없는 동안은 봉을 만들지 않습니다(차트와 같은 규칙). 유효기간 「N봉
이내」는 **실제로 생긴 봉**으로 셉니다 — 한산한 구간에서 시계로 세면 조건이
채워질 기회 없이 만료됩니다.

봉이 **닫히는** 때는 둘입니다. 다음 봉 자리의 첫 체결이 오거나(`add`), 세션이
끝나거나 시세가 끊기거나(`close`). 닫힌 봉만 판정에 씁니다 — 아직 값이 바뀌는
봉으로 신호를 내면 같은 봉에서 신호가 섰다 사라집니다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from backend.sessions.hours import KST, now_kst
from backend.strategy.indicators import Bar

#: 화면의 `BASE_BARS` 와 같은 목록. 분봉은 길이(분), 일봉은 따로입니다.
BAR_MINUTES: dict[str, int] = {
    "1분봉": 1,
    "5분봉": 5,
    "15분봉": 15,
    "30분봉": 30,
    "60분봉": 60,
}

#: 일봉. 길이가 분이 아니라 **거래일 하나**입니다.
DAILY_BAR = "일봉"

#: 국내 야간. 이 창의 체결은 **다음 거래일**에 속합니다(세션 표와 같은 규칙).
NIGHT_CONTEXT = "domestic-night"

_MINUTE_MS = 60_000
_DAY_MS = 86_400_000


def minutes_of(base_bar: str) -> int | None:
    """그 기준 봉의 길이(분). 일봉이면 `None` 입니다."""
    return BAR_MINUTES.get(str(base_bar or "").strip())


def slot_of(timestamp: int, minutes: int) -> int:
    """그 체결이 드는 분봉의 시작 시각(밀리초).

    한국시간의 정각에서 봉 길이만큼씩 끊습니다 — 15분봉이면 :00·:15·:30·:45.
    """
    moment = datetime.fromtimestamp(timestamp / 1000, tz=KST)
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    passed = int((moment - midnight).total_seconds() // 60)
    start = midnight + timedelta(minutes=passed - passed % max(minutes, 1))
    return int(start.timestamp() * 1000)


def day_slot_of(timestamp: int, context: str) -> int:
    """그 체결이 드는 **거래일**의 0시(밀리초).

    국내 야간은 다음 날에 속합니다. 휴장일을 건너뛰지 않습니다 — 봉을 세우는
    데 쓰는 자리라 하루 앞뒤가 달라도 봉이 섞이지 않고, 달력 날짜만으로도
    야간과 그 다음 주간이 한 봉에 들어옵니다.
    """
    moment = datetime.fromtimestamp(timestamp / 1000, tz=KST)
    day: date = moment.date()
    if context == NIGHT_CONTEXT and moment.hour >= 12:
        # 18:00 에 열어 자정을 넘기는 창입니다. 자정 뒤의 체결은 이미 다음 날입니다.
        day = day + timedelta(days=1)
    start = datetime(day.year, day.month, day.day, tzinfo=KST)
    return int(start.timestamp() * 1000)


class BarBuilder:
    """실행 하나가 보는 봉 한 줄.

    받는 체결은 허브가 내는 모양 그대로입니다 — `{"timestamp", "price",
    "quantity"}`. 시각이 없는 체결은 어느 봉에 넣을지 고를 수 없어 버리고
    세어 둡니다(`dropped`).

    **체결은 시각 순으로 들어온다고 봅니다.** 허브가 받은 차례 그대로 넘겨
    주므로 실시간에서는 그렇습니다. 앞뒤가 뒤섞인 체결을 넣으면 지난 자리에
    봉이 새로 서서 봉 목록이 시각 순이 아니게 됩니다 — 기록 둘을 이어 붙여
    흘려 넣을 때가 그렇습니다.
    """

    def __init__(self, base_bar: str, *, limit: int = 2000) -> None:
        self.base_bar = str(base_bar or "").strip()
        self.minutes = minutes_of(self.base_bar)
        #: 담아 둘 봉 수. 지표가 보는 것은 앞쪽 몇백 봉뿐이라 무한정 들지 않습니다.
        self.limit = max(int(limit), 1)
        self.bars: list[Bar] = []
        #: 봉마다의 맥락. `bars` 와 길이가 같습니다.
        self.sessions: list[str] = []
        self.dropped = 0
        self._open: Bar | None = None
        self._open_session = ""
        self._slot = 0

    # ── 채우기 ──────────────────────────────────────────

    def seed(self, rows: list[dict[str, Any]], context: str) -> set[int]:
        """분봉 조회로 받은 봉을 **시각으로 맞물려** 넣고, 읽은 봉의 시각들을 냅니다.

        앞만 채우지 않습니다 — 끊겼다 다시 붙으면 빈 자리가 앞이 아니라
        **가운데나 뒤**에 생깁니다(그동안 실시간이 안 왔고 그 뒤로 다시 오기
        시작하므로). 앞만 채우면 그 구멍이 영영 안 메워집니다.

        같은 자리가 겹치면 **조회 봉이 이깁니다.** 거래소가 적어 둔 것이고,
        이 앱이 실시간으로 세운 봉은 그 구간을 놓쳤을 수 있어 거래량이 모자랄
        수 있습니다. 아직 안 닫힌 봉은 여기 없으므로 그대로 자랍니다.

        **조회 봉에는 제 창이 없습니다.** 응답은 그 구간의 값일 뿐 어느 창에서
        왔는지 말해 주지 않으므로, 부르는 쪽이 준 창(`context`)을 적습니다 —
        실행이 그 창으로 물었으니 그 창의 봉입니다. 일봉이면 창으로 가리지
        않으므로 무엇이 적히든 판정에는 영향이 없습니다(`judged`).
        """
        held = dict(zip((one.timestamp for one in self.bars), self.sessions))
        made = {one.timestamp: one for one in self.bars}
        read: set[int] = set()
        for row in rows:
            bar = _bar_of(row)
            if bar is None:
                self.dropped += 1
                continue
            made[bar.timestamp] = bar
            held[bar.timestamp] = context
            read.add(bar.timestamp)

        self.bars = [made[when] for when in sorted(made)]
        self.sessions = [held[one.timestamp] for one in self.bars]
        self._trim()
        return read

    @property
    def daily(self) -> bool:
        """일봉인지. **일봉은 창으로 가르지 않습니다** — 까닭은 `add` 에."""
        return self.minutes is None

    def add(self, trade: dict[str, Any], context: str) -> list[Bar]:
        """체결 하나를 넣고 **그 때문에 닫힌 봉**을 냅니다.

        닫히는 봉은 많아야 하나입니다. 같은 자리면 열린 봉을 키우고, 자리가
        바뀌거나 세션이 바뀌면 앞의 것을 닫고 새로 엽니다.

        **일봉은 창이 바뀌어도 안 닫습니다.** 「일봉 = 거래일 하나」이고 국내
        야간은 다음 거래일에 속하므로, 한 거래일의 야간과 그 다음 주간이 **한
        봉**입니다. 창으로 가르면 야간 있는 품목의 하루가 늘 두 조각이 되어
        지표가 하루를 두 번 셉니다.
        """
        timestamp = _int(trade.get("timestamp"))
        price = _float(trade.get("price"))
        if timestamp is None or price is None:
            self.dropped += 1
            return []
        volume = float(_int(trade.get("quantity")) or 0)

        slot = self._slot_for(timestamp, context)
        closed: list[Bar] = []
        moved = slot != self._slot or (not self.daily and context != self._open_session)
        if self._open is not None and moved:
            closed = self.close()

        if self._open is None:
            self._open = Bar(
                timestamp=slot, open=price, high=price, low=price, close=price, volume=volume
            )
            self._open_session = context
            self._slot = slot
            return closed

        held = self._open
        self._open = Bar(
            timestamp=held.timestamp,
            open=held.open,
            high=max(held.high, price),
            low=min(held.low, price),
            close=price,
            volume=held.volume + volume,
        )
        return closed

    def close(self) -> list[Bar]:
        """열린 봉을 닫습니다. 세션이 끝났거나 시세가 끊겼을 때 부릅니다.

        열린 봉이 없으면 아무 일도 없습니다 — 세션 끝 신호가 두 번 와도
        빈 봉이 서지 않습니다.
        """
        if self._open is None:
            return []
        made = self._open
        self.bars.append(made)
        self.sessions.append(self._open_session)
        self._open = None
        self._open_session = ""
        self._trim()
        return [made]

    # ── 보기 ────────────────────────────────────────────

    @property
    def open_bar(self) -> Bar | None:
        """아직 닫히지 않은 봉. 값이 더 바뀝니다 — 평가 방식이 「틱」인 실행만 판정에 씁니다."""
        return self._open

    @property
    def open_session(self) -> str:
        """열린 봉의 세션(맥락). 열린 봉이 없으면 빈 글자입니다."""
        return self._open_session

    def ready(self, need: int) -> bool:
        """지표가 설 만큼 모였는지."""
        return len(self.bars) >= need

    def judged(self, session: str) -> list[int]:
        """판정할 봉들이 앉은 자리.

        분봉은 **그 창의 봉**만입니다. 일봉은 거래일이 단위라 창으로 가르지
        않으므로 전부입니다 — 봉에 적힌 창은 그 거래일의 **첫 체결이 온 창**
        이고, 「언제 시작했나」를 남겨 두는 값입니다.
        """
        if self.daily:
            return list(range(len(self.bars)))
        return [at for at, one in enumerate(self.sessions) if one == session]

    def _slot_for(self, timestamp: int, context: str) -> int:
        if self.minutes is None:
            return day_slot_of(timestamp, context)
        return slot_of(timestamp, self.minutes)

    def _trim(self) -> None:
        if len(self.bars) <= self.limit:
            return
        cut = len(self.bars) - self.limit
        self.bars = self.bars[cut:]
        self.sessions = self.sessions[cut:]


def minute_span(count: int, minutes: int, *, now: datetime | None = None) -> tuple[str, str]:
    """분봉 `count` 개를 받으려면 언제부터 언제까지를 물어야 하는지 `(시작, 끝)`.

    날짜는 `YYYYMMDD` 입니다. **넉넉히 잡습니다** — 비는 분은 봉이 되지 않아
    시계로 센 구간에는 늘 봉이 모자랍니다. 하루에 설 수 있는 봉이 몇인지는
    품목마다 달라, 세지 않고 **하루치를 통째로** 며칠 받습니다.
    """
    moment = now or now_kst()
    per_day = max(int(_DAY_MS / (minutes * _MINUTE_MS)), 1)
    days = max(int(count / per_day) + 2, 2)
    start = (moment - timedelta(days=days)).strftime("%Y%m%d")
    return start, moment.strftime("%Y%m%d")


def day_span(count: int, *, now: datetime | None = None) -> tuple[str, str]:
    """일봉 `count` 개를 받으려면 언제부터 언제까지를 물어야 하는지 `(시작, 끝)`.

    **넉넉히 잡습니다** — 주말과 휴장일에는 봉이 서지 않아 달력으로 센 구간에는
    늘 봉이 모자랍니다. 닷새에 사흘 꼴로 보고 이틀을 더 얹습니다.
    """
    moment = now or now_kst()
    days = max(int(count * 5 / 3) + 2, 3)
    start = (moment - timedelta(days=days)).strftime("%Y%m%d")
    return start, moment.strftime("%Y%m%d")


def _bar_of(row: Any) -> Bar | None:
    if not isinstance(row, dict):
        return None
    timestamp = _int(row.get("timestamp"))
    close = _float(row.get("close"))
    if timestamp is None or close is None:
        return None
    high = _float(row.get("high"))
    low = _float(row.get("low"))
    opened = _float(row.get("open"))
    return Bar(
        timestamp=timestamp,
        open=opened if opened is not None else close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close,
        volume=float(_int(row.get("volume")) or 0),
    )


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        made = float(value)
    except (TypeError, ValueError):
        return None
    return made
