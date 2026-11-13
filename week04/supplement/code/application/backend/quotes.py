"""시세 응답을 화면 모양으로 바꿀 때 **두 시장이 함께 쓰는 규칙.**

국내와 해외는 응답이 많이 다릅니다 — 칸 이름도, 오는 차례도, 한 번에 몇
종목이 오는지도 다릅니다. 그런데 **차트 봉과 종목 대조는 두 시장이 같습니다.**
칸 이름까지 같습니다(`occurs1_ref_dt`·`occurs1_exch_tm`·시가·고가·저가·종가·
거래량). 그래서 여기 한 자리에 두고 양쪽이 부릅니다.

두 벌로 두면 한쪽만 고쳐지는 날이 옵니다. 그날 차트는 **시장에 따라 다르게**
섭니다 — 봉이 뒤집히거나, 시각을 못 읽은 줄이 조용히 사라지거나, 종목 대조가
한쪽에서만 됩니다. 화면은 같은 차트 컴포넌트로 둘을 그리므로 그 차이를
알아채기 어렵습니다.

담긴 규칙은 셋입니다.

**봉은 오래된 것부터 냅니다.** NH선물은 최신부터 보내는데 차트는 반대입니다.

**시각을 못 읽은 줄은 빼고, 몇 줄을 뺐는지 알립니다.** 조용히 사라지면 차트에
구멍이 난 것을 알 방법이 없습니다.

**모든 응답이 종목코드를 함께 냅니다.** 화면은 한 종목의 현재가·호가·봉을 따로
부르고, 답이 오는 차례는 부른 차례와 다를 수 있습니다. 종목을 옮긴 직후에 앞
종목의 답이 늦게 닿으면 **화면에 두 종목의 값이 섞입니다.** 답마다 종목이
적혀 있으면 화면이 대조해서 버릴 수 있습니다.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from backend import market_values
from backend.sessions.hours import KST

logger = logging.getLogger(__name__)


def code_of(raw: dict[str, Any], field: str, asked: str) -> str:
    """이 응답이 어느 종목의 것인지.

    **원문 칸이 이깁니다.** 값이 있으면 부를 때 쓴 코드와 달라도 원문을 씁니다 —
    다르다는 것 자체가 화면이 알아야 할 사실입니다. 원문 칸이 비어 올 때만
    부를 때 쓴 코드를 적습니다. 양쪽 다 앞뒤 공백은 털어 냅니다.

    **해외 차트는 이 칸이 실제로 비어 옵니다.** 해외 차트 응답은 `sym` 을 빈
    값으로 주므로, 거기서는 부를 때 쓴 코드가 실제로 쓰입니다.
    """
    return str(raw.get(field) or "").strip() or str(asked or "").strip()


def timestamp(date_text: Any, time_text: Any = None) -> int | None:
    """`YYYYMMDD` 와 `HHMMSS` 를 밀리초로. 날짜가 아니면 `None`.

    **한국시간으로 읽습니다.** 봉의 날짜와 시각은 NH선물이 한국시간으로 주고
    (해외 분봉도 그렇습니다 — `overseas_quotes.py` 첫머리), 이 컴퓨터의 시간대가
    무엇이든 같은 순간이어야 실시간 체결(`realtime/feeds.py`)과 같은 봉에 앉습니다.
    세션 판정과 같은 시계입니다(`sessions/hours.py`).
    """
    date_body = str(date_text or "").strip()
    if len(date_body) != 8 or not date_body.isdigit():
        return None

    time_body = str(time_text or "000000").strip() or "000000"
    if len(time_body) != 6 or not time_body.isdigit():
        return None

    try:
        moment = datetime.strptime(date_body + time_body, "%Y%m%d%H%M%S").replace(tzinfo=KST)
    except ValueError:
        return None
    return int(moment.timestamp() * 1000)


def bars(raw: dict[str, Any], *, minute: bool, asked: str = "") -> dict[str, Any]:
    """차트 응답을 봉 목록으로. 오래된 것부터 나옵니다.

    시각을 읽지 못한 줄은 뺍니다. 몇 줄을 뺐는지 `skipped` 로 알립니다 —
    조용히 사라지면 차트에 구멍이 난 것을 알 방법이 없습니다.

    `asked` 는 부를 때 쓴 종목코드입니다. 원문에 종목 칸이 비어 올 때 씁니다.

    **해외 응답에는 날짜 칸이 둘입니다.** 기준일(`occurs1_ref_dt`)과
    해외영업일(`occurs1_glb_biz_dt`)인데, 자정 봉에서 두 값이
    하루 다릅니다. 해외영업일은 **장 세션의 날짜**라 가로축에 쓰면 봉이 하루씩
    밀립니다. 그래서 국내와 같이 기준일을 씁니다.
    """
    rows = raw.get("Occurs1")
    rows = rows if isinstance(rows, list) else []

    made: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        if not isinstance(row, dict):
            skipped += 1
            continue
        stamp = timestamp(
            row.get("occurs1_ref_dt"),
            row.get("occurs1_exch_tm") if minute else None,
        )
        if stamp is None:
            skipped += 1
            continue
        made.append(
            {
                "timestamp": stamp,
                "open": market_values.number(row.get("occurs1_open_pric")),
                "high": market_values.number(row.get("occurs1_high_pric")),
                "low": market_values.number(row.get("occurs1_low_pric")),
                "close": market_values.number(row.get("occurs1_close_pric")),
                "volume": market_values.count(row.get("occurs1_cum_trd_qty")) or 0,
            }
        )

    made.sort(key=lambda bar: bar["timestamp"])
    if skipped:
        logger.warning("차트에서 시각을 읽지 못한 줄 %d 개를 뺐습니다.", skipped)

    return {
        "code": code_of(raw, "sym", asked),
        "bars": made,
        "skipped": skipped,
        # 이어 받을 때 쓰는 값입니다. 지금 화면은 연속조회를 하지 않습니다.
        "nextKey": str(raw.get("nxt_key") or "").strip(),
    }


def merge_bars(pieces: list[dict[str, Any]], *, sessions: list[str]) -> dict[str, Any]:
    """봉 목록 여럿을 시각순 한 벌로. 주간과 야간을 한 차트에 이어 그릴 때 씁니다.

    봉의 시각이 달력 날짜 기준이라 정렬만으로 순서가 맞습니다. 같은 시각의 봉이
    두 벌에 다 있으면 **뒤엣것**이 남습니다 — 세션 경계의 한 봉이 두 조회에 함께
    실려 오는 경우입니다. 이어 받기 키는 한 벌로 낼 수 없어 빈 값입니다.
    `sessions` 는 무엇을 합쳤는지 화면에 알리는 표시이고, 하나뿐이면 그 벌 그대로에
    표시만 붙입니다.
    """
    if len(pieces) == 1:
        return {**pieces[0], "sessions": list(sessions)}
    by_time: dict[int, dict[str, Any]] = {}
    for piece in pieces:
        for bar in piece.get("bars") or []:
            by_time[int(bar["timestamp"])] = bar
    code = next((str(p.get("code") or "") for p in pieces if p.get("code")), "")
    return {
        "code": code,
        "bars": [by_time[stamp] for stamp in sorted(by_time)],
        "skipped": sum(int(p.get("skipped") or 0) for p in pieces),
        "nextKey": "",
        "sessions": list(sessions),
    }
