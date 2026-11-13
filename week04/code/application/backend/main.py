"""NH선물 Open API 기반 트레이딩 플랫폼 백엔드 진입점.

스캐폴딩 단계이므로 실제 NH선물 Open API 연동 없이 최소 실행 가능한 FastAPI 서버만 제공합니다.

API 엔드포인트는 모두 `/api` 하위에 둡니다. 프론트엔드를 `just build`로 빌드하면
`backend/static/`에 산출물이 생성되며, 해당 폴더가 존재하는 경우 `/` 경로에서 함께 서빙됩니다.
"""
# 회사 망처럼 HTTPS 를 중간에서 다시 서명하는 곳에서는 Python 에 딸린 인증서 목록만으로는
# NH선물 서버의 인증서를 믿지 못해 토큰 발급·조회·실시간 연결이 전부 실패합니다. 운영체제의
# 인증서 저장소를 쓰게 합니다 — 검증을 끄는 것이 아니라 믿을 목록을 바꾸는 것입니다.
# 다른 모듈이 SSL 문맥을 만들기 전에 해야 하므로 어떤 백엔드 모듈보다 먼저 둡니다.
import truststore

truststore.inject_into_ssl()

import asyncio
import logging
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Response
from backend.ui_static import UiStaticFiles
from pydantic import BaseModel, Field

from backend.auth.auth_functions import AuthError, revoke_token
from backend.config import (
    ENVIRONMENT_LABELS,
    ENVIRONMENT_PATTERN,
    Config,
    ConfigError,
    config_view,
    load_config,
    save_default_environment,
    save_markets,
)
from backend.domestic_futureoption.domestic_routes import router as domestic_router
from backend.master import master_daily, master_service
from backend.master.master_routes import router as master_router
from backend.auth import token_store
from backend.nh_api import runtime as nh_runtime
from backend.nh_api.session import REPLAY, current_mode
from backend.overseas_futureoption.overseas_routes import router as overseas_router
from backend.realtime import hub as realtime_hub
from backend.realtime.realtime_routes import router as stream_router
from backend.sessions import service as session_service
from backend.sessions.hours import KST, now_kst
from backend.sessions.sessions_routes import router as session_router
from backend.strategy import pump as strategy_pump
from backend.strategy import service as strategy_service
from backend.strategy import store as strategy_store
from backend.strategy import spread as spread_service
from backend.strategy.spread_routes import router as spread_router
from backend.strategy.rotation_routes import router as rotation_router
from backend.strategy.rotation_runtime import RotationRuntime, service as rotation_service
from backend.strategy.strategy_routes import router as strategy_router
from backend.strategy.strategy_routes import runs_router as strategy_runs_router
from backend.ui.ui_routes import router as ui_router

#: 백엔드 기록 한 줄의 모양 — 시각, 수준, 어느 모듈이 남겼는지, 내용.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging() -> None:
    """백엔드 기록에 시각·수준·모듈 이름을 붙입니다.

    루트 기록기에 처리기가 이미 있으면(다른 프로그램이 이 모듈을 불러 쓰면서 먼저
    정해 두었으면) 손대지 않습니다. 수준도 바꾸지 않으므로 경고(WARNING) 이상만
    보이고 안내(INFO)는 보이지 않는 기본 동작이 그대로입니다.

    uvicorn 의 기록은 uvicorn 이 자기 처리기로 따로 찍고 루트로 올려 보내지 않으므로
    두 번 찍히지 않습니다.
    """
    logging.basicConfig(format=LOG_FORMAT)


# 개발 서버(`uvicorn backend.main:app`)와 데스크톱 앱(`desktop/main.py`) 모두 이 모듈을
# 불러 쓰므로 여기서 한 번 정하면 두 실행 방식에 같이 적용됩니다.
configure_logging()


def _static_dir() -> Path:
    """프론트엔드 빌드 산출물 경로.

    PyInstaller로 패키징된 경우 데이터 파일이 `sys._MEIPASS` 아래에 풀리므로 경로가 달라집니다.
    """
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "backend" / "static"  # type: ignore[attr-defined]
    return Path(__file__).parent / "static"


