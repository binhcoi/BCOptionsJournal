"""Password and session: one user, one password, standard library only.

The password hash lives in ``settings`` so it survives restarts and travels
with the journal. Until one is set, the fixed default logs in and every page
redirects to the Options page until it is changed. A session is a signed,
expiring token in a cookie; changing the password rotates the signing secret,
which logs every browser out.
"""

import base64
import hashlib
import hmac
import secrets
import time

from ..db import store

DEFAULT_PASSWORD = "bcoj"
COOKIE = "bcoj_session"
SESSION_DAYS = 30
_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt}${digest.hex()}"


def verify_password(stored: str, password: str) -> bool:
    try:
        _, iterations, salt, expected = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, AttributeError):
        return False


def password_is_default(conn) -> bool:
    """True until a password has been set; the default then applies."""
    return store.get_setting(conn, "password_hash") is None


def check_password(conn, password: str) -> bool:
    stored = store.get_setting(conn, "password_hash")
    if stored is None:
        return hmac.compare_digest(password, DEFAULT_PASSWORD)
    return verify_password(stored, password)


def set_password(conn, password: str) -> None:
    """Store the new hash and rotate the session secret, so every existing
    session, this browser's included, must log in again."""
    store.set_setting(conn, "password_hash", hash_password(password))
    store.set_setting(conn, "session_secret", secrets.token_urlsafe(32))


def _secret(conn) -> bytes:
    value = store.get_setting(conn, "session_secret")
    if value is None:
        value = secrets.token_urlsafe(32)
        store.set_setting(conn, "session_secret", value)
    return value.encode()


def issue_token(conn) -> str:
    expires = str(int(time.time()) + SESSION_DAYS * 86400)
    sig = hmac.new(_secret(conn), expires.encode(), "sha256").digest()
    return expires + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")


def token_valid(conn, token: str) -> bool:
    if not token or "." not in token:
        return False
    expires, _, sig = token.partition(".")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    want = base64.urlsafe_b64encode(
        hmac.new(_secret(conn), expires.encode(), "sha256").digest()
    ).decode().rstrip("=")
    return hmac.compare_digest(sig, want)


def cookie_header(token: str) -> str:
    return (f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={SESSION_DAYS * 86400}")


def clear_cookie_header() -> str:
    return f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"


def token_from_cookie(header: str) -> str:
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            return value
    return ""
