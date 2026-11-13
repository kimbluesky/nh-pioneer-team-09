"""국내 종목 마스터의 컬럼 배치.

국내 마스터는 세 파일(`fut_opt` · `idx_fut_opt` · `stk_fut_opt`)로 나뉘어 오지만
**세 파일의 컬럼 배치는 같습니다.** 그래서 목록은 여기 한 번만 두고, 파일별 폴더는
어느 파일이 이 배치를 쓰는지만 밝힙니다.

컬럼 이름은 NH선물이 배포하는 헤더정보 문서의 표기를 그대로 씁니다. 대소문자가
섞여 있는 것(`real_Code`, `kofex_Code`)도 원문 그대로입니다 — 보기 좋게 고치면
스펙 문서와 대조할 때 어느 것이 원문인지 알 수 없게 됩니다.
"""
from __future__ import annotations

#: 국내 선물/옵션 마스터의 컬럼 26개. 파일 안 순서 그대로입니다.
DOMESTIC_FIELDS: tuple[str, ...] = (
    "mrkt_tp",       # 마켓구분 (4.지수선물옵션 5.개별주식선물옵션 6.상품선물옵션)
    "fo_tp",         # 선물/옵션/스프레드 구분 (F:선물 O:옵션 C:스프레드)
    "opt_tp",        # 옵션구분 (1:선물 2:스프레드 3:콜옵션 4:풋옵션)
    "under_name",    # 기초자산명
    "exp_ym",        # 만기년월
    "cme_tp",        # 주야간구분 (day, night)
    "class",         # 분류
    "kospi_key",     # 거래소코드
    "series",        # 종목코드
    "rank_serie",    # 상위종목 (KTB에서만 사용)
    "name_kor",      # 한글종목명
    "name_eng",      # 영문종목명
    "upper_p_lv1",   # 상한가 (1단계)
    "lower_p_lv1",   # 하한가 (1단계)
    "strike_p",      # 행사가격
    "inst_grp_cd",   # 파생상품코드
    "p_dec",         # 소수점정보
    "ctrt_size",     # 계약크기, 거래승수
    "rem_cnt",       # 잔존일수
    "dynmc_yn",      # 실시간상한가적용여부
    "dynmc_up_p",    # 실시간상한가
    "dynmc_dn_p",    # 실시간하한가
    "real_Code",     # 실시간종목코드 (실시간 요청 시 사용하는 코드)
    "kofex_Code",    # 당사코드
    "mrkt_code",     # 시장코드 (01.유가증권(주식) … 08.축산물)
    "trd_code",      # 거래대상코드
)