STATIC_DIR = _static_dir()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """서버가 뜨고 나서 종목 마스터를 받기 시작합니다.

    **여기서 다 받기를 기다리면 안 됩니다.** 데스크톱 앱은 서버가 요청을 받을 수 있게
    된 뒤에 창을 여는데, 이 자리에서 기다리면 아홉 개를 다 받을 때까지 창이 안 뜹니다.
    그래서 별도 스레드에 던져 두고 바로 넘어갑니다. 화면은 `/api/master/status` 로
    진행 상황을 보고, 다 받으면 그때 종목 목록이 섭니다.

    이미 받아 둔 것이 있으면 그것부터 읽어 두므로, 받는 동안에도 어제 목록으로
    화면이 섭니다.

    전략 실행에 시세를 물리는 펌프도 여기서 붙였다 뗍니다 — 붙을 것이 없으면
    아무 구독도 걸지 않으므로 전략을 안 쓰는 사용자에게는 없는 것과 같습니다.
    """
    # **어느 폴더에 담고 있는지 한 줄 남깁니다.** 실서버로 띄운 앱과 저장해 둔
    # 응답으로 띄운 앱이 다른 폴더를 쓰는데, 로그에 없으면 실행이 안 보일 때
    # 「사라졌나」와 「다른 자리를 보고 있나」를 가릴 수 없습니다.
    logging.getLogger(__name__).info("전략 상태 자리: %s", strategy_store.run_state_dir())
    task = asyncio.create_task(_prepare_then_keep_fresh())
    hours = asyncio.create_task(_prepare_sessions())
    # 전략 실행에 시세를 물리는 펌프. 담아 둔 실행은 전부 중지 상태로 서므로
    # 붙자마자 주문이 나가는 일은 없습니다.
    engine = strategy_pump.RunPump(strategy_service.service)
    await engine.start()
    spreads = spread_service.SpreadRuntime(spread_service.service)
    rotation = RotationRuntime(rotation_service)
    try:
        await spreads.start()
        await rotation.start()
        yield
    finally:
        await rotation.stop()
        await spreads.stop()
        await engine.stop()
        task.cancel()
        hours.cancel()
        # 실시간 연결을 먼저 닫습니다. 구독 해제를 보내고 나가야 서버 쪽에
        # 이 앱의 구독이 남지 않습니다.
        await realtime_hub.reset()
        # 열어 둔 접속을 닫습니다. 데스크톱 앱은 백엔드를 스레드로 띄우므로
        # 프로세스가 끝나기 전에 여기서 정리해 두는 편이 깔끔합니다.
        nh_runtime.reset()


async def _prepare_master() -> None:
    """받아 둔 것을 먼저 읽고, 그다음 새로 받습니다.

    둘 다 파일을 읽고 통신하는 일이라 이벤트 루프를 막지 않도록 스레드에서 돌립니다.
    여기서 실패해도 앱은 계속 돌아갑니다 — 종목 목록만 비어 있고, 화면이
    `/api/master/status` 로 그 사실을 알 수 있습니다.

    **저장해 둔 응답으로 도는 중에는 받으러 나가지 않습니다.** 그 방식은 서버에
    닿지 않는 곳에서 화면을 띄우려는 것이라, 받아 둔 파일만 씁니다 — 없으면 종목
    목록이 비고 화면이 그 사실을 「없음」으로 봅니다.
    """
    try:
        await asyncio.to_thread(master_service.service.reload)
        # 접속 자리를 처음 만들며 설정 파일을 읽을 수 있어 스레드에서 봅니다.
        if await asyncio.to_thread(nh_runtime.replaying):
            logging.getLogger(__name__).info(
                "저장해 둔 응답으로 도는 중이라 종목 마스터를 받지 않습니다. 받아 둔 파일만 씁니다."
            )
            return
        await asyncio.to_thread(master_service.service.refresh)
    except asyncio.CancelledError:
        raise
    except Exception:
        # 아무도 기다리지 않는 작업이라, 여기서 안 남기면 실패가 통째로 사라집니다.
        logging.getLogger(__name__).exception("종목 마스터를 준비하지 못했습니다.")


