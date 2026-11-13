"""접속 토큰 발급, 폐기, 웹소켓 접속키 발급.

APP Key가 국내와 해외 각각 별도로 발급되므로 접속 토큰도 시장마다 따로 받아야 합니다.
`TokenManager` 는 (환경, 시장) 조합별로 토큰을 보관하고 만료가 가까워지면 다시 발급받습니다.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from backend.config import Config, MarketConfig
from backend.auth.token_store import Stored, TokenStore, fingerprint, key_of

TOKEN_PATH = "/auth-service/v1/token"
REVOKE_PATH = "/auth-service/v1/revoke"
WEBSOCKET_KEY_PATH = "/permission/v1/web-socket-key"

GRANT_TYPE = "client_credentials"

# 만료 시각 형식이 명시되어 있지 않아 흔히 쓰이는 형태를 순서대로 시도합니다.
EXPIRES_AT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y%m%d%H%M%S",
)

DEFAULT_TIMEOUT = 10.0

# 만료 직전에 요청이 나가지 않도록 이만큼 앞서 다시 발급받습니다.
DEFAULT_REFRESH_MARGIN = timedelta(minutes=1)

# 만료 정보를 해석할 수 없을 때 사용할 유효 기간.
FALLBACK_LIFETIME = timedelta(minutes=10)


class AuthError(Exception):
    """인증 요청이 실패했을 때 발생합니다."""


@dataclass(frozen=True)
class Token:
    """발급받은 접속 토큰."""

    access_token: str
    token_type: str
    expires_at: datetime

    @property
    def authorization(self) -> str:
        """`Authorization` 헤더에 넣을 값."""
        return f"{self.token_type} {self.access_token}".strip()

    def is_valid(self, *, margin: timedelta = DEFAULT_REFRESH_MARGIN) -> bool:
        """만료까지 `margin` 이상 남아 있는지."""
        return datetime.now() + margin < self.expires_at


def _parse_expires_at(payload: dict[str, Any]) -> datetime:
    """응답에서 만료 시각을 읽습니다.

    `expires_at` 을 우선 사용하고, 해석할 수 없으면 `expires_in`(초)으로 계산합니다.
    둘 다 읽을 수 없으면 짧은 기본값을 적용해 다음 호출에서 다시 발급받게 합니다.
    """
    raw = str(payload.get("expires_at") or "").strip()
    if raw:
        for fmt in EXPIRES_AT_FORMATS:
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue

    expires_in = str(payload.get("expires_in") or "").strip()
    if expires_in:
        try:
            return datetime.now() + timedelta(seconds=int(float(expires_in)))
        except ValueError:
            pass

    return datetime.now() + FALLBACK_LIFETIME


def _post(
    client: httpx.Client,
    url: str,
    payload: dict[str, str],
    what: str,
    *,
    form: bool,
    headers: dict[str, str] | None = None,
    timeout: Any = httpx.USE_CLIENT_DEFAULT,
) -> dict[str, Any]:
    """엔드포인트마다 받는 형식이 다르므로 `form` 으로 지정합니다.

    `timeout` 을 주지 않으면 `client` 의 기본 시간 제한을 씁니다.

    토큰 발급만 form 형식을 받고(JSON 으로 보내면 본문을 읽지 못해 "필수 파라미터 누락"),
    폐기를 포함한 나머지는 JSON 만 받습니다(form 으로 보내면 415).
    """
    body = {"data": payload} if form else {"json": payload}
    try:
        response = client.post(url, headers=headers, timeout=timeout, **body)
    except httpx.HTTPError as exc:
        raise AuthError(f"{what} 요청에 실패했습니다: {exc}") from exc

    if response.status_code >= 400:
        raise AuthError(
            f"{what} 요청이 거부되었습니다. (HTTP {response.status_code})\n{response.text.strip()}"
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise AuthError(f"{what} 응답을 해석할 수 없습니다: {response.text.strip()}") from exc

    if not isinstance(body, dict):
        raise AuthError(f"{what} 응답 형식이 올바르지 않습니다: {body!r}")
    return body


def issue_token(
    market: MarketConfig,
    base_url: str,
    *,
    client: httpx.Client | None = None,
) -> Token:
    """접속 토큰을 발급받습니다."""
    payload = {
        "grant_type": GRANT_TYPE,
        "appkey": market.app_key,
        "appsecret": market.app_secret,
    }

    owned = client is None
    client = client or httpx.Client(timeout=DEFAULT_TIMEOUT)
    try:
        body = _post(
            client,
            f"{base_url}{TOKEN_PATH}",
            payload,
            f"{market.label} 접속 토큰 발급",
            form=True,
        )
    finally:
        if owned:
            client.close()

    access_token = str(body.get("access_token") or "").strip()
    if not access_token:
        raise AuthError(f"{market.label} 접속 토큰 발급 응답에 access_token 이 없습니다: {body!r}")

    return Token(
        access_token=access_token,
        token_type=str(body.get("token_type") or "Bearer").strip(),
        expires_at=_parse_expires_at(body),
    )


def revoke_token(
    market: MarketConfig,
    base_url: str,
    token: Token,
    *,
    client: httpx.Client | None = None,
    timeout: Any = httpx.USE_CLIENT_DEFAULT,
) -> None:
    """발급받은 접속 토큰을 폐기합니다.

    명세의 입력 항목에는 앱키와 앱시크릿만 있지만, 실제로는 폐기할 토큰을
    `Authorization` 헤더로 함께 보내야 합니다. **토큰을 받을 때 쓴 키**로
    폐기해야 합니다. `timeout` 을 주지 않으면 `client` 의 기본값입니다.
    """
    payload = {"appkey": market.app_key, "appsecret": market.app_secret}

    owned = client is None
    client = client or httpx.Client(timeout=DEFAULT_TIMEOUT)
    try:
        _post(
            client,
            f"{base_url}{REVOKE_PATH}",
            payload,
            f"{market.label} 접속 토큰 폐기",
            form=False,
            headers={"Authorization": token.authorization},
            timeout=timeout,
        )
    finally:
        if owned:
            client.close()


def issue_websocket_key(
    token: Token,
    base_url: str,
    *,
    client: httpx.Client | None = None,
) -> str:
    """웹소켓 접속키를 발급받습니다."""
    owned = client is None
    client = client or httpx.Client(timeout=DEFAULT_TIMEOUT)
    try:
        response = client.get(
            f"{base_url}{WEBSOCKET_KEY_PATH}",
            headers={"Authorization": token.authorization},
        )
    except httpx.HTTPError as exc:
        raise AuthError(f"웹소켓 접속키 발급 요청에 실패했습니다: {exc}") from exc
    finally:
        if owned:
            client.close()

    if response.status_code >= 400:
        raise AuthError(
            f"웹소켓 접속키 발급이 거부되었습니다. (HTTP {response.status_code})\n"
            f"{response.text.strip()}"
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise AuthError(f"웹소켓 접속키 응답을 해석할 수 없습니다: {response.text.strip()}") from exc

    access_key = str((body or {}).get("access_key") or "").strip()
    if not access_key:
        raise AuthError(f"웹소켓 접속키 응답에 access_key 가 없습니다: {body!r}")
    return access_key


class TokenManager:
    """(환경, 시장) 조합별로 접속 토큰을 보관하고 필요할 때 다시 발급받습니다.

    **파일에도 남길 수 있습니다.** `store` 를 주면 발급받은 토큰을 사용자 홈에
    적어 두고, 다음 실행에서 그것을 먼저 봅니다. 앱을 껐다 켤 때마다 시장 수만큼
    다시 발급받지 않게 하는 것이 목적입니다. 무엇을 어떻게 담는지는
    `token_store.py` 첫머리에 있습니다.

    **재생 모드에서는 `store` 를 주지 않습니다.** 만들어 낸 토큰이 파일에 남으면
    다음 실행에서 진짜인 척 섞입니다.
    """

    def __init__(
        self,
        config: Config,
        *,
        refresh_margin: timedelta = DEFAULT_REFRESH_MARGIN,
        client: httpx.Client | None = None,
        store: TokenStore | None = None,
    ) -> None:
        self._config = config
        self._refresh_margin = refresh_margin
        self._client = client
        self._store = store
        self._tokens: dict[tuple[str, str], Token] = {}
        # 여러 곳에서 동시에 물어봅니다. 조회는 스레드에서, 실시간은 연결마다
        # 부릅니다. 잠그지 않으면 같은 시장의 토큰을 한꺼번에 여러 번 발급받아
        # 인증 호출 제한에 걸립니다.
        #
        # **잠금이 둘입니다.** `_lock` 은 담아 둔 것(토큰·설정·자리 잠금 목록)을
        # 읽고 쓰는 짧은 순간만 잡습니다. 발급처럼 서버를 기다리는 일은 (환경,
        # 시장)마다 따로 둔 자리 잠금(`_slot`)만 잡습니다 — 한 시장의 토큰을 받는
        # 동안 다른 시장의 조회까지 멈추지 않게 합니다. 둘을 함께 잡을 때는 늘
        # 자리 잠금이 먼저입니다.
        self._lock = threading.Lock()
        self._slots: dict[tuple[str, str], threading.Lock] = {}

    def _slot(self, key: tuple[str, str]) -> threading.Lock:
        """그 (환경, 시장)의 자리 잠금. 처음 물으면 만듭니다."""
        with self._lock:
            found = self._slots.get(key)
            if found is None:
                found = self._slots[key] = threading.Lock()
            return found

    def _cached(self, key: tuple[str, str], *, margin: timedelta) -> Token | None:
        with self._lock:
            cached = self._tokens.get(key)
        return cached if cached is not None and cached.is_valid(margin=margin) else None

    def _remember(
        self, key: tuple[str, str], market_config: MarketConfig, token: Token, *, store: bool
    ) -> None:
        """받은 토큰을 담습니다. **그 사이 설정이 바뀌었으면 담지 않습니다** — 옛
        키로 받은 토큰이 새 설정 자리에 남으면 다음 요청이 거절당합니다."""
        environment, market = key
        with self._lock:
            now = _credentials_of(self._config, environment, market)
            if now != (market_config.app_key, market_config.app_secret):
                return
            self._tokens[key] = token
        if store:
            self._to_store(environment, market, market_config, token)

    def token(self, market: str, environment: str | None = None) -> Token:
        """유효한 접속 토큰을 반환합니다. 없거나 만료가 가까우면 다시 발급받습니다.

        같은 (환경, 시장)을 여럿이 동시에 물어봐도 **발급은 한 번**입니다.
        """
        environment = environment or self._config.default_environment
        key = (environment, market)

        cached = self._cached(key, margin=self._refresh_margin)
        if cached is not None:
            return cached

        with self._slot(key):
            # 자리를 기다리는 사이 다른 쪽이 받아 두었으면 그것을 씁니다.
            cached = self._cached(key, margin=self._refresh_margin)
            if cached is not None:
                return cached

            with self._lock:
                config = self._config
            env = config.environment(environment)
            market_config = env.market(market)

            kept = self._from_store(environment, market, market_config)
            if kept is not None:
                self._remember(key, market_config, kept, store=False)
                return kept

            issued = issue_token(market_config, env.base_url, client=self._client)
            self._remember(key, market_config, issued, store=True)
            return issued

    def _from_store(
        self, environment: str, market: str, market_config: MarketConfig
    ) -> Token | None:
        """파일에 담아 둔 토큰. 쓸 수 없으면 `None`.

        **버리는 자리가 둘입니다.** 만료가 가까우면 버리고, **APP Key 가
        바뀌었으면** 버립니다. 옛 키로 받은 토큰은 서버가 안 받습니다.
        """
        if self._store is None:
            return None

        kept = self._store.read().get(key_of(environment, market))
        if kept is None:
            return None

        if kept.fingerprint != fingerprint(market_config.app_key):
            self._store.drop(key_of(environment, market))
            return None

        token = Token(
            access_token=kept.access_token,
            token_type=kept.token_type,
            expires_at=kept.expires_at,
        )
        if not token.is_valid(margin=self._refresh_margin):
            self._store.drop(key_of(environment, market))
            return None
        return token

    def _to_store(
        self, environment: str, market: str, market_config: MarketConfig, token: Token
    ) -> None:
        """새로 받은 토큰을 파일에 적어 둡니다. 못 적어도 앱은 그대로 돕니다."""
        if self._store is None:
            return
        try:
            self._store.put(
                key_of(environment, market),
                Stored(
                    access_token=token.access_token,
                    token_type=token.token_type,
                    expires_at=token.expires_at,
                    fingerprint=fingerprint(market_config.app_key),
                ),
            )
        except OSError:
            # 파일에 못 남기는 것은 이번 실행에 손해가 없습니다. 메모리에는
            # 있고, 다음 실행에서 다시 받으면 됩니다.
            pass

    def forget(self, market: str, environment: str | None = None) -> None:
        """그 자리의 토큰을 버립니다. **서버에 폐기를 청하지는 않습니다.**

        APP Key 가 바뀌었거나 서버가 그 토큰을 거절했을 때 부릅니다.
        """
        environment = environment or self._config.default_environment
        key = (environment, market)
        with self._slot(key):
            with self._lock:
                self._tokens.pop(key, None)
            if self._store is not None:
                self._store.drop(key_of(environment, market))

    def use_config(self, config: Config) -> None:
        """설정을 갈아 끼웁니다. 앱을 다시 켜지 않고 새 설정으로 발급받게 합니다.

        **인증 정보가 바뀐 자리의 토큰은 버립니다.** 메모리에 든 토큰은 발급받을
        때의 키를 기억하지 않아, 그대로 두면 키가 바뀌어도 옛 토큰을 계속
        돌려줍니다. 기본 환경만 바뀐 경우에는 아무 토큰도 버리지 않습니다 —
        토큰은 (환경, 시장)마다 따로라 환경을 옮겨도 그대로 쓸 수 있습니다.
        """
        with self._lock:
            before = self._config
            self._config = config
            for environment, market in list(self._tokens):
                if _credentials_of(before, environment, market) != _credentials_of(
                    config, environment, market
                ):
                    self._tokens.pop((environment, market), None)
                    if self._store is not None:
                        self._store.drop(key_of(environment, market))

    def authorization(self, market: str, environment: str | None = None) -> str:
        """`Authorization` 헤더 값을 반환합니다."""
        return self.token(market, environment).authorization

    def headers(self, market: str, environment: str | None = None) -> dict[str, str]:
        """인증이 필요한 요청에 붙일 헤더를 반환합니다."""
        return {"Authorization": self.authorization(market, environment)}

    def websocket_key(self, market: str, environment: str | None = None) -> str:
        """웹소켓 접속키를 발급받습니다."""
        environment = environment or self._config.default_environment
        env = self._config.environment(environment)
        return issue_websocket_key(self.token(market, environment), env.base_url, client=self._client)

    def held(
        self,
        market: str,
        environment: str,
        *,
        app_key: str,
        store: TokenStore | None = None,
    ) -> Token | None:
        """그 키로 **이미 받아 둔** 토큰. 없으면 `None` 이고, **새로 발급하지 않습니다.**

        메모리에 든 것을 먼저 보고(지금 설정의 키가 `app_key` 일 때만 — 메모리
        토큰은 지금 설정의 키로 받은 것입니다), 없으면 파일(`store`, 주지 않으면
        이 관리자의 보관 자리)에서 그 키의 지문과 같은 것을 찾습니다. 만료된 것은
        돌려주지 않습니다.
        """
        with self._lock:
            now_key = _credentials_of(self._config, environment, market)
            cached = self._tokens.get((environment, market))
        if (
            cached is not None
            and now_key is not None
            and now_key[0] == app_key
            and cached.is_valid(margin=timedelta(0))
        ):
            return cached
        return self._kept(environment, market, app_key, store if store is not None else self._store)

    def _kept(
        self, environment: str, market: str, app_key: str, store: TokenStore | None
    ) -> Token | None:
        """파일에 담긴 그 키의 토큰(지문이 같고 만료 전). 없으면 `None`. 버리지 않습니다."""
        if store is None:
            return None
        kept = store.read().get(key_of(environment, market))
        if kept is None or kept.fingerprint != fingerprint(app_key):
            return None
        token = Token(
            access_token=kept.access_token,
            token_type=kept.token_type,
            expires_at=kept.expires_at,
        )
        return token if token.is_valid(margin=timedelta(0)) else None

    def reissue(
        self,
        market: str,
        environment: str,
        *,
        revoke_timeout: Any = httpx.USE_CLIENT_DEFAULT,
    ) -> Reissued:
        """그 자리의 토큰을 **폐기하고 같은 키로 새로 받습니다.**

        해외/LME 실시간 시세 이용 내역을 바꾼 뒤에는 이렇게 다시 받아야 바뀐
        종목이 반영됩니다(명세). 차례는 가진 토큰 폐기 → 발급 → 메모리·파일 갱신
        이고, 그동안 **그 자리만** 잠급니다 — 다른 시장의 조회는 기다리지 않습니다.

        폐기가 거절되거나 시간을 넘겨도 발급은 계속합니다(`revoke_failed`). 가진
        토큰이 없으면 폐기 없이 발급만 합니다. 발급이 실패하면 그 자리를 비운 채
        예외를 올립니다 — 다음 요청이 새로 받습니다.

        **같은 컴퓨터에서 켠 다른 실행이 쓰던 토큰도 함께 폐기됩니다.** 토큰
        파일을 함께 쓰기 때문입니다. 그쪽은 다음 요청에서 다시 받습니다.

        Raises:
            ConfigError: 그 자리에 인증 정보가 없는 경우.
            AuthError: 새로 받지 못한 경우.
        """
        key = (environment, market)
        with self._slot(key):
            with self._lock:
                config = self._config
            env = config.environment(environment)
            market_config = env.market(market)

            old = self._cached(key, margin=timedelta(0)) or self._kept(
                environment, market, market_config.app_key, self._store
            )
            revoked = revoke_failed = False
            if old is not None:
                try:
                    revoke_token(
                        market_config, env.base_url, old, client=self._client, timeout=revoke_timeout
                    )
                    revoked = True
                except Exception:  # noqa: BLE001 — 폐기를 못 해도 새로 받는 것이 이 일의 목적입니다.
                    revoke_failed = True

            with self._lock:
                self._tokens.pop(key, None)
            if self._store is not None:
                self._store.drop(key_of(environment, market))

            issued = issue_token(market_config, env.base_url, client=self._client)
            self._remember(key, market_config, issued, store=True)
            return Reissued(
                revoked=revoked,
                revoke_failed=revoke_failed,
                same_token=old is not None and old.access_token == issued.access_token,
                expires_at=issued.expires_at,
            )

    def revoke(self, market: str, environment: str | None = None) -> None:
        """접속 토큰을 폐기하고 보관 중인 값을 버립니다."""
        environment = environment or self._config.default_environment
        env = self._config.environment(environment)
        token = self.token(market, environment)
        revoke_token(env.market(market), env.base_url, token, client=self._client)
        with self._lock:
            self._tokens.pop((environment, market), None)
        if self._store is not None:
            self._store.drop(key_of(environment, market))

    def clear(self) -> None:
        """메모리에 든 것만 버립니다.

        **파일은 그대로 둡니다.** 다음에 물어보면 파일에서 읽어 옵니다 — 파일을
        비우려면 `forget()` 이나 `revoke()` 를 쓰세요.
        """
        with self._lock:
            self._tokens.clear()


@dataclass(frozen=True)
class Reissued:
    """토큰을 다시 받은 결과. **토큰 글자는 담지 않습니다.**"""

    #: 가진 토큰을 폐기했는지. 가진 것이 없었으면 거짓입니다.
    revoked: bool
    #: 폐기를 청했는데 거절되거나 시간을 넘겼는지.
    revoke_failed: bool
    #: 새로 받은 토큰이 옛 토큰과 같은 글자인지. 참이면 이용 내역 변경이
    #: 반영되지 않았을 수 있습니다.
    same_token: bool
    expires_at: datetime


def _credentials_of(config: Config, environment: str, market: str) -> tuple[str, str] | None:
    """그 자리의 (APP Key, Secret). 설정에 없는 자리는 `None`."""
    try:
        found = getattr(config.environment(environment), market)
    except (ValueError, AttributeError):
        return None
    return (found.app_key, found.app_secret)
