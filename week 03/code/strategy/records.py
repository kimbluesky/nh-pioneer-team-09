"""성과분석이 보는 기록 한 벌.

원천은 **기록 파일 하나**(`journal`)입니다. 실행 장부는 앱을 껐다 켜면 비어서
지난 기간을 말할 수 없고, 실행 파일(`runs._SAVED`)도 장부를 담지 않습니다.
기록 파일의 거래 줄은 실행을 지워도 남습니다.

## 한 답은 한 기록 유형·한 통화입니다

앱 시뮬레이션(`paper`)은 이 앱이 지어낸 체결이고 모의 계좌(`sim`)·실전
계좌(`real`)는 계좌가 채운 체결입니다. 셋을 한 성적으로 섞으면 어느 쪽 성적인지
말할 수 없어 유형마다 따로 답합니다. 국내(원)와 해외(달러)도 더하지 않습니다.

## 거래일은 거래가 **닫힌** 시각으로 셉니다

야간 세션은 다음 거래일에 속합니다(`bars.day_slot_of` 와 같은 경계). 기간은
그 거래일로 가르고 양 끝을 포함합니다.

## 곡선은 실현 기준입니다

기록 파일에는 평가손익의 이력이 없습니다. 그래서 곡선은 거래일마다 그날까지
닫힌 거래의 **순손익(실현 − 수수료) 누적**이고, 답의 `curveBasis` 가 그것을
말합니다(`CURVE_REALIZED`). 화면은 이 값으로 곡선 설명을 고릅니다.

## 상한

거래가 `MAX_TRADES` 를 넘으면 **최근 것부터** 그만큼만 남깁니다. 합계·곡선·
전략별 값은 **남은 거래**의 것이고, 잘렸으면 `truncated` 가 참이며
`earliestAt` 이 남은 것 중 가장 이른 종료 시각입니다.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any

from backend.sessions.hours import KST, iso, now_kst
from backend.strategy import bars as bar_lib

logger = logging.getLogger(__name__)

#: 기록 갈래 — 기록 줄의 `record` 칸 값입니다. 앱 시뮬레이션(종이)·모의 계좌·
#: 실전 계좌. 실행의 갈래(`runs.MODES`)와 같은 값입니다.
PAPER = "paper"
SIM = "sim"
REAL = "real"
RECORDS: tuple[str, ...] = (PAPER, SIM, REAL)

#: 갈래 → 화면의 「기록 유형」 글자. 목록 줄의 `record` 칸도 이 글자입니다.
KIND_LABELS: dict[str, str] = {
    PAPER: "앱 시뮬레이션",
    SIM: "모의 계좌 거래",
    REAL: "실전 계좌 거래",
}

#: 합계 줄의 이름. 가상 자금은 계좌 합계와 같은 말로 부르지 않습니다.
TOTAL_LABELS: dict[str, str] = {
    PAPER: "가상 자금 합계",
    SIM: "모의 계좌 합계",
    REAL: "실전 계좌 합계",
}

#: 시장과 그 통화.
CURRENCIES: dict[str, str] = {"domestic": "KRW", "overseas": "USD"}
# 해외 손익은 달러 그대로 둡니다. 원화로 환산하게 되면 환율의 출처와 시각을 함께
# 적고, 모르면 비웁니다 — 1.0 을 곱해 원화인 척하지 않습니다(`paper` 첫머리).

#: 곡선의 뜻 둘.
#:
#:     realized  거래일마다 그날까지 닫힌 거래의 순손익(실현 − 수수료) 누적.
#:               평가손익은 들지 않습니다.
#:     equity    거래일마다 그날 끝의 실현 − 수수료 + 평가손익(거래일 잔고 줄).
#:               기간 앞의 값을 빼 기간 안의 변화만 그립니다.
#:
#: 답 하나는 한 기준입니다 — 기간 안의 거래마다 그 실행·그 거래일의 잔고 줄이
#: 있어야 `equity` 이고, 하나라도 없으면(잔고 줄이 생기기 전의 기록) 기간 전체가
#: `realized` 입니다. 두 기준을 한 곡선에 이어 붙이면 이음새에서 값이 뜁니다.
CURVE_REALIZED = "realized"
CURVE_EQUITY = "equity"
#: 최대 낙폭의 기준 — 닫힌 거래의 실현 순손익을 청산 시각 차례로 더한 곡선입니다
#: (`trade_drawdown`). 곡선이 평가 기준일 때도 낙폭은 이 기준이라 답에 함께 싣습니다.
DRAWDOWN_BASIS = "trades-realized"

#: 주문 결과 — 엔진이 결과 줄에 적는 값(`engine.FILLED` 등)과 같습니다.
_FILLED_LIKE = ("filled", "partial")
_ENDED = ("filled", "partial", "cancelled", "rejected")
_REJECTED_LIKE = ("rejected", "refused")

#: 한 답에 싣는 거래 수의 상한.
MAX_TRADES = 5000

#: 기간을 주지 않았을 때의 길이(달력일, 오늘 포함). 목록의 「최근 30일」과 같습니다.
DEFAULT_DAYS = 30

#: 버전이 지워져 이름을 모르는 기록의 이름.
UNKNOWN_NAME = "알 수 없음"

# 요청이 틀렸을 때 답하는 말. 화면이 그대로 적습니다.
WHY_MARKET = "시장을 고르세요 — domestic 또는 overseas"
WHY_RECORD = "기록 갈래를 고르세요 — paper 또는 sim"
WHY_DATE = "날짜는 YYYY-MM-DD 로 적습니다"
WHY_RANGE = "시작 날짜가 끝 날짜보다 뒤입니다"

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class RecordsError(ValueError):
    """요청 모양이 틀린 경우입니다. 글자 하나가 화면에 적을 문장입니다."""


@dataclass(frozen=True)
class Query:
    """거르는 조건 한 벌."""

    record: str
    market: str
    start: date
    end: date
    versions: frozenset[str]
    code: str
    #: 해외 안의 통화 하나(`USD`·`HKD`…). 비우면 그 시장의 옛 통화(해외 달러)입니다.
    currency: str = ""

    @property
    def money(self) -> str:
        """이 답이 세는 통화. **한 답은 한 통화입니다** — 통화가 다른 손익을 더하지 않으므로,
        통화를 안 물은 해외 조회도 달러 줄만 셉니다(답의 `currency` 와 숫자가 맞게)."""
        return self.currency or CURRENCIES.get(self.market, "")


#: 모형 이름이 없던 때의 기록 줄이 쓴 모형. 갈래(`record`)로 고릅니다.
LEGACY_MODELS: dict[str, str] = {"paper": "paper-v1", "sim": "sim-v1", "real": "sim-v1"}


#: 통화 거름에 하는 말.
WHY_CURRENCY = "통화는 해외 기록에서 세 글자 통화 코드로 고릅니다"
_CURRENCY = re.compile(r"^[A-Z]{3}$")


def currency_of_row(row: dict[str, Any]) -> str:
    """기록 줄 하나의 통화. 칸이 없는 옛 줄은 그 시장의 옛 통화(국내 원·해외 달러)입니다."""
    written = str(row.get("currency") or "").strip().upper()
    return written or CURRENCIES.get(str(row.get("market") or ""), "")


def model_of(row: dict[str, Any]) -> str:
    """기록 줄 하나를 채운 모형. 이름이 없는 옛 줄은 그 갈래의 첫 모형입니다."""
    named = str(row.get("model") or "").strip()
    if named:
        return named
    return LEGACY_MODELS.get(str(row.get("record") or "paper"), "paper-v1")


def query_of(
    *,
    record: str | None,
    market: str | None,
    start: str | None,
    end: str | None,
    versions: str | None = None,
    code: str | None = None,
    today: date | None = None,
    currency: str | None = None,
) -> Query:
    """쿼리 글자들을 검사해 조건 한 벌로. 틀리면 `RecordsError` 입니다.

    기간을 비우면 **오늘까지 30일**(달력일, 양 끝 포함)입니다. 한쪽만 주면
    다른 쪽은 그 기준으로 채웁니다.
    """
    market = str(market or "").strip()
    if market not in CURRENCIES:
        raise RecordsError(WHY_MARKET)
    record = str(record or "").strip()
    if record not in RECORDS:
        raise RecordsError(WHY_RECORD)
    today = today or now_kst().date()
    last = _date_of(end) if end else today
    first = _date_of(start) if start else last - timedelta(days=DEFAULT_DAYS - 1)
    if first > last:
        raise RecordsError(WHY_RANGE)
    picked = frozenset(one.strip() for one in str(versions or "").split(",") if one.strip())
    wanted = str(currency or "").strip().upper()
    if wanted and (market != "overseas" or not _CURRENCY.match(wanted)):
        raise RecordsError(WHY_CURRENCY)
    return Query(record, market, first, last, picked, str(code or "").strip(), wanted)


def _date_of(text: str) -> date:
    body = str(text).strip()
    if not _DATE.match(body):
        raise RecordsError(WHY_DATE)
    try:
        return date.fromisoformat(body)
    except ValueError as exc:
        raise RecordsError(WHY_DATE) from exc


@lru_cache(maxsize=65536)
def trading_day(closed_at: int, session: str) -> date:
    """그 거래가 드는 거래일. **닫힌 시각**과 그 줄의 세션으로 셉니다.

    한 요청 안에서도 같은 거래를 여러 번 묻고(거름·축·곡선), 목록과 조회가
    같은 기록을 되풀이해 읽습니다. 값이 바뀌지 않는 셈이라 기억해 둡니다.
    """
    start = bar_lib.day_slot_of(int(closed_at), str(session or ""))
    return datetime.fromtimestamp(start / 1000, tz=KST).date()


def day_of_row(row: dict[str, Any], key: str = "closedAt") -> date:
    """기록 줄 하나의 거래일. 줄에 적힌 거래일(`tradingDay`)이 먼저입니다.

    해외 실행은 시간표의 거래일을 줄에 적습니다 — 한국 시각 자정을 넘기는 세션은
    시각만으로 거래일을 셀 수 없습니다. 칸이 없는 줄(국내·옛 줄)은 시각과 세션으로
    셉니다(`trading_day`).
    """
    written = str(row.get("tradingDay") or "").strip()
    if written:
        try:
            return date.fromisoformat(written)
        except ValueError:
            pass
    return trading_day(int(row[key]), str(row.get("session") or ""))


def trade_key(row: dict[str, Any]) -> str:
    """거래 하나를 가리는 열쇠. 겹친 줄을 거르고 거래 id 로도 씁니다.

    기록 줄에는 거래 id 가 없습니다. 한 실행의 거래는 차례로 닫히므로 진입·종료
    시각이 둘 다 같은 서로 다른 거래는 없습니다 — 방향과 수량까지 붙여 둡니다.
    """
    return "|".join(
        str(row.get(name) if row.get(name) is not None else "")
        for name in ("runId", "openedAt", "closedAt", "side", "quantity")
    )


def picked(rows: Iterable[dict[str, Any]], ask: Query) -> list[dict[str, Any]]:
    """조건에 드는 거래 줄 — 겹친 줄은 하나로, 종료 시각 차례로."""
    seen: set[str] = set()
    made: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("record") or PAPER) != ask.record:
            continue
        if str(row.get("market") or "") != ask.market:
            continue
        if ask.versions and str(row.get("versionId") or "") not in ask.versions:
            continue
        if ask.code and str(row.get("code") or "") != ask.code:
            continue
        if currency_of_row(row) != ask.money:
            continue
        closed = _int(row.get("closedAt"))
        if closed is None:
            continue
        if not ask.start <= day_of_row(row) <= ask.end:
            continue
        key = trade_key(row)
        if key in seen:
            continue
        seen.add(key)
        made.append(row)
    made.sort(key=lambda one: (_int(one.get("closedAt")) or 0, trade_key(one)))
    return made


def dataset(
    rows: Iterable[dict[str, Any]],
    ask: Query,
    *,
    versions: Iterable[dict[str, Any]] = (),
    complete: bool = True,
    open_pl: float | None = None,
    day_rows: Iterable[dict[str, Any]] = (),
    order_rows: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """화면의 `Dataset` 한 벌.

    `day_rows`·`order_rows` 는 기록 파일의 거래일 잔고 줄과 주문 결과 줄입니다.
    곡선을 평가 기준으로 세울 수 있는지(`curveBasis`)와 체결 품질(`quality`)을
    여기서 냅니다 — 그 줄이 없던 때의 기록이면 곡선은 실현 기준이고 품질은
    비어 있습니다.

    `versions` 는 전략 저장소의 버전 줄들로, 이름·버전 글자와 전략 묶음을 찾는
    데 씁니다. `open_pl` 은 지금 도는 실행들의 평가손익이고, 기간이 오늘까지가
    아니면 모릅니다(`None`).

    **전략 줄은 버전 하나입니다**(`strategies[].id` = 버전 id). 곁의 `groupId`
    는 버전을 묶는 전략이고, 거래 줄의 `strategyId` 는 화면이 전략 줄을 찾는
    열쇠라 **버전 id** 입니다 — 전략 묶음은 거래 줄의 `groupId` 에 있습니다.
    """
    by_version = {
        str(row.get("id") or ""): row for row in versions if isinstance(row, dict) and row.get("id")
    }
    kept = picked(rows, ask)
    truncated = len(kept) > MAX_TRADES
    if truncated:
        kept = kept[-MAX_TRADES:]

    days = sorted({day_of_row(row) for row in kept})
    labels = [one.isoformat() for one in days]
    day_at = {one: at for at, one in enumerate(days)}

    perfs: dict[str, dict[str, Any]] = {}
    daily: dict[str, list[float]] = {}
    trades: list[dict[str, Any]] = []
    for row in kept:
        version_id = str(row.get("versionId") or "")
        group = _group_of(row, by_version)
        if version_id not in perfs:
            held = by_version.get(version_id)
            perfs[version_id] = {
                "id": version_id,
                "name": str(held.get("name") or "") if held else UNKNOWN_NAME,
                "version": str(held.get("version") or "") if held else "",
                "groupId": group,
                "gross": 0.0,
                "fee": 0.0,
            }
            daily[version_id] = [0.0] * len(days)
        perf = perfs[version_id]
        # 손익 칸이 비어 있는 거래(승수를 몰라 돈으로 못 셈)는 합계에서 빼고 줄에는
        # 모름으로 냅니다. 화면이 「모르는 거래 n건은 뺐습니다」를 적습니다.
        gross = _maybe(row.get("grossPl"))
        fee = _float(row.get("fee"))
        net = _maybe(row.get("netPl"))
        perf["gross"] += gross or 0.0
        perf["fee"] += fee
        slot = day_at[day_of_row(row)]
        daily[version_id][slot] += net or 0.0
        trades.append(
            {
                "id": trade_key(row),
                "strategyId": version_id,
                "versionId": version_id,
                "groupId": group,
                "at": _iso(row.get("closedAt")),
                "openedAt": _iso(row.get("openedAt")),
                "side": str(row.get("side") or ""),
                "qty": _int(row.get("quantity")) or 0,
                "net": net,
                "gross": gross,
                "fee": fee,
                "code": str(row.get("code") or ""),
                "market": str(row.get("market") or ""),
                "session": str(row.get("session") or ""),
                "model": model_of(row),
            }
        )

    balances = _balances(day_rows, ask)
    equity = _equity_ready(kept, balances)
    if equity:
        labels, curves = _equity_curves(balances, kept, ask)
        for version_id in curves:
            if version_id not in perfs:
                held = by_version.get(version_id)
                perfs[version_id] = {
                    "id": version_id,
                    "name": str(held.get("name") or "") if held else UNKNOWN_NAME,
                    "version": str(held.get("version") or "") if held else "",
                    "groupId": _group_of({"versionId": version_id}, by_version),
                    "gross": 0.0,
                    "fee": 0.0,
                }
        strategies = [
            {**perf, "curve": curves.get(version_id, [0.0] * len(labels))}
            for version_id, perf in perfs.items()
        ]
    else:
        strategies = []
        for version_id, perf in perfs.items():
            running = 0.0
            curve = []
            for value in daily[version_id]:
                running += value
                curve.append(running)
            strategies.append({**perf, "curve": curve})

    for one in strategies:
        one["maxDrawdown"] = trade_drawdown(
            row for row in kept if str(row.get("versionId") or "") == one["id"]
        )

    return {
        # 최대 낙폭은 거래(청산 시각) 순 실현 순손익으로 셉니다(`trade_drawdown`).
        # 곡선이 평가 기준(`curveBasis`)이어도 낙폭은 이 기준입니다.
        "maxDrawdown": trade_drawdown(kept),
        "drawdownBasis": DRAWDOWN_BASIS,
        # 전략 묶음마다의 낙폭(`group_drawdowns`).
        "groups": group_drawdowns(strategies, kept),
        # `kind` 는 화면의 「기록 유형」 글자이고 `record` 는 거른 갈래 값(기록 줄의
        # 같은 이름 칸)입니다. 화면은 글자를 적고, 다시 물을 때는 값을 씁니다.
        "kind": KIND_LABELS[ask.record],
        "record": ask.record,
        "market": ask.market,
        "dates": labels,
        "strategies": strategies,
        "trades": trades,
        # 지금 도는 실행의 평가손익입니다. 기간이 오늘에 닿지 않으면 그 기간 말의
        # 값을 기록에서 알 수 없어 0 이고, `openPlKnown` 이 거짓입니다.
        "openPl": float(open_pl or 0.0),
        "openPlKnown": open_pl is not None,
        "totalLabel": TOTAL_LABELS[ask.record],
        "period": f"{ask.start.isoformat()} ~ {ask.end.isoformat()}",
        "from": ask.start.isoformat(),
        "to": ask.end.isoformat(),
        # 물은 통화. 안 물었으면 시장의 옛 통화입니다(해외는 달러).
        "currency": ask.money,
        # 그 기간·시장·기록 갈래의 거래에 있는 통화들 — 통화 거름과 상관없이 셉니다.
        # 서버는 통화를 더하지 않습니다. 통화마다의 답은 `byCurrency` 에 있습니다
        # (`with_by_currency`).
        "currencies": currencies_of(rows, ask),
        # 이 기간의 거래를 채운 모형들. 둘 이상이면 성적이 서로 다른 가정을 섞은
        # 것이라 화면이 표지를 답니다 — 성적은 그대로 더합니다.
        "models": sorted({model_of(row) for row in kept}),
        "mixedModels": len({model_of(row) for row in kept}) > 1,
        "curveBasis": CURVE_EQUITY if equity else CURVE_REALIZED,
        # 체결 품질. 주문 결과 줄이 없는 기간(그 줄이 생기기 전의 기록)은 비어
        # 있습니다. 앱이 꺼질 때 열려 있던 주문은 결과 줄이 없어 분모에 안 듭니다.
        "quality": _quality(order_rows, balances, kept, ask),
        "truncated": truncated,
        "earliestAt": _iso(kept[0].get("closedAt")) if kept else None,
        # 읽는 사이에 기록 파일이 바뀌어 두 번 읽어도 맞지 않았는지.
        "incomplete": not complete,
    }


def group_drawdowns(
    strategies: Iterable[dict[str, Any]], rows: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """전략 묶음마다의 최대 낙폭 — `[{"groupId", "maxDrawdown"}, ...]`.

    묶음에 속한 버전들의 **거래를 합쳐** 청산 시각 차례로 셉니다(`trade_drawdown`).
    버전마다의 낙폭을 더하거나 그중 큰 값을 고르는 것이 아닙니다 — 한 버전이 잃는
    사이 다른 버전이 벌었으면 묶음의 낙폭은 그만큼 작습니다. 버전이 하나뿐인
    묶음도 싣고, 값은 그 버전의 낙폭과 같습니다. 묶음의 거래가 모두 순손익을
    모르면 `None` 입니다. 묶음을 모르는 버전(`groupId` 없음)은 어느 묶음에도 들지
    않습니다. 차례는 전략 줄에 처음 나온 차례입니다.
    """
    members: dict[str, set[str]] = {}
    for one in strategies:
        group = one.get("groupId")
        if group:
            members.setdefault(str(group), set()).add(str(one.get("id") or ""))
    kept = list(rows)
    return [
        {
            "groupId": group,
            "maxDrawdown": trade_drawdown(
                row for row in kept if str(row.get("versionId") or "") in ids
            ),
        }
        for group, ids in members.items()
    ]


def with_by_currency(build: Callable[[Query], dict[str, Any]], ask: Query) -> dict[str, Any]:
    """`build(ask)` 의 답. 해외이고 통화를 안 물었으면 `byCurrency` 를 더합니다.

    `byCurrency` 는 답의 `currencies` 의 통화마다 **그 통화로 물었을 때의 답
    전체**입니다 — 같은 `build` 로 셉니다. 그래서 화면은 한 번의 요청으로 통화마다의
    성적을 받습니다. 통화가 하나여도 싣고, 그 기간에 거래가 없으면 빈 묶음입니다.
    국내이거나 통화를 물었으면 이 칸이 없습니다.

    서로 다른 통화의 손익을 더한 칸은 만들지 않습니다.
    """
    made = build(ask)
    if ask.market == "overseas" and not ask.currency:
        made["byCurrency"] = {one: build(replace(ask, currency=one)) for one in made["currencies"]}
    return made


#: 성과 넷이 없을 때의 줄 칸. 0 으로 적으면 「기록이 있는데 0」으로 읽힙니다.
NO_PERF: dict[str, None] = {"pl": None, "winRate": None, "trades": None, "maxDrawdown": None}


def recent(
    rows: Iterable[dict[str, Any]],
    version: dict[str, Any],
    record: str,
    *,
    today: date | None = None,
    days: int = DEFAULT_DAYS,
) -> dict[str, Any] | None:
    """한 버전·한 기록 갈래의 성과 넷 — **최근 30 달력일**(오늘 포함, 거래일 기준).

    넷의 뜻:

        pl           순손익(실현 − 수수료) 합
        winRate      순손익이 0 보다 큰 거래의 비율(%, 소수 한 자리)
        trades       닫힌 거래 수
        maxDrawdown  거래(청산 시각) 순 누적 순손익의 최대 낙폭(크기, `trade_drawdown`)

    그 갈래의 거래가 없으면 `None` 입니다(넷이 0 인 것과 다릅니다).
    """
    made = performances(rows, [version], today=today, days=days)
    held = made.get(str(version.get("id") or ""))
    return held["performance"].get(record) if held else None


def performance_of(
    rows: Iterable[dict[str, Any]], version: dict[str, Any], *, today: date | None = None
) -> dict[str, Any]:
    """버전 하나의 목록 성과 칸들(`performances` 의 한 줄)."""
    made = performances(rows, [version], today=today)
    return made.get(str(version.get("id") or ""), {**NO_PERF, "performance": _NO_SPLIT})


def performances(
    rows: Iterable[dict[str, Any]],
    versions: Iterable[dict[str, Any]],
    *,
    today: date | None = None,
    days: int = DEFAULT_DAYS,
) -> dict[str, dict[str, Any]]:
    """버전 id → 목록 줄에 붙일 성과 칸들. **거래 줄을 한 번만 훑습니다.**

    목록은 화면을 열 때마다 불리고 버전이 수백일 수 있어, 버전마다 기록 전체를
    다시 거르지 않고 `(버전, 갈래)` 로 한 번에 모읍니다.

    **갈래마다 따로 냅니다**(`performance: {paper, sim, real}`) — 앱 시뮬레이션과
    모의·실전 계좌를 한 성적으로 섞지 않습니다. 기록이 없는 갈래는 `None` 이고, 어느 쪽을
    보일지는 화면이 고릅니다.

    줄의 평평한 칸 넷(`pl`·`winRate`·`trades`·`maxDrawdown`)은 **그 버전의 기록
    유형(`record` 칸) 갈래의 값**입니다. 화면이 이 칸을 바로 읽으므로 비우지
    않습니다. 기록 유형을 모르면 넷 다 `None` 입니다.
    """
    today = today or now_kst().date()
    start = today - timedelta(days=days - 1)
    wanted = {
        str(row.get("id") or ""): row
        for row in versions
        if isinstance(row, dict) and row.get("id")
    }
    bunches: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        version_id = str(row.get("versionId") or "")
        version = wanted.get(version_id)
        if version is None:
            continue
        record = str(row.get("record") or PAPER)
        if record not in RECORDS or str(row.get("market") or "") != str(version.get("market") or ""):
            continue
        closed = _int(row.get("closedAt"))
        if closed is None or not start <= day_of_row(row) <= today:
            continue
        bunches.setdefault((version_id, record), []).append(row)

    made: dict[str, dict[str, Any]] = {}
    for version_id, version in wanted.items():
        split = {
            record: _perf_or_none(bunches.get((version_id, record)), version, version_id, record)
            for record in RECORDS
        }
        own = kind_of_label(str(version.get("record") or ""))
        flat = split.get(own) if own is not None else None
        made[version_id] = {**(flat or NO_PERF), "performance": split}
    return made


#: 두 갈래 다 기록이 없을 때.
_NO_SPLIT: dict[str, None] = {record: None for record in RECORDS}


def _perf(rows: list[dict[str, Any]] | None, version: dict[str, Any]) -> dict[str, Any] | None:
    """모아 둔 한 `(버전, 갈래)` 의 성과 넷. 거래가 없으면 `None`.

    거름은 모을 때 끝났으므로 겹친 줄만 거르고 바로 셉니다 — 조회 답 한 벌
    (`dataset`)을 버전마다 세우면 목록이 수백 ms 가 됩니다. 낙폭은 조회 답과 같은
    셈입니다(`trade_drawdown`).
    """
    if not rows or str(version.get("market") or "") not in CURRENCIES:
        return None
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    nets: list[float] = []
    for row in rows:
        key = trade_key(row)
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
        net = _maybe(row.get("netPl"))
        if net is None:
            continue
        nets.append(net)
    if not nets:
        # 거래는 있는데 모두 순손익을 모릅니다(승수를 몰라 돈으로 못 셈). 거래 수만
        # 싣고 돈으로 센 칸은 모름입니다 — 0 으로 적으면 「본전」으로 읽힙니다.
        return {"pl": None, "winRate": None, "trades": len(unique), "maxDrawdown": None}
    won = sum(1 for one in nets if one > 0)
    return {
        "pl": sum(nets),
        "winRate": round(won / len(nets) * 100, 1),
        "trades": len(nets),
        "maxDrawdown": trade_drawdown(unique),
    }


def _perf_or_none(
    rows: list[dict[str, Any]] | None, version: dict[str, Any], version_id: str, record: str
) -> dict[str, Any] | None:
    """한 `(버전, 갈래)` 의 성과. 셈이 실패하면 그 칸만 비웁니다 — 버전 하나 때문에
    목록 전체의 성과가 사라지지 않게 합니다. 까닭은 기록에 남깁니다."""
    try:
        return _perf(rows, version)
    except Exception:  # noqa: BLE001 — 한 버전의 실패가 목록을 막으면 안 됩니다.
        logger.warning("버전 %s(%s)의 성과를 세지 못했습니다.", version_id, record, exc_info=True)
        return None


def trade_drawdown(rows: Iterable[dict[str, Any]]) -> float | None:
    """닫힌 거래들의 최대 낙폭(크기). 거래가 없으면 `None` 입니다.

    거래의 순손익(실현 − 수수료)을 **청산 시각 차례로** 더한 누적 곡선에서, 앞선
    가장 높은 값(시작 0 을 첫 고점으로)에서 가장 많이 내려간 크기입니다. 같은
    청산 시각은 `trade_key` 순으로 고정합니다. 하루 단위로 묶으면 같은 날 벌었다
    잃은 것이 서로 지워져 낙폭이 0 으로 보입니다. 순손익을 모르는 거래(승수를 몰라
    돈으로 못 셈)는 뺍니다. 미청산 보유의 평가손익은 넣지 않습니다(`DRAWDOWN_BASIS`).
    """
    ordered = sorted(
        (row for row in rows if _maybe(row.get("netPl")) is not None),
        key=lambda one: (_int(one.get("closedAt")) or 0, trade_key(one)),
    )
    if not ordered:
        return None
    running = 0.0
    curve: list[float] = []
    for row in ordered:
        running += _maybe(row.get("netPl")) or 0.0
        curve.append(running)
    return _drawdown(curve)


def kind_of_label(label: str) -> str | None:
    """목록 줄의 기록 유형 글자 → 기록 갈래. 모르는 글자는 `None` 입니다."""
    for kind, said in KIND_LABELS.items():
        if said == label:
            return kind
    return None


def open_pl_of(
    engines: Iterable[Any], ask: Query, *, today: date | None = None
) -> float | None:
    """지금 도는 실행들의 평가손익 합. **기간이 오늘에 닿을 때만** 셉니다.

    지난 기간의 기간 말 평가손익은 기록에 없어 모릅니다(`None`).
    """
    today = today or now_kst().date()
    if ask.end < today:
        return None
    total = 0.0
    for made in engines:
        run = made.run
        if run.mode != ask.record or run.market != ask.market:
            continue
        if ask.versions and run.version_id not in ask.versions:
            continue
        if ask.code and run.code != ask.code:
            continue
        if _run_currency(run) != ask.money:
            continue
        total += made.ledger.unrealized(made.book.last_price)
    return total


def _run_currency(run: Any) -> str:
    """실행의 통화(`runs.currency_of`). 실행 모듈은 쓸 때 읽습니다."""
    from backend.strategy.runs import currency_of

    return currency_of(run)


def currencies_of(rows: Iterable[dict[str, Any]], ask: Query) -> list[str]:
    """그 기간·시장·기록 갈래의 거래 줄에 있는 통화들(버전·종목·통화 거름과 상관없음)."""
    made: set[str] = set()
    for row in rows:
        if str(row.get("record") or PAPER) != ask.record or str(row.get("market") or "") != ask.market:
            continue
        if _int(row.get("closedAt")) is None or not ask.start <= day_of_row(row) <= ask.end:
            continue
        currency = currency_of_row(row)
        if currency:
            made.add(currency)
    return sorted(made)


def _matches(row: dict[str, Any], ask: Query) -> bool:
    """기록 갈래·시장·버전·종목이 조건에 드는지(기간은 따로 봅니다)."""
    if str(row.get("record") or PAPER) != ask.record:
        return False
    if str(row.get("market") or "") != ask.market:
        return False
    if ask.versions and str(row.get("versionId") or "") not in ask.versions:
        return False
    if currency_of_row(row) != ask.money:
        return False
    return not ask.code or str(row.get("code") or "") == ask.code


def _balances(
    rows: Iterable[dict[str, Any]], ask: Query
) -> dict[tuple[str, date], dict[str, Any]]:
    """조건에 드는 거래일 잔고 줄 — `(실행, 거래일)` 마다 하나.

    **기간 앞의 줄도 담습니다.** 곡선은 기간 앞 마지막 값을 빼서 기간 안의
    변화만 그리기 때문입니다. 같은 실행·같은 거래일에 줄이 둘이면(앱을 다시
    켠 뒤 또 쓴 경우) 늦게 쓴 표지(`late`)가 없는 줄을 먼저 씁니다.
    """
    made: dict[tuple[str, date], dict[str, Any]] = {}
    for row in rows:
        if not _matches(row, ask):
            continue
        try:
            day = date.fromisoformat(str(row.get("day") or ""))
        except ValueError:
            continue
        key = (str(row.get("runId") or ""), day)
        held = made.get(key)
        if held is None or (held.get("late") and not row.get("late")):
            made[key] = row
    return made


def _equity_ready(
    kept: list[dict[str, Any]], balances: dict[tuple[str, date], dict[str, Any]]
) -> bool:
    """곡선을 평가 기준으로 세울 수 있는지 — `CURVE_REALIZED` 설명의 「all」.

    기간 안의 거래마다 **그 실행·그 거래일**의 잔고 줄이 있어야 하고, 기간 안에
    잔고 줄이 하나는 있어야 합니다(늦게 쓴 줄도 셉니다).
    """
    if not balances:
        return False
    for row in kept:
        day = day_of_row(row)
        if (str(row.get("runId") or ""), day) not in balances:
            return False
    return True


def _equity_curves(
    balances: dict[tuple[str, date], dict[str, Any]], kept: list[dict[str, Any]], ask: Query
) -> tuple[list[str], dict[str, list[float]]]:
    """평가 기준 곡선 — `(거래일 글자들, 버전 → 곡선)`.

    한 점은 그 거래일 끝의 `실현 − 수수료 + 평가` 에서 **기간 앞 마지막 값**을
    뺀 것입니다(기간 안의 변화). 한 버전의 실행이 여럿이면 더하고, 그날 줄이
    없는 실행(쉬었거나 끝난 실행)은 앞 거래일의 값을 이어 씁니다.
    """
    in_range = {day for (_, day) in balances if ask.start <= day <= ask.end}
    in_range |= {day_of_row(row) for row in kept}
    days = sorted(in_range)
    by_run: dict[str, list[tuple[date, dict[str, Any]]]] = {}
    for (run_id, day), row in balances.items():
        by_run.setdefault(run_id, []).append((day, row))
    curves: dict[str, list[float]] = {}
    for _run_id, rows in by_run.items():
        rows.sort(key=lambda one: one[0])
        version_id = str(rows[0][1].get("versionId") or "")
        before = [row for day, row in rows if day < ask.start]
        base = _float(before[-1].get("equity")) if before else 0.0
        line = curves.setdefault(version_id, [0.0] * len(days))
        at = 0
        last: float | None = None
        for slot, day in enumerate(days):
            while at < len(rows) and rows[at][0] <= day:
                last = _float(rows[at][1].get("equity"))
                at += 1
            if last is not None:
                line[slot] += last - base
    return [one.isoformat() for one in days], curves


def _quality(
    order_rows: Iterable[dict[str, Any]],
    balances: dict[tuple[str, date], dict[str, Any]],
    kept: list[dict[str, Any]],
    ask: Query,
) -> dict[str, Any] | None:
    """체결 품질 — 값마다 분자·분모를 함께. 주문 결과 줄이 없으면 `None`.

        fillRate    체결(전량·일부) ÷ 접수되어 끝난 주문
        rejectRate  (계좌 거절 + 접수 전 막힘) ÷ 낸 시도(끝난 주문 + 접수 전 막힘)
        marginPeak  기간 안 거래일 잔고 줄의 최대 증거금 사용률
        holding     기간 안 거래의 평균 보유시간

    주문 줄은 **끝난 시각의 거래일**로 기간을 가릅니다.
    """
    seen: set[tuple[str, str]] = set()
    outcomes: list[str] = []
    for row in order_rows:
        if not _matches(row, ask):
            continue
        ended = _int(row.get("endedAt"))
        if ended is None:
            continue
        if not ask.start <= day_of_row(row, "endedAt") <= ask.end:
            continue
        key = (str(row.get("runId") or ""), str(row.get("orderId") or ""))
        if key in seen:
            continue
        seen.add(key)
        outcomes.append(str(row.get("outcome") or ""))
    if not outcomes:
        return None
    filled = sum(1 for one in outcomes if one in _FILLED_LIKE)
    ended_count = sum(1 for one in outcomes if one in _ENDED)
    rejected = sum(1 for one in outcomes if one in _REJECTED_LIKE)
    attempts = ended_count + sum(1 for one in outcomes if one == "refused")
    peaks = [
        _float(row.get("marginPeak"))
        for (_, day), row in balances.items()
        if ask.start <= day <= ask.end and row.get("marginPeak") is not None
    ]
    peak = max(peaks) if peaks else None
    spans = [
        (_int(row.get("closedAt")) or 0) - (_int(row.get("openedAt")) or 0) for row in kept
    ]
    holding = sum(spans) / len(spans) if spans else None
    return {
        "holding": _duration(holding) if holding is not None else "—",
        "fillRate": _ratio(filled, ended_count),
        "rejectRate": _ratio(rejected, attempts),
        "marginPeak": f"{peak * 100:.1f}%" if peak is not None else "—",
        "counts": {
            "filled": filled,
            "ended": ended_count,
            "rejected": rejected,
            "attempts": attempts,
            "marginPeak": peak,
            "holdingMs": holding,
        },
    }


def _ratio(part: int, whole: int) -> str:
    """「92.0% (23/25건)」. 분모가 0 이면 「—」입니다."""
    if whole <= 0:
        return "—"
    return f"{part / whole * 100:.1f}% ({part}/{whole}건)"


def _duration(millis: float) -> str:
    """보유시간 글자 — 「45분」·「2시간 5분」·「1일 3시간」."""
    minutes = int(round(millis / 60_000))
    if minutes < 60:
        return f"{minutes}분"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}시간 {minutes}분" if minutes else f"{hours}시간"
    days, hours = divmod(hours, 24)
    return f"{days}일 {hours}시간" if hours else f"{days}일"


def _group_of(row: dict[str, Any], by_version: dict[str, dict[str, Any]]) -> str | None:
    """그 줄의 전략 묶음. 줄에 없으면 버전으로 찾고, 그래도 없으면 `None`."""
    own = str(row.get("groupId") or "")
    if own:
        return own
    held = by_version.get(str(row.get("versionId") or ""))
    group = str(held.get("groupId") or "") if held else ""
    return group or None


def _drawdown(curve: list[float]) -> float:
    """누적 곡선의 최대 낙폭. 기록 전(0)을 첫 고점으로 봅니다 — 첫날부터 잃은
    거래도 낙폭입니다."""
    peak = 0.0
    worst = 0.0
    for value in curve:
        peak = max(peak, value)
        worst = max(worst, peak - value)
    return worst


def _iso(value: Any) -> str | None:
    stamp = _int(value)
    if stamp is None:
        return None
    return iso(datetime.fromtimestamp(stamp / 1000, tz=KST))


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _maybe(value: Any) -> float | None:
    """수이면 수, 비었으면(모름) `None`. 모름을 0 으로 바꾸지 않습니다."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

