from cryptography.fernet import Fernet
from django.conf import settings


def _fernet() -> Fernet:
    key = (settings.GOOGLE_TOKEN_ENCRYPTION_KEY or "").encode("utf-8")
    if not key:
        raise RuntimeError("Missing GOOGLE_TOKEN_ENCRYPTION_KEY in env")
    try:
        return Fernet(key)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "Invalid GOOGLE_TOKEN_ENCRYPTION_KEY. Expected a 32-byte url-safe base64-encoded key."
        ) from exc


def encrypt_str(value: str) -> str:
    value = value or ""
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_str(value: str) -> str:
    return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")
