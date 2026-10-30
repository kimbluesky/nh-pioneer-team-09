"""`/api/strategy/versions…` 라우트 — 전략 버전 담기.

**여기서 담은 설정으로 실행이 돕니다** — 실행 목록(`/api/strategy/runs…`)과
엔진 루프가 그 뒤를 잇고, `sim` 실행은 모의 계좌로, `real` 실행은 실전 계좌로
주문을 냅니다. 계좌로 나가는 길의 문들은 `runs.py` 첫머리에 있습니다.

저장은 **갈래를 봅니다.** 적을 수 없는 값(`error`)이면 담지 않고 400 으로,
그대로는 실행을 시작할 수 없는 설정(`warning`)뿐이면 담고 200 으로 답합니다. 두
경우 다 무엇이 걸렸는지 `problems` 로 함께 냅니다 — 까닭은 `validate.py`
첫머리에.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from backend.sessions.hours import now_kst
from backend.strategy import backtest as backtest_lib
from backend.strategy import instrument, limits, rules, runs, validate
from backend.strategy import records as records_lib
from backend.strategy import journal as journal_lib
from backend.strategy.journal import Journal
from backend.strategy.journal import journal as default_journal
from backend.strategy.limits import LimitStore
from backend.strategy.limits import store as default_limits
from backend.strategy.runs import MAX_RUNS
from backend.strategy.service import (
    NOT_STARTED,
    WHY_NOT_FOUND,
    RunService,
    StrategyRunError,
    qty_problems,
    runnable_problems,
    trading_day_of,
)
from backend.strategy.service import service as default_service
from backend.strategy.store import StrategyStore, StrategyStoreError
from backend.strategy.store import store as default_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/strategy/versions", tags=["strategy"])

# 담지 못했을 때 화면에 적는 말.
_NOT_SAVED = "전략을 저장하지 못했습니다."
_NOT_DELETED = "전략을 지우지 못했습니다."
_NOT_FOUND = "그 전략을 찾지 못했습니다."
_ID_MISMATCH = "주소의 전략과 본문의 전략이 다릅니다."
_LIMITS_NOT_SAVED = "손실 한도를 저장하지 못했습니다."
#: 이 앱 쪽에서 일이 안 됐을 때(파일·계좌 조회 등) 화면에 적는 말. 까닭은 기록에만 남깁니다.
_RUN_NOT_SAVED = "실행 상태를 저장하지 못했습니다. 잠시 뒤 다시 해 주세요."
_NOT_RECONCILED = "계좌와 맞춰 보지 못했습니다. 잠시 뒤 다시 해 주세요."
_NOT_STOPPED = "전체 중지를 마치지 못했습니다. 실행 목록을 확인해 주세요."
_RECORDS_UNREAD = "기록을 읽지 못했습니다. 잠시 뒤 다시 해 주세요."


def failure(
    status: int, message: str, problems: list[dict[str, str]] | None = None
) -> HTTPException:
    """/api/strategy 의 오류 답 한 모양 — `{"detail": {"message", "problems"}}`.

    짚을 칸이 있으면 `problems` 에 저장 검사와 같은 줄(`field`·`severity`·`message`)로
    싣고, 없으면 빈 목록입니다. 요청 모양 자체가 틀린 FastAPI 의 422 는 이 자리를
    지나지 않습니다.
    """
    return HTTPException(
        status_code=status, detail={"message": message, "problems": list(problems or [])}
    )


def get_store() -> StrategyStore:
    """앱이 쓰는 저장소. 테스트는 이 의존성을 바꿔 끼웁니다."""
    return default_store


def get_service() -> RunService:
    """앱이 쓰는 실행 목록. 테스트는 이 의존성을 바꿔 끼웁니다."""
    return default_service


def get_master() -> Any:
    """앱이 쓰는 종목 마스터. 저장할 때 품목을 여기에 대고 봅니다.

    테스트는 이 의존성을 바꿔 끼웁니다 — 열여덟 만 줄을 읽지 않고도 품목 검사를
    지나갈 수 있어야 합니다.
    """
    from backend.master import master_service

    return master_service.service


class SaveRequest(BaseModel):
    """전략 버전 하나를 담는 요청.

    `version` 은 목록 줄이고 `draft` 는 설정입니다. 설정을 아직 안 고친 버전은
    `draft` 없이 담깁니다 — 목록에만 있는 줄이 그렇습니다.
    """

    version: dict[str, Any]
    draft: dict[str, Any] | None = None


def get_journal() -> Journal:
    """앱이 쓰는 기록 파일. 테스트는 이 의존성을 바꿔 끼웁니다."""
    return default_journal


@router.get("")
def read_all(
    store: StrategyStore = Depends(get_store), journal: Journal = Depends(get_journal)
) -> dict[str, Any]:
    """담아 둔 버전 전부.

    `versions` 는 목록 줄, `drafts` 는 버전 id 마다의 설정입니다. 아직 아무것도
    담지 않았으면 둘 다 비어 있습니다 — 오류가 아닙니다.
    """
    made = store.read()
    # 목록 줄의 「최근 30일」 성과 넷. **저장소가 아니라 기록 파일이 답합니다** —
    # 저장소에 적어 두면 거래가 쌓일 때마다 전략 파일을 다시 써야 하고, 둘이
    # 갈리는 날 어느 쪽이 맞는지 고를 수 없습니다. 기록을 못 읽으면 넷을 비웁니다.
    rows = _trade_rows(journal)
    listed = made.get("versions") or []
    try:
        perf = records_lib.performances(rows, listed)
    except Exception:  # noqa: BLE001 — 성과를 못 세도 목록은 냅니다(칸은 빔).
        logger.warning("전략 목록의 성과를 세지 못했습니다.", exc_info=True)
        perf = {}
    versions = [
        {**row, **perf.get(str(row.get("id") or ""), {})} if isinstance(row, dict) else row
        for row in listed
    ]
    # 못 읽은 파일이 있었으면 화면이 그 사실을 말할 수 있어야 합니다 — 기록만
    # 남기면 바탕화면 앱 사용자는 못 봅니다.
    return {
        **made,
        "versions": versions,
        # 매개변수가 생기기 전의 지표 이름 → {지표, 매개변수}. 화면은 이 표 하나로
        # 옛 전략을 읽고, 파일은 다시 쓰지 않습니다(`rules.LEGACY_OPERANDS`).
        "legacyOperands": rules.legacy_table(),
        "store": store.state.payload,
    }


def _trade_rows(journal: Journal) -> list[dict[str, Any]]:
    """기록 파일의 거래 줄. 읽다 막히면 빈 목록입니다 — 목록을 못 세울 까닭은 아닙니다."""
    try:
        return journal.trades()[0]
    except Exception:  # noqa: BLE001 — 성과 칸이 비는 것으로 끝납니다.
        logger.warning("전략 기록을 읽지 못해 목록의 성과를 비웁니다.", exc_info=True)
        return []


@router.put("/{version_id}")
def save_one(
    version_id: str,
    ask: SaveRequest,
    store: StrategyStore = Depends(get_store),
    master: Any = Depends(get_master),
) -> dict[str, Any]:
    """버전 하나를 담습니다. **적을 수 없는 값이 있으면 담지 않습니다.**

    `error` 가 하나라도 있으면 400 으로 돌려보내고 아무것도 담지 않습니다 —
    배지만 달고 받으면 손절 「abc」가 저장된 값이 됩니다. `warning` 뿐이면 담고
    200 으로 답합니다 — 고치던 것을 저장하지 못하면 적어 둔 조건을 잃습니다.
    어느 쪽이든 무엇이 걸렸는지 `problems` 로 함께 냅니다.

    줄이 가리키는 **종목도 여기서 봅니다** — 빈 품목, 종목 마스터에 없는 품목,
    시장이 안 맞는 품목은 담지 않습니다. 색인을 아직 못 받았으면 모양만 봅니다
    (`validate.instrument_problems`). 품목 없이 **역할만** 온 줄은 표로 채워
    받습니다 — 품목 칸이 생기기 전의 화면이 그 모양으로 보냅니다.

    목록 줄의 한도 셋(`marginBudget`·`orderQty`·`dailyLoss`)은 **설정 값으로
    채워 담습니다** — 화면이 목록 줄에 무엇을 적어 보내든 설정이 참입니다.
    까닭은 `validate.FROM_AUX` 에.

    주소의 id 와 본문의 id 가 다르면 받지 않습니다 — 어느 쪽이 맞는지 서버가
    고를 수 없습니다.
    """
    version = dict(ask.version)
    body_id = str(version.get("id") or "").strip()
    if body_id and body_id != version_id:
        raise failure(400, _ID_MISMATCH, [_problem("id", _ID_MISMATCH)])
    version["id"] = version_id
    # **역할만 들고 온 줄도 받습니다.** 품목 칸이 생기기 전의 화면이 보내는
    # 모양입니다 — 여기서 채워 두면 그 줄은 다음부터 품목을 들고 다닙니다.
    version = instrument.filled(version)

    draft = validate.normalized(version, ask.draft)
    version = validate.aligned(version, draft)
    market = str(version.get("market") or "")
    found = validate.problems(draft, market=market) if draft is not None else []
    # **설정을 함께 안 보낸 줄도 봅니다** — 목록에만 있는 줄이 가리키는 종목도
    # 그 줄의 것입니다.
    found = validate.instrument_problems(version, master=master) + found
    if validate.blocked(found):
        raise failure(400, _NOT_SAVED, found)

    try:
        made = store.save(version, draft)
    except StrategyStoreError as exc:
        logger.warning("전략을 담지 못했습니다: %s", exc)
        raise failure(400, f"{_NOT_SAVED} {exc}") from exc
    except OSError as exc:
        logger.warning("전략 파일을 쓰지 못했습니다: %s", exc)
        raise failure(500, _NOT_SAVED) from exc

    return {**made, "problems": found}


#: 끝나지 않은 실행이 붙은 버전을 지우려 할 때의 말. 전략 목록 화면의 삭제 잠금과 같은 글입니다.
_HAS_LIVE_RUNS = "끝나지 않은 실행 {count}개가 있어 지울 수 없습니다 — 먼저 종료하세요"


@router.delete("/{version_id}")
def delete_one(
    version_id: str,
    store: StrategyStore = Depends(get_store),
    service: RunService = Depends(get_service),
) -> dict[str, Any]:
    """버전 하나를 지웁니다. 없던 것이면 404 입니다.

    **그 버전에 끝나지 않은 실행(실행 중·중지·확인 필요 등 종료가 아닌 것)이
    있으면 지우지 않고 409 입니다.** 실행은 시작할 때의 설정을 물고 돌지만,
    목록에서 버전이 사라지면 그 실행이 어느 전략의 것인지 화면이 더는 보여 줄
    수 없습니다. 먼저 실행을 종료한 뒤 지웁니다. 없는 버전은 실행을 세지 않고
    그대로 404 입니다.
    """
    try:
        listed = store.read().get("versions") or []
        known = any(isinstance(row, dict) and row.get("id") == version_id for row in listed)
        live = (
            sum(
                1
                for made in service.engines()
                if made.run.version_id == version_id and made.run.state != runs.ENDED
            )
            if known
            else 0
        )
    except Exception as exc:  # noqa: BLE001 — 실행을 못 세면 지우지 않습니다.
        logger.warning("전략을 지우기 전에 실행 목록을 읽지 못했습니다: %s", exc)
        raise failure(500, _NOT_DELETED) from exc
    if live:
        why = _HAS_LIVE_RUNS.format(count=live)
        raise failure(409, why, [_problem("runs", why)])
    try:
        gone = store.delete(version_id)
    except OSError as exc:
        logger.warning("전략 파일을 쓰지 못했습니다: %s", exc)
        raise failure(500, _NOT_DELETED) from exc
    if not gone:
        raise failure(404, _NOT_FOUND)
    return store.read()


# ── 검증 실행 ────────────────────────────────────────────

#: 실행 라우트. 버전과 prefix 를 나눠 두면 `/versions/{id}` 와 `/runs/{id}` 가
#: 서로의 자리를 가리지 않습니다.
runs_router = APIRouter(prefix="/api/strategy", tags=["strategy"])


class AddRunRequest(BaseModel):
    """검증 실행 하나를 세우는 요청.

    설정(`draft`)을 **그대로 물고 갑니다** — 실행을 시작한 뒤에 전략을 고쳐도
    돌고 있는 실행은 시작할 때의 설정으로 계속합니다.
    """

    id: str
    versionId: str
    draft: dict[str, Any]
    market: str = "domestic"
    code: str
    session: str
    #: 주문당 수량. 안 보내면 설정의 주문당 수량(`aux.perOrder`)이고, 그것도 없으면
    #: 1 입니다 — 백테스트와 같은 셈입니다(`qty_of`).
    qty: int | None = None
    cash: float = 0.0
    feePerContract: float = 0.0
    #: `paper`(종이 장부) · `sim`(모의 계좌) · `real`(실전 계좌).
    mode: str = "paper"
    #: 화면 확인창을 지났다는 표지. 계좌 갈래는 이것이 없으면 한 건도 안 냅니다.
    approved: bool = False
    #: 화면이 믿는 활성 환경(`paper`·`real`). 계좌 갈래는 갈래가 뜻하는 환경·이
    #: 값·서버의 활성 환경이 셋 다 같아야 섭니다. 종이 실행은 보지 않습니다.
    environment: str = ""


class BacktestRequest(BaseModel):
    """지난 봉으로 돌려 보기. **저장 요청과 같은 모양**(`version`·`draft`)에 셈할 것을 더합니다.

    `draft` 는 저장하지 않은 설정이어도 됩니다 — 셈은 아무것도 남기지 않습니다.
    """

    version: dict[str, Any]
    draft: dict[str, Any]
    code: str
    session: str
    #: `1d`·`3d`(마지막 세션 수) · `all`(받은 만큼).
    range: str = "all"
    #: 주문당 수량. 비우면 설정의 주문당 수량(`aux.perOrder`), 그것도 없으면 1 입니다.
    qty: int | None = None
    feePerContract: float = 0.0
    slippageTicks: int = backtest_lib.SLIPPAGE_TICKS


# 셈하지 못했을 때 화면에 적는 말.
_NOT_COUNTED = "고칠 값이 있어 셈하지 않았습니다."


@runs_router.post("/backtest")
def run_backtest(
    ask: BacktestRequest,
    service: RunService = Depends(get_service),
    master: Any = Depends(get_master),
) -> dict[str, Any]:
    """지난 봉으로 돌려 본 답. **읽기만 합니다** — 저장·실행·기록·한도 어디에도 안 씁니다.

    설정은 **저장할 때와 같은 자리**로 봅니다. 적을 수 없는 값이 있거나 엔진이 돌 수
    없는 설정이면 저장과 같은 모양(`{message, problems}`)의 400 입니다. 셈의 가정은
    `backtest.py` 첫머리와 답의 `assumptions` 에 있습니다. 다른 셈이 돌고 있으면
    409, 시간 상한을 넘으면 504, 봉을 못 받으면 502 입니다.
    """
    version = instrument.filled(dict(ask.version))
    draft = validate.normalized(version, ask.draft) or {}
    version = validate.aligned(version, draft)
    market = str(version.get("market") or "")
    session = runs.session_key(ask.session) or ""
    aux = draft.get("aux") if isinstance(draft.get("aux"), dict) else {}

    found = validate.instrument_problems(version, master=master) + validate.problems(
        draft, market=market
    )
    found = validate.unique(
        [one for one in found if validate.blocked([one])] + runnable_problems(draft)
    )
    if session not in runs.MARKET_SESSIONS.get(market, ()):
        found.append(_problem("session", "그 시장에 없는 세션입니다."))
    if ask.range not in backtest_lib.RANGES:
        found.append(_problem("range", backtest_lib.WHY_RANGE))
    if not 0 <= ask.slippageTicks <= backtest_lib.SLIPPAGE_MAX:
        found.append(_problem("slippageTicks", backtest_lib.WHY_SLIPPAGE))
    # 수량 검사는 실행을 세울 때와 같은 자리입니다 — 최대 보유를 넘는 수량으로 셈하면
    # 오류 없이 거래 0 이 나옵니다.
    qty = qty_of(ask.qty, draft)
    found.extend(qty_problems(qty, aux))
    if ask.feePerContract < 0:
        found.append(_problem("feePerContract", backtest_lib.WHY_FEE))
    facts = service.instrument(market, ask.code, session)
    if not facts.get("tick"):
        found.append(_problem("code", backtest_lib.WHY_TICK))
    if market == validate.OVERSEAS and not facts.get("multiplier"):
        # 해외는 승수를 모르면 1 로 세지 않습니다 — 손익이 돈인 척 나옵니다.
        found.append(_problem("code", runs.WHY_MULTIPLIER_UNKNOWN))
    if found:
        raise failure(400, _NOT_COUNTED, found)

    def work() -> dict[str, Any]:
        bars = backtest_lib.gather(
            service,
            market=market,
            code=ask.code,
            session=session,
            base_bar=str(draft.get("baseBar") or ""),
            exchange=str(facts.get("exchange") or ""),
            now=int(now_kst().timestamp() * 1000),
        )
        return backtest_lib.compute(
            draft=draft,
            market=market,
            code=ask.code,
            session=session,
            bars=bars,
            tick=float(facts["tick"]),
            multiplier=facts["multiplier"] if market == validate.OVERSEAS else facts.get("multiplier") or 1.0,
            margin=facts.get("margin"),
            kind=str(facts.get("kind") or ""),
            qty=qty,
            fee_per_contract=ask.feePerContract,
            slippage_ticks=ask.slippageTicks,
            sessions=backtest_lib.RANGES[ask.range],
            trading_day=trading_day_of(market, ask.code),
            expiry=_expiry_of(service, market, ask.code),
        )

    try:
        return backtest_lib.run_one(work)
    except backtest_lib.BacktestError as exc:
        raise failure(exc.status, exc.message) from exc


def _expiry_of(service: Any, market: str, code: str) -> Any:
    """종목 만기일(마스터의 `expiry_date`, 여덟 자리). 모르면 `None` 입니다."""
    from datetime import date as date_cls

    master = getattr(service, "_master", None)
    row = master.locate(market, code) if master is not None else None
    text = str((row or {}).get("expiry_date") or "").strip()
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date_cls(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


def _problem(field: str, message: str) -> dict[str, str]:
    return {"field": field, "severity": validate.ERROR, "message": message}


def qty_of(qty: int | None, draft: dict[str, Any] | None) -> int:
    """주문당 수량. 보낸 값이 있으면 그대로, 없으면 설정의 주문당 수량, 그것도 없으면 1.

    실행을 세울 때와 백테스트가 같은 셈을 씁니다 — 둘이 다른 수량으로 돌면 백테스트
    결과가 실행과 견줄 수 없습니다. 범위(1 이상·최대 보유 이하)는 여기서 보지 않고
    둘이 함께 쓰는 검사가 봅니다(`service.qty_problems`).
    """
    if qty is not None:
        return qty
    aux = (draft or {}).get("aux")
    return _whole_or(aux.get("perOrder") if isinstance(aux, dict) else None, 1)


def _whole_or(value: Any, fallback: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return fallback


@runs_router.get("/runs")
def read_runs(service: RunService = Depends(get_service)) -> dict[str, Any]:
    """돌고 있는 검증 실행 전부."""
    rows = service.rows()
    return {
        "runs": rows,
        "max": MAX_RUNS,
        # 지금 세울 수 있는 계좌 갈래와 서버의 활성 환경. 화면이 확인창의 갈래를
        # 이 답에서 고릅니다(`RunService.account_state`).
        **service.account_state(),
        # 줄을 먼저 받아야 파일을 읽은 뒤의 상태가 실립니다.
        "store": service.store.state.payload,
    }


@runs_router.post("/runs")
def add_run(ask: AddRunRequest, service: RunService = Depends(get_service)) -> dict[str, Any]:
    """실행 하나를 세웁니다.

    `mode` 가 `paper` 면 종이 장부로만 돌고 계좌에 아무것도 안 보냅니다.
    `sim` 이면 **모의 계좌로**, `real` 이면 **실전 계좌로** 실제 주문이 나갑니다 —
    갈래가 뜻하는 환경·몸의 `environment`·서버의 활성 환경이 같고 `approved` 가
    참일 때만입니다.

    저장은 「담되 안 돈다」를 받아 주지만 시작은 **돌 수 있는 설정**만 받습니다 —
    모형이 못 다루는 주문유형, 켜진 진입 규칙이 없는 전략, 적을 수 없는 값은
    여기서 막습니다.
    """
    try:
        return service.add(
            run_id=ask.id,
            version_id=ask.versionId,
            draft=ask.draft,
            market=ask.market,
            code=ask.code,
            session=ask.session,
            qty=qty_of(ask.qty, ask.draft),
            cash=ask.cash,
            fee_per_contract=ask.feePerContract,
            mode=ask.mode,
            environment=ask.environment,
            # **승인은 서버 시계로 적습니다.** 화면이 보낸 시각을 그대로 담으면
            # 승인한 적 없는 실행에 시각만 실어 보낼 수 있습니다.
            approved_at=_now() if ask.approved else 0,
        )
    except StrategyRunError as exc:
        raise _refused(exc) from exc
    except OSError as exc:
        logger.warning("실행을 세운 뒤 담지 못했습니다: %s", exc)
        raise failure(500, _RUN_NOT_SAVED) from exc


@runs_router.get("/runs/{run_id}")
def read_run(run_id: str, service: RunService = Depends(get_service)) -> dict[str, Any]:
    """실행 하나의 한 벌 — 줄·체결·이벤트·잔고 곡선.

    **허브는 이 한 벌을 통째로 보내지 않습니다.** 방송에는 줄·체결·이벤트·열린
    주문만 실리고, 조건 표·잔고 곡선·거래 목록(`trades`)은 이 자리로만 볼 수 있습니다.
    """
    return _acted(service.snapshot, run_id)


@runs_router.post("/runs/{run_id}/stop")
def stop_run(
    run_id: str, service: RunService = Depends(get_service)
) -> dict[str, Any]:
    """신규 진입 중지(`pause`). 보유의 손절·익절 감시는 그대로 돕니다."""
    return _acted(service.pause, run_id, _now())


@runs_router.post("/runs/{run_id}/resume")
def resume_run(run_id: str, service: RunService = Depends(get_service)) -> dict[str, Any]:
    """재개. 다음 봉부터 판정합니다."""
    return _acted(service.resume, run_id, _now())


@runs_router.post("/runs/{run_id}/close")
def close_run(run_id: str, service: RunService = Depends(get_service)) -> dict[str, Any]:
    """선택 전략 청산(`flatten`). 시장가 전량입니다."""
    return _acted(service.flatten, run_id, _now())


@runs_router.post("/runs/{run_id}/end")
def end_run(run_id: str, service: RunService = Depends(get_service)) -> dict[str, Any]:
    """실행 종료. 보유나 미체결이 있으면 막고 까닭을 냅니다.

    「확인 필요」 실행도 이 실행의 장부에 보유·미체결이 없으면 끝납니다 — 계좌로
    나가는 요청이 없습니다. 남은 것이 있으면 400 이고, 문구에 무엇이 남았는지가
    실립니다. 「대사 중」은 400 그대로입니다.
    """
    return _acted(service.end, run_id, _now())


@runs_router.delete("/runs/{run_id}")
def delete_run(run_id: str, service: RunService = Depends(get_service)) -> dict[str, Any]:
    """끝난 실행을 목록에서 지웁니다. **기록 파일은 그대로 남습니다.**"""
    _acted(service.delete, run_id)
    return {"runs": service.rows()}


@runs_router.post("/runs/{run_id}/acknowledge")
def acknowledge_run(
    run_id: str, service: RunService = Depends(get_service)
) -> dict[str, Any]:
    """「확인 필요」를 사용자가 풉니다. **계좌와 다시 맞춰 보고** 중지로 둡니다.

    맞으면 중지 상태로 내려놓습니다 — 판정을 다시 여는 것은 「재개」입니다.
    어긋나 있으면 그대로 「확인 필요」이고 까닭이 이벤트에 한 줄씩 붙습니다. 이
    실행의 장부에 남은 것이 없으면 「실행 종료」(`/end`)로 끝낼 수 있다는 줄도
    붙습니다.
    """
    return _acted(service.acknowledge, run_id, _now())


@runs_router.post("/reconcile")
def reconcile_runs(service: RunService = Depends(get_service)) -> dict[str, Any]:
    """계좌 갈래 실행을 계좌와 맞춰 봅니다. 화면이 진행 탭을 열 때 부르는 자리입니다.

    **상태를 내려놓지 않습니다** — 돌고 있는 실행은 그대로 돌고, 어긋난 실행만
    「확인 필요」가 됩니다. 실행마다 계좌 조회 둘을 쓰므로 **차례로** 돕니다.
    """
    try:
        return service.audit_all(_now())
    except Exception as exc:  # noqa: BLE001 — 까닭은 기록에, 화면에는 한 문장만 냅니다.
        logger.warning("계좌와 맞춰 보지 못했습니다.", exc_info=True)
        raise failure(500, _NOT_RECONCILED) from exc


class StopAllRequest(BaseModel):
    """전체 중지의 대상 갈래. `modes` 를 안 보내면(몸이 없어도) 전부입니다."""

    modes: list[str] | None = None


#: 모르는 갈래에 하는 말.
_WHY_STOP_MODES = "멈출 갈래는 paper·sim·real 가운데서 고릅니다"


@runs_router.post("/stop-all")
def stop_all(
    ask: StopAllRequest | None = None, service: RunService = Depends(get_service)
) -> dict[str, Any]:
    """전체 신규 진입 중지. 이미 중지·확인 필요·끝난 실행은 빼고 셉니다.

    몸의 `modes`(예: `["paper"]`, `["sim", "real"]`)를 주면 그 갈래의 실행만 멈춥니다.
    모르는 갈래가 있으면 400 입니다(`modes` 칸). 답은 `paused`(수)·`pausedIds`(멈춘 실행)·`modes`(적용한
    갈래, 안 주면 전 갈래)·`runs`.
    """
    modes = ask.modes if ask is not None else None
    if modes is not None and (not modes or any(one not in runs.MODES for one in modes)):
        raise failure(400, _WHY_STOP_MODES, [_problem("modes", _WHY_STOP_MODES)])
    try:
        return service.stop_all(_now(), modes)
    except Exception as exc:  # noqa: BLE001 — 까닭은 기록에, 화면에는 한 문장만 냅니다.
        logger.warning("전체 중지를 마치지 못했습니다.", exc_info=True)
        raise failure(500, _NOT_STOPPED) from exc


# ── 성과분석 기록 ─────────────────────────────────────────


@runs_router.get("/records")
def read_records(
    record: str | None = None,
    market: str | None = None,
    start: str | None = Query(None, alias="from"),
    end: str | None = Query(None, alias="to"),
    versions: str | None = None,
    code: str | None = None,
    currency: str | None = None,
    store: StrategyStore = Depends(get_store),
    journal: Journal = Depends(get_journal),
    service: RunService = Depends(get_service),
) -> dict[str, Any]:
    """성과분석이 보는 기록 한 벌. 모양과 뜻은 `records.py` 첫머리에.

    `record`(paper·sim — 기록 줄의 같은 이름 칸)와 `market`(domestic·overseas)은
    반드시 받습니다 — 한 답은 한 기록 갈래·한 통화입니다. `from`·`to` 는 거래일(YYYY-MM-DD, 양 끝
    포함)이고 비우면 오늘까지 30일입니다. `versions` 는 쉼표로 이은 버전 id 들입니다.
    `currency` 는 해외 안의 통화 하나(세 글자)이고, 답의 `currencies` 가 그 기간의
    통화 목록입니다. 서버는 통화가 다른 손익을 더하지 않습니다. 해외에서 통화를 안
    물으면 답의 `byCurrency` 에 통화마다의 답 전체가 함께 옵니다(`records.with_by_currency`).
    기록 파일은 요청마다 한 번 읽습니다.

    **요청 모양이 틀리면 422 와 문장 하나입니다.** 담기를 거절하는 400(저장 검사에
    걸림)과 갈라, 「보낸 값을 고쳐야 한다」를 상태 코드로도 말합니다. 쿼리를
    글자로 받아 직접 보므로 영문 오류 목록이 나가지 않습니다.
    """
    try:
        ask = records_lib.query_of(
            record=record,
            market=market,
            start=start,
            end=end,
            versions=versions,
            code=code,
            currency=currency,
        )
    except records_lib.RecordsError as exc:
        raise failure(400, str(exc)) from exc
    try:
        rows, complete = journal.rows()
    except Exception as exc:  # noqa: BLE001 — 까닭은 기록에, 화면에는 한 문장만 냅니다.
        logger.warning("기록 파일을 읽지 못했습니다.", exc_info=True)
        raise failure(500, _RECORDS_UNREAD) from exc
    versions = store.read().get("versions") or []
    engines = service.engines()

    def build(one: records_lib.Query) -> dict[str, Any]:
        return records_lib.dataset(
            rows[journal_lib.TRADE],
            one,
            versions=versions,
            complete=complete,
            open_pl=records_lib.open_pl_of(engines, one),
            day_rows=rows[journal_lib.DAY],
            order_rows=rows[journal_lib.ORDER],
        )

    return records_lib.with_by_currency(build, ask)


def _acted(action: Any, *args: Any) -> Any:
    try:
        return action(*args)
    except StrategyRunError as exc:
        raise _refused(exc) from exc
    except OSError as exc:
        logger.warning("실행 상태를 담지 못했습니다: %s", exc)
        raise failure(500, _RUN_NOT_SAVED) from exc


def _refused(exc: StrategyRunError) -> HTTPException:
    """거절 하나를 화면이 읽는 모양으로.

    짚을 칸이 있으면 **저장할 때와 같은 모양**(`{message, problems}`)입니다 —
    화면이 두 자리를 한 갈래로 읽습니다. 없는 실행은 404, 나머지는 400.
    """
    if str(exc) == WHY_NOT_FOUND:
        return failure(404, str(exc))
    if exc.problems:
        return failure(400, NOT_STARTED, exc.problems)
    return failure(400, str(exc))


def _now() -> int:
    return int(now_kst().timestamp() * 1000)


# ── 계좌 손실 한도 ────────────────────────────────────────


def get_limits() -> LimitStore:
    """앱이 쓰는 한도. 테스트는 이 의존성을 바꿔 끼웁니다."""
    return default_limits


class LimitsRequest(BaseModel):
    """통화마다의 한도(`KRW`·`USD`·`HKD`… 열쇠). 시장 이름 열쇠 `domestic`·`overseas` 도 받습니다.

    아무 열쇠도 안 보내면 모의 쪽 한도는 **담긴 그대로**입니다 — 실전 한도만
    고치는 요청이 그렇습니다(`real` 도 같은 규칙입니다). 담는 규칙은
    `limits.LimitStore.save` 에 있습니다.
    """

    model_config = ConfigDict(extra="allow")

    domestic: dict[str, Any] | None = None
    overseas: dict[str, Any] | None = None
    #: 실전 계좌의 한도(`{domestic, overseas}`). **안 보내면 실전 한도는 그대로**
    #: 둡니다 — 모의 쪽만 고치는 화면이 실전 한도를 풀지 않습니다.
    real: dict[str, Any] | None = None


@runs_router.get("/limits")
def read_limits(store: LimitStore = Depends(get_limits)) -> dict[str, Any]:
    """계좌 전체 손실 한도. 안 건 시장은 `null` 입니다.

    `limits` 는 모의 계좌·앱 시뮬레이션 실행의 한도이고, `real` 은 실전 계좌
    실행의 한도입니다(모양은 같습니다).
    """
    made = store.read(limits.PAPER)
    return {"limits": made, "real": store.read(limits.REAL), "store": store.state.payload}


@runs_router.put("/limits")
def save_limits(
    ask: LimitsRequest, store: LimitStore = Depends(get_limits)
) -> dict[str, Any]:
    """한도를 담습니다. **전략 저장과 같은 규약으로 거릅니다** — 400 + 문제 목록."""
    body = ask.model_dump()
    real = body.pop("real", None)
    values = {name: row for name, row in body.items() if row is not None}
    found = limits.problems(values)
    if isinstance(real, dict):
        found += [
            {**one, "field": f"real.{one['field']}"}
            for one in limits.problems({k: v for k, v in real.items() if v is not None})
        ]
    elif real is not None:
        found.append({"field": "real", "severity": validate.ERROR, "message": limits.WHY_MARKET})
    if found:
        raise failure(400, _LIMITS_NOT_SAVED, found)
    try:
        # 모의 쪽 한도도 실전 쪽과 같은 규칙입니다 — **시장을 하나도 안 보냈으면
        # 담긴 그대로** 둡니다. 실전 한도만 고치는 요청이 모의 한도를 풀지 않습니다.
        # 하나라도 보냈으면 지금처럼 안 보낸 시장은 안 건 것입니다.
        made = {
            "limits": store.save(values, limits.PAPER) if values else store.read(limits.PAPER)
        }
        # 실전 한도는 **늘 싣습니다** — 안 보냈으면 담긴 그대로입니다. 답 하나로
        # 두 계좌의 한도를 다시 그릴 수 있게 합니다.
        made["real"] = (
            store.save(real, limits.REAL) if isinstance(real, dict) else store.read(limits.REAL)
        )
        return made
    except OSError as exc:
        logger.warning("손실 한도를 담지 못했습니다: %s", exc)
        raise failure(500, _LIMITS_NOT_SAVED) from exc
