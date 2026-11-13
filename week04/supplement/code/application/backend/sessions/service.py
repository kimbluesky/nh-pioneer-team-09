"""시간표를 받아 두는 자리. 앱에 하나입니다.

거래시간은 하루에 한 번이면 충분합니다. 국내는 사실상 안 바뀌고, 해외는 서머타임이
바뀌는 날 값이 바뀝니다 — NH선물이 한국시간으로 환산해 주므로 이 앱은 **날마다 다시
받기만** 하면 됩니다. 기준은 **한국시간 07:00** 입니다. 해외 거래일이 그 언저리에서
바뀌고, 그 전에 받은 표는 그날 것이 아닐 수 있습니다.

받지 못하면 **받아 둔 것을 그대로 씁니다.** 시간표가 없다고 화면이 멈추면 안 됩니다.
받아 둔 것도 없으면 그때는 실패를 그대로 올리고, 판정은 내장 표(`judge`)로 갑니다.

**시간표는 실전 서버에서 받습니다.** 모의 서버는 거래소·품목이 빠져 올 수 있고,
장 운영 시간은 실제 시장의 값이기 때문입니다. 실전 인증 정보가 없는 시장만 모의
서버에서 받고, 어느 쪽에서 받았는지는 표의 `environment` 에 적습니다. 화면에서 고른
환경과 무관한 한 벌이라, 환경을 바꿔도 다시 받지 않습니다.

해외 장운영정보는 한 번에 100줄까지만 옵니다. 전체를 한 번에 물으면 두 쪽이 되어
이어 받아야 합니다. 그래서 **품목그룹 일곱으로 나눠** 받습니다 — 그룹마다 100줄
아래이고, 재생 모드의 저장해 둔 응답도 이 그룹 단위로 있습니다.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, time, timedelta
from typing import Any

from backend.config import ConfigError
from backend.domestic_futureoption import domestic_futureoption_functions as domestic_api
from backend.domestic_futureoption import domestic_hours
from backend.net_failure import unreachable
from backend.nh_api.client import TradeClient
from backend.nh_api.reference import reference_environment
from backend.nh_api.runtime import client as default_client
from backend.overseas_futureoption import overseas_futureoption_functions as overseas_api
from backend.overseas_futureoption import overseas_hours
from backend.sessions.hours import KST, iso, now_kst

logger = logging.getLogger(__name__)

DOMESTIC = "domestic"
OVERSEAS = "overseas"

#: 그 시장의 시간표를 못 받았을 때 판정이 대신 쓰는 것. 기록에 함께 적습니다 —
#: 「못 받았다」만 적으면 읽는 사람은 그 뒤에 무슨 일이 일어나는지 모릅니다.
FALLBACK = {
    DOMESTIC: "내장 표로 판정합니다",
    OVERSEAS: "거래시간을 「모름」으로 답합니다",
}

#: 하루의 시간표가 새로워지는 시각(한국시간). 이 시각 전에 받은 표는 그날 것이 아닙니다.
REFRESH_AT = time(7, 0)

#: 이어 받기 상한. 잘못된 키로 끝없이 도는 것을 막습니다.
MAX_PAGES = 10

#: 새로 받기에 실패한 뒤 다시 가 보기까지의 시간. 요청마다 NH선물을 두드리지 않습니다.
RETRY_AFTER = timedelta(minutes=10)

class SessionService:
    """시간표 두 벌(국내·해외)을 들고 있습니다. 한 번에 하나만 받도록 잠급니다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tables: dict[str, dict[str, Any]] = {}
        self._fetched_at: dict[str, datetime] = {}
        # 마지막으로 받기에 실패한 시각과 그 실패. 잠시 동안은 다시 가지 않고 이것을 답합니다.
        self._failed: dict[str, tuple[datetime, Exception]] = {}

    # ── 읽기 ────────────────────────────────────────────────

    def table(
        self, market: str, *, api: TradeClient | None = None, now: datetime | None = None
    ) -> dict[str, Any]:
        """그 시장의 시간표. 낡았으면 새로 받고, 못 받으면 받아 둔 것을 씁니다.

        Raises:
            NHApiError 계열: 받지 못했고 받아 둔 것도 없는 경우.
        """
        now = now or now_kst()
        if not self.stale(market, now):
            return self._kept(market)
        with self._lock:
            if not self.stale(market, now):
                return self._kept(market)
            try:
                fresh = self._fetch(market, api)
            except Exception as exc:
                kept = self._tables.get(market)
                if kept is None and isinstance(exc, ConfigError):
                    # 인증 정보가 없어 부르지도 않은 것입니다. 기억하지 않습니다 —
                    # 설정을 채우면 다음 요청이 기다리지 않고 바로 받아 가야 합니다.
                    raise
                self._failed[market] = (now, exc)
                if kept is None:
                    raise
                logger.warning(
                    "%s 시간표를 새로 받지 못했습니다(%s). 받아 둔 표를 그대로 씁니다.",
                    market,
                    exc,
                    exc_info=not unreachable(exc),
                )
                return kept
            self._failed.pop(market, None)
            fresh["asOf"] = iso(now)
            self._tables[market] = fresh
            self._fetched_at[market] = now
            return fresh

    def _kept(self, market: str) -> dict[str, Any]:
        """받아 둔 표. 없으면 방금 실패한 그 실패를 다시 올립니다 — 다시 가지 않습니다."""
        kept = self._tables.get(market)
        if kept is not None:
            return kept
        _, exc = self._failed[market]
        raise exc

    def cached(self, market: str) -> dict[str, Any] | None:
        """받아 둔 시간표. 없으면 `None`. 판정이 씁니다 — 판정은 받으러 가지 않습니다."""
        return self._tables.get(market)

    def stale(self, market: str, now: datetime | None = None) -> bool:
        """다시 받아야 하는지. 오늘 07:00(한국시간) 전에 받은 것이면 낡은 것입니다.

        방금 받기에 실패했으면 잠시는 낡지 않은 것으로 봅니다 — 그동안은 받아 둔 것
        (없으면 실패)을 그대로 답하고 NH선물에 다시 가지 않습니다.
        """
        now = now or now_kst()
        failed = self._failed.get(market)
        if failed is not None and now - failed[0] < RETRY_AFTER:
            return False
        fetched = self._fetched_at.get(market)
        if fetched is None or market not in self._tables:
            return True
        return fetched < _boundary(now)

    def refresh_all(self, *, api: TradeClient | None = None) -> None:
        """두 시장을 받아 둡니다. 실패는 기록만 남깁니다 — 앱은 그대로 뜹니다."""
        for market in (DOMESTIC, OVERSEAS):
            try:
                self.table(market, api=api)
            except ConfigError as exc:
                # 실전·모의 어디에도 그 시장의 인증 정보가 없는 것입니다 — 국내만 쓰는
                # 사람에게는 늘 있는 일이라 스택 없이 한 줄만 남깁니다.
                logger.warning(
                    "%s 시간표를 받지 않습니다: %s. %s.", market, exc, FALLBACK[market]
                )
            except Exception as exc:
                # 서버에 닿지도 못한 것이면 한 줄이면 됩니다 — 까닭은
                # `backend/net_failure.py` 첫머리에.
                logger.warning(
                    "%s 시간표를 받지 못했습니다(%s). %s.",
                    market,
                    exc,
                    FALLBACK[market],
                    exc_info=not unreachable(exc),
                )

    def reset(self) -> None:
        """받아 둔 것을 버립니다. 테스트가 씁니다."""
        with self._lock:
            self._tables.clear()
            self._fetched_at.clear()
            self._failed.clear()

    # ── 받기 ────────────────────────────────────────────────

    def _fetch(self, market: str, api: TradeClient | None) -> dict[str, Any]:
        if market not in (DOMESTIC, OVERSEAS):
            raise ValueError(f"모르는 시장입니다: {market!r}")
        api = api or default_client()
        # 받은 환경이 응답 헤더에 남지 않게 합니다 — `backend.nh_api.reference` 첫머리.
        with reference_environment(api, market) as environment:
            if market == DOMESTIC:
                made = domestic_hours.product_detail(
                    domestic_api.product_detail(api=api, environment=environment)
                )
            else:
                made = overseas_hours.market_operations(self._overseas_pages(api, environment))
        made["environment"] = environment
        return made

    @staticmethod
    def _overseas_pages(api: TradeClient, environment: str) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        for group in overseas_hours.PRODUCT_GROUPS:
            next_key = ""
            for _ in range(MAX_PAGES):
                raw = overseas_api.market_operations(
                    prd_grp_cd=group, next_key=next_key, api=api, environment=environment
                )
                pages.append(raw)
                next_key = str(raw.get("nxt_key") or "").strip()
                if not next_key:
                    break
        return pages


def _boundary(now: datetime) -> datetime:
    """`now` 이전의 가장 가까운 07:00(한국시간)."""
    local = now.astimezone(KST)
    today = datetime.combine(local.date(), REFRESH_AT, KST)
    return today if local >= today else today - timedelta(days=1)


#: 앱이 쓰는 하나.
service = SessionService()
