"""저장해 둔 NH선물 Open API 응답을 읽어 들입니다.

파일은 `tests/fixtures/nh/{API}/{환경}/{시장}/{기록ID}.json` 에 있고,
`scripts/import_nh_fixtures.py` 가 만듭니다. 손으로 고치지 마세요.

여기 담긴 값은 실제 응답에서 인증값과 식별자를 바꾼 것입니다. 실제 계좌나
실제 시세가 아니며, 개발과 시험에 씁니다.

## 저장소에 올리지 않는 폴더

실전 계좌 응답에는 **예수금·잔고·손익 금액이 실제 값 그대로** 들어 있습니다.
식별자는 가명으로 바뀌지만 금액은 바뀌지 않습니다. 그래서 이 응답들은
`tests/fixtures/nh_private/` 에 따로 두고 **저장소에 올리지 않습니다**
(`.gitignore` 에 폴더째 들어 있습니다).

그 폴더는 **있을 수도 없을 수도 있습니다.** 저장소를 받은 사람에게는 없고,
받아 둔 사람에게만 있습니다. 그래서 읽을 때 없으면 조용히 넘어가고,
**시험은 그 폴더에 기대지 않습니다** — 기대면 사람마다 결과가 달라집니다.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

# 기록 파일이 놓이는 자리.
FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "nh"

# 저장소에 올리지 않는 자리. 실전 계좌 응답이 여기 놓입니다.
PRIVATE_ROOT = FIXTURE_ROOT.parent / "nh_private"

# 그 폴더를 어디서 읽을지. **빈 값을 넣으면 아예 안 읽습니다.**
PRIVATE_DIR_FLAG = "NH_REPLAY_PRIVATE_DIR"

# 기록 파일 형식. 형식을 바꾸면 이 값을 올리고 `scripts/import_nh_fixtures.py` 를 다시 돌립니다.
FIXTURE_FORMAT = "nh-fixture-1"

INDEX_NAME = "index.json"


class FixtureError(Exception):
    """기록 파일을 읽을 수 없을 때 발생합니다."""


@dataclass(frozen=True)
class Fixture:
    """기록 하나. HTTP 와 실시간이 공통으로 가지는 부분입니다."""

    api_id: str
    environment: str
    market: str
    record_id: str
    collected_at: str
    scenario_id: str
    request: Any

    @property
    def order(self) -> tuple[str, str]:
        """같은 API 안에서 기록을 줄 세우는 기준. 수집한 순서입니다."""
        return (self.collected_at, self.record_id)


@dataclass(frozen=True)
class HttpFixture(Fixture):
    """HTTP 로 부른 기록."""

    method: str
    path: str
    http_status: int
    response: Any


@dataclass(frozen=True)
class StreamEvent:
    """실시간 메시지 하나."""

    seq: int
    direction: str
    offset_ms: float
    payload: Any


@dataclass(frozen=True)
class StreamFixture(Fixture):
    """실시간으로 받은 기록."""

    events: tuple[StreamEvent, ...]

    @property
    def received(self) -> tuple[StreamEvent, ...]:
        """서버가 보내 온 메시지만. 이 앱이 보낸 구독 요청은 뺍니다."""
        return tuple(e for e in self.events if e.direction == "received")


def canonical(value: Any) -> str:
    """요청이 같은지 비교하기 위한 형태. 키 순서는 무시하고 값은 그대로 봅니다."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read_dir(root: Path) -> list[Fixture]:
    """폴더 하나에 있는 기록을 전부 읽습니다."""
    made: list[Fixture] = []
    for path in sorted(root.rglob("*.json")):
        if path.name == INDEX_NAME:
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise FixtureError(f"{path.name}: 파일을 읽을 수 없습니다.") from exc
        made.append(_build(raw, path))
    return made


