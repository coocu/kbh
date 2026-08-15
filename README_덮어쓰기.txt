녹음실 예약 시스템 - 다중 학원 최소수정

[서버 덮어쓰기 파일]
main.py
models.py
schemas.py
security.py
auth_adapter.py
templates/login.html
templates/forgot_password.html
templates/reset_password.html

[핵심 변경]
- 서버/웹 관리자 화면을 특정 학원 전용에서 '녹음실 예약 시스템' 공용 구조로 변경
- 로그인 전에 등록된 학원 선택
- 학원별 원생/강사, 녹음실, 예약, 예약불가, 관리자 비밀번호 완전 분리
- 다른 학원끼리 같은 이름/전화번호/녹음실명/예약시간을 사용해도 서로 충돌하지 않음
- 웹 로그인 화면에 '학원등록' 추가
- 학원등록: Pocket 인증키 확인 -> 학원명 / 최초 관리자 성함 / 전화번호 끝 4자리 / 관리자 비밀번호 등록
- Pocket 인증키는 기존 Pocket 서버(https://poketserver.onrender.com/app/check)의 실제 인증 방식 사용
- 인증키 자체는 예약 서버 DB에 저장하지 않음
- 비밀번호 찾기는 선택한 학원에 등록된 최초 관리자 성함 + 전화번호 끝 4자리로 각각 확인
- 기존 관리자 4개 탭과 회원/예약 검색 기능은 그대로 유지

[기존 킴스 데이터]
첫 배포 시 기존 단일학원 테이블이 있으면 새 다중학원 테이블로 1회 복사합니다.
기존 녹음실 / 원생·강사 / 예약 / 예약불가 / 현재 관리자 비밀번호는 유지됩니다.
기존 테이블은 삭제하지 않으므로 원본 데이터도 남아 있습니다.

[중요 적용 순서]
1. 서버 파일 먼저 덮어쓰기 및 Render 배포
2. 서버 배포 완료 후 iOS 앱 수정파일 덮어쓰기 및 빌드

[Git 예시]
git add main.py models.py schemas.py security.py auth_adapter.py templates/login.html templates/forgot_password.html templates/reset_password.html
git commit -m "다중 학원 녹음실 예약 시스템 전환"
git push origin main
