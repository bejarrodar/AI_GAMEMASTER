from __future__ import annotations

import base64
import os
from pathlib import Path

try:
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
except Exception:  # noqa: BLE001
    Fernet = None
    hashes = None
    PBKDF2HMAC = None


MAGIC = b"AIGMENC1"


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    if Fernet is None or hashes is None or PBKDF2HMAC is None:
        raise RuntimeError("cryptography package is required for encrypted backup operations.")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=390000,
    )
    raw = kdf.derive(passphrase.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def encrypt_bytes(data: bytes, passphrase: str) -> bytes:
    salt = os.urandom(16)
    key = _derive_key(passphrase, salt)
    token = Fernet(key).encrypt(data)
    return MAGIC + salt + token


def decrypt_bytes(data: bytes, passphrase: str) -> bytes:
    if not data.startswith(MAGIC):
        raise ValueError("Input is not an AIGM encrypted payload.")
    salt = data[len(MAGIC) : len(MAGIC) + 16]
    token = data[len(MAGIC) + 16 :]
    key = _derive_key(passphrase, salt)
    return Fernet(key).decrypt(token)


def encrypt_file(src: Path, dest: Path, passphrase: str) -> None:
    plain = src.read_bytes()
    dest.write_bytes(encrypt_bytes(plain, passphrase))


def decrypt_file(src: Path, dest: Path, passphrase: str) -> None:
    encrypted = src.read_bytes()
    plain = decrypt_bytes(encrypted, passphrase)
    dest.write_bytes(plain)
