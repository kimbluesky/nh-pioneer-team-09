"""실서버로 부를지, 저장해 둔 응답으로 답할지 정합니다.

기본은 실서버입니다. 환경변수 `NH_REPLAY` 를 켜면 재생 모드로 뜹니다.

    NH_REPLAY=1                 재생 모드로 실행
    NH_REPLAY_DIR=<폴더>        저장해 둔 응답을 다른 곳에서 읽기 (선택)

설정 파일이 아니라 환경변수인 이유가 있습니다. 재생 모드는 실행할 때 켜고
끄는 개발용 스위치라, 설정 파일에 적어 두면 껐다고 생각하고 켜 둔 채로
쓰기 쉽습니다. 인증 정보를 담는 설정 파일에 개발용 항목을 섞지 않으려는
뜻도 있습니다.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from backend.auth.auth_functions import TokenManager
from backend.auth.token_store import TokenStore
from backend.config import (
    ENVIRONMENTS,
    Config,
    EnvironmentConfig,
    MarketConfig,
    load_config,
)
from backend.nh_api.fixtures import FixtureStore, load_fixtures
from backend.nh_api.replay import replay_base_url, replay_client

REPLAY_FLAG = "NH_REPLAY"
REPLAY_DIR = "NH_REPLAY_DIR"

LIVE = "live"
REPLAY = "replay"

# 재생 모드에서는 인증 정보가 필요 없습니다. 자리를 비워 두면 설정이 없다는
# 오류가 나므로, 실제 값이 아님이 드러나는 문자열을 넣어 둡니다.
REPLAY_PLACEHOLDER = "REPLAY-NOT-A-REAL-KEY"

DEFAULT_TIMEOUT = 10.0

_TRUE = {"1", "true", "yes", "on"}


def replay_enabled(env: Mapping[str, str] | None = None) -> bool:
    """재생 모드로 켜져 있는지."""
    env = os.environ if env is None else env
    return str(env.get(REPLAY_FLAG, "")).strip().lower() in _TRUE


def current_mode(env: Mapping[str, str] | None = None) -> str:
    """지금 어느 쪽으로 도는지. `live` 또는 `replay`."""
    return REPLAY if replay_enabled(env) else LIVE


def fixture_root(env: Mapping[str, str] | None = None) -> Path | None:
    """저장해 둔 응답을 읽을 폴더. 지정하지 않았으면 기본 자리를 씁니다."""
    env = os.environ if env is None else env
    raw = str(env.get(REPLAY_DIR, "")).strip()
    return Path(raw) if raw else None


def replay_config() -> Config:
    """재생 모드에서 쓸 설정. 접속 주소가 전부 재생 주소를 가리킵니다."""
    environments = {
        name: EnvironmentConfig(
            name=name,
            base_url=replay_base_url(name),
            domestic=MarketConfig(name, "domestic", REPLAY_PLACEHOLDER, REPLAY_PLACEHOLDER),
            overseas=MarketConfig(name, "overseas", REPLAY_PLACEHOLDER, REPLAY_PLACEHOLDER),
        )
        for name in ENVIRONMENTS
    }
    # 잘못 눌렀을 때를 생각해 어디서나 모의를 기본으로 둡니다.
    return Config(default_environment="paper", environments=environments)


@dataclass
class Session:
    """한 번의 실행에서 쓰는 접속 상태.

    설정, HTTP 클라이언트, 접속 토큰 보관을 함께 들고 있습니다.
    """

    mode: str
    config: Config
    client: httpx.Client
    tokens: TokenManager
    fixtures: FixtureStore | None = None

    @property
    def is_replay(self) -> bool:
        return self.mode == REPLAY

    def base_url(self, environment: str | None = None) -> str:
        return self.config.environment(environment).base_url

    def reload(self, config: Config) -> None:
        """설정을 갈아 끼웁니다. 앱을 다시 켜지 않고 새 설정으로 돕니다.

        **설정을 드는 자리가 둘입니다** — 여기와 토큰 보관. 한쪽만 바꾸면 요청은
        새 설정의 환경으로 가면서 토큰은 옛 키로 받게 됩니다. 그래서 반드시 이
        한 자리를 통해 함께 바꿉니다.
        """
        self.config = config
        self.tokens.use_config(config)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def create_session(
    *,
    replay: bool | None = None,
    config: Config | None = None,
    fixtures: FixtureStore | None = None,
    root: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Session:
    """이번 실행에서 쓸 접속 상태를 만듭니다.

    `replay` 를 주지 않으면 환경변수를 봅니다.
    """
    use_replay = replay_enabled() if replay is None else replay

    if use_replay:
        store = fixtures or load_fixtures(root if root is not None else fixture_root())
        session_config = config or replay_config()
        client = replay_client(store)
        return Session(
            mode=REPLAY,
            config=session_config,
            client=client,
            tokens=TokenManager(session_config, client=client),
            fixtures=store,
        )

    session_config = config or load_config()
    client = httpx.Client(timeout=timeout)
    return Session(
        mode=LIVE,
        config=session_config,
        client=client,
        # 실서버로 도는 자리에서만 토큰을 파일에 남깁니다. 재생 토큰은 만들어
        # 낸 값이라 남기면 다음 실행에서 진짜인 척 섞입니다.
        tokens=TokenManager(session_config, client=client, store=TokenStore()),
    )
