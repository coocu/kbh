import httpx

AUTH_URL = "https://poketserver.onrender.com/app/check"
ALLOWED_KEYS = {"google", "test.kyh", "kim"}


class AuthNotConfigured(RuntimeError):
    pass


async def verify_license_key(license_key: str) -> bool:
    """
    PocketBlackbox(XCode)(3).zip의 인증 서버/프로토콜과 동일하게 확인한다.

    POST https://poketserver.onrender.com/app/check
    Content-Type: application/json
    Body: {"code": "<인증키>"}

    2xx JSON 응답의 token 값이 비어 있지 않을 때만 성공.
    사용자 요구에 따라 google / test.kyh / kim 세 키만 예약 앱에서 허용한다.
    """
    candidate = license_key.strip()
    if candidate.lower() not in ALLOWED_KEYS:
        return False

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                AUTH_URL,
                json={"code": candidate},
                headers={"Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise AuthNotConfigured("포켓 블랙박스 인증 서버에 연결할 수 없습니다.") from exc

    if not (200 <= response.status_code <= 299):
        raise AuthNotConfigured("포켓 블랙박스 인증 서버 응답을 확인할 수 없습니다.")

    try:
        data = response.json()
    except ValueError as exc:
        raise AuthNotConfigured("포켓 블랙박스 인증 서버 응답 형식이 올바르지 않습니다.") from exc

    token = data.get("token")
    if token is None:
        return False

    return bool(str(token).strip())
