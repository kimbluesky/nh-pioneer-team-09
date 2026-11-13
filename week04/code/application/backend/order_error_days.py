"""주문오류내역을 **여러 날에 걸쳐** 묻는 자리. 두 시장 라우트가 함께 씁니다.

NH선물의 주문오류내역은 조회일자 하루만 받습니다. 기간으로 보고 싶으면 그 안의
날을 **하나씩 차례로** 불러 합칩니다 — 최신 날부터, 날마다 다음 쪽이 없을 때까지.
호출 간격은 호출부가 지키므로 여기서 기다리지 않습니다.

**주말·휴일도 건너뛰지 않습니다.** 휴장 달력이 없고, 해외는 한국시간 토요일
새벽에도 체결이 있습니다. 주문이 없던 날은 0줄로 지나갑니다.

**한 날이 실패해도 나머지 날은 냅니다.** 그 날은 `failedDays` 에 까닭과 함께
실립니다. 모든 날이 실패하면 하루 조회와 같은 실패로 답합니다. 모의투자에서
이 조회를 부를 수 없다는 답을 받으면 그 자리에서 끝냅니다 — 다른 날도 같습니다.

**저장해 둔 응답으로 도는 중이면** 날짜만 다른 요청에 같은 기록이 답합니다. 그대로
합치면 같은 줄이 날마다 되풀이되므로, 그 날의 요청과 똑같은 기록으로 답한 날만
씁니다. 나머지 날의 줄은 버리고 그 날 수를 `replayDroppedDays` 로 알립니다.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from fastapi import HTTPException, Response

from backend.nh_api import error_codes
from backend.nh_api.errors import ERROR_KIND_HEADER
from backend.nh_api.replay import (
    MATCH_API,
    MATCH_CROSS_ENVIRONMENT,
    MATCH_EMPTY,
    MATCH_EXACT,
    MATCH_HEADER,
    MATCH_SYNTHETIC,
    last_replay,
)

#: 한 번에 물을 수 있는 날 수(양끝 포함). 날마다 한 번 이상 부르므로 길수록 오래 걸립니다.
MAX_DAYS = 31

#: 한 번의 기간 조회에서 부르는 쪽 수의 상한. 넘으면 거기서 멈추고 `truncated` 로 알립니다.
MAX_PAGES = 100

HALF_PERIOD = "기간은 시작일과 끝날을 함께 적어 주세요"
WITH_SINGLE_DAY = "기간으로 물을 때는 조회일자와 다음 쪽 키를 비워 두세요"
REVERSED = "시작일이 끝날보다 뒤입니다"
TOO_LONG = f"기간은 {MAX_DAYS}일까지 물을 수 있습니다"
FUTURE = "시작일이 오늘보다 뒤입니다"

#: 재생 답의 가까운 차례. 뒤로 갈수록 요청과 멉니다.
_MATCH_ORDER = (MATCH_EXACT, MATCH_API, MATCH_CROSS_ENVIRONMENT, MATCH_SYNTHETIC)


def period_problem(inq_dt: str, nxt_key: str, first: str, last: str) -> str | None:
    """기간 인자의 모양이 틀렸으면 그 까닭(한글). 날짜 자체의 검사는 부르는 쪽이 합니다."""
    if not first and not last:
        return None
    if not first or not last:
        return HALF_PERIOD
    if inq_dt or nxt_key:
        return WITH_SINGLE_DAY
    if first > last:
        return REVERSED
    return None


def resolve(first: str, last: str, today: date) -> tuple[date, date]:
    """물을 기간 (시작, 끝). **끝날이 오늘보다 뒤면 오늘로 자릅니다.**

    Raises:
        ValueError: 시작일이 오늘보다 뒤이거나, 자른 기간이 `MAX_DAYS` 를 넘는 경우.
    """
    start = date(int(first[:4]), int(first[4:6]), int(first[6:]))
    end = min(date(int(last[:4]), int(last[4:6]), int(last[6:])), today)
    if start > today:
        raise ValueError(FUTURE)
    if (end - start).days + 1 > MAX_DAYS:
        raise ValueError(TOO_LONG)
    return start, end


@dataclass
class Collected:
    """날마다 받은 것을 모은 결과."""

    #: (물은 날, 화면 모양 한 줄)
    rows: list[tuple[date, dict[str, Any]]] = field(default_factory=list)
    #: 부른 날 수(실패한 날 포함).
    days: int = 0
    failed: list[tuple[date, HTTPException]] = field(default_factory=list)
    truncated: bool = False
    #: 재생에서 요청과 다른 기록으로 답해 줄을 버린 날 수.
    dropped: int = 0
    #: 재생으로 답했으면 날들 가운데 가장 먼 답의 표시. 실서버면 빈 글자.
    match: str = ""


def collect(
    first: date,
    last: date,
    fetch: Callable[[date, str], dict[str, Any]],
    *,
    shape: Callable[[dict[str, Any]], list[dict[str, Any]]],
    more: Callable[[dict[str, Any]], bool],
) -> Collected:
    """`last` 부터 `first` 까지 하루씩 부릅니다. `fetch(날, 다음 쪽 키)` 는 원문 한 쪽이고,
    실패는 라우트의 `HTTPException` 으로 올라옵니다.

    한 날 안에서 다음 쪽이 없다고 보는 때는 `more` 가 거짓이거나, 다음 키가 비었거나,
    이미 쓴 키가 다시 오거나, 받은 쪽에 줄이 없거나 바로 앞 쪽과 줄이 같을 때입니다.

    Raises:
        HTTPException: 모의투자에서 부를 수 없는 조회인 경우, 또는 모든 날이 실패한 경우.
    """
    made = Collected()
    pages = 0
    matches: list[str] = []
    day = last
    while day >= first:
        if pages >= MAX_PAGES:
            made.truncated = True
            break
        made.days += 1
        mine: list[dict[str, Any]] = []
        loose = False
        try:
            key = ""
            used: set[str] = set()
            previous: list[dict[str, Any]] | None = None
            while True:
                if pages >= MAX_PAGES:
                    made.truncated = True
                    break
                raw = fetch(day, key)
                pages += 1
                note = last_replay.get()
                if note:
                    matches.append(str(note.get("match") or ""))
                    if note.get("match") != MATCH_EXACT:
                        loose = True
                page = shape(raw)
                if previous is not None and (not page or page == previous):
                    break
                mine.extend(page)
                previous = page
                following = str(raw.get("nxt_key") or "").strip()
                if not more(raw) or not following or following in used:
                    break
                used.add(following)
                key = following
        except HTTPException as exc:
            if (exc.headers or {}).get(ERROR_KIND_HEADER) == error_codes.PAPER_UNAVAILABLE:
                raise
            made.failed.append((day, exc))
            day -= timedelta(days=1)
            continue
        if loose:
            made.dropped += 1
        else:
            made.rows.extend((day, one) for one in mine)
        if made.truncated:
            break
        day -= timedelta(days=1)

    if made.failed and len(made.failed) == made.days:
        raise made.failed[-1][1]
    made.match = _weakest(matches)
    return made


def _weakest(matches: list[str]) -> str:
    if not matches:
        return ""
    known = [m for m in matches if m in _MATCH_ORDER]
    if known:
        return max(known, key=_MATCH_ORDER.index)
    return MATCH_EMPTY if all(m == MATCH_EMPTY for m in matches) else matches[-1]


def answer(
    made: Collected,
    first: date,
    last: date,
    response: Response | None,
    *,
    date_of: Callable[[dict[str, Any], date], str],
    key_of: Callable[[dict[str, Any]], tuple[str, str] | None] = lambda one: None,
) -> dict[str, Any]:
    """모은 것을 기간 답으로. 줄마다 `date` 를 채우고 최신부터 늘어놓습니다.

    `key_of` 가 열쇠를 주는 줄은 한 번만 남깁니다 — 같은 줄이 두 날의 조회에 함께
    걸려 올 수 있어서입니다. 열쇠가 없는 줄은 거르지 않습니다.
    """
    seen: set[tuple[str, str]] = set()
    errors: list[dict[str, Any]] = []
    for day, one in made.rows:
        key = key_of(one)
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        errors.append({**one, "date": date_of(one, day)})
    # 최신 먼저. 같은 때이면 받은 차례를 그대로 둡니다(안정 정렬).
    errors.sort(key=lambda one: (one.get("date") or "", one.get("time") or ""), reverse=True)

    body: dict[str, Any] = {
        "errors": errors,
        "period": {"from": first.isoformat(), "to": last.isoformat()},
        "days": made.days,
        "failedDays": [
            {"date": day.isoformat(), "reason": str(exc.detail)} for day, exc in made.failed
        ],
        "truncated": made.truncated,
        "nextKey": "",
    }
    if made.match:
        body["replayDroppedDays"] = made.dropped
        if response is not None:
            response.headers[MATCH_HEADER] = made.match
    return body
