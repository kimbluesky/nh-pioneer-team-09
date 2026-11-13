"""사용자 홈에 파일을 남기고 읽는 한 자리.

앱이 사용자 홈에 남기는 파일은 두 갈래입니다.

    ~/NHFutures/config/    설정과 인증 — 밖으로 나가면 안 되는 것
    ~/NHFutures/state/     화면 상태 — 관심종목, 배치처럼 새도 곤란할 것 없는 것

두 갈래를 폴더로 나눈 이유가 있습니다. 「config 폴더는 누구에게도 보내지
마세요」가 한 줄로 서야, 설정을 주고받다가 인증 정보가 함께 나가는 일이
생기지 않습니다. 화면 상태를 그 안에 섞으면 그 한 줄이 흐려집니다.

파일을 쓰는 방법은 두 파일이 같습니다. 그래서 여기 한 번만 만들어 두고
양쪽이 씁니다.

* **쓰다 멈춰도 반쪽 파일이 남지 않습니다.** 옆에 임시 파일로 다 쓴 뒤 이름을
  바꿔치기합니다. 앱을 끄거나 전원이 나가는 순간에 걸려도, 남는 것은 새 파일
  아니면 이전 파일입니다.
* **읽다가 깨진 것을 만나면 옆에 남기고 비어 있는 것으로 시작합니다.** 파일 한
  줄이 깨졌다고 화면이 안 뜨면 사용자가 되돌릴 방법이 없습니다. 깨진 파일은
  같은 자리에 `.broken` 을 붙여 두므로 나중에 열어 볼 수 있습니다.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.config import HOME_DIR

# 읽기 결과. 파일이 없는 것과 깨진 것은 다릅니다 — 없는 것은 아직 안 쓴
# 것이고, 깨진 것은 있었는데 못 읽은 것입니다.
MISSING = "missing"
OK = "ok"
BROKEN = "broken"

BROKEN_SUFFIX = ".broken"

#: 화면 상태와 전략 파일이 담기는 폴더.
STATE_DIR = HOME_DIR / "state"
#: 저장해 둔 응답으로 돌 때(`NH_REPLAY=1`) 쓰는 상태 폴더.
REPLAY_STATE_DIR = HOME_DIR / "state-replay"


def state_dir(env: Mapping[str, str] | None = None) -> Path:
    """이 앱이 상태를 담는 폴더. **저장해 둔 응답으로 도는 앱은 따로 씁니다.**

    두 앱이 한 폴더를 보면, 재생으로 띄워 확인하며 바꾼 것(돌던 실행, 예수금
    추이, 화면 배치)이 실서버로 띄운 창에 그대로 올라옵니다. 어느 파일이 이
    폴더를 따르는지는 각 파일이 정합니다 — 전략 목록(`strategies.json`)처럼
    사용자가 적어 둔 설정은 두 앱이 함께 씁니다.
    """
    # 함수 안에서 불러옵니다 — `nh_api.session` 이 토큰 보관(`auth.token_store`)을
    # 거쳐 이 모듈을 부르므로, 모듈 머리에서 부르면 서로를 기다리며 멈춥니다.
    from backend.nh_api.session import replay_enabled

    return REPLAY_STATE_DIR if replay_enabled(env) else STATE_DIR


#: 파일을 쓴 앱을 적는 칸. 값은 `live` 아니면 `replay` 입니다
#: (`nh_api.session.current_mode`).
ORIGIN = "origin"


@dataclass
class ReadState:
    """그 파일을 **마지막으로 읽었을 때 어땠는지.**

    깨진 파일은 읽는 순간 옆으로 치워지고(`read_json`) 그다음 읽기는 「없음」이
    됩니다. 그래서 읽은 그 순간의 상태만 내면 **화면이 늦게 물었을 때 아무
    말도 못 듣습니다** — 실행 목록은 앱이 뜰 때 한 번, 손실 한도는 도는 동안
    한 번만 읽습니다.

    그래서 **못 읽었다는 사실을 들고 있습니다.** 놓는 것은 **다음 성공적인
    쓰기**(또는 온전한 읽기)뿐입니다 — 사용자가 다시 담거나 다시 걸면 그
    알림은 할 일을 다 한 것입니다.
    """

    status: str = MISSING
    #: 다른 앱이 쓴 파일을 만났으면 **그 앱 이름**(`live`·`replay`). 그때는
    #: 담긴 것을 쓰지 않습니다 — 까닭은 `from_other_app()` 에.
    other_app: str = ""
    moved_to: str = ""
    #: 남기지 않고 **지운** 경우. 담긴 것 자체가 인증 자료인 파일이 그렇습니다
    #: (`read_json(keep=False)`).
    removed: bool = False

    @property
    def payload(self) -> dict[str, Any]:
        """화면에 내는 모양. 옮긴 자리는 **파일 이름만** 냅니다.

        전체 경로는 길고, 저장해 둔 응답으로 도는 앱은 폴더가 달라(`state-replay/`)
        오히려 헷갈립니다. 「상태 폴더에 있습니다」는 화면이 말합니다.
        """
        return {
            "status": self.status,
            # 다른 앱이 쓴 파일은 **읽었어도 안 쓴 것**입니다.
            "stored": self.status == OK and not self.other_app,
            "movedTo": self.moved_to or None,
            "otherApp": self.other_app or None,
        }

    def saw(self, status: str, moved_to: str = "", *, removed: bool = False) -> None:
        """읽은 결과 하나를 받습니다. `read_json` 이 읽을 때마다 부릅니다.

        **덮이지 않는 것은 「없음」뿐입니다** — 치운 뒤의 읽기가 늘 「없음」이라
        그것까지 받으면 알림이 한 번 읽는 사이에 사라집니다. **새로 깨진
        것은 받습니다** — 옮겨 둔 자리가 그때마다 달라, 안 받으면 지난번에
        남긴 파일을 알려 주게 됩니다.
        """
        # **읽을 때마다 다시 봅니다.** 다른 앱의 파일을 치우고 이 앱의 것을 쓰면
        # 그다음 읽기는 이 앱의 파일이고, 알림도 함께 놓여야 합니다.
        self.other_app = ""
        if self.status == BROKEN and status == MISSING:
            return
        self.status = status
        self.moved_to = moved_to if status == BROKEN else ""
        self.removed = removed if status == BROKEN else False

    def kept(self) -> str:
        """깨진 파일을 어떻게 했는지 한 마디. 기록에 적습니다."""
        if self.moved_to:
            return f"{self.moved_to} 로 옮겨 두었습니다"
        if self.removed:
            return "담긴 것이 인증 자료라 남기지 않고 지웠습니다"
        return "치우지 못해 그 자리에 그대로 있습니다"

    def wrote(self) -> None:
        """파일을 다 쓴 뒤에 부릅니다 — 알림을 놓습니다."""
        self.status = OK
        self.moved_to = ""
        self.removed = False
        self.other_app = ""


def from_other_app(body: Any, mine: str, *, state: ReadState | None = None) -> bool:
    """담긴 것이 **다른 앱이 쓴 파일**인지.

    저장해 둔 응답으로 도는 앱(`NH_REPLAY=1`)과 실제 서버에 붙는 앱은 상태
    폴더가 다릅니다. 그래도 파일이 섞이는 길은 남아 있어 — 폴더를 통째로
    복사하거나, 다른 앱의 파일을 손으로 옮겨 두는 경우입니다 — 쓴 앱을 파일에
    함께 적습니다. 표지가 다르면 **담긴 것을 쓰지 않습니다.** 저장본으로 돌던
    실행이나 잔고를 실제 서버 쪽이 이어받으면, 화면은 지금 무엇이 도는지
    잘못 말합니다.

    **표지가 없는 파일은 이 앱의 것으로 봅니다** — 표지 칸이 생기기 전에 쓴
    파일에는 이 칸이 없습니다.
    """
    if not isinstance(body, dict):
        return False
    found = str(body.get(ORIGIN) or "").strip()
    if not found or found == mine:
        return False
    if state is not None:
        state.other_app = found
    return True


def read_json(
    path: Path, *, keep: bool = True, state: ReadState | None = None
) -> tuple[dict[str, Any], str]:
    """파일을 읽습니다. 내용과 「어떤 상태였는지」를 함께 돌려줍니다.

    깨져 있으면 옆에 남겨 두고 빈 내용으로 답합니다. 예외를 내지 않습니다.

    **`keep=False` 면 남기지 않고 지웁니다.** 접속 토큰처럼 담긴 것 자체가
    인증 자료일 때 씁니다 — 못 읽은 파일을 `.broken` 으로 남기면 그 안의
    토큰이 그대로 자리에 남습니다. 화면 상태는 남겨 두는 편이 낫고, 인증
    자료는 지우는 편이 낫습니다.

    **`state` 를 건네면 결과를 거기에도 적습니다.** 옮겨 둔 이름은 여기서만
    알 수 있습니다 — 이미 `.broken` 이 있으면 시각을 붙인 다른 이름이 됩니다.
    """
    if not path.is_file():
        _mark(state, MISSING)
        return {}, MISSING

    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError, UnicodeError):
        return {}, _broken(path, keep, state)

    if not isinstance(loaded, dict):
        return {}, _broken(path, keep, state)

    _mark(state, OK)
    return loaded, OK


def _broken(path: Path, keep: bool, state: ReadState | None) -> str:
    """못 읽은 파일을 치우고 「깨짐」을 적습니다.

    **치우고 나서 자리를 봅니다.** 옮기지도 지우지도 못하는 수가 있어
    (`OSError`), 무엇을 하려 했는지가 아니라 **무엇이 되었는지**를 적습니다.
    """
    kept = _set_aside(path, keep)
    name = kept.name if kept is not None else ""
    _mark(state, BROKEN, name, removed=not name and not path.is_file())
    return BROKEN


def _mark(
    state: ReadState | None, status: str, moved_to: str = "", *, removed: bool = False
) -> None:
    if state is not None:
        state.saw(status, moved_to, removed=removed)


def _set_aside(path: Path, keep: bool) -> Path | None:
    """못 읽은 파일을 치웁니다. 남길지 지울지는 부르는 쪽이 정합니다."""
    if keep:
        return keep_broken(path)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    return None


def keep_broken(path: Path) -> Path | None:
    """읽지 못한 파일을 옆에 남깁니다. 남긴 자리를 돌려줍니다.

    **이미 남겨 둔 것이 있으면 덮지 않습니다.** 두 번째로 깨졌다고 첫 번째를
    지워 버리면 처음 무엇 때문에 깨졌는지 볼 수 없게 됩니다. 뒤엣것에는 시각을
    붙이고, 같은 초에 또 깨지면 번호를 하나씩 더해 빈 이름을 찾습니다.
    """
    kept = path.with_name(path.name + BROKEN_SUFFIX)
    if kept.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        kept = path.with_name(f"{path.name}{BROKEN_SUFFIX}.{stamp}")
        # 같은 초에 또 깨지면 번호를 하나씩 더해 빈 이름을 찾습니다.
        serial = 2
        while kept.exists():
            kept = path.with_name(f"{path.name}{BROKEN_SUFFIX}.{stamp}-{serial}")
            serial += 1
    try:
        os.replace(path, kept)
    except OSError:
        return None
    return kept


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """파일에 씁니다. 쓰다 멈춰도 이전 파일이 남습니다.

    Raises:
        OSError: 폴더를 만들지 못했거나 쓰지 못한 경우.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n"

    # 이름을 바꿔치기하려면 같은 폴더에 있어야 합니다. 다른 볼륨이면 바꿔치기가
    # 아니라 복사가 되어 「반쪽 파일이 안 남는다」가 깨집니다.
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
