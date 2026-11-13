"""종목 마스터를 주소에서 받아 압축을 풉니다.

아홉 개를 **한 번에 하나씩 순차로** 받습니다. 한꺼번에 던지지 않는 것은 NH선물의
호출 제한이 이 정적 파일에도 걸릴 수 있기 때문입니다. 아홉 개를 순서대로
받아도 앱을 켜고 나서 뒤에서 도는 일이라 화면을 기다리게 하지 않습니다.

**오늘 날짜 폴더에 아홉 개가 다 있으면 아예 받지 않습니다.** 종목 목록은 하루에 한 번
바뀌므로 켤 때마다 다시 받을 이유가 없습니다.
"""
from __future__ import annotations

import gzip
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from backend.master.master_files import MASTER_FILES, MasterFile
from backend.master.master_store import has_all_files, prune_old_dates, save_master, today_stamp
from backend.net_failure import cannot_connect

logger = logging.getLogger(__name__)

#: 파일 하나를 받을 때 기다리는 시간(초). 가장 큰 파일이 1.3MB 남짓이라 넉넉합니다.
TIMEOUT_SECONDS = 30.0

#: 연결이 될 때까지만 기다리는 시간(초). 짧게 잡습니다 — 서버에 닿지도 못하는
#: 상황이면 아홉 개가 전부 같은 이유로 실패하는데, 파일마다 오래 기다리면 그 시간이
#: 아홉 배로 늘어납니다. 화면은 그동안 옛 목록으로 서 있습니다.
CONNECT_TIMEOUT_SECONDS = 10.0

#: 한 파일을 다시 시도하는 횟수. 처음 한 번에 이 수만큼 더 해 봅니다.
#: 무한히 다시 걸지 않습니다 — 서버가 닫혀 있으면 아홉 개가 전부 같은 이유로 실패하고,
#: 그때는 어제 받아 둔 것으로 화면을 세우는 편이 낫습니다.
#:
#: **연결이 서지도 않은 실패는 다시 걸지 않습니다.** 이름 풀이가 안 되거나 연결이
#: 거부되는 상황이면 다시 걸어도 같은 답이고, 앱이 뜨는 시간만 그만큼 늦어집니다.
#: 받는 도중 멈추거나 끊긴 것은 다릅니다 — 연결은 섰던 것이라 다시 걸면 받아질 수
#: 있어 전처럼 한 번 더 겁니다.
RETRIES = 1


class MasterDownloadError(Exception):
    """마스터 하나를 받지 못한 경우입니다."""


@dataclass(frozen=True)
class DownloadResult:
    """파일 하나를 받아 본 결과."""

    master: MasterFile
    #: 저장한 경로. 실패했으면 `None`.
    path: Path | None
    #: 실패 사유. 성공했으면 `None`.
    error: str | None

    @property
    def ok(self) -> bool:
        return self.error is None


def unpack(content: bytes, *, name: str = "") -> bytes:
    """받은 `.gz` 를 풉니다.

    Raises:
        MasterDownloadError: 압축이 깨졌거나 받다가 끊긴 경우.
    """
    try:
        return gzip.decompress(content)
    except (OSError, EOFError) as exc:
        raise MasterDownloadError(
            f"{name or '마스터 파일'} 의 압축을 풀지 못했습니다. 받는 중에 끊겼을 수 있습니다: {exc}"
        ) from exc


def _failure_text(master: MasterFile, reason: object) -> str:
    """파일 하나가 실패한 사유. 화면의 마스터 상태에 그대로 섭니다."""
    return f"{master.file_name} 을 받지 못했습니다: {reason}"


