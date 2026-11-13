"""날짜별 예수금 스냅샷 — 자산·손익 추이의 재료.

## 왜 쌓아 두나

**NH선물에는 날짜별 줄을 주는 계좌 API 가 없습니다.** 예수금은 하루치만 오고
(`biz_dt` 한 칸), 기간을 받는 것은 해외 누적손익 하나뿐인데 그마저 줄이
품목별이라 날짜가 없습니다. 그래서 추이를 그리려면 **하루치를 날마다 받아
이 앱이 이어 붙이는** 수밖에 없습니다.

받은 날을 쌓아 두면 다음부터는 **빠진 날만** 물으면 됩니다. 한 달이 스물두
번이고 계좌 조회는 초에 열 번이라, 안 쌓아 두면 화면을 열 때마다 그 값을
다시 다 치릅니다.

## 무엇을 쌓나

그날의 **화면용 예수금 한 줄** 그대로입니다(`domestic_account.deposit`·
`overseas_account.deposit` 이 낸 답). 원문을 쌓지 않는 것은, 화면이 읽는 이름과
저장한 이름이 갈리면 추이와 오늘 값이 서로 다른 칸을 그리기 때문입니다.

**언제 받은 값인지**(`fetchedAt`)를 함께 적습니다. 오늘 값은 장중 내내 바뀌어서
「방금 받은 것」과 「아침에 받아 둔 것」이 다릅니다. **「확정된 값인지」는 안
적습니다** — 그것을 아는 길이 없습니다.

**받았는데 값이 없던 날도 적습니다**(`empty`). 휴장일 달력이 없어 물어 봐야만
아는데, 안 적어 두면 그 날을 화면이 갈 때마다 영영 다시 묻습니다.

## 어디에 쌓나

환경과 시장을 **열쇠에 함께** 둡니다 — 모의와 실전 값이 한 그래프에 섞이면
안 됩니다. 저장해 둔 응답으로 도는 앱은 **폴더가 다릅니다**(`state-replay/`,
`strategy/store.py` 와 같은 규칙).

**해외는 한 기준만 쌓습니다**(`BASIS` — 원화 환산 합계). 통화·환산 기준마다
쌓으면 빠진 날을 채우는 조회가 기준 수만큼 늘어납니다. 화면이 다른 기준을
고르면 그 값은 추이에 안 씁니다.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from backend import storage
from backend.nh_api.session import current_mode
from backend.sessions.hours import is_weekend, iso, now_kst
from backend.storage import BROKEN, ORIGIN, ReadState, from_other_app, read_json, write_json

logger = logging.getLogger(__name__)

STATE_DIR = storage.STATE_DIR
REPLAY_STATE_DIR = storage.REPLAY_STATE_DIR

#: 파일 형식. 담는 것이 달라지면 올립니다.
VERSION = 1

#: 해외 값을 쌓는 기준. **화면 머리의 「오늘 예탁자산」과 같은 기준**입니다.
#: 파일에 적어 두는 것은, 나중에 기준이 바뀌면 그 전에 쌓은 값이 무엇이었는지
#: 알 수 있어야 하기 때문입니다.
BASIS = "krw-converted"

#: 저장돼 있어도 **다시 물을 최근 영업일 수**(오늘 포함). 해외 예수금은 그날
#: 정산 전에 물으면 앞 영업일 값을 그날 날짜로 답할 때가 있어, 최근 날의 저장
#: 값은 나중에 다시 받아 덮어야 맞아집니다. 오늘 값은 장중 내내 바뀝니다.
RECENT_DAYS = 2

#: 열쇠 하나에 담을 날 수. 넘으면 **오래된 것부터** 버립니다 — 파일이 끝없이
#: 자라지 않게 하는 자리입니다. 영업일로 세 해쯤입니다.
MAX_DAYS = 800


def state_dir(env: Mapping[str, str] | None = None) -> Path:
    """쌓아 둔 값이 담기는 폴더. 재생은 따로 씁니다.

    저장해 둔 응답으로 도는 앱이 실서버에서 받은 예수금을 추이에 섞으면, 그
    그래프는 어느 서버의 것도 아닌 것이 됩니다.
    """
    return storage.state_dir(env)


def history_path(env: Mapping[str, str] | None = None) -> Path:
    return state_dir(env) / "deposit-history.json"


def as_day(text: str) -> date | None:
    """`YYYYMMDD` 를 날짜로. 여덟 자리 숫자가 아니면 `None` 입니다."""
    body = str(text or "").strip()
    if len(body) != 8 or not body.isdigit():
        return None
    try:
        return datetime.strptime(body, "%Y%m%d").date()
    except ValueError:
        return None


def as_text(day: date) -> str:
    return day.strftime("%Y%m%d")


def wanted_days(first: date, last: date) -> list[date]:
    """그 사이에 **물을 만한 날**. 주말은 빼 둡니다.

    휴장일은 못 가립니다 — 달력이 없습니다. 물어서 빈 답이 오면 그때 적어 두고
    (`empty`) 다시 묻지 않습니다.
    """
    made: list[date] = []
    day = first
    while day <= last:
        if not is_weekend(day):
            made.append(day)
        day += timedelta(days=1)
    return made


def recent_days(today: date, count: int = RECENT_DAYS) -> list[date]:
    """오늘(포함)부터 거슬러 **주말이 아닌 날** `count` 개, 오름차순.

    휴장일은 못 가립니다 — 달력이 없습니다. 토·일이면 금·목입니다.
    """
    made: list[date] = []
    day = today
    while len(made) < count:
        if not is_weekend(day):
            made.append(day)
        day -= timedelta(days=1)
    return sorted(made)


class DepositHistory:
    """날짜별 예수금 스냅샷 파일 하나."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else history_path()
        # 쌓는 자리와 읽는 자리가 다른 요청에서 동시에 옵니다.
        self._lock = threading.Lock()
        #: 마지막으로 읽었을 때 어땠는지. 화면이 「못 읽어 옆에 두었습니다」를
        #: 말할 수 있게 답에 실립니다.
        self.state = ReadState()

    # ── 쌓기 ────────────────────────────────────────────

    def keep(self, *, environment: str, market: str, day: str, row: dict[str, Any]) -> None:
        """그날의 줄을 쌓습니다. **못 쌓아도 예외를 내지 않습니다.**

        쌓기가 실패했다고 예수금 화면이 안 뜨면 안 됩니다 — 기록 파일과 같은
        결입니다. 못 쌓은 것은 기록에 남깁니다.

        **늘 덮어씁니다.** 오늘 값은 장중 내내 바뀌므로 나중에 받은 것이 더
        맞습니다. 언제 받았는지는 `fetchedAt` 이 말합니다.
        """
        self._put(environment=environment, market=market, day=day, row=row)

    def keep_empty(self, *, environment: str, market: str, day: str) -> None:
        """그날은 **받았는데 값이 없었다**고 적습니다.

        휴장일이 여기 담깁니다. 안 적어 두면 화면이 그 날을 갈 때마다 다시
        묻습니다 — 없는 것과 아직 안 받은 것은 다릅니다.

        **값이 쌓인 날은 덮지 않습니다.** 최근 날은 다시 묻는데(`RECENT_DAYS`),
        그때 빈 답이 와도 이미 받은 값이 사라지면 안 됩니다.
        """
        self._put(
            environment=environment, market=market, day=day, row={"empty": True}, keep_values=True
        )

    def _put(
        self,
        *,
        environment: str,
        market: str,
        day: str,
        row: dict[str, Any],
        keep_values: bool = False,
    ) -> None:
        key = _key(environment, market)
        if not key or as_day(day) is None:
            return
        made = dict(row)
        made["fetchedAt"] = iso(now_kst())
        try:
            with self._lock:
                body, status = read_json(self.path, state=self.state)
                if status == BROKEN:
                    logger.warning(
                        "예수금 기록을 읽지 못했습니다(%s): %s. 지난 값이 비어 보입니다.",
                        self.state.kept(),
                        self.path,
                    )
                days = _days(_ours(body, self.state, self.path))
                kept = dict(days.get(key) or {})
                found = kept.get(str(day))
                if keep_values and isinstance(found, dict) and not found.get("empty"):
                    return
                kept[str(day)] = made
                days[key] = _trimmed(kept)
                write_json(
                    self.path,
                    # 어느 앱이 썼는지. 다른 앱은 이 파일을 안 읽습니다
                    # (`storage.from_other_app`).
                    {"version": VERSION, ORIGIN: current_mode(), "basis": BASIS, "days": days},
                )
                self.state.wrote()
        except OSError as exc:
            logger.warning("예수금 기록을 쌓지 못했습니다: %s", exc)

    # ── 읽기 ────────────────────────────────────────────

    def read(self, *, environment: str, market: str, first: date, last: date) -> dict[str, Any]:
        """그 기간의 줄들과 **아직 안 받은 날들**, **다시 받을 날들**.

        `days` 에는 값이 있는 날만 담깁니다 — 빈 날(`empty`)은 그릴 점이
        없습니다. `missing` 은 **물어야 할 날**이라 주말과 빈 날을 뺍니다.
        `refresh` 는 기간 안의 최근 영업일(`RECENT_DAYS`)로, 쌓여 있어도 다시
        받아 덮을 날입니다 — 그 날의 쌓인 값은 다시 받을 때까지 `days` 에
        그대로 있습니다. 기간이 다 지난 날이면 빈 목록입니다.
        """
        recent = recent_days(now_kst().date())
        with self._lock:
            body, status = read_json(self.path, state=self.state)
        if status == BROKEN:
            logger.warning(
                "예수금 기록을 읽지 못했습니다(%s): %s. 지난 값이 비어 보입니다.",
                self.state.kept(),
                self.path,
            )
        kept = _days(_ours(body, self.state, self.path)).get(_key(environment, market)) or {}
        days: list[dict[str, Any]] = []
        missing: list[str] = []
        for day in wanted_days(first, last):
            text = as_text(day)
            found = kept.get(text)
            if not isinstance(found, dict):
                missing.append(text)
                continue
            if found.get("empty"):
                continue
            days.append({**found, "day": text})
        refresh = [as_text(day) for day in recent if first <= day <= last]
        return {"days": days, "missing": missing, "refresh": refresh}


