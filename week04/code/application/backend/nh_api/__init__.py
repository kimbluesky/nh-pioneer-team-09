"""NH선물 Open API 와 이야기하는 공통 계층.

    catalog     API 목록 — 경로, HTTP 방식, 호출 제한 등급
    client      거래 API 를 부르는 공통 자리
    errors      호출 실패를 나누는 예외
    rate_limit  호출 제한 간격
    session     실서버로 부를지 저장해 둔 응답으로 답할지
    fixtures    저장해 둔 응답 읽기
    replay      저장해 둔 응답으로 답하는 전송 계층

카테고리별 API 함수(`backend/domestic_futureoption/` 등)는 이 계층 위에
올립니다. `auth`, `master` 와 같은 공통 자리라 카테고리 파일 규칙은
적용하지 않습니다.
"""
