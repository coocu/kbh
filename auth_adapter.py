import os

import httpx

AUTH_URL = os.getenv("POCKET_AUTH_URL", "https://poketserver.onrender.com/app/check").strip()


class AuthNotConfigured(RuntimeError):
    pass


async def verify_license_key(license_key: str) -> bool:
    """
    기존 Pocket 인증 서버의 실제 프로토콜을 그대로 사용한다.

    POST /app/check
    JSON: {"code": "<인증키>"}

    2xx 응답의 token 값이 비어 있지 않을 때만 인증 성공으로 처리한다.
    인증키 자체는 녹음실 예약 서버 DB에 저장하지 않는다.
    """
    candidate = license_key.strip()
    if not candidate:
        return False
    if not AUTH_URL:
        raise AuthNotConfigured("Pocket 인증 서버 주소가 설정되어 있지 않습니다.")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                AUTH_URL,
                json={"code": candidate},
                headers={"Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise AuthNotConfigured("Pocket 인증 서버에 연결할 수 없습니다.") from exc

    if not (200 <= response.status_code <= 299):
        return False

    try:
        data = response.json()
    except ValueError as exc:
        raise AuthNotConfigured("Pocket 인증 서버 응답 형식이 올바르지 않습니다.") from exc

    token = data.get("token")
    return token is not None and bool(str(token).strip())
