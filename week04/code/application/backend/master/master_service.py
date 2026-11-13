"""종목 마스터의 현재 상태를 들고 있는 자리.

앱을 켜면 서버가 뜬 **뒤에** 마스터를 받기 시작합니다. 받는 동안에도 화면은 열려
있어야 하므로, 받기와 파싱은 별도 스레드에서 돌고 화면은 `/api/master/status` 로
진행 상황을 봅니다.

받기가 끝나면 파싱한 결과를 여기 담아 두고, `/api/master/index` 는 그것을 돌려줍니다.
15만 행을 요청마다 다시 읽지 않기 위해서입니다.
"""
from __future__ import annotations

import logging
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from backend.master.master_download import DownloadResult, download_all
from backend.master.master_files import DOMESTIC, MASTER_FILES
from backend.master.master_index import MasterIndex, build_index
from backend.master.master_store import LoadedMaster, MASTER_DIR, load_all
from backend.sessions.hours import iso, now_kst

logger = logging.getLogger(__name__)

#: 아직 아무것도 안 한 상태.
IDLE = "idle"
#: 주소에서 받는 중.
DOWNLOADING = "downloading"
#: 받아 둔 파일을 읽는 중.
LOADING = "loading"
#: 화면이 쓸 수 있는 상태.
READY = "ready"
#: 받은 것도 없고 예전에 받아 둔 것도 없는 상태.
EMPTY = "empty"


@dataclass(frozen=True)
class _Snapshot:
    """한 번 읽어 만든 것 한 벌. 읽은 파일·색인·배포일이 늘 같은 회차입니다."""

    loaded: list[LoadedMaster] = field(default_factory=list)
    index: MasterIndex | None = None
    #: 국내 마스터가 가리키는 배포일. 국내 마스터가 없으면 `None`.
    published: date | None = None
    #: 이 한 벌을 만든 시각(한국시간). 아직 읽은 적이 없으면 `None`.
    built_at: datetime | None = None


