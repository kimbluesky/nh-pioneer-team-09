# [제보] NH FUTURES API 앱: 청산 요청을 두 번 보내면 반대 포지션이 생깁니다

팀: team-09  
환경: Windows / Python 3.12.10, 앱 가상 체결의 로컬 테스트(합성 시세 사용, NH 서버 호출 없음), 2026-11-11 16:10:34 KST  
대상: NH FUTURES API 앱 > 전략 검증 > 선택 전략 청산 (GitHub fbfca3e)  

## 재현 절차

1. Python 환경이 준비된 NH 프로젝트 폴더에 아래 재현 ZIP을 풀고 실행

   ```powershell
   .\.venv\Scripts\python.exe -B error_audit\repro_R05.py
   ```

2. 스크립트에서 매수 1계약 체결 후 청산 요청. 첫 청산이 미체결인 상태에서 같은 청산 요청을 한 번 더 보냄
3. 테스트용 호가로 두 주문을 체결한 뒤 포지션 확인
4. 비교: 같은 조건에서 청산을 한 번만 요청

## 기대 결과

이미 접수된 청산 주문을 고려해 추가 청산을 막고, 체결 후 포지션이 0계약이 됨

## 실제 결과

- 청산 1회: 최종 0계약
- 청산 2회: 매도 청산 주문이 1계약씩 2건 생김. 모두 체결되면 매도 1계약(position=-1)이 남음
- 두 내부 청산 요청 모두 HTTP 200으로 처리됨

## 로그·캡처

실행 결과: https://github.com/kimbluesky/nh-pioneer-team-09/blob/b8d21fc12368bf3815cb5d61f74b71f4c20e60a6/error_reports/R05_중복청산_반대포지션/result.json
재현 코드: https://github.com/kimbluesky/nh-pioneer-team-09/blob/b8d21fc12368bf3815cb5d61f74b71f4c20e60a6/error_reports/R05_중복청산_반대포지션/reproduce_R05.zip

## 영향

매수 포지션을 청산하려던 전략에 매도 포지션이 생겨 앱의 가상 체결 결과가 잘못됨

## 개선 제안

미체결 청산 수량을 확인해 중복 주문이 나가지 않도록 처리
