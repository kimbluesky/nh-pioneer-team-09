"""계좌 전체 손실 한도.

    ~/NHFutures/state/strategy-limits.json

전략 하나가 아니라 **계좌가 하루에 잃어도 되는 금액**입니다. 전략별 한도가 그
실행 하나를 멈추는 것과 달리, 이 한도에 닿으면 그 시장의 전략이 **다 멈춥니다**.
한 전략이 낸 손실이 아니라 계좌가 진 손실이기 때문입니다.

## 통화마다 따로입니다

한도는 `{통화: {dailyLoss}}` 입니다(`KRW`·`USD`·`HKD`…). 국내는 원이고, 해외는
품목마다 통화가 다릅니다. 통화가 다른 손익을 더하면 뜻이 없는 수가 나오므로
한도도 통화마다 하나씩 둡니다 — 환율로 맞춰 하나로 세지 않습니다.

시장 이름 열쇠(`domestic`·`overseas`)도 받습니다. 파일·요청의 그 두 열쇠는 원·달러
한도로 읽고, 답에도 두 이름을 그 통화의 값으로 함께 싣습니다.

`null` 은 **안 건 것**입니다. 0 이 아닙니다 — 0 은 「한 푼도 잃으면 안 된다」라
첫 수수료에 멈추게 됩니다.

## 실전 계좌는 따로입니다

실전 계좌 실행(`real`)의 손실은 실전 계좌의 것이라, 모의 계좌·앱 시뮬레이션과
**한도도 합계도 따로** 둡니다(`REAL`). 파일의 `limits` 는 모의 쪽 한도이고, 실전
한도는 그 옆 `real` 에 둡니다 — `real` 칸이 없는 파일은 그대로 모의 한도로 읽히고,
실전 한도는 비어 시작합니다.

## 설정 폴더가 아니라 상태 폴더입니다

한도는 새도 곤란할 것이 없는 값이고, `config/` 는 「누구에게도 보내지 마세요」가
한 줄로 서야 하는 자리입니다(`backend/storage.py` 첫머리).
"""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Any

from backend import market_values
from backend.nh_api.session import current_mode
from backend.storage import BROKEN, ORIGIN, ReadState, from_other_app, read_json, write_json
from backend.strategy.store import run_state_dir
from backend.strategy.validate import ERROR

logger = logging.getLogger(__name__)

#: 한도 파일. 실행과 같은 상태 폴더입니다(`store.run_state_dir`) — 한도는
#: 돌고 있는 것을 멈추는 값이라 실행과 같은 자리에서 갈립니다.
LIMITS_PATH = run_state_dir() / "strategy-limits.json"

#: 파일 형식. 담는 것이 달라지면 올립니다. 3 부터 통화마다입니다.
VERSION = 3

#: 한도를 따로 두는 계좌. `PAPER` 는 앱 시뮬레이션과 모의 계좌 실행의 한도이고,
#: `REAL` 은 실전 계좌 실행의 한도입니다.
PAPER = "paper"
REAL = "real"
ENVIRONMENTS = (PAPER, REAL)

#: 시장 이름 열쇠 → 그 통화. 읽기·답·요청에서 이 짝으로 옮깁니다.
LEGACY: dict[str, str] = {"domestic": "KRW", "overseas": "USD"}

#: 시장 이름 열쇠들. 답에 두 이름을 함께 싣습니다.
MARKETS = tuple(LEGACY)

#: 늘 칸이 있는 통화. 걸지 않았어도 `null` 로 답합니다.
BASE_CURRENCIES = ("KRW", "USD")

#: 통화마다의 칸. 지금은 하나뿐입니다.
DAILY_LOSS = "dailyLoss"

#: 통화 코드 모양. 세 글자 영문 대문자입니다(ISO 4217).
_CURRENCY = re.compile(r"^[A-Z]{3}$")

#: 못 받는 값에 하는 말. `validate.py` 와 같은 규약으로 냅니다.
WHY_LOSS = "하루 손실 한도는 0보다 큰 수로 적거나 비워 둡니다"
WHY_MARKET = "모르는 통화입니다"
WHY_TWICE = "같은 통화의 한도를 두 번 보냈습니다"


def blank() -> dict[str, dict[str, Any]]:
    """아무것도 안 건 한도. **`None` 이 「안 걸었다」입니다.**"""
    return {currency: {DAILY_LOSS: None} for currency in BASE_CURRENCIES}