def _build(raw: dict[str, Any], source: Path) -> Fixture:
    if raw.get("format") != FIXTURE_FORMAT:
        raise FixtureError(f"{source.name}: 아는 형식이 아닙니다.")

    common = {
        "api_id": raw["api_id"],
        "environment": raw["environment"],
        "market": raw["market"],
        "record_id": raw["record_id"],
        "collected_at": raw["collected_at"],
        "scenario_id": raw.get("scenario_id") or "",
        "request": raw.get("request"),
    }

    if raw.get("transport") == "WEBSOCKET":
        return StreamFixture(
            **common,
            events=tuple(
                StreamEvent(
                    seq=e["seq"],
                    direction=e["direction"],
                    offset_ms=float(e["offset_ms"]),
                    payload=e["payload"],
                )
                for e in raw.get("events", ())
            ),
        )

    return HttpFixture(
        **common,
        method=raw["method"],
        path=raw["path"],
        http_status=int(raw["http_status"]),
        response=raw.get("response"),
    )


class FixtureStore:
    """저장해 둔 기록 전체."""

    def __init__(self, fixtures: list[Fixture], root: Path) -> None:
        self.root = root
        self._all = sorted(fixtures, key=lambda f: (f.api_id, f.environment, f.market, f.order))

    @classmethod
    def load(cls, root: Path | None = None, private: Path | None = None) -> FixtureStore:
        """저장해 둔 응답을 읽습니다.

        `private` 를 주면 그 폴더의 기록도 **함께** 읽습니다. 폴더가 없으면
        조용히 넘어갑니다 — 그 폴더는 사람마다 있을 수도 없을 수도 있어서,
        없다고 실패하면 저장소를 받은 사람이 시험을 못 돌립니다.
        """
        root = Path(root or FIXTURE_ROOT)
        if not root.is_dir():
            raise FixtureError(
                f"저장해 둔 응답이 없습니다: {root}\n"
                "scripts/import_nh_fixtures.py 로 먼저 만들어 주세요."
            )

        fixtures = _read_dir(root)
        if not fixtures:
            raise FixtureError(f"저장해 둔 응답이 한 건도 없습니다: {root}")

        if private is not None and Path(private).is_dir():
            fixtures.extend(_read_dir(Path(private)))
        return cls(fixtures, root)

    def __len__(self) -> int:
        return len(self._all)

    @property
    def all(self) -> tuple[Fixture, ...]:
        return tuple(self._all)

    @property
    def http(self) -> tuple[HttpFixture, ...]:
        return tuple(f for f in self._all if isinstance(f, HttpFixture))

    @property
    def streams(self) -> tuple[StreamFixture, ...]:
        return tuple(f for f in self._all if isinstance(f, StreamFixture))

    def find_http(
        self,
        *,
        method: str,
        path: str,
        environment: str,
        market: str | None = None,
    ) -> tuple[HttpFixture, ...]:
        """경로가 같은 기록을 수집한 순서대로 돌려줍니다."""
        return tuple(
            f
            for f in self.http
            if f.method == method
            and f.path == path
            and f.environment == environment
            and (market is None or f.market == market)
        )

    def find_stream(
        self,
        api_id: str,
        *,
        environment: str,
        market: str | None = None,
    ) -> tuple[StreamFixture, ...]:
        """실시간 기록을 수집한 순서대로 돌려줍니다."""
        return tuple(
            f
            for f in self.streams
            if f.api_id == api_id
            and f.environment == environment
            and (market is None or f.market == market)
        )


def private_root(env: Mapping[str, str] | None = None) -> Path | None:
    """저장소에 안 올리는 기록을 어디서 읽을지. 안 읽기로 했으면 `None`.

    환경변수 `NH_REPLAY_PRIVATE_DIR` 이 규칙을 정합니다.

        (없음)      기본 자리(`tests/fixtures/nh_private/`)에 있으면 읽습니다
        (빈 값)     **읽지 않습니다.** 시험이 이 값을 비워 두고 돕니다
        <폴더>      그 폴더에서 읽습니다
    """
    env = os.environ if env is None else env
    if PRIVATE_DIR_FLAG not in env:
        return PRIVATE_ROOT
    raw = str(env.get(PRIVATE_DIR_FLAG, "")).strip()
    return Path(raw) if raw else None


@lru_cache(maxsize=4)
def _load_cached(root: Path | None, private: Path | None) -> FixtureStore:
    return FixtureStore.load(root, private)


def load_fixtures(root: Path | None = None) -> FixtureStore:
    """읽어 둔 것이 있으면 다시 읽지 않습니다.

    저장소에 안 올리는 폴더도 함께 봅니다 — 있으면 더해 읽고, 없으면 그대로
    지나갑니다.
    """
    return _load_cached(root, private_root())
