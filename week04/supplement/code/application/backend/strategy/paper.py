"""종이거래 체결 모형 `paper-v2`.

검증 실행은 **주문을 내지 않습니다.** 대신 그 종목의 실시간 호가와 체결을 보고
「그때 냈으면 어떻게 채워졌을까」를 모형으로 채웁니다. 모형이 무엇을 가정했는지
모르면 뒤에 나오는 손익이 무슨 뜻인지도 모르므로, 가정을 여기 적어 두고 이름을
붙입니다(`MODEL`). 모형이 바뀌면 이름도 바꿔야 옛 장부와 섞이지 않습니다.

## 아홉 가지 가정

1. **지연** — 의도를 낸 순간 바로 체결되지 않습니다. `DELAY_SECONDS` 가 지난
   뒤 처음 오는 호가·체결부터 봅니다. 지연을 0 으로 두면 봉 마감 직후의 값을
   그대로 먹어 실제보다 좋은 값이 나옵니다.
2. **시장가는 표시 잔량을 넘지 못합니다** — 반대 호가를 위 단계부터 표시된
   잔량만큼 먹고, 모자라면 다음 단계로 갑니다. 한 가격에 원하는 만큼 다
   채워지는 모형은 슬리피지를 지웁니다.
3. **지정가는 줄을 섭니다** — 도착 순간 그 가격에 이미 서 있던 잔량(`Q`)이
   먼저 채워진 뒤에야 이 주문 차례입니다. 「가격이 닿았다」만으로 체결하지
   않습니다. 다만 가격이 **관통**하면 잔량을 전부 채웁니다.
4. **공백에는 채우지 않습니다** — 시세가 끊긴 동안 대기 주문은 그대로 두되
   체결시키지 않습니다. 끊긴 구간의 값을 모르는데 채우면 없던 체결이 생깁니다.
5. **세션이 끝나면 대기 주문은 취소됩니다** — 다음 세션으로 넘겨 주지
   않습니다. 밤새 걸어 둔 주문이 아침에 채워지는 모양은 실제와 다릅니다.
6. **같은 잔량은 한 번만 먹습니다** — 시장가가 호가의 한 단계를 먹으면 그 몫을
   가격마다 기억해 두고, 같은 잔량이 다음 호가에 다시 보여도 먹은 몫은 빼고
   먹습니다. 그 가격의 잔량이 줄었거나(실제 체결로 소진) 호가에서 빠지면 기억을
   지웁니다. 안 그러면 호가가 그대로인 동안 같은 잔량을 몇 번이고 먹습니다.
7. **관통해도 지난 체결 수량만큼만** — 이 주문의 가격을 지난 체결이 와도 그 체결의
   수량에서 앞 순위(`Q`)를 먼저 깎고 남는 만큼만 채웁니다. 「가격이 넘었으니
   전부」는 얇은 장에서 실제보다 많이 채웁니다.
8. **단일가 창과 가격제한에서는 채우지 않고 미룹니다** — 국내 동시호가(장 시작·
   종가 단일가) 구간은 세션 판정의 시간표가 알려 주고(실행 목록이 구간을 넣습니다),
   **체결·호가의 시각이** 그 구간 안이면 채우지 않습니다. 단일가로 한 값에 맞춰
   채우지는 않습니다 — 그 값을 셈할 출처가 없습니다. 구간이 끝난 뒤 첫 값부터
   위의 규칙대로 다시 봅니다. 시간표에 그 품목이 없으면 늘 미룹니다.
   마지막 값이 실시간 상한가면 매수, 하한가면 매도의 시장가와 도착 즉시 체결을
   미룹니다(실시간 시세의 `up_act_pric`·`lo_act_pric`, 아직 모르면 제한 없음).
   한도 가격에 줄 서 있는 지정가는 그 가격의 체결로 줄 규칙대로 채웁니다. 해외의
   단일가·가격제한, 그리고 변동성완화장치·서킷브레이커는 알려 주는 칸이 없어
   다루지 않습니다 — 거래가 멈추면 체결이 오지 않으므로 그동안 지어낸 체결은
   없습니다.
9. **끊긴 뒤의 손절·익절은 다시 붙은 첫 체결 값으로** — 끊긴 동안은 보지 않고,
   다시 붙은 첫 체결에서 판정해 **그 값에** 바로 채웁니다(지연 없이). 손절가가
   아닙니다 — 끊긴 사이 값이 손절선을 지나갔으면 손실은 손절 폭보다 큽니다.

## 수수료·정산가·환율

수수료는 계약당 설정값입니다. 체결 줄에는 거래소 몫(`feeExchange`)과 청산 몫
(`feeClearing`)을 따로 적고 `fee` 는 그 합입니다. NH선물 API 에는 둘을 나눈 칸이
없어 지금은 설정값 전부가 거래소 몫이고 청산 몫은 `None` 입니다.

보유는 마지막 체결가로 평가합니다. 정산가로 평가하려면 국내 현재가의 전일정산가
(`pd_sett_pric`), 해외 현재가의 정산가(`occurs1_sett_pric`)가 그 출처입니다.

해외 손익은 달러 그대로 둡니다. 원화로 환산하게 되면 환율 출처(해외 미결제약정의
`occurs1_usd_exrt`, 해외 예탁금의 `ref_exrt`)와 그 시각을 함께 적고, 모르면 값을
비웁니다 — 1.0 을 곱해 원화인 척하지 않습니다.

## 다루지 않는 주문유형

국내 조건부지정가·최유리지정가는 **모형이 다루지 않습니다.** 둘 다 거래소가
장 상황을 보고 값을 정하는 유형이라, 호가 다섯 단계만으로는 어느 값에
들어갔을지 고를 수 없습니다. 짐작해서 채우느니 시작할 때 막습니다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 모형 이름. 실행마다 저장해 둡니다 — 가정이 바뀌면 이름도 바꿉니다.
MODEL = "paper-v2"

#: 모형 이름이 없던 때의 종이 기록·실행이 쓴 모형.
LEGACY_MODEL = "paper-v1"

#: 처리 지연(초). 의도를 낸 순간부터 이만큼 지난 뒤의 값부터 봅니다.
DELAY_SECONDS = 0.3

BUY = "buy"
SELL = "sell"

#: 주문유형. 화면의 `ORDER_TYPES` 와 같은 말입니다.
MARKET_ORDER = "시장가"
LIMIT_ORDER = "지정가"
STOP_MARKET = "STOP-M"
STOP_LIMIT = "STOP-L"

#: **모형이 다루지 않는 주문유형** — 까닭은 이 파일 첫머리에.
UNSUPPORTED_ORDER_TYPES = ("조건부지정가", "최유리지정가")

#: 그 주문유형을 담거나 실행을 시작하려 할 때 하는 말. 화면과 같은 글입니다.
#: 종이·계좌 실행 모두에 걸리는 말이라 장부 이름을 넣지 않습니다.
UNSUPPORTED_WHY = "{value}는 전략 주문에 쓰지 않습니다 — 다른 유형을 고르세요"

#: 체결조건. 국내 주문에만 붙습니다.
FILL_ALL_OR_NONE = "FOK"
FILL_AND_KILL = "FAK"

WAITING = "waiting"
PARTIAL = "partial"
FILLED = "filled"
CANCELLED = "cancelled"

#: 열려 있는 주문의 상태. 이 둘만 더 채워집니다.
OPEN_STATUS = (WAITING, PARTIAL)


def supported(order_type: str) -> bool:
    """모형이 그 주문유형을 채울 수 있는지. 담을 때와 시작 전에 봅니다."""
    return str(order_type or "").strip() not in UNSUPPORTED_ORDER_TYPES


def unsupported_why(order_type: str) -> str:
    """그 주문유형을 쓰지 않는다는 말(`UNSUPPORTED_WHY`)."""
    return UNSUPPORTED_WHY.format(value=str(order_type or "").strip())


def fee_of(quantity: int, per_contract: float) -> float:
    """수수료. **계약당 설정값 × 계약 수**이고 체결마다 답니다."""
    return max(int(quantity), 0) * max(float(per_contract), 0.0)


def max_contracts(budget: float | None, margin: float | None) -> int | None:
    """증거금 예산으로 들 수 있는 계약 수. **모르면 `None` 입니다.**

    계약당 증거금은 국내 품목별세부정보에서 옵니다. 해외에는 그 칸이 없어
    아직 모릅니다 — 그때 0 으로 답하면 「한 계약도 못 든다」는 뜻이 되고,
    큰 수로 답하면 예산이 없는 것과 같아집니다. 모른다고 답하고 부르는 쪽이
    그대로 알리게 둡니다.
    """
    if budget is None or margin is None or margin <= 0:
        return None
    return max(int(budget // margin), 0)


@dataclass
class PaperOrder:
    """종이 주문 하나. 열려 있는 동안 호가·체결을 볼 때마다 조금씩 채워집니다."""

    id: str
    side: str
    quantity: int
    order_type: str
    #: 지정가. 시장가·STOP-M 은 `None` 입니다.
    price: float | None = None
    #: 스탑 가격. STOP 주문에만 있습니다.
    stop_price: float | None = None
    #: 국내 체결조건. 해외는 빈 값입니다.
    fill_condition: str = ""
    #: 의도를 낸 순간(밀리초).
    created: int = 0
    filled: int = 0
    status: str = WAITING
    #: 취소된 까닭. 화면이 그대로 적습니다.
    reason: str = ""
    #: 지정가 앞에 서 있던 잔량. 아직 안 재었으면 `None` 입니다.
    queue_ahead: int | None = None
    #: 앞 순위를 덜어 낸 체결량 누적.
    queue_done: int = 0
    #: STOP 이 발동했는지. STOP 이 아니면 처음부터 참입니다.
    armed: bool = False
    #: 도착(지연이 지난 뒤 첫 호가)을 봤는지.
    arrived: bool = False
    fills: list[dict[str, Any]] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return max(self.quantity - self.filled, 0)

    @property
    def open(self) -> bool:
        return self.status in OPEN_STATUS


class PaperBook:
    """한 실행의 종이 주문 장부.

    호가와 체결을 받는 대로 넣어 주면, 그때 채워진 체결을 냅니다. 체결 한 줄은
    `{"orderId", "time", "price", "quantity", "side"}` 이고 **소비한 가격
    단계마다 한 줄**입니다 — 한 줄로 뭉치면 슬리피지가 평균에 묻힙니다.
    """

    def __init__(self, *, delay: float = DELAY_SECONDS) -> None:
        self.delay_ms = int(max(float(delay), 0.0) * 1000)
        self.orders: list[PaperOrder] = []
        #: 마지막 체결가. STOP 발동과 손절·익절이 보는 값입니다.
        self.last_price: float | None = None
        #: 시세가 끊겼는지. 참이면 아무것도 채우지 않습니다.
        self.gap = False
        #: 단일가 구간 `[시작, 끝)`(밀리초). 체결·호가 시각이 그 안이면 채우지 않습니다(가정 8).
        self.auction: tuple[int, int] | None = None
        #: 실시간 상한가·하한가. 모르면 `None` 입니다(가정 8).
        self.upper: float | None = None
        self.lower: float | None = None
        #: 가격마다 이미 먹은 잔량(가정 6). `(쪽, 가격)` → `[본 잔량, 먹은 몫]`.
        self._eaten: dict[tuple[str, float], list[int]] = {}

    # ── 넣기 ────────────────────────────────────────────

    def place(
        self,
        order_id: str,
        *,
        side: str,
        quantity: int,
        order_type: str,
        price: float | None = None,
        stop_price: float | None = None,
        fill_condition: str = "",
        at: int,
    ) -> PaperOrder:
        """주문 의도 하나를 장부에 겁니다. 채우는 것은 다음 호가·체결부터입니다."""
        made = PaperOrder(
            id=order_id,
            side=side,
            quantity=max(int(quantity), 0),
            order_type=str(order_type or "").strip(),
            price=price,
            stop_price=stop_price,
            fill_condition=str(fill_condition or "").strip(),
            created=int(at),
            armed=str(order_type or "").strip() not in (STOP_MARKET, STOP_LIMIT),
        )
        self.orders.append(made)
        return made

    # ── 보기 ────────────────────────────────────────────

    @property
    def open_orders(self) -> list[PaperOrder]:
        return [one for one in self.orders if one.open]

    def on_book(self, book: dict[str, Any], *, at: int) -> list[dict[str, Any]]:
        """호가 한 벌을 보고 채웁니다. 시장가와 도착한 지정가가 여기서 먹습니다."""
        if self.gap:
            return []
        asks = _levels(book.get("asks"), nearest_last=True)
        bids = _levels(book.get("bids"), nearest_last=False)
        self._forget_eaten(BUY, asks)
        self._forget_eaten(SELL, bids)
        if self._in_auction(at):
            return []

        made: list[dict[str, Any]] = []
        for order in self.open_orders:
            if not self._ripe(order, at) or not order.armed:
                continue
            capped = self._capped(order.side)
            if order.order_type in (MARKET_ORDER, STOP_MARKET):
                if not capped:
                    made += self._eat(order, asks if order.side == BUY else bids, None, at)
                continue
            if not order.arrived:
                # 한도에 닿은 쪽은 먹지 않고 줄만 섭니다 — 그 가격의 체결로 채워집니다.
                if capped:
                    made += self._arrive(order, [] if order.side == BUY else asks, bids if order.side == BUY else [], at)
                else:
                    made += self._arrive(order, asks, bids, at)
        return made

    def on_trade(self, trade: dict[str, Any], *, at: int) -> list[dict[str, Any]]:
        """체결 한 건을 보고 채웁니다. 줄 서 있는 지정가와 STOP 이 여기서 움직입니다."""
        price = _float(trade.get("price"))
        quantity = _int(trade.get("quantity")) or 0
        if price is None:
            return []
        self.last_price = price
        if self.gap or self._in_auction(at):
            return []

        made: list[dict[str, Any]] = []
        for order in self.open_orders:
            if not self._ripe(order, at):
                continue
            if not order.armed:
                self._arm(order, price)
                continue
            if order.order_type in (LIMIT_ORDER, STOP_LIMIT) and order.arrived:
                made += self._queued(order, price, quantity, at)
        return made

    def fill_now(self, order: PaperOrder, price: float, *, at: int) -> list[dict[str, Any]]:
        """주문 하나를 지금 그 값에 전량 채웁니다. 끊긴 뒤 첫 체결의 손절·익절이 씁니다(가정 9)."""
        if not order.open or order.remaining <= 0:
            return []
        return [self._fill(order, price, order.remaining, at)]

    def cancel(self, order: PaperOrder, reason: str) -> bool:
        """주문 하나를 취소합니다. **종이는 즉시입니다** — 장부가 이 앱 안에 있어서.

        모의 계좌는 여기서 요청만 내고 확인을 기다립니다(`sim.SimBroker`).
        부르는 쪽이 그 차이를 알 필요가 없도록 얼굴을 맞춰 둡니다.
        """
        if not order.open:
            return False
        order.status = CANCELLED
        order.reason = reason
        return True

    def cancel_open(self, reason: str) -> list[PaperOrder]:
        """열려 있는 주문을 전부 취소합니다. **종이 실행의 세션 마감에서만 부릅니다.**

        지어낸 주문이라 내보낼 곳이 없고, 거두지 않으면 다음 세션 첫 호가에
        채워져 현실에 없던 체결이 성적에 들어갑니다(`engine.close_session`).
        **모의는 이 자리를 안 지납니다** — 그 취소는 계좌로 나가는 요청이라
        세션 마감에 이 앱이 내지 않습니다. 중지도 이 자리를 안 씁니다 — 진입
        주문만 골라 거둡니다(`service._pause`).
        """
        return [one for one in self.open_orders if self.cancel(one, reason)]

    # ── 안쪽 ────────────────────────────────────────────

    def _ripe(self, order: PaperOrder, at: int) -> bool:
        """지연이 지났는지. 의도 시각의 값으로는 채우지 않습니다."""
        return at >= order.created + self.delay_ms

    def _arm(self, order: PaperOrder, price: float) -> None:
        """STOP 이 발동했는지. 매수는 스탑가 이상, 매도는 이하에서 켜집니다."""
        stop = order.stop_price
        if stop is None:
            order.armed = True
            return
        if (order.side == BUY and price >= stop) or (order.side == SELL and price <= stop):
            order.armed = True

    def _arrive(
        self,
        order: PaperOrder,
        asks: list[tuple[float, int]],
        bids: list[tuple[float, int]],
        at: int,
    ) -> list[dict[str, Any]]:
        """지정가가 도착했을 때. 이미 유리하면 먹고, 아니면 줄을 섭니다."""
        order.arrived = True
        limit = order.price
        if limit is None:
            return []

        taking = asks if order.side == BUY else bids
        made = self._eat(order, taking, limit, at)

        if order.remaining and order.fill_condition == FILL_ALL_OR_NONE:
            # 전량이 안 되면 없던 일로 합니다 — 채운 것까지 되돌립니다.
            order.filled = 0
            order.fills.clear()
            order.status = CANCELLED
            order.reason = "FOK — 주문 즉시 전량 체결되지 않아 취소했습니다"
            return []
        if order.remaining and order.fill_condition == FILL_AND_KILL:
            order.status = CANCELLED
            order.reason = "FAK — 주문 즉시 채워지지 않은 잔량을 취소했습니다"
            return made

        if order.remaining:
            # 줄을 섭니다. 앞 순위는 **지금 그 가격에 서 있는 잔량**입니다.
            same_side = bids if order.side == BUY else asks
            order.queue_ahead = next((qty for price, qty in same_side if price == limit), 0)
            order.queue_done = 0
        return made

    def _queued(
        self, order: PaperOrder, price: float, quantity: int, at: int
    ) -> list[dict[str, Any]]:
        """줄 서 있는 지정가를 체결 흐름으로 덜어 냅니다."""
        limit = order.price
        if limit is None or order.queue_ahead is None:
            return []

        # 관통이든 닿기만이든 **그 체결의 수량만큼만** 셉니다. 앞 순위를 먼저
        # 깎고 남는 만큼이 이 주문 몫입니다(가정 7).
        touching = price <= limit if order.side == BUY else price >= limit
        if not touching or quantity <= 0:
            return []

        order.queue_done += quantity
        spare = order.queue_done - order.queue_ahead
        if spare <= 0:
            return []
        order.queue_ahead += spare
        take = min(spare, order.remaining)
        if take <= 0:
            return []
        return [self._fill(order, limit, take, at)]

    def _eat(
        self,
        order: PaperOrder,
        levels: list[tuple[float, int]],
        limit: float | None,
        at: int,
    ) -> list[dict[str, Any]]:
        """반대 호가를 가까운 단계부터 표시 잔량만큼 먹습니다."""
        made: list[dict[str, Any]] = []
        for price, shown in levels:
            if order.remaining <= 0:
                break
            if limit is not None:
                if order.side == BUY and price > limit:
                    break
                if order.side == SELL and price < limit:
                    break
            held = self._eaten.setdefault((order.side, price), [shown, 0])
            take = min(shown - held[1], order.remaining)
            if take <= 0:
                continue
            held[1] += take
            made.append(self._fill(order, price, take, at))
        return made

    def _forget_eaten(self, side: str, levels: list[tuple[float, int]]) -> None:
        """새 호가에서 먹은 몫의 기억을 고칩니다(가정 6).

        그 가격이 호가에서 빠졌거나 잔량이 줄었으면 지웁니다 — 실제 체결이 그
        잔량을 가져갔으니 이 주문이 먹은 몫도 그 안에 든 것으로 봅니다. 잔량이 늘었으면
        본 잔량만 고치고 먹은 몫은 둡니다 — 는 만큼만 새로 먹을 수 있습니다.
        """
        shown = dict(levels)
        for key in [one for one in self._eaten if one[0] == side]:
            now = shown.get(key[1])
            if now is None or now < self._eaten[key][0]:
                del self._eaten[key]
            else:
                self._eaten[key][0] = now

    def _in_auction(self, at: int) -> bool:
        """그 시각이 단일가 구간 안인지(가정 8)."""
        span = self.auction
        return span is not None and span[0] <= at < span[1]

    def _capped(self, side: str) -> bool:
        """마지막 값이 그 방향의 가격제한에 닿았는지(가정 8). 매수는 상한, 매도는 하한."""
        last = self.last_price
        if last is None:
            return False
        if side == BUY:
            return self.upper is not None and last >= self.upper
        return self.lower is not None and last <= self.lower

    def _fill(self, order: PaperOrder, price: float, quantity: int, at: int) -> dict[str, Any]:
        order.filled += quantity
        order.status = FILLED if order.remaining == 0 else PARTIAL
        made = {
            "orderId": order.id,
            # **주문번호만으로는 체결 하나를 못 가립니다** — 한 주문이 여러 번에
            # 나뉘어 채워지고, 한 호가의 여러 단계를 먹으면 **같은 밀리초에** 여러
            # 줄이 납니다. 시각을 열쇠로 쓰면 그 줄들이 화면에서 하나로 뭉칩니다.
            "execNo": fill_no(order.id, len(order.fills) + 1),
            "time": int(at),
            "price": float(price),
            "quantity": int(quantity),
            "side": order.side,
        }
        order.fills.append(made)
        return made


def fill_no(order_id: str, index: int) -> str:
    """장부가 매기는 체결번호. **계좌가 번호를 안 줄 때 씁니다.**

    종이 체결에는 서버가 없어 번호를 받을 데가 없고, 모의도 알림에 체결번호가
    안 실려 오는 줄이 있습니다. 그때 칸을 비우면 화면이 체결을 시각으로 가리게
    되어 **같은 밀리초의 두 체결이 한 줄로 뭉칩니다.**

    모양은 주문 id 에 그 주문 안의 차례를 붙인 것입니다 — 엔진이 주문 id 를
    `{실행 id}-{차례}` 로 매기는 것과 같은 결이라, 눈으로도 어디 것인지 읽힙니다.
    """
    return f"{order_id}#{int(index)}"


def _levels(raw: Any, *, nearest_last: bool) -> list[tuple[float, int]]:
    """호가 한 쪽을 **가까운 단계부터**로 맞춥니다.

    허브는 매도를 먼 값부터, 매수를 가까운 값부터 냅니다(화면이 그리는 차례).
    먹는 쪽은 늘 가까운 단계부터라 여기서 한 방향으로 돌려 둡니다.
    """
    made: list[tuple[float, int]] = []
    for level in raw if isinstance(raw, list) else []:
        if not isinstance(level, dict):
            continue
        price = _float(level.get("price"))
        quantity = _int(level.get("quantity")) or 0
        if price is None:
            continue
        made.append((price, quantity))
    if nearest_last:
        made.reverse()
    return made


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