def currency_of_key(key: Any) -> str | None:
    """요청·파일의 열쇠 하나 → 통화. 시장 이름 열쇠도 받습니다. 아니면 `None`."""
    body = str(key or "").strip()
    made = LEGACY.get(body, body)
    return made if _CURRENCY.match(made) else None


def problems(values: Any) -> list[dict[str, str]]:
    """받을 수 없는 값들. 비어 있으면 담을 수 있습니다.

    같은 통화를 두 번 보내면(예: `domestic` 과 `KRW`) 어느 쪽이 맞는지 고를 수
    없어 받지 않습니다.
    """
    found: list[dict[str, str]] = []
    if not isinstance(values, dict):
        return [{"field": "limits", "severity": ERROR, "message": WHY_MARKET}]
    seen: set[str] = set()
    for key, row in values.items():
        currency = currency_of_key(key)
        if currency is None:
            found.append({"field": str(key), "severity": ERROR, "message": WHY_MARKET})
            continue
        if currency in seen:
            found.append({"field": str(key), "severity": ERROR, "message": WHY_TWICE})
            continue
        seen.add(currency)
        if not isinstance(row, dict):
            found.append({"field": str(key), "severity": ERROR, "message": WHY_LOSS})
            continue
        value = row.get(DAILY_LOSS)
        if value is None:
            continue
        number = _number(value)
        if number is None or number <= 0:
            found.append(
                {"field": f"{key}.{DAILY_LOSS}", "severity": ERROR, "message": WHY_LOSS}
            )
    return found


