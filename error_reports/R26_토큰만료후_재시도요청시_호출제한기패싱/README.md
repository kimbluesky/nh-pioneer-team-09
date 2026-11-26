# [제보] NH FUTURES API: 토큰 만료 뒤 재시도한 요청은 호출 제한기를 거치지 않습니다
팀: team-09
환경: Python 3.13.5 / 원본 코드 오프라인 재현 (합성 데이터 사용, NH 서버 호출 없음), 2026-11-26 KST
대상: 공개 저장소 NH FUTURES API — backend/nh_api/client.py (TradeClient.call)

### 재현 절차:
1. NH FUTURES 프로젝트의 Python 가상환경 또는 같은 라이브러리가 설치된 Python 환경에서 reproduce_R26.zip을 실행합니다. ZIP 압축 해제는 필요 없습니다.
   .\.venv\Scripts\python.exe -B reproduce_R26.zip --output R26_result.json
   (또는 py -3.12 -B reproduce_R26.zip --output R26_result.json)
2. 원본 관련 파일 3개의 SHA-256을 검사하고, 원본 함수를 이용한 대조군과 오류 재현군을 자동 실행합니다.
3. 입력 조건: 모의 국내 현재가 조회; 정상 200 응답과 401 → 200 응답을 가상 HTTP Response로 비교; 실제 서버 호출 없음
4. 정상 대조군과 오류군의 반환값·상태 차이를 비교합니다.

### 기대 결과:
실제 HTTP 요청을 재전송할 때에도 주문/시세 API의 호출 제한을 다시 적용해야 합니다.
정상 대조군: 200 정상 응답 1회: HTTP 전송 1회, 제한기 대기 1회

### 실제 결과:
401 토큰 재발급 필요 응답 → 200 정상 응답: HTTP 전송 2회인데 제한기 대기 1회
- 정상 응답: 전송 1회/대기 1회: 재현 확인
- 401 후 정상: 실제 요청 2회 발생: 재현 확인
- 401 재전송: 제한기 호출은 1회뿐: 재현 확인
- 동일 제한 등급의 모든 전송은 사전 호출 제한기 필요: 재현 확인

### 원본 함수 실행 결과:
PASS R26_SOURCE_INTEGRITY: 3/3
CHECKS: 4/4
[BUG REPRODUCED] R26 401 토큰 재발급 후 API 재전송의 호출 제한 우회

### 근거 (재현 코드·실행 결과):
- reproduce_R26.zip: 실행용 __main__.py, 무수정 원본 소스 스냅샷, SHA-256 확인 자료
- result.json: 대조군·재현군 비교와 세부 관찰값
- source_manifest.json: 검증 대상 원본 파일 크기와 해시값
- README_R26.md: 실행 설명과 재현 조건
출처: nh-futures-open-api-main.zip

### 영향:
토큰 만료 상황에서 인증 재시도 요청이 다른 호출과 겹칠 때 초당 API 요청 횟수 제어가 깨질 수 있습니다.

### 개선 제안:
TradeClient.call()에서 401 이후의 두 번째 _send() 전에 동일 환경·시장·등급의 RateLimiter.wait()를 호출하도록 수정하고 재시도 경로의 속도 제한을 회귀 검증해 주세요.

### 검증 범위: 
업로드된 공개 프로그램 소스를 수정하지 않고 실행한 결과입니다. HTTP·웹소켓 통신과 실계좌에서 동일한 문제가 발생했다는 증거는 아닙니다. 일부 사례는 비정상 외부 입력 또는 동시성 충돌을 통제한 검증으로, 실제 발생 빈도는 별도 확인이 필요합니다.
