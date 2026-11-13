"""종목 마스터와 거래시간표를 하루 한 번 새로 받습니다.

앱은 뜰 때 마스터와 시간표를 받습니다. 데스크톱 앱은 보통 매일 새로 켜므로 그것으로
충분하고, 이 작업은 **며칠씩 켜 두는 서버를 위한 것**입니다. 켜 둔 채 날이 바뀌면
만기가 지난 종목이 목록에 남고 새로 상장된 종목이 빠집니다.

**언제 받나.** 평일 한국시간 07:20 에 한 번 가 봅니다. 거래시간표가 새로워지는
07:00(`backend/sessions/service.py`) 뒤이고, 국내 주간장(08:45) 전입니다.

**받은 것이 오늘 것인지 확인합니다.** NH선물이 그날 마스터를 정확히 몇 시에 올리는지는
공개된 기준이 없습니다. 이른 시각에 받으면 전날 파일을 받을 수 있으므로, 받은 국내
마스터의 배포일(`master_service.published_on`)이 오늘인지 보고, 아니면 30분 뒤에 다시
가 봅니다. 12:00 까지 오늘 것을 못 받으면 그날은 그만두고 다음 평일에 다시 합니다.
지금 쓰는 목록이 이미 오늘 것이면(오늘 켠 경우) 받지 않습니다.

**받는 동안에도 화면과 전략은 그대로 돕니다.** 새 색인은 다 만든 뒤에 바꿔
끼우므로 종목 조회가 잠깐이라도 「없음」으로 답하지 않습니다(`MasterService`).

**저장해 둔 응답으로 도는 중에는 마스터를 받으러 나가지 않습니다.** 앱이 뜰 때와
같은 규칙입니다.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from backend.master import master_service
from backend.master.master_service import MasterService
from backend.nh_api import runtime as nh_runtime
from backend.sessions import service as session_service
from backend.sessions.hours import KST, iso, now_kst

logger = logging.getLogger(__name__)

#: 하루의 첫 시도 시각(한국시간).
RUN_AT = time(7, 20)

#: 오늘 것을 못 받았을 때 다시 가 보기까지의 시간.
RETRY_EVERY = timedelta(minutes=30)

#: 이 시각(한국시간)이 지나면 그날은 더 가 보지 않습니다.
GIVE_UP_AT = time(12, 0)

#: 기다리는 동안 시계를 다시 보는 간격(초). 컴퓨터가 잠들었다 깨어나도 예정 시각을
#: 크게 넘기지 않게 짧게 끊어 잡니다.
POLL_SECONDS = 60.0


class DailyRefresh:
    """하루 한 번 갱신의 예정과 결과를 들고 있습니다."""

    def __init__(
        self,
        master: MasterService,
        *,
        refresh_sessions: Callable[[], None],
        replaying: Callable[[], bool],
        clock: Callable[[], datetime] = now_kst,
    ) -> None:
        self._master = master
        self._refresh_sessions = refresh_sessions
        self._replaying = replaying
        self._clock = clock
        #: 갱신을 끝낸(또는 그만둔) 날. 그날은 다시 가지 않습니다.
        self._settled: date | None = None
        #: 오늘 다시 가 볼 시각. 실패한 뒤에만 채워집니다.
        self._retry_at: datetime | None = None

    # ── 예정 ────────────────────────────────────────────────

    def next_at(self, now: datetime | None = None) -> datetime:
        """다음에 가 볼 시각(한국시간). 이미 지났으면 지금 가야 하는 것입니다."""
        local = (now or self._clock()).astimezone(KST)
        today = local.date()
        if self._open(today):
            first = datetime.combine(today, RUN_AT, KST)
            due = max(first, self._retry_at) if self._retry_at else first
            if due < datetime.combine(today, GIVE_UP_AT, KST):
                return due
        return datetime.combine(_next_weekday(today), RUN_AT, KST)

    def _open(self, today: date) -> bool:
        """오늘 아직 할 일이 남았는지."""
        if today.weekday() >= 5 or self._settled == today:
            return False
        published = self._master.published
        return published is None or published < today

    def status(self, now: datetime | None = None) -> dict[str, object]:
        """`/api/master/status` 에 더해 내는 칸."""
        return {"next_refresh_at": iso(self.next_at(now))}

    # ── 실행 ────────────────────────────────────────────────

    def tick(self, now: datetime | None = None) -> bool:
        """예정 시각이 지났으면 한 번 받습니다. 받으러 갔으면 `True`.

        통신하고 파일을 읽는 일이라 **별도 스레드에서 부르세요.** 어떤 실패도 밖으로
        올리지 않습니다 — 이 작업이 멈추면 다음 날에도 받지 못합니다.
        """
        local = (now or self._clock()).astimezone(KST)
        if local < self.next_at(local):
            return False
        today = local.date()
        try:
            self._attempt(local)
        except Exception:
            logger.exception("종목 마스터를 새로 받는 중에 문제가 생겼습니다.")
            self._failed(local)
            return True
        # 「다시 받기」가 먼저 받고 있어 이번 차례를 건너뛴 경우도 여기서 가립니다 —
        # 그쪽이 오늘 것을 받았으면 성공이고, 아니면 다시 가 봅니다.
        published = self._master.published
        if published is not None and published >= today:
            self._settled = today
            self._retry_at = None
            logger.info("종목 마스터를 오늘(%s) 것으로 바꿨습니다.", today.isoformat())
        elif self._settled == today:
            self._retry_at = None
        else:
            self._failed(local)
        return True

    def _attempt(self, local: datetime) -> None:
        try:
            self._refresh_sessions()
        except Exception:
            # 시간표는 제 자리에서 받아 둔 것을 쓰고 기록도 남깁니다. 마스터는 따로 받습니다.
            logger.exception("거래시간표를 새로 받지 못했습니다.")
        if self._replaying():
            logger.info("저장해 둔 응답으로 도는 중이라 종목 마스터를 새로 받지 않습니다.")
            self._settled = local.date()
            return
        fetched = self._master.refresh(force=True)
        if fetched and not any(item.ok for item in fetched):
            return
        if fetched and self._master.published is None:
            # 국내 마스터가 없어 배포일을 알 수 없습니다. 받은 것을 그대로 씁니다.
            self._settled = local.date()

    def _failed(self, local: datetime) -> None:
        """오늘 것을 못 받았을 때. 시간이 남았으면 다시 가 보고, 아니면 내일로 넘깁니다."""
        retry = local + RETRY_EVERY
        if retry < datetime.combine(local.date(), GIVE_UP_AT, KST):
            self._retry_at = retry
            logger.info("오늘 종목 마스터를 아직 받지 못했습니다. %s 에 다시 받아 봅니다.", iso(retry))
            return
        self._settled = local.date()
        self._retry_at = None
        logger.warning(
            "오늘(%s) 종목 마스터를 받지 못했습니다. 받아 둔 목록(배포일 %s)을 그대로 쓰고 다음 평일에 다시 받습니다.",
            local.date().isoformat(),
            self._master.published.isoformat() if self._master.published else "모름",
        )

    async def run(self) -> None:
        """예정 시각마다 받습니다. 서버가 내려갈 때 취소됩니다."""
        while True:
            now = self._clock()
            wait = (self.next_at(now) - now).total_seconds()
            if wait > 0:
                await asyncio.sleep(min(wait, POLL_SECONDS))
                continue
            await asyncio.to_thread(self.tick, now)


def _next_weekday(day: date) -> date:
    following = day + timedelta(days=1)
    while following.weekday() >= 5:
        following += timedelta(days=1)
    return following


#: 앱이 쓰는 하나. 시험은 `DailyRefresh(...)` 를 따로 만들어 가짜 시계와 받기를 넣으세요.
daily = DailyRefresh(
    master_service.service,
    refresh_sessions=lambda: session_service.service.refresh_all(),
    replaying=lambda: nh_runtime.replaying(),
)
