"""Encryption of stored secrets (mailbox app passwords, mailbox sign-in tokens).

Values are encrypted with Fernet (AES-128-CBC with HMAC-SHA256) and stored as "enc:<token>". The key comes from
the JOBBOT_SECRET_KEY environment variable or, when that is not set, from DATA/.secret.key, which is made on first
use. Keep that key safe and separate from the backups: without it the stored passwords cannot be read, and with it
anyone holding a backup could read them. Values without the "enc:" prefix are read as they are, so files written
before encryption keep working until they are saved again (or `jobbot secrets encrypt` is run).
"""
import os

from . import DATA

PREFIX = "enc:"
_cache = {}


def _key():
    if "key" in _cache:
        return _cache["key"]
    key = os.environ.get("JOBBOT_SECRET_KEY")
    if not key:
        path = DATA / ".secret.key"
        if not path.exists():
            from cryptography.fernet import Fernet
            path.write_bytes(Fernet.generate_key())
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        key = path.read_text().strip()
    _cache["key"] = key
    return key


def _fernet():
    from cryptography.fernet import Fernet
    return Fernet(_key().encode())


def seal(value):
    """Encrypt a value for storage. Empty stays empty; an already sealed value is left as it is."""
    if not value or str(value).startswith(PREFIX):
        return value or ""
    return PREFIX + _fernet().encrypt(str(value).encode()).decode()


def open_(value):
    """The plain value of a stored one (sealed or not)."""
    if not value or not str(value).startswith(PREFIX):
        return value or ""
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(str(value)[len(PREFIX):].encode()).decode()
    except InvalidToken:
        raise RuntimeError("a stored password cannot be decrypted: JOBBOT_SECRET_KEY or DATA/.secret.key has changed")


def is_sealed(value):
    return bool(value) and str(value).startswith(PREFIX)


def seal_env_file(path):
    """Encrypt every *_APP_PASSWORD / *_TOKEN value in one .env file in place. Returns how many were sealed."""
    from pathlib import Path
    path = Path(path)
    if not path.exists():
        return 0
    lines, n = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        key, eq, value = line.partition("=")
        if eq and key.strip().endswith(("_APP_PASSWORD", "_OAUTH_TOKEN")) and value.strip() and not is_sealed(value.strip()):
            line, n = f"{key}={seal(value.strip())}", n + 1
        lines.append(line)
    if n:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return n
