import base64
import hashlib
import hmac
import secrets

KEY_MARKER = "fzmsg"


def generate_api_key() -> tuple[str, str, str]:
    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    raw_key = f"{KEY_MARKER}_{prefix}_{secret}"
    return raw_key, prefix, hash_api_key(raw_key)


def hash_api_key(raw_key: str, *, salt: bytes | None = None) -> str:
    actual_salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(raw_key.encode(), salt=actual_salt, n=2**14, r=8, p=1, dklen=32)
    salt_text = base64.urlsafe_b64encode(actual_salt).decode()
    digest_text = base64.urlsafe_b64encode(digest).decode()
    return f"scrypt$16384$8$1${salt_text}${digest_text}"


def verify_api_key(raw_key: str, encoded_hash: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded_hash.split("$", 5)
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            raw_key.encode(),
            salt=base64.urlsafe_b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
        )
        return hmac.compare_digest(actual, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError):
        return False


def api_key_prefix(raw_key: str) -> str | None:
    parts = raw_key.split("_", 2)
    return parts[1] if len(parts) == 3 and parts[0] == KEY_MARKER and len(parts[1]) == 8 else None