def with_legacy(rows: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """통화 칸에 시장 이름 열쇠 둘(`domestic` = 원, `overseas` = 달러)을 더한 답."""
    made = {currency: dict(row) for currency, row in rows.items()}
    for market, currency in LEGACY.items():
        made[market] = dict(rows.get(currency) or {DAILY_LOSS: None})
    return made


def daily_losses(values: dict[str, dict[str, Any]]) -> dict[str, float]:
    """`{통화: 한도}` — **안 건 통화는 빠집니다.** 판정이 바로 받는 모양입니다.

    시장 이름 열쇠 둘은 같은 통화 칸이 있으면 그 칸을 따릅니다(답에 함께 실리는 값).
    """
    made: dict[str, float] = {}
    for key, row in values.items():
        currency = currency_of_key(key)
        if currency is None or (key in LEGACY and currency in values):
            continue
        number = _number((row or {}).get(DAILY_LOSS) if isinstance(row, dict) else None)
        if number is not None and number > 0:
            made[currency] = number
    return made


class LimitStore:
    """한도 파일 하나."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else LIMITS_PATH
        self._lock = threading.Lock()
        #: 마지막으로 읽었을 때 어땠는지. 화면이 「못 읽어 옆에 두었습니다」를
        #: 말할 수 있게 답에 실립니다(`GET /api/strategy/limits`).
        self.state = ReadState()
        #: 읽어 둔 값(계좌 → 시장 → 칸). **파일은 한 번만 읽습니다** — 한도는 이
        #: 앱이 아니면 아무도 안 고치는 값인데, 판정은 초마다 봅니다. 밖에서
        #: 파일을 고쳐도 다시 읽지 않습니다(앱을 껐다 켜면 반영됩니다).
        self._held: dict[str, dict[str, dict[str, Any]]] | None = None
        #: 파일에서 읽은 실전 한도. `_load` 가 모의 한도와 함께 채웁니다.
        self._loaded_real: dict[str, dict[str, Any]] = blank()

    def read(self, environment: str = PAPER) -> dict[str, dict[str, Any]]:
        """그 계좌의 담긴 한도(통화마다, 시장 이름 열쇠 둘 포함). 못 읽으면 **안 건 것**입니다."""
        held = self._all()
        return with_legacy(held.get(environment, blank()))

    def _all(self) -> dict[str, dict[str, dict[str, Any]]]:
        with self._lock:
            if self._held is not None:
                return self._held
        paper = self._load()
        made = {PAPER: paper, REAL: self._loaded_real}
        with self._lock:
            self._held = made
        return made

    def _load(self) -> dict[str, dict[str, Any]]:
        """담긴 한도를 읽습니다. **못 읽으면 아무것도 안 건 것으로 답합니다.**

        `read_json` 은 깨진 파일을 예외로 올리지 않고 옆으로 치운 뒤 빈 내용으로
        답합니다. 그대로 두면 **걸어 둔 한도가 말없이 풀립니다** — 사용자는
        한도를 걸어 두었다고 믿는데 아무것도 안 막습니다. 그래서 남깁니다.
        """
        held, status = read_json(self.path, state=self.state)
        if status == BROKEN:
            logger.warning(
                "손실 한도 파일을 읽지 못했습니다(%s): %s. "
                "걸어 둔 한도가 풀립니다 — 다시 걸어 주세요.",
                self.state.kept(),
                self.path,
            )
        if from_other_app(held, current_mode(), state=self.state):
            logger.warning(
                "손실 한도 파일을 다른 앱(%s)이 남겼습니다: %s. 담긴 한도를 쓰지 "
                "않습니다 — 다시 걸어 주세요.",
                self.state.other_app,
                self.path,
            )
            self._loaded_real = blank()
            return blank()
        # 실전 한도는 옆 칸입니다. 그 칸이 없는 파일이면 비어 시작합니다.
        self._loaded_real = _rows_of(held.get(REAL) if isinstance(held, dict) else None)
        return _rows_of(held.get("limits") if isinstance(held, dict) else None)

    def save(self, values: dict[str, Any], environment: str = PAPER) -> dict[str, dict[str, Any]]:
        """그 계좌의 한도를 담고 담긴 모양을 냅니다. 다른 계좌의 한도는 그대로 둡니다.

        **시장 이름 열쇠 둘만 보낸 요청은 원·달러 칸만** 바꿉니다 — 그 화면은 다른
        통화를 모르므로, 그 통화의 한도를 풀지 않습니다. 통화 이름이 하나라도 있으면
        보낸 것이 그 계좌의 전부입니다(안 보낸 통화는 안 건 것). 모르는 열쇠는 안 담습니다.

        Raises:
            OSError: 파일을 쓰지 못한 경우.
        """
        before = self._all().get(environment, blank())
        legacy_only = all(key in LEGACY for key in values)
        made = (
            {currency: dict(row) for currency, row in before.items()} if legacy_only else blank()
        )
        for currency in BASE_CURRENCIES:
            made.setdefault(currency, {DAILY_LOSS: None})
        if legacy_only:
            for currency in LEGACY.values():
                made[currency] = {DAILY_LOSS: None}
        for key, row in values.items():
            currency = currency_of_key(key)
            if currency is None:
                continue
            number = _number((row or {}).get(DAILY_LOSS) if isinstance(row, dict) else None)
            made[currency] = {DAILY_LOSS: number if number is not None and number > 0 else None}
        held = {name: {currency: dict(row) for currency, row in rows.items()} for name, rows in self._all().items()}
        held[environment] = made
        with self._lock:
            write_json(
                self.path,
                # 어느 앱이 썼는지. 다른 앱은 이 파일을 안 읽습니다(`storage.from_other_app`).
                {"version": VERSION, ORIGIN: current_mode(), "limits": held[PAPER], REAL: held[REAL]},
            )
            self._held = held
        # 다시 걸었으면 「풀렸다」는 알림은 할 일을 다 한 것입니다.
        self.state.wrote()
        return with_legacy(made)


def _rows_of(rows: Any) -> dict[str, dict[str, Any]]:
    """파일의 한 계좌 칸을 읽습니다. 모양이 아니면 아무것도 안 건 것입니다.

    파일의 `domestic`·`overseas` 열쇠는 원·달러 칸으로 읽습니다.
    """
    made = blank()
    if not isinstance(rows, dict):
        return made
    for key, row in rows.items():
        currency = currency_of_key(key)
        if currency is None:
            continue
        number = _number((row or {}).get(DAILY_LOSS) if isinstance(row, dict) else None)
        if number is not None and number > 0:
            made[currency] = {DAILY_LOSS: number}
        else:
            made.setdefault(currency, {DAILY_LOSS: None})
    return made


def environment_of(run: Any) -> str:
    """그 실행의 손실이 어느 계좌 한도에 드는지. 실전 계좌 실행만 `REAL` 입니다."""
    return REAL if getattr(run, "mode", "") == REAL else PAPER


def _number(value: Any) -> float | None:
    # 유한한 보통 십진수만 — `nan`·`inf`·지수는 숫자가 아닌 것과 같은 갈래입니다.
    return market_values.plain_number(value)


#: 앱이 쓰는 한도. 테스트는 제 것을 만들어 씁니다.
store = LimitStore()
