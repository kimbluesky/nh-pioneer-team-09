"""누가 무엇을 보고 있는지 세고, 스무 자리를 어떻게 나눌지 정합니다.

## 왜 장부가 필요한가

같은 종목을 헤더도 보고 호가창도 보고 차트도 봅니다. 화면마다 구독을 걸면
한 종목에 구독이 세 개 나갑니다. 그래서 **NH선물에 거는 것은 종목 하나에
하나**로 묶고, 몇 화면이 그것을 보고 있는지만 셉니다. 마지막 화면이 놓을 때
구독을 해제합니다.

묶는 열쇠는 화면이 보내는 구독과 다릅니다. 주간 화면과 야간 화면의 주문/체결
알림은 화면에서는 둘이지만 NH선물에서는 **같은 하나**입니다. 열쇠를 화면 쪽
모양으로 잡으면 같은 알림을 두 번 걸고 자리도 두 칸을 씁니다.

## 화면은 「원하는 것 전부」를 보냅니다

늘리고 줄이는 것이 아니라 **지금 보고 있는 것 전체**를 그때그때 보냅니다.
늘리고 줄이는 방식은 화면이 해제를 한 번 빠뜨리면 그 구독이 아무도 안 보는
채로 남고, 그런 자리는 새로고침으로도 안 없어집니다. 전체를 보내면 빠뜨릴
것이 없고, 연결이 끊기면 그 화면 몫이 통째로 사라집니다.

## 스무 자리를 나누는 법

한 번에 유지할 수 있는 구독이 스무 개입니다. 화면이 원하는 것이 그보다 많을
수 있습니다 — 관심종목 여섯에 보고 있는 종목과 차트 둘째 칸까지 겹치면
금세 찹니다.

**고정 배분을 쓰지 않습니다.** 화면마다 필요한 수가 상황에 따라 변해서(2분할을
켜고 끄고, 관심종목을 비우고 채우고), 칸을 미리 나눠 두면 남는 칸을 못 빌려
주고 모자란 칸을 못 채웁니다.

대신 **등급**입니다. 화면이 구독마다 등급을 붙여 보내고, 자리가 모자라면 등급이
낮은 것부터 밀어냅니다. 같은 등급이면 먼저 온 것이 남습니다 — 스크롤을 조금
움직였다고 보고 있던 종목이 밀려나면 안 됩니다.

**주문/체결 알림은 등급 밖입니다.** 체결 통보를 놓치는 것이 이 프로그램에서
가장 나쁜 실패라, 자리 경쟁에 넣지 않고 먼저 잡아 둡니다. 국내와 해외 각각
하나씩 최대 두 자리입니다.

밀려난 구독은 조용히 사라지지 않고 화면에 알립니다. 값이 안 들어오는 칸이
왜 안 들어오는지 화면이 말할 수 있어야 합니다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from backend.nh_api.catalog import STREAM_SUBSCRIPTION_LIMIT
from backend.realtime.feeds import Feed

# 화면이 등급을 안 보냈을 때. 가운데 값이라 위로도 아래로도 자리가 있습니다.
DEFAULT_PRIORITY = 5

# 등급의 범위. 벗어난 값은 이 안으로 당겨 씁니다.
MIN_PRIORITY = 1
MAX_PRIORITY = 9

# NH선물에 실제로 거는 구독 하나를 가리키는 열쇠 — (계좌, 실시간 이름, 종목).
StreamKey = tuple[str, str, str]


def stream_key(feed: Feed) -> StreamKey:
    """이 구독이 NH선물에서 어느 하나에 묶이는지.

    주간 화면과 야간 화면의 주문/체결 알림이 같은 열쇠로 모입니다.
    """
    return (feed.market, feed.api_id, feed.sym)


@dataclass(frozen=True)
class Grant:
    """자리를 나눈 결과."""

    # 실제로 걸 구독. 열쇠마다 그것을 대표하는 구독 하나가 붙습니다.
    granted: dict[StreamKey, Feed]
    # 자리가 모자라 밀려난 것. 화면에 알립니다.
    rejected: tuple[Feed, ...]

    @property
    def keys(self) -> frozenset[StreamKey]:
        return frozenset(self.granted)


@dataclass
class Ledger:
    """지금 무엇을 보고 있는가.

    `limit` 은 한 번에 유지할 수 있는 구독 수입니다. 명세가 스무 개라고
    적고 있어 그것을 기본으로 씁니다.
    """

    limit: int = STREAM_SUBSCRIPTION_LIMIT
    _wishes: dict[str, dict[Feed, int]] = field(default_factory=dict)
    # 처음 나타난 순서. 같은 등급끼리는 이 순서로 남습니다.
    _seen: dict[StreamKey, int] = field(default_factory=dict)
    _tick: int = 0

    def declare(self, client: str, wishes: dict[Feed, int]) -> None:
        """이 화면이 지금 보고 있는 것 전체를 적습니다. 앞서 적은 것은 지워집니다."""
        cleaned = {
            feed: max(MIN_PRIORITY, min(MAX_PRIORITY, int(priority)))
            for feed, priority in wishes.items()
        }
        self._wishes[client] = cleaned
        for feed in cleaned:
            key = stream_key(feed)
            if key not in self._seen:
                self._seen[key] = self._tick
                self._tick += 1

    def release(self, client: str) -> None:
        """이 화면이 나갈 때 부릅니다. 그 몫을 통째로 버립니다."""
        self._wishes.pop(client, None)

    def watchers(self, key: StreamKey) -> int:
        """이 구독을 보고 있는 화면 수."""
        return sum(
            1
            for wishes in self._wishes.values()
            if any(stream_key(feed) == key for feed in wishes)
        )

    def grant(self) -> Grant:
        """자리를 나눕니다.

        같은 열쇠를 여러 화면이 원하면 **가장 높은 등급**(작은 수)으로 셉니다.
        하나라도 급하게 보는 화면이 있으면 그 구독은 급한 것입니다.
        """
        best: dict[StreamKey, tuple[int, Feed]] = {}
        for wishes in self._wishes.values():
            for feed, priority in wishes.items():
                key = stream_key(feed)
                current = best.get(key)
                if current is None or priority < current[0]:
                    best[key] = (priority, feed)

        # 아무도 안 보는 열쇠의 순서 기록은 버립니다. 그대로 두면 오래 켜 둔
        # 프로그램에서 끝없이 쌓입니다.
        self._seen = {key: order for key, order in self._seen.items() if key in best}

        reserved = {key: item[1] for key, item in best.items() if item[1].is_orders}
        contenders = sorted(
            ((key, item) for key, item in best.items() if not item[1].is_orders),
            key=lambda pair: (pair[1][0], self._seen.get(pair[0], 0)),
        )

        room = max(0, self.limit - len(reserved))
        granted: dict[StreamKey, Feed] = dict(reserved)
        rejected: list[Feed] = []
        for key, (_, feed) in contenders:
            if len(granted) - len(reserved) < room:
                granted[key] = feed
            else:
                rejected.append(feed)

        return Grant(granted=granted, rejected=tuple(rejected))
