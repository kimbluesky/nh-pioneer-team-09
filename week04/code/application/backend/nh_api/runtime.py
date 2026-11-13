"""앱이 하나 들고 쓰는 접속 자리.

카테고리별 API 함수는 전부 이 하나를 거칩니다. 매번 새로 만들면 접속 토큰을
매번 다시 발급받게 되고, 호출 제한을 세는 자리도 요청마다 흩어져 제 몫을
지키지 못합니다.

실서버로 갈지 저장해 둔 응답으로 답할지는 `session` 이 정합니다. 여기서는
「하나만 만든다」만 지킵니다.

**활성 환경도 여기서 답합니다.** 모의로 갈지 실전으로 갈지는 이 접속 자리가 든
설정의 기본 환경 하나가 정합니다 — 화면 배지도, 조회도, 주문도, 실시간도 전부
이 값을 따릅니다. 화면이 따로 기억한 값과 서버가 실제로 쓰는 값이 갈리면
모의라 믿고 낸 주문이 실전으로 나갈 수 있어, 기준을 한 자리에 둡니다.

**전환할 때 이 자리를 새로 만들지 않습니다.** 실시간 허브가 이 접속 자리의
세션을 붙들고 있어, 새 세션을 만들면 허브는 닫힌 접속으로 접속키를 받으려다
실패합니다. 설정만 제자리에서 갈아 끼웁니다(`Session.reload`).
"""
from __future__ import annotations

import dataclasses
import threading
from typing import Any

from backend.config import ConfigError, load_config
from backend.nh_api.client import TradeClient, last_environment
from backend.nh_api.session import Session, create_session

# 응답이 어느 환경의 값인지 적는 헤더. 화면은 이것으로 옛 환경 응답을 가립니다.
ENVIRONMENT_HEADER = "x-nh-environment"

_lock = threading.Lock()
_client: TradeClient | None = None

# 환경을 바꾸는 중인지. 주문 라우트가 이것을 보고 그동안 주문을 받지 않습니다 —
# 바꾸는 사이에 낸 주문은 어느 환경으로 나갔는지 뒤에 가리기 어렵습니다.
_switching = False


def client() -> TradeClient:
    """거래 API 호출부. 처음 부를 때 한 번 만듭니다.

    Raises:
        ConfigError: 실서버 모드인데 설정 파일을 읽을 수 없는 경우.
    """
    global _client
    with _lock:
        if _client is None:
            _client = TradeClient(create_session())
        return _client


def use(session: Session) -> TradeClient:
    """쓸 접속 상태를 지정합니다. 테스트와 오프라인 실행에서 씁니다."""
    global _client
    with _lock:
        _client = TradeClient(session)
        return _client


def reset() -> None:
    """만들어 둔 것을 버립니다. 다음에 부를 때 새로 만듭니다."""
    global _client, _switching
    with _lock:
        if _client is not None:
            _client.session.close()
        _client = None
        _switching = False


def forget_token(market: str, environment: str) -> None:
    """들고 있는 접속 토큰 하나를 버립니다. 아직 만든 것이 없으면 아무 일도 안 합니다.

    설정 화면에서 인증 정보가 바뀌었을 때 부릅니다 — 옛 키로 받은 토큰을
    그대로 쓰면 다음 요청이 거절됩니다.
    """
    with _lock:
        if _client is not None:
            _client.session.tokens.forget(market, environment)


# ── 활성 환경 ────────────────────────────────────────────────


def active_environment() -> str | None:
    """지금 요청이 나가는 환경. 설정을 읽지 못해 접속 자리를 못 만들면 `None`.

    `None` 이면 어떤 요청도 NH선물로 나가지 못하는 상태입니다. 화면은 이때
    실전으로 가정하지 말고 「모름」으로 두어야 합니다.
    """
    try:
        return client().session.config.default_environment
    except ConfigError:
        return None


