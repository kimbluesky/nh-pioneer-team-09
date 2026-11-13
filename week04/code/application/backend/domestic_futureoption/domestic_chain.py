"""국내 옵션전체시세 응답을 옵션 체인(행사가별 콜·풋 한 줄) 모양으로 바꿉니다.

한 줄이 행사가 하나이고 그 줄에 콜과 풋이 함께 옵니다. **행마다 실제 콜·풋
종목코드가 실립니다** — 화면이 줄을 고르면 그 코드로 현재가를 조회하고 실시간을
구독합니다. 서버는 행사가가 높은 것부터 내려오는 차례로 주고, 한 번에 다 못 주면
`nxt_stkr_pric`(다음 행사가)로 이어 받습니다 — 이어 받는 일은 `collect()` 가 합니다.

**야간 응답에는 시세가 없습니다.** 종목코드·이름·전일종가·미결제약정만 옵니다
(명세도 그렇습니다). 그래서 야간 체인의 현재가·호가·이론가·그릭스는 전부 `null`
이고, 칸은 주간과 같이 둡니다 — 화면이 세션에 따라 모양을 바꾸지 않아도 됩니다.

가격 칸(현재가·호가·시가·고가·저가·전일종가·이론가)의 `0` 은 값이 아니라 **없음**
입니다 — 옵션 가격은 최소 0.01 이라 0 은 거래·호가가 없다는 뜻이고, 그대로 두면
화면에 「0.00」이 값처럼 섭니다. 대비·미결제·거래량·그릭스의 0 은 값입니다.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend import market_values

# 이어받기를 이만큼까지만 따라갑니다. 코스피200 이 주간 261줄·야간 400줄씩 두세
# 쪽이고, 그보다 많이 돌면 서버나 저장본이 같은 쪽을 되풀이하는 것입니다.
MAX_PAGES = 8


def collect(fetch: Callable[[str], dict[str, Any]], *, first_key: str) -> list[dict[str, Any]]:
    """이어받기 키를 따라 쪽을 다 모읍니다. 원문 쪽의 목록입니다.

    **같은 행사가가 다시 오면 멈춥니다.** 저장본으로 돌 때는 다음 쪽을 물어도
    같은 기록이 다시 오고, 서버가 마지막 행사가를 다음 쪽 첫 줄에 겹쳐 줄 수도
    있어, 새 행사가가 하나도 없는 쪽은 끝으로 봅니다. 겹친 줄은 `chain()` 이
    걸러 냅니다.
    """
    pages: list[dict[str, Any]] = []
    seen: set[str] = set()
    key = first_key
    for _ in range(MAX_PAGES):
        raw = fetch(key)
        pages.append(raw)
        strikes = {_strike_text(row) for row in _rows(raw)}
        if not strikes - seen:
            break
        seen |= strikes
        key = str(raw.get("nxt_stkr_pric") or "").strip()
        if not key:
            break
    return pages


def chain(pages: list[dict[str, Any]], *, night: bool = False) -> dict[str, Any]:
    """모은 쪽들을 체인 하나로. 첫 쪽의 기초자산·선물 값을 씁니다.

    줄은 서버가 준 차례(행사가 높은 것부터)이고, 쪽이 겹쳐 같은 행사가가 두 번
    오면 앞엣것만 남깁니다.
    """
    head = pages[0] if pages else {}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in pages:
        for row in _rows(raw):
            strike = _strike_text(row)
            if strike in seen:
                continue
            seen.add(strike)
            rows.append(_row(row))

    return {
        "market": "domestic",
        "session": "night" if night else "day",
        "underlying": _underlying(head),
        "future": None if night else _future(head),
        "rows": rows,
        "count": len(rows),
    }


def _underlying(raw: dict[str, Any]) -> dict[str, Any] | None:
    """기초자산(현물) — 코드·이름·현물가·전일대비·등락률."""
    if not raw:
        return None
    return {
        "code": _text(raw.get("spt_sym")),
        "name": _text(raw.get("und_nm")),
        "price": _positive(raw.get("spt_pric")),
        "change": market_values.number(raw.get("chg")),
        "changePct": market_values.number(raw.get("rt")),
    }


def _future(raw: dict[str, Any]) -> dict[str, Any] | None:
    """당월 선물 — 주간 응답에만 옵니다."""
    if not raw or not _text(raw.get("fut_sym")):
        return None
    return {
        "code": _text(raw.get("fut_sym")),
        "last": _positive(raw.get("fut_last_pric")),
        "change": market_values.number(raw.get("fut_chg")),
        "changePct": market_values.number(raw.get("fut_rt")),
        "daysLeft": market_values.count(raw.get("fut_rem_dy")),
    }


def _row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "strike": market_values.number(row.get("occurs1_strk_pric")),
        "call": _leg(row, "c"),
        "put": _leg(row, "p"),
    }


def _leg(row: dict[str, Any], side: str) -> dict[str, Any]:
    """콜(`c`) 또는 풋(`p`) 한쪽. 야간에 없는 칸은 `null` 입니다."""

    def get(name: str) -> Any:
        return row.get(f"occurs1_{side}_{name}")

    return {
        "code": _text(get("sym_exch")),
        "name": _text(get("sym_nm")),
        "last": _positive(get("last_pric")),
        "prevClose": _positive(get("pd_close_pric")),
        # 대비는 값에 부호가 붙어 옵니다. 부호 칸(`chg_sign`)은 따로 쓰지 않습니다.
        "change": market_values.number(get("chg")),
        "changePct": market_values.number(get("chg_rt")),
        "open": _positive(get("open_pric")),
        "high": _positive(get("high_pric")),
        "low": _positive(get("low_pric")),
        "bid": _positive(get("bid_pric")),
        "ask": _positive(get("ask_pric")),
        "openInterest": market_values.count(get("opint")),
        "openInterestChange": market_values.count(get("opint_chg")),
        "volume": market_values.count(get("trd_qty")),
        "theory": _positive(get("theo_pric")),
        "theoryChange": market_values.number(get("theo_pric_chg")),
        "iv": market_values.number(get("vol")),
        "disparity": market_values.number(get("dev")),
        "disparityPct": market_values.number(get("dev_rt")),
        "delta": market_values.number(get("delta")),
        "gamma": market_values.number(get("gamma")),
        "theta": market_values.number(get("theta")),
        "vega": market_values.number(get("vega")),
        "rho": market_values.number(get("rho")),
    }


def _rows(raw: dict[str, Any]) -> list[dict[str, Any]]:
    rows = raw.get("Occurs1") if isinstance(raw, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _strike_text(row: dict[str, Any]) -> str:
    return str(row.get("occurs1_strk_pric") or "").strip()


def _positive(text: Any) -> float | None:
    """0 을 「없음」으로 보는 값."""
    value = market_values.number(text)
    return None if value is None or value == 0 else value


def _text(value: Any) -> str | None:
    return str(value or "").strip() or None
