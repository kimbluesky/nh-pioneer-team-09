"""NH선물 실시간 서버와의 연결 하나를 지킵니다.

## 연결은 계좌마다 하나입니다

국내와 해외는 APP Key 가 따로 발급되는 다른 계좌라 접속 토큰이 다릅니다.
연결을 열 때 그 토큰 하나로 세션을 여니 **한 연결이 두 시장을 담지 못합니다.**
그래서 국내용과 해외용을 각각 엽니다.

## 접속키는 한 번 쓰면 끝입니다

명세가 「웹소켓 연결 시 1회 사용」이라고 적고 있습니다. 그래서 **연결할 때마다
새로 발급**받고 저장하지 않습니다. 끊겨서 다시 붙을 때도 새 키를 받습니다.

키와 접속 토큰은 **기록에 남기지 않습니다.** 키는 주소 뒤에 붙어서 나가므로
주소를 그대로 로그에 적으면 키가 로그에 남습니다. 그래서 이 파일은 주소를
찍지 않고 어느 계좌의 연결인지만 적습니다.

## Ping-Pong 을 되돌려야 세션이 유지됩니다

서버가 `{"code":"pingpong"}` 을 보내면 **받은 그대로 되돌려 보내야** 합니다.
안 보내면 세션이 자동으로 종료됩니다. 그래서 이 응답은 구독을 걸기 전
초기화 단계에서도 처리합니다 — 초기화를 기다리는 동안 먼저 올 수 있습니다.

**세션 유지는 이 되돌림 하나로 합니다.** 웹소켓 규격의 핑 프레임은 보내지
않습니다 — 라이브러리가 기본으로 20초마다 보내는 핑 프레임을 켜 두면 국내
실시간 연결이 같은 주기로 끊기므로 핑을 끕니다. 서버가 연결을 닫으면 닫힘
코드와 이유를 기록에 남깁니다(`_listen`). 대신 서버가 보내는 것이 한동안
없으면(`IDLE_TIMEOUT`) 죽은 연결로 보고 다시 붙습니다 — 서버는 세션 유지 신호를
주기적으로 보내므로 살아 있는 연결이 그만큼 조용할 일은 없습니다.

## 끊기면 다시 붙습니다

최근 10분 안에 짧게 끊긴 횟수만큼 1·2·5·10·30·60초로 벌리며 다시 붙습니다.
60초보다 짧게 붙어 있다 끊기면 서버가 정상 절차로 닫았더라도 짧게 끊긴 것으로
셉니다. 60초 넘게 붙어 있었고 그 앞 10분 안에 짧게 끊긴 일이 없으면 기다리지
않고 바로 다시 붙습니다(`Pacer`). 붙고 나면 장부에
있는 구독을 **전부 다시 겁니다** — 서버는 끊기기 전의 구독을 기억하지
않습니다.

**끊긴 동안의 값은 메워지지 않습니다.** 실시간은 지금 일어나는 것만 보내고
지나간 것을 다시 주지 않습니다. 그래서 다시 붙으면 화면에 「조회로 다시
채우라」고 알립니다. 이 신호가 없으면 화면은 멈춘 값을 최신인 줄 알고 그대로
보여 줍니다.

## 서버가 거절한 구독은 「실패」로 들고 있습니다

시세 사용 신청을 하지 않은 거래소의 종목처럼 서버가 받아 주지 않는 구독은
`RT_INIT` 대신 `RT_FAIL` 같은 다른 코드로 답이 옵니다. 그 답을 어느 구독의 것인지
(`data` 의 실시간 이름·종목, 없으면 글 안의 `[action_sym::종목]`)로 짝지어
**그 연결에서 실패한 구독**으로 들고 있고, 바뀔 때마다 허브에 알립니다
(`on_failure`). 허브는 그 구독을 화면에 「받지 못함」으로 답합니다.

실패는 연결 하나의 것입니다. 다시 붙거나 그 구독을 다시 걸면(`refresh`) 지우고
새로 걸어 봅니다. 그 뒤 같은 구독에 `RT_INIT` 이 오면 그때도 지웁니다.

## 한 연결이 지는 구독 수는 값 하나로 정합니다

명세에 「전체 실시간 구독 제한은 20개」라고만 있고, 그것이 연결 단위인지 앱
단위인지, 한 연결에 여럿을 걸 수 있는지는 적혀 있지 않습니다. 그래서 **한
연결이 지는 구독 수를 값 하나로** 둡니다(`NH_STREAM_PER_CONNECTION`, 기본은
전체 상한과 같은 20). 1 로 두면 구독마다 연결이 하나씩 열립니다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol
from urllib.parse import quote as urlquote

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from backend.nh_api.catalog import (
    STREAM_APIS,
    STREAM_PATH,
    STREAM_SUBSCRIPTION_LIMIT,
    Limit,
)
from backend.nh_api import error_codes
from backend.nh_api.rate_limit import RateLimiter
from backend.realtime.feeds import Feed, paired
from backend.realtime.ledger import StreamKey, stream_key

logger = logging.getLogger(__name__)

PER_CONNECTION_FLAG = "NH_STREAM_PER_CONNECTION"

API_VERSION = "1"
ACTION_INIT = "0"
ACTION_SUBSCRIBE = "A"
ACTION_UNSUBSCRIBE = "D"

INIT_CODE = "INIT"
INIT_SUCCESS = "INIT_SUCCESS"
PINGPONG_CODE = "pingpong"
SUBSCRIBED_CODE = "RT_INIT"
#: 서버가 구독을 받아 주지 않았다는 답. 다른 코드의 구독 답도 같은 자리로 갑니다.
FAILED_CODE = "RT_FAIL"

# 거절 글 안에 실린 종목(`... [action_sym::BRNZ26][미신청거래소::BRN]`).
_SYM_IN_TEXT = re.compile(r"\[action_sym::([^\]]*)\]")

# 연결과 초기화를 기다리는 시간.
OPEN_TIMEOUT = 10.0
INIT_TIMEOUT = 10.0

# 닫을 때 상대의 닫기 응답을 기다리는 시간. 이 앱이 끊는 자리(환경 전환·종료)에서
# 상대가 답을 안 주면 그만큼 멈추므로 짧게 둡니다 — 답이 없어도 소켓은 닫힙니다.
CLOSE_TIMEOUT = 1.0

# 웹소켓 규격의 핑 프레임을 **보내지 않습니다**(라이브러리 기본은 20초마다).
# 켜 두면 국내 실시간 연결이 그 주기로 끊깁니다. 세션은 서버의 `pingpong`
# 메시지를 되돌리는 것으로 유지합니다.
PING_INTERVAL = None

# 서버가 보내는 것이 이만큼 없으면 죽은 연결로 보고 다시 붙습니다. 서버가 세션
# 유지 신호를 주기적으로 보내므로, 살아 있는 연결은 시세가 없어도 이보다 자주
# 무언가를 보냅니다. 그 주기는 명세에 없어 넉넉히 둡니다.
IDLE_TIMEOUT = 90.0

# 다시 붙기까지 기다리는 시간(초). 최근에 짧게 끊긴 횟수가 n 이면 n 번째 값을
# 기다립니다. 처음 붙을 때는 기다리지 않습니다. 앞쪽은 잠깐의 끊김에서 빨리
# 돌아오도록 작게 둡니다. 끝 값 60초가 상한으로, 서버가 연결을 계속
# 닫아도 다시 붙는 것(그때마다 접속키 발급 한 건)이 1분에 한 번을 넘지 않습니다.
BACKOFF = (0.0, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)

# 이만큼은 붙어 있어야 「제대로 붙었다」로 봅니다. 그보다 짧게 붙었다 끊기면
# 서버가 정상 절차(code=1000)로 닫았더라도 「짧게 끊김」으로 셉니다. 서버가
# 몇 초~수십 초마다 연결을 닫는 경우(국내 실시간은 20초 주기로 끊길 수
# 있습니다)가 이 값 아래에 들어오도록 넉넉히 1분으로 둡니다.
SETTLED_AFTER = 60.0

# 「최근」의 범위(초). 이보다 오래된 짧은 끊김은 세지 않습니다. 상한(60초)
# 대기를 여러 번 거쳐도 기록이 남을 만큼 길게, 그러면서 한 번 안정된 뒤 다음
# 끊김에는 바로 다시 붙을 만큼 짧게 10분으로 둡니다.
UNSETTLED_WINDOW = 600.0

CONNECTING = "connecting"
LIVE = "live"
RECONNECTING = "reconnecting"
STOPPED = "stopped"

OnData = Callable[[StreamKey, dict[str, Any]], None]

#: 실패한 구독 하나의 (서버 코드, 서버가 보낸 글).
Failure = tuple[str, str]


class OnState(Protocol):
    """상태 알림. (계좌, 상태, 설명) 에 구독 하나의 알림이면 종목이 더 붙습니다."""

    def __call__(self, market: str, state: str, detail: str, sym: str = "") -> None: ...


class AuthGate:
    """접속키를 받아 오는 문 — 한 번에 하나씩, 인증 호출 간격을 지켜서.

    연결마다 새 접속키가 필요합니다. 한 연결이 구독 하나씩만 진다면 연결이
    스무 개가 되고, 문이 없으면 **키 발급 스무 건이 한꺼번에 나갑니다.**
    인증은 초당 세 건이라 그러면 차단됩니다. 끊겨서 한꺼번에 다시 붙을 때도
    같습니다.

    접속 토큰은 여기서 간격을 세지 않습니다. `TokenManager` 가 (환경, 시장)
    마다 하나를 오래 쓰고 발급을 스스로 잠그므로 몰릴 일이 없습니다. 접속키는
    쓸 때마다 새로 받아야 해서 사정이 다릅니다. 다만 **문은 함께 지납니다** —
    토큰이 없어 새로 받는 순간이 겹치면 그것도 인증 호출입니다.
    """

    def __init__(self, limiter: RateLimiter | None = None) -> None:
        self._limiter = limiter or RateLimiter()
        self._lock = asyncio.Lock()

    async def take(
        self,
        *,
        environment: str,
        market: str,
        issue_key: Callable[[], str],
        access_token: Callable[[], str],
    ) -> tuple[str, str]:
        """접속키와 접속 토큰을 받아 옵니다. 둘 다 통신이라 스레드에서 부릅니다."""
        async with self._lock:
            token = await asyncio.to_thread(access_token)
            key = await asyncio.to_thread(self._issue, environment, market, issue_key)
        return key, token

    def _issue(self, environment: str, market: str, issue_key: Callable[[], str]) -> str:
        self._limiter.wait(environment, market, Limit.AUTH)
        return issue_key()


class Pacer:
    """끊긴 뒤 다시 붙기까지 얼마나 기다릴지 정합니다.

    다시 붙을 때마다 접속키를 새로 받아야 합니다. 접속키는 연결 한 번에만
    쓸 수 있어(명세 「웹소켓 연결 시 1회 사용」, 오류 `EST02` 「이미 소비된
    토큰」) 남겨 두었다가 다시 쓸 수 없습니다. 그래서 다시 붙는 횟수 자체를
    줄여야 인증 호출(초당 세 건)도 줄어듭니다.

    `UNSETTLED_WINDOW` 안에서 짧게 끊긴 횟수만큼 `BACKOFF` 의 단계를 올립니다.
    짧게 끊김은 `SETTLED_AFTER` 보다 짧게 붙어 있었던 경우와 오류로 끊긴
    경우입니다. 오래 붙어 있다가 정상으로 닫혔으면 세지 않습니다.
    """

    def __init__(
        self,
        backoff: tuple[float, ...] = BACKOFF,
        *,
        settled_after: float = SETTLED_AFTER,
        window: float = UNSETTLED_WINDOW,
    ) -> None:
        self._backoff = backoff
        self._settled_after = settled_after
        self._window = window
        self._unsettled: list[float] = []

    def after(self, *, now: float, lasted: float, failed: bool) -> float:
        """세션 하나가 끝난 시각과 붙어 있던 시간으로 다음 대기 시간을 냅니다."""
        self._unsettled = [at for at in self._unsettled if now - at < self._window]
        if failed or lasted < self._settled_after:
            self._unsettled.append(now)
        return self._backoff[min(len(self._unsettled), len(self._backoff) - 1)]


def per_connection_limit(env: Mapping[str, str] | None = None) -> int:
    """한 연결이 지는 구독 수. 환경 변수가 없으면 전체 상한과 같습니다."""
    env = os.environ if env is None else env
    raw = str(env.get(PER_CONNECTION_FLAG, "")).strip()
    if not raw:
        return STREAM_SUBSCRIPTION_LIMIT
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s 값을 숫자로 읽지 못해 기본값을 씁니다: %r", PER_CONNECTION_FLAG, raw)
        return STREAM_SUBSCRIPTION_LIMIT
    return max(1, value)


def api_id_of(market: str, action_code: str) -> str | None:
    """자료 메시지가 어느 실시간 API 의 것인지.

    국내 주문/체결은 이름이 `O1_K` 인데 메시지에는 `O1` 로 옵니다. 해외도
    `O1` 이라 **계좌가 갈라 줘야** 어느 쪽인지 정해집니다.
    """
    for spec in STREAM_APIS.values():
        if spec.market == market and spec.action_code == action_code:
            return spec.api_id
    return None


def subscribe_message(feed: Feed, *, action: str) -> dict[str, Any]:
    """구독·해제 메시지 하나.

    주문/체결 알림에는 종목 칸이 없습니다. 빈 종목을 넣어 보내면 그 계좌의
    알림이 아니라 「빈 종목」을 구독하겠다는 말이 됩니다.
    """
    body: dict[str, Any] = {"action_code": feed.action_code}
    if feed.sym:
        body["action_sym"] = feed.sym
    return {"header": {"api_ver": API_VERSION, "action": action}, "body": body}


class Connection:
    """웹소켓 하나. 붙어 있는 동안 구독을 지키고, 끊기면 다시 붙습니다."""

    def __init__(
        self,
        *,
        market: str,
        environment: str,
        base_url: str,
        issue_key: Callable[[], str],
        access_token: Callable[[], str],
        on_data: OnData,
        on_state: OnState,
        gate: AuthGate | None = None,
        connect: Any = None,
        backoff: tuple[float, ...] = BACKOFF,
        idle_timeout: float = IDLE_TIMEOUT,
        settled_after: float = SETTLED_AFTER,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        on_failure: Callable[[], None] | None = None,
    ) -> None:
        self.market = market
        self.environment = environment
        self._base_url = base_url
        self._issue_key = issue_key
        self._access_token = access_token
        self._gate = gate or AuthGate()
        self._on_data = on_data
        self._on_state = on_state
        self._connect = connect or ws_connect
        self._pacer = Pacer(backoff, settled_after=settled_after)
        self._idle_timeout = idle_timeout
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self._sleep = sleep or asyncio.sleep
        # 서버가 정상 절차로 닫았을 때의 (닫힘 코드, 이유). 기록에 남깁니다.
        self._closed_by: tuple[Any, Any] = (None, None)

        self._feeds: dict[StreamKey, Feed] = {}
        # 서버가 거절한 구독과 그 까닭. 이 연결이 붙어 있는 동안만 들고 있습니다.
        self._failed: dict[StreamKey, Failure] = {}
        self._on_failure = on_failure
        self._socket: Any = None
        self._pending: set[asyncio.Task[None]] = set()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._state = STOPPED

    @property
    def state(self) -> str:
        return self._state

    @property
    def keys(self) -> frozenset[StreamKey]:
        return frozenset(self._feeds)

    def __len__(self) -> int:
        return len(self._feeds)

    @property
    def failures(self) -> dict[StreamKey, Failure]:
        """서버가 거절한 구독과 (코드, 글). 구독 자리 셈(`len`)에는 그대로 들어 있습니다."""
        return dict(self._failed)

    def _forget_failures(self, *keys: StreamKey) -> None:
        """주어진 구독의 실패 기록만 지웁니다. 키가 없으면 아무것도 안 합니다.

        바뀐 것이 있으면 알립니다. 전부 지우는 것은 `_forget_all_failures` 입니다.
        """
        removed = [key for key in keys if self._failed.pop(key, None) is not None]
        if removed:
            self._tell_failure()

    def _forget_all_failures(self) -> None:
        """이 연결의 실패 기록을 전부 지웁니다. 있었으면 알립니다."""
        if self._failed:
            self._failed.clear()
            self._tell_failure()

    def _tell_failure(self) -> None:
        if self._on_failure is not None:
            self._on_failure()

    def start(self) -> None:
        """연결을 유지하는 일을 시작합니다."""
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._keep())

    async def stop(self) -> None:
        """연결을 끊고 정리합니다.

        끊기 전에 구독 해제를 보냅니다. 연결이 닫히면 세션도 끝나지만, 나갈
        때 치우고 나가는 편이 서버 쪽에 이 연결의 등록을 남기지 않습니다.
        """
        self._stopping = True
        task, self._task = self._task, None
        socket, self._socket = self._socket, None
        if socket is not None:
            for feed in list(self._registrations().values()):
                await _say(socket, subscribe_message(feed, action=ACTION_UNSUBSCRIBE))
            await _close_quietly(socket)
            # 이 앱이 닫아도 서버의 닫기 응답과 함께 정상 닫힘(code=1000)으로
            # 끝나므로, 받는 쪽에서는 서버가 닫은 것과 구별되지 않습니다.
            # 그래서 닫은 사실은 여기서 남깁니다.
            logger.warning("%s 실시간 연결을 닫았습니다.", self.market)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._forget_all_failures()
        self._set_state(STOPPED, "")

    @property
    def registered(self) -> frozenset[StreamKey]:
        """서버에 등록해 두는 구독 — 걸어 달라는 구독과 그 짝(`_registrations`)."""
        return frozenset(self._registrations())

    def _registrations(self) -> dict[StreamKey, Feed]:
        """서버에 등록해 둘 구독. **국내 체결·호가는 주간·야간 짝을 늘 함께** 둡니다.

        같은 종목의 주간·야간(`KA`/`KC`, `KB`/`KD`)은 서버에서 한 등록으로 묶입니다
        (`feeds.paired`). 한쪽을 해제하면 남은 쪽도 값이 끊기고, 그 뒤에는 남은
        쪽만 다시 구독해도 값이 오지 않습니다. 해제한 쪽을 다시 등록해야 돌아옵니다.

        그래서 한쪽만 필요해도 둘 다 등록하고, 둘 다 필요 없어질 때만 둘 다
        해제합니다. 필요 없는 쪽의 값은 받는 자리에서 버립니다(`_receive`).
        짝을 함께 등록한 것이 서버의 구독 상한(20)에 한 건으로 세어지는지 두
        건으로 세어지는지는 명세에 없습니다. 자리 셈(`len`)은 걸어 달라는 구독만
        세고, 상한에 걸리면 서버가 보내는 코드를 기록에 남깁니다(`_control`).
        """
        made = dict(self._feeds)
        for feed in self._feeds.values():
            partner = paired(feed)
            if partner is not None:
                made.setdefault(stream_key(partner), partner)
        return made

    async def add(self, key: StreamKey, feed: Feed) -> None:
        """구독 하나를 겁니다. 서버에 아직 없는 등록만 보냅니다 — 짝이 먼저 걸려 있었다면 이미 등록돼 있습니다."""
        if key in self._feeds:
            return
        before = self._registrations()
        self._feeds[key] = feed
        for made_key, made in self._registrations().items():
            if made_key not in before:
                await self._send(subscribe_message(made, action=ACTION_SUBSCRIBE))

    async def remove(self, key: StreamKey) -> None:
        """구독 하나를 해제합니다. **짝이 아직 필요하면 서버에는 아무것도 보내지 않습니다.**

        짝 둘 다 필요 없어지면 둘 다 해제합니다(`_registrations`). 해제는 짝도
        함께 끊으므로, 한쪽만 해제하고 남은 쪽을 다시 거는 방식으로는 남은 쪽의
        값이 돌아오지 않습니다.
        """
        if key not in self._feeds:
            return
        before = self._registrations()
        del self._feeds[key]
        self._forget_failures(key)
        after = self._registrations()
        for made_key, made in before.items():
            if made_key not in after:
                await self._send(subscribe_message(made, action=ACTION_UNSUBSCRIBE))

    async def refresh(self, key: StreamKey) -> None:
        """걸려 있는 구독 하나를 **해제했다가 다시 겁니다.** 없는 구독이면 할 일이 없습니다.

        서버 쪽에서 조용히 끊긴 구독을 되살리는 자리입니다. 이미 걸린 줄로 알고
        있는 구독이라 다시 선언해도 메시지가 나가지 않으므로, 여기서 직접
        해제와 구독을 차례로 보냅니다. 해제가 짝도 끊으므로 짝도 이어서 다시
        겁니다. 이 연결의 구독 목록과 자리 셈은 바뀌지 않습니다.

        구독 하나는 같은 종목을 보는 화면과 실행이 함께 씁니다. 해제와 구독
        사이의 아주 짧은 동안 **그 화면들도 값을 못 받습니다.** 연결이 끊겨
        있으면 아무것도 보내지 않습니다 — 다시 붙을 때 걸린 구독을 전부 겁니다.
        """
        feed = self._feeds.get(key)
        if feed is None:
            return
        # 다시 걸어 보는 것이라 앞의 실패는 지웁니다. 또 거절되면 다시 기록됩니다.
        self._forget_failures(key)
        await self._send(subscribe_message(feed, action=ACTION_UNSUBSCRIBE))
        await self._send(subscribe_message(feed, action=ACTION_SUBSCRIBE))
        partner = paired(feed)
        if partner is not None:
            await self._send(subscribe_message(partner, action=ACTION_SUBSCRIBE))

    def _set_state(self, state: str, detail: str) -> None:
        if state == self._state:
            return
        self._state = state
        self._on_state(self.market, state, detail)

    async def _send(self, payload: dict[str, Any]) -> None:
        """붙어 있을 때만 보냅니다. 끊겨 있으면 다시 붙을 때 함께 걸립니다."""
        socket = self._socket
        if socket is None:
            return
        try:
            await socket.send(json.dumps(payload, ensure_ascii=False))
        except (WebSocketException, OSError, RuntimeError) as exc:
            # 보내다 끊긴 것입니다. 다시 붙는 쪽에서 처리합니다.
            logger.debug("%s 실시간 전송이 끊겼습니다: %s", self.market, type(exc).__name__)

    async def _keep(self) -> None:
        wait = 0.0
        while not self._stopping:
            if wait:
                await self._sleep(wait)
            started = self._clock()
            self._closed_by = (None, None)
            try:
                await self._session()
                # 서버가 정상 절차로 닫았습니다. 붙자마자 닫히는 연결이면
                # 바로 다시 붙지 않습니다(`Pacer`).
                lasted = self._clock() - started
                wait = self._pacer.after(now=self._clock(), lasted=lasted, failed=False)
                if not self._stopping:
                    code, reason = self._closed_by
                    logger.warning(
                        "%s 실시간 연결을 서버가 닫았습니다 (code=%s, reason=%r). "
                        "%.1f초 동안 붙어 있었고 %.0f초 뒤 다시 붙습니다.",
                        self.market,
                        code,
                        reason,
                        lasted,
                        wait,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 어떤 실패로도 다시 붙습니다.
                lasted = self._clock() - started
                wait = self._pacer.after(now=self._clock(), lasted=lasted, failed=True)
                if not self._stopping:
                    # 이 앱이 닫는 중이면 받는 쪽이 끊김으로 빠지는 것이
                    # 정상입니다. 그때도 「다시 붙습니다」를 남기면 종료 기록에
                    # 일어나지 않을 일이 적힙니다.
                    #
                    # 주소에는 접속키가 붙어 있어 예외 문자열을 그대로 적지
                    # 않습니다. 무엇이 실패했는지만 남깁니다.
                    logger.warning(
                        "%s 실시간 연결이 끊겼습니다 (%s). 연결을 시작한 지 %.1f초 "
                        "만이며 %.0f초 뒤 다시 붙습니다.",
                        self.market,
                        type(exc).__name__,
                        lasted,
                        wait,
                    )
            finally:
                self._socket = None
            if not self._stopping:
                self._set_state(RECONNECTING, "실시간 연결이 끊겨 다시 붙는 중입니다.")

    async def _session(self) -> None:
        self._set_state(CONNECTING, "실시간 연결을 준비하고 있습니다.")
        key, token = await self._gate.take(
            environment=self.environment,
            market=self.market,
            issue_key=self._issue_key,
            access_token=self._access_token,
        )
        url = f"{_ws_base(self._base_url)}{STREAM_PATH}?access_key={urlquote(key, safe='')}"

        async with self._connect(
            url,
            open_timeout=OPEN_TIMEOUT,
            close_timeout=CLOSE_TIMEOUT,
            ping_interval=PING_INTERVAL,
            ping_timeout=None,
        ) as socket:
            await self._initialise(socket, token)
            # 초기화가 끝난 뒤에야 보낼 수 있는 자리로 둡니다. 그전에 내놓으면
            # 초기화를 기다리는 사이에 걸린 구독이 여기서 한 번, 아래에서 또
            # 한 번 나가 같은 구독이 두 번 걸립니다.
            self._socket = socket
            # 새 연결에서는 전부 새로 걸어 봅니다. 앞 연결의 실패는 지웁니다.
            self._forget_all_failures()
            self._set_state(LIVE, "")
            # 서버는 끊기기 전의 구독을 기억하지 않습니다. 짝도 함께 겁니다.
            for feed in list(self._registrations().values()):
                await self._send(subscribe_message(feed, action=ACTION_SUBSCRIBE))
            await self._listen(socket)

    async def _listen(self, socket: Any) -> None:
        """서버가 보내는 것을 받습니다. 닫히거나 한동안 조용하면 끝납니다.

        **왜 닫혔는지를 기록에 남깁니다** — 닫힘 코드와 이유가 없으면 서버가
        끊은 것인지 이쪽에서 놓친 것인지 가릴 수 없습니다.
        """
        messages = aiter(socket)
        while True:
            try:
                raw = await asyncio.wait_for(anext(messages), self._idle_timeout)
            except StopAsyncIteration:
                # 정상 절차로 닫혔습니다. 이 앱이 닫은 것이면 `stop` 이, 서버가
                # 닫은 것이면 붙어 있던 시간·다음 대기와 함께 `_keep` 이 남깁니다.
                self._closed_by = (
                    getattr(socket, "close_code", None),
                    getattr(socket, "close_reason", None),
                )
                return
            except ConnectionClosed as exc:
                logger.warning(
                    "%s 실시간 연결이 비정상으로 닫혔습니다 (received=%s, sent=%s).",
                    self.market,
                    _close_frame(exc.rcvd),
                    _close_frame(exc.sent),
                )
                raise
            except TimeoutError:
                # 다시 붙는 쪽은 예외 이름만 적으므로 까닭은 여기서 남깁니다.
                logger.warning(
                    "%s 실시간 서버가 %.0f초 동안 아무것도 보내지 않아 죽은 연결로 봅니다.",
                    self.market,
                    self._idle_timeout,
                )
                raise TimeoutError("서버 신호 없음") from None
            self._receive(socket, raw)

    async def _initialise(self, socket: Any, token: str) -> None:
        await socket.send(
            json.dumps(
                {
                    "header": {"api_ver": API_VERSION, "action": ACTION_INIT},
                    "body": {"action_code": "session_init", "session_token": token},
                },
                ensure_ascii=False,
            )
        )
        deadline = asyncio.get_running_loop().time() + INIT_TIMEOUT
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("실시간 세션 초기화 응답이 오지 않았습니다.")
            raw = await asyncio.wait_for(socket.recv(), remaining)
            message = _decode(raw)
            if message is None:
                continue
            if _is_pingpong(message):
                # 초기화를 기다리는 동안에도 올 수 있습니다.
                logger.debug("%s 초기화 중 세션 유지 신호를 되돌립니다: %s", self.market, message)
                await socket.send(json.dumps(message, ensure_ascii=False))
                continue
            if message.get("code") == INIT_CODE and message.get("msg") == INIT_SUCCESS:
                return
            raise ConnectionError("실시간 세션 초기화가 받아들여지지 않았습니다.")

    def _receive(self, socket: Any, raw: Any) -> None:
        message = _decode(raw)
        if message is None:
            return

        code = message.get("code")
        if _is_pingpong(message):
            # 되돌리지 않으면 세션이 종료됩니다. 붙잡아 두지 않으면 보내는
            # 도중에 치워질 수 있어 시작한 일을 들고 있습니다. 기록은 서버가
            # 이 신호를 얼마나 자주 보내는지 재는 데 씁니다(`IDLE_TIMEOUT` 의 근거).
            logger.debug("%s 실시간 세션 유지 신호를 되돌립니다: %s", self.market, message)
            task = asyncio.create_task(_echo(socket, message))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
            return
        if code == SUBSCRIBED_CODE:
            self._acknowledged(message)
            return
        if code is not None:
            self._control(str(code), message)
            return

        body = message.get("body")
        header = message.get("header")
        if not isinstance(body, dict) or not isinstance(header, dict):
            return

        api_id = api_id_of(self.market, str(header.get("action_code") or ""))
        if api_id is None:
            return
        key = (self.market, api_id, str(header.get("action_sym") or ""))
        if key in self._feeds:
            self._on_data(key, body)

    def _acknowledged(self, message: dict[str, Any]) -> None:
        """서버의 구독·해제 답을 기록에 남깁니다.

        답에는 어느 요청의 답인지(`data` 의 구분·실시간 이름·종목)가 실려 옵니다.
        구독을 걸었는데 값이 오지 않을 때, 서버가 그 요청을 받았는지를 이 기록으로
        가립니다. 구독과 해제 때만 오므로 기록이 많지 않습니다.
        """
        data = message.get("data")
        data = data if isinstance(data, dict) else {}
        logger.warning(
            "%s 실시간 구독 답 %s: action=%s code=%s sym=%s msg=%r",
            self.market,
            message.get("code"),
            data.get("action"),
            data.get("action_code"),
            data.get("action_sym"),
            message.get("msg"),
        )
        if str(data.get("action") or "") != ACTION_UNSUBSCRIBE:
            # 구독이 받아들여졌습니다. 앞서 거절된 적이 있으면 지웁니다.
            self._forget_failures(*self._answered(message))

    def _answered(self, message: dict[str, Any]) -> list[StreamKey]:
        """구독 답이 가리키는 구독. 이 연결이 지고 있는 것만 돌려줍니다.

        `data` 에 실시간 이름과 종목이 실려 오면 그것으로 찾습니다. 종목만 알면
        (`data` 에 없고 글에만 `[action_sym::종목]` 이 있으면) 그 종목의 구독을
        전부 가리킵니다. 둘 다 없으면 빈 목록입니다.
        """
        data = message.get("data")
        data = data if isinstance(data, dict) else {}
        code = str(data.get("action_code") or "").strip()
        sym = str(data.get("action_sym") or "").strip()
        if not sym:
            said = str(message.get("message") or message.get("msg") or "")
            found = _SYM_IN_TEXT.search(said)
            sym = found.group(1).strip() if found else ""
        api_id = api_id_of(self.market, code) if code else None
        if api_id is not None:
            key = (self.market, api_id, sym)
            return [key] if key in self._feeds else []
        if not sym:
            return []
        return [key for key in self._feeds if key[2] == sym]

    def _refused(self, code: str, said: str, message: dict[str, Any]) -> None:
        """구독이 거절되었다는 답이면 그 구독을 실패로 들고 화면에 알립니다.

        해제(`D`)에 대한 답은 여기 들지 않습니다 — 그만 보는 구독입니다.
        """
        data = message.get("data")
        if isinstance(data, dict) and str(data.get("action") or "") == ACTION_UNSUBSCRIBE:
            return
        keys = self._answered(message)
        if not keys:
            return
        changed = False
        for key in keys:
            if self._failed.get(key) != (code, said):
                self._failed[key] = (code, said)
                changed = True
        if changed:
            self._tell_failure()

    def _control(self, code: str, message: dict[str, Any]) -> None:
        """자료가 아닌 제어 메시지. 오류 코드면 사전으로 읽어 기록에 남깁니다.

        **구독 상한을 여기서 압니다.** 한 접속키가 아니라 **앱키 하나에 스무
        건**이라, 창을 여럿 띄우면 합쳐서 걸립니다. 그때 서버가
        `EST06` 을 보내는데, 그 글자를 찾는 대신 **코드로** 가립니다.

        상한에 걸리면 기록에 남기고, 답에 어느 구독인지 실려 있으면 그 구독을
        실패로 듭니다(`_refused`). 구독을 스스로 물리거나 차례를 미루지는 않습니다.
        """
        said = str(message.get("message") or message.get("msg") or "").strip()
        trouble = error_codes.describe(code, said)
        # 어느 구독의 답인지 알 수 있으면 그 구독을 실패로 듭니다(첫머리 「서버가
        # 거절한 구독」). 알 수 없으면 기록만 남깁니다.
        self._refused(code, said, message)

        if code == error_codes.SUBSCRIPTION_LIMIT:
            logger.warning(
                "%s 실시간 구독 상한에 걸렸습니다(앱키 하나에 스무 건): %s",
                self.market,
                trouble["message"],
            )
            return

        if trouble["kind"] == error_codes.OK or not error_codes.message_of(code):
            # 사전에 없는 코드도 남깁니다. 구독 요청에 대한 서버의 답일 수 있습니다.
            logger.warning("%s 실시간 제어 메시지: %s %r", self.market, code, said)
            return

        logger.warning(
            "%s 실시간 %s: %s (%s)",
            self.market,
            code,
            trouble["message"],
            trouble["kind"],
        )


def _is_pingpong(message: dict[str, Any]) -> bool:
    """세션 유지 신호인지. 대소문자는 가리지 않습니다 — 되돌리는 것은 받은 그대로입니다."""
    return str(message.get("code") or "").lower() == PINGPONG_CODE


def _close_frame(frame: Any) -> str:
    """닫기 프레임을 기록용 글자로. 없으면 `none`."""
    if frame is None:
        return "none"
    return f"{getattr(frame, 'code', '?')} {getattr(frame, 'reason', '')!r}"


async def _say(socket: Any, payload: dict[str, Any]) -> None:
    """보내 보고, 끊겼으면 그냥 넘어갑니다."""
    try:
        await socket.send(json.dumps(payload, ensure_ascii=False))
    except (WebSocketException, OSError, RuntimeError):
        pass


async def _echo(socket: Any, message: dict[str, Any]) -> None:
    await _say(socket, message)


async def _close_quietly(socket: Any) -> None:
    try:
        await socket.close()
    except (WebSocketException, OSError, RuntimeError):
        pass


def _decode(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        message = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return message if isinstance(message, dict) else None


def _ws_base(base_url: str) -> str:
    """`https://` 주소를 `wss://` 로."""
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://") :]
    if base_url.startswith("http://"):
        return "ws://" + base_url[len("http://") :]
    return base_url
