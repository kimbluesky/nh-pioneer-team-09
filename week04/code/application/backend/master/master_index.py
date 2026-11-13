"""화면이 읽을 색인을 만듭니다.

마스터에는 **품목 목록이 없습니다.** 종목(월물) 행만 있어서 품목은 접어서 만듭니다.
접는 기준은 시장마다 다릅니다.

    국내   `under_name` (기초자산명)   — `trd_code` 와 1:1 로 대응합니다
    해외   `comd` (상품코드)

같은 값이 국내와 해외에 함께 나타나는 경우가 있어(`SFC`) 품목은 **시장과 함께** 세어야
합니다. 시장을 빼고 코드만으로 모으면 두 품목이 한 품목으로 합쳐집니다.

## 국내는 같은 종목코드가 두 줄로 옵니다

국내 지수 선물·옵션은 주간과 야간이 **각각 한 줄씩** 옵니다. 종목코드(`series`)는
같고 세션(`cme_tp`)만 다릅니다. 그래서 코드만으로 모으면 한 줄이 다른 줄을 조용히
덮습니다 — 어느 줄이 남을지는 파일 읽는 순서가 정합니다.

색인은 이렇게 가릅니다.

* **읽어 들일 때**는 두 줄을 다 들고 있습니다. 세션마다 다른 값(상한가·하한가·
  실시간 상한가)이 필요해지는 자리가 있습니다.
* **화면으로 내보낼 때는 종목코드 하나로 접습니다.** 화면은 종목코드 하나를 들고
  「이게 무슨 종목인가」를 묻습니다. 세션이 없는 자리에서 두 줄을 받으면, 백엔드가
  방금 없앤 모호함을 화면이 대신 떠안습니다.

접기는 **색인과 체인 어느 쪽으로 나가든 똑같이 겁니다.** 한쪽만 접으면 같은 종목이
화면에서 한 번은 하나로, 한 번은 둘로 보입니다. 국내는 접히는 줄의 99% 가 옵션이라,
옵션을 빼 두면 안전망이 사실상 없는 것과 같습니다.

접어서 내보내는 컬럼은 **두 줄에서 값이 같은 것만**입니다. 세션마다 달라지는 컬럼은
접는 순간 「어느 값이었나」가 사라지므로 아예 안 싣습니다 — `_SESSION_FIELDS` 를
보세요. 여기에 `mrkt_tp` 가 들어 있는 것이 중요합니다. 이름은 시장 구분처럼 생겼지만
같은 상품이 주간이면 6, 야간이면 4 로 와서 **분류에 쓸 수 없습니다.** 상품 분류가
필요하면 `mrkt_code` 를 쓰세요 — 그쪽은 두 줄에서 같습니다.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from backend.master.master_files import DOMESTIC, OVERSEAS
from backend.master.master_parser import MasterTable
from backend.master.master_store import LoadedMaster
from backend.master.master_widths import WidthCandidates
from backend.sessions.hours import now_kst

logger = logging.getLogger(__name__)

#: 종목의 갈래. 원문 구분(국내 `fo_tp`, 해외 `type`)을 이름만 바꾼 것이고 **접지
#: 않습니다.** 스프레드를 선물에 접어 두면 화면이 되돌릴 수 없습니다 — 스프레드는
#: 만기가 둘이라 월물 목록의 「만기 한 값」 칸에 넣을 값이 없고, 목록에서 고른 것이
#: 그대로 주문 대상이 되는 자리라 섞이면 안 됩니다. 거르는 것은 화면이 합니다.
FUTURES = "futures"
OPTION = "option"
SPREAD = "spread"

_DOMESTIC_KIND = {"F": FUTURES, "C": SPREAD, "O": OPTION}
_OVERSEAS_KIND = {"F": FUTURES, "S": SPREAD, "O": OPTION}

# 종목명에서 품목 이름만 남깁니다.
#
# 마스터에 품목 이름을 담은 컬럼이 없어서 종목명에서 잘라 만듭니다. 잘라 만든 값은
# `product_name` 에 따로 담고 **원문 종목명은 그대로 둡니다** — 자르는 규칙이
# 안 맞는 이름이 나와도 원문과 대조하면 바로 보입니다.
#
#   국내  "코스피200 F 202609"          → "코스피200"
#         "한미반도체 C 202609   190,000(  10)" → "한미반도체"
#   해외  "호주달러 (202609)"            → "호주달러"
#         "WTI원유 옵션 C10.00 (202701)" → "WTI원유 옵션"
_DOMESTIC_TAIL = re.compile(r"\s+(?:F|C|P|SP)\s+\S.*$")
_OVERSEAS_EXPIRY_TAIL = re.compile(r"\s*\(\d{6}\)\s*$")
_OVERSEAS_STRIKE_TAIL = re.compile(r"\s+[CP][\d.]+\s*$")

# 해외 옵션은 행사가와 콜/풋을 담은 컬럼이 아예 없습니다. 종목코드 끝에 들어 있어서
# 거기서 뽑습니다. 원문 컬럼이 아니라는 것이 이름에 드러나도록 `code_` 를 붙입니다.
_OVERSEAS_STRIKE_IN_CODE = re.compile(r"-([CP])([\d.]+)$")

# 코드 모양으로 품목 열쇠를 뽑는 규칙 — 마스터에 없는 종목(만기 지난 것, 해외의
# 행사가 없는 시리즈 코드)의 세션을 판정할 때, **묻는 코드를 풀 때만** 씁니다.
# 표 자체는 행의 진짜 칸(국내 `trd_code`, 해외 `comd`)으로 만듭니다.
#   국내  `A0169000`·`B016A745`·`C0169C42`·`D0169…` — 첫 글자가 갈래(A 선물·B 콜·C 풋·
#         D 스프레드), 둘째·셋째 글자가 거래대상코드(`trd_code`: 01 코스피200·05 미니·
#         06 코스닥150·75 달러…). 이 자리는 그 행의 `trd_code` 와 같습니다.
#   해외  `ESU26`·`O_ESU26`·`O_ESU26-C7100.0`·`S_ESH27-U26` — 품목코드 뒤에 월물 글자와
#         연도 두 자리, 옵션은 그 뒤에 행사가, 스프레드는 두 번째 월물. 위클리 옵션
#         (`O_SPW3FU26-…`)은 이 모양이 아니라 못 풉니다 — 마스터에 있는 동안은 `locate` 가
#         받습니다.
_DOMESTIC_CODE = re.compile(r"^([ABCD])([A-Z0-9]{2})")
_OVERSEAS_SERIES = re.compile(
    r"^(.+?)[FGHJKMNQUVXZ]\d{2}(?:-(?:[CP][\d.]+|[FGHJKMNQUVXZ]\d{2}))?$"
)


def _domestic_product_name(name_kor: str) -> str:
    return _DOMESTIC_TAIL.sub("", name_kor).strip()


def _overseas_product_name(name: str) -> str:
    trimmed = _OVERSEAS_EXPIRY_TAIL.sub("", name)
    return _OVERSEAS_STRIKE_TAIL.sub("", trimmed).strip()


def _strike_from_code(code: str) -> tuple[str, str]:
    """해외 옵션 종목코드에서 콜/풋과 행사가를 뽑습니다. 못 뽑으면 빈 문자열입니다."""
    found = _OVERSEAS_STRIKE_IN_CODE.search(code)
    if found is None:
        return "", ""
    return found.group(1), found.group(2)


def _enrich(table: MasterTable, row: dict[str, str]) -> dict[str, str]:
    """종목 한 줄에 화면이 쓰는 갈래를 곁들입니다.

    원문 컬럼은 하나도 빼지 않고 그대로 담고, `market`·`kind`·`product_code`·
    `expiry` 를 더합니다. 곁들이는 이름은 원문 컬럼과 겹치지 않으므로 어느 것이
    원문인지 헷갈릴 일이 없습니다.
    """
    if table.market == DOMESTIC:
        # 모르는 구분이 오면 선물로 뭉뚱그리지 않고 원문 값을 그대로 둡니다.
        # 뭉뚱그리면 새 갈래가 생긴 날 선물 목록에 조용히 섞여 들어갑니다.
        kind = _DOMESTIC_KIND.get(row["fo_tp"]) or row["fo_tp"]
        made = {
            **row,
            "market": DOMESTIC,
            "source": table.name,
            "kind": kind,
            "product_code": row["under_name"],
            "product_name": _domestic_product_name(row["name_kor"]),
            "expiry": row["exp_ym"][:6],
            # 만기일 8자리. 헤더 문서는 `exp_ym` 을 「만기년월」이라 적지만 국내
            # 마스터는 실제로 `YYYYMMDD` 를 줍니다 — 위클리는 한 달에 만기가
            # 여럿이라, 6자리로 접으면 그 달의 위클리가 한 칸으로 뭉칩니다.
            "expiry_date": row["exp_ym"],
        }
        if kind == SPREAD:
            made["legs"] = _domestic_legs(row)
        return made

    kind = _OVERSEAS_KIND.get(row["type"]) or row["type"]
    code_cp, code_strike = _strike_from_code(row["code"]) if kind == OPTION else ("", "")
    made = {
        **row,
        "market": OVERSEAS,
        "source": table.name,
        "kind": kind,
        "product_code": row["comd"],
        "product_name": _overseas_product_name(row["name"]),
        "expiry": row["yymm"],
        "code_cp": code_cp,
        "code_strike": code_strike,
    }
    if kind == SPREAD:
        made["legs"] = _legs(row.get("yymm"), row.get("yymm1"))
    last_day = _last_trading_day(row)
    if last_day:
        made["expiry_date"] = last_day
    return made


# 국내 스프레드 종목명에 든 두 다리. `코스피200 SP 2609-2612` 의 `2609`·`2612`.
_DOMESTIC_LEGS = re.compile(r"SP (\d{4})-(\d{4})")


def _legs(first: str | None, second: str | None) -> dict[str, str | None] | None:
    """스프레드의 두 다리. 못 읽으면 `None` 입니다.

    **두 값 중 이른 달이 근월입니다.** 마스터가 적어 준 차례는 근월·원월 차례가
    아닙니다 — 그 차례는 종목코드에 쓰인 순서일 뿐이고 품목마다 갈립니다.

        해외  `yymm` 이 코드의 첫 다리, `yymm1` 이 둘째 다리입니다. 첫 다리가
              이른 것(`S_TNU26-H27` — 202609·202703)과 늦은 것(`S_6EZ26-U26` —
              202612·202609)이 함께 있습니다.
        국내  `kofex_Code` 도 마찬가지입니다 — `KOSPI612+609` 는 원월이 먼저인데
              `KTB609+612` 는 근월이 먼저이고, 그 칸에 종목코드가 들어 있는 줄도
              있습니다. 그래서 이름에서 읽습니다.

    두 달을 여섯 자리(`YYYYMM`)로 맞춰 비교하므로 연도가 바뀌어도 어긋나지 않습니다.
    """
    months = sorted(
        month for month in (str(first or "").strip(), str(second or "").strip())
        if len(month) == 6 and month.isdigit()
    )
    if len(months) != 2:
        return None
    # 날짜는 아는 쪽만 채웁니다. 모양을 고정해 두어 화면이 칸 유무를 안 따집니다.
    return {"near": months[0], "far": months[1], "nearDate": None, "farDate": None}


def _domestic_legs(row: dict[str, str]) -> dict[str, str | None] | None:
    """국내 스프레드의 두 다리. 종목명에서 읽습니다 — 까닭은 `_legs` 에.

    **근월의 만기일은 알 수 있습니다.** `exp_ym` 여덟 자리가 근월 다리의 만기일과
    같은 달이라 그 값을 `nearDate` 로 냅니다. 원월 다리의
    날짜는 마스터에 없습니다.
    """
    found = _DOMESTIC_LEGS.search(row.get("name_kor") or "")
    if found is None:
        return None
    made = _legs(f"20{found.group(1)}", f"20{found.group(2)}")
    if made is None:
        return None
    expiry = str(row.get("exp_ym") or "").strip()
    if len(expiry) == 8 and expiry.isdigit() and expiry[:6] == made["near"]:
        made["nearDate"] = expiry
    return made


def _last_trading_day(row: dict[str, str]) -> str | None:
    """해외 줄의 최종거래일 여덟 자리. 그 칸이 없거나 날짜가 아니면 `None`.

    **`lastDt` 는 OPRA 옵션 파일에만 있는 칸입니다.** 다른 해외 파일은 만기년월
    (`yymm`) 여섯 자리까지만 주므로 그 시장이 가를 수 있는 최대가 달입니다.
    OPRA 는 날짜를 주므로 **같은 달 안에서 주마다 갈리는 위클리 옵션**이 만기별로
    나뉩니다 — `O_SPW` 202609 가 09-02 만기와 09-03 만기로 나뉘는 것이 그 예입니다.
    """
    body = str(row.get("lastDt") or "").strip()
    if len(body) != 8 or not body.isdigit():
        return None
    try:
        datetime.strptime(body, "%Y%m%d")
    except ValueError:
        # 있을 수 없는 날은 만기로 쓰지 않습니다. 그 줄은 달로 묶입니다.
        return None
    return body


def _tables_of(loaded: Iterable[LoadedMaster]) -> list[MasterTable]:
    """파싱에 성공한 표만 골라 냅니다. 받기만 하는 파일은 표가 없습니다."""
    return [item.table for item in loaded if item.table is not None]


#: 품목 줄에 얹어 볼 컬럼. **그 품목의 모든 종목에서 값이 같을 때만** 실립니다.
#:
#: 「품목의 성질」처럼 보이는 값도 실제로는 종목마다 다른 경우가 있습니다 —
#: 거래승수(`ctrt_size`)는 권리조정을 거친 종목이 섞이면 한 품목 안에서 갈립니다.
#: 그래서 미리 고르지 않고 **매번 재서**, 갈리면 그 품목에서만 뺍니다. 뺀 값이
#: 필요하면 종목 행에서 읽으세요 — 거기에는 늘 있습니다.
_PRODUCT_HINTS: dict[str, tuple[str, ...]] = {
    DOMESTIC: ("trd_code", "mrkt_code", "p_dec", "ctrt_size"),
    OVERSEAS: ("ExchName", "exch", "sect", "pind", "tsiz"),
}


def _hints_of(contract: dict[str, str]) -> dict[str, str]:
    return {
        field: contract[field]
        for field in _PRODUCT_HINTS[contract["market"]]
        if field in contract
    }


#: 국내 마스터 `cme_tp` 의 야간 값. 같은 종목이 주간 줄과 야간 줄로 따로 옵니다.
NIGHT_SESSION = "night"

#: 국내 마스터에서 **주간·야간 줄의 값이 다를 수 있는** 컬럼.
#:
#: 종목코드로 접으면 「어느 세션의 값이었나」가 사라지므로 화면으로 내보내지 않고,
#: 접기 충돌로도 세지 않습니다. 세션별 값은 읽어 둔 표(`MasterIndex.tables`)의 주간
#: 줄과 야간 줄에 그대로 남아 있습니다.
#:
#: 헤더정보 문서는 칸의 뜻만 적고 세션에 따라 달라지는지는 적지 않습니다. 이 여덟
#: 칸은 **배포 파일에서** 국내 주간 줄과 야간 줄의 값이 갈리는 칸입니다. 칸의 뜻은
#: 레이아웃 선언
#: `backend/domestic_futureoption/master/__init__.py` 에 원문 그대로 있습니다
#: (`mrkt_tp` 15행, `cme_tp` 20행, `kospi_key` 22행, `upper_p_lv1`·`lower_p_lv1`
#: 27~28행, `dynmc_yn`·`dynmc_up_p`·`dynmc_dn_p` 34~36행).
#:
#: * `cme_tp` 주야간구분 — 세션 그 자체입니다.
#: * `kospi_key` 거래소코드 — 세션마다 다른 코드가 옵니다(`KSPOBASE`·`KSPONDV`).
#: * `upper_p_lv1`·`lower_p_lv1` 상한가·하한가 — 스프레드에서 갈릴 수 있습니다.
#: * `dynmc_yn`·`dynmc_up_p`·`dynmc_dn_p` 실시간상한가 적용여부와 그 상한·하한 —
#:   상한·하한은 세션마다 다를 수 있습니다. 적용여부는 대부분 두 줄이 같지만, 같은
#:   상품 안에서도 일부 종목은 주간 `Y`·야간 `N` 으로 옵니다.
#: * `mrkt_tp` 마켓구분 — 이름은 시장 구분처럼 생겼지만 같은 상품이 주간과 야간에
#:   다른 값으로 옵니다(`idx_fut_opt` 의 주간 6·야간 4). 상품 분류에는 `mrkt_code` 를 쓰세요.
_SESSION_FIELDS: frozenset[str] = frozenset(
    {
        "mrkt_tp",
        "cme_tp",
        "kospi_key",
        "upper_p_lv1",
        "lower_p_lv1",
        "dynmc_yn",
        "dynmc_up_p",
        "dynmc_dn_p",
    }
)

#: 주간·야간 줄에서 값이 갈리지만 **한 값만 내보내는** 컬럼.
#:
#: 잔존일수(`rem_cnt`)는 세션마다 다르게 옵니다 — 야간 줄은 다음 거래일의
#: 것이라 주간 줄보다 적거나 같습니다. **차이는 그날그날 다릅니다**: 같은
#: 종목이 어느 날은 주간 80·야간 80, 다음 날은 80·79, 그다음 날은 79·74 로
#: 옵니다. 둘 다 그 세션에서는 맞는 값이지만 화면에서는
#: 숫자가 하나라, **주간 줄의 값**을 씁니다.
#:
#: `_SESSION_FIELDS` 와 달리 **내보냅니다.** 거기 있는 값은 세션을 함께 들고
#: 있어야 뜻이 서는 값이고, 잔존일수는 「만기까지 며칠」이라 한 값으로 족합니다.
#:
#: **이 컬럼은 접기 충돌로 세지 않습니다.** 세면 주간·야간이 함께 오는 국내
#: 종목이 통째로 색인에서 빠집니다.
_DAY_VALUE_FIELDS: frozenset[str] = frozenset({"rem_cnt"})

#: 국내 옵션 신원표의 컬럼. 원문 컬럼(`series`·`opt_tp`)과 색인이 곁들이는
#: 이름(`product_code`·`expiry_date`)이 섞여 있습니다.
#:
#: **만기는 8자리입니다.** 이 표가 답하는 것은 「이 코드가 무엇인가」이고, 위클리는
#: 같은 달에 만기가 여럿이라 6자리로는 서로 다른 종목이 같은 신원을 갖습니다.
#:
#: 행사가·종목명·상한가는 담지 않습니다. 「이 코드가 어느 품목의 어느 달 콜/풋인가」에만
#: 답하는 표입니다.
DOMESTIC_OPTION_INDEX_FIELDS: tuple[str, ...] = (
    "series",
    "product_code",
    "expiry_date",
    "opt_tp",
)


def _rem_cnt_of(contract: dict) -> int | None:
    """그 종목의 잔존일수. 마스터에 없거나 숫자가 아니면 `None` 입니다.

    **0 과 없음은 다릅니다.** 0 은 「오늘이 만기」라는 답이고, 없음은 답이 없는
    것입니다. 없음을 0 으로 바꾸면 화면이 「D-0」이라고 말하면서 오늘 만기라고
    거짓말합니다.
    """
    raw = contract.get("rem_cnt")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text.lstrip("-").isdigit():
        return None
    return int(text)


#: 만기 단위를 가르는 품목명. **마스터에는 위클리 표식이 없습니다** — 국내 레이아웃
#: 셋(지수·주식·상품) 어디에도 주간 여부를 말하는 컬럼이 없고, 문서 묶음 셋을 다
#: 훑어도 위클리를 말하는 곳은 시세 API 의 품목코드 열거뿐이라 마스터와 다른 표입니다.
#:
#: 이 판본에서 걸리는 국내 품목은 넷입니다 — 코스피위클리(WKI)·코스피위클리M(WKM)·
#: 코스닥위클리(WQI)·코스닥위클리M(WQM).
#:
#: 「같은 달에 만기가 둘 이상이면 주간」이라는 판정은 쓰지 않습니다 — 위클리도
#: 한 달에 만기가 하나뿐인 때가 있어, 그 규칙으로는 위클리가 월물로 떨어집니다.
#:
#: **이름에 기대는 것은 다른 길이 없어서입니다.** 코드 규칙이나 접두어로 성질을
#: 알아내는 방식은 쓰지 않습니다 — 그 판본에서 우연히 맞을 뿐이기 때문입니다.
#: 여기서 이름을 쓰는 것은 **마스터에도 문서에도 그 값을 담은 칸이 없기** 때문입니다.
#:
#: **품목명은 배포하는 쪽이 짓는 문자열입니다.** 표기가 바뀌면 예외도 오류도 없이
#: 전부 `month` 로 떨어지고, 화면은 멀쩡한 채 라벨만 조용히 월물로 돌아갑니다.
#: 그 조용한 되돌아감은 시험 `test_날_단위_품목은_이_판본에서_정확히_넷이다` 가
#: 「이 판본에서 날 단위 품목은 정확히 넷」을 확인해 잡습니다.
WEEKLY_IN_NAME = "위클리"


def _expiry_unit(market: str, product_name: str) -> str:
    """그 품목의 만기를 무엇으로 부를지. `day` 면 날짜까지, `month` 면 월물입니다.

    **날짜를 못 받는 시장은 달 단위입니다.** 해외 마스터는 만기년월(6자리)까지만
    주므로, 이름에 위클리가 들어도 세울 날짜가 없습니다 — 못 보여 줄 값을
    약속하면 화면이 빈 자리를 그립니다.
    """
    if market != DOMESTIC:
        return "month"
    return "day" if WEEKLY_IN_NAME in product_name else "month"


def _code_field(market: str) -> str:
    """그 시장에서 종목코드를 담은 원문 컬럼 이름."""
    return "series" if market == DOMESTIC else "code"


#: 품목 열쇠를 담은 원문 컬럼. 표는 이 칸으로 만들고, 코드는 같은 값을 뽑아 찾습니다.
_PRODUCT_KEY_FIELD = {DOMESTIC: "trd_code", OVERSEAS: "comd"}


def _product_key(market: str, code: str) -> str | None:
    """묻는 코드에서 품목 열쇠를 뽑습니다. 모양이 안 맞으면 `None`."""
    if market == DOMESTIC:
        found = _DOMESTIC_CODE.match(code)
        return found.group(2) if found else None
    found = _OVERSEAS_SERIES.match(code)
    return found.group(1) if found else None


def _product_line(market: str, record: dict[str, str], key: str) -> dict[str, str]:
    """품목의 최소 줄 — 판정에 필요한 칸만 (국내 `mrkt_code`, 해외 `comd`·`exch`)."""
    if market == DOMESTIC:
        return {
            "market": DOMESTIC,
            "product_code": record["under_name"],
            "trd_code": key,
            "mrkt_code": record.get("mrkt_code", ""),
        }
    return {
        "market": OVERSEAS,
        "product_code": record["comd"],
        "comd": record["comd"],
        "exch": record.get("exch", ""),
        "ExchName": record.get("ExchName", ""),
    }


def _fold(contract: dict[str, str]) -> dict[str, str]:
    """화면으로 내보낼 모양으로 다듬습니다. 세션마다 달라지는 컬럼을 뺍니다."""
    if contract["market"] != DOMESTIC:
        return contract
    return {k: v for k, v in contract.items() if k not in _SESSION_FIELDS}


def _fingerprint(contract: dict[str, object]) -> int:
    """접은 줄의 지문. **세션마다 값이 다른 컬럼은 빼고** 냅니다.

    잔존일수가 다른 것은 같은 종목이 주간·야간으로 두 번 온 것이지 두 줄이
    어긋난 것이 아닙니다(`_DAY_VALUE_FIELDS`).
    """
    made = {
        k: _comparable(v)
        for k, v in _hashable(contract).items()
        if k not in _DAY_VALUE_FIELDS
    }
    return hash(tuple(sorted(made.items())))


def _comparable(value: object) -> object:
    """두 줄을 견줄 때 쓰는 값. **공백 모양만 다른 값은 같은 값으로 봅니다.**

    같은 종목이 두 번 실리면서 한 줄만 이름 칸의 공백 수가 다르게 오는 일이
    있습니다(`option C66.00 (202808)` 과 `option C66.00    (202808)`). 뜻은 같으므로
    충돌로 세지 않습니다. 견줄 때만 이렇게 볼 뿐, 내보내는 값은 원문 그대로입니다.
    """
    return " ".join(value.split()) if isinstance(value, str) else value


def _adopt_day_row(
    key: tuple[str, str],
    market: str,
    contract: dict[str, str],
    folded: dict[str, str],
    was: tuple[str, int],
    now: tuple[str, int],
    contracts: dict[tuple[str, str], dict[str, str]],
    expiries: dict[tuple[str, str, str], dict[str, object]],
    chains: dict[tuple[str, str, str], list[tuple[str, int]]],
) -> None:
    """야간 줄이 먼저 온 자리를 **주간 줄로 갈아 끼웁니다.**

    배포 파일에서는 대개 주간 줄이 먼저 옵니다. 그래도 파일 차례에 기대지
    않는 것은, 차례가 바뀌는 날
    잔존일수만 조용히 틀리기 때문입니다 — 틀린 것을 아무도 못 봅니다.

    **값만 바꿔서는 모자랍니다.** 색인이 내는 줄(`contracts`)은 값을 들고 있지만
    `locate` 와 체인은 **줄의 위치**를 들고 있어, 위치를 안 바꾸면 같은 종목을
    두고 색인은 주간 값을, 그 둘은 야간 값을 냅니다.
    """
    held = contracts.get(key)
    if held is not None:
        held.clear()
        held.update(folded)
        return

    slot = _slot_of(market, contract)
    found = expiries.get(slot)
    if found is not None:
        # 옵션의 잔존일수는 만기 칸 하나가 들고 있습니다 — 같은 만기의 종목이
        # 모두 같은 값입니다.
        found["rem_cnt"] = _rem_cnt_of(contract)
    made = chains.get(slot)
    if made is not None:
        chains[slot] = [now if one == was else one for one in made]


def _slot_of(market: str, contract: dict[str, str]) -> tuple[str, str, str]:
    """그 종목이 서는 만기 칸.

    **만기를 가르는 것은 날짜입니다.** 8자리 만기일이 오면 그것을 쓰고, 없으면
    만기년월(6자리)이 그 파일이 가를 수 있는 최대입니다. 국내는 늘 날짜가 오고,
    해외는 **OPRA 옵션만** 옵니다(`_last_trading_day`) — 그래서 해외 옵션은
    파일에 따라 날 단위와 달 단위가 섞입니다.
    """
    return (
        market,
        contract["product_code"],
        str(contract.get("expiry_date") or contract["expiry"]),
    )


def _hashable(contract: dict[str, object]) -> dict[str, object]:
    """지문을 낼 수 있는 모양으로. 원문 컬럼은 전부 글자지만 곁들이는 값에는
    사전(스프레드의 `legs`)이 있어 그대로는 해시가 안 됩니다. 값이 달라지면
    글자도 달라지므로 지문의 뜻은 그대로입니다."""
    return {
        key: value if isinstance(value, str) else repr(value)
        for key, value in contract.items()
    }


def _conflicting_fields(kept: dict[str, str], other: dict[str, str]) -> list[str]:
    """같은 종목코드를 단 두 줄에서 값이 어긋나는 컬럼.

    접어서 내보내는 컬럼은 두 줄에서 같아야 합니다. **그게 정말 같은지는 배포되는
    파일을 재 봐야 아는 것이지 보장된 성질이 아닙니다.** 그래서 접기 전에 매번
    확인합니다 — 어긋나면 어느 줄이 남을지가 파일 읽는 순서로 정해지고, 예외 없이
    값만 틀립니다.

    잡히는 경우가 둘입니다.

    * 같은 종목의 주간·야간 줄인데 같아야 할 컬럼이 갈리기 시작한 경우
    * **서로 다른 종목이 같은 코드를 달고 온 경우.** 배포 파일에 실제로 있습니다 —
      만기가 다른 두 계약이 같은 코드로 오는 행이 있습니다

    세션마다 값이 다른 것이 정상인 컬럼은 빼고 봅니다(`_DAY_VALUE_FIELDS`). 공백
    모양만 다른 값도 같은 값으로 봅니다(`_comparable`).
    """
    return sorted(
        k
        for k, v in other.items()
        if k not in _DAY_VALUE_FIELDS
        and _comparable(kept.get(k)) != _comparable(v)
    )


@dataclass(frozen=True)
class MasterIndex:
    """색인 한 벌.

    `payload` 는 `/api/master/index` 가 그대로 돌려주는 것이고, 체인(품목 하나의
    한 만기월에 걸린 옵션들)은 따로 들고 있습니다. 옵션이 14만 행이라 체인까지
    `payload` 에 담으면 화면이 들고 있기 무겁습니다.

    체인은 **줄 내용이 아니라 줄 위치**로 들고 있습니다. 내용을 다 들고 있으면
    14만 개의 사전이 앱이 켜져 있는 내내 남습니다. 위치만 두면 체인을 열 때 그
    체인의 수천 줄만 만들면 됩니다.

    어느 줄을 남길지(주간·야간 접기)는 **색인을 만들 때 이미 정해집니다.** 체인이
    같은 판단을 다시 하지 않으므로 색인과 체인이 서로 다른 답을 낼 수 없습니다.
    """

    payload: dict[str, object]
    #: (시장, 품목코드, 만기년월) → 그 체인에 든 줄의 위치 (파일 이름, 줄 번호)
    chains: dict[tuple[str, str, str], tuple[tuple[str, int], ...]]
    #: 파일 이름 → 파싱한 표
    tables: dict[str, MasterTable]
    #: 야간 줄이 있는 국내 품목(`under_name`). 세션 판정이 「이 품목에 야간이 있나」를
    #: 여기서 봅니다 — 접힌 종목 행에는 `cme_tp` 가 없어 알 수 없습니다.
    night_products: frozenset[str] = frozenset()
    #: 시장 → (코드 → (표 이름, 줄 번호)). `locate` 가 처음 물을 때 채웁니다.
    positions: dict[str, dict[str, tuple[str, int]]] = field(default_factory=dict)
    #: 시장 → (품목 열쇠 → 품목의 최소 줄). `product_of` 가 처음 물을 때 채웁니다.
    #: 열쇠는 행의 진짜 칸 — 국내 `trd_code`(거래대상코드), 해외 `comd`(품목코드).
    product_keys: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)

    def locate(self, market: str, code: str) -> dict[str, str] | None:
        """종목코드 하나의 줄. 없으면 `None`.

        색인은 옵션 행을 안 담으므로 표에서 찾습니다. 처음 물을 때 그 시장의 코드가
        어느 표 몇째 줄인지를 한 번 적어 두고(`positions`), 그 뒤로는 바로 찾습니다 —
        종목마다 열여덟 만 줄을 다시 돌지 않습니다. 주간·야간 두 줄이 있으면 먼저
        만난 줄이고, 세션마다 갈리는 컬럼은 접혀 있습니다.
        """
        wanted = str(code or "").strip()
        if not wanted:
            return None
        found = self._positions(market).get(wanted)
        if found is None:
            return None
        table = self.tables[found[0]]
        return _fold(_enrich(table, dict(zip(table.fields, table.rows[found[1]]))))

    def product_of(self, market: str, code: str) -> dict[str, str] | None:
        """종목코드가 가리키는 **품목**의 최소 줄. 종목이 마스터에 없어도 코드 모양으로 풉니다.

        세션 판정처럼 「어느 품목인가」만 있으면 되는 자리를 위한 것입니다 — 만기가
        지나 마스터에서 빠진 옵션, 아직 실리지 않은 종목, 해외의 행사가 없는 시리즈
        코드(`O_ESU26`)가 그렇습니다. 종목이 있으면 `locate` 의 줄을 그대로 주고,
        없으면 코드에서 푼 품목의 줄을 줍니다 — 거기에는 만기가 없으므로 최종거래일
        판정은 못 합니다. 어느 품목인지도 못 풀면 `None` 입니다.
        """
        found = self.locate(market, code)
        if found is not None:
            return found
        wanted = str(code or "").strip()
        key = _product_key(market, wanted)
        if key is None:
            return None
        return self._product_keys(market).get(key)

    def _product_keys(self, market: str) -> dict[str, dict[str, str]]:
        """그 시장의 품목 열쇠 → 품목의 최소 줄. 처음 부를 때 표에서 만듭니다.

        열쇠는 행의 칸에서 그대로 읽습니다 — 코드에서 뽑지 않습니다. 코드 규칙이 못
        푸는 모양(해외 위클리 옵션)이 표를 오염시키지 않게 하려는 것입니다.
        """
        made = self.product_keys.get(market)
        if made is None:
            made = {}
            key_field = _PRODUCT_KEY_FIELD[market]
            for table in self.tables.values():
                if table.market != market:
                    continue
                key_at = table.index_of(key_field)
                for row in table.rows:
                    key = row[key_at].strip()
                    if not key or key in made:
                        continue
                    made[key] = _product_line(market, dict(zip(table.fields, row)), key)
            self.product_keys[market] = made
        return made

    def _positions(self, market: str) -> dict[str, tuple[str, int]]:
        """그 시장의 코드 → (표 이름, 줄 번호). 처음 부를 때 만듭니다."""
        made = self.positions.get(market)
        if made is None:
            # **주간 줄을 고릅니다.** 세션마다 값이 다른 칸(`_DAY_VALUE_FIELDS`)이
            # 있어, 먼저 만난 줄을 그냥 쓰면 파일 차례에 따라 `locate` 가 색인과
            # 다른 값을 냅니다.
            found: dict[str, tuple[str, int, bool]] = {}
            for table in self.tables.values():
                if table.market != market:
                    continue
                code_at = table.index_of(_code_field(market))
                night_at = table.index_of("cme_tp") if "cme_tp" in table.fields else None
                for at, raw in enumerate(table.rows):
                    night = night_at is not None and raw[night_at] == NIGHT_SESSION
                    held = found.get(raw[code_at])
                    if held is None or (held[2] and not night):
                        found[raw[code_at]] = (table.name, at, night)
            made = {code: (name, at) for code, (name, at, _) in found.items()}
            self.positions[market] = made
        return made

    def chain(self, *, market: str, product_code: str, expiry: str) -> list[dict[str, str]]:
        """품목 하나의 한 만기에 걸린 옵션 종목.

        색인에 행사가 행이 없으므로 체인을 열 때 여기서 받아 갑니다. 한 덩어리가
        가장 클 때도 사천 행 안팎입니다.

        **만기는 날짜 8자리로도, 월 6자리로도 받습니다.** 체인이 서는 자리는 만기일
        하나이지만, 월물 품목을 보는 쪽은 여전히 월로 묻습니다. 6자리로 물으면 그
        달에 걸린 만기를 **모두** 모아 줍니다 — 위클리 품목을 월로 물으면 그 달의
        주간 만기가 한 덩어리로 옵니다.
        """
        wanted = [(market, product_code, expiry)]
        if len(expiry) == 6:
            wanted = [
                slot
                for slot in self.chains
                if slot[0] == market
                and slot[1] == product_code
                and str(slot[2]).startswith(expiry)
            ]
        rows: list[dict[str, str]] = []
        for slot in wanted:
            for name, at in self.chains.get(slot, ()):
                table = self.tables[name]
                rows.append(
                    _fold(_enrich(table, dict(zip(table.fields, table.rows[at]))))
                )
        return rows


def build_index(loaded: Iterable[LoadedMaster], *, today: date | None = None) -> MasterIndex:
    """화면이 앱을 켤 때 한 번 받아 두는 색인을 만듭니다.

    `today` 는 잔존일을 셀 기준 날짜입니다. 비우면 **한국시간의 오늘**이고, 시험이
    날짜를 고정할 때만 넘깁니다.

    `payload` 에 담기는 것은 다섯입니다.

    * `products` — 품목 전부. **선물이냐 옵션이냐로 나누지 않습니다** — 어느 축으로
      나눌지는 화면이 정할 일이라 여기서 굳히면 되돌릴 수 없습니다. 대신
      `contract_counts` 에 종류별 수를 담아, 화면이 원하는 축을 스스로 만들 수 있게
      합니다. 스프레드만 있는 품목이 실제로 있어서, 스프레드를 목록에서 빼면 「월물이
      하나도 없는 품목」이 생깁니다
    * `contracts` — **선물·스프레드 월물 전부.** 국내는 종목코드로 접고, 세션마다
      달라지는 컬럼을 뺀 나머지 원문 컬럼을 그대로 담습니다
    * `option_expiries` — 옵션 품목의 만기월 목록
    * `domestic_option_index` — 국내 옵션 종목의 **신원만** (코드·품목·만기·콜풋)
    * `folding_conflicts` — 접다가 값이 어긋난 종목. 보통은 빈 목록입니다
    * `width_candidates` — 값 칸 폭을 잡기 위한 후보 행. 자릿수·표기가 같은 무리마다
      가장 긴 행 한둘을 원문 그대로 담습니다. 그리는 것은 화면의 일입니다

    옵션 행사가 행을 `contracts` 에 안 담는 이유는 수 때문입니다 — 옵션이 14만 행이라
    전부 담으면 화면이 들고 있기 무겁습니다. 행사가는 체인을 열 때 받아 갑니다.

    그런데 **국내 옵션은 신원표를 따로 둡니다.** 해외 옵션 코드는 품목·만기·행사가가
    코드 안에 다 들어 있어(`O_CLF27-C10.00`) 코드만으로 풀리지만, 국내 옵션 코드는
    위클리가 주차 표기(`2609W1`)라 만기월 규칙을 안 따릅니다. 규칙을 갖다 대면 예외가
    나는 게 아니라 **다른 달이 조용히 나옵니다.** 신원표는 행사가·종목명·상한가를 안
    담아서 무겁지 않습니다.
    """
    tables = {table.name: table for table in _tables_of(loaded)}

    products: dict[tuple[str, str], dict[str, object]] = {}
    hints: dict[tuple[str, str], dict[str, str]] = {}
    contracts: dict[tuple[str, str], dict[str, str]] = {}
    expiries: dict[tuple[str, str, str], int] = {}
    domestic_options: dict[str, tuple[str, ...]] = {}
    chains: dict[tuple[str, str, str], list[tuple[str, int]]] = {}
    conflicts: dict[tuple[str, str], list[str]] = {}
    # 접기 검사에 쓰는 자리. 줄 내용을 들고 있으면 15만 개의 사전이 앱이 켜져 있는
    # 내내 남으므로, 어디 있는 줄인지와 값의 지문만 둡니다.
    seen: dict[tuple[str, str], tuple[str, int, int, bool]] = {}
    widths = WidthCandidates()

    night_products: set[str] = set()

    for table in tables.values():
        code_at = table.index_of(_code_field(table.market))
        for at, raw in enumerate(table.rows):
            key = (table.market, raw[code_at])
            contract = _enrich(table, dict(zip(table.fields, raw)))

            night = contract.get("cme_tp") == NIGHT_SESSION
            if table.market == DOMESTIC and night:
                night_products.add(str(contract["product_code"]))

            # 값 칸 폭 후보는 **접기 전에** 셉니다. 접히는 줄도 화면에 서고,
            # 옵션은 색인에 안 담기지만 국내에서는 행사가가 가장 긴 값을 냅니다.
            widths.add(table.market, contract)

            folded = _fold(contract)
            fingerprint = _fingerprint(folded)

            already = seen.get(key)
            if already is not None:
                if already[3] and not night:
                    # **야간 줄이 먼저 온 자리입니다.** 색인이 내는 줄도, 체인이
                    # 들고 있는 위치도 주간 줄로 바꿉니다.
                    _adopt_day_row(
                        key,
                        table.market,
                        contract,
                        folded,
                        (already[0], already[1]),
                        (table.name, at),
                        contracts,
                        expiries,
                        chains,
                    )
                    seen[key] = (table.name, at, already[2], False)
                if already[2] != fingerprint:
                    # 지문이 다를 때만 남은 줄을 되살려 어느 컬럼이 갈리는지 봅니다.
                    kept_table = tables[already[0]]
                    kept = _fold(
                        _enrich(
                            kept_table,
                            dict(zip(kept_table.fields, kept_table.rows[already[1]])),
                        )
                    )
                    differing = _conflicting_fields(kept, folded)
                    if differing:
                        conflicts.setdefault(key, differing)
                continue

            seen[key] = (table.name, at, fingerprint, night)
            kind = contract["kind"]
            slot = _slot_of(table.market, contract)

            if kind == OPTION:
                found = expiries.get(slot)
                if found is None:
                    expiries[slot] = found = {
                        "expiry": contract["expiry"],
                        "expiry_date": contract.get("expiry_date"),
                        # 잔존일수는 같은 만기의 종목이 모두 같은 값을 가집니다.
                        "rem_cnt": _rem_cnt_of(contract),
                        "contract_count": 0,
                    }
                found["contract_count"] = int(found["contract_count"]) + 1
                chains.setdefault(slot, []).append((table.name, at))
                if table.market == DOMESTIC:
                    domestic_options[raw[code_at]] = tuple(
                        contract[field] for field in DOMESTIC_OPTION_INDEX_FIELDS
                    )
            else:
                contracts[key] = folded

            product_key = (table.market, contract["product_code"])
            product = products.get(product_key)
            if product is None:
                products[product_key] = product = {
                    "market": table.market,
                    "product_code": contract["product_code"],
                    "product_name": contract["product_name"],
                    "expiry_unit": _expiry_unit(
                        table.market, str(contract["product_name"])
                    ),
                    "contract_counts": {FUTURES: 0, OPTION: 0, SPREAD: 0},
                }
                hints[product_key] = _hints_of(contract)
            else:
                # 품목마다 하나만 실을 값이므로, 종목마다 같은지 매번 확인합니다.
                # 갈리는 컬럼은 그 품목에서만 뺍니다 — 종목 행에는 늘 남아 있습니다.
                hints[product_key] = {
                    k: v for k, v in hints[product_key].items() if contract.get(k) == v
                }

            counts = product["contract_counts"]
            counts[kind] = int(counts.get(kind, 0)) + 1  # type: ignore[union-attr]

    if conflicts:
        _drop_conflicts(
            conflicts, tables, products, contracts, chains, expiries, domestic_options
        )

    # 슬롯에서 빠진 종목이 반영된 **뒤에** 셉니다. 어긋나서 뺀 줄이 시리즈 판정을
    # 흔들면 안 됩니다.
    _fill_slots(expiries, chains, tables, today=today or now_kst().date())

    for product_key, product in products.items():
        product |= hints.get(product_key, {})

    payload: dict[str, object] = {
        "products": sorted(
            products.values(), key=lambda p: (str(p["market"]), str(p["product_code"]))
        ),
        "contracts": list(contracts.values()),
        "option_expiries": [
            {
                "market": market,
                "product_code": product_code,
                **found,
            }
            for (market, product_code, _), found in sorted(expiries.items())
        ],
        "domestic_option_index": {
            "fields": list(DOMESTIC_OPTION_INDEX_FIELDS),
            "rows": [list(row) for row in domestic_options.values()],
        },
        "folding_conflicts": [
            {"market": market, "code": code, "fields": fields}
            for (market, code), fields in sorted(conflicts.items())
        ],
        "width_candidates": widths.payload(),
    }
    return MasterIndex(
        payload=payload,
        chains={slot: tuple(found) for slot, found in chains.items()},
        tables=tables,
        night_products=frozenset(night_products),
    )


def _fill_slots(
    expiries: dict[tuple[str, str, str], dict[str, object]],
    chains: dict[tuple[str, str, str], list[tuple[str, int]]],
    tables: dict[str, MasterTable],
    *,
    today: date,
) -> None:
    """만기 슬롯마다 **시리즈 코드**와 **잔존일**을 채웁니다.

    `series_code` 는 그 슬롯의 체인을 부를 때 쓰는 코드입니다 — 해외 옵션 종목코드
    에서 행사가를 뗀 것(`O_SPW3RU26-C6000.0` → `O_SPW3RU26`)이고, **위클리는 슬롯마다
    다릅니다**(같은 달 안에서 주마다 시리즈가 갈립니다). 한 슬롯에 시리즈가 둘 이상
    이면 어느 것으로 불러야 할지 알 수 없어 비우고 알립니다.

    국내는 비어 있습니다 — 체인을 품목코드와 만기년월로 부르므로 시리즈 코드를
    쓰지 않습니다.

    `days_left` 는 만기까지 남은 날입니다. 국내는 마스터가 `rem_cnt` 로 주므로 그
    값을 그대로 쓰고, 해외는 그 칸이 없어 만기일에서 셉니다(만기일을 아는 것은 OPRA
    옵션뿐입니다). **두 시장이 같은 기준으로 셉니다** — 오늘을 세어 넣어 만기 당일이
    1 이고, 지난 만기는 0 입니다(까닭은 `_days_until` 에).
    """
    for slot, found in expiries.items():
        market = slot[0]
        codes = {
            series
            for series in (
                _series_of(market, tables[name].rows[at][tables[name].index_of(_code_field(market))])
                for name, at in chains.get(slot, ())
            )
            if series
        }
        if len(codes) > 1:
            logger.warning(
                "%s %s 만기(%s)에 시리즈가 둘 이상입니다: %s. 체인을 부를 코드를 정하지 못해 비웁니다.",
                market,
                slot[1],
                slot[2],
                ", ".join(sorted(codes)),
            )
        found["series_code"] = codes.pop() if len(codes) == 1 else None
        found["days_left"] = (
            found.get("rem_cnt")
            if market == DOMESTIC
            else _days_until(found.get("expiry_date"), today)
        )


def _series_of(market: str, code: str) -> str | None:
    """옵션 종목코드에서 시리즈 코드. 국내이거나 못 뗄 모양이면 `None`."""
    if market == DOMESTIC:
        return None
    body = str(code or "").strip()
    return _OVERSEAS_STRIKE_IN_CODE.sub("", body) or None


def _days_until(expiry_date: object, today: date) -> int | None:
    """만기일까지 남은 날. 날짜가 아니면 `None`, 이미 지났으면 0.

    **NH 가 국내 마스터에 적어 주는 `rem_cnt` 와 같은 기준입니다 — 오늘을 세어
    넣은 달력 일수**라 만기 당일이 1 입니다. 마스터의 값은
    `rem_cnt == (만기일 − 배포일) + 1` 입니다.

    기준을 맞추지 않으면 같은 날 만기인 국내·해외 종목이 화면에서 「D-1」과
    「D-0」으로 갈립니다.
    """
    body = str(expiry_date or "").strip()
    if len(body) != 8 or not body.isdigit():
        return None
    try:
        day = datetime.strptime(body, "%Y%m%d").date()
    except ValueError:
        return None
    return max((day - today).days + 1, 0)


def days_left_of(row: dict[str, Any], market: str, today: date | None = None) -> int | None:
    """종목 한 줄의 남은 날. **만기 슬롯과 같은 기준입니다.**

    국내는 마스터가 적어 주는 `rem_cnt` 를 그대로 쓰고, 해외는 그 칸이 없어
    만기일에서 셉니다. 기준을 두 곳에서 세면 같은 날 만기인 국내·해외 종목이
    화면에서 「D-1」과 「D-0」으로 갈립니다 — 그 까닭은 `_days_until` 에.
    """
    if market == DOMESTIC:
        body = str(row.get("rem_cnt") or "").strip()
        return int(body) if body.isdigit() else None
    return _days_until(row.get("expiry_date"), today or date.today())


def _drop_conflicts(
    conflicts: dict[tuple[str, str], list[str]],
    tables: dict[str, MasterTable],
    products: dict[tuple[str, str], dict[str, object]],
    contracts: dict[tuple[str, str], dict[str, str]],
    chains: dict[tuple[str, str, str], list[tuple[str, int]]],
    expiries: dict[tuple[str, str, str], int],
    domestic_options: dict[str, tuple[str, ...]],
) -> None:
    """값이 어긋난 종목을 색인과 체인 양쪽에서 뺍니다.

    어느 줄이 맞는지 알 수 없는데 한쪽을 내보내면 화면에는 멀쩡한 종목으로 보이고
    값만 틀립니다. **주문이 그 코드로 나가는 자리**라 없는 편이 낫습니다. 나머지
    종목은 그대로 씁니다 — 한 종목 때문에 파일을 통째로 버릴 이유가 없습니다.

    서로 다른 두 계약이 같은 코드를 달고 온 경우에도 둘 다 뺍니다. 코드로 계약을
    가릴 수 없으면 어느 쪽을 내보내도 반은 틀리기 때문입니다. 무엇을 뺐는지는
    `/api/master/status` 의 `folding_conflicts` 에 남습니다.
    """
    for market, code in conflicts:
        dropped = contracts.pop((market, code), None)
        if dropped is not None:
            counts = products[(market, dropped["product_code"])]["contract_counts"]
            counts[dropped["kind"]] = int(counts.get(dropped["kind"], 0)) - 1  # type: ignore[union-attr]
            continue

        # 옵션이면 체인과 신원표에서도 뺍니다.
        domestic_options.pop(code, None)
        code_field = _code_field(market)
        for slot, found in list(chains.items()):
            if slot[0] != market:
                continue
            kept = [
                (name, at)
                for name, at in found
                if tables[name].rows[at][tables[name].index_of(code_field)] != code
            ]
            if len(kept) == len(found):
                continue
            removed = len(found) - len(kept)
            chains[slot] = kept
            found = expiries.get(slot)
            if found is not None:
                found["contract_count"] = int(found["contract_count"]) - removed
            counts = products[(market, slot[1])]["contract_counts"]
            counts[OPTION] = int(counts.get(OPTION, 0)) - removed  # type: ignore[union-attr]

    # **무슨 일이 생겼는지를 먼저 적습니다.** 기록을 읽는 사람이 알아야 하는 것은
    # 칸 이름이 아니라 「그 종목이 화면에서 사라졌고, 이 앱에서 고칠 수 있는 일이
    # 아니라는 것」입니다. 칸 이름은 뒤에 둡니다 — 물어 오는 사람이 이 줄 하나만
    # 옮겨 적는 일이 많아 빼지는 않습니다.
    logger.warning(
        "종목 %d개를 색인에서 뺐습니다 — 목록·검색·주문에 나오지 않습니다. "
        "같은 종목코드로 서로 다른 계약이 두 줄 와서 어느 쪽인지 가릴 수 없어 둘 다 "
        "뺐습니다(종목 마스터 자료의 문제라 앱에서 고칠 수 없습니다). 어긋난 칸: %s",
        len(conflicts),
        "; ".join(f"{code}({', '.join(fields)})" for (_, code), fields in conflicts.items()),
    )
