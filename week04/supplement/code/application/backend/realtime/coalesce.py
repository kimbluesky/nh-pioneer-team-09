"""오는 대로 넘기지 않고 잠깐 모았다가 한 번에 보냅니다.

호가는 한 종목이 한창일 때 1초에 여러 번 바뀝니다. 그때마다 화면으로 넘기면
브라우저가 1초에 그만큼 다시 그립니다. 눈은 그것을 못 따라가고, 여러 종목이
동시에 움직이면 화면이 밀립니다.

그래서 **100밀리초 동안 모았다가 한 번 보냅니다.** 초당 열 번이라 끊겨 보이지
않고, 대개는 모을 것이 없어 그냥 지나갑니다. 값이 갑자기 쏟아질 때만 일합니다.

## 종류마다 모으는 법이 다릅니다

**호가는 마지막 것만 남깁니다.** 100밀리초 사이에 호가가 세 번 바뀌었으면
화면에 필요한 것은 마지막 모습 하나입니다. 앞의 둘은 이미 지나간 그림입니다.

**시세와 체결은 갈라서 다룹니다.** 시세 값은 마지막 것만 있으면 되지만 체결은
건마다 필요합니다 — 체결 목록이 한 건씩 쌓여 만들어지기 때문입니다. 마지막
것만 남기면 그 사이 체결이 목록에서 사라집니다. 그래서 한 번 보낼 때 **시세는
마지막 값, 체결은 그 사이 전부**를 함께 담습니다.

**주문/체결 알림은 모으기만 하고 하나도 버리지 않습니다.** 접수·확인·체결이
각각 한 건으로 오고, 화면이 주문번호로 이어 붙여 진행을 그립니다. 한 건이라도
빠지면 주문이 어디까지 갔는지 알 수 없게 됩니다.
"""
from __future__ import annotations

from typing import Any

from backend.realtime import feeds
from backend.realtime.feeds import BOOK, ORDERS, QUOTE, Feed
from backend.realtime.ledger import StreamKey, stream_key


class Coalescer:
    """보낼 것을 모아 두었다가 한 번에 내줍니다."""

    def __init__(self) -> None:
        self._quotes: dict[StreamKey, tuple[Feed, dict[str, Any], list[dict[str, Any]]]] = {}
        self._books: dict[StreamKey, tuple[Feed, dict[str, Any]]] = {}
        self._orders: dict[StreamKey, tuple[Feed, list[dict[str, Any]]]] = {}

    def add(self, feed: Feed, body: dict[str, Any]) -> None:
        """NH선물이 보내 온 자료 하나를 담습니다."""
        key = stream_key(feed)

        if feed.kind == QUOTE:
            held = self._quotes.get(key)
            patch, trades = (held[1], held[2]) if held else ({}, [])
            patch.update(feeds.quote_patch(body, context=feed.context))
            trade = feeds.trade_of(body, context=feed.context)
            if trade is not None:
                trades.append(trade)
            self._quotes[key] = (feed, patch, trades)
            return

        if feed.kind == BOOK:
            # 마지막 모습만 남깁니다.
            self._books[key] = (feed, feeds.book_of(body, context=feed.context))
            return

        if feed.kind == ORDERS:
            held = self._orders.get(key)
            events = held[1] if held else []
            events.append(feeds.order_event(body, context=feed.context))
            self._orders[key] = (feed, events)

    def drop(self, key: StreamKey) -> None:
        """이 구독을 그만둘 때 부릅니다. 아직 안 보낸 것도 버립니다.

        주문/체결 알림은 버리지 않습니다 — 화면이 그만 보겠다고 한 것과 체결이
        일어나지 않은 것은 다릅니다.
        """
        self._quotes.pop(key, None)
        self._books.pop(key, None)

    def drain(self) -> list[tuple[StreamKey, dict[str, Any]]]:
        """모아 둔 것을 화면으로 보낼 모양으로 꺼냅니다. 꺼낸 것은 비웁니다.

        열쇠를 함께 냅니다 — 누구에게 보낼지는 이 열쇠로 정해집니다.
        """
        made: list[tuple[StreamKey, dict[str, Any]]] = []

        for key, (feed, patch, trades) in self._quotes.items():
            if not patch and not trades:
                continue
            made.append(
                (
                    key,
                    {
                        "type": QUOTE,
                        "context": feed.context,
                        "sym": feed.sym,
                        "patch": patch,
                        "trades": trades,
                    },
                )
            )
        self._quotes.clear()

        for key, (feed, book) in self._books.items():
            made.append((key, {"type": BOOK, "context": feed.context, "sym": feed.sym, **book}))
        self._books.clear()

        for key, (feed, events) in self._orders.items():
            if not events:
                continue
            made.append((key, {"type": ORDERS, "market": feed.market, "events": events}))
        self._orders.clear()

        return made

    def __len__(self) -> int:
        """아직 안 보낸 구독 수. 테스트가 봅니다."""
        return len(self._quotes) + len(self._books) + len(self._orders)