async def _prepare_then_keep_fresh() -> None:
    """뜰 때 마스터를 받고, 그 뒤로는 하루 한 번 마스터와 시간표를 새로 받습니다.

    데스크톱 앱은 보통 매일 새로 켜므로 뜰 때 받는 것으로 충분하고, 하루 한 번
    갱신은 며칠씩 켜 두는 서버를 위한 것입니다(`backend/master/master_daily.py`).
    뜰 때 받기가 끝난 뒤에 시작해야 두 쪽이 같은 파일을 겹쳐 받지 않습니다.
    """
    await _prepare_master()
    await _keep_fresh()


async def _keep_fresh() -> None:
    """하루 한 번 갱신. 서버가 내려갈 때 취소됩니다."""
    await master_daily.daily.run()


async def _prepare_sessions() -> None:
    """거래시간표를 받아 둡니다. 하루 한 번이면 되는 값이라 뜰 때 한 번 받습니다.

    통신하는 일이라 스레드에서 돌리고, 실패해도 앱은 계속 돌아갑니다 — 국내
    세션 판정은 내장 표로, 해외는 「모름」으로 답합니다. 인증 정보가 없어 접속
    자리를 못 만드는 경우도 같습니다.
    """
    try:
        await asyncio.to_thread(session_service.service.refresh_all)
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.getLogger(__name__).exception("거래시간표를 준비하지 못했습니다.")


app = FastAPI(
    title="NH FUTURES API",
    version="0.1.0",
    lifespan=lifespan,
)

# 라우터는 아래쪽 `app.mount("/")` 보다 **먼저** 등록해야 합니다. 뒤에 등록하면
# 프론트엔드 정적 파일이 `/` 를 차지한 뒤라 배포 모드에서 이 라우트가 가려집니다.
app.include_router(master_router)
app.include_router(ui_router)
app.include_router(domestic_router)
app.include_router(overseas_router)
app.include_router(stream_router)
app.include_router(session_router)
app.include_router(strategy_router)
app.include_router(strategy_runs_router)
app.include_router(spread_router)
app.include_router(rotation_router)


@app.get("/api/health")
def health(response: Response) -> dict[str, object]:
    """서버가 살아 있는지, 어디에 물어보고 있는지, 어느 환경인지 알려줍니다.

    `mode` 가 `replay` 이면 NH선물 서버가 아니라 저장해 둔 응답으로 답하고
    있다는 뜻입니다. 이때 화면에 뜨는 시세와 잔고는 실제 값이 아닙니다.

    `active_environment` 는 지금 요청이 나가는 환경(`paper`·`real`)입니다.
    설정을 읽지 못해 모르면 `null` 이고, 그때는 어떤 요청도 NH선물로 나가지
    못합니다 — 화면은 실전으로 가정하지 말고 「모름」으로 두어야 합니다.
    """
    active = nh_runtime.active_environment()
    _stamp_environment(response, active)
    return {"status": "ok", "mode": current_mode(), "active_environment": active}


class MarketCredentials(BaseModel):
    """한 시장의 인증 정보 중 **사용자가 실제로 고친 항목만** 담습니다.

    빠진 항목은 「안 바꿈」이라 설정 파일의 값이 그대로 남습니다. 화면이 가린
    값을 되돌려 보내 실제 값을 덮어쓰는 일이 생기지 않도록, 애초에 안 보내게
    합니다.
    """

    app_key: str | None = None
    app_secret: str | None = None


class ConfigUpdate(BaseModel):
    """설정 화면이 보내는 부분 갱신. 둘 중 하나는 있어야 합니다.

    `environments` 는 `{환경: {시장: {항목: 값}}}` 로 인증 정보를 고치고,
    `default_environment` 는 활성 환경을 바꿉니다. 둘 다 오면 **인증 정보를
    먼저** 저장하고 그 키로 대상 환경을 검증합니다 — 실전 키를 넣으면서
    실전으로 옮기는 한 번의 저장이 그 순서입니다.
    """

    environments: dict[str, dict[str, MarketCredentials]] = Field(default_factory=dict)
    default_environment: str | None = Field(default=None, pattern=ENVIRONMENT_PATTERN)


