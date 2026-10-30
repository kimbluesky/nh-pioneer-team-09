"""전략이 만든 기록을 한 줄씩 덧붙입니다.

    ~/NHFutures/state/strategy-trades.jsonl

**거래·체결·이벤트는 남깁니다.** 화면에 그리는 성적(잔고 곡선·승률)은 안 담고
**무슨 일이 있었는지만** 담습니다 — 성적은 기록에서 다시 셀 수 있지만, 기록이
없으면 어떤 성적도 다시 세지 못합니다. 분석 화면이 읽을 자리입니다.

## 왜 덧붙이기만 하나

한 줄이 한 사건입니다. 통째로 다시 쓰지 않으므로 **쓰다 멈춰도 앞의 기록이
다치지 않고**, 여러 실행이 같은 파일에 써도 서로의 줄을 지우지 않습니다. 대신
고치거나 지울 수 없습니다 — 잘못 적힌 줄은 남고, 뒤에 바로잡는 줄을 답니다.

## 줄마다 어디 것인지 적습니다

실행 id 만으로는 나중에 그 줄이 어느 전략의 것이었는지 알 수 없습니다. 실행은
지워져도 기록은 남기 때문입니다. 그래서 **전략·버전·시장·종목·환경**을 줄마다
함께 적습니다. `record` 는 그 줄이 **어디서 나온 값인지**를 가리킵니다 —
`paper` 는 종이거래로 지어낸 값, `sim` 은 모의 계좌가, `real` 은 실전 계좌가
낸 값입니다.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from pathlib import Path
from typing import Any

from backend.strategy.store import run_state_dir

logger = logging.getLogger(__name__)

#: 기록 파일. 실행과 같은 상태 폴더입니다 — 재생으로 띄우면 그쪽 폴더입니다
#: (`store.run_state_dir`). 지어낸 체결이 실제 기록에 섞이지 않습니다.
TRADES_PATH = run_state_dir() / "strategy-trades.jsonl"

#: 기록 갈래의 기본값. 종이거래로 지어낸 값이라는 표지입니다.
PAPER = "paper"

#: 줄 갈래. 거래·체결·이벤트에 더해, 실행이 거래일을 넘길 때의 잔고(`day`)와
#: 끝난 주문의 결과(`order`)입니다.
TRADE = "trade"
FILL = "fill"
EVENT = "event"
DAY = "day"
ORDER = "order"

#: 집계가 읽는 갈래. 이 셋만 캐시에 담습니다.
KEPT: tuple[str, ...] = (TRADE, DAY, ORDER)

#: 파일이 이만큼을 넘으면 한 대(`.old`)만 남기고 새로 엽니다.
#:
#: 덧붙이기만 하는 파일은 끝없이 자랍니다. 데스크톱 앱이라 지워 주는 사람이
#: 없어, **한 대는 남기고** 그 앞은 버립니다 — 지난 기록을 다 들고 있는 것보다
#: 디스크가 차지 않는 편이 먼저입니다.
MAX_BYTES = 16 * 1024 * 1024


class Journal:
    """기록 파일 하나. 덧붙이기만 합니다."""

    def __init__(self, path: Path | None = None, *, max_bytes: int = MAX_BYTES) -> None:
        self.path = Path(path) if path is not None else TRADES_PATH
        self.max_bytes = max(int(max_bytes), 1)
        self._lock = threading.Lock()
        #: 파일 바이트를 읽는 자리. 시험이 읽는 사이에 교대를 끼워 넣으려고
        #: 바꿔 낄 수 있게 둡니다.
        self.read_bytes = _read_bytes
        #: 거래 줄 캐시 — `(두 파일의 크기·수정 시각)`, 거래 줄들, 온전히 읽었는지.
        self._cached: tuple[Any, dict[str, list[dict[str, Any]]], bool] | None = None
        #: 거래 줄을 실제로 푼 횟수. 캐시가 도는지 시험이 봅니다.
        self.parsed = 0

    @property
    def old_path(self) -> Path:
        """교대로 옮겨 둔 앞 파일(`.jsonl.old`)."""
        return self.path.with_suffix(".jsonl.old")

    def write(self, kind: str, run: Any, body: dict[str, Any]) -> None:
        """줄 하나를 덧붙입니다. **쓰지 못해도 예외를 내지 않습니다.**

        기록을 남기지 못한 것이 돌고 있는 실행을 멈출 까닭은 아닙니다. 못 쓴
        것은 기록에 남깁니다.
        """
        # **줄 갈래와 사건 갈래는 다른 것입니다.** 이벤트 줄은 제 갈래를
        # `kind` 로 들고 오는데(`signal`·`ready`·`fill`…), 그대로 두면 줄
        # 갈래(`event`)를 덮어 나중에 무엇이 체결이고 무엇이 이벤트인지
        # 가릴 수 없게 됩니다. 사건 갈래는 `event` 로 옮겨 담습니다.
        made = dict(body)
        if "kind" in made:
            made["event"] = made.pop("kind")
        row = {
            "kind": kind,
            "runId": getattr(run, "id", ""),
            "versionId": getattr(run, "version_id", ""),
            "market": getattr(run, "market", ""),
            "code": getattr(run, "code", ""),
            "session": getattr(run, "session", ""),
            # 버전을 묶는 전략. 버전이 지워진 뒤에도 기록을 전략으로 묶을 수
            # 있게 줄마다 적습니다. 이 칸이 없던 때의 줄은 읽을 때 버전으로
            # 찾아 붙입니다(`records`).
            "groupId": getattr(run, "group_id", "") or "",
            # **어느 갈래가 낸 값인지는 실행이 압니다.** 여기서 `paper` 로
            # 굳히면 계좌가 낸 체결까지 지어낸 값으로 남습니다.
            "record": getattr(run, "mode", "") or PAPER,
            # 그 줄의 손익 통화. 이 칸이 없던 때의 줄은 시장으로 읽습니다.
            **({"currency": getattr(run, "currency", "")} if getattr(run, "currency", "") else {}),
            **made,
        }
        try:
            with self._lock:
                self._roll()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as file:
                    file.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.warning("전략 기록을 남기지 못했습니다: %s", exc)

    def read(self, *, run_id: str = "", kind: str = "", limit: int = 0) -> list[dict[str, Any]]:
        """담긴 줄들. **읽지 못하는 줄은 건너뜁니다.**

        덧붙이는 중에 앱이 멈추면 마지막 줄이 반쪽으로 남을 수 있습니다 — 한글
        글자 가운데에서 잘리기도 합니다. **줄마다 따로 풉니다.** 파일을 통째로
        풀면 그 반쪽 글자 하나 때문에 앞의 기록까지 전부 못 읽습니다.
        """
        made: list[dict[str, Any]] = []
        try:
            raw = self.read_bytes(self.path)
        except OSError:
            return made
        for line in raw.split(b"\n"):
            row = _row_of(line)
            if row is None:
                continue
            if run_id and row.get("runId") != run_id:
                continue
            if kind and row.get("kind") != kind:
                continue
            made.append(row)
        return made[-limit:] if limit > 0 else made

    def trades(self) -> tuple[list[dict[str, Any]], bool]:
        """거래 줄 전부 — **앞 파일(`.old`)부터** 시각 차례로. `(줄들, 온전히 읽었는지)`."""
        rows, complete = self.rows()
        return rows[TRADE], complete

    def rows(self) -> tuple[dict[str, list[dict[str, Any]]], bool]:
        """집계가 쓰는 줄 셋(거래·거래일 잔고·주문 결과)을 **한 번 읽기로** 함께.

        `(갈래 → 줄들, 온전히 읽었는지)` 이고 줄은 앞 파일(`.old`)부터 시각
        차례입니다.

        두 파일을 읽는 사이에 교대가 나면 본 파일의 줄이 새 앞 파일로 옮겨 가
        **양쪽 어디에서도 안 읽힙니다.** 그래서 읽기 전·후에 두 파일의 크기와
        수정 시각을 견주고, 달라졌거나 읽다 막혔으면 한 번 통째로 다시 읽습니다.
        두 번째도 어긋나면 읽은 그대로 내고 「온전하지 않다」고 답합니다 — 기록이
        계속 쌓이는 동안 막아 버리면 화면이 영영 값을 못 받습니다.

        같은 두 파일이면 **다시 풀지 않습니다**(캐시). 캐시에는 이 세 갈래만
        둡니다(체결·이벤트는 안 담음). 두 파일을 합쳐도 32MB 안의 줄이라 상한이
        따로 필요 없습니다.
        """
        made: dict[str, list[dict[str, Any]]] | None = None
        for _attempt in range(2):
            before = self._stamp()
            if self._cached is not None and self._cached[0] == before:
                return _copy(self._cached[1]), self._cached[2]
            try:
                made = _merged(self._kept_rows(self.old_path), self._kept_rows(self.path))
            except OSError:
                made = None
            if made is not None and self._stamp() == before:
                self._cached = (before, made, True)
                return _copy(made), True
        logger.warning("전략 기록을 읽는 사이에 파일이 바뀌어 온전히 읽지 못했습니다.")
        return (made if made is not None else {kind: [] for kind in KEPT}), False

    def _stamp(self) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
        """두 파일의 `(크기, 수정 시각 ns)`. 없는 파일은 `None` 입니다."""
        return _stat(self.old_path), _stat(self.path)

    def _kept_rows(self, path: Path) -> dict[str, list[dict[str, Any]]]:
        """한 파일의 거래·잔고·주문 결과 줄. 없는 파일은 빈 목록이고, 읽다 막히면 OSError 입니다.

        줄은 글자로 먼저 고릅니다 — 체결·이벤트 줄까지 모두 풀면 세 배가
        걸립니다. `write` 는 갈래 칸을 **맨 앞**에 쓰므로(`{"kind": "trade", …`)
        줄의 머리만 봅니다. 고른 뒤에도 갈래를 다시 봅니다.
        """
        made: dict[str, list[dict[str, Any]]] = {kind: [] for kind in KEPT}
        try:
            raw = self.read_bytes(path)
        except FileNotFoundError:
            return made
        self.parsed += 1
        for line in raw.split(b"\n"):
            kind = next((name for name, mark in _MARKS.items() if line.startswith(mark)), None)
            if kind is None:
                continue
            row = _row_of(line)
            if row is not None and row.get("kind") == kind:
                made[kind].append(_slim(kind, row))
        return made

    def _roll(self) -> None:
        """자랄 만큼 자랐으면 한 대만 남기고 새로 엽니다."""
        try:
            if self.path.stat().st_size < self.max_bytes:
                return
        except OSError:
            return
        old = self.path.with_suffix(".jsonl.old")
        old.unlink(missing_ok=True)
        self.path.rename(old)
        logger.info("전략 기록이 커져 %s 로 옮기고 새로 엽니다.", old.name)


#: 거래 줄을 글자로 먼저 고르는 표지. `write` 가 쓰는 모양 그대로입니다.
_TRADE_MARK = b'"kind": "trade"'

#: 줄 머리로 고르는 표지들. `write` 가 갈래 칸을 맨 앞에 쓰는 모양 그대로입니다.
_MARKS: dict[str, bytes] = {kind: b'{"kind": "' + kind.encode() + b'"' for kind in KEPT}


#: 캐시에 담을 칸. 잔고·주문 결과 줄은 집계가 읽는 칸만 둡니다 — 주문 줄은
#: 거래마다 둘씩 쌓여, 줄 전체를 담으면 두 파일에서 수십 MB 가 됩니다. 거래
#: 줄은 조회 답에 칸 대부분이 실리므로 그대로 둡니다.
_HEAD: tuple[str, ...] = ("kind", "runId", "versionId", "groupId", "market", "code", "session", "record")
_KEEP: dict[str, tuple[str, ...] | None] = {
    TRADE: None,
    DAY: _HEAD + ("day", "equity", "marginPeak", "late"),
    ORDER: _HEAD + ("orderId", "outcome", "endedAt"),
}


def _slim(kind: str, row: dict[str, Any]) -> dict[str, Any]:
    """캐시에 담을 모양. 머리칸 글자는 한 벌을 나눠 씁니다(`sys.intern`) — 같은
    실행·같은 시장 글자가 줄마다 따로 잡히지 않게 합니다."""
    names = _KEEP.get(kind)
    made = dict(row) if names is None else {name: row.get(name) for name in names if name in row}
    for name in _HEAD:
        value = made.get(name)
        if isinstance(value, str):
            made[name] = sys.intern(value)
    return made


def _merged(
    first: dict[str, list[dict[str, Any]]], second: dict[str, list[dict[str, Any]]]
) -> dict[str, list[dict[str, Any]]]:
    return {kind: first[kind] + second[kind] for kind in KEPT}


def _copy(rows: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """캐시를 밖에서 고치지 못하게 목록만 새로 만듭니다."""
    return {kind: list(one) for kind, one in rows.items()}


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        held = path.stat()
    except OSError:
        return None
    return held.st_size, held.st_mtime_ns


def _row_of(line: bytes) -> dict[str, Any] | None:
    """한 줄을 풉니다. 풀리지 않으면(반쪽 글자·반쪽 줄) `None` 입니다."""
    if not line.strip():
        return None
    try:
        row = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return row if isinstance(row, dict) else None


#: 앱이 쓰는 기록. 테스트는 제 것을 만들어 씁니다.
journal = Journal()
