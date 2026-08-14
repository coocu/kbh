\
import os
import httpx


class AuthNotConfigured(RuntimeError):
    pass


async def verify_license_key(license_key: str) -> bool:
    """
    Exact external auth-server protocol is intentionally NOT guessed.

    Current behavior:
    1) If DEV_LICENSE_KEY is configured, exact match is accepted for testing.
    2) If AUTH_SERVER_URL is present, this function currently raises
       AuthNotConfigured until the auth-server ZIP is supplied and its actual
       request/response contract can be mapped safely.

    After receiving the existing auth-server ZIP, only this adapter needs to be
    changed; reservation logic does not need to be rewritten.
    """
    dev_key = os.getenv("DEV_LICENSE_KEY", "")
    if dev_key and license_key == dev_key:
        return True

    auth_server_url = os.getenv("AUTH_SERVER_URL", "").strip()
    if not auth_server_url:
        raise AuthNotConfigured(
            "인증키 서버가 아직 연결되지 않았습니다. AUTH_SERVER_URL 또는 DEV_LICENSE_KEY가 필요합니다."
        )

    # Do not invent an endpoint or response schema.
    raise AuthNotConfigured(
        "AUTH_SERVER_URL은 설정되어 있지만 기존 인증키 서버의 실제 API 규격이 아직 연결되지 않았습니다."
    )