def _ours(body: Any, state: ReadState, path: Path) -> dict[str, Any]:
    """다른 앱이 쓴 파일이면 **빈 것으로** 봅니다(`storage.from_other_app`).

    저장본으로 도는 앱이 쌓은 잔고를 실제 서버 쪽 추이에 이어 붙이면, 그래프는
    있었던 적 없는 흐름을 그립니다. 그 파일은 다음에 이 앱이 쌓을 때 이 앱의 것으로
    덮입니다 — 저쪽 앱의 값은 저쪽 폴더에 따로 있습니다.
    """
    if not from_other_app(body, current_mode(), state=state):
        return body if isinstance(body, dict) else {}
    logger.warning(
        "예수금 기록을 다른 앱(%s)이 남겼습니다: %s. 담긴 값을 쓰지 않습니다.",
        state.other_app,
        path,
    )
    return {}


#: 기간을 거꾸로 주거나 날짜 모양이 아닐 때 화면에 적는 말.
BAD_PERIOD = "조회 기간을 확인해 주세요."


def answer(first: str, last: str, *, market: str, environment: str) -> dict[str, Any] | None:
    """기간 조회의 답 한 벌. **두 시장이 같은 모양으로 냅니다.**

    기간이 날짜가 아니거나 거꾸로면 `None` 입니다 — 무엇으로 답할지(422 문구)는
    부르는 라우트가 정합니다.
    """
    start, end = as_day(first), as_day(last)
    if start is None or end is None or start > end:
        return None
    made = store.read(environment=environment, market=market, first=start, last=end)
    return {**made, "store": store.state.payload}


