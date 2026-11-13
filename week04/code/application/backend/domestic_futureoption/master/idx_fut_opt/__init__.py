"""`idx_fut_opt.mst` — 국내 마스터 중 한 파일.

이름의 `idx` 는 지수를 뜻하지 않습니다. 담긴 내용은 배포 시점마다 달라지므로
행마다 있는 `mrkt_tp` 로 판단하세요. 컬럼 배치는 다른 국내 파일과 같습니다.
"""
from __future__ import annotations

from backend.domestic_futureoption.master import DOMESTIC_FIELDS

FILE_NAME = "idx_fut_opt.mst"
FIELDS = DOMESTIC_FIELDS
