"""호출 제한을 지키기 위해 요청 사이를 벌립니다.

NH선물 Open API 는 등급마다 초당 허용 건수가 정해져 있고, 넘기면 차단됩니다.
여기서는 초당 건수를 간격으로 바꿔서(초당 10건이면 0.1초) 요청이 그보다
촘촘하게 나가지 않도록 붙잡아 둡니다. 한꺼번에 몰아 보내고 쉬는 방식보다
간격을 지키는 쪽이 안전합니다.

**세는 단위는 계좌 하나입니다.** 제한이 계좌마다 걸리므로 다른 계좌의 요청은
서로의 몫을 깎지 않습니다.

계좌를 가리키는 것은 **환경과 시장** 둘입니다. 모의와 실전은 서로 다른
서버이고, 국내와 해외는 APP Key 가 따로 발급되는 서로 다른 계좌입니다.
그래서 버킷을 환경·시장·등급 셋으로 나눕니다. 국내와 해외를 한 통으로 세면
두 계좌가 각각 쓸 수 있는 몫의 절반씩만 쓰게 됩니다.

기다리는 동안 스레드가 멈추므로, 이벤트 루프 안에서 직접 부르지 말고
`asyncio.to_thread` 처럼 별도 스레드에서 부르세요.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

from backend.nh_api.catalog import LIMIT_PER_SECOND, Limit


class RateLimiter:
    """등급별로 요청 간격을 지킵니다."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        # 시계와 대기를 바꿔 끼울 수 있게 받아 둡니다. 테스트에서 실제로
        # 기다리지 않고도 간격이 지켜지는지 확인할 수 있습니다.
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_allowed: dict[tuple[str, str, Limit], float] = {}

    @staticmethod
    def interval(limit: Limit) -> float:
        """이 등급에서 요청 사이에 두어야 할 최소 간격(초)."""
        return 1.0 / LIMIT_PER_SECOND[limit]

    def wait(self, environment: str, market: str, limit: Limit) -> float:
        """다음 요청을 보내도 될 때까지 기다립니다. 기다린 시간(초)을 돌려줍니다.

        환경과 시장이 계좌 하나를 가리킵니다. 둘 중 하나라도 다르면 다른
        계좌이므로 서로 기다리지 않습니다.
        """
        interval = self.interval(limit)
        key = (environment, market, limit)

        with self._lock:
            now = self._monotonic()
            allowed_at = self._next_allowed.get(key, now)
            delay = max(0.0, allowed_at - now)
            # 다음 요청은 이번 요청을 보내는 시각 기준으로 한 칸 뒤입니다.
            self._next_allowed[key] = max(allowed_at, now) + interval

        if delay > 0:
            self._sleep(delay)
        return delay

    def reset(self) -> None:
        """지금까지의 간격 기록을 버립니다."""
        with self._lock:
            self._next_allowed.clear()