NOTHING_TO_SAVE = "바꿀 항목이 없습니다."
NO_CREDENTIALS_FOR = (
    "{label} 환경에 인증 정보가 없어 전환하지 않았습니다. "
    "국내 또는 해외 인증 정보를 먼저 넣어 주세요."
)
SWITCH_IN_PROGRESS = "환경을 바꾸는 중입니다. 잠시 뒤 다시 시도해 주세요."
SAVE_FAILED = (
    "설정을 저장하지 못했습니다. 잠시 뒤 다시 시도해 주세요."
    " 계속되면 설정 폴더에 쓸 수 있는지 확인해 주세요."
)
#: 보낸 항목 이름을 못 알아본 자리. **다음 걸음을 안 적습니다** — 다시 눌러도 같은
#: 답이 오고, 고칠 것은 화면 쪽에 있어 사용자가 할 수 있는 일이 없습니다.
SAVE_REJECTED = "설정을 저장하지 못했습니다."
REPLAY_NO_SAVE = "저장해 둔 응답으로 도는 중에는 인증 정보를 저장하지 않습니다."


@app.get("/api/config")
def read_config(response: Response) -> dict[str, object]:
    """설정 화면이 보여줄 인증 정보 상태와 활성 환경.

    값 자체는 나가지 않습니다 — 채워져 있는지와 가린 표시만 보냅니다.

    `default_environment` 는 **파일**에 적힌 다음 실행 기본 환경이고,
    `active_environment` 는 서버가 **지금** 쓰는 환경입니다. 전환하면 둘을 함께
    바꾸므로 보통 같고, 실행 중 파일을 손으로 고쳤을 때만 갈립니다. 화면의
    배지와 판정은 `active_environment` 를 따릅니다.
    """
    try:
        view = config_view()
    except ConfigError as exc:
        # 원본 예외 문구를 그대로 내보내지 않습니다. YAML 파서는 오류 난 줄을
        # 인용해서 알려주는데, 그 줄에 인증 정보가 들어 있을 수 있습니다.
        raise HTTPException(
            status_code=500,
            detail="설정 파일을 읽지 못했습니다. 형식이 올바른지 확인해 주세요.",
        ) from exc
    active = nh_runtime.active_environment()
    _stamp_environment(response, active)
    view["active_environment"] = active
    return view


@app.put("/api/config")
async def write_config(update: ConfigUpdate, response: Response) -> dict[str, object]:
    """인증 정보를 저장하고, 활성 환경을 바꿉니다.

    인증 정보는 바뀐 항목만 설정 파일에 쓰고, 그 자리의 토큰 발급을 한 번
    시도합니다. 발급에 실패해도 저장은 남깁니다. 값을 넣어 두고 나중에 고치는
    순서가 자연스럽고, 저장이 안 되면 사용자가 열두 칸을 다시 채워야 합니다.
    저장한 뒤에는 **접속 자리가 새 설정을 읽게** 합니다 — 안 그러면 앱을 다시
    켜기 전까지 옛 키로 발급받아 거절당합니다.

    환경 전환은 대상 환경에 인증 정보가 있을 때만 받고, 파일 → 접속 자리 →
    실시간 연결 순으로 바꾼 뒤 활성 환경을 답합니다. 200 이 아니면 활성 환경은
    그대로입니다. 이전 환경의 미체결 주문은 건드리지 않습니다.

    저장해 둔 응답으로 도는 재생 모드에서는 인증 정보 저장을 **거절**합니다(409).
    환경 전환만 온 요청은 파일을 안 건드리므로 그대로 받습니다.

    파일 쓰기와 토큰 발급은 시간이 걸리는 일이라 스레드에서 돌립니다. 실시간
    연결을 갈아 끼우는 일은 이 루프에서 해야 하므로 이 라우트는 비동기입니다.
    """
    if not update.environments and update.default_environment is None:
        raise HTTPException(status_code=400, detail=NOTHING_TO_SAVE)

    checks: list[dict[str, object]] = []
    if update.environments:
        if current_mode() == REPLAY:
            # 저장해 둔 응답으로 도는 중에는 사용자 설정 파일을 건드리지 않습니다 —
            # 개발용 실행이 진짜 인증 정보를 덮으면 안 됩니다. 「저장했다」고 답하며
            # 안 쓰는 것은 거짓이라 거절로 답합니다. 환경 전환과 같은 잣대입니다.
            raise HTTPException(status_code=409, detail=REPLAY_NO_SAVE)
        checks = await asyncio.to_thread(_save_credentials, update)

    if update.default_environment is not None:
        await _switch_environment(update.default_environment)

    active = nh_runtime.active_environment()
    _stamp_environment(response, active)
    return {"saved": True, "checks": checks, "active_environment": active}


