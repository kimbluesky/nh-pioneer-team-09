"""전략과 버전을 사용자 홈에 남기고 읽습니다.

    ~/NHFutures/state/strategies.json

**설정 폴더가 아니라 상태 폴더입니다.** 전략은 새도 곤란할 것이 없는 값이고,
`config/` 는 「누구에게도 보내지 마세요」가 한 줄로 서야 하는 자리입니다
(`backend/storage.py` 첫머리).

담는 것은 **화면이 만든 모양 그대로**입니다 — 목록 줄(`StrategyVersion`)과 설정
초안(`SettingsDraft`). 백엔드가 모양을 다시 정하지 않는 것은, 같은 값을 두 벌로
적어 두면 화면과 엔진이 서로 다른 전략을 말하게 되기 때문입니다. 다만 **모르는
이름은 받지 않습니다**(`_VERSION_FIELDS`·`_DRAFT_FIELDS`) — 파일이 화면의 임시
값까지 받아 주면 무엇이 저장된 값인지 흐려집니다.

**쓰다 멈춰도 반쪽 파일이 남지 않습니다.** 파일을 다루는 방법은 `storage.py` 가
한 번만 만들어 두고 화면 상태 파일과 함께 씁니다.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from backend import storage
from backend.storage import BROKEN, ReadState, read_json, write_json
from backend.strategy import instrument

logger = logging.getLogger(__name__)

STATE_DIR = storage.STATE_DIR
STRATEGIES_PATH = STATE_DIR / "strategies.json"

#: 저장해 둔 응답으로 돌 때 쓰는 상태 폴더.
REPLAY_STATE_DIR = storage.REPLAY_STATE_DIR


def run_state_dir(env: Mapping[str, str] | None = None) -> Path:
    """**돌던 것**이 담기는 폴더 — 실행·거래 기록·손실 한도입니다.

    저장해 둔 응답으로 도는 앱(`NH_REPLAY=1`)은 **다른 폴더**를 씁니다. 둘이
    한 폴더를 보면, 재생으로 띄운 앱이 실서버에서 돌던 실행을 그대로 이어받아
    세웁니다 — 실제로 그렇게 올라온 적이 있습니다. 실행은 「지금 무엇이 돌고
    있나」이고 그 답은 어느 서버를 보고 있었는지에 딸린 것입니다.

    **전략 자체(`strategies.json`)는 안 나눕니다.** 전략은 사용자가 적어 둔
    설정이지 돌던 것이 아니라서, 재생으로 띄웠다고 안 보이면 곤란합니다.
    """
    return storage.state_dir(env)

#: 파일 형식. 담는 것이 달라지면 올립니다.
VERSION = 1

#: 목록 줄에 담는 이름. 화면의 `StrategyVersion` 과 같습니다.
_VERSION_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "groupId",
        "name",
        "version",
        "market",
        # 줄이 가리키는 종목. 품목이 참이고 종목코드는 **특정 월물을 고른 줄에만**
        # 붙습니다 — 까닭은 `instrument.py` 첫머리에.
        "productCode",
        "productKind",
        "contractCode",
        # 옛 줄의 역할. 새 줄은 안 씁니다 — 읽을 때 품목으로 옮겨 냅니다.
        "role",
        "instrumentFallback",
        "status",
        "record",
        "tags",
        "summary",
        "account",
        "marginBudget",
        "dailyLoss",
        "orderQty",
        "currency",
        "seed",
    }
)

#: 설정 초안에 담는 이름. 화면의 `SettingsDraft` 와 같습니다.
_DRAFT_FIELDS: frozenset[str] = frozenset({"memo", "baseBar", "evalMode", "rules", "aux"})

#: 한 파일에 담을 수 있는 버전 수. 파일이 끝없이 커지는 것을 막습니다.
MAX_VERSIONS = 200


class StrategyStoreError(Exception):
    """저장할 수 없는 요청입니다."""


class StrategyStore:
    """전략 파일 하나."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else STRATEGIES_PATH
        # 요청이 여러 스레드에서 동시에 옵니다. 읽고 합쳐 쓰는 동안은 하나만.
        self._lock = threading.Lock()
        #: 마지막으로 읽었을 때 어땠는지. 화면이 「못 읽어 옆에 두었습니다」를
        #: 말할 수 있게 답에 실립니다(`GET /api/strategy/versions`).
        self.state = ReadState()

    # ── 읽기 ────────────────────────────────────────────

    def read(self) -> dict[str, Any]:
        """담겨 있는 것 전부. 파일이 없으면 빈 목록입니다.

        **옛 줄은 여기서 품목 칸이 채워집니다** — 내보내는 자리에서만 채우고
        파일에는 안 적습니다(`_shown`).
        """
        with self._lock:
            return _shown(self._current())

    # ── 쓰기 ────────────────────────────────────────────

    def save(self, version: dict[str, Any], draft: dict[str, Any] | None) -> dict[str, Any]:
        """버전 하나를 담습니다. 같은 `id` 가 있으면 그 자리를 바꿉니다.

        Raises:
            StrategyStoreError: `id` 가 없거나, 모르는 이름이 섞였거나, 파일이 찬 경우.
            OSError: 파일을 쓰지 못한 경우.
        """
        row = _only(version, _VERSION_FIELDS, "전략")
        version_id = str(row.get("id") or "").strip()
        if not version_id:
            raise StrategyStoreError("전략에 id 가 없습니다.")
        row["id"] = version_id
        kept = _only(draft, _DRAFT_FIELDS, "설정") if draft is not None else None

        with self._lock:
            made = self._current()
            versions = [one for one in made["versions"] if one["id"] != version_id]
            if len(versions) >= MAX_VERSIONS:
                raise StrategyStoreError(
                    f"전략을 {MAX_VERSIONS}개까지 담을 수 있습니다. 쓰지 않는 것을 지워 주세요."
                )
            # **차례를 지킵니다.** 목록이 저장할 때마다 뒤섞이면 화면이 고른 줄을
            # 잃습니다. 있던 자리는 그대로 두고, 새 것만 뒤에 붙입니다.
            at = next(
                (i for i, one in enumerate(made["versions"]) if one["id"] == version_id), None
            )
            if at is None:
                versions.append(row)
            else:
                versions.insert(at, row)

            drafts = dict(made["drafts"])
            if kept is not None:
                drafts[version_id] = kept
            # **담는 것은 받은 그대로, 내는 것은 채워서.** 파일에 적는 목록에는
            # 방금 저장한 줄 말고 **손대지 않은 옛 줄**도 들어 있습니다.
            self._write(versions, drafts)
            return _shown({"versions": versions, "drafts": drafts})

    def delete(self, version_id: str) -> bool:
        """버전 하나를 지웁니다. 없던 것이면 거짓입니다."""
        with self._lock:
            made = self._current()
            versions = [one for one in made["versions"] if one["id"] != version_id]
            if len(versions) == len(made["versions"]):
                return False
            drafts = {key: value for key, value in made["drafts"].items() if key != version_id}
            self._write(versions, drafts)
            return True

    # ── 파일 ────────────────────────────────────────────

    def _current(self) -> dict[str, Any]:
        """파일에 담긴 것 그대로. **여기서는 아무것도 안 채웁니다.**

        담고 지우는 자리가 이 답을 그대로 다시 씁니다. 여기서 채우면 줄 하나를
        저장할 때마다 **손대지 않은 옛 줄까지** 새 칸을 달고 파일에 적힙니다 —
        새 칸을 모르는 앱으로 돌아갔을 때 그 줄들이 통째로 안 열립니다.

        **파일이 통째로 안 읽히면 남깁니다.** `read_json` 이 예외를 안 내고 옆으로
        치운 뒤 빈 내용으로 답하므로, 기록이 없으면 담아 둔 전략이 전부 사라진
        것만 보입니다. 게다가 다음 저장이 그 빈 상태를 파일에 굳힙니다.
        """
        body, status = read_json(self.path, state=self.state)
        if status == BROKEN:
            logger.warning(
                "전략 파일을 읽지 못했습니다(%s): %s. "
                "담아 둔 전략이 목록에 서지 않습니다.",
                self.state.kept(),
                self.path,
            )
        versions = [one for one in (body.get("versions") or []) if isinstance(one, dict)]
        drafts = body.get("drafts")
        return {
            "versions": [one for one in versions if str(one.get("id") or "").strip()],
            "drafts": drafts if isinstance(drafts, dict) else {},
        }

    def _write(self, versions: list[dict[str, Any]], drafts: dict[str, Any]) -> None:
        write_json(self.path, {"version": VERSION, "versions": versions, "drafts": drafts})
        # 다시 담았으면 「못 읽었다」는 알림은 할 일을 다 한 것입니다.
        self.state.wrote()


def _shown(made: dict[str, Any]) -> dict[str, Any]:
    """내보낼 모양. **옛 줄의 품목 칸을 채워 냅니다**(`instrument.filled`).

    채우는 자리를 내보내는 경계 하나로 모아 둡니다 — 파일을 읽는 자리에서
    채우면 그 답을 그대로 다시 쓰는 저장·삭제가 파일까지 고칩니다.
    """
    return {**made, "versions": [instrument.filled(one) for one in made["versions"]]}


def _only(values: Any, allowed: frozenset[str], what: str) -> dict[str, Any]:
    """아는 이름만 남깁니다. 모르는 이름이 오면 알립니다 — 조용히 버리지 않습니다."""
    if not isinstance(values, dict):
        raise StrategyStoreError(f"{what} 이 사전이 아닙니다.")
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise StrategyStoreError(f"{what} 에 모르는 항목이 있습니다: {', '.join(unknown)}")
    return dict(values)


#: 앱이 쓰는 하나.
store = StrategyStore()
