"""`fut_opt.mst` — 국내 지수 선물·스프레드·옵션 마스터.

**파일 이름과 담긴 내용이 어긋납니다.** 어느 시장인지는 파일 이름이 아니라 행마다
있는 `mrkt_tp` 값으로 판단하세요. 한 파일 안에 `mrkt_tp` 가 섞여 오는 것도 실제로
확인된 일이라, 파일 단위로 단정하면 안 됩니다.
"""
from __future__ import annotations

from backend.domestic_futureoption.master import DOMESTIC_FIELDS

FILE_NAME = "fut_opt.mst"
FIELDS = DOMESTIC_FIELDS
