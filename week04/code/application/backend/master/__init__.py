"""종목 마스터 — 내려받기, 파싱, 색인.

NH선물은 상장된 종목의 목록을 REST API 가 아니라 **파일**로 배포합니다. 이 꾸러미는
그 파일 아홉 개를 하루 한 번 받아 두고, 파싱해서 `/api/master` 아래로 내놓습니다.

    master_files      아홉 개 파일의 이름·주소·컬럼 배치
    master_parser     한 파일을 표로 바꾸기
    master_store      받은 파일을 사용자 홈에 날짜별로 두고 다시 읽기
    master_download   주소에서 받아 압축을 풀기
    master_index      화면이 쓸 색인 만들기
    master_routes     `/api/master` 라우트
"""