def _save_credentials(update: ConfigUpdate) -> list[dict[str, object]]:
    """인증 정보를 파일에 쓰고, 접속 자리에 새 설정을 넣고, 발급을 시도합니다."""
    updates = {
        env: {
            market: creds.model_dump(exclude_none=True)
            for market, creds in markets.items()
        }
        for env, markets in update.environments.items()
    }

    # 바뀌기 전 설정. 키가 바뀐 자리의 옛 토큰은 **그 토큰을 받은 키**로 폐기합니다.
    before = _config_before_save()
    try:
        save_markets(updates)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=SAVE_REJECTED) from exc
    except Exception as exc:  # noqa: BLE001 — 파일 파싱·쓰기 실패를 함께 다룹니다.
        # 여기서도 원본 문구를 안 내보냅니다 — 위와 같은 이유입니다.
        raise HTTPException(status_code=500, detail=SAVE_FAILED) from exc

    _revoke_replaced(before, updates)
    _forget_tokens(updates)
    if not _reload_session():
        # 접속 자리가 옛 설정을 든 채입니다. 그 키로 확인하면 방금 비운 자리에
        # 「확인됨」이 서므로 확인하지 않습니다.
        return []
    return _check_credentials(updates)


def _reload_session() -> bool:
    """접속 자리가 방금 저장한 설정을 읽게 합니다. 활성 환경은 그대로입니다.

    파일에 쓸 수 있는 인증 정보가 하나도 안 남았으면(모든 키를 비운 저장) 접속
    자리는 옛 설정을 그대로 들고 다음 저장에서 다시 읽습니다 — 그때 `False`.
    """
    try:
        nh_runtime.reload_config()
    except ConfigError:
        logging.getLogger(__name__).warning("저장한 설정을 접속 자리에 넣지 못했습니다.")
        return False
    return True


async def _switch_environment(target: str) -> None:
    """활성 환경을 바꿉니다. 200 으로 돌아가지 않으면 활성 환경은 그대로입니다.

    차례가 뜻입니다. **검증 → 파일 → 접속 자리 → 실시간.** 실패할 수 있는
    일(파일 쓰기)을 먼저 하고, 실패하지 않는 일(설정 갈아 끼우기)을 그 뒤에
    두어, 파일과 접속 자리가 서로 다른 환경을 말하는 상태를 남기지 않습니다.
    실시간 연결은 자기 힘으로 다시 붙으므로 여기서 기다리지 않고, 갈아 끼우다
    실패해도 기록만 남깁니다 — 파일과 접속 자리는 이미 새 환경이라 여기서
    실패로 답하면 화면이 옛 환경으로 되돌아가 서버와 어긋납니다.

    바꾸는 동안 주문 라우트는 주문을 받지 않습니다(`orders.refusal`).

    **돌고 있는 전략 실행이 있으면 아예 바꾸지 않습니다** — 까닭은
    `backend/strategy/service.py` 의 `running()` 에.
    """
    if not nh_runtime.begin_switch():
        raise HTTPException(status_code=409, detail=SWITCH_IN_PROGRESS)
    try:
        if target == nh_runtime.active_environment():
            return

        # 돌고 있는 전략 실행이 있으면 바꾸지 않습니다. 모의와 실전은
        # 다른 계좌라 환경이 바뀌면 시세도 장부도 다른 것이 되는데, 판정 중인
        # 실행은 그 경계를 모른 채 이어 갑니다.
        busy = await asyncio.to_thread(strategy_service.service.running)
        busy += await asyncio.to_thread(spread_service.service.legs.running)
        if busy:
            raise HTTPException(
                status_code=400, detail=strategy_service.WHY_SWITCH.format(count=len(busy))
            )

        try:
            config = await asyncio.to_thread(_config_in_use)
            if not config.environment(target).configured_markets:
                raise ConfigError(target)
        except ConfigError as exc:
            raise HTTPException(
                status_code=400,
                detail=NO_CREDENTIALS_FOR.format(label=ENVIRONMENT_LABELS[target]),
            ) from exc

        if current_mode() != REPLAY:
            # 저장해 둔 응답으로 도는 중에는 파일을 안 건드립니다. 개발용 실행이
            # 사용자 설정을 바꾸면 안 됩니다.
            try:
                await asyncio.to_thread(save_default_environment, target)
            except Exception as exc:  # noqa: BLE001 — 파일 파싱·쓰기 실패를 함께 다룹니다.
                raise HTTPException(status_code=500, detail=SAVE_FAILED) from exc

        await asyncio.to_thread(nh_runtime.activate, target)
        try:
            await realtime_hub.hub().reconnect()
        except Exception:  # noqa: BLE001 — 연결은 스스로 다시 붙습니다. 전환은 이미 끝났습니다.
            logging.getLogger(__name__).exception("환경을 바꾼 뒤 실시간 연결을 다시 열지 못했습니다.")
    finally:
        nh_runtime.end_switch()


