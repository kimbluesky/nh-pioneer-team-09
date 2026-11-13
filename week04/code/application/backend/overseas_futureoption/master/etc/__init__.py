"""CME·OPRA·LME 외의 거래소 마스터 — 선물·스프레드와 옵션이 파일 둘로 옵니다.

한 파일 안에 여러 거래소가 섞여 옵니다. 어느 거래소인지는 행마다 있는
`ExchName` 과 `exch` 로 판단하세요.
"""
from __future__ import annotations

from backend.overseas_futureoption.master import (
    OVERSEAS_FUTURES_FIELDS,
    OVERSEAS_OPTION_FIELDS,
)

FUTURES_FILE_NAME = "etc_fut.mst"
FUTURES_FIELDS = OVERSEAS_FUTURES_FIELDS

OPTION_FILE_NAME = "etc_opt.mst"
OPTION_FIELDS = OVERSEAS_OPTION_FIELDS
