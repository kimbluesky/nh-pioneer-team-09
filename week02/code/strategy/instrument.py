"""전략 버전이 가리키는 종목.

줄은 **품목**을 들고 다닙니다 — 시장(`market`)·품목코드(`productCode`)·갈래
(`productKind`)의 세 칸입니다. 특정 월물을 고른 줄만 종목코드(`contractCode`)를
하나 더 답니다.

## 왜 품목인가

종목코드는 마스터 판본마다 바뀝니다. 9월물이 만기를 넘기면 어제의 코드가
사라지고, 적어 둔 코드를 들고 있던 줄은 눌러도 열 것이 없어집니다. 품목은 그
사이에도 그대로라, 줄은 품목만 들고 다니고 **코드는 그때그때 색인이 답합니다.**

## 옛 줄의 역할

품목 칸이 생기기 전의 줄은 역할(`role`) 하나만 들고 있습니다. 역할은 「코스피
선물의 자리」처럼 굳어 있는 여덟 가지라, 읽을 때 품목으로 옮겨 낼 수 있습니다.

**옮기는 것은 읽을 때뿐이고 파일은 그대로 둡니다.** 파일을 고쳐 쓰면 품목 칸을
모르는 앱으로 돌아갔을 때 그 줄을 못 읽습니다. 읽는 자리 하나만 이 표를 지나면 화면은
새 칸만 보면 됩니다.
"""
from __future__ import annotations

from typing import Any

#: 품목의 갈래. 값은 종목 마스터 색인이 쓰는 것과 같습니다
#: (`backend/master/master_index.py`) — 한쪽이 「선물」이고 다른 쪽이 `futures`
#: 이면 두 표를 견줄 때마다 옮겨 적어야 합니다.
FUTURES = "futures"
OPTION = "option"
SPREAD = "spread"

KINDS = (FUTURES, OPTION, SPREAD)

#: 줄이 들고 다니는 종목 칸 셋. 월물은 고른 줄에만 있습니다.
PRODUCT_CODE = "productCode"
PRODUCT_KIND = "productKind"
CONTRACT_CODE = "contractCode"
MARKET = "market"

#: 옛 줄의 역할 → 품목.
#:
#: **화면의 `instrumentRoles.ts` 와 같은 표입니다.** 둘이 갈리면 같은 줄이
#: 화면에서는 코스피, 백엔드에서는 코스닥이 됩니다. 역할은 더 늘리지 않습니다 —
#: 새 줄은 품목을 직접 들고 다닙니다.
#:
#: 콜옵션 역할의 갈래는 `option` 입니다. **콜인지 풋인지는 품목이 아니라
#: 월물이 가릅니다** — 한 품목에 콜과 풋이 함께 걸려 있어, 품목 칸으로는 둘을
#: 나눌 수 없습니다.
ROLE_INSTRUMENTS: dict[str, dict[str, str]] = {
    "kospiFutures": {MARKET: "domestic", PRODUCT_CODE: "KOSPI", PRODUCT_KIND: FUTURES},
    "kospiCall": {MARKET: "domestic", PRODUCT_CODE: "KOSPI", PRODUCT_KIND: OPTION},
    "usdFutures": {MARKET: "domestic", PRODUCT_CODE: "USD", PRODUCT_KIND: FUTURES},
    "kosdaqFutures": {MARKET: "domestic", PRODUCT_CODE: "KOSDAQ150", PRODUCT_KIND: FUTURES},
    "jpyFutures": {MARKET: "domestic", PRODUCT_CODE: "JPY", PRODUCT_KIND: FUTURES},
    "esFutures": {MARKET: "overseas", PRODUCT_CODE: "ES", PRODUCT_KIND: FUTURES},
    "wtiFutures": {MARKET: "overseas", PRODUCT_CODE: "CL", PRODUCT_KIND: FUTURES},
    "goldFutures": {MARKET: "overseas", PRODUCT_CODE: "GC", PRODUCT_KIND: FUTURES},
}


def of_role(role: Any) -> dict[str, str] | None:
    """역할 하나가 가리키는 품목. 모르는 역할이면 `None` 입니다."""
    found = ROLE_INSTRUMENTS.get(str(role or "").strip())
    return dict(found) if found is not None else None


def filled(version: dict[str, Any]) -> dict[str, Any]:
    """품목 칸을 채운 줄. **이미 들고 있으면 그대로 둡니다.**

    품목을 직접 고른 줄이 역할도 함께 들고 있을 수 있습니다(고치는 중에 역할이
    남은 줄). 그때 표로 덮으면 사용자가 방금 고른 품목이 사라집니다.

    시장은 비어 있을 때만 채웁니다 — 담긴 값이 역할 표와 다르면 그것은 고쳐야
    할 줄이고, 여기서 조용히 맞추면 무엇이 어긋났는지 아무도 못 봅니다
    (`validate.instrument_problems` 가 짚습니다).
    """
    if not isinstance(version, dict):
        return version
    if str(version.get(PRODUCT_CODE) or "").strip():
        return version
    found = of_role(version.get("role"))
    if found is None:
        return version
    made = dict(version)
    made[PRODUCT_CODE] = found[PRODUCT_CODE]
    made[PRODUCT_KIND] = found[PRODUCT_KIND]
    if not str(made.get(MARKET) or "").strip():
        made[MARKET] = found[MARKET]
    return made