class MasterService:
    """마스터를 받고, 읽고, 색인을 들고 있습니다.

    한 번에 하나만 받도록 잠급니다. 화면이 「다시 받기」를 연달아 누르거나 앱을 켜자마자
    누르면 같은 파일을 두 번 받게 되는데, 그때 저장 중인 파일을 다른 쪽이 덮어씁니다.

    **새로 받는 동안에도 종목 조회는 끊기지 않습니다.** 새 색인을 끝까지 만든 다음
    한 번의 대입으로 바꿔 끼우므로, 그 전에 오는 요청은 앞의 목록으로, 그 뒤에 오는
    요청은 새 목록으로 답합니다. 사이에 「잠깐 없음」이 끼지 않습니다.
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = base_dir
        self._lock = threading.Lock()
        self._stage = IDLE
        self._snapshot = _Snapshot()
        self._downloads: list[DownloadResult] = []

    @property
    def base_dir(self) -> Path:
        return self._base_dir if self._base_dir is not None else MASTER_DIR

    @property
    def busy(self) -> bool:
        """지금 받거나 읽는 중인지."""
        return self._stage in (DOWNLOADING, LOADING)

    # ---- 읽기 ---------------------------------------------------------------

    def reload(self) -> None:
        """받아 둔 파일을 다시 읽어 색인을 새로 만듭니다. 내려받지는 않습니다.

        새 한 벌을 다 만든 뒤에 바꿔 끼웁니다. 읽다가 실패하면 앞의 것을 그대로
        두고 실패를 올립니다.
        """
        self._stage = LOADING
        try:
            loaded = load_all(self._base_dir)
            snapshot = _Snapshot(
                loaded=loaded,
                index=build_index(loaded),
                published=published_on(loaded),
                built_at=now_kst(),
            )
        except BaseException:
            self._stage = READY if self.available else EMPTY
            raise
        self._snapshot = snapshot
        self._stage = READY if self.available else EMPTY

    @property
    def _index(self) -> MasterIndex | None:
        """지금 쓰는 색인. 시험이 색인 안쪽을 들여다볼 때 씁니다."""
        return self._snapshot.index

    @property
    def available(self) -> bool:
        """화면에 내놓을 종목이 하나라도 있는지."""
        return _has_rows(self._snapshot)

    @property
    def published(self) -> date | None:
        """지금 쓰는 국내 마스터의 배포일. 국내 마스터가 없으면 `None`."""
        return self._snapshot.published

    def index(self) -> dict[str, object] | None:
        """`/api/master/index` 가 돌려줄 것. 아직 읽은 것이 없으면 `None` 입니다."""
        snapshot = self._snapshot
        if not _has_rows(snapshot) or snapshot.index is None:
            return None
        return snapshot.index.payload

    def contracts(self, *, market: str, product_code: str, expiry: str) -> list[dict[str, str]]:
        """품목 하나의 한 만기월에 걸린 옵션 종목.

        어느 줄을 남길지는 색인을 만들 때 이미 정해져 있어, 여기서 다시 판단하지
        않습니다. 색인과 체인이 같은 종목을 두고 다른 답을 낼 수 없습니다.
        """
        index = self._snapshot.index
        if index is None:
            return []
        return index.chain(market=market, product_code=product_code, expiry=expiry)

    def locate(self, market: str, code: str) -> dict[str, str] | None:
        """종목코드 하나의 줄. 색인이 없거나 종목이 없으면 `None`."""
        index = self._snapshot.index
        if index is None:
            return None
        return index.locate(market, code)

    def product_of(self, market: str, code: str) -> dict[str, str] | None:
        """종목코드가 가리키는 품목의 줄. 종목이 마스터에 없어도 코드 모양으로 풉니다."""
        index = self._snapshot.index
        if index is None:
            return None
        return index.product_of(market, code)

    def night_products(self) -> frozenset[str]:
        """야간 줄이 있는 국내 품목. 색인이 없으면 비어 있습니다."""
        index = self._snapshot.index
        return index.night_products if index is not None else frozenset()

    # ---- 내려받기 -----------------------------------------------------------

    def refresh(self, *, force: bool = False) -> list[DownloadResult]:
        """받고 나서 다시 읽습니다. 다른 쪽이 이미 받고 있으면 아무것도 하지 않습니다.

        오래 걸리는 일이라 **별도 스레드에서 부르세요.** 요청을 처리하는 자리에서
        그대로 부르면 그 요청이 다 받을 때까지 멈춰 있습니다.

        **새로 받아 온 파일이 있을 때만 다시 읽습니다.** 받은 것이 없으면 디스크의
        파일이 그대로라 다시 읽어도 같은 색인이 나옵니다. 15만 행을 한 번 더 읽는
        데다, 읽으면서 남기는 경고가 한 벌 더 찍혀 기록이 두 배로 보입니다. 아직
        한 번도 읽은 적이 없으면 받은 것이 없어도 읽습니다.
        """
        if not self._lock.acquire(blocking=False):
            logger.info("종목 마스터를 이미 받고 있습니다. 이번 요청은 건너뜁니다.")
            return []
        try:
            self._stage = DOWNLOADING
            fetched: list[DownloadResult] = []
            try:
                fetched = download_all(self._base_dir, force=force)
                self._downloads = fetched
            except Exception:
                # 받기가 실패해도 앱은 계속 돌아야 합니다. 예전에 받아 둔 것이 있으면
                # 그것으로 화면을 세웁니다.
                #
                # **여기로 오는 것은 통신 실패가 아닙니다.** 파일 하나를 못 받는 것은
                # `download_all` 이 안에서 결과로 바꿔 돌려주고 경고도 그쪽에서
                # 남깁니다. 여기까지 올라오는 것은 파일을 쓰지 못했다거나 하는 다른
                # 문제라, 원인을 알아야 고칠 수 있어 스택을 그대로 남깁니다.
                logger.exception("종목 마스터를 받지 못했습니다. 받아 둔 파일로 화면을 세웁니다.")
            if self._snapshot.index is None or any(item.ok for item in fetched):
                self.reload()
            else:
                self._stage = READY if self.available else EMPTY
            return self._downloads
        finally:
            self._lock.release()

    # ---- 상태 ---------------------------------------------------------------

    def status(self) -> dict[str, object]:
        """어느 날짜 파일을 쓰는지, 파일별 행 수, 실패한 파일, 「없음」 여부.

        마스터가 하나도 없어도 오류가 아닙니다 — 「없음」도 화면이 알아야 하는 상태라
        이 라우트는 늘 정상으로 답합니다.
        """
        snapshot = self._snapshot
        by_name = {item.master.name: item for item in snapshot.loaded}
        files: list[dict[str, object]] = []

        for master in MASTER_FILES:
            item = by_name.get(master.name)
            entry: dict[str, object] = {
                "name": master.name,
                "market": master.market,
                "parsed": master.parsed,
                "date": item.stamp if item else None,
                "rows": len(item.table) if item and item.table is not None else None,
                "ok": bool(item and item.ok),
                "error": item.error if item else "아직 읽지 않았습니다.",
            }
            files.append(entry)

        dates = sorted({str(f["date"]) for f in files if f["date"]}, reverse=True)
        index = snapshot.index.payload if snapshot.index else {}
        return {
            "stage": self._stage,
            "available": _has_rows(snapshot),
            "dates": dates,
            "files": files,
            "failed": [str(f["name"]) for f in files if not f["ok"]],
            # 같은 종목코드의 주간·야간 줄에서 값이 어긋나 색인에서 뺀 종목입니다.
            # 보통은 빈 목록이고, 배포 형식이 바뀌면 여기에 나타납니다.
            "folding_conflicts": index.get("folding_conflicts", []),
            # 값 칸 폭 후보를 스스로 재 본 결과입니다. `mismatched` 와
            # `without_candidate` 가 비어 있어야 정상이고, 여기 무엇이 뜨면 그
            # 무리의 폭은 믿지 마세요.
            "width_checks": _width_checks(index),
            "master_dir": str(self.base_dir),
            # 지금 목록을 만든 시각과 그 목록의 국내 배포일. 배포일이 오늘보다
            # 앞이면 아직 오늘 목록을 받지 못한 것입니다.
            "refreshed_at": iso(snapshot.built_at) if snapshot.built_at else None,
            "published": snapshot.published.isoformat() if snapshot.published else None,
        }


def _has_rows(snapshot: _Snapshot) -> bool:
    return any(item.table is not None and len(item.table) for item in snapshot.loaded)


def published_on(loaded: list[LoadedMaster]) -> date | None:
    """국내 마스터가 어느 날 배포된 것인지.

    국내 마스터의 잔존일수(`rem_cnt`)는 배포일을 첫날로 세어 만기일(`exp_ym`
    여덟 자리)까지 센 값입니다. 그래서 줄마다 「만기일 − 잔존일수 + 1」이 배포일이
    되고, 가장 많이 나온 날을 답합니다. 다른 기준으로 오는 줄이 몇 개 섞여도
    흔들리지 않습니다. 국내 마스터가 없으면 `None` 입니다.
    """
    counts: Counter[date] = Counter()
    for item in loaded:
        table = item.table
        if table is None or table.market != DOMESTIC:
            continue
        if "exp_ym" not in table.fields or "rem_cnt" not in table.fields:
            continue
        at_expiry = table.index_of("exp_ym")
        at_left = table.index_of("rem_cnt")
        for row in table.rows:
            expiry = str(row[at_expiry]).strip()
            left = str(row[at_left]).strip()
            if len(expiry) != 8 or not expiry.isdigit() or not left.isdigit():
                continue
            try:
                day = datetime.strptime(expiry, "%Y%m%d").date()
            except ValueError:
                continue
            counts[day - timedelta(days=int(left) - 1)] += 1
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _width_checks(index: dict[str, object]) -> dict[str, object]:
    """색인이 스스로 재 본 값 칸 폭 결과. 색인이 없으면 빈 사전입니다."""
    candidates = index.get("width_candidates")
    if not isinstance(candidates, dict):
        return {}
    checks = candidates.get("checks")
    return checks if isinstance(checks, dict) else {}


#: 앱이 하나 들고 쓰는 자리. 테스트는 `MasterService(임시 폴더)` 를 따로 만들어 쓰세요.
service = MasterService()