def is_empty(row: Any) -> bool:
    """값이 하나도 없는 줄인지. **휴장일이 이렇게 옵니다**(확인 못 함).

    숫자가 한 톨도 없으면 빈 것으로 봅니다. 휴장일에 0 으로 채워 오면 그것은
    「0 이라는 값」이라 그대로 쌓습니다 — 둘을 가릴 길이 없습니다.

    **있는지만 봅니다.** 값이 0 인 것과 값이 없는 것은 다릅니다 — 참·거짓으로
    세면 잔고 0 인 날이 통째로 빈 날이 됩니다.
    """
    return next(_numbers(row), None) is None


def _numbers(value: Any) -> Any:
    """그 줄 안의 숫자들. 이름·날짜 같은 글자는 안 셉니다."""
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        yield value
        return
    if isinstance(value, dict):
        for one in value.values():
            yield from _numbers(one)
        return
    if isinstance(value, (list, tuple)):
        for one in value:
            yield from _numbers(one)


def _key(environment: str, market: str) -> str:
    """환경과 시장을 한 열쇠로. 둘 중 하나라도 비면 쌓지 않습니다."""
    env = str(environment or "").strip()
    place = str(market or "").strip()
    return f"{env}/{place}" if env and place else ""


def _days(body: Any) -> dict[str, dict[str, Any]]:
    made = (body or {}).get("days") if isinstance(body, dict) else None
    if not isinstance(made, dict):
        return {}
    return {key: dict(value) for key, value in made.items() if isinstance(value, dict)}


def _trimmed(kept: dict[str, Any]) -> dict[str, Any]:
    """날이 너무 많으면 **오래된 것부터** 버립니다."""
    if len(kept) <= MAX_DAYS:
        return kept
    keys = sorted(kept)[-MAX_DAYS:]
    return {key: kept[key] for key in keys}


#: 앱이 쓰는 저장소. 테스트는 제 것을 만들어 씁니다.
store = DepositHistory()
