"""실시간 허브 — NH선물에서 받아 화면으로 넘깁니다.

브라우저는 NH선물 실시간 서버에 직접 붙지 못합니다. 붙으려면 접속키가 필요한데
그 키는 APP Key 로 발급받는 것이고, APP Key 는 백엔드에만 있습니다. 화면에
내려보내면 브라우저 안에 인증 정보가 놓입니다. 그래서 **백엔드가 받아서
화면으로 넘기는 2단**입니다.

    화면 여럿 ──/api/stream── [허브] ──wss── NH선물 (계좌마다 하나)

허브가 지는 것은 넷입니다.

1. **연결** — 계좌마다 하나. 접속키 발급, 세션 초기화, Ping-Pong, 재접속 (`link`)
2. **장부** — 누가 무엇을 보고 있는지, 스무 자리를 어떻게 나눌지 (`ledger`)
3. **모으기** — 오는 대로 넘기지 않고 100밀리초씩 모아서 (`coalesce`)
4. **나눠 주기** — 그 구독을 원한 화면에만

## 화면이 주고받는 말

화면 → 허브. 하나뿐입니다.

    {"op": "declare", "feeds": [{"context": "domestic-day", "kind": "quote",
                                 "sym": "A7569000", "priority": 1}, ...]}

**늘리고 줄이는 것이 아니라 지금 보고 있는 것 전체**를 보냅니다. 왜 그런지는
`ledger` 첫머리에 있습니다.

허브 → 화면.

    {"type": "quote", "context", "sym", "patch": {...}, "trades": [...]}
    {"type": "book",  "context", "sym", "asks": [...], "bids": [...], "time", "expectedPrice"?}
    {"type": "orders", "market", "events": [...]}
    {"type": "subscriptions", "granted": [...], "rejected": [...], "failed": [...]}
    {"type": "status", "market", "state", "detail", "refill"?, "sym"?}
    {"type": "environment", "active": "paper" | "real"}
    {"type": "session", "market": "domestic", "state", "session", "since", "until", "nextOpen"}

`patch` 는 **`Quote` 전체가 아니라 실시간이 말할 수 있는 항목만**입니다. 화면은
받은 항목만 갈아 끼웁니다. 자세한 것은 `feeds` 첫머리에 있습니다.

`subscriptions` 는 화면이 선언한 구독을 **그 화면이 보낸 모양 그대로** 나눠 답합니다.
`granted` 는 걸려서 값이 올 구독, `rejected` 는 값이 오지 않을 구독입니다 — 스무
자리가 모자라 밀린 것과 서버가 거절한 것이 둘 다 여기 듭니다. 서버가 거절한 것은
`failed` 에도 까닭과 함께 한 번 더 실립니다.

    "failed": [{"context": "overseas", "kind": "quote", "sym": "BRNZ26",
                "code": "RT_FAIL", "message": "해당 종목은 시세사용신청 후 ..."}]

`code` 와 `message` 는 서버가 보낸 그대로입니다. 거절이 생기거나 풀릴 때(다시
붙어 새로 걸 때 등) 붙어 있는 화면에 `subscriptions` 를 다시 보냅니다.

`status` 의 `state` 는 `connecting` · `live` · `reconnecting` · `stopped` 이고,
저장본으로 돌 때는 `replay-finished` · `replay-empty` 가 더 옵니다. 그 둘은
구독 하나에 대한 알림이라 `sym` 이 붙습니다(계좌 단위 구독이면 없음) — 기록에
없는 종목은 다른 종목 값으로 대신하지 않고 `replay-empty` 로 옵니다.

**붙자마자 `status` 가 먼저 옵니다** — 연결이 서 있는 시장마다 그 시장의 지금
상태 하나입니다(`refill` 없음, 그 시장의 첫 연결이 아직 붙는 중이면 `fresh`).
연결이 없는 시장은 오지 않습니다.

**`refill` 이 참이면 조회를 다시 불러야 합니다.** 끊겼다 다시 붙은 것이고,
끊긴 동안의 변화는 실시간이 메워 주지 않습니다. 이 신호를 무시하면 화면은
빠진 구간이 있는 값을 최신인 줄 알고 그대로 보여 줍니다.

**`session` 은 국내 세션 경계를 지났다는 뜻입니다**(주간 개장·마감·야간 개장·마감과
그 단일가). 화면은 `session` 값(`day`·`night`·없음)으로 실시간 맥락을 바꿔 다시
선언하고 조회를 다시 부릅니다. 붙을 때는 안 옵니다 — 처음 값은 `/api/session` 으로
읽습니다. 해외는 품목마다 시간이 달라 방송하지 않습니다.

**`environment` 는 활성 환경이 바뀌었다는 뜻입니다.** 모의와 실전은 다른 계좌라
계좌·시세·가능수량처럼 환경에 매인 값은 이때 비워야 합니다. 이어서 연결이
새 환경으로 다시 붙으며 `refill` 이 오므로, 그때 새 환경 값을 조회로 채우면
됩니다. 붙을 때는 안 옵니다 — 처음 값은 `/api/config` 로 읽습니다.

## 느린 화면 때문에 다른 화면이 밀리지 않게

화면마다 보낼 것을 담아 두는 자리가 있고 크기가 정해져 있습니다. 브라우저가
못 따라와서 자리가 차면 **가장 오래된 것부터 버립니다.** 실시간 값은 다음 것이
곧 오므로 오래된 것을 붙들고 있을 이유가 없고, 붙들면 그 화면 때문에 허브 전체가
멈춥니다.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from backend.nh_api import runtime
from backend.nh_api.catalog import STREAM_SUBSCRIPTION_LIMIT
from backend.nh_api.session import Session
from backend.realtime.coalesce import Coalescer
from backend.realtime.feeds import Feed, feed_of, paired
from backend.realtime.ledger import DEFAULT_PRIORITY, Ledger, StreamKey, stream_key
from backend.realtime.link import (
    CONNECTING,
    LIVE,
    RECONNECTING,
    STOPPED,
    AuthGate,
    Connection,
    per_connection_limit,
)
from backend.realtime.replay_link import ReplayLink
from backend.sessions.hours import now_kst

logger = logging.getLogger(__name__)

MARKETS = ("domestic", "overseas")

# 화면으로 보낼 것을 모으는 간격(초).
FLUSH_INTERVAL = 0.1

# 화면 하나가 밀린 채로 담아 둘 수 있는 메시지 수.
CLIENT_BACKLOG = 200


class Client:
    """붙어 있는 화면 하나."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=CLIENT_BACKLOG)
        self.keys: frozenset[StreamKey] = frozenset()
        # 이 화면이 **자기 말로** 선언한 구독. 받아들여졌는지 밀렸는지를
        # 되돌릴 때 이것으로 답합니다 — 자기가 보낸 것이 그대로 돌아와야
        # 화면이 자기 목록과 맞대어 볼 수 있습니다.
        self.feeds: tuple[Feed, ...] = ()

    def send(self, payload: dict[str, Any]) -> None:
        """보낼 것을 담습니다. 자리가 차면 가장 오래된 것을 버립니다."""
        try:
            self.queue.put_nowait(payload)
            return
        except asyncio.QueueFull:
            pass
        try:
            self.queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.debug("%s 로 보낼 자리가 없어 한 건을 버렸습니다.", self.name)


