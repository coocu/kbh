\
import os
from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SESSION_MAX_AGE_SECONDS = 60 * 60 * 12
APP_TOKEN_MAX_AGE_SECONDS = 60 * 60 * 24 * 365

_secret = os.getenv("ADMIN_SESSION_SECRET", "")
if not _secret:
    # Safe enough only for local development. Production startup rejects missing secret.
    _secret = "local-dev-only-change-me"

_serializer = URLSafeTimedSerializer(_secret, salt="kbh-session-v1")
_app_serializer = URLSafeTimedSerializer(_secret, salt="kbh-app-token-v1")


def create_admin_session() -> str:
    return _serializer.dumps({"role": "admin"})


def require_admin(request: Request) -> None:
    token = request.cookies.get("kbh_admin")
    if not token:
        raise HTTPException(status_code=401, detail="관리자 로그인이 필요합니다.")
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="관리자 세션이 만료되었습니다.")
    if data.get("role") != "admin":
        raise HTTPException(status_code=403, detail="관리자 권한이 없습니다.")


def create_app_token(academy_name: str) -> str:
    return _app_serializer.dumps({"academy_name": academy_name, "scope": "reservation_app"})


def require_app_token(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="앱 인증이 필요합니다.")
    token = auth[7:].strip()
    try:
        data = _app_serializer.loads(token, max_age=APP_TOKEN_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="앱 인증이 만료되었습니다.")
    if data.get("scope") != "reservation_app":
        raise HTTPException(status_code=403, detail="잘못된 앱 인증입니다.")
    return data
