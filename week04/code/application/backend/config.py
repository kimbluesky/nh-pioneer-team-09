"""사용자 설정 파일 로딩.

설정 파일은 저장소가 아니라 사용자 홈 폴더에 둡니다. APP Key 같은 인증 정보가
실수로 저장소에 커밋되는 것을 막기 위한 것으로, 위치를 프로젝트 안으로 옮기지 마세요.

구조는 세 단계입니다.

    Config              전체 설정. 모든 환경을 함께 들고 있습니다.
      EnvironmentConfig 환경 하나(모의/실전). 접속 주소와 시장별 인증 정보를 가집니다.
        MarketConfig    시장 하나(국내/해외)의 APP Key 와 Secret.

APP Key와 APP Secret Key는 국내와 해외가 각각 별도로 발급되므로 시장 단위로 나눕니다.
따라서 접속 토큰도 시장마다 따로 발급받아야 합니다.

여러 환경을 동시에 사용할 수 있도록, 설정은 환경을 하나로 좁히지 않고 전부 보관합니다.
`default_environment` 는 호출 시 환경을 생략했을 때 쓰일 기본값일 뿐입니다.

"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

# 앱이 사용자 홈에 남기는 것은 전부 이 아래입니다 — 설정 파일도, 웹뷰가 남기는
# 브라우저 저장소도. 한 곳에 모아 두어야 무엇이 남았는지 찾을 수 있고, 지울 때도
# 한 번에 지웁니다.
HOME_DIR = Path.home() / "NHFutures"

CONFIG_PATH = HOME_DIR / "config" / "config.yaml"

# 환경 이름. 설정 파일과 주소 고르기가 이 이름을 씁니다.
#
# **주문도 이 환경으로 나갑니다** — 실전이면 실전으로 접수됩니다. 어느 쪽으로
# 접속할지는 설정 화면에서 고릅니다. 새 설정 파일이 모의로 시작하는 것은
# 아래 `save_markets` 에 적힌 대로 잘못 눌렀을 때를 생각한 기본값입니다.
LIVE_ENVIRONMENT = "real"
PAPER_ENVIRONMENT = "paper"

ENVIRONMENTS = (PAPER_ENVIRONMENT, LIVE_ENVIRONMENT)
MARKETS = ("domestic", "overseas")

# 요청 본문에서 환경 이름을 받는 칸이 쓰는 정규식. 환경이 늘면 여기 한 곳만 바뀝니다.
ENVIRONMENT_PATTERN = "^(" + "|".join(ENVIRONMENTS) + ")$"

DEFAULT_BASE_URLS = {
    "paper": "https://apidemo.futures.co.kr",
    "real": "https://api.futures.co.kr",
}

ENVIRONMENT_LABELS = {"paper": "모의", "real": "실전"}
MARKET_LABELS = {"domestic": "국내", "overseas": "해외"}


class ConfigError(Exception):
    """설정 파일을 읽을 수 없거나 내용이 올바르지 않을 때 발생합니다."""


def _mask(value: str) -> str:
    if not value:
        return "(비어 있음)"
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


# 항목마다 가리는 정도가 다릅니다.
#
#   App Key    앞 4자리만 남깁니다. 「어느 키를 넣었나」를 확인하는 데 쓰는
#              값이라 식별할 만큼은 보여야 합니다.
#   Secret Key 한 글자도 안 보여줍니다. 어느 Secret 인지 구분할 일이 없고,
#              앞뒤 몇 자를 흘릴 이유도 없습니다.
SECRET_MASK = "•" * 8


def _mask_field(field: str, value: str) -> str:
    if not value:
        return ""
    if field == "app_secret":
        return SECRET_MASK
    # 남길 자리보다 짧으면 통째로 가립니다. 안 그러면 짧은 값이 그대로 보입니다.
    if len(value) <= 4:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 4)


@dataclass(frozen=True)
class MarketConfig:
    """한 환경, 한 시장의 인증 정보."""

    environment: str
    market: str
    app_key: str
    app_secret: str

    @property
    def label(self) -> str:
        env = ENVIRONMENT_LABELS.get(self.environment, self.environment)
        market = MARKET_LABELS.get(self.market, self.market)
        return f"{env} {market}"

    @property
    def is_configured(self) -> bool:
        """인증에 필요한 값이 모두 채워져 있는지."""
        return bool(self.app_key and self.app_secret)

    def masked(self) -> dict[str, str]:
        return {
            "app_key": _mask(self.app_key),
            "app_secret": _mask(self.app_secret),
        }


@dataclass(frozen=True)
class EnvironmentConfig:
    """환경 하나의 접속 주소와 시장별 인증 정보."""

    name: str
    base_url: str
    domestic: MarketConfig
    overseas: MarketConfig

    @property
    def label(self) -> str:
        return ENVIRONMENT_LABELS.get(self.name, self.name)

    @property
    def configured_markets(self) -> tuple[str, ...]:
        return tuple(m for m in MARKETS if getattr(self, m).is_configured)

    def market(self, name: str) -> MarketConfig:
        """시장별 인증 정보를 반환합니다.

        Raises:
            ConfigError: 해당 시장의 인증 정보가 설정되어 있지 않은 경우.
        """
        if name not in MARKETS:
            raise ValueError(f"알 수 없는 시장입니다: {name!r} ({' 또는 '.join(MARKETS)})")

        market: MarketConfig = getattr(self, name)
        if not market.is_configured:
            raise ConfigError(
                f"{market.label} 인증 정보가 설정되어 있지 않습니다.\n"
                f"{CONFIG_PATH} 의 {self.name} > {name} 항목에 "
                "app_key 와 app_secret 을 입력해 주세요."
            )
        return market

    def masked(self) -> dict[str, object]:
        return {
            "base_url": self.base_url,
            "configured_markets": list(self.configured_markets),
            "domestic": self.domestic.masked(),
            "overseas": self.overseas.masked(),
        }


@dataclass(frozen=True)
class Config:
    """전체 설정. 모든 환경을 함께 들고 있습니다."""

    default_environment: str
    environments: Mapping[str, EnvironmentConfig]

    def environment(self, name: str | None = None) -> EnvironmentConfig:
        """환경 설정을 반환합니다. 이름을 생략하면 기본 환경을 사용합니다."""
        name = name or self.default_environment
        if name not in self.environments:
            raise ValueError(
                f"알 수 없는 환경입니다: {name!r} ({' 또는 '.join(ENVIRONMENTS)})"
            )
        return self.environments[name]

    def market(self, market: str, environment: str | None = None) -> MarketConfig:
        """환경과 시장을 지정해 인증 정보를 반환합니다."""
        return self.environment(environment).market(market)

    @property
    def configured(self) -> tuple[tuple[str, str], ...]:
        """인증 정보가 채워진 (환경, 시장) 조합."""
        return tuple(
            (env_name, market)
            for env_name in ENVIRONMENTS
            for market in self.environments[env_name].configured_markets
        )

    def masked(self) -> dict[str, object]:
        """로그나 화면에 표시하기 위한 형태. 인증 정보는 가려서 반환합니다."""
        return {
            "default_environment": self.default_environment,
            "configured": [f"{e} > {m}" for e, m in self.configured],
            **{name: env.masked() for name, env in self.environments.items()},
        }


def _read_market(section: Mapping, environment: str, market: str) -> MarketConfig:
    raw = section.get(market)
    if not isinstance(raw, dict):
        raw = {}
    return MarketConfig(
        environment=environment,
        market=market,
        app_key=str(raw.get("app_key") or "").strip(),
        app_secret=str(raw.get("app_secret") or "").strip(),
    )


def _read_environment(raw: Mapping, name: str) -> EnvironmentConfig:
    section = raw.get(name)
    if not isinstance(section, dict):
        section = {}

    base_url = str(section.get("base_url") or "").strip() or DEFAULT_BASE_URLS[name]

    return EnvironmentConfig(
        name=name,
        base_url=base_url.rstrip("/"),
        domestic=_read_market(section, name, "domestic"),
        overseas=_read_market(section, name, "overseas"),
    )


def load_config(path: Path | None = None) -> Config:
    """설정 파일을 읽어 전체 설정을 반환합니다.

    Raises:
        ConfigError: 파일이 없거나, 형식이 잘못되었거나, 사용할 수 있는 인증 정보가 없는 경우.
    """
    path = path or CONFIG_PATH

    if not path.is_file():
        raise ConfigError(
            f"설정 파일을 찾을 수 없습니다: {path}\n"
            "저장소의 config.example.yaml 을 위 경로로 복사한 뒤 값을 입력해 주세요."
        )

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"설정 파일을 해석할 수 없습니다: {path}\n{exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"설정 파일의 내용이 올바르지 않습니다: {path}")

    default_environment = str(raw.get("default_environment") or "").strip()
    if default_environment not in ENVIRONMENTS:
        raise ConfigError(
            f"default_environment 값이 올바르지 않습니다: {default_environment!r}\n"
            f"{' 또는 '.join(ENVIRONMENTS)} 중 하나여야 합니다."
        )

    environments = {name: _read_environment(raw, name) for name in ENVIRONMENTS}

    config = Config(default_environment=default_environment, environments=environments)

    # 어느 환경에도 인증 정보가 없으면 아무 작업도 할 수 없으므로 이때는 막습니다.
    # 일부만 채워진 상태는 정상으로 봅니다. (국내만 이용하거나 모의만 이용하는 경우)
    if not config.configured:
        raise ConfigError(
            "사용할 수 있는 인증 정보가 없습니다.\n"
            f"{path} 의 환경(paper 또는 real) 아래 domestic 또는 overseas 항목에 "
            "app_key 와 app_secret 을 입력해 주세요."
        )

    return config


def config_view(path: Path | None = None) -> dict[str, object]:
    """설정 화면에 보여줄 형태로 읽습니다.

    `load_config` 와 달리 파일이 없거나 인증 정보가 하나도 없어도 예외를 내지
    않습니다. 설정 화면은 값을 처음 넣는 자리이기도 해서, 비어 있는 상태를
    오류가 아니라 「아직 안 넣음」으로 다뤄야 합니다.

    값 자체는 절대 나가지 않습니다. 채워져 있는지(`set`)와 가린 표시(`masked`)만
    보냅니다.
    """
    path = path or CONFIG_PATH
    raw: Mapping = {}
    if path.is_file():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"설정 파일을 해석할 수 없습니다: {path}\n{exc}") from exc
        if isinstance(loaded, dict):
            raw = loaded

    environments: dict[str, object] = {}
    for env_name in ENVIRONMENTS:
        env = _read_environment(raw, env_name)
        markets: dict[str, object] = {}
        for market_name in MARKETS:
            market: MarketConfig = getattr(env, market_name)
            markets[market_name] = {
                field: {"set": bool(value), "masked": _mask_field(field, value)}
                for field, value in (
                    ("app_key", market.app_key),
                    ("app_secret", market.app_secret),
                )
            }
        environments[env_name] = markets

    default_environment = str(raw.get("default_environment") or "").strip()
    return {
        "default_environment": default_environment if default_environment in ENVIRONMENTS else "paper",
        "environments": environments,
    }


def save_markets(updates: Mapping[str, Mapping[str, Mapping[str, str]]], path: Path | None = None) -> None:
    """바뀐 인증 정보만 설정 파일에 씁니다.

    `updates` 는 `{환경: {시장: {항목: 값}}}` 이고, **들어 있는 항목만** 바꿉니다.
    빠진 항목은 파일에 있던 값을 그대로 둡니다.

    화면이 가린 값을 되돌려 보내 덮어쓰는 사고를 구조적으로 막기 위한 형태입니다.
    폼 전체를 받으면, 사용자가 한 칸만 고쳐도 나머지 칸의 가린 문자열이 실제 값을
    덮어씁니다 — 화면에는 고치기 전과 똑같이 보이므로 다음에 인증이 깨져야 압니다.

    파일이 없으면 만듭니다. 위치는 사용자 홈이며 저장소 안에는 어떤 경우에도
    만들지 않습니다.
    """
    path = path or CONFIG_PATH

    raw: dict = {}
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded

    for env_name, markets in updates.items():
        if env_name not in ENVIRONMENTS:
            raise ValueError(f"알 수 없는 환경입니다: {env_name!r}")
        env_section = raw.setdefault(env_name, {})
        if not isinstance(env_section, dict):
            env_section = {}
            raw[env_name] = env_section

        for market_name, fields in markets.items():
            if market_name not in MARKETS:
                raise ValueError(f"알 수 없는 시장입니다: {market_name!r}")
            market_section = env_section.setdefault(market_name, {})
            if not isinstance(market_section, dict):
                market_section = {}
                env_section[market_name] = market_section

            for field, value in fields.items():
                if field not in ("app_key", "app_secret"):
                    raise ValueError(f"알 수 없는 항목입니다: {field!r}")
                market_section[field] = str(value).strip()

    if str(raw.get("default_environment") or "").strip() not in ENVIRONMENTS:
        # 처음 만드는 파일이면 모의로 시작합니다. 실전이 기본이면 잘못 눌렀을 때
        # 실제 계좌로 주문이 나갑니다.
        raw["default_environment"] = "paper"

    _write(raw, path)


def save_default_environment(name: str, path: Path | None = None) -> None:
    """기본 환경만 설정 파일에 씁니다. 인증 정보는 건드리지 않습니다.

    앱이 지금 쓰는 환경과 다음 실행이 시작할 환경은 **한 값**입니다. 실행 중에
    전환하면 여기서 파일에도 적어, 껐다 켰을 때 다른 환경으로 서지 않게 합니다.

    Raises:
        ValueError: 모르는 환경 이름.
    """
    if name not in ENVIRONMENTS:
        raise ValueError(f"알 수 없는 환경입니다: {name!r}")
    path = path or CONFIG_PATH

    raw: dict = {}
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded

    raw["default_environment"] = name
    _write(raw, path)


def _write(raw: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
