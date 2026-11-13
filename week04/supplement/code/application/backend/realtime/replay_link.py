"""저장해 둔 실시간 기록으로 화면을 돌립니다.

서버에 붙지 않고 저장해 둔 메시지를 순서대로 흘려보냅니다. 연결도 접속키도
없으므로 이 파일에는 재접속이 없습니다.

**한 번 흘리고 끝냅니다.** 기록이 15초 남짓이라 다 흘리면 멈춥니다. 되감아
반복하면 누적 거래량이 거꾸로 가고 체결 목록에 같은 체결이 계속 쌓여서, 화면이
이상한지 자료가 이상한지 가릴 수 없게 됩니다. 그래서 끝나면 **끝났다고
알립니다.** 값이 멈춘 것과 자료가 떨어진 것은 화면에서 갈려야 합니다.

기록이 없는 구독은 **없다고 알립니다.** 야간 시세와 주문/체결 알림처럼 구독
답만 있고 자료가 한 건도 안 담긴 기록이 있어서, 화면이 「받는 중」과 「받을
것이 없음」을 가릴 수 있어야 합니다. 기록에 없는 종목도 같습니다 — 다른 종목의
기록으로 대신 흘리지 않고 그 종목이 비었다고 알립니다. 알림에는 종목을 실어
화면이 어느 자리가 빈 것인지 알게 합니다.
"""
from __future__ import annotations

import asyncio
import logging

from backend.nh_api.fixtures import FixtureStore
from backend.nh_api.replay import ReplayMiss, stream_events
from backend.realtime.feeds import Feed
from backend.realtime.ledger import StreamKey
from backend.realtime.link import LIVE, STOPPED, OnData, OnState

logger = logging.getLogger(__name__)

REPLAY_FINISHED = "replay-finished"
REPLAY_EMPTY = "replay-empty"

# 수집했을 때의 간격을 그대로 둡니다. 값이 실제와 비슷한 속도로 움직여야
# 화면이 그 속도를 견디는지 볼 수 있습니다.
DEFAULT_SPEED = 1.0


class ReplayLink:
    """저장해 둔 기록을 흘려보냅니다. `Connection` 과 같은 자리에 끼웁니다."""

    def __init__(
        self,
        *,
        market: str,
        environment: str,
        store: FixtureStore,
        on_data: OnData,
        on_state: OnState,
        speed: float = DEFAULT_SPEED,
    ) -> None:
        self.market = market
        self.environment = environment
        self._store = store
        self._on_data = on_data
        self._on_state = on_state
        self._speed = speed

        self._feeds: dict[StreamKey, Feed] = {}
        self._tasks: dict[StreamKey, asyncio.Task[None]] = {}
        self._state = STOPPED
        self._running = False

    @property
    def state(self) -> str:
        return self._state

    @property
    def keys(self) -> frozenset[StreamKey]:
        return frozenset(self._feeds)

    def __len__(self) -> int:
        return len(self._feeds)

    def start(self) -> None:
        self._running = True
        self._set_state(LIVE, "저장해 둔 실시간 자료로 돌고 있습니다.")

    async def stop(self) -> None:
        self._running = False
        tasks = list(self._tasks.values())
        self._tasks.clear()
        self._feeds.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._set_state(STOPPED, "")

    async def add(self, key: StreamKey, feed: Feed) -> None:
        if key in self._feeds:
            return
        self._feeds[key] = feed
        self._tasks[key] = asyncio.create_task(self._flow(key, feed))

    async def remove(self, key: StreamKey) -> None:
        self._feeds.pop(key, None)
        task = self._tasks.pop(key, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def refresh(self, key: StreamKey) -> None:
        """다시 걸기. 저장본은 서버 쪽 등록이 없어 끊길 일이 없으므로 할 일이 없습니다."""
        return None

    def _set_state(self, state: str, detail: str) -> None:
        if state == self._state:
            return
        self._state = state
        self._on_state(self.market, state, detail)

    async def _flow(self, key: StreamKey, feed: Feed) -> None:
        """구독 하나 몫의 기록을 흘려보냅니다."""
        try:
            events = stream_events(
                self._store,
                feed.api_id,
                environment=self.environment,
                market=feed.market,
                action_sym=feed.sym or None,
                speed=self._speed,
            )
            sent = 0
            async for payload in events:
                if not isinstance(payload, dict):
                    continue
                body = payload.get("body")
                # 구독 확인 같은 제어 메시지가 섞여 옵니다. 자료에만 `body` 가
                # 있습니다.
                if isinstance(body, dict):
                    self._on_data(key, body)
                    sent += 1
        except ReplayMiss:
            self._note(feed, REPLAY_EMPTY, "저장해 둔 자료에 이 종목의 기록이 없습니다.")
            return
        except asyncio.CancelledError:
            raise
        else:
            if sent == 0:
                self._note(feed, REPLAY_EMPTY, "저장해 둔 기록에 자료 메시지가 없습니다.")
            else:
                self._note(feed, REPLAY_FINISHED, "저장해 둔 실시간 자료가 끝났습니다.")
        finally:
            self._tasks.pop(key, None)

    def _note(self, feed: Feed, state: str, detail: str) -> None:
        if not self._running:
            return
        # 연결 상태가 아니라 구독 하나에 대한 알림이라 `_set_state` 를 거치지
        # 않습니다. 같은 연결의 다른 구독은 계속 돌고 있습니다.
        self._on_state(self.market, state, detail, feed.sym)
