"""서버에 **닿지 못해** 생긴 실패인지 가려냅니다.

앱을 켤 때 거래시간표 두 벌과 종목 마스터를 받습니다. 망이 잠깐 끊겨 있으면 셋이
각각 실패하는데, 앱은 멈추지 않고 내장 표와 받아 둔 파일로 이어서 뜹니다. 그런
자리에 트레이스백을 통째로 남기면 기록을 여는 사람에게 큰 사고처럼 보입니다.
무엇을 못 받았고 **무엇으로 대신하는지** 한 줄이면 됩니다.

이름 풀이 실패·연결 거부·시간 초과처럼 서버에 닿지도 못한 실패만 그렇게 봅니다.
그 밖의 실패(응답을 읽지 못함, 파일을 쓰지 못함)는 원인을 알아야 고칠 수 있으므로
트레이스백을 그대로 남깁니다.

    logger.warning("… 무엇으로 대신합니다.", exc_info=not unreachable(exc))

**가리는 자리가 둘입니다.** `unreachable` 은 「기록을 한 줄로 줄일까」이고,
`cannot_connect` 는 「다시 걸어 볼까, 남은 것을 포기할까」입니다. 뒤엣것이 더
좁습니다 — 받는 도중 끊긴 것은 다시 걸면 받아질 수 있어 포기할 자리가 아닙니다.
"""
from __future__ import annotations

import httpx

from backend.nh_api.errors import TransportError

#: 닿지 못한 것으로 보는 예외.
#:
#: `httpx.TransportError` 가 이름 풀이·연결 거부·시간 초과·연결 끊김을 모두 품는
#: 윗자리입니다(`ConnectError`·`ConnectTimeout`·`ReadTimeout` 따위). 종목 마스터는
#: 이 예외를 그대로 만나고, 거래 API 호출부는 그것을 이 앱의 `TransportError` 로
#: 바꿔 냅니다.
_UNREACHABLE = (httpx.TransportError, TransportError)

#: **연결 자체가 안 선** 예외. 위 묶음보다 좁습니다.
#:
#: 이름을 못 풀거나 연결을 거부당한 것이라, 같은 주소로 다시 걸어도 답이 같습니다.
#: `ConnectTimeout` 은 `ConnectError` 의 아랫자리가 아니라 시간 초과 쪽 갈래라
#: 따로 적습니다. 받는 **도중** 멈추거나 끊긴 것(`ReadTimeout`·`ReadError`·
#: `RemoteProtocolError`)은 여기 없습니다 — 연결은 섰던 것이라 다시 걸면 받아질
#: 수 있고, 한 파일이 그랬다고 나머지까지 못 받을 이유가 없습니다.
_CANNOT_CONNECT = (httpx.ConnectError, httpx.ConnectTimeout)


def unreachable(exc: BaseException | None) -> bool:
    """서버에 닿지 못해 생긴 실패인지. 받는 도중 끊긴 것도 포함합니다.

    **기록을 한 줄로 줄일지**를 정할 때 씁니다.
    """
    return _caused_by(exc, _UNREACHABLE)


def cannot_connect(exc: BaseException | None) -> bool:
    """연결 자체가 서지 않아 생긴 실패인지.

    **다시 걸어 볼지, 남은 것을 포기할지**를 정할 때 씁니다. `unreachable` 보다
    좁습니다 — 까닭은 `_CANNOT_CONNECT` 에.
    """
    return _caused_by(exc, _CANNOT_CONNECT)


def _caused_by(exc: BaseException | None, kinds: tuple[type[BaseException], ...]) -> bool:
    """그 예외이거나 그것이 원인인지.

    **원인 사슬(`raise … from …`)까지 봅니다.** 받는 쪽이 자기 예외로 감싸 올리는
    자리가 많아, 맨 바깥 예외만 보면 대부분 놓칩니다. 감싸지 않고 새로 일으킨
    예외(`__context__` 로만 이어진 것)는 보지 않습니다 — 접속이 끊긴 김에 드러난
    다른 문제일 수 있고, 그것은 트레이스백이 있어야 고칩니다.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, kinds):
            return True
        seen.add(id(exc))
        exc = exc.__cause__
    return False
