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
REGISTRATION_TOKEN_MAX_AGE_SECONDS = 60 * 10
RECOVERY_TOKEN_MAX_AGE_SECONDS = 60 * 10
PASSWORD_ITERATIONS = 240_000

_secret = os.getenv("ADMIN_SESSION_SECRET", "")
if not _secret:
    _secret = "local-dev-only-change-me"

_serializer = URLSafeTimedSerializer(_secret, salt="recording-admin-session-v3")
_app_serializer = URLSafeTimedSerializer(_secret, salt="recording-app-token-v3")
_recovery_serializer = URLSafeTimedSerializer(_secret, salt="recording-admin-recovery-v3")
_registration_serializer = URLSafeTimedSerializer(_secret, salt="recording-academy-registration-v1")
_management_serializer = URLSafeTimedSerializer(_secret, salt="recording-academy-management-v1")


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
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()


def _current_admin_password_version(academy_id: int) -> str:
    from db import SessionLocal
    from models import AdminCredential

    with SessionLocal() as db:
        credential = db.get(AdminCredential, academy_id)
        if credential is None:
            raise HTTPException(status_code=503, detail="관리자 비밀번호가 설정되어 있지 않습니다.")
        return admin_password_version(credential.password_hash)


def _ensure_academy_active(academy_id: int) -> None:
    # 비활성화된 학원은 기존 로그인 세션/앱 토큰이 남아 있어도 사용할 수 없게 한다.
    from db import SessionLocal
    from models import Academy

    with SessionLocal() as db:
        academy = db.get(Academy, academy_id)
        if academy is None or not academy.is_active:
            raise HTTPException(status_code=401, detail="현재 비활성화된 학원입니다.")


def create_admin_session(academy_id: int, password_hash: str) -> str:
    return _serializer.dumps({
        "role": "admin",
        "academy_id": academy_id,
        "password_version": admin_password_version(password_hash),
    })


def _validate_admin_data(data: dict) -> dict:
    if data.get("role") != "admin" and data.get("scope") != "admin_app":
        raise HTTPException(status_code=403, detail="관리자 권한이 없습니다.")

    academy_id = data.get("academy_id")
    if not isinstance(academy_id, int):
        raise HTTPException(status_code=401, detail="관리자 로그인을 다시 해 주세요.")

    if data.get("password_version") != _current_admin_password_version(academy_id):
        raise HTTPException(status_code=401, detail="관리자 비밀번호가 변경되었습니다. 다시 로그인해 주세요.")
    _ensure_academy_active(academy_id)
    return data


def _load_web_admin_cookie(token: str) -> dict:
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="관리자 세션이 만료되었습니다.")
    return _validate_admin_data(data)


def _load_admin_app_token(token: str) -> dict:
    try:
        data = _app_serializer.loads(token, max_age=ADMIN_APP_TOKEN_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="관리자 앱 인증이 만료되었습니다.")

    if data.get("scope") != "admin_app":
        raise HTTPException(status_code=403, detail="관리자 권한이 없습니다.")
    return _validate_admin_data(data)


def require_admin(request: Request) -> dict:
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


def create_admin_recovery_token(academy_id: int) -> str:
    return _recovery_serializer.dumps({
        "scope": "admin_password_recovery",
        "academy_id": academy_id,
    })


def verify_admin_recovery_token(token: str) -> dict:
    try:
        data = _recovery_serializer.loads(token, max_age=RECOVERY_TOKEN_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="비밀번호 찾기 인증 시간이 만료되었습니다.")

    if data.get("scope") != "admin_password_recovery" or not isinstance(data.get("academy_id"), int):
        raise HTTPException(status_code=403, detail="잘못된 비밀번호 찾기 인증입니다.")
    return data


def create_academy_registration_token() -> str:
    return _registration_serializer.dumps({
        "scope": "academy_registration",
        "nonce": secrets.token_urlsafe(12),
    })


def verify_academy_registration_token(token: str) -> dict:
    try:
        data = _registration_serializer.loads(token, max_age=REGISTRATION_TOKEN_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="학원 등록 인증 시간이 만료되었습니다. 인증키를 다시 확인해 주세요.")

    if data.get("scope") != "academy_registration":
        raise HTTPException(status_code=403, detail="잘못된 학원 등록 인증입니다.")
    return data


def create_academy_management_token() -> str:
    return _management_serializer.dumps({
        "scope": "academy_management",
        "nonce": secrets.token_urlsafe(12),
    })


def verify_academy_management_token(token: str) -> dict:
    try:
        data = _management_serializer.loads(token, max_age=REGISTRATION_TOKEN_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="학원관리 인증 시간이 만료되었습니다. 인증키를 다시 확인해 주세요.")

    if data.get("scope") != "academy_management":
        raise HTTPException(status_code=403, detail="잘못된 학원관리 인증입니다.")
    return data


def create_app_token(academy_id: int, academy_name: str, name: str, phone_last4: str) -> str:
    return _app_serializer.dumps({
        "academy_id": academy_id,
        "academy_name": academy_name,
        "scope": "reservation_app",
        "name": name,
        "phone_last4": phone_last4,
    })


def create_admin_app_token(academy_id: int, academy_name: str, password_hash: str) -> str:
    return _app_serializer.dumps({
        "academy_id": academy_id,
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

    if data.get("scope") != "reservation_app" or not isinstance(data.get("academy_id"), int):
        raise HTTPException(status_code=403, detail="잘못된 앱 인증입니다.")
    _ensure_academy_active(data["academy_id"])
    return data
