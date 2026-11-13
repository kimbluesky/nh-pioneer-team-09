"""NH선물 서버에 접속하지 않고, 저장해 둔 응답으로 대신 답합니다.

NH선물 서버 없이 화면을 띄우고 처리 코드를 고칠 때 씁니다. 기본은 실서버이고,
`NH_REPLAY=1` 로 켭니다.

주의할 점이 셋 있습니다.

1. **접속 토큰과 웹소켓 접속키는 만들어 낸 값입니다.** 수집된 응답이 없어
   재생할 것이 없습니다. 값 자체에 만들어 낸 값이라는 표시가 들어 있고,
   응답 헤더 `x-nh-replay-match` 도 `synthetic` 으로 옵니다.
2. **요청을 고르는 인자가 같은 기록으로만 답합니다.** 날짜·시각·이어받기 키가
   달라도(화면은 차트를 오늘 날짜로 부릅니다) 조회구분 같은 인자가 같은 기록이면
   그것으로 답하고, 헤더와 기록에 그 사실을 남깁니다. 종목을 받는 API 는 같은
   종목의 기록만 씁니다. **그런 기록이 없으면 빈 응답(0건)입니다** — 다른 인자나
   다른 종목의 기록으로 답하면 화면이 틀린 줄을 맞는 줄로 보게 됩니다. 어느
   정도 다른 요청에도 답이 오므로 재생 모드의 응답은 요청에 대한 답이 아닐 수
   있고, 헤더 `x-nh-replay-match` 가 그것을 말합니다.
3. 실시간도 같은 규칙입니다. 구독한 종목의 기록이 없으면 다른 종목의 기록을
   대신 흘려보내고 경고를 남깁니다. 그리고 저장해 둔 메시지를 순서대로
   내보내는 것뿐이라, 요청에 따라 잔고나 체결이 달라지는 모의 거래가 아닙니다.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx

from backend.nh_api.catalog import HTTP_APIS, Limit
from backend.nh_api.fixtures import FixtureStore, HttpFixture, StreamFixture, canonical

logger = logging.getLogger(__name__)

# 재생 모드에서 쓰는 주소입니다. 실제로 존재하지 않는 이름이라 이름 풀이가
# 일어나도 밖으로 나가지 않고, 환경을 주소로 구분할 수 있습니다.
REPLAY_HOSTS = {"paper": "paper.replay.invalid", "real": "real.replay.invalid"}

# 만들어 낸 인증값. 실제 값처럼 보이지 않도록 일부러 이런 글자를 씁니다.
SYNTHETIC_TOKEN = "REPLAY-SYNTHETIC-TOKEN-NOT-ISSUED-BY-NH"
SYNTHETIC_WEBSOCKET_KEY = "REPLAY-SYNTHETIC-WEBSOCKET-KEY-NOT-ISSUED-BY-NH"

TOKEN_PATH = "/auth-service/v1/token"
REVOKE_PATH = "/auth-service/v1/revoke"
WEBSOCKET_KEY_PATH = "/permission/v1/web-socket-key"

SYNTHETIC_PATHS = {
    ("POST", TOKEN_PATH),
    ("POST", REVOKE_PATH),
    ("GET", WEBSOCKET_KEY_PATH),
}

# 재생한 방식을 알리는 헤더 값.
MATCH_EXACT = "exact"
MATCH_API = "api"
MATCH_CROSS_ENVIRONMENT = "cross-environment"
MATCH_SYNTHETIC = "synthetic"
# 고르는 인자가 같은 기록이 없어 빈 응답으로 답한 경우.
MATCH_EMPTY = "empty"

# 빈 응답의 본문. 실제 0건 응답이 이 모양입니다 — 배열 칸(`Occurs1`)이 아예 없고
# 건수와 이어받기 키만 옵니다. 변환하는 쪽은 배열이 없으면 0건으로 읽습니다.
EMPTY_BODY: dict[str, str] = {"nrec": "0000", "nxt_key": ""}

# 요청에서 **기록을 고르는 인자가 아닌** 칸. 이 칸이 달라도 같은 요청으로 봅니다.
#
# 종목은 따로 봅니다(같은 종목의 기록만 쓰되, 없는 종목이면 빈 응답). 날짜·시각은
# 화면이 오늘 날짜로 부르므로 수집한 날과 늘 다르고, 이어받기 키는 쪽을 가리킬
# 뿐 자료를 가르지 않습니다. 이것들까지 보면 재생에서 차트·예수금이 통째로 빕니다.
NON_SELECTING_FIELDS: frozenset[str] = frozenset(
    {
        "sym",
        # 해외 현재가의 종목 목록. 종목마다 따로 고르므로 여기서는 안 봅니다.
        "Iccurs1",
        "biz_dt",
        "inq_dt",
        "inq_strt_dt",
        "inq_ed_dt",
        "ref_dt",
        "strt_tm",
        "ed_tm",
        "nxt_key",
        "req_qty",
        "nxt_stkr_pric",
        # 해외 만기일 조회의 잔존일 기준. 값이 크면 작은 값을 품으므로 안 보고,
        # 같은 종목의 기록 중 줄이 담긴 것을 고릅니다.
        "rem_dy",
    }
)

#: **이어받기 키가 쪽을 가르는 API.** 그 API 에서는 키를 고르는 인자로 봅니다.
#:
#: 해외 분봉은 **날짜 인자를 안 봅니다** — 어느 날짜를 넣어도 가장 최근 쪽이
#: 오고, 과거로 가는 길은 키 하나뿐입니다(`overseas_history.py`). 그래서 키를
#: 안 보면 **어느 쪽을 물어도 같은 쪽**이 와서, 재생에서는 한 쪽만 돌고 맙니다.
#:
#: 다른 API 는 그대로 둡니다. 거기서는 키가 쪽을 가리킬 뿐 자료를 가르지 않아,
#: 보기 시작하면 차트·예수금이 통째로 빕니다.
CURSOR_SELECTS: frozenset[str] = frozenset({"/trade/v1/overseas/chart-minute"})

# 명세가 「0 또는 공백이면 전체」라고 적은 칸과 그 값. 요청이 전체면 그 칸이 무엇인
# 기록이든 맞는 것으로 봅니다 — 전체 조회는 부분 조회를 품습니다. 반대로 기록이
# 전체이고 요청이 특정 값이면 맞지 않습니다: 미체결만 물었는데 전체 기록으로
# 답하면 체결 끝난 줄이 미체결로 섭니다. 명세의 설명 칸에서 옮긴 목록입니다.
WILDCARDS: dict[str, str] = {
    "exec_div": "0",  # 국내 주문내역 체결구분
    "trd_div": "0",  # 국내 주문내역 매매구분
    "trd_tm_div_cd": "0",  # 국내 주문내역 거래시간구분
    "ord_div_cd": "0",  # 국내 주문내역 주문구분
    "trd_div_cd": "0",  # 국내 주문오류·해외 주문내역 매수매도구분
    "inq_div": "0",  # 해외 주문내역 조회구분
    "fut_opt_div_cd": "",  # 해외 장운영정보·품목별세부정보·품목만기 상품구분
    "prd_grp_cd": "",  # 해외 품목그룹
    "cur_cd": "",  # 해외 통화
    "exch_cd": "",  # 해외 거래소(장운영정보·품목별세부정보에서만 공백이 전체)
    "prd_cd": "",  # 해외 검색구분
    "prd_nm": "",  # 해외 품목명(검색구분이 공백이면 안 봄)
    "sett_cd": "",  # 해외 결제구분
    "und_sym": "",  # 해외 미결제만기 기초종목
}

# 대신 답할 때, 그 기록이 **요청한 종목의 것인지**.
#
# 단계(`api`·`cross-environment`)만으로는 「값이 왜 이런가」를 못 가립니다.
# 같은 `api` 라도 요청한 종목의 다른 날 기록이면 값은 그 종목의 것입니다.
# `other`(다른 종목의 기록)는 이제 나가지 않습니다 — 다른 종목의 기록으로는
# 답하지 않고 빈 응답으로 답합니다. 낱말은 화면이 알고 있어 그대로 둡니다.
# 종목을 안 받는 API 와 빈 응답은 `none` 입니다.
SYMBOL_SAME = "same"
SYMBOL_OTHER = "other"
SYMBOL_NONE = "none"

# 응답에 붙는 헤더 이름.
REPLAY_HEADER = "x-nh-replay"
MATCH_HEADER = "x-nh-replay-match"
SYMBOL_HEADER = "x-nh-replay-symbol"
RECORD_HEADER = "x-nh-replay-record"

# 요청에서 종목을 가리키는 항목. 국내와 해외가 같은 이름을 씁니다.
SYMBOL_FIELD = "sym"

# 여러 종목을 한 번에 묻는 요청(해외 현재가)의 목록 칸과, 그 안·응답 줄의 종목 칸.
SYMBOLS_FIELD = "Iccurs1"
SYMBOLS_ITEM_FIELD = "iccurs1_sym"
ROW_SYMBOL_FIELD = "occurs1_sym"

# 주문·정정·취소의 경로. 이 경로는 요청이 통째로 같은 기록으로만 답합니다.
ORDER_PATHS: frozenset[tuple[str, str]] = frozenset(
    (spec.method, spec.path) for spec in HTTP_APIS.values() if spec.limit == Limit.ORDER
)

# 방금 부른 조회가 무엇으로 답한 것인지.
#
# 이 표시는 httpx 응답 헤더에만 붙으므로, 카테고리 함수가 본문만 꺼내 가면
# **화면은 자기가 보는 값이 대신 답한 것인지 알 수 없습니다.** 라우트가
# 응답을 만들 때 여기서 읽어 화면 쪽 헤더로 옮깁니다.
#
# 요청마다 갈리는 값이라 `ContextVar` 에 둡니다. 요청 하나가 한 작업 안에서
# 처리되므로 동시에 들어온 다른 요청의 값과 섞이지 않습니다.
last_replay: ContextVar[dict[str, str] | None] = ContextVar("nh_last_replay", default=None)


def note_replay(headers: Mapping[str, str]) -> None:
    """응답 헤더에서 재생 표시를 읽어 둡니다. 실서버 응답이면 지웁니다."""
    if headers.get(REPLAY_HEADER):
        last_replay.set(
            {
                "match": headers.get(MATCH_HEADER, ""),
                "symbol": headers.get(SYMBOL_HEADER, ""),
                "record": headers.get(RECORD_HEADER, ""),
            }
        )
    else:
        last_replay.set(None)


def symbol_of(payload: Any) -> str:
    """요청이 가리키는 종목. 종목을 안 받는 API 면 빈 문자열."""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get(SYMBOL_FIELD) or "").strip()


def symbols_of(payload: Any) -> list[str]:
    """여러 종목을 한 번에 묻는 요청의 종목들(부른 차례). 그런 요청이 아니면 빈 목록."""
    if not isinstance(payload, dict):
        return []
    items = payload.get(SYMBOLS_FIELD)
    if not isinstance(items, list):
        return []
    made = []
    for item in items:
        if isinstance(item, dict):
            sym = str(item.get(SYMBOLS_ITEM_FIELD) or "").strip()
            if sym:
                made.append(sym)
    return made


def replay_base_url(environment: str) -> str:
    """그 환경의 재생 주소."""
    try:
        return f"http://{REPLAY_HOSTS[environment]}"
    except KeyError:
        raise ValueError(f"모르는 환경입니다: {environment!r}") from None


def environment_of(host: str) -> str | None:
    for name, replay_host in REPLAY_HOSTS.items():
        if host == replay_host:
            return name
    return None


class ReplayMiss(httpx.TransportError):
    """재생할 응답을 찾지 못한 경우입니다."""


class ReplayTransport(httpx.MockTransport):
    """저장해 둔 응답으로 답하는 전송 계층."""

    def __init__(self, store: FixtureStore) -> None:
        super().__init__(self.handle)
        self.store = store
        # 어떤 요청을 어떤 기록으로 답했는지 남깁니다. 화면이 이상할 때
        # 무엇이 대신 답한 것인지 확인하는 자리입니다.
        self.log: list[dict[str, str]] = []

    # -- 요청 읽기 -------------------------------------------------------

    @staticmethod
    def _payload(request: httpx.Request) -> Any:
        if request.method == "GET":
            return {}
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("application/x-www-form-urlencoded"):
            return {k: v[0] for k, v in parse_qs(request.content.decode(), keep_blank_values=True).items()}
        if not request.content:
            return {}
        try:
            return json.loads(request.content)
        except ValueError:
            raise ReplayMiss("요청 본문을 읽을 수 없습니다.") from None

    # -- 응답 고르기 -----------------------------------------------------

    def choose(
        self,
        *,
        method: str,
        path: str,
        payload: Any,
        environment: str,
    ) -> tuple[HttpFixture | None, str, str]:
        """이 요청에 답할 기록과, 어떻게 골랐는지를 돌려줍니다.

        **고르는 인자가 같은 기록만 후보입니다.** 고르는 인자는 요청에서 종목·
        날짜·시각·이어받기 키(`NON_SELECTING_FIELDS`)를 뺀 나머지 전부이고
        (**해외 분봉만 키를 봅니다** — `CURSOR_SELECTS`),
        명세가 「전체」라고 적은 값은 어떤 기록과도 맞습니다(`WILDCARDS`). 종목을
        받는 API 는 그중 **같은 종목의 기록**만 씁니다 — 차트를 오늘 날짜로 부르면
        수집한 날과 달라도 같은 종목의 기록이 답이고, 다른 종목의 봉은 답이 아닙니다.

        후보 안의 차례는 셋입니다.

            1. 그 환경에서 요청이 통째로 같은 기록 (`exact`)
            2. 그 환경의 후보 (`api`)
            3. 다른 환경에서 1·2 (`cross-environment`)

        어느 환경에도 후보가 없으면 **빈 응답**입니다(`empty`) — 조회구분이 다른
        기록으로 답하면 화면이 틀린 줄을 맞는 줄로 보게 됩니다. 그 API 의 기록이
        하나도 없는 것은 다릅니다: 그때는 답하지 않습니다(`ReplayMiss`). 「자료가
        없다」와 「기록이 없다」를 섞지 않기 위해서입니다.

        **주문은 통째로 같은 기록만 씁니다.** 수량이나 가격이 다른 주문에 기록된
        접수로 답하면 낸 적 없는 주문이 접수된 것처럼 보이고, 빈 응답이면 주문번호
        없는 접수가 됩니다. 어느 쪽도 답이 아니라 없으면 답하지 않습니다.

        여럿 중 하나를 고르는 잣대는 어느 자리에서나 **정상 → 줄 담김 → 이른 것**
        입니다. 고른 결과가 요청한 종목의 것인지는 세 번째 값으로 함께 돌려줍니다.

        **종목 목록을 받는 요청(해외 현재가의 `Iccurs1`)은 종목마다 따로 고릅니다.**
        기록은 대개 한 종목씩이라 목록을 통째로 견주면 두 종목만 물어도 빈 응답이
        됩니다. 종목마다 기록을 찾아 그 줄을 모아 한 응답으로 만들고, 기록 없는
        종목은 빠집니다 — 전부 없을 때만 빈 응답입니다(`_choose_many`).
        """
        wanted = symbol_of(payload)
        same_env = self.store.find_http(method=method, path=path, environment=environment)
        others = tuple(f for f in self.store.http if f.method == method and f.path == path)
        if not others:
            raise ReplayMiss(f"저장해 둔 응답에 {method} {path} 기록이 없습니다.")

        if (method, path) in ORDER_PATHS:
            for fixtures, match in ((same_env, MATCH_EXACT), (others, MATCH_CROSS_ENVIRONMENT)):
                same_request = _same_request(fixtures, payload)
                if same_request:
                    return _prefer_success(same_request), match, _symbol_state(wanted, wanted)
            raise ReplayMiss(f"저장해 둔 응답에 이 주문과 같은 {method} {path} 기록이 없습니다.")

        requested = symbols_of(payload)
        # 목록 요청은 종목이 목록 안에 있어 통째로 같으면 곧 같은 종목의 기록입니다.
        symbol = SYMBOL_SAME if requested else _symbol_state(wanted, wanted)
        for fixtures, cross in ((same_env, False), (others, True)):
            same_request = _same_request(fixtures, payload)
            if same_request:
                match = MATCH_CROSS_ENVIRONMENT if cross else MATCH_EXACT
                return _prefer_success(same_request), match, symbol
            if requested:
                continue
            candidates = _candidates(fixtures, payload, wanted)
            if candidates:
                match = MATCH_CROSS_ENVIRONMENT if cross else MATCH_API
                return _prefer_success(candidates), match, symbol

        if requested:
            return _choose_many(same_env, others, payload, requested)

        # 답한 기록이 없으니 종목 판정도 없습니다.
        return None, MATCH_EMPTY, SYMBOL_NONE

    # -- 만들어 낸 인증 응답 ---------------------------------------------

    @staticmethod
    def synthetic(path: str) -> dict[str, Any]:
        """접속 토큰과 웹소켓 접속키는 수집된 응답이 없어 만들어 냅니다."""
        if path == TOKEN_PATH:
            expires_at = datetime.now() + timedelta(hours=1)
            return {
                "access_token": SYNTHETIC_TOKEN,
                "token_type": "Bearer",
                "expires_in": "3600",
                "expires_at": expires_at.strftime("%Y-%m-%d %H:%M:%S"),
            }
        if path == WEBSOCKET_KEY_PATH:
            return {"access_key": SYNTHETIC_WEBSOCKET_KEY}
        return {}

    # -- 처리 -----------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        environment = environment_of(request.url.host)
        if environment is None:
            raise ReplayMiss(
                f"재생 모드에서는 {request.url.host} 로 요청하지 않습니다. "
                "재생 주소로만 부를 수 있습니다."
            )

        method = request.method
        path = request.url.path

        if (method, path) in SYNTHETIC_PATHS:
            self._record(method, path, MATCH_SYNTHETIC, SYMBOL_NONE, "")
            return self._response(200, self.synthetic(path), MATCH_SYNTHETIC, SYMBOL_NONE, "")

        fixture, match, symbol = self.choose(
            method=method,
            path=path,
            payload=self._payload(request),
            environment=environment,
        )
        if fixture is None:
            logger.warning(
                "재생: %s %s 요청을 고르는 인자가 같은 기록이 없어 빈 응답으로 답합니다.",
                method,
                path,
            )
            self._record(method, path, match, symbol, "")
            return self._response(200, dict(EMPTY_BODY), match, symbol, "")
        if match != MATCH_EXACT:
            # 종목을 안 받는 API(계좌·전체시세)에는 종목 이야기를 쓰지 않습니다.
            about = {SYMBOL_SAME: "같은 종목의 "}.get(symbol, "")
            logger.warning(
                "재생: %s %s 요청과 같은 기록이 없어 %s %s기록(%s)으로 답합니다.",
                method,
                path,
                fixture.environment,
                about,
                fixture.record_id,
            )
        self._record(method, path, match, symbol, fixture.record_id)
        return self._response(
            fixture.http_status, fixture.response, match, symbol, fixture.record_id
        )

    def _record(
        self, method: str, path: str, match: str, symbol: str, record_id: str
    ) -> None:
        self.log.append(
            {
                "method": method,
                "path": path,
                "match": match,
                "symbol": symbol,
                "record_id": record_id,
            }
        )

    @staticmethod
    def _response(
        status: int, body: Any, match: str, symbol: str, record_id: str
    ) -> httpx.Response:
        headers = {REPLAY_HEADER: "1", MATCH_HEADER: match, SYMBOL_HEADER: symbol}
        if record_id:
            # 헤더에는 ASCII 만 담을 수 있습니다. 기록 번호는 원래 ASCII 이지만,
            # 아닌 값이 들어와도 응답이 깨지지 않게 해 둡니다.
            headers[RECORD_HEADER] = record_id.encode("ascii", "replace").decode("ascii")
        return httpx.Response(status, json=body, headers=headers)


def selecting(payload: Any, path: str = "") -> dict[str, Any]:
    """요청에서 기록을 고르는 인자만. 종목·날짜·시각·이어받기 키는 뺍니다.

    **`path` 가 `CURSOR_SELECTS` 면 이어받기 키는 봅니다** — 그 API 는 키가
    쪽을 가르는 자리입니다.
    """
    if not isinstance(payload, dict):
        return {}
    skipped = NON_SELECTING_FIELDS - {"nxt_key"} if path in CURSOR_SELECTS else NON_SELECTING_FIELDS
    return {k: v for k, v in payload.items() if k not in skipped}


def selects_same(request: Any, recorded: Any, path: str = "") -> bool:
    """이 요청이 그 기록을 골라도 되는지 — 고르는 인자가 같거나 요청 쪽이 전체.

    **양쪽에 다 있는 칸만 견줍니다.** 한쪽에만 있는 칸은 값이 다른 것이 아니라
    견줄 수 없는 것이라, 그것 때문에 후보에서 빼지 않습니다 — 기록을 만든 요청과
    이 앱의 요청은 싣는 칸이 조금 다를 수 있고, 여기서 막으려는 것은 「같은 칸에
    다른 값」입니다.
    """
    asked = selecting(request, path)
    kept = selecting(recorded, path)
    for key, value in asked.items():
        if key not in kept:
            continue
        if _text(value) == WILDCARDS.get(key, None):
            continue
        if _text(value) != _text(kept[key]):
            return False
    return True


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _choose_many(
    same_env: tuple[HttpFixture, ...],
    others: tuple[HttpFixture, ...],
    payload: Any,
    requested: list[str],
) -> tuple[HttpFixture | None, str, str]:
    """여러 종목을 한 번에 묻는 요청에 종목마다 기록을 찾아 한 응답으로 모읍니다.

    종목마다 그 환경 → 다른 환경 차례로 찾고, 찾은 기록에서 **그 종목의 줄**만
    가져와 부른 차례대로 늘어놓습니다. 어느 하나라도 다른 환경에서 왔으면
    `cross-environment`, 아니면 `api` 입니다. 기록 없는 종목은 빠지고, 전부
    없으면 빈 응답입니다. 모은 응답의 기록 번호는 쓴 기록들을 `+` 로 이은 것입니다.
    """
    rows: list[Any] = []
    used: list[HttpFixture] = []
    crossed = False
    for sym in requested:
        for fixtures, cross in ((same_env, False), (others, True)):
            pool = tuple(
                f
                for f in fixtures
                if selects_same(payload, f.request, f.path) and sym in symbols_of(f.request)
            )
            if not pool:
                continue
            fixture = _prefer_success(pool)
            row = _row_of(fixture, sym)
            if row is None:
                continue
            rows.append(row)
            used.append(fixture)
            crossed = crossed or cross
            break

    if not rows:
        return None, MATCH_EMPTY, SYMBOL_NONE

    record_id = "+".join(dict.fromkeys(f.record_id for f in used))
    made = dataclasses.replace(
        used[0],
        record_id=record_id,
        http_status=200,
        response={"nrec": f"{len(rows):04d}", "Occurs1": rows},
    )
    return made, (MATCH_CROSS_ENVIRONMENT if crossed else MATCH_API), SYMBOL_SAME


def _row_of(fixture: HttpFixture, sym: str) -> Any:
    """기록의 응답에서 그 종목의 줄. 없으면 `None`."""
    body = fixture.response if isinstance(fixture.response, dict) else {}
    rows = body.get("Occurs1")
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and str(row.get(ROW_SYMBOL_FIELD) or "").strip() == sym:
            return row
    return None


def _candidates(
    fixtures: tuple[HttpFixture, ...], payload: Any, wanted: str
) -> tuple[HttpFixture, ...]:
    """고르는 인자가 같고, 종목을 받는 요청이면 종목도 같은 기록 전부."""
    return tuple(
        f
        for f in fixtures
        if selects_same(payload, f.request, f.path) and (not wanted or symbol_of(f.request) == wanted)
    )


def _symbol_state(wanted: str, found: str) -> str:
    """고른 기록이 요청한 종목의 것인지."""
    if not wanted:
        return SYMBOL_NONE
    return SYMBOL_SAME if found == wanted else SYMBOL_OTHER


def _same_request(
    fixtures: tuple[HttpFixture, ...], payload: Any
) -> tuple[HttpFixture, ...]:
    """요청이 통째로 같은 기록 전부."""
    return tuple(f for f in fixtures if canonical(f.request) == canonical(payload))


def has_rows(fixture: HttpFixture) -> bool:
    """이 응답에 줄이 담겨 있는지.

    `Occurs1` 같은 배열이 비지 않았으면 담긴 것입니다. 배열 자체가 없는 응답
    (예수금처럼 줄이 없는 API)은 「담긴 것이 아니다」로 봅니다 — 그런 API 는
    기록끼리 견줄 때 이 잣대가 아무 차이도 못 내므로 그대로 순서를 지킵니다.
    """
    body = fixture.response
    if not isinstance(body, dict):
        return False
    return any(
        key.startswith("Occurs") and isinstance(value, list) and value
        for key, value in body.items()
    )


def _prefer_rows(fixtures: tuple[HttpFixture, ...]) -> HttpFixture:
    """줄이 담긴 기록을 먼저 고릅니다. 없으면 가장 이른 기록.

    **같은 요청으로 여러 번 수집된 기록이 있습니다.** 잔고나 주문내역처럼
    요청에 조건이 거의 없는 API 가 그렇습니다 — 잔고는 요청이 이어받기 키
    하나뿐이라 언제 불러도 요청이 같습니다. 그래서 포지션을 열기 전과 연 뒤에
    받은 응답이 **요청으로는 구분되지 않고**, 수집 순서대로 첫 기록이 뽑힙니다.

    그 첫 기록이 빈 응답이면 **화면이 늘 빈 표를 봅니다.** 자료가 없는 것이
    아니라 있는데 못 보는 것이라, 화면을 고치는 사람은 백엔드를 의심하게
    됩니다. 실시간 재생이 같은 상황에서 자료가 담긴 기록을 먼저 고르는 것과
    같은 생각입니다.

    빈 응답도 실제 응답이라 지우지는 않습니다. 같은 요청에 둘 다 있을 때
    **어느 쪽을 보여 줄지**의 문제이고, 화면을 띄워 보는 것이 재생의 목적이라
    담긴 쪽이 낫습니다.
    """
    for fixture in fixtures:
        if has_rows(fixture):
            return fixture
    return fixtures[0]


def _prefer_success(fixtures: tuple[HttpFixture, ...]) -> HttpFixture:
    """여럿 중 하나를 고릅니다 — **정상 > 줄 담김 > 이른 것** 차례.

    오류 응답이 먼저 수집되어 있다는 이유로 화면이 오류를 보게 되지 않도록
    정상 응답을 앞에 두고, 정상이 여럿이면 줄이 담긴 것을 먼저 봅니다.

    **요청이 통째로 같은 기록을 고를 때도 이 차례를 씁니다.** 같은 요청에
    오류 응답과 정상 응답이 함께 수집돼 있을 수 있는데, 이 모듈은 화면을
    띄워 보려고 있는 것이라 정상 쪽이 낫습니다. 고르는 규칙을 자리마다 다르게
    두면 「왜 이 기록이 나왔는가」를 자리마다 따로 설명해야 합니다.
    """
    good = tuple(f for f in fixtures if f.http_status < 400)
    return _prefer_rows(good) if good else fixtures[0]


def replay_client(store: FixtureStore) -> httpx.Client:
    """재생 전송 계층을 붙인 클라이언트. 밖으로 나가는 통신이 없습니다."""
    return httpx.Client(transport=ReplayTransport(store), trust_env=False)


@dataclass(frozen=True)
class StreamMatch:
    """고른 실시간 기록과, 어떻게 골랐는지.

    `match` 가 `exact` 가 아니면 **요청한 환경의 기록이 아니거나**(`cross-environment`)
    **어느 종목이 나올지 정해지지 않은 것**(`api` — 종목을 받는 구독에 종목을
    안 준 경우)입니다. 종목을 지정한 구독에 다른 종목의 기록이 나오는 일은 없습니다.
    """

    fixture: StreamFixture
    match: str

    @property
    def is_exact(self) -> bool:
        return self.match == MATCH_EXACT

    @property
    def action_sym(self) -> str | None:
        """이 기록이 구독했던 종목. 계좌 알림처럼 종목이 없으면 None."""
        return _stream_symbol(self.fixture)


def _has_data(fixture: StreamFixture) -> bool:
    """서버가 자료 메시지를 보내 온 기록인지.

    연결 확인이나 구독 확인만 오간 기록은 자료가 없는 것으로 봅니다. 자료
    메시지에는 `body` 가 있고, 제어 메시지에는 `code` 와 `msg` 만 있습니다.
    """
    return any(isinstance(e.payload, dict) and "body" in e.payload for e in fixture.received)


def _pick(fixtures: tuple[StreamFixture, ...]) -> StreamFixture:
    """자료가 담긴 기록을 먼저 고릅니다. 없으면 가장 이른 기록."""
    with_data = tuple(f for f in fixtures if _has_data(f))
    return (with_data or fixtures)[0]


def find_stream(
    store: FixtureStore,
    api_id: str,
    *,
    environment: str,
    market: str | None = None,
    action_sym: str | None = None,
    record_id: str | None = None,
) -> StreamMatch:
    """실시간 기록 하나를 고르고, 요청과 맞는 기록인지 함께 알려줍니다.

    HTTP 와 같은 규칙입니다. **종목을 준 구독은 그 종목의 기록만 씁니다** — 그
    환경에 있으면 `exact`, 다른 환경에만 있으면 `cross-environment`, 어느 환경에도
    없으면 `ReplayMiss` 입니다. 다른 종목의 기록으로 대신 흘리면 화면이 남의 값을
    그 종목의 것으로 그립니다 — 관심종목 셋이 한 종목 값으로 같아지는 식입니다.

    종목을 안 준 구독은 그 환경의 기록, 없으면 다른 환경의 기록입니다. 계좌 알림처럼
    종목이 없는 구독이면 `exact` 이고, 종목을 받는 구독인데 안 줬으면 어느 종목이
    나올지 정해지지 않으므로 `api` 로 표시합니다.
    """
    candidates = store.find_stream(api_id, environment=environment, market=market)
    others = tuple(
        f
        for f in store.streams
        if f.api_id == api_id
        and f.environment != environment
        and (market is None or f.market == market)
    )

    if record_id is not None:
        for fixture in candidates:
            if fixture.record_id == record_id:
                return StreamMatch(fixture, MATCH_EXACT)
        raise ReplayMiss(f"{api_id} 에 {record_id} 기록이 없습니다.")

    if action_sym is not None:
        for pool, match in ((candidates, MATCH_EXACT), (others, MATCH_CROSS_ENVIRONMENT)):
            same_symbol = tuple(f for f in pool if _stream_symbol(f) == action_sym)
            if same_symbol:
                return StreamMatch(_pick(same_symbol), match)
        raise ReplayMiss(f"저장해 둔 응답에 {api_id} {action_sym} 실시간 기록이 없습니다.")

    if candidates:
        takes_symbol = any(_stream_symbol(f) for f in candidates)
        return StreamMatch(_pick(candidates), MATCH_API if takes_symbol else MATCH_EXACT)
    if others:
        return StreamMatch(_pick(others), MATCH_CROSS_ENVIRONMENT)
    raise ReplayMiss(f"저장해 둔 응답에 {api_id} 실시간 기록이 없습니다.")


def _stream_symbol(fixture: StreamFixture) -> str | None:
    """이 기록이 구독했던 종목. 종목 없는 구독이면 None."""
    if not isinstance(fixture.request, dict):
        return None
    return (fixture.request.get("body") or {}).get("action_sym") or None


async def stream_events(
    store: FixtureStore,
    api_id: str,
    *,
    environment: str,
    market: str | None = None,
    action_sym: str | None = None,
    record_id: str | None = None,
    speed: float = 0.0,
    strict: bool = False,
) -> AsyncIterator[Any]:
    """저장해 둔 실시간 메시지를 순서대로 흘려보냅니다.

    서버가 보내 온 것만 나옵니다. 구독 확인 같은 제어 메시지도 섞여 있으므로
    받는 쪽이 구분해야 합니다.

    요청한 종목의 기록이 그 환경에 없으면 다른 환경의 같은 종목 기록으로 대신
    흘려보내고 경고를 남깁니다 — 어느 환경에도 없으면 `ReplayMiss` 입니다. 다른
    종목의 기록으로 대신하는 일은 없습니다. 다른 환경의 값도 곤란한 자리라면
    `strict=True` 로 부르세요. 그때는 대신 답하지 않고 막습니다. 어느 기록을 쓸지
    미리 알아야 하면 `find_stream` 을 먼저 부르세요.

    `speed` 가 0 이면 기다리지 않고 이어서 내보내고, 1 이면 수집했을 때의
    간격을 그대로 둡니다.
    """
    chosen = find_stream(
        store,
        api_id,
        environment=environment,
        market=market,
        action_sym=action_sym,
        record_id=record_id,
    )

    if not chosen.is_exact:
        message = (
            f"재생: {api_id}({environment}) 구독과 맞는 기록이 없어 "
            f"{chosen.fixture.environment} {chosen.action_sym or '계좌'} 기록"
            f"({chosen.fixture.record_id})으로 대신 내보냅니다."
        )
        if strict:
            raise ReplayMiss(message)
        logger.warning(
            "재생: %s(%s) 구독과 맞는 기록이 없어 %s %s 기록(%s)으로 대신 내보냅니다.",
            api_id,
            environment,
            chosen.fixture.environment,
            chosen.action_sym or "계좌",
            chosen.fixture.record_id,
        )

    previous = 0.0
    for event in chosen.fixture.received:
        if speed > 0:
            gap = max(0.0, event.offset_ms - previous) / 1000 / speed
            if gap:
                await asyncio.sleep(gap)
        previous = event.offset_ms
        yield event.payload