def replaying() -> bool:
    """저장해 둔 응답으로 도는 중인지. 설정을 읽지 못해 접속 자리를 못 만들면 실서버로 봅니다.

    설정이 없는 실서버 기동에서도 종목 마스터처럼 인증이 필요 없는 일은 해야 하므로,
    「모름」을 실서버 쪽으로 둡니다.
    """
    try:
        return client().session.is_replay
    except ConfigError:
        return False


def held_session() -> Session | None:
    """이미 만들어 둔 접속 자리의 세션. 아직 없으면 만들지 않고 `None`."""
    with _lock:
        return _client.session if _client is not None else None


def _held_environment() -> str | None:
    """이미 만들어 둔 접속 자리의 환경. 아직 없으면 만들지 않고 `None`."""
    with _lock:
        if _client is None:
            return None
        return _client.session.config.default_environment


def stamp_environment(response: Any) -> None:
    """응답에 어느 환경의 값인지 적습니다.

    NH선물을 부른 응답에는 **그 요청이 나간 환경**을 적습니다 — 환경을 바꾸기
    직전에 나가 바뀐 뒤 도착한 응답이 새 환경 값처럼 보이지 않게 하려는
    것입니다. 부르지 않은 응답(거절·오류)에는 그때의 활성 환경을 적고, 접속
    자리가 아직 없어 모르면 안 적습니다.
    """
    environment = last_environment.get() or _held_environment()
    if environment:
        response.headers[ENVIRONMENT_HEADER] = environment


def reload_config() -> None:
    """설정 파일을 다시 읽어 들고 있는 접속 자리에 넣습니다. **활성 환경은 그대로.**

    설정 화면에서 인증 정보를 저장한 뒤 부릅니다. 안 부르면 접속 자리는 앱을
    켤 때 읽은 옛 키로 계속 발급받아, 새 키를 넣었는데도 다시 켜기 전까지
    거절당합니다.

    파일의 기본 환경은 안 따릅니다 — 환경을 바꾸는 길은 `activate` 하나이고,
    그 길만이 실시간 연결까지 갈아 끼웁니다. 저장해 둔 응답으로 도는 중이면
    파일을 보지 않습니다. 접속 자리가 아직 없으면 할 일이 없습니다 — 처음
    만들 때 파일을 읽습니다.

    Raises:
        ConfigError: 파일을 읽을 수 없거나 인증 정보가 하나도 없는 경우.
    """
    with _lock:
        current = _client
    if current is None or current.session.is_replay:
        return
    fresh = load_config()
    # 지금 환경을 읽어 새 설정에 싣는 것과 갈아 끼우는 것은 한 잠금 안에서 —
    # 그 사이에 환경이 바뀌면 옛 환경을 새 설정에 실어 되돌려 놓게 됩니다.
    with _lock:
        current.session.reload(
            dataclasses.replace(
                fresh, default_environment=current.session.config.default_environment
            )
        )


def activate(environment: str) -> None:
    """활성 환경을 바꿉니다. 같은 값이면 아무 일도 안 합니다.

    실시간 연결은 여기서 안 건드립니다 — 연결은 만들 때의 환경으로 고정되어
    있어 허브가 갈아 끼워야 하고, 그 순서는 부르는 쪽(`backend.main`)이 지킵니다.

    Raises:
        ConfigError: 실서버 모드인데 설정 파일을 읽을 수 없는 경우.
    """
    session = client().session
    with _lock:
        if session.config.default_environment == environment:
            return
        session.reload(dataclasses.replace(session.config, default_environment=environment))


def begin_switch() -> bool:
    """전환을 시작합니다. 이미 다른 전환이 진행 중이면 `False`."""
    global _switching
    with _lock:
        if _switching:
            return False
        _switching = True
        return True


def end_switch() -> None:
    """전환을 끝냅니다. 성공했든 못 했든 반드시 부릅니다."""
    global _switching
    with _lock:
        _switching = False


def switching() -> bool:
    """환경을 바꾸는 중인지. 주문 라우트가 봅니다."""
    with _lock:
        return _switching
