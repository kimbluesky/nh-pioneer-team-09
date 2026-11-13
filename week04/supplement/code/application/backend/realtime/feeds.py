"""실시간 메시지를 화면이 쓰는 모양으로 바꿉니다 — 화면 계약이 여기 있습니다.

## 무엇을 구독하는가

구독 하나는 **(화면 맥락, 종류, 종목)** 셋으로 정해집니다. 이 셋이 같으면 같은
구독이고, 여러 화면이 같은 것을 보고 있어도 NH선물에는 한 번만 겁니다.

    맥락  domestic-day · domestic-night · overseas   (화면의 `MarketContext` 와 같은 낱말)
    종류  quote(시세체결) · book(호가) · orders(주문/체결 알림)
    종목  국내·해외 종목코드. `orders` 는 계좌 단위라 비어 있습니다

맥락과 종류가 NH선물의 실시간 이름을 정합니다. 부르는 쪽은 이 이름을 몰라도
됩니다.

| | quote | book | orders |
| --- | --- | --- | --- |
| domestic-day | KA | KB | O1_K |
| domestic-night | KC | KD | O1_K |
| overseas | FA | FB | O1 |

주문/체결 알림은 밤낮을 가리지 않습니다 — 계좌가 하나이기 때문입니다. 국내와
해외는 계좌가 달라 각각 겁니다.

## 시세는 왜 「덮어쓰기」가 아닌가

실시간이 보내는 `quote` 는 **`Quote` 전체가 아니라 실시간이 말할 수 있는 항목만**
담습니다. 종목 이름, 계약 크기, 최종거래일, 상장 후 최고·최저, 잔존일수는
실시간에 아예 오지 않습니다. 전체를 보내는 것처럼 덮으면 조회로 받아 둔 그
값들이 지워집니다. 그래서 화면은 **받은 항목만 갈아 끼웁니다.**

**첫 값이 없습니다.** 구독하면 다음 체결이 일어날 때까지 아무것도 오지 않습니다.
그래서 종목을 열 때는 조회로 한 번 채우고 그 위에 실시간을 덮습니다. 끊겼다
다시 붙었을 때도 같습니다 — 끊긴 동안의 변화는 실시간이 메워 주지 않습니다.

## 체결은 왜 목록인가

시세 값은 마지막 것만 있으면 되지만 **체결은 건마다 필요합니다.** 체결 목록이
한 건씩 쌓여 만들어지기 때문입니다. 그래서 한 번 보낼 때 시세는 마지막 값
하나, 체결은 그 사이에 일어난 것 전부를 배열로 함께 보냅니다.

국내는 체결이 아닌 메시지가 섞여 옵니다. 명세가 「국내 KA/KC 는 틱구분이 1인
것만 이용하고, 0인 값은 체결량이 0인 기타 데이터」라고 적고 있어 그대로
따릅니다. 그런 메시지도 **시세 값은 갈아 끼우고 체결 목록에만 넣지 않습니다** —
가격 자체는 그 시점의 값입니다. 해외에는 이 규칙이 없습니다(명세가 이 규칙을
국내로 한정합니다).

## 체결의 매수·매도는 정하지 않습니다

체결구분(`exec_div`)의 값이 무엇을 뜻하는지 **명세에 설명이 없습니다.** 국내는
`1`·`2`, 해외는 `+`·`-` 로 옵니다. 매매구분(`trd_div`)이 「1:매수, 2:매도」라서
국내가 같은 규칙일 법하지만, 해외가 다른 글자를 쓰는 것을 보면 그 짐작이
맞는다는 보장이 없습니다.

**같은 이름이 다른 뜻으로 쓰이는 곳이 있습니다.** 주문내역 조회의
`exec_div` 는 「0:전체, 1:체결, 2:미체결, 3:미접수」로 매수·매도와 아무 상관이
없습니다. 이름이 같다고 뜻이 같지 않습니다.

**그래서 매수·매도를 정하지 않습니다.** 받은 글자를 `sideCode` 로 그대로
올리고 `side` 는 만들지 않습니다. 짐작으로 색을 칠하면 **틀려도 화면에서
표가 나지 않습니다** — 그것이 이 자리를 비워 두는 이유입니다.

주문/체결 알림의 매수·매도는 다릅니다. `trd_div` 는 명세에 뜻이 적혀 있어
그대로 씁니다.

## 체결로 봉을 잇기

체결마다 `timestamp`(밀리초, 한국시간 기준 순간)를 함께 냅니다. 조회한 분봉의
`timestamp` 와 같은 시계라 화면이 두 자료를 한 축에 놓을 수 있습니다.

* **봉 열쇠** = `floor(timestamp / 60000) * 60000`(1분). 다른 주기는 그 배수.
* **거래량** = 그 분에 든 체결의 `quantity` 합. `patch.volume` 은 당일 누적이라
  봉에 더하지 않습니다.
* **조회 봉과 잇는 규칙**: 조회 응답은 진행 중인 현재 분까지 담고 있습니다.
  체결의 봉 열쇠가 조회의 마지막 봉보다 **앞이면 버리고**, **같으면 그 봉을
  늘리고**(고가·저가·종가 갱신, 거래량 더함), **뒤면 새 봉**입니다. 같은 분은
  조회가 담은 체결과 겹칠 수 있으므로 **첫 체결이 온 뒤 마지막 2분을 조회로
  한 번 다시 받아 덮으면** 그 어긋남이 사라집니다. 끊겼다 붙어 `refill` 을
  받았을 때도 같은 절차입니다.

시각의 출처가 시장마다 다릅니다. **해외 실시간의 `exch_tm` 은 거래소 현지
시각**이고 한국시각은 `ko_exec_tm`·`ko_trd_dt` 에 따로 옵니다 — 해외 분봉은
한국시간이라 그쪽을 씁니다. **국내 실시간에는 날짜가 없어** 받은 순간의 날짜에
시각을 붙이되, 자정 언저리에 받은 체결은 하루 앞뒤 중 받은 순간에 가장 가까운
날로 둡니다(야간 세션이 자정을 넘습니다).

저장해 둔 기록으로 도는 재생 모드에서는 국내 체결의 날짜가 **오늘**이고(기록을
지금 흘려보내므로) 조회 봉은 수집한 날이라, 체결이 조회 봉 위에 앉지 않고 뒤에
새 봉으로 섭니다. 결함이 아니라 재생의 성질입니다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from backend import market_values, orders, quotes
from backend.nh_api.catalog import stream_spec
from backend.sessions.hours import now_kst, to_kst

DOMESTIC_DAY = "domestic-day"
DOMESTIC_NIGHT = "domestic-night"
OVERSEAS = "overseas"
CONTEXTS = (DOMESTIC_DAY, DOMESTIC_NIGHT, OVERSEAS)

QUOTE = "quote"
BOOK = "book"
ORDERS = "orders"
KINDS = (QUOTE, BOOK, ORDERS)

# 맥락·종류가 정하는 NH선물 실시간 이름.
STREAM_OF: dict[tuple[str, str], str] = {
    (DOMESTIC_DAY, QUOTE): "KA",
    (DOMESTIC_DAY, BOOK): "KB",
    (DOMESTIC_DAY, ORDERS): "O1_K",
    (DOMESTIC_NIGHT, QUOTE): "KC",
    (DOMESTIC_NIGHT, BOOK): "KD",
    (DOMESTIC_NIGHT, ORDERS): "O1_K",
    (OVERSEAS, QUOTE): "FA",
    (OVERSEAS, BOOK): "FB",
    (OVERSEAS, ORDERS): "O1",
}

# 맥락이 어느 계좌인가. 접속 토큰과 웹소켓 연결이 이 단위로 갈립니다.
MARKET_OF: dict[str, str] = {
    DOMESTIC_DAY: "domestic",
    DOMESTIC_NIGHT: "domestic",
    OVERSEAS: "overseas",
}

# 호가가 한 번에 오는 단계 수. 채워지지 않은 단계는 빼고 냅니다.
BOOK_DEPTH: dict[str, int] = {DOMESTIC_DAY: 10, DOMESTIC_NIGHT: 10, OVERSEAS: 5}

# 국내에서 체결로 세는 틱구분 값. 나머지는 체결량이 0 인 기타 데이터입니다.
TRADE_TICK = "1"

# 처리구분. 명세에 뜻이 적혀 있는 값입니다.
ORDER_STATUS: dict[str, str] = {
    "1": "accepted",
    "3": "confirmed",
    "4": "partial",
    "6": "filled",
    "8": "auto-cancelled",
    "9": "rejected",
}

# 매매구분. 명세에 뜻이 적혀 있는 값입니다.
ORDER_SIDE: dict[str, str] = {"1": "buy", "2": "sell"}

# 가격조건. **국내와 해외의 뜻이 다릅니다.** 같은 `2` 가 국내에서는 시장가이고
# 해외에서는 지정가입니다. 하나로 합치면 주문 목록에 반대 조건이 적힙니다.
# **값은 명세의 한글 이름 그대로입니다.** 주문내역 조회는 이 뜻을 이름 칸으로
# 바로 주는데(`occurs1_pric_cnd_nm`) 실시간에는 코드만 옵니다. 한쪽만 영문
# 낱말로 옮기면 **화면이 같은 주문을 두 이름으로 받습니다** — 목록 위에 알림을
# 이어 붙이는 자리라 한 벌이어야 합니다.
PRICE_CONDITION: dict[str, dict[str, str]] = {
    "domestic": {"1": "지정가", "2": "시장가", "3": "조건부", "4": "최유리"},
    "overseas": {
        "1": "시장가",
        "2": "지정가",
        "3": "STOP시장가",
        "4": "STOP지정가",
        "5": "OCO",
    },
}

# 매체구분. 이 주문이 어디서 나갔는지입니다. 이 앱이 낸 것이 아니면
# 화면이 정정·취소를 잠급니다.
ORDER_SOURCE: dict[str, str] = {
    "A": "API",
    "R": "REST API",
    "B": "Back",
    "D": "DMA",
    "F": "FRONT",
    "H": "HTS",
    "M": "MTS",
    "I": "WTS",
    "Y": "YES",
    "X": "Expert",
    "Z": "FIX",
}

# 이 앱이 주문을 낼 때 쓰는 길. 이 길로 나간 주문만 화면에서 고칩니다.
OWN_SOURCE = "R"


@dataclass(frozen=True)
class Feed:
    """구독 하나. 같은 값이면 같은 구독입니다."""

    context: str
    kind: str
    sym: str = ""

    def __post_init__(self) -> None:
        if self.context not in CONTEXTS:
            raise ValueError(f"모르는 화면 맥락입니다: {self.context!r}")
        if self.kind not in KINDS:
            raise ValueError(f"모르는 실시간 종류입니다: {self.kind!r}")
        if self.kind == ORDERS:
            if self.sym:
                raise ValueError("주문/체결 알림은 계좌 단위라 종목을 받지 않습니다.")
        elif not self.sym:
            raise ValueError(f"{self.kind} 구독에는 종목이 필요합니다.")

    @property
    def api_id(self) -> str:
        """NH선물 실시간 이름."""
        return STREAM_OF[(self.context, self.kind)]

    @property
    def action_code(self) -> str:
        """구독 메시지에 넣는 값.

        국내 주문/체결은 이름이 `O1_K` 인데 메시지에 넣는 값은 `O1` 이라
        이름과 다릅니다.
        """
        return stream_spec(self.api_id).action_code

    @property
    def market(self) -> str:
        """어느 계좌인가. 웹소켓 연결이 이 단위로 갈립니다."""
        return MARKET_OF[self.context]

    @property
    def is_orders(self) -> bool:
        """주문/체결 알림인지. 예산에서 자리를 미리 잡아 두는 구독입니다."""
        return self.kind == ORDERS

    def as_dict(self) -> dict[str, str]:
        """화면이 보내 온 모양 그대로."""
        return {"context": self.context, "kind": self.kind, "sym": self.sym}


# 국내 체결·호가의 주간·야간 짝. `paired` 가 씁니다.
_DAY_NIGHT: dict[str, str] = {DOMESTIC_DAY: DOMESTIC_NIGHT, DOMESTIC_NIGHT: DOMESTIC_DAY}


def paired(feed: Feed) -> Feed | None:
    """NH선물이 **한 등록으로 묶는** 짝 구독. 없으면 `None`.

    국내 체결·호가는 주간과 야간의 실시간 이름이 다르지만(`KA`/`KC`, `KB`/`KD`),
    같은 종목의 같은 종류는 서버에서 하나로 묶입니다. 한쪽을 해제하면 **짝도
    함께 끊기고, 서버는 그것을 알리지 않습니다.** 두 방향 모두 그렇습니다 —
    주간 호가를 해제하면 야간 호가가, 야간 체결을 해제하면 주간 체결이 끊깁니다.
    끊긴 쪽은 그것만 다시 구독해서는 돌아오지 않고, 해제한 쪽을 다시 등록해야
    돌아옵니다. 그래서 연결은 짝을 늘 함께 등록합니다(`link.Connection`).

    종류가 다른 구독(체결과 호가)과 해외의 체결·호가는 한쪽을 해제해도 끊기지
    않습니다. 주문/체결 알림은 주간·야간이 한 구독이라 짝이 없습니다.
    """
    other = _DAY_NIGHT.get(feed.context)
    if other is None or feed.kind == ORDERS:
        return None
    return Feed(context=other, kind=feed.kind, sym=feed.sym)


def feed_of(raw: Any) -> Feed:
    """화면이 보내 온 구독 하나를 읽습니다. 모양이 틀리면 `ValueError`."""
    if not isinstance(raw, dict):
        raise ValueError("구독은 객체로 보내 주세요.")
    return Feed(
        context=str(raw.get("context") or ""),
        kind=str(raw.get("kind") or ""),
        sym=str(raw.get("sym") or "").strip(),
    )


_clock = market_values.clock


_date = market_values.date


def _positive(text: Any) -> float | None:
    """0 을 「없음」으로 보는 값.

    가격 칸이 채워지지 않으면 빈 값이 아니라 `0` 또는 `000000000000000` 으로
    옵니다. 그대로 내면 화면에 「0원」이 섭니다.
    """
    value = market_values.number(text)
    return None if value is None or value == 0 else value


def _put(into: dict[str, Any], key: str, value: Any) -> None:
    """값이 있을 때만 담습니다. 없는 값을 담으면 화면의 기존 값을 지웁니다."""
    if value is not None:
        into[key] = value


def _number_text(value: Any) -> str | None:
    """자릿수를 지킨 번호 글자. **전부 0 이면 비운 것으로 봅니다.**

    체결번호가 그렇습니다 — 주문/체결 알림의 접수 줄은 `0000000000` 으로,
    체결 줄만 실제 번호로 옵니다. 0 을 번호로 담으면 접수 알림들이 **서로 같은
    번호**를 갖게 되어, 체결을 가리려고 둔 칸이 오히려 줄을 뭉칩니다.

    **수로 바꾸지 않습니다.** 앞자리 0 이 뜻을 가질 수 있고, 주문번호도 글자로
    다룹니다(`backend/orders.py`).
    """
    body = str(value or "").strip()
    if not body or set(body) == {"0"}:
        return None
    return body


def quote_patch(body: dict[str, Any], *, context: str) -> dict[str, Any]:
    """시세 메시지에서 화면의 `Quote` 에 갈아 끼울 항목만 뽑습니다.

    **`Quote` 전체가 아닙니다.** 여기 없는 항목은 조회와 종목 마스터가 채웁니다.
    """
    made: dict[str, Any] = {}
    _put(made, "last", market_values.number(body.get("last_pric")))
    # 실시간은 전일대비를 `diff_rt` 에, 등락률을 `rt` 에 담습니다. 조회는
    # 전일대비가 `chg` 입니다 — 같은 값의 이름이 경로마다 다릅니다.
    _put(made, "change", market_values.number(body.get("diff_rt")))
    _put(made, "changePct", market_values.number(body.get("rt")))
    _put(made, "open", market_values.number(body.get("open_pric")))
    _put(made, "high", market_values.number(body.get("high_pric")))
    _put(made, "low", market_values.number(body.get("low_pric")))
    _put(made, "volume", market_values.count(body.get("cum_trd_qty")))
    _put(made, "openInterest", market_values.count(body.get("opint")))

    if MARKET_OF[context] == "domestic":
        # 거래대금은 백만 단위로 옵니다. 조회와 같은 단위라 바꾸지 않습니다.
        _put(made, "turnover", market_values.number(body.get("cum_trd_amt")))
        _put(made, "openInterestChange", market_values.number(body.get("opint_diff")))
        # 상한가·하한가가 아니라 **실시간** 상·하한입니다.
        _put(made, "upperLimit", market_values.number(body.get("up_act_pric")))
        _put(made, "lowerLimit", market_values.number(body.get("lo_act_pric")))
        # 야간에는 현물가가 0 으로 옵니다. 「0원」이 아니라 「없음」입니다.
        _put(made, "spot", _positive(body.get("und_last_pric")))
        _put(made, "theory", _positive(body.get("theo_pric")))
        _put(made, "disparity", market_values.number(body.get("dev_rt")))
        _put(made, "iv", _positive(body.get("ivol")))
        # 실시간이 보내는 베이시스는 마켓베이시스 하나입니다. 조회의
        # 이론베이시스(`theo_bas`)는 실시간에 오지 않습니다.
        _put(made, "basisActual", market_values.number(body.get("mkt_bas")))
        if context == DOMESTIC_NIGHT:
            # 야간에는 기초자산현재가 칸에 현물 지수가 아닌 값이 옵니다 — 움직이지
            # 않는 엉뚱한 수라 현물가로 쓸 수 없고, 그 값으로 센 마켓베이시스도 뜻이
            # 없습니다. 야간 현재가 조회와 같은 규칙으로 **비웁니다.** 칸을 빼지 않고
            # `null` 로 싣는 것은, 받은 칸만 갈아 끼우는 화면에 주간에 받아 둔
            # 현물가·베이시스가 야간까지 남지 않게 하려는 것입니다.
            made["spot"] = None
            made["basisActual"] = None
        _put_expected_price(made, body)
    return made


def _put_expected_price(made: dict[str, Any], body: dict[str, Any]) -> None:
    """예상체결가. **0 이나 빈 값이면 `null` 을 담아 화면의 값을 지웁니다.**

    단일가 시간(개장·마감 동시호가)에만 값이 있고 접속매매로 돌아오면 `0` 으로
    옵니다. 다른 항목처럼 없는 값을 안 담으면 동시호가 때의 예상체결가가 화면에
    그대로 남습니다. 칸 자체가 안 온 메시지(해외)에는 담지 않습니다.
    """
    if "est_exec_pric" in body:
        made["expectedPrice"] = _positive(body.get("est_exec_pric"))


def trade_of(
    body: dict[str, Any], *, context: str, received: datetime | None = None
) -> dict[str, Any] | None:
    """시세 메시지에서 체결 한 건을 뽑습니다. 체결이 아니면 `None`.

    `received` 는 이 메시지를 받은 순간입니다(시험이 넣습니다). 국내 체결의
    날짜를 정하는 데만 씁니다 — 국내 실시간에는 날짜가 없습니다.

    매수·매도는 정하지 않습니다 — 파일 첫머리의 「체결의 매수·매도는 정하지
    않습니다」 참고.
    """
    domestic = MARKET_OF[context] == "domestic"
    if domestic and str(body.get("tick_div") or "").strip() != TRADE_TICK:
        return None

    quantity = market_values.count(body.get("exec_qty"))
    if not quantity:
        return None

    price = market_values.number(body.get("last_pric"))
    if price is None:
        return None

    made: dict[str, Any] = {"price": price, "quantity": quantity}
    if domestic:
        clock = body.get("exch_tm")
        _put(made, "time", _clock(clock))
        _put(made, "timestamp", _domestic_stamp(clock, received or now_kst()))
    else:
        # 해외의 `exch_tm` 은 거래소 현지 시각입니다. 한국시각 칸을 씁니다.
        clock = body.get("ko_exec_tm")
        _put(made, "time", _clock(clock))
        _put(made, "timestamp", quotes.timestamp(body.get("ko_trd_dt"), clock))
    side_code = str(body.get("exec_div") or "").strip()
    if side_code:
        made["sideCode"] = side_code
    return made


def _domestic_stamp(clock: Any, received: datetime) -> int | None:
    """국내 체결의 순간(밀리초). 시각만 오므로 날짜는 받은 순간에서 가져옵니다.

    자정 언저리가 문제입니다 — 23:59:59 체결이 00:00:00 에 도착하면 받은 날짜를
    그대로 붙일 때 하루 뒤로 밀립니다. 그래서 어제·오늘·내일 중 받은 순간에
    **가장 가까운** 날로 둡니다. 체결이 받은 순간보다 몇 시간씩 앞서거나 뒤설 일은
    없으므로 이 셋 밖은 볼 필요가 없습니다.
    """
    body = str(clock or "").strip()
    if len(body) != 6 or not body.isdigit():
        return None
    moment = to_kst(received)
    candidates = []
    for offset in (-1, 0, 1):
        day = (moment + timedelta(days=offset)).strftime("%Y%m%d")
        stamp = quotes.timestamp(day, body)
        if stamp is not None:
            candidates.append(stamp)
    if not candidates:
        return None
    here = int(moment.timestamp() * 1000)
    return min(candidates, key=lambda stamp: abs(stamp - here))


def book_of(body: dict[str, Any], *, context: str) -> dict[str, Any]:
    """호가 메시지를 화면 모양으로.

    `asks` 는 **먼 값부터**, `bids` 는 **가까운 값부터**입니다. 값이 안 채워진
    단계는 빠지므로 둘 다 빈 배열일 수 있습니다.

    `time` 은 한국시각(`HH:MM:SS`)입니다. 국내는 `exch_tm` 이 한국 거래소의
    시각이라 그대로 쓰고, 해외는 `exch_tm` 이 거래소 현지 시각이라 한국시각 칸
    `ko_exec_tm` 을 씁니다 — 체결과 같은 기준입니다. 한국시각 칸이 비어 오면
    현지 시각으로 채우지 않고 `time` 을 뺍니다. 다른 시간대의 시각을 한국시각
    자리에 두는 것보다 없는 편이 낫습니다.
    """
    depth = BOOK_DEPTH[context]
    steps = range(1, depth + 1)
    made: dict[str, Any] = {
        "asks": market_values.asks(
            (body.get(f"ask_pric_{i}"), body.get(f"ask_qty_{i}")) for i in steps
        ),
        "bids": market_values.bids(
            (body.get(f"bid_pric_{i}"), body.get(f"bid_qty_{i}")) for i in steps
        ),
    }
    domestic = MARKET_OF[context] == "domestic"
    _put(made, "time", _clock(body.get("exch_tm" if domestic else "ko_exec_tm")))
    if domestic:
        _put_expected_price(made, body)
    return made


#: **이 상태가 되면 더 고칠 수 없습니다** — 체결완료·자동취소·거부.
#: 값은 이 파일의 `ORDER_STATUS` 것이고, 조회 쪽 표와 같아 보여도 함께 쓰지
#: 않습니다(같은 이름이 API 마다 다른 뜻으로 쓰입니다).
FINISHED_STATUS = frozenset({"6", "8", "9"})


def _editable(made: dict[str, Any], source_code: str) -> bool:
    """화면에서 고칠 수 있는 주문인지. **주문내역 줄과 같은 규칙입니다.**

    셋을 다 만족해야 참입니다 — 이 앱의 매체, 잔량 남음, 끝나지 않은 상태.
    다른 길로 낸 주문은 이 앱의 주문 폼으로 표현할 수 없어 잠그고, 체결이 끝났거나
    취소·거부된 주문은 더 손댈 수 없습니다.

    **잔량을 모르면 거짓입니다.** 「잔량이 0」과 「잔량 칸이 안 왔다」는 다른
    일이지만 답은 같습니다 — 고칠 수 있다고 말할 근거가 없습니다.
    """
    if source_code != OWN_SOURCE:
        return False

    remaining = made.get("remaining")
    if remaining is None or remaining <= 0:
        return False

    return made.get("statusCode") not in FINISHED_STATUS


def order_event(body: dict[str, Any], *, context: str) -> dict[str, Any]:
    """주문/체결 알림 한 건을 화면 모양으로.

    **한 건도 버리지 않습니다.** 접수·확인·체결이 각각 한 건으로 오고, 화면은
    주문번호로 이어 붙여 진행을 그립니다.

    **칸 이름과 값 체계는 주문내역 조회와 한 벌입니다** — 화면이 조회로 받은
    목록 위에 이 알림을 이어 붙이기 때문입니다. 이어 붙일 열쇠(`id`)부터 두
    경로가 다르면 합칠 수가 없습니다. 그래서 주문번호는 `id`, 종목은 `code`,
    주문 종류는 명세의 한글 이름(`orderType`)으로 냅니다 — 조회 쪽이 이름 칸을
    그대로 주므로 실시간이 거기 맞춥니다. 한쪽을 고치면 **다른 쪽도 함께**
    고쳐야 하고, `tests/test_domestic_account.py` 가 둘이 갈라지는 것을 봅니다.
    """
    # 주문/체결 알림에는 밤낮 구분이 없습니다 — 계좌가 하나라 주간 화면과 야간
    # 화면이 같은 알림을 봅니다. 그래서 맥락이 아니라 계좌를 적습니다.
    market = MARKET_OF[context]
    made: dict[str, Any] = {"market": market}

    _put(made, "id", str(body.get("ord_no") or "").strip() or None)
    _put(made, "originalOrderNo", orders.original_no(body.get("org_ord_no"), body.get("ord_no")))
    _put(made, "code", str(body.get("sym") or "").strip() or None)

    status_code = str(body.get("ord_sts") or "").strip()
    if status_code:
        made["statusCode"] = status_code
        _put(made, "status", ORDER_STATUS.get(status_code))

    side_code = str(body.get("trd_div") or "").strip()
    if side_code:
        made["sideCode"] = side_code
        _put(made, "side", ORDER_SIDE.get(side_code))

    condition_code = str(body.get("pric_cnd") or "").strip()
    if condition_code:
        made["orderTypeCode"] = condition_code
        _put(made, "orderType", PRICE_CONDITION[market].get(condition_code))

    source_code = str(body.get("med_div") or "").strip()
    if source_code:
        made["sourceCode"] = source_code
        _put(made, "source", ORDER_SOURCE.get(source_code))

    _put(made, "quantity", market_values.count(body.get("ord_qty")))
    _put(made, "filled", market_values.count(body.get("exec_qty")))
    _put(made, "remaining", market_values.count(body.get("rem_ord_qty")))
    _put(made, "cancelled", market_values.count(body.get("cxl_qty")))
    # 시장가 주문은 주문가격이 0 으로 옵니다. 가격이 없는 것이 맞습니다.
    _put(made, "price", _positive(body.get("ord_pric")))
    _put(made, "fillPrice", _positive(body.get("exec_pric")))
    _put(made, "execNo", _number_text(body.get("exec_no")))
    # 화면이 주문내역 목록 위에 이 알림을 이어 붙이므로 칸 이름을 그쪽에
    # 맞춥니다. 체결됐으면 체결시각, 아니면 접수시각입니다.
    _put(made, "time", _clock(body.get("exec_tm")) or _clock(body.get("rcv_tm")))
    _put(made, "date", _date(body.get("ord_dt")))
    _put(made, "errorCode", str(body.get("err_cd") or "").strip() or None)

    if source_code:
        # **잔량과 상태를 담은 뒤에 봅니다.** 화면은 주문내역 목록 위에 이
        # 알림을 이어 붙이므로, 여기서 매체만 보고 참을 내면 **방금 목록에서
        # 거짓으로 정한 값을 알림이 덮습니다** — 끝난 주문에 정정 단추가
        # 다시 섭니다.
        made["editable"] = _editable(made, source_code)
    # 체결평균가(`exec_avg_pric`)는 체결된 뒤에도 0 으로 옵니다. 채워지지
    # 않는 칸으로 보고 쓰지 않습니다.
    return made
