"""해외 종목 마스터의 컬럼 배치.

해외 마스터는 거래소별 파일로 오고, 컬럼 배치는 **세 가지**입니다.

    선물·스프레드   20개  (CME · 기타 거래소 · LME 가 같습니다)
    옵션            17개  (CME · 기타 거래소)
    OPRA 옵션       19개  (옵션 17개 + `weekDay` + `lastDt`)

컬럼 이름은 NH선물이 배포하는 헤더정보 문서의 표기를 그대로 씁니다. 국내는
`price_bid`, 해외는 `bid_price` 처럼 두 시장의 이름 규칙이 다른데, 이것도
원문 그대로 두는 것이 원칙입니다.
"""
from __future__ import annotations

#: 해외 선물·스프레드 마스터의 컬럼 20개.
OVERSEAS_FUTURES_FIELDS: tuple[str, ...] = (
    "code",         # 종목코드
    "VenderCode",   # 종목코드(시세벤더코드) — 실시간 요청 시 사용
    "ExchName",     # 거래소이름
    "enam",         # 종목명(영어)
    "name",         # 종목명(한글)
    "exch",         # exchange id
    "type",         # 선물옵션구분 (F.선물 O.옵션 S.스프레드)
    "sect",         # 상품구분 (10.통화 20.금리 30.지수 40.농산물 50.금속 60.에너지 70.Soft)
    "pind",         # 가격표시방법 — 소수 자릿수가 아니라 표시 방법 코드입니다
    "trdf",         # 거래여부 (0.거래 가능 1.거래 불가)
    "tsiz",         # 호가단위
    "adjv",         # 가격조정계수
    "lmon",         # 최근월물구분_거래량기준
    "lmon_my",      # 최근월물구분_만기일기준
    "comd",         # 상품코드
    "yymm",         # 만기년월1 (스프레드면 만기년월 1)
    "yymm1",        # 만기년월2 (스프레드의 만기년월 2)
    "prev",         # 전일종가
    "tval",         # 가격변동폭
    "subExch",      # 서브거래소코드
)

#: 해외 옵션 마스터의 컬럼 17개. 선물에 있는 `lmon`·`lmon_my`·`yymm1` 이 없습니다.
OVERSEAS_OPTION_FIELDS: tuple[str, ...] = (
    "code",
    "VenderCode",
    "ExchName",
    "enam",
    "name",
    "exch",
    "type",
    "sect",
    "pind",
    "trdf",
    "tsiz",
    "adjv",
    "comd",
    "yymm",         # 만기년월
    "prev",
    "tval",
    "subExch",
)

#: OPRA 옵션 마스터의 컬럼 19개. 옵션 17개 뒤에 두 개가 더 붙습니다.
OPRA_OPTION_FIELDS: tuple[str, ...] = OVERSEAS_OPTION_FIELDS + (
    "weekDay",      # 옵션 만기 요일값 — OPRA 옵션에만 있습니다
    "lastDt",       # 최종거래일 — OPRA 옵션에만 있습니다
)
