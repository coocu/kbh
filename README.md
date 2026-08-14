# 킴스보컬미디학원 예약 서버

FastAPI + PostgreSQL 기반 예약 서버 초안입니다.

## 현재 구현

- 녹음실 추가 / 이름 수정 / 삭제(소프트 삭제)
- 녹음실 일시 사용중지 / 재개
- 예약불가 시간대 지정 / 해제
- 앱 사용자 예약 생성
- 동일 녹음실의 겹치는 예약 차단
- 예약은 현재 시점부터 최대 3개월까지만 생성
- 예약 상태 자동 계산: `예약중` / `사용중` / `사용종료` / `취소됨`
- 관리자 예약 취소
- 관리자 웹: `/admin`
- 공개 앱 API에서는 닉네임/전화번호 끝 4자리를 노출하지 않음
- 관리자 API에서만 닉네임/전화번호 끝 4자리 확인 가능
- 앱 최초 인증용 `/api/v1/auth/register` 준비

## 인증키 서버 연결 상태

기존 인증키 서버 ZIP을 아직 받지 못했기 때문에,
외부 인증서버의 URL/요청 body/성공 응답 형식을 임의로 추측하지 않았습니다.

`auth_adapter.py` 한 파일만 교체하면 예약 로직을 건드리지 않고 연결할 수 있게 분리했습니다.

현재 테스트하려면 Render 환경변수에 `DEV_LICENSE_KEY`를 임시로 넣을 수 있습니다.
실제 인증키 서버 연결 후에는 `DEV_LICENSE_KEY`를 삭제하세요.

## 로컬 실행

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export WEB_ADMIN_PASSWORD='원하는비밀번호'
export ADMIN_SESSION_SECRET='충분히긴랜덤문자열'
export DEV_LICENSE_KEY='test-key'

uvicorn main:app --reload
```

브라우저:

- 관리자: `http://127.0.0.1:8000/admin`
- API 문서: `http://127.0.0.1:8000/docs`
- 상태 확인: `http://127.0.0.1:8000/health`

## Render 배포 핵심값

Python Web Service:

- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`

필수 환경변수:

- `DATABASE_URL` = Render Postgres 내부 연결 URL
- `ACADEMY_NAME` = `킴스보컬미디학원`
- `WEB_ADMIN_PASSWORD` = 관리자 웹 비밀번호
- `ADMIN_SESSION_SECRET` = 긴 랜덤 문자열
- `DEV_LICENSE_KEY` = 실제 인증서버 연결 전 테스트시에만 사용
- `AUTH_SERVER_URL` = 실제 인증서버 연결 후 사용

중요: `.env`는 GitHub에 올리지 마세요.
