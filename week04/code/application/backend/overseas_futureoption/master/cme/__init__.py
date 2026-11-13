"""CME 마스터 — 선물·스프레드와 옵션이 파일 둘로 옵니다."""
from __future__ import annotations

from backend.overseas_futureoption.master import (
    OVERSEAS_FUTURES_FIELDS,
    OVERSEAS_OPTION_FIELDS,
)

FUTURES_FILE_NAME = "cme_fut.mst"
FUTURES_FIELDS = OVERSEAS_FUTURES_FIELDS

OPTION_FILE_NAME = "cme_opt.mst"
OPTION_FIELDS = OVERSEAS_OPTION_FIELDS
