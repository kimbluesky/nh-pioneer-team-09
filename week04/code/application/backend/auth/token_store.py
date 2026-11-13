"""접속 토큰을 사용자 홈에 보관합니다.

    ~/NHFutures/config/tokens.json

**왜 보관하나.** 토큰은 시장마다 따로 받고 한동안 유효합니다. 메모리에만
들고 있으면 앱을 껐다 켤 때마다 시장 수만큼 다시 발급받습니다. 발급도 호출
제한(초당 3건)을 쓰는 요청이고, 자주 껐다 켜는 사용자에게는 그만큼 낭비입니다.

**왜 `config/` 인가.** 담긴 것이 인증 자료이기 때문입니다. 이 폴더는 「누구에게도
보내지 마세요」가 한 줄로 서는 자리이고, 화면 상태(`state/`)와 섞지 않습니다.
**토큰 파일 하나만 있어도 만료 전까지는 그 계좌로 조회가 됩니다.**

**어느 APP Key 로 받은 것인지 함께 적습니다.** 키가 바뀌면 옛 토큰은 못 씁니다.
그래서 키의 지문(sha256 앞 열여섯 자)을 함께 담고, 지문이 다르면 그 자리를
버립니다. **키 자체는 담지 않습니다** — 되돌릴 수 없는 지문만으로 「같은
키인가」를 가릴 수 있습니다.

**못 읽으면 지웁니다.** 다른 파일(화면 상태)은 못 읽었을 때 옆에 `.broken` 으로
남겨 두지만, 여기서는 남기지 않습니다. 남기면 인증 자료가 파일로 하나 더
굴러다닙니다. 토큰은 다시 받으면 그만이라 잃을 것이 없습니다.

**권한은 POSIX 에서만 뜻이 있습니다.** `0600` 으로 둡니다. Windows 에서는
`os.chmod` 가 읽기전용 표시만 바꾸므로 권한으로 좁힐 수 없고,
파일이 사용자 프로필 아래에 있다는 것에 기댑니다.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.config import HOME_DIR
from backend.storage import BROKEN, read_json, write_json

TOKENS_PATH = HOME_DIR / "config" / "tokens.json"

# 파일 형식. 담는 것이 달라지면 올립니다.
VERSION = 1

# 만료 시각을 적는 형식. 읽을 때는 발급 응답과 같은 형식들을 다 받아 봅니다.
STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# APP Key 지문 길이. 열여섯 자면 다른 키와 겹칠 일이 없습니다.
FINGERPRINT_LENGTH = 16


def fingerprint(app_key: str) -> str:
    """APP Key 의 지문. **키 자체는 어디에도 남기지 않습니다.**"""
    digest = hashlib.sha256(str(app_key or "").encode("utf-8")).hexdigest()
    return digest[:FINGERPRINT_LENGTH]


def default_path() -> Path:
    """토큰 파일 자리.

    상수를 그대로 쓰지 않고 함수로 감싸는 까닭이 있습니다. **시험이 사용자
    홈을 건드리면 안 됩니다.** 시험 준비(`tests/conftest.py`)가 이 함수를
    임시 폴더로 돌려 두면, 어느 시험이 실서버 세션을 만들어도 진짜 파일이
    생기지 않습니다 — 시험마다 조심하는 규율이 아니라 **기본값이 지킵니다.**
    """
    return TOKENS_PATH


def key_of(environment: str, market: str) -> str:
    """파일 안에서 쓰는 자리 이름. 환경과 시장마다 따로 담습니다."""
    return f"{environment}:{market}"


@dataclass(frozen=True)
class Stored:
    """파일에서 읽어 온 토큰 하나."""

    access_token: str
    token_type: str
    expires_at: datetime
    fingerprint: str


class TokenStore:
    """토큰 파일 하나를 읽고 씁니다.

    **재생 모드에서는 이 자리를 아예 만들지 않습니다.** 재생의 토큰은 만들어 낸
    값이라, 파일에 남기면 다음 실행에서 진짜인 척 섞입니다.
    """

    def __init__(self, path: Path | None = None) -> None:
        # 자리를 부를 때 정합니다. 미리 굳혀 두면 시험이 다른 자리를 가리킬 수
        # 없어, 사용자 홈의 진짜 파일을 건드리게 됩니다.
        self.path = default_path() if path is None else path

    def read(self) -> dict[str, Stored]:
        """담긴 것을 모두 읽습니다. 없거나 못 읽으면 빈 것으로 답합니다."""
        loaded, state = read_json(self.path, keep=False)
        if state == BROKEN or not loaded:
            return {}
        if loaded.get("version") != VERSION:
            # 형식이 다르면 읽지 않습니다. 토큰은 다시 받으면 그만입니다.
            return {}

        entries = loaded.get("tokens")
        if not isinstance(entries, dict):
            return {}

        made: dict[str, Stored] = {}
        for name, entry in entries.items():
            one = _stored(entry)
            if one is not None:
                made[str(name)] = one
        return made

    def write(self, entries: dict[str, Stored]) -> None:
        """담긴 것을 통째로 다시 씁니다. 쓰다 멈춰도 이전 파일이 남습니다."""
        payload: dict[str, Any] = {
            "version": VERSION,
            "tokens": {
                name: {
                    "access_token": one.access_token,
                    "token_type": one.token_type,
                    "expires_at": one.expires_at.strftime(STAMP_FORMAT),
                    "key": one.fingerprint,
                }
                for name, one in entries.items()
            },
        }
        write_json(self.path, payload)
        _narrow(self.path)

    def put(self, name: str, one: Stored) -> None:
        """자리 하나를 담습니다."""
        entries = self.read()
        entries[name] = one
        self.write(entries)

    def drop(self, name: str) -> None:
        """자리 하나를 버립니다. 없으면 아무 일도 하지 않습니다."""
        entries = self.read()
        if entries.pop(name, None) is None:
            return
        self.write(entries)


def _stored(entry: Any) -> Stored | None:
    """줄 하나를 읽습니다. 모양이 어긋나면 그 줄만 버립니다."""
    if not isinstance(entry, dict):
        return None

    token = str(entry.get("access_token") or "").strip()
    if not token:
        return None

    raw = str(entry.get("expires_at") or "").strip()
    try:
        expires_at = datetime.strptime(raw, STAMP_FORMAT)
    except ValueError:
        return None

    return Stored(
        access_token=token,
        token_type=str(entry.get("token_type") or "Bearer").strip(),
        expires_at=expires_at,
        fingerprint=str(entry.get("key") or "").strip(),
    )


def _narrow(path: Path) -> None:
    """파일 권한을 그 사용자만 읽고 쓰게 좁힙니다.

    POSIX 에서만 뜻이 있습니다. Windows 에서는 읽기전용 표시만 바뀌므로
    건드리지 않습니다 — 자세한 까닭은 이 파일 첫머리에.
    """
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        # 권한을 못 좁혔다고 앱이 안 뜨면 안 됩니다. 파일은 이미 쓴 뒤입니다.
        pass


def forget(environment: str, market: str, path: Path | None = None) -> None:
    """그 자리의 토큰을 파일에서 지웁니다.

    설정 화면에서 APP Key 가 바뀌었을 때 부릅니다 — 옛 키로 받은 토큰은
    쓸 수 없습니다.
    """
    TokenStore(path).drop(key_of(environment, market))