#: 「새 연결」 표지(`fresh`)를 다는 상태들. 그 시장이 아직 한 번도 `live` 가 아니었을
#: 때만 답니다 — 한 번 받은 뒤의 끊김·다시 붙음에는 달지 않습니다.
FRESH_STATES = (CONNECTING, RECONNECTING, LIVE)

#: 시장 연결의 상태들. 새로 붙는 화면에 알리는 것은 이 가운데 마지막 것입니다.
CONNECTION_STATES = (CONNECTING, LIVE, RECONNECTING, STOPPED)


class MarketGroup:
    """한 계좌의 연결들. 한 연결이 지는 구독 수를 넘으면 하나 더 엽니다."""

    def __init__(self, market: str, *, capacity: int, make_link: Callable[[], Any]) -> None:
        self.market = market
        self._capacity = max(1, capacity)
        self._make_link = make_link
        self._links: list[Any] = []

    @property
    def links(self) -> tuple[Any, ...]:
        return tuple(self._links)

    async def apply(self, wanted: dict[StreamKey, Feed]) -> None:
        """이 계좌가 지금 걸고 있어야 할 구독 전체."""
        holder: dict[StreamKey, Any] = {
            key: link for link in self._links for key in link.keys
        }

        # 국내 체결·호가의 주간·야간 짝은 한 연결에 함께 둡니다. 연결이 짝을 서버에
        # 늘 함께 등록해 두므로(`Connection._registrations`), 짝이 든 연결에 걸면
        # 서버로 새로 나가는 것이 없습니다. **해제보다 먼저** 겁니다 — 주간에서
        # 야간으로 바꿔 선언할 때 해제가 먼저 가면 짝 둘 다 서버에서 내려갔다가
        # 다시 올라갑니다.
        for key, feed in wanted.items():
            if key in holder:
                continue
            partner = paired(feed)
            link = holder.get(stream_key(partner)) if partner is not None else None
            if link is not None:
                await link.add(key, feed)
                holder[key] = link

        for key, link in list(holder.items()):
            if key not in wanted:
                await link.remove(key)
                del holder[key]

        for key, feed in wanted.items():
            if key in holder:
                continue
            await self._room().add(key, feed)

        for link in list(self._links):
            if len(link) == 0:
                await link.stop()
                self._links.remove(link)

    def _room(self) -> Any:
        for link in self._links:
            if len(link) < self._capacity:
                return link
        link = self._make_link()
        link.start()
        self._links.append(link)
        return link

    @property
    def failures(self) -> dict[StreamKey, tuple[str, str]]:
        """이 계좌의 연결들이 서버에게 거절당한 구독과 (코드, 글)."""
        made: dict[StreamKey, tuple[str, str]] = {}
        for link in self._links:
            made.update(getattr(link, "failures", {}))
        return made

    async def refresh(self, key: StreamKey) -> None:
        """이 구독을 들고 있는 연결에서 해제했다가 다시 겁니다(`Connection.refresh`)."""
        for link in self._links:
            if key in link.keys:
                await link.refresh(key)

    async def stop(self) -> None:
        for link in self._links:
            await link.stop()
        self._links.clear()


