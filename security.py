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


def create_admin_session() -> str:
    return _serializer.dumps({"role": "admin"})


def _load_web_admin_cookie(token: str) -> dict:
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="관리자 세션이 만료되었습니다.")
    if data.get("role") != "admin":
        raise HTTPException(status_code=403, detail="관리자 권한이 없습니다.")
    return data


def _load_admin_app_token(token: str) -> dict:
    try:
        data = _app_serializer.loads(token, max_age=ADMIN_APP_TOKEN_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="관리자 앱 인증이 만료되었습니다.")
    if data.get("scope") != "admin_app":
        raise HTTPException(status_code=403, detail="관리자 권한이 없습니다.")
    return data


def require_admin(request: Request) -> dict:
    cookie = request.cookies.get("kbh_admin")
    if cookie:
        return _load_web_admin_cookie(cookie)

    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return _load_admin_app_token(auth[7:].strip())

    raise HTTPException(status_code=401, detail="관리자 로그인이 필요합니다.")


def create_app_token(academy_name: str, name: str, phone_last4: str) -> str:
    return _app_serializer.dumps({
        "academy_name": academy_name,
        "scope": "reservation_app",
        "name": name,
        "phone_last4": phone_last4,
    })


def create_admin_app_token(academy_name: str) -> str:
    return _app_serializer.dumps({
        "academy_name": academy_name,
        "scope": "admin_app",
        "role": "admin",
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
