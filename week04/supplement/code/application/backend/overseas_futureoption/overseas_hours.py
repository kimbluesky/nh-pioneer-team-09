"""해외 장운영정보 응답을 화면·판정이 쓰는 모양으로 바꿉니다.

품목마다 한 줄이고 거래시간이 셋 옵니다 — 1차(`trd_tm_inf`)·2차(`trd_tm_inf_2`)·
익일(`nxd_trd_tm_inf`). **전부 한국시간**이고 서머타임이 반영된 값입니다. 없는
구간은 `00:00 ~ 00:00` 으로 오는데, 그것은 구간이 아니라 「없음」이라 `None` 입니다.

줄의 열쇠는 (거래소, 품목코드)입니다. 품목코드는 마스터의 `comd` 와 같은 글자이고
(옵션 `O_…`, 스프레드 `S_…`), 거래소는 API 낱말(`CME`·`EUREX`·`SGX`·`HKEX`·`ICE`·
`CBOE`·`JPX`)입니다. 마스터의 거래소 코드(`01`·`04`…)와 잇는 표는 `EXCHANGES` 에
있습니다 — 레이아웃 문서의 표를 옮긴 것이고, 문서가 `HKE` 라 적은 것을 API 는
`HKEX` 로 주어 그 한 곳만 API 를 따릅니다.

한 번에 100줄까지만 오므로 여러 응답을 합쳐 받습니다. 같은 열쇠가 두 번 오면
뒤엣것이 앞엣것을 덮습니다.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from backend.master.master_index import FUTURES, OPTION, SPREAD
from backend.sessions.hours import range_dict

#: 마스터 `exch` 코드 → 장운영정보의 거래소 낱말.
EXCHANGES: dict[str, str] = {
    "01": "CME",
    "04": "EUREX",
    "05": "SGX",
    "06": "HKEX",
    "11": "ICE",
    "15": "CBOE",
}

OTHER = "other"
_KIND = {"F": FUTURES, "O": OPTION, "S": SPREAD}

#: 품목그룹코드. 전체를 나눠 받을 때 이 차례로 부릅니다(명세의 나열 순서).
PRODUCT_GROUPS = ("FG1", "FG2", "FG3", "FG4", "FG5", "FG6", "FG9")


def market_operations(raws: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """장운영정보 응답들 → `{products}`. 열쇠가 겹치면 뒤엣것이 남습니다."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in raws:
        rows = raw.get("Occurs1") if isinstance(raw, dict) else None
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            made = _product(row)
            merged[(made["exchange"], made["productCode"])] = made
    return {"products": list(merged.values())}


def _product(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "exchange": str(row.get("occurs1_exch_cd") or "").strip(),
        "productCode": str(row.get("occurs1_prd_cd") or "").strip(),
        "kind": kind_of(row.get("occurs1_fut_opt_div_cd")),
        "name": str(row.get("occurs1_prd_nm") or "").strip(),
        "koreanName": str(row.get("occurs1_ko_prd_nm") or "").strip(),
        "group": str(row.get("occurs1_prd_grp_cd") or "").strip(),
        "groupName": str(row.get("occurs1_prd_grp_nm") or "").strip(),
        "currency": str(row.get("occurs1_cur_cd") or "").strip(),
        "trading": range_dict(row.get("occurs1_trd_tm_inf")),
        "second": range_dict(row.get("occurs1_trd_tm_inf_2")),
        "nextDayTrading": range_dict(row.get("occurs1_nxd_trd_tm_inf")),
        "settlementWindow": range_dict(row.get("occurs1_sett_pric_tm_inf")),
    }


def kind_of(kind_code: Any) -> str:
    """상품구분코드(F 선물·O 옵션·S 스프레드)를 화면 낱말로. 그 밖은 `other`.

    장운영정보와 품목별세부정보가 같은 코드를 씁니다. 품목별세부정보는 `Z`(기타)도
    낸다고 명세에 적혀 있고, 그 값도 `other` 입니다.
    """
    return _KIND.get(str(kind_code or "").strip(), OTHER)


def exchange_of(exch_code: str) -> str:
    """마스터 거래소 코드의 API 낱말. 표에 없으면 코드 그대로입니다."""
    return EXCHANGES.get(str(exch_code or "").strip(), str(exch_code or "").strip())
