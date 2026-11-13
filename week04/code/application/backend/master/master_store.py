"""받아 둔 종목 마스터를 사용자 홈에 날짜별로 두고 다시 읽습니다.

    ~/NHFutures/master/20260902/fut_opt.mst
                              /stk_fut_opt.mst
                              ...

설정 파일과 같은 홈 폴더이고 저장소 안이 아닙니다. 저장소에는 마스터를 동봉하지
않습니다 — 종목 목록은 매일 바뀌어서 동봉한 순간부터 틀린 값이 됩니다.

날짜 폴더로 나누는 이유는 **오늘 것을 못 받았을 때 어제 것으로 화면을 세우기**
위해서입니다. 파일 하나만 못 받았으면 그 파일만 옛것을 씁니다.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from backend.master.master_files import MASTER_FILES, MasterFile
from backend.master.master_parser import MasterParseError, MasterTable, parse_master_file

logger = logging.getLogger(__name__)

#: 마스터를 두는 곳. 설정 파일과 같은 홈 폴더입니다.
MASTER_DIR = Path.home() / "NHFutures" / "master"

#: 남겨 두는 날짜 폴더 수. 오늘 것을 못 받았을 때 되짚어 갈 만큼만 남깁니다.
KEEP_DATES = 3

_DATE_DIR = re.compile(r"^\d{8}$")


def _root(base_dir: Path | None) -> Path:
    return base_dir if base_dir is not None else MASTER_DIR


def today_stamp(today: date | None = None) -> str:
    """오늘 날짜 폴더의 이름 (`20260902`)."""
    return (today or date.today()).strftime("%Y%m%d")


def date_dirs(base_dir: Path | None = None) -> list[Path]:
    """날짜 폴더를 최신 순으로 돌려줍니다.

    이름이 여덟 자리 숫자인 폴더만 봅니다. 사용자가 다른 폴더를 만들어 두었더라도
    건드리지 않습니다.
    """
    root = _root(base_dir)
    if not root.is_dir():
        return []
    found = [p for p in root.iterdir() if p.is_dir() and _DATE_DIR.match(p.name)]
    return sorted(found, key=lambda p: p.name, reverse=True)


def save_master(name_or_file: str | MasterFile, content: bytes, *, stamp: str,
                base_dir: Path | None = None) -> Path:
    """압축을 푼 마스터 한 개를 날짜 폴더에 씁니다.

    같은 이름이 이미 있으면 덮어씁니다. 받다가 중간에 끊긴 파일이 남아 있는 것보다
    새로 받은 것으로 바꾸는 편이 낫습니다.
    """
    file_name = (
        name_or_file.file_name if isinstance(name_or_file, MasterFile) else name_or_file
    )
    folder = _root(base_dir) / stamp
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / file_name
    path.write_bytes(content)
    return path


def find_master(master: MasterFile, base_dir: Path | None = None) -> tuple[Path, str] | None:
    """이 파일의 가장 최근 사본과 그 날짜를 찾습니다.

    오늘 폴더에 없으면 그 전날로 되짚어 갑니다. 하나도 없으면 `None` 입니다.
    """
    for folder in date_dirs(base_dir):
        candidate = folder / master.file_name
        if candidate.is_file():
            return candidate, folder.name
    return None


def has_all_files(stamp: str, base_dir: Path | None = None) -> bool:
    """그 날짜 폴더에 아홉 개가 다 있는지. 다 있으면 그날은 다시 받지 않습니다."""
    folder = _root(base_dir) / stamp
    if not folder.is_dir():
        return False
    return all((folder / m.file_name).is_file() for m in MASTER_FILES)


@dataclass(frozen=True)
class LoadedMaster:
    """파일 하나를 읽어 본 결과. 성공이든 실패든 한 자리에 담습니다."""

    master: MasterFile
    #: 어느 날짜 폴더에서 읽었는지. 못 찾았으면 `None`.
    stamp: str | None
    #: 파싱 결과. 파싱하지 않는 파일이거나 실패했으면 `None`.
    table: MasterTable | None
    #: 실패 사유. 성공했으면 `None`.
    error: str | None

    @property
    def ok(self) -> bool:
        return self.error is None


def load_master(master: MasterFile, base_dir: Path | None = None) -> LoadedMaster:
    """파일 하나를 찾아서 읽습니다. 실패해도 예외를 내지 않고 사유를 담아 돌려줍니다."""
    found = find_master(master, base_dir)
    if found is None:
        return LoadedMaster(master, None, None, "받아 둔 파일이 없습니다.")

    path, stamp = found
    if not master.parsed:
        # 받아만 두는 파일입니다. 있다는 것까지만 확인합니다.
        return LoadedMaster(master, stamp, None, None)

    try:
        table = parse_master_file(
            path, master.fields or (), name=master.name, market=master.market
        )
    except MasterParseError as exc:
        return LoadedMaster(master, stamp, None, str(exc))

    return LoadedMaster(master, stamp, table, None)


def load_all(base_dir: Path | None = None) -> list[LoadedMaster]:
    """아홉 개를 전부 읽어 봅니다.

    한 파일이 실패해도 나머지는 그대로 씁니다. 다만 **절반 넘게 실패하면** 배치가
    바뀌었거나 받는 쪽이 고장 난 것일 수 있어 경고를 남깁니다 — 그냥 두면
    「종목이 몇 개 없네」로 보이고 아무도 알아채지 못합니다.
    """
    loaded = [load_master(m, base_dir) for m in MASTER_FILES]

    failed = [item for item in loaded if not item.ok]
    if failed and len(failed) * 2 > len(loaded):
        logger.warning(
            "종목 마스터 %d개 중 %d개를 읽지 못했습니다: %s",
            len(loaded),
            len(failed),
            "; ".join(f"{item.master.name} — {item.error}" for item in failed),
        )
    return loaded


def prune_old_dates(base_dir: Path | None = None, keep: int = KEEP_DATES) -> list[str]:
    """오래된 날짜 폴더를 지우고, 지운 날짜를 돌려줍니다.

    최신 `keep` 개를 남기되, **지금 쓰고 있는 사본이 든 폴더는 그보다 오래됐어도
    남깁니다.** 어떤 파일을 여러 날 못 받으면 그 파일의 마지막 사본이 오래된 폴더에만
    남는데, 날짜만 보고 지우면 그 종목들이 통째로 사라집니다. 못 받은 파일일수록
    옛 사본이 필요하다는 점에서, 날짜 기준만으로 지우는 것은 앞뒤가 안 맞습니다.

    지우기 전에 무엇을 지우는지 로그에 남깁니다 — 사용자 홈에서 지우는 일이라
    조용히 하면 안 됩니다.
    """
    folders = date_dirs(base_dir)
    in_use = {
        found[1] for found in (find_master(m, base_dir) for m in MASTER_FILES) if found
    }
    doomed = [f for f in folders[keep:] if f.name not in in_use]
    removed: list[str] = []

    for folder in doomed:
        files = sorted(p.name for p in folder.iterdir() if p.is_file())
        logger.info("오래된 종목 마스터를 지웁니다: %s (%s)", folder, ", ".join(files) or "빈 폴더")
        try:
            for path in folder.iterdir():
                if path.is_file():
                    path.unlink()
            folder.rmdir()
        except OSError as exc:
            # 지우기에 실패해도 앱은 계속 돌아야 합니다. 다음 실행에서 다시 시도합니다.
            logger.warning("종목 마스터 폴더를 지우지 못했습니다: %s (%s)", folder, exc)
            continue
        removed.append(folder.name)

    return removed