def _config_in_use() -> Config:
    """전환을 검증할 설정. 접속 자리가 든 것이고, 아직 없으면 그 자리를 만들며 읽습니다.

    Raises:
        ConfigError: 실서버 모드인데 설정 파일을 읽을 수 없는 경우.
    """
    return nh_runtime.client().session.config


def _stamp_environment(response: Response, active: str | None) -> None:
    if active:
        response.headers[nh_runtime.ENVIRONMENT_HEADER] = active


#: 옛 토큰 폐기의 시간 제한(초). 폐기는 저장의 곁일이라 오래 기다리지 않습니다.
REVOKE_TIMEOUT = 3.0


def _config_before_save() -> Config | None:
    """저장하기 전의 설정. 접속 자리가 든 것을 먼저, 없으면 파일을 읽습니다.

    둘 다 없으면(인증 정보가 하나도 없던 파일) `None` — 폐기할 옛 토큰도 없습니다.
    """
    session = nh_runtime.held_session()
    if session is not None:
        return session.config
    try:
        return load_config()
    except ConfigError:
        return None


def _revoke_replaced(before: Config | None, updates: dict) -> None:
    """APP Key 가 바뀐 자리의 옛 토큰을 NH선물에 폐기합니다.

    **바뀐 자리만**입니다 — 새 키가 옛 키와 다르거나, 그 시장을 비운 경우. 키가
    같으면(Secret 만 바꿈 포함) 부르지 않습니다. 폐기는 **이미 받아 둔 토큰만**
    대상입니다 — 메모리나 토큰 파일에 없으면 아무것도 부르지 않고, 폐기하려고
    새로 발급받지도 않습니다.

    **저장을 막지 않습니다.** 폐기가 거절되거나 시간을 넘겨도 기록만 남깁니다 —
    그 토큰은 뒤이어 메모리와 파일에서 버려져 다시 쓰이지 않습니다.

    앱을 끌 때와 환경을 바꿀 때는 폐기하지 않습니다. 토큰 파일은 같은 컴퓨터의
    다른 실행과 함께 쓰는 자리라, 한쪽이 끄며 폐기하면 다른 쪽의 토큰이 죽습니다.
    """
    session = nh_runtime.held_session()
    if before is None or session is None or session.is_replay:
        return
    store = token_store.TokenStore()
    for env_name, markets in updates.items():
        for market_name, fields in markets.items():
            try:
                old = before.environment(env_name).market(market_name)
            except (ConfigError, ValueError):
                continue
            new_key = fields.get("app_key", old.app_key)
            if new_key == old.app_key:
                continue
            token = session.tokens.held(
                market_name, env_name, app_key=old.app_key, store=store
            )
            if token is None:
                continue
            try:
                revoke_token(
                    old,
                    before.environment(env_name).base_url,
                    token,
                    client=session.client,
                    timeout=REVOKE_TIMEOUT,
                )
            except Exception:  # noqa: BLE001 — 거절·시간 초과·연결 실패 모두 저장을 막지 않습니다.
                logging.getLogger(__name__).warning(
                    "%s 의 옛 접속 토큰을 폐기하지 못했습니다.", old.label
                )