class Hub:
    """실시간을 받아 화면으로 넘기는 자리. 앱에 하나입니다."""

    def __init__(
        self,
        *,
        session: Session | None = None,
        environment: str | None = None,
        limit: int = STREAM_SUBSCRIPTION_LIMIT,
        per_connection: int | None = None,
        flush_interval: float = FLUSH_INTERVAL,
        connect: Any = None,
        replay_speed: float | None = None,
        gate: AuthGate | None = None,
        session_clock: bool = True,
    ) -> None:
        self._session = session
        self._environment = environment
        # 접속키 발급은 연결이 몇 개든 이 문 하나를 지납니다.
        self._gate = gate or AuthGate()
        self._ledger = Ledger(limit=limit)
        self._coalescer = Coalescer()
        self._clients: dict[str, Client] = {}
        # 화면이 붙은 횟수. 바뀐 것만 방송하는 쪽이 새 화면을 알아보는 데 씁니다.
        self._attached = 0
        self._groups: dict[str, MarketGroup] = {}
        self._subscribed: frozenset[StreamKey] = frozenset()
        # 지금 걸려 있는 구독. 자료가 오면 이 표로 어느 구독인지 찾습니다.
        self._granted: dict[StreamKey, Feed] = {}
        self._was_live: dict[str, bool] = {}
        # 시장마다 마지막으로 알린 연결 상태와 문구. 새로 붙는 화면에 이것을 알립니다.
        self._market_state: dict[str, tuple[str, str]] = {}
        self._flush_interval = flush_interval
        self._per_connection = per_connection
        self._connect = connect
        self._replay_speed = replay_speed
        self._flusher: asyncio.Task[None] | None = None
        # 국내 세션 경계를 알리는 시계. 시험은 끌 수 있습니다.
        self._session_clock_wanted = session_clock
        self._session_clock: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # ── 살림 ────────────────────────────────────────────────

    @property
    def session(self) -> Session:
        if self._session is None:
            self._session = runtime.client().session
        return self._session

    @property
    def environment(self) -> str:
        return self._environment or self.session.config.default_environment

    def start(self) -> None:
        if self._flusher is None:
            self._flusher = asyncio.create_task(self._flush_forever())
        if self._session_clock_wanted and self._session_clock is None:
            self._session_clock = asyncio.create_task(self._session_clock_forever())

    async def stop(self) -> None:
        for task in (self._flusher, self._session_clock):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._flusher = None
        self._session_clock = None
        for group in self._groups.values():
            await group.stop()
        self._groups.clear()
        self._clients.clear()
        self._subscribed = frozenset()
        self._was_live.clear()
        self._market_state.clear()

    # ── 화면 ────────────────────────────────────────────────

    def attach(self, name: str) -> Client:
        """화면 하나가 붙을 때 부릅니다.

        연결이 서 있는 시장마다 **지금 상태**를 먼저 알립니다. 다른 화면이 마지막으로
        받은 상태와 같습니다 — 다시 붙는 중인 시장을 「받는 중」으로 알리면 새 화면만
        끊긴 것을 모릅니다. 다시 채우라는 표지(`refill`)는 달지 않습니다. 이 화면은
        끊긴 적이 없고, 붙은 뒤 조회로 처음 값을 받습니다. 연결이 없는 시장은 알릴
        것이 없습니다.
        """
        client = Client(name)
        self._clients[name] = client
        self._attached += 1
        self.start()
        for market, (state, detail) in self._market_state.items():
            payload: dict[str, Any] = {
                "type": "status",
                "market": market,
                "state": state,
                "detail": detail,
            }
            if state in FRESH_STATES and not self._was_live.get(market):
                # 그 시장의 첫 연결이 아직 붙는 중입니다(`_on_state` 와 같은 표지).
                payload["fresh"] = True
            client.send(payload)
        return client

    async def detach(self, name: str) -> None:
        """화면 하나가 나갈 때 부릅니다. 그 몫의 구독을 놓습니다."""
        self._clients.pop(name, None)
        self._ledger.release(name)
        await self._reconcile()

    async def declare(self, name: str, wishes: dict[Feed, int]) -> None:
        """이 화면이 지금 보고 있는 것 전체를 적습니다."""
        client = self._clients.get(name)
        if client is None:
            return
        self._ledger.declare(name, wishes)
        client.feeds = tuple(wishes)
        client.keys = frozenset(stream_key(feed) for feed in wishes)
        await self._reconcile()

    async def reconnect(self) -> None:
        """NH선물 쪽 연결만 새로 엽니다. **붙어 있는 화면은 그대로 둡니다.**

        활성 환경이 바뀌었을 때 부릅니다. 연결은 만들 때의 환경으로 고정되어
        있어 갈아 끼우는 수밖에 없습니다. 화면이 보던 것은 환경이 아니라
        종목이라, 선언은 그대로 두고 새 환경에서 같은 것을 다시 겁니다.

        화면에는 먼저 환경이 바뀌었다고 알립니다 — 계좌·시세처럼 환경에 매인
        값을 비우라는 신호입니다. 그 뒤 연결이 다시 붙으면 기존의 `refill` 이
        따라오므로, 화면은 새 환경 값을 조회로 다시 채우게 됩니다.

        `stop()` 과 다릅니다 — 그쪽은 화면까지 다 내보내는 종료입니다.
        """
        async with self._lock:
            self._broadcast({"type": "environment", "active": self.environment})
            for group in self._groups.values():
                await group.stop()
            self._groups.clear()
            # 옛 환경에서 받은 값이 아직 안 나갔으면 여기서 버립니다. 새 환경
            # 값처럼 화면에 서면 안 됩니다.
            for key in self._subscribed:
                self._coalescer.drop(key)
            self._subscribed = frozenset()
            self._granted = {}
            await self._reconcile_locked()

    async def refresh(self, feeds: list[Feed]) -> None:
        """걸려 있는 구독들을 NH선물 쪽에 **다시 겁니다.** 장부는 그대로입니다.

        서버 쪽에서 조용히 끊긴 구독을 되살리는 자리입니다 — 같은 종목의 짝
        구독이 해제되면 서버는 알리지 않고 이쪽도 끊습니다(`feeds.paired`).
        장부에는 걸린 채로 남아 있어 다시 선언해도 아무것도 나가지 않으므로,
        연결에 직접 해제와 구독을 보내게 합니다.

        **이미 걸려 있는 구독만** 다시 겁니다. 자리가 모자라 밀린 구독은
        건너뜁니다 — 여기서 새로 걸면 상한을 넘깁니다. 같은 구독을 보는 화면도
        해제와 구독 사이에 잠깐 값을 못 받습니다(`Connection.refresh`).
        """
        async with self._lock:
            before = self._failures()
            await self._refresh_locked(feeds)
            # 다시 걸며 지운 실패가 있으면 알립니다 — 이 동안 `_on_failure` 는 답하지 않습니다.
            if self._failures() != before:
                self._answer(self._subscribed)

    async def _refresh_locked(self, feeds: list[Feed]) -> None:
        """`refresh` 의 몸. **`_lock` 을 쥔 채로만** 부릅니다."""
        for feed in feeds:
            key = stream_key(feed)
            if key not in self._subscribed:
                continue
            group = self._groups.get(feed.market)
            if group is not None:
                await group.refresh(key)

    # ── 안쪽 ────────────────────────────────────────────────

    async def reconnect_market(self, market: str) -> str:
        """**그 시장의 연결만** 내렸다가 다시 겁니다. 답은 `idle`·`reconnected`.

        토큰을 다시 받은 뒤 부릅니다 — 연결은 붙을 때 토큰과 접속키를 받으므로,
        다시 열어야 새 토큰으로 붙습니다. `reconnect()` 와 달리 환경이 바뀐 것이
        아니라 **환경 알림을 내지 않고** 다른 시장의 연결도 건드리지 않습니다.
        화면에는 그 시장 연결의 상태(다시 붙는 중 → 받는 중)만 갑니다.

        그 시장에 걸어 둔 구독이 없으면 할 일이 없습니다(`idle`).
        """
        async with self._lock:
            group = self._groups.pop(market, None)
            if group is None:
                return "idle"
            await group.stop()
            mine = frozenset(key for key in self._subscribed if key[0] == market)
            for key in mine:
                # 옛 연결에서 받아 아직 안 나간 값은 버립니다.
                self._coalescer.drop(key)
            self._subscribed = self._subscribed - mine
            self._granted = {
                key: feed for key, feed in self._granted.items() if key[0] != market
            }
            await self._reconcile_locked()
            return "reconnected"

    async def _reconcile(self) -> None:
        async with self._lock:
            await self._reconcile_locked()

    async def _reconcile_locked(self) -> None:
        """장부와 연결을 맞춥니다. **`_lock` 을 쥔 채로만** 부릅니다."""
        grant = self._ledger.grant()

        for key in self._subscribed - grant.keys:
            # 아직 안 보낸 것이 남아 있으면 그만 본 종목의 값이 뒤늦게
            # 화면에 섭니다.
            self._coalescer.drop(key)

        for market in MARKETS:
            wanted = {
                key: feed for key, feed in grant.granted.items() if key[0] == market
            }
            group = self._groups.get(market)
            if group is None:
                if not wanted:
                    continue
                group = self._groups[market] = self._group(market)
            await group.apply(wanted)
            if not group.links:
                # 그 시장의 연결이 모두 닫혔습니다. 다음에 서는 연결은 **새 연결**이라
                # 「다시 붙음」(`refill`)이 아닙니다 — 그렇게 알리면 갓 세운 계좌 실행이
                # 재접속 뒤 대사를 타고 중지로 내려앉습니다.
                self._was_live.pop(market, None)
                # 연결이 없으니 새로 붙는 화면에 알릴 상태도 없습니다.
                self._market_state.pop(market, None)

        self._subscribed = grant.keys
        self._granted = dict(grant.granted)
        self._answer(grant.keys)

    def _answer(self, live: frozenset[StreamKey]) -> None:
        """화면마다 **자기가 선언한 말로** 결과를 되돌립니다.

        여기서 대표 구독을 그대로 방송하면 안 됩니다. 주간 화면과 야간 화면이
        주문 알림을 각각 선언하면 NH선물 쪽에서는 하나로 모이는데, 대표 하나만
        방송하면 야간 화면은 자기가 보낸 것이 받아들여진 목록에도 밀린 목록에도
        없는 답을 받습니다. 남의 화면이 밀린 것까지 섞여 옵니다.

        서버가 거절한 구독은 걸려 있어도 값이 오지 않으므로 `rejected` 에 두고,
        까닭은 `failed` 에 붙입니다.
        """
        failures = self._failures()
        for client in self._clients.values():
            granted = [
                feed
                for feed in client.feeds
                if stream_key(feed) in live and stream_key(feed) not in failures
            ]
            rejected = [feed for feed in client.feeds if feed not in granted]
            failed = []
            for feed in client.feeds:
                failure = failures.get(stream_key(feed))
                if failure is not None:
                    code, message = failure
                    failed.append({**feed.as_dict(), "code": code, "message": message})
            client.send(
                {
                    "type": "subscriptions",
                    "granted": [feed.as_dict() for feed in granted],
                    "rejected": [feed.as_dict() for feed in rejected],
                    "failed": failed,
                }
            )

    def _failures(self) -> dict[StreamKey, tuple[str, str]]:
        """모든 계좌의 연결이 서버에게 거절당한 구독과 (코드, 글)."""
        made: dict[StreamKey, tuple[str, str]] = {}
        for group in self._groups.values():
            made.update(group.failures)
        return made

    def _on_failure(self) -> None:
        """연결이 거절된 구독을 새로 알았거나 지웠을 때 부릅니다. 화면에 다시 답합니다.

        **구독을 맞추는 중(`_lock` 을 쥔 동안)에는 답하지 않습니다.** 그 사이의
        구독 목록은 아직 새 선언이 반영되기 전이라, 여기서 답하면 막 올린 구독이
        「밀림」으로 나갑니다. 맞추는 쪽이 끝에 지금의 실패를 실어 한 번 답합니다.
        """
        if self._lock.locked():
            return
        self._answer(self._subscribed)

    def _group(self, market: str) -> MarketGroup:
        session = self.session
        environment = self.environment
        capacity = (
            self._per_connection
            if self._per_connection is not None
            else per_connection_limit()
        )

        if session.is_replay:
            store = session.fixtures
            if store is None:
                raise RuntimeError("저장본으로 돌 자료가 준비되지 않았습니다.")
            speed = self._replay_speed

            def make_replay() -> Any:
                link = ReplayLink(
                    market=market,
                    environment=environment,
                    store=store,
                    on_data=self._on_data,
                    on_state=self._on_state,
                    **({} if speed is None else {"speed": speed}),
                )
                return link

            return MarketGroup(market, capacity=capacity, make_link=make_replay)

        def make_live() -> Any:
            return Connection(
                market=market,
                environment=environment,
                base_url=session.base_url(environment),
                issue_key=lambda: session.tokens.websocket_key(market, environment),
                access_token=lambda: session.tokens.token(market, environment).access_token,
                on_data=self._on_data,
                on_state=self._on_state,
                gate=self._gate,
                connect=self._connect,
                on_failure=self._on_failure,
            )

        return MarketGroup(market, capacity=capacity, make_link=make_live)

    def _on_data(self, key: StreamKey, body: dict[str, Any]) -> None:
        feed = self._granted.get(key)
        if feed is None:
            # 해제 직후에 오는 메시지입니다. 그만 보겠다고 한 값을 화면에
            # 올리지 않습니다.
            return
        self._coalescer.add(feed, body)

    def _on_state(self, market: str, state: str, detail: str, sym: str = "") -> None:
        payload: dict[str, Any] = {
            "type": "status",
            "market": market,
            "state": state,
            "detail": detail,
        }
        if sym:
            payload["sym"] = sym
        if state in CONNECTION_STATES:
            # 구독 하나에 대한 알림(저장본의 끝남·없음)은 연결 상태가 아니라 남기지 않습니다.
            self._market_state[market] = (state, detail)
        if state in FRESH_STATES and not self._was_live.get(market):
            # 그 시장에서 아직 한 번도 받은 적이 없는 연결입니다(첫 연결이 붙는 중).
            # 끊긴 것이 아니므로 받는 쪽은 공백으로 볼 까닭이 없습니다. 화면에는
            # 그대로 연결 상태로 보입니다.
            payload["fresh"] = True
        if state == LIVE:
            if self._was_live.get(market):
                # 다시 붙은 것입니다. 끊긴 동안의 값은 실시간이 안 메워 줍니다.
                payload["refill"] = True
            self._was_live[market] = True
        self._broadcast(payload)

    @property
    def attached(self) -> int:
        """지금까지 화면이 붙은 횟수. 다시 붙은 것도 셉니다.

        바뀐 것이 있을 때만 방송하는 쪽(`strategy.pump`)이 이 값이 늘었으면
        같은 내용이라도 한 번 더 보냅니다 — 새 화면은 앞 방송을 받지 않았기 때문입니다.
        """
        return self._attached

    def publish(self, payload: dict[str, Any]) -> None:
        """백엔드 안에서 만든 것을 붙어 있는 화면 전부에 보냅니다.

        NH선물에서 온 값이 아니라 **백엔드가 만든 것**을 내보내는 자리입니다 —
        전략 실행 상태처럼 구독과 짝이 없는 것이 여기로 갑니다. 구독으로 오는
        값은 `_on_data` 를 거치므로 이 자리를 쓰지 않습니다.
        """
        self._broadcast(payload)

    def _broadcast(self, payload: dict[str, Any]) -> None:
        for client in self._clients.values():
            client.send(payload)

    async def _flush_forever(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            try:
                self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 한 번 실패했다고 여기서 끝나면 **그 뒤로 아무것도 안 나갑니다.**
                # 화면은 값이 멈춘 것을 장이 조용한 것으로 봅니다.
                logger.exception("모아 둔 실시간 값을 화면으로 못 보냈습니다.")

    async def _session_clock_forever(self) -> None:
        """국내 세션 경계마다 화면에 알립니다.

        붙어 있는 화면이 「지금 주간인가 야간인가」를 각자 시계로 재면 같은 순간에
        다른 답을 냅니다. 여기서 한 시계로 재어 경계에 한 번 방송하고, 화면은
        그것을 받아 맥락을 바꿉니다 — 구독을 대신 바꿔 주지는 않습니다. 화면이
        조회·주문에 쓰는 맥락도 같은 값이라 화면이 들고 있어야 합니다.
        """
        last: tuple[str, str | None] | None = None
        while True:
            try:
                notice, wait = session_notice(now_kst())
            except Exception:
                # 시간표를 못 읽어도 시계는 멈추지 않습니다. 한 시간 뒤 다시 봅니다.
                logger.exception("세션 경계를 셈하지 못했습니다.")
                await asyncio.sleep(SESSION_CLOCK_RETRY)
                continue
            # 경계를 지났을 때만 알립니다. 기다림이 상한에 걸려 깬 것은 경계가
            # 아닙니다 — **시간표가 그대로면** 다음 경계도 그대로라 둘 다 안
            # 바뀝니다. 그 사이 시간표를 새로 받아 줄이 달라졌으면 다음 경계가
            # 바뀌어 한 번 나갑니다. 화면이 세션을 다시 묻는 것뿐이라 그래도 됩니다.
            #
            # **상태만 보면 모자랍니다.** 공통 표의 상태는 품목마다 다른 경계를
            # 모르므로, 09:00 에 여는 품목의 경계에서는 안 바뀝니다. 다음
            # 경계(`nextEdge`)가 바뀐 것이 「경계를 지났다」입니다.
            edge_key = (notice["state"], notice.get("nextEdge"))
            if last is not None and edge_key != last:
                self._broadcast(notice)
            last = edge_key
            await asyncio.sleep(wait)

    def flush(self) -> None:
        """모아 둔 것을 원한 화면에만 나눠 줍니다."""
        for key, message in self._coalescer.drain():
            for client in self._clients.values():
                if key in client.keys:
                    client.send(message)


# 세션 경계를 셈하지 못했을 때 다시 볼 때까지의 시간(초).
SESSION_CLOCK_RETRY = 3600.0
# 경계까지 아무리 멀어도 이만큼 지나면 다시 셈합니다 — 그 사이 시간표가 바뀔 수 있습니다.
SESSION_CLOCK_MAX_WAIT = 3600.0


def session_notice(now: datetime) -> tuple[dict[str, Any], float]:
    """지금의 국내 세션 알림과, 다음 경계까지 기다릴 시간(초).

    **상태는 품목과 무관합니다** — 야간이 있는 품목 기준이고, 최종거래일의 이른
    마감처럼 종목마다 다른 사정은 여기 안 실립니다. 화면은 이 알림을 **경계를
    지났다는 신호로만** 쓰고 세션은 종목마다 다시 묻습니다.

    **기다림은 품목 줄들의 경계로 잽니다**(`nextEdge`, `judge.domestic_edges`).
    공통 표의 경계 하나로 재면 소수 쪽 품목의 경계에서 시계가 안 울려, 그
    품목을 보던 화면은 다음 경계까지 옛 세션으로 남습니다.

    `until` 은 **공통 표 기준**입니다. `nextEdge` 와 다를 수 있고,
    다른 것이 정상입니다 — 하나는 「이 시계의 세션이 끝나는 때」, 하나는 「어느
    품목이든 다음으로 뭔가 바뀌는 때」입니다.
    """
    from backend.sessions import judge
    from backend.sessions import service as session_service

    table = session_service.service.cached(judge.DOMESTIC)
    made = judge.judge_domestic(None, has_night=True, table=table, now=now)
    edge = judge.domestic_edges(table, now)
    notice = {
        "type": "session",
        "market": "domestic",
        "state": made["state"],
        "session": made["session"],
        "since": made["since"],
        "until": made["until"],
        "nextOpen": made["nextOpen"],
        "nextEdge": edge,
        # **여기서는 품목 줄이 안 나옵니다.** 종목 없이 재는 시계라 판정이 품목
        # 줄을 찾을 데가 없어, 값은 늘 공통 표 아니면 내장 표입니다.
        "hoursLayer": made["hoursLayer"],
    }
    if not edge:
        return notice, SESSION_CLOCK_RETRY
    seconds = (datetime.fromisoformat(edge) - now).total_seconds()
    return notice, max(1.0, min(seconds, SESSION_CLOCK_MAX_WAIT))


def wishes_of(raw: Any) -> dict[Feed, int]:
    """화면이 보내 온 구독 목록을 읽습니다. 모양이 틀리면 `ValueError`."""
    if not isinstance(raw, list):
        raise ValueError("구독 목록은 배열로 보내 주세요.")
    made: dict[Feed, int] = {}
    for item in raw:
        feed = feed_of(item)
        priority = item.get("priority") if isinstance(item, dict) else None
        try:
            made[feed] = int(priority) if priority is not None else DEFAULT_PRIORITY
        except (TypeError, ValueError):
            made[feed] = DEFAULT_PRIORITY
    return made


_hub: Hub | None = None


def hub() -> Hub:
    """앱이 쓰는 허브. 처음 부를 때 한 번 만듭니다."""
    global _hub
    if _hub is None:
        _hub = Hub()
    return _hub


async def reset() -> None:
    """만들어 둔 허브를 버립니다. 테스트가 씁니다."""
    global _hub
    if _hub is not None:
        await _hub.stop()
    _hub = None
