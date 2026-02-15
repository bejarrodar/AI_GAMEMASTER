from __future__ import annotations

import pytest

from aigm.ops.backup_crypto import decrypt_bytes, encrypt_bytes

cryptography = pytest.importorskip("cryptography")


def test_encrypt_decrypt_roundtrip() -> None:
    plain = b"hello backup"
    passphrase = "correct horse battery staple"
    enc = encrypt_bytes(plain, passphrase)
    out = decrypt_bytes(enc, passphrase)
    assert out == plain


def test_decrypt_with_wrong_passphrase_fails() -> None:
    plain = b"secret"
    enc = encrypt_bytes(plain, "pass-a")
    with pytest.raises(Exception):
        decrypt_bytes(enc, "pass-b")