class ReissueRequest(BaseModel):
    """토큰 다시 받기. 지금 쓰는 환경의 한 시장입니다."""

    environment: str = Field(pattern=ENVIRONMENT_PATTERN)
    market: str = Field(pattern=r"^(domestic|overseas)$")


REPLAY_NO_REISSUE = "저장해 둔 응답으로 도는 중에는 토큰을 다시 받지 않습니다."
REISSUE_NOT_ACTIVE = "지금 쓰는 환경의 토큰만 다시 받습니다. 환경을 먼저 바꿔 주세요."
REISSUE_NO_KEY = "{label} 인증 정보가 없어 토큰을 받을 수 없습니다."
REISSUE_BUSY = "그 시장의 토큰을 받는 중입니다. 잠시 뒤 다시 시도해 주세요."
REISSUE_REFUSED = "토큰을 새로 받지 못했습니다. 인증 정보를 확인해 주세요."
REISSUE_UNREACHABLE = "토큰을 새로 받지 못했습니다. 서버에 닿지 않습니다. 잠시 뒤 다시 시도해 주세요."

#: 다시 받는 중인 (환경, 시장). 같은 자리를 겹쳐 부르면 폐기와 발급이 두 번 나갑니다.
_reissuing: set[tuple[str, str]] = set()
_reissuing_lock = threading.Lock()


@app.post("/api/auth/reissue")
async def reissue_token(ask: ReissueRequest, response: Response) -> dict[str, object]:
    """접속 토큰을 **폐기하고 같은 키로 새로 받습니다.** 그 시장의 실시간도 다시 붙습니다.

    해외/LME 실시간 시세 이용 내역을 바꾼 뒤 부르는 자리입니다 — 명세는 그때
    기존 토큰을 폐기하고 새로 받아야 바뀐 종목이 반영된다고 적고 있습니다.

    **같은 컴퓨터에서 켠 다른 실행이 쓰던 토큰도 함께 폐기됩니다.** 토큰 파일을
    함께 쓰기 때문이고, 그쪽은 다음 요청에서 새로 받습니다.

    옛 토큰 폐기가 거절되거나 시간을 넘겨도 새로 받기는 계속합니다
    (`revokeFailed`). 새 토큰이 옛 토큰과 같으면 `sameToken` 이 참입니다 — 이용
    내역 변경이 아직 반영되지 않았을 수 있습니다. 토큰 글자는 답에 싣지 않습니다.

    지금 쓰는 환경만 받습니다. 저장해 둔 응답으로 도는 중에는 거절합니다(409).
    """
    if current_mode() == REPLAY:
        raise HTTPException(status_code=409, detail=REPLAY_NO_REISSUE)
    if nh_runtime.switching():
        raise HTTPException(status_code=409, detail=SWITCH_IN_PROGRESS)
    try:
        session = nh_runtime.client().session
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=REISSUE_NOT_ACTIVE) from exc
    if session.is_replay:
        raise HTTPException(status_code=409, detail=REPLAY_NO_REISSUE)
    if ask.environment != session.config.default_environment:
        raise HTTPException(status_code=400, detail=REISSUE_NOT_ACTIVE)
    market_config = getattr(session.config.environment(ask.environment), ask.market)
    if not market_config.is_configured:
        raise HTTPException(
            status_code=400, detail=REISSUE_NO_KEY.format(label=market_config.label)
        )

    slot = (ask.environment, ask.market)
    with _reissuing_lock:
        if slot in _reissuing:
            raise HTTPException(status_code=409, detail=REISSUE_BUSY)
        _reissuing.add(slot)
    try:
        try:
            done = await asyncio.to_thread(
                session.tokens.reissue,
                ask.market,
                ask.environment,
                revoke_timeout=REVOKE_TIMEOUT,
            )
        except AuthError as exc:
            # 원문은 기록에도 싣지 않습니다 — 서버가 보낸 값이 되비쳐 올 수 있습니다.
            logging.getLogger(__name__).warning("%s 토큰을 새로 받지 못했습니다.", market_config.label)
            unreachable = isinstance(exc.__cause__, httpx.TransportError)
            raise HTTPException(
                status_code=504 if unreachable else 502,
                detail=REISSUE_UNREACHABLE if unreachable else REISSUE_REFUSED,
            ) from None
        except ConfigError as exc:
            raise HTTPException(
                status_code=400, detail=REISSUE_NO_KEY.format(label=market_config.label)
            ) from exc
    finally:
        with _reissuing_lock:
            _reissuing.discard(slot)

    stream = await realtime_hub.hub().reconnect_market(ask.market)
    _stamp_environment(response, ask.environment)
    return {
        "environment": ask.environment,
        "market": ask.market,
        "reissuedAt": now_kst().isoformat(timespec="seconds"),
        "expiresAt": done.expires_at.astimezone(KST).isoformat(timespec="seconds"),
        "revoked": done.revoked,
        "revokeFailed": done.revoke_failed,
        "sameToken": done.same_token,
        "stream": stream,
    }


