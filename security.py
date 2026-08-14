import base64
import hashlib
import hmac
import os
import secrets

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SESSION_MAX_AGE_SECONDS = 60 * 60 * 12
APP_TOKEN_MAX_AGE_SECONDS = 60 * 60 * 24 * 365
ADMIN_APP_TOKEN_MAX_AGE_SECONDS = 60 * 60 * 24 * 30
PASSWORD_ITERATIONS = 240_000

_secret = os.getenv("ADMIN_SESSION_SECRET", "")
if not _secret:
    _secret = "local-dev-only-change-me"

_serializer = URLSafeTimedSerializer(_secret, salt="kbh-session-v1")
_app_serializer = URLSafeTimedSerializer(_secret, salt="kbh-app-token-v2")
_recovery_serializer = URLSafeTimedSerializer(_secret, salt="kbh-admin-recovery-v1")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    salt_text = base64.urlsafe_b64encode(salt).decode("ascii")
    digest_text = base64.urlsafe_b64encode(digest).decode("ascii")
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt_text}${digest_text}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
    except Exception:
        return False

    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def admin_password_version(password_hash: str) -> str:
    # 실제 비밀번호나 hash 원문을 토큰에 넣지 않고 비교용 버전값만 사용.
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()


def _current_admin_password_version() -> str:
    # Lazy import로 순환 import를 피한다.
    from db import SessionLocal
    from models import AdminCredential

    with SessionLocal() as db:
        credential = db.get(AdminCredential, 1)
        if credential is None:
            raise HTTPException(status_code=503, detail="관리자 비밀번호가 초기화되지 않았습니다.")
        return admin_password_version(credential.password_hash)


def create_admin_session(password_hash: str) -> str:
    return _serializer.dumps({
        "role": "admin",
        "password_version": admin_password_version(password_hash),
    })


def _load_web_admin_cookie(token: str) -> dict:
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="관리자 세션이 만료되었습니다.")

    if data.get("role") != "admin":
        raise HTTPException(status_code=403, detail="관리자 권한이 없습니다.")

    if data.get("password_version") != _current_admin_password_version():
        raise HTTPException(status_code=401, detail="관리자 비밀번호가 변경되었습니다. 다시 로그인해 주세요.")

    return data


def _load_admin_app_token(token: str) -> dict:
    try:
        data = _app_serializer.loads(token, max_age=ADMIN_APP_TOKEN_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="관리자 앱 인증이 만료되었습니다.")

    if data.get("scope") != "admin_app":
        raise HTTPException(status_code=403, detail="관리자 권한이 없습니다.")

    if data.get("password_version") != _current_admin_password_version():
        raise HTTPException(status_code=401, detail="관리자 비밀번호가 변경되었습니다. 다시 로그인해 주세요.")

    return data


def require_admin(request: Request) -> dict:
    # WKWebView에 이전/만료된 관리자 쿠키가 남아 있어도
    # 유효한 앱 Bearer token이 있으면 그 토큰으로 인증할 수 있게 fallback 한다.
    cookie_error: HTTPException | None = None

    cookie = request.cookies.get("kbh_admin")
    if cookie:
        try:
            return _load_web_admin_cookie(cookie)
        except HTTPException as exc:
            cookie_error = exc

    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return _load_admin_app_token(auth[7:].strip())

    if cookie_error is not None:
        raise cookie_error

    raise HTTPException(status_code=401, detail="관리자 로그인이 필요합니다.")


def create_admin_recovery_token() -> str:
    return _recovery_serializer.dumps({
        "scope": "admin_password_recovery",
    })


def verify_admin_recovery_token(token: str) -> dict:
    try:
        data = _recovery_serializer.loads(token, max_age=60 * 10)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="비밀번호 찾기 인증 시간이 만료되었습니다.")

    if data.get("scope") != "admin_password_recovery":
        raise HTTPException(status_code=403, detail="잘못된 비밀번호 찾기 인증입니다.")

    return data


def create_app_token(academy_name: str, name: str, phone_last4: str) -> str:
    return _app_serializer.dumps({
        "academy_name": academy_name,
        "scope": "reservation_app",
        "name": name,
        "phone_last4": phone_last4,
    })


def create_admin_app_token(academy_name: str, password_hash: str) -> str:
    return _app_serializer.dumps({
        "academy_name": academy_name,
        "scope": "admin_app",
        "role": "admin",
        "password_version": admin_password_version(password_hash),
    })


def require_app_token(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="앱 로그인이 필요합니다.")

    token = auth[7:].strip()
    try:
        data = _app_serializer.loads(token, max_age=APP_TOKEN_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="앱 인증이 만료되었습니다.")

    if data.get("scope") != "reservation_app":
        raise HTTPException(status_code=403, detail="잘못된 앱 인증입니다.")

    return data
