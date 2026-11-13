"""내려받을 종목 마스터 아홉 개의 목록.

파일마다 이름·주소·컬럼 배치·어느 시장인지를 한자리에 모아 둡니다. 파일이 늘거나
주소가 바뀌면 여기만 고치면 됩니다.

**`lme_fut.mst` 는 받기만 하고 파싱하지 않습니다.** LME 는 화면에 나오지 않아
파싱한 값을 쓰는 곳이 없습니다. 파일은 받아 두므로, 파싱하려면 `fields` 에 해외
선물 컬럼 배치를 넣으면 됩니다.
"""
from __future__ import annotations

from dataclasses import dataclass

from backend.domestic_futureoption.master import fut_opt, idx_fut_opt, stk_fut_opt
from backend.overseas_futureoption.master import cme, etc, opra

#: 마스터 파일이 놓여 있는 주소의 앞부분. 뒤에 `{이름}.mst.gz` 가 붙습니다.
MASTER_URL_BASE = "https://apidev.futures.co.kr/guide/master"

#: 시장 구분. 프론트엔드가 쓰는 값과 같은 이름을 씁니다.
DOMESTIC = "domestic"
OVERSEAS = "overseas"


@dataclass(frozen=True)
class MasterFile:
    """마스터 파일 하나."""

    #: 압축을 푼 상태의 파일 이름 (`fut_opt.mst`). 사용자 홈에 이 이름으로 저장합니다.
    file_name: str
    #: 국내(`domestic`) 인지 해외(`overseas`) 인지.
    market: str
    #: 컬럼 배치. `None` 이면 받기만 하고 파싱하지 않습니다.
    fields: tuple[str, ...] | None

    @property
    def name(self) -> str:
        """확장자를 뺀 이름. 상태 응답과 주소에 씁니다."""
        return self.file_name.removesuffix(".mst")

    @property
    def url(self) -> str:
        """내려받을 주소. 압축된 상태로 옵니다."""
        return f"{MASTER_URL_BASE}/{self.file_name}.gz"

    @property
    def parsed(self) -> bool:
        return self.fields is not None


#: 내려받는 순서 그대로입니다. 한 번에 하나씩 순차로 받습니다.
MASTER_FILES: tuple[MasterFile, ...] = (
    MasterFile(fut_opt.FILE_NAME, DOMESTIC, fut_opt.FIELDS),
    MasterFile(stk_fut_opt.FILE_NAME, DOMESTIC, stk_fut_opt.FIELDS),
    MasterFile(idx_fut_opt.FILE_NAME, DOMESTIC, idx_fut_opt.FIELDS),
    MasterFile(cme.FUTURES_FILE_NAME, OVERSEAS, cme.FUTURES_FIELDS),
    MasterFile(cme.OPTION_FILE_NAME, OVERSEAS, cme.OPTION_FIELDS),
    MasterFile("lme_fut.mst", OVERSEAS, None),
    MasterFile(opra.OPTION_FILE_NAME, OVERSEAS, opra.OPTION_FIELDS),
    MasterFile(etc.OPTION_FILE_NAME, OVERSEAS, etc.OPTION_FIELDS),
    MasterFile(etc.FUTURES_FILE_NAME, OVERSEAS, etc.FUTURES_FIELDS),
)

#: 이름으로 찾을 때 씁니다.
MASTER_FILES_BY_NAME: dict[str, MasterFile] = {m.name: m for m in MASTER_FILES}

#: 파싱까지 하는 파일만.
PARSED_MASTER_FILES: tuple[MasterFile, ...] = tuple(m for m in MASTER_FILES if m.parsed)
