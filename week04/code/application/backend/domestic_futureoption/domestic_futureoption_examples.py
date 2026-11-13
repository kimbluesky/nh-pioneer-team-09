"""국내 선물/옵션 REST 실행 예제.

저장해 둔 응답으로 돌려 보는 예제입니다. **NH선물 서버에 접속하지 않습니다.**

    uv run python -m backend.domestic_futureoption.domestic_futureoption_examples

실서버로 부르려면 설정 파일에 인증 정보가 있어야 하고, 아래 `SESSION` 을
`create_session()` 으로 바꾸면 됩니다. 그때는 실제 호출이 나가고 호출 제한도
실제로 걸립니다.
"""
from __future__ import annotations

import json

from backend.domestic_futureoption import domestic_futureoption_functions as api
from backend.domestic_futureoption import domestic_quotes
from backend.domestic_futureoption import domestic_trend
from backend.nh_api.client import TradeClient
from backend.nh_api.session import create_session

# 재생 모드의 저장 응답에 들어 있는 종목입니다. 실서버로 바꿔 부를 때도 그대로 씁니다.
SYMBOL = "A7569000"
DATE = "20260909"

# 위 종목이 속한 품목(75 미국달러). 선물전체시세는 품목으로 묻습니다.
PRODUCT = "75"


def show(title: str, value: object) -> None:
    print(f"\n== {title} ==")
    print(json.dumps(value, ensure_ascii=False, indent=1)[:1200])


def main() -> None:
    # 저장해 둔 응답으로 답하는 접속입니다. 실서버 호출이 나가지 않습니다.
    with create_session(replay=True) as session:
        client = TradeClient(session)

        raw = api.price(SYMBOL, api=client, environment="real")
        show("현재가 원문", {k: raw[k] for k in list(raw)[:8]})
        show("현재가 화면 모양", domestic_quotes.quote(raw))

        raw = api.price_bid(SYMBOL, api=client, environment="real")
        show("호가", domestic_quotes.book(raw))

        raw = api.trend_investor(f"{PRODUCT}1", ref_dt=DATE, api=client, environment="real")
        made = domestic_trend.investor_trend(raw, inq_tp=api.QUANTITY, ref_dt=DATE)
        show("투자자 매매동향 앞 두 무리", made["investors"][:2])
        print(f"\n{made['date']} 기준, 단위 {made['unit']}, 무리 {len(made['investors'])}개")

        raw = api.price_all_futures(PRODUCT, api=client, environment="real")
        made = domestic_quotes.all_futures(raw, asked=PRODUCT)
        show("선물전체시세 앞 두 월물", made["rows"][:2])
        print(f"\n품목 {made['product']} 의 월물 {made['count']}개")

        raw = api.chart_minute(
            SYMBOL, start_date=DATE, end_date=DATE, api=client, environment="real"
        )
        made = domestic_quotes.bars(raw, minute=True)
        show("분봉 앞 세 개", made["bars"][:3])
        print(f"\n분봉 {len(made['bars'])}개, 뺀 줄 {made['skipped']}개")


if __name__ == "__main__":
    main()
