"""인증 기능 실행 예제.

실행:
    uv run python -m backend.auth.auth_examples

설정 파일(~/NHFutures/config/config.yaml)에 인증 정보가 입력되어 있어야 합니다.
토큰 값은 그대로 출력하지 않고 앞뒤 일부만 표시합니다.
"""
from __future__ import annotations

from backend.auth.auth_functions import AuthError, TokenManager
from backend.config import ConfigError, load_config


def preview(value: str) -> str:
    """값을 그대로 노출하지 않고 길이와 앞뒤 일부만 보여줍니다."""
    if not value:
        return "(비어 있음)"
    if len(value) <= 8:
        return f"{'*' * len(value)} ({len(value)}자)"
    return f"{value[:4]}...{value[-4:]} ({len(value)}자)"


def main() -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        print(exc)
        return

    print(f"기본 환경: {config.default_environment}")
    print(f"사용 가능한 조합: {', '.join(f'{e} > {m}' for e, m in config.configured)}\n")

    manager = TokenManager(config)

    for environment, market in config.configured:
        env = config.environment(environment)
        print(f"[{environment} > {market}] {env.base_url}")

        try:
            # 접속 토큰 발급
            token = manager.token(market, environment)
            print(f"  접속 토큰   : {preview(token.access_token)}")
            print(f"  토큰 타입   : {token.token_type}")
            print(f"  만료 시각   : {token.expires_at:%Y-%m-%d %H:%M:%S}")

            # 같은 조합을 다시 요청하면 보관 중인 토큰을 재사용한다.
            assert manager.token(market, environment) is token
            print("  재요청      : 보관 중인 토큰 재사용")

            # 웹소켓 접속키 발급
            key = manager.websocket_key(market, environment)
            print(f"  웹소켓 키   : {preview(key)}")
        except AuthError as exc:
            print(f"  실패: {exc}")

        print()


if __name__ == "__main__":
    main()
