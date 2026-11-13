"""`stk_fut_opt.mst` — 국내 마스터 중 한 파일.

개별주식 선물과 옵션이 들어오지만, 어느 쪽이 얼마나 오는지는 배포 시점마다
달라집니다. 행마다 있는 `mrkt_tp` 와 `fo_tp` 로 판단하세요. 컬럼 배치는 다른
국내 파일과 같습니다.
"""
from __future__ import annotations

from backend.domestic_futureoption.master import DOMESTIC_FIELDS

FILE_NAME = "stk_fut_opt.mst"
FIELDS = DOMESTIC_FIELDS
