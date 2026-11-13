"""시장 참고 자료를 어느 환경에서 받을지 고르는 자리.

거래시간표, 해외 품목별 세부정보(계약단위·틱·틱값), 해외 만기일, 국내 투자자
매매동향은 **계좌가 아니라 시장의 값**입니다. 모의 서버는 품목 목록과 거래시간이
실전과 다를 수 있어, 넷 모두 화면에서 고른 환경과 상관없이 **실전 서버에서 받고**,
그 시장의 실전 인증 정보가 없을 때만 모의 서버에서 받습니다.

받은 환경과 화면에서 고른 환경이 달라도, 이 자료를 내는 응답의 `x-nh-environment`
헤더에는 **고른 환경**이 적힙니다. 화면은 그 헤더로 다른 환경의 응답을 버리는데,
이 자료는 고른 환경과 무관하게 한 벌로 쓰는 것이라 환경을 바꿀 때 버릴 자료가
아니기 때문입니다.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from backend.config import (
    CONFIG_PATH,
    LIVE_ENVIRONMENT,
    MARKET_LABELS,
    PAPER_ENVIRONMENT,
    Config,
    ConfigError,
)
from backend.nh_api.client import TradeClient, last_environment

#: 참고 자료를 받을 환경의 차례. 그 시장의 인증 정보가 있는 첫 환경에서 받습니다.
SOURCE_ENVIRONMENTS = (LIVE_ENVIRONMENT, PAPER_ENVIRONMENT)

#: 인증 정보가 없을 때 라우트가 내는 상태 코드. 요청이 잘못된 것이 아니라 이 앱이
#: 아직 그 시장을 부를 준비가 안 된 것이고, 설정을 채우면 같은 요청이 됩니다.
MISSING_KEYS_STATUS = 503


class MissingMarketKeys(ConfigError):
    """그 시장의 인증 정보가 실전·모의 어디에도 없어 참고 자료를 받을 수 없습니다.

    문구(`str(exc)`)는 **화면에 그대로 섭니다** — 무엇을 어디에 넣으면 되는지를
    말합니다. 인증 정보 값은 담지 않습니다.
    """

    def __init__(self, market: str) -> None:
        self.market = market
        label = MARKET_LABELS.get(market, market)
        super().__init__(
            f"설정 파일에 {label} 인증 정보가 없습니다 — {CONFIG_PATH} 의 "
            f"{LIVE_ENVIRONMENT}(실전) 또는 {PAPER_ENVIRONMENT}(모의) > {market} 항목에 "
            "app_key 와 app_secret 을 넣어 주세요."
        )


def source_environment(config: Config, market: str) -> str:
    """그 시장의 참고 자료를 받을 환경. 실전 인증 정보가 있으면 실전, 없으면 모의입니다.

    Raises:
        MissingMarketKeys: 어느 환경에도 그 시장의 인증 정보가 없는 경우.
    """
    for environment in SOURCE_ENVIRONMENTS:
        settings = config.environments.get(environment)
        if settings is not None and market in settings.configured_markets:
            return environment
    raise MissingMarketKeys(market)


@contextmanager
def reference_environment(api: TradeClient, market: str) -> Iterator[str]:
    """그 시장의 참고 자료를 받을 환경을 내주고, 끝나면 헤더에 적힐 환경을 되돌립니다.

    호출부는 부를 때마다 부른 환경을 `last_environment` 에 적고, 라우트는 그 값을
    `x-nh-environment` 헤더로 옮깁니다. 이 블록을 나가면 그 값이 들어오기 전의 값으로,
    비어 있었으면 이 호출부의 활성 환경으로 돌아갑니다 — 블록 안에서 받은 환경이
    헤더에 남지 않습니다.

    Raises:
        MissingMarketKeys: 어느 환경에도 그 시장의 인증 정보가 없는 경우. 부르기 전에 납니다.
    """
    config = api.session.config
    environment = source_environment(config, market)
    before = last_environment.get() or config.default_environment
    try:
        yield environment
    finally:
        last_environment.set(before)