def fetch_one(client: httpx.Client, master: MasterFile) -> bytes:
    """마스터 하나를 받아 압축을 푼 내용을 돌려줍니다.

    Raises:
        MasterDownloadError: 통신에 실패했거나 압축이 깨진 경우.
    """
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            response = client.get(master.url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            last = exc
            # 연결이 서지도 않은 것이면 다시 걸지 않습니다 — 까닭은 `RETRIES` 에.
            # 받는 **도중** 멈추거나 끊긴 것은 전처럼 한 번 더 걸어 봅니다.
            if attempt < RETRIES and not cannot_connect(exc):
                logger.info("%s 를 다시 받아 봅니다 (%s)", master.file_name, exc)
                continue
            raise MasterDownloadError(_failure_text(master, exc)) from exc
        return unpack(response.content, name=master.file_name)

    # 여기까지 오지 않습니다. 마지막 예외를 삼키지 않도록 남겨 둡니다.
    raise MasterDownloadError(_failure_text(master, last))


def download_all(
    base_dir: Path | None = None,
    *,
    stamp: str | None = None,
    force: bool = False,
    client: httpx.Client | None = None,
) -> list[DownloadResult]:
    """아홉 개를 순차로 받아 오늘 날짜 폴더에 씁니다.

    Args:
        base_dir: 마스터를 두는 폴더. 생략하면 사용자 홈입니다.
        stamp: 날짜 폴더 이름. 생략하면 오늘입니다.
        force: 오늘 폴더가 이미 다 차 있어도 다시 받습니다.
        client: 쓸 HTTP 연결. 생략하면 새로 만들고 끝나면 닫습니다.

    하나가 실패해도 나머지는 계속 받습니다. 실패한 파일은 어제 것이 있으면 그것을
    쓰게 되므로, 여기서 멈추면 받을 수 있었던 파일까지 못 쓰게 됩니다.

    **연결이 서지도 않으면 거기서 그만둡니다.** 아홉 개가 한 주소에서 오므로 이름
    풀이가 안 되면 나머지도 같은 결과입니다. 남은 파일까지 하나씩 가 보면 앱이 뜨는
    시간만 늦어지고 기록이 같은 줄로 찹니다. 그래서 그 자리에서 **경고 한 줄**을
    남기고 남은 파일은 같은 사유로 실패 처리합니다 — 파일별 사유는 기록에 남기되
    `DEBUG` 로 둡니다. 화면은 받아 둔 파일로 그대로 섭니다.

    **받는 도중 멈추거나 끊긴 것은 여기 들지 않습니다.** 그 파일만 실패로 두고
    나머지는 그대로 받습니다 — 큰 파일 하나가 도중에 멈췄다고 그날 나머지 여덟을
    통째로 포기할 이유가 없습니다.
    """
    stamp = stamp or today_stamp()

    if not force and has_all_files(stamp, base_dir):
        logger.info("오늘(%s) 받아 둔 종목 마스터가 이미 있습니다. 내려받지 않습니다.", stamp)
        return []

    owned = client is None
    client = client or httpx.Client(
        timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
        follow_redirects=True,
    )

    results: list[DownloadResult] = []
    # 서버에 닿지 못한 사유. 한 번 채워지면 남은 파일은 가 보지 않습니다.
    offline: BaseException | None = None
    try:
        for master in MASTER_FILES:
            if offline is not None:
                same = _failure_text(master, offline)
                logger.debug("%s", same)
                results.append(DownloadResult(master, None, same))
                continue

            try:
                content = fetch_one(client, master)
            except MasterDownloadError as exc:
                if cannot_connect(exc):
                    offline = exc.__cause__ or exc
                    logger.warning(
                        "종목 마스터를 받지 못했습니다(%s). 받아 둔 파일로 화면을 세웁니다.",
                        offline,
                    )
                    logger.debug("%s", exc)
                else:
                    logger.warning("%s", exc)
                results.append(DownloadResult(master, None, str(exc)))
                continue

            path = save_master(master, content, stamp=stamp, base_dir=base_dir)
            results.append(DownloadResult(master, path, None))
    finally:
        if owned:
            client.close()

    if any(item.ok for item in results):
        prune_old_dates(base_dir)

    return results
