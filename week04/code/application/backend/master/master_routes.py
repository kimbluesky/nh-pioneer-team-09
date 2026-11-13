"""`/api/master` 라우트.

화면은 앱을 켤 때 `/api/master/index` 를 한 번 받아 두고, 그 위에서 종목코드를 풀고
검색합니다. 코드 하나마다 서버에 물어보면 화면이 그리는 도중에 기다리게 되고 호출도
많아집니다.

**시세는 여기 없습니다.** 마스터는 어떤 종목이 상장되어 있는지만 알려 줍니다.
현재가·호가는 따로 조회합니다.
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response

from backend.master import master_daily
from backend.master.master_files import DOMESTIC, OVERSEAS
from backend.master.master_service import MasterService, service
from backend.nh_api import runtime as nh_runtime

router = APIRouter(prefix="/api/master", tags=["master"])

#: 종목 목록을 못 낸 까닭. **화면이 그대로 적습니다**(종목 화면의 안내 줄).
#:
#: 그래서 주소를 적지 않습니다 — 읽는 사람은 이 앱을 쓰는 분이고, 주소를 칠
#: 자리가 없습니다. 같은 화면에 있는 **단추 이름**으로 다음 할 일을 말합니다.
_NO_MASTER = "종목 목록을 아직 받지 못했습니다. 「다시 받기」를 눌러 주세요."
REPLAY_NO_REFRESH = "저장해 둔 응답으로 도는 중에는 종목 마스터를 다시 받지 않습니다."


def get_service() -> MasterService:
    """앱이 쓰는 마스터 자리. 테스트는 이 의존성을 바꿔 임시 폴더를 쓰게 합니다."""
    return service


def get_daily() -> master_daily.DailyRefresh:
    """하루 한 번 갱신의 자리. 테스트는 이 의존성을 바꿔 가짜 시계를 쓰게 합니다."""
    return master_daily.daily


def replaying() -> bool:
    """저장해 둔 응답으로 도는 중인지. 테스트는 이 의존성을 바꿔 끼웁니다.

    판정은 앱이 뜰 때와 같은 자리(`runtime.replaying`)의 것입니다.
    """
    return nh_runtime.replaying()


@router.get("/status")
def read_status(
    master: MasterService = Depends(get_service),
    daily: master_daily.DailyRefresh = Depends(get_daily),
) -> dict[str, object]:
    """마스터의 현재 상태.

    마스터가 없어도 정상으로 답합니다. 「없다」는 것도 화면이 알아야 하는 상태이고,
    여기까지 오류로 만들면 상태를 확인할 방법이 없어집니다.

    `refreshed_at` 은 지금 목록을 만든 시각, `published` 는 그 목록의 국내 배포일,
    `next_refresh_at` 은 하루 한 번 갱신이 다음에 받으러 갈 시각(한국시간)입니다.
    """
    return {**master.status(), **daily.status()}


@router.get("/index")
def read_index(master: MasterService = Depends(get_service)) -> dict[str, object]:
    """화면이 앱을 켤 때 한 번 받아 두는 색인.

    Raises:
        HTTPException: 마스터가 하나도 없는 경우(503). 「없다」와 「찾았는데 0건」을
            가르기 위해 빈 목록이 아니라 오류로 답합니다.
    """
    index = master.index()
    if index is None:
        raise HTTPException(status_code=503, detail=_NO_MASTER)
    return index


@router.get("/products/{code}/contracts")
def read_contracts(
    code: str,
    market: str = Query(description="domestic 또는 overseas"),
    expiry: str = Query(description="만기일 8자리 (20260903) 또는 만기년월 6자리 (202609)"),
    master: MasterService = Depends(get_service),
) -> list[dict[str, str]]:
    """품목 하나의 한 만기월에 걸린 옵션 종목.

    색인에는 옵션의 행사가 행이 없습니다. 체인을 열 때 이 라우트로 받아 갑니다.
    한 만기월이 가장 많을 때도 사천 행 안팎입니다.

    `market` 을 함께 받는 이유는 같은 품목코드가 국내와 해외에 함께 있기 때문입니다.
    코드만으로는 어느 쪽인지 가릴 수 없습니다.
    """
    if market not in (DOMESTIC, OVERSEAS):
        raise HTTPException(
            status_code=400, detail=f"market 은 {DOMESTIC} 또는 {OVERSEAS} 여야 합니다."
        )
    if master.index() is None:
        raise HTTPException(status_code=503, detail=_NO_MASTER)

    return master.contracts(market=market, product_code=code, expiry=expiry)


@router.post("/refresh", status_code=202)
def refresh(
    background: BackgroundTasks,
    response: Response,
    master: MasterService = Depends(get_service),
    replay: bool = Depends(replaying),
) -> dict[str, object]:
    """마스터를 다시 받습니다.

    받는 데 시간이 걸리므로 기다리지 않고 바로 답합니다. 진행 상황은
    `/api/master/status` 의 `stage` 로 봅니다. 저장해 둔 응답으로 도는 중에는
    받으러 나가지 않고 거절합니다 — 앱이 뜰 때와 같은 규칙입니다.
    """
    if replay:
        raise HTTPException(status_code=409, detail=REPLAY_NO_REFRESH)
    if master.busy:
        response.status_code = 409
        return {"started": False, "detail": "이미 받고 있습니다."}

    background.add_task(master.refresh, force=True)
    return {"started": True}
