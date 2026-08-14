# Legacy compatibility file.
# Authentication is now handled only by the KBH server database in main.py.

class AuthNotConfigured(RuntimeError):
    pass


async def verify_license_key(license_key: str) -> bool:
    return False
