"""허브와 실행 사이의 펌프.

허브가 내는 시세를 받아 실행에 넣고, 실행이 만든 것을 다시 허브로 내보냅니다.
엔진이 허브를 모르고 허브가 엔진을 모르는 채로 둘을 잇는 자리입니다 — 그래야
봉 하나를 손으로 지어 넣는 시험이 그대로 돌아갑니다.

## 구독은 한 벌로 선언합니다

화면 하나가 붙은 것처럼 **펌프 전체가 허브 손님 하나**(`NAME`)입니다. 실행이
일곱이어도 같은 종목이면 구독은 하나입니다 — 허브의 장부가 같은 종목을 한 번만
겁니다. 실행이 늘거나 줄 때마다 **지금 필요한 것 전체**를 다시 선언합니다(허브가
늘고 줄기를 받지 않고 전체를 받는 까닭은 `ledger` 첫머리에).

## 두 박자로 돕니다

    받기   허브가 보낸 것을 오는 대로 꺼내 그 종목을 보는 실행에 넣습니다.
    내기   `INTERVAL` 마다 `tick_clock` 을 돌려 **체결이 안 오는 것도 하나의
           사건**으로 보고, 실행 상태를 한 묶음으로 만들어 **앞 방송과 달라졌을
           때** 방송합니다(`RunPump._send`).

방송을 체결마다 하지 않는 것은, 한산한 종목과 바쁜 종목이 화면을 다르게 흔들지
않게 하려는 것입니다. 허브의 시세 묶음과 같은 박자입니다.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from backend.nh_api import runtime
from backend.realtime import hub as hub_module
from backend.realtime.feeds import BOOK, DOMESTIC_NIGHT, ORDERS, QUOTE, Feed
from backend.realtime.ledger import DEFAULT_PRIORITY
from backend.realtime.link import LIVE
from backend.sessions.hours import now_kst
from backend.strategy import engine as engine_lib
from backend.strategy import limits as limit_lib
from backend.strategy import runs
from backend.strategy import service as service_lib
from backend.strategy.service import IDLE, RunService

logger = logging.getLogger(__name__)

#: 허브에 붙을 때 쓰는 이름. 화면 이름과 겹치지 않게 한 자리에 둡니다.
NAME = "strategy"

#: 방송 채널. 화면이 `type` 으로 가립니다.
CHANNEL = "strategy"

#: 방송과 시계 확인의 박자(초). 허브의 시세 묶음과 같습니다.
INTERVAL = 0.1

#: 바뀐 것이 없는 묶음을 다시 보내는 간격(밀리초).
#:
#: 방송은 **바뀐 것이 있을 때만** 나갑니다(`RunPump.beat`). 다만 화면 쪽 큐가 차면
#: 가장 오래된 것부터 버려지므로, 마지막 방송 하나가 그렇게 사라지면 화면은 다음
#: 변화까지 옛 모습에 머뭅니다. 그래서 도는 실행이 있는 동안은 이 간격마다 같은
#: 묶음을 한 번 더 보냅니다. 끝난 실행만 남았으면 한 번만 더 보내고 멈춥니다.
RESEND_MS = 5_000

#: 실행 하나가 잡는 구독 둘. 체결이 있어야 봉이 서고, 호가가 있어야 종이 체결이 됩니다.
KINDS = (QUOTE, BOOK)

#: 「체결이 오지 않습니다」 공백이 이만큼(밀리초) 이어지면, 그동안 시장이 흘렀는지
#: 현재가 조회로 한 번 물어봅니다. 조용한 종목은 체결이 한참 없을 수 있어 바로
#: 묻지 않습니다.
PROBE_AFTER = 60_000

#: 같은 (맥락, 종목)을 다시 물어보기까지의 최소 간격(밀리초). 실행이 여럿이어도,
#: 공백이 풀렸다 다시 나도 이 간격 안에서는 한 번입니다 — 시세 조회에는 초당
#: 제한이 있습니다.
PROBE_EVERY = 60_000


class RunPump:
    """허브에 붙어 실행을 돌립니다."""

    def __init__(
        self,
        service: RunService,
        *,
        hub: Any = None,
        interval: float = INTERVAL,
        name: str = NAME,
        limits: Any = None,
        judge: Any = None,
        opening: Any = None,
        probe: Any = None,
        replaying: Any = None,
        closing: Any = None,
        auction: Any = None,
        unknown: Any = None,
    ) -> None:
        self.service = service
        #: 계좌 전체 손실 한도를 읽는 자리.
        self.limits = limits if limits is not None else limit_lib.store
        #: 그 종목이 지금 장중인지 답하는 자리. 바꿔 낄 수 있게 둡니다.
        self.judge = judge if judge is not None else _open_now
        #: 그 종목의 지금 세션이 **언제 열렸는지**(밀리초, 모르면 `None`). 봉 순서의
        #: 기준입니다. 장중 판정을 바꿔 끼운 자리에서는 같이 바꿔 끼우지 않으면
        #: 모른다고 답합니다 — 판정만 지어낸 곳에서 실제 시간표를 읽지 않게 합니다.
        if opening is not None:
            self.opening = opening
        else:
            self.opening = _opening_now if judge is None else _no_opening
        #: 그 종목의 **그 창**이 언제 닫히는지(밀리초, 모르면 `None`). 세션 끝 정리가
        #: 보는 값입니다. 장중 판정을 바꿔 끼운 자리에서는 같이 바꿔 끼우지 않으면
        #: 모른다고 답합니다.
        if closing is not None:
            self.closing = closing
        else:
            self.closing = _closing_now if judge is None else _no_opening
        #: 그 종목의 장 시간표를 모르는지. 「창 밖」과 갈라야 세션 끝 정리를 못 하는
        #: 때를 알립니다. 판정을 갈아 끼운 시험은 늘 안다고 봅니다.
        if unknown is not None:
            self.unknown = unknown
        else:
            self.unknown = service_lib.timetable_unknown if judge is None else _never_unknown
        #: 그 종목의 **그 창**의 지금 또는 다음 단일가(동시호가) 구간. 종이 장부가 체결
        #: 시각과 견주어 그 안이면 채우지 않습니다. 장중 판정을 바꿔 끼운 자리에서는
        #: 같이 바꿔 끼우지 않으면 없다고 답합니다.
        if auction is not None:
            self.auction = auction
        else:
            self.auction = _auction_now if judge is None else _no_auction
        self.name = name
        self.interval = max(float(interval), 0.01)
        self._hub = hub
        self._client: Any = None
        self._tasks: list[asyncio.Task[None]] = []
        #: 마지막으로 선언한 구독. 달라졌을 때만 다시 선언합니다.
        self._declared: dict[Feed, int] = {}
        #: 실행이 직전 확인에 장중이었는지. 나가는 순간을 잡는 데 씁니다.
        self._was_open: dict[str, bool] = {}
        #: 세션·한도를 마지막으로 본 초. 박자마다 보기에는 무거운 일입니다.
        self._checked = 0
        #: 마지막으로 남긴 박자 예외. 같은 것이 이어지면 다시 안 남깁니다.
        self._last_fault = ""
        #: 실행마다 마지막으로 남긴 예외. 같은 것이 이어지면 다시 안 남깁니다.
        self._last_broke: dict[str, str] = {}
        #: 실행마다 마지막으로 **성공한** 값. 못 꺼낸 박자에 그대로 세웁니다.
        self._last_good: dict[tuple[str, str], Any] = {}
        #: 그 실행의 종목을 지금 조회해 누적 거래량을 답하는 자리. `(실행) → 수 | None`.
        #: 바꿔 낄 수 있게 둔 것은, 시험이 통신 없이 이 길을 지나가야 하기 때문입니다.
        self.probe = probe if probe is not None else _market_volume
        #: 저장해 둔 응답으로 도는 중인지 답하는 자리. 그때는 조회가 저장본이라
        #: 시장이 흘렀는지 물어볼 뜻이 없습니다.
        self.replaying = replaying if replaying is not None else runtime.replaying
        #: (맥락, 종목)마다 마지막으로 물어본 시각(밀리초).
        self._probed: dict[tuple[str, str], int] = {}
        #: 마지막으로 보낸 묶음(JSON 문자열)과 그 시각. 같은 것을 박자마다 다시
        #: 보내지 않으려고 둡니다.
        self._sent_key: str | None = None
        self._sent_at = 0
        #: 마지막 변화 뒤 같은 묶음을 한 번 더 보냈는지.
        self._resent = False
        #: 마지막으로 보낼 때 허브가 센 「화면이 붙은 횟수」. 늘었으면 새 화면입니다.
        self._seen_attached: Any = None

    @property
    def hub(self) -> Any:
        """붙을 허브. 안 주면 앱이 쓰는 것입니다."""
        if self._hub is None:
            self._hub = hub_module.hub()
        return self._hub

    # ── 살고 죽기 ───────────────────────────────────────

    async def start(self) -> None:
        """허브에 붙고 두 박자를 돌립니다. 두 번 불러도 하나만 돕니다."""
        if self._tasks:
            return
        self._client = self.hub.attach(self.name)
        await self.declare()
        self._tasks = [
            asyncio.create_task(self._take_forever()),
            asyncio.create_task(self._beat_forever()),
            asyncio.create_task(self._audit_forever()),
        ]

    async def stop(self) -> None:
        """구독을 놓고 나갑니다. **실행은 그대로 둡니다** — 값만 안 들어옵니다."""
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        if self._client is not None:
            await self.hub.detach(self.name)
            self._client = None
        for made in self.service.engines():
            made.feed = IDLE

    async def declare(self) -> None:
        """지금 필요한 구독 **전체**를 다시 선언합니다.

        실행이 늘거나 줄 때마다 부릅니다. 같은 종목을 보는 실행이 둘이어도
        구독은 하나입니다.
        """
        if self._client is None:
            return
        self._declared = self.wishes()
        await self.hub.declare(self.name, self._declared)

    async def sync(self) -> bool:
        """필요한 구독이 달라졌으면 다시 선언합니다. 선언했으면 참입니다.

        실행이 늘거나 줄 때 라우트가 부르지 않고 **박자마다 맞춰 봅니다.**
        라우트는 다른 스레드에서 도는데 선언은 이벤트 루프의 일이라, 부르는
        자리를 늘리면 그 경계를 넘나드는 길이 여럿 생깁니다.
        """
        if self._client is None or self.wishes() == self._declared:
            return False
        await self.declare()
        return True

    def wishes(self) -> dict[Feed, int]:
        """돌고 있는 실행들이 보아야 하는 구독 한 벌.

        계좌 갈래(모의·실전) 실행이 하나라도 있으면 그 계좌의 **주문/체결 알림**을 하나 더
        겁니다 — 계좌 단위라 실행이 몇이든 하나면 됩니다.
        """
        made: dict[Feed, int] = {}
        for run in self.service.watching():
            for kind in KINDS:
                made[Feed(context=run.session, kind=kind, sym=run.code)] = DEFAULT_PRIORITY
            if run.mode in runs.ACCOUNT_MODES:
                made[Feed(context=run.session, kind=ORDERS)] = DEFAULT_PRIORITY
        return made

    # ── 받기 ────────────────────────────────────────────

    async def _take_forever(self) -> None:
        while True:
            payload = await self._client.queue.get()
            try:
                for run_id in self.take(payload, at=_now()):
                    # **조회는 루프 밖에서 합니다.** 통신이고 제한기에서 기다릴
                    # 수 있어, 여기서 그대로 부르면 그동안 다른 실행의 시세가
                    # 큐에 쌓인 채 멈춥니다.
                    await asyncio.to_thread(self.catch_up, run_id)
            except Exception as exc:  # noqa: BLE001 — 한 건 때문에 펌프가 멈추면 안 됩니다.
                # 박자와 같은 자리로 보냅니다 — 같은 것은 한 번만 남고 실행에
                # 이벤트가 답니다. 기록에만 남기면 화면은 값이 안 오는 까닭을
                # 모른 채 「살아 있는데 봉이 안 선다」만 보게 됩니다.
                self._fault(exc, _now())

    def catch_up(self, run_id: str) -> None:
        """다시 붙은 실행 하나를 따라잡게 합니다. 통신이 있어 루프 밖에서 부릅니다.

        먼저 끊긴 동안의 봉을 조회로 다시 받습니다(`service.refill`). 그다음 계좌 갈래
        실행은 **계좌와 맞춰 봅니다** — 끊겨 있던 동안 계좌에서 무슨 일이
        있었는지 이 앱은 모릅니다. 맞으면 대사 전 상태로 돌아가고, 어긋나면 확인을
        기다립니다. 종이 실행은 대사가 할 일이 없어 봉만 다시 받아 이어 갑니다.

        **확인을 기다리는 실행은 봉만 다시 받고 맞춰 보지 않습니다.** 그 상태는
        사용자가 확인해야 풀립니다. 봉은 받아 두어야 확인 뒤 다시 돌 때 끊긴 동안의
        구멍 없이 판정합니다.
        """
        self.service.refill(run_id)
        made = next((one for one in self.service.engines() if one.run.id == run_id), None)
        if made is None or made.run.state == runs.ATTENTION:
            return
        self.service.reconcile(run_id, _now())

    def take(self, payload: dict[str, Any], *, at: int) -> list[str]:
        """허브가 보낸 것 하나를 알맞은 실행에 넣습니다.

        **앞을 다시 채워야 하는 실행의 id 를 냅니다** — 채우는 일 자체는 통신이라
        여기서 하지 않고 부르는 쪽이 루프 밖으로 넘깁니다.
        """
        kind = str(payload.get("type") or "")
        if kind == QUOTE:
            self._quote(payload, at)
        elif kind == BOOK:
            self._book(payload, at)
        elif kind == "status":
            return self._status(payload, at)
        elif kind == "subscriptions":
            self._subscriptions(payload, at)
        elif kind == ORDERS:
            self._orders(payload, at)
        return []

    def _quote(self, payload: dict[str, Any], at: int) -> None:
        context = str(payload.get("context") or "")
        trades = payload.get("trades")
        patch = payload.get("patch")
        volume = patch.get("volume") if isinstance(patch, dict) else None
        for made in self._for(context, payload.get("sym")):
            # 살아 있다고 적을지는 엔진이 정합니다(`engine.RunEngine.mark_live`).
            # 공백에서 돌아오는 것은 엔진이 첫 체결에서 맡습니다.
            made.mark_live()
            if volume is not None:
                made.on_volume(volume)
            if isinstance(patch, dict) and ("upperLimit" in patch or "lowerLimit" in patch):
                # 실시간 상한가·하한가. 종이 장부가 그 값에 닿은 쪽을 채우지 않습니다.
                made.on_limits(patch.get("upperLimit"), patch.get("lowerLimit"))
            took = False
            for one in trades if isinstance(trades, list) else []:
                if isinstance(one, dict):
                    made.on_trade(one, context, at=at)
                    took = True
            if took:
                # 「틱」 실행은 묶음마다 한 번, 진행 중인 봉까지 넣어 판정합니다.
                # 「마감」 실행에서는 아무 일도 하지 않습니다.
                made.judge_tick(at)

    def _book(self, payload: dict[str, Any], at: int) -> None:
        for made in self._for(payload.get("context"), payload.get("sym")):
            made.on_book(payload, at=at)

    def _status(self, payload: dict[str, Any], at: int) -> list[str]:
        """연결 상태. `live` 가 아니면 그 시장의 실행은 공백입니다.

        다시 붙었다는 표지(`refill`)가 오면 **끊긴 동안의 봉을 실시간이 메워
        주지 않으므로** 조회로 다시 받아 이어야 합니다. 여기서는 **누가 채워야
        하는지만** 답하고, 채우는 일은 부르는 쪽이 루프 밖으로 넘깁니다.
        """
        if payload.get("fresh"):
            # 그 시장의 첫 연결이 붙는 중입니다. 받던 시세가 끊긴 것이 아니라
            # 공백도, 다시 채울 봉도 없습니다. 실행은 첫 시세가 올 때 살아납니다.
            return []
        market = str(payload.get("market") or "")
        live = str(payload.get("state") or "") == LIVE
        made: list[str] = []
        for one in self.service.engines():
            if _market_of(one.run.session) != market or one.run.state == runs.ENDED:
                # 끝난 실행은 시세를 보지 않습니다. 공백 줄도, 봉 조회도 없습니다.
                continue
            one.on_status(live, at=at)
            # 확인을 기다리는 실행도 봉은 다시 받습니다. 맞춰 보는 일만 건너뜁니다
            # (`catch_up`).
            if live and payload.get("refill"):
                made.append(one.run.id)
        return made

    def _orders(self, payload: dict[str, Any], at: int) -> None:
        """주문/체결 알림 묶음. **계좌 단위 구독이라 종목이 없습니다.**

        같은 계좌를 보는 실행 모두에 넣고, **주문번호로 제 것만** 골라 받게
        합니다(`sim.SimBroker.on_order`). 실행마다 따로 구독하면 같은 알림을
        일곱 번 받으면서 구독 자리만 먹습니다.
        """
        market = str(payload.get("market") or "")
        events = payload.get("events")
        for one in self.service.engines():
            if _market_of(one.run.session) != market:
                continue
            for event in events if isinstance(events, list) else []:
                if isinstance(event, dict):
                    one.on_order(event, at=at)

    def _subscriptions(self, payload: dict[str, Any], at: int) -> None:
        """구독 답. **밀린 자리가 있으면 그 실행은 시세를 못 받습니다.**

        스무 자리를 나눠 쓰다 밀리면 조용히 값이 안 옵니다 — 화면에는 살아
        있는 것처럼 보이는데 봉이 안 서는 상태가 됩니다. 그 실행을 `idle` 로
        되돌리고 까닭을 남깁니다.

        서버가 등록을 거절한 구독(`failed`)이면 자리 때문이 아니므로 **서버가 보낸
        코드와 글을 그대로** 남깁니다(`refused_text`).
        """
        rejected = payload.get("rejected")
        failed = payload.get("failed")
        if not isinstance(rejected, list):
            return
        missed = {
            (str(one.get("context") or ""), str(one.get("sym") or ""))
            for one in rejected
            if isinstance(one, dict)
        }
        refused: dict[tuple[str, str], str] = {}
        for one in failed if isinstance(failed, list) else []:
            if not isinstance(one, dict):
                continue
            where = (str(one.get("context") or ""), str(one.get("sym") or ""))
            refused.setdefault(where, refused_text(one.get("code"), one.get("message")))
        for one in self.service.engines():
            where = (one.run.session, one.run.code)
            if where not in missed and where not in refused:
                continue
            one.feed = IDLE
            one._say(at, engine_lib.GAP, refused.get(where, NO_ROOM))

    def _for(self, context: Any, sym: Any) -> list[engine_lib.RunEngine]:
        """그 종목·창을 보는 실행들. **끝난 실행은 뺍니다.**

        끝난 실행은 자기 구독이 없지만, 같은 종목을 다른 실행이 보고 있으면 그
        시세가 여기로 옵니다. 넘기면 끝난 실행의 봉·조건 표·곡선이 계속 움직이고
        안 낸 신호가 기록에 쌓여, 살아 있는 실행처럼 보입니다.
        """
        where = str(context or "")
        code = str(sym or "")
        return [
            made
            for made in self.service.engines()
            if made.run.session == where
            and made.run.code == code
            and made.run.state != runs.ENDED
        ]

    # ── 초마다 ──────────────────────────────────────────

    def check(self, at: int, *, every: int = 1000) -> bool:
        """세션 경계와 계좌 한도를 봅니다. **초에 한 번**입니다.

        세션은 **종목마다** 봅니다 — 국내도 품목마다 시간이 다릅니다.

        둘 다 마스터·시간표를 뒤지는 일이라 박자(100밀리초)마다 하기에는
        무겁고, 그만큼 자주 볼 까닭도 없습니다.
        """
        second = at // max(int(every), 1)
        if second == self._checked:
            return False
        self._checked = second
        self._sessions(at)
        self._session_ends(at)
        # 계좌마다 따로 봅니다 — 실전 계좌의 손실은 실전 한도로만 셉니다.
        for environment in limit_lib.ENVIRONMENTS:
            self.service.account_loss(
                limit_lib.daily_losses(self.limits.read(environment)), at, environment=environment
            )
        return True

    def _sessions(self, at: int) -> None:
        """실행이 창 밖으로 나갔는지. 나간 순간에 세션을 닫습니다.

        **종목마다 봅니다.** 국내도 세션 시간이 품목마다 달라(개장 08:45 인 품목과
        09:00 인 품목이 함께 옵니다) 시장 하나의 경계로 닫으면 그중 한쪽은 늘
        어긋납니다 — 열린 봉이 일찍 닫히고 미체결이 먼저 취소됩니다.

        **실행이 도는 창만 봅니다.** 국내는 한 종목이 주간과 야간에 두 번 열려,
        「지금 이 종목이 열려 있나」만 보면 주간 실행이 야간에도 장중으로 잡힙니다 —
        그러면 15:45 에 한 번 닫히고 다음 날 06:00 에 또 닫혀, 거래 한 번 없는
        주간 실행이 야간 끝에 「세션 종료」를 받습니다.

        **허브의 세션 방송으로는 안 닫습니다.** 두 길이 함께 돌면 같은 실행이
        두 번 닫힙니다. 방송은 화면이 쓰는 시장 시계로 남습니다.

        판정은 화면이 쓰는 그 자리(`sessions_routes.judged`)를 그대로 부릅니다 —
        같은 순간에 화면과 엔진이 다른 답을 내면 안 됩니다.
        """
        for made in self.service.engines():
            now_open = self.judge(made.run.market, made.run.code, made.run.session)
            if self._was_open.get(made.run.id) and not now_open:
                made.close_session(at)
            self._was_open[made.run.id] = now_open
            made.set_auction(
                self.auction(made.run.market, made.run.code, made.run.session) if now_open else None
            )
            # 개장 시각은 **거래일마다 한 번** 받습니다. 판정이 무거워 박자마다
            # 부르지 않습니다.
            if now_open and not made.open_known(at, made.run.session):
                since = self.opening(made.run.market, made.run.code, made.run.session)
                if since is not None:
                    made.note_open(made.run.session, since)

    def _session_ends(self, at: int) -> None:
        """세션 끝 정리를 켠 실행이 그 창의 끝 N분 전에 들었는지. 들었으면 한 번 정리합니다.

        끝 시각은 화면이 쓰는 세션 판정의 `until` 입니다 — 품목마다·조기 마감·야간
        자정 넘김을 그 판정이 압니다. 고정 시각을 쓰지 않습니다. 같은 세션에 두 번
        하지 않는 것은 엔진이 지킵니다(`engine.RunEngine.session_end`).
        """
        for made in self.service.engines():
            aux = made.aux
            if not aux.get("sessionEndFlat"):
                continue
            try:
                minutes = int(float(str(aux.get("sessionEndMinutes") or 0)))
            except ValueError:
                continue
            if minutes <= 0:
                continue
            if self.unknown(made.run.market, made.run.code):
                # 장 시간표를 모릅니다. 체결이 들어오는 동안(장이 흐름)만 알립니다 —
                # 장이 닫힌 밤에 매번 남기지 않습니다.
                if made.last_trade_at and at - made.last_trade_at < made.gap_ms:
                    made.note_session_end_unknown(at, service_lib.SESSION_END_UNKNOWN_NOTE)
                continue
            until = self.closing(made.run.market, made.run.code, made.run.session)
            if until is None or not until - minutes * 60_000 <= at < until:
                continue
            made.session_end(at, until)

    # ── 내기 ────────────────────────────────────────────

    async def _beat_forever(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.sync()
                await self.refresh()
                self.beat(at=_now())
                self._last_fault = ""
            except Exception as exc:  # noqa: BLE001 — 한 박자 때문에 펌프가 멈추면 안 됩니다.
                self._fault(exc, _now())

    async def _audit_forever(self) -> None:
        """대사를 기다리는 실행과 거래일이 바뀐 실행을 계좌와 맞춰 봅니다.

        **방송 박자와 따로 돕니다.** 계좌 조회는 초당 제한이 있어 한 건에
        1초 가까이 걸릴 수 있는데, 방송 박자(100밀리초) 안에서 기다리면 그동안
        화면이 아무 값도 못 받습니다.
        """
        while True:
            await asyncio.sleep(self.interval)
            try:
                # 아직 대사를 못 지난 실행이 먼저입니다 — 그 실행은 판정도
                # 주문도 열려 있지 않습니다.
                for run_id in self.service.reconcile_wanted():
                    await asyncio.to_thread(self.service.reconcile, run_id, _now())
                for run_id in self.service.audit_wanted():
                    await asyncio.to_thread(self.service.audit, run_id, _now())
                await self.confirm_orders(_now())
                await self.probe_quiet(_now())
            except Exception as exc:  # noqa: BLE001 — 한 건 때문에 펌프가 멈추면 안 됩니다.
                self._fault(exc, _now())

    async def confirm_orders(self, at: int) -> int:
        """접수 뒤 결과를 확인할 때가 된 계좌 주문들을 확인합니다. 확인한 건수를 냅니다.

        계좌가 접수 답을 준 뒤에 거절하는 주문이 있습니다(`backend/order_outcome.py`).
        조회는 루프 밖에서 하고 계좌 조회의 호출 제한을 지납니다. **저장해 둔 응답으로
        도는 중에는 조회하지 않습니다** — 저장본의 내역은 방금 낸 주문과 상관이 없습니다.
        """
        wanted = self.service.confirm_wanted(at)
        if not wanted:
            return 0
        looking = not self.replaying()
        for run_id, order_no in wanted:
            await asyncio.to_thread(self.service.confirm, run_id, order_no, at, look=looking)
        return len(wanted)

    async def refresh(self) -> int:
        """재개한 실행의 구독을 NH선물 쪽에 다시 겁니다. 다시 건 구독 수를 냅니다.

        장부에는 걸린 채로 남아 있는 구독이라 다시 선언해도 아무것도 나가지
        않습니다. 허브에 **해제했다가 다시 걸어** 달라고 청합니다(`Hub.refresh`).
        """
        feeds: list[Feed] = []
        for made in self.service.engines():
            if not made.refresh_wanted:
                continue
            made.refresh_wanted = False
            feeds.extend(Feed(context=made.run.session, kind=kind, sym=made.run.code) for kind in KINDS)
        rearm = getattr(self.hub, "refresh", None) if self._client is not None else None
        if not feeds or rearm is None:
            return 0
        await rearm(feeds)
        return len(feeds)

    def probes_wanted(self, at: int) -> list[tuple[tuple[str, str], list[engine_lib.RunEngine]]]:
        """시장이 흘렀는지 물어볼 (맥락, 종목)과 그것을 보는 실행들.

        「체결이 오지 않습니다」 공백이 `PROBE_AFTER` 를 넘겼고 이 공백에 아직
        묻지 않은 실행만 셉니다. 같은 종목은 `PROBE_EVERY` 안에 다시 묻지 않습니다.
        """
        groups: dict[tuple[str, str], list[engine_lib.RunEngine]] = {}
        for made in self.service.engines():
            if made.feed != engine_lib.GAP or not made.quiet_since or made.probe_asked:
                continue
            if made.run.state in (runs.ENDED, runs.ATTENTION):
                # 끝난 실행 때문에 시세를 묻지 않습니다(조회 한도를 씁니다). 확인을
                # 기다리는 실행은 구독을 놓은 것이라 체결이 안 오는 것이 당연합니다.
                continue
            if at - made.quiet_since < PROBE_AFTER:
                continue
            groups.setdefault((made.run.session, made.run.code), []).append(made)
        return [
            (key, group)
            for key, group in groups.items()
            if key not in self._probed or at - self._probed[key] >= PROBE_EVERY
        ]

    async def probe_quiet(self, at: int) -> int:
        """체결이 안 오는 실행의 종목을 조회해, 시장이 흘렀으면 끊김으로 알립니다.

        **(맥락, 종목)마다 한 번** 부르고 그 종목을 보는 실행들이 답을 나눠
        씁니다. 조회는 루프 밖에서 하고, 앱의 다른 조회와 같은 호출 제한을
        지납니다. 부른 횟수를 냅니다.

        **흘렀다**는 조회의 누적 거래량이 이 실행이 실시간으로 마지막에 본 누적
        거래량보다 큰 것입니다. 그 뒤에 체결이 있었는데 이 앱에 오지 않았다는
        뜻입니다. 조회가 실패하거나 견줄 값이 없으면 판단하지 않고 기존 문장을
        그대로 둡니다.
        """
        wanted = self.probes_wanted(at)
        if not wanted:
            return 0
        replaying = self.replaying()
        asked = 0
        for key, group in wanted:
            for made in group:
                made.probe_asked = True
            if replaying:
                continue
            self._probed[key] = at
            asked += 1
            try:
                volume = await asyncio.to_thread(self.probe, group[0].run)
            except Exception:  # noqa: BLE001 — 못 물어봤으면 판단하지 않을 뿐입니다.
                logger.warning("%s %s 의 현재가를 받지 못해 끊김을 가리지 못했습니다.", *key, exc_info=True)
                continue
            if not isinstance(volume, (int, float)) or isinstance(volume, bool):
                continue
            for made in group:
                if made.last_volume is not None and volume > made.last_volume:
                    made.note_feed_lost(at)
        return asked

    def _fault(self, exc: Exception, at: int) -> None:
        """박자가 터졌을 때. **같은 것은 한 번만 남깁니다.**

        박자는 초에 열 번입니다. 고쳐지지 않는 예외 하나가 같은 줄을 초당 열
        번씩 쌓으면, 기록에서 다른 일이 묻히고 파일만 자랍니다. 대신 실행에
        이벤트로 한 번 달아 화면이 볼 수 있게 합니다.
        """
        mark = f"{type(exc).__name__}: {exc}"
        if mark == self._last_fault:
            return
        self._last_fault = mark
        logger.exception("실행 상태를 방송하지 못했습니다.")
        for one in self.service.engines():
            # 기록이 아니라 화면에만 답니다(`_broke` 와 같은 까닭).
            one._say(at, engine_lib.GAP, f"{FAULT} {mark}", keep=False)

    def beat(self, *, at: int) -> dict[str, Any] | None:
        """시계를 한 번 돌리고 지금 모습을 냅니다. 실행이 없으면 `None` 입니다.

        **실행마다 따로 꺼냅니다**(`_taken`). 한 실행이 값을 못 내도 묶음은
        나갑니다 — 여기서 멈추면 멀쩡한 실행들의 화면까지 같은 자리에 멎습니다.

        묶음은 늘 만들어 돌려주지만, **화면으로 보내는 것은 바뀐 것이 있을
        때뿐**입니다(`_send`).
        """
        made = self.service.engines()
        if not made:
            # 다음에 실행이 다시 서면 앞 방송과 같아 보여도 보냅니다.
            self._sent_key = None
            return None
        # 이 박자에 값을 하나라도 못 낸 실행. **온전히 다시 낼 때까지** 까닭을
        # 들고 있습니다 — 한 칸이 되고 다른 칸이 안 되는 사이에 알림이 풀리면,
        # 같은 고장이 박자마다 다시 적힙니다.
        broke: set[str] = set()
        # 시계도 실행마다 감쌉니다. 한 실행이 여기서 터지면 값을 꺼내 보기도
        # 전에 묶음이 통째로 안 나갑니다 — 그 실행만 시계를 못 돈 것이 됩니다.
        for one in made:
            if one.run.state == runs.ENDED:
                # 끝난 실행은 시세를 보지 않습니다. 공백도 없습니다. 방송 줄에는 그대로
                # 섭니다 — 목록에서 끝난 실행을 보고 지울 수 있어야 합니다.
                continue
            self._taken(one, "tick", lambda engine: engine.tick_clock(at), at=at, broke=broke)
        self.check(at)
        # **줄은 맨 나중입니다.** 조각 하나를 못 꺼낸 실행은 그 줄에도 「묵었다」를
        # 적어야 하는데, 줄을 먼저 만들면 그 사실이 아직 없습니다. 그러면 화면은
        # 멀쩡한 줄 옆에서 미체결 표만 비어 **「없습니다」로 읽습니다** — 못 받은
        # 것과 없는 것은 다릅니다.
        opens = [self._taken(one, "openOrders", _open_orders, at=at, broke=broke) for one in made]
        fills = [self._taken(one, "fills", _fills, at=at, broke=broke) for one in made]
        events = [self._taken(one, "events", _events, at=at, broke=broke) for one in made]
        rows = [self._row_of(one, at=at, broke=broke) for one in made]
        for one in made:
            if one.run.id not in broke:
                self._last_broke.pop(one.run.id, None)
        payload = {
            "type": CHANNEL,
            "runs": rows,
            "fills": [row for one in fills if one for row in one],
            "events": [row for one in events if one for row in one],
            # **최근 몇 줄이 아니라 지금 열려 있는 것 전부**입니다. 체결·이벤트
            # 는 쌓이는 값이라 화면이 이어 붙이지만, 이 목록은 그 순간의 모습
            # 이라 덮어써야 채워진 주문이 표에서 사라집니다.
            "openOrders": [row for one in opens if one for row in one],
        }
        self._send(payload, made, at)
        return payload

    def _send(self, payload: dict[str, Any], made: list[engine_lib.RunEngine], at: int) -> bool:
        """묶음을 화면으로 보낼지 정하고 보냅니다. 보냈으면 참입니다.

        보내는 때는 셋입니다.

        - **앞 방송과 다를 때.** 줄·체결·이벤트·열린 주문 어느 하나라도 바뀌면
          그 박자에 나갑니다. 화면은 체결·이벤트를 열쇠로 이어 붙이고 줄과 열린
          주문은 덮어쓰므로, 같은 묶음을 다시 받아도 달라지는 것이 없습니다.
        - **새 화면이 붙었을 때.** 그 화면은 앞 방송을 받지 못했습니다.
        - **같은 묶음이 `RESEND_MS` 동안 이어졌을 때.** 화면 쪽 큐에서 버려진
          방송을 메우는 자리입니다. 끝난 실행만 남았으면 한 번으로 그칩니다 —
          끝난 실행은 다시 바뀌지 않는데 계속 보내면, 전략 탭을 쓰지 않는
          화면까지 시세와 같은 큐에서 자리를 내줍니다.
        """
        key = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        attached = getattr(self.hub, "attached", None)
        if key != self._sent_key or attached != self._seen_attached:
            self._resent = False
        elif at - self._sent_at < RESEND_MS:
            return False
        elif self._resent and all(one.run.state == runs.ENDED for one in made):
            return False
        else:
            self._resent = True
        self._sent_key = key
        self._sent_at = at
        self._seen_attached = attached
        self.hub.publish(payload)
        return True

    def _row_of(
        self, made: engine_lib.RunEngine, *, at: int, broke: set[str]
    ) -> dict[str, Any]:
        """방송에 싣는 그 실행의 줄. **못 꺼내도 줄은 섭니다.**

        목록은 화면의 표이자 중지·종료 단추가 서는 자리라, 한 줄이 사라지면
        사용자는 실행이 없어진 줄 알고 그것을 멈출 방법까지 잃습니다.

        대신 **묵은 값이라고 적어 보냅니다**(`stale`). 안 적으면 화면이 멈춘
        손익을 지금 값으로 그립니다. 값이 다시 나오면 그 칸은 사라집니다 —
        화면이 「묵었다」를 지우는 데 또 한 박자를 쓰지 않게요.

        **줄 자체는 멀쩡해도 적습니다.** 이 박자에 그 실행의 **어느 조각이든**
        못 꺼냈으면(`broke`) 묵은 줄입니다 — 열린 주문만 못 받았는데 줄이 성하면,
        화면은 빈 미체결 표를 「없습니다」로 그립니다.
        """
        got = self._taken(made, "runs", self.service.row, at=at, broke=broke)
        if got is not None and made.run.id not in broke:
            return got
        # 앞 박자 줄이 있으면 그것을, 한 번도 못 꺼냈으면 장부 없이 아는 것만.
        return {**(got or self.service.bare_row(made)), "stale": True}

    def _taken(
        self,
        made: engine_lib.RunEngine,
        name: str,
        take: Any,
        *,
        at: int,
        broke: set[str],
    ) -> Any:
        """엔진 하나에서 방송에 실을 값을 꺼냅니다. **못 꺼내면 앞 박자 값**입니다.

        한 실행이 값을 못 내는 것과 묶음이 통째로 안 나가는 것은 다릅니다.
        뒤엣것이면 사용자는 멀쩡한 실행까지 멈춘 것을 보고, 어느 실행이
        말썽인지도 알 수 없습니다.

        **빼 버리지도 않습니다.** 실행 목록은 화면의 표이자 중지·종료 단추가
        서는 줄이라, 한 줄이 사라지면 사용자는 실행이 없어진 줄 알고 그것을
        멈출 방법까지 잃습니다. 그래서 마지막으로 성공한 값을 그대로 세우고,
        무슨 일이 있었는지는 그 실행의 이벤트로 알립니다(`_broke`).

        **한 번도 못 꺼낸 실행은 세울 앞 값이 없어 `None`** 입니다 — 그때는
        빠집니다. 지어낸 줄을 세우면 화면이 없는 값을 그립니다.
        """
        key = (made.run.id, name)
        try:
            got = take(made)
        except Exception as exc:  # noqa: BLE001 — 한 실행 때문에 묶음이 멈추면 안 됩니다.
            broke.add(made.run.id)
            self._broke(made, exc, at)
            return self._last_good.get(key)
        self._last_good[key] = got
        return got

    def _broke(self, made: engine_lib.RunEngine, exc: Exception, at: int) -> None:
        """그 실행의 값을 못 꺼냈을 때. **같은 까닭은 실행마다 한 번만** 남깁니다.

        박자는 초에 열 번입니다. 고쳐지지 않는 예외 하나가 같은 줄을 초당 열
        번씩 쌓으면 기록에서 다른 일이 묻히고 파일만 자랍니다.
        """
        mark = f"{type(exc).__name__}: {exc}"
        if self._last_broke.get(made.run.id) == mark:
            return
        self._last_broke[made.run.id] = mark
        logger.exception("실행 %s 의 줄을 내지 못했습니다.", made.run.id)
        # **기록에는 안 남깁니다.** 거래에서 일어난 일이 아니라 앱이 값을 못
        # 꺼냈다는 운영 사고입니다 — 거래 기록 사이에 끼면 그 실행이 무엇을
        # 했는지 읽기 어려워집니다. 화면은 이벤트로 봅니다.
        made._say(at, engine_lib.GAP, f"{FAULT} {mark}", keep=False)


def _open_orders(made: engine_lib.RunEngine) -> list[dict[str, Any]]:
    """그 실행에 아직 안 채워진 가상 주문들. `_taken` 에 건네는 자리입니다."""
    return made.open_orders


def _fills(made: engine_lib.RunEngine) -> list[dict[str, Any]]:
    """그 실행의 최근 체결. 화면이 이어 붙이므로 매번 다 보내지 않습니다."""
    return made.fills[-_RECENT:]


def _events(made: engine_lib.RunEngine) -> list[dict[str, Any]]:
    """그 실행의 최근 이벤트. 같은 까닭으로 매번 다 보내지 않습니다."""
    return made.events[-_RECENT:]


#: 한 묶음에 싣는 최근 줄 수. 화면은 이어 붙이므로 매번 다 보낼 까닭이 없습니다.
_RECENT = 20

#: 박자가 터졌을 때 실행에 남기는 말의 머리. 뒤에 예외 한 줄이 붙습니다.
FAULT = "실행을 돌리다 막혔습니다 —"

#: 구독 자리가 없어 밀렸을 때 남기는 말.
NO_ROOM = "실시간 시세 한도를 넘어 이 실행은 시세를 못 받습니다"

#: 서버가 실시간 등록을 거절했을 때 남기는 말의 머리. 뒤에 서버의 코드와 글이 붙습니다.
REFUSED = "실시간 등록을 서버가 거절했습니다 —"


def refused_text(code: Any, message: Any) -> str:
    """서버가 거절한 구독에 남기는 말. 코드와 글은 서버가 보낸 그대로, 글이 비면 코드만."""
    said = " ".join(
        part for part in (str(code or "").strip(), str(message or "").strip()) if part
    )
    return f"{REFUSED} {said}" if said else REFUSED

#: 실행이 쓰는 창 이름 → 세션 판정이 내는 값.
_WINDOW_OF = {"domestic-day": "day", "domestic-night": "night"}


def _open_now(market: str, code: str, session: str = "") -> bool:
    """그 종목의 **그 창**이 지금 열려 있는지. 화면이 쓰는 판정 그대로입니다.

    같은 함수를 부르는 것이 핵심입니다 — 국내는 품목마다 세션 시간이 달라,
    엔진이 따로 셈하면 화면은 열려 있다는데 실행은 닫힌 상태가 됩니다.

    **국내는 창까지 맞춰 봅니다.** 한 종목이 주간과 야간에 두 번 열리므로
    「닫혔는지」만 보면 주간 실행이 야간에도 열린 것으로 잡힙니다. 해외는 창이
    하나뿐이라 닫혔는지만 봅니다.

    **「모른다」는 닫힘이 아닙니다.** 그 종목을 종목 마스터에서 못 찾거나 거래시간
    표에 그 품목 줄이 없으면 판정이 창을 하나도 못 세워 마감으로 답합니다
    (`judge.UNKNOWN`). 그것을 닫힘으로 읽으면, 장이 열려 있는데 실행이 세션을
    닫고 판정을 내려놓습니다 — 표가 낡아 줄이 빠지는 것만으로 그렇게 됩니다.
    """
    from backend.sessions import judge
    from backend.sessions.sessions_routes import judged

    made = judged(market, code, now_kst())
    if str(made.get("source") or "") == judge.UNKNOWN:
        return True
    if str(made.get("state") or "") == judge.CLOSED:
        return False
    if market != judge.DOMESTIC:
        return True
    return str(made.get("session") or "") == _WINDOW_OF.get(str(session or ""), "")


def _opening_now(market: str, code: str, session: str = "") -> int | None:
    """그 종목의 **그 창**이 지금 거래 중이면 그 창이 연 시각(밀리초). 아니면 `None`.

    화면이 쓰는 세션 판정(`sessions_routes.judged`)의 `since` 를 씁니다. 그 값은
    **지금 들어 있는 창**의 시작이라, 거래 창 안에 있을 때만 개장 시각입니다 —
    동시호가·종가 단일가 창에서는 그 창의 시작이 오므로 받지 않습니다.

    국내는 주간·야간의 거래 창(`day`·`night`)입니다. 해외는 첫 구간(`open`)일
    때만 받습니다 — 둘째 구간·익일 구간에서는 그 날의 개장 시각을 이 판정으로
    알 수 없습니다. 판정이 품목을 모르면(`unknown`) `None` 입니다.
    """
    from backend.sessions import judge
    from backend.sessions.sessions_routes import judged

    made = judged(market, code, now_kst())
    if str(made.get("source") or "") == judge.UNKNOWN:
        return None
    state = str(made.get("state") or "")
    if market == judge.DOMESTIC:
        window = _WINDOW_OF.get(str(session or ""), "")
        if not window or state != window or str(made.get("session") or "") != window:
            return None
    elif state != judge.OPEN:
        return None
    return _millis(made.get("since"))


#: 끝을 모르는 구간의 끝(밀리초). 「계속」입니다.
_FOREVER = 2**62


def _auction_now(market: str, code: str, session: str = "") -> tuple[int, int] | None:
    """그 종목의 **그 창**의 단일가(장 시작·종가 동시호가) 구간 `[시작, 끝)`. 국내만 압니다.

    지금 단일가면 그 구간이고, 지금 거래 창이면 **다음 경계부터** 다음 개장까지입니다
    — 거래 창이 끝나는 시각이 종가 단일가의 시작이라, 박자 사이에 경계를 넘은 체결도
    체결 시각으로 가립니다. 화면이 쓰는 세션 판정(시간표를 못 받았으면 내장 표)의
    값입니다. 판정이 품목을 모르면 늘 미룹니다 — 짐작해서 채우지 않습니다. 해외는
    단일가를 알려 주는 칸이 없어 `None` 입니다.
    """
    from backend.sessions import judge
    from backend.sessions.sessions_routes import judged

    if market != judge.DOMESTIC:
        return None
    made = judged(market, code, now_kst())
    if str(made.get("source") or "") == judge.UNKNOWN:
        return (0, _FOREVER)
    state = str(made.get("state") or "")
    window = _WINDOW_OF.get(str(session or ""), "")
    if str(made.get("session") or "") != window:
        return None
    since = _millis(made.get("since"))
    until = _millis(made.get("until"))
    if state in (judge.PRE_OPEN, judge.PRE_NIGHT, judge.CLOSING_AUCTION):
        return (since if since is not None else 0, until if until is not None else _FOREVER)
    if state == window and until is not None:
        later = _millis(made.get("nextOpen"))
        return (until, later if later is not None and later > until else _FOREVER)
    return None


def _never_unknown(market: str, code: str) -> bool:
    return False


def _no_auction(market: str, code: str, session: str = "") -> tuple[int, int] | None:
    return None


def _closing_now(market: str, code: str, session: str = "") -> int | None:
    """그 종목의 **그 창**이 지금 거래 중이면 그 창이 닫히는 시각(밀리초). 아니면 `None`.

    `_opening_now` 와 같은 판정에서 `until` 을 읽습니다 — 거래 창 안에 있을 때만
    그 창의 끝입니다.
    """
    from backend.sessions import judge
    from backend.sessions.sessions_routes import judged

    made = judged(market, code, now_kst())
    if str(made.get("source") or "") == judge.UNKNOWN:
        return None
    state = str(made.get("state") or "")
    if market == judge.DOMESTIC:
        window = _WINDOW_OF.get(str(session or ""), "")
        if not window or state != window or str(made.get("session") or "") != window:
            return None
    elif state not in (judge.OPEN, judge.OPEN_SECOND, judge.NEXT_DAY):
        return None
    else:
        # 해외는 **그 거래일의 마지막 구간 끝**입니다 — 구간이 여럿인 품목(홍콩 등)은
        # 첫 구간이 끝나도 거래일이 끝난 것이 아니라 거기서 정리하지 않습니다.
        from backend.sessions.sessions_routes import trading_day_end

        last = trading_day_end(market, code, now_kst())
        if last is not None:
            return int(last.timestamp() * 1000)
    return _millis(made.get("until"))


def _no_opening(market: str, code: str, session: str = "") -> int | None:
    """개장 시각을 모른다고 답합니다. 장중 판정을 바꿔 끼운 자리의 기본값입니다."""
    return None


def _millis(text: Any) -> int | None:
    """ISO 시각 글자를 밀리초로. 읽을 수 없으면 `None` 입니다."""
    try:
        moment = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        return None
    return int(moment.timestamp() * 1000)


def _market_of(session: str) -> str:
    return "domestic" if str(session).startswith("domestic") else "overseas"


def _market_volume(run: runs.Run) -> int | None:
    """그 실행의 종목을 지금 조회해 누적 거래량을 냅니다. 모르면 `None`.

    **화면이 쓰는 현재가 조회와 같은 함수와 같은 읽기**입니다. 국내 야간 실행은
    야간 현재가를 부릅니다 — 실시간의 누적 거래량도 그 창의 값이라, 주간 값과
    견주면 늘 달라 보입니다.
    """
    if _market_of(run.session) == "overseas":
        from backend.overseas_futureoption import overseas_futureoption_functions as overseas_api
        from backend.overseas_futureoption import overseas_quotes

        if not run.exchange:
            return None
        raw = overseas_api.price([(run.exchange, run.code)])
        rows = overseas_quotes.quotes_of(raw, asked=[run.code]).get("quotes") or []
        row = next((one for one in rows if one.get("code") == run.code), None)
    else:
        from backend.domestic_futureoption import domestic_futureoption_functions as domestic_api
        from backend.domestic_futureoption import domestic_quotes

        fetch = domestic_api.price_night if run.session == DOMESTIC_NIGHT else domestic_api.price
        row = domestic_quotes.quote(fetch(run.code), asked=run.code)
    volume = (row or {}).get("volume")
    if isinstance(volume, (int, float)) and not isinstance(volume, bool):
        return int(volume)
    return None


def _now() -> int:
    return int(now_kst().timestamp() * 1000)


#: 앱이 쓰는 펌프. 테스트는 제 것을 만들어 씁니다.
pump: RunPump | None = None
