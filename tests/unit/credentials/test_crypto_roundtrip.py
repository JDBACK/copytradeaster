import pytest
from cryptography.exceptions import InvalidTag

from copytradeaster.credentials import crypto

KEY = bytes(range(32))
OTHER_KEY = bytes(range(32, 64))


def test_roundtrip():
    blob = crypto.encrypt(KEY, b'{"signer_key": "abc"}')
    assert crypto.decrypt(KEY, blob) == b'{"signer_key": "abc"}'


def test_blob_layout_nonce_12b():
    blob = crypto.encrypt(KEY, b"x" * 100)
    assert len(blob) == 12 + 100 + 16  # nonce + plaintext + tag


def test_tamper_raises_invalid_tag():
    blob = bytearray(crypto.encrypt(KEY, b"secret"))
    blob[-1] ^= 0xFF
    with pytest.raises(InvalidTag):
        crypto.decrypt(KEY, bytes(blob))


def test_wrong_key_raises_invalid_tag():
    blob = crypto.encrypt(KEY, b"secret")
    with pytest.raises(InvalidTag):
        crypto.decrypt(OTHER_KEY, blob)


def test_unique_nonce_per_encryption():
    b1 = crypto.encrypt(KEY, b"same")
    b2 = crypto.encrypt(KEY, b"same")
    assert b1[:12] != b2[:12]


def test_key_length_validated():
    with pytest.raises(ValueError, match="32 bytes"):
        crypto.encrypt(b"short", b"x")
    with pytest.raises(ValueError, match="32 bytes"):
        crypto.decrypt(b"short", b"y" * 32)