def _forget_tokens(updates: dict) -> None:
    """인증 정보가 바뀐 자리의 접속 토큰을 버립니다.

    **옛 키로 받은 토큰은 서버가 안 받습니다.** 메모리에 든 것과 파일에 담아
    둔 것을 함께 버려, 다음 요청이 새 키로 다시 받게 합니다.

    APP Key 만 바뀌었는지 Secret 만 바뀌었는지 가리지 않습니다 — 그 자리를
    건드렸으면 버립니다. 토큰은 다시 받으면 그만이라 잃을 것이 없습니다.
    """
    for env_name, markets in updates.items():
        for market_name in markets:
            token_store.forget(env_name, market_name)
            nh_runtime.forget_token(market_name, env_name)


def _check_credentials(updates: dict) -> list[dict[str, object]]:
    """바뀐 자리마다 토큰 발급을 한 번 시도해 봅니다.

    **앱의 접속 자리를 거칩니다.** 방금 접속 자리에 넣은 새 설정으로 발급받고,
    받은 토큰은 그 자리에 보관되어 첫 조회가 다시 발급받지 않습니다. 저장해 둔
    응답으로 도는 재생 모드에서는 **확인하지 않습니다** — 만들어 낸 토큰으로
    「확인됨」을 내면 거짓이고, NH선물에 진짜 발급을 청하면 개발용 실행이 실서버에
    닿는 것입니다. 접속 자리를 만들 수 없으면(인증 정보가 하나도 없음) 확인할 것이
    없습니다.

    실패 사유는 이 앱이 만든 문구만 내보냅니다. NH선물 서버의 원본 응답을 그대로
    넘기면 그 안에 이 앱이 보낸 값이 되비쳐 올 수 있고, 그러면 브라우저의
    네트워크 탭과 콘솔에 인증 정보가 남습니다.
    """
    try:
        session = nh_runtime.client().session
    except ConfigError:
        return []
    if session.is_replay:
        return []

    checks: list[dict[str, object]] = []
    for env_name, markets in updates.items():
        for market_name in markets:
            try:
                session.tokens.token(market_name, env_name)
            except (AuthError, ConfigError, ValueError):
                checks.append({"environment": env_name, "market": market_name, "ok": False})
                continue
            except Exception:  # noqa: BLE001 — 통신 실패도 「확인 못 함」으로 같이 다룹니다.
                checks.append({"environment": env_name, "market": market_name, "ok": False})
                continue
            checks.append({"environment": env_name, "market": market_name, "ok": True})
    return checks


# 프론트엔드 빌드 산출물이 있으면 정적 파일로 서빙합니다.
# 개발 모드(`just dev`)에서는 Vite 개발 서버가 프론트엔드를 담당하므로 이 폴더가 없어도 됩니다.
if STATIC_DIR.is_dir():
    app.mount("/", UiStaticFiles(directory=STATIC_DIR, html=True), name="static")
